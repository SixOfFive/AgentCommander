# AgentCommander — braindump.md

**Single source of context for any future Claude instance after a compaction event.** Read it first; trust it; update it whenever the project's shape changes.

Repo: `C:\Users\sixoffive\Documents\AgentCommander` · Remote: `github.com/SixOfFive/AgentCommander` (main was force-pushed earlier in the project to overwrite the prior TypeScript rewrite — that history is gone). Branch is `main` only — work has been pushed directly throughout.

## What this is

A local CLI multi-agent LLM orchestration tool. **Pure-Python stdlib only — zero runtime dependencies.** Forks the *internals* of EngineCommander (`C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander`, read-only reference) but drops everything web-shaped: no marketplace, no rentals, no SSO, no `/v1/*` proxy, no multi-tenant, no PWA. Single user, single machine, one Python process.

User's philosophy verbatim: **"one computer, one LLM, one army of agents."** The 19 agent roles all run on whichever model the user picked; per-role specialization is opt-in.

## Hard constraints (do NOT violate)

- **stdlib only** — no `httpx`, no `pydantic`, no `rich`, no `prompt_toolkit`. Use `urllib`, `dataclasses`, ANSI escapes, `sqlite3`, `argparse`, `re`. Provider streaming is `urllib.request.urlopen` line-iterating an HTTP body.
- **Serial only** — no `parallel` action, no async. Pipeline runs one role/tool at a time. TUI thread + worker thread is the only concurrency, and that's solely so `/stop` can fire mid-run.
- **Highly modular** — providers, tools, and guard families all self-register through Protocol-based registries. Adding one is "drop a `.py` and call `register(...)` at module top-level."
- **No credentials in source** — endpoints + api keys live ONLY in the project-local SQLite DB (gitignored). DB path: **`<cwd>/.agentcommander/db.sqlite`** — project-local since the corruption incident; `%APPDATA%` is now used only for the TypeCast catalog cache.
- **Auto-commit hook is on** — `.claude/settings.json` has a `PostToolUse` hook on `Edit|Write|MultiEdit|NotebookEdit`. Every Edit/Write fires `git add -A && git commit`. Never push to remote without confirming.
- **The TS rewrite has been wiped** — not scope. Don't try to bring back Electron/TypeScript. Python is the only target.
- **llama.cpp gets minimal-surface treatment** — single model per process. Never tell it to unload; signature parity only on `should_cancel`/`unload`/`list_loaded_details`. Ollama is the primary provider.
- **Slash commands and their output are screen-only** — they never enter the `messages` table or the `scratchpad_entries` table. Confirmed by the user as a hard requirement.

## Architecture map (`src/agentcommander/`)

| Layer | Files | Purpose |
|---|---|---|
| Entry | `cli.py`, `__main__.py` | argparse → `tui.app.run_tui()`; catches `DBAlreadyOpen` with friendly message |
| Types | `types.py` | `Role` enum (19 values), `ProviderConfig`, `OrchestratorDecision`, `ScratchpadEntry` (with `message_id` + `replaced_message_ids`), `LoopState`, `PipelineEvent` |
| Safety | `safety/` | `dangerous_patterns.py`, `sandbox.py`, `host_validator.py`, `prompt_injection.py` |
| Agents | `agents/manifest.py`, `agents/prompts.py` (loads `resources/prompts/{ROLE}.md`) |
| DB | `db/connection.py` (locks + auto-repair + signal handlers), `db/schema.sql`, `db/repos.py` |
| Providers | `providers/base.py`, `ollama.py`, `llamacpp.py`, `bootstrap.py` |
| Tools | `tools/dispatcher.py`, `file_tool.py` / `code_tool.py` / `web_tool.py` / `process_tool.py` |
| Engine | `engine/engine.py` (`PipelineRun.events()` generator), `actions.py`, `scratchpad.py`, `role_call.py`, `role_resolver.py` |
| Guards | `engine/guards/` — 9 families ported from EC + my additions: `decision`, `flow`, `execute`, `write`, `output`, `fetch`, `post_step`, `done` + shared `types.py`. ~110+ individual guards. |
| TypeCast | `typecast/catalog.py` (conditional-GET), `vram.py`, `autoconfig.py` (threshold-cascade picker, ban list, min-context filter) |
| TUI | `tui/app.py`, `setup.py` (first-run wizard + reusable `prompt_for_ollama_endpoint`), `commands.py` (slash registry), `render.py`, `markdown.py`, `status_bar.py`, `permissions.py`, `terminal_input.py`, `autocomplete.py`, `ansi.py` |

## Database (project-local SQLite, gitignored)

**Path: `<cwd>/.agentcommander/db.sqlite`** — each project has its own DB.

Tables:
- `migrations` (reserved)
- `config` (key-value JSON; holds `working_directory`, `context_override_tokens`, `session_ceiling_tokens`, `autoconfig_banned_models`)
- `providers` (id, type, name, endpoint, api_key, enabled, created_at)
- `role_assignments` (role PK, provider_id, model, is_override, **`context_window_tokens`**, updated_at)
- `conversations` (id PK, title, working_directory, created_at, updated_at, archived, pinned)
- `messages` (id PK, conversation_id FK, role, content, created_at) — **user view, never compacted**
- `token_usage`
- `model_hints` (TypeCast hint accumulator — wiring deferred)
- `audit_log`
- `pipeline_runs`, `pipeline_steps`
- `operational_rules` (preflight/postmortem — deferred)
- `fs_permissions` (path, operation, decision, scope=`exact`/`subtree`, created_at) — **subtree scope IS wired** via `_load_persisted` walking ancestors
- **`scratchpad_entries`** (id, conversation_id, run_id, step, role, action, input, output, duration_ms, content, **`message_id`** FK to messages, **`replaced_message_ids`** JSON array, **`is_replaced`** flag, timestamp, created_at) — model-side memory; **compactable**

