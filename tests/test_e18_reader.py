"""E18: the real reader of Sidedoor's browser (linkedin_reader) against a fake DevTools endpoint and a fake
browser guard (tests/live_view_fakes.py). No browser, nothing outside 127.0.0.1.

The marker test (owner A1): the fake Chrome holds a marker as LinkedIn's session cookie. Every way the read
can end is forced, and the marker is then looked for in every log record, every file under the home and the
data folder (the guard's audit and the saved site sessions among them) and every exception. It first proves
it can find a marker that is deliberately written (the must-hit control)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import contextlib
import io
import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import browser_runtime
import linkedin_reader
import linkedin_session
import live_view
import run_locks
try:
    import live_view_fakes
except ImportError:
    from tests import live_view_fakes

MARKER = "AQEDAT-READER-MARKER-5c1d9e77"
AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/142.0.0.0 Safari/537.36"


class Chrome(live_view_fakes.FakeChrome):
    """The fake DevTools endpoint, with a cookie jar and a user agent."""

    def __init__(self):
        super().__init__()
        self.cookies = [{"name": "li_at", "value": MARKER, "domain": ".www.linkedin.com", "path": "/"},
                        {"name": "JSESSIONID", "value": "ajax:1", "domain": ".linkedin.com", "path": "/"}]
        self.agent, self.raw = AGENT, None

    async def _answer(self, connection, message):
        method = message.get("method")
        if self.raw is not None and method == "Storage.getCookies":
            await connection.send(self.raw)   # a malformed answer, as sent
            return
        if method in ("Storage.getCookies", "Browser.getVersion") or (
                method == "Runtime.evaluate" and "userAgent" in str((message.get("params") or {}).get("expression"))):
            delay = self.delays.get(method, 0)
            if delay:
                await asyncio.sleep(delay)
            result = ({"cookies": self.cookies} if method == "Storage.getCookies" else
                      {"userAgent": self.agent} if method == "Browser.getVersion" else
                      {"result": {"type": "string", "value": self.agent}})
            with contextlib.suppress(Exception):
                await connection.send(json.dumps({"id": message["id"], "result": result}))
            return
        await super()._answer(connection, message)


class Records(logging.Handler):
    def __init__(self):
        super().__init__(level=0)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class TheRealReader(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home, self.root = Path(self.directory.name) / "home", Path(self.directory.name) / "data"
        self.home.mkdir()
        (self.root / "state").mkdir(parents=True)
        self.chrome = Chrome().start()
        self.addCleanup(self.chrome.stop)
        self.records, self.err = Records(), io.StringIO()
        for logger in (logging.getLogger(), linkedin_reader.LOG, live_view.LOG):   # the app's loggers do not all reach the root
            logger.addHandler(self.records)
            self.addCleanup(logger.removeHandler, self.records)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stderr(self.err))
        stack.enter_context(mock.patch.object(browser_runtime, "unavailable", return_value=None))
        stack.enter_context(mock.patch.object(browser_runtime, "chrome_running", return_value=False))
        self.kept = []   # every exception a read raised, for the scan

    def read(self, *extra, view=None, wait=5, supported=True):
        self.command = live_view_fakes.guard_command(self.chrome, *extra)
        try:
            return linkedin_reader.read(self.home, self.root, {}, view, guard_command=self.command,
                                        endpoint_wait=wait, is_supported=lambda config: supported)
        except Exception as error:
            self.kept.append(error)
            raise

    def leaks(self):
        needle = MARKER.encode()
        found = sorted(str(path) for path in Path(self.directory.name).rglob("*")
                       if path.is_file() and needle in path.read_bytes())
        if MARKER in "\n".join(self.records.lines) + self.err.getvalue():
            found.append("log")
        for error in self.kept:
            text = "%r %s %r %r %r" % (error, error, error.args, error.__cause__, error.__context__)
            if MARKER in text:
                found.append("exception")
        return found

    def test_the_marker_is_found_when_it_is_deliberately_written(self):
        logging.getLogger("sidedoor").warning("deliberate %s", MARKER)
        self.assertEqual(self.leaks(), ["log"])
        self.records.lines.clear()
        self.err.truncate(0), self.err.seek(0)
        (self.root / "reports").mkdir()
        (self.root / "reports" / "audit.jsonl").write_text(json.dumps({"cookie": MARKER}))
        self.kept.append(ValueError("cookie was " + MARKER))
        found = self.leaks()
        self.assertEqual(len(found), 2)
        self.assertIn("exception", found)
        self.assertTrue(found[0].endswith("audit.jsonl"))

    def test_it_reads_the_cookie_and_the_user_agent_and_writes_them_nowhere(self):
        session = self.read()
        self.assertEqual((session.cookie, session.user_agent), (MARKER, AGENT))   # not vacuous: the marker was read
        self.assertEqual(self.command.starts, 1)   # the product's own launch, once
        asked = [item["method"] for item in self.chrome.methods()]
        self.assertIn("Storage.getCookies", asked)
        for never in ("Page.navigate", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent", "Network.setCookie", "Storage.setCookies"):
            self.assertNotIn(never, asked)   # it only asks; it opens, clicks and types nothing
        self.assertNotIn(MARKER, repr(session) + str(session))
        self.assertEqual(self.leaks(), [])
        self.assertFalse((self.home / "site-sessions.enc").exists())   # no site session is saved by this path
        self.assertFalse((self.root / "reports" / linkedin_reader.FOLDER / "session").exists())   # the guard's records are removed
        # The browser's lock is free again.
        with run_locks.browser_session(self.root) as free:
            self.assertTrue(free)

    def test_not_signed_in_is_none(self):
        for cookies in ([], [{"name": "JSESSIONID", "value": MARKER, "domain": ".linkedin.com"}],
                        [{"name": "li_at", "value": MARKER, "domain": ".notlinkedin.com"}],
                        [{"name": "li_at", "value": MARKER, "domain": "linkedin.com.example.org"}],
                        [{"name": "li_at", "value": "  ", "domain": ".linkedin.com"}]):
            self.chrome.cookies = cookies
            self.assertIsNone(self.read())
        self.assertEqual(self.leaks(), [])

    def test_every_failure_is_a_bare_unavailable_and_the_marker_is_written_nowhere(self):
        def fails(kind=linkedin_session.Unavailable, **named):
            with self.assertRaises(kind) as raised:
                self.read(*named.pop("extra", ()), **named)
            self.assertIs(type(raised.exception), kind)
            self.assertEqual((raised.exception.args, raised.exception.__cause__, raised.exception.__context__), ((), None, None))
        fails(extra=("--no-endpoint",), wait=0.3)                       # the browser gave no endpoint
        fails(extra=("--windowed",), wait=0.3)                          # only a Chrome with a window: never attached to
        fails(supported=False)                                           # this computer cannot start it without a window
        self.chrome.raw = '{"id": 1, "result": {"cookies": "%s"}}' % MARKER   # malformed: not a list
        fails()
        self.chrome.raw = "not json " + MARKER
        fails()
        self.chrome.raw = None
        with mock.patch.object(linkedin_reader, "CALL_SECONDS", 0.2):    # no answer in time
            self.chrome.delays["Storage.getCookies"] = 2
            fails()
            self.chrome.delays.clear()
        self.chrome.agent = ""                                           # a sign-in, but no user agent to send with it
        fails()
        self.chrome.agent = AGENT
        with mock.patch.object(live_view, "debugger_url", side_effect=OSError("refused " + MARKER)):   # endpoint down
            fails()
        with mock.patch.object(browser_runtime, "unavailable", return_value="RUNTIME_MISSING"):
            fails()
        # Another browser session holds the browser: busy, a different answer, and nothing is started.
        held, release = threading.Event(), threading.Event()

        def hold():
            with run_locks.browser_session(self.root):
                held.set()
                release.wait(10)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(10))
        try:
            fails(linkedin_session.Busy)
            self.assertEqual(self.command.starts, 0)
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(self.leaks(), [])
        self.assertIn("LINKEDIN_SESSION_NOT_READ", "\n".join(self.records.lines))   # the kind of failure is logged
        self.assertEqual(self.read().cookie, MARKER)   # and after all that the next read works

    def test_through_the_seam_and_while_the_live_view_is_open(self):
        # `linkedin_session.current` is what the rest of Sidedoor calls.
        self.command = live_view_fakes.guard_command(self.chrome)
        previous = linkedin_session.set_reader(lambda home: linkedin_reader.read(
            home, self.root, {}, None, guard_command=self.command, endpoint_wait=5, is_supported=lambda config: True))
        self.addCleanup(linkedin_session.set_reader, previous)
        self.assertEqual(linkedin_session.current(self.home).cookie, MARKER)
        # The user is signing in in the dashboard's live view: its Chrome is read, nothing is started or stopped.
        guard = mock.Mock(endpoint=mock.Mock(return_value=self.chrome.endpoint))
        view = mock.Mock(session=mock.Mock(closing=False, guard=guard))
        with mock.patch.object(browser_runtime, "end_session") as ended:
            session = self.read(view=view)
        self.assertEqual((session.cookie, self.command.starts), (MARKER, 0))
        ended.assert_not_called()
        self.assertEqual(self.leaks(), [])

    def test_install_wires_the_seam_for_the_app(self):
        previous = linkedin_session.set_reader(None)
        self.addCleanup(linkedin_session.set_reader, previous)
        with self.assertRaises(linkedin_session.Unavailable):
            linkedin_session.current(self.home)
        with mock.patch.object(linkedin_reader, "read", return_value=None) as read:
            linkedin_reader.install(self.home, self.root, {"x": 1}, "VIEW")
            self.assertIsNone(linkedin_session.current())
            self.assertEqual(read.call_args.args, (self.home, self.root, {"x": 1}, "VIEW"))


if __name__ == "__main__":
    unittest.main()


class TheJoin(unittest.TestCase):
    """E18 on main's live view and Needs you cards: [Open Sidedoor's browser] opens the live view on LinkedIn's
    sign-in page; a LinkedIn step that needs the user has a Needs you card; Done connects outreach."""

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            import dashboard
            import license_fixtures
        except ImportError:
            self.skipTest("dashboard packages are not installed")
        import linkedin_connection
        import setup_steps
        self.lc, self.setup_steps, self.dashboard = linkedin_connection, setup_steps, dashboard
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.home.mkdir()
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        import paths
        Path(paths.database(Path(self.directory.name) / "data")).parent.mkdir(parents=True)   # an install's data folder exists
        self.browser = mock.Mock(is_open=mock.Mock(return_value=False), open=mock.Mock(return_value="opened"))
        self.app = dashboard.create_app("test-token-value", 47999, home=self.home, root=Path(self.directory.name) / "data",
                                        config={"text_model": {"model": "test/model"}}, browser=self.browser,
                                        relay_url="https://relay.test")
        self.client = TestClient(self.app, base_url="http://127.0.0.1:47999", follow_redirects=False)
        self.client.get("/", params={"t": "test-token-value"})
        self.post = {"origin": "http://127.0.0.1:47999", "hx-request": "true"}
        self.view = self.app.state.live_view
        self.addCleanup(linkedin_connection.attach, None)
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(
            lambda home: linkedin_session.Session(MARKER, AGENT)))
        self.addCleanup(linkedin_connection._sent.clear)
        self.accounts = (403, {"error": "LICENSE_NOT_LINKED"})
        self.calls = []

        def transport(method, path, **named):
            self.calls.append((method, path))
            if path == "/link/session":
                self.accounts = (200, {"items": [{"type": "LINKEDIN", "id": "acct-1", "name": "Alex", "status": "OK",
                                                  "connection_params": {"im": {"publicIdentifier": "alex"}}}]})
                return 200, {"connected": True, "kind": "create"}
            return (200, {"premium": False}) if path == "/users/me" else self.accounts
        import unipile_port
        patch = mock.patch.object(unipile_port, "RelayTransport", return_value=transport)
        patch.start()
        self.addCleanup(patch.stop)

    def can_show(self, yes=True):
        patch = mock.patch.object(self.view, "is_supported", lambda config: yes)
        patch.start()
        self.addCleanup(patch.stop)

    def target(self):
        found = [target for target in self.view.targets.values() if target.job == self.lc.CARD_KEY]
        return found[0] if found else None

    def done(self):
        """The user pressed Done in the live view: what the live view then calls, as it calls it."""
        ended = live_view.Ended(live_view.DONE, "https://www.linkedin.com/feed/", "linkedin.com", self.lc.CARD_KEY,
                                "sign_in", 20.0, {}, live_view.ENDED_WORDS[live_view.DONE])
        self.target().on_ended(ended)

    def test_open_sidedoors_browser_is_the_live_view_on_linkedins_sign_in_page(self):
        self.can_show()
        self.setup_steps.linkedin_status(self.home, "https://relay.test")
        for path, mark in (("/setup/linkedin/open", 'id="step-7"'), ("/settings/linkedin/open", 'id="connection-linkedin"')):
            shown = self.client.post(path, headers=self.post).text
            target = self.target()
            self.assertIsNotNone(target)
            self.assertEqual((target.url, target.purpose, target.domain), ("https://www.linkedin.com/login", "sign_in", "linkedin.com"))
            self.assertIn("/live-view/%s/fragment?start=1" % target.id, shown)   # inside the step: no separate window
            self.assertIn('hx-trigger="live-view-done"', shown)
            self.assertIn(mark, shown)
        self.browser.open.assert_not_called()
        self.assertEqual(len([t for t in self.view.targets.values() if t.job == self.lc.CARD_KEY]), 1)   # one step, wherever it is opened
        for page in ("/setup?open=7", "/settings"):
            self.assertIn("/static/live_view.js", self.client.get(page).text)
        # Done: Sidedoor reads its browser and connects outreach; nothing else to press.
        self.done()
        self.assertIn(("POST", "/link/session"), self.calls)
        state = self.setup_steps.load_state(self.home)
        self.assertEqual(state["steps"]["linkedin"]["status"], "done")
        self.assertIn("LinkedIn connected: Alex.", self.client.get("/setup/linkedin/status", headers={"hx-request": "true"}).text)
        self.assertNotIn(MARKER, json.dumps(state))
        # Cancel (or any other ending) connects nothing.
        self.calls.clear()
        self.target().on_ended(live_view.Ended(live_view.CANCEL, None, "linkedin.com", self.lc.CARD_KEY, "sign_in", 1.0, {}, "Cancelled."))
        self.assertEqual(self.calls, [])

    def test_where_the_live_view_cannot_be_shown_the_browser_window_opens_as_before(self):
        self.can_show(False)
        self.setup_steps.linkedin_status(self.home, "https://relay.test")
        shown = self.client.post("/settings/linkedin/open", headers=self.post).text
        self.browser.open.assert_called_once()
        self.assertNotIn("/live-view/", shown)
        self.assertIsNone(self.target())

    def test_a_linkedin_step_that_needs_the_user_has_a_needs_you_card(self):
        import access_cards
        import desktop_alerts
        import overview_page
        self.can_show()
        setup = self.setup_steps

        def cards():
            page = self.client.get("/access/section", params={"at": "overview"}).text
            return page, access_cards.linkedin_card()
        # Before Setup is finished: no card (Setup itself is where LinkedIn is connected).
        setup.linkedin_status(self.home, "https://relay.test")
        self.assertEqual(setup.load_state(self.home)["steps"]["linkedin"]["status"], "needs_you")
        self.assertIsNone(cards()[1])
        setup.update_state(lambda value: value.update(setup_finished_at="2026-11-01T00:00:00Z"), self.home)
        page, card = cards()
        self.assertEqual((card["company"], card["need"], card["step"]["label"]), ("LinkedIn", access_cards.LINKEDIN_WORDS, "Sign in"))
        words = "browser needs you to sign in to LinkedIn."   # after the apostrophe the page escapes
        self.assertIn(words, page)
        self.assertIn(">Sign in</button>", page)
        self.assertNotIn("Abandon", page)
        self.assertNotIn("· None", page)
        for wrong in ("Unipile", "I'm signed in", "Open Sidedoor's browser"):   # the card names its own button only
            self.assertNotIn(wrong, access_cards.LINKEDIN_WORDS)
        # Counted where Needs you is counted (the chip, the tray, the desktop notification).
        counted = self.client.get("/").text
        self.assertIn(words, counted)
        # Its button opens the same live view step as Setup and Settings do.
        step = self.client.post("/access/step", data={"job_key": self.lc.CARD_KEY, "at": "overview"}, headers=self.post).text
        self.assertIn("/live-view/%s/fragment?start=1" % self.target().id, step)
        self.done()
        self.assertEqual(setup.load_state(self.home)["steps"]["linkedin"]["status"], "done")
        page, card = cards()
        self.assertIsNone(card)
        self.assertNotIn(words, page)
        # A card drawn before that, pressed after: said plainly.
        late = self.client.post("/access/step", data={"job_key": self.lc.CARD_KEY, "at": "overview"}, headers=self.post).text
        self.assertIn(access_cards.LINKEDIN_GONE, late)
        # A state that asks nothing of the user ("reconnecting") has no card; the day-long outage has one, and
        # its Overview line is a new red condition, which is what the desktop notification is posted for.
        setup.record(self.home, "linkedin", "checking", self.lc.RECONNECTING, offer="signin")
        self.assertIsNone(access_cards.linkedin_card())
        waiting = desktop_alerts.health_conditions([overview_page.linkedin_item(setup.load_state(self.home)["steps"]["linkedin"])])
        setup.record(self.home, "linkedin", "needs_you", self.lc.UNREACHED_A_DAY, offer="signin")
        self.assertIsNone(access_cards.linkedin_card())   # a sign-in cannot mend what is not reached: no "sign in" card
        asked = desktop_alerts.health_conditions([overview_page.linkedin_item(setup.load_state(self.home)["steps"]["linkedin"])])
        self.assertEqual((asked[0]["tone"], asked[0]["text"]), ("error", self.lc.shown(self.lc.UNREACHED_A_DAY)))
        setup.record(self.home, "linkedin", "needs_you", self.lc.SIGN_IN_AGAIN, offer="reconnect")
        self.assertIsNotNone(access_cards.linkedin_card())
        self.assertNotEqual(waiting[0]["key"], asked[0]["key"])
        # Where the step cannot be shown there is no card with nothing to press; Overview's line still says it.
        self.can_show(False)
        self.assertIsNone(access_cards.linkedin_card())


class CardOnlyWhereASignInHelps(TheJoin):
    """Join review: the outreach card says "needs you to sign in", so it is raised only in the states a sign-in
    in Sidedoor's browser can clear. Every outreach state is listed here with card or no card."""
    test_open_sidedoors_browser_is_the_live_view_on_linkedins_sign_in_page = None
    test_where_the_live_view_cannot_be_shown_the_browser_window_opens_as_before = None
    test_a_linkedin_step_that_needs_the_user_has_a_needs_you_card = None

    def states(self):
        lc = self.lc
        support = self.setup_steps.CERTIFICATE_FAILED % "the LinkedIn service's"
        return [   # (status, sentence, offer, card)
            ("needs_you", "LinkedIn is not connected.", "connect", True),
            ("needs_you", lc.SIGN_IN_AGAIN, "reconnect", True),
            ("needs_you", lc.MESSAGES[lc.NOT_SIGNED_IN][1], "signin", True),
            ("needs_you", lc.MESSAGES[lc.REFUSED][1], "signin", True),
            ("needs_you", lc.MESSAGES[lc.MISMATCH][1], "signin", True),
            ("needs_you", lc.MESSAGES[lc.ELSEWHERE][1], "signin", True),
            ("needs_you", lc.NOT_CONNECTED_A_DAY, "signin", True),
            ("needs_you", lc.SENT_NOT_CONNECTED, "sent", False),          # already signed in: only waiting to show
            ("needs_you", lc.MESSAGES[lc.GONE][1], "signin", False),      # a sign-in cannot fix it: contact support
            ("needs_you", lc.DISCONNECTED_READ, "signin", False),         # the hourly check sends first
            ("needs_you", lc.UNREACHED_A_DAY, "signin", False),           # the browser or the service is not reached
            ("needs_you", lc.UNREADABLE, "signin", False),                # the browser cannot be read
            ("needs_you", lc.OPEN_MESSAGE, "signin", False),
            ("needs_you", "Needs your license first.", "none", False),
            ("failed", lc.UNREADABLE, "signin", False),
            ("failed", lc.MESSAGES[lc.BUSY][1], "signin", False),
            ("failed", lc.MESSAGES[lc.UNREACHABLE][1], "signin", False),
            ("failed", lc.MESSAGES[lc.LATER][1], "signin", False),
            ("failed", lc.MESSAGES[lc.TEMPORARY][1], "signin", False),
            ("failed", lc.MESSAGES[lc.UNAVAILABLE][1], "signin", False),
            ("failed", support, "signin", False),
            ("failed", "Your subscription isn't active. Check Settings > License.", "retry", False),
            ("checking", lc.RECONNECTING, "signin", False),
            ("done", "LinkedIn connected: Alex.", "reconnect", False)]

    def test_every_outreach_state_has_a_card_only_where_a_sign_in_can_clear_it(self):
        import access_cards
        self.can_show()
        self.setup_steps.update_state(lambda value: value.update(setup_finished_at="2026-11-01T00:00:00Z"), self.home)
        listed = self.states()
        # Every sentence the connection code can record is in the table.
        lc = self.lc
        known = {value for name, value in vars(lc).items() if name.isupper() and isinstance(value, str) and ". " in value + " "
                 and " " in value} | {message for unused, message in lc.MESSAGES.values()}
        known -= {lc.STILL_CONNECTED, lc.NO_CHROME, lc.BROWSER_BUSY}   # a note on a connected row; window-only answers
        self.assertEqual(known - {sentence for unused, sentence, offer, card in listed}, set())
        for status, sentence, offer, card in listed:
            self.setup_steps.record(self.home, "linkedin", status, sentence, offer=offer)
            self.assertEqual(access_cards.linkedin_card() is not None, card, sentence)

    def test_signed_in_done_and_not_yet_connected_has_no_card_and_a_second_done_sends_nothing(self):
        import access_cards
        self.can_show()
        setup = self.setup_steps
        setup.update_state(lambda value: value.update(setup_finished_at="2026-11-01T00:00:00Z"), self.home)
        setup.save_sender(self.home, "acct-1", "https://www.linkedin.com/in/alex")
        listing = (200, {"items": [{"type": "LINKEDIN", "id": "acct-1", "name": "Alex", "status": "CONNECTING"}]})
        self.accounts = listing

        def transport(method, path, **named):   # accepted every time; the account does not read connected yet
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "reconnect"}) if path == "/link/session" else self.accounts
        import unipile_port
        patch = mock.patch.object(unipile_port, "RelayTransport", return_value=transport)
        patch.start()
        self.addCleanup(patch.stop)
        setup.record(self.home, "linkedin", "needs_you", self.lc.SIGN_IN_AGAIN, offer="reconnect")
        self.assertIsNotNone(access_cards.linkedin_card())
        self.client.post("/access/step", data={"job_key": self.lc.CARD_KEY, "at": "overview"}, headers=self.post)
        self.done()
        sends = [call for call in self.calls if call[0] == "POST"]
        self.assertEqual(sends, [("POST", "/link/session")])
        step = setup.load_state(self.home)["steps"]["linkedin"]
        self.assertEqual((step["status"], step["message"]), ("needs_you", self.lc.SENT_NOT_CONNECTED))   # its own true sentence
        self.assertIsNone(access_cards.linkedin_card())   # no "needs you to sign in" card: the user did
        self.assertNotIn("needs you to sign in to LinkedIn", self.client.get("/access/section", params={"at": "overview"}).text)
        self.assertIn("does not show as connected yet", self.client.get("/settings").text)
        # Done again (from Setup or Settings) with the same sign-in: nothing is sent again.
        self.client.post("/settings/linkedin/open", headers=self.post)
        self.done()
        self.done()
        self.assertEqual([call for call in self.calls if call[0] == "POST"], sends)
        # A different sign-in is sent.
        linkedin_session.set_reader(lambda home: linkedin_session.Session("another-sign-in", AGENT))
        self.done()
        self.assertEqual(len([call for call in self.calls if call[0] == "POST"]), 2)

    def test_sentences_name_what_the_user_presses_in_each_case(self):
        import overview_page
        lc = self.lc
        setup = self.setup_steps
        told = [sentence for unused, sentence, offer, card in self.states() if "Open Sidedoor's browser" in sentence]
        # The must-hit control: as recorded, each of these names [I'm signed in] (true where the window opens).
        self.assertGreaterEqual(len(told), 6)
        self.assertTrue(all("I'm signed in" in sentence for sentence in told))
        for available in (True, False):
            self.can_show(available)
            for sentence in told:
                setup.record(self.home, "linkedin", "needs_you", sentence, offer="signin")
                step = setup.load_state(self.home)["steps"]["linkedin"]
                pages = [self.client.get("/settings").text, self.client.get("/setup?open=7").text,
                         overview_page.linkedin_item(step)["text"]]
                for page in pages:
                    page = page.replace("&#39;", "'")
                    row = page.split('id="connection-linkedin"')[1].split("<form")[0] if 'id="connection-linkedin"' in page else (
                        page.split('id="step-7"')[1].split("<form")[0] if 'id="step-7"' in page else page)
                    if available:   # the live view: sign in there, press Done
                        self.assertIn("press Done", row, sentence)
                        self.assertNotIn("click I'm signed in", row, sentence)
                        self.assertNotIn("in that window", row)
                    else:           # the window: come back and click [I'm signed in]
                        self.assertIn("click I'm signed in", row, sentence)
                        self.assertNotIn("press Done", row)
        # Sentences about the button that is on the row in both cases are left as they are.
        self.can_show(True)
        for sentence in (lc.DISCONNECTED_READ, lc.SENT_NOT_CONNECTED, lc.UNREADABLE):
            self.assertEqual(lc.shown(sentence), sentence)
        guide = self.client.get("/setup/guide/linkedin", headers={"hx-request": "true"}).text.replace("&#39;", "'")
        import dashboard
        if dashboard.GUIDE_PLATFORM != "windows":
            self.assertNotIn("in that window", guide)
            self.assertNotIn("click <strong>I'm signed in</strong> again", guide)
