"""Agent layer.

Single source of truth for the 19 agent roles:
  - manifest.py: structured metadata (purpose, defaults, output contract, guards)
  - prompts.py: loads the system-prompt .md files from resources/prompts/
"""

from agentcommander.agents.manifest import (
    AGENTS,
    AGENTS_BY_ROLE,
    AgentCategory,
    AgentDef,
    OutputContract,
    agents_in_category,
    get_agent,
)
from agentcommander.agents.prompts import (
    clear_prompt_cache,
    get_role_prompt,
    list_available_prompts,
)

__all__ = [
    "AGENTS",
    "AGENTS_BY_ROLE",
    "AgentCategory",
    "AgentDef",
    "OutputContract",
    "agents_in_category",
    "clear_prompt_cache",
    "get_agent",
    "get_role_prompt",
    "list_available_prompts",
]
