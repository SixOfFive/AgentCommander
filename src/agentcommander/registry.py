"""Protocol-based registries.

Single source of truth for *modular* extensibility. Three things plug in
through this module: providers, tools, guard families. Each has:
  - a Protocol that defines the interface
  - a typed dict (id → instance) registry
  - simple `register` / `get` / `list_all` / `unregister` helpers
  - optional `discover` that auto-imports a directory so adding a new file
    is enough to register a new plugin

NOTE: The Protocol shapes here are import-only — concrete classes live in
the providers/, tools/, engine/guards/ packages. Keeping the protocols here
avoids circular imports.
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Protocol, TypeVar, runtime_checkable

from agentcommander.types import OrchestratorDecision, ScratchpadEntry

# ─── Provider protocol ─────────────────────────────────────────────────────


@runtime_checkable
class Provider(Protocol):
    """Streaming chat-completion provider (Ollama, llama.cpp, OpenRouter, ...)."""

    id: str
    type: str

    def health(self) -> bool: ...

    def list_models(self) -> list[dict[str, Any]]: ...

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        json_mode: bool = False,
    ) -> Iterable[dict[str, Any]]:
        """Yield streaming chunks. Each chunk: {'content': str, 'done': bool, 'usage'?: dict}."""
        ...


# ─── Tool protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class ToolHandler(Protocol):
    """A single tool implementation."""

    name: str
    description: str
    privileged: bool
    input_schema: dict[str, Any]

    def __call__(self, payload: dict[str, Any], ctx: "ToolContext") -> "ToolResult": ...


# Concrete dataclasses for tool-call payload/response live in tools/types.py.
# We import them lazily inside the registry to avoid circular deps when this
# module is imported very early.


# ─── Guard protocol ────────────────────────────────────────────────────────


@runtime_checkable
class GuardFamily(Protocol):
    """A guard family (decision / flow / execute / write / output / fetch /
    post-step / done). Each family exposes a single `run(ctx)` entry-point
    that internally dispatches to its individual guards.
    """

    family_name: str  # "decision", "flow", "execute", ...

    def run(self, ctx: Any) -> Any:  # GuardVerdict — concrete shape per family
        ...


# ─── Generic typed registry ────────────────────────────────────────────────

T = TypeVar("T")


class Registry(dict[str, T]):
    """Thin wrapper over dict with a friendlier registration API.

    Subclass-free — instantiate one per kind.
    """

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def register(self, item: T, *, key: str | None = None) -> T:
        k = key or getattr(item, "id", None) or getattr(item, "name", None) or getattr(item, "family_name", None)
        if not isinstance(k, str):
            raise ValueError(f"{self.kind}: cannot infer registration key for {item!r}")
        self[k] = item
        return item

    def unregister(self, key: str) -> None:
        self.pop(key, None)

    def list_all(self) -> list[T]:
        return list(self.values())


providers: Registry[Provider] = Registry("provider")
tools: Registry["ToolHandler"] = Registry("tool")
guard_families: Registry[GuardFamily] = Registry("guard")


# ─── Auto-discovery ────────────────────────────────────────────────────────


def discover_plugins(package_name: str) -> list[str]:
    """Import every submodule of `package_name` so they can self-register on import.

    Used at startup: discover_plugins("agentcommander.tools.builtin") imports
    every .py file in that package, each of which calls `tools.register(...)`
    at module top-level.

    Returns the list of imported module names.
    """
    package = importlib.import_module(package_name)
    pkg_path = getattr(package, "__path__", None)
    if not pkg_path:
        return []
    imported: list[str] = []
    for _finder, name, _is_pkg in pkgutil.iter_modules(pkg_path):
        full = f"{package_name}.{name}"
        importlib.import_module(full)
        imported.append(full)
    return imported


def discover_directory(path: Path, package_name: str) -> list[str]:
    """Import every .py file under `path` as part of `package_name`.

    For user-supplied plugin directories outside the installed package.
    """
    if not path.exists() or not path.is_dir():
        return []
    imported: list[str] = []
    for py in sorted(path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        mod_name = f"{package_name}.{py.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, py)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        imported.append(mod_name)
    return imported


# Re-export the dataclass types needed by tool-handler signatures so callers
# only need to import from this module.
def _import_tool_types() -> tuple[type, type]:
    from agentcommander.tools.types import ToolContext, ToolResult  # noqa: PLC0415
    return ToolContext, ToolResult


# Lazy attribute access so `from agentcommander.registry import ToolContext`
# works without forcing tools/ to be imported eagerly.
def __getattr__(name: str) -> Any:
    if name in ("ToolContext", "ToolResult"):
        ctx_t, res_t = _import_tool_types()
        return {"ToolContext": ctx_t, "ToolResult": res_t}[name]
    raise AttributeError(name)


__all__ = [
    "GuardFamily",
    "Provider",
    "Registry",
    "ToolHandler",
    "discover_directory",
    "discover_plugins",
    "guard_families",
    "providers",
    "tools",
]


# Suppress unused warning — these are referenced through __getattr__ above
_ = (OrchestratorDecision, ScratchpadEntry, Callable, AsyncIterator)
