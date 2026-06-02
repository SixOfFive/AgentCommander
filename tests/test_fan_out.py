"""Tests for the parallel fan-out primitive (fleet-utilization prototype).

Covers the pure primitive in ``engine/fan_out.py``: step validation,
deterministic ordering, genuine concurrency (timing), the serial degrade
path, and per-step error isolation. ``call_role`` and ``resolve_role`` are
faked so no provider/DB is needed.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from agentcommander.engine import fan_out as fo


class _RR:
    def __init__(self, model):
        self.model = model
        self.provider_id = "p"
        self.context_window_tokens = None


class TestValidateSteps(unittest.TestCase):

    def test_keeps_role_actions(self) -> None:
        steps = [{"action": "review", "input": "x"},
                 {"action": "critique", "input": "y"},
                 {"action": "test", "input": "z"}]
        runnable, skipped = fo.validate_steps(steps)
        self.assertEqual(len(runnable), 3)
        self.assertEqual(skipped, [])

    def test_rejects_tools_done_and_garbage(self) -> None:
        steps = [
            {"action": "review", "input": "ok"},
            {"action": "write_file", "path": "x"},   # side-effecting tool
            {"action": "done"},                       # terminal
            {"action": "fan_out", "steps": []},       # no nesting
            "not-a-dict",                             # malformed
        ]
        runnable, skipped = fo.validate_steps(steps)
        self.assertEqual([s["action"] for s in runnable], ["review"])
        self.assertEqual(len(skipped), 4)

    def test_empty(self) -> None:
        self.assertEqual(fo.validate_steps(None), ([], []))
        self.assertEqual(fo.validate_steps([]), ([], []))


class TestRunFanOut(unittest.TestCase):

    def _patch(self, call_impl):
        return (
            mock.patch.object(fo, "call_role", side_effect=call_impl),
            mock.patch.object(fo, "resolve_role", return_value=_RR("m")),
        )

    def test_results_in_step_order_regardless_of_completion(self) -> None:
        # Make the FIRST step the SLOWEST so completion order != submit order.
        delays = {"a": 0.30, "b": 0.15, "c": 0.02}

        def fake(role, *, user_input, **kw):
            time.sleep(delays[user_input])
            return f"out:{user_input}"

        p1, p2 = self._patch(fake)
        subs = [{"action": "review", "input": "a"},
                {"action": "critique", "input": "b"},
                {"action": "test", "input": "c"}]
        with p1, p2:
            results = fo.run_fan_out(subs, scratchpad_text="", conversation_id=None,
                                     parallel=True, max_workers=4)
        # Ordered by input index, NOT by who finished first.
        self.assertEqual([r.input for r in results], ["a", "b", "c"])
        self.assertEqual([r.output for r in results],
                         ["out:a", "out:b", "out:c"])
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual([r.index for r in results], [0, 1, 2])

    def test_parallel_actually_overlaps(self) -> None:
        def fake(role, *, user_input, **kw):
            time.sleep(0.25)
            return "x"

        p1, p2 = self._patch(fake)
        subs = [{"action": "review", "input": str(i)} for i in range(3)]
        with p1, p2:
            t0 = time.time()
            fo.run_fan_out(subs, scratchpad_text="", conversation_id=None,
                           parallel=True, max_workers=4)
            wall = time.time() - t0
        # 3×0.25=0.75s serial; concurrent should be well under half that.
        self.assertLess(wall, 0.5, f"expected overlap, wall={wall:.2f}s")

    def test_serial_does_not_overlap(self) -> None:
        def fake(role, *, user_input, **kw):
            time.sleep(0.1)
            return "x"

        p1, p2 = self._patch(fake)
        subs = [{"action": "review", "input": str(i)} for i in range(3)]
        with p1, p2:
            t0 = time.time()
            results = fo.run_fan_out(subs, scratchpad_text="", conversation_id=None,
                                     parallel=False)
            wall = time.time() - t0
        self.assertEqual([r.input for r in results], ["0", "1", "2"])
        self.assertGreaterEqual(wall, 0.25, f"serial should sum, wall={wall:.2f}s")

    def test_per_step_error_isolated(self) -> None:
        def fake(role, *, user_input, **kw):
            if user_input == "boom":
                raise RuntimeError("provider exploded")
            return f"out:{user_input}"

        p1, p2 = self._patch(fake)
        subs = [{"action": "review", "input": "ok1"},
                {"action": "critique", "input": "boom"},
                {"action": "test", "input": "ok2"}]
        with p1, p2:
            results = fo.run_fan_out(subs, scratchpad_text="", conversation_id=None,
                                     parallel=True)
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertIn("provider exploded", results[1].error)
        self.assertTrue(results[2].ok)
        # The good steps still produced output.
        self.assertEqual(results[0].output, "out:ok1")
        self.assertEqual(results[2].output, "out:ok2")

    def test_single_step_uses_serial_path(self) -> None:
        # len==1 short-circuits to the sequential branch even with parallel=True.
        def fake(role, *, user_input, **kw):
            return "solo"

        p1, p2 = self._patch(fake)
        with p1, p2:
            results = fo.run_fan_out([{"action": "review", "input": "x"}],
                                     scratchpad_text="", conversation_id=None,
                                     parallel=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].output, "solo")

    def test_cancel_callback_threaded_through(self) -> None:
        seen = {}

        def fake(role, *, user_input, should_cancel=None, **kw):
            seen["cancel"] = should_cancel
            return "x"

        p1, p2 = self._patch(fake)
        sentinel = lambda: False  # noqa: E731
        with p1, p2:
            fo.run_fan_out([{"action": "review", "input": "x"}],
                           scratchpad_text="", conversation_id=None,
                           should_cancel=sentinel, parallel=False)
        self.assertIs(seen["cancel"], sentinel)


class TestHostRouting(unittest.TestCase):
    """plan_host_routing is makespan-aware: spread to an alternate host only
    when per-host throughput says it reduces predicted wall-clock."""

    class _RRObj:
        def __init__(self, pid, model):
            self.provider_id = pid
            self.model = model

    def _resolver(self, table):
        # table: {Role: (provider_id, model)}
        return lambda role: (self._RRObj(*table[role]) if role in table else None)

    def _tps(self, table):
        # table: {(provider_id, model): tokens_per_second}; missing → None
        return lambda pid, model: table.get((pid, model))

    def test_no_throughput_data_stays_on_default(self) -> None:
        # Safety: with no per-host throughput known, never gamble on an
        # alternate — keep every step on its fast default host.
        from agentcommander.types import Role
        table = {Role.RESEARCHER: ("A", "M")}
        installed = {"A": {"M"}, "B": {"M"}}
        subs = [{"action": "research", "input": str(i)} for i in range(3)]
        planned = fo.plan_host_routing(subs, resolve_fn=self._resolver(table),
                                       installed_by_provider=installed)
        self.assertEqual([p["provider_id"] for p in planned], ["A", "A", "A"])
        self.assertFalse(any(p["_rerouted"] for p in planned))

    def test_comparable_hosts_split(self) -> None:
        # Equal throughput on both hosts → splitting 2 steps halves makespan.
        from agentcommander.types import Role
        table = {Role.RESEARCHER: ("A", "M")}
        installed = {"A": {"M"}, "B": {"M"}}
        tps = self._tps({("A", "M"): 50.0, ("B", "M"): 50.0})
        subs = [{"action": "research", "input": str(i)} for i in range(2)]
        planned = fo.plan_host_routing(subs, resolve_fn=self._resolver(table),
                                       installed_by_provider=installed, throughput_fn=tps)
        self.assertEqual([p["provider_id"] for p in planned], ["A", "B"])
        self.assertEqual([p["_rerouted"] for p in planned], [False, True])

    def test_much_slower_alt_keeps_both_on_fast(self) -> None:
        # The real-fleet case: B (3060) ~5x slower than A (4070). Splitting
        # would bottleneck on B, so the makespan greedy keeps BOTH on A.
        from agentcommander.types import Role
        table = {Role.RESEARCHER: ("A", "M")}
        installed = {"A": {"M"}, "B": {"M"}}
        tps = self._tps({("A", "M"): 50.0, ("B", "M"): 10.0})
        subs = [{"action": "research", "input": str(i)} for i in range(2)]
        planned = fo.plan_host_routing(subs, resolve_fn=self._resolver(table),
                                       installed_by_provider=installed, throughput_fn=tps)
        self.assertEqual([p["provider_id"] for p in planned], ["A", "A"])
        self.assertFalse(any(p["_rerouted"] for p in planned))

    def test_three_steps_two_fast_hosts(self) -> None:
        from agentcommander.types import Role
        table = {Role.RESEARCHER: ("A", "M")}
        installed = {"A": {"M"}, "B": {"M"}}
        tps = self._tps({("A", "M"): 50.0, ("B", "M"): 50.0})
        subs = [{"action": "research", "input": str(i)} for i in range(3)]
        planned = fo.plan_host_routing(subs, resolve_fn=self._resolver(table),
                                       installed_by_provider=installed, throughput_fn=tps)
        # balanced 2/1 across the two equal hosts, default first
        self.assertEqual([p["provider_id"] for p in planned], ["A", "B", "A"])

    def test_unmeasured_alt_never_used(self) -> None:
        # A measured+fast, B unmeasured → B is not gambled on; both stay on A.
        from agentcommander.types import Role
        table = {Role.RESEARCHER: ("A", "M")}
        installed = {"A": {"M"}, "B": {"M"}}
        tps = self._tps({("A", "M"): 50.0})  # B unknown
        subs = [{"action": "research", "input": str(i)} for i in range(2)]
        planned = fo.plan_host_routing(subs, resolve_fn=self._resolver(table),
                                       installed_by_provider=installed, throughput_fn=tps)
        self.assertEqual([p["provider_id"] for p in planned], ["A", "A"])

    def test_unresolvable_role_passes_through(self) -> None:
        planned = fo.plan_host_routing(
            [{"action": "research", "input": "x"}],
            resolve_fn=lambda role: None, installed_by_provider={"A": {"M"}})
        self.assertIsNone(planned[0]["provider_id"])
        self.assertFalse(planned[0]["_rerouted"])


class TestFanOutDecisionGuards(unittest.TestCase):
    """fan_out must survive the decision-guard chain (regression: it was
    rejected by unknown_action_guard, causing an infinite re-orchestrate loop)."""

    def _decision(self):
        from agentcommander.types import OrchestratorDecision
        return OrchestratorDecision(
            action="fan_out", reasoning="panel",
            steps=[{"action": "review", "input": "r"},
                   {"action": "critique", "input": "c"},
                   {"action": "test", "input": "t"}])

    def test_fan_out_is_a_known_action(self) -> None:
        from agentcommander.engine.guards.decision_guards import _SPECIAL_ACTIONS, _all_known_actions
        self.assertIn("fan_out", _SPECIAL_ACTIONS)
        self.assertIn("fan_out", _all_known_actions())

    def test_fan_out_passes_decision_guards(self) -> None:
        from agentcommander.engine.guards.decision_guards import run_decision_guards
        res = run_decision_guards({
            "decision": self._decision(), "scratchpad": [], "iteration": 1,
            "user_message": "review critique test", "browser_available": False,
        })
        self.assertEqual(res["verdict"]["action"], "pass")
        self.assertEqual(res["decision"].action, "fan_out")

    def test_unknown_action_guard_excludes_fan_out_from_menu(self) -> None:
        # fan_out is special — it must not be advertised in the generic
        # "pick one of" nudge for a truly-unknown action.
        from agentcommander.engine.guards.decision_guards import (
            _all_known_actions, _SPECIAL_ACTIONS)
        menu = _all_known_actions() - _SPECIAL_ACTIONS
        self.assertNotIn("fan_out", menu)


if __name__ == "__main__":
    unittest.main()
