"""E16.T2 phase 1, piece 3 (owner, 2026-11-12): the live view attaches to a RUNNING session's Chrome.
A second DevTools client on that session's endpoint: no browser lock, no guard, no Chrome of its own; every
ending only disconnects this client. Done tells the session's hold the user is done. The person's input is
passed on as in every other live view (owner, 2026-11-13).
Fakes only (tests/live_view_fakes.py): no browser, no Node, nothing off 127.0.0.1."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import json
import unittest
import unittest.mock
from pathlib import Path

import browser_runtime
import form_check_hold
import live_view
import run_locks
import test_live_view as lv


@unittest.skipUnless(lv.DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class AttachTest(lv.Case):
    def session_folder(self, headless=True, endpoint=None):
        """The folder of a running attended submit: its guard's audit names the Chrome it started."""
        folder = Path(self.temp.name) / "submit-session"
        folder.mkdir(exist_ok=True)
        rows = [{"decision": "FORWARDED", "tool": "browser_navigate"}]
        if endpoint is not False:
            rows.insert(0, {"decision": "BACKGROUND_LAUNCHED", "endpoint": endpoint or self.chrome.endpoint,
                            "headless": headless})
        (folder / browser_runtime.GUARD_AUDIT).write_text("".join(json.dumps(row) + "\n" for row in rows))
        return folder

    async def running_session(self):
        """The running session's own client on its Chrome (as @playwright/mcp is), and the browser lock it holds."""
        from websockets.asyncio.client import connect
        url = await asyncio.to_thread(live_view.debugger_url, self.chrome.endpoint)
        own = await connect(url, proxy=None, logger=live_view.WIRE_LOG)
        self.addAsyncCleanup(own.close)
        lock = run_locks.browser_session(self.home)
        self.assertTrue(lock.__enter__())
        self.addCleanup(lock.__exit__, None, None, None)
        self.assertTrue(await lv.until(lambda: len(self.chrome.connections) == 1))
        return own

    async def own_client_alive(self, own):
        await own.send(json.dumps({"id": 9001, "method": "Target.getTargets", "params": {}}))
        while True:
            answer = json.loads(await asyncio.wait_for(own.recv(), 5))
            if answer.get("id") == 9001:
                return bool(answer["result"]["targetInfos"])

    async def attached(self, folder=None, **offer):
        view = self.view()
        target = view.offer_attached("job-1", folder or self.session_folder(), "jobs.example.test", **offer)
        outcome = await view.start(target)
        if isinstance(outcome, live_view.Session):
            self.addAsyncCleanup(view.end, live_view.CANCEL)
        return view, outcome

    async def test_it_shows_the_running_sessions_page_with_no_lock_no_guard_and_no_chrome_of_its_own(self):
        own = await self.running_session()
        folder = self.session_folder()
        view, session = await self.attached(folder)
        self.assertIsInstance(session, live_view.Session)                  # an ordinary start would say "busy"
        self.assertEqual(session.chrome.active, "MAIN")
        self.assertEqual(session.chrome.url(), lv.URL)
        self.assertEqual(view.guard_command.starts, 0)
        self.assertIsNone(session.guard)
        self.assertIsNone(session.hold)
        self.assertEqual(len(self.chrome.connections), 2)                  # two clients on one endpoint
        self.assertFalse(self.lock_free())                                 # the session's lock, untouched
        socket = lv.FakeSocket()
        serving = asyncio.create_task(view.serve(session, socket))
        # The person's clicks and keys are passed on as in every other live view.
        view.handle(session, lv.CLICK)
        view.handle(session, lv.key("a", "KeyA"))
        self.assertTrue(await lv.until(lambda: self.chrome.methods("Input.dispatchKeyEvent")))
        self.assertTrue(self.chrome.methods("Input.dispatchMouseEvent"))
        with unittest.mock.patch.object(view, "_stop_chrome") as stop_chrome, \
                unittest.mock.patch.object(browser_runtime, "end_session") as end_session:
            ended = await view.end(live_view.DONE)
            await serving
        self.assertEqual(ended.reason, live_view.DONE)
        self.assertFalse(ended.site_session_saved)
        stop_chrome.assert_not_called()
        end_session.assert_not_called()
        # Done: the session's hold is told, and only this client is gone. The session's Chrome runs on.
        self.assertTrue((folder / form_check_hold.DONE_FILE).exists())
        self.assertTrue(await lv.until(lambda: len(self.chrome.connections) == 1))
        self.assertTrue(await self.own_client_alive(own))
        self.assertEqual(set(self.chrome.pages), {"MAIN"})
        self.assertLessEqual({row["method"] for row in self.chrome.methods()}, lv.ALLOWED_METHODS)
        self.assertFalse(self.lock_free())
        self.assertIsNone(view.session)
        self.assertFalse((self.home / "reports" / live_view.FOLDER / "session").exists())   # no session folder of its own

    async def test_a_press_or_a_key_that_would_submit_the_form_is_not_passed_on_and_everything_else_is(self):
        """Owner, 2026-11-13 ("a+b"). The page itself answers, by structure, whether what is under the point (or
        focused) is the form's submit control; here the fake Chrome gives that answer."""
        await self.running_session()
        view, session = await self.attached()
        socket = lv.FakeSocket()
        serving = asyncio.create_task(view.serve(session, socket))
        answers = {"point": True, "key": True}
        self.chrome.submits = lambda expression: answers["point" if "elementFromPoint(x,y)" in expression else "key"]
        release = json.dumps({"t": "mouse", "type": "mouseReleased", "x": 5, "y": 5, "button": "left", "buttons": 0, "clicks": 1})
        view.handle(session, lv.CLICK)                                         # on the site's Submit
        view.handle(session, release)
        view.handle(session, lv.key("Enter", "Enter"))                         # Enter in a field of the form
        view.handle(session, lv.key("Enter", "Enter", "up"))
        view.handle(session, lv.key(" ", "Space"))                             # Space on the focused Submit
        view.handle(session, lv.key(" ", "Space", "up"))
        view.handle(session, lv.key("a", "KeyA"))                              # an ordinary key: never asked about
        self.assertTrue(await lv.until(lambda: self.chrome.methods("Input.dispatchKeyEvent")))
        self.assertEqual([row["params"]["key"] for row in self.chrome.methods("Input.dispatchKeyEvent")], ["a"])
        self.assertEqual(self.chrome.methods("Input.dispatchMouseEvent"), [])
        self.assertEqual(session.chrome.counts["submit_refused"], 3)
        self.assertTrue(await lv.until(lambda: len(socket.messages("refused")) == 3))
        # Anything else (the check's own frame, a field, a link): passed on, press and release, in order.
        answers.update(point=False, key=False)
        view.handle(session, lv.CLICK)
        view.handle(session, release)
        view.handle(session, lv.key("Enter", "Enter"))
        self.assertTrue(await lv.until(lambda: len(self.chrome.methods("Input.dispatchMouseEvent")) == 2))
        self.assertEqual([row["params"]["type"] for row in self.chrome.methods("Input.dispatchMouseEvent")],
                         ["mousePressed", "mouseReleased"])
        self.assertTrue(await lv.until(lambda: len(self.chrome.methods("Input.dispatchKeyEvent")) == 2))
        # A page that does not answer: the press is not passed on (the safe side).
        answers.update(point=None)
        with unittest.mock.patch.object(live_view, "SUBMIT_CHECK_SECONDS", 0.2):
            view.handle(session, lv.CLICK)
            view.handle(session, release)
            self.assertTrue(await lv.until(lambda: session.chrome.counts["submit_refused"] == 4))
        self.assertEqual(len(self.chrome.methods("Input.dispatchMouseEvent")), 2)
        await view.end(live_view.CANCEL)
        await serving

    async def test_done_closes_the_view_at_once_whatever_the_busy_chrome_is_doing(self):
        """Owner, 2026-11-13 ("G1: a"): he saw about 5 seconds. The viewers are told before anything waits on Chrome."""
        import time
        await self.running_session()
        folder = self.session_folder()
        view, session = await self.attached(folder)
        socket = lv.FakeSocket()
        serving = asyncio.create_task(view.serve(session, socket))
        self.chrome.delays["Page.stopScreencast"] = 3                          # the AI is busy in that Chrome
        started = time.monotonic()
        ending = asyncio.create_task(view.end(live_view.DONE))
        self.assertTrue(await lv.until(lambda: socket.messages("ended"), 1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(socket.messages("ended")[0]["words"], "Done. Sidedoor carries on from here.")
        self.assertTrue((folder / form_check_hold.DONE_FILE).exists())         # the hold was told first
        await ending
        self.assertLess(time.monotonic() - started, 2.0)                       # and the press's own answer does not wait
        await serving

    async def test_the_view_tells_the_hold_of_the_users_presses_and_closes_by_itself_when_the_ai_resumes(self):
        """Owner, 2026-11-13: "If the user forgets to click Done, the AI carries on with its job and auto closes
        the Live view when done"."""
        await self.running_session()
        folder = self.session_folder()
        with unittest.mock.patch.object(live_view, "RESUME_WATCH_SECONDS", 0.05):
            view, session = await self.attached(folder)
            socket = lv.FakeSocket()
            serving = asyncio.create_task(view.serve(session, socket))
            view.handle(session, lv.CLICK)                                     # the tick
            self.assertTrue(await lv.until(lambda: (folder / form_check_hold.ACT_FILE).exists()))
            # The session's hold returned and the AI carried on (its guard recorded it): no more input, then closed.
            form_check_hold.hold_file(folder).write_text(json.dumps({"held": 12.0, "began": None, "resumed": True}))
            sent = len(self.chrome.methods("Input.dispatchMouseEvent"))
            view.handle(session, lv.CLICK)
            view.handle(session, lv.key("a", "KeyA"))
            self.assertTrue(await lv.until(lambda: view.session is None))
            await serving
        self.assertEqual(len(self.chrome.methods("Input.dispatchMouseEvent")), sent)
        self.assertEqual(self.chrome.methods("Input.dispatchKeyEvent"), [])
        self.assertEqual(view.last.reason, live_view.FINISHED)
        self.assertEqual(socket.messages("ended")[0]["words"], "Done. Sidedoor carries on from here.")
        self.assertTrue(form_check_hold.user_done(folder))                     # the card says it is done
        self.assertFalse((folder / form_check_hold.DONE_FILE).exists())        # no hold is ended by it
        self.assertEqual(len(self.chrome.connections), 1)                      # the session's Chrome runs on

    async def test_an_ordinary_live_view_asks_nothing_and_passes_everything_as_before(self):
        view = self.view()
        session = await view.start(view.offer("job-1", lv.URL, "sign_in"))
        self.addAsyncCleanup(view.end, live_view.CANCEL)
        self.chrome.submits = lambda expression: True
        view.handle(session, lv.CLICK)
        view.handle(session, lv.key("Enter", "Enter"))
        self.assertTrue(await lv.until(lambda: self.chrome.methods("Input.dispatchKeyEvent")))
        self.assertTrue(self.chrome.methods("Input.dispatchMouseEvent"))
        self.assertFalse([row for row in self.chrome.methods("Runtime.evaluate")
                          if live_view.SUBMIT_MARK in row["params"]["expression"]])

    async def test_cancel_and_the_lease_detach_without_telling_the_hold_and_leave_chrome_running(self):
        own = await self.running_session()
        for reason in (live_view.CANCEL, live_view.LEASE, live_view.LEFT):
            folder = self.session_folder()
            view, session = await self.attached(folder)
            self.assertIsInstance(session, live_view.Session)
            ended = await view.end(reason)
            self.assertEqual(ended.reason, reason)
            self.assertFalse((folder / form_check_hold.DONE_FILE).exists(), reason)
            self.assertTrue(await lv.until(lambda: len(self.chrome.connections) == 1))
            self.assertTrue(await self.own_client_alive(own))
            self.assertEqual(view.guard_command.starts, 0)

    async def test_the_sessions_chrome_going_away_ends_the_view_and_nothing_is_stopped_by_it(self):
        await self.running_session()
        folder = self.session_folder()
        view, session = await self.attached(folder)
        await asyncio.to_thread(self.chrome.drop_clients)                  # the submit session ended
        self.assertTrue(await lv.until(lambda: view.session is None))
        self.assertEqual(view.last.reason, live_view.CLOSED)
        self.assertEqual(view.last.words, "Sidedoor's browser closed.")      # the session is over: no "press again"
        self.assertIn("Press", live_view.ended_words(live_view.CLOSED, "human_check"))   # an ordinary view: as before
        self.assertFalse((folder / form_check_hold.DONE_FILE).exists())
        self.assertEqual(view.guard_command.starts, 0)

    async def test_a_session_with_no_hidden_chrome_to_show_is_not_shown_and_nothing_is_started(self):
        for folder in (self.session_folder(endpoint=False), self.session_folder(headless=False),
                       Path(self.temp.name) / "no-such-session"):
            view, outcome = await self.attached(folder)
            self.assertIsInstance(outcome, live_view.Failed)
            self.assertEqual(outcome.reason, live_view.NOT_SHOWN)
            self.assertEqual(view.guard_command.starts, 0)
            self.assertIsNone(view.session)
            self.assertEqual(len(self.chrome.connections), 0)
            self.assertTrue(self.lock_free())

    async def test_one_view_at_a_time_and_the_same_running_session_is_one_target(self):
        await self.running_session()
        folder = self.session_folder()
        view, session = await self.attached(folder)
        self.assertEqual(view.offer_attached("job-1", folder, "jobs.example.test"), session.target.id)
        other = view.offer("job-2", "https://other.example.test/", "sign_in")
        self.assertEqual((await view.start(other)).reason, live_view.OPEN)

    def test_a_real_page_is_what_makes_the_view_showable_never_a_blank_one(self):
        import http.server
        import threading
        pages = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(pages).encode()
                self.send_response(200 if self.path == "/json/list" else 404)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = "http://127.0.0.1:%d" % server.server_address[1]
        self.assertIsNone(live_view.session_page(endpoint))
        pages[:] = [{"type": "page", "url": "about:blank"}, {"type": "page", "url": "chrome://newtab/"},
                    {"type": "service_worker", "url": "https://x.example/sw.js"}]
        self.assertIsNone(live_view.session_page(endpoint))
        pages.append({"type": "page", "url": "https://jobs.example.test/apply"})
        self.assertEqual(live_view.session_page(endpoint), "https://jobs.example.test/apply")
        self.assertIsNone(live_view.session_page("http://127.0.0.1:9", timeout=0.5))   # no Chrome there

    def test_the_instruction_shown_with_the_view_is_one_plain_sentence_pair(self):
        self.assertEqual(live_view.ATTACHED_WORDS, "Do the human check. Do not press the site's Submit button.")
        self.assertNotIn("—", live_view.ATTACHED_WORDS)


if __name__ == "__main__":
    unittest.main()
