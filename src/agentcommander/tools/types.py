"""Tool layer types.

Tools never raise to the engine — failures are converted to
`ToolResult(ok=False, error=...)` so the orchestrator can decide whether to
retry, route around, or surface to the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolContext:
    """Per-invocation context, threaded through every tool call."""

    working_directory: str | None
    conversation_id: str | None
    audit: Callable[[str, Any], None]


@dataclass
class ToolResult:
    ok: bool
    output: str | None = None
    error: str | None = None
    data: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


# Handler signature: callable(payload, ctx) -> ToolResult.
# Synchronous — there is no parallel execution in AgentCommander.
ToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass
class ToolDescriptor:
    """One tool's metadata + handler. Registered with the dispatcher."""

    name: str
    description: str
    privileged: bool                  # EC's admin-gated set (24 verbs)
    input_schema: dict[str, Any]      # JSON Schema (advisory)
    handler: ToolHandler
