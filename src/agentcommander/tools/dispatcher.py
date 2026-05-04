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


# ─── Minimal JSON Schema validator (subset our tools use) ──────────────────


_TYPE_PY: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "string": str,
    "integer": int,        # bool is also int — handled below
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "null": type(None),
}


def _validate_payload(payload: Any, schema: dict[str, Any] | None,
                       path: str = "") -> str | None:
    """Validate ``payload`` against ``schema``. Returns None if valid,
    or a short error string ("path: reason") if not.

    Supports:
      - ``type``: object / string / integer / number / boolean / array / null
      - ``required``: list of required keys (for type=object)
      - ``properties``: dict of name → subschema
      - ``additionalProperties``: subschema for unlisted keys
      - ``enum``: list of allowed values
      - ``minimum`` / ``maximum``: numeric bounds (inclusive)

    Anything outside this subset is silently treated as "no constraint."
    """
    if not schema:
        return None

    expected_type = schema.get("type")
    if expected_type:
        py_type = _TYPE_PY.get(expected_type)
        if py_type is None:
            # Unknown type — skip validation (forward-compatible).
            pass
        else:
            # JSON Schema treats integers as a refinement of numbers, so
            # ``type: integer`` rejects floats AND bools (booleans are a
            # Python int subclass but semantically distinct).
            if expected_type == "integer":
                if not isinstance(payload, int) or isinstance(payload, bool):
                    return f"{path or '<root>'}: must be integer (got {type(payload).__name__})"
            elif expected_type == "boolean":
                if not isinstance(payload, bool):
                    return f"{path or '<root>'}: must be boolean (got {type(payload).__name__})"
            elif expected_type == "number":
                if isinstance(payload, bool) or not isinstance(payload, (int, float)):
                    return f"{path or '<root>'}: must be number (got {type(payload).__name__})"
            elif not isinstance(payload, py_type):
                return f"{path or '<root>'}: must be {expected_type} (got {type(payload).__name__})"

    enum = schema.get("enum")
    if enum is not None and payload not in enum:
        # Truncate the enum list in the error message — long enums are noisy.
        shown = ", ".join(repr(e) for e in enum[:8])
        if len(enum) > 8:
            shown += ", ..."
        return f"{path or '<root>'}: must be one of [{shown}]"

    minimum = schema.get("minimum")
    if minimum is not None and isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if payload < minimum:
            return f"{path or '<root>'}: must be >= {minimum} (got {payload})"
    maximum = schema.get("maximum")
    if maximum is not None and isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if payload > maximum:
            return f"{path or '<root>'}: must be <= {maximum} (got {payload})"

    if expected_type == "object" and isinstance(payload, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in payload:
                return f"{path + '.' + key if path else key}: required field missing"
        properties = schema.get("properties") or {}
        for key, val in payload.items():
            sub = properties.get(key)
            child_path = f"{path}.{key}" if path else key
            if sub is not None:
                err = _validate_payload(val, sub, child_path)
                if err:
                    return err
            else:
                ap = schema.get("additionalProperties")
                if isinstance(ap, dict):
                    err = _validate_payload(val, ap, child_path)
                    if err:
                        return err
                # else: extra fields are allowed (forward-compat)

    if expected_type == "array" and isinstance(payload, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(payload):
                err = _validate_payload(item, items, f"{path}[{i}]")
                if err:
                    return err

    return None


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
    # Side-effect imports. Each module's top-level ``register(...)`` call
    # adds itself to ``_REGISTRY`` on first import.
    from agentcommander.tools import (  # noqa: F401
        browser_tool,
        code_tool,
        env_tool,
        file_tool,
        git_tool,
        http_tool,
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

    # Validate against the descriptor's input_schema BEFORE invoking the
    # handler. Catches buggy model payloads (wrong field types, missing
    # required keys, out-of-range integers, enum violations) with a clear
    # ``path: reason`` message instead of letting the handler raise an
    # opaque AttributeError 5 frames in. Tools without a schema (or with
    # an empty {}) bypass this — opt-in safety, not enforcement.
    if descriptor.input_schema:
        # Tolerate non-dict payloads at the top level so we get the
        # consistent "must be object" error rather than an AttributeError
        # when the validator descends into ``properties``.
        if not isinstance(payload, dict):
            return ToolResult(
                ok=False,
                error=f"{name}: payload must be an object (got "
                       f"{type(payload).__name__})",
            )
        err = _validate_payload(payload, descriptor.input_schema)
        if err:
            return ToolResult(ok=False, error=f"{name}: {err}")

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
