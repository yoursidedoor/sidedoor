import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import applications
import desktop_adapter
import profile_home
import resources


NOW = "2026-09-17T16:00:00+00:00"


class DesktopAdapterSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # The arm gate reads config.json; this fixture's own copy stands in for the bundled one.
        override = patch.object(resources, "OVERRIDE", self.root)
        override.start()
        self.addCleanup(override.stop)
        self.now = NOW
        self.config = {"application_adapter": "desktop", "applications_enabled": True,
                       "execution_ownership_verified": True}
        resume = b"synthetic resume; no real candidate data"
        (self.root / "resume.pdf").write_bytes(resume)
        self.policy = {
            "preapproved_applications": True, "max_attempts": 3,
            "max_reconcile_attempts": 3, "retry_delays_seconds": [900, 3600, 7200],
            "minimum_base_salary_usd": 190000, "funding_stages": ["Series B"],
            "industries": ["AI"], "levels": ["Staff Software Engineer"],
            "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
            "answers": {"email": {"semantic": "contact_email", "value": "candidate@example.invalid",
                                   "source": "synthetic test fixture"}},
        }
        self.write_config()
        profile_home.use(self, self.policy)
        self.database = self.connect()
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        with self.database:
            self.key = applications.ingest(self.database, {
                "company": "Synthetic Company", "role": "Staff Software Engineer",
                "url": "https://jobs.ashbyhq.com/synthetic/role-123", "requisition_id": "role-123",
            }, self.now)
        for target in ("socket.create_connection", "socket.socket.connect", "subprocess.run"):
            guard = patch(target, side_effect=AssertionError("No external actions in safety tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def connect(self):
        database = sqlite3.connect(self.root / "fixture.sqlite3", timeout=10)
        database.row_factory = sqlite3.Row
        return database

    def write_config(self):
        (self.root / "config.json").write_text(json.dumps(self.config))

    def job(self):
        job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone())
        job["candidate_email"] = self.policy["answers"]["email"]["value"]
        return job

    def review(self):
        evidence = {"observed_at": self.now, "verified": True, "source": "synthetic official evidence"}
        return dict(evidence, employer="synthetic company", requisition="role-123",
                    official_url=json.loads(self.job()["payload"])["url"], role=json.loads(self.job()["payload"])["role"],
                    candidate_email=self.job()["candidate_email"],
                    official_destination_verified=True, action_scope="application_only",
                    employee_count_evidence=dict(evidence, value="51-200"),
                    facts={"open": True, "funding_stage": "Series B", "industry": "AI",
                           "level": "Staff Software Engineer", "specialization": "backend",
                           "country": "US", "city": "Seattle", "work_mode": "remote",
                           "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                           "hiring_geography_includes_candidate": True,
                           "citizenship_clearance_requirements_met": True},
                    scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
                    compensation=dict(evidence, currency="USD", minimum=200000, maximum=250000, period="annual_base"),
                    history=dict(evidence, job_key=self.key, application=False, interview=False, referral="NONE"),
                    questions=[{"id": "email", "semantic": "contact_email", "required": True}],
                    all_required_questions_enumerated=True, form_revision="fixture-form-v1")

    def queue(self, operation, payload):
        with self.assertRaises(applications.AdapterPending) as pending:
            self.adapter.call(operation, payload)
        return pending.exception.request_id

    def prepare_submit(self):
        def inspect_or_queue(operation, payload):
            if operation == "inspect":
                return self.review()
            return self.adapter.call(operation, payload)
        provider = Mock(can_submit=True)
        provider.call.side_effect = inspect_or_queue
        applications.process(self.database, self.job(), provider, self.policy, self.root, self.now)
        request = self.database.execute("SELECT * FROM desktop_requests WHERE operation='submit'").fetchone()
        self.assertIsNotNone(request)
        return request["request_id"], json.loads(request["payload"])

    def claim(self, request_id=None):
        return desktop_adapter.claim(self.database, self.root, "synthetic-consumer", self.now, request_id=request_id)

    def arm(self, request):
        return desktop_adapter.arm(self.database, self.root,
                                   dict(request, observed_form_revision="fixture-form-v1"), self.now)

    def add_duplicate(self):
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("synthetic company", "role-123", "APPLICATION", "synthetic duplicate evidence"))

    def request_state(self, request_id):
        return self.database.execute("SELECT state FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0]

    def test_same_attempt_rejects_changed_payload_before_and_after_completion(self):
        request_id, payload = self.prepare_submit()
        mutations = {
            "answers": {"email": "different@example.invalid"}, "resume": str(self.root / "other.pdf"),
            "form_revision": "different-form", "idempotency_key": "different-key", "no_extra_answers": False,
        }
        for state in ("PENDING", "DONE"):
            for field, value in mutations.items():
                with self.subTest(state=state, field=field):
                    changed = copy.deepcopy(payload)
                    changed[field] = value
                    with self.assertRaisesRegex(ValueError, "SUBMISSION_ATTEMPT_PAYLOAD_CHANGED"):
                        self.adapter.call("submit", changed)
                    self.assertEqual(self.request_state(request_id), state)
            if state == "PENDING":
                request = self.claim(request_id)
                self.arm(request)
                desktop_adapter.complete(self.database, dict(request, result={"acknowledged": True}), self.now)
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0], 1)

    def test_identical_submit_replay_returns_ack_without_new_request(self):
        request_id, payload = self.prepare_submit()
        self.assertEqual(self.queue("submit", payload), request_id)
        request = self.claim(request_id)
        self.arm(request)
        desktop_adapter.complete(self.database, dict(request, result={"acknowledged": True}), self.now)
        self.now = desktop_adapter.later(self.now, 901)
        result = self.adapter.call("submit", payload)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["requires_independent_receipt"])
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests").fetchone()[0], 1)

    def test_simultaneous_consumers_claim_only_one_request(self):
        self.queue("inspect", {"job": self.job()})
        self.queue("verify", {"job": self.job()})
        ready = threading.Barrier(2)

        def consumer(owner):
            database = self.connect()
            try:
                ready.wait(timeout=5)
                return desktop_adapter.claim(database, self.root, owner, self.now)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consumer, owner) for owner in ("first", "second")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.database.execute("SELECT sum(claims) FROM desktop_requests").fetchone()[0], 1)

    def test_stale_worker_cannot_create_second_submission_attempt(self):
        stale_job = self.job()
        request_id, payload = self.prepare_submit()
        database = self.connect()
        self.addCleanup(database.close)
        adapter = desktop_adapter.DesktopAdapter(database, self.root, self.config, lambda: self.now)
        provider = Mock(can_submit=True)
        provider.call.side_effect = lambda operation, body: self.review() if operation == "inspect" else adapter.call(operation, body)
        applications.process(database, stale_job, provider, self.policy, self.root, self.now)
        counts = (database.execute("SELECT count(*) FROM application_attempts").fetchone()[0],
                  database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0])
        self.assertEqual(counts, (1, 1),
                         "A second worker's stale QUEUED snapshot must not overwrite SUBMITTING via READY")
        self.assertEqual(self.request_state(request_id), "PENDING")

    def test_simultaneous_arm_consumes_submit_intent_only_once(self):
        request_id, payload = self.prepare_submit()
        request = self.claim(request_id)
        ready = threading.Barrier(2)

        def consumer():
            database = self.connect()
            try:
                ready.wait(timeout=5)
                try:
                    return desktop_adapter.arm(database, self.root,
                                               dict(request, observed_form_revision="fixture-form-v1"), self.now)
                except ValueError as error:
                    return str(error)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consumer) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(isinstance(result, dict) and result.get("may_submit_once") is True
                             for result in results), 1)
        self.assertEqual(results.count("SUBMIT_INTENT_ALREADY_CONSUMED_OR_READ_ONLY"), 1)

    def test_unclaimed_submit_expiry_does_not_enqueue_replacement(self):
        request_id, payload = self.prepare_submit()
        self.now = desktop_adapter.later(self.now, desktop_adapter.REQUEST_SECONDS)
        with self.assertRaises(TimeoutError):
            self.adapter.call("submit", payload)
        self.assertEqual(self.adapter.call("submit", payload)["status"], "UNKNOWN")
        self.assertIsNone(self.claim(request_id))
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 1)

    # The pause and duplicate checks at claim time were deleted (owner, 2026-09-30): the
    # submission gate runs once, at arm, which the two *_prevents_arm tests below pin.

    def test_pause_after_claim_prevents_arm(self):
        request_id, payload = self.prepare_submit()
        request = self.claim(request_id)
        self.config["applications_enabled"] = False
        self.write_config()
        with self.assertRaisesRegex(ValueError, "DESKTOP_SUBMISSION_NOT_ENABLED"):
            self.arm(request)
        self.assertIsNone(self.database.execute("SELECT armed_at FROM desktop_requests").fetchone()[0])

    def test_duplicate_evidence_after_claim_prevents_arm(self):
        request_id, payload = self.prepare_submit()
        request = self.claim(request_id)
        self.add_duplicate()
        with self.assertRaisesRegex(ValueError, "DURABLE_SUBMISSION_CLAIM_INVALID"):
            self.arm(request)

    # test_arm_rechecks_history_age_after_claim was deleted on 2026-09-22. It pinned the
    # one-hour freshness window on the saved history block, which the owner removed from
    # both submission gates: an undated block meant "not fresh", so a read whose mailbox
    # search had failed was refused at the gate for having found nothing.

    def test_expired_submit_claim_is_never_reissued_even_without_arm(self):
        request_id, payload = self.prepare_submit()
        request = self.claim(request_id)
        self.now = request["lease_until"]
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            self.arm(request)
        self.assertIsNone(self.claim(request_id))
        self.assertEqual(self.request_state(request_id), "UNKNOWN")
        self.assertEqual(self.adapter.call("submit", payload)["status"], "UNKNOWN")
        self.assertEqual(self.database.execute("SELECT claims FROM desktop_requests").fetchone()[0], 1)

    def test_request_expiry_rejects_completion_at_exact_boundary(self):
        request_id = self.queue("inspect", {"job": self.job()})
        self.now = desktop_adapter.later(self.now, desktop_adapter.REQUEST_SECONDS - 1)
        request = self.claim(request_id)
        self.now = desktop_adapter.later(self.now, 1)
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)
        self.assertIsNone(self.claim(request_id))
        self.assertEqual(self.request_state(request_id), "EXPIRED")

    def test_reclaimed_read_rejects_old_token_and_duplicate_completion(self):
        request_id = self.queue("inspect", {"job": self.job()})
        first = self.claim(request_id)
        self.now = first["lease_until"]
        second = self.claim(request_id)
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            desktop_adapter.complete(self.database, dict(first, result=self.review()), self.now)
        desktop_adapter.complete(self.database, dict(second, result=self.review()), self.now)
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            desktop_adapter.complete(self.database, dict(second, result=self.review()), self.now)

    def test_absence_evidence_cannot_release_pending_submit_for_retry(self):
        request_id, payload = self.prepare_submit()
        verification_id = self.queue("verify", {"job": self.job()})
        verification = self.claim(verification_id)
        absence = {"status": "NOT_SUBMITTED", "source": "synthetic ATS record", "verified": True,
                   "observed_at": self.now, "job_key": self.key, "authoritative_absence": True,
                   "no_inflight": True, "evidence_kind": "authenticated_ats_application_record"}
        with self.assertRaisesRegex(ValueError, "SUBMISSION_STILL_INFLIGHT"):
            desktop_adapter.complete(self.database, dict(verification, result=absence), self.now)
        self.assertEqual(self.request_state(request_id), "PENDING")

    def test_expired_armed_submit_reconciles_without_another_submit(self):
        request_id, payload = self.prepare_submit()
        request = self.claim(request_id)
        self.arm(request)
        self.now = request["lease_until"]
        applications.process(self.database, self.job(), self.adapter, self.policy, self.root, self.now)
        self.assertEqual(self.request_state(request_id), "UNKNOWN")
        verification = self.claim()
        self.assertEqual(verification["payload"]["operation"], "reconcile")
        self.assertEqual(self.database.execute("SELECT count(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED"):
            desktop_adapter.complete(self.database, dict(request, result={"acknowledged": True}), self.now)


if __name__ == "__main__":
    unittest.main()
