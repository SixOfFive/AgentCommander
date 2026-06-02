# AgentCommander — improvement roadmap

Engineering backlog distilled from a full read of the codebase + braindump
(rounds 1–51) on 2026-06-02. Ordered by leverage. Items respect the project's
hard constraints: **stdlib-only, modular, local-first, project-local DB**, and
no paid services. **Serial-only was relaxed** (owner decision, 2026-06-02) for
the opt-in parallel fan-out prototype below — AC remains serial by default.

## Status legend
- **DONE** — shipped + tested
- **NEXT** — highest-value remaining work
- **LATER** — worthwhile, not urgent
- **PARKED** — deliberately deferred (see note)

---

## DONE

### #1 — Schema-constrained orchestrator decoding
Constrain the orchestrator's JSON output to the `OrchestratorDecision` schema
at the sampler level (Ollama `format:<schema>`, llama.cpp / OpenRouter
`response_format:json_schema`) instead of the loose `format:"json"`. The
`action` field is constrained to the 27 real verbs, so phantom verbs are
impossible at decode time. Module: `engine/decision_schema.py` (schema derived
from the dataclass, drift-tested). 18 tests in `tests/test_decision_schema.py`.
Live-verified on BEAST: gemma3:4b emitted no `action` field under loose JSON
but a valid verb under the schema.

> **Follow-up (#1b, LATER):** strict *per-action* required fields via
> `if/then/else` once a strong orchestrator makes the larger GBNF grammar
> affordable. Today only `action` is required and `missing_fields_guard`
> backstops per-action requirements. See the `decision_schema.py` docstring.

### #3 — Scored eval harness
`evals/` — golden cases + tolerant scorers + a runner that replays prompts
through the real engine and reports pass/fail + iterations + latency. Includes
a `--schema-off` A/B control to quantify #1. Replaces the eyeball-the-DB loop.
See `evals/README.md`.

### Parallel fan-out (prototype) — fleet utilization
**Relaxes the historical "serial-only / no parallel action" hard constraint**,
by owner decision (2026-06-02). The orchestrator can emit one `fan_out`
decision whose independent **role** sub-steps (reuse the existing `steps`
field) run concurrently on a bounded stdlib `ThreadPoolExecutor`; each sub-step
uses its role's assigned provider, so binding panel roles to different hosts
puts multiple GPUs to work at once.

- `engine/fan_out.py` — pure primitive (`validate_steps`, `run_fan_out`),
  deterministic step-order results, per-step error isolation, cancellation
  threaded through.
- `engine.py::_dispatch_fan_out` — flag-gated integration; **disabled →
  sequential degrade** (same results), which also gives the eval harness a
  clean parallel-vs-serial A/B.
- `actions.py` — `FANOUT_ACTION` + `FANOUT_SUB_ACTIONS` (role verbs only;
  side-effecting tools never parallelized); auto-flows into the #1 schema enum.
- `/parallel on|off|status` (config `fan_out_enabled`, default OFF).
- Thread-safety: SQLite is already lock-serialized; added a module lock +
  unique temp filename to `model_stats.json` writes.
- 9 tests in `tests/test_fan_out.py` (overlap timing, ordering determinism,
  error isolation). Live: ran a reviewer/critic/tester panel across BEAST +
  THEOCOMP — 2.4× step overlap; correct ordered results.

Wired end-to-end and live-verified (2026-06-02): orchestrator emits `fan_out`,
panel runs concurrently across BEAST+THEOCOMP (~1.8–2.5× overlap), engine
converges (panel → forced `summarize` → `done`) in ~54s. Fixes made while
making it work:
- ORCHESTRATOR.md gained an accurate `fan_out` section; **three phantom
  sections removed** (`Batch Actions`, `Parallel Batch Execution`, `Parallel
  role execution`) — none was ever wired in the engine.
- `unknown_action_guard` now recognizes `fan_out` (added to `_SPECIAL_ACTIONS`)
  — it was rejecting every fan_out, causing an infinite re-orchestrate loop.
- Bounded convergence in `events()`: 2nd fan_out in a turn → forced summarize;
  3rd → done. Stops the orchestrator re-emitting the same panel forever.
- **#1 schema perf fix (important):** dropped `additionalProperties:false` from
  the decision schema — on qwen2.5:14b it cut a constrained orchestrator call
  from **134s → 12s** (the closed-set grammar was pathological). No correctness
  loss (`from_dict` drops unknown keys).

**Follow-ups (LATER):** read-only tool fan-out (multi-source `fetch`); per-step
rate-limit retry with UI countdown (a worker that hits a rate limit currently
records a failed sub-step); live streaming of concurrent steps into separate
popout blocks. **Fleet caveat:** speedup ≈ sum/slowest-step — large only when
sub-steps land on *distinct* hosts (same-host Ollama calls contend for one GPU).

### #6 — TypeCast hint accumulator (was "deferred" in braindump)
Verified already wired: `engine._bump_hint_for_label` bumps `(model, role)`
by ±0.1 on classify/orchestrate/role success/failure; `db.repos` persists to
`model_hints`; `typecast/autoconfig.py` folds the hints into role scoring.
No action needed — braindump's "deferred" note is stale.

---

## NEXT

### #4 — Guard telemetry + `/guards stats`
**Problem.** There are ~140+ guards across 9 families (`preventive_guards.py`
alone is 1537 lines). Nobody knows which actually fire in practice, so the
suite only grows — never shrinks. You flagged "too many" at round 41.

**Approach.**
- Add a `guard_fires` table (`family, guard_name, verdict, conversation_id,
  run_id, ts`) and a counter bump at each guard's decision point (or
  centrally in the guard-runner loop in `engine/guards/*`).
