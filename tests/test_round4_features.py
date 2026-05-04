"""Tests for the round-4 deferred features:

  1. Preflight + postmortem meta-agents (engine/meta_agents.py)
  2. TypeCast hint accumulator (autoconfig._role_score_with_hint + bumps)
  3. New tools: http_request, git, env, browser

Pure unit tests — no live LLM calls, no real network. We mock at the
seam between the meta-agent and ``call_role`` so the verdict-parsing
code paths are exercised against canned model outputs. Tool tests
either hit deterministic local subjects (env vars, ``git`` against the
test's own repo) or stub out the network with a fake urlopen.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ─── Shared fixtures ──────────────────────────────────────────────────────


def _ensure_db() -> None:
    """Open the project DB once. ``init_db`` is idempotent."""
    from agentcommander.db.connection import init_db
    init_db()


@contextmanager
def _stub_urlopen(response_factory):
    """Patch urllib.request.urlopen with a context-manager-yielding fake.

    ``response_factory(url, request)`` returns a ``_FakeResponse``-shaped
    object (or raises). The fake supports ``read``, iteration, ``status``,
    ``headers``, and ``geturl``.
    """
    real = __import__("urllib.request", fromlist=["urlopen"]).urlopen

    def fake(req, *args, **kwargs):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        return response_factory(url, req)

    with mock.patch("urllib.request.urlopen", fake):
        yield
    # Sanity: confirm we restored.
    assert __import__("urllib.request", fromlist=["urlopen"]).urlopen is real


class _FakeResponse:
    """Minimal duck-type for urlopen result. Supports ``with`` + read."""

    def __init__(self, body: bytes, *, status: int = 200,
                 headers: dict | None = None, url: str = "https://example.com/"):
        self._buf = BytesIO(body)
        self.status = status
        self.headers = _FakeHeaders(headers or {"Content-Type": "text/html"})
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n) if n > 0 else self._buf.read()

    def __iter__(self):
        # urllib iterates lines; for tests that iterate (SSE-shaped) we
        # split on newlines. Most of our tests use ``read``.
        return iter(self._buf.readlines())

    def geturl(self) -> str:
        return self._url


class _FakeHeaders:
    """Dict + ``.get`` + ``.items`` wrapper matching urllib's HTTPMessage."""

    def __init__(self, d: dict):
        self._d = d

    def get(self, k: str, default=None):
        for key, val in self._d.items():
            if key.lower() == k.lower():
                return val
        return default

    def items(self):
        return list(self._d.items())


# ─── 1. Preflight + postmortem ────────────────────────────────────────────


