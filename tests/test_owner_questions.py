"""Questions only the owner can answer (owner, 2026-10-24), end to end from what the owner sees.

"If the required question has 'options', e.g like green card, permanent residence, citizen, etc..
Those should be displayed too, so a user can pass in their answer. It still be persisted ... so it can
be reused in the future. It should be shown on settings page too."

Shaped like the two real cases on the owner's install (2026-10-23):
- ASI: a required choice question, export_control_protected_individual_status, with five options;
  the answering AI said UNKNOWN and the judge accepted it.
- InsForge: two required text questions (GitHub profile, Twitter / X profile); the judge said
  CANNOT_ANSWER ("The packet records no GitHub profile URL").

Each test runs the real applications cycle (mock browser, fake answering model), renders /approvals
with TestClient, answers there, and checks profile.json, the job and a later job asking the same thing.
"""

import datetime as dt
import html
import importlib.util
import json
import unittest
from unittest import mock

import applications
import orchestrator
import profile_home
import setup_steps
import user_profile
import test_applications_e2e as fixtures
from answering_fixtures import is_judge_prompt, judge_reply
from test_answering_agent import PACKET_MARKER, entry

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient
    import dashboard

PORT = 47823
SECRET = "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
CONFIG = {"application_answering": {"enabled": True, "timeout_seconds": 5}}

EXPORT_CHOICES = ["A United States citizen or national.",
                  "A person lawfully admitted for permanent resident of the United States (IE. a green card holder).",
                  "A person admitted as a refugee to the united states under 8 U.S.C. 1157.",
                  "A person admitted as an asylee to the United States under 8 U.S.C 1158.",
                  "None of the above."]
EXPORT = {"id": None, "required": True, "semantic": "export_control_protected_individual_status", "answer_type": "choice",
          "label": "Are you any of the following (export control): select the one that applies", "choices": EXPORT_CHOICES}
GITHUB = {"id": None, "required": True, "semantic": "github_profile", "answer_type": "text", "choices": [],
          "label": "GitHub profile *"}
TWITTER = {"id": None, "required": True, "semantic": "twitter_profile", "answer_type": "text", "choices": [],
           "label": "Twitter / X profile *"}


