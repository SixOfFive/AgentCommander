"""Done guards — run when the orchestrator says 'done'.

Each guard may:
  - push a nudge to scratchpad and return 'continue' (re-ask the orchestrator)
  - mutate decision (redirect action) and return 'pass'
  - return 'break' with final_output to exit the loop
  - return 'pass' if it has no opinion

Ported from EngineCommander/src/main/orchestration/guards/done-guards.ts.
"""
from __future__ import annotations

import json as _json
import re
from typing import Any

from agentcommander.engine.guards.types import (
    GuardVerdict,
    code_context,
    has_deliverable,
    push_system_nudge,
    user_wants_action,
)
from agentcommander.engine.scratchpad import build_final_output
from agentcommander.types import OrchestratorDecision, ScratchpadEntry

_NEXT_DIRECTIVE_RX = re.compile(r"\n?\[NEXT:\s[^\]]*]")


def _role_configured(role: str) -> bool:
    try:
        from agentcommander.engine.role_resolver import resolve as _resolve
        return _resolve(role) is not None
    except Exception:  # noqa: BLE001
        return False


# ─── Hard blocks ───────────────────────────────────────────────────────────


def debugger_fix_incomplete_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                                    decision: OrchestratorDecision) -> GuardVerdict:  # noqa: ARG001
    last_dbg = next((e for e in reversed(scratchpad) if e.role == "debugger"), None)
    if not last_dbg:
        return GuardVerdict(action="pass")
    after = scratchpad[scratchpad.index(last_dbg):]
    has_success = any(
        e.action == "execute" and e.output
        and not any(m in e.output for m in ("Error", "Traceback", "SyntaxError"))
        and "successfully" in e.output
        for e in after
    )
    has_write = any(
        e.action == "write_file" and "Successfully" in (e.output or "")
        for e in after
    )
    if has_success:
        return GuardVerdict(action="pass")
    file_path = ""
    if has_write:
        last_write = next(
            (e for e in reversed(after)
             if e.action == "write_file" and "Successfully" in (e.output or "")),
            None,
        )
        file_path = last_write.input if last_write else ""
    msg = (f"STOP: applied the debugger fix to {file_path} but never re-executed it. "
           f"Execute the fixed code NOW to verify before calling done."
           if has_write else
           "STOP: the debugger found a fix but you have NOT applied it yet. "
           "Apply the fix with write_file, then execute it to verify.")
    push_system_nudge(scratchpad, iteration, "debugger_fix_incomplete", msg)
    return GuardVerdict(action="continue")


