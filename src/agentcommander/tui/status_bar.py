"""Persistent bottom status bar.

Reserves the last two terminal rows via an ANSI scroll-region escape and
keeps a live status line painted there:

  bottom-left:  current role/model running
  bottom-right: tokens-in / tokens-out  ·  context now / [chain min cap]

Pure stdlib. Uses standard ANSI sequences:

  ESC[<top>;<bottom>r    set scroll region (1-based, inclusive)
  ESC[s                  save cursor
  ESC[u                  restore cursor
  ESC[<row>;<col>H       move cursor

Drawing convention:
  - The scroll region is rows 1 .. (H-2)
  - Status occupies rows (H-1) .. H
    (row H-1 = separator rule, row H = the live data)

When the user resizes the terminal we re-pin the scroll region. We poll for
size changes lazily on each redraw to keep this stdlib-only (no signal handler
required, which keeps it cross-platform-clean on Windows).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from agentcommander.tui.ansi import (
    DIM,
    RESET,
    fg256,
    style,
    supports_color,
    term_size,
    write,
)


@dataclass
class StatusState:
    role: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    context_now: int = 0           # tokens we're currently sending in messages
    context_cap_min: int | None = None
    pipeline_running: bool = False
    workdir: str | None = None
    diff_picks: dict[str, str] = field(default_factory=dict)


class StatusBar:
    """Owns the bottom 2 rows of the terminal."""

    def __init__(self) -> None:
        self.state = StatusState()
        self._cols, self._rows = term_size()
        self._installed = False
        self._enabled = sys.stdout.isatty() and supports_color()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def install(self) -> None:
        """Reserve the bottom 2 rows and paint an empty status."""
        if not self._enabled or self._installed:
            return
        self._cols, self._rows = term_size()
        self._set_scroll_region(1, max(1, self._rows - 2))
        # Move cursor to top-of-scroll-region so subsequent prints don't land
        # on a status row mid-redraw.
        write(f"\x1b[{1};{1}H")
        self._installed = True
        self.redraw()

    def uninstall(self) -> None:
        """Restore the full screen as the scroll region; clear status rows."""
        if not self._enabled or not self._installed:
            return
        self._set_scroll_region(1, self._rows)
        # Wipe the bottom 2 rows.
        write(f"\x1b[{self._rows - 1};{1}H\x1b[2K")
        write(f"\x1b[{self._rows};{1}H\x1b[2K")
        # Cursor home.
        write(f"\x1b[{1};{1}H")
        self._installed = False

    def _set_scroll_region(self, top: int, bottom: int) -> None:
        write(f"\x1b[{top};{bottom}r")

    # ── State updates ──────────────────────────────────────────────────────

    def set_role(self, role: str | None, model: str | None) -> None:
        self.state.role = role
        self.state.model = model
        self.redraw()

    def add_tokens(self, *, prompt: int = 0, completion: int = 0) -> None:
        self.state.tokens_in += max(0, prompt)
        self.state.tokens_out += max(0, completion)
        self.redraw()

    def reset_run(self) -> None:
        self.state.role = None
        self.state.model = None
        self.state.tokens_in = 0
        self.state.tokens_out = 0
        self.state.context_now = 0
        self.state.pipeline_running = False
        self.redraw()

    def set_running(self, running: bool) -> None:
        self.state.pipeline_running = running
        self.redraw()

    def set_context(self, *, now: int | None = None,
                    cap_min: int | None = None) -> None:
        if now is not None:
            self.state.context_now = now
        if cap_min is not None:
            self.state.context_cap_min = cap_min
        self.redraw()

    def set_workdir(self, workdir: str | None) -> None:
        self.state.workdir = workdir
        self.redraw()

    # ── Redraw ─────────────────────────────────────────────────────────────

    def redraw(self) -> None:
        if not self._enabled or not self._installed:
            return

        # Re-detect size in case the terminal was resized.
        cols, rows = term_size()
        if (cols, rows) != (self._cols, self._rows):
            self._cols, self._rows = cols, rows
            self._set_scroll_region(1, max(1, rows - 2))

        # Save cursor + position state so the user's input/output isn't
        # disturbed when we paint over the bottom rows.
        write("\x1b7")  # save cursor (DECSC)

        rule_color = fg256(238) if supports_color() else ""
        rule = ("─" * cols) if rule_color else ("-" * cols)
        sep_row = max(1, rows - 1)
        data_row = max(1, rows)

        write(f"\x1b[{sep_row};1H\x1b[2K")  # move + clear line
        if rule_color:
            write(f"{rule_color}{rule}{RESET}")
        else:
            write(rule)

        write(f"\x1b[{data_row};1H\x1b[2K")
        write(self._compose_data_row(cols))

        write("\x1b8")  # restore cursor (DECRC)
        sys.stdout.flush()

    def _compose_data_row(self, cols: int) -> str:
        """Build the live data line. Left = role/model, right = tokens/context."""
        s = self.state
        # ── Left ──
        if s.role and s.model:
            verb = "▸" if s.pipeline_running else "·"
            left = f"{verb} {s.role} → {s.model}"
        elif s.pipeline_running:
            left = "▸ ..."
        else:
            left = "· idle"
        if s.workdir:
            wd = s.workdir
            if len(wd) > 32:
                wd = "…" + wd[-31:]
            left = f"{left}    [{wd}]"

        # ── Right ──
        ctx_part = ""
        if s.context_now or s.context_cap_min:
            cap = f"[{_humanize_tokens(s.context_cap_min)}]" if s.context_cap_min else ""
            ctx_part = f"ctx {_humanize_tokens(s.context_now)} {cap}".strip()
        token_part = f"in {_humanize_tokens(s.tokens_in)}  out {_humanize_tokens(s.tokens_out)}"
        right_parts = [p for p in (token_part, ctx_part) if p]
        right = "  ·  ".join(right_parts)

        # Compose, padding the gap. Account for ANSI codes only roughly.
        plain_left = left
        plain_right = right
        gap = max(1, cols - len(plain_left) - len(plain_right))

        if supports_color():
            left_styled = style("accent", left.split(" ", 1)[0]) + " " + left.split(" ", 1)[1] \
                if " " in left else style("accent", left)
            right_styled = style("muted", right) if right else ""
            return left_styled + (" " * gap) + right_styled
        return plain_left + (" " * gap) + plain_right


def _humanize_tokens(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".rstrip("0").rstrip(".") + "k" if False else f"{n // 1000}k" if n % 1000 == 0 else f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}m"


# Module-level singleton. The TUI grabs one and feeds it events.
_global: StatusBar | None = None


def get_status_bar() -> StatusBar:
    global _global
    if _global is None:
        _global = StatusBar()
    return _global


# Suppress unused-DIM warning (reserved for future styling tweaks)
_ = DIM
