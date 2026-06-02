# AgentCommander

Local multi-agent LLM orchestration CLI. Pure-Python (stdlib only — zero runtime dependencies). Mimics the Claude Code Linux console look.

> **Status:** v0.1.0 — full port of the EngineCommander internals (safety layer, 19 agents, 9 guard families, tool dispatcher, providers, TypeCast) plus a live read-only mirror, project-local SQLite with corruption defense, persistent chat history, cross-turn scratchpad memory, and per-model throughput tracking. Single-user, single-machine.
>
> **Recent:** schema-constrained orchestrator decoding (the decision JSON is constrained to the action grammar at the sampler level), opt-in parallel **fan-out** across the fleet with makespan-aware host routing, read-only **vault recall** (long-term memory over a local notes vault), and a scored **eval harness** (`evals/`) that replays golden prompts through the real engine.

## Concept

> One computer, your local models, one army of agents.

Install models in Ollama (or point at a llama.cpp server). On startup, AgentCommander scores your installed models against the TypeCast catalog and picks a per-role best fit — the strongest available model for the orchestrator, a fast small one for translator, a code-tuned model for coder/debugger, and so on across all 19 specialized roles (router, orchestrator, planner, coder, reviewer, summarizer, architect, critic, tester, debugger, researcher, refactorer, translator, data_analyst, vision, audio, image_gen, preflight, postmortem). You can pin any role explicitly with `/roles set`. The orchestrator runs a guarded serial loop — every iteration emits one JSON action which is dispatched as a role delegation, a tool call, or `done`. Streamed tokens render live, status bar shows role / model / tokens / context / timers / throughput, and a watcher process can attach read-only to follow along.

## Design

| Constraint | Decision |
|---|---|
| Dependencies | **Zero.** stdlib only — `urllib`, `sqlite3`, `re`, `argparse`, ANSI escapes |
| Concurrency | **Serial by default.** The orchestrator loop is sequential (one TUI thread + one engine worker so `/stop` can fire mid-run). An **opt-in `fan_out`** action runs independent role sub-steps concurrently on a bounded `ThreadPoolExecutor` (stdlib) — used for panels (review+critique+test) and fleet utilization. Still no asyncio |
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

Add a provider, then let autoconfig pick per-role best fits, and send a prompt:

```text
❯ /providers add ollama-local ollama "Local Ollama" http://127.0.0.1:11434
❯ /typecast autoconfigure         # picks the best model per role from what's installed
❯ Build me a python script that prints the current weather in NYC
```

Per-role picks land like this — different models for different jobs:

```
role          model                      tok/s   kind
────────────  ─────────────────────────  ──────  ─────
router        devstral-small-2:24b       3 t/s   auto
orchestrator  devstral-small-2:24b       3 t/s   auto
planner       codestral:22b              5 t/s   auto
coder         devstral-small-2:24b       3 t/s   auto
reviewer      gemma4:e2b                 27 t/s  auto
summarizer    aya:8b                     32 t/s  auto
critic        cogito:8b                  6 t/s   auto
tester        cogito:8b                  6 t/s   auto
debugger      codestral:22b              5 t/s   auto
researcher    qwen2.5:7b                 —       auto
refactorer    command-r7b:7b             55 t/s  auto
translator    gemma4:e2b                 27 t/s  auto
…
```

If you'd prefer one model everywhere, `/roles assign-all <provider_id> <model>` overrides every role to that pick. Pin individual roles with `/roles set <role> <provider_id> <model>` (DB-persisted; survives restart).

When no installed model is in the TypeCast catalog (e.g. a single uncatalogued GGUF on llama.cpp), autoconfig falls back to assigning that model to every text-capable role; vision/audio/image_gen roles are left unset unless the model name hints at multimodal capability (`llava`, `qwen-vl`, `gemma-3`, `llama-3.2-vision`, etc.).

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
| `/compact [undo]` | Manually compact the active chat's scratchpad via the summarizer; `/compact undo` restores the most recent compaction's originals |
| `/parallel [on\|off\|status\|route on\|off]` | Toggle opt-in `fan_out` (concurrent role sub-steps) and host routing across the fleet |
| `/vault [set <path>\|off\|search <q>\|status]` | Connect read-only recall over a local notes vault (long-term memory). Path stored in the gitignored project DB |
| `/new [<title>]` | Alias for `/chat new` |
| `/status` | Stacked-bar of per-model usage for the current chat |
| `/agents` | The 19 agents — category, output contract, prompt availability |
| `/tools` | Registered tools |
| `/history` | Recent conversations |
| `/preflight [on\|off\|rules]` | Toggle the pre-dispatch meta-agent; show operational rules |
| `/postmortem [on\|off]` | Toggle the post-failure meta-agent that writes operational rules from observed patterns |

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

