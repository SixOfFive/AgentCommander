"""Main REPL — read input, dispatch slash commands or run the pipeline.

Pure stdlib. Uses `input()` (with readline if available for line editing
+ history). When the pipeline runs, events stream live via render_event().
The pipeline runs on a worker thread so the main thread can poll the keyboard
non-blocking and react to `/stop`.
"""
from __future__ import annotations

import os
import queue
import shlex
import sys
import threading
import traceback

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
from agentcommander.tui.terminal_input import poll_chars, raw_mode
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
    # init_db is normally called by cli.py before this runs (which also
    # surfaces DBAlreadyOpen friendly). It's idempotent, so re-calling here
    # is safe — protects callers that import run_tui directly.
    init_db()
    bootstrap_tools()
    bootstrap_providers()
    # Per spec: every startup, fetch latest TypeCast catalog. Failure = use cache.
    try:
        refresh_catalog()
    except Exception:  # noqa: BLE001 — never crash startup
        pass
    # Trim the live-event stream so the DB doesn't grow unbounded across
    # long sessions. Mirror only consumes the live tail (events arriving
    # after attach) so older rows are useless.
    try:
        import time as _t
        from agentcommander.db.repos import prune_pipeline_events
        cutoff_ms = int((_t.time() - 3600) * 1000)  # 1 hour
        prune_pipeline_events(cutoff_ms)
    except Exception:  # noqa: BLE001
        pass


def _ensure_conversation(state: dict) -> str:
    if state.get("conversation_id"):
        return state["conversation_id"]
    conv = create_conversation(title="Conversation",
                                working_directory=state.get("working_dir"))
    state["conversation_id"] = conv.id
    # Tell any mirror process which chat we're now on.
    try:
        from agentcommander.db.repos import set_active_conversation_id
        set_active_conversation_id(conv.id)
    except Exception:  # noqa: BLE001
        pass
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


def _refresh_or_paid_balance(bar) -> None:  # noqa: ANN001 - StatusBar local
    """Pull the OpenRouter Paid balance from the first configured provider
    of that type and push it into the status bar.

    Silent-no-op when:
      - no openrouter-paid provider is configured (Ollama / OR-Free users)
      - the provider's get_balance() returns None (transport / auth fail)

    Cheap-by-design: at most one HTTP call per role-end. We don't
    aggregate across multiple OR Paid providers; only the first wins.
    Users wanting multi-account balances can extend later.
    """
    try:
        from agentcommander.providers.base import list_active
    except Exception:  # noqa: BLE001
        return
    for provider in list_active():
        if getattr(provider, "type", None) != "openrouter-paid":
            continue
        if not hasattr(provider, "get_balance"):
            continue
        try:
            balance = provider.get_balance()
        except Exception:  # noqa: BLE001
            return
        if not balance:
            return
        try:
            bar.set_or_balance(
                credits_remaining=balance.get("credits_remaining"),
                credits_total=balance.get("credits_total"),
                daily_limit=balance.get("daily_limit"),
                daily_limit_remaining=balance.get("daily_limit_remaining"),
            )
        except Exception:  # noqa: BLE001
            pass
        return


def _unload_active_models() -> None:
    """Best-effort: ask each provider to evict its loaded models. Used when
    the user halts mid-run (/stop, /exit, /quit) so VRAM frees right away
    instead of waiting for Ollama's 5-minute idle timer.
    """
    try:
        from agentcommander.providers.base import list_active
        for p in list_active():
            try:
                p.unload_all_loaded()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass


