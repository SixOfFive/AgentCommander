"""Chat session logging — write user-visible messages to a per-session log file.

For every conversation we maintain a human-readable log at
``<working_dir>/logs/YYYY-MM-DD-HH-MM-SS.log``. The timestamp comes from
the conversation's ``created_at``, so:

  * Resuming an existing chat appends to its existing log.
  * ``/chat new`` creates a fresh conversation → new log file.
  * ``/chat clear`` deletes the conversation; the next prompt creates a
    new one with a new timestamp → new log file.

Only USER prompts and ASSISTANT finals are logged here — this is the
user-facing chat view, not the model-facing scratchpad. Best-effort:
filesystem errors never propagate (the chat must keep working even if
the log write fails).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


_LOGS_DIRNAME = "logs"

# Rotate the active log when it crosses this many bytes. Long sessions can
# otherwise grow unbounded — at ~2 KB per turn average, 10 MB is roughly
# 5000 turns, plenty for any single conversation. After rotation the next
# write starts a fresh file at `<base>.log`; the prior content lands at
# `<base>.001.log` (or 002, 003, …).
_LOG_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB

# Cap on rotated parts kept on disk per conversation. The oldest one is
# deleted to enforce this — prevents a runaway agent from filling the
# disk if it gets stuck in a loop.
_LOG_MAX_PARTS = 20


def _filename_for(created_at_ms: int) -> str:
    """``YYYY-MM-DD-HH-MM-SS.log`` derived from the conversation's start time
    in local time (matches what the user sees in shell ``date`` output)."""
    dt = datetime.fromtimestamp(created_at_ms / 1000.0)
    return dt.strftime("%Y-%m-%d-%H-%M-%S") + ".log"


def chat_log_path(working_dir: str | None, created_at_ms: int) -> Path:
    base = Path(working_dir) if working_dir else Path.cwd()
    return base / _LOGS_DIRNAME / _filename_for(created_at_ms)


def log_message(
    working_dir: str | None,
    created_at_ms: int,
    role: str,
    content: str,
    *,
    msg_time_ms: int | None = None,
) -> None:
    """Append a formatted entry. Best-effort — never raises.

    Format::

        [YYYY-MM-DD HH:MM:SS] ROLE:
        <content>
        <blank line>
    """
    try:
        path = chat_log_path(working_dir, created_at_ms)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        ts = msg_time_ms if msg_time_ms is not None else created_at_ms
        stamp = datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {role.upper()}:\n{content}\n\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 — logging must never break the chat
        pass


def _rotate_if_needed(path: Path) -> None:
    """Roll the log over if it has crossed ``_LOG_ROTATE_BYTES``.

    Layout::

        <base>.log         ← current
        <base>.001.log     ← previous (most recent rotated)
        <base>.002.log
        ...                up to ``_LOG_MAX_PARTS`` rotated parts

    The oldest rotated part is deleted when the cap is hit, so disk use
    is bounded at ``_LOG_ROTATE_BYTES * (_LOG_MAX_PARTS + 1)``.
    Best-effort — swallows OSError so a transient FS issue never breaks
    a chat turn.
    """
    try:
        if not path.exists():
            return
        if path.stat().st_size < _LOG_ROTATE_BYTES:
            return
        stem = path.stem  # "<conv-stamp>"
        parent = path.parent
        # Find the next free index.
        next_idx = 1
        while (parent / f"{stem}.{next_idx:03d}.log").exists():
            next_idx += 1
        rotated = parent / f"{stem}.{next_idx:03d}.log"
        path.rename(rotated)
        # Enforce the cap: delete the lowest-numbered (oldest) rotated
        # part if we're now over the limit. We keep the 20 most-recent.
        all_parts = sorted(parent.glob(f"{stem}.[0-9][0-9][0-9].log"))
        excess = len(all_parts) - _LOG_MAX_PARTS
        for old in all_parts[:max(0, excess)]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass
