# AgentCommander

Local multi-agent LLM orchestration CLI. Pure-Python (stdlib only — zero runtime dependencies). Mimics the Claude Code Linux console look.

> **Status:** v0.1.0 — full port of the EngineCommander internals (safety layer, 19 agents, 9 guard families, tool dispatcher, providers, TypeCast) plus a live read-only mirror, project-local SQLite with corruption defense, persistent chat history, cross-turn scratchpad memory, and per-model throughput tracking. Single-user, single-machine.

## Concept

> One computer, one LLM, one army of agents.

Pick a model in Ollama. AgentCommander assigns it to all 19 specialized roles (router, orchestrator, planner, coder, reviewer, vision, etc.). The orchestrator runs a guarded serial loop — every iteration emits one JSON action which is dispatched as a role delegation, a tool call, or `done`. Streamed tokens render live, status bar shows role / model / tokens / context / timers / throughput, and a watcher process can attach read-only to follow along.

## Design

| Constraint | Decision |
|---|---|
| Dependencies | **Zero.** stdlib only — `urllib`, `sqlite3`, `re`, `argparse`, ANSI escapes |
| Concurrency | **Serial.** No `parallel` action, no async coordination. One TUI thread + one engine worker thread (so `/stop` can fire mid-run) |
| Multi-tenant | **No.** Single user, single working directory |
| Network | Local Ollama / llama.cpp by default; OpenRouter / Anthropic / Google as plug-in providers (deferred) |
| Plugins | Protocol-based registries — drop a `.py` and register at module top-level |
| UI | Pure ANSI escape codes + char-mode raw stdin (autocomplete + history) + bottom status bar in 3 reserved rows |
| Persistence | Project-local SQLite at `<cwd>/.agentcommander/db.sqlite` (gitignored). Each project gets its own DB; auto-repair on startup |

## Install

Python 3.10+ is required. No venv or pip install needed for everyday use — the launchers add `src/` to `PYTHONPATH` automatically:

```bash
# Linux / macOS
./ac.sh

# Windows
ac.bat
```

If you'd rather install it (`pip install -e .`):

```bash
pip install -e .
ac
```

## First run

```text
❯ ./ac.sh

  workdir: /home/you/code/scratch

  ╭──────────────────────────────────────╮
  │   AgentCommander  ·  multi-agent CLI │
  ╰──────────────────────────────────────╯

  v0.1.0  ·  0 provider(s)  ·  305 model(s) in TypeCast catalog
  type /help for commands  ·  /quit to exit
```

Add a provider, pick a model, send a prompt:

```text
❯ /providers add ollama-local ollama "Local Ollama" http://127.0.0.1:11434
❯ /models ollama-local
❯ /roles assign-all ollama-local qwen3:8b
❯ Build me a python script that prints the current weather in NYC
```

Or just run `/autoconfig` and let TypeCast pick a per-role best fit from your installed models.

The bottom three rows are reserved for the live status bar:

```
─────────────────────────────────────────────────────────────────────────
              ▸ coder → devstral-small-2:24b @ 38 t/s  ·  in 2.1k  out 410  ·  ctx 4.2k/8k [████░░░░]  ·  run 0:23  ·  total 2:14
❯
```

## Slash commands

| Command | Purpose |
|---|---|
| `/help [<cmd>]` | List commands or show details for one |
| `/quit`, `/exit` | Exit (mid-run also unloads loaded models) |
| `/clear` | Wipe the scroll region |
| `/stop` | Halt the active pipeline mid-stream |
| `/workdir <path>` | Set the working directory (sandbox boundary) |
| `/providers [add\|test\|rm]` | Manage providers |
| `/models <provider_id>` | List installed models with running tok/s |
| `/roles [set\|unset\|auto\|assign-all]` | Show / edit role → (provider, model) bindings |
| `/typecast [refresh\|autoconfigure]` | TypeCast catalog status / re-fetch / dispatch |
| `/autoconfig [minctx <N>\|ban <model>\|unban <model>\|bans\|clear]` | Threshold-cascade picker, ban list, min-context filter |
| `/context [<N>\|off]` | Session-wide `num_ctx` override |
| `/vram` | Total + Ollama `/api/ps` live + catalog estimates |
| `/db [check\|reindex\|vacuum\|backup\|salvage\|reset]` | Inspect + repair the project DB |
| `/chat [list\|new\|clear\|resume\|title\|export]` | Manage conversations |
| `/new [<title>]` | Alias for `/chat new` |
| `/status` | Stacked-bar of per-model usage for the current chat |
| `/agents` | The 19 agents — category, output contract, prompt availability |
| `/tools` | Registered tools |
| `/history` | Recent conversations |

