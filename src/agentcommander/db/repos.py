"""Thin SQL wrappers — one function per query.

Imports are kept narrow so this module stays a clean boundary:
modules that need persistence call these; nobody else writes raw SQL.
"""
from __future__ import annotations

import json
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
    get_db().execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (msg.id, msg.conversation_id, msg.role, msg.content, msg.created_at),
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
        "SELECT role, provider_id, model, is_override FROM role_assignments WHERE role = ?",
        (role_value,),
    ).fetchone()
    if row is None:
        return None
    return {
        "role": row["role"],
        "provider_id": row["provider_id"],
        "model": row["model"],
        "is_override": bool(row["is_override"]),
    }


def set_role_assignment(role: Role | str, provider_id: str, model: str,
                        is_override: bool = True) -> None:
    role_value = role.value if isinstance(role, Role) else role
    get_db().execute(
        "INSERT INTO role_assignments (role, provider_id, model, is_override, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(role) DO UPDATE SET "
        "  provider_id = excluded.provider_id, model = excluded.model, "
        "  is_override = excluded.is_override, updated_at = excluded.updated_at",
        (role_value, provider_id, model, 1 if is_override else 0, _now_ms()),
    )


def list_role_assignments() -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT role, provider_id, model, is_override FROM role_assignments"
    ).fetchall()
    return [
        {"role": r["role"], "provider_id": r["provider_id"], "model": r["model"],
         "is_override": bool(r["is_override"])}
        for r in rows
    ]


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
