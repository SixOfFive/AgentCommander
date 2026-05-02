"""Filesystem sandbox.

Ported from EngineCommander/src/main/utils/sandbox.ts. The multi-tenant
EC_DATA_DIR/workspaces guard is removed — AgentCommander runs single-user
with one user-chosen working directory at a time.

RULES:
  1. If no working directory is set, ALL filesystem access is denied.
  2. If a working directory is set, access is restricted to that directory
     and its children.
  3. No path traversal (../) can escape the sandbox.
  4. Symlinks that point outside the sandbox are rejected too (realpath check).
  5. The working directory itself must exist and be a real directory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Operation = Literal["read", "write", "delete", "list"]


class FilesystemSecurityError(Exception):
    """Raised when a path operation would escape the sandbox."""


def is_valid_directory(dir_path: str) -> bool:
    try:
        return os.path.isdir(dir_path)
    except OSError:
        return False


def is_path_within(candidate_path: str, working_directory: str) -> bool:
    """Return True if candidate_path is at or under working_directory.

    Two checks:
      1. Logical: normalized + resolved path must start with the base.
      2. Symlink: if the path exists, dereference and re-check.
    """
    if "\0" in candidate_path or "\0" in working_directory:
        return False

    try:
        logical_candidate = os.path.normpath(os.path.abspath(os.path.join(working_directory, candidate_path)))
        logical_base = os.path.normpath(os.path.abspath(working_directory))
    except (ValueError, OSError):
        return False

    sep = os.sep
    alt_sep = "/" if sep != "/" else "\\"

    def _under(child: str, base: str) -> bool:
        return (
            child == base
            or child.startswith(base + sep)
            or child.startswith(base + alt_sep)
        )

    if not _under(logical_candidate, logical_base):
        return False

    if os.path.exists(logical_candidate):
        try:
            real_candidate = os.path.realpath(logical_candidate)
            real_base = os.path.realpath(logical_base)
            return _under(real_candidate, real_base)
        except OSError:
            # If realpath fails, fall through to logical pass.
            pass

    return True


def safe_path(candidate_path: str, working_directory: str) -> str | None:
    """Return the resolved absolute path if it stays inside the sandbox; else None.

    Rejects:
      - empty paths (no file to operate on)
      - any control character (0x00-0x1F + 0x7F) — NUL, newline, CR,
        TAB, etc. They're technically legal filenames on POSIX but almost
        always indicate an injection or a broken paste in model output;
        on Windows the OS rejects most of them anyway, but we want a
        consistent boundary across platforms.
      - paths that resolve outside the working directory
    """
    if not candidate_path:
        return None
    for ch in candidate_path:
        if ord(ch) < 32 or ord(ch) == 127:
            return None
    try:
        resolved = os.path.normpath(os.path.abspath(os.path.join(working_directory, candidate_path)))
    except (ValueError, OSError):
        return None
    if not is_path_within(resolved, working_directory):
        return None
    return resolved


def require_working_directory(working_directory: str | None) -> str:
    """Raise FilesystemSecurityError if no valid working dir is set; else return it."""
    if not working_directory:
        raise FilesystemSecurityError(
            "No working directory set. Filesystem access is denied. Pick a working directory first."
        )
    if not is_valid_directory(working_directory):
        raise FilesystemSecurityError(
            f"Working directory does not exist or is not a directory: {working_directory}"
        )
    return working_directory


def validate_file_access(
    file_path: str, working_directory: str | None, operation: Operation
) -> str:
    """Validate that `operation` on `file_path` is within the sandbox.

    Returns the absolute resolved path; raises FilesystemSecurityError on violation.
    """
    base = require_working_directory(working_directory)
    resolved = safe_path(file_path, base)
    if resolved is None:
        raise FilesystemSecurityError(
            f'BLOCKED: {operation} access to "{file_path}" — path is outside the working directory "{base}"'
        )
    return resolved


_DANGEROUS_RX = [
    r"rm\s+-rf?\s+/",
    r"del\s+/[sfq]",
    r"rmdir\s+/s",
    r"format\s+[a-z]:",
    r"mkfs\.",
    r"dd\s+if=",
    r">\s*/dev/",
    r"chmod\s+777",
    r"\.\./\.\./",
]


def scan_for_filesystem_risks(text: str, working_directory: str | None) -> list[str]:
    """Scan text output from models for suspicious filesystem patterns.

    Returns a list of warning strings; does not block.
    """
    import re
    warnings: list[str] = []

    for path in re.findall(r"[A-Za-z]:\\[^\s\"'`]+", text):
        if working_directory and not is_path_within(path, working_directory):
            warnings.append(f"Suspicious path outside working directory: {path}")
        elif not working_directory:
            warnings.append(f"Path reference with no working directory set: {path}")

    for path in re.findall(r"/(?:etc|usr|var|tmp|home|root|proc|sys|dev|boot|opt)\b[^\s\"'`]*", text):
        if working_directory and not is_path_within(path, working_directory):
            warnings.append(f"Suspicious path outside working directory: {path}")
        elif not working_directory:
            warnings.append(f"Path reference with no working directory set: {path}")

    for rx in _DANGEROUS_RX:
        if re.search(rx, text, re.IGNORECASE):
            warnings.append(f"Dangerous command pattern detected: {rx}")

    return warnings


def relative_to_workdir(absolute_path: str, working_directory: str) -> str:
    """Helper for human-readable display — returns path relative to workdir if inside, else absolute."""
    try:
        return str(Path(absolute_path).relative_to(working_directory))
    except ValueError:
        return absolute_path
