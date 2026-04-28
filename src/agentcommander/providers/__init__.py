"""LLM providers — pluggable, registered through the registry.

Each backend lives in its own module. A built-in provider self-registers a
factory function in the central registry; user-supplied providers can be
added the same way (drop a .py with a `register()` call).

  - base.py: shared types + Provider base class
  - ollama.py: Ollama HTTP /api/chat (streaming)
  - llamacpp.py: llama-server OpenAI-compat /v1/chat/completions (streaming)
  - openrouter.py, anthropic.py, google.py: stubs (TODO)
"""

from agentcommander.providers.base import (
    ChatChunk,
    ChatMessage,
    ProviderBase,
    ProviderError,
    list_active,
    provider_factory,
    rebuild_from_db,
    register_factory,
    resolve,
)

__all__ = [
    "ChatChunk",
    "ChatMessage",
    "ProviderBase",
    "ProviderError",
    "list_active",
    "provider_factory",
    "rebuild_from_db",
    "register_factory",
    "resolve",
]
