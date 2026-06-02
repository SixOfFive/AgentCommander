"""Tests for schema-constrained orchestrator decoding (improvement #1).

Two layers:

1. The schema generator (``engine/decision_schema.py``) — pure, no network.
   The most important test is ``test_schema_props_match_dataclass``: a
   drift detector that fails if ``OrchestratorDecision`` grows/loses a field
   without the schema following. This mirrors the project's existing
   schema-drift detector for tool payloads.

2. Provider wiring — that passing ``json_schema`` actually lands in each
   provider's request body in the right shape, and takes precedence over
   the loose ``json_mode`` path. The request body is fully built before any
   response is read, so a fake ``urlopen`` that just captures ``req.data``
   is enough — no live daemon required.
"""
from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from dataclasses import fields
from unittest import mock

from agentcommander.engine.actions import ALL_ACTIONS
from agentcommander.engine.decision_schema import (
    orchestrator_decision_field_names,
    orchestrator_decision_schema,
)
from agentcommander.types import OrchestratorDecision


# ─── 1. Schema generator (pure) ─────────────────────────────────────────────


class TestDecisionSchema(unittest.TestCase):

    def test_action_enum_is_all_actions(self) -> None:
        s = orchestrator_decision_schema()
        self.assertEqual(set(s["properties"]["action"]["enum"]), set(ALL_ACTIONS))

    def test_every_real_verb_present(self) -> None:
        enum = orchestrator_decision_schema()["properties"]["action"]["enum"]
        for verb in ("done", "fetch", "write_file", "execute", "research",
                     "translate", "http_request", "git", "env", "browser"):
            self.assertIn(verb, enum)

    def test_schema_props_match_dataclass(self) -> None:
        """Drift detector: schema properties == OrchestratorDecision fields.

        If someone adds a field to OrchestratorDecision but not to the
        schema (or vice-versa), this fails — preventing a silent gap where
        a field the parser consumes can't be generated under the grammar.
        """
        schema_props = set(orchestrator_decision_schema()["properties"].keys())
        dc_fields = {f.name for f in fields(OrchestratorDecision)}
        self.assertEqual(schema_props, dc_fields)

    def test_field_names_helper_matches_dataclass(self) -> None:
        self.assertEqual(
            set(orchestrator_decision_field_names()),
            {f.name for f in fields(OrchestratorDecision)},
        )

    def test_only_action_required(self) -> None:
        self.assertEqual(orchestrator_decision_schema()["required"], ["action"])

    def test_no_closed_set_restriction(self) -> None:
        # Must NOT set additionalProperties:false — it makes the GBNF grammar
        # an order of magnitude slower to decode (134s vs 12s on qwen2.5:14b)
        # for zero benefit (from_dict drops unknown keys). See module docstring.
        self.assertNotIn("additionalProperties", orchestrator_decision_schema())

    def test_non_string_field_types(self) -> None:
        props = orchestrator_decision_schema()["properties"]
        self.assertEqual(props["port"]["type"], "integer")
        self.assertEqual(props["headers"]["type"], "object")
        self.assertEqual(props["steps"]["type"], "array")

    def test_default_field_is_string(self) -> None:
        props = orchestrator_decision_schema()["properties"]
        for name in ("url", "path", "content", "reasoning", "input", "command"):
            self.assertEqual(props[name]["type"], "string", name)

    def test_json_serializable(self) -> None:
        # Must round-trip cleanly — it ships inside an HTTP request body.
        s = orchestrator_decision_schema()
        self.assertEqual(json.loads(json.dumps(s)), s)

    def test_returns_fresh_dict(self) -> None:
        a = orchestrator_decision_schema()
        a["properties"]["action"]["enum"].append("MUTATED")
        b = orchestrator_decision_schema()
        self.assertNotIn("MUTATED", b["properties"]["action"]["enum"])


# ─── 2. Provider wiring (fake urlopen captures the request body) ────────────


