"""E15.T6 stage 1 (owner, 2026-11-12): the one setting "When a site needs an account", what it does
to a walled job today, and the wall description a session reports. Sidedoor cannot sign in or create
an account yet (`access_outcomes.resolver_available` is False), so every account wall still ends as
a wait or a close with its reason."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import access_outcomes
import application_browser
import applications
import desktop_adapter
import paths
import setup_steps
import test_access_outcomes_e15_t1 as t1
import test_evidence_binding_and_hold_order as binding
import test_settings_page as settings_tests

PASSKEY_WORDS = "This site signs in with a passkey. Apply yourself if you want this job."
WALL = {"sign_in_offered": True, "sign_up_offered": True, "apply_without_account_offered": False,
        "sso_providers": ["google", "linkedin"], "account_exists_signal": False, "second_step": "EMAIL_CODE",
        "signed_in_as": None, "profile_required": None}   # profile_required: the site's own profile first (2026-11-14)
CODE = re.compile(r"\b[A-Z]{2,}_[A-Z0-9_]+\b")


def stop(code, wall=None, origin=None):
    result = {"blocked": dict({"code": code, "next_action": "The user does this step"}, **({"wall": wall} if wall is not None else {}))}
    return dict(result, observed_origin=origin) if origin else result


def page(url, body="Sign in"):
    return "### Page\n- Page URL: " + url + "\n### Snapshot\n" + body


class ResolverTests(unittest.TestCase):
    setUp = t1.AccessOutcomeTests.setUp
    connect = t1.AccessOutcomeTests.connect
    write_config = t1.AccessOutcomeTests.write_config
    review = t1.AccessOutcomeTests.review
    job = t1.AccessOutcomeTests.job
    cycle = t1.AccessOutcomeTests.cycle
    advance = t1.AccessOutcomeTests.advance
    row = t1.AccessOutcomeTests.row
    requests = t1.AccessOutcomeTests.requests
    waits = t1.AccessOutcomeTests.waits
    events = t1.AccessOutcomeTests.events
    view = t1.AccessOutcomeTests.view
    notices = t1.AccessOutcomeTests.notices
    second_job = t1.AccessOutcomeTests.second_job
    assert_waiting = t1.AccessOutcomeTests.assert_waiting

    def choose(self, value):
        with self.database:
            return access_outcomes.set_account_setting(self.database, value, self.now)

    def blocked_session(self, code, key=None, wall=None, origin=None):
        """One cycle claims the job's inspect; the session stops on `code`; the next cycle records it."""
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now)
        self.assertIsNotNone(request, "no session was started")
        if key:
            self.assertEqual(request["payload"]["job"]["job_key"], key)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=stop(code, wall, origin)), self.now)["accepted"])
        self.advance(60)
        self.cycle()

    def no_session(self):
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now))

    def one_job_at_a_time(self):
        patcher = patch.object(applications, "same_posting_duplicate", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- The setting's one store --------------------------------------------------------------------

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_never_chosen_reads_none_and_applies_as_ask(self):
        self.assertIsNone(access_outcomes.account_setting(self.database))
        self.assertEqual(access_outcomes.effective_setting(self.database), access_outcomes.ASK)
        self.assertFalse(access_outcomes.resolver_available())

    def test_the_three_values_are_saved_and_an_unknown_one_is_refused(self):
        for value in ("SIGN_IN", "ASK", "SKIP"):
            self.choose(value)
            self.assertEqual(access_outcomes.account_setting(self.database), value)
        with self.assertRaises(ValueError):
            self.choose("Ask me each time")
        self.assertEqual(access_outcomes.account_setting(self.database), "SKIP")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_account_setting").fetchone()[0], 1)

    # --- Ask, and not chosen ------------------------------------------------------------------------

    def test_not_chosen_a_sign_in_wall_is_one_wait_and_the_site_is_held_then_released_together(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual(wait["source"], "session")
        second, third = self.second_job(), self.second_job("789")
        for hours in (0, 1, 30):
            self.advance(hours * 3600)
            self.cycle()
            self.no_session()
        # One blocked session for the whole site; the others wait behind it with no session of their own.
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 1)
        for key in (second, third):
            held = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED", key)
            self.assertEqual((held["source"], held["deadline"]), ("host:" + self.key, wait["deadline"]))
        with self.database:
            released = access_outcomes.resume(self.database, self.key, self.now)
        self.assertEqual(sorted(released), sorted([self.key, second, third]))
        self.assertEqual({self.row(key)["state"] for key in (self.key, second, third)}, {"RETRY_WAIT"})
        self.assertEqual(access_outcomes.waiting(self.database), [])

    def test_ask_me_each_time_waits_with_true_words(self):
        self.choose("ASK")
        self.blocked_session("MFA_REQUIRED")
        self.assert_waiting("ASK_EACH_TIME", "MFA_REQUIRED")   # the wall's kind stays the session's code
        card = access_outcomes.waiting(self.database)[0]
        self.assertEqual(card["words"], "This site needs an account.")
        self.assertIn(card["words"] + " Sidedoor waits 7 days, then closes this application.", self.notices()[0])

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_for_me_behaves_as_ask_until_sidedoor_can_do_it(self):
        self.assertEqual(self.choose("SIGN_IN"), [])
        self.assertEqual(access_outcomes.account_setting(self.database), "SIGN_IN")   # kept as chosen
        self.assertEqual(access_outcomes.effective_setting(self.database), "ASK")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertEqual(len(self.requests()), 1)

    def test_a_sibling_under_another_account_reason_is_released_with_the_sign_in(self):
        # A wait written before the setting existed (SIGN_IN) beside one written now on the same site.
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.database.execute("UPDATE application_access_waits SET reason='SIGN_IN'")
        self.database.commit()
        other = self.second_job()
        with self.database:
            access_outcomes.record(self.database, dict(self.row(other)), "AUTHENTICATION_REQUIRED", self.now)
        self.assertEqual([wait["reason"] for wait in self.waits(other)], ["ASK_EACH_TIME"])
        with self.database:
            released = access_outcomes.resume(self.database, other, self.now)
        self.assertEqual(sorted(released), sorted([self.key, other]))
        # Nothing is left holding the site against the job that was just released.
        self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))

    # --- Skip -----------------------------------------------------------------------------------------

    def test_skip_closes_an_account_wall_and_try_again_reopens_it_once(self):
        self.choose("SKIP")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_SKIPPED_BY_SETTING"))
        self.assertEqual([(wait["state"], wait["outcome"], wait["reason"], wait["source"]) for wait in self.waits()],
                         [("CLOSED", "SKIPPED", "SIGN_IN", "session")])
        self.assertEqual(access_outcomes.waiting(self.database), [])
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT)[0]["why"], "SKIPPED")
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
            again = access_outcomes.try_again(self.database, self.key, self.now)
        self.assertEqual((again["reopened"], again["why"]), (False, "NOT_CLOSED"))
        self.assertEqual((self.row()["state"], len(self.events("APPLICATION_REOPENED"))), ("QUEUED", 1))
        # The reopened job gets a session of its own; the same wall closes it again, with Try again.
        self.blocked_session("AUTHENTICATION_REQUIRED", self.key)
        self.assertEqual((self.row()["state"], len(self.requests())), ("ABANDONED", 2))
        self.assertEqual([wait["outcome"] for wait in self.waits()], ["SKIPPED", "SKIPPED"])

    def test_try_again_refuses_a_skipped_job_whose_submission_is_uncertain(self):
        self.choose("SKIP")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) VALUES ('armed','b',?,'submit','{}',"
                              "'CONSUMED',?,?,?,'{}','gone')", (self.key, self.now, self.now, self.now))
        self.database.commit()
        with self.database:
            answer = access_outcomes.try_again(self.database, self.key, self.now)
        self.assertEqual((answer["reopened"], answer["why"]), (False, "SUBMISSION_UNCERTAIN"))
        self.assertEqual((self.row()["state"], self.events("APPLICATION_REOPENED")), ("ABANDONED", []))

    def test_skip_never_closes_a_job_whose_submission_is_uncertain(self):
        self.choose("SKIP")
        with patch.object(applications, "uncertain_submission", return_value=True), self.database:
            self.assertEqual(access_outcomes.record(self.database, dict(self.row()), "AUTHENTICATION_REQUIRED", self.now),
                             access_outcomes.WAITING)
        self.assertEqual((self.row()["state"], [wait["state"] for wait in self.waits()]), ("BLOCKED", ["WAITING"]))

    def test_skip_never_closes_a_human_check(self):
        self.choose("SKIP")
        self.blocked_session("CAPTCHA_REQUIRED")
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT), [])
        self.assertEqual(access_outcomes.waiting(self.database)[0]["words"], "The site needs a human check.")

    def test_skip_closes_only_a_wall_the_job_met_itself_never_another_job_on_that_host(self):
        # Employer A's job meets an account wall on a shared host and is skipped.
        self.one_job_at_a_time()
        self.choose("SKIP")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assertEqual(self.row()["state"], "ABANDONED")
        # Employer B's job on the same host, next day: its own session, and it proceeds.
        self.advance(86400)
        with self.database:
            other = applications.ingest(self.database, {
                "company": "Another Employer", "role": "Staff Software Engineer",
                "url": "https://jobs.ashbyhq.com/another/role-9", "requisition_id": "role-9"}, self.now)
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now)
        self.assertEqual(request["payload"]["job"]["job_key"], other)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.waits(other), [])
        self.assertNotIn(self.row(other)["state"], ("ABANDONED", "EXHAUSTED"))
        self.assertNotEqual(self.row(other)["blocker"], "ACCESS_SKIPPED_BY_SETTING")

    # --- Changing the setting ------------------------------------------------------------------------

    def test_choosing_skip_closes_open_account_waits_and_frees_the_jobs_held_behind_them(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")                      # met the wall itself
        held = self.second_job()
        self.cycle()                                                         # held behind it, no session
        self.assertEqual(self.waits(held)[0]["source"], "host:" + self.key)
        check, passkey, unsure = self.second_job("789"), self.second_job("790"), self.second_job("791")
        with self.database:
            access_outcomes.record(self.database, dict(self.row(check)), "CAPTCHA_REQUIRED", self.now)
            access_outcomes.record(self.database, dict(self.row(passkey)), "AUTHENTICATION_REQUIRED", self.now,
                                   result=stop("AUTHENTICATION_REQUIRED", dict(WALL, second_step="PASSKEY")))
        uncertain = applications.uncertain_submission
        with patch.object(applications, "uncertain_submission", side_effect=lambda database, key: key == unsure or uncertain(database, key)):
            with self.database:
                access_outcomes.record(self.database, dict(self.row(unsure)), "AUTHENTICATION_REQUIRED", self.now)
            # D21 (owner, 2026-11-12): the uncertain job's wait holds no site, so the held job is freed in this save.
            self.assertEqual(self.choose("SKIP"), [held])
            for key in (self.key, passkey):
                self.assertEqual((self.row(key)["state"], self.row(key)["blocker"], self.waits(key)[-1]["outcome"]),
                                 ("ABANDONED", "ACCESS_SKIPPED_BY_SETTING", "SKIPPED"))
                self.assertEqual(self.view(key)["closed"], "Closed by your setting.")
            # A human check, and a job whose submission is uncertain, keep waiting.
            self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED", check)
            self.assertEqual((self.row(unsure)["state"], self.waits(unsure)[-1]["state"]), ("BLOCKED", "WAITING"))
            self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))
        # The held job is released for its own session; it was never closed.
        with self.database:
            access_outcomes.abandon(self.database, unsure, self.now)
            self.assertEqual(self.choose("SKIP"), [])
        self.assertEqual((self.row(held)["state"], self.waits(held)[-1]["outcome"]), ("RETRY_WAIT", access_outcomes.HOST_RELEASED))
        self.assertEqual(self.events(access_outcomes.CLOSED_EVENT, held), [])
        # Try again works on a job the save closed; under Skip the same wall closes it again.
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        # The held job's own session: Skip closes it only because it meets the wall itself.
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now)
        self.assertIn(request["payload"]["job"]["job_key"], (held, self.key))

    def test_choosing_skip_with_one_wait_frees_its_held_job_in_the_same_save(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")
        held = self.second_job()
        self.cycle()
        self.assertEqual(self.choose("SKIP"), [held])
        self.assertEqual((self.row()["state"], self.row(held)["state"]), ("ABANDONED", "RETRY_WAIT"))
        self.blocked_session("AUTHENTICATION_REQUIRED", held)
        self.assertEqual((self.row(held)["blocker"], len(self.requests(held))), ("ACCESS_SKIPPED_BY_SETTING", 1))
        self.assertEqual(self.choose("ASK"), [])   # moving away from Skip reopens nothing
        self.assertEqual({self.row()["state"], self.row(held)["state"]}, {"ABANDONED"})

    def test_moving_from_skip_to_ask_reopens_nothing(self):
        self.choose("SKIP")
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assertEqual(self.choose("ASK"), [])
        self.cycle()
        self.no_session()
        self.assertEqual((self.row()["state"], self.events("APPLICATION_REOPENED")), ("ABANDONED", []))

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_choosing_for_me_releases_nothing_while_sidedoor_cannot_do_it(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        self.assertEqual(self.choose("SIGN_IN"), [])
        with self.database:
            self.assertEqual(access_outcomes.release_for_setting(self.database, self.now), [])
        self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")

    def test_choosing_for_me_releases_every_ask_wait_once_sidedoor_can_do_it(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")
        held = self.second_job()
        self.cycle()
        check = self.second_job("789")
        with self.database:   # a human check on another job is no account wall: it stays
            access_outcomes.record(self.database, dict(self.row(check)), "CAPTCHA_REQUIRED", self.now)
        with patch.object(access_outcomes, "resolver_available", return_value=True):
            self.assertEqual(sorted(self.choose("SIGN_IN")), sorted([self.key, held]))
            self.assertEqual(self.choose("SIGN_IN"), [])   # saving the same choice again changes nothing
        for key in (self.key, held):
            self.assertEqual((self.row(key)["state"], self.row(key)["blocker"]), ("RETRY_WAIT", None))
            self.assertEqual(self.waits(key)[-1]["outcome"], access_outcomes.SETTING_RELEASED)
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED", check)
        # One attempt: the released job's next session is its own.
        self.cycle()
        self.assertIsNotNone(desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now))

    # --- The wall description ------------------------------------------------------------------------

    def test_a_valid_description_is_stored_with_the_wait_and_shown_to_the_card(self):
        self.blocked_session("MFA_REQUIRED", wall=WALL, origin="https://accounts.site.example")
        wait = self.assert_waiting("ASK_EACH_TIME", "MFA_REQUIRED")
        expected = dict(WALL, observed_origin="https://accounts.site.example")
        self.assertEqual(access_outcomes.description(wait), expected)
        self.assertEqual(access_outcomes.waiting(self.database)[0]["description"], expected)
        self.assertEqual(expected["second_step"], "EMAIL_CODE")   # kept with the wait, the kind stays the code's

    def test_a_malformed_description_is_cleaned_never_a_failure(self):
        wall = {"sign_in_offered": "yes", "sign_up_offered": 1, "apply_without_account_offered": None,
                "sso_providers": ["google", "facebook", 7, ["apple"], "apple", "google"], "account_exists_signal": True,
                "second_step": "FACE", "signed_in_as": {"email": "a@b.example"}, "observed_origin": "https://model.example",
                "password": "never kept"}
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=wall)
        stored = access_outcomes.description(self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED"))
        self.assertEqual(stored, {"sign_in_offered": None, "sign_up_offered": None, "apply_without_account_offered": None,
                                  "sso_providers": ["google", "apple"], "account_exists_signal": True,
                                  "second_step": "UNKNOWN", "signed_in_as": None, "observed_origin": None,
                                  "profile_required": None})
        for wrong in ("text", ["list"], 5):
            self.assertIsNone(access_outcomes.clean_description(wrong))
        self.assertEqual(access_outcomes.clean_description({"sso_providers": "google"})["sso_providers"], None)
        self.assertEqual(access_outcomes.clean_description({"signed_in_as": " alex@example.com "})["signed_in_as"], "alex@example.com")

    def test_a_missing_description_is_fine(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertIsNone(wait["description"])
        self.assertIsNone(access_outcomes.waiting(self.database)[0]["description"])

    def test_a_human_check_keeps_no_account_description(self):
        self.blocked_session("CAPTCHA_REQUIRED", wall=WALL)
        self.assertIsNone(self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")["description"])

    def normalized(self, result, page="https://Accounts.Site.example:8443/login?next=/apply&token=abc"):
        request = {"request_id": "synthetic", "binding": "synthetic-binding", "max_tool_calls": 40,
                   "payload": {"operation": "inspect", "job": {"job_key": "synthetic company:role-123"}}, "context": {}}
        raw = {"request_id": "synthetic", "binding": "synthetic-binding", "result_json": json.dumps(result), "evidence": []}
        answers = page if isinstance(page, list) else [("browser_snapshot", globals()["page"](page) if page else "Sign in")]
        trace = "".join(json.dumps(binding.event("read%d" % index, text, tool=tool)) + "\n"
                        for index, (tool, text) in enumerate(answers)).encode()
        return application_browser.normalize(request, raw, trace, binding.OBSERVED_AT, Path("synthetic-trace.jsonl"))

    def test_the_observed_origin_reads_only_the_page_header_of_navigation_and_snapshot_answers(self):
        real, other = "https://login.employer.example/signin", "https://trusted.example/x"
        stopped = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in", "wall": WALL}}
        for answers in (
                # a line placed before the header, or after it, in the same answer
                [("browser_snapshot", "### New console messages\n- Page URL: " + other + "\n" + page(real))],
                [("browser_snapshot", page(real, "- text: a\n- Page URL: " + other + "\n### Page\n- Page URL: " + other))],
                # output of a tool that echoes what the model wrote
                [("browser_navigate", page(real)), ("browser_console_messages", "### Result\n" + page(other))],
                [("browser_navigate", page(real)), ("browser_network_requests", page(other))],
                [("browser_navigate", page(real)), ("browser_find", page(other))]):
            self.assertEqual(self.normalized(stopped, answers)["observed_origin"], "https://login.employer.example", answers)
        # It is an observation, not a proof: the session's last navigation decides it.
        moved = self.normalized(stopped, [("browser_navigate", page(real)), ("browser_navigate", page(other))])
        self.assertEqual(moved["observed_origin"], "https://trusted.example")
        self.assertIn("NOT trustworthy for any security decision", access_outcomes.description_of.__doc__)

    def test_the_observed_origin_is_the_page_the_browser_showed_never_the_models(self):
        forged = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "Sign in", "observed_origin": "https://model.example",
                              "wall": dict(WALL, observed_origin="https://model.example", second_step="TELEPATHY")},
                  "observed_origin": "https://model.example"}
        result = self.normalized(forged)
        self.assertEqual(result["observed_origin"], "https://accounts.site.example:8443")   # origin only: no path, no query
        self.assertNotIn("model.example", json.dumps(result))
        self.assertEqual(result["blocked"]["wall"], dict(WALL, second_step="UNKNOWN"))
        self.assertEqual(access_outcomes.description_of(result)["observed_origin"], "https://accounts.site.example:8443")
        # No page address in the session's own tool answers: no origin at all (the wait still has its host).
        result = self.normalized(forged, page=None)
        self.assertNotIn("observed_origin", result)
        self.assertNotIn("model.example", json.dumps(result))
        self.assertIsNone(access_outcomes.description_of(result)["observed_origin"])
        # A stop that is not an account wall carries no origin, and the model's is still removed.
        other = self.normalized({"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "x"}, "observed_origin": "https://model.example"})
        self.assertNotIn("observed_origin", other)
        # An older session's result, with no description, normalizes as before.
        plain = self.normalized({"blocked": {"code": "MFA_REQUIRED", "next_action": "Enter the code"}})
        self.assertEqual(plain["blocked"], {"code": "MFA_REQUIRED", "next_action": "Enter the code"})

    # --- Passkey (R8 b) and the guest path (R10 a) ---------------------------------------------------

    def test_a_passkey_only_site_waits_with_the_owners_words(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=dict(WALL, second_step="PASSKEY"))
        self.assert_waiting("PASSKEY", "AUTHENTICATION_REQUIRED")
        self.assertEqual(access_outcomes.WAIT_REASONS["PASSKEY"], PASSKEY_WORDS)
        self.assertEqual(access_outcomes.waiting(self.database)[0]["words"], PASSKEY_WORDS)
        # The owner's words alone in the notice and the next action; the 7 days still apply.
        self.assertIn("Next: " + PASSKEY_WORDS + " Deadline: " + self.waits()[0]["deadline"], self.notices()[0])
        self.assertNotIn("Sidedoor waits", self.notices()[0] + self.row()["next_action"])
        self.assertEqual(self.row()["next_action"], PASSKEY_WORDS)
        # Applications (join with the cards, E15.T2): the owner's words, then when the application closes.
        todo = self.view()["todo"]
        self.assertTrue(todo.startswith(PASSKEY_WORDS + " Closes on ") and todo.endswith(" unless you abandon it first."), todo)
        self.assertNotIn("Will try again automatically", json.dumps(self.view(), default=str))
        # A passkey wait holds nothing: the next job on that site gets its own session and its own card.
        self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))
        other = self.second_job()
        self.blocked_session("AUTHENTICATION_REQUIRED", other, wall=dict(WALL, second_step="PASSKEY"))
        self.assertEqual((self.assert_waiting("PASSKEY", "AUTHENTICATION_REQUIRED", other)["source"], len(self.requests(other))),
                         ("session", 1))
        self.assertEqual(len(access_outcomes.waiting(self.database)), 2)
        self.now = self.waits()[0]["deadline"]
        self.cycle()
        self.assertEqual(self.row()["blocker"], "ACCESS_WAITED_7_DAYS")

    def test_a_sign_in_on_the_site_releases_its_passkey_wait_for_one_more_attempt(self):
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")                                        # A: a sign-in wait
        passkey, check = self.second_job(), self.second_job("789")
        with self.database:
            access_outcomes.record(self.database, dict(self.row(passkey)), "AUTHENTICATION_REQUIRED", self.now,
                                   result=stop("AUTHENTICATION_REQUIRED", dict(WALL, second_step="PASSKEY")))
            access_outcomes.record(self.database, dict(self.row(check)), "CAPTCHA_REQUIRED", self.now)
            released = access_outcomes.resume(self.database, self.key, self.now)
        self.assertEqual(sorted(released), sorted([self.key, passkey]))
        self.assertEqual({self.row(key)["state"] for key in (self.key, passkey)}, {"RETRY_WAIT"})
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED", check)   # never released by a sign-in
        # Each released job gets one session; the passkey prompt again is a fresh passkey wait and card.
        results = {self.key: self.review(), passkey: stop("AUTHENTICATION_REQUIRED", dict(WALL, second_step="PASSKEY"))}
        seen = []
        for _ in range(4):
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now)
            if request is None or request["payload"]["operation"] != "inspect":
                continue
            key = request["payload"]["job"]["job_key"]
            seen.append(key)
            self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=results[key]), self.now)["accepted"])
            self.advance(60)
        self.cycle()
        self.assertEqual(sorted(seen), sorted([self.key, passkey]))   # one session each
        again = self.assert_waiting("PASSKEY", "AUTHENTICATION_REQUIRED", passkey)
        self.assertEqual([wait["outcome"] for wait in self.waits(passkey)], ["released", None])
        self.assertEqual(again["source"], "session")
        card = [item for item in access_outcomes.waiting(self.database) if item["job_key"] == passkey][0]
        self.assertEqual((card["words"], self.row(passkey)["next_action"]), (PASSKEY_WORDS, PASSKEY_WORDS))
        # A passkey wait still holds no other job on the site.
        self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))

    def test_a_sign_in_on_the_site_never_releases_a_passkey_sibling_whose_submission_is_uncertain(self):
        """The join of E15.T2's guard in `_release` with this branch's wider `resume`: the sign-in releases
        every account wait on the site, a passkey wait included, and never a job that may have reached
        the employer (an armed submit request on file)."""
        self.one_job_at_a_time()
        self.blocked_session("AUTHENTICATION_REQUIRED")
        passkey, other = self.second_job(), self.second_job("789")
        with self.database:
            for key in (passkey, other):
                access_outcomes.record(self.database, dict(self.row(key)), "AUTHENTICATION_REQUIRED", self.now,
                                       result=stop("AUTHENTICATION_REQUIRED", dict(WALL, second_step="PASSKEY")))
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) VALUES ('armed','b',?,'submit','{}',"
                              "'CONSUMED',?,?,?,'{}','gone')", (passkey, self.now, self.now, self.now))
        self.database.commit()
        self.assertTrue(applications.uncertain_submission(self.database, passkey))
        before = (dict(self.row(passkey)), self.waits(passkey))
        with self.database:
            released = access_outcomes.resume(self.database, self.key, self.now)
        self.assertEqual(sorted(released), sorted([self.key, other]))
        self.assertEqual((dict(self.row(passkey)), self.waits(passkey)), before)   # its wait and its hold, untouched
        self.assert_waiting("PASSKEY", "AUTHENTICATION_REQUIRED", passkey)
        # Asked for by its own key, it is still never released.
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, passkey, self.now), [])
        self.assertEqual((dict(self.row(passkey)), self.waits(passkey)), before)

    def test_skip_closes_a_passkey_wall_too(self):
        self.choose("SKIP")
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=dict(WALL, second_step="PASSKEY"))
        self.assertEqual((self.row()["state"], self.waits()[0]["reason"], self.waits()[0]["outcome"]), ("ABANDONED", "PASSKEY", "SKIPPED"))

    def test_a_stop_that_reports_a_guest_path_is_an_ordinary_account_wall(self):
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=dict(WALL, apply_without_account_offered=True))
        wait = self.assert_waiting("ASK_EACH_TIME", "AUTHENTICATION_REQUIRED")
        self.assertIs(access_outcomes.description(wait)["apply_without_account_offered"], True)

    def test_the_session_is_told_to_take_the_guest_path_and_still_never_signs_in(self):
        prompt = " ".join(application_browser.PROMPT.split())
        self.assertIn("a way to apply without an account (apply as a guest, continue without signing in), that is not such a stop",
                      prompt)
        self.assertIn("For authentication or MFA return blocked with code AUTHENTICATION_REQUIRED or MFA_REQUIRED and next_action.",
                      prompt)
        for field in access_outcomes.DESCRIPTION_FLAGS + ("sso_providers", "second_step", "signed_in_as") + access_outcomes.SECOND_STEPS:
            self.assertIn(field, prompt)
        self.assertNotIn("observed_origin", prompt)   # never the model's to report
        self.assertIn("Never enter a password, sign in, add or create an account, complete MFA", prompt)

    def test_a_session_that_got_through_is_an_ordinary_read(self):
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "resolver-fixture", self.now)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.waits(), [])
        self.assertNotIn(self.row()["blocker"], access_outcomes.WALL_REASONS)

    # --- Old databases ---------------------------------------------------------------------------------

    def test_a_database_from_before_this_task_opens_and_its_waits_behave_as_before(self):
        self.blocked_session("AUTHENTICATION_REQUIRED")
        # The table as E15.T1 made it: no description column, no setting table, a SIGN_IN wait.
        self.database.executescript("""
            ALTER TABLE application_access_waits RENAME TO newer;
            CREATE TABLE application_access_waits (
                id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, host TEXT, wall TEXT NOT NULL,
                reason TEXT NOT NULL, source TEXT NOT NULL, since TEXT NOT NULL, deadline TEXT NOT NULL,
                state TEXT NOT NULL, ended_at TEXT, outcome TEXT, updated_at TEXT NOT NULL);
            INSERT INTO application_access_waits SELECT id,job_key,host,wall,'SIGN_IN',source,since,deadline,state,
                ended_at,outcome,updated_at FROM newer;
            DROP TABLE newer; DROP TABLE application_account_setting;""")
        self.assertIsNone(access_outcomes.account_setting(self.database))
        self.assertEqual(access_outcomes.waiting(self.database)[0]["description"], None)
        self.one_job_at_a_time()
        other = self.second_job()
        applications.initialize(self.database)
        applications.initialize(self.database)   # twice: the migration is safe to repeat
        self.assertIn("description", {row[1] for row in self.database.execute("PRAGMA table_info(application_access_waits)")})
        card = access_outcomes.waiting(self.database)[0]
        self.assertEqual((card["reason"], card["words"], card["description"]), ("SIGN_IN", "The site needs you to sign in.", None))
        self.cycle()   # the old wait still holds its site
        self.no_session()
        self.assertEqual(self.waits(other)[0]["source"], "host:" + self.key)
        with self.database:
            self.assertEqual(sorted(access_outcomes.resume(self.database, self.key, self.now)), sorted([self.key, other]))
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=WALL)
        self.assertEqual(len([wait for wait in self.waits() + self.waits(other) if wait["description"]]), 1)

    # --- Words -----------------------------------------------------------------------------------------

    def test_no_words_show_a_code_an_em_dash_or_the_word_automation(self):
        shown = list(access_outcomes.WAIT_REASONS.values()) + list(access_outcomes.ACCOUNT_SETTING_LABELS.values()) + [
            access_outcomes.ACCOUNT_SETTING_TITLE, access_outcomes.UNREADABLE_WORDS, access_outcomes.NOT_CHOSEN_WORDS,
            access_outcomes.SETTING_RELEASED_WORDS]
        self.blocked_session("AUTHENTICATION_REQUIRED", wall=dict(WALL, second_step="PASSKEY"))
        view = self.view()
        shown += [str(view.get(part) or "") for part in ("closed", "reason", "todo", "problem", "status")]
        self.choose("SKIP")
        self.one_job_at_a_time()
        other = self.second_job()
        with self.database:
            access_outcomes.record(self.database, dict(self.row(other)), "AUTHENTICATION_REQUIRED", self.now)
        view = self.view(other)
        self.assertEqual(view["closed"], "Closed by your setting.")
        self.assertEqual(view["todo"], "If you want this job, open the posting and apply yourself.")
        self.assertNotIn("can't do", json.dumps(view, default=str))
        with self.assertLogs("sidedoor", level="INFO") as logged, self.database:
            access_outcomes.record(self.database, dict(self.row(self.second_job("790"))), "AUTHENTICATION_REQUIRED", self.now)
        self.assertTrue(any(": Closed by your setting." in line
                            for line in logged.output), logged.output)
        self.assertNotIn("gave up", " ".join(logged.output))
        shown += [str(view.get(part) or "") for part in ("closed", "reason", "todo", "problem", "status")]
        shown.append(str(self.row(other)["next_action"]))
        for words in shown:
            self.assertIsNone(CODE.search(words), words)
            self.assertNotIn("—", words)
            self.assertNotIn("automation", words.lower())


