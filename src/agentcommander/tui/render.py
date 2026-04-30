"""Renderers for the terminal UI.

Pure stdlib. Each renderer takes structured data (a PipelineEvent, a Message,
etc.) and emits formatted lines that match Claude Code's linux look.
"""
from __future__ import annotations

import textwrap
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


_streaming_state: dict = {"role": None, "had_chars": False}


def render_role_delta(role: str, delta: str) -> None:
    """Print a streaming token delta for `role` immediately.

    Called from the engine's on_role_delta hook (synchronous). Adds a
    one-time role header on first delta of a new role, then streams chars
    raw (no markdown). When the role finishes, render_event(role) clears
    and re-renders with full markdown formatting.
    """
    if not delta:
        return
    if _streaming_state["role"] != role:
        if _streaming_state["had_chars"]:
            writeln()
        writeln()
        writeln(style("role_label", f"  ▸ {role}"))
        write("    ")
        _streaming_state["role"] = role
        _streaming_state["had_chars"] = False
    # Replace newlines with newline + indent so streamed prose stays aligned.
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


def _close_streaming() -> None:
    if _streaming_state["had_chars"]:
        writeln()
    _streaming_state["role"] = None
    _streaming_state["had_chars"] = False


def render_event(evt: PipelineEvent) -> None:
    """Live-render a single PipelineEvent during a run."""
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
        _close_streaming()
        if not had_streaming:
            writeln()
            writeln(style("role_label", f"  ▸ {evt.role}"))
            if evt.output:
                from agentcommander.tui.markdown import render_markdown
                writeln(render_markdown(_short(evt.output, 4000), indent="    "))
        # If streaming did happen, the live render is already on screen —
        # don't duplicate. (For long polished output, the assistant message
        # at the end of the run will markdown-render the same content.)
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