def contradictory_done_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                              max_iter: int, decision: OrchestratorDecision) -> GuardVerdict:
    done_text = (decision.input or "").lower()
    claims_success = bool(re.search(r"\b(success|complet|work|done|running|output)\b", done_text))
    last_exec = next((e for e in reversed(scratchpad) if e.action == "execute"), None)
    last_failed = bool(last_exec and any(
        m in (last_exec.output or "") for m in ("Error", "Traceback", "failed")
    ))
    no_success_after = not any(
        e.action == "execute" and "successfully" in (e.output or "")
        and e.timestamp > (last_exec.timestamp if last_exec else 0)
        for e in scratchpad
    )
    if claims_success and last_failed and no_success_after and iteration < max_iter - 3:
        push_system_nudge(scratchpad, iteration, "contradictory_done",
                          "STOP: you claim the task is complete but the last execution FAILED. "
                          "Fix the error and verify with a successful execute before completing.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def error_in_output_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                           max_iter: int, decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    has_traceback = bool(re.search(r"Traceback \(most recent call last\)", text, re.IGNORECASE))
    has_error = bool(re.search(r"^\s*([\w.]+Error|Exception):\s+", text, re.MULTILINE))
    has_stack = bool(re.search(r'File ".*", line \d+', text, re.MULTILINE))
    if (has_traceback or (has_error and has_stack)) and iteration < max_iter - 3:
        push_system_nudge(scratchpad, iteration, "error_in_output",
                          "STOP: your done output contains an error traceback. Fix the error "
                          "first — use the debugger if needed — then present successful output.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def apology_only_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                        decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    text = (decision.input or "").lower()
    is_apology = bool(re.match(
        r"^(i('m| am)\s+(sorry|unable|not able|afraid)|unfortunately|i\s+apologize"
        r"|i\s+cannot|i\s+can'?t|regrettably)",
        text.strip(), re.IGNORECASE,
    ))
    no_deliverable = not has_deliverable(scratchpad)
    if is_apology and no_deliverable and iteration < max_iter - 3:
        push_system_nudge(scratchpad, iteration, "apology_only",
                          f"STOP: do NOT apologize or refuse. Use your tools — execute code, "
                          f"fetch URLs, write files. The user expects action, not excuses. "
                          f'Original request: "{user_message[:200]}"')
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def refuse_to_act_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                         decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    text = ((decision.input or "")).lower().strip()
    if len(text) > 300:
        return GuardVerdict(action="pass")
    if not user_message or len(user_message.strip()) < 3:
        return GuardVerdict(action="pass")
    refuse_patterns = [
        r"\bno (specific )?action (required|needed|to take)\b",
        r"\bno (specific )?task (to|required|identified)\b",
        r"\bnothing (to do|to execute|specific)\b",
        r"\bcannot determine (what|the task)\b",
        r"\bneed (more|clearer) (information|details|context|specification)\b",
        r"\bplease (provide|clarify|specify) (more|what|your)\b",
        r"\bunclear (what|task|request)\b",
        r"\bno (query|request) (to|specified|provided)\b",
    ]
    if not any(re.search(p, text, re.IGNORECASE) for p in refuse_patterns):
        return GuardVerdict(action="pass")
    if has_deliverable(scratchpad) or iteration >= max_iter - 3:
        return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration, "refuse_to_act",
                      f"STOP: do NOT refuse ambiguous requests. The user asked: "
                      f'"{user_message[:300]}". Make a reasonable best-effort interpretation '
                      f"and DO THE WORK — produce a sample output, template, or assumption "
                      f"and proceed.")
    return GuardVerdict(action="continue")


_FACTUAL_QA_PREFIX_RX = re.compile(
    r"^\s*(who|what|where|when|why|how|which|name|tell me|is|are|does|do)\b",
    re.IGNORECASE,
)


def echo_request_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                        decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    text = (decision.input or "").strip()
    if len(text) < 20:
        return GuardVerdict(action="pass")

    # Factual Q&A short-circuit: at iter 1 with no tool work yet, an answer
    # that "echoes" question words is usually just the natural shape of the
    # response ("What is the capital of France?" → "The capital of France
    # is Paris" — 2/3 of long words overlap, but it's a correct direct
    # answer, not evasion). Without this gate the guard rejects the done,
    # the orchestrator gives up and dispatches a fetch tool, then we waste
    # iterations summarizing Wikipedia for trivia the model already knew.
    tool_count = len([e for e in scratchpad if e.role == "tool"])
    if (iteration <= 1
            and tool_count == 0
            and _FACTUAL_QA_PREFIX_RX.match(user_message or "")):
        return GuardVerdict(action="pass")

    user_words = {w for w in user_message.lower().split() if len(w) > 3}
    done_words = [w for w in text.lower().split() if len(w) > 3]
    overlap = sum(1 for w in done_words if w in user_words)
    overlap_ratio = overlap / max(len(done_words), 1)
    if (overlap_ratio > 0.6
            and not has_deliverable(scratchpad)
            and tool_count <= 1
            and iteration < max_iter - 3):
        push_system_nudge(scratchpad, iteration, "echo_request",
                          "STOP: you are repeating the user's request back instead of doing "
                          "the work. Use your tools and present RESULTS, not a restatement.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


_USER_ASKED_ABOUT_CAPS_RX = re.compile(
    r"\b("
    r"what (tools|capabilities|can you|are you able)"
    r"|what.*(do|can).*you.*(have|do)"
    r"|your (capabilities|tools|features|abilities)"
    r"|tools? (do|are) you (have|have access)"
    r"|what.*access.*to"
    r"|list (your |the )?(tools|capabilities|features|commands|actions)"
    r"|what (commands|actions) (do you|are)"
    r")\b",
    re.IGNORECASE,
)


def capabilities_list_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                              decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    # If the user explicitly asked about the agent's tools/capabilities,
    # listing them IS the correct answer — don't flag it as evasion.
    if _USER_ASKED_ABOUT_CAPS_RX.search(user_message or ""):
        return GuardVerdict(action="pass")

    text = (decision.input or "").lower()
    caps = re.search(
        r"\b(i can help|i('m| am) able to|here('s| is) what i can|my capabilities|"
        r"i have access to|i can assist|available tools|available actions)\b",
        text, re.IGNORECASE,
    )
    if caps and not has_deliverable(scratchpad) and iteration < max_iter - 3:
        push_system_nudge(scratchpad, iteration, "capabilities_list",
                          f"STOP: do NOT list your capabilities. The user asked you to DO "
                          f"something. Use your tools to complete the task NOW. "
                          f'Original request: "{user_message[:200]}"')
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


# ─── Quality gates ─────────────────────────────────────────────────────────


def code_review_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                       decision: OrchestratorDecision) -> GuardVerdict:
    has_code_files = [
        e for e in scratchpad
        if e.action == "write_file"
        and re.search(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c)$", e.input or "", re.IGNORECASE)
        and "Successfully" in (e.output or "")
    ]
    has_review = any(e.role == "reviewer" for e in scratchpad)
    if (len(has_code_files) >= 2 and not has_review
            and _role_configured("reviewer") and iteration < max_iter - 2):
        decision.action = "review"
        decision.reasoning = "Multiple code files written — running quality review before completing."
        decision.input = (f"Review the code that was written. Files: "
                          f'{", ".join(e.input for e in has_code_files)}')
    return GuardVerdict(action="pass")


def code_test_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                     decision: OrchestratorDecision) -> GuardVerdict:
    code_files = [
        e for e in scratchpad
        if e.action == "write_file"
        and re.search(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c)$", e.input or "", re.IGNORECASE)
        and "Successfully" in (e.output or "")
    ]
    has_test = any(e.role == "tester" for e in scratchpad)
    has_exec_success = any(
        e.action == "execute" and "successfully" in (e.output or "")
        for e in scratchpad
    )
    if (code_files and not has_test and _role_configured("tester")
            and iteration < max_iter - 2 and has_exec_success):
        decision.action = "test"
        decision.reasoning = "Code was executed but not tested. Running tests to verify correctness."
        decision.input = (f"Write and run tests for: "
                          f'{", ".join(e.input for e in code_files)}')
    return GuardVerdict(action="pass")


