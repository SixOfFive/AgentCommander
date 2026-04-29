"""Scratchpad helpers — compact, push nudges, build final output.

Ported from EngineCommander/src/main/orchestration/engine-output.ts (final
output assembly) plus the nudge / compaction utilities from engine.ts.

The scratchpad is the engine's working memory: every role call and tool
result is appended as a `ScratchpadEntry`. `compact_scratchpad` produces a
trimmed view for inclusion in role prompts.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from agentcommander.types import ScratchpadEntry

CONTENT_ROLES: frozenset[str] = frozenset({
    "vision", "researcher", "translator", "planner", "architect",
    "reviewer", "data_analyst", "summarizer", "refactorer", "coder", "audio",
})


_NEXT_DIRECTIVE_RX = re.compile(r"\n?\[NEXT:\s[^\]]*]")


def push_nudge(scratchpad: list[ScratchpadEntry], iteration: int,
               reason: str, output: str) -> None:
    """Append a `system_nudge` entry — guards use this to redirect the orchestrator."""
    scratchpad.append(ScratchpadEntry(
        step=iteration,
        role="tool",
        action="system_nudge",
        input=reason,
        output=output,
        timestamp=time.time(),
    ))


def compact_scratchpad(scratchpad: list[ScratchpadEntry], *, tail: int = 20,
                       max_input: int = 200, max_output: int = 400) -> str:
    """Compact the last `tail` entries into a string for role prompt inclusion."""
    recent = scratchpad[-tail:] if tail > 0 else scratchpad
    lines: list[str] = []
    for e in recent:
        inp = (e.input or "")[:max_input]
        out = (e.output or "")[:max_output]
        prefix = f"step {e.step} {e.role}/{e.action}: "
        if inp:
            lines.append(f"{prefix}in={inp} out={out}")
        else:
            lines.append(f"{prefix}out={out}")
    return "\n".join(lines)


def _clean_for_user(text: str) -> str:
    return _NEXT_DIRECTIVE_RX.sub("", text).rstrip().strip()


def _same_head(a: str, b: str, n: int = 120) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()[:n]  # noqa: E731
    return norm(a) == norm(b)


def build_final_output(scratchpad: Iterable[ScratchpadEntry]) -> str:
    """Build a user-visible final output from scratchpad entries.

    Priority order (matches EC):
      1. Last real summarizer output (not compaction artifacts)
      2. Last content-role output (vision/researcher/...)
      3. Execution stdout from successful runs
      4. Step-by-step report
    """
    pad = list(scratchpad)

    # 1. Summarizer
    summary = next((e for e in reversed(pad)
                    if e.role == "summarizer" and e.action != "compress"), None)
    if summary and len(summary.output) > 50:
        return _clean_for_user(summary.output)

    # 2. Content-role last output
    content_entries = [
        e for e in pad
        if e.role in CONTENT_ROLES
        and e.action not in ("compress", "system_nudge")
        and isinstance(e.output, str)
        and len(e.output.strip()) > 80
    ]
    if content_entries:
        return _clean_for_user(content_entries[-1].output)

    # 3. Execution stdout from success
    files = [e for e in pad
             if e.action == "write_file" and "Successfully" in (e.output or "")]
    successful_execs = [e for e in pad
                        if e.action == "execute"
                        and "successfully" in (e.output or "")
                        and "SyntaxError" not in (e.output or "")]
    errors = [e for e in pad
              if (("Error" in (e.output or "")) or ("failed" in (e.output or "")))
              and e.action != "system_nudge"]
    # Failed executions specifically — different surfacing path. The user
    # cares about exit code + stderr, not the step echo (which is what was
    # happening pre-Bug-C-followup: just "### Step 1: tool/execute\nexit
    # code 1" with no actionable content).
    failed_execs = [e for e in pad
                    if e.action == "execute"
                    and "successfully" not in (e.output or "").lower()
                    and (e.output or "").strip()]

    # 3b. Other successful tool outputs that still carry a meaningful answer.
    # list_dir, read_file, and fetch all return useful content the user
    # asked for; without these, a question like "list files in this dir"
    # would fall through to the step-by-step echo of the router entry,
    # forcing the chat fallback to compensate (extra model call).
    successful_tool_outputs = [
        e for e in pad
        if e.role == "tool"
        and e.action in ("list_dir", "read_file", "fetch")
        and "successfully" in (e.output or "").lower()
    ]

    parts: list[str] = []
    if successful_execs:
        last_exec = successful_execs[-1]
        m = re.search(r"successfully[^:]*:\n([\s\S]+)", last_exec.output or "")
        stdout = _clean_for_user((m.group(1) if m else "").strip())
        if stdout and len(stdout) > 5:
            parts.append(f"**Execution Output:**\n```\n{stdout[:3000]}\n```")
    if files:
        parts.append(f"**Files created:** {', '.join(f.input for f in files)}")
    if successful_execs:
        parts.append(f"**Successful executions:** {len(successful_execs)}")
    if successful_tool_outputs:
        # Surface the most-recent tool output as the actual answer body.
        last = successful_tool_outputs[-1]
        m = re.search(r"successfully[^:]*:\n([\s\S]+)", last.output or "",
                      re.IGNORECASE)
        body = _clean_for_user((m.group(1) if m else (last.output or "")).strip())
        # Fetch outputs are often raw HTML/JSON — cap aggressively. list_dir
        # / read_file are usually short and we want them in full (capped 3k).
        if last.action == "fetch":
            body = body[:1500] + ("\n…[truncated]" if len(body) > 1500 else "")
        else:
            body = body[:3000]
        if body:
            label = {"list_dir": "Directory listing",
                     "read_file": "File contents",
                     "fetch": "Fetched content"}.get(last.action, last.action)
            input_hint = (last.input or "").strip()
            header = f"**{label}**"
            if input_hint:
                header += f" ({input_hint[:120]})"
            parts.append(f"{header}:\n```\n{body}\n```")
    if failed_execs:
        # Surface the last failed execute with its output (stderr + exit
        # code message). Without this, a failed run just shows "Step 1:
        # tool/execute\nexit code 1" in the step echo, which doesn't tell
        # the user what went wrong.
        last_fail = failed_execs[-1]
        body = (last_fail.output or "").strip()
        # Trim if very long (some scripts spew megabytes of stderr)
        if len(body) > 3000:
            body = body[:3000] + "\n…[truncated]"
        # Also surface the script that failed (decision.input → entry.input)
        # so the user can see what was attempted.
        script = (last_fail.input or "").strip()
        header = "**Execution failed**"
        parts.append(f"{header}:\n```\n{body}\n```")
        if script and len(script) < 2000:
            parts.append(f"**Script that failed:**\n```\n{script[:2000]}\n```")
    elif errors:
        # Generic error count for non-execute tool failures (kept the old
        # behavior so we don't regress).
        parts.append(f"**Errors encountered:** {len(errors)}")

    # 4. Step-by-step fallback (deduped). Skip:
    #   - router/classify: internal scaffolding, never useful as final output
    #     (and surfacing it triggers _is_router_echo, forcing chat fallback)
    #   - tool entries other than execute (their content is surfaced above
    #     when applicable; otherwise omit rather than dump raw scratchpad)
    #   - debugger / system_nudge: orchestration noise
    meaningful = [e for e in pad
                  if (e.role != "tool" or e.action == "execute")
                  and e.role not in ("router", "debugger")
                  and e.action != "system_nudge"][-6:]
    deduped: list[ScratchpadEntry] = []
    for e in meaningful:
        if deduped and _same_head(deduped[-1].output or "", e.output or ""):
            continue
        deduped.append(e)
    for e in deduped[-3:]:
        if e.action == "execute" and "successfully" in (e.output or "") and parts and parts[0].startswith("**Execution Output"):
            continue
        parts.append(f"### Step {e.step}: {e.role}/{e.action}\n"
                     f"{_clean_for_user(e.output or '')[:3000]}")

    if parts:
        return "\n\n".join(parts)
    return "The pipeline completed but produced no summary. Check the pipeline steps for details."
