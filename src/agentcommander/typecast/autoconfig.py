"""Autoconfig — pick a model for a role using TypeCast scores + VRAM fit.

Per-role assignment logic (current):
  For each role, pick the best-scoring installed model that fits VRAM and
  isn't on the entry's `avoid_for` list. Frame the decision as a descending
  threshold cascade — start at 100, drop by 10 each round down to
  ``ROLE_SCORE_MIN_THRESHOLD`` (10). The cascade is documentation: in code
  it collapses to "best pick if its score >= MIN_THRESHOLD, else unset"
  because TypeCast scores are coarse multiples of 20 (20/40/60/80/100/null).

  A role left unset after threshold 10 means no installed model can fill it
  (typical for vision/audio/image_gen on a text-only stack). Those rows show
  as `unset` in /roles and the user is expected to either install a capable
  model or assign one manually with /roles set.

`pick_default_model` / `pick_per_role` / `suggest_config` remain in the
public API for callers and tests, but the live `apply_autoconfigure` path
no longer uses them.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from agentcommander.typecast.catalog import get_catalog
from agentcommander.typecast.vram import detect_vram
from agentcommander.types import ALL_ROLES, Role


# Lowest TypeCast role-score that will still earn a role assignment.
# Scores below this leave the role unset for the user to fill with /roles set.
ROLE_SCORE_MIN_THRESHOLD = 10
ROLE_SCORE_MAX_THRESHOLD = 100
ROLE_SCORE_THRESHOLD_STEP = 10

# Config-table key holding the user's autoconfig ban list (JSON array of
# model IDs). Banned models are excluded from candidate consideration so
# autoconfigure never picks them — useful when a particular model is
# misbehaving on the user's hardware.
BANNED_MODELS_CONFIG_KEY = "autoconfig_banned_models"


def get_banned_models() -> set[str]:
    """Read the persisted ban list as a lowercase-comparable set."""
    from agentcommander.db.repos import get_config  # lazy: avoid circulars
    raw = get_config(BANNED_MODELS_CONFIG_KEY, None)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for entry in raw:
        if isinstance(entry, str) and entry:
            out.add(entry)
    return out


def set_banned_models(models: set[str] | list[str]) -> None:
    """Replace the persisted ban list with ``models``."""
    from agentcommander.db.repos import set_config  # lazy: avoid circulars
    set_config(BANNED_MODELS_CONFIG_KEY, sorted(set(models)))


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
    """Build candidate list from the loaded catalog, filtered to installed models.

    Banned models (per ``/autoconfig ban``) are dropped here so they're
    invisible to every downstream picker — the default election, per-role
    scoring, the threshold cascade. The user can still call them directly
    via ``/roles set`` if they want.
    """
    result = get_catalog()
    if result is None:
        return []
    banned = get_banned_models()
    out: list[ModelCandidate] = []
    for model_id, entry in result.catalog.items():
        if model_id == "_meta":
            continue
        if not isinstance(entry, dict):
            continue
        if model_id not in installed_model_ids:
            continue
        if model_id in banned:
            continue
        out.append(ModelCandidate(model_id=model_id, entry=entry))
    return out


def _role_score(entry: dict[str, Any], typecast_role: str) -> float:
    """TypeCast catalog score for ``(model, role)``. Range: 0–100 in the
    catalog (coarse multiples of 20). The hint accumulator adds an
    independent ±100-clamped adjustment on top — see ``_role_score_with_hint``.
    """
    role_scores = entry.get("roleScores") or {}
    rs = role_scores.get(typecast_role) or {}
    score = rs.get("score")
    return float(score) if isinstance(score, (int, float)) else 0.0


def _role_score_with_hint(
    entry: dict[str, Any], typecast_role: str, model_id: str,
) -> float:
    """``_role_score`` + the persisted hint bump for this ``(model, role)``.

    The hint accumulator (see ``engine.engine._bump_hint_for_label``)
    bumps a per-(model, role) score ±0.1 after every role call —
    positive for success, negative for failure / rate-limit / role-not-
    assigned. Hints clamp at ±100, matching the catalog's range, so a
    chronically-broken model is reachable from the cascade only by
    accumulating ~1000 successes after it earned its negative hint.

    Hints are read lazily (one DB query per call) — overhead is fine
    here because autoconfig runs once at startup, not per-token.
    """
    base = _role_score(entry, typecast_role)
    try:
        from agentcommander.db.repos import get_hint
        hint = get_hint(model_id, typecast_role)
    except Exception:  # noqa: BLE001
        hint = 0.0
    return base + hint


def _avoids(entry: dict[str, Any], typecast_role: str) -> bool:
    avoid_for = entry.get("avoid_for") or []
    return typecast_role in avoid_for if isinstance(avoid_for, list) else False


def pick_default_model(candidates: list[ModelCandidate]) -> ModelCandidate | None:
    """Pick the single best model to assign to ALL roles.

    Scoring: count of roles where ``base_score + hint`` > 0, weighted by
    score sum. Filters: must fit VRAM, must have at least one positive
    role. Hints from the model_hints table are folded in — a chronic
    failer earns a negative hint and falls behind in this election.
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
            s = _role_score_with_hint(cand.entry, tc, cand.model_id)
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
        s = _role_score_with_hint(cand.entry, tc, cand.model_id)
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
    """Result of applying autoconfig — what got picked + what was preserved.

    The picks here are NOT persisted to the DB. They are loaded into the
    in-memory `engine.role_resolver._autoconfig` table so they're effective
    for the current run and recomputed every launch. Only user overrides
    (set via /roles set ...) live in the DB.
    """

    default_model: str | None
    provider_id: str | None
    # role.value -> (provider_id, model) chosen by autoconfig this run
    role_picks: dict[str, tuple[str, str]] = field(default_factory=dict)
    # role.value -> model — existing user overrides that were respected (read from DB)
    user_overrides: dict[str, str] = field(default_factory=dict)
    # role.value -> model — picks that differ from the most-common ("primary") model
    diff_picks: dict[str, str] = field(default_factory=dict)
    # role.value list — roles where no installed model scored >= the minimum
    # threshold; left for the user to assign manually with /roles set
    unset_roles: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    # True when no installed model was in the TypeCast catalog and we fell
    # back to assigning the first installed model to all text-capable roles.
    # Mainly fires for llama.cpp (single GGUF, never in the catalog).
    fallback_no_catalog: bool = False


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


