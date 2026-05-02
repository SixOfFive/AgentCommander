"""Renderers for the terminal UI.

Pure stdlib. Each renderer takes structured data (a PipelineEvent, a Message,
etc.) and emits formatted lines that match Claude Code's linux look.
"""
from __future__ import annotations

import textwrap
import time
from datetime import datetime, timezone
from typing import Any

from agentcommander.engine.engine import PipelineEvent
from agentcommander.tui.ansi import (
    CLEAR_LINE,
    PALETTE,
    fg256,
    style,
    term_size,
    write,
    writeln,
)
from agentcommander.tui.markdown import render_markdown
from agentcommander.tui.popouts import (
    PopoutBlock,
    add_delta as _popout_add_delta,
    begin_block as _popout_begin,
    finalize_block as _popout_finalize,
    is_popout_role,
    render_collapse as _popout_render_collapse,
    get_registry as _popout_registry,
)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _wrap(text: str, indent: str = "") -> list[str]:
    """Word-wrap to terminal width, preserving paragraph breaks."""
    cols, _ = term_size()
    width = max(40, cols - len(indent) - 2)
    out: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            out.append(indent.rstrip())
            continue
        wrapped = textwrap.wrap(
            raw_line,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        for line in wrapped or [raw_line]:
            out.append(indent + line)
    return out


def _short(text: str, n: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ─── Banner ────────────────────────────────────────────────────────────────


_LOGO = """\
  ╭──────────────────────────────────────╮
  │   AgentCommander  ·  multi-agent CLI │
  ╰──────────────────────────────────────╯
"""


def render_banner(*, version: str, providers_count: int, models_count: int,
                  working_dir: str | None) -> None:
    writeln()
    if working_dir:
        writeln(style("muted", f"  workdir: {working_dir}"))
    else:
        writeln(style("warn", "  workdir: (not set — pick one with /workdir <path>)"))
    writeln()
    for line in _LOGO.rstrip().split("\n"):
        writeln(style("accent", line))
    writeln()
    writeln(style("muted", f"  v{version}  ·  {providers_count} provider(s)  ·  "
                            f"{models_count} model(s) in TypeCast catalog"))
    writeln(style("muted", "  type /help for commands  ·  /quit to exit"))
    writeln()


# ─── User / assistant messages ─────────────────────────────────────────────


def render_user_message(text: str) -> None:
    writeln()
    writeln(style("user_label", "You ❯ ") + style("user_text", _short(text, 200)))
    if "\n" in text:
        for line in _wrap(text, indent="    ")[1:]:  # remaining lines
            writeln(style("user_text", line))


def render_assistant_message(text: str, *, markdown: bool = True) -> None:
    """Render the final assistant message.

    `markdown=True` (default) runs the text through render_markdown for
    bold/italic/code/headings/bullets/links. Set to False for raw passthrough
    when the text is already pre-formatted.
    """
    writeln()
    writeln(style("assistant_label", "● ") + style("assistant_text", "AgentCommander"))
    if markdown:
        rendered = render_markdown(text, indent="")
        writeln(rendered)
    else:
        for line in _wrap(text, indent=""):
            writeln(style("assistant_text", line))
    writeln()


def render_system_line(text: str) -> None:
    writeln(style("system_text", "  " + text))


def render_error(text: str) -> None:
    writeln(style("error", "  ⚠ " + text))


# ─── Pipeline events ───────────────────────────────────────────────────────


_streaming_state: dict = {"role": None, "had_chars": False,
                          "block": None, "started_at": 0.0}

# Bridge from on_role_end (which has the authoritative token counts) to
# the role/error PipelineEvent (which doesn't carry usage). The
# callback writes here keyed by role name; the event handler reads and
# clears. Multiple concurrent role-calls of the same role can't overlap
# because the engine is single-threaded per pipeline run.
_pending_role_usage: dict[str, dict[str, int]] = {}


def note_role_end_for_popout(role: str, *, prompt_tokens: int,
                              completion_tokens: int) -> None:
    """Called from app.py's ``_on_role_end`` so the role-event handler
    can finalize the popout with real token counts. The numbers come
    from the provider's response (Ollama ``eval_count`` / OpenRouter
    ``usage.completion_tokens``) so they're authoritative.
    """
    _pending_role_usage[role] = {
        "prompt": int(prompt_tokens or 0),
        "completion": int(completion_tokens or 0),
    }


# Strip every ANSI escape from streamed model output. The renderer wraps
# the cleaned delta in its own style codes via ``style()``; if the model
# emits its own escapes they'd otherwise pass through to the terminal.
# That would let any model — accidentally or maliciously — clear the
# screen (\x1b[2J), set the window title (\x1b]0;...\x07), reposition
# the cursor over the status bar, manipulate clipboard via OSC 52, etc.
#
# Pattern catches:
#   - CSI: ESC [ <params> <final>      (most common: colors, cursor moves)
#   - OSC: ESC ] <body> (BEL or ST)    (window title, hyperlinks, clipboard)
#   - SS3: ESC O <final>               (function-key sequences)
#   - Single-char ESC commands (RIS, DECSC, etc.)
import re as _re
_ANSI_STRIP_RX = _re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"     # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC terminated by BEL or ST
    r"|\x1bO[@-~]"                  # SS3
    r"|\x1b[@-~]"                   # other ESC + final byte (covers Fe 0x40-0x5F and Fs 0x60-0x7E like RIS \x1bc)
)


def _sanitize_model_text(s: str) -> str:
    """Strip ANSI escape sequences from streamed model text."""
    if "\x1b" not in s:
        return s
    return _ANSI_STRIP_RX.sub("", s)


def render_role_delta(role: str, delta: str) -> None:
    """Print a streaming token delta for `role` immediately.

    Called from the engine's on_role_delta hook (synchronous, on the
    worker thread). Adds a one-time role header on first delta of a new
    role, then streams chars raw (no markdown). When the role finishes,
    render_event(role) clears and re-renders with full markdown formatting.

    Wrapped in ``stdout_atomic`` so a delta render can't be interleaved
    with the main thread's status-bar repaint (which would otherwise
    cause delta text to land on the wrong row).
    """
    if not delta:
        return
    # Strip embedded ANSI escapes from the model's stream BEFORE we wrap
    # it in our own style codes. Without this, a model emitting raw
    # control sequences (intentional or not) could clear the screen,
    # reposition the cursor onto the status bar, set the window title,
    # or — on some terminals — manipulate the system clipboard via OSC.
    delta = _sanitize_model_text(delta)
    if not delta:
        return  # was nothing but escapes
    from agentcommander.tui.ansi import stdout_atomic
    with stdout_atomic():
        if _streaming_state["role"] != role:
            if _streaming_state["had_chars"]:
                writeln()
            writeln()
            writeln(style("role_label", f"  ▸ {role}"))
            write("    ")
            _streaming_state["role"] = role
            _streaming_state["had_chars"] = False
            _streaming_state["started_at"] = time.time()
            # Open a popout block for sub-agent roles. Orchestrator and
            # router stream as before — they're either the user-facing
            # reply (orchestrator) or too short to bother (router).
            if is_popout_role(role):
                _streaming_state["block"] = _popout_begin(role)
            else:
                _streaming_state["block"] = None
        # Replace newlines with newline + indent so streamed prose stays
        # aligned with the role's indent.
        if "\n" in delta:
            parts = delta.split("\n")
            for i, p in enumerate(parts):
                if i > 0:
                    writeln()
                    write("    ")
                if p:
                    write(style("assistant_text", p))
                    _streaming_state["had_chars"] = True
        else:
            write(style("assistant_text", delta))
            _streaming_state["had_chars"] = True
        # Track everything streamed so collapse() can erase the right
        # number of rows AND so /popout <id> can re-print on expand.
        block = _streaming_state["block"]
        if block is not None:
            _popout_add_delta(block, delta)


def _close_streaming() -> None:
    if _streaming_state["had_chars"]:
        writeln()
    _streaming_state["role"] = None
    _streaming_state["had_chars"] = False
    _streaming_state["block"] = None
    _streaming_state["started_at"] = 0.0


def _finalize_active_popout(*, ok: bool, error: str = "") -> PopoutBlock | None:
    """Pull the in-flight popout block off ``_streaming_state``, finalize
    it with token counts (from the on_role_end bridge) + duration, and
    return it. The caller decides whether to render the collapsed
    summary. Returns None when no popout was active (e.g. orchestrator,
    or no streaming happened).
    """
    block = _streaming_state.get("block")
    if block is None:
        return None
    started = _streaming_state.get("started_at") or 0.0
    duration_ms = int(max(0.0, (time.time() - started)) * 1000) if started else 0
    usage = _pending_role_usage.pop(block.role, {})
    _popout_finalize(
        block, ok=ok,
        prompt_tokens=usage.get("prompt", 0),
        completion_tokens=usage.get("completion", 0),
        duration_ms=duration_ms,
        error=error,
    )
    return block


def render_event(evt: PipelineEvent) -> None:
    """Live-render a single PipelineEvent during a run.

    Wrapped in ``stdout_atomic`` so a multi-write event render (e.g. an
    iteration marker that emits 2 lines, or a guard event with a label
    + reason) can't be split by a worker-thread role-delta write.
    """
    from agentcommander.tui.ansi import stdout_atomic
    with stdout_atomic():
        _render_event_inner(evt)


def _render_event_inner(evt: PipelineEvent) -> None:
    if evt.type == "iteration":
        if evt.action:
            _close_streaming()
            writeln(
                style("iter_marker", f"  ⟳ iter {evt.iteration}  →  ")
                + style("iter_action", evt.action)
            )
        elif evt.iteration is not None and evt.iteration > 0:
            pass
        elif evt.extra and "category" in evt.extra:
            writeln(style("muted", f"  router: category = {evt.extra['category']}"))
    elif evt.type == "role":
        # The role just completed. If we streamed it live, re-render the full
        # output with markdown. Otherwise (no streaming) print plain.
        had_streaming = _streaming_state["had_chars"] and _streaming_state["role"] == evt.role
        # Capture the popout block (if any) BEFORE _close_streaming clears it.
        active_block = _finalize_active_popout(ok=True) if is_popout_role(evt.role) else None
        _close_streaming()
        if active_block is not None:
            # Sub-agent role: snap to a single collapsed summary line.
            # Per spec, ok-on-completion always defaults to collapsed.
            _popout_render_collapse(active_block)
        elif is_popout_role(evt.role) and not had_streaming:
            # Replay path: this is a historical sub-agent role call — no
            # streaming happened (deltas were skipped during replay). Build
            # a synthetic collapsed block from the event so the user sees
            # ▶ <role>-<n> [stats · ok] in the transcript and can /popout
            # <id> to view the full output.
            synthetic = _popout_begin(evt.role or "?")
            if evt.output:
                _popout_add_delta(synthetic, evt.output)
            _popout_finalize(
                synthetic, ok=True,
                prompt_tokens=0,
                completion_tokens=len((evt.output or "").split()),  # rough
                duration_ms=0,
            )
            # Replay blocks are scrolled context, no streamed lines to erase
            synthetic.in_viewport = False
            from agentcommander.tui.popouts import render_summary_line
            writeln(render_summary_line(synthetic))
        elif not had_streaming:
            # Orchestrator / router replay: header + output as before.
            writeln()
            writeln(style("role_label", f"  ▸ {evt.role}"))
            if evt.output:
                from agentcommander.tui.markdown import render_markdown
                writeln(render_markdown(_short(evt.output, 4000), indent="    "))
        # If streaming did happen for orchestrator/router, the live render is
        # already on screen — don't duplicate. (For long polished output, the
        # assistant message at the end of the run will markdown-render the
        # same content.)
    elif evt.type == "role_delta":
        render_role_delta(evt.role or "?", evt.delta or "")
    elif evt.type == "tool":
        _close_streaming()
        marker = style("tool_ok", "✓") if evt.ok else style("tool_err", "✗")
        writeln(f"  {marker} " + style("tool_marker", f"tool:{evt.tool}"))
        if evt.error:
            writeln(style("tool_err", "    " + evt.error))
        elif evt.output:
            for line in _wrap(_short(evt.output, 600), indent="    "):
                writeln(style("system_text", line))
    elif evt.type == "guard":
        _close_streaming()
        writeln(style("guard_label", f"  ⌫ guard:{evt.family}  ") +
                style("muted", f"({evt.reason})"))
    elif evt.type == "swap":
        # Rate-limit fast-swap on an OR provider: vote shifted, a
        # different model picked from the catalog, no wait. One-line
        # display per swap so the user can see the model rotation.
        _close_streaming()
        from_m = evt.swap_from_model or "?"
        to_m = evt.swap_to_model or "?"
        attempt = evt.retry_attempt or 0
        max_a = evt.retry_max or "?"
        writeln(
            style("warn", "  ↪ swap on ") +
            style("accent", evt.role or "?") +
            style("muted", f":  {from_m}  →  ") +
            style("accent", to_m) +
            style("muted", f"  (attempt {attempt}/{max_a})")
        )
    elif evt.type == "retry":
        # Provider rate-limit backoff. Initial event for an attempt has
        # the full wait_seconds; subsequent countdown events have the
        # remaining seconds. Format both as a single user-visible line —
        # the engine throttles announce frequency so this doesn't flood.
        _close_streaming()
        wait = evt.retry_wait_seconds or 0
        attempt = evt.retry_attempt or 0
        max_a = evt.retry_max or 5
        # Compact mm:ss format for waits ≥ 60s.
        if wait >= 60:
            mm, ss = divmod(wait, 60)
            time_str = f"{mm}:{ss:02d}"
        else:
            time_str = f"{wait}s"
        action = evt.reason or "?"
        writeln(
            style("warn", "  ◌ rate-limited") +
            style("muted", f"  ({action})  retrying in ") +
            style("warn", time_str) +
            style("muted", f"  attempt {attempt}/{max_a}")
        )
    elif evt.type == "done":
        _close_streaming()
        if evt.final:
            render_assistant_message(evt.final)
    elif evt.type == "error":
        # Errored role: finalize the popout (if any) but DON'T collapse —
        # spec says failed agents stay expanded by default so the user
        # sees the error inline. The error line is still rendered below.
        if evt.role and is_popout_role(evt.role):
            block = _finalize_active_popout(ok=False, error=evt.error or "")
            if block is not None:
                # Append the summary line under the streamed content so
                # the user can collapse it later (Tab + Space, click, or
                # /popout <id>) when they're done reading.
                _close_streaming()
                from agentcommander.tui.popouts import render_summary_line
                writeln(render_summary_line(block))
                writeln(style("error", f"  ⚠ {evt.error}"))
                writeln()
                return
        _close_streaming()
        writeln()
        writeln(style("error", f"  ⚠ {evt.error}"))
        writeln()


# ─── Tables (providers, roles, etc.) ──────────────────────────────────────


def render_table(headers: list[str], rows: list[list[str]], *,
                 indent: str = "  ") -> None:
    if not rows:
        writeln(indent + style("muted", "(empty)"))
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    sep = "  "
    header_line = sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    writeln(indent + style("muted", header_line))
    writeln(indent + style("rule", sep.join("─" * w for w in widths)))
    for r in rows:
        line = sep.join(cell.ljust(widths[i]) for i, cell in enumerate(r))
        writeln(indent + line)


# ─── Status line ──────────────────────────────────────────────────────────


def render_status_line(*, working_dir: str | None, default_model: str | None,
                       running: bool = False) -> None:
    """Print a single-line status footer (between turns)."""
    parts: list[str] = []
    parts.append(f"workdir: {working_dir or '(none)'}")
    parts.append(f"model: {default_model or '(none)'}")
    if running:
        parts.append("status: running")
    line = "  ".join(parts)
    writeln(style("muted", "  " + line))


# Suppress unused import warning — `datetime` reserved for future timestamp lines.
_ = (datetime, timezone, fg256, PALETTE, Any)
