"""Patch O: the invariants that had to survive merging F and H into A+B+D+I.

Two additions converge on `application_recovery.py`:

* **F** stopped `application_browser.consume()` nulling a failed investigation's
  findings. The two edits F could not make itself live here: `finding_context`
  reports the returned `error_code` and the explicit `resolution_verified` flag,
  and `recover()` adopts a returned RECOVERABLE code as the blocker instead of
  discarding it. Neither weakens the `status == "RESOLVED"` gate.
* **H** proved that a recorded, explicit, expiring, job-scoped reopen allowance is
  a dead letter unless `recover()` scopes `starts`/`findings`/`prepared`/`decisions`
  to it: every reopened job re-abandons inside the same cycle.

Everything here runs in an isolated temp fixture with sockets and `subprocess.Popen`
patched to raise. No live browser, no submission, no project state, no network.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import application_recovery
import applications
import desktop_adapter
import orchestrator
from manual_application_browser import Fixture
from manual_partial_review_recovery import complete, forbidden, inspections, offset
from manual_unknown_inspection_recovery import CANONICAL, CODE, NOW, RESULT, reread_phase, seed


RETURNED = "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"
GRANT = "user-authorized-reopen-example-1"
AUTHORIZATION = ("User explicitly instructed on 2026-09-18 to reopen these abandoned application jobs; "
                 "every original abandonment record, reason, evidence, compute charge and attempt count is retained.")
SOCKETS = ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
           "socket.socket.sendto", "socket.getaddrinfo", "subprocess.Popen")


def preserved_failure(code=RETURNED, requisition="role-123"):
    """Exactly the failure result patch F's `application_browser.consume()` now returns.

    `status` stays UNRESOLVED and `resolution_verified` is False: the browser AI's
    reported findings are preserved for the coordinator to read, and were explicitly
    rejected by validation rather than applied.
    """
    return {"status": "UNRESOLVED", "reason": code,
            "error": {"code": code, "message": "The observed posting did not establish the exact role identity"},
            "resolution": {"identity": {"requisition": requisition}, "destination": None,
                           "history": [{"evidence_id": "synthetic-history-1", "relationship": "NONE"}]},
            "resolution_verified": False,
            "reported_investigation": {"origin": "unverified_model_result", "status": "UNRESOLVED",
                                       "resolution_reported": True, "resolution_at": "/resolution",
                                       "note": "validation rejected this resolution; it was not applied",
                                       "reported_evidence_note": "model quotes were never validated against the trace"},
            "next_action": "Review this failed attempt, which returned %s, and choose a different supported read" % code}


class ConsumerAdapter(desktop_adapter.DesktopAdapter):
    """Inspect-only synthetic adapter: it can never submit and never verifies a receipt."""

    def __init__(self, item):
        self.now = NOW
        self.decisions = []
        self.operations = []
        super().__init__(item.database, item.root, item.config, lambda: self.now)
        self.can_submit = False

    def recovery_decision(self, packet):
        assert packet["read_only"] and isinstance(packet["previous_decisions"], list)
        assert "review" not in packet, "the parked-hold AI review is deleted (owner, 2026-09-27)"
        self.decisions.append(packet)
        return {"action": "RETRY", "method": "official_posting", "progress": False,
                "instructions": "Synthetic bounded read " + str(packet["recovery"]["attempt"]),
                "reason": "Inspection remains incomplete"}

    def call(self, operation, payload):
        assert operation in {"inspect", "investigate", "reconcile"}, operation
        self.operations.append(operation)
        return super().call(operation, payload)


def cycle(item, seconds=0):
    """One scheduled application cycle, exactly as launchd reaches it."""
    item.adapter.now = desktop_adapter.later(NOW, seconds)
    with patch.object(applications, "build_adapter", lambda *args, **kwargs: item.adapter):
        return orchestrator.application_cycle(item.root, item.config, item.database, item.adapter.now)


def claims(item):
    return item.database.execute(
        "SELECT COALESCE(sum(claims),0) FROM desktop_requests WHERE operation='investigate'").fetchone()[0]


def park(item):
    """Reach the real parked hold through the real path: three claims, last decision RETRY."""
    seed(item, held=True)
    base = reread_phase(item, RESULT)
    for attempt in range(3):
        seconds = base + attempt * 1000
        cycle(item, seconds)
        assert len(item.adapter.decisions) == attempt + 1, item.job()
        complete(item, "investigate", {
            "status": "FAILED",
            "reason": "Read the company posting and observed SEE ON ASHBY and APPLY NOW links",
            "browser_evidence": [{"pointer": "/observed_links", "quote": CANONICAL,
                                  "tool_call_id": "synthetic-read-" + str(attempt + 1)}]})
        cycle(item, seconds + 60)
    job = item.job()
    assert job["state"] == "BLOCKED" and job["blocker"] == application_recovery.CLAIMS_EXHAUSTED_BLOCKER, job
    assert len(application_recovery.records(item.database, item.key,
                                            application_recovery.CLAIMS_EXHAUSTED_EVENT)) == 1
    assert claims(item) == 3 and job["attempts"] == 0
    return base


@contextlib.contextmanager
def fixture():
    """One isolated synthetic project root with the inspect-only consumer adapter."""
    with contextlib.ExitStack() as guards:
        for target in SOCKETS:
            guards.enter_context(patch(target, forbidden))
        temporary = guards.enter_context(tempfile.TemporaryDirectory(prefix="merge-integration-"))
        item = Fixture(Path(temporary))
        (item.root / "reports").mkdir(exist_ok=True)
        item.config["application_recovery"] = {"enabled": True, "max_attempts": 3, "deadline_hours": 48}
        item.adapter = ConsumerAdapter(item)
        guards.callback(item.database.close)
        yield item


def records(item, event, key=None):
    return application_recovery.records(item.database, key or item.key, event)


def rows(item, event=None):
    sql = "SELECT id,job_key,event,detail,at FROM application_events"
    return [tuple(row) for row in item.database.execute(
        sql + (" WHERE event=?" if event else "") + " ORDER BY id", (event,) if event else ())]


def investigate(item, result, seconds):
    """One real bounded investigation claim: coordinator decides, the mailbox answers."""
    cycle(item, seconds)
    complete(item, "investigate", result)
    cycle(item, seconds + 60)


FAILED_READ = {"status": "FAILED", "reason": "Synthetic bounded read"}


def service(item):
    """Answer whatever read the existing desktop mailbox is holding, as the desktop would."""
    envelope = desktop_adapter.claim(item.database, item.root, "synthetic-reader",
                                     item.adapter.now, read_only=True)
    if not envelope:
        return None
    operation = envelope["payload"]["operation"]
    desktop_adapter.complete(item.database, dict(envelope,
                             result=RESULT if operation == "inspect" else dict(FAILED_READ)), item.adapter.now)
    return operation


def reinspect(item, seconds, step=200, steps=6):
    """Drive scheduled cycles until the reopened job has re-inspected and opened a FRESH lifecycle.

    This is the whole point of the reopen: the job goes back through
    `applications.process`'s local gate, duplicate protection and `adapter.call("inspect")`,
    and re-enters recovery only because that ordinary read blocks again.
    """
    before = len(records(item, "APPLICATION_RECOVERY_STARTED"))
    at = seconds
    for _ in range(steps):
        cycle(item, at)
        if len(records(item, "APPLICATION_RECOVERY_STARTED")) > before:
            return at
        service(item)
        at += step
    raise AssertionError("the reopened job never re-entered recovery: " + str(dict(item.job())))


def first_claim(item, result):
    """Start the real recovery lifecycle and spend exactly one claim on `result`."""
    seed(item, held=True)
    base = reread_phase(item, RESULT)
    investigate(item, result, base)
    return base


def reopen(item, seconds, claims=3, hours=48.0, expires=None, key=None):
    """Record the bounded, explicit, job-scoped allowance, as H's tool does.

    This is the durable grant only: an attempt allowance with no time limit (grants never
    expire, owner 2026-09-28, so the tool stores a never-expiring placeholder), a
    consumption ledger row per job and one appended `APPLICATION_REOPENED` event. It
    erases nothing - every original lifecycle, finding, decision, spent claim and
    abandonment row stays exactly where it was.
    """
    key = key or item.key
    at = desktop_adapter.later(NOW, seconds)
    expires = expires if expires is not None else "9999-12-31T23:59:59+00:00"
    with item.database:
        item.database.execute("""CREATE TABLE IF NOT EXISTS application_reopen_grants (
            authorization_id TEXT PRIMARY KEY, authorized_by TEXT NOT NULL, job_keys TEXT NOT NULL,
            investigation_claims INTEGER NOT NULL, deadline_hours REAL NOT NULL,
            authorized_at TEXT NOT NULL, expires_at TEXT NOT NULL, authorization TEXT NOT NULL)""")
        item.database.execute("""CREATE TABLE IF NOT EXISTS application_reopen_grant_jobs (
            authorization_id TEXT NOT NULL, job_key TEXT NOT NULL, reopened_at TEXT NOT NULL,
            reopen_event_id INTEGER NOT NULL, defect_classes TEXT NOT NULL,
            original_abandoned_at TEXT NOT NULL, original_reason TEXT NOT NULL,
            original_attempts INTEGER NOT NULL, deadline TEXT NOT NULL,
            PRIMARY KEY(authorization_id,job_key))""")
        job = item.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        applications.event(item.database, key, "APPLICATION_REOPENED", {
            "authorization_id": GRANT, "authorized_by": "user:explicit-instruction:2026-09-18",
            "authorization": AUTHORIZATION,
            "allowance": {"investigation_claims": claims, "deadline_hours": hours, "expires_at": expires},
            "previous_state": {"state": job["state"], "blocker": job["blocker"], "attempts": job["attempts"]},
            "submission_status": "NOT_SUBMITTED", "history_preserved": True}, at)
        event_id = item.database.execute("SELECT MAX(id) FROM application_events WHERE job_key=? AND event=?",
                                         (key, "APPLICATION_REOPENED")).fetchone()[0]
        item.database.execute("INSERT OR REPLACE INTO application_reopen_grants VALUES (?,?,?,?,?,?,?,?)",
                              (GRANT, "user:explicit-instruction:2026-09-18", json.dumps([key]),
                               claims, hours, at, expires, AUTHORIZATION))
        item.database.execute("INSERT OR REPLACE INTO application_reopen_grant_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                              (GRANT, key, at, event_id, json.dumps(["CAP_VS_JUDGMENT_CONFLATION"]),
                               at, "RECOVERY_ATTEMPTS_EXHAUSTED", job["attempts"], expires))
        item.database.execute("""UPDATE application_jobs SET state='QUEUED',blocker=NULL,
            next_action='Reopened under an explicit bounded user authorization',next_at=NULL,updated_at=?
            WHERE job_key=?""", (at, key))
        item.database.execute("DELETE FROM application_blockers WHERE job_key=?", (key,))
    return at


class ErrorCodeThreadingTests(unittest.TestCase):
    """Patch F, end to end through the merged `recover()`."""

    def test_returned_recoverable_code_becomes_the_blocker_and_reaches_the_coordinator(self):
        with fixture() as item:
            base = first_claim(item, preserved_failure())
            job = item.job()
            # The stale original blocker was INSPECTION_INCOMPLETE; the next cycle now
            # re-enters recovery on the code the browser actually returned.
            self.assertEqual(job["blocker"], RETURNED)
            self.assertEqual(job["state"], "BLOCKED")
            lifecycle = records(item, "APPLICATION_RECOVERY_STARTED")[0]
            self.assertEqual(lifecycle["blocker"], CODE, "the lifecycle's original blocker is untouched")

            finding = records(item, "APPLICATION_RECOVERY_INVESTIGATED")[-1]
            context = application_recovery.finding_context(finding)
            self.assertEqual(context["error_code"], RETURNED)
            self.assertIs(context["resolution_verified"], False)
            self.assertEqual(context["reported_investigation"]["origin"], "unverified_model_result")
            self.assertEqual(context["status"], "UNRESOLVED")
            self.assertEqual(context["resolution"]["identity"], "role-123",
                             "F's preserved requisition is reported, not nulled")

            # End to end: the very next coordinator packet carries the code and the flag.
            cycle(item, base + 2000)
            packet = item.adapter.decisions[-1]
            self.assertEqual(packet["recovery"]["attempt"], 2)
            self.assertEqual(packet["previous_findings"][-1]["error_code"], RETURNED)
            self.assertIs(packet["previous_findings"][-1]["resolution_verified"], False)
            self.assertEqual(packet["recovery"]["blocker"], RETURNED)

    def test_an_unknown_or_non_recoverable_returned_code_is_not_adopted(self):
        with fixture() as item:
            first_claim(item, dict(preserved_failure(code="DESKTOP_INVESTIGATION_EXPIRED"),
                                   reason="DESKTOP_INVESTIGATION_EXPIRED"))
            self.assertEqual(item.job()["blocker"], CODE, "only a RECOVERABLE code is adopted")

    def test_a_user_hold_always_wins_over_a_returned_code(self):
        self.assertFalse(application_recovery.RECOVERABLE & application_recovery.USER_HOLDS)
        with fixture() as item:
            result = dict(preserved_failure(),
                          blocked={"code": "AUTHENTICATION_REQUIRED",
                                   "next_action": "The owner must sign in before this application can be read"})
            first_claim(item, result)
            self.assertEqual(item.job()["blocker"], "AUTHENTICATION_REQUIRED")
            self.assertFalse(records(item, "APPLICATION_ABANDONED"))


class ResolvedGateTests(unittest.TestCase):
    """The `status == "RESOLVED"` gate: a preserved-but-rejected finding is reported, never adopted."""

    def test_preserved_unresolved_findings_never_reach_apply_resolution(self):
        with fixture() as item:
            with patch.object(application_recovery, "apply_resolution",
                              side_effect=AssertionError("apply_resolution must be unreachable")) as applied:
                first_claim(item, preserved_failure())
            applied.assert_not_called()
            self.assertFalse(records(item, "APPLICATION_RECOVERY_RESOLVED"))
            self.assertFalse(records(item, "APPLICATION_RECOVERY_RESOLUTION_REJECTED"))
            self.assertFalse(item.database.execute(
                "SELECT 1 FROM application_jobs WHERE job_key=? AND state='QUEUED'", (item.key,)).fetchone())
            # reported, not adopted
            self.assertEqual(application_recovery.finding_context(
                records(item, "APPLICATION_RECOVERY_INVESTIGATED")[-1])["resolution"]["identity"], "role-123")

    def test_a_resolved_result_without_bound_evidence_is_still_rejected_and_recorded(self):
        with fixture() as item:
            first_claim(item, dict(preserved_failure(), status="RESOLVED", resolution_verified=True,
                                   evidence={"source": "unbound", "verified": True}))
            rejected = records(item, "APPLICATION_RECOVERY_RESOLUTION_REJECTED")
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0]["reason"].startswith("UNVERIFIED_RESOLUTION:"))
            self.assertFalse(records(item, "APPLICATION_RECOVERY_RESOLVED"))
            self.assertNotEqual(item.job()["state"], "QUEUED")


class ReopenAllowanceTests(unittest.TestCase):
    """Patch H: an explicit, recorded, expiring, job-scoped allowance is actually consumed."""

    def test_reopened_job_gets_a_new_bounded_lifecycle_and_the_original_is_preserved(self):
        with fixture() as item:
            base = park(item)
            original = records(item, "APPLICATION_RECOVERY_STARTED")[0]
            preserved = rows(item)
            self.assertEqual(claims(item), 3)

            moment = reopen(item, base + 4000)
            self.assertEqual(rows(item)[:len(preserved)], preserved,
                             "the reopen only appends: every original row is byte-identical")

            reads = inspections(item)
            reinspect(item, base + 4100)
            self.assertGreater(inspections(item), reads,
                               "the reopened job made a real ordinary inspection first")
            starts = records(item, "APPLICATION_RECOVERY_STARTED")
            self.assertEqual(len(starts), 2, "a genuinely fresh lifecycle, not a reused one")
            self.assertEqual(starts[0], original, "the original lifecycle is untouched")
            fresh = starts[-1]
            self.assertEqual(fresh["max_attempts"], 3, "the ordinary cap, never raised")
            self.assertGreater(fresh["started_at"], moment)
            # usage is never erased: the spent claims and the parked hold stay in the ledger
            self.assertEqual(len(records(item, application_recovery.CLAIMS_EXHAUSTED_EVENT)), 1)
            self.assertEqual(claims(item), 3, "no fourth claim was minted by the reopen itself")
            self.assertFalse(records(item, "APPLICATION_ABANDONED"), "the job was not re-abandoned")

    def test_the_new_allowance_is_itself_three_claims_and_then_parks_again(self):
        with fixture() as item:
            base = park(item)
            reopen(item, base + 4000)
            moment = reinspect(item, base + 4100)
            for attempt in range(3):
                self.assertEqual(service(item), "investigate", "claim %d of the new allowance" % (attempt + 1))
                cycle(item, moment + 60)
                moment += 1000
                if attempt < 2:
                    cycle(item, moment)
            job = item.job()
            self.assertEqual(job["state"], "BLOCKED")
            self.assertEqual(job["blocker"], application_recovery.CLAIMS_EXHAUSTED_BLOCKER)
            self.assertEqual(claims(item), 6, "three original claims plus the three authorized ones")
            self.assertEqual(len(records(item, "APPLICATION_RECOVERY_STARTED")), 2,
                             "still exactly one lifecycle initializer per allowance")
            # Re-entry is refused again inside the new scope. The allowance is worth exactly
            # what the ordinary lifecycle is worth: three claims - never a fourth claim, and
            # never an unbounded loop. A parked job stays parked (the AI review of parked
            # holds is deleted, owner 2026-09-27).
            for step in range(24):
                cycle(item, moment + 6000 + step * 900)
                service(item)
            self.assertEqual(claims(item), 6, "no fourth claim in either allowance")
            self.assertFalse(records(item, application_recovery.HOLD_REVIEW_EVENT), "no hold review")
            self.assertEqual(item.job()["blocker"], application_recovery.CLAIMS_EXHAUSTED_BLOCKER)
            self.assertEqual(len(records(item, "APPLICATION_RECOVERY_STARTED")), 2)
            self.assertFalse(records(item, "APPLICATION_ABANDONED"))
            settled = (claims(item), len(records(item, application_recovery.HOLD_REVIEW_EVENT)),
                       len(records(item, "APPLICATION_RECOVERY_STARTED")),
                       len(records(item, "APPLICATION_RECOVERY_INVESTIGATED")))
            for step in range(24):
                cycle(item, moment + 40000 + step * 900)
                service(item)
            self.assertEqual((claims(item), len(records(item, application_recovery.HOLD_REVIEW_EVENT)),
                              len(records(item, "APPLICATION_RECOVERY_STARTED")),
                              len(records(item, "APPLICATION_RECOVERY_INVESTIGATED"))), settled,
                             "48 further scheduled cycles change nothing: bounded, no loop")

    def test_an_allowance_never_expires(self):
        """Owner, 2026-09-28: application recovery has no deadline, so a reopen grant stays in
        scope however much time passes, even one stored with an old expiry value."""
        with fixture() as item:
            base = park(item)
            at = reopen(item, base + 4000, expires=desktop_adapter.later(NOW, base + 4000 + 3600))
            later = desktop_adapter.later(at, 30 * 86400)
            self.assertIsNotNone(application_recovery.reopen_scope(item.database, item.key, at))
            self.assertIsNotNone(application_recovery.reopen_scope(item.database, item.key, later))

    def test_the_allowance_is_job_scoped_and_uses_no_cross_job_sql(self):
        with fixture() as item:
            base = park(item)
            at = reopen(item, base + 4000)
            self.assertIsNotNone(application_recovery.reopen_scope(item.database, item.key, at))
            self.assertIsNone(application_recovery.reopen_scope(item.database, "other employer:other-role", at))
            granted = json.loads(item.database.execute(
                "SELECT job_keys FROM application_reopen_grants WHERE authorization_id=?", (GRANT,)).fetchone()[0])
            self.assertEqual(granted, [item.key])

    def test_no_grant_table_means_every_read_is_exactly_the_unscoped_read(self):
        with fixture() as item:
            base = park(item)
            self.assertIsNone(application_recovery.reopen_scope(item.database, item.key, NOW))
            self.assertIsNone(application_recovery.reopen_scope(item.database, item.key, None))
            for event in ("APPLICATION_RECOVERY_STARTED", "APPLICATION_RECOVERY_INVESTIGATED",
                          "APPLICATION_RECOVERY_DECIDED", application_recovery.CLAIMS_EXHAUSTED_EVENT):
                self.assertEqual(application_recovery.scoped(item.database, item.key, event, None),
                                 application_recovery.records(item.database, item.key, event))
            self.assertEqual(claims(item), 3)
            self.assertTrue(base)

    def test_a_live_allowance_never_bypasses_a_user_hold_a_final_state_or_an_uncertain_submission(self):
        for blocker, state in (("AUTHENTICATION_REQUIRED", "BLOCKED"), (None, "ABANDONED"),
                               ("SUBMISSION_OUTCOME_UNKNOWN", "BLOCKED")):
            with self.subTest(blocker=blocker, state=state), fixture() as item:
                base = park(item)
                at = reopen(item, base + 4000)
                with item.database:
                    item.database.execute("UPDATE application_jobs SET state=?,blocker=? WHERE job_key=?",
                                          (state, blocker, item.key))
                before = rows(item)
                job = dict(item.database.execute("SELECT * FROM application_jobs WHERE job_key=?",
                                                 (item.key,)).fetchone(), candidate_email="candidate@example.invalid")
                started = application_recovery.recover(item.database, job, item.adapter, item.config,
                                                       blocker or CODE, "next", at)
                self.assertFalse(started, "recovery refuses the protected job even under a live allowance")
                self.assertEqual(rows(item), before)
                self.assertEqual(claims(item), 3)

    def test_a_compute_wait_under_a_live_allowance_consumes_nothing(self):
        with fixture() as item:
            base = park(item)
            at = reopen(item, base + 4000)
            before = rows(item)
            tables = {row[0] for row in item.database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            reservations = ([tuple(row) for row in item.database.execute(
                "SELECT * FROM worker_compute_reservations ORDER BY rowid")]
                if "worker_compute_reservations" in tables else None)
            job = dict(item.database.execute("SELECT * FROM application_jobs WHERE job_key=?",
                                             (item.key,)).fetchone(), candidate_email="candidate@example.invalid")

            class Waiting:
                root = item.root

                def recovery_decision(self, packet):
                    raise applications.AdapterPending("synthetic-capacity", desktop_adapter.later(at, 3600))

                def call(self, operation, payload):
                    raise AssertionError("no read is issued while compute is exhausted")

            application_recovery.recover(item.database, job, Waiting(), item.config, CODE, "next", at)
            self.assertEqual(claims(item), 3, "a compute wait is not a failed investigation")
            self.assertEqual(item.job()["attempts"], 0)
            if reservations is not None:
                self.assertEqual([tuple(row) for row in item.database.execute(
                    "SELECT * FROM worker_compute_reservations ORDER BY rowid")], reservations)
            self.assertEqual([row for row in rows(item) if row[2].startswith("APPLICATION_RECOVERY_INVESTIGATED")],
                             [row for row in before if row[2].startswith("APPLICATION_RECOVERY_INVESTIGATED")])


class MergeCoexistenceTests(unittest.TestCase):
    """The two additions must not have disturbed what A+B+D+I already proved."""

    def test_cap_exhaustion_with_an_ai_retry_is_a_parked_hold_not_an_abandonment(self):
        with fixture() as item:
            park(item)
            job = item.job()
            self.assertEqual(job["state"], "BLOCKED")
            self.assertEqual(job["blocker"], "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED")
            self.assertFalse(records(item, "APPLICATION_ABANDONED"))
            held = records(item, application_recovery.CLAIMS_EXHAUSTED_EVENT)
            self.assertEqual(len(held), 1)
            self.assertIs(held[0]["terminal"], False)
            self.assertEqual(held[0]["last_decision"]["action"], "RETRY")
            self.assertTrue(item.database.execute(
                "SELECT 1 FROM application_blockers WHERE job_key=? AND code=?",
                (item.key, application_recovery.CLAIMS_EXHAUSTED_BLOCKER)).fetchone())


if __name__ == "__main__":
    unittest.main()
