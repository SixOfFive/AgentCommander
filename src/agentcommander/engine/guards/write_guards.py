"""Write guards — run when the action is `write_file`.

Inspect content quality, detect duplicates, block overwrites of critical
files, catch truncated/placeholder code before it hits disk.

Ported from EngineCommander/src/main/orchestration/guards/write-guards.ts.
"""
from __future__ import annotations

import re
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.types import ScratchpadEntry

MAX_FILE_SIZE = 100_000

_CODE_FILE_RX = re.compile(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c|html|css)$", re.IGNORECASE)
_CODE_BRACE_RX = re.compile(r"\.(js|ts|java|cpp|c|go|rs)$", re.IGNORECASE)
_PY_RX = re.compile(r"\.py$", re.IGNORECASE)
_HTML_RX = re.compile(r"\.html?$", re.IGNORECASE)
_TEST_FILENAME_RX = re.compile(r"\b(test_|_test|\.test\.|\.spec\.)", re.IGNORECASE)
_TEST_DIR_RX = re.compile(r"\b(tests?|__tests__|spec)\b", re.IGNORECASE)
_PLACEHOLDER_RX = re.compile(
    r"^\s*(#\s*TODO|//\s*TODO|#\s*FIXME|//\s*FIXME|pass\s*$|\.\.\.|\.\.\.\s*$"
    r"|#\s*placeholder|//\s*placeholder|#\s*implement|//\s*implement"
    r"|#\s*add .* here|//\s*add .* here|#\s*your .* here|//\s*your .* here)",
    re.IGNORECASE,
)
_PY_UNFINISHED_RX = re.compile(
    r"^(\s*)(def|class|if|for|while|try|except|with)\s+.*:\s*$"
)
_LINE_COMMENT_RX = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RX = re.compile(r"/\*[\s\S]*?\*/")
_DBL_STR_RX = re.compile(r'"(?:[^"\\]|\\.)*"')
_SGL_STR_RX = re.compile(r"'(?:[^'\\]|\\.)*'")
_BACKTICK_STR_RX = re.compile(r"`(?:[^`\\]|\\.)*`")

_CRITICAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.env$", re.IGNORECASE),
    re.compile(r"^\.gitignore$", re.IGNORECASE),
    re.compile(r"^package\.json$", re.IGNORECASE),
    re.compile(r"^package-lock\.json$", re.IGNORECASE),
    re.compile(r"^pyproject\.toml$", re.IGNORECASE),
    re.compile(r"^poetry\.lock$", re.IGNORECASE),
    re.compile(r"^tsconfig\.json$", re.IGNORECASE),
    re.compile(r"^docker-compose\.ya?ml$", re.IGNORECASE),
    re.compile(r"^Dockerfile$", re.IGNORECASE),
    re.compile(r"^\.github/", re.IGNORECASE),
    re.compile(r"^\.gitlab-ci\.yml$", re.IGNORECASE),
]


