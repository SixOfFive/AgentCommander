"""URL form of /roles set auto-creates an ollama provider.

Lets the user bind a role to a server that isn't in /providers yet:

    /roles set orchestrator http://192.168.15.103:11434 devstral:24b

The handler synthesizes a provider id from the host:port, registers the
provider, then proceeds with the normal role-override path. Re-using the
same URL on a later call must NOT create a duplicate provider.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestRolesSetUrlForm(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = Path.cwd()
        import os
        os.chdir(self._tmp.name)
        # Re-init DB in the temp dir so we don't pollute the project DB.
        from agentcommander.db import connection as _conn
        _conn._db = None  # type: ignore[attr-defined]
        from agentcommander.db.connection import init_db
        init_db()

    def tearDown(self) -> None:
        import os
        os.chdir(self._cwd)
        from agentcommander.db import connection as _conn
        try:
            _conn.close_db()
        except Exception:  # noqa: BLE001
            pass
        _conn._db = None  # type: ignore[attr-defined]
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            # Windows occasionally holds the sqlite file briefly; tmp dir is
            # in %TEMP% and gets reaped automatically. Don't fail the test.
            pass

    def _run_set(self, *args: str) -> list[str]:
        """Invoke cmd_roles with the given argv and capture render_system_line output."""
        from agentcommander.tui import commands as cmd_mod
        captured: list[str] = []
        with mock.patch.object(cmd_mod, "render_system_line",
                               side_effect=lambda s: captured.append(s)):
            cmd_mod.cmd_roles(mock.Mock(), list(args))
        return captured

    def test_url_form_creates_provider_and_sets_role(self) -> None:
        from agentcommander.db.repos import list_providers, get_role_assignment
        from agentcommander.types import Role

        out = self._run_set("set", "orchestrator",
                            "http://192.168.15.103:11434", "devstral-small-2:24b")

        providers = list_providers()
        # Exactly one auto-created provider, type=ollama, endpoint matches.
        self.assertEqual(len(providers), 1)
        p = providers[0]
        self.assertEqual(p.type, "ollama")
        self.assertEqual(p.endpoint, "http://192.168.15.103:11434")
        self.assertTrue(p.id.startswith("auto-"))

        # Role assignment points at the auto-created provider.
        ra = get_role_assignment(Role("orchestrator"))
        self.assertIsNotNone(ra)
        self.assertEqual(ra["provider_id"], p.id)
        self.assertEqual(ra["model"], "devstral-small-2:24b")
        # Confirmation lines mention provider creation + role binding.
        joined = "\n".join(out)
        self.assertIn("created provider", joined)
        self.assertIn("orchestrator", joined)

    def test_url_form_reuses_existing_provider_on_repeat(self) -> None:
        from agentcommander.db.repos import list_providers
        url = "http://192.168.15.103:11434"
        self._run_set("set", "orchestrator", url, "devstral-small-2:24b")
        out2 = self._run_set("set", "coder", url, "qwen3-coder:30b")

        providers = list_providers()
        self.assertEqual(len(providers), 1, "second URL set must reuse the provider")
        self.assertIn("reused existing provider", "\n".join(out2))

    def test_trailing_slash_normalised_for_dedup(self) -> None:
        from agentcommander.db.repos import list_providers
        self._run_set("set", "orchestrator",
                      "http://192.168.15.103:11434", "m1:8b")
        self._run_set("set", "coder",
                      "http://192.168.15.103:11434/", "m2:8b")
        self.assertEqual(len(list_providers()), 1)

    def test_https_url_also_accepted(self) -> None:
        from agentcommander.db.repos import list_providers
        self._run_set("set", "orchestrator",
                      "https://example.com:8443", "model:1b")
        providers = list_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].endpoint, "https://example.com:8443")

    def test_non_url_pid_takes_normal_path(self) -> None:
        """Passing an existing provider id (not a URL) must NOT create a
        provider — the original behavior is preserved."""
        from agentcommander.db.repos import list_providers, upsert_provider
        from agentcommander.types import ProviderConfig
        upsert_provider(ProviderConfig(
            id="manual-prov", type="ollama",  # type: ignore[arg-type]
            name="manual", endpoint="http://x:1", enabled=True))
        before = len(list_providers())
        self._run_set("set", "orchestrator", "manual-prov", "m:1b")
        self.assertEqual(len(list_providers()), before)


if __name__ == "__main__":
    unittest.main()
