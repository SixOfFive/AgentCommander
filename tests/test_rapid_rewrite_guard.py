"""Regression tests for rapid_rewrite_guard's intent-aware completion.

When a 14B orchestrator writes a requested file successfully and then
redundantly re-writes the SAME file without running anything, the old guard
only nudged "execute it first" — which the model ignored, looping until the
iteration cap (2026-06-02 eval: write-hello-file hit the chat cap of 5 and
erred instead of finishing). The guard now splits on intent:

  * pure file-creation request  -> break/done ("Created `X` successfully.")
  * request that wants it RUN    -> keep nudging toward execute

The run-intent regex must NOT match the descriptive "...prints 'hello world'
when run".
"""
from __future__ import annotations

import unittest

from agentcommander.engine.guards.flow_guards import rapid_rewrite_guard
from agentcommander.types import OrchestratorDecision, ScratchpadEntry


def _just_wrote(path: str = "eval_hello.py"):
    return [ScratchpadEntry(step=1, role="tool", action="write_file",
                            input=path, output=f"Successfully wrote {path}",
                            timestamp=0.0)]


def _rewrite_decision(path: str = "eval_hello.py"):
    return OrchestratorDecision(action="write_file", path=path, content="print('hello world')")


class TestRapidRewriteIntent(unittest.TestCase):
    def test_pure_write_breaks_to_done(self):
        v = rapid_rewrite_guard(
            _just_wrote(), 2, _rewrite_decision(), 0, 0,
            "Write a Python file named eval_hello.py that prints 'hello world' when run.",
        )["verdict"]
        self.assertEqual(v["action"], "break")
        self.assertIn("eval_hello.py", v["final_output"])

    def test_pure_create_breaks(self):
        v = rapid_rewrite_guard(
            _just_wrote("config.yaml"), 2, _rewrite_decision("config.yaml"), 0, 0,
            "Create a file named config.yaml with the default settings.",
        )["verdict"]
        self.assertEqual(v["action"], "break")

    def test_run_request_still_nudges_execute(self):
        for um in (
            "Write a reverser, then run it on 'hello'.",
            "Write a script that computes 12 factorial and run it.",
            "Write a script that sums 1..10. What is the output?",
        ):
            with self.subTest(user_message=um):
                v = rapid_rewrite_guard(_just_wrote(), 2, _rewrite_decision(),
                                        0, 0, um)["verdict"]
                self.assertEqual(v["action"], "continue")
                self.assertIsNone(v["final_output"])

    def test_inert_without_prior_write(self):
        # No matching prior write in scratchpad -> guard passes, never breaks.
        v = rapid_rewrite_guard([], 1, _rewrite_decision(), 0, 0,
                                "Write a file named eval_hello.py.")["verdict"]
        self.assertEqual(v["action"], "pass")

    def test_inert_when_execute_between(self):
        # A successful execute between the writes means the rewrite is a real
        # iteration (fix-and-rerun), not a redundant loop -> guard stays out.
        pad = _just_wrote() + [ScratchpadEntry(step=2, role="tool", action="execute",
                                               input="python", output="ran ok",
                                               timestamp=0.0)]
        v = rapid_rewrite_guard(pad, 3, _rewrite_decision(), 0, 0,
                                "Write eval_hello.py.")["verdict"]
        self.assertEqual(v["action"], "pass")


if __name__ == "__main__":
    unittest.main()
