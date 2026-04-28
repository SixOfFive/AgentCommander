"""Fetch / web-content guards — analyze fetch results.

Run AFTER a fetch to detect login walls, JS-only SPAs, paywalls, etc., and
append hint strings to the scratchpad so the orchestrator picks a different
approach next iteration.

Ported from EngineCommander/src/main/orchestration/guards/fetch-guards.ts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentcommander.types import ScratchpadEntry


_LOGIN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(sign\s*in|log\s*in|log\s*on|authenticate)\b.*\b(to\s+continue|to\s+access|required)\b", re.IGNORECASE),
    re.compile(r"<form[^>]*\b(login|signin|auth)\b", re.IGNORECASE),
    re.compile(r"\b(username|email)\b.*\b(password)\b", re.IGNORECASE),
    re.compile(r"\b(403|401)\b.*\b(forbidden|unauthorized|access\s+denied)\b", re.IGNORECASE),
    re.compile(r"\byou\s+(must|need\s+to)\s+(log|sign)\s*(in|on|up)\b", re.IGNORECASE),
]

_JS_PATTERNS_RX: list[re.Pattern[str]] = [
    re.compile(r"\bnoscript\b.*\b(enable\s+javascript|javascript\s+is\s+required|requires?\s+javascript)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r'<div\s+id="(root|app|__next|__nuxt)">\s*</div>', re.IGNORECASE),
    re.compile(r"\byou\s+need\s+to\s+enable\s+javascript\b", re.IGNORECASE),
    re.compile(r"\bthis\s+(page|app|site)\s+requires?\s+javascript\b", re.IGNORECASE),
]
_JS_FRAMEWORK_RX = re.compile(r"\b(react|angular|vue|svelte|next)\b", re.IGNORECASE)

_PAYWALL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(subscribe|subscription)\s+(to\s+)?(read|access|view|continue)\b", re.IGNORECASE),
    re.compile(r"\b(premium|pro)\s+(content|article|member)", re.IGNORECASE),
    re.compile(r"\bpaywall\b", re.IGNORECASE),
    re.compile(r"\bcookie\s+(consent|notice|banner|policy)\b.*\b(accept|agree|allow)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bgdpr\b.*\b(consent|accept)\b", re.IGNORECASE),
]

_API_URL_RX = re.compile(r"\.(json|api)\b|/api/|/v[0-9]+/|format=json", re.IGNORECASE)


def detect_login_wall(content: str) -> str:
    for p in _LOGIN_PATTERNS:
        if p.search(content):
            return ("\n[WARNING: Response appears to be a login/authentication page, "
                    "not the actual content. Try a different URL or use a browser session.]")
    return ""


def detect_js_required(content: str) -> str:
    for p in _JS_PATTERNS_RX:
        if p.search(content):
            return ("\n[WARNING: Page requires JavaScript to render. "
                    "Use a browser tool instead of fetch.]")
    if _JS_FRAMEWORK_RX.search(content) and len(content) < 500:
        return ("\n[WARNING: Response looks like an empty SPA shell. "
                "Use a browser tool instead of fetch.]")
    return ""


def detect_paywall(content: str) -> str:
    for p in _PAYWALL_PATTERNS:
        if p.search(content):
            return "\n[NOTE: Content may be behind a paywall or cookie consent wall. Results may be incomplete.]"
    return ""


def detect_empty_response(content: str, url: str) -> str:
    if len(content.strip()) < 50 and "api." not in url and "/json" not in url:
        return ("\n[WARNING: Response is very short (<50 chars). "
                "The page may have redirected, requires JavaScript, or returned an error.]")
    return ""


def detect_content_mismatch(content: str, url: str) -> str:
    expects_json = bool(_API_URL_RX.search(url))
    is_html = (content.strip().startswith("<")
               and ("<html" in content or "<!DOCTYPE" in content))
    if expects_json and is_html:
        return ("\n[WARNING: URL suggests JSON response but received HTML. "
                "The API may require authentication, or the endpoint may have changed.]")
    return ""


def analyze_fetch_result(content: str, url: str) -> str:
    """Combine all 5 fetch-content checks into a single hint string (may be empty)."""
    return (
        detect_login_wall(content)
        + detect_js_required(content)
        + detect_paywall(content)
        + detect_empty_response(content, url)
        + detect_content_mismatch(content, url)
    )


# ─── HTTP request validator ────────────────────────────────────────────────


@dataclass
class HttpRequestValidation:
    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    warning: str | None = None


def validate_http_request(method: str, url: str,
                          headers: dict[str, str] | None = None,
                          body: str | None = None) -> HttpRequestValidation:
    fixed_headers = dict(headers or {})
    warning: str | None = None
    upper = method.upper()

    if upper in ("POST", "PUT", "PATCH") and body:
        if "Content-Type" not in fixed_headers and "content-type" not in fixed_headers:
            stripped = body.strip()
            if stripped.startswith(("{", "[")):
                fixed_headers["Content-Type"] = "application/json"
            elif "=" in body and "&" in body:
                fixed_headers["Content-Type"] = "application/x-www-form-urlencoded"

    if "Accept" not in fixed_headers and "accept" not in fixed_headers:
        if _API_URL_RX.search(url):
            fixed_headers["Accept"] = "application/json"

    if upper == "GET" and body:
        warning = ("GET request with body is unusual — some servers ignore it. "
                   "Consider POST if sending data.")

    return HttpRequestValidation(method=upper, url=url, headers=fixed_headers,
                                 body=body, warning=warning)


# ─── Git operation validator ───────────────────────────────────────────────


_GIT_SENSITIVE: list[re.Pattern[str]] = [
    re.compile(r"\.env$", re.IGNORECASE),
    re.compile(r"credentials", re.IGNORECASE),
    re.compile(r"secrets?\.json", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"\.p12$", re.IGNORECASE),
]


@dataclass
class GitValidation:
    command: str
    files: str | None
    message: str | None
    warning: str | None = None
    blocked: bool = False


def validate_git_operation(command: str, files: str | None = None,
                           message: str | None = None,
                           scratchpad: list[ScratchpadEntry] | None = None) -> GitValidation:
    warning: str | None = None
    blocked = False

    if command == "commit" and scratchpad is not None:
        has_add = any(e.action == "git" and e.input == "add" for e in scratchpad)
        if not has_add:
            warning = 'Committing without a prior "git add" — changes may not be staged.'

    if command == "add" and files:
        file_list = [f.strip() for f in re.split(r"[\s,]+", files) if f.strip()]
        sensitive = [f for f in file_list if any(p.search(f) for p in _GIT_SENSITIVE)]
        if sensitive:
            warning = (f"WARNING: about to git add potentially sensitive file(s): "
                       f'{", ".join(sensitive)}. These may contain credentials.')
        if ".env" in file_list:
            warning = ("BLOCKED: .env files should not be committed — they contain "
                       "secrets. Add .env to .gitignore instead.")
            blocked = True

    if command == "commit":
        if not message or len(message.strip()) < 3:
            message = "Update files"
            warning = "Auto-generated commit message (original was empty/too short)."
        elif "\n" in message:
            first_line = message.split("\n", 1)[0]
            if len(first_line) > 100:
                rest = message[len(first_line):]
                message = first_line[:72] + "\n\n" + rest.lstrip()
                warning = "Reformatted long commit message (first line capped at 72 chars)."

    return GitValidation(command=command, files=files, message=message,
                         warning=warning, blocked=blocked)


# Suppress unused-Any warning — `Any` is used implicitly by ctx-style callers
_ = Any
