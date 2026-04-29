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

_RUNNERS: dict[str, str] = {
    # language alias → file extension
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "node": ".js",
    "bash": ".sh",
    "sh": ".sh",
    "shell": ".sh",
}

_LANG_FAMILY: dict[str, str] = {
    "python": "python", "py": "python",
    "javascript": "node", "js": "node", "node": "node",
    "bash": "bash", "sh": "bash", "shell": "bash",
}


def _resolve_python_cmd() -> list[str] | None:
    """Pick a working Python interpreter prefix.

    Windows: try ``py -3`` first (the Windows Python launcher), then
    ``python``, then ``python3`` — matches ``ac.bat``'s resolution chain.
    POSIX: ``python3`` first, then ``python``. Returns ``None`` when none
    of them is on PATH; caller surfaces a clean error instead of letting
    subprocess raise FileNotFoundError or, worse, exit code 9009 from a
    Windows shell that didn't recognize the name.
    """
    if sys.platform == "win32":
        py = shutil.which("py")
        if py:
            return [py, "-3"]
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            return [path]
    return None


def _resolve_runner(language: str) -> tuple[str, list[str]] | None:
    """Return ``(file_extension, command_prefix)`` or None if the language
    isn't recognized OR no interpreter is on PATH.
    """
    lang = language.lower()
    ext = _RUNNERS.get(lang)
    family = _LANG_FAMILY.get(lang)
    if ext is None or family is None:
        return None
    if family == "python":
        cmd = _resolve_python_cmd()
        return (ext, cmd) if cmd else None
    if family == "node":
        node = shutil.which("node")
        return (ext, [node]) if node else None
    if family == "bash":
        # Windows: Git Bash / WSL puts `bash` on PATH if installed.
        b = shutil.which("bash")
        return (ext, [b]) if b else None
    return None


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
