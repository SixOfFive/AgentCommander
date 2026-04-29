"""call_role — single entry point for invoking any of the 19 agent roles.

Picks the user's assigned (provider, model) for the role, injects the
role's system prompt, streams tokens, records token usage, returns the
final text.

Mirrors EC's callRole signature so the guard families port with minimal
adaptation.
"""
from __future__ import annotations

import time
from typing import Callable

from agentcommander.agents import get_agent, get_role_prompt
from agentcommander.agents.manifest import OutputContract
from agentcommander.db.repos import audit, insert_token_usage
from agentcommander.engine.role_resolver import resolve as resolve_role
from agentcommander.providers.base import ChatMessage, ProviderError, resolve
from agentcommander.types import Role


class RoleNotAssigned(RuntimeError):
    """Raised when a role has no provider/model bound. Engine catches this
    and either skips (optional roles) or aborts (required roles)."""


def call_role(role: Role | str, *, user_input: str, scratchpad_text: str = "",
              conversation_id: str | None = None,
              json_mode: bool | None = None,
              num_ctx: int | None = None,
              on_delta: Callable[[str], None] | None = None,
              on_finish: Callable[[int | None, int | None], None] | None = None) -> str:
    """Invoke the given role through its assigned provider+model.

    Returns the full assistant-content string. Streams deltas through
    `on_delta` if supplied (for live UI rendering).

    Raises:
        RoleNotAssigned: if the role has no provider/model in the DB.
        ProviderError: if the provider transport fails.
    """
    role_enum = Role(role) if isinstance(role, str) else role
    agent = get_agent(role_enum)

    resolved = resolve_role(role_enum)
    if resolved is None:
        raise RoleNotAssigned(
            f'Role "{role_enum.value}" is not assigned to a provider/model. '
            f"Configure via the CLI: /providers add ... && /roles set {role_enum.value} ..."
        )

    provider = resolve(resolved.provider_id)
    model = resolved.model
    system_prompt = get_role_prompt(role_enum)

    messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    if scratchpad_text:
        messages.append(ChatMessage(role="user", content=scratchpad_text))
    messages.append(ChatMessage(role="user", content=user_input))

    if json_mode is None:
        json_mode = agent.output_contract is OutputContract.JSON_STRICT

    started = time.time()
    collected: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    try:
        for chunk in provider.chat(
            model=model,
            messages=messages,
            temperature=agent.default_temperature,
            max_tokens=agent.default_max_tokens,
            num_ctx=num_ctx,
            json_mode=json_mode,
        ):
            if chunk.content:
                collected.append(chunk.content)
                if on_delta:
                    on_delta(chunk.content)
            if chunk.done:
                prompt_tokens = chunk.prompt_tokens
                completion_tokens = chunk.completion_tokens
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

    duration_ms = int((time.time() - started) * 1000)
    audit("role.call", {
        "role": role_enum.value,
        "model": model,
        "kind": resolved.kind,
        "duration_ms": duration_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    })
    try:
        insert_token_usage(
            conversation_id=conversation_id,
            role=role_enum.value,
            provider_id=resolved.provider_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001
        pass

    return "".join(collected)