### DB hardening (corruption defense — installed after a real corruption incident)

`init_db` does:
1. `PRAGMA journal_mode = WAL`, `synchronous = FULL`, `cell_size_check = ON`, `wal_checkpoint(TRUNCATE)` on open
2. **Single-instance lock** via `<dbpath>.lock` — `msvcrt.locking` on Windows, `fcntl.flock` on POSIX. Concurrent processes fail with `DBAlreadyOpen` (friendly cli.py message)
3. Idempotent ALTER for `context_window_tokens` column
4. Idempotent backfill of conversation titles from first user message
5. **Auto check + repair**: `PRAGMA quick_check` → if not ok, `REINDEX` + retry. Result stashed in `_last_auto_repair`, surfaced in startup banner
6. **Atexit + SIGINT/SIGTERM/SIGBREAK handlers** call `close_db()` which does final `wal_checkpoint(TRUNCATE)` + releases the lock — prevents the kill-during-checkpoint corruption that bit us originally

### `/db` command — full DB recovery surface

```
/db                    # path + size + integrity status
/db check              # full integrity_check report
/db reindex            # rebuild every index (often clears corruption)
/db vacuum             # rewrite the file (defragment)
/db backup <path>      # byte-copy via SQLite backup API (preserves corruption)
/db salvage <path>     # row-by-row export to fresh DB; skips unreadable rows
/db reset              # DESTRUCTIVE: archive corrupt DB to *.corrupt-NNN, init fresh
```

## Slash command registry

**16 unique commands** (with aliases). `/help <cmd>` shows full details for any of them; autocomplete popup surfaces them as you type `/`.

| Command | Sub-commands | Notes |
|---|---|---|
| `/help [<cmd>]` | — | Full registry table or per-cmd details |
| `/quit`, `/exit` | — | Exit; mid-run cancels + sets should_exit |
| `/clear` | — | ANSI screen wipe |
| `/stop` | — | Halt active pipeline; mid-run typing also recognized |
| `/workdir [<path>]` | — | Show/set sandbox dir |
| `/providers` | `add`, `test`, `rm` | DB-backed |
| `/models <pid>` | — | List provider's installed models |
| `/roles` | `set`, `unset`, `auto`, `assign-all` | Role/model bindings |
| `/typecast` | `refresh`, `autoconfigure` | Catalog status / re-fetch / dispatch |
| **`/autoconfig`** | `minctx <N>`, `ban <model>`, `unban <model>`, `bans`, `clear` | TypeCast picker; `minctx` filters + persists; `clear` re-prompts endpoint and rescans |
| **`/context [<N>|off]`** | — | Session-wide `num_ctx` override; persisted; warns when value exceeds picked models' training ctx |
| **`/vram`** | — | Detected total + Ollama `/api/ps` live + catalog estimates for non-loaded role models |
| **`/db`** | `check`, `reindex`, `vacuum`, `backup`, `salvage`, `reset` | DB inspect + recovery |
| **`/chat`** | `list`, `new [<title>]`, `clear`, `resume <id>`, `title <name>`, `export <path>` | Conversation manage; `clear` is destructive (wipes messages + scratchpad + screen); resume replays history |
| `/agents` | — | 19-role manifest |
| `/tools` | — | Registered tool verbs |
| `/history` | — | Recent conversations table |
| `/new [<title>]` | — | Start new conversation (legacy — `/chat new` is preferred) |

### Autocomplete popup (`tui/autocomplete.py` + `status_bar.read_line_at_bottom`)

- Custom char-mode input loop (replaced `input()`); raw_mode CM in `terminal_input.py`
- **Tier 1**: typing `/` → matches all top-level commands
- **Tier 2**: typing `/cmd ` → matches sub-commands from `SUB_COMMANDS` table (covers `/autoconfig`, `/typecast`, `/providers`, `/roles`, `/context`, `/chat`)
- **Tab** inserts the highlighted match (replacing only the trailing token, not the whole buffer)
- **Up/Down**: cycle popup highlight; when popup empty, navigate in-process input history
- **Esc**: dismiss popup, keep buffer
- **Enter**: submit
- Past the second token, no completion (free-form args)

## Key flows

### Startup sequence (`tui/app.py:run_tui`)