- `/guards stats` slash command (`tui/commands.py`): table sorted by fire
  count, with last-fired timestamp and pass/break/continue breakdown.
- Cross-reference with an eval run (#3): guards that never fire across the
  full golden suite + normal usage are deletion candidates — and many should
  now be dead weight after #1 (`unknown_action`, `sentence_as_action`, parts
  of `field_swap`/`missing_fields`).

**Files.** `db/schema.sql`, `db/repos.py`, `engine/guards/` runners,
`tui/commands.py`. **Effort:** ~half day. **Acceptance:** after a golden-suite
run, `/guards stats` lists fire counts; ≥1 guard provably removable.

### #5 — Break up `engine.py` (2770 lines)
**Problem.** Modularity is a hard constraint, but `engine.py` has become a
god-file: pipeline loop + chat fallback + compaction + `_LIVE_DATA_PATTERNS_FORCED`
+ `_infer_live_data_url` + `_detect_tool_syntax_intent` + `_honor_tool_text_as_intent`
+ `_payload_from_textual_call`.

**Approach.** Extract the weak-model recovery subsystem into
`engine/recovery.py` (tool-syntax-as-intent, forced live-data fetch, payload
building) and the compaction helpers are already partly in `scratchpad.py`.
Bonus: much of this recovery code becomes removable once #1/#4 prove the
guards/coercion it backstops no longer fire — do #4's measurement first so
the split doubles as a delete.

**Files.** New `engine/recovery.py`; `engine/engine.py` shrinks to the loop.
**Effort:** ~1 day (mechanical, test-covered). **Acceptance:** `engine.py`
< ~1500 lines; 276 unit tests + eval suite unchanged.

---

## LATER

### #7 — Orchestrator fast-path for single-step intents
**Problem.** Every turn pays a full orchestrator round-trip even for trivial
single-tool / single-role requests. On a slow local orchestrator that's the
dominant latency.

**Approach.** The router already classifies intent. When the classification is
an unambiguous single tool/role (e.g. "what's the weather" → fetch, "translate
X" → translator) and the message has no multi-step markers ("then", "after"),
dispatch directly and skip the orchestrator call. Guarded, opt-out, measured
against #3 to confirm no quality regression.

**Files.** `engine/engine.py` (pre-orchestrate shortcut), `engine/actions.py`
(intent→action map). **Effort:** ~1 day. **Acceptance:** eval iteration counts
drop for single-step cases with no pass-rate loss.

### #8 — Token-accurate counting where it's free
**Problem.** The ctx bar, compaction trigger, and num_ctx decisions run on a
char-based estimate (`model_stats.estimate_tokens_from_chars`).

**Approach.** llama.cpp exposes an exact `/tokenize` endpoint — use it for
llama.cpp-backed roles; keep the shape-aware estimate as the Ollama fallback
(Ollama has no clean public tokenize). Surface "exact" vs "estimated" the same
way `model_stats` already flags throughput.

**Files.** `providers/llamacpp.py` (+`tokenize()`), `providers/base.py`
(default estimate), call sites in `engine/scratchpad.py` / `tui/status_bar.py`.
**Effort:** ~half day. **Acceptance:** llama.cpp roles report exact token
counts; Ollama unchanged.

---

## PARKED

### #2 — OpenRouter as a first-class orchestrator backend
**Status: parked by owner (2026-06-02).** Local models are the point; the
free OpenRouter tier rate-limits hard and the good models cost money, which
the owner doesn't want to spend. NVIDIA's hosted models are reserved for a
separate project and must not be touched here.

**Keep-for-future note.** The plumbing is already mostly present
(`providers/openrouter.py`, `typecast/openrouter_catalog.py`, the
`resources/typecast-openrouter-free.json` catalog, and #1 already wired the
`json_schema` response_format for OpenRouter). If the owner revisits cloud
later, the remaining work is: (a) a clean `/roles set orchestrator <or-provider>
<model>` flow that survives autoconfigure, (b) the rate-limit→swap path
exercised end-to-end, (c) a spend cap UI for paid models. A strong cloud
orchestrator would sidestep the local-hardware latency wall and make most of
the weak-model recovery code (see #5) removable — but only revisit on the
owner's say-so.

---

## Field findings (act-on-when-convenient)

- **Orchestrator role currently resolves to a llama.cpp `Llama-4-Scout-17B`
  (split GGUF at `100.106.215.128:8080`) that returns empty content** (~2 min
  per call; identical with schema on/off). Surfaced by the first `evals` run.
  This makes the live engine non-functional for real prompts right now —
  rebind the orchestrator to a working model (`/roles set orchestrator
  auto-192.168.15.103-11434 qwen2.5:14b` scored PASS in 11.5 s) or fix the
  llama-server hosting that GGUF. Not an engine bug.
