"""env tool — read environment variables, with secret-redaction.

Models often want to know what's in PATH, what HOME points at, or
whether a particular feature flag is set. The shell ``execute`` tool
covers this but at the cost of a permission prompt and the risk of the
model accidentally printing a secret to the scratchpad (where compaction
later might persist it).

This tool does the right thing automatically:

  - ``read``: returns the value of a single env var. If the name matches
    a secret-pattern (API_KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL,
    PRIVATE_KEY, etc.), the response is REDACTED — first/last 3 chars
    plus a length count — never the full value.
  - ``list``: returns the names of every env var (no values), so the
    model can discover what's available without seeing secrets.
  - ``list_filtered``: returns name + value for non-secret vars only.

There is no ``write`` verb. Process env-var mutations bleed into every
subsequent subprocess call (``execute``, ``git``, ``http_request``, ...);
that's a footgun the orchestrator shouldn't be able to trigger by
itself. If the user wants to set an env var for a specific run, they
can do it from the shell wrapper around ``ac``.
"""
from __future__ import annotations

import os
import re
from typing import Any

from agentcommander.tools.dispatcher import register
from agentcommander.tools.types import ToolContext, ToolDescriptor, ToolResult


# Patterns that match common secret-bearing env var names. Case-insensitive.
# Conservative: false positives (NEW_RELIC_LICENSE_KEY) get redacted; that's
# the right default. The user can still see them via the shell directly.
_SECRET_PATTERNS = [
    re.compile(r"(^|_)(API[_-]?KEY|APIKEY)(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)TOKEN(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)SECRET(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)PASSWORD(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)PASSWD(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)PASS(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)CREDENTIAL(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)PRIVATE[_-]?KEY(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)CLIENT[_-]?SECRET(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)ACCESS[_-]?KEY(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)AUTH[_-]?TOKEN(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)BEARER(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)SESSION(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)SIGNING[_-]?KEY(_|$)", re.IGNORECASE),
    re.compile(r"^OPENAI_", re.IGNORECASE),
    re.compile(r"^ANTHROPIC_", re.IGNORECASE),
    re.compile(r"^GITHUB[_-]?TOKEN", re.IGNORECASE),
    re.compile(r"^AWS_", re.IGNORECASE),
    re.compile(r"^GOOGLE_API", re.IGNORECASE),
]


def _is_secret(name: str) -> bool:
    """True if ``name`` matches any secret-pattern."""
    return any(p.search(name) for p in _SECRET_PATTERNS)


def _redact(value: str) -> str:
    """Mask ``value`` to its first/last 3 chars + length.

    Short values (≤ 6 chars) get fully redacted — masking the first 3 of
    a 4-char value still leaks 1 char. Empty strings stay empty (a
    legitimate "this var is set but empty" signal).
    """
    if not value:
        return ""
    if len(value) <= 6:
        return f"<redacted; {len(value)} chars>"
    return f"{value[:3]}…{value[-3:]} <redacted; {len(value)} chars total>"


def _env_read(name: str) -> ToolResult:
    if name not in os.environ:
        return ToolResult(
            ok=False,
            error=f"env var {name!r} is not set",
            data={"name": name, "set": False},
        )
    value = os.environ[name]
    if _is_secret(name):
        masked = _redact(value)
        return ToolResult(
            ok=True,
            output=masked,
            data={"name": name, "set": True, "redacted": True,
                  "length": len(value)},
        )
    return ToolResult(
        ok=True,
        output=value,
        data={"name": name, "set": True, "redacted": False},
    )


def _env_list() -> ToolResult:
    """Names only — no values, even non-secret. Models that just want to
    discover what's available can use this without any redaction question."""
    names = sorted(os.environ.keys())
    return ToolResult(
        ok=True,
        output="\n".join(names),
        data={"count": len(names), "names": names},
    )


def _env_list_filtered() -> ToolResult:
    """Name=value for non-secret vars; secrets show up by name only with a
    ``<redacted>`` placeholder so the model can tell they exist without
    seeing them."""
    lines: list[str] = []
    safe: dict[str, str] = {}
    redacted: list[str] = []
    for name in sorted(os.environ.keys()):
        value = os.environ[name]
        if _is_secret(name):
            lines.append(f"{name}=<redacted>")
            redacted.append(name)
        else:
            lines.append(f"{name}={value}")
            safe[name] = value
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        data={
            "safe_count": len(safe),
            "redacted_count": len(redacted),
            "redacted_names": redacted,
        },
    )


def _env(payload: dict[str, Any], ctx: ToolContext) -> ToolResult:
    verb = payload.get("verb") or payload.get("input") or "list"
    if not isinstance(verb, str):
        return ToolResult(ok=False, error="verb must be a string")
    verb = verb.strip().lower()

    if verb == "read":
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(ok=False,
                              error="`name` is required for verb=read")
        return _env_read(name)

    if verb == "list":
        return _env_list()

    if verb == "list_filtered":
        return _env_list_filtered()

    return ToolResult(
        ok=False,
        error=(f"unsupported verb {verb!r}. Available: read, list, "
               f"list_filtered"),
    )


register(ToolDescriptor(
    name="env",
    description=(
        "Read process environment variables with automatic secret redaction. "
        "Verbs: `read` (one var, redacted if name matches secret pattern), "
        "`list` (names only — no values), `list_filtered` (name=value for "
        "non-secrets, name=<redacted> for secrets). No write verb — env "
        "mutations would leak into all subsequent subprocess calls."
    ),
    privileged=False,
    input_schema={
        "type": "object",
        "properties": {
            "verb": {"type": "string",
                     "enum": ["read", "list", "list_filtered"]},
            "name": {"type": "string"},
        },
    },
    handler=_env,
))
