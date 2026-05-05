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
- Tool dispatcher + file/code/web/process/**http/git/env/browser** tools
- `code_tool` resolves Python via `py -3` first on Windows (no more 9009)
- Engine main loop with all 9 guard families wired
- **~120+ guards** (round 22–28 added unknown_action, unassigned_role, prompt_template_leak,
  reviewer_verdict, tester_verdict; widened fetch_retry, repeated_tool_call,
  consecutive_nudge; reordered consecutive_nudge to fire first)
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
- **138/138 unit tests pass** (round 22–28 added/reshaped tests; mouse tests removed)

## What's deferred (NOT done)

- **Preflight + postmortem meta-agents** — `operational_rules` table exists but `apply_preflight` / `apply_postmortem` not wired
- **TypeCast hint accumulator** — `model_hints` table exists, no engine code bumps `(model, role) ± 0.1`
- **Image generation, audio (TTS / ASR) tools** — file/code/web/process/http/git/env/browser exist; image+audio do not
- **OpenRouter / Anthropic / Google providers** — only Ollama + llama.cpp; partial OR work in `tests/test_round4_features.py`
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

---

# RESUME-AFTER-COMPACTION (rounds 22–28)

This block is the latest. Read it first.

## Where things stand

- **Last commit**: `4751970` on `main`, pushed to `origin/main` (2026-05-04). Working tree clean of source changes; only `.claude/*` config drift unstaged.
- **Tests**: **138/138** unit tests pass (was 109; net +29 over the rounds). 11 tests dropped when mouse code was removed.
- **Live verification**: rounds 22–28 each ran 5–20 prompts through real `ac.bat` against the user's Ollama at `192.168.15.103:11434` (devstral-small-2:24b orchestrator + role-specialist models). Cross-turn leak class is closed end-to-end. Average iters/prompt for multi-step code tasks dropped from ~9.2 (round 26) to ~4.3 (round 27 after turn-scoping fix).

## Mouse implementation REMOVED

- `src/agentcommander/tui/mouse_input.py` — **deleted**.
- `src/agentcommander/tui/terminal_input.py` — reverted to plain `msvcrt.getwch` / `kbhit` on Windows. No more ctypes / ReadConsoleInputW / SetConsoleMode tampering.
- All `enable_mouse_mode` / `disable_mouse_mode` / `parse_mouse_events` call sites stripped from `app.py`, `mirror.py`, `status_bar.py`.
- `_handle_popout_click` (app.py) and `_bottom_prompt_handle_click` (status_bar.py) deleted.
- **Why**: enabling xterm SGR mouse required `ENABLE_VIRTUAL_TERMINAL_INPUT` + disabling `ENABLE_QUICK_EDIT_MODE` on Windows console — which broke Windows Terminal's mouse-wheel scrollback and right-click paste. User explicitly chose scrollback over click-to-toggle. Popouts still toggle via Tab / Shift-Tab / Space / Enter / `/popout`.
- `tests/test_terminal_input.py` deleted (was for ctypes mouse synthesizer); `TestMouseParser` class removed from `test_popouts.py`.

## Cross-turn scratchpad leak — fully closed

The single biggest class of bug across rounds 22–27. Manifested as: prompt N's tool result / write_file output / chat reply leaking as the answer to prompt N+1. Multiple layers contributed; all now fixed:

1. **Orchestrator never saw the user's question after turn 1** — `engine.py:1438` was `user_input=scratchpad_text or self.opts.user_message`, dropping the user message any time scratchpad was non-empty. Now always passes `self.opts.user_message`; scratchpad goes via the dedicated `scratchpad_text` channel.
2. **Scratchpad context wrapper** — `role_call.py` now wraps `scratchpad_text` with `"## Prior conversation context (read-only — do not copy verbatim) … ## End of prior context"` delimiter so models can distinguish context from current task.
3. **`compact_scratchpad` strips engine wrappers** — `"successfully completed:\n"` (added by `_dispatch_tool` to tool outputs) is stripped before serialization. Was teaching the model to copy the engine's own scaffolding back as a fake `done.input`.
4. **`_is_scratchpad_leak` detector + chat-fallback route** — backstops in the done branch. Patterns: `"successfully completed:"` prefix, `"Summarize what was done"` (role-prompt scaffolding), `"Work completed:"`, `"Pipeline observations:"`, 3+ `TEST NNN:` references (multi-test-summary hallucination loop).
5. **`LoopState.turn_start_idx`** — index marking where THIS turn's entries begin in the hydrated scratchpad. Set in `_hydrate_scratchpad_from_db` after prior entries load. Threaded through:
   - `build_final_output(scratchpad, current_turn_start=0)` — slices ALL priority paths (summarizer, content-roles, files, executions, tool outputs, step echo) to current-turn entries only.
   - `_scratchpad_context_block` — chat fallback's context now turn-scoped too.
   - `consecutive_nudge_guard`, `dead_end_guard`, `raw_content_guard` — terminal break paths use scoped output.
   - Guard runners (`run_done_guards`, `run_flow_guards`, `run_post_step_guards`) read `turn_start_idx` from ctx.
6. **Hydration filters** — `_hydrate_scratchpad_from_db` skips `chat/reply` entries (they're conversational output, not work product) and `router` entries (they tagged a different question) from cross-turn loading.
7. **Files-list dedup** — `build_final_output`'s `Files created:` line dedupes paths so retry loops on the same file don't show `X.py, X.py, X.py × 7`.

## Tool dispatch fixes

- **`_decision_to_payload` extended** — was returning `{}` for `http_request`, `git`, `env`, `browser`, causing schema-required-field failures (silent breakage that round-23 hid behind retry loops). All four now route their decision fields through. Optional fields with `None` are dropped (round-24 fetch fix generalized).
- **`git` tool sandbox seal** — required `.git/` to exist directly inside `cwd`. Without it, git's default walk-up behavior climbed out of `AgentTesting/` into the parent AgentCommander repo, leaking the sandbox. Now sets `GIT_DIR` + `GIT_WORK_TREE` explicitly as belt-and-suspenders. Returns clean error if no `.git/` in cwd.
- **`fetch_retry_guard` widened** — now counts failures across both `fetch` and `http_request` for the same URL (was per-action, missed orchestrators alternating verbs on a broken URL).
- **`consecutive_nudge_guard` widened** — counts ANY non-`successfully` tool output as "no progress" (was system_nudge-only). Stuck loops with interleaved failures + nudges now break out at 5 instead of running indefinitely.

## JSON verdict contracts on Reviewer + Tester

- `Role.REVIEWER` and `Role.TESTER` now have `output_contract = OutputContract.JSON_STRICT` in the manifest. Engine's `call_role` automatically passes `json_mode=True`.
- New `REVIEWER.md` schema: `{"verdict": "PASS"|"FAIL", "blockers": [...], "warnings": [...], "suggestions": [...], "summary": "..."}` — each blocker has `category / file / line / problem / fix`.
- New `TESTER.md` schema: `{"verdict": "PASS"|"FAIL", "test_files": [...], "command": "...", "tests_total": N, "tests_passed": N, "tests_failed": N, "failures": [...], "summary": "..."}`.
- `_parse_json_verdict` in `done_guards.py` is robust — handles raw JSON, ```json fenced markdown, embedded `{...}` blocks if model added preamble.
- `reviewer_verdict_guard` and `tester_verdict_guard` parse and act on the JSON. On FAIL with non-empty blockers/failures, push a nudge naming the first 3 with file:line. Loop-cap at 2 nudges per verdict — after that, accept the done with the FAIL surfacing as the user-visible answer (better than spinning forever).

## New guards added (preemptive + reactive)

- **`unknown_action_guard`** (decision) — rejects hallucinated verbs like `send_email`, `query_database`. Has a synonym-rewrite fast path: `ls`→`list_dir`, `cat`→`read_file`, `curl`→`fetch`, etc.
- **`unassigned_role_guard`** (decision) — catches `action=<role>` when that role has no provider/model bound (avoids mid-dispatch `RoleNotAssigned` raise).
- **`prompt_template_leak_guard`** (done) — rejects done outputs starting with `"Summarize what was done"`, `"Work completed:"`, `"Pipeline observations:"`, etc.
- **`reviewer_verdict_guard` / `tester_verdict_guard`** (done) — see JSON contract section above.
- **`echo_request_guard`** extended — now has a verbatim-echo backstop that fires regardless of `tool_count`. Catches the case where the orchestrator's `done.input` is the literal user prompt restated.

## Guard runner ordering fix

`consecutive_nudge_guard` was LAST in `run_flow_guards`, but the runner short-circuits on the first non-pass verdict. So `repeated_tool_call_guard` always fired first → consecutive_nudge_guard never incremented its counter → 5-cap never triggered → 26-iter fetch loops were possible. Moved to FIRST so it sees the prior turn's nudge before any new guard adds another.

## Done-guard mutators changed to nudge+continue

Several guards used to silently mutate `decision.action` and `decision.input`, then return `pass`. The action mutation never reached the dispatch switch (we're past it inside `_handle_done`); the placeholder text written to `decision.input` got picked up by `raw_content_guard` and shipped as the final answer. Round-23 caught the symptom: `"Write and run tests for: helper.py, helper.py, helper.py × 4"` appearing as the user-visible answer.

Fixed in: `code_review_guard`, `code_test_guard`, `code_dump_guard`, `verbose_fluff_guard`. All now push a system_nudge naming the redirect and return `continue` so the orchestrator gets a fresh decision.

## Role-prompt refactor (14 prompts)

All freeform prompts rewritten to consistent shape:

```
## Identity        — who you are in 1-2 sentences
## Mission         — what you're called for
## Critical Rules  — numbered must/must-not list
## Output Contract — exact format expected
## Few-Shot Examples / Workflow / Checklist
## Common Failures — anti-patterns
## Success Metrics — what good output looks like
```

Refactored: ARCHITECT, CODER, CRITIC, DATA_ANALYST, DEBUGGER, PLANNER, REFACTORER, RESEARCHER, REVIEWER (also JSON_STRICT), ROUTER, SUMMARIZER, TESTER (also JSON_STRICT), TRANSLATOR, VISION.

Left as-is: **ORCHESTRATOR** (was the template), **PREFLIGHT** (already JSON-strict, battle-tested), **POSTMORTEM** (same).

## Other fixes

- **`Conversation` dataclass + `get_conversation` / `list_conversations`** silently dropped the `working_directory` column — added the field, fixed all three sites.
- **`_safe_token_count(None)`** correctly returns `None` (not 0) — distinguishes "missing" from "actually zero" so usage tallies don't get poisoned.
- **`fetch` payload drops None-valued optional fields** — was emitting `method: null` which schema rejected with `"method: must be string (got NoneType)"` and triggered fetch retry loops.

## What's deferred (still NOT done)

- **Preflight + postmortem meta-agents wiring** — `operational_rules` table exists; `apply_preflight` / `apply_postmortem` not invoked from engine.
- **TypeCast hint accumulator** — `model_hints` table exists; engine doesn't bump `(model, role) ± 0.1` after each call.
- **OpenRouter / Anthropic / Google providers** — only Ollama + llama.cpp wired. (`tests/test_round4_features.py` exists from earlier work; check there for partial impl.)
- **Image generation, audio (TTS/ASR)** tools — only file/code/web/process/http/git/env/browser exist.

## Updated sanity-check checklist

1. `PYTHONUTF8=1 PYTHONPATH=src py -3 -m unittest discover tests` → **138 OK**
2. `git status --short` → only `.claude/*` config drift (auto-commit hook is currently NOT firing — `.claude/settings.json` is missing in the index)
3. `git log --oneline origin/main..HEAD` → empty (everything pushed at `4751970`)
4. `cd AgentTesting && cmd.exe //c "..\ac.bat --version"` → `AgentCommander 0.1.0`
5. `cd AgentTesting && echo "what is 2+2" | cmd.exe //c "..\ac.bat"` → `4` somewhere in the output

## File map for round 22–28 changes

```
src/agentcommander/types.py                       ← LoopState.turn_start_idx, Conversation.working_directory
src/agentcommander/db/repos.py                    ← Conversation.working_directory plumbed through
src/agentcommander/agents/manifest.py             ← REVIEWER + TESTER → JSON_STRICT
src/agentcommander/engine/engine.py               ← turn_start_idx, _decision_to_payload routing,
                                                    orchestrator user_input fix, _is_scratchpad_leak,
                                                    _scratchpad_context_block scoping, hydration filters
src/agentcommander/engine/role_call.py            ← scratchpad context wrapper (Prior context delimiter)
src/agentcommander/engine/scratchpad.py           ← compact_scratchpad strip wrapper, build_final_output
                                                    current_turn_start, files dedup
src/agentcommander/engine/guards/decision_guards.py ← unknown_action_guard, unassigned_role_guard
src/agentcommander/engine/guards/done_guards.py   ← prompt_template_leak_guard, reviewer/tester
                                                    verdict guards, _parse_json_verdict, code_review/
                                                    code_test/code_dump/verbose_fluff nudge+continue
src/agentcommander/engine/guards/flow_guards.py   ← consecutive_nudge_guard moved first, widened to
                                                    count failed tools, fetch_retry counts both verbs,
                                                    repeated_tool_call covers write/execute (cap=8)
src/agentcommander/engine/guards/post_step_guards.py ← turn_start_idx threading
src/agentcommander/tools/git_tool.py              ← .git/ in cwd required, GIT_DIR/GIT_WORK_TREE set
src/agentcommander/tools/http_tool.py             ← (added in pre-session work; payload now routed)
src/agentcommander/tools/env_tool.py              ← (added in pre-session work; payload now routed)
src/agentcommander/tools/browser_tool.py          ← (added in pre-session work; payload now routed)
src/agentcommander/tui/terminal_input.py          ← reverted to msvcrt; mouse code removed
src/agentcommander/tui/{app,mirror,status_bar}.py ← mouse hooks/handlers removed
src/agentcommander/tui/popouts.py                 ← docs updated to reflect mouse removal
src/agentcommander/tui/mouse_input.py             ← DELETED
resources/prompts/{14 files}.md                   ← refactored to standard shape
tests/test_terminal_input.py                      ← DELETED (mouse synthesizer tests)
tests/test_popouts.py                             ← TestMouseParser class removed
tests/test_round4_features.py                     ← (added in pre-session work)
```

---

# RESUME-AFTER-COMPACTION (rounds 29–30 + ORCHESTRATOR.md cleanup, 2026-05-04)

**This is the latest section. Read it first if you're a future Claude.**

## TL;DR — what this session did

Heavy-context multi-agent testing (rounds 29–30) surfaced several real bugs.
Fixed all of them in 6 commits, all pushed to `origin/main`. ORCHESTRATOR.md
also got a major cleanup — was advertising ~30 phantom tools that don't exist
in the dispatcher; now lists exactly the 13 real ones.

## Commits this session (newest first)

```
76367db  prompt: orchestrator now lists only the 13 real tools
b38c991  done_guards: next_steps_guard catches passive intent forms
d737bfe  flow_guards: role_spam_guard hard-break at 8+ consecutive same-role calls
3a08ec1  guards: surface execute failures with code preview; widen next_steps_guard
26ceffa  docs: refresh braindump + README for rounds 22-28
4751970  engine: turn-scoped scratchpad, JSON verdict guards, payload routing,
         prompt refactor, mouse removal
```

All on `origin/main`. Working tree is clean of source changes (only
`.claude/*` config drift remains — that's gitignored / local-only and
the auto-commit hook is currently NOT firing because `.claude/settings.json`
was deleted from the index earlier).

## ORCHESTRATOR.md is now accurate

Massive cleanup. Removed entries for ~30 phantom verbs that the prompt
listed but no tool dispatcher implements:

> **Removed**: `search`, `screenshot`, `click`, `type_text`, `extract_text`,
> `evaluate_js`, `browser_agent`, `browse`, `generate_image`, `generate_music`,
> `speak`, `system_info`, `remote_exec`, `service_status`, `check_port`,
> `dns_lookup`, `workspace_summary`, `find_files`, `diff`, `patch`, `archive`,
> `sql_query`, `csv_read`, `curl`, `background_exec`, `scratchpad_search`,
> `use_template`, `read_env`.

The orchestrator's "Available Actions" table now contains exactly the
13 registered tools + `done`:

```
read_file, write_file, list_dir, delete_file, execute, fetch,
http_request, git, env, browser, start_process, kill_process,
check_process, done
```

`execute` accepts `language` ∈ `{python, py, javascript, js, node, bash,
sh, shell, pip, npm}` — pip/npm aren't separate tools.

A **"What ISN'T a tool" table** was added that maps each phantom verb to
the real alternative (e.g. `search` → `execute` with bash grep; `sql_query`
→ `execute` with python sqlite3; `curl` → `fetch` or `http_request`).
Gives the model a route forward instead of just rejection.

Image-chaining and prompt-enhancement critical rules (#13 + #14) deleted
— both referenced `generate_image` which doesn't exist. Browser-agent
section deleted. Three-way `fetch / http_request / browser` chooser kept
(those three DO exist).

Net: ORCHESTRATOR.md went from 395 → 331 lines (~16% smaller, 100% accurate).

The `unknown_action_guard` (added round 28) is the safety net — if any
older instruction makes the orchestrator emit a phantom verb, it gets
rejected with the real menu instead of the dispatcher returning empty
payload + schema-validation failure.

## New guards added this session (rounds 29–30)

| Guard | Family | What it catches |
|---|---|---|
| Hard-break in `role_spam_guard` | flow | ≥8 consecutive same-role calls (e.g. `review × 16`). Returns `break` with `build_final_output` instead of just nudging. Round-29 caught a 16-iter review loop where the orchestrator ignored every nudge — `consecutive_nudge_guard` couldn't accumulate because reviewer's JSON output looked "productive". |
| `next_steps_guard` first-person extension | done | Catches `"I'll write tests"`, `"I am going to run it"`, `"Let me X"`, `"Next, I'll Y"`. Was missing this entire pattern class. |
| `next_steps_guard` passive-intent extension | done | Catches passive forms like `"Tests are needed next"`, `"Implementation is required"`, `"Next step is to X"`. Round-30 caught the model dodging first-person commitment. |

## New diagnostic added this session

**Code preview on execute failures**: when `execute` returns non-zero exit
code with empty stdout/stderr, the failure output now includes the script
that ran (capped 800 chars):

```
--- script (python) ---
python binary_search_tree29.py
--- no stdout / stderr produced ---
```

Round-29's `exit code 49` mystery was the orchestrator emitting shell
syntax (`python file.py`) as Python code. With the preview visible in
scratchpad, the next iteration's orchestrator can see what it sent and
self-correct (switch language to bash). Without this, the failure was
just `"exit code 49"` with no signal.

## Open / suspected issues

- **`exit code 49` from execute** — orchestrator on devstral-24B sometimes
  emits `language: python, input: "python <file>.py"` (shell syntax in
  the wrong language). With the new code-preview, the model has a chance
  to self-correct, but the underlying model-quality issue persists. If
  it keeps surfacing, consider auto-rewriting `language: python` →
  `language: bash` when the input starts with `python` / `node` / etc.
- **Filename misremembering on long context**: model shortens
  `binary_search_tree29.py` → `binarysearchtree29.py` after many turns.
  Model-side, not engine-side. No fix available.
- **Auto-commit hook not firing**: `.claude/settings.json` was deleted
  in the index sometime mid-session. Manual commits work fine; the
  user's stored "commit often" feedback memory now reminds future
  sessions to commit explicitly per unit of work rather than relying
  on the hook.

## Cross-turn artifacts work as designed

Round-29 deliberately mixed cross-turn references: TEST 9 ("list every
python file created in this conversation"), TEST 10 ("summarize what was
built"), TEST 11 ("translate the summary to french"). All three correctly
referenced earlier turns' outputs — cross-turn memory works WHEN
LEGITIMATELY needed, while the turn-scoping fix prevents prior turns'
tool results from leaking as the answer to unrelated current prompts.
This is the intended balance and it holds.

## Multi-agent coverage observed in round 29

Eight distinct agents fired across the run: chat (fallback), coder,
planner, refactorer, researcher, reviewer (JSON_STRICT), summarizer,
translator. Reviewer JSON contract honored on every call. Translator
output clean (Spanish + French). Researcher produced a real comparison
of AVL vs Red-Black trees with sources.

## Updated sanity-check checklist on resume

1. `PYTHONUTF8=1 PYTHONPATH=src py -3 -m unittest discover tests` → **138 OK**
2. `git log --oneline origin/main..HEAD` → empty (latest pushed `76367db`)
3. `git status --short` → only `.claude/*` config drift
4. `cd AgentTesting && cmd.exe //c "..\ac.bat --version"` → `AgentCommander 0.1.0`
5. `cd AgentTesting && echo "what is 2+2" | cmd.exe //c "..\ac.bat"` → `4` somewhere in the output
6. `PYTHONUTF8=1 PYTHONPATH=src py -3 -c "from agentcommander.tools.dispatcher import bootstrap_builtins, list_tools; bootstrap_builtins(); print(len(list_tools()))"` → **13**

## File map for round 29–30 changes

```
src/agentcommander/tools/code_tool.py             ← code-preview on exit-N failure
src/agentcommander/engine/guards/done_guards.py   ← next_steps_guard intent regex (1st-person + passive)
src/agentcommander/engine/guards/flow_guards.py   ← role_spam_guard hard-break at 8+
resources/prompts/ORCHESTRATOR.md                 ← 30 phantom tools removed; 13 real ones listed;
                                                    "What ISN'T a tool" alternative table added
braindump.md                                       ← this section (rounds 29–30)
```

## File-level reading order (for the next Claude)

When debugging or extending, open in this order:

1. **`braindump.md`** (this file) — high-level state. Read newest section first.
2. `src/agentcommander/types.py` — all dataclasses (`Role`, `OrchestratorDecision`, `ScratchpadEntry`, `LoopState`, `PipelineEvent`)
3. `src/agentcommander/engine/engine.py` — pipeline loop, chat fallback, compaction, turn-scoping, `_decision_to_payload`, `_is_scratchpad_leak`
4. `src/agentcommander/engine/scratchpad.py` — `compact_scratchpad`, `build_final_output(scratchpad, current_turn_start=0)`, `sanitize_scratchpad_text`
5. `src/agentcommander/engine/role_call.py` — provider invocation, scratchpad context wrapper, tool registry appendix
6. `src/agentcommander/agents/manifest.py` — 19-role manifest with output contracts
7. `src/agentcommander/engine/guards/{decision,done,flow,post_step}_guards.py` — guard families
8. `src/agentcommander/tools/dispatcher.py` — schema-validated invoke; `_REGISTRY` is source of truth for tools
9. `resources/prompts/ORCHESTRATOR.md` (+ `RECIPES.md`) — orchestrator system prompt
10. `src/agentcommander/tui/app.py:run_tui` — startup sequence + REPL

## Hard rules that haven't changed

- stdlib only, serial only, modular by default
- Project-local DB at `<cwd>/.agentcommander/db.sqlite`
- **Always run `ac.bat` from `AgentTesting/`** (saved as feedback memory)
- **Commit often, never batch** — per-unit-of-work commits, push at milestones (push needs confirmation, commit doesn't) — saved as feedback memory
- Mouse removed → native scrollback works in Windows Terminal
- Push goes directly to `main`; no PR workflow

## Pointers

- Memory dir: `C:\Users\sixoffive\.claude\projects\C--Users-sixoffive-Documents-AgentCommander\memory\` — see `MEMORY.md`
- EngineCommander upstream (read-only): `C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander`
- TypeCast: `https://github.com/SixOfFive/TypeCast`
- AgentCommander remote: `https://github.com/SixOfFive/AgentCommander`
- User email (auto-memory): `hvr.biz@gmail.com`

Last commit at compaction: **`76367db`** ("prompt: orchestrator now lists only the 13 real tools").
138/138 unit tests pass. Working tree is clean of source changes.

---

# RESUME-AFTER-COMPACTION (rounds 31–40, 2026-05-04 → 2026-05-05)

**Newest section. Read first if you're a future Claude resuming after compaction.**

## TL;DR — what shipped

Major round of follow-ups after rounds 29–30. Highlights:

- **Auto-fetch path on weak orchestrators** — when chat fallback or `done.input` emits `fetch <url>` / `list_dir` / etc. as plain text, the engine actually runs the tool and re-streams a summary. Symmetrical between both surfaces.
- **Deterministic forced-fetch** for live-data questions when the orchestrator refuses despite nudges. Pattern-match → URL → execute. Closed the weather-question case end-to-end on Qwen3.6-35B (which couldn't follow JSON).
- **Chat log feature** — every user/assistant message also gets appended to `<wd>/logs/<conv-time>.log`. New chat → new file. Rotation at 10 MB × 20 parts. Refuses to run in the AC source repo.
- **Self-measured throughput** in `<wd>/.agentcommander/model_stats.json` (with shape-aware char→token estimation). Honest "—" instead of fake `100 t/s` for unmeasured models.
- **`/compact` slash command** — manual scratchpad compaction. Reuses the auto-compact routine; subsequent calls do summary-of-summaries.
- **Per-role num_ctx cap** for router (8 k by default; was inheriting session ceiling). Skip tool-registry appendix on router.
- **shell-in-wrong-language guard** — `execute(language=python, input="python file.py")` → auto-rewrites to `bash`. Closes round-29's exit-49 mystery.
- **Windows Microsoft Store Python stub workaround** — bash on Windows resolves `python` to a stub that exits 49; the engine now rewrites those tokens to the real `py -3` path.
- **Markdown underscore-emphasis killed** — was eating filename underscores (`__pycache__` → `pycache`).
- **17 sub-fixes** in one batch (URL cleanup, retry+cancel on auto-fetch, host validation, multi-step support via lenient `next_steps_guard`, smaller-router-model hint, etc.)
- **Provider startup retry** (1.5 s + 1 retry) for transient llama-server / Ollama unreachability.
- Latest commit: **`f3c0088`**.

## Test count

**176/176** unit tests pass (was 138). Net +38 over these rounds:
- 21 new tests in `tests/test_live_data_inference.py`
- 17 new tests in `tests/test_shell_in_wrong_language.py`

Test runner: `PYTHONUTF8=1 PYTHONPATH=src py -3 -m unittest discover tests` (must run from repo root).

## New / restructured files

| File | What | Why |
|---|---|---|
| `src/agentcommander/chat_log.py` | NEW — file-system chat transcript | User-spec'd readable log next to the DB. Per-conversation file at `<wd>/logs/YYYY-MM-DD-HH-MM-SS.log`. Rotates at 10 MB. |
| `src/agentcommander/model_stats.py` | NEW — side-by-side throughput JSON | Catalog-independent self-measured rates. Shape-aware char→token estimate (CJK 1.5, code 3.0, prose 4.0). |
| `src/agentcommander/providers/capability_hints.py` | NEW — capability inference from id | Substring-match on model id for vision/audio/image_gen capabilities. Used by autoconfig fallback when catalog is silent. |
| `src/agentcommander/cli.py` | EXTENDED — program-folder check | Refuses to launch with cwd == repo root. Loud red banner. |
| `src/agentcommander/engine/scratchpad.py` | EXTENDED — `compact_conversation_db`, `build_compaction_prompt` | Standalone helpers used by both auto- and manual-compaction. |
| `tests/test_live_data_inference.py` | NEW — 21 tests | Weather/news/time URL inference. |
| `tests/test_shell_in_wrong_language.py` | NEW — 17 tests | `execute(language=python, input="python <file>")` rewrite. |

## Engine changes — auto-fetch + forced-fetch

The orchestrator on weaker models routinely emits tool syntax as plain text instead of JSON actions. Two recovery paths now exist:

### A. Chat fallback / done.input — auto-execute on tool syntax

`PipelineRun._detect_tool_syntax_intent(text)` looks at the **last non-empty line** of:
- The chat fallback's streamed reply
- The orchestrator's `done.input`

If it matches `^<verb>[\s+<arg>]?$` and the line is ≤ 300 chars, treat it as the tool call the model intended. Honored by `_honor_tool_text_as_intent(verb, arg, …)`:

1. Validates the URL via `safety.host_validator.validate_user_host` (rejects loopback, link-local, non-HTTP)
2. Cleans the arg via `_clean_textual_arg` (strips trailing punctuation, surrounding quotes/backticks/brackets, `<>`)
3. Builds the payload via `_payload_from_textual_call`
4. Runs the tool
5. Re-streams chat with the result in context, wrapped in retry-on-rate-limit + cancel checks

**Safe verbs** (auto-executed): `fetch`, `http_request`, `read_file`, `list_dir`, `browser`, `check_process`, `env`.

**Unsafe verbs** (apologize, never auto-run): `write_file`, `delete_file`, `execute`, `git`, `start_process`, `kill_process`. Refusal message asks the user to retry — orchestrator must follow the JSON contract for these.

**Synonyms accepted** (via shared `TOOL_VERB_SYNONYMS` in `decision_guards.py`): `ls`/`dir`→`list_dir`, `cat`→`read_file`, `curl`/`wget`→`fetch`, `rm`/`del`→`delete_file`, `bash`/`sh`→`execute`, `ps`→`check_process`. Same map drives both `unknown_action_guard` and the chat-fallback intent detector.

### B. Live-data forced-fetch — deterministic URL inference

When `premature_done_count >= 2` (orchestrator declined twice) AND the user message matches a live-data pattern, the engine infers a URL and runs it itself.

`_LIVE_DATA_PATTERNS_FORCED` table:

| Pattern | URL builder |
|---|---|
| `weather/forecast/temperature in <X>` | `https://wttr.in/<city>?format=3` |
| `weather` (bare) | `https://wttr.in/?format=3` (IP geolocation) |
| `current time / time in <X>` | `https://worldtimeapi.org/api/timezone/<region>` |
| `what time is it` (bare) | `https://worldtimeapi.org/api/ip` |
| `today's news / latest news / breaking news / top stories` | `https://news.google.com/rss` |

Priority ordering matters — specific patterns (with location) win over bare fallbacks.

Falls through to chat fallback when nothing matches.

## Engine changes — premature-done escape hatch

After 2 premature dones, if scratchpad has ≥ 2 successful tool calls (write_file / execute / fetch / read_file / list_dir / git / browser / http_request) **in the current turn**, use `build_final_output(scratchpad, turn_start_idx)` directly instead of paying for another chat-fallback LLM call. This shaves ~5400 prompt tokens off multi-step runs where the answer is already in the pad.

## Guard changes

- **`live_data_question_guard`** (NEW, in done_guards) — pre-empts `done` on weather/news/time questions when no fetch has happened yet. 2-nudge cap to avoid loops.
- **`tool_call_as_chat_guard`** (NEW, in done_guards) — `done.input` matches `<verb> <arg>`? Push a JSON-shape nudge and continue.
- **`shell_in_wrong_language_guard`** (NEW, in decision_guards) — `execute(language=python, input="python <file>")` → silently rewrites `language` to `bash`. Strips `.exe` extensions and path prefixes when matching the first token. Inserted in `run_decision_guards` BEFORE `missing_fields_guard` so the rewritten language is what gets validated.
- **`next_steps_guard`** — added a lenient bypass: when scratchpad has ≥ 2 successful tool calls in this turn, the guard exits before checking for "I'll" / "next, I'll" intent prose. Avoids rejecting completion-report dones that incidentally mention "I'll verify".
- **Decision-guard runner order**: `empty_action` → `sentence_as_action` → `unknown_action` → `unassigned_role` → `field_swap` → **`shell_in_wrong_language`** → `missing_fields` → `malformed_url` → `templating_placeholder` → `disabled_browser` → `delete_new_file`.

## Chat-fallback prompt tightening

`CHAT_FALLBACK_SYSTEM_PROMPT` (engine.py:47) explicitly says:

> You are NOT the orchestrator and you CANNOT call tools from this role. Do NOT emit tool-call syntax such as `fetch <url>`, `read_file <path>`, `execute ...`. If you need data you don't have, say so plainly.

Combined with the post-guard, this either prevents the leak entirely or auto-recovers when the model still leaks.

## ORCHESTRATOR.md prune

Was 14 critical rules + bigger appendix; now 8 rules ordered by frequency-of-violation. Top 3:

1. **Tools are JSON, not text.** `{"action":"<verb>",...}`. Writing `"fetch https://..."` in `done.input` ships the literal string.
2. **Live-data questions need `fetch` FIRST.** Lists weather/time/news/price endpoints.
3. **Don't `done` early.** Multi-step trigger words ("then", "after", "also") → all steps before done.

Plus new "Bad — tool syntax as plain text" + "Good — live-data question (weather)" few-shot examples.

## Self-measured throughput

`record_throughput(model, completion_tokens, duration_ms, *, chars_completed, sample_text)`:

- When `completion_tokens` is missing (some llama.cpp builds report 0), falls back to `estimate_tokens_from_chars(chars, sample_text)` — divisor depends on content shape:
  - CJK ≥ 20% of chars → 1.5
  - Code-heavy punctuation ≥ 10% → 3.0
  - Else (prose) → 4.0
- Mirrors observation to `<wd>/.agentcommander/model_stats.json` with `source: "estimated"|"measured"` flag.
- DB `model_throughput` table still operational, but first measurement uses `rate` directly (was averaging with the 100 t/s seed which skewed early reads).
- `get_throughput()` returns `None` for unmeasured models so UI renders "—" instead of a fake `100 t/s`.

## Per-role num_ctx caps

`engine/role_resolver.py` has `_PER_ROLE_DEFAULT_CTX_CAP`:

```python
{Role.ROUTER: 8192}
```

When the autoconfig session-ceiling is None or larger than the cap, the cap wins for these roles. `/context` override beats this; `/roles set` per-role context beats this. Saves the router from allocating a 128 k KV cache on local 30B+ stacks.

`role_call.py` skips the tool registry appendix for `Role.ROUTER` — saves ~400 prompt tokens per turn.

## Provider probe retry

`autoconfig._gather_installed` retries each provider's `list_models()` once with a 1.5 s pause on first failure. Closes the "all roles unset on first launch after server restart" race.

## Capability detection

`provider.get_model_capabilities(model)` returns a set of tags from `{"text", "vision", "audio", "image_gen"}`:

- **Ollama** — calls `/api/show` and reads the `capabilities` array; merges with name-heuristic.
- **llama.cpp** — name-heuristic only (`/v1/models` doesn't surface modality).

Capability hints (`providers/capability_hints.py`) substring-match against tables like `_VISION_HINTS` (llava, moondream, qwen-vl, llama-3.2-vision, gemma-3, pixtral, internvl, smolvlm, …).

Autoconfig's fallback path uses these to decide which non-text roles get the model: `vision` role only assigned when `vision` capability detected; same for `audio`/`image_gen`. Default → goes to `unset_roles`.

## /compact slash command

`cmd_compact` in `tui/commands.py` calls the same routine as the auto-compact (90 % threshold) but unconditionally. Subsequent calls summarize the prior summary too — true summary-of-summaries.

Standalone helper in `engine/scratchpad.py`:

```python
def compact_conversation_db(conversation_id, *, summarize_fn, keep_tail=6, run_id=None, audit_fn=None) -> dict | None:
```

Lists live (non-replaced) scratchpad rows, splits off the last `keep_tail` as live working context, summarizes the rest via the caller's `summarize_fn`, inserts a synthetic `system/compacted` row, marks originals `is_replaced=1`. Returns `{summary_id, replaced_count, original_chars, summary_chars, summary_text}` or `None`.

`_maybe_compact_scratchpad` in engine.py refactored to use this helper.

**Important design fact**: compaction is layered. Layer 1 (`messages` table — user view) is **never** compacted. Layer 2 (`scratchpad_entries`) — old rows are flagged `is_replaced=1` but **never deleted**, so audit/postmortem/future re-analysis sees full fidelity. Only Layer 3 (the prompt fed to the model) sees the summary instead of originals.

## Markdown renderer fix

Old `_BOLD_RX = ... | __([^_\n]+?)__` and `_ITALIC_RX = ... | _([^_\n]+?)_` ate filename underscores: `__pycache__` rendered as bold "pycache", `binary_search_tree29.py` showed as "binarysearchtree29.py" with `search` italicized. Even with CommonMark-style intraword lookarounds, `__pycache__` at line edges still matched.

**Fix**: killed underscore-emphasis entirely in `tui/markdown.py`. `**bold**` and `*italic*` still work. Filenames preserved.

## Logs feature

`<wd>/logs/<conversation-created-at>.log` plain-text transcript. Append on every `messages` row insert via hook in `db/repos.py:append_message`. Format:

```
[YYYY-MM-DD HH:MM:SS] USER:
<content>

[YYYY-MM-DD HH:MM:SS] ASSISTANT:
<content>
```

Best-effort — log failures never break the chat. Rotation at 10 MB; up to 20 historical parts (`<base>.001.log`, `<base>.002.log`, …) before oldest is deleted.

`/chat new` and `/chat clear` produce a new conversation → new log file. `/chat resume` appends to the existing file.

`AgentTesting/` is gitignored → `logs/` inside it is implicitly covered.

## Program-folder check

`cli.py:_detect_program_folder()` finds the AC source repo by walking from `agentcommander.__file__` up to a directory containing both `pyproject.toml` and `src/agentcommander/__init__.py`. If `cwd` resolves to that path, exit code 2 with a loud red banner — refuses to start. Pollution prevention.

## Auto-commit hook restored

`.claude/settings.json` was deleted from the index mid-session at compaction time. Restored:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {"type": "command", "command": "git add -A && (git diff --cached --quiet || git commit -m \"auto: changes from claude\" --quiet) 2>/dev/null || true"}
        ]
      }
    ]
  }
}
```

Loaded at session start; new sessions auto-commit on every Edit/Write.

## Smaller-router-model startup hint

When autoconfig binds the same big model to both `router` and `orchestrator`, startup prints a muted hint:

> hint: router uses the same large model as the orchestrator (devstral-small-2:24b). A small 1-3B model would classify intent in <1 s. Bind one with `/roles set router <provider_id> <small_model>` to drop ~5-15 s per turn.

Only fires when the bound model name contains a "big-marker" substring (70b, 35b, 24b, 22b, 20b, 13b, etc.). Easy win the user can act on without engine work.

## Windows Microsoft Store Python stub workaround

Bash on Windows (Git Bash) resolves `python` / `python3` to `%LocalAppData%/Microsoft/WindowsApps/python3.exe` — a stub that exits 49 with "Python was not found, install from the Microsoft Store". This caused round-29-style mysterious exit-49 failures even on systems with Python correctly installed via `py -3`.

Fix in `code_tool.py`: when `language ∈ {bash, sh, shell}` on Windows AND the script contains `python ` / `python3 `, rewrite those tokens to the resolved real interpreter path. Backslashes converted to forward slashes (bash escape-strips them otherwise). Quote paths with spaces.

Regex: `(^|[\s;&|])python3?(\s)` → `\1<resolved-path>\2`. Function-based substitution avoids regex-escape issues with Windows backslashes.

## Updated sanity-check checklist

1. `cd C:\Users\sixoffive\Documents\AgentCommander && PYTHONUTF8=1 PYTHONPATH=src py -3 -m unittest discover tests` → **176 OK**
2. `git log --oneline origin/main..HEAD` → empty (everything pushed; auto-commits land continuously)
3. `git status --short` → only `.claude/settings.local.json` drift (local-only permissions)
4. `cd AgentTesting && cmd.exe //c "..\ac.bat --version"` → `AgentCommander 0.1.0`
5. `cd AgentTesting && echo "what is 2+2" | cmd.exe //c "..\ac.bat"` → `4` somewhere
6. `cd AgentTesting && echo "what is the weather in edmonton" | cmd.exe //c "..\ac.bat"` → real weather data via wttr.in (live-data forced-fetch path)
7. `PYTHONUTF8=1 PYTHONPATH=src py -3 -c "from agentcommander.tools.dispatcher import bootstrap_builtins, list_tools; bootstrap_builtins(); print(len(list_tools()))"` → **13**

## File map for rounds 31–40

```
src/agentcommander/chat_log.py                       NEW — chat transcript writer + rotation
src/agentcommander/model_stats.py                    NEW — side-by-side throughput JSON
src/agentcommander/providers/capability_hints.py     NEW — name-heuristic capability inference
src/agentcommander/providers/base.py                 + get_model_capabilities default
src/agentcommander/providers/ollama.py               + get_model_capabilities via /api/show
src/agentcommander/providers/llamacpp.py             + get_model_capabilities via name heuristic
src/agentcommander/cli.py                            + program-folder refuse banner
src/agentcommander/db/repos.py                       record_throughput sample_text path,
                                                     get_throughput → Optional[float],
                                                     append_message → chat_log hook
src/agentcommander/typecast/autoconfig.py            fallback_no_catalog + role-required-capability
                                                     map; _gather_installed retry
src/agentcommander/engine/role_resolver.py           _PER_ROLE_DEFAULT_CTX_CAP (router=8192)
src/agentcommander/engine/role_call.py               skip tool-registry appendix on router;
                                                     pass sample_text to record_throughput
src/agentcommander/engine/engine.py                  _honor_tool_text_as_intent,
                                                     _detect_tool_syntax_intent,
                                                     _infer_live_data_url + table,
                                                     premature-done escape via build_final_output,
                                                     _maybe_compact_scratchpad refactored to
                                                     compact_conversation_db
src/agentcommander/engine/scratchpad.py              compact_conversation_db,
                                                     build_compaction_prompt
src/agentcommander/engine/guards/done_guards.py      tool_call_as_chat_guard,
                                                     live_data_question_guard,
                                                     next_steps_guard lenient bypass
src/agentcommander/engine/guards/decision_guards.py  shell_in_wrong_language_guard,
                                                     TOOL_VERB_SYNONYMS shared map
src/agentcommander/tools/code_tool.py                Windows MS-store-Python-stub workaround
src/agentcommander/tui/app.py                        autoconfig fallback note,
                                                     smaller-router-model hint,
                                                     "—" instead of fake tok/s
src/agentcommander/tui/commands.py                   cmd_compact + /compact registry entry
src/agentcommander/tui/markdown.py                   killed underscore emphasis
resources/prompts/ORCHESTRATOR.md                    prune 14→8 critical rules,
                                                     live-data + tool-syntax-as-text examples
resources/prompts/ROUTER.md                          unchanged
.claude/settings.json                                restored auto-commit hook
tests/test_live_data_inference.py                    NEW — 21 tests
tests/test_shell_in_wrong_language.py                NEW — 17 tests
```

## Open / known issues (model-side, not engine)

- **Router miscategorizes "write X then run it" as `question`** sometimes (10-iter cap). Better with "project" classification. Model-quality issue; could prompt-tune but root cause is model strength.
- **`WinError 10061` flapping** on first chat-call after fresh ac.bat launch — Ollama loading the model and rejecting parallel requests during. Worked on second turn. Could add a one-shot retry on connection-refused at the provider layer.
- **worldtimeapi.org SSL handshake unreliable** on user's network. Could add a backup time API to the live-data table.
- **Filename underscore mangling on long-context devstral** — separate from the markdown fix. Model itself drops underscores on summarization. No engine fix.

## Hard rules (unchanged)

- stdlib only, serial only, modular by default
- Project-local DB at `<cwd>/.agentcommander/db.sqlite`
- **Always run `ac.bat` from `AgentTesting/`** (saved as feedback memory)
- **Commit often, never batch** — auto-commit hook fires per Edit/Write; push at milestones (push needs confirmation)
- Mouse removed → native scrollback works in Windows Terminal
- Push goes directly to `main`; no PR workflow
- Slash commands and their output are screen-only — never enter `messages` or `scratchpad_entries`

## Pointers

- Memory dir: `C:\Users\sixoffive\.claude\projects\C--Users-sixoffive-Documents-AgentCommander\memory\` — see `MEMORY.md`
- EngineCommander upstream (read-only): `C:\Users\sixoffive\Documents\Claude_Projects\EngineCommander`
- TypeCast: `https://github.com/SixOfFive/TypeCast`
- AgentCommander remote: `https://github.com/SixOfFive/AgentCommander`
- User email (auto-memory): `hvr.biz@gmail.com`

Last commit at update: **`f3c0088`**.
**176/176** unit tests pass. Working tree is clean of source changes.


