"""Tests for the persistent input-history backbuffer (Up/Down recall)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcommander.tui import status_bar as sb


class _HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hist-")
        self.file = Path(self.dir) / "input_history"
        self._patch = mock.patch.object(sb, "_history_file", return_value=self.file)
        self._patch.start()
        sb._history.clear()
        sb._history_loaded = False

    def tearDown(self):
        self._patch.stop()
        sb._history.clear()
        sb._history_loaded = False


class TestRecord(_HistoryTest):
    def test_records_and_persists(self):
        sb._record_history("first")
        sb._record_history("second")
        self.assertEqual(sb._history, ["first", "second"])
        self.assertEqual(self.file.read_text(encoding="utf-8").split(), ["first", "second"])

    def test_dedup_consecutive(self):
        sb._record_history("a")
        sb._record_history("a")
        sb._record_history("b")
        sb._record_history("a")
        self.assertEqual(sb._history, ["a", "b", "a"])

    def test_skips_blank(self):
        sb._record_history("   ")
        sb._record_history("")
        self.assertEqual(sb._history, [])

    def test_survives_reload(self):
        sb._record_history("alpha")
        sb._record_history("beta")
        # Simulate a fresh launch.
        sb._history.clear()
        sb._history_loaded = False
        sb.load_input_history()
        self.assertEqual(sb._history, ["alpha", "beta"])


class TestLoadTrim(_HistoryTest):
    def test_load_trims_to_max(self):
        with mock.patch.object(sb, "_HISTORY_MAX", 3):
            self.file.write_text("\n".join(f"c{i}" for i in range(10)) + "\n",
                                 encoding="utf-8")
            sb.load_input_history()
            self.assertEqual(sb._history, ["c7", "c8", "c9"])
            # File rewritten trimmed.
            self.assertEqual(self.file.read_text(encoding="utf-8").split(),
                             ["c7", "c8", "c9"])

    def test_load_missing_file_is_noop(self):
        sb.load_input_history()
        self.assertEqual(sb._history, [])


if __name__ == "__main__":
    unittest.main()
