"""Persistent bottom status bar + bottom-anchored input row.

Reserves the last THREE terminal rows via an ANSI scroll-region escape:

    rows 1 .. (H-3)   scroll region for messages (content scrolls UP)
    row H-2           thin separator rule
    row H-1           live status (right-aligned: role → model · tokens · ctx)
    row H             input prompt (where the user types)

Drawing convention:
  - Cursor is parked at the bottom of the scroll region (row H-3) between
    inputs. Newline at H-3 → scroll region shifts up by one line, top row
    falls off, cursor stays at H-3. So every print/writeln naturally adds
    to the bottom and pushes the rest upward — the terminal-log feel.
  - Status redraws save+restore cursor so they don't disturb the scroll
    region's cursor position.
  - `read_line_at_bottom` parks the cursor on row H, calls input(), then
    restores cursor to row H-3 so subsequent output streams in correctly.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from agentcommander.tui.ansi import (
    BOLD,
    DIM,
    RESET,
    fg256,
    style,
    supports_color,
    term_size,
    write,
)

# Number of reserved rows at the bottom (rule + status + input).
RESERVED_ROWS = 3


@dataclass
class StatusState:
    role: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    context_now: int = 0
    context_cap_min: int | None = None
    pipeline_running: bool = False
    workdir: str | None = None
    diff_picks: dict[str, str] = field(default_factory=dict)
    # When a pipeline is running, the REPL hands us the user's in-flight typing
    # so the bar can keep it visible on the input row across redraws. None ==
    # idle (no pre-typed buffer to display).
    pending_input: str | None = None


class StatusBar:
    """Owns the bottom RESERVED_ROWS rows of the terminal."""

    def __init__(self) -> None:
        self.state = StatusState()
        self._cols, self._rows = term_size()
        self._installed = False
        self._enabled = sys.stdout.isatty() and supports_color()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def scroll_bottom_row(self) -> int:
        """Last row inside the scroll region (1-indexed)."""
        return max(1, self._rows - RESERVED_ROWS)

    def status_row(self) -> int:
        """Row where the live status data lives."""
        return max(1, self._rows - 1)

    def rule_row(self) -> int:
        return max(1, self._rows - 2)

    def input_row(self) -> int:
        return max(1, self._rows)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def install(self) -> None:
        """Reserve the bottom rows and park the cursor at the scroll-region bottom."""
        if not self._enabled or self._installed:
            return
        self._cols, self._rows = term_size()
        self._set_scroll_region(1, self.scroll_bottom_row())
        # Park at scroll-region bottom so subsequent writes naturally push
        # existing content upward instead of overwriting from the top.
        write(f"\x1b[{self.scroll_bottom_row()};1H")
        self._installed = True
        self.redraw()

    def uninstall(self) -> None:
        """Restore the full screen as the scroll region; clear the reserved rows."""
        if not self._enabled or not self._installed:
            return
        self._set_scroll_region(1, self._rows)
        for r in (self.rule_row(), self.status_row(), self.input_row()):
            write(f"\x1b[{r};1H\x1b[2K")
        write(f"\x1b[{1};1H")
        self._installed = False

    def park_cursor(self) -> None:
        """Move the cursor to the bottom of the scroll region. Call before
        emitting normal content so it scrolls up correctly."""
        if not self._enabled or not self._installed:
            return
        write(f"\x1b[{self.scroll_bottom_row()};1H")

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

    def set_context(self, *, now: int | None = None, cap_min: int | None = None) -> None:
        if now is not None:
            self.state.context_now = now
        if cap_min is not None:
            self.state.context_cap_min = cap_min
        self.redraw()

    def set_workdir(self, workdir: str | None) -> None:
        self.state.workdir = workdir
        self.redraw()

    def set_pending_input(self, text: str | None) -> None:
        """Update the in-flight typing buffer painted on the input row.

        Pass an empty string to show just the prompt while a run is active,
        a populated string to echo what the user has typed, or None when no
        run is active and the row should be left clear for `read_line_at_bottom`.
        """
        self.state.pending_input = text
        self.redraw()

    # ── Redraw ─────────────────────────────────────────────────────────────

    def redraw(self) -> None:
        if not self._enabled or not self._installed:
            return

        # Resize-aware: re-pin the scroll region if the terminal changed.
        cols, rows = term_size()
        if (cols, rows) != (self._cols, self._rows):
            self._cols, self._rows = cols, rows
            self._set_scroll_region(1, self.scroll_bottom_row())

        write("\x1b7")  # save cursor

        # Separator rule
        rule_color = fg256(238) if supports_color() else ""
        rule = ("─" * cols) if rule_color else ("-" * cols)
        write(f"\x1b[{self.rule_row()};1H\x1b[2K")
        if rule_color:
            write(f"{rule_color}{rule}{RESET}")
        else:
            write(rule)

        # Status row — right-aligned data block.
        write(f"\x1b[{self.status_row()};1H\x1b[2K")
        write(self._compose_status_row(cols))

        # Input row — clear and (optionally) re-paint the in-flight typing
        # buffer so the user can see what they're typing while the pipeline
        # streams output above. When pending_input is None we leave the row
        # blank so `read_line_at_bottom` can paint its own prompt.
        write(f"\x1b[{self.input_row()};1H\x1b[2K")
        if self.state.pending_input is not None:
            prompt_text = "❯ "
            if supports_color():
                write(style("user_label", prompt_text))
            else:
                write(prompt_text)
            cap = max(0, cols - len(prompt_text) - 1)
            txt = self.state.pending_input
            if cap == 0:
                txt = ""
            elif len(txt) > cap:
                txt = ("…" + txt[-(cap - 1):]) if cap > 1 else "…"
            write(txt)

        write("\x1b8")  # restore cursor
        sys.stdout.flush()

    def _compose_status_row(self, cols: int) -> str:
        s = self.state

        # Build the visible-text version (no ANSI codes) so we can right-align it.
        if s.role and s.model:
            verb = "▸" if s.pipeline_running else "·"
            role_part = f"{verb} {s.role} → {s.model}"
        elif s.pipeline_running:
            role_part = "▸ ..."
        else:
            role_part = "· idle"

        token_part = f"in {_humanize(s.tokens_in)}  out {_humanize(s.tokens_out)}"
        ctx_part = ""
        if s.context_now or s.context_cap_min:
            cap = f"[{_humanize(s.context_cap_min)}]" if s.context_cap_min else ""
            ctx_part = f"ctx {_humanize(s.context_now)} {cap}".strip()

        # Working directory deliberately lives in the top banner, not here —
        # the bottom row is reserved for run-time data (role, tokens, context)
        # that changes during a pipeline. Keeping static info up top leaves
        # room for future status fields without crowding.

        plain_parts = [p for p in (role_part, token_part, ctx_part) if p]
        plain = "  ·  ".join(plain_parts)
        # Right-align: pad with spaces on the left.
        pad = max(0, cols - len(plain))

        if not supports_color():
            return (" " * pad) + plain

        # Re-render with ANSI so the role marker pops.
        styled_role = style("accent", role_part) if role_part else ""
        styled_tokens = style("muted", token_part)
        styled_ctx = style("muted", ctx_part) if ctx_part else ""

        styled_parts = [p for p in (styled_role, styled_tokens, styled_ctx) if p]
        sep = style("rule", "  ·  ")
        styled = sep.join(styled_parts)

        return (" " * pad) + styled


def _humanize(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k" if n % 1000 else f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}m"


# Module-level singleton.
_global: StatusBar | None = None


def get_status_bar() -> StatusBar:
    global _global
    if _global is None:
        _global = StatusBar()
    return _global


# ─── Bottom-anchored input ────────────────────────────────────────────────


# In-process input history. Ring-bounded so a long session doesn't grow
# without bound. Most-recent entry is at index -1.
_HISTORY_MAX = 200
_history: list[str] = []


def _record_history(line: str) -> None:
    """Append ``line`` to the input history, skipping consecutive duplicates."""
    line = line.strip()
    if not line:
        return
    if _history and _history[-1] == line:
        return
    _history.append(line)
    if len(_history) > _HISTORY_MAX:
        del _history[: len(_history) - _HISTORY_MAX]


def _paint_input_row(prompt_text: str, buffer: str, cols: int, input_row: int) -> None:
    """Repaint the input row with prompt + current buffer, leaving the cursor
    parked just after the buffer's last character.
    """
    write(f"\x1b[{input_row};1H\x1b[2K")
    if supports_color():
        write(style("user_label", prompt_text))
    else:
        write(prompt_text)
    visible_cap = max(0, cols - len(prompt_text) - 1)
    if len(buffer) <= visible_cap:
        visible = buffer
    elif visible_cap >= 1:
        visible = "…" + buffer[-(visible_cap - 1):]
    else:
        visible = ""
    write(visible)
    sys.stdout.flush()


def read_line_at_bottom(prompt_text: str = "❯ ") -> str | None:
    """Bottom-anchored, char-mode prompt with slash-command autocomplete.

    Reads keystrokes one at a time, echoes them onto the input row at the
    bottom of the terminal, and shows a popup of matching slash commands
    just above the status-bar rule whenever the buffer starts with ``/``.

      Tab          → insert the highlighted completion (does NOT submit;
                     user is free to keep typing args).
      Up / Down    → navigate the popup.
      Esc          → dismiss the popup but keep the buffer.
      Enter        → submit the line.
      Backspace    → edit the buffer; clears the popup once buffer no
                     longer matches.
      Ctrl-C       → raise KeyboardInterrupt (caller decides what to do).
      Ctrl-D / EOF → return None.

    Returns the submitted line, or None on EOF.
    """
    # Lazy imports — keeps status_bar.py importable before the autocomplete
    # module is loaded (and avoids any future circular concerns).
    from agentcommander.tui.autocomplete import (
        EVT_BACKSPACE,
        EVT_CHAR,
        EVT_DOWN,
        EVT_ENTER,
        EVT_ESCAPE,
        EVT_INTERRUPT,
        EVT_TAB,
        EVT_UP,
        clear_popup_rows,
        match_commands,
        paint_popup,
        parse_events,
    )
    from agentcommander.tui.terminal_input import raw_mode, read_chars_blocking

    bar = get_status_bar()
    if not bar.enabled or not bar._installed:
        # Plain mode (non-TTY or no scroll region installed) — fall back to
        # vanilla input() so piped runs still work.
        try:
            return input(prompt_text)
        except EOFError:
            return None

    cols, rows = term_size()
    input_row = rows
    rule_row = max(1, rows - 2)

    buffer = ""
    matches: list = []
    selected_idx = 0
    popup_height = 0

    def _refresh_popup() -> None:
        """Recompute matches against the current buffer and repaint the popup.
        Clears stale rows when the popup shrinks or disappears."""
        nonlocal matches, selected_idx, popup_height
        new_matches = match_commands(buffer)
        new_height = min(len(new_matches), 6)  # MAX_POPUP_HEIGHT mirrored

        if new_height < popup_height:
            clear_popup_rows(popup_height - new_height,
                             rule_row - popup_height)

        if not new_matches:
            matches = []
            selected_idx = 0
            popup_height = 0
            return

        # Reset selection when the candidate set changes shape.
        if (len(new_matches) != len(matches)
                or (matches and new_matches[0].name != matches[0].name)):
            selected_idx = 0
        if selected_idx >= len(new_matches):
            selected_idx = len(new_matches) - 1

        matches = new_matches
        popup_height = paint_popup(
            matches, selected_idx,
            popup_top_row=rule_row - new_height,
            cols=cols,
        )

    _paint_input_row(prompt_text, buffer, cols, input_row)
    _refresh_popup()

    submitted: str | None = None
    interrupted = False
    eof = False

    # History-navigation state. ``history_idx`` is None when the buffer
    # represents the user's live draft; otherwise it indexes into
    # ``_history`` from the end (0 = most recent). ``saved_draft`` holds
    # whatever the user had typed before they entered history mode so we
    # can restore it when they navigate past the newest entry.
    history_idx: int | None = None
    saved_draft: str = ""

    def _history_up() -> bool:
        """Move toward older history. Returns True if the buffer changed."""
        nonlocal history_idx, saved_draft, buffer
        if not _history:
            return False
        if history_idx is None:
            saved_draft = buffer
            history_idx = 0
        elif history_idx + 1 < len(_history):
            history_idx += 1
        else:
            return False  # already at the oldest entry
        buffer = _history[-(history_idx + 1)]
        return True

    def _history_down() -> bool:
        """Move toward newer history (or back to the live draft)."""
        nonlocal history_idx, buffer
        if history_idx is None:
            return False
        if history_idx == 0:
            history_idx = None
            buffer = saved_draft
            return True
        history_idx -= 1
        buffer = _history[-(history_idx + 1)]
        return True

    try:
        with raw_mode() as raw_ok:
            if not raw_ok:
                # Couldn't put the terminal into char-mode — fall back to
                # line-mode input(). No autocomplete, but still works.
                try:
                    return input("")
                except EOFError:
                    return None

            while True:
                try:
                    chunk = read_chars_blocking()
                except KeyboardInterrupt:
                    interrupted = True
                    break

                events = parse_events(chunk)
                if not events:
                    continue

                buffer_changed = False
                popup_only_change = False

                for evt in events:
                    if evt.kind == EVT_INTERRUPT:
                        interrupted = True
                        break
                    if evt.kind == EVT_ENTER:
                        submitted = buffer
                        break
                    if evt.kind == EVT_BACKSPACE:
                        # Editing leaves history-nav mode — the buffer is now
                        # the user's live draft regardless of where it came from.
                        history_idx = None
                        if buffer:
                            buffer = buffer[:-1]
                            buffer_changed = True
                        continue
                    if evt.kind == EVT_CHAR:
                        history_idx = None
                        buffer += evt.data or ""
                        buffer_changed = True
                        continue
                    if evt.kind == EVT_TAB:
                        if matches:
                            history_idx = None
                            buffer = matches[selected_idx].name
                            buffer_changed = True
                        continue
                    if evt.kind == EVT_UP:
                        if matches:
                            selected_idx = (selected_idx - 1) % len(matches)
                            popup_only_change = True
                        elif _history_up():
                            buffer_changed = True
                        continue
                    if evt.kind == EVT_DOWN:
                        if matches:
                            selected_idx = (selected_idx + 1) % len(matches)
                            popup_only_change = True
                        elif _history_down():
                            buffer_changed = True
                        continue
                    if evt.kind == EVT_ESCAPE:
                        if popup_height > 0:
                            clear_popup_rows(popup_height,
                                             rule_row - popup_height)
                            matches = []
                            selected_idx = 0
                            popup_height = 0
                        continue

                if interrupted or submitted is not None:
                    break

                if buffer_changed:
                    _paint_input_row(prompt_text, buffer, cols, input_row)
                    _refresh_popup()
                elif popup_only_change and matches:
                    # Just re-highlight the popup — buffer is unchanged.
                    paint_popup(matches, selected_idx,
                                popup_top_row=rule_row - popup_height,
                                cols=cols)
    except OSError:
        eof = True

    # Tear down the popup before yielding control back to the REPL.
    if popup_height > 0:
        clear_popup_rows(popup_height, rule_row - popup_height)

    if interrupted:
        bar.park_cursor()
        raise KeyboardInterrupt
    if eof:
        bar.park_cursor()
        return None
    if submitted is None:
        bar.park_cursor()
        return None

    # Move the input cursor onto a fresh line under the input row before
    # parking — gives a clean visual break for the rendered user message.
    write(f"\x1b[{input_row};1H\x1b[2K")
    sys.stdout.flush()
    bar.park_cursor()
    bar.redraw()
    _record_history(submitted)
    return submitted


# Suppress unused symbol warnings for reserved-future references.
_ = (BOLD, DIM)
