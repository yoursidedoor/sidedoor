"""E12.T2 / E12.T5 in the app: progress counts go with the heartbeat only while the Settings toggle
is on (off by default), and the relay's signed "update required" flag shows the update banner,
while one with a bad signature is ignored. Offline: every relay call is a fake."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sqlite3
import unittest
from unittest import mock

import license_clock
import license_fixtures
import paths
import progress_share
import setup_steps
import update_check
from test_license_clock import ClockCase


class ProgressShare(ClockCase):
    def sent(self):
        return [body for url, body in self.relay.calls if url.endswith("/license/heartbeat")][-1]

    def test_off_by_default_nothing_sent_and_on_sends_the_three_dates(self):
        self.activate()
        self.assertFalse(progress_share.enabled(self.home))
        self.assertEqual(self.heartbeat(), "ok")
        body = self.sent()
        self.assertNotIn("milestones", body)
        self.assertEqual(set(body), {"key", "machine", "version", "milestones_opt_out"})   # L9: off says so
        self.assertIs(body["milestones_opt_out"], True)

        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-11-01T09:00:00Z"), self.home)
        database = paths.database(home=self.home)
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as write:
            write.execute("CREATE TABLE engagement_events (source TEXT, kind TEXT, occurred_at TEXT)")
            write.executemany("INSERT INTO engagement_events VALUES (?, ?, ?)",
                              [("unipile", "initial_sent", "2026-11-04T10:00:00+00:00"),
                               ("heyreach", "initial_sent", "2026-11-03T08:00:00+00:00"),
                               ("linkedin-browser", "initial_sent", "2026-06-01T08:00:00+00:00"),   # L8: imported
                               ("unipile", "inbound", "2026-11-02T08:00:00+00:00")])
        self.assertEqual(self.heartbeat(), "ok")
        self.assertNotIn("milestones", self.sent())          # still off

        progress_share.set_enabled(True, self.home)
        license_clock._last_check["mono"] = None
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.sent()["milestones"], {"setup_finished": "2026-11-01", "first_drafted": None,
                                                     "first_sent": "2026-11-03"})
        progress_share.set_enabled(False, self.home)
        self.assertEqual(self.heartbeat(), "ok")
        self.assertNotIn("milestones", self.sent())
        self.assertIs(self.sent()["milestones_opt_out"], True)

    def test_an_unreadable_database_leaves_its_dates_out(self):
        # M2: a failed read is not "not reached": the two database dates are left out, not sent as null.
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-11-01T09:00:00Z"), self.home)
        database = paths.database(home=self.home)
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(b"not a database" * 100)
        self.assertEqual(progress_share.milestones(self.home), {"setup_finished": "2026-11-01"})


class UpdateFlag(ClockCase):
    def flagged_relay(self, minimum):
        def post(url, body, timeout=None):
            if url.endswith("/license/activate"):
                return self.relay(url, body)
            return 200, json.dumps(license_fixtures.signed(body["key"], body["machine"], self.true,
                                                           update_required=True, min_version=minimum)).encode()
        return post

    def test_signed_flag_shows_the_update_banner_and_a_bad_signature_is_ignored(self):
        self.activate()
        self.assertIsNone(update_check.banner(self.home))
        with mock.patch.object(update_check, "installed_version", return_value="0.4.0"):
            license_clock.check(self.home, "https://relay.test", post=self.flagged_relay("0.5.0"), now=self.local)
            found = update_check.banner(self.home)
            self.assertEqual((found["text"], found["action_href"], found["dismiss"], found["update_phase"]),
                             ("Please update Sidedoor to version 0.5.0 or newer.", update_check.DOWNLOAD_PAGE,
                              "0.5.0", "available"))
            # A tampered entitlement (the flag added after signing) is not trusted: no banner.
            stored_file = self.home / setup_steps.ENTITLEMENT_FILE
            stored = json.loads(stored_file.read_text())
            claims = json.loads(stored["entitlement"])
            claims["min_version"] = "0.6.0"
            stored["entitlement"] = json.dumps(claims, sort_keys=True, separators=(",", ":"))
            stored_file.write_text(json.dumps(stored))
            self.assertIsNone(update_check.banner(self.home))
            # An install already at the minimum shows nothing.
            license_clock._last_check["mono"] = None
            license_clock.check(self.home, "https://relay.test", post=self.flagged_relay("0.4.0"), now=self.local)
            self.assertIsNone(update_check.banner(self.home))


import test_settings_page as settings_base


@unittest.skipUnless(settings_base.DEPENDENCIES, "fastapi/httpx not installed")
class SettingsToggle(settings_base.SettingsPageTest):
    """Only this test of the Settings page fixture runs here."""

    def run(self, result=None):   # the inherited Settings tests run in their own module
        if self._testMethodName != "test_toggle_off_by_default_and_saved_on":
            return None
        return super().run(result)

    def test_toggle_off_by_default_and_saved_on(self):
        page = self.client.get("/settings").text
        self.assertIn(progress_share.LABEL, page)
        self.assertIn('id="s-share-progress">', page)               # not checked
        answer = self.client.post("/settings/progress", data={"enabled": "on"}, headers=settings_base.HTMX)
        self.assertEqual(answer.status_code, 200)
        self.assertIn('id="s-share-progress" checked', answer.text)
        self.assertTrue(progress_share.enabled(self.home))
        self.client.post("/settings/progress", data={}, headers=settings_base.HTMX)
        self.assertFalse(progress_share.enabled(self.home))


if __name__ == "__main__":
    unittest.main()
