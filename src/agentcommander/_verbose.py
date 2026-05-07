"""Verbose-mode print gate.

Single source of truth for "should this diagnostic line be shown?". Every
print that's noisy, low-level, or only useful when debugging transport
should route through ``verbose_print()`` instead of going straight to
stdout/stderr. The ``--verbose`` CLI flag flips this on; without it,
the calls are no-ops.

Usage::

    from agentcommander._verbose import verbose_print
    verbose_print(f"[conn] {sockname} → {peer}")

The flag is process-global. Round-47 introduced this module because
``[conn] (...) → host:port`` lifecycle prints were polluting the TUI
banner; gating them lets the user opt in via ``ac --verbose`` when
debugging provider transport, but keeps the default launch quiet.
"""
from __future__ import annotations

import sys

_enabled: bool = False


def set_enabled(value: bool) -> None:
    """Toggle verbose output. Called by ``cli.main`` after parsing args."""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    """Return whether verbose output is currently on."""
    return _enabled


def verbose_print(*args: object, **kwargs: object) -> None:
    """Like ``print`` but only fires when ``--verbose`` is on.

    Defaults to writing to ``sys.stderr`` so the diagnostic doesn't
    interleave with the TUI's stdout-driven status line / role table.
    Override with ``file=...`` if you want stdout (rare).
    """
    if not _enabled:
        return
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)  # type: ignore[arg-type]
