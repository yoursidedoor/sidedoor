"""E16 Y1 c (owner, 2026-11-13): "stop the running job and requeue it"; "Stop, requeue and continue from where
it stopped. If it was already on submission page and we stop it, there is no reason for it to go back to
reading phase". The user pressed a human check's card while another application's session holds the browser:
that session is stopped by its own guard (never after arm), its request is handed back, and the job is left
exactly as it was the moment before that session, due now, at the same stage.
Offline: the browser is the fake @playwright/mcp; no Chrome, no AI, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
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
import application_connection  # noqa: E402
import applications  # noqa: E402
import attended_submit  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import desktop_adapter  # noqa: E402
import live_view  # noqa: E402
import scheduler_loop  # noqa: E402
import session_yield  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402
from browser_fixtures import Client, fake_runtime  # noqa: E402
from manual_application_browser import Fixture, request_at  # noqa: E402
from test_browser_driver import submit_fixture  # noqa: E402


class Arm:
    """The arm claim of a submit session, as the guard holds it."""

    def __init__(self, attempted=False):
        self.attempted = attempted

    def arm(self, arguments):
        self.attempted = True
        return {"may_submit_once": True}

    def record_denial(self, denial):
        pass


class TheGuard(unittest.TestCase):
    def session(self, operation, **given):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "snapshot.yaml").write_text('- button "Submit application" [ref=e1]\n', encoding="utf-8")
        (folder / "calls.jsonl").write_text("")
        to_proxy, from_client = os.pipe()
        to_client, from_proxy = os.pipe()
        proxy = browser_guard.Proxy(operation, fake_runtime(folder / "snapshot.yaml", folder / "calls.jsonl"),
                                    audit=folder / "guard.jsonl", stdin=os.fdopen(to_proxy, encoding="utf-8"),
                                    stdout=os.fdopen(from_proxy, "w", encoding="utf-8"), **given)
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=proxy.run()), daemon=True)
        thread.start()
        client = Client(streams=(os.fdopen(from_client, "w", encoding="utf-8"), os.fdopen(to_client, encoding="utf-8")))

        def close():
            client.close()
            thread.join(10)
        self.addCleanup(close)
        return folder, client, thread, result

    def decisions(self, folder):
        return [json.loads(line)["decision"] for line in (folder / "guard.jsonl").read_text().splitlines()]

    def ask(self, folder):
        (folder / browser_guard.YIELD_FILE).write_text(json.dumps({"for": "job-b"}))

    def stopped(self, folder, thread, seconds=10):
        thread.join(seconds)
        return not thread.is_alive() and browser_runtime.guard_stopped(folder) == browser_guard.YIELD_REASON

    def test_a_read_and_a_submit_that_has_not_armed_stop_when_asked(self):
        for operation, given in (("inspect", {}), ("submit", {"arm_binding": Arm()})):
            folder, client, thread, result = self.session(operation, **given)
            self.assertFalse(client.call("browser_snapshot")[0])
            started = time.monotonic()
            self.ask(folder)
            self.assertTrue(self.stopped(folder, thread), operation)
            self.assertLess(time.monotonic() - started, 5)                     # asked, seen and stopped in seconds
            self.assertIn("YIELDED", self.decisions(folder))
            self.assertEqual(result["code"], 1)                                # the starter ends it as a stopped session

    def test_never_after_the_arm_was_called_and_the_session_runs_on_untouched(self):
        """A request arriving between the arm call and the Submit click does nothing."""
        arm = Arm()
        folder, client, thread, result = self.session("submit", arm_binding=arm)
        self.assertFalse(client.call("arm", {"observed_form_revision": "v1"})[0])   # the arm
        self.assertTrue(arm.attempted)
        self.ask(folder)                                                       # ... then the request
        deadline = time.monotonic() + 5
        while "YIELD_REFUSED" not in self.decisions(folder) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("YIELD_REFUSED", self.decisions(folder))
        self.assertEqual(json.loads((folder / browser_guard.YIELD_REFUSED_FILE).read_text()), {"reason": "ARMED"})
        self.assertFalse(client.call("browser_click", {"element": "Submit application", "target": "e1"})[0])   # the one Submit
        self.assertFalse(client.call("browser_snapshot")[0])
        self.assertTrue(thread.is_alive())
        self.assertIsNone(browser_runtime.guard_stopped(folder))
        self.assertNotIn("YIELDED", self.decisions(folder))
        # A denied arm counts as called too: the session is past the point where it may be cut.
        folder, client, thread, result = self.session("submit", arm_binding=Arm(attempted=True))
        self.ask(folder)
        time.sleep(1.5)
        self.assertTrue(thread.is_alive())
        self.assertIsNone(browser_runtime.guard_stopped(folder))

    def test_an_access_step_an_attended_submit_and_other_operations_are_never_stopped(self):
        class Held:
            def call(self):
                return "x"
        for operation, given, why in (("inspect", {"saved_sign_in": "jobs.example.com"}, "ACCESS_STEP"),
                                      ("submit", {"arm_binding": Arm(), "human_check_wait": Held()}, "ATTENDED"),
                                      ("webmail", {}, None), ("reconcile", {}, None), ("linkedin", {}, None)):
            folder, client, thread, result = self.session(operation, **given)
            self.ask(folder)
            time.sleep(1.5)
            self.assertTrue(thread.is_alive(), operation)
            self.assertIsNone(browser_runtime.guard_stopped(folder), operation)
            if why:
                self.assertEqual(json.loads((folder / browser_guard.YIELD_REFUSED_FILE).read_text()), {"reason": why})

    def test_only_the_guards_own_message_stops_it_never_one_a_client_sends(self):
        folder, client, thread, result = self.session("inspect")
        client.send({"jsonrpc": "2.0", "id": 77, "method": browser_guard.YIELD_METHOD})
        client.send({"jsonrpc": "2.0", "method": browser_guard.YIELD_METHOD, "params": {"x": 1}})
        self.assertFalse(client.call("browser_snapshot")[0])
        self.assertTrue(thread.is_alive())
        self.assertIsNone(browser_runtime.guard_stopped(folder))


class TheHandBack(unittest.TestCase):
    """Real `consume`: a session its guard stopped for an attended submit is handed back as one that never ran."""

    def asked_for(self, database, now):
        """Another job's attended submit is asked for and its card is being watched."""
        attended_submit.initialize(database)
        attended_submit._watched_column(database)
        with database:
            database.execute("INSERT OR REPLACE INTO %s(job_key,state,requested_at,human_check,polled_at) VALUES (?,?,?,'{}',?)"
                             % attended_submit.TABLE, ("job-b", attended_submit.REQUESTED, now, now))

    def stopped_session(self, fixture, request_id, arm=False):
        def scripted(config, prompt, directory, timeout):
            if arm:
                binding = application_browser.ArmBinding(config["_application_guard_claim"])
                binding.arm({"observed_form_revision": "fixture-form-v1"})
            (directory / browser_runtime.GUARD_STOPPED).write_text(json.dumps({"reason": browser_guard.YIELD_REASON}))
            (directory / "trace.jsonl").write_text("")
            raise RuntimeError(browser_runtime.SETUP_STOPPED_CODE)
        return application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)

    def request(self, fixture, request_id):
        return dict(fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone())

    def test_a_read_and_an_unarmed_submit_are_handed_back_with_their_claim_and_no_result(self):
        for operation in ("inspect", "submit"):
            if operation == "submit":
                fixture, request_id = submit_fixture(self)
            else:
                folder = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, folder, True)
                fixture = Fixture(folder / "fixture")
                self.addCleanup(fixture.database.close)
                request_id = fixture.queue("inspect")
            self.asked_for(fixture.database, applications.stamp())
            summary = self.stopped_session(fixture, request_id)
            self.assertIs(summary.get("yielded"), True, summary)
            self.assertNotEqual(summary.get("state"), "UNKNOWN")               # nothing to reconcile
            row = self.request(fixture, request_id)
            self.assertEqual((row["state"], row["claims"], row["result"], row["armed_at"]), ("EXPIRED", 0, None, None))
            self.assertIn(session_yield.YIELDED_EVENT, [item[0] for item in fixture.database.execute(
                "SELECT event FROM desktop_events WHERE request_id=?", (request_id,))])

    def test_a_submit_that_armed_is_never_handed_back_whatever_stopped_it(self):
        fixture, request_id = submit_fixture(self)
        self.asked_for(fixture.database, applications.stamp())
        summary = self.stopped_session(fixture, request_id, arm=True)
        self.assertNotIn("yielded", summary)
        self.assertEqual(summary.get("state"), "UNKNOWN")                      # reconciled as any armed session is
        self.assertIsNotNone(self.request(fixture, request_id)["armed_at"])
        self.assertNotEqual(self.request(fixture, request_id)["state"], "EXPIRED")

    def test_a_session_that_was_asked_to_stop_is_never_counted_whatever_became_of_the_card(self):
        """Review, 2026-11-13: the asker's card stopped being watched before the session ended; the stopped
        session took the ordinary failure path (a try counted, its submit reconciled)."""
        fixture, request_id = submit_fixture(self)                             # no attended request is live any more
        summary = self.stopped_session(fixture, request_id)
        self.assertIs(summary.get("yielded"), True)
        self.assertEqual(self.request(fixture, request_id)["state"], "EXPIRED")

    def test_a_session_its_guard_stopped_for_another_reason_is_an_ordinary_stopped_session(self):
        fixture, request_id = submit_fixture(self)

        def scripted(config, prompt, directory, timeout):
            (directory / browser_runtime.GUARD_STOPPED).write_text(json.dumps({"reason": "CLIENT_HINTS_FAILED"}))
            (directory / "trace.jsonl").write_text("")
            raise RuntimeError(browser_runtime.SETUP_STOPPED_CODE)
        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        self.assertNotIn("yielded", summary)


