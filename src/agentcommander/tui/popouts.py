"""Role-popout system: collapsible blocks for sub-agent role calls.

Per the user's spec ("/popout" feature):
  - Each sub-agent role-call (researcher, coder, reviewer, etc.) gets its
    own collapsible "block" in the live conversation view.
  - Streaming text is visible WHILE the role runs.
  - When the role emits ``done``, the streamed content snaps shut to a
    single summary line: ``▶ researcher-2 [12.3s · 2.8k tok · ok]``.
  - Tool calls invoked by that role stay visible at all times — they
    have observable side effects the user already saw fire.
  - Failed roles stay EXPANDED (the user wants to see the error).
  - Three interaction surfaces all work:
      mouse  — click on the summary line  (xterm SGR mouse)
      keyboard — Tab cycles focus, Space/Enter toggles, Esc clears
      slash    — /popout <id>  /popout list  /popout expand|collapse all

Rendering strategy: append-only with cursor-up + erase-to-EOL. When the
block snaps shut we walk back N lines (counted while streaming) and
``\\x1b[J`` (erase below); the status bar is then repainted by its own
ticker. This works for blocks still in the viewport. Blocks that have
already scrolled past the top are left as-is (the slash command can
still toggle them in the registry, and re-print the content if the user
expands them later).

Cross-cutting concerns:
  - Mirror viewers reconstruct their own popout registry from the
    pipeline_events stream. Each viewer's collapsed-state map is local.
  - Replay (resumed conversations) loads historical role calls as
    collapsed blocks; tool calls from the same role stay visible.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from agentcommander.tui.ansi import (
    CLEAR_LINE,
    fg256,
    style,
    supports_color,
    term_size,
    write,
    writeln,
)

# ─── Scope: which roles get popouts ────────────────────────────────────────

# Per spec: "just sub agents" — orchestrator stays fully visible (it IS
# the assistant's reply), router is too short-lived to bother with.
# Everything else collapses.
_NON_POPOUT_ROLES = frozenset({"orchestrator", "router"})


def is_popout_role(role: str | None) -> bool:
    """True iff a role-call for ``role`` should be wrapped in a popout block."""
    if not role:
        return False
    return role.lower() not in _NON_POPOUT_ROLES


# ─── Block data ────────────────────────────────────────────────────────────


_INDENT = "    "  # matches render_role_delta's 4-space content indent


@dataclass
class PopoutBlock:
    """One collapsible block tracking a single role-call.

    ``id`` is ``<role>-<n>`` where n is the 1-indexed call order in the
    current run (so multiple ``researcher`` calls become researcher-1,
    researcher-2, ...). This is what shows in the slash command list.

    ``line_count`` is the cumulative number of terminal rows the block's
    streamed body has occupied since the role header was printed. It's
    incremented every time ``add_delta`` writes content, accounting for
    embedded newlines AND terminal-side line wrapping. Used by collapse()
    to walk the cursor back the right number of lines before erasing.

    Status fields (ok / error / prompt_tokens / completion_tokens /
    duration_ms) are populated when ``finalize()`` is called from the
    role-end event handler. Until then status is "running".
    """

    id: str
    role: str
    model: str = ""
    status: str = "running"  # "running" | "ok" | "error"
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0

    # Render bookkeeping
    line_count: int = 0
    content: str = ""  # captured streamed content for re-expand later
    collapsed: bool = False
    in_viewport: bool = True  # set False when we know the block scrolled off
    # Captured terminal width when the block started — used as the
    # canonical wrap divisor regardless of terminal resizes mid-render.
    cols_at_start: int = 0


@dataclass
class PopoutRegistry:
    """Per-process registry. The TUI app and each mirror viewer have one."""

    blocks: list[PopoutBlock] = field(default_factory=list)
    by_id: dict[str, PopoutBlock] = field(default_factory=dict)
    role_counts: dict[str, int] = field(default_factory=dict)
    focus_id: Optional[str] = None  # currently keyboard-focused block id
    # Map of (row_start, row_end) → block id, only for blocks currently
    # in the viewport. Updated when a block is created and erased on
    # collapse/scroll-out. Used by mouse-click dispatch.
    _row_index: dict[int, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def reset(self) -> None:
        """Wipe state — called at the start of every new pipeline run."""
        with self.lock:
            self.blocks.clear()
            self.by_id.clear()
            self.role_counts.clear()
            self.focus_id = None
            self._row_index.clear()

    def next_id_for(self, role: str) -> str:
        """Allocate ``<role>-<n>`` where n is the 1-indexed call count."""
        with self.lock:
            n = self.role_counts.get(role, 0) + 1
            self.role_counts[role] = n
            return f"{role}-{n}"

    def register(self, block: PopoutBlock) -> None:
        with self.lock:
            self.blocks.append(block)
            self.by_id[block.id] = block

    def get(self, block_id: str) -> Optional[PopoutBlock]:
        return self.by_id.get(block_id)

    def list_open(self) -> list[PopoutBlock]:
        """Blocks the user can interact with (any non-running)."""
        with self.lock:
            return [b for b in self.blocks if b.status != "running"]

    def cycle_focus(self, direction: int) -> Optional[str]:
        """Move keyboard focus forward (+1) or backward (-1) through
        in-viewport non-running blocks. Returns the new focused id (or
        None if no blocks are eligible)."""
        with self.lock:
            eligible = [b for b in self.blocks
                        if b.status != "running" and b.in_viewport]
            if not eligible:
                self.focus_id = None
                return None
            ids = [b.id for b in eligible]
            if self.focus_id not in ids:
                self.focus_id = ids[0] if direction >= 0 else ids[-1]
            else:
                idx = (ids.index(self.focus_id) + direction) % len(ids)
                self.focus_id = ids[idx]
            return self.focus_id

    def clear_focus(self) -> None:
        with self.lock:
            self.focus_id = None


# Module-level singleton for the primary TUI. Mirror viewers create their
# own instance via PopoutRegistry().
_REGISTRY = PopoutRegistry()


def get_registry() -> PopoutRegistry:
    """The primary TUI's registry."""
    return _REGISTRY