def setup_only_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                      max_iter_ref: list[int], user_message: str) -> GuardVerdict:
    setup_actions = ("pip", "npm", "venv", "mkdir", "install")

    def is_setup_step(e: ScratchpadEntry) -> bool:
        return (
            e.role != "tool"
            or (e.action == "execute" and any(s in (e.input or "").lower() for s in setup_actions))
            or e.action == "list_dir"
            or (e.action == "write_file"
                and re.search(r"requirements|package\.json|\.env|setup\.", e.input or "", re.IGNORECASE))
        )

    setup_only = all(is_setup_step(e) for e in scratchpad)
    if (setup_only and not has_deliverable(scratchpad) and user_wants_action(user_message)
            and iteration < max_iter_ref[0] - 2):
        push_system_nudge(scratchpad, iteration, "setup_only_incomplete",
                          f'STOP: you set up the environment but never executed the actual task. '
                          f'User asked: "{user_message[:200]}". Execute the actual task NOW.')
        max_iter_ref[0] = min(max_iter_ref[0] + 10, 200)
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def next_steps_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                      max_iter_ref: list[int], decision: OrchestratorDecision) -> GuardVerdict:
    text = (decision.input or "").lower()
    has_next_steps = bool(re.search(
        r"\b(next\s+steps?|to\s*do\s+next|todo|remaining\s+tasks?|still\s+need"
        r"|not\s+yet\s+completed?|follow[- ]?up)\b", text, re.IGNORECASE,
    ))
    has_instructions = bool(re.search(
        r"\b(create\s+a|write\s+a|build\s+a|run\s+the|execute\s+the|test\s+the)\b",
        text, re.IGNORECASE,
    ))
    if has_next_steps and has_instructions and iteration < max_iter_ref[0] - 2:
        m = re.search(r"(?:next\s+steps?|to\s*do|remaining)[:\s]*\n([\s\S]+?)(?:\n---|\n##|\n\n\n|$)",
                      decision.input or "", re.IGNORECASE)
        steps = (m.group(1).strip() if m else (decision.input or "")[:500])
        push_system_nudge(scratchpad, iteration, "next_steps_incomplete",
                          f"STOP: you listed next steps instead of completing the task. "
                          f"Execute them NOW. Steps:\n\n{steps}")
        max_iter_ref[0] = min(max_iter_ref[0] + 10, 200)
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def environment_ready_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                              max_iter_ref: list[int], decision: OrchestratorDecision,
                              user_message: str) -> GuardVerdict:
    text = (decision.input or "").lower()
    is_ready = bool(re.search(
        r"\b(environment\s+is\s+ready|setup\s+(is\s+)?complete|ready\s+for|"
        r"now\s+installed|successfully\s+installed)\b", text, re.IGNORECASE,
    )) and not has_deliverable(scratchpad)
    if is_ready and user_wants_action(user_message) and iteration < max_iter_ref[0] - 2:
        push_system_nudge(scratchpad, iteration, "env_ready_no_action",
                          f'STOP: you said the environment is "ready" but never did the task. '
                          f'NOW execute the actual task: "{user_message[:200]}"')
        max_iter_ref[0] = min(max_iter_ref[0] + 5, 200)
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def asking_permission_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                              decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    text = (decision.input or "").lower()
    asking = bool(re.search(
        r"\b(let\s+me\s+know|would\s+you\s+like|do\s+you\s+want|shall\s+i|"
        r"should\s+i|if\s+you'?d?\s+like)\b", text, re.IGNORECASE,
    ))
    if asking and not has_deliverable(scratchpad) and iteration < max_iter - 2:
        push_system_nudge(scratchpad, iteration, "asking_permission",
                          f"STOP: do NOT ask the user for permission. Execute the task and "
                          f'present results. Original request: "{user_message[:200]}"')
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def code_execution_guard(scratchpad: list[ScratchpadEntry], decision: OrchestratorDecision,
                          user_message: str) -> GuardVerdict:
    cc = code_context(scratchpad, user_message)
    if cc.needs_execution and (cc.has_code_step or cc.has_write_file) and cc.user_wants_execution:
        last_code = next(
            (e for e in reversed(scratchpad) if e.role == "coder" or e.action == "write_file"),
            None,
        )
        code_content = (last_code.output or "") if last_code else ""
        if cc.has_write_file:
            last_write = next(
                (e for e in reversed(scratchpad) if e.action == "write_file"), None,
            )
            written_path = last_write.input if last_write else ""
            is_python = (written_path.endswith(".py")
                         or "import " in code_content or "def " in code_content)
            decision.action = "execute"
            decision.reasoning = "User asked to run the code — must execute before finishing"
            decision.language = "python" if is_python else "javascript"
            decision.input = code_content[:8000]
        elif cc.has_code_step:
            is_python = "import " in code_content or "def " in code_content or "print(" in code_content
            decision.action = "execute"
            decision.reasoning = "User asked to run the code — must execute before finishing"
            decision.language = "python" if is_python else "javascript"
            decision.input = code_content[:8000]
    return GuardVerdict(action="pass")


