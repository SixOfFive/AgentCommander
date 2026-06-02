"""Weak-model recovery helpers — extracted from engine.py (#5).

Pure, side-effect-free functions the engine uses to recover when a local model
emits tool intent as plain text, declines a live-data fetch, or needs its
decision mapped to a tool payload. Kept out of ``engine.py`` so the pipeline
loop stays readable; nothing here touches ``PipelineRun`` state.

  - ``decision_to_payload`` — OrchestratorDecision → tool dispatcher payload.
  - ``detect_tool_syntax_intent`` — spot ``<verb> <arg>`` leaked as text.
  - ``clean_textual_arg`` — strip quotes/brackets/punctuation off a textual arg.
  - ``payload_from_textual_call`` — map a detected ``<verb> <arg>`` to a payload.
  - ``infer_live_data_url`` / ``LIVE_DATA_PATTERNS_FORCED`` — deterministic
    weather/time/news URL inference for the forced-fetch path.

Pure stdlib.
"""
from __future__ import annotations

import re
from typing import Any

from agentcommander.types import OrchestratorDecision


# ─── Live-data URL inference (deterministic forced-fetch) ───────────────────

LIVE_DATA_PATTERNS_FORCED: tuple[tuple[str, "re.Pattern[str]", Any], ...] = (
    # Weather: "weather in <X>", "forecast for <X>", "temperature in <X>".
    # We extract the location after the preposition and stop at the first
    # punctuation / "today" / "now" / end-of-string.
    (
        "weather",
        re.compile(
            r"\b(?:weather|forecast|temperature)\s+"
            r"(?:in|for|at|of)\s+"
            r"([a-zA-Z][a-zA-Z\s\-']*?)"
            r"(?:[,\.\?!;]|\s+today|\s+now|\s+right now|\s*$)",
            re.IGNORECASE,
        ),
        lambda m: f"https://wttr.in/{m.group(1).strip().replace(' ', '+')}?format=3",
    ),
    # Bare "weather" with no location — wttr.in falls back to the caller's
    # IP-resolved location.
    (
        "weather-bare",
        re.compile(
            r"\b(?:weather|forecast|temperature)\b"
            r"(?!\s+(?:in|for|at|of))",
            re.IGNORECASE,
        ),
        lambda m: "https://wttr.in/?format=3",
    ),
    # Current time in a location: "time in <X>", "current time in <X>".
    (
        "time",
        re.compile(
            r"\b(?:current\s+time|time)\s+(?:in|for|at)\s+"
            r"([a-zA-Z][a-zA-Z\s/\-']*?)"
            r"(?:[,\.\?!;]|\s*$)",
            re.IGNORECASE,
        ),
        lambda m: (
            f"https://worldtimeapi.org/api/timezone/"
            f"{m.group(1).strip().replace(' ', '_')}"
        ),
    ),
    # Bare "what time is it" — falls back to the IP endpoint.
    (
        "time-bare",
        re.compile(
            r"\b(?:what\s+time(?:\s+is\s+it)?|current\s+time)\b"
            r"(?!\s+(?:in|for|at))",
            re.IGNORECASE,
        ),
        lambda m: "https://worldtimeapi.org/api/ip",
    ),
    # News: "today's news", "latest news", "headlines", "breaking news"
    # → Google News top-stories RSS. Generic and reliable.
    (
        "news",
        re.compile(
            r"\b(?:today'?s?\s+news|latest\s+news|news\s+headlines|"
            r"breaking\s+news|top\s+(?:stories|headlines))\b",
            re.IGNORECASE,
        ),
        lambda m: "https://news.google.com/rss",
    ),
)


def infer_live_data_url(user_message: str) -> str | None:
    """Pattern-match a live-data question to a concrete URL, else ``None``.

    Used by the deterministic forced-fetch path: when the orchestrator has
    refused to fetch despite repeated nudges, the engine takes over and runs
    the inferred URL itself. The table is deliberately small (weather, time,
    news) and the patterns specific — stock/crypto and sports scores aren't
    included because the URL space is too varied to guess a sane default.
    """
    if not user_message:
        return None
    for _name, rx, builder in LIVE_DATA_PATTERNS_FORCED:
        m = rx.search(user_message)
        if m is None:
            continue
        try:
            url = builder(m)
        except Exception:  # noqa: BLE001
            continue
        if url and isinstance(url, str):
            return url
    return None


