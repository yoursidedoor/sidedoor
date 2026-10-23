"""Identity comparison of employer and role title in `application_destination`.

Offline only: no browser, no network, no production database, no service. The replay
cases read synthetic request/output/trace fixtures under `tests/fixtures/application-browser/`
(shaped like saved investigations: page-cased employers, re-punctuated titles) and drive the
unmodified `application_browser.normalize`.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import json
from pathlib import Path
import sqlite3
import unittest

import application_browser as browser
import application_destination
import applications
import outreach_history_browser
import reply_worker

ROOT = Path(application_destination.__file__).parent
SAVED = Path(__file__).resolve().parent / "fixtures" / "application-browser"
EMAIL = "alex.example@example.com"
STORED_ROLE = "Staff Software Engineer — Developer Platform"
OBSERVED_ROLE = "Staff Software Engineer, Developer Platform"
POSTING = "https://job-boards.greenhouse.io/umbrella/jobs/9000000001"

# Six investigations of the kind once rejected with APPLICATION_OFFICIAL_IDENTITY_REQUIRED on
# employer casing and/or role punctuation alone.
REPLAYED = (("request-fixt0011", "umbrella:9000000001 attempt 1"),
            ("request-fixt0014", "umbrella:9000000001 attempt 2"),
            ("request-fixt0013", "weyland ai attempt 1"),
            ("request-fixt0012", "weyland ai attempt 2"),
            ("request-fixt0022", "massive dynamic attempt 1"),
            ("request-fixt0020", "massive dynamic attempt 2"))


def job(**changes):
    payload = {"company": "Umbrella", "role": STORED_ROLE, "url": POSTING,
               "requisition_id": "9000000001"}
    payload.update(changes.pop("payload", {}))
    record = {"job_key": "umbrella:9000000001", "employer": "umbrella",
              "requisition": "9000000001", "candidate_email": EMAIL,
              "payload": json.dumps(payload)}
    record.update(changes)
    return record


def quotes(*values):
    return [{"tool_call_id": "item_%d" % index,
             "quote": "%d heading %s, Value: 1" % (index, value)}
            for index, value in enumerate(values)]


def evidence(**changes):
    # The page title equals the stored one by default; a different rendering is accepted only
    # on the read's own role_matches_expected judgement (owner, 2026-10-07, item 3).
    record = {"job_key": "umbrella:9000000001", "employer": "Umbrella", "role": STORED_ROLE,
              "requisition": "9000000001", "official_url": POSTING, "candidate_email": EMAIL,
              "identity_basis": "official_canonical_posting_id", "official_destination_verified": True,
              "independent_read": True, "verified": True, "observed_at": applications.stamp(),
              "source": "application-browser:synthetic",
              "browser_evidence": quotes(STORED_ROLE, "Umbrella", POSTING)}
    record.update(changes)
    return record


class EmployerNormalizationTests(unittest.TestCase):
    """`applications.identity` stores a casefolded employer; the page shows title case."""

    def test_page_case_employer_is_accepted_by_validate_identity(self):
        self.assertEqual(application_destination.validate_identity(
            evidence(employer="Umbrella"), job(), applications.stamp()), POSTING)

    def test_unicode_and_whitespace_employer_forms_are_accepted(self):
        for employer in ("UMBRELLA", "  Umbrella  ", "Um­brella".replace("­", ""),
                         "Ｕｍｂｒｅｌｌａ", "Umbrella\n"):
            with self.subTest(employer=employer):
                self.assertEqual(application_destination.validate_identity(
                    evidence(employer=employer), job(), applications.stamp()), POSTING)

    def test_a_different_employer_is_still_rejected(self):
        for employer in ("Umbrella Security", "um brella", "Umbrell", "another company",
                         "Umbrella Inc.", "", None, 7, ["Umbrella"]):
            with self.subTest(employer=employer):
                with self.assertRaisesRegex(ValueError, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"):
                    application_destination.validate_identity(
                        evidence(employer=employer), job(), applications.stamp())

class RoleTitleReconciliationTests(unittest.TestCase):
    """A different page title is this job's role only on the read's own judgement."""

    def test_a_different_title_is_reconciled_only_on_the_reads_judgement(self):
        # Owner, 2026-10-07 (item 3): the AI's role_matches_expected replaces the
        # punctuation-only word comparison.
        for flag in (None, False, "true"):
            with self.subTest(role_matches_expected=flag), \
                    self.assertRaisesRegex(ValueError, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"):
                application_destination.validate_identity(
                    evidence(role=OBSERVED_ROLE, role_matches_expected=flag), job(), applications.stamp())
        proof = evidence(role="Fullstack/" + OBSERVED_ROLE + " (Remote)", role_matches_expected=True)
        self.assertEqual(application_destination.validate_identity(proof, job(), applications.stamp()), POSTING)
        self.assertEqual(proof["role_title_reconciliation"]["previous_role"], STORED_ROLE)
        self.assertEqual(proof["role_title_reconciliation"]["role"], "Fullstack/" + OBSERVED_ROLE + " (Remote)")
        self.assertEqual(proof["role_title_reconciliation"]["requisition"], "9000000001")
        self.assertEqual(proof["role_title_reconciliation"]["basis"], "READ_JUDGED_SAME_ROLE")
        self.assertFalse(proof["role_title_reconciliation"]["eligibility_and_history_clearance"])

    def test_a_sub_resource_of_the_stored_posting_is_the_same_posting(self):
        target = POSTING + "/application"
        self.assertEqual(application_destination.validate_identity(
            evidence(official_url=target, browser_evidence=quotes(OBSERVED_ROLE, "Umbrella", target)),
            job(), applications.stamp()), target)

    def test_a_genuinely_different_role_is_still_rejected(self):
        for role in ("Other Role", "Senior Software Engineer, Developer Platform",
                     "Staff Software Engineer, Developer Platform II",
                     "Developer Platform, Staff Software Engineer",
                     "Staff Software Engineer", "Staff Software Engineer, Developer Experience",
                     "—", "", "   ", None, 7):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"):
                    application_destination.validate_identity(
                        evidence(role=role, browser_evidence=quotes(str(role), "Umbrella", POSTING)),
                        job(), applications.stamp())

    def test_a_different_role_quoted_from_the_page_is_still_rejected(self):
        """The whole page text is quotable; being on the page is not being this posting."""
        page = [{"tool_call_id": "item_0", "quote":
                 "Umbrella careers: Staff Software Engineer, Developer Platform; "
                 "Senior Product Designer, Growth; Staff Software Engineer, Detections"}]
        with self.assertRaisesRegex(ValueError, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"):
            application_destination.validate_identity(
                evidence(role="Senior Product Designer, Growth", browser_evidence=page),
                job(), applications.stamp())

    def test_an_unresolved_requisition_reconciles_a_judged_title(self):
        """`reconcile_requisition` exists for these jobs."""
        unresolved = job(requisition="unresolved:c00000000000000000000016")
        self.assertEqual(application_destination.validate_identity(
            evidence(role=OBSERVED_ROLE, role_matches_expected=True), unresolved, applications.stamp()), POSTING)

    def test_a_known_requisition_must_match_before_a_title_is_reconciled(self):
        with self.assertRaisesRegex(ValueError, "APPLICATION_OFFICIAL_IDENTITY_REQUIRED"):
            application_destination.validate_identity(
                evidence(requisition="9000000098", role=OBSERVED_ROLE, role_matches_expected=True),
                job(), applications.stamp())

class LoadBearingGatesUnchangedTests(unittest.TestCase):
    """Reconciliation must never open a gate other than the two title comparisons."""

    def test_every_other_identity_gate_still_rejects(self):
        for change in ({"observed_at": "2000-01-01T00:00:00+00:00"}, {"independent_read": False},
                       {"independent_read": "true"}, {"official_destination_verified": False},
                       {"identity_basis": "guessed_url_slug"}, {"job_key": "umbrella:other"},
                       {"candidate_email": "other@example.invalid"}, {"requisition": "9000000098"},
                       {"requisition": "*"}, {"requisition": "unresolved:abc"},
                       {"requisition": "ABC-1"}, {"requisition": ""}, {"requisition": None},
                       {"official_url": "http://job-boards.greenhouse.io/umbrella/jobs/9000000001"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    application_destination.validate_identity(evidence(**change), job(), applications.stamp())

    def test_a_transcription_artifact_in_the_requisition_is_normalized_not_rejected(self):
        """Whitespace and case are how a page RENDERS an id, not which id it is.

        The read must quote the requisition verbatim ("Req ID:  R56"), so demanding it
        arrive stripped and lowercased is a demand it cannot meet - that was the defect
        DZ fixed. This case previously sat in the reject list above and passed only by
        accident: validate_identity already stripped after DZ, and the refusal came from
        role_reconciliation's raw comparison, which EF folded too (owner, 2026-09-21).
        A genuinely different id, "*", "unresolved:", empty and None all still reject above.
        """
        for rendered in ("9000000001 ", " 9000000001", "9000000001\t"):
            with self.subTest(rendered=rendered):
                self.assertEqual(
                    application_destination.validate_identity(
                        evidence(requisition=rendered), job(), applications.stamp()), POSTING)

    def test_public_url_safety_is_untouched(self):
        for url in ("http://careers.example/jobs", "https://127.0.0.1/jobs", "https://localhost/jobs",
                    "https://portal.internal/jobs", "https://user:pass@example.com/jobs",
                    "https://careers.example/jobs?access_token=secret", "https://aiapply.co/jobs"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                application_destination.public_url(url)

    def test_same_posting_never_accepts_a_parent_or_sibling(self):
        self.assertTrue(application_destination.same_posting(POSTING, POSTING))
        self.assertTrue(application_destination.same_posting(POSTING + "/application", POSTING))
        self.assertFalse(application_destination.same_posting(POSTING, POSTING + "/application"))
        self.assertFalse(application_destination.same_posting(
            "https://job-boards.greenhouse.io/umbrella/jobs/90000000011", POSTING))
        self.assertFalse(application_destination.same_posting(
            "https://job-boards.greenhouse.io.example/umbrella/jobs/9000000001/x", POSTING))


class ReconciliationEventTests(unittest.TestCase):
    """`ROLE_TITLE_RECONCILED` mirrors `identity_reconciliation`'s existing reconciled events."""

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.addCleanup(self.database.close)
        self.database.execute("""CREATE TABLE application_events (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, at TEXT NOT NULL,
            event TEXT NOT NULL, detail TEXT NOT NULL)""")

    def events(self):
        return [(row["event"], json.loads(row["detail"]))
                for row in self.database.execute("SELECT * FROM application_events ORDER BY id")]

    def test_a_reconciled_title_is_recorded_with_both_titles_and_the_evidence(self):
        now = applications.stamp()
        proof = evidence(role=OBSERVED_ROLE, role_matches_expected=True)
        application_destination.validate_identity(proof, job(), now, self.database)
        self.assertEqual([kind for kind, _ in self.events()], ["ROLE_TITLE_RECONCILED"])
        detail = self.events()[0][1]
        self.assertEqual(detail["previous_role"], STORED_ROLE)
        self.assertEqual(detail["role"], OBSERVED_ROLE)
        self.assertEqual(detail["job_key"], "umbrella:9000000001")
        self.assertEqual(detail["requisition"], "9000000001")
        self.assertEqual(detail["official_url"], POSTING)
        self.assertEqual(detail["evidence"]["source"], "application-browser:synthetic")
        self.assertIs(detail["eligibility_and_history_clearance"], False)

    def test_an_exact_title_records_nothing(self):
        application_destination.validate_identity(
            evidence(role=STORED_ROLE, browser_evidence=quotes(STORED_ROLE, "Umbrella", POSTING)),
            job(), applications.stamp(), self.database)
        self.assertEqual(self.events(), [])

    def test_a_rejected_identity_records_nothing(self):
        for change in ({"role": "Other Role"}, {"employer": "another company"},
                       {"candidate_email": "other@example.invalid"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    application_destination.validate_identity(
                        evidence(**change), job(), applications.stamp(), self.database)
        self.assertEqual(self.events(), [])

class SavedProductionEvidenceReplayTests(unittest.TestCase):
    """Replay of saved-shape investigations through the real browser normalizer."""

    def replay(self, name):
        directory = SAVED / name
        request = json.loads((directory / "request.json").read_text())
        raw = json.loads(outreach_history_browser.read(directory / "output.json", 128 * 1024),
                         object_pairs_hook=reply_worker._unique_object)
        # These reads predate role_matches_expected (2026-10-07); give each the judgement the
        # prompt now asks for, so the replay checks that the real read resolves with it.
        result = json.loads(raw["result_json"])
        for section in ("identity", "destination"):
            found = ((result.get("resolution") or {}).get(section) or {}).get("evidence")
            if isinstance(found, dict):
                found["role_matches_expected"] = True
        raw["result_json"] = json.dumps(result)
        trace = outreach_history_browser.read(directory / "trace.jsonl", browser.MAX_TRACE)
        return request, browser.normalize(request, raw, trace, applications.stamp(), directory / "trace.jsonl")

    def test_the_six_rejected_investigations_now_resolve(self):
        for name, label in REPLAYED:
            with self.subTest(request=name, job=label):
                if not (SAVED / name).is_dir():
                    self.skipTest("saved request %s is not present" % name)
                request, result = self.replay(name)
                job_record = request["payload"]["job"]
                identity = result["resolution"]["identity"]
                self.assertEqual(result["status"], "RESOLVED")
                self.assertEqual(identity["requisition"], job_record["requisition"])
                self.assertEqual(identity["evidence"]["job_key"], job_record["job_key"])
                self.assertEqual(applications.normalized(identity["evidence"]["employer"]), job_record["employer"])
                self.assertEqual(application_destination.words(identity["evidence"]["role"]),
                                 application_destination.words(json.loads(job_record["payload"])["role"]))
                self.assertFalse(job_record["requisition"].startswith("unresolved:"))

    def test_the_replayed_role_reconciliations_are_recorded(self):
        for name in ("request-fixt0011", "request-fixt0014", "request-fixt0013", "request-fixt0012"):
            with self.subTest(request=name):
                if not (SAVED / name).is_dir():
                    self.skipTest("saved request %s is not present" % name)
                _, result = self.replay(name)
                recorded = result["resolution"]["identity"]["evidence"]["role_title_reconciliation"]
                self.assertEqual(recorded["basis"], "READ_JUDGED_SAME_ROLE")
                self.assertNotEqual(recorded["previous_role"], recorded["role"])

    def test_a_tampered_replay_role_is_still_rejected(self):
        """The same saved read, with the title swapped for another role, must not resolve."""
        directory = SAVED / "request-fixt0011"
        if not directory.is_dir():
            self.skipTest("saved request is not present")
        request = json.loads((directory / "request.json").read_text())
        raw = json.loads(outreach_history_browser.read(directory / "output.json", 128 * 1024),
                         object_pairs_hook=reply_worker._unique_object)
        trace = outreach_history_browser.read(directory / "trace.jsonl", browser.MAX_TRACE)
        for field, value in (("role", "Senior Software Engineer, Developer Platform"),
                             ("employer", "Umbrella Security"),
                             ("requisition", "9000000098")):
            with self.subTest(field=field):
                tampered = copy.deepcopy(raw)
                result = json.loads(tampered["result_json"])
                result["resolution"]["identity"]["evidence"][field] = value
                tampered["result_json"] = json.dumps(result)
                with self.assertRaises(ValueError):
                    browser.normalize(request, tampered, trace, applications.stamp(), directory / "trace.jsonl")


if __name__ == "__main__":
    unittest.main()
