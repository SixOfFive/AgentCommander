"""Tests for /autoconfig memory <budget>.

Two surfaces:

1. ``_parse_memory_gb`` — the string parser. Mirrors the conventions used
   by ``_parse_token_count``: positive only, suffix-aware, hard cap.
2. ``_best_pick_for_role`` — verifies the budget filter actually drops
   candidates whose ``estimatedVramGb`` exceeds the cap. Models with no
   estimate in the catalog must pass through (same convention as
   ``fits_available_vram`` — "don't filter when we can't measure").
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestParseMemoryGb(unittest.TestCase):

    def setUp(self) -> None:
        from agentcommander.tui.commands import _parse_memory_gb
        self.parse = _parse_memory_gb

    def test_gb_suffix(self) -> None:
        self.assertEqual(self.parse("12gb"), 12.0)
        self.assertEqual(self.parse("16GB"), 16.0)
        self.assertEqual(self.parse("6.5g"), 6.5)

    def test_bare_number_is_gb(self) -> None:
        self.assertEqual(self.parse("8"), 8.0)
        self.assertEqual(self.parse("24"), 24.0)

    def test_tb_suffix_multiplies_by_1024(self) -> None:
        self.assertEqual(self.parse("1tb"), 1024.0)
        self.assertEqual(self.parse("2T"), 2048.0)

    def test_rejects_garbage(self) -> None:
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse("xyz"))
        self.assertIsNone(self.parse("12gb extra"))

    def test_rejects_non_positive(self) -> None:
        self.assertIsNone(self.parse("0"))
        self.assertIsNone(self.parse("-12gb"))

    def test_caps_unreasonable_values(self) -> None:
        # 5tb = 5120gb, just over the 4096 cap — must be rejected so a
        # fat-finger entry doesn't silently disable the filter.
        self.assertIsNone(self.parse("5tb"))
        self.assertIsNone(self.parse("100000gb"))


class TestBestPickRespectsMemoryBudget(unittest.TestCase):
    """Synthetic candidate list, no catalog dependency."""

    def _mk(self, model_id: str, score: float, vram_gb: float | None) -> object:
        from agentcommander.typecast.autoconfig import ModelCandidate
        entry: dict = {"roleScores": {"coder": {"score": score}}}
        if vram_gb is not None:
            entry["estimatedVramGb"] = vram_gb
        return ModelCandidate(model_id=model_id, entry=entry)

    def test_drops_models_over_budget(self) -> None:
        from unittest import mock
        from agentcommander.typecast import autoconfig
        from agentcommander.types import Role

        cands = [
            self._mk("big:70b", 100, 40.0),     # over 12 GB
            self._mk("mid:13b", 80, 8.0),       # under 12 GB
            self._mk("small:3b", 60, 2.0),      # under 12 GB
        ]
        # Stub VRAM detection so the live filter doesn't interfere.
        with mock.patch.object(autoconfig, "fits_available_vram",
                               return_value=True):
            best, score = autoconfig._best_pick_for_role(
                Role("coder"), cands, max_memory_gb=12.0,
            )
        self.assertIsNotNone(best)
        self.assertEqual(best.model_id, "mid:13b")
        self.assertEqual(score, 80.0)

    def test_no_budget_picks_highest_score(self) -> None:
        from unittest import mock
        from agentcommander.typecast import autoconfig
        from agentcommander.types import Role

        cands = [
            self._mk("big:70b", 100, 40.0),
            self._mk("mid:13b", 80, 8.0),
        ]
        with mock.patch.object(autoconfig, "fits_available_vram",
                               return_value=True):
            best, _ = autoconfig._best_pick_for_role(
                Role("coder"), cands, max_memory_gb=0.0,
            )
        self.assertEqual(best.model_id, "big:70b")

    def test_unknown_estimate_passes_filter(self) -> None:
        """Catalog entries without estimatedVramGb shouldn't be silently
        dropped — same convention as fits_available_vram."""
        from unittest import mock
        from agentcommander.typecast import autoconfig
        from agentcommander.types import Role

        cands = [self._mk("unknown:7b", 80, None)]
        with mock.patch.object(autoconfig, "fits_available_vram",
                               return_value=True):
            best, _ = autoconfig._best_pick_for_role(
                Role("coder"), cands, max_memory_gb=12.0,
            )
        self.assertIsNotNone(best)
        self.assertEqual(best.model_id, "unknown:7b")

    def test_budget_below_smallest_returns_none(self) -> None:
        from unittest import mock
        from agentcommander.typecast import autoconfig
        from agentcommander.types import Role

        cands = [
            self._mk("a:7b", 80, 5.0),
            self._mk("b:13b", 90, 10.0),
        ]
        with mock.patch.object(autoconfig, "fits_available_vram",
                               return_value=True):
            best, _ = autoconfig._best_pick_for_role(
                Role("coder"), cands, max_memory_gb=2.0,
            )
        self.assertIsNone(best)


if __name__ == "__main__":
    unittest.main()
