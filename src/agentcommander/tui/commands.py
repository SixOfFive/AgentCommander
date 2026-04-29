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


def cmd_stop(ctx: CommandContext, _args: list[str]) -> None:
    cancel = ctx.state.get("active_cancel")
    if cancel is None:
        render_system_line("(no active pipeline run)")
        return
    try:
        cancel.set()
    except AttributeError:
        render_system_line("(cancel signal not available)")
        return
    render_system_line(style("warn", "halting the active pipeline…"))


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


def _parse_token_count(s: str) -> int | None:
    """Parse strings like '128k', '32K', '4096', '1.5m' into an integer
    token count. Suffix 'k' multiplies by 1024, 'm' by 1024*1024 (binary
    convention — matches the TypeCast catalog's contextLength values).
    Returns None when the input can't be parsed or is not strictly positive.
    """
    if not s:
        return None
    raw = s.strip().lower()
    mult = 1
    if raw.endswith("k"):
        mult = 1024
        raw = raw[:-1]
    elif raw.endswith("m"):
        mult = 1024 * 1024
        raw = raw[:-1]
    try:
        n = int(float(raw) * mult)
    except ValueError:
        return None
    return n if n > 0 else None


def _humanize_tokens_short(n: int | None) -> str:
    """Compact display: 4096 → '4096', 32768 → '32k', 131072 → '128k'."""
    if n is None or n <= 0:
        return "?"
    if n < 1024:
        return str(n)
    if n < 1024 * 1024:
        v = n / 1024
        return f"{v:.0f}k" if abs(v - round(v)) < 0.05 else f"{v:.1f}k"
    v = n / (1024 * 1024)
    return f"{v:.0f}m" if abs(v - round(v)) < 0.05 else f"{v:.1f}m"


def cmd_context(ctx: CommandContext, args: list[str]) -> None:
    """`/context` — show, set, or clear the session-wide num_ctx override.

    Set with ``/context 32k`` (suffix k = 1024 tokens, m = 1024². Raw integers
    also accepted). Clear with ``/context off`` / ``/context clear``.

    The override beats per-role ``context_window_tokens`` and is sent to every
    provider call as ``num_ctx`` for the rest of the session. If the chosen
    value exceeds any picked model's catalog ``contextLength``, every offender
    is listed as a warning, but the override is applied anyway — the user is
    in charge.
    """
    from agentcommander.db.repos import audit, get_config, set_config
    from agentcommander.engine.role_resolver import (
        SESSION_CONTEXT_OVERRIDE_KEY,
        resolve as resolve_role,
    )
    from agentcommander.tui.status_bar import get_status_bar
    from agentcommander.types import ALL_ROLES
    from agentcommander.typecast.catalog import get_catalog

    bar = get_status_bar()

    if not args:
        current = get_config(SESSION_CONTEXT_OVERRIDE_KEY, None)
        if current is None:
            render_system_line("session context override: " + style("muted", "(unset)"))
            render_system_line(style("muted",
                "  set with /context <N>  (e.g. /context 32k or /context 65536)"))
            return
        try:
            n = int(current)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            render_system_line(
                f"session context override: {style('accent', _humanize_tokens_short(n))} "
                + style("muted", f"({n} tokens)")
            )
            render_system_line(style("muted", "  clear with /context off"))
        else:
            render_system_line("session context override: " + style("muted", "(unset)"))
        return

    head = args[0].lower()
    if head in ("off", "clear", "none", "unset"):
        existing = get_config(SESSION_CONTEXT_OVERRIDE_KEY, None)
        # Best path here is just to remove the row entirely so /context with
        # no args reports it as unset.
        from agentcommander.db.connection import get_db
        get_db().execute(
            "DELETE FROM config WHERE key = ?",
            (SESSION_CONTEXT_OVERRIDE_KEY,),
        )
        audit("context.clear", {"previous": existing})
        # Clear the bar's cap directly — set_context(cap_min=...) only writes
        # truthy values, so we go through state for the explicit None.
        bar.state.context_cap_min = None
        bar.redraw()
        render_system_line("cleared session context override " + style("muted",
            "(roles fall back to per-role context_window_tokens / provider default)"))
        return

    parsed = _parse_token_count(args[0])
    if parsed is None:
        render_system_line(f'could not parse: "{args[0]}"')
        render_system_line(style("muted",
            "  usage: /context <N>   (32k, 128K, 65536, 1.5m, …)"))
        render_system_line(style("muted",
            "         /context off   (clear the override)"))
        return

    # Persist the override
    set_config(SESSION_CONTEXT_OVERRIDE_KEY, parsed)
    audit("context.set", {"tokens": parsed})

    # Surface offenders: roles whose model's catalog contextLength is below
    # the override. We use the live resolver so /roles set + /autoconfig
    # picks are both reflected.
    offenders: list[tuple[str, str, int]] = []
    catalog_result = get_catalog()
    cat = catalog_result.catalog if catalog_result else {}
    seen: set[tuple[str, str]] = set()
    for role in ALL_ROLES:
        rr = resolve_role(role)
        if rr is None:
            continue
        key = (role.value, rr.model)
        if key in seen:
            continue
        seen.add(key)
        entry = cat.get(rr.model) if isinstance(cat, dict) else None
        if not isinstance(entry, dict):
            continue
        raw = entry.get("contextLength")
        if not isinstance(raw, (int, float)) or raw <= 0:
            continue
        if parsed > int(raw):
            offenders.append((role.value, rr.model, int(raw)))

    # Update the bar's displayed cap immediately. The next role call will
    # also pick up the new override via resolve_role.
    bar.set_context(cap_min=parsed)

    render_system_line(
        f"session context override: {style('accent', _humanize_tokens_short(parsed))} "
        + style("muted", f"({parsed} tokens)")
    )

    if offenders:
        render_system_line(style("warn",
            f"  WARNING: {len(offenders)} role(s) use a model with a "
            "smaller training context than the override:"))
        for role_name, model, model_ctx in offenders:
            render_system_line(style("warn",
                f"    {role_name} → {model}  "
                f"(training {_humanize_tokens_short(model_ctx)} < {_humanize_tokens_short(parsed)})"))
        render_system_line(style("muted",
            "  override applied anyway; the model may truncate or refuse "
            "prompts that exceed its training window."))