def _handle_in_run_command(line: str, state: dict,
                           cancel_event: threading.Event) -> None:
    """Recognize control commands typed mid-run.

    /stop          → cancel the in-flight call, halt the pipeline, unload models
    /exit, /quit   → same as /stop, plus exit the REPL after the pipeline drains
    anything else  → queue for the next REPL iteration (existing behavior)

    Cancellation propagates through ``cancel_event`` to ``self.is_cancelled``
    in the engine, which feeds ``should_cancel`` to the provider's chat loop
    so the active stream closes mid-response.
    """
    if not line:
        return
    if line == "/stop":
        cancel_event.set()
        render_system_line(style("warn",
            "  /stop received — halting the pipeline…"))
        _unload_active_models()
        return
    if line in ("/exit", "/quit"):
        cancel_event.set()
        state["should_exit"] = True
        render_system_line(style("warn",
            f"  {line} received — halting and exiting…"))
        _unload_active_models()
        return
    state["queued_next"] = line
    render_system_line(style("muted",
        f"  queued for after this run: {line}"))


def _run_pipeline(state: dict, user_message: str) -> None:
    conv_id = _ensure_conversation(state)
    # Capture the new `messages` row id so the engine can tag the
    # router/classify scratchpad entry with it — the join key between the
    # user-view (full-fidelity, never compacted) and the model-view
    # (the scratchpad, which compaction can rewrite).
    user_msg = append_message(conv_id, "user", user_message)

    bar = get_status_bar()
    bar.reset_run()
    bar.set_running(True)

    # Live tee: every engine event the user sees on this primary screen
    # also lands in `pipeline_events` for `ac --mirror` to replay.
    from agentcommander.engine import live_tee
    run_id_holder: dict[str, str | None] = {"run_id": None}

    # Mark a fresh user turn so mirror sees the user message in the live
    # stream too (it'll also discover it via the messages table, but this
    # keeps event ordering tidy).
    live_tee.tee_event(
        "user/message",
        {"text": user_message, "message_id": user_msg.id},
        conversation_id=conv_id,
        flush_deltas=True,
    )

    def _on_role_start(role: str, model: str, num_ctx: int | None = None) -> None:
        # Display cap precedence:
        #   1. Explicit num_ctx from /context override or
        #      /autoconfig --mincontext — what the provider is actually
        #      being told to use.
        #   2. The autoconfig session ceiling = min(contextLength) across
        #      picked models. This is the same number the startup banner
        #      announces, so the bar matches.
        #   3. The current model's catalog contextLength — last-resort
        #      fallback when no ceiling has been computed (e.g. no
        #      autoconfig has run this session).
        #   4. None — bar omits the cap (shows "ctx N" only).
        # Provider behavior is unaffected; this only sets the display cap.
        display_cap = num_ctx
        if display_cap is None:
            from agentcommander.db.repos import get_config as _gc
            ceiling = _gc("session_ceiling_tokens", None)
            if isinstance(ceiling, (int, str)):
                try:
                    n = int(ceiling)
                    if n > 0:
                        display_cap = n
                except (TypeError, ValueError):
                    pass
        if display_cap is None and model:
            catalog = get_catalog()
            if catalog is not None:
                entry = catalog.catalog.get(model)
                if isinstance(entry, dict):
                    raw = entry.get("contextLength")
                    if isinstance(raw, (int, float)) and raw > 0:
                        display_cap = int(raw)
        bar.set_role(role, model, num_ctx=display_cap)
        live_tee.tee_role_start(role, model, display_cap,
                                conversation_id=conv_id,
                                run_id=run_id_holder["run_id"])

    def _on_role_end(role: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        bar.add_tokens(prompt=prompt_tokens, completion=completion_tokens)
        live_tee.tee_role_end(role, model, prompt_tokens, completion_tokens,
                              conversation_id=conv_id,
                              run_id=run_id_holder["run_id"])
        # Refresh the OpenRouter Paid balance on the bar after each call
        # so the user can watch their account draw down in near-real time.
        # Best-effort; no-op if no openrouter-paid provider is configured
        # or the credits / auth-key endpoints are unreachable.
        try:
            _refresh_or_paid_balance(bar)
        except Exception:  # noqa: BLE001
            pass

    def _on_role_delta(role: str, delta: str) -> None:
        # Tee FIRST (cheap, buffered) then forward to the screen renderer
        # so a slow terminal doesn't delay the mirror's view.
        try:
            live_tee.tee_delta(role, bar.state.model or "?", delta,
                               conversation_id=conv_id,
                               run_id=run_id_holder["run_id"])
        except Exception:  # noqa: BLE001
            pass
        render_role_delta(role, delta)

    opts = RunOptions(
        conversation_id=conv_id,
        user_message=user_message,
        user_message_id=user_msg.id,
        working_directory=state.get("working_dir"),
        on_role_delta=_on_role_delta,
        on_role_start=_on_role_start,
        on_role_end=_on_role_end,
    )
    run = PipelineRun(opts)
    cancel_event = threading.Event()
    run.cancel_event = cancel_event
    state["active_cancel"] = cancel_event
    if getattr(run, "run_id", None):
        run_id_holder["run_id"] = run.run_id

    events_q: queue.Queue = queue.Queue()
    final_holder: dict[str, str | None] = {"final": None, "error": None}

    def _runner() -> None:
        try:
            for evt in run.events():
                events_q.put(("event", evt))
                if evt.type == "error" and evt.error:
                    # Capture the last error so we can store a placeholder
                    # assistant message if the run ends without a done.
                    final_holder["error"] = evt.error
                # Tee non-delta engine events to the mirror stream. Delta
                # events come in via the on_role_delta callback (already
                # tee'd above with batching) — the engine doesn't yield
                # discrete role_delta PipelineEvents on the hot path, but
                # we filter just in case to avoid double-recording.
                if evt.type != "role_delta":
                    try:
                        payload = {k: v for k, v in evt.__dict__.items()
                                   if v is not None}
                        live_tee.tee_event(
                            f"engine/{evt.type}",
                            payload,
                            conversation_id=conv_id,
                            run_id=run_id_holder["run_id"],
                            # Don't force a flush before guard/iteration
                            # markers — the next role/delta would just
                            # re-flush moments later. Only flush at done.
                            flush_deltas=(evt.type == "done"),
                        )
                    except Exception:  # noqa: BLE001
                        pass
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

    # Tick the bar's run timer once per second so the "run X" / "total Y"
    # display advances even when no engine events arrive (e.g. the active
    # role is mid-generation with no completion-token chunk yet).
    import time as _time
    last_timer_tick = _time.monotonic()
    TIMER_TICK_INTERVAL_S = 1.0

    with raw_mode() as raw_ready:
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
                    now_t = _time.monotonic()
                    if now_t - last_timer_tick >= TIMER_TICK_INTERVAL_S:
                        bar.redraw()
                        last_timer_tick = now_t
                    chunk = poll_chars()
                    if chunk:
                        if raw_ready:
                            typed_buffer, action = _consume_input_chunk(typed_buffer, chunk)
                            bar.set_pending_input(typed_buffer)
                            if action is not None and action[0] == "submit":
                                _handle_in_run_command(action[1], state, cancel_event)
                        else:
                            legacy_buffer += chunk
                            if any(c in legacy_buffer for c in ("\n", "\r")):
                                line = legacy_buffer.replace("\r", "\n").split("\n", 1)[0].strip()
                                legacy_buffer = ""
                                _handle_in_run_command(line, state, cancel_event)
                    continue

                if kind == "end":
                    break
                if kind == "event":
                    # Pin retry countdowns to the bar so they survive
                    # scroll churn. The first event for an attempt has
                    # the full wait_seconds; later 15-second updates
                    # are no-ops on the bar (which derives a smooth
                    # per-second ticker from the wall clock). Retry
                    # events also go to render_event so the scroll
                    # area shows them once.
                    if getattr(data, "type", None) == "retry":
                        try:
                            bar.set_retry_state(
                                attempt=data.retry_attempt,
                                max_a=data.retry_max,
                                wait_s=data.retry_wait_seconds,
                            )
                        except Exception:  # noqa: BLE001
                            pass
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

    # Always persist SOMETHING as the assistant turn — even if the run
    # errored out. Without this, /chat resume shows a one-sided history
    # (just "You" lines, no replies). The placeholder is short, honest,
    # and tells the user what happened so they can decide whether to
    # retry or move on.
    if not final_holder["final"]:
        err = final_holder.get("error") or ""
        if "rate-limit" in err.lower() or "rate limit" in err.lower():
            placeholder = "(no reply — provider rate-limited; try again later)"
        elif "max iterations" in err.lower():
            placeholder = "(no reply — pipeline gave up after max iterations)"
        elif "cancelled" in err.lower() or "/stop" in err.lower():
            placeholder = "(reply cancelled)"
        elif err:
            placeholder = f"(no reply — {err[:140]})"
        else:
            placeholder = "(no reply — pipeline ended without producing output)"
        final_holder["final"] = placeholder

    if final_holder["final"]:
        # Persist the assistant turn into the user-view (`messages`) AND
        # drop a synthetic scratchpad entry pointing at it. The scratchpad
        # row is what the next pipeline run picks up on hydrate, so the
        # model sees "assistant said X" in the prior context.
        assistant_msg = append_message(conv_id, "assistant", final_holder["final"])
        try:
            from agentcommander.db.repos import insert_scratchpad_entry
            import time as _t
            insert_scratchpad_entry(
                conversation_id=conv_id,
                run_id=None,
                step=0,
                role="assistant",
                action="reply",
                input_text=user_message,
                output_text=final_holder["final"],
                timestamp=_t.time(),
                message_id=assistant_msg.id,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                from agentcommander.db.repos import audit
                audit("scratchpad.assistant_persist_failed",
                      {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass
        # Tee the final assistant message id so the mirror knows when to
        # render the canonical reply (it can also pick it up from the
        # `messages` table on its next poll).
        try:
            live_tee.tee_event(
                "assistant/final",
                {"text": final_holder["final"], "message_id": assistant_msg.id},
                conversation_id=conv_id,
                run_id=run_id_holder["run_id"],
                flush_deltas=True,
            )
        except Exception:  # noqa: BLE001
            pass
    bar.set_running(False)
    # Drop any stale delta buffer so a cancelled or errored run can't bleed
    # leftover text into the next pipeline's mirror stream.
    try:
        live_tee.reset_active_buffer()
    except Exception:  # noqa: BLE001
        pass


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

    'tok/s' is the running-average tokens-per-second for that model
    (default 100 when no measurement exists yet — converges to the real
    rate after a few calls).
    """
    from agentcommander.db.repos import get_throughput
    from agentcommander.tui.status_bar import _fmt_tps

    rows: list[list[str]] = []
    for role in ALL_ROLES:
        rr = resolve_role(role)
        if rr is None:
            rows.append([role.value, "—", "—", "—", style("warn", "unset")])
        else:
            tps = get_throughput(rr.model)
            rows.append([role.value, rr.model, rr.provider_id,
                         _fmt_tps(tps), rr.kind])
    render_system_line("Role → model assignments:")
    render_table(["role", "model", "provider", "tok/s", "kind"], rows)


def _refresh_or_role_picks_from_catalog() -> int:
    """Re-pick non-override OR roles from the latest catalog scores.

    Called once per launch BEFORE ``_run_startup_autoconfigure`` so the
    program — not the user — drives which models get used. Voting from
    prior runs (rate-limit -1s, sibling +1s, quality failures) shifts the
    catalog scores; this function propagates those shifts into
    ``role_assignments`` so the next pipeline call uses the freshest picks.

    Skipped when:
      - The role has ``is_override=True`` (user explicitly pinned with
        ``/roles set`` — manual choice survives refresh).
      - The role's current provider isn't an OR provider (Ollama / llama.cpp
        live in the main TypeCast catalog and have their own refresh path).

    Returns the number of role assignments that actually changed.
    """
    from agentcommander.db.repos import (
        get_role_assignment, list_role_assignments,
        list_providers, set_role_assignment,
    )
    from agentcommander.typecast.openrouter_catalog import (
        TIER_FREE, TIER_PAID, load, pick_for_role,
    )

    # Map provider_id → provider_type for fast lookup
    provider_types = {p.id: p.type for p in list_providers()}

    changed = 0
    for r in list_role_assignments():
        if r.get("is_override"):
            # Manual pin — leave alone.
            continue
        ptype = provider_types.get(r["provider_id"])
        if ptype not in ("openrouter-free", "openrouter-paid"):
            continue
        tier = TIER_FREE if ptype == "openrouter-free" else TIER_PAID
        new_pick = pick_for_role(tier, r["role"], fallback=r["model"])
        if new_pick and new_pick != r["model"]:
            cat = load(tier)
            entry = cat.get("_models", {}).get(new_pick) or {}
            ctx = entry.get("contextLength") or r.get("context_window_tokens") or 16384
            set_role_assignment(
                role=r["role"],
                provider_id=r["provider_id"],
                model=new_pick,
                is_override=False,
                context_window_tokens=ctx,
            )
            changed += 1
    return changed


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

    from agentcommander.db.repos import get_throughput as _gtp
    from agentcommander.tui.status_bar import _fmt_tps as _ftp

    def _model_with_tps(name: str | None) -> str:
        if not name:
            return "?"
        return f"{name} {style('muted', f'@ {_ftp(_gtp(name))}')}"

    n_auto = len(applied.role_picks)
    n_overrides = len(applied.user_overrides)
    n_diff = len(applied.diff_picks)
    n_unset = len(applied.unset_roles)
    msg = (f"autoconfigured {n_auto} role(s) → primary model "
           f'{style("accent", applied.default_model or "?")}'
           f' {style("muted", f"@ {_ftp(_gtp(applied.default_model or ""))}")}'
           f' on {applied.provider_id}')
    render_system_line(msg)
    if n_diff:
        render_system_line(f"  + {n_diff} role(s) got a stronger TypeCast pick:")
        for role_name, model in applied.diff_picks.items():
            render_system_line(f"    {role_name} → {_model_with_tps(model)}")
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

    _print_session_context_summary(applied)


def _humanize_tokens(n: int | None) -> str:
    """Compact integer-token display: 4096 → '4096', 32768 → '32k', 131072 → '128k'."""
    if n is None or n <= 0:
        return "?"
    if n < 1024:
        return str(n)
    if n < 1024 * 1024:
        v = n / 1024
        return f"{v:.0f}k" if abs(v - round(v)) < 0.05 else f"{v:.1f}k"
    v = n / (1024 * 1024)
    return f"{v:.0f}m" if abs(v - round(v)) < 0.05 else f"{v:.1f}m"


def _picked_model_contexts(role_picks: dict[str, tuple[str, str]]) -> list[tuple[str, str, int]]:
    """Walk ``role_picks`` against the catalog. Returns
    ``[(role_value, model, contextLength), ...]`` for models with a known
    contextLength. Used to compute the session ceiling and to find offenders
    when ``/context N`` exceeds a model's training cap.
    """
    catalog_result = get_catalog()
    if catalog_result is None:
        return []
    cat = catalog_result.catalog
    out: list[tuple[str, str, int]] = []
    for role_value, (_pid, model) in role_picks.items():
        entry = cat.get(model) if isinstance(cat, dict) else None
        if not isinstance(entry, dict):
            continue
        raw = entry.get("contextLength")
        if not isinstance(raw, (int, float)) or raw <= 0:
            continue
        out.append((role_value, model, int(raw)))
    return out


def _print_session_context_summary(applied) -> None:
    """Show the autoconfig-determined session context ceiling and any
    user-set override (``/context``).

    Ceiling = min(contextLength) across distinct picked models. The display
    name is the model that hit that minimum — if you set ``/context`` above
    this value, that model will be the offender that gets warned about.

    The ceiling is also persisted into ``config.session_ceiling_tokens`` so
    the bottom status bar can fall back to it when no per-role / /context
    override exists. Without that, the bar would show each role's own
    catalog ``contextLength`` (e.g. 128k) and disagree with this banner.
    """
    from agentcommander.db.repos import get_config, set_config
    from agentcommander.db.connection import get_db

    if not applied.role_picks:
        # No picks → no meaningful ceiling. Wipe any stale value so the bar
        # doesn't keep reporting last session's number.
        get_db().execute("DELETE FROM config WHERE key = ?",
                         ("session_ceiling_tokens",))
        return

    rows = _picked_model_contexts(applied.role_picks)
    if not rows:
        render_system_line(style("muted",
            "  session max context: unknown "
            "(no catalog contextLength for picked models)"))
        get_db().execute("DELETE FROM config WHERE key = ?",
                         ("session_ceiling_tokens",))
        return

    # Smallest training context across distinct picked models — the ceiling
    # at which all roles are guaranteed to be inside their training window.
    by_model: dict[str, int] = {}
    for _r, m, ctx in rows:
        if m not in by_model or ctx < by_model[m]:
            by_model[m] = ctx
    smallest_ctx = min(by_model.values())
    smallest_models = sorted(m for m, c in by_model.items() if c == smallest_ctx)
    label = smallest_models[0] if len(smallest_models) == 1 else (
        f"{smallest_models[0]} +{len(smallest_models) - 1} other(s)"
    )
    render_system_line(
        f'  session max context: {style("accent", _humanize_tokens(smallest_ctx))} '
        f'(lowest training ctx, set by {label})'
    )

    # Persist so the bar can reach for this same number when nothing more
    # specific is set.
    set_config("session_ceiling_tokens", smallest_ctx)

    raw_override = get_config("context_override_tokens", None)
    if isinstance(raw_override, (int, str)):
        try:
            override_tokens = int(raw_override)
        except (TypeError, ValueError):
            override_tokens = 0
        if override_tokens > 0:
            render_system_line(style("muted",
                f"  /context override active: {_humanize_tokens(override_tokens)} "
                "(beats per-role context_window_tokens)"))


def run_tui() -> int:
    """Entry point — runs the REPL until /quit or EOF."""
    _bootstrap()

    # Working directory: a persisted setting wins (set via /workdir), but
    # otherwise default to the directory the launcher was invoked from. We
    # don't persist the cwd default — moving the program elsewhere should
    # follow the new cwd, not pin the old one.
    state: dict = {
        "working_dir": get_config("working_directory", None) or os.getcwd(),
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

    # Surface auto-repair result so the user sees that REINDEX fired (and
    # whether it cleared the integrity issue). Silent when quick_check
    # passed at startup, which is the common path.
    from agentcommander.db.connection import last_auto_repair as _last_repair
    repair = _last_repair()
    if repair is not None:
        if repair.get("fixed"):
            render_system_line(style("muted",
                f"  db auto-repair: REINDEX cleared a quick_check issue "
                f"(was {repair['before']!r})"))
        else:
            render_system_line(style("warn",
                f"  db integrity issue: {repair.get('before')!r} → {repair.get('after')!r}"))
            render_system_line(style("muted",
                "    try /db check, then /db reindex / /db vacuum / /db reset"))

    # First-run flow: ask for the Ollama endpoint and persist it.
    if needs_first_run_setup():
        if not first_run_wizard():
            render_error("setup did not complete; you can configure manually with /providers add")
            # Continue into the REPL anyway so the user can recover.

    # After first-run setup, attempt to auto-assign roles using TypeCast scores.
    # Existing per-role overrides (is_override=1) are preserved.
    if list_providers():
        # Program-driven OR refresh: re-pick non-override OR roles from
        # the latest catalog so accumulated votes (from prior sessions
        # AND prior runs in this same session via the swap path) drive
        # which models get used. Manual /roles set pins survive.
        try:
            n_refreshed = _refresh_or_role_picks_from_catalog()
            if n_refreshed:
                render_system_line(style("muted",
                    f"  catalog-driven refresh: {n_refreshed} OR role(s) "
                    "re-pinned from latest votes"))
        except Exception:  # noqa: BLE001
            pass
        _run_startup_autoconfigure()
        _print_role_assignments()

    if resolve_role(Role.ORCHESTRATOR) is None:
        render_system_line("Orchestrator role unassigned. After adding a provider:")
        render_system_line("  /models <provider_id>          # see available models")
        render_system_line("  /roles assign-all <provider_id> <model>   # one-shot setup")

    # workdir may have changed during first-run/autoconfig; re-sync the bar.
    bar.set_workdir(state.get("working_dir"))

    # Resume the most recent chat for this project so the conversation
    # continues where the user left off. The user-view messages are
    # replayed on screen; the model-side scratchpad is hydrated lazily by
    # PipelineRun on the next prompt. /chat clear / /chat new break out
    # of the resume path explicitly.
    try:
        from agentcommander.db.repos import (
            list_conversations, list_messages,
        )
        recent = list_conversations()
    except Exception:  # noqa: BLE001
        recent = []
    if recent:
        most_recent = recent[0]
        state["conversation_id"] = most_recent.id
        try:
            from agentcommander.db.repos import set_active_conversation_id
            set_active_conversation_id(most_recent.id)
        except Exception:  # noqa: BLE001
            pass
        try:
            past_msgs = list_messages(most_recent.id)
        except Exception:  # noqa: BLE001
            past_msgs = []
        if past_msgs:
            render_system_line(style("muted",
                f"  resuming chat {most_recent.id[:8]} "
                f"({len(past_msgs)} message(s)) — "
                "use /chat list to switch, /chat clear to start fresh"))
            for m in past_msgs:
                if m.role == "user":
                    render_user_message(m.content)
                elif m.role == "assistant":
                    render_assistant_message(m.content, markdown=True)

    # Seed the bar's context cap AFTER autoconfig has run, so we read this
    # session's freshly persisted ceiling (not whatever stale value was in
    # the DB from a previous launch). Precedence matches _on_role_start:
    # /context override first, then the session ceiling. Without this, the
    # idle bar would show no cap until the first role call fires.
    seed_cap: int | None = None
    persisted_ctx = get_config("context_override_tokens", None)
    if isinstance(persisted_ctx, (int, str)):
        try:
            n = int(persisted_ctx)
            if n > 0:
                seed_cap = n
        except (TypeError, ValueError):
            pass
    if seed_cap is None:
        ceiling = get_config("session_ceiling_tokens", None)
        if isinstance(ceiling, (int, str)):
            try:
                n = int(ceiling)
                if n > 0:
                    seed_cap = n
            except (TypeError, ValueError):
                pass
    if seed_cap is not None:
        bar.set_context(cap_min=seed_cap)

    # Seed the OR Paid balance so it's visible before the first role call.
    try:
        _refresh_or_paid_balance(bar)
    except Exception:  # noqa: BLE001
        pass

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

    # Free VRAM before goodbye: ask each provider to unload any models it's
    # holding. The Ollama provider hits /api/ps + /api/generate keep_alive=0;
    # llama.cpp and others inherit the no-op base method. Best-effort —
    # network failures here shouldn't stop the user from exiting.
    try:
        from agentcommander.providers.base import list_active
        total = 0
        for p in list_active():
            try:
                total += p.unload_all_loaded()
            except Exception:  # noqa: BLE001
                continue
        if total > 0:
            writeln(style("muted", f"  unloaded {total} model(s) from memory"))
    except Exception:  # noqa: BLE001
        pass

    writeln(style("muted", "  goodbye."))
    return 0


# Suppress unused-imports referenced through indirection
_ = (PALETTE, set_config)