class Yielding(desktop_adapter.DesktopAdapter):
    """The desktop adapter, with the session of `operation` stopped for an attended submit (as the hosted adapter
    reports it after `consume` handed the request back)."""

    def __init__(self, base, operation):
        self.__dict__.update(base.__dict__)
        self.base, self.yield_operation, self.yielded_requests = base, operation, []

    def call(self, operation, payload):
        if operation != self.yield_operation or self.yielded_requests:
            return self.base.call(operation, payload)
        try:
            return self.base.call(operation, payload)
        except applications.AdapterPending as pending:
            now = self.clock()
            envelope = desktop_adapter.claim(self.database, self.root, "yield-fixture", now, request_id=pending.request_id)
            assert session_yield.hand_back(self.database, envelope, pending.request_id, operation, now)
            self.yielded_requests.append(pending.request_id)
            raise application_connection.SessionYielded(pending.request_id, now)


class TheJobKeepsItsStage(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def asked_for(self):
        attended_submit.initialize(self.database)
        attended_submit._watched_column(self.database)
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO %s(job_key,state,requested_at,human_check,polled_at) VALUES (?,?,?,'{}',?)"
                                  % attended_submit.TABLE, ("job-b", attended_submit.REQUESTED, self.now, self.now))

    def stop_round_after_this_job(self):
        """One round that ends after its first job (as when research takes its turn): the job is seen as the
        stop left it, before anything runs it again."""
        calls = []
        applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now,
                           should_stop=lambda: bool(calls) or calls.append(1))
        session_yield.take_stopped()

    def picture(self):
        """Everything the job has earned: its row, its read record, its answers, its approval, its reads' claims."""
        job = dict(self.row())
        tables = {name: [dict(row) for row in self.database.execute(
            "SELECT * FROM %s WHERE job_key=? ORDER BY rowid" % name, (self.key,))]
            for name in ("application_answers", "application_approvals", "application_requisitions")
            if self.database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            and "job_key" in {row[1] for row in self.database.execute("PRAGMA table_info(%s)" % name)}}
        claims = [(row["operation"], row["claims"]) for row in self.database.execute(
            "SELECT operation,claims FROM desktop_requests WHERE job_key=? AND state NOT IN ('EXPIRED','CONSUMED') ORDER BY rowid", (self.key,))]
        return {"job": {key: job[key] for key in ("state", "attempts", "blocker", "next_action", "reconcile_attempts")},
                "tables": tables, "claims": claims,
                "failures": len(self.events("APPLICATION_PREFLIGHT_FAILED")) + len(self.events("SUBMIT_PATH_EXCEPTION")),
                "waits": len(self.waits())}

    def test_a_read_that_was_stopped_is_due_again_as_a_read_with_nothing_spent(self):
        self.asked_for()
        before = self.picture()
        base, self.adapter = self.adapter, Yielding(self.adapter, "inspect")
        self.stop_round_after_this_job()
        self.assertEqual(len(self.adapter.yielded_requests), 1)
        self.assertEqual(self.picture(), before)                               # state, try, claims: as before the session
        self.assertFalse(self.row()["next_at"] and self.row()["next_at"] > self.now)   # due now
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.adapter = base
        self.advance(1)
        self.cycle()                                                           # the next round starts the same operation
        request = desktop_adapter.claim(self.database, self.root, "fixture", self.now)
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertNotIn(request["request_id"], self.adapter.__dict__.get("yielded_requests", []))

    def test_a_submit_that_was_stopped_is_due_again_as_a_submit_with_its_read_answers_and_try(self):
        request = self.run_until_submit()                                      # the job reaches its submit once...
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={
            "blocked": {"code": "FILE_UPLOAD_FAILED", "next_action": "retry"}, "acknowledged": False,
            "prearm_blocked": True}), self.now)["accepted"])
        for _ in range(6):                                                     # ...and is ready to submit again
            self.advance(60)
            if self.row()["next_at"] and self.row()["next_at"] > self.now:
                self.now = self.row()["next_at"]
            self.cycle()
            if self.database.execute("SELECT 1 FROM desktop_requests WHERE job_key=? AND operation='submit' AND state='PENDING'",
                                     (self.key,)).fetchone():
                break
        pending = self.database.execute("SELECT request_id,attempt_id FROM desktop_requests WHERE job_key=? AND operation='submit' "
                                        "AND state='PENDING'", (self.key,)).fetchone()
        self.assertIsNotNone(pending, dict(self.row()))
        # Put the job back to the moment before that second submit session, and run it again with the stop.
        with self.database:
            self.database.execute("DELETE FROM desktop_requests WHERE request_id=?", (pending["request_id"],))
            self.database.execute("DELETE FROM application_attempts WHERE attempt_id=?", (pending["attempt_id"],))
            self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',attempts=attempts-1,next_at=NULL WHERE job_key=?", (self.key,))
        self.asked_for()
        before = self.picture()
        reads = len([row for row in self.requests() if row["operation"] == "inspect"])
        base, self.adapter = self.adapter, Yielding(self.adapter, "submit")
        self.advance(1)
        self.stop_round_after_this_job()
        self.assertEqual(len(self.adapter.yielded_requests), 1)
        after = self.picture()
        self.assertEqual(after, before)                                        # state, try, read, answers, approval: identical
        self.assertIsNone(self.row()["next_at"])                               # due now
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.assertEqual(self.database.execute("SELECT state FROM desktop_requests WHERE request_id=?",
                                               (self.adapter.yielded_requests[0],)).fetchone()[0], "CONSUMED")
        self.assertIsNotNone(session_yield.kept_read(self.database, self.key))
        self.assertEqual(len(self.events(session_yield.RESTORED_EVENT)), 1)
        # The next round goes straight to the submit again: no read, even with no reusable read on record.
        self.adapter = base
        self.advance(1)
        with mock.patch.object(applications, "reusable_read", return_value=None):
            self.cycle()
        again = desktop_adapter.claim(self.database, self.root, "fixture", self.now)
        self.assertEqual(again["payload"]["operation"], "submit")
        self.assertTrue(again["payload"]["answers"])
        self.assertEqual(len([row for row in self.requests() if row["operation"] == "inspect"]), reads)
        self.assertEqual(len(self.events("APPLICATION_READ_KEPT_FOR_YIELDED_SUBMIT")), 1)
        self.assertIsNone(session_yield.kept_read(self.database, self.key))    # the kept read has served


