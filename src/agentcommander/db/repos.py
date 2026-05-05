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
        "SELECT id, title, working_directory, created_at, updated_at "
        "FROM conversations WHERE archived = 0 ORDER BY updated_at DESC"
    ).fetchall()
    return [Conversation(id=r["id"], title=r["title"],
                         working_directory=r["working_directory"],
                         created_at=r["created_at"],
                         updated_at=r["updated_at"]) for r in rows]


def create_conversation(title: str, working_directory: str | None = None) -> Conversation:
    conv = Conversation(id=str(uuid.uuid4()), title=title,
                        working_directory=working_directory,
                        created_at=_now_ms(), updated_at=_now_ms())
    get_db().execute(
        "INSERT INTO conversations (id, title, working_directory, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv.id, conv.title, working_directory, conv.created_at, conv.updated_at),
    )
    return conv


def get_conversation(conv_id: str) -> Conversation | None:
    row = get_db().execute(
        "SELECT id, title, working_directory, created_at, updated_at "
        "FROM conversations WHERE id = ?",
        (conv_id,),
    ).fetchone()
    if row is None:
        return None
    return Conversation(id=row["id"], title=row["title"],
                        working_directory=row["working_directory"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"])


def delete_conversation(conv_id: str) -> None:
    """Delete a conversation and all its descendant rows.

    ``messages``, ``scratchpad_entries``, and ``pipeline_runs`` cascade
    via FK constraints (ON DELETE CASCADE — see schema.sql). The
    ``pipeline_events`` table is FK-less by design (it serves as a live
    mirror feed + audit log) so we sweep it explicitly here to avoid
    orphaned events piling up after explicit deletion. The startup
    pruner (``prune_pipeline_events``) is the long-term cap; this is
    just immediate cleanup so a deletion is fully visible.

    Also clears the ``active_conversation_id`` config row if it points at
    the conversation being deleted — without this, a later
    ``get_active_conversation_id()`` returns the dead id and any
    ``list_messages`` call silently returns ``[]`` instead of an error,
    making the "where did my chat go?" failure mode hard to debug.
    """
    db = get_db()
    db.execute("DELETE FROM pipeline_events WHERE conversation_id = ?", (conv_id,))
    db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    # If active points here, drop the dangle.
    try:
        active = get_active_conversation_id()
    except Exception:  # noqa: BLE001 — config layer should never break delete
        active = None
    if active == conv_id:
        try:
            set_active_conversation_id(None)
        except Exception:  # noqa: BLE001
            pass


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

    # File-system chat log: append a human-readable line to
    # `<conversation_working_dir>/logs/YYYY-MM-DD-HH-MM-SS.log`. This is the
    # user-facing chat view (NOT the scratchpad). Best-effort — log write
    # failures must never break the chat.
    try:
        from agentcommander.chat_log import log_message
        conv_row = db.execute(
            "SELECT created_at, working_directory FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        if conv_row is not None:
            log_message(
                conv_row["working_directory"],
                conv_row["created_at"],
                role,
                content,
                msg_time_ms=msg.created_at,
            )
    except Exception:  # noqa: BLE001 — never break message persistence over a log
        pass

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


# ─── Operational rules (preflight + postmortem) ────────────────────────────


def insert_operational_rule(
    *,
    fingerprint_version: int,
    action_type: str,
    target_pattern: str | None,
    context_tags: list[str] | None,
    constraint_text: str,
    suggested_reorder: list[dict] | None,
    origin: str,
    confidence: float,
    example_run_id: str | None,
) -> int:
    """Insert a postmortem-derived (or manually authored) rule. Returns row id."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO operational_rules "
        "(fingerprint_version, action_type, target_pattern, context_tags, "
        " constraint_text, suggested_reorder, origin, confidence, "
        " helped_count, hurt_count, example_run_id, created_at, archived) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 0)",
        (
            fingerprint_version,
            action_type,
            target_pattern,
            json.dumps(context_tags) if context_tags else None,
            constraint_text,
            json.dumps(suggested_reorder) if suggested_reorder else None,
            origin,
            float(confidence),
            example_run_id,
            _now_ms(),
        ),
    )
    return int(cur.lastrowid or 0)


def list_operational_rules_for_action(action_type: str) -> list[dict[str, Any]]:
    """Return un-archived rules whose ``action_type`` matches.

    Preflight calls this each iteration to find rules that should be
    quoted to the meta-agent. We don't pre-filter on context_tags here —
    the agent decides relevance from the tags + constraint text.
    """
    rows = get_db().execute(
        "SELECT id, fingerprint_version, action_type, target_pattern, "
        "       context_tags, constraint_text, suggested_reorder, origin, "
        "       confidence, helped_count, hurt_count "
        "FROM operational_rules "
        "WHERE archived = 0 AND action_type = ? "
        "ORDER BY confidence DESC, id DESC",
        (action_type,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            tags = json.loads(r["context_tags"]) if r["context_tags"] else []
        except (TypeError, ValueError):
            tags = []
        try:
            reorder = json.loads(r["suggested_reorder"]) if r["suggested_reorder"] else None
        except (TypeError, ValueError):
            reorder = None
        out.append({
            "id": int(r["id"]),
            "fingerprint_version": int(r["fingerprint_version"]),
            "action_type": r["action_type"],
            "target_pattern": r["target_pattern"],
            "context_tags": tags,
            "constraint_text": r["constraint_text"],
            "suggested_reorder": reorder,
            "origin": r["origin"],
            "confidence": float(r["confidence"]),
            "helped_count": int(r["helped_count"]),
            "hurt_count": int(r["hurt_count"]),
        })
    return out


def bump_rule_outcome(rule_id: int, *, helped: bool) -> None:
    """Increment helped_count / hurt_count for a rule. Called by postmortem
    when a rule's prediction matched (or contradicted) the run outcome."""
    db = get_db()
    if helped:
        db.execute(
            "UPDATE operational_rules SET helped_count = helped_count + 1 "
            "WHERE id = ?", (rule_id,),
        )
    else:
        db.execute(
            "UPDATE operational_rules SET hurt_count = hurt_count + 1 "
            "WHERE id = ?", (rule_id,),
        )


def archive_operational_rule(rule_id: int) -> None:
    get_db().execute(
        "UPDATE operational_rules SET archived = 1 WHERE id = ?", (rule_id,),
    )


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


def list_recent_pipeline_events_for_conv(
    conversation_id: str, *, limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return the ``limit`` MOST-RECENT events for a single conversation,
    in chronological (id ASC) order.

    Used for mirror reattach + primary resume — both want "what just
    happened in this chat" without scanning unrelated events. Without
    this helper, callers were doing ``list_pipeline_events_after(0, limit=N)``
    which pulls the EARLIEST N events globally — wrong when the table
    has more than N total rows from prior sessions.
    """
    if not conversation_id:
        return []
    try:
        rows = get_db().execute(
            "SELECT id, conversation_id, run_id, event_type, payload, created_at "
            "FROM pipeline_events WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
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
    # Caller wants chronological order; SQL gave us reverse, flip it.
    out.reverse()
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


def prune_audit_log(older_than_ms: int | None = None,
                    keep_last: int | None = None) -> int:
    """Trim the audit_log so it doesn't grow unbounded across long sessions.

    Two strategies, applied in order:
      - ``older_than_ms``: delete rows whose ``created_at`` is older than
        this epoch-ms cutoff (e.g. 30 days ago). Default: 30 days.
      - ``keep_last``: after the time-based prune, if the table still has
        more than ``keep_last`` rows, delete the oldest until only
        ``keep_last`` remain. Acts as a hard ceiling so a runaway-event
        burst (test loops, debug spam) can't fill the disk before the
        next time-based prune fires. Default: 100,000.

    Returns total rowcount deleted. Silent on DB errors — auditing is a
    side-channel; failures here must never crash startup.
    """
    import time as _t
    cutoff = (
        older_than_ms
        if older_than_ms is not None
        else int((_t.time() - 30 * 24 * 3600) * 1000)
    )
    keep_n = keep_last if keep_last is not None else 100_000
    db = get_db()
    n_total = 0
    try:
        cur = db.execute(
            "DELETE FROM audit_log WHERE created_at < ?", (cutoff,),
        )
        n_total += cur.rowcount or 0
    except sqlite3.DatabaseError:
        return n_total
    # Hard ceiling: if too many rows remain, drop the oldest.
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
        ).fetchone()
        n = int(row["c"]) if row else 0
        if n > keep_n:
            cur = db.execute(
                "DELETE FROM audit_log WHERE id IN ("
                "  SELECT id FROM audit_log ORDER BY created_at ASC LIMIT ?"
                ")",
                (n - keep_n,),
            )
            n_total += cur.rowcount or 0
    except sqlite3.DatabaseError:
        pass
    return n_total


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


# ─── Model throughput (running tokens/second average) ──────────────────────

# Seed value used when a model has no recorded measurement yet. Picked to be
# vaguely plausible for a small-to-medium local model on consumer hardware
# so the first display reads sensibly; the running average converges to the
# real rate within a few samples.
DEFAULT_TOKENS_PER_SECOND = 100.0


def get_throughput(model: str | None) -> float | None:
    """Return the current running-average tokens/sec for ``model``, or
    ``None`` when no measurement exists yet.

    Returning ``None`` (rather than the historic 100 t/s seed) lets the UI
    render an honest "—" for unmeasured models. ``_fmt_tps`` on the TUI
    side already converts ``None`` to an empty string, so callers don't
    need to special-case it; they just don't show a tok/s badge until
    real data has flowed.
    """
    if not model:
        return None
    try:
        row = get_db().execute(
            "SELECT tokens_per_second FROM model_throughput WHERE model = ?",
            (model,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return float(row["tokens_per_second"])
    except (TypeError, ValueError):
        return None


def record_throughput(model: str | None, completion_tokens: int | None,
                      duration_ms: int | None,
                      *, chars_completed: int | None = None) -> float | None:
    """Update the running-average tokens/sec for ``model`` from one call's
    measurement.

    Formula:

        rate    = completion_tokens / (duration_ms / 1000)
        new_avg = (old_avg + rate) / 2

    When the provider doesn't report ``completion_tokens`` (e.g. some
    llama.cpp builds — Qwen3.6-35B-A3B-UD ships ``usage`` as zeros over
    the streaming OpenAI-compat endpoint), fall back to estimating tokens
    from ``chars_completed`` (chars / 4 ≈ tokens). Without this, the table
    stays pinned to the seed default and ``/status`` shows a meaningless
    100 t/s for every uncatalogued local model.

    Also mirrors the observation into the side-by-side
    ``model_stats.json`` file (see ``agentcommander.model_stats``) so the
    user has a transparent, hand-readable record of self-measured rates
    independent of the SQLite store.

    Skips the write entirely (returns the existing average instead) when
    the inputs would produce a meaningless rate: zero duration, no model
    name, AND neither tokens nor chars supplied.
    """
    if not model:
        return None
    if not duration_ms or duration_ms <= 0:
        return get_throughput(model)

    effective_tokens = completion_tokens if (completion_tokens and completion_tokens > 0) else 0
    if effective_tokens <= 0 and chars_completed and chars_completed > 0:
        # Lazy import — repos.py is imported very early; keep this cycle-safe.
        from agentcommander.model_stats import estimate_tokens_from_chars
        effective_tokens = estimate_tokens_from_chars(chars_completed)
    if effective_tokens <= 0:
        return get_throughput(model)

    # Mirror to the side-by-side JSON file. Best-effort.
    try:
        from agentcommander.model_stats import record_observation
        record_observation(
            model,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            chars_completed=chars_completed,
        )
    except Exception:  # noqa: BLE001
        pass

    seconds = duration_ms / 1000.0
    rate = float(effective_tokens) / seconds
    db = get_db()
    try:
        row = db.execute(
            "SELECT tokens_per_second FROM model_throughput WHERE model = ?",
            (model,),
        ).fetchone()
        if row is None:
            new_avg = (DEFAULT_TOKENS_PER_SECOND + rate) / 2.0
            db.execute(
                "INSERT INTO model_throughput (model, tokens_per_second, samples, updated_at) "
                "VALUES (?, ?, 1, ?)",
                (model, new_avg, _now_ms()),
            )
        else:
            old_avg = float(row["tokens_per_second"])
            new_avg = (old_avg + rate) / 2.0
            db.execute(
                "UPDATE model_throughput "
                "SET tokens_per_second = ?, samples = samples + 1, updated_at = ? "
                "WHERE model = ?",
                (new_avg, _now_ms(), model),
            )
        return new_avg
    except sqlite3.DatabaseError:
        return None


def list_throughput() -> list[dict[str, Any]]:
    """Return all known throughput rows (model, tokens_per_second, samples).

    Used by ``/status`` and other UI surfaces that show the current rate
    per model. Order: highest measured rate first so the fastest models
    surface in tables.
    """
    try:
        rows = get_db().execute(
            "SELECT model, tokens_per_second, samples, updated_at "
            "FROM model_throughput ORDER BY tokens_per_second DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"model": r["model"], "tokens_per_second": float(r["tokens_per_second"]),
         "samples": int(r["samples"]), "updated_at": r["updated_at"]}
        for r in rows
    ]