def _entry_context_length(entry: dict[str, Any]) -> int:
    """Best-effort read of the catalog entry's max context window in tokens.
    Returns 0 when the catalog doesn't carry that field for this model."""
    raw = entry.get("contextLength")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(float(raw))
        except ValueError:
            return 0
    return 0


def _best_pick_for_role(
    role: Role,
    candidates: list[ModelCandidate],
    *,
    min_context: int = 0,
) -> tuple[ModelCandidate | None, float]:
    """Return ``(best_candidate, best_score)`` for this role, or ``(None, 0)``.

    Filters:
      - must fit available VRAM
      - must not be in the entry's ``avoid_for`` list
      - if ``min_context > 0``, the catalog entry's ``contextLength`` must be
        at least ``min_context`` tokens (drops models trained on a smaller
        window so we don't quietly downgrade the user's chosen context)

    The "best" candidate is the one with the highest TypeCast score on this
    role; ties are broken by iteration order (which mirrors the catalog).
    """
    tc = _AC_TO_TYPECAST.get(role)
    if not tc:
        return None, 0.0
    best: ModelCandidate | None = None
    best_score = -1.0
    for cand in candidates:
        if not fits_available_vram(cand.entry):
            continue
        if _avoids(cand.entry, tc):
            continue
        if min_context > 0 and _entry_context_length(cand.entry) < min_context:
            continue
        s = _role_score(cand.entry, tc)
        if s > best_score:
            best_score = s
            best = cand
    if best is None or best_score <= 0:
        return None, 0.0
    return best, best_score


