"""The live view (E15.T5): a person's sign-in or human check in Sidedoor's hidden Chrome, from the dashboard.
Fakes only (tests/live_view_fakes.py, tests/fake_live_guard.py): no browser, no Node, nothing off 127.0.0.1."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import datetime as dt
import gc
import importlib.util
import inspect
import re
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import browser_runtime
import failures_page
import live_view
import live_view_fakes as fakes
import paths
import run_locks
import test_dashboard_overview as base
import user_profile

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "websockets"))
URL = "https://jobs.example.test/apply"
MAIN = {"MAIN": {"targetId": "MAIN", "type": "page", "url": URL, "title": "Apply"}}
ORIGIN = {"origin": f"http://127.0.0.1:{base.PORT}"}
WS = f"ws://127.0.0.1:{base.PORT}"
ALLOWED_METHODS = {
    "Target.setDiscoverTargets", "Target.getTargets", "Target.attachToTarget", "Target.activateTarget",
    "Page.bringToFront", "Page.enable", "Page.startScreencast", "Page.stopScreencast", "Page.screencastFrameAck",
    "Runtime.evaluate", "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText"}


def key(character, code, kind="down", mod=0):
    return json.dumps({"t": "key", "type": kind, "key": character, "code": code, "mod": mod})


def move(x):
    return json.dumps({"t": "mouse", "type": "mouseMoved", "x": x, "y": 10, "mod": 0, "button": "none", "buttons": 0})


CLICK = json.dumps({"t": "mouse", "type": "mousePressed", "x": 5, "y": 5, "button": "left", "buttons": 1, "clicks": 1})


class FakeSocket:
    """A viewer's socket for LiveView.serve: what it was sent, and a way to leave."""

    def __init__(self):
        self.inbox, self.sent, self.closed = asyncio.Queue(), [], False

    async def receive_text(self):
        item = await self.inbox.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    async def send_text(self, text):
        self.sent.append(text)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self.inbox.put_nowait(None)

    def pictures(self):
        return [item for item in self.sent if isinstance(item, bytes)]

    def messages(self, kind):
        return [json.loads(item) for item in self.sent if isinstance(item, str) and json.loads(item)["t"] == kind]


