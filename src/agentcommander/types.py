"""Cross-cutting type definitions.

Pure stdlib — dataclasses + Enum + typing. No pydantic.

Mirrors EngineCommander/src/main/orchestration/engine-types.ts where applicable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal


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


@dataclass
class ProviderConfig:
    """Per-provider config persisted to the user-data SQLite file.

    The DB lives in the OS user-data directory (XDG_DATA_HOME / %APPDATA% /
    Application Support) and is gitignored — endpoints and API keys never
    ship with the source tree.
    """

    id: str
    type: ProviderType
    name: str
    endpoint: str | None = None
    api_key: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProviderConfig":
        return cls(
            id=d["id"],
            type=d["type"],
            name=d["name"],
            endpoint=d.get("endpoint"),
            api_key=d.get("api_key"),
            enabled=bool(d.get("enabled", True)),
        )


# ─── Engine ────────────────────────────────────────────────────────────────


@dataclass
class OrchestratorDecision:
    """Mirrors EC's OrchestratorDecision shape. Most fields are optional —
    different actions populate different fields.

    Constructed from the orchestrator's JSON output via `from_dict`. Extra
    keys in the JSON are tolerated and dropped.
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OrchestratorDecision":
        if not isinstance(d, dict):
            raise TypeError("OrchestratorDecision.from_dict requires a dict")
        action = d.get("action")
        if not isinstance(action, str):
            raise ValueError("decision.action is required (string)")
        # Pick only known fields to avoid surprises.
        known = {
            "action", "reasoning", "input", "url", "language", "path", "content",
            "pattern", "command", "message", "files", "method", "headers", "body",
            "host", "port", "prefer", "steps",
        }
        kwargs: dict[str, Any] = {k: d.get(k) for k in known if k in d}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ScratchpadEntry:
    step: int
    role: str  # "router"... | "tool" | "parallel" | "system"
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
    created_at: int
    updated_at: int


@dataclass
class Message:
    id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: int


# ─── Pipeline events emitted to the TUI ────────────────────────────────────


@dataclass
class PipelineEvent:
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
