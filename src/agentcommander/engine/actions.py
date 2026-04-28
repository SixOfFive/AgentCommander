"""Engine action set.

Mirrors EC's engine-actions.ts. Each iteration of the orchestrator emits a
JSON decision naming exactly one of these. The engine routes:
  - Role delegations  → role_call(role, ...)
  - Tool actions      → tools.invoke(name, payload, ctx)
  - 'done'            → run done-guards, emit final output, exit

NOTE: AC is SERIAL — there is no `parallel` action. Roles run one at a time.
"""
from __future__ import annotations

from agentcommander.types import Role


# Role delegations the orchestrator can emit.
ROLE_ACTIONS: frozenset[str] = frozenset({
    "plan",         # planner
    "code",         # coder
    "review",       # reviewer
    "summarize",   # summarizer
    "architect",    # architect
    "critique",     # critic
    "test",         # tester
    "debug",        # debugger
    "research",     # researcher
    "refactor",     # refactorer
    "translate",    # translator
    "analyze_data", # data_analyst
    "vision",       # vision
})

# Tool actions the orchestrator can dispatch.
TOOL_ACTIONS: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "list_dir",
    "delete_file",
    "execute",
    "fetch",
    "start_process",
    "kill_process",
    "check_process",
})

ALL_ACTIONS: frozenset[str] = ROLE_ACTIONS | TOOL_ACTIONS | {"done"}

# orchestrator action → role enum
ACTION_TO_ROLE: dict[str, Role] = {
    "plan": Role.PLANNER,
    "code": Role.CODER,
    "review": Role.REVIEWER,
    "summarize": Role.SUMMARIZER,
    "architect": Role.ARCHITECT,
    "critique": Role.CRITIC,
    "test": Role.TESTER,
    "debug": Role.DEBUGGER,
    "research": Role.RESEARCHER,
    "refactor": Role.REFACTORER,
    "translate": Role.TRANSLATOR,
    "analyze_data": Role.DATA_ANALYST,
    "vision": Role.VISION,
}
