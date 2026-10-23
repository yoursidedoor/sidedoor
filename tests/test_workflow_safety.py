import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import applications
import conversion
import orchestrator
import workflow_tasks


NOW = "2026-09-17T16:00:00+00:00"
DEADLINE = "2026-09-18T16:00:00+00:00"
EVIDENCE = [{"source": "synthetic-fixture", "observed_at": NOW}]


def after(seconds):
    return (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()


class InterleavedCursor:
    def __init__(self, cursor, callback):
        self.cursor = cursor
        self.callback = callback

    def fetchone(self):
        row = self.cursor.fetchone()
        self.callback()
        return row


class InterleavedConnection:
    def __init__(self, database, callback):
        self.database = database
        self.callback = callback

    def __getattr__(self, name):
        return getattr(self.database, name)

    def __enter__(self):
        self.database.__enter__()
        return self

    def __exit__(self, *args):
        return self.database.__exit__(*args)

    def execute(self, query, parameters=()):
        cursor = self.database.execute(query, parameters)
        if query.startswith("SELECT * FROM workflow_tasks WHERE task_id="):
            callback = self.callback
            self.callback = lambda: None
            return InterleavedCursor(cursor, callback)
        return cursor


class SafetyFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        (self.root / "reports").mkdir()
        self.path = self.root / "state/orchestrator.sqlite3"
        self.database = self.connect()
        self.addCleanup(self.database.close)
        workflow_tasks.initialize(self.database)
        self.config = {"timezone": "America/Los_Angeles", "applications_enabled": False}
        for target in ("socket.socket.connect", "urllib.request.urlopen", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("No external effects in workflow review"))
            guard.start()
            self.addCleanup(guard.stop)

    def connect(self):
        database = sqlite3.connect(self.path, timeout=5)
        database.row_factory = sqlite3.Row
        return database

    def enqueue(self, action="one", **changes):
        arguments = {"worker": "synthetic-worker", "action_id": action, "parent_run_id": "parent",
                     "inputs": EVIDENCE, "deadline": DEADLINE, "now": NOW}
        arguments.update(changes)
        with self.database:
            return workflow_tasks.enqueue(self.database, **arguments)

    def row(self, task_id):
        return dict(self.database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone())


class WorkflowLeaseSafetyTests(SafetyFixture):
    def test_exact_lease_boundary_rejects_completion_without_mutation(self):
        task_id = self.enqueue()
        claim = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=1)
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            with self.database:
                workflow_tasks.finish(self.database, task_id, claim["token"], after(1), {}, EVIDENCE)
        self.assertEqual(list(self.database.iterdump()), before)

    def test_deadline_caps_lease_and_prevents_reclaim(self):
        task_id = self.enqueue(deadline=after(2))
        claim = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=300)
        self.assertEqual(claim["lease_until"], after(2))
        self.assertEqual(workflow_tasks.claim(self.database, task_id, after(2))["status"], "EXHAUSTED")
        self.assertEqual(self.row(task_id)["attempts"], 1)

    def test_reclaimed_read_rejects_prior_token_and_replayed_completion(self):
        task_id = self.enqueue()
        first = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=1)
        second = workflow_tasks.claim(self.database, task_id, after(1))
        with self.assertRaises(ValueError):
            workflow_tasks.finish(self.database, task_id, first["token"], after(1), {}, EVIDENCE)
        with self.database:
            workflow_tasks.finish(self.database, task_id, second["token"], after(1), {}, EVIDENCE)
        before = self.row(task_id)
        with self.assertRaises(ValueError):
            workflow_tasks.finish(self.database, task_id, second["token"], after(1), {}, EVIDENCE)
        self.assertEqual(self.row(task_id), before)

    def test_reclaim_between_completion_read_and_update_cannot_overwrite_new_owner(self):
        task_id = self.enqueue()
        original = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=1)
        other = self.connect()
        self.addCleanup(other.close)
        replacement = []

        def reclaim():
            replacement.append(workflow_tasks.claim(other, task_id, after(1)))

        interleaved = InterleavedConnection(self.database, reclaim)
        with self.assertRaises(ValueError):
            with self.database:
                workflow_tasks.finish(interleaved, task_id, original["token"], NOW, {}, EVIDENCE)
        self.assertEqual(replacement[0]["status"], "CLAIMED")
        self.assertEqual(self.row(task_id)["attempts"], 2)
        self.assertEqual(self.row(task_id)["status"], "RUNNING")

    def test_write_timeout_and_requested_retry_never_issue_another_claim(self):
        for mode in ("timeout", "retry"):
            with self.subTest(mode=mode):
                task_id = self.enqueue(action=mode, effect="WRITE")
                claim = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=1)
                if mode == "retry":
                    with self.database:
                        status = workflow_tasks.finish(self.database, task_id, claim["token"], NOW,
                                                       {}, [], status="RETRY_WAIT", blocker="UNKNOWN",
                                                       next_action="Request retry")
                    self.assertEqual(status, "UNCERTAIN")
                for current in (after(1), DEADLINE):
                    self.assertEqual(workflow_tasks.claim(self.database, task_id, current)["status"], "UNCERTAIN")
                self.assertEqual(self.row(task_id)["attempts"], 1)

    def test_abandoned_reads_exhaust_without_fourth_claim(self):
        task_id = self.enqueue()
        for attempt in range(3):
            self.assertEqual(workflow_tasks.claim(self.database, task_id, after(attempt), lease_seconds=1)["status"], "CLAIMED")
        self.assertEqual(workflow_tasks.claim(self.database, task_id, after(3))["status"], "EXHAUSTED")
        self.assertEqual(self.row(task_id)["attempts"], 3)

    def test_retry_backoff_survives_restart_and_exact_boundary(self):
        task_id = self.enqueue(max_attempts=2)
        claim = workflow_tasks.claim(self.database, task_id, NOW)
        with self.database:
            workflow_tasks.finish(self.database, task_id, claim["token"], NOW, {}, [],
                                  status="RETRY_WAIT", blocker="OFFLINE", next_action="Retry read")
        restarted = self.connect()
        self.addCleanup(restarted.close)
        self.assertEqual(workflow_tasks.claim(restarted, task_id, after(899))["status"], "RETRY_WAIT")
        claim = workflow_tasks.claim(restarted, task_id, after(900))
        self.assertEqual(claim["status"], "CLAIMED")
        with restarted:
            self.assertEqual(workflow_tasks.finish(restarted, task_id, claim["token"], after(900), {}, [],
                                                  status="RETRY_WAIT", blocker="OFFLINE", next_action="Retry read"), "EXHAUSTED")


