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

**Runtime host-aware routing (framework shipped, opt-in, OFF by default).**
`fan_out.plan_host_routing` + `call_role(provider_id=, model=)` override let
concurrent sub-steps run on distinct hosts that have the role's model (same
model, different GPU). Gated behind `fan_out_route_hosts` (config, default
False) because of a measured finding:

> **Naive spreading HURTS on a heterogeneous-speed fleet.** Live test: a 2-way
> `research` fan-out routed BEAST(4070)+THEOCOMP(3060) at `ministral-3:14b` ran
> in **51s** (bottlenecked on the 3060's ~51s vs the 4070's ~10s), while
> keeping both on the 4070 ran in **15.7s** — routing was **0.31×** (3× slower).
> Offloading to a much slower GPU makes it the makespan bottleneck.

**Makespan-aware routing — DONE (2026-06-02).** Routing is now throughput-aware
and safe: it offloads a sub-step to an alternate host only when measured
per-`(host, model)` throughput predicts a lower wall-clock; otherwise it keeps
the step on its fast default host. Added an additive `model_throughput_by_host`
table + `record_throughput(provider_id=)` + `get_throughput_for_host`;
`plan_host_routing` takes a `throughput_fn` and greedily minimises makespan
(load on host = 1/throughput). Unmeasured alternates are never gambled on.
Live-verified: BEAST 10.8 t/s vs THEOCOMP 5.4 t/s (2× gap) → router kept BOTH
research steps on BEAST (no slow-node split). Toggle: `/parallel route on|off`
(`fan_out_route_hosts`, default off; ON in the AgentTesting project DB).

**Other follow-ups (LATER):** read-only tool fan-out (multi-source `fetch`);
per-step rate-limit retry with UI countdown (a rate-limited worker currently
records a failed sub-step); live streaming of concurrent steps into separate
popout blocks.

### Vault recall — long-term memory over a local notes vault (DONE, 2026-06-02)
Read-only tools `vault_search` + `vault_read` give the orchestrator recall over
a local Obsidian-style vault (projects, decisions, infra, patterns).
- `tools/vault_tool.py`: semantic search reuses an existing
  `_index/embeddings.json` (cosine in pure Python; query embedded via Ollama
  `nomic-embed-text`), falls back to lexical. Lexical is restricted to the
  indexed/curated note set + stopword-filtered, so it never surfaces raw
  session archives (info-hygiene). Sandboxed read-only to the vault dir.
- Verbs added to `TOOL_ACTIONS` (flow into the #1 schema enum + guards);
  `_decision_to_payload` maps them; `repeated_tool_call_guard` caps
  `vault_search`=3 / `vault_read`=5 (a weak orchestrator looped 13x otherwise).
- `/vault set|off|search|status`; vault PATH in the project-local DB
  (gitignored). Vault CONTENT only ever lands in the gitignored project DB/logs
  — never the source tree. Tool code is generic (committed); no private data.
- ORCHESTRATOR.md gained a recall protocol (when to search: named projects,
  "what was/how did we", decision re-derivation).
- 15 tests in `tests/test_vault_tool.py` (incl. dotted-name regression:
  os.path.splitext truncated "llama.cpp…"→"llama"). Live-verified end-to-end:
  search→read→answer against the real 1,626-note vault.

**Read-only enforcement (owner directive — AC must never write the vault):**
three layers — (1) the vault tools only open files for reading; (2) a sandbox
*read-only zone* (`safety/sandbox.register_readonly_zone`, synced from config in
`dispatcher.invoke`) refuses `write_file`/`delete_file` inside the vault even
when the working directory IS the vault; (3) a `code_tool` guard blocks
`execute` code that writes/deletes inside the vault. Live-verified all three
block while reads pass. Best-effort on `execute` (arbitrary code) — the
airtight guarantee is an OS read-only ACL on the vault. Tests in
`tests/test_vault_readonly.py` (8).

**Caveats / follow-ups:** synthesis faithfulness is model-bound (a 14B read the
right note but summarized loosely) — a stronger orchestrator or a forced
summarizer-over-read-content pass would help. The embeddings index is
maintained externally (vault-maintenance); AC is a read-only consumer.

### #6 — TypeCast hint accumulator (was "deferred" in braindump)
Verified already wired: `engine._bump_hint_for_label` bumps `(model, role)`
by ±0.1 on classify/orchestrate/role success/failure; `db.repos` persists to
`model_hints`; `typecast/autoconfig.py` folds the hints into role scoring.
No action needed — braindump's "deferred" note is stale.

### Binding/error robustness (DONE, 2026-06-02)
Surfaced by a live fan_out research run that 404'd (researcher bound to
`cogito:8b`, not installed on BEAST):
- **Clearer Ollama errors** (`providers/ollama.py`): a 404 now reads "model 'X'
  not found on <endpoint> — install it or rebind" instead of bare "HTTP 404".
- **`/roles set` model validation** (`tui/commands.verify_model_installed`):
  ✓ confirms / ⚠ warns + suggests closest installed names / notes if
  unreachable. Non-blocking. Catches ghost bindings at bind time.
- Tests: `test_ollama_404.py` (2), `test_roles_verify.py` (6).

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

## Field findings

- **RESOLVED:** the orchestrator was resolving to a broken llama.cpp
  `Llama-4-Scout-17B` (empty content). Rebound in the AgentTesting DB to
  `qwen2.5:14b` on BEAST (router→gemma3:4b, panel roles split BEAST/THEOCOMP,
  summarizer→qwen2.5:14b). Researcher was a ghost (`cogito:8b`, uninstalled) →
  fixed via `/roles set researcher … llama3:8b-instruct-q4_K_M`. The new
  `/roles set` model validation now catches this class at bind time.
- The `100.106.215.128:8080` host is a llama.cpp server mis-registered as an
  *ollama*-type provider (so `/api/chat` 404s). If you want to use it, register
  it with `/providers add … llamacpp` instead of ollama.

## Quality follow-ups (model-bound, not structural)

- **Vault synthesis faithfulness** — a 14B reads the right note but summarizes
  loosely. A forced `summarize`-over-`vault_read`-content pass (or a stronger
  orchestrator) would tighten recall answers.
- **fan_out live UI** — concurrent sub-steps don't stream; the status bar can't
  show them running side-by-side. Wiring each concurrent role into its own
  popout block (the popout system exists) would make parallelism visible.
