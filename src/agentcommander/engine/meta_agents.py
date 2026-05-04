"""Preflight + postmortem meta-agents.

Two LLM-driven safety checks that bracket the engine's iteration loop:

* ``apply_preflight(decision, ...)`` runs *after* the orchestrator picks
  an action but *before* the engine dispatches it. The preflight role
  reads matching operational rules + recent scratchpad and returns one
  of three verdicts:

    - ``approve`` — proceed with the original decision (default path).
    - ``reorder`` — execute 1–3 prerequisite actions first, then the
      original. The preflight returns the prereq list; the engine
      injects them into the next iteration as if the orchestrator had
      emitted them.
    - ``abort``  — stop with a user-visible reason (the engine surfaces
      it as a ``done`` event so the run terminates cleanly).

* ``apply_postmortem(...)`` runs *after* a pipeline ends with a non-done
  status (``failed`` / ``cancelled`` / ``max_iterations`` exhaustion).
  The postmortem reads the full transcript and may emit:

    - a generalized ``rule`` that gets persisted to ``operational_rules``
      so future preflights catch the same pattern,
    - a ``retry`` proposal (logged for now; auto-retry is a follow-up),
    - a ``user_prompt`` for a fix that needs human approval (logged).

Both meta-agents are best-effort: if the role is unassigned, the model
output is malformed, or the call errors, they degrade silently to a
no-op so a flaky meta-agent never breaks the main pipeline.

Invariants:
  - Meta-agents are READ-ONLY. They cannot dispatch tools or call other
    roles. The preflight may *propose* prerequisite steps; the engine
    decides whether to honour them.
  - Output is JSON. Output guards run; malformed JSON degrades to
    ``approve`` for preflight and "no-op" for postmortem.
  - Persistence: postmortem-derived rules go in ``operational_rules``
    with ``origin='postmortem'`` so a future ``/rules`` command can
    distinguish them from manually-authored ones.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from agentcommander.db.repos import (
    audit,
    bump_rule_outcome,
    insert_operational_rule,
    list_operational_rules_for_action,
)
from agentcommander.engine.role_call import RoleNotAssigned, call_role
from agentcommander.providers.base import ProviderError
from agentcommander.types import OrchestratorDecision, Role, ScratchpadEntry


# Bumped if the rule schema changes in a backwards-incompatible way; older
# rules with mismatched versions are still listed but their fields may be
# ignored by newer preflights. Kept at 1 until we evolve the shape.
RULE_FINGERPRINT_VERSION: int = 1

# How many trailing scratchpad entries to show the preflight. The role
# prompt (resources/prompts/PREFLIGHT.md) calls out "last 10 entries" —
# matching that here keeps the meta-agent's view consistent with what its
# system prompt promises.
PREFLIGHT_TAIL_ENTRIES: int = 10

# Maximum prereq steps a preflight can inject. The role prompt caps at 3;
# we enforce it on our side so a runaway model can't shove a 50-step plan
# into the pipeline.
PREFLIGHT_MAX_REORDER_STEPS: int = 3


# ─── Preflight ────────────────────────────────────────────────────────────


@dataclass
class PreflightVerdict:
    """Structured result of an ``apply_preflight`` call.

    ``verdict`` is one of: ``"approve"`` (default; proceed unchanged),
    ``"reorder"`` (run ``reorder_steps`` BEFORE the original action,
    then the original), ``"abort"`` (halt the pipeline; surface
    ``reason`` to the user).

    ``rules_consulted`` is the list of rule ids the meta-agent's prompt
    saw — used by postmortem to bump helped/hurt counts.
    """
    verdict: str = "approve"
    reason: str = ""
    reorder_steps: list[OrchestratorDecision] = field(default_factory=list)
    rules_consulted: list[int] = field(default_factory=list)


def _format_rules_for_prompt(rules: list[dict[str, Any]]) -> str:
    """Render matched rules as the prompt's "rules you see" section."""
    if not rules:
        return ""
    lines = ["", "## Matched rules"]
    for r in rules:
        reorder = r.get("suggested_reorder")
        reorder_str = json.dumps(reorder) if reorder else "null"
        lines.append(
            f"[rule #{r['id']} — confidence {r['confidence']:.2f}, "
            f"helped {r['helped_count']} / hurt {r['hurt_count']}]"
        )
        lines.append(f"constraint: {r['constraint_text']}")
        lines.append(f"suggested_reorder: {reorder_str}")
        lines.append("")
    return "\n".join(lines)


