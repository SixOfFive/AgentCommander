"""Pipeline engine — the main serial orchestration loop.

Mirrors EC's engine.ts. AC's adaptations:
  - Synchronous Python (no parallel action, no async)
  - Generator-based — caller iterates events for live UI
  - Guard hook points are clearly labeled; each family lives in `engine/guards/`

The engine NEVER raises to its caller — pipeline failures yield a
`PipelineEvent(type='error', ...)` and finish.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from agentcommander.db.repos import (
    audit,
    get_role_assignment,
    insert_pipeline_run,
    update_pipeline_run,
)
from agentcommander.engine.actions import (
    ACTION_TO_ROLE,
    ROLE_ACTIONS,
    TOOL_ACTIONS,
)
from agentcommander.engine.role_call import RoleNotAssigned, call_role
from agentcommander.engine.scratchpad import build_final_output, compact_scratchpad, push_nudge
from agentcommander.providers.base import ProviderError
from agentcommander.tools.dispatcher import invoke as invoke_tool
from agentcommander.types import LoopState, OrchestratorDecision, Role, ScratchpadEntry


CATEGORY_MAX_ITERATIONS: dict[str, int] = {
    "project": 100,
    "code": 60,
    "research": 30,
    "question": 10,
    "chat": 5,
}


@dataclass
class RunOptions:
    conversation_id: str
    user_message: str
    working_directory: str | None = None


@dataclass
class PipelineEvent:
    """Streamed from engine → CLI for live rendering."""

    type: str  # iteration | role | role_delta | tool | guard | done | error
    iteration: int | None = None
    action: str | None = None
    role: str | None = None
    output: str | None = None
    delta: str | None = None
    tool: str | None = None
    ok: bool | None = None
    error: str | None = None
    final: str | None = None
    family: str | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ─── Guard hook helpers (lazy imports to avoid module-load circulars) ──────


def _try_import_guards():
    """Import guards lazily; return None for any that aren't ported yet."""
    out: dict[str, Any] = {
        "decision": None,
        "flow": None,
        "execute": None,
        "write": None,
        "output": None,
        "fetch": None,
        "post_step": None,
        "done": None,
    }
    try:
        from agentcommander.engine.guards import decision_guards
        out["decision"] = decision_guards.run_decision_guards
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import flow_guards
        out["flow"] = flow_guards.run_flow_guards
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import execute_guards
        out["execute"] = execute_guards.run_execute_guards
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import write_guards
        out["write"] = write_guards.run_write_guards
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import output_guards
        out["output"] = output_guards.sanitize_output
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import fetch_guards
        out["fetch"] = fetch_guards.analyze_fetch_result
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import post_step_guards
        out["post_step"] = post_step_guards.run_post_step_guards
    except ImportError:
        pass
    try:
        from agentcommander.engine.guards import done_guards
        out["done"] = done_guards.run_done_guards
    except ImportError:
        pass
    return out


# ─── Pipeline run ──────────────────────────────────────────────────────────