def file_readback_guard(scratchpad: list[ScratchpadEntry], decision: OrchestratorDecision,
                         user_message: str) -> GuardVerdict:
    has_write = any(e.action == "write_file" for e in scratchpad)
    has_read = any(e.action == "read_file" for e in scratchpad)
    user_wants_verify = bool(re.search(
        r"\b(read.*back|confirm|verify|check|show.*content|cat)\b",
        user_message, re.IGNORECASE,
    ))
    if has_write and not has_read and user_wants_verify:
        last_write = next(
            (e for e in reversed(scratchpad) if e.action == "write_file"), None,
        )
        if last_write and last_write.input:
            decision.action = "read_file"
            decision.reasoning = "User asked to read back/verify the file — must read before finishing"
            decision.path = last_write.input
    return GuardVerdict(action="pass")


def multi_step_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                      user_message: str,
                      decision: OrchestratorDecision | None = None) -> GuardVerdict:
    user_wants_multi = bool(re.search(
        r"\b(then|after|also|and then|next|second|finally|both)\b",
        user_message, re.IGNORECASE,
    ))
    tool_count = len([e for e in scratchpad if e.role == "tool"])
    if not (user_wants_multi and tool_count <= 1 and len(scratchpad) <= 3):
        return GuardVerdict(action="pass")
    # If the orchestrator's done has a substantial reply, assume it covered
    # both parts in one go (typical for chat-style multi-step requests like
    # "tell me X and then count Y"). Without this, a comprehensive answer
    # gets rejected as premature and the orchestrator wastes 1-2 iterations
    # before producing the same answer again. Threshold tuned to be larger
    # than terse responses but still permissive — anything with 2+ short
    # sentences clears it.
    if decision is not None:
        body = (decision.input or "").strip()
        if len(body) >= 80:
            return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration, "multi_step_incomplete",
                      f'WARNING: the user request has multiple steps ("{user_message[:200]}"). '
                      f"Only {len(scratchpad)} steps completed. Complete ALL steps before done.")
    return GuardVerdict(action="continue")