# ─── Wrap-aware line counting ──────────────────────────────────────────────


def _count_visible_lines(text: str, cols: int, indent_cols: int) -> int:
    """Estimate how many terminal rows ``text`` will occupy when streamed
    into a region indented by ``indent_cols`` columns and wrapped at
    ``cols`` total columns.

    The estimate is conservative on the LOW side (under-counts), so that
    if it's wrong the collapse erases too FEW lines (leaving a bit of
    streamed content visible) rather than too MANY (clobbering content
    above the block). Better to leak a tiny visual artifact than corrupt
    the scrollback above us.

    Conservative rules:
      - Each ``\\n`` in text is exactly one new row.
      - On each segment between newlines, we add ``len(seg) // body_cols``
        wrap-induced rows (integer division — drops the partial last row,
        which is what the cursor sits on, not a wrapped continuation).
    """
    if cols <= indent_cols + 1:
        return text.count("\n")
    body = max(1, cols - indent_cols)
    rows = 0
    for seg in text.split("\n"):
        if seg:
            rows += len(seg) // body
        rows += 1
    # The first segment doesn't contribute a leading newline — we counted
    # one extra. (split("a\nb") → ["a", "b"], 2 segs, 1 \n; we counted 2.)
    return max(0, rows - 1)


# ─── Summary line ──────────────────────────────────────────────────────────


def _fmt_duration(ms: int) -> str:
    if ms <= 0:
        return "0s"
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return f"{n} tok"
    if n < 1_000_000:
        return f"{n / 1000:.1f}k tok"
    return f"{n / 1_000_000:.1f}M tok"


def _arrow(collapsed: bool) -> str:
    return "▶" if collapsed else "▼"


def render_summary_line(block: PopoutBlock, *, focused: bool = False) -> str:
    """Format the one-line collapsed/expanded header for ``block``.

    Format:
        ▶ researcher-2 [12.3s · 2,847 tok · ok]
        ▼ coder-1 [4.2s · 512 tok · ok]
        ▼ critic-1 [2.1s · 200 tok · ✗ provider error: timeout]

    The arrow flips with ``block.collapsed``. When ``focused`` is True
    (keyboard focus), the line gets an inverted-style highlight so the
    user can see Tab moved.
    """
    arrow = _arrow(block.collapsed)
    parts = [_fmt_duration(block.duration_ms),
             _fmt_tokens(block.completion_tokens)]
    if block.status == "ok":
        parts.append("ok")
    elif block.status == "error":
        err = block.error or "error"
        # Truncate error text so the summary line doesn't break the
        # column. Full error stays visible in the expanded body.
        if len(err) > 60:
            err = err[:57] + "…"
        parts.append(f"✗ {err}")
    else:
        parts.append("running…")
    inner = " · ".join(parts)
    text = f"  {arrow} {block.id} [{inner}]"
    if focused and supports_color():
        # Subtle highlight: inverted style on the bullet + id.
        text = (
            "  " + style("accent", arrow) + " "
            + style("user_label", block.id)
            + " "
            + style("muted", f"[{inner}]")
        )
    elif supports_color():
        text = (
            "  " + style("accent", arrow) + " "
            + style("role_label", block.id)
            + " "
            + style("muted", f"[{inner}]")
        )
    return text


# ─── Lifecycle hooks (called by render.py) ─────────────────────────────────


def begin_block(role: str, model: str = "",
                 registry: PopoutRegistry | None = None) -> PopoutBlock:
    """Allocate + register a new popout block for a starting role.

    Caller is responsible for printing the role header (the existing
    ``render_role_delta`` already does that). This just claims the id
    and resets the line counter.
    """
    reg = registry or _REGISTRY
    block_id = reg.next_id_for(role)
    cols, _ = term_size()
    block = PopoutBlock(
        id=block_id, role=role, model=model,
        cols_at_start=cols, line_count=0,
    )
    reg.register(block)
    return block


