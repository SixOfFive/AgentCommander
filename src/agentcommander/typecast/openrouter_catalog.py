"""OpenRouter scores catalogs — separate files for free and paid tiers.

Two catalogs live at the AgentCommander repo root (the "main project
folder"), parallel to the main TypeCast catalog for Ollama:

  - ``resources/typecast-openrouter-free.json`` — scored ``:free``-tier
    models. ``configure_openrouter_free`` fetches OpenRouter's /models
    endpoint, filters to ids ending ``:free``, and seeds the catalog
    with metadata. Votes accumulate from live runs.
  - ``resources/typecast-openrouter-paid.json`` — scored paid-tier
    models. Same flow, filtered to ids NOT ending ``:free``. Free
    models never appear in this catalog so the picker can't fall back
    to one when paid is selected.

Voting (matches the user's spec): each successful call from an OR
provider records a +1 vote for ``(model, role)``. Each rate-limit (HTTP
429) records a -1 vote. Over time, the highest-voted model per role
becomes the autoconfig pick.

Metadata propagated from ``/v1/models`` per entry:
  - name              display name (e.g. "Llama 3.3 70B Instruct")
  - contextLength     max context window (top_provider.context_length
                      preferred over the root context_length)
  - max_completion    top_provider.max_completion_tokens (or None)
  - pricing_prompt    USD per million input tokens (str — OR returns
                      it as a string for precision)
  - pricing_completion USD per million output tokens
  - modality          "text" / "multimodal" / etc.
  - supported_params  list of params accepted by /v1/chat/completions
                      (used to gate features like response_format)

The threshold-cascade picker in ``autoconfig.py`` doesn't currently
consume these — it's Ollama-only — but the data is here for when we
extend it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


TIER_FREE = "free"
TIER_PAID = "paid"

_FILENAME_BY_TIER: dict[str, str] = {
    TIER_FREE: "typecast-openrouter-free.json",
    TIER_PAID: "typecast-openrouter-paid.json",
}

_ENV_BY_TIER: dict[str, str] = {
    TIER_FREE: "AGENTCOMMANDER_OR_FREE_CATALOG",
    TIER_PAID: "AGENTCOMMANDER_OR_PAID_CATALOG",
}

VOTE_INCREMENT = 1
VOTE_MAX = 1_000_000
VOTE_MIN = -1_000_000


def _check_tier(tier: str) -> None:
    if tier not in _FILENAME_BY_TIER:
        raise ValueError(f'tier must be "free" or "paid"; got {tier!r}')


def empty_catalog(tier: str) -> dict[str, Any]:
    """Return the seed shape used when the file is missing or corrupt.

    Two top-level keys: ``_meta`` and ``_models``. The shape mirrors
    the main TypeCast catalog so a future generic picker could consume
    either file.
    """
    _check_tier(tier)
    return {
        "_meta": {
            "tier": tier,
            "description": (
                f"OpenRouter {tier} model scores per agent role. Fetched "
                "from OpenRouter /v1/models on configure; votes "
                "accumulated from live runs (+1 success, -1 rate-limit)."
            ),
            "registrySource": "openrouter.ai/models",
            "voteIncrement": VOTE_INCREMENT,
            "voteMax": VOTE_MAX,
            "voteMin": VOTE_MIN,
            "lastFetchedAt": None,
            "lastVoteAt": None,
            "voteCount": 0,
            "modelCount": 0,
        },
        "_models": {},
    }


def catalog_filename(tier: str) -> str:
    _check_tier(tier)
    return _FILENAME_BY_TIER[tier]


def catalog_path(tier: str) -> Path:
    """Locate the catalog file for ``tier`` in the AgentCommander repo.

    Search order (matches ``agents/prompts.py:_prompt_dir``):
      1. env override (``AGENTCOMMANDER_OR_FREE_CATALOG`` / ``..._PAID_...``)
      2. installed-package neighbor: ``<pkg>/../../resources/<file>``
      3. repo-dev: walk up from this module until ``resources/<file>``
      4. fallback: first parent with a ``resources/`` dir even if the
         file isn't there yet — that's where ``save`` will create it.
    """
    _check_tier(tier)
    import os

    fname = _FILENAME_BY_TIER[tier]
    env_key = _ENV_BY_TIER[tier]

    env = os.environ.get(env_key)
    if env:
        return Path(env)

    pkg_neighbor = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "resources" / fname
    )
    if pkg_neighbor.is_file():
        return pkg_neighbor

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "resources" / fname
        if candidate.is_file():
            return candidate
        if (parent / "resources").is_dir():
            return parent / "resources" / fname

    return Path("resources") / fname


def load(tier: str) -> dict[str, Any]:
    """Read the catalog. Returns the empty seed shape on any failure
    (missing file, corrupt JSON, wrong-shape root). Voting writes
    silently overwrite a corrupt file with the empty shape on the next
    save so transient corruption self-heals.
    """
    _check_tier(tier)
    path = catalog_path(tier)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return empty_catalog(tier)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return empty_catalog(tier)
    if not isinstance(data, dict):
        return empty_catalog(tier)
    if "_models" not in data or not isinstance(data["_models"], dict):
        data["_models"] = {}
    if "_meta" not in data or not isinstance(data["_meta"], dict):
        data["_meta"] = empty_catalog(tier)["_meta"]
    return data


def save(tier: str, catalog: dict[str, Any]) -> bool:
    """Persist the catalog to disk. Returns True on success, False on
    write failure (read-only filesystem, missing parent dir, etc.).

    Updates ``_meta.modelCount`` and ``_meta.lastVoteAt`` (when this
    save is part of a vote — caller-driven). Other meta fields are
    preserved as-is.
    """
    _check_tier(tier)
    path = catalog_path(tier)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    meta = catalog.setdefault("_meta", empty_catalog(tier)["_meta"])
    meta["tier"] = tier
    meta["modelCount"] = len(catalog.get("_models", {}))
    try:
        path.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _empty_role_stats() -> dict[str, Any]:
    """Per-(model, role) stats. Each role tracked independently so a
    model that's great at coder doesn't get credit for the orchestrator
    role, and vice versa. ``score`` is what the picker reads — built
    from successes/rate_limits per role."""
    return {
        "score": 0,
        "successes": 0,
        "rate_limits": 0,
        "runs": 0,
        "lastBumpAt": 0,
    }


def _empty_model_entry() -> dict[str, Any]:
    """Per-model row default. Stats are now nested under ``by_role`` —
    a flat ``score`` field is gone. Metadata fields stay flat (they're
    intrinsic properties of the model, not per-role)."""
    return {
        # Metadata (filled by ``populate_from_openrouter``):
        "name": None,
        "contextLength": None,
        "max_completion_tokens": None,
        "pricing_prompt": None,
        "pricing_completion": None,
        "modality": None,
        "supported_params": [],
        # Per-role voting stats — added on first vote for that role.
        # Shape: {role_name: {score, successes, rate_limits, runs, lastBumpAt}}
        "by_role": {},
    }


def _ensure_role_stats(entry: dict[str, Any], role: str) -> dict[str, Any]:
    """Reach into ``entry["by_role"][role]``, initializing if absent.
    Returns the per-role stats dict for direct mutation by the caller.
    Also handles legacy entries that have ``score`` at the top level
    (pre-by-role schema) by silently dropping the global field."""
    by_role = entry.setdefault("by_role", {})
    stats = by_role.get(role)
    if stats is None:
        stats = _empty_role_stats()
        by_role[role] = stats
    return stats


def populate_from_openrouter(tier: str,
                              openrouter_models: list[dict[str, Any]]) -> int:
    """Refresh the catalog's metadata from a fresh /v1/models response.

    Existing votes (``score``, ``preferred_for``, ``avoid_for``,
    ``successes``, ``rate_limits``, ``runs``, ``lastBumpAt``) are
    preserved — only the metadata fields are overwritten. New models
    get added with the empty-entry shape; models that disappeared
    upstream are kept (votes still apply if upstream un-deprecates).

    Returns the number of models in the catalog after populate.
    """
    _check_tier(tier)
    catalog = load(tier)
    models = catalog["_models"]

    free_filter = (tier == TIER_FREE)

    for m in openrouter_models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        is_free = mid.endswith(":free")
        if free_filter and not is_free:
            continue
        if not free_filter and is_free:
            continue

        entry = models.get(mid) or _empty_model_entry()

        # Metadata (overwrite — these change on upstream model updates)
        entry["name"] = m.get("name") or m.get("canonical_slug") or mid
        # OR exposes context_length at root AND nested under top_provider.
        # Prefer top_provider's value when present (it's the cap that the
        # actual upstream provider honors; the root one can be a hint).
        ctx = m.get("context_length")
        tp = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else None
        if tp and isinstance(tp.get("context_length"), int):
            ctx = tp["context_length"]
        entry["contextLength"] = ctx if isinstance(ctx, int) else None
        if tp and isinstance(tp.get("max_completion_tokens"), int):
            entry["max_completion_tokens"] = tp["max_completion_tokens"]
        else:
            entry["max_completion_tokens"] = None
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        # Prices are returned as strings for precision (e.g. "0.00000060");
        # we store them as-is and let the UI format.
        entry["pricing_prompt"] = pricing.get("prompt")
        entry["pricing_completion"] = pricing.get("completion")
        arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
        entry["modality"] = arch.get("modality") or arch.get("input_modalities")
        supported = m.get("supported_parameters")
        entry["supported_params"] = supported if isinstance(supported, list) else []

        models[mid] = entry

    catalog["_meta"]["lastFetchedAt"] = int(time.time() * 1000)
    save(tier, catalog)
    return len(models)


def record_vote(tier: str, model_id: str, role: str, *,
                scope: str = "preferred",
                increment: int = VOTE_INCREMENT) -> int:
    """Apply one ±vote to ``(model_id, role)``. Returns the new per-role
    score for that pair.

    Per-(model, role) scoring: a +1 for ``coder`` does NOT raise the
    model's score for ``translator``. The picker reads ``by_role[role]``
    when ranking candidates, so each agent ends up with its own
    independent best-fit ranking.

    ``scope='preferred'`` → ``+increment``, ``successes`` counter +1.
    ``scope='avoid'`` → ``-increment``, ``rate_limits`` counter +1.

    The model is registered on the fly if absent; metadata stays None
    until the next ``populate_from_openrouter``.
    """
    _check_tier(tier)
    catalog = load(tier)
    models = catalog["_models"]
    if model_id not in models:
        models[model_id] = _empty_model_entry()
    entry = models[model_id]
    stats = _ensure_role_stats(entry, role)

    if scope == "preferred":
        delta = abs(increment)
        stats["successes"] = int(stats.get("successes", 0)) + 1
    elif scope == "avoid":
        delta = -abs(increment)
        stats["rate_limits"] = int(stats.get("rate_limits", 0)) + 1
    else:
        raise ValueError(f'scope must be "preferred" or "avoid"; got {scope!r}')

    stats["score"] = max(VOTE_MIN,
                         min(VOTE_MAX, int(stats.get("score", 0)) + delta))
    stats["runs"] = int(stats.get("runs", 0)) + 1
    stats["lastBumpAt"] = int(time.time() * 1000)
    catalog["_meta"]["voteCount"] = int(catalog["_meta"].get("voteCount", 0)) + 1
    catalog["_meta"]["lastVoteAt"] = int(time.time() * 1000)
    save(tier, catalog)
    return int(stats["score"])


def pick_for_role(tier: str, role: str, *,
                   fallback: str | None = None) -> str | None:
    """Return the best model for ``role`` from the catalog.

    Selection rules:
      1. Models with ``role in preferred_for`` rank first
      2. Among those, highest ``score`` wins (ties broken by ``successes``,
         then alphabetical for determinism)
      3. If nothing has the role in preferred_for, fall back to the
         single highest-scoring model overall (any positive score)
      4. Final fallback: ``fallback`` argument

    Returns ``None`` only when the catalog is empty AND no fallback
    is provided.
    """
    _check_tier(tier)
    catalog = load(tier)
    models: dict[str, dict[str, Any]] = catalog.get("_models") or {}
    if not models:
        return fallback

    role_lower = role.lower()

    def _key(item: tuple[str, dict[str, Any]]) -> tuple:
        mid, entry = item
        score = int(entry.get("score", 0))
        succ = int(entry.get("successes", 0))
        return (-score, -succ, mid)

    # Tier 1: explicit preferred_for hits
    preferred = [
        (mid, e) for mid, e in models.items()
        if role_lower in [r.lower() for r in (e.get("preferred_for") or [])]
        and role_lower not in [r.lower() for r in (e.get("avoid_for") or [])]
    ]
    if preferred:
        preferred.sort(key=_key)
        return preferred[0][0]

    # Tier 2: any model with positive score (and not in avoid_for for this role)
    scored = [
        (mid, e) for mid, e in models.items()
        if int(e.get("score", 0)) > 0
        and role_lower not in [r.lower() for r in (e.get("avoid_for") or [])]
    ]
    if scored:
        scored.sort(key=_key)
        return scored[0][0]

    # Tier 3: any model not in avoid_for (sort by score desc — even 0
    # is fine since it just means "untested but allowed")
    allowed = [
        (mid, e) for mid, e in models.items()
        if role_lower not in [r.lower() for r in (e.get("avoid_for") or [])]
    ]
    if allowed:
        allowed.sort(key=_key)
        return allowed[0][0]

    return fallback


def has_data(tier: str) -> bool:
    """True when the catalog has at least one model registered."""
    _check_tier(tier)
    return bool(load(tier).get("_models"))


# ─── Rate-limit voting (the only voting trigger) ──────────────────────────
#
# Voting model per the user's spec:
#   - On HTTP 429 for (model X, role R):
#       - X gets -1 (and role goes into avoid_for[X])
#       - every OTHER model in the same tier gets +1 for R
#         (and role goes into preferred_for[Y])
#
# Successful calls do NOT trigger voting. Only rate-limit events shift
# the relative ranking. This keeps the catalog signal pure ("X gets
# throttled, others don't") instead of accumulating monotonic +1s on
# whichever model the user happens to call most.
#
# Implemented as a single load → mutate-everything → save pass so a
# catalog with hundreds of models doesn't churn the disk.


def vote_after_rate_limit(tier: str, failed_model: str, role: str) -> int:
    """Single batched vote: -1 the failed model, +1 every other model in
    the same tier's catalog for ``role``. Returns the count of OTHER
    models that got the +1.

    Counters tracked per entry: ``score`` (clamped), ``runs`` (every
    bump), ``successes`` / ``rate_limits`` (separate counters so the
    user can see why a model's score is what it is).
    """
    _check_tier(tier)
    if not failed_model or not role:
        return 0
    catalog = load(tier)
    models = catalog["_models"]
    now_ms = int(time.time() * 1000)

    # Penalize the failed model — register it on the fly if absent so a
    # vote against a model the user typed manually still lands.
    if failed_model not in models:
        models[failed_model] = _empty_model_entry()
    failed = models[failed_model]
    fpref = failed.setdefault("preferred_for", [])
    favoid = failed.setdefault("avoid_for", [])
    if role not in favoid:
        favoid.append(role)
    if role in fpref:
        fpref.remove(role)
    failed["score"] = max(VOTE_MIN,
                          int(failed.get("score", 0)) - VOTE_INCREMENT)
    failed["rate_limits"] = int(failed.get("rate_limits", 0)) + 1
    failed["runs"] = int(failed.get("runs", 0)) + 1
    failed["lastBumpAt"] = now_ms

    # Boost every other model in the catalog. "Other" = different id.
    boosted = 0
    for mid, entry in models.items():
        if mid == failed_model:
            continue
        epref = entry.setdefault("preferred_for", [])
        eavoid = entry.setdefault("avoid_for", [])
        if role not in epref:
            epref.append(role)
        if role in eavoid:
            eavoid.remove(role)
        entry["score"] = min(VOTE_MAX,
                             int(entry.get("score", 0)) + VOTE_INCREMENT)
        entry["successes"] = int(entry.get("successes", 0)) + 1
        entry["runs"] = int(entry.get("runs", 0)) + 1
        entry["lastBumpAt"] = now_ms
        boosted += 1

    catalog["_meta"]["voteCount"] = (
        int(catalog["_meta"].get("voteCount", 0)) + boosted + 1
    )
    catalog["_meta"]["lastVoteAt"] = now_ms
    save(tier, catalog)
    return boosted


def vote_after_rate_limit_for_provider(provider_type: str | None,
                                       failed_model: str | None,
                                       role: str) -> int:
    """Dispatch the batched 429 vote to the right tier based on the
    provider's ``type`` field. Silent no-op for non-OR providers
    (Ollama / llama.cpp 429s — rare but possible — don't write to the
    OR catalogs).

    Returns the number of models boosted (0 when no-op or empty catalog).
    """
    if not provider_type or not failed_model or not role:
        return 0
    if provider_type == "openrouter-free":
        try:
            return vote_after_rate_limit(TIER_FREE, failed_model, role)
        except Exception:  # noqa: BLE001
            return 0
    if provider_type == "openrouter-paid":
        try:
            return vote_after_rate_limit(TIER_PAID, failed_model, role)
        except Exception:  # noqa: BLE001
            return 0
    return 0
