"""Tests: the notes vault is read-only to AgentCommander.

Two layers of protection beyond the vault tools (which only open files for
reading):
  1. A sandbox read-only zone — write/delete file ops inside the vault are
     refused even when the working directory is the vault itself.
  2. A best-effort execute guard — code that writes/deletes inside the vault
     is blocked before it runs (reads pass).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from agentcommander.safety import sandbox
from agentcommander.tools import code_tool


class TestReadonlyZone(unittest.TestCase):
    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="vault-ro-")
        sandbox.clear_readonly_zones()
        sandbox.register_readonly_zone(self.vault)

    def tearDown(self):
        sandbox.clear_readonly_zones()

    def test_membership(self):
        self.assertTrue(sandbox.is_in_readonly_zone(os.path.join(self.vault, "n.md")))
        self.assertFalse(sandbox.is_in_readonly_zone(tempfile.gettempdir() + "/elsewhere.txt"))

    def test_write_denied_even_when_workdir_is_vault(self):
        # Worst case: the working directory IS the vault. Reads pass, writes don't.
        sandbox.validate_file_access("note.md", self.vault, "read")  # no raise
        with self.assertRaises(sandbox.FilesystemSecurityError):
            sandbox.validate_file_access("note.md", self.vault, "write")
        with self.assertRaises(sandbox.FilesystemSecurityError):
            sandbox.validate_file_access("note.md", self.vault, "delete")

    def test_write_denied_when_workdir_above_vault(self):
        parent = os.path.dirname(self.vault)
        rel = os.path.join(os.path.basename(self.vault), "note.md")
        # read allowed, write/delete refused because it lands in the zone
        sandbox.validate_file_access(rel, parent, "read")
        with self.assertRaises(sandbox.FilesystemSecurityError):
            sandbox.validate_file_access(rel, parent, "write")

    def test_no_zone_no_restriction(self):
        sandbox.clear_readonly_zones()
        wd = tempfile.mkdtemp(prefix="wd-")
        # Without a zone, a normal in-workdir write validates fine.
        p = sandbox.validate_file_access("x.txt", wd, "write")
        self.assertTrue(p.endswith("x.txt"))


class TestExecuteVaultGuard(unittest.TestCase):
    def _patch_vault(self, root):
        return mock.patch("agentcommander.db.repos.get_config",
                          side_effect=lambda k, d=None: root if k == "vault_path" else d)

    def test_blocks_write_to_vault(self):
        root = r"C:/Users/me/Obsidian/myvault"
        with self._patch_vault(root):
            self.assertIsNotNone(code_tool._scan_vault_write(
                "open('C:/Users/me/Obsidian/myvault/notes/x.md', 'w').write('hi')"))
            self.assertIsNotNone(code_tool._scan_vault_write(
                "import os; os.remove('C:/Users/me/Obsidian/myvault/a.md')"))
            self.assertIsNotNone(code_tool._scan_vault_write(
                "echo hacked > C:/Users/me/Obsidian/myvault/a.md"))

    def test_allows_read_of_vault(self):
        root = r"C:/Users/me/Obsidian/myvault"
        with self._patch_vault(root):
            self.assertIsNone(code_tool._scan_vault_write(
                "open('C:/Users/me/Obsidian/myvault/notes/x.md').read()"))
            self.assertIsNone(code_tool._scan_vault_write(
                "print(open('C:/Users/me/Obsidian/myvault/a.md','r').read())"))

    def test_allows_write_outside_vault(self):
        root = r"C:/Users/me/Obsidian/myvault"
        with self._patch_vault(root):
            self.assertIsNone(code_tool._scan_vault_write(
                "open('output.txt','w').write('result')"))

    def test_no_vault_configured_passes(self):
        with mock.patch("agentcommander.db.repos.get_config", return_value=None):
            self.assertIsNone(code_tool._scan_vault_write("open('x','w')"))


if __name__ == "__main__":
    unittest.main()
