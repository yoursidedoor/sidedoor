"""E18 (one LinkedIn sign-in): the relay's session route in front of a fake Unipile, and the computer
side against a fake browser reader. No network beyond 127.0.0.1, no browser.

The marker tests (owner A1) send a marker as the session, force every failure path and then look for it
in every place of ours: the relay's log lines, logging records, response bodies, every file beside the
relay database, and on the computer every file in the home, every returned state and every log record.
Each first proves it can find a marker that is deliberately written there (a must-hit control)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import io
import json
import logging
import ssl
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

import linkedin_connection
import linkedin_session
import orchestrator
import outreach
import setup_steps
import unipile_port
try:
    from relay_optional import (LICENSE, MINE, OTHER_LICENSE, THEIRS, UNLINKED_LICENSE, RelayHarness, needs_relay,
                                server)
except ImportError:   # run as tests.test_e18_one_signin from the repo root
    from tests.relay_optional import (LICENSE, MINE, OTHER_LICENSE, THEIRS, UNLINKED_LICENSE, RelayHarness,
                                      needs_relay, server)

# RelayHarness silences it; the marker test wants the real one
REAL_LOG_REQUEST = server.Handler.log_request if server else None
MARKER = "AQEDAT-MARKER-cookie-7f3a9c51e2"
AGENT = "Mozilla/5.0 (Macintosh) Chrome/142.0 Safari/537.36"
OK_ACCOUNT = (200, {"items": [{"type": "LINKEDIN", "id": MINE, "name": "Alex", "status": "OK",
                               "connection_params": {"im": {"publicIdentifier": "alex"}}}]})
LOST_ACCOUNT = (200, {"items": [{"type": "LINKEDIN", "id": MINE, "name": "Alex", "status": "CREDENTIALS"}]})


class Records(logging.Handler):
    """Every logging record of the process while attached, formatted with its traceback."""

    def __init__(self):
        super().__init__(level=0)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class Capture:
    """stderr, stdout and logging for the length of a test."""

    def __init__(self, test):
        self.err, self.out, self.records = io.StringIO(), io.StringIO(), Records()
        stack = contextlib.ExitStack()
        test.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stderr(self.err))
        stack.enter_context(contextlib.redirect_stdout(self.out))
        root = logging.getLogger()
        root.addHandler(self.records)
        stack.callback(root.removeHandler, self.records)

    def text(self):
        return self.err.getvalue() + self.out.getvalue() + "\n".join(self.records.lines)


def files_holding(folder, marker):
    """The files under `folder` whose bytes hold `marker` (a database's rows and its journal included)."""
    needle = marker.encode("utf-8")
    return sorted(str(path) for path in Path(folder).rglob("*") if path.is_file() and needle in path.read_bytes())


@needs_relay
class RelaySessionRoute(RelayHarness):
    def setUp(self):
        super().setUp()
        real = mock.patch.object(server.Handler, "log_request", REAL_LOG_REQUEST)
        real.start()
        self.addCleanup(real.stop)
        self.capture = Capture(self)
        self.bodies = []

    def post(self, body, license_key=LICENSE, raw=None):
        headers = {"content-type": "application/json"}
        if license_key:
            headers["X-License-Key"] = license_key
        data = raw if raw is not None else json.dumps(body).encode()
        status, unused, answer = self.raw_request("POST", "/link/session", headers, data)
        self.bodies.append(answer.decode("utf-8", "replace"))
        return status, json.loads(answer)

    def session(self, **extra):
        return dict({"session": MARKER, "user_agent": AGENT, "country": "us"}, **extra)

    def sent(self, path):
        return [json.loads(item["raw"]) for item in self.upstream.requests
                if item["method"] == "POST" and item["path"] == "/api/v1" + path]

    def leaks(self):
        found = files_holding(self.directory.name, MARKER)
        if MARKER in self.capture.text():
            found.append("log")
        if any(MARKER in body for body in self.bodies):
            found.append("response")
        return found

    def test_the_marker_is_found_when_it_is_deliberately_written(self):
        # The must-hit control: each place the real test searches does report a marker put there.
        server.sys.stderr.write("deliberate " + MARKER + "\n")
        self.assertEqual(self.leaks(), ["log"])
        logging.getLogger("relay").error("deliberate %s", MARKER)
        self.capture.err.truncate(0), self.capture.err.seek(0)
        self.assertEqual(self.leaks(), ["log"])
        self.capture.records.lines.clear()
        self.store.register(UNLINKED_LICENSE, MARKER)   # a database row
        self.assertEqual(len(self.leaks()), 1)
        self.assertIn("relay.sqlite3", self.leaks()[0])
        self.bodies.append("echo " + MARKER)
        self.assertIn("response", self.leaks())

    def test_reconnect_keeps_the_account_and_create_links_a_new_one(self):
        self.set_response("POST /accounts/" + MINE, (200, {"object": "AccountReconnected", "account_id": MINE}))
        self.assertEqual(self.post(self.session()), (200, {"connected": True, "kind": "reconnect"}))
        # Exactly the session, the user agent and the country (owner A2), under the relay's own key.
        self.assertEqual(self.sent("/accounts/" + MINE), [{"provider": "LINKEDIN", "access_token": MARKER,
                                                          "user_agent": AGENT, "country": "US"}])
        self.assertEqual(self.store.account_for(LICENSE), MINE)   # the same account: the sender id never moves
        self.assertEqual(setup_steps.sender_id_for(self.store.account_for(LICENSE)), zlib.crc32(MINE.encode()))
        self.assertEqual(self.sent("/accounts"), [])
        self.set_response("POST /accounts", (201, {"object": "AccountCreated", "account_id": "NEW9"}))
        without = {"session": MARKER, "user_agent": AGENT}
        self.assertEqual(self.post(without, UNLINKED_LICENSE), (200, {"connected": True, "kind": "create"}))
        self.assertEqual(self.sent("/accounts"), [{"provider": "LINKEDIN", "access_token": MARKER, "user_agent": AGENT}])
        self.assertEqual(self.store.account_for(UNLINKED_LICENSE), "NEW9")
        # B4: a license that has a linked account never gets a new one. Unipile answers 404 to the reconnect
        # (the fake's default): refused with a fixed code, no create call, the stored account id unchanged.
        self.set_response("POST /accounts", (201, {"object": "AccountCreated", "account_id": "NEW10"}))
        creates = len(self.sent("/accounts"))
        self.assertEqual(self.post(self.session(), OTHER_LICENSE), (409, {"error": "SESSION_ACCOUNT_GONE"}))
        self.assertEqual(len(self.sent("/accounts")), creates)
        self.assertEqual(self.store.account_for(OTHER_LICENSE), THEIRS)
        # A8: this route deletes nothing and queues nothing for removal.
        self.assertEqual(self.store.pending_removals(), [])
        self.assertNotIn("DELETE", [item["method"] for item in self.upstream.requests])
        self.assertEqual(self.leaks(), [])
        self.assertIn("POST session_link 200", self.capture.err.getvalue())   # the log names the route and status only

    def test_the_session_reaches_no_log_response_or_database_on_any_failure(self):
        echo = {"status": 401, "type": "errors/invalid_credentials", "detail": "bad token " + MARKER}
        # Unipile refuses and echoes the request; the relay passes none of it on.
        self.set_response("POST /accounts/" + MINE, (401, echo))
        self.assertEqual(self.post(self.session()), (422, {"error": "SESSION_REFUSED", "upstream_status": 401}))
        self.assertEqual(len(self.sent("/accounts/" + MINE)), 1)   # not vacuous: the session did travel
        self.set_response("POST /accounts/" + MINE, (500, echo))
        self.assertEqual(self.post(self.session()), (502, {"error": "SESSION_UPSTREAM_FAILED", "upstream_status": 500}))
        # Unipile's rate limit is not a refusal of the sign-in (round 3): the user is never told to sign in again for it.
        self.set_response("POST /accounts/" + MINE, (429, echo))
        self.assertEqual(self.post(self.session()), (502, {"error": "SESSION_UPSTREAM_FAILED", "upstream_status": 429}))
        self.set_response("POST /accounts/" + MINE, (202, {"object": "Checkpoint", "account_id": MINE, "echo": MARKER}))
        self.assertEqual(self.post(self.session())[0], 422)
        self.set_response("POST /accounts/" + MINE, (200, {"object": "AccountReconnected", "account_id": THEIRS}))
        self.assertEqual(self.post(self.session()), (502, {"error": "SESSION_ACCOUNT_MISMATCH"}))
        self.assertEqual(self.store.account_for(LICENSE), MINE)
        self.set_response("POST /accounts/" + MINE, (200, MARKER.encode()))   # not JSON
        self.assertEqual(self.post(self.session())[0], 200)   # a reconnect that names no account keeps this one
        # Create: an account another license holds, an unreadable answer, a refusal.
        self.set_response("POST /accounts", (201, {"object": "AccountCreated", "account_id": THEIRS, "echo": MARKER}))
        self.assertEqual(self.post(self.session(), UNLINKED_LICENSE), (409, {"error": "ACCOUNT_LINKED_ELSEWHERE"}))
        self.set_response("POST /accounts", (201, {"echo": MARKER}))
        self.assertEqual(self.post(self.session(), UNLINKED_LICENSE), (502, {"error": "SESSION_UPSTREAM_UNREADABLE"}))
        self.set_response("POST /accounts", (403, echo))
        self.assertEqual(self.post(self.session(), UNLINKED_LICENSE)[0], 422)
        self.assertIsNone(self.store.account_for(UNLINKED_LICENSE))
        # Requests the relay refuses itself.
        self.assertEqual(self.post(self.session(), "LIC-NOT-SOLD"), (401, {"error": "LICENSE_INVALID"}))
        self.assertEqual(self.post(self.session(), None), (401, {"error": "LICENSE_INVALID"}))
        self.assertEqual(self.post(self.session(country="USA")), (400, {"error": "COUNTRY_INVALID"}))
        self.assertEqual(self.post(self.session(user_agent="")), (400, {"error": "SESSION_INVALID"}))
        self.assertEqual(self.post(None, raw=b'{"session": "' + MARKER.encode() + b'", broken'), (400, {"error": "JSON_INVALID"}))
        self.assertEqual(self.post(self.session(pad="x" * server.MAX_BODY))[0], 413)
        # An error inside the relay whose text holds the session: answered RELAY_ERROR, echoed nowhere.
        with mock.patch.object(self.relay, "_upstream", side_effect=RuntimeError("request was " + MARKER)):
            self.assertEqual(self.post(self.session()), (500, {"error": "RELAY_ERROR"}))
        with mock.patch.object(self.store, "register_digest", side_effect=RuntimeError(MARKER)):
            self.set_response("POST /accounts", (201, {"object": "AccountCreated", "account_id": "NEW8"}))
            self.assertEqual(self.post(self.session(), UNLINKED_LICENSE), (500, {"error": "RELAY_ERROR"}))
        # Unipile cannot be reached.
        self.upstream.shutdown()
        self.upstream.server_close()
        self.assertEqual(self.post(self.session()), (502, {"error": "UPSTREAM_UNAVAILABLE"}))
        self.upstream = type("Closed", (), {"shutdown": lambda self: None, "server_close": lambda self: None})()
        self.assertEqual(self.leaks(), [])
        self.assertNotIn(MARKER, repr(self.relay))
        self.assertGreaterEqual(self.capture.err.getvalue().count("POST session_link"), 12)   # the paths were logged
        self.assertEqual(self.store.pending_removals(), [])

    def test_two_requests_for_one_unlinked_license_create_one_account(self):
        # Round 4: a double click, or the button during the hourly send. Unipile is slow; both arrive.
        self.set_response("POST /accounts", (201, {"object": "AccountCreated", "account_id": "NEW11"}))
        self.set_response("POST /accounts/" + MINE, (200, {"object": "AccountReconnected", "account_id": MINE}))
        entered, release = threading.Event(), threading.Event()
        upstream = self.relay._upstream

        def slow(method, path, *args, **named):
            if (method, path) == ("POST", "/accounts"):
                entered.set()
                release.wait(10)
            return upstream(method, path, *args, **named)
        answers = []
        with mock.patch.object(self.relay, "_upstream", slow):
            first = threading.Thread(target=lambda: answers.append(self.post(self.session(), UNLINKED_LICENSE)))
            first.start()
            self.assertTrue(entered.wait(10))
            second = self.post(self.session(), UNLINKED_LICENSE)   # while the first is still at Unipile
            other = self.post(self.session())                      # another license never waits on it
            release.set()
            first.join(10)
        self.assertEqual(second, (409, {"error": "SESSION_IN_PROGRESS"}))
        self.assertEqual(other, (200, {"connected": True, "kind": "reconnect"}))
        self.assertEqual(answers, [(200, {"connected": True, "kind": "create"})])
        self.assertEqual(len(self.sent("/accounts")), 1)   # exactly one create call
        self.assertEqual(self.store.account_for(UNLINKED_LICENSE), "NEW11")
        self.assertEqual(self.store.pending_removals(), [])
        self.assertEqual(self.store.removals(), [])   # nothing queued for deletion
        # Afterwards the same license reconnects its one account; the lock was released.
        self.set_response("POST /accounts/NEW11", (200, {"object": "AccountReconnected", "account_id": "NEW11"}))
        self.assertEqual(self.post(self.session(), UNLINKED_LICENSE), (200, {"connected": True, "kind": "reconnect"}))
        self.assertEqual(len(self.sent("/accounts")), 1)
        self.assertEqual(self.leaks(), [])

    def test_the_app_side_sends_through_the_relay_end_to_end(self):
        home = Path(self.directory.name) / "home"
        home.mkdir()
        (home / "license.key").write_text(LICENSE + "\n")
        self.addCleanup(linkedin_session.set_reader,
                        linkedin_session.set_reader(lambda home: linkedin_session.Session(MARKER, AGENT)))
        self.set_response("POST /accounts/" + MINE, (200, {"object": "AccountReconnected", "account_id": MINE}))
        self.assertEqual(linkedin_connection.send(home, self.base), linkedin_connection.CONNECTED)
        self.assertEqual(self.sent("/accounts/" + MINE), [{"provider": "LINKEDIN", "access_token": MARKER, "user_agent": AGENT}])
        self.set_response("POST /accounts/" + MINE, (401, {"detail": MARKER}))
        self.assertEqual(linkedin_connection.send(home, self.base), linkedin_connection.REFUSED)
        self.assertEqual(self.leaks(), [])


class ComputerSide(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        (self.home / "license.key").write_text("SD-TESTKEY\n")
        self.capture = Capture(self)
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(None))
        self.addCleanup(linkedin_connection._sent.clear)
        linkedin_connection._sent.clear()
        linkedin_connection.attach(None)   # no dashboard here: another test's app must not decide the wording
        self.accounts = OK_ACCOUNT
        self.session_answer = (200, {"connected": True, "kind": "reconnect"})
        self.calls = []
        self.states = []
        # By default the install was connected once (its sending identity is saved), as every user whose
        # LinkedIn can "disconnect" has been; a first-connect test calls `never_connected`.
        setup_steps.save_sender(self.home, MINE, "https://www.linkedin.com/in/alex")
        home = mock.patch.object(orchestrator.user_profile, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        # Every write to the home is kept too, so a value written and then overwritten is still seen.
        write = setup_steps._write_private
        spy = mock.patch.object(setup_steps, "_write_private", side_effect=lambda path, text, *rest, **named: (
            self.states.append(str(text)), write(path, text, *rest, **named))[1])
        spy.start()
        self.addCleanup(spy.stop)

    def transport(self, method, path, **named):
        self.calls.append((method, path))
        if path == linkedin_connection.SESSION_PATH:
            self.sent_body = dict(named["body"])
            if isinstance(self.session_answer, BaseException):
                raise self.session_answer
            if self.session_answer[0] == 200:
                self.accounts = OK_ACCOUNT
            return self.session_answer
        if path == "/users/me":
            return 200, {"object": "AccountOwnerProfile", "premium": True}
        return self.accounts

    def never_connected(self):
        (self.home / setup_steps.SENDER_FILE).unlink(missing_ok=True)
        self.assertIsNone(setup_steps.saved_sender(self.home))

    def signed_in(self, cookie=MARKER):
        linkedin_session.set_reader(lambda home: linkedin_session.Session(cookie, AGENT))

    def step(self):
        state = setup_steps.load_state(self.home)
        self.states.append(json.dumps(state))
        return state["steps"].get("linkedin", {})

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def leaks(self):
        found = files_holding(self.home, MARKER)
        if MARKER in self.capture.text():
            found.append("log")
        if any(MARKER in state for state in self.states):
            found.append("state")
        return found

    def test_the_marker_is_found_when_it_is_deliberately_written(self):
        logging.getLogger("sidedoor").warning("deliberate %s", MARKER)
        self.assertEqual(self.leaks(), ["log"])
        self.capture.records.lines.clear()
        setup_steps.record(self.home, "linkedin", "failed", "deliberate " + MARKER)
        self.step()
        found = self.leaks()
        self.assertIn("state", found)   # a returned state
        self.assertTrue(any(name.endswith("setup-state.json") for name in found), found)   # a file in the home

    def test_the_session_is_written_nowhere_on_any_path(self):
        self.signed_in()
        failure = OSError("could not send " + MARKER)
        certificate = ssl.SSLCertVerificationError("certificate verify failed " + MARKER)
        for answer in ((200, {"connected": True, "kind": "create"}),
                       (422, {"error": "SESSION_REFUSED", "upstream_status": 401, "echo": MARKER}),
                       (401, {"error": "LICENSE_INVALID", "echo": MARKER}), (500, MARKER), (404, {"error": "ROUTE_NOT_FOUND"}),
                       failure, certificate, ValueError(MARKER)):
            self.session_answer = answer
            self.accounts = LOST_ACCOUNT
            with mock.patch.object(unipile_port, "refused_now"):
                self.states.append(json.dumps(linkedin_connection.connect(self.home, transport=self.transport)))
                self.step()
                self.states.append(json.dumps(setup_steps.linkedin_status(self.home, transport=self.transport)))
                self.states.append(str(linkedin_connection.keep(self.home, transport=self.transport)))
            self.step()
        self.assertEqual(self.sent_body, {"session": MARKER, "user_agent": AGENT})   # not vacuous: it was sent
        self.assertGreaterEqual(len(self.posts()), 8)
        session = linkedin_session.current(self.home)
        self.states.extend([repr(session), str(session), "%s" % [session]])
        with self.assertRaises(TypeError):
            json.dumps(session)
        with self.assertRaises(TypeError):
            __import__("pickle").dumps(session)
        # A reader whose own error holds the session: only a bare Unavailable comes out.
        def broken(home):
            raise RuntimeError("cookie was " + MARKER)
        linkedin_session.set_reader(broken)
        with self.assertRaises(linkedin_session.Unavailable) as raised:
            linkedin_session.current(self.home)
        self.states.append(repr(raised.exception) + str(raised.exception.__context__) + str(raised.exception.__cause__))
        self.assertEqual(self.leaks(), [])

    def test_an_install_connected_the_old_way_is_left_alone_and_asked_nothing(self):
        # Owner A4: connected through the hosted page before E18, signed in in Sidedoor's browser too.
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        self.signed_in()
        self.assertEqual(linkedin_connection.keep(self.home, transport=self.transport), "READ")
        self.assertEqual(linkedin_connection.keep(self.home, transport=self.transport), "READ")
        self.assertEqual(self.posts(), [])   # its session is never sent while it reads connected
        self.assertEqual((self.step()["status"], self.step()["message"]), ("done", "LinkedIn connected: Alex."))
        state = setup_steps.load_state(self.home)
        self.assertNotIn(linkedin_connection.MODE_KEY, state)
        self.assertFalse(linkedin_connection.held(self.home))
        self.assertIs(orchestrator.unipile_ready({"sender_id": 5, "sender_profile": "p"}, port="PORT"), "PORT")
        # Its next reconnect moves it to the new way, with no question: the account reads disconnected,
        # the browser's session is sent, and it is connected again.
        self.accounts = LOST_ACCOUNT
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual(self.posts(), [("POST", "/link/session")])
        self.assertEqual(self.step()["status"], "done")
        self.assertEqual(setup_steps.load_state(self.home)[linkedin_connection.MODE_KEY], "session")
        self.assertFalse(linkedin_connection.held(self.home))

    def test_a_disconnect_holds_outreach_and_asks_only_when_resending_fails(self):
        self.accounts = LOST_ACCOUNT
        # The browser cannot be read now: held, tried again later, the user is not asked.
        def busy(home):
            raise linkedin_session.Busy()
        linkedin_session.set_reader(busy)
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual((self.step()["status"], self.step()["message"]), ("checking", linkedin_connection.RECONNECTING))
        self.assertTrue(linkedin_connection.held(self.home))
        # Owner A5: every outreach step waits on a hold that spends no try; nothing fails or is dropped.
        with self.assertRaises(unipile_port.LinkedInDisconnected) as raised:
            orchestrator.unipile_ready({"sender_id": 5, "sender_profile": "p"}, port="PORT")
        self.assertTrue(orchestrator.license_hold(raised.exception))
        self.assertEqual(outreach._read_error(raised.exception), ("RETRY_WAIT", "LINKEDIN_DISCONNECTED"))
        self.assertIn("LINKEDIN_DISCONNECTED", outreach._setup_hold_codes())
        # The relay cannot be reached: the same.
        self.signed_in()
        self.session_answer = OSError("offline")
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual(self.step()["status"], "checking")
        # The browser is signed out, or the sign-in is refused: now the user is asked.
        linkedin_session.set_reader(lambda home: None)
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual((self.step()["status"], self.step()["offer"]), ("needs_you", "reconnect"))
        self.assertIn("sign in again", self.step()["message"])
        self.signed_in()
        self.session_answer = (422, {"error": "SESSION_REFUSED", "upstream_status": 401})
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual(self.step()["status"], "needs_you")
        self.assertTrue(linkedin_connection.held(self.home))
        # The user signs in again and clicks [I'm signed in]: connected, outreach goes on.
        self.session_answer = (200, {"connected": True, "kind": "reconnect"})
        linkedin_connection.connect(self.home, transport=self.transport)
        self.assertEqual(self.step()["status"], "done")
        self.assertFalse(linkedin_connection.held(self.home))
        self.assertIs(orchestrator.unipile_ready({"sender_id": 5, "sender_profile": "p"}, port="PORT"), "PORT")
        self.assertEqual(setup_steps.saved_premium(self.home)["premium"], True)   # A6: the Premium check still runs

    def test_a_browser_that_cannot_be_read_never_promises_a_reconnect(self):
        # Review round 1, B2: disconnected and no reader at all (set_reader(None) in setUp). Nothing can
        # reconnect it, so nothing shown may say a reconnect is under way or coming.
        self.accounts = LOST_ACCOUNT
        shown = []
        for unused in range(4):
            linkedin_connection.keep(self.home, transport=self.transport)
            step = self.step()
            shown.append(step)
            self.assertTrue(linkedin_connection.held(self.home))   # outreach waits throughout
            with self.assertRaises(unipile_port.LinkedInDisconnected):
                orchestrator.unipile_ready({"sender_id": 5, "sender_profile": "p"}, port="PORT")
        self.assertEqual(self.posts(), [])   # the outcome: no reconnect was ever tried
        for step in shown:
            self.assertNotEqual(step["status"], "checking", step)   # never shown as in progress
            text = step["message"].lower()
            for promise in ("reconnecting", "tries again", "try again", "will ", "within", "soon", "busy"):
                self.assertNotIn(promise, text)
            self.assertIn("could not read its browser", text)
        self.assertEqual([step["status"] for step in shown], ["failed", "failed", "needs_you", "needs_you"])
        # [I'm signed in] with no reader says the same true thing, not "busy".
        linkedin_connection.connect(self.home, transport=self.transport)
        self.assertNotIn("busy", self.step()["message"].lower())
        self.assertIn("could not read its browser", self.step()["message"])

    def test_a_busy_browser_says_it_will_try_again_and_does(self):
        # The control: a browser that is only busy now. The words may promise another try, and it happens.
        self.accounts = LOST_ACCOUNT
        tries = []

        def reader(home):
            tries.append(1)
            if len(tries) == 1:
                raise linkedin_session.Busy()
            return linkedin_session.Session(MARKER, AGENT)
        linkedin_session.set_reader(reader)
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual((self.step()["status"], self.step()["message"]), ("checking", linkedin_connection.RECONNECTING))
        self.assertTrue(linkedin_connection.held(self.home))
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual(self.posts(), [("POST", "/link/session")])
        self.assertEqual(self.step()["status"], "done")
        self.assertFalse(linkedin_connection.held(self.home))

    def test_relay_refusals_the_user_must_act_on_each_say_so(self):
        # B3 and B4: these never clear by waiting, so each asks the user with its own sentence; outreach stays held.
        self.accounts = LOST_ACCOUNT
        self.signed_in()
        seen = []
        for answer, words in (((409, {"error": "ACCOUNT_LINKED_ELSEWHERE"}), "already connected to another Sidedoor license"),
                              ((502, {"error": "SESSION_ACCOUNT_MISMATCH"}), "signed in to a different LinkedIn account"),
                              ((409, {"error": "SESSION_ACCOUNT_GONE"}), "no longer exists")):
            self.session_answer = answer
            for act in (lambda: linkedin_connection.keep(self.home, transport=self.transport),
                        lambda: linkedin_connection.connect(self.home, transport=self.transport)):
                act()
                step = self.step()
                self.assertEqual(step["status"], "needs_you", step)
                self.assertIn(words, step["message"])
                seen.append(step["message"])
                self.assertTrue(linkedin_connection.held(self.home))
        for message in seen:
            for wrong in ("few minutes", "reconnecting", "tries again", "ACCOUNT", "SESSION", "409", "502"):
                self.assertNotIn(wrong, message)

    def test_a_listed_but_disconnected_account_never_reads_connected(self):
        # B5: the relay accepted the session, but the account does not read connected. No read, of any kind,
        # at any later time, may say connected or release the hold.
        self.accounts = LOST_ACCOUNT
        self.signed_in()
        self.session_answer = (202, {"connected": True})   # accepted by the fake below, account left as it is

        def transport(method, path, **named):
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "reconnect"}) if path == "/link/session" else self.accounts
        linkedin_connection.connect(self.home, transport=transport)
        sent = self.step()
        self.assertEqual((sent["status"], sent["message"]), ("needs_you", linkedin_connection.SENT_NOT_CONNECTED))
        self.assertTrue(linkedin_connection.held(self.home))
        for unused in range(3):   # page reads (Check again), now and later
            setup_steps.linkedin_status(self.home, transport=transport)
            self.assertNotEqual(self.step()["status"], "done")
            self.assertTrue(linkedin_connection.held(self.home))
        # The states older versions left behind (a hosted link in flight) do not read connected either.
        for kind in ("create", "session", "reconnect"):
            setup_steps.update_state(lambda value: value.update(linkedin_link_started_at=setup_steps.now_iso(),
                                                                linkedin_link_kind=kind), self.home)
            setup_steps.linkedin_status(self.home, transport=transport)
            self.assertNotEqual(self.step()["status"], "done", kind)
            self.assertTrue(linkedin_connection.held(self.home), kind)
        with self.assertRaises(unipile_port.LinkedInDisconnected):
            orchestrator.unipile_ready({"sender_id": 5, "sender_profile": "p"}, port="PORT")
        self.accounts = OK_ACCOUNT   # it does connect: the next read says so and outreach goes on
        setup_steps.linkedin_status(self.home, transport=transport)
        self.assertEqual(self.step()["status"], "done")
        self.assertFalse(linkedin_connection.held(self.home))

    def test_a_page_read_never_sends_the_session(self):
        # B6: the status routes only read. Sending stays with the hourly check and [I'm signed in].
        self.accounts = LOST_ACCOUNT
        self.signed_in()
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        setup_steps.linkedin_status(self.home, transport=self.transport)
        self.assertEqual(self.posts(), [])
        step = self.step()
        self.assertEqual((step["status"], step["message"]), ("needs_you", linkedin_connection.DISCONNECTED_READ))
        self.assertTrue(linkedin_connection.held(self.home))
        try:
            from fastapi.testclient import TestClient
            import dashboard
        except ImportError:
            return
        app = dashboard.create_app("test-token-value", 47999, home=self.home, root=self.home / "data",
                                   config={"text_model": {"model": "test/model"}}, browser=mock.Mock())
        client = TestClient(app, base_url="http://127.0.0.1:47999", follow_redirects=False)
        client.get("/", params={"t": "test-token-value"})
        with mock.patch.object(unipile_port, "RelayTransport", return_value=self.transport):
            for path in ("/setup/linkedin/status", "/settings/linkedin/status"):
                self.assertEqual(client.get(path, headers={"hx-request": "true"}).status_code, 200)
        self.assertEqual(self.posts(), [])
        self.assertIn(("GET", "/accounts"), self.calls)
        linkedin_connection.keep(self.home, transport=self.transport)   # the hourly check does send
        self.assertEqual(self.posts(), [("POST", "/link/session")])

    def test_a_day_without_reaching_the_browser_or_the_relay_asks_and_notifies_once(self):
        # A12 (owner): retried every hour; after 24 hours a Needs you state and one notification.
        import datetime as dt
        self.accounts = LOST_ACCOUNT
        self.signed_in()
        self.session_answer = OSError("offline")
        start = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        clock = [start]
        with mock.patch.object(linkedin_connection, "_now", lambda: clock[0]):
            for hour in range(24):
                clock[0] = start + dt.timedelta(hours=hour)
                if hour == 5:   # the browser is busy for a while: the same wait
                    def busy(home):
                        raise linkedin_session.Busy()
                    linkedin_session.set_reader(busy)
                linkedin_connection.keep(self.home, transport=self.transport)
                self.assertEqual((self.step()["status"], self.step()["message"]), ("checking", linkedin_connection.RECONNECTING))
                self.assertIsNone(linkedin_connection.pending_notice(self.home))
            self.assertEqual(len(self.posts()), 5)   # it kept trying
            clock[0] = start + dt.timedelta(hours=24)
            linkedin_connection.keep(self.home, transport=self.transport)
            step = self.step()
            self.assertEqual((step["status"], step["message"]), ("needs_you", linkedin_connection.UNREACHED_A_DAY))
            notice = linkedin_connection.pending_notice(self.home)
            self.assertEqual(notice["message"], linkedin_connection.UNREACHED_A_DAY)
            # The round hands it to the product's notification outbox, once.
            import sqlite3
            database = sqlite3.connect(":memory:")
            database.execute("CREATE TABLE notification_outbox(event_key TEXT PRIMARY KEY, message TEXT, state TEXT, updated_at TEXT)")
            for unused in range(2):
                orchestrator.deliver_linkedin_notice(database, self.home)
                clock[0] += dt.timedelta(hours=1)
                linkedin_connection.keep(self.home, transport=self.transport)   # still failing: still asked, no new notice
                self.assertEqual(self.step()["status"], "needs_you")
            self.assertEqual(database.execute("SELECT message, state FROM notification_outbox").fetchall(),
                             [(linkedin_connection.UNREACHED_A_DAY, "PENDING")])
            self.assertTrue(linkedin_connection.held(self.home))
            # Reachable again: it reconnects, and a later outage starts a new day of its own.
            self.signed_in()
            self.session_answer = (200, {"connected": True, "kind": "reconnect"})
            linkedin_connection.keep(self.home, transport=self.transport)
            self.assertEqual(self.step()["status"], "done")
            self.accounts = LOST_ACCOUNT
            self.session_answer = OSError("offline")
            linkedin_connection.keep(self.home, transport=self.transport)
            self.assertEqual(self.step()["status"], "checking")

    def test_a_held_port_makes_no_write_request(self):
        # A9: the check sits at the write itself, so a pass already past the readiness check sends nothing.
        with mock.patch.object(unipile_port, "RelayTransport", return_value=self.transport):
            port = unipile_port.for_install({"linkedin_provider": "unipile"}, self.home, account_id=MINE)
        linkedin_connection.hold(self.home)
        for write in (lambda: port.send_invitation("P1", "hello"), lambda: port.send_message("c1", "hello"),
                      lambda: port.start_chat("P1", "hello")):
            with self.assertRaises(unipile_port.RelayRefusedWrite) as raised:
                write()
            self.assertTrue(raised.exception.never_forwarded)
            self.assertEqual(raised.exception.hold.code, "LINKEDIN_DISCONNECTED")
        self.assertEqual(self.calls, [])
        linkedin_connection.release(self.home)
        self.accounts = (201, {"object": "UserInvitationSent"})
        port.send_invitation("P1", "hello")
        self.assertEqual(self.posts(), [("POST", "/users/invite")])

    def test_every_outcome_of_the_hourly_check_shows_a_true_sentence(self):
        # Review round 2: "reconnecting ... within the hour" only where the next hourly try can work with
        # nothing done by the user. Every outcome word is listed here, so a new one cannot fall into it.
        lc = linkedin_connection
        certificate = setup_steps.CERTIFICATE_FAILED % "the LinkedIn service's"
        expected = {
            lc.CONNECTED: ("done", "LinkedIn connected: Alex."),
            lc.BUSY: ("checking", lc.RECONNECTING),            # the browser frees up by itself
            lc.UNREACHABLE: ("checking", lc.RECONNECTING),     # the network or the service comes back by itself
            lc.LATER: ("checking", lc.RECONNECTING),           # a rate limit or the provider's own trouble passes
            lc.CERTIFICATE: ("failed", certificate),           # the same sentence the button shows
            lc.TEMPORARY: ("failed", lc.MESSAGES[lc.TEMPORARY][1]),
            lc.UNAVAILABLE: ("failed", lc.UNREADABLE),
            lc.NOT_SIGNED_IN: ("needs_you", lc.SIGN_IN_AGAIN),
            lc.REFUSED: ("needs_you", lc.SIGN_IN_AGAIN),
            lc.ELSEWHERE: ("needs_you", lc.MESSAGES[lc.ELSEWHERE][1]),
            lc.MISMATCH: ("needs_you", lc.MESSAGES[lc.MISMATCH][1]),
            lc.GONE: ("needs_you", lc.MESSAGES[lc.GONE][1]),
            lc.LICENSE: ("failed", "Your subscription isn't active. Check Settings > License."),
            lc.NO_LICENSE: ("needs_you", "Needs your license first."),
        }
        self.assertEqual(set(lc.OUTCOMES), set(expected))
        words = {name: value for name, value in vars(lc).items() if name.isupper() and isinstance(value, str) and value == name}
        self.assertEqual(set(words), set(expected))   # every outcome word the module defines is in OUTCOMES
        for outcome, (status, message) in expected.items():
            setup_steps.update_state(lambda value: [value.pop(key, None) for key in list(value) if key.startswith("linkedin_")], self.home)
            self.accounts = LOST_ACCOUNT

            def sent(*args, **named):
                if outcome == lc.CONNECTED:
                    self.accounts = OK_ACCOUNT
                return outcome
            with mock.patch.object(lc, "send", sent):
                lc.keep(self.home, transport=self.transport)
            step = self.step()
            self.assertEqual((step["status"], step["message"]), (status, message), outcome)
            if outcome not in (lc.BUSY, lc.UNREACHABLE, lc.LATER):
                for promise in ("reconnecting", "tries again", "within the hour"):
                    self.assertNotIn(promise, step["message"].lower(), outcome)
            if outcome != lc.CONNECTED:
                self.assertTrue(lc.held(self.home), outcome)
        # The button shows the same sentence as the hourly check for a certificate failure and for an
        # answer that would fail the same way again.
        for outcome in (lc.CERTIFICATE, lc.TEMPORARY):
            with mock.patch.object(lc, "send", return_value=outcome):
                lc.connect(self.home, transport=self.transport)
            self.assertEqual(self.step()["message"], expected[outcome][1])
        self.assertNotIn("few minutes", lc.MESSAGES[lc.TEMPORARY][1])

    def test_only_a_service_that_cannot_be_reached_counts_as_unreachable(self):
        lc = linkedin_connection
        self.signed_in()
        for answer, outcome in (((400, {"error": "SESSION_INVALID"}), lc.TEMPORARY),
                                ((502, {"error": "SESSION_UPSTREAM_FAILED", "upstream_status": 500}), lc.LATER),
                                ((502, {"error": "SESSION_UPSTREAM_FAILED", "upstream_status": 429}), lc.LATER),
                                ((429, {"error": "TOO_MANY_REQUESTS"}), lc.LATER),
                                ((429, "slow down"), lc.LATER),   # a rate limit is never "sign in again"
                                ((408, None), lc.LATER),
                                ((418, None), lc.TEMPORARY),
                                ((502, {"error": "SESSION_UPSTREAM_UNREADABLE"}), lc.TEMPORARY),
                                ((500, {"error": "RELAY_ERROR"}), lc.TEMPORARY),
                                ((404, {"error": "ROUTE_NOT_FOUND"}), lc.TEMPORARY),
                                ((502, {"error": "UPSTREAM_UNAVAILABLE"}), lc.UNREACHABLE),
                                ((504, {"error": "UPSTREAM_TIMEOUT"}), lc.UNREACHABLE),
                                ((503, {"error": "LICENSE_CHECK_UNAVAILABLE"}), lc.UNREACHABLE),
                                ((503, "no healthy machine"), lc.UNREACHABLE),   # not the relay's own answer
                                (OSError("offline"), lc.UNREACHABLE)):
            self.session_answer = answer
            self.assertEqual(lc.send(self.home, transport=self.transport), outcome, answer)

    @needs_relay
    def test_every_answer_the_relay_can_give_on_this_route_is_mapped_on_purpose(self):
        # Round 3: each real answer of POST /link/session (its own code and the shared request handling) has
        # a deliberate outcome. A code added to the relay later fails here until it is mapped.
        import inspect
        import re
        lc = linkedin_connection
        mapped = {
            "LICENSE_INVALID": lc.LICENSE, "LICENSE_CHECK_UNAVAILABLE": lc.UNREACHABLE,
            "UPSTREAM_TIMEOUT": lc.UNREACHABLE, "UPSTREAM_UNAVAILABLE": lc.UNREACHABLE,
            "SESSION_REFUSED": lc.REFUSED, "SESSION_UPSTREAM_FAILED": lc.LATER,
            "ACCOUNT_LINKED_ELSEWHERE": lc.ELSEWHERE, "SESSION_ACCOUNT_MISMATCH": lc.MISMATCH,
            "SESSION_ACCOUNT_GONE": lc.GONE, "SESSION_IN_PROGRESS": lc.LATER,
            # The app's own request was wrong, or the relay failed: the same request would fail again.
            "SESSION_INVALID": lc.TEMPORARY, "COUNTRY_INVALID": lc.TEMPORARY, "SESSION_UPSTREAM_UNREADABLE": lc.TEMPORARY,
            "JSON_INVALID": lc.TEMPORARY, "BODY_REQUIRED": lc.TEMPORARY, "BODY_TOO_LARGE": lc.TEMPORARY,
            "CONTENT_LENGTH_REQUIRED": lc.TEMPORARY, "CONTENT_LENGTH_INVALID": lc.TEMPORARY,
            "ROUTE_NOT_FOUND": lc.TEMPORARY, "RELAY_ERROR": lc.TEMPORARY,
        }
        relay = server.Relay
        source = "".join(inspect.getsource(part) for part in (
            relay._session_link, relay._session_answer, relay._body, relay._license, relay._upstream,
            server.parse_json_body, server.match_route, server.Handler._serve))
        codes = set(re.findall(r'"([A-Z][A-Z_]{5,})"', source)) - {"POST", "LINKEDIN"}
        self.assertEqual(codes - set(mapped), set())
        self.signed_in()
        statuses = {"LICENSE_INVALID": 401, "LICENSE_CHECK_UNAVAILABLE": 503, "UPSTREAM_TIMEOUT": 504, "SESSION_REFUSED": 422,
                    "ROUTE_NOT_FOUND": 404, "RELAY_ERROR": 500, "BODY_TOO_LARGE": 413, "CONTENT_LENGTH_REQUIRED": 411}
        with mock.patch.object(unipile_port, "refused_now"):
            for code, outcome in mapped.items():
                status = statuses.get(code, 502 if "UPSTREAM" in code or code == "SESSION_ACCOUNT_MISMATCH" else
                                      409 if code in ("ACCOUNT_LINKED_ELSEWHERE", "SESSION_ACCOUNT_GONE", "SESSION_IN_PROGRESS") else 400)
                self.session_answer = (status, {"error": code})
                self.assertEqual(lc.send(self.home, transport=self.transport), outcome, code)

    def test_the_button_and_the_hourly_check_never_send_at_the_same_time(self):
        # Round 4: one send at a time on the computer. A second click, or the hourly check, while a send is
        # in flight waits for it and sends nothing itself.
        self.signed_in()
        entered, release = threading.Event(), threading.Event()

        def slow(method, path, **named):
            if path == linkedin_connection.SESSION_PATH:
                entered.set()
                release.wait(10)
            return self.transport(method, path, **named)
        self.accounts = LOST_ACCOUNT
        first = threading.Thread(target=lambda: linkedin_connection.connect(self.home, transport=slow))
        first.start()
        self.assertTrue(entered.wait(10))
        done = []
        others = [threading.Thread(target=lambda: done.append(linkedin_connection.connect(self.home, transport=slow))),
                  threading.Thread(target=lambda: done.append(linkedin_connection.keep(self.home, transport=slow)))]
        for thread in others:
            thread.start()
        threading.Event().wait(0.3)
        self.assertEqual(done, [])   # both wait for the send in flight
        release.set()
        for thread in [first] + others:
            thread.join(10)
        self.assertEqual(len(done), 2)
        self.assertEqual(self.posts(), [("POST", "/link/session")])   # one send in all
        self.assertEqual(self.step()["status"], "done")

    def test_three_unreadable_tries_must_be_in_a_row(self):
        # B12: a busy or unreachable hour in between starts the count again.
        self.accounts = LOST_ACCOUNT
        seen = []

        def hour(reader):
            linkedin_session.set_reader(reader)
            linkedin_connection.keep(self.home, transport=self.transport)
            seen.append(self.step()["status"])

        def busy(home):
            raise linkedin_session.Busy()
        for reader in (None, None, busy, None, None, None):
            hour(reader)
        self.assertEqual(seen, ["failed", "failed", "checking", "failed", "failed", "needs_you"])

    def test_accepted_but_never_connected_asks_after_a_day_and_notifies_once(self):
        # B11: the relay accepts the session every hour, but the account never reads connected.
        import datetime as dt
        self.accounts = LOST_ACCOUNT
        self.signed_in()

        def transport(method, path, **named):
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "reconnect"}) if path == "/link/session" else self.accounts
        start = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        clock = [start]
        with mock.patch.object(linkedin_connection, "_now", lambda: clock[0]):
            for hour in range(24):
                clock[0] = start + dt.timedelta(hours=hour)
                linkedin_connection.keep(self.home, transport=transport)
                self.assertEqual((self.step()["status"], self.step()["message"]),
                                 ("needs_you", linkedin_connection.SENT_NOT_CONNECTED), hour)
                self.assertIsNone(linkedin_connection.pending_notice(self.home))
            for later in (24, 25):
                clock[0] = start + dt.timedelta(hours=later)
                linkedin_connection.keep(self.home, transport=transport)
                step = self.step()
                self.assertEqual((step["status"], step["message"]), ("needs_you", linkedin_connection.NOT_CONNECTED_A_DAY))
            self.assertEqual(linkedin_connection.pending_notice(self.home)["message"], linkedin_connection.NOT_CONNECTED_A_DAY)
            self.assertEqual(setup_steps.load_state(self.home)[linkedin_connection.NOTIFIED_KEY],
                             start.replace(microsecond=0).isoformat())   # one notice for this outage
            self.assertEqual(len(self.posts()), 26)   # it kept sending each hour
            self.assertTrue(linkedin_connection.held(self.home))
            self.accounts = OK_ACCOUNT
            linkedin_connection.keep(self.home, transport=transport)
            self.assertEqual(self.step()["status"], "done")
            self.assertNotIn(linkedin_connection.UNREACHED_KEY, setup_steps.load_state(self.home))

    def test_the_hourly_check_runs_while_the_scheduler_is_paused_and_starts_nothing(self):
        # B10: paused (by the user, the license or a key hold), the LinkedIn check still runs once each hour;
        # no job is started.
        import scheduler_loop
        try:
            from test_scheduler_loop import CONFIG, Clock, Spawner, at
        except ImportError:
            from tests.test_scheduler_loop import CONFIG, Clock, Spawner, at
        checks = []
        for name, options in (("user pause", {"is_paused": lambda: True}),
                              ("license pause", {"is_paused": lambda: False, "license_reason": lambda: "trial ended"}),
                              ("key hold", {"is_paused": lambda: False, "credentials_hold": lambda: "locked"})):
            clock, spawner = Clock(at(8, 50)), Spawner()
            del checks[:]
            loop = scheduler_loop.Loop(self.home / "data", clock=clock, spawner=spawner,
                                       config=dict(CONFIG, linkedin_provider="unipile"),
                                       linkedin_keep=lambda: checks.append(clock()), **options)
            for unused in range(3):   # ticks inside one hour: one check
                loop.tick()
            self.assertEqual(len(checks), 1, name)
            clock.now = at(9, 1)
            loop.tick()
            loop.tick()
            clock.now = at(10, 1)
            loop.tick()
            self.assertEqual(len(checks), 3, name)
            self.assertEqual(spawner.calls, [], name)   # nothing else ran
        # Not a Unipile install, or the owner's local setup: no check.
        for config in (CONFIG, dict(CONFIG, linkedin_provider="unipile", linkedin_transport="local-dev")):
            del checks[:]
            loop = scheduler_loop.Loop(self.home / "data", clock=Clock(at(8, 50)), spawner=Spawner(), config=config,
                                       is_paused=lambda: True, linkedin_keep=lambda: checks.append(1))
            loop.tick()
            self.assertEqual(checks, [])

    def test_a_changed_session_is_sent_again_and_an_unchanged_one_is_not(self):
        self.signed_in("first-cookie")
        linkedin_connection.connect(self.home, transport=self.transport)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(linkedin_connection.keep(self.home, transport=self.transport), "UNCHANGED")
        self.assertEqual(len(self.posts()), 1)
        self.signed_in("second-cookie")   # LinkedIn replaced the cookie (owner A3)
        self.assertEqual(linkedin_connection.keep(self.home, transport=self.transport), "RESENT")
        self.assertEqual(self.sent_body["session"], "second-cookie")
        self.assertEqual(linkedin_connection.keep(self.home, transport=self.transport), "UNCHANGED")
        self.assertEqual(len(self.posts()), 2)

    def test_the_round_holds_at_once_when_its_own_account_read_says_disconnected(self):
        account = mock.Mock(valid=False, account_id=MINE, own_profile="https://www.linkedin.com/in/alex")
        port = mock.Mock(account=mock.Mock(return_value=account))
        with mock.patch.object(orchestrator, "linkedin_port_for", return_value=port):
            orchestrator.sync_sender({"linkedin_provider": "unipile"}, self.home)
            self.assertTrue(linkedin_connection.held(self.home))
            account.valid = True
            orchestrator.sync_sender({"linkedin_provider": "unipile"}, self.home)
        self.assertFalse(linkedin_connection.held(self.home))

    def test_only_the_country_is_sent_as_location(self):
        (self.home / "profile.json").write_text(json.dumps({"answers": {
            "current_country": {"value": "United Kingdom"}, "current_city": {"value": "Leeds"}}}))
        self.signed_in()
        linkedin_connection.send(self.home, transport=self.transport)
        self.assertEqual(self.sent_body, {"session": MARKER, "user_agent": AGENT, "country": "GB"})


