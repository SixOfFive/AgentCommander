"""Ollama provider — pure stdlib (urllib for HTTP).

Talks to the Ollama HTTP API:
  GET  /api/tags         → list installed models
  POST /api/chat (stream) → streaming chat completion (one JSON object per line)

Honors the user's context-ceiling override via `options.num_ctx` so big
models actually load with the requested window (up to trained max).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
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
from agentcommander.safety.host_validator import validate_provider_host
from agentcommander.types import ProviderConfig


_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

# Idle-unload window for loaded models. Ollama's own default is also 5
# minutes — we send it explicitly so behavior doesn't drift if the daemon
# default ever changes. To unload immediately use 0 (see `unload()`).
KEEP_ALIVE_IDLE = "5m"


def _post_stream(
    url: str, body: dict[str, Any], timeout: float = 600.0,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """POST a JSON body and yield each newline-delimited JSON object.

    When ``should_cancel`` is supplied and returns True between chunks,
    the loop breaks; the surrounding ``with urllib.request.urlopen(...)``
    context manager closes the underlying socket so the daemon stops
    generating tokens for this request.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            if should_cancel is not None and should_cancel():
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                # Bad chunk — skip rather than abort the whole stream.
                continue


def _get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _safe_token_count(raw: Any) -> int | None:
    """Coerce a model-reported token count to a non-negative int, or
    None if the value is missing / non-numeric / nonsense.

    Ollama (and other OpenAI-compatible daemons) will occasionally emit
    ``eval_count: -100`` or even ``"thirty"`` if their internal counter
    overflows or a wrapper stringifies the field. Without this clamp the
    bad number lands in throughput EMA, the popout token display, and
    the status bar — so a single bad response permanently skews the
    running average. We coerce silently rather than raise: a missing
    token count is recoverable; crashing the run isn't.
    """
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, n)


