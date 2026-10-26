"""Email support (owner, 2026-10-24): the button, the route, the draft session's prompt and its endings.
Fakes only: no Chrome, no Codex, no email."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import importlib.util
import io
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import browser_runtime
import report_problem
import setup_steps
import support_email
import test_dashboard_overview as base
import version

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

ORIGIN = {"origin": f"http://127.0.0.1:{base.PORT}"}
SUMMARY = "Sidedoor diagnostic summary\nVersion: 0.1.0\n\nChecks:\nOK  Home folder: fine\n"


class FakeHold:
    def __init__(self, free=True):
        self.free, self.released = free, False

    def take(self):
        return self.free

    def release(self):
        self.released = True


class BodyTest(unittest.TestCase):
    def test_body_is_the_bug_report_template_filled_with_the_summary(self):
        text = support_email.body(SUMMARY, "0.1.0", "macOS Apple chip")
        template = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
        labels = re.findall(r"^\s+label: (.+)$", template.read_text(), re.M)
        labels = [label for label in labels if not label.startswith("I removed")]
        self.assertEqual(labels, [name for name, _ in support_email.FIELDS])
        for label in labels:
            self.assertIn(label + "\n", text)
        self.assertIn("Your computer\nmacOS Apple chip\n", text)
        self.assertIn("Sidedoor version\n0.1.0\n", text)
        self.assertIn("Diagnostic summary\n" + SUMMARY, text)
        self.assertEqual(support_email.subject("0.1.0"), "Sidedoor problem report 0.1.0")

    def test_prompt_forbids_send(self):
        self.assertIn("NEVER press Send", support_email.PROMPT)
        self.assertIn("presses Send themselves", support_email.PROMPT)
        self.assertIn("never by running code on the page", support_email.PROMPT)

    def test_prompt_lets_the_ai_pick_only_the_users_account_and_skip_optional_prompts(self):
        prompt = support_email.PROMPT
        self.assertIn("click the account whose address is exactly request.webmail.mailbox_email, and only that\none", prompt)
        self.assertIn("click that skip control", prompt)
        self.assertIn("Never type anything into a sign-in page", prompt)
        self.assertNotIn("never pick an account", prompt)
        self.assertNotIn("never type or click", support_email.SignInWait.listing()["description"])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = {"browser_driver": {"isolated": True}}
        for patcher in (mock.patch.object(support_email, "session_config",
                                          lambda config, home, root: dict(config, _browser_operation="support_email")),
                        mock.patch.object(browser_runtime, "missing", lambda config=None: [])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, result):
        seen = {}

        def executor(config, prompt, directory, timeout):
            seen.update(config=config, prompt=prompt)
            (directory / "output.json").write_text(json.dumps(result))
        status, hold, front = support_email.Status(), FakeHold(), mock.Mock()
        front.activate.return_value = True
        wait = mock.Mock(side_effect=lambda profile: setattr(self, "opened", status.get()))
        payload = {"to": support_email.SUPPORT, "subject": "s", "body": "b", "webmail": {}}
        with mock.patch.object(browser_runtime, "lock_holder", return_value=4242), \
                mock.patch.object(browser_runtime, "holds_profile", return_value=True), \
                mock.patch.object(browser_runtime, "end_session") as end:
            support_email.run_session(status, hold, None, self.root, self.config, payload,
                                      executor=executor, frontmost=front, wait=wait)
        return status.get(), hold, front, wait, end, seen

    def test_a_started_draft_comes_forward_and_holds_the_browser_until_closed(self):
        state, hold, front, wait, end, seen = self.run_with({"draft_open": True, "reason": "Compose is open."})
        self.assertEqual(self.opened["state"], "open")
        self.assertEqual(self.opened["message"], support_email.WORDS["open"])
        self.assertEqual(state["state"], "idle")     # after the user closed the window
        self.assertEqual(state["last"], "closed")
        self.assertEqual(state["message"], "Your draft window was closed. Click Email support to start a new one.")
        front.activate.assert_called_once_with(4242)
        wait.assert_called_once()
        end.assert_not_called()                      # the draft window is the user's
        self.assertTrue(hold.released)
        self.assertEqual(seen["config"]["_browser_operation"], "support_email")
        self.assertIn("NEVER press Send", seen["prompt"])
        self.assertIn('"to": "%s"' % support_email.SUPPORT, seen["prompt"])

    def test_a_draft_that_could_not_start_says_why_and_closes_chrome(self):
        state, hold, front, wait, end, _ = self.run_with({"draft_open": False, "reason": "The page asks you to sign in."})
        self.assertEqual(state["state"], "failed")
        self.assertIn("The page asks you to sign in.", state["message"])
        self.assertEqual(state["address"], support_email.SUPPORT)
        front.activate.assert_not_called()
        wait.assert_not_called()
        end.assert_called_once()
        self.assertTrue(hold.released)

    def test_the_session_folder_with_its_trace_is_deleted_on_success_and_failure(self):
        folder = self.root / "reports" / "support-email"
        for result in ({"draft_open": True}, {"draft_open": False, "reason": "No."}):
            with mock.patch("paths.reports", return_value=self.root / "reports"):
                self.run_with(result)
            self.assertEqual(list(folder.iterdir()), [], result)

    def test_a_failing_profile_lookup_still_releases_and_fails(self):
        status, hold = support_email.Status(), FakeHold()
        with mock.patch.object(browser_runtime, "profile", side_effect=OSError("x")), \
                mock.patch.object(browser_runtime, "end_session"):
            support_email.run_session(status, hold, None, self.root, self.config, {}, executor=mock.Mock())
        self.assertEqual(status.get()["state"], "failed")
        self.assertTrue(hold.released)

    def test_a_window_left_open_is_closed_after_thirty_minutes(self):
        now = [0.0]
        sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        # chrome_running is the one window check on every platform (Windows reads Chrome's lockfile).
        with mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session") as end:
            support_email.wait_closed("/p", pages=lambda profile: 1, sleep=sleep, clock=lambda: now[0])
        end.assert_called_once()
        self.assertGreaterEqual(now[0], 30 * 60)
        self.assertLess(now[0], 30 * 60 + 2 * support_email.WATCH_SECONDS)
        self.assertIn("Applications wait until you close it", support_email.WORDS["open"])
        self.assertIn("Drafts", support_email.WORDS["open"])

    def test_profile_busy_says_so(self):
        status, hold = support_email.Status(), FakeHold()
        with mock.patch.dict(self.config, {"browser_driver": {}}), \
                mock.patch.object(browser_runtime, "prepare_profile", side_effect=browser_runtime.BrowserProfileBusy("x")), \
                mock.patch.object(browser_runtime, "end_session"):
            support_email.run_session(status, hold, None, self.root, self.config, {}, executor=mock.Mock())
        self.assertEqual(status.get()["state"], "busy")
        self.assertTrue(hold.released)


class Recorder(support_email.Status):
    """A Status that keeps every state it was set to."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def set(self, state, *arguments, **options):
        value = super().set(state, *arguments, **options)
        self.seen.append(value)
        return value


