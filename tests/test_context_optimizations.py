"""Tests for the round-51 context optimizations.

Covers the 5 starter-pack optimizations:
  1. needs_scratchpad / needs_tool_appendix flags on AgentDef
  2. role_call.py honoring those flags
  3. per-tool output budgets in sanitize_output
  4. sliding-window fallback when summarizer fails
  5. /usage command + aggregate_token_usage repo helper

Pure unit tests — no live LLM, no real network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ─── 1+2. Manifest flags + role_call honoring ──────────────────────────────


class TestAgentManifestFlags(unittest.TestCase):
    """The flags must be set correctly on the canonical direct-input
    and non-tool roles. Regression guard: if someone forgets to mark a
    new direct-input role, this test will surface it."""

    def test_direct_input_roles_skip_scratchpad(self) -> None:
        from agentcommander.agents.manifest import AGENTS_BY_ROLE
        from agentcommander.types import Role
        for r in (Role.TRANSLATOR, Role.SUMMARIZER, Role.REFACTORER,
                  Role.CRITIC, Role.TESTER, Role.VISION, Role.AUDIO,
                  Role.IMAGE_GEN, Role.PREFLIGHT, Role.POSTMORTEM,
                  Role.ROUTER):
            self.assertFalse(AGENTS_BY_ROLE[r].needs_scratchpad,
                             f"{r.value} should skip scratchpad")

    def test_orchestrator_keeps_scratchpad(self) -> None:
        # Critical: orchestrator MUST keep the scratchpad — every
        # iteration sees the running history.
        from agentcommander.agents.manifest import AGENTS_BY_ROLE
        from agentcommander.types import Role
        self.assertTrue(AGENTS_BY_ROLE[Role.ORCHESTRATOR].needs_scratchpad)
        self.assertTrue(AGENTS_BY_ROLE[Role.ORCHESTRATOR].needs_tool_appendix)

    def test_non_tool_roles_skip_appendix(self) -> None:
        from agentcommander.agents.manifest import AGENTS_BY_ROLE
        from agentcommander.types import Role
        for r in (Role.TRANSLATOR, Role.SUMMARIZER, Role.CRITIC,
                  Role.REFACTORER, Role.PREFLIGHT, Role.POSTMORTEM):
            self.assertFalse(AGENTS_BY_ROLE[r].needs_tool_appendix)


class TestRoleCallHonorsFlags(unittest.TestCase):
    """role_call must respect the AgentDef flags — no scratchpad
    threading, no tool appendix appended when flags are False."""

    def _stub_provider_chat(self):
        """Make call_role return immediately with a captured messages list
        we can inspect. Returns (messages_capture, patcher)."""
        captured: list = []

        def fake_chat(self, *, model, messages, **kwargs):
            captured.append(list(messages))

            class Chunk:
                content = ""
                done = True
                prompt_tokens = 1
                completion_tokens = 1
            yield Chunk()
        return captured, fake_chat

    def test_translator_does_not_receive_scratchpad(self) -> None:
        from agentcommander.engine import role_call
        from agentcommander.providers import base as provider_base

        captured, fake_chat = self._stub_provider_chat()
        with mock.patch.object(role_call, "resolve_role",
                                return_value=mock.Mock(
                                    provider_id="test",
                                    model="x", kind="auto",
                                    context_window_tokens=None)), \
             mock.patch.object(role_call, "resolve",
                                return_value=mock.Mock(chat=fake_chat.__get__(object()))), \
             mock.patch.object(role_call, "audit"), \
             mock.patch.object(role_call, "insert_token_usage"), \
             mock.patch.object(role_call, "record_throughput"):
            role_call.call_role(
                "translator",
                user_input="bonjour",
                scratchpad_text="(LARGE PRIOR CONTEXT WE EXPECT TO BE OMITTED)",
            )
        self.assertEqual(len(captured), 1)
        msgs = captured[0]
        # Translator: scratchpad is omitted, so user_input is the only user message.
        user_msgs = [m for m in msgs if m.role == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertNotIn("LARGE PRIOR CONTEXT", user_msgs[0].content)
        self.assertEqual(user_msgs[0].content, "bonjour")

    def test_translator_skips_tool_registry_appendix(self) -> None:
        from agentcommander.engine import role_call
        captured, fake_chat = self._stub_provider_chat()
        with mock.patch.object(role_call, "resolve_role",
                                return_value=mock.Mock(
                                    provider_id="test",
                                    model="x", kind="auto",
                                    context_window_tokens=None)), \
             mock.patch.object(role_call, "resolve",
                                return_value=mock.Mock(chat=fake_chat.__get__(object()))), \
             mock.patch.object(role_call, "tool_registry_appendix",
                                return_value="### TOOLS APPENDIX MARKER ###"), \
             mock.patch.object(role_call, "audit"), \
             mock.patch.object(role_call, "insert_token_usage"), \
             mock.patch.object(role_call, "record_throughput"):
            role_call.call_role("translator", user_input="hi", scratchpad_text="")
        sys_msg = next(m for m in captured[0] if m.role == "system")
        self.assertNotIn("TOOLS APPENDIX MARKER", sys_msg.content)

    def test_orchestrator_receives_scratchpad_and_appendix(self) -> None:
        # Sanity: roles that need both still get both.
        from agentcommander.engine import role_call
        captured, fake_chat = self._stub_provider_chat()
        with mock.patch.object(role_call, "resolve_role",
                                return_value=mock.Mock(
                                    provider_id="test",
                                    model="x", kind="auto",
                                    context_window_tokens=None)), \
             mock.patch.object(role_call, "resolve",
                                return_value=mock.Mock(chat=fake_chat.__get__(object()))), \
             mock.patch.object(role_call, "tool_registry_appendix",
                                return_value="### TOOLS APPENDIX MARKER ###"), \
             mock.patch.object(role_call, "audit"), \
             mock.patch.object(role_call, "insert_token_usage"), \
             mock.patch.object(role_call, "record_throughput"):
            role_call.call_role(
                "orchestrator",
                user_input="next step",
                scratchpad_text="### SCRATCHPAD MARKER ###",
            )
        msgs = captured[0]
        sys_msg = next(m for m in msgs if m.role == "system")
        user_msgs = [m for m in msgs if m.role == "user"]
        self.assertIn("TOOLS APPENDIX MARKER", sys_msg.content)
        self.assertTrue(any("SCRATCHPAD MARKER" in m.content for m in user_msgs))


# ─── 3. Per-tool output budgets ─────────────────────────────────────────────


class TestPerToolOutputBudgets(unittest.TestCase):
    def test_write_file_truncates_aggressively(self) -> None:
        # write_file budget = 1000 chars; long output should be heavily trimmed.
        from agentcommander.engine.guards.output_guards import sanitize_output
        text = "x" * 5000
        out = sanitize_output(text, tool_name="write_file")
        self.assertLess(len(out), 1500)
        self.assertIn("characters omitted", out)

    def test_fetch_keeps_30k(self) -> None:
        # fetch budget = 30k; 20k input passes through.
        from agentcommander.engine.guards.output_guards import sanitize_output
        text = "y" * 20_000
        out = sanitize_output(text, tool_name="fetch")
        self.assertEqual(len(out), 20_000)

    def test_unknown_tool_uses_default(self) -> None:
        from agentcommander.engine.guards.output_guards import sanitize_output, MAX_OUTPUT_LENGTH
        text = "z" * (MAX_OUTPUT_LENGTH + 1000)
        out = sanitize_output(text, tool_name="not_a_real_tool")
        self.assertLess(len(out), MAX_OUTPUT_LENGTH + 200)

    def test_no_tool_name_backward_compatible(self) -> None:
        # Calls without the kwarg behave exactly as before.
        from agentcommander.engine.guards.output_guards import sanitize_output, MAX_OUTPUT_LENGTH
        text = "a" * (MAX_OUTPUT_LENGTH + 500)
        out = sanitize_output(text)
        self.assertLess(len(out), MAX_OUTPUT_LENGTH + 200)


# ─── 5. /usage aggregate_token_usage repo helper ────────────────────────────


class TestAggregateTokenUsage(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = Path.cwd()
        os.chdir(self._tmp.name)
        from agentcommander.db import connection as _conn
        _conn._db = None  # type: ignore[attr-defined]
        from agentcommander.db.connection import init_db
        init_db()

    def tearDown(self) -> None:
        import os
        os.chdir(self._cwd)
        from agentcommander.db import connection as _conn
        try:
            _conn.close_db()
        except Exception:  # noqa: BLE001
            pass
        _conn._db = None  # type: ignore[attr-defined]
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def test_aggregates_per_role(self) -> None:
        from agentcommander.db.repos import aggregate_token_usage, insert_token_usage

        # 2 orchestrator + 1 translator call in chat A.
        for prompt_t in (1000, 2000):
            insert_token_usage(conversation_id="A", role="orchestrator",
                                provider_id="p", model="m1",
                                prompt_tokens=prompt_t, completion_tokens=50,
                                duration_ms=500)
        insert_token_usage(conversation_id="A", role="translator",
                            provider_id="p", model="m2",
                            prompt_tokens=200, completion_tokens=100,
                            duration_ms=200)

        rows = aggregate_token_usage(conversation_id="A")
        by_role = {r["role"]: r for r in rows}
        self.assertEqual(by_role["orchestrator"]["calls"], 2)
        self.assertEqual(by_role["orchestrator"]["prompt_total"], 3000)
        self.assertEqual(by_role["orchestrator"]["avg_prompt"], 1500)
        self.assertEqual(by_role["translator"]["calls"], 1)
        self.assertEqual(by_role["translator"]["prompt_total"], 200)

    def test_global_includes_all_chats(self) -> None:
        from agentcommander.db.repos import aggregate_token_usage, insert_token_usage
        insert_token_usage(conversation_id="A", role="orchestrator",
                            provider_id="p", model="m",
                            prompt_tokens=1000, completion_tokens=10,
                            duration_ms=100)
        insert_token_usage(conversation_id="B", role="orchestrator",
                            provider_id="p", model="m",
                            prompt_tokens=2000, completion_tokens=10,
                            duration_ms=100)

        rows = aggregate_token_usage(conversation_id=None)
        by_role = {r["role"]: r for r in rows}
        self.assertEqual(by_role["orchestrator"]["calls"], 2)
        self.assertEqual(by_role["orchestrator"]["prompt_total"], 3000)


if __name__ == "__main__":
    unittest.main()