OLD_FLOW_WORDS = ("Connect again", "Reconnect again", "the Unipile page", "this link is valid", "LinkedIn sign-in page",
                  "sign-in link works", "Get a new link", "Waiting for LinkedIn...",
                  "Connect LinkedIn</button>", "click Connect LinkedIn", "Click Connect LinkedIn",
                  "<strong>Connect LinkedIn</strong>")
USER_FACING = ("setup_steps.py", "linkedin_connection.py", "dashboard.py", "settings_page.py", "overview_page.py",
               "doctor.py", "approvals_page.py", "unipile_port.py", "failures_page.py", "networking_page.py")


def old_flow_hits(files):
    """(file, phrase) for each old hosted-flow button or page a user-facing file still names. Lines that are
    only a comment are skipped."""
    found = []
    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("#") or text.startswith("{#"):
                continue
            found.extend((Path(path).name, phrase) for phrase in OLD_FLOW_WORDS if phrase in text)
    return found


class SetupAndSettingsRound5(ComputerSide):
    """Review round 5: what Setup and Settings show."""

    def client(self):
        try:
            from fastapi.testclient import TestClient
            import dashboard
        except ImportError:
            self.skipTest("dashboard packages are not installed")
        app = dashboard.create_app("test-token-value", 47999, home=self.home, root=self.home / "data",
                                   config={"text_model": {"model": "test/model"}}, browser=mock.Mock(),
                                   relay_url="https://relay.test")
        client = TestClient(app, base_url="http://127.0.0.1:47999", follow_redirects=False)
        client.get("/", params={"t": "test-token-value"})
        import license_fixtures
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        patch = mock.patch.object(unipile_port, "RelayTransport", return_value=self.transport)
        patch.start()
        self.addCleanup(patch.stop)
        return client

    def settings_row(self, client):
        page = client.get("/settings").text
        return page.split('id="connection-linkedin"')[1].split('id="connection-')[0]

    def setup_step(self, client):
        return client.get("/setup/step/7", headers={"hx-request": "true"}).text.split('id="step-7"')[1].split("</li>")[0]

    def test_no_user_facing_text_names_the_old_page_link_or_buttons(self):
        root = Path(setup_steps.ROOT)
        files = [root / name for name in USER_FACING] + sorted((root / "dashboard_assets" / "templates").rglob("*.html"))
        # The must-hit control: the scan finds each phrase in a file that holds it.
        control = self.home / "control.html"
        control.write_text("\n".join("<p>%s</p>" % phrase for phrase in OLD_FLOW_WORDS))
        self.assertEqual(len(old_flow_hits([control])), len(OLD_FLOW_WORDS))
        self.assertTrue(all((root / name).is_file() for name in USER_FACING))
        self.assertGreater(len(files), 30)
        self.assertEqual(old_flow_hits(files), [])

    def test_a_hosted_link_left_in_flight_by_an_older_version_is_simply_not_connected(self):
        # Finding 1: pressed the old [Connect LinkedIn], updated before finishing.
        client = self.client()
        for started, kind in ((setup_steps.now_iso(), "create"), ("2000-01-01T00:00:00Z", "create"),
                              (setup_steps.now_iso(), "reconnect")):
            setup_steps.update_state(lambda value: value.update(linkedin_link_started_at=started, linkedin_link_kind=kind), self.home)
            setup_steps.record(self.home, "linkedin", "checking", "Waiting for LinkedIn... (this link is valid for about an hour)",
                               offer="waiting", kind=kind)
            self.accounts = (403, {"error": "LICENSE_NOT_LINKED"})
            step = setup_steps.linkedin_status(self.home, transport=self.transport)["steps"]["linkedin"]
            self.assertEqual((step["status"], step["message"]), ("needs_you", "LinkedIn is not connected."))
            state = setup_steps.load_state(self.home)
            self.assertNotIn("linkedin_link_started_at", state)
            self.assertNotIn("linkedin_link_kind", state)
            for shown in (self.setup_step(client), self.settings_row(client)):
                self.assertIn("I'm signed in</button>", shown)
                self.assertIn("Open Sidedoor's browser</button>", shown)
                for gone in ("this link", "Unipile page", "Connect again", "Waiting for LinkedIn"):
                    self.assertNotIn(gone, shown)

    def test_no_screen_names_the_vendor_or_explains_where_the_session_goes(self):
        # Owner, 2026-11-12 (S6: "reject. overexposure"): the disclosure sentence is gone in both forms, and
        # nothing replaces it. UI text names the action only.
        import re
        banned = ("Unipile", "LinkedIn session", "your session", "session is sent", "sends your", "sign-in was sent")

        def hits(texts):
            return sorted({word for text in texts for word in banned if word.lower() in text.lower()})
        # The must-hit control: both rejected sentences are found.
        self.assertEqual(hits(["When you click I'm signed in, Sidedoor sends your LinkedIn session to Unipile, the service "
                               "that delivers your invitations and messages."]), ["LinkedIn session", "Unipile", "sends your"])
        self.assertEqual(hits(["Your sign-in was sent, but LinkedIn does not show as connected yet."]), ["sign-in was sent"])
        # Every template, without its template comments.
        root = Path(setup_steps.ROOT) / "dashboard_assets" / "templates"
        templates = [re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S) for path in sorted(root.rglob("*.html"))]
        self.assertGreater(len(templates), 30)
        self.assertEqual(hits(templates), [])
        # Every sentence the LinkedIn connection code can put on the step, the Overview line or a notice.
        lc = linkedin_connection
        sentences = [value for name, value in vars(lc).items() if name.isupper() and isinstance(value, str) and " " in value]
        sentences += [message for unused, message in lc.MESSAGES.values()]
        self.assertGreater(len(sentences), 20)
        self.assertEqual(hits(sentences), [])
        # What Setup and Settings render: connected (the old way), not connected, disconnected, accepted but not listed.
        client = self.client()
        pages = []

        def render():
            pages.extend([self.setup_step(client), self.settings_row(client), json.dumps(self.step())])
        setup_steps.linkedin_status(self.home, transport=self.transport)
        render()
        self.assertIn("Reconnect LinkedIn</button>", pages[1])
        self.accounts = (403, {"error": "LICENSE_NOT_LINKED"})
        setup_steps.linkedin_status(self.home, transport=self.transport)
        render()
        self.assertIn("I'm signed in</button>", pages[3])
        self.signed_in()
        accounts = self.accounts
        linkedin_connection.connect(self.home, transport=lambda method, path, **named: (
            (200, {"connected": True, "kind": "create"}) if path == "/link/session" else accounts))
        render()
        self.accounts = LOST_ACCOUNT
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        setup_steps.linkedin_status(self.home, transport=self.transport)
        render()
        self.assertEqual(hits(pages), [])
        self.assertNotIn("linkedin-disclosure", "".join(pages))

    def test_a_connected_user_whose_account_vanished_is_held_when_they_press_reconnect(self):
        # Round 8 (1): was connected, the account is no longer listed, the relay still has the link (so it
        # would forward a write). Pressing [Reconnect LinkedIn] and being accepted must leave outreach held.
        lc = linkedin_connection
        self.signed_in()
        setup_steps.linkedin_status(self.home, transport=self.transport)
        self.assertEqual(self.step()["status"], "done")
        self.assertIsNotNone(setup_steps.saved_sender(self.home))   # it was connected
        self.assertFalse(lc.held(self.home))
        self.accounts = (200, {"items": [], "cursor": None})

        def accepted(method, path, **named):
            return (200, {"connected": True, "kind": "reconnect"}) if path == "/link/session" else self.accounts
        lc.connect(self.home, transport=accepted)
        self.assertEqual(self.step()["message"], lc.SENT_NOT_CONNECTED)
        self.assertTrue(lc.held(self.home))
        with self.assertRaises(unipile_port.LinkedInDisconnected):
            orchestrator.unipile_ready(setup_steps.with_sender({"linkedin_provider": "unipile"}, self.home), port="PORT")
        setup_steps.linkedin_status(self.home, transport=accepted)   # a later read does not lift it
        self.assertTrue(lc.held(self.home))

    def test_a_first_connect_listed_but_not_ok_is_never_called_a_day_long_outage(self):
        # Round 8 (2): never connected. Accepted, listed, but the account stays "CONNECTING".
        import datetime as dt
        lc = linkedin_connection
        self.never_connected()
        self.signed_in()
        self.accounts = (200, {"items": [{"type": "LINKEDIN", "id": MINE, "name": "Alex", "status": "CONNECTING"}]})

        def accepted(method, path, **named):
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "create"}) if path == "/link/session" else self.accounts
        start = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        clock = [start]
        with mock.patch.object(lc, "_now", lambda: clock[0]):
            lc.connect(self.home, transport=accepted)
            for hour in range(0, 31):   # hourly checks and page reads for more than a day
                clock[0] = start + dt.timedelta(hours=hour)
                lc.keep(self.home, transport=accepted)
                setup_steps.linkedin_status(self.home, transport=accepted)
                step = self.step()
                self.assertEqual((step["status"], step["message"], step["offer"]), ("needs_you", lc.SENT_NOT_CONNECTED, "sent"), hour)
                state = setup_steps.load_state(self.home)
                self.assertNotIn(lc.UNREACHED_KEY, state)
                self.assertIsNone(lc.pending_notice(self.home))
                self.assertFalse(lc.held(self.home))
            self.assertEqual(len(self.posts()), 1)   # only the user's own press sent anything
            # The gate that keeps outreach from a never-connected user: no sending identity is saved, so
            # every outreach step stops at the readiness check.
            self.assertIsNone(setup_steps.saved_sender(self.home))
            config = setup_steps.with_sender({"linkedin_provider": "unipile"}, self.home)
            with self.assertRaises(unipile_port.SenderNotConfigured) as raised:
                orchestrator.unipile_ready(config, port="PORT")
            self.assertTrue(orchestrator.license_hold(raised.exception))
            # A refusal on a later press is kept over it; then it connects.
            self.session_answer = (409, {"error": "ACCOUNT_LINKED_ELSEWHERE"})
            lc.connect(self.home, transport=self.transport_with(self.accounts))
            setup_steps.linkedin_status(self.home, transport=accepted)
            self.assertEqual(self.step()["message"], lc.MESSAGES[lc.ELSEWHERE][1])
            self.accounts = OK_ACCOUNT
            setup_steps.linkedin_status(self.home, transport=accepted)
            self.assertEqual(self.step()["status"], "done")
            self.assertIsNotNone(setup_steps.saved_sender(self.home))

    def transport_with(self, accounts):
        def call(method, path, **named):
            return self.session_answer if path == "/link/session" else accounts
        return call

    def test_a_first_connect_accepted_but_not_listed_is_not_an_outage(self):
        # Review round 7: never connected, so there is no "day without reconnecting" to speak of.
        import datetime as dt
        lc = linkedin_connection
        not_linked = (403, {"error": "LICENSE_NOT_LINKED"})
        self.accounts = not_linked
        self.never_connected()
        self.signed_in()

        def accepted(method, path, **named):
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "create"}) if path == "/link/session" else self.accounts
        start = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        clock = [start]

        def clean():
            state = setup_steps.load_state(self.home)
            self.assertNotIn(lc.UNREACHED_KEY, state)
            self.assertFalse(lc.held(self.home))
            self.assertIsNone(lc.pending_notice(self.home))
        with mock.patch.object(lc, "_now", lambda: clock[0]):
            lc.connect(self.home, transport=accepted)
            self.assertEqual((self.step()["status"], self.step()["message"], self.step()["offer"]),
                             ("needs_you", lc.SENT_NOT_CONNECTED, "sent"))
            clean()
            for hour in range(1, 73):   # three days of hourly checks: it never lists
                clock[0] = start + dt.timedelta(hours=hour)
                lc.keep(self.home, transport=accepted)
                self.assertEqual(self.step()["message"], "LinkedIn is not connected.")
                clean()
            self.assertEqual(len(self.posts()), 1)   # the hourly check sends nothing for a never-connected install
            # The user presses again: accepted, and now listed but not yet OK.
            self.accounts = (200, {"items": [{"type": "LINKEDIN", "id": MINE, "name": "Alex", "status": "CONNECTING"}]})
            lc.connect(self.home, transport=accepted)
            self.assertEqual((self.step()["status"], self.step()["message"]), ("needs_you", lc.SENT_NOT_CONNECTED))
            self.assertIsNone(lc.pending_notice(self.home))
            self.assertNotIn("for a day", self.step()["message"])
            # A stale clock an earlier version of this path left behind does not survive a plain "not connected".
            setup_steps.update_state(lambda value: value.update({lc.UNREACHED_KEY: start.isoformat(), lc.HOLD_KEY: {"since": "x"}}), self.home)
            self.accounts = not_linked
            setup_steps.linkedin_status(self.home, transport=accepted)
            clean()
            # The control: an account that was connected, disconnects and stays so for a day still asks and notifies.
            self.accounts = OK_ACCOUNT
            setup_steps.linkedin_status(self.home, transport=accepted)
            self.assertEqual(self.step()["status"], "done")
            self.accounts = LOST_ACCOUNT
            begin = clock[0]
            for hour in range(0, 25):
                clock[0] = begin + dt.timedelta(hours=hour)
                lc.keep(self.home, transport=accepted)
            self.assertEqual((self.step()["status"], self.step()["message"]), ("needs_you", lc.NOT_CONNECTED_A_DAY))
            self.assertEqual(lc.pending_notice(self.home)["message"], lc.NOT_CONNECTED_A_DAY)
            self.assertTrue(lc.held(self.home))

    def test_a_refused_first_connect_keeps_its_reason_on_the_next_read(self):
        # Finding 3.
        self.accounts = (403, {"error": "LICENSE_NOT_LINKED"})
        self.signed_in()
        for answer in ((409, {"error": "ACCOUNT_LINKED_ELSEWHERE"}), (422, {"error": "SESSION_REFUSED", "upstream_status": 401})):
            self.session_answer = answer
            refused = linkedin_connection.connect(self.home, transport=self.transport)["steps"]["linkedin"]
            self.assertEqual(refused["status"], "needs_you")
            for unused in range(2):
                read = setup_steps.linkedin_status(self.home, transport=self.transport)["steps"]["linkedin"]
                self.assertEqual((read["status"], read["message"]), (refused["status"], refused["message"]))
        # Acting again replaces it.
        linkedin_session.set_reader(lambda home: None)
        again = linkedin_connection.connect(self.home, transport=self.transport)["steps"]["linkedin"]
        self.assertIn("is not signed in to LinkedIn", again["message"])

    def test_a_connected_step_stays_connected_when_a_reconnect_has_to_wait(self):
        # Finding 4: [Reconnect LinkedIn] on a connected row while the browser is busy (or the service is away).
        import overview_page
        client = self.client()
        setup_steps.linkedin_status(self.home, transport=self.transport)

        def busy(home):
            raise linkedin_session.Busy()
        for reader, answer, words in ((busy, None, "browser is busy right now."),
                                      (None, OSError("offline"), "LinkedIn service. Check your internet and try again."),
                                      (None, (429, "slow down"), "could not connect LinkedIn just now.")):
            if reader:
                linkedin_session.set_reader(reader)
            else:
                self.signed_in()
                self.session_answer = answer
            shown = client.post("/settings/linkedin/connect", headers={"origin": "http://127.0.0.1:47999", "hx-request": "true"}).text
            step = self.step()
            self.assertEqual((step["status"], step["message"]), ("done", "LinkedIn connected: Alex."))
            self.assertIn(words, shown)
            self.assertIn("LinkedIn is still connected.", shown)
            self.assertIn("Reconnect LinkedIn</button>", shown)
            self.assertFalse(linkedin_connection.held(self.home))
        # The note goes with the next read.
        setup_steps.linkedin_status(self.home, transport=self.transport)
        self.assertNotIn("notice", self.step())

    def test_the_chip_and_overview_agree_with_a_message_that_asks_the_user_to_act(self):
        # Finding 5.
        import dashboard
        acts = (linkedin_connection.DISCONNECTED_READ, linkedin_connection.SENT_NOT_CONNECTED)
        self.accounts = LOST_ACCOUNT
        self.signed_in()
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        read = setup_steps.linkedin_status(self.home, transport=self.transport)["steps"]["linkedin"]
        self.assertEqual((read["status"], read["message"]), ("needs_you", acts[0]))

        def accepted(method, path, **named):
            return (200, {"connected": True, "kind": "reconnect"}) if path == "/link/session" else self.accounts
        sent = linkedin_connection.connect(self.home, transport=accepted)["steps"]["linkedin"]
        self.assertEqual((sent["status"], sent["message"]), ("needs_you", acts[1]))
        self.assertNotIn(dashboard.STATUS["needs_you"][0], ("Checking", "Waiting"))
        # [Open Sidedoor's browser] while the browser is taken: a full sentence.
        busy = linkedin_connection.browser_opened(self.home, "busy")["steps"]["linkedin"]["message"]
        self.assertEqual(busy, "Sidedoor's browser is busy with a scheduled task. Try again in a few minutes.")
        # Only a state that asks nothing of the user says it is waiting.
        linkedin_session.set_reader(lambda home: (_ for _ in ()).throw(linkedin_session.Busy()))
        linkedin_connection.keep(self.home, transport=self.transport)
        self.assertEqual((self.step()["status"], self.step()["message"]), ("checking", linkedin_connection.RECONNECTING))

    def test_each_state_shows_a_sentence_chip_overview_line_and_buttons_that_agree_and_stay_true(self):
        # Review round 6: walked in order; for each state the sentence, the chip, Overview's line and button,
        # and the row's buttons. A sentence must still be true if the user reads it tomorrow.
        import dashboard
        import overview_page
        lc = linkedin_connection
        client = self.client()
        post = {"origin": "http://127.0.0.1:47999", "hx-request": "true"}
        signin, check = "I'm signed in</button>", "Check again</button>"

        def shown(sentence, chip, overview_text, overview_button, has, lacks=()):
            step = self.step()
            self.assertEqual(step.get("message"), sentence)
            self.assertEqual(dashboard.STATUS[step["status"]][0], chip)
            item = overview_page.linkedin_item(step)
            self.assertEqual((item["text"], (item["fix"] or {}).get("label")), (overview_text, overview_button))
            row = self.settings_row(client)
            for button in has:
                self.assertIn(button, row)
            for button in lacks:
                self.assertNotIn(button, row)

        def read():   # a page read, now or tomorrow
            setup_steps.linkedin_status(self.home, transport=self.transport)

        bare = "LinkedIn is not connected."
        self.never_connected()
        self.accounts = (403, {"error": "LICENSE_NOT_LINKED"})
        read()
        shown(bare, "Needs you", bare, "Sign in to LinkedIn", [signin], [check])
        # 1. Open the browser: true now.
        client.app.state   # the app is the one built by self.client()
        with mock.patch.object(lc, "browser_opened", wraps=lc.browser_opened):
            lc.browser_opened(self.home, "opened")
        shown(lc.OPEN_MESSAGE, "Needs you", lc.OPEN_MESSAGE, "Sign in to LinkedIn", [signin])
        # Quit, come back later: the browser is not open any more, so nothing says it is.
        read()
        shown(bare, "Needs you", bare, "Sign in to LinkedIn", [signin], [check])
        for outcome in ("busy", "unclean", "no_chrome", "failed"):   # nor any other word about the browser just then
            lc.browser_opened(self.home, outcome)
            read()
            shown(bare, "Needs you", bare, "Sign in to LinkedIn", [signin])
        # 2. Press [I'm signed in] while the browser has no sign-in: said now, not kept for tomorrow.
        linkedin_session.set_reader(lambda home: None)
        client.post("/settings/linkedin/connect", headers=post)
        shown(lc.MESSAGES[lc.NOT_SIGNED_IN][1], "Needs you", lc.MESSAGES[lc.NOT_SIGNED_IN][1], "Sign in to LinkedIn", [signin])
        read()
        shown(bare, "Needs you", bare, "Sign in to LinkedIn", [signin])
        # 3. Refused (linked to another license): the reason is kept until the user acts again.
        self.signed_in()
        self.session_answer = (409, {"error": "ACCOUNT_LINKED_ELSEWHERE"})
        client.post("/settings/linkedin/connect", headers=post)
        refused = lc.MESSAGES[lc.ELSEWHERE][1]
        shown(refused, "Needs you", refused, "Sign in to LinkedIn", [signin])
        read()
        shown(refused, "Needs you", refused, "Sign in to LinkedIn", [signin])
        # 4. Press again: accepted, but the account is not listed yet. The press shows its own result, with
        #    [Check again]; the kept reason never hides it. Overview names the same action as the sentence.
        accounts = self.accounts

        def accepted(method, path, **named):
            self.calls.append((method, path))
            return (200, {"connected": True, "kind": "create"}) if path == "/link/session" else accounts
        with mock.patch.object(unipile_port, "RelayTransport", return_value=accepted):
            client.post("/settings/linkedin/connect", headers=post)
        shown(lc.SENT_NOT_CONNECTED, "Needs you", lc.SENT_NOT_CONNECTED, "Check again", [check, signin])
        # [Check again], still not listed: back to the bare, lasting line.
        read()
        shown(bare, "Needs you", bare, "Sign in to LinkedIn", [signin], [check])
        # 5. Refused, then connected: the reason goes.
        self.session_answer = (422, {"error": "SESSION_REFUSED", "upstream_status": 401})
        client.post("/settings/linkedin/connect", headers=post)
        shown(lc.MESSAGES[lc.REFUSED][1], "Needs you", lc.MESSAGES[lc.REFUSED][1], "Sign in to LinkedIn", [signin])
        self.session_answer = (200, {"connected": True, "kind": "create"})
        client.post("/settings/linkedin/connect", headers=post)
        shown("LinkedIn connected: Alex.", "Done", "Connected", None, ["Reconnect LinkedIn</button>"], [signin, check])
        # 6. Disconnected later: a word about the browser just then is not kept there either.
        self.accounts = LOST_ACCOUNT
        read()
        shown(lc.DISCONNECTED_READ, "Needs you", lc.DISCONNECTED_READ, "Sign in to LinkedIn", [signin])
        linkedin_session.set_reader(lambda home: (_ for _ in ()).throw(linkedin_session.Busy()))
        client.post("/settings/linkedin/connect", headers=post)
        shown(lc.MESSAGES[lc.BUSY][1], "Failed", lc.MESSAGES[lc.BUSY][1], "Sign in to LinkedIn", [signin])
        read()
        shown(lc.DISCONNECTED_READ, "Needs you", lc.DISCONNECTED_READ, "Sign in to LinkedIn", [signin])
        lc.browser_opened(self.home, "opened")
        read()
        shown(lc.DISCONNECTED_READ, "Needs you", lc.DISCONNECTED_READ, "Sign in to LinkedIn", [signin])
        # A refusal while disconnected is kept, as when not connected.
        self.signed_in()
        self.session_answer = (502, {"error": "SESSION_ACCOUNT_MISMATCH"})
        client.post("/settings/linkedin/connect", headers=post)
        read()
        shown(lc.MESSAGES[lc.MISMATCH][1], "Needs you", lc.MESSAGES[lc.MISMATCH][1], "Sign in to LinkedIn", [signin])


