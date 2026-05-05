"""Tests for the deterministic forced-fetch URL inference.

Exercises ``PipelineRun._infer_live_data_url`` — the table-driven
pattern-match that picks a concrete URL when the orchestrator has
declined twice on a live-data question. Pure regex/lambda unit tests;
no live model, no real network.
"""
from __future__ import annotations

import unittest

from agentcommander.engine.engine import PipelineRun


def _infer(msg: str) -> str | None:
    """Bind-free helper — the method only reads class-level state."""
    return PipelineRun._infer_live_data_url(PipelineRun, msg)  # type: ignore[arg-type]


class TestWeatherInference(unittest.TestCase):
    def test_weather_in_city(self) -> None:
        self.assertEqual(
            _infer("what is the weather in edmonton, alberta?"),
            "https://wttr.in/edmonton?format=3",
        )

    def test_weather_in_simple_city(self) -> None:
        self.assertEqual(
            _infer("weather in tokyo"),
            "https://wttr.in/tokyo?format=3",
        )

    def test_forecast_for_multiword_city(self) -> None:
        # Multi-word cities collapse spaces to + per wttr.in URL convention.
        self.assertEqual(
            _infer("forecast for new york today"),
            "https://wttr.in/new+york?format=3",
        )

    def test_temperature_in_with_trailing_modifier(self) -> None:
        self.assertEqual(
            _infer("temperature in paris right now"),
            "https://wttr.in/paris?format=3",
        )

    def test_bare_weather_falls_back_to_ip_geolocation(self) -> None:
        self.assertEqual(
            _infer("what's the weather?"),
            "https://wttr.in/?format=3",
        )

    def test_bare_forecast(self) -> None:
        self.assertEqual(
            _infer("show me the forecast"),
            "https://wttr.in/?format=3",
        )


class TestTimeInference(unittest.TestCase):
    def test_current_time_in_city(self) -> None:
        self.assertEqual(
            _infer("current time in tokyo"),
            "https://worldtimeapi.org/api/timezone/tokyo",
        )

    def test_what_time_is_it_falls_back_to_ip(self) -> None:
        self.assertEqual(
            _infer("what time is it?"),
            "https://worldtimeapi.org/api/ip",
        )

    def test_time_in_multiword_location(self) -> None:
        # Multi-word locations get joined with underscores, matching
        # the worldtimeapi.org timezone-path convention.
        self.assertEqual(
            _infer("time in new york"),
            "https://worldtimeapi.org/api/timezone/new_york",
        )


class TestNewsInference(unittest.TestCase):
    def test_todays_news(self) -> None:
        self.assertEqual(
            _infer("today's news"),
            "https://news.google.com/rss",
        )

    def test_latest_headlines(self) -> None:
        self.assertEqual(
            _infer("latest news headlines"),
            "https://news.google.com/rss",
        )

    def test_breaking_news(self) -> None:
        self.assertEqual(
            _infer("breaking news"),
            "https://news.google.com/rss",
        )

    def test_top_stories(self) -> None:
        self.assertEqual(
            _infer("top stories today"),
            "https://news.google.com/rss",
        )


class TestNonLiveDataReturnsNone(unittest.TestCase):
    def test_code_question(self) -> None:
        self.assertIsNone(_infer("how do I sort a list in python?"))

    def test_math_question(self) -> None:
        self.assertIsNone(_infer("what is 2+2?"))

    def test_file_action(self) -> None:
        self.assertIsNone(_infer("write fizzbuzz.py"))

    def test_capability_question(self) -> None:
        self.assertIsNone(_infer("what tools do you have?"))

    def test_empty_message(self) -> None:
        self.assertIsNone(_infer(""))

    def test_word_weather_inside_unrelated_prose(self) -> None:
        # "weatherbeaten" / "feathered" wouldn't match thanks to \b
        # boundaries in the regex.
        self.assertIsNone(_infer("the weatherbeaten ship sailed on"))


class TestPriorityOrdering(unittest.TestCase):
    """Specific patterns must win over the bare fall-through. Verifies
    the table's ordering invariant — without it, "weather in tokyo"
    would resolve to the bare fallback URL."""

    def test_weather_in_city_beats_bare_weather(self) -> None:
        url = _infer("weather in tokyo today")
        self.assertEqual(url, "https://wttr.in/tokyo?format=3")
        self.assertNotEqual(url, "https://wttr.in/?format=3")

    def test_time_in_city_beats_bare_time(self) -> None:
        url = _infer("current time in london")
        self.assertEqual(url, "https://worldtimeapi.org/api/timezone/london")
        self.assertNotEqual(url, "https://worldtimeapi.org/api/ip")


if __name__ == "__main__":
    unittest.main()