class TheStoppedJobContinuesInTheSameRound(unittest.TestCase):
    """The real run of 2026-11-13: after the attended submit ended, the stopped job waited for the next hourly
    round. Owner: "the other job continues"."""
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def test_a_submit_stopped_for_a_press_starts_again_right_after_the_attended_job_with_no_read(self):
        session_yield.take_stopped()
        request = self.run_until_submit()                                      # job A reaches its submit once...
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={
            "blocked": {"code": "FILE_UPLOAD_FAILED", "next_action": "retry"}, "acknowledged": False,
            "prearm_blocked": True}), self.now)["accepted"])
        for _ in range(6):
            self.advance(60)
            if self.row()["next_at"] and self.row()["next_at"] > self.now:
                self.now = self.row()["next_at"]
            self.cycle()
            pending = self.database.execute("SELECT request_id,attempt_id FROM desktop_requests WHERE job_key=? "
                                            "AND operation='submit' AND state='PENDING'", (self.key,)).fetchone()
            if pending:
                break
        with self.database:                                                    # ...and stands just before its next one
            self.database.execute("DELETE FROM desktop_requests WHERE request_id=?", (pending["request_id"],))
            self.database.execute("DELETE FROM application_attempts WHERE attempt_id=?", (pending["attempt_id"],))
            self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',attempts=attempts-1,next_at=NULL WHERE job_key=?", (self.key,))
        attempts = self.row()["attempts"]
        reads = len([row for row in self.requests() if row["operation"] == "inspect"])
        other = self.second_job()                                              # job B: its card will be pressed
        with self.database:
            self.database.execute("UPDATE application_jobs SET next_at=? WHERE job_key=?", (desktop_adapter.later(self.now, 86400), other))
        attended_submit.initialize(self.database)
        attended_submit._watched_column(self.database)
        order = []
        real = applications.process

        def process(database, job, *args, **kwargs):
            order.append(job["job_key"])
            return real(database, job, *args, **kwargs)

        base = self.adapter

        class Pressed(Yielding):
            def call(inner, operation, payload):                              # the press comes while A's submit session runs
                if operation == "submit" and not inner.yielded_requests:
                    with self.database:
                        self.database.execute("INSERT OR REPLACE INTO %s(job_key,state,requested_at,human_check,polled_at) "
                                              "VALUES (?,?,?,'{}',?)" % attended_submit.TABLE,
                                              (other, attended_submit.REQUESTED, self.now, self.now))
                return Yielding.call(inner, operation, payload)

        self.adapter = Pressed(base, "submit")
        self.advance(1)
        with mock.patch.object(applications, "process", process), \
                mock.patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        self.assertEqual(order[:3], [self.key, other, self.key], order)        # A stopped, B (attended), then A again
        self.assertEqual(order.count(self.key), 2)                             # once per stop: no loop
        again = self.database.execute("SELECT request_id FROM desktop_requests WHERE job_key=? AND operation='submit' "
                                      "AND state='PENDING'", (self.key,)).fetchone()
        self.assertIsNotNone(again, dict(self.row()))                          # its submit is started again in this round
        self.assertNotIn(again["request_id"], self.adapter.yielded_requests)
        self.assertEqual(len([row for row in self.requests() if row["operation"] == "inspect"]), reads)   # with no read
        self.assertEqual(self.row()["attempts"], attempts + 1)                 # the one try of the session now running
        self.assertEqual(session_yield.take_stopped(), [])

    def test_a_round_that_was_told_to_stop_leaves_the_stopped_job_due_for_the_next_start(self):
        session_yield.take_stopped()
        self.attended_key = None
        base, self.adapter = self.adapter, Yielding(self.adapter, "inspect")
        attended_submit.initialize(self.database)
        attended_submit._watched_column(self.database)
        with self.database:
            self.database.execute("INSERT INTO %s(job_key,state,requested_at,human_check,polled_at) VALUES ('job-b',?,?,'{}',?)"
                                  % attended_submit.TABLE, (attended_submit.REQUESTED, self.now, self.now))
        calls = []
        applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now,
                           should_stop=lambda: bool(calls) or calls.append(1))   # research takes its turn after the first job
        self.assertEqual(len(self.adapter.yielded_requests), 1)
        self.assertFalse(self.row()["next_at"] and self.row()["next_at"] > self.now)   # due at the next start, as it was


