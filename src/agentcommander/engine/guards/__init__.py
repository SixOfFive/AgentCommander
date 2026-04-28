"""Guard families ported from EngineCommander/src/main/orchestration/guards/.

9 families, ~110 individual guards, ~3,400 LOC at upstream.

  - types.py:           shared verdict + helpers (hasDeliverable, codeContext)
  - output_guards.py:   sanitize execution output (ANSI, base64, secrets, …)
  - write_guards.py:    block bad write_file calls
  - fetch_guards.py:    annotate fetch results with hint strings
  - post_step_guards.py: dead-end / anti-stuck / repeat-error / module-not-found
  - decision_guards.py: validate orchestrator JSON before dispatch
  - flow_guards.py:     cap repeated tool calls, oscillation, stale loops
  - execute_guards.py:  rewrite/block code on every `execute` action
  - done_guards.py:     reject premature `done`, force review/test/execute
"""

from agentcommander.engine.guards.types import (
    GuardVerdict,
    code_context,
    has_deliverable,
    user_wants_action,
)

__all__ = [
    "GuardVerdict",
    "code_context",
    "has_deliverable",
    "user_wants_action",
]
