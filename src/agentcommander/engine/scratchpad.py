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
    if errors:
        parts.append(f"**Errors encountered:** {len(errors)}")

    # 4. Step-by-step fallback (deduped)
    meaningful = [e for e in pad
                  if (e.role != "tool" or e.action == "execute")
                  and e.role != "debugger"
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