Type `/` to open the autocomplete popup; Tab inserts the highlighted match. Up/Down navigates history when the popup is closed, popup matches when it's open. Esc dismisses.

## Mirror mode

Run a second instance to follow what the primary is doing, read-only. Skips the single-instance lock, opens the DB with `mode=ro`, polls the live event stream. Coexists with the primary, or starts before it exists:

```bash
# Terminal 1 — primary
./ac.sh

# Terminal 2 — read-only follower in the same project dir
./ac.sh --mirror
```

The mirror sees:
- User messages, role transitions, streamed token chunks (~10 Hz coalescing)
- Iteration markers, tool calls + results, guard events
- Status bar with the primary's role / model / tokens / ctx / timers / tok/s
- Conversation switches (`/chat new` / `/chat resume` / `/chat clear` on primary)

Only `/exit` and `/quit` are accepted as input. On exit the mirror does NOT call `provider.unload` — primary owns the loaded models.

## What's persistent across runs

- **Conversations + messages** — auto-resume the most recent chat for the project on launch. `/chat new` starts fresh; `/chat clear` is destructive.
- **Scratchpad** — the model-facing memory (router decisions, role outputs, tool results) survives across turns and across program restarts. Auto-compacted via the summarizer role when it would exceed `num_ctx × 0.5`.
- **Provider configs + role assignments** — endpoints and per-role overrides survive in the DB.
- **Per-model throughput** — running-average tokens/second per model, displayed everywhere a model is shown. Updated after every call via `new_avg = (old_avg + completion_tokens / duration) / 2`. Default 100 tok/s for never-measured models.
- **Filesystem permissions** — "always allow" decisions for read/write/execute, with subtree scope. `Y once` decisions live in-memory only.

## DB hardening

The original corruption incident (`Tree X page Y: btreeInitPage error 11`) drove four overlapping defenses, all enabled by default:

1. **Single-instance lock** (`<dbpath>.lock`) — `msvcrt.locking` on Windows / `fcntl.flock` on POSIX. Refuses concurrent primary processes with a friendly message; the mirror skips this lock entirely.
2. **`PRAGMA synchronous = FULL`** + **`cell_size_check = ON`** — durability + runtime page-shape validation.
3. **Auto check + repair on startup** — `quick_check` first; on failure, `REINDEX` and re-check. Result surfaces in the startup banner.
4. **`atexit` + `SIGINT` / `SIGTERM` / `SIGBREAK` handlers** — `wal_checkpoint(TRUNCATE)` before the connection closes so a kill mid-write doesn't tear WAL state.

Manual recovery: `/db check`, `/db reindex`, `/db vacuum`, `/db backup <path>`, `/db salvage <path>` (row-by-row to a fresh DB), `/db reset` (DESTRUCTIVE).

## What's ported from EngineCommander

