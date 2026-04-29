"""Autoconfig — pick a model for a role using TypeCast scores + VRAM fit.

The user's stated norm is "one computer, one LLM, one army of agents" — so
the *primary* affordance is `pick_default_model(installed)` which returns
one well-rounded model that scores positively on the most roles.
`pick_per_role(role, installed)` is the advanced override.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentcommander.typecast.catalog import get_catalog
from agentcommander.typecast.vram import detect_vram
from agentcommander.types import ALL_ROLES, Role


# ─── Mapping: AC Role enum ↔ TypeCast role names ───────────────────────────
#
# TypeCast uses singular role identifiers ("router", "orchestrator", …).
# The AC Role enum mirrors them with one exception: TypeCast tracks
# `postcheck` (a meta-role that didn't get its own AC role).
_AC_TO_TYPECAST: dict[Role, str] = {
    Role.ROUTER: "router",
    Role.ORCHESTRATOR: "orchestrator",
    Role.PLANNER: "planner",
    Role.CODER: "coder",
    Role.REVIEWER: "reviewer",
    Role.SUMMARIZER: "summarizer",
    Role.ARCHITECT: "architect",
    Role.CRITIC: "critic",
    Role.TESTER: "tester",
    Role.DEBUGGER: "debugger",
    Role.RESEARCHER: "researcher",
    Role.REFACTORER: "refactorer",
    Role.TRANSLATOR: "translator",
    Role.DATA_ANALYST: "data_analyst",
    Role.PREFLIGHT: "preflight",
    Role.POSTMORTEM: "postmortem",
}


@dataclass
class ModelCandidate:
    model_id: str
    entry: dict[str, Any]


@dataclass
class RolePick:
    role: Role
    model_id: str | None
    reason: str


@dataclass
class AutoconfigSuggestion:
    default_model: ModelCandidate | None
    overrides: list[RolePick]


def fits_available_vram(entry: dict[str, Any]) -> bool:
    """Filter out models that exceed available VRAM.

    Returns True (allow) if VRAM is unknown — we don't filter when we can't
    measure.
    """
    vram = detect_vram()
    if vram.total_gb == 0.0:
        return True
    needed = float(entry.get("estimatedVramGb") or 0)
    return needed <= vram.total_gb * 0.95


def build_candidates(installed_model_ids: set[str]) -> list[ModelCandidate]:
    """Build candidate list from the loaded catalog, filtered to installed models."""
    result = get_catalog()
    if result is None:
        return []
    out: list[ModelCandidate] = []
    for model_id, entry in result.catalog.items():
        if model_id == "_meta":
            continue
        if not isinstance(entry, dict):
            continue
        if model_id not in installed_model_ids:
            continue
        out.append(ModelCandidate(model_id=model_id, entry=entry))
    return out


def _role_score(entry: dict[str, Any], typecast_role: str) -> float:
    role_scores = entry.get("roleScores") or {}
    rs = role_scores.get(typecast_role) or {}
    score = rs.get("score")
    return float(score) if isinstance(score, (int, float)) else 0.0


def _avoids(entry: dict[str, Any], typecast_role: str) -> bool:
    avoid_for = entry.get("avoid_for") or []
    return typecast_role in avoid_for if isinstance(avoid_for, list) else False


def pick_default_model(candidates: list[ModelCandidate]) -> ModelCandidate | None:
    """Pick the single best model to assign to ALL roles.

    Scoring: count of roles where score > 0, weighted by score sum.
    Filters: must fit VRAM, must have at least one positive role.
    """
    scored: list[tuple[ModelCandidate, int, float]] = []
    for cand in candidates:
        if not fits_available_vram(cand.entry):
            continue
        positive_count = 0
        score_sum = 0.0
        for role in ALL_ROLES:
            tc = _AC_TO_TYPECAST.get(role)
            if not tc:
                continue
            s = _role_score(cand.entry, tc)
            if s > 0:
                positive_count += 1
            score_sum += s
        if positive_count == 0:
            continue
        scored.append((cand, positive_count, score_sum))
    scored.sort(key=lambda t: (t[1], t[2]), reverse=True)
    return scored[0][0] if scored else None


def pick_per_role(role: Role, candidates: list[ModelCandidate]) -> ModelCandidate | None:
    tc = _AC_TO_TYPECAST.get(role)
    if not tc:
        return None
    scored: list[tuple[ModelCandidate, float]] = []
    for cand in candidates:
        if not fits_available_vram(cand.entry):
            continue
        if _avoids(cand.entry, tc):
            continue
        s = _role_score(cand.entry, tc)
        if s <= 0:
            continue
        scored.append((cand, s))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[0][0] if scored else None


def suggest_config(installed_model_ids: set[str]) -> AutoconfigSuggestion:
    """Return one default model + per-role overrides where catalog says another wins by ≥30."""
    candidates = build_candidates(installed_model_ids)
    default = pick_default_model(candidates)
    overrides: list[RolePick] = []
    if default is None:
        return AutoconfigSuggestion(default_model=None, overrides=[])
    default_scores = default.entry.get("roleScores") or {}
    for role in ALL_ROLES:
        tc = _AC_TO_TYPECAST.get(role)
        if not tc:
            continue
        default_score = (default_scores.get(tc) or {}).get("score") or 0
        best = pick_per_role(role, candidates)
        if not best or best.model_id == default.model_id:
            continue
        best_score = _role_score(best.entry, tc)
        if best_score - default_score >= 30:
            overrides.append(RolePick(
                role=role,
                model_id=best.model_id,
                reason=(f"{best.model_id} scores {best_score} vs "
                        f"{default.model_id}'s {default_score} on {tc}"),
            ))
    return AutoconfigSuggestion(default_model=default, overrides=overrides)


# ─── Apply phase ──────────────────────────────────────────────────────────


@dataclass
class AutoconfigApplied:
    """Result of applying autoconfig — what changed and what was preserved."""

    default_model: str | None
    provider_id: str | None
    role_picks: dict[str, str]              # role.value -> model assigned by autoconfig
    user_overrides: dict[str, str]          # role.value -> existing user-set model (untouched)
    diff_picks: dict[str, str]              # role.value -> better-than-default model from TypeCast
    skipped_reason: str | None = None       # set if autoconfig didn't run (no models, no providers, etc.)


def _gather_installed(providers: list) -> tuple[set[str], dict[str, str]]:
    """Walk active providers and collect installed model IDs.

    Returns (set of model_ids, dict of model_id → first-provider-id-that-has-it).
    """
    installed_ids: set[str] = set()
    model_to_provider: dict[str, str] = {}
    for p in providers:
        try:
            for m in p.list_models():
                mid = m.get("id")
                if not mid:
                    continue
                installed_ids.add(mid)
                model_to_provider.setdefault(mid, p.id)
        except Exception:  # noqa: BLE001 — provider unreachable; just skip
            continue
    return installed_ids, model_to_provider


def apply_autoconfigure(
    *,
    providers: list,
    get_role_assignment_fn,
    set_role_assignment_fn,
    audit_fn=None,
) -> AutoconfigApplied:
    """Run TypeCast best-fit and write non-override role assignments to the DB.

    Args:
      providers: list of active provider instances (must expose .id and .list_models()).
      get_role_assignment_fn: db.repos.get_role_assignment.
      set_role_assignment_fn: db.repos.set_role_assignment.
      audit_fn: optional db.repos.audit for telemetry.

    Behavior:
      - Skips any role with `is_override=True` in the DB (user pinned manually).
      - Picks one default model from TypeCast that scores positive on the most roles.
      - For each role, assigns either a TypeCast diff-pick (if it beats the
        default by ≥30) or the default model.
      - Writes `is_override=False` for every assignment it makes.

    Returns AutoconfigApplied with details about what was assigned + preserved.
    """
    installed, model_to_provider = _gather_installed(providers)
    if not installed:
        return AutoconfigApplied(
            default_model=None, provider_id=None,
            role_picks={}, user_overrides={}, diff_picks={},
            skipped_reason="no installed models found across active providers",
        )

    suggestion = suggest_config(installed)
    if suggestion.default_model is None:
        return AutoconfigApplied(
            default_model=None, provider_id=None,
            role_picks={}, user_overrides={}, diff_picks={},
            skipped_reason=("no installed model has a positive TypeCast score "
                            "(or none fits available VRAM)"),
        )

    default_model = suggestion.default_model.model_id
    provider_id = model_to_provider.get(default_model)
    # Fall back to the first provider listed if the model→provider lookup failed
    # for any reason (very unusual — e.g. listing changed between calls).
    if not provider_id and providers:
        provider_id = providers[0].id
    if not provider_id:
        return AutoconfigApplied(
            default_model=default_model, provider_id=None,
            role_picks={}, user_overrides={}, diff_picks={},
            skipped_reason="no provider id resolvable for the chosen default model",
        )

    diff_pick_models: dict[str, str] = {
        o.role.value: o.model_id or default_model for o in suggestion.overrides
    }

    role_picks: dict[str, str] = {}
    user_overrides: dict[str, str] = {}
    for role in ALL_ROLES:
        existing = get_role_assignment_fn(role)
        if existing and existing.get("is_override"):
            user_overrides[role.value] = existing["model"]
            continue
        target_model = diff_pick_models.get(role.value, default_model)
        target_provider = model_to_provider.get(target_model, provider_id)
        set_role_assignment_fn(role, target_provider, target_model, is_override=False)
        role_picks[role.value] = target_model

    if audit_fn is not None:
        try:
            audit_fn("typecast.autoconfigure", {
                "default_model": default_model,
                "provider_id": provider_id,
                "diff_picks": diff_pick_models,
                "preserved_overrides": list(user_overrides.keys()),
            })
        except Exception:  # noqa: BLE001
            pass

    return AutoconfigApplied(
        default_model=default_model,
        provider_id=provider_id,
        role_picks=role_picks,
        user_overrides=user_overrides,
        diff_picks={k: v for k, v in diff_pick_models.items() if v != default_model},
    )
