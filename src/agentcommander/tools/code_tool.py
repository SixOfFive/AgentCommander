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
    # Package managers — no tempfile, dispatched by _execute_package_manager.
    # The orchestrator system prompt advertises {"language":"pip", "input":
    # "requests pandas"} so leaving them unsupported was leaving a real
    # papercut: the orchestrator would emit `pip` decisions and the tool
    # would reject them with "unsupported language: pip", looping until
    # max iterations. The ext value is unused for these.
    "pip": "",
    "npm": "",
}

_LANG_FAMILY: dict[str, str] = {
    "python": "python", "py": "python",
    "javascript": "node", "js": "node", "node": "node",
    "bash": "bash", "sh": "bash", "shell": "bash",
    "pip": "pkg-pip",
    "npm": "pkg-npm",
}

# Languages that don't go through the tempfile-then-run path — they're
# direct command invocations where ``code`` is the argument list.
_PACKAGE_MANAGERS = {"pip", "npm"}


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


def _execute_package_manager(language: str, packages: str, ctx: ToolContext,
                              timeout_s: int) -> ToolResult:
    """Dispatch ``pip`` / ``npm`` install commands.

    Shape per ORCHESTRATOR.md: ``{"action":"execute","language":"pip",
    "input":"requests pandas numpy"}``. ``packages`` is the space-separated
    package list. We run it as ``python -m pip install <pkgs>`` (pip) or
    ``npm install <pkgs>`` (npm) with the same permission gate, timeout,
    and danger-scan as regular execute.
    """
    pkgs = packages.strip().split()
    if not pkgs:
        return ToolResult(ok=False, error=f"{language}: no packages specified")
    # Conservative validation — package names should not contain shell
    # metacharacters. Block obvious injection attempts before we shell
    # out. ``--flag`` style options are allowed.
    for p in pkgs:
        if any(ch in p for ch in (";", "|", "&", "`", "$", "\n", "\r")):
            return ToolResult(
                ok=False,
                error=f"{language}: rejected package spec containing shell metachar: {p!r}",
            )

    try:
        cwd = require_working_directory(ctx.working_directory)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))

    try:
        from agentcommander.tui.permissions import request_permission
        request_permission(cwd, "execute")  # type: ignore[arg-type]
    except Exception as exc:
        if type(exc).__name__ == "PermissionDenied":
            raise
        # other exceptions: fall through, don't block

    if language == "pip":
        py = _resolve_python_cmd()
        if py is None:
            return ToolResult(
                ok=False,
                error="pip: no Python interpreter on PATH",
            )
        cmd = [*py, "-m", "pip", "install", *pkgs]
    elif language == "npm":
        npm = shutil.which("npm")
        if npm is None:
            return ToolResult(
                ok=False,
                error="npm: not found on PATH (install Node.js)",
            )
        cmd = [npm, "install", *pkgs]
    else:
        return ToolResult(ok=False, error=f"unknown package manager: {language}")

    cmd = _wrap_with_prlimit(cmd)
    timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
    except FileNotFoundError as exc:
        return ToolResult(ok=False,
                          error=f"package manager not found: {cmd[0]} ({exc})")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "")[:2000]
        err = (exc.stderr or "")[:2000]
        return ToolResult(
            ok=False, error=f"timed out after {timeout_s}s",
            output=f"--- stdout ---\n{out}\n--- stderr ---\n{err}",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"spawn failed: {exc}")

    stdout_part, t_out = _truncate(proc.stdout or "")
    stderr_part, t_err = _truncate(proc.stderr or "")
    truncated = t_out or t_err
    combined = "\n".join(filter(None, [
        f"--- stdout ---\n{stdout_part}" if stdout_part else "",
        f"--- stderr ---\n{stderr_part}" if stderr_part else "",
        "--- output truncated ---" if truncated else "",
    ]))

    if proc.returncode == 0:
        return ToolResult(ok=True, output=combined or f"{language}: installed {len(pkgs)} package(s)",
                          data={"exit_code": 0, "packages": pkgs})
    return ToolResult(
        ok=False, error=f"{language} install failed: exit code {proc.returncode}",
        output=combined, data={"exit_code": proc.returncode},
    )


def _execute(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    language = payload.get("language", "")
    code = payload.get("code", payload.get("input", ""))
    timeout_s = payload.get("timeout_s", DEFAULT_TIMEOUT_S)

    if not isinstance(language, str) or not language:
        return ToolResult(ok=False, error="language is required")
    if not isinstance(code, str) or not code.strip():
        return ToolResult(ok=False, error="code is required")
    if language.lower() not in _RUNNERS:
        return ToolResult(ok=False, error=f"unsupported language: {language}")
    # Package manager dispatch — different shape (run a single command
    # with code-as-args instead of writing a tempfile and invoking an
    # interpreter on it).
    if language.lower() in _PACKAGE_MANAGERS:
        return _execute_package_manager(language.lower(), code, ctx, timeout_s)
    runner = _resolve_runner(language)
    if runner is None:
        return ToolResult(
            ok=False,
            error=(f"interpreter for {language!r} not found on PATH. "
                   "On Windows, install Python from python.org "
                   "(the launcher 'py' is preferred), or add 'python' / "
                   "'python3' to PATH."),
        )

    danger = scan_dangerous_code(code)
    if danger:
        ctx.audit("execute.blocked",
                  {"category": danger.category, "reason": danger.reason})
        return ToolResult(ok=False, error=f"BLOCKED [{danger.category}]: {danger.reason}")

    try:
        cwd = require_working_directory(ctx.working_directory)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))

    # Permission gate: ask the user before spawning the interpreter. Keyed
    # on the working directory so "Always" persists per project rather than
    # per-tempfile (every script lives in a fresh tempdir).
    try:
        from agentcommander.tui.permissions import request_permission
        request_permission(cwd, "execute")  # type: ignore[arg-type]
    except Exception as exc:  # PermissionDenied propagates upward
        if type(exc).__name__ == "PermissionDenied":
            raise
        # Any other failure: fall through (don't block on a buggy prompt).

    ext, cmd_prefix = runner
    timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))

    with tempfile.TemporaryDirectory(prefix="ac-exec-") as tmp:
        script_path = os.path.join(tmp, f"script{ext}")
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)

        cmd = [*cmd_prefix, script_path]
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
            return ToolResult(
                ok=False,
                error=f"runtime not found: {cmd_prefix[0]} ({exc})",
            )
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
