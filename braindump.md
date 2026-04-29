# AgentCommander — braindump.md

**This file is the single source of context for any future Claude instance after a compaction event.** Read it first; trust it; update it whenever the project's shape changes.

Repo: `C:\Users\sixoffive\Documents\AgentCommander` · Remote: `github.com/SixOfFive/AgentCommander` (main was force-pushed to overwrite the prior TypeScript rewrite — that history is gone).

## What this is

A local CLI multi-agent LLM orchestration tool. **Pure-Python stdlib only — zero runtime dependencies.** Forks the *internals* of EngineCommander (`C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander`, read-only reference) but drops everything web-shaped: no marketplace, no rentals, no SSO, no `/v1/*` proxy, no multi-tenant, no PWA. Single user, single machine, one Python process.

User's philosophy verbatim: **"one computer, one LLM, one army of agents."** The 19 agent roles all run on whichever model the user picked; per-role specialization is opt-in.

## Hard constraints (do NOT violate)

- **stdlib only** — no `httpx`, no `pydantic`, no `rich`, no `prompt_toolkit`. Use `urllib`, `dataclasses`, ANSI escapes, `sqlite3`, `argparse`, `re`. The provider streaming is `urllib.request.urlopen` line-iterating an HTTP body.
- **Serial only** — no `parallel` action, no async coordination. The pipeline runs one role/tool at a time. The TUI thread + a worker thread is the only concurrency, and that's solely so `/stop` can fire mid-run.
- **Highly modular** — providers, tools, and guard families all self-register through Protocol-based registries. Adding one is "drop a `.py` and call `register(...)` at module top-level."
- **No credentials or addresses in source** — endpoints + api keys live ONLY in the user-data SQLite, which is gitignored. The DB is at `%APPDATA%/AgentCommander/agentcommander.sqlite` (Windows) / `~/.local/share/agentcommander/agentcommander.sqlite` (Linux) / `~/Library/Application Support/AgentCommander/...` (macOS).
- **Auto-commit hook is on** — `.claude/settings.json` has a `PostToolUse` hook on `Edit|Write|MultiEdit|NotebookEdit` that runs `git add -A && git commit -m "auto: changes from claude"`. Every Edit/Write fires it. Never push to remote without confirming.
- **The TS rewrite has been wiped** — it is NOT scope. Don't try to bring back Electron/TypeScript. Python is the only target.

## Architecture map (`src/agentcommander/`)

| Layer | Files | Purpose |
|---|---|---|
| Entry | `cli.py`, `__main__.py` | argparse → `tui.app.run_tui()` |
| Types | `types.py` | `Role` enum (19 values), `ProviderConfig`, `OrchestratorDecision`, `ScratchpadEntry` (with `message_id` + `replaced_message_ids` for future compression), `LoopState`, `PipelineEvent` |
| Registry | `registry.py` | `Provider` / `ToolHandler` / `GuardFamily` Protocols + Registry helpers |
| Safety | `safety/` | `dangerous_patterns.py` (~30 regexes), `sandbox.py` (`is_path_within` + symlink-escape check), `host_validator.py` (strict + permissive), `prompt_injection.py` (18 patterns) — verbatim ports of EC's utils |
| Agents | `agents/manifest.py` (single source of truth for the 19 roles + defaults), `agents/prompts.py` (loads `resources/prompts/{ROLE}.md`) |
| DB | `db/connection.py` (sqlite3 stdlib), `db/schema.sql` (idempotent, all CREATE IF NOT EXISTS), `db/repos.py` |
| Providers | `providers/base.py` (`ProviderBase` + factory registry), `ollama.py`, `llamacpp.py`, `bootstrap.py` |
| Tools | `tools/dispatcher.py`, `file_tool.py` / `code_tool.py` / `web_tool.py` / `process_tool.py` |
| Engine | `engine/engine.py` (`PipelineRun.events()` generator), `actions.py`, `scratchpad.py`, `role_call.py`, `role_resolver.py` (DB override → in-memory autoconfig), `engine-output.py`/`scratchpad.py` build_final_output |
| Guards | `engine/guards/` — 9 families ported verbatim from EC: `decision`, `flow`, `execute`, `write`, `output`, `fetch`, `post_step`, `done` + shared `types.py`. ~110 individual guards. |
| TypeCast | `typecast/catalog.py` (conditional-GET with ETag/Last-Modified), `vram.py`, `autoconfig.py` |
| TUI | `tui/app.py`, `setup.py` (first-run wizard), `commands.py` (slash registry), `render.py`, `markdown.py` (minimal ANSI markdown), `status_bar.py`, `permissions.py`, `ansi.py` |

