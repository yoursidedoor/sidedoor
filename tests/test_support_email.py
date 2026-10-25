"""Email support (owner, 2026-10-24): the button, the route, the draft session's prompt and its endings.
Fakes only: no Chrome, no Codex, no email."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import importlib.util
import json
import re
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
        with mock.patch.object(browser_runtime, "lock_holder", return_value=4242), \
                mock.patch.object(browser_runtime, "holds_profile", return_value=True), \
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
        self.assertTrue(payload["webmail"]["mailbox_url"].startswith("https://"))
        self.assertEqual(self.client.get("/report-problem/email").json()["state"], "starting")

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