1. `_bootstrap()` — `enable_ansi()`, `init_db()` (acquires lock, runs auto-repair, signal handlers register), `bootstrap_tools()`, `bootstrap_providers()`, `refresh_catalog()`.
2. **Install StatusBar BEFORE banner** — sets ANSI scroll region; parks cursor at H-3.
3. `render_banner` — workdir at top, then logo, version, providers/models count.
4. **Auto-repair status banner line** — if `_last_auto_repair` is set, prints `db auto-repair: REINDEX cleared a quick_check issue …` (or warn variant).
5. First-run wizard if no providers (uses `prompt_for_ollama_endpoint` helper).
6. `_run_startup_autoconfigure()` — calls `apply_autoconfigure()`, picks per role via threshold cascade 100→10, persists `session_ceiling_tokens` config row.
7. `_print_role_assignments()` + `_print_session_context_summary` — role table + "session max context: 8k (set by command-r7b:7b)" line + active `/context` override line if set.
8. **Seed bar's context cap** from override / ceiling (after autoconfig, so we read fresh values).
9. **Resume most recent chat** — sets `state.conversation_id`, replays past `messages` to screen via `render_user_message` / `render_assistant_message`. Print line: `resuming chat <id> (N message(s)) — use /chat list to switch, /chat clear to start fresh`.
10. REPL loop.

### Pipeline (`engine/engine.py:PipelineRun.events()`)

```
events() generator:
  insert_pipeline_run
  _hydrate_scratchpad_from_db      ← cross-turn memory (loads prior, filters is_replaced)
  _maybe_compact_scratchpad        ← if hydrated text exceeds budget, summarizer compresses
  _classify_category(opts)         ← router; fires on_role_start/end
  _push_entry(router/classify)     ← persisted with message_id=user_msg.id
  for iteration in 1..max:
    _orchestrate(opts)             ← orchestrator; fires hooks
    decision-guards
    chat-coercion (action: "chat" → done with reasoning as input)
    if done:
      _handle_done                  ← runs done-guards
      if final is router-echo:
        if decision.input is meaningful: use it
        else: yield from _chat_fallback_stream  ← live streamed chat reply
      yield done
    if action in ROLE_ACTIONS: yield from _dispatch_role
    if action in TOOL_ACTIONS: yield from _dispatch_tool (catches PermissionDenied → friendly final)
    post_step guards
```

`_run_pipeline()` (app.py) runs `events()` in a worker thread; main thread polls events queue + char-mode stdin (for /stop, /exit, /quit, queued next prompt). `cancel_event` is checked at iteration boundaries AND inside the provider's `_post_stream` (mid-token cancellation).

### Compaction (`engine/engine.py:_maybe_compact_scratchpad`)

- Trigger: `compact_scratchpad(state)` text exceeds `session_ceiling_tokens × 4 × 0.5` chars (~50% of context budget)
- Action: keep last 6 entries verbatim, summarize older via `Role.SUMMARIZER` (`json_mode=False`), insert synthetic `system/compacted` row with `replaced_message_ids = [...]`, mark originals `is_replaced=1`
- Yields `guard/compaction` events at start AND end so user sees `⌫ guard:compaction (compacting 14 prior scratchpad entries via summarizer …)` instead of silent pause
- Failure-graceful: summarizer unassigned/errored → "keeping originals", run proceeds uncompacted

### Role resolution + num_ctx precedence (`engine/role_resolver.py`)

```python
def resolve(role) -> ResolvedRole | None:
    session_ctx = _session_context_override()    # /context override
    ceiling     = _session_ceiling_tokens()      # autoconfig-derived

    # 1. DB role override (set by /roles set or /autoconfig minctx)
    a = get_role_assignment(role)
    if a:
        per_role_ctx = a["context_window_tokens"]
        ctx = session_ctx if session_ctx else (per_role_ctx if per_role_ctx else ceiling)
        return ResolvedRole(..., kind="override", context_window_tokens=ctx)

    # 2. In-memory autoconfig
    pair = _autoconfig.get(role)
    if pair:
        ctx = session_ctx if session_ctx else ceiling
        return ResolvedRole(..., kind="auto", context_window_tokens=ctx)
    return None
```

**num_ctx precedence (highest → lowest):** `/context` override → per-role `context_window_tokens` → session ceiling → None (provider default). The session ceiling falling-through means models get the announced cap by default instead of Ollama's 2048/4096.

### TypeCast autoconfig (`typecast/autoconfig.py`)

- **Threshold cascade**: for each role, find best-scoring installed model. Walk thresholds 100 → 10 (step −10). First threshold that's met wins. Roles with no qualifying model → `unset_roles` (typical for `vision`/`audio`/`image_gen` on a text-only stack).
- **Ban list**: `BANNED_MODELS_CONFIG_KEY = "autoconfig_banned_models"` in config table. `build_candidates` filters bans before any picker sees them.
- **Min-context filter**: `apply_autoconfigure(min_context=N)` — drops candidates with `contextLength < N`, persists picks to DB with `context_window_tokens = N`.
- **Session ceiling**: `min(contextLength)` across distinct picked models, persisted as `session_ceiling_tokens`, used by both display and resolver.

### Filesystem permissions (`tui/permissions.py`)

- `Operation` literal: `"read" | "write" | "delete" | "execute"`
- **Subtree scope wired**: `_load_persisted(path, op)` does exact match first, then walks `path.parents` looking for `scope='subtree'` rows
- `grant_subtree(path, op, decision)` is a public helper used to pre-authorize sandbox dirs
- Non-TTY: auto-deny (no silent exfiltration)
- `request_permission` keyed on the cwd for `execute` (not the tempfile path), so a single Always-allow covers a project

### TUI layout (3 reserved bottom rows)

```
1 .. H-3   scroll region (cursor parks at H-3 so writes scroll UP)
H-2        thin separator rule
H-1        live status, RIGHT-ALIGNED:
           ▸ role → model  ·  in N out N  ·  ctx now/max [████░░]  ·  run mm:ss  ·  total mm:ss
H          input prompt "❯ "
```

