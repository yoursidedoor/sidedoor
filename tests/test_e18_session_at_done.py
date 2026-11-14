"""Found in the owner's real-browser check (2026-11-13): he signed in to LinkedIn in the live view, pressed Done
seconds later, and the step said "LinkedIn is not connected." with no reason and nothing sent. The session was
read only after the live view's Chrome had been stopped, from a new Chrome on the profile, where the cookie
was not yet written; and the redraw after Done read the account again and replaced the reason.

Here: the app as it builds itself, the live view's real start and end, a stand-in guard and Chrome (the contact
finder card's fixture). The Chrome holds the sign-in only in memory: any read after it stops finds nothing, as
on the owner's Mac. No browser, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import contextlib
import json
import re
import unittest
from unittest import mock

import browser_runtime
import linkedin_connection
import linkedin_contacts as lc
import linkedin_session
import setup_steps
import unipile_port
import test_e15_join as join
import test_e15_linkedin_sign_in as finder_card

MARKER = "AQEDAT-SIGNED-IN-SECONDS-AGO-31"
AGENT = "Mozilla/5.0 (Macintosh) Chrome/142.0 Safari/537.36"
NOT_SIGNED_IN = linkedin_connection.MESSAGES[linkedin_connection.NOT_SIGNED_IN][1]


class _Fixture(finder_card.LinkedInSignIn):
    """The finder card's fixture without its tests (they run in their own module)."""


for _name in [name for name in dir(finder_card.LinkedInSignIn) if name.startswith("test_")]:
    setattr(_Fixture, _name, None)


