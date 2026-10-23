"""The prior-application history comparison must fold case/unicode/whitespace only.

`application_jobs.employer` is `applications.normalized(role["company"])` by
construction (`applications.identity`), so the stored side is already folded and the
browser's reported side — page or message text — is the only one that needs folding.

These fixtures deliberately never hand the comparison a pre-normalized employer. The
stored key is `"synthetic company"`; every accepted case here reports the page-cased
`"Synthetic Company"` (or a unicode/whitespace variant of it), which is what a browser
reading the employer's own posting or acknowledgment email actually returns. Feeding the
folded value straight back in is the fixture trap that kept this defect green.

This gate decides whether a *prior application* exists, so the negative cases matter more
than the positive one: a genuinely different employer, a missing employer and a
non-string employer must each still be refused, leaving the existence question
unresolved and the job blocked rather than permitted.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import json
from pathlib import Path
import tempfile
import unittest

import application_browser as browser
import applications
import external_history
import manual_browser_recovery_slice as manual

EVIDENCE_ID = "gmail:synthetic-prior-777"
OTHER_EVIDENCE_ID = "gmail:synthetic-other-employer-888"
SAME_REQUISITION_EVIDENCE_ID = "gmail:synthetic-same-requisition-666"
PRIOR = "prior-role-999"
# The stored key is "synthetic company"; this is what the page renders.
PAGE_CASED = "Synthetic Company"


class HistoryComparisonKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = manual.RecoveryFixture(Path(temporary.name) / "history")
        self.addCleanup(self.fixture.database.close)
        self.job = self.fixture.job()
        self.assertEqual(self.job["employer"], "synthetic company")
        self.assertEqual(applications.normalized(PAGE_CASED), self.job["employer"])
        self.assertNotEqual(PAGE_CASED, self.job["employer"])
        self.save(EVIDENCE_ID, self.job["employer"])
        self.save(OTHER_EVIDENCE_ID, "other company")
        self.save(SAME_REQUISITION_EVIDENCE_ID, self.job["employer"], "role-123")

    def save(self, evidence_id, employer, requisition=PRIOR):
        record = {"verified": True, "independent_read": True, "mailbox_email": self.job["candidate_email"],
                  "source": "https://mail.google.com/mail/u/0/#all/synthetic", "requisition": requisition}
        with self.fixture.database:
            self.fixture.database.execute(
                "INSERT INTO application_external_history VALUES (?,?,?,NULL,?,?,?,?,?)",
                (evidence_id, employer, "APPLICATION_CONFIRMED", "Saved prior role", applications.stamp(),
                 applications.stamp(), json.dumps(record), "synthetic-hash-" + evidence_id))

    def proposal(self, employer, relationship="DISTINCT_ROLE", prior=PRIOR, evidence_id=EVIDENCE_ID):
        evidence = {"verified": True, "job_key": self.fixture.key, "evidence_id": evidence_id,
                    "candidate_email": self.job["candidate_email"], "current_requisition": "role-123",
                    "prior_requisition": prior,
                    "comparison": "Saved prior requisition " + str(prior) + " compared with current role-123"}
        if employer is not _ABSENT:
            evidence["employer"] = employer
        return {"status": "RESOLVED", "reason": "Exact saved prior process compared with the current role",
                "next_action": "Apply the existing history gate",
                "resolution": {"identity": None, "history": [
                    {"evidence_id": evidence_id, "relationship": relationship, "evidence": evidence}]}}

    def compare(self, employer, relationship="DISTINCT_ROLE", prior=PRIOR, evidence_id=EVIDENCE_ID):
        request = manual.envelope(self.fixture)
        saved = next(index for index, row in enumerate(request["context"]["external_history"])
                     if row["evidence_id"] == evidence_id) if any(
            row["evidence_id"] == evidence_id for row in request["context"]["external_history"]) else 0

        def bind_saved(raw):
            for entry in raw["evidence"]:
                if entry["pointer"] == "/resolution/history/0/evidence/prior_requisition":
                    entry.update(tool_call_id="context:/context/external_history/" + str(saved) + "/payload",
                                 quote=str(prior))
        return manual.normalized(self.fixture, request,
                                 self.proposal(employer, relationship, prior, evidence_id), bind_saved)

    def refused(self, employer, code="APPLICATION_HISTORY_COMPARISON_INVALID", **kwargs):
        before = list(self.fixture.database.iterdump())
        with self.assertRaises(ValueError) as refusal:
            self.compare(employer, **kwargs)
        self.assertEqual(str(refusal.exception), code)
        # Refusing leaves prior-application existence unresolved: nothing is recorded,
        # so the job stays blocked rather than being permitted to submit.
        self.assertEqual(before, list(self.fixture.database.iterdump()))
        self.assertEqual(self.fixture.database.execute(
            "SELECT count(*) FROM application_history_resolutions").fetchone()[0], 0)

    # ------------------------------------------------------------------ accepted

    def test_page_cased_employer_is_accepted_and_reported_verbatim(self):
        result = self.compare(PAGE_CASED)
        entry = result["resolution"]["history"][0]
        self.assertEqual(entry["relationship"], "DISTINCT_ROLE")
        self.assertEqual(entry["evidence"]["employer"], PAGE_CASED)
        self.assertEqual(entry["evidence"]["prior_requisition"], PRIOR)
        self.assertIs(entry["evidence"]["independent_read"], True)

    def test_unicode_and_whitespace_variants_of_the_stored_key_are_accepted(self):
        for employer in ("Synthetic Company", "SYNTHETIC COMPANY", "synthetic company",
                         "  Synthetic   Company  ", "Ｓynthetic Company", "Synthetic\tCompany"):
            with self.subTest(employer=employer):
                self.assertEqual(applications.normalized(employer), self.job["employer"])
                result = self.compare(employer)
                self.assertEqual(result["resolution"]["history"][0]["evidence"]["employer"], employer)

    def test_page_cased_same_role_comparison_still_produces_the_duplicate_gate(self):
        result = self.compare(PAGE_CASED, relationship="SAME_ROLE", prior="role-123",
                              evidence_id=SAME_REQUISITION_EVIDENCE_ID)
        with self.fixture.database:
            self.assertTrue(external_history.resolve(
                self.fixture.database, SAME_REQUISITION_EVIDENCE_ID, self.fixture.key, "SAME_ROLE",
                result["resolution"]["history"][0]["evidence"], applications.stamp()))
        self.assertEqual(self.fixture.database.execute(
            "SELECT relationship FROM application_history_resolutions WHERE evidence_id=?",
            (SAME_REQUISITION_EVIDENCE_ID,)).fetchone()[0], "SAME_ROLE")
        self.assertEqual(applications.local_gate(self.fixture.database, self.fixture.job(), applications.stamp())[0],
                         "DUPLICATE")

    # ------------------------------------------------------------------ refused

    def test_a_genuinely_different_employer_is_still_refused(self):
        for employer in ("Other Company", "Synthetic Company Holdings", "SyntheticCompany",
                         "Synthetic Compan", "synthetic-company", "Synthetic Company GmbH"):
            with self.subTest(employer=employer):
                self.assertNotEqual(applications.normalized(employer), self.job["employer"])
                self.refused(employer)

    def test_a_missing_or_non_string_employer_is_refused_and_never_raises_a_type_error(self):
        # The packet is JSON, so these are the non-string shapes a model can actually send.
        for employer in (_ABSENT, None, 123, 1.5, True, ["Synthetic Company"],
                         {"value": "Synthetic Company"}, ""):
            with self.subTest(employer=employer):
                self.refused(employer)

    def test_the_fold_does_not_widen_which_saved_record_a_job_may_claim(self):
        # "other company" has its own saved acknowledgment. It is not in this job's
        # context at all, so no reported employer spelling can reach it.
        self.assertNotIn(OTHER_EVIDENCE_ID, [row["evidence_id"] for row
                                             in manual.envelope(self.fixture)["context"]["external_history"]])
        for employer in (PAGE_CASED, "Other Company", "other company"):
            with self.subTest(employer=employer):
                self.refused(employer, code="APPLICATION_HISTORY_SCOPE_INVALID", evidence_id=OTHER_EVIDENCE_ID)

    def test_a_contradicted_relationship_is_still_refused_with_a_page_cased_employer(self):
        # Folding the employer must not let a wrong SAME_ROLE/DISTINCT_ROLE judgment through.
        # A saved prior requisition that differs from the current one is not the same role,
        # and a saved prior requisition equal to the current one is not a distinct one.
        self.refused(PAGE_CASED, relationship="SAME_ROLE", prior=PRIOR)
        self.refused(PAGE_CASED, relationship="DISTINCT_ROLE", prior="role-123",
                     evidence_id=SAME_REQUISITION_EVIDENCE_ID)

    def test_the_stored_side_of_every_live_style_comparison_is_normalized_by_construction(self):
        # The fix folds one side only; this pins the assumption that makes that safe.
        for company in ("Synthetic Company", "Sirius", "Virtucon", "Umbrella", "Massive Dynamic"):
            with self.subTest(company=company):
                stored = applications.identity({"company": company, "role": "Staff Software Engineer",
                                                "url": "https://jobs.ashbyhq.com/synthetic/role-123",
                                                "requisition_id": "role-123"})[0]
                self.assertEqual(stored, applications.normalized(stored))
                self.assertEqual(stored, applications.normalized(company))


class _Marker:
    def __init__(self, label):
        self.label = label

    def __repr__(self):
        return self.label


_ABSENT = _Marker("<key absent>")
_DEFAULT = _Marker("<suite default>")


if __name__ == "__main__":
    unittest.main()


class CurrentRequisitionKeyTests(HistoryComparisonKeyTests):
    """`:587` — the same one-sided fold, for the requisition rather than the employer.

    `current_requisition` is `job["requisition"]` (stored `.strip().casefold()`,
    `applications.py:80`) or `identity["requisition"]`, which `validate_identity` refuses
    unless it already equals `requisition.strip().casefold()`. Either way the right-hand
    side is a stored-form key and the evidence side is raw browser output.

    The stored requisition here is `"role-123"`; no accepted case below reports that exact
    string, so none can pass by being pre-normalized.
    """

    # The stored key is "role-123"; this is what a mixed-case ATS renders.
    ATS_CASED = "ROLE-123"

    def proposal(self, employer, relationship="DISTINCT_ROLE", prior=PRIOR, evidence_id=EVIDENCE_ID,
                 current=_DEFAULT):
        built = super().proposal(employer, relationship, prior, evidence_id)
        evidence = built["resolution"]["history"][0]["evidence"]
        if current is _ABSENT:
            evidence.pop("current_requisition")
        else:
            # `None` is a value under test here, so the "use the default" marker must not be `None`.
            evidence["current_requisition"] = self.ATS_CASED if current is _DEFAULT else current
        return built

    def compare(self, employer=PAGE_CASED, relationship="DISTINCT_ROLE", prior=PRIOR,
                evidence_id=EVIDENCE_ID, current=_DEFAULT):
        request = manual.envelope(self.fixture)
        saved = next((index for index, row in enumerate(request["context"]["external_history"])
                      if row["evidence_id"] == evidence_id), 0)

        def bind_saved(raw):
            for entry in raw["evidence"]:
                if entry["pointer"] == "/resolution/history/0/evidence/prior_requisition":
                    entry.update(tool_call_id="context:/context/external_history/" + str(saved) + "/payload",
                                 quote=str(prior))
        return manual.normalized(self.fixture, request,
                                 self.proposal(employer, relationship, prior, evidence_id, current), bind_saved)

    def setUp(self):
        super().setUp()
        self.assertEqual(self.job["requisition"], "role-123")
        self.assertNotEqual(self.ATS_CASED, self.job["requisition"])
        self.assertEqual(self.ATS_CASED.strip().casefold(), self.job["requisition"])

    # ------------------------------------------------------------------ accepted

    def test_an_ats_cased_current_requisition_is_accepted_and_reported_verbatim(self):
        result = self.compare()
        entry = result["resolution"]["history"][0]
        self.assertEqual(entry["evidence"]["current_requisition"], self.ATS_CASED)
        self.assertEqual(entry["relationship"], "DISTINCT_ROLE")

    def test_case_and_surrounding_whitespace_variants_of_the_stored_requisition_are_accepted(self):
        for current in ("ROLE-123", "Role-123", "rOlE-123", "  role-123  ", "\trole-123\n"):
            with self.subTest(current=current):
                self.assertEqual(current.strip().casefold(), self.job["requisition"])
                result = self.compare(current=current)
                self.assertEqual(result["resolution"]["history"][0]["evidence"]["current_requisition"], current)

    def test_an_ats_cased_same_role_comparison_still_produces_the_duplicate_gate(self):
        result = self.compare(relationship="SAME_ROLE", prior="role-123",
                              evidence_id=SAME_REQUISITION_EVIDENCE_ID)
        with self.fixture.database:
            self.assertTrue(external_history.resolve(
                self.fixture.database, SAME_REQUISITION_EVIDENCE_ID, self.fixture.key, "SAME_ROLE",
                result["resolution"]["history"][0]["evidence"], applications.stamp()))
        self.assertEqual(applications.local_gate(self.fixture.database, self.fixture.job(), applications.stamp())[0],
                         "DUPLICATE")

    # ------------------------------------------------------------------ refused

    def test_a_genuinely_different_current_requisition_is_still_refused(self):
        for current in ("ROLE-456", "role-1234", "role 123", "role_123", "ROLE-123-B", "role-12", ""):
            with self.subTest(current=current):
                self.assertNotEqual(current.strip().casefold(), self.job["requisition"])
                self.refused(PAGE_CASED, current=current)

    def test_a_missing_or_non_string_current_requisition_is_refused_without_an_attribute_error(self):
        for current in (_ABSENT, None, 123, 1.5, True, ["role-123"], {"value": "role-123"}):
            with self.subTest(current=current):
                self.refused(PAGE_CASED, current=current)

    def test_the_requisition_fold_is_the_exact_inverse_of_the_storing_function_not_normalized(self):
        # `applications.normalized` additionally applies NFKC and collapses interior whitespace,
        # which is wider than `applications.identity`'s `.strip().casefold()`. Using it here would
        # accept requisitions that could never have produced this stored key.
        self.assertEqual(browser.requisition_key("  ROLE-123 "), "role-123")
        self.assertIsNone(browser.requisition_key(None))
        self.assertIsNone(browser.requisition_key(123))
        for company_requisition in ("role-123", "REQ 123", "R-9 7", "Req--45"):
            with self.subTest(requisition=company_requisition):
                stored = applications.identity({"company": "Synthetic Company", "role": "Staff Software Engineer",
                                                "url": "https://jobs.ashbyhq.com/synthetic/role-123",
                                                "requisition_id": company_requisition})[1]
                self.assertEqual(stored, browser.requisition_key(stored))
                self.assertEqual(stored, browser.requisition_key(company_requisition))
        # A value `normalized` would accept but the storing function never produces.
        self.assertNotEqual(browser.requisition_key("REQ  123"), applications.normalized("REQ  123"))


class IdentityDestinationEmployerTests(unittest.TestCase):
    """`:566-568` — the identity/destination cross-check compared two raw employer spellings.

    Both sections have already passed `application_destination.validate_identity`, which binds
    each `employer` to `job["employer"]` through `employer_key`. So this comparison could never
    catch a wrong employer; it could only fire when the browser rendered the same employer two
    ways in one result. The live data shows the browser doing exactly that across requests
    (`'Umbrella'` and `'umbrella'` for `umbrella:9000000001`).
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = manual.RecoveryFixture(Path(temporary.name) / "sections")
        self.addCleanup(self.fixture.database.close)
        self.job = self.fixture.job()
        self.assertEqual(self.job["employer"], "synthetic company")

    def evidence(self, employer):
        built = dict(manual.identity(self.fixture),
                     official_url=json.loads(self.job["payload"])["url"])
        if employer is _ABSENT:
            built.pop("employer")
        else:
            built["employer"] = employer
        return built

    def propose(self, identity_employer, destination_employer):
        identity = self.evidence(identity_employer)
        destination = self.evidence(destination_employer)
        return {"status": "RESOLVED", "reason": "Observed the official canonical posting identity",
                "next_action": "Inspect the current form",
                "resolution": {"identity": {"requisition": "role-123", "evidence": identity},
                               "destination": {"url": destination["official_url"], "evidence": destination},
                               "history": []}}

    def run_sections(self, identity_employer, destination_employer):
        return manual.normalized(self.fixture, manual.envelope(self.fixture),
                                 self.propose(identity_employer, destination_employer))

    def refused(self, identity_employer, destination_employer, code):
        before = list(self.fixture.database.iterdump())
        with self.assertRaises(ValueError) as refusal:
            self.run_sections(identity_employer, destination_employer)
        self.assertEqual(str(refusal.exception), code)
        self.assertEqual(before, list(self.fixture.database.iterdump()))
        self.assertEqual(self.fixture.database.execute(
            "SELECT count(*) FROM application_history_resolutions").fetchone()[0], 0)

    # ------------------------------------------------------------------ accepted

    def test_one_result_may_render_the_same_employer_two_ways(self):
        for identity_employer, destination_employer in (
                (PAGE_CASED, "SYNTHETIC COMPANY"), ("SYNTHETIC COMPANY", PAGE_CASED),
                (PAGE_CASED, "synthetic company"), ("  Synthetic   Company  ", PAGE_CASED)):
            with self.subTest(identity=identity_employer, destination=destination_employer):
                result = self.run_sections(identity_employer, destination_employer)
                resolution = result["resolution"]
                self.assertEqual(resolution["identity"]["evidence"]["employer"], identity_employer)
                self.assertEqual(resolution["destination"]["evidence"]["employer"], destination_employer)

    # ------------------------------------------------------------------ refused

    def test_a_genuinely_different_employer_in_either_section_is_still_refused(self):
        # Each section is refused against the stored key *before* the cross-check is reached, so
        # the fold cannot let a wrong employer through either side. Two earlier gates do the work:
        # `require_observation` (the employer was never quoted from the read) and `validate_identity`
        # (`employer_key(...) != job["employer"]`). Both codes are recorded exactly.
        required, unobserved = "APPLICATION_OFFICIAL_IDENTITY_REQUIRED", "APPLICATION_INVESTIGATION_IDENTITY_UNOBSERVED"
        for identity_employer, destination_employer, code in (
                (PAGE_CASED, "Other Company", required), ("Other Company", PAGE_CASED, required),
                (PAGE_CASED, "Synthetic Company Holdings", required), ("SyntheticCompany", PAGE_CASED, required),
                (PAGE_CASED, _ABSENT, required), (_ABSENT, PAGE_CASED, required),
                (PAGE_CASED, None, required), (PAGE_CASED, 123, required)):
            with self.subTest(identity=identity_employer, destination=destination_employer):
                self.refused(identity_employer, destination_employer, code)

    def test_the_other_cross_checked_fields_stay_exact(self):
        # Only the employer term was folded. A differing role or candidate_email between the two
        # sections must still raise the cross-check's own code.
        for field, value in (("role", "Other Role"), ("candidate_email", "wrong@example.invalid"),
                             ("requisition", "other-role")):
            with self.subTest(field=field):
                proposal = self.propose(PAGE_CASED, PAGE_CASED)
                proposal["resolution"]["destination"]["evidence"][field] = value
                before = list(self.fixture.database.iterdump())
                with self.assertRaises(ValueError):
                    manual.normalized(self.fixture, manual.envelope(self.fixture), proposal)
                self.assertEqual(before, list(self.fixture.database.iterdump()))
