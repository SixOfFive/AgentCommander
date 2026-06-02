"""Regression tests for question_only_done_guard's intent awareness.

question_only_done_guard nudges a short ``done`` that is just a question
bounced back to the user — UNLESS the user explicitly asked the model to ask a
question. The 2026-06-02 v2 eval (case ``clarifying-question``, prompt "Ask me
exactly one question to get started") caught the guard firing twice on exactly
that legitimate output, forcing the model to pad its reply over two wasted
iterations. The guard had no view of the user message until that fix. These
tests pin the intent allowlist while keeping the genuine bounce-back nudge.
"""
from __future__ import annotations

import unittest

from agentcommander.engine.guards.preventive_guards import question_only_done_guard
from agentcommander.types import OrchestratorDecision


def _verdict(user_message: str, text: str):
    decision = OrchestratorDecision(action="done", input=text)
    return question_only_done_guard([], 0, decision, user_message)


class TestQuestionOnlyIntent(unittest.TestCase):
    def test_user_asked_for_a_question_passes(self):
        # The reported regression.
        v = _verdict(
            "I want to book a flight but haven't decided the details. "
            "Ask me exactly one question to get started.",
            "Where would you like to travel?",
        )
        self.assertEqual(v.action, "pass")

    def test_clarification_phrasings_pass(self):
        for um in (
            "Help me scaffold the project — ask a clarifying question first.",
            "Before you start, what would you need to know?",
            "Ask me a question to narrow it down.",
            "Pose one question so we can begin.",
        ):
            with self.subTest(user_message=um):
                self.assertEqual(_verdict(um, "What framework are you using?").action,
                                 "pass")

    def test_genuine_bounce_back_still_nudged(self):
        # Control: the user did NOT invite a question. A bare question bounced
        # back instead of answering must STILL be nudged — the fix only adds an
        # intent exemption, it must not disable the guard.
        v = _verdict("What is the capital of France?",
                     "What do you mean by capital?")
        self.assertEqual(v.action, "continue")

    def test_no_user_message_preserves_legacy_behavior(self):
        # Defaulted/empty user_message must not crash and must keep nudging a
        # bare question (back-compat with any caller not passing the message).
        v = _verdict("", "What do you mean?")
        self.assertEqual(v.action, "continue")


if __name__ == "__main__":
    unittest.main()
