"""First-run setup wizard.

Triggered when the providers table is empty. Offers a backend selection
(Ollama / llama.cpp / OpenRouter Free / OpenRouter Paid) and runs the
appropriate configurator for that backend. After this completes, the rest
of the boot sequence (TypeCast refresh + autoconfigure) runs as normal.

Backend choices and what each one means:
  - **Ollama** — local daemon. Prompt for endpoint URL; persist as
    ``ollama-default``. Models picked by TypeCast threshold-cascade.
  - **llama.cpp** — single-model local server. Prompt for endpoint URL;
    persist as ``llamacpp-default``.
  - **OpenRouter Free** — cloud OpenAI-compat API, free tier only.
    Prompt for an API key; persist as ``openrouter-free`` and
    auto-assign every text role to ``OPENROUTER_FREE_DEFAULT_MODEL``.
    Vision / audio / image_gen are left ``unset`` because the free tier
    only covers chat models.
  - **OpenRouter Paid** — registered for completeness but currently
    disabled at the wizard layer until best-guess role-to-model
    selection is implemented.

Pure stdlib — uses ``read_line_at_bottom``. Falls back to a hardcoded
Ollama localhost endpoint if stdin isn't a TTY (so piped smoke tests
don't hang).
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
DEFAULT_LLAMACPP_ENDPOINT = "http://127.0.0.1:8080"
DEFAULT_PROVIDER_ID = "ollama-default"
DEFAULT_PROVIDER_NAME = "Local Ollama"
DEFAULT_LLAMACPP_PROVIDER_ID = "llamacpp-default"
DEFAULT_LLAMACPP_PROVIDER_NAME = "Local llama.cpp"
DEFAULT_OPENROUTER_FREE_PROVIDER_ID = "openrouter-free"
DEFAULT_OPENROUTER_FREE_PROVIDER_NAME = "OpenRouter Free"
DEFAULT_OPENROUTER_PAID_PROVIDER_ID = "openrouter-paid"
DEFAULT_OPENROUTER_PAID_PROVIDER_NAME = "OpenRouter Paid"
DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"

# Backend selection codes. Returned by ``prompt_for_backend`` and consumed
# by ``configure_backend`` to dispatch to the right configurator.
BACKEND_OLLAMA = "ollama"
BACKEND_LLAMACPP = "llamacpp"
BACKEND_OPENROUTER_FREE = "openrouter-free"
BACKEND_OPENROUTER_PAID = "openrouter-paid"
BACKEND_CANCELLED = None


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


# ─── Backend selection (called by first-run wizard + /autoconfig clear) ───


def prompt_for_backend(*, default: str = BACKEND_OLLAMA,
                       max_attempts: int = 3) -> str | None:
    """Show a numbered backend menu and return the selected code.

    Codes:
      ``BACKEND_OLLAMA`` / ``BACKEND_LLAMACPP`` / ``BACKEND_OPENROUTER_FREE``
      / ``BACKEND_OPENROUTER_PAID`` (currently disabled — selecting it
      surfaces a notice and re-prompts) / ``None`` on cancel.

    Non-TTY: returns ``default`` immediately so piped smoke tests keep
    using Ollama like before this menu existed.
    """
    if not sys.stdin.isatty():
        return default

    render_system_line(style("muted", "  Choose a backend:"))
    render_system_line("    1) Ollama " + style("muted", "(local; recommended)"))
    render_system_line("    2) llama.cpp " + style("muted", "(local; single model per server)"))
    render_system_line("    3) OpenRouter Free " + style("muted", "(cloud; free tier — needs an API key)"))
    render_system_line("    4) OpenRouter Paid " + style("muted", "(cloud; vote-driven per-role picks)"))
    render_system_line(style("muted",
        "  Press Enter for option 1, or type 1/2/3/4."))

    for _ in range(max_attempts):
        try:
            raw = read_line_at_bottom("backend ❯ ")
        except KeyboardInterrupt:
            writeln()
            render_error("backend selection cancelled")
            return None
        if raw is None:
            render_error("backend selection cancelled (EOF)")
            return None
        choice = raw.strip().lower()
        if not choice:
            return default
        if choice in ("1", "ollama"):
            return BACKEND_OLLAMA
        if choice in ("2", "llamacpp", "llama.cpp", "llama-cpp"):
            return BACKEND_LLAMACPP
        if choice in ("3", "openrouter-free", "openrouter free", "free"):
            return BACKEND_OPENROUTER_FREE
        if choice in ("4", "openrouter-paid", "openrouter paid", "paid"):
            return BACKEND_OPENROUTER_PAID
        render_error(f'unrecognized choice: "{raw}" — type 1, 2, 3, or 4.')

    render_error(f"could not get a valid backend after {max_attempts} attempts")
    return None


def prompt_for_llamacpp_endpoint(default: str | None = None,
                                 max_attempts: int = 3) -> str | None:
    """Same shape as ``prompt_for_ollama_endpoint`` but with llama.cpp's
    default port (8080). Reuses ``_normalize_endpoint`` and the host
    validator so the user can enter ``host:port`` shortcuts.
    """
    fallback = default or DEFAULT_LLAMACPP_ENDPOINT
    if not sys.stdin.isatty():
        render_system_line(
            f"non-interactive stdin detected — using {fallback}"
        )
        return fallback

    render_system_line(style("muted",
        f"  Enter llama.cpp server URL (or press Enter for {fallback}):"))

    for _ in range(max_attempts):
        try:
            raw_or_none = read_line_at_bottom("llamacpp url ❯ ")
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
        # llama.cpp default port — adjust the normalize fallback so a bare
        # host gets ":8080" instead of Ollama's ":11434".
        s = raw if re.match(r"^https?://", raw, re.IGNORECASE) else "http://" + raw
        parsed = urlparse(s)
        if not parsed.hostname:
            render_error(f'could not parse "{raw}" — try host:port or http://host:port')
            continue
        port = parsed.port or (8080 if parsed.scheme == "http" else 443)
        normalized = f"{parsed.scheme}://{parsed.hostname}:{port}"
        check = validate_provider_host(normalized)
        if not check.ok:
            render_error(f"invalid endpoint: {check.reason}")
            continue
        return normalized

    render_error(f"could not get a valid endpoint after {max_attempts} attempts")
    return None


def prompt_for_openrouter_api_key(*, default: str | None = None,
                                  max_attempts: int = 3) -> str | None:
    """Prompt for an OpenRouter API key. Returns the trimmed key, or
    ``None`` on cancel. Non-TTY returns ``default`` (which is typically
    None) so piped tests don't try to use OpenRouter.

    The key is persisted in ``providers.api_key`` for THIS PROJECT'S DB
    only — each project keeps its own. Never committed to source.
    """
    if not sys.stdin.isatty():
        return default

    render_system_line(style("muted",
        "  Get one free at https://openrouter.ai/keys (sign in, click "
        "Create Key)."))
    render_system_line(style("muted",
        "  Stored only in this project's local SQLite — never committed."))
    if default:
        render_system_line(style("muted",
            f"  (Press Enter to keep the existing key on file: …{default[-6:]})"))

    for _ in range(max_attempts):
        try:
            raw = read_line_at_bottom("api key ❯ ")
        except KeyboardInterrupt:
            writeln()
            render_error("api key prompt cancelled")
            return None
        if raw is None:
            render_error("api key prompt cancelled (EOF)")
            return None
        s = raw.strip()
        if not s:
            if default:
                return default
            render_error("api key is empty — please paste it or Ctrl-C to cancel")
            continue
        # OpenRouter keys start with `sk-or-` — light validation so a
        # paste from the wrong place fails fast.
        if not s.startswith("sk-"):
            render_error("doesn't look like an OpenRouter key (expected sk-or-…). "
                         "Try again or Ctrl-C to cancel.")
            continue
        return s

    render_error(f"could not get a valid api key after {max_attempts} attempts")
    return None


def _configure_openrouter_tier(
    tier: str,
    *,
    provider_id: str,
    provider_name: str,
    fallback_model: str,
    existing_key: str | None,
    msg_no_models_explainer: str,
) -> bool:
    """Shared core for configure_openrouter_{free,paid}.

    Steps:
      1. Prompt for API key (or reuse the existing one with Enter)
      2. Persist the provider row + rebuild instance cache
      3. Fetch /v1/models and populate the tier's catalog with metadata
         (preserving any accumulated votes)
      4. Pin every text role to the highest-voted model from the catalog
         (DB rows that the user manually overrides via /roles set
         survive subsequent /autoconfig clear runs that pick this same
         tier — but on a fresh assign-all, the catalog wins)

    ``tier`` is "free" or "paid". The two configs differ only in:
      - which models show up in the catalog (filtered by :free suffix)
      - the fallback model used when the catalog is empty (first run
        before the populate succeeds)

    Returns True on success, False on cancel / hard failure.
    """
    from agentcommander.db.repos import (
        clear_role_assignments,
        set_config,
        set_role_assignment,
    )
    from agentcommander.providers.base import resolve as resolve_provider
    from agentcommander.providers.openrouter import OpenRouterProvider
    from agentcommander.typecast.openrouter_catalog import (
        TIER_FREE,
        load as load_or_catalog,
        pick_for_role,
        populate_from_openrouter,
    )
    from agentcommander.types import ALL_ROLES, Role

    api_key = prompt_for_openrouter_api_key(default=existing_key)
    if not api_key:
        return False

    cfg = ProviderConfig(
        id=provider_id,
        type=f"openrouter-{tier}",
        name=provider_name,
        endpoint=DEFAULT_OPENROUTER_ENDPOINT,
        api_key=api_key,
        enabled=True,
    )
    upsert_provider(cfg)
    rebuild_from_db()
    audit(f"setup.openrouter_{tier}", {"provider_id": cfg.id})

    # Fetch the catalog from OpenRouter and refresh local metadata. We
    # use the live provider instance so /v1/models goes through the
    # same auth + endpoint config the rest of the engine will.
    populated = 0
    try:
        provider = resolve_provider(cfg.id)
        if isinstance(provider, OpenRouterProvider):
            models = provider.list_models()
            populated = populate_from_openrouter(tier, models)
            render_system_line(style("muted",
                f"  populated catalog: {populated} {tier} model(s) from "
                "openrouter.ai/models"))
    except Exception as exc:  # noqa: BLE001
        render_system_line(style("warn",
            f"  catalog populate failed: {type(exc).__name__}: {exc}  "
            "(continuing with fallback model + accumulated votes)"))

    # Conservative ctx ceiling for the free tier — openrouter/free
    # routes to many models with varying windows (8k–32k); 16k splits
    # the difference. Paid tier: pull the contextLength from the
    # CATALOG entry of whichever model the picker returns for the
    # ORCHESTRATOR (the most ctx-hungry role). Falls back to 16k if
    # that fails.
    or_ctx_default = 16384

    # Wipe prior role_assignments and re-pin from the catalog.
    skipped = {Role.VISION, Role.AUDIO, Role.IMAGE_GEN}
    clear_role_assignments()
    n_assigned = 0
    catalog = load_or_catalog(tier)
    for role in ALL_ROLES:
        if role in skipped:
            continue
        # Pick: vote-driven highest scorer, falling back to the
        # tier-specific default when the catalog has no usable pick
        # (typically first-time setup before any votes).
        picked = pick_for_role(tier, role.value, fallback=fallback_model)
        if picked is None:
            continue
        # Per-role context override: pull from catalog metadata when
        # available so the bar shows the model's actual training cap.
        entry = catalog.get("_models", {}).get(picked)
        ctx = (
            int(entry.get("contextLength")) if entry and isinstance(entry.get("contextLength"), int)
            else or_ctx_default
        )
        set_role_assignment(
            role=role,
            provider_id=cfg.id,
            model=picked,
            is_override=True,
            context_window_tokens=ctx,
        )
        n_assigned += 1

    set_config("session_ceiling_tokens", or_ctx_default)

    render_system_line(
        f"added provider {style('accent', cfg.id)} "
        f"{style('muted', f'(ctx {or_ctx_default // 1024}k default)')}"
    )
    if populated == 0:
        render_system_line(style("muted",
            f"  {msg_no_models_explainer}"))
    render_system_line(style("muted",
        f"  assigned {n_assigned} text role(s) "
        "(vision/audio/image_gen left unset — chat-only)"))
    return True


def configure_openrouter_free(*, existing_key: str | None = None) -> bool:
    """Persist an ``openrouter-free`` provider, fetch the OpenRouter
    catalog, and pin every text role to the highest-voted ``:free``
    model (or the fallback when no votes exist yet).

    Roles ``vision``, ``audio``, ``image_gen`` are intentionally left
    unset — the free tier covers chat models only.
    """
    from agentcommander.providers.openrouter import OPENROUTER_FREE_DEFAULT_MODEL
    return _configure_openrouter_tier(
        "free",
        provider_id=DEFAULT_OPENROUTER_FREE_PROVIDER_ID,
        provider_name=DEFAULT_OPENROUTER_FREE_PROVIDER_NAME,
        fallback_model=OPENROUTER_FREE_DEFAULT_MODEL,
        existing_key=existing_key,
        msg_no_models_explainer=(
            "no :free models populated yet (network failed or empty "
            "catalog) — falling back to the default. /autoconfig clear "
            "→ OpenRouter Free will retry the fetch."
        ),
    )


def configure_openrouter_paid(*, existing_key: str | None = None) -> bool:
    """Same machinery as the free configurator, but populates the paid
    catalog (excludes ``:free`` models). On first setup (no votes yet)
    every role gets pinned to the fallback model; voting refines the
    picks over time.
    """
    return _configure_openrouter_tier(
        "paid",
        provider_id=DEFAULT_OPENROUTER_PAID_PROVIDER_ID,
        provider_name=DEFAULT_OPENROUTER_PAID_PROVIDER_NAME,
        fallback_model="anthropic/claude-sonnet-4.5",
        existing_key=existing_key,
        msg_no_models_explainer=(
            "no paid models populated yet (network failed) — falling "
            "back to claude-sonnet-4.5. /autoconfig clear → OpenRouter "
            "Paid will retry the fetch."
        ),
    )


def configure_ollama() -> bool:
    """Run the Ollama-specific configurator. Returns True on success."""
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
    audit("setup.ollama", {"provider_id": cfg.id, "endpoint": endpoint})
    render_system_line(f'added provider {style("accent", cfg.id)} → {endpoint}')
    return True


def configure_llamacpp() -> bool:
    """Run the llama.cpp-specific configurator. Returns True on success."""
    endpoint = prompt_for_llamacpp_endpoint()
    if endpoint is None:
        return False
    cfg = ProviderConfig(
        id=DEFAULT_LLAMACPP_PROVIDER_ID,
        type="llamacpp",
        name=DEFAULT_LLAMACPP_PROVIDER_NAME,
        endpoint=endpoint,
        api_key=None,
        enabled=True,
    )
    upsert_provider(cfg)
    rebuild_from_db()
    audit("setup.llamacpp", {"provider_id": cfg.id, "endpoint": endpoint})
    render_system_line(f'added provider {style("accent", cfg.id)} → {endpoint}')
    return True


def configure_backend(backend: str, *, existing_key: str | None = None) -> bool:
    """Dispatch to the right configurator for the given backend code.

    ``existing_key`` is forwarded to the OpenRouter path so an existing
    project DB's stored key surfaces as the default when the user re-runs
    /autoconfig clear.
    """
    if backend == BACKEND_OLLAMA:
        return configure_ollama()
    if backend == BACKEND_LLAMACPP:
        return configure_llamacpp()
    if backend == BACKEND_OPENROUTER_FREE:
        return configure_openrouter_free(existing_key=existing_key)
    if backend == BACKEND_OPENROUTER_PAID:
        render_error(
            "OpenRouter Paid is disabled. Add a paid provider manually "
            "with /providers add and pin per-role models with /roles set."
        )
        return False
    return False


def first_run_wizard() -> bool:
    """Run the first-time setup. Returns True if a provider was added."""
    writeln()
    writeln(style("accent", "  ─── first-run setup ──────────────────────────────"))
    writeln(style("muted", "  No providers configured. Pick a backend below."))
    writeln(style("muted", "  Endpoint URLs and API keys are stored in this "
                            "project's local DB only (never committed)."))
    writeln()

    backend = prompt_for_backend(default=BACKEND_OLLAMA)
    if backend is None:
        return False

    ok = configure_backend(backend)
    if not ok:
        return False

    writeln(style("accent", "  ────────────────────────────────────────────────────"))
    writeln()
    return True
