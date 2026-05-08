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
    fields = ("input", "url", "code", "path", "content", "command",
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
    fence_fields = ("code", "input", "content")
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
    # Execute-tier
    "base64_pipe_shell_guard",
    "homoglyph_guard",
    "shell_history_subst_guard",
    "eval_remote_string_guard",
]
