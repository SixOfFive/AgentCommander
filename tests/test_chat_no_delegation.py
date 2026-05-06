"""Tests for ``chat_category_no_delegation_guard``.

Round-43 caught the orchestrator delegating to ``tester`` on a junk
chat-category input ("b1-trav"), wasting iterations on small-talk.
The guard nudges once per turn; the orchestrator stays in charge if
it insists after the nudge.
"""
from __future__ import annotations

import time
import unittest

from agentcommander.engine.guards.decision_guards import (
    chat_category_no_delegation_guard,
)
from agentcommander.types import OrchestratorDecision, ScratchpadEntry


def _entry(role: str, action: str, input_: str = "", output: str = "") -> ScratchpadEntry:
    return ScratchpadEntry(
        step=0, role=role, action=action,
        input=input_, output=output, timestamp=time.time(),
    )


def _router(category: str) -> ScratchpadEntry:
    return _entry("router", "classify", input_="x", output=category)


def _delegation(action: str) -> OrchestratorDecision:
    return OrchestratorDecision(action=action, reasoning="", input="x")


class TestChatCategoryNoDelegation(unittest.TestCase):
    def test_chat_plus_test_action_nudges(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("test"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "continue",
                         "delegation on chat-category should be nudged + continue")
        # The nudge should have been appended.
        nudges = [e for e in scratchpad if e.action == "system_nudge"
                  and e.input == "chat_no_delegation"]
        self.assertEqual(len(nudges), 1, "should push exactly one nudge")

    def test_chat_plus_code_action_nudges(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("code"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "continue")

    def test_chat_plus_planner_action_nudges(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("plan"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "continue")


class TestNonChatCategoryAllowsDelegation(unittest.TestCase):
    """Other categories (code/project/research/question) freely allow
    role delegation — that's the whole point of multi-agent flows."""

    def test_code_category_allows_test_delegation(self) -> None:
        scratchpad = [_router("code")]
        verdict = chat_category_no_delegation_guard(
            _delegation("test"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")
        self.assertFalse(any(e.action == "system_nudge" for e in scratchpad))

    def test_project_category_allows_planner_delegation(self) -> None:
        scratchpad = [_router("project")]
        verdict = chat_category_no_delegation_guard(
            _delegation("plan"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")

    def test_research_category_allows_researcher_delegation(self) -> None:
        scratchpad = [_router("research")]
        verdict = chat_category_no_delegation_guard(
            _delegation("research"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")


class TestNonRoleActionsPassThrough(unittest.TestCase):
    """Tool actions and `done` aren't delegations. Pass through cleanly
    even on chat category."""

    def test_chat_done_passes(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("done"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")

    def test_chat_fetch_passes(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("fetch"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")

    def test_chat_write_file_passes(self) -> None:
        scratchpad = [_router("chat")]
        verdict = chat_category_no_delegation_guard(
            _delegation("write_file"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")


class TestSingleFire(unittest.TestCase):
    """The guard must NOT keep firing once it's nudged. If the
    orchestrator insists on delegating after seeing the nudge, allow
    it — single-fire policy."""

    def test_second_call_with_nudge_present_passes(self) -> None:
        scratchpad = [_router("chat")]
        # First call: nudge fires
        v1 = chat_category_no_delegation_guard(
            _delegation("test"), scratchpad, iteration=1,
        )
        self.assertEqual(v1.action, "continue")
        # Second call (next iteration, orchestrator re-emits delegation): pass
        v2 = chat_category_no_delegation_guard(
            _delegation("test"), scratchpad, iteration=2,
        )
        self.assertEqual(v2.action, "pass",
                         "second delegation attempt after nudge should pass")
        # And the nudge wasn't pushed twice.
        nudges = [e for e in scratchpad if e.action == "system_nudge"
                  and e.input == "chat_no_delegation"]
        self.assertEqual(len(nudges), 1, "still only one nudge in scratchpad")


class TestNoRouterEntry(unittest.TestCase):
    """Defensive: if scratchpad has no router entry yet, don't nudge.
    Probably means we're called outside the normal flow — fail open."""

    def test_no_router_entry_passes(self) -> None:
        scratchpad: list[ScratchpadEntry] = []
        verdict = chat_category_no_delegation_guard(
            _delegation("test"), scratchpad, iteration=1,
        )
        self.assertEqual(verdict.action, "pass")


if __name__ == "__main__":
    unittest.main()
