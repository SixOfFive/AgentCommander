"""Main REPL — read input, dispatch slash commands or run the pipeline.

Pure stdlib. Uses `input()` (with readline if available for line editing
+ history). When the pipeline runs, events stream live via render_event().
"""
from __future__ import annotations

import shlex
import sys
import traceback

from agentcommander import __version__
from agentcommander.db.connection import init_db
from agentcommander.db.repos import (
    append_message,
    create_conversation,
    get_config,
    get_role_assignment,
    list_providers,
    set_config,
)
from agentcommander.engine.engine import PipelineRun, RunOptions
from agentcommander.providers.bootstrap import bootstrap as bootstrap_providers
from agentcommander.tools.dispatcher import bootstrap_builtins as bootstrap_tools
from agentcommander.tui.ansi import (
    PALETTE,
    SHOW_CURSOR,
    enable_ansi,
    style,
    write,
    writeln,
)
from agentcommander.tui.commands import COMMANDS, CommandContext
from agentcommander.tui.render import (
    render_assistant_message,
    render_banner,
    render_error,
    render_event,
    render_role_delta,
    render_system_line,
    render_user_message,
)
from agentcommander.types import Role
from agentcommander.typecast import detect_vram, get_catalog, refresh_catalog


# Try to enable readline for line editing + history (stdlib).
try:
    import readline  # noqa: F401
except ImportError:
    pass


# ─── Bootstrap ─────────────────────────────────────────────────────────────


def _bootstrap() -> None:
    enable_ansi()
    init_db()
    bootstrap_tools()
    bootstrap_providers()
    # Per spec: every startup, fetch latest TypeCast catalog. Failure = use cache.
    try:
        refresh_catalog()
    except Exception:  # noqa: BLE001 — never crash startup
        pass


def _ensure_conversation(state: dict) -> str:
    if state.get("conversation_id"):
        return state["conversation_id"]
    conv = create_conversation(title="Conversation",
                                working_directory=state.get("working_dir"))
    state["conversation_id"] = conv.id
    return conv.id


def _default_model() -> str | None:
    a = get_role_assignment(Role.ORCHESTRATOR)
    return a["model"] if a else None


# ─── REPL ──────────────────────────────────────────────────────────────────


def _read_line() -> str | None:
    """Read one line from the user. Returns None on Ctrl-D / EOF."""
    try:
        prompt = style("user_label", "❯ ") if sys.stdout.isatty() else ""
        return input(prompt)
    except EOFError:
        return None
    except KeyboardInterrupt:
        # Ctrl-C interrupts the current input but doesn't exit.
        writeln()
        return ""


def _run_pipeline(state: dict, user_message: str) -> None:
    conv_id = _ensure_conversation(state)
    append_message(conv_id, "user", user_message)

    opts = RunOptions(
        conversation_id=conv_id,
        user_message=user_message,
        working_directory=state.get("working_dir"),
        on_role_delta=render_role_delta,  # live typewriter streaming
    )
    run = PipelineRun(opts)

    final_text: str | None = None
    try:
        for evt in run.events():
            render_event(evt)
            if evt.type == "done":
                final_text = evt.final
            elif evt.type == "error" and not final_text:
                final_text = None
    except KeyboardInterrupt:
        render_error("interrupted by user")
    except Exception as exc:  # noqa: BLE001
        render_error(f"engine crashed: {type(exc).__name__}: {exc}")
        if state.get("debug"):
            traceback.print_exc()

    if final_text:
        append_message(conv_id, "assistant", final_text)


def _handle_input(state: dict, line: str) -> None:
    line = line.strip()
    if not line:
        return
    if line.startswith("/"):
        try:
            argv = shlex.split(line)
        except ValueError:
            argv = line.split()
        name, args = argv[0], argv[1:]
        cmd = COMMANDS.get(name)
        if cmd is None:
            render_error(f"unknown command: {name}  (try /help)")
            return
        cmd.handler(CommandContext(state=state), args)
        return
    # Plain message → run the pipeline.
    render_user_message(line)
    _run_pipeline(state, line)


def run_tui() -> int:
    """Entry point — runs the REPL until /quit or EOF."""
    _bootstrap()

    state: dict = {
        "working_dir": get_config("working_directory", None),
        "conversation_id": None,
        "should_exit": False,
        "debug": False,
    }

    catalog = get_catalog()
    vram = detect_vram()
    render_banner(
        version=__version__,
        providers_count=len(list_providers()),
        models_count=catalog.model_count if catalog else 0,
        vram_gb=vram.total_gb,
        working_dir=state["working_dir"],
    )

    if not list_providers():
        render_system_line("No providers configured. Add one to start:")
        render_system_line("  /providers add ollama-local ollama \"Local Ollama\" http://127.0.0.1:11434")

    if not get_role_assignment(Role.ORCHESTRATOR):
        render_system_line("Orchestrator role unassigned. After adding a provider:")
        render_system_line("  /models <provider_id>          # see available models")
        render_system_line("  /roles assign-all <provider_id> <model>   # one-shot setup")

    while not state["should_exit"]:
        line = _read_line()
        if line is None:
            writeln()
            break
        try:
            _handle_input(state, line)
        except KeyboardInterrupt:
            writeln()
            continue
        except Exception as exc:  # noqa: BLE001
            render_error(f"{type(exc).__name__}: {exc}")
            if state.get("debug"):
                traceback.print_exc()

    write(SHOW_CURSOR)
    writeln(style("muted", "  goodbye."))
    return 0


# Suppress unused-imports referenced through indirection
_ = (PALETTE, set_config)
