"""E15.T1 (owner, 2026-11-10): a walled application is resolved, waits on the user for 7 days, or
closes with its reason and a Try again. Never retried on its own, never a silent loss."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import threading
import time
import unittest
from unittest.mock import patch

import access_outcomes
import applications
import desktop_adapter
import test_desktop_adapter_safety as fixtures
import test_upload_failure_limit as uploads


def stop(code):
    return {"blocked": {"code": code, "next_action": "The user does this step"}}


class AccessOutcomeTests(unittest.TestCase):
    setUp = fixtures.DesktopAdapterSafetyTests.setUp
    connect = fixtures.DesktopAdapterSafetyTests.connect
    write_config = fixtures.DesktopAdapterSafetyTests.write_config
    review = fixtures.DesktopAdapterSafetyTests.review
    job = fixtures.DesktopAdapterSafetyTests.job
    cycle = uploads.UploadFailureLimitTests.cycle
    advance = uploads.UploadFailureLimitTests.advance
    run_until_submit = uploads.UploadFailureLimitTests.run_until_submit

    def row(self, key=None):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key or self.key,)).fetchone()

    def requests(self, key=None):
        return self.database.execute("SELECT request_id,operation FROM desktop_requests WHERE job_key=? ORDER BY rowid",
                                     (key or self.key,)).fetchall()

    def waits(self, key=None):
        return [dict(row) for row in self.database.execute(
            "SELECT * FROM application_access_waits WHERE job_key=? ORDER BY id", (key or self.key,))]

    def events(self, kind, key=None):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id", (key or self.key, kind))]

    def blocked_session(self, code, key=None):
        """One cycle claims the job's inspect; the session stops on `code`; the next cycle records it."""
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertIsNotNone(request, "no session was started")
        self.assertEqual(request["payload"]["operation"], "inspect")
        if key:
            self.assertEqual(request["payload"]["job"]["job_key"], key)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=stop(code)), self.now)["accepted"])
        self.advance(60)
        self.cycle()

    def assert_waiting(self, reason, code, key=None):
        row = self.row(key)
        waits = [wait for wait in self.waits(key) if wait["state"] == "WAITING"]
        self.assertEqual((row["state"], row["blocker"], row["attempts"]), ("BLOCKED", code, 0))
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]["reason"], waits[0]["wall"]), (reason, code))
        self.assertEqual(waits[0]["deadline"], desktop_adapter.later(waits[0]["since"], access_outcomes.WAIT_SECONDS))
        self.assertEqual(row["next_at"], waits[0]["deadline"])
        self.assertEqual(applications.job_route(row["blocker"], row["state"]), "ORDINARY")   # dashboard unchanged until T2
        return waits[0]

    # --- Waits ---------------------------------------------------------------------------------

    def test_a_sign_in_wall_waits_on_the_user(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual(wait["host"], "jobs.ashbyhq.com")
        self.assertEqual(wait["source"], "session")
        self.assertEqual(self.events(access_outcomes.RECORDED_EVENT)[0]["reason"], "ASK_EACH_TIME")
        # The owner's notice for the code stays as today.
        self.assertTrue(self.database.execute("SELECT 1 FROM notification_outbox WHERE event_key=?",
                                              (self.key + ":AUTHENTICATION_REQUIRED",)).fetchone())

    def test_a_sign_in_code_wall_waits_as_a_sign_in(self):
        self.blocked_session("MFA_REQUIRED")
        self.assert_waiting("ASK_EACH_TIME", "MFA_REQUIRED")

    def test_a_human_check_waits_on_the_user(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        card = access_outcomes.waiting(self.database)
        self.assertEqual(len(card), 1)
        self.assertEqual((card[0]["employer"], card[0]["role"], card[0]["url"], card[0]["reason"]),
                         ("Synthetic Company", "Staff Software Engineer", "https://jobs.ashbyhq.com/synthetic/role-123", "HUMAN_CHECK"))
        self.assertEqual(card[0]["words"], access_outcomes.WAIT_REASONS["HUMAN_CHECK"])
        self.assertNotIn("—", card[0]["words"])

    def test_no_retry_while_waiting_one_blocked_session_per_wall(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        for hours in (1, 24, 24 * 5):
            self.advance(hours * 3600)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.row()["state"], "BLOCKED")

    # --- Closed ----------------------------------------------------------------------------------

    def test_seven_days_with_no_answer_closes_with_that_reason(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        wait = self.waits()[0]
        self.advance(access_outcomes.WAIT_SECONDS - 3600)
        self.cycle()
        self.assertEqual(self.row()["state"], "BLOCKED")
        self.now = wait["deadline"]
        self.cycle()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_WAITED_7_DAYS"))
        self.assertEqual((self.waits()[0]["state"], self.waits()[0]["outcome"]), ("CLOSED", "WAITED_7_DAYS"))
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT)[0]["why"], "WAITED_7_DAYS")
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 1)

    def test_abandon_closes_at_once(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            self.assertTrue(access_outcomes.abandon(self.database, self.key, self.now))
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))
        self.assertEqual(self.waits()[0]["outcome"], "ABANDONED")
        self.assertEqual(access_outcomes.waiting(self.database), [])

    def test_a_posting_that_closes_while_waiting_closes_the_job(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            self.assertTrue(access_outcomes.posting_closed(self.database, self.key, self.now))
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_POSTING_CLOSED"))
        self.assertEqual(self.waits()[0]["outcome"], "POSTING_CLOSED")
        # Only a waiting job closes this way.
        with self.database:
            self.assertFalse(access_outcomes.posting_closed(self.database, self.key, self.now))

    def test_the_skip_setting_closes_at_once(self):
        with patch.object(access_outcomes, "account_setting", return_value="SKIP"):
            self.blocked_session("AUTHENTICATION_REQUIRED")
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_SKIPPED_BY_SETTING"))
        # E15.T6: the closed wait is on record, so Try again finds a job this module closed.
        self.assertEqual([(wait["state"], wait["outcome"]) for wait in self.waits()], [("CLOSED", "SKIPPED")])
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT)[0]["wall"], "AUTHENTICATION_REQUIRED")

    def test_a_job_closed_by_another_path_settles_its_wait(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            applications.hold(self.database, dict(self.row()), "OUTSIDE_CONFIRMED_SCOPE", "Research ruled it out", self.now, "INELIGIBLE")
        self.assertEqual((self.waits()[0]["state"], self.waits()[0]["outcome"]), ("CLOSED", "OUTSIDE_CONFIRMED_SCOPE"))
        self.assertEqual(access_outcomes.waiting(self.database), [])

    def test_closed_codes_have_plain_words_on_the_applications_page(self):
        import applications_page
        for code in access_outcomes.CLOSE_CODES:
            self.assertEqual(applications_page.CODE_WORDS[code], access_outcomes.CLOSE_WORDS[code])
            self.assertNotIn("—", applications_page.CODE_WORDS[code])
            self.assertIn(code, applications_page.ACCESS_CLOSE_CODES)

    # --- Resume, repeat, Try again -----------------------------------------------------------------

    def test_resume_releases_the_hold_and_the_next_run_starts_one_session(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, self.key, self.now), [self.key])
        row = self.row()
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("RETRY_WAIT", None, None))
        self.assertEqual((self.waits()[0]["state"], self.waits()[0]["outcome"]), ("RESOLVED", "released"))
        self.assertEqual(self.events(access_outcomes.RELEASED_EVENT)[0]["wall"], "CAPTCHA_REQUIRED")
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertEqual(len(self.requests()), 2)
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, self.key, self.now), [])

    def test_a_repeated_human_check_brings_the_hold_back_never_a_close(self):
        for _ in range(3):
            self.blocked_session("CAPTCHA_REQUIRED")
            self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
            with self.database:
                access_outcomes.resume(self.database, self.key, self.now)
        self.blocked_session("CAPTCHA_REQUIRED")
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertEqual([wait["state"] for wait in self.waits()], ["RESOLVED"] * 3 + ["WAITING"])
        self.assertEqual(self.row()["state"], "BLOCKED")
        self.assertFalse(self.events(applications.ACCESS_GIVE_UP_EVENT))

    def test_try_again_reopens_a_closed_job(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
            again = access_outcomes.try_again(self.database, self.key, self.now)
        self.assertEqual((again["reopened"], again["why"]), (False, "NOT_CLOSED"))
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("QUEUED", None))
        self.assertEqual(len(self.events("APPLICATION_REOPENED")), 1)
        self.assertEqual(self.events("APPLICATION_REOPENED")[0]["previous_state"]["blocker"], "ACCESS_ABANDONED_BY_YOU")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")

    def assert_try_again_refused(self, why):
        before = (dict(self.row()), self.database.execute("SELECT COUNT(*) FROM application_events").fetchone()[0])
        with self.database:
            answer = access_outcomes.try_again(self.database, self.key, self.now)
        self.assertEqual((answer["reopened"], answer["why"]), (False, why))
        self.assertTrue(answer["words"])
        self.assertEqual((dict(self.row()), self.database.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]), before)
        self.assertEqual(self.events("APPLICATION_REOPENED"), [])
        self.cycle()
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))

    def test_try_again_never_reopens_a_job_whose_submission_is_uncertain(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        # An armed submit on record, its attempt unsettled: it may have reached the employer.
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) VALUES ('armed','b',?,'submit','{}',"
                              "'CONSUMED',?,?,?,'{}','gone')", (self.key, self.now, self.now, self.now))
        self.database.commit()
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        requests = len(self.requests())
        self.assert_try_again_refused("SUBMISSION_UNCERTAIN")
        self.assertEqual((self.row()["state"], len(self.requests())), ("ABANDONED", requests))

    def test_try_again_only_reopens_a_job_this_wall_closed(self):
        # Given up by another path, never behind a wall.
        with self.database:
            applications.hold(self.database, dict(self.row()), "RETRIES_EXHAUSTED", "Tries spent", self.now, "EXHAUSTED")
        self.assert_try_again_refused("NOT_CLOSED_BY_A_WALL")
        self.assertEqual(self.row()["state"], "EXHAUSTED")

    def test_try_again_writes_the_reopen_record_recovery_reads(self):
        import application_recovery
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        reopened = self.database.execute("SELECT id FROM application_events WHERE job_key=? AND event='APPLICATION_REOPENED'",
                                         (self.key,)).fetchall()
        self.assertEqual(len(reopened), 1)
        # Recovery starts its count after the reopen, so earlier findings do not close the job again.
        self.assertEqual(application_recovery.reopen_scope(self.database, self.key, self.now), reopened[0][0])

    # --- A wall met by the submit session --------------------------------------------------------------

    def test_a_wall_before_submit_waits_at_once_with_no_further_session(self):
        request = self.run_until_submit()
        result = {"acknowledged": False, "prearm_blocked": True, "blocked": stop("CAPTCHA_REQUIRED")["blocked"]}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.row()["state"], "BLOCKED")          # never the 15-minute reconcile loop
        wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        sessions = len(self.requests())
        for _ in range(12):
            self.advance(1200)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(len(self.requests()), sessions)
        self.assertEqual([item["id"] for item in self.waits()], [wait["id"]])

    def test_a_submit_that_armed_still_reconciles_and_never_waits_on_a_wall(self):
        request = self.run_until_submit()
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.assertTrue(desktop_adapter.complete(
            self.database, dict(request, result=dict(stop("CAPTCHA_REQUIRED"), acknowledged=False)), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.row()["state"], "RECONCILE")
        self.assertEqual(self.waits(), [])
        self.assertTrue(applications.uncertain_submission(self.database, self.key))

    # --- Hold first, then the record -------------------------------------------------------------------

    def test_a_refused_hold_leaves_no_waiting_row(self):
        with self.database:
            applications.hold(self.database, dict(self.row()), "OUTSIDE_CONFIRMED_SCOPE", "Research ruled it out", self.now, "INELIGIBLE")
            self.assertIsNone(access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now))
        self.assertEqual(self.waits(), [])
        self.assertEqual(self.events(access_outcomes.RECORDED_EVENT), [])

    # --- The closed card -------------------------------------------------------------------------------

    def test_a_closed_card_says_its_reason_once(self):
        import applications_page
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        import datetime
        view = applications_page.detail(applications_page.overview.Reader(self.database), self.key,
                                        datetime.datetime.fromisoformat(self.now), lambda moment: moment.isoformat(), None)
        self.assertEqual(view["closed"], "You abandoned this application.")
        shown = " ".join(str(view.get(part) or "") for part in ("closed", "reason", "todo"))
        self.assertEqual(shown.count("You abandoned this application."), 1)
        self.assertEqual(shown.count("apply"), 1)

    def test_a_posting_closed_card_never_says_to_apply(self):
        import applications_page
        import datetime
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.posting_closed(self.database, self.key, self.now)
        view = applications_page.detail(applications_page.overview.Reader(self.database), self.key,
                                        datetime.datetime.fromisoformat(self.now), lambda moment: moment.isoformat(), None)
        self.assertEqual(view["closed"], "The posting closed while this application waited for you.")
        self.assertEqual(view["todo"], "The posting has closed, so there is nothing for you to do.")
        shown = " ".join(str(view.get(part) or "") for part in ("closed", "reason", "todo")) + str(self.row()["next_action"])
        for word in ("apply", "Apply", "automation", "\u2014"):
            self.assertNotIn(word, shown)

    # --- Two callers at once: the check and the write stay together --------------------------------

    def race(self, call, between=None):
        """`call(connection)` runs on a second connection while this one holds the write lock, as a
        dashboard click beside the runner. Returns its answer, given after this side committed."""
        out = {}
        self.database.execute("BEGIN IMMEDIATE")

        def run():
            other = self.connect()
            try:
                with other:
                    out["answer"] = call(other)
            except Exception as error:   # the test reads it
                out["answer"] = error
            finally:
                other.close()
        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.3)
        try:
            out["mine"] = between() if between else None
            self.database.execute("COMMIT")
        finally:
            thread.join()
        return out

    def reopen_counts(self):
        return (len(self.events("APPLICATION_REOPENED")),
                self.database.execute("SELECT COUNT(*) FROM application_reopen_grants").fetchone()[0],
                self.database.execute("SELECT COUNT(*) FROM application_reopen_grant_jobs").fetchone()[0])

    def test_two_try_agains_at_once_reopen_once(self):
        # Review round 2 (R2.test_two_connections): both calls passed the checks, the second reset a
        # job the runner had already picked up.
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)

        def first_then_the_runner_picks_it_up():
            answer = access_outcomes.try_again(self.database, self.key, self.now)
            self.database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=1 WHERE job_key=?", (self.key,))
            return answer
        out = self.race(lambda other: access_outcomes.try_again(other, self.key, self.now), first_then_the_runner_picks_it_up)
        self.assertTrue(out["mine"]["reopened"])
        self.assertEqual((out["answer"]["reopened"], out["answer"]["why"]), (False, "NOT_CLOSED"))
        row = self.row()
        self.assertEqual((row["state"], row["attempts"]), ("SUBMITTING", 1))
        self.assertEqual(self.reopen_counts(), (1, 1, 1))

    def test_two_try_agains_at_once_with_nothing_in_between(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        out = self.race(lambda other: access_outcomes.try_again(other, self.key, self.now),
                        lambda: access_outcomes.try_again(self.database, self.key, self.now))
        self.assertEqual(sorted([out["mine"]["reopened"], out["answer"]["reopened"]]), [False, True])
        self.assertEqual((self.row()["state"], self.reopen_counts()), ("QUEUED", (1, 1, 1)))

    def test_try_again_refuses_a_job_that_moved_on_after_it_was_read(self):
        # The guarded write on its own: the job changes between the read and the write on one connection.
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        original = applications.uncertain_submission

        def moves_on(database, key):
            answer = original(database, key)
            database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=1 WHERE job_key=?", (key,))
            return answer
        with patch.object(applications, "uncertain_submission", moves_on), self.database:
            answer = access_outcomes.try_again(self.database, self.key, self.now)
        self.assertEqual((answer["reopened"], answer["why"]), (False, "NOT_CLOSED"))
        self.assertEqual((self.row()["state"], self.row()["attempts"], self.events("APPLICATION_REOPENED")), ("SUBMITTING", 1, []))

    def test_two_walls_recorded_at_once_leave_one_wait(self):
        job = dict(self.row())
        out = self.race(lambda other: access_outcomes.record(other, job, "AUTHENTICATION_REQUIRED", self.now),
                        lambda: access_outcomes.record(self.database, job, "AUTHENTICATION_REQUIRED", self.now))
        self.assertEqual((out["mine"], out["answer"]), ("WAITING", "WAITING"))
        self.assertEqual(len(self.waits()), 1)
        self.assertEqual(len(self.events(access_outcomes.RECORDED_EVENT)), 1)

    def test_two_resumes_at_once_release_once(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        out = self.race(lambda other: access_outcomes.resume(other, self.key, self.now),
                        lambda: access_outcomes.resume(self.database, self.key, self.now))
        self.assertEqual(sorted([out["mine"], out["answer"]]), [[], [self.key]])
        self.assertEqual(len(self.events(access_outcomes.RELEASED_EVENT)), 1)

    def test_abandon_beside_a_resume_closes_nothing_that_was_released(self):
        self.blocked_session("CAPTCHA_REQUIRED")

        def resumed_and_picked_up():
            access_outcomes.resume(self.database, self.key, self.now)
            self.database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=1 WHERE job_key=?", (self.key,))
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                                  "VALUES ('live',?,'live','SUBMITTING',?)",
                                  (self.key, self.now))
        for call in (lambda other: access_outcomes.abandon(other, self.key, self.now),
                     lambda other: access_outcomes.posting_closed(other, self.key, self.now),
                     lambda other: access_outcomes.expire(other, "2999-01-01T00:00:00+00:00")):
            out = self.race(call, resumed_and_picked_up if self.row()["state"] == "BLOCKED" else None)
            self.assertFalse(out["answer"])
        self.assertEqual(self.row()["state"], "SUBMITTING")
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT), [])

    # --- Host rule ---------------------------------------------------------------------------------

    def second_job(self, number="456"):
        with self.database:
            return applications.ingest(self.database, {
                "company": "Synthetic Company", "role": "Senior Engineer " + number,
                "url": "https://jobs.ashbyhq.com/synthetic/role-" + number, "requisition_id": "role-" + number}, self.now)

    def test_a_sign_in_wait_holds_the_host_behind_an_older_human_check(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        second = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.blocked_session("AUTHENTICATION_REQUIRED", second)
            self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", second)
            third = self.second_job("789")
            self.cycle()
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(self.requests(third), [])
        self.assertEqual(self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", third)["source"], "host:" + second)
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def test_no_session_on_a_host_whose_sign_in_is_waiting(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(self.requests(other), [])
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", other)
        self.assertEqual(wait["source"], "host:" + self.key)
        # The user signs in once: both jobs are released (R6).
        with self.database:
            self.assertEqual(sorted(access_outcomes.resume(self.database, self.key, self.now)), sorted([self.key, other]))
        self.assertEqual((self.row(other)["state"], self.row(other)["next_at"]), ("RETRY_WAIT", None))

    def test_a_human_check_does_not_hold_the_host(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["job"]["job_key"], other)

    def test_host_held_jobs_take_the_holding_deadline_and_are_released_with_it(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        first = self.waits()[0]
        self.advance(3 * 86400)
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
            held = self.waits(other)[0]
            self.assertEqual((held["source"], held["deadline"], held["since"]), ("host:" + self.key, first["deadline"], self.now))
            self.assertEqual(self.row(other)["next_at"], first["deadline"])
            # A host-held wait never holds the host itself.
            self.assertIsNone(access_outcomes.host_holder(self.database, held["host"], self.now, self.key))
            self.now = first["deadline"]
            self.cycle()
            self.assertEqual((self.row()["state"], self.row()["blocker"]), ("ABANDONED", "ACCESS_WAITED_7_DAYS"))
            released = self.waits(other)[0]
            self.assertEqual((released["state"], released["outcome"]), ("RESOLVED", access_outcomes.HOST_RELEASED))
            self.assertEqual(self.events(access_outcomes.CLOSED_EVENT, other), [])
            # Its one session, and a wall there starts a new 7 days of its own.
            self.blocked_session("AUTHENTICATION_REQUIRED", other)
        again = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", other)
        self.assertEqual(again["source"], "session")
        self.assertEqual(len(self.requests(other)), 1)

    # Owner, 2026-11-13: no release path frees a job whose submission may have reached the employer.

    def held_with_an_armed_submit(self):
        """`other` is held behind this job's sign-in and has an armed submit whose attempt is unsettled."""
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        self.assertEqual(self.waits(other)[0]["source"], "host:" + self.key)
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) VALUES ('armed','b',?,'submit','{}',"
                              "'CONSUMED',?,?,?,'{}','gone')", (other, self.now, self.now, self.now))
        self.database.commit()
        self.assertTrue(applications.uncertain_submission(self.database, other))
        return other, (dict(self.row(other)), self.waits(other))

    def assert_untouched(self, other, before):
        self.assertEqual((dict(self.row(other)), self.waits(other)), before)
        self.assertEqual(self.events(access_outcomes.RELEASED_EVENT, other), [])

    def test_a_held_job_with_an_uncertain_submission_is_not_released_by_the_holders_resume(self):
        other, before = self.held_with_an_armed_submit()
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, self.key, self.now), [self.key])
        self.assert_untouched(other, before)
        with self.database:   # nor by a resume of its own
            self.assertEqual(access_outcomes.resume(self.database, other, self.now), [])
        self.assert_untouched(other, before)

    def test_a_held_job_with_an_uncertain_submission_is_not_released_when_the_holder_is_abandoned(self):
        other, before = self.held_with_an_armed_submit()
        with self.database:
            self.assertTrue(access_outcomes.abandon(self.database, self.key, self.now))
            access_outcomes.expire(self.database, self.now)
        self.assert_untouched(other, before)

    def test_a_held_job_with_an_uncertain_submission_is_not_released_at_the_holders_seven_days(self):
        other, before = self.held_with_an_armed_submit()
        self.now = self.waits()[0]["deadline"]
        with self.database:   # the sweep the runner makes at the start of each applications run
            self.assertEqual(access_outcomes.expire(self.database, self.now), [self.key])
        self.assertEqual(self.row()["blocker"], "ACCESS_WAITED_7_DAYS")
        self.assert_untouched(other, before)
        with self.database:   # and the one it makes when the held job itself comes due
            access_outcomes.expire(self.database, self.now, only={other})
        self.assert_untouched(other, before)

    # Owner, 2026-11-13: a waiting job whose submission is uncertain is reconciled, never left unchecked,
    # and never given an ordinary read or a second submit.

    def operations(self, key):
        return [operation for _, operation in self.requests(key)]

    def held_and_submitting(self):
        """The reviewer's fixture: `second` is held behind this job's sign-in with an armed submit and
        a live SUBMITTING attempt, `third` is held too, and the holder is then abandoned."""
        second, _ = self.held_with_an_armed_submit()
        third = self.second_job("789")
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        self.database.execute("UPDATE desktop_requests SET attempt_id='att1' WHERE request_id='armed'")
        self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) "
                              "VALUES ('att1',?,'idem','SUBMITTING',?)", (second, self.now))
        self.database.commit()
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        return second, third

    def daily_cycles(self, days):
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            for _ in range(days):
                self.advance(86400)
                self.cycle()

    def test_a_waiting_job_with_a_live_submit_is_reconciled_and_never_read_or_submitted_again(self):
        second, third = self.held_and_submitting()
        self.daily_cycles(9)
        operations = self.operations(second)
        self.assertIn("reconcile", operations)
        self.assertEqual(set(operations), {"submit", "reconcile"})
        self.assertEqual(operations.count("submit"), 1)   # the armed one on record; never a second
        self.assertEqual(self.row(second)["state"], "RECONCILE")
        self.assertEqual(self.events(access_outcomes.RELEASED_EVENT, second), [])
        self.assertNotIn(self.row(third)["blocker"], ("AUTHENTICATION_REQUIRED",))   # the certain job was freed as before

    def test_the_reconcile_starts_on_the_next_round_and_keeps_its_own_pacing(self):
        second, third = self.held_and_submitting()
        made = self.now
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.advance(3600)
            self.cycle()   # the first round after it became uncertain, six days before its wait's deadline
            first = self.database.execute("SELECT operation,created_at FROM desktop_requests WHERE job_key=? ORDER BY rowid",
                                          (second,)).fetchall()[1:]
            self.assertEqual([tuple(row) for row in first], [("reconcile", self.now)])
            self.assertLess(self.now, desktop_adapter.later(made, 86400))
            # Each read comes back undecided: reconcile's own pacing spaces them (15 minutes, then
            # hours), however often a round runs. Rounds every five minutes for five hours: three reads.
            for _ in range(60):
                request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
                if request and request["payload"]["job"]["job_key"] == second:
                    self.assertEqual(request["payload"]["operation"], "reconcile")
                    desktop_adapter.complete(self.database, dict(request, result={"status": "UNKNOWN"}), self.now)
                elif request:   # the freed `third` meets the sign-in in its one session
                    desktop_adapter.complete(self.database, dict(request, result=stop("AUTHENTICATION_REQUIRED")), self.now)
                self.advance(300)
                self.cycle()
        self.assertEqual(self.operations(second), ["submit"] + ["reconcile"] * 3)
        self.assertGreater(self.row(second)["next_at"], desktop_adapter.later(self.now, 3600))
        # An ordinary waiting job is selected exactly as before: `third` now waits on its own sign-in
        # and gets no request of any kind while it waits.
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", third)
        count = len(self.requests(third))
        self.daily_cycles(5)
        self.assertEqual((len(self.requests(third)), self.row(third)["state"]), (count, "BLOCKED"))

    def test_reconciled_as_not_submitted_it_is_released_and_gets_one_session(self):
        second, _ = self.held_and_submitting()
        self.daily_cycles(8)
        # Reconcile settles it as not submitted (what `reconcile` writes for an absent application).
        self.database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED' WHERE job_key=?", (second,))
        self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE job_key=? AND operation='reconcile'", (second,))
        self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',blocker='RECONCILED_ABSENT',next_at=NULL WHERE job_key=?", (second,))
        self.database.commit()
        self.assertFalse(applications.uncertain_submission(self.database, second))
        before = self.operations(second)
        self.daily_cycles(1)
        wait = self.waits(second)[0]
        self.assertEqual((wait["state"], wait["outcome"]), ("RESOLVED", access_outcomes.HOST_RELEASED))
        # The same run goes on to its one read: an ordinary job again, and never a second submit.
        self.assertEqual(self.operations(second)[len(before):], ["inspect"])

    def test_reconciled_as_submitted_its_wait_ends_and_nothing_is_submitted_again(self):
        second, _ = self.held_and_submitting()
        self.daily_cycles(8)
        self.database.execute("UPDATE application_jobs SET state='SUBMITTED',blocker=NULL WHERE job_key=?", (second,))
        self.database.commit()
        before = self.operations(second)
        self.daily_cycles(3)
        wait = self.waits(second)[0]
        self.assertEqual((wait["state"], wait["outcome"]), ("RESOLVED", access_outcomes.JOB_FINISHED))
        self.assertEqual((self.operations(second), before.count("submit")), (before, 1))

    def test_an_uncertain_waiting_job_that_does_not_route_to_reconcile_starts_nothing(self):
        """An armed submit on record with no live attempt: its route is not RECONCILE, so it stays
        exactly where it is. Nothing but reconcile is ever started for an uncertain waiting job."""
        second, before = self.held_with_an_armed_submit()
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
        self.daily_cycles(9)
        self.assertEqual(self.operations(second), ["submit"])
        self.assert_untouched(second, before)

    def test_host_held_jobs_are_released_when_the_holding_wait_is_abandoned(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
            self.assertEqual(self.row(other)["state"], "BLOCKED")
            with self.database:
                access_outcomes.abandon(self.database, self.key, self.now)
            self.advance(3600)
            self.cycle()
            self.assertEqual(self.waits(other)[0]["outcome"], access_outcomes.HOST_RELEASED)
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["job"]["job_key"], other)

    def test_one_wall_then_a_new_job_a_day_for_twenty_days(self):
        # Review round 2: each held job's own 7-day wait held the host in turn, and no job ran for 20 days.
        self.blocked_session("AUTHENTICATION_REQUIRED")
        start, first = self.now, self.waits()[0]
        sessions, keys, free_hours = {self.key: 1}, [self.key], []
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            for hour in range(20 * 24):
                self.advance(3600)
                if hour % 24 == 0:
                    keys.append(self.second_job(str(1000 + hour)))
                self.cycle()
                if access_outcomes.host_holder(self.database, first["host"], self.now) is None:
                    free_hours.append(self.now)
                request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
                if request:
                    key = request["payload"]["job"]["job_key"]
                    sessions[key] = sessions.get(key, 0) + 1
                    desktop_adapter.complete(self.database, dict(request, result=stop("AUTHENTICATION_REQUIRED")), self.now)
        closed = [key for key in keys if self.row(key)["blocker"] == "ACCESS_WAITED_7_DAYS"]
        self.report = {"sessions": [sessions.get(key, 0) for key in keys], "closed": len(closed),
                       "first_free": free_hours[0] if free_hours else None, "free_hours": len(free_hours),
                       "states": [self.row(key)["state"] for key in keys]}
        # The host is held 7 days from the first wall and is free at that deadline, not later.
        self.assertGreaterEqual(free_hours[0], first["deadline"])
        self.assertLess(free_hours[0], desktop_adapter.later(first["deadline"], 3600))
        # Never more than one session per job per wall, and nobody closes for a wall it never met.
        self.assertLessEqual(max(sessions.values()), 1)
        # Every job that was waiting when the site was released got its one session, and so did the
        # ones released a week later; only the jobs of the last days still wait behind the newest wall.
        self.assertEqual([sessions.get(key, 0) for key in keys[:14]], [1] * 14)
        for key in closed:
            self.assertEqual(sessions.get(key), 1)
        self.assertIn(self.key, closed)
        import os
        if os.environ.get("E15_SHOW"):
            print(self.report, start, first["deadline"])

    # --- A wall result read late (review round 2) ---------------------------------------------------

    def late_ticks(self, hours, answer):
        """Hourly ticks where each session's answer lands after the tick, so it is first read an hour
        after the request was made (past the 15-minute reuse). Returns the sessions started."""
        started = 0
        for _ in range(hours):
            self.advance(3600)
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            if request:
                started += 1
                self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=answer()), self.now)["accepted"])
        return started

    def states(self, key=None):
        return [row[0] for row in self.database.execute("SELECT state FROM desktop_requests WHERE job_key=? ORDER BY rowid",
                                                        (key or self.key,))]

    def test_a_wall_result_read_late_waits_once_with_no_new_session(self):
        # The reviewer's 72 hours: 72 sessions before, 1 after.
        self.assertEqual(self.late_ticks(72, lambda: stop("AUTHENTICATION_REQUIRED")), 1)
        self.assertEqual((len(self.requests()), self.states()), (1, ["CONSUMED"]))
        row, waits = self.row(), self.waits()
        self.assertEqual((row["state"], row["blocker"]), ("BLOCKED", "AUTHENTICATION_REQUIRED"))
        self.assertEqual([(wait["state"], wait["reason"], wait["source"], wait["host"]) for wait in waits],
                         [("WAITING", "ASK_EACH_TIME", "session", "jobs.ashbyhq.com")])
        # The 7 days count from when the session ended on the wall, not from the late read.
        met = self.database.execute("SELECT at FROM desktop_events WHERE event='RESULT_COMMITTED'").fetchone()[0]
        self.assertEqual((waits[0]["since"], waits[0]["deadline"], row["next_at"]),
                         (met, desktop_adapter.later(met, access_outcomes.WAIT_SECONDS), waits[0]["deadline"]))
        self.assertLess(met, self.events(access_outcomes.RECORDED_EVENT)[0]["deadline"])
        self.assertTrue(self.database.execute("SELECT 1 FROM notification_outbox WHERE event_key=?",
                                              (self.key + ":AUTHENTICATION_REQUIRED",)).fetchone())

    def unread_wall_answers(self):
        seen, real = [], access_outcomes.unread_wall

        def watched(*args, **kwargs):
            seen.append(real(*args, **kwargs))
            return seen[-1]
        return seen, patch.object(access_outcomes, "unread_wall", watched)

    def test_a_successful_read_read_late_is_still_read_afresh(self):
        seen, watching = self.unread_wall_answers()
        with watching:
            started = self.late_ticks(6, self.review)
        self.assertGreater(started, 1)            # the 15-minute rule for page content stands
        self.assertTrue(seen)
        self.assertFalse(any(seen))
        self.assertEqual(self.waits(), [])
        self.assertNotIn("CONSUMED", self.states())

    def test_a_late_stop_that_is_not_a_wall_behaves_as_before(self):
        seen, watching = self.unread_wall_answers()
        with watching:
            self.late_ticks(6, lambda: stop("FORM_NOT_FOUND"))
        self.assertTrue(seen)
        self.assertFalse(any(seen))
        self.assertEqual(self.waits(), [])
        self.assertNotIn("CONSUMED", self.states())

    def test_an_old_wall_result_never_brings_back_a_wait_after_a_resume(self):
        self.late_ticks(3, lambda: stop("CAPTCHA_REQUIRED"))
        self.assertEqual(len(self.waits()), 1)
        with self.database:
            access_outcomes.resume(self.database, self.key, self.now)
        # Even if the old result were still unread, it is from before that wait: spent, never a new wait.
        self.database.execute("UPDATE desktop_requests SET state='DONE' WHERE job_key=?", (self.key,))
        self.database.commit()
        self.advance(3600)
        self.cycle()
        self.assertEqual([wait["state"] for wait in self.waits()], ["RESOLVED"])
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertEqual((len(self.requests()), self.states()[0]), (2, "CONSUMED"))

    def test_try_again_after_a_late_wall_starts_one_new_session(self):
        self.late_ticks(3, lambda: stop("AUTHENTICATION_REQUIRED"))
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        self.assertEqual(self.late_ticks(1, lambda: stop("AUTHENTICATION_REQUIRED")), 1)
        self.assertEqual([wait["state"] for wait in self.waits()], ["CLOSED"])
        self.assertEqual(self.late_ticks(5, lambda: stop("AUTHENTICATION_REQUIRED")), 0)
        self.assertEqual(([wait["state"] for wait in self.waits()], len(self.requests())), (["CLOSED", "WAITING"], 2))

    def test_two_late_wall_results_make_one_wait_and_both_are_spent(self):
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        desktop_adapter.complete(self.database, dict(request, result=stop("AUTHENTICATION_REQUIRED")), self.now)
        columns = [row[1] for row in self.database.execute("PRAGMA table_info(desktop_requests)")]
        copy = dict(zip(columns, self.database.execute("SELECT * FROM desktop_requests").fetchone()), request_id="second")
        self.database.execute("INSERT INTO desktop_requests(%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
                              [copy[name] for name in columns])
        self.database.commit()
        self.advance(7200)
        self.cycle()
        self.assertEqual(([wait["state"] for wait in self.waits()], self.states()), (["WAITING"], ["CONSUMED", "CONSUMED"]))
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))

    def test_a_late_wall_before_submit_waits_and_an_armed_one_still_reconciles(self):
        request = self.run_until_submit()
        result = {"acknowledged": False, "prearm_blocked": True, "blocked": stop("CAPTCHA_REQUIRED")["blocked"]}
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        sessions = len(self.requests())
        for _ in range(5):
            self.advance(3600)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual((self.row()["state"], self.row()["blocker"], len(self.requests())), ("BLOCKED", "CAPTCHA_REQUIRED", sessions))
        self.assertEqual([wait["state"] for wait in self.waits()], ["WAITING"])
        self.assertFalse(applications.uncertain_submission(self.database, self.key))

    def test_a_late_wall_on_an_armed_submit_is_never_a_wait(self):
        request = self.run_until_submit()
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.assertTrue(desktop_adapter.complete(
            self.database, dict(request, result=dict(stop("CAPTCHA_REQUIRED"), acknowledged=False)), self.now)["accepted"])
        armed = request["request_id"]
        for _ in range(3):
            self.advance(3600)
            self.cycle()
        self.assertEqual(self.waits(), [])
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertNotIn(self.row()["state"], ("BLOCKED", "ABANDONED"))
        self.assertTrue(self.database.execute("SELECT armed_at FROM desktop_requests WHERE request_id=?", (armed,)).fetchone()[0])

    def test_a_late_sign_in_wall_holds_the_host_until_its_own_deadline(self):
        self.late_ticks(2, lambda: stop("AUTHENTICATION_REQUIRED"))
        first = self.waits()[0]
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.assertEqual(self.late_ticks(3, lambda: stop("AUTHENTICATION_REQUIRED")), 0)
        held = self.waits(other)[0]
        self.assertEqual((held["source"], held["deadline"], self.requests(other)), ("host:" + self.key, first["deadline"], []))

    # --- Review round 3 ------------------------------------------------------------------------------

    def wall_left_unread(self, code="AUTHENTICATION_REQUIRED"):
        """One session ends on a wall and nothing reads its result. Returns the request."""
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=stop(code)), self.now)["accepted"])
        return request

    def review_for(self, key):
        row = self.row(key)
        payload = json.loads(row["payload"])
        return dict(self.review(), requisition=row["requisition"], employer=row["employer"],
                    official_url=payload["url"], role=payload["role"])

    def job_on(self, host, number):
        with self.database:
            return applications.ingest(self.database, {
                "company": "Company " + number, "role": "Engineer " + number,
                "url": "https://%s/careers/role-%s" % (host, number), "requisition_id": "role-" + number}, self.now)

    def view(self, key=None):
        import applications_page
        import datetime
        return applications_page.detail(applications_page.overview.Reader(self.database), key or self.key,
                                        datetime.datetime.fromisoformat(self.now), lambda moment: moment.isoformat(), None)

    def notices(self, key=None):
        return [row[0] for row in self.database.execute(
            "SELECT message FROM notification_outbox WHERE event_key LIKE ? ORDER BY rowid", ((key or self.key) + ":%",))]

    # Fix 1: a late wall result counts only if it is the job's newest request and under 7 days old.

    def test_a_wall_result_first_seen_after_eight_days_off_is_one_fresh_session(self):
        self.wall_left_unread()
        self.advance(8 * 86400)
        self.cycle()
        self.assertEqual((self.waits(), self.states()), ([], ["CONSUMED", "PENDING"]))
        self.assertNotEqual(self.row()["state"], "ABANDONED")
        # The fresh session's wall is read in time: a wait with its full 7 days, a card before any close.
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        desktop_adapter.complete(self.database, dict(request, result=stop("AUTHENTICATION_REQUIRED")), self.now)
        self.advance(60)
        self.cycle()
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertGreater(wait["deadline"], desktop_adapter.later(self.now, 6 * 86400))
        self.advance(3600)
        self.cycle()
        self.assertEqual((self.row()["state"], self.events(access_outcomes.CLOSED_EVENT), len(self.requests())), ("BLOCKED", [], 2))

    def test_an_old_wall_result_followed_by_a_good_read_is_only_spent(self):
        # A database from before this module: the wall result stayed DONE, and the job has read fine since.
        wall = self.wall_left_unread("CAPTCHA_REQUIRED")
        self.advance(3600)
        columns = [row[1] for row in self.database.execute("PRAGMA table_info(desktop_requests)")]
        good = dict(zip(columns, self.database.execute("SELECT * FROM desktop_requests").fetchone()), request_id="good",
                    created_at=self.now, expires_at=desktop_adapter.later(self.now, 1500), result=json.dumps(self.review()))
        self.database.execute("INSERT INTO desktop_requests(%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
                              [good[name] for name in columns])
        self.database.commit()
        self.advance(2 * 86400)
        self.cycle()
        after = self.row()
        self.assertEqual(self.waits(), [])
        self.assertNotIn(after["blocker"], access_outcomes.WALL_REASONS)
        self.assertNotIn(after["state"], ("BLOCKED", "ABANDONED"))
        state = dict(self.database.execute("SELECT request_id,state FROM desktop_requests").fetchall())
        self.assertEqual((state[wall["request_id"]], state[good["request_id"]]), ("CONSUMED", "DONE"))
        # As before this module: the stale good read is read afresh.
        self.assertEqual(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)["payload"]["operation"], "inspect")
        self.assertEqual(len(self.requests()), 3)

    def test_a_three_day_late_newest_wall_result_still_waits_once(self):
        self.wall_left_unread()
        met = self.now
        self.advance(3 * 86400)
        self.cycle()
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual((wait["since"], len(self.requests()), self.states()), (met, 1, ["CONSUMED"]))
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))

    def test_a_bare_owner_hold_that_goes_on_to_read_drops_as_before_and_waits_on_its_own_wall(self):
        # An owner-route hold with nothing behind it (no gate, no card): the ordinary path goes on to
        # read the posting and the hold is dropped by that read, here as before this module. What this
        # module adds: the job met this wall itself a day ago, so the read is that result. It waits on
        # its own wall, counted from when it met it, and spends no second session on it. (A hold with
        # a card behind it returns before any read: test_owner_question_hold_is_never_replaced_by_a_site_hold.)
        self.wall_left_unread()
        met = self.now
        with self.database:
            applications.hold(self.database, dict(self.row()), "REQUIRED_ELIGIBILITY_ANSWER", "Check the posting", self.now, "BLOCKED")
        self.assertEqual(applications.job_route("REQUIRED_ELIGIBILITY_ANSWER", "BLOCKED"), "OWNER")
        self.advance(86400)
        self.cycle()
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual((wait["source"], wait["since"], wait["deadline"]),
                         ("session", met, desktop_adapter.later(met, access_outcomes.WAIT_SECONDS)))
        self.assertEqual(len(self.requests()), 1)
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))

    def test_one_wall_has_one_deadline_whichever_path_records_its_wait(self):
        recorded = {}
        for path in ("unread_wall", "adapter"):
            fixtures.DesktopAdapterSafetyTests.setUp(self)
            self.wall_left_unread()
            met = self.now
            self.advance(7 * 86400 - 3600)
            with patch.object(access_outcomes, "unread_wall", return_value=False) if path == "adapter" else patch.object(
                    access_outcomes, "reused_wall_since", side_effect=AssertionError("the adapter path was taken")):
                self.cycle()
            wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
            recorded[path] = (wait["since"], wait["deadline"], self.row()["next_at"], len(self.requests()))
            self.assertEqual(recorded[path], (met, desktop_adapter.later(met, access_outcomes.WAIT_SECONDS), wait["deadline"], 1))
            self.assertIn("Deadline: " + wait["deadline"], self.notices()[0])
        self.assertEqual(recorded["unread_wall"], recorded["adapter"])

    def test_a_wall_read_in_time_still_counts_from_the_read(self):
        self.wall_left_unread()
        self.advance(600)
        self.cycle()
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual((wait["since"], self.states()), (self.now, ["DONE"]))

    # Fix 2: a job with a session of its own to read is never held behind the site.

    def three_on_one_site(self, answer):
        """Three jobs on one site get their sessions in one tick; each session ends with `answer(key)`
        before the next tick reads any of them."""
        keys = [self.key, self.second_job("456"), self.second_job("789")]
        self.cycle()
        done = []
        for _ in keys:
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            key = request["payload"]["job"]["job_key"]
            self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=answer(key)), self.now)["accepted"])
            done.append(key)
        self.assertEqual(sorted(done), sorted(keys))
        return keys

    def test_three_jobs_walled_in_one_tick_each_wait_on_their_own_result(self):
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            keys = self.three_on_one_site(lambda key: stop("AUTHENTICATION_REQUIRED"))
            self.advance(60)
            self.cycle()
            deadlines = set()
            for key in keys:
                wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", key)
                self.assertEqual(wait["source"], "session")
                deadlines.add(wait["deadline"])
            self.assertEqual(len(deadlines), 1)
            self.now = deadlines.pop()
            self.cycle()
            for hour in range(48):
                self.advance(3600)
                self.cycle()
                self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        for key in keys:
            self.assertEqual((self.row(key)["state"], self.row(key)["blocker"], len(self.requests(key))),
                             ("ABANDONED", "ACCESS_WAITED_7_DAYS", 1))
            self.assertEqual([(wait["source"], wait["outcome"]) for wait in self.waits(key)], [("session", "WAITED_7_DAYS")])

    def test_a_good_read_of_its_own_is_used_at_once_beside_a_walled_job(self):
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            good = "synthetic company:role-789"
            keys = self.three_on_one_site(lambda key: self.review_for(key) if key == good else stop("AUTHENTICATION_REQUIRED"))
            self.advance(60)
            self.cycle()
            self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", keys[0])
            self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", keys[1])
            row = self.row(good)
            self.assertEqual((self.waits(good), self.notices(good)), ([], []))
            self.assertNotIn(row["blocker"], access_outcomes.WALL_REASONS)
            self.assertNotEqual(row["state"], "BLOCKED")
            # It goes on with the read it has: the next request is its submit, with no second read.
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual((request["payload"]["job"]["job_key"], request["payload"]["operation"]), (good, "submit"))
        self.assertEqual([row[1] for row in self.requests(good)], ["inspect", "submit"])

    def test_a_request_in_flight_is_not_held_and_a_stale_good_read_is(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        job = dict(self.row(other))
        insert = ("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,expires_at,result) "
                  "VALUES (?,?,?,'inspect','{}',?,?,?,?)")
        with self.database:
            self.database.execute(insert, ("flying", "b1", other, "PENDING", self.now, desktop_adapter.later(self.now, 1500), None))
            self.assertFalse(access_outcomes.wait_on_host(self.database, job, self.now))
            # Expired without a session: nothing of its own to read any more.
            self.assertTrue(access_outcomes.own_session(self.database, other, desktop_adapter.later(self.now, 1499)))
            self.assertFalse(access_outcomes.own_session(self.database, other, desktop_adapter.later(self.now, 1500)))
            self.database.execute("UPDATE desktop_requests SET state='DONE',result='{}' WHERE request_id='flying'")
            self.assertFalse(access_outcomes.wait_on_host(self.database, job, desktop_adapter.later(self.now, 899)))
            self.assertEqual(self.waits(other), [])
            # A good read older than the reuse window is read afresh by the adapter: held, no new session.
            self.assertTrue(access_outcomes.wait_on_host(self.database, job, desktop_adapter.later(self.now, 900)))
        self.assertEqual((self.waits(other)[0]["source"], len(self.requests(other))), ("host:" + self.key, 1))

    # Fix 3 and 6: the words of a host-held wait, and one full stop.

    def test_a_host_held_job_is_told_the_truth_everywhere(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        words = "Waiting for your sign-in on this site."   # the state only, for what holds the site (owner, 2026-11-14)
        self.assertEqual(access_outcomes.HOST_HELD_WORDS, words)
        self.assertEqual(self.row(other)["next_action"], words)
        self.assertEqual(self.database.execute("SELECT required_input FROM application_blockers WHERE job_key=?",
                                               (other,)).fetchone()[0], words)
        card = [item for item in access_outcomes.waiting(self.database) if item["job_key"] == other][0]
        self.assertEqual(card["words"], words)
        notices = self.notices(other)
        self.assertEqual(len(notices), 1)
        self.assertIn("Next: " + words + " Deadline:", notices[0])
        shown = notices[0] + json.dumps(self.view(other), default=str)
        for wrong in ("closes this application", "This site needs an account", "automation", "—", ".."):
            self.assertNotIn(wrong, shown)
        # The job that met the wall keeps its own words.
        self.assertIn("This site needs an account. Sidedoor waits 7 days, then closes this application. Deadline:",
                      self.notices()[0])

    def test_no_notice_or_stored_text_has_a_doubled_full_stop(self):
        seen = 0
        for reason in access_outcomes.WAIT_REASONS:
            self.database.execute("DELETE FROM notification_outbox")
            with self.database:
                self.assertEqual(access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now,
                                                        reason=reason), "WAITING")
            texts = self.notices() + [self.row()["next_action"]]
            self.assertEqual(len(texts), 2)
            self.assertIn(access_outcomes.WAIT_REASONS[reason], texts[0])
            for text in texts:
                self.assertNotIn("..", text)
                seen += 1
            with self.database:
                access_outcomes.resume(self.database, self.key, self.now)
        for why in access_outcomes.CLOSE_REASONS:
            self.database.execute("DELETE FROM notification_outbox")
            with self.database:
                access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now)
                self.assertTrue(access_outcomes.close(self.database, dict(self.row()), why, self.now))
            for text in self.notices() + [self.row()["next_action"], access_outcomes.CLOSE_REASONS[why][1]]:
                self.assertNotIn("..", text)
                seen += 1
            with self.database:
                self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        self.assertGreaterEqual(seen, 2 * len(access_outcomes.WAIT_REASONS) + 3 * len(access_outcomes.CLOSE_REASONS))

    # Fix 4 and 5: the released job's one session, and its words.

    def released_behind_a_new_holder(self):
        """`other` is host-held, released when the holder is abandoned, and a third job then holds the
        site again before `other` has run. Returns (other, third)."""
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        self.cycle()
        self.assertEqual(self.waits(other)[0]["source"], "host:" + self.key)
        self.advance(3600)
        with self.database:
            access_outcomes.abandon(self.database, self.key, self.now)
            access_outcomes.expire(self.database, self.now)   # the release; its session is deferred
        self.assertEqual(self.waits(other)[0]["outcome"], access_outcomes.HOST_RELEASED)
        third = self.second_job("789")
        with self.database:
            self.assertEqual(access_outcomes.record(self.database, dict(self.row(third)), "AUTHENTICATION_REQUIRED", self.now), "WAITING")
        return other, third

    def test_a_released_job_keeps_its_one_session_until_it_starts_then_can_be_held_again(self):
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            other, third = self.released_behind_a_new_holder()
            # Deferred for a day (a pause, no compute): the pass is still there.
            self.advance(86400)
            with self.database:
                self.assertFalse(access_outcomes.wait_on_host(self.database, dict(self.row(other)), self.now))
            self.cycle()
            self.assertEqual(len(self.requests(other)), 1)
            self.assertEqual(len(self.waits(other)), 1)
            # Its one session runs and meets no wall. The pass is spent: with the site held again, the
            # job is held like any other once that read is stale, and starts no second session.
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            self.assertEqual(request["payload"]["job"]["job_key"], other)
            desktop_adapter.complete(self.database, dict(request, result=stop("FORM_NOT_FOUND")), self.now)
            self.advance(3600)
            self.cycle()
            self.advance(3600)
            self.cycle()
        waits = self.waits(other)
        self.assertEqual([(wait["state"], wait["source"]) for wait in waits],
                         [("RESOLVED", "host:" + self.key), ("WAITING", "host:" + third)])
        self.assertEqual(len(self.requests(other)), 1)

    def test_a_released_job_says_the_site_is_free_and_names_no_problem(self):
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            other, _ = self.released_behind_a_new_holder()
        words = "Sidedoor tries this application on its next round."
        view = self.view(other)
        self.assertEqual((self.row(other)["state"], self.row(other)["blocker"]), ("RETRY_WAIT", None))
        self.assertEqual((view["problem"], view["todo"]), (None, words))
        shown = json.dumps(view, default=str)
        for wrong in ("problem it will retry", "Will try again automatically", "automation", "—"):
            self.assertNotIn(wrong, shown)
        # A release by the user's own step says the same true line (owner, 2026-11-12, F7).
        with self.database:
            access_outcomes.resume(self.database, _, self.now)
        self.assertEqual(self.view(_)["todo"], words)

    # Fix 7: www.site.com and site.com are one site; no other subdomain is merged.

    def test_a_leading_www_is_ignored_by_the_host_rule(self):
        for holder_host, held_host in (("www.example.com", "example.com"), ("example.org", "www.example.org")):
            holder, held = self.job_on(holder_host, "h" + holder_host[:5]), self.job_on(held_host, "w" + holder_host[:5])
            apart = self.job_on("jobs." + held_host.replace("www.", ""), "j" + holder_host[:5])
            with self.database:
                access_outcomes.record(self.database, dict(self.row(holder)), "AUTHENTICATION_REQUIRED", self.now)
                self.assertEqual(self.waits(holder)[0]["host"], holder_host.replace("www.", ""))
                # A row written before this rule kept the www: it still matches.
                self.database.execute("UPDATE application_access_waits SET host=? WHERE job_key=?", (holder_host, holder))
                self.assertFalse(access_outcomes.wait_on_host(self.database, dict(self.row(apart)), self.now))
                self.assertTrue(access_outcomes.wait_on_host(self.database, dict(self.row(held)), self.now))
            self.assertEqual((self.waits(held)[0]["source"], self.waits(apart)), ("host:" + holder, []))
            # One sign-in releases both, across the www.
            with self.database:
                self.assertEqual(sorted(access_outcomes.resume(self.database, holder, self.now)), sorted([holder, held]))
        self.assertEqual([access_outcomes.site(host) for host in
                          ("WWW.Example.com", "www.jobs.example.com", "jobs.example.com", "boards.greenhouse.io", "jobs.ashbyhq.com", "www2.example.com", None)],
                         ["example.com", "jobs.example.com", "jobs.example.com", "boards.greenhouse.io", "jobs.ashbyhq.com", "www2.example.com", None])

    # --- Review round 4 ------------------------------------------------------------------------------

    def test_a_job_waiting_on_the_owner_is_never_held_behind_a_site(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            for number, code in (("456", "REQUIRED_ELIGIBILITY_ANSWER"), ("789", "DESIGNATED_RESUME_MISMATCH")):
                other = self.second_job(number)
                with self.database:
                    applications.hold(self.database, dict(self.row(other)), code, "Check this yourself", self.now, "BLOCKED")
                self.advance(3600)
                self.cycle()
                # Never a site hold while the job is on the owner's route. (This bare hold has nothing
                # behind it, so the ordinary path reads the posting as it would with no site held, and
                # from then on the job is an ordinary one; the full hold with its card is
                # test_owner_question_hold_is_never_replaced_by_a_site_hold.)
                self.assertEqual(self.waits(other), [])
                self.assertNotIn(self.row(other)["blocker"], access_outcomes.WALL_REASONS)
        self.assertTrue(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))

    def test_a_host_held_job_that_later_meets_its_own_wall_gets_its_own_notice(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        first = self.waits()[0]
        self.assertEqual(self.database.execute("SELECT event_key FROM notification_outbox").fetchall()[0][0],
                         self.key + ":AUTHENTICATION_REQUIRED")          # the ordinary key, as before
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.advance(86400)
            self.cycle()
            self.cycle()
            held = self.notices(other)
            self.assertEqual(len(held), 1)
            self.assertIn(access_outcomes.HOST_HELD_WORDS + " Deadline: " + first["deadline"], held[0])
            with self.database:
                access_outcomes.abandon(self.database, self.key, self.now)
            self.advance(3600)
            self.blocked_session("AUTHENTICATION_REQUIRED", other)
            own = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", other)
            # Held once more behind nobody: its own wall repeated after a release writes no third notice.
            self.cycle()
        notices = dict(self.database.execute("SELECT event_key,message FROM notification_outbox WHERE event_key LIKE ?", (other + ":%",)).fetchall())
        self.assertEqual(sorted(notices), [other + ":AUTHENTICATION_REQUIRED", other + ":AUTHENTICATION_REQUIRED:SITE_HOLD:%d" % first["id"]])
        self.assertEqual(notices[other + ":AUTHENTICATION_REQUIRED:SITE_HOLD:%d" % first["id"]], held[0])
        self.assertIn("This site needs an account. Sidedoor waits 7 days, then closes this application. Deadline: " + own["deadline"],
                      notices[other + ":AUTHENTICATION_REQUIRED"])
        self.assertNotEqual(own["deadline"], first["deadline"])
        self.assertEqual(len(self.notices()), 1)        # the job that only met its own wall: one notice
        # Recent notices still reads both keys as this job's.
        import approvals_page
        for key in notices:
            view = approvals_page.notice_view(self.database, key, notices[key], None)
            self.assertEqual((view["href"], view["text"].endswith("The application site asked for a sign-in.")),
                             (approvals_page._job_href(other), True))

    def test_a_job_held_behind_two_holders_in_turn_gets_one_notice_per_holding_wait(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
            self.cycle()
            with self.database:
                access_outcomes.abandon(self.database, self.key, self.now)
                access_outcomes.expire(self.database, self.now)
            third = self.second_job("789")
            with self.database:
                access_outcomes.record(self.database, dict(self.row(third)), "AUTHENTICATION_REQUIRED", self.now)
            # Its one session runs, meets no wall, and the read goes stale: held again, by the new holder.
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            self.assertEqual(request["payload"]["job"]["job_key"], other)
            desktop_adapter.complete(self.database, dict(request, result=stop("FORM_NOT_FOUND")), self.now)
            for _ in range(3):
                self.advance(3600)
                self.cycle()
        self.assertEqual([wait["source"] for wait in self.waits(other)], ["host:" + self.key, "host:" + third])
        self.assertEqual(len(self.notices(other)), 2)

    # --- Review round 5: each wait of a job's own is told once ------------------------------------------

    def notice_rows(self, key=None):
        return self.database.execute("SELECT event_key,message FROM notification_outbox WHERE event_key LIKE ? ORDER BY rowid",
                                     ((key or self.key) + ":%",)).fetchall()

    def assert_told(self, count, code="AUTHENTICATION_REQUIRED"):
        """`count` notices, the newest for the open wait: its key, its words and its deadline."""
        rows, wait = self.notice_rows(), [wait for wait in self.waits() if wait["state"] == "WAITING"][0]
        self.assertEqual([row[0] for row in rows],
                         [self.key + ":" + code + (":WAIT:%d" % number if number > 1 else "") for number in range(1, count + 1)])
        self.assertIn("Sidedoor waits 7 days, then closes this application. Deadline: " + wait["deadline"] + ".", rows[-1][1])
        import approvals_page
        view = approvals_page.notice_view(self.database, rows[-1][0], rows[-1][1], None)
        self.assertEqual(view["href"], approvals_page._job_href(self.key))
        self.assertNotIn("WAIT", view["text"])
        return rows

    def test_one_wait_is_told_once_however_many_ticks_pass(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assert_told(1)
        for _ in range(50):
            self.advance(3600)
            self.cycle()
        with self.database:   # and a second record for the same open wait
            access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now)
        self.assert_told(1)

    def test_the_same_wall_after_try_again_is_told_again_with_its_new_deadline(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        first = self.assert_told(1)
        self.now = self.waits()[0]["deadline"]
        self.cycle()
        self.assertEqual(self.row()["blocker"], "ACCESS_WAITED_7_DAYS")
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        self.blocked_session("AUTHENTICATION_REQUIRED")
        rows = self.assert_told(2)
        self.assertEqual(rows[0], first[0])                 # the first notice stays as it was
        self.assertNotEqual(self.waits()[0]["deadline"], self.waits()[1]["deadline"])
        for _ in range(10):
            self.advance(3600)
            self.cycle()
        self.assert_told(2)

    def test_a_wall_again_after_the_user_signed_in_is_told_again(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        with self.database:
            access_outcomes.resume(self.database, self.key, self.now)
        self.advance(86400)
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assert_told(2)

    def test_a_repeated_human_check_is_told_once_per_wait(self):
        for count in (1, 2, 3):
            self.blocked_session("CAPTCHA_REQUIRED")
            self.assert_told(count, "CAPTCHA_REQUIRED")
            with self.database:
                access_outcomes.resume(self.database, self.key, self.now)
            self.advance(3600)
        # Another wall on the same job starts its own count.
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assertEqual([row[0] for row in self.notice_rows()][3:], [self.key + ":AUTHENTICATION_REQUIRED"])

    def test_a_wait_already_told_under_the_old_key_is_not_told_twice(self):
        # A wait recorded by the code before this rule, mid-wait: its notice is under `job:code`.
        self.blocked_session("AUTHENTICATION_REQUIRED")
        told = self.notice_rows()
        self.assertEqual([row[0] for row in told], [self.key + ":AUTHENTICATION_REQUIRED"])
        applications.initialize(self.database)      # opened again by this code
        for _ in range(5):
            self.advance(3600)
            self.cycle()
        with self.database:
            access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now)
        self.assertEqual(self.notice_rows(), told)

    def test_a_notice_from_before_this_module_is_not_repeated_by_the_first_wait(self):
        # From before the waits existed: the wall's notice is there, no wait row. The first wait adds none.
        with self.database:
            applications.notify(self.database, self.key + ":AUTHENTICATION_REQUIRED", "Told before", self.now)
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assertEqual([tuple(row) for row in self.notice_rows()], [(self.key + ":AUTHENTICATION_REQUIRED", "Told before")])

    # --- Review round 7: one answer to "will this read be reused", and finished jobs --------------------

    def skewed_cycle(self, start, live, clock=True):
        """A long cycle: it started at `start` (its `now`) and reaches the job at `live` (the adapter's
        clock). With `clock` the cycle runs as scheduled ones do, on the real clock (`applications.stamp`):
        its first reading is the start, every later one the live time."""
        self.now = live
        if not clock:
            return applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=start)
        readings = iter([start])
        with patch.object(applications, "stamp", lambda: next(readings, self.now)):
            applications.cycle(self.database, self.config, self.root, adapter=self.adapter)

    def assert_one_session_and_its_wait(self):
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual((len(self.requests()), wait["source"]), (1, "session"))
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        return wait

    def test_a_wall_result_reached_late_in_a_long_cycle_is_one_session(self):
        for clock in (True, False):
            with self.subTest(clock=clock):
                fixtures.DesktopAdapterSafetyTests.setUp(self)
                self.wall_left_unread()
                met = self.now
                self.skewed_cycle(desktop_adapter.later(met, 14 * 60), desktop_adapter.later(met, 16 * 60), clock)
                wait = self.assert_one_session_and_its_wait()
                self.assertEqual(self.row()["state"], "BLOCKED")
                if clock:
                    self.assertEqual((wait["since"], self.states()), (met, ["CONSUMED"]))

    def test_the_reuse_mark_passing_between_the_check_and_the_read_is_one_session(self):
        self.wall_left_unread()
        met, real = self.now, access_outcomes.unread_wall

        def then_time_passes(*args, **kwargs):
            answer = real(*args, **kwargs)
            self.now = desktop_adapter.later(met, 901)      # the adapter is asked two seconds later
            return answer
        self.now = desktop_adapter.later(met, 899)
        with patch.object(access_outcomes, "unread_wall", then_time_passes):
            self.cycle()
        self.assert_one_session_and_its_wait()

    def test_a_wall_result_exactly_at_the_reuse_mark_is_one_session(self):
        self.wall_left_unread()
        met = self.now
        self.now = desktop_adapter.later(met, desktop_adapter.READ_REUSE_SECONDS)
        self.cycle()
        self.assertEqual((self.assert_one_session_and_its_wait()["since"], self.states()), (met, ["CONSUMED"]))

    def good_read_beside_a_holder(self):
        """`other` has a finished good read nobody has read; this fixture's first job waits on a sign-in."""
        other = self.second_job()
        self.cycle()
        for _ in range(2):
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            key = request["payload"]["job"]["job_key"]
            result = self.review_for(other) if key == other else stop("AUTHENTICATION_REQUIRED")
            self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        read = self.now
        self.advance(60)
        applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now, only={self.key})
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        return other, read

    def test_a_good_read_past_the_reuse_mark_on_the_live_clock_is_held_not_read_again(self):
        # Before this module the adapter (16 minutes on its clock) read the posting afresh here. The site
        # is held, so the job waits instead: no fresh session, where the stale cycle time spent one.
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            other, read = self.good_read_beside_a_holder()
            self.skewed_cycle(desktop_adapter.later(read, 14 * 60), desktop_adapter.later(read, 16 * 60))
        self.assertEqual((self.waits(other)[0]["source"], len(self.requests(other))), ("host:" + self.key, 1))

    def test_a_good_read_inside_the_reuse_mark_on_the_live_clock_is_used(self):
        # As before this module: the adapter hands the read back, the job goes on, no second read.
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            other, read = self.good_read_beside_a_holder()
            self.skewed_cycle(desktop_adapter.later(read, 10 * 60), desktop_adapter.later(read, 14 * 60 + 30))
        self.assertEqual(self.waits(other), [])
        self.assertEqual([row[1] for row in self.requests(other)], ["inspect", "submit"])

    def test_a_wait_ends_when_its_job_finished_another_way(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        other = self.second_job()
        with patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
            # Neither job is finished: both waits stand, cycle after cycle.
            self.advance(3600)
            self.cycle()
            self.assertEqual(([wait["state"] for wait in self.waits()], self.waits(other)[0]["source"]), (["WAITING"], "host:" + self.key))
            self.assertEqual(len(access_outcomes.waiting(self.database)), 2)
            with self.database:   # what conversion.py writes for a confirmed interview: no `hold`, no `settle`
                self.database.execute("UPDATE application_jobs SET state='DUPLICATE',blocker='EXISTING_INTERVIEW',next_at=NULL "
                                      "WHERE job_key=?", (self.key,))
            # At once, before any sweep: no card for the finished job, and it holds its site no longer.
            self.assertEqual([card["job_key"] for card in access_outcomes.waiting(self.database)], [other])
            self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now, other))
            self.cycle()
            wait = self.waits()[0]
            self.assertEqual((wait["state"], wait["outcome"]), ("RESOLVED", access_outcomes.JOB_FINISHED))
            # The job held behind it is released as when a holder ends, and gets its session.
            self.assertEqual(self.waits(other)[0]["outcome"], access_outcomes.HOST_RELEASED)
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
            self.assertEqual(request["payload"]["job"]["job_key"], other)
            desktop_adapter.complete(self.database, dict(request, result=stop("FORM_NOT_FOUND")), self.now)
            for _ in range(3):
                self.advance(4 * 86400)
                self.cycle()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("DUPLICATE", "EXISTING_INTERVIEW"))
        self.assertEqual((self.events(access_outcomes.CLOSED_EVENT), [wait["outcome"] for wait in self.waits()]),
                         ([], [access_outcomes.JOB_FINISHED]))
        with self.database:
            self.assertFalse(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        self.assertEqual(self.row()["state"], "DUPLICATE")
        self.assertEqual([card["job_key"] for card in access_outcomes.waiting(self.database) if card["job_key"] == self.key], [])

    def test_every_final_state_ends_the_wait_and_no_other_does(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        for state in sorted(applications.FINAL) + ["QUEUED", "RETRY_WAIT", "WAITING_BROWSER", "BLOCKED"]:
            with self.database:
                self.database.execute("UPDATE application_access_waits SET state='WAITING',outcome=NULL,ended_at=NULL")
                self.database.execute("UPDATE application_jobs SET state=? WHERE job_key=?", (state, self.key))
                access_outcomes.expire(self.database, self.now)
            final = state in applications.FINAL
            self.assertEqual((self.waits()[0]["state"], self.waits()[0]["outcome"]),
                             ("RESOLVED", access_outcomes.JOB_FINISHED) if final else ("WAITING", None), state)
            self.assertEqual(len(access_outcomes.waiting(self.database)), 0 if final else 1, state)

    # --- Older database ----------------------------------------------------------------------------

    def test_an_older_database_without_the_table_reads_as_no_waits(self):
        self.database.execute("DROP TABLE application_access_waits")
        self.database.commit()
        self.assertIsNone(access_outcomes.open_wait(self.database, self.key))
        self.assertEqual(access_outcomes.waiting(self.database), [])
        with self.database:
            self.assertEqual(access_outcomes.expire(self.database, self.now), [])
            self.assertFalse(access_outcomes.wait_on_host(self.database, dict(self.row()), self.now))
        applications.initialize(self.database)
        self.assertTrue(access_outcomes.present(self.database))
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
