"""SQLite connection + schema bootstrap.

Single shared connection for the process. Use `get_db()` everywhere; do not
construct your own `sqlite3.Connection`.

The schema is applied on first call to `init_db()`. All CREATE statements
are idempotent so it's safe to call repeatedly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

_db: sqlite3.Connection | None = None
_db_path: Path | None = None

# True when the active connection was opened via init_db_readonly() — the
# mirror codepath. Several shutdown / maintenance steps (wal_checkpoint,
# REINDEX) require write access and are skipped when this is set, so a
# mirror process never tries to write to the DB it's only meant to follow.
_is_readonly: bool = False

# Open file handle backing the per-DB single-instance lock. Held for the
# lifetime of the process; released on graceful shutdown via close_db.
_lock_handle = None  # type: ignore[var-annotated]
_lock_path: Path | None = None

# Result of the most recent init_db auto-repair attempt. None when
# integrity_check passed cleanly on startup. The TUI reads this and
# surfaces a banner line so the user knows REINDEX fired (or didn't).
_last_auto_repair: dict | None = None


def last_auto_repair() -> dict | None:
    """Return the most recent auto-repair status, or None if the DB was clean."""
    return _last_auto_repair


class DBAlreadyOpen(RuntimeError):
    """Raised when another AgentCommander process holds the DB lock.

    Refusing concurrent processes is the simplest defense against the
    "Tree X page Y: btreeInitPage error 11" corruption pattern, which
    arises when two writers race a WAL checkpoint and one of them gets
    killed mid-write.
    """


def _acquire_db_lock(db_target: Path) -> None:
    """Take an exclusive file lock on ``<db_target>.lock``.

    Cross-platform: ``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX.
    Auto-releases when the process exits (the handle goes away). Stale lock
    files left by crashed processes are reusable — a fresh `flock`/`locking`
    call against an unowned file succeeds.
    """
    global _lock_handle, _lock_path
    if _lock_handle is not None:
        return  # already locked in this process
    lock_path = db_target.with_suffix(db_target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                raise DBAlreadyOpen(
                    f"another AgentCommander process is using {db_target}. "
                    "Close it before starting a new one. "
                    f"(lock file: {lock_path})"
                ) from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise DBAlreadyOpen(
                    f"another AgentCommander process is using {db_target}. "
                    "Close it before starting a new one. "
                    f"(lock file: {lock_path})"
                ) from exc
    except DBAlreadyOpen:
        raise
    except Exception:  # noqa: BLE001
        handle.close()
        raise
    _lock_handle = handle
    _lock_path = lock_path


def _release_db_lock() -> None:
    """Best-effort release of the single-instance lock."""
    global _lock_handle, _lock_path
    if _lock_handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            try:
                _lock_handle.seek(0)
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            _lock_handle.close()
        except OSError:
            pass
    finally:
        _lock_handle = None
        _lock_path = None


def _default_db_dir() -> Path:
    """OS-appropriate user-data directory.

    Used for **shared / cross-project** state (catalog cache, etc.) — NOT
    the conversation DB. The DB lives project-locally now (see
    ``_project_db_dir``) so different working directories keep separate
    history, role assignments, and provider configs.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AgentCommander"
    if "darwin" in os.sys.platform:  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "AgentCommander"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agentcommander"


def _project_db_dir() -> Path:
    """Project-local data directory: ``<cwd>/.agentcommander/``.

    Each working directory gets its own SQLite DB so a project's
    conversations / role-assignments / providers don't leak into
    unrelated projects. Add ``.agentcommander/`` to .gitignore at the
    project root to keep credentials out of source control.
    """
    return Path.cwd() / ".agentcommander"


