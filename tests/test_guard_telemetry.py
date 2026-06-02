"""Tests for guard telemetry (#4): name extraction + fire counters."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentcommander.engine.guards.types import guard_name, record_fire


# Module-level guards so a lambda referencing them puts the name in co_names
# (a global), mirroring how the real runners wrap guards.
def sample_guard(*a):
    return None


def another_guard(*a):
    return None


class TestGuardName(unittest.TestCase):
    def test_bare_function(self):
        self.assertEqual(guard_name(sample_guard), "sample_guard")

    def test_lambda_wrapper(self):
        self.assertEqual(guard_name(lambda: sample_guard(1, 2, 3)), "sample_guard")

    def test_lambda_picks_guard_suffixed_name(self):
        # Even if the lambda references other globals, the *_guard one wins.
        self.assertEqual(guard_name(lambda: (len([]), another_guard())), "another_guard")

    def test_fallback(self):
        self.assertEqual(guard_name(object()), "<guard>")


class TestFireCounters(unittest.TestCase):
    def setUp(self):
        from agentcommander.db.connection import init_db
        self.dir = tempfile.mkdtemp(prefix="guardtel-")
        init_db(str(Path(self.dir) / "db.sqlite"))

    def test_pass_is_noop(self):
        from agentcommander.db.repos import guard_fire_stats
        record_fire("decision", sample_guard, "pass")
        self.assertEqual(guard_fire_stats(), [])

    def test_records_and_increments(self):
        from agentcommander.db.repos import guard_fire_stats, record_guard_fire
        record_guard_fire("decision", "unknown_action_guard", "continue")
        record_guard_fire("decision", "unknown_action_guard", "continue")
        record_guard_fire("done", "next_steps_guard", "continue")
        stats = {(s["family"], s["guard"]): s["count"] for s in guard_fire_stats()}
        self.assertEqual(stats[("decision", "unknown_action_guard")], 2)
        self.assertEqual(stats[("done", "next_steps_guard")], 1)

    def test_sorted_by_count_desc(self):
        from agentcommander.db.repos import guard_fire_stats, record_guard_fire
        record_guard_fire("flow", "a_guard", "break")
        for _ in range(3):
            record_guard_fire("flow", "b_guard", "continue")
        stats = guard_fire_stats()
        self.assertEqual(stats[0]["guard"], "b_guard")  # higher count first

    def test_record_fire_via_helper(self):
        from agentcommander.db.repos import guard_fire_stats
        record_fire("write", sample_guard, "continue")
        stats = guard_fire_stats()
        self.assertEqual(stats[0]["guard"], "sample_guard")
        self.assertEqual(stats[0]["verdict"], "continue")

    def test_clear(self):
        from agentcommander.db.repos import (
            clear_guard_fires, guard_fire_stats, record_guard_fire)
        record_guard_fire("decision", "x_guard", "continue")
        self.assertEqual(clear_guard_fires(), 1)
        self.assertEqual(guard_fire_stats(), [])


if __name__ == "__main__":
    unittest.main()
