"""Owner, 2026-10-01: upload failures are bounded, and free-text character limits reach the AI."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import answering_agent
import application_browser
import applications
import desktop_adapter
import test_desktop_adapter_safety as fixtures


UPLOAD_STOP = {"acknowledged": False, "prearm_blocked": True,
               "blocked": {"code": "FILE_UPLOAD_FAILED", "next_action": "Restore the file chooser"}}


class UploadFailureLimitTests(unittest.TestCase):
    setUp = fixtures.DesktopAdapterSafetyTests.setUp
    connect = fixtures.DesktopAdapterSafetyTests.connect
    write_config = fixtures.DesktopAdapterSafetyTests.write_config
    review = fixtures.DesktopAdapterSafetyTests.review
    job = fixtures.DesktopAdapterSafetyTests.job

    def cycle(self):
        return applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now)

    def advance(self, seconds):
        self.now = desktop_adapter.later(self.now, seconds)

    def row(self):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone()

    def run_until_submit(self):
        """Cycle until a submit request is claimable; complete any inspect on the way."""
        for _ in range(12):
            next_at = self.row()["next_at"]
            if next_at and next_at > self.now:
                self.now = next_at
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "upload-fixture", self.now)
            if request and request["payload"]["operation"] == "submit":
                return request
            if request:
                accepted = desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)
                self.assertTrue(accepted["accepted"])
            self.advance(60)
        self.fail("no submit request; job is %s" % dict(self.row()))

    def fail_upload(self):
        request = self.run_until_submit()
        accepted = desktop_adapter.complete(self.database, dict(request, result=dict(UPLOAD_STOP)), self.now)
        self.assertTrue(accepted["accepted"])
        # Reconcile disposes of the pre-arm stop, then closes the never-armed attempt.
        for _ in range(3):
            self.advance(1200)
            self.cycle()
            if self.row()["state"] != "RECONCILE":
                break

    def test_third_upload_failure_gives_the_job_up(self):
        self.fail_upload()
        self.assertEqual(self.row()["state"], "RETRY_WAIT")
        self.fail_upload()
        self.assertEqual(self.row()["state"], "RETRY_WAIT")
        self.fail_upload()
        job = self.job()
        self.assertEqual((job["state"], job["blocker"]), ("EXHAUSTED", applications.UPLOAD_GIVE_UP_CODE))
        spent = self.database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=?",
                                      (self.key, applications.UPLOAD_GIVE_UP_EVENT)).fetchall()
        self.assertEqual(len(spent), 1)
        self.assertEqual(len(json.loads(spent[0][0])["failures"]), 3)
        self.assertFalse(self.database.execute("SELECT 1 FROM application_attempts WHERE job_key=? "
                                               "AND state='SUBMITTING'", (self.key,)).fetchone())

    def test_an_uncertain_submission_is_never_given_up(self):
        for _ in range(2):
            self.fail_upload()
        request = self.run_until_submit()
        # This one was armed: the outcome is uncertain, whatever the history of upload failures.
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.database.execute("UPDATE desktop_requests SET result=? WHERE request_id=?",
                              (json.dumps(UPLOAD_STOP), request["request_id"]))
        # A third never-armed failure on record, as if left by an earlier session.
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,result) VALUES ('old-upload','b',?,'submit','{}','CONSUMED',?,?,?)",
                              (self.key, self.now, self.now, json.dumps(UPLOAD_STOP)))
        self.database.commit()
        self.assertEqual(len(applications.upload_failures(self.database, self.key)), 3)
        with self.database:
            self.assertFalse(applications.give_up_on_uploads(self.database, dict(self.row()), self.now))
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertNotEqual(self.row()["state"], "EXHAUSTED")

    def record(self, request_id, code, armed=False, attempt_id=None):
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) "
                              "VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?,?,?)",
                              (request_id, self.key, self.now, self.now, self.now if armed else None,
                               json.dumps(dict(UPLOAD_STOP, blocked=dict(UPLOAD_STOP["blocked"], code=code))),
                               attempt_id))
        self.database.commit()

    def attempt(self, attempt_id, state):
        self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                              "VALUES (?,?,?,?,?)", (attempt_id, self.key, self.key, state, self.now))
        self.database.commit()

    def three_failures(self):
        self.record("old-1", "RESUME_UPLOAD_FAILED")
        self.record("old-2", "FILE_UPLOAD_FAILED")
        self.record("old-3", "FILE_UPLOAD_FAILED")

    def give_up(self):
        with self.database:
            return applications.give_up_on_uploads(self.database, dict(self.row()), self.now)

    def test_the_older_resume_upload_code_counts(self):
        # Wonka 416f reaches 3 only with its 2026-09-29 RESUME_UPLOAD_FAILED.
        self.record("other", "BROWSER_CAPABILITY_UNAVAILABLE")
        self.record("old-1", "RESUME_UPLOAD_FAILED")
        self.record("old-2", "FILE_UPLOAD_FAILED")
        self.assertFalse(self.give_up())
        self.record("old-3", "RESUME_UPLOAD_FAILED")
        self.assertEqual([item["code"] for item in applications.upload_failures(self.database, self.key)],
                         ["RESUME_UPLOAD_FAILED", "FILE_UPLOAD_FAILED", "RESUME_UPLOAD_FAILED"])
        self.assertTrue(self.give_up())
        self.assertEqual(self.row()["state"], "EXHAUSTED")

    def test_process_gives_up_a_waiting_job_before_any_new_request(self):
        self.three_failures()
        self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',blocker='RECONCILED_ABSENT',next_at=NULL "
                              "WHERE job_key=?", (self.key,))
        self.database.commit()
        before = self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0]
        self.cycle()
        self.assertEqual((self.row()["state"], self.row()["blocker"]), ("EXHAUSTED", applications.UPLOAD_GIVE_UP_CODE))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], before)

    def test_a_live_submitting_attempt_alone_prevents_giving_up(self):
        self.three_failures()
        self.attempt("live", "SUBMITTING")
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertFalse(self.give_up())

    def test_an_armed_unsettled_submit_alone_prevents_giving_up(self):
        self.three_failures()
        self.attempt("settled", "CONFIRMED_NOT_SUBMITTED")
        self.record("armed-settled", "OTHER", armed=True, attempt_id="settled")
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.record("armed-open", "OTHER", armed=True, attempt_id="no-such-attempt")
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertFalse(self.give_up())

    def test_an_uncertain_blocker_alone_prevents_giving_up(self):
        self.three_failures()
        self.database.execute("UPDATE application_jobs SET blocker='SUBMISSION_OUTCOME_UNKNOWN' WHERE job_key=?", (self.key,))
        self.database.commit()
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertFalse(self.give_up())

    def test_an_embedded_frame_reopen_answers_the_upload_failures_before_it(self):
        # Owner, 2026-10-02 (Hooli): a job reopened under the embedded-form class is not given up
        # again on the failures that reopen answered; three new failures after it still give it up.
        self.three_failures()
        self.advance(60)
        # A reopen under any other defect class (Cyberdyne's) resets nothing.
        applications.event(self.database, self.key, "APPLICATION_REOPENED",
                           {"reason": {"defect_classes": ["COMPUTE_WAIT_REREADS_20261002"]}}, self.now)
        self.database.commit()
        self.assertEqual(len(applications.upload_failures(self.database, self.key)), 3)
        self.advance(60)
        applications.event(self.database, self.key, "APPLICATION_REOPENED",
                           {"reason": {"defect_classes": [applications.UPLOAD_RESET_REOPEN_CLASS]}}, self.now)
        self.database.commit()
        self.assertEqual(applications.upload_failures(self.database, self.key), [])
        self.assertFalse(self.give_up())
        self.advance(60)
        for name in ("new-1", "new-2"):
            self.record(name, "FILE_UPLOAD_FAILED")
        self.assertFalse(self.give_up())
        self.record("new-3", "FILE_UPLOAD_FAILED")
        self.assertEqual([item["request_id"] for item in applications.upload_failures(self.database, self.key)],
                         ["new-1", "new-2", "new-3"])
        self.assertTrue(self.give_up())

    def test_three_never_armed_file_chooser_timeouts_give_the_job_up(self):
        # Gigs (2026-10-02): every stop said "Timed out after 3000ms waiting for file chooser".
        timeout = dict(UPLOAD_STOP, blocked=dict(UPLOAD_STOP["blocked"],
                                                 reason="Error: Timed out after 3000ms waiting for file chooser."))
        for count, name in enumerate(("t-1", "t-2", "t-3"), 1):
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                                  "created_at,expires_at,result) VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?)",
                                  (name, self.key, self.now, self.now, json.dumps(timeout)))
            self.database.commit()
            if count < 3:
                self.assertFalse(self.give_up())
        self.assertEqual([item["request_id"] for item in applications.upload_failures(self.database, self.key)],
                         ["t-1", "t-2", "t-3"])
        self.assertTrue(self.give_up())
        self.assertEqual(self.row()["state"], "EXHAUSTED")
        self.assertEqual(self.row()["blocker"], applications.UPLOAD_GIVE_UP_CODE)

    def stored_result(self, request):
        return json.loads(self.database.execute("SELECT result FROM desktop_requests WHERE request_id=?",
                                                (request["request_id"],)).fetchone()[0])

    def test_complete_keeps_shortened_answers_armed_and_prearm(self):
        shortened = [{"field": "motivation", "label": "Why us?", "max_characters": 300,
                      "supplied_length": 477, "typed_text": "Short."}]
        request = self.run_until_submit()
        desktop_adapter.complete(self.database, dict(request, result=dict(UPLOAD_STOP, shortened_answers=shortened)), self.now)
        self.assertEqual(self.stored_result(request)["shortened_answers"], shortened)
        self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE request_id=?", (request["request_id"],))
        self.database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED'")
        self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',blocker='RECONCILED_ABSENT',next_at=NULL")
        self.database.commit()
        request = self.run_until_submit()
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        desktop_adapter.complete(self.database, dict(request, result={"acknowledged": True, "shortened_answers": shortened}), self.now)
        self.assertEqual(self.stored_result(request)["shortened_answers"], shortened)


class CharacterLimitTests(unittest.TestCase):
    def test_max_characters_reaches_the_answering_packet(self):
        question = {"id": "q1", "label": "Why this company?", "semantic": "motivation",
                    "required": True, "answer_type": "text", "max_characters": 300}
        packet = answering_agent.packet_question(question, "k")
        self.assertEqual(packet["max_characters"], 300)
        for missing in (None, 0, True, "none"):
            self.assertIsNone(answering_agent.packet_question(dict(question, max_characters=missing), "k")["max_characters"])
        self.assertIsNone(answering_agent.packet_question({"label": "x"}, "k")["max_characters"])

    def test_normalize_keeps_shortened_answers_on_a_submit(self):
        import test_evidence_binding_and_hold_order as binding
        shortened = [{"field": "motivation", "typed_text": "Short."}]
        result = binding.inspect({"acknowledged": True, "status": "UNKNOWN", "shortened_answers": shortened},
                                 [], operation="submit")
        self.assertEqual(result["shortened_answers"], shortened)
        self.assertNotIn("shortened_answers", binding.inspect({"acknowledged": True}, [], operation="submit"))

    def test_prompts_carry_the_rule(self):
        self.assertIn("FIELD_LIMIT_TOO_SHORT_FOR_FACT", application_browser.OPERATION_PROMPTS["submit"])
        self.assertIn("A FACT value is never shortened", application_browser.OPERATION_PROMPTS["submit"])
        self.assertIn("max_characters", application_browser.OPERATION_PROMPTS["inspect"])
        self.assertIn("shortened_answers", application_browser.OPERATION_PROMPTS["submit"])
        self.assertIn("rewrite it shorter yourself", application_browser.OPERATION_PROMPTS["submit"])
        self.assertNotIn("SUPPLIED_ANSWER_EXCEEDS_FIELD_LIMIT", json.dumps(application_browser.OPERATION_PROMPTS) + application_browser.PROMPT)
        with open(answering_agent.Path(__file__).resolve().parent.parent / "answering-prompt.md", encoding="utf-8") as prompt:
            self.assertIn("max_characters", prompt.read())

    def test_submit_may_open_an_embedded_forms_own_address(self):
        # Owner, 2026-10-02: the frame's address carries `token=<id>`; the submit trace guard
        # accepts opening it in a new tab (only investigate reads check URLs in code).
        self.assertIn("Embedded forms (owner, 2026-10-02)", application_browser.OPERATION_PROMPTS["submit"])
        url = "https://job-boards.eu.greenhouse.io/embed/job_app?for=engineeringathooli&token=9000000005"
        self.assertIn('browser_tabs (action "new" with that url)', application_browser.OPERATION_PROMPTS["submit"])
        item = {"type": "mcp_tool_call", "server": "browser", "tool": "browser_tabs",
                "arguments": {"action": "new", "url": url}}
        self.assertTrue(application_browser.tool_item(item, submit=True))


if __name__ == "__main__":
    unittest.main()
