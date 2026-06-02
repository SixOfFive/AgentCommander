"""Preventive guards — broad-pattern catches for common LLM failure modes.

These are layered on top of the existing 9 guard families. Each one targets
a recurring failure pattern observed across rounds (or one I'd expect from
similar LLM behaviour in the wild) and is implemented as a regex / simple-
pattern check so the cost is microseconds per call.

Categories (mirrors the existing family split — guards are wired into the
matching runner):

  decision-tier:
    - zero_width_unicode_guard            silent strip of ZWSP / NBSP / BOM
    - code_fence_in_arg_guard             strip ```lang fences from code/input/content
    - markdown_link_extract_guard         pull URL out of [text](url) on fetch
    - url_scheme_typo_guard               fix htttps:// / htps:// / http:/
    - placeholder_url_guard               block fetch with example.com / YOUR_API_KEY / <url>
    - empty_role_input_guard              block role dispatch with empty input
    - protocol_relative_url_guard         //host/path → https://host/path
    - tracking_param_strip_guard          strip utm_* params from fetch URLs

  done-tier:
    - ai_disclaimer_guard                 "As an AI, I cannot…" reflexes
    - training_cutoff_leak_guard          "my knowledge cutoff…" deflections
    - unfilled_template_guard             <TODO> / XXX / [INSERT] / {your-…}
    - fake_citation_guard                 [1][2][3]… without any fetch
    - stale_year_guard                    claims a year >1y before today
    - hedge_only_guard                    "It depends…" with no concrete content
    - unclosed_codefence_guard            silent close-up odd ``` count
    - over_apologetic_guard               3+ apologies in one done.input
    - dangling_promise_guard              "I will research / let me look that up"

  execute-tier:
    - base64_pipe_shell_guard             echo X | base64 -d | sh / bash / python
    - homoglyph_guard                     Cyrillic letters in Python identifiers
    - shell_history_subst_guard           !! / !$ in non-interactive shell
    - eval_remote_string_guard            eval(requests.get(...).text)

All guards default to silent rewrites where possible, fall back to nudges
that re-orchestrate, and never break the loop hard. False positives are
biased low — patterns are tightened to catch the specific failure shape
(e.g. ``markdown_link_extract_guard`` only fires when the URL field IS
the markdown link, not when prose merely contains one).
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from agentcommander.engine.guards.types import GuardVerdict, push_system_nudge
from agentcommander.types import OrchestratorDecision, ScratchpadEntry


# ─── Shared regexes ────────────────────────────────────────────────────────

# Zero-width / invisible characters often introduced by copy-paste from
# rendered LLM output. Stripping them is silent — there's no legitimate
# reason for any of these to appear in tool args, code, or URLs.
_ZW_RX = re.compile(
    "[​‌‍⁠﻿]"   # ZWSP, ZWNJ, ZWJ, WJ, BOM
    "| "                             # NBSP — common in pasted code
)

# Markdown code-fence wrappers. Match ``` followed by optional language tag,
# then content, then a trailing ```. Permissive about whitespace.
_FENCE_RX = re.compile(
    r"\A\s*```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\n```\s*\Z",
    re.DOTALL,
)

# Markdown link form: [label](https://target). Used to pull URLs out of
# decision.url when the model emits a markdown-formatted link there.
_MD_LINK_RX = re.compile(r"\A\s*\[[^\]]*\]\(([^)\s]+)\)\s*\Z")

# Common URL-scheme typos. Each is a regex paired with the canonical scheme.
_URL_SCHEME_TYPOS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^htttps?://", re.IGNORECASE), "https://"),
    (re.compile(r"^htps://", re.IGNORECASE), "https://"),
    (re.compile(r"^htttp://", re.IGNORECASE), "http://"),
    (re.compile(r"^htp://", re.IGNORECASE), "http://"),
    (re.compile(r"^https?:/(?!/)", re.IGNORECASE), None),  # http:/foo → http://foo
    (re.compile(r"^https?(?=//)", re.IGNORECASE), None),   # https//foo → https://foo
)

# URL placeholder patterns. Anything matching these in a fetch / http_request
# URL is the model emitting a template, not a real target.
_URL_PLACEHOLDERS = (
    "your_api_key", "your-api-key", "your_token", "your-token",
    "<api_key>", "<token>", "<url>", "<your-",
    "example.com/api", "api.example.com",
    "{api_key}", "{token}", "{url}",
    "[your-", "[api-key]", "[token]",
    "insert_", "replace_with_",
    "xxx.com",
)

# Role names that must receive non-empty input when dispatched. Excludes
# roles invoked without an explicit content argument (like `done`).
_INPUT_REQUIRED_ROLES = frozenset({
    "translator", "summarizer", "critic", "reviewer", "tester",
    "debugger", "researcher", "refactorer", "architect", "planner",
    "coder", "data_analyst",
})


# ─── Decision-tier guards ──────────────────────────────────────────────────


def zero_width_unicode_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Silently strip zero-width / NBSP / BOM characters from any string field.

    These slip in via copy-paste from rendered model output and break shell
    commands, URL parses, file paths. Stripping them never harms a valid
    payload — they have no semantic meaning in any of the tools we dispatch.
    """
    fields = ("input", "url", "path", "content", "command",
              "language", "host", "body", "files")
    for f in fields:
        val = getattr(decision, f, None)
        if isinstance(val, str) and _ZW_RX.search(val):
            setattr(decision, f, _ZW_RX.sub("", val))
    return GuardVerdict(action="pass")


