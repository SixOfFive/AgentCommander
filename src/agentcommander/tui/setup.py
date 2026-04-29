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
from agentcommander.tui.ansi import style, write, writeln
from agentcommander.tui.render import render_error, render_system_line
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


def first_run_wizard() -> bool:
    """Run the first-time setup. Returns True if a provider was added."""
    writeln()
    writeln(style("accent", "  ─── first-run setup ──────────────────────────────"))
    writeln(style("muted", "  No providers configured. Let's add Ollama as the default."))
    writeln(style("muted", "  Your endpoint is stored in the local user-data DB only "
                            "(never committed to source)."))
    writeln()

    # Non-TTY (piped) — pick the default and move on so smoke tests don't hang.
    if not sys.stdin.isatty():
        endpoint = DEFAULT_OLLAMA_ENDPOINT
        render_system_line(f"non-interactive stdin detected — using default {endpoint}")
    else:
        attempts = 0
        endpoint: str | None = None
        while attempts < 3:
            attempts += 1
            write(style("user_label", f"  Ollama server URL [default {DEFAULT_OLLAMA_ENDPOINT}]: "))
            try:
                raw = input().strip()
            except (EOFError, KeyboardInterrupt):
                writeln()
                render_error("setup cancelled")
                return False
            if not raw:
                endpoint = DEFAULT_OLLAMA_ENDPOINT
                break
            normalized = _normalize_endpoint(raw)
            if normalized is None:
                render_error(f'could not parse "{raw}" — try host:port or http://host:port')
                continue
            check = validate_provider_host(normalized)
            if not check.ok:
                render_error(f"invalid endpoint: {check.reason}")
                continue
            endpoint = normalized
            break
        if endpoint is None:
            render_error("could not get a valid endpoint after 3 attempts; aborting setup")
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
