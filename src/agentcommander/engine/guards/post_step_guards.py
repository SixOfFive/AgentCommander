"""Post-step guards — run after a role produces output OR after a tool runs.

Detect dead-ends (same output 3x), stuck retry patterns (3 consecutive
failures of the same action), repeated error signatures, ModuleNotFoundError
auto-install nudge, NoneType attribute hints (with API recommendations for
known-bad scrape targets like Hacker News / Reddit).

Ported from EngineCommander/src/main/orchestration/guards/post-step-guards.ts.
"""
from __future__ import annotations

import re
import time
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.engine.scratchpad import build_final_output
from agentcommander.types import ScratchpadEntry


_ERR_SIGNATURE_RXS: list[re.Pattern[str]] = [
    re.compile(r"([A-Z][a-zA-Z]*(?:Error|Exception)):\s*(.+?)(?:\n|$)"),
    re.compile(r"(?:^|\n)([A-Z][a-zA-Z]*(?:Error))\s*:\s*(.+?)(?:\n|$)"),
    re.compile(r"(\w+):\s*(command not found|No such file|Permission denied)"),
    re.compile(r"(?:Failed|FAILED):\s*(.+?)(?:\n|$)"),
]
_TMPFILE_RX = re.compile(r"[_\w-]+_(ec|ac)_temp_\d+\.\w+")
_LINE_NUM_RX = re.compile(r"line\s+\d+", re.IGNORECASE)
_LINE_COL_RX = re.compile(r":\d+:\d+")
_PID_RX = re.compile(r"pid[= ]\d+", re.IGNORECASE)
_HEX_PTR_RX = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)

_PY_STDLIB: frozenset[str] = frozenset({
    "os", "sys", "json", "re", "math", "time", "datetime", "pathlib",
    "subprocess", "threading", "logging", "itertools", "functools",
    "collections", "typing", "argparse", "socket", "io", "random", "copy",
})

_IMPORT_TO_PIP: dict[str, str] = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "magic": "python-magic",
}


def dead_end_guard(output_hashes: dict[str, int], role: str, validated_output: str,
                   scratchpad: list[ScratchpadEntry]) -> GuardVerdict:
    output_hash = f"{role}:{validated_output[:100]}"
    count = output_hashes.get(output_hash, 0) + 1
    output_hashes[output_hash] = count
    if count >= 3:
        return GuardVerdict(action="break", final_output=build_final_output(scratchpad))
    return GuardVerdict(action="pass")


