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

    # Verbatim-echo backstop: if done.input EQUALS the user message
    # (after normalization), it's an echo regardless of how many tools
    # ran. Round-23 caught this: the haiku-and-translate task ran
    # write_file + read_file + translate, then the orchestrator emitted
    # done.input = "write a haiku about coffee in haiku23.txt then
    # translate it to spanish" (the exact user message). The original
    # overlap-ratio check skipped it because tool_count > 1. The
    # verbatim form has no false-positive risk: a real reply just doesn't
    # equal the user's prompt verbatim.
    if user_message:
        norm_user = re.sub(r"\s+", " ", user_message).strip().lower()
        norm_done = re.sub(r"\s+", " ", text).strip().lower()
        if (norm_user and norm_done == norm_user) or (
                len(norm_user) > 30 and norm_user in norm_done
                and len(norm_done) < len(norm_user) * 1.4):
            push_system_nudge(scratchpad, iteration, "echo_verbatim",
                              "STOP: done.input is the user's request restated "
                              "verbatim, not an answer. Present what was actually "
                              "produced (file contents, translation, computed "
                              "result) — not a restatement of what was asked.")
            return GuardVerdict(action="continue")

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
    """When 2+ code files were written without a review pass, push a
    nudge asking the orchestrator to call the reviewer next.

    Round-23 bug history: this guard used to mutate ``decision.action``
    and ``decision.input`` directly and return ``pass``. But we're called
    from inside ``_handle_done`` — past the action-dispatch switch — so
    the action mutation never took effect. Worse, the placeholder text
    "Review the code that was written. Files: …" got picked up later by
    ``raw_content_guard`` and shipped as the user-visible reply, even
    though the orchestrator hadn't actually run a review. Fix: push a
    system_nudge and return ``continue`` so the orchestrator gets a
    fresh decision with the reminder visible.
    """
    has_code_files = [
        e for e in scratchpad
        if e.action == "write_file"
        and re.search(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c)$", e.input or "", re.IGNORECASE)
        and "Successfully" in (e.output or "")
    ]
    has_review = any(e.role == "reviewer" for e in scratchpad)
    if (len(has_code_files) >= 2 and not has_review
            and _role_configured("reviewer") and iteration < max_iter - 2):
        files = ", ".join(e.input for e in has_code_files)
        push_system_nudge(scratchpad, iteration, "code_review_redirect",
                          f"BLOCKED: {len(has_code_files)} code files were "
                          f"written ({files}) without a review. Before "
                          f"emitting done, call action=\"review\" with the "
                          f"file list as input. Then done.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def code_test_guard(scratchpad: list[ScratchpadEntry], iteration: int, max_iter: int,
                     decision: OrchestratorDecision) -> GuardVerdict:
    """When code was written + executed successfully but never tested,
    push a nudge asking the orchestrator to call the tester next.

    Same fix as ``code_review_guard``: never mutate decision fields
    inside _handle_done — that mutation can't reach the dispatch switch
    and the polluted decision.input gets shipped by later guards as the
    final answer (round-22/23 leak).
    """
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
        files = ", ".join(e.input for e in code_files)
        push_system_nudge(scratchpad, iteration, "code_test_redirect",
                          f"BLOCKED: code was written and run but never "
                          f"tested. Before emitting done, call "
                          f"action=\"test\" with these files as input: "
                          f"{files}. Then done.")
        return GuardVerdict(action="continue")
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
    # Round-29 caught: orchestrator emits done with body "The X file has
    # been created. Next, I'll write 4 test cases and run them." — an
    # intent statement, not a completion. The first regex didn't match
    # because "next," has a comma after "next", not " steps". Catch
    # the common intent constructions explicitly: "I'll do X", "I will
    # do X", "I am going to do X", "Let me do X", "Next, I'll", "Now
    # I'll" — all signal the model is announcing future work rather
    # than reporting completed work.
    # First-person intent: "I'll write", "I will run", "I am going to",
    # "Let me X", "Next, I'll Y", "Now I'll Z".
    has_intent = bool(re.search(
        r"(?:^|[\s.,])(?:i'?\s*ll|i\s+will|i\s+am\s+going\s+to|i'?m\s+going\s+to"
        r"|let\s+me|now\s+i'?ll|next,?\s+i'?ll|next,?\s+i\s+will)\s+"
        r"(?:write|create|run|execute|build|test|add|implement|make|"
        r"finish|complete|do|verify|check)\b",
        text, re.IGNORECASE,
    ))
    # Passive intent: "Tests are needed", "Next step is to X", "Still
    # need to Y". Round-30 caught "Tests are needed next" slipping
    # through because it's passive — the model dodges first-person
    # commitment but the meaning is still "work isn't done yet".
    if not has_intent:
        has_intent = bool(re.search(
            r"\b(?:tests?|implementation|review|refactor(?:ing)?|fix(?:es)?|"
            r"test\s+cases?|next\s+step|verification|further\s+work)\s+"
            r"(?:are|is|will\s+be)\s+(?:needed|required|the\s+next|"
            r"to\s+follow|coming|pending)\b",
            text, re.IGNORECASE,
        ))
    has_instructions = bool(re.search(
        r"\b(create\s+a|write\s+a|build\s+a|run\s+the|execute\s+the|test\s+the)\b",
        text, re.IGNORECASE,
    ))
    if (has_next_steps and has_instructions
            and iteration < max_iter_ref[0] - 2):
        m = re.search(r"(?:next\s+steps?|to\s*do|remaining)[:\s]*\n([\s\S]+?)(?:\n---|\n##|\n\n\n|$)",
                      decision.input or "", re.IGNORECASE)
        steps = (m.group(1).strip() if m else (decision.input or "")[:500])
        push_system_nudge(scratchpad, iteration, "next_steps_incomplete",
                          f"STOP: you listed next steps instead of completing the task. "
                          f"Execute them NOW. Steps:\n\n{steps}")
        max_iter_ref[0] = min(max_iter_ref[0] + 10, 200)
        return GuardVerdict(action="continue")
    if has_intent and iteration < max_iter_ref[0] - 2:
        push_system_nudge(scratchpad, iteration, "future_intent_in_done",
                          f"STOP: done.input contains an INTENT statement "
                          f"(\"I'll do X\" / \"Next, I'll Y\" / \"I am going "
                          f"to Z\") instead of reporting completed work. "
                          f"Either DO the work now (dispatch the action) "
                          f"or report what was actually completed without "
                          f"announcing future steps.")
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


