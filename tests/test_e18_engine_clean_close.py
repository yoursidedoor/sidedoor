"""Confirming review of 9f8c4e8: on macOS an engine's guard still signalled Chrome in its own `finally`, before
the starter's clean close ran, so a sign-in made in a session's last seconds could be lost again. Now Chrome
gets the request to close before any signal, whoever sends it; the guard never waits while an engine may be
waiting for it; the starter waits in its `finally`; and Chrome is always stopped by someone.
A guard built as the product builds it, with Chrome's close, the wait and the signals recorded. No browser."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import application_browser
import browser_guard
import browser_runtime
import portable

ENDPOINT = "http://127.0.0.1:53111"


class Order(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)
        self.profile = str(self.directory / "profile")
        self.events = []
        self.gone = True          # Chrome exits when asked
        self.running = True
        clean = mock.patch.dict(os.environ)
        clean.start()
        self.addCleanup(clean.stop)
        os.environ.pop(browser_runtime.CLEAN_CLOSE_VARIABLE, None)   # a session an engine started

        self.asks = 0
        self.second_ask_refused = False   # Chrome mid-shutdown: its DevTools port no longer answers

        def request(endpoint):
            self.asks += 1
            if self.second_ask_refused and self.asks > 1:
                self.events.append("request refused")
                return False
            self.events.append("request")
            return True

        def wait(path, seconds):
            self.events.append("wait %g" % seconds)
            if self.gone:
                self.running = False
            return self.gone

        def signals(config, *rest, **named):
            self.events.append("signals")
            self.running = False
        for name, fake in (("close_browser", request), ("wait_chrome_gone", wait), ("end_session", signals),
                           ("chrome_running", lambda path: self.running)):
            patch = mock.patch.object(browser_runtime, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def platform(self, name):
        patch = mock.patch.object(browser_runtime.sys, "platform", name)
        patch.start()
        self.addCleanup(patch.stop)

    def guard(self, starter):
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            proxy = browser_guard.Proxy(
                "submit", ["node", "cli.js"], audit=self.directory / browser_runtime.GUARD_AUDIT, stdin=io.StringIO(""),
                stdout=io.StringIO(), focus_keeper=False, background_chrome=self.profile, starter_pid=starter,
                headless_user_agent="Mozilla/5.0 Chrome/141.0.0.0", launch_headless=lambda path, ua: ENDPOINT)
        self.addCleanup(proxy.audit.close)
        proxy.child = mock.Mock(poll=lambda: 1)
        return proxy

    def guard_ends(self, proxy):
        """What `Proxy.run` does when its engine lets go of it, in its order."""
        proxy.close_own_chrome()
        proxy.stop_background_chrome()

    def starter_ends(self, wait_seconds=browser_runtime.CLOSE_WAIT_SECONDS):
        """What `application_browser.execute` does in its `finally`, in its order."""
        config = {"browser_driver": {"profile": self.profile}, "_application_operation": "submit"}
        browser_runtime.close_cleanly(config, self.directory, wait_seconds)
        browser_runtime.end_session(config)

    def dead_pid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def test_request_then_the_starters_wait_then_signals(self):
        self.platform("darwin")
        self.guard_ends(self.guard(os.getpid()))
        # The guard asked, waited for nothing (its engine may be waiting for it) and sent no signal.
        self.assertEqual(self.events, ["request"])
        self.assertTrue(self.running)
        self.starter_ends()
        self.assertEqual(self.events, ["request", "request", "wait 10", "signals"])   # asked again (harmless), waited, then stopped
        self.assertFalse(self.running)

    def test_the_starter_waits_even_when_chrome_no_longer_takes_its_own_ask(self):
        # The normal end: the guard asked, Chrome is shutting down and its DevTools port is already closed, so
        # the starter's own ask is refused. Chrome is still running: the starter waits before any signal.
        self.platform("darwin")
        self.second_ask_refused = True
        self.guard_ends(self.guard(os.getpid()))
        self.starter_ends()
        self.assertEqual(self.events, ["request", "request refused", "wait 10", "signals"])
        # Nobody managed to ask (no record of the endpoint at all): the wait, then the old stop.
        del self.events[:]
        self.running = True
        (self.directory / browser_runtime.GUARD_AUDIT).unlink()
        self.starter_ends()
        self.assertEqual(self.events, ["wait 10", "signals"])
        # No Chrome on the profile: nothing to wait for.
        del self.events[:]
        self.starter_ends()
        self.assertEqual(self.events, ["signals"])

    def test_a_guard_that_was_stopped_before_it_could_ask_is_covered_by_the_starter(self):
        self.platform("darwin")
        self.guard(os.getpid())   # it started Chrome (its record names the endpoint) and was then stopped
        self.starter_ends()   # the engine did not wait for its guard: the starter's own request comes before its signals
        self.assertEqual(self.events, ["request", "wait 10", "signals"])

    def test_the_starter_has_died_so_the_guard_waits_and_stops_chrome_itself(self):
        self.platform("darwin")
        os.environ.pop(browser_runtime.STARTER_VARIABLE, None)
        self.guard_ends(self.guard(self.dead_pid()))   # the app was killed
        self.assertEqual(self.events, ["request", "wait 10", "signals"])
        self.assertFalse(self.running)
        # A guard that was not told its starter never waits (an engine may be waiting for it, inside the
        # session's time limit): asked, then stopped at once, as before this change.
        del self.events[:]
        self.asks, self.running = 0, True
        self.guard_ends(self.guard(None))
        self.assertEqual(self.events, ["request", "signals"])
        self.assertFalse(self.running)

    def test_chrome_ignores_the_request_and_is_still_stopped(self):
        self.platform("darwin")
        self.gone = False
        self.guard_ends(self.guard(os.getpid()))
        self.starter_ends()
        self.assertEqual(self.events[-2:], ["wait 10", "signals"])   # the starter's signals stop it
        self.assertFalse(self.running)
        del self.events[:]
        self.running = True
        self.guard_ends(self.guard(self.dead_pid()))                 # no starter: the guard's own
        self.assertEqual(self.events, ["request", "wait 10", "signals"])
        self.assertFalse(self.running)

    def test_the_live_views_guard_and_windows_are_as_before(self):
        # The live view's guard (it may wait): asks, waits, stops, all by itself.
        self.platform("darwin")
        os.environ[browser_runtime.CLEAN_CLOSE_VARIABLE] = "1"
        self.guard_ends(self.guard(os.getpid()))
        self.assertEqual(self.events, ["request", "wait 10", "signals"])
        # Windows: asks, waits on the profile's lockfile (5 s), then ends its Job Object; the starter's pid changes nothing.
        os.environ.pop(browser_runtime.CLEAN_CLOSE_VARIABLE)
        self.platform("win32")
        for starter in (os.getpid(), None):
            del self.events[:]
            waited = []
            with mock.patch.object(portable, "wait_profile_free", lambda path, seconds: waited.append(seconds)):
                self.guard_ends(self.guard(starter))
            self.assertEqual((self.events, waited), (["request", "signals"], [5.0]), starter)
        self.assertFalse(browser_runtime.close_cleanly({"browser_driver": {"profile": self.profile}}, self.directory))

    def test_the_starter_is_named_to_every_guard_and_its_wait_comes_out_of_unused_time(self):
        # Every engine starts the guard with `guard_environment` (browser_runtime.codex_options, engines.py).
        self.assertEqual(browser_runtime.guard_environment({"HOME": "/h", "SECRET": "x"}),
                         {"HOME": "/h", browser_runtime.STARTER_VARIABLE: str(os.getpid())})
        import engines
        import inspect
        self.assertEqual(inspect.getsource(engines).count("browser_runtime.guard_environment()"), 2)
        self.assertNotIn("browser_runtime.session_environment()", inspect.getsource(engines))
        self.assertIn("guard_environment()", inspect.getsource(browser_runtime.codex_options))
        with mock.patch.dict(os.environ, {browser_runtime.STARTER_VARIABLE: "4242"}):
            self.assertEqual(self.guard(None).starter_pid, 4242)   # the guard reads it
        # A session that finished with time to spare waits up to 10 s; one that ended at its limit waits for
        # nothing, so its result is handed in under its lease no later than before the wait existed.
        self.assertEqual(browser_runtime.close_wait(100.0, clock=lambda: 50.0), browser_runtime.CLOSE_WAIT_SECONDS)
        self.assertEqual(browser_runtime.close_wait(100.0, clock=lambda: 96.5), 3.5)
        self.assertEqual(browser_runtime.close_wait(100.0, clock=lambda: 130.0), 0.0)
        source = __import__("inspect").getsource(application_browser.execute)
        self.assertIn("close_cleanly(config, directory, browser_runtime.close_wait(deadline))", source)
        self.assertLess(source.index("close_cleanly(config, directory"), source.index("kill_tree(worker.pid)"))


if __name__ == "__main__":
    unittest.main()
