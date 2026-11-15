"""A read's stop says whether it is final: a final stop Python has no rule for closes the job at once
with the AI's own sentence; anything else retries as before. A posting found closed by the submit
session, before Submit, closes the job the same way as a read (owner's picks Z1 a and Z2 a, 2026-11-14)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import access_cards
import access_outcomes
import application_recovery
import application_browser
import applications
import applications_page
import desktop_adapter
import manual_application_receipts as manual
import overview_page
import test_access_outcomes_e15_t1 as walls
import test_evidence_binding_and_hold_order as binding
import tests.test_closed_posting_abandon as closed

RAW_CODE, view_of = closed.RAW_CODE, closed.view_of

CODE = "POSTING_CANNOT_TAKE_APPLICATION"
FIXED = "Sidedoor's AI found this posting cannot take your application."
SAID = "The employer says it only takes applications from people who already live in Germany"


class FinalReadStopTest(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def read(self, code, **stated):
        fixture = self.fixture

        class Reads:
            can_submit = False

            def call(inner, operation, payload):
                return {"blocked": dict({"code": code, "next_action": "Nothing more can be read here."}, **stated),
                        "partial_review": {"facts": {"open": None}}}
        applications.process(fixture.database, fixture.job(), Reads(), fixture.policy, fixture.root, fixture.now,
                             config=fixture.config)
        job = fixture.job()
        return job["state"], job["blocker"]

    def reason(self):
        job = self.fixture.job()
        return applications_page.closed_reason(overview_page.Reader(self.fixture.database), job["job_key"],
                                               job["state"], job["blocker"])

    def test_the_prompt_declares_final_and_reason(self):
        self.assertIn('"final": true or false, "reason": "..."', application_browser.PROMPT)
        self.assertIn("final is a JSON boolean", application_browser.PROMPT)

    def recorded_reason(self):
        row = self.fixture.database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id DESC",
                                            (self.fixture.key, access_outcomes.PLAIN_CLOSED_EVENT)).fetchone()
        return json.loads(row[0]).get("ai_reason")

    def test_a_final_stop_closes_at_once_and_the_page_shows_only_the_fixed_sentence(self):
        self.assertEqual(self.read("FORM_UNAVAILABLE", final=True, reason="  " + SAID + "\n"), ("ABANDONED", CODE))
        job = self.fixture.job()
        self.assertIsNone(job["next_at"])
        events = [row[0] for row in self.fixture.database.execute("SELECT event FROM application_events WHERE job_key=?", (job["job_key"],))]
        self.assertNotIn(applications.INCONCLUSIVE_EVENT, events)
        view = view_of(self.fixture.database, job["job_key"])
        self.assertEqual((view["group"], view["needs_you"], view["closed"], view["abandon"]), ("closed", False, FIXED, None))
        self.assertEqual(view["try_again"], "Try again")
        # The AI's sentence stays in the record and is shown nowhere a user reads (owner's pick Y2 b).
        self.assertEqual(self.recorded_reason(), SAID)
        self.assertIsNone(self.reason())
        for text in [view["closed"], view["todo"], view["problem"]] + [
                line["text"] for line in applications_page.history(overview_page.Reader(self.fixture.database), job["job_key"])]:
            self.assertIsNone(RAW_CODE.search(text or ""), text)
            self.assertNotIn("Germany", text or "")

    def test_any_reason_or_none_closes_with_the_fixed_sentence_only(self):
        for said in ("The read stopped with FORM_UNAVAILABLE", None, 7):
            with self.subTest(said=said):
                self.setUp()
                self.assertEqual(self.read("FORM_UNAVAILABLE", final=True, reason=said), ("ABANDONED", CODE))
                self.assertIsNone(self.reason())
                self.assertEqual(view_of(self.fixture.database, self.fixture.key)["closed"], FIXED)

    def test_only_the_boolean_true_closes(self):
        before = self.read("FORM_UNAVAILABLE")                      # absent: today's path
        self.assertNotIn(before[0], applications.FINAL)
        for stated in ({"final": False}, {"final": "true"}, {"final": 1}, {"final": None}, {"final": {"value": True}}):
            with self.subTest(stated=stated):
                self.setUp()
                self.assertEqual(self.read("FORM_UNAVAILABLE", reason=SAID, **stated), before)

    def test_a_code_python_has_a_rule_for_ignores_final(self):
        for code in ("AUTHENTICATION_REQUIRED", "IDENTITY_MISMATCH", "REQUIRED_ELIGIBILITY_ANSWER", "INSPECTION_INCOMPLETE",
                     "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED", "SUBMISSION_OUTCOME_UNKNOWN"):
            with self.subTest(code=code):
                self.setUp()
                without = self.read(code)
                self.setUp()
                self.assertEqual(self.read(code, final=True, reason=SAID), without)
                self.assertNotEqual(without[0], "ABANDONED")
        self.setUp()
        self.assertEqual(self.read("AUTHENTICATION_REQUIRED", final=True), ("BLOCKED", "AUTHENTICATION_REQUIRED"))
        self.assertEqual(len(access_outcomes.waiting(self.fixture.database)), 1)      # its sign-in card
        self.setUp()
        self.assertEqual(self.read("ROLE_CLOSED", final=True, reason=SAID), ("ABANDONED", "POSTING_CLOSED"))

    def test_a_job_that_may_have_reached_the_employer_is_not_closed(self):
        before = self.read("FORM_UNAVAILABLE")
        self.setUp()
        with mock.patch.object(applications, "uncertain_submission", return_value=True):
            self.assertEqual(self.read("FORM_UNAVAILABLE", final=True, reason=SAID), before)


class NormalizeKeepsTheVerdictTest(unittest.TestCase):
    """The real `normalize` on each shape a stop has arrived in: `final` and `reason` reach result["blocked"]
    exactly as given."""

    def normalized(self, result):
        request = {"request_id": "synthetic", "binding": "synthetic-binding", "max_tool_calls": 40,
                   "payload": {"operation": "inspect", "job": {"job_key": "synthetic company:role-123"}}, "context": {}}
        raw = {"request_id": "synthetic", "binding": "synthetic-binding", "result_json": json.dumps(result), "evidence": []}
        trace = (json.dumps(binding.event("read0", "The form is gone", tool="browser_snapshot")) + "\n").encode()
        return application_browser.normalize(request, raw, trace, binding.OBSERVED_AT, Path("synthetic-trace.jsonl"))

    def test_every_shape_keeps_final_and_reason(self):
        stop = {"code": "FORM_UNAVAILABLE", "final": True, "reason": SAID, "next_action": "Nothing to do"}
        partial = {"facts": {"open": None}}
        shapes = {
            "blocked": {"blocked": stop, "partial_review": partial},
            "blocker": {"blocker": stop, "partial_review": partial},
            "status": {"status": "BLOCKED", "code": "FORM_UNAVAILABLE", "final": True, "reason": SAID, "partial_review": partial},
            "string": {"blocked": "FORM_UNAVAILABLE", "final": True, "reason": SAID, "partial_review": partial},
            "wrapped": {"status": "BLOCKED", "review": {"blocked": stop, "role": "Engineer"}},
            "wrapper states it": {"status": "BLOCKED", "code": "FORM_UNAVAILABLE", "final": True, "reason": SAID,
                                  "review": {"role": "Engineer"}},
        }
        for name, result in shapes.items():
            with self.subTest(shape=name):
                blocked = self.normalized(result)["blocked"]
                self.assertEqual((blocked["code"], blocked["final"], blocked["reason"]), ("FORM_UNAVAILABLE", True, SAID))

    def test_a_value_is_never_coerced(self):
        for given in ("true", 1, None, False):
            blocked = self.normalized({"blocked": {"code": "FORM_UNAVAILABLE", "final": given, "next_action": "x"}})["blocked"]
            self.assertIs(type(blocked["final"]), type(given))
            self.assertEqual(blocked["final"], given)
        self.assertNotIn("final", self.normalized({"blocked": {"code": "FORM_UNAVAILABLE", "next_action": "x"}})["blocked"])

    def test_a_read_with_no_stop_of_its_own_is_incomplete_whatever_it_says_is_final(self):
        # Everything under partial_review and no stop stated: Python's own code, which has a rule (another read).
        blocked = self.normalized({"final": True, "reason": SAID, "partial_review": {"role": "Engineer"}})["blocked"]
        self.assertEqual(blocked["code"], "INSPECTION_INCOMPLETE")
        self.assertIn("INSPECTION_INCOMPLETE", application_recovery.RECOVERABLE)


class ClosedAtSubmitTest(walls.AccessOutcomeTests):
    """Only the three tests below run here: the inherited ones belong to their own module."""

    def stopped_before_submit(self, code, arm=False):
        request = self.run_until_submit()
        if arm:
            desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
            result = dict(walls.stop(code), acknowledged=False)
        else:
            result = {"acknowledged": False, "prearm_blocked": True, "blocked": walls.stop(code)["blocked"]}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return self.row()

    def attempts(self):
        return [row[0] for row in self.database.execute("SELECT state FROM application_attempts WHERE job_key=?", (self.key,))]

    def test_closed_posting_before_submit_closes_the_job(self):
        row = self.stopped_before_submit("ROLE_CLOSED")
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("ABANDONED", "POSTING_CLOSED", None))
        self.assertEqual(self.attempts(), ["CONFIRMED_NOT_SUBMITTED"])
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        sessions = len(self.requests())
        for _ in range(4):
            self.advance(3600)
            self.cycle()
        self.assertEqual((self.row()["state"], len(self.requests())), ("ABANDONED", sessions))

    def test_closed_posting_after_arming_reconciles_and_never_closes(self):
        row = self.stopped_before_submit("ROLE_CLOSED", arm=True)
        self.assertNotIn(row["state"], applications.FINAL)
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertEqual(self.attempts(), ["SUBMITTING"])

    def test_already_applied_before_submit_closes_with_no_try_again(self):
        row = self.stopped_before_submit("ALREADY_APPLIED")
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("ABANDONED", "SITE_SHOWS_ALREADY_APPLIED", None))
        self.assertEqual(self.attempts(), ["CONFIRMED_NOT_SUBMITTED"])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_events WHERE job_key=? AND event='RETRY_WAIT'",
                                               (self.key,)).fetchone()[0], 0)
        view = view_of(self.database, self.key)
        self.assertEqual((view["group"], view["closed"], view["try_again"], view["abandon"], view["submitted_at"], view["receipt"]),
                         ("closed", "The site shows you already applied.", None, None, None, None))
        reader = overview_page.Reader(self.database)
        self.assertIsNone(applications_page.closed_reason(reader, self.key, row["state"], row["blocker"]))
        for line in applications_page.history(reader, self.key):
            self.assertIsNone(RAW_CODE.search(line["text"]), line)
        # Try again is refused for it, whoever asks, and no later round starts anything.
        self.assertFalse(access_outcomes.recovery_try_again(row["state"], row["blocker"]))
        self.assertFalse(access_cards.can_try_again(row["state"], row["blocker"]))
        with self.database:
            self.assertFalse(access_outcomes.try_again(self.database, self.key, self.now).get("reopened"))
        sessions = len(self.requests())
        for _ in range(4):
            self.advance(3600)
            self.cycle()
        self.assertEqual((self.row()["state"], len(self.requests())), ("ABANDONED", sessions))

    def test_already_applied_after_arming_reconciles_and_never_closes(self):
        row = self.stopped_before_submit("ALREADY_APPLIED", arm=True)
        self.assertNotIn(row["state"], applications.FINAL)
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertEqual(self.attempts(), ["SUBMITTING"])

    def test_already_applied_with_a_binding_mismatch_is_refused_as_before(self):
        request = self.run_until_submit()
        result = {"acknowledged": False, "prearm_blocked": True, "blocked": walls.stop("ALREADY_APPLIED")["blocked"]}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        for other in (dict(result, prearm_blocked=False), dict(result, blocked=dict(result["blocked"], next_action="Other"))):
            with self.assertRaisesRegex(ValueError, "PREARM_BLOCKER_BINDING_MISMATCH"):
                applications.hold_submission_blocker(self.database, dict(self.row()), other, self.now)
            self.database.rollback()
        self.assertNotIn(self.row()["state"], applications.FINAL)
        self.assertEqual(self.attempts(), ["SUBMITTING"])

    def test_the_read_never_stops_a_job_on_already_applied_as_a_close(self):
        self.assertEqual(application_browser.PROMPT.count("ALREADY_APPLIED") + sum(
            text.count("ALREADY_APPLIED") for name, text in application_browser.OPERATION_PROMPTS.items() if name != "submit"), 0)
        self.assertIn("arm: return blocked with code ALREADY_APPLIED and a next_action.",
                      application_browser.OPERATION_PROMPTS["submit"])

    def test_another_stop_before_submit_waits_to_reconcile_as_before(self):
        row = self.stopped_before_submit("PAGE_UNUSABLE")
        self.assertEqual((row["state"], row["blocker"]), ("RECONCILE", "PAGE_UNUSABLE"))


for _name in [name for name in dir(walls.AccessOutcomeTests) if name.startswith("test_")]:
    setattr(ClosedAtSubmitTest, _name, None)   # inherited tests are not run a second time


@unittest.skipUnless(closed.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class FinalStopPageTest(unittest.TestCase):
    setUp, page, row = (getattr(closed.ParkedJobsTest, name) for name in ("setUp", "page", "row"))

    def test_the_page_shows_only_the_fixed_sentence_and_no_raw_code(self):
        payload = json.dumps({"company": "Acme", "role": "Engineer", "url": "https://jobs.example.com/final"})
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                                  "VALUES ('final','acme','final',?,'QUEUED',?)", (payload, self.now))
            self.assertTrue(access_outcomes.close_plain(self.database, self.row("final"), "FINAL_STOP", self.now,
                                                        read_code="FORM_UNAVAILABLE", reason=SAID))
        self.assertEqual((self.row("final")["state"], self.row("final")["blocker"]), ("ABANDONED", CODE))
        page = self.page("/applications?job=final")
        text = re.sub(r"<[^>]+>", " ", page)
        self.assertIn(FIXED, text)
        self.assertNotIn("Germany", page)          # the AI's sentence is in the record only
        self.assertIn(">Try again</button>", page)
        self.assertNotIn(">Abandon</button>", page)
        self.assertNotIn("Blocked, needs you", text)
        self.assertIsNone(RAW_CODE.search(text), RAW_CODE.findall(text))


class LogPage(unittest.TestCase):
    def test_the_log_page_does_not_print_the_ais_sentence(self):
        import failures_page
        detail = {"why": "FINAL_STOP", "code": "POSTING_CANNOT_TAKE_APPLICATION", "ai_reason": "Email your resume to them."}
        self.assertIsNone(failures_page._error_text(detail))

if __name__ == "__main__":
    unittest.main()
