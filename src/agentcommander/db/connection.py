"""SQLite connection + schema bootstrap.

Single shared connection for the process. Use `get_db()` everywhere; do not
construct your own `sqlite3.Connection`.

The schema is applied on first call to `init_db()`. All CREATE statements
are idempotent so it's safe to call repeatedly.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_db: sqlite3.Connection | None = None
_db_path: Path | None = None


def _default_db_dir() -> Path:
    """OS-appropriate user-data directory."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AgentCommander"
    if "darwin" in os.sys.platform:  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "AgentCommander"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agentcommander"


def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized — call init_db() during startup")
    return _db


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or reuse) the connection, apply schema, return the connection."""
    global _db, _db_path

    if _db is not None:
        return _db

    target = Path(db_path) if db_path else _default_db_dir() / "agentcommander.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    schema_path = Path(__file__).with_name("schema.sql")
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(schema_sql)

    # Idempotent column adds for DBs created before a column existed.
    # SQLite raises OperationalError when the column is already there — we
    # swallow it so this is safe to re-run on every startup.
    for ddl in (
        "ALTER TABLE role_assignments ADD COLUMN context_window_tokens INTEGER",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # Idempotent backfill: derive scannable conversation titles from the
    # first user message for conversations still on the legacy default
    # title ("Conversation" / "New conversation"). Runs once per DB until
    # every legacy row gets a real title, then becomes a no-op.
    try:
        conn.execute(
            "UPDATE conversations SET title = trim(substr("
            "  (SELECT m.content FROM messages m WHERE m.conversation_id = conversations.id"
            "   AND m.role = 'user' ORDER BY m.created_at ASC LIMIT 1),"
            "  1, 60)) "
            "WHERE title IN ('Conversation', 'New conversation', '') "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = conversations.id "
            "            AND m.role = 'user')"
        )
    except sqlite3.OperationalError:
        pass

    _db = conn
    _db_path = target
    return conn


def close_db() -> None:
    global _db, _db_path
    if _db is not None:
        try:
            _db.close()
        finally:
            _db = None
            _db_path = None


def db_path() -> Path | None:
    return _db_path
