"""Thin SQL wrappers — one function per query.

Imports are kept narrow so this module stays a clean boundary:
modules that need persistence call these; nobody else writes raw SQL.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from agentcommander.db.connection import get_db
from agentcommander.types import Conversation, Message, ProviderConfig, Role


def _now_ms() -> int:
    return int(time.time() * 1000)


# ─── Conversations ─────────────────────────────────────────────────────────


def list_conversations() -> list[Conversation]:
    rows = get_db().execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "WHERE archived = 0 ORDER BY updated_at DESC"
    ).fetchall()
    return [Conversation(id=r["id"], title=r["title"],
                         created_at=r["created_at"], updated_at=r["updated_at"]) for r in rows]


def create_conversation(title: str, working_directory: str | None = None) -> Conversation:
    conv = Conversation(id=str(uuid.uuid4()), title=title,
                        created_at=_now_ms(), updated_at=_now_ms())
    get_db().execute(
        "INSERT INTO conversations (id, title, working_directory, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv.id, conv.title, working_directory, conv.created_at, conv.updated_at),
    )
    return conv


def get_conversation(conv_id: str) -> Conversation | None:
    row = get_db().execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
        (conv_id,),
    ).fetchone()
    if row is None:
        return None
    return Conversation(id=row["id"], title=row["title"],
                        created_at=row["created_at"], updated_at=row["updated_at"])


def delete_conversation(conv_id: str) -> None:
    get_db().execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def touch_conversation(conv_id: str) -> None:
    get_db().execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (_now_ms(), conv_id))


# ─── Messages ──────────────────────────────────────────────────────────────


def list_messages(conv_id: str) -> list[Message]:
    rows = get_db().execute(
        "SELECT id, conversation_id, role, content, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY created_at ASC",
        (conv_id,),
    ).fetchall()
    return [Message(id=r["id"], conversation_id=r["conversation_id"],
                    role=r["role"], content=r["content"], created_at=r["created_at"])
            for r in rows]


def append_message(conv_id: str, role: str, content: str) -> Message:
    msg = Message(id=str(uuid.uuid4()), conversation_id=conv_id,
                  role=role, content=content, created_at=_now_ms())  # type: ignore[arg-type]
    db = get_db()
    db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (msg.id, msg.conversation_id, msg.role, msg.content, msg.created_at),
    )
    # Auto-title: the first user message becomes the conversation title so
    # /history shows something scannable instead of every row reading
    # "Conversation". Only fires while the title is still the default and
    # this is a user turn — keeps explicit /new <title> overrides intact.
    if role == "user":
        row = db.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        if row is not None and row["title"] in ("Conversation", "New conversation", ""):
            derived = " ".join((content or "").split())[:60].strip()
            if derived:
                db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (derived, conv_id),
                )
    touch_conversation(conv_id)
    return msg


# ─── Config (key-value JSON) ───────────────────────────────────────────────


def get_config(key: str, fallback: Any = None) -> Any:
    row = get_db().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is None:
        return fallback
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return fallback


def set_config(key: str, value: Any) -> None:
    get_db().execute(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), _now_ms()),
    )


# ─── Providers ─────────────────────────────────────────────────────────────


def list_providers() -> list[ProviderConfig]:
    rows = get_db().execute(
        "SELECT id, type, name, endpoint, api_key, enabled FROM providers ORDER BY name"
    ).fetchall()
    return [ProviderConfig(id=r["id"], type=r["type"], name=r["name"],
                           endpoint=r["endpoint"], api_key=r["api_key"],
                           enabled=bool(r["enabled"])) for r in rows]


def upsert_provider(p: ProviderConfig) -> None:
    get_db().execute(
        "INSERT INTO providers (id, type, name, endpoint, api_key, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  type = excluded.type, name = excluded.name, endpoint = excluded.endpoint, "
        "  api_key = excluded.api_key, enabled = excluded.enabled",
        (p.id, p.type, p.name, p.endpoint, p.api_key, 1 if p.enabled else 0, _now_ms()),
    )


def delete_provider(provider_id: str) -> None:
    get_db().execute("DELETE FROM providers WHERE id = ?", (provider_id,))


def get_provider(provider_id: str) -> ProviderConfig | None:
    row = get_db().execute(
        "SELECT id, type, name, endpoint, api_key, enabled FROM providers WHERE id = ?",
        (provider_id,),
    ).fetchone()
    if row is None:
        return None
    return ProviderConfig(id=row["id"], type=row["type"], name=row["name"],
                          endpoint=row["endpoint"], api_key=row["api_key"],
                          enabled=bool(row["enabled"]))


# ─── Role assignments ──────────────────────────────────────────────────────


def get_role_assignment(role: Role | str) -> dict[str, Any] | None:
    role_value = role.value if isinstance(role, Role) else role
    row = get_db().execute(
        "SELECT role, provider_id, model, is_override, context_window_tokens "
        "FROM role_assignments WHERE role = ?",
        (role_value,),
    ).fetchone()
    if row is None:
        return None
    return {
        "role": row["role"],
        "provider_id": row["provider_id"],
        "model": row["model"],
        "is_override": bool(row["is_override"]),
        "context_window_tokens": row["context_window_tokens"],
    }


def set_role_assignment(role: Role | str, provider_id: str, model: str,
                        is_override: bool = True,
                        context_window_tokens: int | None = None) -> None:
    """Upsert one role's binding. ``context_window_tokens`` is the num_ctx
    we want the provider to use; pass ``None`` to inherit the provider's
    runtime default.
    """
    role_value = role.value if isinstance(role, Role) else role
    get_db().execute(
        "INSERT INTO role_assignments "
        "  (role, provider_id, model, is_override, context_window_tokens, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(role) DO UPDATE SET "
        "  provider_id = excluded.provider_id, model = excluded.model, "
        "  is_override = excluded.is_override, "
        "  context_window_tokens = excluded.context_window_tokens, "
        "  updated_at = excluded.updated_at",
        (role_value, provider_id, model, 1 if is_override else 0,
         context_window_tokens, _now_ms()),
    )


def list_role_assignments() -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT role, provider_id, model, is_override, context_window_tokens "
        "FROM role_assignments"
    ).fetchall()
    return [
        {"role": r["role"], "provider_id": r["provider_id"], "model": r["model"],
         "is_override": bool(r["is_override"]),
         "context_window_tokens": r["context_window_tokens"]}
        for r in rows
    ]


def clear_role_assignments() -> int:
    """Delete every row in ``role_assignments``. Returns the number of rows
    removed. Used by ``/autoconfig clear`` to wipe persisted picks before
    rebuilding the in-memory autoconfig.
    """
    cur = get_db().execute("DELETE FROM role_assignments")
    return cur.rowcount or 0


# ─── Audit log ─────────────────────────────────────────────────────────────


def audit(event_type: str, details: Any = None) -> None:
    get_db().execute(
        "INSERT INTO audit_log (event_type, details, created_at) VALUES (?, ?, ?)",
        (event_type, json.dumps(details) if details is not None else None, _now_ms()),
    )


# ─── Model hints (TypeCast accumulator) ────────────────────────────────────


def bump_hint(model_id: str, role: Role | str, delta: float) -> float:
    """Apply a ±delta to (model, role) hint score, clamped to [-100, 100]."""
    role_value = role.value if isinstance(role, Role) else role
    db = get_db()
    row = db.execute(
        "SELECT score, runs FROM model_hints WHERE model_id = ? AND role = ?",
        (model_id, role_value),
    ).fetchone()
    if row is None:
        score = max(-100.0, min(100.0, delta))
        db.execute(
            "INSERT INTO model_hints (model_id, role, score, runs, last_bump_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (model_id, role_value, score, _now_ms()),
        )
    else:
        score = max(-100.0, min(100.0, float(row["score"]) + delta))
        db.execute(
            "UPDATE model_hints SET score = ?, runs = runs + 1, last_bump_at = ? "
            "WHERE model_id = ? AND role = ?",
            (score, _now_ms(), model_id, role_value),
        )
    return score


def get_hint(model_id: str, role: Role | str) -> float:
    role_value = role.value if isinstance(role, Role) else role
    row = get_db().execute(
        "SELECT score FROM model_hints WHERE model_id = ? AND role = ?",
        (model_id, role_value),
    ).fetchone()
    return float(row["score"]) if row else 0.0


# ─── Pipeline run tracking ─────────────────────────────────────────────────


def insert_pipeline_run(run_id: str, conversation_id: str) -> None:
    get_db().execute(
        "INSERT INTO pipeline_runs (id, conversation_id, started_at, status, iterations) "
        "VALUES (?, ?, ?, 'running', 0)",
        (run_id, conversation_id, _now_ms()),
    )


def update_pipeline_run(run_id: str, *, status: str, iterations: int,
                        category: str | None = None, error: str | None = None) -> None:
    get_db().execute(
        "UPDATE pipeline_runs "
        "SET ended_at = ?, status = ?, iterations = ?, category = COALESCE(?, category), error = ? "
        "WHERE id = ?",
        (_now_ms(), status, iterations, category, error, run_id),
    )


def insert_pipeline_step(run_id: str, *, iteration: int, step_type: str, name: str,
                         input_text: str | None = None, output_text: str | None = None,
                         duration_ms: int | None = None) -> None:
    get_db().execute(
        "INSERT INTO pipeline_steps (run_id, iteration, step_type, name, input, output, duration_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, iteration, step_type, name, input_text, output_text, duration_ms, _now_ms()),
    )


def insert_token_usage(*, conversation_id: str | None, role: str,
                       provider_id: str | None, model: str | None,
                       prompt_tokens: int | None, completion_tokens: int | None,
                       duration_ms: int | None) -> None:
    get_db().execute(
        "INSERT INTO token_usage (conversation_id, role, provider_id, model, "
        "  prompt_tokens, completion_tokens, duration_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (conversation_id, role, provider_id, model,
         prompt_tokens, completion_tokens, duration_ms, _now_ms()),
    )


# ─── Scratchpad (model-facing memory) ──────────────────────────────────────
#
# These wrap `scratchpad_entries` — the persisted, cross-turn equivalent of
# the in-memory scratchpad list. The user-view `messages` table is left
# alone; compaction only writes here.

def insert_scratchpad_entry(
    *,
    conversation_id: str,
    run_id: str | None,
    step: int,
    role: str,
    action: str,
    input_text: str,
    output_text: str,
    timestamp: float,
    duration_ms: int | None = None,
    content: str | None = None,
    message_id: str | None = None,
    replaced_message_ids: list[str] | None = None,
    is_replaced: bool = False,
    entry_id: str | None = None,
) -> str:
    """Persist one scratchpad row. Returns the (possibly auto-generated) id."""
    eid = entry_id or str(uuid.uuid4())
    rmi = json.dumps(replaced_message_ids) if replaced_message_ids else None
    get_db().execute(
        "INSERT INTO scratchpad_entries "
        "  (id, conversation_id, run_id, step, role, action, input, output, "
        "   duration_ms, content, message_id, replaced_message_ids, "
        "   is_replaced, timestamp, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, conversation_id, run_id, step, role, action,
         input_text or "", output_text or "",
         duration_ms, content, message_id, rmi,
         1 if is_replaced else 0, timestamp, _now_ms()),
    )
    return eid


def list_scratchpad_entries(
    conversation_id: str,
    *,
    include_replaced: bool = False,
) -> list[dict[str, Any]]:
    """Return all scratchpad rows for a conversation in timestamp order.

    `include_replaced=False` (default) hides entries that compaction has
    superseded — exactly what the prompt-builder wants. Pass True for
    audit/inspection paths.
    """
    sql = (
        "SELECT id, conversation_id, run_id, step, role, action, input, "
        "       output, duration_ms, content, message_id, "
        "       replaced_message_ids, is_replaced, timestamp "
        "FROM scratchpad_entries WHERE conversation_id = ?"
    )
    params: tuple[Any, ...] = (conversation_id,)
    if not include_replaced:
        sql += " AND is_replaced = 0"
    sql += " ORDER BY timestamp ASC, step ASC, created_at ASC"
    rows = get_db().execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        rmi_raw = r["replaced_message_ids"]
        try:
            rmi = json.loads(rmi_raw) if rmi_raw else None
        except (TypeError, ValueError):
            rmi = None
        out.append({
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "run_id": r["run_id"],
            "step": r["step"],
            "role": r["role"],
            "action": r["action"],
            "input": r["input"],
            "output": r["output"],
            "duration_ms": r["duration_ms"],
            "content": r["content"],
            "message_id": r["message_id"],
            "replaced_message_ids": rmi,
            "is_replaced": bool(r["is_replaced"]),
            "timestamp": r["timestamp"],
        })
    return out


def mark_scratchpad_replaced(entry_ids: list[str]) -> int:
    """Flag the given scratchpad entries as superseded by a compaction.

    Returns the number of rows updated. The originals stay in the table for
    replay/inspection — only the prompt-builder filters them out.
    """
    if not entry_ids:
        return 0
    placeholders = ",".join("?" * len(entry_ids))
    cur = get_db().execute(
        f"UPDATE scratchpad_entries SET is_replaced = 1 WHERE id IN ({placeholders})",
        tuple(entry_ids),
    )
    return cur.rowcount or 0


def clear_scratchpad(conversation_id: str) -> int:
    """Delete every scratchpad row for one conversation. Returns rowcount.
    Used when starting a fresh /new conversation, not by compaction."""
    cur = get_db().execute(
        "DELETE FROM scratchpad_entries WHERE conversation_id = ?",
        (conversation_id,),
    )
    return cur.rowcount or 0


# ─── Active conversation tracker (mirror handshake) ───────────────────────
#
# The primary process writes the currently-displayed conversation id into
# config so a read-only mirror knows which conversation to follow. Cleared
# (set to None) by `/chat clear`; updated by `/chat new`, `/chat resume`,
# and the startup auto-resume.

ACTIVE_CONVERSATION_KEY = "active_conversation_id"


def set_active_conversation_id(conv_id: str | None) -> None:
    """Persist the active conversation id so a mirror can follow it.

    Pass ``None`` to clear (e.g. after ``/chat clear`` deletes the chat).
    """
    set_config(ACTIVE_CONVERSATION_KEY, conv_id)


def get_active_conversation_id() -> str | None:
    raw = get_config(ACTIVE_CONVERSATION_KEY, None)
    return raw if isinstance(raw, str) else None


# ─── Pipeline events (mirror live stream) ─────────────────────────────────
#
# Each engine event the primary wants the mirror to see lands here as one
# row. Payload is JSON, shape varies by event_type. Mirror polls
# `id > last_seen_id` and renders in id-order.

def insert_pipeline_event(
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
) -> int:
    """Append one event to the mirror stream. Returns the new event id.

    Cheap-by-design: a single INSERT, no joins, no triggers. ``payload`` is
    serialized to JSON; pass dataclass dicts (asdict) or plain dicts.

    Returns 0 on any DB error (locked, table missing on a half-migrated
    DB, etc.). The tee path swallows non-zero failures silently — losing
    a mirror event must never break the primary's pipeline.
    """
    body = json.dumps(payload or {}, default=_json_default)
    try:
        cur = get_db().execute(
            "INSERT INTO pipeline_events (conversation_id, run_id, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, run_id, event_type, body, _now_ms()),
        )
        return int(cur.lastrowid or 0)
    except sqlite3.DatabaseError:
        return 0


def list_pipeline_events_after(last_id: int, *, limit: int = 1000) -> list[dict[str, Any]]:
    """Mirror-side poll: return events with ``id > last_id`` in id order.

    Returns ``[]`` if the table doesn't exist yet — this happens when the
    mirror attaches to a DB created by an older AgentCommander build that
    pre-dates the migration. Once primary opens the DB once with the new
    schema, the table appears and the mirror picks up live events on its
    next tick.
    """
    try:
        rows = get_db().execute(
            "SELECT id, conversation_id, run_id, event_type, payload, created_at "
            "FROM pipeline_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (last_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            payload = {}
        out.append({
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "run_id": r["run_id"],
            "event_type": r["event_type"],
            "payload": payload,
            "created_at": r["created_at"],
        })
    return out


def latest_pipeline_event_id() -> int:
    """Highest id in the events table, or 0 if empty / missing.

    Mirror calls this once at startup so it doesn't replay the entire
    historical event stream — only events that arrive AFTER mirror attached.
    Returns 0 when the table doesn't exist (older DB, mirror attaches
    before primary's first run with the new schema).
    """
    try:
        row = get_db().execute(
            "SELECT MAX(id) AS m FROM pipeline_events"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row["m"] is None:
        return 0
    return int(row["m"])


def prune_pipeline_events(older_than_ms: int) -> int:
    """Delete events older than the given epoch-ms cutoff. Returns rowcount.

    Called once on primary startup to keep the DB from growing unboundedly
    across long sessions. Mirror sees only the live tail anyway, so trimming
    history is harmless. Returns 0 silently when the table doesn't exist
    (fresh DB during migration window).
    """
    try:
        cur = get_db().execute(
            "DELETE FROM pipeline_events WHERE created_at < ?",
            (older_than_ms,),
        )
        return cur.rowcount or 0
    except sqlite3.DatabaseError:
        return 0


# ─── Status bar state mirror ───────────────────────────────────────────────
#
# Primary serializes its StatusState every change and on each timer tick.
# Mirror reads this on poll and applies fields onto its own bar so the user
# sees role/model/tokens/ctx/timers exactly as the primary shows them.

BAR_STATE_KEY = "bar_state_json"


def set_bar_state(state_dict: dict[str, Any]) -> None:
    set_config(BAR_STATE_KEY, state_dict)


def get_bar_state() -> dict[str, Any] | None:
    raw = get_config(BAR_STATE_KEY, None)
    return raw if isinstance(raw, dict) else None


def _json_default(obj: Any) -> Any:
    """Best-effort coercion for non-stdlib-JSON types in event payloads."""
    if hasattr(obj, "value"):  # Enum
        return obj.value
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)
