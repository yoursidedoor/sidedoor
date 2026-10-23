"""A failed investigate must report what the browser actually returned.

Production defect: consume()'s exception handler replaced the browser AI's whole
investigation with {identity: None, destination: None, history: []}, so the AI
coordinator read "null identity and empty history" and abandoned jobs whose
identity had in fact been read correctly. These checks are synthetic and
isolated: no network, no browser, no production database, no side effects.

The suite needs a failed investigation that still carries real findings, so the
fixture packet must be rejected by deterministic validation on purpose. That
deliberate rejection trigger is `official_destination_verified: False` — see
`identity_result` for why, and for the trigger this replaced.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import tempfile
import unittest

import application_browser as browser
import application_destination
import application_recovery
import applications
import desktop_adapter
import manual_application_browser as manual_browser
import manual_browser_recovery_slice as manual_slice


def observation(result):
    return "SYNTHETIC BROWSER OBSERVATION ONLY\n" + browser.encoded(result)


class FailedInvestigationPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="browser-preservation-")
        self.addCleanup(temporary.cleanup)
        self.fixture = manual_slice.RecoveryFixture(Path(temporary.name) / "fixture")
        self.addCleanup(self.fixture.database.close)
        self.now = applications.stamp()
        self.recovery = {"attempt": 1, "max_attempts": 3, "started_at": self.now,
                         "deadline": desktop_adapter.later(self.now, 3600),
                         "previous_failure": {"reason": "PREFLIGHT_TRANSIENT"},
                         "supported_alternative": {"method": "official_posting",
                                                   "instructions": "Read the official posting identity"}}
        self.packet = {"job": self.fixture.job(), "recovery": self.recovery}

    def queue(self):
        try:
            self.fixture.adapter.call("investigate", self.packet)
        except applications.AdapterPending as pending:
            return pending.request_id
        raise AssertionError("Expected a durable investigate mailbox request")

    def run_investigate(self, result, events=None, mutate=None):
        request_id = self.queue()

        def executor(config, prompt, directory, timeout):
            request = json.loads((directory / "request.json").read_text())
            manual_browser.write_output(directory, request, result, events, mutate)

        summary = browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                  request_id, executor)
        stored = self.fixture.database.execute(
            "SELECT state,claims,result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        return request_id, summary, dict(stored)

    def identity_result(self, official_destination_verified=False):
        """A real investigation the deterministic gate rejects for one deliberate reason.

        The browser read the canonical posting and reported everything correctly —
        employer, role, requisition role-123, the application link it reached and a
        comparison against a prior process — but it honestly did NOT attest that the
        link it recorded is the employer's own official application destination, so
        it returned `official_destination_verified: False`.
        `application_destination.validate_identity` requires that attestation, so the
        whole packet is refused with APPLICATION_OFFICIAL_IDENTITY_REQUIRED (the exact
        production code) while still containing findings the coordinator needs. That is
        the situation this suite exists for: the findings must be reported, not erased.

        Why this trigger and not the original one. This fixture used to force the
        rejection with an employer differing from the stored one only in letter case
        (`employer="Synthetic Company"` against the folded `synthetic company`). That
        was the production sub-cause at the time, but the employer/role identity fix in
        `application_destination.py` deliberately folds case, unicode form, whitespace
        and title punctuation, so that packet is now valid and the fixture silently
        stopped rejecting where it meant to. The employer is still the realistic
        page-cased form here — it must not be pre-folded to the stored key — but it is
        no longer the thing being rejected.

        A withheld attestation is not a spelling variant, so no normalization or
        reconciliation fix can make it pass: honouring it would mean accepting an
        unverified official destination, which the project's rules forbid outright.
        `test_failed_investigate_preserves_real_identity_destination_and_history`
        pins this by asserting that the same evidence is accepted once, and only once,
        the attestation is present.
        """
        evidence = dict(manual_slice.identity(self.fixture), employer="Synthetic Company",
                        official_destination_verified=official_destination_verified)
        # The browser's own report of a prior application, preserved verbatim. It is
        # raw model output: the employer reads as it appeared on the page, and nothing
        # here is folded to a stored key to make a later gate pass.
        history = {"evidence_id": "gmail:synthetic-prior-999", "relationship": "DISTINCT_ROLE",
                   "evidence": {"verified": True, "evidence_id": "gmail:synthetic-prior-999",
                                "job_key": self.fixture.key, "employer": "Synthetic Company",
                                "candidate_email": self.fixture.job()["candidate_email"],
                                "current_requisition": "role-123", "prior_requisition": "prior-999",
                                "comparison": "Prior process prior-999 is distinct from current role-123"}}
        return {"status": "RESOLVED", "reason": "Observed the official canonical posting identity",
                "next_action": "Reconcile the canonical requisition and inspect the current form",
                "suggested_alternatives": ["Re-read the observed official posting link"],
                "resolution": {"identity": {"requisition": "role-123", "evidence": evidence},
                               "destination": {"url": evidence["official_url"], "evidence": evidence},
                               "history": [history]}}

    def test_failed_investigate_preserves_real_identity_destination_and_history(self):
        reported = self.identity_result()
        request_id, summary, stored = self.run_investigate(reported)
        self.assertEqual(summary["state"], "FAILED")
        self.assertEqual(summary["code"], "APPLICATION_OFFICIAL_IDENTITY_REQUIRED")
        self.assertTrue(summary["failure_committed"])
        result = json.loads(stored["result"])

        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertIs(result["resolution_verified"], False)
        self.assertEqual(result["reported_investigation"]["origin"], "unverified_model_result")
        self.assertIs(result["reported_investigation"]["resolution_reported"], True)

        identity = result["resolution"]["identity"]
        self.assertEqual(identity["requisition"], "role-123")
        self.assertEqual(identity["evidence"]["employer"], "Synthetic Company")
        self.assertIs(identity["evidence"]["official_destination_verified"], False)
        self.assertEqual(identity["evidence"]["official_url"],
                         "https://careers.synthetic.example/application?job=role-123")
        self.assertEqual(result["resolution"]["destination"]["url"],
                         "https://careers.synthetic.example/application?job=role-123")
        self.assertEqual([entry["evidence_id"] for entry in result["resolution"]["history"]],
                         ["gmail:synthetic-prior-999"])
        self.assertEqual(result["reported_investigation"]["reported_status"], "RESOLVED")
        self.assertEqual(result["reported_investigation"]["suggested_alternatives"],
                         ["Re-read the observed official posting link"])
        self.assertTrue(result["reported_investigation"]["reported_evidence"])

        replayed = self.fixture.adapter.call("investigate", self.packet)
        self.assertEqual(replayed, result)
        finding = {"attempt": 1, "at": self.now, "actual_reads": 1,
                   "supported_alternative": self.recovery["supported_alternative"],
                   "reason": replayed["reason"], "result": replayed}
        coordinator = application_recovery.finding_context(finding)
        self.assertEqual(coordinator["resolution"]["identity"], "role-123")
        self.assertEqual(coordinator["resolution"]["history"],
                         [{"evidence_id": "gmail:synthetic-prior-999", "relationship": "DISTINCT_ROLE"}])
        self.assertEqual(coordinator["status"], "UNRESOLVED")

        # The fixture's rejection is the one it intends and nothing else: this exact
        # evidence is refused while the official-destination attestation is withheld
        # and accepted the moment it is present. Without this guard the suite could
        # drift again — an unrelated fix could make the packet valid, the failure could
        # start coming from a different gate, and the preservation assertions above
        # would still pass while testing something nobody chose.
        now = applications.stamp()
        observed = dict(identity["evidence"], source="application-browser:synthetic-guard", observed_at=now)
        with self.assertRaises(ValueError) as refused:
            application_destination.validate_identity(dict(observed), self.fixture.job(), now)
        self.assertEqual(str(refused.exception), "APPLICATION_OFFICIAL_IDENTITY_REQUIRED")
        self.assertEqual(application_destination.validate_identity(
            dict(observed, official_destination_verified=True), self.fixture.job(), now),
            "https://careers.synthetic.example/application?job=role-123")

        self.assertEqual(self.fixture.job()["requisition"], "role-123")
        self.assertEqual(self.fixture.database.execute(
            "SELECT count(*) FROM application_attempts").fetchone()[0], 0)
        self.assertIsNone(self.fixture.database.execute(
            "SELECT armed_at FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])

    def test_genuinely_null_provider_resolution_is_recorded_as_null(self):
        reported = {"status": "RESOLVED", "reason": "No official identity could be observed",
                    "next_action": "Choose another supported read",
                    "resolution": {"identity": None, "destination": None, "history": []}}
        _, summary, stored = self.run_investigate(reported)
        self.assertEqual(summary["code"], "APPLICATION_INVESTIGATION_EMPTY_RESOLUTION")
        result = json.loads(stored["result"])
        self.assertEqual(result["resolution"], {"identity": None, "destination": None, "history": []})
        self.assertEqual(result["reported_investigation"]["origin"], "unverified_model_result")
        self.assertIs(result["reported_investigation"]["resolution_reported"], True)
        self.assertEqual(result["reported_investigation"]["reported_reason"],
                         "No official identity could be observed")

    def test_absent_browser_output_is_not_reported_as_a_null_finding(self):
        request_id = self.queue()

        def executor(config, prompt, directory, timeout):
            (directory / "trace.jsonl").write_text("")
            raise TimeoutError("APPLICATION_BROWSER_TIMEOUT")

        summary = browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                  request_id, executor)
        self.assertTrue(summary["failure_committed"])
        result = json.loads(self.fixture.database.execute(
            "SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        self.assertEqual(result["resolution"], {"identity": None, "destination": None, "history": []})
        self.assertEqual(result["reported_investigation"]["origin"], "absent")
        self.assertIs(result["reported_investigation"]["resolution_reported"], False)
        self.assertNotIn("reported_status", result["reported_investigation"])

    def test_error_code_is_available_downstream_and_is_recoverable(self):
        _, summary, stored = self.run_investigate(self.identity_result())
        result = json.loads(stored["result"])
        self.assertEqual(result["error"]["code"], "APPLICATION_OFFICIAL_IDENTITY_REQUIRED")
        self.assertIn(result["error"]["code"], application_recovery.RECOVERABLE)
        self.assertEqual(application_recovery.failure_code(result["error"]["code"]),
                         "APPLICATION_OFFICIAL_IDENTITY_REQUIRED")
        self.assertIn("APPLICATION_OFFICIAL_IDENTITY_REQUIRED", result["next_action"])
        failures = application_recovery.previous_execution_failures(
            self.fixture.database, self.fixture.key, self.fixture.root.resolve())
        self.assertEqual([entry["code"] for entry in failures],
                         ["APPLICATION_OFFICIAL_IDENTITY_REQUIRED"])
        self.assertEqual(failures[0]["operation"], "investigate")
        self.assertTrue(failures[0]["artifacts"]["trace"].startswith("reports/application-browser/request-"))

    def test_failed_investigation_consumes_exactly_one_claim(self):
        request_id, summary, stored = self.run_investigate(self.identity_result())
        self.assertEqual(stored["claims"], 1)
        self.assertEqual(stored["state"], "DONE")
        self.assertTrue(summary["failure_committed"])
        self.assertEqual(self.fixture.database.execute(
            "SELECT COALESCE(SUM(claims),0) FROM desktop_requests WHERE operation='investigate'").fetchone()[0], 1)
        self.assertIsNone(desktop_adapter.claim(self.fixture.database, self.fixture.root,
                                                "second-consumer", request_id=request_id))
        self.assertEqual(browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                         request_id, lambda *args: None)["state"], "NOT_CLAIMED")
        self.assertEqual(self.fixture.database.execute(
            "SELECT COALESCE(SUM(claims),0) FROM desktop_requests WHERE operation='investigate'").fetchone()[0], 1)
        lifecycle = {"started_at": self.now, "blocker": "PREFLIGHT_TRANSIENT",
                     "deadline": self.recovery["deadline"], "max_attempts": 3}
        self.assertEqual(application_recovery.actual_reads(
            self.fixture.database, self.fixture.key, self.packet, self.fixture.adapter), 1)
        self.assertEqual(application_recovery.read_count(
            self.fixture.database, self.fixture.key, lifecycle, []), 1)


if __name__ == "__main__":
    unittest.main()