# ─── Scratchpad-leak detection ──────────────────────────────────────────────


def is_scratchpad_leak(text: str) -> bool:
    """True when ``text`` is a verbatim copy of the engine's own scratchpad
    scaffolding rather than a real model reply.

    Round-22 catch: after a successful tool action, the orchestrator (a weak
    model under context pressure) sometimes emits the engine's own
    ``successfully completed:`` wrapper, a role-prompt phrase, or a prior
    turn's fake multi-test summary as its ``done.input``. Detecting these
    mechanical signatures lets the engine route to the chat fallback for a
    fresh attempt instead of shipping the scaffolding as the answer.
    """
    if not text:
        return False
    norm = text.lstrip()
    norm_lower = norm.lower()
    # 1. Engine's tool-success wrapper — never a real reply.
    if norm_lower.startswith("successfully completed:"):
        return True
    # 2. Role-prompt scaffolding regurgitation.
    if norm_lower.startswith("summarize what was done"):
        return True
    # 3. Multi-test-summary hallucination loop: 3+ "TEST NNN:" references means
    #    the reply is echoing prior turns rather than answering this one.
    if len(re.findall(r"\bTEST\s+\d{2,3}\b", norm)) >= 3:
        return True
    return False


# ─── Tool-syntax-as-text detection + payload building ───────────────────────


def detect_tool_syntax_intent(text: str) -> tuple[str, str] | None:
    """Detect ``<verb> [<arg>]`` in the LAST non-empty line of ``text``.

    Shared between chat-fallback's stream output and the orchestrator's
    ``done.input`` branch — both surfaces leak tool syntax under the same
    conditions, and both should recover the same way. Accepts synonyms
    (``ls``, ``cat``, ``curl``, ``rm``, …) via the shared ``TOOL_VERB_SYNONYMS``
    map. Returns ``(verb, arg)`` (arg may be ``""``) or ``None``. The 300-char
    line cap prevents false-positives on prose that ends mentioning a tool.
    """
    if not text:
        return None
    lines = [l for l in (ln.strip() for ln in text.split("\n")) if l]
    if not lines:
        return None
    last = lines[-1]
    if len(last) > 300:
        return None
    from agentcommander.engine.guards.decision_guards import TOOL_VERB_SYNONYMS
    canonical = (
        "read_file", "write_file", "list_dir", "delete_file", "execute",
        "fetch", "http_request", "git", "env", "browser",
        "start_process", "kill_process", "check_process",
    )
    all_verbs = sorted(set(canonical) | set(TOOL_VERB_SYNONYMS.keys()),
                       key=len, reverse=True)
    verb_alt = "|".join(re.escape(v) for v in all_verbs)
    m_arg = re.match(
        r"^(" + verb_alt + r")\s+(?!\{)([^\s].*?)\s*$",
        last, re.IGNORECASE,
    )
    if m_arg:
        verb = m_arg.group(1).lower()
        verb = TOOL_VERB_SYNONYMS.get(verb, verb)
        return verb, m_arg.group(2).strip()
    m_only = re.match(r"^(" + verb_alt + r")\s*$", last, re.IGNORECASE)
    if m_only:
        verb = m_only.group(1).lower()
        verb = TOOL_VERB_SYNONYMS.get(verb, verb)
        return verb, ""
    return None