async def until(check, seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.01)
    return bool(check())


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home, self.profile = Path(self.temp.name) / "home", Path(self.temp.name) / "profile"
        self.home.mkdir()
        self.config = {"browser_driver": {"profile": str(self.profile)}}
        self.chrome = fakes.FakeChrome(MAIN).start()
        self.addCleanup(self.chrome.stop)
        for name, value in (("FRONT_WATCH_SECONDS", 0.1), ("STATUS_SECONDS", 0.1)):
            patcher = mock.patch.object(live_view, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def view(self, **options):
        settings = dict(root=self.home, config=self.config, guard_command=fakes.guard_command(self.chrome),
                        is_supported=lambda config: True, leave_grace_seconds=30, lock_retry_seconds=0.2,
                        endpoint_wait_seconds=5)
        settings.update(options)
        return live_view.LiveView(**settings)

    async def live(self, view=None, **offer):
        view = view or self.view()
        session = await view.start(view.offer("job-1", URL, "sign_in", **offer))
        self.assertIsInstance(session, live_view.Session)
        self.addAsyncCleanup(view.end, live_view.CANCEL)
        return view, session

    def lock_free(self):
        found = []

        def probe():
            with run_locks.browser_session(self.home) as held:
                found.append(held)
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return found[0]

    def audit(self):
        path = paths.reports(self.home) / live_view.FOLDER / "session" / browser_runtime.GUARD_AUDIT
        return [json.loads(line) for line in path.read_text().splitlines()]

    def keys(self):
        return [row["params"] for row in self.chrome.methods("Input.dispatchKeyEvent")]


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class StartTest(Case):
    async def test_the_launch_is_the_products_inspect_session_and_nothing_is_added(self):
        """Item 1: Chrome comes from the product's own guard command for `inspect`; the second client only
        attaches, fronts the page, asks for frames and passes input (no user agent, emulation or network call)."""
        folder = self.home / "anywhere"
        with mock.patch.object(browser_runtime, "guard_command", return_value=["guard"]) as command:
            self.assertEqual(live_view.LiveView(root=self.home, config=self.config).guard_command(self.config, folder), ["guard"])
        command.assert_called_once_with(self.config, folder, "inspect")
        view, session = await self.live()
        self.assertEqual([row["arguments"] for row in self.audit() if row["decision"] == "FORWARDED"], [{"url": URL}])
        self.assertEqual(session.chrome.active, "MAIN")
        socket = FakeSocket()
        serving = asyncio.create_task(view.serve(session, socket))
        view.handle(session, CLICK)
        view.handle(session, key("a", "KeyA"))
        view.handle(session, json.dumps({"t": "paste", "text": "x"}))
        self.assertTrue(await until(lambda: self.chrome.methods("Input.insertText")))
        await view.end(live_view.DONE)
        await serving
        self.assertLessEqual({row["method"] for row in self.chrome.methods()}, ALLOWED_METHODS)

    async def test_one_live_session_at_a_time_and_only_for_an_offered_target(self):
        view, session = await self.live()
        other = view.offer("job-2", "https://other.example.test/", "human_check")
        refused = await view.start(other)
        self.assertEqual((refused.reason, refused.words), (live_view.OPEN, live_view.ONE_AT_A_TIME_WORDS))
        self.assertIs(await view.start(session.target.id), session)   # the same step opened again rejoins it
        self.assertEqual((await view.start("https://evil.example/")).reason, live_view.UNKNOWN)
        self.assertEqual(view.guard_command.starts, 1)
        self.assertEqual(view.offer("job-2", "https://other.example.test/", "human_check"), other)   # offered again: the same id
        for bad in ("http://plain.example.test/", "chrome://settings", "file:///etc/passwd", "javascript:alert(1)", ""):
            with self.assertRaises(ValueError):
                view.offer("job-3", bad, "sign_in")

    async def test_the_browser_lock_is_held_for_the_whole_session_and_released_by_every_ending(self):
        """Item 3: other browser work queues behind state/browser.lock exactly as behind any session (an
        application session takes run_locks.browser_session first); nothing else is locked."""
        self.assertTrue(self.lock_free())
        view, session = await self.live()
        self.assertFalse(self.lock_free())
        with run_locks.held(self.home, run_locks.RUNNER, run_locks.APPLICATIONS) as others:
            self.assertTrue(others)   # research, drafting and the applications job's own locks are untouched
        await view.end(live_view.CANCEL)
        self.assertTrue(self.lock_free())

    async def test_a_busy_browser_is_refused_in_plain_words_after_one_short_retry(self):
        view = self.view()
        with run_locks.held(self.home, run_locks.BROWSER) as held:
            self.assertTrue(held)
            started = time.monotonic()
            refused = await view.start(view.offer("job-1", URL, "sign_in"))
            self.assertGreaterEqual(time.monotonic() - started, 0.2)   # it waited once
        self.assertEqual((refused.reason, refused.words), (live_view.BUSY, live_view.BUSY_WORDS))
        self.assertEqual(view.guard_command.starts, 0)
        self.assertIsNone(view.session)

    async def test_a_chrome_on_the_profile_is_refused_before_anything_starts(self):
        view = self.view()
        with mock.patch.object(browser_runtime, "unavailable", return_value="PROFILE_IN_USE"):
            refused = await view.start(view.offer("job-1", URL, "sign_in"))
        self.assertEqual(refused.reason, live_view.WINDOW_OPEN)
        self.assertIn("Close the Sidedoor Chrome window", refused.words)
        self.assertEqual(view.guard_command.starts, 0)
        self.assertTrue(self.lock_free())

    async def test_a_step_that_cannot_be_shown_is_tried_twice_then_recorded_for_failures_and_metrics(self):
        """Item 7 (R12): no endpoint from the guard -> one retry -> plain words, a Failures row, a count."""
        database = base.database_with_schema(self.home)
        database.close()
        view = self.view(guard_command=fakes.guard_command(self.chrome, "--no-endpoint"), endpoint_wait_seconds=0.3)
        refused = await view.start(view.offer("job-1", URL, "sign_in"))
        self.assertEqual((refused.reason, refused.words), (live_view.NOT_SHOWN, live_view.NOT_SHOWN_WORDS))
        self.assertEqual(view.guard_command.starts, 2)
        self.assertTrue(self.lock_free())
        self.assertEqual([row["decision"] for row in self.audit()], ["STOPPED"])   # the guard was stopped again
        with run_locks.held(self.home, run_locks.BROWSER):   # and a busy browser, which is a wait, not a failure
            await view.start(view.offer("job-1", URL, "sign_in"))
        database = sqlite3.connect(paths.database(self.home))
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        counts = live_view.failure_counts(database)
        self.assertEqual((counts["total"], counts["busy"], counts["by_domain"]), (1, 1, {"jobs.example.test": 1}))
        shown = failures_page.gather(database, {"timezone": "UTC"}, {}, home=self.home)["needs_you"]
        self.assertEqual([(item["text"], item["todo"], item["count"]) for item in shown],
                         [(live_view.FAILURE_WORDS + " Site: jobs.example.test.", live_view.FAILURE_TODO, 1)])

    async def test_where_the_guard_starts_no_reachable_chrome_nothing_is_started(self):
        """G3, Windows today (live_view.supported): the step is not available, so a card shows no button; a
        direct start says so in plain words with no "try again", no Failures row and no count."""
        base.database_with_schema(self.home).close()
        self.assertFalse(browser_runtime.background_launch(self.config, platform="win32"))
        view = self.view(is_supported=live_view.supported)   # the suite's own setting: no background launch
        self.assertFalse(view.available("sign_in"))
        self.assertFalse(live_view.available("human_check", self.config))
        self.assertIsNone(view.offer("job-1", URL, "sign_in"))
        self.assertTrue(self.view().available("sign_in"))
        for target in ("anything", None):
            refused = await view.start(target)
            self.assertEqual((refused.reason, refused.words), (live_view.UNAVAILABLE, live_view.NOT_AVAILABLE_WORDS))
        self.assertNotIn("try again", live_view.NOT_AVAILABLE_WORDS.lower())
        self.assertEqual(view.guard_command.starts, 0)
        database = sqlite3.connect(paths.database(self.home))
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        counts = live_view.failure_counts(database)
        self.assertEqual((counts["total"], counts["busy"], counts["by_reason"]), (0, 0, {}))
        self.assertEqual(failures_page.gather(database, {"timezone": "UTC"}, {}, home=self.home)["needs_you"], [])


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class LeaseAndEndingTest(Case):
    def test_lease_is_ten_minutes_renewed_by_activity_and_thirty_at_most(self):
        lease = live_view.Lease(1000.0)
        self.assertEqual(lease.ends, 1600.0)
        lease.touch(1500.0)
        self.assertEqual(lease.ends, 2100.0)
        lease.touch(2099.0)
        self.assertEqual(lease.ends, 2699.0)
        lease.touch(2600.0)
        self.assertEqual(lease.ends, 2800.0)   # 30 minutes from the start, however active
        self.assertEqual((live_view.LEASE_SECONDS, live_view.LEASE_MAX_SECONDS), (600, 1800))

    async def test_the_lease_ends_an_idle_session_and_input_renews_it_up_to_the_cap(self):
        ended = []
        view, session = await self.live(self.view(lease_seconds=0.5, lease_max_seconds=1.3), on_ended=ended.append)
        for _ in range(4):   # active for 0.8 s: past the first lease end
            await asyncio.sleep(0.2)
            view.handle(session, CLICK)
        self.assertIs(view.session, session)
        for _ in range(4):   # still active, but the cap is the cap
            await asyncio.sleep(0.2)
            if view.session is session:
                view.handle(session, CLICK)
        self.assertTrue(await until(lambda: ended))
        self.assertEqual((ended[0].reason, ended[0].words), (live_view.LEASE, "Timed out. Press Sign in when you're ready."))
        self.assertLess(ended[0].seconds, 1.9)
        self.assertTrue(self.lock_free())
        self.assertIsNotNone(view.target(session.target.id))   # still waiting: the card can open it again

    async def test_done_ends_the_way_a_product_session_ends_and_calls_the_site_session_hook(self):
        """Item 4: frames stopped, second client closed, guard stopped, Chrome ended through
        browser_runtime.end_session, lock released, a result record; the hook runs while the guard is alive."""
        seen, ended = [], []

        def hook(domain, job):
            seen.append((domain, job, [row["decision"] for row in self.audit()], self.lock_free()))
        view, session = await self.live(on_ended=ended.append)
        with mock.patch.object(live_view, "on_user_step_finished", hook), \
                mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session", return_value=True) as end_session:
            result = await view.end(live_view.DONE)
        self.assertEqual(seen, [("jobs.example.test", "job-1", ["BACKGROUND_LAUNCHED", "FORWARDED"], False)])
        end_session.assert_called_once_with(self.config)
        self.assertEqual((result.reason, result.final_url, result.domain, result.job), ("done", URL, "jobs.example.test", "job-1"))
        self.assertEqual(ended, [result])
        self.assertEqual(self.chrome.methods("Page.stopScreencast")[-1]["session"], "S-MAIN")
        self.assertTrue(await until(lambda: not self.chrome.connections))
        self.assertEqual(self.audit()[-1]["decision"], "STOPPED")
        self.assertEqual(session.guard.process.returncode, 0)
        self.assertTrue(self.lock_free())
        self.assertIsNone(view.session)
        self.assertIsNone(view.target(session.target.id))
        self.assertIsNone(await view.end(live_view.DONE))   # a second ending finds nothing to end

    async def test_cancel_leaving_the_check_hook_and_a_lost_browser_each_end_it(self):
        seen = []
        with mock.patch.object(live_view, "on_user_step_finished", lambda domain, job: seen.append(domain)):
            view, session = await self.live()
            self.assertEqual((await view.end(live_view.CANCEL)).reason, live_view.CANCEL)
            self.assertEqual(seen, [])   # nothing to save after a cancel

            ended = []   # the person left: the socket closed and did not come back within the grace
            view, session = await self.live(self.view(leave_grace_seconds=0.3), on_ended=ended.append)
            socket = FakeSocket()
            serving = asyncio.create_task(view.serve(session, socket))
            await asyncio.sleep(0.4)
            self.assertIs(view.session, session)   # a viewer is there: no ending
            socket.inbox.put_nowait(None)
            await serving
            self.assertTrue(await until(lambda: ended))
            self.assertEqual(ended[0].reason, live_view.LEFT)
            self.assertEqual(seen, [])

            pages, ended = [], []   # the seam for E15.T4: a check hook says the page reached its target state

            def check(page):
                pages.append(page)
                return len(pages) >= 2
            view, session = await self.live(self.view(check_seconds=0.05), check=check, on_ended=ended.append)
            self.assertTrue(await until(lambda: ended))
            self.assertEqual((ended[0].reason, ended[0].final_url), (live_view.FINISHED, URL))
            self.assertEqual((pages[0].url, pages[0].domain, pages[0].job), (URL, "jobs.example.test", "job-1"))
            self.assertEqual(seen, ["jobs.example.test"])

            ended = []   # Chrome went away under the session
            view, session = await self.live(on_ended=ended.append)
            await asyncio.to_thread(self.chrome.drop_clients)
            self.assertTrue(await until(lambda: ended))
            self.assertEqual(ended[0].reason, live_view.CLOSED)
        self.assertTrue(self.lock_free())


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class LessonsTest(Case):
    async def test_lesson_1_input_is_never_awaited(self):
        """Chrome answers every mouse event after 5 s. 200 moves and 20 keys still go out at once and in
        order; only moves are ever dropped; the handler never blocks; the lease moves."""
        view, session = await self.live()
        self.chrome.delays["Input.dispatchMouseEvent"] = 5
        before, started = session.lease.ends, time.monotonic()
        typed = "abcdefghijklmnopqrst"
        for index in range(200):
            view.handle(session, move(index))
        for letter in typed:
            view.handle(session, key(letter, "Key" + letter.upper()))
        view.handle(session, CLICK)
        self.assertLess(time.monotonic() - started, 0.5)   # the handler never waited on Chrome
        self.assertTrue(await until(lambda: len(self.keys()) == 20, 1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual("".join(params["text"] for params in self.keys()), typed)
        mouse = [row["params"]["type"] for row in self.chrome.methods("Input.dispatchMouseEvent")]
        self.assertEqual(mouse.count("mouseMoved"), live_view.MAX_OUTSTANDING)   # the bounded queue
        self.assertEqual(mouse.count("mousePressed"), 1)                         # a click is never dropped
        self.assertEqual(session.chrome.counts["moves_dropped"], 200 - live_view.MAX_OUTSTANDING)
        self.assertGreater(session.chrome.outstanding, live_view.MAX_OUTSTANDING)
        self.assertGreater(session.lease.ends, before)
        self.assertFalse(inspect.iscoroutinefunction(live_view.LiveView.handle))
        self.assertFalse(inspect.iscoroutinefunction(live_view.Chrome.input))
        self.assertEqual(live_view.status_words(True, session.chrome.outstanding, 0), live_view.SLOW_WORDS)

    async def test_lesson_2_the_shown_page_is_kept_in_front_and_popups_are_followed(self):
        view, session = await self.live()
        shown = [(row["method"], row["params"].get("targetId"), row["session"]) for row in self.chrome.methods()
                 if row["method"] in ("Target.activateTarget", "Page.bringToFront")]
        self.assertEqual(shown[:2], [("Target.activateTarget", "MAIN", None), ("Page.bringToFront", None, "S-MAIN")])
        self.chrome.take_front("SOMETHING-ELSE")   # the page is hidden: a mouse event would now take 5 s
        self.assertTrue(await until(lambda: self.chrome.front == "MAIN", 2.0))   # the one-second watchdog
        self.assertGreaterEqual(session.chrome.counts["refronted"], 1)
        socket = FakeSocket()
        asyncio.create_task(view.serve(session, socket))
        self.chrome.open_page("POPUP", "", opener="MAIN")   # "Sign in with Google": a pop-up, hidden at first
        self.assertTrue(await until(lambda: session.chrome.active == "POPUP" and self.chrome.front == "POPUP"))
        self.assertTrue(await until(lambda: any(b"S-POPUP" in picture for picture in socket.pictures())))
        view.handle(session, CLICK)
        self.assertTrue(await until(lambda: [row for row in self.chrome.methods("Input.dispatchMouseEvent")
                                             if row["session"] == "S-POPUP"]))
        self.chrome.close_page("POPUP")   # the pop-up closes itself: back to the page it came from
        self.assertTrue(await until(lambda: session.chrome.active == "MAIN" and self.chrome.front == "MAIN"))
        self.chrome.open_page("TAB", "https://idp.example.test/login")   # a new tab with no opener
        self.assertTrue(await until(lambda: session.chrome.active == "TAB"))

    async def test_lesson_3_chrome_pages_are_never_shown_or_followed(self):
        view, session = await self.live()
        self.chrome.open_page("HELP", "chrome://settings/help", take_front=True)   # the profile opens it itself
        self.chrome.open_page("NEWTAB", "chrome://newtab/", opener="MAIN")
        self.chrome.open_page("TOOLS", "devtools://devtools/bundled/inspector.html")
        self.assertTrue(await until(lambda: self.chrome.front == "MAIN", 2.0))
        attached = [row["params"]["targetId"] for row in self.chrome.methods("Target.attachToTarget")]
        self.assertEqual((attached, session.chrome.active), (["MAIN"], "MAIN"))
        self.chrome.open_page("POPUP", "", opener="MAIN")
        self.assertTrue(await until(lambda: session.chrome.active == "POPUP"))
        self.chrome.change("POPUP", "chrome://whats-new/")   # a followed page turns into one of Chrome's own
        self.assertTrue(await until(lambda: session.chrome.active == "MAIN"))
        for url, expected in (("https://a.example/", True), ("http://a.example/", True), ("", True), ("about:blank", True),
                              ("chrome://help", False), ("chrome-extension://x/y.html", False),
                              ("chrome-untrusted://new-tab-page/", False), ("devtools://x", False), (None, False)):
            self.assertEqual(live_view.shown_url(url), expected, url)

    async def test_lesson_4_key_codes_come_from_the_physical_key_never_the_character(self):
        view, session = await self.live()
        view.handle(session, key(".", "Period"))
        view.handle(session, key("Delete", "Delete"))
        view.handle(session, key("a", "KeyQ"))   # an AZERTY keyboard: the character is a, the key is Q's
        view.handle(session, key("@", "Digit2", mod=live_view.SHIFT))
        view.handle(session, key("Enter", "Enter"))
        self.assertTrue(await until(lambda: len(self.keys()) == 5))
        got = [(params["key"], params["windowsVirtualKeyCode"], params.get("text"), params["type"]) for params in self.keys()]
        self.assertEqual(got, [(".", 190, ".", "keyDown"), ("Delete", 46, None, "rawKeyDown"), ("a", 81, "a", "keyDown"),
                               ("@", 50, "@", "keyDown"), ("Enter", 13, "\r", "keyDown")])
        self.assertNotEqual(live_view.virtual_key(".", "Period"), ord("."))   # 46 is Delete
        self.assertEqual([live_view.virtual_key("", code) for code in ("Comma", "Slash", "Numpad7", "Space", "KeyZ")],
                         [188, 191, 103, 32, 90])
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        self.assertIn("code: event.code", viewer)
        self.assertNotIn("keyCode", viewer)   # the viewer sends the physical key; only the server maps it

    async def test_lesson_5_the_devtools_reader_never_waits(self):
        """Following a pop-up from inside the reader deadlocked the spike. Here the attach takes a second and
        the pictures of the shown page keep arriving meanwhile."""
        self.assertFalse(inspect.iscoroutinefunction(live_view.Chrome._on_message))
        view, session = await self.live()
        socket = FakeSocket()
        asyncio.create_task(view.serve(session, socket))
        self.assertTrue(await until(lambda: socket.pictures()))
        self.chrome.delays["Target.attachToTarget"] = 1.0
        self.chrome.open_page("POPUP", "", opener="MAIN")
        self.assertTrue(await until(lambda: self.chrome.methods("Target.attachToTarget")[-1]["params"]["targetId"] == "POPUP"))
        seen, started = len(socket.pictures()), time.monotonic()
        self.chrome.frame("MAIN")
        self.assertTrue(await until(lambda: len(socket.pictures()) > seen, 0.5))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(session.chrome.active, "MAIN")   # the attach has not answered yet
        self.assertTrue(await until(lambda: session.chrome.active == "POPUP", 3.0))

    async def test_lesson_6_command_combinations_and_function_keys_stay_away_from_chrome(self):
        view, session = await self.live()
        before = session.lease.ends
        for character, code, mod in (("q", "KeyQ", live_view.META), ("t", "KeyT", live_view.CTRL), ("Meta", "MetaLeft", live_view.META),
                                     ("l", "KeyL", live_view.META), ("w", "KeyW", live_view.CTRL | live_view.SHIFT),
                                     ("v", "KeyV", live_view.META), ("F5", "F5", 0), ("F12", "F12", 0)):
            for kind in ("down", "up"):
                view.handle(session, key(character, code, kind, mod))
        self.assertEqual(session.lease.ends, before)   # nothing was passed on, so nothing renewed the lease
        for character, mod in (("a", live_view.META), ("c", live_view.CTRL), ("x", live_view.META), ("z", live_view.META)):
            view.handle(session, key(character, "Key" + character.upper(), "down", mod))
        view.handle(session, json.dumps({"t": "paste", "text": "pasted"}))
        self.assertTrue(await until(lambda: self.chrome.methods("Input.insertText")))
        self.assertEqual([(params["key"], params.get("commands"), params.get("text"), params["type"]) for params in self.keys()],
                         [("a", ["selectAll"], None, "rawKeyDown"), ("c", ["copy"], None, "rawKeyDown"),
                          ("x", ["cut"], None, "rawKeyDown"), ("z", ["undo"], None, "rawKeyDown")])
        self.assertEqual(session.chrome.counts["keys_not_sent"], 16)

    async def test_lesson_7_a_viewer_gets_a_picture_on_joining_and_a_status_line(self):
        view, session = await self.live(self.view(leave_grace_seconds=5))
        first = FakeSocket()
        serving = asyncio.create_task(view.serve(session, first))
        self.assertTrue(await until(lambda: first.pictures() and first.messages("status")))   # a still page, yet a picture
        self.assertEqual(first.messages("meta")[0], {"t": "meta", "width": 1280, "height": 900})
        self.assertEqual(first.messages("status")[0]["words"], live_view.LIVE_WORDS)
        first.inbox.put_nowait(None)   # the socket drops
        await serving
        again = FakeSocket()   # the viewer reconnects within the grace: same session, and a picture at once
        asyncio.create_task(view.serve(session, again))
        self.assertTrue(await until(lambda: again.pictures() and again.messages("meta")))
        self.assertIs(view.session, session)
        self.chrome.close_page("MAIN")
        self.assertTrue(await until(lambda: any(row["words"] == live_view.NO_PAGE_WORDS for row in again.messages("status"))))
        self.assertEqual(live_view.status_words(True, 0, 45), "No new picture for 45 seconds. The page may be loading.")
        result = await view.end(live_view.CANCEL)
        self.assertEqual(again.messages("ended"), [{"t": "ended", "reason": "cancel", "words": result.words}])
        self.assertTrue(again.closed)
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        self.assertIn("setTimeout(connect, 1500)", viewer)   # the page's own reconnect, to the same session
        self.assertEqual(viewer.count("/start"), 1)           # only the person's click ever starts one

    def test_every_sentence_a_person_sees_is_in_house_style(self):
        self.assertFalse(hasattr(live_view, "APPLY_YOURSELF_WORDS"))   # D20: nothing said it; the card says the passkey sentence
        assets = Path(live_view.__file__).parent / "dashboard_assets"
        pages = [(assets / "templates" / name).read_text() for name in ("live_view.html", "live_view_fragment.html")]
        shown = [re.sub(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]*>", " ", page) for page in pages]   # the words, not the markup
        for name in ("aria-label", "data-opening", "data-reconnecting", "data-not-shown", "data-over"):
            self.assertIn(name, pages[1])
        shown.append(" ".join(re.findall(r'aria-label="([^"]*)"', pages[1])))
        for words in live_view.ALL_WORDS + tuple(shown):
            lowered = words.lower()
            for banned in ("—", "–", "devtools", "cdp", "headless", "lock", "guard", "automation", "websocket",
                           "socket", "screencast"):
                self.assertNotIn(banned, lowered, words)


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class ReviewFixesTest(Case):
    async def test_g1_a_chrome_with_a_window_is_never_attached_to(self):
        """The guard's fallback (BACKGROUND_LAUNCHED without headless: a minimized window it keeps minimized):
        the start fails in plain words, once, and everything it started is stopped."""
        base.database_with_schema(self.home).close()
        view = self.view(guard_command=fakes.guard_command(self.chrome, "--windowed"))
        with mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session", return_value=True) as end_session:
            refused = await view.start(view.offer("job-1", URL, "sign_in"))
        self.assertEqual((refused.reason, refused.words, refused.final), (live_view.NOT_SHOWN, live_view.NO_BACKGROUND_WORDS, True))
        self.assertNotIn("again", refused.words.lower())   # P5: a retry would only open that window again
        database = sqlite3.connect(paths.database(self.home))
        self.addCleanup(database.close)
        self.assertEqual(live_view.failure_counts(database)["by_reason"], {live_view.NOT_SHOWN: 1})   # still a failure
        self.assertEqual(view.guard_command.starts, 1)   # a second try would only open that window again
        self.assertEqual(self.chrome.methods(), [])      # nothing was attached, fronted or asked for pictures
        self.assertEqual([row["decision"] for row in self.audit()], ["HEADLESS_LAUNCH_FAILED", "BACKGROUND_LAUNCHED", "STOPPED"])
        end_session.assert_called_once_with(self.config)
        self.assertTrue(self.lock_free())
        self.assertIsNone(view.session)

    async def test_g2_the_site_shown_is_named_above_the_view_and_follows_every_change(self):
        view, session = await self.live()
        socket = FakeSocket()
        asyncio.create_task(view.serve(session, socket))

        def named():
            return [row["words"] for row in socket.messages("site")] + [row["site"] for row in socket.messages("status")]
        self.assertTrue(await until(lambda: "Now showing: jobs.example.test" in named()))
        self.chrome.change("MAIN", "https://accounts.google.com/signin?token=TOKEN-IN-QUERY#frag")   # a redirect
        other = "Now showing: accounts.google.com (opened from jobs.example.test)"
        self.assertTrue(await until(lambda: other in [row["words"] for row in socket.messages("site")]))
        self.chrome.open_page("POPUP", "http://plain.example.test/login/path", opener="MAIN")       # a pop-up, plain http
        plain = "Not secure. Now showing: plain.example.test (opened from jobs.example.test)"
        self.assertTrue(await until(lambda: plain in named()))
        self.chrome.close_page("POPUP")
        self.assertTrue(await until(lambda: named()[-1] == other))
        for text in (item for item in socket.sent if isinstance(item, str)):
            for hidden in ("TOKEN-IN-QUERY", "/signin", "/login/path", "frag"):
                self.assertNotIn(hidden, text)   # never the path or the query
        for url, words in (
                ("https://jobs.example.test@evil.example/a", "Now showing: evil.example (opened from jobs.example.test)"),
                ("https://xn--pple-43d.com/", "Now showing: xn--pple-43d.com (opened from jobs.example.test)"),
                ("https://JOBS.example.test:8443/x?y=1", "Now showing: jobs.example.test"),
                ("about:blank", "Now showing: a blank page"), ("", "Now showing: a blank page"), (None, "Now showing: a blank page")):
            self.assertEqual(live_view.site_words(url, "jobs.example.test"), words)
        hostile = live_view.site_words("https://<img src=x onerror=alert(1)>.evil.example/<script>", "jobs.example.test")
        self.assertNotIn("<script>", hostile)   # the path never shows; whatever the host is, it is shown as text:
        assets = Path(live_view.__file__).parent / "dashboard_assets"
        viewer = (assets / "static" / "live_view.js").read_text()
        self.assertIn("site.textContent = words", viewer)
        self.assertNotIn("innerHTML", viewer)
        self.assertNotIn("insertAdjacentHTML", viewer)
        fragment = (assets / "templates" / "live_view_fragment.html").read_text()
        self.assertLess(fragment.index("data-live-site"), fragment.index("data-live-canvas"))   # above the picture
        self.assertNotIn("|safe", fragment)

    async def test_p1_cancel_during_a_failed_starts_teardown_waits_for_the_whole_teardown(self):
        base.database_with_schema(self.home).close()
        view = self.view(guard_command=fakes.guard_command(self.chrome, "--no-endpoint"), endpoint_wait_seconds=0.3)
        calls = []

        def slow_stop():
            calls.append("in")
            calls.append(("lock free", self.lock_free()))
            time.sleep(0.8)
            calls.append("out")
        view._stop_chrome = slow_stop
        starting = asyncio.create_task(view.start(view.offer("job-1", URL, "sign_in")))
        self.assertTrue(await until(lambda: calls))
        result = await view.end(live_view.CANCEL)
        self.assertEqual(calls, ["in", ("lock free", False), "out"])   # it returned only after Chrome was stopped
        self.assertEqual(result.reason, live_view.CANCEL)
        self.assertEqual((await starting).reason, live_view.CANCEL)
        self.assertTrue(self.lock_free())
        self.assertEqual(view.guard_command.starts, 1)

    async def test_p1_two_cancels_and_two_endings_share_one_teardown(self):
        view = self.view(guard_command=fakes.guard_command(self.chrome, "--hang"))
        with mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session", return_value=True) as end_session:
            starting = asyncio.create_task(view.start(view.offer("job-1", URL, "sign_in")))
            await asyncio.sleep(0.5)
            first = asyncio.create_task(view.end(live_view.CANCEL))   # two tabs, two Cancels
            await asyncio.sleep(0.002)
            second = await view.end(live_view.CANCEL)
            self.assertEqual(((await first).reason, second.reason, (await starting).reason), ("cancel", "cancel", "cancel"))
            self.assertEqual(end_session.call_count, 1)
            self.assertEqual(self.audit()[-1]["decision"], "STOPPED")
            self.assertTrue(self.lock_free())
            end_session.reset_mock()
            view.guard_command = fakes.guard_command(self.chrome)   # a live session: Done and Cancel at once
            session = await view.start(view.offer("job-1", URL, "sign_in"))
            done = asyncio.create_task(view.end(live_view.DONE))
            await asyncio.sleep(0)
            self.assertIs(await view.end(live_view.CANCEL), await done)   # the same ending, the same record
            self.assertEqual(((await done).reason, end_session.call_count), (live_view.DONE, 1))
            end_session.reset_mock()
            session = await view.start(view.offer("job-1", URL, "sign_in"))   # whoever asked goes away mid-ending
            stop_chrome = view._stop_chrome
            view._stop_chrome = lambda: (time.sleep(0.3), stop_chrome())
            asked = asyncio.create_task(view.end(live_view.CANCEL))
            await asyncio.sleep(0.01)
            asked.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asked
            self.assertEqual(end_session.call_count, 1)   # it waited for the ending, which ran to the end
            self.assertIsNone(view.session)
            self.assertEqual(session.guard.process.returncode, 0)
        self.assertTrue(self.lock_free())

    async def test_p8_quit_returns_within_its_bound_however_long_the_ending_takes(self):
        view, session = await self.live()
        ending, finished = view._end, []

        async def slow(reason, current):
            await asyncio.sleep(1.5)
            finished.append(await ending(reason, current))
        view._end = slow
        started = time.monotonic()
        await view.quit(seconds=0.3)
        self.assertLess(time.monotonic() - started, 0.8)   # Quit went on; it did not wait for the ending
        self.assertEqual(finished, [])
        self.assertTrue(await until(lambda: finished, 5.0))   # (here the loop lives on, so the ending completes)
        self.assertEqual(finished[0].reason, live_view.CLOSED)
        self.assertTrue(self.lock_free())
        started = time.monotonic()
        await view.quit(seconds=0.3)   # nothing live: nothing to wait for
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_p8_a_hung_card_hook_cannot_keep_the_browser_held(self):
        hung = threading.Event()
        self.addCleanup(hung.set)
        self.assertEqual(live_view.HOOK_SECONDS, 60)
        with mock.patch.object(live_view, "HOOK_SECONDS", 0.3), \
                mock.patch.object(live_view, "on_user_step_finished", lambda domain, job: hung.wait(30)):
            view, session = await self.live(on_ended=lambda result: hung.wait(30))
            started = time.monotonic()
            result = await view.end(live_view.DONE)   # both hooks hang
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result.reason, live_view.DONE)
        self.assertEqual(self.audit()[-1]["decision"], "STOPPED")
        self.assertTrue(self.lock_free())
        self.assertIsNone(view.session)

    async def test_r4_an_ending_for_a_new_session_is_never_answered_with_an_older_sessions(self):
        """Session 1's last hook hangs; meanwhile the browser is free and session 2 starts. Done on session 2
        ends session 2 (its own teardown, its own record)."""
        hung = threading.Event()
        self.addCleanup(hung.set)
        view = self.view()
        with mock.patch.object(live_view, "HOOK_SECONDS", 20):
            one = await view.start(view.offer("job-1", URL, "sign_in", on_ended=lambda result: hung.wait(20)))
            first = asyncio.create_task(view.end(live_view.DONE))
            self.assertTrue(await until(lambda: view.session is None))
            self.assertTrue(self.lock_free())
            self.assertFalse(first.done())   # still in its hook
            two = await view.start(view.offer("job-2", URL + "?2", "sign_in"))
            self.assertIsInstance(two, live_view.Session)
            self.assertIsNot(two, one)
            second = await asyncio.wait_for(view.end(live_view.DONE), 10)
            self.assertEqual((second.job, second.reason), ("job-2", live_view.DONE))
            self.assertIsNone(view.session)
            self.assertEqual(two.guard.process.returncode, 0)
            self.assertTrue(self.lock_free())
            three = await view.start(view.offer("job-3", URL + "?3", "sign_in"))   # and an ending by itself
            view._lost(three)
            self.assertTrue(await until(lambda: view.session is None))
            self.assertEqual(view.last.job, "job-3")
            hung.set()
            self.assertEqual((await first).job, "job-1")
        self.assertTrue(self.lock_free())

    async def test_r4_an_ending_cancelled_during_a_hook_still_stops_everything(self):
        """Quit while a hook is waited for: the event loop cancels the ending. Chrome and the guard are still
        stopped and the browser let go. The same when the loop closes under the teardown itself."""
        hung = threading.Event()
        self.addCleanup(hung.set)
        entered = []

        def hook(domain, job):
            entered.append(1)
            hung.wait(20)
        with mock.patch.object(live_view, "HOOK_SECONDS", 20), mock.patch.object(live_view, "on_user_step_finished", hook), \
                mock.patch.object(browser_runtime, "chrome_running", return_value=True), \
                mock.patch.object(browser_runtime, "end_session", return_value=True) as end_session:
            view = self.view()
            session = await view.start(view.offer("job-1", URL, "sign_in"))
            asked = asyncio.create_task(view.end(live_view.DONE))
            self.assertTrue(await until(lambda: entered))
            session.ending.cancel()   # what a closing event loop does to every task
            with self.assertRaises(asyncio.CancelledError):
                await asked
            self.assertEqual(end_session.call_count, 1)
            self.assertEqual(self.audit()[-1]["decision"], "STOPPED")
            self.assertEqual(session.guard.process.returncode, 0)
            self.assertIsNone(view.session)
            self.assertTrue(self.lock_free())
            end_session.reset_mock()
            hung.set()
            view = self.view()   # the loop closes while the teardown itself is waiting on the guard
            session = await view.start(view.offer("job-1", URL, "sign_in"))

            async def never(grace=15.0):
                await asyncio.sleep(30)
            session.guard.stop = never
            asked = asyncio.create_task(view.end(live_view.CANCEL))
            self.assertTrue(await until(lambda: session.teardown is not None))
            await asyncio.sleep(0.05)
            session.teardown.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asked
            self.assertEqual(end_session.call_count, 1)
            self.assertTrue(self.lock_free())
            self.assertTrue(await until(lambda: session.guard.process.returncode is not None))   # that one process, ended

    async def test_p4_a_blank_window_opened_by_a_page_is_named_by_that_page(self):
        view, session = await self.live()
        socket = FakeSocket()
        asyncio.create_task(view.serve(session, socket))
        self.chrome.open_page("POPUP", "about:blank", opener="MAIN")   # its opener writes a form into it
        words = "Now showing: a window opened by jobs.example.test"
        self.assertTrue(await until(lambda: words in [row["words"] for row in socket.messages("site")]))
        self.assertEqual(live_view.site_words("about:blank", "jobs.example.test"), "Now showing: a blank page")
        self.assertEqual(live_view.site_words("", "a.example", "chrome://settings"), "Now showing: a blank page")

    async def test_p6_a_setup_that_runs_this_operation_with_a_window_does_not_offer_the_step(self):
        with mock.patch.object(browser_runtime, "BACKGROUND_ALLOWED", True), \
                mock.patch.object(browser_runtime.sys, "platform", "darwin"), \
                mock.patch.object(browser_runtime, "headed_user_agent", return_value="Mozilla/5.0 test"):
            self.assertTrue(live_view.supported(self.config))
            windowed = {"browser_driver": dict(self.config["browser_driver"], headless_operations=["submit", "verify"])}
            self.assertFalse(live_view.supported(windowed))
            self.assertFalse(live_view.available("sign_in", windowed))
            view = live_view.LiveView(root=self.home, config=windowed, guard_command=fakes.guard_command(self.chrome))
            self.assertIsNone(view.offer("job-1", URL, "sign_in"))
            refused = await view.start("anything")
            self.assertEqual((refused.reason, refused.words), (live_view.UNAVAILABLE, live_view.NOT_AVAILABLE_WORDS))
            self.assertEqual(view.guard_command.starts, 0)
            with mock.patch.object(browser_runtime, "headed_user_agent", return_value=None):   # Chrome's version unreadable
                self.assertFalse(live_view.supported(self.config))

    async def test_p7_a_page_of_chromes_own_is_never_named_and_odd_addresses_show_their_true_host(self):
        view, session = await self.live()
        socket = FakeSocket()
        asyncio.create_task(view.serve(session, socket))
        self.assertTrue(await until(lambda: socket.messages("status")))
        self.chrome.change("MAIN", "chrome://settings/passwords")
        self.assertTrue(await until(lambda: session.chrome.active is None))
        await asyncio.sleep(0.3)
        for text in (item for item in socket.sent if isinstance(item, str)):
            self.assertNotIn("settings", text)
        self.assertEqual(socket.messages("site")[-1]["words"], "Now showing: a blank page")
        opened = "jobs.example.test"
        for url, host in (("https://evil.example\\@accounts.google.com/", "evil.example"),          # a backslash is a slash
                          ("https://accounts.google.com@evil.example/", "evil.example"),
                          ("https://\u0430pple.com/", "xn--pple-43d.com"),                              # a Cyrillic a
                          ("https://ev\til.example/", "evil.example"),
                          ("HTTPS://B\u00fccher.example/", "xn--bcher-kva.example")):
            self.assertEqual(live_view.site_words(url, opened), "Now showing: %s (opened from %s)" % (host, opened), url)
        long = live_view.site_words("https://accounts.google.com." + "a" * 260 + ".evil.example/", opened)
        self.assertIn(".evil.example (opened from", long)   # a long name keeps its end, which says whose it is
        self.assertNotIn("accounts.google.com", long)
        self.assertTrue(live_view.site_words("https://caf\u00e9.example/", opened).isascii())
        click = {"type": "mousePressed", "x": 1, "y": 1, "button": "left"}
        self.assertIsNone(live_view.mouse_params(dict(click, buttons=True)))
        self.assertIsNone(live_view.mouse_params(dict(click, clicks=True)))
        self.assertEqual(live_view.mouse_params(dict(click, buttons=1, clicks=1))["clickCount"], 1)

    async def test_g5_the_connection_to_chrome_uses_the_logger_that_cannot_write_and_it_matters(self):
        import websockets.asyncio.client as client
        real, seen = client.connect, []

        def spy(url, **options):
            seen.append(options)
            return real(url, **options)
        with mock.patch.object(client, "connect", spy):
            view, session = await self.live()
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["logger"], live_view.WIRE_LOG)
        await view.end(live_view.CANCEL)
        records, library = Records(), logging.getLogger("websockets.client")
        level = library.level
        library.addHandler(records)
        library.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (library.removeHandler(records), library.setLevel(level)))
        for logger, typed in ((None, "Zebra77"), (live_view.WIRE_LOG, "Quokka88")):
            options = {} if logger is None else {"logger": logger}
            chrome = live_view.Chrome(self.chrome.endpoint, connect=lambda url: real(url, proxy=None, **options))
            await chrome.start()
            chrome.input({"t": "key", "type": "down", "key": typed, "code": "KeyZ"})
            self.assertTrue(await until(lambda: any(params["key"] == typed for params in self.keys())))
            await chrome.close()
        self.assertTrue(any("Zebra77" in line for line in records.lines))    # the library's own logger shows the key
        self.assertFalse(any("Quokka88" in line for line in records.lines))  # the live view's never does

    async def test_g6_what_the_viewer_sends_is_checked_and_a_bad_message_never_ends_the_session(self):
        for text in ('{"t": "mouse", "x": NaN}', '{"t": "mouse", "x": Infinity}', '{"x": -Infinity}', "[1]", '"x"',
                     "x" * (live_view.MESSAGE_MAX_CHARACTERS + 1)):
            with self.assertRaises(ValueError):
                live_view.read_message(text)
        moved = {"t": "mouse", "type": "mouseMoved", "x": 99999, "y": -5}
        self.assertEqual([live_view.mouse_params(moved, (1280, 900))[name] for name in ("x", "y")], [1280.0, 0.0])
        self.assertIsNone(live_view.mouse_params(dict(moved, x=float("inf"))))   # what "1e999" parses to
        self.assertIsNone(live_view.mouse_params(dict(moved, y="12")))
        wheel = live_view.wheel_params({"x": 5, "y": 5, "dx": 1e9, "dy": -1e9})
        self.assertEqual((wheel["deltaX"], wheel["deltaY"]), (10000.0, -10000.0))
        self.assertIsNone(live_view.wheel_params({"x": 5, "y": 5, "dx": float("nan"), "dy": 0}))
        self.assertIsNone(live_view.paste_params({"text": "p" * (live_view.PASTE_MAX_CHARACTERS + 1)}))
        self.assertEqual(len(live_view.paste_params({"text": "p" * 10000})["text"]), 10000)
        self.assertIsNone(live_view.key_params({"type": "down", "key": "k" * 33, "code": "KeyK"}))
        self.assertIsNone(live_view.key_params({"type": "down", "key": "k", "code": "K" * 33}))
        view, session = await self.live()
        before = session.lease.ends
        for text in ('{"t": "mouse", "type": "mouseMoved", "x": NaN, "y": 1}', '{"t": "mouse", "type": "mouseMoved", "x": 1e999, "y": 1}',
                     '{"t": "wheel", "x": 1, "y": 1, "dx": Infinity, "dy": 0}', json.dumps({"t": "paste", "text": "p" * 10001}),
                     json.dumps({"t": "key", "type": "down", "key": "k" * 40, "code": "KeyK"}), "not json", "x" * 200000):
            view.handle(session, text)
        self.assertEqual(session.lease.ends, before)   # none of them counted as the person acting
        view.handle(session, json.dumps({"t": "mouse", "type": "mousePressed", "x": 5000, "y": 5000, "button": "left"}))
        self.assertTrue(await until(lambda: self.chrome.methods("Input.dispatchMouseEvent")))
        self.assertIs(view.session, session)           # still live
        sent = [row for row in self.chrome.methods() if row["method"].startswith("Input.")]
        self.assertEqual([(row["params"]["x"], row["params"]["y"]) for row in sent], [(1280.0, 900.0)])   # held inside the picture
        self.assertEqual(session.counts["unreadable"], 4)   # the two that are not JSON numbers, the text, the long one
        source = Path(live_view.__file__).with_name("dashboard.py").read_text()
        self.assertIn("ws_max_size=live_view.SOCKET_MAX_BYTES", source)   # the shipped server's frame limit
        self.assertLessEqual(live_view.SOCKET_MAX_BYTES, 16 * 1024 * 1024 // 32)

    async def test_g7_an_ending_scheduled_by_the_session_itself_survives_garbage_collection(self):
        ended = []
        view, session = await self.live(on_ended=ended.append)
        view._lost(session)
        self.assertEqual(len(view.endings), 1)   # held by the view, not only by the event loop's weak reference
        gc.collect()
        self.assertTrue(await until(lambda: ended))
        self.assertEqual(ended[0].reason, live_view.CLOSED)
        self.assertTrue(await until(lambda: not view.endings))
        self.assertTrue(self.lock_free())
        source = Path(live_view.__file__).read_text()
        self.assertEqual(source.count("asyncio.create_task(self.end("), 1)   # only inside _end_soon

    async def test_g8_cancel_while_the_page_is_still_opening_stops_the_start_and_frees_the_browser(self):
        base.database_with_schema(self.home).close()
        view = self.view(guard_command=fakes.guard_command(self.chrome, "--hang"))
        starting = asyncio.create_task(view.start(view.offer("job-1", URL, "sign_in")))
        await asyncio.sleep(0.5)
        self.assertFalse(starting.done())        # it would hang for minutes
        self.assertFalse(self.lock_free())
        result = await view.end(live_view.CANCEL)
        self.assertEqual((result.reason, result.words, result.job), (live_view.CANCEL, "Cancelled.", "job-1"))
        refused = await starting
        self.assertEqual((refused.reason, refused.words), (live_view.CANCEL, "Cancelled."))
        self.assertTrue(self.lock_free())
        self.assertEqual(self.audit()[-1]["decision"], "STOPPED")   # the guard it started was stopped
        self.assertEqual(view.guard_command.starts, 1)
        database = sqlite3.connect(paths.database(self.home))
        self.addCleanup(database.close)
        self.assertEqual(live_view.failure_counts(database)["by_reason"], {})   # the person's choice: not a failure
        self.assertIsNone(await view.end(live_view.DONE))                        # Done cannot cancel a start
        view.guard_command = fakes.guard_command(self.chrome)
        self.assertIsInstance(await view.start(view.offer("job-1", URL, "sign_in")), live_view.Session)   # and it opens after
        await view.end(live_view.CANCEL)
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        self.assertIn("cancelButton.hidden = false;   // a page that is slow to open can be cancelled", viewer)


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class CrashTest(Case):
    async def test_a_live_session_killed_midway_leaves_no_lock_and_no_chrome_after_the_next_start(self):
        """R14: Sidedoor is killed with a live session open (and its guard dies without closing Chrome). The
        lock goes with the process; the next start's check (browser_runtime.unavailable: close_orphan and the
        stale-profile recovery) closes the Chrome that was left, and the session starts."""
        marker = str(self.profile)

        def ps(argv, **options):   # the real `ps`, minus other programs' lines (a Sidedoor running on this Mac)
            done = subprocess.run(argv, **options)
            if "-axo" in argv:
                done.stdout = "\n".join(line for line in done.stdout.splitlines() if marker in line)
            return done
        child = subprocess.Popen([sys.executable, str(Path(__file__).parent / "live_view_crash_child.py"),
                                  str(self.home), str(self.profile)], stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
        try:
            self.assertEqual(await asyncio.to_thread(child.stdout.readline), "LIVE\n")
            left = browser_runtime.lock_holder(self.profile)
            self.addCleanup(lambda: browser_runtime.holds_profile(left, self.profile) and os.kill(left, signal.SIGKILL))
            self.assertTrue(browser_runtime.chrome_running(self.profile))
            self.assertFalse(self.lock_free())
        finally:
            child.kill()   # the exact process this test started
            child.wait()
            child.stdout.close()
        self.assertTrue(self.lock_free())                               # the lock went with the process
        self.assertTrue(browser_runtime.chrome_running(self.profile))   # its Chrome did not
        view, session = await self.live(self.view(ps=ps))
        self.assertFalse(browser_runtime.chrome_running(self.profile))
        self.assertFalse(browser_runtime.holds_profile(left, self.profile))
        self.assertFalse(os.path.lexists(self.profile / "SingletonLock"))
        await view.end(live_view.DONE)
        self.assertTrue(self.lock_free())
        self.assertFalse(browser_runtime.chrome_running(self.profile))


if DEPENDENCIES:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import dashboard

SECRET_TYPED = "hunter2.Zebra!42"
SECRET_PASTED = "paste-9f3c-correct-horse-battery"
KEY_CODES = {".": "Period", "!": "Digit1"}


class Records(logging.Handler):
    def __init__(self):
        super().__init__(level=0)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_text")))


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class RoutesTest(unittest.TestCase):
    """Through the dashboard as the app builds it: create_app with home=None and SIDEDOOR_HOME set."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home, self.profile = Path(self.temp.name) / "home", Path(self.temp.name) / "profile"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.chrome = fakes.FakeChrome(MAIN).start()
        self.addCleanup(self.chrome.stop)
        app = dashboard.create_app(base.SECRET, base.PORT)
        self.view = app.state.live_view
        self.assertEqual(self.view.root, self.home)
        self.view.config = {"browser_driver": {"profile": str(self.profile)}}
        self.view.guard_command = fakes.guard_command(self.chrome)
        self.view.is_supported = lambda config: True
        self.view.leave_grace_seconds = 0.5
        self.stack = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        self.client = self.stack.__enter__()   # one event loop for the whole test, as in the running app
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.client.get("/", params={"t": base.SECRET})
        self.target = self.view.offer("job-1", URL, "sign_in")

    def start(self):
        answer = self.client.post(f"/live-view/{self.target}/start", headers=ORIGIN)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.addCleanup(lambda: self.client.post("/live-view/end?reason=cancel", headers=ORIGIN))
        return answer.json()["session"]

    def picture(self, socket):
        for _ in range(50):
            message = socket.receive()
            if message.get("bytes"):
                return message["bytes"]
        self.fail("no picture")

    def refused(self, path, client=None, **options):
        try:
            with (client or self.client).websocket_connect(path, **options):
                return False
        except WebSocketDisconnect as closed:
            return closed.code

    def test_the_page_and_fragment_need_the_dashboard_sign_in_and_an_offered_target(self):
        stranger = TestClient(self.client.app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        for path in (f"/live-view/{self.target}", f"/live-view/{self.target}/fragment", "/static/live_view.js"):
            self.assertEqual(stranger.get(path).status_code, 403, path)
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(stranger.post(f"/live-view/{self.target}/start", headers=ORIGIN).status_code, 403)
        page = self.client.get(f"/live-view/{self.target}").text
        for piece in ("data-live-view", f'data-target="{self.target}"', "data-live-canvas", ">Done<", ">Cancel<", ">Sign in<",
                      "data-live-status", "/static/live_view.js", "jobs.example.test"):
            self.assertIn(piece, page)
        self.assertNotIn("<script>", page)   # the page's script policy allows no inline script
        self.assertIn("data-autostart", self.client.get(f"/live-view/{self.target}/fragment?start=1").text)
        self.assertEqual(self.client.get("/live-view/not-a-target").status_code, 404)
        self.assertEqual(self.client.post("/live-view/not-a-target/start", headers=ORIGIN).status_code, 404)

    def test_no_request_can_name_the_page_to_open_and_other_sites_cannot_start_or_end_one(self):
        for path in ("/live-view/start", "/live-view/open", "/live-view"):
            answer = self.client.post(path, headers=ORIGIN, params={"url": "https://evil.example/"}, json={"url": "https://evil.example/"})
            self.assertIn(answer.status_code, (404, 405), path)
        answer = self.client.post(f"/live-view/{self.target}/start", headers=ORIGIN, params={"url": "https://evil.example/"})
        self.assertEqual(answer.status_code, 200)
        self.addCleanup(lambda: self.client.post("/live-view/end?reason=cancel", headers=ORIGIN))
        self.assertEqual([row["arguments"] for row in self.audit() if row["decision"] == "FORWARDED"], [{"url": URL}])
        for headers in ({"origin": "https://evil.example"}, {}):
            self.assertEqual(self.client.post(f"/live-view/{self.target}/start", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/live-view/end?reason=done", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/live-view/end?reason=lease", headers=ORIGIN).status_code, 400)
        self.assertIsNotNone(self.view.session)
        self.assertEqual(self.client.post("/live-view/end?reason=done", headers=ORIGIN).json()["words"], live_view.ENDED_WORDS["done"])
        self.assertEqual(self.client.post("/live-view/end?reason=done", headers=ORIGIN).status_code, 409)

    def audit(self):
        path = paths.reports(self.home) / live_view.FOLDER / "session" / browser_runtime.GUARD_AUDIT
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_the_socket_needs_the_cookie_this_dashboards_origin_and_the_live_session(self):
        session = self.start()
        path = f"{WS}/live-view/ws?s={session}"
        stranger = TestClient(self.client.app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        self.assertEqual(self.refused(path, client=stranger, headers=ORIGIN), 4403)             # no cookie
        stranger.cookies.set(dashboard.COOKIE, "wrong")
        self.assertEqual(self.refused(path, client=stranger, headers=ORIGIN), 4403)             # a wrong cookie
        self.assertEqual(self.refused(path), 4403)                                              # no Origin
        self.assertEqual(self.refused(path, headers={"origin": "https://evil.example"}), 4403)  # another site's page
        self.assertEqual(self.refused(path, headers={"origin": "null"}), 4403)
        self.assertEqual(self.refused(path, headers={"origin": ORIGIN["origin"], "host": "evil.example"}), 4403)
        self.assertEqual(self.refused(f"/live-view/ws?s={session}", headers=ORIGIN), 4403)     # another Host name
        self.assertEqual(self.refused(f"{WS}/live-view/ws?s=wrong", headers=ORIGIN), 4404)
        self.assertEqual(self.refused(f"{WS}/live-view/ws", headers=ORIGIN), 4404)
        self.assertEqual(self.chrome.methods("Input.dispatchMouseEvent"), [])
        with self.client.websocket_connect(path, headers=ORIGIN) as socket:
            self.assertTrue(self.picture(socket).startswith(fakes.FRAME))   # one picture on joining
            socket.send_text(CLICK)
            self.assertTrue(self.chrome.wait_for(lambda: self.chrome.methods("Input.dispatchMouseEvent")))
        with self.client.websocket_connect(path, headers=ORIGIN) as socket:   # a dropped socket comes back
            self.assertTrue(self.picture(socket).startswith(fakes.FRAME))
            self.assertEqual(self.client.post("/live-view/end?reason=done", headers=ORIGIN).json()["state"], "ended")
            for _ in range(50):
                message = socket.receive()
                if message.get("text") and json.loads(message["text"])["t"] == "ended":
                    break
            else:
                self.fail("the viewer was not told it ended")
        self.assertEqual(self.refused(path, headers=ORIGIN), 4404)   # the session is over

    def test_g3_a_start_where_the_step_is_not_available_says_so_once(self):
        self.view.is_supported = lambda config: False
        answer = self.client.post(f"/live-view/{self.target}/start", headers=ORIGIN)
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json(), {"state": "failed", "reason": "unavailable", "again": False,
                                         "words": "This step can't be done inside Sidedoor on this computer yet. "
                                                  "Apply yourself with the link on the card."})
        self.assertEqual(self.view.guard_command.starts, 0)

    def test_p2_a_paste_too_long_is_said_in_words_by_the_page_and_dropped_quietly_by_the_server(self):
        page = self.client.get(f"/live-view/{self.target}/fragment").text
        self.assertIn('data-paste-long="That paste is too long for this view. Type it, or paste it in smaller parts."', page)
        self.assertIn('data-paste-max="10000"', page)
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        check, sending = viewer.index("text.length > Number(root.dataset.pasteMax)"), viewer.index('send({t: "paste"')
        self.assertLess(check, sending)   # checked, said and kept from being sent
        self.assertIn("notice(root.dataset.pasteLong); return;", viewer)
        session = self.start()
        with self.client.websocket_connect(f"{WS}/live-view/ws?s={session}", headers=ORIGIN) as socket:
            socket.send_text(json.dumps({"t": "paste", "text": "p" * 10001}))   # a client that sends it anyway
            socket.send_text(json.dumps({"t": "paste", "text": "short"}))
            self.assertTrue(self.chrome.wait_for(lambda: self.chrome.methods("Input.insertText")))
            self.picture(socket)   # the socket is still open
        self.assertEqual([row["params"] for row in self.chrome.methods("Input.insertText")], [{"text": "short"}])

    def test_p5_a_browser_that_only_opens_with_a_window_does_not_invite_another_try(self):
        self.view.guard_command = fakes.guard_command(self.chrome, "--windowed")
        answer = self.client.post(f"/live-view/{self.target}/start", headers=ORIGIN)
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json(), {"state": "failed", "reason": "not_shown", "again": False,
                                         "words": "Sidedoor couldn't open its browser in the background on this computer. "
                                                  "Apply yourself with the link on the card."})
        self.view.guard_command = fakes.guard_command(self.chrome, "--no-endpoint")
        self.view.endpoint_wait_seconds = 0.2
        self.assertTrue(self.client.post(f"/live-view/{self.target}/start", headers=ORIGIN).json()["again"])

    def test_the_person_leaving_ends_the_session(self):
        session = self.start()
        with self.client.websocket_connect(f"{WS}/live-view/ws?s={session}", headers=ORIGIN) as socket:
            self.picture(socket)
        self.assertTrue(self.chrome.wait_for(lambda: self.view.session is None, 5.0))
        self.assertEqual(self.view.last.reason, live_view.LEFT)

    def test_what_is_typed_reaches_chrome_and_nothing_else(self):
        """Item 6: a secret typed as keys and another pasted go through the authenticated socket into Chrome.
        Afterwards neither is in any log record, any file of the Sidedoor home (the database, the guard's
        records, everything), or anything the viewer was sent; no picture is on disk either."""
        base.database_with_schema(self.home).close()
        records = Records()
        watched = [logging.getLogger(), logging.getLogger("sidedoor")]
        levels = [logger.level for logger in watched]
        for logger in watched:
            logger.addHandler(records)
            logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: [(logger.removeHandler(records), logger.setLevel(level)) for logger, level in zip(watched, levels)])
        session = self.start()
        sent_to_viewer = []
        with self.client.websocket_connect(f"{WS}/live-view/ws?s={session}", headers=ORIGIN) as socket:
            sent_to_viewer.append(self.picture(socket))
            for character in SECRET_TYPED:
                code = KEY_CODES.get(character) or ("Digit" + character if character.isdigit() else "Key" + character.upper())
                for kind in ("down", "up"):
                    socket.send_text(key(character, code, kind, live_view.SHIFT if character.isupper() or character == "!" else 0))
            socket.send_text(json.dumps({"t": "paste", "text": SECRET_PASTED}))
            socket.send_text("not json " + SECRET_PASTED)                                    # an unreadable message
            socket.send_text(json.dumps({"t": "mouse", "type": SECRET_PASTED, "x": SECRET_PASTED}))   # a malformed one
            self.assertTrue(self.chrome.wait_for(lambda: self.chrome.methods("Input.insertText")))
            self.assertEqual(self.client.post("/live-view/end?reason=done", headers=ORIGIN).status_code, 200)
            for _ in range(50):
                message = socket.receive()
                sent_to_viewer.append(message.get("bytes") or (message.get("text") or "").encode())
                if message["type"] == "websocket.close" or b'"ended"' in sent_to_viewer[-1]:
                    break
        typed = "".join(row["params"].get("text", "") for row in self.chrome.methods("Input.dispatchKeyEvent"))
        self.assertEqual(typed, SECRET_TYPED)                                                 # Chrome got the keys
        self.assertEqual([row["params"] for row in self.chrome.methods("Input.insertText")], [{"text": SECRET_PASTED}])
        self.assertEqual(self.view.last.counts["keys"], 2 * len(SECRET_TYPED))                 # counts only
        self.assertTrue(any("LIVE_VIEW_ENDED" in line for line in records.lines))             # the scan sees real records
        # The websocket library's own frame log (DEBUG) can never write for the connection to Chrome, whatever
        # is set on it, and no logging configuration can reach it (it is not a registered logger).
        live_view.WIRE_LOG.setLevel(logging.DEBUG)
        live_view.WIRE_LOG.disabled = False
        live_view.WIRE_LOG.addHandler(records)
        live_view.WIRE_LOG.debug("> TEXT %s", SECRET_PASTED)
        live_view.WIRE_LOG.error("%s", SECRET_PASTED)
        self.assertFalse(live_view.WIRE_LOG.isEnabledFor(logging.DEBUG))
        self.assertNotIn("live-view-wire", logging.Logger.manager.loggerDict)
        files = [path for path in Path(self.temp.name).rglob("*") if path.is_file()]
        names = {path.name for path in files}
        self.assertLessEqual({"orchestrator.sqlite3", browser_runtime.GUARD_AUDIT, "guard.stderr"}, names)
        for secret in (SECRET_TYPED, SECRET_PASTED, SECRET_TYPED[:7], fakes.FRAME.decode()):
            for line in records.lines:
                self.assertNotIn(secret, line)
            for path in files:
                self.assertNotIn(secret.encode(), path.read_bytes(), path)
        for secret in (SECRET_TYPED, SECRET_PASTED):
            for item in sent_to_viewer:
                self.assertNotIn(secret.encode(), item)
        self.assertFalse((paths.reports(self.home) / live_view.FOLDER / "session" / browser_runtime.OUTPUT_FOLDER).exists())

    def test_no_model_runs_and_no_model_gets_a_tool_while_the_live_view_holds_the_browser(self):
        """A live session starts the guard directly: no engine, no model call, and every model session that
        needs the browser waits behind state/browser.lock (run_locks.browser_session) as it does today."""
        import model_calls
        import text_model
        import engines
        with mock.patch.object(text_model, "complete", side_effect=AssertionError("a model was called")), \
                mock.patch.object(engines, "engine", side_effect=AssertionError("an engine was asked for")), \
                mock.patch.object(model_calls, "record", side_effect=AssertionError("a model call was recorded")):
            session = self.start()   # the guard is the one process it starts
            with self.client.websocket_connect(f"{WS}/live-view/ws?s={session}", headers=ORIGIN) as socket:
                socket.send_text(CLICK)
                self.picture(socket)
        self.assertEqual(self.view.guard_command.starts, 1)
        held = []
        thread = threading.Thread(target=lambda: held.append(run_locks.browser_session(self.home).__enter__()))
        thread.start()
        thread.join()
        self.assertEqual(held, [False])
        source = Path(live_view.__file__).read_text()
        for name in ("text_model", "engines", "codex_options", "model_calls", "agent_worker"):
            self.assertNotIn("import " + name, source)


@unittest.skipUnless(DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class ServedTest(unittest.TestCase):
    """The same routes behind the server the app ships (uvicorn with the websockets package), on a free
    loopback port and a temporary home: the handshake, the refusals and the pictures as a browser gets them."""

    def test_the_shipped_server_serves_the_socket_and_refuses_strangers(self):
        import socket as sockets
        import urllib.error
        import urllib.request

        import uvicorn
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "home"
        home.mkdir()
        chrome = fakes.FakeChrome(MAIN).start()
        self.addCleanup(chrome.stop)
        listener = sockets.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self.assertNotEqual(port, paths.DASHBOARD_PORT)
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(home)}):
            app = dashboard.create_app(base.SECRET, port)
        view = app.state.live_view
        view.config = {"browser_driver": {"profile": str(Path(temp.name) / "profile")}}
        view.guard_command, view.is_supported = fakes.guard_command(chrome), lambda config: True
        # As dashboard._serve configures it. The warning level matters: at DEBUG the websocket library would
        # write every frame the viewer sends (what is typed) to uvicorn's log.
        self.assertIn('log_level="warning", access_log=False', Path(dashboard.__file__).read_text())
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, server_header=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(10)
            listener.close()
        self.addCleanup(stop)
        self.assertTrue(chrome.wait_for(lambda: server.started, 10))
        records = Records()
        logging.getLogger().addHandler(records)
        self.addCleanup(logging.getLogger().removeHandler, records)
        origin, cookie = f"http://127.0.0.1:{port}", f"{dashboard.COOKIE}={base.SECRET}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, headers):
            try:
                with opener.open(urllib.request.Request(origin + path, data=b"", headers=headers, method="POST"), timeout=20) as answer:
                    return answer.status, json.load(answer)
            except urllib.error.HTTPError as error:
                return error.code, None
        target = view.offer("job-1", URL, "human_check")
        self.assertEqual(post(f"/live-view/{target}/start", {"Origin": origin})[0], 403)                     # no cookie
        self.assertEqual(post(f"/live-view/{target}/start", {"Origin": "https://evil.example", "Cookie": cookie})[0], 403)
        status, body = post(f"/live-view/{target}/start", {"Origin": origin, "Cookie": cookie})
        self.assertEqual((status, body["state"]), (200, "live"))
        url = f"ws://127.0.0.1:{port}/live-view/ws?s={body['session']}"
        for headers, given in (({"Cookie": cookie}, "https://evil.example"), ({}, origin), ({"Cookie": cookie}, None),
                               ({"Cookie": dashboard.COOKIE + "=wrong"}, origin)):
            with self.assertRaises(InvalidStatus) as refused:
                connect(url, additional_headers=headers, origin=given, proxy=None, open_timeout=10)
            self.assertEqual(refused.exception.response.status_code, 403)
        with connect(url, additional_headers={"Cookie": cookie}, origin=origin, proxy=None, open_timeout=10) as viewer:
            got = [viewer.recv(timeout=10) for _ in range(3)]
            while not any(isinstance(item, bytes) for item in got):
                got.append(viewer.recv(timeout=10))
            self.assertTrue(next(item for item in got if isinstance(item, bytes)).startswith(fakes.FRAME))   # binary pictures
            viewer.send(json.dumps({"t": "paste", "text": SECRET_PASTED}))
            self.assertTrue(chrome.wait_for(lambda: chrome.methods("Input.insertText")))
            self.assertEqual(post("/live-view/end?reason=done", {"Origin": origin, "Cookie": cookie})[1]["state"], "ended")
        self.assertEqual([row["params"] for row in chrome.methods("Input.insertText")], [{"text": SECRET_PASTED}])
        for line in records.lines:
            self.assertNotIn(SECRET_PASTED, line)
        for path in Path(temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET_PASTED.encode(), path.read_bytes(), path)

    def test_p3_quit_ends_the_live_session_before_the_server_has_stopped(self):
        import socket as sockets
        import urllib.request

        import uvicorn
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "home"
        home.mkdir()
        chrome = fakes.FakeChrome(MAIN).start()
        self.addCleanup(chrome.stop)
        listener = sockets.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(home)}):
            app = dashboard.create_app(base.SECRET, port)
        view = app.state.live_view
        view.config = {"browser_driver": {"profile": str(Path(temp.name) / "profile")}}
        view.guard_command, view.is_supported = fakes.guard_command(chrome), lambda config: True
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        self.assertTrue(chrome.wait_for(lambda: server.started, 10))
        origin = f"http://127.0.0.1:{port}"
        request = urllib.request.Request(f"{origin}/live-view/{view.offer('job-1', URL, 'sign_in')}/start", data=b"", method="POST",
                                         headers={"Origin": origin, "Cookie": f"{dashboard.COOKIE}={base.SECRET}"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20).close()
        session = view.session
        self.assertIsNotNone(session)
        server.should_exit = True   # Quit
        thread.join(live_view.QUIT_SECONDS + 10)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(view.session)
        self.assertEqual(view.last.reason, live_view.CLOSED)
        self.assertEqual(session.guard.process.returncode, 0)   # its guard was stopped the ordinary way
        audit = paths.reports(home) / live_view.FOLDER / "session" / browser_runtime.GUARD_AUDIT
        self.assertEqual(json.loads(audit.read_text().splitlines()[-1])["decision"], "STOPPED")
        with run_locks.browser_session(home) as held:
            self.assertTrue(held)
        self.assertLessEqual(live_view.QUIT_SECONDS, 10)

    def test_g4_the_servers_own_frame_log_never_holds_what_is_typed_whatever_its_level(self):
        """uvicorn's websocket server writes each frame at DEBUG on `uvicorn.error`. With that logger at DEBUG
        and a handler on it, a secret sent through the served socket is absent; with the live view's filter
        taken off it is there (so the filter is what keeps it out). An oversized frame closes that socket only."""
        import socket as sockets
        import urllib.request

        import uvicorn
        from websockets.sync.client import connect
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "home"
        home.mkdir()
        chrome = fakes.FakeChrome(MAIN).start()
        self.addCleanup(chrome.stop)
        listener = sockets.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server_log = logging.getLogger(live_view.SERVER_LOGGER)
        for known in [known for known in server_log.filters if isinstance(known, live_view.NoFrameLog)]:
            server_log.removeFilter(known)
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(home)}):
            app = dashboard.create_app(base.SECRET, port)
            dashboard.create_app(base.SECRET, port)   # registering twice adds the filter once
        filters = [known for known in server_log.filters if isinstance(known, live_view.NoFrameLog)]
        self.assertEqual(len(filters), 1)
        view = app.state.live_view
        view.config = {"browser_driver": {"profile": str(Path(temp.name) / "profile")}}
        view.guard_command, view.is_supported = fakes.guard_command(chrome), lambda config: True
        records, level = Records(), server_log.level
        server_log.addHandler(records)   # on the logger itself: it does not pass records up
        server_log.setLevel(logging.DEBUG)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, ws_max_size=live_view.SOCKET_MAX_BYTES))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(10)
            listener.close()
            server_log.removeHandler(records)
            server_log.setLevel(level)
            if not any(isinstance(known, live_view.NoFrameLog) for known in server_log.filters):
                server_log.addFilter(filters[0])
        self.addCleanup(stop)
        self.assertTrue(chrome.wait_for(lambda: server.started, 10))
        origin, cookie = f"http://127.0.0.1:{port}", f"{dashboard.COOKIE}={base.SECRET}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        target = view.offer("job-1", URL, "sign_in")
        request = urllib.request.Request(f"{origin}/live-view/{target}/start", data=b"", method="POST",
                                         headers={"Origin": origin, "Cookie": cookie})
        with opener.open(request, timeout=20) as answer:
            url = f"ws://127.0.0.1:{port}/live-view/ws?s={json.load(answer)['session']}"

        def send(*messages):
            with connect(url, additional_headers={"Cookie": cookie}, origin=origin, proxy=None, open_timeout=10) as viewer:
                for message in messages:
                    viewer.send(message)
                viewer.recv(timeout=10)
                time.sleep(0.2)
        send(key("Kiwi4411", "KeyK"), json.dumps({"t": "paste", "text": SECRET_PASTED}))
        self.assertTrue(chrome.wait_for(lambda: chrome.methods("Input.insertText")))
        for line in records.lines:
            self.assertNotIn(SECRET_PASTED, line)
            self.assertNotIn("Kiwi4411", line)
        from websockets.exceptions import ConnectionClosed
        with connect(url, additional_headers={"Cookie": cookie}, origin=origin, proxy=None, open_timeout=10) as viewer:
            viewer.send("z" * (live_view.SOCKET_MAX_BYTES + 10))   # larger than the server's frame limit
            with self.assertRaises(ConnectionClosed) as closed:
                for _ in range(200):
                    viewer.recv(timeout=5)
            self.assertEqual(closed.exception.rcvd.code, 1009)     # that socket closes; the session goes on
        self.assertIsNotNone(view.session)
        self.assertEqual(view.session.counts.get("unreadable", 0), 0)   # it never reached the handler
        self.assertEqual(len(chrome.methods("Input.insertText")), 1)
        server_log.removeFilter(filters[0])   # the control: without the filter the same logger shows the paste
        send(json.dumps({"t": "paste", "text": "control-" + SECRET_PASTED}))
        self.assertTrue(chrome.wait_for(lambda: len(chrome.methods("Input.insertText")) == 2))
        self.assertTrue(any("control-" + SECRET_PASTED in line for line in records.lines))
        request = urllib.request.Request(f"{origin}/live-view/end?reason=cancel", data=b"", method="POST",
                                         headers={"Origin": origin, "Cookie": cookie})
        opener.open(request, timeout=20).close()


if __name__ == "__main__":
    unittest.main()