class SignInTest(unittest.TestCase):
    """Option A (owner, 2026-10-25): the window stays open while the user signs in, then the email starts."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for patcher in (mock.patch.object(support_email, "session_config",
                                          lambda config, home, root: dict(config, _browser_operation="support_email")),
                        mock.patch.object(browser_runtime, "missing", lambda config=None: []),
                        mock.patch.object(support_email, "SIGNIN_WATCH_SECONDS", 0.01),
                        mock.patch("paths.reports", return_value=self.root / "reports")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, result, sign_in=True):
        status, hold, seen = Recorder(), FakeHold(), {}

        def executor(config, prompt, directory, timeout):
            seen["context"] = json.loads(Path(config["_application_human_check"]).read_text())
            seen["directory"] = str(directory)
            if sign_in:   # the AI met the account chooser and called the guard's wait (the real one)
                wait = support_email.SignInWait(config["_application_human_check"], sleep=lambda seconds: None)
                wait.call()
                for _ in range(500):   # the user signs in meanwhile: the page shows "signin"
                    if status.get()["state"] == "signin":
                        break
                    threading.Event().wait(0.01)
                seen["during"] = status.get()
            (directory / "output.json").write_text(json.dumps(result))
        front = mock.Mock()
        front.activate.side_effect = lambda pid: seen.setdefault(
            "released_when_forward", []).append(
            (Path(seen["directory"]) / support_email.FRONT_FILE).exists())
        payload = {"to": support_email.SUPPORT, "subject": "s", "body": "b", "webmail": {"provider": "Gmail"}}
        with mock.patch.object(browser_runtime, "lock_holder", return_value=4242), \
                mock.patch.object(browser_runtime, "holds_profile", return_value=True), \
                mock.patch.object(browser_runtime, "end_session") as end:
            support_email.run_session(status, hold, None, self.root, {"browser_driver": {"isolated": True}}, payload,
                                      executor=executor, frontmost=front, wait=lambda profile: None)
        return status, hold, seen, end, front

    def test_signed_in_during_the_wait_opens_the_draft(self):
        status, hold, seen, end, front = self.run_with({"draft_open": True, "needs_sign_in": False, "reason": "Open."})
        self.assertEqual(seen["during"]["state"], "signin")
        self.assertEqual(seen["during"]["message"], "Sign in to Gmail in the window that opened. "
                                                    "Sidedoor starts your email once you're signed in.")
        self.assertTrue(status.busy() is False)
        self.assertEqual([value["state"] for value in status.seen], ["signin", "open", "idle"])
        self.assertTrue(front.activate.called)    # the window came forward for the sign-in
        end.assert_not_called()                   # the window was never closed while waiting
        self.assertTrue(hold.released)

    def test_the_guard_is_told_to_stop_keeping_the_window_back_before_it_comes_forward(self):
        _, _, seen, _, _ = self.run_with({"draft_open": True, "needs_sign_in": False, "reason": "Open."}, sign_in=False)
        self.assertEqual(seen["released_when_forward"], [True])

    def test_still_signed_out_fails_with_the_settings_link(self):
        status, hold, seen, end, _ = self.run_with({"draft_open": False, "needs_sign_in": True, "reason": "x"})
        self.assertEqual(seen["during"]["state"], "signin")
        state = status.get()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["settings"], "/settings#connection-browser")
        self.assertIn("weren't signed in to Gmail", state["message"])
        self.assertIn("Sidedoor's browser in Settings", state["message"])
        self.assertEqual(state["address"], support_email.SUPPORT)
        self.assertEqual(state["fallback"], support_email.FALLBACK)
        end.assert_called_once()                  # the session is over: its Chrome is closed
        self.assertTrue(hold.released)

    def test_other_failures_keep_the_reason_and_no_settings_link(self):
        status, _, _, _, _ = self.run_with({"draft_open": False, "needs_sign_in": False,
                                            "reason": "The page would not load."}, sign_in=False)
        state = status.get()
        self.assertEqual(state["message"], "Sidedoor couldn't open your email: The page would not load.")
        self.assertNotIn("settings", state)
        self.assertNotIn("signin", [value["state"] for value in status.seen])

    def test_the_session_is_given_the_sign_in_wait(self):
        _, _, seen, _, _ = self.run_with({"draft_open": True, "needs_sign_in": False, "reason": "r"})
        self.assertEqual(seen["context"], {"directory": seen["directory"]})   # no root, request, claim or job
        self.assertIn("needs_sign_in", support_email.schema()["required"])
        self.assertIn("call wait_for_human_check", support_email.PROMPT)

    def test_the_wait_returns_every_ten_seconds_for_ten_minutes_then_expires(self):
        now = [support_email.dt.datetime(2026, 10, 25, tzinfo=support_email.dt.timezone.utc)]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += support_email.dt.timedelta(seconds=seconds)
        (self.root / "c.json").write_text(json.dumps({"directory": str(self.root)}))
        wait = support_email.SignInWait(self.root / "c.json", sleep=sleep, clock=lambda: now[0])
        answers = [wait.call() for _ in range(61)]
        self.assertEqual(slept, [10] * 60)
        self.assertTrue(all("call wait_for_human_check again" in answer for answer in answers[:60]))
        self.assertTrue(answers[60].startswith("WAIT_EXPIRED"))
        # Owner, 2026-10-25 (fix a): each answer says the wait is still running and how long is left.
        self.assertTrue(all(answer.startswith("THE WAIT IS NOT OVER") for answer in answers[:60]))
        self.assertIn("about 10 more minutes", answers[0])
        self.assertIn("less than 2 minutes to", answers[59])
        until = json.loads((self.root / support_email.SIGNIN_FILE).read_text())["until"]
        self.assertEqual(until, "2026-10-25T00:13:00+00:00")   # 600 s wait + 180 s to write the email


class FakeMac:
    """MacFrontmost's place: a scripted front app and recorded activations. Never the real desktop."""
    USER_APP, CHROME = __import__("os").getpid(), 4242   # the user app must be a running pid (pid_alive)
    last = None   # the instance the guard built

    def __init__(self, *arguments, **options):
        self.now, self.activated = self.USER_APP, []
        FakeMac.last = self

    def front(self):
        return self.now

    def activate(self, pid):
        self.activated.append(pid)
        self.now = pid
        return True

    def shown(self, pid):
        return True