def cmd_autoconfig(ctx: CommandContext, args: list[str]) -> None:
    """`/autoconfig [--mincontext N] | /autoconfig clear`.

    With no flags, runs the in-memory autoconfigure (same as `/roles auto`).

    With `--mincontext N`, narrows the candidate set to installed models
    whose TypeCast `contextLength` is at least N tokens, then persists the
    picks to `role_assignments` (with `context_window_tokens = N`) so they
    survive restarts and the chosen num_ctx flows to every model call.
    Prior autoconfig-persisted rows are wiped first so a higher / lower
    threshold can re-pick; rows set by `/roles set` (no context) are kept.

    With `clear`, deletes every row in `role_assignments` and then runs
    the default in-memory autoconfigure.
    """
    from agentcommander.db.connection import get_db
    from agentcommander.db.repos import (
        audit,
        clear_role_assignments,
        get_role_assignment,
        list_providers,
        set_role_assignment,
        upsert_provider,
    )
    from agentcommander.engine.role_resolver import set_autoconfig
    from agentcommander.providers.base import list_active, rebuild_from_db
    from agentcommander.tui.setup import (
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_NAME,
        prompt_for_ollama_endpoint,
    )
    from agentcommander.typecast import apply_autoconfigure
    from agentcommander.typecast.autoconfig import (
        get_banned_models,
        set_banned_models,
    )
    from agentcommander.types import ProviderConfig, Role

    # Subcommand: bans → list the banned set
    if args and args[0] == "bans":
        banned = sorted(get_banned_models())
        if not banned:
            render_system_line("autoconfig ban list: " + style("muted", "(empty)"))
            render_system_line(style("muted",
                "  add with /autoconfig ban <model_id>"))
        else:
            render_system_line(f"autoconfig ban list ({len(banned)} model(s)):")
            for m in banned:
                render_system_line(f"  - {m}")
            render_system_line(style("muted",
                "  remove with /autoconfig unban <model_id>"))
        return

    # Subcommand: ban → add a model, then re-run autoconfigure so the
    # banned model drops out of role assignments immediately.
    if args and args[0] == "ban":
        if len(args) < 2:
            render_system_line(
                "usage: /autoconfig ban <model_id>   "
                "(banned models are excluded from autoconfig picks)"
            )
            return
        target = args[1].strip()
        if not target:
            render_system_line("model_id is empty")
            return
        banned = get_banned_models()
        if target in banned:
            render_system_line(f"already banned: {target}")
            return
        banned.add(target)
        set_banned_models(banned)
        audit("autoconfig.ban", {"model": target, "size": len(banned)})
        render_system_line(
            f"banned {style('accent', target)} from autoconfigure  "
            + style("muted", f"({len(banned)} model(s) now banned)")
        )
        # Fall through to autoconfigure path so the user sees the new picks
        # without having to type a second command.
        args = []  # noqa: F841 — intentionally drop into default flow

    # Subcommand: unban → remove a model, then re-run autoconfigure
    if args and args[0] == "unban":
        if len(args) < 2:
            render_system_line("usage: /autoconfig unban <model_id>")
            return
        target = args[1].strip()
        banned = get_banned_models()
        if target not in banned:
            render_system_line(f"not in ban list: {target}")
            return
        banned.discard(target)
        set_banned_models(banned)
        audit("autoconfig.unban", {"model": target, "size": len(banned)})
        render_system_line(
            f"unbanned {style('accent', target)}  "
            + style("muted", f"({len(banned)} model(s) still banned)")
        )
        args = []  # noqa: F841 — fall through to default autoconfigure

    # Subcommand: clear → wipe DB role assignments, re-prompt for the Ollama
    # endpoint (rescanning against the new server if it changed), then fall
    # through to default in-memory autoconfigure.
    if args and args[0] == "clear":
        removed = clear_role_assignments()
        audit("autoconfig.clear", {"removed_rows": removed})
        render_system_line(style("warn",
            f"cleared {removed} persisted role assignment(s) from the DB"))

        # Find the existing Ollama provider so we can show its endpoint as
        # the default and only upsert when the user actually changes it.
        all_providers = list_providers()
        ollama_provider = next(
            (p for p in all_providers if p.type == "ollama"), None
        )
        current_endpoint = ollama_provider.endpoint if ollama_provider else None

        new_endpoint = prompt_for_ollama_endpoint(default=current_endpoint)
        if new_endpoint is None:
            render_system_line(style("warn",
                "endpoint prompt cancelled — leaving server unchanged; "
                "running autoconfigure against the existing provider"))
        elif new_endpoint != current_endpoint:
            cfg = ProviderConfig(
                id=ollama_provider.id if ollama_provider else DEFAULT_PROVIDER_ID,
                type="ollama",
                name=ollama_provider.name if ollama_provider else DEFAULT_PROVIDER_NAME,
                endpoint=new_endpoint,
                api_key=ollama_provider.api_key if ollama_provider else None,
                enabled=True,
            )
            upsert_provider(cfg)
            rebuild_from_db()
            audit("autoconfig.endpoint_change", {
                "provider_id": cfg.id,
                "from": current_endpoint,
                "to": new_endpoint,
            })
            render_system_line(style("muted",
                f"  endpoint updated: {current_endpoint or '(none)'}  →  {new_endpoint}"))
            render_system_line(style("muted",
                "  rescanning installed models against the new server…"))
        else:
            render_system_line(style("muted",
                f"  endpoint unchanged ({current_endpoint})"))

        args = args[1:]

    # Parse --mincontext N
    min_context = 0
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--mincontext", "--min-context"):
            if i + 1 >= len(args):
                render_system_line(
                    "usage: /autoconfig --mincontext <N>   "
                    "(e.g. 128k, 32K, or a raw token count like 32768)"
                )
                return
            parsed = _parse_token_count(args[i + 1])
            if parsed is None:
                render_system_line(
                    f'could not parse mincontext value: "{args[i + 1]}"  '
                    "(try 128k, 32K, or a raw token count)"
                )
                return
            min_context = parsed
            i += 2
            continue
        render_system_line(f"unknown argument to /autoconfig: {a}")
        render_system_line(
            "usage: /autoconfig [--mincontext <N>]   |   /autoconfig clear"
        )
        return

    providers = list_active()
    if not providers:
        render_system_line("no active providers — add one with /providers add")
        return

    # When persisting with a new threshold, drop any rows the previous
    # autoconfig persisted (they carry a non-null context_window_tokens) so
    # this run can re-pick them. Rows from /roles set keep that column NULL
    # and survive untouched.
    if min_context > 0:
        get_db().execute(
            "DELETE FROM role_assignments WHERE context_window_tokens IS NOT NULL"
        )

    applied = apply_autoconfigure(
        providers=providers,
        get_role_assignment_fn=get_role_assignment,
        audit_fn=audit,
        min_context=min_context,
    )

    if applied.skipped_reason:
        render_system_line(f"autoconfigure skipped: {applied.skipped_reason}")
        set_autoconfig({})
        return

    if not applied.role_picks:
        msg = "autoconfigure picked nothing"
        if min_context > 0:
            msg += (f" — no installed model has contextLength "
                    f">= {min_context} tokens")
        render_system_line(style("warn", msg))
        if applied.unset_roles:
            render_system_line(style("muted",
                f"  unset roles: {', '.join(applied.unset_roles)}"))
        return

    # Persist when --mincontext was given. The chosen num_ctx is stored
    # alongside each (role, provider, model) so call_role can pass it as
    # `options.num_ctx` on every Ollama request — no silent default fallback.
    persisted = 0
    if min_context > 0:
        for role_value, (pid, model) in applied.role_picks.items():
            try:
                role_enum = Role(role_value)
            except ValueError:
                continue
            set_role_assignment(
                role_enum, pid, model,
                is_override=True,
                context_window_tokens=min_context,
            )
            persisted += 1
        audit("autoconfig.persist", {
            "min_context": min_context,
            "role_count": persisted,
            "default_model": applied.default_model,
        })

    # Always update the in-memory autoconfig — the persisted DB rows already
    # win at resolve time, but populating in-memory keeps `/roles` output
    # consistent (showing kind="auto" for non-persisted picks).
    in_memory: dict[Role, tuple[str, str]] = {}
    for role_value, (pid, model) in applied.role_picks.items():
        try:
            in_memory[Role(role_value)] = (pid, model)
        except ValueError:
            continue
    set_autoconfig(in_memory)

    n_picks = len(applied.role_picks)
    n_diff = len(applied.diff_picks)
    n_unset = len(applied.unset_roles)
    n_pre = len(applied.user_overrides)

    if min_context > 0:
        render_system_line(
            f"autoconfigured {n_picks} role(s) at mincontext={min_context} "
            f'→ primary {style("accent", applied.default_model or "?")} '
            f"on {applied.provider_id}  "
            + style("muted", f"({persisted} persisted; num_ctx={min_context} "
                              "will be passed to every call)")
        )
    else:
        render_system_line(
            f"autoconfigured {n_picks} role(s) in memory "
            f'→ primary {style("accent", applied.default_model or "?")} '
            f"on {applied.provider_id}"
        )
    if n_diff:
        render_system_line(f"  + {n_diff} stronger pick(s):")
        for r, m in applied.diff_picks.items():
            render_system_line(f"    {r} → {m}")
    if n_unset:
        render_system_line(style("warn",
            f"  {n_unset} role(s) left unset (no installed model met the threshold):"))
        render_system_line(style("muted",
            f"    {', '.join(applied.unset_roles)}"))
        render_system_line(style("muted",
            "    fix with /roles set <role> <provider_id> <model>  "
            "(or install a model that fits)"))
    if n_pre:
        render_system_line(
            f"  preserved {n_pre} prior user override(s) — "
            "use /roles unset <role> or /autoconfig clear to release"
        )


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


