"""FU11 (owner's pick R1 a, 2026-11-17): a listing whose own Apply link leads to a posting for the same
employer and role under another real requisition id is this job's posting. The inspect read says so with a
structured LISTING_LINK_IDENTITY verdict; Python re-records the job under that posting (old id kept as the
job's key, new id an alias), reads it again there, and submits. Refused when another job owns the
destination (DUPLICATE, nothing submitted), when the verdict was not reached from the listing on file, or
from a submit session (a stop, never a re-record)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import access_outcomes
import application_browser
import application_recovery
import applications
import applications_page
import desktop_adapter
import identity_reconciliation
import posting_judge
import test_access_outcomes_e15_t1 as walls
import test_applications_e2e as fixtures

YC = "https://www.ycombinator.com/companies/beam/jobs/CnrHNaM-site-reliability-engineer"
WAAS = "https://www.workatastartup.com/jobs/98389"
OLD_KEY, NEW_KEY = "beam:cnrhnam", "beam:98389"
# Literals, not the modules' constants, so each test fails on a tree without this change for its own reason.
CODE, RECONCILED, REFUSED, DUPLICATE = "LISTING_LINK_IDENTITY", "LISTING_LINK_RECONCILED", "LISTING_LINK_REFUSED", "SAME_POSTING_LINKED"


def listing(url=YC, requisition="CnrHNaM"):
    return {"company": "Beam", "role": "Site Reliability Engineer", "url": url,
            "rationale": "MOCK FIXTURE ONLY", "requisition_id": requisition}


def verdict(**changes):
    return dict({"code": CODE, "final": False,
                 "next_action": "Re-record the job under the posting the Apply link opens",
                 "reason": "The listing's Apply link opens the employer's own posting for this role under another id.",
                 "destination_url": WAAS, "destination_requisition": "98389", "reached_from": YC,
                 "employer_matches_expected": True, "role_matches_expected": True}, **changes)


class ListingLinkATS(fixtures.MockATS):
    """The mock browser: a read of the YC listing follows its Apply link and returns the verdict; a read
    of any other URL (the Work at a Startup posting once the job is re-recorded) is an ordinary read."""

    def __init__(self, root, stop=None):
        super().__init__(root)
        self.stop = stop or verdict()

    def call(self, operation, payload):
        job = payload["job"]
        if operation == "inspect" and json.loads(job["payload"])["url"] == YC:
            self.calls.append((operation, payload))
            return {"blocked": dict(self.stop),
                    "partial_review": {"employer": "Beam", "role": "Site Reliability Engineer", "facts": {"open": True}}}
        return super().call(operation, payload)


class ListingLinkIdentityTests(fixtures.ApplicationEndToEndTests):
    def setUp(self):
        super().setUp()
        self.adapter = ListingLinkATS(self.root)

    def events(self, key, kind=None):
        rows = self.database.execute("SELECT event,detail FROM application_events WHERE job_key=? ORDER BY id", (key,))
        return [(row[0], json.loads(row[1])) for row in rows if kind is None or row[0] == kind]

    def aliases(self):
        return dict(self.database.execute("SELECT alias_key,canonical_key FROM application_aliases"))

    def test_the_worked_example_end_to_end(self):
        key = self.discover(listing())
        self.assertEqual(key, OLD_KEY)
        self.cycle()
        job = self.job(OLD_KEY)
        payload = json.loads(job["payload"])
        # Re-recorded in place: same key, history and events; the destination's id and URL are the job's now.
        self.assertEqual((job["state"], job["blocker"], job["requisition"]), ("QUEUED", None, "98389"))
        self.assertEqual((payload["url"], payload["requisition_id"], payload["role"]), (WAAS, "98389", "Site Reliability Engineer"))
        self.assertEqual(self.aliases(), {NEW_KEY: OLD_KEY})
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
        reconciled, = self.events(OLD_KEY, RECONCILED)
        self.assertEqual((reconciled[1]["previous_requisition"], reconciled[1]["requisition"], reconciled[1]["url"]),
                         ("cnrhnam", "98389", WAAS))
        self.assertEqual([event for event, _ in self.events(OLD_KEY)], ["DISCOVERED", RECONCILED])
        self.assertEqual(self.submissions(), [])
        # The next pass reads the destination as the job's own URL, answers and submits there, once.
        self.cycle(60)
        self.assertEqual(self.job(OLD_KEY)["state"], "SUBMITTED")
        submission, = self.submissions()
        self.assertEqual((submission["job"]["requisition"], json.loads(submission["job"]["payload"])["url"]), ("98389", WAAS))
        self.assertEqual([op for op, _ in self.adapter.calls], ["inspect", "inspect", "submit", "verify"])
        self.assertEqual(json.loads(self.adapter.calls[1][1]["job"]["payload"])["url"], WAAS)

    def test_a_later_copy_of_either_posting_resolves_to_the_one_job(self):
        self.discover(listing())
        self.cycle()
        self.cycle(60)
        self.assertEqual(self.job(OLD_KEY)["state"], "SUBMITTED")
        with self.database:
            self.assertEqual(applications.ingest(self.database, listing(WAAS, "98389"), fixtures.NOW), OLD_KEY)
            self.assertEqual(applications.ingest(self.database, listing(), fixtures.NOW), OLD_KEY)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
        self.cycle(120)
        self.assertEqual(len(self.submissions()), 1)

    def test_a_destination_another_job_already_applied_to_makes_this_job_a_duplicate(self):
        with self.database:
            owner = applications.ingest(self.database, listing(WAAS, "98389"), fixtures.NOW)
            self.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (owner,))
        self.assertEqual(owner, NEW_KEY)
        key = self.discover(listing())
        self.cycle()
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"], job["requisition"]), ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER, "cnrhnam"))
        self.assertEqual(json.loads(job["payload"])["url"], YC)
        self.assertEqual(job["next_action"], "Same posting as " + NEW_KEY + "; that job applied to it")
        duplicate, = self.events(key, DUPLICATE)
        self.assertEqual(duplicate[1]["same_as"], NEW_KEY)
        self.assertEqual(self.aliases(), {})
        self.assertEqual(self.job(NEW_KEY)["state"], "SUBMITTED")
        self.cycle(60)
        self.cycle(3600)
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.job(key)["state"], "DUPLICATE")

    def test_a_destination_owned_through_an_alias_or_a_queued_job_makes_this_job_its_copy(self):
        for label, prepare, carrier in (
                ("alias of another job", lambda: self.database.execute(
                    "INSERT INTO application_aliases VALUES (?,?,?)", (NEW_KEY, "beam:other", "{}")), "beam:other"),
                ("queued job", lambda: applications.ingest(self.database, listing(WAAS, "98389"), fixtures.NOW), NEW_KEY)):
            with self.subTest(label):
                with self.database:
                    for table in ("application_aliases", "application_jobs", "application_source_keys", "application_events"):
                        self.database.execute("DELETE FROM " + table)
                    self.database.execute("INSERT OR IGNORE INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                                          "VALUES ('beam:other','beam','other','{}','QUEUED',?)", (fixtures.NOW,))
                    prepare()
                key = self.discover(listing())
                self.assertEqual(self.follow(key), "DUPLICATE")
                # An open holder that has not applied carries the application; this job is its copy and
                # closes (owner's pick D1, 2026-11-18; it used to wait and read again).
                job = self.job(key)
                self.assertEqual((job["state"], job["blocker"], job["requisition"]),
                                 ("DUPLICATE", posting_judge.SAME_JOB_BLOCKER, "cnrhnam"))
                self.assertEqual(job["next_action"], "Same posting as " + carrier + "; that job carries this application")
                self.assertEqual(self.events(key, DUPLICATE)[-1][1]["same_as"], carrier)
                self.assertEqual(self.events(key, REFUSED), [])

    def test_a_verdict_not_reached_from_the_listing_or_for_another_role_is_refused(self):
        self.adapter.stop = verdict(reached_from="https://www.ycombinator.com/companies/beam/jobs/other-listing")
        key = self.discover(listing())
        self.cycle()
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"], job["requisition"]), ("BLOCKED", CODE, "cnrhnam"))
        self.assertIsNotNone(job["next_at"])
        self.assertEqual(json.loads(job["payload"])["url"], YC)
        self.assertEqual(self.aliases(), {})
        refused, = self.events(key, REFUSED)
        self.assertIn("not reached from the listing", refused[1]["reason"])
        self.assertEqual(self.events(key, RECONCILED), [])
        self.assertEqual(self.submissions(), [])
        # Every other way the verdict can fail to bind, refused with nothing changed.
        before = list(self.database.iterdump())
        for label, change in (("different role", {"role_matches_expected": False}),
                              ("role not judged", {"role_matches_expected": None}),
                              ("different employer", {"employer_matches_expected": False}),
                              ("the listing itself", {"destination_url": YC}),
                              ("the job's own id", {"destination_requisition": "CnrHNaM"}),
                              ("a placeholder id", {"destination_requisition": "unresolved:abc"}),
                              ("no id", {"destination_requisition": None}),
                              ("not https", {"destination_url": "http://www.workatastartup.com/jobs/98389"}),
                              ("no listing named", {"reached_from": None})):
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    identity_reconciliation.rebind_listing_link(self.database, key, verdict(**change), fixtures.NOW)
                self.assertEqual(list(self.database.iterdump()), before)
        # A placeholder job is never re-recorded by this path (IDENTITY_MISMATCH is its path).
        with self.database:
            placeholder = applications.ingest(self.database, dict(listing("https://beam.example/careers/sre", ""), company="Beam Two"), fixtures.NOW)
        self.assertTrue(self.job(placeholder)["requisition"].startswith("unresolved:"))
        with self.assertRaises(ValueError):
            identity_reconciliation.rebind_listing_link(
                self.database, placeholder, verdict(reached_from="https://beam.example/careers/sre"), fixtures.NOW)

    def test_an_outstanding_submission_refuses_the_re_record(self):
        key = self.discover(listing())
        with self.database:
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                                  "VALUES ('in-flight',?,?,'SUBMITTING',?)", (key, key, fixtures.NOW))
        with self.assertRaises(ValueError):
            identity_reconciliation.rebind_listing_link(self.database, key, verdict(), fixtures.NOW)
        self.assertEqual(self.job(key)["requisition"], "cnrhnam")
        self.assertEqual(self.aliases(), {})

    def try_again_takes_the_new_path(self, park):
        key = self.discover(listing())
        with self.database:
            self.assertTrue(park(dict(self.job(key))))
        parked = self.job(key)
        self.assertTrue(access_outcomes.recovery_try_again(parked["state"], parked["blocker"]))
        self.cycle()
        self.assertEqual(self.adapter.calls, [])   # parked: nothing runs on its own
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, key, fixtures.NOW)["reopened"])
        self.cycle(60)
        self.assertEqual((self.job(key)["state"], self.job(key)["requisition"]), ("QUEUED", "98389"))
        self.cycle(120)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)

    def test_try_again_on_a_job_the_user_abandoned_takes_the_new_path(self):
        self.try_again_takes_the_new_path(lambda job: access_outcomes.close_plain(self.database, job, "ABANDONED", fixtures.NOW))

    def test_try_again_on_a_job_parked_with_its_investigations_spent_takes_the_new_path(self):
        self.try_again_takes_the_new_path(lambda job: applications.hold(
            self.database, job, application_recovery.CLAIMS_EXHAUSTED_BLOCKER, application_recovery.CLAIMS_EXHAUSTED_ACTION, fixtures.NOW))

    # --- FU11 review I1 to I5 -------------------------------------------------------------------

    def follow(self, key):
        """The read's verdict acted on directly, so another job's own pass in the same cycle (it may submit
        or reconcile first) does not decide the case under test."""
        with self.database:
            return applications.follow_listing_link(self.database, dict(self.job(key)), verdict(), self.policy, fixtures.NOW)

    def holder(self, url=WAAS, requisition="98389", company="Beam"):
        with self.database:
            return applications.ingest(self.database, dict(listing(url, requisition), company=company), fixtures.NOW)

    def set_state(self, key, state, blocker=None):
        with self.database:
            self.database.execute("UPDATE application_jobs SET state=?,blocker=? WHERE job_key=?", (state, blocker, key))

    def assert_rebound(self, key):
        job = self.job(key)
        self.assertEqual((job["state"], job["requisition"], json.loads(job["payload"])["url"]), ("QUEUED", "98389", WAAS))

    def test_a_copy_of_this_job_does_not_hold_the_destination(self):
        other = self.holder()
        key = self.discover(listing())
        with self.database:
            applications.hold(self.database, dict(self.job(other)), posting_judge.SAME_JOB_BLOCKER, "copy", fixtures.NOW, "DUPLICATE")
            applications.event(self.database, other, "SAME_POSTING_LINKED", {"same_as": key}, fixtures.NOW)
        self.cycle()
        self.assert_rebound(key)
        self.assertEqual(self.job(other)["state"], "DUPLICATE")
        self.assertEqual(self.aliases()[NEW_KEY], key)
        self.assertEqual(len(self.events(key, "LISTING_LINK_POSTING_TAKEN_OVER")), 1)
        self.assertEqual(len(self.events(other, "LISTING_LINK_POSTING_TAKEN_OVER")), 1)
        with self.database:
            self.assertEqual(applications.ingest(self.database, listing(WAAS, "98389"), fixtures.NOW), key)
        self.cycle(60)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual([item["job"]["job_key"] for item in self.submissions()], [key])
        self.assertEqual(self.job(other)["state"], "DUPLICATE")

    def test_a_holder_closed_without_applying_does_not_hold_the_destination(self):
        for state in ("INELIGIBLE", "ABANDONED", "EXHAUSTED"):
            with self.subTest(state):
                with self.database:
                    for table in ("application_aliases", "application_jobs", "application_source_keys",
                                  "application_events", "application_attempts", "application_blockers"):
                        self.database.execute("DELETE FROM " + table)
                    if state == "EXHAUSTED":   # tried and provably sent nothing
                        self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                                              "VALUES ('spent',?,?,'CONFIRMED_NOT_SUBMITTED',?)", (NEW_KEY, NEW_KEY, fixtures.NOW))
                other = self.holder()
                self.set_state(other, state)
                key = self.discover(listing())
                self.cycle()
                self.assert_rebound(key)
                self.assertEqual(self.job(other)["state"], state)

    def test_a_holder_that_applied_or_may_have_makes_this_job_its_copy(self):
        for label, prepare, words in (
                ("submission outcome unknown", lambda other: self.set_state(other, "BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN"),
                 "; that job may already have applied to it, so this one never applies"),
                ("an attempt not settled as unsent", lambda other: (self.set_state(other, "EXHAUSTED"), self.database.execute(
                    "INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                    "VALUES ('sent',?,?,'SUBMITTED',?)", (other, other, fixtures.NOW)), self.database.commit()),
                 "; that job may already have applied to it, so this one never applies"),
                ("a copy of a submitted job", None, "; that job applied to it")):
            with self.subTest(label):
                with self.database:
                    for table in ("application_aliases", "application_jobs", "application_source_keys",
                                  "application_events", "application_attempts", "application_blockers"):
                        self.database.execute("DELETE FROM " + table)
                other = self.holder()
                carrier = other
                if prepare:
                    prepare(other)
                else:   # B is a closed copy of C, and C submitted: C is named, never B
                    carrier = self.holder("https://jobs.example.com/beam/sre", "elsewhere")
                    self.set_state(carrier, "SUBMITTED")
                    self.set_state(other, "DUPLICATE", posting_judge.SAME_JOB_BLOCKER)
                    with self.database:
                        applications.event(self.database, other, "SAME_POSTING_LINKED", {"same_as": carrier}, fixtures.NOW)
                key = self.discover(listing())
                self.assertEqual(self.follow(key), "DUPLICATE")
                job = self.job(key)
                self.assertEqual((job["state"], job["requisition"]), ("DUPLICATE", "cnrhnam"))
                self.assertEqual(job["next_action"], "Same posting as " + carrier + words)
                self.assertEqual(self.events(key, DUPLICATE)[-1][1]["same_as"], carrier)
                self.cycle(60)
                self.assertEqual([item for item in self.submissions() if item["job"]["job_key"] == key], [])

    def test_a_job_with_the_destination_as_its_address_holds_it_whatever_its_id_or_employer(self):
        for label, company, requisition, state, expected in (
                ("placeholder id", "Beam", "", "QUEUED", "DUPLICATE"),
                ("another employer spelling", "Beam AI", "98389", "QUEUED", "DUPLICATE"),
                ("another employer spelling, submitted", "Beam AI", "98389", "SUBMITTED", "DUPLICATE"),
                ("placeholder id, closed without applying", "Beam", "", "ABANDONED", "QUEUED")):
            with self.subTest(label):
                with self.database:
                    for table in ("application_aliases", "application_jobs", "application_source_keys",
                                  "application_events", "application_attempts", "application_blockers"):
                        self.database.execute("DELETE FROM " + table)
                other = self.holder(WAAS, requisition, company)
                if not requisition:
                    self.assertTrue(self.job(other)["requisition"].startswith("unresolved:"))
                self.set_state(other, state)
                key = self.discover(listing())
                self.follow(key)
                self.assertEqual(self.job(key)["state"], expected)
                if expected == "QUEUED":
                    self.assert_rebound(key)
                    with self.database:   # a later copy found by that URL alone is this job
                        self.assertEqual(applications.ingest(self.database, listing(WAAS, ""), fixtures.NOW), key)
                else:
                    self.assertEqual(self.job(key)["requisition"], "cnrhnam")

    def test_the_page_words_for_a_refused_verdict(self):
        words = applications_page.code_words(CODE)
        self.assertEqual(words, "The apply link leads to a posting Sidedoor could not confirm is this job. "
                                "It reads the listing again later.")
        self.assertNotIn("—", words)
        self.assertNotIn("_", words)
        self.adapter.stop = verdict(role_matches_expected=False)
        key = self.discover(listing())
        self.cycle()
        self.assertEqual(applications_page.code_words(self.job(key)["blocker"]), words)

    def test_a_tracking_query_or_fragment_on_the_listing_is_the_same_listing(self):
        for reached in (YC + "?utm_source=x#apply", YC + "/"):
            with self.subTest(reached):
                with self.database:
                    for table in ("application_aliases", "application_jobs", "application_source_keys", "application_events"):
                        self.database.execute("DELETE FROM " + table)
                key = self.discover(listing())
                identity_reconciliation.rebind_listing_link(self.database, key, verdict(reached_from=reached), fixtures.NOW)
                self.assert_rebound(key)
        key = self.discover(listing("https://www.ycombinator.com/companies/beam/jobs/other", "Other1"))
        with self.assertRaisesRegex(ValueError, "not reached from the listing"):
            identity_reconciliation.rebind_listing_link(
                self.database, key, verdict(reached_from="https://www.ycombinator.com/companies/beam/jobs/other-2"), fixtures.NOW)

    def test_a_job_waiting_to_retry_takes_the_verdict(self):
        key = self.discover(listing())
        self.set_state(key, "RETRY_WAIT", "RECONCILED_ABSENT")
        identity_reconciliation.rebind_listing_link(self.database, key, verdict(), fixtures.NOW)
        self.assert_rebound(key)

    def test_the_prompt_declares_the_verdict_and_python_never_investigates_it(self):
        inspect_rule = " ".join(application_browser.PROMPT.split())
        for text in ("following the listing's own Apply link",
                     "return blocked with code LISTING_LINK_IDENTITY, reporting inside blocked: destination_url",
                     "destination_requisition", "reached_from (the job's own URL",
                     "employer_matches_expected and role_matches_expected",
                     "Never return LISTING_LINK_IDENTITY for a page reached any other way, for a different employer or role, "
                     "or for a page that shows no id"):
            self.assertIn(text, inspect_rule)
        self.assertIn("a submit session never re-records a job", " ".join(application_browser.OPERATION_PROMPTS["submit"].split()))
        self.assertEqual((applications.LISTING_LINK_CODE, identity_reconciliation.LISTING_LINK_EVENT,
                          applications.LISTING_LINK_REFUSED_EVENT, applications.LISTING_LINK_DUPLICATE_EVENT),
                         (CODE, RECONCILED, REFUSED, DUPLICATE))
        self.assertNotIn(CODE, application_browser.OPERATION_PROMPTS["investigate"])
        self.assertNotIn(CODE, application_recovery.RECOVERABLE)
        self.assertEqual(applications.job_route(CODE, "BLOCKED"), "ORDINARY")
        self.assertIn(CODE, applications_page.CODE_WORDS)


class SubmitSessionStopTests(walls.AccessOutcomeTests):
    """A submit session that returns the code stops the job as any pre-arm stop does; nothing is re-recorded."""

    def test_a_submit_session_returning_the_code_stops_without_re_recording(self):
        request = self.run_until_submit()
        stop = verdict(reached_from="https://jobs.ashbyhq.com/synthetic/role-123",
                       destination_url="https://www.workatastartup.com/jobs/777", destination_requisition="777")
        result = {"acknowledged": False, "prearm_blocked": True, "blocked": stop}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"], row["requisition"]), ("RECONCILE", CODE, "role-123"))
        self.assertEqual(json.loads(row["payload"])["url"], "https://jobs.ashbyhq.com/synthetic/role-123")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_aliases").fetchone()[0], 0)
        self.assertEqual(self.events(RECONCILED), [])
        self.assertFalse(self.database.execute("SELECT 1 FROM application_jobs WHERE requisition='777'").fetchone())


for _name in [name for name in dir(walls.AccessOutcomeTests) if name.startswith("test_")]:
    setattr(SubmitSessionStopTests, _name, None)   # inherited tests are not run a second time
for _name in [name for name in dir(fixtures.ApplicationEndToEndTests) if name.startswith("test_")]:
    setattr(ListingLinkIdentityTests, _name, None)


if __name__ == "__main__":
    unittest.main()


class TakenOverTryAgainTests(fixtures.ApplicationEndToEndTests):
    """Review H1/H3 (owner, 2026-11-18): once a listing has taken over the posting of a job the user
    abandoned, Try again on that job is refused while the taker holds it, so two open jobs never share
    one posting; a takeover at a later moment is recorded again."""
    # Helpers borrowed, not inherited, so the class above does not run twice.
    events, follow, holder = ListingLinkIdentityTests.events, ListingLinkIdentityTests.follow, ListingLinkIdentityTests.holder

    def setUp(self):
        super().setUp()
        self.adapter = ListingLinkATS(self.root)

    def test_try_again_is_refused_while_the_taker_holds_the_posting(self):
        other = self.holder()
        with self.database:
            access_outcomes.close_plain(self.database, dict(self.job(other)), "ABANDONED", fixtures.NOW)
        key = self.discover(listing())
        self.assertEqual(self.follow(key), "RECONCILED")
        with self.database:
            result = access_outcomes.try_again(self.database, other, fixtures.NOW)
        self.assertEqual((result["reopened"], result["why"]), (False, "POSTING_TAKEN_OVER"))
        self.assertEqual(result["words"], "Another listing of this role is carrying this application.")
        self.assertEqual(self.job(other)["state"], "ABANDONED")
        # Once the taker is closed without applying too, the user may try the old job again.
        with self.database:
            access_outcomes.close_plain(self.database, dict(self.job(key)), "ABANDONED", fixtures.NOW)
            self.assertTrue(access_outcomes.try_again(self.database, other, fixtures.NOW)["reopened"])

    def test_a_takeover_at_a_later_moment_is_recorded_again(self):
        with self.database:
            for at in (fixtures.NOW, "2026-09-18T16:00:00+00:00", fixtures.NOW):
                identity_reconciliation.record_takeover(self.database, "taker", ["held"], [], WAAS, "98389", at)
        self.assertEqual(len(self.events("taker", "LISTING_LINK_POSTING_TAKEN_OVER")), 2)
