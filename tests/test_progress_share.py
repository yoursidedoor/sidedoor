"""E12.T2 / E12.T5 in the app: progress counts go with the heartbeat while the progress box shows
on (the saved choice; before one, on until Start is done), and the relay's signed "update required" flag shows the update banner,
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

    def test_off_once_start_is_done_nothing_sent_and_on_sends_the_three_dates(self):
        self.activate()
        setup_steps.record(self.home, "start", "done", "Sidedoor is running.")   # Start done, no choice: off
        self.assertFalse(progress_share.enabled(self.home))
        self.assertFalse(progress_share.shown(self.home))
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

    def test_before_start_the_heartbeat_sends_what_the_box_shows(self):
        # Review 2026-11-10 (2): the box shows on while Start is pending, so the heartbeat sends
        # milestones, not the opt-out; a saved off still sends the opt-out.
        self.activate()
        self.assertNotIn(progress_share.SETTING, setup_steps.load_state(self.home))
        self.assertTrue(progress_share.shown(self.home))
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.sent()["milestones"], {"setup_finished": None, "first_drafted": None, "first_sent": None})
        self.assertNotIn("milestones_opt_out", self.sent())
        progress_share.set_enabled(False, self.home)
        license_clock._last_check["mono"] = None
        self.assertEqual(self.heartbeat(), "ok")
        self.assertNotIn("milestones", self.sent())
        self.assertIs(self.sent()["milestones_opt_out"], True)

    def test_after_reset_setup_file_the_box_shows_on_and_sends_on_until_a_choice(self):
        # Review 2026-11-10 (3): [Reset setup file] re-derives the steps without Start, so Start reads
        # pending again and the heartbeat sends milestones until the user chooses.
        self.activate()
        progress_share.set_enabled(False, self.home)                     # the lost file held a choice
        setup_steps.record(self.home, "start", "done", "Sidedoor is running.")
        (self.home / "profile.json").write_text(json.dumps({"answers": {"email": {"value": "alex@example.com"}}}))
        root = self.home / "root"
        database = paths.database(root)
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as write:
            write.execute("CREATE TABLE runs(slot TEXT)")
            write.execute("INSERT INTO runs VALUES ('s')")
        broken = self.home / setup_steps.STATE_FILE
        broken.write_text("{not json")
        with mock.patch.object(setup_steps, "find_codex", return_value=None):
            result = setup_steps.reset_state(self.home, root, run=lambda *a, **k: None)
        self.assertTrue(result["reset"])
        state = setup_steps.load_state(self.home)
        self.assertEqual(state["email_lock_basis"], "PROFILE_EMAIL_AFTER_RESET")
        self.assertNotIn("start", state["steps"])
        self.assertNotIn(progress_share.SETTING, state)
        self.assertTrue(progress_share.shown(self.home))
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.sent()["milestones"]["setup_finished"], state["setup_finished_at"][:10])
        self.assertNotIn("milestones_opt_out", self.sent())
        progress_share.set_enabled(False, self.home)
        self.assertFalse(progress_share.shown(self.home))
        license_clock._last_check["mono"] = None
        self.assertEqual(self.heartbeat(), "ok")
        self.assertIs(self.sent()["milestones_opt_out"], True)
        self.assertNotIn("milestones", self.sent())

    def test_a_setup_file_that_cannot_be_read_shares_nothing_and_is_left_as_it_is(self):
        # Review round 4 (Low): a file that is there but cannot be read is not "no choice yet": off.
        self.activate()
        path = self.home / setup_steps.STATE_FILE
        for broken in (b"\xff\xfe not json", b"{not json", b"[]"):
            path.write_bytes(broken)
            self.assertFalse(progress_share.shown(self.home), broken)
            self.assertFalse(progress_share.enabled(self.home), broken)
            self.assertEqual(progress_share.heartbeat_fields(self.home), {"milestones_opt_out": True}, broken)
            with self.assertRaises(setup_steps.StateFileUnreadable):   # GL2: said, never written over
                progress_share.save_shown_if_unchosen(self.home)
            self.assertEqual(path.read_bytes(), broken)
        license_clock._last_check["mono"] = None
        self.assertEqual(self.heartbeat(), "ok")
        self.assertNotIn("milestones", self.sent())
        self.assertIs(self.sent()["milestones_opt_out"], True)

    def test_a_saved_value_that_is_not_true_or_false_shares_nothing_and_is_kept(self):
        self.activate()
        path = self.home / setup_steps.STATE_FILE
        for value in ("yes", 1, None, {"on": True}):
            path.write_text(json.dumps({"steps": {}, progress_share.SETTING: value}))   # Start still pending
            before = path.read_bytes()
            self.assertFalse(progress_share.shown(self.home), value)
            self.assertFalse(progress_share.enabled(self.home), value)
            self.assertEqual(progress_share.heartbeat_fields(self.home), {"milestones_opt_out": True}, value)
            progress_share.save_shown_if_unchosen(self.home)
            self.assertEqual(setup_steps.load_state(self.home)[progress_share.SETTING], value)
            self.assertEqual(json.loads(before)[progress_share.SETTING], value)

    def test_no_setup_file_yet_is_no_choice_yet_on_until_start_is_done(self):
        path = self.home / setup_steps.STATE_FILE
        path.unlink(missing_ok=True)
        self.assertTrue(progress_share.shown(self.home))                 # file absent, Start pending: on
        self.assertFalse(progress_share.enabled(self.home))              # nothing saved
        self.assertIn("milestones", progress_share.heartbeat_fields(self.home))
        progress_share.save_shown_if_unchosen(self.home)                 # Start saves what the box showed
        self.assertIs(setup_steps.load_state(self.home)[progress_share.SETTING], True)
        path.write_text(json.dumps({"steps": {"start": {"status": "done"}}}))   # key absent, Start done: off
        self.assertFalse(progress_share.shown(self.home))
        progress_share.save_shown_if_unchosen(self.home)
        self.assertNotIn(progress_share.SETTING, setup_steps.load_state(self.home))

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
        import setup_steps
        setup_steps.record(self.home, "start", "done", "Sidedoor is running.")   # Start done: off until turned on
        page = self.client.get("/settings").text
        self.assertIn(progress_share.LABEL, page)
        self.assertIn('id="s-share-progress">', page)               # not checked
        self.assertIn('<p class="muted choice-help" id="s-share-progress-help">%s</p>' % progress_share.HELP, page)   # M3
        low = progress_share.HELP.lower()
        for word in ("\u2014", "\u2013", "heartbeat", "milestone", "automation", "_", "relay", "license", "utc"):
            self.assertNotIn(word, low)
        self.assertEqual(low.count("the day"), len(progress_share.milestones(self.home)))   # one date named per date sent
        answer = self.client.post("/settings/progress", data={"enabled": "on"}, headers=settings_base.HTMX)
        self.assertEqual(answer.status_code, 200)
        self.assertIn('id="s-share-progress" checked', answer.text)
        self.assertTrue(progress_share.enabled(self.home))
        self.client.post("/settings/progress", data={}, headers=settings_base.HTMX)
        self.assertFalse(progress_share.enabled(self.home))


if __name__ == "__main__":
    unittest.main()
