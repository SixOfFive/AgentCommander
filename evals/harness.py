"""Headless engine harness for the eval suite (improvement #3).

Drives a real ``PipelineRun`` end-to-end against the user's configured
providers/models — the same code path the TUI uses — but without any
terminal UI. Returns a structured result per prompt: final answer, roles
fired, tools dispatched, iteration count, wall-clock duration, and any
error. The eval runner scores those results against the golden cases.

Why run the *real* engine instead of mocking? The whole point of #3 is to
measure what actually ships: whether a guard helped or just added latency,
whether constrained decoding (#1) changed the pass rate, whether a model
swap regressed anything. Mocks can't answer those questions.

Safety / conventions (see CLAUDE.md + braindump):
  - Run from ``AgentTesting/`` so ``Path.cwd()/.agentcommander/db.sqlite`` is
    the user's real DB (with persisted providers + role assignments). This
    module never copies the DB and never reads/writes the api_key.
  - Every conversation it creates is tracked and deleted on completion, so
    the eval run leaves no rows behind in the real DB.
  - File-writing cases get a throwaway working dir under cwd, pre-authorized
    for non-interactive writes via ``grant_subtree``.

Pure stdlib.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─── Bootstrap (mirror the TUI startup, minus the UI) ───────────────────────


def bootstrap(db_path: "str | Path | None" = None) -> dict[str, Any]:
    """Initialise DB + tools + providers + role assignments headlessly.

    Returns a small dict describing the resolved environment (provider count,
    role → model table) for the runner to print in its header.
    """
    from agentcommander.db.connection import init_db
    from agentcommander.db.repos import audit, get_role_assignment, get_config
    from agentcommander.providers import bootstrap as provider_bootstrap
    from agentcommander.providers.base import list_active
    from agentcommander.tools.dispatcher import bootstrap_builtins
    from agentcommander.typecast.autoconfig import apply_autoconfigure
    from agentcommander.engine.role_resolver import set_autoconfig
    from agentcommander.types import Role

    init_db(db_path)
    bootstrap_builtins()
    provider_bootstrap.bootstrap()  # registers factories + rebuilds instances

    providers = list_active()

    # Honor the same preferred-backend filter the TUI applies at startup so
    # the eval resolves the same models the user actually runs with.
    preferred = get_config("preferred_backend", None)
    if isinstance(preferred, str) and preferred:
        filtered = [p for p in providers if p.type == preferred]
        if filtered:
            providers = filtered

    role_table: dict[str, str] = {}
    if providers:
        applied = apply_autoconfigure(
            providers=providers,
            get_role_assignment_fn=get_role_assignment,
            audit_fn=audit,
        )
        if not applied.skipped_reason:
            in_memory: dict[Role, tuple[str, str]] = {}
            for role_value, (provider_id, model) in applied.role_picks.items():
                try:
                    in_memory[Role(role_value)] = (provider_id, model)
                except ValueError:
                    continue
            set_autoconfig(in_memory)

    # Build the effective role → model view for reporting (DB overrides win
    # over autoconfig at resolve time; resolve() already encodes that).
    from agentcommander.engine.role_resolver import resolve as resolve_role
    for role in Role:
        rr = resolve_role(role)
        if rr is not None:
            role_table[role.value] = rr.model

    return {
        "providers": [{"id": p.id, "type": p.type} for p in providers],
        "roles": role_table,
    }


# ─── Per-case execution ─────────────────────────────────────────────────────


@dataclass
class CaseResult:
    final: str = ""
    iterations: int = 0
    roles: list[str] = field(default_factory=list)        # role-call order
    tools: list[dict] = field(default_factory=list)        # [{tool, ok}]
    actions: list[str] = field(default_factory=list)       # decision actions seen
    error: str | None = None
    timed_out: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "final": self.final,
            "iterations": self.iterations,
            "roles": self.roles,
            "tools": self.tools,
            "actions": self.actions,
            "error": self.error,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
        }


def run_case(prompt: str, *, timeout_s: float = 300.0,
             working_directory: "str | None" = None) -> CaseResult:
    """Run one prompt through the engine and collect a structured result.

    The engine generator runs on a worker thread; a watchdog sets the
    pipeline's ``cancel_event`` after ``timeout_s`` so a hung/slow model
    can't wedge the whole suite. The engine checks the flag at iteration
    boundaries and mid-stream.
    """
    from agentcommander.db.repos import append_message, create_conversation, delete_conversation
    from agentcommander.engine.engine import PipelineRun, RunOptions

    wd = working_directory or str(Path.cwd())
    conv = create_conversation(title="eval", working_directory=wd)
    user_msg = append_message(conv.id, "user", prompt)

    opts = RunOptions(
        conversation_id=conv.id,
        user_message=prompt,
        working_directory=wd,
        user_message_id=user_msg.id,
    )
    run = PipelineRun(opts)
    run.cancel_event = threading.Event()

    result = CaseResult()
    events_q: "queue.Queue[Any]" = queue.Queue()
    _SENTINEL = object()

    def _worker() -> None:
        try:
            for ev in run.events():
                events_q.put(ev)
        except Exception as exc:  # noqa: BLE001
            events_q.put(("__error__", f"{type(exc).__name__}: {exc}"))
        finally:
            events_q.put(_SENTINEL)

    started = time.time()
    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    deadline = started + timeout_s
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            run.cancel_event.set()
            result.timed_out = True
            break
        try:
            item = events_q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if item is _SENTINEL:
            break
        if isinstance(item, tuple) and item and item[0] == "__error__":
            result.error = item[1]
            continue
        _absorb_event(item, result)

    # If we timed out, give the worker a moment to notice the cancel flag and
    # unwind, then drain whatever it managed to emit.
    if result.timed_out:
        worker.join(timeout=10.0)
        while True:
            try:
                item = events_q.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL or (isinstance(item, tuple) and item and item[0] == "__error__"):
                if isinstance(item, tuple):
                    result.error = result.error or item[1]
                continue
            _absorb_event(item, result)

    result.duration_ms = int((time.time() - started) * 1000)

    try:
        delete_conversation(conv.id)
    except Exception:  # noqa: BLE001
        pass

    return result


def _absorb_event(ev: Any, result: CaseResult) -> None:
    """Fold one PipelineEvent into the accumulating CaseResult."""
    etype = getattr(ev, "type", None)
    if etype == "iteration" and getattr(ev, "iteration", None) is not None:
        result.iterations = max(result.iterations, int(ev.iteration))
    elif etype == "role":
        role = getattr(ev, "role", None)
        action = getattr(ev, "action", None)
        if role:
            result.roles.append(role)
        if action:
            result.actions.append(action)
    elif etype == "tool":
        result.tools.append({
            "tool": getattr(ev, "tool", None) or getattr(ev, "action", None),
            "ok": getattr(ev, "ok", None),
        })
        action = getattr(ev, "action", None)
        if action:
            result.actions.append(action)
    elif etype == "done":
        final = getattr(ev, "final", None)
        if final:
            result.final = final
    elif etype == "error":
        err = getattr(ev, "error", None)
        if err:
            result.error = err
