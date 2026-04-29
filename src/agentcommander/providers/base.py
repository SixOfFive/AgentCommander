"""Provider base class + factory registry.

Each provider type (ollama, llamacpp, openrouter, anthropic, google) registers
a factory: a callable that maps a ProviderConfig → live ProviderBase instance.

The engine asks `resolve(provider_id)` for the live instance. Live instances
are cached per id; call `rebuild_from_db()` after the user mutates provider
config to refresh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Protocol, runtime_checkable

from agentcommander.types import ProviderConfig


class ProviderError(Exception):
    """Raised by provider implementations on transport/auth/format errors."""


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ChatChunk:
    content: str = ""
    done: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class _ProviderImpl(Protocol):
    id: str
    type: str

    def health(self) -> bool: ...

    def list_models(self) -> list[dict[str, Any]]: ...

    def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        json_mode: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterable[ChatChunk]: ...


class ProviderBase:
    """Concrete subclasses inherit from this for typed `id` / `type`.

    Subclasses must implement `health`, `list_models`, `chat`. We keep this
    as a regular class (not ABC) so subclasses can opt out of unsupported
    methods cleanly with a `ProviderError`.
    """

    id: str
    type: str

    def __init__(self, *, id: str, type: str) -> None:
        self.id = id
        self.type = type

    def health(self) -> bool:
        raise NotImplementedError

    def list_models(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        json_mode: bool = False,
    ) -> Iterable[ChatChunk]:
        raise NotImplementedError

    # ── Optional: model unloading ──
    # Provider types that hold models in memory between calls can override
    # these to free resources at exit. The default is a no-op so providers
    # that don't have the concept (e.g. llama.cpp serves one model per
    # process and is shut down with the process) don't accidentally do
    # anything. Only the Ollama provider overrides these in this codebase.

    def unload(self, model: str) -> bool:
        """Evict ``model`` from the provider's memory. Default: no-op."""
        return False

    def unload_all_loaded(self) -> int:
        """Evict every model currently resident on this provider.
        Returns the count of successful unloads. Default: no-op."""
        return 0


# ─── Factory registry ──────────────────────────────────────────────────────

ProviderFactory = Callable[[ProviderConfig], ProviderBase]

_factories: dict[str, ProviderFactory] = {}
_instances: dict[str, ProviderBase] = {}


def register_factory(provider_type: str, factory: ProviderFactory) -> None:
    """Register a provider factory by type id (e.g. 'ollama')."""
    _factories[provider_type] = factory


def provider_factory(provider_type: str) -> ProviderFactory:
    """Decorator form for builtin providers to self-register."""

    def decorator(factory: ProviderFactory) -> ProviderFactory:
        register_factory(provider_type, factory)
        return factory

    return decorator


def resolve(provider_id: str) -> ProviderBase:
    """Get the live provider instance for an id.

    Caller must call `rebuild_from_db()` first (or after config change).
    """
    inst = _instances.get(provider_id)
    if inst is None:
        raise ProviderError(f"Provider not loaded or not enabled: {provider_id}")
    return inst


def list_active() -> list[ProviderBase]:
    return list(_instances.values())


def rebuild_from_db() -> None:
    """Re-read the providers table and rebuild the live instance cache.

    Disabled providers are skipped. Failures during construction (e.g. an
    invalid endpoint) are logged via audit and the provider is left out.
    """
    # Lazy imports to avoid circulars at module load.
    from agentcommander.db.repos import audit, list_providers

    _instances.clear()
    for p in list_providers():
        if not p.enabled:
            continue
        factory = _factories.get(p.type)
        if factory is None:
            audit("provider.unknown_type", {"id": p.id, "type": p.type})
            continue
        try:
            _instances[p.id] = factory(p)
        except Exception as exc:  # noqa: BLE001
            audit("provider.build_failed", {"id": p.id, "error": str(exc)})


def loaded_factories() -> list[str]:
    return sorted(_factories.keys())
