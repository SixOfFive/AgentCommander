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
    from agentcommander.db.connection import get_db
    from agentcommander.db.repos import (
        audit,
        get_role_assignment,
        set_role_assignment,
    )
    from agentcommander.engine.role_resolver import (
        autoconfig_table,
        resolve as resolve_role,
        set_autoconfig,
    )
    from agentcommander.providers.base import list_active
    from agentcommander.typecast import apply_autoconfigure
    from agentcommander.types import ALL_ROLES, Role

    def _print_all() -> None:
        rows: list[list[str]] = []
        for role in ALL_ROLES:
            rr = resolve_role(role)
            if rr is None:
                rows.append([role.value, "—", "—", style("warn", "unset")])
            else:
                rows.append([role.value, rr.model, rr.provider_id, rr.kind])
        render_table(["role", "model", "provider", "kind"], rows)

    def _try_role(role_str: str) -> Role | None:
        try:
            return Role(role_str)
        except ValueError:
            render_system_line(f'unknown role: "{role_str}"  (try /agents)')
            return None

    if not args:
        _print_all()
        return

    head = args[0]

    # Single-role display: `/roles <role>`
    try:
        role = Role(head)
    except ValueError:
        role = None
    if role is not None and len(args) == 1:
        rr = resolve_role(role)
        if rr is None:
            render_system_line(f'{role.value}: ' + style("warn", "unset"))
            render_system_line(f"  bind one with: /roles set {role.value} <provider_id> <model>")
            return
        kind_label = ("override (user-set, persisted)" if rr.kind == "override"
                      else "auto (in-memory, recomputed each launch)")
        render_system_line(f'{style("role_label", role.value)}')
        render_system_line(f"  provider: {rr.provider_id}")
        render_system_line(f"  model:    {rr.model}")
        render_system_line(f"  kind:     {kind_label}")
        return

    sub = head
    rest = args[1:]

    if sub == "set":
        if len(rest) < 3:
            render_system_line("usage: /roles set <role> <provider_id> <model>")
            return
        role = _try_role(rest[0])
        if role is None:
            return
        pid = rest[1]
        model = " ".join(rest[2:])
        # Every /roles set is persisted as an override.
        set_role_assignment(role, pid, model, is_override=True)
        render_system_line(f"set {role.value} → {pid} / {model}  "
                           + style("muted", "(override; persisted; beats autoconfig)"))
        return

    if sub == "unset":
        if not rest:
            render_system_line("usage: /roles unset <role>")
            return
        role = _try_role(rest[0])
        if role is None:
            return
        existing = get_role_assignment(role)
        if existing is None:
            render_system_line(f"{role.value}: no override to unset")
            return
        get_db().execute("DELETE FROM role_assignments WHERE role = ?", (role.value,))
        audit("roles.unset", {"role": role.value, "previous_model": existing["model"]})
        # Re-resolve to show what autoconfig now provides (if anything).
        rr = resolve_role(role)
        if rr is not None:
            render_system_line(
                f"unset {role.value} override → now using {style('accent', rr.model)} "
                f"({rr.kind})"
            )
        else:
            render_system_line(f"unset {role.value}  "
                               + style("muted", "(no autoconfig pick available — run /roles auto)"))
        return

    if sub == "auto":
        providers = list_active()
        if not providers:
            render_system_line("no active providers — add one with /providers add")
            return
        applied = apply_autoconfigure(
            providers=providers,
            get_role_assignment_fn=get_role_assignment,
            audit_fn=audit,
        )
        if applied.skipped_reason:
            render_system_line(f"autoconfigure skipped: {applied.skipped_reason}")
            set_autoconfig({})
            return
        # Update the in-memory resolver map — never persisted.
        in_memory: dict[Role, tuple[str, str]] = {}
        for role_value, (pid, model) in applied.role_picks.items():
            try:
                in_memory[Role(role_value)] = (pid, model)
            except ValueError:
                continue
        set_autoconfig(in_memory)
        render_system_line(
            f"autoconfigured {len(applied.role_picks)} role(s) in memory "
            f'→ default {style("accent", applied.default_model or "?")} '
            f"on {applied.provider_id}"
        )
        if applied.diff_picks:
            render_system_line(f"  + {len(applied.diff_picks)} stronger pick(s):")
            for r, m in applied.diff_picks.items():
                render_system_line(f"    {r} → {m}")
        if applied.user_overrides:
            render_system_line(
                f"  preserved {len(applied.user_overrides)} user override(s) — "
                "use /roles unset <role> to release"
            )
        return

    if sub == "assign-all":
        if len(rest) < 2:
            render_system_line("usage: /roles assign-all <provider_id> <model>")
            return
        pid, model = rest[0], " ".join(rest[1:])
        # Treat assign-all as a bulk override (every role becomes user-set).
        for role in ALL_ROLES:
            set_role_assignment(role, pid, model, is_override=True)
        render_system_line(f"assigned {pid}/{model} as override on all {len(ALL_ROLES)} roles")
        return

    # Defensive: silence unused-symbol warning for the imported helper.
    _ = autoconfig_table
    render_system_line(f"unknown sub-command: /roles {sub}  (try /help roles)")


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
        # Dispatch to /roles auto so there's exactly one autoconfig path.
        cmd_roles(ctx, ["auto"])
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
        SlashCommand(
            name="/help", aliases=("/?",),
            summary="list commands or show detailed help for one",
            handler=cmd_help,
            usage="/help            # list every command\n"
                  "/help <command>  # detailed help for a specific command",
            examples=("/help", "/help providers", "/help /typecast"),
        ),
        SlashCommand(
            name="/quit", aliases=("/exit",),
            summary="exit AgentCommander",
            handler=cmd_quit,
            usage="/quit",
            details="Closes the SQLite connection and returns to the shell. "
                    "Ctrl-D / EOF behaves the same. Ctrl-C aborts an in-progress "
                    "pipeline run but does not exit the TUI.",
        ),
        SlashCommand(
            name="/clear", aliases=(),
            summary="clear the screen",
            handler=cmd_clear,
            usage="/clear",
            details="ANSI screen-clear; conversation history is preserved in SQLite.",
        ),
        SlashCommand(
            name="/workdir", aliases=("/wd",),
            summary="set or show the working directory",
            handler=cmd_workdir,
            usage="/workdir              # show current\n"
                  "/workdir <path>       # set to <path>",
            details="The working directory is the sandbox boundary for every "
                    "filesystem and execute action. Without it, file/code tools "
                    "refuse to run. Path is persisted across sessions.",
            examples=("/workdir ~/code/scratch", "/workdir D:\\projects\\demo"),
        ),
        SlashCommand(
            name="/providers", aliases=("/p",),
            summary="manage LLM providers",
            handler=cmd_providers,
            usage="/providers                                       # list\n"
                  "/providers add <id> <type> <name> <endpoint>     # add\n"
                  "/providers test <id>                             # health check\n"
                  "/providers rm <id>                               # remove",
            details="Supported types: ollama, llamacpp.\n"
                    "Endpoints + ids are stored in the SQLite DB at "
                    "$XDG_DATA_HOME/agentcommander/agentcommander.sqlite (gitignored). "
                    "After add/rm, the in-memory provider registry is rebuilt automatically.",
            examples=(
                "/providers add ollama-local ollama \"Local Ollama\" http://127.0.0.1:11434",
                "/providers add llama llamacpp \"llama-server\" http://127.0.0.1:8080",
                "/providers test ollama-local",
            ),
        ),
        SlashCommand(
            name="/models", aliases=("/m",),
            summary="list installed models on a provider",
            handler=cmd_models,
            usage="/models <provider_id>",
            details="Hits the provider's listing endpoint (Ollama: /api/tags, "
                    "llama.cpp: /v1/models). Used by /typecast autoconfigure to "
                    "filter the TypeCast catalog to what's actually installed.",
            examples=("/models ollama-local",),
        ),
        SlashCommand(
            name="/roles", aliases=("/r",),
            summary="manage role → (provider, model) assignments",
            handler=cmd_roles,
            usage="/roles                                           # table of all assignments\n"
                  "/roles <role>                                    # show one role's assignment\n"
                  "/roles set <role> <provider_id> <model>          # set a per-role override\n"
                  "/roles unset <role>                              # remove the override\n"
                  "/roles auto                                      # re-run TypeCast autoconfig\n"
                  "/roles assign-all <provider_id> <model>          # assign all 19 roles flat",
            details="The 19 agent roles are listed by /agents.\n"
                    "Two kinds of assignments live in the DB:\n"
                    "  · 'auto'      — picked by TypeCast (best-fit per role)\n"
                    "  · 'override'  — set by you with /roles set; never overwritten by /roles auto.\n"
                    "Workflow: at startup the program calls TypeCast to pick a default\n"
                    "model + per-role stronger picks. You can override any role with\n"
                    "/roles set <role> ... and the override survives subsequent /roles auto runs.\n"
                    "Use /roles unset <role> to clear an override and let TypeCast re-pick.",
            examples=(
                "/roles                              # show all 19 roles",
                "/roles coder                        # show just the coder binding",
                "/roles set coder ollama-default qwen3-coder:30b",
                "/roles unset coder",
                "/roles auto                         # re-run autoconfig (respects overrides)",
                "/roles assign-all ollama-default qwen3:8b",
            ),
        ),
        SlashCommand(
            name="/typecast", aliases=("/tc",),
            summary="TypeCast catalog: status / refresh / autoconfigure",
            handler=cmd_typecast,
            usage="/typecast                  # show source + model count\n"
                  "/typecast refresh          # force re-fetch from GitHub\n"
                  "/typecast autoconfigure    # pick best installed model + per-role overrides",
            details="The TypeCast catalog (github.com/SixOfFive/TypeCast) scores "
                    "every published local model on each of the 17 roles in the "
                    "TypeCast schema. AgentCommander pulls a fresh copy every "
                    "startup and falls back to the local cache on network failure. "
                    "autoconfigure picks one well-rounded default model that fits "
                    "your VRAM, then suggests per-role overrides where another "
                    "model meaningfully outscores it.",
            examples=("/typecast", "/typecast autoconfigure"),
        ),
        SlashCommand(
            name="/agents", aliases=(),
            summary="list the 19 agents + prompt status",
            handler=cmd_agents,
            usage="/agents",
            details="Shows every role's category (router/controller/producer/media/meta), "
                    "output contract (freeform vs strict JSON), whether a system-prompt "
                    "markdown file exists in resources/prompts/, and whether the role "
                    "is required or optional for the pipeline to run.",
        ),
        SlashCommand(
            name="/tools", aliases=(),
            summary="list registered tools",
            handler=cmd_tools,
            usage="/tools",
            details="Lists every action verb the orchestrator can dispatch — "
                    "read_file, write_file, execute, fetch, etc. \"priv\" means the "
                    "tool requires extra safety checks (the EC admin-gated set).",
        ),
        SlashCommand(
            name="/history", aliases=(),
            summary="show recent conversations",
            handler=cmd_history,
            usage="/history",
            details="Top 20 most-recent conversations from SQLite. Stored in "
                    "$XDG_DATA_HOME/agentcommander/agentcommander.sqlite.",
        ),
        SlashCommand(
            name="/new", aliases=(),
            summary="start a new conversation",
            handler=cmd_new,
            usage="/new [<title>]",
            details="Creates a new conversation row and switches the active context "
                    "to it. The first message you send afterward is logged under the "
                    "new conversation. Title is optional; defaults to \"New conversation\".",
            examples=("/new", "/new Bug repro for fetch timeout"),
        ),
    ]
    out: dict[str, SlashCommand] = {}
    for c in cmds:
        out[c.name] = c
        for alias in c.aliases:
            out[alias] = c
    return out


COMMANDS: dict[str, SlashCommand] = _build_registry()
