"""Slash-command autocomplete — match, paint popup, parse key events.

The popup floats just above the status-bar rule, occupying up to
``MAX_POPUP_HEIGHT`` rows. It's painted with save/restore cursor so it
never moves the input-row cursor; on dismissal we clear the rows we drew.
The rows it covers are bottom-of-scroll-region content the user just saw,
so there's no information loss — only a transient occlusion.

Matching is anchored to the typed prefix while the buffer is still on its
first whitespace-separated token. Once the user has typed past the command
name (e.g. ``/help foo``), no completions are offered.

Pure stdlib.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from agentcommander.tui.ansi import (
    RESET,
    fg256,
    style,
    supports_color,
    write,
)

MAX_POPUP_HEIGHT = 6


@dataclass
class CompletionMatch:
    name: str          # "/models"
    summary: str       # one-line help, possibly truncated
    aliases: tuple[str, ...] = ()


def match_commands(buffer: str) -> list[CompletionMatch]:
    """Return SlashCommand entries whose name (or alias) starts with the
    typed prefix. Returns ``[]`` when:
      - the buffer doesn't start with ``/``
      - the user has typed past the command name (any whitespace present)
    """
    if not buffer.startswith("/"):
        return []
    # Once the user is into args (saw a space), stop offering completions —
    # the command itself is settled.
    if any(c.isspace() for c in buffer):
        return []

    # Lazy import to avoid pulling the command registry at module-load time.
    from agentcommander.tui.commands import COMMANDS

    needle = buffer.lower()
    seen: set[int] = set()
    out: list[CompletionMatch] = []
    for cmd in COMMANDS.values():
        if id(cmd) in seen:
            continue
        names = (cmd.name,) + cmd.aliases
        if any(n.lower().startswith(needle) for n in names):
            seen.add(id(cmd))
            out.append(CompletionMatch(
                name=cmd.name,
                summary=cmd.summary,
                aliases=cmd.aliases,
            ))
    out.sort(key=lambda m: m.name)
    return out


def paint_popup(
    matches: list[CompletionMatch],
    selected_idx: int,
    popup_top_row: int,
    cols: int,
) -> int:
    """Paint the popup above the rule. Returns the height (rows drawn).

    Save+restore cursor so the input row's cursor position is preserved.
    """
    if not matches:
        return 0
    height = min(len(matches), MAX_POPUP_HEIGHT)
    write("\x1b7")
    name_col_width = max((len(m.name) for m in matches[:height]), default=10)
    name_col_width = max(name_col_width, 12)
    for i in range(height):
        m = matches[i]
        row = popup_top_row + i
        write(f"\x1b[{row};1H\x1b[2K")
        marker = "›" if i == selected_idx else " "
        name_padded = m.name.ljust(name_col_width)
        line = f"  {marker} {name_padded}  {m.summary}"
        if len(line) > cols - 1:
            line = line[:cols - 2] + "…"
        if not supports_color():
            write(line)
            continue
        if i == selected_idx:
            # Highlighted row — bright accent on the marker + name, default on the summary.
            head = f"  {marker} {name_padded}"
            tail = f"  {m.summary}"
            tail_len = (cols - 1) - len(head)
            if tail_len < 0:
                tail_len = 0
            tail = tail[:tail_len]
            write(style("accent", head))
            write(style("user_label", tail))
        else:
            head = f"  {marker} {name_padded}"
            tail = f"  {m.summary}"
            tail_len = (cols - 1) - len(head)
            if tail_len < 0:
                tail_len = 0
            tail = tail[:tail_len]
            write(fg256(244) + head + RESET)
            write(style("muted", tail))
    write("\x1b8")
    sys.stdout.flush()
    return height


def clear_popup_rows(num_rows: int, popup_top_row: int) -> None:
    """Clear ``num_rows`` rows starting at ``popup_top_row``. Cursor preserved."""
    if num_rows <= 0:
        return
    write("\x1b7")
    for i in range(num_rows):
        write(f"\x1b[{popup_top_row + i};1H\x1b[2K")
    write("\x1b8")
    sys.stdout.flush()


# ─── Key-event parser ──────────────────────────────────────────────────────


# Event kinds for the read loop.
EVT_CHAR = "char"            # printable char appended to buffer (data = ch)
EVT_BACKSPACE = "backspace"
EVT_ENTER = "enter"
EVT_TAB = "tab"
EVT_UP = "up"
EVT_DOWN = "down"
EVT_ESCAPE = "escape"
EVT_INTERRUPT = "interrupt"  # Ctrl-C (POSIX cbreak preserves ISIG so this is rarely synthesized; here for completeness)


@dataclass
class InputEvent:
    kind: str
    data: str | None = None


def parse_events(chunk: str) -> list[InputEvent]:
    """Decode a chunk of raw bytes into discrete input events.

    Handles:
      - POSIX CSI escapes: ``ESC [ <params> <final>`` (arrow keys mostly)
      - POSIX SS3 escapes: ``ESC O <final>`` (F-keys)
      - Windows special-key prefixes: ``\\x00`` / ``\\xe0`` followed by a code
      - Tab, Enter (CR/LF), Backspace (BS/DEL)
      - Lone ESC → EVT_ESCAPE (used to dismiss the popup)
      - Other control bytes are dropped silently
    """
    events: list[InputEvent] = []
    i = 0
    n = len(chunk)
    while i < n:
        ch = chunk[i]

        # Windows special-key prefix
        if ch in ("\x00", "\xe0"):
            if i + 1 < n:
                code = chunk[i + 1]
                if code == "H":
                    events.append(InputEvent(EVT_UP))
                elif code == "P":
                    events.append(InputEvent(EVT_DOWN))
                # left/right/F-keys/etc. ignored — we don't support cursor movement yet
            i += 2
            continue

        # POSIX ANSI escape
        if ch == "\x1b":
            i += 1
            if i >= n:
                events.append(InputEvent(EVT_ESCAPE))
                continue
            nxt = chunk[i]
            if nxt == "[":
                i += 1
                # Walk until a final byte (terminator in 0x40-0x7E for CSI)
                code = ""
                while i < n:
                    c2 = chunk[i]
                    i += 1
                    if c2 == "~" or ("A" <= c2 <= "Z") or ("a" <= c2 <= "z"):
                        code = c2
                        break
                if code == "A":
                    events.append(InputEvent(EVT_UP))
                elif code == "B":
                    events.append(InputEvent(EVT_DOWN))
                # left/right/etc. dropped
            elif nxt == "O":
                # SS3 — consume one final byte
                i += 2
            else:
                # Lone ESC followed by something we don't recognize
                events.append(InputEvent(EVT_ESCAPE))
            continue

        i += 1
        if ch == "\t":
            events.append(InputEvent(EVT_TAB))
            continue
        if ch in ("\r", "\n"):
            events.append(InputEvent(EVT_ENTER))
            continue
        if ch in ("\x7f", "\x08"):
            events.append(InputEvent(EVT_BACKSPACE))
            continue
        if ch == "\x03":
            events.append(InputEvent(EVT_INTERRUPT))
            continue
        if ord(ch) < 32:
            continue
        events.append(InputEvent(EVT_CHAR, ch))
    return events
