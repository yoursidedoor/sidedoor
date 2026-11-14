"""V6 (owner, 2026-11-12): when a hidden session's page cannot be told the installed Chrome's own user agent,
client hints and screen, the guard stops the session, on macOS and Windows: nothing more reaches the browser,
the reason is in the audit, Chrome is ended, and the session's launcher ends it as an ordinary failed session
(never a submitted application, never a second submit). No Chrome starts here."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

import application_browser
import applications
import browser_guard
import browser_runtime
import desktop_adapter
import outreach_history_browser
import test_claude_submit as drill
from manual_application_browser import request_at, write_output
from test_browser_driver_review import scratch
from test_browser_headless import UA_141, answer

NAVIGATE = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "browser_navigate", "arguments": {"url": "https://boards.example.com/apply/7"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def run_guard(testcase, directory, works, platform="darwin", operation="inspect"):
    """A hidden session's guard run to its end: the client's `initialized`, then one browser call. The
    override call (after the page is open) answers an error unless `works` (a list: one answer per
    override call, the last one repeated). Returns what happened."""
    directory = Path(directory)
    held = threading.Event()
    testcase.addCleanup(held.set)
    seen = {"forwarded": [], "ended": [], "codes": [], "order": []}
    answers = list(works) if isinstance(works, (list, tuple)) else [works]

    def browser(method, params):
        code = params["arguments"]["code"]
        seen["codes"].append(code)
        if "setUserAgentOverride" in code:
            seen["order"].append("override")
            ok = answers.pop(0) if len(answers) > 1 else answers[0]
            return answer([{"url": "about:blank"}] if ok else [{"error": "Target closed"}])
        return answer([{"closing": True}])
    with mock.patch.object(browser_runtime.sys, "platform", platform), \
            mock.patch.object(browser_guard.subprocess, "Popen"), \
            mock.patch.object(browser_runtime, "end_session",
                              lambda config: seen["ended"].append(config["browser_driver"]["profile"])), \
            mock.patch.object(browser_runtime.portable, "wait_profile_free", lambda path, seconds: True):
        proxy = browser_guard.Proxy(
            operation, ["node", "cli.js"], audit=directory / "guard.jsonl", stdin=iter(held.wait, True),
            stdout=io.StringIO(), focus_keeper=False, background_chrome=str(directory / "profile"),
            headless_user_agent=UA_141, launch_headless=lambda path, user_agent: "http://127.0.0.1:53111",
            client_hints={"platform": "Windows" if platform == "win32" else "macOS"},
            screen={"width": 1536, "height": 864, "scale": 1.25})
        testcase.addCleanup(proxy.audit.close)
        proxy.child = mock.Mock(stdout=iter(held.wait, True), poll=lambda: None)
        proxy.child_call = browser
        proxy.to_child = lambda message: (seen["forwarded"].append(message), seen["order"].append(
            "forwarded " + (message.get("params") or {}).get("name", message["method"])))
        for message in (INITIALIZED, NAVIGATE, None):
            proxy.requests.put(message)
        seen["exit"] = proxy.run()
    seen["audit"] = [json.loads(line) for line in (directory / "guard.jsonl").read_text().splitlines()]
    seen["proxy"] = proxy
    return seen


class GuardStops(unittest.TestCase):
    def test_a_failed_override_stops_the_session_on_the_mac_and_on_windows(self):
        for platform in ("darwin", "win32"):
            directory = scratch(self)
            seen = run_guard(self, directory, works=False, platform=platform)
            self.assertEqual(seen["forwarded"], [INITIALIZED], platform)        # the browser call never reached it
            decisions = [row["decision"] for row in seen["audit"]]
            # V14: tried once more before the stop; nothing reaches the browser in between.
            self.assertEqual(decisions, ["BACKGROUND_LAUNCHED", "CLIENT_HINTS_RETRY", "CLIENT_HINTS_FAILED",
                                         "SESSION_STOPPED", "CHROME_CLOSE_ASKED"])   # asked on both; on macOS nothing waits here (its starter does)
            self.assertEqual(seen["audit"][3]["reason"], "CLIENT_HINTS_FAILED")
            self.assertEqual(seen["audit"][2]["pages"], [{"error": "Target closed"}])
            self.assertEqual(seen["order"], ["forwarded notifications/initialized", "override", "override"])
            self.assertEqual(browser_runtime.guard_stopped(directory), "CLIENT_HINTS_FAILED")
            self.assertEqual(seen["ended"], [str(directory / "profile")])       # Chrome ended, the profile freed
            self.assertEqual(seen["exit"], 1)
            seen["proxy"].child.stdin.close.assert_called()                     # the browser server is stopped
            self.assertNotIn("FORWARDED", decisions)

    def test_an_override_with_no_answer_stops_it_too(self):
        directory = scratch(self)
        with mock.patch.object(browser_guard, "placed_windows", lambda value: None):
            seen = run_guard(self, directory, works=True)
        self.assertEqual(seen["forwarded"], [INITIALIZED])
        self.assertEqual(browser_runtime.guard_stopped(directory), "CLIENT_HINTS_FAILED")

    def test_one_failure_then_a_success_runs_the_session_normally(self):
        # V14: a slow first attach (no answer, or an error) is tried once more; only two failures stop it.
        for platform in ("darwin", "win32"):
            directory = scratch(self)
            seen = run_guard(self, directory, works=[False, True], platform=platform)
            self.assertEqual(seen["forwarded"], [INITIALIZED, NAVIGATE], platform)
            # The details are set before anything is forwarded to the browser's pages.
            self.assertEqual(seen["order"], ["forwarded notifications/initialized", "override", "override",
                                             "forwarded browser_navigate"])
            decisions = [row["decision"] for row in seen["audit"]]
            self.assertEqual(decisions[:4], ["BACKGROUND_LAUNCHED", "CLIENT_HINTS_RETRY", "CLIENT_HINTS_SET", "FORWARDED"])
            self.assertNotIn("SESSION_STOPPED", decisions)
            self.assertNotIn("CLIENT_HINTS_FAILED", decisions)
            self.assertIsNone(browser_runtime.guard_stopped(directory))            # no stop file
            self.assertEqual(seen["exit"], 0)
            self.assertIsNone(seen["proxy"].stop_reason)

    def test_control_when_the_override_is_set_the_session_runs_as_before(self):
        for platform in ("darwin", "win32"):
            directory = scratch(self)
            seen = run_guard(self, directory, works=True, platform=platform)
            self.assertEqual(seen["forwarded"], [INITIALIZED, NAVIGATE], platform)
            self.assertEqual(seen["order"].count("override"), 1)                   # one call only
            decisions = [row["decision"] for row in seen["audit"]]
            self.assertEqual(decisions[:3], ["BACKGROUND_LAUNCHED", "CLIENT_HINTS_SET", "FORWARDED"])
            self.assertNotIn("SESSION_STOPPED", decisions)
            self.assertIsNone(browser_runtime.guard_stopped(directory))
            self.assertEqual(seen["exit"], 0)
            self.assertEqual(seen["ended"], [str(directory / "profile")])

    def test_a_session_that_is_not_hidden_has_nothing_to_set_and_is_never_stopped(self):
        proxy = mock.Mock(headless=False, client_hints={"platform": "macOS"}, screen=None)
        self.assertTrue(browser_guard.Proxy.apply_client_hints(proxy))
        proxy.stop_session.assert_not_called()


class ApplicationSession(drill.Drill):
    """The session's launcher: a session its guard stopped is a failed one, whatever the AI answered."""

    def consume(self, request_id, works, arm=False, answer_it=True):
        seen = {}

        def scripted(config, prompt, directory, timeout):
            (directory / "trace.jsonl").write_text("")
            if arm:
                application_browser.ArmBinding(config["_application_guard_claim"]).arm(drill.FORM)
            seen.update(run_guard(self, directory, works, operation=config["_application_operation"]))
            if answer_it:   # an answer that would be accepted
                write_output(directory, request_at(directory), self.fixture.review())
        return application_browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                           request_id, scripted), seen

    def test_a_stopped_inspect_is_a_failed_session_and_the_job_is_untouched(self):
        before = self.fixture.job()["state"]
        summary, seen = self.consume(self.fixture.queue("inspect"), works=False)
        self.assertEqual(seen["forwarded"], [INITIALIZED])
        self.assertEqual(summary["code"], "APPLICATION_BROWSER_SETUP_STOPPED", summary)
        self.assertNotEqual(summary["state"], "COMPLETE")
        self.assertEqual(self.fixture.job()["state"], before)
        self.assertNotEqual(self.fixture.job()["state"], "SUBMITTED")
        directory, = {path.parent for path in self.fixture.root.rglob(browser_runtime.GUARD_STOPPED)}
        self.assertIn("SESSION_STOPPED", [json.loads(line)["decision"]
                                          for line in (directory / "guard.jsonl").read_text().splitlines()])

    def test_one_failure_then_a_success_completes_and_spends_no_attempt(self):
        before = self.fixture.job()
        summary, seen = self.consume(self.fixture.queue("inspect"), works=[False, True])
        self.assertEqual(seen["forwarded"], [INITIALIZED, NAVIGATE])
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertNotIn("code", summary)
        self.assertEqual(list(self.fixture.root.rglob(browser_runtime.GUARD_STOPPED)), [])
        review = self.fixture.adapter.call("inspect", {"job": self.fixture.job()})   # the read is delivered, no failure
        self.assertTrue(review["facts"]["open"])
        self.assertEqual(self.fixture.job()["attempts"], before["attempts"])

    def test_control_the_same_inspect_with_the_override_set_completes(self):
        summary, seen = self.consume(self.fixture.queue("inspect"), works=True)
        self.assertEqual(seen["forwarded"], [INITIALIZED, NAVIGATE])
        self.assertEqual(summary["state"], "COMPLETE", summary)

    def test_a_stopped_armed_submit_is_never_submitted_and_never_submitted_again(self):
        request_id = self.fixture.prepare_submit()
        summary, seen = self.consume(request_id, works=False, arm=True, answer_it=False)
        self.assertEqual(seen["forwarded"], [INITIALIZED])
        self.assertEqual((summary["state"], summary["requires_reconciliation"], summary["code"]),
                         ("UNKNOWN", True, "APPLICATION_BROWSER_SETUP_STOPPED"))
        self.assertIsNotNone(self.row(request_id)["armed_at"])
        self.assertEqual(self.fixture.job()["state"], "SUBMITTING")            # not SUBMITTED
        # The armed request is never handed out again, and the next round reads (reconcile), never submits.
        self.assertIsNone(desktop_adapter.claim(self.fixture.database, self.fixture.root, "next", applications.stamp(),
                                                request_id=request_id))
        applications.process(self.fixture.database, self.fixture.job(),
                             desktop_adapter.DesktopAdapter(self.fixture.database, self.fixture.root, self.fixture.config),
                             self.fixture.policy, self.fixture.root, applications.stamp())
        self.assertEqual([op for op, _ in self.operations()], ["submit", "reconcile"])


