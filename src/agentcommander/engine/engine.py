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
    ProviderRateLimited,
    resolve as resolve_provider,
)
from agentcommander.tools.dispatcher import invoke as invoke_tool
from agentcommander.types import LoopState, OrchestratorDecision, Role, ScratchpadEntry


CHAT_FALLBACK_SYSTEM_PROMPT = (
    "You are a helpful assistant in a CLI. Respond directly and concisely "
    "to the user's message. Plain text only — no JSON, no markdown headers.\n"
    "\n"
    "You are NOT the orchestrator and you CANNOT call tools from this role. "
    "Do NOT emit tool-call syntax such as `fetch <url>`, `read_file <path>`, "
    "`execute ...`, etc. — that is not a valid reply, it is shell syntax "
    "the user cannot run. If you need data you don't have, say so plainly "
    "(e.g. \"I don't have live weather data — try asking again so the "
    "orchestrator fetches it\").\n"
    "\n"
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

    type: str  # iteration | role | role_delta | tool | guard | done | error | retry | swap
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
    # Retry events (rate-limit backoff): which attempt + how long to wait.
    # The renderer formats these as a countdown line that updates every
    # 15 s during the wait — e.g. "rate-limited; retrying in 1:45 (attempt
    # 2/5)". Don't pollute scratchpad / messages with these.
    retry_attempt: int | None = None
    retry_max: int | None = None
    retry_wait_seconds: int | None = None
    # Swap events (rate-limit on OR provider → switch to alternate model).
    # Display: "↪ swap on coder: <from> → <to> (attempt N/M)". Like retry
    # events, never enters scratchpad.
    swap_from_model: str | None = None
    swap_to_model: str | None = None
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

        Sanitizes ``input`` / ``output`` of ASCII control bytes (0x00–0x08,
        0x0B–0x1F, 0x7F) and ANSI escape sequences before storing. The
        scratchpad is fed back into role prompts on subsequent iterations
        — so any control char emitted by a model would otherwise burn
        tokens, possibly confuse downstream model parsing, and clutter
        debug output. Tabs / newlines / CR are kept (legitimate
        formatting). Sanitization happens once at write-time so reads
        and re-renders all see clean text.
        """
        from agentcommander.engine.scratchpad import sanitize_scratchpad_text
        if isinstance(entry.input, str):
            entry.input = sanitize_scratchpad_text(entry.input)
        if isinstance(entry.output, str):
            entry.output = sanitize_scratchpad_text(entry.output)
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

        Cross-turn hydration also skips ``chat/reply`` entries — those
        are the chat-fallback's user-facing reply from a prior turn,
        and showing them to this turn's orchestrator caused round-24
        leak symptoms (the prior turn's haiku reply got copied verbatim
        into the next turn's answer). The user-view ``messages`` table
        is the right place for cross-turn conversational memory.
        Router classifications are also dropped — they describe routing
        for a different question and only confuse model-side context.
        """
        try:
            from agentcommander.db.repos import list_scratchpad_entries
            rows = list_scratchpad_entries(self.opts.conversation_id)
        except Exception:  # noqa: BLE001
            return
        for r in rows:
            role = r["role"]
            action = r["action"]
            # Skip prior chat-fallback replies — they're conversational
            # output, not work product. The model would copy them.
            if role == "chat" and action == "reply":
                continue
            # Skip prior router classifications — they tagged a
            # different question and would mislead the orchestrator
            # if treated as ongoing context.
            if role == "router":
                continue
            self.state.scratchpad.append(ScratchpadEntry(
                step=r["step"],
                role=role,
                action=action,
                input=r["input"] or "",
                output=r["output"] or "",
                timestamp=r["timestamp"],
                duration_ms=r["duration_ms"],
                content=r["content"],
                message_id=r["message_id"],
                replaced_message_ids=r["replaced_message_ids"],
            ))
        # Mark where this turn's entries begin. Anything appended after
        # this point belongs to the current run; build_final_output uses
        # the boundary to scope user-visible output candidates so prior
        # turns' tool results can't leak forward.
        self.state.turn_start_idx = len(self.state.scratchpad)

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
        # If conversation_id doesn't reference a real row, the FK in
        # pipeline_runs would raise IntegrityError and the entire run
        # would crash before yielding any events. Catch that, surface a
        # clean error event, and stop — instead of letting the exception
        # propagate to whoever's iterating events().
        try:
            insert_pipeline_run(self.run_id, opts.conversation_id)
        except Exception as exc:  # noqa: BLE001
            yield PipelineEvent(
                type="error",
                error=(f"failed to record pipeline run "
                       f"(conversation_id={opts.conversation_id!r}): "
                       f"{type(exc).__name__}: {exc}"),
            )
            return

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
            try:
                category = yield from self._classify_category(opts.user_message, opts)
            except ProviderRateLimited as exc:
                # Five retries exhausted on the router. Surface a clean
                # error and stop without polluting the scratchpad.
                yield PipelineEvent(
                    type="error",
                    error="rate-limited (provider blocked our retries) — "
                          "try /autoconfig clear to switch providers, "
                          "or wait and retry your prompt",
                )
                update_pipeline_run(self.run_id, status="failed",
                                    iterations=0, error=f"rate-limited: {exc}",
                                    category="?")
                return
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
                    # No postmortem on /stop — the role prompt explicitly
                    # excludes "user explicitly cancelled" from the patterns
                    # worth analyzing.
                    return

                self.state.iteration = iteration
                yield PipelineEvent(type="iteration", iteration=iteration)

                # Preflight reorder injection. When the previous iteration's
                # preflight returned "reorder", the prerequisite steps were
                # pushed onto state.preflight_queue (with the original
                # action appended last). Drain the queue head-first
                # *before* asking the orchestrator for a fresh decision,
                # so the prereq sequence runs in the order preflight asked
                # for. Once the queue empties, control reverts to the
                # orchestrator. This keeps the meta-agent reorder behavior
                # local to the engine — guards and dispatch see normal
                # OrchestratorDecisions and don't need to know preflight
                # exists.
                if self.state.preflight_queue:
                    decision = self.state.preflight_queue.pop(0)
                    yield PipelineEvent(
                        type="guard", family="preflight",
                        reason=f"preflight-injected step: {decision.action}",
                    )
                else:
                    try:
                        decision = yield from self._orchestrate(opts)
                    except ProviderRateLimited as exc:
                        # Retries exhausted on the orchestrator. Surface a
                        # clean error and stop — DO NOT include the raw 429
                        # body in the user-visible event (it gets logged via
                        # update_pipeline_run for /history, but the user sees
                        # only the actionable message). Mirror already saw
                        # the live countdown via retry events.
                        yield PipelineEvent(
                            type="error",
                            error="rate-limited (provider blocked our retries) — "
                                  "/autoconfig clear to switch providers, "
                                  "or wait and re-run",
                        )
                        update_pipeline_run(self.run_id, status="failed",
                                            iterations=iteration,
                                            error=f"rate-limited: {exc}",
                                            category=category)
                        self._maybe_run_postmortem(
                            final_status="failed",
                            error_text=f"rate-limited: {exc}",
                        )
                        return
                    except (ProviderError, RoleNotAssigned) as exc:
                        yield PipelineEvent(type="error", error=str(exc))
                        update_pipeline_run(self.run_id, status="failed",
                                            iterations=iteration, error=str(exc),
                                            category=category)
                        self._maybe_run_postmortem(
                            final_status="failed",
                            error_text=str(exc),
                        )
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

                # Preflight meta-agent (opt-in via config "preflight_enabled").
                # Skipped on `done` (terminal — preflight has nothing to add)
                # and on preflight-injected steps (we already vetted them
                # the iteration we created them, re-checking would loop).
                # When verdict is "abort", surface as a friendly final and
                # stop. When "reorder", queue the prereqs + the original
                # action and `continue` so the next iteration drains them.
                if (decision.action != "done"
                        and not self._was_preflight_injected(decision)
                        and self._preflight_enabled()):
                    verdict = self._run_preflight(decision)
                    if verdict.verdict == "abort":
                        yield PipelineEvent(
                            type="guard", family="preflight",
                            reason=f"preflight aborted: {verdict.reason}",
                        )
                        final_msg = (
                            f"Preflight halted the pipeline:\n\n"
                            f"  {verdict.reason}\n\n"
                            f"Disable preflight with `/preflight off` to "
                            f"force the action through, or rephrase your "
                            f"request to avoid the flagged hazard."
                        )
                        yield PipelineEvent(type="done", final=final_msg)
                        update_pipeline_run(
                            self.run_id, status="failed",
                            iterations=iteration,
                            error=f"preflight abort: {verdict.reason}",
                            category=category,
                        )
                        self._maybe_run_postmortem(
                            final_status="failed",
                            error_text=f"preflight abort: {verdict.reason}",
                        )
                        return
                    if verdict.verdict == "reorder" and verdict.reorder_steps:
                        # Mark each injected step so the next iteration
                        # skips re-running preflight on it (avoids
                        # infinite preflight-on-preflight-step loops).
                        for step in verdict.reorder_steps:
                            step.reasoning = (
                                f"[preflight-injected] {step.reasoning or ''}"
                            )
                        # Tail of the queue is the original decision so the
                        # user's intent still runs after the prereqs. Mark
                        # it too — its prereqs already passed preflight,
                        # re-checking the same action wastes a call.
                        decision.reasoning = (
                            f"[preflight-injected] {decision.reasoning or ''}"
                        )
                        self.state.preflight_queue.extend(
                            verdict.reorder_steps + [decision]
                        )
                        yield PipelineEvent(
                            type="guard", family="preflight",
                            reason=(f"preflight reorder: queued "
                                    f"{len(verdict.reorder_steps)} prereq(s) "
                                    f"before {decision.action}"),
                        )
                        continue

                # Done branch
                if decision.action == "done":
                    final = self._handle_done(decision, opts.user_message)
                    if final is None:
                        # Track repeated premature-done rejections. Trivia /
                        # short Q&A often produces 2-3 rejections in a row
                        # because the orchestrator re-emits the same shape
                        # (empty input or router echo). Without this short-
                        # circuit, the run burns iterations on a guaranteed-
                        # to-loop sequence; after 2 rejections we know the
                        # orchestrator can't converge and we hand off to the
                        # chat fallback (which writes a real answer).
                        self.state.premature_done_count = (
                            getattr(self.state, "premature_done_count", 0) + 1
                        )
                        yield PipelineEvent(type="guard", family="done",
                                            reason="rejecting premature done")
                        if self.state.premature_done_count >= 2:
                            yield PipelineEvent(
                                type="guard", family="done",
                                reason="2 premature dones — handing off to chat fallback",
                            )
                            yield from self._chat_fallback_stream(
                                opts.user_message, opts,
                            )
                            update_pipeline_run(
                                self.run_id, status="done",
                                iterations=iteration, category=category,
                            )
                            return
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

                    # Scratchpad-leak hallucination — the orchestrator
                    # parroted our own ``successfully completed:``
                    # wrapper. Round-22 caught this. Fire chat fallback
                    # so the user gets a fresh attempt at the actual
                    # question.
                    if self._is_scratchpad_leak(final):
                        yield PipelineEvent(
                            type="guard", family="done",
                            reason="rejecting scratchpad-leak in done.input",
                        )
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
                        "turn_start_idx": self.state.turn_start_idx,
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
            self._maybe_run_postmortem(
                final_status="max_iterations",
                error_text=f"max iterations ({self._max_iterations})",
            )

        except Exception as exc:  # noqa: BLE001 — outermost engine boundary
            yield PipelineEvent(type="error", error=f"{type(exc).__name__}: {exc}")
            update_pipeline_run(self.run_id, status="failed",
                                iterations=self.state.iteration, error=str(exc))
            self._maybe_run_postmortem(
                final_status="failed",
                error_text=f"{type(exc).__name__}: {exc}",
            )

    # ── Meta-agent helpers (preflight + postmortem) ──────────────────────

    def _preflight_enabled(self) -> bool:
        """Read the persisted ``preflight_enabled`` config flag.

        Default: disabled. Preflight adds one extra LLM call per iteration
        — opt-in only so casual chat usage doesn't pay the cost. Toggle
        with ``/preflight on`` / ``/preflight off``.
        """
        try:
            from agentcommander.db.repos import get_config
            raw = get_config("preflight_enabled", None)
        except Exception:  # noqa: BLE001
            return False
        if raw is None:
            return False
        s = str(raw).strip().lower()
        return s in ("1", "true", "yes", "on", "enabled")

    def _postmortem_enabled(self) -> bool:
        """Read the persisted ``postmortem_enabled`` config flag.

        Default: disabled. Postmortem runs only on FAILED runs but still
        costs one LLM call per failure — opt-in. Toggle with
        ``/postmortem on`` / ``/postmortem off``.
        """
        try:
            from agentcommander.db.repos import get_config
            raw = get_config("postmortem_enabled", None)
        except Exception:  # noqa: BLE001
            return False
        if raw is None:
            return False
        s = str(raw).strip().lower()
        return s in ("1", "true", "yes", "on", "enabled")

    def _was_preflight_injected(self, decision: OrchestratorDecision) -> bool:
        """Detect a decision that was injected by a prior preflight reorder.

        We tag injected decisions by prepending ``[preflight-injected]`` to
        their ``reasoning`` field. Re-checking such steps would loop
        (preflight on a preflight-approved step would re-emit the same
        prereqs forever).
        """
        r = decision.reasoning or ""
        return r.startswith("[preflight-injected]")

    def _run_preflight(self, decision: OrchestratorDecision):
        """Invoke the preflight meta-agent for ``decision``. Lazy import so
        this module's load-time graph doesn't pull in the meta-agent code
        unless preflight is actually enabled."""
        from agentcommander.engine.meta_agents import apply_preflight
        return apply_preflight(
            decision,
            scratchpad=self.state.scratchpad,
            conversation_id=self.opts.conversation_id,
            should_cancel=self.is_cancelled,
        )

    def _maybe_run_postmortem(self, *, final_status: str,
                              error_text: str | None) -> None:
        """Run the postmortem meta-agent on a failed pipeline (opt-in).

        Best-effort: any exception is swallowed (audited) so a flaky
        postmortem can't break the failure path. The postmortem's
        outputs (rules, retry proposals, user prompts) are persisted /
        audited inside ``apply_postmortem``; this caller only needs to
        gate on the config flag.
        """
        if not self._postmortem_enabled():
            return
        try:
            from agentcommander.engine.meta_agents import apply_postmortem
            apply_postmortem(
                run_id=self.run_id,
                conversation_id=self.opts.conversation_id,
                scratchpad=self.state.scratchpad,
                final_status=final_status,
                error_text=error_text,
                should_cancel=self.is_cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                audit("postmortem.invocation_failed",
                      {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass

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

    # ── Rate-limit retry helper ──────────────────────────────────────
    #
    # Provider rate-limits (HTTP 429) bubble up as ProviderRateLimited.
    # Generic ProviderErrors stay generic and surface as run failures
    # (with scratchpad nudges), but rate-limits are infra noise — we
    # want to wait, retry, and keep the model's context clean.
    #
    # Schedule: 60s, 120s, 240s, 480s, 960s (1 / 2 / 4 / 8 / 16 min).
    # After 5 retries we give up and yield a real error event. Server
    # ``Retry-After`` hints raise the wait above the schedule when
    # they're longer; never below.

    _RATE_LIMIT_BACKOFF_S: tuple[int, ...] = (60, 120, 240, 480, 960)
    _RETRY_ANNOUNCE_INTERVAL_S: int = 15

    def _label_to_role(self, action_label: str) -> "Role | None":
        """Map a rate-limit ``action_label`` back to a ``Role`` enum.

        Call sites pass the role.value verbatim for direct delegations
        (``"coder"``, ``"orchestrator"``); the router and chat-fallback
        paths pass static labels (``"classify"``, ``"orchestrate"``,
        ``"chat"``) which we map explicitly.
        """
        try:
            return Role(action_label)
        except ValueError:
            label_map = {
                "classify": Role.ROUTER,
                "orchestrate": Role.ORCHESTRATOR,
                "chat": Role.ORCHESTRATOR,
            }
            return label_map.get(action_label)

    def _is_or_provider_for(self, role: "Role | None") -> str | None:
        """Return the OR provider type ("openrouter-free" / "openrouter-paid")
        if this role's provider is one, else None. Used to gate the
        swap-on-429 fast path — Ollama / llama.cpp 429s still get the
        slow backoff because they don't have a per-tier catalog of
        alternates to swap between.
        """
        if role is None:
            return None
        rr = resolve_role(role)
        if rr is None:
            return None
        try:
            from agentcommander.providers.base import resolve as resolve_provider
            provider = resolve_provider(rr.provider_id)
        except Exception:  # noqa: BLE001
            return None
        ptype = getattr(provider, "type", None)
        if ptype in ("openrouter-free", "openrouter-paid"):
            return ptype
        return None

    def _swap_role_to(self, role: "Role", new_model: str) -> bool:
        """Update the role's DB assignment to ``new_model`` so the next
        ``call_role(role, ...)`` re-resolves to it. Preserves ``is_override``
        so the swap survives subsequent autoconfig runs (this IS a
        deliberate user-facing pin once chosen).

        Pulls ``contextLength`` from the catalog for the new model so the
        bar shows the right ctx cap on the next role-start.
        """
        from agentcommander.db.repos import get_role_assignment, set_role_assignment
        from agentcommander.typecast.openrouter_catalog import (
            TIER_FREE, TIER_PAID, load,
        )
        existing = get_role_assignment(role)
        if existing is None:
            return False
        provider_id = existing["provider_id"]
        # Look up ctx from whichever tier the new model belongs to.
        ctx = existing.get("context_window_tokens")
        for tier in (TIER_FREE, TIER_PAID):
            cat = load(tier)
            entry = cat.get("_models", {}).get(new_model)
            if entry and isinstance(entry.get("contextLength"), int):
                ctx = int(entry["contextLength"])
                break
        try:
            set_role_assignment(
                role=role, provider_id=provider_id, model=new_model,
                is_override=True, context_window_tokens=ctx,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def _pick_alternate(self, provider_type: str, role: "Role",
                        exclude: set[str]) -> str | None:
        """Pick the next-best model from the appropriate catalog for
        ``role``, skipping any model in ``exclude`` (rate-limited
        this run) and any model whose composite (vote + capability)
        rank for THIS role is below zero.

        Composite ranking matches ``pick_for_role`` so a swap during
        a run lands on the same model the next ``/autoconfig`` would
        — consistent behaviour avoids surprises.
        """
        from agentcommander.typecast.agent_requirements import (
            is_eligible, score_match,
        )
        from agentcommander.typecast.openrouter_catalog import (
            TIER_FREE, TIER_PAID, load,
        )
        tier = TIER_FREE if provider_type == "openrouter-free" else TIER_PAID
        catalog = load(tier)
        models = catalog.get("_models") or {}

        role_key = role.value

        def _stats(entry):
            return (entry.get("by_role") or {}).get(role_key, {}) or {}

        candidates = []
        for mid, e in models.items():
            if mid in exclude:
                continue
            if not is_eligible(role_key, mid, e):
                continue
            per_role = int(_stats(e).get("score", 0))
            # Filter only on real vote evidence (matches pick_for_role).
            # Capability bonus shapes ranking but doesn't exclude.
            if per_role < 0:
                continue
            bonus = score_match(role_key, mid, e)
            composite = per_role + bonus
            candidates.append((mid, e, composite))

        if not candidates:
            return None

        def _key(item):
            mid, e, composite = item
            succ = int(_stats(e).get("successes", 0))
            return (-composite, -succ, mid)

        candidates.sort(key=_key)
        return candidates[0][0]

    # Hint accumulator — independent of the OpenRouter provider-side vote
    # system. The OR catalog tracks per-tier model votes for swap-on-429
    # behavior. This table is the LOCAL hint accumulator (model_hints DB
    # table) that adjusts the autoconfig score on the next startup so a
    # chronically-failing model gets dropped from the threshold cascade
    # automatically. Bumps are small (±0.1) so steady-state behavior takes
    # many runs to drift; clamped to ±100 in the repo helper.
    HINT_BUMP_SUCCESS: float = 0.1
    HINT_BUMP_FAILURE: float = -0.1

    def _bump_hint_for_label(self, action_label: str, delta: float) -> None:
        """Apply a ±delta hint bump for whatever (model, role) corresponds
        to ``action_label``. Best-effort; any DB / mapping failure is
        swallowed so a flaky hint store can't break a successful run.

        ``action_label`` is the same shape ``_record_failure_vote`` uses:
        a Role.value for direct delegations, or one of the generic labels
        (``"classify"`` / ``"orchestrate"`` / ``"chat"``).
        """
        role_enum = self._label_to_role(action_label)
        if role_enum is None:
            return
        rr = resolve_role(role_enum)
        if rr is None:
            return
        try:
            from agentcommander.db.repos import bump_hint
            bump_hint(rr.model, role_enum.value, delta)
        except Exception:  # noqa: BLE001
            pass

    def _record_failure_vote(self, action_label: str) -> None:
        """Best-effort -1 vote (no sibling boost) for the model that just
        failed to PERFORM for this role. Asymmetric to rate-limit voting
        on purpose — a quality failure on one model is specific to that
        model, not signal about the others.

        Triggered from the engine's non-rate-limit error paths:
        ProviderError fallthrough, JSON parse failures, etc. Maps the
        ``action_label`` to a Role the same way ``_record_rate_limit_vote``
        does so the vote lands on the right (model, role) pair.
        """
        try:
            from agentcommander.providers.base import resolve as resolve_provider
            from agentcommander.typecast.openrouter_catalog import (
                vote_after_failure_for_provider,
            )
        except Exception:  # noqa: BLE001
            return
        role_enum = self._label_to_role(action_label)
        if role_enum is None:
            return
        rr = resolve_role(role_enum)
        if rr is None:
            return
        try:
            provider = resolve_provider(rr.provider_id)
        except Exception:  # noqa: BLE001
            return
        provider_type = getattr(provider, "type", None)
        try:
            vote_after_failure_for_provider(
                provider_type, rr.model, role_enum.value,
            )
        except Exception:  # noqa: BLE001
            pass

    def _record_rate_limit_vote(self, action_label: str) -> None:
        """Best-effort vote-down for the model currently being throttled.

        Maps ``action_label`` (which the call sites pass as the role
        ``Role.value`` for orchestrator/coder/etc., or a generic string
        like "classify"/"chat" for non-role calls) back to a Role to
        look up the resolved provider/model. When the mapping fails
        (generic label) we skip voting — there's no clean way to
        attribute a 429 to a specific role without it.
        """
        try:
            from agentcommander.providers.base import resolve as resolve_provider
            from agentcommander.typecast.openrouter_catalog import (
                vote_after_rate_limit_for_provider,
            )
        except Exception:  # noqa: BLE001
            return
        # Identify the role. ``action_label`` is the role.value for
        # _dispatch_role calls, which is the case we care about most.
        # Treat "classify" / "orchestrate" / "chat" as separate roles
        # too so the router and orchestrator are votable.
        try:
            role_enum = Role(action_label)
        except ValueError:
            # Map the generic labels back to their actual roles.
            label_map = {
                "classify": Role.ROUTER,
                "orchestrate": Role.ORCHESTRATOR,
                "chat": Role.ORCHESTRATOR,
            }
            role_enum = label_map.get(action_label)
            if role_enum is None:
                return
        rr = resolve_role(role_enum)
        if rr is None:
            return
        try:
            provider = resolve_provider(rr.provider_id)
        except Exception:  # noqa: BLE001
            return
        provider_type = getattr(provider, "type", None)
        try:
            vote_after_rate_limit_for_provider(
                provider_type, rr.model, role_enum.value,
            )
        except Exception:  # noqa: BLE001
            pass

    def _retry_on_rate_limit(self, action_label: str,
                             fn) -> "Iterator[PipelineEvent]":
        """Generator wrapper that retries ``fn()`` on ProviderRateLimited.

        Three phases, picked automatically:

        **Phase 1: Fast-swap (OR providers only)** — on 429, vote
        (-1 failing, +1 siblings), pick next-best from catalog
        excluding rate-limited-this-run, swap role's DB assignment,
        retry immediately. Loop until success or catalog exhausts.
        No sleep — rotating to a fresh model is faster than waiting
        on a throttled one.

        **Phase 2: Slow backoff** — kicks in when phase 1 exhausts
        the catalog (every model in this tier hit 429), or
        immediately for non-OR providers (Ollama / llama.cpp don't
        have catalog alternatives). Schedule 60→120→240→480→960s,
        /stop-aware sleep, 15-second countdown announces. Up to ~16
        minutes total wait spread across 5 attempts. Before each
        retry the OR variant re-picks the best-voted model from the
        catalog so the wait gives every model a chance to recover.

        **Phase 3: Surrender** — after backoff exhausts, re-raise
        ProviderRateLimited so the caller can yield a clean error
        event and end the run.

        ``self.is_cancelled()`` honored throughout — /stop breaks the
        sleep within ~1 second.

        Usage::

            result = yield from self._retry_on_rate_limit("orchestrate",
                lambda: call_role(...))
        """
        role = self._label_to_role(action_label)
        or_provider_type = self._is_or_provider_for(role)

        # ── Phase 1: Fast swap for OR providers ─────────────────────
        if or_provider_type and role is not None:
            rate_limited_this_run: set[str] = set()
            from agentcommander.typecast.openrouter_catalog import (
                TIER_FREE, TIER_PAID, load,
            )
            tier = TIER_FREE if or_provider_type == "openrouter-free" else TIER_PAID
            max_attempts = len(load(tier).get("_models", {})) + 1

            from agentcommander.db.repos import get_role_assignment

            catalog_exhausted = False
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn()
                except ProviderRateLimited:
                    if self.is_cancelled():
                        raise
                    # Vote first so swap picks reflect this run's losers.
                    self._record_rate_limit_vote(action_label)
                    # Mark the current model as off-limits for this run.
                    cur = get_role_assignment(role)
                    cur_model = cur["model"] if cur else None
                    if cur_model:
                        rate_limited_this_run.add(cur_model)
                    # Find an alternate.
                    alt = self._pick_alternate(or_provider_type, role,
                                                rate_limited_this_run)
                    if alt is None:
                        # Catalog exhausted — fall through to Phase 2
                        # (slow backoff). Don't raise yet; the wait
                        # gives throttled models a chance to recover.
                        catalog_exhausted = True
                        break
                    # Swap and retry.
                    if not self._swap_role_to(role, alt):
                        catalog_exhausted = True
                        break
                    yield PipelineEvent(
                        type="swap",
                        role=role.value,
                        swap_from_model=cur_model,
                        swap_to_model=alt,
                        retry_attempt=attempt,
                        retry_max=max_attempts,
                    )
            # If we hit max_attempts without breaking, treat as exhausted.
            if not catalog_exhausted:
                catalog_exhausted = True

            # Tell the user we're switching to slow mode so the long
            # wait isn't a surprise. One-line announcement before the
            # backoff schedule kicks in.
            yield PipelineEvent(
                type="retry",
                reason=f"{action_label} (catalog exhausted; falling back to slow retries)",
                retry_attempt=0,
                retry_max=len(self._RATE_LIMIT_BACKOFF_S),
                retry_wait_seconds=self._RATE_LIMIT_BACKOFF_S[0],
            )
            # Reset the per-run set so phase 2 can try ANY model again
            # — enough time will pass during the sleep that previously
            # throttled models may have recovered their per-minute quota.
            rate_limited_this_run.clear()

        # ── Phase 2: Slow backoff schedule ──────────────────────────
        # Used as fall-through after OR catalog exhausts AND as the
        # primary path for non-OR providers (Ollama / llama.cpp).
        schedule = self._RATE_LIMIT_BACKOFF_S
        last_exc: ProviderRateLimited | None = None
        for attempt, base_wait in enumerate(schedule, start=1):
            # For OR providers in fall-through, re-pick the best-voted
            # model from the catalog before each retry. The slow wait
            # gave throttled models a chance to recover; we want to
            # retry the most reliable one, not the last-failed one.
            if or_provider_type and role is not None:
                from agentcommander.typecast.openrouter_catalog import (
                    pick_for_role,
                )
                tier = (
                    "free" if or_provider_type == "openrouter-free" else "paid"
                )
                best = pick_for_role(tier, role.value, fallback=None)
                if best:
                    self._swap_role_to(role, best)
            try:
                return fn()
            except ProviderRateLimited as exc:
                last_exc = exc
                # Vote down the failing model (and boost siblings) so
                # repeat throttling on this model permanently shifts
                # autoconfig away from it. Best-effort: never let a
                # vote failure abort the retry loop.
                self._record_rate_limit_vote(action_label)
                # Server-suggested wait wins if it's longer than our slot.
                wait = max(int(base_wait), int(exc.retry_after or 0))
                # Initial announcement for this attempt.
                yield PipelineEvent(
                    type="retry",
                    reason=action_label,
                    retry_attempt=attempt,
                    retry_max=len(schedule),
                    retry_wait_seconds=wait,
                )
                # Cancellation-aware sleep with periodic countdown.
                elapsed = 0.0
                next_announce_at = wait - self._RETRY_ANNOUNCE_INTERVAL_S
                while elapsed < wait:
                    if self.is_cancelled():
                        # Re-raise so the caller's surrounding try/except
                        # flows can convert this to a "cancelled" event.
                        raise
                    time.sleep(1.0)
                    elapsed += 1.0
                    remaining = int(wait - elapsed)
                    if remaining > 0 and remaining <= next_announce_at:
                        yield PipelineEvent(
                            type="retry",
                            reason=action_label,
                            retry_attempt=attempt,
                            retry_max=len(schedule),
                            retry_wait_seconds=remaining,
                        )
                        next_announce_at = remaining - self._RETRY_ANNOUNCE_INTERVAL_S
        # All attempts exhausted — let the caller decide what to do
        # (typically: yield a final error event and stop the pipeline).
        if last_exc is not None:
            raise last_exc
        raise ProviderRateLimited("rate limit retries exhausted")

    def _classify_category(self, user_message: str,
                           opts: "RunOptions") -> "Iterator[PipelineEvent]":
        """Generator: yields retry events on rate-limit, returns the
        category string via StopIteration.value (caller uses ``yield from``)."""
        if resolve_role(Role.ROUTER) is None:
            return "question"
        # Tell the bar the router is about to run, before the network call.
        model_name, _ = self._emit_role_start(Role.ROUTER, opts)
        prompt_tokens = completion_tokens = 0

        def _capture(p: int | None, c: int | None) -> None:
            nonlocal prompt_tokens, completion_tokens
            prompt_tokens = p or 0
            completion_tokens = c or 0

        def _do_call() -> str:
            return call_role(Role.ROUTER, user_input=user_message,
                             conversation_id=self.opts.conversation_id,
                             json_mode=True, on_finish=_capture,
                             should_cancel=self.is_cancelled)

        try:
            raw = yield from self._retry_on_rate_limit("classify", _do_call)
            parsed = json.loads(raw)
            result = str(parsed.get("category", "question"))
            self._bump_hint_for_label("classify", self.HINT_BUMP_SUCCESS)
        except (ProviderError, RoleNotAssigned, ValueError, json.JSONDecodeError):
            # Router failed to produce a parseable category. Quality
            # downvote (no sibling boost) so persistent router-format
            # offenders sink in the rankings over time.
            self._record_failure_vote("classify")
            self._bump_hint_for_label("classify", self.HINT_BUMP_FAILURE)
            result = "question"
        self._emit_role_end(Role.ROUTER, model_name, opts,
                            prompt_tokens, completion_tokens)
        return result

    def _orchestrate(self, opts: "RunOptions") -> "Iterator[PipelineEvent]":
        """Generator: yields retry events on rate-limit, returns
        OrchestratorDecision via StopIteration.value.

        The retry helper raises ProviderRateLimited if all attempts are
        exhausted — let it propagate so events() catches it and emits
        the run-failure error.
        """
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

        def _do_call() -> str:
            # Always pass the user's actual message as user_input. Prior
            # to the round-22 fix this was ``scratchpad_text or
            # user_message``, which silently dropped the user's question
            # any time the scratchpad was non-empty (i.e. on every turn
            # after the first). The orchestrator then saw only the
            # scratchpad as its "task" and produced summaries / parroted
            # prior tool outputs instead of answering the new question.
            # Scratchpad still goes via the dedicated ``scratchpad_text``
            # channel, where call_role threads it as a separate user
            # message labeled as prior context.
            return call_role(Role.ORCHESTRATOR,
                             user_input=self.opts.user_message,
                             scratchpad_text=scratchpad_text,
                             conversation_id=self.opts.conversation_id,
                             json_mode=True,
                             on_finish=_capture,
                             should_cancel=self.is_cancelled)
        try:
            raw = yield from self._retry_on_rate_limit("orchestrate", _do_call)
            try:
                parsed = json.loads(raw)
                decision = OrchestratorDecision.from_dict(parsed)
            except (json.JSONDecodeError, ValueError, TypeError):
                # Model emitted invalid JSON for the orchestrator role.
                # Quality failure → downvote (model, orchestrator) only.
                self._record_failure_vote("orchestrate")
                self._bump_hint_for_label("orchestrate", self.HINT_BUMP_FAILURE)
                decision = OrchestratorDecision(
                    action="done",
                    reasoning="orchestrator returned invalid JSON; halting",
                    input=build_final_output(self.state.scratchpad, self.state.turn_start_idx),
                )
            else:
                # Parsed JSON cleanly → reward the orchestrator's hint.
                self._bump_hint_for_label("orchestrate", self.HINT_BUMP_SUCCESS)
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

        def _do_call() -> str:
            return call_role(role,
                             user_input=decision.input or opts.user_message,
                             scratchpad_text=compact_scratchpad(self.state.scratchpad),
                             conversation_id=opts.conversation_id,
                             on_delta=on_delta,
                             on_finish=on_finish,
                             should_cancel=self.is_cancelled)

        try:
            output = yield from self._retry_on_rate_limit(role.value, _do_call)
        except ProviderRateLimited as exc:
            # Retries exhausted on a rate-limit. Surface a clean error WITHOUT
            # push_nudge — rate-limit text must never enter the scratchpad
            # (would teach the model to "see" infrastructure problems as part
            # of its task context). The mirror still saw the countdown via
            # the retry events that fired during the wait.
            if opts.on_role_end is not None:
                try:
                    opts.on_role_end(role.value, model_name, 0, 0)
                except Exception:  # noqa: BLE001
                    pass
            yield PipelineEvent(
                type="error", role=role.value,
                error="rate-limited (provider blocked our retries) — "
                      "/autoconfig clear to switch providers, or wait and re-run",
            )
            return
        except (ProviderError, RoleNotAssigned) as exc:
            # Quality failure on this model for this role. Per spec:
            # downvote ONLY this (model, role) — no sibling boost,
            # because a model erroring out is a quality signal specific
            # to that model, not evidence the others would do better.
            self._record_failure_vote(role.value)
            # Hint accumulator: -0.1 for the local DB scoring layer so the
            # next /autoconfig run prefers a different model for this role.
            self._bump_hint_for_label(role.value, self.HINT_BUMP_FAILURE)
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
        # Hint accumulator: +0.1 for the (model, role) pair on success.
        # Cumulatively, this rewards models that keep delivering useful
        # output for a role and lets autoconfig prefer them on the next
        # startup — even when their static catalog score is tied with
        # another candidate.
        self._bump_hint_for_label(role.value, self.HINT_BUMP_SUCCESS)
        yield PipelineEvent(type="role", role=role.value, output=output)

        # Post-step guards (dead-end, anti-stuck, repeat-error)
        if self._guards["post_step"]:
            ps = self._guards["post_step"]({
                "scratchpad": self.state.scratchpad,
                "turn_start_idx": self.state.turn_start_idx,
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
            return decision.input or build_final_output(self.state.scratchpad, self.state.turn_start_idx)
        verdict = self._guards["done"]({
            "scratchpad": self.state.scratchpad,
            "turn_start_idx": self.state.turn_start_idx,
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

        Scoped to THIS turn's entries (``state.turn_start_idx`` onward).
        Round-27 caught the chat fallback regurgitating a prior turn's
        fizzbuzz summary as the answer to "say complete" because the
        context block fed it the entire conversation's scratchpad.

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
        start = max(0, self.state.turn_start_idx)
        for e in self.state.scratchpad[start:]:
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

    def _is_scratchpad_leak(self, text: str) -> bool:
        """True when ``text`` is a verbatim copy of the engine's own
        scratchpad scaffolding rather than a real model reply.

        Round-22 stress catch: once a prior turn ran a successful tool
        action, the orchestrator's compact_scratchpad input contained the
        engine's ``successfully completed:\\n`` wrapper from
        ``_dispatch_tool``. On subsequent UNRELATED questions, the
        orchestrator (a 24B model under context pressure) sometimes
        emitted ``decision.input`` = that exact wrapped output as its
        answer — bypassing both the chat fallback and the router-echo
        check. Detecting the wrapper prefix gives us a mechanical, model-
        agnostic signal that we're looking at a leak rather than a reply,
        so we can route to ``_chat_fallback_stream`` for a fresh attempt.

        ``compact_scratchpad`` now also strips the wrapper at the prompt-
        construction layer so the model is less likely to learn the
        pattern. This check is the safety net for the case where the
        model already learned it during the run (or generates the prefix
        on its own initiative).
        """
        if not text:
            return False
        norm = text.lstrip()
        norm_lower = norm.lower()
        # 1. Engine's tool-success wrapper. Never a real reply — only
        #    _dispatch_tool produces this exact prefix at engine.py:1615.
        if norm_lower.startswith("successfully completed:"):
            return True
        # 2. Role-prompt scaffolding regurgitation. The summarizer /
        #    architect / planner prompts use phrases like "Summarize what
        #    was done" and "Work completed:" that should never appear in
        #    a model's user-facing reply — when the orchestrator emits
        #    them as ``done.input``, it's reflecting prompt template text
        #    rather than answering. Round-22 example: TEST 053 came back
        #    with "Summarize what was done. User asked: ..." instead of
        #    naming three planets.
        if norm_lower.startswith("summarize what was done"):
            return True
        # 3. Multi-test-summary hallucination loop. Once the orchestrator
        #    emits a fake "All test cases processed successfully: TEST X:
        #    ..." block (which itself is a self-reinforcing leak from a
        #    prior turn's bad output), every subsequent turn copies it.
        #    The signature is: 3+ "TEST NNN:" patterns when only one
        #    "TEST NNN" appeared in the current user message — i.e. the
        #    reply references prior turns rather than answering this one.
        import re as _re
        test_refs = _re.findall(r"\bTEST\s+\d{2,3}\b", norm)
        if len(test_refs) >= 3:
            return True
        return False

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

        # Wrap the streaming call in a closure so the retry helper can
        # re-invoke it cleanly on rate-limit. The closure resets the
        # accumulators each attempt so a partial stream from a 429'd
        # earlier attempt doesn't bleed into the next one.
        def _do_stream() -> None:
            nonlocal prompt_tokens, completion_tokens
            collected.clear()
            prompt_tokens = None
            completion_tokens = None
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

        try:
            yield from self._retry_on_rate_limit("chat", _do_stream)
        except ProviderRateLimited:
            # Retries exhausted. Surface a clean error WITHOUT including
            # the rate-limit body — the message stays user-visible only
            # via the retry events that fired during the wait.
            yield PipelineEvent(
                type="error",
                error="chat fallback rate-limited (retries exhausted) — "
                      "/autoconfig clear to switch providers, or wait and re-run",
            )
            return
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
            # is what drove this turn. Pass `chars_completed` so providers
            # that don't report usage (some llama.cpp builds) still get
            # a real tok/s number via char-based estimation.
            chars_total = sum(len(c) for c in collected)
            record_throughput(
                model_name, completion_tokens, fallback_duration_ms,
                chars_completed=chars_total,
            )
        except Exception:  # noqa: BLE001
            pass

        final = "".join(collected).strip()
        if not final:
            final = "(model returned no content)"

        # Post-guard: chat fallback is supposed to produce plain prose. When
        # the model has been primed by the tool-registry appendix and emits
        # tool syntax instead (e.g. `fetch https://wttr.in/Edmonton` or
        # `I'll list the files...\n\nlist_dir`), the model has actually told
        # us what it WANTS done. For read-only tools we can honor that
        # intent: execute the tool ourselves, then re-run the chat call
        # with the result in context so the user gets a real answer. For
        # unsafe verbs (write_file, execute, git, delete_file,
        # start_process, kill_process) we fall back to a clear error
        # rather than auto-executing arbitrary writes.
        #
        # Match policy: look at the LAST non-empty line of the final. If
        # that line is bare ``<verb>`` or ``<verb> <arg>``, treat the
        # whole final as a tool intent. This catches the multi-line case
        # where the model wraps the call in a preamble sentence
        # ("I'll list the files for you.\n\nlist_dir"), while still
        # rejecting prose that just MENTIONS a tool ("you can use fetch
        # to grab a URL.").
        lines = [l for l in (ln.strip() for ln in final.split("\n")) if l]
        last_line = lines[-1] if lines else ""
        verb_re = (
            r"(read_file|write_file|list_dir|delete_file|execute|fetch|"
            r"http_request|git|env|browser|start_process|kill_process|"
            r"check_process)"
        )
        bad_with_arg = re.match(
            r"^" + verb_re + r"\s+(?!\{)([^\s].*?)\s*$",
            last_line, re.IGNORECASE,
        )
        bad_verb_only = re.match(
            r"^" + verb_re + r"\s*$",
            last_line, re.IGNORECASE,
        )
        if (bad_with_arg or bad_verb_only) and len(last_line) <= 300:
            if bad_with_arg:
                bad_verb = bad_with_arg.group(1).lower()
                bad_arg = bad_with_arg.group(2).strip()
            else:
                bad_verb = bad_verb_only.group(1).lower()
                bad_arg = ""
            yield from self._honor_tool_text_as_intent(
                bad_verb, bad_arg, user_message, opts,
                provider, model_name, num_ctx,
                system_content, marker_role, on_delta,
            )
            return

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

    # Tools that are safe to auto-execute when the model emits them as
    # plain-text in chat fallback (HTTP-only or filesystem-read). Mutating
    # tools (write_file, delete_file, execute, git, start/kill_process)
    # are intentionally excluded — auto-running arbitrary writes from
    # ambiguous text would be a foot-gun.
    _AUTO_EXEC_SAFE_VERBS: tuple[str, ...] = (
        "fetch", "http_request", "read_file", "list_dir",
        "browser", "check_process", "env",
    )

    @staticmethod
    def _clean_textual_arg(verb: str, raw: str) -> str:
        """Strip the noise the model wraps around args in chat-style emissions.

        Models routinely produce ``fetch "https://example.com".`` (quoted +
        period) or ``read_file `./foo.py` `` (backticks). Without cleanup
        the URL/path the dispatcher sees is malformed and the tool fails.

        Removed:
          * matched surrounding pairs: ``"..."``, ``'...'``, ``` `...` ```,
            ``<...>``, ``(...)``, ``[...]``
          * trailing sentence punctuation: ``.,;:!?)]>``
        Internal spaces are preserved (a path can contain them).
        """
        s = raw.strip()
        # Strip matched surrounding pairs (apply repeatedly — model may
        # nest e.g. `<"https://...">`).
        pairs = (("\"", "\""), ("'", "'"), ("`", "`"),
                 ("<", ">"), ("(", ")"), ("[", "]"))
        changed = True
        while changed:
            changed = False
            for opener, closer in pairs:
                if len(s) >= 2 and s.startswith(opener) and s.endswith(closer):
                    s = s[len(opener):-len(closer)].strip()
                    changed = True
        # Trim trailing sentence punctuation (URL paths end with letters,
        # digits, slashes, %, &, =, etc.; never with a sentence terminator).
        while s and s[-1] in ".,;:!?)]>":
            s = s[:-1].rstrip()
        return s

    def _payload_from_textual_call(self, verb: str, arg: str) -> dict[str, Any] | None:
        """Best-effort: map ``<verb> [<arg>]`` to a real tool payload.

        Returns ``None`` when the verb isn't safely auto-executable, when
        a required arg is missing, or when the arg shape doesn't match
        what the tool expects. Conservative on purpose — better to fall
        back to the apology than to ship a malformed payload.

        For verbs whose default behavior is unambiguous (``env``,
        ``list_dir``), missing args fill in sensibly (``list_dir`` →
        current working dir; ``env`` → all env vars).
        """
        cleaned = self._clean_textual_arg(verb, arg) if arg else ""
        if verb == "fetch":
            return {"url": cleaned} if cleaned else None
        if verb == "browser":
            return {"url": cleaned} if cleaned else None
        if verb == "http_request":
            return {"url": cleaned, "method": "GET"} if cleaned else None
        if verb == "read_file":
            return {"path": cleaned} if cleaned else None
        if verb == "list_dir":
            # `list_dir` alone → assume the working directory.
            return {"path": cleaned or "."}
        if verb == "check_process":
            return {"name": cleaned} if cleaned else None
        if verb == "env":
            # `env` alone → list all env. With an arg, treat it as a key.
            return {"name": cleaned} if cleaned else {}
        return None

    def _honor_tool_text_as_intent(
        self, verb: str, arg: str, user_message: str, opts: "RunOptions",
        provider: Any, model_name: str, num_ctx: int | None,
        system_content: str, marker_role: str,
        on_delta: Any,
    ) -> "Iterator[PipelineEvent]":
        """When chat fallback emits ``<verb> <arg>`` as plain text, take
        that as the intent it actually was, run the tool ourselves (for
        safe verbs), then re-stream a chat call with the result in
        context so the user gets a real answer.

        Falls back to a clean apology when the verb is unsafe to
        auto-execute or the tool itself fails.
        """
        # Unsafe verb (write_file / execute / git / delete_file / start_process /
        # kill_process) → don't auto-run. Surface a clear message instead.
        if verb not in self._AUTO_EXEC_SAFE_VERBS:
            replacement = (
                f"The model emitted `{verb} {arg}` as plain text, which is "
                f"not a real tool call and is not safe to auto-execute "
                f"(write/execute tools require an explicit JSON action). "
                f"Please retry the prompt — the orchestrator should call "
                f"`{verb}` properly. If this keeps happening, try "
                f"/roles set orchestrator <a model that follows JSON better>."
            )
            self._push_entry(ScratchpadEntry(
                step=self.state.iteration, role=marker_role, action="reply",
                input=user_message, output=replacement, timestamp=time.time(),
            ))
            yield PipelineEvent(type="done", final=replacement)
            return

        payload = self._payload_from_textual_call(verb, arg)
        if payload is None:
            yield PipelineEvent(type="done",
                final=f"Couldn't auto-recover from `{verb} {arg}` "
                      f"— retry the prompt.")
            return

        # Surface the auto-execute as a normal tool event so the bar /
        # mirror see the same thing they would for a JSON dispatch.
        started = time.time()
        try:
            result = invoke_tool(verb, payload,
                                  working_directory=opts.working_directory,
                                  conversation_id=opts.conversation_id)
        except Exception as exc:  # noqa: BLE001
            yield PipelineEvent(type="done",
                final=f"Tried to auto-run `{verb} {arg}` after the model "
                      f"emitted it as text, but the tool itself raised: "
                      f"{type(exc).__name__}: {exc}. Retry the prompt.")
            return

        output_text = result.output or ""
        if self._guards["output"] and output_text:
            output_text = self._guards["output"](output_text)

        if not result.ok:
            err = result.error or "tool returned no output"
            yield PipelineEvent(type="tool", tool=verb, ok=False,
                                output=output_text, error=err)
            apology = (
                f"Auto-recovered from `{verb} {arg}` (the model emitted it "
                f"as text instead of a JSON action) but the tool failed: "
                f"{err}. Retry or rephrase."
            )
            self._push_entry(ScratchpadEntry(
                step=self.state.iteration, role=marker_role, action="reply",
                input=user_message, output=apology, timestamp=time.time(),
            ))
            yield PipelineEvent(type="done", final=apology)
            return

        # Persist the tool call so /chat / scratchpad reflect it.
        self._push_entry(ScratchpadEntry(
            step=self.state.iteration, role="tool", action=verb,
            input=arg, output=f"successfully completed:\n{output_text}",
            timestamp=time.time(),
            duration_ms=int((time.time() - started) * 1000),
        ))
        yield PipelineEvent(type="tool", tool=verb, ok=True,
                            output=output_text, error=None)

        # Re-stream the chat call with the tool result baked in. We don't
        # recurse into _chat_fallback_stream — just do a single LLM call
        # bounded to summarizing the data we already have.
        summary_prompt_user = (
            f"User asked: {user_message}\n\n"
            f"--- Output of `{verb} {arg}` ---\n"
            f"{output_text[:8000]}\n"
            f"--- End output ---\n\n"
            f"Answer the user's question using the data above. Plain text "
            f"only — no JSON, no tool-call syntax. If the data is HTML or "
            f"raw, extract the answer from it. Be concise."
        )
        messages = [
            ChatMessage(role="system", content=system_content),
            ChatMessage(role="user", content=summary_prompt_user),
        ]

        if opts.on_role_start is not None:
            try:
                opts.on_role_start(marker_role, model_name, num_ctx)
            except Exception:  # noqa: BLE001
                pass

        summary_collected: list[str] = []
        summary_started = time.time()
        sp_tokens: int | None = None
        sc_tokens: int | None = None
        try:
            for chunk in provider.chat(
                model=model_name, messages=messages,
                num_ctx=num_ctx, json_mode=False,
                should_cancel=self.is_cancelled,
            ):
                if chunk.content:
                    summary_collected.append(chunk.content)
                    if on_delta:
                        on_delta(chunk.content)
                if chunk.done:
                    sp_tokens = chunk.prompt_tokens
                    sc_tokens = chunk.completion_tokens
        except Exception as exc:  # noqa: BLE001
            yield PipelineEvent(
                type="error",
                error=f"chat re-summarize after auto-{verb} failed: "
                      f"{type(exc).__name__}: {exc}",
            )
            return

        summary_duration_ms = int((time.time() - summary_started) * 1000)
        if opts.on_role_end is not None:
            try:
                opts.on_role_end(marker_role, model_name,
                                 sp_tokens or 0, sc_tokens or 0)
            except Exception:  # noqa: BLE001
                pass

        # Throughput tracking — same path as the primary chat fallback.
        try:
            from agentcommander.db.repos import insert_token_usage, record_throughput
            chars_total = sum(len(c) for c in summary_collected)
            insert_token_usage(
                conversation_id=opts.conversation_id,
                role=marker_role,
                provider_id=resolve_role(Role.ORCHESTRATOR).provider_id,  # type: ignore[union-attr]
                model=model_name,
                prompt_tokens=sp_tokens,
                completion_tokens=sc_tokens,
                duration_ms=summary_duration_ms,
            )
            record_throughput(
                model_name, sc_tokens, summary_duration_ms,
                chars_completed=chars_total,
            )
        except Exception:  # noqa: BLE001
            pass

        final = "".join(summary_collected).strip()
        if not final:
            final = (f"Fetched data from `{verb} {arg}` but the model "
                     f"didn't produce a summary. Raw:\n{output_text[:600]}")

        # Re-check the auto-recovery summary for the SAME pattern. If the
        # model managed to emit tool syntax AGAIN, don't loop — surface a
        # clean apology and stop.
        recheck = re.match(
            r"^\s*(read_file|write_file|list_dir|delete_file|execute|fetch|"
            r"http_request|git|env|browser|start_process|kill_process|"
            r"check_process)\s+(?!\{)([^\n]+?)\s*$",
            final, re.IGNORECASE,
        )
        if recheck and len(final) <= 200 and "\n" not in final:
            final = (
                f"I auto-fetched `{verb} {arg}` and got data, but the "
                f"summarizer model emitted more tool syntax instead of "
                f"summarizing. The orchestrator model isn't following "
                f"the chat contract on this hardware — try a different "
                f"model with /roles set."
            )

        self._push_entry(ScratchpadEntry(
            step=self.state.iteration, role=marker_role, action="reply",
            input=user_message, output=final, timestamp=time.time(),
        ))
        yield PipelineEvent(type="done", final=final)


def _decision_to_payload(decision: OrchestratorDecision, exec_code: str,
                        exec_language: str) -> dict[str, Any]:
    """Map an OrchestratorDecision to the tool dispatcher's payload shape.

    Always omits None-valued optional fields. The dispatcher's
    ``_validate_payload`` enforces JSON-Schema types, so passing
    ``"method": None`` for an optional string field fails with
    ``"method: must be string (got NoneType)"``. Round-24 caught this
    on ``fetch``; the same fix is now applied to every tool that has
    optional fields the orchestrator might omit.

    Tools missing here would land at the catch-all ``return {}`` and
    fail with their schema's required-field error — silent breakage
    that round-23 hid behind retry loops. ``http_request``, ``git``,
    ``env``, and ``browser`` are routed below explicitly.
    """
    a = decision.action
    if a in ("read_file", "list_dir", "delete_file"):
        return {"path": decision.path or decision.input}
    if a == "write_file":
        return {"path": decision.path or decision.input,
                "content": decision.content or ""}
    if a == "execute":
        return {"language": exec_language, "code": exec_code}
    if a == "fetch":
        payload: dict[str, Any] = {"url": decision.url or decision.input}
        if decision.method:
            payload["method"] = decision.method
        if decision.headers:
            payload["headers"] = decision.headers
        if decision.body is not None:
            payload["body"] = decision.body
        return payload
    if a == "http_request":
        # Same shape as fetch but with `json` body support implicit via
        # ``body``. Schema requires `url`, optional `method`/`headers`/
        # `body`. Drop None-valued optionals.
        payload = {"url": decision.url or decision.input}
        if decision.method:
            payload["method"] = decision.method
        if decision.headers:
            payload["headers"] = decision.headers
        if decision.body is not None:
            payload["body"] = decision.body
        return payload
    if a == "git":
        # Schema requires `verb` (status/log/diff/show/branch/ls_files);
        # optional `n`/`revision`/`pattern`. The orchestrator's older
        # prompt called this "command" — both names are accepted as
        # input source for `verb`. ``message``/``files`` are NOT in the
        # schema (this is a read-only git tool — mutations go through
        # `execute`); they're silently dropped if the orchestrator
        # emits them.
        payload = {"verb": decision.command or decision.input or "status"}
        if decision.pattern:
            payload["pattern"] = decision.pattern
        return payload
    if a == "env":
        # Schema: optional `verb` (read/list/list_filtered) + optional
        # `name`. We pass both when present; the tool defaults `verb`
        # to "list" if absent.
        payload = {}
        verb_src = decision.command or decision.input or ""
        if verb_src and verb_src in ("read", "list", "list_filtered"):
            payload["verb"] = verb_src
        if decision.path:  # repurpose `path` for the var name slot
            payload["name"] = decision.path
        return payload
    if a == "browser":
        return {"url": decision.url or decision.input}
    if a == "start_process":
        return {"command": decision.command or decision.input}
    if a in ("kill_process", "check_process"):
        return {"id": decision.input}
    return {}