| Component | Status | Notes |
|---|---|---|
| Dangerous-command scanner | verbatim | 30+ patterns: fork bombs, exfil, persistence, privesc, curl-to-shell, reverse shells, shutdown |
| Filesystem sandbox | adapted | Single working dir; no multi-tenant `EC_DATA_DIR` workspaces |
| SSRF host validator | verbatim | strict (`validate_user_host`) + permissive (`validate_provider_host`) |
| Prompt-injection detection | verbatim | 18 patterns; halts pipeline on definite/likely match |
| 19 role prompts | copied | `resources/prompts/*.md` |
| Engine action set | ported | role + tool actions + `done`. **No `parallel`** (serial-only) |
| Engine main loop | ported | scratchpad, generator-based events, guard hook points |
| 9 guard families (~110+ guards) | ported | decision, flow, execute, write, output, fetch, post_step, done + shared types |
| Tools: file / code / web / process | ported | sandbox-gated |
| Provider: Ollama | ported | streaming via stdlib `urllib`; `keep_alive=5m`; `/api/ps`; `should_cancel` mid-stream |
| Provider: llama.cpp | ported | OpenAI-compat SSE via stdlib `urllib` |
| TypeCast catalog | ported | startup conditional-GET from GitHub → cache → bundled fallback |
| TypeCast autoconfig | ported | threshold-cascade picker with ban list and min-context filter |

## Beyond the port

- **Read-only mirror** (`ac --mirror`) — live event stream replay for a watcher process
- **Live status bar** — bottom-anchored 3 rows showing role/model/tokens/ctx (with fill bar) / run+total timers / running-avg tok/s
- **Streaming token deltas** — coalesced ~10 Hz to balance smooth display with low DB write pressure
- **Cross-turn scratchpad** — model memory persisted in `scratchpad_entries`; auto-compacted via the summarizer role
- **Project-local DB** — `<cwd>/.agentcommander/db.sqlite`. Each project gets its own state; the catalog cache stays global
- **Auto-resume on startup** — the most recent chat for this project re-renders on launch
- **Per-model throughput tracking** — running EMA of tok/s shown everywhere a model is named
- **Session context ceiling** — `min(contextLength)` across picked models becomes the announced cap; resolver falls through `/context override → per-role → ceiling → None`

## Explicitly NOT in scope

- Marketplace, escrow, ledger, drift watcher
- `/v1/*` proxy with multiple key types
- SSO / multi-tenant
- Web PWA
- Apache reverse proxy
- ec_proxy_usage / ec_security_log multi-table audit (replaced by single `audit_log`)

## Modular layout

```
src/agentcommander/
├── cli.py                  argparse entry — invoked by ac.bat / ac.sh
├── types.py                shared dataclasses + Role enum
├── registry.py             Protocol-based plugin primitives
├── safety/                 dangerous_patterns, sandbox, host_validator, prompt_injection
├── agents/                 19-role manifest + prompt loader
├── db/                     connection (lock + auto-repair + signals) + schema.sql + repos
├── providers/              base + ollama + llamacpp (auto-registered on import)
├── tools/                  dispatcher + file_tool / code_tool / web_tool / process_tool
├── engine/
│   ├── engine.py           PipelineRun (generator yielding PipelineEvents)
│   ├── role_call.py        invoke a role via its assigned provider
│   ├── live_tee.py         tee events + bar state into pipeline_events / config
│   ├── role_resolver.py    num_ctx precedence: /context → per-role → ceiling → None
│   ├── actions.py          ROLE_ACTIONS / TOOL_ACTIONS / ACTION_TO_ROLE
│   ├── scratchpad.py       compaction + final-output assembly
│   └── guards/             9 families: output, write, fetch, post_step, decision,
│                           flow, execute, done + shared types
├── typecast/               catalog (conditional-GET), vram detect, autoconfig
└── tui/                    ansi.py + render.py + markdown.py + commands.py + app.py +
                            status_bar.py + autocomplete.py + terminal_input.py +
                            permissions.py + setup.py + mirror.py
resources/prompts/          19 role .md system prompts
```

Every plugin layer (providers, tools, guard families) is a Python module that registers itself on import. To add a new provider type, drop a `.py` next to `providers/ollama.py` with a `@provider_factory("yourtype")` decorator. Tools follow the same pattern via `register(ToolDescriptor(...))`.

## License

UNLICENSED. Personal fork.
