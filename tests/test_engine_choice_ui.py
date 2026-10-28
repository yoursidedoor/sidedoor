"""U1 3g: the engine choice in Setup and Settings, Claude's banners and the Antigravity notice."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engines
import license_fixtures
import provider_limits
import setup_steps
import user_profile

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

PORT = 47824
SECRET = "test-token-value"
HTMX = {"origin": f"http://127.0.0.1:{PORT}", "hx-request": "true"}


class FakeBrowser:
    def is_open(self):
        return False


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class EngineChoiceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home, self.root = Path(directory.name) / "home", Path(directory.name) / "root"
        self.home.mkdir()
        self.root.mkdir()
        environment = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        # No real Claude is ever probed: Check finds none.
        finder = mock.patch.object(setup_steps, "find_claude", return_value=None)
        finder.start()
        self.addCleanup(finder.stop)
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root,
                                   config={"text_model": {"model": "test/model"}}, browser=FakeBrowser(),
                                   relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def signed_out(self):
        provider_limits.noted(provider_limits.ProviderUsageLimit("CLAUDE_SIGNED_OUT", kind=provider_limits.CLAUDE_SIGNED_OUT),
                              self.home)

    def test_save_engine_lays_it_over_the_config(self):
        self.client.post("/settings/engine", data={"engine": "claude"}, headers=HTMX)
        self.assertEqual(setup_steps.load_state(self.home)["engine"], "claude")
        self.assertIs(engines.engine(setup_steps.with_engine({"model": "m"}, self.home)), engines.CLAUDE)
        self.client.post("/setup/engine", data={"engine": "codex"}, headers=HTMX)
        self.assertIs(engines.engine(setup_steps.with_engine({"engine": "claude"}, self.home)), engines.CODEX)

    def test_switch_clears_claude_waits_and_banner_links_to_settings(self):
        self.client.post("/settings/engine", data={"engine": "claude"}, headers=HTMX)
        self.signed_out()
        page = self.client.get("/settings").text
        self.assertIn('href="/settings#connection-claude"', page)   # R4, during Setup too
        self.assertIn('id="connection-claude"', page)
        self.client.post("/settings/engine", data={"engine": "codex"}, headers=HTMX)
        self.assertFalse(any((self.home / name).exists() for name in provider_limits.CLAUDE_WAIT_FILES.values()))
        self.assertNotIn("Claude is signed out", self.client.get("/settings").text)   # R6
        self.client.post("/settings/engine", data={"engine": "claude"}, headers=HTMX)
        self.signed_out()   # the clear is not sticky
        self.assertIn("Claude is signed out", self.client.get("/settings").text)

    def test_antigravity_listed_and_notice_when_chosen(self):
        page = self.client.get("/setup").text
        self.assertIn('value="antigravity"', page)   # U1 4: registered in engines.ENGINES
        self.assertNotIn("Claude Code", page)
        self.assertNotIn("antigravity-chosen", self.client.get("/settings").text)
        with mock.patch.object(setup_steps, "find_antigravity", return_value=None):
            self.client.post("/settings/engine", data={"engine": "antigravity"}, headers=HTMX)
            for page in (self.client.get("/settings").text, self.client.get("/setup").text):
                self.assertIn("antigravity-chosen", page)
                self.assertIn("may suspend your Antigravity or Gemini CLI account", page)
                self.assertIn("Antigravity is not installed.", page)
                self.assertIn("/setup/guide/antigravity", page)
        self.assertEqual(self.client.get("/setup/guide/antigravity").status_code, 200)

    def test_antigravity_chosen_applies_allow_rules_and_finishes_step_6(self):
        program = self.home / "agy"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        with mock.patch.object(setup_steps, "find_antigravity", return_value=str(program)), \
                mock.patch.object(setup_steps, "antigravity_probe", return_value=(True, [
                    ("claude-sonnet-4-6", "Claude Sonnet 4.6"), ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
                    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)")])), \
                mock.patch.object(engines.AntigravityEngine, "agy_home", staticmethod(lambda home=None: self.home / "agy-home")):
            self.client.post("/setup/engine", data={"engine": "antigravity"}, headers=HTMX)
            state = setup_steps.load_state(self.home)
            self.assertTrue(setup_steps.engine_done(state))
            rules = __import__("json").loads((self.home / "agy-home" / "settings.json").read_text())["permissions"]["allow"]
            self.assertIn("mcp(sidedoor_browser/arm)", rules)
            page = self.client.get("/settings").text
            self.assertIn("Antigravity is installed and signed in.", page)
            self.assertIn("Gemini 3.8 Flash often froze while reading job pages", page)   # M2
            self.assertIn('<option value="claude-sonnet-4-6" selected>', page)
            self.client.post("/settings/antigravity/models", data={"main": "gemini-3.8-flash-high",
                                                                    "read": "claude-sonnet-4-6"}, headers=HTMX)
            self.assertEqual(setup_steps.load_state(self.home)["antigravity_model"], "gemini-3.8-flash-high")
            self.assertIn('<option value="gemini-3.8-flash-high" selected>', self.client.get("/setup").text)

    def test_g1_finished_setup_names_claude_in_banner_and_health(self):
        setup_steps.update_state(lambda value: value.update(setup_finished_at="2026-10-28T00:00:00+00:00"), self.home)
        self.client.post("/settings/engine", data={"engine": "claude"}, headers=HTMX)
        settings = self.client.get("/settings").text
        self.assertIn("Claude stopped working.", settings)
        self.assertNotIn("Codex stopped working.", settings)
        overview = self.client.get("/").text
        self.assertIn("Claude is not installed.", overview)   # Health shows Claude's own record
        self.assertNotIn("Signed in with ChatGPT", overview)

    def test_g2_only_the_chosen_engines_waits_show(self):
        provider_limits.noted(provider_limits.ProviderUsageLimit("CODEX_NOT_FOUND", kind=provider_limits.CODEX_NOT_FOUND),
                              self.home)
        self.client.post("/settings/engine", data={"engine": "claude"}, headers=HTMX)
        self.assertNotIn("find Codex. Open", self.client.get("/settings").text)
        self.assertTrue((self.home / provider_limits.CODEX_WAIT_FILE).exists())   # kept, only hidden
        self.signed_out()   # an in-flight Claude run's wait after a switch to Codex stays hidden
        self.client.post("/settings/engine", data={"engine": "codex"}, headers=HTMX)
        self.signed_out()
        page = self.client.get("/settings").text
        self.assertIn("find Codex. Open", page)
        self.assertNotIn("Claude is signed out", page)

    def test_antigravity_g1_g2_g6(self):
        setup_steps.update_state(lambda value: value.update(setup_finished_at="2026-10-28T00:00:00+00:00"), self.home)
        with mock.patch.object(setup_steps, "find_antigravity", return_value=None):
            self.client.post("/settings/engine", data={"engine": "antigravity"}, headers=HTMX)
        settings = self.client.get("/settings").text
        self.assertIn("Antigravity stopped working.", settings)   # G1: the banner names Antigravity
        self.assertIn("Antigravity is not installed.", self.client.get("/").text)   # G1: Health shows its record
        self.signed_out()   # G2: a Claude wait stays hidden while Antigravity is chosen
        provider_limits.noted(provider_limits.ProviderUsageLimit(
            "x", kind=provider_limits.ANTIGRAVITY_SIGNED_OUT), self.home)
        page = self.client.get("/settings").text
        self.assertIn("Antigravity is signed out", page)
        self.assertIn('href="/settings#connection-antigravity"', page)
        self.assertNotIn("Claude is signed out", page)
        self.client.post("/settings/engine", data={"engine": "codex"}, headers=HTMX)
        self.assertNotIn("Antigravity is signed out", self.client.get("/settings").text)
        with mock.patch.object(setup_steps, "find_antigravity", return_value=None):
            self.client.post("/settings/engine", data={"engine": "antigravity"}, headers=HTMX)
        email = self.client.post("/settings/email", headers=HTMX).text   # G6: the email check names it
        self.assertIn("#connection-antigravity", email)
        self.assertIn("Antigravity", email)

    def test_g3_claude_sign_in_changes_clear_claude_waits(self):
        for change in (lambda: setup_steps.choose_claude_auth("subscription", self.home),
                       lambda: setup_steps.remove_claude_key(self.home)):
            self.signed_out()
            change()
            self.assertFalse((self.home / provider_limits.CLAUDE_WAIT_FILES[provider_limits.CLAUDE_SIGNED_OUT]).exists())


if __name__ == "__main__":
    unittest.main()