class TheReviewersOrder(unittest.TestCase):
    """Review, 2026-11-13, in its order: job A is handled in a round and ends waiting on its form check; the
    round moves on to job B; the user presses A's card; the card keeps asking every second. Exactly one stop,
    A's attended session starts, B resumes once after it."""
    for _name in ("setUp", "connect", "write_config", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name
    _fixture = setUp
    CHECK = {"place": "ON_FORM", "after_question": 2, "kind": "OTHER", "form_readable": True}

    def setUp(self):
        self._fixture()
        for target, name, value in ((attended_submit, "_running", lambda: True),
                                    (scheduler_loop, "load_config", lambda root=None: {"applications_enabled": True}),
                                    (scheduler_loop, "run_now", mock.Mock())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        session_yield.take_stopped()

    def review(self):
        return dict(walls.AccessOutcomeTests.review(self), human_check=self.CHECK)

    def run_round(self, only=None, presses=True):
        """One round in which B's session is "running" while the card of A is pressed and then asks 4 more times."""
        test, order, stops = self, [], []

        class Running(desktop_adapter.DesktopAdapter):
            def __init__(inner, base):
                inner.__dict__.update(base.__dict__)
                inner.base = base

            def call(inner, operation, payload):
                key = payload["job"]["job_key"]
                if key != test.other:
                    return inner.base.call(operation, payload)
                try:
                    return inner.base.call(operation, payload)
                except applications.AdapterPending as pending:
                    if isinstance(pending, application_connection.CapacityWait):
                        raise
                    now = inner.clock()
                    envelope = desktop_adapter.claim(test.database, test.root, "running-fixture", now, request_id=pending.request_id)
                    directory = Path(tempfile.mkdtemp())
                    test.addCleanup(shutil.rmtree, directory, True)
                    with test.database:
                        desktop_adapter.record(test.database, pending.request_id, "BROWSER_SESSION_DIRECTORY", now,
                                               {"directory": str(directory)})
                    if presses:
                        live = TheCardAsks.Live()
                        attended_submit.step(test.database, test.key, now, live, poll=not test.first_press())   # the press
                        for _ in range(4):
                            attended_submit.step(test.database, test.key, now, live, poll=True)                 # the card asking
                    if (directory / browser_guard.YIELD_FILE).exists():       # its guard stops it; its starter hands it back
                        assert session_yield.hand_back(test.database, envelope, pending.request_id, operation, now)
                        stops.append(pending.request_id)
                        raise application_connection.SessionYielded(pending.request_id, now)
                    with test.database:                                        # it runs on: its read completes
                        pass
                    assert desktop_adapter.complete(test.database, dict(envelope, result=walls.AccessOutcomeTests.review(test)), now)["accepted"]
                    return inner.base.call(operation, payload)

        real = applications.process

        def process(database, job, *args, **kwargs):
            order.append(job["job_key"])
            return real(database, job, *args, **kwargs)

        base, self.adapter = self.adapter, Running(self.adapter)
        try:
            with mock.patch.object(applications, "process", process), \
                    mock.patch.object(applications, "same_posting_duplicate", return_value=False):
                applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now, only=only)
        finally:
            self.adapter = base
        return order, stops

    def first_press(self):
        return not attended_submit.live(self.database, self.key)

    def ready_and_waiting(self):
        """A is read, answered and ready: it waits on the user for its form check. B is queued behind it."""
        for _ in range(6):
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "fixture", self.now)
            if request:
                self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
            self.advance(60)
            if self.waits():
                break
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        with self.database:                                                    # A is due to be looked at in the round again
            self.database.execute("UPDATE application_jobs SET updated_at=? WHERE job_key=?", ("2000-01-01T00:00:00+00:00", self.key))
        self.other = self.second_job()

    def test_exactly_one_stop_the_attended_session_starts_and_the_stopped_job_resumes_once(self):
        self.ready_and_waiting()
        order, stops = self.run_round()
        self.assertEqual(len(stops), 1, order)                                 # exactly one stop, whatever the polls
        self.assertEqual(len(self.events(session_yield.ASKED_EVENT)), 1)
        after = order[order.index(self.other) + 1:]
        self.assertEqual(after[:2], [self.key, self.other], order)             # A (attended) next, then B again
        self.assertEqual(order.count(self.other), 2)                           # B resumes once: no lap after lap
        submit = self.database.execute("SELECT state FROM desktop_requests WHERE job_key=? AND operation='submit'",
                                       (self.key,)).fetchone()
        self.assertIsNotNone(submit, "A's attended submit was not started")
        reads = self.database.execute("SELECT state,claims FROM desktop_requests WHERE job_key=? AND operation='inspect' "
                                      "ORDER BY rowid", (self.other,)).fetchall()
        self.assertEqual(len(reads), 2)                                        # the stopped session, and the one after it
        self.assertEqual(reads[0]["claims"], 0)                                # the stopped one: its claim returned, uncounted
        self.assertFalse(self.database.execute("SELECT 1 FROM application_events WHERE job_key=? AND event='APPLICATION_PREFLIGHT_FAILED'",
                                               (self.other,)).fetchone())      # and never a failure

    def test_a_round_that_will_not_run_the_pressed_job_stops_nothing(self):
        self.ready_and_waiting()
        order, stops = self.run_round(only={self.other})
        self.assertEqual(stops, [])                                            # no stop at all
        self.assertEqual(order, [self.other])
        self.assertFalse(self.events(session_yield.ASKED_EVENT))
        self.assertEqual(attended_submit.due(self.database, self.now), [self.key])   # the request waits for a round of its own