_USER_REQUESTED_FILE_RX = re.compile(
    r"\b(?:write|create|make|save|put|generate)(?:\s+a)?\s+(?:file\s+)?"
    r"(?:called|named|at\s+|to\s+)?\s*"
    r"['\"`]?([a-zA-Z0-9_\-/\\.]+\.(?:py|js|ts|html|css|json|sh|txt|csv|md|yaml|yml|toml))['\"`]?",
    re.IGNORECASE,
)


def unwritten_file_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                           max_iter: int, decision: OrchestratorDecision,
                           user_message: str) -> GuardVerdict:
    """Catch the "execute-instead-of-write" pattern.

    When the user explicitly names a file to write (``"write fizzbuzz.py"``,
    ``"create config.json"``, etc.) but the orchestrator goes straight to
    ``execute`` (which uses a tempfile) and emits ``done`` claiming the
    file exists — no ``write_file`` for that path ever ran. Push the
    orchestrator back to do the write before finishing.

    Fires AT MOST ONCE per run. If the orchestrator ignores the first
    nudge, we don't keep blocking — better to let the run finish with a
    less-than-ideal output than to lock the orchestrator in a loop.
    """
    matches = _USER_REQUESTED_FILE_RX.findall(user_message or "")
    if not matches:
        return GuardVerdict(action="pass")
    requested = {(m if isinstance(m, str) else m[0]).strip() for m in matches}
    requested = {r for r in requested if r}
    if not requested:
        return GuardVerdict(action="pass")

    # Already-fired check: if we previously pushed an unwritten_file
    # nudge, don't fire again. The model gets one chance to respond; if
    # it can't, looping wastes iterations and burns the user's patience.
    already_fired = any(
        e.role == "tool" and e.action == "system_nudge"
        and e.input == "unwritten_file"
        for e in scratchpad
    )
    if already_fired:
        return GuardVerdict(action="pass")

    written: set[str] = set()
    for e in scratchpad:
        if e.action == "write_file" and "Successfully" in (e.output or ""):
            inp = (e.input or "")
            for r in list(requested):
                if r.replace("\\", "/").lower() in inp.replace("\\", "/").lower():
                    written.add(r)
    missing = requested - written
    if not missing or iteration >= max_iter - 2:
        return GuardVerdict(action="pass")
    files_str = ", ".join(sorted(missing))
    push_system_nudge(scratchpad, iteration, "unwritten_file",
                      f"STOP: the user asked for {files_str} to be written, but "
                      f"no write_file action created {'them' if len(missing) > 1 else 'it'}. "
                      f"Use write_file with the exact filename "
                      f"{'(' + next(iter(missing)) + ')' if len(missing) == 1 else ''} "
                      f"BEFORE finishing. Don't claim success without doing the write.")
    return GuardVerdict(action="continue")


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


