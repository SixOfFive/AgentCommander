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


# Per-command sub-option tables. Surfaced once the user types whitespace
# after a top-level command. Keyed by the SlashCommand's canonical name —
# `/ac` typed by the user resolves to `/autoconfig` via COMMANDS first, so
# aliases are handled automatically.
SUB_COMMANDS: dict[str, list[CompletionMatch]] = {
    "/autoconfig": [
        CompletionMatch(name="clear",
                        summary="wipe role assignments + re-prompt for the Ollama endpoint, then redo"),
        CompletionMatch(name="minctx",
                        summary="filter to models with at least N context tokens (e.g. minctx 128k)"),
        CompletionMatch(name="ban",
                        summary="exclude <model_id> from autoconfig picks, then re-run"),
        CompletionMatch(name="unban",
                        summary="re-allow a previously banned <model_id>, then re-run"),
        CompletionMatch(name="bans",
                        summary="list models currently excluded from autoconfig"),
    ],
    "/typecast": [
        CompletionMatch(name="refresh",
                        summary="force re-fetch the TypeCast catalog from GitHub"),
        CompletionMatch(name="autoconfigure",
                        summary="pick best installed model + per-role overrides"),
    ],
    "/providers": [
        CompletionMatch(name="add",
                        summary="add a provider: <id> <type> <name> <endpoint>"),
        CompletionMatch(name="test",
                        summary="health-check a provider: <id>"),
        CompletionMatch(name="rm",
                        summary="remove a provider: <id>"),
    ],
    "/roles": [
        CompletionMatch(name="set",
                        summary="pin a role override: <role> <provider_id> <model>"),
        CompletionMatch(name="unset",
                        summary="release a per-role override: <role>"),
        CompletionMatch(name="auto",
                        summary="re-run autoconfig (in-memory only; respects overrides)"),
        CompletionMatch(name="assign-all",
                        summary="bulk-assign every role: <provider_id> <model>"),
    ],
    "/context": [
        CompletionMatch(name="off",
                        summary="clear the session-wide num_ctx override"),
    ],
    "/preflight": [
        CompletionMatch(name="on",
                        summary="enable preflight (extra LLM call per orchestrator step)"),
        CompletionMatch(name="off",
                        summary="disable preflight"),
        CompletionMatch(name="rules",
                        summary="list active operational rules"),
    ],
    "/postmortem": [
        CompletionMatch(name="on",
                        summary="enable postmortem (LLM call after each failed run)"),
        CompletionMatch(name="off",
                        summary="disable postmortem"),
    ],
    "/chat": [
        CompletionMatch(name="list",
                        summary="list recent chats with message counts"),
        CompletionMatch(name="new",
                        summary="start a fresh chat: /chat new [<title>]"),
        CompletionMatch(name="clear",
                        summary="DESTRUCTIVE: delete current chat + scratchpad, clear screen"),
        CompletionMatch(name="resume",
                        summary="switch to existing chat: /chat resume <id_prefix>"),
        CompletionMatch(name="title",
                        summary="rename current chat: /chat title <new title>"),
        CompletionMatch(name="export",
                        summary="write chat to markdown: /chat export <path>"),
    ],
}


def match_commands(buffer: str) -> list[CompletionMatch]:
    """Return completion matches for the current buffer.

    Two tiers:
      1. **Top-level**: buffer starts with ``/`` and has no whitespace —
         match against every registered SlashCommand by name + aliases.
      2. **Sub-commands**: buffer has exactly one whitespace-separated
         token plus a partial second word — match against ``SUB_COMMANDS``
         for the first token's canonical command. Sub-options that take
         further free-form arguments (paths, urls, role names) are out of
         scope here; we stop after the second token has any whitespace.

    Returns ``[]`` when the buffer doesn't start with ``/`` or when no
    matches apply.
    """
    if not buffer.startswith("/"):
        return []

    # Lazy import to avoid pulling the command registry at module-load time.
    from agentcommander.tui.commands import COMMANDS

    # Tier 1: top-level command name still being typed.
    if not any(c.isspace() for c in buffer):
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

    # Tier 2: sub-command on the second token. Anything past the second
    # token gets free-form args we don't try to complete.
    parts = buffer.split(maxsplit=1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if any(c.isspace() for c in rest):
        return []

    cmd = COMMANDS.get(head)
    if cmd is None:
        return []

    subs = SUB_COMMANDS.get(cmd.name, [])
    if not subs:
        return []

    needle = rest.lower()
    return [s for s in subs if s.name.lower().startswith(needle)]


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
