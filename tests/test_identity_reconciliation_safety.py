import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import applications
import external_history
import identity_reconciliation


NOW = "2026-09-17T16:00:00+00:00"
# Synthetic stand-in for reports/role-identity-evidence.json (not carried in the repo).
SAVED_EVIDENCE = Path(__file__).resolve().parent / "fixtures/role-identity-evidence.json"


class BeforeIdentityTransaction:
    def __init__(self, database, mutate):
        self.database = database
        self.mutate = mutate
        self.injected = False

    def __getattr__(self, name):
        return getattr(self.database, name)

    def execute(self, statement, parameters=()):
        if statement.startswith(("SAVEPOINT", "BEGIN")) and not self.injected and not self.database.in_transaction:
            self.injected = True
            self.mutate()
        return self.database.execute(statement, parameters)


class IdentityReconciliationSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "synthetic.sqlite3"
        self.database = self.connect()
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        self.role = {"company": "Synthetic Company", "role": "Staff Platform Engineer",
                     "url": "https://careers.example.invalid/roles/canonical-123", "requisition_id": "",
                     "rationale": "Synthetic identity test only"}
        with self.database:
            self.key = applications.ingest(self.database, self.role, NOW)
        self.proof = {"job_key": self.key, "employer": "synthetic company", "role": self.role["role"],
                      "source": self.role["url"], "requisition": "canonical-123", "verified": True,
                      "observed_at": NOW, "independent_read": True,
                      "identity_basis": "official_canonical_posting_id"}
        for target in ("socket.create_connection", "socket.socket.connect", "urllib.request.urlopen", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("Identity review has no external access"))
            guard.start()
            self.addCleanup(guard.stop)

    def connect(self):
        database = sqlite3.connect(self.path, timeout=2)
        database.row_factory = sqlite3.Row
        return database

    def row(self):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone())

    def reconcile(self, proof=None, database=None):
        return identity_reconciliation.reconcile_requisition(
            database or self.database, self.key, "canonical-123", self.proof if proof is None else proof, NOW)

    def mutate_other(self, mutate):
        other = self.connect()
        try:
            with other:
                mutate(other)
        finally:
            other.close()

    def run_race(self, mutate):
        connection = BeforeIdentityTransaction(self.database, lambda: self.mutate_other(mutate))
        rejected = False
        try:
            self.reconcile(database=connection)
        except ValueError:
            rejected = True
        self.assertTrue(connection.injected, "Fixture must exercise a competing commit before identity transaction")
        return rejected

    def test_saved_resolved_proof_updates_only_a_synthetic_copy_with_same_old_key(self):
        saved = json.loads(SAVED_EVIDENCE.read_text())
        proof = saved["resolved"]
        role = {"company": proof["employer"], "role": proof["role"], "url": proof["source"]}
        with self.database:
            key = applications.ingest(self.database, role, proof["observed_at"])
        self.assertEqual(key, proof["job_key"])
        result = identity_reconciliation.reconcile_requisition(
            self.database, key, proof["requisition"], proof, proof["observed_at"])
        self.assertEqual(result["job_key"], key)
        with self.database:
            self.assertEqual(applications.ingest(self.database, dict(role, requisition_id=proof["requisition"]), NOW), key)
        self.assertFalse(proof["submission_authorized_by_this_record"])
        self.assertIn("Complete application/referral history", proof["unknowns"])
        self.assertEqual(self.database.execute("SELECT state FROM application_jobs WHERE job_key=?", (key,)).fetchone()[0], "QUEUED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)

    def test_saved_unresolved_unknown_is_not_identity_or_absence_evidence(self):
        saved = json.loads(SAVED_EVIDENCE.read_text())["unresolved"]
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile(proof=saved)
        self.assertFalse(saved["absence_proven"])
        self.assertEqual(list(self.database.iterdump()), before)

    def test_independent_proof_fields_and_freshness_are_bound_to_saved_role(self):
        for change in ({"employer": "another company"}, {"role": "Another Role"},
                       {"source": self.role["url"] + "/other"}, {"requisition": "another-id"},
                       {"identity_basis": "guessed_url_slug"}, {"independent_read": False},
                       {"verified": False}, {"observed_at": "2026-09-16T15:59:59+00:00"},
                       {"observed_at": "2026-09-17T16:01:01+00:00"}):
            with self.subTest(change=change):
                before = list(self.database.iterdump())
                with self.assertRaises(ValueError):
                    self.reconcile(dict(self.proof, **change))
                self.assertEqual(list(self.database.iterdump()), before)

    def test_explicit_proof_job_key_mismatch_is_rejected(self):
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile(dict(self.proof, job_key="another-company:another-job"))
        self.assertEqual(list(self.database.iterdump()), before)

    def test_official_source_requires_an_https_authority(self):
        role = dict(self.role, url="https:///roles/canonical-123")
        with self.database:
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(role), self.key))
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile(dict(self.proof, source=role["url"]))
        self.assertEqual(list(self.database.iterdump()), before)

    def test_old_job_and_dependent_record_ids_survive_without_eligibility_clearance(self):
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='ELIGIBILITY_UNKNOWN',next_action='Verify eligibility' WHERE job_key=?", (self.key,))
            self.database.execute("INSERT INTO application_blockers VALUES (?, 'ELIGIBILITY_UNKNOWN', 'orchestrator', 'Saved unknown facts', 0, NULL, ?)", (self.key, NOW))
            self.database.execute("INSERT INTO application_answers VALUES (?, 'email', 'contact_email', ?, 'synthetic source')", (self.key, json.dumps("candidate@example.invalid")))
            self.database.execute("INSERT INTO application_reviews VALUES ('saved-review',?,?,'ELIGIBILITY_UNKNOWN','{}')", (self.key, NOW))
            self.database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES (?, 'synthetic saved blocker', 'PENDING', ?)", (self.key + ":blocker", NOW))
        tables = ("application_blockers", "application_answers", "application_reviews", "notification_outbox")
        before = {table: [tuple(row) for row in self.database.execute("SELECT * FROM " + table)] for table in tables}
        original = self.row()
        result = self.reconcile()
        for field in ("job_key", "state", "attempts", "blocker", "next_action", "receipt", "reconcile_attempts"):
            self.assertEqual(self.row()[field], original[field])
        for table in tables:
            self.assertEqual([tuple(row) for row in self.database.execute("SELECT * FROM " + table)], before[table])
        self.assertEqual(result["job_key"], self.key)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
        detail = json.loads(self.database.execute("SELECT detail FROM application_events WHERE event='REQUISITION_RECONCILED'").fetchone()[0])
        self.assertFalse(detail["eligibility_and_history_clearance"])

    def test_existing_positive_history_under_old_requisition_still_blocks(self):
        original = self.row()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,'INTERVIEW','synthetic existing process')",
                                  (original["employer"], original["requisition"]))
        self.assertEqual(applications.local_gate(self.database, original, NOW)[0], "DUPLICATE")
        self.reconcile()
        self.assertEqual(self.row()["requisition"], "canonical-123")
        gate = applications.local_gate(self.database, self.row(), NOW)
        self.assertIsNotNone(gate, "Identity-only reconciliation removed an existing history gate")
        self.assertEqual(gate[0], "DUPLICATE")

    def test_rediscovery_after_restart_preserves_original_requisition_history_gate(self):
        original = self.row()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,'APPLICATION','synthetic saved application')",
                                  (original["employer"], original["requisition"]))
        self.reconcile()
        restarted = self.connect()
        self.addCleanup(restarted.close)
        with restarted:
            key = applications.ingest(restarted, dict(self.role, requisition_id="canonical-123"), NOW)
        self.assertEqual(key, self.key)
        current = dict(restarted.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())
        self.assertEqual(current["requisition"], "canonical-123")
        self.assertEqual(applications.local_gate(restarted, current, NOW)[0], "DUPLICATE")
        self.assertEqual(restarted.execute("SELECT requisition FROM application_history").fetchone()[0], original["requisition"])
        self.assertEqual(restarted.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)

    def test_employer_history_without_requisition_remains_unresolved(self):
        with self.database:
            self.database.execute("INSERT INTO application_external_history VALUES ('synthetic-history', 'synthetic company', 'INTERVIEW', NULL, 'Saved role', ?, ?, '{}', 'synthetic-hash')", (NOW, NOW))
        self.reconcile()
        # Requisition-less employer history no longer holds the job (owner, 2026-10-06: "we should
        # never block employers unless the jobs are the same"); reconciliation still invents no
        # resolution for it.
        self.assertIsNone(external_history.gate(self.database, self.row()))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_history_resolutions").fetchone()[0], 0)

    def test_terminal_claimed_and_uncertain_states_are_never_rebound(self):
        for state in sorted(applications.FINAL | applications.UNCERTAIN | {"CLOSED", "CLAIMED", "SUBMISSION_UNKNOWN", "REFERRAL_HOLD"}):
            with self.subTest(state=state):
                with self.database:
                    self.database.execute("UPDATE application_jobs SET state=? WHERE job_key=?", (state, self.key))
                before = list(self.database.iterdump())
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(list(self.database.iterdump()), before)

    def test_unknown_submission_blocker_is_not_cleared_by_blocked_zero_attempt_state(self):
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='SUBMISSION_OUTCOME_UNKNOWN' WHERE job_key=?", (self.key,))
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(list(self.database.iterdump()), before)

    def test_durable_attempt_blocks_even_if_counter_was_not_updated(self):
        with self.database:
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) VALUES ('saved-attempt',?,?,'SUBMITTING',?)", (self.key, self.key, NOW))
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(list(self.database.iterdump()), before)

    def test_same_identity_replay_preserves_terminal_receipt_and_audit(self):
        self.reconcile()
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='SUBMITTED',receipt='synthetic saved receipt',attempts=1 WHERE job_key=?", (self.key,))
        before = list(self.database.iterdump())
        self.assertEqual(self.reconcile()["state"], "ALREADY_RECONCILED")
        self.assertEqual(list(self.database.iterdump()), before)

    def test_preexisting_conflicting_alias_is_rejected_without_changes(self):
        with self.database:
            foreign = applications.ingest(self.database, dict(self.role, company="Other Company"), NOW)
            resolved_key = applications.identity(dict(self.role, requisition_id="canonical-123"))[2]
            self.database.execute("INSERT INTO application_aliases VALUES (?,?,'synthetic other identity')", (resolved_key, foreign))
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(list(self.database.iterdump()), before)

    def test_changed_payload_race_cannot_overwrite_newer_role_with_stale_proof(self):
        newer = dict(self.role, role="Different Platform Role", url="https://careers.example.invalid/other-role")
        rejected = self.run_race(lambda other: other.execute(
            "UPDATE application_jobs SET payload=?,updated_at=? WHERE job_key=?",
            (json.dumps(newer), "2026-09-17T16:00:01+00:00", self.key)))
        self.assertEqual(json.loads(self.row()["payload"]), newer, "Stale identity snapshot overwrote a newer role")
        self.assertTrue(rejected)

    def test_alias_collision_race_does_not_reconcile_into_other_tasks_alias(self):
        with self.database:
            foreign = applications.ingest(self.database, dict(self.role, company="Other Company"), NOW)
        original = self.row()
        resolved_key = applications.identity(dict(self.role, requisition_id="canonical-123"))[2]
        rejected = self.run_race(lambda other: other.execute(
            "INSERT INTO application_aliases VALUES (?,?,'concurrent synthetic identity')", (resolved_key, foreign)))
        self.assertEqual(self.row(), original, "Conflicting concurrent alias was ignored while changing the job")
        self.assertTrue(rejected)

    def test_resolved_job_collision_race_cannot_create_two_jobs_for_one_requisition(self):
        original = self.row()
        rejected = self.run_race(lambda other: applications.ingest(
            other, dict(self.role, requisition_id="canonical-123"), NOW))
        self.assertEqual(self.row(), original, "A concurrently created resolved job was missed by collision checks")
        self.assertTrue(rejected)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs WHERE requisition='canonical-123'").fetchone()[0], 1)

    def test_concurrent_terminal_transition_is_preserved_and_alias_rolls_back(self):
        rejected = self.run_race(lambda other: other.execute(
            "UPDATE application_jobs SET state='SUBMITTED',receipt='synthetic concurrent receipt' WHERE job_key=?", (self.key,)))
        self.assertTrue(rejected)
        self.assertEqual(self.row()["state"], "SUBMITTED")
        self.assertEqual(self.row()["receipt"], "synthetic concurrent receipt")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)

    def test_concurrent_attempt_is_preserved_and_reconciliation_rejected(self):
        rejected = self.run_race(lambda other: other.execute(
            "INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) VALUES ('concurrent-attempt',?,?,'SUBMITTING',?)",
            (self.key, self.key, NOW)))
        self.assertTrue(rejected)
        self.assertTrue(self.row()["requisition"].startswith("unresolved:"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)

    def test_event_persistence_failure_rolls_back_identity_and_alias(self):
        self.database.execute("""CREATE TRIGGER reject_identity_event BEFORE INSERT ON application_events
            WHEN NEW.event='REQUISITION_RECONCILED' BEGIN SELECT RAISE(FAIL, 'synthetic audit failure'); END""")
        before = list(self.database.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.reconcile()
        self.assertEqual(list(self.database.iterdump()), before)

    def test_outer_transaction_rollback_preserves_original_identity(self):
        before = list(self.database.iterdump())
        with self.assertRaisesRegex(RuntimeError, "synthetic caller failure"):
            with self.database:
                self.database.execute("UPDATE application_jobs SET blocker='synthetic pending mutation' WHERE job_key=?", (self.key,))
                self.reconcile()
                raise RuntimeError("synthetic caller failure")
        self.assertEqual(list(self.database.iterdump()), before)


class RehearsalProofSelectionTests(unittest.TestCase):
    """tools/rehearse_migration.py names no employer: it takes the evidence file's resolved proof."""

    def test_resolved_proof_prefers_resolved_then_any_entry_with_a_key_and_requisition(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "rehearse_migration.py"
        spec = importlib.util.spec_from_file_location("rehearse_migration", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        saved = json.loads(SAVED_EVIDENCE.read_text())
        self.assertEqual(tool.resolved_proof(saved), saved["resolved"])
        other = {"job_key": "any co:1", "requisition": "1"}
        self.assertEqual(tool.resolved_proof({"note": "x", "some employer": other}), other)
        with self.assertRaises(AssertionError):
            tool.resolved_proof({"unresolved": {"job_key": "any co:unresolved:0"}})


if __name__ == "__main__":
    unittest.main()
