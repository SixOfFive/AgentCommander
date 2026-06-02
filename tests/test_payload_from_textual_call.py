"""Tests for ``recovery.payload_from_textual_call`` (round-45 bugs).

Trim: kept the two round-45 bug regressions (check_process id, env verb),
the schema-drift detector (would have caught both bugs automatically),
and a few sanity verbs to confirm the dispatch table still maps correctly.

(Moved from PipelineRun to engine/recovery.py in the #5 engine.py split.)
"""
from __future__ import annotations

import unittest

from agentcommander.engine import recovery


def _build(verb: str, arg: str = "") -> dict | None:
    return recovery.payload_from_textual_call(verb, arg)


class TestPayloadFromTextualCall(unittest.TestCase):
    def test_check_process_uses_id_field(self) -> None:
        # Round-45 bug #1: was building {"name": ...} but schema needs "id".
        self.assertEqual(_build("check_process", "abc-123"), {"id": "abc-123"})

    def test_env_with_arg_reads_that_var(self) -> None:
        # Round-45 bug #2: was missing "verb"; default "list" ignored "name".
        self.assertEqual(_build("env", "PATH"), {"verb": "read", "name": "PATH"})

    def test_env_no_arg_lists_all(self) -> None:
        self.assertEqual(_build("env", ""), {"verb": "list"})

    def test_safe_verbs_dispatch(self) -> None:
        # Sanity: a few known-good verb mappings to confirm the table.
        self.assertEqual(_build("fetch", "https://example.com"),
                         {"url": "https://example.com"})
        self.assertEqual(_build("read_file", "./foo.py"), {"path": "./foo.py"})
        self.assertEqual(_build("list_dir", ""), {"path": "."})

    def test_unsafe_verb_returns_none(self) -> None:
        # Verbs not in the safe-auto-exec set must not produce a payload.
        self.assertIsNone(_build("execute", "ls"))


class TestFieldsAgainstActualSchemas(unittest.TestCase):
    """Schema-drift detector — cross-checks every payload key against the
    actual tool's input_schema.properties. Would have caught both round-45
    bugs automatically; MUST stay even after trimming."""

    def test_check_process_payload_keys_in_schema(self) -> None:
        from agentcommander.tools.dispatcher import bootstrap_builtins, get_tool as get
        bootstrap_builtins()
        descriptor = get("check_process")
        self.assertIsNotNone(descriptor)
        schema_props = set(descriptor.input_schema.get("properties", {}).keys())
        payload = _build("check_process", "x")
        for key in payload:
            self.assertIn(key, schema_props,
                          f"payload key {key!r} not in check_process schema")

    def test_env_payload_keys_in_schema(self) -> None:
        from agentcommander.tools.dispatcher import bootstrap_builtins, get_tool as get
        bootstrap_builtins()
        descriptor = get("env")
        self.assertIsNotNone(descriptor)
        schema_props = set(descriptor.input_schema.get("properties", {}).keys())
        for arg in ("PATH", ""):
            payload = _build("env", arg)
            for key in payload:
                self.assertIn(key, schema_props,
                              f"payload key {key!r} not in env schema (arg={arg!r})")


if __name__ == "__main__":
    unittest.main()
