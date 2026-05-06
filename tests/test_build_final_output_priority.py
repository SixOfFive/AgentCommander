"""Tests for build_final_output priority rules.

The function picks one of: summarizer / content-role / execute-stdout /
step-report when surfacing a turn's final answer. Round-42 (T6 math
word-problem trace) caught a precedence bug: when the coder produced
code AND a successful execute followed, the function shipped the code
instead of the execute's stdout. This file pins that behavior down so
future refactors can't regress it.
"""
from __future__ import annotations

import time
import unittest

from agentcommander.engine.scratchpad import build_final_output
from agentcommander.types import ScratchpadEntry


def _entry(step: int, role: str, action: str,
           input_: str, output: str, ts_offset: float = 0.0) -> ScratchpadEntry:
    return ScratchpadEntry(
        step=step, role=role, action=action,
        input=input_, output=output,
        timestamp=time.time() + ts_offset,
    )


# A coder output long enough to trip the > 80-char content-role gate.
LONG_CODER_OUTPUT = (
    '```python\n'
    'def catch_up_time():\n'
    '    """Train head-start divided by relative speed."""\n'
    '    head_start = 60  # miles (1 hour at 60 mph)\n'
    '    relative_speed = 80 - 60  # mph\n'
    '    hours = head_start / relative_speed\n'
    '    return f"4 pm + {hours} hours = 7 pm"\n'
    '\n'
    'print(catch_up_time())\n'
    '```'
)
EXEC_OK_7PM = "successfully completed:\n--- stdout ---\n4 pm + 3.0 hours = 7 pm"


class TestCoderFollowedByExecute(unittest.TestCase):
    """When coder writes code AND a successful execute follows in the
    same turn, the execute stdout is the answer (the code was the means)."""

    def test_execute_stdout_appears_before_code(self) -> None:
        pad = [
            _entry(0, "router", "classify", "math word problem", "question"),
            _entry(1, "coder", "code", "train catch-up", LONG_CODER_OUTPUT, 1.0),
            _entry(2, "tool", "write_file", "catch_up.py",
                   "successfully completed:\nSuccessfully wrote 312 bytes", 2.0),
            _entry(3, "tool", "execute", "python catch_up.py", EXEC_OK_7PM, 3.0),
        ]
        final = build_final_output(pad, current_turn_start=0)
        # Execute stdout marker must come before the code in the rendered output.
        result_pos = final.find("7 pm")
        code_pos = final.find("def catch_up_time")
        self.assertGreaterEqual(result_pos, 0, "execute stdout missing")
        if code_pos >= 0:
            self.assertLess(result_pos, code_pos,
                            "execute stdout must come before the code in the final")

    def test_execution_output_block_present(self) -> None:
        pad = [
            _entry(0, "router", "classify", "x", "question"),
            _entry(1, "coder", "code", "y", LONG_CODER_OUTPUT, 1.0),
            _entry(2, "tool", "execute", "python x.py", EXEC_OK_7PM, 2.0),
        ]
        final = build_final_output(pad, current_turn_start=0)
        # The dedicated "Execution Output:" header should fire — that's
        # the surfacing path the precedence fix routes to.
        self.assertIn("Execution Output", final)


class TestCoderWithoutExecute(unittest.TestCase):
    """Without a successful execute, the coder output IS the answer.
    Don't break the standard "show me the code" path while fixing the
    code+execute path."""

    def test_coder_output_dominates_when_no_execute(self) -> None:
        pad = [
            _entry(0, "router", "classify", "x", "code"),
            _entry(1, "coder", "code", "y", LONG_CODER_OUTPUT, 1.0),
        ]
        final = build_final_output(pad, current_turn_start=0)
        # Without a successful execute, the coder's content is what
        # the user wants. We accept either the raw coder output OR
        # the step-report version — both surface the code.
        self.assertIn("def catch_up_time", final)

    def test_coder_with_failed_execute_keeps_code_in_view(self) -> None:
        # Execute failed (SyntaxError) — the user still wants to see
        # the code so they can fix it. The "successful execute"
        # criterion guards against demoting coder for failures.
        failed = "exit code 1\n--- stderr ---\nSyntaxError: invalid syntax"
        pad = [
            _entry(0, "router", "classify", "x", "code"),
            _entry(1, "coder", "code", "y", LONG_CODER_OUTPUT, 1.0),
            _entry(2, "tool", "execute", "python x.py", failed, 2.0),
        ]
        final = build_final_output(pad, current_turn_start=0)
        # The code should still be reachable in the final output.
        self.assertIn("def catch_up_time", final)


class TestNonCoderContentRoles(unittest.TestCase):
    """Researcher / vision / translator etc. output is the answer; an
    incidental successful execute that follows shouldn't displace them."""

    def test_researcher_output_not_displaced_by_execute(self) -> None:
        researcher_out = (
            "TCP and UDP comparison:\n\n"
            "* TCP is connection-oriented and reliable, with ordered delivery.\n"
            "* UDP is connectionless and faster, with no delivery guarantees.\n"
            "* Use TCP for web browsing and email; UDP for streaming and games."
        )
        pad = [
            _entry(0, "router", "classify", "compare TCP UDP", "research"),
            _entry(1, "researcher", "research", "compare", researcher_out, 1.0),
            # Some incidental execute happened (e.g. checking a fact)
            _entry(2, "tool", "execute", "python check.py",
                   "successfully completed:\n--- stdout ---\nverified", 2.0),
        ]
        final = build_final_output(pad, current_turn_start=0)
        # Researcher output should dominate; the precedence demotion
        # is specific to coder, not all content roles.
        self.assertIn("connection-oriented", final)


if __name__ == "__main__":
    unittest.main()
