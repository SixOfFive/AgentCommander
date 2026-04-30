"""OpenRouter provider — pure stdlib, OpenAI-compatible REST API.

OpenRouter (https://openrouter.ai) proxies many model vendors behind a
single OpenAI-compatible endpoint. We register two provider TYPES rather
than two separate classes so the same wire protocol is reused, but the
configured-model-pool differs by type:

  - ``openrouter-free`` — autoconfig points every text role at
    ``OPENROUTER_FREE_DEFAULT_MODEL``. Free tier models are tagged
    ``:free`` upstream and have a daily / minute-rate cap; that cap is
    enough for casual interactive use without billing setup.
  - ``openrouter-paid`` — registered for completeness so adding it later
    is a one-line config change. Setup currently refuses to create one
    until best-guess role-to-model selection is implemented.

Auth: ``Authorization: Bearer <api_key>`` (api_key persisted per project
in ``providers.api_key``). The optional ``HTTP-Referer`` and ``X-Title``
headers are sent so the request appears in OpenRouter's analytics with
this project's name; they're nice-to-have, not required.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from agentcommander.providers.base import (
    ChatChunk,
    ChatMessage,
    ProviderBase,
    ProviderError,
    ProviderRateLimited,
    provider_factory,
)
from agentcommander.types import ProviderConfig


_DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"

# Default model id used when autoconfig fires for an openrouter-free
# provider. Pinned to ``openrouter/free`` per the user's spec — single
# identifier covering the free tier rather than picking a specific
# upstream model. If you'd rather hard-pin a particular free model
# (e.g. ``meta-llama/llama-3.3-70b-instruct:free``), override per-role
# via /roles set or edit this constant.
OPENROUTER_FREE_DEFAULT_MODEL = "openrouter/free"


def _headers(api_key: str | None) -> dict[str, str]:
    """Compose request headers — API key + analytics tags."""
    h = {
        "Content-Type": "application/json",
        # Both fields are documented as optional but improve OpenRouter's
        # rate-limit / abuse behavior for hobby integrations. Pinning the
        # title means the user can see "AgentCommander" in their dashboard
        # rather than "(unknown app)".
        "HTTP-Referer": "https://github.com/SixOfFive/AgentCommander",
        "X-Title": "AgentCommander",
    }
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


class OpenRouterProvider(ProviderBase):
    """Talks to OpenRouter's /v1/chat/completions and /v1/models endpoints.

    The ``type`` field is parameterized so the same class serves both
    ``openrouter-free`` and (eventually) ``openrouter-paid`` flavors.
    The class doesn't enforce free-vs-paid at the API layer — OpenRouter
    decides based on the model id you pass — but the autoconfig surface
    only ever assigns ``:free`` models to ``openrouter-free`` providers.
    """

    def __init__(self, *, id: str, type: str, endpoint: str | None,
                 api_key: str | None) -> None:
        super().__init__(id=id, type=type)
        self.endpoint = (endpoint or _DEFAULT_ENDPOINT).rstrip("/")
        self.api_key = api_key

    # ── Balance / credits ──

    def get_balance(self) -> dict[str, Any] | None:
        """Fetch credits + auth-key info so the status bar can show
        "$ remaining / $ total" plus any daily usage cap.

        Returns ``{"credits_remaining": float, "credits_total": float,
        "credits_used": float, "daily_limit": float | None,
        "daily_limit_remaining": float | None, "is_free_tier": bool}``,
        or ``None`` on transport / auth failure (no key, network, 401).

        Hits two endpoints:
          - ``GET /credits`` → ``{"data": {"total_credits", "total_usage"}}``
          - ``GET /auth/key`` → ``{"data": {"limit", "limit_remaining",
            "usage", "is_free_tier", "rate_limit": {...}}}``

        We tolerate a partial response (one endpoint up, the other down)
        so the UI degrades gracefully rather than blanking on a hiccup.
        """
        if not self.api_key:
            return None
        out: dict[str, Any] = {
            "credits_remaining": None,
            "credits_total": None,
            "credits_used": None,
            "daily_limit": None,
            "daily_limit_remaining": None,
            "is_free_tier": None,
        }
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/credits",
                method="GET",
                headers=_headers(self.api_key),
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            d = data.get("data") if isinstance(data, dict) else None
            if isinstance(d, dict):
                total = d.get("total_credits")
                used = d.get("total_usage")
                if isinstance(total, (int, float)):
                    out["credits_total"] = float(total)
                if isinstance(used, (int, float)):
                    out["credits_used"] = float(used)
                if (isinstance(total, (int, float))
                        and isinstance(used, (int, float))):
                    out["credits_remaining"] = float(total) - float(used)
        except (urllib.error.URLError, OSError, ValueError):
            pass
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/auth/key",
                method="GET",
                headers=_headers(self.api_key),
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            d = data.get("data") if isinstance(data, dict) else None
            if isinstance(d, dict):
                lim = d.get("limit")
                rem = d.get("limit_remaining")
                if isinstance(lim, (int, float)):
                    out["daily_limit"] = float(lim)
                if isinstance(rem, (int, float)):
                    out["daily_limit_remaining"] = float(rem)
                if isinstance(d.get("is_free_tier"), bool):
                    out["is_free_tier"] = d.get("is_free_tier")
        except (urllib.error.URLError, OSError, ValueError):
            pass

        # If both fetches failed, surface None so the caller can blank the
        # display rather than show stale zeros.
        if (out["credits_total"] is None
                and out["daily_limit"] is None):
            return None
        return out

    # ── Health ──

    def health(self) -> bool:
        """One GET against /models. 200 = the API is reachable + key valid."""
        if not self.api_key:
            return False
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/models",
                method="GET",
                headers=_headers(self.api_key),
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError):
            return False

    # ── Models ──

    def list_models(self) -> list[dict[str, Any]]:
        """Catalog of every model OpenRouter exposes. ~300 entries.

        We project to the same shape Ollama returns so downstream UI
        (``/models <id>``, role assignment table) doesn't need a special
        case. The ``id`` field is what gets passed to ``chat`` later.
        """
        if not self.api_key:
            raise ProviderError(
                "OpenRouter API key not configured. "
                "Set it via /providers add openrouter-free openrouter-free "
                "\"OpenRouter Free\" https://openrouter.ai/api/v1 "
                "or re-run /autoconfig clear."
            )
        req = urllib.request.Request(
            f"{self.endpoint}/models",
            method="GET",
            headers=_headers(self.api_key),
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenRouter /models failed: {exc}") from exc

        items = data.get("data", []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or ""
            ctx = item.get("context_length")
            pricing = item.get("pricing") or {}
            out.append({
                "id": mid,
                "family": item.get("name") or "",
                "parameter_size": item.get("architecture", {}).get("modality")
                                  if isinstance(item.get("architecture"), dict) else "",
                "context_length": ctx,
                "pricing_prompt": pricing.get("prompt"),
                "pricing_completion": pricing.get("completion"),
            })
        return out

    # ── Chat ──

    def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,  # noqa: ARG002 - OpenRouter ignores at request time
        json_mode: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[ChatChunk]:
        """Stream a chat completion via the OpenAI-compat SSE endpoint.

        OpenRouter ignores ``num_ctx`` — it routes to whichever upstream
        provider hosts the model and inherits that provider's window. We
        accept the parameter for signature parity with Ollama / llama.cpp
        but don't forward it.

        Cancellation: between SSE lines we check ``should_cancel`` and
        return early. The ``with urllib.request.urlopen(...)`` context
        manager closes the underlying socket so the upstream stops
        generating tokens for this request.
        """
        if not self.api_key:
            raise ProviderError(
                "OpenRouter API key not configured for provider " + self.id
            )

        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
            "temperature": temperature if temperature is not None else 0.3,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=data,
            method="POST",
            headers=_headers(self.api_key),
        )
        try:
            with urllib.request.urlopen(req, timeout=600.0) as resp:
                for raw in resp:
                    if should_cancel is not None and should_cancel():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        yield ChatChunk(content="", done=True)
                        return
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    delta = ""
                    choices = obj.get("choices") or []
                    if choices and isinstance(choices, list):
                        d = choices[0].get("delta") or {}
                        delta = d.get("content", "") or ""
                    usage = obj.get("usage")
                    if delta:
                        yield ChatChunk(content=delta, done=False, raw=obj)
                    if usage:
                        # OpenRouter sometimes emits usage as the last SSE
                        # message before [DONE]. Fold it into a final chunk
                        # so the engine has token counts even when [DONE]
                        # follows a beat later.
                        yield ChatChunk(
                            content="",
                            done=True,
                            prompt_tokens=usage.get("prompt_tokens"),
                            completion_tokens=usage.get("completion_tokens"),
                            raw=obj,
                        )
                        return
        except urllib.error.HTTPError as exc:
            # Surface OpenRouter's structured error body (rate limits,
            # auth failures, model unavailable) so the user sees something
            # actionable rather than "HTTP 429".
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            raise ProviderError(
                f"OpenRouter chat failed: HTTP {exc.code} {exc.reason} — {err_body[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenRouter chat failed: {exc}") from exc

        yield ChatChunk(content="", done=True)


# ─── Factory registrations ────────────────────────────────────────────────
#
# Two distinct ``type`` ids sharing the same class. We split free and paid
# so /providers (and any future autoconfig branching) can tell them apart
# without re-deriving from the model name.

@provider_factory("openrouter-free")
def _openrouter_free_factory(p: ProviderConfig) -> ProviderBase:
    return OpenRouterProvider(
        id=p.id, type="openrouter-free",
        endpoint=p.endpoint, api_key=p.api_key,
    )


@provider_factory("openrouter-paid")
def _openrouter_paid_factory(p: ProviderConfig) -> ProviderBase:
    return OpenRouterProvider(
        id=p.id, type="openrouter-paid",
        endpoint=p.endpoint, api_key=p.api_key,
    )
