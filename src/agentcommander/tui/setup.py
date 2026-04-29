"""First-run setup wizard.

Triggered when the providers table is empty. Prompts for the Ollama
endpoint (server address + port), validates lightly, and persists a
`ollama-default` provider row to the user-data SQLite. After this completes,
the rest of the boot sequence (TypeCast refresh + autoconfigure) runs as
normal.

Pure stdlib — uses `input()`. Falls back to a hardcoded localhost endpoint
if stdin is not a TTY (so piped smoke tests don't hang).
"""
from __future__ import annotations

import re
import sys
from urllib.parse import urlparse

from agentcommander.db.repos import audit, list_providers, upsert_provider
from agentcommander.providers.base import rebuild_from_db
from agentcommander.safety.host_validator import validate_provider_host
from agentcommander.tui.ansi import style, writeln
from agentcommander.tui.render import render_error, render_system_line
from agentcommander.tui.status_bar import read_line_at_bottom
from agentcommander.types import ProviderConfig

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_PROVIDER_ID = "ollama-default"
DEFAULT_PROVIDER_NAME = "Local Ollama"


def _normalize_endpoint(raw: str) -> str | None:
    """Accept input forms like '127.0.0.1:11434', 'localhost', 'http://host:port',
    'host.example.com'. Return a normalized URL or None on invalid input."""
    s = raw.strip()
    if not s:
        return None
    # If a scheme is missing, prepend http://
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = "http://" + s
    parsed = urlparse(s)
    if not parsed.hostname:
        return None
    # Normalize: scheme://host[:port]
    host = parsed.hostname
    port = parsed.port
    if port is None and ":" in (parsed.netloc or ""):
        return None  # malformed
    if port is None:
        # Default Ollama port if user only typed a host.
        port = 11434 if parsed.scheme == "http" else 443
    return f"{parsed.scheme}://{host}:{port}"


def needs_first_run_setup() -> bool:
    return not list_providers()


def prompt_for_ollama_endpoint(default: str | None = None,
                                max_attempts: int = 3) -> str | None:
    """Prompt the user for an Ollama endpoint URL on the bottom-anchored row.

    Returns the normalized URL on success, or ``None`` if the prompt was
    cancelled (Ctrl-C / EOF / used up all attempts). When stdin is not a TTY,
    falls back to ``default`` (or ``DEFAULT_OLLAMA_ENDPOINT``) without
    blocking — matters for piped smoke tests.

    Used by both first-run setup and ``/autoconfig clear``.
    """
    fallback = default or DEFAULT_OLLAMA_ENDPOINT

    if not sys.stdin.isatty():
        render_system_line(
            f"non-interactive stdin detected — using {fallback}"
        )
        return fallback

    render_system_line(style("muted",
        f"  Enter Ollama server URL (or press Enter for {fallback}):"))

    for _ in range(max_attempts):
        try:
            raw_or_none = read_line_at_bottom("ollama url ❯ ")
        except KeyboardInterrupt:
            writeln()
            render_error("endpoint prompt cancelled")
            return None
        if raw_or_none is None:
            render_error("endpoint prompt cancelled (EOF)")
            return None
        raw = raw_or_none.strip()
        if not raw:
            return fallback
        normalized = _normalize_endpoint(raw)
        if normalized is None:
            render_error(f'could not parse "{raw}" — try host:port or http://host:port')
            continue
        check = validate_provider_host(normalized)
        if not check.ok:
            render_error(f"invalid endpoint: {check.reason}")
            continue
        return normalized

    render_error(f"could not get a valid endpoint after {max_attempts} attempts")
    return None


def first_run_wizard() -> bool:
    """Run the first-time setup. Returns True if a provider was added."""
    writeln()
    writeln(style("accent", "  ─── first-run setup ──────────────────────────────"))
    writeln(style("muted", "  No providers configured. Let's add Ollama as the default."))
    writeln(style("muted", "  Your endpoint is stored in the local user-data DB only "
                            "(never committed to source)."))
    writeln()

    endpoint = prompt_for_ollama_endpoint()
    if endpoint is None:
        return False

    cfg = ProviderConfig(
        id=DEFAULT_PROVIDER_ID,
        type="ollama",
        name=DEFAULT_PROVIDER_NAME,
        endpoint=endpoint,
        api_key=None,
        enabled=True,
    )
    upsert_provider(cfg)
    rebuild_from_db()
    audit("setup.first_run", {"provider_id": cfg.id, "endpoint": endpoint})

    render_system_line(f'added provider {style("accent", cfg.id)} → {endpoint}')
    writeln(style("accent", "  ────────────────────────────────────────────────────"))
    writeln()
    return True
