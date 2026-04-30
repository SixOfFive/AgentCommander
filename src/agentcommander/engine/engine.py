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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from agentcommander.db.repos import (
    audit,
    insert_pipeline_run,
    update_pipeline_run,
)
from agentcommander.engine.actions import (
    ACTION_TO_ROLE,
    ROLE_ACTIONS,
    TOOL_ACTIONS,
)
from agentcommander.engine.role_call import (
    RoleNotAssigned,
    call_role,
    tool_registry_appendix,
)
from agentcommander.engine.role_resolver import resolve as resolve_role
from agentcommander.engine.scratchpad import build_final_output, compact_scratchpad, push_nudge
from agentcommander.providers.base import (
    ChatMessage,
    ProviderError,
    resolve as resolve_provider,
)
from agentcommander.tools.dispatcher import invoke as invoke_tool
from agentcommander.types import LoopState, OrchestratorDecision, Role, ScratchpadEntry


CHAT_FALLBACK_SYSTEM_PROMPT = (
    "You are a helpful assistant in a CLI. Respond directly and concisely "
    "to the user's message. Plain text only — no JSON, no markdown headers. "
    "If the user message contains pipeline observations (raw HTML, JSON, "
    "tool output), extract the answer from that data — do not claim you "
    "lack access to information that is right there in the prompt."
)


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
    # ID of the row this user_message lives at in the `messages` table.
    # When set, the engine tags the router/classify scratchpad entry with
    # this id so the model-view (scratchpad) and user-view (messages) stay
    # joined. None for runs not initiated through the REPL.
    user_message_id: str | None = None
    # Optional live hooks. Synchronous; should not block.
    #   on_role_delta(role, delta_text) — every streamed token from the active role
    #   on_role_start(role, model, num_ctx) — fired when a role call begins;
    #                                       num_ctx is the configured context
    #                                       window for this role (None to use
    #                                       the provider's default)
    #   on_role_end(role, model, prompt_tokens, completion_tokens) — when it finishes
    #   on_context_update(current, cap_min) — token-count of the next prompt being sent
    on_role_delta: "Any | None" = None
    on_role_start: "Any | None" = None
    on_role_end: "Any | None" = None
    on_context_update: "Any | None" = None


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
        # External cancel signal — TUI sets this when the user types /stop.
        # Engine checks it at iteration boundaries and before each dispatch.
        self.cancel_event: Any | None = None

    def is_cancelled(self) -> bool:
        ce = self.cancel_event
        if ce is None:
            return False
        try:
            return bool(ce.is_set())
        except AttributeError:
            return False

    def _push_entry(self, entry: ScratchpadEntry) -> None:
        """Append to in-memory scratchpad AND persist to ``scratchpad_entries``.

        This is the model-view write path. The user-view (``messages``
        table) is touched separately by the TUI; compaction operates only
        on this side. DB persistence is best-effort — a transient SQLite
        error must never crash the engine mid-run, so we audit + swallow.
        """
        self.state.scratchpad.append(entry)
        try:
            from agentcommander.db.repos import insert_scratchpad_entry
            insert_scratchpad_entry(
                conversation_id=self.opts.conversation_id,
                run_id=self.run_id,
                step=entry.step,
                role=entry.role,
                action=entry.action,
                input_text=entry.input or "",
                output_text=entry.output or "",
                timestamp=entry.timestamp,
                duration_ms=entry.duration_ms,
                content=entry.content,
                message_id=entry.message_id,
                replaced_message_ids=entry.replaced_message_ids,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                audit("scratchpad.persist_failed",
                      {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass

    def _hydrate_scratchpad_from_db(self) -> None:
        """Load this conversation's prior scratchpad into ``state.scratchpad``.

        Called once at the start of each run so the orchestrator/router/etc.
        see the full conversation context when they read
        ``compact_scratchpad`` for prompt-building. Replaced entries
        (compaction artifacts) are filtered at the SQL layer.
        """
        try:
            from agentcommander.db.repos import list_scratchpad_entries
            rows = list_scratchpad_entries(self.opts.conversation_id)
        except Exception:  # noqa: BLE001
            return
        for r in rows:
            self.state.scratchpad.append(ScratchpadEntry(
                step=r["step"],
                role=r["role"],
                action=r["action"],
                input=r["input"] or "",
                output=r["output"] or "",
                timestamp=r["timestamp"],
                duration_ms=r["duration_ms"],
                content=r["content"],
                message_id=r["message_id"],
                replaced_message_ids=r["replaced_message_ids"],
            ))

    # ── Scratchpad compaction ─────────────────────────────────────────────
    #
    # When the cross-turn scratchpad gets large enough that compact_scratchpad
    # would crowd out the system prompt + user message + response budget,
    # we summarize the oldest entries via the summarizer role and replace
    # them with one synthetic entry. Originals stay in the DB (is_replaced=1)
    # so /history and audit views can still show full fidelity. The user-view
    # `messages` table is never touched.
    #
    # Trigger: scratchpad text length > _compaction_budget_chars(). Keep the
    # most recent COMPACTION_KEEP_TAIL entries verbatim — they're the live
    # working context the orchestrator needs. Older entries fold into one
    # summary.

    COMPACTION_KEEP_TAIL: int = 6
    COMPACTION_TRIGGER_FRACTION: float = 0.9  # of session context window
    COMPACTION_DEFAULT_NUM_CTX: int = 8192    # fallback when no ceiling set

    def _compaction_budget_chars(self) -> int:
        """Char budget for the prompt-side scratchpad. Roughly 90% of the
        session context window expressed as chars (4 chars/token estimate).
        Compaction fires only when we're close to the ceiling so the model
        keeps as much real history as possible before the summarizer trims
        it — earlier compaction discards information unnecessarily."""
        from agentcommander.db.repos import get_config
        raw = get_config("session_ceiling_tokens", None)
        try:
            tokens = int(raw) if raw else self.COMPACTION_DEFAULT_NUM_CTX
        except (TypeError, ValueError):
            tokens = self.COMPACTION_DEFAULT_NUM_CTX
        return int(tokens * 4 * self.COMPACTION_TRIGGER_FRACTION)

    def _summarize_rows_for_compaction(self, rows: list[dict]) -> str | None:
        """Summarize old scratchpad rows via the summarizer role.

        Returns the summary text (stripped), or ``None`` on any failure
        (summarizer unassigned, network error, empty reply). Caller falls
        back to "no compaction" — better to keep originals than to lose
        context to a botched summary.
        """
        if not rows:
            return None
        lines: list[str] = []
        for r in rows:
            inp = (r.get("input") or "")[:400]
            out = (r.get("output") or "")[:800]
            prefix = f"step {r['step']} {r['role']}/{r['action']}: "
            if inp:
                lines.append(f"{prefix}in={inp} out={out}")
            else:
                lines.append(f"{prefix}out={out}")
        body = "\n".join(lines)
        prompt = (
            "Compress this prior conversation history into a concise summary "
            "(under 800 words). Preserve: user's stated goals, key facts and "
            "results from tool calls, decisions made, unresolved questions. "
            "Plain text only — no markdown headers, no JSON.\n\n"
            + body
        )
        try:
            summary = call_role(
                Role.SUMMARIZER,
                user_input=prompt,
                conversation_id=self.opts.conversation_id,
                json_mode=False,
                should_cancel=self.is_cancelled,
            )
        except (ProviderError, RoleNotAssigned):
            return None
        except Exception as exc:  # noqa: BLE001
            audit("compaction.summarizer_failed",
                  {"error": f"{type(exc).__name__}: {exc}"})
            return None
        if not summary or not summary.strip():
            return None
        return summary.strip()

    def _maybe_compact_scratchpad(self) -> Iterator[PipelineEvent]:
        """Replace oldest scratchpad entries with a summary if the prompt
        text would exceed the budget. Yields a guard event so the user sees
        the compaction happen instead of just a long pause."""
        if len(self.state.scratchpad) <= self.COMPACTION_KEEP_TAIL:
            return
        text = compact_scratchpad(self.state.scratchpad,
                                  tail=len(self.state.scratchpad))
        budget = self._compaction_budget_chars()
        if len(text) <= budget:
            return

        # Pull the same rows from DB — we need their ids to flag is_replaced.
        from agentcommander.db.repos import (
            list_scratchpad_entries, mark_scratchpad_replaced,
        )
        rows = list_scratchpad_entries(self.opts.conversation_id)
        if len(rows) <= self.COMPACTION_KEEP_TAIL:
            return  # safety: in-memory and DB views disagreed; skip
        old_rows = rows[: -self.COMPACTION_KEEP_TAIL]
        old_ids = [r["id"] for r in old_rows]
        if not old_ids:
            return

        yield PipelineEvent(
            type="guard", family="compaction",
            reason=(f"compacting {len(old_ids)} prior scratchpad entr"
                    f"{'y' if len(old_ids) == 1 else 'ies'} via summarizer "
                    f"(prompt was {len(text)} chars, budget {budget})"),
        )

        summary = self._summarize_rows_for_compaction(old_rows)
        if summary is None:
            yield PipelineEvent(
                type="guard", family="compaction",
                reason="summarizer unavailable / failed — keeping originals",
            )
            return

        # Insert synthetic compaction entry, flag the originals, and rebuild
        # the in-memory scratchpad. The DB's is_replaced=1 hides the
        # originals from future hydrate calls but keeps them queryable for
        # audit / /history paths.
        from agentcommander.db.repos import insert_scratchpad_entry
        synth_id = insert_scratchpad_entry(
            conversation_id=self.opts.conversation_id,
            run_id=self.run_id,
            step=0,
            role="system",
            action="compacted",
            input_text=f"compacted {len(old_ids)} entries",
            output_text=summary,
            timestamp=time.time(),
            replaced_message_ids=old_ids,
        )
        mark_scratchpad_replaced(old_ids)
        audit("compaction.applied", {
            "summary_id": synth_id,
            "replaced_count": len(old_ids),
            "original_chars": len(text),
            "summary_chars": len(summary),
        })

        # Rebuild state.scratchpad: synthetic summary first, then the
        # untouched tail (which already exists in the in-memory list).
        keep = self.state.scratchpad[-self.COMPACTION_KEEP_TAIL:]
        self.state.scratchpad.clear()
        self.state.scratchpad.append(ScratchpadEntry(
            step=0, role="system", action="compacted",
            input="", output=summary, timestamp=time.time(),
            replaced_message_ids=old_ids,
        ))
        self.state.scratchpad.extend(keep)

        yield PipelineEvent(
            type="guard", family="compaction",
            reason=(f"compacted into {len(summary)} char summary "
                    f"(scratchpad now {self.COMPACTION_KEEP_TAIL + 1} entries)"),
        )

    def events(self) -> Iterator[PipelineEvent]:
        """Yield events from the pipeline run. Synchronous generator."""
        opts = self.opts
        insert_pipeline_run(self.run_id, opts.conversation_id)

        # Cross-turn memory: rehydrate scratchpad from prior turns of this
        # conversation before any role/router call goes out, so they see
        # full context. Replaced (compacted) entries are filtered out.
        self._hydrate_scratchpad_from_db()

        # If the rehydrated scratchpad would crowd out the prompt budget,
        # compact the oldest entries via the summarizer. Yields one or two
        # guard events so the user can see compaction happening (it can
        # take several seconds to summarize).
        try:
            yield from self._maybe_compact_scratchpad()
        except Exception as exc:  # noqa: BLE001
            audit("compaction.unexpected_error",
                  {"error": f"{type(exc).__name__}: {exc}"})

        try:
            category = self._classify_category(opts.user_message, opts)
            self._max_iterations = CATEGORY_MAX_ITERATIONS.get(category, 20)
            yield PipelineEvent(type="iteration", iteration=0, extra={"category": category})

            # Router/classify entry. Tagged with the user's `messages.id`
            # (when supplied) so the model-view links back to the user-view.
            self._push_entry(ScratchpadEntry(
                step=0, role="router", action="classify",
                input=opts.user_message, output=category, timestamp=time.time(),
                message_id=opts.user_message_id,
            ))

            for iteration in range(1, self._max_iterations + 1):
                if self.is_cancelled():
                    yield PipelineEvent(type="error", error="cancelled by /stop")
                    update_pipeline_run(self.run_id, status="cancelled",
                                        iterations=iteration - 1, error="cancelled by /stop",
                                        category=category)
                    return

                self.state.iteration = iteration
                yield PipelineEvent(type="iteration", iteration=iteration)

                try:
                    decision = self._orchestrate(opts)
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

                # Forgive a class of common orchestrator hallucinations: when
                # the router classifies a casual message as "chat" the model
                # often echoes the category as the action ("chat" / "respond"
                # / "answer" / "reply"). None of those are registered actions,
                # so without this they'd hit the unknown-action nudge and the
                # next iteration would emit `done` with no payload. Treat them
                # as a `done` whose input is whatever the model put in
                # ``input`` or, failing that, ``reasoning`` — the trivia path
                # the orchestrator prompt already documents
                # (``{"action":"done","input":"2+2=4"}``).
                _CHAT_LIKE_ACTIONS = {"chat", "respond", "answer", "reply"}
                if decision.action in _CHAT_LIKE_ACTIONS:
                    coerced_input = (decision.input or decision.reasoning or "").strip()
                    decision = OrchestratorDecision(
                        action="done",
                        reasoning=(f"coerced {decision.action!r} → done "
                                   f"({decision.reasoning or 'no reason given'})"),
                        input=coerced_input,
                    )

                yield PipelineEvent(type="iteration", iteration=iteration,
                                    action=decision.action)

                # Done branch
                if decision.action == "done":
                    final = self._handle_done(decision, opts.user_message)
                    if final is None:
                        yield PipelineEvent(type="guard", family="done",
                                            reason="rejecting premature done")
                        continue

                    # Two failure modes for trivial chat / single-line
                    # questions, both surface here as the same symptom — the
                    # final is a build_final_output step-by-step echo of the
                    # router's classification ("Step 0: router/classify\n
                    # question"). Detect that and recover:
                    #
                    # 1. If decision.input has a real reply that the
                    #    done-guard runner overrode (it returns
                    #    build_final_output ignoring decision.input — see
                    #    done_guards.run_done_guards line 717), use it.
                    # 2. Otherwise stream a direct chat call against the
                    #    orchestrator's model so the user gets an actual
                    #    response instead of the classification echoed back.
                    if self._is_router_echo(final):
                        decision_input = (decision.input or "").strip()
                        if decision_input and not self._is_router_echo(decision_input):
                            final = decision_input
                        else:
                            yield from self._chat_fallback_stream(
                                opts.user_message, opts,
                            )
                            update_pipeline_run(
                                self.run_id, status="done",
                                iterations=iteration, category=category,
                            )
                            return

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
                    try:
                        yield from self._dispatch_tool(decision, iteration, opts)
                    except Exception as exc:
                        # PermissionDenied: the user (or the non-TTY auto-deny)
                        # said no. Halt the pipeline cleanly with a friendlier
                        # synthesis instead of a raw error line.
                        if type(exc).__name__ == "PermissionDenied":
                            denied_path = getattr(exc, "path", None)
                            denied_op = getattr(exc, "operation", decision.action)
                            target = denied_path or decision.path or decision.url or decision.input or "?"
                            # Match the "halted: permission denied" wording —
                            # we don't know whether the user actually said
                            # "Deny" interactively or whether the non-TTY
                            # auto-deny kicked in, so don't claim either.
                            error_msg = f"halted: permission denied for {denied_op} {target}"
                            yield PipelineEvent(type="error", error=error_msg)
                            # Friendly final so the user sees an actual
                            # ● AgentCommander reply rather than just the
                            # raw error line scrolling off.
                            final_msg = (
                                f"I couldn't complete that — permission for "
                                f"`{denied_op}` on `{target}` was denied "
                                "(or auto-denied because stdin isn't an "
                                "interactive terminal).\n\n"
                                "Next steps:\n"
                                f"  - run AgentCommander interactively and "
                                f"choose [a] Always or [t] Yes once when prompted\n"
                                f"  - or set a writable working directory "
                                f"with `/workdir <path>`"
                            )
                            yield PipelineEvent(type="done", final=final_msg)
                            update_pipeline_run(
                                self.run_id, status="cancelled",
                                iterations=iteration,
                                error=str(exc), category=category,
                            )
                            return
                        raise
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

    def _emit_role_start(self, role: Role, opts: "RunOptions") -> tuple[str, int | None]:
        """Fire ``opts.on_role_start`` for ``role`` so the status bar flips
        to ``▸ <role> → <model>`` *before* the call goes out. Returns
        ``(model_name, num_ctx)`` so the matching ``_emit_role_end`` doesn't
        re-resolve mid-flight (the values it needs are cached here).
        """
        rr = resolve_role(role)
        model_name = rr.model if rr else "?"
        num_ctx = rr.context_window_tokens if rr else None
        if opts.on_role_start is not None:
            try:
                opts.on_role_start(role.value, model_name, num_ctx)
            except Exception:  # noqa: BLE001
                pass
        return model_name, num_ctx

    def _emit_role_end(self, role: Role, model_name: str, opts: "RunOptions",
                       prompt_tokens: int, completion_tokens: int) -> None:
        if opts.on_role_end is not None:
            try:
                opts.on_role_end(role.value, model_name,
                                 prompt_tokens or 0, completion_tokens or 0)
            except Exception:  # noqa: BLE001
                pass

    def _classify_category(self, user_message: str, opts: "RunOptions") -> str:
        if resolve_role(Role.ROUTER) is None:
            return "question"
        # Tell the bar the router is about to run, before the network call.
        model_name, _ = self._emit_role_start(Role.ROUTER, opts)
        prompt_tokens = completion_tokens = 0

        def _capture(p: int | None, c: int | None) -> None:
            nonlocal prompt_tokens, completion_tokens
            prompt_tokens = p or 0
            completion_tokens = c or 0

        try:
            raw = call_role(Role.ROUTER, user_input=user_message,
                            conversation_id=self.opts.conversation_id,
                            json_mode=True, on_finish=_capture,
                            should_cancel=self.is_cancelled)
            parsed = json.loads(raw)
            result = str(parsed.get("category", "question"))
        except (ProviderError, RoleNotAssigned, ValueError, json.JSONDecodeError):
            result = "question"
        self._emit_role_end(Role.ROUTER, model_name, opts,
                            prompt_tokens, completion_tokens)
        return result

    def _orchestrate(self, opts: "RunOptions") -> OrchestratorDecision:
        if resolve_role(Role.ORCHESTRATOR) is None:
            return OrchestratorDecision(action="done",
                                        reasoning="orchestrator role is not assigned to a model")
        # Bar flips to ▸ orchestrator → <model> before we actually send.
        model_name, _ = self._emit_role_start(Role.ORCHESTRATOR, opts)
        prompt_tokens = completion_tokens = 0

        def _capture(p: int | None, c: int | None) -> None:
            nonlocal prompt_tokens, completion_tokens
            prompt_tokens = p or 0
            completion_tokens = c or 0

        scratchpad_text = compact_scratchpad(self.state.scratchpad)
        try:
            raw = call_role(Role.ORCHESTRATOR,
                            user_input=scratchpad_text or self.opts.user_message,
                            scratchpad_text=scratchpad_text,
                            conversation_id=self.opts.conversation_id,
                            json_mode=True,
                            on_finish=_capture,
                            should_cancel=self.is_cancelled)
            try:
                parsed = json.loads(raw)
                decision = OrchestratorDecision.from_dict(parsed)
            except (json.JSONDecodeError, ValueError, TypeError):
                decision = OrchestratorDecision(
                    action="done",
                    reasoning="orchestrator returned invalid JSON; halting",
                    input=build_final_output(self.state.scratchpad),
                )
        finally:
            self._emit_role_end(Role.ORCHESTRATOR, model_name, opts,
                                prompt_tokens, completion_tokens)
        return decision

    def _dispatch_role(self, decision: OrchestratorDecision, iteration: int,
                       opts: RunOptions) -> Iterator[PipelineEvent]:
        role = ACTION_TO_ROLE[decision.action]
        started = time.time()

        # Look up resolved (provider, model) for status events.
        rr = resolve_role(role)
        model_name = rr.model if rr else "?"
        num_ctx = rr.context_window_tokens if rr else None

        # on_role_start
        if opts.on_role_start is not None:
            try:
                opts.on_role_start(role.value, model_name, num_ctx)
            except Exception:  # noqa: BLE001
                pass

        # Live streaming: deltas render immediately inside the call_role
        # provider loop via opts.on_role_delta.
        on_delta = None
        if opts.on_role_delta is not None:
            def on_delta(delta: str, _role: str = role.value) -> None:
                opts.on_role_delta(_role, delta)

        # Capture real token counts from the provider's final chunk.
        usage_holder: dict[str, int | None] = {"prompt": None, "completion": None}

        def on_finish(prompt_tokens: int | None, completion_tokens: int | None) -> None:
            usage_holder["prompt"] = prompt_tokens
            usage_holder["completion"] = completion_tokens

        try:
            output = call_role(role,
                               user_input=decision.input or opts.user_message,
                               scratchpad_text=compact_scratchpad(self.state.scratchpad),
                               conversation_id=opts.conversation_id,
                               on_delta=on_delta,
                               on_finish=on_finish,
                               should_cancel=self.is_cancelled)
        except (ProviderError, RoleNotAssigned) as exc:
            push_nudge(self.state.scratchpad, iteration, f"{role.value}_failed",
                       f"Role {role.value} call failed: {exc}")
            if opts.on_role_end is not None:
                try:
                    opts.on_role_end(role.value, model_name, 0, 0)
                except Exception:  # noqa: BLE001
                    pass
            yield PipelineEvent(type="error", role=role.value, error=str(exc))
            return

        if opts.on_role_end is not None:
            try:
                opts.on_role_end(
                    role.value, model_name,
                    usage_holder["prompt"] or 0,
                    usage_holder["completion"] or 0,
                )
            except Exception:  # noqa: BLE001
                pass

        self._push_entry(ScratchpadEntry(
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

        self._push_entry(ScratchpadEntry(
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

    def _is_bare_scratchpad(self) -> bool:
        """True when the scratchpad has nothing the user would want to see —
        only the router classification and/or system nudges.

        Real tool calls (execute, write_file, fetch, …) and any non-router
        role output count as content — only the router entry and any system
        nudges are skipped, so a successful tool run is NOT treated as bare.
        """
        for e in self.state.scratchpad:
            if e.role == "router":
                continue
            if e.action == "system_nudge":
                continue
            return False
        return True

    def _scratchpad_context_block(self, max_chars: int = 6000) -> str:
        """Compress the scratchpad into a context block the chat fallback can
        feed to the model.

        Skips router classifications and system_nudges (they only describe
        flow, not data). Strips the engine's ``successfully completed:\\n``
        wrapper that the tool dispatcher adds. Truncates each entry to fit
        ``max_chars`` total so a giant fetched HTML doesn't blow the context
        window.

        Returns ``""`` when the scratchpad has nothing worth telling the
        model about — in that case the fallback just chats with the user
        message alone.
        """
        parts: list[str] = []
        used = 0
        for e in self.state.scratchpad:
            if e.role == "router":
                continue
            if e.action == "system_nudge":
                continue
            out = (e.output or "").strip()
            if not out:
                continue
            out = re.sub(r"^successfully completed:\s*\n", "", out, count=1).strip()
            if not out:
                continue
            avail = max_chars - used
            if avail <= 200:
                break
            head = f"[{e.role}/{e.action}]"
            inp = (e.input or "").strip()
            if inp:
                head += f" input={inp[:200]}"
            snippet = (out if len(out) <= avail
                       else out[:avail].rstrip() + "\n...[truncated]")
            parts.append(f"{head}\n{snippet}")
            used += len(snippet) + len(head) + 2
        return "\n\n".join(parts)

    def _is_router_echo(self, text: str) -> bool:
        """True when ``text`` is a non-answer the chat fallback should
        replace — either empty, just the router's classification word, the
        legacy step-by-step echo, or build_final_output's "no summary"
        placeholder (which fires when the scratchpad has no surfaceable
        content — typically just a router entry).

        Used after _handle_done to detect "the pipeline produced no real
        reply" so we can fire the chat fallback.
        """
        norm = (text or "").strip().lower()
        if not norm:
            return True
        # build_final_output's terminal fallback when nothing else surfaced.
        if "produced no summary" in norm:
            return True
        router_entry = next(
            (e for e in self.state.scratchpad if e.role == "router"),
            None,
        )
        if router_entry is None:
            return False
        cat = (router_entry.output or "").strip().lower()
        if not cat:
            return False
        if norm == cat:
            return True
        # Legacy: build_final_output used to leak "### Step 0: router/classify"
        # on bare scratchpads. Step 4 now excludes router rows, so this is a
        # safety net for cases I haven't anticipated.
        if "router/classify" in norm and cat in norm:
            return True
        return False

    def _chat_fallback_stream(
        self, user_message: str, opts: "RunOptions",
    ) -> Iterator["PipelineEvent"]:
        """Stream a direct chat reply against the orchestrator's resolved
        model, bypassing the orchestrator's JSON-heavy system prompt.

        Used when the iteration loop reaches ``done`` with no payload AND
        nothing meaningful in scratchpad — typical for casual greetings or
        single-line questions that don't warrant a full pipeline.
        """
        rr = resolve_role(Role.ORCHESTRATOR)
        if rr is None:
            yield PipelineEvent(
                type="error",
                error="no orchestrator assigned — can't produce a reply",
            )
            return

        model_name = rr.model
        num_ctx = rr.context_window_tokens
        marker_role = "chat"

        if opts.on_role_start is not None:
            try:
                opts.on_role_start(marker_role, model_name, num_ctx)
            except Exception:  # noqa: BLE001
                pass

        on_delta = None
        if opts.on_role_delta is not None:
            def on_delta(delta: str, _r: str = marker_role) -> None:
                opts.on_role_delta(_r, delta)

        provider = resolve_provider(rr.provider_id)

        # The chat fallback also gets the live tool registry appended so a
        # user asking "what tools do you have?" via casual chat gets an
        # honest answer rather than a hallucinated or generic list.
        system_content = CHAT_FALLBACK_SYSTEM_PROMPT
        appendix = tool_registry_appendix()
        if appendix:
            system_content = system_content.rstrip() + "\n" + appendix + "\n"

        # When the pipeline already did real work (fetch / execute / a role
        # call) but build_final_output dropped it (because non-execute tool
        # entries are filtered out, and the orchestrator emitted done before
        # summarizing), surface that work to the chat model so it can
        # actually answer using the data instead of producing a generic
        # "I don't have access to …" reply.
        context_block = self._scratchpad_context_block()
        if context_block:
            user_text = (
                f"User asked: {user_message}\n\n"
                f"--- Pipeline observations ---\n"
                f"{context_block}\n"
                f"--- End observations ---\n\n"
                f"Answer the user's question using the data above. If the "
                f"data is HTML or raw output, extract the answer from it."
            )
        else:
            user_text = user_message

        messages = [
            ChatMessage(role="system", content=system_content),
            ChatMessage(role="user", content=user_text),
        ]
        collected: list[str] = []
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        fallback_started = time.time()
        try:
            for chunk in provider.chat(
                model=model_name, messages=messages,
                num_ctx=num_ctx, json_mode=False,
                should_cancel=self.is_cancelled,
            ):
                if chunk.content:
                    collected.append(chunk.content)
                    if on_delta:
                        on_delta(chunk.content)
                if chunk.done:
                    prompt_tokens = chunk.prompt_tokens
                    completion_tokens = chunk.completion_tokens
        except (ProviderError, Exception) as exc:  # noqa: BLE001
            yield PipelineEvent(
                type="error",
                error=f"chat fallback failed: {type(exc).__name__}: {exc}",
            )
            return

        if opts.on_role_end is not None:
            try:
                opts.on_role_end(
                    marker_role, model_name,
                    prompt_tokens or 0, completion_tokens or 0,
                )
            except Exception:  # noqa: BLE001
                pass

        # Record this in token_usage so /status sees the chat-fallback
        # call too. Without this row the bar would underrepresent
        # casual-conversation turns where the orchestrator-as-chat path
        # produces the answer directly.
        fallback_duration_ms = int((time.time() - fallback_started) * 1000)
        try:
            from agentcommander.db.repos import insert_token_usage, record_throughput
            insert_token_usage(
                conversation_id=opts.conversation_id,
                role=marker_role,
                provider_id=rr.provider_id,
                model=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=fallback_duration_ms,
            )
            # Same running-average update the role_call path does, so the
            # bar's "@ N t/s" stays accurate even when the chat fallback
            # is what drove this turn.
            record_throughput(model_name, completion_tokens, fallback_duration_ms)
        except Exception:  # noqa: BLE001
            pass

        final = "".join(collected).strip()
        if not final:
            final = "(model returned no content)"

        # Drop a record into the scratchpad so audits / postmortems can see
        # that a chat-fallback fired instead of a real role chain. Persisted
        # via _push_entry so this turn's reply is visible to the next turn's
        # context build.
        self._push_entry(ScratchpadEntry(
            step=self.state.iteration,
            role=marker_role,
            action="reply",
            input=user_message,
            output=final,
            timestamp=time.time(),
        ))

        yield PipelineEvent(type="done", final=final)


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
