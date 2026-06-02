"""Tests for /roles set model-existence validation (catch ghost bindings)."""
from __future__ import annotations

import unittest
from unittest import mock

from agentcommander.tui import commands
from agentcommander.providers import base as provider_base


class _FakeProvider:
    def __init__(self, models):
        self._models = models

    def list_models(self):
        return [{"id": m} for m in self._models]


class _Unreachable:
    def list_models(self):
        raise OSError("connection refused")


class TestVerifyModelInstalled(unittest.TestCase):
    def test_present(self):
        with mock.patch.object(provider_base, "resolve",
                               return_value=_FakeProvider(["qwen2.5:14b", "gemma3:4b"])):
            msg = commands.verify_model_installed("beast", "qwen2.5:14b")
        self.assertIn("verified", msg)

    def test_latest_normalization(self):
        with mock.patch.object(provider_base, "resolve",
                               return_value=_FakeProvider(["cogito:latest"])):
            msg = commands.verify_model_installed("beast", "cogito")
        self.assertIn("verified", msg)

    def test_missing_warns_with_suggestions(self):
        with mock.patch.object(provider_base, "resolve",
                               return_value=_FakeProvider(["cogito:latest", "llama3:8b"])):
            msg = commands.verify_model_installed("beast", "cogito:8b")
        self.assertIn("NOT installed", msg)
        self.assertIn("404", msg)
        self.assertIn("cogito:latest", msg)  # near-match suggestion

    def test_unreachable_warns_but_allows(self):
        with mock.patch.object(provider_base, "resolve", return_value=_Unreachable()):
            msg = commands.verify_model_installed("beast", "x")
        self.assertIn("couldn't reach", msg)

    def test_provider_not_loaded(self):
        with mock.patch.object(provider_base, "resolve",
                               side_effect=provider_base.ProviderError("nope")):
            msg = commands.verify_model_installed("ghost", "x")
        self.assertIn("not loaded", msg)

    def test_model_present_helper(self):
        self.assertTrue(commands._model_present("a:1", ["a:1"]))
        self.assertTrue(commands._model_present("a", ["a:latest"]))
        self.assertFalse(commands._model_present("a:8b", ["a:latest"]))


if __name__ == "__main__":
    unittest.main()
