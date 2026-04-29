"""llama.cpp provider — talks to `llama-server --openai`.

Uses the OpenAI-compatible /v1/chat/completions SSE endpoint via stdlib
urllib. Each SSE line is parsed and yielded as a ChatChunk.
"""
from __future__ import annotations

import json
import urllib.error
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

_DEFAULT_ENDPOINT = "http://127.0.0.1:8080"


class LlamaCppProvider(ProviderBase):
    def __init__(self, *, id: str, endpoint: str) -> None:
        check = validate_provider_host(endpoint)
        if not check.ok:
            raise ProviderError(f"Invalid llama.cpp endpoint: {check.reason}")
        super().__init__(id=id, type="llamacpp")
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.endpoint}/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError):
            return False

    def list_models(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(f"{self.endpoint}/v1/models", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            raise ProviderError(f"llama.cpp /v1/models failed: {exc}") from exc
        items = data.get("data", []) if isinstance(data, dict) else []
        return [{"id": item.get("id", "")} for item in items if isinstance(item, dict)]

    def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,  # noqa: ARG002 - unused; llama-server ignores at request time
        json_mode: bool = False,
        should_cancel: "Callable[[], bool] | None" = None,  # noqa: ARG002 - signature parity; llama-server cancellation not wired
    ) -> Iterator[ChatChunk]:
        from typing import Callable  # noqa: F401 — local import for annotation
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
            f"{self.endpoint}/v1/chat/completions",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600.0) as resp:
                for raw in resp:
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
                        yield ChatChunk(
                            content="",
                            done=True,
                            prompt_tokens=usage.get("prompt_tokens"),
                            completion_tokens=usage.get("completion_tokens"),
                            raw=obj,
                        )
                        return
        except urllib.error.URLError as exc:
            raise ProviderError(f"llama.cpp /v1/chat/completions failed: {exc}") from exc

        yield ChatChunk(content="", done=True)


@provider_factory("llamacpp")
def _llamacpp_factory(p: ProviderConfig) -> ProviderBase:
    return LlamaCppProvider(id=p.id, endpoint=p.endpoint or _DEFAULT_ENDPOINT)
