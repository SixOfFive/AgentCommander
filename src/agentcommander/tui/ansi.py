"""ANSI escape codes + small terminal helpers.

Designed to match Claude Code's linux look:
  - dim grey for system text
  - bright white for user message labels
  - accent (cyan) for role names
  - warm yellow for tool / iteration markers
  - muted red for errors

Pure stdlib. Works on:
  - Linux / macOS terminals (always)
  - Windows Terminal (always)
  - Windows ConHost via VT enable on startup (handled in `enable_ansi()`)
"""
from __future__ import annotations

import os
import shutil
import sys

# ─── Color codes ───────────────────────────────────────────────────────────

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
INVERT = "\x1b[7m"

# 8-color foreground
BLACK = "\x1b[30m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
WHITE = "\x1b[37m"

# Bright variants
BR_BLACK = "\x1b[90m"
BR_RED = "\x1b[91m"
BR_GREEN = "\x1b[92m"
BR_YELLOW = "\x1b[93m"
BR_BLUE = "\x1b[94m"
BR_MAGENTA = "\x1b[95m"
BR_CYAN = "\x1b[96m"
BR_WHITE = "\x1b[97m"


def fg256(n: int) -> str:
    """Foreground color from the 256-color palette."""
    return f"\x1b[38;5;{n}m"


def bg256(n: int) -> str:
    return f"\x1b[48;5;{n}m"


# ─── Cursor + screen ───────────────────────────────────────────────────────

CLEAR_SCREEN = "\x1b[2J\x1b[H"
CLEAR_LINE = "\x1b[2K"
SAVE_CURSOR = "\x1b7"
RESTORE_CURSOR = "\x1b8"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


def move_cursor(row: int, col: int) -> str:
    return f"\x1b[{row};{col}H"


# ─── Setup ─────────────────────────────────────────────────────────────────


def enable_ansi() -> bool:
    """Enable ANSI VT processing on Windows + force UTF-8 stdout/stderr everywhere.

    No-op on Linux/macOS for the VT side; UTF-8 reconfigure runs everywhere
    so box-drawing characters render even when stdout is redirected.

    Returns True if ANSI is supported.
    """
    # Force UTF-8 on text streams. Python 3.7+ ships .reconfigure().
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            STD_OUTPUT_HANDLE = -11
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            # Make CP65001 the active code page for the Console (best effort).
            try:
                kernel32.SetConsoleOutputCP(65001)
                kernel32.SetConsoleCP(65001)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False
    return True


def supports_color() -> bool:
    """Best-effort check; avoid escape codes when piping output."""
    if not sys.stdout.isatty():
        # Allow forcing via env var.
        return os.environ.get("FORCE_COLOR") == "1"
    if os.environ.get("NO_COLOR"):
        return False
    return True


def term_size() -> tuple[int, int]:
    """Return (columns, rows)."""
    size = shutil.get_terminal_size(fallback=(100, 30))
    return size.columns, size.lines


# ─── Style helpers ─────────────────────────────────────────────────────────

# Fixed AC palette — keep stable across the whole TUI.
PALETTE = {
    "user_label":      BR_WHITE + BOLD,
    "user_text":       BR_WHITE,
    "assistant_label": fg256(75),  # soft cyan
    "assistant_text":  WHITE,
    "system_text":     DIM,
    "tool_marker":     fg256(180),  # warm tan
    "tool_ok":         GREEN,
    "tool_err":        BR_RED,
    "iter_marker":     fg256(245),  # mid grey
    "iter_action":     BR_CYAN,
    "guard_label":     fg256(208),  # warm orange
    "role_label":      fg256(141),  # muted purple
    "error":           BR_RED,
    "muted":           BR_BLACK,
    "rule":            DIM,
    "border":          fg256(238),  # very dim
    "accent":          fg256(75),
    "warn":            fg256(214),
}


def style(name: str, text: str) -> str:
    """Wrap `text` with the palette entry `name`. No-op if color disabled."""
    if not supports_color():
        return text
    code = PALETTE.get(name, "")
    return f"{code}{text}{RESET}" if code else text


def write(s: str = "") -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def writeln(s: str = "") -> None:
    write(s + "\n")
