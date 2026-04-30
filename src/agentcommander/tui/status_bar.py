"""Persistent bottom status bar + bottom-anchored input row.

Status row layout (right-aligned, separated by ` · `):
  ▸ role → model    in {prompt}  out {completion}    ctx {now}/{max}    run {mm:ss}    total {mm:ss}

Timers tick from inside `redraw()` — there's no background thread. The TUI
loop calls `redraw()` once per second during a run so the displayed time
stays current without coupling to engine events.


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
import time
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
    # Run timer — wall-clock duration of the current pipeline run. While
    # running, `redraw()` recomputes `run_elapsed_ms` from `run_started_at`
    # so the displayed value tracks the clock without an explicit tick.
    # When a run ends, the final duration is added to `total_elapsed_ms`
    # (session-cumulative) and `run_started_at` is cleared.
    run_started_at: float | None = None
    run_elapsed_ms: int = 0
    total_elapsed_ms: int = 0
    # Running-average tokens/second for ``model``, looked up at set_role
    # time and refreshed on each role-end so the bar shows what to expect
    # from this model right now. None == not yet known (display omits the
    # rate); 100.0 is the seed default for never-measured models.
    model_tps: float | None = None


# Fields to persist to / load from `config.bar_state_json` so a mirror can
# reproduce the primary's bar. We deliberately omit `workdir` and
# `pending_input` because those are local-process concerns: the mirror
# shows its own working directory in the banner, and never has typed text.
_MIRRORED_BAR_FIELDS = (
    "role", "model", "tokens_in", "tokens_out",
    "context_now", "context_cap_min",
    "pipeline_running",
    "run_elapsed_ms", "total_elapsed_ms",
    "model_tps",
)


def _state_to_dict(state: StatusState) -> dict:
    """Snapshot the mirror-relevant fields of a StatusState as a plain dict."""
    return {k: getattr(state, k) for k in _MIRRORED_BAR_FIELDS}


def _apply_dict_to_state(state: StatusState, d: dict) -> None:
    """Copy mirror-relevant fields from a dict into a StatusState in-place."""
    for k in _MIRRORED_BAR_FIELDS:
        if k in d:
            setattr(state, k, d[k])


class StatusBar:
    """Owns the bottom RESERVED_ROWS rows of the terminal."""

    def __init__(self) -> None:
        self.state = StatusState()
        self._cols, self._rows = term_size()
        self._installed = False
        self._enabled = sys.stdout.isatty() and supports_color()
        # Mirror mode: when True, this bar is owned by `ac --mirror` and
        # must NOT tee its state back to the DB (mirror is read-only and
        # would dirty the snapshot the primary owns). Also adjusts the
        # right-side text to show "(read-only)" so the watcher knows.
        self._mirror_mode: bool = False

    def set_mirror_mode(self, on: bool) -> None:
        """Mark this bar as the mirror's display. Disables state tee, adds
        the read-only badge, suppresses the input-row prompt repaint."""
        self._mirror_mode = on
        self.redraw()

    @property
    def mirror_mode(self) -> bool:
        return self._mirror_mode

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
        """Restore the full screen as the scroll region; clear the reserved rows.

        Parks the cursor at the bottom row so the parent shell's prompt
        appears below the rendered content instead of overwriting it.
        """
        if not self._enabled or not self._installed:
            return
        self._set_scroll_region(1, self._rows)
        for r in (self.rule_row(), self.status_row(), self.input_row()):
            write(f"\x1b[{r};1H\x1b[2K")
        # Park at the last visible row, not row 1. The exit "goodbye." line
        # then lands at the very bottom and the shell prompt wraps naturally
        # below it instead of overwriting whatever was on rows 1-2.
        write(f"\x1b[{self._rows};1H")
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

    def set_role(self, role: str | None, model: str | None,
                 num_ctx: int | None = None) -> None:
        """Mark which (role, model) is active. ``num_ctx`` updates the
        displayed context-window cap when provided (so the bar shows
        ``ctx N/M`` against the actual limit configured for this role)."""
        self.state.role = role
        self.state.model = model
        if num_ctx is not None:
            self.state.context_cap_min = num_ctx
        self.redraw()
        # Role transitions are mirror-critical too: bypass the throttle so
        # the watcher sees "▸ coder → devstral-small-2:24b" the instant
        # primary fires the role-start, not 100 ms later.
        if not self._mirror_mode:
            try:
                from agentcommander.engine import live_tee
                live_tee.maybe_tee_bar_state(_state_to_dict(self.state), force=True)
            except Exception:  # noqa: BLE001
                pass

    def add_tokens(self, *, prompt: int = 0, completion: int = 0) -> None:
        self.state.tokens_in += max(0, prompt)
        self.state.tokens_out += max(0, completion)
        # Show the most recent call's prompt size as "current context use".
        # Each role call ships its own input window, so the latest reading
        # is the meaningful one — not a sum across calls.
        if prompt > 0:
            self.state.context_now = prompt
        self.redraw()

    def reset_run(self) -> None:
        self.state.role = None
        self.state.model = None
        self.state.tokens_in = 0
        self.state.tokens_out = 0
        self.state.context_now = 0
        self.state.pipeline_running = False
        self.state.run_started_at = None
        self.state.run_elapsed_ms = 0
        self.redraw()

    def set_running(self, running: bool) -> None:
        was_running = self.state.pipeline_running
        self.state.pipeline_running = running
        if running and not was_running:
            self.state.run_started_at = time.time()
            self.state.run_elapsed_ms = 0
        elif not running and was_running:
            if self.state.run_started_at is not None:
                final_ms = int((time.time() - self.state.run_started_at) * 1000)
                self.state.run_elapsed_ms = final_ms
                self.state.total_elapsed_ms += final_ms
            self.state.run_started_at = None
            # Clear the live context-use reading on run end. context_now
            # holds the LAST role's prompt-token count; when no model is
            # active anymore that number is stale (showing "7.8k/8.2k"
            # with a red bar suggests something is loaded — nothing is).
            # The cap_min stays so the bar shows "ctx —/8.2k" — the
            # configured ceiling is still relevant info.
            self.state.context_now = 0
        self.redraw()
        # Run-state transitions are mirror-critical: a watcher needs to see
        # pipeline_running flip immediately, not whenever the throttle
        # next allows a write. Force an unthrottled tee here so the bar in
        # the mirror process drops the "▸ running" verb the moment the
        # primary's run actually ended.
        if not self._mirror_mode:
            try:
                from agentcommander.engine import live_tee
                live_tee.maybe_tee_bar_state(_state_to_dict(self.state), force=True)
            except Exception:  # noqa: BLE001
                pass

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
        # Refresh the live run timer from the wall clock. This is why the
        # TUI loop calls redraw() once per second during a pipeline — without
        # that periodic call, the timer would only advance on engine events
        # (role start/end, token chunks).
        if self.state.pipeline_running and self.state.run_started_at is not None:
            self.state.run_elapsed_ms = int(
                (time.time() - self.state.run_started_at) * 1000
            )

        # Tee the live bar state to the mirror (primary only — mirror is RO).
        # MUST run before the _enabled / _installed early-return so headless
        # primary (piped stdin, remote shell, no-color terminal) still keeps
        # the mirror's bar in sync. Throttled to ~10 Hz internally.
        if not self._mirror_mode:
            try:
                from agentcommander.engine import live_tee
                live_tee.maybe_tee_bar_state(_state_to_dict(self.state))
            except Exception:  # noqa: BLE001
                pass

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

        # Mirror badge — pinned to the FAR LEFT so the watcher can't miss
        # that this terminal is read-only no matter what else is on the row.
        mirror_badge_plain = "● MIRROR (read-only)" if self._mirror_mode else ""

        token_part = f"in {_humanize(s.tokens_in)}  out {_humanize(s.tokens_out)}"

        # Context: "ctx N/M" when both known; "ctx N" when only the running
        # tally is set; "ctx —/M" when only the cap is known (e.g. between
        # the role-start signal and the first prompt-tokens reading).
        ctx_part = ""
        # ctx N/M plus an optional fill-bar. The bar's plain form (used for
        # right-alignment math) and styled form (with fill-level coloring)
        # are computed together so the visible width stays consistent.
        ctx_part = ""
        ctx_part_styled = ""
        if s.context_now and s.context_cap_min:
            text = f"ctx {_humanize(s.context_now)}/{_humanize(s.context_cap_min)}"
            bar_plain, bar_styled = _render_ctx_bar(s.context_now, s.context_cap_min)
            ctx_part = f"{text} {bar_plain}" if bar_plain else text
            ctx_part_styled = (
                f"{style('muted', text)} {bar_styled}"
                if bar_styled
                else style("muted", text)
            )
        elif s.context_cap_min:
            ctx_part = f"ctx —/{_humanize(s.context_cap_min)}"
            ctx_part_styled = style("muted", ctx_part)
        elif s.context_now:
            ctx_part = f"ctx {_humanize(s.context_now)}"
            ctx_part_styled = style("muted", ctx_part)

        # Run timer: live elapsed while the pipeline is active, otherwise the
        # last completed run's duration (so the user can see "that took X").
        # Total accumulates across the session.
        run_part = f"run {_humanize_duration_ms(s.run_elapsed_ms)}"
        total_part = f"total {_humanize_duration_ms(s.total_elapsed_ms)}"

        # Working directory deliberately lives in the top banner, not here —
        # the bottom row is reserved for run-time data (role, tokens, context,
        # timers) that changes during a pipeline.

        plain_parts = [p for p in (role_part, token_part, ctx_part, run_part, total_part) if p]
        plain = "  ·  ".join(plain_parts)
        # Right-align: pad with spaces on the left, but reserve the left
        # edge for the mirror badge when present.
        right_width = max(0, cols - len(plain))
        if mirror_badge_plain:
            # Badge + at least 2 spaces of separation from the right block.
            badge_w = len(mirror_badge_plain) + 2
            right_width = max(0, cols - badge_w - len(plain))
            badge_pad = " " * right_width

        if not supports_color():
            if mirror_badge_plain:
                return mirror_badge_plain + "  " + badge_pad + plain
            return (" " * right_width) + plain

        # Re-render with ANSI so the role marker pops.
        styled_role = style("accent", role_part) if role_part else ""
        styled_tokens = style("muted", token_part)
        styled_run = style("muted", run_part)
        styled_total = style("muted", total_part)

        styled_parts = [p for p in
                        (styled_role, styled_tokens, ctx_part_styled,
                         styled_run, styled_total) if p]
        sep = style("rule", "  ·  ")
        styled = sep.join(styled_parts)

        if mirror_badge_plain:
            badge_styled = style("warn", mirror_badge_plain)
            return badge_styled + "  " + badge_pad + styled
        return (" " * right_width) + styled


def _humanize(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k" if n % 1000 else f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}m"


def _render_ctx_bar(now: int, cap: int, cells: int = 8) -> tuple[str, str]:
    """Render a tiny fill-bar for the ``ctx N/M`` segment.

    Returns ``(plain, styled)``:
      - plain  — used for right-align width math; visible char count only
      - styled — ANSI-wrapped: filled cells colored by fill ratio
                 (green < 60% < yellow < 85% < red), empty cells dim grey

    Falls back to ASCII ``[####----]`` when the terminal can't do color so
    the bar still shows fill, just without the visual heat-map.

    Returns ``("", "")`` when there's nothing meaningful to draw (cap unset
    or context_now is zero) — the caller drops the bar.
    """
    if cap <= 0 or now <= 0:
        return "", ""
    ratio = min(1.0, now / cap)
    filled = int(round(ratio * cells))
    if filled == 0 and ratio > 0:
        filled = 1  # always show at least one cell of fill
    if filled > cells:
        filled = cells
    empty = cells - filled

    if not supports_color():
        plain = "[" + ("#" * filled) + ("-" * empty) + "]"
        return plain, plain

    full_block = "█" * filled
    empty_block = "░" * empty
    plain = "[" + full_block + empty_block + "]"

    if ratio >= 0.85:
        full_color = "\x1b[91m"   # bright red — running out
    elif ratio >= 0.60:
        full_color = "\x1b[93m"   # bright yellow — half full+
    else:
        full_color = "\x1b[92m"   # bright green — comfortable
    empty_color = fg256(238)      # dim grey — matches the rule color

    styled = (
        "[" + full_color + full_block + RESET
        + empty_color + empty_block + RESET + "]"
    )
    return plain, styled


def _humanize_duration_ms(ms: int | None) -> str:
    """Format a duration in milliseconds as ``0:05`` / ``1:23`` / ``1:23:45``."""
    if ms is None or ms < 0:
        ms = 0
    total_s = ms // 1000
    if total_s < 60:
        return f"0:{total_s:02d}"
    m, s = divmod(total_s, 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


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
                            # Replace only the trailing whitespace-separated
                            # token so sub-command completion preserves the
                            # leading command (`/autoconfig cle` → `/autoconfig clear`).
                            # When there's no whitespace yet, this still
                            # replaces the entire buffer (top-level case).
                            last_ws = max(buffer.rfind(" "),
                                          buffer.rfind("\t"))
                            if last_ws >= 0:
                                buffer = buffer[: last_ws + 1] + matches[selected_idx].name
                            else:
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
