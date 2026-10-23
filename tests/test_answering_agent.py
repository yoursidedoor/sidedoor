"""The AI answering step, and the Python boundary that makes its mistakes survivable.

`resolve_answers` matched a form question to a recorded answer by exact string equality on a
semantic the employer's own browser AI authored freely, so `office_attendance_2_3_days_per_
week_in_springfield_or_austin` could never be satisfied and neither could the next employer's invention.
`answering_agent` gives that mapping back to the AI, which AGENTS.md assigns to it, and keeps
every consequence in Python.

The safety tests are the point of the file. Each of the eight invariants named in the fix
report has at least one test here that fails if Python stops enforcing it:

  1 attribution or nothing     AttributionTests
  2 authorization computed     AuthorizationComputationTests, FabricationTests
  3 willingness never capability  WillingnessTests
  4 never auto-answered        NeverAnsweredTests
  5 start date computed        StartDateTests
  6 raw output preserved       AuditTests
  7 fail safe                  DegradationTests, SeamTests
  8 nothing else loosens       SeamTests

No live AI call is made anywhere in this file. Every model response, the answer judge's
included, is produced by a local deterministic fake; the direct OpenRouter call and the judge
rounds are tested in `test_answering_judge.py`.
"""

import hashlib
import json
import pathlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import answering_agent
import answering_fixtures
import applications
import orchestrator
import workflow_tasks
from test_applications_e2e import NOW, PIPELINE, MockATS, role
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)
import profile_home
import user_profile


POLICY = user_profile.load()
PACKET_MARKER = "\n\nUNTRUSTED_APPLICATION_PACKET_JSON:\n"

# The question that started this: Vandelay Security's own wording and its own invented semantic,
# which no fixed policy key can match.
OFFICE_ATTENDANCE = {
    "id": None, "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
    "semantic": "office_attendance_2_3_days_per_week_in_springfield_or_austin",
    "label": "Can you work from our office 2-3 days a week? Our offices are in Springfield, IL and Austin.",
}

GERMANY_AUTHORIZATION = {
    "id": "q-de-auth", "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
    "semantic": "currently_authorized_to_work_in_germany",
    "label": "Are you legally authorized to work in Germany?",
}

EU_SPONSORSHIP = {
    "id": "q-eu-sponsor", "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
    "semantic": "requires_visa_sponsorship_in_eu",
    "label": "Will you now or in the future require visa sponsorship to work in the EU?",
}

RESIDENCE_AUTHORIZATION = {
    "id": "q-residence-auth", "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
    "semantic": "authorized_where_you_reside",
    "label": "Are you authorized to work in the country where you reside?",
}

NO_BASIS = {
    "id": "q-k8s", "required": True, "answer_type": "number", "choices": [],
    "semantic": "years_of_kubernetes_experience",
    "label": "How many years of Kubernetes experience do you have?",
}


def job_row(job_key="vandelay security:demo", employer="vandelay security", requisition="demo"):
    return {"job_key": job_key, "employer": employer, "requisition": requisition,
            "candidate_email": POLICY["answers"]["email"]["value"]}


def us_review(**facts):
    base = {"facts": {"country": "US", "city": "Springfield, IL; Austin, TX",
                      "work_mode": "hybrid", "region": "Midwest"},
            "compensation": {"currency": "USD", "minimum": 200000, "maximum": 300000},
            "official_url": "https://jobs.ashbyhq.com/vandelay/demo", "role": "Staff Software Engineer",
            "form_revision": "vandelay-form-v1"}
    base["facts"].update(facts)
    return base


def eu_review(**facts):
    defaults = {"country": "DE", "city": "Berlin", "work_mode": "remote", "region": "Europe"}
    defaults.update(facts)
    return us_review(**defaults)


def entry(question, **overrides):
    """A schema-complete model entry, defaulting to a safe UNKNOWN."""
    value = {"question_key": question["question_key"], "question_semantic": question["semantic"],
             "question_class": "OTHER", "status": "UNKNOWN", "answer_type": question["answer_type"],
             "value": None, "jurisdiction": None, "assertion": None, "affirmative_value": None,
             "negative_value": None, "attribution": [], "derivation": "fixture",
             "unknown_reason": "fixture default"}
    value.update(overrides)
    return value


class FakeModel:
    """A deterministic local stand-in for the subscription executor. Never a live call."""

    def __init__(self, respond=None, raw=None, error=None, judge=None):
        self.respond = respond or (lambda question, packet: entry(question))
        self.raw = raw
        self.error = error
        self.judge = judge  # decide(item, packet) for the answer judge; None accepts every answer
        self.prompts = []
        self.packets = []
        self.judge_prompts = []

    def __call__(self, prompt):
        if answering_fixtures.is_judge_prompt(prompt):
            self.judge_prompts.append(prompt)
            return answering_fixtures.judge_reply(prompt, self.judge)
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        packet = json.loads(prompt.split(PACKET_MARKER, 1)[1])
        self.packets.append(packet)
        if self.raw is not None:
            return self.raw
        return json.dumps({"packet_sha256": packet["packet_sha256"],
                           "answers": [self.respond(question, packet)
                                       for question in packet["questions"]]})


class AnsweringFixture(unittest.TestCase):
    """An isolated root, an isolated database and a policy identical to the recorded one."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ("state", "reports", "logs"):
            (self.root / folder).mkdir()
        for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
            (self.root / name).write_text((orchestrator.ROOT / name).read_text())
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        workflow_tasks.initialize(self.database)
        self.policy = json.loads(json.dumps(POLICY))
        self.job = job_row()
        self.database.execute(
            "INSERT INTO application_jobs VALUES (?,?,?,?,?,0,0,NULL,NULL,NULL,NULL,?)",
            (self.job["job_key"], self.job["employer"], self.job["requisition"],
             json.dumps(role()), "READY", NOW))
        self.database.commit()
        self.config = {"application_answering": {"enabled": True, "timeout_seconds": 5},
                       "timezone": "UTC"}
        self.network = patch("socket.create_connection",
                             side_effect=AssertionError("Tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_answer(self, questions, model, review=None, policy=None, config=None, now=NOW):
        return answering_agent.answer(
            self.database, self.job, questions, policy or self.policy, now, model,
            root=self.root, config=self.config if config is None else config,
            review=review if review is not None else us_review())

    def key(self, question):
        return applications.question_field(question)


# --------------------------------------------------------------------------------------
# Invariant 2 - authorization and sponsorship are answered by the AI from the recorded map.
# --------------------------------------------------------------------------------------

EU_MEMBERS = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
              "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


def map_reader(code, proposition):
    """A fake answering model that reads the recorded map from its packet as the prompt says:
    a country's own entry, else the EU entry for a member state, else '*'."""
    def respond(question, packet):
        recorded = packet["policy"]["answers"]["work_authorization_by_jurisdiction"]["value"]
        status = recorded.get(code) or (recorded.get("EU") if code in EU_MEMBERS else None) or recorded["*"]
        if proposition == "HOLDS":
            truth = status in {"AUTHORIZED_NO_SPONSORSHIP_REQUIRED", "AUTHORIZED_SPONSORSHIP_REQUIRED"}
            question_class = "WORK_AUTHORIZATION_HELD"
        else:
            truth = status != "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"
            question_class = "SPONSORSHIP_REQUIREMENT"
        return entry(question, question_class=question_class, status="ANSWERED", value=truth,
                     jurisdiction=code, unknown_reason=None, derivation="map: " + status,
                     attribution=[{"pointer": "/policy/answers/work_authorization_by_jurisdiction/value",
                                   "quote": status}])
    return respond


