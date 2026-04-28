"""start_process / kill_process / check_process — long-running background processes.

Safety stack:
  1. scan_dangerous_command() — pattern-block destructive commands BEFORE spawning
  2. require_working_directory() — block if no working dir set
  3. prlimit wrap (Linux) — same caps as `execute`
  4. Tracks live processes in an in-memory registry (per process lifetime)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from agentcommander.safety.dangerous_patterns import scan_dangerous_command
from agentcommander.safety.sandbox import require_working_directory
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult

MAX_BUF_BYTES = 200_000


@dataclass
class _ProcessRecord:
    id: str
    command: str
    proc: subprocess.Popen
    started_at: float
    stdout: str = ""
    stderr: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_REGISTRY: dict[str, _ProcessRecord] = {}


def _wrap_with_prlimit(shell_cmd: str) -> list[str]:
    """Run a shell command with prlimit caps on Linux; plain `sh -c` elsewhere."""
    inner = ["sh", "-c", shell_cmd]
    if sys.platform != "linux":
        return inner
    prlimit = shutil.which("prlimit")
    if not prlimit:
        return inner
    return [prlimit, "--nproc=256", "--as=2147483648", "--fsize=524288000",
            "--cpu=300", "--", *inner]


def _drain(record: _ProcessRecord) -> None:
    """Background thread: read stdout/stderr into the record buffers."""

    def reader(stream, attr: str) -> None:
        if stream is None:
            return
        for chunk in iter(lambda: stream.read(4096), b""):
            try:
                text = chunk.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            with record.lock:
                cur = getattr(record, attr)
                remaining = MAX_BUF_BYTES - len(cur)
                if remaining > 0:
                    setattr(record, attr, cur + text[:remaining])

    threading.Thread(target=reader, args=(record.proc.stdout, "stdout"), daemon=True).start()
    threading.Thread(target=reader, args=(record.proc.stderr, "stderr"), daemon=True).start()


def _start_process(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = payload.get("command") or payload.get("input", "")
    if not isinstance(command, str) or not command.strip():
        return ToolResult(ok=False, error="command is required")
    danger = scan_dangerous_command(command)
    if danger:
        ctx.audit("start_process.blocked", {"category": danger.category, "reason": danger.reason})
        return ToolResult(ok=False, error=f"BLOCKED [{danger.category}]: {danger.reason}")
    try:
        cwd = require_working_directory(ctx.working_directory)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))

    cmd = _wrap_with_prlimit(command)
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        return ToolResult(ok=False, error=f"spawn failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"spawn failed: {type(exc).__name__}: {exc}")

    import time
    record = _ProcessRecord(id=str(uuid.uuid4()), command=command, proc=proc,
                            started_at=time.time())
    _REGISTRY[record.id] = record
    _drain(record)
    ctx.audit("start_process", {"id": record.id, "command": command, "pid": proc.pid})
    return ToolResult(ok=True, output=f"started process {record.id} (pid {proc.pid})",
                      data={"id": record.id, "pid": proc.pid})


def _kill_process(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    pid_id = payload.get("id") or payload.get("input")
    if not isinstance(pid_id, str):
        return ToolResult(ok=False, error="id is required")
    record = _REGISTRY.get(pid_id)
    if record is None:
        return ToolResult(ok=False, error=f"process not found: {pid_id}")
    try:
        record.proc.kill()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"kill failed: {exc}")
    ctx.audit("kill_process", {"id": pid_id})
    _REGISTRY.pop(pid_id, None)
    return ToolResult(ok=True, output=f"killed {pid_id}")


def _check_process(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    pid_id = payload.get("id") or payload.get("input")
    if not isinstance(pid_id, str):
        return ToolResult(ok=False, error="id is required")
    record = _REGISTRY.get(pid_id)
    if record is None:
        return ToolResult(ok=False, error=f"process not found: {pid_id}")
    rc = record.proc.poll()
    with record.lock:
        out, err = record.stdout, record.stderr
    return ToolResult(
        ok=True,
        data={
            "id": pid_id,
            "command": record.command,
            "started_at": record.started_at,
            "exit_code": rc,
            "still_running": rc is None,
            "stdout": out,
            "stderr": err,
        },
        output=(out or "") + (("\n--- stderr ---\n" + err) if err else ""),
    )


register(ToolDescriptor(
    name="start_process",
    description="Spawn a background shell process. Resource-limited on Linux.",
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    handler=_start_process,
))

register(ToolDescriptor(
    name="kill_process",
    description="Kill a previously-started background process by id.",
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    handler=_kill_process,
))

register(ToolDescriptor(
    name="check_process",
    description="Read status + buffered output for a background process.",
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    handler=_check_process,
))