class TheCardAsks(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name
    _fixture = setUp

    def setUp(self):
        self._fixture()
        for target, name, value in ((attended_submit, "_running", lambda: True),
                                    (scheduler_loop, "load_config", lambda root=None: {"applications_enabled": True}),
                                    (scheduler_loop, "run_now", mock.Mock())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        request = self.run_until_submit()                                      # this job waits on a form check
        check = {"place": "ON_FORM", "after_question": 2, "form_readable": True}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={
            "blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "x", "human_check": check}, "acknowledged": False,
            "prearm_blocked": True}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.other = self.second_job()
        with self.database:
            session_yield.round_started(self.database, None, self.now)        # a round is running, every job in scope

    def running(self, operation, armed=False, folder=True, request="busy"):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        with self.database:
            self.database.execute("DELETE FROM desktop_requests WHERE job_key=?", (self.other,))
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                created_at,expires_at,lease_until,claims,armed_at) VALUES (?,?,?,?,'{}','CLAIMED',?,?,?,1,?)""",
                                  (request, "b-" + request, self.other, operation, self.now, desktop_adapter.later(self.now, 900),
                                   desktop_adapter.later(self.now, 900), self.now if armed else None))
            if folder:
                desktop_adapter.record(self.database, request, "BROWSER_SESSION_DIRECTORY", self.now, {"directory": str(directory)})
        return directory

    class Live:
        session = None

        def available(self, purpose=None):
            return True

    def step(self, poll=True):
        return attended_submit.step(self.database, self.key, self.now, self.Live(), poll=poll)

    def test_a_running_read_or_unarmed_submit_is_asked_to_stop_once_and_the_card_says_opening(self):
        for index, operation in enumerate(("inspect", "submit")):
            if index:
                self.setUp()                                                   # a new job and a new request
            self.assertIn(">Opening the application.<", self.step(poll=False))    # the press
            directory = self.running(operation, request="running-" + operation)
            for _ in range(5):                                                 # the card asks every second
                self.assertIn(">Opening the application.<", self.step())
            self.assertEqual(json.loads((directory / browser_guard.YIELD_FILE).read_text())["for"], self.key)
            self.assertEqual(len(self.events(session_yield.ASKED_EVENT)), 1)   # one per request, whatever the polls

    def test_one_stop_per_request_a_second_session_is_never_stopped_by_the_same_request(self):
        """Review, 2026-11-13: each poll stopped the restarted session again, lap after lap."""
        self.step(poll=False)
        first = self.running("inspect", request="first")
        self.step()
        self.assertTrue((first / browser_guard.YIELD_FILE).exists())
        second = self.running("inspect", request="second")                     # the stopped job, started again
        for _ in range(5):
            self.assertIn(">Opens in a moment.<", self.step())                 # it runs to its end
        self.assertFalse((second / browser_guard.YIELD_FILE).exists())
        self.assertEqual(len(self.events(session_yield.ASKED_EVENT)), 1)
        self.step(poll=False)                                                  # pressing the same request again: still spent
        self.assertFalse((second / browser_guard.YIELD_FILE).exists())

    def test_nothing_is_stopped_when_the_round_will_not_run_the_asking_job(self):
        self.step(poll=False)
        for scope in ([self.other], "none"):                                   # a round for other jobs only; no round at all
            with self.database:
                self.database.execute("UPDATE %s SET stop_request=NULL" % attended_submit.TABLE)
                if scope == "none":
                    session_yield.round_ended(self.database)
                else:
                    session_yield.round_started(self.database, set(scope), self.now)
            directory = self.running("inspect")
            for _ in range(3):
                self.assertIn(">Opening the application.<", self.step())       # its true line; then the bound's
            self.assertFalse((directory / browser_guard.YIELD_FILE).exists())
        self.assertFalse(self.events(session_yield.ASKED_EVENT))

    def test_what_may_not_be_stopped_is_left_alone_and_the_card_says_the_short_wait(self):
        self.step(poll=False)
        for operation, armed in (("submit", True), ("reconcile", False), ("verify", False), ("investigate", False)):
            directory = self.running(operation, armed)
            self.assertIn(">Opens in a moment.<", self.step(), operation)
            self.assertFalse((directory / browser_guard.YIELD_FILE).exists(), operation)
        directory = self.running("inspect")                                    # an access step: its guard refused
        (directory / browser_guard.YIELD_REFUSED_FILE).write_text(json.dumps({"reason": "ACCESS_STEP"}))
        self.assertIn(">Opens in a moment.<", self.step())
        self.assertFalse((directory / browser_guard.YIELD_FILE).exists())

    def test_pressing_the_button_again_starts_the_applications_job_now_and_a_poll_does_not(self):
        self.step(poll=False)                                                  # the press
        scheduler_loop.run_now.reset_mock()
        self.step()                                                            # the card asking by itself
        scheduler_loop.run_now.assert_not_called()
        self.step(poll=False)                                                  # "Open this again.": pressed
        scheduler_loop.run_now.assert_called_once_with("applications")

    def test_drawing_the_page_asks_nothing(self):
        self.step(poll=False)
        directory = self.running("inspect")
        with mock.patch.object(applications, "stamp", return_value=self.now):
            self.assertTrue(attended_submit.cards(self.database, self.now))
        self.assertFalse((directory / browser_guard.YIELD_FILE).exists())

    def test_nothing_is_asked_without_a_press_and_a_page_the_user_opened_is_never_stopped(self):
        directory = self.running("inspect")
        session_yield.ask(self.database, self.key, self.now, write=False)      # a page being drawn: nothing is asked
        self.assertFalse((directory / browser_guard.YIELD_FILE).exists())
        self.assertIsNone(session_yield.ask(self.database, self.other, self.now))   # nothing else is running: nothing to ask
        live = self.Live()
        live.session = object()                                                # the user's own sign-in view is open
        html = attended_submit.step(self.database, self.key, self.now, live)
        self.assertEqual(html, '<p class="access-hint muted" role="status">%s</p>' % live_view.ONE_AT_A_TIME_WORDS)
        self.assertFalse(attended_submit.live(self.database, self.key))        # nothing was asked for
        self.assertFalse((directory / browser_guard.YIELD_FILE).exists())


if __name__ == "__main__":
    unittest.main()
