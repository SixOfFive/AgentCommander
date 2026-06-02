#!/usr/bin/env python3
"""Eval runner for AgentCommander (improvement #3).

Replays a committed golden set of prompts through the real engine, scores
each result against tolerant assertions, and reports pass/fail + iterations +
latency per case. Converts the old "run a battery, eyeball the DB" loop into
a measurable regression harness.

USAGE — always from AgentTesting/ (so it uses the real DB with your
providers + role assignments; see CLAUDE.md / braindump):

    cd AgentTesting
    PYTHONUTF8=1 PYTHONPATH=../src py -3 ../evals/run_eval.py [options]

Common invocations:

    # validate the case file + scorers without any LLM calls
    ... run_eval.py --dry-run

    # quick smoke: one fast case
    ... run_eval.py --id math-subtract

    # offline regression (skip network-dependent cases)
    ... run_eval.py --skip-tag live

    # A/B the effect of schema-constrained decoding (#1): run once normally,
    # once with --schema-off, compare pass rates + iterations
    ... run_eval.py --schema-off

Results: a per-run JSON under evals/results/ and a one-line summary appended
to evals/results/history.tsv (both gitignored — machine/run specific). The
golden cases, scorers, and this runner ARE committed.

Pure stdlib.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Make the harness importable whether invoked as `evals/run_eval.py` or
# `../evals/run_eval.py` from AgentTesting/.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))

import harness  # noqa: E402
import scorers  # noqa: E402

_RESULTS_DIR = _THIS.parent / "results"


def _load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: bad JSON: {exc}")
    return cases


def _select(cases: list[dict], args) -> list[dict]:
    out = cases
    if args.id:
        wanted = set(args.id)
        out = [c for c in out if c.get("id") in wanted]
    if args.category:
        cats = set(args.category)
        out = [c for c in out if c.get("category") in cats]
    if args.tag:
        tags = set(args.tag)
        out = [c for c in out if tags & set(c.get("tags", []))]
    if args.skip_tag:
        skip = set(args.skip_tag)
        out = [c for c in out if not (skip & set(c.get("tags", [])))]
    if args.limit:
        out = out[: args.limit]
    return out


def _validate_cases(cases: list[dict]) -> list[str]:
    """Dry-run validation: every case well-formed, every scorer known."""
    problems: list[str] = []
    known = set(scorers.known_scorer_types())
    seen_ids: set[str] = set()
    for c in cases:
        cid = c.get("id", "<no-id>")
        if cid in seen_ids:
            problems.append(f"{cid}: duplicate id")
        seen_ids.add(cid)
        if not c.get("prompt"):
            problems.append(f"{cid}: missing prompt")
        checks = c.get("checks") or []
        if not checks:
            problems.append(f"{cid}: no checks")
        for chk in checks:
            if chk.get("type") not in known:
                problems.append(f"{cid}: unknown scorer {chk.get('type')!r}")
    return problems


def _patch_schema_off() -> None:
    """A/B control: disable schema-constrained decoding so the orchestrator
    falls back to loose json_mode. Lets the runner measure #1's effect."""
    import agentcommander.engine.engine as engine_mod
    engine_mod.orchestrator_decision_schema = lambda: None  # type: ignore[assignment]


def _fmt_ms(ms: int) -> str:
    s = ms / 1000.0
    return f"{s:5.1f}s" if s < 60 else f"{int(s)//60}m{int(s)%60:02d}s"


def _guard_counts() -> dict[str, int]:
    """Snapshot {guard_name: total_fire_count} from the telemetry table (#4).

    Diffing this around each case attributes guard fires to the prompt that
    triggered them — the signal for spotting false-positive guards (a guard
    that fires on a case whose output was actually fine) and dead guards
    (never fires across a diverse suite). Returns {} if the table/DB isn't
    available so the runner degrades gracefully.
    """
    try:
        from agentcommander.db.repos import guard_fire_stats
        return {s["guard"]: int(s["count"]) for s in guard_fire_stats()}
    except Exception:  # noqa: BLE001
        return {}


