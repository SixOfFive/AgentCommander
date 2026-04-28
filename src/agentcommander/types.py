"""Cross-cutting type definitions for AgentCommander.

Mirrors the shapes from EngineCommander/src/main/orchestration/engine-types.ts
plus the shared types for IPC. Pydantic models for the values that cross
provider/orchestrator boundaries; plain dataclasses everywhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ─── Roles (the 19 EC pipeline roles) ──────────────────────────────────────


class Role(str, Enum):
    ROUTER = "router"
    ORCHESTRATOR = "orchestrator"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    SUMMARIZER = "summarizer"
    VISION = "vision"
    AUDIO = "audio"
    IMAGE_GEN = "image_gen"
    ARCHITECT = "architect"
    CRITIC = "critic"
    TESTER = "tester"
    DEBUGGER = "debugger"
    RESEARCHER = "researcher"
    REFACTORER = "refactorer"
    TRANSLATOR = "translator"
    DATA_ANALYST = "data_analyst"
    PREFLIGHT = "preflight"
    POSTMORTEM = "postmortem"


ALL_ROLES: tuple[Role, ...] = tuple(Role)


# ─── Provider config ───────────────────────────────────────────────────────

ProviderType = Literal["ollama", "llamacpp", "openrouter", "anthropic", "google"]


class ProviderConfig(BaseModel):
    id: str
    type: ProviderType
    name: str
    endpoint: str | None = None
    api_key: str | None = None  # encrypted on disk via OS keyring later
    enabled: bool = True


# ─── Engine ────────────────────────────────────────────────────────────────


class OrchestratorDecision(BaseModel):
    """Mirrors EC's OrchestratorDecision shape. All fields except `action`
    are optional — different actions populate different fields.
    """

    action: str
    reasoning: str | None = None
    input: str | None = None
    url: str | None = None
    language: str | None = None
    path: str | None = None
    content: str | None = None
    pattern: str | None = None
    command: str | None = None
    message: str | None = None
    files: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    body: str | None = None
    host: str | None = None
    port: int | None = None
    prefer: str | None = None
    steps: list[dict[str, Any]] | None = None

    model_config = {"extra": "ignore"}


@dataclass
class ScratchpadEntry:
    step: int
    role: str  # Role.value | "tool" | "parallel" | "system"
    action: str
    input: str
    output: str
    timestamp: float
    duration_ms: int | None = None
    content: str | None = None


@dataclass
class LoopState:
    """Per-run mutable state threaded through guard families."""

    iteration: int = 0
    scratchpad: list[ScratchpadEntry] = field(default_factory=list)
    consecutive_nudges: int = 0
    plan_call_count: int = 0
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    file_write_registry: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, int] = field(default_factory=dict)


# ─── Conversations / messages ──────────────────────────────────────────────


@dataclass
class Conversation:
    id: str
    title: str
    created_at: int  # epoch ms
    updated_at: int


@dataclass
class Message:
    id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: int


# ─── Pipeline events emitted to the TUI ────────────────────────────────────


class PipelineEvent(BaseModel):
    """Streamed from engine → CLI for live rendering."""

    type: Literal["iteration", "role", "role_delta", "tool", "guard", "done", "error"]
    iteration: int | None = None
    action: str | None = None
    role: str | None = None
    output: str | None = None
    delta: str | None = None
    tool: str | None = None
    ok: bool | None = None
    error: str | None = None
    final: str | None = None
    family: str | None = None
    reason: str | None = None
