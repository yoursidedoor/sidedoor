"""K1 (owner, 2026-11-13): when the text-AI key is refused on a real call (401 or 403), or the saved key
cannot be read, Overview's health row for that key says so with the same "Replace key" button, until a
call works again (a real one, or Settings' Save and test). An outage (network, timeout, server error) never
shows it: the key is not the problem then.

The row is read through `overview_page.gather`, the page's own producer; the calls are `text_model.complete`
with a fake transport. No network.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import desktop_alerts
import overview_page
import provider_limits
import setup_steps
import text_model
import user_profile
from test_dashboard_overview import CONFIG, database_with_schema, finished_home

ANSWER = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
WORKED = lambda url, data, headers, timeout: (200, ANSWER)


def status(code):
    return lambda url, data, headers, timeout: (code, b'{"error": {"message": "no", "code": %d}}' % code)


def raising(error):
    def transport(url, data, headers, timeout):
        raise error
    return transport


HTML = lambda code: (lambda url, data, headers, timeout: (code, b"<html><body><h1>Forbidden</h1></body></html>"))
MODERATION = lambda url, data, headers, timeout: (403, json.dumps({"error": {
    "code": 403, "message": "Input flagged", "metadata": {"reasons": ["x"], "provider_name": "p"}}}).encode())
IN_BODY_401 = lambda url, data, headers, timeout: (200, json.dumps({"error": {"code": 401, "message": "No auth credentials found"}}).encode())


class KeyHealthRow(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="key-health-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        finished_home(self.home)
        patched = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        self.database = database_with_schema(self.root)
        self.addCleanup(self.database.close)
        (self.root / "key").write_text("sk-synthetic-key-0000")
        self.config = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(self.root / "key")}, "model": "m"}

    def call(self, transport, **extra):
        try:
            text_model.complete(self.config, "prompt", transport=transport, timeout=5, **extra)
        except Exception as error:
            return error
        return None

    def row(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            view = overview_page.gather(self.database, CONFIG, self.home, setup_steps.progress(self.home), False)
        [item] = [item for item in view["health"] if item["name"] == "OpenRouter"]
        return item, view

    def test_a_refused_key_shows_on_the_row_and_a_working_call_clears_it(self):
        self.assertEqual(self.row()[0]["text"], "Key works")
        for refusal in (status(401), IN_BODY_401):                           # the provider's own invalid-credentials error
            self.assertIsInstance(self.call(refusal), text_model.TextModelHTTPError)
            item, view = self.row()
            self.assertEqual((item["tone"], item["text"], item["state"]), ("error", "Key not accepted", "key_refused"))
            self.assertEqual(item["fix"], {"label": "Replace key", "href": "/settings#connections"})
            self.assertIn(item, view["alerts"])                              # repeated under the Overview title, as any red row
            self.assertIsNone(provider_limits.wait_of(self.call(refusal)))   # not a provider wait: the failure is what it was
            self.assertIsNone(self.call(WORKED))                             # the next call that works
            self.assertEqual(self.row()[0]["text"], "Key works")
            self.assertIsNone(provider_limits.key_problem(self.home))

    def test_a_key_that_cannot_be_read_shows_too(self):
        (self.root / "key").unlink()
        self.assertIsInstance(self.call(WORKED), OSError)
        item, _ = self.row()
        self.assertEqual((item["tone"], item["text"], item["state"]), ("error", "Key can't be read", "key_unreadable"))
        (self.root / "key").write_text("sk-synthetic-key-0000")
        self.assertIsNone(self.call(WORKED))
        self.assertEqual(self.row()[0]["text"], "Key works")

    def test_an_outage_or_another_refusal_never_shows_it(self):
        for name, transport in (("a 403 in the provider's own JSON", status(403)), ("a moderation 403", MODERATION),
                                ("a 403 page that is not the provider's", HTML(403)),
                                ("a 401 page that is not the provider's", HTML(401)),
                                ("a 401 with no message", lambda *a: (401, b'{"error": {"code": 401}}')),
                                ("a server error", status(503)), ("a bad request", status(400)),
                                ("out of credit", status(402)), ("a timeout", raising(TimeoutError("t"))),
                                ("the network", raising(ConnectionResetError("reset")))):
            self.assertIsNotNone(self.call(transport), name)
            self.assertIsNone(provider_limits.key_problem(self.home), name)
            self.assertNotIn(self.row()[0]["text"], provider_limits.KEY_PROBLEM_WORDS.values(), name)
            provider_limits.clear(provider_limits.CREDIT_WAIT_FILE, self.home)

    def test_a_key_being_tested_records_nothing_and_a_passing_test_clears_the_row(self):
        self.assertIsInstance(self.call(status(401), api_key="sk-or-v1-under-test"), text_model.TextModelHTTPError)
        self.assertIsNone(provider_limits.key_problem(self.home))            # Setup's own message covers a key under test
        self.call(status(401))
        self.assertEqual(self.row()[0]["text"], "Key not accepted")
        # Settings' Save and test, passing: the same call with the pasted key.
        setup_steps.save_and_test_openrouter("sk-or-v1-synthetic-0000", self.config, self.home, transport=WORKED, to_file=True)
        self.assertEqual(self.row()[0]["text"], "Key works")

    def test_the_row_drives_the_desktop_notification_only_through_the_existing_health_rule(self):
        self.call(status(401))
        conditions = desktop_alerts.health_conditions(self.row()[1]["health"])
        self.assertEqual([c["key"] for c in conditions if ":OpenRouter:" in c["key"] or "OpenRouter" in c["key"]],
                         ["health:OpenRouter:error:key_refused"])


if __name__ == "__main__":
    unittest.main()
