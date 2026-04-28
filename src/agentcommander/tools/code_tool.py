"""Execute code tool — Python / JS / bash.

SAFETY GATE STACK:
  1. scan_dangerous_code() — pattern-block destructive code BEFORE spawning
  2. require_working_directory() — block if no working dir set
  3. prlimit wrap (Linux) — nproc=256, virt=2GB, fsize=500MB, cpu=300s
  4. Output captured + truncated; non-zero exit → ToolResult(ok=False)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from agentcommander.safety.dangerous_patterns import scan_dangerous_code
from agentcommander.safety.sandbox import require_working_directory
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult

MAX_OUTPUT_BYTES = 200_000
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600

_RUNNERS: dict[str, tuple[str, str]] = {
    # language → (file extension, command name)
    "python": (".py", "python3"),
    "py": (".py", "python3"),
    "javascript": (".js", "node"),
    "js": (".js", "node"),
    "node": (".js", "node"),
    "bash": (".sh", "bash"),
    "sh": (".sh", "bash"),
    "shell": (".sh", "bash"),
}


def _resolve_runner(language: str) -> tuple[str, str] | None:
    return _RUNNERS.get(language.lower())


def _wrap_with_prlimit(cmd: list[str]) -> list[str]:
    """Linux only: prepend prlimit caps. On Windows/macOS returns cmd unchanged."""
    if sys.platform != "linux":
        return cmd
    prlimit = shutil.which("prlimit")
    if not prlimit:
        return cmd
    return [
        prlimit,
        "--nproc=256",
        "--as=2147483648",     # 2 GB virtual memory
        "--fsize=524288000",   # 500 MB file size
        "--cpu=300",           # 5 min CPU
        "--",
        *cmd,
    ]


def _truncate(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    head = encoded[: int(max_bytes * 0.7)].decode("utf-8", errors="replace")
    tail = encoded[-int(max_bytes * 0.25):].decode("utf-8", errors="replace")
    return f"{head}\n... [output truncated] ...\n{tail}", True


def _execute(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    language = payload.get("language", "")
    code = payload.get("code", payload.get("input", ""))
    timeout_s = payload.get("timeout_s", DEFAULT_TIMEOUT_S)

    if not isinstance(language, str) or not language:
        return ToolResult(ok=False, error="language is required")
    if not isinstance(code, str) or not code.strip():
        return ToolResult(ok=False, error="code is required")
    runner = _resolve_runner(language)
    if runner is None:
        return ToolResult(ok=False, error=f"unsupported language: {language}")

    danger = scan_dangerous_code(code)
    if danger:
        ctx.audit("execute.blocked",
                  {"category": danger.category, "reason": danger.reason})
        return ToolResult(ok=False, error=f"BLOCKED [{danger.category}]: {danger.reason}")

    try:
        cwd = require_working_directory(ctx.working_directory)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))

    ext, cmd_name = runner
    timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))

    with tempfile.TemporaryDirectory(prefix="ac-exec-") as tmp:
        script_path = os.path.join(tmp, f"script{ext}")
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)

        cmd = [cmd_name, script_path]
        cmd = _wrap_with_prlimit(cmd)

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            return ToolResult(ok=False,
                              error=f"runtime not found: {cmd_name} ({exc})")
        except subprocess.TimeoutExpired as exc:
            stdout, _ = _truncate(exc.stdout or "")
            stderr, _ = _truncate(exc.stderr or "")
            return ToolResult(
                ok=False,
                error=f"timed out after {timeout_s}s",
                output=f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"spawn failed: {exc}")

    stdout_part = proc.stdout or ""
    stderr_part = proc.stderr or ""
    truncated = False

    if stdout_part:
        stdout_part, t = _truncate(stdout_part)
        truncated = truncated or t
    if stderr_part:
        stderr_part, t = _truncate(stderr_part)
        truncated = truncated or t

    combined = "\n".join(filter(None, [
        f"--- stdout ---\n{stdout_part}" if stdout_part else "",
        f"--- stderr ---\n{stderr_part}" if stderr_part else "",
        "--- output truncated ---" if truncated else "",
    ]))

    if proc.returncode == 0:
        return ToolResult(
            ok=True,
            output=combined or "(no output)",
            data={"exit_code": 0},
        )
    return ToolResult(
        ok=False,
        error=f"exit code {proc.returncode}",
        output=combined,
        data={"exit_code": proc.returncode},
    )


register(ToolDescriptor(
    name="execute",
    description=(
        "Run Python, JavaScript, or bash code in the working directory. "
        "Output captured. Resource-limited on Linux via prlimit."
    ),
    privileged=True,
    input_schema={
        "type": "object",
        "properties": {
            "language": {"type": "string", "enum": list(_RUNNERS.keys())},
            "code": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_S},
        },
        "required": ["language", "code"],
    },
    handler=_execute,
))
