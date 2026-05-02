"""SSRF-safe host validator.

Ported from EngineCommander/src/main/utils/host-validator.ts.

Used for: provider endpoint configuration, MCP server URLs, web/fetch tool
targets when an LLM hands one over.

Two functions:
  - validate_user_host: strict; rejects loopback/link-local for LLM-supplied URLs
  - validate_provider_host: permissive; allows loopback (e.g. local Ollama)
                           but still blocks cloud-metadata link-local
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


@dataclass
class HostCheck:
    ok: bool
    reason: str | None = None


_REJECT_PATTERNS_USER: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(file|javascript|data|gopher|ftp|jar|ldap|dict):", re.IGNORECASE),
     "scheme not allowed (only http[s]:// or bare host:port)"),
    (re.compile(r"(^|//)\s*localhost(:|/|$)", re.IGNORECASE),
     "loopback hostname rejected (SSRF guard)"),
    (re.compile(r"(^|//)127\.\d+\.\d+\.\d+(:|/|$)"),
     "loopback IP 127.0.0.0/8 rejected (SSRF guard)"),
    (re.compile(r"(^|//)0\.0\.0\.0(:|/|$)"),
     "wildcard IP 0.0.0.0 rejected (SSRF guard)"),
    (re.compile(r"(^|//)169\.254\.\d+\.\d+(:|/|$)"),
     "link-local 169.254.0.0/16 rejected (blocks cloud metadata SSRF)"),
    (re.compile(r"(^|//)\[?::1\]?(:|/|$)"),
     "IPv6 loopback ::1 rejected (SSRF guard)"),
    (re.compile(r"(^|//)\[?(::|0:0:0:0:0:0:0:0)\]?(:|/|$)"),
     "IPv6 wildcard rejected (SSRF guard)"),
    (re.compile(r"(^|//)\[?f[cd][0-9a-f]{2}:", re.IGNORECASE),
     "IPv6 link-local fe80::/ or ULA fc00::/7 rejected (SSRF guard)"),
]

_REJECT_PATTERNS_PROVIDER: list[tuple[re.Pattern[str], str]] = [
    # ftp is included here too — providers always speak HTTP(S); allowing
    # ftp:// would let a misconfigured endpoint exfiltrate the api_key over
    # cleartext. Same blocklist as user URLs minus the loopback bans.
    (re.compile(r"^\s*(file|javascript|data|gopher|ftp|jar|ldap|dict):", re.IGNORECASE),
     "scheme not allowed"),
    (re.compile(r"(^|//)169\.254\.\d+\.\d+(:|/|$)"),
     "link-local 169.254.0.0/16 rejected (cloud metadata SSRF)"),
]


def _check(host: str, patterns: list[tuple[re.Pattern[str], str]]) -> HostCheck:
    if not isinstance(host, str):
        return HostCheck(ok=False, reason="host must be a string")
    trimmed = host.strip()
    if not trimmed:
        return HostCheck(ok=False, reason="host is required")
    if len(trimmed) > 253:
        return HostCheck(ok=False, reason="host too long (>253 chars)")
    # Control characters in URLs are an injection vector — some URL
    # parsers truncate the host at the first NUL, sending the request
    # to a different host than the visible string suggests
    # ("http://example.com\x00.attacker.com" routed to example.com on
    # a strict parser, but to attacker.com on a lax one). Reject any
    # 0x00–0x1F or 0x7F byte unconditionally.
    for ch in trimmed:
        if ord(ch) < 32 or ord(ch) == 127:
            return HostCheck(ok=False, reason="host contains control character")
    # Percent-encoded bypass: a URL like ``http://%6c%6f%63%61%6c%68%6f%73%74``
    # decodes to ``http://localhost`` once urllib actually issues the
    # request, but the regex above wouldn't match the encoded form. Run
    # the patterns against BOTH the literal string and a single-pass
    # decoded form so the loopback / link-local guards can't be slipped
    # past with %XX. (One decode pass matches what urllib does on the
    # request side; we don't loop since double-decoding isn't standard
    # and would over-match legitimate paths.)
    try:
        decoded = urllib.parse.unquote(trimmed)
    except Exception:  # noqa: BLE001 — defensive
        decoded = trimmed
    candidates = (trimmed, decoded) if decoded != trimmed else (trimmed,)
    for pattern, reason in patterns:
        for cand in candidates:
            if pattern.search(cand):
                return HostCheck(ok=False, reason=reason)
    return HostCheck(ok=True)


def validate_user_host(host: str) -> HostCheck:
    """Strict: reject loopback + link-local + non-HTTP schemes.

    Use this for URLs an LLM picks (web fetch, MCP autodiscover, etc.).
    """
    return _check(host, _REJECT_PATTERNS_USER)


def validate_provider_host(host: str) -> HostCheck:
    """Permissive: allow loopback so the user can configure local Ollama.

    Still rejects link-local 169.254.x.x (cloud metadata) and bad schemes.
    """
    return _check(host, _REJECT_PATTERNS_PROVIDER)
