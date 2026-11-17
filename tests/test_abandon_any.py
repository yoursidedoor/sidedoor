"""Abandon on every job that is not finished and not being submitted (owner, 2026-11-16: "there is no
way for me to abandon the Job on applications page").

The button is on the Applications page and on each "Questions for you" card. It is never offered, and a
forged press is refused, for a job being submitted or confirmed, one whose submission may have reached
the employer, and a finished job. A job abandoned while the round is reading or answering it stays closed.

Each test runs the real applications cycle (mock browser, fake answering model) and the real dashboard
through TestClient, and reads the HTML the user sees. The class runs only methods named test_owner_...
(`test_owner_questions.OwnerQuestions.run`).
"""

import html
import json
import re
import unittest
from unittest import mock

import access_cards
import access_outcomes
import access_step
import applications
import applications_page
import approvals_page
import desktop_adapter
import profile_home
import test_applications_e2e as fixtures
import test_dashboard_overview
import test_owner_questions as base   # the class by its module: a from-import would run its tests here too
from test_owner_questions import DEPENDENCIES, GITHUB, ORIGIN, TWITTER, Model

HTMX = dict(ORIGIN, **{"hx-request": "true"})
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
ABANDON_FORM = 'action="/applications/abandon"'
CARD_FORM = 'action="/approvals/questions/abandon"'
OPEN_NOTICES = ("PENDING", "WAITING_TRANSPORT", "WAITING_OWNER")


