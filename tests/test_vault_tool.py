"""Tests for the read-only vault recall tools (vault_search / vault_read).

Use a throwaway vault dir + a fake embeddings index; the embedding HTTP call
is monkeypatched so no network/DB is needed. Covers: lexical ranking +
stopword filtering, the curated-scope restriction (the fix that keeps the
lexical fallback off raw session archives), semantic cosine ranking, the
sandbox (no escaping the vault root), and the unconfigured guard.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from agentcommander.tools import vault_tool as vt


def _make_vault():
    root = tempfile.mkdtemp(prefix="vault-test-")
    os.makedirs(os.path.join(root, "Topics"))
    os.makedirs(os.path.join(root, "_index"))
    os.makedirs(os.path.join(root, "sessions"))
    notes = {
        "Topics/llamacpp VRAM.md":
            "# llama.cpp VRAM\nFit a large MoE model into limited VRAM with "
            "offloading and quantization on llama.cpp.",
        "Topics/Cooking.md":
            "# Cooking\nHow to bake sourdough bread at home.",
        # In the tree but NOT in the index — must be excluded from lexical
        # when an index exists (curated-scope restriction).
        "sessions/raw log.md":
            "raw chat log mentioning VRAM and llama.cpp and a secret token.",
    }
    for rel, body in notes.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(body)
    # Index covers only the two curated Topics notes.
    index = {
        "llamacpp VRAM": {"mtime": 1, "vec": [1.0, 0.0, 0.0]},
        "Cooking": {"mtime": 1, "vec": [0.0, 1.0, 0.0]},
    }
    with open(os.path.join(root, "_index", "embeddings.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return root


class TestLexical(unittest.TestCase):
    def setUp(self):
        self.root = _make_vault()

    def test_finds_relevant_note(self):
        res = vt._lexical_search(self.root, "VRAM llama.cpp", 5)
        self.assertTrue(res)
        self.assertEqual(res[0][0], "llamacpp VRAM")

    def test_stopwords_filtered(self):
        # All-stopword-ish query still resolves to meaningful terms.
        res = vt._lexical_search(self.root, "how to fit a model in VRAM", 5)
        names = [n for n, _ in res]
        self.assertIn("llamacpp VRAM", names)

    def test_restricted_to_indexed_scope(self):
        # The sessions/ note matches "VRAM"/"llama.cpp" but isn't in the index,
        # so it must NOT be returned (keeps fallback off raw archives).
        res = vt._lexical_search(self.root, "VRAM llama.cpp secret token", 5)
        names = [n for n, _ in res]
        self.assertNotIn("raw log", names)
        self.assertIn("llamacpp VRAM", names)


class TestResolveAndSandbox(unittest.TestCase):
    def setUp(self):
        self.root = _make_vault()

    def test_resolve_by_name(self):
        p = vt._resolve_note(self.root, "llamacpp VRAM")
        self.assertIsNotNone(p)
        self.assertTrue(p.endswith("llamacpp VRAM.md"))

    def test_resolve_by_relpath(self):
        p = vt._resolve_note(self.root, "Topics/Cooking.md")
        self.assertIsNotNone(p)

    def test_traversal_blocked(self):
        self.assertIsNone(vt._resolve_note(self.root, "../../../etc/passwd"))
        self.assertIsNone(vt._resolve_note(self.root, "../secrets"))

    def test_dotted_name_resolves(self):
        # Regression: os.path.splitext truncated "llama.cpp …" → "llama".
        # Note titles with dots (llama.cpp, hvr.biz, dwd.info) must resolve.
        os.makedirs(os.path.join(self.root, "Topics"), exist_ok=True)
        with open(os.path.join(self.root, "Topics", "llama.cpp VRAM tricks.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("# dotted\nbody")
        self.assertIsNotNone(vt._resolve_note(self.root, "llama.cpp VRAM tricks"))
        self.assertIsNotNone(vt._resolve_note(self.root, "[[llama.cpp VRAM tricks]]"))

    def test_note_key_strips_md_not_dots(self):
        self.assertEqual(vt._note_key("Topics/hvr.biz topics.md"), "hvr.biz topics")
        self.assertEqual(vt._note_key("dwd.info plan"), "dwd.info plan")


class TestSemantic(unittest.TestCase):
    def setUp(self):
        self.root = _make_vault()

    def test_cosine(self):
        self.assertAlmostEqual(vt._cosine([1, 0, 0], [1, 0, 0]), 1.0, places=6)
        self.assertAlmostEqual(vt._cosine([1, 0, 0], [0, 1, 0]), 0.0, places=6)

    def test_semantic_ranks_by_cosine(self):
        # Query vector aligned with the VRAM note's [1,0,0].
        with mock.patch.object(vt, "_embed_endpoint", return_value="http://x"), \
             mock.patch.object(vt, "_embed_query", return_value=[0.9, 0.1, 0.0]):
            res = vt._semantic_search(self.root, "vram moe", 5)
        self.assertIsNotNone(res)
        self.assertEqual(res[0][0], "llamacpp VRAM")

    def test_semantic_none_when_embed_fails(self):
        with mock.patch.object(vt, "_embed_endpoint", return_value="http://x"), \
             mock.patch.object(vt, "_embed_query", return_value=None):
            self.assertIsNone(vt._semantic_search(self.root, "q", 5))

    def test_semantic_none_when_no_endpoint(self):
        with mock.patch.object(vt, "_embed_endpoint", return_value=None):
            self.assertIsNone(vt._semantic_search(self.root, "q", 5))


class TestHandlers(unittest.TestCase):
    def _ctx(self):
        from agentcommander.tools.types import ToolContext
        return ToolContext(working_directory=None, conversation_id=None,
                           audit=lambda *a, **k: None)

    def test_unconfigured_returns_error(self):
        with mock.patch.object(vt, "vault_root", return_value=None):
            r = vt._vault_search({"input": "x"}, self._ctx())
            self.assertFalse(r.ok)
            self.assertIn("not configured", r.error)
            r2 = vt._vault_read({"input": "x"}, self._ctx())
            self.assertFalse(r2.ok)

    def test_read_missing_note(self):
        root = _make_vault()
        with mock.patch.object(vt, "vault_root", return_value=root):
            r = vt._vault_read({"input": "no such note"}, self._ctx())
            self.assertFalse(r.ok)
            self.assertIn("not found", r.error)

    def test_search_lexical_via_handler(self):
        root = _make_vault()
        with mock.patch.object(vt, "vault_root", return_value=root):
            r = vt._vault_search({"input": "VRAM llama.cpp", "mode": "lexical"},
                                 self._ctx())
        self.assertTrue(r.ok)
        self.assertIn("llamacpp VRAM", r.output)
        self.assertEqual(r.data["mode"], "lexical")


if __name__ == "__main__":
    unittest.main()
