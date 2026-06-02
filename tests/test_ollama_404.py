"""Regression: a 404 from Ollama /api/chat (model not installed) must produce
a self-explanatory error naming the model — not a bare 'HTTP 404 Not Found'
that sends you chasing a URL bug when a role is bound to a ghost model."""
from __future__ import annotations

import email.message
import io
import unittest
import urllib.error
from unittest import mock

from agentcommander.providers import ollama as ollama_mod
from agentcommander.providers.base import ChatMessage, ProviderError


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://192.168.15.103:11434/api/chat", code, "Not Found",
        email.message.Message(), io.BytesIO(body))


class TestOllama404(unittest.TestCase):
    def _provider(self):
        return ollama_mod.OllamaProvider(id="beast", endpoint="http://192.168.15.103:11434")

    def _run(self, code, body):
        def _raise(*a, **k):
            raise _http_error(code, body)
        with mock.patch.object(ollama_mod, "_post_stream", side_effect=_raise):
            list(self._provider().chat(model="cogito:8b",
                                       messages=[ChatMessage(role="user", content="hi")]))

    def test_404_names_the_model_and_host(self):
        with self.assertRaises(ProviderError) as cm:
            self._run(404, b'{"error": "model \\"cogito:8b\\" not found, try pulling it first"}')
        msg = str(cm.exception)
        self.assertIn("cogito:8b", msg)
        self.assertIn("not found", msg.lower())
        self.assertIn("192.168.15.103", msg)
        # The bare-reason wording must be gone.
        self.assertNotEqual(msg, "Ollama /api/chat failed: HTTP 404 Not Found")

    def test_other_http_error_includes_body_detail(self):
        with self.assertRaises(ProviderError) as cm:
            self._run(500, b'{"error": "internal boom"}')
        msg = str(cm.exception)
        self.assertIn("500", msg)
        self.assertIn("internal boom", msg)


if __name__ == "__main__":
    unittest.main()