def code_fence_in_arg_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Silently unwrap ```language … ``` fences from code-bearing fields.

    Models commonly wrap the code arg in markdown code fences as if writing
    a chat reply (especially after the orchestrator prompt mentions code).
    The execute / write_file tools then either fail to parse or ship a
    file with literal ``` lines at the top. This guard is a structural
    rewrite — only fires when the ENTIRE field is a single fenced block.
    """
    # Different actions store the code in different fields; map each.
    # OrchestratorDecision does not have a separate `code` field — code
    # rides on `input` for execute and `content` for write_file.
    fence_fields = ("input", "content")
    target_actions = {"execute", "write_file", "code"}
    if (decision.action or "").lower() not in target_actions:
        return GuardVerdict(action="pass")
    for f in fence_fields:
        val = getattr(decision, f, None)
        if not isinstance(val, str):
            continue
        m = _FENCE_RX.match(val)
        if m is not None:
            setattr(decision, f, m.group("body"))
    return GuardVerdict(action="pass")


def markdown_link_extract_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """``decision.url = "[Google](https://google.com)"`` → ``"https://google.com"``.

    Silent rewrite. Only triggers when the entire url field IS the markdown
    link — prose URLs aren't touched.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str):
        return GuardVerdict(action="pass")
    m = _MD_LINK_RX.match(url)
    if m is not None:
        decision.url = m.group(1)
    return GuardVerdict(action="pass")


def url_scheme_typo_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Fix the common scheme typos: ``htttps://`` / ``htps://`` / ``http:/host``.

    Silent rewrite — unambiguous fixes the user/model never wanted differently.
    Affects ``decision.url``; leaves the rest of the URL intact.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str) or not url:
        return GuardVerdict(action="pass")
    # First handle htttps / htps style typos with a real replacement.
    for rx, repl in _URL_SCHEME_TYPOS:
        if repl is None:
            continue
        if rx.match(url):
            decision.url = rx.sub(repl, url, count=1)
            return GuardVerdict(action="pass")
    # http:/foo (single slash) and https//foo (no colon) — fix structurally.
    if re.match(r"^https?:/(?!/)", url, re.IGNORECASE):
        decision.url = re.sub(r"^(https?):/", r"\1://", url, count=1, flags=re.IGNORECASE)
    elif re.match(r"^https?//", url, re.IGNORECASE):
        decision.url = re.sub(r"^(https?)//", r"\1://", url, count=1, flags=re.IGNORECASE)
    return GuardVerdict(action="pass")


def protocol_relative_url_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """``//host.com/path`` → ``https://host.com/path``.

    Protocol-relative URLs are valid in HTML but fail when handed to
    urllib.request directly (no scheme). Default to https — http is rare
    on modern targets and downgrading is worse than upgrading.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if isinstance(url, str) and url.startswith("//") and not url.startswith("///"):
        decision.url = "https:" + url
    return GuardVerdict(action="pass")


def placeholder_url_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> GuardVerdict:
    """Block fetch / http_request when the URL is obviously a placeholder.

    Catches: ``YOUR_API_KEY``, ``<url>``, ``api.example.com/v1/...``,
    ``{token}``, ``[your-id]``, ``insert_xxx``. The model is meant to fill
    them with real data before dispatching; if it didn't, the call would
    either hit example.com (returns IANA's reserved-domain page) or 404,
    burning an iteration. Better to nudge.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str) or not url:
        return GuardVerdict(action="pass")
    low = url.lower()
    if not any(p in low for p in _URL_PLACEHOLDERS):
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "placeholder_url",
        f'BLOCKED: URL contains a placeholder ("{url[:80]}"). Replace it with '
        f"the real value before dispatching. If you do not have the real "
        f"value, ask the user — do NOT fetch a template URL.",
    )
    return GuardVerdict(action="continue")


def empty_role_input_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> GuardVerdict:
    """Block role dispatches whose input is empty / whitespace-only.

    Roles like translator, summarizer, critic depend on the input field
    being the actual content to operate on. An empty dispatch costs one
    full LLM call and produces no useful output. The orchestrator gets
    a nudge naming the role and listing what input it expects.
    """
    role = (decision.action or "").lower()
    if role not in _INPUT_REQUIRED_ROLES:
        return GuardVerdict(action="pass")
    raw = decision.input
    txt = raw if isinstance(raw, str) else ""
    if txt.strip():
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "empty_role_input",
        f"BLOCKED: dispatched `{role}` with empty input. {role} operates "
        f"on the content in `input` — paste the actual text/code/question "
        f"to process before dispatching. If the prior step's output is what "
        f"you mean to feed in, copy it into `input` explicitly.",
    )
    return GuardVerdict(action="continue")


def tracking_param_strip_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Drop ``utm_*`` / ``fbclid`` / ``gclid`` from fetch URLs.

    These don't change response content but blow up the URL length, which
    inflates duplicate-call detection (different tracking params look like
    different URLs to ``repeated_tool_call_guard``). Silent strip is safe
    — every responsible server ignores them.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str) or "?" not in url:
        return GuardVerdict(action="pass")
    base, _, query = url.partition("?")
    if not query:
        return GuardVerdict(action="pass")
    parts = query.split("&")
    keep: list[str] = []
    for part in parts:
        key = part.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in ("fbclid", "gclid", "msclkid", "mc_eid", "mc_cid"):
            continue
        keep.append(part)
    new_q = "&".join(keep)
    if new_q == query:
        return GuardVerdict(action="pass")
    decision.url = base + ("?" + new_q if new_q else "")
    return GuardVerdict(action="pass")


# ─── Done-tier guards ──────────────────────────────────────────────────────


_AI_DISCLAIMER_RX = re.compile(
    r"\b(as an ai( language model)?|i'?m (just )?an ai|"
    r"i (cannot|can'?t|am unable to|don'?t have the ability to)|"
    r"i (lack|do not have) (the ability|access) (to|for))\b",
    re.IGNORECASE,
)


def ai_disclaimer_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject reflexive AI-disclaimers on benign requests.

    Pattern: model emits "As an AI, I cannot fetch URLs" or "I'm just an AI
    and don't have access to the internet" when the engine has tools that
    DO have those abilities. The right behaviour is to call the tool, not
    decline. Nudge with the relevant capability hint.

    Does NOT fire when the request actually IS prohibited (financial advice,
    user-private-data exfil, etc.) — those legitimately get declined. We
    detect that via has_deliverable: if the orchestrator already worked on
    the task (any tool call this turn) and is now declining, it's the bad
    pattern. If no tool was attempted yet, also bad — nudge to try.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 20:
        return GuardVerdict(action="pass")
    if not _AI_DISCLAIMER_RX.search(text):
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "ai_disclaimer",
        "BLOCKED: done.input declines via 'As an AI…' / 'I cannot…'. "
        "AgentCommander has tools (fetch, http_request, execute, browse, "
        "list_dir, read_file, write_file, git, env, …) — use them instead "
        "of declining. If you need live data, dispatch fetch with the URL. "
        "If you need a file, dispatch read_file. If you genuinely cannot "
        "answer because the request is prohibited (financial advice, "
        "credentials), say WHY in one sentence — do NOT use a generic "
        "AI-incapacity disclaimer.",
    )
    return GuardVerdict(action="continue")


_CUTOFF_RX = re.compile(
    r"\b(my (knowledge|training) (cut-?off|cutoff|data) (is|ends|stops|was)|"
    r"as of my (last|knowledge) (update|cutoff)|"
    r"i was (trained|last updated) (on|in|until)|"
    r"my training (data )?(only )?(extends|goes) (to|up to|through))\b",
    re.IGNORECASE,
)


def training_cutoff_leak_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject 'my training data ends in 2024' deflections.

    The model uses these to wave away live-data questions. Engine has fetch.
    Nudge to dispatch fetch instead of citing the training cutoff.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 20:
        return GuardVerdict(action="pass")
    if not _CUTOFF_RX.search(text):
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "training_cutoff_leak",
        "BLOCKED: done.input cites the model's training cutoff. The user "
        "doesn't care when you were trained — they want a current answer. "
        "Dispatch fetch on a relevant API / news source / data feed and "
        "answer from the result. Do NOT emit done with a cutoff disclaimer.",
    )
    return GuardVerdict(action="continue")


_TEMPLATE_PLACEHOLDER_RX = re.compile(
    r"(<TODO>|<FIXME>|XXX{2,}|"
    r"\[INSERT[^\]]*\]|\[YOUR[^\]]*\]|\[REDACTED\]|\[PLACEHOLDER\]|"
    r"\{your[-_][^}]+\}|\{api[-_]?key\}|\{token\}|\{name\}|"
    r"<your[-_][^>]+>|<placeholder>|<example>)",
    re.IGNORECASE,
)


def unfilled_template_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Block done.input still containing template placeholders.

    The model copy-pasted a stub answer without filling in the variables.
    Catches: ``<TODO>``, ``XXX``, ``[INSERT NAME]``, ``[YOUR EMAIL]``,
    ``{your-token}``, ``{api_key}``, ``<placeholder>``.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    m = _TEMPLATE_PLACEHOLDER_RX.search(text)
    if m is None:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "unfilled_template",
        f'BLOCKED: done.input contains an unfilled template placeholder '
        f'("{m.group(0)[:50]}"). Replace it with the actual value. If you '
        f'don\'t have the value, fetch / read it before answering — never '
        f'ship template syntax to the user.',
    )
    return GuardVerdict(action="continue")


_FAKE_CITATION_RX = re.compile(r"\[\d{1,3}\]")


def fake_citation_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Block done.input with many ``[1][2][3]`` citations but no fetches.

    The model is hallucinating sources. Either it should actually fetch
    them, or strip the markers — never both ship.

    Threshold: 3+ distinct citation tokens AND zero successful fetches in
    this turn's scratchpad.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    matches = _FAKE_CITATION_RX.findall(text)
    if len(set(matches)) < 3:
        return GuardVerdict(action="pass")
    has_fetch = any(
        e.action in ("fetch", "http_request", "browse")
        and "Successfully" in (e.output or "")
        for e in scratchpad
    )
    if has_fetch:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "fake_citations",
        f"BLOCKED: done.input has {len(set(matches))} citation markers "
        f"({', '.join(sorted(set(matches))[:5])}) but you never fetched any "
        f"sources this turn. Either dispatch fetch on the real URLs and "
        f"cite from the response, or remove the markers — don't fabricate "
        f"citations. The user can tell.",
    )
    return GuardVerdict(action="continue")


_YEAR_CLAIM_RX = re.compile(
    r"\b(?:as of|in|year is|currently|today is)\s+(?:january|february|march|"
    r"april|may|june|july|august|september|october|november|december|"
    r"\d{1,2}/\d{1,2}/)?\s*(\d{4})\b",
    re.IGNORECASE,
)


def stale_year_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Block done.input that asserts a year >1y before the current date.

    Pattern: model says "as of 2024" or "the year is 2023" when system
    clock says 2026. Almost always indicates the model is answering from
    training data instead of fetching live. Nudge to fetch.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 10:
        return GuardVerdict(action="pass")
    m = _YEAR_CLAIM_RX.search(text)
    if m is None:
        return GuardVerdict(action="pass")
    try:
        claimed = int(m.group(1))
    except (ValueError, IndexError):
        return GuardVerdict(action="pass")
    current = _dt.datetime.now().year
    if claimed >= current - 1 or claimed < 2000:
        # Allow last year (e.g. early-Jan answer about events from Dec
        # last year) and reject obviously historical years (claimed < 2000
        # is almost certainly someone's birth year, not a stale-cutoff).
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "stale_year",
        f"BLOCKED: done.input asserts the year is {claimed}. The current "
        f"year is {current}. Either you're answering from stale training "
        f"data, or the user asked about a past year. If they asked about "
        f"now, dispatch fetch and answer from current data. If they asked "
        f"about {claimed}, name {claimed} explicitly so it's clear.",
    )
    return GuardVerdict(action="continue")


_HEDGE_PATTERNS = (
    "it depends",
    "without more information",
    "without additional context",
    "i would need to know more",
    "this is a complex topic",
    "there are many factors",
    "the answer varies",
    "it's hard to say",
)


def hedge_only_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input that is pure hedging, no concrete content.

    The model punts on the question by listing all the reasons it can't
    answer. Triggers when the entire response is short (≤ 250 chars) AND
    contains a hedge pattern AND has no concrete data (no numbers, no
    proper nouns past the first 30 chars). Does NOT fire on long, hedged
    answers — those usually contain real content even with hedge clauses.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    txt = text.strip()
    if len(txt) > 250 or len(txt) < 20:
        return GuardVerdict(action="pass")
    low = txt.lower()
    if not any(p in low for p in _HEDGE_PATTERNS):
        return GuardVerdict(action="pass")
    # Concrete-content sniff: digits, prose past the hedge phrase.
    has_digits = bool(re.search(r"\d", txt))
    if has_digits:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "hedge_only",
        "BLOCKED: done.input is mostly hedging ('it depends', 'without more "
        "info', etc.) with no concrete answer. If you need clarification, "
        "ask one specific question. If you can give a partial answer with "
        "stated assumptions, do that. Don't ship hedge-only responses — "
        "they're indistinguishable from refusing.",
    )
    return GuardVerdict(action="continue")


def unclosed_codefence_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Silently close an odd number of ``` fences in done.input.

    The model opened a code block and forgot to close it. Markdown
    renderers then format everything afterward as code. Pure rendering
    fix — append a closing fence on a new line.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    n = text.count("```")
    if n == 0 or n % 2 == 0:
        return GuardVerdict(action="pass")
    decision.input = text.rstrip() + "\n```"
    return GuardVerdict(action="pass")