## Key flows

### Startup sequence (`tui/app.py:run_tui`)

1. `_bootstrap()` — `enable_ansi()` (UTF-8 stdout, VT mode on Windows), `init_db()`, `bootstrap_tools()`, `bootstrap_providers()` (registers ollama/llamacpp factories), `refresh_catalog()` (TypeCast conditional-GET).
2. **Install StatusBar BEFORE banner** — sets ANSI scroll region `\x1b[1;{H-3}r`, parks cursor at row H-3 so subsequent prints scroll up correctly.
3. `render_banner` — prints into the scroll region.
4. `needs_first_run_setup()` → if no providers, `first_run_wizard()` prompts for Ollama URL via `read_line_at_bottom`, persists provider, calls `rebuild_from_db()`.
5. `_run_startup_autoconfigure()` — calls `apply_autoconfigure()` which queries each provider's `list_models()`, runs TypeCast best-fit, returns a `dict[role_value, (provider_id, model)]`. Result is stashed in the in-memory `role_resolver._autoconfig` table — **never persisted**.
6. `_print_role_assignments()` — prints role → model table from `resolve_role()` (each row tagged `override` / `auto` / `unset`).
7. REPL loop: `read_line_at_bottom("❯ ")` → `_handle_input()` → either run slash command or `_run_pipeline()`.

### Role resolution (`engine/role_resolver.py`)

```python
def resolve(role) -> ResolvedRole | None:
    # 1. DB override (set by /roles set ... — every set is is_override=True)
    a = get_role_assignment(role)
    if a: return ResolvedRole(provider_id=a["provider_id"], model=a["model"], kind="override")
    # 2. In-memory autoconfig (recomputed every launch)
    pair = _autoconfig.get(role)
    if pair: return ResolvedRole(provider_id=pair[0], model=pair[1], kind="auto")
    return None
```

`call_role()` and the engine consult **only** `role_resolver.resolve` — never `get_role_assignment` directly.

### TypeCast catalog (`typecast/catalog.py`)

- URL: `https://raw.githubusercontent.com/SixOfFive/TypeCast/main/models-catalog.json`
- Cache: `<user_data>/typecast/models-catalog.json` + `models-catalog.meta.json` (etag, last_modified, fetched_at)
- Conditional GET sends `If-None-Match` + `If-Modified-Since`. 304 → keep cache (`source="cache-fresh"`). 200 → write new body + headers (`source="remote"`). Network fail → cache fallback (`source="cache"`). No cache → bundled (`source="bundled"`). None → empty.
- Autoconfig in `typecast/autoconfig.py:apply_autoconfigure` runs every launch in memory only.

### Pipeline (`engine/engine.py:PipelineRun.events()`)

Generator yields `PipelineEvent`s. Order per iteration: `iteration` → `_orchestrate()` (calls Role.ORCHESTRATOR with `json_mode=True`) → decision-guards → `done`?: done-guards → flow-guards → role/tool dispatch → post-step-guards. Threaded by `_run_pipeline()`; events flow back via `queue.Queue`. `cancel_event: threading.Event` is checked at iteration top + before tool dispatch — set by `/stop` or `cmd_stop`.

### TUI layout (3 reserved rows)

```
1 .. H-3   scroll region (messages; cursor parked at H-3 so writes scroll UP)
H-2        thin separator rule (dim grey)
H-1        live status, RIGHT-ALIGNED: "{verb} {role} → {model}  ·  in {N}  out {N}  ·  ctx {now} [{cap}]  ·  [{workdir}]"
H          input prompt "❯ " (where the user types)
```

`StatusBar` (`tui/status_bar.py`) owns rows H-2..H. Save/restore cursor on every `redraw()`. `read_line_at_bottom()` moves cursor to (H,1), clears, prints prompt, calls `input("")`, then parks cursor back at H-3.

### Filesystem permissions (`tui/permissions.py`)

Every file read/write/delete passes through `request_permission(path, op)`:
- Persisted "always allow"/"always deny" in `fs_permissions` table → return immediately
- Session-cached "yes once" → return
- Otherwise prompt: `[d] Deny  [t] Yes this time  [a] Always (persist)`
- Deny raises `PermissionDenied` — `tools/dispatcher.py` re-raises it; `engine/engine.py:_dispatch_tool` catches it and emits a `cancelled` event then returns. Pipe halts; future planned steps don't run.
- Non-TTY → automatic deny (piped runs can never silently exfiltrate).