@unittest.skipUnless(settings_tests.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class AccountSettingPageTests(unittest.TestCase):
    """Through the app's real entry point (`dashboard.create_app`), as the installed app registers it."""
    setUp = settings_tests.SettingsPageTest.setUp
    client_for = settings_tests.SettingsPageTest.client_for

    def section(self, page=None):
        page = page if page is not None else self.client.get("/settings").text
        start = page.index('<section class="card settings-section" id="mode"')
        return page[start:page.index("</section>", start)]

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_the_renamed_section_holds_exactly_one_new_control_not_yet_chosen(self):
        page = self.client.get("/settings").text
        section = self.section(page)
        self.assertIn("<h2>Applying</h2>", section)
        self.assertEqual((page.count("<h2>Mode</h2>"), page.count("<h2>Applications</h2>")), (0, 0))
        self.assertIn('<a href="#mode">Applying</a>', page)   # the old anchor still leads here
        self.assertIn("<h3>Mode</h3>", section)
        self.assertIn('<h3 id="account-setting">When a site needs an account</h3>', section)
        # Two choices while Sidedoor cannot sign in itself; the third is not shown or promised.
        self.assertEqual(re.findall(r'name="account" value="(\w+)"', page), ["ASK", "SKIP"])
        for label in ("Ask me each time", "Skip jobs that need an account"):
            self.assertEqual(section.count("<strong>%s</strong>" % label), 1)
        for gone in ("Sign in and create accounts for me", "arrives with an update", "do this yet"):
            self.assertNotIn(gone, page)
        # Not chosen: no option is ticked, and the page says what applies now.
        self.assertEqual(re.findall(r'name="account" value="\w+" checked', section), [])
        self.assertIn("Not chosen yet. Until you choose, Sidedoor asks you each time.", section)
        # Guard rail (E15.T10): Settings gains exactly one control, and Setup gains no step (since
        # E15.T10 the same question is inside step 5: tests/test_account_banner_e15_t10.py).
        self.assertEqual(sorted(set(re.findall(r'<input type="radio" name="(\w+)"', section))), ["account", "mode"])
        self.assertEqual(page.count('action="/settings/account"'), 1)
        self.assertEqual(len(setup_steps.STEPS), 10)
        self.assertEqual([key for _, key, _ in setup_steps.STEPS],
                         ["license", "openrouter", "resume", "profile", "mode", "codex", "linkedin", "browser", "login", "start"])

    def saved(self):
        database = sqlite3.connect(paths.database(self.root))   # as the applications run opens it
        try:
            return access_outcomes.account_setting(database), access_outcomes.effective_setting(database)
        finally:
            database.close()

    def test_saving_writes_the_one_store_the_applications_run_reads(self):
        for value, label in (("SKIP", "Skip jobs that need an account"), ("ASK", "Ask me each time")):
            response = self.client.post("/settings/account", data={"account": value}, headers=settings_tests.HTMX)
            self.assertEqual(response.status_code, 200)
            section = self.section(response.text)
            self.assertIn('name="account" value="%s" checked' % value, section)
            self.assertEqual(len(re.findall(r'name="account" value="\w+" checked', section)), 1)
            self.assertIn("Saved: %s." % label, section)
            self.assertNotIn("Not chosen yet", section)
            self.assertEqual(access_outcomes.install_account_setting(self.root), value)
            self.assertEqual(self.saved(), (value, value))
        self.assertIn('name="mode" value="automatic" checked', self.section())   # Mode beside it is unchanged

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_the_third_choice_is_not_accepted_or_shown_until_sidedoor_can_do_it(self):
        response = self.client.post("/settings/account", data={"account": "SIGN_IN"}, headers=settings_tests.HTMX)
        self.assertIn("Choose one of the options shown.", response.text)
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        # A stored "for me" (a downgrade) applies and shows as Ask me each time.
        access_outcomes.set_install_account_setting("SIGN_IN", root=self.root)
        section = self.section()
        self.assertEqual(re.findall(r'name="account" value="(\w+)" checked', section), ["ASK"])
        self.assertNotIn("Not chosen yet", section)
        self.assertEqual(self.saved(), ("SIGN_IN", "ASK"))
        # Once Sidedoor can: the third choice is rendered, selectable and saved.
        with patch.object(access_outcomes, "resolver_available", return_value=True):
            section = self.section()
            self.assertEqual(re.findall(r'name="account" value="(\w+)"', section), ["SIGN_IN", "ASK", "SKIP"])
            self.assertIn('value="SIGN_IN" checked', section)
            self.client.post("/settings/account", data={"account": "ASK"}, headers=settings_tests.HTMX)
            response = self.client.post("/settings/account", data={"account": "SIGN_IN"}, headers=settings_tests.HTMX)
            self.assertIn("Saved: Sign in and create accounts for me.", self.section(response.text))
            self.assertEqual(self.saved(), ("SIGN_IN", "SIGN_IN"))

    def test_an_unknown_value_saves_nothing(self):
        for data in ({"account": "EVERYTHING"}, {"account": "skip"}, {}):
            response = self.client.post("/settings/account", data=data, headers=settings_tests.HTMX)
            self.assertIn("Choose one of the options shown.", response.text)
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        self.assertIn("Not chosen yet", self.section())

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_a_setting_that_cannot_be_read_is_not_shown_as_not_chosen(self):
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=settings_tests.HTMX)
        with patch.object(access_outcomes, "_read_setting", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(access_outcomes.install_account_setting(self.root), access_outcomes.UNREADABLE)
            section = self.section()
        self.assertIn("Sidedoor couldn&#39;t read this setting just now. Reload the page in a moment.", section)
        self.assertNotIn("Not chosen yet", section)
        self.assertEqual(re.findall(r'name="account" value="\w+" checked', section), [])
        self.assertEqual(re.findall(r'name="account" value="(\w+)"', section), ["ASK", "SKIP"])   # the control stays
        # Saving while the database cannot be written is refused with the busy sentence; nothing changes.
        with patch.object(access_outcomes, "set_install_account_setting", side_effect=sqlite3.OperationalError("database is locked")):
            response = self.client.post("/settings/account", data={"account": "ASK"}, headers=settings_tests.HTMX)
        self.assertIn(settings_tests.settings_page.BUSY, response.text)
        self.assertEqual(self.saved()[0], "SKIP")
        # A damaged database is not "busy": the save says the setting could not be read.
        for damaged in (sqlite3.DatabaseError("file is not a database"), sqlite3.DatabaseError("database disk image is malformed"),
                        sqlite3.OperationalError("unable to open database file")):
            with patch.object(access_outcomes, "set_install_account_setting", side_effect=damaged):
                response = self.client.post("/settings/account", data={"account": "ASK"}, headers=settings_tests.HTMX)
            self.assertIn("Sidedoor couldn&#39;t read this setting just now. Reload the page in a moment.", response.text)
            self.assertNotIn(settings_tests.settings_page.BUSY, response.text)
        self.assertEqual(self.saved()[0], "SKIP")
        self.assertIn('value="SKIP" checked', self.section())

    def test_a_plain_form_post_comes_back_to_the_section(self):
        response = self.client.post("/settings/account", data={"account": "ASK"}, headers=settings_tests.ORIGIN)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/settings#mode"))
        self.assertIn('value="ASK" checked', self.section())

    def test_the_settings_page_shows_no_code_em_dash_or_the_word_automation_in_the_section(self):
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=settings_tests.HTMX)
        text = re.sub(r"<[^>]+>", " ", self.section())
        self.assertIsNone(CODE.search(text), text)
        self.assertNotIn("—", text)
        self.assertNotIn("automation", text.lower())


if __name__ == "__main__":
    unittest.main()
