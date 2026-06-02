"""Parallel fan-out primitive (prototype) — fleet utilization.

The orchestrator can emit a single ``fan_out`` decision whose ``steps`` list
holds independent sub-decisions. This module runs those sub-steps — each a
role delegation — and returns their results **in step order**, regardless of
which finished first. The engine (`_dispatch_fan_out`) then folds the ordered
results into the scratchpad deterministically.

Why this is safe to parallelize despite AC's serial heritage:
  - The sub-steps are independent by construction (the orchestrator grouped
    them precisely because none depends on another's output this turn).
  - Each sub-step resolves to its role's assigned provider, so binding
    reviewer→BEAST, critic→THEOCOMP, tester→Jerry makes three GPUs work at
    once — the whole point (fleet utilization).
  - The work is I/O-bound (HTTP streaming to a model server), so a stdlib
    ``ThreadPoolExecutor`` is the right tool — no asyncio, no new deps.
  - Shared mutable state is thread-safe: the SQLite connection is a
    lock-serialized ``_LockedConnection``; ``model_stats`` writes are guarded
    by a module lock. The scratchpad is NOT mutated here — the caller appends
    entries on the main thread after gather, in deterministic order.

Constraints honored:
  - **stdlib only** (`concurrent.futures`).
  - Only role sub-actions run in parallel (``FANOUT_SUB_ACTIONS``); side-
    effecting tools are excluded so steps can't race on the filesystem.
  - Cancellation: every worker is handed the run's ``should_cancel`` and the
    provider checks it mid-stream, so ``/stop`` still unwinds a fan-out.

Prototype limitations (documented, not hidden):
  - No per-step rate-limit retry with UI countdown (that path is a generator
    that can't run in a worker thread). A worker that hits ProviderRateLimited
    records it as a failed sub-step. Local Ollama rarely rate-limits.
  - No live token streaming for sub-steps (output is collected, then rendered
    as completed blocks) — avoids interleaved tokens from concurrent roles.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from agentcommander.engine.actions import ACTION_TO_ROLE, FANOUT_SUB_ACTIONS
from agentcommander.engine.role_call import call_role
from agentcommander.engine.role_resolver import resolve as resolve_role

# Bounded so a runaway `steps` list (or a model that emits 50 sub-tasks) can't
# open 50 sockets at once. min(this, len(steps)) workers are used.
FANOUT_MAX_WORKERS: int = 4


@dataclass
class FanOutResult:
    """Outcome of one fan-out sub-step. Aligned to the input step index."""
    index: int
    action: str
    input: str
    role: str
    output: str
    ok: bool
    error: str | None
    duration_ms: int
    model: str | None


def validate_steps(steps: "list[Any] | None") -> tuple[list[dict], list[str]]:
    """Split raw ``decision.steps`` into (runnable sub-steps, skip reasons).

    A sub-step is runnable iff it's a dict whose ``action`` is in
    ``FANOUT_SUB_ACTIONS`` (the role delegations). Everything else — tool
    verbs, ``done``, nested ``fan_out``, malformed entries — is rejected with
    a human-readable reason so the engine can nudge intelligently.
    """
    runnable: list[dict] = []
    skipped: list[str] = []
    for i, raw in enumerate(steps or []):
        if not isinstance(raw, dict):
            skipped.append(f"step {i}: not an object ({type(raw).__name__})")
            continue
        act = raw.get("action")
        if act not in FANOUT_SUB_ACTIONS:
            skipped.append(f"step {i}: action {act!r} not allowed in fan_out")
            continue
        runnable.append(raw)
    return runnable, skipped


def _run_one(index: int, sub: dict, *, scratchpad_text: str,
             conversation_id: str | None,
             should_cancel: Callable[[], bool] | None) -> FanOutResult:
    action = str(sub.get("action"))
    inp = sub.get("input") or ""
    if not isinstance(inp, str):
        inp = str(inp)
    role = ACTION_TO_ROLE.get(action)
    started = time.time()
    if role is None:  # defensive — validate_steps should have filtered this
        return FanOutResult(index, action, inp, "?", "", False,
                            f"not a fan-out sub-action: {action!r}", 0, None)
    rr = resolve_role(role)
    model = rr.model if rr else None
    try:
        out = call_role(
            role,
            user_input=inp,
            scratchpad_text=scratchpad_text,
            conversation_id=conversation_id,
            on_delta=None,            # no live streaming for concurrent steps
            should_cancel=should_cancel,
        )
        return FanOutResult(index, action, inp, role.value, out, True, None,
                            int((time.time() - started) * 1000), model)
    except Exception as exc:  # noqa: BLE001 - isolate per-step failure
        return FanOutResult(index, action, inp, role.value, "", False,
                            f"{type(exc).__name__}: {exc}",
                            int((time.time() - started) * 1000), model)


def run_fan_out(subs: list[dict], *, scratchpad_text: str,
                conversation_id: str | None,
                should_cancel: Callable[[], bool] | None = None,
                parallel: bool = True,
                max_workers: int = FANOUT_MAX_WORKERS) -> list[FanOutResult]:
    """Run ``subs`` and return results aligned to input order.

    ``parallel=True`` runs them concurrently on a bounded thread pool;
    ``parallel=False`` runs them sequentially (the degrade path when the
    ``fan_out_enabled`` flag is off — same results, no concurrency). Either
    way the returned list is ordered by the original step index, so the
    scratchpad stays deterministic.
    """
    results: list[FanOutResult | None] = [None] * len(subs)

    def _task(i: int, sub: dict) -> FanOutResult:
        return _run_one(i, sub, scratchpad_text=scratchpad_text,
                        conversation_id=conversation_id,
                        should_cancel=should_cancel)

    if parallel and len(subs) > 1:
        workers = max(1, min(max_workers, len(subs)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="fanout") as ex:
            futures = {ex.submit(_task, i, sub): i for i, sub in enumerate(subs)}
            for fut in as_completed(futures):
                r = fut.result()
                results[r.index] = r
    else:
        for i, sub in enumerate(subs):
            results[i] = _task(i, sub)

    # No slot can be None (every _task returns a FanOutResult), but assert
    # the invariant for safety before the caller indexes the list.
    return [r for r in results if r is not None]
