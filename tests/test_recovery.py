"""Tests for engine/recovery.py (extracted in the #5 engine.py split).

payload_from_textual_call and infer_live_data_url have dedicated suites
(test_payload_from_textual_call, test_live_data_inference); this covers the
other extracted helpers.
"""
from __future__ import annotations

import unittest

from agentcommander.engine import recovery


class TestScratchpadLeak(unittest.TestCase):
    def test_tool_success_wrapper(self):
        self.assertTrue(recovery.is_scratchpad_leak("successfully completed:\nwrote x.py"))

    def test_role_scaffolding(self):
        self.assertTrue(recovery.is_scratchpad_leak("Summarize what was done. User asked: ..."))

    def test_multi_test_echo(self):
        self.assertTrue(recovery.is_scratchpad_leak("TEST 001: ok TEST 002: ok TEST 003: ok"))

    def test_real_reply_passes(self):
        self.assertFalse(recovery.is_scratchpad_leak("The three planets are Mercury, Venus, Earth."))
        self.assertFalse(recovery.is_scratchpad_leak(""))


class TestDetectToolSyntax(unittest.TestCase):
    def test_verb_with_arg(self):
        self.assertEqual(recovery.detect_tool_syntax_intent("fetch https://example.com"),
                         ("fetch", "https://example.com"))

    def test_synonym_rewrite(self):
        # `ls` → list_dir via TOOL_VERB_SYNONYMS.
        verb, _ = recovery.detect_tool_syntax_intent("ls .")
        self.assertEqual(verb, "list_dir")

    def test_last_line_only(self):
        self.assertEqual(recovery.detect_tool_syntax_intent("blah blah\nread_file foo.py"),
                         ("read_file", "foo.py"))

    def test_prose_is_not_a_call(self):
        self.assertIsNone(recovery.detect_tool_syntax_intent("I will fetch the data for you soon."))

    def test_json_not_treated_as_text_call(self):
        self.assertIsNone(recovery.detect_tool_syntax_intent('fetch {"url": "x"}'))


class TestCleanTextualArg(unittest.TestCase):
    def test_strips_quotes_and_punct(self):
        self.assertEqual(recovery.clean_textual_arg("fetch", '"https://example.com".'),
                         "https://example.com")

    def test_strips_backticks_and_brackets(self):
        self.assertEqual(recovery.clean_textual_arg("read_file", "`<./foo.py>`"), "./foo.py")

    def test_preserves_internal_spaces(self):
        self.assertEqual(recovery.clean_textual_arg("read_file", "my file.py"), "my file.py")


if __name__ == "__main__":
    unittest.main()
