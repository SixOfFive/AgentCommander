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


if __name__ == "__main__":
    unittest.main()
