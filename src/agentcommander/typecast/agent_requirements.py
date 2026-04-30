"""Agent → model capability requirements.

Each role has a profile of what makes a model a good fit. The
``score_match`` function computes a per-(role, model) capability bonus
from OpenRouter catalog metadata. ``pick_for_role`` adds this bonus to
the vote score as a TIE-BREAKER (not a primary signal), so:

  - Vote score drives ranking when there's clear evidence
  - Capability match wins ties between models the user hasn't yet
    differentiated through votes
  - Hard requirements (e.g. ``vision`` role needs a multimodal model)
    filter out unsuitable candidates entirely

The bonus magnitude is intentionally small (≤ 0.5 fractional) so a
single negative vote (-1) overrides any number of capability hints.
The voting signal is the source of truth — capabilities just nudge
the initial untested ranking toward sensible defaults.
"""
from __future__ import annotations

from typing import Any


# Per-role capability profile. Each role lists:
#   - name_hints: substrings in the model id that suggest fitness
#     (e.g. ``"coder"`` for the coder role, ``"research"`` for the
#     researcher role). Each match adds +0.1 to the bonus.
#   - min_ctx: minimum useful context length. Models below this still
#     qualify but get a -0.2 penalty.
#   - needs_json: if True, models supporting response_format get +0.3
#     and models without get -0.3 (router/orchestrator/planner emit
#     strict JSON; non-JSON models hallucinate the format).
#   - needs_modality: hard filter — when set, only models with this
#     modality qualify at all. Used for vision/audio/image_gen.
#
# Bonuses cap around ±0.5 so vote accumulation always dominates.

AGENT_REQUIREMENTS: dict[str, dict[str, Any]] = {
    # JSON-strict decision roles — need response_format support
    "router":       {"needs_json": True, "min_ctx": 4096},
    "orchestrator": {"needs_json": True, "min_ctx": 16384},
    "planner":      {"needs_json": True, "min_ctx": 8192},

    # Code-focused roles — prefer models with code/coder/instruct hints
    "coder":        {"name_hints": ["coder", "code", "codestral"],
                     "min_ctx": 8192},
    "reviewer":     {"name_hints": ["instruct", "chat", "review"],
                     "min_ctx": 8192},
    "tester":       {"name_hints": ["code", "instruct"],
                     "min_ctx": 8192},
    "debugger":     {"name_hints": ["code", "instruct", "debug"],
                     "min_ctx": 8192},
    "refactorer":   {"name_hints": ["code", "instruct", "refactor"],
                     "min_ctx": 8192},

    # Long-context / reasoning roles — researcher needs to chew through
    # multiple sources; summarizer needs to compress big inputs.
    "researcher":   {"min_ctx": 65536,
                     "name_hints": ["research", "deep"]},
    "summarizer":   {"min_ctx": 32768,
                     "name_hints": ["summar", "instruct"]},

    # Specialized text roles
    "translator":   {"name_hints": ["multilingual", "polyglot", "translate"],
                     "min_ctx": 4096},
    "data_analyst": {"name_hints": ["analyst", "data", "instruct"],
                     "min_ctx": 16384},
    "architect":    {"name_hints": ["instruct", "architect"],
                     "min_ctx": 16384},
    "critic":       {"name_hints": ["instruct", "critic"],
                     "min_ctx": 8192},

    # Meta roles — small context, no JSON requirement
    "preflight":    {"min_ctx": 4096},
    "postmortem":   {"min_ctx": 8192},

    # Hard-modality roles — only multimodal models qualify
    "vision":       {"needs_modality": "image"},
    "audio":        {"needs_modality": "audio"},
    "image_gen":    {"needs_modality": "image_gen"},
}


def _has_modality(entry: dict[str, Any], wanted: str) -> bool:
    """Loose modality match. OpenRouter exposes the field as either a
    single string ("text", "multimodal", "image+text") or a list. We
    just look for the wanted token anywhere in the lowercased value.
    """
    mod = entry.get("modality")
    if mod is None:
        return False
    if isinstance(mod, list):
        joined = " ".join(str(m).lower() for m in mod)
    else:
        joined = str(mod).lower()
    return wanted.lower() in joined


def _supports_json(entry: dict[str, Any]) -> bool:
    """True when the model's supported_params include response_format
    (the OpenAI-compat way to request strict JSON output)."""
    params = entry.get("supported_params") or []
    if not isinstance(params, list):
        return False
    return any(str(p).lower() == "response_format" for p in params)


def is_eligible(role: str, model_id: str, entry: dict[str, Any]) -> bool:
    """Hard filter: returns False for models that can't physically
    handle this role (modality mismatch). Soft mismatches (low ctx,
    no JSON support) are penalized in ``score_match`` but stay
    eligible — the vote signal can still pull them up."""
    req = AGENT_REQUIREMENTS.get(role.lower(), {})
    needs_modality = req.get("needs_modality")
    if needs_modality and not _has_modality(entry, needs_modality):
        return False
    return True


def score_match(role: str, model_id: str, entry: dict[str, Any]) -> float:
    """Compute the capability-bonus for ``(role, model)``.

    Returns a float in roughly [-0.5, +0.5]. Added to the vote score
    in ``pick_for_role`` as a tie-breaker, so accumulated votes always
    dominate but capability sense breaks initial ties toward sensible
    defaults.
    """
    req = AGENT_REQUIREMENTS.get(role.lower(), {})
    if not req:
        return 0.0
    bonus = 0.0
    name_lower = (model_id or "").lower()

    # Name hints: +0.1 per match, capped at +0.3
    hints = req.get("name_hints") or []
    n_matches = sum(1 for h in hints if h.lower() in name_lower)
    bonus += min(0.3, 0.1 * n_matches)

    # JSON requirement: +0.3 bonus when explicitly declared. NO penalty
    # for absence — OpenRouter's /v1/models returns ``supported_parameters: []``
    # for almost every free model regardless of what the model actually
    # accepts, so penalizing missing-field would unfairly bury everyone.
    # The bonus still rewards models that DO declare it; absence stays
    # neutral until a real run tells us otherwise (vote signal handles that).
    if req.get("needs_json") and _supports_json(entry):
        bonus += 0.3

    # Min ctx: -0.2 if model is below the threshold
    min_ctx = req.get("min_ctx")
    if isinstance(min_ctx, int) and min_ctx > 0:
        ctx = entry.get("contextLength")
        if isinstance(ctx, int) and ctx < min_ctx:
            bonus -= 0.2

    return bonus