class AuthorizationByModelTests(AnsweringFixture):
    """Owner, 2026-10-21 ("remove those blockers"): the answering AI reads the jurisdiction
    map itself, the EU line included; Python only fits its answer to the control."""

    def set_map(self, mapping):
        self.policy["answers"]["work_authorization_by_jurisdiction"]["value"] = mapping

    def test_an_eu_line_answers_a_german_question_authorized(self):
        self.set_map({"*": "NOT_AUTHORIZED", "US": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                      "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        sponsorship = dict(EU_SPONSORSHIP, id="q-de-sponsor", semantic="requires_sponsorship_in_germany",
                           label="Will you require visa sponsorship to work in Germany?")
        holds, needs = map_reader("DE", "HOLDS"), map_reader("DE", "SPONSORSHIP")
        model = FakeModel(lambda q, packet: (holds if q["semantic"] == GERMANY_AUTHORIZATION["semantic"]
                                             else needs)(q, packet))
        result = self.run_answer([GERMANY_AUTHORIZATION, sponsorship], model, review=eu_review())
        self.assertEqual(result["answers"][self.key(GERMANY_AUTHORIZATION)]["value"], "Yes")
        self.assertEqual(result["answers"][self.key(sponsorship)]["value"], "No")
        self.assertEqual(result["unknown"], [])

    def test_a_country_entry_wins_over_the_eu_line(self):
        self.set_map({"*": "NOT_AUTHORIZED", "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                      "DE": "NOT_AUTHORIZED"})
        result = self.run_answer([GERMANY_AUTHORIZATION], FakeModel(map_reader("DE", "HOLDS")),
                                 review=eu_review())
        self.assertEqual(result["answers"][self.key(GERMANY_AUTHORIZATION)]["value"], "No")

    def test_the_default_entry_covers_a_place_nothing_else_does(self):
        self.set_map({"*": "NOT_AUTHORIZED", "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        question = dict(GERMANY_AUTHORIZATION, id="q-jp", semantic="currently_authorized_to_work_in_japan",
                        label="Are you legally authorized to work in Japan?")
        result = self.run_answer([question], FakeModel(map_reader("JP", "HOLDS")), review=eu_review(country="JP"))
        self.assertEqual(result["answers"][self.key(question)]["value"], "No")

    def test_python_no_longer_computes_a_deferred_answer(self):
        """The deferral status is gone from the contract: an output using it is refused."""
        model = FakeModel(lambda q, packet: entry(
            q, question_class="WORK_AUTHORIZATION_HELD", status="DEFER_TO_POLICY_COMPUTATION",
            jurisdiction="DE", assertion="HOLDS_AUTHORIZATION_IN_JURISDICTION", unknown_reason=None))
        result = self.run_answer([GERMANY_AUTHORIZATION], model, review=eu_review())
        self.assertNotIn(self.key(GERMANY_AUTHORIZATION), result.get("answers") or {})


class FabricationTests(AnsweringFixture):
    """Adversarial model output. Every one of these is a plausible, confident falsehood."""

    def fabricate(self, **overrides):
        model = FakeModel(lambda question, packet: entry(question, **overrides))
        return model

    def test_a_quote_that_does_not_occur_at_its_pointer_no_longer_discards(self):
        """Owner, 2026-09-27: the quote dependency is gone; only the pointer must resolve."""
        result = self.run_answer([NO_BASIS], self.fabricate(
            question_class="CANDIDATE_HISTORY_FACT", status="ANSWERED", value=9,
            unknown_reason=None, derivation="Nine years.",
            attribution=[{"pointer": "/policy/answers/full_name/value", "quote": "9 years"}]))
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["audit"]["verdicts"][0]["outcome"], "ANSWERED")

    def test_a_substituted_packet_hash_is_not_what_rejects_the_output(self):
        # Owner, 2026-10-07 (item 10): the echo is no longer checked; an empty answer set
        # still fails, on coverage.
        model = FakeModel(raw=json.dumps({"packet_sha256": "0" * 64, "answers": []}))
        result = self.run_answer([NO_BASIS], model)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_COVERAGE_INVALID")
        self.assertEqual(result["unknown"], [self.key(NO_BASIS)])

    def test_an_extra_or_missing_question_rejects_the_whole_output(self):
        def respond(question, packet):
            return entry(question)
        model = FakeModel(respond)
        original = model.__call__

        def smuggle(prompt):
            raw = json.loads(original(prompt))
            raw["answers"].append(dict(raw["answers"][0], question_key="invented-question"))
            return json.dumps(raw)
        result = answering_agent.answer(self.database, self.job, [NO_BASIS], self.policy, NOW,
                                        smuggle, root=self.root, config=self.config,
                                        review=us_review())
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_COVERAGE_INVALID")
        self.assertEqual(result["unknown"], [self.key(NO_BASIS)])

# --------------------------------------------------------------------------------------
# Invariant 1 - attribution or nothing; and the class rule that keeps a preference from
# becoming a fact.
# --------------------------------------------------------------------------------------

class AttributionTests(AnsweringFixture):
    def test_the_office_attendance_question_is_answered_from_the_location_guidance(self):
        """The question no fixed policy key could ever match."""
        model = FakeModel(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            derivation="Springfield, IL is one of the offices and the top-ranked hybrid/onsite preference.",
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"},
                         {"pointer": "/review/facts/city", "quote": "Springfield, IL"}]))
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        record = result["answers"][self.key(OFFICE_ATTENDANCE)]
        self.assertEqual(record["value"], "Yes")
        self.assertEqual(record["semantic"], OFFICE_ATTENDANCE["semantic"])
        self.assertIn("/policy/location_priority/0",
                      [citation["pointer"] for citation in record["attribution"]])
        self.assertIn("cites=/policy/location_priority/0", record["source"])

    def test_the_answer_is_keyed_by_the_same_null_safe_key_resolve_answers_uses(self):
        """Four of five questions in the only real review have a null id."""
        self.assertIsNone(OFFICE_ATTENDANCE["id"])
        key = self.key(OFFICE_ATTENDANCE)
        self.assertTrue(key.startswith("unidentified-question:"))
        model = FakeModel(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        self.assertIn(key, result["answers"])
        self.assertEqual(result["answers"][key]["question_id"], None)
        scoped = dict(self.policy, answers=dict(self.policy["answers"]))
        scoped["answers"][key] = {"semantic": OFFICE_ATTENDANCE["semantic"],
                                  "value": "Yes", "source": result["answers"][key]["source"]}
        answers, missing = applications.resolve_answers([OFFICE_ATTENDANCE], scoped)
        self.assertEqual(missing, [])
        self.assertEqual(answers[key], "Yes")

    def test_our_pointer_resolution_agrees_with_the_browsers(self):
        import application_browser
        packet = {"policy": {"answers": {"a~b": {"value": [1, {"c/d": "x"}]}}}, "list": [7, 8]}
        for pointer in ("/list/1", "/policy/answers/a~0b/value/1/c~1d", "/policy/answers/a~0b/value/0"):
            self.assertEqual(answering_agent.at_pointer(packet, pointer),
                             application_browser.at_pointer(packet, pointer))

    def test_the_recorded_floor_is_absent_from_the_packet_entirely(self):
        model = FakeModel()
        self.run_answer([NO_BASIS], model)
        packet = model.packets[0]
        self.assertNotIn("minimum_base_salary_usd", packet["policy"])
        self.assertNotIn("salary_relaxation", packet["policy"])
        # The two floor KEYS, not the digits (owner, 2026-09-22). This asserted the literal
        # "180000" was absent anywhere in the packet, which was only ever true by coincidence:
        # the example profile's desired band starts at 180000, so the floor's value and the desired
        # band's lower bound are the same number. `desired_base_salary_usd` BELONGS in the
        # packet - it is what the model answers a compensation question from - so matching on
        # the digits tested the wrong thing and would fail on any policy where the two agree.
        self.assertNotIn("minimum_base_salary_usd", json.dumps(packet))
        self.assertNotIn("salary_relaxation", json.dumps(packet))

    def test_the_model_supplies_the_truth_and_python_still_supplies_the_wording(self):
        """The surviving half of the old rule, after the owner's 2026-09-22 decision.

        The model decides every answer on the form now except authorization and sponsorship
        (owner, 2026-09-22). It still never decides what string goes into the control. `_boolean_literal` reads the affirmative off the
        employer's own `choices`, so a form whose affirmative is spelled "Of course"
        receives "Of course" - a string the model never wrote and could not have written,
        because the only thing it returned was the boolean True.
        """
        question = {"id": None, "required": True, "semantic": "office_attendance",
                    "label": "Are you able to be in the office three days a week?",
                    "answer_type": "choice", "choices": ["Of course", "No"]}
        model = FakeModel(lambda q, packet: entry(
            q, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        record = self.run_answer([question], model)["answers"][self.key(question)]
        self.assertEqual(record["value"], "Of course")
        self.assertEqual(record["basis"], "MODEL_ANSWERED")

class WillingnessTests(AnsweringFixture):
    def willingness_pointer(self):
        for key, recorded in self.policy["answers"].items():
            if recorded.get("semantic") == "relocation_destinations_in_ranked_order":
                return "/policy/answers/" + key + "/value/2"
        raise AssertionError("the recorded relocation preference is missing")

class NeverAnsweredTests(AnsweringFixture):
    # Gender is deliberately absent: the owner explicitly disclosed it on 2026-09-18 and it is
    # recorded in `profile.json` with that provenance, so it is answered from the
    # recorded value and from nothing else. `ExplicitSelfIdentificationTests` below pins both
    # halves of that - the one family that is answered and the many that are not.
    SENSITIVE = [
        ("race_ethnicity", "Please select your race/ethnicity"),
        ("hispanic_or_latino", "Are you Hispanic or Latino?"),
        ("veteran_status", "Protected veteran status"),
        ("disability_status", "Voluntary self-identification of disability (CC-305)"),
        ("transgender", "Do you identify as transgender?"),
        ("sexual_orientation", "What is your sexual orientation?"),
        ("eeo_pronouns", "EEO: preferred pronouns"),
        ("certify_true", "I certify that the information provided is true and complete"),
        ("agree_terms", "I agree to the terms and conditions"),
        ("background_check", "Do you authorize a background check?"),
        ("esignature", "Electronic signature"),
    ]



    def test_the_resume_upload_is_never_an_ai_answer(self):
        policy = json.loads(json.dumps(self.policy))
        policy["answers"].pop("resume")
        question = {"id": "q-resume", "required": True, "answer_type": "text", "choices": [],
                    "semantic": "resume", "label": "Resume/CV"}
        result = self.run_answer([question], FakeModel(), policy=policy)
        self.assertEqual(result["refused"],
                         [{"question_key": self.key(question),
                           "reason": "DESIGNATED_RESUME_ATTACHED_OUT_OF_BAND"}])

    def test_answers_unknown_and_refused_partition_the_required_questions(self):
        questions = [OFFICE_ATTENDANCE, NO_BASIS,
                     {"id": "q-eeo", "required": True, "answer_type": "text", "choices": [],
                      "semantic": "gender_identity", "label": "Gender identity"},
                     {"id": "q-optional", "required": False, "answer_type": "text",
                      "choices": [], "semantic": "website", "label": "Website"}]
        result = self.run_answer(questions, FakeModel())
        covered = (set(result["answers"]) | set(result["unknown"])
                   | {item["question_key"] for item in result["refused"]})
        self.assertEqual(covered, {self.key(question) for question in questions[:3]})
        self.assertEqual(len(result["unknown"]), len(set(result["unknown"])))


# --------------------------------------------------------------------------------------
# Invariant 5 - the start date is computed in Python.
# --------------------------------------------------------------------------------------

class StartDateTests(AnsweringFixture):
    START = {"id": "q-start", "required": True, "answer_type": "text", "choices": [],
             "semantic": "earliest_start_date", "label": "What is your earliest start date?"}

    def test_the_start_date_is_the_recorded_rule_applied_to_the_application_date(self):
        model = FakeModel(lambda q, packet: entry(
            q, question_class="AVAILABILITY", status="ANSWERED", value="2026-09-18",
            unknown_reason=None,
            attribution=[{"pointer": "/policy/answers/notice_period/value", "quote": "2 weeks"}]))
        result = self.run_answer([self.START], model)
        record = result["answers"][self.key(self.START)]
        self.assertEqual(record["value"], "2026-10-15")
        self.assertEqual(record["value"], applications.earliest_start_date(NOW, self.policy))
        self.assertTrue(record["source"].startswith(applications.COMPUTED_ANSWER_SOURCE_PREFIX))
        self.assertEqual(model.prompts, [], "the model is never asked for a start date")

    def test_the_floor_moves_with_the_application_date_and_is_never_earlier(self):
        for day, expected in (("2026-09-17T16:00:00+00:00", "2026-10-15"),
                              ("2026-10-01T00:00:00+00:00", "2026-10-29")):
            result = self.run_answer([self.START], FakeModel(), now=day)
            value = result["answers"][self.key(self.START)]["value"]
            self.assertEqual(value, expected)
            self.assertGreaterEqual(value, str(day)[:10])
            self.assertEqual(
                (__import__("datetime").date.fromisoformat(value)
                 - __import__("datetime").date.fromisoformat(str(day)[:10])).days, 28)

    def test_without_a_recorded_rule_the_owner_is_asked_rather_than_the_model(self):
        policy = json.loads(json.dumps(self.policy))
        policy["answers"].pop("earliest_start_date_rule")
        model = FakeModel()
        result = self.run_answer([self.START], model, policy=policy)
        self.assertEqual(result["unknown"], [self.key(self.START)])
        self.assertEqual(model.prompts, [])


# --------------------------------------------------------------------------------------
# Invariant 7 - fail safe. Nothing here may ever become a guess.
# --------------------------------------------------------------------------------------

class DegradationTests(AnsweringFixture):
    def assert_degraded(self, model, outcome):
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["unknown"], [self.key(OFFICE_ATTENDANCE)])
        self.assertEqual(result["audit"]["outcome"], outcome)
        return result

    def test_an_unavailable_executor_degrades_to_the_fact_request_path(self):
        self.assert_degraded(FakeModel(error=OSError("no executor")),
                             "ANSWERING_EXECUTOR_FAILED")

    def test_a_timeout_degrades_to_the_fact_request_path(self):
        self.assert_degraded(FakeModel(error=TimeoutError("deadline")), "ANSWERING_TIMEOUT")

    def test_malformed_output_degrades_to_the_fact_request_path(self):
        self.assert_degraded(FakeModel(raw="not json at all"), "ANSWERING_OUTPUT_NOT_JSON")

    def test_an_empty_response_degrades_to_the_fact_request_path(self):
        self.assert_degraded(FakeModel(raw="   "), "ANSWERING_OUTPUT_MISSING")

    def test_an_oversized_response_degrades_to_the_fact_request_path(self):
        self.assert_degraded(FakeModel(raw="x" * (answering_agent.MAX_OUTPUT_BYTES + 1)),
                             "ANSWERING_OUTPUT_EXCEEDS_LIMIT")

    def test_an_unclassified_envelope_degrades_to_the_fact_request_path(self):
        # The packet_sha256 echo is optional since 2026-10-07; the answers are not.
        self.assert_degraded(FakeModel(raw=json.dumps({"packet_sha256": "0" * 64})),
                             "ANSWERING_OUTPUT_ENVELOPE_INVALID")

    def test_a_compute_wait_consumes_nothing_writes_nothing_and_fails_nothing(self):
        with self.database:
            workflow_tasks.reserve_compute(self.database, "synthetic-whole-day", NOW, 10800, "UTC")
        model = FakeModel()
        result = self.assert_degraded(model, "ANSWERING_COMPUTE_BUDGET_EXHAUSTED")
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM worker_compute_reservations WHERE action_id LIKE 'application-answering:%'"
        ).fetchone()[0], 0)
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM application_answers").fetchone()[0], 0)
        self.assertIsNone(result["audit"]["raw"])

    def test_the_step_is_off_unless_it_is_explicitly_enabled(self):
        for config in ({}, {"application_answering": {}},
                       {"application_answering": {"enabled": "true"}},
                       {"application_answering": {"enabled": 1}}):
            model = FakeModel()
            result = self.run_answer([OFFICE_ATTENDANCE], model, config=config)
            self.assertEqual(result["audit"]["outcome"], "DISABLED")
            self.assertEqual(model.prompts, [])
            self.assertEqual(result["unknown"], [self.key(OFFICE_ATTENDANCE)])

    def test_materialize_never_raises_whatever_the_step_does(self):
        def explode(prompt):
            raise MemoryError("catastrophic")
        scoped = dict(self.policy, answers=dict(self.policy["answers"]))
        result = answering_agent.materialize(self.database, self.job, [OFFICE_ATTENDANCE],
                                             scoped, NOW, explode, root=self.root,
                                             config=self.config, review=us_review())
        self.assertEqual(result["answers"], {})
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM application_answers").fetchone()[0], 0)

    def test_a_browser_adapter_is_never_mistaken_for_the_model(self):
        adapter = MockATS(self.root)
        self.assertIsNone(answering_agent.injected_executor(adapter))
        self.assertIsNone(answering_agent.injected_executor(
            applications.CommandAdapter({"application_adapter_command": ["/bin/true"]})))
        result = self.run_answer([OFFICE_ATTENDANCE], adapter)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_EXECUTOR_NOT_CONFIGURED")
        self.assertEqual(adapter.calls, [])


# --------------------------------------------------------------------------------------
# Invariant 6 - raw model output preserved beside a normalized audit.
# --------------------------------------------------------------------------------------

class AuditTests(AnsweringFixture):
    def attempt(self):
        model = FakeModel(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        row = self.database.execute(
            "SELECT * FROM application_answering_attempts WHERE job_key=?",
            (self.job["job_key"],)).fetchone()
        return model, result, row

    def test_the_raw_output_is_stored_verbatim_with_its_hash_and_packet_binding(self):
        model, result, row = self.attempt()
        self.assertEqual(row["raw"], result["audit"]["raw"])
        self.assertEqual(json.loads(row["raw"])["packet_sha256"], row["packet_sha256"])
        self.assertEqual(row["raw_sha256"],
                         hashlib.sha256(row["raw"].encode("utf-8")).hexdigest())
        self.assertEqual(row["outcome"], "COMPLETE")
        self.assertEqual(row["model"], self.config.get("model"))

    def test_the_normalized_audit_records_a_verdict_per_question(self):
        model, result, row = self.attempt()
        audit = json.loads(row["audit"])
        self.assertEqual([verdict["question_key"] for verdict in audit["verdicts"]],
                         [self.key(OFFICE_ATTENDANCE)])
        self.assertEqual(audit["verdicts"][0]["outcome"], "ANSWERED")
        self.assertEqual(audit["verdicts"][0]["cites"], ["/policy/location_priority/0"],
                         "a preference carries the model's own resolved citations and no "
                         "Python-manufactured ones")

    def test_the_audit_row_cannot_be_rewritten_or_deleted(self):
        model, result, row = self.attempt()
        for statement in ("UPDATE application_answering_attempts SET raw='edited'",
                          "DELETE FROM application_answering_attempts"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.database:
                    self.database.execute(statement)

# --------------------------------------------------------------------------------------
# The seam: invariants 7 and 8 through the real application cycle.
# --------------------------------------------------------------------------------------

class SeamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ("state", "reports", "logs"):
            (self.root / folder).mkdir()
        for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
            (self.root / name).write_text((orchestrator.ROOT / name).read_text())
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        workflow_tasks.initialize(self.database)
        self.policy = json.loads(json.dumps(POLICY))
        (self.root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        self.policy["resume"] = {"path": "mock-resume.pdf", "sha256": hashlib.sha256(
            (self.root / "mock-resume.pdf").read_bytes()).hexdigest()}
        self.policy["answers"]["resume"]["value"] = self.policy["resume"]["path"]
        profile_home.use(self, self.policy)
        self.adapter = MockATS(self.root)
        self.questions = [{"id": "email", "semantic": "contact_email", "required": True},
                          OFFICE_ATTENDANCE]
        self.adapter.customize = lambda result, job: result.update(
            questions=[dict(question) for question in self.questions],
            facts=dict(result["facts"], city="Springfield, IL; Austin, TX",
                       country="US", work_mode="hybrid"))
        self.network = patch("socket.create_connection",
                             side_effect=AssertionError("Tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def cycle(self, config):
        return applications.cycle(self.database, config, self.root, self.adapter, NOW)

    def discover(self):
        orchestrator.store_research(self.database, PIPELINE,
                                    {"summary": "Mock research", "blockers": [],
                                     "opportunities": [role()], "drafts": []})
        return applications.identity(role())[2]

    def job(self, key):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?",
                                     (key,)).fetchone()

    def enabled(self, respond):
        self.adapter.answering_executor = FakeModel(respond)
        return {"application_answering": {"enabled": True, "timeout_seconds": 5},
                "timezone": "UTC"}

    def attempts(self):
        if not self.database.execute(
                "SELECT 1 FROM sqlite_master WHERE name='application_answering_attempts'").fetchone():
            return 0
        return self.database.execute(
            "SELECT COUNT(*) FROM application_answering_attempts").fetchone()[0]


    def test_an_attributed_answer_lets_the_application_complete(self):
        key = self.discover()
        config = self.enabled(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        self.cycle(config)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        submitted = [payload for operation, payload in self.adapter.calls if operation == "submit"]
        field = applications.question_field(OFFICE_ATTENDANCE)
        self.assertEqual(submitted[0]["answers"][field], "Yes")
        row = self.database.execute("SELECT * FROM application_answers WHERE field=?",
                                    (field,)).fetchone()
        self.assertTrue(row["source"].startswith(answering_agent.AI_SOURCE_PREFIX))



    def test_an_owner_supplied_answer_is_never_overwritten_by_the_step(self):
        key = self.discover()
        field = applications.question_field(OFFICE_ATTENDANCE)
        with self.database:
            self.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                  (key, field, OFFICE_ATTENDANCE["semantic"], json.dumps("No"),
                                   "authenticated owner decision"))
        config = self.enabled(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        self.cycle(config)
        row = self.database.execute("SELECT * FROM application_answers WHERE field=?",
                                    (field,)).fetchone()
        self.assertEqual(json.loads(row["value"]), "No")
        self.assertEqual(row["source"], "authenticated owner decision")


    def test_a_duplicate_or_ineligible_job_never_reaches_the_step(self):
        key = self.discover()
        with self.database:
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  (applications.identity(role())[0],
                                   applications.identity(role())[1], "APPLICATION", "historical"))
        config = self.enabled(lambda question, packet: entry(question))
        self.cycle(config)
        self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.adapter.answering_executor.prompts, [])
        self.assertEqual(self.attempts(), 0)


class ContractTests(AnsweringFixture):
    """The shape a parallel adversarial suite is written against."""

    def test_the_documented_keys_are_always_present(self):
        result = self.run_answer([OFFICE_ATTENDANCE], FakeModel())
        for field in ("answers", "unknown", "refused", "audit"):
            self.assertIn(field, result)
        self.assertIsInstance(result["answers"], dict)
        self.assertIsInstance(result["unknown"], list)
        self.assertIsInstance(result["refused"], list)
        self.assertIsInstance(result["audit"], dict)

    def test_every_answer_carries_value_semantic_attribution_and_question_id(self):
        model = FakeModel(lambda question, packet: entry(
            question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        for record in result["answers"].values():
            for field in ("value", "semantic", "attribution", "question_id"):
                self.assertIn(field, record)

    def test_a_recorded_policy_answer_is_reported_with_its_own_provenance(self):
        question = {"id": "email", "required": True, "answer_type": "text", "choices": [],
                    "semantic": "contact_email", "label": "Email"}
        model = FakeModel()
        result = self.run_answer([question], model)
        record = result["answers"][self.key(question)]
        self.assertEqual(record["value"], POLICY["answers"]["email"]["value"])
        self.assertEqual(record["basis"], "RECORDED_POLICY_ANSWER")
        self.assertEqual(model.prompts, [], "a question policy already answers is never asked")


class ShapeTests(AnsweringFixture):
    """The properties that survive a phrasing nobody anticipated.

    The first version of this module gated its safety properties on regular expressions
    matching text an employer writes - the same defect class as `resolve_answers` matching a
    semantic by string equality. An adversarial review broke all nine of them by writing the
    eleventh phrasing, in three languages. The rule now is that the gate is on what the
    answer *asserts*, not on what the question says: Python derives every literal, and a
    value it did not derive is never emitted. These tests pin that shape, not those wordings.
    """

    ESCAPES = [
        "Will you be able to work from Spain from day one?",
        "Do you have the unrestricted right to live and work in the United Kingdom?",
        "Can you commence employment in Germany without restriction?",
        "Is there anything that would prevent you from taking up employment in Germany?",
        "Do you need us to do anything for you to start working in Ireland?",
        "Do you already have the documents needed to be hired in Germany?",
        "Are you free to accept a role based in Berlin without further formalities?",
        "\u00bfTiene permiso de trabajo en Espa\u00f1a?",
        "Besitzen Sie eine Arbeitserlaubnis f\u00fcr Deutschland?",
        "Avez-vous le droit de travailler en France?",
    ]

    def elect(self, label, question_class="IDENTITY_OR_CONTACT", value="Yes"):
        question = {"id": None, "required": True, "semantic": label, "label": label,
                    "answer_type": "choice", "choices": ["Yes", "No"]}
        model = FakeModel(lambda q, packet: entry(
            q, question_class=question_class, status="ANSWERED", value=value,
            unknown_reason=None, derivation="The candidate is authorized.",
            attribution=[{"pointer": "/policy/answers/us_work_authorized/value", "quote": "true"}]))
        return question, self.run_answer([question], model, review=eu_review())

    def test_the_model_supplied_literals_are_never_read(self):
        """F2: a truth rendered through a model-written literal is still a lie."""
        question = {"id": "q-eu", "required": True, "semantic": "eu_authorized",
                    "label": "Are you legally authorized to work in the EU?",
                    "answer_type": "choice", "choices": ["Yes", "No"]}
        model = FakeModel(lambda q, packet: entry(
            q, question_class="WORK_AUTHORIZATION_HELD", status="ANSWERED", value=False,
            affirmative_value="No", negative_value="Yes", unknown_reason=None,
            attribution=[{"pointer": "/questions/0/label", "quote": "EU"}]))
        result = self.run_answer([question], model, review=eu_review())
        self.assertEqual(result["answers"][self.key(question)]["value"], "No")

    def test_a_recorded_relocation_country_yields_onsite_willingness_there(self):
        """Owner decision 2026-09-19: Europe onsite/hybrid is in scope with relocation, and
        Germany is a recorded relocation destination (was: Europe remote only, answered No)."""
        question = {"id": "field-cologne", "required": True, "semantic": "onsite_cologne",
                    "label": "Would you be willing to work on-site in Leipzig?",
                    "answer_type": "choice", "choices": ["Yes", "No"]}
        model = FakeModel(lambda q, packet: entry(
            q, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/3", "quote": "Europe remote"}]))
        result = self.run_answer([question], model, review=eu_review(city="Leipzig",
                                                                    work_mode="onsite"))
        self.assertEqual(result["answers"][self.key(question)]["value"], "Yes")

    def test_a_recorded_affirmative_cannot_be_matched_onto_a_foreign_jurisdiction(self):
        """`resolve_answers` binds on a semantic the browser AI authored, so guard that too.

        The guard lives in `applications.answer_scope_conflict`, inside `resolve_answers`,
        so the recorded `true` never reaches this step at all: the question arrives as one
        nothing recorded answers, and the owner is asked.
        """
        question = {"id": None, "required": True, "semantic": "currently_authorized_to_work_in_us",
                    "label": "Are you authorized to work in Germany?", "answer_type": "choice",
                    "choices": ["Yes", "No"]}
        answers, missing = applications.resolve_answers([question], self.policy)
        self.assertEqual(answers, {})
        self.assertEqual(missing, [self.key(question)])
        result = self.run_answer([question], FakeModel(), review=eu_review())
        self.assertEqual(result["unknown"], [self.key(question)])
        self.assertEqual(result["answers"], {})


class ResolveAnswersScopeGuardTests(unittest.TestCase):
    """The submission path's own guard, which does not depend on the answering step at all.

    `resolve_answers` binds a question to a recorded answer by exact equality on a `semantic`
    that the employer's own browser AI authors freely. A read that labels *"Are you authorized
    to work in Germany?"* with the semantic `currently_authorized_to_work_in_us` therefore got
    the owner's recorded `true`: a false claim of German work authorization, typed into an
    employer's form, on the submission path, with no AI involved. That route existed whether
    or not `answering_agent` was installed, enabled or working - which is exactly the case
    where `resolve_answers` runs alone - so the guard lives in `applications`, reads the
    recorded answer's own declared scope, and is proved here with `answering_agent`
    deliberately unimportable.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ("state", "reports", "logs"):
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        applications.initialize(self.database)
        workflow_tasks.initialize(self.database)
        self.policy = json.loads(json.dumps(POLICY))
        (self.root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        self.policy["resume"] = {"path": "mock-resume.pdf", "sha256": hashlib.sha256(
            (self.root / "mock-resume.pdf").read_bytes()).hexdigest()}
        self.policy["answers"]["resume"]["value"] = self.policy["resume"]["path"]
        profile_home.use(self, self.policy)
        self.adapter = MockATS(self.root)
        self.network = patch("socket.create_connection",
                             side_effect=AssertionError("Tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def resolve(self, semantic, label, text=False):
        question = ({"id": None, "required": True, "semantic": semantic, "label": label, "answer_type": "text"}
                    if text else {"id": None, "required": True, "semantic": semantic, "label": label,
                                  "answer_type": "choice", "choices": ["Yes", "No"]})
        return question, applications.resolve_answers([question], self.policy)

    def test_a_us_authorization_record_cannot_answer_a_german_question(self):
        question, (answers, missing) = self.resolve("currently_authorized_to_work_in_us",
                                                    "Are you authorized to work in Germany?")
        self.assertEqual(answers, {})
        self.assertEqual(missing, [applications.question_field(question)])

    def test_an_authorization_record_is_left_to_the_answering_ai(self):
        """Owner, 2026-10-07 (item 6): Python no longer applies an authorization record by
        semantic, even to its own US question; the answering AI reads the question."""
        for label in ("Are you authorized to work in the US?", "Are you authorized to work?"):
            question, (answers, missing) = self.resolve("currently_authorized_to_work_in_us", label)
            self.assertEqual(answers, {}, label)
            self.assertEqual(missing, [applications.question_field(question)], label)

    def test_an_eu_record_can_never_answer_a_uk_question_or_the_reverse(self):
        """The UK is not in the EU and has its own immigration system."""
        for semantic, label in (("currently_authorized_to_work_in_eu",
                                 "Do you have the right to work in the United Kingdom?"),
                                ("currently_authorized_to_work_in_uk",
                                 "Are you legally authorized to work in the EU?")):
            question, (answers, missing) = self.resolve(semantic, label)
            self.assertEqual(answers, {}, label)
            self.assertEqual(missing, [applications.question_field(question)], label)

    def test_the_us_sponsorship_record_cannot_answer_a_european_sponsorship_question(self):
        question, (answers, missing) = self.resolve(
            "requires_visa_sponsorship_now_or_future_in_us",
            "Will you now or in the future require visa sponsorship to work in Germany?")
        self.assertEqual(answers, {})
        self.assertEqual(missing, [applications.question_field(question)])

    def test_the_outside_us_sponsorship_record_cannot_answer_a_us_question(self):
        question, (answers, missing) = self.resolve(
            "requires_visa_sponsorship_now_or_future_outside_us",
            "Will you now or in the future require sponsorship in the United States?")
        self.assertEqual(answers, {})
        self.assertEqual(missing, [applications.question_field(question)])

    def test_the_jurisdiction_map_object_is_never_typed_into_a_form(self):
        question, (answers, missing) = self.resolve("work_authorization_by_jurisdiction",
                                                    "Work authorization")
        self.assertEqual(answers, {})
        self.assertEqual(missing, [applications.question_field(question)])

    def test_a_non_jurisdictional_record_is_untouched_by_the_guard(self):
        """Willingness, identity and every other recorded answer behave exactly as before."""
        for semantic, label, expected in (
                ("contact_email", "Email", POLICY["answers"]["email"]["value"]),
                # (a recorded yes/no such as willing_to_relocate is left to the answering AI
                # since 2026-10-07, item 6)
                ("notice_period", "What is your notice period?", "2 weeks")):
            # A quoted fact on a text control (a yes/no control goes to the AI, review round 3).
            question, (answers, missing) = self.resolve(semantic, label, text=True)
            self.assertEqual(missing, [], label)
            self.assertEqual(answers[applications.question_field(question)], expected, label)


    def test_the_answering_step_and_the_matcher_share_one_jurisdiction_vocabulary(self):
        """Two copies of this table is two things to forget to update."""
        self.assertIs(answering_agent.JURISDICTION_TOKENS, applications.JURISDICTION_TOKENS)
        self.assertIs(answering_agent.jurisdictions_named, applications.jurisdictions_named)
        self.assertIs(answering_agent.jurisdiction_of, applications.jurisdiction_of)
        self.assertIs(answering_agent.question_text, applications.question_text)


class SingleGateTests(AnsweringFixture):
    """One gate, on the emission path, that every producer passes.

    Round 2 of the adversarial review found the same structural mistake twice: the checks
    lived inside one producer, and there were three. `resolve_answers` walked around them
    (F10) and the recorded-answer short circuit walked around them again (F12). `admit` is
    now the only way an answer enters `answers`, from any producer, and the recorded-answer
    check it shares with `resolve_answers` lives in `applications` so the matcher cannot
    disagree with it.
    """

    UK_QUESTION = {"id": None, "required": True, "answer_type": "text", "choices": [],
                   "semantic": "Do you have the unrestricted right to live and work in the United Kingdom?",
                   "label": "Do you have the unrestricted right to live and work in the United Kingdom?"}

    def test_a_negated_question_is_refused_on_the_recorded_answer_path_as_well(self):
        """F12: the polarity net ran on the model path only, and the model was never called."""
        for semantic, label in (("currently_authorized_to_work_in_eu",
                                 "Are you NOT authorized to work in the EU?"),
                                ("currently_authorized_to_work_in_uk",
                                 "Do you lack the right to work in the United Kingdom?"),
                                ("currently_authorized_to_work_in_us",
                                 "Are you NOT authorized to work in the US?")):
            question = {"id": None, "required": True, "semantic": semantic, "label": label,
                        "answer_type": "choice", "choices": ["Yes", "No"]}
            answers, missing = applications.resolve_answers([question], self.policy)
            self.assertEqual(answers, {}, label)
            self.assertEqual(missing, [self.key(question)], label)
            result = self.run_answer([question], FakeModel(), review=eu_review())
            self.assertNotIn(self.key(question), result["answers"], label)

    def test_the_matcher_leaves_asserted_facts_to_the_answering_step(self):
        # Owner, 2026-10-07 (item 6): decided from the recorded answer alone, never the question.
        self.assertIs(applications.recorded_answer_for_ai(self.policy["answers"]["us_work_authorized"]), True)
        self.assertIs(applications.recorded_answer_for_ai(self.policy["answers"]["email"]), False)

    def test_the_willingness_computation_is_still_right_when_nothing_is_named(self):
        """The mirror control: the fix is about which input is read, not about refusing."""
        question = {"id": None, "required": True, "semantic": "relocate_to_listed_office",
                    "label": "If the role needs it, would you move to the office "
                             "this posting names?",
                    "answer_type": "choice", "choices": ["Yes", "No"]}
        model = FakeModel(lambda q, packet: entry(
            q, question_class="LOCATION_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}]))
        result = self.run_answer([question], model)
        self.assertEqual(result["answers"][self.key(question)]["value"], "Yes")

    def test_a_named_place_that_matches_the_role_still_answers(self):
        """"NYC" and "DC" on a posting recorded at both must still be answerable."""
        result = self.run_answer([OFFICE_ATTENDANCE], FakeModel(lambda q, packet: entry(
            q, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
            unknown_reason=None,
            attribution=[{"pointer": "/policy/location_priority/0", "quote": "Springfield, IL hybrid/onsite"}])))
        self.assertEqual(result["answers"][self.key(OFFICE_ATTENDANCE)]["value"], "Yes")

class SavedProductionReadTests(unittest.TestCase):
    """The guard, against the questions production actually produced.

    Every one of the 49 saved browser results predates the contract change that adds the
    employer's verbatim `label`, so every one of them records only an authored `semantic`.
    That is the state the first real forms arrive in, and it is exactly the state in which an
    earlier revision of `answer_scope_conflict` returned `False` - "no jurisdiction named,
    therefore no conflict" - which made a missing label a satisfied guard.

    These tests use a saved-shape `result.json` fixture (the stored review's questions, which
    carry no label), because the missing label is the thing under test.
    """

    SAVED = (Path(__file__).resolve().parent
             / "fixtures/application-browser/request-fixt0008/result.json")

    def setUp(self):
        self.saved = json.loads(self.SAVED.read_text())
        self.questions = [question for question in self.saved.get("questions") or []
                          if isinstance(question, dict) and question.get("required")]
        self.assertTrue(self.questions, "the saved read records no required questions")
        self.policy = json.loads(json.dumps(POLICY))

    def test_the_saved_read_really_does_record_no_label(self):
        """The premise, asserted rather than assumed."""
        self.assertTrue(all("label" not in question for question in self.questions))
        self.assertEqual(sum(1 for question in self.questions if question.get("id") is None), 4)

    def test_a_jurisdictional_record_binds_to_none_of_them(self):
        """The finding: absent evidence must not be passing evidence.

        For each real saved question, the browser AI is taken to have authored the semantic of
        a jurisdiction-scoped recorded answer - which is the whole F10 scenario, and the one
        thing the authored semantic makes possible. With no label there is no way to tell what
        was asked, so the recorded answer must not bind.
        """
        scoped = ["currently_authorized_to_work_in_us", "currently_authorized_to_work_in_eu",
                  "currently_authorized_to_work_in_uk",
                  "requires_visa_sponsorship_now_or_future_in_us",
                  "requires_visa_sponsorship_now_or_future_outside_us"]
        for question in self.questions:
            for semantic in scoped:
                # The saved object verbatim, with the one field the browser AI authors freely
                # swapped for a jurisdiction-scoped one. That substitution is the whole F10
                # scenario and the only thing an authored semantic makes possible; everything
                # else about the question - no label, null id - is exactly as production
                # recorded it.
                candidate = dict(question, semantic=semantic)
                answers, missing = applications.resolve_answers([candidate], self.policy)
                self.assertEqual(
                    answers, {},
                    "a " + semantic + " record bound to a saved question with no recorded "
                    "wording: " + repr(candidate))
                self.assertEqual(missing, [applications.question_field(candidate)])

    def test_the_non_jurisdictional_saved_answers_are_unaffected(self):
        """The mirror control: narrowing the guard must not break the ordinary path.

        Four of the five saved questions are identity, resume and profile fields whose
        semantics match recorded answers. Those still resolve, because their recorded answers
        are not jurisdictional and nothing about them depends on reading the question.
        """
        policy = json.loads(json.dumps(self.policy))
        policy["resume"] = {"path": policy["answers"]["resume"]["value"], "sha256": "unused"}
        answers, missing = applications.resolve_answers(
            [dict(question) for question in self.questions], policy)
        resolved = {question["semantic"] for question in self.questions
                    if applications.question_field(question) in answers}
        self.assertEqual(resolved, {"legal_or_resume_full_name", "contact_email", "resume",
                                    "linkedin_profile"})
        self.assertEqual(missing, [applications.question_field(question)
                                   for question in self.questions
                                   if question["semantic"]
                                   == "office_attendance_2_3_days_per_week_in_springfield_or_austin"])

class FailClosedTests(AnsweringFixture):
    """Absent or unparseable evidence must never be permissive evidence.

    Three findings in a row had this shape - `resolve_answers` reachable around the gate, the
    policy short circuit reachable around it, and then a missing label read as a satisfied
    guard. These pin the sweep that followed: every place in the two guarded files where a
    check could return "no problem" because it could not see or could not parse its input.
    """

    def test_an_unclassified_jurisdictional_record_does_not_bind(self):
        """A future `currently_authorized_to_work_in_japan` must be classified deliberately."""
        policy = json.loads(json.dumps(self.policy))
        policy["answers"]["jp"] = {"semantic": "currently_authorized_to_work_in_japan",
                                   "value": True, "source": "hypothetical future entry"}
        question = {"id": None, "required": True,
                    "semantic": "currently_authorized_to_work_in_japan",
                    "label": "Are you authorized to work in Japan?",
                    "answer_type": "choice", "choices": ["Yes", "No"]}
        answers, missing = applications.resolve_answers([question], policy)
        self.assertEqual(answers, {})
        self.assertEqual(missing, [self.key(question)])


# --------------------------------------------------------------------------------------
# Invariant 9 - an experience figure is COMPUTED from the resume's own dates, never quoted
# and never supplied by the model.
#
# The owner asked for "How many years of Kubernetes experience do you have?" to be answered
# from his resume. It cannot be a quotation: the resume says "September 2016 - February 2021"
# and "Kubernetes operators", and no substring of that is the answer. So the model says which dated
# sections bear on the question, and Python does the subtraction and applies the bounds. The
# tests below pin each bound the owner named, and the attacks that would walk around them.
# --------------------------------------------------------------------------------------

RESUME_TAILSPIN_HEADER = "Backend Engineer II | September 2016 \u2013 February 2021"
RESUME_TAILSPIN_KUBERNETES = "Kubernetes operators"
RESUME_FABRIKAM_HEADER = "Staff Platform Engineer | March 2021 \u2013 August 2025"
RESUME_SKILLS_LINE = "Infrastructure: Kubernetes, Terraform, Helm, Linux"

KUBERNETES_YEARS = {"id": "q-k8s-years", "required": True, "answer_type": "number", "choices": [],
                    "semantic": "years_of_kubernetes_experience",
                    "label": "How many years of Kubernetes experience do you have?"}
TOTAL_YEARS = {"id": "q-total", "required": True, "answer_type": "number", "choices": [],
               "semantic": "years_of_experience",
               "label": "How many years of professional software engineering experience do you have?"}


def resume_entry(question, assertion, quotes, **overrides):
    return entry(question, question_class="COMPUTED_EXPERIENCE",
                 status="DEFER_TO_POLICY_COMPUTATION", assertion=assertion, unknown_reason=None,
                 attribution=[{"pointer": "/candidate_resume/text", "quote": quote}
                              for quote in quotes],
                 derivation="the dated sections that bear on this question", **overrides)


class ResumeFixture(AnsweringFixture):
    """The standard fixture plus the designated resume, so the packet actually carries it."""

    def setUp(self):
        super().setUp()
        import reply_resume
        target = self.root / reply_resume.source()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((orchestrator.ROOT / reply_resume.source()).read_bytes())
        text = self.root / user_profile.load()["resume_raw"]["text_path"]
        text.write_bytes((orchestrator.ROOT / user_profile.load()["resume_raw"]["text_path"]).read_bytes())
        self.document = reply_resume.context(self.root)

    def value_for(self, question, model, **kwargs):
        result = self.run_answer([question], model, **kwargs)
        key = self.key(question)
        if key in result["answers"]:
            return result["answers"][key], result
        return None, result

    def reason_for(self, result, question):
        return next((item["reason"] for item in result["audit"]["verdicts"]
                     if item["question_key"] == self.key(question)), result["audit"]["outcome"])


class ComputedExperienceTests(ResumeFixture):
    def test_without_the_resume_in_the_packet_the_owner_is_asked(self):
        import reply_resume
        (self.root / reply_resume.source()).unlink()
        record, result = self.value_for(KUBERNETES_YEARS, FakeModel(
            lambda q, packet: resume_entry(q, "EXPERIENCE_WITH_TECHNOLOGY_IN_RESUME",
                                           [RESUME_TAILSPIN_KUBERNETES])))
        self.assertIsNone(record)
        self.assertIn(self.key(KUBERNETES_YEARS), result["unknown"])


class ExplicitSelfIdentificationTests(ResumeFixture):
    """Gender, from the owner's recorded value, answered by the AI (no Python routing since 2026-10-21)."""

    GENDER_CHOICE = {"id": "q-gender", "required": True, "answer_type": "choice",
                     "choices": ["Male", "Female", "Decline to self-identify"],
                     "semantic": "gender", "label": "Gender"}
    GENDER_TEXT = {"id": "q-gender-text", "required": True, "answer_type": "text", "choices": [],
                   "semantic": "gender_identity", "label": "What is your gender identity?"}
    def gender_reader(self, q, packet):
        """A model that answers from the owner's recorded gender in its packet, as the prompt says."""
        recorded = next(saved["value"] for saved in packet["policy"]["answers"].values()
                        if saved.get("semantic") == "gender_self_identification")
        return entry(q, question_class="DEMOGRAPHIC_VOLUNTARY", status="ANSWERED", value=recorded,
                     unknown_reason=None, derivation="the recorded self-identification")

    def test_gender_goes_to_the_ai_with_the_recorded_value_in_its_packet(self):
        """Owner, 2026-10-21: no Python routing by the question's wording; the AI answers it."""
        for question in (self.GENDER_CHOICE, self.GENDER_TEXT):
            with self.subTest(label=question["label"]):
                model = FakeModel(self.gender_reader)
                result = self.run_answer([question], model)
                record = result["answers"][self.key(question)]
                self.assertEqual(record["value"], "Male")
                self.assertTrue(record["source"].startswith(answering_agent.AI_SOURCE_PREFIX))
                self.assertEqual(len(model.prompts), 1)
                self.assertEqual(len(model.judge_prompts), 1)

    def test_the_ai_answer_is_written_where_the_submission_path_looks_for_it(self):
        scoped = json.loads(json.dumps(self.policy))
        result = answering_agent.materialize(
            self.database, self.job, [self.GENDER_CHOICE], scoped, NOW, FakeModel(self.gender_reader),
            root=self.root, config=self.config, review=us_review())
        self.assertEqual(result["written"], [self.key(self.GENDER_CHOICE)])
        answers, missing = applications.resolve_answers([self.GENDER_CHOICE], scoped)
        self.assertEqual(answers[self.key(self.GENDER_CHOICE)], "Male")
        self.assertEqual(missing, [])

    def test_an_optional_self_identification_never_reaches_the_step_or_holds_the_job(self):
        optional = dict(self.GENDER_TEXT, required=False, id="q-optional-eeo")
        veteran = {"id": "q-optional-vet", "required": False, "answer_type": "choice",
                   "choices": ["Yes", "No"], "semantic": "veteran_status",
                   "label": "Protected veteran status"}
        result = self.run_answer([OFFICE_ATTENDANCE, optional, veteran], FakeModel(
            lambda q, packet: entry(
                q, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
                unknown_reason=None,
                attribution=[{"pointer": "/policy/location_priority/0",
                              "quote": "Springfield, IL hybrid/onsite"}])))
        for question in (optional, veteran):
            key = self.key(question)
            self.assertNotIn(key, result["unknown"])
            self.assertNotIn(key, {item["question_key"] for item in result["refused"]})
        answers, missing = applications.resolve_answers([OFFICE_ATTENDANCE, optional, veteran],
                                                        self.policy)
        self.assertNotIn(self.key(optional), missing)
        self.assertNotIn(self.key(veteran), missing)

# --------------------------------------------------------------------------------------
# Invariant 10 - a composed paragraph is checked for what it ASSERTS, not for where each
# phrase came from.
#
# The owner asked for this twice and accepted that it will not be perfectly accurate. So
# attribution is an audit record here and a mediocre paragraph is an accepted outcome. What
# is not accepted, and what these tests pin, is an employer, school, degree, certification,
# duration, year, salary or work-authorization claim that his context does not carry.
# --------------------------------------------------------------------------------------

WHY_THIS_COMPANY = {"id": None, "required": True, "answer_type": "text", "choices": [],
                    "semantic": "why_this_company",
                    "label": "Why do you want to work here?"}
GROUNDED_PARAGRAPH = (
    "I have spent the last nine years building freight and warehouse software at Tailspin "
    "and Fabrikam, most recently owning the streaming inventory pipeline for Fabrikam. That "
    "work maps directly onto the reliability problems this team is solving, and I am looking "
    "for a role where deep ownership of a hard backend problem is the job.")


class ComposedFreeTextTests(ResumeFixture):
    def compose(self, value, question=None, attribution=None):
        question = question or WHY_THIS_COMPANY
        return self.value_for(question, FakeModel(lambda q, packet: entry(
            q, question_class="ROLE_SPECIFIC_FREE_TEXT", status="ANSWERED", value=value,
            unknown_reason=None, derivation="composed from the resume and the role",
            attribution=attribution if attribution is not None else [])))

    def test_a_bounded_control_is_never_answered_with_prose(self):
        question = {"id": "q-bounded", "required": True, "answer_type": "choice",
                    "choices": ["Yes", "No"], "semantic": "previously_applied",
                    "label": "Have you previously applied to this company?"}
        record, _ = self.compose("I have not applied to this company before.", question=question)
        self.assertIsNone(record, "prose answered a yes/no control")

    def test_the_raw_composition_is_preserved_for_review(self):
        record, result = self.compose(GROUNDED_PARAGRAPH)
        row = self.database.execute(
            "SELECT raw FROM application_answering_attempts WHERE job_key=?",
            (self.job["job_key"],)).fetchone()
        self.assertIn("streaming inventory pipeline for Fabrikam", row["raw"], "the composed paragraph was not preserved")


class CredentialFileUploadTests(ResumeFixture):
    """Personio's required `Certificate` field is a file upload, not an attestation."""

    def test_a_required_certificate_upload_is_answerable_not_refused(self):
        """Owner, 2026-09-23: "If a job asks me to upload my certifcate as a file. Just supply
        my resume." A file control makes no assertion, and refusing it sent a REQUIRED field to
        the owner at every employer that asks for a certificate."""
        question = {"id": None, "required": True, "semantic": "Certificate (required)",
                    "label": "Certificate", "answer_type": "file"}
        result = self.run_answer([question], FakeModel())
        self.assertEqual(result["refused"], [], "a certificate upload is no longer withheld")

# --------------------------------------------------------------------------------------
# Round 4 - the adversary's findings against the computed-experience class (AE-F14 to F21).
#
# Each of these was a live false claim against the real resume before the fix. The owner
# waived accuracy, not fabrication: 3 versus 4 years of Kubernetes is the imprecision he
# accepted; seven years of COBOL, or any number at all in a CISSP field, is not.
# --------------------------------------------------------------------------------------

class SingleValueRangeTests(unittest.TestCase):
    """Owner, 2026-10-01: a single-value numeric control gets a range's lower limit.

    A strict allow-list: the whole answer must be an amount or a range of amounts, with only
    currency, "~"/approx. and a salary word around it; anything else is refused."""

    NUMBER = {"id": "q-salary", "label": "Expected Salary", "required": True, "answer_type": "number"}
    TEXT = {"id": "q-salary", "label": "Expected Salary", "required": True, "answer_type": "text"}

    def fit(self, answer):
        return answering_agent.conform_value(answer, self.NUMBER)

    def assertRefused(self, *answers):
        for answer in answers:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self.fit(answer)

    def test_ranges_fit_to_lower_limit(self):
        for answer, expected in (("USD 180,000\u2013250,000 base salary", 180000),
                                 ("$150,000 - $200,000", 150000), ("150k\u2013200k", 150000),
                                 ("USD 150000 to 200000", 150000), ("150-200k per year", 150000),
                                 ("CAD 120,000-140,000", 120000), ("\u00a350-60k", 50000),
                                 ("~150-200k", 150000), ("approx. 150-200k", 150000),
                                 ("$180,000\u2014$250,000", 180000), ("250k-180k", 180000),
                                 ("180,000 USD - 250,000 USD", 180000), ("180-250k", 180000),
                                 ("USD 180,000\u2013250,000 annual base salary", 180000),
                                 ("USD 200,000\u2013300,000 base salary annually", 200000),
                                 ("150k annual base", 150000), ("150k annual salary", 150000),
                                 ("150k per year base", 150000)):
            with self.subTest(answer=answer):
                self.assertEqual(self.fit(answer), expected)

    def test_shared_suffix_only_when_low_end_lacks_magnitude(self):
        self.assertEqual(self.fit("USD 100\u2013150,000"), 100000)
        self.assertEqual(self.fit("10-15,000"), 10000)
        self.assertEqual(self.fit("180\u2013250"), 180)
        self.assertEqual(self.fit("100-999"), 100)
        self.assertEqual(self.fit("180k-250,000"), 180000)
        # Ambiguous or mixed magnitude: refused rather than typed wrong.
        self.assertRefused("1-1,000", "2-2,000", "10-10,000", "1-5,000", "9-15,000",
                           "999-1,000,000", "100.5-150,000", "500-1,000", "180k-250", "1k-2",
                           "180,000-250", "180000-25", "100-1000", "01-05", "0-0", "1,500k-2,000k",
                           "1500k")

    def test_anything_outside_the_allow_list_refused(self):
        self.assertRefused("1-2 mil", "100-150 m", "100-150 grand", "10-12 lacs", "10-12 LPA",
                           "1-2 Cr", "50-60 Tsd", "50-60 TEUR", "2-4 weeks", "75-90/hr",
                           "75 per hour", "100-150k + equity", "8-10 years", "180-250 thousand",
                           "180-250 M", "180-250 MM", "5 million", "401k", "401(k)", "403b",
                           "457(b)", "negotiable", "1-2-3", "\u20ac90.000",
                           "alex.example@example.com", "+1", "Python 3", "02139", "-5-10",
                           "150k annual base salary per year", "\u0663\u0660\u0660",
                           "\uff13\uff10\uff10", "\uff11\uff15\uff10k",
                           "20001-4321")

    def test_salary_word_with_only_small_bare_amounts_refused(self):
        self.assertRefused("USD 150-200 per year", "$1-2 base", "150 per year",
                           "180-250 base salary")
        self.assertEqual(self.fit("180-250"), 180)

    def test_k_amounts_are_exact(self):
        from decimal import Decimal
        for hundredths in range(100000):
            whole, fraction = divmod(hundredths, 100)
            answer = "%d.%02dk" % (whole, fraction)
            value = self.fit(answer)
            self.assertEqual(Decimal(str(value)), Decimal("%d.%02d" % (whole, fraction)) * 1000, answer)
        self.assertEqual(self.fit("2.01k"), 2010)
        self.assertIsInstance(self.fit("2.01k"), int)
        self.assertEqual(self.fit("150000.5"), 150000.5)

    def test_single_amount(self):
        self.assertEqual(self.fit(5), 5)
        self.assertEqual(self.fit("USD 180,000"), 180000)
        self.assertEqual(self.fit("185k"), 185000)
        self.assertEqual(self.fit("-5"), -5)
        self.assertEqual(self.fit("180000 per year"), 180000)
        self.assertEqual(self.fit("approximately 150k"), 150000)

    def test_text_control_keeps_range(self):
        answer = "USD 180,000\u2013250,000 base salary"
        self.assertEqual(answering_agent.conform_value(answer, self.TEXT), answer)


class EchoedKeyTests(unittest.TestCase):
    """A mistyped echo of a supplied question key (Woodgrove, 2026-10-06)."""

    SALARY = "unidentified-question:desired_base_salary:aaabbb000111"
    PHONE = "unidentified-question:phone_country:0123456789ab"

    def output(self, *keys):
        answers = [entry({"question_key": key, "semantic": key.split(":")[1], "answer_type": "text"},
                         status="ANSWERED", value="x") for key in keys]
        return json.dumps({"packet_sha256": "p", "answers": answers})

    def test_one_dropped_letter_maps_back_to_the_supplied_key(self):
        parsed = answering_agent.parse_output(self.output("unidentified-question:desired_base_salary:aabbb000111", self.PHONE),
                                              {"packet_sha256": "p"}, [self.SALARY, self.PHONE])
        self.assertEqual({self.SALARY, self.PHONE}, set(parsed))

    def test_an_unsupplied_name_or_an_ambiguous_one_still_fails_coverage(self):
        for keys, askable in ((("unidentified-question:other:abc", self.PHONE), [self.SALARY, self.PHONE]),
                              (("unidentified-question:desired_base_salary:zzz",),
                               [self.SALARY, "unidentified-question:desired_base_salary:111111111111"])):
            with self.assertRaisesRegex(answering_agent.AnsweringUnavailable, "ANSWERING_COVERAGE_INVALID"):
                answering_agent.parse_output(self.output(*keys), {"packet_sha256": "p"}, askable)


if __name__ == "__main__":
    unittest.main()