def clean_textual_arg(verb: str, raw: str) -> str:
    """Strip the noise the model wraps around args in chat-style emissions.

    Models routinely produce ``fetch "https://example.com".`` (quoted + period)
    or ``read_file `./foo.py` `` (backticks). Removes matched surrounding pairs
    (``"" '' `` `` <> () []``) and trailing sentence punctuation; preserves
    internal spaces (a path can contain them).
    """
    s = raw.strip()
    pairs = (("\"", "\""), ("'", "'"), ("`", "`"),
             ("<", ">"), ("(", ")"), ("[", "]"))
    changed = True
    while changed:
        changed = False
        for opener, closer in pairs:
            if len(s) >= 2 and s.startswith(opener) and s.endswith(closer):
                s = s[len(opener):-len(closer)].strip()
                changed = True
    while s and s[-1] in ".,;:!?)]>":
        s = s[:-1].rstrip()
    return s


def payload_from_textual_call(verb: str, arg: str) -> dict[str, Any] | None:
    """Best-effort: map ``<verb> [<arg>]`` to a real tool payload, or ``None``
    when the verb isn't safely auto-executable / a required arg is missing.

    Conservative — better to fall back to the apology than ship a malformed
    payload. Unambiguous-default verbs (``env``, ``list_dir``) fill in missing
    args sensibly.
    """
    cleaned = clean_textual_arg(verb, arg) if arg else ""
    if verb == "fetch":
        return {"url": cleaned} if cleaned else None
    if verb == "browser":
        return {"url": cleaned} if cleaned else None
    if verb == "http_request":
        return {"url": cleaned, "method": "GET"} if cleaned else None
    if verb == "read_file":
        return {"path": cleaned} if cleaned else None
    if verb == "list_dir":
        return {"path": cleaned or "."}
    if verb == "check_process":
        # check_process schema requires `id`, not `name` (round-45).
        return {"id": cleaned} if cleaned else None
    if verb == "env":
        # `env` alone → list; with an arg → READ that var (round-45).
        if cleaned:
            return {"verb": "read", "name": cleaned}
        return {"verb": "list"}
    return None


# ─── Decision → tool payload ────────────────────────────────────────────────


def decision_to_payload(decision: OrchestratorDecision, exec_code: str,
                        exec_language: str) -> dict[str, Any]:
    """Map an OrchestratorDecision to the tool dispatcher's payload shape.

    Always omits None-valued optional fields — the dispatcher's
    ``_validate_payload`` rejects ``"method": None`` for an optional string
    field. Every tool the orchestrator can dispatch is routed explicitly;
    the catch-all ``{}`` is reached only for verbs that take no payload.
    """
    a = decision.action
    if a in ("read_file", "list_dir", "delete_file"):
        return {"path": decision.path or decision.input}
    if a == "write_file":
        return {"path": decision.path or decision.input,
                "content": decision.content or ""}
    if a == "execute":
        return {"language": exec_language, "code": exec_code}
    if a == "fetch":
        payload: dict[str, Any] = {"url": decision.url or decision.input}
        if decision.method:
            payload["method"] = decision.method
        if decision.headers:
            payload["headers"] = decision.headers
        if decision.body is not None:
            payload["body"] = decision.body
        return payload
    if a == "http_request":
        payload = {"url": decision.url or decision.input}
        if decision.method:
            payload["method"] = decision.method
        if decision.headers:
            payload["headers"] = decision.headers
        if decision.body is not None:
            payload["body"] = decision.body
        return payload
    if a == "git":
        # Read-only git: `verb` required; optional `pattern`. message/files
        # are dropped (mutations go through `execute`).
        payload = {"verb": decision.command or decision.input or "status"}
        if decision.pattern:
            payload["pattern"] = decision.pattern
        return payload
    if a == "env":
        payload = {}
        verb_src = decision.command or decision.input or ""
        if verb_src and verb_src in ("read", "list", "list_filtered"):
            payload["verb"] = verb_src
        if decision.path:  # repurpose `path` for the var name slot
            payload["name"] = decision.path
        return payload
    if a == "browser":
        return {"url": decision.url or decision.input}
    if a == "start_process":
        return {"command": decision.command or decision.input}
    if a in ("kill_process", "check_process"):
        return {"id": decision.input}
    if a in ("vault_search", "vault_read"):
        # Both take their argument in `input` (query / note name); accept
        # `path` as a fallback for a note path.
        return {"input": decision.input or decision.path or ""}
    return {}
