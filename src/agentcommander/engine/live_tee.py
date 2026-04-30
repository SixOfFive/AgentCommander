"""Live event + status-bar tee for the read-only mirror.

Primary AgentCommander writes a stream of events into ``pipeline_events``
and a snapshot of its status-bar state into ``config.bar_state_json``.
``ac --mirror`` polls both and replays them so the watcher sees the same
activity primary sees, with ~250ms lag.

Why not a sidecar log file?
  - Single source of truth: the same DB already holds messages and
    scratchpad. One file, one lock-free reader contract via WAL.
  - Mirror needs zero extra state to discover the stream — it just opens
    the project's `.agentcommander/db.sqlite`.

Volume management
  - Token deltas can hit 60+/sec. Each is a tiny INSERT (~80 bytes payload,
    sub-ms on SSD). Sustained, that's fine for WAL+sync=FULL.
  - We coalesce delta chunks: instead of one INSERT per token, we buffer
    text per-(role, model) and flush at ~10 Hz or when role ends. That
    keeps the live experience responsive without thrashing the disk.
  - Primary prunes events older than 1h on startup (see app bootstrap).

Mirror safety
  - Every tee call swallows exceptions. A failed write to the live stream
    must NEVER kill the primary's pipeline — the mirror is a luxury, not
    a dependency.
  - The mirror process never imports this module. All writes happen on the
    primary's connection.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agentcommander.db.repos import (
    insert_pipeline_event,
    set_bar_state,
)


# How long to buffer delta text before flushing. 100 ms = 10 Hz, smooth to
# the eye, ~10 INSERTs/sec of coalesced text instead of 60+ raw token writes.
DELTA_FLUSH_INTERVAL_S = 0.10


@dataclass
class _DeltaBuffer:
    """In-memory accumulator for one role's streaming text.

    A single buffer is keyed on (role, model); when the role changes we
    flush the previous buffer immediately so events stay ordered.
    """
    role: str
    model: str
    text: str = ""
    last_flush: float = field(default_factory=time.monotonic)
    conversation_id: str | None = None
    run_id: str | None = None


# Module-level state. The primary process is single-threaded engine-side
# (one PipelineRun at a time), but the timer-tick thread can also redraw
# the bar — guard with a lock so deltas don't tear.
_lock = threading.Lock()
_active_buf: _DeltaBuffer | None = None


def _safe_emit(event_type: str, payload: dict[str, Any] | None = None,
               *, conversation_id: str | None = None,
               run_id: str | None = None) -> None:
    """Insert one event row, swallowing any DB failure.

    The primary's pipeline must keep running even if the events table is
    locked, the disk fills, or the migration didn't apply yet. Mirror is
    eventual-consistency by design; a missed event is acceptable.
    """
    try:
        insert_pipeline_event(
            event_type=event_type,
            payload=payload or {},
            conversation_id=conversation_id,
            run_id=run_id,
        )
    except Exception:  # noqa: BLE001
        # Intentional: never propagate tee failures into the engine.
        pass


def tee_event(event_type: str, payload: dict[str, Any] | None = None,
              *, conversation_id: str | None = None,
              run_id: str | None = None,
              flush_deltas: bool = True) -> None:
    """Tee one engine-level event to the mirror stream.

    ``flush_deltas`` forces any pending delta-buffer to drain BEFORE the
    new event is recorded, so the mirror sees text in the order it actually
    happened (the model produced 'hello world' BEFORE the role/end fired,
    not after).
    """
    if flush_deltas:
        flush_deltas_now()
    _safe_emit(event_type, payload,
               conversation_id=conversation_id, run_id=run_id)


def tee_role_start(role: str, model: str, num_ctx: int | None,
                   *, conversation_id: str | None = None,
                   run_id: str | None = None) -> None:
    """Begin a new role's streaming window. Flushes any prior buffer."""
    global _active_buf
    with _lock:
        # Flush previous role's leftover text, if any. Different role =
        # different buffer; we can't merge them.
        if _active_buf is not None and _active_buf.text:
            _safe_emit("role/delta", {
                "role": _active_buf.role,
                "model": _active_buf.model,
                "text": _active_buf.text,
            }, conversation_id=_active_buf.conversation_id,
               run_id=_active_buf.run_id)
            _active_buf.text = ""
        _active_buf = _DeltaBuffer(
            role=role, model=model,
            conversation_id=conversation_id, run_id=run_id,
        )
    _safe_emit("role/start", {
        "role": role,
        "model": model,
        "num_ctx": num_ctx,
    }, conversation_id=conversation_id, run_id=run_id)


