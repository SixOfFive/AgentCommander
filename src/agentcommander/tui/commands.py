"""Slash-command registry for the TUI.

Each command lives in its own function. `COMMANDS` is the single registry
the REPL consults. Adding a new command = adding one entry here (modular).

  /help                  list commands
  /quit, /exit           exit
  /clear                 clear screen
  /workdir <path>        set the working directory
  /providers             list configured providers
  /providers add <id> <type> <name> <endpoint>   add a provider
  /providers test <id>   test reachability
  /providers rm <id>     remove
  /models <provider_id>  list installed models for a provider
  /roles                 list role assignments
  /roles set <role> <provider_id> <model>        assign one role
  /roles assign-all <provider_id> <model>        assign every role
  /typecast              show catalog status
  /typecast refresh      force re-fetch
  /typecast autoconfigure  pick & assign best models from catalog
  /agents                list the 19 agents + status
  /tools                 list registered tools
  /history               list recent conversations
  /new <title>           start a new conversation
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agentcommander.tui.ansi import style
from agentcommander.tui.render import render_system_line, render_table


@dataclass
class CommandContext:
    """State the REPL passes to commands so they can mutate the session."""
    state: dict  # mutable; e.g. state['working_dir'], state['conversation_id']


@dataclass
class SlashCommand:
    name: str                              # "/help"
    aliases: tuple[str, ...]
    summary: str                           # one-line help
    handler: Callable[["CommandContext", list[str]], None]
    usage: str = ""                        # canonical usage line, e.g. "/providers add <id> <type> <name> <endpoint>"
    details: str = ""                      # multi-line detailed help (markdown-light)
    examples: tuple[str, ...] = ()


# ─── Built-in commands ─────────────────────────────────────────────────────


def cmd_help(ctx: CommandContext, args: list[str]) -> None:
    if args:
        # Detailed help for a specific command — accept both `/help foo` and `/help /foo`.
        target = args[0]
        if not target.startswith("/"):
            target = "/" + target
        cmd = COMMANDS.get(target)
        if cmd is None:
            render_system_line(f"unknown command: {target}  (try /help)")
            return
        render_system_line(f"{cmd.name}  —  {cmd.summary}")
        if cmd.aliases:
            render_system_line(f"  aliases: {', '.join(cmd.aliases)}")
        if cmd.usage:
            render_system_line("  usage:")
            for ln in cmd.usage.split("\n"):
                render_system_line(f"    {ln}")
        if cmd.details:
            render_system_line("  details:")
            for ln in cmd.details.rstrip().split("\n"):
                render_system_line(f"    {ln}")
        if cmd.examples:
            render_system_line("  examples:")
            for ex in cmd.examples:
                render_system_line(f"    {ex}")
        return

    render_system_line("Available commands:")
    seen: set[int] = set()
    rows: list[list[str]] = []
    for c in COMMANDS.values():
        if id(c) in seen:
            continue
        seen.add(id(c))
        aliases = (" / " + ", ".join(c.aliases)) if c.aliases else ""
        rows.append([c.name + aliases, c.summary])
    render_table(["command", "summary"], rows)
    render_system_line("")
    render_system_line("Use  /help <command>  for detailed help on any of the above.")


def cmd_quit(ctx: CommandContext, _args: list[str]) -> None:
    ctx.state["should_exit"] = True


def cmd_clear(ctx: CommandContext, _args: list[str]) -> None:
    from agentcommander.tui.ansi import CLEAR_SCREEN, write
    write(CLEAR_SCREEN)


def cmd_workdir(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.db.repos import set_config
    if not args:
        wd = ctx.state.get("working_dir")
        render_system_line(f"workdir: {wd or '(not set)'}")
        return
    path = " ".join(args)
    import os
    if not os.path.isdir(path):
        render_system_line(f"not a directory: {path}")
        return
    abs_path = os.path.abspath(path)
    set_config("working_directory", abs_path)
    ctx.state["working_dir"] = abs_path
    render_system_line(f"workdir set: {abs_path}")


def cmd_providers(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.db.repos import (
        delete_provider,
        get_provider,
        list_providers,
        upsert_provider,
    )
    from agentcommander.providers.base import rebuild_from_db, resolve, ProviderError
    from agentcommander.types import ProviderConfig

    if not args:
        rows = [
            [p.id, p.type, p.name, p.endpoint or "—",
             "on" if p.enabled else "off"]
            for p in list_providers()
        ]
        render_table(["id", "type", "name", "endpoint", "enabled"], rows)
        return

    sub = args[0]
    rest = args[1:]
    if sub == "add":
        if len(rest) < 4:
            render_system_line("usage: /providers add <id> <type> <name> <endpoint>")
            return
        pid, ptype, name, endpoint = rest[0], rest[1], rest[2], " ".join(rest[3:])
        upsert_provider(ProviderConfig(id=pid, type=ptype, name=name,  # type: ignore[arg-type]
                                       endpoint=endpoint, enabled=True))
        rebuild_from_db()
        render_system_line(f"added provider: {pid}")
    elif sub == "rm":
        if not rest:
            render_system_line("usage: /providers rm <id>")
            return
        delete_provider(rest[0])
        rebuild_from_db()
        render_system_line(f"removed provider: {rest[0]}")
    elif sub == "test":
        if not rest:
            render_system_line("usage: /providers test <id>")
            return
        pid = rest[0]
        if get_provider(pid) is None:
            render_system_line(f"unknown provider: {pid}")
            return
        try:
            ok = resolve(pid).health()
        except ProviderError as exc:
            render_system_line(f"error: {exc}")
            return
        render_system_line(f"{pid}: {'healthy ✓' if ok else 'unreachable ✗'}")
    else:
        render_system_line(f"unknown sub-command: /providers {sub}")


def cmd_models(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.providers.base import ProviderError, resolve
    if not args:
        render_system_line("usage: /models <provider_id>")
        return
    try:
        models = resolve(args[0]).list_models()
    except ProviderError as exc:
        render_system_line(f"error: {exc}")
        return
    rows = [[m.get("id", ""), m.get("family") or "", m.get("parameter_size") or ""] for m in models]
    render_table(["model", "family", "size"], rows)


def cmd_roles(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.db.repos import list_role_assignments, set_role_assignment
    from agentcommander.types import ALL_ROLES, Role

    if not args:
        existing = {a["role"]: a for a in list_role_assignments()}
        rows: list[list[str]] = []
        for role in ALL_ROLES:
            a = existing.get(role.value)
            if a:
                rows.append([role.value, a["provider_id"], a["model"],
                             "override" if a["is_override"] else "default"])
            else:
                rows.append([role.value, "—", "—", style("warn", "unset")])
        render_table(["role", "provider", "model", "kind"], rows)
        return

    sub = args[0]
    rest = args[1:]
    if sub == "set":
        if len(rest) < 3:
            render_system_line("usage: /roles set <role> <provider_id> <model>")
            return
        role_str, pid, model = rest[0], rest[1], " ".join(rest[2:])
        try:
            Role(role_str)
        except ValueError:
            render_system_line(f"unknown role: {role_str}")
            return
        set_role_assignment(role_str, pid, model, is_override=True)
        render_system_line(f"set {role_str} → {pid} / {model}")
    elif sub == "assign-all":
        if len(rest) < 2:
            render_system_line("usage: /roles assign-all <provider_id> <model>")
            return
        pid, model = rest[0], " ".join(rest[1:])
        for role in ALL_ROLES:
            set_role_assignment(role, pid, model, is_override=False)
        render_system_line(f"assigned {pid}/{model} to all {len(ALL_ROLES)} roles")
    else:
        render_system_line(f"unknown sub-command: /roles {sub}")


def cmd_typecast(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.typecast import (
        get_catalog,
        refresh_catalog,
        suggest_config,
    )
    from agentcommander.providers.base import ProviderError, list_active

    if not args:
        result = get_catalog()
        if result is None:
            render_system_line("catalog: not loaded yet — refreshing now…")
            result = refresh_catalog()
        render_system_line(f"source: {result.source}  ·  models: {result.model_count}")
        if result.remote_error:
            render_system_line(f"last remote error: {result.remote_error}")
        return

    sub = args[0]
    if sub == "refresh":
        result = refresh_catalog()
        render_system_line(f"refreshed from {result.source} — {result.model_count} models")
        if result.remote_error:
            render_system_line(f"(remote error: {result.remote_error})")
    elif sub == "autoconfigure":
        installed: set[str] = set()
        for prov in list_active():
            try:
                for m in prov.list_models():
                    if m.get("id"):
                        installed.add(m["id"])
            except ProviderError:
                continue
        if not installed:
            render_system_line("no installed models found across active providers; "
                               "configure a provider first with /providers add")
            return
        suggestion = suggest_config(installed)
        if suggestion.default_model is None:
            render_system_line("no suitable installed model in the TypeCast catalog "
                               "(installed but unscored, or doesn't fit VRAM)")
            return
        from agentcommander.db.repos import list_providers, set_role_assignment
        from agentcommander.types import ALL_ROLES
        # Pick the provider that has this model; prefer the first hit.
        chosen_provider_id: str | None = None
        for prov in list_active():
            try:
                if any(m.get("id") == suggestion.default_model.model_id
                       for m in prov.list_models()):
                    chosen_provider_id = prov.id
                    break
            except ProviderError:
                continue
        if not chosen_provider_id:
            providers = list_providers()
            chosen_provider_id = providers[0].id if providers else None
        if not chosen_provider_id:
            render_system_line("no provider available")
            return
        for role in ALL_ROLES:
            set_role_assignment(role, chosen_provider_id,
                                suggestion.default_model.model_id, is_override=False)
        for o in suggestion.overrides:
            set_role_assignment(o.role, chosen_provider_id, o.model_id or "", is_override=True)
        render_system_line(
            f"set {suggestion.default_model.model_id} for all {len(ALL_ROLES)} roles "
            f"(+ {len(suggestion.overrides)} per-role overrides)"
        )
    else:
        render_system_line(f"unknown sub-command: /typecast {sub}")


def cmd_agents(ctx: CommandContext, _args: list[str]) -> None:
    from agentcommander.agents import AGENTS, list_available_prompts
    available = list_available_prompts()
    rows = [
        [a.role.value, a.category.value,
         a.output_contract.value, "yes" if available.get(a.role) else "fallback",
         "optional" if a.optional else "required"]
        for a in AGENTS
    ]
    render_table(["role", "category", "output", "prompt", "kind"], rows)


def cmd_tools(ctx: CommandContext, _args: list[str]) -> None:
    from agentcommander.tools import list_tools
    rows = [[t.name, "priv" if t.privileged else "open", t.description[:80]]
            for t in list_tools()]
    render_table(["tool", "priv", "description"], rows)


def cmd_history(ctx: CommandContext, _args: list[str]) -> None:
    from agentcommander.db.repos import list_conversations
    convs = list_conversations()
    if not convs:
        render_system_line("(no conversations yet)")
        return
    rows = [[c.id[:8], c.title[:60]] for c in convs[:20]]
    render_table(["id", "title"], rows)


def cmd_new(ctx: CommandContext, args: list[str]) -> None:
    from agentcommander.db.repos import create_conversation
    title = " ".join(args).strip() or "New conversation"
    conv = create_conversation(title=title,
                                working_directory=ctx.state.get("working_dir"))
    ctx.state["conversation_id"] = conv.id
    render_system_line(f"new conversation: {conv.id[:8]} — {conv.title}")


# ─── Registry ──────────────────────────────────────────────────────────────


def _build_registry() -> dict[str, SlashCommand]:
    cmds = [
        SlashCommand("/help", ("/?",), "list commands", cmd_help),
        SlashCommand("/quit", ("/exit",), "exit AgentCommander", cmd_quit),
        SlashCommand("/clear", (), "clear the screen", cmd_clear),
        SlashCommand("/workdir", ("/wd",), "set or show the working directory", cmd_workdir),
        SlashCommand("/providers", ("/p",), "manage providers (add/rm/test/list)", cmd_providers),
        SlashCommand("/models", ("/m",), "list installed models on a provider", cmd_models),
        SlashCommand("/roles", ("/r",), "manage role → model assignments", cmd_roles),
        SlashCommand("/typecast", ("/tc",), "TypeCast catalog (status/refresh/autoconfigure)", cmd_typecast),
        SlashCommand("/agents", (), "list the 19 agents + prompt status", cmd_agents),
        SlashCommand("/tools", (), "list registered tools", cmd_tools),
        SlashCommand("/history", (), "recent conversations", cmd_history),
        SlashCommand("/new", (), "start a new conversation: /new <title>", cmd_new),
    ]
    out: dict[str, SlashCommand] = {}
    for c in cmds:
        out[c.name] = c
        for alias in c.aliases:
            out[alias] = c
    return out


COMMANDS: dict[str, SlashCommand] = _build_registry()
