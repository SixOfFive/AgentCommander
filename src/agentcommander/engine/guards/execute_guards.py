"""Execute guards — run when the action is `execute`.

Fix common LLM code mistakes (language detection, syntax fixes, missing
imports, GUI calls in headless env) and detect stuck retry loops.

Ported from EngineCommander/src/main/orchestration/guards/execute-guards.ts.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.types import ScratchpadEntry

PYTHON_LANGS = {"python", "py"}
JAVASCRIPT_LANGS = {"javascript", "js", "node"}
BASH_LANGS = {"bash", "sh", "shell"}


@dataclass
class _Input:
    code: str
    language: str
    scratchpad: list[ScratchpadEntry]
    iteration: int
    working_directory: str | None
    file_write_registry: dict[str, str]


def _pass(input_: _Input) -> dict[str, Any]:
    return {"code": input_.code, "language": input_.language,
            "verdict": {"action": "pass"}}


def _continue(input_: _Input) -> dict[str, Any]:
    return {"code": input_.code, "language": input_.language,
            "verdict": {"action": "continue"}}


# ─── Individual guards ─────────────────────────────────────────────────────


def python_command_guard(input_: _Input) -> dict[str, Any]:
    if (input_.language == "python"
            and re.match(r"^\s*(python3?|\./)\s+\S+\.py\s*$", input_.code.strip(), re.IGNORECASE)):
        input_.language = "bash"
    return _pass(input_)


def python_param_quote_guard(input_: _Input) -> dict[str, Any]:
    if input_.language in PYTHON_LANGS and "def " in input_.code:
        input_.code = re.sub(
            r'(\(|,\s*)"([a-zA-Z_]\w*)"(\s*[:=)\],])', r"\1\2\3", input_.code,
        )
    return _pass(input_)


def python_unquoted_args_guard(input_: _Input) -> dict[str, Any]:
    if input_.language in PYTHON_LANGS:
        input_.code = re.sub(
            r"\bopen\s*\(([^)]+),\s*([rwab]\+?)\s*\)",
            lambda m: f"open({m.group(1)}, '{m.group(2)}')",
            input_.code,
        )
        input_.code = re.sub(
            r"\bmode\s*=\s*([rwab]\+?)(?=[\s,)])",
            lambda m: f"mode='{m.group(1)}'", input_.code,
        )
    return _pass(input_)


def error_pattern_guard(input_: _Input) -> dict[str, Any]:
    if input_.language in PYTHON_LANGS:
        input_.code = re.sub(
            r"^(\s*)print\s+(?!\()(.*?)$",
            lambda m: f"{m.group(1)}print({m.group(2)})",
            input_.code, flags=re.MULTILINE,
        )
        input_.code = re.sub(
            r"except\s+(\w+)\s*,\s*(\w+)",
            lambda m: f"except {m.group(1)} as {m.group(2)}", input_.code,
        )
        input_.code = re.sub(
            r'(?<![fFbBrRuU])"([^"]*\{[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*\}[^"]*)"',
            lambda m: f'f"{m.group(1)}"', input_.code,
        )
    if input_.language == "bash":
        if (not input_.code.startswith("#!") and not input_.code.startswith("set ")
                and len(input_.code.split("\n")) > 3):
            input_.code = "#!/bin/bash\nset -e\n" + input_.code
    return _pass(input_)


def bash_python_guard(input_: _Input) -> dict[str, Any]:
    if (input_.language == "bash"
            and re.search(r"^\s*(import |from |def |class |#!.*python)", input_.code, re.MULTILINE)):
        input_.language = "python"
    return _pass(input_)


def python3_replacement_guard(input_: _Input) -> dict[str, Any]:
    if (input_.language == "bash" and "python " in input_.code
            and "python3" not in input_.code):
        input_.code = re.sub(r"\bpython\b", "python3", input_.code)
    return _pass(input_)


def pip_npm_command_guard(input_: _Input) -> dict[str, Any]:
    if input_.language == "pip":
        original = input_.code
        input_.code = re.sub(r"^\s*pip3?\s+install\s+(-r\s+\S+|)", "", input_.code, flags=re.IGNORECASE).strip()
        if not input_.code and "-r " in original:
            req_match = re.search(r"-r\s+(\S+)", original)
            if req_match and input_.working_directory:
                req_path = os.path.join(input_.working_directory, req_match.group(1))
                if os.path.isfile(req_path):
                    try:
                        with open(req_path, encoding="utf-8") as f:
                            lines = [
                                line.strip() for line in f
                                if line.strip() and not line.strip().startswith("#")
                            ]
                        input_.code = " ".join(lines)
                    except OSError:
                        pass
        if not input_.code:
            input_.code = re.sub(r"^\s*pip3?\s+install\s+", "", original, flags=re.IGNORECASE).strip()
    if input_.language == "npm":
        input_.code = re.sub(r"^\s*npm\s+install\s+", "", input_.code, flags=re.IGNORECASE).strip()
    return _pass(input_)


def markdown_fence_guard(input_: _Input) -> dict[str, Any]:
    fence = re.match(r"^```(\w*)\s*\n([\s\S]*?)```\s*$", input_.code, re.MULTILINE)
    if fence:
        fence_lang = fence.group(1).lower()
        input_.code = fence.group(2)
        if fence_lang in ("javascript", "js", "node", "ts") and input_.language in PYTHON_LANGS:
            input_.language = "javascript"
        elif fence_lang in ("python", "py") and input_.language in JAVASCRIPT_LANGS:
            input_.language = "python"
        elif fence_lang in BASH_LANGS:
            input_.language = "bash"
    return _pass(input_)


def mixed_shell_python_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS and input_.language != "":
        return _pass(input_)
    lines = [ln.strip() for ln in input_.code.split("\n") if ln.strip()]
    if len(lines) < 2:
        return _pass(input_)

    shell_verbs = re.compile(
        r"^(?:python3?|pytest|pip3?|npm|npx|node|bash|sh|zsh|ls|cat|grep|find|"
        r"mkdir|rm|cp|mv|docker|kubectl|git|curl|wget|echo|which|chmod|chown|"
        r"touch|head|tail|sort|uniq|wc|awk|sed|ps|kill|source|export|env|make|"
        r"du|df|stat|file|basename|dirname|readlink|realpath|tar|zip|unzip|gzip|"
        r"gunzip|tee|xargs|tr|cut|paste|fold|date|hostname|uname|whoami|id|pwd|"
        r"printf|timeout|sleep)\b"
    )
    py_structure = re.compile(
        r"^(?:def |class |import |from \S+ import|if __name__|print\s*\(|@[a-z]|"
        r"async def|try:\s*$|with \S+:|for \S+ in |while |[a-zA-Z_]\w*\s*=\s*(?!=))"
    )
    shell_lines = [ln for ln in lines if shell_verbs.match(ln) and not py_structure.match(ln)]
    py_lines = [ln for ln in lines if py_structure.match(ln) and not shell_verbs.match(ln)]
    if shell_lines and py_lines:
        push_system_nudge(input_.scratchpad, input_.iteration, "mixed_shell_python",
                          f"SYSTEM: execute mixed shell commands and Python in one snippet. "
                          f"AgentCommander cannot execute mixed-language snippets. Issue ONE "
                          f"execute per language.")
        return _continue(input_)
    return _pass(input_)


def cross_language_guard(input_: _Input) -> dict[str, Any]:
    code = input_.code
    if input_.language in PYTHON_LANGS and "import " not in code and "def " not in code:
        js_signals = sum([
            bool(re.search(r"\bconst\s+\w+\s*=", code)),
            bool(re.search(r"\blet\s+\w+\s*=", code)),
            bool(re.search(r"\bconsole\.\w+\s*\(", code)),
            bool(re.search(r"\brequire\s*\(", code)),
            bool(re.search(r"\bfunction\s+\w+\s*\(", code)),
            bool(re.search(r"=>\s*\{", code)),
            bool(re.search(r"\}\s*\)\s*;?\s*$", code)),
        ])
        if js_signals >= 2:
            input_.language = "javascript"

    if (input_.language in JAVASCRIPT_LANGS
            and "const " not in code and "let " not in code and "function " not in code):
        py_signals = sum([
            bool(re.search(r"^(import|from)\s+\w+", code, re.MULTILINE)),
            bool(re.search(r"\bdef\s+\w+\s*\(", code)),
            bool(re.search(r"\bprint\s*\(", code)),
            bool(re.search(r":\s*$", (code.split("\n", 1)[0] if "\n" in code else code))),
            "elif" in code,
            "except" in code,
        ])
        if py_signals >= 2:
            input_.language = "python"
    return _pass(input_)


def empty_execute_guard(input_: _Input) -> dict[str, Any]:
    if not input_.code.strip():
        push_system_nudge(input_.scratchpad, input_.iteration, "empty_execute",
                          "BLOCKED: execute called with empty code.")
        return _continue(input_)
    return _pass(input_)


def infinite_loop_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    if re.search(r"while\s+(True|1)\s*:", input_.code):
        if not re.search(r"\bbreak\b|\breturn\b|\bsys\.exit\b|\braise\b|\bexit\(\)", input_.code):
            input_.code = (
                "import signal\n"
                "def _ac_timeout(signum, frame): raise TimeoutError('Loop exceeded 30s safety limit')\n"
                "signal.signal(signal.SIGALRM, _ac_timeout)\n"
                "signal.alarm(30)\n"
                + input_.code
            )
    return _pass(input_)


def absolute_path_guard(input_: _Input) -> dict[str, Any]:
    return _pass(input_)


def recursive_execution_guard(input_: _Input) -> dict[str, Any]:
    if input_.language != "bash":
        return _pass(input_)
    if (re.search(r"\$0|\$BASH_SOURCE|\bsource\s+\$", input_.code)
            or re.search(r"\bexec\s+\$0", input_.code)):
        push_system_nudge(input_.scratchpad, input_.iteration, "recursive_script",
                          "BLOCKED: script appears to execute itself recursively. "
                          "Rewrite without self-referencing.")
        return _continue(input_)
    return _pass(input_)


def destructive_command_guard(input_: _Input) -> dict[str, Any]:
    if input_.language != "bash":
        return _pass(input_)
    if re.search(r"\brm\s+(-[rfRF]{2,}|--recursive\s+--force|--force\s+--recursive)\b",
                 input_.code):
        push_system_nudge(input_.scratchpad, input_.iteration, "destructive_command",
                          "BLOCKED: rm -rf is prohibited. Use targeted delete_file actions.")
        return _continue(input_)
    if re.search(r"\b(mkfs|dd\s+if=|:\(\)\{|fork\s*bomb|>\s*/dev/sd)", input_.code, re.IGNORECASE):
        push_system_nudge(input_.scratchpad, input_.iteration, "destructive_command",
                          "BLOCKED: dangerous system command detected. Use safe alternatives.")
        return _continue(input_)
    return _pass(input_)


def secrets_in_code_guard(input_: _Input) -> dict[str, Any]:
    patterns = [
        r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[=:]\s*[\"'][a-zA-Z0-9_\-]{20,}[\"']",
        r"sk-[a-zA-Z0-9]{20,}",
        r"sk-or-v1-[a-f0-9]{64}",
        r"ghp_[a-zA-Z0-9]{36}",
        r"glpat-[a-zA-Z0-9\-_]{20,}",
        r"xox[bposa]-[a-zA-Z0-9\-]+",
        r"AIza[a-zA-Z0-9_\-]{35}",
        r"AKIA[A-Z0-9]{16}",
        r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----",
    ]
    for p in patterns:
        if re.search(p, input_.code, re.IGNORECASE):
            push_system_nudge(input_.scratchpad, input_.iteration, "secrets_in_code",
                              "WARNING: code appears to contain a hardcoded API key or secret. "
                              "Use environment variables or a .env file instead.")
            break
    return _pass(input_)


def sleep_cap_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    def repl(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if n > 30:
            return f"time.sleep(30)  # capped from {n}s"
        return m.group(0)
    input_.code = re.sub(r"\btime\.sleep\s*\(\s*(\d+)\s*\)", repl, input_.code)
    return _pass(input_)


def package_manager_flag_guard(input_: _Input) -> dict[str, Any]:
    if input_.language != "bash":
        return _pass(input_)
    for cmd in ("apt-get", "apt", "yum", "dnf"):
        if (re.search(rf"\b{cmd}\s+install\b", input_.code)
                and not re.search(r"-y\b", input_.code)):
            input_.code = re.sub(rf"\b{cmd}\s+install\b", f"{cmd} install -y", input_.code)
    return _pass(input_)


def async_await_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    has_async_def = bool(re.search(r"\basync\s+def\b", code))
    has_asyncio_run = bool(re.search(r"asyncio\.run\s*\(", code))
    has_await = "await" in code
    if has_async_def and has_await and not has_asyncio_run and "# no-asyncio-run" not in code:
        m = re.search(r"async\s+def\s+(\w+)", code)
        if m:
            func_name = m.group(1)
            call_pattern = re.compile(rf"^{func_name}\s*\(", re.MULTILINE)
            if call_pattern.search(code):
                code = call_pattern.sub(f"asyncio.run({func_name}(", code)
                if "import asyncio" not in code:
                    code = "import asyncio\n" + code
                input_.code = code
    return _pass(input_)


_IMPORT_CHECKS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bjson\.(dumps|loads|load|dump)\b"), "json", "import json"),
    (re.compile(r"\bos\.(path|getcwd|listdir|mkdir|makedirs|environ|system|remove)\b"),
     "os", "import os"),
    (re.compile(r"\bsys\.(argv|exit|path|stdin|stdout)\b"), "sys", "import sys"),
    (re.compile(r"\bre\.(match|search|findall|sub|compile|split)\b"), "re", "import re"),
    (re.compile(r"\bmath\.(sqrt|ceil|floor|pi|log|sin|cos)\b"), "math", "import math"),
    (re.compile(r"\bdatetime\.(datetime|date|time|timedelta)\b"),
     "datetime", "from datetime import datetime, timedelta"),
    (re.compile(r"\brandom\.(randint|choice|random|shuffle|sample)\b"), "random", "import random"),
    (re.compile(r"\bcsv\.(reader|writer|DictReader|DictWriter)\b"), "csv", "import csv"),
    (re.compile(r"\btime\.(sleep|time|strftime|perf_counter)\b"), "time", "import time"),
    (re.compile(r"\bpathlib\.Path\b|\bPath\s*\("), "pathlib", "from pathlib import Path"),
    (re.compile(r"\bcollections\.(Counter|defaultdict|OrderedDict|namedtuple)\b"),
     "collections", "import collections"),
    (re.compile(r"\bCounter\s*\("), "Counter", "from collections import Counter"),
    (re.compile(r"\bdefaultdict\s*\("), "defaultdict", "from collections import defaultdict"),
]


def missing_import_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    additions: list[str] = []
    for usage_rx, mod, statement in _IMPORT_CHECKS:
        if usage_rx.search(code):
            has = re.search(rf"\b(import\s+{mod}|from\s+{mod}\s+import)\b", code)
            if not has:
                additions.append(statement)
    if additions:
        input_.code = "\n".join(additions) + "\n" + code
    return _pass(input_)


def gui_blocking_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    if (re.search(r"\bplt\.show\s*\(\s*\)", code)
            or re.search(r"\bmatplotlib\.pyplot.*\.show\s*\(\s*\)", code)):
        if "savefig" not in code:
            code = re.sub(
                r"\bplt\.show\s*\(\s*\)",
                "plt.savefig('output.png', dpi=150, bbox_inches='tight')\n"
                "print('Chart saved to output.png')",
                code,
            )
        else:
            code = re.sub(r"\bplt\.show\s*\(\s*\)", "# plt.show() removed (headless)", code)
    if re.search(r"\b(tkinter|tk)\b", code) and re.search(r"\.mainloop\s*\(\s*\)", code):
        code = re.sub(r"\.mainloop\s*\(\s*\)",
                      "  # .mainloop() removed (headless environment)", code)
    if re.search(r"\bwebbrowser\.open\s*\(", code):
        code = re.sub(r"\bwebbrowser\.open\s*\(([^)]+)\)",
                      r'print(f"URL: {\1}")', code)
    input_.code = code
    return _pass(input_)


def os_system_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    if re.search(r"\bos\.system\s*\(", code):
        code = re.sub(r"\bos\.system\s*\(([^)]+)\)",
                      r"subprocess.run(\1, shell=True, check=True)", code)
        if "import subprocess" not in code:
            code = "import subprocess\n" + code
        input_.code = code
    return _pass(input_)


def tmp_path_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS and input_.language != "bash":
        return _pass(input_)
    if re.search(r"[\"']/tmp/", input_.code):
        input_.code = re.sub(r"([\"'])/tmp/", lambda m: f"{m.group(1)}./", input_.code)
    return _pass(input_)


def subprocess_timeout_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    if (re.search(r"subprocess\.(run|call|check_output|check_call)\s*\(", code)
            and not re.search(r"timeout\s*=", code)):
        def add_timeout(m: re.Match[str]) -> str:
            method, args = m.group(1), m.group(2)
            if args.strip().endswith(","):
                return f"subprocess.{method}({args} timeout=60)"
            return f"subprocess.{method}({args}, timeout=60)"
        code = re.sub(r"subprocess\.(run|call|check_output|check_call)\s*\(([^)]*)\)",
                      add_timeout, code)
        input_.code = code
    return _pass(input_)


def requests_error_handling_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    code = input_.code
    uses_requests = bool(re.search(r"\brequests\.(get|post|put|delete|patch|head)\s*\(", code))
    has_try = "try:" in code
    if uses_requests and not has_try and len(code.split("\n")) < 30:
        if not re.search(r"^\s*def\s+", code, re.MULTILINE):
            indented = "\n".join("    " + ln for ln in code.split("\n"))
            input_.code = (
                "try:\n" + indented +
                '\nexcept requests.exceptions.RequestException as e:\n'
                '    print(f"Request failed: {e}")'
            )
    return _pass(input_)


def encoding_guard(input_: _Input) -> dict[str, Any]:
    if input_.language not in PYTHON_LANGS:
        return _pass(input_)
    has_non_ascii = any(ord(c) > 0x7F for c in input_.code)
    has_encoding = bool(re.search(r"^#.*coding[:=]\s*(utf-?8|ascii|latin-?1)",
                                   input_.code, re.MULTILINE | re.IGNORECASE))
    if has_non_ascii and not has_encoding and not input_.code.startswith("#!"):
        input_.code = "# -*- coding: utf-8 -*-\n" + input_.code
    return _pass(input_)


def file_path_guard(input_: _Input) -> dict[str, Any]:
    if input_.language == "python" and input_.working_directory:
        candidate = input_.code.strip()
        if re.match(r"^\S+\.py$", candidate):
            full = os.path.join(input_.working_directory, candidate)
            if os.path.isfile(full):
                try:
                    with open(full, encoding="utf-8") as f:
                        input_.code = f.read()
                except OSError:
                    pass
    return _pass(input_)


def file_typo_guard(input_: _Input) -> dict[str, Any]:
    """Catch the orchestrator referencing a file that doesn't exist but a
    similar-named one was written this run.

    Real-world failure mode: orchestrator writes ``linked_list.py`` then
    tries ``python linkedlist.py``. The execute fails with FileNotFound,
    orchestrator retries the same wrong path, ~30 failures accumulate
    before the run gives up. This guard catches it before dispatch.

    Detection logic:
      1. Pull every ``<name>.<ext>`` token referenced in the execute code
      2. For each referenced file:
         - skip if it exists on disk (real)
         - skip if file_write_registry has it (just written this run)
         - look for a similar name in the registry (Levenshtein ≤ 3 OR
           one contains the other after stripping non-alphanumerics)
         - if found, nudge: "you said X, but you wrote Y — fix it"
    """
    if not input_.file_write_registry:
        return _pass(input_)

    # Tokenize file references in the code. Match `name.ext` with a
    # word-boundary on either side so we don't pick up substrings of
    # paths like /etc/hosts.
    refs = set(re.findall(r"\b([\w.\-/]+\.(?:py|js|ts|sh|rb|go|rs))\b",
                          input_.code))
    if not refs:
        return _pass(input_)

    written_paths = set(input_.file_write_registry.keys())
    if not written_paths:
        return _pass(input_)

    def _slug(name: str) -> str:
        """Strip dirs, extension, and non-alphanumerics — coder/coder_v2/
        Coder all collapse to 'coder'. Used for fuzzy comparison."""
        base = os.path.basename(name)
        base, _ = os.path.splitext(base)
        return re.sub(r"[^a-z0-9]", "", base.lower())

    def _levenshtein(a: str, b: str, *, cap: int = 4) -> int:
        """Bounded Levenshtein — returns ``cap`` if distance ≥ cap.
        Cheaper than the full O(mn) version for our short-string case
        and lets us bail out early."""
        if a == b:
            return 0
        m, n = len(a), len(b)
        if abs(m - n) >= cap:
            return cap
        # Two-row DP
        prev = list(range(n + 1))
        for i, ca in enumerate(a, 1):
            curr = [i] + [0] * n
            min_in_row = curr[0]
            for j, cb in enumerate(b, 1):
                cost = 0 if ca == cb else 1
                curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
                if curr[j] < min_in_row:
                    min_in_row = curr[j]
            if min_in_row >= cap:
                return cap
            prev = curr
        return min(prev[n], cap)

    for ref in refs:
        # Resolve to absolute path under working_directory and check disk
        full = (
            os.path.join(input_.working_directory, ref)
            if input_.working_directory and not os.path.isabs(ref)
            else ref
        )
        if os.path.isfile(full):
            continue  # exists, no typo
        # Match against in-registry paths (compare basenames since the
        # registry stores absolute paths from write_file)
        ref_slug = _slug(ref)
        if not ref_slug:
            continue
        if any(_slug(p) == ref_slug for p in written_paths):
            continue  # exact slug match — same file by another name
        # Find a fuzzy match in the registry
        candidates = []
        for written in written_paths:
            written_slug = _slug(written)
            if not written_slug:
                continue
            # substring match (linkedlist ⊂ linked_list_v2 etc.)
            if ref_slug in written_slug or written_slug in ref_slug:
                candidates.append(written)
                continue
            # bounded Levenshtein on slugs
            if _levenshtein(ref_slug, written_slug, cap=4) <= 3:
                candidates.append(written)
        if candidates:
            suggestion = candidates[0]
            push_system_nudge(
                input_.scratchpad, input_.iteration, "file_typo",
                f"You're trying to execute {ref!r} but it doesn't exist. "
                f"You wrote {os.path.basename(suggestion)!r} this run. "
                f"Did you mean to use {os.path.basename(suggestion)!r}?",
            )
            return _continue(input_)

    return _pass(input_)


def repeated_execute_failure_guard(input_: _Input) -> dict[str, Any]:
    failures = [
        e for e in input_.scratchpad
        if e.action == "execute" and e.output and (
            "failed" in e.output or "Error" in e.output or "error" in e.output
        )
    ]
    if len(failures) < 2:
        return _pass(input_)

    def sig(s: str) -> str:
        err = re.search(r"(\w+Error):\s*(.{0,60})", s)
        loc = re.search(r'File "([^"]+)", line (\d+)', s)
        err_type = err.group(1) if err else ""
        err_msg = err.group(2).strip() if err else ""
        file_line = (f"{os.path.basename(loc.group(1))}:{loc.group(2)}" if loc else "")
        return f"{err_type}|{file_line}|{err_msg[:40]}"

    last_sig = sig(failures[-1].output or "")
    prev_sig = sig(failures[-2].output or "")
    same_pattern = len(last_sig) > 5 and last_sig == prev_sig
    too_many = len(failures) >= 3

    # Check whether the debugger role is configured (lazy import to avoid
    # pulling repos in tests that don't init the DB).
    debugger_configured = False
    try:
        from agentcommander.engine.role_resolver import resolve as _resolve
        debugger_configured = _resolve("debugger") is not None
    except Exception:  # noqa: BLE001
        debugger_configured = False

    if (same_pattern or too_many) and debugger_configured:
        last_err = failures[-1].output or ""
        idx = last_err.find("Stderr:")
        snippet_start = idx if idx >= 0 else 0
        err_snippet = last_err[snippet_start:snippet_start + 500]
        push_system_nudge(input_.scratchpad, input_.iteration, "execute-retry-loop",
                          f"STOP RETRYING. Execute has failed {len(failures)} times with the "
                          f'same error. You MUST use the "debug" action NOW:\n\n'
                          f"{err_snippet[:400]}\n\n"
                          f"The debugger will diagnose the root cause. Do NOT call execute "
                          f"again until debug responds.")
        return _continue(input_)
    return _pass(input_)


def pre_execute_verify_guard(input_: _Input) -> dict[str, Any]:
    if not input_.file_write_registry:
        return _pass(input_)
    is_server_run = (
        re.search(r"\b(main|app|server|run|start|uvicorn|gunicorn|flask)\b",
                  input_.code, re.IGNORECASE)
        and input_.language in PYTHON_LANGS | BASH_LANGS | JAVASCRIPT_LANGS
    )
    if not is_server_run:
        return _pass(input_)
    failed = [(p, s) for p, s in input_.file_write_registry.items() if s == "syntax-fail"]
    if not failed:
        return _pass(input_)
    file_list = ", ".join(p for p, _ in failed)
    push_system_nudge(input_.scratchpad, input_.iteration, "pre_execute_verify",
                      f"BLOCKED: cannot run the project — {len(failed)} file(s) have syntax "
                      f"errors: {file_list}. Fix the syntax errors first with write_file.")
    return _continue(input_)


# ─── Runner ────────────────────────────────────────────────────────────────


def run_execute_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run execute guards in sequence. Threads code/language through.

    ctx: {code, language, scratchpad, iteration, working_directory, file_write_registry}
    Returns: {code, language, verdict: {action, final_output?}}.
    """
    input_ = _Input(
        code=ctx.get("code", ""),
        language=ctx.get("language", ""),
        scratchpad=ctx["scratchpad"],
        iteration=ctx["iteration"],
        working_directory=ctx.get("working_directory"),
        file_write_registry=ctx.get("file_write_registry") or {},
    )

    verify = pre_execute_verify_guard(input_)
    if verify["verdict"]["action"] == "continue":
        return verify

    guards: list[Any] = [
        markdown_fence_guard, empty_execute_guard, destructive_command_guard,
        recursive_execution_guard, python_command_guard, mixed_shell_python_guard,
        cross_language_guard, bash_python_guard, python3_replacement_guard,
        python_param_quote_guard, python_unquoted_args_guard, error_pattern_guard,
        async_await_guard, missing_import_guard, gui_blocking_guard,
        os_system_guard, subprocess_timeout_guard, encoding_guard,
        pip_npm_command_guard, file_path_guard, package_manager_flag_guard,
        tmp_path_guard, secrets_in_code_guard, sleep_cap_guard,
        infinite_loop_guard, absolute_path_guard, requests_error_handling_guard,
        repeated_execute_failure_guard,
    ]
    for guard in guards:
        result = guard(input_)
        if result["verdict"]["action"] == "continue":
            return result
        input_.code = result["code"]
        input_.language = result["language"]

    return {"code": input_.code, "language": input_.language,
            "verdict": {"action": "pass"}}
