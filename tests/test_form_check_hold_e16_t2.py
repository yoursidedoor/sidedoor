"""E16.T2 phase 1, piece 1 (owner, 2026-11-12, Y1 to Y4): the hold of an ATTENDED submit session.
The AI calls the guard's hold tool, the call is held until the user's Done or the time limit, and the time
held moves the session's deadline and the arm's deadline by exactly that much. Never after arm.
Offline: the browser is the fake @playwright/mcp; no Chrome, no AI, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_outcomes  # noqa: E402
import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import desktop_adapter  # noqa: E402
import engines  # noqa: E402
import form_check_hold  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402
from browser_fixtures import Client, fake_runtime  # noqa: E402
from manual_application_browser import request_at  # noqa: E402
from test_browser_driver import submit_fixture  # noqa: E402
from test_stop_at_once_e16_t1 import CHECK, step  # noqa: E402

WAIT = browser_guard.HUMAN_CHECK_TOOL
START = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc)


class Clock:
    """A clock the hold's own sleep moves; `at` runs a callback once that many seconds have passed."""

    def __init__(self, start=None):
        self.now, self.start, self.due = start or dt.datetime.now(dt.timezone.utc), None, []
        self.start = self.now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)
        for item in list(self.due):
            if (self.now - self.start).total_seconds() >= item[0]:
                self.due.remove(item)
                item[1]()

    def at(self, seconds, action):
        self.due.append((seconds, action))


class Arm:
    def __init__(self, attempted=False):
        self.attempted = attempted


