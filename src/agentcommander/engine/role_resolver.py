"""Role → (provider, model) resolver.

Two-tier lookup:
  1. DB `role_assignments` table — user-set OVERRIDES only (always is_override=1).
     Survives across runs. Set by `/roles set <role> <provider> <model>`.
  2. In-memory autoconfig table — recomputed every startup by walking installed
     models + the TypeCast catalog. Never written to disk; if the catalog or
     model set changes, the next launch picks up the new best fit automatically.

Resolution order: override → autoconfig → None (role unassigned).

This module is the single read path that `call_role` and the TUI use.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentcommander.types import Role


@dataclass(frozen=True)
class ResolvedRole:
    role: Role
    provider_id: str
    model: str
    kind: str  # "override" | "auto"
    # When set, the provider should call the model with this num_ctx instead
    # of its built-in default. Persisted in `role_assignments` for overrides
    # set via `/autoconfig --mincontext N`. None means "use provider default".
    context_window_tokens: int | None = None


# In-memory map populated by the startup autoconfigure step.
# Cleared and re-populated every run; never persisted.
_autoconfig: dict[Role, tuple[str, str]] = {}


def set_autoconfig(table: dict[Role, tuple[str, str]]) -> None:
    """Replace the in-memory autoconfig map. Called by the startup wizard."""
    _autoconfig.clear()
    _autoconfig.update(table)


def clear_autoconfig() -> None:
    _autoconfig.clear()


def autoconfig_table() -> dict[Role, tuple[str, str]]:
    """Read-only view of the current in-memory autoconfig map."""
    return dict(_autoconfig)


SESSION_CONTEXT_OVERRIDE_KEY = "context_override_tokens"


def _session_context_override() -> int | None:
    """Read the session-wide num_ctx override (set by ``/context <N>``).

    Stored as an integer-valued JSON in the ``config`` table. Returns None
    when unset or unparseable. This override beats per-role
    ``context_window_tokens`` so a single ``/context 32k`` re-pins every
    role at once without touching their persisted bindings.
    """
    from agentcommander.db.repos import get_config  # lazy: avoid circulars
    raw = get_config(SESSION_CONTEXT_OVERRIDE_KEY, None)
    if raw is None:
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _session_ceiling_tokens() -> int | None:
    """Read the autoconfig-derived session ceiling (smallest training
    contextLength across picked models). Persisted in the config table by
    ``_print_session_context_summary`` at startup. Used as the *default*
    num_ctx when no explicit /context override or per-role binding exists,
    so every model runs with the announced ceiling instead of falling
    back to Ollama's 2048/4096 default.
    """
    from agentcommander.db.repos import get_config  # lazy: avoid circulars
    raw = get_config("session_ceiling_tokens", None)
    if raw is None:
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def resolve(role: Role | str) -> ResolvedRole | None:
    """Look up the (provider, model) bound to a role. Override beats autoconfig.

    Returns None if neither tier has a binding.

    ``context_window_tokens`` precedence (highest → lowest):
      1. session override from ``/context <N>`` — uniform for every role
      2. per-role override persisted on ``role_assignments`` (set by
         ``/autoconfig --mincontext`` or ``/roles set`` with a context)
      3. autoconfig session ceiling — the smallest training contextLength
         across picked models, computed at startup. Without this, the bar
         would announce e.g. "8k" but providers would actually receive
         num_ctx=None and fall back to Ollama's 2048/4096 default.
      4. None — provider's built-in default (only when no autoconfig has
         run yet, e.g. before first /providers add).
    """
    from agentcommander.db.repos import get_role_assignment  # lazy: avoid circulars

    role_enum = Role(role) if isinstance(role, str) else role
    session_ctx = _session_context_override()
    ceiling = _session_ceiling_tokens()

    def _pick_ctx(per_role_ctx: int | None) -> int | None:
        if session_ctx is not None:
            return session_ctx
        if per_role_ctx is not None:
            return per_role_ctx
        return ceiling

    # 1. DB override (user-set)
    a = get_role_assignment(role_enum)
    if a is not None:
        per_role_ctx = a.get("context_window_tokens")
        return ResolvedRole(
            role=role_enum,
            provider_id=a["provider_id"],
            model=a["model"],
            kind="override",
            context_window_tokens=_pick_ctx(per_role_ctx),
        )

    # 2. In-memory autoconfig
    pair = _autoconfig.get(role_enum)
    if pair is not None:
        return ResolvedRole(
            role=role_enum,
            provider_id=pair[0],
            model=pair[1],
            kind="auto",
            context_window_tokens=_pick_ctx(None),
        )

    return None


def resolve_all() -> list[ResolvedRole]:
    """Return ResolvedRole for every role enum that currently has a binding."""
    out: list[ResolvedRole] = []
    for r in Role:
        rr = resolve(r)
        if rr is not None:
            out.append(rr)
    return out
