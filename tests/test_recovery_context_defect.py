"""Patch BA: an investigation the orchestrator could not issue is not evidence.

Stark (2026-09-19): by the third recovery claim the recovery context repeated every earlier
finding with the investigating AI's verbose report, outgrew the desktop request cap, and the
request was refused with APPLICATION_RECOVERY_CONTEXT_INVALID before any read. The failure was
recorded as an investigation and the coordinator abandoned on it.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import unittest
from unittest.mock import patch

import application_recovery
import applications
import desktop_adapter
import test_applications_e2e as fixtures


DECISION = {"action": "RETRY", "method": "official_posting", "progress": False,
            "instructions": "Read the official posting once, read-only", "reason": "fixture"}


def oversized_recovery():
    report = {"origin": "unverified_model_result", "reported_status": "RESOLVED",
              "reported_reason": "fixture", "reported_evidence": [{"quote": "x" * 1500}] * 4}
    finding = {"attempt": 1, "at": fixtures.NOW, "actual_reads": 1, "reason": "fixture", "status": "UNRESOLVED",
               "supported_alternative": {"method": "observed_links", "instructions": "i" * 900},
               "reported_investigation": report}
    return {"blocker": "REQUISITION_MISMATCH", "started_at": fixtures.NOW, "deadline": fixtures.NOW,
            "max_attempts": 3, "attempt": 3, "previous_failure": finding, "previous_attempts": [finding, finding],
            "previous_decisions": [{"attempt": 1, "decision": dict(DECISION, artifacts={"raw": "r" * 500})}],
            "previous_execution_failures": [], "coordinator_failures": [],
            "supported_alternative": {"method": "official_posting", "instructions": DECISION["instructions"]}}


class RecoveryContextDefectTests(unittest.TestCase):
    setUpBase = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover

    def setUp(self):
        self.setUpBase()
        self.config = {"application_adapter": "desktop", "applications_enabled": True,
                       "execution_ownership_verified": True,
                       "application_recovery": {"enabled": True, "max_attempts": 3, "deadline_hours": 48}}
        self.now = fixtures.NOW
        self.desktop = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        key = self.discover()
        self.job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                        candidate_email=self.policy["answers"]["email"]["value"])

    def test_oversized_context_is_compacted_not_refused(self):
        recovery = oversized_recovery()
        self.assertGreater(len(desktop_adapter.encode(recovery)), 16384)
        body = self.desktop.request_body("investigate", {"job": self.job, "recovery": recovery})
        sent = body["recovery"]
        self.assertLessEqual(len(desktop_adapter.encode(sent)), 16384)
        for field in ("started_at", "attempt", "blocker", "supported_alternative"):
            self.assertEqual(sent[field], recovery[field])
        small = {"blocker": "REQUISITION_MISMATCH", "started_at": fixtures.NOW, "attempt": 1}
        self.assertEqual(self.desktop.request_body("investigate", {"job": self.job, "recovery": small})["recovery"], small)

    def test_unissued_investigation_is_a_defect_hold_not_a_finding(self):
        original = self.desktop.request_body

        def refused(operation, payload):
            if operation == "investigate":
                raise ValueError("APPLICATION_RECOVERY_CONTEXT_INVALID")
            return original(operation, payload)

        with self.database:
            applications.hold(self.database, self.job, "REQUISITION_MISMATCH", "Resolve the requisition", self.now)
        self.desktop.recovery_decision = lambda packet: dict(DECISION)
        with patch.object(self.desktop, "request_body", side_effect=refused):
            self.assertTrue(application_recovery.recover(self.database, self.job, self.desktop, self.config,
                                                         "REQUISITION_MISMATCH", "Resolve the requisition", self.now))
            key = self.job["job_key"]
            self.assertEqual(application_recovery.records(self.database, key, "APPLICATION_RECOVERY_INVESTIGATED"), [])
            self.assertEqual(len(application_recovery.records(self.database, key, application_recovery.ORCHESTRATOR_DEFECT_EVENT)), 1)
            row = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
            self.assertEqual((row["state"], row["blocker"]), ("BLOCKED", application_recovery.ORCHESTRATOR_DEFECT_BLOCKER))
            # Past the deadline, while the defect persists: parked, no coordinator call, no abandonment.
            self.desktop.recovery_decision = lambda packet: self.fail("coordinator must not be asked")
            late = (dt.datetime.fromisoformat(self.now) + dt.timedelta(hours=49)).isoformat()
            parked = dict(row, candidate_email=self.job["candidate_email"])
            self.assertTrue(application_recovery.recover(self.database, parked, self.desktop, self.config,
                                                         "REQUISITION_MISMATCH", "Resolve the requisition", late))
            self.assertEqual(self.database.execute("SELECT state FROM application_jobs WHERE job_key=?", (key,)).fetchone()[0], "BLOCKED")
        # Defect fixed: the same prepared AI decision is issued as a real read-only request.
        application_recovery.recover(self.database, parked, self.desktop, self.config,
                                     "REQUISITION_MISMATCH", "Resolve the requisition", self.now)
        request = self.database.execute("SELECT payload FROM desktop_requests WHERE job_key=? AND operation='investigate'", (key,)).fetchone()
        self.assertIsNotNone(request)
        self.assertEqual(json.loads(request[0])["recovery"]["supported_alternative"]["method"], "official_posting")


if __name__ == "__main__":
    unittest.main()
