# AgentCommander eval harness

A committed, scored regression suite for the orchestration engine. It replays
a golden set of prompts through the **real** engine (your configured
providers + role assignments), scores each result against tolerant
assertions, and reports pass/fail + iteration count + latency per case.

This replaces the old "run a shell battery, eyeball the DB" loop. Now you can
answer questions like *"did that new guard help or just add latency?"* and
*"did schema-constrained decoding change the pass rate?"* with numbers.

## Why it runs the real engine

Mocks can't tell you what actually ships. The harness drives a live
`PipelineRun` exactly the way the TUI does — same role resolution, same
guards, same tools — just without the terminal UI. Output is nondeterministic
(local models), so the scorers assert *properties* ("the answer contains 4",
"a `write_file` tool succeeded", "every action was a real verb"), never exact
strings.

## Running

**Always run from `AgentTesting/`** so it uses your real DB
(`AgentTesting/.agentcommander/db.sqlite`) with your providers and role
assignments. The harness never copies the DB and never touches the api_key;
every conversation it creates is deleted when the case finishes.

```bash
cd AgentTesting

# validate the case file + scorers, no LLM calls
PYTHONUTF8=1 PYTHONPATH=../src py -3 ../evals/run_eval.py --dry-run

# quick smoke — one fast case
PYTHONUTF8=1 PYTHONPATH=../src py -3 ../evals/run_eval.py --id math-subtract

# offline regression (skip network-dependent cases)
PYTHONUTF8=1 PYTHONPATH=../src py -3 ../evals/run_eval.py --skip-tag live

# full suite
PYTHONUTF8=1 PYTHONPATH=../src py -3 ../evals/run_eval.py
```

### A/B: measure the effect of schema-constrained decoding (#1)

```bash
# baseline (schema ON) then control (schema OFF), compare pass rate + iters
... run_eval.py --skip-tag live
... run_eval.py --skip-tag live --schema-off
```

`--schema-off` disables the JSON-Schema constraint so the orchestrator falls
back to loose `format:"json"` — the pre-#1 behavior. Compare the two runs'
summaries (and `results/history.tsv`) to see the delta.

## Files

| File | Committed? | What |
|---|---|---|
| `cases.jsonl` | yes | Golden prompts + expected checks. One JSON object per line. |
| `scorers.py` | yes | Tolerant assertion functions (`contains`, `tool_fired`, `action_in_set`, …). |
| `harness.py` | yes | Headless engine bootstrap + `run_case()` with per-case timeout. |
| `run_eval.py` | yes | CLI runner: select, score, report, persist. |
| `results/` | **no** (gitignored) | Per-run JSON + `history.tsv` scoreboard. Machine/run specific. |

## Adding a case

Append a line to `cases.jsonl`:

```json
{"id": "my-case", "category": "chat", "tags": [], "timeout_s": 180,
 "prompt": "…", "checks": [{"type": "no_error"}, {"type": "contains", "value": "…"}],
 "notes": "why this case exists"}
```

Tag a case `"filesystem"` to have the runner give it a throwaway, pre-authorized
working dir (for `write_file` / `execute`). Tag it `"live"`/`"network"` if it
needs the internet, so `--skip-tag live` can exclude it.

### Scorer types

`contains`, `contains_all`, `contains_any`, `not_contains`, `regex`,
`min_length`, `action_in_set`, `tool_fired`, `role_fired`, `max_iterations`,
`no_error`. See `scorers.py` for arguments. A case passes only if **all** its
checks pass.

`action_in_set` (no args) asserts every decision verb the orchestrator emitted
is a registered action — the in-engine proof that schema-constrained decoding
is holding.
