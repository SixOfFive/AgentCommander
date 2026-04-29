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
        if num_ctx is not None:
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
            for chunk in _post_stream(url, body, should_cancel=should_cancel):
                # Cancellation check after each chunk — closes the socket
                # via the _post_stream context manager so the daemon stops
                # generating tokens for this request.
                if should_cancel is not None and should_cancel():
                    return
                content = ""
                msg = chunk.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content", "") or ""
                done = bool(chunk.get("done", False))
                if done:
                    yield ChatChunk(
                        content=content,
                        done=True,
                        prompt_tokens=chunk.get("prompt_eval_count"),
                        completion_tokens=chunk.get("eval_count"),
                        raw=chunk,
                    )
                    return
                if content:
                    yield ChatChunk(content=content, done=False, raw=chunk)
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
        try:
            data = _get_json(f"{self.endpoint}/api/ps", timeout=3.0)
        except (urllib.error.URLError, OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        models = data.get("models", []) or []
        out: list[str] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("model")
            if isinstance(name, str) and name:
                out.append(name)
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