class FocusTest(unittest.TestCase):
    """Owner, 2026-10-25: while the AI works, Sidedoor's Chrome never takes the front from the user's app;
    it comes forward only for the sign-in wait or the finished draft."""

    def proxy(self):
        import browser_guard
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        (directory / support_email.SIGNIN_CONTEXT).write_text(json.dumps({"directory": str(directory)}))
        order = []
        FakeMac.last = None

        def launch(path):
            order.append(("launch", FakeMac.last.activated[:] if FakeMac.last else None))
            return "http://127.0.0.1:53111"
        with mock.patch.object(browser_guard.subprocess, "Popen"), \
                mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_guard.sys, "platform", "darwin"), \
                mock.patch.object(browser_guard, "MacFrontmost", FakeMac), \
                mock.patch.object(browser_guard, "FOCUS_POLL_SECONDS", 3600):
            proxy = browser_guard.Proxy("support_email", ["node"], audit=directory / "guard.jsonl",
                                        stdin=io.StringIO(""), stdout=io.StringIO(),
                                        human_check=str(directory / support_email.SIGNIN_CONTEXT),
                                        background_chrome=str(directory), launch_background=launch)
        self.addCleanup(proxy.stopped.set)
        proxy.place_window = mock.Mock()
        for patcher in (mock.patch.object(browser_runtime, "lock_holder", return_value=FakeMac.CHROME),
                        mock.patch.object(browser_runtime, "holds_profile", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return proxy, FakeMac.last, directory, order

    def test_chrome_taking_the_front_while_the_ai_works_is_given_back_without_minimizing(self):
        proxy, mac, _, order = self.proxy()
        self.assertIsNotNone(proxy.keeper_thread)          # running before Chrome was started
        self.assertEqual(order, [("launch", [])])
        self.assertFalse(proxy.offscreen)                  # on-screen, behind: never minimized
        mac.now = FakeMac.CHROME                           # Chrome activated itself (a new tab, a navigation)
        proxy.focus_keeper.poll()
        self.assertEqual(mac.activated, [FakeMac.USER_APP])
        proxy.place_window.assert_not_called()

    def test_the_sign_in_wait_lets_the_window_stay_in_front(self):
        proxy, mac, _, _ = self.proxy()
        proxy.human_check_wait.sleep = lambda seconds: None
        proxy.human_check_wait.call()                      # the user must sign in
        for _ in range(3):                                 # in front for the user, also between the 10 s waits
            mac.now = FakeMac.CHROME
            proxy.focus_keeper.poll()
        self.assertEqual(mac.activated, [])

    def test_the_finished_draft_brought_forward_is_never_sent_back(self):
        proxy, mac, directory, _ = self.proxy()
        (directory / support_email.FRONT_FILE).write_text("{}")   # run_session, just before bring_forward
        mac.now = FakeMac.CHROME
        proxy.focus_keeper.poll()
        (directory / support_email.FRONT_FILE).unlink()            # released for good
        proxy.focus_keeper.poll()
        self.assertEqual(mac.activated, [])


class HeadlessFirstTest(unittest.TestCase):
    """Owner, 2026-10-25 ("B + C"): the AI makes and saves the draft headless; the saved draft then reopens
    in a window behind the user's app, which comes forward. Anything else takes the visible path."""
    GMAIL_DRAFT = "https://mail.google.com/mail/u/0/#drafts?compose=18f2a"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events, self.prompts = [], []
        for patcher in (mock.patch.object(support_email, "session_config",
                                          lambda config, home, root: dict(config, _browser_operation="support_email")),
                        mock.patch.object(browser_runtime, "missing", lambda config=None: []),
                        mock.patch.object(browser_runtime, "background_launch", lambda config=None, platform=None: True),
                        mock.patch.object(browser_runtime, "chrome_app", lambda binary: "/Applications/Google Chrome.app"),
                        mock.patch.object(setup_steps, "chrome_path", lambda platform=None: "/chrome"),
                        mock.patch.object(browser_runtime, "lock_holder", return_value=4242),
                        mock.patch.object(browser_runtime, "holds_profile", return_value=True),
                        mock.patch.object(browser_runtime, "end_session",
                                          side_effect=lambda config=None: self.events.append("end_session")),
                        mock.patch.object(support_email, "SIGNIN_WATCH_SECONDS", 0.01),
                        mock.patch("paths.reports", return_value=self.root / "reports")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, results, provider="Gmail", reopen_error=None, headless_error=None):
        status, hold, calls = Recorder(), FakeHold(), []

        def executor(config, prompt, directory, timeout):
            headless = bool(config.get(browser_runtime.HEADLESS_PHASE))
            calls.append({"headless": headless, "wait_offered": "_application_human_check" in config,
                          "prompt": "headless" if prompt.startswith(support_email.HEADLESS_PROMPT) else "visible",
                          "hold_released": hold.released})
            self.events.append("headless AI" if headless else "visible AI")
            self.prompts.append(prompt)
            if headless and headless_error:
                raise headless_error
            (directory / "output.json").write_text(json.dumps(results.pop(0)))

        def launch(app, path, onscreen=False, url=None):
            self.events.append(("reopen", onscreen, url))
            if reopen_error:
                raise reopen_error
            return "http://127.0.0.1:53111"
        front = mock.Mock()
        front.activate.side_effect = lambda pid: self.events.append("forward")
        payload = {"to": support_email.SUPPORT, "subject": "s", "body": "b",
                   "webmail": {"provider": provider, "mailbox_email": "user@example.com"}}
        with mock.patch.object(browser_runtime, "launch_background", side_effect=launch):
            support_email.run_session(status, hold, None, self.root, {"browser_driver": {"isolated": True}}, payload,
                                      executor=executor, frontmost=front,
                                      wait=lambda profile: self.events.append("waiting for the user"))
        return status, hold, calls

    def saved(self, url=GMAIL_DRAFT):
        return {"draft_open": True, "needs_sign_in": False, "draft_url": url, "reason": "Saved."}

    def visible_open(self):
        return {"draft_open": True, "needs_sign_in": False, "draft_url": None, "reason": "Open."}

    def test_signed_in_already_the_saved_draft_reopens_behind_then_comes_forward(self):
        status, hold, calls = self.run_with([self.saved()])
        self.assertEqual(calls, [{"headless": True, "wait_offered": False, "prompt": "headless", "hold_released": False}])
        self.assertEqual(self.events, ["headless AI", "end_session", ("reopen", True, self.GMAIL_DRAFT), "forward",
                                       "waiting for the user"])
        self.assertEqual([value["state"] for value in status.seen], ["open", "idle"])
        self.assertTrue(hold.released)

    def test_outlook_drafts_reopen_too(self):
        url = "https://outlook.live.com/mail/0/drafts/id/AQMkADAw"
        self.run_with([self.saved(url)], provider="Outlook.com")
        self.assertIn(("reopen", True, url), self.events)

    def test_a_password_needed_goes_to_the_visible_window_with_the_wait_and_the_hold_kept(self):
        status, hold, calls = self.run_with([{"draft_open": False, "needs_sign_in": True, "draft_url": None, "reason": "r"},
                                             self.visible_open()])
        self.assertEqual([(call["headless"], call["wait_offered"], call["prompt"], call["hold_released"]) for call in calls],
                         [(True, False, "headless", False), (False, True, "visible", False)])
        self.assertEqual(self.events, ["headless AI", "end_session", "visible AI", "forward", "waiting for the user"])
        self.assertTrue(hold.released)

    def test_no_address_on_the_webmails_hosts_takes_the_visible_path(self):
        for url in (None, "https://evil.example/mail/#drafts?compose=1", "http://mail.google.com/x",
                    "https://user@mail.google.com/x"):
            self.events.clear()
            _, _, calls = self.run_with([self.saved(url), self.visible_open()])
            self.assertEqual([call["headless"] for call in calls], [True, False], url)
            self.assertFalse([event for event in self.events if isinstance(event, tuple)], url)

    def test_a_reopen_or_headless_failure_takes_the_visible_path(self):
        _, _, calls = self.run_with([self.saved(), self.visible_open()], reopen_error=OSError("no port"))
        self.assertEqual([call["headless"] for call in calls], [True, False])
        self.events.clear()
        _, _, calls = self.run_with([self.visible_open()], headless_error=RuntimeError("x"))
        self.assertEqual([call["headless"] for call in calls], [True, False])

    def test_other_webmails_keep_the_visible_path_from_the_start(self):
        _, _, calls = self.run_with([self.visible_open()], provider="Proton Mail")
        self.assertEqual([call["headless"] for call in calls], [False])
        self.assertNotIn("headless_attempt", self.prompts[0])

    @staticmethod
    def request_of(prompt):
        return json.loads(prompt.split("\nREQUEST_JSON:\n", 1)[1])

    def test_after_a_headless_attempt_the_visible_ai_is_told_to_finish_its_saved_draft(self):
        # The headless AI failed part-way (its draft may be autosaved): the visible AI gets the facts.
        self.run_with([self.visible_open()], headless_error=RuntimeError("x"))
        visible = self.prompts[1]
        self.assertEqual(self.request_of(visible)["headless_attempt"], {"ran": True, "draft_url": None})
        self.assertIn(support_email._EARLIER, visible)
        self.assertNotIn(support_email._EARLIER, self.prompts[0])
        # A reported draft address on the webmail's hosts goes with it (the reopen failed here).
        self.prompts.clear()
        self.run_with([self.saved(), self.visible_open()], reopen_error=OSError("no port"))
        self.assertEqual(self.request_of(self.prompts[1])["headless_attempt"], {"ran": True, "draft_url": self.GMAIL_DRAFT})
        # An address off the webmail's hosts is not passed on.
        self.prompts.clear()
        self.run_with([self.saved("https://evil.example/x"), self.visible_open()])
        self.assertEqual(self.request_of(self.prompts[1])["headless_attempt"]["draft_url"], None)

    def test_without_a_headless_attempt_the_visible_prompt_is_unchanged(self):
        self.run_with([self.visible_open()], provider="Proton Mail")
        payload = self.request_of(self.prompts[0])
        self.assertEqual(self.prompts[0], support_email.PROMPT + "\nREQUEST_JSON:\n" + json.dumps(payload))

    def test_the_headless_prompt_never_waits_and_reports_the_saved_draft(self):
        self.assertNotIn("wait_for_human_check", support_email.HEADLESS_PROMPT)
        self.assertIn("shows\nthat it saved the draft", support_email.HEADLESS_PROMPT)
        self.assertIn("report draft_open false and needs_sign_in true", support_email.HEADLESS_PROMPT)
        self.assertIn("click the account whose address is exactly request.webmail.mailbox_email",
                      support_email.HEADLESS_PROMPT)
        self.assertIn("draft_url", support_email.schema()["required"])

    def test_only_the_headless_phase_runs_headless(self):
        with mock.patch.object(browser_runtime, "headed_user_agent", return_value="UA"):
            self.assertIsNone(browser_runtime.headless_user_agent({}, "support_email"))
            self.assertEqual(browser_runtime.headless_user_agent({browser_runtime.HEADLESS_PHASE: True}, "support_email"),
                             "UA")
        command = browser_runtime.background_command("/A.app", "/p", onscreen=True, url=self.GMAIL_DRAFT)
        self.assertEqual(command[-1], self.GMAIL_DRAFT)
        self.assertEqual(browser_runtime.background_command("/A.app", "/p")[-1], "about:blank")


class DeadlineTest(unittest.TestCase):
    """outreach_history_browser.communicate: the session's deadline moves past the sign-in wait."""

    def fake(self, finish_after):
        clock = [0.0]

        class Worker:
            calls = 0

            def communicate(self, pending, timeout):
                Worker.calls += 1
                if clock[0] + timeout >= finish_after:
                    clock[0] = finish_after
                    return
                clock[0] += timeout
                raise subprocess.TimeoutExpired("codex", timeout)
        return Worker(), clock

    def test_no_wait_keeps_the_ordinary_timeout(self):
        import outreach_history_browser
        worker, clock = self.fake(finish_after=400)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(subprocess.TimeoutExpired):
            outreach_history_browser.communicate(worker, "p", 300, directory, ["codex"], clock=lambda: clock[0])
        self.assertAlmostEqual(clock[0], 300, delta=1)

    def test_the_sign_in_wait_moves_the_deadline(self):
        import outreach_history_browser
        worker, clock = self.fake(finish_after=700)
        with tempfile.TemporaryDirectory() as directory:
            until = support_email.dt.datetime.now(support_email.dt.timezone.utc) + support_email.dt.timedelta(seconds=780)
            (Path(directory) / support_email.SIGNIN_FILE).write_text(json.dumps({"until": until.isoformat()}))
            outreach_history_browser.communicate(worker, "p", 300, directory, ["codex"], clock=lambda: clock[0])
        self.assertEqual(clock[0], 700)

    def test_the_deadline_never_moves_past_the_ceiling(self):
        import outreach_history_browser
        worker, clock = self.fake(finish_after=10 ** 6)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(subprocess.TimeoutExpired):
            until = support_email.dt.datetime.now(support_email.dt.timezone.utc) + support_email.dt.timedelta(days=1)
            (Path(directory) / support_email.SIGNIN_FILE).write_text(json.dumps({"until": until.isoformat()}))
            outreach_history_browser.communicate(worker, "p", 300, directory, ["codex"], clock=lambda: clock[0])
        self.assertAlmostEqual(clock[0], 300 + 600 + 180, delta=1)


@unittest.skipUnless(DEPENDENCIES, "fastapi and httpx are needed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        base.finished_home(self.home)
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        self.client.get("/", params={"t": base.SECRET})
        build = mock.patch.object(report_problem, "build", return_value=(SUMMARY, report_problem.issue_url("0.1.0", "Other")))
        build.start()
        self.addCleanup(build.stop)

    def webmail(self, label, url):
        setup_steps.update_state(lambda state: state.update(webmail={"label": label, "url": url}), self.home)

    def post(self, hold):
        started = threading.Event()
        calls = []

        def session(*arguments):
            calls.append(arguments)
            started.set()
        with mock.patch.object(setup_steps, "_Hold", return_value=hold), \
                mock.patch.object(support_email, "run_session", session):
            response = self.client.post("/report-problem/email", headers=ORIGIN)
            if response.status_code == 202:
                started.wait(5)
        return response, calls

    def test_click_starts_the_session_with_to_subject_and_body(self):
        self.webmail("Outlook.com", "https://outlook.live.com/mail/")
        response, calls = self.post(FakeHold())
        self.assertEqual(response.status_code, 202)
        answer = response.json()
        self.assertEqual(answer["state"], "starting")
        self.assertEqual(answer["message"], "Opening your email...")
        self.assertEqual(answer["summary"], SUMMARY)
        payload = calls[0][5]
        self.assertEqual(payload["to"], "support@yoursidedoor.com")
        self.assertEqual(payload["subject"], "Sidedoor problem report %s" % version.current())
        self.assertIn("What happened?\n", payload["body"])
        self.assertIn("Diagnostic summary\n" + SUMMARY, payload["body"])
        self.assertEqual(payload["webmail"]["provider"], "Outlook.com")
        self.assertEqual(payload["webmail"]["mailbox_email"], "user@example.com")
        self.assertTrue(payload["webmail"]["mailbox_url"].startswith("https://"))
        self.assertEqual(self.client.get("/report-problem/email").json()["state"], "starting")

    def test_a_draft_window_closed_before_any_poll_still_reports_how_it_ended(self):
        # The real run: starting -> open -> idle within microseconds, before the page's first poll.
        self.webmail("Gmail", "https://mail.google.com/")
        fresh = self.client.get("/report-problem/email").json()
        self.assertEqual(fresh, {"state": "idle"})   # no session yet: nothing to show
        hold, ended, real = FakeHold(), threading.Event(), support_email.run_session

        def executor(config, prompt, directory, timeout):
            (directory / "output.json").write_text(json.dumps({"draft_open": True, "reason": "Compose is open."}))

        def session(status, hold, home, root, config, payload):
            try:
                real(status, hold, home, root, {"browser_driver": {"isolated": True}}, payload,
                                          executor=executor, frontmost=mock.Mock(), wait=lambda profile: None)
            finally:
                ended.set()
        with mock.patch.object(setup_steps, "_Hold", return_value=hold), \
                mock.patch.object(support_email, "run_session", session), \
                mock.patch.object(support_email, "session_config", lambda config, home, root: dict(config)), \
                mock.patch.object(browser_runtime, "missing", lambda config=None: []), \
                mock.patch.object(browser_runtime, "lock_holder", return_value=None), \
                mock.patch.object(browser_runtime, "end_session"), \
                mock.patch("paths.reports", return_value=self.root / "reports"):
            self.assertEqual(self.client.post("/report-problem/email", headers=ORIGIN).status_code, 202)
            self.assertTrue(ended.wait(5))
        later = self.client.get("/report-problem/email").json()
        self.assertEqual(later["state"], "idle")
        self.assertEqual(later["last"], "closed")
        self.assertEqual(later["message"], "Your draft window was closed. Click Email support to start a new one.")
        self.assertTrue(hold.released)
        self.assertEqual(self.client.get("/report-problem/email").json()["last"], "closed")   # stays until a new click

    def test_no_webmail_says_so_and_links_to_settings(self):
        response, calls = self.post(FakeHold())
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer["state"], "no_webmail")
        self.assertEqual(answer["message"], support_email.WORDS["no_webmail"])
        self.assertEqual(answer["settings"], "/settings#connection-webmail")
        self.assertEqual(calls, [])

    def test_busy_browser_says_so_and_still_hands_back_the_summary(self):
        self.webmail("Gmail", "https://mail.google.com/")
        response, calls = self.post(FakeHold(free=False))
        answer = response.json()
        self.assertEqual(answer["state"], "busy")
        self.assertEqual(answer["message"], support_email.WORDS["busy"])
        self.assertEqual(answer["summary"], SUMMARY)
        self.assertEqual(answer["address"], support_email.SUPPORT)
        self.assertEqual(calls, [])

    def test_button_renders_on_failures(self):
        page = self.client.get("/failures").text
        self.assertIn('id="support-email-button"', page)
        self.assertIn("/static/support_email.js", page)
        self.assertEqual(self.client.post("/report-problem/email").status_code, 403)   # no Origin


if __name__ == "__main__":
    unittest.main()


class WindowsLaunchTest(unittest.TestCase):
    """Windows (owner, 2026-10-25): Sidedoor starts the draft window's Chrome itself, in a Job Object of its
    own (never the Codex worker's), and @playwright/mcp attaches over CDP, so the worker's kill_tree leaves
    the window open. Fakes only: portable's Windows branch is patched, no Chrome starts."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / "profile"
        self.killed, self.contained = [], []
        browser_runtime._STARTED.clear()
        for patcher in (mock.patch.object(browser_runtime.portable, "WINDOWS", True),
                        mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True),
                        mock.patch.object(browser_runtime.sys, "platform", "win32"),
                        mock.patch.object(browser_runtime.portable, "worker_spawn_options",
                                          lambda core=True: {"creationflags": 0x08000204}),
                        mock.patch.object(browser_runtime.portable, "contain", self.contained.append),
                        mock.patch.object(browser_runtime.portable, "kill_tree", self.killed.append),
                        mock.patch.object(browser_runtime.portable, "profile_busy", lambda path: False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(browser_runtime._STARTED.clear)

    def popen(self, argv, **options):
        self.argv, self.options = argv, options
        (self.profile / browser_runtime.DEVTOOLS_PORT_FILE).write_text("53999\n/devtools/browser/x")
        return mock.Mock(pid=777)

    def test_only_the_shown_operation_on_windows_launches_itself(self):
        config = {"browser_driver": {"profile": str(self.profile)}}
        self.assertTrue(browser_runtime.windows_launch(config, "support_email"))
        self.assertFalse(browser_runtime.windows_launch(config, "submit"))
        self.assertFalse(browser_runtime.windows_launch({"browser_driver": {"isolated": True}}, "support_email"))
        with mock.patch.object(browser_runtime.portable, "WINDOWS", False):
            self.assertFalse(browser_runtime.windows_launch(config, "support_email"))

    def test_chrome_starts_in_its_own_job_and_end_session_ends_that_job(self):
        endpoint = browser_runtime.launch_windows("C:/Chrome/chrome.exe", self.profile, popen=self.popen)
        self.assertEqual(endpoint, "http://127.0.0.1:53999")
        self.assertEqual(self.argv[:3], ["C:/Chrome/chrome.exe", "--user-data-dir=" + str(self.profile),
                                         "--remote-debugging-port=0"])
        self.assertIn("--window-position=%d,%d" % browser_runtime.ONSCREEN_POSITION, self.argv)
        self.assertEqual(self.options["creationflags"], 0x08000204)
        self.assertEqual([chrome.pid for chrome in self.contained], [777])
        with mock.patch.object(browser_runtime.os, "kill") as kill:
            browser_runtime.end_session({"browser_driver": {"profile": str(self.profile)}})
        self.assertEqual(self.killed, [777])
        kill.assert_not_called()
        browser_runtime.end_session({"browser_driver": {"profile": str(self.profile)}})
        self.assertEqual(self.killed, [777])   # once only

    def test_the_browser_server_attaches_instead_of_launching(self):
        config = {"browser_driver": {"profile": str(self.profile)},
                  browser_runtime.CDP_ENDPOINT: "http://127.0.0.1:53999"}
        command = browser_runtime.mcp_command(config, self.root / "session", "support_email")
        self.assertEqual(command[command.index("--cdp-endpoint") + 1], "http://127.0.0.1:53999")
        self.assertNotIn("--user-data-dir", command)
        plain = browser_runtime.mcp_command({"browser_driver": {"profile": str(self.profile)}}, self.root / "s2", "submit")
        self.assertNotIn("--cdp-endpoint", plain)
        self.assertIn("--user-data-dir", plain)

    def test_the_session_launches_chrome_before_the_ai_and_hands_it_the_endpoint(self):
        events, seen = [], {}

        def executor(config, prompt, directory, timeout):
            events.append("AI")
            seen.update(config)
            (directory / "output.json").write_text(json.dumps(
                {"draft_open": True, "needs_sign_in": False, "draft_url": None, "reason": "Open."}))
        payload = {"to": support_email.SUPPORT, "subject": "s", "body": "b", "webmail": {"provider": "Gmail"}}
        with mock.patch.object(support_email, "session_config",
                               lambda config, home, root: dict(config, _browser_operation="support_email")), \
                mock.patch.object(browser_runtime, "missing", lambda config=None: []), \
                mock.patch.object(browser_runtime, "prepare_profile", lambda path: None), \
                mock.patch.object(setup_steps, "chrome_path", lambda platform=None: "C:/Chrome/chrome.exe"), \
                mock.patch.object(browser_runtime, "launch_windows",
                                  side_effect=lambda binary, path: events.append(("launch", binary, str(path)))
                                  or "http://127.0.0.1:53999"), \
                mock.patch("paths.reports", return_value=self.root / "reports"):
            support_email.run_session(Recorder(), FakeHold(), None, self.root,
                                      {"browser_driver": {"profile": str(self.profile)}}, payload, executor=executor,
                                      frontmost=mock.Mock(), wait=lambda profile: events.append("waiting"))
        self.assertEqual(events, [("launch", "C:/Chrome/chrome.exe", str(self.profile)), "AI", "waiting"])
        self.assertEqual(seen[browser_runtime.CDP_ENDPOINT], "http://127.0.0.1:53999")
        self.assertNotIn(browser_runtime.HEADLESS_PHASE, seen)   # Windows has no headless phase

    def test_the_window_watch_reads_chromes_lockfile_on_windows(self):
        with mock.patch.object(browser_runtime.portable, "profile_busy", lambda path: True):
            self.assertTrue(browser_runtime.chrome_running(self.profile))
        self.assertFalse(browser_runtime.chrome_running(self.profile))
