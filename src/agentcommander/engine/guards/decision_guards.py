"""Decision guards — validate orchestrator decisions before dispatch.

Run after JSON parsing but BEFORE the action dispatch switch. Catch
malformed decisions, missing required fields, disabled tools, and common
decision-level mistakes that would waste iterations.

Ported from EngineCommander/src/main/orchestration/guards/decision-guards.ts.
"""
from __future__ import annotations

import re
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.types import OrchestratorDecision, ScratchpadEntry


_BROWSER_ACTIONS: frozenset[str] = frozenset({
    "browse", "screenshot", "click", "type_text", "extract_text", "evaluate_js",
})


def empty_action_guard(decision: OrchestratorDecision,
                       scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    if not decision.action or not decision.action.strip():
        push_system_nudge(scratchpad, iteration, "empty_action",
                          'BLOCKED: decision has no "action" field. You MUST specify an action '
                          "(execute, write_file, fetch, read_file, plan, code, done, etc.).")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def sentence_as_action_guard(decision: OrchestratorDecision,
                             scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    action = decision.action or ""
    if " " in action and len(action.split()) > 3:
        action_words = [
            "execute", "write_file", "read_file", "fetch", "browse", "search",
            "list_dir", "delete_file", "plan", "code", "review", "done",
            "git", "http_request", "screenshot", "vision", "debug", "test",
            "architect", "start_process", "kill_process", "check_process",
        ]
        found = next((a for a in action_words if a in action.lower()), None)
        if found:
            decision.action = found
            if not decision.reasoning:
                decision.reasoning = action
            return GuardVerdict(action="pass")
        push_system_nudge(scratchpad, iteration, "sentence_as_action",
                          f'BLOCKED: "action" must be a single tool name, not a sentence. '
                          f'You wrote: "{action[:100]}". Put the tool name in "action" and '
                          f'your explanation in "reasoning".')
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def missing_fields_guard(decision: OrchestratorDecision,
                          scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    action = decision.action

    def _stringify(v: Any) -> str:
        if isinstance(v, str):
            return v
        if v is None:
            return ""
        try:
            import json
            return json.dumps(v)
        except (TypeError, ValueError):
            return str(v)

    input_str = _stringify(decision.input)
    content_str = _stringify(decision.content)
    reasoning_str = decision.reasoning or ""

    if action == "execute" and not input_str.strip():
        if content_str.strip():
            decision.input = content_str
        elif reasoning_str and re.search(r"\b(import|def|print|for|while|if)\b", reasoning_str):
            decision.input = reasoning_str
            decision.reasoning = "Code was in reasoning field — auto-moved"
        else:
            push_system_nudge(scratchpad, iteration, "missing_execute_code",
                              'BLOCKED: execute action requires code in "input".')
            return GuardVerdict(action="continue")

    if action == "write_file" and not decision.path and not decision.input:
        push_system_nudge(scratchpad, iteration, "missing_write_path",
                          'BLOCKED: write_file requires a "path" field.')
        return GuardVerdict(action="continue")

    if action in ("fetch", "browse", "http_request") and not decision.url and not decision.input:
        push_system_nudge(scratchpad, iteration, "missing_url",
                          f'BLOCKED: {action} requires a URL in "url" or "input".')
        return GuardVerdict(action="continue")

    return GuardVerdict(action="pass")


def malformed_url_guard(decision: OrchestratorDecision,
                        scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    if decision.action not in ("fetch", "browse", "http_request"):
        return GuardVerdict(action="pass")
    raw = decision.url or decision.input or ""
    if not isinstance(raw, str):
        return GuardVerdict(action="pass")

    url = raw
    if url and not url.startswith(("http://", "https://", "file://")):
        if "." in url and " " not in url:
            url = "https://" + url
    url = re.sub(r"^htps://", "https://", url, flags=re.IGNORECASE)
    url = re.sub(r"^htpp://", "http://", url, flags=re.IGNORECASE)
    url = re.sub(r"^htp://", "http://", url, flags=re.IGNORECASE)
    url = url.strip()

    if url.startswith("file://"):
        push_system_nudge(scratchpad, iteration, "file_url_blocked",
                          "BLOCKED: file:// URLs are not supported. "
                          "Use read_file to read local files.")
        return GuardVerdict(action="continue")

    if re.search(r"//[^/]*:[^/]*@", url):
        url = re.sub(r"(//)[^/]*:[^/]*@", r"\1", url)

    if decision.url:
        decision.url = url
    else:
        decision.input = url
    return GuardVerdict(action="pass")


def disabled_browser_guard(decision: OrchestratorDecision,
                           scratchpad: list[ScratchpadEntry], iteration: int,
                           browser_available: bool) -> GuardVerdict:
    if decision.action not in _BROWSER_ACTIONS or browser_available:
        return GuardVerdict(action="pass")
    if decision.action in ("browse", "screenshot"):
        decision.action = "fetch"
        return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration, "browser_disabled",
                      f'BLOCKED: browser action "{decision.action}" is not available. '
                      f"Use fetch for plain HTML / JSON content.")
    return GuardVerdict(action="continue")


def field_swap_guard(decision: OrchestratorDecision,
                     _scratchpad: list[ScratchpadEntry],
                     _iteration: int) -> GuardVerdict:
    reasoning = decision.reasoning or ""
    if len(reasoning) <= 100 or (decision.input and len(decision.input) >= 20):
        return GuardVerdict(action="pass")
    code_signals = [
        bool(re.match(r"^(import|from|def|class|function|const|let|var|#!)\b", reasoning, re.MULTILINE)),
        bool(re.search(r"\b(print|console\.log|return|if.*:$|for.*in.*:)", reasoning, re.MULTILINE)),
        bool(re.search(r"[{}\[\]();]=>", reasoning)),
    ]
    if sum(code_signals) >= 2 and decision.action in ("execute", "write_file"):
        decision.input = reasoning
        decision.reasoning = "(code moved from reasoning to input by guard)"
    return GuardVerdict(action="pass")


def delete_new_file_guard(decision: OrchestratorDecision,
                          scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    if decision.action != "delete_file":
        return GuardVerdict(action="pass")
    target = decision.path or decision.input or ""
    was_just_created = any(
        e.action == "write_file" and e.input == target
        and "Successfully" in (e.output or "")
        for e in scratchpad
    )
    was_not_failed = not any(
        e.action == "execute" and target in (e.output or "") and "Error" in (e.output or "")
        for e in scratchpad
    )
    if was_just_created and was_not_failed:
        push_system_nudge(scratchpad, iteration, "delete_new_file",
                          f'WARNING: you just created "{target}" and now want to delete it '
                          f"without it causing any errors. If you need to rewrite, use write_file. "
                          f"If you truly need to delete, explain why in reasoning.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def templating_placeholder_guard(decision: OrchestratorDecision,
                                  scratchpad: list[ScratchpadEntry],
                                  iteration: int) -> GuardVerdict:
    if decision.action != "execute":
        return GuardVerdict(action="pass")
    code = decision.input or ""
    m = re.search(r"\{\{\s*([a-zA-Z_][\w\s.\-]*?)\s*\}\}", code)
    if not m:
        return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration, "templating_placeholder",
                      f"SYSTEM: your execute code contains a templating placeholder "
                      f"({m.group(0)}). AgentCommander does NOT render templates — that "
                      f"string reaches Python verbatim and crashes. Either embed the value "
                      f"literally, or split into two actions (one to parse the prior output, "
                      f"one to use it). Retry without placeholder syntax.")
    return GuardVerdict(action="continue")


# ─── Action-verb validation ────────────────────────────────────────────────
#
# Round-22 model-flow testing surfaced cases where the orchestrator emitted
# action verbs that don't exist in the dispatcher (e.g. ``send_email``,
# ``query_database``, ``call_api``). The dispatcher returned "no such tool"
# as a tool failure, which polluted the scratchpad and burned an iteration
# without surfacing the real problem to the model in actionable terms. This
# guard rejects unknown verbs at decision time with a system_nudge that
# lists the actual registered actions, giving the orchestrator a concrete
# correction path on the next iteration.

# Special action verbs that the engine handles directly (not in any registry).
_SPECIAL_ACTIONS: frozenset[str] = frozenset({"done", "chat", "delegate"})


def _all_known_actions() -> frozenset[str]:
    """Snapshot every legal action verb at the current moment.

    Sources:
      - ROLE_ACTIONS (engine.actions) — verbs that dispatch to a Role
      - TOOL_ACTIONS (engine.actions) — verbs the tools.dispatcher knows
      - _BROWSER_ACTIONS — opt-in browser verbs
      - _SPECIAL_ACTIONS — done / chat / delegate

    Computed lazily so test-time tool registration changes are visible.
    """
    from agentcommander.engine.actions import ROLE_ACTIONS, TOOL_ACTIONS
    return ROLE_ACTIONS | TOOL_ACTIONS | _BROWSER_ACTIONS | _SPECIAL_ACTIONS


def unknown_action_guard(decision: OrchestratorDecision,
                          scratchpad: list[ScratchpadEntry],
                          iteration: int) -> GuardVerdict:
    """Reject orchestrator decisions whose ``action`` isn't a known verb.

    Why this exists: when the model invents a verb (``send_email``,
    ``query_database``, ``analyze_image``) the dispatcher records a
    ``no such tool`` failure that then shows up in scratchpad as if the
    user actually attempted that action. The orchestrator then keeps
    retrying minor variations. Catching it pre-dispatch gives the model
    one targeted nudge with the actual menu of options.

    Coexists with ``sentence_as_action_guard`` — that one converts
    multi-word actions into the embedded verb if any. By the time we
    reach this guard the action is a single token; we just need to
    confirm that token is real.
    """
    action = (decision.action or "").strip().lower()
    if not action:
        return GuardVerdict(action="pass")  # empty-action handles this
    known = _all_known_actions()
    if action in known:
        return GuardVerdict(action="pass")
    # Whitelist a few common synonyms the model might emit. Cheaper than
    # nudging the model when the verb is obviously equivalent to a known
    # one — we just rewrite the action and continue.
    synonyms = {
        "list_files": "list_dir",
        "ls": "list_dir",
        "cat": "read_file",
        "create_file": "write_file",
        "save_file": "write_file",
        "run": "execute",
        "shell": "execute",
        "bash": "execute",
        "curl": "fetch",
        "get": "fetch",
        "http": "http_request",
        "post": "http_request",
    }
    if action in synonyms:
        decision.action = synonyms[action]
        return GuardVerdict(action="pass")
    # Truly unknown — nudge the model with the real menu.
    sample = sorted(known - _SPECIAL_ACTIONS)
    # Trim to keep the nudge readable; the model just needs the gist.
    sample_str = ", ".join(sample[:24])
    if len(sample) > 24:
        sample_str += f", … ({len(sample) - 24} more)"
    push_system_nudge(scratchpad, iteration, "unknown_action",
                      f'BLOCKED: action="{decision.action}" is not a registered '
                      f"verb. Pick one of: {sample_str}. If the user wants "
                      f"something none of these can do, use action=\"done\" "
                      f"and explain in input.")
    return GuardVerdict(action="continue")


# ─── Role-assignment check ─────────────────────────────────────────────────


def unassigned_role_guard(decision: OrchestratorDecision,
                           scratchpad: list[ScratchpadEntry],
                           iteration: int) -> GuardVerdict:
    """Catch ``action=<role>`` when that role has no provider/model bound.

    Round-22 stress: with an opt-in role like ``vision`` unassigned, the
    orchestrator could still emit ``{"action": "vision", ...}`` and the
    engine would raise ``RoleNotAssigned`` mid-dispatch — the run died
    with a stack trace instead of converting the failure into a nudge.
    This guard checks role resolvability *before* dispatch.
    """
    from agentcommander.engine.actions import ACTION_TO_ROLE
    from agentcommander.engine.role_resolver import resolve as resolve_role
    action = (decision.action or "").strip().lower()
    role = ACTION_TO_ROLE.get(action)
    if role is None:
        return GuardVerdict(action="pass")  # not a role action
    try:
        resolved = resolve_role(role)
    except Exception:  # noqa: BLE001 — defensive, any resolver failure
        resolved = None
    if resolved is not None:
        return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration, "unassigned_role",
                      f'BLOCKED: action="{decision.action}" delegates to the '
                      f'{role.value} role, but no provider/model is assigned '
                      f"to it (the user hasn't run /roles set or autoconfig "
                      f"didn't pick one). Either pick a different action, "
                      f'or use action="done" with the answer you can give '
                      f"directly.")
    return GuardVerdict(action="continue")


# ─── Runner ────────────────────────────────────────────────────────────────


def run_decision_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run decision guards in sequence. Returns the first non-pass verdict.

    ctx: {decision, scratchpad, iteration, user_message, browser_available}
    Returns: {decision, verdict: {action, final_output?}}.
    """
    decision: OrchestratorDecision = ctx["decision"]
    scratchpad: list[ScratchpadEntry] = ctx["scratchpad"]
    iteration: int = ctx["iteration"]
    browser_available: bool = bool(ctx.get("browser_available", False))

    guards = [
        lambda: empty_action_guard(decision, scratchpad, iteration),
        lambda: sentence_as_action_guard(decision, scratchpad, iteration),
        # unknown_action_guard runs AFTER sentence_as_action so the
        # synonym-rewrite-or-extract logic gets first crack at the verb.
        lambda: unknown_action_guard(decision, scratchpad, iteration),
        lambda: unassigned_role_guard(decision, scratchpad, iteration),
        lambda: field_swap_guard(decision, scratchpad, iteration),
        lambda: missing_fields_guard(decision, scratchpad, iteration),
        lambda: malformed_url_guard(decision, scratchpad, iteration),
        lambda: templating_placeholder_guard(decision, scratchpad, iteration),
        lambda: disabled_browser_guard(decision, scratchpad, iteration, browser_available),
        lambda: delete_new_file_guard(decision, scratchpad, iteration),
    ]
    for guard in guards:
        verdict = guard()
        if verdict.action == "continue":
            return {"decision": decision,
                    "verdict": {"action": "continue", "final_output": None}}
    return {"decision": decision, "verdict": {"action": "pass", "final_output": None}}
