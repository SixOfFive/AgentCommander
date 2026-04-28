# AgentCommander

Local multi-agent LLM orchestration CLI. Pure-Python (stdlib only — zero dependencies). Mimics the Claude Code Linux console look.

> **Status:** v0.1.0 — full port of the EngineCommander internals (safety layer, 19 agents, 9 guard families, tool dispatcher, providers, TypeCast). Single-user, single-machine. No website, no marketplace, no rentals.

## Concept

> One computer, one LLM, one army of agents.

Pick a model in Ollama. AgentCommander assigns it to all 19 specialized roles (router, orchestrator, planner, coder, reviewer, vision, etc.). The orchestrator runs a guarded serial loop — every iteration emits one JSON action which is dispatched as a role delegation, a tool call, or `done`.

## Design

| Constraint | Decision |
|---|---|
| Dependencies | **Zero.** stdlib only — `urllib`, `sqlite3`, `re`, `argparse`, ANSI escapes |
| Concurrency | **Serial.** No `parallel` action, no async coordination |
| Multi-tenant | **No.** Single user, single working directory |
| Network | Local Ollama / llama.cpp by default; OpenRouter / Anthropic / Google as plug-in providers |
| Plugins | Protocol-based registries — drop a `.py` and register at module top-level |
| UI | Pure ANSI escape codes + `input()` (with `readline` if available) |

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

  ╭──────────────────────────────────────╮
  │   AgentCommander  ·  multi-agent CLI │
  ╰──────────────────────────────────────╯

  v0.1.0  ·  0 provider(s)  ·  305 model(s) in TypeCast catalog  ·  VRAM 12 GB
  workdir: (not set — pick one with /workdir <path>)
  type /help for commands  ·  /quit to exit
```

Add a provider, pick a model, set a working dir:

```text
❯ /providers add ollama-local ollama "Local Ollama" http://127.0.0.1:11434
❯ /models ollama-local
❯ /roles assign-all ollama-local qwen3:8b
❯ /workdir ~/code/scratch
❯ Build me a python script that prints the current weather in NYC
```

`/typecast autoconfigure` will pick the best installed model from the TypeCast catalog and assign per-role overrides where another model strongly outperforms.

## Slash commands

| Command | Purpose |
|---|---|
| `/help` | List all commands |
| `/workdir <path>` | Set the working directory (sandbox boundary) |
| `/providers` | List configured providers |
| `/providers add <id> <type> <name> <endpoint>` | Add a provider (`ollama` or `llamacpp`) |
| `/providers test <id>` | Health check |
| `/providers rm <id>` | Remove |
| `/models <provider_id>` | List installed models |
| `/roles` | Show role → (provider, model) bindings |
| `/roles assign-all <provider_id> <model>` | Assign one model to all 19 roles |
| `/roles set <role> <provider_id> <model>` | Per-role override |
| `/typecast` | Catalog status |
| `/typecast refresh` | Re-fetch from GitHub |
| `/typecast autoconfigure` | Pick best installed model + per-role overrides |
| `/agents` | The 19 agents — category, output contract, prompt availability |
| `/tools` | Registered tools |
| `/history` | Recent conversations |
| `/new <title>` | Start a new conversation |
| `/quit` | Exit |

## What's ported from EngineCommander

| Component | Status | Notes |
|---|---|---|
| Dangerous-command scanner | ✅ verbatim | 30+ patterns: fork bombs, exfil, persistence, privesc, curl-to-shell, reverse shells, shutdown |
| Filesystem sandbox | ✅ adapted | Single working dir; no multi-tenant `EC_DATA_DIR` workspaces |
| SSRF host validator | ✅ verbatim | strict (`validate_user_host`) + permissive (`validate_provider_host`) |
| Prompt-injection detection | ✅ verbatim | 18 patterns; halts pipeline on definite/likely match |
| 19 role prompts | ✅ copied | `resources/prompts/*.md` (mirrors EC) |
| Engine action set | ✅ ported | role + tool actions + `done`. **No `parallel`** (serial-only) |
| Engine main loop | ✅ ported | scratchpad, generator-based events, guard hook points |
| Decision guards | ✅ 8 guards | validate orchestrator JSON before dispatch |
| Flow guards | ✅ 13 guards | repeat caps, oscillation, stale loops, debugger quota |
| Execute guards | ✅ ~28 guards | language detect, missing imports, secrets, prlimit, GUI block |
| Write guards | ✅ 9 guards | empty/duplicate/critical/truncated/identical |
| Output guards | ✅ 9 guards | ANSI strip, base64 strip, secret redaction, truncate |
| Fetch guards | ✅ 7 guards | login walls, JS-only SPAs, paywalls, content mismatch |
| Post-step guards | ✅ 5 guards | dead-end, anti-stuck, repeat-error, ModuleNotFound, NoneType hint |
| Done guards | ✅ ~25 guards | premature-completion blockers + output cleanup |
| Tools: file (read/write/list/delete) | ✅ | sandbox-gated |
| Tool: execute (Python/JS/bash) | ✅ | prlimit on Linux + dangerous-pattern scan |
| Tool: web fetch | ✅ | SSRF + injection scan |
| Tool: process (start/kill/check) | ✅ | prlimit on Linux |
| Provider: Ollama | ✅ | streaming via stdlib `urllib` |
| Provider: llama.cpp | ✅ | OpenAI-compat SSE via stdlib `urllib` |
| TypeCast catalog | ✅ | startup-fetch from GitHub → cache → bundled fallback |
| TypeCast autoconfig | ✅ | VRAM-fitted, score-based default + per-role overrides |
| TUI banner + slash commands | ✅ | pure ANSI |

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
├── safety/                 4 modules: dangerous_patterns, sandbox, host_validator, prompt_injection
├── agents/                 19-role manifest + prompt loader
├── db/                     sqlite3 + schema.sql + repos
├── providers/              base + ollama + llamacpp (auto-registered on import)
├── tools/                  dispatcher + file_tool / code_tool / web_tool / process_tool
├── engine/
│   ├── engine.py           PipelineRun (generator yielding PipelineEvents)
│   ├── role_call.py        invoke a role via its assigned provider
│   ├── actions.py          ROLE_ACTIONS / TOOL_ACTIONS / ACTION_TO_ROLE
│   ├── scratchpad.py       compaction + final-output assembly
│   └── guards/             9 families — output, write, fetch, post_step,
│                           decision, flow, execute, done + shared types
├── typecast/               catalog (fetch+cache), vram detect, autoconfig
└── tui/                    ansi.py + render.py + commands.py + app.py
resources/prompts/          19 role .md system prompts
```

Every plugin layer (providers, tools, guard families) is a Python module that registers itself on import. To add a new provider type, drop a `.py` next to `providers/ollama.py` with a `@provider_factory("yourtype")` decorator. Tools follow the same pattern via `register(ToolDescriptor(...))`.

## License

UNLICENSED. Personal fork.
