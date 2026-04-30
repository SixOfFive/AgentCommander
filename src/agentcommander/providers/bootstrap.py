"""Provider bootstrap — auto-discovers built-in providers and refreshes from DB.

Importing this module registers every built-in provider type's factory.
Call `bootstrap()` once at startup; safe to call again to reload after
the user mutates provider config.
"""
from __future__ import annotations

# Import each builtin provider so its `@provider_factory` registers.
# openrouter registers TWO factory types — `openrouter-free` and
# `openrouter-paid` — sharing one class but distinguished so the setup
# wizard can branch on intent (free tier auto-assigns one model to every
# text role; paid tier needs per-role best-guess and is currently disabled).
from agentcommander.providers import llamacpp, ollama, openrouter  # noqa: F401  (side-effect imports)
from agentcommander.providers.base import (
    loaded_factories,
    rebuild_from_db,
)


def bootstrap() -> list[str]:
    """Register builtins (already happened on import) and rebuild instances."""
    rebuild_from_db()
    return loaded_factories()
