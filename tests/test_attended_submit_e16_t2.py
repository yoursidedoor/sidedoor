"""E16.T2 phase 2 (owner, 2026-11-12/13): the card of a human check ON THE FORM starts an attended submit.
The press records a request and releases the wait; the applications job takes that job first and its
submit session is the attended one; the card shows one true line and then the view attached to that
session. Review mode: nothing is asked for before the application is approved (E16.T4).
Offline: no browser, no AI, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_cards  # noqa: E402
import access_outcomes  # noqa: E402
import application_connection  # noqa: E402
import applications  # noqa: E402
import approvals  # noqa: E402
import attended_submit  # noqa: E402
import browser_runtime  # noqa: E402
import desktop_adapter  # noqa: E402
import form_check_hold  # noqa: E402
import live_view  # noqa: E402
import overview_page  # noqa: E402
import scheduler_control  # noqa: E402
import scheduler_loop  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402
import test_applications_job as jobs  # noqa: E402

STOP = {"code": "CAPTCHA_REQUIRED", "next_action": "A person has to do the human check"}
ON_FORM = {"place": "ON_FORM", "after_question": 2, "form_readable": True}


class FakeLive:
    """The dashboard's live view, as `attended_submit.step` uses it."""

    def __init__(self, available=True):
        self.offered, self.is_available = [], available

    def available(self, purpose=None):
        return self.is_available

    def offer_attached(self, job, directory, domain, on_ended=None):
        self.offered.append((job, str(directory), domain))
        return "target-1"


