"""Scorers for the eval suite (improvement #3).

Each check in a golden case names a scorer ``type`` plus its arguments. A
scorer takes ``(result_dict, check_dict)`` and returns ``(passed, detail)``.

Scorers are intentionally *tolerant*: local-model output is nondeterministic,
so we assert properties ("the answer contains 4", "a write_file tool ran",
"the action was a real verb", "no phantom-verb leaked") rather than exact
strings. A case passes only if ALL its checks pass.

Pure stdlib (``re`` only).
"""
from __future__ import annotations

import re
from typing import Any, Callable


def _final(result: dict) -> str:
    return (result.get("final") or "")


def _final_lower(result: dict) -> str:
    return _final(result).lower()


def check_contains(result: dict, check: dict) -> tuple[bool, str]:
    """Final answer contains the (case-insensitive) substring ``value``."""
    needle = str(check["value"])
    ok = needle.lower() in _final_lower(result)
    return ok, f"contains {needle!r}: {ok}"


def check_contains_all(result: dict, check: dict) -> tuple[bool, str]:
    final = _final_lower(result)
    needles = [str(v) for v in check["values"]]
    missing = [n for n in needles if n.lower() not in final]
    return (not missing), f"missing {missing}" if missing else "all present"


def check_contains_any(result: dict, check: dict) -> tuple[bool, str]:
    final = _final_lower(result)
    needles = [str(v) for v in check["values"]]
    hit = [n for n in needles if n.lower() in final]
    return bool(hit), f"matched {hit}" if hit else f"none of {needles}"


def check_not_contains(result: dict, check: dict) -> tuple[bool, str]:
    needle = str(check["value"])
    ok = needle.lower() not in _final_lower(result)
    return ok, f"absent {needle!r}: {ok}"


def check_regex(result: dict, check: dict) -> tuple[bool, str]:
    pat = str(check["pattern"])
    flags = re.IGNORECASE if check.get("ignorecase", True) else 0
    ok = re.search(pat, _final(result), flags) is not None
    return ok, f"regex {pat!r}: {ok}"


def check_min_length(result: dict, check: dict) -> tuple[bool, str]:
    n = int(check["value"])
    actual = len(_final(result))
    return actual >= n, f"len {actual} >= {n}"


def check_action_in_set(result: dict, check: dict) -> tuple[bool, str]:
    """Every decision action observed must be a real registered verb.

    Directly exercises improvement #1: under schema-constrained decoding the
    orchestrator can't emit a phantom verb, so this should always hold.
    """
    from agentcommander.engine.actions import ALL_ACTIONS
    allowed = set(check.get("values") or ALL_ACTIONS)
    actions = result.get("actions") or []
    bad = [a for a in actions if a not in allowed]
    return (not bad), f"phantom actions {bad}" if bad else f"all {len(actions)} actions valid"


def check_tool_fired(result: dict, check: dict) -> tuple[bool, str]:
    """A tool with name ``value`` ran (optionally requiring ok=True)."""
    name = str(check["value"])
    require_ok = check.get("require_ok", True)
    hits = [t for t in (result.get("tools") or []) if t.get("tool") == name]
    if not hits:
        return False, f"tool {name!r} never fired"
    if require_ok and not any(t.get("ok") for t in hits):
        return False, f"tool {name!r} fired but never succeeded"
    return True, f"tool {name!r} fired ({len(hits)}x)"


def check_role_fired(result: dict, check: dict) -> tuple[bool, str]:
    name = str(check["value"])
    ok = name in (result.get("roles") or [])
    return ok, f"role {name!r} fired: {ok}"


def check_max_iterations(result: dict, check: dict) -> tuple[bool, str]:
    n = int(check["value"])
    actual = int(result.get("iterations") or 0)
    return actual <= n, f"iterations {actual} <= {n}"


def check_no_error(result: dict, check: dict) -> tuple[bool, str]:
    err = result.get("error")
    timed_out = result.get("timed_out")
    if timed_out:
        return False, "timed out"
    return (err is None), f"error: {err}" if err else "no error"


_SCORERS: dict[str, Callable[[dict, dict], tuple[bool, str]]] = {
    "contains": check_contains,
    "contains_all": check_contains_all,
    "contains_any": check_contains_any,
    "not_contains": check_not_contains,
    "regex": check_regex,
    "min_length": check_min_length,
    "action_in_set": check_action_in_set,
    "tool_fired": check_tool_fired,
    "role_fired": check_role_fired,
    "max_iterations": check_max_iterations,
    "no_error": check_no_error,
}


def known_scorer_types() -> list[str]:
    return sorted(_SCORERS)


def score_case(result: dict, checks: list[dict]) -> tuple[bool, list[dict]]:
    """Run every check against the result. Returns (all_passed, details)."""
    details: list[dict] = []
    all_passed = True
    for check in checks:
        ctype = check.get("type")
        scorer = _SCORERS.get(ctype)
        if scorer is None:
            details.append({"type": ctype, "passed": False,
                            "detail": f"unknown scorer type {ctype!r}"})
            all_passed = False
            continue
        try:
            passed, detail = scorer(result, check)
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"scorer raised {type(exc).__name__}: {exc}"
        details.append({"type": ctype, "passed": passed, "detail": detail})
        all_passed = all_passed and passed
    return all_passed, details
