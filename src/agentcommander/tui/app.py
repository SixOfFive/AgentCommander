"""Main REPL — read input, dispatch slash commands or run the pipeline.

Pure stdlib. Uses `input()` (with readline if available for line editing
+ history). When the pipeline runs, events stream live via render_event().
The pipeline runs on a worker thread so the main thread can poll the keyboard
non-blocking and react to `/stop`.
"""
from __future__ import annotations

import queue
import shlex
import sys
import threading
import traceback
from contextlib import contextmanager

from agentcommander import __version__
from agentcommander.db.connection import init_db
from agentcommander.db.repos import (
    append_message,
    create_conversation,
    get_config,
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
from agentcommander.engine.role_resolver import (
    autoconfig_table as _autoconfig_table,
    resolve as resolve_role,
    set_autoconfig as _set_autoconfig,
)
from agentcommander.tui.commands import COMMANDS, CommandContext
from agentcommander.tui.render import (
    render_assistant_message,
    render_banner,
    render_error,
    render_event,
    render_role_delta,
    render_system_line,
    render_table,
    render_user_message,
)
from agentcommander.tui.setup import first_run_wizard, needs_first_run_setup
from agentcommander.tui.status_bar import get_status_bar, read_line_at_bottom
from agentcommander.types import ALL_ROLES, Role
from agentcommander.typecast import (
    apply_autoconfigure,
    get_catalog,
    refresh_catalog,
)


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
    rr = resolve_role(Role.ORCHESTRATOR)
    return rr.model if rr else None


# ─── REPL ──────────────────────────────────────────────────────────────────


def _read_line() -> str | None:
    """Read one line from the user, anchored to the bottom row.

    Returns None on EOF, "" on Ctrl-C (interrupt without exit).
    """
    try:
        return read_line_at_bottom("❯ ")
    except KeyboardInterrupt:
        writeln()
        return ""


def _poll_stdin_chunk() -> str:
    """Non-blocking read of whatever the user has typed so far.

    Returns a (possibly empty) string. Cross-platform:
      - Windows: msvcrt.kbhit / msvcrt.getwch — already char-at-a-time, no echo.
      - POSIX:   select.select + os.read — char-at-a-time when the terminal
                 is in cbreak mode (see `_pipeline_input_mode`). Falls back to
                 line-buffered reads if cbreak isn't active.
    """
    if not sys.stdin.isatty():
        return ""
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:
            return ""
        out: list[str] = []
        # Drain everything currently buffered so a fast typist doesn't lose
        # keystrokes between polls.
        while msvcrt.kbhit():
            try:
                ch = msvcrt.getwch()
            except OSError:
                break
            out.append(ch)
        return "".join(out)
    try:
        import os
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return ""
        fd = sys.stdin.fileno()
        return os.read(fd, 256).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


@contextmanager
def _pipeline_input_mode():
    """Switch the terminal into cbreak + no-echo for the duration of a run.

    Yields True when char-at-a-time input with self-managed echo is available
    (so the caller can paint typed characters onto the input row themselves).
    Yields False on Windows non-tty / POSIX without termios / piped stdin —
    the caller should fall back to line-mode and accept that mid-run typing
    won't render until Enter.

    On Windows we don't need to touch the terminal: msvcrt.getwch is already
    char-at-a-time and doesn't echo.
    """
    if not sys.stdin.isatty():
        yield False
        return
    if sys.platform == "win32":
        yield True
        return
    try:
        import termios
        import tty
    except ImportError:
        yield False
        return
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        new_attrs = termios.tcgetattr(fd)
        # Disable terminal echo so we don't double-render keystrokes (the bar
        # paints them on the input row; the OS would otherwise also echo them
        # wherever the cursor happens to be parked).
        new_attrs[3] = new_attrs[3] & ~termios.ECHO  # type: ignore[index]
        termios.tcsetattr(fd, termios.TCSANOW, new_attrs)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _consume_input_chunk(buffer: str, chunk: str) -> tuple[str, tuple[str, str] | None]:
    """Apply a chunk of typed characters to the in-flight buffer.

    Returns ``(new_buffer, action)``. ``action`` is ``("submit", line)`` when
    the user pressed Enter (line is the buffer contents at submit time, with
    surrounding whitespace stripped), otherwise ``None``.

    Backspace (DEL or BS) edits in place. Bare control bytes are dropped, and
    common terminal escape sequences are consumed as a unit so arrow keys and
    function keys don't leak ``[A`` / ``OP`` etc. into the buffer:
      - CSI: ``ESC [ <params> <final>`` where final is a letter or ``~``
      - SS3: ``ESC O <final>`` (F1-F4 on many terminals)
    Windows special-key prefixes (``\\x00`` / ``\\xe0``) consume the next byte too.
    """
    new_buf = buffer
    i = 0
    n = len(chunk)
    while i < n:
        ch = chunk[i]
        # Windows special-key prefix — skip the next byte (arrow / F-key code).
        if ch in ("\x00", "\xe0"):
            i += 2
            continue
        # POSIX ANSI escape — consume the whole sequence so it doesn't leak.
        if ch == "\x1b":
            i += 1
            if i < n and chunk[i] == "[":
                i += 1
                while i < n:
                    c2 = chunk[i]
                    i += 1
                    if c2 == "~" or ("A" <= c2 <= "Z") or ("a" <= c2 <= "z"):
                        break
            elif i < n and chunk[i] == "O":
                # SS3: ESC O <one final byte>
                i += 2
            # else: lone ESC — already swallowed.
            continue
        i += 1
        if ch in ("\r", "\n"):
            line = new_buf.strip()
            return "", ("submit", line)
        if ch in ("\x7f", "\x08"):
            new_buf = new_buf[:-1]
            continue
        if ord(ch) < 32:
            # Drop other control bytes (tab, etc.). Ctrl-C still raises
            # KeyboardInterrupt because cbreak preserves ISIG.
            continue
        new_buf += ch
    return new_buf, None


def _run_pipeline(state: dict, user_message: str) -> None:
    conv_id = _ensure_conversation(state)
    append_message(conv_id, "user", user_message)

    bar = get_status_bar()
    bar.reset_run()
    bar.set_running(True)

    def _on_role_start(role: str, model: str) -> None:
        bar.set_role(role, model)

    def _on_role_end(role: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        bar.add_tokens(prompt=prompt_tokens, completion=completion_tokens)

    opts = RunOptions(
        conversation_id=conv_id,
        user_message=user_message,
        working_directory=state.get("working_dir"),
        on_role_delta=render_role_delta,
        on_role_start=_on_role_start,
        on_role_end=_on_role_end,
    )
    run = PipelineRun(opts)
    cancel_event = threading.Event()
    run.cancel_event = cancel_event
    state["active_cancel"] = cancel_event

    events_q: queue.Queue = queue.Queue()
    final_holder: dict[str, str | None] = {"final": None}

    def _runner() -> None:
        try:
            for evt in run.events():
                events_q.put(("event", evt))
                if evt.type == "done":
                    final_holder["final"] = evt.final
        except KeyboardInterrupt:
            events_q.put(("error", "interrupted"))
        except Exception as exc:  # noqa: BLE001
            events_q.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            events_q.put(("end", None))

    worker = threading.Thread(target=_runner, daemon=True, name="ac-pipeline")
    worker.start()

    typed_buffer = ""
    legacy_buffer = ""  # only used when char-mode isn't available

    with _pipeline_input_mode() as raw_ready:
        if raw_ready:
            bar.set_pending_input("")
            render_system_line(style("muted",
                "  (type your next prompt while this runs — Enter queues it · /stop halts)"))
        else:
            render_system_line(style("muted",
                "  (type /stop and press Enter to halt this run)"))

        try:
            while True:
                try:
                    kind, data = events_q.get(timeout=0.05 if raw_ready else 0.15)
                except queue.Empty:
                    chunk = _poll_stdin_chunk()
                    if chunk:
                        if raw_ready:
                            typed_buffer, action = _consume_input_chunk(typed_buffer, chunk)
                            bar.set_pending_input(typed_buffer)
                            if action is not None and action[0] == "submit":
                                line = action[1]
                                if line == "/stop":
                                    cancel_event.set()
                                    render_system_line(style("warn",
                                        "  /stop received — halting the pipeline…"))
                                elif line:
                                    state["queued_next"] = line
                                    render_system_line(style("muted",
                                        f"  queued for after this run: {line}"))
                        else:
                            legacy_buffer += chunk
                            if any(c in legacy_buffer for c in ("\n", "\r")):
                                line = legacy_buffer.replace("\r", "\n").split("\n", 1)[0].strip()
                                legacy_buffer = ""
                                if line == "/stop":
                                    cancel_event.set()
                                    render_system_line(style("warn",
                                        "  /stop received — halting the pipeline…"))
                                elif line:
                                    state["queued_next"] = line
                                    render_system_line(style("muted",
                                        f"  queued for after this run: {line}"))
                    continue

                if kind == "end":
                    break
                if kind == "event":
                    try:
                        render_event(data)
                    except Exception as exc:  # noqa: BLE001
                        render_error(f"render error: {exc}")
                elif kind == "error":
                    render_error(str(data))
                    if state.get("debug"):
                        traceback.print_exc()
        except KeyboardInterrupt:
            # Ctrl-C during a run: ask the pipeline to stop, then drain so the
            # worker thread exits cleanly before we return to the REPL.
            cancel_event.set()
            render_system_line(style("warn", "  ^C received — halting the pipeline…"))
            while True:
                try:
                    kind, _ = events_q.get(timeout=0.5)
                except queue.Empty:
                    break
                if kind == "end":
                    break

    # If the user had a half-typed line buffered when the run finished but
    # never pressed Enter, surface it once so it isn't silently lost.
    leftover = typed_buffer.strip() if raw_ready else legacy_buffer.strip()
    if leftover and "queued_next" not in state:
        render_system_line(style("muted",
            f'  unsent input dropped: "{leftover}" (re-type it at the prompt)'))

    bar.set_pending_input(None)
    state.pop("active_cancel", None)
    if final_holder["final"]:
        append_message(conv_id, "assistant", final_holder["final"])
    bar.set_running(False)


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


def _print_role_assignments() -> None:
    """Show the active role → (provider, model) bindings.

    Pulls from the RoleResolver so it reflects DB overrides + in-memory
    autoconfig. The 'kind' column distinguishes:
      - 'override': set by /roles set, persisted in DB
      - 'auto':     picked by /roles auto / startup autoconfigure (in-memory)
      - 'unset':    no binding from either source
    """
    rows: list[list[str]] = []
    for role in ALL_ROLES:
        rr = resolve_role(role)
        if rr is None:
            rows.append([role.value, "—", "—", style("warn", "unset")])
        else:
            rows.append([role.value, rr.model, rr.provider_id, rr.kind])
    render_system_line("Role → model assignments:")
    render_table(["role", "model", "provider", "kind"], rows)


def _run_startup_autoconfigure() -> None:
    """Compute best-fit role → model in memory. NOT persisted to DB.

    Recomputed on every launch — if the user pulls or removes a model, the
    next start picks it up automatically. User overrides set via `/roles set`
    survive in the DB and beat the autoconfig at resolve time.
    """
    from agentcommander.db.repos import audit, get_role_assignment as _gra
    from agentcommander.providers.base import list_active

    providers = list_active()
    if not providers:
        return

    applied = apply_autoconfigure(
        providers=providers,
        get_role_assignment_fn=_gra,
        audit_fn=audit,
    )

    if applied.skipped_reason:
        render_system_line(style("warn", f"autoconfigure skipped: {applied.skipped_reason}"))
        _set_autoconfig({})
        return

    # Stash the picks in the in-memory resolver. Convert role.value → Role enum.
    in_memory_table: dict[Role, tuple[str, str]] = {}
    for role_value, (provider_id, model) in applied.role_picks.items():
        try:
            in_memory_table[Role(role_value)] = (provider_id, model)
        except ValueError:
            continue
    _set_autoconfig(in_memory_table)

    n_auto = len(applied.role_picks)
    n_overrides = len(applied.user_overrides)
    n_diff = len(applied.diff_picks)
    n_unset = len(applied.unset_roles)
    msg = (f"autoconfigured {n_auto} role(s) → primary model "
           f'{style("accent", applied.default_model or "?")}'
           f' on {applied.provider_id}')
    render_system_line(msg)
    if n_diff:
        render_system_line(f"  + {n_diff} role(s) got a stronger TypeCast pick:")
        for role_name, model in applied.diff_picks.items():
            render_system_line(f"    {role_name} → {model}")
    if n_unset:
        render_system_line(style("warn",
            f"  {n_unset} role(s) left unset (no installed model scores ≥ "
            f"the minimum threshold):"))
        render_system_line(style("muted",
            f"    {', '.join(applied.unset_roles)}"))
        render_system_line(style("muted",
            "    use /roles set <role> <provider_id> <model> to fill them in"))
    if n_overrides:
        render_system_line(f"  preserved {n_overrides} user override(s) "
                           f"(use /roles unset <role> to release)")


def run_tui() -> int:
    """Entry point — runs the REPL until /quit or EOF."""
    _bootstrap()

    state: dict = {
        "working_dir": get_config("working_directory", None),
        "conversation_id": None,
        "should_exit": False,
        "debug": False,
    }

    # Install the persistent bottom panel BEFORE printing the banner so all
    # subsequent output scrolls up naturally inside the reserved region
    # instead of overwriting the top of the screen.
    bar = get_status_bar()
    bar.set_workdir(state["working_dir"])
    bar.install()

    catalog = get_catalog()
    render_banner(
        version=__version__,
        providers_count=len(list_providers()),
        models_count=catalog.model_count if catalog else 0,
        working_dir=state["working_dir"],
    )

    # First-run flow: ask for the Ollama endpoint and persist it.
    if needs_first_run_setup():
        if not first_run_wizard():
            render_error("setup did not complete; you can configure manually with /providers add")
            # Continue into the REPL anyway so the user can recover.

    # After first-run setup, attempt to auto-assign roles using TypeCast scores.
    # Existing per-role overrides (is_override=1) are preserved.
    if list_providers():
        _run_startup_autoconfigure()
        _print_role_assignments()

    if resolve_role(Role.ORCHESTRATOR) is None:
        render_system_line("Orchestrator role unassigned. After adding a provider:")
        render_system_line("  /models <provider_id>          # see available models")
        render_system_line("  /roles assign-all <provider_id> <model>   # one-shot setup")

    # workdir may have changed during first-run/autoconfig; re-sync the bar.
    bar.set_workdir(state.get("working_dir"))

    while not state["should_exit"]:
        # Drain any prompt the user pre-typed during the previous run before
        # asking for fresh input.
        queued = state.pop("queued_next", None)
        if queued is not None:
            try:
                _handle_input(state, queued)
            except KeyboardInterrupt:
                writeln()
            except Exception as exc:  # noqa: BLE001
                render_error(f"{type(exc).__name__}: {exc}")
                if state.get("debug"):
                    traceback.print_exc()
            continue

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

    bar.uninstall()
    write(SHOW_CURSOR)
    writeln(style("muted", "  goodbye."))
    return 0


# Suppress unused-imports referenced through indirection
_ = (PALETTE, set_config)
