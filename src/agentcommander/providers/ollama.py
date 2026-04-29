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
from typing import Any, Iterator

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


def _post_stream(url: str, body: dict[str, Any], timeout: float = 600.0) -> Iterator[dict[str, Any]]:
    """POST a JSON body and yield each newline-delimited JSON object from the stream."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
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
            for chunk in _post_stream(url, body):
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


@provider_factory("ollama")
def _ollama_factory(p: ProviderConfig) -> ProviderBase:
    return OllamaProvider(id=p.id, endpoint=p.endpoint or _DEFAULT_ENDPOINT)