class PipelineRun:
    """One execution of the orchestration loop. Caller iterates `events()` to render."""

    def __init__(self, opts: RunOptions) -> None:
        self.run_id = str(uuid.uuid4())
        self.opts = opts
        self.state = LoopState()
        self._max_iterations = 20
        self._guards = _try_import_guards()

    def events(self) -> Iterator[PipelineEvent]:
        """Yield events from the pipeline run. Synchronous generator."""
        opts = self.opts
        insert_pipeline_run(self.run_id, opts.conversation_id)

        try:
            category = self._classify_category(opts.user_message)
            self._max_iterations = CATEGORY_MAX_ITERATIONS.get(category, 20)
            yield PipelineEvent(type="iteration", iteration=0, extra={"category": category})

            self.state.scratchpad.append(ScratchpadEntry(
                step=0, role="router", action="classify",
                input=opts.user_message, output=category, timestamp=time.time(),
            ))

            for iteration in range(1, self._max_iterations + 1):
                self.state.iteration = iteration
                yield PipelineEvent(type="iteration", iteration=iteration)

                try:
                    decision = self._orchestrate()
                except (ProviderError, RoleNotAssigned) as exc:
                    yield PipelineEvent(type="error", error=str(exc))
                    update_pipeline_run(self.run_id, status="failed",
                                        iterations=iteration, error=str(exc),
                                        category=category)
                    return

                # Decision guards (validate JSON shape, fix common LLM mistakes)
                if self._guards["decision"]:
                    result = self._guards["decision"]({
                        "decision": decision,
                        "scratchpad": self.state.scratchpad,
                        "iteration": iteration,
                        "user_message": opts.user_message,
                        "browser_available": False,
                    })
                    if result["verdict"]["action"] == "continue":
                        yield PipelineEvent(type="guard", family="decision",
                                            reason="rewriting orchestrator decision")
                        continue
                    decision = result["decision"]

                yield PipelineEvent(type="iteration", iteration=iteration,
                                    action=decision.action)

                # Done branch
                if decision.action == "done":
                    final = self._handle_done(decision, opts.user_message)
                    if final is None:
                        yield PipelineEvent(type="guard", family="done",
                                            reason="rejecting premature done")
                        continue
                    yield PipelineEvent(type="done", final=final)
                    update_pipeline_run(self.run_id, status="done",
                                        iterations=iteration, category=category)
                    return

                # Flow guards (cross-cutting caps + completion)
                if self._guards["flow"]:
                    fr = self._guards["flow"]({
                        "scratchpad": self.state.scratchpad,
                        "iteration": iteration,
                        "decision": decision,
                        "plan_call_count": self.state.plan_call_count,
                        "consecutive_nudges": self.state.consecutive_nudges,
                        "tool_call_counts": self.state.tool_call_counts,
                        "user_message": opts.user_message,
                    })
                    self.state.plan_call_count = fr["plan_call_count"]
                    self.state.consecutive_nudges = fr["consecutive_nudges"]
                    if fr["verdict"]["action"] == "continue":
                        yield PipelineEvent(type="guard", family="flow", reason="flow-guard nudge")
                        continue
                    if fr["verdict"]["action"] == "break":
                        yield PipelineEvent(type="done", final=fr["verdict"]["final_output"])
                        update_pipeline_run(self.run_id, status="done",
                                            iterations=iteration, category=category)
                        return

                # Dispatch — role delegation
                if decision.action in ROLE_ACTIONS:
                    yield from self._dispatch_role(decision, iteration, opts)
                    continue

                # Dispatch — tool action
                if decision.action in TOOL_ACTIONS:
                    yield from self._dispatch_tool(decision, iteration, opts)
                    continue

                # Unknown action
                push_nudge(
                    self.state.scratchpad, iteration, "unknown_action",
                    f'BLOCKED: Unknown action "{decision.action}". '
                    "Valid: role delegations (plan/code/review/...) or tools (read_file/write_file/execute/fetch/...) or done.",
                )

            yield PipelineEvent(type="error",
                                error=f"hit max iterations ({self._max_iterations})")
            update_pipeline_run(self.run_id, status="failed",
                                iterations=self._max_iterations,
                                error=f"max iterations ({self._max_iterations})",
                                category=category)

        except Exception as exc:  # noqa: BLE001 — outermost engine boundary
            yield PipelineEvent(type="error", error=f"{type(exc).__name__}: {exc}")
            update_pipeline_run(self.run_id, status="failed",
                                iterations=self.state.iteration, error=str(exc))

    # ─────────────────────────────────────────────────────────────────────

    def _classify_category(self, user_message: str) -> str:
        if get_role_assignment(Role.ROUTER) is None:
            return "question"
        try:
            raw = call_role(Role.ROUTER, user_input=user_message, json_mode=True)
            parsed = json.loads(raw)
            return str(parsed.get("category", "question"))
        except (ProviderError, RoleNotAssigned, ValueError, json.JSONDecodeError):
            return "question"

    def _orchestrate(self) -> OrchestratorDecision:
        if get_role_assignment(Role.ORCHESTRATOR) is None:
            return OrchestratorDecision(action="done",
                                        reasoning="orchestrator role is not assigned to a model")
        scratchpad_text = compact_scratchpad(self.state.scratchpad)
        raw = call_role(Role.ORCHESTRATOR,
                        user_input=scratchpad_text or self.opts.user_message,
                        scratchpad_text=scratchpad_text,
                        json_mode=True)
        try:
            parsed = json.loads(raw)
            return OrchestratorDecision.from_dict(parsed)
        except (json.JSONDecodeError, ValueError, TypeError):
            return OrchestratorDecision(
                action="done",
                reasoning="orchestrator returned invalid JSON; halting",
                input=build_final_output(self.state.scratchpad),
            )

    def _dispatch_role(self, decision: OrchestratorDecision, iteration: int,
                       opts: RunOptions) -> Iterator[PipelineEvent]:
        role = ACTION_TO_ROLE[decision.action]
        started = time.time()
        try:
            output = call_role(role,
                               user_input=decision.input or opts.user_message,
                               scratchpad_text=compact_scratchpad(self.state.scratchpad),
                               conversation_id=opts.conversation_id)
        except (ProviderError, RoleNotAssigned) as exc:
            push_nudge(self.state.scratchpad, iteration, f"{role.value}_failed",
                       f"Role {role.value} call failed: {exc}")
            yield PipelineEvent(type="error", role=role.value, error=str(exc))
            return

        self.state.scratchpad.append(ScratchpadEntry(
            step=iteration, role=role.value, action=decision.action,
            input=decision.input or "", output=output,
            timestamp=time.time(),
            duration_ms=int((time.time() - started) * 1000),
        ))
        yield PipelineEvent(type="role", role=role.value, output=output)

        # Post-step guards (dead-end, anti-stuck, repeat-error)
        if self._guards["post_step"]:
            ps = self._guards["post_step"]({
                "scratchpad": self.state.scratchpad,
                "iteration": iteration,
                "output_hashes": self.state.output_hashes,
                "role": role.value,
                "validated_output": output,
            })
            if ps["action"] == "break":
                yield PipelineEvent(type="done", final=ps["final_output"])

    def _dispatch_tool(self, decision: OrchestratorDecision, iteration: int,
                       opts: RunOptions) -> Iterator[PipelineEvent]:
        # Write guards (pre-dispatch)
        if decision.action == "write_file" and self._guards["write"]:
            wv = self._guards["write"]({
                "scratchpad": self.state.scratchpad,
                "iteration": iteration,
                "file_path": decision.path or decision.input or "",
                "file_content": decision.content or "",
                "user_message": opts.user_message,
            })
            if wv["action"] != "pass":
                yield PipelineEvent(type="guard", family="write", reason="write-guard block")
                return

        # Execute guards (rewrite code/lang or block)
        execute_code = decision.input or ""
        execute_language = decision.language or "python"
        if decision.action == "execute" and self._guards["execute"]:
            ev = self._guards["execute"]({
                "code": execute_code,
                "language": execute_language,
                "scratchpad": self.state.scratchpad,
                "iteration": iteration,
                "working_directory": opts.working_directory,
                "file_write_registry": self.state.file_write_registry,
            })
            if ev["verdict"]["action"] != "pass":
                yield PipelineEvent(type="guard", family="execute", reason="execute-guard block")
                return
            execute_code = ev["code"]
            execute_language = ev["language"]

        # Build payload
        payload = _decision_to_payload(decision, execute_code, execute_language)

        started = time.time()
        result = invoke_tool(decision.action, payload,
                             working_directory=opts.working_directory,
                             conversation_id=opts.conversation_id)

        # Sanitize output
        output_text = result.output or ""
        if self._guards["output"] and output_text:
            output_text = self._guards["output"](output_text)

        # Fetch hint analysis
        if decision.action == "fetch" and self._guards["fetch"] and output_text:
            hints = self._guards["fetch"](output_text, decision.url or decision.input or "")
            if hints:
                output_text = output_text + hints

        scratch_input = decision.path or decision.url or decision.input or ""
        scratch_output = (f"successfully completed:\n{output_text}"
                          if result.ok else (result.error or "failed"))

        self.state.scratchpad.append(ScratchpadEntry(
            step=iteration, role="tool", action=decision.action,
            input=scratch_input, output=scratch_output,
            timestamp=time.time(),
            duration_ms=int((time.time() - started) * 1000),
            content=decision.content if decision.action == "write_file" else None,
        ))

        if decision.action == "write_file" and result.ok and decision.path:
            self.state.file_write_registry[decision.path] = decision.content or ""

        if not result.ok:
            audit("tool.failure", {"tool": decision.action, "error": result.error})

        yield PipelineEvent(type="tool", tool=decision.action, ok=result.ok,
                            output=output_text, error=result.error)

    def _handle_done(self, decision: OrchestratorDecision, user_message: str) -> str | None:
        """Run done-guards. Return final output to break, or None to continue."""
        if not self._guards["done"]:
            return decision.input or build_final_output(self.state.scratchpad)
        verdict = self._guards["done"]({
            "scratchpad": self.state.scratchpad,
            "iteration": self.state.iteration,
            "max_iterations_ref": [self._max_iterations],
            "user_message": user_message,
            "decision": decision,
        })
        if verdict["action"] == "continue":
            return None
        if verdict["action"] == "break":
            return verdict["final_output"]
        # 'pass' — fall through (decision was mutated to a different action)
        return None


def _decision_to_payload(decision: OrchestratorDecision, exec_code: str,
                        exec_language: str) -> dict[str, Any]:
    a = decision.action
    if a in ("read_file", "list_dir", "delete_file"):
        return {"path": decision.path or decision.input}
    if a == "write_file":
        return {"path": decision.path or decision.input, "content": decision.content or ""}
    if a == "execute":
        return {"language": exec_language, "code": exec_code}
    if a == "fetch":
        return {"url": decision.url or decision.input,
                "method": decision.method, "headers": decision.headers, "body": decision.body}
    if a == "start_process":
        return {"command": decision.command or decision.input}
    if a in ("kill_process", "check_process"):
        return {"id": decision.input}
    return {}