def _parse_retry_after(raw: Any) -> float | None:
    """Parse an HTTP ``Retry-After`` header value per RFC 7231 §7.1.3.

    Two formats are valid:
      - delay-seconds: a non-negative integer ("60")
      - HTTP-date: an absolute date in IMF-fixdate / RFC 850 / asctime
        format (e.g. "Wed, 21 Oct 2026 07:28:00 GMT")

    Returns the wait in seconds (>= 0), or ``None`` if the value is
    missing / unparseable. Negative integer-seconds are clamped to 0
    rather than passed through — a server emitting a negative wait is
    buggy and we don't want the engine's backoff math to flip sign.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Try integer-seconds first (the common case).
    try:
        v = float(s)
        return max(0.0, v)
    except ValueError:
        pass
    # Fall back to HTTP-date. urllib has a parser tucked away.
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    import time as _time
    delta = dt.timestamp() - _time.time()
    return max(0.0, delta)


class OllamaProvider(ProviderBase):
    def __init__(self, *, id: str, endpoint: str) -> None:
        check = validate_provider_host(endpoint)
        if not check.ok:
            raise ProviderError(f"Invalid Ollama endpoint: {check.reason}")
        super().__init__(id=id, type="ollama")
        self.endpoint = endpoint.rstrip("/")

    # ── Health ──

    def health(self) -> bool:
        try:
            _get_json(f"{self.endpoint}/api/tags", timeout=3.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ── Models ──

    def list_models(self) -> list[dict[str, Any]]:
        try:
            data = _get_json(f"{self.endpoint}/api/tags", timeout=10.0)
        except urllib.error.URLError as exc:
            raise ProviderError(f"Ollama /api/tags failed: {exc}") from exc
        except (ValueError, OSError) as exc:
            # ValueError covers JSONDecodeError (it's a subclass) for when
            # the daemon returns non-JSON (HTML error page, truncated body,
            # corrupt response). Wrap so callers can catch ProviderError as
            # a single contract instead of leaking ``json.decoder``
            # internals.
            raise ProviderError(f"Ollama /api/tags returned invalid response: {exc}") from exc

        models = data.get("models", []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for m in models:
            details = m.get("details", {}) if isinstance(m, dict) else {}
            out.append({
                "id": m.get("name", ""),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
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
        num_ctx: int | None = None,
        json_mode: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[ChatChunk]:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        # Validate num_ctx — Ollama silently re-loads the model with weird
        # contexts (or fails opaquely deep in the C runtime) when given
        # garbage. A few caller bugs we've seen produce strings ("32k"),
        # negatives, zero, or absurdly huge values. Reject non-int or
        # out-of-range values up front so the error message points at us
        # rather than at a daemon stack trace 30s into the call.
        if num_ctx is not None:
            if isinstance(num_ctx, bool) or not isinstance(num_ctx, int):
                raise ProviderError(
                    f"num_ctx must be a positive int (got {type(num_ctx).__name__}: {num_ctx!r})"
                )
            if num_ctx <= 0:
                raise ProviderError(
                    f"num_ctx must be > 0 (got {num_ctx})"
                )
            # 16M tokens is far above any current model's training cap.
            # Beyond this is almost certainly a unit mistake (KB vs tokens)
            # or a runaway computation. Cap at 16M so we never accidentally
            # ask Ollama to allocate gigabytes of KV-cache.
            if num_ctx > 16_000_000:
                raise ProviderError(
                    f"num_ctx too large ({num_ctx}); max 16,000,000"
                )
            options["num_ctx"] = num_ctx

        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
            # Keep the model resident for KEEP_ALIVE_IDLE after this call,
            # then auto-unload. Sent on every request so the timer resets
            # whenever a role uses the model — exactly what we want.
            "keep_alive": KEEP_ALIVE_IDLE,
        }
        if options:
            body["options"] = options
        if json_mode:
            body["format"] = "json"

        url = f"{self.endpoint}/api/chat"
        try:
            stream = _post_stream(url, body, should_cancel=should_cancel)
            for chunk in stream:
                # Cancellation check after each chunk — closes the socket
                # via the _post_stream context manager so the daemon stops
                # generating tokens for this request.
                if should_cancel is not None and should_cancel():
                    return
                # ``_post_stream`` decodes whatever JSON value the line
                # contains — that's normally a dict, but a misbehaving
                # daemon (or a man-in-the-middle injecting noise into the
                # stream) can emit a bare int / string / list. ``.get``
                # only exists on dicts; without this guard the engine
                # crashes with ``AttributeError: 'int' object has no
                # attribute 'get'``. Skip non-dict lines silently —
                # consistent with how ``_post_stream`` handles undecodable
                # bytes.
                if not isinstance(chunk, dict):
                    continue
                content = ""
                msg = chunk.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content", "") or ""
                done = bool(chunk.get("done", False))
                if done:
                    yield ChatChunk(
                        content=content,
                        done=True,
                        # Clamp negative / non-int token counts to 0 — a
                        # misbehaving daemon emitting eval_count=-100 (or
                        # the string "thirty") would otherwise propagate
                        # nonsense into throughput EMA, the popout token
                        # display, the bar's running totals, etc. None
                        # passes through unchanged so callers can still
                        # distinguish "not reported" from "reported as
                        # zero".
                        prompt_tokens=_safe_token_count(chunk.get("prompt_eval_count")),
                        completion_tokens=_safe_token_count(chunk.get("eval_count")),
                        raw=chunk,
                    )
                    return
                if content:
                    yield ChatChunk(content=content, done=False, raw=chunk)
        except urllib.error.HTTPError as exc:
            # 429 → bubble as ProviderRateLimited so the engine's retry
            # helper kicks in instead of treating this as a final failure.
            # Ollama rarely rate-limits (it's local), but a remote daemon
            # behind a proxy could; surface it cleanly when it happens.
            if exc.code == 429:
                # Robust Retry-After parsing — handles seconds AND HTTP-date
                # formats per RFC 7231, clamps negatives to 0, returns None
                # on any parse failure (engine then uses its own backoff).
                retry_hdr = exc.headers.get("Retry-After") if exc.headers else None
                retry_after = _parse_retry_after(retry_hdr)
                raise ProviderRateLimited(
                    f"Ollama rate-limited: HTTP {exc.code}",
                    retry_after=retry_after,
                ) from exc
            raise ProviderError(f"Ollama /api/chat failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Ollama /api/chat failed: {exc}") from exc

    # ── Unload (Ollama-specific; llama.cpp serves one model per process and
    #    should never be told to unload — only the Ollama subclass overrides
    #    these). ──

    def list_loaded(self) -> list[str]:
        """Model IDs currently resident in Ollama's memory (per /api/ps).

        Returns an empty list on transport failure or if the daemon doesn't
        support /api/ps. Used by `unload_all_loaded` so we only POST to
        models that are actually loaded — avoids spurious requests against
        models we never used this session.
        """
        return [d["name"] for d in self.list_loaded_details() if d.get("name")]

    def list_loaded_details(self) -> list[dict[str, Any]]:
        """Rich metadata for resident models — what /vram needs.

        Wraps Ollama's /api/ps. Each dict has: name, size_vram (bytes),
        size (bytes), expires_at (ISO8601 string), details (parameter_size,
        quantization_level, family, format). Returns ``[]`` on transport
        failure so `/vram` degrades gracefully when the daemon is down.
        """
        try:
            data = _get_json(f"{self.endpoint}/api/ps", timeout=3.0)
        except (urllib.error.URLError, OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        models = data.get("models", []) or []
        out: list[dict[str, Any]] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("model")
            if not isinstance(name, str) or not name:
                continue
            out.append({
                "name": name,
                "size_vram": m.get("size_vram"),
                "size": m.get("size"),
                "expires_at": m.get("expires_at"),
                "details": m.get("details") if isinstance(m.get("details"), dict) else {},
            })
        return out

    def unload(self, model: str) -> bool:
        """Tell Ollama to evict this model from memory immediately.

        Posts ``{"model": <name>, "keep_alive": 0}`` to /api/generate with
        no prompt — the daemon treats this as "load with zero retention",
        which unloads any resident copy. Returns True on success, False if
        the request fails (best-effort; we don't want exit cleanup to throw).
        """
        body = {"model": model, "keep_alive": 0}
        url = f"{self.endpoint}/api/generate"
        try:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url=url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def unload_all_loaded(self) -> int:
        """Unload every model currently resident on this Ollama daemon.

        Returns the count of successful unloads. Called at app exit so the
        user's VRAM is freed cleanly without waiting for the 5-minute idle
        timer.
        """
        count = 0
        for model_id in self.list_loaded():
            if self.unload(model_id):
                count += 1
        return count


@provider_factory("ollama")
def _ollama_factory(p: ProviderConfig) -> ProviderBase:
    return OllamaProvider(id=p.id, endpoint=p.endpoint or _DEFAULT_ENDPOINT)
