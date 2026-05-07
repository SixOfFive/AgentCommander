"""Tests for ``_parse_token_count`` — round-46 added an upper bound.

Both ``/context <N>`` and ``/autoconfig minctx <N>`` go through this
parser. Without the cap, ``/context 999999999`` was accepted as 953.7M
tokens and would have tried to send a 953-million-token num_ctx to
providers, blowing past every model's training window and likely
crashing the daemon.
"""
from __future__ import annotations

import unittest

from agentcommander.tui.commands import _parse_token_count, _MAX_TOKEN_COUNT


class TestNormalValues(unittest.TestCase):
    def test_raw_integer(self) -> None:
        self.assertEqual(_parse_token_count("4096"), 4096)

    def test_k_suffix_lower(self) -> None:
        self.assertEqual(_parse_token_count("32k"), 32 * 1024)

    def test_k_suffix_upper(self) -> None:
        self.assertEqual(_parse_token_count("128K"), 128 * 1024)

    def test_m_suffix(self) -> None:
        self.assertEqual(_parse_token_count("1m"), 1024 * 1024)

    def test_decimal_with_suffix(self) -> None:
        self.assertEqual(_parse_token_count("1.5m"), int(1.5 * 1024 * 1024))

    def test_at_cap_accepted(self) -> None:
        # Boundary: 16M exactly should pass.
        self.assertEqual(_parse_token_count(str(_MAX_TOKEN_COUNT)), _MAX_TOKEN_COUNT)

    def test_just_under_cap(self) -> None:
        self.assertEqual(_parse_token_count(str(_MAX_TOKEN_COUNT - 1)),
                         _MAX_TOKEN_COUNT - 1)


class TestRejectedValues(unittest.TestCase):
    def test_zero_rejected(self) -> None:
        self.assertIsNone(_parse_token_count("0"))

    def test_negative_rejected(self) -> None:
        self.assertIsNone(_parse_token_count("-100"))

    def test_one_over_cap_rejected(self) -> None:
        self.assertIsNone(_parse_token_count(str(_MAX_TOKEN_COUNT + 1)))

    def test_round46_value_rejected(self) -> None:
        # The exact value that triggered the discovery: 999999999 →
        # parses as 999_999_999 which is 953.7M, way over the 16M cap.
        self.assertIsNone(_parse_token_count("999999999"))

    def test_huge_m_rejected(self) -> None:
        # 100m = 100 * 1024 * 1024 = ~105M > 16M cap
        self.assertIsNone(_parse_token_count("100m"))

    def test_garbage_rejected(self) -> None:
        self.assertIsNone(_parse_token_count("not-a-number"))

    def test_empty_rejected(self) -> None:
        self.assertIsNone(_parse_token_count(""))

    def test_whitespace_only_rejected(self) -> None:
        self.assertIsNone(_parse_token_count("   "))

    def test_mixed_garbage(self) -> None:
        self.assertIsNone(_parse_token_count("abc123"))


class TestCapValue(unittest.TestCase):
    """The cap itself should be sensible — higher than any real model
    but not so high that the protection is meaningless."""

    def test_cap_greater_than_largest_known_model(self) -> None:
        # Llama 4 Scout has 10M context; 16M cap leaves headroom.
        self.assertGreaterEqual(_MAX_TOKEN_COUNT, 10 * 1024 * 1024)

    def test_cap_below_absurd_value(self) -> None:
        # 1 billion tokens = ~4 GB of text. Cap is well below.
        self.assertLess(_MAX_TOKEN_COUNT, 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