- **Conversations + messages** — auto-resume the most recent chat for the project on launch. The replayed transcript is bracketed by `─── begin replay ───` / `─── end replay — type your next prompt below ───` markers so you can tell scrollback from live activity. `/chat new` starts fresh; `/chat clear` is destructive.
- **Chat log files** — every user prompt + assistant final also gets appended to `<wd>/logs/<conversation-start-time>.log` as plain text. New chat → new file. Rotates at 10 MB (up to 20 historical parts).
- **Scratchpad** — the model-facing memory (router decisions, role outputs, tool results) survives across turns and across program restarts. Auto-compacted via the summarizer role when it would exceed `num_ctx × 0.9` — compaction fires near the ceiling so the model keeps as much real history as possible before the summarizer trims it. Manual: `/compact` runs the same routine on demand; `/compact undo` restores the most recent round's originals (compaction marks rows `is_replaced=1` rather than deleting them, so undo is full-fidelity).
- **Provider configs + role assignments** — endpoints and per-role overrides survive in the DB.
- **Per-model throughput** — running-average tokens/second per model, displayed everywhere a model is shown. Updated after every call via `new_avg = (old_avg + rate) / 2` (first measurement is the rate directly). When the provider doesn't report `usage` (some llama.cpp builds), AgentCommander estimates from streamed character count with a shape-aware divisor (CJK 1.5, code 3.0, prose 4.0). Self-measured stats also mirror to a side-by-side `<wd>/.agentcommander/model_stats.json` for transparency. Unmeasured models render as `—` in the role table — no fake default value.
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
| Engine action set | ported + extended | role + tool actions + `done`, plus an opt-in `fan_out` meta-action (concurrent role sub-steps). The decision JSON is now constrained to this action grammar at decode time (see "schema-constrained decoding" below) |
| Engine main loop | ported | scratchpad, generator-based events, guard hook points |
| 9 guard families (~140+ guards) | ported + extended | decision, flow, execute, write, output, fetch, post_step, done + shared types. Recent additions: `live_data_question_guard` (forced-fetch on weather/news/time questions), `tool_call_as_chat_guard` (rejects done.input that's tool syntax as plain text), `chat_category_no_delegation_guard` (chat-category turns shouldn't delegate to specialist roles), `shell_in_wrong_language_guard` (auto-rewrites `execute(language=python, input="python file.py")` to `bash`), `next_steps_guard` lenient bypass (passes done when ≥2 successful tool calls completed), plus the original `unknown_action`/`unassigned_role`/`prompt_template_leak`/`reviewer_verdict`/`tester_verdict` set |
| Tools: file / code / web / process / **http / git / env / browser / vault_search / vault_read** | ported + extended | sandbox-gated (15 tools). `git` is read-only by design; mutating verbs go through `execute`. `git` requires `.git/` in cwd (no climbing to parent repos). `vault_search`/`vault_read` are read-only recall over a local notes vault (see below) |
| Provider: Ollama | ported | streaming via stdlib `urllib`; `keep_alive=5m`; `/api/ps`; `should_cancel` mid-stream |
| Provider: llama.cpp | ported | OpenAI-compat SSE via stdlib `urllib` |
| TypeCast catalog | ported | startup conditional-GET from GitHub → cache → bundled fallback |
| TypeCast autoconfig | ported | threshold-cascade picker with ban list and min-context filter |

## Beyond the port

