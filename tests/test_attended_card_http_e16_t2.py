"""E16.T2, the first real sitting (owner at the dashboard, 2026-11-13): after the card was pressed and the
attended session had started, the card said "This application is no longer waiting for you." and showed no
view. Here: the real dashboard handlers over HTTP, with the real records written in that sitting's order
(the wait, the press, the wait released, the session started, the endpoint recorded), and a page reload."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import access_cards
import access_outcomes
import applications
import attended_submit
import browser_runtime
import desktop_adapter
import form_check_hold
import live_view
import scheduler_loop
from tests import test_access_cards_e15_t2 as cards

CHECK = {"after_question": 5, "form_readable": True, "kind": "OTHER", "place": "ON_FORM"}
KEY = "kestrel aerospace:mock-11"


@unittest.skipUnless(cards.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class TheSitting(unittest.TestCase):
    for _name in ("row", "page", "post", "cards"):   # the E15.T2 HTTP harness (not its tests)
        locals()[_name] = getattr(cards.AccessCardsTest, _name)
    del _name

    def setUp(self):
        cards.AccessCardsTest.setUp(self)
        self.view = self.client.app.state.live_view
        # This computer can show a live view, Sidedoor is running and applications are on (the sitting's install).
        for target, name, value in ((self.view, "is_supported", lambda config=None: True),
                                    (attended_submit, "_running", lambda: True),
                                    (scheduler_loop, "load_config", lambda root=None: {"applications_enabled": True}),
                                    (scheduler_loop, "run_now", mock.Mock())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        payload = json.dumps({"company": "Kestrel Aerospace", "role": "Staff Software Engineer",
                              "url": "https://careers.example.com/jobs/1001/apply"})
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                                  "VALUES (?,?,?,?,'QUEUED',?)", (KEY, "kestrel aerospace", "mock-11", payload, self.now))
        with self.database:   # 20:25:25: ready to submit, the job waits on the user (applications.process)
            self.assertEqual(access_outcomes.record(self.database, self.row(KEY), "CAPTCHA_REQUIRED", self.now,
                                                    result={"blocked": {"human_check": CHECK}}), "WAITING")

    def runner_starts_the_session(self, endpoint=False):
        """20:25:55: the applications round claimed the submit; the job is SUBMITTING and its session has a folder."""
        attempt, request = str(uuid.uuid4()), str(uuid.uuid4())
        folder = Path(tempfile.mkdtemp())
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='SUBMITTING',attempts=1,blocker=NULL WHERE job_key=?", (KEY,))
            self.database.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at,evidence) "
                                  "VALUES (?,?,?,'SUBMITTING',?,'{}')", (attempt, KEY, KEY, self.now))
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,
                expires_at,lease_until,claims,attempt_id) VALUES (?,?,?,'submit','{}','CLAIMED',?,?,?,1,?)""",
                                  (request, "b", KEY, self.now, desktop_adapter.later(self.now, 900),
                                   desktop_adapter.later(self.now, 960), attempt))
            self.assertIn(form_check_hold.ATTENDED_KEY, attended_submit.claim(self.database, {}, KEY, "submit", request, self.now))
            desktop_adapter.record(self.database, request, "BROWSER_SESSION_DIRECTORY", self.now, {"directory": str(folder)})
        self.assertTrue(applications.uncertain_submission(self.database, KEY))   # as in the sitting
        return folder

    def endpoint_recorded(self, folder):
        (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps(
            {"decision": "BACKGROUND_LAUNCHED", "endpoint": "http://127.0.0.1:9", "headless": True}) + "\n")

    def poll(self):
        """The first call is the press (the card's button); every later one is the card asking by itself."""
        pressed, self.pressed = getattr(self, "pressed", False), True
        response = self.client.post("/access/step", data=dict({"job_key": KEY, "at": "approvals"}, **({"poll": "1"} if pressed else {})),
                                    headers=cards.HTMX)
        self.assertEqual(response.status_code, 200, response.text[:300])
        import html
        return html.unescape(response.text)

    def test_the_card_keeps_its_place_by_the_request_from_the_press_to_the_view(self):
        page = self.page("/approvals")
        self.assertIn(">Do the human check</button>", page)
        # The one instruction is on the card BEFORE the press, in the error colour, directly above the view's place.
        (card,) = [card for card in self.cards(page) if "Kestrel Aerospace" in card]
        warning = '<p class="access-warning" role="note">Do the human check. Do not press the site\'s Submit button.</p>'
        self.assertIn(warning, card)
        self.assertLess(card.index(warning), card.index('class="access-step"'))
        self.assertIn(".access-warning { color: var(--error); font-weight: 700;", self.client.get("/static/dashboard.css").text)
        pressed = self.poll()                                                  # 20:25:37: the press
        self.assertIn(">Opening the application.<", pressed)
        self.assertIn('hx-post="/access/step"', pressed)
        self.assertEqual(self.database.execute("SELECT state FROM application_access_waits").fetchone()[0], "RESOLVED")
        self.assertIn(">Opening the application.<", self.poll())               # the wait is gone; the request holds the place
        folder = self.runner_starts_the_session()
        opening = self.poll()                                                  # the session runs, Chrome not up yet
        self.assertNotIn(access_cards.GONE, opening)
        self.assertNotIn(live_view.GONE_WORDS, opening)
        self.assertIn(">Opening the application.<", opening)
        self.endpoint_recorded(folder)
        with mock.patch.object(live_view, "session_page", return_value=None):   # Chrome is up, on a blank page
            blank = self.poll()
        self.assertIn(">Opening the application.<", blank)                     # never a blank view
        self.assertNotIn("live-view", blank)
        self.assertEqual(self.view.targets, {})
        with mock.patch.object(live_view, "session_page", return_value="https://careers.example.com/jobs/1001/apply"):
            shown = self.poll()
        (target,) = self.view.targets.values()
        self.assertEqual((target.job, target.attach), (KEY, folder))
        self.assertIn("/live-view/%s/fragment" % target.id, shown)
        self.assertNotIn("every 1s", shown)
        self.assertIn("every 1s", blank)
        fragment = self.client.get("/live-view/%s/fragment" % target.id, params={"start": "1", "card": "1"})
        self.assertEqual(fragment.status_code, 200, fragment.text[:300])
        self.assertNotIn(live_view.GONE_WORDS, fragment.text)
        # Done closes the view on the press itself (owner, 2026-11-13, every live view): the page has the done
        # line at hand and its script hides the picture before the server answers.
        self.assertIn('data-done="Done. Sidedoor carries on from here."', fragment.text)
        script = self.client.get("/static/live_view.js").text
        self.assertIn('if (closedEarly) { live(false);', script)
        self.assertIn('"live-view-over"', script)

    def test_a_reload_and_coming_back_from_another_page_show_the_card_of_the_live_request(self):
        self.poll()
        for started in (False, True):
            if started:
                self.endpoint_recorded(self.runner_starts_the_session())
            for path in ("/approvals", "/"):
                (card,) = [card for card in self.cards(self.page(path)) if "Kestrel Aerospace" in card]
                self.assertIn(">Do the human check</button>", card)
                self.assertIn('hx-trigger="load"', card)                       # its line or its view loads by itself
                self.assertIn(KEY, card)
                self.assertNotIn(">Abandon</button>", card)
            self.assertNotIn(access_cards.GONE, self.poll())
        section = self.client.get("/access/section", params={"at": "approvals"}, headers=cards.HTMX)
        self.assertIn("Kestrel Aerospace", section.text)

    def test_when_the_session_is_over_the_card_goes_and_a_late_poll_says_so(self):
        self.poll()
        self.runner_starts_the_session()
        with self.database:
            request = self.database.execute("SELECT request_id FROM desktop_requests").fetchone()[0]
            attended_submit.ended(self.database, KEY, request, self.now)
        self.assertEqual([card for card in self.cards(self.page("/approvals")) if "Kestrel Aerospace" in card], [])
        late = self.poll()                                                     # the section is drawn again; never a view
        self.assertIn('hx-get="/access/section?at=approvals"', late)
        self.assertIn('hx-target="#sign-ins"', late)
        self.assertNotIn("live-view", late)

    # --- after Done (owner, 2026-11-13: "why does the approval card still show 'do the human check button' after
    # the check was successfully completed?") ---

    def attached(self):
        """Press, the session starts, the view attaches."""
        self.poll()
        folder = self.runner_starts_the_session()
        self.endpoint_recorded(folder)
        with mock.patch.object(live_view, "session_page", return_value="https://careers.example.com/jobs/1001/apply"):
            self.assertIn("/live-view/", self.poll())
        return folder

    def card(self, path="/approvals"):
        found = [card for card in self.cards(self.page(path)) if "Kestrel Aerospace" in card]
        return found[0] if found else None

    def request_id(self):
        return self.database.execute("SELECT request_id FROM desktop_requests").fetchone()[0]

    def test_after_done_the_card_offers_nothing_and_says_only_that_it_is_done_while_the_session_runs(self):
        folder = self.attached()
        self.assertIn(">Do the human check</button>", self.card())
        form_check_hold.finish(folder)                                         # Done in the attached view
        with mock.patch.object(live_view, "session_page", return_value="https://careers.example.com/jobs/1001/apply"):
            polled = self.poll()
            for path in ("/approvals", "/"):
                card = self.card(path)
                # In the green of the success banner (owner, 2026-11-13), the same classes the flash uses.
                self.assertIn('<p class="access-need note note-success">Done. Sidedoor carries on from here.', card)
                # Nothing is kept under the green line (owner, 2026-11-13: an empty gap of about 30 px): no row of
                # actions, and the place of the view takes no space.
                self.assertNotIn("access-actions", card)
                self.assertIn('class="access-step access-step-quiet"', card)
                for absent in ("</button>", "access-warning", "Sidedoor's browser closed", "The site needs a human check"):
                    self.assertNotIn(absent, card, absent)
        self.assertIn('hx-trigger="every 1s"', polled)                         # it keeps asking, and shows nothing
        self.assertNotIn("live-view", polled)
        self.assertNotIn("<p", polled)
        # The redraw after Done says the sentence once: the card says it, so the section's flash does not.
        section = self.client.get("/access/section", params={"at": "approvals", "ended": "done"}, headers=cards.HTMX).text
        self.assertEqual(section.count("Done. Sidedoor carries on from here."), 1)

    def test_the_hold_opening_again_after_done_brings_the_view_and_the_instruction_back(self):
        folder = self.attached()
        form_check_hold.finish(folder)
        self.assertTrue(form_check_hold.user_done(folder))
        later = desktop_adapter.later(applications.stamp(), 30)                # the pass expired: the AI holds again
        form_check_hold.hold_file(folder).write_text(json.dumps({"held": 20.0, "began": later}))
        self.assertFalse(form_check_hold.user_done(folder))
        with mock.patch.object(live_view, "session_page", return_value="https://careers.example.com/jobs/1001/apply"):
            self.assertIn("/live-view/", self.poll())
            card = self.card()
        self.assertIn("access-warning", card)
        self.assertIn(">Do the human check</button>", card)

    def test_the_ai_resuming_without_a_done_press_puts_the_card_in_the_same_done_state(self):
        folder = self.attached()
        form_check_hold.mark_done(folder)                                      # the view closed by itself (the AI resumed)
        with mock.patch.object(live_view, "session_page", return_value="https://careers.example.com/jobs/1001/apply"):
            self.assertNotIn("live-view", self.poll())
            card = self.card()
        self.assertIn('<p class="access-need note note-success">Done. Sidedoor carries on from here.', card)
        self.assertNotIn("</button>", card)
        self.assertNotIn("access-warning", card)

    def test_when_the_session_ends_submitted_the_card_goes(self):
        folder = self.attached()
        form_check_hold.finish(folder)
        with self.database:
            attended_submit.ended(self.database, KEY, self.request_id(), self.now)
            self.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (KEY,))
        self.assertIn('hx-get="/access/section?at=approvals"', self.poll())    # the poll draws the section again
        self.assertIsNone(self.card())

    def test_when_the_session_ends_without_a_submit_the_card_returns_in_its_ordinary_state(self):
        folder = self.attached()
        form_check_hold.finish(folder)
        with self.database:
            attended_submit.ended(self.database, KEY, self.request_id(), self.now)
            self.database.execute("UPDATE application_attempts SET state='CONFIRMED_NOT_SUBMITTED' WHERE job_key=?", (KEY,))
            self.database.execute("UPDATE desktop_requests SET state='CONSUMED' WHERE job_key=?", (KEY,))
            self.database.execute("UPDATE application_jobs SET state='RETRY_WAIT',attempts=0 WHERE job_key=?", (KEY,))
            self.assertEqual(access_outcomes.record(self.database, self.row(KEY), "CAPTCHA_REQUIRED", self.now,
                                                    result={"blocked": {"human_check": CHECK}}), "WAITING")
        self.assertIn('hx-get="/access/section?at=approvals"', self.poll())
        card = self.card()
        self.assertIn("The site needs a human check.", card)
        self.assertIn(">Do the human check</button>", card)
        self.assertIn("access-warning", card)
        self.assertNotIn("Done. Sidedoor carries on", card)

    def test_a_check_that_gates_the_site_keeps_its_present_card_with_no_red_line(self):
        with self.database:
            self.database.execute("UPDATE application_access_waits SET human_check=?",
                                  (json.dumps({"place": "BEFORE_FORM", "after_question": None, "kind": "OTHER", "form_readable": True}),))
        card = self.card()
        self.assertIn(">Do the human check</button>", card)
        self.assertNotIn("access-warning", card)


if __name__ == "__main__":
    unittest.main()