_APOLOGY_RX = re.compile(r"\b(i'?m sorry|sorry|apologi[sz]e|my apologies|"
                          r"i regret|forgive me|i made a mistake)\b",
                          re.IGNORECASE)


def over_apologetic_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input with 3+ apologies in one reply.

    Indicates the model is in a self-flagellation loop instead of doing
    work. Ship the answer or report the genuine problem — don't pile
    on apologies.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 30:
        return GuardVerdict(action="pass")
    if len(_APOLOGY_RX.findall(text)) < 3:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "over_apologetic",
        "BLOCKED: done.input contains 3+ apologies. Cut the apologies and "
        "ship the actual answer (or, if there's a real failure, name it once "
        "concretely and stop).",
    )
    return GuardVerdict(action="continue")


_DANGLING_PROMISE_RX = re.compile(
    r"\b(i'?ll (research|look (this )?up|fetch|check|investigate|find out|get back)|"
    r"let me (research|look (this )?up|fetch|check|find|investigate)|"
    r"i will (research|look up|fetch|check|investigate|find out)|"
    r"give me a (moment|sec|second) (to|while i)|"
    r"hold on while i|one moment while i)\b",
    re.IGNORECASE,
)


def dangling_promise_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject 'I'll research that' / 'let me check' as a final answer.

    The model is treating the orchestrator like a chat partner. There IS
    no "later" — done is the final step. Either dispatch the action now
    or, if you don't know how, say so concretely.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 10:
        return GuardVerdict(action="pass")
    if not _DANGLING_PROMISE_RX.search(text):
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "dangling_promise",
        "BLOCKED: done.input promises future work ('I'll research…', "
        "'let me check…'). There is no 'later' — `done` ends the run. "
        "Dispatch the action you were going to do, OR answer with what "
        "you have, OR say plainly that the action would require a tool "
        "you can't reach. Don't promise and ship.",
    )
    return GuardVerdict(action="continue")


