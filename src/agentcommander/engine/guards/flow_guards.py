"""Flow guards — run before dispatching any action.

Cross-cutting caps and pattern-detection. Prevent re-planning, cap repeated
tool calls, detect oscillation, force completion after too many consecutive
nudges.

Ported from EngineCommander/src/main/orchestration/guards/flow-guards.ts.
"""
from __future__ import annotations

import re
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.engine.scratchpad import build_final_output
from agentcommander.types import OrchestratorDecision, ScratchpadEntry


def _result(action: str, *, plan_call_count: int, consecutive_nudges: int,
            final_output: str | None = None) -> dict[str, Any]:
    return {
        "verdict": {"action": action, "final_output": final_output},
        "plan_call_count": plan_call_count,
        "consecutive_nudges": consecutive_nudges,
    }


def plan_redirect_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                         decision: OrchestratorDecision, plan_call_count: int,
                         consecutive_nudges: int) -> dict[str, Any]:
    if decision.action != "plan":
        return _result("pass", plan_call_count=plan_call_count,
                       consecutive_nudges=consecutive_nudges)
    plan_call_count += 1
    if plan_call_count > 2:
        push_system_nudge(scratchpad, iteration, "plan_blocked",
                          "BLOCKED: planning called too many times. Use write_file/execute to implement.")
        return _result("continue", plan_call_count=plan_call_count,
                       consecutive_nudges=consecutive_nudges)
    if plan_call_count > 1:
        plan_output = next(
            (e.output for e in reversed(scratchpad) if e.role == "planner"),
            "",
        )
        has_code_here = any(
            e.role == "coder" or
            (e.action == "write_file" and re.search(r"\.(py|js|ts|sh|rb)$", e.input or "", re.IGNORECASE))
            for e in scratchpad
        )
        if not has_code_here:
            decision.action = "code"
            decision.reasoning = "Plan already exists — executing it now"
            decision.input = (
                f"A plan already exists. Write the COMPLETE code for it now. "
                f"Output ALL files with full code:\n\n{plan_output}"
            )
        else:
            last = scratchpad[-1] if scratchpad else None
            if last and last.role == "coder":
                decision.action = "review"
                decision.reasoning = "Code written — reviewing before continuing"
                decision.input = f"Review this code:\n\n{last.output}"
    return _result("pass", plan_call_count=plan_call_count,
                   consecutive_nudges=consecutive_nudges)