def anti_stuck_guard(scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    if len(scratchpad) < 3:
        return GuardVerdict(action="pass")
    last_n = scratchpad[-3:]
    same_action = all(e.action == last_n[0].action for e in last_n)
    all_failed = all(
        any(m in (e.output or "") for m in ("Error", "failed", "ERROR", "BLOCKED"))
        for e in last_n
    )
    if same_action and all_failed:
        push_system_nudge(scratchpad, iteration, f"anti_stuck:{last_n[0].action}",
                          f'SYSTEM: The "{last_n[0].action}" action has failed 3 times in a row. '
                          f"Do NOT retry the same approach. Try a completely different strategy: "
                          f"skip the failing step, use an alternative tool, simplify, or move on.")
    return GuardVerdict(action="pass")


def _error_signature(output: str) -> str | None:
    if not output:
        return None
    for rx in _ERR_SIGNATURE_RXS:
        m = rx.search(output)
        if m:
            normalized = m.group(0)
            normalized = _TMPFILE_RX.sub("<tmpfile>", normalized)
            normalized = _LINE_NUM_RX.sub("line N", normalized)
            normalized = _LINE_COL_RX.sub(":N:N", normalized)
            normalized = _PID_RX.sub("pid=N", normalized)
            normalized = _HEX_PTR_RX.sub("0xN", normalized)
            return normalized[:200]
    return None


def repeat_error_guard(scratchpad: list[ScratchpadEntry], iteration: int) -> GuardVerdict:
    sigs: dict[str, int] = {}
    for e in scratchpad:
        sig = _error_signature(e.output or "")
        if sig:
            sigs[sig] = sigs.get(sig, 0) + 1
    hottest: tuple[str, int] | None = None
    for sig, count in sigs.items():
        if count >= 2 and (hottest is None or count > hottest[1]):
            hottest = (sig, count)
    if hottest is None:
        return GuardVerdict(action="pass")
    sig_text, count = hottest
    already_nudged = any(
        e.role == "tool" and e.action == "repeat_error_nudge"
        and isinstance(e.input, str) and sig_text in e.input
        for e in reversed(scratchpad)
    )
    if already_nudged:
        return GuardVerdict(action="pass")
    scratchpad.append(ScratchpadEntry(
        step=iteration, role="tool", action="repeat_error_nudge",
        input=sig_text,
        output=(f"SYSTEM: The same error has occurred {count} times: "
                f'"{sig_text[:120]}". Your previous fixes did NOT resolve it. '
                f"STOP reusing the broken approach. Choose ONE: "
                f"(a) use an entirely different method (different library, different data source); "
                f"(b) add defensive null-checks and error handling so the failure no longer blocks progress; "
                f'(c) call "done" with a clear explanation of what worked and what didn\'t.'),
        timestamp=time.time(),
    ))
    return GuardVerdict(action="pass")


def module_not_found_autoinstall_guard(scratchpad: list[ScratchpadEntry],
                                        iteration: int) -> GuardVerdict:
    if not scratchpad:
        return GuardVerdict(action="pass")
    last = scratchpad[-1]
    if not last.output:
        return GuardVerdict(action="pass")
    m = re.search(
        r"(?:ModuleNotFoundError|ImportError):\s*No module named\s*['\"]([a-zA-Z0-9_.\-]+)['\"]",
        last.output,
    )
    if not m:
        return GuardVerdict(action="pass")
    pkg = m.group(1).split(".")[0]
    if pkg in _PY_STDLIB:
        return GuardVerdict(action="pass")
    pip_name = _IMPORT_TO_PIP.get(pkg, pkg)
    recently_nudged = any(
        e.role == "tool" and e.action == "autoinstall_nudge"
        and isinstance(e.input, str) and e.input == pip_name
        for e in scratchpad[-5:]
    )
    if recently_nudged:
        return GuardVerdict(action="pass")
    scratchpad.append(ScratchpadEntry(
        step=iteration, role="tool", action="autoinstall_nudge",
        input=pip_name,
        output=(f'SYSTEM: Missing Python package "{pkg}". Your NEXT action must be an '
                f'execute with language=bash and input=`pip install {pip_name}` — do NOT '
                f"retry the script until this install succeeds. After the install, retry the original execute."),
        timestamp=time.time(),
    ))
    return GuardVerdict(action="pass")


_NONETYPE_RX = re.compile(
    r"AttributeError:\s*['\"]?NoneType['\"]?\s*object\s*has\s*no\s*attribute"
)
_HN_RX = re.compile(r"news\.ycombinator\.com|hacker\s*news|\bHN\b", re.IGNORECASE)
_REDDIT_RX = re.compile(r"reddit\.com", re.IGNORECASE)


def nonetype_attribute_hint_guard(scratchpad: list[ScratchpadEntry],
                                   iteration: int) -> GuardVerdict:
    if not scratchpad:
        return GuardVerdict(action="pass")
    last = scratchpad[-1]
    if not last.output or not _NONETYPE_RX.search(last.output):
        return GuardVerdict(action="pass")
    recently_nudged = any(
        e.role == "tool" and e.action == "nonetype_nudge" for e in scratchpad[-4:]
    )
    if recently_nudged:
        return GuardVerdict(action="pass")
    recent_blob = "\n".join(
        f"{e.input or ''} {e.output or ''}" for e in scratchpad[-8:]
    )
    api_hint = ""
    if _HN_RX.search(recent_blob):
        api_hint = (
            " For Hacker News specifically, STOP scraping HTML. Use the official Firebase API: "
            "GET https://hacker-news.firebaseio.com/v0/topstories.json returns story IDs; "
            "GET https://hacker-news.firebaseio.com/v0/item/<id>.json for each story's "
            "{title, score, descendants}."
        )
    elif _REDDIT_RX.search(recent_blob):
        api_hint = (
            " For Reddit, append .json to any URL "
            "(e.g. https://www.reddit.com/r/programming/top.json) "
            "to get structured JSON without scraping."
        )
    scratchpad.append(ScratchpadEntry(
        step=iteration, role="tool", action="nonetype_nudge",
        input="nonetype",
        output=(
            "SYSTEM: AttributeError on NoneType means a prior step returned None and the next "
            "line tried to access an attribute on it. Fix pattern: wrap the lookup in a null "
            "check — e.g. `el = row.find('span', class_='score'); score = int(el.text) if el else 0`. "
            'Do NOT just "try a different selector" — also make the code tolerate missing '
            f"elements so one bad row doesn't abort the whole script.{api_hint}"
        ),
        timestamp=time.time(),
    ))
    return GuardVerdict(action="pass")


# ─── Runner ────────────────────────────────────────────────────────────────


def run_post_step_guards(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run post-step guards in sequence. Engine threads ctx with:
    scratchpad, iteration, output_hashes, role, validated_output.

    Returns dict {action, final_output?}.
    """
    scratchpad = ctx["scratchpad"]
    iteration = ctx["iteration"]
    output_hashes = ctx["output_hashes"]
    role = ctx["role"]
    validated_output = ctx["validated_output"]

    v1 = dead_end_guard(output_hashes, role, validated_output, scratchpad)
    if v1.action != "pass":
        return {"action": v1.action, "final_output": v1.final_output}

    # The remaining guards push nudges but never break the loop.
    anti_stuck_guard(scratchpad, iteration)
    repeat_error_guard(scratchpad, iteration)
    module_not_found_autoinstall_guard(scratchpad, iteration)
    nonetype_attribute_hint_guard(scratchpad, iteration)
    return {"action": "pass"}