def _guard_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Per-case fire counts: guards whose count rose between two snapshots."""
    out: dict[str, int] = {}
    for guard, n in after.items():
        d = n - before.get(guard, 0)
        if d > 0:
            out[guard] = d
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentCommander eval runner")
    ap.add_argument("--cases", default=str(_THIS.parent / "cases.jsonl"))
    ap.add_argument("--id", action="append", help="run only these case ids")
    ap.add_argument("--category", action="append", help="filter by category")
    ap.add_argument("--tag", action="append", help="only cases with these tags")
    ap.add_argument("--skip-tag", action="append", help="exclude cases with these tags")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="override per-case timeout (seconds)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate cases + scorers, no LLM calls")
    ap.add_argument("--schema-off", action="store_true",
                    help="disable schema-constrained decoding (A/B control for #1)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"cases file not found: {cases_path}", file=sys.stderr)
        return 2
    all_cases = _load_cases(cases_path)
    selected = _select(all_cases, args)

    problems = _validate_cases(selected)
    if args.dry_run:
        print(f"loaded {len(all_cases)} case(s), selected {len(selected)}")
        if problems:
            print("VALIDATION PROBLEMS:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("all selected cases valid; scorers known.")
        for c in selected:
            print(f"  {c['id']:24} [{c.get('category','?'):12}] "
                  f"{len(c.get('checks',[]))} check(s)  tags={c.get('tags',[])}")
        return 0

    if problems:
        print("refusing to run — fix these case problems (or use --dry-run):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if not selected:
        print("no cases selected.")
        return 0

    if args.schema_off:
        _patch_schema_off()

    env = harness.bootstrap()
    schema_mode = "OFF (loose json_mode)" if args.schema_off else "ON (constrained)"
    print("=" * 72)
    print(f"AgentCommander eval — {len(selected)} case(s)  "
          f"schema-constrained decoding: {schema_mode}")
    print(f"providers: {[p['id'] for p in env['providers']]}")
    orch = env["roles"].get("orchestrator", "?")
    print(f"orchestrator → {orch}   router → {env['roles'].get('router','?')}")
    print("=" * 72)

    run_records: list[dict] = []
    n_pass = 0
    suite_started = time.time()

    for i, case in enumerate(selected, 1):
        cid = case["id"]
        timeout_s = args.timeout or float(case.get("timeout_s", 300))
        wd = None
        tmp = None
        if "filesystem" in case.get("tags", []):
            tmp = tempfile.mkdtemp(prefix=f"eval-{cid}-", dir=str(Path.cwd()))
            wd = tmp
            # Pre-authorize non-interactive writes/exec/read in the temp dir.
            try:
                from agentcommander.tui.permissions import grant_subtree
                for op in ("read", "write", "execute", "delete"):
                    grant_subtree(tmp, op, "allow")
            except Exception:  # noqa: BLE001
                pass

        print(f"\n[{i}/{len(selected)}] {cid}  ({case.get('category','?')}, "
              f"timeout {int(timeout_s)}s)")
        if not args.quiet:
            print(f"    prompt: {case['prompt'][:90]}")

        guards_before = _guard_counts()
        result = harness.run_case(case["prompt"], timeout_s=timeout_s,
                                  working_directory=wd)
        guards_fired = _guard_delta(guards_before, _guard_counts())
        rdict = result.to_dict()
        passed, details = scorers.score_case(rdict, case.get("checks", []))
        n_pass += int(passed)

        status = "PASS" if passed else "FAIL"
        flags = []
        if result.timed_out:
            flags.append("TIMEOUT")
        if result.error:
            flags.append("ERR")
        flag_s = f"  [{' '.join(flags)}]" if flags else ""
        print(f"    {status}{flag_s}  {_fmt_ms(result.duration_ms)}  "
              f"iters={result.iterations}  roles={result.roles}")
        if not args.quiet or not passed:
            for d in details:
                mark = "ok " if d["passed"] else "XX "
                print(f"      {mark}{d['type']}: {d['detail']}")
            if not passed and result.final:
                preview = result.final.replace("\n", " ")[:160]
                print(f"      final: {preview}")

        run_records.append({
            "id": cid,
            "category": case.get("category"),
            "passed": passed,
            "details": details,
            "result": rdict,
        })

        if tmp:
            try:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

    suite_ms = int((time.time() - suite_started) * 1000)
    rate = (100.0 * n_pass / len(selected)) if selected else 0.0
    print("\n" + "=" * 72)
    print(f"RESULT: {n_pass}/{len(selected)} passed ({rate:.0f}%)   "
          f"total {_fmt_ms(suite_ms)}   schema={schema_mode}")
    print("=" * 72)

    _write_results(env, args, selected, run_records, n_pass, suite_ms)
    return 0 if n_pass == len(selected) else 1


def _write_results(env, args, selected, run_records, n_pass, suite_ms) -> None:
    try:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        payload = {
            "stamp": stamp,
            "schema_constrained": not args.schema_off,
            "orchestrator": env["roles"].get("orchestrator"),
            "passed": n_pass,
            "total": len(selected),
            "suite_ms": suite_ms,
            "cases": run_records,
        }
        out = _RESULTS_DIR / f"{stamp}.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        hist = _RESULTS_DIR / "history.tsv"
        new = not hist.exists()
        with hist.open("a", encoding="utf-8") as fh:
            if new:
                fh.write("stamp\tschema\torchestrator\tpassed\ttotal\trate\tsuite_ms\n")
            rate = (100.0 * n_pass / len(selected)) if selected else 0.0
            fh.write(f"{stamp}\t{'on' if not args.schema_off else 'off'}\t"
                     f"{env['roles'].get('orchestrator')}\t{n_pass}\t{len(selected)}\t"
                     f"{rate:.0f}\t{suite_ms}\n")
        print(f"wrote {out}")
    except Exception as exc:  # noqa: BLE001
        print(f"(could not write results: {exc})")


if __name__ == "__main__":
    raise SystemExit(main())
