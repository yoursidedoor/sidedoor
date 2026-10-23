import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest.mock import patch

import application_destination
import application_recovery
import applications
import identity_reconciliation
import test_applications_e2e as fixtures


class IdentityReconciliationTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp

    def prepare(self):
        role = dict(fixtures.role(), url="https://official.example/careers/known-posting", requisition_id="")
        with self.database:
            key = applications.ingest(self.database, role, fixtures.NOW)
        evidence = dict(fixtures.evidence(), employer="example ai", role=role["role"], source=role["url"],
                        independent_read=True, requisition="known-posting", identity_basis="official_canonical_posting_id")
        return key, role, evidence

    def test_identity_updates_in_place_and_new_discovery_reuses_same_task(self):
        key, role, evidence = self.prepare()
        result = identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(result["state"], "IDENTITY_RECONCILED_GATES_UNCHANGED")
        with self.database:
            self.assertEqual(applications.ingest(self.database, dict(role, requisition_id="known-posting")), key)
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        self.assertEqual(job["requisition"], "known-posting")
        self.assertEqual(job["state"], "QUEUED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
        self.assertEqual(identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)["state"], "ALREADY_RECONCILED")

    def test_wrong_role_stale_evidence_and_known_identity_are_rejected(self):
        key, role, evidence = self.prepare()
        for change in ({"source": "https://other.example/role"}, {"role": "Other Role"},
                       {"observed_at": "2026-09-10T16:00:00+00:00"}, {"independent_read": False}):
            with self.assertRaises(ValueError):
                identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", dict(evidence, **change), fixtures.NOW)
        identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_requisition(self.database, key, "other-posting", dict(evidence, requisition="other-posting"), fixtures.NOW)

    def test_terminal_or_attempted_jobs_are_never_rebound(self):
        key, role, evidence = self.prepare()
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=1 WHERE job_key=?", (key,))
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)

    def test_existing_resolved_job_collision_does_not_merge_blindly(self):
        key, role, evidence = self.prepare()
        with self.database:
            applications.ingest(self.database, dict(role, requisition_id="known-posting"), fixtures.NOW)
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(list(self.database.iterdump()), before)

    def test_official_page_employer_letter_case_and_spacing_reconciles(self):
        # The stored employer is normalized by construction (applications.identity -> normalized()).
        # An independent read quotes the employer as the page renders it. That difference is cosmetic
        # only and must not be reported as an identity failure.
        for observed in ("Example AI", "EXAMPLE AI", "  Example   AI  ", "Example AI", "Example ＡI"):
            with self.subTest(observed=observed):
                key, role, evidence = self.prepare()
                result = identity_reconciliation.reconcile_requisition(
                    self.database, key, "known-posting", dict(evidence, employer=observed), fixtures.NOW)
                self.assertEqual(result["state"], "IDENTITY_RECONCILED_GATES_UNCHANGED")
                job = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
                self.assertEqual(job["employer"], "example ai")
                self.assertEqual(job["requisition"], "known-posting")
                self.assertEqual(job["state"], "QUEUED")
                with self.database:
                    self.database.execute("DELETE FROM application_aliases")
                    self.database.execute("DELETE FROM application_jobs")
                    self.database.execute("DELETE FROM application_source_keys")
                    self.database.execute("DELETE FROM application_events")

    def test_employer_normalization_never_admits_a_different_company(self):
        key, role, evidence = self.prepare()
        before = list(self.database.iterdump())
        for other in ("Other Company", "example ai gmbh", "exampleai", "example a i", "example", "", None, 7):
            with self.subTest(other=other):
                with self.assertRaises(ValueError):
                    identity_reconciliation.reconcile_requisition(
                        self.database, key, "known-posting", dict(evidence, employer=other), fixtures.NOW)
                self.assertEqual(list(self.database.iterdump()), before)

    def test_employer_normalization_does_not_loosen_role_or_requisition(self):
        key, role, evidence = self.prepare()
        before = list(self.database.iterdump())
        for change in ({"role": "Another Role"}, {"role": "Staff Software Engineer II"},
                       {"requisition": "another-posting"}, {"requisition": "known-posting-2"},
                       {"job_key": "example ai:another-posting"},
                       {"identity_basis": "guessed_url_slug"}, {"independent_read": False},
                       {"verified": False}, {"observed_at": "2026-09-10T16:00:00+00:00"},
                       {"source": "https://other.example/careers/known-posting"}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    identity_reconciliation.reconcile_requisition(
                        self.database, key, "known-posting", dict(evidence, employer="Example AI", **change), fixtures.NOW)
                self.assertEqual(list(self.database.iterdump()), before)

    def test_recovery_still_rejects_a_different_candidate_and_attempted_work(self):
        key, role, evidence = self.prepare()
        job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                   candidate_email="alex.example@example.com")
        resolution = {"identity": {"requisition": "known-posting",
                                   "evidence": dict(evidence, employer="Example AI", job_key=key,
                                                    candidate_email="someone.else@example.invalid")}}
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            application_recovery.apply_resolution(self.database, job, resolution, fixtures.NOW)
        self.assertEqual(list(self.database.iterdump()), before)
        # A bare attempts=1 no longer refuses (owner, 2026-09-21); an OUTSTANDING submission
        # does, so this half now asserts the protection that is actually load-bearing.
        with self.database:
            self.database.execute("UPDATE application_jobs SET attempts=1 WHERE job_key=?", (key,))
            self.database.execute(
                "INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                "VALUES ('in-flight',?,?,'SUBMITTING',?)", (key, key, fixtures.NOW))
        job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                   candidate_email="alex.example@example.com")
        resolution["identity"]["evidence"]["candidate_email"] = "alex.example@example.com"
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            application_recovery.apply_resolution(self.database, job, resolution, fixtures.NOW)
        self.assertEqual(list(self.database.iterdump()), before)
    # --- Browser identity path: the employer AND the role title now go through
    # --- application_destination's shared helpers, so this module and validate_identity
    # --- cannot disagree about what "the same posting" means.

    SUB_PAGE = "https://official.example/careers/known-posting/application"

    def prepare_browser(self, stored_role="Staff Software Engineer — Developer Platform",
                        observed_role="Staff Software Engineer, Developer Platform", **overrides):
        """A job whose tracker title carries research punctuation, plus a full browser packet.

        This is the real production shape: the tracker holds an em dash and spaced slashes from
        the handoff spreadsheet, the live posting renders a comma, and the browser quotes the
        page. The stored URL uses a host `applications.identity` does not recognise, so the job
        keeps an `unresolved:` requisition and `reconcile_requisition` does real work.
        """
        import desktop_adapter
        desktop_adapter.initialize(self.database)
        role = dict(fixtures.role(), url="https://official.example/careers/known-posting",
                    requisition_id="", role=stored_role)
        with self.database:
            key = applications.ingest(self.database, role, fixtures.NOW)
        evidence = dict(fixtures.evidence(), employer="Example AI", role=observed_role, job_key=key,
                        requisition="known-posting", identity_basis="official_canonical_posting_id",
                        independent_read=True, official_url=role["url"],
                        official_destination_verified=True, candidate_email="alex.example@example.com",
                        source="application-browser:request-mock",
                        # The read judged the page's rendering to be this job's role (owner,
                        # 2026-10-07, item 3: the AI flag replaces the punctuation-only rule).
                        role_matches_expected=True,
                        browser_evidence=[{"tool_call_id": "call-1", "url": role["url"],
                                           "quote": "Example AI is hiring: " + observed_role
                                                    + " — Remote (US). Apply for this Job"}])
        return key, role, dict(evidence, **overrides)

    def identity_state(self):
        """Everything a reconciliation may change, minus the purely advisory audit events.

        `ROLE_TITLE_RECONCILED` is an audit record written by `validate_identity`; it binds no
        identity on its own. Every row that does bind one is captured here, and
        `test_a_rejected_packet_is_rolled_back_through_the_production_entry_point` shows the
        production caller rolls the audit row back too.
        """
        state = {}
        for table in ("application_jobs", "application_aliases", "application_source_keys",
                      "application_attempts", "application_blockers"):
            state[table] = list(self.database.execute("SELECT * FROM " + table))
        state["events"] = [row[0] for row in self.database.execute(
            "SELECT event FROM application_events WHERE event<>'ROLE_TITLE_RECONCILED' ORDER BY id")]
        return state

    def role_events(self, key):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE event='ROLE_TITLE_RECONCILED' AND job_key=? ORDER BY id",
            (key,))]

    def clear(self):
        with self.database:
            for table in ("application_aliases", "application_jobs", "application_source_keys",
                          "application_events"):
                self.database.execute("DELETE FROM " + table)

    def test_browser_identity_path_reconciles_the_page_employer_and_title(self):
        # Both halves of the defect at once, against the real application_destination fix rather
        # than a simulation of it: the page renders "Example AI" where the tracker stores
        # "example ai", and renders a comma where the tracker stores an em dash. Neither is an
        # identity failure, and the whole write path must complete.
        key, role, evidence = self.prepare_browser()
        result = identity_reconciliation.reconcile_requisition(
            self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(result["state"], "IDENTITY_RECONCILED_GATES_UNCHANGED")
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        self.assertEqual(job["requisition"], "known-posting")
        self.assertEqual(job["employer"], "example ai")
        self.assertEqual(job["state"], "QUEUED")
        # The tracker title is NOT rewritten; only the comparison tolerates the variance.
        self.assertEqual(json.loads(job["payload"])["role"], "Staff Software Engineer — Developer Platform")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)
        detail, = self.role_events(key)
        self.assertEqual(detail["previous_role"], "Staff Software Engineer — Developer Platform")
        self.assertEqual(detail["role"], "Staff Software Engineer, Developer Platform")
        self.assertEqual(detail["basis"], "READ_JUDGED_SAME_ROLE")
        self.assertIs(detail["eligibility_and_history_clearance"], False)
        self.assertEqual(application_destination.words(detail["previous_role"]),
                         application_destination.words(detail["role"]))
        self.assertEqual([row[0] for row in self.database.execute(
            "SELECT event FROM application_events WHERE job_key=? ORDER BY id", (key,))],
            ["DISCOVERED", "ROLE_TITLE_RECONCILED", "REQUISITION_RECONCILED"])

    def test_the_ashby_application_sub_page_is_the_same_posting(self):
        # The real weyland ai second read (request-fixt0012) returned the posting's
        # `/application` sub-page as its official_url. That is the same posting one level down.
        key, role, evidence = self.prepare_browser(official_url=self.SUB_PAGE)
        result = identity_reconciliation.reconcile_requisition(
            self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(result["state"], "IDENTITY_RECONCILED_GATES_UNCHANGED")
        self.assertEqual(self.role_events(key)[0]["official_url"], self.SUB_PAGE)

    def test_a_title_reconciliation_never_admits_a_different_role(self):
        # A "quoted from the page" test is nearly vacuous on its own: the saved browser output
        # quotes the whole encoded result, so a wrong title can still be a genuine substring of a
        # genuine quote. Both of the QUOTED cases below are real substrings of this packet's own
        # quote and are still refused, because the word sequence must match exactly.
        quote = "Example AI is hiring: Staff Software Engineer, Developer Platform — Remote (US). Apply for this Job"
        for label, change in (
                ("different role", {"role": "Other Role", "role_matches_expected": False}),
                ("Senior, not Staff", {"role": "Senior Software Engineer, Developer Platform", "role_matches_expected": False}),
                ("QUOTED prefix", {"role": "Staff Software Engineer", "role_matches_expected": False}),
                ("QUOTED suffix", {"role": "Developer Platform", "role_matches_expected": False}),
                ("QUOTED, one extra real word", {"role": "Staff Software Engineer, Developer Platform — Remote", "role_matches_expected": False}),
                ("same words, wrong order", {"role": "Developer Platform, Staff Software Engineer", "role_matches_expected": False}),
                ("empty", {"role": ""}), ("not a string", {"role": 7}), ("absent", {"role": None}),
                # The three "title quoted?" cases were deleted (owner, 2026-09-27): the title no
                # longer has to be quoted; the word-sequence and binding cases all remain.
                # The three "official_url outside the stored posting" cases were deleted (owner,
                # 2026-10-07, item 3): that condition belonged to the punctuation-only title rule.
                ("different employer", {"employer": "Other Company"}),
                ("employer near-miss", {"employer": "example ai gmbh"}),
                ("different job_key", {"job_key": "example ai:other-posting"}),
                ("stale evidence", {"observed_at": "2026-09-10T16:00:00+00:00"}),
                ("not an independent read", {"independent_read": False}),
                ("destination not verified", {"official_destination_verified": False}),
                ("guessed identity basis", {"identity_basis": "guessed_url_slug"}),
                # ("no browser evidence" removed - owner, 2026-09-27: no quote evidence is required)
                ("foreign source", {"source": "https://official.example/careers/known-posting"})):
            with self.subTest(case=label):
                key, role, evidence = self.prepare_browser(**change)
                before = self.identity_state()
                with self.assertRaises(ValueError):
                    identity_reconciliation.reconcile_requisition(
                        self.database, key, "known-posting", evidence, fixtures.NOW)
                self.assertEqual(self.identity_state(), before)
                row = self.database.execute("SELECT requisition FROM application_jobs WHERE job_key=?", (key,)).fetchone()
                self.assertTrue(row["requisition"].startswith("unresolved:"))
                self.clear()

    def test_a_rejected_packet_is_rolled_back_through_the_production_entry_point(self):
        # The only case above that reaches a database write at all is the one rejected AFTER
        # validate_identity: the advisory ROLE_TITLE_RECONCILED row. The production caller,
        # application_recovery.apply_resolution, wraps the whole reconciliation in a SAVEPOINT,
        # so nothing at all survives a rejection.
        for change in ({"source": "https://official.example/careers/known-posting"},
                       {"role": "Staff Software Engineer", "role_matches_expected": False}, {"employer": "Other Company"},
                       {"candidate_email": "someone.else@example.invalid"}):
            with self.subTest(change=change):
                key, role, evidence = self.prepare_browser(**change)
                job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone(),
                           candidate_email="alex.example@example.com")
                before = list(self.database.iterdump())
                with self.assertRaises(ValueError):
                    application_recovery.apply_resolution(
                        self.database, job, {"identity": {"requisition": "known-posting", "evidence": evidence}},
                        fixtures.NOW)
                self.assertEqual(list(self.database.iterdump()), before)
                self.clear()

    def test_the_title_rule_is_application_destinations_and_is_not_reimplemented_here(self):
        # Requirement: one definition of employer folding and one definition of title
        # reconciliation, shared with validate_identity. Non-browser evidence is used so
        # validate_identity is not involved and only this module's own use is under test.
        key, role, evidence = self.prepare()
        with patch.object(application_destination, "role_accepted", return_value=False):
            with self.assertRaises(ValueError):
                identity_reconciliation.reconcile_requisition(
                    self.database, key, "known-posting", evidence, fixtures.NOW)
        with patch.object(application_destination, "employer_key", return_value="not the stored key"):
            with self.assertRaises(ValueError):
                identity_reconciliation.reconcile_requisition(
                    self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(
            identity_reconciliation.reconcile_requisition(
                self.database, key, "known-posting", evidence, fixtures.NOW)["state"],
            "IDENTITY_RECONCILED_GATES_UNCHANGED")

    def test_a_punctuation_variant_title_is_still_refused_without_a_browser_read(self):
        # The loosening is confined to an observed, quoted, same-posting browser read. Evidence
        # with no official_url cannot reconcile a title at all, exactly as before this change.
        key, role, evidence = self.prepare()
        before = list(self.database.iterdump())
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_requisition(
                self.database, key, "known-posting",
                dict(evidence, role=role["role"] + " — Developer Platform"), fixtures.NOW)
        self.assertEqual(list(self.database.iterdump()), before)

    def test_the_reconciliation_is_recorded_once_across_requisition_then_destination(self):
        # Production order is reconcile_requisition, then reconcile_destination. The durable
        # ROLE_TITLE_RECONCILED record belongs to the validate_identity call in
        # reconcile_requisition and must not be written twice for one posting.
        key, role, evidence = self.prepare_browser()
        identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        result = identity_reconciliation.reconcile_destination(
            self.database, key, role["url"], dict(evidence), fixtures.NOW)
        self.assertEqual(result["state"], "ALREADY_RECONCILED")
        self.assertEqual(len(self.role_events(key)), 1)
        # The reconciliation also travels inside the evidence recorded with REQUISITION_RECONCILED,
        # so it stays auditable even for a caller that supplies no database.
        detail = json.loads(self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event='REQUISITION_RECONCILED'",
            (key,)).fetchone()[0])
        self.assertEqual(detail["evidence"]["role_title_reconciliation"]["basis"], "READ_JUDGED_SAME_ROLE")

    def test_the_sub_page_destination_reconciles_and_keeps_every_other_gate(self):
        # reconcile_destination needed no change: its employer/role binding is entirely
        # validate_identity's. Proven, not assumed -- the same packet that reconciles here is
        # still refused when its destination, requisition or browser binding is wrong.
        key, role, evidence = self.prepare_browser(official_url=self.SUB_PAGE)
        identity_reconciliation.reconcile_requisition(self.database, key, "known-posting", evidence, fixtures.NOW)
        before = self.identity_state()
        for label, url, change in (
                ("url is not the observed destination", role["url"], {}),
                ("evidence requisition disagrees with the job", self.SUB_PAGE, {"requisition": "other-posting"}),
                ("different employer", self.SUB_PAGE, {"employer": "Other Company"}),
                ("different role", self.SUB_PAGE, {"role": "Senior Software Engineer, Developer Platform",
                                                    "role_matches_expected": False}),
                # ("no browser evidence" removed - owner, 2026-09-27: no quote evidence is required)
                ("foreign source", self.SUB_PAGE, {"source": "https://official.example/careers/known-posting"}),
                ("stale evidence", self.SUB_PAGE, {"observed_at": "2026-09-10T16:00:00+00:00"})):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    identity_reconciliation.reconcile_destination(
                        self.database, key, url, dict(evidence, **change), fixtures.NOW)
                self.assertEqual(self.identity_state(), before)
        result = identity_reconciliation.reconcile_destination(
            self.database, key, self.SUB_PAGE, dict(evidence), fixtures.NOW)
        self.assertEqual(result["state"], "DESTINATION_RECONCILED_GATES_UNCHANGED")
        self.assertEqual(json.loads(self.database.execute(
            "SELECT payload FROM application_jobs WHERE job_key=?", (key,)).fetchone()[0])["url"], self.SUB_PAGE)

    def test_outstanding_and_terminal_work_still_refuses_the_reconciled_browser_packet(self):
        # A terminal state or an OUTSTANDING submission still refuses a valid identity. A bare
        # attempts != 0 no longer does (owner, 2026-09-21): Pawnee Health had eight attempts,
        # every one already reconciled to CONFIRMED_NOT_SUBMITTED, and was refused and then
        # abandoned. See test_past_reconciled_attempts_no_longer_refuse_a_valid_identity below
        # for the allowed case, and the safety suite for the concurrent-SUBMITTING race.
        for state, attempts in (("SUBMITTING", 1), ("ABANDONED", 0), ("SUBMITTED", 0)):
            with self.subTest(state=state, attempts=attempts):
                key, role, evidence = self.prepare_browser()
                with self.database:
                    self.database.execute("UPDATE application_jobs SET state=?,attempts=? WHERE job_key=?",
                                          (state, attempts, key))
                before = self.identity_state()
                with self.assertRaises(ValueError):
                    identity_reconciliation.reconcile_requisition(
                        self.database, key, "known-posting", evidence, fixtures.NOW)
                self.assertEqual(self.identity_state(), before)
                row = self.database.execute("SELECT requisition,state,attempts FROM application_jobs WHERE job_key=?",
                                            (key,)).fetchone()
                self.assertTrue(row["requisition"].startswith("unresolved:"))
                self.assertEqual((row["state"], row["attempts"]), (state, attempts))
                self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)
                self.clear()

    def test_past_reconciled_attempts_no_longer_refuse_a_valid_identity(self):
        """A job that tried and provably did NOT submit is the one most needing a correction.

        This is the Pawnee Health case of 2026-09-21: eight attempts, every one
        CONFIRMED_NOT_SUBMITTED, refused at 21:23:15Z and abandoned 37 seconds later. The
        reconciled rows ARE the independent outcome reconciliation the refusal demanded.
        """
        key, role, evidence = self.prepare_browser()
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='QUEUED',attempts=2 WHERE job_key=?", (key,))
            for index in range(2):
                self.database.execute(
                    "INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                    "VALUES (?,?,?,'CONFIRMED_NOT_SUBMITTED',?)",
                    ("spent-%d" % index, key, key + ":%d" % index, fixtures.NOW))
        result = identity_reconciliation.reconcile_requisition(
            self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(result["state"], "IDENTITY_RECONCILED_GATES_UNCHANGED")
        self.assertEqual(result["requisition"], "known-posting")
        row = self.database.execute("SELECT requisition,attempts FROM application_jobs WHERE job_key=?", (key,)).fetchone()
        self.assertEqual((row["requisition"], row["attempts"]), ("known-posting", 2))

    def test_a_live_submitting_attempt_still_refuses_a_valid_identity(self):
        """The narrowing keeps the protection that matters: a submission actually in flight."""
        key, role, evidence = self.prepare_browser()
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='QUEUED',attempts=1 WHERE job_key=?", (key,))
            self.database.execute(
                "INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                "VALUES ('in-flight',?,?,'SUBMITTING',?)", (key, key, fixtures.NOW))
        before = self.identity_state()
        with self.assertRaises(ValueError):
            identity_reconciliation.reconcile_requisition(
                self.database, key, "known-posting", evidence, fixtures.NOW)
        self.assertEqual(self.identity_state(), before)
        self.assertTrue(self.database.execute(
            "SELECT requisition FROM application_jobs WHERE job_key=?", (key,)).fetchone()[0].startswith("unresolved:"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)