def repeated_tool_call_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                              decision: OrchestratorDecision, tool_call_counts: dict[str, int],
                              plan_call_count: int, consecutive_nudges: int) -> dict[str, Any]:
    """Cap repeated tool calls to the same target and overall.

    Per-action caps tuned by what's idempotent vs side-effecting:
      - read-only verbs (list_dir, fetch, read_file, search): 5 total
      - write/execute verbs: 8 total — they often need retries with
        different content, but 8+ on a single tool in one turn is
        almost always a stuck-loop (round-23 caught a write_file × 8
        loop on greet23.py).
    """
    capped = {
        "list_dir": 5, "fetch": 5, "read_file": 5, "search": 5,
        "write_file": 8, "execute": 8,
    }
    cap = capped.get(decision.action)
    if cap is None:
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)

    target = decision.path or decision.url or decision.input or ""
    consecutive_same = 0
    for e in reversed(scratchpad):
        if e.action == decision.action and (e.input == target or e.input == (decision.path or "")):
            consecutive_same += 1
        else:
            break
    if consecutive_same >= 2:
        push_system_nudge(scratchpad, iteration, decision.action,
                          f'STOP: "{decision.action}" called {consecutive_same} times for the '
                          f"same target. The result hasn't changed. Move on.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)

    total = tool_call_counts.get(decision.action, 0) + 1
    tool_call_counts[decision.action] = total
    if total > cap:
        push_system_nudge(scratchpad, iteration, decision.action,
                          f'LIMIT: "{decision.action}" has been called {total} times total. '
                          f"Use what you have and proceed.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def oscillation_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                       decision: OrchestratorDecision, plan_call_count: int,
                       consecutive_nudges: int) -> dict[str, Any]:
    if decision.action not in ("write_file", "execute"):
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    recent = scratchpad[-8:]
    writes = [e for e in recent if e.action == "write_file"]
    fails = [
        e for e in recent
        if e.action == "execute" and e.output
        and ("Error" in e.output or "Traceback" in e.output)
    ]
    if len(writes) >= 2 and len(fails) >= 2:
        types = [re.search(r"(\w+Error)", f.output or "") for f in fails]
        type_strs = [m.group(1) for m in types if m]
        all_same = type_strs and len(set(type_strs)) == 1
        if all_same:
            err_type = type_strs[0]
            push_system_nudge(scratchpad, iteration, "oscillation_detected",
                              f"STOP: stuck in a write→execute→fail loop. The same {err_type} "
                              f"keeps occurring. Do NOT rewrite the same file. Use debug, "
                              f"try a different approach, or simplify.")
            return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def rapid_rewrite_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                         decision: OrchestratorDecision, plan_call_count: int,
                         consecutive_nudges: int) -> dict[str, Any]:
    if decision.action != "write_file":
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    target = decision.path or decision.input or ""
    recent = scratchpad[-2:]
    just_wrote = any(
        e.action == "write_file" and e.input == target
        and "Successfully" in (e.output or "")
        for e in recent
    )
    no_exec_between = not any(
        e.action == "execute" or e.role in ("debugger", "coder", "reviewer") for e in recent
    )
    if just_wrote and no_exec_between:
        push_system_nudge(scratchpad, iteration, "rapid_rewrite",
                          f'WARNING: you just wrote "{target}" and are writing it again '
                          f"without testing. Execute it first.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def overkill_delegation_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                                decision: OrchestratorDecision, plan_call_count: int,
                                consecutive_nudges: int, user_message: str) -> dict[str, Any]:
    if decision.action not in ("plan", "architect"):
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    prior_nudge = any(
        e.role == "tool" and e.action == "system_nudge" and e.input == "overkill_delegation"
        for e in scratchpad
    )
    if prior_nudge:
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    route_text = ""
    for e in scratchpad:
        if e.role == "router":
            route_text = (e.output or "").lower()
            break
    is_simple_category = "chat" in route_text or "question" in route_text
    is_short = len(user_message) < 300
    if is_simple_category or is_short:
        reason = (f'a simple {"chat" if "chat" in route_text else "question"} task'
                  if is_simple_category
                  else f"a short task ({len(user_message)} chars) — {decision.action} adds overhead")
        push_system_nudge(scratchpad, iteration, "overkill_delegation",
                          f"OVERKILL: {decision.action} is not needed for {reason}. "
                          f"Use direct tool actions and then done. Keep it simple.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def repeated_read_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                         decision: OrchestratorDecision, plan_call_count: int,
                         consecutive_nudges: int) -> dict[str, Any]:
    if decision.action != "read_file":
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    target = decision.path or decision.input or ""
    reads = [e for e in scratchpad if e.action == "read_file" and e.input == target]
    last_write = next(
        (e for e in reversed(scratchpad) if e.action == "write_file" and e.input == target),
        None,
    )
    last_write_idx = scratchpad.index(last_write) if last_write else -1
    reads_after = [r for r in reads if scratchpad.index(r) > last_write_idx]
    if len(reads_after) >= 3:
        push_system_nudge(scratchpad, iteration, "repeated_read",
                          f'LIMIT: you have read "{target}" {len(reads_after)} times '
                          f"without modifying it. Use the information you already have.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def broad_search_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                        decision: OrchestratorDecision, plan_call_count: int,
                        consecutive_nudges: int) -> dict[str, Any]:
    if decision.action != "search":
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    pattern = decision.input or decision.pattern or ""
    if len(pattern) <= 2 and not re.match(r"^[.^$]", pattern):
        push_system_nudge(scratchpad, iteration, "broad_search",
                          f'WARNING: search pattern "{pattern}" is too short. '
                          f"Use a more specific pattern.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def stale_progress_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                          plan_call_count: int, consecutive_nudges: int) -> dict[str, Any]:
    if iteration < 20 or iteration % 10 != 0:
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    tool_steps = [e for e in scratchpad if e.role == "tool" and e.action != "system_nudge"]
    success_steps = [
        e for e in tool_steps
        if "successfully" in (e.output or "").lower()
        or (e.action == "fetch" and len(e.output or "") > 200)
    ]
    if not success_steps and len(tool_steps) >= 5:
        push_system_nudge(scratchpad, iteration, "stale_progress",
                          f"WARNING: {iteration} iterations completed with no successful "
                          f"results. Simplify your approach, try a different strategy, "
                          f"or call done with an explanation.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def role_spam_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                     decision: OrchestratorDecision, plan_call_count: int,
                     consecutive_nudges: int) -> dict[str, Any]:
    role_actions = {"plan", "code", "review", "architect", "critique",
                    "test", "debug", "summarize"}
    if decision.action not in role_actions:
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    consecutive_same = 0
    for e in reversed(scratchpad):
        role_name = e.role
        # crude action→role mapping
        target = (
            decision.action
            if role_name == decision.action else
            ("coder" if decision.action == "code" else
             "critic" if decision.action == "critique" else
             decision.action + "r")
        )
        if role_name == target:
            consecutive_same += 1
        elif e.action != "system_nudge":
            break
    if consecutive_same >= 4:
        push_system_nudge(scratchpad, iteration, "role_spam",
                          f'STOP: "{decision.action}" agent called {consecutive_same} times '
                          f"in a row without using its output. Try a different action.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def debugger_quota_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                          decision: OrchestratorDecision, plan_call_count: int,
                          consecutive_nudges: int) -> dict[str, Any]:
    if decision.action != "debug":
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)

    def sig(s: str) -> str:
        m = re.search(r"([A-Z][a-zA-Z]*(?:Error|Exception)):\s*(.+?)(?:\n|$)", s)
        if not m:
            return ""
        normalized = m.group(0)
        normalized = re.sub(r"line\s+\d+", "line N", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r":\d+:\d+", ":N:N", normalized)
        return normalized[:200]

    sigs_after_debug: list[str] = []
    for i, e in enumerate(scratchpad):
        if e.action == "debug":
            for j in range(i + 1, min(i + 3, len(scratchpad))):
                s = sig(scratchpad[j].output or "")
                if s:
                    sigs_after_debug.append(s)
                    break

    counts: dict[str, int] = {}
    for s in sigs_after_debug:
        counts[s] = counts.get(s, 0) + 1
    stuck = next(((s, c) for s, c in counts.items() if c >= 2), None)
    if stuck:
        sig_text, count = stuck
        push_system_nudge(scratchpad, iteration, "debug_quota",
                          f'BLOCKED: "debug" called {count} times and the error is still '
                          f'"{sig_text[:120]}". The debugger\'s fixes are NOT working. '
                          f"Choose: (a) rewrite from scratch with a different approach; "
                          f"(b) make the code tolerant of the failure; "
                          f'(c) call done with an explanation.')
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def fetch_retry_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                       decision: OrchestratorDecision, plan_call_count: int,
                       consecutive_nudges: int) -> dict[str, Any]:
    """Block repeated retries of the same URL across fetch/http_request.

    Round-23 caught a 26-iteration loop on https://httpbin.org/status/200
    where the orchestrator alternated between ``fetch`` and
    ``http_request`` after each failure. The previous version only
    counted the CURRENT action's failures, so each switch reset the
    count and the cap never bit. We now count failures across both
    verbs for the same URL — the cap is about the URL being broken,
    not about which verb tried it.
    """
    if decision.action not in ("fetch", "http_request"):
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    url = decision.url or decision.input or ""
    # Count failures for this URL across BOTH verbs. The orchestrator
    # treating fetch and http_request as alternatives doesn't change the
    # fact that the URL is broken from our side.
    prev = [e for e in scratchpad
            if e.action in ("fetch", "http_request") and e.input == url]
    failed = [e for e in prev
              if any(m in (e.output or "")
                     for m in ("Error", "failed", "SSRF", "Rejected",
                               "404", "403", "500", "Connection",
                               "timed out", "Timeout"))]
    if len(failed) >= 2:
        push_system_nudge(scratchpad, iteration, "fetch_retry_blocked",
                          f'BLOCKED: "{url[:100]}" has failed {len(failed)} times '
                          f"across fetch/http_request. Do NOT retry the same "
                          f"URL — switching verbs won't help. Try a different "
                          f"URL or use action=\"done\" to report the failure.")
        return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def protected_directory_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                                decision: OrchestratorDecision, plan_call_count: int,
                                consecutive_nudges: int) -> dict[str, Any]:
    if decision.action not in ("write_file", "delete_file"):
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    path = decision.path or decision.input or ""
    protected = [r"^node_modules/", r"^\.git/", r"^__pycache__/", r"^\.venv/"]
    for p in protected:
        if re.match(p, path, re.IGNORECASE):
            top = path.split("/", 1)[0]
            push_system_nudge(scratchpad, iteration, "protected_directory",
                              f'BLOCKED: cannot {decision.action} in "{path}" — protected '
                              f'directory ({top}). Write under the working directory.')
            return _result("continue", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


def consecutive_nudge_guard(scratchpad: list[ScratchpadEntry],
                              plan_call_count: int, consecutive_nudges: int,
                              turn_start_idx: int = 0) -> dict[str, Any]:
    """Break the run when the orchestrator has made no productive
    progress for too many iterations.

    Round-23 history: this used to count *only* system_nudge entries.
    But on the httpbin.org/status/200 test, the orchestrator alternated
    between failing fetch calls and nudge-triggers — every failed tool
    call reset the counter to 0, so the cap never fired and the run
    burned 26 iterations. Now: any non-productive last entry (system
    nudge OR a failed tool call) counts toward the budget, so a stuck
    fetch↔http_request loop converges to break-out within the
    threshold.

    Productive entries (successful tool results, role outputs from
    researcher/coder/etc.) reset the counter — those represent
    forward progress.
    """
    last = scratchpad[-1] if scratchpad else None
    if last is None:
        return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)

    is_nudge = last.action == "system_nudge"
    is_blocked_write = (last.action == "write_file"
                       and (last.output or "").startswith("BLOCKED"))
    # Any tool entry whose output doesn't say "successfully" is treated
    # as a failure for stuck-loop detection. The previous keyword list
    # missed schema-validation errors like "method: must be string (got
    # NoneType)" — round-24 caught this. The "successfully" marker is
    # written by every successful tool path (engine.py:1615), so its
    # absence is a reliable signal.
    is_failed_tool = (last.role == "tool"
                      and last.action != "system_nudge"
                      and bool(last.output)
                      and "successfully" not in (last.output or "").lower())

    if is_nudge or is_blocked_write or is_failed_tool:
        consecutive_nudges += 1
        if consecutive_nudges >= 5:
            final = (build_final_output(scratchpad, turn_start_idx) if scratchpad
                     else "The pipeline could not complete this task. Please try rephrasing your request.")
            return _result("break", plan_call_count=plan_call_count,
                           consecutive_nudges=consecutive_nudges, final_output=final)
    else:
        consecutive_nudges = 0
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)