class Case(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "review", "job", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name
    _fixture = setUp

    def setUp(self):
        self._fixture()
        # Sidedoor is running, applications are on, and nothing the product knows is in the way.
        for target, name, value in ((attended_submit, "_running", lambda: True),
                                    (scheduler_loop, "load_config", lambda root=None: {"applications_enabled": True})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    watching = True   # the card is open and asking (the tests' rounds run on the fixture's clock)

    def cycle(self, *args, **kwargs):
        if self.watching and attended_submit._present(self.database):
            with self.database:
                attended_submit._watched_column(self.database)
                self.database.execute("UPDATE %s SET polled_at=? WHERE state=?" % attended_submit.TABLE,
                                      (self.now, attended_submit.REQUESTED))
        return walls.AccessOutcomeTests.cycle(self, *args, **kwargs)

    def form_check(self, check=ON_FORM):
        """The job's unattended submit stopped on a check: it waits on the user."""
        request = self.run_until_submit()
        blocked = dict(STOP, human_check=check) if check else dict(STOP)
        self.assertTrue(desktop_adapter.complete(self.database, dict(
            request, result={"blocked": blocked, "acknowledged": False, "prearm_blocked": True}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        return self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def attended_row(self):
        if not attended_submit._present(self.database):
            return None
        row = self.database.execute("SELECT * FROM %s WHERE job_key=?" % attended_submit.TABLE, (self.key,)).fetchone()
        return dict(row) if row else None

    def cards(self):
        reader = overview_page.Reader(self.database)
        with mock.patch.object(applications, "stamp", return_value=self.now):   # the fixture's clock, for a live lease
            return access_cards.view(reader, {}, lambda value: value, now=None)["cards"]


class ThePress(Case):
    def test_the_press_records_the_request_releases_the_wait_and_asks_for_the_applications_job(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now") as soon:
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        soon.assert_called_once_with("applications")
        row = self.attended_row()
        self.assertEqual((row["state"], json.loads(row["human_check"])["place"]), (attended_submit.REQUESTED, "ON_FORM"))
        self.assertEqual([wait["state"] for wait in self.waits()], [access_outcomes.RESOLVED])
        self.assertEqual((self.row()["state"], self.row()["next_at"]), ("RETRY_WAIT", None))   # due now
        self.assertEqual(attended_submit.due(self.database, self.now), [self.key])
        self.assertEqual(len(self.events("ATTENDED_SUBMIT_REQUESTED")), 1)

    def test_a_check_that_gates_the_site_or_has_no_recorded_place_keeps_the_ordinary_step(self):
        for check in ({"place": "BEFORE_FORM", "after_question": None}, None):
            with self.subTest(check=check):
                self.setUp()
                self.form_check(check)
                with mock.patch.object(scheduler_loop, "run_now") as soon:
                    self.assertIsNone(attended_submit.step(self.database, self.key, self.now, FakeLive()))
                soon.assert_not_called()
                self.assertIsNone(self.attended_row())
                self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def test_review_mode_asks_for_the_approval_first_and_changes_nothing(self):
        self.form_check()
        with mock.patch.object(approvals, "mode", return_value=approvals.REVIEW), \
                mock.patch.object(scheduler_loop, "run_now") as soon:
            self.assertFalse(attended_submit.approved(self.database, self.key))
            html = attended_submit.step(self.database, self.key, self.now, FakeLive())
            self.assertEqual(html, '<p class="access-hint muted" role="status">Approve this application first.</p>')
            self.assertNotIn("hx-post", html)                              # a sentence, not a view that waits
            soon.assert_not_called()
            self.assertIsNone(self.attended_row())
            self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
            with self.database:                                            # the owner approved it
                self.database.execute("INSERT INTO application_approvals(job_key,approval_id,state,held_at,snapshot,"
                                      "payload_hash) VALUES (?,?,'APPROVED',?,'{}','h')", (self.key, "a1", self.now))
            self.assertTrue(attended_submit.approved(self.database, self.key))
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))

    def blocked_by(self):
        """Each thing the product knows is in the way, with its line."""
        import workflow_tasks
        return (
            (mock.patch.object(attended_submit, "_running", lambda: False), "Start Sidedoor, then open this again."),
            (mock.patch.object(scheduler_control, "paused", return_value=True), "Start Sidedoor, then open this again."),
            (mock.patch.object(scheduler_loop, "load_config", lambda root=None: {"applications_enabled": False}),
             "Applications are off."),
            (mock.patch.object(scheduler_loop, "load_config",
                               lambda root=None: {"applications_enabled": True, "scope": "monitor_existing_pipeline"}),
             "Applications are off."),
            (mock.patch.object(workflow_tasks, "compute_used", return_value=10800 - 239),
             "Out of compute today. Open this again tomorrow."))

    def test_what_the_product_knows_is_in_the_way_is_said_before_anything_is_recorded(self):
        self.form_check()
        for patcher, words in self.blocked_by():
            with patcher, mock.patch.object(scheduler_loop, "run_now") as soon:
                html = attended_submit.step(self.database, self.key, self.now, FakeLive())
                self.assertEqual(html, '<p class="access-hint muted" role="status">%s</p>' % words)   # no polling
                soon.assert_not_called()
            self.assertIsNone(self.attended_row())
            self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertIsNone(attended_submit.in_the_way(self.database, self.now))

    def test_after_the_press_a_line_that_waiting_cannot_change_ends_the_request_and_the_button_works_again(self):
        """Owner, 2026-11-13 (a Low he picked): under "Applications are off." and the like the card asked, and
        wrote its row, every second for ever. The request is ended at once and the job waits for its card again."""
        for patcher, words in self.blocked_by():
            self.setUp()
            self.form_check()
            with mock.patch.object(scheduler_loop, "run_now"):
                self.assertIn("every 1s", attended_submit.step(self.database, self.key, self.now, FakeLive()))
            with patcher:
                html = attended_submit.step(self.database, self.key, self.now, FakeLive(), poll=True)
                self.assertEqual(html, '<p class="access-hint muted" role="status">%s</p>' % words)   # nothing asks again
                self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
                wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")                     # its card, as before the press
                self.assertTrue(access_outcomes.form_check(access_outcomes.check(wait)))
                self.assertEqual(len(self.events("ATTENDED_SUBMIT_HANDED_BACK")), 1)
                # The button works: a press says the same true line, and with nothing in the way makes a new request.
                self.assertEqual(attended_submit.step(self.database, self.key, self.now, FakeLive()),
                                 '<p class="access-hint muted" role="status">%s</p>' % words)
                self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
            with mock.patch.object(scheduler_loop, "run_now"):
                self.assertIn(">Opening the application.<", attended_submit.step(self.database, self.key, self.now, FakeLive()))
            self.assertEqual(self.attended_row()["state"], attended_submit.REQUESTED)

    def test_the_lines_that_can_change_by_waiting_keep_asking(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            attended_submit.step(self.database, self.key, self.now, FakeLive())
        late = desktop_adapter.later(self.now, attended_submit.OPENING_BOUND_SECONDS + 1)
        self.assertIn("every 1s", attended_submit.step(self.database, self.key, late, FakeLive(), poll=True))   # "Not open yet."
        self.assertEqual(self.attended_row()["state"], attended_submit.REQUESTED)
        self.assertEqual(attended_submit.STUCK_WORDS, ("Start Sidedoor, then open this again.", "Applications are off.",
                                                       "Out of compute today. Open this again tomorrow."))

    def test_a_session_whose_compute_could_not_be_booked_says_so_and_the_job_waits_for_its_card_again(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            attended_submit.step(self.database, self.key, self.now, FakeLive())
        with self.database:                                                    # the adapter's capacity wait
            self.database.execute("UPDATE application_jobs SET state='WAITING_BROWSER',blocker=? WHERE job_key=?",
                                  (attended_submit.COMPUTE_CODE, self.key))
        self.assertEqual(attended_submit.step(self.database, self.key, self.now, FakeLive(), poll=True),
                         '<p class="access-hint muted" role="status">Out of compute today. Open this again tomorrow.</p>')
        self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")

    def test_opening_the_application_is_never_said_past_its_bound(self):
        self.form_check()
        live = FakeLive()
        with mock.patch.object(scheduler_loop, "run_now"):
            html = attended_submit.step(self.database, self.key, self.now, live)
        self.assertIn(">Opening the application.<", html)
        self.assertIn("every 1s", html)
        at = desktop_adapter.later(self.now, attended_submit.OPENING_BOUND_SECONDS)
        self.assertIn(">Opening the application.<", attended_submit.step(self.database, self.key, at, live))
        late = desktop_adapter.later(self.now, attended_submit.OPENING_BOUND_SECONDS + 1)
        slow = attended_submit.step(self.database, self.key, late, live)
        self.assertIn('<p class="access-hint muted" role="status">Not open yet. Open this again.</p>', slow)
        # Review, 2026-11-13: the slow line keeps asking, so the request stays due and the view can still open.
        self.assertIn("every 1s", slow)
        self.assertEqual(attended_submit.due(self.database, desktop_adapter.later(late, 10)), [self.key])
        much_later = desktop_adapter.later(late, 600)                          # the other job's read finally ended
        self.assertIn("every 1s", attended_submit.step(self.database, self.key, much_later, live, poll=True))
        self.assertEqual(attended_submit.due(self.database, much_later), [self.key])
        self.assertEqual(self.attended_row()["state"], attended_submit.REQUESTED)
        (card,) = [card for card in self.cards() if card.get("attended")]         # the card and its button are there
        self.assertEqual(card["step"]["label"], "Do the human check")

    def test_a_computer_that_cannot_show_a_view_records_nothing(self):
        self.form_check()
        self.assertIsNone(attended_submit.step(self.database, self.key, self.now, FakeLive(available=False)))
        self.assertIsNone(self.attended_row())
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")


class TheSession(Case):
    def pressed(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))

    def test_only_the_requested_jobs_next_submit_is_attended_and_only_once(self):
        self.pressed()
        config = {"a": 1}
        self.assertIs(attended_submit.claim(self.database, config, self.key, "inspect", "r-read", self.now), config)
        self.assertIs(attended_submit.claim(self.database, config, "another-job", "submit", "r-other", self.now), config)
        given = attended_submit.claim(self.database, config, self.key, "submit", "r-submit", self.now)
        self.assertEqual(given[form_check_hold.ATTENDED_KEY],
                         {"human_check": {"place": "ON_FORM", "after_question": 2, "kind": "OTHER", "form_readable": True},
                          "order": attended_submit.HALF_FIRST})
        self.assertEqual(given["a"], 1)
        self.assertEqual((self.attended_row()["state"], self.attended_row()["request_id"]), (attended_submit.STARTED, "r-submit"))
        self.assertEqual(attended_submit.due(self.database, self.now), [])
        self.assertIs(attended_submit.claim(self.database, config, self.key, "submit", "r-again", self.now), config)
        attended_submit.ended(self.database, self.key, "r-other", self.now)        # another request: nothing
        self.assertEqual(self.attended_row()["state"], attended_submit.STARTED)
        attended_submit.ended(self.database, self.key, "r-submit", self.now)
        self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)

    def test_the_round_takes_the_requested_job_first_and_its_submit_session_is_the_attended_one(self):
        other = self.second_job()                                          # queued, and due as well
        self.pressed()
        self.assertNotEqual(other, self.key)
        order = []
        real = applications.process

        def process(database, job, *args, **kwargs):
            order.append(job["job_key"])
            return real(database, job, *args, **kwargs)

        with mock.patch.object(applications, "process", process):
            self.cycle()
        self.assertEqual(order[0], self.key, order)
        request = self.run_until_submit()                                  # the released job reaches its submit again
        given = attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        self.assertIn(form_check_hold.ATTENDED_KEY, given)

    def test_the_adapter_gives_the_attended_config_to_that_submit_session_and_ends_the_request_after_it(self):
        case = jobs.BrowserTurnTests("test_a_busy_browser_is_a_capacity_wait_that_spends_nothing")
        case.setUp()
        self.addCleanup(case.doCleanups)
        database, job = case.database, case.job()
        attended_submit.initialize(database)
        with database:
            # Asked for, and the card is being watched at the adapter's own clock.
            database.execute("INSERT INTO %s(job_key,state,requested_at,human_check) VALUES (?,?,?,?)" % attended_submit.TABLE,
                             (job["job_key"], attended_submit.REQUESTED, jobs.part_c.NOW, json.dumps(ON_FORM)))
        seen = []

        class Stop(Exception):
            pass

        def consumer(root, database, config, request_id):
            seen.append((config.get(form_check_hold.ATTENDED_KEY), request_id))
            raise Stop()

        with self.assertRaises(Stop):                                      # a read of that job: not attended
            case.adapter(consumer).call("inspect", {"job": job, "read_only": True})
        self.assertEqual([item[0] for item in seen], [None])
        state = lambda: database.execute("SELECT state FROM %s" % attended_submit.TABLE).fetchone()[0]  # noqa: E731
        self.assertEqual(state(), attended_submit.REQUESTED)
        with mock.patch.object(desktop_adapter.DesktopAdapter, "call",
                               side_effect=applications.AdapterPending("r-submit", self.now)), \
                mock.patch.object(application_connection.HostedDesktopAdapter, "request_body",
                                  return_value={"attempt_id": "a1", "job": job}), \
                mock.patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None):
            with database:
                database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                    created_at,expires_at,claims,attempt_id) VALUES ('r-submit','b',?,'submit','{}','PENDING',?,?,0,'a1')""",
                                 (job["job_key"], self.now, desktop_adapter.later(self.now, 900)))
            with self.assertRaises(Stop):
                case.adapter(consumer).call("submit", {"job": job, "attempt_id": "a1"})
        self.assertEqual(seen[-1], ({"human_check": ON_FORM, "order": attended_submit.HALF_FIRST}, "r-submit"))
        self.assertEqual(state(), attended_submit.ENDED)                   # ended whatever the session did


class TheCard(Case):
    def pressed(self, live):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            return attended_submit.step(self.database, self.key, self.now, live)

    def start_session(self, endpoint=True):
        """The applications job reached the submit: the request is claimed and the session has its folder."""
        request = self.run_until_submit()
        attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        folder = Path(tempfile.mkdtemp())
        with self.database:
            desktop_adapter.record(self.database, request["request_id"], "BROWSER_SESSION_DIRECTORY", self.now,
                                   {"directory": str(folder)})
        if endpoint:
            (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps(
                {"decision": "BACKGROUND_LAUNCHED", "endpoint": "http://127.0.0.1:9", "headless": True}) + "\n")
        return request, folder

    def test_the_card_says_one_true_line_and_then_shows_the_view_of_that_session(self):
        live = FakeLive()
        html = self.pressed(live)
        self.assertIn(attended_submit.STARTING_WORDS, html)
        self.assertIn('hx-trigger="every 1s"', html)                       # it asks again by itself
        self.assertIn("&quot;job_key&quot;", html)
        self.assertEqual(live.offered, [])
        # The job stopped waiting, so its own card is gone; this one holds the view, and is counted.
        (card,) = [card for card in self.cards() if card.get("attended")]
        self.assertEqual((card["key"], card["step"]["label"], card["abandon"]), (self.key, "Do the human check", False))
        self.assertEqual(access_cards.card_count(overview_page.Reader(self.database)), 1)
        # Another application's session is running: the other line (Y5).
        other = self.second_job()
        with self.database:
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                created_at,expires_at,lease_until,claims,armed_at) VALUES ('busy','b',?,'submit','{}','CLAIMED',?,?,?,1,?)""",
                                  (other, self.now, desktop_adapter.later(self.now, 900), desktop_adapter.later(self.now, 900),
                                   self.now))   # armed: it runs to its end (an unarmed one is asked to stop, E16 Y1 c)
        self.assertIn(attended_submit.BUSY_WORDS, attended_submit.step(self.database, self.key, self.now, live))
        with self.database:
            self.database.execute("DELETE FROM desktop_requests WHERE request_id='busy'")
        # Its own session started, Chrome not yet: still the first line. Then the view, with the owner's line.
        request, folder = self.start_session(endpoint=False)
        self.assertIn(attended_submit.STARTING_WORDS, attended_submit.step(self.database, self.key, self.now, live))
        (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps(
            {"decision": "BACKGROUND_LAUNCHED", "endpoint": "http://127.0.0.1:9", "headless": True}) + "\n")
        # Chrome is up but shows no real page yet: never a blank view (owner, 2026-11-13).
        with mock.patch.object(live_view, "session_page", return_value=None):
            self.assertIn(attended_submit.STARTING_WORDS, attended_submit.step(self.database, self.key, self.now, live))
        self.assertEqual(live.offered, [])
        with mock.patch.object(live_view, "session_page", return_value="https://jobs.example.test/apply"):
            html = attended_submit.step(self.database, self.key, self.now, live)
        self.assertEqual(live.offered, [(self.key, str(folder), live_view.domain_of(json.loads(self.row()["payload"])["url"]))])
        (card,) = [card for card in self.cards() if card.get("attended")]
        self.assertEqual(card["warning"], "Do the human check. Do not press the site's Submit button.")   # above the view
        self.assertIn("/live-view/target-1/fragment", html)
        self.assertNotIn("every 1s", html)
        # The session is over: the request ends, the card goes, and the ordinary step applies again.
        attended_submit.ended(self.database, self.key, request["request_id"], self.now)
        self.assertEqual([card for card in self.cards() if card.get("attended")], [])
        self.assertIsNone(attended_submit.status(self.database, self.key, self.now))

    def test_a_session_that_died_or_a_job_that_went_another_way_leaves_no_card(self):
        live = FakeLive()
        self.pressed(live)
        request, folder = self.start_session()
        with self.database:                                                # the process was killed: the lease ran out
            self.database.execute("UPDATE desktop_requests SET lease_until=? WHERE request_id=?",
                                  (desktop_adapter.later(self.now, -1), request["request_id"]))
        self.assertEqual([card for card in self.cards() if card.get("attended")], [])   # drawn read-only
        self.assertEqual(self.attended_row()["state"], attended_submit.STARTED)
        self.assertIsNone(attended_submit.step(self.database, self.key, self.now, live))
        self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
        self.assertIn("/access/section?at=overview",
                      attended_submit.step(self.database, self.key, self.now, live, at="overview", poll=True))

    def test_an_install_with_no_database_or_no_such_table_draws_no_card(self):
        import sqlite3
        self.assertEqual(attended_submit.cards(None, self.now), [])
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        self.assertEqual(attended_submit.cards(empty, self.now), [])
        self.assertEqual(access_cards.attended_cards(None), [])

    def test_the_cards_button_goes_through_the_ordinary_route(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"), \
                mock.patch.object(attended_submit, "step", return_value="<p>attended</p>") as step:
            self.assertEqual(access_cards.step(self.database, self.key), "<p>attended</p>")
        step.assert_called_once()

    def test_the_template_loads_the_attended_cards_view_by_itself(self):
        template = (Path(access_cards.__file__).parent / "dashboard_assets/templates/access_cards.html").read_text()
        self.assertIn("{% if c.attended %}", template)
        self.assertIn('hx-trigger="load"', template)


class TheReadAlreadyKnows(Case):
    """Owner, 2026-11-13: a read that finds a human check ON THE FORM, with the form readable, is a completed
    read. The application goes on (answering, and approval in Review mode); ready to submit, it waits on the
    user, and the card's button starts the attended submit. No unattended submit is spent finding the check."""
    read_check = {"place": "ON_FORM", "after_question": 5, "kind": "OTHER", "form_readable": True}

    def review(self):
        review = walls.AccessOutcomeTests.review(self)
        return dict(review, human_check=self.read_check) if self.read_check else review

    def until_ready(self, rounds=8):
        """Rounds until the job stops moving; any read on the way is completed with this class's review."""
        for _ in range(rounds):
            next_at = self.row()["next_at"]
            if next_at and next_at > self.now and self.row()["state"] != "BLOCKED":
                self.now = next_at
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "form-check-fixture", self.now)
            if request and request["payload"]["operation"] == "submit":
                return request
            if request:
                self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
            self.advance(60)
        return None

    def submits(self):
        return [row for row in self.requests() if row["operation"] == "submit"]

    def test_ready_to_submit_the_job_waits_on_the_user_and_no_unattended_submit_is_started(self):
        self.assertIsNone(self.until_ready())
        wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")          # attempts 0
        self.assertEqual(access_outcomes.check(wait), self.read_check)
        self.assertEqual(self.submits(), [])
        self.assertFalse(self.database.execute("SELECT 1 FROM application_attempts WHERE job_key=?", (self.key,)).fetchone())
        (card,) = [card for card in self.cards() if card["key"] == self.key]
        self.assertEqual(card["need"], "The site needs a human check.")
        self.assertIsNotNone(attended_submit.form_check_wait(self.database, self.key))   # its button starts the attended submit

    def test_the_press_starts_the_first_submit_and_it_is_the_attended_one(self):
        self.assertIsNone(self.until_ready())
        with mock.patch.object(scheduler_loop, "run_now"):
            html = attended_submit.step(self.database, self.key, self.now, FakeLive())
        self.assertIn(">Opening the application.<", html)
        request = self.until_ready()
        self.assertIsNotNone(request, "the attended submit was not started")
        self.assertEqual(len(self.submits()), 1)                               # the first submit of this job
        given = attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        self.assertEqual(given[form_check_hold.ATTENDED_KEY]["human_check"], self.read_check)
        # It ends at its time limit: the card comes back; still no unattended submit is started.
        attended_submit.ended(self.database, self.key, request["request_id"], self.now)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={
            "blocked": dict(STOP, human_check=self.read_check), "acknowledged": False, "prearm_blocked": True}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertIsNone(self.until_ready())
        self.assertEqual(len(self.submits()), 1)

    def test_review_mode_holds_for_approval_first_and_the_card_comes_after_it(self):
        with mock.patch.object(approvals, "mode", return_value=approvals.REVIEW):
            self.assertIsNone(self.until_ready())
            self.assertEqual(self.row()["blocker"], applications.AWAITING_APPLICATION_APPROVAL)
            self.assertEqual(self.waits(), [])                                 # no human-check card before the approval
            self.assertEqual([card for card in self.cards() if card["key"] == self.key], [])
            self.assertEqual(self.submits(), [])

    def test_an_approved_application_waits_for_the_card_and_spends_no_unattended_submit(self):
        """Review mode, after "Approve and submit" (the sitting's order: approved 20:25:17, the card 20:25:25).
        The approval starts a round, not a submit: the round finds the check on the form and waits on the user."""
        with mock.patch.object(approvals, "mode", return_value=approvals.REVIEW):
            self.assertIsNone(self.until_ready())
            self.assertEqual(self.row()["blocker"], applications.AWAITING_APPLICATION_APPROVAL)
            with mock.patch.object(applications, "approved_application", return_value={"answers": {}}):
                with self.database:                                        # "Approve and submit"
                    self.database.execute("UPDATE application_jobs SET blocker=?,next_at=NULL WHERE job_key=?",
                                          (applications.APPLICATION_APPROVED, self.key))
                    self.assertEqual(self.database.execute(
                        "UPDATE application_approvals SET state='APPROVED' WHERE job_key=?", (self.key,)).rowcount, 1)
                for _ in range(3):                                         # round after round, with no press
                    self.assertIsNone(self.until_ready(rounds=2))
                self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
                self.assertEqual(self.submits(), [])
                self.assertEqual(self.row()["attempts"], 0)
                with mock.patch.object(scheduler_loop, "run_now"):          # the press: the first submit, attended
                    self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
                request = self.until_ready()
                self.assertIsNotNone(request)
                self.assertEqual(len(self.submits()), 1)

    def reads(self):
        return [row for row in self.requests() if row["operation"] == "inspect"]

    def test_the_press_goes_straight_to_the_attended_submit_with_no_second_read(self):
        """The dry run of 2026-11-13: a 60 s read ran between the press and the attended session."""
        self.assertIsNone(self.until_ready())
        reads = len(self.reads())
        self.advance(3600)                                                   # long after the read
        with mock.patch.object(applications, "reusable_read", return_value=None), \
                mock.patch.object(scheduler_loop, "run_now") as now:        # no stored complete read to fall back on
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
            now.assert_called_once_with("applications")                      # started at once, not at the next tick
            request = self.until_ready(rounds=1)
        self.assertIsNotNone(request, "the round after the press did not start the submit")
        self.assertEqual(request["payload"]["operation"], "submit")
        self.assertEqual(len(self.reads()), reads)
        self.assertEqual(len(self.events("APPLICATION_READ_KEPT_FOR_ATTENDED_SUBMIT")), 1)
        # The submit carries the application's answers and questions, as any submit does.
        self.assertTrue(request["payload"]["answers"])

    def test_the_session_starts_at_the_forms_own_address_when_the_read_recorded_it(self):
        """The dry run of 2026-11-13: the first page shown was the posting, and the AI reached the form 10 s later."""
        form = "https://jobs.ashbyhq.com/synthetic/role-123/application"
        self.review = lambda: dict(walls.AccessOutcomeTests.review(self), human_check=self.read_check,
                                   official_url=form, official_destination_verified=True)
        self.assertIsNone(self.until_ready())
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        request = self.until_ready()
        told = attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        self.assertEqual(told[form_check_hold.ATTENDED_KEY]["start_url"], form)
        for review in ({"official_url": form}, {"official_url": form, "official_destination_verified": False},
                       {"official_url": "javascript:alert(1)", "official_destination_verified": True},
                       {"official_url": "http://jobs.ashbyhq.com/x", "official_destination_verified": True},   # the submit's own rule
                       {"official_url": "https://127.0.0.1/apply", "official_destination_verified": True},
                       {"official_url": None, "official_destination_verified": True}, None):
            self.assertIsNone(attended_submit.form_address(review), review)   # then: the posting's address

    def test_without_a_press_the_kept_read_is_not_used(self):
        self.assertIsNone(self.until_ready())
        self.assertIsNotNone(attended_submit.ready_read(self.database, self.key))
        with mock.patch.object(applications, "reusable_read", return_value=None):
            with self.database:
                access_outcomes.resume(self.database, self.key, self.now)   # released some other way
            self.cycle()
        self.assertFalse(self.events("APPLICATION_READ_KEPT_FOR_ATTENDED_SUBMIT"))

    def test_a_form_whose_questions_are_hidden_until_the_check_is_done_is_not_this_case(self):
        self.read_check = dict(self.read_check, form_readable=False)
        self.assertIsNotNone(self.until_ready())                               # the ordinary path, as before


class NobodyWatching(Case):
    """Review, 2026-11-13: a request whose card had stopped asking started a session nobody saw, which held the
    browser for its 9 minutes. An attended session starts only while its card is being watched now."""

    def test_a_request_whose_card_stopped_asking_is_ended_and_the_job_waits_for_its_card_again(self):
        self.assertEqual(attended_submit.WATCH_SECONDS, 15)
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        self.assertEqual(attended_submit.due(self.database, desktop_adapter.later(self.now, 15)), [self.key])
        self.watching = False                                                  # "Not open yet. Open this again.": no more polls
        submits = len([row for row in self.requests() if row["operation"] == "submit"])
        self.advance(180)
        self.assertEqual(attended_submit.due(self.database, self.now), [])
        self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
        for _ in range(4):                                                     # the round that would have started it
            self.cycle()
            self.advance(60)
        self.assertEqual(len([row for row in self.requests() if row["operation"] == "submit"]), submits)
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")                 # the card is back, to be pressed again
        self.assertIsNotNone(attended_submit.form_check_wait(self.database, self.key))

    def test_each_poll_of_the_card_keeps_the_request_due(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            attended_submit.step(self.database, self.key, self.now, FakeLive())
        self.advance(60)
        attended_submit.step(self.database, self.key, self.now, FakeLive(), poll=True)   # the card asking
        self.assertEqual(attended_submit.due(self.database, desktop_adapter.later(self.now, 10)), [self.key])

    def pending_submit(self):
        """Rounds until the job's submit request exists, unclaimed (the adapter is about to start its session)."""
        for _ in range(8):
            self.cycle()
            row = self.database.execute("SELECT request_id FROM desktop_requests WHERE job_key=? AND operation='submit' "
                                        "AND state='PENDING'", (self.key,)).fetchone()
            if row:
                return row[0]
            request = desktop_adapter.claim(self.database, self.root, "fixture", self.now)
            if request:
                self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
            self.advance(60)
        self.fail("no submit request")

    def test_a_card_closed_between_the_rounds_check_and_the_session_gets_no_session_and_the_card_back(self):
        """Owner, 2026-11-13 (a Low he picked): the claim on an unwatched row gave one unattended submit."""
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        request_id = self.pending_submit()
        self.assertEqual(self.row()["attempts"], 1)                            # the round passed its check and claimed the try
        later = desktop_adapter.later(self.now, attended_submit.WATCH_SECONDS + 1)   # the tab was closed meanwhile
        self.assertTrue(attended_submit.unwatched(self.database, self.key, later))
        self.assertFalse(attended_submit.unwatched(self.database, self.key, self.now))
        self.assertTrue(attended_submit.hand_back(self.database, self.root, self.key, request_id, later))
        self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)
        self.watching = False
        self.now = later
        self.advance(60)
        self.cycle()
        self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")                 # attempts 0: no try spent; its card is back
        self.assertTrue(self.events("ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED"))
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.assertFalse(self.database.execute("SELECT 1 FROM desktop_events WHERE request_id=? AND event='BROWSER_SESSION_DIRECTORY'",
                                               (request_id,)).fetchone())      # no session ever ran for it

    def test_the_adapter_starts_no_session_for_an_unwatched_request(self):
        case = jobs.BrowserTurnTests("test_a_busy_browser_is_a_capacity_wait_that_spends_nothing")
        case.setUp()
        self.addCleanup(case.doCleanups)
        database, job = case.database, case.job()
        attended_submit.initialize(database)
        attended_submit._watched_column(database)
        stale = desktop_adapter.later(jobs.part_c.NOW, -(attended_submit.WATCH_SECONDS + 5))
        with database:
            database.execute("INSERT INTO %s(job_key,state,requested_at,human_check,polled_at) VALUES (?,?,?,?,?)"
                             % attended_submit.TABLE, (job["job_key"], attended_submit.REQUESTED, stale, json.dumps(ON_FORM), stale))
        answered = {"status": "UNKNOWN", "prearm_blocked": True}
        calls = iter([applications.AdapterPending("r-submit", self.now), answered])

        def call(*args, **kwargs):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(desktop_adapter.DesktopAdapter, "call", call), \
                mock.patch.object(application_connection.HostedDesktopAdapter, "request_body",
                                  return_value={"attempt_id": "a1", "job": job}), \
                mock.patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None), \
                mock.patch.object(attended_submit, "hand_back", return_value=True) as back:
            with database:
                database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                    created_at,expires_at,claims,attempt_id) VALUES ('r-submit','b',?,'submit','{}','PENDING',?,?,0,'a1')""",
                                 (job["job_key"], self.now, desktop_adapter.later(self.now, 900)))
            result = case.adapter(lambda *args: self.fail("no session may start")).call("submit", {"job": job, "attempt_id": "a1"})
        self.assertIs(result, answered)
        back.assert_called_once()
        self.assertEqual(back.call_args.args[2:4], (job["job_key"], "r-submit"))

    def test_a_request_nobody_watches_is_not_made_attended_at_the_claim_either(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        config = {}
        self.assertIs(attended_submit.claim(self.database, config, self.key, "submit", "r1",
                                            desktop_adapter.later(self.now, attended_submit.WATCH_SECONDS + 1)), config)
        self.assertIn(form_check_hold.ATTENDED_KEY, attended_submit.claim(self.database, config, self.key, "submit", "r1", self.now))

    def test_the_kept_read_is_dropped_once_the_session_has_it(self):
        self.form_check()
        with self.database:
            attended_submit.remember_ready(self.database, self.key, {"official_url": "https://jobs.example.com/a"}, self.now)
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        request = self.run_until_submit()
        attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        self.assertIsNone(attended_submit.ready_read(self.database, self.key))


class WhileAnotherApplicationRuns(Case):
    """Y5 (owner, 2026-11-12): "when a submit is in progress, a short honest wait, then the check opens by
    itself". Nothing running is interrupted; the attended session is the round's next one."""

    def test_a_press_during_a_round_is_taken_next_in_that_round_after_the_job_in_hand(self):
        self.form_check()
        other = self.second_job()
        third = self.second_job("789")
        order, pressed = [], []
        real = applications.process

        def process(database, job, *args, **kwargs):
            order.append(job["job_key"])
            result = real(database, job, *args, **kwargs)
            if job["job_key"] != self.key and not pressed:               # the user presses while this one runs
                pressed.append(job["job_key"])
                with mock.patch.object(scheduler_loop, "run_now"):
                    self.assertIsNone(attended_submit.press(database, self.key, self.now))
            return result

        with mock.patch.object(applications, "process", process), \
                mock.patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        self.assertEqual(len(pressed), 1)
        self.assertEqual(order[order.index(pressed[0]) + 1], self.key, order)   # next, before the jobs already listed
        self.assertEqual(sorted(order), sorted([self.key, other, third]), order)
        self.assertEqual(order.count(self.key), 1)

    def test_the_card_says_so_while_another_applications_session_runs_and_nothing_of_it_is_touched(self):
        self.form_check()
        other = self.second_job()
        with self.database:
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,
                created_at,expires_at,lease_until,claims,armed_at) VALUES ('busy','b',?,'submit','{}','CLAIMED',?,?,?,1,?)""",
                                  (other, self.now, desktop_adapter.later(self.now, 900),
                                   desktop_adapter.later(self.now, 900), self.now))
        before = dict(self.database.execute("SELECT * FROM desktop_requests WHERE request_id='busy'").fetchone())
        with mock.patch.object(scheduler_loop, "run_now"):
            html = attended_submit.step(self.database, self.key, self.now, FakeLive())
        self.assertIn(">Opens in a moment.<", html)
        self.assertIn("every 1s", html)
        self.assertEqual(dict(self.database.execute("SELECT * FROM desktop_requests WHERE request_id='busy'").fetchone()), before)
        self.assertEqual(self.attended_row()["state"], attended_submit.REQUESTED)


class AfterASessionThatTimedOut(Case):
    """E16.T3, the submit path. What it asks for already exists there and is pinned here for the attended
    session: a submit session that ends without a result and never armed (its time ran out) spends no try
    (`applications.reconcile`, ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED), the job is retried in a new session
    (which starts from the site's saved session, site_sessions), and if the site asks again the card comes
    back. Never a silent close."""

    def test_the_try_is_not_spent_the_job_is_retried_and_the_card_comes_back_if_the_site_asks_again(self):
        self.form_check()
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        request = self.run_until_submit()
        self.assertIn(form_check_hold.ATTENDED_KEY,
                      attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now))
        self.assertEqual(self.row()["attempts"], 1)
        # The session ran out of time: no result was ever stored, and its lease is over.
        self.now = desktop_adapter.later(self.database.execute(
            "SELECT lease_until FROM desktop_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0], 1)
        self.cycle()
        self.assertEqual(self.row()["attempts"], 0)                                  # the try was not spent
        self.assertTrue(self.events("ATTEMPT_NOT_COUNTED_CAPABILITY_BLOCKED"))
        self.assertNotIn(self.row()["state"], applications.FINAL)                    # never a silent close
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        self.assertIsNone(attended_submit.status(self.database, self.key, self.now))   # its card is gone
        # The retry spends no unattended session on a check the job is known to have: the card comes back.
        submits = len([row for row in self.requests() if row["operation"] == "submit"])
        for _ in range(3):                                                     # past the retry's own backoff
            if self.row()["state"] == "RETRY_WAIT" and self.row()["next_at"] and self.row()["next_at"] > self.now:
                self.now = self.row()["next_at"]
            self.advance(1)
            self.cycle()
        wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")          # attempts 0
        self.assertEqual(access_outcomes.check(wait)["place"], "ON_FORM")
        self.assertEqual(len(self.waits()), 2)
        self.assertEqual(len([row for row in self.requests() if row["operation"] == "submit"]), submits)


class TheOrder(Case):
    """Y3 and the owner's fallback: one stored setting per kind of check, HALF_FIRST unless set; the number
    that decides it is recorded by the attended sessions themselves."""

    def test_every_kind_starts_half_first_and_a_stored_setting_is_what_changes_it(self):
        attended_submit.initialize(self.database)
        for kind in access_outcomes.CHECK_KINDS + (None, "SOMETHING_NEW"):
            self.assertEqual(attended_submit.order_for(self.database, kind), attended_submit.HALF_FIRST, kind)
        with self.database:
            attended_submit.set_order(self.database, "HCAPTCHA", attended_submit.CHECK_FIRST, self.now)
        self.assertEqual(attended_submit.order_for(self.database, "HCAPTCHA"), attended_submit.CHECK_FIRST)
        self.assertEqual(attended_submit.order_for(self.database, "TURNSTILE"), attended_submit.HALF_FIRST)
        with self.database:
            attended_submit.set_order(self.database, "HCAPTCHA", attended_submit.HALF_FIRST, self.now)
        self.assertEqual(attended_submit.order_for(self.database, "HCAPTCHA"), attended_submit.HALF_FIRST)
        for kind, order in (("HCAPTCHA", "LAST"), ("CAPTCHA9", attended_submit.CHECK_FIRST)):
            with self.assertRaisesRegex(ValueError, "HUMAN_CHECK_ORDER_INVALID"):
                attended_submit.set_order(self.database, kind, order, self.now)

    def attended_session(self, kind="HCAPTCHA"):
        self.form_check(dict(ON_FORM, kind=kind))
        with mock.patch.object(scheduler_loop, "run_now"):
            self.assertIsNone(attended_submit.press(self.database, self.key, self.now))
        request = self.run_until_submit()
        config = attended_submit.claim(self.database, {}, self.key, "submit", request["request_id"], self.now)
        folder = Path(tempfile.mkdtemp())
        with self.database:
            desktop_adapter.record(self.database, request["request_id"], "BROWSER_SESSION_DIRECTORY", self.now,
                                   {"directory": str(folder)})
        return request, config, folder

    def test_the_session_is_told_the_place_the_kind_and_the_order_for_that_kind(self):
        attended_submit.initialize(self.database)
        with self.database:
            attended_submit.set_order(self.database, "TURNSTILE", attended_submit.CHECK_FIRST, self.now)
        request, config, folder = self.attended_session("TURNSTILE")
        self.assertEqual(config[form_check_hold.ATTENDED_KEY],
                         {"human_check": {"place": "ON_FORM", "after_question": 2, "kind": "TURNSTILE", "form_readable": True},
                          "order": attended_submit.CHECK_FIRST})

    def test_an_accepted_attended_submit_records_the_seconds_from_done_to_the_arm(self):
        request, config, folder = self.attended_session()
        done_at = desktop_adapter.later(self.now, 100)
        (folder / "human_check.json").write_text(json.dumps({"held": 40.0, "began": None, "done_at": done_at}))
        with self.database:
            self.database.execute("UPDATE desktop_requests SET armed_at=?,result=? WHERE request_id=?",
                                  (desktop_adapter.later(done_at, 37), json.dumps({"acknowledged": True}), request["request_id"]))
        attended_submit.ended(self.database, self.key, request["request_id"], self.now)
        (number,) = attended_submit.measurements(self.database, "HCAPTCHA")
        self.assertEqual((number["kind"], number["order"], number["seconds"], number["job_key"]),
                         ("HCAPTCHA", attended_submit.HALF_FIRST, 37.0, self.key))
        self.assertEqual(attended_submit.measurements(self.database, "TURNSTILE"), [])
        # Recording a number changes no setting.
        self.assertEqual(attended_submit.order_for(self.database, "HCAPTCHA"), attended_submit.HALF_FIRST)

    def test_a_session_that_never_armed_or_was_not_confirmed_records_no_number(self):
        for armed, result in ((False, {"acknowledged": False}), (True, {"acknowledged": False}), (True, None)):
            with self.subTest(armed=armed, result=result):
                self.setUp()
                request, config, folder = self.attended_session()
                (folder / "human_check.json").write_text(json.dumps({"held": 40.0, "began": None, "done_at": self.now}))
                with self.database:
                    self.database.execute("UPDATE desktop_requests SET armed_at=?,result=? WHERE request_id=?",
                                          (desktop_adapter.later(self.now, 5) if armed else None,
                                           json.dumps(result) if result else None, request["request_id"]))
                attended_submit.ended(self.database, self.key, request["request_id"], self.now)
                self.assertEqual(attended_submit.measurements(self.database), [])
                self.assertEqual(self.attended_row()["state"], attended_submit.ENDED)


if __name__ == "__main__":
    unittest.main()