# ─── Execute-tier guards ───────────────────────────────────────────────────


_BASE64_PIPE_RX = re.compile(
    r"\bbase64\s+(-d|--decode|-D)\b.*\|\s*(sh|bash|zsh|python|node|perl|ruby)\b",
    re.IGNORECASE,
)
_DECODE_PIPE_RX = re.compile(
    r"\b(echo|printf|cat)\s+[\"']?[A-Za-z0-9+/=]{40,}[\"']?\s*\|\s*"
    r"(base64\s+(-d|--decode))?\s*\|?\s*(sh|bash|python|node)\b",
    re.IGNORECASE,
)


def base64_pipe_shell_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> tuple[str, GuardVerdict]:
    """Block ``base64 -d | sh`` and friends — obfuscated payload exec.

    The legitimate use case for piping base64 through bash is approximately
    zero in this engine. The illegitimate use case (LLM was tricked into
    decoding+executing an attacker's payload) is the only realistic one.
    Hard block with a nudge that names the pattern.
    """
    if _BASE64_PIPE_RX.search(code) or _DECODE_PIPE_RX.search(code):
        push_system_nudge(
            scratchpad, iteration, "base64_pipe_shell",
            "BLOCKED: execute code pipes a base64-decoded blob into a shell "
            "or interpreter (e.g. `... | base64 -d | sh`). This is the "
            "shape of an obfuscated-payload attack and is never the right "
            "answer for an engine task. If the user asked you to decode "
            "base64, run the decode and SHOW the result — never pipe it "
            "to an interpreter.",
        )
        return code, GuardVerdict(action="continue")
    return code, GuardVerdict(action="pass")


# Cyrillic letters that look like Latin in monospace fonts. If any appear
# in identifier-like positions in source code, the file will compile but
# fail at lookup time with confusing NameError. Block, ask for re-emit.
_CYRILLIC_HOMOGLYPHS = "аеорсхуАВЕНКМОРСТХ"


def homoglyph_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> tuple[str, GuardVerdict]:
    """Block source code containing Cyrillic homoglyphs.

    Pattern: model trained on multilingual data emits a Cyrillic 'а' (U+0430)
    where Latin 'a' (U+0061) was meant. Compiles; at runtime
    ``def myfunc():`` and the call site ``myfunс()`` (Cyrillic 's') are
    different identifiers, NameError. Block at execute time so the user
    isn't stuck debugging an invisible difference.
    """
    if not any(c in code for c in _CYRILLIC_HOMOGLYPHS):
        return code, GuardVerdict(action="pass")
    # Don't false-positive on legitimate Cyrillic content (e.g. docstrings
    # in Russian) — only flag when the homoglyphs appear OUTSIDE strings.
    # Coarse heuristic: strip quoted regions, then re-check.
    stripped = re.sub(r'"""[\s\S]*?"""', "", code)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', "", stripped)
    stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "", stripped)
    stripped = re.sub(r"#[^\n]*", "", stripped)
    if not any(c in stripped for c in _CYRILLIC_HOMOGLYPHS):
        return code, GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "homoglyph",
        "BLOCKED: execute code contains Cyrillic letters in identifier "
        "positions (visually identical to Latin in most fonts). This will "
        "compile but fail at lookup with confusing NameError. Re-emit the "
        "code with ASCII Latin letters only.",
    )
    return code, GuardVerdict(action="continue")


_HISTORY_SUBST_RX = re.compile(r"(^|[\s;|&])!![\s;|&\n$]|"
                                r"(^|[\s;|&])!\$")


def shell_history_subst_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Strip ``!!`` / ``!$`` history substitution from non-interactive shell.

    These only work in interactive bash; in a script they're literal text
    or syntax errors depending on hist-flag state. The model uses them
    when copying interactive examples. Silent strip is safe — the syntax
    has no meaningful side effect in a one-shot execution.
    """
    if not _HISTORY_SUBST_RX.search(code):
        return code, GuardVerdict(action="pass")
    cleaned = _HISTORY_SUBST_RX.sub(lambda m: m.group(0).replace("!!", "").replace("!$", ""),
                                     code)
    return cleaned, GuardVerdict(action="pass")


_EVAL_REMOTE_RX = re.compile(
    r"\b(eval|exec)\s*\(\s*("
    r"requests\.\w+\([^)]*\)\.(?:text|content)|"
    r"urllib\.request\.urlopen\([^)]*\)\.read\(\)|"
    r"urlopen\([^)]*\)\.read\(\)|"
    r"subprocess\.\w+\([^)]*curl[^)]*\)|"
    r")\s*\)",
    re.IGNORECASE,
)


def eval_remote_string_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> tuple[str, GuardVerdict]:
    """Block ``eval(requests.get(url).text)`` style remote-code execution.

    Catches the "fetch X then exec what comes back" pattern. The engine
    already has destructive-command, secrets-in-code, and SSRF defenses;
    this is the missing link for "fetched payload → eval/exec".
    """
    if not _EVAL_REMOTE_RX.search(code):
        return code, GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "eval_remote_string",
        "BLOCKED: execute code calls eval/exec on the response of a remote "
        "fetch. That ships unbounded remote code into the engine's process. "
        "Never do this. If you need to run downloaded code, write it to a "
        "file first, review it, then execute it as a separate step.",
    )
    return code, GuardVerdict(action="continue")


# ─── Round-50 batch — typography / secrets / interactive-shell artefacts ──


# Curly / smart quotes that come back when a model copies prose into a code
# field. Both single and double, both opening and closing. Map each to its
# straight-quote equivalent.
_SMART_QUOTE_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "「": '"', "」": '"', "『": '"', "』": '"',
})

# Em / en dashes that the model substitutes for `--` (autocorrect-style).
# Replacing them is safe inside code; in prose the user wouldn't have us
# rewrite — but tool args / code shouldn't contain dashes-as-typography.
_EM_DASH_RX = re.compile(r"[—–]")


def smart_quote_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Replace curly quotes with straight quotes in code-bearing fields.

    Curly quotes blow up shell parsing (``"hello"`` ≠ ``"hello"``), break
    JSON parses inside ``http_request.body``, and turn into mojibake when
    the receiving end isn't expecting them. Silent rewrite is safe — the
    only legitimate use of curly quotes in tool args is presentational
    text we don't dispatch.
    """
    if (decision.action or "").lower() not in (
        "execute", "write_file", "code", "http_request",
    ):
        return GuardVerdict(action="pass")
    for f in ("input", "content", "body"):
        v = getattr(decision, f, None)
        if isinstance(v, str) and any(c in v for c in "“”‘’„‟‚‛「」『』"):
            setattr(decision, f, v.translate(_SMART_QUOTE_MAP))
    return GuardVerdict(action="pass")