def code_dump_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                     decision: OrchestratorDecision) -> GuardVerdict:
    """When done.input is mostly raw code, redirect via nudge to summarize.

    Round-23 fix: previously this mutated decision.action / decision.input
    in place and returned ``pass``. The action mutation never reached the
    dispatch switch (we're past it inside _handle_done), and the
    instruction-string substitute for decision.input was then shipped by
    raw_content_guard as the final answer, presenting the user with
    "Explain what was done and present the results …" instead of the
    actual code or its explanation. Now: push a nudge and return
    continue so the orchestrator gets a fresh chance to call
    summarize.
    """
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
        push_system_nudge(scratchpad, iteration, "code_dump_redirect",
                          f"BLOCKED: done.input is mostly raw code "
                          f"({code_lines}/{len(lines)} lines). Don't dump "
                          f"code as the answer — call action=\"summarize\" "
                          f"with the code as input to get a user-friendly "
                          f"explanation, then done.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def verbose_fluff_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                         decision: OrchestratorDecision) -> GuardVerdict:
    """When done.input is verbose with high filler-phrase ratio, redirect
    via nudge to summarize. See ``code_dump_guard`` for why this is now
    nudge-and-continue rather than silent mutation.
    """
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
        push_system_nudge(scratchpad, iteration, "verbose_fluff_redirect",
                          f"BLOCKED: done.input is {len(text)} chars with "
                          f"{len(fluff)} filler phrases ("
                          f"ratio {fluff_ratio:.1f}). Call "
                          f"action=\"summarize\" to tighten it, then done.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def raw_content_guard(scratchpad: list[ScratchpadEntry], decision: OrchestratorDecision,
                       user_message: str,
                       turn_start_idx: int = 0) -> GuardVerdict:
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
    final = cleaned or build_final_output(scratchpad, turn_start_idx)

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


# ─── JSON-verdict gates (reviewer / tester) ───────────────────────────────


def _parse_json_verdict(output: str) -> dict[str, Any] | None:
    """Try to extract the JSON verdict block from a role's output.

    Reviewer and tester emit JSON_STRICT — but some local models leak a
    leading explanation or wrap the JSON in ```json ... ```. Try the
    raw output first, then strip a markdown fence if present, then
    salvage the first balanced ``{...}`` block.

    Returns the parsed dict or None if no salvageable JSON is present.
    """
    if not output:
        return None
    s = output.strip()

    # Strip markdown fence if the model added one
    if s.startswith("```"):
        # remove first line (``` or ```json) and trailing ```
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()

    # Direct parse attempt
    try:
        obj = _json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass

    # Salvage: find the first balanced top-level {...}
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = s[start:i + 1]
                try:
                    obj = _json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (ValueError, TypeError):
                    pass
                start = -1
    return None


def reviewer_verdict_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                            decision: OrchestratorDecision) -> GuardVerdict:  # noqa: ARG001
    """Read the reviewer's JSON verdict; block done if FAIL with blockers.

    Reviewer emits JSON_STRICT (manifest output_contract). Schema:
        {"verdict": "PASS"|"FAIL", "blockers": [...], "warnings": [...],
         "suggestions": [...], "summary": "..."}

    If the most-recent reviewer entry's verdict == "FAIL" with non-empty
    blockers, push a nudge that names them and ask the orchestrator to
    address before claiming done. If verdict == "PASS" or no reviewer
    output exists, this guard passes — other guards still run.
    """
    last_review = next((e for e in reversed(scratchpad)
                        if e.role == "reviewer" and e.action != "system_nudge"),
                       None)
    if last_review is None:
        return GuardVerdict(action="pass")
    parsed = _parse_json_verdict(last_review.output or "")
    if parsed is None:
        return GuardVerdict(action="pass")  # not parseable → don't block

    verdict = str(parsed.get("verdict", "")).upper()
    blockers = parsed.get("blockers") or []
    if verdict == "FAIL" and blockers:
        # Don't keep re-firing if the orchestrator has already been
        # nudged about THIS verdict. Without this, a stuck orchestrator
        # that keeps calling review (instead of code) gets nudged on
        # every iteration and the run burns turns. After 2 nudges,
        # accept the done with the FAIL as the user-visible answer
        # — better to surface "the reviewer found these issues" than
        # spin forever.
        prior_nudges = sum(
            1 for e in scratchpad
            if e.action == "system_nudge" and e.input == "reviewer_failed"
        )
        if prior_nudges >= 2:
            return GuardVerdict(action="pass")
        names = []
        for b in blockers[:3]:
            if isinstance(b, dict):
                where = b.get("file") or "?"
                line = b.get("line")
                problem = b.get("problem") or b.get("category") or "blocker"
                loc = f"{where}:{line}" if line else where
                names.append(f"{loc} — {problem}")
            else:
                names.append(str(b)[:120])
        push_system_nudge(scratchpad, iteration, "reviewer_failed",
                          f"BLOCKED: reviewer returned FAIL with "
                          f"{len(blockers)} blocker(s). Do NOT re-run review — "
                          f"dispatch action=code to FIX the blockers, then "
                          f"action=review to re-check. First few: "
                          + "; ".join(names))
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def tester_verdict_guard(scratchpad: list[ScratchpadEntry], iteration: int,
                          decision: OrchestratorDecision) -> GuardVerdict:  # noqa: ARG001
    """Read the tester's JSON verdict; block done if FAIL with failures.

    Tester emits JSON_STRICT. Schema:
        {"verdict": "PASS"|"FAIL", "test_files": [...], "command": "...",
         "tests_total": N, "tests_passed": N, "tests_failed": N,
         "failures": [{"test", "expected", "actual", ...}], "summary": "..."}

    If verdict == "FAIL" with non-empty failures, push a nudge naming
    the failing tests so the orchestrator can debug, then continue.
    """
    last_test = next((e for e in reversed(scratchpad)
                      if e.role == "tester" and e.action != "system_nudge"),
                     None)
    if last_test is None:
        return GuardVerdict(action="pass")
    parsed = _parse_json_verdict(last_test.output or "")
    if parsed is None:
        return GuardVerdict(action="pass")

    verdict = str(parsed.get("verdict", "")).upper()
    failures = parsed.get("failures") or []
    total = parsed.get("tests_total") or 0
    failed = parsed.get("tests_failed") or len(failures)
    if verdict == "FAIL" and (failures or failed > 0):
        # Same loop-cap as reviewer_verdict_guard. After 2 nudges,
        # accept the done with the failures as the user-visible answer.
        prior_nudges = sum(
            1 for e in scratchpad
            if e.action == "system_nudge" and e.input == "tester_failed"
        )
        if prior_nudges >= 2:
            return GuardVerdict(action="pass")
        names = []
        for f in failures[:3]:
            if isinstance(f, dict):
                t = f.get("test") or "?"
                actual = f.get("actual") or "?"
                names.append(f"{t}: {str(actual)[:80]}")
            else:
                names.append(str(f)[:120])
        push_system_nudge(scratchpad, iteration, "tester_failed",
                          f"BLOCKED: tester returned FAIL — {failed}/{total or '?'} "
                          f"test(s) failed. Do NOT re-run tester — dispatch "
                          f"action=debug or action=code to fix the failing "
                          f"tests, then re-run. First few: "
                          + "; ".join(names))
        return GuardVerdict(action="continue")
    # Special case: tester reported tests_total=0 → tests weren't actually
    # run, just written. The orchestrator should dispatch execute next,
    # not done.
    if total == 0 and verdict != "PASS":
        push_system_nudge(scratchpad, iteration, "tester_did_not_run",
                          "BLOCKED: tester returned tests_total=0 — tests "
                          "were written but not executed. Dispatch "
                          "action=execute with the tester's command, then "
                          "call done.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def prompt_template_leak_guard(decision: OrchestratorDecision,
                                 scratchpad: list[ScratchpadEntry],
                                 iteration: int,
                                 max_iter: int) -> GuardVerdict:
    """Reject ``done`` outputs that are obviously leaked role-prompt scaffolding.

    Round-22 stress surfaced cases where the orchestrator regurgitated the
    summarizer / planner role prompt template back as ``done.input``
    instead of answering the user. Patterns observed:

      - "Summarize what was done. User asked: ... Work completed: * fetch: ..."
      - "Work completed:" followed by bulleted action list
      - "Next directives:" / "Pipeline observations:"

    These phrases are scaffolding the orchestrator never authored — they
    only appear when the model is reflecting context back at us. Catch
    them at done time, push a nudge that names the leak, and let the run
    re-orchestrate (which after the engine.py:1438 fix means the model
    will actually see the user's question on the next attempt).

    The earlier ``_is_scratchpad_leak`` in engine.py is a backstop that
    routes to chat fallback. This guard is the proactive version that
    runs before that backstop and gives the orchestrator a chance to
    self-correct without the chat-fallback round-trip.
    """
    text = (decision.input or "").lstrip()
    if len(text) < 20:
        return GuardVerdict(action="pass")
    norm = text.lower()
    leak_prefixes = (
        "summarize what was done",
        "work completed:",
        "pipeline observations:",
        "next directives:",
        "task summary:",
        "execution log:",
    )
    matched = next((p for p in leak_prefixes if norm.startswith(p)), None)
    if matched is None:
        return GuardVerdict(action="pass")
    push_system_nudge(scratchpad, iteration,
                      "prompt_template_leak",
                      f'BLOCKED: done.input started with "{matched}" — that is '
                      f"role-prompt scaffolding, not an answer the user asked "
                      f"for. The user's actual message is in the most-recent "
                      f"user turn; answer that directly with action=done and "
                      f"a real reply.")
    # Bump the iteration cap so the recovery has room to converge —
    # rejected dones are common with weaker orchestrators on context-
    # heavy turns.
    return GuardVerdict(action="continue")