def apply_autoconfigure(
    *,
    providers: list,
    get_role_assignment_fn,
    audit_fn=None,
    min_context: int = 0,
) -> AutoconfigApplied:
    """Run TypeCast best-fit per role and return an in-memory map. Does NOT write the DB.

    Args:
      providers: list of active provider instances (must expose .id and .list_models()).
      get_role_assignment_fn: db.repos.get_role_assignment — used only to
        identify roles the user has overridden (so we know to skip them).
      audit_fn: optional db.repos.audit for telemetry.

    Behavior:
      - Calls each provider's list_models() to discover what's installed.
      - For each role, walks a descending threshold cascade
        (``ROLE_SCORE_MAX_THRESHOLD`` → ``ROLE_SCORE_MIN_THRESHOLD``,
        step = ``ROLE_SCORE_THRESHOLD_STEP``). The first round at which some
        installed model meets the threshold for that role wins it.
      - Roles where no installed model scores at or above
        ``ROLE_SCORE_MIN_THRESHOLD`` end up in ``unset_roles`` — typically
        vision/audio/image_gen on a text-only stack. The user fills those in
        manually with ``/roles set <role> <provider> <model>``.
      - Roles with a DB override are preserved as-is; the override wins at
        resolve time.
      - Returns an in-memory map of (role -> (provider, model)). The caller is
        expected to feed that into engine.role_resolver.set_autoconfig.
      - The summary fields ``default_model`` / ``diff_picks`` describe the
        most-common model across role_picks (the "primary") and which roles
        diverged from it — used only for the boot-line summary.

    Recomputed every startup so a newly-pulled or removed model is reflected
    automatically without DB writes.
    """
    installed, model_to_provider = _gather_installed(providers)
    if not installed:
        return AutoconfigApplied(
            default_model=None, provider_id=None,
            skipped_reason="no installed models found across active providers",
        )

    candidates = build_candidates(installed)
    if not candidates:
        # Fallback: nothing is in the TypeCast catalog (typical for
        # llama.cpp serving a single uncatalogued GGUF, or an Ollama box
        # with only out-of-catalog models). Assign the first installed
        # model to every role that has a TypeCast role mapping
        # (vision/audio/image_gen have no mapping and stay unset).
        return _apply_fallback_no_catalog(
            installed=installed,
            model_to_provider=model_to_provider,
            providers=providers,
            get_role_assignment_fn=get_role_assignment_fn,
            audit_fn=audit_fn,
        )

    role_picks: dict[str, tuple[str, str]] = {}
    user_overrides: dict[str, str] = {}
    unset_roles: list[str] = []

    # Threshold cascade — pick a model for each role at the first round where
    # some installed model meets the threshold. Equivalent to "best-pick gated
    # at MIN_THRESHOLD" (TypeCast scores fall on multiples of 20), but the
    # explicit walk matches the user-facing description and makes future
    # threshold tweaks (e.g. adding a stricter modality filter at high
    # thresholds) a one-line change.
    for role in ALL_ROLES:
        existing = get_role_assignment_fn(role)
        if existing and existing.get("is_override"):
            user_overrides[role.value] = existing["model"]
            continue
        best, best_score = _best_pick_for_role(
            role, candidates, min_context=min_context,
        )
        assigned = False
        if best is not None:
            for threshold in range(
                ROLE_SCORE_MAX_THRESHOLD,
                ROLE_SCORE_MIN_THRESHOLD - 1,
                -ROLE_SCORE_THRESHOLD_STEP,
            ):
                if best_score >= threshold:
                    provider_id = model_to_provider.get(best.model_id) or providers[0].id
                    role_picks[role.value] = (provider_id, best.model_id)
                    assigned = True
                    break
        if not assigned:
            unset_roles.append(role.value)

    # "primary" model = the one used by the most roles; used only for the
    # one-line boot summary. With "one army of agents" this is usually the
    # default a user thinks of, but with the per-role picker it's just a label.
    counts = Counter(m for (_, m) in role_picks.values())
    if counts:
        default_model = counts.most_common(1)[0][0]
        provider_id = next(
            (p for (p, m) in role_picks.values() if m == default_model),
            None,
        )
    else:
        default_model = None
        provider_id = None

    diff_picks: dict[str, str] = {}
    if default_model:
        for role_value, (_, m) in role_picks.items():
            if m != default_model:
                diff_picks[role_value] = m

    if audit_fn is not None:
        try:
            audit_fn("typecast.autoconfigure", {
                "default_model": default_model,
                "provider_id": provider_id,
                "role_picks": {k: m for k, (_, m) in role_picks.items()},
                "preserved_overrides": list(user_overrides.keys()),
                "unset_roles": unset_roles,
            })
        except Exception:  # noqa: BLE001
            pass

    return AutoconfigApplied(
        default_model=default_model,
        provider_id=provider_id,
        role_picks=role_picks,
        user_overrides=user_overrides,
        diff_picks=diff_picks,
        unset_roles=unset_roles,
    )