class NoticeWhilePaused(unittest.TestCase):
    """Round 3: paused for a day with LinkedIn disconnected and unreachable. The hourly check still runs, and
    its notification is really delivered (not only queued), once, with nothing else started."""

    def test_the_notice_is_delivered_once_while_paused_and_nothing_else_runs(self):
        import datetime as dt
        import dashboard
        import notifications
        import scheduler_loop
        try:
            from test_scheduler_loop import CONFIG, Clock, Spawner, at
        except ImportError:
            from tests.test_scheduler_loop import CONFIG, Clock, Spawner, at
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home, root = Path(directory.name) / "home", Path(directory.name) / "data"
        home.mkdir()
        (home / "license.key").write_text("SD-TESTKEY\n")
        setup_steps.save_sender(home, MINE, "https://www.linkedin.com/in/alex")   # it was connected before the outage
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(
            lambda home: linkedin_session.Session(MARKER, AGENT)))
        self.addCleanup(linkedin_connection._sent.clear)
        import paths
        Path(paths.database(root)).parent.mkdir(parents=True)
        database = orchestrator.connect(root)
        self.addCleanup(database.close)
        __import__("applications").initialize(database)
        notifications.initialize(database)
        database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES ('other:1','Another notice','PENDING','2026-10-22T00:00:00+00:00')")
        database.commit()

        def transport(method, path, **named):
            if path == "/link/session":
                raise OSError("offline")
            return LOST_ACCOUNT
        clock, spawner = Clock(at(8, 50)), Spawner()

        def check():
            linkedin_connection.keep(home, transport=transport)
            dashboard.linkedin_notify(home, root, {})
        loop = scheduler_loop.Loop(root, clock=clock, spawner=spawner, is_paused=lambda: True,
                                   config=dict(CONFIG, linkedin_provider="unipile"), linkedin_keep=check)

        def rows():
            return [tuple(row) for row in database.execute(
                "SELECT event_key,state,attempts,json_extract(delivery,'$.provider') FROM notification_outbox ORDER BY event_key")]
        with mock.patch.object(linkedin_connection, "_now", lambda: clock().astimezone(dt.timezone.utc)):
            for hour in range(24):
                loop.tick()
                clock.now += dt.timedelta(hours=1)
            self.assertEqual(rows(), [("other:1", "PENDING", 0, None)])   # not a day yet: nothing queued, nothing sent
            for unused in range(4):   # the day is up, and three more paused hours
                loop.tick()
                clock.now += dt.timedelta(hours=1)
        found = rows()
        self.assertEqual(len(found), 2)
        key, state, attempts, provider = found[0]
        self.assertTrue(key.startswith("linkedin-unreachable:"))
        self.assertEqual((state, attempts, provider), ("DELIVERED", 1, "dashboard"))   # delivered, exactly once
        self.assertEqual(found[1], ("other:1", "PENDING", 0, None))   # no other notice was touched
        self.assertEqual(spawner.calls, [])   # no job ran
        self.assertEqual(setup_steps.load_state(home)["steps"]["linkedin"]["status"], "needs_you")
        self.assertTrue(linkedin_connection.held(home))
        import approvals_page
        self.assertEqual(approvals_page.notice_count(database), 1)   # what the dashboard's notice list shows

    def test_an_undelivered_notice_is_withdrawn_once_linkedin_is_connected_again(self):
        # Round 4: "has not been able to reconnect LinkedIn for a day" is untrue once it has reconnected.
        import dashboard
        import paths
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home, root = Path(directory.name) / "home", Path(directory.name) / "data"
        home.mkdir()
        Path(paths.database(root)).parent.mkdir(parents=True)
        database = orchestrator.connect(root)
        self.addCleanup(database.close)
        __import__("applications").initialize(database)
        __import__("notifications").initialize(database)
        message = linkedin_connection.UNREACHED_A_DAY
        for key, state, delivery in (("linkedin-unreachable:a", "PENDING", None), ("linkedin-unreachable:b", "UNCERTAIN", None),
                                     ("linkedin-unreachable:c", "DELIVERED", json.dumps({"provider": "dashboard"})),
                                     ("other:1", "PENDING", None)):
            database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at,delivery) VALUES (?,?,?,?,?)",
                             (key, message, state, "2026-10-22T00:00:00+00:00", delivery))
        database.commit()
        setup_steps.update_state(lambda value: value.update(
            {linkedin_connection.NOTICE_KEY: {"key": "linkedin-unreachable:d", "message": message}}), home)

        def rows():
            return {row[0]: row[1] for row in database.execute("SELECT event_key,state FROM notification_outbox")}
        # Still disconnected: the queued notice is handed over and the undelivered ones are delivered.
        setup_steps.record(home, "linkedin", "needs_you", message, offer="signin")
        before = dict(rows())
        # Connected again before anything was delivered: nothing undelivered goes out, by either path.
        setup_steps.record(home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")
        dashboard.linkedin_notify(home, root, {})
        self.assertEqual(rows(), {"linkedin-unreachable:c": "DELIVERED", "other:1": "PENDING"})
        self.assertIsNone(linkedin_connection.pending_notice(home))
        self.assertEqual(before["linkedin-unreachable:c"], "DELIVERED")   # a delivered one is left alone
        # The round's own hand-over does the same.
        setup_steps.update_state(lambda value: value.update(
            {linkedin_connection.NOTICE_KEY: {"key": "linkedin-unreachable:e", "message": message}}), home)
        database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES ('linkedin-unreachable:f',?,'PENDING','x')", (message,))
        database.commit()
        self.assertFalse(orchestrator.deliver_linkedin_notice(database, home))
        self.assertEqual(rows(), {"linkedin-unreachable:c": "DELIVERED", "other:1": "PENDING"})
        self.assertIsNone(linkedin_connection.pending_notice(home))


try:
    from test_unipile_e2e import InvitationThroughTheRelay, ROOT as E2E_ROOT
except ImportError:
    from tests.test_unipile_e2e import InvitationThroughTheRelay, ROOT as E2E_ROOT


@needs_relay
class HeldInvitation(InvitationThroughTheRelay):
    """A9 (owner): a pass already past the readiness check cannot send on a disconnected account, and the
    held invitation goes out exactly once after the reconnect. Reuses the fixture only."""
    test_one_invitation_and_one_reply_through_the_relay = None
    test_without_a_saved_sender_the_passes_hold_with_no_paid_call = None

    def invitations_sent(self):
        return len([request for request in self.upstream.requests
                    if (request["method"], request["path"]) == ("POST", "/api/v1/users/invite")])

    def test_a_disconnect_after_the_readiness_check_sends_nothing_then_once(self):
        setup_steps.linkedin_status(self.home, self.base)
        config = self.job_config()
        self.assertEqual(orchestrator.draft_promotion_cycle(self.database, config)["queued"], 1)
        original = unipile_port.UnipilePort.send_invitation

        def disconnect_then_send(port, provider_id, note):
            linkedin_connection.hold(self.home)   # noticed between the readiness check and the write
            return original(port, provider_id, note)
        with mock.patch.object(unipile_port.UnipilePort, "send_invitation", disconnect_then_send):
            orchestrator.new_contact_execution_cycle(E2E_ROOT, self.database, config)
        row, action = self.invite()
        result = json.loads(row["result"])
        self.assertEqual((row["state"], result.get("attempts")), ("INVITE_RETRY_WAIT", []))   # held: no try spent
        self.assertEqual(result["relay_refusals"][0]["hold"], "LINKEDIN_DISCONNECTED")
        self.assertEqual(self.invitations_sent(), 0)
        for unused in range(2):   # still disconnected: every pass waits
            with self.assertRaises(unipile_port.LinkedInDisconnected):
                orchestrator.new_contact_execution_cycle(E2E_ROOT, self.database, config)
        self.assertEqual(self.invitations_sent(), 0)
        self.assertEqual(self.invite()[0]["state"], "INVITE_RETRY_WAIT")   # not failed, not dropped
        linkedin_connection.release(self.home)   # reconnected
        orchestrator.new_contact_execution_cycle(E2E_ROOT, self.database, config)
        self.assertEqual(self.invitations_sent(), 1)
        self.set_response("GET /users/e2e-invitee", (200, {"provider_id": "INVITEE1", "network_distance": "SECOND_DEGREE",
                                                           "invitation": {"type": "SENT", "status": "PENDING"},
                                                           "public_identifier": "e2e-invitee"}))
        for unused in range(2):
            orchestrator.new_contact_execution_cycle(E2E_ROOT, self.database, config)
        self.assertEqual(self.invitations_sent(), 1)   # exactly once, never twice
        self.assertEqual(self.invite()[0]["state"], "CONFIRMED_SENT")


class ThroughTheAppDefaults(unittest.TestCase):
    """The app's own entry point with its defaults (home=None, SIDEDOOR_HOME set), as the installed app runs."""

    def test_setup_and_settings_connect_with_one_sign_in(self):
        try:
            from fastapi.testclient import TestClient
            import dashboard
            import license_fixtures
        except ImportError:
            self.skipTest("dashboard packages are not installed")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name) / "home"
        home.mkdir()
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        license_fixtures.install(home, valid_until="2999-01-01T00:00:00Z")
        self.addCleanup(linkedin_session.set_reader,
                        linkedin_session.set_reader(lambda home: linkedin_session.Session(MARKER, AGENT)))
        self.addCleanup(linkedin_connection._sent.clear)
        accounts, sent = [(403, {"error": "LICENSE_NOT_LINKED"})], []

        def transport(method, path, **named):
            if path == "/link/session":
                sent.append(named["body"])
                accounts[0] = OK_ACCOUNT
                return 200, {"connected": True, "kind": "create"}
            return (200, {"premium": False}) if path == "/users/me" else accounts[0]
        browser = mock.Mock(is_open=mock.Mock(return_value=False), open=mock.Mock(return_value="opened"))
        origin = {"origin": "http://127.0.0.1:47999", "hx-request": "true"}
        with mock.patch.dict("os.environ", {"SIDEDOOR_HOME": str(home)}), \
                mock.patch.object(unipile_port, "RelayTransport", return_value=transport):
            app = dashboard.create_app("test-token-value", 47999, root=Path(directory.name) / "data",
                                       config={"text_model": {"model": "test/model"}}, browser=browser)
            client = TestClient(app, base_url="http://127.0.0.1:47999", follow_redirects=False)
            client.get("/", params={"t": "test-token-value"})
            setup = client.get("/setup?open=7").text
            self.assertEqual(setup.count("<h2>Connect LinkedIn</h2>"), 1)
            done = client.post("/setup/linkedin/connect", headers=origin).text
            settings = client.get("/settings").text
        self.assertEqual(sent, [{"session": MARKER, "user_agent": AGENT}])
        self.assertIn("LinkedIn connected: Alex.", done)
        self.assertEqual(settings.count("<h3>LinkedIn</h3>"), 1)
        self.assertIn("Your LinkedIn account: not Premium", settings)
        self.assertNotIn("Sign in there to LinkedIn", settings)
        self.assertEqual(setup_steps.load_state(home)[linkedin_connection.MODE_KEY], "session")
        self.assertEqual(files_holding(directory.name, MARKER), [])
        self.assertNotIn(MARKER, setup + done + settings)


if __name__ == "__main__":
    unittest.main()
