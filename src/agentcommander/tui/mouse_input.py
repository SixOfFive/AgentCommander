"""xterm SGR mouse mode for the popout system.

Two-step protocol:
  1. On TUI start, write ``\\x1b[?1000h\\x1b[?1006h`` to enable
     button-press reporting in SGR (extended) format. This is the
     modern variant that survives terminal sizes >223 cols (the legacy
     X10 form encodes coordinates as bytes which cap at 255-32 = 223).
  2. On TUI exit, write the matching disable sequences so the user's
     terminal isn't left in mouse mode if the process aborts.

When mouse mode is active, click events arrive on stdin as:
    \\x1b[<{button};{x};{y}M    (M = press, m = release)

Where button bits encode:
    0–2  = button code (0=left, 1=middle, 2=right, 3=release in legacy)
    +32  = motion (drag — we ignore)
    +64  = wheel (4=up, 5=down — we ignore)

We only react to *press of left button on a popout summary row*. Drag,
release, wheel, and right-click are all swallowed silently so they don't
leak into the typed-input buffer.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from agentcommander.tui.ansi import write


# Enable button-press tracking + SGR extended coordinate format.
_ENABLE = "\x1b[?1000h\x1b[?1006h"
_DISABLE = "\x1b[?1006l\x1b[?1000l"


def enable_mouse_mode() -> None:
    """Turn on xterm SGR mouse reporting. Idempotent — safe to call
    multiple times. Best-effort: silently no-ops on terminals that
    don't support the escape (the user just doesn't get clicks)."""
    if not sys.stdout.isatty():
        return
    try:
        write(_ENABLE)
    except Exception:  # noqa: BLE001
        pass


def disable_mouse_mode() -> None:
    """Turn off mouse reporting. Critical to call on TUI exit so the
    user's shell isn't left receiving click events for every pointer
    motion."""
    if not sys.stdout.isatty():
        return
    try:
        write(_DISABLE)
    except Exception:  # noqa: BLE001
        pass


# Match a single SGR mouse report. Group 1 = button code, 2 = x, 3 = y,
# 4 = "M" (press) or "m" (release).
_MOUSE_RX = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


@dataclass
class MouseEvent:
    button: int   # 0=left, 1=middle, 2=right
    x: int        # 1-indexed column
    y: int        # 1-indexed row
    pressed: bool # True for press, False for release


def parse_mouse_events(buffer: str) -> tuple[str, list[MouseEvent]]:
    """Pull every complete SGR mouse report out of ``buffer``.

    Returns ``(remainder, events)``. ``remainder`` is ``buffer`` with
    each matched escape removed — caller should put it back into the
    typed-input pipeline so non-mouse bytes still reach the keystroke
    consumer.
    """
    if "\x1b[<" not in buffer:
        return buffer, []
    events: list[MouseEvent] = []
    out = []
    i = 0
    while i < len(buffer):
        m = _MOUSE_RX.match(buffer, i)
        if m:
            btn_raw = int(m.group(1))
            x = int(m.group(2))
            y = int(m.group(3))
            pressed = m.group(4) == "M"
            # Mask off motion/wheel bits — we only care about basic
            # button presses. Bit 5 = motion (32), bit 6 = wheel (64).
            # Legacy "release" is button 3; SGR uses the M/m flag instead.
            if not (btn_raw & 0b0110_0000):
                events.append(MouseEvent(
                    button=btn_raw & 0b0000_0011,
                    x=x, y=y, pressed=pressed,
                ))
            i = m.end()
        else:
            out.append(buffer[i])
            i += 1
    return "".join(out), events


__all__ = [
    "MouseEvent",
    "enable_mouse_mode",
    "disable_mouse_mode",
    "parse_mouse_events",
]