class TestPreflight(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def _mk_decision(self, action: str = "execute", input_text: str = "rm /"):
        from agentcommander.types import OrchestratorDecision
        return OrchestratorDecision(action=action, input=input_text,
                                    reasoning="testing")

    def test_role_not_assigned_silent_skip(self) -> None:
        from agentcommander.engine.meta_agents import apply_preflight
        decision = self._mk_decision()
        # No role assignment for PREFLIGHT in the test DB → returns approve.
        verdict = apply_preflight(
            decision, scratchpad=[], conversation_id=None,
        )
        self.assertEqual(verdict.verdict, "approve")
        self.assertIn("not assigned", verdict.reason)

    def test_approve_path(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        with mock.patch.object(meta_agents, "call_role",
                               return_value='{"verdict": "approve", "reason": "looks fine"}'):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "approve")
        self.assertEqual(verdict.reason, "looks fine")

    def test_abort_path_returns_reason(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        with mock.patch.object(meta_agents, "call_role",
                               return_value='{"verdict": "abort", "reason": "destructive command"}'):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "abort")
        self.assertEqual(verdict.reason, "destructive command")

    def test_reorder_returns_decisions(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        canned = json.dumps({
            "verdict": "reorder",
            "reorder_steps": [
                {"action": "list_dir", "input": ".",
                 "reasoning": "verify what's there"},
                {"action": "read_file", "input": "manifest.txt",
                 "reasoning": "check before deletion"},
            ],
            "reason": "verify before delete",
        })
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "reorder")
        self.assertEqual(len(verdict.reorder_steps), 2)
        self.assertEqual(verdict.reorder_steps[0].action, "list_dir")
        self.assertEqual(verdict.reorder_steps[1].action, "read_file")

    def test_reorder_caps_at_max_steps(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        steps = [{"action": f"list_dir", "input": str(i)} for i in range(10)]
        canned = json.dumps({
            "verdict": "reorder", "reorder_steps": steps,
            "reason": "many prereqs",
        })
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "reorder")
        self.assertLessEqual(
            len(verdict.reorder_steps),
            meta_agents.PREFLIGHT_MAX_REORDER_STEPS,
        )

    def test_empty_reorder_coerces_to_approve(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        canned = '{"verdict": "reorder", "reorder_steps": [], "reason": "no reason"}'
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "approve")

    def test_malformed_json_coerces_to_approve(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        with mock.patch.object(meta_agents, "call_role",
                               return_value="not json at all"):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "approve")

    def test_strips_markdown_fences(self) -> None:
        from agentcommander.engine import meta_agents
        decision = self._mk_decision()
        canned = '```json\n{"verdict": "approve", "reason": "ok"}\n```'
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            verdict = meta_agents.apply_preflight(
                decision, scratchpad=[], conversation_id=None,
            )
        self.assertEqual(verdict.verdict, "approve")

    def test_abort_bumps_rule_helped(self) -> None:
        """When preflight aborts and rules were consulted, those rules
        should be marked helped++ so chronically-correct patterns gain
        confidence over time. Closes the rule-feedback loop.
        """
        from agentcommander.db.repos import (
            insert_operational_rule, list_operational_rules_for_action,
            archive_operational_rule,
        )
        from agentcommander.engine import meta_agents

        rid = insert_operational_rule(
            fingerprint_version=1, action_type="execute",
            target_pattern=None, context_tags=["dangerous"],
            constraint_text="rm -rf is risky",
            suggested_reorder=None, origin="manual",
            confidence=0.5, example_run_id=None,
        )
        try:
            before = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]

            decision = self._mk_decision()
            with mock.patch.object(
                meta_agents, "call_role",
                return_value='{"verdict": "abort", "reason": "matches rule"}',
            ):
                verdict = meta_agents.apply_preflight(
                    decision, scratchpad=[], conversation_id=None,
                )
            self.assertEqual(verdict.verdict, "abort")
            self.assertIn(rid, verdict.rules_consulted)

            after = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]
            self.assertEqual(after, before + 1)
        finally:
            archive_operational_rule(rid)

    def test_reorder_bumps_rule_helped(self) -> None:
        from agentcommander.db.repos import (
            insert_operational_rule, list_operational_rules_for_action,
            archive_operational_rule,
        )
        from agentcommander.engine import meta_agents

        rid = insert_operational_rule(
            fingerprint_version=1, action_type="execute",
            target_pattern=None, context_tags=["needs-prep"],
            constraint_text="check before exec",
            suggested_reorder=None, origin="manual",
            confidence=0.5, example_run_id=None,
        )
        try:
            before = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]

            decision = self._mk_decision()
            canned = json.dumps({
                "verdict": "reorder",
                "reorder_steps": [{"action": "list_dir", "input": "."}],
                "reason": "verify first",
            })
            with mock.patch.object(meta_agents, "call_role",
                                   return_value=canned):
                verdict = meta_agents.apply_preflight(
                    decision, scratchpad=[], conversation_id=None,
                )
            self.assertEqual(verdict.verdict, "reorder")
            self.assertIn(rid, verdict.rules_consulted)

            after = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]
            self.assertEqual(after, before + 1)
        finally:
            archive_operational_rule(rid)

    def test_approve_does_not_bump_rules(self) -> None:
        """Approve verdict should NOT bump consulted rules — no decision
        was made on the basis of any rule's pattern. Bumping on approve
        would inflate the helped count for rules that never fired."""
        from agentcommander.db.repos import (
            insert_operational_rule, list_operational_rules_for_action,
            archive_operational_rule,
        )
        from agentcommander.engine import meta_agents

        rid = insert_operational_rule(
            fingerprint_version=1, action_type="execute",
            target_pattern=None, context_tags=[],
            constraint_text="never used",
            suggested_reorder=None, origin="manual",
            confidence=0.5, example_run_id=None,
        )
        try:
            before = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]

            decision = self._mk_decision()
            with mock.patch.object(
                meta_agents, "call_role",
                return_value='{"verdict": "approve", "reason": "fine"}',
            ):
                meta_agents.apply_preflight(
                    decision, scratchpad=[], conversation_id=None,
                )

            after = next(
                r for r in list_operational_rules_for_action("execute")
                if r["id"] == rid
            )["helped_count"]
            self.assertEqual(after, before)
        finally:
            archive_operational_rule(rid)