def _format_gb(bytes_val: int | float | None) -> str:
    """Format a byte count as a friendly GB string."""
    if bytes_val is None:
        return "?"
    try:
        gb = float(bytes_val) / (1024 ** 3)
    except (TypeError, ValueError):
        return "?"
    if gb >= 100:
        return f"{gb:.0f} GB"
    if gb >= 10:
        return f"{gb:.1f} GB"
    return f"{gb:.2f} GB"


def cmd_vram(ctx: CommandContext, _args: list[str]) -> None:
    """Show VRAM usage: total detected, what's currently loaded (live from
    each provider), and catalog estimates for role-assigned models that
    aren't loaded right now.
    """
    from agentcommander.engine.role_resolver import resolve as resolve_role
    from agentcommander.providers.base import list_active
    from agentcommander.typecast.catalog import get_catalog
    from agentcommander.typecast.vram import detect_vram
    from agentcommander.types import ALL_ROLES

    # ── Total detected ────────────────────────────────────────────────────
    vram = detect_vram()
    total_gb = vram.total_gb if vram.total_gb > 0 else None
    total_label = (
        f"{total_gb:.1f} GB" if total_gb else "unknown"
    )
    render_system_line(
        f"VRAM total (detected via {vram.source}): "
        + style("accent", total_label)
    )
    if vram.details:
        render_system_line(style("muted", f"  ({vram.details})"))

    # ── Live: what each provider has loaded right now ────────────────────
    loaded: list[dict[str, object]] = []
    for p in list_active():
        try:
            for d in p.list_loaded_details():
                d_with_provider: dict[str, object] = dict(d)
                d_with_provider["provider"] = p.id
                loaded.append(d_with_provider)
        except Exception:  # noqa: BLE001
            continue

    total_loaded_bytes = sum(
        int(d.get("size_vram") or 0)
        for d in loaded
        if isinstance(d.get("size_vram"), (int, float))
    )

    render_system_line("")
    if loaded:
        render_system_line(f"Loaded models ({len(loaded)}):")
        rows: list[list[str]] = []
        for d in loaded:
            name = str(d.get("name", "?"))
            size_str = _format_gb(d.get("size_vram"))
            details = d.get("details") if isinstance(d.get("details"), dict) else {}
            param = (details.get("parameter_size") if isinstance(details, dict) else "") or ""
            quant = (details.get("quantization_level") if isinstance(details, dict) else "") or ""
            tag = " ".join(x for x in (param, quant) if x)
            expires = str(d.get("expires_at") or "")
            # Trim ISO-8601 to HH:MM:SS for readability
            if "T" in expires:
                expires = expires.split("T", 1)[1][:8]
            rows.append([name, size_str, tag, expires])
        render_table(["model", "vram", "size/quant", "keep_alive_until"], rows)

        loaded_gb = total_loaded_bytes / (1024 ** 3)
        render_system_line(
            f"  total loaded: {style('accent', f'{loaded_gb:.2f} GB')}"
        )
        if total_gb:
            free_gb = max(0.0, total_gb - loaded_gb)
            pct = min(100.0, (loaded_gb / total_gb) * 100)
            render_system_line(
                f"  free (est):   {style('accent', f'{free_gb:.2f} GB')}  "
                + style("muted", f"({pct:.0f}% used of detected total)")
            )
    else:
        render_system_line(style("muted",
            "  (no models currently loaded — none of the active providers "
            "report a resident model)"))

    # ── Catalog estimates for role-assigned models not currently loaded ──
    seen: set[str] = set()
    role_models: list[tuple[str, str]] = []  # (model, comma-separated roles)
    role_for_model: dict[str, list[str]] = {}
    for role in ALL_ROLES:
        rr = resolve_role(role)
        if rr is None:
            continue
        role_for_model.setdefault(rr.model, []).append(role.value)
        if rr.model in seen:
            continue
        seen.add(rr.model)
        role_models.append((rr.model, ""))

    loaded_names = {str(d.get("name", "")) for d in loaded}
    unloaded_models = [
        m for (m, _) in role_models if m and m not in loaded_names
    ]

    if unloaded_models:
        catalog_result = get_catalog()
        cat = catalog_result.catalog if catalog_result else {}
        render_system_line("")
        render_system_line(
            "Role-assigned models not currently loaded "
            + style("muted", "(catalog estimate, no context-size adjustment):")
        )
        rows = []
        for m in sorted(unloaded_models):
            entry = cat.get(m) if isinstance(cat, dict) else None
            est = "?"
            ctx_len = ""
            if isinstance(entry, dict):
                est_val = entry.get("estimatedVramGb")
                if isinstance(est_val, (int, float)) and est_val > 0:
                    est = f"~{float(est_val):.1f} GB"
                ctx_raw = entry.get("contextLength")
                if isinstance(ctx_raw, (int, float)) and ctx_raw > 0:
                    n = int(ctx_raw)
                    if n >= 1024:
                        ctx_len = f"{n // 1024}k ctx"
                    else:
                        ctx_len = f"{n} ctx"
            roles_using = ", ".join(role_for_model.get(m, []))
            rows.append([m, est, ctx_len, roles_using[:60]])
        render_table(["model", "est. vram", "trained ctx", "used by"], rows)

    render_system_line("")
    render_system_line(style("muted",
        "Note: live values come from Ollama's /api/ps; catalog estimates "
        "don't include context-size overhead — actual VRAM with a large "
        "num_ctx will be higher than the catalog figure."))