class Words(unittest.TestCase):
    """V13: a session the guard stopped has its own sentence, not the one for an AI that stopped."""

    def test_the_stop_has_its_own_true_sentence(self):
        import applications_page
        import failures_page
        tables = [value for value in vars(applications_page).values()
                  if isinstance(value, dict) and "APPLICATION_BROWSER_EXECUTOR_FAILED" in value]
        self.assertEqual(len(tables), 1)
        words = tables[0][browser_runtime.SETUP_STOPPED_CODE]
        self.assertEqual(words, "Sidedoor stopped this session because its browser could not be set up correctly.")
        self.assertNotEqual(words, tables[0]["APPLICATION_BROWSER_EXECUTOR_FAILED"])
        self.assertNotRegex(words, r"[A-Z]{3,}_[A-Z_]+|\u2014|automation|AI\b")
        self.assertIn(browser_runtime.SETUP_STOPPED_CODE, failures_page.EXTRA_FAILURE_CODES)   # a failed try, counted


class LiveStop(unittest.TestCase):
    """The launchers end the session as soon as the guard has stopped it, not when the AI gives up."""

    def test_the_history_launcher_fails_the_session_while_the_worker_is_still_running(self):
        directory = scratch(self)
        worker = mock.Mock()
        worker.communicate.side_effect = subprocess.TimeoutExpired("codex", 0.5)
        calls = []

        def clock():
            calls.append(1)
            if len(calls) == 3:   # the guard stops the session during the second wait
                (directory / browser_runtime.GUARD_STOPPED).write_text(json.dumps({"reason": "CLIENT_HINTS_FAILED"}))
            return 0.0
        with self.assertRaisesRegex(RuntimeError, "HISTORY_BROWSER_EXECUTOR_FAILED"):
            outreach_history_browser.communicate(worker, "prompt", 60, directory, ["codex"], clock=clock)
        self.assertLessEqual(worker.communicate.call_count, 2)

    def test_a_worker_that_finishes_after_the_guard_stopped_is_still_a_failed_session(self):
        directory = scratch(self)
        worker = mock.Mock()
        worker.communicate.side_effect = lambda *a, **k: (directory / browser_runtime.GUARD_STOPPED).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "HISTORY_BROWSER_EXECUTOR_FAILED"):
            outreach_history_browser.communicate(worker, "prompt", 60, directory, ["codex"])
        worker = mock.Mock()
        outreach_history_browser.communicate(worker, "prompt", 60, scratch(self), ["codex"])   # control
        worker.communicate.assert_called_once()

    def test_the_application_launcher_checks_the_guard_in_its_wait_loop(self):
        source = Path(application_browser.__file__).read_text()
        loop = source[source.index("def execute(config, prompt, directory, timeout):"):source.index("def leaves(")]
        self.assertLess(loop.index("browser_runtime.guard_stopped(directory)"), loop.index("worker.communicate("))


if __name__ == "__main__":
    unittest.main()