class TestPostmortem(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def test_persists_rule_on_emit(self) -> None:
        from agentcommander.engine import meta_agents
        canned = json.dumps({
            "rule": {
                "action_type": "execute",
                "target_pattern": None,
                "context_tags": ["dangerous-command", "no-pacing"],
                "constraint_text": "Avoid rm -rf without confirmation.",
                "suggested_reorder": None,
                "confidence": 0.7,
            },
            "retry": None,
            "user_prompt": None,
            "confidence": 0.7,
            "reason": "destructive without preflight",
        })
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            result = meta_agents.apply_postmortem(
                run_id="test-run-1",
                conversation_id=None,
                scratchpad=[],
                final_status="failed",
                error_text="something failed",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNotNone(result.rule_id)
        # Verify the rule was actually persisted.
        from agentcommander.db.repos import list_operational_rules_for_action
        rules = list_operational_rules_for_action("execute")
        self.assertTrue(any(r["id"] == result.rule_id for r in rules))

    def test_role_unassigned_returns_none(self) -> None:
        from agentcommander.engine.meta_agents import apply_postmortem
        result = apply_postmortem(
            run_id="r", conversation_id=None, scratchpad=[],
            final_status="failed", error_text=None,
        )
        # No POSTMORTEM role assigned in test DB → silent skip.
        self.assertIsNone(result)

    def test_malformed_output_returns_none(self) -> None:
        from agentcommander.engine import meta_agents
        with mock.patch.object(meta_agents, "call_role",
                               return_value="garbage output"):
            result = meta_agents.apply_postmortem(
                run_id="r", conversation_id=None, scratchpad=[],
                final_status="failed", error_text=None,
            )
        self.assertIsNone(result)

    def test_no_rule_emit_still_returns_result(self) -> None:
        from agentcommander.engine import meta_agents
        canned = json.dumps({
            "rule": None, "retry": None, "user_prompt": None,
            "confidence": 0.0,
            "reason": "Transient or non-generalizable failure",
        })
        with mock.patch.object(meta_agents, "call_role", return_value=canned):
            result = meta_agents.apply_postmortem(
                run_id="r", conversation_id=None, scratchpad=[],
                final_status="failed", error_text=None,
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result.rule_id)
        self.assertEqual(result.confidence, 0.0)


class TestOperationalRulesRepo(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def test_insert_and_list(self) -> None:
        from agentcommander.db.repos import (
            insert_operational_rule, list_operational_rules_for_action,
            archive_operational_rule, bump_rule_outcome,
        )
        rid = insert_operational_rule(
            fingerprint_version=1, action_type="write_file",
            target_pattern=r"\.env$", context_tags=["secret-risk"],
            constraint_text="Never write .env files",
            suggested_reorder=None, origin="manual",
            confidence=0.9, example_run_id=None,
        )
        rules = list_operational_rules_for_action("write_file")
        self.assertTrue(any(r["id"] == rid for r in rules))
        # Bump helped.
        bump_rule_outcome(rid, helped=True)
        rules = list_operational_rules_for_action("write_file")
        rule = next(r for r in rules if r["id"] == rid)
        self.assertEqual(rule["helped_count"], 1)
        # Archive — should disappear from the active list.
        archive_operational_rule(rid)
        rules = list_operational_rules_for_action("write_file")
        self.assertFalse(any(r["id"] == rid for r in rules))


# ─── 2. Hint accumulator ──────────────────────────────────────────────────


class TestHintAccumulator(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()
        # The DB persists across test runs (project-local SQLite). Without
        # this wipe, bump_hint accumulates: a 2nd run of `unittest discover`
        # leaves model-X with +10 instead of +5, etc. Clear all the
        # well-known test rows so each test starts from a known baseline.
        from agentcommander.db.connection import get_db
        get_db().execute(
            "DELETE FROM model_hints WHERE model_id IN "
            "('model-X', 'model-Y', 'model-Z', 'model-W')"
        )

    def test_role_score_with_hint_adds_bump(self) -> None:
        from agentcommander.db.repos import bump_hint
        from agentcommander.typecast.autoconfig import _role_score_with_hint

        bump_hint("model-X", "router", 5.0)
        entry = {"roleScores": {"router": {"score": 50}}}
        score = _role_score_with_hint(entry, "router", "model-X")
        self.assertAlmostEqual(score, 55.0)

    def test_negative_hint_reduces_score(self) -> None:
        from agentcommander.db.repos import bump_hint
        from agentcommander.typecast.autoconfig import _role_score_with_hint

        bump_hint("model-Y", "coder", -10.0)
        entry = {"roleScores": {"coder": {"score": 80}}}
        score = _role_score_with_hint(entry, "coder", "model-Y")
        self.assertAlmostEqual(score, 70.0)

    def test_clamp_at_plus_minus_100(self) -> None:
        from agentcommander.db.repos import bump_hint, get_hint
        # 200 cumulative — clamp to +100.
        for _ in range(20):
            bump_hint("model-Z", "tester", 15.0)
        self.assertAlmostEqual(get_hint("model-Z", "tester"), 100.0)
        # Negative side.
        for _ in range(20):
            bump_hint("model-W", "tester", -15.0)
        self.assertAlmostEqual(get_hint("model-W", "tester"), -100.0)


# ─── 3a. http_request tool ────────────────────────────────────────────────


class TestHttpTool(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def _ctx(self):
        from agentcommander.tools.types import ToolContext
        return ToolContext(working_directory=tempfile.gettempdir(),
                           conversation_id=None,
                           audit=lambda *_: None)

    def test_blocks_loopback(self) -> None:
        from agentcommander.tools.http_tool import _http_request
        result = _http_request({"url": "http://127.0.0.1/secret"}, self._ctx())
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("BLOCKED", result.error)

    def test_rejects_body_and_json_together(self) -> None:
        from agentcommander.tools.http_tool import _http_request
        result = _http_request(
            {"url": "https://example.com", "body": "x", "json": {"a": 1}},
            self._ctx(),
        )
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("not both", result.error)

    def test_parses_json_response(self) -> None:
        from agentcommander.tools.http_tool import _http_request

        def factory(url, req):
            return _FakeResponse(
                b'{"hello": "world", "n": 42}',
                headers={"Content-Type": "application/json"},
            )

        with _stub_urlopen(factory):
            result = _http_request(
                {"url": "https://api.example.com/x", "method": "GET"},
                self._ctx(),
            )
        self.assertTrue(result.ok)
        assert result.data is not None
        self.assertEqual(result.data["json"], {"hello": "world", "n": 42})


# ─── 3b. git tool ─────────────────────────────────────────────────────────


class TestGitTool(unittest.TestCase):
    """Run real ``git`` against the project's own checkout. AC's repo IS
    the working dir; ``git status`` etc. always have data to return."""

    def setUp(self) -> None:
        _ensure_db()

    def _ctx(self, cwd: str | None = None):
        from agentcommander.tools.types import ToolContext
        return ToolContext(
            working_directory=cwd or str(Path(__file__).resolve().parent.parent),
            conversation_id=None, audit=lambda *_: None,
        )

    def test_status(self) -> None:
        from agentcommander.tools.git_tool import _git
        result = _git({"verb": "status"}, self._ctx())
        # Either ok=True (we're in a git repo) or "not a git repository"
        # error — the repo presence depends on how the test is invoked.
        if result.ok:
            self.assertIsNotNone(result.output)
        else:
            assert result.error is not None
            self.assertTrue(
                "not a git repository" in result.error.lower()
                or "git is not installed" in result.error.lower()
            )

    def test_log_caps_n(self) -> None:
        from agentcommander.tools.git_tool import _git, _build_argv
        # 5000 should clamp to 200 in argv.
        argv = _build_argv("log", {"n": 5000})
        assert argv is not None
        self.assertIn("-200", argv)

    def test_unsupported_verb(self) -> None:
        from agentcommander.tools.git_tool import _git
        result = _git({"verb": "push"}, self._ctx())
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("unsupported", result.error)

    def test_diff_rejects_metachars(self) -> None:
        from agentcommander.tools.git_tool import _build_argv
        # Shell metachars in revision must be rejected.
        self.assertIsNone(_build_argv("diff", {"revision": "HEAD; rm -rf /"}))
        self.assertIsNone(_build_argv("diff", {"revision": "--all"}))
        # Clean revision passes.
        self.assertIsNotNone(_build_argv("diff", {"revision": "HEAD~3"}))

    def test_show_requires_revision(self) -> None:
        from agentcommander.tools.git_tool import _build_argv
        self.assertIsNone(_build_argv("show", {}))
        self.assertIsNone(_build_argv("show", {"revision": ""}))
        self.assertIsNotNone(_build_argv("show", {"revision": "HEAD"}))


# ─── 3c. env tool ─────────────────────────────────────────────────────────


class TestEnvTool(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def _ctx(self):
        from agentcommander.tools.types import ToolContext
        return ToolContext(working_directory=tempfile.gettempdir(),
                           conversation_id=None,
                           audit=lambda *_: None)

    def test_list_returns_names(self) -> None:
        from agentcommander.tools.env_tool import _env
        os.environ["AC_TEST_NORMAL"] = "hello"
        try:
            result = _env({"verb": "list"}, self._ctx())
            self.assertTrue(result.ok)
            self.assertIn("AC_TEST_NORMAL", result.output or "")
            # No values exposed on `list`.
            self.assertNotIn("hello", result.output or "")
        finally:
            del os.environ["AC_TEST_NORMAL"]

    def test_read_redacts_secret_pattern(self) -> None:
        from agentcommander.tools.env_tool import _env
        os.environ["AC_TEST_API_KEY"] = "sk-supersecretvalue1234"
        try:
            result = _env({"verb": "read", "name": "AC_TEST_API_KEY"},
                          self._ctx())
            self.assertTrue(result.ok)
            self.assertNotIn("supersecretvalue", result.output or "")
            assert result.data is not None
            self.assertTrue(result.data.get("redacted"))
        finally:
            del os.environ["AC_TEST_API_KEY"]

    def test_read_plain_var_returns_value(self) -> None:
        from agentcommander.tools.env_tool import _env
        os.environ["AC_TEST_FLAG"] = "yes"
        try:
            result = _env({"verb": "read", "name": "AC_TEST_FLAG"}, self._ctx())
            self.assertTrue(result.ok)
            self.assertEqual(result.output, "yes")
        finally:
            del os.environ["AC_TEST_FLAG"]

    def test_unset_var_returns_error(self) -> None:
        from agentcommander.tools.env_tool import _env
        # Pick a name that's almost certainly unset.
        result = _env(
            {"verb": "read", "name": "AC_DEFINITELY_NOT_SET_42"},
            self._ctx(),
        )
        self.assertFalse(result.ok)

    def test_list_filtered_redacts_secrets(self) -> None:
        from agentcommander.tools.env_tool import _env
        os.environ["AC_TEST_TOKEN"] = "tok-supersecret"
        os.environ["AC_TEST_PUBLIC"] = "public-value"
        try:
            result = _env({"verb": "list_filtered"}, self._ctx())
            self.assertTrue(result.ok)
            self.assertIn("AC_TEST_PUBLIC=public-value", result.output or "")
            self.assertIn("AC_TEST_TOKEN=<redacted>", result.output or "")
            self.assertNotIn("supersecret", result.output or "")
        finally:
            del os.environ["AC_TEST_TOKEN"]
            del os.environ["AC_TEST_PUBLIC"]


# ─── 3d. browser tool ─────────────────────────────────────────────────────


class TestBrowserTool(unittest.TestCase):

    def setUp(self) -> None:
        _ensure_db()

    def _ctx(self):
        from agentcommander.tools.types import ToolContext
        return ToolContext(working_directory=tempfile.gettempdir(),
                           conversation_id=None,
                           audit=lambda *_: None)

    def test_extracts_text_strips_script(self) -> None:
        from agentcommander.tools.browser_tool import _browser
        html = (b"<html><head><title>Test</title></head>"
                b"<body><h1>Hello</h1>"
                b"<script>alert('x');</script>"
                b"<p>This is a paragraph.</p>"
                b"<a href='/next'>read more</a>"
                b"</body></html>")

        def factory(url, req):
            return _FakeResponse(
                html, headers={"Content-Type": "text/html"},
                url="https://example.com/page",
            )

        with _stub_urlopen(factory):
            result = _browser({"url": "https://example.com/page"}, self._ctx())
        self.assertTrue(result.ok)
        self.assertIn("Hello", result.output or "")
        self.assertIn("This is a paragraph", result.output or "")
        # Script content must NOT appear.
        self.assertNotIn("alert", result.output or "")
        # Title surfaces in data.
        assert result.data is not None
        self.assertEqual(result.data["title"], "Test")
        # Link list resolved to absolute.
        self.assertEqual(len(result.data["links"]), 1)
        self.assertEqual(result.data["links"][0]["href"],
                         "https://example.com/next")

    def test_blocks_loopback(self) -> None:
        from agentcommander.tools.browser_tool import _browser
        result = _browser({"url": "http://localhost/admin"}, self._ctx())
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIn("BLOCKED", result.error)


if __name__ == "__main__":
    unittest.main()
