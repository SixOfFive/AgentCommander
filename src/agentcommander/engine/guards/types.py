"""Shared types + helpers for all guard families.

Ported from EngineCommander/src/main/orchestration/guards/guard-types.ts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agentcommander.types import ScratchpadEntry


# ─── Verdict ───────────────────────────────────────────────────────────────


@dataclass
class GuardVerdict:
    action: Literal["pass", "continue", "break"]
    final_output: str | None = None  # only meaningful when action == "break"


def _verdict_pass() -> GuardVerdict:
    return GuardVerdict(action="pass")


def _verdict_continue() -> GuardVerdict:
    return GuardVerdict(action="continue")


def _verdict_break(final_output: str) -> GuardVerdict:
    return GuardVerdict(action="break", final_output=final_output)


# ─── Helpers (`hasDeliverable`, `userWantsAction`, `codeContext`) ──────────


_USER_WANTS_ACTION_RX = re.compile(
    r"\b(fetch|browse|scrape|screenshot|run|execute|show|display|get|find|extract|test|check|monitor|watch)\b",
    re.IGNORECASE,
)


def has_deliverable(scratchpad: list[ScratchpadEntry]) -> bool:
    setup_actions = ("pip", "npm", "venv", "mkdir", "install")
    for e in scratchpad:
        if (e.action == "execute" and "successfully" in (e.output or "")
                and not any(s in (e.input or "").lower() for s in setup_actions)):
            return True
        if e.action in ("browse", "screenshot", "extract_text"):
            return True
        if e.action == "fetch" and len(e.output or "") > 100:
            return True
        if (e.action == "execute" and len(e.output or "") > 200
                and "Installing" not in (e.output or "")):
            return True
    return False


def user_wants_action(user_message: str) -> bool:
    return bool(_USER_WANTS_ACTION_RX.search(user_message))


@dataclass
class CodeContextResult:
    has_coder_role: bool
    has_write_file: bool
    has_code_file: bool
    has_code_step: bool
    user_wants_execution: bool
    needs_execution: bool


_CODE_FILE_RX = re.compile(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c|html)$", re.IGNORECASE)
_USER_WANTS_EXEC_RX = re.compile(
    r"\b(run|execute|show.*output|show.*result|test|try|launch)\b", re.IGNORECASE,
)


def code_context(scratchpad: list[ScratchpadEntry], user_message: str) -> CodeContextResult:
    has_coder_role = any(e.role == "coder" for e in scratchpad)
    has_write_file = any(e.action == "write_file" for e in scratchpad)
    has_code_file = any(
        e.action == "write_file" and _CODE_FILE_RX.search(e.input or "")
        for e in scratchpad
    )
    has_code_step = has_coder_role or has_code_file
    user_wants_execution = bool(_USER_WANTS_EXEC_RX.search(user_message))

    last_execute = next((e for e in reversed(scratchpad) if e.action == "execute"), None)
    last_write = next(
        (e for e in reversed(scratchpad) if e.action == "write_file" or e.role == "coder"),
        None,
    )
    execute_failed = bool(last_execute and any(
        m in (last_execute.output or "") for m in ("Error", "error", "Traceback", "SyntaxError")
    ))
    code_rewritten_after_execute = bool(
        last_execute and last_write and last_write.timestamp > last_execute.timestamp
    )
    needs_execution = (last_execute is None) or (execute_failed and code_rewritten_after_execute)

    return CodeContextResult(
        has_coder_role=has_coder_role,
        has_write_file=has_write_file,
        has_code_file=has_code_file,
        has_code_step=has_code_step,
        user_wants_execution=user_wants_execution,
        needs_execution=needs_execution,
    )


# ─── Nudge helper (used by every guard family) ─────────────────────────────


def push_system_nudge(scratchpad: list[ScratchpadEntry], iteration: int,
                      reason: str, output: str) -> None:
    import time
    scratchpad.append(ScratchpadEntry(
        step=iteration, role="tool", action="system_nudge",
        input=reason, output=output, timestamp=time.time(),
    ))
