"""The answer judge (owner, 2026-10-21, option A) and the direct OpenRouter call. No network.

A separate AI judge reads every answer with the raw résumé, the answering AI's own instructions
and the owner's recorded answers; it returns ACCEPT / REVISE / CANNOT_ANSWER per answer, REVISE
answers go back to the answering AI, and the rounds end when the judge asks for nothing more (at
most 7). Both calls are direct `text_model.complete` calls in the judges' shape.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest import mock

import answering_agent
import answering_fixtures
import applications
import provider_limits
import text_model
import test_answering_agent as base
from test_answering_agent import FakeModel, entry, us_review, OFFICE_ATTENDANCE, NOW

SECRET = "sk-or-v1-synthetic-secret-0123456789"
CISSP = {"id": "q-cissp", "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
         "semantic": "holds_cissp", "label": "Do you hold an active CISSP certification?"}
WEATHER = {"id": "q-weather", "required": True, "answer_type": "text", "choices": [],
           "semantic": "weather_and_feeling", "label": "How is the weather and how are you feeling?"}


def willing(question, packet):
    return entry(question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value=True,
                 unknown_reason=None, derivation="Springfield, IL is the top preference.")


class Scripted(FakeModel):
    """Answers per call from `answers` (a list of respond functions, the last one repeating) and
    judges per round from `verdicts` (a list of decide functions, the last one repeating)."""

    def __init__(self, answers, verdicts):
        super().__init__(answers[0])
        self.answers, self.judge_packets = list(answers), []
        self.verdicts = [(lambda item, packet, fixed=decide: fixed) if isinstance(decide, str) else decide
                         for decide in verdicts]

    def __call__(self, prompt):
        if answering_fixtures.is_judge_prompt(prompt):
            self.judge_packets.append(answering_fixtures.judge_packet(prompt))
            self.judge = self.verdicts[min(len(self.judge_prompts), len(self.verdicts) - 1)]
        else:
            self.respond = self.answers[min(len(self.prompts), len(self.answers) - 1)]
        return super().__call__(prompt)


class JudgeRoundTests(base.ResumeFixture):
    def test_all_accepted_on_round_one_is_exactly_two_calls(self):
        model = Scripted([willing], ["ACCEPT"])
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        key = self.key(OFFICE_ATTENDANCE)
        self.assertEqual((len(model.prompts), len(model.judge_prompts)), (1, 1))
        self.assertEqual(result["answers"][key]["value"], "Yes")
        self.assertEqual(result["answers"][key]["judge"]["verdict"], "ACCEPT")
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["audit"]["outcome"], "COMPLETE")
        self.assertEqual([item["round"] for item in result["audit"]["rounds"]], [1])

    def test_the_judge_reads_the_resume_the_answering_rules_and_the_recorded_answers(self):
        model = Scripted([willing], ["ACCEPT"])
        self.run_answer([OFFICE_ATTENDANCE], model)
        prompt, packet = model.judge_prompts[0], model.judge_packets[0]
        rules = (self.root / answering_agent.PROMPT_FILE).read_text(encoding="utf-8")
        self.assertIn(answering_agent.RULES_MARKER + rules, prompt)
        self.assertEqual(packet["candidate_resume"]["text"], self.document["text"])
        self.assertIn("work_authorization_by_jurisdiction", packet["policy"]["answers"])
        reviewed = packet["answers_to_review"][0]
        self.assertEqual(reviewed["question"]["choices"], ["Yes", "No"])
        self.assertEqual(reviewed["form_fit"], {"fits": True, "value_entered": "Yes", "problem": None})

    def test_revise_goes_back_to_the_answering_ai_and_its_repair_is_accepted(self):
        def refuse(question, packet):
            return entry(question, status="UNKNOWN", unknown_reason="no weather data")

        def small_talk(question, packet):
            return entry(question, question_class="OTHER", status="ANSWERED", unknown_reason=None,
                         value="Doing well, thanks for asking.", derivation="small talk")
        model = Scripted([refuse, small_talk],
                         [lambda item, packet: ("REVISE", "small talk is answerable", "one warm sentence"), "ACCEPT"])
        result = self.run_answer([WEATHER], model)
        key = self.key(WEATHER)
        self.assertEqual((len(model.prompts), len(model.judge_prompts)), (2, 2))
        repair = json.loads(model.prompts[1].split(base.PACKET_MARKER, 1)[1])
        self.assertEqual(repair["repair_feedback"][0]["reviewer_reason"], "small talk is answerable")
        self.assertEqual(repair["repair_feedback"][0]["reviewer_correction"], "one warm sentence")
        self.assertEqual(result["answers"][key]["value"], "Doing well, thanks for asking.")
        self.assertEqual(result["answers"][key]["judge"]["round"], 2)
        self.assertEqual(model.judge_packets[1]["answers_to_review"][0]["earlier_reviews"][0]["verdict"], "REVISE")

    def test_a_judge_that_keeps_revising_stops_at_seven_rounds(self):
        model = Scripted([willing], [lambda item, packet: ("REVISE", "again", None)])
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        key = self.key(OFFICE_ATTENDANCE)
        self.assertEqual(len(model.judge_prompts), answering_agent.MAX_ROUNDS)
        self.assertEqual(answering_agent.MAX_ROUNDS, 7)
        self.assertEqual(len(model.prompts), 7)  # the answer and six repairs; the last REVISE is not repaired
        self.assertEqual((result["answers"], result["unknown"]), ({}, [key]))
        self.assertTrue(result["audit"]["verdicts"][-1]["reason"].startswith("JUDGE_ROUND_LIMIT"))

    def test_cannot_answer_is_recorded_unknown(self):
        model = Scripted([willing], [lambda item, packet: ("CANNOT_ANSWER", "nothing records it", None)])
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        key = self.key(OFFICE_ATTENDANCE)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [key]))
        self.assertEqual(result["audit"]["verdicts"][0]["reason"], "JUDGE_CANNOT_ANSWER: nothing records it")

    def test_the_judge_sends_back_a_credential_the_resume_lacks(self):
        def claims(question, packet):
            return entry(question, question_class="CANDIDATE_HISTORY_FACT", status="ANSWERED",
                         value=True, unknown_reason=None, derivation="security background")

        def corrected(question, packet):
            return dict(claims(question, packet), value=False, derivation="the résumé records no CISSP")

        def resume_reader(item, packet):
            claimed = item["answer"]["value"] is True
            if claimed and "CISSP" not in packet["candidate_resume"]["text"]:
                return ("REVISE", "the résumé records no CISSP", "answer No")
            return "ACCEPT"
        model = Scripted([claims, corrected], [resume_reader])
        result = self.run_answer([CISSP], model)
        self.assertEqual(result["answers"][self.key(CISSP)]["value"], "No")
        self.assertEqual(len(model.judge_prompts), 2)

    def test_an_accepted_value_the_control_cannot_take_is_shown_as_a_fact_and_never_written(self):
        model = Scripted([lambda q, packet: dict(willing(q, packet), value="Maybe")], ["ACCEPT"])
        result = self.run_answer([OFFICE_ATTENDANCE], model)
        fit = model.judge_packets[0]["answers_to_review"][0]["form_fit"]
        self.assertEqual(fit, {"fits": False, "value_entered": None, "problem": "MODEL_VALUE_NOT_BOOLEAN"})
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(OFFICE_ATTENDANCE)]))

    def test_no_askable_question_calls_nothing(self):
        model = Scripted([willing], ["ACCEPT"])
        result = self.run_answer([dict(OFFICE_ATTENDANCE, required=False)], model)
        self.assertEqual((model.prompts, model.judge_prompts, result["audit"]["outcome"]), ([], [], "NO_QUESTIONS"))

    def test_a_failure_mid_loop_keeps_what_was_accepted(self):
        def accept_office(item, packet):
            return "ACCEPT" if item["question"]["label"] == OFFICE_ATTENDANCE["label"] else ("REVISE", "fix", None)

        def small_talk(question, packet):
            return (willing(question, packet) if question["label"] == OFFICE_ATTENDANCE["label"] else
                    entry(question, status="ANSWERED", unknown_reason=None, value="Fine, thanks."))

        class FailingSecondJudge(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt) and self.judge_prompts:
                    raise RuntimeError("provider refused")
                return super().__call__(prompt)
        result = self.run_answer([OFFICE_ATTENDANCE, WEATHER], FailingSecondJudge([small_talk], [accept_office]))
        self.assertEqual(list(result["answers"]), [self.key(OFFICE_ATTENDANCE)])
        self.assertEqual(result["unknown"], [self.key(WEATHER)])
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_EXECUTOR_FAILED")
        self.assertEqual(result["audit"]["rounds"][1]["judge"]["outcome"], "ANSWERING_EXECUTOR_FAILED")

    def test_a_judge_output_that_misses_a_question_uses_the_answer_as_a_failed_judge_call(self):
        class Short(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt):
                    return json.dumps({"verdicts": []})
                return super().__call__(prompt)
        result = self.run_answer([OFFICE_ATTENDANCE], Short([willing], ["ACCEPT"]))
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_JUDGE_COVERAGE_INVALID")
        self.assertEqual(result["answers"][self.key(OFFICE_ATTENDANCE)]["judge"]["judge_failed"],
                         "ANSWERING_JUDGE_COVERAGE_INVALID")  # G3


class DirectCallTests(base.ResumeFixture):
    """No injected executor: both calls go to OpenRouter through `text_model.complete`."""

    def run_direct(self, reply, questions=(OFFICE_ATTENDANCE,), timeout=5):
        config = dict(self.config, text_model={"model": "z-ai/glm-5.3"})
        config["application_answering"] = {"enabled": True, "timeout_seconds": timeout}
        self.requests = []

        def transport(url, data, headers, seconds):
            body = json.loads(data)
            self.requests.append((url, body, seconds, headers))
            return reply(body)

        with mock.patch.object(text_model, "key", return_value=SECRET), mock.patch.object(text_model, "_post", transport):
            return self.run_answer(list(questions), None, config=config)

    @staticmethod
    def ok(content, usage=None):
        return 200, json.dumps({"model": "z-ai/glm-5.3", "usage": usage or {"prompt_tokens": 9000, "completion_tokens": 300,
                                                                          "cost": 0.004},
                                "choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()

    def model_reply(self, body):
        prompt = body["messages"][-1]["content"]
        if answering_fixtures.is_judge_prompt(prompt):
            return self.ok("```json\n" + answering_fixtures.judge_reply(prompt) + "\n```")
        packet = json.loads(prompt.split(base.PACKET_MARKER, 1)[1])
        return self.ok(json.dumps({"packet_sha256": packet["packet_sha256"],
                                   "answers": [willing(q, packet) for q in packet["questions"]]}))

    def test_both_calls_are_low_effort_strict_schema_and_cheapest_honouring_provider(self):
        result = self.run_direct(self.model_reply)
        self.assertEqual(result["answers"][self.key(OFFICE_ATTENDANCE)]["value"], "Yes")
        self.assertEqual(len(self.requests), 2)
        for (url, body, seconds, headers), schema_file, name, ceiling in zip(
                self.requests, (answering_agent.SCHEMA_FILE, answering_agent.JUDGE_SCHEMA_FILE),
                ("application_answers", "application_answer_review"),
                (answering_agent.ANSWER_MAX_TOKENS, answering_agent.JUDGE_MAX_TOKENS)):
            schema = json.loads((self.root / schema_file).read_text())
            self.assertEqual(url, text_model.BASE_URL + "/chat/completions")
            self.assertEqual((body["model"], body["reasoning"], body["max_tokens"], seconds),
                             ("z-ai/glm-5.3", {"effort": "low"}, ceiling, 5))
            self.assertEqual(body["response_format"], {"type": "json_schema", "json_schema": {
                "name": name, "strict": True, "schema": schema}})
            self.assertEqual(body["provider"], {"sort": "price", "require_parameters": True})
            self.assertIn(json.dumps(schema, indent=2)[:40], body["messages"][0]["content"])
        calls = result["audit"]["calls"]
        self.assertEqual([(call["role"], call["reasoning_effort"]) for call in calls], [("answer", "low"), ("judge", "low")])
        self.assertEqual(calls[0]["usage"]["cost"], 0.004)
        bookings = [row[0] for row in self.database.execute(
            "SELECT action_id FROM worker_compute_reservations ORDER BY rowid")]
        self.assertEqual([booking.split(":")[0] for booking in bookings],
                         ["application-answering", "application-answering-judge"])

    def test_a_402_is_a_capacity_wait_with_its_status_and_never_the_key(self):
        # 2026-10-23 (owner): out of OpenRouter credit is a capacity wait, never a counted retry.
        error = {"error": {"message": "This request requires more credits. key " + SECRET, "code": 402}}
        result = self.run_direct(lambda body: (402, json.dumps(error).encode()))
        key = self.key(OFFICE_ATTENDANCE)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [key]))
        self.assertEqual(result["audit"]["outcome"], provider_limits.CODE)
        self.assertEqual(result["audit"]["provider_wait"]["kind"], provider_limits.OUT_OF_CREDIT)
        self.assertEqual(result["audit"]["calls"][0]["http_status"], 402)
        saved = self.database.execute("SELECT audit FROM application_answering_attempts").fetchone()[0]
        self.assertIn("HTTP 402", saved)
        self.assertNotIn(SECRET, saved)

    def test_a_judge_402_is_a_capacity_wait_not_a_failed_judge(self):
        # G3 uses the answer over a FAILED judge call; out of credit (2026-10-23) is a capacity wait,
        # which G3 never uses an answer over: the question stays open for the next cycle.
        def reply(body):
            if answering_fixtures.is_judge_prompt(body["messages"][-1]["content"]):
                return 402, b'{"error": {"message": "credits", "code": 402}}'
            return self.model_reply(body)
        result = self.run_direct(reply)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(OFFICE_ATTENDANCE)]))
        self.assertEqual(result["audit"]["outcome"], provider_limits.CODE)

    def test_a_timeout_is_answering_timeout(self):
        def reply(body):
            raise TimeoutError("timed out")
        result = self.run_direct(reply)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_TIMEOUT")
        self.assertEqual(result["unknown"], [self.key(OFFICE_ATTENDANCE)])

    def test_without_a_text_model_nothing_is_called(self):
        result = self.run_answer([OFFICE_ATTENDANCE], None)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_EXECUTOR_NOT_CONFIGURED")
        self.assertEqual(result["unknown"], [self.key(OFFICE_ATTENDANCE)])


class JudgeSeamTests(unittest.TestCase):
    """Through `applications.cycle`: a question the judge finds no truthful answer to (CANNOT_ANSWER) is
    asked on Approvals and holds the job for the owner (owner, 2026-10-24); nothing is submitted."""

    _fixture = base.SeamTests
    setUp, cycle, discover, job = _fixture.setUp, _fixture.cycle, _fixture.discover, _fixture.job
    del _fixture

    def test_a_rejected_answer_is_asked_of_the_owner_and_nothing_is_submitted(self):
        key = self.discover()
        model = Scripted([willing], [lambda item, packet: ("CANNOT_ANSWER", "not recorded", None)])
        self.adapter.answering_executor = model
        self.cycle({"application_answering": {"enabled": True, "timeout_seconds": 5}, "timezone": "UTC"})
        self.assertEqual((len(model.prompts), len(model.judge_prompts)), (1, 1))
        job = self.job(key)
        self.assertEqual(job["state"], "BLOCKED")
        self.assertEqual(job["blocker"], "UNKNOWN_REQUIRED_ANSWERS:" + applications.question_field(OFFICE_ATTENDANCE))
        self.assertEqual(job["attempts"], 0)
        self.assertEqual([row[0] for row in self.database.execute("SELECT field FROM user_decisions WHERE state='PENDING'")],
                         [applications.question_field(OFFICE_ATTENDANCE)])
        self.assertFalse([call for call in self.adapter.calls if call[0] == "submit"])


GENDER_MENU = {"id": "q-gender", "required": True, "answer_type": "choice", "choices": ["Man", "Woman", "Decline to state"],
               "semantic": "gender_self_identification", "label": "Gender"}


class FixReviewTests(base.ResumeFixture):
    """Owner-approved review fixes J1, J2, J3, J5 (2026-10-22)."""

    def test_j1_a_recorded_value_that_is_not_an_option_goes_to_the_ai(self):
        key = self.key(GENDER_MENU)
        answers, missing = applications.resolve_answers([GENDER_MENU], self.policy)
        self.assertEqual((answers, missing), ({}, [key]))  # never "Male" typed onto the menu

        def picks(question, packet):
            recorded = next(saved["value"] for saved in packet["policy"]["answers"].values()
                            if saved.get("semantic") == "gender_self_identification")
            self.assertEqual(recorded, "Male")
            return entry(question, question_class="DEMOGRAPHIC_VOLUNTARY", status="ANSWERED", unknown_reason=None,
                         value="Man", derivation="recorded Male; the menu's option is Man")
        model = Scripted([picks], ["ACCEPT"])
        result = self.run_answer([GENDER_MENU], model)
        self.assertEqual(result["answers"][key]["value"], "Man")
        self.assertEqual((len(model.prompts), len(model.judge_prompts)), (1, 1))

    def test_j1_a_recorded_value_that_is_exactly_an_option_is_applied_as_recorded(self):
        menu = dict(GENDER_MENU, choices=["Male", "Female", "Decline to state"])
        model = Scripted([willing], ["ACCEPT"])
        result = self.run_answer([menu], model)
        self.assertEqual(result["answers"][self.key(menu)]["value"], "Male")
        self.assertEqual((model.prompts, model.judge_prompts), ([], []))

    def test_j3_an_unbound_verdict_does_not_discard_the_round(self):
        def foreign_key_for_weather(item, packet):
            return "ACCEPT"

        class OneStray(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt) and not self.judge_prompts:
                    self.judge_prompts.append(prompt)
                    self.judge_packets.append(answering_fixtures.judge_packet(prompt))
                    reply = json.loads(answering_fixtures.judge_reply(prompt))
                    for verdict in reply["verdicts"]:
                        if verdict["question_key"] == self_key(WEATHER):
                            verdict["question_key"] = "not-supplied-anywhere"
                    reply["verdicts"].append({"question_key": 7})  # malformed
                    return json.dumps(reply)
                return super().__call__(prompt)
        self_key = self.key

        def answers(question, packet):
            return (willing(question, packet) if question["label"] == OFFICE_ATTENDANCE["label"] else
                    entry(question, status="ANSWERED", unknown_reason=None, value="Fine, thanks."))
        model = OneStray([answers], [foreign_key_for_weather])
        result = self.run_answer([OFFICE_ATTENDANCE, WEATHER], model)
        self.assertEqual(result["answers"][self.key(OFFICE_ATTENDANCE)]["judge"]["round"], 1)
        self.assertEqual(result["answers"][self.key(WEATHER)]["judge"]["round"], 2)
        self.assertEqual((len(model.prompts), len(model.judge_prompts)), (2, 2))
        second = model.judge_packets[1]["answers_to_review"]
        self.assertEqual([item["question"]["question_key"] for item in second], [self.key(WEATHER)])
        repair = json.loads(model.prompts[1].split(base.PACKET_MARKER, 1)[1])
        self.assertEqual(repair["repair_feedback"][0]["reviewer_reason"], answering_agent.UNJUDGED_REASON)
        self.assertEqual(len(result["audit"]["rounds"][0]["judge"]["unbound"]), 2)

    def test_j5_each_call_is_settled_from_its_own_start(self):
        settled = []
        original = answering_agent.workflow_tasks.settle_compute

        def capture(database, booking, started_at, completed_at, reason):
            settled.append((booking.split(":")[0], started_at, completed_at))
            return original(database, booking, started_at, completed_at, reason)
        model = Scripted([willing, willing], [lambda item, packet: ("REVISE", "again", None), "ACCEPT"])
        with mock.patch.object(answering_agent.workflow_tasks, "settle_compute", capture):
            self.run_answer([OFFICE_ATTENDANCE], model)
        self.assertEqual([name for name, _start, _end in settled],
                         ["application-answering", "application-answering-judge",
                          "application-answering-repair", "application-answering-judge"])
        for (_name, start, end), (_next, next_start, _next_end) in zip(settled, settled[1:]):
            self.assertNotEqual(start, NOW)
            self.assertLessEqual(start, end)
            self.assertLessEqual(end, next_start)  # never counted from the step's start


class LengthLimitTests(base.ResumeFixture):
    """J2: a cut-off answer is too long, not malformed; the answering ceiling fits a long form."""

    run_direct, model_reply, ok = DirectCallTests.run_direct, DirectCallTests.model_reply, staticmethod(DirectCallTests.ok)

    def test_j2_finish_reason_length_is_output_exceeds_limit(self):
        def cut(body):
            return 200, json.dumps({"model": "m", "usage": {}, "choices": [
                {"finish_reason": "length", "message": {"content": '{"packet_sha256": "x", "answers": [{"quest'}}]}).encode()
        result = self.run_direct(cut)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_OUTPUT_EXCEEDS_LIMIT")
        self.assertEqual(self.requests[0][1]["max_tokens"], 24000)

    def test_j2_a_judge_cut_off_is_output_exceeds_limit_too(self):
        def reply(body):
            if answering_fixtures.is_judge_prompt(body["messages"][-1]["content"]):
                return 200, json.dumps({"model": "m", "usage": {}, "choices": [
                    {"finish_reason": "length", "message": {"content": '{"verdicts": [{"quest'}}]}).encode()
            return self.model_reply(body)
        self.assertEqual(self.run_direct(reply)["audit"]["outcome"], "ANSWERING_OUTPUT_EXCEEDS_LIMIT")

    def test_j2_broken_json_that_stopped_normally_is_still_not_json(self):
        result = self.run_direct(lambda body: self.ok('{"packet_sha256": "x", "answers": [{"quest'))
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_OUTPUT_NOT_JSON")


CLEARANCE = {"id": "q-secret", "required": True, "answer_type": "boolean", "choices": ["Yes", "No"],
             "semantic": "active_secret_clearance", "label": "Do you hold an active Secret clearance?"}
K8S_YEARS = {"id": "q-k8s", "required": True, "answer_type": "number", "choices": [],
             "semantic": "years_of_kubernetes", "label": "How many years of Kubernetes experience do you have?"}
WHY_US = {"id": "q-why", "required": True, "answer_type": "text", "choices": [],
          "semantic": "why_us", "label": "Why do you want to work here?"}


def answered(value, question_class="OTHER"):
    return lambda question, packet: entry(question, question_class=question_class, status="ANSWERED",
                                          unknown_reason=None, value=value, derivation="fixture")


class OverJudgeTests(base.ResumeFixture):
    """J6 option B (owner, 2026-10-22): an objection the judge itself flags untrue_fact false does
    not block; one flagged true does."""

    def test_example_1_cannot_answer_without_an_untrue_fact_uses_the_answer(self):
        model = Scripted([answered(False, "CANDIDATE_HISTORY_FACT")],
                         [lambda item, packet: ("CANNOT_ANSWER", "résumé mentions DoD work, unsure", None, False)])
        result = self.run_answer([CLEARANCE], model)
        key = self.key(CLEARANCE)
        self.assertEqual(result["answers"][key]["value"], "No")
        self.assertEqual(result["answers"][key]["judge"],
                         {"verdict": "CANNOT_ANSWER", "round": 1, "reason": "résumé mentions DoD work, unsure",
                          "used_over_judge": True})
        audit = result["audit"]["verdicts"][0]
        self.assertTrue(audit["used_over_judge"])
        self.assertEqual((audit["judge"]["verdict"], audit["round"], audit["outcome"]), ("CANNOT_ANSWER", 1, "ANSWERED"))
        self.assertEqual(result["unknown"], [])

    def test_example_2_an_untrue_fact_is_never_sent(self):
        model = Scripted([answered(6, "COMPUTED_EXPERIENCE")],
                         [lambda item, packet: ("REVISE", "the résumé supports 3 years", "3", True)])
        result = self.run_answer([K8S_YEARS], model)
        self.assertEqual(len(model.judge_prompts), answering_agent.MAX_ROUNDS)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(K8S_YEARS)]))

    def test_example_3_a_style_objection_at_the_end_uses_the_latest_paragraph(self):
        model = Scripted([answered("Generic paragraph."), answered("Revised paragraph.")],
                         [lambda item, packet: ("REVISE", "too generic", "be specific", False)])
        result = self.run_answer([WHY_US], model)
        record = result["answers"][self.key(WHY_US)]
        self.assertEqual(record["value"], "Revised paragraph.")
        self.assertEqual((record["judge"]["verdict"], record["judge"]["round"]), ("REVISE", answering_agent.MAX_ROUNDS))

    def test_example_4_an_unknown_answer_has_nothing_to_use(self):
        model = Scripted([lambda question, packet: entry(question)],
                         [lambda item, packet: ("CANNOT_ANSWER", "could be answered", None, False)])
        result = self.run_answer([WHY_US], model)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(WHY_US)]))

    def test_a_verdict_missing_the_flag_is_unjudged_and_never_passes_on_its_own(self):
        why_key = self.key(WHY_US)

        class NoFlag(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt):
                    self.judge_prompts.append(prompt)
                    reply = json.loads(answering_fixtures.judge_reply(
                        prompt, lambda item, packet: ("REVISE", "could be firmer", None, False)))
                    for verdict in reply["verdicts"]:
                        if verdict["question_key"] == why_key:
                            del verdict["untrue_fact"]
                    return json.dumps(reply)
                return super().__call__(prompt)
        model = NoFlag([lambda q, packet: (answered(False, "CANDIDATE_HISTORY_FACT") if q["label"] == CLEARANCE["label"]
                                           else answered("A paragraph."))(q, packet)], ["ACCEPT"])
        result = self.run_answer([CLEARANCE, WHY_US], model)
        # Both stay under review to round 7: the clearance answer on a flagged-false REVISE (used at
        # the end), the paragraph on a verdict without the flag (unjudged, J3: never used on its own).
        self.assertEqual(result["answers"][self.key(CLEARANCE)]["value"], "No")
        self.assertTrue(result["answers"][self.key(CLEARANCE)]["judge"]["used_over_judge"])
        self.assertEqual(len(model.judge_prompts), answering_agent.MAX_ROUNDS)
        self.assertEqual(result["unknown"], [why_key])
        self.assertNotIn(why_key, result["answers"])
        self.assertTrue(result["audit"]["rounds"][0]["judge"]["unbound"])


class LengthCutSettlementTests(base.ResumeFixture):
    """J8: a call cut at max_tokens is settled by its duration; an HTTP failure keeps its booking."""

    run_direct, model_reply, ok = DirectCallTests.run_direct, DirectCallTests.model_reply, staticmethod(DirectCallTests.ok)

    def settled(self):
        return {row[0].split(":")[0]: row[1] for row in self.database.execute(
            "SELECT action_id, elapsed_seconds FROM worker_compute_refunds")}

    def cut(self, judge_only=False):
        def reply(body):
            if judge_only and not answering_fixtures.is_judge_prompt(body["messages"][-1]["content"]):
                return self.model_reply(body)
            return 200, json.dumps({"model": "m", "usage": {}, "choices": [
                {"finish_reason": "length", "message": {"content": '{"cut": "of'}}]}).encode()
        return reply

    def test_a_length_cut_answer_call_is_settled_and_still_exceeds_the_limit(self):
        result = self.run_direct(self.cut(), timeout=600)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_OUTPUT_EXCEEDS_LIMIT")
        self.assertIn("application-answering", self.settled())

    def test_a_length_cut_judge_call_is_settled_too(self):
        result = self.run_direct(self.cut(judge_only=True), timeout=600)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_OUTPUT_EXCEEDS_LIMIT")
        self.assertEqual(set(self.settled()), {"application-answering", "application-answering-judge"})

    def test_an_http_402_keeps_its_whole_booking(self):
        result = self.run_direct(lambda body: (402, b'{"error": {"message": "credits", "code": 402}}'), timeout=600)
        self.assertEqual(result["audit"]["outcome"], provider_limits.CODE)
        self.assertEqual(self.settled(), {})
        booked = self.database.execute("SELECT seconds FROM worker_compute_reservations").fetchone()[0]
        self.assertEqual(booked, 600)


if __name__ == "__main__":
    unittest.main()


class JudgeFailureTests(base.ResumeFixture):
    """G3 (owner, 2026-10-22): a judge or repair call that fails is treated like the judge objecting
    with no untrue fact (J6 option B): the answering AI's latest ANSWERED, fitting answer is used and
    the audit says it went through because the judge failed. An answer the judge flagged untrue_fact
    true still blocks; an UNKNOWN or non-fitting answer is not used."""

    @staticmethod
    def by_label(*calls):
        """One respond function per answering call: {label: value} (None for UNKNOWN)."""
        def respond(values):
            def answer(question, packet):
                value = values[question["label"]]
                return entry(question) if value is None else answered(value)(question, packet)
            return answer
        return [respond(values) for values in calls]

    @staticmethod
    def failing(error, judge_round=None, repair=None):
        class Failing(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt):
                    if judge_round is not None and len(self.judge_prompts) == judge_round - 1:
                        raise error
                elif repair is not None and len(self.prompts) == repair:
                    raise error
                return super().__call__(prompt)
        return Failing

    # Worked example 3
    def test_judge_timeout_in_round_2_uses_the_round_2_answer_but_never_a_flagged_one(self):
        answers = self.by_label({WHY_US["label"]: "Generic paragraph.", WEATHER["label"]: "Rainy, and I am sad."},
                                {WHY_US["label"]: "Specific paragraph.", WEATHER["label"]: "Rainy, and I am sad."})
        # Round 1: both REVISE with untrue_fact true (the fixture's default for an objection).
        model = self.failing(TimeoutError("judge timed out"), judge_round=2)(answers, [lambda item, packet: ("REVISE", "untrue", "fix")])
        result = self.run_answer([WHY_US, WEATHER], model)
        why, weather = self.key(WHY_US), self.key(WEATHER)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_TIMEOUT")
        self.assertEqual(result["answers"][why]["value"], "Specific paragraph.")
        self.assertEqual(result["answers"][why]["judge"],
                         {"verdict": None, "round": 2, "judge_failed": "ANSWERING_TIMEOUT", "used_over_judge": True})
        audit = [verdict for verdict in result["audit"]["verdicts"] if verdict.get("judge_failed")]
        self.assertEqual([(item["question_key"], item["round"]) for item in audit], [(why, 2)])
        # The round-1 answer the judge flagged untrue, repeated by the repair, still blocks.
        self.assertEqual(result["unknown"], [weather])
        self.assertNotIn(weather, result["answers"])

    def test_judge_executor_failure_in_round_1_uses_the_answer(self):
        model = self.failing(RuntimeError("provider refused"), judge_round=1)(
            self.by_label({WHY_US["label"]: "A paragraph."}), ["ACCEPT"])
        result = self.run_answer([WHY_US], model)
        record = result["answers"][self.key(WHY_US)]
        self.assertEqual((record["value"], record["judge"]["judge_failed"]), ("A paragraph.", "ANSWERING_EXECUTOR_FAILED"))
        self.assertEqual((result["unknown"], result["audit"]["outcome"]), ([], "ANSWERING_EXECUTOR_FAILED"))
        self.assertTrue(result["audit"]["verdicts"][0]["used_over_judge"])

    def test_a_failed_repair_call_uses_the_answer_unless_it_was_flagged_untrue(self):
        def decide(item, packet):
            untrue = item["question"]["label"] == WEATHER["label"]
            return ("REVISE", "objection", None, untrue)
        model = self.failing(RuntimeError("provider refused"), repair=1)(
            self.by_label({WHY_US["label"]: "A paragraph.", WEATHER["label"]: "Rainy, and I am sad."}), [decide])
        result = self.run_answer([WHY_US, WEATHER], model)
        self.assertEqual(result["answers"][self.key(WHY_US)]["value"], "A paragraph.")
        self.assertEqual(result["answers"][self.key(WHY_US)]["judge"]["judge_failed"], "ANSWERING_EXECUTOR_FAILED")
        self.assertEqual(result["unknown"], [self.key(WEATHER)])

    def test_an_unknown_answer_is_not_used_when_the_judge_fails(self):
        model = self.failing(TimeoutError("judge timed out"), judge_round=1)(
            self.by_label({WHY_US["label"]: None}), ["ACCEPT"])
        result = self.run_answer([WHY_US], model)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(WHY_US)]))

    def test_a_value_the_control_cannot_take_is_not_used_when_the_judge_fails(self):
        model = self.failing(TimeoutError("judge timed out"), judge_round=1)(
            self.by_label({K8S_YEARS["label"]: "many years"}), ["ACCEPT"])
        result = self.run_answer([K8S_YEARS], model)
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(K8S_YEARS)]))

    def test_a_judge_compute_wait_is_not_a_failed_call(self):
        model = Scripted(self.by_label({WHY_US["label"]: "A paragraph."}), ["ACCEPT"])
        reserve = answering_agent.workflow_tasks.reserve_compute
        calls = []

        def second_refused(*args, **kwargs):
            calls.append(args[1])
            return reserve(*args, **kwargs) if len(calls) == 1 else False
        with mock.patch.object(answering_agent.workflow_tasks, "reserve_compute", side_effect=second_refused):
            result = self.run_answer([WHY_US], model)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_COMPUTE_BUDGET_EXHAUSTED")
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(WHY_US)]))


class RawFlagTests(base.ResumeFixture):
    """GL3 (owner, 2026-10-22): an untrue_fact flag in a judge item that fails validation still
    flags that answer for a later judge failure, and a failed repair call never uses an answer whose
    verdict was unjudged (untrue_fact None), as `_over_judge` refuses it."""

    def run_rounds(self, rounds, repair_fails=False):
        """`rounds`: per judge call, a function (key) -> the raw verdict items for WHY_US, or an
        exception. WEATHER is accepted with a valid item, so the round binds (no coverage failure)."""
        answer = JudgeFailureTests.by_label({WHY_US["label"]: "A paragraph.", WEATHER["label"]: "Sunny, thanks."})

        class Raw(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt):
                    self.judge_prompts.append(prompt)
                    step = rounds[min(len(self.judge_prompts) - 1, len(rounds) - 1)]
                    if isinstance(step, BaseException):
                        raise step
                    items = []
                    for reviewed in answering_fixtures.judge_packet(prompt)["answers_to_review"]:
                        key = reviewed["question"]["question_key"]
                        items += (step(key) if reviewed["question"]["label"] == WHY_US["label"] else
                                  [{"question_key": key, "verdict": "ACCEPT", "reason": "fine", "correction": None,
                                    "untrue_fact": False}])
                    return json.dumps({"verdicts": items})
                if repair_fails and self.prompts:
                    raise RuntimeError("provider refused")
                return super().__call__(prompt)
        return self.run_answer([WHY_US, WEATHER], Raw(answer, ["ACCEPT"]))

    @staticmethod
    def item(key, **changes):
        return dict({"question_key": key, "verdict": "REVISE", "reason": "untrue", "correction": None,
                     "untrue_fact": True}, **changes)

    def assert_blocked(self, result):
        self.assertEqual((list(result["answers"]), result["unknown"]), ([self.key(WEATHER)], [self.key(WHY_US)]))

    def test_a_flag_in_an_item_with_an_extra_key_still_blocks_after_a_judge_failure(self):
        result = self.run_rounds([lambda key: [self.item(key, extra="x")], TimeoutError("judge timed out")])
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_TIMEOUT")
        self.assert_blocked(result)

    def test_a_string_true_flag_still_blocks_after_a_judge_failure(self):
        result = self.run_rounds([lambda key: [self.item(key, untrue_fact="true")], RuntimeError("refused")])
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_EXECUTOR_FAILED")
        self.assert_blocked(result)

    def test_a_misspelled_key_with_a_flag_still_blocks(self):
        def misspelled(key):
            name, fingerprint = key.rsplit(":", 1)
            return [self.item(name + ":" + fingerprint[:-1], extra="x")]
        self.assert_blocked(self.run_rounds([misspelled, TimeoutError("judge timed out")]))

    def test_coverage_invalid_still_honours_the_flags_in_the_raw_output(self):
        class OnlyInvalid(Scripted):
            def __call__(self, prompt):
                if answering_fixtures.is_judge_prompt(prompt):
                    self.judge_prompts.append(prompt)
                    items = [RawFlagTests.item(reviewed["question"]["question_key"], untrue_fact="true")
                             for reviewed in answering_fixtures.judge_packet(prompt)["answers_to_review"]]
                    return json.dumps({"verdicts": items})
                return super().__call__(prompt)
        model = OnlyInvalid(JudgeFailureTests.by_label({WHY_US["label"]: "A paragraph."}), ["ACCEPT"])
        result = self.run_answer([WHY_US], model)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_JUDGE_COVERAGE_INVALID")
        self.assertEqual((result["answers"], result["unknown"]), ({}, [self.key(WHY_US)]))

    def test_a_failed_repair_never_uses_an_answer_whose_verdict_was_unjudged(self):
        # Round 1's only item has no flag at all: an unjudged REVISE (untrue_fact None).
        def unflagged(key):
            item = self.item(key)
            del item["untrue_fact"]
            return [item]
        result = self.run_rounds([unflagged], repair_fails=True)
        self.assertEqual(result["audit"]["outcome"], "ANSWERING_EXECUTOR_FAILED")
        self.assert_blocked(result)

    def test_a_false_flag_in_an_invalid_item_flags_nothing(self):
        result = self.run_rounds([lambda key: [self.item(key, untrue_fact="false", extra="x")],
                                  TimeoutError("judge timed out")])
        self.assertEqual(result["answers"][self.key(WHY_US)]["judge"]["judge_failed"], "ANSWERING_TIMEOUT")