- **Workdir is in the banner area at the top** — moved out of the bottom row for future expansion.
- **Status row fields**: role+verb (`▸` running / `·` idle), token totals (cumulative), `ctx now/max` with a fill bar (green<60%<yellow<85%<red), run timer (elapsed for current run), total timer (session-cumulative).
- **`context_now` clears when run ends** — `set_running(False)` zeros it so post-run display is `ctx —/8.2k` instead of stale "7.8k/8.2k" with red bar.
- **Bar timer ticks once per second** during a run from inside the run loop (calls `bar.redraw()` even when no engine event arrives).
- **Bar uninstall parks cursor at last row** — so shell prompt appears below content on exit, not overwriting it.
- **`writeln` clears to end-of-line** — `s + "\x1b[K\n"` — prevents residual chars from previous content bleeding through when new content is shorter.

## Slash command + chat semantics (the user's mental model)

User explicitly stated:
1. **Two views per conversation:**
   - User view = `messages` table = full fidelity, never compacted
   - Scratchpad = `scratchpad_entries` table = model-facing, can be compacted; entries link to user view via `message_id`
2. **Compaction only touches the scratchpad side.** Messages stay readable forever.
3. **Slash commands and their output are screen-only.** Never enter `messages` or `scratchpad_entries`. Confirmed by inspecting DB after a session — `/help` and `/agents` produce no DB rows.
4. **Startup auto-resumes the most recent chat for the project.** `/chat new` starts fresh without deleting; `/chat clear` deletes current + clears screen.
5. **Project-local DB.** Each working directory has its own state. `AgentTesting/.agentcommander/db.sqlite` is separate from the project root's DB.

## Resources

- `resources/prompts/*.md` — 19 role system prompts. Loaded by `agents/prompts.py:get_role_prompt`. **The orchestrator's prompt + every role's prompt now gets the live tool-registry appendix** appended at call time so models can answer "what tools do you have?" honestly.
- The orchestrator additionally gets a self-introspection directive ("answer DIRECTLY with `{action: done, input: <list>}`, do NOT delegate to research/plan/architect/coder").

## Launchers

