"""Tests for ``shell_in_wrong_language_guard``.

Catches the round-29 / devstral-24B failure mode where the orchestrator
emits ``execute(language=python, input="python hello.py")`` and the
execute tool exits 49 because it tries to interpret ``python hello.py``
as a Python program instead of a shell command. The guard rewrites the
language to ``bash`` so the call succeeds; the orchestrator never has
to reason about this difference.
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


class TestShellLanguageRewrite(unittest.TestCase):
    def test_python_with_python_command_rewrites_to_bash(self) -> None:
        self.assertEqual(_run("execute", "python", "python hello.py"), "bash")

    def test_py_alias_with_python3_rewrites_to_bash(self) -> None:
        self.assertEqual(_run("execute", "py", "python3 main.py"), "bash")

    def test_javascript_with_node_rewrites_to_bash(self) -> None:
        self.assertEqual(_run("execute", "javascript", "node server.js"), "bash")

    def test_js_alias_rewrites_to_bash(self) -> None:
        self.assertEqual(_run("execute", "js", "bash setup.sh"), "bash")

    def test_path_prefix_on_command_still_rewrites(self) -> None:
        # `/usr/bin/python` should be recognized as a python invocation.
        self.assertEqual(_run("execute", "python", "/usr/bin/python foo.py"), "bash")
        # Windows-style path with backslashes too.
        self.assertEqual(_run("execute", "python", r"C:\Python\python.exe foo.py"), "bash")

    def test_pip_install_pattern_rewrites(self) -> None:
        # `pip install requests` is a shell invocation, not python code.
        self.assertEqual(_run("execute", "python", "pip install requests"), "bash")


class TestRealPythonCodeStaysAsPython(unittest.TestCase):
    """The guard must not rewrite legitimate Python code emitted as
    ``language=python``. False positives would break real execute calls."""

    def test_print_call_stays_python(self) -> None:
        self.assertEqual(_run("execute", "python", "print('hi')"), "python")

    def test_import_statement_stays_python(self) -> None:
        self.assertEqual(_run("execute", "python", "import os; print(os.getcwd())"), "python")

    def test_function_definition_stays_python(self) -> None:
        self.assertEqual(_run("execute", "python", "def f(x): return x*2\nprint(f(5))"), "python")

    def test_class_definition_stays_python(self) -> None:
        self.assertEqual(_run("execute", "python", "class C: pass\nprint(C())"), "python")


class TestNonExecuteUntouched(unittest.TestCase):
    """Only ``execute`` is in scope. Other actions pass through unchanged."""

    def test_write_file_with_shellish_input_stays_as_is(self) -> None:
        self.assertEqual(_run("write_file", "python", "python hello.py"), "python")

    def test_fetch_action_unaffected(self) -> None:
        self.assertEqual(_run("fetch", "python", "python foo.py"), "python")


class TestAlreadyCorrectLanguageStaysAlone(unittest.TestCase):
    """If the model already picked ``bash``, ``shell``, ``pip``, etc., we
    don't second-guess it. The rewrite only fires for code-interpreter
    languages that mismatch the input shape."""

    def test_bash_with_shell_command_unchanged(self) -> None:
        self.assertEqual(_run("execute", "bash", "python hello.py"), "bash")

    def test_pip_install_via_pip_language_unchanged(self) -> None:
        # `pip` is a valid `execute` language for pip-install; the input
        # being shell-like ("requests") is the expected shape.
        self.assertEqual(_run("execute", "pip", "requests"), "pip")


class TestEdgeCases(unittest.TestCase):
    def test_empty_input_skipped(self) -> None:
        # No input → nothing to rewrite. (missing_fields_guard handles
        # the empty-input nudge separately.)
        self.assertEqual(_run("execute", "python", ""), "python")

    def test_whitespace_only_input_skipped(self) -> None:
        self.assertEqual(_run("execute", "python", "   \n  "), "python")

    def test_unknown_first_token_no_rewrite(self) -> None:
        # If the first token isn't a known shell command, leave alone —
        # could be a real Python identifier. ``foo bar baz`` is invalid
        # Python, but that's for the execute tool to error on, not us.
        self.assertEqual(_run("execute", "python", "foo bar baz"), "python")


if __name__ == "__main__":
    unittest.main()
