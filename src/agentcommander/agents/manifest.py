"""Agent manifest — structured metadata for all 19 EC roles.

Single source of truth. The engine reads from this for default temps, max
tokens, output contract, and which guard families apply. Adding a new role =
appending an AgentDef + dropping a {ROLE}.md prompt file in resources/prompts/.

Sourced from `EngineCommander/agents_export.md` and `engine.ts` defaults.
The 19 roles are SERIAL — never run in parallel. The `parallel` action that
EC supported is deliberately not ported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from agentcommander.types import Role


class AgentCategory(str, Enum):
    """Functional grouping of the 19 agents."""

    ROUTER = "router"            # classify intent (runs once at start)
    CONTROLLER = "controller"    # orchestrator (runs every iteration)
    PRODUCER = "producer"        # content producers (planner, coder, reviewer, ...)
    MEDIA = "media"              # vision, audio, image_gen
    META = "meta"                # preflight, postmortem (JSON verdict gates)


class OutputContract(str, Enum):
    """What the engine expects back from a role call."""

    FREEFORM = "freeform"      # arbitrary text — guards parse it loosely
    JSON_STRICT = "json_strict"  # must parse as JSON; guards salvage if not


@dataclass(frozen=True)
class AgentDef:
    """Static metadata for one of the 19 agent roles."""

    role: Role
    category: AgentCategory
    purpose: str
    invocation: str
    output_contract: OutputContract
    default_temperature: float
    default_max_tokens: int
    optional: bool                            # can the pipeline run without this role?
    guards: tuple[str, ...] = ()              # guard families that inspect this role's output
    recommended_model_size: str = ""          # human-readable hint

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "category": self.category.value,
            "purpose": self.purpose,
            "invocation": self.invocation,
            "output_contract": self.output_contract.value,
            "default_temperature": self.default_temperature,
            "default_max_tokens": self.default_max_tokens,
            "optional": self.optional,
            "guards": list(self.guards),
            "recommended_model_size": self.recommended_model_size,
        }


# ─── The 19-role manifest ──────────────────────────────────────────────────

AGENTS: tuple[AgentDef, ...] = (
    AgentDef(
        role=Role.ROUTER,
        category=AgentCategory.ROUTER,
        purpose="Classify the user's message into one of 5 intent categories so the orchestrator can pick the right strategy.",
        invocation="Once per pipeline run, at the very start. Output drives CATEGORY_MAX_ITERATIONS (project=100, code=60, research=30, question=10, chat=5).",
        output_contract=OutputContract.JSON_STRICT,
        default_temperature=0.1,
        default_max_tokens=256,
        optional=False,
        recommended_model_size="3B-8B instruct (larger is overkill)",
    ),
    AgentDef(
        role=Role.ORCHESTRATOR,
        category=AgentCategory.CONTROLLER,
        purpose="The decision-maker. Each iteration emits exactly one JSON action: a role delegation, a tool action, or 'done'.",
        invocation="Every iteration of the main loop. Sees the compacted scratchpad + user message + category.",
        output_contract=OutputContract.JSON_STRICT,
        default_temperature=0.3,
        default_max_tokens=16000,
        optional=False,
        guards=("decision", "flow"),
        recommended_model_size="≥7B with strong JSON discipline",
    ),
    AgentDef(
        role=Role.PLANNER,
        category=AgentCategory.PRODUCER,
        purpose="Decompose a complex task into ordered steps before execution.",
        invocation="Called via the 'plan' action. Output guides subsequent code/tool actions.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.4,
        default_max_tokens=8000,
        optional=True,
        recommended_model_size="≥7B for non-trivial decomposition",
    ),
    AgentDef(
        role=Role.CODER,
        category=AgentCategory.PRODUCER,
        purpose="Write production-quality code from a description or plan.",
        invocation="Called via the 'code' action. Output is captured and typically followed by write_file + execute.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.2,
        default_max_tokens=16000,
        optional=True,
        guards=("execute", "write"),
        recommended_model_size="code-specialized model preferred",
    ),
    AgentDef(
        role=Role.REVIEWER,
        category=AgentCategory.PRODUCER,
        purpose="Critique recently-written code for correctness, style, and edge cases.",
        invocation="Called via 'review'. Auto-triggered by done-guards when 2+ code files written without review.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.SUMMARIZER,
        category=AgentCategory.PRODUCER,
        purpose="Compact and present results to the user.",
        invocation="Called via 'summarize'. Auto-triggered by done-guards when output is raw code or verbose fluff.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=4000,
        optional=True,
    ),
    AgentDef(
        role=Role.VISION,
        category=AgentCategory.MEDIA,
        purpose="Describe or analyze images.",
        invocation="Called via 'vision' when an image is in the conversation context.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=4000,
        optional=True,
        recommended_model_size="multimodal model required",
    ),
    AgentDef(
        role=Role.AUDIO,
        category=AgentCategory.MEDIA,
        purpose="Transcribe or describe audio.",
        invocation="Called when audio is in scope (TTS / ASR).",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=4000,
        optional=True,
    ),
    AgentDef(
        role=Role.IMAGE_GEN,
        category=AgentCategory.MEDIA,
        purpose="Generate images from text prompts (ComfyUI / external).",
        invocation="Called via 'generate_image'. Cap of 3 generations per run (image_generation_cap_guard).",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.7,
        default_max_tokens=1000,
        optional=True,
    ),
    AgentDef(
        role=Role.ARCHITECT,
        category=AgentCategory.PRODUCER,
        purpose="High-level system design — directory layout, module boundaries, data flow. Should NOT write code.",
        invocation="Called via 'architect' for project-scale tasks.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.4,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.CRITIC,
        category=AgentCategory.PRODUCER,
        purpose="Adversarial pre-implementation review of a plan. Find holes before code is written.",
        invocation="Called via 'critique'.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.5,
        default_max_tokens=4000,
        optional=True,
    ),
    AgentDef(
        role=Role.TESTER,
        category=AgentCategory.PRODUCER,
        purpose="Write executable tests for code — typically pytest / unittest / vitest.",
        invocation="Called via 'test'. Auto-triggered by done-guards when code was executed but not tested.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.2,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.DEBUGGER,
        category=AgentCategory.PRODUCER,
        purpose="Diagnose root cause of a failure and propose a fix. Output is meta-analysis, not user-visible.",
        invocation="Called via 'debug'. Quota-capped by debugger_quota_guard at 2 attempts on identical error signatures.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.RESEARCHER,
        category=AgentCategory.PRODUCER,
        purpose="Synthesize information from fetched sources with citations.",
        invocation="Called via 'research' for question/research category prompts.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.4,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.REFACTORER,
        category=AgentCategory.PRODUCER,
        purpose="Restructure existing code without changing behavior.",
        invocation="Called via 'refactor'.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.2,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.TRANSLATOR,
        category=AgentCategory.PRODUCER,
        purpose="Translate text between languages. Output ONLY the translation — no commentary.",
        invocation="Called via 'translate'.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.1,
        default_max_tokens=4000,
        optional=True,
    ),
    AgentDef(
        role=Role.DATA_ANALYST,
        category=AgentCategory.PRODUCER,
        purpose="Analyze tabular/structured data — summary stats, trends, anomalies.",
        invocation="Called via 'analyze_data'.",
        output_contract=OutputContract.FREEFORM,
        default_temperature=0.3,
        default_max_tokens=8000,
        optional=True,
    ),
    AgentDef(
        role=Role.PREFLIGHT,
        category=AgentCategory.META,
        purpose="Pre-dispatch safety check. Returns approve / reorder / abort verdict against the proposed action.",
        invocation="Called by the engine after orchestrator decision, before tool dispatch. Cost-gated.",
        output_contract=OutputContract.JSON_STRICT,
        default_temperature=0.1,
        default_max_tokens=1000,
        optional=True,
        recommended_model_size="≥4B with reliable JSON",
    ),
    AgentDef(
        role=Role.POSTMORTEM,
        category=AgentCategory.META,
        purpose="Post-failure analysis. Emits retry / rule / user_prompt verdict with confidence.",
        invocation="Called by the engine when a run terminates unsuccessfully and is deemed learnable.",
        output_contract=OutputContract.JSON_STRICT,
        default_temperature=0.3,
        default_max_tokens=2000,
        optional=True,
    ),
)


AGENTS_BY_ROLE: dict[Role, AgentDef] = {a.role: a for a in AGENTS}


def get_agent(role: Role | str) -> AgentDef:
    """Look up an agent by Role enum or string id."""
    if isinstance(role, str):
        role = Role(role)
    if role not in AGENTS_BY_ROLE:
        raise KeyError(f"No agent defined for role: {role}")
    return AGENTS_BY_ROLE[role]


def agents_in_category(category: AgentCategory) -> list[AgentDef]:
    return [a for a in AGENTS if a.category is category]


# Sanity check — keep manifest in sync with the Role enum.
_ROLES_IN_MANIFEST = {a.role for a in AGENTS}
_ROLES_IN_ENUM = set(Role)
assert _ROLES_IN_MANIFEST == _ROLES_IN_ENUM, (
    f"Manifest/Role enum drift: missing in manifest = {_ROLES_IN_ENUM - _ROLES_IN_MANIFEST}, "
    f"missing in enum = {_ROLES_IN_MANIFEST - _ROLES_IN_ENUM}"
)
del _ROLES_IN_MANIFEST, _ROLES_IN_ENUM