class WorkflowTransactionSafetyTests(SafetyFixture):
    def test_simultaneous_claims_grant_exactly_one_owner(self):
        task_id = self.enqueue()
        barrier = threading.Barrier(4, timeout=5)

        def claim():
            database = self.connect()
            try:
                barrier.wait()
                return workflow_tasks.claim(database, task_id, NOW)["status"]
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda unused: claim(), range(4)))
        self.assertEqual(outcomes.count("CLAIMED"), 1)
        self.assertEqual(outcomes.count("RUNNING"), 3)

    def test_concurrent_enqueue_of_identical_identity_is_idempotent(self):
        barrier = threading.Barrier(2, timeout=5)

        def enqueue():
            database = self.connect()
            try:
                interleaved = InterleavedConnection(
                    database, lambda: None if database.in_transaction else barrier.wait())
                with database:
                    return workflow_tasks.enqueue(interleaved, "synthetic-worker", "racing", "parent",
                                                  EVIDENCE, DEADLINE, NOW)
            except Exception as error:
                return type(error).__name__
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda unused: enqueue(), range(2)))
        self.assertEqual(len(set(outcomes)), 1, outcomes)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0], 1)

    def test_compute_reservations_cannot_exceed_budget_under_concurrency(self):
        barrier = threading.Barrier(2, timeout=5)

        def reserve(action):
            database = self.connect()
            try:
                barrier.wait()
                return workflow_tasks.reserve_compute(database, action, NOW, 6000)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, ("first", "second")))
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.database.execute("SELECT SUM(seconds) FROM worker_compute_reservations").fetchone()[0], 6000)

    def test_compute_reservation_does_not_commit_callers_unfinished_transaction(self):
        self.database.execute("CREATE TABLE synthetic_business(value TEXT)")
        try:
            with self.database:
                self.database.execute("INSERT INTO synthetic_business VALUES ('unfinished')")
                workflow_tasks.reserve_compute(self.database, "reservation", NOW, 10)
                raise RuntimeError("Synthetic later transaction failure")
        except (RuntimeError, ValueError):
            pass
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM synthetic_business").fetchone()[0], 0)

    def test_changed_payload_and_submission_effect_cannot_rebind_identity(self):
        task_id = self.enqueue()
        for changes in ({"inputs": EVIDENCE + EVIDENCE}, {"effect": "WRITE"},
                        {"max_attempts": 1}, {"deadline": after(60)}, {"company_id": "another"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.enqueue(**changes)
        self.assertEqual(self.row(task_id)["status"], "QUEUED")

    def test_parent_identity_cannot_change_silently(self):
        task_id = self.enqueue()
        with self.assertRaises(ValueError):
            self.enqueue(parent_run_id="different-parent")
        self.assertEqual(self.row(task_id)["parent_run_id"], "parent")

    def test_failed_completion_event_rolls_back_business_and_task_state(self):
        task_id = self.enqueue()
        claim = workflow_tasks.claim(self.database, task_id, NOW)
        self.database.execute("CREATE TABLE synthetic_business(value TEXT)")
        self.database.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON workflow_task_events
            WHEN NEW.event='COMPLETE' BEGIN SELECT RAISE(FAIL, 'synthetic event persistence failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database:
                self.database.execute("INSERT INTO synthetic_business VALUES ('unfinished')")
                workflow_tasks.finish(self.database, task_id, claim["token"], NOW, {}, EVIDENCE)
        self.assertEqual(self.row(task_id)["status"], "RUNNING")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM synthetic_business").fetchone()[0], 0)

    def test_handoff_rolls_back_every_child_when_completion_event_fails(self):
        parent = self.enqueue()
        claim = workflow_tasks.claim(self.database, parent, NOW)
        self.database.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON workflow_task_events
            WHEN NEW.event='COMPLETE' BEGIN SELECT RAISE(FAIL, 'synthetic event failure'); END""")
        child = {"worker": "next", "action_id": "child", "inputs": EVIDENCE, "deadline": DEADLINE}
        before = list(self.database.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            workflow_tasks.handoff(self.database, parent, claim["token"], NOW, [child], {}, EVIDENCE)
        self.assertEqual(list(self.database.iterdump()), before)


class ServicePersistenceIsolationTests(SafetyFixture):
    def setUp(self):
        super().setUp()
        applications.initialize(self.database)
        self.inbox = Mock(return_value={"state": "COMPLETE"})
        self.application = Mock(return_value={"state": "PAUSED"})
        self.report = Mock(return_value={"state": "NOT_DUE"})
        self.notifications = Mock(return_value={"state": "COMPLETE"})
        for target, attribute, replacement in (
            (orchestrator.decision_transport, "cycle", self.inbox),
            (orchestrator, "posting_judge_cycle", self.application),  # applications left the hourly cycle (2026-10-02)
            (orchestrator.reporting, "daily", self.report),
            (orchestrator.notifications, "dispatch", self.notifications),
            (conversion, "cycle", Mock(return_value={"state": "COMPLETE"})),
        ):
            guard = patch.object(target, attribute, replacement)
            guard.start()
            self.addCleanup(guard.stop)
        clock = patch.object(orchestrator, "utcnow", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def service(self):
        return orchestrator.service_cycle(self.root, self.config, self.database)

    def assert_services_continue(self):
        failure = None
        result = None
        try:
            result = self.service()
        except Exception as error:
            failure = type(error).__name__
        self.assertEqual(self.notifications.call_count, 1,
                         "A local worker persistence failure skipped notifications; escaped=" + str(failure))
        self.assertIsNone(failure, "Service cycle must return a structured local failure")
        return result

    def test_enqueue_persistence_failure_isolates_only_affected_worker(self):
        self.database.execute("""CREATE TRIGGER reject_enqueue BEFORE INSERT ON workflow_tasks
            WHEN NEW.worker='decision-inbox' BEGIN SELECT RAISE(FAIL, 'synthetic insert failure'); END""")
        result = self.assert_services_continue()
        self.assertEqual(result["decision_transport"]["state"], "FAILED")
        self.assertEqual(self.inbox.call_count, 1)

    def test_claim_persistence_failure_isolates_only_affected_worker(self):
        self.database.execute("""CREATE TRIGGER reject_claim BEFORE UPDATE ON workflow_tasks
            WHEN NEW.worker='decision-inbox' AND NEW.status='RUNNING'
            BEGIN SELECT RAISE(FAIL, 'synthetic claim failure'); END""")
        result = self.assert_services_continue()
        self.assertEqual(result["decision_transport"]["state"], "FAILED")
        self.assertEqual(self.inbox.call_count, 1)

    def test_error_record_persistence_failure_does_not_abort_following_services(self):
        self.application.side_effect = RuntimeError("synthetic-private-diagnostic")
        self.database.execute("""CREATE TRIGGER reject_error_event BEFORE INSERT ON workflow_task_events
            WHEN NEW.event='BLOCKED' BEGIN SELECT RAISE(FAIL, 'synthetic persistence failure'); END""")
        result = self.assert_services_continue()
        self.assertEqual(result["posting_judge"]["state"], "FAILED")
        self.assertNotIn("synthetic-private-diagnostic", json.dumps(result))
        row = self.database.execute("SELECT status FROM workflow_tasks WHERE worker='posting-judge'").fetchone()
        self.assertNotEqual(row["status"], "COMPLETE")

    def test_completion_persistence_failure_returns_failure_and_continues(self):
        self.database.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON workflow_task_events
            WHEN NEW.event='COMPLETE' AND
                (SELECT worker FROM workflow_tasks WHERE task_id=NEW.task_id)='posting-judge'
            BEGIN SELECT RAISE(FAIL, 'synthetic completion failure'); END""")
        result = self.assert_services_continue()
        self.assertEqual(result["posting_judge"]["state"], "FAILED")
        self.assertEqual(self.application.call_count, 1)
        self.assertEqual(self.database.execute("SELECT status FROM workflow_tasks WHERE worker='posting-judge'").fetchone()[0], "BLOCKED")

    def test_callback_exception_rolls_back_uncommitted_work_and_redacts_details(self):
        self.database.execute("CREATE TABLE synthetic_business(value TEXT)")

        def fail(*args):
            self.database.execute("INSERT INTO synthetic_business VALUES ('unfinished')")
            raise RuntimeError("synthetic-private-diagnostic")

        self.application.side_effect = fail
        result = self.assert_services_continue()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM synthetic_business").fetchone()[0], 0)
        self.assertNotIn("synthetic-private-diagnostic", json.dumps(result))

    def test_stale_callback_result_cannot_become_complete_or_stop_notifications(self):
        clock = [NOW]

        def finish_after_expiry(*args):
            clock[0] = after(21600)  # the cycle-step lease is 6 hours (2026-09-29)
            return {"state": "COMPLETE"}

        self.application.side_effect = finish_after_expiry
        with patch.object(orchestrator, "utcnow", side_effect=lambda: clock[0]):
            result = self.assert_services_continue()
        self.assertEqual(result["posting_judge"]["state"], "FAILED")
        self.assertEqual(result["posting_judge"]["claim_state"], "STALE_REQUIRES_RECONCILIATION")
        self.assertNotEqual(self.database.execute("SELECT status FROM workflow_tasks WHERE worker='posting-judge'").fetchone()[0], "COMPLETE")

    def test_same_parent_action_replay_does_not_rebind_dynamic_timestamp(self):
        function = Mock(return_value={"state": "COMPLETE"})
        orchestrator.worker_step(self.database, "stable-parent", "synthetic-worker", function)
        with patch.object(orchestrator, "utcnow", return_value=after(1)):
            result = orchestrator.worker_step(self.database, "stable-parent", "synthetic-worker", function)
        self.assertEqual(function.call_count, 1)
        self.assertEqual(result["state"], "COMPLETE")

    def test_single_report_write_failure_still_attempts_other_reports(self):
        attempted = []

        def write(path, value):
            attempted.append(path.name)
            if path.name == "daily-report.json":
                raise OSError("Synthetic per-file persistence failure")

        with patch.object(orchestrator, "write_json", side_effect=write):
            self.assert_services_continue()
        self.assertIn("service-status.json", attempted)
        self.assertIn("recovery-status.json", attempted)
        # reports/applications.json is written only by the applications job (2026-10-02).
        self.assertNotIn("applications.json", attempted)

    def assert_optional_schema_failure_isolated(self, denied_table, result_key, callback):
        def authorize(action, table, column, database_name, trigger):
            if action == sqlite3.SQLITE_CREATE_TABLE and table == denied_table:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.database.set_authorizer(authorize)
        self.addCleanup(self.database.set_authorizer, None)
        result = None
        failure = None
        try:
            result = self.service()
        except Exception as error:
            failure = type(error).__name__
        proof = self.enqueue(action="unaffected-durable-claim")
        self.assertEqual(workflow_tasks.claim(self.database, proof, NOW)["status"], "CLAIMED")
        self.assertEqual(self.notifications.call_count, 1,
                         "Optional schema failure stopped unrelated services despite usable task persistence: " + str(failure))
        self.assertIsNone(failure)
        self.assertEqual(result[result_key]["state"], "FAILED")
        callback.assert_not_called()

    def test_conversion_schema_failure_does_not_block_unrelated_services(self):
        self.assert_optional_schema_failure_isolated("conversion_evidence", "conversion", conversion.cycle)

    def assert_shared_schema_failure_stops_callbacks(self, module):
        result = None
        with patch.object(module, "initialize", side_effect=sqlite3.OperationalError("synthetic shared schema unavailable")):
            try:
                result = self.service()
            except sqlite3.Error:
                pass
        for callback in (self.inbox, self.application, self.report, self.notifications,
                         conversion.cycle):
            callback.assert_not_called()
        if result is not None:
            self.assertIn(result.get("state"), {"FAILED", "BLOCKED"})

    def test_unavailable_shared_task_schema_stops_callbacks_without_claims(self):
        self.assert_shared_schema_failure_stops_callbacks(workflow_tasks)

    def test_unavailable_shared_application_schema_stops_callbacks(self):
        self.assert_shared_schema_failure_stops_callbacks(applications)

    def test_report_construction_failure_still_attempts_independent_reports(self):
        attempted = []
        failure = None
        result = None
        with patch.object(applications, "report", side_effect=sqlite3.OperationalError("synthetic application report unavailable")), \
                patch.object(orchestrator, "write_json", side_effect=lambda path, value: attempted.append(path.name)):
            try:
                result = self.service()
            except Exception as error:
                failure = type(error).__name__
        self.notifications.assert_called_once()
        # The daily report is written only on the cycle it is due (owner, 2026-10-06); the recovery
        # report is written every cycle and stands in as the independent report here.
        self.assertIn("recovery-status.json", attempted,
                      "Eager construction of a different report prevented every write: " + str(failure))
        self.assertIn("service-status.json", attempted)
        self.assertIsNone(failure)
        # The hourly cycle no longer builds the application report at all: reports/applications.json
        # belongs to the applications job (owner, 2026-10-02), so nothing failed here.
        self.assertNotIn("applications.json", attempted)
        self.assertEqual(result["persistence_errors"], [])


class BrowserSettleTransactionTests(unittest.TestCase):
    """A zero-credit browser settle must not leave an implicit transaction open.

    `settle_browser_reservations` writes `settled_at` for every reservation it settles,
    including one whose session consumed its whole booking and credits nothing back. The caller
    used to commit only `if credited`, so that write stayed in an open transaction and the next
    job's per-job `BEGIN IMMEDIATE` died on "cannot start a transaction within a
    transaction". Rekall, dinoco and example aerospace each lost a cycle to it on 2026-09-24.
    """

    def test_settling_with_nothing_to_credit_leaves_no_open_transaction(self):
        import desktop_adapter
        with tempfile.TemporaryDirectory() as temporary:
            database = sqlite3.connect(Path(temporary) / "synthetic.sqlite3")
            database.row_factory = sqlite3.Row
            applications.initialize(database)
            workflow_tasks.initialize(database)
            desktop_adapter.initialize(database)
            booked, ran_until = NOW, "2026-09-17T17:00:00+00:00"
            columns = {row[1]: "{}" for row in database.execute("PRAGMA table_info(desktop_requests)")
                       if row[3] and row[4] is None}
            columns.update(request_id="rq1", job_key="j:1", state="DONE")
            with database:
                workflow_tasks.reserve_compute(database, "application-browser:r1", booked, 480)
                workflow_tasks.track_browser_reservation(database, "application-browser:r1", "j:1", 1, 480, booked)
                database.execute("INSERT INTO desktop_requests(" + ",".join(columns) + ") VALUES ("
                                 + ",".join("?" * len(columns)) + ")", tuple(columns.values()))
                database.execute("INSERT INTO desktop_events(request_id,at,event,detail) "
                                 "VALUES ('rq1',?,'BROWSER_CONSUMER_COMPLETED','{}')", (ran_until,))
            # An hour of session against an eight-minute booking: nothing to credit back.
            with database:
                credited = workflow_tasks.settle_browser_reservations(database, "j:1", ran_until)
            self.assertEqual(credited, 0)
            self.assertIsNotNone(
                database.execute("SELECT settled_at FROM application_browser_reservations").fetchone()[0])
            self.assertFalse(database.in_transaction)
            database.execute("BEGIN IMMEDIATE")
            database.rollback()
            database.close()