def run_done_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run done-guards in priority order. Returns first non-pass verdict
    (or break with final_output from raw_content_guard).

    ctx: {scratchpad, iteration, max_iterations_ref ([int]), user_message, decision}
    Returns: {action, final_output?}.
    """
    scratchpad = ctx["scratchpad"]
    turn_start_idx = int(ctx.get("turn_start_idx") or 0)
    iteration = ctx["iteration"]
    max_iter_ref = ctx["max_iterations_ref"]  # mutable single-element list
    user_message = ctx["user_message"]
    decision: OrchestratorDecision = ctx["decision"]
    max_iter = max_iter_ref[0]

    guards: list[Any] = [
        # prompt_template_leak runs first because if the done.input is
        # obviously leaked scaffolding, none of the downstream guards
        # need to look at it — they'd just rubber-stamp meaningless text.
        lambda: prompt_template_leak_guard(decision, scratchpad, iteration, max_iter),
        # JSON-verdict gates from reviewer/tester. These run BEFORE the
        # legacy code_review/code_test guards (which only nudge to call
        # those roles in the first place). If the role already ran and
        # returned FAIL, block done with the actual failures named.
        lambda: reviewer_verdict_guard(scratchpad, iteration, decision),
        lambda: tester_verdict_guard(scratchpad, iteration, decision),
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
        lambda: unwritten_file_guard(scratchpad, iteration, max_iter, decision, user_message),
        lambda: hallucinated_file_guard(scratchpad, decision),
        lambda: missing_results_guard(scratchpad, decision),
        lambda: incomplete_code_block_guard(decision),
        lambda: code_in_done_execute_guard(scratchpad, decision, user_message),
        lambda: code_dump_guard(scratchpad, iteration, decision),
        lambda: verbose_fluff_guard(scratchpad, iteration, decision),
        lambda: raw_content_guard(scratchpad, decision, user_message, turn_start_idx),
    ]

    for guard in guards:
        verdict = guard()
        if verdict.action != "pass":
            return {"action": verdict.action, "final_output": verdict.final_output}

    return {"action": "break",
            "final_output": build_final_output(scratchpad, turn_start_idx)}