def tee_delta(role: str, model: str, text: str,
              *, conversation_id: str | None = None,
              run_id: str | None = None) -> None:
    """Accumulate one streamed-text chunk. Flushes periodically.

    Called from the primary's wrapped ``on_role_delta`` callback. Empty
    deltas are dropped. If we've been buffering for more than
    ``DELTA_FLUSH_INTERVAL_S`` since the last flush, this call also flushes
    — that gives the mirror its smooth ~10Hz update cadence without us
    needing a background timer thread.
    """
    if not text:
        return
    now = time.monotonic()
    flush_payload: dict[str, Any] | None = None
    flush_meta: tuple[str | None, str | None] = (conversation_id, run_id)
    with _lock:
        global _active_buf
        if _active_buf is None or _active_buf.role != role or _active_buf.model != model:
            # No active buffer or role changed. Open a fresh one.
            if _active_buf is not None and _active_buf.text:
                # Persist the previous role's tail before swapping.
                _safe_emit("role/delta", {
                    "role": _active_buf.role,
                    "model": _active_buf.model,
                    "text": _active_buf.text,
                }, conversation_id=_active_buf.conversation_id,
                   run_id=_active_buf.run_id)
            _active_buf = _DeltaBuffer(
                role=role, model=model,
                conversation_id=conversation_id, run_id=run_id,
                last_flush=now,
            )
        _active_buf.text += text
        if (now - _active_buf.last_flush) >= DELTA_FLUSH_INTERVAL_S:
            flush_payload = {
                "role": _active_buf.role,
                "model": _active_buf.model,
                "text": _active_buf.text,
            }
            flush_meta = (_active_buf.conversation_id, _active_buf.run_id)
            _active_buf.text = ""
            _active_buf.last_flush = now
    if flush_payload is not None:
        _safe_emit("role/delta", flush_payload,
                   conversation_id=flush_meta[0], run_id=flush_meta[1])


def tee_role_end(role: str, model: str,
                 prompt_tokens: int, completion_tokens: int,
                 *, conversation_id: str | None = None,
                 run_id: str | None = None) -> None:
    """Close a role's streaming window. Flushes any pending text first."""
    flush_deltas_now()
    _safe_emit("role/end", {
        "role": role,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }, conversation_id=conversation_id, run_id=run_id)


def flush_deltas_now() -> None:
    """Drain whatever's in the current delta buffer to the events table."""
    global _active_buf
    payload: dict[str, Any] | None = None
    meta: tuple[str | None, str | None] = (None, None)
    with _lock:
        if _active_buf is not None and _active_buf.text:
            payload = {
                "role": _active_buf.role,
                "model": _active_buf.model,
                "text": _active_buf.text,
            }
            meta = (_active_buf.conversation_id, _active_buf.run_id)
            _active_buf.text = ""
            _active_buf.last_flush = time.monotonic()
    if payload is not None:
        _safe_emit("role/delta", payload,
                   conversation_id=meta[0], run_id=meta[1])


def reset_active_buffer() -> None:
    """Drop the in-flight delta buffer. Called at run boundaries so a
    pending tail from a cancelled run doesn't leak into the next one."""
    global _active_buf
    with _lock:
        _active_buf = None


def tee_bar_state(state_dict: dict[str, Any]) -> None:
    """Snapshot the primary's status-bar state to config.

    Single config row updated in place — the mirror only ever needs the
    latest snapshot, not history. Throttling lives in the caller (the
    StatusBar's redraw is debounced).
    """
    try:
        set_bar_state(state_dict)
    except Exception:  # noqa: BLE001
        pass


# ─── Bar-state-throttle helper ────────────────────────────────────────────
#
# StatusBar.redraw is called frequently (every keystroke during typing,
# every event during a run, plus a 1Hz timer tick). Persisting on every
# call would be wasteful — bar_state writes don't need to be more granular
# than the mirror's poll interval (~200ms). The throttle below collapses
# multiple calls inside a window into one DB write.

_LAST_BAR_PERSIST: float = 0.0
_BAR_PERSIST_INTERVAL_S = 0.10


def maybe_tee_bar_state(state_dict: dict[str, Any], *, force: bool = False) -> None:
    """Throttled wrapper around ``tee_bar_state``.

    Call this on every redraw. Persists at most ~10 Hz unless ``force`` is
    set, which is used at run-boundary moments (start, end, role-change)
    so the mirror sees those transitions without waiting for the throttle.
    """
    global _LAST_BAR_PERSIST
    now = time.monotonic()
    if not force and (now - _LAST_BAR_PERSIST) < _BAR_PERSIST_INTERVAL_S:
        return
    _LAST_BAR_PERSIST = now
    tee_bar_state(state_dict)
