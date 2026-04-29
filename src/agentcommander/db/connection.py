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

    # Defensive scrub: if a previous version of AgentCommander persisted
    # endpoints or API keys in the providers table, drop those columns now.
    # The project does NOT store credentials or addresses on disk.
    _scrub_legacy_credentials(conn)

    _db = conn
    _db_path = target
    return conn


def _scrub_legacy_credentials(conn: sqlite3.Connection) -> None:
    """Remove endpoint/api_key columns from providers table if they exist.

    Older installs may have these columns from earlier schema versions.
    This is a one-time, idempotent migration — safe to run on every startup.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()}
    except sqlite3.OperationalError:
        return

    for col in ("endpoint", "api_key"):
        if col in cols:
            # First nullify any stored values (in case DROP COLUMN isn't supported).
            try:
                conn.execute(f"UPDATE providers SET {col} = NULL")
            except sqlite3.OperationalError:
                pass
            # SQLite 3.35+ supports DROP COLUMN. Older builds will silently fail —
            # the column still exists but contains NULL, which is acceptable.
            try:
                conn.execute(f"ALTER TABLE providers DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass


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
