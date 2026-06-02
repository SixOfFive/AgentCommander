"""JSON-Schema generator for the orchestrator's decision object.

This is the heart of *schema-constrained decoding*. Modern local runtimes
constrain generation to a JSON Schema at the sampler level:

  - **Ollama** (0.5+) accepts a full JSON Schema in the request's ``format``
    field and converts it to a GBNF grammar internally, so the model
    *physically cannot* emit an invalid ``action`` verb or a malformed shape.
  - **llama.cpp** server accepts the same schema via
    ``response_format: {"type": "json_schema", ...}`` (also GBNF under the hood).

Before this module, the engine sent ``format: "json"`` — which forces
*syntactically valid* JSON but says nothing about *shape*. The model could
still emit ``{"action": "search"}`` (a verb no tool implements) or
``{"foo": "bar"}``. A whole family of guards existed to catch those
schema-level violations *after* generation: ``unknown_action_guard``,
``sentence_as_action_guard``, ``empty_action_guard``, large parts of
``field_swap_guard`` / ``missing_fields_guard``. Constraining ``action`` to
the real verb enum at decode time removes the failure at the source.

Design choice — **flat, and grammar-cheap.** The schema constrains:
  - ``action`` to the exact enum of registered verbs (``ALL_ACTIONS``) — the
    constraint that matters, since it makes phantom verbs impossible,
  - every property to the type ``OrchestratorDecision`` actually consumes
    (so the model is *guided* to fill e.g. ``steps`` for a ``fan_out``).

It deliberately does **NOT** set ``additionalProperties: false`` and does
**not** encode per-action required fields via ``if/then/else`` / ``oneOf``.
Both make the generated GBNF grammar far more expensive: a closed-set object
with ~18 optional properties forces an "any-subset-in-any-order, nothing
else" grammar that slowed constrained decoding on qwen2.5:14b from ~12s to
~134s for the same 400-char decision (measured 2026-06-02, live on BEAST).
The closed set bought nothing anyway — ``from_dict`` already drops unknown
keys. Leaving ``additionalProperties`` unset keeps decoding fast while still
guiding the model via the typed property list. ``missing_fields_guard``
remains the cheap backstop for "fetch needs a url". Promoting to strict
per-action schemas is tracked in ROADMAP.md (#1b) only if a future strong
orchestrator makes the grammar cost acceptable.

The property set is derived from ``OrchestratorDecision``'s dataclass fields,
so the schema can never silently drift from what ``from_dict`` consumes.
``tests/test_decision_schema.py`` asserts this equivalence.

Pure stdlib: the schema is a plain ``dict`` placed in the request body.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields
from typing import Any

from agentcommander.engine.actions import ALL_ACTIONS
from agentcommander.types import OrchestratorDecision

# Field name → JSON-Schema type for the non-string fields of
# OrchestratorDecision. Anything not listed here defaults to "string"
# (the overwhelming majority — url/path/content/reasoning/input/etc.).
_NON_STRING_FIELD_TYPES: dict[str, dict[str, Any]] = {
    "port": {"type": "integer"},
    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
    "steps": {"type": "array", "items": {"type": "object"}},
}


def _property_schema(field_name: str) -> dict[str, Any]:
    """JSON-Schema fragment for one OrchestratorDecision field."""
    if field_name == "action":
        # The constraint that matters most: action must be a real verb.
        return {"type": "string", "enum": sorted(ALL_ACTIONS)}
    override = _NON_STRING_FIELD_TYPES.get(field_name)
    if override is not None:
        return dict(override)
    return {"type": "string"}


def orchestrator_decision_field_names() -> list[str]:
    """The exact field set consumed by OrchestratorDecision.from_dict.

    Derived from the dataclass so the schema and the parser can't drift.
    """
    return [f.name for f in dataclass_fields(OrchestratorDecision)]


def orchestrator_decision_schema() -> dict[str, Any]:
    """Build the JSON Schema constraining the orchestrator's decision.

    Returns a fresh dict on every call (callers may mutate / wrap it).
    """
    properties: dict[str, Any] = {
        name: _property_schema(name) for name in orchestrator_decision_field_names()
    }
    return {
        "type": "object",
        "properties": properties,
        # Only `action` is structurally required. Per-action field
        # requirements stay with missing_fields_guard (see module docstring).
        "required": ["action"],
        # NOTE: intentionally NO "additionalProperties": false — it makes the
        # GBNF grammar an order of magnitude more expensive to decode (see
        # module docstring) for zero correctness benefit (from_dict drops
        # unknown keys). The typed `properties` above still guide generation.
    }
