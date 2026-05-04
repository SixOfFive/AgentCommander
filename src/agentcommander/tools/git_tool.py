"""Git tool — read-only-by-default subset of git commands.

Many of the engine's debugging / planning workflows want to see the
current branch, recent commits, or the diff of an in-flight change. The
existing ``execute`` tool can run arbitrary shell, but each call costs
a permission prompt (``execute`` cwd) — that prompt fatigue is the wrong
tradeoff for "git status".

This tool exposes only a hand-curated set of READ verbs that are
intrinsically safe (don't mutate the repo, don't talk to the network)
and runs them under a short subprocess timeout. Mutating verbs (add /
commit / push / reset / checkout) are intentionally absent — the user
should run those by hand or via ``execute`` (where the permission flow
applies). That asymmetry is the whole point: read-only git is a frequent
workflow primitive; mutating git deserves the same scrutiny as any
other side effect.

Verbs:

  - ``status``   — short status (porcelain v1 single-line output)
  - ``log``      — last N commits, oneline format. ``n`` arg (default 10).
  - ``diff``     — diff working tree vs HEAD, or vs ``revision`` if given
  - ``show``     — single commit (``revision`` required)
  - ``branch``   — list local branches with the active one starred
  - ``ls_files`` — tracked files (filtered by optional ``pattern`` glob)

Each verb runs ``git`` from the engine's working directory. If git isn't
installed or the cwd isn't a git repo, the tool returns a clean error —
not a stack trace. Output is capped at 50 KB so a 50,000-commit ``log``
can't blow the prompt budget.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from agentcommander.safety.sandbox import require_working_directory
from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult


GIT_TIMEOUT_S = 30.0
MAX_OUTPUT_BYTES = 50_000

# Map verb → argv tail. Anything not in this dict is rejected. Keeping it
# explicit means a future contributor adding "git push" by accident would
# need to add it here on purpose, not just forget to filter the input.
_READ_ONLY_VERBS = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "ls_files",
}


def _build_argv(verb: str, payload: dict[str, Any]) -> list[str] | None:
    """Translate (verb, payload) → an argv list. Returns ``None`` if the
    verb is unsupported or its arguments are invalid."""
    if verb == "status":
        # Porcelain v1 — stable, single-line per file. ``--branch`` adds the
        # current branch as a header so the model sees branch context too.
        return ["status", "--branch", "--porcelain=v1"]

    if verb == "log":
        n = payload.get("n", 10)
        try:
            n_i = int(n)
        except (TypeError, ValueError):
            return None
        n_i = max(1, min(n_i, 200))
        return ["log", f"-{n_i}", "--oneline", "--decorate"]

    if verb == "diff":
        revision = payload.get("revision")
        argv = ["diff", "--stat=200,140", "--no-color"]
        if isinstance(revision, str) and revision.strip():
            # Forbid shell metachars / `--` / leading-dash so a malicious
            # revision can't smuggle additional flags.
            r = revision.strip()
            if any(c in r for c in (";", "|", "&", "$", "`", "<", ">", "\n")):
                return None
            if r.startswith("-"):
                return None
            argv.append(r)
        return argv

    if verb == "show":
        revision = payload.get("revision")
        if not isinstance(revision, str) or not revision.strip():
            return None
        r = revision.strip()
        if any(c in r for c in (";", "|", "&", "$", "`", "<", ">", "\n")):
            return None
        if r.startswith("-"):
            return None
        return ["show", "--no-color", r]

    if verb == "branch":
        return ["branch", "-vv", "--no-color"]

    if verb == "ls_files":
        pattern = payload.get("pattern")
        argv = ["ls-files"]
        if isinstance(pattern, str) and pattern.strip():
            p = pattern.strip()
            # Same metachar guard.
            if any(c in p for c in (";", "|", "&", "$", "`", "<", ">", "\n")):
                return None
            if p.startswith("-"):
                return None
            argv.append("--")
            argv.append(p)
        return argv

    return None


def _git(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    verb = payload.get("verb") or payload.get("input")
    if not isinstance(verb, str):
        return ToolResult(ok=False, error="verb is required (string)")
    verb = verb.strip().lower()
    if verb not in _READ_ONLY_VERBS:
        return ToolResult(
            ok=False,
            error=(f"unsupported verb {verb!r}. Available: "
                   f"{', '.join(sorted(_READ_ONLY_VERBS))}. "
                   f"Mutating git verbs (add/commit/push/...) are "
                   f"intentionally not exposed — use the `execute` tool."),
        )

    argv_tail = _build_argv(verb, payload)
    if argv_tail is None:
        return ToolResult(
            ok=False,
            error=f"invalid arguments for verb {verb!r}",
        )

    git_path = shutil.which("git")
    if git_path is None:
        return ToolResult(
            ok=False,
            error="git is not installed (or not on PATH)",
        )

    try:
        cwd = require_working_directory(ctx.working_directory)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc))

    # Refuse to climb out of the working directory. Git's default behavior
    # walks up the directory tree looking for ``.git/`` — that means
    # running ``git status`` from a subdirectory of a parent repo would
    # report on the PARENT repo, leaking out of the engine's sandbox. The
    # user's expectation is that the working directory is the jail; we
    # enforce it by requiring ``.git`` to exist directly inside cwd
    # (either as a directory or a file — worktrees and submodules use a
    # file pointer, both forms are valid).
    git_marker = Path(cwd) / ".git"
    if not git_marker.exists():
        return ToolResult(
            ok=False,
            error=(f"working directory is not a git repository ({cwd}). "
                   f"git scoping requires .git/ inside the working "
                   f"directory itself — climbing to parent repos is "
                   f"refused so the sandbox stays sealed."),
        )

    cmd = [git_path, *argv_tail]
    # Set GIT_DIR + GIT_WORK_TREE explicitly so git can't accidentally
    # discover a parent repo even if the cwd check above had a gap.
    import os
    git_env = os.environ.copy()
    git_env["GIT_DIR"] = str(git_marker if git_marker.is_dir()
                              else Path(cwd) / ".git")
    git_env["GIT_WORK_TREE"] = str(cwd)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=git_env,
            timeout=GIT_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            ok=False,
            error=f"git {verb} timed out after {GIT_TIMEOUT_S}s",
        )
    except FileNotFoundError as exc:
        return ToolResult(ok=False, error=f"git not found: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")

    truncated = False
    if len(stdout) > MAX_OUTPUT_BYTES:
        stdout = stdout[:MAX_OUTPUT_BYTES]
        truncated = True

    if proc.returncode != 0:
        # Common case: not a git repo. Return a friendly error rather
        # than the raw "fatal: not a git repository" so the model can
        # branch on "repo missing" without parsing.
        msg = stderr.strip() or stdout.strip() or f"git {verb} exited {proc.returncode}"
        if "not a git repository" in msg.lower():
            return ToolResult(
                ok=False,
                error=f"working directory is not a git repository ({cwd})",
            )
        return ToolResult(
            ok=False,
            error=f"git {verb} failed (exit {proc.returncode}): {msg[:1000]}",
            output=stdout or None,
        )

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"output truncated at {MAX_OUTPUT_BYTES // 1000} KB"
        )

    return ToolResult(
        ok=True,
        output=stdout,
        warnings=warnings,
        data={
            "verb": verb,
            "exit_code": proc.returncode,
            "truncated": truncated,
        },
    )


register(ToolDescriptor(
    name="git",
    description=(
        "Read-only git: status, log, diff, show, branch, ls_files. "
        "Runs from the engine's working directory. Mutating verbs (add, "
        "commit, push, reset, checkout) are intentionally NOT exposed — "
        "use the `execute` tool for those."
    ),
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {
            "verb": {"type": "string",
                     "enum": sorted(_READ_ONLY_VERBS)},
            "n": {"type": "integer", "minimum": 1, "maximum": 200},
            "revision": {"type": "string"},
            "pattern": {"type": "string"},
        },
        "required": ["verb"],
    },
    handler=_git,
))