def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized — call init_db() during startup")
    return _db


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or reuse) the connection, apply schema, return the connection."""
    global _db, _db_path

    if _db is not None:
        return _db

    target = Path(db_path) if db_path else _project_db_dir() / "db.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Single-instance lock: prevents two ac.bat processes from opening the
    # same project DB at once. The original corruption ("Tree 21 page 40
    # btreeInitPage error 11") came from concurrent test sessions plus a
    # timeout kill during WAL checkpoint — exactly what this guards against.
    # The lock auto-releases when the process exits (handle goes away).
    _acquire_db_lock(target)

    conn = sqlite3.connect(
        str(target),
        check_same_thread=False,
        isolation_level=None,
        # Block waiting up to 5s for a busy DB instead of failing immediately —
        # important when the engine worker thread and the TUI main thread
        # both write (e.g. user message + scratchpad entry on the same turn).
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # synchronous=FULL forces fsync after every commit. This is what stops
    # WAL/checkpoint state from getting torn when the process is killed
    # mid-write. Slightly slower than NORMAL on slow disks; on SSDs the
    # difference is unmeasurable for our write patterns.
    conn.execute("PRAGMA synchronous = FULL")
    # Extra runtime corruption detection — cheap insurance.
    try:
        conn.execute("PRAGMA cell_size_check = ON")
    except sqlite3.DatabaseError:
        pass
    # WAL autocheckpoint at 1000 pages (default) is fine. Force a checkpoint
    # on first open so any leftover WAL from a prior crashed run gets folded
    # back into the main DB before we touch it.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass

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
    #
    # DatabaseError catches "database disk image is malformed" — if the DB
    # is corrupted, we'd rather start the TUI in a degraded state (so the
    # user can run `/db check` and recover) than crash on import.
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
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        # Don't block startup. /db check will surface the underlying
        # integrity issue with actionable detail.
        pass

    # Auto integrity check + REINDEX repair attempt. SQLite's quick_check
    # is fast (much faster than integrity_check) and catches the common
    # 'malformed' cases. When it fails we run REINDEX — that rebuilds
    # every index from the underlying tables, which fixes the common
    # "index out of sync with table" corruption without losing any rows.
    # The result is stashed in `_last_auto_repair` so the TUI banner can
    # tell the user what happened — they don't have to dig through the
    # audit log to know REINDEX fired.
    global _last_auto_repair
    _last_auto_repair = None
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        ok = bool(row) and (row[0] == "ok")
        if not ok:
            before = row[0] if row else "?"
            try:
                conn.execute("REINDEX")
                row2 = conn.execute("PRAGMA quick_check").fetchone()
                ok2 = bool(row2) and (row2[0] == "ok")
                _last_auto_repair = {
                    "before": before,
                    "after": row2[0] if row2 else "?",
                    "fixed": ok2,
                }
                conn.execute(
                    "INSERT INTO audit_log (event_type, details, created_at) "
                    "VALUES (?, ?, ?)",
                    ("db.auto_repair", json.dumps(_last_auto_repair),
                     int(time.time() * 1000)),
                )
            except sqlite3.DatabaseError as exc:
                _last_auto_repair = {
                    "before": before,
                    "after": f"REINDEX failed: {exc}",
                    "fixed": False,
                }
    except sqlite3.DatabaseError as exc:
        # Even quick_check failed — file is badly damaged.
        _last_auto_repair = {
            "before": f"quick_check failed: {exc}",
            "after": "(skipped)",
            "fixed": False,
        }

    _db = conn
    _db_path = target
    return conn


def init_db_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open the project DB in **read-only mirror mode**.

    Used by ``ac --mirror`` — a passive follower that watches a primary
    AgentCommander process. Differences from ``init_db``:

      - **No lock acquired.** The single-instance lock exists to prevent
        two writers from racing the WAL. The mirror never writes, so it
        deliberately skips the lock — that's why it can coexist with a
        running primary, or start before primary exists.
      - **SQLite-enforced read-only** via the ``file:<path>?mode=ro`` URI.
        The connection physically refuses INSERT/UPDATE/DELETE — belt and
        braces with the lock skip.
      - **No schema execute, no REINDEX, no PRAGMA writes.** The primary
        owns DB shape; mirror just reads what's there.
      - **No checkpoint on close.** ``wal_checkpoint`` requires write
        access, which the mirror doesn't have. The ``_is_readonly`` flag
        is set so ``close_db`` skips that step.

    Caller must ensure the file exists. The mirror's startup polls for
    the file's appearance before calling this.
    """
    global _db, _db_path, _is_readonly

    if _db is not None:
        return _db

    target = Path(db_path)
    if not target.exists():
        raise FileNotFoundError(f"DB file not found: {target}")

    uri = f"file:{target.as_posix()}?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,
        isolation_level=None,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    # foreign_keys is per-connection, not per-DB — turning it on for our
    # SELECTs is harmless. We do NOT set journal_mode (would require write).
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.DatabaseError:
        pass

    _db = conn
    _db_path = target
    _is_readonly = True
    return conn


def is_readonly() -> bool:
    """True when the active connection was opened via ``init_db_readonly``."""
    return _is_readonly


def close_db() -> None:
    """Close the connection cleanly: WAL checkpoint, close handle, release
    the file lock. Idempotent — safe to call multiple times.

    Critical for corruption-avoidance: a process killed without checkpointing
    can leave WAL/main-DB out of sync. close_db forces a TRUNCATE checkpoint
    so the WAL is fully merged before the handle goes away.

    Mirror mode: ``wal_checkpoint`` requires write access, so when the
    connection was opened read-only we skip it. There's nothing to flush —
    the mirror never wrote anything.
    """
    global _db, _db_path, _is_readonly
    if _db is not None:
        try:
            if not _is_readonly:
                try:
                    _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
            _db.close()
        finally:
            _db = None
            _db_path = None
            _is_readonly = False
    _release_db_lock()


def _install_shutdown_hooks() -> None:
    """Register handlers that close the DB on graceful AND signal-induced
    exits. Belt-and-braces: atexit fires on normal Python shutdown; the
    signal handlers catch the cases atexit might miss (Ctrl-C twice,
    Windows ``timeout`` kill, parent shell teardown).
    """
    import atexit
    atexit.register(close_db)
    try:
        import signal
        # SIGINT and SIGTERM are the common termination signals. We
        # re-raise after closing so Python's normal shutdown path runs
        # (otherwise atexit handlers wouldn't fire on the same signal).
        def _on_signal(signum, _frame):  # type: ignore[no-untyped-def]
            try:
                close_db()
            finally:
                # Restore default handler and re-raise so the process exits
                # with the conventional status (128 + signum on POSIX).
                try:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)
                except Exception:  # noqa: BLE001
                    pass
        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _on_signal)
                except (ValueError, OSError):
                    # Some hosts (threaded callers, IDEs) don't allow signal
                    # registration; fail open.
                    pass
    except Exception:  # noqa: BLE001
        pass


# Register shutdown hooks once when the module is imported. Re-registering
# is harmless (atexit dedupes by callable id), but importing this module
# multiple times would only register once thanks to Python module caching.
_install_shutdown_hooks()


def db_path() -> Path | None:
    return _db_path
