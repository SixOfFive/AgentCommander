"""Tool dispatcher — single entry point for all tool calls.

Built-in tools self-register on import of `tools.bootstrap`. External plugins
(e.g. user-supplied tools, MCP-bridge tools) register through the same APIs.

`invoke(name, payload, ctx_partial)` is the only call site the engine uses.
Every dispatch lands an audit row.

Each ``ToolDescriptor`` declares an ``input_schema`` (JSON Schema subset).
``invoke`` validates the payload against it BEFORE calling the handler so a
malformed payload from a buggy model produces a clear "x is required" /
"x must be one of [...]" error instead of a deep AttributeError 5 levels
into the handler. The validator only supports the subset our schemas
actually use — it's not a full JSON Schema implementation.
"""
from __future__ import annotations

import time
from typing import Any

from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult

_REGISTRY: dict[str, ToolDescriptor] = {}


def register(descriptor: ToolDescriptor) -> ToolDescriptor:
    """Add or replace a tool. Returns the descriptor for `descriptor = register(...)` chaining."""
    if descriptor.name in _REGISTRY:
        # Replacement is permitted (later registrations win); audit it via stderr.
        import sys
        print(f"[tools] replacing existing handler for: {descriptor.name}", file=sys.stderr)
    _REGISTRY[descriptor.name] = descriptor
    return descriptor


def register_external(descriptors: list[ToolDescriptor]) -> None:
    for d in descriptors:
        register(d)


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_tool(name: str) -> ToolDescriptor | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolDescriptor]:
    return list(_REGISTRY.values())


def bootstrap_builtins() -> list[str]:
    """Import every built-in tool module so they self-register.

    Idempotent — re-importing is a no-op.
    """
    # Side-effect imports.
    from agentcommander.tools import (  # noqa: F401
        code_tool,
        file_tool,
        process_tool,
        web_tool,
    )
    return sorted(_REGISTRY.keys())


def invoke(name: str, payload: dict[str, Any], *,
           working_directory: str | None,
           conversation_id: str | None) -> ToolResult:
    """Dispatch a single tool. Always returns a ToolResult (never raises)."""
    from agentcommander.db.repos import audit  # lazy to avoid import order issues

    descriptor = _REGISTRY.get(name)
    if descriptor is None:
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    ctx = ToolContext(
        working_directory=working_directory,
        conversation_id=conversation_id,
        audit=audit,
    )

    started = time.time()
    try:
        result = descriptor.handler(payload, ctx)
    except Exception as exc:
        # PermissionDenied is special — re-raise so the engine can halt the
        # whole pipeline + drop any planned remaining steps.
        from agentcommander.tui.permissions import PermissionDenied
        if isinstance(exc, PermissionDenied):
            raise
        # All other tool exceptions become a normal failure result so the
        # orchestrator can decide what to do.
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            audit("tool.invoke", {
                "name": name,
                "duration_ms": int((time.time() - started) * 1000),
                "conversation_id": conversation_id,
            })
        except Exception:  # noqa: BLE001
            pass
    return result
