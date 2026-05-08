"""Tests for the round-50 preventive guard batch.

Adds typography normalisation (smart quotes, em-dash, HTML entities),
URL hygiene (trailing punct, embedded whitespace, encoded traversal),
chatbot-bleed detection (here-is-only, signoff, emoji, model name leak,
turn markers, repeated paragraphs, question-only, all-caps), interactive-
shell artefact stripping (REPL/bash/PS prompts, shebang), and a few
hard execute-tier blocks (sudo, insecure TLS, Windows backslash).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _mk(**fields):
    from agentcommander.types import OrchestratorDecision
    return OrchestratorDecision(action=fields.pop("action", "fetch"), **fields)


def _last_nudge(scratchpad) -> str:
    nudges = [e for e in scratchpad if e.action == "system_nudge"]
    return nudges[-1].input if nudges else ""


# ─── Decision-tier — typography ────────────────────────────────────────────


class TestSmartQuoteGuard(unittest.TestCase):

    def test_replaces_curly_quotes_in_input(self) -> None:
        from agentcommander.engine.guards.preventive_guards import smart_quote_guard
        d = _mk(action="execute", input="echo “hello world”")
        smart_quote_guard(d, [], 1)
        self.assertEqual(d.input, 'echo "hello world"')

    def test_replaces_in_http_body(self) -> None:
        from agentcommander.engine.guards.preventive_guards import smart_quote_guard
        d = _mk(action="http_request", body='{"name": ‘alice’}')
        smart_quote_guard(d, [], 1)
        self.assertEqual(d.body, '{"name": \'alice\'}')

    def test_skips_unrelated_action(self) -> None:
        from agentcommander.engine.guards.preventive_guards import smart_quote_guard
        d = _mk(action="fetch", url="https://example.com", input="“hi”")
        smart_quote_guard(d, [], 1)
        # fetch is not in the target set; input is left as-is.
        self.assertEqual(d.input, "“hi”")


class TestEmDashInCodeGuard(unittest.TestCase):

    def test_replaces_em_dash_with_double_hyphen(self) -> None:
        from agentcommander.engine.guards.preventive_guards import em_dash_in_code_guard
        d = _mk(action="execute", input="git commit —m 'msg'")
        em_dash_in_code_guard(d, [], 1)
        self.assertEqual(d.input, "git commit --m 'msg'")

    def test_replaces_en_dash(self) -> None:
        from agentcommander.engine.guards.preventive_guards import em_dash_in_code_guard
        d = _mk(action="execute", input="ls –la")
        em_dash_in_code_guard(d, [], 1)
        self.assertEqual(d.input, "ls --la")


class TestHtmlEntityDecodeGuard(unittest.TestCase):

    def test_decodes_amp_in_url(self) -> None:
        from agentcommander.engine.guards.preventive_guards import html_entity_decode_guard
        d = _mk(action="fetch", url="https://x.com?a=1&amp;b=2")
        html_entity_decode_guard(d, [], 1)
        self.assertEqual(d.url, "https://x.com?a=1&b=2")

    def test_decodes_lt_gt_in_input(self) -> None:
        from agentcommander.engine.guards.preventive_guards import html_entity_decode_guard
        d = _mk(action="execute", input="echo &lt;tag&gt;")
        html_entity_decode_guard(d, [], 1)
        self.assertEqual(d.input, "echo <tag>")


class TestUrlTrailingPunctGuard(unittest.TestCase):

    def test_strips_trailing_period(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_trailing_punct_guard
        d = _mk(action="fetch", url="https://example.com/page.")
        url_trailing_punct_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com/page")

    def test_strips_trailing_comma(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_trailing_punct_guard
        d = _mk(action="fetch", url="https://example.com,")
        url_trailing_punct_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com")

    def test_strips_trailing_paren(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_trailing_punct_guard
        d = _mk(action="fetch", url="https://example.com)")
        url_trailing_punct_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com")

    def test_keeps_internal_punctuation(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_trailing_punct_guard
        d = _mk(action="fetch", url="https://example.com/path?q=1")
        url_trailing_punct_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com/path?q=1")


class TestUrlEmbeddedWhitespaceGuard(unittest.TestCase):

    def test_strips_internal_whitespace_to_pass(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_embedded_whitespace_guard
        d = _mk(action="fetch", url="https://example\n.com/path")
        v = url_embedded_whitespace_guard(d, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertEqual(d.url, "https://example.com/path")

    def test_blocks_when_strip_breaks_url(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_embedded_whitespace_guard
        scratchpad = []
        d = _mk(action="fetch", url="not a url at all")
        v = url_embedded_whitespace_guard(d, scratchpad, 1)
        self.assertEqual(v.action, "continue")


class TestUrlEncodedTraversalGuard(unittest.TestCase):

    def test_blocks_url_encoded_traversal(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_encoded_traversal_guard
        scratchpad = []
        d = _mk(action="read_file", path="files/%2e%2e/etc/passwd")
        v = url_encoded_traversal_guard(d, scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_blocks_uppercase_encoded(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_encoded_traversal_guard
        scratchpad = []
        d = _mk(action="read_file", path="x/%2E%2E%2Fetc/passwd")
        v = url_encoded_traversal_guard(d, scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_passes_clean_path(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_encoded_traversal_guard
        d = _mk(action="read_file", path="files/data.json")
        v = url_encoded_traversal_guard(d, [], 1)
        self.assertEqual(v.action, "pass")


# ─── Done-tier — chatbot bleed ─────────────────────────────────────────────


class TestHereIsOnlyGuard(unittest.TestCase):

    def test_blocks_pure_here_is_filler(self) -> None:
        from agentcommander.engine.guards.preventive_guards import here_is_only_guard
        scratchpad = []
        d = _mk(action="done", input="Here is the information you requested.")
        v = here_is_only_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_with_actual_content(self) -> None:
        from agentcommander.engine.guards.preventive_guards import here_is_only_guard
        d = _mk(action="done",
                 input="Here is the answer: 42 cycles per second.")
        v = here_is_only_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_passes_with_code_block(self) -> None:
        from agentcommander.engine.guards.preventive_guards import here_is_only_guard
        d = _mk(action="done", input="Here is the script:\n```\nls\n```")
        v = here_is_only_guard([], 1, d)
        self.assertEqual(v.action, "pass")


class TestChatbotSignoffGuard(unittest.TestCase):

    def test_strips_hope_this_helps(self) -> None:
        from agentcommander.engine.guards.preventive_guards import chatbot_signoff_guard
        text = ("The current time in Edmonton is 14:32. Hope this helps! "
                "Let me know if you have more questions.")
        d = _mk(action="done", input=text)
        chatbot_signoff_guard([], 1, d)
        # Signoff trimmed; main content preserved.
        self.assertIn("14:32", d.input)
        self.assertNotIn("Hope this helps", d.input)

    def test_preserves_short_answer(self) -> None:
        """Pattern is too close to the start to safely chop."""
        from agentcommander.engine.guards.preventive_guards import chatbot_signoff_guard
        d = _mk(action="done", input="Hope this helps! The answer is 42.")
        original = d.input
        chatbot_signoff_guard([], 1, d)
        self.assertEqual(d.input, original)


class TestExcessiveEmojiGuard(unittest.TestCase):

    def test_blocks_five_or_more_emoji(self) -> None:
        from agentcommander.engine.guards.preventive_guards import excessive_emoji_guard
        scratchpad = []
        d = _mk(action="done", input="Done! 🎉🚀✨🎊🌟 All set.")
        v = excessive_emoji_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_under_threshold(self) -> None:
        from agentcommander.engine.guards.preventive_guards import excessive_emoji_guard
        d = _mk(action="done", input="Build succeeded! 🎉")
        v = excessive_emoji_guard([], 1, d)
        self.assertEqual(v.action, "pass")


class TestModelNameLeakGuard(unittest.TestCase):

    def test_blocks_claude_mention(self) -> None:
        from agentcommander.engine.guards.preventive_guards import model_name_leak_guard
        scratchpad = []
        d = _mk(action="done", input="As Claude, I think the answer is 42.")
        v = model_name_leak_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_blocks_gpt4_mention(self) -> None:
        from agentcommander.engine.guards.preventive_guards import model_name_leak_guard
        d = _mk(action="done", input="GPT-4 would handle this differently.")
        v = model_name_leak_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_allows_when_user_asked_about_model(self) -> None:
        import time
        from agentcommander.engine.guards.preventive_guards import model_name_leak_guard
        from agentcommander.types import ScratchpadEntry
        scratchpad = [ScratchpadEntry(
            step=0, role="user", action="message",
            input="What model are you, Claude or GPT?",
            output="", timestamp=time.time(),
        )]
        d = _mk(action="done", input="I'm running on Claude.")
        v = model_name_leak_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "pass")


class TestTurnMarkerLeakGuard(unittest.TestCase):

    def test_blocks_user_assistant_markers(self) -> None:
        from agentcommander.engine.guards.preventive_guards import turn_marker_leak_guard
        scratchpad = []
        d = _mk(action="done",
                 input="User: what time is it?\nAssistant: It's 3pm.")
        v = turn_marker_leak_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_blocks_human_prefix_at_start(self) -> None:
        from agentcommander.engine.guards.preventive_guards import turn_marker_leak_guard
        d = _mk(action="done", input="Human: hello there friends.")
        v = turn_marker_leak_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_normal_prose(self) -> None:
        from agentcommander.engine.guards.preventive_guards import turn_marker_leak_guard
        d = _mk(action="done",
                 input="The user asked about the weather. The result is 25°C.")
        v = turn_marker_leak_guard([], 1, d)
        self.assertEqual(v.action, "pass")


class TestRepeatedParagraphGuard(unittest.TestCase):

    def test_blocks_duplicated_paragraph(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repeated_paragraph_guard
        scratchpad = []
        para = "This is a long paragraph that explains the result clearly."
        d = _mk(action="done", input=f"{para}\n\n{para}\n\n{para}")
        v = repeated_paragraph_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_distinct_paragraphs(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repeated_paragraph_guard
        d = _mk(action="done",
                 input="Paragraph one with content.\n\n"
                       "Paragraph two has different content here.\n\n"
                       "Paragraph three closes out the answer nicely.")
        v = repeated_paragraph_guard([], 1, d)
        self.assertEqual(v.action, "pass")


class TestQuestionOnlyDoneGuard(unittest.TestCase):

    def test_blocks_short_question_only(self) -> None:
        from agentcommander.engine.guards.preventive_guards import question_only_done_guard
        scratchpad = []
        d = _mk(action="done", input="Could you clarify what you mean?")
        v = question_only_done_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_question_with_statement(self) -> None:
        from agentcommander.engine.guards.preventive_guards import question_only_done_guard
        d = _mk(action="done",
                 input="The answer is 42. Did you want me to explain why?")
        v = question_only_done_guard([], 1, d)
        self.assertEqual(v.action, "pass")


class TestAllCapsShoutGuard(unittest.TestCase):

    def test_blocks_all_caps_long_text(self) -> None:
        from agentcommander.engine.guards.preventive_guards import all_caps_shout_guard
        scratchpad = []
        d = _mk(action="done",
                 input="ATTENTION! THIS IS AN IMPORTANT NOTICE FOR ALL USERS NOW!")
        v = all_caps_shout_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "continue")

    def test_passes_normal_case(self) -> None:
        from agentcommander.engine.guards.preventive_guards import all_caps_shout_guard
        d = _mk(action="done",
                 input="The HTTP status was 200 OK. Everything works as expected.")
        v = all_caps_shout_guard([], 1, d)
        self.assertEqual(v.action, "pass")


# ─── Execute-tier — interactive prompts and platform mistakes ──────────────


class TestReplPromptInCodeGuard(unittest.TestCase):

    def test_strips_python_repl_prompts(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repl_prompt_in_code_guard
        code = ">>> def foo():\n...     return 1\n>>> foo()"
        new_code, v = repl_prompt_in_code_guard(code, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertNotIn(">>>", new_code)

    def test_passes_normal_code(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repl_prompt_in_code_guard
        code = "def foo():\n    return 1\nprint(foo())"
        new_code, v = repl_prompt_in_code_guard(code, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertEqual(new_code, code)


class TestBashDollarPromptGuard(unittest.TestCase):

    def test_strips_dominant_dollar_prompts(self) -> None:
        from agentcommander.engine.guards.preventive_guards import bash_dollar_prompt_guard
        code = "$ ls -la\n$ pwd\n$ whoami"
        new_code, v = bash_dollar_prompt_guard(code, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertEqual(new_code, "ls -la\npwd\nwhoami")

    def test_keeps_dollar_var_references(self) -> None:
        from agentcommander.engine.guards.preventive_guards import bash_dollar_prompt_guard
        code = "echo $PATH\nfor i in $(seq 1 10); do echo $i; done"
        new_code, v = bash_dollar_prompt_guard(code, [], 1)
        # Not enough leading "$ " patterns → stay as-is.
        self.assertEqual(new_code, code)


class TestPowerShellPromptGuard(unittest.TestCase):

    def test_strips_ps_prompt(self) -> None:
        from agentcommander.engine.guards.preventive_guards import (
            powershell_prompt_in_code_guard,
        )
        code = "PS C:\\Users\\me> Get-Process\nPS C:\\Users\\me> dir"
        new_code, v = powershell_prompt_in_code_guard(code, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertNotIn("PS C:", new_code)


class TestSudoInExecuteGuard(unittest.TestCase):

    def test_blocks_sudo(self) -> None:
        from agentcommander.engine.guards.preventive_guards import sudo_in_execute_guard
        scratchpad = []
        _, v = sudo_in_execute_guard("sudo apt install vim", scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_passes_when_no_sudo(self) -> None:
        from agentcommander.engine.guards.preventive_guards import sudo_in_execute_guard
        _, v = sudo_in_execute_guard("apt-cache show vim", [], 1)
        self.assertEqual(v.action, "pass")


class TestInsecureTlsFlagGuard(unittest.TestCase):

    def test_blocks_curl_k(self) -> None:
        from agentcommander.engine.guards.preventive_guards import insecure_tls_flag_guard
        scratchpad = []
        _, v = insecure_tls_flag_guard("curl -k https://internal.example.com",
                                        scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_blocks_wget_no_check(self) -> None:
        from agentcommander.engine.guards.preventive_guards import insecure_tls_flag_guard
        _, v = insecure_tls_flag_guard(
            "wget --no-check-certificate https://x.com", [], 1,
        )
        self.assertEqual(v.action, "continue")

    def test_passes_normal_curl(self) -> None:
        from agentcommander.engine.guards.preventive_guards import insecure_tls_flag_guard
        _, v = insecure_tls_flag_guard("curl https://example.com -o file", [], 1)
        self.assertEqual(v.action, "pass")


class TestWindowsBackslashInPython(unittest.TestCase):

    def test_blocks_unescaped_path(self) -> None:
        from agentcommander.engine.guards.preventive_guards import (
            windows_backslash_in_python_guard,
        )
        scratchpad = []
        code = 'open("C:\\Users\\foo\\file.txt")'
        _, v = windows_backslash_in_python_guard(code, scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_passes_raw_string(self) -> None:
        from agentcommander.engine.guards.preventive_guards import (
            windows_backslash_in_python_guard,
        )
        code = r'open(r"C:\Users\foo\file.txt")'
        _, v = windows_backslash_in_python_guard(code, [], 1)
        self.assertEqual(v.action, "pass")


class TestShebangMismatchGuard(unittest.TestCase):

    def test_strips_shebang(self) -> None:
        from agentcommander.engine.guards.preventive_guards import shebang_mismatch_guard
        code = "#!/bin/bash\necho hello"
        new_code, v = shebang_mismatch_guard(code, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertEqual(new_code, "echo hello")

    def test_passes_no_shebang(self) -> None:
        from agentcommander.engine.guards.preventive_guards import shebang_mismatch_guard
        code = "echo hello"
        new_code, v = shebang_mismatch_guard(code, [], 1)
        self.assertEqual(new_code, code)


# ─── Output-tier — extra secret patterns ───────────────────────────────────


class TestRedactExtraSecrets(unittest.TestCase):

    def test_redacts_github_fine_grained_pat(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = "token: github_pat_" + "A" * 82 + " end"
        out = redact_extra_secrets(text)
        self.assertIn("github_pat_[REDACTED]", out)
        self.assertNotIn("A" * 82, out)

    def test_redacts_stripe_live(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = "sk_live_" + "B" * 30 + " was used"
        out = redact_extra_secrets(text)
        self.assertIn("sk_live_[REDACTED]", out)

    def test_redacts_anthropic_key(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = "key=sk-ant-api03-aBcDeFgHiJkLmNoPqRsT-_xyz"
        out = redact_extra_secrets(text)
        self.assertIn("sk-ant-[REDACTED]", out)

    def test_redacts_jwt(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoiLCJpYXQiOjE2MDAwMDAwfQ"
                ".aBcDeFgHiJkLmNoPqRsTu")
        out = redact_extra_secrets(text)
        self.assertIn("[JWT-REDACTED]", out)
        self.assertNotIn("eyJzdWI", out)

    def test_redacts_40_hex_token(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = "secret=abcdef0123456789abcdef0123456789abcdef01 end"
        out = redact_extra_secrets(text)
        self.assertIn("[40-HEX-REDACTED]", out)


if __name__ == "__main__":
    unittest.main()
