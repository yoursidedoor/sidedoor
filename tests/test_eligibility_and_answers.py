"""Eligibility, answer resolution and the truthfulness boundary.

Four defects are covered here, each with a reproduction that fails on ROOT:

  D1  `",".join(missing)` raised `TypeError` because a real form returns `"id": null`.
  D2  no policy answer carried semantic `resume`, so the required resume question -
      asked at 7 of the 9 observed employers - could never be satisfied.
  F1  every non-US role was `REQUIRED_ELIGIBILITY_ANSWER` before a single form question
      was read, which contradicts the owner's instruction to pursue EU roles.
  F2  the onsite/hybrid exclusion was the literal `{"seattle", "san francisco"}`, so
      Bellevue, Redmond, Kirkland, Tacoma and Everett all passed.

The truthfulness tests are the point of the file. Pursuing a role in a jurisdiction the
owner is not authorized in must never become a claim that they are authorized there, and
`TruthfulnessTests` proves both halves: the gate stops refusing, and no phrasing, no
recorded preference and no code path yields an affirmative authorization answer.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import applications
import orchestrator
import research_workflow
import test_research_workflow
from test_applications_e2e import NOW, PIPELINE, MockATS, role, review
import profile_home
import user_profile


POLICY = user_profile.load()

# The five questions the only real enumerated review contains, verbatim from the saved
# Vandelay Security form review. Four of the five carry `"id": null`.
VANDELAY_SECURITY_QUESTIONS = [
    {"id": None, "required": True, "semantic": "legal_or_resume_full_name"},
    {"id": None, "required": True, "semantic": "contact_email"},
    {"id": None, "required": True, "semantic": "resume"},
    {"id": "00000000-0000-4000-8000-0000000000b1", "required": True, "semantic": "linkedin_profile"},
    {"id": None, "required": True, "semantic": "office_attendance_2_3_days_per_week_in_springfield_or_austin"},
]

# Every way a form could ask whether the owner currently holds the right to work in a
# jurisdiction they do not hold it in. The semantic is what `resolve_answers` matches on;
# the label is what the question says. None of these may ever resolve to an affirmative.
EU_AUTHORIZATION_PHRASINGS = [
    ("currently_authorized_to_work_in_eu", "Are you legally authorized to work in the EU?"),
    ("currently_authorized_to_work_in_eu", "Do you have the right to work in the European Union?"),
    ("authorized_to_work_in_eu", "Are you authorized to work in the EU without restriction?"),
    ("work_authorization_eu", "EU work authorization?"),
    ("currently_authorized_to_work_in_germany", "Are you authorized to work in Germany?"),
    ("currently_authorized_to_work_in_spain", "Are you authorized to work in Spain?"),
    ("currently_authorized_to_work_in_france", "Are you authorized to work in France?"),
    ("currently_authorized_to_work_in_uk", "Do you have the right to work in the United Kingdom?"),
    ("currently_authorized_to_work_in_ireland", "Are you authorized to work in Ireland?"),
    ("currently_authorized_to_work_in_netherlands", "Are you authorized to work in the Netherlands?"),
    ("currently_authorized_to_work_in_switzerland", "Are you authorized to work in Switzerland?"),
    ("currently_authorized_to_work_in_canada", "Are you eligible to work in Canada?"),
    ("legally_permitted_to_work_in_a_european_country",
     "Can you currently work legally in a European country?"),
    ("eligible_to_work_in_country_where_role_is_based",
     "Are you authorized to work where this role is located?"),
    ("right_to_work_in_role_country", "Do you hold a right to work in the country of this role?"),
    ("eu_citizenship", "Are you an EU citizen?"),
    ("holds_eu_passport", "Do you hold an EU passport?"),
    ("holds_eu_work_permit", "Do you hold an EU work permit?"),
    ("visa_status_eu", "What is your EU visa status?"),
    ("authorized_without_sponsorship_in_eu", "Can you work in the EU without sponsorship?"),
]

AFFIRMATIVE = (True, 1, "yes", "y", "true", "authorized", "eu citizen")


def affirmative(value):
    """Does this value read as "yes, authorized" to an employer?"""
    if isinstance(value, str):
        return value.strip().casefold() in {str(entry).casefold() for entry in AFFIRMATIVE}
    return any(value is entry or value == entry for entry in AFFIRMATIVE if not isinstance(entry, str))


def eu_remote_review(job, now=NOW):
    """A genuine EU-remote role: eligible in every respect except EU authorization."""
    result = review(job, now)
    result["facts"].update(country="DE", region="Europe", city="Berlin",
                           work_mode="remote", authorization="COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED")
    return result


class NullQuestionIdTests(unittest.TestCase):
    """D1 - a null question id must not crash, collide or become an unanswerable request."""

    def test_missing_answers_for_null_ids_do_not_raise(self):
        # ROOT: `",".join(missing)` over [None, None] raises TypeError.
        answers, missing = applications.resolve_answers(VANDELAY_SECURITY_QUESTIONS, POLICY)
        joined = ",".join(missing)
        self.assertTrue(all(isinstance(field, str) and field for field in missing))
        self.assertIn("office_attendance", joined)

    def test_two_null_id_questions_never_share_an_answer_key(self):
        # ROOT: both matched null-id questions write `answers[None]`, so the email answer
        # silently overwrites the full-name answer and one key carries the wrong value.
        answers, _ = applications.resolve_answers(VANDELAY_SECURITY_QUESTIONS, POLICY)
        self.assertIn("Alex Example", answers.values())
        self.assertIn("alex.example@example.com", answers.values())
        self.assertEqual(len(answers), len({applications.question_field(question)
                                            for question in VANDELAY_SECURITY_QUESTIONS
                                            if applications.question_field(question) in answers}))

    def test_derived_field_is_stable_across_reads_and_unique_per_question(self):
        first = [applications.question_field(question) for question in VANDELAY_SECURITY_QUESTIONS]
        again = [applications.question_field(dict(question)) for question in reversed(VANDELAY_SECURITY_QUESTIONS)]
        self.assertEqual(first, list(reversed(again)))
        self.assertEqual(len(set(first)), len(first))
        self.assertTrue(all(isinstance(field, str) and field.strip() for field in first))

    def test_non_string_ids_are_all_survivable(self):
        for identifier in (None, "", "   ", 17, [], {}, False):
            with self.subTest(identifier=identifier):
                field = applications.question_field({"id": identifier, "semantic": "x", "required": True})
                self.assertIsInstance(field, str)
                self.assertTrue(field.strip())

    def test_duplicate_unanswerable_questions_are_reported_once(self):
        questions = [dict(VANDELAY_SECURITY_QUESTIONS[4]), dict(VANDELAY_SECURITY_QUESTIONS[4])]
        _, missing = applications.resolve_answers(questions, POLICY)
        self.assertEqual(len(missing), 1)


class ResumeAnswerTests(unittest.TestCase):
    """D2 - the resume question resolves, and only ever to the designated file."""

    def test_resume_question_resolves_to_the_designated_resume(self):
        question = [{"id": None, "semantic": "resume", "required": True}]
        answers, missing = applications.resolve_answers(question, POLICY)
        self.assertEqual(missing, [])
        self.assertEqual(list(answers.values()), [POLICY["resume"]["path"]])

    def test_a_resume_answer_naming_any_other_file_is_refused(self):
        policy = json.loads(json.dumps(POLICY))
        policy["answers"]["resume"]["value"] = "/tmp/some-other-resume.pdf"
        question = [{"id": None, "semantic": "resume", "required": True}]
        answers, missing = applications.resolve_answers(question, policy)
        self.assertEqual(answers, {})
        self.assertEqual(len(missing), 1)

    def test_owner_supplied_resume_path_cannot_override_the_designated_file(self):
        policy = json.loads(json.dumps(POLICY))
        policy["answers"]["resume-field"] = {"value": "~/Downloads/other.pdf", "semantic": "resume",
                                             "source": "authenticated-owner-decision:mock"}
        question = {"id": "resume-field", "semantic": "resume", "required": True}
        answers, missing = applications.resolve_answers([question], policy)
        self.assertEqual(answers, {})
        # Reported under the key the question resolves to, not the id the read reported (DU).
        self.assertEqual(missing, [applications.question_field(question)])


class TruthfulnessTests(unittest.TestCase):
    """No phrasing, preference or code path may produce a claim of EU authorization."""

    def resolved(self, semantic, label, policy=None, answer_type="boolean"):
        question = [{"id": None, "semantic": semantic, "label": label, "required": True,
                     "answer_type": answer_type}]
        answers, missing = applications.resolve_answers(question, policy or POLICY)
        return (list(answers.values()) or [None])[0], missing

    def test_no_recorded_answer_asserts_authorization_outside_the_united_states(self):
        for key, answer in POLICY["answers"].items():
            semantic = answer["semantic"]
            authorization = ("authoriz" in semantic or "permitted" in semantic
                             or "citizen" in semantic or "right_to_work" in semantic)
            if not authorization or semantic.endswith("_in_us") or semantic.endswith("_to_work_in_us"):
                continue
            with self.subTest(key=key):
                self.assertFalse(affirmative(answer["value"]), key + " asserts authorization")

    def test_jurisdiction_map_is_default_deny_and_grants_only_the_united_states(self):
        jurisdictions = applications.policy_answer(POLICY, "work_authorization_by_jurisdiction")
        self.assertEqual(jurisdictions["*"], "NOT_AUTHORIZED")
        for code, status in jurisdictions.items():
            if code == "US":
                continue
            with self.subTest(code=code):
                self.assertEqual(status, "NOT_AUTHORIZED")
        for code in ("EU", "ES", "FR", "DE", "GB"):
            self.assertIn(code, jurisdictions)

    def test_every_eu_authorization_phrasing_answers_no_or_asks_the_owner(self):
        for semantic, label in EU_AUTHORIZATION_PHRASINGS:
            with self.subTest(semantic=semantic):
                value, missing = self.resolved(semantic, label)
                self.assertFalse(affirmative(value), label)
                self.assertTrue(value is False or value is None, label)
                if value is None:
                    self.assertEqual(len(missing), 1)

    def test_the_us_authorization_answer_can_never_answer_a_non_us_question(self):
        for semantic, label in EU_AUTHORIZATION_PHRASINGS:
            with self.subTest(semantic=semantic):
                value, _ = self.resolved(semantic, label)
                self.assertIsNot(value, True)

    def test_recorded_yes_no_and_authorization_facts_are_left_to_the_answering_ai(self):
        # Owner, 2026-10-07 (item 6): Python no longer applies a recorded yes/no or
        # right-to-work fact by semantic; the answering AI answers the question. String
        # facts are still filled by semantic.
        for semantic, label in (("currently_authorized_to_work_in_us", "Are you authorized to work in the US?"),
                                ("requires_visa_sponsorship_now_or_future_outside_us",
                                 "Will you now or in the future require sponsorship?"),
                                ("requires_visa_sponsorship_now_or_future_in_us",
                                 "Will you now or in the future require US sponsorship?"),
                                ("willing_to_relocate", "Would you relocate?"),
                                ("open_to_employer_visa_sponsorship", "Open to sponsorship?")):
            with self.subTest(semantic=semantic):
                value, missing = self.resolved(semantic, label)
                self.assertIsNone(value)
                self.assertEqual(len(missing), 1)
        self.assertEqual(self.resolved("current_country", "Country of residence", answer_type="text")[0], "United States")

    def test_an_optional_question_with_a_recorded_yes_no_fact_is_put_to_the_answering_ai(self):
        # Owner, 2026-09-21: a recorded optional answer is filled. Since 2026-10-07 the AI
        # applies yes/no facts, so the optional question goes to it instead of being left blank.
        optional = {"id": "q", "required": False, "answer_type": "boolean", "choices": ["Yes", "No"],
                    "semantic": "willing_to_relocate", "label": "Would you relocate?"}
        self.assertTrue(applications.recorded_fact_for_ai(optional, POLICY))
        self.assertFalse(applications.recorded_fact_for_ai(dict(optional, semantic="unrecorded_thing"), POLICY))
        ai_only = {"answers": {"other": {"semantic": "k8s", "value": "Yes", "source": "ai-answer:x"}}}
        self.assertFalse(applications.recorded_fact_for_ai(dict(optional, semantic="k8s"), ai_only))
        self.assertFalse(applications.recorded_fact_for_ai({"id": "c", "required": False, "answer_type": "text",
                                                            "semantic": "current_country", "label": "Country"}, POLICY))
        # A yes/no stored as text ("Yes"/"No") is left to the AI like a boolean.
        for value in ("Yes", "No", "yes."):
            self.assertTrue(applications.recorded_answer_for_ai({"semantic": "office_attendance_location",
                                                                 "value": value, "source": "ai-answer:x"}))
        self.assertFalse(applications.recorded_answer_for_ai({"semantic": "notice_period", "value": "2 weeks",
                                                              "source": "owner"}))
        # Review round 3: 1/0, a value put to a yes/no control, and any answer the AI wrote for
        # another question are left to the AI too.
        yes_no = {"answer_type": "boolean", "choices": ["Yes", "No"]}
        for saved, question in (({"semantic": "sms", "value": 1, "source": "owner"}, None),
                                ({"semantic": "sms", "value": "Yes, I consent", "source": "owner"}, yes_no),
                                ({"semantic": "itar", "value": "I am a U.S. Person as defined by the ITAR",
                                  "source": "ai-answer:abc"}, None)):
            self.assertTrue(applications.recorded_answer_for_ai(saved, question), saved)

    def test_willingness_to_relocate_and_to_be_sponsored_never_leak_into_authorization(self):
        for semantic, label in EU_AUTHORIZATION_PHRASINGS:
            with self.subTest(semantic=semantic):
                self.assertFalse(affirmative(self.resolved(semantic, label)[0]))

    def test_pursuing_an_unauthorized_jurisdiction_writes_no_authorization_claim(self):
        # The gate that decides whether to APPLY reads only the sponsorship willingness and
        # the employer's own observed statement. It is not in the answer pool at all.
        review = eu_remote_review({"employer": "eu", "requisition": "1", "job_key": "eu:1",
                                   "payload": json.dumps(role()), "candidate_email": "x@example.com"})
        self.assertTrue(applications.pursue_without_authorization(review, POLICY))
        for semantic, label in EU_AUTHORIZATION_PHRASINGS:
            with self.subTest(semantic=semantic):
                self.assertFalse(affirmative(self.resolved(semantic, label)[0]))

    def test_only_values_already_recorded_in_the_policy_can_ever_be_answered(self):
        """The structural half of the proof.

        `resolve_answers` returns `saved["value"]` and nothing else: there is no branch
        that composes, negates, defaults or infers a value. So the complete set of answers
        the system can emit is the set of values recorded in the policy (plus per-job saved
        owner answers), and an affirmative EU authorization is not among them.
        """
        recorded = [answer["value"] for answer in POLICY["answers"].values()]
        for semantic, label in EU_AUTHORIZATION_PHRASINGS:
            with self.subTest(semantic=semantic):
                value, _ = self.resolved(semantic, label)
                self.assertTrue(value is None or value in recorded)
        jurisdictions = applications.policy_answer(POLICY, "work_authorization_by_jurisdiction")
        self.assertEqual({status for code, status in jurisdictions.items() if code != "US"}, {"NOT_AUTHORIZED"})


class NoticeAndStartDateTests(unittest.TestCase):
    """Two different facts. Neither may answer the other's question."""

    def test_notice_period_is_recorded_and_start_date_is_not_a_stored_date(self):
        self.assertEqual(applications.policy_answer(POLICY, "notice_period"), "2 weeks")
        rule = applications.earliest_start_rule(POLICY)
        self.assertEqual(rule["basis"], "application_date")
        self.assertEqual(rule["offset_days"], 28)

    def test_notice_period_never_answers_an_earliest_start_question(self):
        for semantic in sorted(applications.EARLIEST_START_SEMANTICS):
            with self.subTest(semantic=semantic):
                answers, missing = applications.resolve_answers(
                    [{"id": None, "semantic": semantic, "required": True}], POLICY)
                self.assertNotIn("2 weeks", answers.values())
                self.assertEqual(len(missing), 1)

    def test_the_rule_object_itself_is_never_submittable_as_an_answer(self):
        answers, _ = applications.resolve_answers(
            [{"id": None, "semantic": "earliest_start_date", "required": True}], POLICY)
        self.assertEqual(answers, {})

    def test_the_computed_date_is_application_date_plus_four_weeks(self):
        self.assertEqual(applications.earliest_start_date("2026-09-18T16:00:00+00:00", POLICY), "2026-10-16")
        self.assertEqual(applications.earliest_start_date("2026-12-31T23:59:00+00:00", POLICY), "2027-01-28")
        self.assertIsNone(applications.earliest_start_date(None, POLICY))


