"""Tests for ``shell_in_wrong_language_guard`` (round-29 fix).

Trim: kept the canonical positive (the round-29 trace), the critical
false-positive defense (real Python stays Python), the .exe stripping
edge case, the non-execute pass-through, and the empty-input skip.
Dropped redundant variants of each.
"""
from __future__ import annotations

import unittest

from agentcommander.engine.guards.decision_guards import (
    shell_in_wrong_language_guard,
)
from agentcommander.types import OrchestratorDecision


def _run(action: str, language: str, input_str: str) -> str:
    d = OrchestratorDecision(
        action=action, language=language, input=input_str, reasoning="",
    )
    shell_in_wrong_language_guard(d, [], 1)
    return d.language or ""


class TestShellInWrongLanguage(unittest.TestCase):
    def test_round29_trace_rewrites_to_bash(self) -> None:
        # `python hello.py` tagged as language=python → bash
        self.assertEqual(_run("execute", "python", "python hello.py"), "bash")

    def test_real_python_code_preserved(self) -> None:
        # Critical false-positive defense.
        self.assertEqual(_run("execute", "python", "print('hi')"), "python")
        self.assertEqual(_run("execute", "python", "def f(x): return x*2"), "python")

    def test_exe_extension_stripped_for_match(self) -> None:
        # `python.exe foo.py` should still be recognised as a shell call.
        self.assertEqual(_run("execute", "python",
                              r"C:\Python\python.exe foo.py"), "bash")

    def test_non_execute_action_pass_through(self) -> None:
        # Only execute is in scope.
        self.assertEqual(_run("write_file", "python", "python hello.py"), "python")

    def test_empty_input_skipped(self) -> None:
        self.assertEqual(_run("execute", "python", ""), "python")


if __name__ == "__main__":
    unittest.main()
