"""A refused owner answer goes back to the owner; a closed job says its real stop (owner, 2026-10-24).

Blueprints AI ("blueprints ai:unresolved:437e0fa2..."): the owner answered "Graduation date" with "NA"
on a text box. Each of three submit sessions stopped before arming with INVALID_SUPPLIED_ANSWER, each
was counted, and the job closed EXHAUSTED; its card then said "Sidedoor ran into a problem it will
retry." Now the session names the field as a structured value: an owner answer is re-asked with the
site's reason and no try is spent; an AI answer is retried as before. Fakes only.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sqlite3
import unittest

import applications
import applications_page
import desktop_adapter
import overview_page
import test_upload_failure_limit as upload

# The live rows of "blueprints ai:unresolved:437e0fa2ec3abc1a321a378c" (read with ?mode=ro, 2026-10-24),
# minimised to what this needs: the form's question as read, the owner's card, the stored answer and the
# submit session's stop.
GRADUATION = {"answer_type": "date", "id": "e499", "label": "Graduation date", "max_characters": None,
              "required": True, "semantic": "graduation_date"}
FIELD = "unidentified-question:graduation_date:9951f3e4c26e"
CARD = "cc85ac9cac86402491953ef9de67416b"   # user_decisions.id, answer_type "text" as the old card stored it
OWNER_SOURCE = "authenticated-owner-decision:" + CARD
REASON = ("The current form still requires Graduation date with Date format: mm/dd/yyyy. The supplied answer is n/a, "
          "which is not a date; no valid graduation date is supplied. The form was not armed or submitted.")
NEXT = ("Correct the saved answer for " + FIELD + " with the candidate's actual graduation date in mm/dd/yyyy format "
        "before another submission attempt.")


def refused(field=FIELD):
    return {"acknowledged": False, "prearm_blocked": True, "status": "UNKNOWN", "requires_independent_receipt": True,
            "blocked": {"code": "INVALID_SUPPLIED_ANSWER", "field": field, "reason": REASON, "next_action": NEXT}}


class RefusedAnswer(upload.UploadFailureLimitTests):
    def run(self, result=None):   # only this class's tests (the inherited ones run in their own module)
        return super().run(result) if self._testMethodName.startswith("test_refused_") else result

    def review(self):
        review = upload.UploadFailureLimitTests.review(self)
        review["questions"] = review["questions"] + [dict(GRADUATION)]
        return review

    def answered(self, source=OWNER_SOURCE, value="n/a"):
        """The answer stored before the submit: the owner's "n/a" from the old text-box card, or the AI's."""
        with self.database:
            if source == OWNER_SOURCE:
                self.database.execute(
                    "INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,"
                    "policy_hash,expires_at,state,summary,event_key,label) VALUES (?,?,?,'graduation_date','text','[]',"
                    "'p','j','q',0,'ANSWERED','s',?,'Graduation date')", (CARD, self.key, FIELD, "decision:" + CARD))
            self.database.execute("INSERT INTO application_answers VALUES (?,?,'graduation_date',?,?) ON CONFLICT(job_key,field) "
                                  "DO UPDATE SET value=excluded.value,source=excluded.source",
                                  (self.key, FIELD, json.dumps(value), source))

    def submit(self, result):
        """One submit session ending with `result`; reconcile then disposes of it. Returns its payload."""
        request = self.run_until_submit()
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        for _ in range(3):
            self.advance(1200)
            self.cycle()
            if self.row()["state"] != "RECONCILE":
                break
        return request["payload"]

    def cards(self):
        return self.database.execute("SELECT * FROM user_decisions WHERE job_key=? AND state='PENDING'", (self.key,)).fetchall()

    def owner_answers(self, value):
        [card] = self.cards()
        import decision_transport
        result = decision_transport.answer_locally(self.database, card["id"], value, shown_payload_hash=card["payload_hash"],
                                                   now=self.now)
        self.assertEqual(result["state"], "APPLIED", result)

    def refusals(self):
        return self.database.execute("SELECT COUNT(*) FROM application_events WHERE job_key=? AND event=?",
                                     (self.key, applications.OWNER_ANSWER_REFUSED_EVENT)).fetchone()[0]

    def test_refused_blueprints_replay_asks_once_spends_nothing_then_submits_the_date(self):
        self.answered()
        payload = self.submit(refused())
        self.assertEqual(payload["answers"][FIELD], "n/a")
        job = self.row()
        self.assertEqual((job["state"], job["blocker"], job["attempts"]), ("BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:" + FIELD, 0))
        [card] = self.cards()
        self.assertEqual((card["field"], card["replaces_id"], card["answer_type"], card["label"], card["required"]),
                         (FIELD, CARD, "date", "Graduation date", 1))
        self.assertEqual(card["site_reason"], REASON)
        self.assertIsNone(self.database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?",
                                                (self.key, FIELD)).fetchone())
        import approvals_page
        [shown] = approvals_page.questions(self.database, applications.dt.datetime.fromisoformat(self.now))
        self.assertEqual((shown["control"], shown["required"], shown["site_reason"]), ("date", True, REASON))
        # While the question waits nothing is submitted and no try is spent.
        submits = self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0]
        self.advance(3600)
        self.cycle()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], submits)
        # The owner picks a real date: the next submit carries it (ISO; the browser types it as mm/dd/yyyy).
        self.owner_answers("2019-05-31")
        request = self.run_until_submit()
        self.assertEqual(request["payload"]["answers"][FIELD], "2019-05-31")
        self.assertEqual((self.row()["attempts"], self.refusals(), len(self.cards())), (1, 1, 0))

    def test_refused_the_same_owner_value_twice_closes_the_job(self):
        self.answered()
        self.submit(refused())
        self.owner_answers("2019-05-31")
        self.submit(refused())                                     # the site refuses the owner's date too
        self.assertEqual(self.row()["state"], "BLOCKED")           # a different value: asked once more
        self.owner_answers("2019-05-31")
        self.submit(refused())                                     # the same value refused a second time
        job = self.row()
        self.assertEqual((job["state"], job["blocker"]), ("EXHAUSTED", applications.REFUSED_TWICE_CODE))
        self.assertEqual((self.refusals(), len(self.cards())), (2, 0))
        reader = overview_page.Reader(self.database)
        self.assertEqual(applications_page.closed_reason(reader, self.key, job["state"], job["blocker"]),
                         "The application site wouldn't accept the answer to \u201cGraduation date\u201d twice, so "
                         "nothing was submitted. Apply by hand if you want this one.")

    def until_approval_hold(self):
        """Cycle (completing any inspect read) until the job is held for approval."""
        for _ in range(8):
            next_at = self.row()["next_at"]
            if next_at and next_at > self.now:
                self.now = next_at
            self.cycle()
            if self.row()["blocker"] == applications.AWAITING_APPLICATION_APPROVAL:
                return
            request = desktop_adapter.claim(self.database, self.root, "upload-fixture", self.now)
            if request:
                self.assertEqual(request["payload"]["operation"], "inspect")
                self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
            self.advance(60)
        self.fail("not held for approval: %s" % dict(self.row()))

    def test_refused_after_approval_in_review_mode_is_held_for_approval_again(self):
        import approvals
        with self.database:
            approvals.store_mode(self.database, approvals.REVIEW)
        self.answered()
        self.until_approval_hold()
        self.assertEqual(self.row()["blocker"], applications.AWAITING_APPLICATION_APPROVAL)
        card = self.database.execute("SELECT payload_hash FROM application_approvals WHERE job_key=?", (self.key,)).fetchone()[0]
        approvals.approve_application(self.database, self.key, {}, shown_payload_hash=card, now=self.now)
        self.assertTrue(self.database.execute("SELECT source FROM application_answers WHERE job_key=? AND field=?",
                                              (self.key, FIELD)).fetchone()[0].startswith(applications.OWNER_APPROVED_SOURCE))
        self.submit(refused())                                     # the approved "n/a" is refused by the site
        self.assertEqual((self.row()["blocker"], self.row()["attempts"]), ("UNKNOWN_REQUIRED_ANSWERS:" + FIELD, 0))
        self.owner_answers("2019-05-31")
        submits = self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0]
        self.until_approval_hold()
        # The new answer was never approved: held for approval again, nothing submitted.
        self.assertEqual(self.row()["blocker"], applications.AWAITING_APPLICATION_APPROVAL)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE operation='submit'").fetchone()[0], submits)
        snapshot = json.loads(self.database.execute("SELECT snapshot FROM application_approvals WHERE job_key=?", (self.key,)).fetchone()[0])
        self.assertIn({"field": FIELD, "value": "2019-05-31"},
                      [{"field": item["field"], "value": item["value"]} for item in snapshot["questions"]])

    def test_refused_ai_answer_goes_to_the_owner_and_the_same_value_twice_closes(self):
        self.answered("ai-answer:a1 basis=MODEL_ANSWERED", "May 2019")
        self.submit(refused())
        job = self.row()
        self.assertEqual((job["state"], job["blocker"], job["attempts"]), ("BLOCKED", "UNKNOWN_REQUIRED_ANSWERS:" + FIELD, 0))
        [card] = self.cards()
        self.assertEqual((card["answer_type"], card["replaces_id"], card["site_reason"]), ("date", None, REASON))
        # The same value refused a second time closes the job, whoever gave it (owner, 2026-10-24).
        with self.database:
            self.database.execute("UPDATE user_decisions SET state='ANSWERED' WHERE id=?", (card["id"],))
        self.answered("ai-answer:a2 basis=MODEL_ANSWERED", "May 2019")
        self.submit(refused())
        job = self.row()
        self.assertEqual((job["state"], job["blocker"]), ("EXHAUSTED", applications.REFUSED_TWICE_CODE))
        self.assertEqual((self.refusals(), len(self.cards())), (1, 0))
        reader = overview_page.Reader(self.database)
        self.assertEqual(applications_page.closed_reason(reader, self.key, job["state"], job["blocker"]),
                         "The application site wouldn't accept the answer to \u201cGraduation date\u201d twice, so "
                         "nothing was submitted. Apply by hand if you want this one.")

    def test_refused_unclear_date_order_closes_at_once_without_asking(self):
        self.answered()
        self.submit({"acknowledged": False, "prearm_blocked": True, "status": "UNKNOWN",
                     "blocked": {"code": "DATE_ORDER_UNCLEAR", "field": FIELD,
                                 "next_action": "The date field shows no day/month order; apply by hand."}})
        job = self.row()
        self.assertEqual((job["state"], job["blocker"]), ("EXHAUSTED", applications.DATE_ORDER_CODE))
        self.assertEqual((len(self.cards()), self.refusals()), (0, 0))
        reader = overview_page.Reader(self.database)
        self.assertEqual(applications_page.closed_reason(reader, self.key, job["state"], job["blocker"]),
                         "The site's date field for \u201cGraduation date\u201d doesn't show whether it's day/month or "
                         "month/day, so Sidedoor didn't guess and nothing was submitted. Apply by hand if you want this one.")


class ClosedReason(unittest.TestCase):
    """This job's real event sequence (application_events, 2026-10-24), shown on Applications."""

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        applications.initialize(self.database)
        desktop_adapter.initialize(self.database)
        self.key = "blueprints ai:unresolved:437e0fa2ec3abc1a321a378c"
        events = []
        for attempt in (0, 1, 2):
            events.append(("RECONCILE", {"code": "INVALID_SUPPLIED_ANSWER", "route": "RECONCILE", "attempts": attempt,
                                         "next_action": "Correct the saved answer before another submission attempt."}))
            events.append(("RECONCILED_GUARD_NEVER_ARMED", {"request_id": "r%d" % attempt}))
            events.append(("RETRY_WAIT", {"code": "RECONCILED_ABSENT", "route": "ORDINARY", "attempts": attempt + 1})
                          if attempt < 2 else
                          ("EXHAUSTED", {"code": "RETRIES_EXHAUSTED", "route": "ORDINARY", "attempts": 3}))
        for kind, detail in events:
            self.database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES (?,?,?,?)",
                                  (self.key, "2026-10-24T15:30:22+00:00", kind, json.dumps(detail)))
        self.reader = overview_page.Reader(self.database)

    def reason(self):
        return applications_page.closed_reason(self.reader, self.key, "EXHAUSTED", "RETRIES_EXHAUSTED")

    def test_the_real_stop_is_shown_never_will_retry(self):
        reason = self.reason()
        self.assertEqual(reason, "Its last try stopped on this: The application site didn't accept one of the answers.")
        self.assertNotIn("will retry", reason)

    def test_the_field_label_is_named_when_the_session_named_the_field(self):
        question = {"id": "e499", "label": "Graduation date", "semantic": "graduation_date", "required": True,
                    "answer_type": "date"}
        field = applications.question_field(question)
        self.database.execute("INSERT INTO application_requisition_records(job_key,employer,requisition,identity_sha256,"
                              "contract_revision,questions,conditional_questions,review,complete,missing_fields,input_sha256,"
                              "observed_at,updated_at) VALUES (?,'blueprints ai','r','i','c',?,'[]','{}',1,'[]','x','t','t')",
                              (self.key, json.dumps([question])))
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,result) VALUES ('r2','b',?,'submit','{}','CONSUMED','t','t',?)",
                              (self.key, json.dumps(refused(field))))
        self.assertEqual(self.reason(), "Its last try stopped because the application site didn't accept your answer to "
                                        "“Graduation date” (it needs a date).")

    def test_an_unknown_stop_code_on_a_closed_job_has_no_retry_words(self):
        self.database.execute("INSERT INTO application_events(job_key,at,event,detail) VALUES (?,?,?,?)",
                              (self.key, "t", "RECONCILE", json.dumps({"code": "SOMETHING_NEW"})))
        self.assertEqual(self.reason(), "Its last try stopped on this: " + applications_page.CLOSED_PROBLEM_WORDS)


if __name__ == "__main__":
    unittest.main()