### `/stop` (threaded pipeline)

`_run_pipeline` runs `run.events()` in a background thread, pushes events onto `queue.Queue`. Main thread polls `events_q.get(timeout=0.15)` and during empty timeouts calls `_poll_stdin_chunk()` (msvcrt on Windows / `select.select` on POSIX). If the user types `/stop\n`, `cancel_event.set()`. Engine checks `is_cancelled()` at iteration top + reports `cancelled by /stop`.

## Slash commands (live in `tui/commands.py`)

| Command | Notes |
|---|---|
| `/help [<cmd>]` | Detail block with usage + details + examples |
| `/quit`, `/exit` | Exit the TUI (no confirm) |
| `/clear` | ANSI screen clear |
| `/stop` | Halt active pipeline (also typeable mid-run via the keystroke poller) |
| `/workdir [<path>]` | Show or set sandbox dir |
| `/providers [add\|rm\|test]` | DB-backed providers (endpoint + api_key persisted in user-data DB) |
| `/models <pid>` | List installed models from a provider |
| `/roles` | Table of all 19 roles with kind=override/auto/unset |
| `/roles <role>` | One-role detail |
| `/roles set <role> <pid> <model>` | Override (persisted) |
| `/roles unset <role>` | Drop override; lets autoconfig re-pick |
| `/roles auto` | Re-run TypeCast autoconfig in memory; respects overrides |
| `/roles assign-all <pid> <model>` | Bulk-set every role as override |
| `/typecast [refresh\|autoconfigure]` | Status / re-fetch / dispatch to /roles auto |
| `/agents` | 19-role manifest table |
| `/tools` | Registered tool verbs |
| `/history`, `/new` | Conversation management |

## Database (sqlite3, stdlib, gitignored)

Tables: `migrations`, `config` (key-value JSON), `providers` (id, type, name, endpoint, api_key, enabled), `role_assignments` (role PK, provider_id, model, is_override always 1 in current usage), `conversations`, `messages`, `token_usage`, `model_hints` (TypeCast hint accumulator — wiring deferred), `audit_log` (every tool call + role call + guard block + permission decision), `pipeline_runs` + `pipeline_steps` (replay/inspector), `operational_rules` (preflight/postmortem — deferred), `fs_permissions` (persisted Always-allow/Always-deny).

## Resources

- `resources/prompts/*.md` — 19 role system prompts (copied verbatim from EC). Loaded by `agents/prompts.py:get_role_prompt`. Missing files fall back to a generic prompt.

## Launchers

- `ac.bat` — Windows. **MUST be CRLF**. Uses `>nul 2>nul` (NOT `>/dev/null` — bash sandbox auto-translates this; rewrite via Python `pathlib` to bypass). Sets `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` + `PYTHONPATH=src`.
- `ac.sh` — POSIX. LF endings. Same idea.

## What's complete

- Safety (4 modules, 20 unit tests passing)
- 19-role manifest
- DB layer (sqlite3) + repos
- Ollama + llama.cpp providers
- Tool dispatcher + file/code/web/process tools
- Engine main loop with guard hook points
- All 9 guard families (~110 guards total) ported and wired
- TypeCast catalog with conditional-GET freshness
- In-memory autoconfig (recomputed every launch, not DB-persisted)
- RoleResolver (DB override → in-memory)
- Bottom-anchored TUI with scroll region + status bar + bottom input
- ANSI markdown renderer for assistant output
- Streaming tokens via on_role_delta with auto-indent
- Token usage threaded through call_role → status bar
- Filesystem permission prompts with persisted Always
- /stop with threaded pipeline + non-blocking keystroke poll
- Auto-commit hook installed and active

## What's deferred (NOT done)

- **Preflight + postmortem meta-agents** — `operational_rules` table exists but `apply_preflight` / `apply_postmortem` not wired. EC has them at `EngineCommander/src/main/orchestration/{preflight,postmortem}.ts` + `utils/action-fingerprint.ts`. Engine has stub TODO comments at the dispatch points.
- **TypeCast hint accumulator** — `model_hints` table exists but no engine code bumps `(model, role) ± 0.1` per run. EC's logic is in `engine.ts` post-step.
- **Browser tool / image gen / git tool / http tool / env tool** — only file/code/web/process exist. EC has the others at `EngineCommander/src/main/tools/*.tool.ts`.
- **OpenRouter / Anthropic / Google providers** — only Ollama + llama.cpp implemented.
- **Compression of scratchpad messages** — `ScratchpadEntry.message_id` + `replaced_message_ids` fields exist (placeholder); no compression pass yet.
- **Live context-length tracking on the status bar** — fields exist (`StatusState.context_now` + `context_cap_min`), no engine-side hook updates them yet.

