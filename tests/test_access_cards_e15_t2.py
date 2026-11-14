"""Needs you cards for applications waiting on a sign-in or a human check (E15.T2), by outcome:
what the user sees on Overview, Approvals and Applications, and what each button leaves behind."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import html
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import access_cards
import access_outcomes
import applications
import applications_page
import desktop_adapter
import overview_page
import setup_steps
import user_step
from tests.test_dashboard_overview import CONFIG, PORT, SECRET, database_with_schema, finished_home

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
SIGN_IN_WORDS = "The site needs you to sign in."
CHECK_WORDS = "The site needs a human check."


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class AccessCardsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        finished_home(self.home)
        self.database = database_with_schema(self.root)
        self.addCleanup(self.database.close)
        desktop_adapter.initialize(self.database)
        self.database.commit()
        self.now = applications.stamp()
        chrome = mock.patch.object(setup_steps, "chrome_path", return_value="/chrome")
        chrome.start()
        self.addCleanup(chrome.stop)
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=CONFIG,
                                   browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    # --- fixtures ---------------------------------------------------------------------------------

    def job(self, key, company, role, site="jobs.example.com"):
        payload = json.dumps({"company": company, "role": role, "url": "https://%s/%s" % (site, key)})
        self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                              "VALUES (?,?,?,?,'QUEUED',?)", (key, company.lower(), key, payload, self.now))
        self.database.commit()
        return key

    def row(self, key):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key,)).fetchone())

    def wall(self, key, code="AUTHENTICATION_REQUIRED", reason="SIGN_IN"):
        """The job's own session stopped on a sign-in or a human check. A sign-in is recorded under the
        SIGN_IN reason these cards were built on (join with E15.T6: an account wall met today waits as
        ASK_EACH_TIME, covered in tests/test_e15_join.py; `reason=None` records it that way)."""
        with self.database:
            self.assertEqual(access_outcomes.record(
                self.database, self.row(key), code, self.now,
                reason=reason if access_outcomes.WALL_REASONS.get(code) == "SIGN_IN" else None), "WAITING")

    def behind(self, key):
        """The job is held behind its site's waiting sign-in (the cost rule)."""
        with self.database:
            self.assertTrue(access_outcomes.wait_on_host(self.database, self.row(key), self.now))

    def site_group(self):
        """Stripe met the sign-in first; two more jobs on that site are held behind it."""
        self.wall(self.job("lead", "Stripe", "Staff Engineer"))
        self.behind(self.job("second", "Stripe", "Data Engineer"))
        self.behind(self.job("third", "Stripe", "Platform Engineer"))

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def post(self, path, key, at="approvals", headers=HTMX):
        response = self.client.post(path, data={"job_key": key, "at": at}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def step_available(self):
        """The live view has landed: the user's step can be shown."""
        return mock.patch.object(user_step, "available", return_value=True)

    def needs(self):
        return overview_page.needs_you(overview_page.Reader(self.database), dt.datetime.now(dt.timezone.utc))

    def cards(self, page):
        return re.findall(r'<article class="approval-card access-card".*?</article>', page, re.S)

    def app_job(self, key):
        view = applications_page.gather(self.database, CONFIG, self.home)
        return next(item for item in view["list"]["jobs"] if item["key"] == key)

    # --- the cards --------------------------------------------------------------------------------

    def test_a_waiting_job_is_one_card_on_overview_and_approvals(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        # Round 6: a card says its deadline as a date and a time ("Nov 19 at 10:00"), whatever day it is.
        deadline = access_cards.deadline_words(overview_page._utc(access_outcomes.open_wait(self.database, "one")["deadline"]),
                                               overview_page.ZoneInfo(CONFIG["timezone"]))
        for path in ("/", "/approvals"):
            [card] = self.cards(self.page(path))
            for shown in ("Stripe", "Staff Engineer", SIGN_IN_WORDS, "Waits for you until %s, then closes." % deadline,
                          "You can also apply yourself.", 'href="https://jobs.example.com/one"', "Abandon",
                          "I've signed in", 'action="/access/done"'):   # owner, 2026-11-14: a sign-in or human-check card always has its done button
                self.assertIn(shown, card, path)
            # Where Sidedoor cannot show the sign-in itself, the card offers: I've signed in, apply yourself, or Abandon.
            for absent in ('action="/access/step"', "Sidedoor's browser", "/settings#connections"):
                self.assertNotIn(absent, card, path)

    def test_a_wait_made_without_a_session_result_has_its_done_button_and_abandon_works(self):   # owner, 2026-11-14: "Fix"
        import access_step
        key = self.job("crowd:r1", "CrowdStrike", "Engineer", "crowd.wd5.myworkdayjobs.com")
        with self.database:   # the read cap's path: a step that never ran is settled, and the wall is recorded with no result
            access_outcomes.set_account_setting(self.database, "SIGN_IN", self.now)
            access_outcomes.record(self.database, self.row(key), "AUTHENTICATION_REQUIRED", self.now, result={"blocked": {"code": "AUTHENTICATION_REQUIRED"}})
            access_step.settle_unrun(self.database, key, self.now)
            self.assertEqual(access_outcomes.record(self.database, self.row(key), "AUTHENTICATION_REQUIRED", self.now), "WAITING")
            self.database.execute("UPDATE application_access_waits SET reason='SIGN_IN' WHERE job_key=?", (key,))
        with self.step_available(), mock.patch.object(user_step, "openable", return_value=False):   # its step cannot be opened
            [card] = self.cards(self.page("/approvals"))
            self.assertNotIn('action="/access/step', card)
            for shown in (SIGN_IN_WORDS, ">I've signed in</button>", ">Abandon</button>", "Waits for you until "):
                self.assertIn(shown, card)
            # A press that fails says so on the card, never nothing.
            with mock.patch.object(access_cards, "abandon", side_effect=RuntimeError("anything")):
                failed = self.post("/access/abandon", key)
            self.assertIn("That did not go through. Reload the page and try again.", failed)
            self.assertEqual(self.row(key)["state"], "BLOCKED")
            self.assertIn(access_cards.ABANDONED, self.post("/access/abandon", key))
        self.assertEqual((self.row(key)["state"], self.row(key)["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))

    def test_a_site_group_is_one_card_with_its_held_jobs_and_one_action(self):
        self.site_group()
        for path in ("/", "/approvals"):
            [card] = self.cards(self.page(path))
            self.assertLess(card.index("Staff Engineer"), card.index("Also waiting on this site:"))
            self.assertLess(card.index("Also waiting on this site:"), card.index("Data Engineer"))
            self.assertIn("Platform Engineer", card)
            self.assertEqual(card.count('action="/access/done"'), 1)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
        with self.step_available():
            [card] = self.cards(self.page("/approvals"))
            self.assertEqual((card.count("I've signed in"), card.count('action="/access/done"'), card.count(">Sign in</button>")), (1, 1, 1))
            self.assertNotIn(access_outcomes.HOST_HELD_WORDS, card)   # the card says what the lead needs

    def test_human_checks_are_never_grouped(self):
        self.wall(self.job("a", "Stripe", "Staff Engineer"), "CAPTCHA_REQUIRED")
        self.wall(self.job("b", "Stripe", "Data Engineer"), "CAPTCHA_REQUIRED")
        cards = self.cards(self.page("/approvals"))
        self.assertEqual(len(cards), 2)
        for card in cards:
            self.assertIn(CHECK_WORDS, card)
            self.assertIn("You can also apply yourself.", card)
            self.assertEqual(card.count(">I've done the check</button>"), 1)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
            self.assertNotIn("Sidedoor's browser", card)
            self.assertNotIn("Also waiting on this site:", card)

    def test_needs_you_counts_each_waiting_job_once(self):
        self.site_group()
        self.wall(self.job("check", "Acme", "Designer", "acme.example.com"), "CAPTCHA_REQUIRED")
        self.job("owner", "Acme", "Analyst", "acme.example.com")
        self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='DESIGNATED_RESUME_MISMATCH' WHERE job_key='owner'")
        self.database.commit()
        needs = self.needs()
        # C7: Needs you counts the cards shown (the site's three jobs are one card, the human check another).
        self.assertEqual((needs["sign_ins"], needs["applications"], len(overview_page.waiting_keys(overview_page.Reader(self.database)))), (2, 1, 4))
        reader = overview_page.Reader(self.database)
        now = dt.datetime.now(dt.timezone.utc)
        self.assertFalse(set(overview_page.waiting_keys(reader)) & set(overview_page.blocked_on_owner_keys(reader, now)))
        self.assertFalse(set(overview_page.waiting_keys(reader)) & set(overview_page.retrying_keys(reader, now)))
        # Applications' "Blocked, needs you" filter is the two Overview numbers together, and lists those jobs.
        view = applications_page.gather(self.database, CONFIG, self.home, show="needs_you")
        # Owner, 2026-11-14 (E15.T14 L4): the two jobs only waiting behind the lead's card are In progress, not here.
        self.assertEqual(view["counts"]["needs_you"], needs["applications"] + 2)   # Applications lists and counts jobs
        self.assertEqual(sorted(item["key"] for item in view["list"]["jobs"]), ["check", "lead", "owner"])
        self.assertEqual(view["counts"]["retrying"], 0)
        page = self.page("/")
        self.assertIn("sign-ins or human checks to do", page)
        self.assertNotIn("Nothing needs you right now.", page)

    # --- the buttons ------------------------------------------------------------------------------

    def test_abandon_closes_the_job_and_applications_offers_try_again(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        answer = self.post("/access/abandon", "one")
        self.assertEqual(self.cards(answer), [])
        self.assertIn(access_cards.ABANDONED, answer)
        self.assertEqual((self.row("one")["state"], self.row("one")["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))
        self.assertEqual(self.needs()["sign_ins"], 0)
        page = self.page("/applications")
        self.assertIn("You abandoned this application.", page)
        self.assertIn('action="/access/try-again"', page)
        self.assertIn(">Try again</button>", page)
        # A second click on the same card: nothing left to do, said plainly.
        self.assertIn(access_cards.GONE, self.post("/access/abandon", "one"))
        self.assertEqual(self.row("one")["state"], "ABANDONED")

    def test_abandon_on_overview_redraws_needs_you_with_its_count(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        self.wall(self.job("two", "Acme", "Designer", "acme.example.com"))
        answer = self.post("/access/abandon", "one", at="overview")
        self.assertIn('id="overview-needs"', answer)
        self.assertEqual(len(self.cards(answer)), 1)
        self.assertRegex(answer, r'<span class="overview-number">1</span>\s*<span class="overview-needs-what">sign-in or human check to do')

    def test_abandoning_the_lead_frees_the_jobs_held_behind_it(self):
        self.site_group()
        answer = self.post("/access/abandon", "lead")
        self.assertEqual(self.cards(answer), [])
        self.assertEqual([self.row(key)["state"] for key in ("lead", "second", "third")], ["ABANDONED", "RETRY_WAIT", "RETRY_WAIT"])
        self.assertEqual(self.app_job("second")["todo"], access_outcomes.HOST_RELEASED_WORDS)

    def test_a_held_job_can_be_abandoned_alone_and_a_plain_post_answers_with_the_page(self):
        self.site_group()
        answer = self.post("/access/abandon", "third")
        [card] = self.cards(answer)
        self.assertIn("Data Engineer", card)
        self.assertNotIn("Platform Engineer", card)
        self.assertEqual([self.row(key)["state"] for key in ("lead", "second", "third")], ["BLOCKED", "BLOCKED", "ABANDONED"])
        # Without htmx: Overview answers with the whole page and what happened; Approvals goes back to its section.
        page = self.post("/access/abandon", "second", at="overview", headers=ORIGIN)
        self.assertIn(access_cards.ABANDONED, page)
        self.assertIn("<h1", page)
        # Approvals too, like its other cards' actions: a refusal is not lost.
        self.database.execute("UPDATE application_access_waits SET reason='PASSKEY' WHERE job_key='lead'")
        self.database.commit()
        refused = self.post("/access/done", "lead", headers=ORIGIN)
        self.assertIn("<h1", refused)
        self.assertIn(access_cards.NOT_OFFERED, refused)
        self.assertEqual(self.row("lead")["state"], "BLOCKED")   # a passkey card offers no done button, so none is honoured
        self.database.execute("UPDATE application_access_waits SET reason='SIGN_IN' WHERE job_key='lead'")
        self.database.commit()
        with self.step_available():
            page = self.post("/access/done", "lead", headers=ORIGIN)
        self.assertIn(access_cards.RELEASED_ONE, page)
        self.assertEqual(self.row("lead")["state"], "RETRY_WAIT")

    def test_the_last_holder_leaving_frees_every_job_held_behind_the_site(self):
        """`second` and `third` are held behind `lead`; `lead2` meets the same sign-in itself."""
        self.site_group()
        self.wall(self.job("lead2", "Stripe", "Second Lead"))
        with self.step_available():
            [card] = self.cards(self.page("/approvals"))
        # The job that met the sign-in itself has its own deadline on its line; a held job has none.
        self.assertEqual(card.count("Waits for you until "), 2)
        self.assertRegex(card, r"Second Lead</span> <span class=\"muted\">Waits for you until .*?, then closes\.</span>")
        self.assertNotRegex(card, r"Data Engineer</span> <span class=\"muted\">Waits")
        self.post("/access/abandon", "lead")
        self.assertEqual([self.row(key)["state"] for key in ("second", "third", "lead2")], ["BLOCKED"] * 3)   # lead2 still holds the site
        answer = self.post("/access/abandon", "lead2")
        self.assertEqual(self.cards(answer), [])
        self.assertEqual([self.row(key)["state"] for key in ("second", "third")], ["RETRY_WAIT", "RETRY_WAIT"])
        self.assertEqual(self.needs()["sign_ins"], 0)
        self.assertNotIn(access_outcomes.HOST_HELD_WORDS, self.page("/applications") + self.page("/approvals") + self.page("/"))

    def test_done_never_releases_a_job_whose_submission_is_uncertain(self):
        self.wall(self.job("sent", "Acme", "Designer"))
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,armed_at,result,attempt_id) VALUES ('armed','b','sent','submit','{}','CONSUMED',?,?,?,'{}','gone')",
                              (self.now, self.now, self.now))
        self.database.commit()
        before = self.row("sent")
        with self.step_available():
            answer = self.post("/access/done", "sent")
        self.assertIn(access_cards.NOT_ABANDONED, answer)
        self.assertEqual(len(self.cards(answer)), 1)
        self.assertEqual((self.row("sent"), access_outcomes.open_wait(self.database, "sent")["state"]), (before, "WAITING"))

    def armed_submit(self, key):
        """An armed submit on record, its attempt unsettled: it may have reached the employer."""
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,armed_at,result,attempt_id) VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?,'{}',?)",
                              ("armed-" + key, key, self.now, self.now, self.now, "gone-" + key))
        self.database.commit()

    def test_an_uncertain_job_on_a_site_card_stays_put_when_the_lead_is_done_or_abandoned(self):
        for path, lead_state, said in (("/access/done", "RETRY_WAIT", access_cards.RELEASED_MANY % 2 + " " + access_cards.STAYS_ONE),
                                       ("/access/abandon", "ABANDONED", access_cards.ABANDONED)):
            with self.subTest(path), self.step_available():
                site = path.rsplit("/", 1)[1] + ".example.com"
                names = [self.job(path + name, "Stripe", name, site) for name in ("lead", "second", "third")]
                self.wall(names[0])
                self.behind(names[1])
                self.behind(names[2])
                self.armed_submit(names[1])
                before = (self.row(names[1]), access_outcomes.open_wait(self.database, names[1]))
                answer = self.post(path, names[0])
                self.assertIn(said, answer)   # the count never includes the job that stayed
                self.assertEqual([self.row(name)["state"] for name in names], [lead_state, "BLOCKED", "RETRY_WAIT"])
                self.assertEqual((self.row(names[1]), access_outcomes.open_wait(self.database, names[1])), before)

    def test_done_on_a_card_led_by_an_uncertain_job_releases_the_others_and_says_so(self):
        self.site_group()
        self.armed_submit("lead")
        before = (self.row("lead"), access_outcomes.open_wait(self.database, "lead"))
        with self.step_available():
            answer = self.post("/access/done", "lead")
        self.assertIn("Sidedoor tries these 2 applications on its next round. One application stays open "
                      "while Sidedoor checks whether it reached the employer.", answer)
        self.assertEqual((self.row("lead"), access_outcomes.open_wait(self.database, "lead")), before)
        self.assertEqual([self.row(key)["state"] for key in ("second", "third")], ["RETRY_WAIT", "RETRY_WAIT"])

    def test_reasons_a_done_button_cannot_settle_have_none(self):
        """Ask each time, single sign-on asking for more, the step that could not be shown, and an
        account reason this page does not list yet: apply yourself and Abandon; Sign in only for
        "ask each time", and only once the user's step is available."""
        reasons = ("ASK_EACH_TIME", "SSO_SCOPE", "STEP_NOT_SHOWN", "PASSKEY")
        for number, reason in enumerate(reasons):
            key = self.job("r%d" % number, "Acme %d" % number, "Designer", "site%d.example.com" % number)
            self.wall(key)
            self.database.execute("UPDATE application_access_waits SET reason=? WHERE job_key=?", (reason, key))
        self.database.commit()
        with mock.patch.object(access_outcomes, "ACCOUNT_REASONS", access_outcomes.ACCOUNT_REASONS | {"PASSKEY"}):
            for available in (False, True):
                with mock.patch.object(user_step, "available", return_value=available):
                    cards = self.cards(self.page("/approvals"))
                    self.assertEqual(len(cards), 4)
                    for number, card in enumerate(cards):
                        # E15 join: the passkey card's own sentence (E15.T6) already says to apply
                        # yourself, so it has the posting link without the second sentence.
                        # F5: "also" only where the card offers a step; a card with nothing else to do
                        # says "Apply yourself if you want this job." (the passkey card's own sentence does).
                        # Owner, 2026-11-14: "ask each time" is a sign-in card, so it always has I've signed in.
                        asks = reasons[number] == "ASK_EACH_TIME"
                        self.assertEqual("You can also apply yourself." in card, asks)
                        self.assertEqual(card.count("Apply yourself if you want this job."), 0 if asks else 1)
                        self.assertIn(">Open the posting</a>", card)
                        self.assertIn(">Abandon</button>", card)
                        self.assertEqual(card.count('action="/access/done'), 1 if asks else 0)
                        self.assertNotIn("Try again now", card)
                        self.assertEqual('action="/access/step' in card, available and reasons[number] == "ASK_EACH_TIME", reasons[number])
                    for number in range(1, 4):   # and the server honours no done the card does not offer
                        self.assertIn(access_cards.NOT_OFFERED, self.post("/access/done", "r%d" % number))
        self.assertEqual([self.row("r%d" % number)["state"] for number in range(1, 4)], ["BLOCKED"] * 3)
        self.assertIn(access_cards.RELEASED_ONE, self.post("/access/done", "r0"))   # "ask each time" has I've signed in
        self.assertEqual(self.row("r0")["state"], "RETRY_WAIT")

    def test_the_sign_ins_chip_counts_the_waiting_jobs_and_all_includes_them(self):
        chip = lambda page, words: int(re.search(r">%s <span class=\"app-count\">(\d+)</span>" % words, page).group(1))
        shown = lambda page: [name for name in ("messages", "applications", "sign-ins", "questions", "notes", "notices")
                              if 'id="%s"' % name in page]
        self.site_group()
        self.wall(self.job("check", "Acme", "Designer", "acme.example.com"), "CAPTCHA_REQUIRED")
        everything, own, other = self.page("/approvals"), self.page("/approvals?label=sign-ins"), self.page("/approvals?label=questions")
        self.assertEqual((chip(everything, "Sign-ins and human checks"), chip(everything, "All"), self.needs()["sign_ins"]), (2, 2, 2))   # D6: the chip and Needs you count the same cards
        self.assertIn("sign-ins", shown(everything))
        self.assertEqual((shown(own), len(self.cards(own))), (["sign-ins"], 2))
        self.assertEqual(shown(other), ["questions"])
        self.assertNotIn("Send these", own)
        # Its forms keep the label, and an action's answer brings the chip row with its new counts.
        self.assertIn('action="/access/abandon?label=sign-ins"', own)
        response = self.client.post("/access/abandon?label=sign-ins", data={"job_key": "check", "at": "approvals"}, headers=HTMX)
        answer = html.unescape(response.text)
        self.assertIn('hx-swap-oob="true"', answer)
        self.assertEqual((chip(answer, "Sign-ins and human checks"), chip(answer, "All")), (1, 1))
        self.assertIn('action="/access/abandon?label=sign-ins"', answer)
        # A plain post keeps the page's place; under a label that hides the section its answer is at the top.
        plain = html.unescape(self.client.post("/access/abandon?label=sign-ins&notes_page=2", data={"job_key": "third", "at": "approvals"},
                                               headers=ORIGIN).text)
        self.assertEqual((shown(plain), access_cards.ABANDONED in plain), (["sign-ins"], True))
        hidden = html.unescape(self.client.post("/access/abandon?label=questions", data={"job_key": "second", "at": "approvals"},
                                                headers=ORIGIN).text)
        self.assertEqual((shown(hidden), access_cards.ABANDONED in hidden), (["questions"], True))

    def test_an_unknown_reason_has_neutral_words_and_no_done_button(self):
        self.wall(self.job("odd", "Acme", "Designer"))
        self.wall(self.job("mail", "Stripe", "Staff Engineer", "stripe.example.com"))
        self.database.execute("UPDATE application_access_waits SET reason='SOMETHING_NEW' WHERE job_key='odd'")
        self.database.execute("UPDATE application_access_waits SET reason='CONFIRMATION_EMAIL_MISSING' WHERE job_key='mail'")
        self.database.commit()
        with self.step_available():
            mail, odd = sorted(self.cards(self.page("/approvals")), key=lambda card: "Acme" in card)
            self.assertIn("This application is waiting for you.", odd)
            self.assertIn("Apply yourself if you want this job.", odd)
            self.assertIn("You can also apply yourself.", mail)   # it offers "I've confirmed it"
            for absent in (SIGN_IN_WORDS, "Try again now", 'action="/access/done"', 'action="/access/step"'):
                self.assertNotIn(absent, odd)
            self.assertIn(access_cards.NOT_OFFERED, self.post("/access/done", "odd"))
        self.assertEqual(self.row("odd")["state"], "BLOCKED")
        self.assertTrue(self.app_job("odd")["todo"].startswith("This application is waiting for you. Closes on "))
        # The email confirmation is the user's own to do, so its done button stays, with no browser link.
        for shown in ("The site's confirmation email did not arrive. Confirm it yourself.", ">I've confirmed it</button>",
                      "Ask the site to send it again, or confirm your account on the site. Then press I've confirmed it."):
            self.assertIn(shown, mail)
        self.assertNotIn("/settings#connections", mail)

    def test_done_releases_the_whole_site_group(self):
        self.site_group()
        step = self.step_available()
        step.start()
        self.addCleanup(step.stop)
        answer = self.post("/access/done", "lead")
        self.assertEqual(self.cards(answer), [])
        self.assertIn(access_cards.RELEASED_MANY % 3, answer)
        for key in ("lead", "second", "third"):
            self.assertEqual((self.row(key)["state"], self.row(key)["blocker"]), ("RETRY_WAIT", None), key)
        self.assertEqual(self.needs()["sign_ins"], 0)
        self.assertIn(access_cards.GONE, self.post("/access/done", "lead"))   # a double click

    def test_try_again_reopens_once_and_refuses_an_uncertain_submission(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        self.wall(self.job("sent", "Acme", "Designer", "acme.example.com"))
        self.post("/access/abandon", "one")
        self.post("/access/abandon", "sent")
        self.assertIn(access_cards.REOPENED, self.post("/access/try-again", "one"))
        self.assertEqual(self.row("one")["state"], "QUEUED")
        again = self.post("/access/try-again", "one")
        self.assertIn("This application is not closed.", again)
        reopened = self.database.execute("SELECT COUNT(*) FROM application_events WHERE job_key='one' AND event='APPLICATION_REOPENED'")
        self.assertEqual(reopened.fetchone()[0], 1)
        # An armed submit on record, its attempt unsettled: it may have reached the employer.
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,armed_at,result,attempt_id) VALUES ('armed','b','sent','submit','{}','CONSUMED',?,?,?,'{}','gone')",
                              (self.now, self.now, self.now))
        self.database.commit()
        refused = self.post("/access/try-again", "sent")
        self.assertIn("This application may already have reached the employer, so Sidedoor will not apply again.", refused)
        self.assertEqual(self.row("sent")["state"], "ABANDONED")
        # Without htmx the page itself carries the words.
        self.assertIn("may already have reached the employer", self.post("/access/try-again", "sent", headers=ORIGIN))

    def test_a_forged_job_key_changes_nothing(self):
        self.job("queued", "Acme", "Designer")
        self.job("owner", "Acme", "Analyst")
        self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='DESIGNATED_RESUME_MISMATCH' WHERE job_key='owner'")
        self.database.commit()
        before = [self.row(key) for key in ("queued", "owner")]
        for path in ("/access/abandon", "/access/done", "/access/step", "/access/try-again"):
            for key in ("queued", "owner", "no-such-job", ""):
                answer = self.post(path, key)
                self.assertNotIn(access_cards.ABANDONED, answer)
                self.assertNotIn(access_cards.REOPENED, answer)
        self.assertEqual([self.row(key) for key in ("queued", "owner")], before)
        # And the dashboard's own Origin check still stands in front of every one of them.
        self.assertEqual(self.client.post("/access/abandon", data={"job_key": "queued"}).status_code, 403)

    # --- the two seams ----------------------------------------------------------------------------

    def test_step_buttons_show_only_while_the_users_step_is_available(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        self.wall(self.job("two", "Acme", "Designer", "acme.example.com"), "CAPTCHA_REQUIRED")
        for path in ("/", "/approvals"):
            page = self.page(path)
            self.assertNotIn('action="/access/step"', page)
            self.assertNotIn(">Sign in</button>", page)
            self.assertNotIn("Do the human check", page)
            self.assertEqual(page.count('action="/access/done"'), 2)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
        self.assertIn(access_cards.STEP_UNAVAILABLE, self.post("/access/step", "one"))
        with mock.patch.object(user_step, "available", return_value=True), \
                mock.patch.object(user_step, "start", return_value={"available": True, "html": "<b>live view</b>"}) as start:
            for path in ("/", "/approvals"):
                page = self.page(path)
                self.assertIn(">Sign in</button>", page)
                self.assertIn(">Do the human check</button>", page)
                self.assertIn(">I've signed in</button>", page)
            self.assertEqual(self.post("/access/step", "two"), "<b>live view</b>")
            start.assert_called_once_with("two", user_step.HUMAN_CHECK)

    def test_the_setting_link_shows_only_for_the_two_other_values(self):
        self.wall(self.job("one", "Stripe", "Staff Engineer"))
        self.wall(self.job("two", "Acme", "Designer", "acme.example.com"), "CAPTCHA_REQUIRED")
        # The owner's pick (restored, F8a): every account card links to the setting at its real control
        # (Settings > Applying), whatever the setting says, except "for me" once Sidedoor can do that.
        # The sentence about Sidedoor signing in goes with it only once that choice is offered.
        self.assertEqual(access_cards.SETTING_HREF, "/settings#account-setting")
        self.assertIn('id="account-setting"', self.page("/settings"))
        for can in (False, True):
            with mock.patch.object(access_outcomes, "resolver_available", return_value=can):
                page = self.page("/approvals")
                self.assertEqual(page.count('>Change "When a site needs an account" in Settings</a>'), 1)
                self.assertEqual(page.count(access_cards.SETTING_WORDS), 1 if can else 0)
        for can, value, shown in ((False, None, 1), (False, "ASK", 1), (False, "SKIP", 1), (False, "SIGN_IN", 1),
                                  (True, None, 1), (True, "SIGN_IN", 0), (True, "ASK", 1), (True, "SKIP", 1)):
            with mock.patch.object(access_outcomes, "account_setting", return_value=value), \
                    mock.patch.object(access_outcomes, "resolver_available", return_value=can):
                for path in ("/", "/approvals"):
                    # Once: on the sign-in card, never on the human check. (The card's own link: since
                    # E15.T10 this finished install with no saved choice also shows the release banner,
                    # whose button leads to the same place.)
                    self.assertEqual(self.page(path).count('<a href="%s">' % access_cards.SETTING_HREF), shown, (can, value, path))

    # --- Applications' words ----------------------------------------------------------------------

    def test_applications_says_what_a_waiting_held_and_released_job_waits_for(self):
        self.site_group()
        lead, held = self.app_job("lead"), self.app_job("second")
        self.assertEqual((lead["state"], lead["group"], lead["problem"], lead["next_at"]), ("Blocked, needs you", "needs_you", None, None))
        self.assertTrue(lead["todo"].startswith(SIGN_IN_WORDS + " Waits for you until "))   # its card has I've signed in
        self.assertTrue(lead["todo"].endswith(", then closes."))
        with self.step_available():
            todo = self.app_job("lead")["todo"]
            self.assertTrue(todo.startswith(SIGN_IN_WORDS + " Waits for you until ") and todo.endswith(", then closes."), todo)
        # F5: no sign-in step on this computer, so the held job does not say one is waiting.
        # Owner, 2026-11-14 (E15.T14 L4): it only waits behind the lead's card, so it is "Waiting", In progress.
        self.assertEqual((held["state"], held["group"], held["problem"], held["todo"], held["next_at"]),
                         ("Waiting", "progress", None, access_cards.HELD_NO_STEP_WORDS, None))
        with self.step_available():
            self.assertEqual(self.app_job("second")["todo"], access_outcomes.HOST_HELD_WORDS)
        reader = overview_page.Reader(self.database)
        history = [line["text"] for line in applications_page.history(reader, "second")]
        self.assertIn("Held: " + access_cards.HELD_NO_STEP_WORDS, history)
        self.assertEqual(applications_page.history(reader, "lead")[0]["text"], "Held: The application site asked for a sign-in.")
        shown = json.dumps([lead, held, history], default=str)
        for wrong in ("Waiting to retry", "Will try again automatically", "Next attempt"):
            self.assertNotIn(wrong, shown + self.page("/applications"))
        self.assertNotIn("asked for a sign-in", json.dumps([held, history], default=str))
        self.post("/access/abandon", "lead")
        self.assertEqual((self.app_job("second")["problem"], self.app_job("second")["todo"]), (None, access_outcomes.HOST_RELEASED_WORDS))

    def test_no_page_shows_a_code_an_em_dash_or_automation(self):
        self.site_group()
        self.wall(self.job("check", "Acme", "Designer", "acme.example.com"), "CAPTCHA_REQUIRED")
        self.wall(self.job("gone", "Acme", "Analyst", "other.example.com"), "MFA_REQUIRED")
        self.post("/access/abandon", "gone")
        feature = set(access_outcomes.WAIT_REASONS) | set(access_outcomes.WALL_REASONS) | set(access_outcomes.CLOSE_CODES) \
            | set(access_outcomes.CLOSE_REASONS) | {"WAITING", "host_released"}
        with mock.patch.object(user_step, "available", return_value=True), \
                mock.patch.object(access_outcomes, "account_setting", return_value="ASK"):
            pages = [self.page(path) for path in ("/", "/approvals", "/applications", "/applications?job=lead",
                                                  "/applications?job=second", "/applications?job=gone")]
        pages += [self.post("/access/done", "check", at="overview"), self.post("/access/try-again", "gone")]
        for page in pages:
            text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", text)
            self.assertFalse(feature & set(RAW_CODE.findall(text)) | {word for word in feature if re.search(r"\b%s\b" % word, text)})
            self.assertNotIn("—", text)
            self.assertNotIn("automation", text.lower())


if __name__ == "__main__":
    unittest.main()
