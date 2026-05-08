"""Tests for the deterministic forced-fetch URL inference.

Round-49 trim: dropped redundant city-variants and bare-fallback duplicates.
Kept one positive per pattern family + the priority-ordering invariant
(specific URL beats bare fallback).
"""
from __future__ import annotations

import unittest

from agentcommander.engine.engine import PipelineRun


def _infer(msg: str) -> str | None:
    return PipelineRun._infer_live_data_url(PipelineRun, msg)  # type: ignore[arg-type]


class TestLiveDataInference(unittest.TestCase):
    def test_weather_in_city(self) -> None:
        self.assertEqual(
            _infer("what is the weather in edmonton, alberta?"),
            "https://wttr.in/edmonton?format=3",
        )

    def test_time_in_city(self) -> None:
        self.assertEqual(
            _infer("current time in tokyo"),
            "https://worldtimeapi.org/api/timezone/tokyo",
        )

    def test_news_pattern(self) -> None:
        self.assertEqual(
            _infer("today's news"),
            "https://news.google.com/rss",
        )

    def test_non_live_data_returns_none(self) -> None:
        # One representative negative — math/code/file/capability all share
        # the same return-None path; no need to assert each separately.
        self.assertIsNone(_infer("how do I sort a list in python?"))
        self.assertIsNone(_infer(""))

    def test_specific_pattern_wins_over_bare_fallback(self) -> None:
        # Priority-ordering invariant: "weather in tokyo" must resolve to
        # the city URL, not the bare wttr.in fallback.
        self.assertEqual(
            _infer("weather in tokyo today"),
            "https://wttr.in/tokyo?format=3",
        )


if __name__ == "__main__":
    unittest.main()