def em_dash_in_code_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Rewrite em/en dashes to ``--`` inside execute / write_file code.

    Catches ``git commit —m`` style autocorrect leakage. Only runs on code
    actions; prose finals (``done``) keep their typography.
    """
    if (decision.action or "").lower() not in ("execute", "write_file", "code"):
        return GuardVerdict(action="pass")
    for f in ("input", "content"):
        v = getattr(decision, f, None)
        if isinstance(v, str) and _EM_DASH_RX.search(v):
            setattr(decision, f, _EM_DASH_RX.sub("--", v))
    return GuardVerdict(action="pass")


# HTML entity decode — limited to entities a model likely emits when it's
# been over-trained on web content. We deliberately don't decode the full
# entity catalogue — that risks mangling legitimate prose (e.g. "&amp;"
# in a chat answer). Tool args / code are the target.
_HTML_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&apos;": "'", "&#39;": "'", "&#x27;": "'", "&#x2F;": "/",
    "&nbsp;": " ", "&#160;": " ",
}


def html_entity_decode_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Decode common HTML entities in code/url/path fields."""
    if (decision.action or "").lower() not in (
        "execute", "write_file", "fetch", "http_request",
    ):
        return GuardVerdict(action="pass")
    for f in ("input", "content", "url", "path"):
        v = getattr(decision, f, None)
        if not isinstance(v, str) or "&" not in v:
            continue
        out = v
        for ent, repl in _HTML_ENTITY_MAP.items():
            out = out.replace(ent, repl)
        if out != v:
            setattr(decision, f, out)
    return GuardVerdict(action="pass")


_URL_TRAILING_PUNCT_RX = re.compile(r"[.,;:!?)\]}>'\"]+$")


