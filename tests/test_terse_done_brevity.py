"""Regression tests for terse_done_guard's brevity allowlist.

terse_done_guard nudges a one-word ``done`` (``OK``/``yes``/...) that did no
work — UNLESS the user explicitly asked for a terse reply. The 2026-06-02 eval
(case 7, "Respond with exactly: OK") showed "exactly" was missing from the
allowlist, so a correct one-word answer got nudged (2 iters / 57s instead of 1).
These tests pin the fix and guard against the guard over-firing again, while
keeping the genuine lazy-done nudge intact.
"""
from __future__ import annotations

import unittest

from agentcommander.engine.guards.done_guards import terse_done_guard
from agentcommander.types import OrchestratorDecision


def _verdict(user_message: str, text: str = "OK", *, scratchpad=None,
             iteration: int = 0, max_iter: int = 10):
    decision = OrchestratorDecision(action="done", input=text)
    return terse_done_guard(scratchpad if scratchpad is not None else [],
                            iteration, max_iter, decision, user_message)


class TestTerseDoneBrevity(unittest.TestCase):
    def test_exactly_is_allowlisted(self):
        # The reported regression: "exactly" now reads as an explicit terse ask.
        self.assertEqual(_verdict("Respond with exactly: OK").action, "pass")

    def test_brevity_synonyms_allowlisted(self):
        for um in (
            "Reply verbatim: OK",
            "Answer precisely: yes",
            "Respond literally with: done",
            "Say exactly OK",
        ):
            with self.subTest(user_message=um):
                self.assertEqual(_verdict(um, text=um.split()[-1]).action, "pass")

    def test_existing_just_only_still_allowlisted(self):
        # No regression to the pre-existing allowlist entries.
        self.assertEqual(_verdict("Reply with just: OK").action, "pass")
        self.assertEqual(_verdict("Only say yes", text="yes").action, "pass")

    def test_genuine_lazy_done_still_nudged(self):
        # Control: a long task with a bare "OK" and no work done must STILL
        # nudge — the fix must not blanket-disable the guard.
        v = _verdict("Research the history of the Roman aqueduct system "
                     "and summarize the key engineering innovations.", text="OK")
        self.assertEqual(v.action, "continue")


if __name__ == "__main__":
    unittest.main()
