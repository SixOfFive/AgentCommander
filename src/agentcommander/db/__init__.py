"""Database layer — sqlite3 (stdlib).

Single-user, single-machine. No tenant_id / user_id columns.

  - schema.sql: CREATE TABLE IF NOT EXISTS for the whole schema (idempotent)
  - connection.py: open / close / migrate
  - repos/: one module per table family (conversations, providers, runs, ...)
"""

from agentcommander.db.connection import close_db, get_db, init_db

__all__ = ["close_db", "get_db", "init_db"]