def cmd_db(ctx: CommandContext, args: list[str]) -> None:
    """Inspect and repair the SQLite store.

    /db                 # show DB path + integrity status
    /db check           # run PRAGMA integrity_check / quick_check, list issues
    /db vacuum          # rebuild the file (defragments, can recover from light corruption)
    /db reindex         # rebuild every index (often clears 'database disk image is malformed')
    /db backup <path>   # write a copy of the DB to <path> (uses sqlite backup API)
    /db reset           # DESTRUCTIVE: rename the DB out of the way and re-init a fresh one
    """
    import os
    import shutil
    import sqlite3
    import time
    from agentcommander.db.connection import get_db, _db_path, init_db, close_db

    sub = (args[0] if args else "").lower()

    if sub in ("", "status", "info"):
        path = str(_db_path or "?")
        size = "?"
        try:
            size_bytes = os.path.getsize(path)
            size = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / 1024 / 1024:.1f} MB"
        except OSError:
            pass
        render_system_line(f"DB path: {style('accent', path)}")
        render_system_line(style("muted", f"  size: {size}"))
        # Light health check
        try:
            row = get_db().execute("PRAGMA quick_check").fetchone()
            ok = bool(row) and (row[0] == "ok")
            if ok:
                render_system_line("  integrity: " + style("accent", "ok") +
                                   " (quick_check)")
            else:
                render_system_line(style("warn", f"  integrity: {row[0] if row else 'unknown'}"))
                render_system_line(style("muted",
                    "  run /db check for the full report, then /db reindex or /db vacuum"))
        except sqlite3.DatabaseError as exc:
            render_system_line(style("warn", f"  integrity: ERROR — {exc}"))
            render_system_line(style("muted",
                "  the DB is corrupted. Try: /db reindex, then /db vacuum, then /db backup, "
                "and as a last resort /db reset"))
        return

    if sub == "check":
        try:
            rows = get_db().execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            render_system_line(style("warn", f"integrity_check failed: {exc}"))
            return
        if rows and rows[0][0] == "ok":
            render_system_line("integrity_check: " + style("accent", "ok"))
        else:
            render_system_line(style("warn",
                f"integrity_check found {len(rows)} issue(s):"))
            for r in rows[:30]:
                render_system_line(f"  {r[0]}")
            if len(rows) > 30:
                render_system_line(style("muted",
                    f"  …and {len(rows) - 30} more"))
            render_system_line(style("muted",
                "  Try: /db reindex (often resolves index corruption), "
                "then /db vacuum if issues remain"))
        return

    if sub == "vacuum":
        try:
            get_db().execute("VACUUM")
            render_system_line("VACUUM: " + style("accent", "complete"))
        except sqlite3.DatabaseError as exc:
            render_system_line(style("warn", f"VACUUM failed: {exc}"))
        return

    if sub == "reindex":
        try:
            get_db().execute("REINDEX")
            render_system_line("REINDEX: " + style("accent", "complete"))
        except sqlite3.DatabaseError as exc:
            render_system_line(style("warn", f"REINDEX failed: {exc}"))
        return

    if sub == "backup":
        if len(args) < 2:
            render_system_line("usage: /db backup <path-to-new-file>")
            return
        target = args[1]
        if os.path.exists(target):
            render_system_line(style("warn",
                f"refusing to overwrite existing file: {target}"))
            return
        try:
            with sqlite3.connect(target) as dst:
                get_db().backup(dst)
            size_kb = os.path.getsize(target) / 1024
            render_system_line(f"backup written: {style('accent', target)} "
                               + style("muted", f"({size_kb:.1f} KB)"))
        except (sqlite3.DatabaseError, OSError) as exc:
            render_system_line(style("warn", f"backup failed: {exc}"))
        return

    if sub == "reset":
        # DESTRUCTIVE — rename the corrupt DB out of the way (don't delete,
        # so the user can still try recovery later) and re-init a fresh one.
        path = str(_db_path or "")
        if not path or not os.path.exists(path):
            render_system_line(style("warn", "no DB path resolved"))
            return
        ts = int(time.time())
        archived = f"{path}.corrupt-{ts}"
        # Close + rename + reopen
        close_db()
        try:
            shutil.move(path, archived)
            for ext in (".db-wal", ".db-shm", "-wal", "-shm",
                        "-journal", ".db-journal"):
                # Move journal/WAL companions out of the way too
                for candidate in (path + ext, path[:-len(".sqlite")] + ext if path.endswith(".sqlite") else None):
                    if candidate and os.path.exists(candidate):
                        try:
                            shutil.move(candidate, candidate + f".corrupt-{ts}")
                        except OSError:
                            pass
            init_db()
            render_system_line(f"old DB archived to: {style('muted', archived)}")
            render_system_line("  fresh DB initialized — restart AgentCommander to fully reload")
            render_system_line(style("muted",
                "  to recover data: copy the archived file into a new SQLite tool "
                "and run .recover, then re-import salvageable rows"))
        except OSError as exc:
            render_system_line(style("warn", f"reset failed: {exc}"))
            try:
                init_db()  # re-open the corrupt DB so the TUI keeps working
            except Exception:  # noqa: BLE001
                pass
        return

    render_system_line(f"unknown sub-command: /db {sub}")
    render_system_line("  try: /db, /db check, /db vacuum, /db reindex, /db backup <path>, /db reset")


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
            name="/stop", aliases=(),
            summary="halt the active pipeline run + cancel any planned future steps",
            handler=cmd_stop,
            usage="/stop",
            details=(
                "Mid-run, type /stop and press Enter to abort. The engine checks the\n"
                "cancel signal at iteration boundaries, so the active role call or tool\n"
                "may take a moment to finish, but no new dispatches happen after /stop.\n"
                "Ctrl-C also works (sends KeyboardInterrupt to the pipeline thread)."
            ),
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
            name="/context", aliases=("/ctx",),
            summary="show / set / clear the session-wide num_ctx override",
            handler=cmd_context,
            usage="/context              # show current override\n"
                  "/context <N>          # set the override (e.g. 32k, 128K, 65536)\n"
                  "/context off          # clear (roles fall back to their own setting)",
            details=(
                "The session override is the num_ctx value sent to the provider on\n"
                "every call this session. It beats per-role context_window_tokens\n"
                "(set by /autoconfig --mincontext or /roles set), so a single\n"
                "/context 32k re-pins every role at once without touching their\n"
                "persisted bindings.\n"
                "\n"
                "Suffixes: k = 1024 tokens, m = 1024² (binary, matches the TypeCast\n"
                "catalog's contextLength).\n"
                "\n"
                "If the value exceeds any picked model's training contextLength\n"
                "from the catalog, every offending role/model is printed as a\n"
                "warning. The override is still applied — your call. Beyond a\n"
                "model's training window the provider may truncate the prompt or\n"
                "refuse the request.\n"
                "\n"
                "Persistence: stored in the config table, survives restarts. The\n"
                "startup banner reports it under \"/context override active\"."
            ),
            examples=(
                "/context",
                "/context 32k",
                "/context 65536",
                "/context off",
            ),
        ),
        SlashCommand(
            name="/autoconfig", aliases=("/ac",),
            summary="run TypeCast autoconfigure with optional context-window filter",
            handler=cmd_autoconfig,
            usage="/autoconfig                            # default in-memory autoconfig\n"
                  "/autoconfig --mincontext <N>           # filter by ctx + persist picks\n"
                  "/autoconfig ban <model_id>             # exclude a model + re-run\n"
                  "/autoconfig unban <model_id>           # re-allow a model + re-run\n"
                  "/autoconfig bans                       # list banned models\n"
                  "/autoconfig clear                      # wipe persisted picks + redo",
            details=(
                "Walks the TypeCast catalog over every installed model and assigns\n"
                "the best-scoring fit per role using the threshold cascade\n"
                "(scores 100 → 90 → … → 10; roles with no qualifying model land in\n"
                "the unset list). Suffix tokens with k/m for binary multiples — \n"
                "128k = 131072 tokens.\n"
                "\n"
                "--mincontext <N>  drops candidates whose catalog contextLength\n"
                "                  is below N tokens (so a 32k-trained model is\n"
                "                  skipped when you ask for 128k). The picks are\n"
                "                  persisted to role_assignments with\n"
                "                  context_window_tokens = N, so call_role passes\n"
                "                  num_ctx=N on every Ollama request — no silent\n"
                "                  fallback to the runtime default. Persisted rows\n"
                "                  are skipped on subsequent startup autoconfigs;\n"
                "                  re-run with a new --mincontext to re-pick them.\n"
                "                  Pre-existing /roles set overrides (no context)\n"
                "                  are preserved.\n"
                "\n"
                "ban <model_id>    adds the model to a persisted ban list. Banned\n"
                "                  models are invisible to every autoconfig\n"
                "                  picker — default election, per-role scoring,\n"
                "                  threshold cascade. Useful when a model is\n"
                "                  misbehaving on this hardware. After updating\n"
                "                  the list, /autoconfig automatically re-runs\n"
                "                  so role assignments reflect the change.\n"
                "                  /roles set still works manually for banned\n"
                "                  models if you want to use one explicitly.\n"
                "\n"
                "unban <model_id>  removes <model_id> from the ban list and\n"
                "                  re-runs autoconfigure.\n"
                "\n"
                "bans              prints the current ban list.\n"
                "\n"
                "clear             deletes every row in role_assignments — both\n"
                "                  /roles set overrides AND prior --mincontext\n"
                "                  picks — then re-prompts for the Ollama\n"
                "                  server URL (Enter keeps the current one). If\n"
                "                  the URL changed, updates the provider record\n"
                "                  and rebuilds the in-memory provider registry\n"
                "                  so the next list_models() hits the new host.\n"
                "                  Finally runs the default in-memory\n"
                "                  autoconfigure (does not persist). Does NOT\n"
                "                  touch the ban list — bans survive clears.\n"
                "\n"
                "With no flags, behaves like /roles auto: in-memory picks only,\n"
                "recomputed every launch."
            ),
            examples=(
                "/autoconfig",
                "/autoconfig --mincontext 128k",
                "/autoconfig --mincontext 32000",
                "/autoconfig ban gemma3:1b",
                "/autoconfig unban gemma3:1b",
                "/autoconfig bans",
                "/autoconfig clear",
            ),
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
            name="/vram", aliases=(),
            summary="show VRAM usage: detected total, loaded models (live), role estimates",
            handler=cmd_vram,
            usage="/vram",
            details=(
                "Three sections, in order:\n"
                "  1. Detected total VRAM (nvidia-smi / wmic / Apple Silicon).\n"
                "  2. Live loaded-model table — pulled from each provider's\n"
                "     list_loaded_details(). For Ollama this hits /api/ps and\n"
                "     reports the daemon's actual size_vram per model, not an\n"
                "     estimate. The keep_alive_until column shows when the\n"
                "     model auto-unloads (5m default — see /autoconfig docs).\n"
                "  3. Catalog estimate for any model that's role-assigned but\n"
                "     not currently loaded. estimatedVramGb is the model's\n"
                "     base footprint at a small num_ctx; actual usage scales\n"
                "     up with context size and isn't shown here.\n"
                "\n"
                "Useful when you're trying to figure out why something is\n"
                "slow (model swapping in/out) or when planning a /context\n"
                "override (\"how much headroom do I actually have?\")."
            ),
            examples=("/vram",),
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
            name="/db", aliases=(),
            summary="inspect or repair the SQLite store",
            handler=cmd_db,
            usage="/db                    # show path + integrity status\n"
                  "/db check              # full integrity_check report\n"
                  "/db reindex            # rebuild every index (fast, often clears corruption)\n"
                  "/db vacuum             # rebuild the file (slower, fully defragments)\n"
                  "/db backup <path>      # write a copy via sqlite backup API\n"
                  "/db reset              # DESTRUCTIVE: archive corrupt DB + start fresh",
            details=(
                "Most 'database disk image is malformed' errors come from a\n"
                "broken index — rare but possible after an interrupted write.\n"
                "Order to try when a check fails:\n"
                "  1. /db reindex      (free; almost always fixes it)\n"
                "  2. /db vacuum       (rewrites the whole file)\n"
                "  3. /db backup foo.sqlite  (preserve what you have)\n"
                "  4. /db reset        (rename to *.corrupt-NNN, fresh DB)\n"
                "\n"
                "Startup also runs PRAGMA quick_check automatically and tries\n"
                "REINDEX if it fails — most users never see this command."
            ),
            examples=("/db", "/db check", "/db reindex", "/db backup ~/ac-backup.sqlite"),
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