class _EmptyResp:
    """Context-manager response that yields no stream lines.

    The provider builds and sends its request body *before* reading any
    response, so an empty stream is enough to exercise body construction.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def read(self, *a):
        return b""


@contextmanager
def _capture_body(holder: dict):
    def fake(req, *args, **kwargs):  # noqa: ARG001
        try:
            holder["body"] = json.loads(req.data.decode("utf-8"))
        except Exception:  # noqa: BLE001
            holder["body"] = None
        return _EmptyResp()

    with mock.patch("urllib.request.urlopen", fake):
        yield


def _drain(gen) -> None:
    try:
        for _ in gen:
            pass
    except Exception:  # noqa: BLE001 - we only care about the captured request
        pass


class TestOllamaSchemaBody(unittest.TestCase):

    def _provider(self):
        from agentcommander.providers.ollama import OllamaProvider
        return OllamaProvider(id="test-ollama", endpoint="http://127.0.0.1:11434")

    def _msgs(self):
        from agentcommander.providers.base import ChatMessage
        return [ChatMessage(role="user", content="hi")]

    def test_schema_sets_format_to_schema(self) -> None:
        holder: dict = {}
        schema = orchestrator_decision_schema()
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_schema=schema))
        self.assertEqual(holder["body"]["format"], schema)

    def test_json_mode_only_is_loose(self) -> None:
        holder: dict = {}
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_mode=True))
        self.assertEqual(holder["body"]["format"], "json")

    def test_schema_takes_precedence_over_json_mode(self) -> None:
        holder: dict = {}
        schema = orchestrator_decision_schema()
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_mode=True, json_schema=schema))
        self.assertEqual(holder["body"]["format"], schema)

    def test_no_format_when_neither(self) -> None:
        holder: dict = {}
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs()))
        self.assertNotIn("format", holder["body"])


class TestLlamaCppSchemaBody(unittest.TestCase):

    def _provider(self):
        from agentcommander.providers.llamacpp import LlamaCppProvider
        return LlamaCppProvider(id="test-llamacpp", endpoint="http://127.0.0.1:8080")

    def _msgs(self):
        from agentcommander.providers.base import ChatMessage
        return [ChatMessage(role="user", content="hi")]

    def test_schema_sets_json_schema_response_format(self) -> None:
        holder: dict = {}
        schema = orchestrator_decision_schema()
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_schema=schema))
        rf = holder["body"]["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["schema"], schema)

    def test_json_mode_only_is_json_object(self) -> None:
        holder: dict = {}
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_mode=True))
        self.assertEqual(holder["body"]["response_format"], {"type": "json_object"})


class TestOpenRouterSchemaBody(unittest.TestCase):

    def _provider(self):
        from agentcommander.providers.openrouter import OpenRouterProvider
        return OpenRouterProvider(id="test-or", type="openrouter",
                                  endpoint="https://openrouter.ai/api/v1",
                                  api_key="sk-test")

    def _msgs(self):
        from agentcommander.providers.base import ChatMessage
        return [ChatMessage(role="user", content="hi")]

    def test_schema_sets_json_schema_response_format(self) -> None:
        holder: dict = {}
        schema = orchestrator_decision_schema()
        with _capture_body(holder):
            _drain(self._provider().chat(model="m", messages=self._msgs(),
                                         json_schema=schema))
        rf = holder["body"]["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["schema"], schema)


# ─── 3. call_role forwards the schema and forces json output ────────────────


class TestCallRoleForwardsSchema(unittest.TestCase):

    def test_schema_forwarded_and_forces_json_mode(self) -> None:
        """call_role must pass json_schema through and never send a schema
        with json_mode False (a schema is a strict superset of json_mode)."""
        from agentcommander.engine import role_call

        captured: dict = {}

        class _FakeProvider:
            def chat(self, **kwargs):
                captured.update(kwargs)
                from agentcommander.providers.base import ChatChunk
                yield ChatChunk(content="{}", done=True,
                                prompt_tokens=1, completion_tokens=1)

        class _Resolved:
            provider_id = "p"
            model = "m"
            kind = "override"
            context_window_tokens = None

        schema = orchestrator_decision_schema()
        with mock.patch.object(role_call, "resolve_role", return_value=_Resolved()), \
             mock.patch.object(role_call, "resolve", return_value=_FakeProvider()), \
             mock.patch.object(role_call, "audit"), \
             mock.patch.object(role_call, "insert_token_usage"):
            role_call.call_role("orchestrator", user_input="hi",
                                json_mode=False, json_schema=schema)

        self.assertEqual(captured["json_schema"], schema)
        self.assertIs(captured["json_mode"], True)


if __name__ == "__main__":
    unittest.main()
