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
from dataclasses import dataclass


@dataclass
class HostCheck:
    ok: bool
    reason: str | None = None


_REJECT_PATTERNS_USER: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(file|javascript|data|gopher|ftp|jar|ldap|dict):", re.IGNORECASE),
     "scheme not allowed (only http[s]:// or bare host:port)"),
    (re.compile(r"^\s*localhost(:|/|$)", re.IGNORECASE),
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
    for pattern, reason in patterns:
        if pattern.search(trimmed):
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