- **Schema-constrained decoding** — the orchestrator's decision JSON is constrained to a JSON Schema at the sampler level (Ollama `format: <schema>`, llama.cpp / OpenRouter `response_format: json_schema`), not the loose `format: "json"`. The `action` field is locked to the real verb enum, so phantom verbs are impossible at decode time — closing a whole class of failures the guards used to catch after the fact. Schema is derived from the `OrchestratorDecision` dataclass (drift-tested). Deliberately omits `additionalProperties: false` — the closed-set GBNF grammar made a 14B decision call ~10x slower for no correctness gain.
- **Parallel fan-out** (opt-in, `/parallel on`) — the orchestrator can emit one `fan_out` decision whose independent **role** sub-steps (e.g. review + critique + test) run concurrently on a bounded stdlib `ThreadPoolExecutor`. Results integrate in deterministic step order; per-step errors are isolated; `/stop` still unwinds it. Tool actions are never parallelized. Bounded convergence (panel → forced `summarize` → `done`) keeps a weak orchestrator from re-emitting the same fan-out.
- **Makespan-aware host routing** (opt-in, `/parallel route on`) — concurrent sub-steps can run on alternate hosts that have the role's model (*same model, different GPU*), but **only when measured per-`(host, model)` throughput predicts a lower wall-clock**. Offloading to a much slower GPU is avoided (a 3060 ran a 14B ~5x slower than a 4070, so a naive split was 3x slower — the router keeps both on the fast card). Unmeasured hosts are never gambled on; with no data, steps stay on their default host. Throughput is tracked per host in `model_throughput_by_host`.
- **Vault recall** (`vault_search` / `vault_read`, read-only) — long-term memory over a local Obsidian-style notes vault (projects, decisions, infra, patterns). Semantic search reuses an existing `_index/embeddings.json` (cosine in pure Python; query embedded via Ollama `nomic-embed-text`), with a lexical fallback restricted to the indexed/curated note set + stopword-filtered (so it never surfaces raw session archives). Sandboxed read-only to the vault dir; the vault PATH lives in the gitignored project DB and vault content never enters the source tree. The orchestrator prompt carries a recall protocol (named projects, "what was / how did we", decision re-derivation). Configure with `/vault set <path>`.
- **Eval harness** (`evals/`) — replays a committed golden set of prompts through the *real* engine and scores pass/fail + iterations + latency with tolerant assertions. Includes a `--schema-off` A/B control to quantify schema-constrained decoding. Run from `AgentTesting/`; it tracks + deletes its own conversations so the project DB stays clean. Replaces the old eyeball-the-DB stress-test loop.
- **Read-only mirror** (`ac --mirror`) — live event stream replay for a watcher process
- **Live status bar** — bottom-anchored 3 rows showing role/model/tokens/ctx (with fill bar) / run+total timers / running-avg tok/s
- **Streaming token deltas** — coalesced ~10 Hz to balance smooth display with low DB write pressure
- **Cross-turn scratchpad** — model memory persisted in `scratchpad_entries`; auto-compacted via the summarizer role. Cross-turn entries are visible to the orchestrator as context but excluded from the current turn's user-visible answer (turn boundary tracked by `LoopState.turn_start_idx`).
- **JSON verdict contracts on Reviewer / Tester** — both roles emit `{verdict: PASS|FAIL, blockers/failures, summary}`; done-guards parse the JSON and block done with named blockers when FAIL.
- **Project-local DB** — `<cwd>/.agentcommander/db.sqlite`. Each project gets its own state; the catalog cache stays global
- **Auto-resume on startup** — the most recent chat for this project re-renders on launch, bracketed by visual delimiters so replay is distinguishable from live activity
- **Per-model throughput tracking** — running EMA of tok/s shown everywhere a model is named; mirrored to `model_stats.json`. Char-based fallback when providers don't report usage
- **Session context ceiling** — `min(contextLength)` across picked models becomes the announced cap; resolver falls through `/context override → per-role → ceiling → None`. `/context` capped at 50M tokens (anything larger crashes providers)
- **Chat log files** — `<wd>/logs/<conversation-start-time>.log` plain-text transcripts of every user prompt + assistant final, with size-based rotation
- **Auto-fetch from chat-fallback intent** — when chat fallback emits tool syntax as text (e.g. `fetch <url>` or `list_dir`), the engine recognizes the intent, executes the tool, and re-streams chat with the result in context. Synonyms accepted (`ls`/`cat`/`curl`/`rm`/etc.). Same logic when the orchestrator stuffs tool syntax into `done.input`.
- **Deterministic forced-fetch** — when the orchestrator declines twice on a live-data question (weather, time, news), the engine pattern-matches the user message to a known endpoint (wttr.in, worldtimeapi.org, Google News RSS) and runs the fetch itself. Closes the "weak orchestrator can't follow JSON contract" gap on local models.
- **Capability detection** — providers self-report `text` / `vision` / `audio` / `image_gen` capabilities (Ollama via `/api/show`; llama.cpp via name heuristic). Autoconfig's no-catalog fallback uses these to gate which non-text roles a model can fill.
- **Indirect prompt-injection defense** — fetched content scanned for injection patterns; `definite`/`likely` halts the tool at the dispatcher; `suspicious` role-label-mimicry chars (▸ ▶ ▼ ●) get defanged to `>` so fetched pages can't visually impersonate the agent's own status lines in the user's chat.
- **Program-folder check** — refuses to launch with `cwd == AgentCommander source repo root` to prevent polluting the source tree with `.agentcommander/`, `logs/`, and tool-created files.
- **Per-role num_ctx caps** — router defaults to 8 k context (saves KV-cache allocation on local 30B+ models). Overridable via `/context` or `/roles set`.
- **Smaller-router-model startup hint** — when autoconfig binds the same large model to both router and orchestrator, prints a hint suggesting a 1-3B classifier to drop ~5-15 s/turn.
- **Provider startup retry** — 1.5 s + 1 retry on `list_models()` failure to forgive transient daemon restarts.
- **Windows MS-Store-Python-stub workaround** — bash on Windows resolves `python`/`python3` to a stub that exits 49; the engine rewrites those tokens in bash scripts to the real `py -3` interpreter (with backslash → forward-slash conversion).