def url_trailing_punct_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> GuardVerdict:
    """Strip a trailing ``.`` / ``,`` / etc. that came from end-of-sentence.

    The model's training corpus has a lot of "Visit https://example.com." in
    prose. When it surfaces a URL into a tool arg, the punctuation often
    rides along.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str) or not url:
        return GuardVerdict(action="pass")
    cleaned = _URL_TRAILING_PUNCT_RX.sub("", url)
    if cleaned != url and cleaned:
        decision.url = cleaned
    return GuardVerdict(action="pass")


_URL_WHITESPACE_RX = re.compile(r"[\s\r\n\t]+")


def url_embedded_whitespace_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> GuardVerdict:
    """Strip embedded whitespace/newlines from fetch URLs.

    Models occasionally line-wrap URLs in their output. Silent strip if
    the URL becomes valid; otherwise nudge.
    """
    if (decision.action or "").lower() not in ("fetch", "http_request", "browse"):
        return GuardVerdict(action="pass")
    url = decision.url
    if not isinstance(url, str) or not _URL_WHITESPACE_RX.search(url):
        return GuardVerdict(action="pass")
    cleaned = _URL_WHITESPACE_RX.sub("", url)
    if cleaned and (cleaned.startswith(("http://", "https://"))
                     or cleaned.startswith("//")):
        decision.url = cleaned
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "url_whitespace",
        f'BLOCKED: URL contains embedded whitespace ("{url[:80]}"). '
        "Re-emit the URL on a single line with no spaces or newlines.",
    )
    return GuardVerdict(action="continue")


_URL_ENCODED_TRAVERSAL_RX = re.compile(
    r"%2[eE]%2[eE][/\\]|%2[eE]%2[eE]%2[fF5]",
)


def url_encoded_traversal_guard(
    decision: OrchestratorDecision,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> GuardVerdict:
    """Block URL-encoded path traversal: ``%2e%2e/`` / ``%2e%2e%2f``.

    The plain-text form ``../`` is caught upstream by the safety layer;
    this is the URL-encoded variant a model might emit when it's been
    trained on attack-pattern prose. Hard block.
    """
    targets = (decision.path, decision.url, decision.input)
    for t in targets:
        if isinstance(t, str) and _URL_ENCODED_TRAVERSAL_RX.search(t):
            push_system_nudge(
                scratchpad, iteration, "url_encoded_traversal",
                "BLOCKED: arg contains a URL-encoded path-traversal "
                "sequence (%2e%2e/ or similar). Path traversal is never "
                "a legitimate request shape — re-emit with a real path.",
            )
            return GuardVerdict(action="continue")
    return GuardVerdict(action="pass")


# ─── Done-tier — chatbot-bleed and presentation-quality guards ─────────────


_HERE_IS_PREFIX_RX = re.compile(
    r"\Ahere\s+(is|are|'?s)\s+", re.IGNORECASE,
)


def here_is_only_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject ``Here is the X you asked for.`` finals with no actual content.

    Triggers when:
      - done.input starts with "Here is/are/'s"
      - total length under 100 chars
      - no digits, no URLs, no code fences

    Long "Here is X:" introductions to real content are fine — those have
    digits / fences / structure past the prefix.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) > 100 or len(text) < 15:
        return GuardVerdict(action="pass")
    if not _HERE_IS_PREFIX_RX.match(text):
        return GuardVerdict(action="pass")
    if any(c.isdigit() for c in text) or "```" in text or "://" in text:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "here_is_only",
        "BLOCKED: done.input starts with 'Here is …' but contains no "
        "actual content (no digits, no URLs, no code). Either include "
        "the deliverable in done.input, or summarize what was produced.",
    )
    return GuardVerdict(action="continue")


_CHATBOT_SIGNOFF_RX = re.compile(
    r"\b(hope (this|that) helps|let me know if (you|there)|"
    r"feel free to ask|happy to help further|"
    r"is there anything else( you'?d like)?|"
    r"if you have (any|more) questions|"
    r"don'?t hesitate to (ask|reach out)|"
    r"i hope this answers your question)\b",
    re.IGNORECASE,
)


def chatbot_signoff_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Strip chatbot sign-off boilerplate from done.input.

    Doesn't reject the whole done — silent trim of the trailing fluff so
    the user gets a clean answer. The orchestrator emits these out of
    chatbot habit; the engine is one-shot and doesn't carry that pattern.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 30:
        return GuardVerdict(action="pass")
    m = _CHATBOT_SIGNOFF_RX.search(text)
    if m is None:
        return GuardVerdict(action="pass")
    # Chop from the start of the matched signoff sentence to end-of-input.
    # Find the sentence boundary before the match.
    cut = text.rfind(".", 0, m.start())
    cut2 = text.rfind("\n", 0, m.start())
    cut = max(cut, cut2)
    if cut < 0 or cut < len(text) // 4:
        # Match is too close to the start to safely chop.
        return GuardVerdict(action="pass")
    decision.input = text[: cut + 1].rstrip()
    return GuardVerdict(action="pass")


# Emoji codepoint ranges — broad enough to catch most user-visible emoji
# without blowing up on normal punctuation.
_EMOJI_RX = re.compile(
    "["
    "\U0001f300-\U0001f5ff"   # symbols & pictographs
    "\U0001f600-\U0001f64f"   # emoticons
    "\U0001f680-\U0001f6ff"   # transport & map
    "\U0001f700-\U0001f77f"   # alchemical
    "\U0001f900-\U0001f9ff"   # supplemental symbols
    "\U0001fa00-\U0001fa6f"   # symbols-and-pictographs ext
    "\U0001fa70-\U0001faff"   # symbols-and-pictographs ext-A
    "\U00002600-\U000027bf"   # dingbats
    "]"
)


def excessive_emoji_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Block done.input with 5+ emoji.

    Emojis suggest the model is treating the user as a customer-service
    interaction. The engine ships technical answers — emoji clutter
    obscures the data. One or two are fine; piles aren't.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(_EMOJI_RX.findall(text)) < 5:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "excessive_emoji",
        "BLOCKED: done.input contains 5+ emoji. Re-emit without the "
        "decoration — engine answers are technical, emoji obscures data.",
    )
    return GuardVerdict(action="continue")


_MODEL_NAME_RX = re.compile(
    r"\b(gpt-?[345]|chat-?gpt|claude(\s+[a-z0-9.]+)?|llama-?\d|"
    r"gemini(\s+(pro|ultra|flash|nano))?|"
    r"mistral|mixtral|phi-?\d|qwen-?\d|deepseek)\b",
    re.IGNORECASE,
)


def model_name_leak_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input that names the model itself.

    "I'm Claude" / "as GPT-4 I think" / "Llama can't…" — leakage of the
    underlying model identity into a user-facing reply. The engine
    abstracts the model; the user shouldn't see the model name unless
    they explicitly asked for it.

    Allowed exception: the user's own message contained the model name
    (e.g. "what model are you?"). We compare against a passed-through
    user-message field if the runner threads it; without that signal,
    we err on the side of nudging.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 10:
        return GuardVerdict(action="pass")
    if not _MODEL_NAME_RX.search(text):
        return GuardVerdict(action="pass")
    # Heuristic exception: question-about-model context — if any prior
    # user message in the scratchpad mentions a model name, allow.
    for e in scratchpad:
        if e.role == "user" and isinstance(e.input, str):
            if _MODEL_NAME_RX.search(e.input):
                return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "model_name_leak",
        "BLOCKED: done.input names a specific model (GPT/Claude/Llama/etc.). "
        "AgentCommander abstracts which model is doing what — don't surface "
        "the underlying model identity unless the user asked. Re-emit "
        "without the model reference.",
    )
    return GuardVerdict(action="continue")


_TURN_MARKER_RX = re.compile(
    r"(\A|\n)(user|assistant|human|ai|system)\s*:",
    re.IGNORECASE,
)


def turn_marker_leak_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input with conversation turn markers (``User:``, ``Assistant:``).

    These leak from training data when the model thinks it's writing a
    transcript. Block — the user should see prose, not transcript syntax.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if not _TURN_MARKER_RX.search(text):
        return GuardVerdict(action="pass")
    # Multiple markers OR a marker at the very start indicate transcript-mode.
    matches = _TURN_MARKER_RX.findall(text)
    if len(matches) < 2 and not _TURN_MARKER_RX.match(text):
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "turn_marker_leak",
        "BLOCKED: done.input contains conversation turn markers "
        "(User:/Assistant:/Human:). That's transcript syntax leaking from "
        "training. Re-emit as a direct reply, no role labels.",
    )
    return GuardVerdict(action="continue")


def repeated_paragraph_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input with the same paragraph repeated.

    Indicates the model went into a degenerate-output loop. Short repeats
    (lines < 30 chars) are exempt — the user might genuinely want a
    bullet list with similar phrasing.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 100:
        return GuardVerdict(action="pass")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) < 2:
        return GuardVerdict(action="pass")
    seen: set[str] = set()
    for p in paragraphs:
        if len(p) < 30:
            continue
        if p in seen:
            push_system_nudge(
                scratchpad, iteration, "repeated_paragraph",
                "BLOCKED: done.input repeats the same paragraph. The model "
                "may be in an output loop. Re-emit a single, non-repeating "
                "answer.",
            )
            return GuardVerdict(action="continue")
        seen.add(p)
    return GuardVerdict(action="pass")


_QUESTION_BACK_RX = re.compile(r"\?\s*\Z")
# The user explicitly invited a question-back / clarification. When this
# matches the user message, a short question IS the requested deliverable, so
# question_only_done_guard must not nag for it.
_ASKED_FOR_QUESTION_RX = re.compile(
    r"\bask\s+(me|us)\b"
    r"|\bask\b[^.?!]{0,40}\bquestions?\b"
    r"|\b(a|one|some|any|each|the|your)\s+questions?\b"
    r"|\bclarif(y|ying|ication)\b"
    r"|\bwhat\s+(do|would|will)\s+you\s+need\b",
    re.IGNORECASE,
)


def question_only_done_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
    decision: OrchestratorDecision,
    user_message: str = "",
) -> GuardVerdict:
    """Reject short done.input that is JUST a question back to the user.

    Rare-but-real pattern: model bounces the question back without taking
    action. Triggers when input ends with `?` AND is under 80 chars AND
    contains no statement. Long replies that incidentally end with `?`
    are fine — those have prose substance.

    There IS a legitimate "I need clarification" path; for that use case
    the answer should be longer / contain explicit "I need…" phrasing,
    which keeps it under this guard's threshold.

    EXCEPTION: when the user *explicitly asked the model to ask a question*
    ("Ask me one question to get started", "what would you need to know?",
    "ask a clarifying question"), a short question-back IS the requested
    output — nudging it is a false positive. This guard had no view of the
    request at all until 2026-06-02, when the v2 eval (case
    ``clarifying-question``) caught it firing twice on exactly that prompt,
    forcing the model to pad its answer over two wasted iterations. Mirror
    terse_done_guard: consult the user message before blocking.
    """
    if _ASKED_FOR_QUESTION_RX.search(user_message or ""):
        return GuardVerdict(action="pass")
    text = decision.input if isinstance(decision.input, str) else ""
    txt = text.strip()
    if len(txt) > 80 or len(txt) < 5:
        return GuardVerdict(action="pass")
    if not _QUESTION_BACK_RX.search(txt):
        return GuardVerdict(action="pass")
    # If there's a statement before the question (sentence count > 1), allow.
    sentences = re.split(r"[.!]\s", txt)
    if len([s for s in sentences if s.strip()]) > 1:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "question_only_done",
        f'BLOCKED: done.input is a single short question ("{txt}"). Either '
        f"answer with what you know, OR explain WHAT specifically you need "
        f"clarified and WHY. Don't bounce the question back unaltered.",
    )
    return GuardVerdict(action="continue")


def all_caps_shout_guard(
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
    decision: OrchestratorDecision,
) -> GuardVerdict:
    """Reject done.input that is mostly uppercase prose (>50% caps over 50 chars).

    Indicates the model is in an "EMPHASIS MODE" loop or copying yelling
    from training data. Acronyms / code snippets / log lines have caps too,
    so we count alphabetic chars only and require a reasonable run length.
    """
    text = decision.input if isinstance(decision.input, str) else ""
    if len(text) < 50:
        return GuardVerdict(action="pass")
    alpha = [c for c in text if c.isalpha()]
    if len(alpha) < 30:
        return GuardVerdict(action="pass")
    upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    if upper_ratio < 0.7:
        return GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "all_caps_shout",
        "BLOCKED: done.input is mostly uppercase. Re-emit in normal case — "
        "the engine isn't a shouting context.",
    )
    return GuardVerdict(action="continue")


# ─── Execute-tier — interactive prompts and platform mistakes ──────────────


_REPL_PROMPT_RX = re.compile(
    r"^(\s*)(>>>|\.\.\.|In\s+\[\d+\]:|Out\s*\[\d+\]:|\$|#)\s+",
    re.MULTILINE,
)


def repl_prompt_in_code_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Strip leading ``>>>``, ``...``, ``In [n]:``, ``$``, ``#`` from code lines.

    The model copy-pasted REPL / shell session output as a script. With
    the prompts in place, the script fails with a SyntaxError on the
    first prompt char. Silent strip of leading prompt sequences makes
    the code runnable.

    Conservative: only strips when the line CLEARLY starts with a prompt
    token followed by whitespace. ``>>> def foo():`` becomes ``def foo():``;
    a Python comment ``# comment`` stays intact (comment guard requires
    the ``#`` to be followed by a single space AND for >50% of lines to
    start with prompts to fire).
    """
    if not code:
        return code, GuardVerdict(action="pass")
    lines = code.split("\n")
    # Count lines starting with REPL/shell prompts.
    prompted = 0
    for line in lines:
        if re.match(r"^\s*(>>>|\.\.\.|In\s+\[\d+\]:|Out\s*\[\d+\]:)\s", line):
            prompted += 1
    # Only fire when the prompt pattern is dominant (>= 25% of non-blank
    # lines) — otherwise stripping ``$`` / ``#`` would break legitimate
    # bash scripts and Python comments.
    non_blank = sum(1 for line in lines if line.strip())
    if non_blank == 0 or prompted * 4 < non_blank:
        return code, GuardVerdict(action="pass")
    cleaned = re.sub(
        r"^(\s*)(>>>|\.\.\.|In\s+\[\d+\]:|Out\s*\[\d+\]:)\s+",
        r"\1", code, flags=re.MULTILINE,
    )
    return cleaned, GuardVerdict(action="pass")


