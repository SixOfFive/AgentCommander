"""Read-only mirror — passive follower of a primary AgentCommander process.

Started via ``ac --mirror``. Opens the project's SQLite DB **read-only**
(``mode=ro`` URI, no application-level lock) so it coexists with a primary
or even starts before primary exists. Polls the live event stream
(``pipeline_events``) and the bar-state snapshot (``config.bar_state_json``)
so the watcher sees role/model/tokens/ctx/timers and live streamed text
just as the primary does, with ~250 ms lag.

Allowed input: ``/exit`` and ``/quit`` only. Anything else triggers a hint
and is dropped. On exit the mirror does NOT call ``provider.unload`` —
primary owns the loaded models, and we must not drop them out from under it.

Design notes
------------
* No bootstrap of providers/tools/catalog. The mirror never makes outbound
  network calls or runs code on the host. It's pure DB reads + screen paint.
* Conversation switches (``/chat new`` / ``resume`` / ``clear`` on primary)
  are detected by polling ``config.active_conversation_id``. When it
  changes, mirror clears the screen, replays the new conversation's
  ``messages`` history, and continues following.
* Initial attachment: mirror replays only the active conversation's stored
  ``messages`` so the watcher has context, then captures the current
  ``MAX(pipeline_events.id)`` as the event cursor — historical events that
  were already pruned aren't replayable, and we don't want to flood the
  screen with stale chunks anyway.
* All renders go through the existing render functions, so styling and
  markdown handling match exactly what primary shows.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from agentcommander import __version__
from agentcommander.tui.ansi import (
    SHOW_CURSOR,
    enable_ansi,
    style,
    write,
    writeln,
)
from agentcommander.tui.render import (
    render_assistant_message,
    render_banner,
    render_role_delta,
    render_system_line,
    render_user_message,
)
from agentcommander.tui.status_bar import (
    _MIRRORED_BAR_FIELDS,
    _apply_dict_to_state,
    get_status_bar,
)
from agentcommander.tui.terminal_input import poll_chars, raw_mode

# Poll cadence. 200 ms is responsive enough that streamed text feels live
# while keeping the SQLite read pressure trivial.
POLL_INTERVAL_S = 0.20

# ANSI: clear from cursor to end of screen — used when switching the
# displayed conversation so the new one doesn't overlap the old.
_CLEAR_BELOW = "\x1b[J"


def _project_db_path() -> Path:
    """Same path as init_db's default — the mirror runs in the project dir
    and watches that project's DB. (No way to point it at another project
    for now; if needed, wire a --working-dir like the primary has.)"""
    return Path.cwd() / ".agentcommander" / "db.sqlite"


def _wait_for_db(path: Path, *, on_tick) -> None:
    """Block until ``path`` exists. Yields control via ``on_tick`` between
    checks so the status bar timer / spinner stays alive while we wait."""
    while not path.exists():
        on_tick()
        time.sleep(POLL_INTERVAL_S * 2)


def _replay_conversation(conv_id: str | None) -> int:
    """Render the active conversation's stored messages. Returns the id of
    the last event we should treat as "already seen" — the high-water mark
    primary's events stream is at right now, so polling starts AFTER it.

    Tolerant of: missing pipeline_events table (older DB pre-migration),
    missing messages, transient DB errors. Returns 0 in any error path so
    the next poll picks up everything new from id > 0.
    """
    from agentcommander.db.repos import (
        latest_pipeline_event_id,
        list_messages,
    )

    if not conv_id:
        render_system_line(style("muted",
            "  (no active conversation — waiting for primary to start one)"))
        try:
            return latest_pipeline_event_id()
        except Exception:  # noqa: BLE001
            return 0

    try:
        msgs = list_messages(conv_id)
    except Exception as exc:  # noqa: BLE001
        render_system_line(style("warn", f"  failed to load messages: {exc}"))
        msgs = []

    render_system_line(style("muted",
        f"  following chat {conv_id[:8]} "
        f"({len(msgs)} message(s) of history)"))
    for m in msgs:
        if m.role == "user":
            render_user_message(m.content)
        elif m.role == "assistant":
            render_assistant_message(m.content, markdown=True)

    # Snap event cursor to "now" so we don't replay everything that's
    # already on disk — only events that arrive AFTER attach. If the
    # events table doesn't exist yet (older DB), 0 is the safe answer
    # and the next tick will retry.
    try:
        return latest_pipeline_event_id()
    except Exception:  # noqa: BLE001
        return 0


def _apply_bar_state(snapshot: dict[str, Any] | None) -> None:
    """Copy primary's StatusState fields into the mirror's bar (in-memory),
    leaving local-only fields alone (workdir, mirror_mode, pending_input)."""
    if not snapshot:
        return
    bar = get_status_bar()
    _apply_dict_to_state(bar.state, snapshot)
    bar.redraw()


def _render_event(evt: dict[str, Any], active_conv_id: str | None) -> None:
    """Apply one streamed pipeline event to the screen.

    Events for OTHER conversations are dropped — we only mirror the
    currently-active chat. Most event payloads piggy-back on the
    ``PipelineEvent.__dict__`` shape, so we re-construct just enough to
    drive ``render_event`` / ``render_role_delta`` directly.
    """
    if evt.get("conversation_id") and evt.get("conversation_id") != active_conv_id:
        return
    et = evt.get("event_type") or ""
    payload = evt.get("payload") or {}

    if et == "user/message":
        text = payload.get("text") or ""
        if text:
            render_user_message(text)
        return

    if et == "assistant/final":
        text = payload.get("text") or ""
        if text:
            render_assistant_message(text, markdown=True)
        return

    if et == "role/start":
        # Primary already prints the role header on first-delta via
        # render_role_delta — so a separate header here would duplicate.
        # Just nothing to render visually; bar state covers the metadata.
        return

    if et == "role/delta":
        role = payload.get("role") or "?"
        text = payload.get("text") or ""
        if text:
            render_role_delta(role, text)
        return

    if et == "role/end":
        # Streaming closes naturally on the next role-start or the final.
        # If we want a visible boundary marker, primary's engine/done event
        # will render the assistant message via assistant/final.
        return

    if et.startswith("engine/"):
        # Forwarded PipelineEvent. Reconstruct a thin object that
        # render_event can consume.
        kind = et[len("engine/"):]
        if kind == "guard":
            family = payload.get("family") or "?"
            reason = payload.get("reason") or ""
            writeln(style("guard_label", f"  ⌫ guard:{family}  ")
                    + style("muted", f"({reason})"))
            return
        if kind == "iteration":
            extra = payload.get("extra") or {}
            action = payload.get("action")
            if action:
                writeln(style("iter_marker",
                              f"  ⟳ iter {payload.get('iteration')}  →  ")
                        + style("iter_action", action))
            elif "category" in extra:
                writeln(style("muted",
                              f"  router: category = {extra['category']}"))
            return
        if kind == "tool":
            ok = bool(payload.get("ok"))
            tool = payload.get("tool") or "?"
            marker = "✓" if ok else "✗"
            writeln(f"  {marker} " + style("tool_marker", f"tool:{tool}"))
            err = payload.get("error")
            out = payload.get("output")
            if err:
                writeln(style("tool_err", "    " + str(err)))
            elif out:
                # Don't bother re-wrapping; primary already wrapped the
                # text it streamed. Trim to a sensible height.
                snippet = str(out)
                if len(snippet) > 600:
                    snippet = snippet[:600] + "…"
                writeln("    " + snippet.replace("\n", "\n    "))
            return
        if kind == "error":
            writeln(style("error", f"  ⚠ {payload.get('error') or 'error'}"))
            return
        # done is handled via assistant/final tee; ignore here.
        return


def _drain_input(buffer: str, chunk: str) -> tuple[str, str | None]:
    """Accumulate a typed chunk into ``buffer``. Returns ``(new_buffer, line)``
    where ``line`` is the submitted text on Enter (and the buffer resets)."""
    new_buf = buffer
    i = 0
    n = len(chunk)
    while i < n:
        ch = chunk[i]
        # Windows special-key prefix → swallow the next byte.
        if ch in ("\x00", "\xe0"):
            i += 2
            continue
        # POSIX escape: skip any CSI/SS3 sequence so arrow keys don't leak.
        if ch == "\x1b":
            i += 1
            if i < n and chunk[i] == "[":
                i += 1
                while i < n:
                    c2 = chunk[i]
                    i += 1
                    if c2 == "~" or ("A" <= c2 <= "Z") or ("a" <= c2 <= "z"):
                        break
            elif i < n and chunk[i] == "O":
                i += 2
            continue
        i += 1
        if ch in ("\r", "\n"):
            line = new_buf.strip()
            return "", line
        if ch in ("\x7f", "\x08"):
            new_buf = new_buf[:-1]
            continue
        if ord(ch) < 32:
            continue
        new_buf += ch
    return new_buf, None


def run_mirror() -> int:
    """Entry point — reads the project DB, follows the primary indefinitely."""
    enable_ansi()
    db_path = _project_db_path()

    bar = get_status_bar()
    bar.set_workdir(str(Path.cwd()))
    bar.set_mirror_mode(True)
    bar.install()

    render_banner(
        version=__version__,
        providers_count=0,
        models_count=0,
        working_dir=str(Path.cwd()),
    )
    render_system_line(style("warn",
        "  ▸ MIRROR MODE (read-only) — only /exit and /quit are accepted"))
    render_system_line(style("muted",
        "    primary's role/model, tokens, context, and live tokens "
        "stream here"))

    if not db_path.exists():
        render_system_line(style("muted",
            f"  waiting for primary to create {db_path} …"))
        _wait_for_db(db_path, on_tick=bar.redraw)
        render_system_line(style("muted", "  primary DB found — attaching"))

    # Open RO. Skip lock, no schema, no signal-write hooks. Reuses the
    # standard `_db` slot so the existing repos.py SELECT helpers work
    # against the read-only connection without any modification.
    from agentcommander.db.connection import init_db_readonly
    try:
        init_db_readonly(db_path)
    except Exception as exc:  # noqa: BLE001
        render_system_line(style("error",
            f"  failed to open mirror DB: {type(exc).__name__}: {exc}"))
        bar.uninstall()
        return 1

    # Initial state: read active conv, replay messages, capture cursors.
    from agentcommander.db.repos import (
        get_active_conversation_id,
        get_bar_state,
        list_pipeline_events_after,
        list_conversations,
    )

    def _resolve_active() -> str | None:
        """Active conv = config row, or fallback to most-recent."""
        cid = get_active_conversation_id()
        if cid:
            return cid
        try:
            convs = list_conversations()
            if convs:
                return convs[0].id
        except Exception:  # noqa: BLE001
            pass
        return None

    active_conv_id: str | None = _resolve_active()
    last_event_id = _replay_conversation(active_conv_id)
    _apply_bar_state(get_bar_state())

    typed: str = ""
    should_exit = False

    with raw_mode():
        while not should_exit:
            try:
                tick_ok = True
                # ── 1. Conversation switch detection ──────────────────
                new_active = _resolve_active()
                if new_active != active_conv_id:
                    # Clear scroll-region content, redraw banner-ish line,
                    # replay new conv. We don't strictly need to clear, but
                    # a divider keeps the watcher oriented.
                    bar.park_cursor()
                    write(_CLEAR_BELOW)
                    if new_active is None:
                        render_system_line(style("warn",
                            "  ◇ primary cleared the active chat"))
                    else:
                        render_system_line(style("warn",
                            f"  ◇ primary switched to chat {new_active[:8]}"))
                    active_conv_id = new_active
                    last_event_id = _replay_conversation(active_conv_id)

                # ── 2. Drain new events ───────────────────────────────
                try:
                    new_events = list_pipeline_events_after(last_event_id, limit=500)
                except Exception:  # noqa: BLE001
                    new_events = []
                for evt in new_events:
                    try:
                        _render_event(evt, active_conv_id)
                    except Exception:  # noqa: BLE001
                        # Never let a render error kill the mirror.
                        pass
                    last_event_id = max(last_event_id, int(evt.get("id") or 0))

                # ── 3. Apply bar snapshot ─────────────────────────────
                try:
                    _apply_bar_state(get_bar_state())
                except Exception:  # noqa: BLE001
                    pass

                # ── 4. Handle input ───────────────────────────────────
                chunk = poll_chars()
                if chunk:
                    typed, line = _drain_input(typed, chunk)
                    if line is not None:
                        if line in ("/exit", "/quit", "/q"):
                            should_exit = True
                        elif line:
                            render_system_line(style("muted",
                                f"  mirror is read-only — '{line}' ignored. "
                                "use /exit or /quit to leave."))

                time.sleep(POLL_INTERVAL_S)
            except KeyboardInterrupt:
                should_exit = True
                break
            except Exception:  # noqa: BLE001
                # Catch-all: a transient DB error (locked, malformed
                # row, etc.) must not kill the mirror. Sleep a beat and
                # try again next tick. Errors are intentionally silent —
                # the mirror is a passive observer, not a debugger.
                time.sleep(POLL_INTERVAL_S * 2)
                continue

    bar.uninstall()
    write(SHOW_CURSOR)
    writeln(style("muted",
        "  mirror exited (primary unaffected — its loaded models stay)"))
    return 0


# Suppress unused-import noise while keeping symbols available for tests.
_ = (os, _MIRRORED_BAR_FIELDS)