def _format_scratchpad_tail(scratchpad: list[ScratchpadEntry], n: int) -> str:
    """Render the last ``n`` entries as plaintext for the preflight prompt.

    Truncates very long input/output fields so the meta-agent's prompt
    stays bounded — without this a single tool call with 50 KB of output
    could push us past the role's context window.
    """
    if not scratchpad:
        return "(empty scratchpad — first iteration)"
    tail = scratchpad[-n:]
    lines: list[str] = []
    for e in tail:
        inp = (e.input or "")[:200]
        out = (e.output or "")[:400]
        prefix = f"step {e.step} {e.role}/{e.action}"
        if inp:
            lines.append(f"{prefix}: in={inp!r} out={out!r}")
        else:
            lines.append(f"{prefix}: out={out!r}")
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    """The role prompt forbids fenced output, but small models cheat. If
    the response is wrapped in ```json … ```, peel it before parsing."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Match ```optional-tag\n...content...\n```
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", s, flags=re.DOTALL)
    return m.group(1).strip() if m else s


def _parse_preflight_json(raw: str) -> dict[str, Any] | None:
    """Coerce model output to a dict. Returns ``None`` if unparseable."""
    s = _strip_code_fences(raw)
    if not s:
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _bump_consulted_rules(rule_ids: list[int], *, helped: bool) -> None:
    """Best-effort bump of helped/hurt counts for the rules a verdict
    leaned on. Failures are audited and swallowed — a transient SQLite
    error must never prevent the verdict from returning.

    Called on ``reorder`` and ``abort`` verdicts where the consulted
    rules visibly informed the decision; the ``approve`` path doesn't
    bump (no decision was made *because of* a rule).
    """
    if not rule_ids:
        return
    for rid in rule_ids:
        try:
            bump_rule_outcome(rid, helped=helped)
        except Exception as exc:  # noqa: BLE001
            audit("preflight.rule_bump_failed",
                  {"rule_id": rid, "error": f"{type(exc).__name__}: {exc}"})


def _decision_from_step(step: dict[str, Any]) -> OrchestratorDecision | None:
    """Build an OrchestratorDecision from a preflight reorder step."""
    if not isinstance(step, dict):
        return None
    action = step.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    return OrchestratorDecision(
        action=action.strip(),
        input=step.get("input") if isinstance(step.get("input"), str) else None,
        reasoning=(step.get("reasoning") if isinstance(step.get("reasoning"), str)
                   else "preflight-injected prerequisite"),
    )


def apply_preflight(
    decision: OrchestratorDecision,
    *,
    scratchpad: list[ScratchpadEntry],
    conversation_id: str | None,
    should_cancel: Callable[[], bool] | None = None,
) -> PreflightVerdict:
    """Run the preflight role. Returns ``PreflightVerdict``.

    Failure modes (all coerced to ``approve`` so the engine keeps moving):
      - The preflight role isn't assigned to a provider/model.
      - The provider call raises (network, rate limit, etc.).
      - Model output is malformed JSON or has an invalid verdict.

    On any of these we audit the failure once and return ``approve`` so a
    flaky meta-agent never blocks the user's run. Hard aborts are
    intentional; silent skips are not.
    """
    # Look up matching rules first — when the rule store is empty (no
    # postmortems have run yet) we still want to invoke preflight, but a
    # caller can configure a min-rules threshold by skipping when empty.
    # We always invoke: even with zero rules, the preflight catches
    # ordering issues from raw scratchpad reasoning.
    try:
        rules = list_operational_rules_for_action(decision.action)
    except Exception as exc:  # noqa: BLE001
        audit("preflight.rules_query_failed",
              {"error": f"{type(exc).__name__}: {exc}"})
        rules = []

    rules_section = _format_rules_for_prompt(rules)
    tail_section = _format_scratchpad_tail(scratchpad, PREFLIGHT_TAIL_ENTRIES)

    user_input = (
        f"## Proposed action\n"
        f"action: {decision.action}\n"
        f"input: {decision.input or ''}\n"
        f"reasoning: {decision.reasoning or ''}\n"
        f"\n"
        f"## Recent scratchpad (last {PREFLIGHT_TAIL_ENTRIES} entries)\n"
        f"{tail_section}"
        f"{rules_section}"
    )

    rule_ids = [r["id"] for r in rules]

    try:
        raw = call_role(
            Role.PREFLIGHT,
            user_input=user_input,
            conversation_id=conversation_id,
            json_mode=True,
            should_cancel=should_cancel,
        )
    except RoleNotAssigned:
        # No preflight role configured — silent skip. This is the common
        # case for users who haven't opted in.
        return PreflightVerdict(verdict="approve",
                                reason="preflight role not assigned",
                                rules_consulted=rule_ids)
    except ProviderError as exc:
        audit("preflight.provider_error",
              {"error": f"{type(exc).__name__}: {exc}"})
        return PreflightVerdict(verdict="approve",
                                reason="preflight provider error (skipped)",
                                rules_consulted=rule_ids)
    except Exception as exc:  # noqa: BLE001
        audit("preflight.unexpected_error",
              {"error": f"{type(exc).__name__}: {exc}"})
        return PreflightVerdict(verdict="approve",
                                reason="preflight crashed (skipped)",
                                rules_consulted=rule_ids)

    obj = _parse_preflight_json(raw)
    if obj is None:
        audit("preflight.malformed_output", {"raw_head": raw[:200]})
        return PreflightVerdict(verdict="approve",
                                reason="preflight output unparseable",
                                rules_consulted=rule_ids)

    verdict = obj.get("verdict")
    reason = obj.get("reason") or ""
    if not isinstance(reason, str):
        reason = str(reason)

    if verdict == "approve":
        return PreflightVerdict(verdict="approve", reason=reason,
                                rules_consulted=rule_ids)

    if verdict == "abort":
        # Abort backed by rules → those rules informed the call. Bump
        # helped so chronically-correct rules drift up in confidence.
        _bump_consulted_rules(rule_ids, helped=True)
        return PreflightVerdict(verdict="abort", reason=reason,
                                rules_consulted=rule_ids)

    if verdict == "reorder":
        raw_steps = obj.get("reorder_steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            # Empty reorder is the prompt's explicit "approve instead"
            # case — but small models hit it anyway. Coerce to approve.
            return PreflightVerdict(verdict="approve",
                                    reason="empty reorder coerced to approve",
                                    rules_consulted=rule_ids)
        steps: list[OrchestratorDecision] = []
        for raw_step in raw_steps[:PREFLIGHT_MAX_REORDER_STEPS]:
            step = _decision_from_step(raw_step)
            if step is not None:
                steps.append(step)
        if not steps:
            return PreflightVerdict(verdict="approve",
                                    reason="reorder steps invalid; approving",
                                    rules_consulted=rule_ids)
        # Reorder backed by rules → bump helped on those rules. Same
        # rationale as abort: the rule's pattern visibly steered the
        # verdict. Successive correct steers raise its confidence in
        # /preflight rules and in the rule store's helped/hurt ratio.
        _bump_consulted_rules(rule_ids, helped=True)
        return PreflightVerdict(verdict="reorder", reason=reason,
                                reorder_steps=steps,
                                rules_consulted=rule_ids)

    # Unknown verdict — be permissive.
    audit("preflight.unknown_verdict", {"verdict": str(verdict)[:50]})
    return PreflightVerdict(verdict="approve",
                            reason=f"unknown verdict {verdict!r}",
                            rules_consulted=rule_ids)


# ─── Postmortem ───────────────────────────────────────────────────────────


@dataclass
class PostmortemResult:
    """What the postmortem extracted from a failed run.

    ``rule_id`` is set when the postmortem persisted a new rule; ``None``
    when no rule was generated. ``retry_proposal`` and ``user_prompt``
    are surfaced to the engine for logging and (eventually) auto-retry /
    user surfacing — currently the engine just audits them.
    """
    rule_id: int | None = None
    retry_proposal: dict[str, Any] | None = None
    user_prompt: dict[str, Any] | None = None
    confidence: float = 0.0
    reason: str = ""


def _format_full_transcript(scratchpad: list[ScratchpadEntry]) -> str:
    """Render every scratchpad entry for the postmortem prompt.

    Unlike preflight (last 10 entries), postmortem needs the full run to
    diagnose. Truncates per-entry to keep the prompt under control: 200
    chars input, 600 chars output. A 50-iteration run with full payloads
    would otherwise bust most context windows.
    """
    if not scratchpad:
        return "(empty scratchpad)"
    lines: list[str] = []
    for e in scratchpad:
        inp = (e.input or "")[:200]
        out = (e.output or "")[:600]
        prefix = f"step {e.step} {e.role}/{e.action}"
        if inp:
            lines.append(f"{prefix}: in={inp!r} out={out!r}")
        else:
            lines.append(f"{prefix}: out={out!r}")
    return "\n".join(lines)


def _persist_rule(
    rule: dict[str, Any], example_run_id: str | None,
) -> int | None:
    """Validate a postmortem-emitted rule and persist it. Returns the
    new rule id, or ``None`` if the rule was malformed."""
    if not isinstance(rule, dict):
        return None
    action_type = rule.get("action_type")
    constraint_text = rule.get("constraint_text")
    if not isinstance(action_type, str) or not action_type.strip():
        return None
    if not isinstance(constraint_text, str) or not constraint_text.strip():
        return None
    target_pattern = rule.get("target_pattern")
    if target_pattern is not None and not isinstance(target_pattern, str):
        target_pattern = None
    tags = rule.get("context_tags")
    if not isinstance(tags, list):
        tags = []
    tags = [t for t in tags if isinstance(t, str)][:8]
    reorder = rule.get("suggested_reorder")
    if not isinstance(reorder, list):
        reorder = None
    confidence = rule.get("confidence")
    try:
        conf_f = float(confidence) if confidence is not None else 0.5
    except (TypeError, ValueError):
        conf_f = 0.5
    conf_f = max(0.0, min(1.0, conf_f))
    try:
        return insert_operational_rule(
            fingerprint_version=RULE_FINGERPRINT_VERSION,
            action_type=action_type.strip(),
            target_pattern=target_pattern,
            context_tags=tags,
            constraint_text=constraint_text.strip(),
            suggested_reorder=reorder,
            origin="postmortem",
            confidence=conf_f,
            example_run_id=example_run_id,
        )
    except Exception as exc:  # noqa: BLE001
        audit("postmortem.rule_persist_failed",
              {"error": f"{type(exc).__name__}: {exc}"})
        return None


def apply_postmortem(
    *,
    run_id: str,
    conversation_id: str | None,
    scratchpad: list[ScratchpadEntry],
    final_status: str,
    error_text: str | None,
    should_cancel: Callable[[], bool] | None = None,
) -> PostmortemResult | None:
    """Run the postmortem role on a failed pipeline. Returns ``None`` when
    the role is unassigned, the call fails, or output is malformed — all
    silent skips. Returns a populated ``PostmortemResult`` otherwise.

    ``final_status`` is one of ``"failed"`` / ``"cancelled"`` /
    ``"max_iterations"`` (mirrors ``pipeline_runs.status``). ``error_text``
    is the engine's surfaced error message, if any.
    """
    transcript = _format_full_transcript(scratchpad)
    user_input = (
        f"## Run state\n"
        f"final_status: {final_status}\n"
        f"error_text: {error_text or '(none)'}\n"
        f"\n"
        f"## Run transcript (chronological)\n"
        f"{transcript}\n"
    )

    try:
        raw = call_role(
            Role.POSTMORTEM,
            user_input=user_input,
            conversation_id=conversation_id,
            json_mode=True,
            should_cancel=should_cancel,
        )
    except RoleNotAssigned:
        return None
    except ProviderError as exc:
        audit("postmortem.provider_error",
              {"error": f"{type(exc).__name__}: {exc}"})
        return None
    except Exception as exc:  # noqa: BLE001
        audit("postmortem.unexpected_error",
              {"error": f"{type(exc).__name__}: {exc}"})
        return None

    obj = _parse_preflight_json(raw)  # same fence-strip + dict-coercion logic
    if obj is None:
        audit("postmortem.malformed_output", {"raw_head": raw[:200]})
        return None

    rule = obj.get("rule") if isinstance(obj.get("rule"), dict) else None
    rule_id = _persist_rule(rule, run_id) if rule else None

    retry = obj.get("retry") if isinstance(obj.get("retry"), dict) else None
    user_prompt = (
        obj.get("user_prompt") if isinstance(obj.get("user_prompt"), dict) else None
    )

    confidence = obj.get("confidence")
    try:
        conf_f = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    conf_f = max(0.0, min(1.0, conf_f))

    reason = obj.get("reason") or ""
    if not isinstance(reason, str):
        reason = str(reason)

    audit("postmortem.applied", {
        "run_id": run_id,
        "rule_id": rule_id,
        "has_retry": retry is not None,
        "has_user_prompt": user_prompt is not None,
        "confidence": conf_f,
        "final_status": final_status,
    })

    return PostmortemResult(
        rule_id=rule_id,
        retry_proposal=retry,
        user_prompt=user_prompt,
        confidence=conf_f,
        reason=reason,
    )