## Modular layout

```
src/agentcommander/
├── cli.py                  argparse entry — invoked by ac.bat / ac.sh; refuses to
│                           run in the source repo root
├── types.py                shared dataclasses + Role enum
├── registry.py             Protocol-based plugin primitives
├── chat_log.py             plain-text chat transcript writer with rotation
├── model_stats.py          side-by-side model_stats.json with shape-aware token estimation
├── safety/                 dangerous_patterns, sandbox, host_validator,
│                           prompt_injection (incl. defang_role_labels)
├── agents/                 19-role manifest + prompt loader
├── db/                     connection (lock + auto-repair + signals) + schema.sql + repos
├── providers/              base + ollama + llamacpp + capability_hints
│                           (auto-registered on import)
├── tools/                  dispatcher + file_tool / code_tool / web_tool / process_tool /
│                           http_tool / git_tool / env_tool / browser_tool /
│                           vault_tool (read-only notes-vault recall)
├── engine/
│   ├── engine.py           PipelineRun (generator yielding PipelineEvents);
│   │                       _detect_tool_syntax_intent + _honor_tool_text_as_intent
│   │                       + _infer_live_data_url + _dispatch_fan_out
│   ├── decision_schema.py  JSON Schema for the orchestrator decision (action enum
│   │                       from ALL_ACTIONS), used for schema-constrained decoding
│   ├── fan_out.py          parallel fan-out primitive + makespan-aware host routing
│   ├── role_call.py        invoke a role via its assigned provider (optional
│   │                       host/model override for fan-out routing); skips tool
│   │                       registry appendix on router
│   ├── live_tee.py         tee events + bar state into pipeline_events / config
│   ├── role_resolver.py    num_ctx precedence: /context → per-role → ceiling → None;
│   │                       per-role default caps (router=8k)
│   ├── actions.py          ROLE_ACTIONS / TOOL_ACTIONS / ACTION_TO_ROLE / FANOUT_ACTION
│   ├── scratchpad.py       compaction + final-output assembly + compact_conversation_db
│   └── guards/             9 families: output, write, fetch, post_step, decision,
│                           flow, execute, done + shared types
├── typecast/               catalog (conditional-GET), vram detect, autoconfig
│                           (with no-catalog fallback path)
└── tui/                    ansi.py + render.py + markdown.py + commands.py + app.py +
                            status_bar.py + autocomplete.py + terminal_input.py +
                            permissions.py + setup.py + mirror.py + popouts.py
evals/                      scored eval harness — golden cases + tolerant scorers +
                            runner (replays prompts through the real engine).
                            results/ is gitignored; cases/scorers/runner committed
resources/prompts/          19 role .md system prompts (14 refactored to standard
                            Identity / Mission / Critical Rules / Output Contract /
                            Examples / Common Failures / Success Metrics shape;
                            REVIEWER + TESTER emit JSON_STRICT verdicts)
```

Every plugin layer (providers, tools, guard families) is a Python module that registers itself on import. To add a new provider type, drop a `.py` next to `providers/ollama.py` with a `@provider_factory("yourtype")` decorator. Tools follow the same pattern via `register(ToolDescriptor(...))`.

## License

UNLICENSED. Personal fork.
