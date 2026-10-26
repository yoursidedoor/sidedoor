"""Review mode holds job applications too (owner, 2026-10-24).

"The one Review switch (Settings > Mode) holds EVERYTHING that goes out ... AND job applications,
including email applications. Automatic mode is unchanged." A held application waits on Approvals;
the owner can edit every answer (select, multi-select, text), the cover letter and an email's subject
and body; Approve saves exactly what is on screen and that is what is submitted on the next pass; a
question that appears after the approval holds the job again; Reject closes it, never submitted.

Each test runs the real applications cycle against the mock ATS (no browser, no network) with the
answering model off: the form's answers are recorded beforehand, as the answering step leaves them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import hashlib
import html
import importlib.util
import json
import re
from pathlib import Path
import unittest
from unittest import mock

import answering_agent
import applications
import approvals
import scheduler_loop
import test_applications_e2e as e2e

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient
    import dashboard

PORT, SECRET = 47824, "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}

START = {"id": "start", "required": True, "semantic": "earliest_start_window", "label": "When could you start?",
         "answer_type": "choice", "control": "select", "choices": ["Immediately", "Within 1 month", "Within 3 months"]}
STACK = {"id": "stack", "required": True, "semantic": "production_stack", "label": "Which of these have you used in production?",
         "answer_type": "multi_choice", "control": "checkbox", "choices": ["TypeScript", "React", "Rust", "Go"]}
WHY = {"id": "why", "required": True, "semantic": "why_this_company", "label": "Why do you want to work here?",
       "answer_type": "text", "control": "textarea", "choices": []}
NEW = {"id": "visa", "required": True, "semantic": "relocation_ok", "label": "Can you relocate?",
       "answer_type": "boolean", "control": "radio", "choices": ["Yes", "No"]}
DRAFTED = {"earliest_start_window": "Within 1 month", "production_stack": ["TypeScript", "React"],
           "why_this_company": "I have used the product daily for three years.", "relocation_ok": "Yes"}
TARGET = {"address": "careers@mockcompany.example", "subject": "Application"}
WORDED = {"id": "auth", "required": True, "semantic": "us_work_authorization", "label": "Work authorization",
          "answer_type": "boolean", "control": "radio", "choices": ["I am authorized to work", "I am not authorized"]}
# What `outcome` returned for test_automatic_mode_is_unchanged's job on main (79732b7), recorded 2026-10-25.
AUTOMATIC_ON_MAIN = json.loads('{"job": ["SUBMITTED", null, 1], "submissions": [{"keys": ["answers", "attempt_id", "candidate_email_binding", "conditional_questions_deferred", "form_revision", "idempotency_key", "job", "no_extra_answers", "questions", "recorded_facts", "resume"], "answers": {"unidentified-question:contact_email:8acd927c8ba4": "alex.example@example.com", "unidentified-question:earliest_start_window:c84a3c723a51": "Within 1 month", "unidentified-question:production_stack:4f355f91efa5": ["TypeScript", "React"], "unidentified-question:why_this_company:0597c4ed7a35": "I have used the product daily for three years."}, "resume": "mock-resume.pdf", "form_revision": "questions-sha256:6527b61f40463cace289002a4e36d012ba18d5fd0886a91244ac23579aacdc8a", "email_application": null}], "attempts": ["VERIFIED"], "events": ["DISCOVERED", "ELIGIBILITY_CHECKED", "APPLICATION_READ_COMPLETE", "HISTORY_ANSWERS_RESUME_CLEARED", "WORKER_SUBMISSION_STARTED", "SUBMISSION_VERIFIED"], "calls": ["inspect", "submit", "verify"]}')


def field(question):
    return applications.question_field(question)


class Base(unittest.TestCase):
    def setUp(self):
        self.case = e2e.ApplicationEndToEndTests(next(name for name in dir(e2e.ApplicationEndToEndTests)
                                                       if name.startswith("test_")))
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.database, self.adapter = self.case.database, self.case.adapter
        self.questions = [START, STACK, WHY]
        self.email = None
        self.inspected = []
        self.on_inspect = None
        self.mutate = None
        original = self.adapter.call

        def call(operation, payload):
            result = original(operation, payload)
            if operation == "inspect":
                self.inspected.append(payload["job"]["job_key"])
                if self.email:
                    result.update(application_method="EMAIL", email_application=self.email, questions=[])
                else:
                    result["questions"] = result["questions"] + [dict(question) for question in self.questions]
                if self.on_inspect:
                    self.on_inspect(payload["job"])
                if self.mutate:
                    self.mutate(result)
            return result
        self.adapter.call = call
        self.config = {}

    def review_mode(self, on=True):
        approvals.store_mode(self.database, approvals.REVIEW if on else approvals.AUTOMATIC)
        self.database.commit()

    def discover(self, company="Example AI", requisition="role-123"):
        key = self.case.discover(e2e.role(company, requisition))
        questions = applications.email_questions(self.email) if self.email else self.questions + [NEW]
        with self.database:
            for question in questions:
                value = DRAFTED.get(question["semantic"], "Synthetic drafted text for " + question["semantic"])
                self.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                      (key, field(question), question["semantic"], json.dumps(value), "ai-answer:synthetic"))
        return key

    def cycle(self, hours=0):
        self.adapter.now = (dt.datetime.fromisoformat(e2e.NOW) + dt.timedelta(hours=hours)).isoformat()
        applications.cycle(self.database, self.config, self.case.root, self.adapter, self.adapter.now)
        self.assertFalse(self.database.in_transaction)

    def job(self, key):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone()

    def card(self, key):
        return self.database.execute("SELECT * FROM application_approvals WHERE job_key=?", (key,)).fetchone()

    def submits(self):
        return [payload for operation, payload in self.adapter.calls if operation == "submit"]

    def outcome(self, key):
        """Everything a cycle leaves for one job, without ids and clock values: the job's state, its
        submissions (payload keys, answers, résumé file, form revision), attempts and the event kinds."""
        job = self.job(key)
        submissions = [{"keys": sorted(payload), "answers": payload["answers"], "resume": Path(payload["resume"]).name,
                        "form_revision": payload.get("form_revision"), "email_application": payload.get("email_application")}
                       for payload in self.submits()]
        return {"job": [job["state"], job["blocker"], job["attempts"]], "submissions": submissions,
                "attempts": [row[0] for row in self.database.execute("SELECT state FROM application_attempts WHERE job_key=?", (key,))],
                "events": [row[0] for row in self.database.execute("SELECT event FROM application_events WHERE job_key=? ORDER BY id", (key,))],
                "calls": [operation for operation, _ in self.adapter.calls]}

    def approve(self, key, form):
        return approvals.approve_application(self.database, key, form, shown_payload_hash=self.card(key)["payload_hash"])


class HeldInReview(Base):
    def test_review_holds_a_filled_application_before_anything_is_submitted(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        job, card = self.job(key), self.card(key)
        self.assertEqual(("BLOCKED", applications.AWAITING_APPLICATION_APPROVAL, None), (job["state"], job["blocker"], job["next_at"]))
        self.assertEqual(([], 0), (self.submits(), self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0]))
        snapshot = json.loads(card["snapshot"])
        shown = {item["field"]: item for item in snapshot["questions"]}
        self.assertEqual("WAITING", card["state"])
        self.assertEqual(["TypeScript", "React"], shown[field(STACK)]["value"])
        self.assertEqual(("select", START["choices"]), (shown[field(START)]["control"], shown[field(START)]["choices"]))
        self.cycle(hours=2)   # a held job is not picked up again: no second read
        self.assertEqual([key], self.inspected)

    def test_automatic_mode_is_unchanged(self):
        """The same job in Automatic mode ends exactly as main (79732b7) left it: AUTOMATIC_ON_MAIN was
        recorded by running this test's `outcome` against main's code."""
        key = self.discover()
        self.cycle()
        self.assertEqual(AUTOMATIC_ON_MAIN, json.loads(json.dumps(self.outcome(key))))

    def test_an_email_application_is_held_too_and_sends_the_edited_subject_and_body(self):
        self.email, self.config = TARGET, {"email_applications_enabled": True}
        self.review_mode()
        key = self.discover()
        self.cycle()
        self.assertEqual(applications.AWAITING_APPLICATION_APPROVAL, self.job(key)["blocker"])
        self.assertEqual([], self.submits())
        body = applications.email_questions(TARGET)[0]
        self.assertEqual("APPROVED", self.approve(key, {"subject": ["Staff engineer application"],
                                                        "body": ["Hello,\r\nmy edited message."]})["state"])
        self.cycle(hours=1)
        [submitted] = self.submits()
        self.assertEqual({"address": TARGET["address"], "subject": "Staff engineer application"}, submitted["email_application"])
        self.assertEqual("Hello,\nmy edited message.", submitted["answers"][field(body)])


class ApproveAndReject(Base):
    def test_approve_saves_the_edits_and_the_next_pass_submits_exactly_them(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        result = self.approve(key, {"answer:" + field(START): ["Immediately"],
                                    "answer:" + field(STACK): ["TypeScript", "React", "Rust", "Go"],
                                    "answer:" + field(WHY): ["Edited reason."]})
        self.assertEqual(("APPROVED", True), (result["state"], result["edited"]))
        saved = {row["field"]: (json.loads(row["value"]), row["source"]) for row in
                 self.database.execute("SELECT * FROM application_answers WHERE job_key=?", (key,))}
        self.assertEqual(["TypeScript", "React", "Rust", "Go"], saved[field(STACK)][0])
        self.assertTrue(saved[field(START)][1].startswith(applications.OWNER_APPROVED_SOURCE))
        self.assertEqual((applications.APPLICATION_APPROVED, None), (self.job(key)["blocker"], self.job(key)["next_at"]))
        self.cycle(hours=1)
        [submitted] = self.submits()
        self.assertEqual("Immediately", submitted["answers"][field(START)])
        self.assertEqual(["TypeScript", "React", "Rust", "Go"], submitted["answers"][field(STACK)])
        self.assertEqual("Edited reason.", submitted["answers"][field(WHY)])
        self.assertEqual("SUBMITTED", self.job(key)["state"])

    def test_a_choice_that_is_not_one_of_the_forms_options_is_refused_and_nothing_written(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        result = self.approve(key, {"answer:" + field(START): ["Next year"]})
        self.assertEqual("REFUSED", result["state"])
        self.assertEqual(("WAITING", applications.AWAITING_APPLICATION_APPROVAL), (self.card(key)["state"], self.job(key)["blocker"]))

    def test_a_question_that_appears_after_the_approval_holds_the_job_again(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        self.approve(key, {"answer:" + field(START): ["Immediately"], "answer:" + field(STACK): ["Go"]})
        self.questions = [START, STACK, WHY, NEW]
        self.cycle(hours=1)
        self.assertEqual([], self.submits())
        self.assertEqual(applications.AWAITING_APPLICATION_APPROVAL, self.job(key)["blocker"])
        card = self.card(key)
        shown = {item["field"]: item["value"] for item in json.loads(card["snapshot"])["questions"]}
        self.assertEqual(("WAITING", "Yes", "Immediately", ["Go"]),
                         (card["state"], shown[field(NEW)], shown[field(START)], shown[field(STACK)]))

    def test_reject_closes_the_job_and_it_is_never_submitted_or_retried(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        result = approvals.reject_application(self.database, key, shown_payload_hash=self.card(key)["payload_hash"],
                                              reason="Not this one")
        self.assertEqual("REJECTED", result["state"])
        self.assertEqual(("ABANDONED", applications.APPLICATION_REJECTED), (self.job(key)["state"], self.job(key)["blocker"]))
        self.cycle(hours=3)
        self.assertEqual(([key], []), (self.inspected, self.submits()))

    def test_an_approval_made_while_a_run_is_going_is_taken_in_that_run(self):
        self.review_mode()
        first = self.discover()
        self.cycle()
        self.assertEqual([], self.submits())
        second = self.discover("Other AI", "role-456")

        def approve_first(job):
            if job["job_key"] == second and self.card(first)["state"] == "WAITING":
                self.approve(first, {"answer:" + field(STACK): ["TypeScript", "React"]})
        self.on_inspect = approve_first
        self.cycle(hours=1)   # lists only the second job; the first is approved while it is read
        self.assertEqual([first], [payload["job"]["job_key"] for payload in self.submits()])
        self.assertEqual(("SUBMITTED", applications.AWAITING_APPLICATION_APPROVAL),
                         (self.job(first)["state"], self.job(second)["blocker"]))

    def test_approval_asks_the_scheduler_to_start_an_applications_run_at_its_next_tick(self):
        loop = scheduler_loop.Loop(self.case.root, clock=lambda: dt.datetime(2026, 10, 22, 9, 40, tzinfo=dt.timezone.utc),
                                   spawner=lambda name: mock.Mock(poll=lambda: 0), is_paused=lambda: False, config={})
        loop.tick()
        self.assertEqual([], loop.tick()["started"])
        scheduler_loop.install(loop)
        self.addCleanup(scheduler_loop.install, None)
        self.assertTrue(scheduler_loop.run_soon("applications"))
        self.assertEqual(["applications"], loop.tick()["started"])


class CoverageAndOptions(Base):
    def held(self, **change):
        self.review_mode()
        key = self.discover()
        self.cycle()
        self.assertEqual(applications.AWAITING_APPLICATION_APPROVAL, self.job(key)["blocker"])
        return key

    def test_a_worded_yes_no_saves_exactly_the_option_picked(self):
        self.questions = [WORDED]
        DRAFTED["us_work_authorization"] = True   # stored as a truth (no literal), shown as the form's options
        self.addCleanup(DRAFTED.pop, "us_work_authorization")
        for pick in WORDED["choices"]:
            with self.subTest(pick=pick):
                self.setUp()
                self.questions = [WORDED]
                key = self.held()
                self.approve(key, {"answer:" + field(WORDED): [pick]})
                self.cycle(hours=1)
                self.assertEqual(pick, self.submits()[0]["answers"][field(WORDED)])

    def test_an_unedited_worded_answer_is_kept_as_stored(self):
        item = {"field": "f", "answer_type": "boolean", "control": "radio", "choices": WORDED["choices"]}
        self.assertEqual("I am not authorized", approvals._fitted(dict(item, value="I am not authorized"), ["I am not authorized"]))
        self.assertIs(True, approvals._fitted(dict(item, value=True), []))   # nothing shown chosen, nothing posted
        self.assertEqual("I am authorized to work", approvals._fitted(dict(item, value=False), ["I am authorized to work"]))

    def test_a_resume_changed_after_approval_holds_the_job_again(self):
        key = self.held()
        self.approve(key, {"answer:" + field(STACK): ["Go"]})
        resume = self.case.root / "mock-resume.pdf"
        resume.write_bytes(b"another resume, chosen after the approval")
        policy = dict(self.case.policy, resume=dict(self.case.policy["resume"], sha256=hashlib.sha256(resume.read_bytes()).hexdigest()))
        profile_home.use(self.case, policy)
        self.cycle(hours=1)
        self.assertEqual(([], applications.AWAITING_APPLICATION_APPROVAL, "WAITING"),
                         (self.submits(), self.job(key)["blocker"], self.card(key)["state"]))
        self.assertEqual(policy["resume"]["sha256"], json.loads(self.card(key)["snapshot"])["resume_sha256"])

    def test_a_destination_changed_after_approval_holds_the_job_again(self):
        key = self.held()
        self.approve(key, {"answer:" + field(STACK): ["Go"]})
        self.mutate = lambda result: result.update(official_url="https://jobs.ashbyhq.com/example/role-123/apply-elsewhere")
        self.cycle(hours=1)
        self.assertEqual(([], applications.AWAITING_APPLICATION_APPROVAL), (self.submits(), self.job(key)["blocker"]))
        self.assertTrue(json.loads(self.card(key)["snapshot"])["destination"].endswith("apply-elsewhere"))

    def test_a_stored_read_without_options_is_read_afresh_before_a_card_is_shown(self):
        bare = dict(START, choices=[])
        for first, reads in ((bare, 2), (START, 1)):
            with self.subTest(options=bool(first["choices"])):
                self.setUp()
                self.questions = [first, STACK, WHY]
                self.review_mode()
                key = self.discover()
                resume = self.case.root / "mock-resume.pdf"
                kept = resume.read_bytes()
                resume.write_bytes(b"changed for one pass")   # the read is saved complete, then the job waits
                self.cycle()
                self.assertEqual("DESIGNATED_RESUME_MISMATCH", self.job(key)["blocker"])
                resume.write_bytes(kept)
                self.cycle(hours=2)
                self.assertEqual((reads, applications.AWAITING_APPLICATION_APPROVAL), (len(self.inspected), self.job(key)["blocker"]))

    def test_an_email_with_no_subject_shows_an_empty_box_and_sends_the_typed_subject(self):
        self.email, self.config = {"address": TARGET["address"], "subject": None}, {"email_applications_enabled": True}
        self.review_mode()
        key = self.discover()
        subject = applications.email_questions(self.email)[1]
        with self.database:   # the drafted subject is empty
            self.database.execute("UPDATE application_answers SET value=? WHERE job_key=? AND field=?",
                                  (json.dumps(""), key, field(subject)))
        self.cycle()
        self.assertEqual(applications.AWAITING_APPLICATION_APPROVAL, self.job(key)["blocker"])
        import approvals_page
        [card] = approvals_page.applications_waiting(self.database)
        self.assertEqual("", card["subject"])
        self.approve(key, {"subject": ["My typed subject"], "body": ["Hello."]})
        self.cycle(hours=1)
        [submitted] = self.submits()
        self.assertIsNone(submitted["email_application"]["subject"])   # the session takes the subject answer
        self.assertEqual("My typed subject", submitted["answers"][field(subject)])


class MultiChoiceAnswers(unittest.TestCase):
    def test_a_multi_choice_answer_is_a_list_of_the_forms_options(self):
        self.assertEqual(["Rust", "Go"], answering_agent.conform_value(["Rust", "Go"], STACK))
        with self.assertRaises(ValueError):
            answering_agent.conform_value(["Haskell"], STACK)


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class ApprovalsCard(Base):
    def setUp(self):
        super().setUp()
        app = dashboard.create_app(SECRET, PORT, home=None, root=self.case.root, config={},
                                   browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def test_the_card_posts_what_is_on_screen_and_that_is_submitted(self):
        self.review_mode()
        key = self.discover()
        self.cycle()
        page = html.unescape(self.client.get("/approvals").text)
        self.assertIn("Applications waiting", page)
        self.assertIn('value="Within 1 month" selected', page)
        self.assertIn('value="React" checked', page)
        response = self.client.post("/approvals/applications/approve", headers=ORIGIN, data={
            "application_id": key, "payload_hash": self.card(key)["payload_hash"],
            "answer:" + field(START): "Within 3 months", "answer:" + field(STACK): ["Rust", "TypeScript"],
            "answer:" + field(WHY): DRAFTED["why_this_company"]})
        self.assertEqual(200, response.status_code)
        self.assertIn("Approved", response.text)
        self.cycle(hours=1)
        [submitted] = self.submits()
        self.assertEqual(("Within 3 months", ["TypeScript", "Rust"]),
                         (submitted["answers"][field(START)], submitted["answers"][field(STACK)]))


    def test_a_date_question_is_a_date_picker_holding_the_drafted_date(self):
        graduated = {"id": "grad", "required": True, "semantic": "graduation_date", "label": "Graduation date",
                     "answer_type": "date", "control": "text", "choices": []}
        self.questions = [START, graduated]
        DRAFTED["graduation_date"] = "2016-05-15"
        self.addCleanup(DRAFTED.pop, "graduation_date")
        self.review_mode()
        self.discover()
        self.cycle()
        page = html.unescape(self.client.get("/approvals").text)
        name = "answer:" + field(graduated)
        self.assertRegex(page, r'<input type="date"[^>]*name="%s" value="2016-05-15"' % re.escape(name))


if __name__ == "__main__":
    unittest.main()


class ResumeWordsTest(unittest.TestCase):
    def test_the_card_names_the_users_own_file_not_the_storage_name(self):
        import applications
        raw = {"path": "/home/resume/original.docx", "file_name": "Jordan_Sample_Resume.docx", "sha256": "x"}
        ats = {"path": "/home/resume/application-1.pdf", "sha256": "y"}
        policy = {"resume_raw": raw, "resume": ats}
        self.assertEqual("ATS version (PDF, written from Jordan_Sample_Resume.docx)", applications._resume_words(ats, policy))
        self.assertEqual("Your résumé as uploaded (Jordan_Sample_Resume.docx)", applications._resume_words(raw, policy))