def plan_without_code_guard(scratchpad: list[ScratchpadEntry],
                              decision: OrchestratorDecision) -> GuardVerdict:
    has_plan = any(e.role == "planner" for e in scratchpad)
    has_code_file = any(
        e.action == "write_file"
        and re.search(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c|html)$", e.input or "", re.IGNORECASE)
        for e in scratchpad
    )
    has_coder = any(e.role == "coder" for e in scratchpad)
    if has_plan and not has_code_file and not has_coder:
        plan_output = next((e.output for e in reversed(scratchpad) if e.role == "planner"), "")
        decision.action = "code"
        decision.reasoning = "Must execute the plan before finishing"
        decision.input = (f"You have this plan. Write the COMPLETE code for it now. "
                          f"Output ALL files with full code:\n\n{plan_output}")
    return GuardVerdict(action="pass")


# ─── Content quality ──────────────────────────────────────────────────────


def terse_done_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                      decision: OrchestratorDecision, user_message: str) -> GuardVerdict:
    text = (decision.input or "").strip()
    brevity = re.search(
        r"\b(reply|respond|answer|say)\s+(with\s+)?(just|only)\b"
        r"|\b(just|only)\s+(say|reply|respond|answer)\b"
        r"|\bone[- ]word\b|\bin\s+one\s+word\b|\b(yes\s+or\s+no)\b",
        user_message, re.IGNORECASE,
    )
    if brevity:
        return GuardVerdict(action="pass")
    if (re.match(r"^(ok|done|yes|no|sure|got it|understood|noted|completed|finished|ready)\.?$",
                 text, re.IGNORECASE)
            and len(user_message) > 20 and iteration < max_iter - 3):
        has_work = any(
            e.role == "tool" and e.action != "system_nudge" for e in scratchpad
        )
        if not has_work:
            push_system_nudge(scratchpad, iteration, "terse_done",
                              f'STOP: you responded with "{text}" but did NO work. '
                              f'User asked: "{user_message[:200]}". Use your tools to complete '
                              f"the task, then provide a substantive response.")
            return GuardVerdict(action="continue")

    if (len(text) < 10
            and len([e for e in scratchpad if e.role == "tool"]) > 2):
        decision.action = "summarize"
        steps_summary = "\n".join(
            f"- {e.action}: {(e.output or '')[:100]}"
            for e in scratchpad
            if e.role == "tool" and e.action != "system_nudge"
        )
        decision.input = (f'Summarize what was done. User asked: "{user_message[:200]}"\n\n'
                          f"Work completed:\n{steps_summary}")
    return GuardVerdict(action="pass")


