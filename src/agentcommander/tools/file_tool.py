"""File I/O tools — sandbox-gated.

read_file, write_file, list_dir, delete_file. Every path flows through
`validate_file_access()` so the agent can only touch the working directory.

This module self-registers on import via `register(...)` calls at module
bottom (the `tools.bootstrap_builtins()` call wires that in).
"""
from __future__ import annotations

import os
from typing import Any

from agentcommander.safety.sandbox import (
    relative_to_workdir,
    require_working_directory,
    validate_file_access,
)
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult


def _ask_permission(path: str, op: str) -> None:
    """Consult the user for read/write/delete permission. Raises PermissionDenied on deny.

    Lazily imported so unit tests that don't init the TUI can still load this module.
    """
    from agentcommander.tui.permissions import request_permission
    request_permission(path, op)  # type: ignore[arg-type]

MAX_READ_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_WRITE_BYTES = 10 * 1024 * 1024  # 10 MB


def _read_file(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        return ToolResult(ok=False, error="path is required")
    try:
        safe = validate_file_access(path, ctx.working_directory, "read")
        _ask_permission(safe, "read")
        size = os.path.getsize(safe)
        if size > MAX_READ_BYTES:
            return ToolResult(ok=False,
                              error=f"file too large ({size} bytes; max {MAX_READ_BYTES})")
        with open(safe, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return ToolResult(ok=True, output=content, data={"bytes": size})
    except Exception as exc:  # noqa: BLE001 — tool boundary
        return ToolResult(ok=False, error=str(exc))


def _write_file(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = payload.get("path")
    content = payload.get("content", "")
    if not isinstance(path, str) or not path:
        return ToolResult(ok=False, error="path is required")
    if not isinstance(content, str):
        return ToolResult(ok=False, error="content must be a string")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return ToolResult(ok=False,
                          error=f"content too large; max {MAX_WRITE_BYTES} bytes")
    try:
        safe = validate_file_access(path, ctx.working_directory, "write")
        _ask_permission(safe, "write")
        os.makedirs(os.path.dirname(safe) or ".", exist_ok=True)
        with open(safe, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        rel = relative_to_workdir(safe, ctx.working_directory or "")
        ctx.audit("file.write", {"path": rel, "bytes": len(content)})
        return ToolResult(ok=True, output=f"Successfully wrote {len(content)} bytes to {rel}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))


def _list_dir(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = payload.get("path")
    try:
        base = require_working_directory(ctx.working_directory)
        target = validate_file_access(path, base, "list") if path else base
        entries: list[dict[str, str]] = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            if os.path.islink(full):
                kind = "link"
            elif os.path.isdir(full):
                kind = "dir"
            else:
                kind = "file"
            entries.append({"name": name, "type": kind})
        text = "\n".join(f"{'[d]' if e['type'] == 'dir' else '[f]'} {e['name']}" for e in entries)
        return ToolResult(ok=True, output=text, data={"entries": entries})
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))


def _delete_file(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        return ToolResult(ok=False, error="path is required")
    try:
        safe = validate_file_access(path, ctx.working_directory, "delete")
        _ask_permission(safe, "delete")
        if not os.path.exists(safe):
            return ToolResult(ok=False, error=f"path does not exist: {path}")
        os.remove(safe)
        rel = relative_to_workdir(safe, ctx.working_directory or "")
        ctx.audit("file.delete", {"path": rel})
        return ToolResult(ok=True, output=f"deleted {rel}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))


# ─── Registration ──────────────────────────────────────────────────────────

register(ToolDescriptor(
    name="read_file",
    description="Read a UTF-8 text file from the working directory.",
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    handler=_read_file,
))

register(ToolDescriptor(
    name="write_file",
    description="Create or overwrite a UTF-8 text file in the working directory.",
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    handler=_write_file,
))

register(ToolDescriptor(
    name="list_dir",
    description="List entries in a directory under the working directory.",
    privileged=False,
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    handler=_list_dir,
))

register(ToolDescriptor(
    name="delete_file",
    description="Delete a file in the working directory.",
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    handler=_delete_file,
))