def empty_file_write_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, _file_path: str, file_content: str,
) -> GuardVerdict:
    if not file_content.strip():
        push_system_nudge(scratchpad, iteration, "empty_write",
                          "BLOCKED: write_file called with empty content. "
                          "Provide actual content in the \"content\" field.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def duplicate_write_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, _file_content: str,
) -> GuardVerdict:
    prev = next(
        (e for e in reversed(scratchpad)
         if e.action == "write_file" and e.input == file_path
         and "Successfully" in (e.output or "")),
        None,
    )
    if prev is None:
        return GuardVerdict(action="pass")
    idx = scratchpad.index(prev)
    after = scratchpad[idx + 1:]
    no_changes = not any(
        e.role in ("coder", "debugger")
        or (e.action == "execute" and "successfully" in (e.output or ""))
        for e in after
    )
    same_failure = any(
        e.action == "execute" and ("Error" in (e.output or "") or "failed" in (e.output or ""))
        for e in after
    )
    if no_changes and same_failure:
        push_system_nudge(scratchpad, iteration, "duplicate_write",
                          f'WARNING: You are writing "{file_path}" again but haven\'t changed '
                          f"the approach since the last failure. Use the debugger first or try "
                          f"a fundamentally different solution.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def placeholder_content_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, file_content: str,
) -> GuardVerdict:
    if not _CODE_FILE_RX.search(file_path):
        return GuardVerdict(action="pass")
    lines = [ln for ln in file_content.split("\n") if ln.strip()]
    if len(lines) < 3:
        return GuardVerdict(action="pass")
    placeholder_lines = sum(1 for ln in lines if _PLACEHOLDER_RX.search(ln))
    if placeholder_lines >= 3 and placeholder_lines / len(lines) > 0.3:
        push_system_nudge(scratchpad, iteration, "placeholder_content",
                          f'WARNING: "{file_path}" has {placeholder_lines} placeholder lines '
                          f"(TODO, FIXME, pass, …). Write COMPLETE, runnable code.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def truncated_code_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, file_content: str,
) -> GuardVerdict:
    if not re.search(r"\.(py|js|ts|java|cpp|c|go|rs|rb)$", file_path, re.IGNORECASE):
        return GuardVerdict(action="pass")
    if len(file_content) < 50:
        return GuardVerdict(action="pass")

    if _CODE_BRACE_RX.search(file_path):
        stripped = _LINE_COMMENT_RX.sub("", file_content)
        stripped = _BLOCK_COMMENT_RX.sub("", stripped)
        stripped = _DBL_STR_RX.sub('""', stripped)
        stripped = _SGL_STR_RX.sub("''", stripped)
        stripped = _BACKTICK_STR_RX.sub("``", stripped)
        braces = stripped.count("{") - stripped.count("}")
        parens = stripped.count("(") - stripped.count(")")
        brackets = stripped.count("[") - stripped.count("]")
        imbalance = abs(braces) + abs(parens) + abs(brackets)
        if imbalance > 2:
            details: list[str] = []
            if braces > 0:
                details.append(f"{braces} unclosed {{")
            if parens > 0:
                details.append(f"{parens} unclosed (")
            if brackets > 0:
                details.append(f"{brackets} unclosed [")
            push_system_nudge(scratchpad, iteration, "truncated_code",
                              f'WARNING: "{file_path}" appears truncated — '
                              f'unbalanced delimiters ({", ".join(details)}). '
                              f"Rewrite the COMPLETE file.")
            return GuardVerdict(action="continue")

    if _PY_RX.search(file_path):
        last_line = (file_content.rstrip().split("\n")[-1] if file_content.strip() else "")
        if _PY_UNFINISHED_RX.match(last_line):
            push_system_nudge(scratchpad, iteration, "truncated_code",
                              f'WARNING: "{file_path}" ends with "{last_line.strip()}" '
                              f"which needs a body. Rewrite the COMPLETE file.")
            return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def huge_file_write_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, file_content: str,
) -> GuardVerdict:
    if len(file_content) > MAX_FILE_SIZE:
        push_system_nudge(scratchpad, iteration, "huge_file_write",
                          f'BLOCKED: write_file for "{file_path}" has {len(file_content)} '
                          f"chars (~{len(file_content) // 1024} KB). Likely a mistake — "
                          f"write smaller, focused files.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def critical_file_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, user_message: str,
) -> GuardVerdict:
    basename = re.sub(r"^.*[/\\]", "", file_path)
    is_critical = any(p.search(basename) or p.search(file_path) for p in _CRITICAL_PATTERNS)
    if not is_critical:
        return GuardVerdict(action="pass")
    file_ref = re.escape(basename)
    if re.search(file_ref, user_message, re.IGNORECASE):
        return GuardVerdict(action="pass")
    has_read = any(e.action == "read_file" and e.input == file_path for e in scratchpad)
    if not has_read:
        push_system_nudge(scratchpad, iteration, "critical_file_overwrite",
                          f'WARNING: about to overwrite critical file "{file_path}" without '
                          f"reading it first. Read with read_file before overwriting.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def identical_rewrite_guard(
    scratchpad: list[ScratchpadEntry], iteration: int, file_path: str, file_content: str,
) -> GuardVerdict:
    last_read = next(
        (e for e in reversed(scratchpad) if e.action == "read_file" and e.input == file_path),
        None,
    )
    if last_read is None:
        return GuardVerdict(action="pass")
    read_content = (last_read.output or "").strip()
    write_content = file_content.strip()
    if read_content and read_content == write_content:
        push_system_nudge(scratchpad, iteration, "identical_rewrite",
                          f'SKIPPED: write_file for "{file_path}" has identical content to '
                          f"the last read_file. No changes to apply. Move on.")
        return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


def html_doctype_guard(
    _scratchpad: list[ScratchpadEntry], _iteration: int, file_path: str, file_content: str,
) -> GuardVerdict:
    if _HTML_RX.search(file_path):
        if "<html" in file_content and not file_content.strip().lower().startswith("<!doctype"):
            # Just log — caller may auto-prepend or pass through.
            pass
    return GuardVerdict(action="pass")


def test_file_location_guard(
    _scratchpad: list[ScratchpadEntry], _iteration: int, file_path: str, _file_content: str,
) -> GuardVerdict:
    is_test = bool(_TEST_FILENAME_RX.search(file_path))
    in_test_dir = bool(_TEST_DIR_RX.search(file_path))
    if is_test and not in_test_dir and ("/" in file_path or "\\" in file_path):
        # Soft warning, no block.
        pass
    return GuardVerdict(action="pass")


def strip_markdown_fences(content: str) -> str:
    """Helper: strip surrounding ``` ... ``` if the whole content is fenced."""
    m = re.match(r"^```\w*\s*\n([\s\S]*?)```\s*$", content)
    return m.group(1) if m else content


# ─── Runner ─────────────────────────────────────────────────────────────────


def run_write_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run write guards in sequence. Returns the first non-pass verdict.

    Engine passes ctx with: scratchpad, iteration, file_path, file_content, user_message.
    Returns a dict with keys: action ('pass'|'continue'|'break'), final_output (optional).
    """
    scratchpad = ctx["scratchpad"]
    iteration = ctx["iteration"]
    file_path = ctx["file_path"]
    file_content = ctx["file_content"]
    user_message = ctx.get("user_message", "")

    guards = [
        lambda: empty_file_write_guard(scratchpad, iteration, file_path, file_content),
        lambda: huge_file_write_guard(scratchpad, iteration, file_path, file_content),
        lambda: identical_rewrite_guard(scratchpad, iteration, file_path, file_content),
        lambda: duplicate_write_guard(scratchpad, iteration, file_path, file_content),
        lambda: placeholder_content_guard(scratchpad, iteration, file_path, file_content),
        lambda: truncated_code_guard(scratchpad, iteration, file_path, file_content),
        lambda: critical_file_guard(scratchpad, iteration, file_path, user_message),
        lambda: html_doctype_guard(scratchpad, iteration, file_path, file_content),
        lambda: test_file_location_guard(scratchpad, iteration, file_path, file_content),
    ]
    for guard in guards:
        verdict = guard()
        if verdict.action != "pass":
            return {"action": verdict.action, "final_output": verdict.final_output}
    return {"action": "pass"}