_BASH_PROMPT_RX = re.compile(r"^(\s*)\$\s+(?=\S)", re.MULTILINE)
_PS_PROMPT_RX = re.compile(r"^(\s*)PS\s+[A-Z]:\\[^>\n]*>\s*", re.MULTILINE)


def bash_dollar_prompt_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Strip leading ``$ `` from bash code lines.

    Same mechanism as repl_prompt_in_code_guard but for bash sessions —
    fires only when most non-blank lines have the ``$`` prefix, so
    legitimate ``$VAR`` references aren't mangled.
    """
    if not code or "$" not in code:
        return code, GuardVerdict(action="pass")
    lines = code.split("\n")
    prompted = sum(1 for line in lines if _BASH_PROMPT_RX.match(line))
    non_blank = sum(1 for line in lines if line.strip())
    if non_blank == 0 or prompted * 3 < non_blank:
        return code, GuardVerdict(action="pass")
    cleaned = _BASH_PROMPT_RX.sub(r"\1", code)
    return cleaned, GuardVerdict(action="pass")


def powershell_prompt_in_code_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Strip ``PS C:\\Users\\…>`` PowerShell prompts from code."""
    if not _PS_PROMPT_RX.search(code):
        return code, GuardVerdict(action="pass")
    cleaned = _PS_PROMPT_RX.sub(r"\1", code)
    return cleaned, GuardVerdict(action="pass")


_SUDO_RX = re.compile(r"(^|[\s;|&])sudo\s+", re.MULTILINE)


def sudo_in_execute_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> tuple[str, GuardVerdict]:
    """Block ``sudo`` in execute.

    The engine runs as a regular user against project-local resources.
    There is no reason for `sudo` here — if the model emits it, it's
    almost certainly transposed from a tutorial that assumes a different
    environment. Hard block with a nudge to drop it.
    """
    if not _SUDO_RX.search(code):
        return code, GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "sudo_in_execute",
        "BLOCKED: execute code uses `sudo`. The engine runs as a regular "
        "user; sudo would prompt for a password it cannot provide. Drop the "
        "sudo prefix — the underlying command will work without it on a "
        "developer machine, or fail with a clear permission error if it "
        "genuinely needs root.",
    )
    return code, GuardVerdict(action="continue")


_CURL_INSECURE_RX = re.compile(
    r"\b(curl\s+(-\w*k\w*|\-\-insecure)|"
    r"wget\s+(-\w*\-no-check\w*|\-\-no-check-certificate))\b",
    re.IGNORECASE,
)


def insecure_tls_flag_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,
) -> tuple[str, GuardVerdict]:
    """Block ``curl -k`` / ``wget --no-check-certificate``.

    Bypassing TLS verification opens MITM at exactly the moments the user
    expects security. If the target is genuinely self-signed or expired,
    fix the cert / use a different endpoint. The flag is never the right
    answer in our engine (which has no test-cert workflow).
    """
    if not _CURL_INSECURE_RX.search(code):
        return code, GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "insecure_tls_flag",
        "BLOCKED: execute uses `curl -k` / `wget --no-check-certificate` "
        "to bypass TLS verification. That defeats HTTPS. Use a properly "
        "trusted endpoint, or import the target's CA explicitly — never "
        "skip cert validation.",
    )
    return code, GuardVerdict(action="continue")


