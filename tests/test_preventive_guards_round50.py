"""Round-50 preventive guard batch — one positive per guard, plus negative
cases ONLY where false-positive risk was a design concern.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _mk(**fields):
    from agentcommander.types import OrchestratorDecision
    return OrchestratorDecision(action=fields.pop("action", "fetch"), **fields)


# ─── Decision-tier — typography / URL hygiene ──────────────────────────────


class TestDecisionTier(unittest.TestCase):
    def test_smart_quote_replaces_curly(self) -> None:
        from agentcommander.engine.guards.preventive_guards import smart_quote_guard
        d = _mk(action="execute", input="echo “hello”")
        smart_quote_guard(d, [], 1)
        self.assertEqual(d.input, 'echo "hello"')

    def test_em_dash_replaced_in_code(self) -> None:
        from agentcommander.engine.guards.preventive_guards import em_dash_in_code_guard
        d = _mk(action="execute", input="git commit —m 'msg'")
        em_dash_in_code_guard(d, [], 1)
        self.assertEqual(d.input, "git commit --m 'msg'")

    def test_html_entity_decoded_in_url(self) -> None:
        from agentcommander.engine.guards.preventive_guards import html_entity_decode_guard
        d = _mk(action="fetch", url="https://x.com?a=1&amp;b=2")
        html_entity_decode_guard(d, [], 1)
        self.assertEqual(d.url, "https://x.com?a=1&b=2")

    def test_url_trailing_punct_stripped(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_trailing_punct_guard
        d = _mk(action="fetch", url="https://example.com/page.")
        url_trailing_punct_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com/page")

    def test_url_embedded_whitespace_strips_to_pass(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_embedded_whitespace_guard
        d = _mk(action="fetch", url="https://example\n.com/path")
        v = url_embedded_whitespace_guard(d, [], 1)
        self.assertEqual(v.action, "pass")
        self.assertEqual(d.url, "https://example.com/path")

    def test_url_encoded_traversal_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_encoded_traversal_guard
        d = _mk(action="read_file", path="files/%2e%2e/etc/passwd")
        v = url_encoded_traversal_guard(d, [], 1)
        self.assertEqual(v.action, "continue")


# ─── Done-tier — chatbot bleed / presentation ──────────────────────────────


class TestDoneTier(unittest.TestCase):
    def test_here_is_only_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import here_is_only_guard
        d = _mk(action="done", input="Here is the information you requested.")
        v = here_is_only_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_here_is_passes_with_content(self) -> None:
        # FP defense: real content with "Here is" prefix is fine.
        from agentcommander.engine.guards.preventive_guards import here_is_only_guard
        d = _mk(action="done", input="Here is the answer: 42 cycles per second.")
        v = here_is_only_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_chatbot_signoff_trims(self) -> None:
        from agentcommander.engine.guards.preventive_guards import chatbot_signoff_guard
        text = ("The current time in Edmonton is 14:32. Hope this helps! "
                "Let me know if you have more questions.")
        d = _mk(action="done", input=text)
        chatbot_signoff_guard([], 1, d)
        self.assertIn("14:32", d.input)
        self.assertNotIn("Hope this helps", d.input)

    def test_excessive_emoji_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import excessive_emoji_guard
        d = _mk(action="done", input="Done! 🎉🚀✨🎊🌟 All set.")
        v = excessive_emoji_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_model_name_leak_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import model_name_leak_guard
        d = _mk(action="done", input="As Claude, I think the answer is 42.")
        v = model_name_leak_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_model_name_passes_when_user_asked(self) -> None:
        # Critical FP defense: user explicitly asked → don't block.
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

    def test_turn_marker_leak_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import turn_marker_leak_guard
        d = _mk(action="done", input="User: what time is it?\nAssistant: It's 3pm.")
        v = turn_marker_leak_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_repeated_paragraph_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repeated_paragraph_guard
        para = "This is a long paragraph that explains the result clearly."
        d = _mk(action="done", input=f"{para}\n\n{para}\n\n{para}")
        v = repeated_paragraph_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_question_only_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import question_only_done_guard
        d = _mk(action="done", input="Could you clarify what you mean?")
        v = question_only_done_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_question_with_statement_passes(self) -> None:
        # FP defense: question after substantive answer is fine.
        from agentcommander.engine.guards.preventive_guards import question_only_done_guard
        d = _mk(action="done", input="The answer is 42. Want me to explain why?")
        v = question_only_done_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_all_caps_shout_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import all_caps_shout_guard
        d = _mk(action="done",
                 input="ATTENTION! THIS IS AN IMPORTANT NOTICE FOR ALL USERS NOW!")
        v = all_caps_shout_guard([], 1, d)
        self.assertEqual(v.action, "continue")


# ─── Execute-tier — interactive prompts / platform mistakes ────────────────


class TestExecuteTier(unittest.TestCase):
    def test_repl_prompt_strips(self) -> None:
        from agentcommander.engine.guards.preventive_guards import repl_prompt_in_code_guard
        code = ">>> def foo():\n...     return 1\n>>> foo()"
        new_code, _ = repl_prompt_in_code_guard(code, [], 1)
        self.assertNotIn(">>>", new_code)

    def test_repl_prompt_passes_normal_code(self) -> None:
        # FP defense: code without prompts stays exactly as-is.
        from agentcommander.engine.guards.preventive_guards import repl_prompt_in_code_guard
        code = "def foo():\n    return 1\nprint(foo())"
        new_code, _ = repl_prompt_in_code_guard(code, [], 1)
        self.assertEqual(new_code, code)

    def test_bash_dollar_prompt_strips_dominant(self) -> None:
        from agentcommander.engine.guards.preventive_guards import bash_dollar_prompt_guard
        code = "$ ls -la\n$ pwd\n$ whoami"
        new_code, _ = bash_dollar_prompt_guard(code, [], 1)
        self.assertEqual(new_code, "ls -la\npwd\nwhoami")

    def test_bash_dollar_keeps_var_refs(self) -> None:
        # Critical FP defense: $VAR / $(...) must NOT be stripped.
        from agentcommander.engine.guards.preventive_guards import bash_dollar_prompt_guard
        code = "echo $PATH\nfor i in $(seq 1 10); do echo $i; done"
        new_code, _ = bash_dollar_prompt_guard(code, [], 1)
        self.assertEqual(new_code, code)

    def test_powershell_prompt_strips(self) -> None:
        from agentcommander.engine.guards.preventive_guards import (
            powershell_prompt_in_code_guard,
        )
        code = "PS C:\\Users\\me> Get-Process\nPS C:\\Users\\me> dir"
        new_code, _ = powershell_prompt_in_code_guard(code, [], 1)
        self.assertNotIn("PS C:", new_code)

    def test_sudo_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import sudo_in_execute_guard
        _, v = sudo_in_execute_guard("sudo apt install vim", [], 1)
        self.assertEqual(v.action, "continue")

    def test_insecure_tls_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import insecure_tls_flag_guard
        _, v = insecure_tls_flag_guard("curl -k https://internal.example.com", [], 1)
        self.assertEqual(v.action, "continue")

    def test_windows_backslash_blocks_unescaped(self) -> None:
        from agentcommander.engine.guards.preventive_guards import (
            windows_backslash_in_python_guard,
        )
        _, v = windows_backslash_in_python_guard(
            'open("C:\\Users\\foo\\file.txt")', [], 1,
        )
        self.assertEqual(v.action, "continue")

    def test_windows_backslash_passes_raw_string(self) -> None:
        # FP defense: r"C:\..." is the correct fix; must not block.
        from agentcommander.engine.guards.preventive_guards import (
            windows_backslash_in_python_guard,
        )
        _, v = windows_backslash_in_python_guard(
            r'open(r"C:\Users\foo\file.txt")', [], 1,
        )
        self.assertEqual(v.action, "pass")

    def test_shebang_strips(self) -> None:
        from agentcommander.engine.guards.preventive_guards import shebang_mismatch_guard
        new_code, _ = shebang_mismatch_guard("#!/bin/bash\necho hello", [], 1)
        self.assertEqual(new_code, "echo hello")


# ─── Output-tier — extra secret patterns ───────────────────────────────────


class TestRedactExtraSecrets(unittest.TestCase):
    def test_redacts_multiple_secret_patterns(self) -> None:
        from agentcommander.engine.guards.preventive_guards import redact_extra_secrets
        text = (
            "GH PAT: github_pat_" + "A" * 82 + "\n"
            "Stripe: sk_live_" + "B" * 30 + "\n"
            "Anthropic: sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ-_xyz123\n"
            "JWT: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoiLCJpYXQiOjE2fQ.aBcDeFgHiJkLmN\n"
            "Hex: abcdef0123456789abcdef0123456789abcdef01\n"
        )
        out = redact_extra_secrets(text)
        self.assertIn("github_pat_[REDACTED]", out)
        self.assertIn("sk_live_[REDACTED]", out)
        self.assertIn("sk-ant-[REDACTED]", out)
        self.assertIn("[JWT-REDACTED]", out)
        self.assertIn("[40-HEX-REDACTED]", out)
        # And none of the original secret bytes survive.
        self.assertNotIn("A" * 82, out)
        self.assertNotIn("B" * 30, out)


if __name__ == "__main__":
    unittest.main()
