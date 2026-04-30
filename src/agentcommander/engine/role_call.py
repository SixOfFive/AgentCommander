"""call_role — single entry point for invoking any of the 19 agent roles.

Picks the user's assigned (provider, model) for the role, injects the
role's system prompt, streams tokens, records token usage, returns the
final text.

For the orchestrator (the role that drives action selection), the live
tool registry is appended to its system prompt so it can answer "what
tools do you have access to?" accurately even as tools are added or
removed at runtime.

Mirrors EC's callRole signature so the guard families port with minimal
adaptation.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from agentcommander.agents import get_agent, get_role_prompt
from agentcommander.agents.manifest import OutputContract
from agentcommander.db.repos import audit, insert_token_usage
from agentcommander.engine.role_resolver import resolve as resolve_role
from agentcommander.providers.base import ChatMessage, ProviderError, resolve
from agentcommander.types import Role


class RoleNotAssigned(RuntimeError):
    """Raised when a role has no provider/model bound. Engine catches this
    and either skips (optional roles) or aborts (required roles)."""


def tool_registry_appendix() -> str:
    """Return a markdown section listing currently registered tools.

    Appended to the orchestrator's and chat-fallback's system prompts so
    the model knows what tools are actually wired up at this moment. Lists
    tool name, privileged flag, and one-line description — enough for the
    model to answer "what tools do you have access to?" honestly.

    Returns ``""`` when no tools are registered (e.g. before bootstrap)
    so the appendix doesn't pollute the prompt with an empty section.
    """
    try:
        from agentcommander.tools.dispatcher import list_tools
        tools = list_tools()
    except Exception:  # noqa: BLE001
        return ""
    if not tools:
        return ""
    lines = [
        "",
        "## Currently registered tools (live)",
        "",
        "Action verbs the orchestrator can dispatch *right now*. Use this list",
        "when the user asks what you can do — these are your actual capabilities.",
        "",
    ]
    for t in tools:
        priv = " [privileged]" if t.privileged else ""
        # Trim very long descriptions so a single tool can't dominate the prompt.
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        lines.append(f"- **{t.name}**{priv}: {desc}")
    return "\n".join(lines)


def call_role(role: Role | str, *, user_input: str, scratchpad_text: str = "",
              conversation_id: str | None = None,
              json_mode: bool | None = None,
              num_ctx: int | None = None,
              on_delta: Callable[[str], None] | None = None,
              on_finish: Callable[[int | None, int | None], None] | None = None,
              should_cancel: Optional[Callable[[], bool]] = None) -> str:
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

    # Self-introspection: append the live tool registry to *every* role's
    # system prompt so a "what tools do you have access to?" question can
    # be answered honestly even when the orchestrator delegates the response
    # to a specialist role (e.g. researcher) instead of answering directly.
    # Without this, delegated roles invent fictional tools because their
    # static prompts have no awareness of the program's actual capabilities.
    appendix = tool_registry_appendix()
    if appendix:
        system_prompt = system_prompt.rstrip() + "\n" + appendix + "\n"

    # Extra directive for the orchestrator: don't delegate self-introspection.
    # The list above is authoritative and current — research-style decomposition
    # produces meandering hallucinated answers about NLP / information retrieval
    # / knowledge synthesis that have nothing to do with the registered tools.
    if role_enum is Role.ORCHESTRATOR:
        system_prompt = system_prompt.rstrip() + "\n\n" + (
            "## Self-introspection rule\n\n"
            "When the user asks about YOUR tools, capabilities, or what you "
            "can do, answer DIRECTLY with `{\"action\": \"done\", \"input\": "
            "\"<short bullet list of the registered tools above>\"}`. Do NOT "
            "delegate to research / plan / architect / coder. The tool list "
            "in the section above is authoritative and current — the user "
            "wants that exact list, not a research report about it.\n"
        )

    # If the caller didn't pin a context size, fall back to whatever was
    # persisted on the role assignment (set by `/autoconfig --mincontext N`).
    # That ensures the configured num_ctx actually reaches the provider
    # instead of the runtime defaulting silently.
    if num_ctx is None:
        num_ctx = resolved.context_window_tokens

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
            should_cancel=should_cancel,
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

    if on_finish is not None:
        try:
            on_finish(prompt_tokens, completion_tokens)
        except Exception:  # noqa: BLE001
            pass

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

    # Update running-average tokens/second for this model. Per spec:
    #   new_avg = (old_avg + completion_tokens/duration_seconds) / 2
    # The repo helper handles the missing-row case (seeds with the 100 t/s
    # default) and skips silently when inputs don't produce a meaningful
    # measurement (zero tokens or zero duration).
    try:
        from agentcommander.db.repos import record_throughput
        record_throughput(model, completion_tokens, duration_ms)
    except Exception:  # noqa: BLE001
        pass

    return "".join(collected)