@unittest.skipUnless(join.DEPENDENCIES, "dashboard packages (fastapi, httpx, websockets) are not installed")
class SessionAtDone(_Fixture):
    def setUp(self):
        super().setUp()
        import license_fixtures
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        self.signed_in = True       # in the live view's running Chrome
        self.asked = []             # every DevTools method the running Chrome was asked
        answer = self.chrome._answer

        async def with_cookies(connection, message):
            method = message.get("method")
            self.asked.append(method)
            result = None
            if method == "Storage.getCookies":
                result = {"cookies": [{"name": "li_at", "value": MARKER, "domain": ".www.linkedin.com"}] if self.signed_in else
                          [{"name": "li_rm", "value": "remember", "domain": ".linkedin.com"}]}
            elif method == "Runtime.evaluate" and "userAgent" in str((message.get("params") or {}).get("expression")):
                result = {"result": {"type": "string", "value": AGENT}}
            if result is None:
                return await answer(connection, message)
            with contextlib.suppress(Exception):
                await connection.send(json.dumps({"id": message["id"], "result": result}))
        self.chrome._answer = with_cookies
        # After the live view's Chrome has stopped, a new Chrome on the profile finds no sign-in (not on disk).
        self.reads_after_stop = 0

        def from_disk(home):
            self.reads_after_stop += 1
            return None
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(from_disk))
        self.addCleanup(linkedin_connection._sent.clear)
        self.addCleanup(linkedin_connection._captured.clear)
        self.sent, self.calls = [], []

        def transport(method, path, **named):
            self.calls.append((method, path))
            if path == "/link/session":
                self.sent.append(dict(named["body"]))
                return 200, {"connected": True, "kind": "create"}
            if path == "/users/me":
                return 200, {"premium": False}
            if self.sent:
                return 200, {"items": [{"type": "LINKEDIN", "id": "acct-1", "name": "Alex", "status": "OK",
                                        "connection_params": {"im": {"publicIdentifier": "alex"}}}]}
            return 403, {"error": "LICENSE_NOT_LINKED"}
        patch = mock.patch.object(unipile_port, "RelayTransport", return_value=transport)
        patch.start()
        self.addCleanup(patch.stop)

    def step(self):
        return setup_steps.load_state(self.home)["steps"].get("linkedin", {})

    def open_from(self, path):
        """[Open Sidedoor's browser] on Setup or Settings: the live view block inside the step, and its target."""
        shown = self.client.post(path, headers=join.HTMX).text
        found = re.search(r"/live-view/([\w-]+)/fragment\?start=1", shown)
        self.assertIsNotNone(found, shown[:300])
        return found.group(1), shown

    def test_done_seconds_after_signing_in_connects_and_the_step_says_so(self):
        setup_steps.linkedin_status(self.home)
        self.assertEqual(self.step()["message"], "LinkedIn is not connected.")
        target, shown = self.open_from("/settings/linkedin/open")
        self.assertIn('hx-get="/settings/linkedin/status?after=done" hx-trigger="live-view-done"', shown)
        self.assertEqual(self.start_live(target)["state"], "live")
        self.assertEqual(self.end_live("done")["reason"], "done")   # pressed at once: nothing is on disk yet
        # Read from the step's own Chrome while it still ran, and sent: the link did not wait for the disk.
        self.assertEqual(self.sent, [{"session": MARKER, "user_agent": AGENT}])
        self.assertEqual(self.reads_after_stop, 0)
        self.assertIn("Storage.getCookies", self.asked)
        self.assertEqual((self.step()["status"], self.step()["message"]), ("done", "LinkedIn connected: Alex."))
        # The redraw the page asks for after Done shows that, and reads nothing again.
        before = len(self.calls)
        for path in ("/settings/linkedin/status?after=done", "/setup/linkedin/status?after=done"):
            redrawn = self.client.get(path, headers={"hx-request": "true"}).text
            self.assertIn("LinkedIn connected: Alex.", redrawn)
        self.assertEqual(len(self.calls), before)
        state = (self.home / "setup-state.json").read_text()
        self.assertNotIn(MARKER, state)
        self.assertEqual(linkedin_connection._captured, {})   # nothing of it is kept in memory after the send

    def test_done_without_a_sign_in_says_the_true_reason_and_the_redraw_keeps_it(self):
        self.signed_in = False
        setup_steps.linkedin_status(self.home)
        target, shown = self.open_from("/setup/linkedin/open")
        self.assertIn('hx-get="/setup/linkedin/status?after=done" hx-trigger="live-view-done"', shown)
        self.start_live(target)
        self.end_live("done")
        self.assertEqual(self.sent, [])
        self.assertEqual((self.step()["status"], self.step()["message"]), ("needs_you", NOT_SIGNED_IN))
        self.assertEqual(self.reads_after_stop, 0)   # the answer came from the Chrome the user was in
        import html
        for path in ("/setup/linkedin/status?after=done", "/settings/linkedin/status?after=done"):
            redrawn = html.unescape(self.client.get(path, headers={"hx-request": "true"}).text)
            self.assertIn("is not signed in to LinkedIn", redrawn, path)        # the reason, not the bare line
            self.assertNotIn("LinkedIn is not connected.", redrawn, path)
        # Before the fix this is what the page asked for after Done: a read, which replaced the reason.
        plain = html.unescape(self.client.get("/setup/linkedin/status", headers={"hx-request": "true"}).text)
        self.assertIn("LinkedIn is not connected.", plain)

    def test_when_the_running_chrome_cannot_be_read_the_ordinary_read_follows(self):
        setup_steps.linkedin_status(self.home)
        target, shown = self.open_from("/settings/linkedin/open")
        self.start_live(target)
        with mock.patch("linkedin_reader.read_running", side_effect=linkedin_session.Unavailable()):
            self.end_live("done")
        self.assertEqual(self.reads_after_stop, 1)
        self.assertEqual(self.step()["message"], NOT_SIGNED_IN)

    def test_cancel_reads_and_sends_nothing(self):
        setup_steps.linkedin_status(self.home)
        target, shown = self.open_from("/settings/linkedin/open")
        self.start_live(target)
        self.assertEqual(self.end_live("cancel")["reason"], "cancel")
        self.assertNotIn("Storage.getCookies", self.asked)
        self.assertEqual((self.sent, self.reads_after_stop), ([], 0))
        self.assertEqual(self.step()["message"], "LinkedIn is not connected.")
        self.assertEqual(linkedin_connection._captured, {})

    def test_the_one_linkedin_card_connects_outreach_from_the_same_read_and_reads_nothing_when_connected(self):
        setup_steps.save_sender(self.home, "acct-1", "https://www.linkedin.com/in/alex")
        # Both needs open: the finder's step, and outreach waiting on a sign-in.
        self.wall()
        linkedin_connection.hold(self.home)
        setup_steps.record(self.home, "linkedin", "needs_you", linkedin_connection.SIGN_IN_AGAIN, offer="reconnect")
        target, unused = self.press_step(lc.CARD_KEY)
        self.start_live(target)
        self.end_live("done")
        self.assertEqual(self.sent, [{"session": MARKER, "user_agent": AGENT}])
        self.assertEqual(self.reads_after_stop, 0)
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "SIGN_IN")])
        # Only the finder waiting, outreach connected: the cookies are not even asked for.
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        del self.asked[:], self.sent[:]
        self.wall()
        target, unused = self.press_step(lc.CARD_KEY)
        self.start_live(target)
        self.end_live("done")
        self.assertNotIn("Storage.getCookies", self.asked)
        self.assertEqual(self.sent, [])