# ─── Runner ────────────────────────────────────────────────────────────────


def run_flow_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run flow guards in sequence. Returns first non-pass verdict.

    ctx: {scratchpad, iteration, decision, plan_call_count, consecutive_nudges,
          tool_call_counts, user_message}
    Returns: {verdict: {action, final_output?}, plan_call_count, consecutive_nudges}
    """
    scratchpad = ctx["scratchpad"]
    turn_start_idx = int(ctx.get("turn_start_idx") or 0)
    iteration = ctx["iteration"]
    decision: OrchestratorDecision = ctx["decision"]
    plan_call_count = ctx["plan_call_count"]
    consecutive_nudges = ctx["consecutive_nudges"]
    tool_call_counts = ctx["tool_call_counts"]
    user_message = ctx.get("user_message", "")

    # consecutive_nudge_guard MUST run first. The runner short-circuits on
    # the first non-pass verdict, and every other guard returns continue
    # (= push a nudge and retry). Round-23 caught the symptom: flow-guard
    # nudges fired 10+ times in a row on read_file/write_file loops
    # without consecutive_nudge_guard ever incrementing its counter,
    # because earlier guards always returned first. Running it FIRST
    # means it sees the PRIOR turn's nudge in scratchpad and breaks out
    # of the loop before another nudge gets stacked on top.
    guards: list[Any] = [
        lambda: consecutive_nudge_guard(scratchpad, plan_call_count, consecutive_nudges, turn_start_idx),
        lambda: plan_redirect_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: overkill_delegation_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges, user_message),
        lambda: protected_directory_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: repeated_tool_call_guard(scratchpad, iteration, decision, tool_call_counts, plan_call_count, consecutive_nudges),
        lambda: repeated_read_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: broad_search_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: fetch_retry_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: debugger_quota_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: role_spam_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: oscillation_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
        lambda: stale_progress_guard(scratchpad, iteration, plan_call_count, consecutive_nudges),
        lambda: rapid_rewrite_guard(scratchpad, iteration, decision, plan_call_count, consecutive_nudges),
    ]
    for guard in guards:
        result = guard()
        plan_call_count = result["plan_call_count"]
        consecutive_nudges = result["consecutive_nudges"]
        if result["verdict"]["action"] != "pass":
            return result
    return _result("pass", plan_call_count=plan_call_count, consecutive_nudges=consecutive_nudges)
