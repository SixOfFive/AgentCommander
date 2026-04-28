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