class AbandonsDuring(Model):
    """The answering AI, with the user's Abandon pressed on the dashboard while its call is running."""

    def __init__(self, press, **options):
        super().__init__(**options)
        self.press, self.said = press, None

    def __call__(self, prompt):
        if self.said is None:
            self.said = self.press()
        return super().__call__(prompt)


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class AbandonAnyJob(base.OwnerQuestions):
    # Only this class's own tests run here (the inherited ones run in their own module).
    def run(self, result=None):
        if self._testMethodName not in vars(type(self)):
            return result
        return super().run(result)

    def setUp(self):
        super().setUp()
        self.policy["answers"].pop("github", None)   # as the owner's install: no GitHub link on record
        profile_home.use(self, self.policy)
        self.dashboard()
        test_dashboard_overview.finished_home(self.home)
        desktop_adapter.initialize(self.database)
        self.database.commit()

    # --- helpers ----------------------------------------------------------------------------------------

    def held_on_two_questions(self):
        key = self.discover(fixtures.role("InsForge (InstaCloud)", "insforge-1"))
        self.form({"insforge (instacloud)": [GITHUB, TWITTER]})
        self.run_cycle(Model("CANNOT_ANSWER"))
        job = self.job(key)
        self.assertEqual(job["state"], "BLOCKED")
        self.assertTrue(job["blocker"].startswith("UNKNOWN_REQUIRED_ANSWERS:"))
        return key

    def put(self, key, state, blocker=None, next_at=None):
        payload = json.dumps({"company": "Acme", "role": "Engineer " + key, "url": "https://jobs.example.com/" + key})
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,next_at,updated_at) "
                                  "VALUES (?,?,?,?,?,?,?,?)", (key, "acme", key, payload, state, blocker, next_at, self.clock.isoformat()))
        return key

    def armed(self, key):
        """An armed submit on record, its attempt unsettled: it may have reached the employer."""
        now = self.clock.isoformat()
        with self.database:
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                                  "expires_at,armed_at,result,attempt_id) VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?,'{}',?)",
                                  ("armed-" + key, key, now, now, now, "gone-" + key))

    def press(self, key, headers=HTMX):
        response = self.client.post("/applications/abandon", data={"job_key": key}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def press_card(self, decision_id, headers=HTMX):
        response = self.client.post("/approvals/questions/abandon", data={"decision_id": decision_id}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def record_of(self, key):
        """Everything a refused press must leave alone: the job, its events, cards, waits and notices."""
        tables = {"application_jobs": "job_key=?", "application_events": "job_key=?", "user_decisions": "job_key=?",
                  "application_blockers": "job_key=?"}
        found = {name: [tuple(row) for row in self.database.execute("SELECT * FROM %s WHERE %s" % (name, where), (key,))]
                 for name, where in tables.items()}
        found["notices"] = [tuple(row) for row in self.database.execute("SELECT event_key,state FROM notification_outbox ORDER BY 1")]
        if access_outcomes.present(self.database):
            found["waits"] = [tuple(row) for row in self.database.execute(
                "SELECT * FROM %s WHERE job_key=?" % access_outcomes.TABLE, (key,))]
        return found

    def events(self, key, kind):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id", (key, kind))]

    def no_raw_code(self, page):
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", page))
        self.assertIsNone(RAW_CODE.search(text), RAW_CODE.findall(text))

    # --- the question card ------------------------------------------------------------------------------

    def test_owner_abandon_on_a_question_card_closes_the_job_and_all_its_cards(self):
        key = self.held_on_two_questions()
        by_field = {decision["field"]: decision for decision in self.pending(key)}
        github, twitter = by_field[applications.question_field(GITHUB)], by_field[applications.question_field(TWITTER)]
        with self.database:   # each card's notice is waiting to go out
            for decision in (github, twitter):
                applications.notify(self.database, decision["event_key"], "A question for you", self.clock.isoformat())
        notices = lambda: dict(self.database.execute(
            "SELECT event_key,state FROM notification_outbox WHERE event_key IN (?,?)", (github["event_key"], twitter["event_key"])))
        self.assertTrue(all(state in OPEN_NOTICES for state in notices().values()) and len(notices()) == 2, notices())

        # What the user sees: Abandon on each card, and on the job's row and page on Applications.
        page = self.page()
        for question in (GITHUB, TWITTER):
            card = self.card(page, question["label"])
            self.assertIn(CARD_FORM, card)
            self.assertIn(">%s</button>" % approvals_page.ABANDON_QUESTION_LABEL, card)
        self.assertIn(ABANDON_FORM, self.page("/applications"))
        self.assertIn(">Abandon</button>", self.page("/applications?job=" + key))

        answer = self.press_card(github["id"])
        self.assertIn(access_cards.ABANDONED, answer)
        # The job's other card leaves the page with it.
        self.assertIn('<article id="%s" hx-swap-oob="delete">' % approvals_page.dom("question", twitter["id"]), answer)
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"], job["next_at"]), ("ABANDONED", "ABANDONED_BY_YOU", None))
        self.assertEqual(self.pending(key), [])
        cards = {row["id"]: (row["state"], row["last_error"]) for row in self.database.execute(
            "SELECT id,state,last_error FROM user_decisions WHERE job_key=?", (key,))}
        self.assertEqual(cards, {github["id"]: ("SUPERSEDED", access_outcomes.ABANDONED_CARD_REASON),
                                 twitter["id"]: ("SUPERSEDED", access_outcomes.ABANDONED_CARD_REASON)})
        self.assertEqual(set(notices().values()), {"CANCELLED"})
        [closed] = self.events(key, access_outcomes.PLAIN_CLOSED_EVENT)
        self.assertEqual((closed["why"], closed["submission_status"], closed["previous_state"]["state"]),
                         ("ABANDONED", "NOT_SUBMITTED", "BLOCKED"))
        self.assertTrue(closed["previous_state"]["blocker"].startswith("UNKNOWN_REQUIRED_ANSWERS:"))

        # Approvals shows neither card; Applications says it in the existing words and keeps Try again.
        page = self.page()
        self.assertNotIn(GITHUB["label"], page)
        self.assertNotIn(TWITTER["label"], page)
        detail = self.page("/applications?job=" + key)
        self.assertIn("You abandoned this application.", detail)
        self.assertIn("Given up", detail)
        self.assertIn(">Try again</button>", detail)
        self.assertNotIn(ABANDON_FORM, detail)
        self.no_raw_code(detail)

        # A press on the other card of a page drawn before: nothing changes, and it says so.
        before = self.record_of(key)
        self.assertIn(access_cards.GONE, self.press_card(twitter["id"]))   # a card no longer open abandons nothing (C3)
        self.assertIn(access_cards.ALREADY_FINISHED, self.press(key))
        self.assertEqual(self.record_of(key), before)

        # The round leaves it closed: no read, nothing submitted.
        reads = len(self.adapter.calls)
        self.run_cycle(Model("CANNOT_ANSWER"), days=1)
        self.assertEqual((len(self.adapter.calls), self.job(key)["state"], self.pending(key)), (reads, "ABANDONED", []))

        # Try again brings it back, as for every job the user abandoned.
        again = html.unescape(self.client.post("/access/try-again", data={"job_key": key}, headers=HTMX).text)
        self.assertIn(access_cards.REOPENED, again)
        self.assertEqual(self.job(key)["state"], "QUEUED")

    def test_owner_abandon_on_a_question_card_without_htmx_shows_the_page(self):
        key = self.held_on_two_questions()
        decision = self.pending(key)[0]
        page = self.press_card(decision["id"], headers=ORIGIN)
        self.assertIn(access_cards.ABANDONED, page)
        self.assertNotIn(GITHUB["label"], page)
        self.assertNotIn(TWITTER["label"], page)
        self.assertEqual(self.job(key)["state"], "ABANDONED")
        # A card id nobody has: nothing is closed.
        self.assertIn(access_cards.GONE, self.press_card("no-such-card"))

    # --- the Applications page --------------------------------------------------------------------------

    def test_owner_abandon_is_offered_for_every_open_job_and_closes_it(self):
        import application_recovery
        later = (self.clock.replace(year=self.clock.year + 1)).isoformat()
        open_jobs = {"queued": ("QUEUED", None, None), "retrying": ("RETRY_WAIT", "APPLICATION_BROWSER_TIMEOUT", later),
                     "waiting-browser": ("WAITING_BROWSER", "DESKTOP_BROWSER_PENDING", later),
                     "answering": ("BLOCKED", "ANSWERING_RETRY:f1", later),
                     "approved": ("BLOCKED", applications.APPLICATION_APPROVED, None),
                     "parked": ("BLOCKED", application_recovery.CLAIMS_EXHAUSTED_BLOCKER, None),
                     "needs-you": ("BLOCKED", application_recovery.NEEDS_YOU_BLOCKER, None)}
        for key, (state, blocker, next_at) in open_jobs.items():
            self.put(key, state, blocker, next_at)
        listing = self.page("/applications")
        self.assertEqual(listing.count(ABANDON_FORM), len(open_jobs))
        for key in open_jobs:
            detail = self.page("/applications?job=" + key)
            self.assertIn(ABANDON_FORM, detail, key)
            self.assertIn(">Abandon</button>", detail, key)
            self.assertIn(access_cards.ABANDONED, self.press(key), key)
            job = self.job(key)
            self.assertEqual((job["state"], job["blocker"]), ("ABANDONED", "ABANDONED_BY_YOU"), key)
            detail = self.page("/applications?job=" + key)
            self.assertIn("You abandoned this application.", detail, key)
            self.assertIn(">Try again</button>", detail, key)
            self.assertNotIn(ABANDON_FORM, detail, key)
            self.no_raw_code(detail)
        self.assertNotIn(ABANDON_FORM, self.page("/applications"))
        # The round reads none of them and submits nothing.
        self.run_cycle(Model("ACCEPT"))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual({self.job(key)["state"] for key in open_jobs}, {"ABANDONED"})

    def test_owner_abandon_is_never_offered_and_a_forged_press_changes_nothing(self):
        refused = {"submitting": ("SUBMITTING", None, access_cards.NOT_ABANDONED),
                   "confirming": ("RECONCILE", "SUBMISSION_OUTCOME_UNKNOWN", access_cards.NOT_ABANDONED),
                   "unknown-outcome": ("BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", access_cards.NOT_ABANDONED),
                   "armed": ("QUEUED", None, access_cards.NOT_ABANDONED),
                   "armed-retrying": ("RETRY_WAIT", "APPLICATION_BROWSER_TIMEOUT", access_cards.NOT_ABANDONED),
                   "submitted": ("SUBMITTED", None, access_cards.ALREADY_FINISHED),
                   "duplicate": ("DUPLICATE", "EXISTING_INTERVIEW", access_cards.ALREADY_FINISHED),
                   "ineligible": ("INELIGIBLE", "RESEARCH_INELIGIBLE", access_cards.ALREADY_FINISHED),
                   "exhausted": ("EXHAUSTED", "RETRIES_EXHAUSTED", access_cards.ALREADY_FINISHED),
                   "abandoned": ("ABANDONED", "APPLICATION_REJECTED_BY_YOU", access_cards.ALREADY_FINISHED)}
        self.assertEqual({state for state, _, _ in refused.values()} & applications_page.FINAL, applications_page.FINAL)
        for key, (state, blocker, _) in refused.items():
            self.put(key, state, blocker)
        self.armed("armed")
        self.armed("armed-retrying")
        self.put("control", "QUEUED")   # the page does show the button where it is offered
        self.assertEqual(self.page("/applications").count(ABANDON_FORM), 1)
        for key, (state, blocker, words) in refused.items():
            self.assertNotIn(ABANDON_FORM, self.page("/applications?job=" + key), key)
            before = self.record_of(key)
            self.assertIn(words, self.press(key), key)
            self.assertIn(words, self.press(key, headers=ORIGIN), key)   # a plain post too
            self.assertEqual(self.record_of(key), before, key)
        self.assertIn(access_cards.GONE, self.press("no-such-job"))
        self.assertIn(access_cards.GONE, self.press(""))
        # The same rule on a question card: a card whose job is being submitted has no Abandon and refuses one.
        key = self.held_on_two_questions()
        decision = self.pending(key)[0]
        self.assertIn(CARD_FORM, self.page())
        self.armed(key)
        self.assertNotIn(CARD_FORM, self.page())
        self.assertNotIn(ABANDON_FORM, self.page("/applications?job=" + key))
        before = self.record_of(key)
        answer = self.press_card(decision["id"])
        self.assertIn(access_cards.NOT_ABANDONED, answer)
        self.assertIn(GITHUB["label"] if decision["field"] == applications.question_field(GITHUB) else TWITTER["label"], answer)
        self.assertEqual(self.record_of(key), before)
        self.assertEqual(len(self.pending(key)), 2)

    def test_owner_abandon_on_a_job_waiting_for_a_sign_in_closes_its_wait_too(self):
        key = self.discover(fixtures.role("Walled Co", "walled-1"))
        now = self.clock.isoformat()
        with self.database:
            self.assertEqual(access_outcomes.record(self.database, dict(self.job(key)), "AUTHENTICATION_REQUIRED", now),
                             access_outcomes.WAITING)
        self.assertIn(ABANDON_FORM, self.page("/applications?job=" + key))
        self.assertIn(access_cards.ABANDONED, self.press(key))
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))
        wait = self.database.execute("SELECT state,outcome FROM %s WHERE job_key=?" % access_outcomes.TABLE, (key,)).fetchone()
        self.assertEqual(tuple(wait), (access_outcomes.CLOSED, "ABANDONED"))
        self.assertEqual(self.events(key, access_outcomes.CLOSED_EVENT)[0]["submission_status"], "NOT_SUBMITTED")
        detail = self.page("/applications?job=" + key)
        self.assertIn("You abandoned this application.", detail)
        self.assertIn(">Try again</button>", detail)
        self.assertIn(access_cards.REOPENED, html.unescape(self.client.post("/access/try-again", data={"job_key": key}, headers=HTMX).text))

    # --- abandoned while the round is working on it -----------------------------------------------------

    def test_owner_a_job_abandoned_while_it_is_being_answered_gets_no_card_and_stays_closed(self):
        key = self.discover(fixtures.role("InsForge (InstaCloud)", "insforge-1"))
        self.form({"insforge (instacloud)": [GITHUB, TWITTER]})
        model = AbandonsDuring(lambda: self.press(key), verdict="CANNOT_ANSWER")
        self.run_cycle(model)
        self.assertIn(access_cards.ABANDONED, model.said)   # the press landed while the answering call ran
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"], job["next_at"]), ("ABANDONED", "ABANDONED_BY_YOU", None))
        self.assertEqual(self.pending(key), [])             # the finished answering step asked nothing
        self.assertNotIn(GITHUB["label"], self.page())
        self.assertEqual(self.submissions(), [])
        reads = len(self.adapter.calls)
        self.run_cycle(Model("CANNOT_ANSWER"), days=1)
        self.assertEqual((len(self.adapter.calls), self.job(key)["state"], self.pending(key)), (reads, "ABANDONED", []))

    def test_owner_a_job_abandoned_while_it_is_being_answered_is_never_submitted(self):
        # The control: the same job, not abandoned, is submitted by this round.
        control = self.discover(fixtures.role("Control Co", "control-1"))
        self.form({"control co": [TWITTER], "answered co": [TWITTER]})
        self.run_cycle(Model(values={TWITTER["semantic"]: "https://x.com/example_user"}))
        self.assertEqual((self.job(control)["state"], len(self.submissions())), ("SUBMITTED", 1))
        key = self.discover(fixtures.role("Answered Co", "answered-1"))
        model = AbandonsDuring(lambda: self.press(key), values={TWITTER["semantic"]: "https://x.com/example_user"})
        self.run_cycle(model)
        self.assertIn(access_cards.ABANDONED, model.said)
        self.assertEqual((self.job(key)["state"], self.job(key)["blocker"]), ("ABANDONED", "ABANDONED_BY_YOU"))
        self.assertEqual(len(self.submissions()), 1)        # only the control's
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts WHERE job_key=?", (key,)).fetchone()[0], 0)
        self.assertIn(access_cards.ALREADY_FINISHED, self.press(key))

    def test_owner_a_job_abandoned_while_it_is_being_read_is_not_made_due_by_a_sign_in_wall(self):
        said = {}

        def walled(result, job):
            if job["employer"] == "gone co":
                said["press"] = self.press(job["job_key"])
            result["blocked"] = {"code": "AUTHENTICATION_REQUIRED", "next_action": "sign in"}
        self.adapter.customize = walled
        with self.database:
            access_outcomes.set_account_setting(self.database, access_outcomes.SIGN_IN_FOR_ME, self.clock.isoformat())
        with mock.patch.object(access_outcomes, "resolver_available", return_value=True):
            # The control: this wall, met by a job nobody abandoned, plans Sidedoor's own step and makes it due.
            control = self.discover(fixtures.role("Stays Co", "stays-1"))
            self.run_cycle(Model("ACCEPT"))
            self.assertEqual((self.job(control)["state"], self.job(control)["next_action"]), ("RETRY_WAIT", access_step.PLANNED_WORDS))
            with self.database:
                self.database.execute("UPDATE application_jobs SET next_at='9999-01-01T00:00:00+00:00' WHERE job_key=?", (control,))
            key = self.discover(fixtures.role("Gone Co", "gone-1"))
            self.run_cycle(Model("ACCEPT"))
        self.assertIn(access_cards.ABANDONED, said["press"])
        job = self.job(key)
        self.assertEqual((job["state"], job["blocker"]), ("ABANDONED", "ABANDONED_BY_YOU"))
        self.assertIsNone(access_outcomes.open_wait(self.database, key))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM %s WHERE job_key=?" % access_step.TABLE, (key,)).fetchone()[0], 0)
        self.assertIn("You abandoned this application.", self.page("/applications?job=" + key))

    def test_owner_a_sign_in_wall_recorded_for_a_job_abandoned_meanwhile_writes_nothing(self):
        """The write under the round's re-check (`applications.process` compares the job with what it read):
        a wall recorded from a row read before the user's Abandon plans no step and makes nothing due."""
        now = self.clock.isoformat()
        wall = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "sign in"}}
        with self.database:
            access_outcomes.set_account_setting(self.database, access_outcomes.SIGN_IN_FOR_ME, now)
        control, key = self.discover(fixtures.role("Stays Co", "stays-1")), self.discover(fixtures.role("Gone Co", "gone-1"))
        read_before = dict(self.job(key))
        self.assertIn(access_cards.ABANDONED, self.press(key))
        with mock.patch.object(access_outcomes, "resolver_available", return_value=True):
            with self.database:   # the control: the same call for an open job plans the step and makes it due
                self.assertEqual(access_outcomes.record(self.database, dict(self.job(control)), "AUTHENTICATION_REQUIRED", now, result=wall),
                                 access_step.PLANNED)
            self.assertEqual(self.job(control)["state"], "RETRY_WAIT")
            before = self.record_of(key)
            with self.database:
                self.assertIsNone(access_outcomes.record(self.database, read_before, "AUTHENTICATION_REQUIRED", now, result=wall))
        self.assertEqual(self.record_of(key), before)
        self.assertEqual((self.job(key)["state"], self.job(key)["blocker"]), ("ABANDONED", "ABANDONED_BY_YOU"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM %s WHERE job_key=?" % access_step.TABLE, (key,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
