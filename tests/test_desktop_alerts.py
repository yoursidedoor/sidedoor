"""E15.T3: desktop notifications and the tray count. Nothing real is ever posted here: the OS call is
a list the test reads, the clock is the test's, and the tray is pystray faked."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import datetime as dt
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import desktop_alerts
import notifications
import overview_page
import tray

DASHBOARD = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DASHBOARD:
    import dashboard
    import test_settings_page as settings_base

START = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.timezone.utc)
HOUR, DAY = dt.timedelta(hours=1), dt.timedelta(days=1)
POSTED = []   # every notification any test posted, for the wording check at the end


def red(name="LinkedIn", text="LinkedIn needs you to sign in again", href="/settings#connections"):
    return overview_page._item(name, "error", text, fix={"label": "Fix", "href": href} if href else None)


def amber(name="Browser profile", text="Email check not done yet"):
    return overview_page._item(name, "warning", text, fix={"label": "Open Settings", "href": "/settings#connections"})


NO_NEEDS = dict.fromkeys((row[0] for row in overview_page.NEEDS_ROWS), 0)


class Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.now = START
        self.items, self.needs, self.extra = [], dict(NO_NEEDS), []
        self.posted = []
        desktop_alerts._acted.clear()
        self.watcher = self.make()

    def make(self, tray=True, combine_over=2):
        def post(title, text, href):
            self.posted.append((title, text, href))
            POSTED.append((title, text))
            return True
        return desktop_alerts.Watcher(self.home, provider=notifications.Desktop(post=post if tray else None),
                                      clock=lambda: self.now, combine_over=combine_over,
                                      gather=lambda now: {"conditions": desktop_alerts.health_conditions(self.items) + self.extra,
                                                          "needs": self.needs})

    def tick(self, after=dt.timedelta(minutes=1)):
        self.now += after
        before = len(self.posted)
        self.watcher.tick()
        return self.posted[before:]

    def known(self):
        """An install that already has its state: later ticks are ordinary ones."""
        self.assertEqual(self.tick(), [])


class Rules(Case):
    def test_first_run_after_upgrade_posts_nothing_then_the_red_item_gets_its_daily_reminder(self):
        self.items = [red(), red("License", "Your license has expired", "/settings#license"), amber()]
        self.needs = dict(NO_NEEDS, questions=4, messages=2)
        self.assertEqual(self.tick(), [])                    # no burst
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.watcher.total, 6)
        later = self.tick(DAY)
        self.assertEqual(sorted(title for title, _, _ in later), ["License", "LinkedIn"])   # red only, once

    def test_rule_1_once_when_it_starts_then_one_reminder_a_day_while_red(self):
        self.known()
        self.items = [red()]
        self.assertEqual(self.tick(), [("LinkedIn", "LinkedIn needs you to sign in again.", "/settings#connections")])
        for _ in range(20):
            self.assertEqual(self.tick(HOUR), [])            # while it lasts: nothing
        self.assertEqual(len(self.tick(4 * HOUR)), 1)        # a day on: one reminder
        self.assertEqual(self.tick(HOUR), [])
        self.assertEqual(len(self.tick(DAY)), 1)

    def test_rule_2_amber_once_and_never_again(self):
        self.known()
        self.items = [amber()]
        self.assertEqual(len(self.tick()), 1)
        for _ in range(5):
            self.assertEqual(self.tick(DAY), [])

    def test_amber_turning_red_notifies_at_once(self):
        self.known()
        self.items = [amber("License", "Your trial ends in 2 days")]
        self.assertEqual(len(self.tick()), 1)
        self.items = [red("License", "Your trial has ended")]
        self.assertEqual([text for _, text, _ in self.tick()], ["Your trial has ended."])
        self.assertEqual(self.tick(), [])

    # --- review round 1 ---

    def test_u1_a_reminder_due_at_relaunch_waits_for_the_tray_and_is_posted_once(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        saved = desktop_alerts.load(self.home)
        with mock.patch.object(notifications, "_desktop_post", None):
            self.watcher = self.make(tray=False)             # relaunched 25 hours on; the tray is not up yet
            self.assertEqual(self.tick(25 * HOUR), [])
            self.assertEqual(desktop_alerts.load(self.home), saved)   # nothing marked
        self.watcher = self.make()                           # the tray is ready
        self.assertEqual(len(self.tick()), 1)                # the reminder
        self.assertEqual(self.tick(), [])                    # once

    def test_u1_with_no_tray_nothing_is_marked(self):
        self.known()
        with mock.patch.object(notifications, "_desktop_post", None):
            self.watcher = self.make(tray=False)
            self.items = [red(), amber()]
            self.extra = [{"key": "update:9.9.9", "tone": "info", "title": "Your Sidedoor", "text": "Sidedoor 9.9.9 is available.",
                           "href": None, "once": True}]
            self.needs = dict(NO_NEEDS, questions=2)
            before = desktop_alerts.load(self.home)
            for _ in range(3):
                self.assertEqual(self.tick(DAY), [])
            self.assertEqual(desktop_alerts.load(self.home), before)
        self.watcher = self.make()
        self.assertEqual(self.tick(), [("Your Sidedoor", "4 things need attention in Sidedoor", "/")])

    def test_u1_the_watcher_waits_for_a_way_to_post_before_its_first_check(self):
        checks = []
        provider = mock.Mock()
        provider.available.return_value = False
        watcher = desktop_alerts.Watcher(self.home, provider=provider, gather=lambda now: checks.append(now))
        with mock.patch.object(desktop_alerts, "WAIT_SECONDS", 0.01):
            watcher.start()
            self.addCleanup(watcher.stop)
            import time
            time.sleep(0.1)
            self.assertEqual(checks, [])
            self.assertIsNone(desktop_alerts.load(self.home))

    def test_u2_a_check_never_touches_the_browser_lock_or_a_process(self):
        import browser_runtime
        import setup_steps
        import test_dashboard_overview as overview_base
        root = self.home / "root"
        root.mkdir()
        home = root / "home"
        overview_base.finished_home(home)
        database = overview_base.database_with_schema(root)
        self.addCleanup(database.close)
        names = ("display_state", "close_orphan", "clear_stale_lock", "profile_in_use", "session_holds_lock",
                 "guard_owns_profile", "unavailable")
        with contextlib.ExitStack() as stack:
            spies = [stack.enter_context(mock.patch.object(browser_runtime, name)) for name in names
                     if hasattr(browser_runtime, name)]
            stack.enter_context(mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"))
            stack.enter_context(mock.patch.object(overview_page, "snapshot", lambda path, use, read: use(database)))
            missing = stack.enter_context(mock.patch.object(browser_runtime, "missing", return_value=["node"]))
            found = desktop_alerts.gather(home, root, None, overview_base.pacific(2026, 10, 22, 8, 30), config=overview_base.CONFIG)
        self.assertGreaterEqual(len(spies), 5)
        for spy in spies:
            spy.assert_not_called()
        missing.assert_called()
        self.assertIn("health:Browser profile:error:RUNTIME_MISSING", [c["key"] for c in found["conditions"]])

    def test_u3_an_unrelated_action_quiets_nothing(self):
        self.known()
        desktop_alerts.user_acted("/approvals/messages/approve", self.now + dt.timedelta(seconds=20))
        desktop_alerts.user_acted("/pause", self.now + dt.timedelta(seconds=20))
        self.items = [amber()]
        self.needs = dict(NO_NEEDS, questions=2)
        self.assertEqual(sorted(text for _, text, _ in self.tick()), ["2 questions to answer", "Email check not done yet."])

    def test_u3_the_window_never_skips_a_reminder(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        self.now += DAY - dt.timedelta(minutes=1)
        desktop_alerts.user_acted("/settings/linkedin/reconnect", self.now + dt.timedelta(seconds=30))
        self.assertEqual(len(self.tick()), 1)

    def test_u3_routes_name_the_lines_they_can_change(self):
        for path, item in (("/pause", "Scheduler"), ("/resume", "Scheduler"), ("/start", "Scheduler"),
                           ("/settings/license/check", "License"), ("/license/check", "License"),
                           ("/settings/linkedin/reconnect", "LinkedIn"), ("/settings/openrouter", "OpenRouter"),
                           ("/settings/claude/key", "engine"), ("/settings/codex/auth", "engine"), ("/settings/engine", "engine"),
                           ("/settings/browser/open", "Browser profile"), ("/settings/webmail", "Browser profile"),
                           ("/setup-file/reset", "banner:setup-file"), ("/setup/mode", "setup")):
            desktop_alerts._acted.clear()
            desktop_alerts.user_acted(path, self.now)
            self.assertIn(item, desktop_alerts._user_caused(self.now), path)
        for path in ("/approvals/questions/answer", "/settings/profile", "/settings/notifications", "/update/dismiss"):
            desktop_alerts._acted.clear()
            desktop_alerts.user_acted(path, self.now)
            self.assertEqual(desktop_alerts._user_caused(self.now), set(), path)
        codex = dict(red("Codex", "Codex stopped working"), state="stopped:failed")
        self.assertEqual(desktop_alerts.health_conditions([codex])[0]["item"], "engine")

    def scheduler(self, **loop):
        return overview_page.scheduler_item(dict({"running": True}, **loop), None, {}, lambda moment: "10:02")

    def test_u4_a_schedule_problem_joining_notifies_once_and_a_second_job_does_not(self):
        self.known()
        self.items = [self.scheduler(job_failures={"research": {"at": None}})]
        self.assertEqual(len(self.tick()), 1)
        self.items = [self.scheduler(job_failures={"research": {"at": None}, "applications": {"at": None}})]
        self.assertEqual(self.tick(), [])                    # K1: which jobs is not part of the condition
        self.items = [self.scheduler(job_failures={"research": {"at": None}, "applications": {"at": None}}, last_error="x")]
        self.assertEqual(len(self.tick()), 1)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(DAY), [])                 # amber: no reminder

    def test_u4_license_amber_to_amber_and_red_to_red_notify_once_on_the_change(self):
        self.known()
        self.items = [dict(amber("License", "Your trial ends in 2 days"), state="trial:")]
        self.assertEqual(len(self.tick()), 1)
        self.items = [dict(amber("License", "Your trial ends tomorrow"), state="trial:")]   # only the words moved
        self.assertEqual(self.tick(DAY), [])
        self.items = [dict(amber("License", "Your payment didn't go through"), state="payment_retrying:")]
        self.assertEqual([text for _, text, _ in self.tick()], ["Your payment didn't go through."])
        self.assertEqual(self.tick(), [])
        self.items = [dict(red("License", "Your license has expired"), state="expired:")]
        self.assertEqual(len(self.tick()), 1)
        self.items = [dict(red("License", "This key is in use on another computer"), state="license_problem:machine")]
        self.assertEqual([text for _, text, _ in self.tick()], ["This key is in use on another computer."])
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.tick(DAY)), 1)             # red: its reminder

    def test_u5_windows_posts_one_notification_for_two_due_together(self):
        self.watcher = self.make(combine_over=1)
        self.known()
        self.items = [red(), amber()]
        self.assertEqual(self.tick(), [("Your Sidedoor", "2 things need attention in Sidedoor", "/")])
        self.items = [red(), amber(), red("OpenRouter", "Out of credits")]
        self.assertEqual([title for title, _, _ in self.tick()], ["OpenRouter"])   # one alone keeps its own page
        with mock.patch.object(desktop_alerts.os, "name", "nt"):
            self.assertEqual(desktop_alerts.Watcher(self.home).combine_over, 1)

    def test_u6_things_need_you_counts_what_waits(self):
        self.known()
        self.needs = dict(NO_NEEDS, questions=2, messages=3, notes=1)
        self.assertEqual(self.tick(), [("Your Sidedoor", "6 things need you", "/")])
        self.assertEqual(self.watcher.total, 6)              # the tray's number
        self.assertEqual(tray.count_title(self.watcher.total), "Your Sidedoor: 6 things need you")

    def test_rule_3_three_arriving_together_are_one_notification(self):
        self.known()
        self.items = [red(), amber()]
        self.needs = dict(NO_NEEDS, questions=1)
        self.assertEqual(self.tick(), [("Your Sidedoor", "3 things need attention in Sidedoor", "/")])
        self.assertEqual(self.tick(), [])

    def test_two_arriving_together_stay_two(self):
        self.known()
        self.items = [red(), amber()]
        self.assertEqual(len(self.tick()), 2)

    def test_rule_4_the_users_own_pause_is_notified_only_after_seven_days(self):
        self.known()
        paused = (self.now + dt.timedelta(minutes=1)).isoformat()
        item = overview_page.scheduler_item({"running": True}, None, {"user_paused_at": paused}, lambda moment: "09:01")
        self.assertEqual(item["tone"], "warning")
        self.items = [item]
        self.assertEqual(self.tick(), [])
        for _ in range(6):
            self.assertEqual(self.tick(DAY), [])
        after = self.tick(DAY)                                # seven days on
        self.assertEqual(len(after), 1)
        self.assertIn("Paused by you", after[0][1])
        self.assertEqual(self.tick(DAY), [])                  # once
        # resumed and paused again: the week starts again
        again = dict(item, user_paused_at=self.now.isoformat())
        self.items = [again]
        self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.tick(7 * DAY)), 1)

    def test_rule_5_switch_off_posts_nothing_and_the_count_still_shows(self):
        self.known()
        self.assertTrue(desktop_alerts.enabled(self.home))   # on by default
        desktop_alerts.set_enabled(False, self.home)
        self.items = [red()]
        self.needs = dict(NO_NEEDS, notes=2)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.watcher.total, 2)
        desktop_alerts.set_enabled(True, self.home)
        self.assertEqual(self.tick(), [])                    # nothing saved up for later

    def test_rule_6_an_update_is_notified_once_per_version(self):
        self.known()

        def update(version):
            return {"key": "update:" + version, "tone": "info", "title": "Your Sidedoor",
                    "text": "Sidedoor %s is available." % version, "href": None, "once": True}
        self.extra = [update("0.5.0")]
        self.assertEqual(self.tick(), [("Your Sidedoor", "Sidedoor 0.5.0 is available.", "/")])
        self.assertEqual(self.tick(DAY), [])
        self.extra = []                                       # dismissed, then offered again
        self.tick()
        self.extra = [update("0.5.0")]
        self.assertEqual(self.tick(DAY), [])
        self.extra = [update("0.5.1")]
        self.assertEqual(len(self.tick()), 1)

    def test_rule_7_a_click_opens_the_items_fix_page_or_overview(self):
        self.known()
        self.items = [red(href="/settings#connections")]
        self.assertEqual(self.tick()[0][2], "/settings#connections")
        self.items = [red("Scheduler", "Paused because of your license", None)]   # a button, no link
        self.assertEqual(self.tick()[0][2], "/")
        self.needs = dict(NO_NEEDS, applications=1)
        self.assertEqual(self.tick(), [("Needs you", "1 application blocked on you", "/applications?show=needs_you")])

    def test_count_up_notifies_and_count_down_does_not(self):
        self.known()
        self.needs = dict(NO_NEEDS, questions=1)
        self.assertEqual(self.tick(), [("Needs you", "1 question to answer", "/approvals#questions")])
        self.assertEqual(self.tick(), [])                    # unchanged
        self.needs = dict(NO_NEEDS, questions=3)
        self.assertEqual([text for _, text, _ in self.tick()], ["3 questions to answer"])
        self.needs = dict(NO_NEEDS, questions=2)
        self.assertEqual(self.tick(), [])                    # down
        self.needs = dict(NO_NEEDS, questions=3)
        self.assertEqual(len(self.tick()), 1)                # and up again
        self.assertEqual(self.watcher.total, 3)

    def test_every_needs_you_kind_notifies_and_a_new_kind_starts_quietly(self):
        self.known()
        for key, one, _many, href, _label in overview_page.NEEDS_ROWS:
            self.needs = dict(NO_NEEDS, **{key: 1})
            self.assertEqual(self.tick(), [("Needs you", "1 " + one, href)])
        # a kind added by a later version: one row; counts found at upgrade are known (E15 join: the
        # sign-in cards' own row, `sign_ins`, is now one of NEEDS_ROWS and is covered by the loop above)
        rows = overview_page.NEEDS_ROWS + (("later_kind", "thing to look at", "things to look at", "/approvals", "Open"),)
        state, notes = desktop_alerts.evaluate(desktop_alerts.load(self.home), [], dict(self.needs, later_kind=2),
                                               self.now, rows=rows)
        self.assertEqual(notes, [])
        _, notes = desktop_alerts.evaluate(state, [], dict(self.needs, later_kind=3), self.now, rows=rows)
        self.assertEqual([note["text"] for note in notes], ["3 things to look at"])

    def test_red_then_green_then_red_again_notifies_again_after_a_day(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        self.items = [overview_page._item("LinkedIn", "success", "Connected")]
        self.assertEqual(self.tick(), [])
        self.items = [red()]
        self.assertEqual(self.tick(HOUR), [])                # K1: back within the day: said already
        self.assertEqual(len(self.tick(DAY)), 1)             # still red a day after the post: its reminder
        self.items = []
        self.assertEqual(self.tick(DAY), [])
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)                # a day after the last post: new again

    # --- review round 2 ---

    def day_of_minutes(self, items_at):
        """Posts per line over 24 hours of minute checks; `items_at(minute)` is what Overview shows then."""
        self.known()
        start = len(self.posted)
        for minute in range(24 * 60):
            self.items = items_at(minute)
            self.tick()
        return [title for title, _, _ in self.posted[start:]]

    def test_k1_a_flapping_job_is_announced_once_in_a_day(self):
        def flapping(minute):   # research fails and succeeds in turn, every five minutes
            return [self.scheduler(job_failures={"research": {"at": None}} if (minute // 5) % 2 == 0 else {})]
        self.assertEqual(self.day_of_minutes(flapping), ["Scheduler"])   # 144 before

    def test_k1_a_job_flapping_beside_one_that_stays_failed_is_announced_once(self):
        def both(minute):
            failed = {"run": {"at": None}}
            if (minute // 5) % 2 == 0:
                failed["research"] = {"at": None}
            return [self.scheduler(job_failures=failed)]
        self.assertEqual(self.day_of_minutes(both), ["Scheduler"])       # 288 before
        self.assertEqual(self.scheduler(job_failures={"run": {}, "research": {}})["state"], "running|job")

    def test_k1_a_red_line_flapping_gets_at_most_one_post_and_one_reminder(self):
        def flapping(minute):
            return [red()] if (minute // 5) % 2 == 0 else []
        self.assertLessEqual(len(self.day_of_minutes(flapping)), 2)

    def test_k1_two_states_of_one_line_alternating_are_each_said_once(self):
        def alternating(minute):
            state = "claude:signed_out" if (minute // 5) % 2 == 0 else "claude:not_found"
            return [dict(red("Claude", "Claude needs you"), state=state)]
        self.assertEqual(self.day_of_minutes(alternating), ["Claude", "Claude"])

    # --- review round 3 ---

    def test_k6_a_different_problem_on_a_line_that_went_green_is_announced_at_once(self):
        for tone in (red, amber):
            self.known()
            self.items = [dict(tone("License", "Your license has expired"), state="expired:")]
            self.assertEqual(len(self.tick()), 1)
            self.items = []
            self.assertEqual(self.tick(HOUR), [])
            self.items = [dict(tone("License", "This key is in use on another computer"), state="license_problem:machine")]
            self.assertEqual([text for _, text, _ in self.tick(6 * HOUR)], ["This key is in use on another computer."])
            self.assertEqual(self.tick(), [])
            self.items = []
            self.tick(2 * DAY)

    def test_k6_three_states_of_one_line_in_turn_are_each_said_once_a_day(self):
        def turning(minute):
            return [dict(red("Claude", "Claude needs you"), state="claude:%d" % ((minute // 5) % 3))]
        self.assertEqual(len(self.day_of_minutes(turning)), 3)

    def test_k7_an_amber_problem_kept_back_is_said_once_more_when_the_day_is_up(self):
        self.known()
        self.items = [amber()]
        self.assertEqual(len(self.tick()), 1)                # 09:02
        self.items = []
        self.assertEqual(self.tick(HOUR), [])
        self.items = [amber()]
        self.assertEqual(self.tick(6 * HOUR), [])            # back within the day: kept back
        self.assertEqual(self.tick(16 * HOUR), [])
        self.assertEqual(len(self.tick(2 * HOUR)), 1)        # a day after the first post, still showing: once more
        for _ in range(7):
            self.assertEqual(self.tick(DAY), [])             # then silent, as amber is

    def test_k8_a_refused_post_is_logged_once_per_run_of_failures(self):
        self.known()
        real = self.watcher.provider.post
        refusing = [True]
        self.watcher.provider = notifications.Desktop(post=lambda *a: False if refusing[0] else real(*a))
        self.items = [amber()]
        with self.assertLogs(desktop_alerts.LOG, "INFO") as logs:
            for _ in range(180):
                self.assertEqual(self.tick(), [])            # still tried at each check
            refusing[0] = False
            self.assertEqual(len(self.tick()), 1)
            self.assertEqual(self.tick(), [])
        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "INFO"])

    def test_k1_a_different_line_still_notifies_at_once(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        self.items = [red(), red("OpenRouter", "Out of credits")]
        self.assertEqual([title for title, _, _ in self.tick()], ["OpenRouter"])
        self.items = [red(), red("OpenRouter", "Out of credits"), amber()]
        self.assertEqual([title for title, _, _ in self.tick()], ["Browser profile"])

    def test_k1_the_daily_reminder_still_comes_while_it_stays_red(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        self.assertEqual(len(self.tick(DAY)), 1)
        self.assertEqual(len(self.tick(DAY)), 1)

    def test_k2_removing_saved_keys_and_license_actions_quiet_their_consequences(self):
        def caused(path):
            desktop_alerts._acted.clear()
            desktop_alerts.user_acted(path, self.now)
            return desktop_alerts._user_caused(self.now)
        self.assertEqual(caused("/settings/keys/remove"), {"License", "OpenRouter", "Scheduler"})
        for path in ("/license/check", "/settings/license/check", "/settings/license/activate", "/settings/license/change",
                     "/setup/license"):
            self.assertEqual(caused(path), {"License", "Scheduler"}, path)
        self.assertEqual(caused("/data/move"), {"Scheduler"})
        self.assertEqual(caused("/setup/start"), {"Scheduler"})
        self.assertLessEqual({"License", "LinkedIn", "OpenRouter", "engine", "Browser profile", "Scheduler"},
                             caused("/setup-file/reset"))
        self.known()
        desktop_alerts.user_acted("/settings/keys/remove", self.now + dt.timedelta(seconds=10))
        self.items = [red("OpenRouter", "Saved keys removed"),
                      overview_page.scheduler_item({"running": True}, None, {"license_paused_at": "2026-11-12T09:00:00+00:00"}, str),
                      dict(amber("License", "No license yet"), state="no_license")]
        self.assertEqual(self.tick(), [])

    def test_k3_a_post_that_fails_is_tried_again_at_the_next_check_once(self):
        self.known()
        answers = [False, False]
        real = self.watcher.provider.post
        self.watcher.provider = notifications.Desktop(post=lambda *a: answers.pop() if answers else real(*a))
        self.items = [amber()]
        self.extra = [{"key": "update:9.9.9", "tone": "info", "title": "Your Sidedoor", "text": "Sidedoor 9.9.9 is available.",
                       "href": None, "once": True}]
        self.assertEqual(self.tick(), [])                    # the tray was stopping: neither was shown
        self.assertEqual([text for _, text, _ in self.tick()], ["Email check not done yet.", "Sidedoor 9.9.9 is available."])
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(DAY), [])                 # once each

    def test_k3_a_failed_count_and_a_failed_reminder_are_tried_again(self):
        self.known()
        self.items = [red()]
        self.assertEqual(len(self.tick()), 1)
        real = self.watcher.provider.post
        answers = [False, False]
        self.watcher.provider = notifications.Desktop(post=lambda *a: answers.pop() if answers else real(*a))
        self.needs = dict(NO_NEEDS, questions=2)
        self.assertEqual(self.tick(DAY), [])                 # the reminder and the count both failed
        self.assertEqual(sorted(title for title, _, _ in self.tick()), ["LinkedIn", "Needs you"])
        self.assertEqual(self.tick(), [])

    def test_k4_getting_better_is_not_announced_and_getting_worse_is(self):
        self.known()
        self.items = [dict(red("License", "Payment failed"), state="expired:")]
        self.assertEqual(len(self.tick()), 1)
        self.items = [dict(amber("License", "Payment retrying"), state="payment_retrying:")]
        self.assertEqual(self.tick(), [])                    # red to amber
        self.assertEqual(self.tick(DAY), [])
        self.items = [dict(red("License", "Your license has expired"), state="grace_spent:")]
        self.assertEqual(len(self.tick()), 1)                # amber to red
        self.items = []
        self.tick()
        self.items = [amber("OpenRouter", "Key not checked")]
        self.assertEqual(len(self.tick()), 1)                # green to amber

    def test_a_restart_between_ticks_does_not_repeat(self):
        self.known()
        self.items = [red()]
        self.needs = dict(NO_NEEDS, messages=2)
        self.assertEqual(len(self.tick()), 2)
        self.watcher = self.make()                           # the app was quit and opened again
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(HOUR), [])
        self.assertEqual(len(self.tick(DAY)), 1)             # the red item's reminder, still on its day

    def test_what_the_user_just_did_in_the_dashboard_is_not_notified(self):
        self.known()
        self.items = [red("OpenRouter", "The key was refused")]
        desktop_alerts.user_acted("/settings/openrouter", self.now + dt.timedelta(seconds=30))   # they saved a key in Settings
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(HOUR), [])
        self.assertEqual(len(self.tick(DAY)), 1)             # still red a day later: the reminder

    def test_setup_left_unfinished_is_notified_once_after_a_day(self):
        self.known()
        self.extra = [desktop_alerts._from_banner("setup", {"tone": "warning", "text": "Setup is not finished: 3 steps left.",
                                                             "action_href": "/setup"}, delay=desktop_alerts.SETUP_SECONDS)]
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(12 * HOUR), [])
        self.assertEqual(self.tick(12 * HOUR), [("Your Sidedoor", "Setup is not finished: 3 steps left.", "/setup")])
        self.assertEqual(self.tick(DAY), [])

    def test_no_tray_means_unavailable(self):
        with mock.patch.object(notifications, "_desktop_post", None):
            with self.assertRaises(notifications.Unavailable):
                notifications.Desktop().send_notification({"event_key": "k", "message": "m"})


class Sources(Case):
    def test_health_items_keep_their_name_tone_words_and_fix(self):
        scheduler = overview_page.scheduler_item({"running": True, "job_failures": {"research": {"at": None}},
                                                  "last_error": "TICK_FAILED: internal"}, None, {}, str)
        found = desktop_alerts.health_conditions([overview_page._item("LinkedIn", "success", "Connected"), red(), scheduler])
        self.assertEqual([c["key"] for c in found], ["health:LinkedIn:error:", "health:Scheduler:warning:running|schedule|job"])
        self.assertEqual(found[0]["href"], "/settings#connections")
        self.assertIn("Research last stopped with an error.", found[1]["text"])
        self.assertNotIn("TICK_FAILED", found[1]["text"])    # a note's detail never goes out
        self.assertNotIn("See what failed", found[1]["text"])

    def test_before_setup_ends_only_setup_itself(self):
        found = desktop_alerts.gather(self.home, self.home, None, self.now)
        self.assertIsNone(found["needs"])
        self.assertEqual([c["key"] for c in found["conditions"]], ["setup"])
        self.assertEqual(found["conditions"][0]["delay"], desktop_alerts.SETUP_SECONDS)
        self.assertEqual(found["conditions"][0]["href"], "/setup")

    def test_a_finished_install_reads_exactly_what_overview_shows(self):
        import setup_steps
        import test_dashboard_overview as overview_base
        root = self.home / "root"
        root.mkdir()
        home = root / "home"
        overview_base.finished_home(home, openrouter=("failed", "OpenRouter refused this key."))
        database = overview_base.database_with_schema(root)
        self.addCleanup(database.close)
        progress, now = setup_steps.progress(home), overview_base.pacific(2026, 10, 22, 8, 30)
        page = overview_page.gather(database, overview_base.CONFIG, home, progress, False, now=now)
        mine = overview_page.attention(database, overview_base.CONFIG, home, progress, False, now=now)
        self.assertEqual(mine["needs"], page["needs"])
        self.assertEqual(mine["health"], page["health"])
        found = {c["key"]: c for c in desktop_alerts.health_conditions(mine["health"])}
        self.assertEqual(found["health:OpenRouter:error:failed"]["text"], "OpenRouter refused this key.")
        self.assertEqual(found["health:OpenRouter:error:failed"]["href"], "/settings#connections")
        self.assertIn("health:Scheduler:error:stopped", found)   # not running: as the page says
        POSTED.extend((c["title"], c["text"]) for c in found.values())
        with mock.patch.object(overview_page, "snapshot", lambda path, use, read: use(database)):
            whole = desktop_alerts.gather(home, root, None, now, config=overview_base.CONFIG)
        self.assertEqual(whole["needs"], page["needs"])
        self.assertEqual({c["key"] for c in whole["conditions"]}, set(found))

    def test_overview_card_and_notification_share_their_rows(self):
        template = (Path(overview_page.__file__).parent / "dashboard_assets/templates/overview.html").read_text(encoding="utf-8")
        # E15 join: the card is its own template (overview_needs.html, E15.T2), given the same rows.
        self.assertIn("rows = v.needs_rows", template)
        card = (Path(overview_page.__file__).parent / "dashboard_assets/templates/overview_needs.html").read_text(encoding="utf-8")
        self.assertIn("{% for key, one, many, href, label in rows %}", card)
        self.assertNotIn("set rows", card)
        self.assertIn("sign_ins", [row[0] for row in overview_page.NEEDS_ROWS])


class TrayDelivery(unittest.TestCase):
    def icon(self):
        actions = mock.Mock()
        made = tray.Tray(actions, modules=(mock.Mock(), mock.Mock()), call_on_main=lambda function: function())
        made.icon, made._ready = mock.Mock(), True
        return made, actions

    def test_windows_posts_through_the_tray_library_and_a_click_opens_the_page(self):
        made, actions = self.icon()
        self.assertTrue(made.notify("LinkedIn", "Needs you to sign in again.", "/settings#connections", platform="win32"))
        made.icon.notify.assert_called_once_with("Needs you to sign in again.", "LinkedIn")
        original = mock.Mock(__name__="_on_notify")
        made.icon._message_handlers = {1025: original, 2: mock.Mock(__name__="_on_stop")}
        self.assertTrue(tray.windows_click(made.icon, lambda: made._open_page(made._clicked)))
        made.icon._message_handlers[1025](0, tray.BALLOON_CLICKED)
        actions.open_page.assert_called_once_with("/settings#connections")
        original.assert_not_called()
        made.icon._message_handlers[1025](0, 0x0202)         # an ordinary click on the icon: as before
        original.assert_called_once_with(0, 0x0202)

    def test_an_injected_backend_never_reaches_the_os_on_macos(self):
        made, _ = self.icon()
        made.run_command = mock.Mock()
        with mock.patch.object(tray, "mac_notify") as native:
            self.assertTrue(made.notify("T", "B", "/", platform="darwin"))
        native.assert_not_called()
        made.run_command.assert_not_called()
        made.icon.notify.assert_called_once_with("B", "T")

    def test_macos_posts_as_sidedoor_and_falls_back_to_applescript(self):
        made, actions = self.icon()
        made.native, made.run_command = True, mock.Mock()
        started = []
        made.start_thread = started.append
        with mock.patch.object(tray, "mac_notify", return_value=True) as native:
            self.assertTrue(made.notify("T", "B", "/approvals#notes", platform="darwin"))
        native.assert_called_once_with("T", "B", "/approvals#notes", made._open_page)
        made.run_command.assert_not_called()
        for outcome in ({"return_value": False}, {"side_effect": RuntimeError("no centre")}):
            made.run_command.reset_mock()
            with mock.patch.object(tray, "mac_notify", **outcome):
                self.assertTrue(made.notify('T"; do shell script "x', "B", "/", platform="darwin"))
            made.run_command.assert_not_called()             # U7: never on the main thread
            started.pop()()
            command = made.run_command.call_args.args[0]
            self.assertEqual(command, ["/usr/bin/osascript", "-e", "on run argv", "-e",
                                       "display notification (item 1 of argv) with title (item 2 of argv) sound name \"default\"",
                                       "-e", "end run", "B", 'T"; do shell script "x'])   # words are arguments only

    def test_mac_notify_delivers_and_its_click_returns_the_page(self):
        class Object:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self
        note = mock.Mock()
        centre = mock.Mock()
        foundation = mock.Mock(NSObject=Object)
        foundation.NSUserNotificationCenter.defaultUserNotificationCenter.return_value = centre
        foundation.NSUserNotification.alloc.return_value.init.return_value = note
        clicked = []
        self.addCleanup(tray._NOTE_DELEGATE.clear)
        self.assertTrue(tray.mac_notify("LinkedIn", "Sign in again.", "/settings#connections", clicked.append, foundation))
        note.setTitle_.assert_called_once_with("LinkedIn")
        note.setInformativeText_.assert_called_once_with("Sign in again.")
        centre.deliverNotification_.assert_called_once_with(note)
        delegate = centre.setDelegate_.call_args.args[0]
        shown = mock.Mock()
        shown.userInfo.return_value = note.setUserInfo_.call_args.args[0]
        delegate.userNotificationCenter_didActivateNotification_(centre, shown)
        self.assertEqual(clicked, ["/settings#connections"])
        foundation.NSUserNotificationCenter.defaultUserNotificationCenter.return_value = None
        self.assertFalse(tray.mac_notify("T", "B", "/", clicked.append, foundation))

    def test_mac_notify_plays_the_standard_sound_and_is_delivered_without_one(self):   # owner, 2026-11-14
        class Object:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

        def made(**names):
            note, centre = mock.Mock(), mock.Mock()
            foundation = mock.Mock(spec=["NSObject", "NSUserNotificationCenter", "NSUserNotification"] + list(names), NSObject=Object, **names)
            foundation.NSUserNotificationCenter.defaultUserNotificationCenter.return_value = centre
            foundation.NSUserNotification.alloc.return_value.init.return_value = note
            return foundation, note, centre
        self.addCleanup(tray._NOTE_DELEGATE.clear)
        foundation, note, centre = made(NSUserNotificationDefaultSoundName="DefaultSoundName")
        self.assertTrue(tray.mac_notify("T", "B", "/", print, foundation))
        note.setSoundName_.assert_called_once_with("DefaultSoundName")
        centre.deliverNotification_.assert_called_once_with(note)
        # A Foundation without that name: no sound, still delivered.
        foundation, note, centre = made()
        self.assertTrue(tray.mac_notify("T", "B", "/", print, foundation))
        note.setSoundName_.assert_not_called()
        centre.deliverNotification_.assert_called_once_with(note)
        # A note that will not take a sound: still delivered.
        foundation, note, centre = made(NSUserNotificationDefaultSoundName="DefaultSoundName")
        note.setSoundName_.side_effect = RuntimeError("no such selector")
        self.assertTrue(tray.mac_notify("T", "B", "/", print, foundation))
        centre.deliverNotification_.assert_called_once_with(note)
        # The AppleScript way asks for the default sound too; the words still go in as arguments.
        command = tray.osascript_command("Title", "Text")
        self.assertIn('display notification (item 1 of argv) with title (item 2 of argv) sound name "default"', command)
        self.assertEqual(command[-2:], ["Text", "Title"])

    def test_not_ready_posts_nothing(self):
        made, _ = self.icon()
        made._ready = False
        self.assertFalse(made.notify("T", "B", "/", platform="win32"))

    def test_the_count_shows_in_the_hover_text_and_not_at_zero(self):
        self.assertEqual(tray.count_title(0), "Your Sidedoor")
        self.assertEqual(tray.count_title(1), "Your Sidedoor: 1 thing needs you")
        self.assertEqual(tray.count_title(4), "Your Sidedoor: 4 things need you")
        made, actions = self.icon()
        actions.waiting.return_value = 4
        made._refresh_waiting()
        self.assertEqual(made.icon.title, "Your Sidedoor: 4 things need you")
        actions.waiting.return_value = 0
        made._refresh_waiting()
        self.assertEqual(made.icon.title, "Your Sidedoor")


@unittest.skipUnless(DASHBOARD, "fastapi/httpx not installed")
class Links(unittest.TestCase):
    def test_page_url_keeps_the_page_and_refuses_anything_else(self):
        base = "http://127.0.0.1:%d" % dashboard.PORT
        self.assertEqual(dashboard.page_url("S", "/settings#connections"), base + "/settings?t=S#connections")
        self.assertEqual(dashboard.page_url("S", "/applications?show=needs_you"), base + "/applications?show=needs_you&t=S")
        for other in (None, "", "https://example.com/x", "//example.com"):
            self.assertEqual(dashboard.page_url("S", other), base + "/?t=S")

    def test_a_notification_click_opens_through_the_same_opener_as_the_tray(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(dashboard, "announce") as announce:
            dashboard.TrayActions("S", Path(root)).open_page("/approvals#questions")
        announce.assert_called_once_with("http://127.0.0.1:%d/approvals?t=S#questions" % dashboard.PORT, True)


if DASHBOARD:
    class Switch(settings_base.SettingsPageTest):
        """Only these tests of the Settings page fixture run here."""

        def run(self, result=None):   # the inherited Settings tests run in their own module
            if not self._testMethodName.startswith("test_e15t3_"):
                return None
            return super().run(result)

        def test_e15t3_switch_on_by_default_and_saved_off(self):
            page = self.client.get("/settings").text
            self.assertIn(desktop_alerts.LABEL, page)
            self.assertEqual(page.count('id="s-desktop-alerts"'), 1)
            self.assertIn('id="s-desktop-alerts" checked', page)
            answer = self.client.post("/settings/notifications", data={}, headers=settings_base.HTMX)
            self.assertEqual(answer.status_code, 200)
            self.assertIn('id="s-desktop-alerts">', answer.text)
            self.assertFalse(desktop_alerts.enabled(self.home))
            self.client.post("/settings/notifications", data={"enabled": "on"}, headers=settings_base.HTMX)
            self.assertTrue(desktop_alerts.enabled(self.home))

        def test_e15t3_tokened_link_keeps_its_page_and_drops_the_token(self):
            answer = self.client.get("/applications", params={"show": "needs_you", "t": settings_base.SECRET})
            self.assertEqual(answer.status_code, 303)
            self.assertEqual(answer.headers["location"], "/applications?show=needs_you")
            self.assertEqual(self.client.get("/", params={"t": settings_base.SECRET}).headers["location"], "/")

        def test_e15t3_a_change_in_the_dashboard_is_noted(self):
            desktop_alerts._acted.clear()
            self.client.get("/settings")
            self.client.post("/settings/notifications", data={"enabled": "on"}, headers=settings_base.HTMX)
            self.assertEqual(desktop_alerts._acted, {})
            self.client.post("/pause", headers=settings_base.HTMX)
            self.assertEqual(set(desktop_alerts._acted), {"Scheduler"})


class Wording(unittest.TestCase):
    def test_zz_nothing_posted_has_a_code_or_an_em_dash(self):
        texts = [part for pair in POSTED for part in pair] + [desktop_alerts.LABEL, tray.count_title(3)]
        texts += ["%d %s" % (count, words) for _, one, many, _, _ in overview_page.NEEDS_ROWS
                  for count, words in ((1, one), (2, many))]
        self.assertGreater(len(texts), 20)
        for text in texts:
            self.assertNotRegex(text, r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b[A-Z]{4,}\b", text)
            self.assertNotIn("—", text)
            self.assertNotIn("automation", text.lower())


def load_tests(loader, tests, pattern):
    """The wording check last: it reads what the other tests posted."""
    suite = unittest.TestSuite()
    cases = [Rules, Sources, TrayDelivery, Links] + ([Switch] if DASHBOARD else []) + [Wording]
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == "__main__":
    unittest.main()
