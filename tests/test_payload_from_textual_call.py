"""Tests for ``PipelineRun._payload_from_textual_call``.

Auto-recovery path that maps chat-fallback emissions like
``check_process <uuid>`` or ``env PATH`` to real tool payloads. Round-45
caught two field-name bugs that would have made auto-recovered tool
calls fail with schema-validation errors:

  * ``check_process`` was building ``{"name": ...}`` but the tool schema
    requires ``id``.
  * ``env <name>`` was building ``{"name": ...}`` with no ``verb`` —
    env tool defaults verb to ``list`` and ignores ``name``, so a request
    like "env PATH" returned all env names instead of PATH's value.

Both routes go through the safe-auto-exec set, so the bugs were live in
production.
"""
from __future__ import annotations

import unittest

from agentcommander.engine.engine import PipelineRun


def _build(verb: str, arg: str = "") -> dict | None:
    return PipelineRun._payload_from_textual_call(PipelineRun, verb, arg)  # type: ignore[arg-type]


class TestCheckProcess(unittest.TestCase):
    """Schema requires `id`, not `name`. Round-45 fix."""

    def test_check_process_uses_id_field(self) -> None:
        payload = _build("check_process", "abc-123")
        self.assertEqual(payload, {"id": "abc-123"})

    def test_check_process_no_arg_returns_none(self) -> None:
        # Without an id, can't run check_process — fall through to apology.
        self.assertIsNone(_build("check_process", ""))

    def test_check_process_strips_quotes(self) -> None:
        # _clean_textual_arg strips surrounding quotes/backticks/punct.
        payload = _build("check_process", '"abc-123"')
        self.assertEqual(payload, {"id": "abc-123"})


class TestEnvVerbInference(unittest.TestCase):
    """`env` with no arg lists all; with an arg, READS that var."""

    def test_env_with_arg_reads_that_var(self) -> None:
        payload = _build("env", "PATH")
        self.assertEqual(payload, {"verb": "read", "name": "PATH"})

    def test_env_no_arg_lists_all(self) -> None:
        payload = _build("env", "")
        self.assertEqual(payload, {"verb": "list"})

    def test_env_with_quoted_arg(self) -> None:
        payload = _build("env", "'HOME'")
        self.assertEqual(payload, {"verb": "read", "name": "HOME"})


class TestOtherSafeVerbsUnchanged(unittest.TestCase):
    """Sanity-check the round-45 changes didn't break the other safe verbs."""

    def test_fetch_uses_url(self) -> None:
        payload = _build("fetch", "https://example.com")
        self.assertEqual(payload, {"url": "https://example.com"})

    def test_http_request_uses_url_method(self) -> None:
        payload = _build("http_request", "https://api.example.com/x")
        self.assertEqual(payload, {"url": "https://api.example.com/x", "method": "GET"})

    def test_read_file_uses_path(self) -> None:
        payload = _build("read_file", "./foo.py")
        self.assertEqual(payload, {"path": "./foo.py"})

    def test_list_dir_no_arg_defaults_to_dot(self) -> None:
        payload = _build("list_dir", "")
        self.assertEqual(payload, {"path": "."})

    def test_browser_uses_url(self) -> None:
        payload = _build("browser", "https://example.com")
        self.assertEqual(payload, {"url": "https://example.com"})


class TestUnsafeVerbsReturnNone(unittest.TestCase):
    """Verbs not in _AUTO_EXEC_SAFE_VERBS shouldn't be matched here at
    all — but the helper function itself returns None for unknown verbs
    anyway. Sanity check."""

    def test_write_file_returns_none(self) -> None:
        self.assertIsNone(_build("write_file", "x.py"))

    def test_execute_returns_none(self) -> None:
        self.assertIsNone(_build("execute", "ls"))

    def test_unknown_verb_returns_none(self) -> None:
        self.assertIsNone(_build("send_email", "foo@bar.com"))


class TestFieldsAgainstActualSchemas(unittest.TestCase):
    """Cross-check: every payload key returned by _payload_from_textual_call
    must exist in the corresponding tool's input_schema. Catches drift
    between this helper and the tool descriptors."""

    def test_check_process_payload_keys_in_schema(self) -> None:
        from agentcommander.tools.dispatcher import bootstrap_builtins, get
        bootstrap_builtins()
        descriptor = get("check_process")
        self.assertIsNotNone(descriptor, "check_process not registered")
        schema_props = set(descriptor.input_schema.get("properties", {}).keys())
        payload = _build("check_process", "x")
        self.assertIsNotNone(payload)
        for key in payload:
            self.assertIn(key, schema_props,
                          f"payload key {key!r} not in check_process schema")

    def test_env_payload_keys_in_schema(self) -> None:
        from agentcommander.tools.dispatcher import bootstrap_builtins, get
        bootstrap_builtins()
        descriptor = get("env")
        self.assertIsNotNone(descriptor, "env not registered")
        schema_props = set(descriptor.input_schema.get("properties", {}).keys())
        for arg in ("PATH", ""):
            payload = _build("env", arg)
            self.assertIsNotNone(payload)
            for key in payload:
                self.assertIn(key, schema_props,
                              f"payload key {key!r} not in env schema (arg={arg!r})")


if __name__ == "__main__":
    unittest.main()