class TheHold(unittest.TestCase):
    def setUp(self):
        self.fixture, self.request_id = submit_fixture(self)
        self.database = self.fixture.database
        self.now = applications.stamp()
        self.request = desktop_adapter.claim(self.database, self.fixture.root, "hold-fixture", self.now,
                                             request_id=self.request_id)
        self.directory = Path(tempfile.mkdtemp(prefix="hold-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        config = form_check_hold.prepare({form_check_hold.ATTENDED_KEY: True}, self.directory, self.fixture.root,
                                         self.request_id, "submit")
        self.context = config[form_check_hold.CONFIG_KEY]
        self.clock = Clock()

    def hold(self, arm=None):
        return form_check_hold.Hold(self.context, arm or Arm(), sleep=self.clock.sleep, clock=self.clock)

    def lease(self):
        return self.database.execute("SELECT lease_until FROM desktop_requests WHERE request_id=?",
                                     (self.request_id,)).fetchone()[0]

    def events(self):
        return [row[0] for row in self.database.execute("SELECT event FROM desktop_events WHERE request_id=?",
                                                        (self.request_id,))]

    def test_done_ends_the_hold_and_only_the_time_held_is_counted(self):
        lease = self.lease()
        seen = {}

        def done():
            seen["open"] = form_check_hold.open_now(self.directory)
            form_check_hold.finish(self.directory)

        self.clock.at(40, done)
        self.assertEqual(self.hold().call(), form_check_hold.DONE_TEXT)
        self.assertTrue(seen["open"])                                      # the view can tell a hold is open
        self.assertFalse(form_check_hold.open_now(self.directory))
        held = form_check_hold.held_in(form_check_hold.hold_file(self.directory))
        self.assertTrue(40 <= held <= 41, held)
        self.assertIn("done_at", form_check_hold.read(form_check_hold.hold_file(self.directory)))   # the user's Done
        self.assertFalse((self.directory / form_check_hold.DONE_FILE).exists())
        # The claim lives to its own end plus the longest pause: once, the same end at a second hold.
        self.assertEqual(self.lease(), desktop_adapter.later(lease, form_check_hold.LIMIT_SECONDS))
        self.assertIn("HUMAN_CHECK_WAIT", self.events())
        self.assertNotIn(form_check_hold.TIMED_OUT_EVENT, self.events())
        self.clock.at((self.clock.now - self.clock.start).total_seconds() + 10,
                      lambda: form_check_hold.finish(self.directory))
        self.assertEqual(self.hold().call(), form_check_hold.DONE_TEXT)
        self.assertEqual(self.lease(), desktop_adapter.later(lease, form_check_hold.LIMIT_SECONDS))
        self.assertTrue(50 <= form_check_hold.held_in(form_check_hold.hold_file(self.directory)) <= 52)

    def test_the_time_limit_is_nine_minutes_for_the_whole_session_and_is_recorded(self):
        self.assertEqual(form_check_hold.LIMIT_SECONDS, 9 * 60)
        self.clock.at(100, lambda: form_check_hold.finish(self.directory))
        self.assertEqual(self.hold().call(), form_check_hold.DONE_TEXT)
        self.assertEqual(self.hold().call(), form_check_hold.EXPIRED_TEXT)   # the rest of the nine minutes
        total = (self.clock.now - self.clock.start).total_seconds()
        self.assertTrue(540 <= total <= 542, total)
        self.assertEqual(form_check_hold.held_in(form_check_hold.hold_file(self.directory)), 540)
        self.assertIn(form_check_hold.TIMED_OUT_EVENT, self.events())
        job_key = self.database.execute("SELECT job_key FROM desktop_requests WHERE request_id=?",
                                        (self.request_id,)).fetchone()[0]
        self.assertTrue(form_check_hold.timed_out(self.database, job_key))
        before = self.clock.now
        self.assertEqual(self.hold().call(), form_check_hold.EXPIRED_TEXT)   # nothing left: answered at once
        self.assertEqual(self.clock.now, before)
        self.assertIn("CAPTCHA_REQUIRED", form_check_hold.EXPIRED_TEXT)

    def act(self, at):
        """The attached view passed a press of the user's on to the page at clock time `at`."""
        (self.directory / form_check_hold.ACT_FILE).write_text(json.dumps({"at": at.isoformat()}))

    def test_the_user_acting_and_then_stopping_ends_the_hold_without_done(self):
        """Owner, 2026-11-13: "If the user forgets to click Done, the AI carries on with its job"."""
        self.act(self.clock.now - dt.timedelta(seconds=30))                   # before this hold: not this hold's
        self.clock.at(20, lambda: self.act(self.clock.now))                   # the tick
        self.clock.at(22, lambda: self.act(self.clock.now))                   # still at it: the settle starts again
        self.assertEqual(self.hold().call(), form_check_hold.ACTED_TEXT)
        waited = (self.clock.now - self.clock.start).total_seconds()
        self.assertTrue(22 + form_check_hold.SETTLE_SECONDS <= waited <= 22 + form_check_hold.SETTLE_SECONDS + 1, waited)
        record = form_check_hold.read(form_check_hold.hold_file(self.directory))
        self.assertEqual((record["acted_returns"], record["began"]), (1, None))
        self.assertIn("done_at", record)
        self.assertNotIn(form_check_hold.TIMED_OUT_EVENT, self.events())
        for words in ("USER_ACTED", "has not said they are done", "call wait_for_human_check again", "Never touch the check"):
            self.assertIn(words, form_check_hold.ACTED_TEXT)

    def test_what_the_user_did_after_the_ais_last_read_counts_even_before_the_hold_opened(self):
        """The real run of 2026-11-13, in its order: 13:33:59 fill, 13:34:01 snapshot, 13:34:03 click, 13:34:05 the
        user's tick, 13:34:06 the hold opens. Nothing resumed for 40 s. Now it returns after the settle."""
        hold = self.hold()
        hold.note_call("browser_fill_form")
        hold.note_call("browser_snapshot")                                     # the AI's last look at the page
        self.clock.sleep(2)
        hold.note_call("browser_click")
        self.clock.sleep(2)
        self.act(self.clock.now)                                               # the tick, before the hold
        self.clock.sleep(1)
        opened = self.clock.now
        self.assertEqual(hold.call(), form_check_hold.ACTED_TEXT)
        waited = (self.clock.now - opened).total_seconds()
        self.assertTrue(waited <= form_check_hold.SETTLE_SECONDS, waited)      # 4 s after the tick, not the 9 minutes
        self.assertIn("since you last read it", form_check_hold.ACTED_TEXT)
        # The AI reads again and holds again with nothing new from the user: it waits (here, to Done).
        hold.note_call("browser_snapshot")
        again = (self.clock.now - self.clock.start).total_seconds()
        self.clock.at(again + 30, lambda: form_check_hold.finish(self.directory))
        self.assertEqual(hold.call(), form_check_hold.DONE_TEXT)
        self.assertTrue((self.clock.now - self.clock.start).total_seconds() >= again + 30)

    def test_what_the_ai_has_already_seen_does_not_end_a_hold(self):
        hold = self.hold()
        self.act(self.clock.now)                                               # the user ticked first
        self.clock.sleep(5)
        hold.note_call("browser_snapshot")                                     # the AI joined and read the page after it
        self.clock.at((self.clock.now - self.clock.start).total_seconds() + 20, lambda: form_check_hold.finish(self.directory))
        self.assertEqual(hold.call(), form_check_hold.DONE_TEXT)
        self.assertIn("Read the page first", form_check_hold.Hold.listing()["description"])

    def test_with_no_hold_and_after_a_hold_the_user_did_not_end_input_closes_only_at_the_arm(self):
        """Review, 2026-11-13: a tick can open an image challenge the user is still solving while the AI reads
        and fills other fields. Only the arm (made after the AI read the check as done) closes the user's input."""
        hold = self.hold()
        self.act(self.clock.now)                                               # the tick; a challenge opens
        self.clock.sleep(1)
        hold.note_call("browser_snapshot")
        hold.note_call("browser_fill_form")                                    # the AI goes on with another field
        self.assertFalse(form_check_hold.resumed(self.directory))
        self.assertTrue(form_check_hold.input_open(self.directory))            # the user's challenge clicks still pass
        hold.note_call("arm")
        self.assertTrue(form_check_hold.resumed(self.directory))
        self.assertFalse(form_check_hold.input_open(self.directory))
        self.assertEqual(form_check_hold.held_in(form_check_hold.hold_file(self.directory)), 0)   # no time was held

    def test_the_arm_closes_input_even_when_the_user_never_clicked(self):
        """Review, 2026-11-13: the check passed by itself, no hold, no click; input stayed open through Submit."""
        hold = self.hold()
        hold.note_call("browser_snapshot")
        hold.note_call("browser_fill_form")
        self.assertTrue(form_check_hold.input_open(self.directory))
        self.assertFalse((self.directory / form_check_hold.ACT_FILE).exists())
        hold.note_call("arm")
        self.assertTrue(form_check_hold.resumed(self.directory))               # the view passes nothing and closes
        self.assertFalse(form_check_hold.input_open(self.directory))

    def test_after_a_hold_that_returned_on_the_users_action_input_also_closes_only_at_the_arm(self):
        hold = self.hold()
        hold.note_call("browser_snapshot")
        self.clock.at(2, lambda: self.act(self.clock.now))
        self.assertEqual(hold.call(), form_check_hold.ACTED_TEXT)
        hold.note_call("browser_snapshot")
        hold.note_call("browser_fill_form")
        self.assertFalse(form_check_hold.resumed(self.directory))              # the challenge may still be open
        hold.note_call("arm")
        self.assertTrue(form_check_hold.resumed(self.directory))

    def test_only_a_few_returns_on_action_then_only_done_or_the_limit(self):
        """Each such return is one more turn of the user's AI: 6 at most in a session."""
        self.assertEqual((form_check_hold.ACTED_RETURNS, form_check_hold.SETTLE_SECONDS), (6, 4))
        for index in range(form_check_hold.ACTED_RETURNS):
            started = (self.clock.now - self.clock.start).total_seconds()
            self.clock.at(started + 1, lambda: self.act(self.clock.now))
            self.assertEqual(self.hold().call(), form_check_hold.ACTED_TEXT, index)
        started = (self.clock.now - self.clock.start).total_seconds()
        self.clock.at(started + 1, lambda: self.act(self.clock.now))          # a seventh action: no return
        self.clock.at(started + 60, lambda: form_check_hold.finish(self.directory))
        self.assertEqual(self.hold().call(), form_check_hold.DONE_TEXT)
        self.assertTrue((self.clock.now - self.clock.start).total_seconds() >= started + 60)

    def test_the_ai_has_resumed_at_its_first_call_that_is_not_a_read_and_input_closes_then(self):
        hold = self.hold()
        self.assertTrue(form_check_hold.input_open(self.directory))            # before any hold: the user may go first
        hold.note_call("browser_fill_form")                                    # the AI filling before any hold: not "resumed"
        self.assertFalse(form_check_hold.resumed(self.directory))
        seen = {}
        self.clock.at(5, lambda: seen.update(open=form_check_hold.input_open(self.directory)) or form_check_hold.finish(self.directory))
        self.assertEqual(hold.call(), form_check_hold.DONE_TEXT)
        self.assertTrue(seen["open"])                                          # while the hold is open
        for name in sorted(form_check_hold.READ_TOOLS):                        # the AI reading the page again
            hold.note_call(name)
        self.assertFalse(form_check_hold.resumed(self.directory))
        self.assertTrue(form_check_hold.input_open(self.directory))
        hold.note_call("browser_fill_form")                                    # it carries on
        self.assertTrue(form_check_hold.resumed(self.directory))
        self.assertFalse(form_check_hold.input_open(self.directory))
        self.clock.at((self.clock.now - self.clock.start).total_seconds() + 3,
                      lambda: seen.update(again=form_check_hold.input_open(self.directory)) or form_check_hold.finish(self.directory))
        self.assertEqual(hold.call(), form_check_hold.DONE_TEXT)               # the pass expired: a hold again
        self.assertTrue(seen["again"])
        self.assertFalse(form_check_hold.resumed(self.directory))

    def test_a_hold_never_begins_once_the_arm_was_called_or_the_request_is_armed(self):
        with self.assertRaises(form_check_hold.Refused):                       # read in the guard: ArmBinding.attempted
            self.hold(Arm(attempted=True)).call()
        self.assertFalse(form_check_hold.hold_file(self.directory).exists())
        self.assertNotIn("HUMAN_CHECK_WAIT", self.events())
        desktop_adapter.arm(self.database, self.fixture.root,
                            dict(self.request, observed_form_revision="fixture-form-v1"), self.now)
        lease = self.lease()
        with self.assertRaises(form_check_hold.Refused):                       # read in the database: armed_at
            self.hold(Arm(attempted=False)).call()
        self.assertEqual(self.lease(), lease)
        self.assertFalse(form_check_hold.open_now(self.directory))
        self.assertEqual(self.clock.now, self.clock.start)                     # nothing was held


class OnlyAnAttendedSubmit(unittest.TestCase):
    def test_prepare_gives_the_hold_to_an_attended_submit_and_to_nothing_else(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        key = form_check_hold.ATTENDED_KEY
        for config, operation in (({}, "submit"), ({key: False}, "submit"), ({key: True}, "inspect"),
                                  ({key: True}, "investigate"), ({key: True}, "reconcile")):
            prepared = form_check_hold.prepare(config, folder, folder, "r1", operation)
            self.assertNotIn(form_check_hold.CONFIG_KEY, prepared, (config, operation))
            self.assertFalse(form_check_hold.attended(prepared))
        prepared = form_check_hold.prepare({key: True}, folder, folder, "r1", "submit")
        self.assertEqual(json.loads(Path(prepared[form_check_hold.CONFIG_KEY]).read_text()),
                         {"directory": str(folder), "root": str(folder), "request_id": "r1"})
        self.assertTrue(form_check_hold.attended(prepared))

    def proxy(self, operation, folder, **given):
        (folder / "snapshot.yaml").write_text('- button "Submit application" [ref=e1]\n', encoding="utf-8")
        (folder / "calls.jsonl").write_text("")
        to_proxy, from_client = os.pipe()
        to_client, from_proxy = os.pipe()
        proxy = browser_guard.Proxy(operation, fake_runtime(folder / "snapshot.yaml", folder / "calls.jsonl"),
                                    audit=folder / "guard.jsonl", stdin=os.fdopen(to_proxy, encoding="utf-8"),
                                    stdout=os.fdopen(from_proxy, "w", encoding="utf-8"), **given)
        thread = threading.Thread(target=proxy.run, daemon=True)
        thread.start()
        client = Client(streams=(os.fdopen(from_client, "w", encoding="utf-8"), os.fdopen(to_client, encoding="utf-8")))

        def close():
            client.close()
            thread.join(10)
        self.addCleanup(close)
        return client

    def folder(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    def context(self, folder):
        return form_check_hold.prepare({form_check_hold.ATTENDED_KEY: True}, folder, folder, "r1", "submit")[
            form_check_hold.CONFIG_KEY]

    def test_the_guard_offers_the_tool_only_with_the_context_and_the_arm_claim_of_a_submit(self):
        for operation, given in (("submit", {"arm_binding": Arm()}),            # no context: not attended
                                 ("inspect", {"human_check": True, "arm_binding": Arm()}),
                                 ("submit", {"human_check": True})):            # no arm claim
            folder = self.folder()
            if given.get("human_check"):
                given = dict(given, human_check=self.context(folder))
            client = self.proxy(operation, folder, **given)
            names = {tool["name"] for tool in client.request("tools/list")["tools"]}
            self.assertNotIn(WAIT, names, (operation, sorted(given)))
            self.assertEqual(client.call(WAIT), (True, "Tool wait_for_human_check is not available in this session."))

    def test_an_attended_submit_holds_one_call_until_done_and_refuses_it_after_the_arm(self):
        folder = self.folder()
        arm = Arm()
        with mock.patch.object(form_check_hold.Hold, "_extend", return_value=True), \
                mock.patch.object(form_check_hold.Hold, "_record"):
            client = self.proxy("submit", folder, human_check=self.context(folder), arm_binding=arm)
            names = {tool["name"] for tool in client.request("tools/list")["tools"]}
            self.assertIn(WAIT, names)
            client.send({"jsonrpc": "2.0", "id": 900, "method": "tools/call", "params": {"name": WAIT, "arguments": {}}})
            deadline = time.monotonic() + 10
            while not form_check_hold.open_now(folder) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(form_check_hold.open_now(folder))
            self.assertFalse(client.call("browser_snapshot")[0])               # the relay keeps running meanwhile
            self.assertFalse([message for message in client.stray if message.get("id") == 900])
            form_check_hold.finish(folder)                                     # the user's Done
            deadline = time.monotonic() + 10
            while form_check_hold.open_now(folder) and time.monotonic() < deadline:
                time.sleep(0.05)
            client.call("browser_snapshot")
            answers = [message["result"] for message in client.stray if message.get("id") == 900]
            self.assertEqual([(answer["isError"], answer["content"][0]["text"]) for answer in answers],
                             [(False, form_check_hold.DONE_TEXT)])
            # The guard tells the hold of the engine's other calls: a read is not resuming, a click is.
            self.assertFalse(form_check_hold.resumed(folder))
            client.call("browser_click", {"element": "Submit application", "target": "e1"})
            self.assertTrue(form_check_hold.resumed(folder))
            arm.attempted = True                                               # the one arm was called
            self.assertEqual(client.call(WAIT), (True, form_check_hold.REFUSED_TEXT))
            self.assertFalse(form_check_hold.open_now(folder))

    def decisions(self, folder):
        return [json.loads(line)["decision"] for line in (folder / "guard.jsonl").read_text().splitlines()]

    def test_the_attended_guard_opens_the_applications_address_itself_before_the_engines_first_step(self):
        """Owner, 2026-11-13: "first there was a blank page". Only an attended submit; an http(s) address only.
        (The fake browser does not list the guard's own calls; the guard's record of the call is what is read.)"""
        url = "https://jobs.example.test/apply/1001"
        folder = self.folder()
        client = self.proxy("submit", folder, human_check=self.context(folder), arm_binding=Arm(), start_url=url)
        client.request("tools/list")                                           # the engine has made no page call
        deadline = time.monotonic() + 10
        while "START_PAGE_OPENED" not in self.decisions(folder) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("START_PAGE_OPENED", self.decisions(folder))
        self.assertEqual((folder / "calls.jsonl").read_text(), "")             # nothing of the engine's ran
        for operation, given in (("submit", {"arm_binding": Arm(), "start_url": url}),             # not attended
                                 ("inspect", {"start_url": url}),
                                 ("submit", {"human_check": True, "arm_binding": Arm(), "start_url": "file:///etc/passwd"}),
                                 ("submit", {"human_check": True, "arm_binding": Arm(), "start_url": "javascript:alert(1)"})):
            folder = self.folder()
            if given.get("human_check"):
                given = dict(given, human_check=self.context(folder))
            client = self.proxy(operation, folder, **given)
            client.request("tools/list")
            client.call("browser_snapshot")
            self.assertFalse([item for item in self.decisions(folder) if item.startswith("START_PAGE")], (operation, sorted(given)))

    def test_the_trace_rule_lets_the_held_call_stand_for_an_attended_session_only(self):
        (finished,) = [event for event in engines.CODEX.trace_reader().feed(
            json.dumps(step("c1", WAIT, form_check_hold.DONE_TEXT)) + "\n") if event.kind == "tool"]
        self.assertEqual(finished.phase, "completed")
        self.assertFalse(application_browser.tool_event(finished, True, live=True, held=True))
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            application_browser.tool_event(finished, True, live=True)


class EveryEngineAllowsTheLongCall(unittest.TestCase):
    """The hold answers after up to 9 minutes; each engine's tool-call limit for the session is 630 s, and
    the tool is in the session's list. Without the hold: the ordinary limit, and no tool."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "schema.json").write_text(json.dumps(application_browser.schema()))
        self.assertGreater(browser_runtime.HUMAN_CHECK_TOOL_TIMEOUT_SECONDS, form_check_hold.LIMIT_SECONDS)

    def config(self, attended, **extra):
        base = dict({"_browser_operation": "submit", "_application_operation": "submit",
                     "_application_guard_claim": str(self.folder / "claim.json"),
                     "browser_driver": {"isolated": True, "headless": True, "runtime_dir": str(self.folder / "runtime")}},
                    **extra)
        if attended:
            base[form_check_hold.ATTENDED_KEY] = True
        return form_check_hold.prepare(base, self.folder, self.folder, "r1", "submit")

    def test_codex(self):
        import tomllib
        for attended, seconds in ((True, 630), (False, browser_runtime.TOOL_TIMEOUT_SECONDS)):
            config = self.config(attended)
            options = browser_runtime.codex_options(config, self.folder, "submit", config["_application_guard_claim"],
                                                    config.get(form_check_hold.CONFIG_KEY))
            server = tomllib.loads("\n".join(options))["mcp_servers"]["browser"]
            self.assertEqual(server["tool_timeout_sec"], seconds)
            self.assertEqual(WAIT in server["tools"], attended)
            self.assertEqual("--human-check" in server["args"], attended)

    def test_claude(self):
        for attended, milliseconds in ((True, "630000"), (False, "110000")):
            config = self.config(attended, claude=str(self.folder / "claude"), engine="claude")
            command = engines.CLAUDE.browser_command(config, self.folder, application=True, main_model=True)
            self.assertEqual(engines.CLAUDE.environment(command, {})["MCP_TOOL_TIMEOUT"], milliseconds)
            self.assertEqual("mcp__browser__" + WAIT in engines.CLAUDE.allowed_of(command), attended)

    def test_antigravity(self):
        seen = []
        real = engines.AntigravityEngine._built

        def built(engine, *args, **kwargs):
            seen.append(kwargs)
            return real(engine, *args, **kwargs)

        for attended, seconds in ((True, 630), (False, engines.ANTIGRAVITY.TOOL_TIMEOUT_SECONDS)):
            config = self.config(attended, antigravity=str(self.folder / "agy"), engine="antigravity")
            with mock.patch.object(engines.AntigravityEngine, "_built", built):
                engines.ANTIGRAVITY.browser_command(config, self.folder, application=True, main_model=True)
            self.assertEqual(seen[-1]["tool_timeout"], seconds)
            tools = seen[-1]["servers"][browser_runtime.SERVER][2]
            self.assertEqual(WAIT in tools, attended)


class TheDeadlinesMoveByTheTimeHeld(unittest.TestCase):
    def claim(self, folder, seconds_left, held=None, began=None):
        hold = folder / application_browser.HOLD_FILE
        if held is not None:
            hold.write_text(json.dumps({"held": held, "began": began}))
        path = folder / "claim.json"
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds_left)
        path.write_text(json.dumps({"root": str(folder), "envelope": {"request_id": "r1", "binding": "b"},
                                    "deadline": deadline.isoformat(), "staged": {},
                                    "hold": str(hold) if held is not None else None}))
        return application_browser.ArmBinding(path)

    def test_the_arm_is_judged_against_its_deadline_plus_exactly_the_time_held(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        arguments = {"observed_form_revision": "fixture-form-v1"}
        armed = {"may_submit_once": True}
        with mock.patch.object(application_browser.desktop_adapter, "arm", return_value=armed), \
                mock.patch.object(application_browser.sqlite3, "connect"):
            # 60 s past its own deadline:
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                self.claim(folder, -60).arm(arguments)                         # no hold: as ever
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                self.claim(folder, -60, held=50).arm(arguments)                # held less than it is late
            self.assertEqual(self.claim(folder, -60, held=70).arm(arguments), armed)   # held more: in time
            # Nothing but the time held moves it: a hold file of another kind, or an unreadable one, adds nothing.
            binding = self.claim(folder, -60, held=70)
            (folder / application_browser.HOLD_FILE).write_text(json.dumps({"until": "2999-01-01T00:00:00+00:00"}))
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                binding.arm(arguments)
            # Never more than the nine minutes.
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                self.claim(folder, -(form_check_hold.LIMIT_SECONDS + 30), held=5000).arm(arguments)

    @unittest.skipIf(os.name == "nt", "POSIX-only, as the other sessions that run a real worker process")
    def test_the_session_deadline_moves_by_the_time_held_and_no_further(self):
        worker = [sys.executable, "-c", "import time; time.sleep(60)"]

        def run(record):
            started = time.monotonic()
            with tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(application_browser.browser_runtime, "require"), \
                    mock.patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                    mock.patch.object(application_browser, "command", return_value=worker), \
                    mock.patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                    self.assertRaisesRegex(TimeoutError, "APPLICATION_BROWSER_TIMEOUT"):
                if record is not None:
                    (Path(root) / application_browser.HOLD_FILE).write_text(json.dumps(record))
                application_browser.execute({}, "prompt", Path(root), 1)
            return time.monotonic() - started

        self.assertLess(run(None), 2.5)
        took = run({"held": 2.0, "began": None})                              # a finished hold of 2 s: 1 + 2
        self.assertTrue(2.8 < took < 4.5, took)
        began = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        with mock.patch.object(form_check_hold, "LIMIT_SECONDS", 3):
            took = run({"held": 0.0, "began": began})                        # an open hold: it runs to its limit
        self.assertTrue(3.5 < took < 5.5, took)


class TheAttendedSession(unittest.TestCase):
    """Real `consume`: what an attended submit session is started with, and what an ordinary one is."""

    def run_session(self, attended):
        fixture, request_id = submit_fixture(self)
        seen = {}

        def scripted(config, prompt, directory, timeout):
            seen.update(config=dict(config), prompt=prompt, directory=directory,
                        claim=json.loads(Path(config["_application_guard_claim"]).read_text()))
            request = request_at(directory)
            (directory / "output.json").write_text(json.dumps({
                "request_id": request["request_id"], "binding": request["binding"], "evidence": [],
                "result_json": json.dumps(dict(CHECK, acknowledged=False))}))
            (directory / "trace.jsonl").write_text("")

        config = dict(fixture.config, **({form_check_hold.ATTENDED_KEY: self.TOLD} if attended else {}))
        application_browser.consume(fixture.root, fixture.database, config, request_id, scripted)
        seen["request"] = json.loads((seen["directory"] / "request.json").read_text())
        return seen

    TOLD = {"human_check": {"place": "ON_FORM", "after_question": 3, "kind": "HCAPTCHA"}, "order": "HALF_FIRST"}

    def test_the_guard_is_given_the_forms_address_when_the_session_was_told_one(self):
        form = "https://jobs.example.test/apply/1001/form"
        with mock.patch.object(type(self), "TOLD", dict(self.TOLD, start_url=form)):
            seen = self.run_session(True)
        self.assertEqual(seen["config"][browser_runtime.START_URL_CONFIG_KEY], form)
        self.assertEqual(self.run_session(True)["config"][browser_runtime.START_URL_CONFIG_KEY],
                         seen["request"]["payload"]["url"])                     # none recorded: the posting's address

    def test_the_attended_session_is_given_the_goal_the_facts_and_the_owners_lines(self):
        seen = self.run_session(True)
        self.assertEqual(seen["request"]["attended"], self.TOLD)           # where the check is, and the order
        self.assertIn('"attended"', seen["prompt"])
        for words in ("waiting as little as possible", "The user is present now", "A call to it pauses",
                      "the page may have changed", "request.attended.human_check", "request.attended.order",
                      "start from the bottom to the top", "fill half the answers first",
                      "never click, tick, solve or otherwise touch a human check"):
            self.assertIn(words, seen["prompt"], words)
        self.assertNotIn("attended", self.run_session(False)["request"])

    def test_only_an_attended_submit_gets_the_hold_its_prompt_and_its_arm_deadline(self):
        seen = self.run_session(False)
        self.assertNotIn(form_check_hold.CONFIG_KEY, seen["config"])
        self.assertNotIn(WAIT, seen["prompt"])
        self.assertIsNone(seen["claim"]["hold"])
        seen = self.run_session(True)
        self.assertEqual(Path(seen["config"][form_check_hold.CONFIG_KEY]), seen["directory"] / form_check_hold.CONTEXT)
        self.assertIn(application_browser.ATTENDED_SUPPLEMENT, seen["prompt"])
        self.assertIn("the page may have changed", seen["prompt"])
        self.assertEqual(seen["claim"]["hold"], str(seen["directory"] / application_browser.HOLD_FILE))
        argv = browser_runtime.guard_command(seen["config"], seen["directory"], "submit",
                                             seen["config"]["_application_guard_claim"],
                                             seen["config"][form_check_hold.CONFIG_KEY])
        self.assertEqual(argv[argv.index("--human-check") + 1], seen["config"][form_check_hold.CONFIG_KEY])
        # The guard is given the application's own address to open at once: the one the request names.
        self.assertEqual(argv[argv.index("--start-url") + 1], seen["request"]["payload"]["url"])
        self.assertIn("already opened the application's address", seen["prompt"])
        plain = self.run_session(False)
        self.assertNotIn("--start-url", browser_runtime.guard_command(
            plain["config"], plain["directory"], "submit", plain["config"]["_application_guard_claim"], None))


class TheCardAfterTheTimeLimit(unittest.TestCase):
    """The hold timed out, the session ended never-armed on CAPTCHA_REQUIRED: the existing wall path."""
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "blocked_session", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def end_submit(self, timed_out):
        request = self.run_until_submit()
        if timed_out:
            desktop_adapter.record(self.database, request["request_id"], form_check_hold.TIMED_OUT_EVENT, self.now, {})
            self.database.commit()
        self.assertTrue(desktop_adapter.complete(self.database, dict(
            request, result=dict(CHECK, acknowledged=False, prearm_blocked=True)), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")          # attempts 0: the try is not counted

    def card(self):
        return [card for card in access_outcomes.waiting(self.database) if card["job_key"] == self.key][0]

    def test_the_card_comes_back_with_the_owners_words_and_the_try_is_not_counted(self):
        wait = self.end_submit(timed_out=True)
        self.assertEqual(json.loads(wait["human_check"]), {"timed_out": True})
        self.assertEqual(self.card()["words"], "Timed out. Open again when you're ready.")
        self.assertTrue(self.row()["next_action"].startswith("Timed out. Open again when you're ready. "))
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.assertTrue(self.events("ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED"))

    def test_a_check_met_without_a_hold_says_what_it_always_said(self):
        wait = self.end_submit(timed_out=False)
        self.assertIsNone(wait["human_check"])
        self.assertEqual(self.card()["words"], access_outcomes.WAIT_REASONS["HUMAN_CHECK"])


if __name__ == "__main__":
    unittest.main()