_WIN_PATH_LITERAL_RX = re.compile(r"['\"]C:\\\\\\\\[^'\"]+['\"]|['\"]C:\\\\[^'\"]+['\"]")


def windows_backslash_in_python_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Flag literal ``C:\\Users\\…`` Windows paths in Python that aren't raw strings.

    These get interpreted as escape sequences (``\\U`` is unicode-escape,
    ``\\t`` is tab, etc.) and silently corrupt paths. The fix is either
    a raw-string ``r'C:\\Users\\…'`` or forward slashes ``C:/Users/…``.

    We don't auto-rewrite — the right fix depends on whether the path
    is inside a docstring, a regex, or a real path. Just nudge.
    """
    if "C:\\" not in code:
        return code, GuardVerdict(action="pass")
    # Check for unescaped backslash sequences likely to confuse Python.
    if not re.search(r'(?<![rRbB])"[^"]*C:\\[a-zA-Z]', code) \
       and not re.search(r"(?<![rRbB])'[^']*C:\\[a-zA-Z]", code):
        return code, GuardVerdict(action="pass")
    push_system_nudge(
        scratchpad, iteration, "windows_backslash_in_python",
        "BLOCKED: Python code contains a literal Windows path with `\\` "
        "outside a raw string (e.g. \"C:\\Users\\…\"). Backslashes in "
        "regular strings are escape sequences — `\\U` and `\\t` will "
        "corrupt the path. Use a raw string r\"C:\\Users\\…\" or forward "
        "slashes \"C:/Users/…\".",
    )
    return code, GuardVerdict(action="continue")


_SHEBANG_MISMATCH_RX = re.compile(
    r"\A#!\s*(/usr/bin/env\s+)?(\S+)",
)


def shebang_mismatch_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Strip a misleading shebang from code (we use the `language` field instead).

    Pattern: model emits ``#!/bin/bash`` followed by Python code (or vice
    versa). The shebang is meaningless when we hand the code to the
    interpreter through `language=…` — and may confuse a future write_file
    + execute pair where the file extension does the lookup. Silent strip.
    """
    if not code.startswith("#!"):
        return code, GuardVerdict(action="pass")
    # Strip the entire first line (the shebang).
    nl = code.find("\n")
    if nl == -1:
        return "", GuardVerdict(action="pass")
    return code[nl + 1:], GuardVerdict(action="pass")


_PIP_GLOBAL_RX = re.compile(
    r"\bpip\s+install\s+(?!.*--user)(?!.*-r\s)(?!.*\.)\S",
    re.IGNORECASE,
)


def pip_global_install_warn_guard(
    code: str,
    scratchpad: list[ScratchpadEntry], iteration: int,  # noqa: ARG001
) -> tuple[str, GuardVerdict]:
    """Pass-through for pip install — already covered by pip_npm_command_guard
    but kept here as a no-op anchor for future tightening."""
    return code, GuardVerdict(action="pass")


# ─── Output-tier — extra secret patterns ────────────────────────────────────


# These extend the `redact_secrets` set in output_guards.py with patterns
# that landed in OWASP / GitHub-secret-scanning lists more recently. The
# function is a pure-string transform; callers chain it after the existing
# `sanitize_output`.

_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub fine-grained PATs and OAuth tokens.
    (re.compile(r"github_pat_[A-Za-z0-9_]{82}"), "github_pat_[REDACTED]"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"), "gho_[REDACTED]"),
    (re.compile(r"ghu_[A-Za-z0-9]{36}"), "ghu_[REDACTED]"),
    (re.compile(r"ghs_[A-Za-z0-9]{36}"), "ghs_[REDACTED]"),
    (re.compile(r"ghr_[A-Za-z0-9]{36}"), "ghr_[REDACTED]"),
    # Stripe live + restricted keys (sk_live / rk_live).
    (re.compile(r"sk_live_[A-Za-z0-9]{24,}"), "sk_live_[REDACTED]"),
    (re.compile(r"rk_live_[A-Za-z0-9]{24,}"), "rk_live_[REDACTED]"),
    (re.compile(r"pk_live_[A-Za-z0-9]{24,}"), "pk_live_[REDACTED]"),
    # Anthropic API keys.
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "sk-ant-[REDACTED]"),
    # JWT tokens — three base64url segments separated by dots, length-gated
    # so we don't false-positive on URL paths with dotted segments.
    (re.compile(
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ), "eyJ[JWT-REDACTED]"),
    # Generic long hex tokens (32+ chars all-hex) — likely AWS secret /
    # API token. Conservative length so commit hashes (40 hex) and SHA-256
    # digests don't get touched if surrounded by markers like "sha256:".
    (re.compile(r"(?<![a-fA-F0-9:])[a-fA-F0-9]{40}(?![a-fA-F0-9])"),
     "[40-HEX-REDACTED]"),
)


def redact_extra_secrets(text: str) -> str:
    """Apply the round-50 secret patterns. Pure function for chaining
    after `sanitize_output`."""
    if not text:
        return text
    for rx, repl in _EXTRA_SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text


__all__ = [
    # Decision-tier
    "zero_width_unicode_guard",
    "code_fence_in_arg_guard",
    "markdown_link_extract_guard",
    "url_scheme_typo_guard",
    "protocol_relative_url_guard",
    "placeholder_url_guard",
    "empty_role_input_guard",
    "tracking_param_strip_guard",
    "smart_quote_guard",
    "em_dash_in_code_guard",
    "html_entity_decode_guard",
    "url_trailing_punct_guard",
    "url_embedded_whitespace_guard",
    "url_encoded_traversal_guard",
    # Done-tier
    "ai_disclaimer_guard",
    "training_cutoff_leak_guard",
    "unfilled_template_guard",
    "fake_citation_guard",
    "stale_year_guard",
    "hedge_only_guard",
    "unclosed_codefence_guard",
    "over_apologetic_guard",
    "dangling_promise_guard",
    "here_is_only_guard",
    "chatbot_signoff_guard",
    "excessive_emoji_guard",
    "model_name_leak_guard",
    "turn_marker_leak_guard",
    "repeated_paragraph_guard",
    "question_only_done_guard",
    "all_caps_shout_guard",
    # Execute-tier
    "base64_pipe_shell_guard",
    "homoglyph_guard",
    "shell_history_subst_guard",
    "eval_remote_string_guard",
    "repl_prompt_in_code_guard",
    "bash_dollar_prompt_guard",
    "powershell_prompt_in_code_guard",
    "sudo_in_execute_guard",
    "insecure_tls_flag_guard",
    "windows_backslash_in_python_guard",
    "shebang_mismatch_guard",
    "pip_global_install_warn_guard",
    # Output-tier
    "redact_extra_secrets",
]
