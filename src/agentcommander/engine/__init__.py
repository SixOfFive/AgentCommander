"""Pipeline engine — orchestration loop with guards.

  - actions.py: action enum + role-action mapping
  - scratchpad.py: serialization, compaction, output assembly
  - role_call.py: invoke a role through the configured provider
  - engine.py: PipelineRun — the main serial loop
  - guards/: 9 guard families (decision, flow, execute, write, output, fetch,
             post-step, done, plus shared types)
"""

from agentcommander.engine.actions import (
    ALL_ACTIONS,
    ACTION_TO_ROLE,
    ROLE_ACTIONS,
    TOOL_ACTIONS,
)
from agentcommander.engine.engine import PipelineEvent, PipelineRun, RunOptions
from agentcommander.engine.scratchpad import (
    build_final_output,
    compact_scratchpad,
    push_nudge,
)

__all__ = [
    "ACTION_TO_ROLE",
    "ALL_ACTIONS",
    "PipelineEvent",
    "PipelineRun",
    "ROLE_ACTIONS",
    "RunOptions",
    "TOOL_ACTIONS",
    "build_final_output",
    "compact_scratchpad",
    "push_nudge",
]