- `ac.bat` — Windows. **MUST be CRLF**. Uses `>nul 2>nul`. Sets `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `PYTHONPATH=src`. Resolves Python via `py -3` first, then `python`, then `python3` (matches `code_tool._resolve_python_cmd`).
- `ac.sh` — POSIX. LF endings.

## Bugs found and fixed in this session

| Bug | Symptom | Fix |
|---|---|---|
| A | `echo_request_guard` rejected factual Q&A whose answer naturally repeats question words ("Paris" rejected as echo) | Short-circuit on factual prefix at iter 1 with no tool work |
| B | `capabilities_list_guard` rejected valid capability questions | Skip when user message asks about tools/capabilities |
| C | `build_final_output` ignored `list_dir` / `read_file` / `fetch`; failed-execute showed raw step echo | Surface those outputs as `**Directory listing**` / `**File contents**` / `**Fetched content**` / `**Execution failed**` blocks; exclude router from step list |
| D | "halted by user" wording on auto-deny (non-TTY) | Neutral "halted: permission denied for X Y" |
| E | No final synthesis on permission denial | Yields `done` event with concrete next-steps |
| F | `has_deliverable` required >100 chars for fetch (excluded compact JSON) | Any successful fetch counts; same for `list_dir`/`read_file`/`write_file` |
| G | Every conversation titled "Conversation" → `/history` unscannable | Auto-derive from first user message + idempotent backfill |
| H | `multi_step_guard` rejected comprehensive multi-part chat answers | Skip when `decision.input` ≥ 80 chars |
| I | False positive (turned out to be capture artifact) | — |
| J | Model uses `execute`-only when user named a file → file never written | New `unwritten_file_guard` with **fire-once** gating |
| Corruption | `Tree X page Y: btreeInitPage error 11` | Single-instance lock + `synchronous=FULL` + `cell_size_check` + atexit/signal handlers + auto-repair on startup |
| ctx stale | After run ends, bar showed `ctx 7.8k/8.2k [████]` (stale) | `set_running(False)` clears `context_now`; post-run shows `ctx —/8.2k` |
| `--mincontext` | User wanted shorter syntax | Renamed to `minctx`; `--mincontext` / `--min-context` kept as aliases |

## What's complete

- All hard constraints satisfied
- 19-role manifest + system prompts
- Project-local SQLite DB with corruption defense (lock + auto-repair + signals)
- Ollama + llama.cpp providers (Ollama: keep_alive=5m, /api/ps, unload-on-exit, should_cancel mid-stream)
- Tool dispatcher + file/code/web/process tools
- `code_tool` resolves Python via `py -3` first on Windows (no more 9009)
- Engine main loop with all 9 guard families wired
- ~110+ guards including new `unwritten_file_guard` and Bug A/B/H/J fixes inside existing ones
- TypeCast catalog (conditional-GET) + threshold-cascade autoconfig + ban list + min-context filter
- Cross-turn scratchpad persistence (`scratchpad_entries` table) + hydration + compaction
- RoleResolver with `/context > per-role > session_ceiling > None` precedence
- Bottom-anchored TUI with scroll region + status bar (workdir up top; bar shows role/tokens/ctx-fill-bar/run-timer/total-timer)
- ANSI markdown renderer with `\x1b[K` line-clearing
- Streaming tokens via on_role_delta with auto-indent
- Filesystem permissions with subtree scope wired
- /stop / /exit / /quit cancel mid-stream + unload models
- Auto-commit hook installed and active
- 16 slash commands with autocomplete (top-level + sub-commands), tab insertion, history nav
- Chat fallback (with scratchpad context block) for bare-scratchpad / router-echo finals
- Self-introspection: tool registry injected into every role's system prompt
- Startup chat resume + `/chat list / new / clear / resume / title / export`
- `/db` and `/vram` and `/context` commands
- `/autoconfig` with minctx + ban/unban/bans + clear-with-endpoint-reprompt
- 20/20 unit tests pass

## What's deferred (NOT done)

- **Preflight + postmortem meta-agents** — `operational_rules` table exists but `apply_preflight` / `apply_postmortem` not wired
- **TypeCast hint accumulator** — `model_hints` table exists, no engine code bumps `(model, role) ± 0.1`
- **Browser tool / image gen / git tool / http tool / env tool** — only file/code/web/process exist
- **OpenRouter / Anthropic / Google providers** — only Ollama + llama.cpp
- **Model registers as a streaming role for orchestrator** — orchestrator + chat fallback don't display token-by-token, just the final result. Roles dispatched via `_dispatch_role` DO stream live (existing behavior)

## Common gotchas (learned the hard way)

- **DB corruption from concurrent processes** — pre-lock days, parallel test runs got SQLite torn between WAL and main pages. Lock file + signals fix this. If a `.lock` file exists, a fresh process cleans it up via `flock` reuse — no stale-lock concern.
- **Ollama `size_vram` in `/api/ps` includes offloaded layers** — `/vram` may show "loaded 14.3 GB" against a 6 GB GPU. Documented behavior, not our bug.
- **`ac.bat` line endings** — Python `Path.write_text` writes LF; cmd.exe needs CRLF. The Edit tool preserves CRLF if it's already there. Bash sandbox rewrites `>nul` → `>/dev/null` in heredocs — write CRLF + `>nul` from Python directly.
- **Windows stdout cp1252** — `enable_ansi()` reconfigures stdout to UTF-8 + sets console code page to 65001. `PYTHONUTF8=1` in `ac.bat` is belt+suspenders.
- **Scroll region quirks** — content scrolls when newline emits at row H-3. Writes outside the region just sit there. Always save/restore cursor (`\x1b7`/`\x1b8`) when status-bar painting touches reserved rows.
- **`urllib.error.HTTPError` for 304** — conditional GET catches this explicitly, returns `not_modified=True`. Don't treat 304 as transport failure.
- **Bash sandbox uses POSIX paths but app runs on Windows** — when testing the launcher, use `cmd.exe //c ".\ac.bat"` from bash.
- **Background-task captured output can be truncated** — observed during testing. Always cross-check against the DB if a captured transcript looks weird.
- **Models hallucinate JSON output** — small/medium local models sometimes emit markdown when they should emit JSON actions. The chat-action coercion (`{"action":"chat"}` → `done`) and chat fallback paper over the common cases. For "write X then run X" prompts on weak orchestrators, the new `unwritten_file_guard` catches the gap.
- **Session ceiling is real** — when the banner says "session max context: 8k", every role gets called with `num_ctx=8192` by default now (after the deepseek-context fix). If you want a higher cap, `/context 32k`. If you want per-role differentiation, `/autoconfig minctx N`.
- **Slash-command output is intentionally NOT in DB** — confirmed by user as a hard requirement. Don't accidentally append to `messages` from slash handlers.

## Recent decisions worth remembering

- **DB is project-local** — switched from `%APPDATA%/AgentCommander/agentcommander.sqlite` to `<cwd>/.agentcommander/db.sqlite`. Catalog cache stays global.
- **Single-instance lock** — concurrent processes refused with friendly message. Trade-off: can't run two `ac.bat` against the same project. Different projects (different cwd) have different DBs and don't conflict.
- **`synchronous=FULL`** — durability over speed for the kind of workload AC has (small writes, infrequent).
- **`/autoconfig minctx`** — flag renamed (no dashes); old `--mincontext` kept as alias.
- **`/chat clear` is destructive** — deletes messages + scratchpad + conversation row. Use `/chat new` to start fresh while keeping old ones.
- **Auto-resume on startup** — most recent chat for the project DB loads automatically. Past messages re-render. State `conversation_id` is set so next prompt continues that chat.
- **Subtree permissions wired** — `grant_subtree(path, op, "allow")` pre-authorizes a directory tree. AgentTesting/ has subtree allow for write/execute/read/delete persisted, so non-TTY tests work end-to-end there.

## User preferences (do these by default)

- **Commit on every change** — auto-commit hook is on; don't disable.
- **Modular by default** — new feature → its own module under appropriate package, self-register.
- **Pure stdlib** — never reach for a pip dep.
- **Drive through the TUI** — when verifying behavior, use `ac.bat` + slash commands. Don't `curl` Ollama directly.
- **Confirm before destructive ops** — force-push, drop-table, rm -rf.
- **Push to main directly** — the user pushes through main; no PR workflow set up. (Don't try to create PRs from main → main; gh CLI isn't installed anyway.)

## Pointers

- Memory directory: `C:\Users\sixoffive\.claude\projects\C--Users-sixoffive-Documents-AgentCommander\memory\` — see `MEMORY.md`.
- EngineCommander upstream (read-only): `C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander`. Port from here when adding features.
- TypeCast: `https://github.com/SixOfFive/TypeCast` — `models-catalog.json`. Conditional-GET on every startup.
- User's email: `hvr.biz@gmail.com` (from auto-memory).

## Sanity-check checklist on resume

1. `cd C:\Users\sixoffive\Documents\AgentCommander && PYTHONUTF8=1 PYTHONPATH=src py -3 -m unittest discover tests` → 20 OK
2. `git status --short` → clean (auto-commit fires every Edit/Write)
3. `git remote -v` → `origin` points at `github.com/SixOfFive/AgentCommander`
4. `git log --oneline origin/main..HEAD` → empty (everything pushed)
5. `cmd.exe //c ".\ac.bat --version"` → `AgentCommander 0.1.0`
6. `echo "/db" | timeout 30 ./ac.bat 2>&1 | head -c 500` → `integrity: ok (quick_check)` somewhere
7. `echo "/chat list" | timeout 30 ./ac.bat 2>&1 | head -c 500` → table of recent chats with `*` on active
8. `echo "what is 2+2" | timeout 60 ./ac.bat 2>&1 | tail -c 500` → `4` as the assistant final
9. `cat .gitignore | grep -E "agentcommander|AgentTesting"` → both ignored

## File-level reading order (for the next Claude)

When debugging or extending, open in this order:

1. `braindump.md` (this file) → high-level state
2. `src/agentcommander/types.py` → all dataclasses
3. `src/agentcommander/db/schema.sql` → DB shape
4. `src/agentcommander/db/connection.py` → init + locks + auto-repair
5. `src/agentcommander/engine/engine.py` → pipeline loop, chat fallback, compaction
6. `src/agentcommander/engine/role_resolver.py` → num_ctx precedence
7. `src/agentcommander/tui/app.py:run_tui` → startup sequence + REPL
8. `src/agentcommander/tui/commands.py` → slash registry
9. `src/agentcommander/typecast/autoconfig.py` → role-picking logic

Last updated end of session covering: chat resume + /chat family + minctx rename + ctx-clear-on-end + corruption defense + permissions subtree + many guard fixes + project-local DB.

---

# RESUME-AFTER-COMPACTION (rounds 11–20 + popout feature)

This block is the latest. Read it first if you're a future Claude resuming after compaction.

## Where I'm leaving off — round-20 done; not yet successfully run end-to-end

Round-20 file `AgentTesting/stress_test_20_real_models.py` is now CORRECT in design and should run end-to-end against the user's real models. **Do NOT introduce DB copying or `upsert_provider` from snapshot data when refactoring.** The user's OR api_key must stay in `AgentTesting/.agentcommander/db.sqlite` (gitignored three different ways). Audit confirms nothing sensitive is tracked.

**HOW TO RUN**: from inside `AgentTesting/` (this is how the user invokes — `cd AgentTesting && py -3 stress_test_20_real_models.py`). The script uses `Path.cwd()` for the DB path; it does NOT chdir. So if you `cd` to AgentTesting/ first, it opens `AgentTesting/.agentcommander/db.sqlite` (the real DB with their providers + api_key). If you run from project root, it opens the empty project-root DB and almost everything SKIPs.

```
cd AgentTesting
PYTHONUTF8=1 PYTHONPATH=../src py -3 stress_test_20_real_models.py 2>&1 | tee round20_output.log
```

**State of round-20 file:**
- ✅ Uses `Path.cwd()` for the DB path — respects whatever directory user invokes from
- ✅ Opens user's real DB via `init_db()`, no copy, no replication, api_key never moves
- ✅ `bootstrap_providers()` called after `init_db()` (right ordering)
- ✅ `apply_autoconfigure()` runs to populate in-memory role bindings; falls back to audit-log-derived bindings if catalog doesn't recognize installed models
- ✅ `GLOBAL_BUDGET_S=600` / `_budget_cancel()` callable available; `section()` prints remaining time at every section header
- ✅ Every `create_conversation(...)` wrapped with `_track(...)` — atexit hook deletes test data on exit (cascades to messages + scratchpad + pipeline_runs + pipeline_events via FK + the explicit pipeline_events sweep we added in `delete_conversation`)
- ✅ Section H trimmed to ONE representative pipeline (H.76, max 60s); H.77–H.85 SKIP cleanly with a note explaining why
- ✅ Clean failure path: if user's primary `ac` is running and holds the DB lock, the script exits with code 2 + a clear "stop primary first" message
- ⏳ **NOT YET TESTED end-to-end against the real 4070** — every previous attempt died for unrelated reasons (DB at wrong path, factories not bootstrapped, runaway killed by user). The infrastructure should now be right; first run should succeed.

To continue: just run it. If it hits a real bug, fix it. If it works cleanly, report results to the user.

## Tests added this session (rounds 11–19, all stress-tested, all passing)

| Round | Focus | Findings |
|---|---|---|
| 11 | Provider net failures + scratchpad corruption + WAL pressure | 1 bug: `validate_provider_host` accepted `ftp://` → fix added ftp to provider reject list + null-byte rejection |
| 12 | Bootstrap idempotency, prompt injection edges, audit load | 1 bug: `validate_user_host("http://localhost")` slipped past — localhost regex was `^\s*localhost`, fixed to `(^\|//)\s*localhost` |
| 13 | Write atomicity, dispatcher edges, term_size weirdness | 3 bugs: `write_file` was non-atomic (data loss); `user_wants_action(None)` crashed; `OllamaProvider.list_models()` raised raw `JSONDecodeError`. All fixed. |
| 14 | 50-test wide sweep | 1 bug: percent-encoded localhost (`%6c%6f%63...`) bypass — added URL-decode pass in host validator |
| 15 | Long-running / sustained pipeline + popout stress | 1 bug: stale `_streaming_state` survived across pipeline runs after Ctrl-C / crash → `reset_render_state()` added in app.py's `_run_pipeline` |
| 16 | Dispatcher cancel/panic, scratchpad compact, role assignment integrity | 1 bug: `init_db_readonly` returned existing `_db` without checking it was actually readonly + same path → guard added |
| 17 | Model-interaction edges + guard gap audit | 4 bugs: `chat()` `AttributeError` on non-dict stream chunks (added `isinstance(chunk, dict)` check); `num_ctx` accepted garbage (added validator: positive int ≤ 16M); negative token counts poisoned EMA (`_safe_token_count` clamps); Retry-After parser brittle (added HTTP-date support + clamps negatives via new `_parse_retry_after`). 6 architectural WARNs catalogued. |
| 18 | 50 tests across 10 new categories | **TOOL SCHEMA ENFORCEMENT IMPLEMENTED** — `_validate_payload` in `tools/dispatcher.py` checks every payload against the descriptor's `input_schema` (object/string/integer/number/boolean/array/null + required/properties/enum/min/max). Bug: `delete_conversation` left dangling `active_conversation_id` → fixed |
| 19 | Fresh exploratory sweep | 3 fixes: scratchpad now sanitizes ANSI + control bytes at write time (`sanitize_scratchpad_text` runs in `_push_entry`); role-label mimicry pattern added to `prompt_injection.py` (catches `▸ orchestrator`, `▶ researcher-N`, `● AgentCommander`); `_apply_dict_to_state(None)` no longer crashes |

**Cumulative**: ~700 stress tests + 109 unit tests, all passing. ~36 real bugs fixed across rounds.

## NEW FEATURE: collapsible role popouts (`tui/popouts.py`)

Major TUI feature, implemented in worktree `feature/role-popouts` and merged. User's spec verbatim:

- Each sub-agent role-call (researcher, coder, reviewer, …) gets its own collapsible block
- Streaming text visible WHILE running; on `done` the block snaps to a single summary line: `▶ researcher-2 [12.3s · 2,847 tok · ok]`
- Failed roles stay EXPANDED (so the user sees the error inline)
- Tool calls inside a role stay visible regardless of collapse state
- Three interaction surfaces, all working: **mouse click** (xterm SGR mouse mode), **keyboard** (Tab/Shift-Tab cycle, Space/Enter toggle, Esc blur), **slash command** (`/popout <id>` / `/popout list` / `/popout expand|collapse all`, alias `/po`)
- Block IDs are `<role>-<n>` (1-indexed per pipeline run, reset every turn)
- Mirror viewers reconstruct independently from `pipeline_events`; each viewer has its own collapse state
- Replay/resume synthesizes collapsed blocks from historical role events

**Files added**:
- `src/agentcommander/tui/popouts.py` — `PopoutBlock`, `PopoutRegistry`, lifecycle, line-counting, summary formatting, cursor-up + erase-to-end collapse render
- `src/agentcommander/tui/mouse_input.py` — xterm SGR mouse mode enable/disable + parser
- `tests/test_popouts.py` — 38 unit tests

**Files modified**:
- `tui/render.py` — `render_role_delta` opens popout for sub-agents; `_render_event` finalize+collapse on role/error
- `tui/app.py` — keyboard nav in `_consume_input_chunk`, mouse parser dispatch, mode setup/teardown around `bar.install()`, click handler in bottom-prompt input loop too
- `tui/mirror.py` — same role/end + error hooks; mouse clicks toggle local registry
- `tui/commands.py` — `/popout` slash command registered with `/po` alias

**Known limitations** (not bugs):
- Mouse click toggles the *most-recent* (or focused) block, not the specific block under the cursor — registry doesn't track row positions across scrolls. Slash + keyboard remain precise.
- Cursor-up + erase only works for blocks still in the viewport. Blocks scrolled past the top get marked `in_viewport=False` and the summary appends below; slash command can re-print content fresh.

## API key safety — confirmed clean

`git ls-files | grep -iE "(\.sqlite|\.agentcommander|api_key|credential)"` → empty. `git check-ignore -v AgentTesting/...` → all matches against `.gitignore:46:AgentTesting/`. The `sk-or-` strings in tracked source code are all regex literals in `safety/dangerous_patterns.py`, `engine/guards/output_guards.py`, and the OR setup wizard — never a real key. **The user's OR api_key is at `AgentTesting/.agentcommander/db.sqlite` and that path is gitignored three different ways (`AgentTesting/`, `*.sqlite`, `.agentcommander/`).**

## Cleanup done this session

- Deleted stray empty `.agentcommander/` at the repo root (created by stress tests pre-tempfile-confinement; had 0 conversations / 0 providers / 2 stale config rows).
- All stress tests rounds 10–19 now create their tempdirs INSIDE `AgentTesting/` via `tempfile.mkdtemp(prefix="ac-stressNN-", dir=str(_test_root))`. Source tree stays untouched.
- Round 20 is being further changed (in progress) to NOT use a tempdir at all — the user wants it to run against the real DB directly so api_keys never leave their gitignored home.

## Real-model testing (round 20) — current status

**Goal**: hit the user's actual Ollama daemon at `http://192.168.15.103:11434` (where the 4070 lives) and exercise their autoconfig'd role/model pairings.

**Iteration count: 5 attempts so far**, each fixed the next layer:
1. Test ran from project root → empty DB → 0 providers
2. Added readonly snapshot reader → still 0 (path was project root which is empty; real DB is in `AgentTesting/.agentcommander/`)
3. Wrong path order → DB not init when bootstrap_providers ran
4. Fixed order — providers all 4 healthy ✓ — but `apply_autoconfigure` returned 0 picks because the test env's TypeCast catalog doesn't recognize the user's installed models (`cogito:8b`, `devstral-small-2:24b`, `gemma4:e2b`)
5. Added audit-log-derived bindings fallback (reads `event_type='role.call'` rows to discover what roles → models the user used in production). **The test process from this attempt was killed (PID 24712/21680) after running 5+ min — output never flushed because of `tee` buffering.**

The user's real role/model pairings discovered from the audit log:
- router → devstral-small-2:24b
- orchestrator → devstral-small-2:24b
- researcher → cogito:8b
- translator → gemma4:e2b

**To finish**: complete the rewrite (see "Where I'm leaving off"), enforce the budgets, then run.

## Open architectural-WARN items (round 17, deliberately deferred)

1. Spend cap for paid OR (not a bug — feature work, needs UI)
2. Provider hot-reload (low impact)

(Tool schema enforcement / scratchpad sanitize / role-label mimicry / prompt size cap — all FROM the round-17 catalogue have been resolved in rounds 18–19. See per-round table above.)

## Important: when you continue with tests

- The user invokes round-20 **from inside AgentTesting/** — that's where the real DB lives. The script uses `Path.cwd()` (no `os.chdir`) so wherever the user is when they invoke it IS the workdir. Earlier rounds 10–19 still create their own tempdirs under AgentTesting/ — they need fresh empty DBs and that's fine. Only round 20 uses the user's real DB directly.
- The user said: **"the changes you just made (copying the db) NEVER gets to github, that contains an api key for openrouter"** — confirmed safe by audit; the round-20 code does NOT copy the DB anywhere. **Do not introduce `upsert_provider` from snapshot data** when refactoring again — go directly through the user's gitignored real DB.
- The user is running `ac --mirror` in a separate window (PID 17148/10680 last we saw). It's read-only, never blocks anything. Don't kill it.
- Auto-commit hook fires on every Edit/Write — your changes commit themselves. Push only when explicitly asked.
- The runaway round-20 process (5+ min hammering the GPU) showed why hard timeouts matter — `_budget_cancel()` is now wired in, but if any future test ever calls `provider.chat()` without passing `should_cancel=_budget_cancel`, the budget won't enforce. Audit any new model-call site you add.
- AgentTesting/ files (including stress_test_*.py) are gitignored — when you Edit/Write them, the auto-commit hook will still fire but the changes never enter the index. They live only on disk. That's intentional: no test code or test DB ever lands in git.

## File map for new code (rounds 11–19 + popout)

```
src/agentcommander/tui/popouts.py        ← NEW: popout system
src/agentcommander/tui/mouse_input.py    ← NEW: xterm SGR mouse parser
src/agentcommander/tools/dispatcher.py   ← schema enforcement (_validate_payload)
src/agentcommander/providers/ollama.py   ← _safe_token_count, _parse_retry_after, num_ctx validation, non-dict chunk skip
src/agentcommander/providers/openrouter.py ← uses _parse_retry_after + _safe_token_count
src/agentcommander/engine/scratchpad.py  ← sanitize_scratchpad_text
src/agentcommander/engine/engine.py      ← _push_entry calls sanitize_scratchpad_text
src/agentcommander/safety/host_validator.py ← URL-decode pass for percent-encoded loopback bypass
src/agentcommander/safety/prompt_injection.py ← role-label mimicry pattern
src/agentcommander/db/repos.py           ← delete_conversation clears active id, prune_audit_log
src/agentcommander/db/connection.py      ← init_db_readonly state-confusion guard
src/agentcommander/tui/render.py         ← reset_render_state, popout integration
src/agentcommander/tui/app.py            ← popout keyboard nav, mouse dispatch, registry/render reset on each run
src/agentcommander/tui/mirror.py         ← popout reconstruction from events
src/agentcommander/tui/commands.py       ← /popout slash command
src/agentcommander/tui/status_bar.py     ← bottom-prompt mouse click handler, _apply_dict_to_state(None) tolerant
tests/test_safety.py                     ← +many regression tests
tests/test_popouts.py                    ← NEW: 38 unit tests
AgentTesting/stress_test_{10..20}.py     ← rounds 10–20, all confined to AgentTesting/
AgentTesting/round20_output.log          ← currently 0 bytes (last run was killed mid-flight)
```

