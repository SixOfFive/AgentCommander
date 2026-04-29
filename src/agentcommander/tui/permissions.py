"""Filesystem permission prompts.

When a tool wants to read or write a file, the engine consults this module.
For each (path, operation) pair we either:
  - Already have a persisted "always allow" / "always deny" → return immediately
  - Already have a session-scoped "yes once" / "no once" → return that
  - Otherwise → prompt the user with three options:
      [d] Deny  (and halt the pipeline + cancel any future planned tasks)
      [t] Yes this time   (allow only this call)
      [a] Always   (persist to fs_permissions and allow forever)

A 'd' (Deny) raises PermissionDenied, which the engine catches and uses to
break out of the run. The user_message is preserved; only the in-flight pipe
stops.

Pure stdlib. Reads from `input()`. If stdin is not a TTY we default to DENY
so non-interactive runs can never silently exfiltrate.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal

from agentcommander.db.connection import get_db
from agentcommander.tui.ansi import RESET, fg256, style, write, writeln


Operation = Literal["read", "write", "delete", "execute"]
Decision = Literal["allow", "deny"]


class PermissionDenied(RuntimeError):
    """Raised when the user denies a filesystem operation. The engine catches
    this and aborts the active pipeline run."""

    def __init__(self, path: str, operation: Operation) -> None:
        super().__init__(f'permission denied: {operation} {path}')
        self.path = path
        self.operation = operation


@dataclass
class _Cached:
    decision: Decision
    persisted: bool  # True if loaded from / written to fs_permissions table


# Session-only cache for "Yes this time" decisions (cleared when TUI exits).
_session_cache: dict[tuple[str, Operation], _Cached] = {}


def _load_persisted(path: str, op: Operation) -> _Cached | None:
    """Look up a persisted decision for ``(path, op)``.

    Resolution order:
      1. Exact match on ``path``  (any scope)
      2. Subtree match on any ancestor of ``path`` whose row has
         ``scope='subtree'`` — closest ancestor wins so a more-specific
         subtree allow overrides a broader deny.

    The subtree walk lets the user (or a setup script) say "allow write
    everywhere under /home/me/projects/foo" with one row instead of one
    per file.
    """
    db = get_db()
    # 1. Exact path match.
    row = db.execute(
        "SELECT decision FROM fs_permissions WHERE path = ? AND operation = ?",
        (path, op),
    ).fetchone()
    if row is not None and row["decision"] in ("allow", "deny"):
        return _Cached(decision=row["decision"], persisted=True)

    # 2. Subtree match — walk parents from nearest to root, return the
    # first one with a subtree-scoped row. We don't include `path` itself
    # again here (already matched above as exact).
    from pathlib import Path
    p = Path(path)
    if not p.is_absolute():
        p = p.resolve()
    for ancestor in p.parents:
        row = db.execute(
            "SELECT decision FROM fs_permissions "
            "WHERE path = ? AND operation = ? AND scope = 'subtree'",
            (str(ancestor), op),
        ).fetchone()
        if row is not None and row["decision"] in ("allow", "deny"):
            return _Cached(decision=row["decision"], persisted=True)
    return None


def _persist(path: str, op: Operation, decision: Decision,
             scope: str = "exact") -> None:
    import time
    if scope not in ("exact", "subtree"):
        scope = "exact"
    get_db().execute(
        "INSERT INTO fs_permissions (path, operation, decision, scope, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(path, operation) DO UPDATE SET "
        "  decision = excluded.decision, scope = excluded.scope, "
        "  created_at = excluded.created_at",
        (path, op, decision, scope, int(time.time() * 1000)),
    )


def grant_subtree(path: str, op: Operation, decision: Decision = "allow") -> None:
    """Public helper: persist a subtree-scoped allow/deny under ``path``.

    Used by setup scripts and the test harness to pre-authorize a sandbox
    directory without an interactive prompt. Path is normalized to absolute.
    """
    _persist(_abs(path), op, decision, scope="subtree")
    # Ensure cache for this exact path is consistent
    _session_cache[(_abs(path), op)] = _Cached(decision=decision, persisted=True)


def _abs(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _format_prompt(abs_path: str, op: Operation) -> str:
    rel = abs_path
    try:
        cwd = os.getcwd()
        if abs_path.startswith(cwd):
            rel = "." + abs_path[len(cwd):]
    except OSError:
        pass

    verb_color = {
        "read": fg256(75),       # blue — passive
        "write": fg256(214),     # orange — modifying
        "delete": fg256(204),    # red — destructive
        "execute": fg256(208),   # bright orange — running code
    }.get(op, "")
    verb = f"{verb_color}{op}{RESET}" if verb_color else op
    return (f"  permission requested: {verb}  {style('accent', rel)}\n"
            f"    [{style('warn', 'd')}] Deny      "
            f"[{style('accent', 't')}] Yes this time      "
            f"[{style('accent', 'a')}] Always (persist)")


def request_permission(path: str, op: Operation) -> bool:
    """Ask the user whether the tool may perform `op` on `path`.

    Returns True to allow, False is never returned — denial raises
    PermissionDenied so the engine can halt cleanly.

    Resolution order:
      1. Persisted decision (fs_permissions table) → use as-is
      2. Session-scoped decision → use as-is
      3. Interactive prompt (TTY only)
      4. Non-interactive fallback → deny
    """
    abs_path = _abs(path)
    key = (abs_path, op)

    cached = _session_cache.get(key)
    if cached is not None:
        if cached.decision == "deny":
            raise PermissionDenied(abs_path, op)
        return True

    persisted = _load_persisted(abs_path, op)
    if persisted is not None:
        _session_cache[key] = persisted
        if persisted.decision == "deny":
            raise PermissionDenied(abs_path, op)
        return True

    # Non-TTY fallback: deny silently. Never auto-allow on a piped run.
    if not sys.stdin.isatty():
        _session_cache[key] = _Cached(decision="deny", persisted=False)
        raise PermissionDenied(abs_path, op)

    # Interactive prompt.
    writeln()
    writeln(_format_prompt(abs_path, op))
    write(style("user_label", "  > "))
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        writeln()
        raise PermissionDenied(abs_path, op) from None

    if answer in ("d", "deny", "n", "no"):
        _session_cache[key] = _Cached(decision="deny", persisted=False)
        raise PermissionDenied(abs_path, op)
    if answer in ("a", "always", "yes from now on"):
        _persist(abs_path, op, "allow")
        _session_cache[key] = _Cached(decision="allow", persisted=True)
        return True
    if answer in ("t", "this time", "yes", "y", "once"):
        _session_cache[key] = _Cached(decision="allow", persisted=False)
        return True
    # Default on unrecognized input: treat as Deny for safety.
    _session_cache[key] = _Cached(decision="deny", persisted=False)
    raise PermissionDenied(abs_path, op)


def clear_session_cache() -> None:
    """Drop session-scoped 'yes this time' decisions. Persisted ones are kept."""
    _session_cache.clear()


def list_persisted() -> list[dict[str, str]]:
    rows = get_db().execute(
        "SELECT path, operation, decision FROM fs_permissions ORDER BY path"
    ).fetchall()
    return [{"path": r["path"], "operation": r["operation"], "decision": r["decision"]}
            for r in rows]


def revoke_persisted(path: str, op: Operation | None = None) -> int:
    abs_path = _abs(path)
    if op is None:
        cur = get_db().execute("DELETE FROM fs_permissions WHERE path = ?", (abs_path,))
    else:
        cur = get_db().execute(
            "DELETE FROM fs_permissions WHERE path = ? AND operation = ?",
            (abs_path, op),
        )
    # Also drop session cache entries for this path
    for key in list(_session_cache.keys()):
        if key[0] == abs_path and (op is None or key[1] == op):
            _session_cache.pop(key, None)
    return cur.rowcount or 0