def add_delta(block: PopoutBlock, delta: str) -> None:
    """Update the block's content buffer + line-count accounting.

    The actual on-screen write still happens in ``render_role_delta`` —
    this just observes the stream so we can later collapse and replay.
    """
    if not delta:
        return
    block.content += delta
    cols = block.cols_at_start or term_size()[0]
    block.line_count += _count_visible_lines(delta, cols, indent_cols=4)


def finalize_block(block: PopoutBlock, *,
                    ok: bool,
                    prompt_tokens: int = 0,
                    completion_tokens: int = 0,
                    duration_ms: int = 0,
                    error: str = "") -> None:
    """Record final status. Doesn't paint — the caller decides whether
    to collapse-on-success vs leave-expanded-on-error and invoke
    ``render_collapse`` / ``render_expand`` accordingly.
    """
    block.status = "ok" if ok else "error"
    block.error = error
    block.prompt_tokens = max(0, int(prompt_tokens or 0))
    block.completion_tokens = max(0, int(completion_tokens or 0))
    block.duration_ms = max(0, int(duration_ms or 0))
    # Per spec: failures stay expanded by default.
    block.collapsed = ok


# ─── Painting (cursor-up + erase + replace with summary) ───────────────────


def _walk_back_and_erase(line_count: int) -> None:
    """Move cursor up ``line_count`` rows (column 1) and erase to end of
    display. The status bar repaints itself on its own ticker so we
    don't need to restore it explicitly here.
    """
    if line_count <= 0:
        return
    # ``\x1b[<N>F`` = cursor preceding line, column 1, N times.
    # ``\x1b[0J`` = erase from cursor to end of display.
    write(f"\x1b[{line_count}F\x1b[0J")


def render_collapse(block: PopoutBlock) -> None:
    """Paint: walk back over the streamed content, erase it, print the
    collapsed summary line in its place.

    Safe to call only on the rendering thread (typically inside
    ``stdout_atomic`` from the caller in render.py).

    If the block has scrolled out of the viewport — we can't undo writes
    we've already lost — we just append the summary line below current
    position. The user's view degrades gracefully.
    """
    block.collapsed = True
    cols, rows = term_size()
    # If the block is taller than the viewport, we've already scrolled
    # off the role header — abandon the erase and append.
    if block.line_count >= max(1, rows - 4):
        block.in_viewport = False
        writeln(render_summary_line(block))
        return
    _walk_back_and_erase(block.line_count + 1)  # +1 for the role header line
    # The role header was at "line_count + 1" rows up — by walking back
    # that far we landed on (and just erased) it as well. Replace the
    # whole block with just the one summary line.
    writeln(render_summary_line(block))


def render_expand_inline(block: PopoutBlock) -> None:
    """Paint: re-print the captured content under the summary line.

    Used when the user toggles a collapsed block expand-ward via
    slash command, mouse click, or keyboard. We don't try to slot the
    expansion back at the original screen position (terminal scrollback
    is one-way); we just append the content fresh below the cursor.
    """
    block.collapsed = False
    # Header + content
    writeln()
    writeln(style("role_label", f"  ▸ {block.role}") + style("muted", f"  ({block.id})"))
    indent = _INDENT
    for line in (block.content or "").split("\n"):
        writeln(indent + style("assistant_text", line))
    # Then re-print the summary so the user can collapse again.
    writeln(render_summary_line(block))


def toggle_block(block_id: str, registry: PopoutRegistry | None = None) -> bool:
    """Flip ``collapsed`` and repaint inline. Returns True on success,
    False if the id is unknown.
    """
    reg = registry or _REGISTRY
    block = reg.get(block_id)
    if block is None or block.status == "running":
        return False
    if block.collapsed:
        render_expand_inline(block)
    else:
        # If the block is far above us, in-place collapse is impossible —
        # just mark it so future expands re-print fresh. The visible
        # streamed content will scroll away naturally.
        if not block.in_viewport:
            block.collapsed = True
        else:
            render_collapse(block)
    return True


def list_block_summaries(
    registry: PopoutRegistry | None = None,
) -> list[tuple[str, str, str, str]]:
    """For ``/popout list``: returns rows of (id, role, status, summary)."""
    reg = registry or _REGISTRY
    out: list[tuple[str, str, str, str]] = []
    with reg.lock:
        for b in reg.blocks:
            status = b.status
            if status == "ok":
                detail = f"{_fmt_duration(b.duration_ms)} · {_fmt_tokens(b.completion_tokens)}"
            elif status == "error":
                detail = f"✗ {b.error[:50]}"
            else:
                detail = "running…"
            shown = "collapsed" if b.collapsed else "expanded"
            out.append((b.id, b.role, status, f"{shown} · {detail}"))
    return out


__all__ = [
    "PopoutBlock",
    "PopoutRegistry",
    "is_popout_role",
    "get_registry",
    "begin_block",
    "add_delta",
    "finalize_block",
    "render_summary_line",
    "render_collapse",
    "render_expand_inline",
    "toggle_block",
    "list_block_summaries",
]