class ChromeIsLeftToWriteItsCookies(unittest.TestCase):
    """Every guard session's end, on macOS too: Chrome is asked to close itself and waited for before it is
    stopped (tests/test_windows_hidden.py has the guard's own test)."""

    def test_wait_chrome_gone(self):
        running = [True, True, False]
        clock = [0.0]
        with mock.patch.object(browser_runtime, "chrome_running", lambda path: running.pop(0)):
            self.assertTrue(browser_runtime.wait_chrome_gone("p", 10, clock=lambda: clock[0], sleep=lambda s: None))
        with mock.patch.object(browser_runtime, "chrome_running", lambda path: True):
            def tick(seconds):
                clock[0] += 4
            self.assertFalse(browser_runtime.wait_chrome_gone("p", 10, clock=lambda: clock[0], sleep=tick))
        with mock.patch.object(browser_runtime, "chrome_running", side_effect=OSError):
            self.assertFalse(browser_runtime.wait_chrome_gone("p", 1))


class ApplicationSessionsCloseCleanlyOutsideTheirTimeLimit(unittest.TestCase):
    """Review round 1: an engine may wait for its guard, so the guard's own wait for Chrome would sit inside
    the session's time limit and could turn a finished session into a timeout. The close is the starter's:
    after the engine has exited and been judged, in `finally`, where it can change no outcome."""

    def run_session(self, worker, timeout, close, kill=True):
        import application_browser
        import pathlib
        import sys
        import tempfile
        self.events = []
        really_kill = application_browser.portable.kill_tree   # the mock records and really stops the worker
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(application_browser.browser_runtime, "require"), \
                mock.patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                mock.patch.object(application_browser, "command", return_value=[sys.executable, "-c", worker]), \
                mock.patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                mock.patch.object(application_browser.browser_runtime, "close_cleanly", side_effect=close, create=True), \
                mock.patch.object(application_browser.portable, "kill_tree", side_effect=lambda pid: (
                    self.events.append("stop the session's processes"), really_kill(pid) if kill else None)), \
                mock.patch.object(application_browser.browser_runtime, "end_session",
                                  side_effect=lambda config: self.events.append("stop what is left of Chrome")):
            return application_browser.execute({"_application_operation": "submit"}, "prompt", pathlib.Path(root), timeout)

    @unittest.skipIf(__import__("os").name == "nt", "POSIX process groups")
    def test_a_finished_session_is_never_timed_out_by_the_close(self):
        import time

        def slow_close(config, directory, wait_seconds=None):
            self.events.append("ask Chrome to close")
            time.sleep(2.5)   # longer than everything the session's time limit has left

        started = time.monotonic()
        self.run_session("pass", 2, slow_close)   # the engine exits at once: the session finished
        self.assertGreater(time.monotonic() - started, 2.4)   # the wait happened, and past the time limit
        self.assertEqual(self.events, ["ask Chrome to close", "stop the session's processes", "stop what is left of Chrome"])

    @unittest.skipIf(__import__("os").name == "nt", "POSIX process groups")
    def test_a_session_that_ran_out_of_time_is_still_a_timeout_and_chrome_is_still_closed(self):
        def close(config, directory, wait_seconds=None):
            self.events.append("ask Chrome to close")
        with self.assertRaisesRegex(TimeoutError, "APPLICATION_BROWSER_TIMEOUT"):
            self.run_session("import time; time.sleep(30)", 0.5, close)
        self.assertEqual(self.events[0], "ask Chrome to close")

    @unittest.skipIf(__import__("os").name == "nt", "POSIX process groups")
    def test_an_engine_that_will_not_exit_never_replaces_the_sessions_own_outcome(self):
        # Stopped at its time limit, and then it does not die within the 5 seconds it is given: the outcome is
        # still the session's own (the time limit), never a different error raised from the clean-up, and what
        # is left of Chrome is still stopped.
        with self.assertRaisesRegex(TimeoutError, "APPLICATION_BROWSER_TIMEOUT"):
            self.run_session("import time; time.sleep(7)", 0.5, lambda *given: None, kill=False)
        self.assertEqual(self.events, ["stop the session's processes", "stop what is left of Chrome"])
        # A finished session whose engine process lingers is still a finished session.
        import application_browser
        import subprocess
        real = subprocess.Popen.wait

        def stuck(process, timeout=None):
            if timeout == 5:
                raise subprocess.TimeoutExpired("engine", 5)
            return real(process, timeout)
        with mock.patch.object(application_browser.subprocess.Popen, "wait", stuck):
            self.run_session("pass", 5, lambda *given: None)   # nothing raised
        self.assertEqual(self.events[-1], "stop what is left of Chrome")

    def test_close_cleanly_asks_waits_and_never_raises(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / browser_runtime.GUARD_AUDIT).write_text(
                json.dumps({"decision": "BACKGROUND_LAUNCHED", "endpoint": "http://127.0.0.1:53111", "headless": True}) + "\n")
            config = {"browser_driver": {"profile": str(folder / "profile")}, "_application_operation": "submit"}
            seen = []
            with mock.patch.object(browser_runtime, "windows_host", return_value=False), \
                    mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                    mock.patch.object(browser_runtime, "close_browser", side_effect=lambda endpoint: seen.append(endpoint) or True), \
                    mock.patch.object(browser_runtime, "wait_chrome_gone",
                                      side_effect=lambda path, seconds: seen.append((str(path), seconds)) or True):
                self.assertTrue(browser_runtime.close_cleanly(config, folder))
                self.assertEqual(seen, ["http://127.0.0.1:53111", (str(folder / "profile"), browser_runtime.CLOSE_WAIT_SECONDS)])
                del seen[:]
                # Never for the user's draft window, a test's isolated profile, or a session with no Chrome of its own.
                shown = next(iter(browser_runtime.SHOWN_OPERATIONS))
                self.assertFalse(browser_runtime.close_cleanly(dict(config, _browser_operation=shown), folder))
                self.assertFalse(browser_runtime.close_cleanly({"browser_driver": {"isolated": True}}, folder))
                self.assertEqual(seen, [])
                # Chrome is still running but cannot be asked (no record of its endpoint, or the ask fails): it
                # is waited for all the same, before anything stops it (a Chrome someone asked is shutting down).
                waited = (str(folder / "profile"), browser_runtime.CLOSE_WAIT_SECONDS)
                self.assertTrue(browser_runtime.close_cleanly(config, folder / "nowhere"))
                self.assertEqual(seen, [waited])
                with mock.patch.object(browser_runtime, "close_browser", side_effect=RuntimeError("boom")):
                    self.assertTrue(browser_runtime.close_cleanly(config, folder))
                self.assertEqual(seen, [waited, waited])
                with mock.patch.object(browser_runtime, "wait_chrome_gone", side_effect=RuntimeError("boom")):
                    self.assertFalse(browser_runtime.close_cleanly(config, folder))   # never raises
            with mock.patch.object(browser_runtime, "windows_host", return_value=False), \
                    mock.patch.object(browser_runtime, "chrome_running", return_value=False), \
                    mock.patch.object(browser_runtime, "close_browser") as asked:
                self.assertFalse(browser_runtime.close_cleanly(config, folder))   # already gone: nothing to ask
                asked.assert_not_called()

    def test_every_engine_session_closes_before_it_stops_and_the_live_views_guard_is_told_it_may_wait(self):
        import inspect
        import application_browser
        import live_view
        import outreach_history_browser
        for module in (application_browser, outreach_history_browser):   # inspect, submit, the access step; the finder, the email check
            source = inspect.getsource(module.execute)
            self.assertLess(source.index("close_cleanly(config, directory"), source.index("kill_tree(worker.pid)"), module.__name__)
        self.assertIn("CLEAN_CLOSE_VARIABLE", inspect.getsource(live_view.Guard.start))
        import access_step
        self.assertIn("application_browser.execute", inspect.getsource(access_step))


if __name__ == "__main__":
    unittest.main()