def hallucinated_file_guard(scratchpad: list[ScratchpadEntry],
                              decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    refs = re.findall(
        r"[`\"']([a-zA-Z0-9_\-/.]+\.(py|js|ts|html|css|json|sh|txt|csv|md))[`\"']", text,
    )
    if not refs:
        return GuardVerdict(action="pass")
    known: set[str] = set()
    for e in scratchpad:
        if e.action in ("write_file", "read_file") and e.input:
            known.add(e.input)
    hallucinated = [
        r[0] for r in refs
        if r[0] not in known and "." in r[0] and not r[0].startswith("http")
    ]
    if hallucinated:
        decision.input = text + (
            f"\n\n_Note: file(s) "
            f'{", ".join("`" + f + "`" for f in hallucinated)}'
            f" are referenced but were not created in this session._"
        )
    return GuardVerdict(action="pass")


def missing_results_guard(scratchpad: list[ScratchpadEntry],
                            decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    success_execs = [
        e for e in scratchpad
        if e.action == "execute" and "successfully" in (e.output or "")
    ]
    if not success_execs or len(text) < 20:
        return GuardVerdict(action="pass")
    last = success_execs[-1]
    m = re.search(r"successfully[^:]*:\n([\s\S]+)", last.output or "")
    stdout = (m.group(1).strip() if m else "")
    if stdout and len(stdout) > 30 and stdout[:40] not in text:
        decision.input = text + f"\n\n**Output:**\n```\n{stdout[:3000]}\n```"
    return GuardVerdict(action="pass")


def incomplete_code_block_guard(decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    open_fences = text.count("```")
    if open_fences % 2 != 0:
        decision.input = text + "\n```"
    return GuardVerdict(action="pass")


def code_in_done_execute_guard(scratchpad: list[ScratchpadEntry],
                                  decision: OrchestratorDecision,
                                  user_message: str) -> GuardVerdict:
    text = decision.input or ""
    if len(text) < 100:
        return GuardVerdict(action="pass")
    user_wants = bool(re.search(
        r"\b(run |execute |exec |launch |show me|show us|print(ing)? |output |"
        r"so (I|we) can see|use (python|javascript|node|bash|js|ts|ruby|sh)|"
        r"just .*(run|execute)|call done when|run .*\.(py|js|sh|ts|rb)|"
        r"python3? \w+\.py|node \w+\.js|bash \w+\.sh|"
        r"what (is|was|does) .* (return|print|output))",
        user_message, re.IGNORECASE,
    ))
    if not user_wants:
        return GuardVerdict(action="pass")
    if any(e.action == "execute" and "successfully" in (e.output or "")
           and "SyntaxError" not in (e.output or "")
           for e in scratchpad):
        return GuardVerdict(action="pass")

    extracted = ""
    language = "python"
    fenced = re.search(r"```(\w+)?\s*\n([\s\S]*?)```", text)
    if fenced:
        lang = (fenced.group(1) or "").lower()
        if re.match(r"^(csv|json|yaml|yml|toml|md|markdown|text|txt|html|xml)$", lang):
            return GuardVerdict(action="pass")
        if lang in ("js", "javascript", "node", "ts"):
            language = "javascript"
        elif lang in ("bash", "sh", "shell"):
            language = "bash"
        else:
            language = "python"
        extracted = fenced.group(2).strip()
    else:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if len(lines) < 5:
            return GuardVerdict(action="pass")
        code_lines = sum(
            1 for ln in lines
            if re.match(r"^\s*(import |from |def |class |if |for |while |return |"
                        r"print\(|#|//|{|}|\[|\])", ln)
            or re.match(r"^\s*\w+\s*[=({\[]", ln)
        )
        if code_lines / len(lines) < 0.6:
            return GuardVerdict(action="pass")
        if re.search(r"\bdef \w+\(|^from \w+|^import \w+", text, re.MULTILINE):
            language = "python"
        elif re.search(r"\bfunction \w+\(|\bconst \w+\s*=|\bconsole\.log\(", text):
            language = "javascript"
        extracted = text

    if not extracted or len(extracted) < 50:
        return GuardVerdict(action="pass")
    decision.action = "execute"
    decision.reasoning = "User asked to run/execute — code was dumped in done.input; executing it now"
    decision.language = language
    decision.input = extracted[:8000]
    return GuardVerdict(action="pass")


def code_dump_guard(decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < 5:
        return GuardVerdict(action="pass")
    code_lines = sum(
        1 for ln in lines
        if re.match(r"^\s*(import |from |def |class |if |for |while |return |"
                    r"print\(|#|//|{|}|\[|\])", ln)
        or re.match(r"^\s*\w+\s*[=({\[]", ln)
    )
    if code_lines / len(lines) > 0.7 and len(lines) > 10:
        decision.action = "summarize"
        decision.input = (
            "The user's task is complete. Explain what was done and present the results "
            "in a user-friendly way. Here is the code that was written:\n\n"
            f"```\n{text[:6000]}\n```"
        )
    return GuardVerdict(action="pass")


def verbose_fluff_guard(decision: OrchestratorDecision) -> GuardVerdict:
    text = decision.input or ""
    if len(text) <= 5000:
        return GuardVerdict(action="pass")
    fluff = re.findall(
        r"\b(certainly|absolutely|of course|happy to help|let me explain|"
        r"as you can see|in summary|to summarize|as mentioned|as noted|"
        r"it'?s worth noting|importantly|essentially|basically|furthermore|"
        r"moreover|additionally|in addition|that being said)\b",
        text, re.IGNORECASE,
    )
    fluff_ratio = len(fluff) / (len(text) / 500)
    if fluff_ratio > 2:
        decision.action = "summarize"
        decision.input = (
            f"Summarize this response concisely. Remove filler phrases and verbose "
            f"explanations. Keep only essential information and results:\n\n{text[:8000]}"
        )
    return GuardVerdict(action="pass")


def raw_content_guard(scratchpad: list[ScratchpadEntry], decision: OrchestratorDecision,
                       user_message: str) -> GuardVerdict:
    raw = decision.input or ""
    looks_raw = (
        raw.startswith("<?xml")
        or raw.startswith("<rss")
        or raw.startswith("<html")
        or raw.startswith("<!DOCTYPE")
        or (raw.startswith("{") and len(raw) > 500 and '"' in raw)
    )
    if looks_raw and len(raw) > 200:
        parsed = raw
        if "<item>" in raw or "<entry>" in raw:
            items: list[str] = []
            entries = re.findall(r"<item>[\s\S]*?</item>", raw) or re.findall(r"<entry>[\s\S]*?</entry>", raw)
            for item in entries[:10]:
                title = re.search(r"<title>([^<]+)</title>", item)
                t = ""
                if title:
                    t = (title.group(1)
                         .replace("&amp;", "&")
                         .replace("&lt;", "<")
                         .replace("&gt;", ">")
                         .replace("&quot;", '"'))
                link = (re.search(r"<link>([^<]+)</link>", item)
                        or re.search(r'<link[^>]*href="([^"]+)"', item))
                lk = link.group(1) if link else ""
                desc = re.search(r"<description>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</description>", item)
                d = re.sub(r"<[^>]+>", "", desc.group(1)).strip() if desc else ""
                if t:
                    items.append(f"- **{t}**" + (f" — {lk}" if lk else "")
                                 + (f"\n  {d[:200]}" if d else ""))
            if items:
                parsed = f"Extracted {len(items)} articles:\n\n" + "\n\n".join(items)
        elif raw.startswith("{") or raw.startswith("["):
            try:
                obj = _json.loads(raw)
                parsed = _json.dumps(obj, indent=2)[:8000]
            except (ValueError, TypeError):
                pass
        decision.action = "summarize"
        decision.input = (
            "Present this data as a clean, well-formatted answer to the user's question. "
            "Use the ACTUAL titles and links below — do NOT use placeholders:\n\n"
            f"{parsed[:8000]}"
        )
        return GuardVerdict(action="pass")

    cleaned = _NEXT_DIRECTIVE_RX.sub("", raw).rstrip()
    final = cleaned or build_final_output(scratchpad)

    user_wants_output = bool(re.search(
        r"\b(show.*output|show.*result|run.*it|execute.*it|print|display)\b",
        user_message, re.IGNORECASE,
    ))
    if user_wants_output:
        last_success = next(
            (e for e in reversed(scratchpad)
             if e.action == "execute"
             and e.output
             and "successfully" in e.output
             and "SyntaxError" not in e.output),
            None,
        )
        if last_success:
            m = re.search(r"successfully[^:]*:\n([\s\S]+)", last_success.output or "")
            stdout = _NEXT_DIRECTIVE_RX.sub("", (m.group(1).strip() if m else "")).strip()
            if stdout and len(stdout) > 5 and stdout[:50] not in final:
                final = (f"{final}\n\n---\n**Execution Output:**\n```\n{stdout}\n```"
                         if final
                         else f"**Execution Output:**\n```\n{stdout}\n```")

    return GuardVerdict(action="break", final_output=final)


# ─── Runner ────────────────────────────────────────────────────────────────


def run_done_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run done-guards in priority order. Returns first non-pass verdict
    (or break with final_output from raw_content_guard).

    ctx: {scratchpad, iteration, max_iterations_ref ([int]), user_message, decision}
    Returns: {action, final_output?}.
    """
    scratchpad = ctx["scratchpad"]
    iteration = ctx["iteration"]
    max_iter_ref = ctx["max_iterations_ref"]  # mutable single-element list
    user_message = ctx["user_message"]
    decision: OrchestratorDecision = ctx["decision"]
    max_iter = max_iter_ref[0]

    guards: list[Any] = [
        lambda: debugger_fix_incomplete_guard(scratchpad, iteration, decision),
        lambda: contradictory_done_guard(scratchpad, iteration, max_iter, decision),
        lambda: error_in_output_guard(scratchpad, iteration, max_iter, decision),
        lambda: apology_only_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: refuse_to_act_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: echo_request_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: capabilities_list_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: code_review_guard(scratchpad, iteration, max_iter, decision),
        lambda: code_test_guard(scratchpad, iteration, max_iter, decision),
        lambda: setup_only_guard(scratchpad, iteration, max_iter_ref, user_message),
        lambda: next_steps_guard(scratchpad, iteration, max_iter_ref, decision),
        lambda: environment_ready_guard(scratchpad, iteration, max_iter_ref, decision, user_message),
        lambda: asking_permission_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: code_execution_guard(scratchpad, decision, user_message),
        lambda: file_readback_guard(scratchpad, decision, user_message),
        lambda: multi_step_guard(scratchpad, iteration, user_message, decision),
        lambda: plan_without_code_guard(scratchpad, decision),
        lambda: terse_done_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: hallucinated_file_guard(scratchpad, decision),
        lambda: missing_results_guard(scratchpad, decision),
        lambda: incomplete_code_block_guard(decision),
        lambda: code_in_done_execute_guard(scratchpad, decision, user_message),
        lambda: code_dump_guard(decision),
        lambda: verbose_fluff_guard(decision),
        lambda: raw_content_guard(scratchpad, decision, user_message),
    ]

    for guard in guards:
        verdict = guard()
        if verdict.action != "pass":
            return {"action": verdict.action, "final_output": verdict.final_output}

    return {"action": "break", "final_output": build_final_output(scratchpad)}