## Recent / in-flight decisions

- **Autoconfig is in-memory only** — was DB-persisted; refactored so the user can pull/remove a model and the next launch re-picks automatically. Only `/roles set` writes the DB now (always as `is_override=True`).
- **Force-pushed main** — wiped the upstream TS history; `main` now reflects the Python rewrite.
- **3-row TUI layout** — input row at the bottom, status row above it (right-aligned), separator rule above that. Scroll region cursor parks at H-3 so content scrolls UP from the bottom.
- **`message_id` placeholder added to ScratchpadEntry** — for future compression that replaces N originals with 1 synthetic summary while still showing originals when requested.

## Common gotchas (learned the hard way)

- **`ac.bat` line endings** — Python `Path.write_text` writes LF; cmd.exe needs CRLF. The Edit tool preserves CRLF if it's already there. The bash sandbox rewrites `>nul` → `>/dev/null` when it sees a redirection — write CRLF + `>nul` from Python directly, not via `cat <<EOF` in bash.
- **Windows stdout is cp1252** by default — non-ASCII (box-drawing chars in banner) errors with `UnicodeEncodeError`. Mitigated via `enable_ansi()` which calls `sys.stdout.reconfigure(encoding="utf-8")` AND sets the console code page to 65001. `PYTHONUTF8=1` in `ac.bat` is the belt + suspenders.
- **Scroll region quirks** — content scrolls when newline is emitted at the bottom row of the region. If you write to row outside the region, it sits there until manually overwritten. Always save/restore cursor (`\x1b7` / `\x1b8`) when status-bar painting touches reserved rows.
- **`input()` after status bar install** — moves the cursor naturally; we always re-park at H-3 after via `bar.park_cursor()`.
- **`urllib.error.HTTPError` for 304** — the conditional GET loop catches this case explicitly and returns `not_modified=True`. Don't treat 304 as a transport failure.
- **The bash sandbox in this dev environment uses POSIX paths but the actual app runs on Windows.** When testing the user's launcher, use `cmd.exe //c ".\ac.bat"` from bash.
- **Autoconfig "skipped: no installed model has a positive TypeCast score"** is normal when the user only has vision/specialized models. Not a bug — the catalog correctly refuses to recommend.
- **`get_role_assignment` returns the override row only** — never autoconfig picks. Use `role_resolver.resolve()` for the unified view.

## User preferences (do these by default)

- **Commit on every change** — auto-commit hook is on; don't try to disable it without explicit instruction.
- **Modular by default** — when adding a new feature, place it in its own module under the appropriate package and self-register if it's a tool/provider/guard.
- **Pure stdlib** — never reach for a pip dep. If something feels like it needs `requests`, use `urllib.request`. If something feels like it needs `rich`, use ANSI escapes.
- **Treat the TUI as the primary interface** — when verifying behavior, drive through `ac.bat` + slash commands. Don't `curl` Ollama directly to bypass the program (that's a documented mistake).
- **Confirm before destructive ops** — force-push, drop-table, rm -rf. The user said "do 1" once for a force-push; that doesn't generalize.

## Pointers to remembered files

- Memory directory: `C:\Users\sixoffive\.claude\projects\C--Users-sixoffive-Documents-AgentCommander\memory\` — see `MEMORY.md` index.
- EngineCommander upstream (read-only reference): `C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander` — port from here when adding features. The TypeScript was the source of truth for guards/agents/tools.
- TypeCast: `https://github.com/SixOfFive/TypeCast` — `models-catalog.json` is the per-model role-fit benchmark; we conditional-GET it on every startup.

## Sanity-check checklist on resume

1. `cd C:\Users\sixoffive\Documents\AgentCommander && py -3.14 -m unittest discover tests` → 20 OK
2. `cmd.exe //c ".\ac.bat --version"` → `AgentCommander 0.1.0`
3. `git status` → clean (auto-commit fires every Edit/Write)
4. `git remote -v` → `origin` points at `github.com/SixOfFive/AgentCommander`
5. `ac.bat` first line bytes → `b'@echo off\r'` (CRLF, ASCII, no BOM)