class Model:
    """The answering AI and its judge. The AI answers a question from an owner answer kept for its
    semantic (as the real AI reads /policy/answers), else UNKNOWN; the judge returns `verdict` for an
    UNKNOWN (ACCEPT for ASI, CANNOT_ANSWER for InsForge) and accepts every answer."""

    def __init__(self, verdict="ACCEPT", error=None, values=None):
        self.verdict, self.error, self.packets = verdict, error, []
        self.values = values or {}   # {semantic: value} the AI answers outright (fitting its control or not)

    def __call__(self, prompt):
        if self.error is not None:
            raise self.error
        if is_judge_prompt(prompt):
            return judge_reply(prompt, lambda item, packet: (
                "ACCEPT" if item["answer"]["status"] == "ANSWERED"
                or self.verdict == "ACCEPT" else (self.verdict, "The packet records no answer to this.", None, False)))
        packet = json.loads(prompt.split(PACKET_MARKER, 1)[1])
        self.packets.append(packet)
        answers = []
        for question in packet["questions"]:
            kept = [answer for answer in packet["policy"]["answers"].values() if answer.get("semantic") == question["semantic"]]
            if question["semantic"] in self.values:
                answers.append(entry(question, status="ANSWERED", unknown_reason=None, value=self.values[question["semantic"]],
                                     derivation="fixture"))
            elif kept:
                answers.append(entry(question, status="ANSWERED", unknown_reason=None, value=kept[-1]["value"],
                                     derivation="The owner answered this on Approvals."))
            else:
                answers.append(entry(question, unknown_reason="The packet does not record this."))
        return json.dumps({"packet_sha256": packet["packet_sha256"], "answers": answers})


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class OwnerQuestions(fixtures.ApplicationEndToEndTests):
    # Only this class's tests run here (the inherited end-to-end tests run in their own module).
    def run(self, result=None):
        if not self._testMethodName.startswith("test_owner_"):
            return result
        return super().run(result)

    def setUp(self):
        super().setUp()
        for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
            (self.root / name).write_text((orchestrator.ROOT / name).read_text())
        self.dashboard()
        self.clock = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def dashboard(self):
        self.home = user_profile.home()
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config={},
                                   browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def form(self, questions_by_employer):
        def customize(result, job):
            result["questions"].extend(questions_by_employer.get(job["employer"], []))
        self.adapter.customize = customize

    def run_cycle(self, model, days=0):
        now = (self.clock + dt.timedelta(days=days)).isoformat()
        self.adapter.now = now
        self.adapter.answering_executor = model
        applications.cycle(self.database, CONFIG, self.root, self.adapter, now)
        self.assertFalse(self.database.in_transaction)

    def page(self, path="/approvals"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return html.unescape(response.text)

    def card(self, page, text):
        cards = [card.split("</article>")[0] for card in page.split('<article class="approval-card')[1:] if text in card]
        self.assertEqual(len(cards), 1, text)
        return cards[0]

    def pending(self, key=None):
        sql = "SELECT * FROM user_decisions WHERE state='PENDING'" + (" AND job_key=?" if key else "")
        return [dict(row) for row in self.database.execute(sql, (key,) if key else ())]

    def answer(self, decision, value):
        response = self.client.post("/approvals/questions/answer", headers=ORIGIN, data={
            "decision_id": decision["id"], "payload_hash": decision["payload_hash"], "answer": value})
        self.assertEqual(response.status_code, 200)
        return html.unescape(response.text)

    # --- ASI: a required choice question --------------------------------------------------------------

    def test_owner_asi_choice_question_is_asked_with_its_options_answered_kept_and_reused(self):
        asi = self.discover(fixtures.role("ASI", "asi-1"))
        self.form({"asi": [EXPORT], "later co": [EXPORT]})
        self.run_cycle(Model("ACCEPT"))
        job = self.job(asi)
        field = applications.question_field(EXPORT)
        # An owner hold: no try spent, nothing submitted, not due again until answered or expired.
        self.assertEqual((job["state"], job["blocker"], job["attempts"]), ("BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:" + field, 0))
        [decision] = self.pending(asi)
        self.assertEqual(job["next_at"], dt.datetime.fromtimestamp(decision["expires_at"] / 1000, dt.timezone.utc).isoformat())
        self.assertEqual(self.submissions(), [])
        # The read was saved complete, so waiting never counts toward the inspect cap.
        self.assertEqual(self.database.execute("SELECT complete FROM application_requisition_records WHERE job_key=?",
                                               (asi,)).fetchone()[0], 1)
        # A cycle meanwhile does not read the page again.
        reads = len([call for call in self.adapter.calls if call[0] == "inspect"])
        self.run_cycle(Model("ACCEPT"), days=1)
        self.assertEqual(len([call for call in self.adapter.calls if call[0] == "inspect"]), reads)

        # What the owner sees: the card with the question as the form asks it and all five options.
        page = self.page()
        card = self.card(page, EXPORT["label"])
        for choice in EXPORT_CHOICES:
            self.assertIn('type="radio" name="answer" value="%s"' % choice, card)
        self.assertIn("Questions for you", page)
        import test_dashboard_overview
        test_dashboard_overview.finished_home(self.home)
        overview = self.page("/")
        self.assertIn("/approvals#questions", overview)
        applications_page = self.page("/applications")
        self.assertIn("Answer Sidedoor's question about this application on Approvals.", applications_page)
        failures = self.page("/failures")
        self.assertNotIn("UNKNOWN_REQUIRED_ANSWERS", failures)

        # An answer that is not one of the options is refused on the same card (owner, 2026-10-24).
        self.assertIn("Choose one of the form's options.", self.answer(decision, "Something else"))
        self.assertEqual([row["id"] for row in self.pending(asi)], [decision["id"]])
        green_card = EXPORT_CHOICES[1]
        self.assertIn("Answer saved.", self.answer(decision, green_card))
        self.assertIsNone(self.job(asi)["next_at"])                                  # due again now
        [(key, kept)] = setup_steps.owner_answers(self.home)
        self.assertEqual((kept["value"], kept["semantic"], kept["question"], kept["employer"], kept["choices"]),
                         (green_card, EXPORT["semantic"], EXPORT["label"], "ASI", EXPORT_CHOICES))  # as the posting writes it

        # Settings shows it: question, where first asked, the answer, its date, Edit with the options, Remove.
        settings = self.page("/settings")
        section = settings.split('id="answers"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Your saved answers", section)
        self.assertIn(EXPORT["label"], section)
        self.assertIn("First asked for ASI, Staff Software Engineer", section)
        self.assertRegex(section, r'<option value="%s" selected>' % __import__("re").escape(green_card))  # the answer is the selected option
        self.assertIn("answered " + kept["recorded_at"][:10], section)
        self.assertIn('<option value="None of the above."', section)
        self.assertIn("Remove", section)

        # Its next try uses it: the job is submitted with the chosen option.
        self.run_cycle(Model("ACCEPT"), days=1)
        self.assertEqual(self.job(asi)["state"], "SUBMITTED")
        evidence = json.loads(self.database.execute("SELECT evidence FROM application_attempts WHERE job_key=?", (asi,)).fetchone()[0])
        self.assertEqual(evidence["answers"][field], green_card)

        # A later job asking the same thing gets the saved answer through the answering AI: no new card.
        later = self.discover(fixtures.role("Later Co", "later-1"))
        model = Model("ACCEPT")
        self.run_cycle(model, days=1)
        self.assertEqual(self.job(later)["state"], "SUBMITTED")
        self.assertEqual(self.pending(), [])
        self.assertIn(key, model.packets[0]["policy"]["answers"])

        # Remove: the next job that needs it asks again.
        response = self.client.post("/settings/answers/remove", data={"key": key}, headers=ORIGIN)
        self.assertEqual(response.status_code, 303)
        third = self.discover(fixtures.role("Third Co", "third-1"))
        self.form({"third co": [EXPORT]})
        self.run_cycle(Model("ACCEPT"), days=1)
        self.assertEqual(len(self.pending(third)), 1)

    def test_owner_expired_question_is_asked_again_never_given_up(self):
        asi = self.discover(fixtures.role("ASI", "asi-2"))
        self.form({"asi": [EXPORT]})
        self.run_cycle(Model("ACCEPT"))
        [first] = self.pending(asi)
        self.run_cycle(Model("ACCEPT"), days=8)                                      # past the 7-day deadline
        job = self.job(asi)
        self.assertEqual((job["state"], job["attempts"]), ("BLOCKED", 0))
        self.assertTrue(job["blocker"].startswith("UNKNOWN_REQUIRED_ANSWERS:"))
        self.assertEqual(self.database.execute("SELECT state FROM user_decisions WHERE id=?", (first["id"],)).fetchone()[0], "EXPIRED")
        [second] = self.pending(asi)
        self.assertGreater(second["expires_at"], first["expires_at"])

    def test_owner_a_failed_answering_call_stays_a_retry_and_asks_nobody(self):
        asi = self.discover(fixtures.role("ASI", "asi-3"))
        self.form({"asi": [EXPORT]})
        self.run_cycle(Model(error=TimeoutError("model timeout")))
        self.assertTrue(self.job(asi)["blocker"].startswith("ANSWERING_RETRY:"))
        self.assertEqual(self.pending(), [])

    # --- Review round 1 -------------------------------------------------------------------------------

    def test_owner_q1_mixed_case_is_one_owner_hold_no_reread_then_the_rest_is_answered(self):
        office = {"id": None, "required": True, "semantic": "preferred_office", "answer_type": "choice",
                  "label": "Preferred office", "choices": ["Austin", "Denver"]}
        asi = self.discover(fixtures.role("ASI", "asi-4"))
        self.form({"asi": [EXPORT, office]})
        self.run_cycle(Model("ACCEPT", values={"preferred_office": "Paris"}))     # does not fit: the AI's retry
        job = self.job(asi)
        self.assertEqual((job["blocker"], job["attempts"]), ("UNKNOWN_REQUIRED_ANSWERS:" + applications.question_field(EXPORT), 0))
        [decision] = self.pending(asi)
        self.assertEqual(job["next_at"], dt.datetime.fromtimestamp(decision["expires_at"] / 1000, dt.timezone.utc).isoformat())
        self.assertEqual(self.database.execute("SELECT complete FROM application_requisition_records WHERE job_key=?",
                                               (asi,)).fetchone()[0], 1)
        reads = len([call for call in self.adapter.calls if call[0] == "inspect"])
        for day in (1, 2, 3):
            self.run_cycle(Model("ACCEPT", values={"preferred_office": "Paris"}), days=day)
        self.assertEqual(len([call for call in self.adapter.calls if call[0] == "inspect"]), reads)
        self.assertIn("Answer saved.", self.answer(decision, EXPORT_CHOICES[1]))
        self.run_cycle(Model("ACCEPT", values={"preferred_office": "Austin"}), days=3)
        self.assertEqual(self.job(asi)["state"], "SUBMITTED")

    def test_owner_q1_drifted_options_supersede_the_old_card(self):
        self.policy["answers"].pop("github", None)
        profile_home.use(self, self.policy)
        self.dashboard()
        asi = self.discover(fixtures.role("ASI", "asi-5"))
        self.form({"asi": [EXPORT, GITHUB]})
        self.run_cycle(Model("ACCEPT"))
        cards = {decision["field"]: decision for decision in self.pending(asi)}
        old = cards[applications.question_field(EXPORT)]
        self.assertIn("Answer saved.", self.answer(cards[applications.question_field(GITHUB)], "https://github.com/example-user"))
        drifted = dict(EXPORT, choices=EXPORT_CHOICES + ["Prefer not to say."])
        self.form({"asi": [drifted, GITHUB]})
        self.run_cycle(Model("ACCEPT"), days=1)
        [current] = self.pending(asi)
        self.assertEqual(json.loads(current["choices"]), drifted["choices"])
        self.assertEqual(self.database.execute("SELECT state,last_error FROM user_decisions WHERE id=?", (old["id"],)).fetchone()[:],
                         ("SUPERSEDED", "QUESTION_REASKED"))
        card = self.card(self.page(), EXPORT["label"])   # six choices: a menu (owner, 2026-10-24: radios for a few)
        self.assertEqual(card.count("<option value=") - card.count('<option value="">'), 6)

    def test_owner_q2_a_question_no_longer_missing_closes_its_card(self):
        self.policy["answers"].pop("github", None)
        profile_home.use(self, self.policy)
        self.dashboard()
        asi = self.discover(fixtures.role("ASI", "asi-6"))
        self.form({"asi": [EXPORT, GITHUB]})
        self.run_cycle(Model("ACCEPT"))
        cards = {decision["field"]: decision for decision in self.pending(asi)}
        self.assertIn("Answer saved.", self.answer(cards[applications.question_field(GITHUB)], "https://github.com/example-user"))
        self.form({"asi": [GITHUB]})                                                 # the re-read form dropped it
        self.run_cycle(Model("ACCEPT"), days=1)
        self.assertEqual(self.job(asi)["state"], "SUBMITTED")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.database.execute("SELECT state,last_error FROM user_decisions WHERE id=?",
                                               (cards[applications.question_field(EXPORT)]["id"],)).fetchone()[:],
                         ("SUPERSEDED", "QUESTION_NO_LONGER_MISSING"))
        self.assertNotIn(EXPORT["label"], self.page())

    def test_owner_q3_an_answer_on_a_database_without_the_label_column_is_kept(self):
        asi = self.discover(fixtures.role("ASI", "asi-7"))
        self.form({"asi": [EXPORT]})
        self.run_cycle(Model("ACCEPT"))
        [decision] = self.pending(asi)
        with self.database:
            self.database.execute("ALTER TABLE user_decisions DROP COLUMN label")
        with mock.patch("decision_transport.initialize"):        # as a dashboard that never re-initializes
            self.assertIn("Answer saved.", self.answer(decision, EXPORT_CHOICES[0]))
        [(_key, kept)] = setup_steps.owner_answers(self.home)
        self.assertEqual(kept["value"], EXPORT_CHOICES[0])

    # --- InsForge: required text questions --------------------------------------------------------------

    def test_owner_insforge_text_questions_get_a_text_box_and_the_answers_are_used(self):
        # Like the owner's install, this profile records no GitHub link (the example profile does).
        self.policy["answers"].pop("github", None)
        profile_home.use(self, self.policy)
        self.dashboard()
        insforge = self.discover(fixtures.role("InsForge (InstaCloud)", "insforge-1"))
        self.form({"insforge (instacloud)": [GITHUB, TWITTER]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        job = self.job(insforge)
        self.assertTrue(job["blocker"].startswith("UNKNOWN_REQUIRED_ANSWERS:"))
        self.assertEqual(job["attempts"], 0)
        page = self.page()
        for question in (GITHUB, TWITTER):
            card = self.card(page, question["label"])
            self.assertIn('<input type="text" name="answer"', card)   # a short value: one line (owner, 2026-10-24)
            self.assertNotIn('type="radio"', card)
        by_field = {decision["field"]: decision for decision in self.pending(insforge)}
        self.assertIn("Answer saved.", self.answer(by_field[applications.question_field(GITHUB)], "https://github.com/example-user"))
        self.assertEqual(self.job(insforge)["state"], "BLOCKED")
        # One answer makes the job due; the other question is still open, so it holds again on its own card.
        self.run_cycle(Model("CANNOT_ANSWER"), days=1)
        self.assertEqual([decision["field"] for decision in self.pending(insforge)], [applications.question_field(TWITTER)])
        self.assertIn("Answer saved.", self.answer(self.pending(insforge)[0], "https://x.com/example_user"))
        self.run_cycle(Model("CANNOT_ANSWER"), days=1)
        self.assertEqual(self.job(insforge)["state"], "SUBMITTED")
        saved = {answer["semantic"]: answer["value"] for _key, answer in setup_steps.owner_answers(self.home)}
        self.assertEqual(saved, {"github_profile": "https://github.com/example-user", "twitter_profile": "https://x.com/example_user"})


    # --- Typed controls (owner, 2026-10-24: Blueprints' "Graduation date" was a text box) ------------

    GRADUATION = {"id": None, "required": True, "semantic": "graduation_date", "answer_type": "date", "choices": [],
                  "label": "Graduation date", "format": "mm/dd/yyyy"}
    YEARS = {"id": None, "required": True, "semantic": "years_experience_rust", "answer_type": "number", "choices": [],
             "label": "Years of Rust experience"}
    RELOCATE = {"id": None, "required": True, "semantic": "willing_to_relocate_sf", "answer_type": "boolean", "choices": [],
                "label": "Are you willing to relocate to San Francisco?"}

    def test_owner_each_control_type_renders_the_forms_own_control(self):
        self.policy["answers"].pop("github", None)
        profile_home.use(self, self.policy)
        self.dashboard()
        key = self.discover(fixtures.role("Blueprints AI", "bp-typed"))
        self.form({"blueprints ai": [self.GRADUATION, self.YEARS, self.RELOCATE, EXPORT, GITHUB]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        page = self.page()
        date = self.card(page, "Graduation date")
        self.assertIn('<input type="date" name="answer"', date)
        self.assertIn("Graduation date</strong> · <span class=\"approval-q-required\">Required</span>", date)
        self.assertIn("Format: mm/dd/yyyy", date)
        number = self.card(page, "Years of Rust experience")
        self.assertIn('<input type="number" name="answer"', number)
        yes_no = self.card(page, self.RELOCATE["label"])
        self.assertIn('type="radio" name="answer" value="Yes"', yes_no)
        self.assertIn('type="radio" name="answer" value="No"', yes_no)
        choice = self.card(page, EXPORT["label"])
        self.assertEqual(choice.count('type="radio"'), len(EXPORT_CHOICES))
        text = self.card(page, GITHUB["label"])
        self.assertIn('<input type="text" name="answer"', text)
        self.assertNotIn("Format:", text)

    def test_owner_a_date_question_refuses_na_and_keeps_its_card(self):
        key = self.discover(fixtures.role("Blueprints AI", "bp-na"))
        self.form({"blueprints ai": [self.GRADUATION]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        [decision] = self.pending(key)
        self.assertEqual(decision["answer_type"], "date")
        for bad in ("NA", "n/a", "05/2019", "2019-13-01"):
            with self.subTest(answer=bad):
                self.assertIn("This question needs a date.", self.answer(decision, bad))
        self.assertEqual([row["id"] for row in self.pending(key)], [decision["id"]])   # the same card, not re-asked
        self.assertIsNone(self.database.execute("SELECT 1 FROM application_answers WHERE job_key=?", (key,)).fetchone())
        self.assertIn("Answer saved.", self.answer(decision, "2019-05-31"))
        stored = self.database.execute("SELECT value FROM application_answers WHERE job_key=? AND field=?",
                                       (key, decision["field"])).fetchone()[0]
        self.assertEqual(json.loads(stored), "2019-05-31")

    def test_owner_a_multi_select_question_gets_checkboxes_and_stores_a_list(self):
        stack = {"id": None, "required": True, "semantic": "production_stack", "answer_type": "multi_choice",
                 "label": "Which of these have you used in production?", "choices": ["Rust", "Go", "PostgreSQL"]}
        key = self.discover(fixtures.role("Blueprints AI", "bp-multi"))
        self.form({"blueprints ai": [stack]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        [decision] = self.pending(key)
        self.assertEqual(decision["answer_type"], "multi_choice")
        card = self.card(self.page(), stack["label"])
        for choice in stack["choices"]:
            self.assertIn('type="checkbox" name="answer_multi" value="%s"' % choice, card)

        def tick(values):
            response = self.client.post("/approvals/questions/answer", headers=ORIGIN, data={
                "decision_id": decision["id"], "payload_hash": decision["payload_hash"], "answer_multi": values})
            return html.unescape(response.text)
        self.assertIn("Tick one or more of the form's options.", tick(["Rust", "Haskell"]))
        self.assertIn("Answer saved.", tick(["Rust", "PostgreSQL"]))
        stored = self.database.execute("SELECT value FROM application_answers WHERE job_key=? AND field=?",
                                       (key, decision["field"])).fetchone()[0]
        self.assertEqual(json.loads(stored), ["Rust", "PostgreSQL"])

    # --- Optional questions (owner, 2026-10-24: "ANY question, not only required ones") -------------

    CLEARANCE = {"id": None, "required": False, "semantic": "active_security_clearance", "answer_type": "boolean",
                 "choices": [], "label": "Do you hold an active security clearance?"}

    def unsure_model(self, verdict):
        """The answering AI is unsure (UNKNOWN) about the clearance question until the owner's answer is in
        the packet; the judge returns `verdict` for that UNKNOWN."""
        inner = Model(verdict)

        def model(prompt):
            if is_judge_prompt(prompt):
                return inner(prompt)
            packet = json.loads(prompt.split(PACKET_MARKER, 1)[1])
            inner.packets.append(packet)
            owner = [answer for answer in packet["policy"]["answers"].values()
                     if answer.get("semantic") == self.CLEARANCE["semantic"]
                     and str(answer.get("source", "")).startswith("authenticated-owner-decision:")]
            answers = [entry(question, status="ANSWERED", unknown_reason=None, value=owner[-1]["value"], derivation="owner")
                       if owner else entry(question, unknown_reason="The packet does not record this.")
                       for question in packet["questions"]]
            return json.dumps({"packet_sha256": packet["packet_sha256"], "answers": answers})
        return model

    def optional_setup(self):
        # A recorded yes/no fact for the semantic is what puts an optional question to the answering AI
        # (`applications.recorded_fact_for_ai`); the AI still judges it does not answer this question.
        self.policy["answers"]["clearance"] = {"value": False, "semantic": self.CLEARANCE["semantic"], "source": "Setup"}
        profile_home.use(self, self.policy)
        self.dashboard()
        key = self.discover(fixtures.role("ASI", "asi-opt"))
        self.form({"asi": [self.CLEARANCE]})
        return key, applications.question_field(self.CLEARANCE)

    def test_owner_optional_question_both_unsure_is_asked_and_the_answer_is_used(self):
        key, field = self.optional_setup()
        self.run_cycle(self.unsure_model("ACCEPT"))
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"], job["attempts"]), ("BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:" + field, 0))
        self.assertEqual(self.submissions(), [])
        [decision] = self.pending(key)
        self.assertEqual((decision["field"], decision["answer_type"]), (field, "boolean"))
        self.card(self.page(), self.CLEARANCE["label"])
        self.assertIn("Answer saved.", self.answer(decision, "No"))
        self.run_cycle(self.unsure_model("ACCEPT"), days=1)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        evidence = json.loads(self.database.execute("SELECT evidence FROM application_attempts WHERE job_key=?", (key,)).fetchone()[0])
        self.assertIs(evidence["answers"][field], False)
        self.assertEqual(self.pending(), [])

    def test_owner_optional_question_judge_cannot_answer_is_asked(self):
        key, field = self.optional_setup()
        self.run_cycle(self.unsure_model("CANNOT_ANSWER"))
        self.assertEqual(self.job(key)["blocker"], "UNKNOWN_REQUIRED_ANSWERS:" + field)
        self.assertEqual(len(self.pending(key)), 1)

    def test_owner_optional_question_judge_revise_is_not_asked_and_left_blank(self):
        key, field = self.optional_setup()
        self.run_cycle(self.unsure_model("REVISE"))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        evidence = json.loads(self.database.execute("SELECT evidence FROM application_attempts WHERE job_key=?", (key,)).fetchone()[0])
        self.assertNotIn(field, evidence["answers"])

    # --- Blueprints (2026-10-24): an answer saved on Approvals while the applications run is going ---

    def blueprints_setup(self):
        # The owner's install: no GitHub link in Setup's answers; InsForge asked for it first.
        self.policy["answers"].pop("github", None)
        profile_home.use(self, self.policy)
        self.dashboard()
        insforge = self.discover(fixtures.role("InsForge (InstaCloud)", "insforge-1"))
        self.form({"insforge (instacloud)": [GITHUB], "blueprints ai": [GITHUB]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        [card] = self.pending(insforge)
        blueprints = self.discover(fixtures.role("Blueprints AI", "blueprints-1"))
        return insforge, card, blueprints

    def test_owner_blueprints_answer_saved_during_a_run_reaches_a_later_job_of_that_run(self):
        insforge, card, blueprints = self.blueprints_setup()
        # The run starts (and loads the profile) before the owner answers InsForge's card; the answer
        # is saved through Approvals while the run is reading Blueprints' page, as on the install
        # (run 09:15, answer 09:17, Blueprints answered 09:33).
        customize = self.adapter.customize

        def answer_during_read(result, job):
            customize(result, job)
            if job["employer"] == "blueprints ai" and not setup_steps.owner_answers(self.home):
                self.assertIn("Answer saved.", self.answer(card, "https://github.com/example-user"))
        self.adapter.customize = answer_during_read
        model = Model("CANNOT_ANSWER")
        self.run_cycle(model, days=1)
        [(key, _kept)] = setup_steps.owner_answers(self.home)
        blueprints_packets = [packet for packet in model.packets if packet["job"]["employer"] == "blueprints ai"]
        self.assertTrue(blueprints_packets)
        self.assertEqual(blueprints_packets[0]["policy"]["answers"][key]["value"], "https://github.com/example-user")
        self.assertEqual(self.pending(blueprints), [])
        self.assertEqual(self.job(blueprints)["state"], "SUBMITTED")

    def test_owner_blueprints_card_raised_after_a_save_is_closed_and_its_job_answered_again(self):
        insforge, card, blueprints = self.blueprints_setup()
        # The answer is saved while Blueprints is being answered: its packet did not have it, so it
        # raises a card; that card is closed at once and the job is due again.
        inner = Model("CANNOT_ANSWER")

        def model(prompt):
            if not is_judge_prompt(prompt) and not setup_steps.owner_answers(self.home) and "blueprints" in prompt.lower():
                reply = inner(prompt)
                self.assertIn("Answer saved.", self.answer(card, "https://github.com/example-user"))
                return reply
            return inner(prompt)
        self.run_cycle(model, days=1)
        self.assertEqual(self.pending(blueprints), [])
        closed = self.database.execute("SELECT state FROM user_decisions WHERE job_key=?", (blueprints,)).fetchall()
        self.assertEqual([row[0] for row in closed], ["STALE"])
        self.assertIsNone(self.job(blueprints)["next_at"])
        self.run_cycle(Model("CANNOT_ANSWER"), days=1)
        self.assertEqual(self.pending(blueprints), [])
        self.assertEqual(self.job(blueprints)["state"], "SUBMITTED")


    def test_owner_kept_answers_reach_the_submit_session_and_outreach_facts(self):
        # The submit session's recorded facts and outreach's verified location take the answer in
        # force per semantic, so a kept Approvals answer, newer than Setup's, is the one they get.
        setup_steps.save_owner_answer("d1", "open_to_employer_visa_sponsorship", "No", "test", self.home, yes_no=True)
        setup_steps.save_owner_answer("d2", "current_location", "Washington, DC, USA", "test", self.home)
        job = self.discover(fixtures.role("Later Co", "later-1"))
        self.run_cycle(Model("ACCEPT"), days=1)
        self.assertEqual(self.job(job)["state"], "SUBMITTED")
        [submit] = [payload for operation, payload in self.adapter.calls if operation == "submit"]
        self.assertIs(submit["recorded_facts"]["open_to_employer_visa_sponsorship"], False)
        import outreach_promotion
        self.assertEqual(outreach_promotion.verified_location(), "Washington, DC, USA")


    def test_owner_models_get_only_the_answer_in_force_per_semantic(self):
        # Setup records "does not need sponsorship" (False); a later kept Approvals answer says True.
        # Every model packet carries only the kept True for that semantic.
        semantic = "requires_visa_sponsorship_now_or_future_in_us"
        self.policy["answers"]["requires_visa_sponsorship_us"] = {"value": False, "semantic": semantic, "source": "Setup"}
        profile_home.use(self, self.policy)
        self.dashboard()
        setup_steps.save_owner_answer("d1", semantic, "Yes", "test", self.home, yes_no=True)
        question = {"id": None, "required": True, "semantic": semantic, "answer_type": "boolean", "choices": [],
                    "label": "Will you now or in the future require sponsorship?"}

        def sponsorship(packet_answers):
            return [answer["value"] for answer in packet_answers.values() if answer.get("semantic") == semantic]
        # The answering AI and judge packet, and the recorded entry it is matched to.
        import answering_agent
        policy = applications.with_current_kept_answers(user_profile.load())
        self.assertEqual(sponsorship(answering_agent.packet_policy(policy)["answers"]), [True])
        self.assertIs(answering_agent._recorded_for(policy, question, "no-such-key")["value"], True)
        # The inspect / investigate / submit read context.
        import application_browser
        import desktop_adapter
        desktop_adapter.initialize(self.database)
        job = self.discover(fixtures.role("Later Co", "later-1"))
        row = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job,)).fetchone()
        context = application_browser.context(self.root, self.database, {"job": dict(row)})
        self.assertEqual(sponsorship(context["policy"]["answers"]), [True])
        # Research qualification gets the same narrowed policy.
        self.assertEqual(sponsorship(applications.policy_for_model(policy)["answers"]), [True])
        # A job's own answer is never dropped, nor an answer without a semantic.
        mixed = dict(policy["answers"], **{"field-x": {"value": False, "semantic": semantic, "source": "authenticated-owner-decision:1"},
                                         "free": {"value": "x", "source": "Setup"}})
        kept = applications.answers_for_model(mixed)
        self.assertIn("field-x", kept)
        self.assertIn("free", kept)
        self.assertNotIn("requires_visa_sponsorship_us", kept)


if __name__ == "__main__":
    unittest.main()
