"""Shared terminal-input plumbing — raw mode and char polling.

Used by:
  - `status_bar.read_line_at_bottom` for the between-runs prompt with
    autocomplete (blocking reads).
  - `app._run_pipeline` for the during-run typing handler (non-blocking polls).

Pure stdlib. POSIX uses termios cbreak + ECHO-off. Windows uses
``msvcrt.getwch`` / ``msvcrt.kbhit`` — keyboard only, but the terminal
keeps its native mouse-wheel scrollback and select-to-copy intact.

Mouse input was removed in favor of native scrollback. The previous
ctypes implementation enabled ``ENABLE_MOUSE_INPUT`` and disabled
``ENABLE_QUICK_EDIT_MODE`` on the console, which broke Windows
Terminal's mouse-wheel scrollback and right-click paste. Popout
expand/collapse is still available via Tab/Space/Enter and the
``/popout`` slash command.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager


@contextmanager
def raw_mode():
    """Switch the terminal into cbreak + no-echo for the duration of the block.

    Yields True when char-at-a-time input with self-managed echo is available
    (so callers can paint typed characters themselves). Yields False when
    stdin isn't a TTY or termios isn't usable — callers should fall back to
    line-mode behavior.

    Windows: nothing to do; msvcrt.getwch is already char-at-a-time and
    doesn't echo. Yields True for TTY stdin.
    """
    if not sys.stdin.isatty():
        yield False
        return
    if sys.platform == "win32":
        yield True
        return
    try:
        import termios
        import tty
    except ImportError:
        yield False
        return
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        new_attrs = termios.tcgetattr(fd)
        new_attrs[3] = new_attrs[3] & ~termios.ECHO  # type: ignore[index]
        termios.tcsetattr(fd, termios.TCSANOW, new_attrs)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def poll_chars() -> str:
    """Non-blocking read of whatever the user has typed so far.

    Returns "" if nothing's queued. Used by the in-run handler that polls
    stdin alongside an event queue.
    """
    if not sys.stdin.isatty():
        return ""
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:
            return ""
        out: list[str] = []
        while msvcrt.kbhit():
            try:
                out.append(msvcrt.getwch())
            except OSError:
                break
        return "".join(out)
    try:
        import os
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return ""
        return os.read(sys.stdin.fileno(), 256).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def read_chars_blocking() -> str:
    """Block until at least one character arrives, then drain anything else
    that's already buffered (so multi-byte sequences like ``\\x1b[A`` arrive
    intact). Returns the combined string.

    Raises ``KeyboardInterrupt`` on Ctrl-C in cbreak mode (POSIX), since
    cbreak preserves ISIG. On Windows we synthesize the same behavior by
    detecting Ctrl-C as a special control char.
    """
    if not sys.stdin.isatty():
        # Non-TTY: fall back to line-buffered. Returns whatever readline
        # gives, including the trailing newline.
        return sys.stdin.readline()
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:
            return sys.stdin.readline()
        ch = msvcrt.getwch()  # blocks
        if ch == "\x03":  # Ctrl-C — msvcrt swallows it, we re-raise
            raise KeyboardInterrupt
        out = [ch]
        while msvcrt.kbhit():
            try:
                out.append(msvcrt.getwch())
            except OSError:
                break
        return "".join(out)
    import os
    import select
    fd = sys.stdin.fileno()
    chunk = os.read(fd, 256).decode("utf-8", errors="replace")
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            break
        try:
            more = os.read(fd, 256).decode("utf-8", errors="replace")
        except OSError:
            break
        if not more:
            break
        chunk += more
    return chunk