class ApplicationGateTests(unittest.TestCase):
    """The gate that decides whether to apply, driven through the real worker cycle."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ["state", "reports", "logs"]:
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        self.policy = user_profile.load()
        (self.root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        self.policy["resume"] = {"path": "mock-resume.pdf",
                                 "sha256": hashlib.sha256((self.root / "mock-resume.pdf").read_bytes()).hexdigest()}
        if "resume" in self.policy["answers"]:
            self.policy["answers"]["resume"]["value"] = self.policy["resume"]["path"]
        profile_home.use(self, self.policy)
        self.adapter = MockATS(self.root)
        self.network = patch("socket.create_connection", side_effect=AssertionError("Tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def discover(self, opportunity=None):
        opportunity = opportunity or role()
        orchestrator.store_research(self.database, PIPELINE,
                                    {"summary": "Mock research", "blockers": [], "opportunities": [opportunity], "drafts": []})
        return applications.identity(opportunity)[2]

    def cycle(self, seconds=0):
        self.adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()
        return applications.cycle(self.database, {}, self.root, self.adapter, self.adapter.now)

    def job(self, key):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()

    def submissions(self):
        return [payload for operation, payload in self.adapter.calls if operation == "submit"]

    def eu_role(self, name="EU Remote", requisition="eu-1", mutate=None):
        candidate = role(name, requisition)
        key = self.discover(candidate)

        def customize(result, job):
            result["facts"].update(country="DE", region="Europe", city="Berlin", work_mode="remote",
                                   authorization="COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED")
            if mutate:
                mutate(result)
        self.adapter.customize = customize
        return key

    def test_eu_remote_role_without_authorization_is_pursued(self):
        # ROOT: BLOCKED / REQUIRED_ELIGIBILITY_ANSWER before any form question is read.
        key = self.eu_role()
        self.cycle()
        job = self.job(key)
        self.assertNotEqual(job["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertEqual(job["state"], "SUBMITTED")

    def test_an_employer_that_states_it_cannot_sponsor_still_blocks(self):
        key = self.eu_role("EU No Sponsorship", "eu-2",
                           lambda result: result["facts"].update(employer_sponsorship=False))
        self.cycle()
        self.assertEqual(self.job(key)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertEqual(self.submissions(), [])

    def test_a_clearance_requirement_still_blocks_a_pursued_eu_role(self):
        key = self.eu_role("EU Clearance", "eu-3",
                           lambda result: result["facts"].update(citizenship_clearance_requirements_met=False))
        self.cycle()
        self.assertEqual(self.job(key)["blocker"], "CLEARANCE_OR_CITIZENSHIP_REQUIRED")
        self.assertEqual(self.submissions(), [])

    def test_sponsorship_willingness_is_what_the_gate_reads(self):
        policy = user_profile.load()
        policy["answers"].pop("open_to_visa_sponsorship")
        profile_home.use(self, policy)
        key = self.eu_role("EU No Willingness", "eu-4")
        self.cycle()
        self.assertEqual(self.job(key)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")

    def test_a_role_with_a_us_location_option_is_not_held_for_the_first_country(self):
        # Cohere, 2026-10-23: Toronto, Montreal, New York, San Francisco. The read's authorization
        # verdict was for Canada, its first country; the New York option is a US role.
        policy = user_profile.load()
        policy["answers"].pop("open_to_visa_sponsorship")
        profile_home.use(self, policy)
        options = [{"city": "Toronto", "country": "CA", "state": None},
                   {"city": "New York", "country": "US", "state": "NY"}]
        key = self.eu_role("CA And US", "ca-us-1", lambda result: result["facts"].update(
            country="CA", region="North America", city="Toronto", work_mode="hybrid",
            authorization="NOT_AUTHORIZED", location_options=options))
        self.cycle()
        self.assertNotEqual(self.job(key)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")

    def multi_location_role(self, name, requisition, options, policy_changes, first=None):
        policy = user_profile.load()
        policy["answers"].pop("open_to_visa_sponsorship", None)
        policy_changes(policy)
        profile_home.use(self, policy)
        first = first if first is not None else options[0]
        return self.eu_role(name, requisition, lambda result: result["facts"].update(
            country=first["country"], region=None, city=first["city"], work_mode="hybrid",
            authorization=first.get("authorization"), location_options=[dict(option) for option in options]))

    @staticmethod
    def fictional_user(policy, countries, jurisdictions, us_resident=False, us_authorized=False):
        """A stranger's profile: only their own countries and jurisdiction map decide."""
        policy["countries"] = countries
        policy["answers"]["work_authorization_by_jurisdiction"]["value"] = jurisdictions
        policy["answers"]["us_permanent_resident"] = {"semantic": "us_lawful_permanent_resident",
                                                      "value": us_resident, "source": "test"}
        policy["answers"]["us_work_authorized"] = {"semantic": "currently_authorized_to_work_in_us",
                                                   "value": us_authorized, "source": "test"}

    def test_a_us_resident_option_without_its_own_verdict_is_chosen(self):
        # An older read (no per-option verdicts): the first country's verdict was NOT_AUTHORIZED; the
        # US option is labelled from the user's own recorded answers, so it outranks the CA one.
        key = self.multi_location_role(
            "Older Read CA US", "j1-older",
            [{"city": "Toronto", "state": None, "country": "CA"}, {"city": "New York", "state": "NY", "country": "US"}],
            lambda policy: self.fictional_user(policy, ["CA", "US"], {"US": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                                                                      "*": "NOT_AUTHORIZED"}, us_resident=True),
            first={"city": "Toronto", "country": "CA", "authorization": "NOT_AUTHORIZED"})
        self.cycle()
        self.assertEqual(self.chosen_location(key)["city"], "New York")

    def test_a_uk_only_user_with_no_first_country_is_matched_by_the_options(self):
        key = self.multi_location_role(
            "No First Country", "j1-uk",
            [{"city": "Berlin", "state": None, "country": "DE", "authorization": "NOT_AUTHORIZED"},
             {"city": "London", "state": None, "country": "GB", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"}],
            lambda policy: self.fictional_user(policy, ["DE", "GB"], {"GB": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                                                                      "*": "NOT_AUTHORIZED"}),
            first={"city": None, "country": None, "authorization": None})
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(self.chosen_location(key)["country"], "GB")

    def test_a_user_without_us_authorization_still_applies_through_a_us_option(self):
        # The existing US rule (owner, 2026-10-14): sponsorship is the employer's decision.
        key = self.multi_location_role(
            "No Auth Anywhere CA US", "j1-noauth",
            [{"city": "Toronto", "state": None, "country": "CA", "authorization": "NOT_AUTHORIZED"},
             {"city": "Austin", "state": "TX", "country": "US", "authorization": "NOT_AUTHORIZED"}],
            lambda policy: self.fictional_user(policy, ["CA", "US"], {"*": "NOT_AUTHORIZED"}))
        self.cycle()
        self.assertNotEqual(self.job(key)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertEqual(self.chosen_location(key)["country"], "US")

    def test_an_option_without_a_verdict_takes_the_first_country_verdict(self):
        key = self.multi_location_role(
            "Older Read DE CA", "j1-fallback",
            [{"city": "Berlin", "state": None, "country": "DE"}, {"city": "Toronto", "state": None, "country": "CA"}],
            lambda policy: self.fictional_user(policy, ["DE", "CA"], {"EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                                                                      "*": "NOT_AUTHORIZED"}),
            first={"city": "Berlin", "country": "DE", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(self.chosen_location(key)["country"], "DE")

    def chosen_location(self, key):
        row = self.database.execute("SELECT evidence FROM application_reviews WHERE job_key=? ORDER BY rowid DESC",
                                    (key,)).fetchone()
        return json.loads(row["evidence"]).get("eligible_location")

    def test_an_eu_authorized_user_is_matched_to_the_eu_option(self):
        # J1 A (owner, 2026-10-23): a user authorized in the EU, offered CA, US and DE, is DE.
        def eu_user(policy):
            policy["countries"] = ["CA", "US", "DE"]
            policy["answers"]["us_permanent_resident"] = {"semantic": "us_lawful_permanent_resident",
                                                          "value": False, "source": "test"}
            policy["answers"]["work_authorization_by_jurisdiction"]["value"] = {
                "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "*": "NOT_AUTHORIZED"}
        options = [{"city": "Toronto", "state": None, "country": "CA", "authorization": "NOT_AUTHORIZED"},
                   {"city": "New York", "state": "NY", "country": "US", "authorization": "NOT_AUTHORIZED"},
                   {"city": "Berlin", "state": None, "country": "DE", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"}]
        key = self.multi_location_role("CA US DE", "j1-eu", options, eu_user)
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        self.assertEqual(self.chosen_location(key)["country"], "DE")
        self.assertEqual(self.submissions()[0]["eligible_location"]["city"], "Berlin")

    def test_a_us_permanent_resident_is_matched_to_a_us_option(self):
        # Cohere, 2026-10-23: Toronto, Montreal, New York, San Francisco.
        def resident(policy):
            policy["countries"] = ["CA", "US"]
            policy["answers"]["us_permanent_resident"] = {"semantic": "us_lawful_permanent_resident",
                                                          "value": True, "source": "test"}
        options = [{"city": "Toronto", "state": None, "country": "CA", "authorization": "NOT_AUTHORIZED"},
                   {"city": "Montreal", "state": None, "country": "CA", "authorization": "NOT_AUTHORIZED"},
                   {"city": "New York", "state": "NY", "country": "US", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"},
                   {"city": "San Francisco", "state": "CA", "country": "US", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"}]
        key = self.multi_location_role("Cohere SRE", "j1-cohere", options, resident)
        self.cycle()
        self.assertNotEqual(self.job(key)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertEqual(self.chosen_location(key)["country"], "US")

    def test_no_authorized_option_is_held_naming_every_option(self):
        def outside(policy):  # a user with no authorization anywhere
            self.fictional_user(policy, ["CA", "DE"], {"*": "NOT_AUTHORIZED"})
        options = [{"city": "Toronto", "state": None, "country": "CA", "authorization": "NOT_AUTHORIZED"},
                   {"city": "Berlin", "state": None, "country": "DE", "authorization": "NOT_AUTHORIZED"}]
        key = self.multi_location_role("CA DE", "j1-none", options, outside)
        self.cycle()
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"]), ("BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER"))
        self.assertIn("Toronto (CA)", job["next_action"])
        self.assertIn("Berlin (DE)", job["next_action"])
        self.assertEqual(self.submissions(), [])

    def test_an_option_outside_the_users_countries_is_never_chosen(self):
        def eu_user(policy):
            policy["countries"] = ["US", "FR"]
            policy["answers"]["work_authorization_by_jurisdiction"]["value"] = {
                "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "*": "NOT_AUTHORIZED"}
        options = [{"city": "Berlin", "state": None, "country": "DE", "authorization": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"},
                   {"city": "New York", "state": "NY", "country": "US", "authorization": "NOT_AUTHORIZED"}]
        key = self.multi_location_role("DE US", "j1-scope", options, eu_user)
        self.cycle()
        self.assertEqual(self.chosen_location(key)["country"], "US")

    def onsite(self, name, requisition, **facts):
        candidate = role(name, requisition)
        key = self.discover(candidate)
        self.adapter.customize = lambda result, job: result["facts"].update(**facts)
        return key

    def test_an_ai_location_exclusion_is_ineligible(self):
        # Owner, 2026-10-07 (item 5): the onsite exclusions are the read's scope_fit.location
        # verdict, not a Python match on city and state.
        for index, (city, mode) in enumerate([("Bellevue", "onsite"), ("San Francisco", "hybrid")]):
            with self.subTest(city=city, mode=mode):
                candidate = role("Excluded " + city, "wa-" + str(index))
                key = self.discover(candidate)

                def customize(result, job, city=city, mode=mode):
                    result["facts"].update(city=city, work_mode=mode)
                    result.setdefault("scope_fit", {})["location"] = {"in_scope": False, "reason": city + " " + mode + " is excluded"}
                self.adapter.customize = customize
                self.cycle()
                self.assertEqual(self.job(key)["state"], "INELIGIBLE", city)
        self.assertEqual(self.submissions(), [])

    def test_us_remote_is_unaffected_anywhere_including_washington(self):
        for index, city in enumerate(["Bellevue", "Seattle", "San Francisco", "Anywhere"]):
            with self.subTest(city=city):
                key = self.onsite("Remote " + city, "rem-" + str(index), city=city, work_mode="remote")
                self.cycle()
                self.assertEqual(self.job(key)["state"], "SUBMITTED", city)

    def test_dc_and_nyc_onsite_are_accepted(self):
        for index, (city, state) in enumerate([("Washington", "District of Columbia"), ("New York", "New York")]):
            with self.subTest(city=city):
                key = self.onsite("Onsite " + city, "on-" + str(index), city=city, state=state, work_mode="onsite")
                self.cycle()
                self.assertEqual(self.job(key)["state"], "SUBMITTED", city)

    def test_a_real_form_with_null_question_ids_holds_instead_of_crashing(self):
        # ROOT: TypeError out of `",".join(missing)` inside the worker cycle.
        key = self.discover()
        self.adapter.customize = lambda result, job: result.update(questions=json.loads(json.dumps(VANDELAY_SECURITY_QUESTIONS)))
        self.cycle()
        job = self.job(key)
        self.assertTrue(job["blocker"].startswith("ANSWERING_RETRY:"))
        self.assertNotIn("None", job["blocker"])
        fields = [row[0] for row in self.database.execute(
            "SELECT field FROM user_decisions WHERE job_key=? AND state='PENDING'", (key,))]
        self.assertEqual(fields, [], "the owner is never asked; the AI retries")
        self.assertEqual(self.submissions(), [])

    def test_the_resume_question_alone_no_longer_holds_the_job(self):
        # ROOT: the resume question is always missing, so the job holds forever.
        key = self.discover()
        self.adapter.customize = lambda result, job: result.update(
            questions=[{"id": "resume", "semantic": "resume", "required": True},
                       {"id": "email", "semantic": "contact_email", "required": True}])
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        submitted = self.submissions()[0]
        # Answers are keyed by applications.question_field, which derives the key from what
        # the question IS rather than the id the read reported (DU, owner 2026-09-21).
        resume_field = applications.question_field({"id": "resume", "semantic": "resume", "required": True})
        self.assertEqual(submitted["answers"][resume_field], self.policy["resume"]["path"])
        self.assertTrue(submitted["resume"].endswith(self.policy["resume"]["path"]))

    def test_an_earliest_start_question_is_answered_from_the_rule_and_persisted(self):
        key = self.discover()
        self.adapter.customize = lambda result, job: result.update(
            questions=[{"id": "start", "semantic": "earliest_start_date", "required": True},
                       {"id": "notice", "semantic": "notice_period", "required": True}])
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        submitted = self.submissions()[0]
        start_field = applications.question_field({"id": "start", "semantic": "earliest_start_date", "required": True})
        notice_field = applications.question_field({"id": "notice", "semantic": "notice_period", "required": True})
        self.assertEqual(submitted["answers"][notice_field], "2 weeks")
        self.assertEqual(submitted["answers"][start_field],
                         (dt.datetime.fromisoformat(NOW).date() + dt.timedelta(days=28)).isoformat())
        saved = self.database.execute("SELECT source FROM application_answers WHERE job_key=? AND field=?", (key, start_field)).fetchone()
        self.assertTrue(saved[0].startswith(applications.COMPUTED_ANSWER_SOURCE_PREFIX))


class ResearchRankingTests(unittest.TestCase):
    """The research side must apply the same exclusion, not a second city list."""

    def opportunity(self, city, mode, state=None):
        candidate = test_research_workflow.role()
        test_research_workflow.change(candidate, "city", city)
        test_research_workflow.change(candidate, "work_mode", mode)
        if state:
            test_research_workflow.change(candidate, "state", state)
        return research_workflow.normalize_opportunity(candidate, now=NOW, policy=POLICY)

    def test_washington_state_onsite_is_deprioritized(self):
        for city in ["Seattle", "Bellevue", "Redmond", "Kirkland", "Tacoma", "Everett", "San Francisco"]:
            with self.subTest(city=city):
                self.assertEqual(self.opportunity(city, "onsite")["ranking"]["location_priority"], 99)

    def test_a_recorded_state_excludes_a_city_absent_from_the_list(self):
        self.assertEqual(self.opportunity("Issaquah", "hybrid", state="WA")["ranking"]["location_priority"], 99)

    def test_us_remote_and_dc_onsite_keep_their_ranks(self):
        self.assertEqual(self.opportunity("Bellevue", "remote")["ranking"]["location_priority"], 2)
        self.assertEqual(self.opportunity("New York", "onsite")["ranking"]["location_priority"], 3)


if __name__ == "__main__":
    unittest.main()
