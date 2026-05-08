"""Tests for ``chat_category_no_delegation_guard`` (round-43).

Trim: kept the canonical positive (chat + role action), the negative
(non-chat allows), the single-fire invariant, and the no-router-entry
defensive case. Dropped redundant action-variants.
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


class TestChatNoDelegation(unittest.TestCase):
    def test_chat_plus_role_nudges(self) -> None:
        scratchpad = [_router("chat")]
        v = chat_category_no_delegation_guard(_delegation("test"), scratchpad, 1)
        self.assertEqual(v.action, "continue")
        nudges = [e for e in scratchpad if e.action == "system_nudge"
                  and e.input == "chat_no_delegation"]
        self.assertEqual(len(nudges), 1)

    def test_non_chat_allows_delegation(self) -> None:
        scratchpad = [_router("code")]
        v = chat_category_no_delegation_guard(_delegation("test"), scratchpad, 1)
        self.assertEqual(v.action, "pass")

    def test_single_fire_then_passes(self) -> None:
        # Critical invariant: don't fight the model — one nudge, then allow.
        scratchpad = [_router("chat")]
        chat_category_no_delegation_guard(_delegation("test"), scratchpad, 1)
        v2 = chat_category_no_delegation_guard(_delegation("test"), scratchpad, 2)
        self.assertEqual(v2.action, "pass")

    def test_no_router_entry_passes(self) -> None:
        # Defensive: outside normal flow, fail open.
        v = chat_category_no_delegation_guard(_delegation("test"), [], 1)
        self.assertEqual(v.action, "pass")


if __name__ == "__main__":
    unittest.main()
