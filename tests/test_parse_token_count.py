"""Tests for ``_parse_token_count`` — round-46 cap protection.

Trim: kept boundary cases (at cap, just over cap, garbage) plus the
round-46 trigger value 999999999 that motivated the cap. Dropped
redundant suffix-variants and decimal-with-suffix.
"""
from __future__ import annotations

import unittest

from agentcommander.tui.commands import _parse_token_count, _MAX_TOKEN_COUNT


class TestParseTokenCount(unittest.TestCase):
    def test_at_cap_accepted(self) -> None:
        self.assertEqual(_parse_token_count(str(_MAX_TOKEN_COUNT)), _MAX_TOKEN_COUNT)

    def test_just_under_cap_with_k_suffix(self) -> None:
        # Exercises both suffix parsing and a value below the cap.
        self.assertEqual(_parse_token_count("128k"), 128 * 1024)

    def test_just_over_cap_rejected(self) -> None:
        self.assertIsNone(_parse_token_count(str(_MAX_TOKEN_COUNT + 1)))

    def test_round46_trigger_value_rejected(self) -> None:
        # The exact value that motivated the cap: 999999999 was being
        # accepted as 953.7M tokens, would have crashed providers.
        self.assertIsNone(_parse_token_count("999999999"))

    def test_garbage_and_zero_rejected(self) -> None:
        self.assertIsNone(_parse_token_count("not-a-number"))
        self.assertIsNone(_parse_token_count("0"))
        self.assertIsNone(_parse_token_count(""))


if __name__ == "__main__":
    unittest.main()
