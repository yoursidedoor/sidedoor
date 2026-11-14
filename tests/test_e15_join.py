"""E15 joined: a job meets a sign-in or a human check -> a card under Needs you -> the card's button opens
the live view of Sidedoor's browser -> the user does their step and presses Done -> the site's session is
saved -> the job, and the jobs held behind that site, are released -> the next read starts with the
session back; and the tray count and the desktop notification include the card.

Fakes only: the desktop adapter's request queue for application sessions, tests/fake_live_guard.py and a
scripted DevTools server for the live view (tests/live_view_fakes.py), a cookie jar for site sessions. The
dashboard is the app as it builds itself (create_app with its defaults, SIDEDOOR_HOME a temporary home).
No browser, no model, nothing off 127.0.0.1.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import datetime as dt
import hashlib
import html
import importlib.util
import json
import re
import time
import unittest
from pathlib import Path
from unittest import mock

import access_cards
import access_outcomes
import applications
import applications_page
import browser_runtime
import credentials
import desktop_adapter
import desktop_alerts
import live_view
import live_view_fakes as fakes
import notifications
import overview_page
import paths
import resources
import site_sessions
import user_step
from test_dashboard_overview import PORT, SECRET, database_with_schema, finished_home

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "websockets"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
SITE = "jobs.ashbyhq.com"
URL = "https://jobs.ashbyhq.com/synthetic/role-123"
SESSION_VALUE = "s3cr3t-signed-in-cookie-7c1e"
ASK_WORDS = "This site needs an account."
CHECK_WORDS = "The site needs a human check."
PASSKEY_WORDS = "This site signs in with a passkey. Apply yourself if you want this job."
PASSKEY_WALL = {"sign_in_offered": True, "second_step": "PASSKEY"}
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
INTERNAL = re.compile(r"\b(automation|wall|walls|host|hosts|guard|lease)\b", re.I)


def stop(code, wall=None):
    return {"blocked": dict({"code": code, "next_action": "The user does this step"}, **({"wall": wall} if wall else {}))}


def cookie(name, value, domain=SITE, expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "Lax"}


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx, websockets) are not installed")
class Flow(unittest.TestCase):
    def setUp(self):
        resume = b"synthetic resume; no real candidate data"
        self.policy = {
            "preapproved_applications": True, "max_attempts": 3, "max_reconcile_attempts": 3,
            "retry_delays_seconds": [900, 3600, 7200], "minimum_base_salary_usd": 190000,
            "funding_stages": ["Series B"], "industries": ["AI"], "levels": ["Staff Software Engineer"],
            "resume": {"path": "resume.pdf", "sha256": hashlib.sha256(resume).hexdigest()},
            "answers": {"email": {"semantic": "contact_email", "value": "candidate@example.invalid",
                                  "source": "synthetic test fixture"}}}
        self.home = profile_home.use(self, self.policy)       # SIDEDOOR_HOME: the app's own default home and root
        self.root = self.home
        (self.root / "resume.pdf").write_bytes(resume)
        self.config = {"application_adapter": "desktop", "applications_enabled": True, "execution_ownership_verified": True}
        (self.root / "config.json").write_text(json.dumps(self.config))
        override = mock.patch.object(resources, "OVERRIDE", self.root)
        override.start()
        self.addCleanup(override.stop)
        finished_home(self.home)
        credentials.clear_cache()
        site_sessions._reported.clear()
        self.database = database_with_schema(self.root)
        self.addCleanup(self.database.close)
        desktop_adapter.initialize(self.database)
        self.database.commit()
        self.assertEqual(paths.database(None), self.root / "state" / "orchestrator.sqlite3")
        self.now = applications.stamp()
        self.adapter = desktop_adapter.DesktopAdapter(self.database, self.root, self.config, lambda: self.now)
        same = mock.patch.object(applications, "same_posting_duplicate", return_value=False)
        same.start()
        self.addCleanup(same.stop)
        self.key = self.ingest("123", "Staff Software Engineer")
        # The live view as the app registers it, with a stand-in guard and Chrome.
        self.chrome = fakes.FakeChrome({"MAIN": {"targetId": "MAIN", "type": "page", "url": URL, "title": "Apply"}}).start()
        self.addCleanup(self.chrome.stop)
        self.cookies = self.root / "cookies-the-site-set.json"
        self.cookies.write_text(json.dumps([cookie("session", SESSION_VALUE), cookie("elsewhere", "x", "other.example.com")]))
        app = dashboard.create_app(SECRET, PORT)              # home=None, root=None, config=None: the installed path
        self.addCleanup(user_step.connect, None)
        self.view = app.state.live_view
        self.assertEqual(self.view.root, self.home)
        self.view.config = {"browser_driver": {"profile": str(self.root / "profile")}}
        self.guard_configs = []
        inner = fakes.guard_command(self.chrome)

        def guard_command(config, directory):
            self.guard_configs.append(config)
            site = (config or {}).get(site_sessions.CONFIG_KEY)
            return inner(config, directory) + (["--site-session", site, "--cookies", str(self.cookies)] if site else []) \
                + (["--page", self.ends_on] if site and self.ends_on else [])
        self.ends_on = None                                    # the page the browser is on when the step ends
        self.view.guard_command = guard_command
        self.supported = True
        self.view.is_supported = lambda config: self.supported
        self.view.leave_grace_seconds = 30
        self.stack = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client = self.stack.__enter__()                  # one event loop for the whole test, as in the app
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.client.get("/", params={"t": SECRET})

    # --- fixtures -----------------------------------------------------------------------------------

    def ingest(self, number, role="Senior Engineer"):
        with self.database:
            return applications.ingest(self.database, {
                "company": "Synthetic Company", "role": role, "requisition_id": "role-" + number,
                "url": "https://jobs.ashbyhq.com/synthetic/role-" + number}, self.now)

    def cycle(self):
        return applications.cycle(self.database, self.config, self.root, adapter=self.adapter, now=self.now)

    def advance(self, seconds=60):
        self.now = desktop_adapter.later(self.now, seconds)

    def claim(self):
        return desktop_adapter.claim(self.database, self.root, "join-fixture", self.now)

    def session(self, result, key=None):
        """One applications round starts a read for a job; the session ends with `result`; the next round
        records it. A wall is met in a session that `application_browser.consume` started, which makes the
        store's key first (`site_sessions.before_session`); the adapter stand-in does that here."""
        self.cycle()
        request = self.claim()
        self.assertIsNotNone(request, "no session was started")
        self.assertEqual(request["payload"]["operation"], "inspect")
        if key:
            self.assertEqual(request["payload"]["job"]["job_key"], key)
        self.assertIsNone(site_sessions.before_session(site_sessions.site_key(request["payload"]["url"])))
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=result), self.now)["accepted"])
        self.advance()
        self.cycle()
        return request

    def row(self, key=None):
        return dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key or self.key,)).fetchone())

    def waits(self, key=None):
        return [dict(row) for row in self.database.execute(
            "SELECT * FROM application_access_waits WHERE job_key=? ORDER BY id", (key or self.key,))]

    def open_waits(self):
        return {row[0] for row in self.database.execute("SELECT job_key FROM application_access_waits WHERE state='WAITING'")}

    def events(self, kind, key=None):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id", (key or self.key, kind))]

    def uncertain(self, key):
        """An armed submit request is on file for the job: its submission may have reached the employer."""
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                              "created_at,expires_at,armed_at,result,attempt_id) VALUES (?,?,?,'submit','{}',"
                              "'CONSUMED',?,?,?,'{}',?)", ("armed-" + key, "b-" + key, key, self.now, self.now, self.now, "gone-" + key))
        self.database.commit()
        self.assertTrue(applications.uncertain_submission(self.database, key))

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def cards(self, page):
        return re.findall(r'<article class="approval-card access-card".*?</article>', page, re.S)

    def post(self, path, key, at="approvals"):
        response = self.client.post(path, data={"job_key": key, "at": at}, headers=HTMX)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def press_step(self, key=None):
        """The card's Sign in / Do the human check: the block the card embeds, and the live view it loads."""
        block = self.post("/access/step", key or self.key)
        found = re.fullmatch(r'<div hx-get="(/live-view/([\w-]+)/fragment\?start=1&card=1)" hx-trigger="load" hx-target="this" hx-swap="outerHTML"></div>', block)
        self.assertIsNotNone(found, block)
        fragment = self.page(found.group(1))
        self.assertIn("data-live-view", fragment)
        self.assertIn("data-autostart", fragment)
        return found.group(2), fragment

    def start_live(self, target):
        answer = self.client.post(f"/live-view/{target}/start", headers=ORIGIN)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.addCleanup(lambda: self.client.post("/live-view/end?reason=cancel", headers=ORIGIN))
        return answer.json()

    def end_live(self, reason):
        answer = self.client.post("/live-view/end?reason=" + reason, headers=ORIGIN)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def step(self, key=None, reason="done"):
        target, _ = self.press_step(key)
        self.start_live(target)
        return self.end_live(reason)

    def store(self):
        return site_sessions._load(None)

    def needs(self):
        return overview_page.needs_you(overview_page.Reader(self.database), dt.datetime.now(dt.timezone.utc))

    def app_job(self, key=None):
        view = applications_page.gather(self.database, self.config, self.home)
        return next(item for item in view["list"]["jobs"] if item["key"] == (key or self.key))

    def assert_plain(self, *pages):
        for page in pages:
            text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", text)
            self.assertEqual(RAW_CODE.findall(text), [], text[:200])
            self.assertNotIn("—", text)
            self.assertNotIn("automation", text.lower())

    # --- the whole flow -----------------------------------------------------------------------------

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @mock.patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_wall_card_sign_in_done_saved_released_and_the_next_read_starts_signed_in(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held = self.ingest("456")                              # found after the site asked for the sign-in
        self.cycle()                                           # so it is held behind the site: no session
        self.assertIsNone(self.claim())
        self.assertEqual(self.open_waits(), {self.key, held})
        self.assertEqual(self.waits()[0]["reason"], "ASK_EACH_TIME")
        # One card for the site on Overview and on Approvals, with one Sign in.
        for path in ("/", "/approvals"):
            page = self.page(path)
            cards = self.cards(page)
            self.assertEqual(len(cards), 1, path)
            self.assertIn(ASK_WORDS, cards[0])
            self.assertIn("Senior Engineer", cards[0])
            self.assertEqual(cards[0].count('action="/access/step'), 1)
            self.assertIn(">Sign in</button>", cards[0])
            self.assertEqual(cards[0].count('action="/access/done'), 1)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
            self.assertNotIn("Try again now", cards[0])
            self.assertNotIn(access_cards.SETTING_WORDS, page)   # seam E: no link to a choice that is not offered
            self.assertIn("/static/live_view.js", page)           # the page runs the block the card embeds
            self.assertNotIn("Sidedoor's browser first", page)
        self.assertEqual(self.needs()["sign_ins"], 1)          # one card (C7), listing both jobs
        waiting = self.app_job()
        self.assertTrue(waiting["todo"].startswith(ASK_WORDS + " Waits for you until "), waiting["todo"])
        self.assertNotIn("Will try again automatically", json.dumps([waiting, self.app_job(held)], default=str))
        # Sign in: the live view opens the job's own page, for the wait's site, with that site's session in use.
        target, fragment = self.press_step()
        offered = self.view.target(target)
        self.assertEqual((offered.job, offered.url, offered.purpose, offered.domain), (self.key, URL, "sign_in", SITE))
        self.assertIs(offered.check, user_step._check)         # it only notices a job that stopped waiting (L4)
        self.assertNotIn("data-live-start", fragment)          # the card's own Sign in is the one control
        self.assertEqual(self.start_live(target)["state"], "live")
        self.assertEqual(self.guard_configs[-1][site_sessions.CONFIG_KEY], SITE)
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(self.open_waits(), {self.key, held})  # nothing moves while the user is there
        # Done: saved, then released.
        ended = self.end_live("done")
        self.assertEqual((ended["reason"], ended["words"]), ("done", "Done. Sidedoor carries on from here."))
        self.assertIsNone(self.view.session)
        self.assertTrue(site_sessions.has(SITE))
        entry = self.store()["sites"][SITE]
        self.assertEqual([item["name"] for item in entry["cookies"]], ["session"])   # the site's own, nothing else's
        sealed = (self.home / site_sessions.STORE_FILE).read_bytes()
        self.assertNotIn(SESSION_VALUE.encode(), sealed)                           # encrypted at rest
        self.assertEqual(self.store()["pending"], {})
        self.assertEqual(self.open_waits(), set())
        self.assertEqual({self.row(key)["state"] for key in (self.key, held)}, {"RETRY_WAIT"})
        self.assertEqual(self.events(user_step.FINISHED_EVENT),
                         [{"purpose": "sign_in", "site_session_saved": True, "released": 2}])
        self.assertNotIn(SESSION_VALUE, json.dumps([dict(row) for row in self.database.execute("SELECT * FROM application_events")]))
        for path in ("/", "/approvals"):
            self.assertEqual(self.cards(self.page(path)), [])
        self.assertEqual(self.needs()["sign_ins"], 0)
        # A page drawn before still shows the button: pressing it says the truth.
        self.assertIn(access_cards.GONE, self.post("/access/step", self.key))
        # The next applications round: each released job's read is on that site, and the guard of that
        # session puts the saved cookies back before any page work (site_sessions.restore, as
        # browser_guard.Proxy.restore_site_session calls it; Chrome had exited, so it holds none).
        seen, proceeded = [], set()
        for _ in range(8):
            self.cycle()
            request = self.claim()
            if request is None:
                self.advance()
                continue
            if request["payload"]["operation"] != "inspect":   # a read went through: the application goes on
                proceeded.add(request["payload"]["job"]["job_key"])
                break
            site = site_sessions.site_key(request["payload"]["url"])
            self.assertEqual(site, SITE)
            self.assertIsNone(site_sessions.before_session(site))          # live: no sign-in asked again without a session
            jar = FakeJar(request["payload"]["url"])
            self.assertEqual(site_sessions.restore(site, jar.call)["outcome"], "restored")
            self.assertEqual(jar.value("session"), SESSION_VALUE)
            job = self.row(request["payload"]["job"]["job_key"])
            seen.append(job["job_key"])
            self.assertTrue(desktop_adapter.complete(
                self.database, dict(request, result=self.review(job)), self.now)["accepted"])
            self.advance()
        self.assertEqual(sorted(seen), sorted([self.key, held]))
        self.assertTrue(proceeded and proceeded <= {self.key, held}, proceeded)
        self.assertEqual(self.open_waits(), set())             # the reads went through: no card comes back
        for key in (self.key, held):
            self.assertNotIn(self.row(key)["blocker"], access_outcomes.WALL_REASONS)
            self.assertEqual(len(self.waits(key)), 1)

    def review(self, job):
        payload = json.loads(job["payload"])
        evidence = {"observed_at": self.now, "verified": True, "source": "synthetic official evidence"}
        return dict(evidence, employer="synthetic company", requisition=job["requisition"],
                    official_url=payload["url"], role=payload["role"],
                    candidate_email=self.policy["answers"]["email"]["value"],
                    official_destination_verified=True, action_scope="application_only",
                    employee_count_evidence=dict(evidence, value="51-200"),
                    facts={"open": True, "funding_stage": "Series B", "industry": "AI",
                           "level": "Staff Software Engineer", "specialization": "backend",
                           "country": "US", "city": "Seattle", "work_mode": "remote",
                           "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                           "hiring_geography_includes_candidate": True,
                           "citizenship_clearance_requirements_met": True},
                    scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"},
                               "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
                    compensation=dict(evidence, currency="USD", minimum=200000, maximum=250000, period="annual_base"),
                    history=dict(evidence, job_key=job["job_key"], application=False, interview=False, referral="NONE"),
                    questions=[{"id": "email", "semantic": "contact_email", "required": True}],
                    all_required_questions_enumerated=True, form_revision="fixture-form-v1")

    def test_if_the_site_asks_again_the_card_comes_back(self):
        """Done is the user's statement. A site that was in fact not signed in asks again at the next read:
        a new wait and its card, never a silent close."""
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.step()
        self.assertEqual(self.open_waits(), set())
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        self.assertEqual([(wait["state"], wait["outcome"]) for wait in self.waits()], [("RESOLVED", "released"), ("WAITING", None)])
        self.assertEqual(len(self.cards(self.page("/approvals"))), 1)

    def test_cancel_leaves_everything_as_it_was(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        before = (self.row(), self.waits())
        target, _ = self.press_step()
        self.start_live(target)
        self.assertEqual(self.end_live("cancel")["words"], "Cancelled.")
        self.assertEqual((self.row(), self.waits()), before)
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(self.store()["pending"], {})          # what the guard kept is dropped, not left behind
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [])
        card = self.cards(self.page("/approvals"))
        self.assertEqual(len(card), 1)
        self.assertIn(">Sign in</button>", card[0])
        # The same step can be opened again.
        again, _ = self.press_step()
        self.assertEqual(again, target)

    def test_the_time_running_out_leaves_the_card(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        before = (self.row(), self.waits())
        self.view.lease_seconds = 0.3
        target, _ = self.press_step()
        self.start_live(target)
        deadline = time.monotonic() + 15
        while self.view.last is None and time.monotonic() < deadline:
            self.client.get("/approvals")                      # lets the app's loop run
            time.sleep(0.05)
        self.assertEqual((self.view.last.reason, self.view.last.words), ("lease", "Timed out. Press Sign in when you're ready."))
        self.assertEqual((self.row(), self.waits()), before)
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(len(self.cards(self.page("/approvals"))), 1)

    def test_a_human_check_done_saves_and_releases_that_job_only(self):
        other = self.ingest("456")
        self.session(stop("CAPTCHA_REQUIRED"), self.key)
        self.session(stop("CAPTCHA_REQUIRED"), other)          # a human check holds no site: each job has its own
        self.assertEqual(self.open_waits(), {self.key, other})
        cards = self.cards(self.page("/approvals"))
        self.assertEqual(len(cards), 2)
        for card in cards:
            self.assertIn(CHECK_WORDS, card)
            self.assertIn(">Do the human check</button>", card)
        target, fragment = self.press_step(self.key)
        self.assertNotIn("data-live-start", fragment)          # the card's own button is the one control
        self.assertIn(">Do the human check</button>", self.page("/live-view/" + target))   # the view's own page: the same words
        self.assertEqual(self.view.target(target).purpose, "human_check")
        self.start_live(target)
        self.end_live("done")
        self.assertTrue(site_sessions.has(SITE))               # a site-wide pass can carry; a form's cannot, and then the card comes back
        self.assertEqual(self.open_waits(), {other})
        self.assertEqual((self.row()["state"], self.row(other)["state"]), ("RETRY_WAIT", "BLOCKED"))
        self.assertEqual(self.events(user_step.FINISHED_EVENT),
                         [{"purpose": "human_check", "site_session_saved": True, "released": 1}])

    def test_an_uncertain_job_in_the_site_group_is_never_released(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held, sure = self.ingest("456"), self.ingest("789")
        self.cycle()
        self.assertEqual(self.open_waits(), {self.key, held, sure})
        self.uncertain(held)
        before = (self.row(held), self.waits(held))
        self.step()
        self.assertEqual(self.open_waits(), {held})
        self.assertEqual((self.row(held), self.waits(held)), before)
        self.assertEqual({self.row(key)["state"] for key in (self.key, sure)}, {"RETRY_WAIT"})
        self.assertEqual(self.events(user_step.FINISHED_EVENT)[0]["released"], 2)

    # --- F2: a job that may already be with the employer is not waiting on the user ---------------------

    def test_f2_an_uncertain_job_has_no_buttons_and_says_it_is_being_checked(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.uncertain(self.key)
        before = (self.row(), self.waits())
        for path in ("/", "/approvals"):
            cards = self.cards(self.page(path))
            self.assertEqual(len(cards), 1)
            self.assertIn(access_cards.NOT_ABANDONED, cards[0])
            self.assertIn(">Open the posting</a>", cards[0])
            for absent in (ASK_WORDS, "Waits for you", "apply yourself", "Apply yourself", '<form', ">Sign in</button>",
                           ">Abandon</button>", access_cards.SETTING_LABEL):
                self.assertNotIn(absent, cards[0], absent)
        # A press from an older page, and the step itself, are refused with the same sentence.
        self.assertIn(access_cards.NOT_ABANDONED, self.post("/access/step", self.key))
        self.assertEqual(user_step.start(self.key, user_step.SIGN_IN), {"available": False, "words": access_cards.NOT_ABANDONED})
        self.assertEqual(self.view.targets, {})
        self.assertIn(access_cards.NOT_ABANDONED, self.post("/access/abandon", self.key))
        # Applications: not "needs you", the same sentence, no "waits for you".
        job = self.app_job()
        self.assertEqual((job["state"], job["needs_you"], job["todo"], job["todo_href"]),
                         ("Waiting for confirmation", False, access_cards.NOT_ABANDONED, None))
        self.assertNotIn(self.key, applications_page.needs_you_keys(overview_page.Reader(self.database), dt.datetime.now(dt.timezone.utc)))
        # And whatever ends a step for it, it is never released or requeued.
        with self.database:
            pass
        self.assertEqual(access_cards.step_finished(self.database, self.key, user_step.SIGN_IN, self.now), [])
        self.assertEqual((self.row(), self.waits()), before)

    def test_f2_an_uncertain_job_never_leads_its_sites_card(self):
        second = self.ingest("456")
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        with self.database:
            access_outcomes.record(self.database, self.row(second), "AUTHENTICATION_REQUIRED", self.now)
        self.uncertain(self.key)                               # the job that met the sign-in first
        [card] = self.cards(self.page("/approvals"))
        head, listed = card.split("Also waiting on this site:")
        self.assertIn("Senior Engineer", head)                 # the other job leads; its button is the one shown
        self.assertIn('name="job_key" value="%s"' % second, card.split('action="/access/step')[1].split("</form>")[0])
        self.assertIn("Staff Software Engineer", listed)
        self.assertIn(access_cards.NOT_ABANDONED, listed)
        self.assertNotIn('aria-label="Abandon', listed)        # no Abandon that cannot work
        before = (self.row(), self.waits())
        self.step(second)
        self.assertEqual((self.row(), self.waits()), before)
        self.assertEqual(self.row(second)["state"], "RETRY_WAIT")

    # --- F3, F5, F6, F7, F8a: what each card says and offers ------------------------------------------

    def test_f3_a_human_check_card_has_its_step_and_no_done_button(self):
        self.session(stop("CAPTCHA_REQUIRED"))
        for path in ("/", "/approvals"):
            [card] = self.cards(self.page(path))
            self.assertIn(">Do the human check</button>", card)
            self.assertIn("You can also apply yourself.", card)
            self.assertEqual(card.count(">I've done the check</button>"), 1)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
            self.assertNotIn(access_cards.SETTING_LABEL, card)   # F8a
        # Its endings name the button the user presses.
        self.view.lease_seconds = 0.3
        self.start_live(self.press_step()[0])
        self.until(lambda: self.view.last is not None)
        self.assertEqual(self.view.last.words, "Timed out. Press Do the human check when you're ready.")
        self.assertIn('data-over="This page has closed. Press Do the human check when you\'re ready."',
                      self.page("/live-view/%s/fragment" % self.press_step()[0]))

    def test_f5_f6_a_group_card_keeps_the_leads_lines_together_and_held_jobs_say_what_is_true(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held = self.ingest("456")
        self.cycle()
        [card] = self.cards(self.page("/approvals"))
        self.assertLess(card.index("You can also apply yourself."), card.index("Also waiting on this site:"))   # F6
        self.assertEqual(card.count("apply yourself"), 1)
        self.assertEqual(self.app_job(held)["todo"], access_outcomes.HOST_HELD_WORDS)
        self.supported = False                                 # no sign-in step on this computer
        [card] = self.cards(self.page("/approvals"))
        self.assertLess(card.index("You can also apply yourself."), card.index("Also waiting on this site:"))
        self.assertEqual(self.app_job(held)["todo"],
                         "Waiting for your sign-in on this site.")   # the state only (owner, 2026-11-14)
        self.assertNotIn("waiting for you to sign in", json.dumps(self.app_job(held), default=str) + self.page("/applications?job=" + held))

    def test_f7_one_name_per_thing(self):
        empty = self.page("/approvals")
        self.assertIn(">Sign-ins and human checks <", empty)                      # the chip
        self.assertIn('<h2 id="sign-ins-title">Sign-ins and human checks</h2>', empty)
        self.assertIn("No application is waiting for you to sign in or do a human check.", empty)
        self.assertNotIn("Sign-ins and checks", empty)
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.step()
        job = self.app_job()                                   # after Done: what is true, never "Will try again automatically."
        self.assertEqual((job["todo"], job["problem"]), ("Sidedoor tries this application on its next round.", None))
        self.assertNotIn("Will try again automatically", self.page("/applications?job=" + self.key))
        shown = [access_cards.RELEASED_ONE, access_cards.RELEASED_MANY, access_cards.REOPENED, access_cards.GONE,
                 live_view.GONE_WORDS, live_view.OVER_WORDS, *live_view.ENDED_WORDS.values(),
                 *access_outcomes.WAIT_REASONS.values(), *access_outcomes.CLOSE_WORDS.values()]
        self.assertEqual(live_view.GONE_WORDS, access_cards.GONE)
        for sentence in shown:
            for wrong in ("applications run", "by hand", "Open again", "a check", "the check"):
                self.assertNotIn(wrong, sentence)

    # E15.T8: Sidedoor can now sign in itself; this is the state with that path switched off.
    @mock.patch.object(access_outcomes, "resolver_available", lambda: False)
    def test_f8a_account_cards_link_to_the_setting_and_skip_shows_it_once_under_the_cards(self):
        check = self.ingest("456")
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
        link = '<a href="/settings#account-setting">Change "When a site needs an account" in Settings</a>'
        for path in ("/", "/approvals"):                       # never chosen (asks each time): on the account card only
            page = self.page(path)
            account, human = sorted(self.cards(page), key=lambda card: CHECK_WORDS in card)
            self.assertEqual((account.count(link), human.count(link), page.count(link)), (1, 0, 1), path)
            self.assertNotIn(access_cards.SETTING_WORDS, page)   # that choice is not offered in this release
        self.assertIn('id="account-setting"', self.page("/settings"))
        self.client.post("/settings/account", data={"account": "ASK"}, headers=ORIGIN)
        self.assertEqual(self.page("/approvals").count(link), 1)
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=ORIGIN)
        for path in ("/", "/approvals"):                       # Skip: no account card is left; once under the cards
            page = self.page(path)
            cards = self.cards(page)
            self.assertEqual(len(cards), 1)
            self.assertEqual((cards[0].count(link), page.count(link)), (0, 1), path)
        self.post("/access/abandon", check)                    # and where the section is shown with no card at all
        self.assertEqual(self.page("/approvals").count(link), 1)

    # --- F4: switching to Skip says what it closed -----------------------------------------------------

    def test_f4_switching_to_skip_says_how_many_it_closed_and_try_again_works_under_ask(self):
        saved = "Saved: Skip jobs that need an account."
        answer = html.unescape(self.client.post("/settings/account", data={"account": "SKIP"}, headers=HTMX).text)
        self.assertIn(saved + "</p>", answer)                  # nothing was waiting: the message as it was
        self.client.post("/settings/account", data={"account": "ASK"}, headers=ORIGIN)
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        answer = html.unescape(self.client.post("/settings/account", data={"account": "SKIP"}, headers=HTMX).text)
        self.assertIn(saved + " 1 waiting application was closed. Bring it back with Try again on Applications.</p>", answer)
        closed = "Closed by your setting."
        self.assertEqual(self.app_job()["closed"], closed)
        # Back to Ask: the closed line is still true, and Try again brings the job back to its card.
        answer = html.unescape(self.client.post("/settings/account", data={"account": "ASK"}, headers=HTMX).text)
        self.assertIn("Saved: Ask me each time.</p>", answer)
        self.assertEqual((self.app_job()["closed"], self.app_job()["try_again"]), (closed, "Try again"))
        self.assertNotIn("skips jobs", self.page("/applications?job=" + self.key))
        self.assertIn("Reopened. Sidedoor reads the posting again on its next round.", self.post("/access/try-again", self.key))
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        self.assertIn(self.key, self.open_waits())
        self.assertEqual(self.waits()[-1]["reason"], "ASK_EACH_TIME")
        self.assertTrue(any(ASK_WORDS in card and ">Sign in</button>" in card for card in self.cards(self.page("/approvals"))))
        # Two at once.
        second = self.ingest("456")
        with self.database:
            access_outcomes.record(self.database, self.row(second), "AUTHENTICATION_REQUIRED", self.now)
        answer = html.unescape(self.client.post("/settings/account", data={"account": "SKIP"}, headers=HTMX).text)
        self.assertIn(saved + " 2 waiting applications were closed. Bring one back with Try again on Applications.</p>", answer)

    def test_a_save_that_fails_still_releases_and_is_recorded(self):
        """The user did their part: the wait is released; nothing was kept, and the record says so."""
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.cookies.write_text("[]")                          # the site set nothing the guard could keep
        self.step()
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(self.row()["state"], "RETRY_WAIT")
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": False, "released": 1}])

    # --- L1 and the walkthrough's finding: what is swapped, and how much of the page --------------------

    def test_l1_the_list_is_drawn_again_after_done_only(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        for at, path, target in (("overview", "/", "overview-needs"), ("approvals", "/approvals", "sign-ins")):
            card = self.cards(self.page(path))[0]
            self.assertIn('<div class="access-step" hx-get="/access/section?at=%s&ended=done" hx-trigger="live-view-done" '
                          'hx-target="#%s" hx-swap="outerHTML"></div>' % (at, target), card)
        # Cancel: nothing is drawn again (the card keeps the view's sentence); the server's list is as it was.
        self.step(reason="cancel")
        for at, target in (("overview", "overview-needs"), ("approvals", "sign-ins")):
            section = self.page("/access/section?at=" + at)
            cards = self.cards(section)
            self.assertEqual(len(cards), 1)
            self.assertIn(">Sign in</button>", cards[0])
            self.assertNotIn("data-live-view", section)
        # Done: the list is drawn again with what Done said; the card is gone, Overview's count with it.
        self.step()
        for at in ("overview", "approvals"):
            section = self.page("/access/section?at=%s&ended=done" % at)
            self.assertEqual(self.cards(section), [])
            self.assertIn('role="status">Done. Sidedoor carries on from here.</p>', section)
            self.assertNotIn("sign-in or human check to do", section)
        self.assertIn("Nothing needs you right now.", self.page("/access/section?at=overview"))
        self.assertNotIn("carries on", self.page("/access/section?at=approvals&ended=<b>x</b>"))   # only the server's own sentence
        # The view asks for that redraw only after Done, once the server has answered; never for another ending.
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        self.assertIn('if (reason === "done" || reason === "finished") root.dispatchEvent(new CustomEvent("live-view-done"', viewer)
        answered = viewer.index('post("/live-view/end?reason=" + reason)')
        self.assertLess(answered, viewer.index("closed(answer.body.reason);", answered))
        self.assertIn('finish(message.words); if (!asked) closed(message.reason); }', viewer)
        failed = viewer[viewer.index('if (answer.body.state !== "live")'):viewer.index("session = answer.body.session")]
        self.assertNotIn("closed(", failed)
        self.assertEqual(viewer.count("closed("), 3)           # its definition and the two calls above
        self.assertNotIn("live-view-closed", viewer)

    def test_a_step_is_shown_inside_its_own_card_and_the_rest_of_the_page_stays(self):
        """What htmx does with each answer, worked out from the page itself (`Swaps`): which element is
        replaced, and how much. The real-browser walkthrough found the live view replacing the whole section,
        because the block `/access/step` returns inherited the hx-target of the element it was put in."""
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        for number, host in (("811", "one.example.com"), ("812", "two.example.com")):
            key = self.ingest(number)
            payload = dict(json.loads(self.row(key)["payload"]), url="https://%s/role-%s" % (host, number))
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), key))
            self.database.commit()
            with self.database:
                access_outcomes.record(self.database, self.row(key), "CAPTCHA_REQUIRED" if number == "812" else "AUTHENTICATION_REQUIRED", self.now)
        for path, section_id in (("/approvals", "sign-ins"), ("/", "overview-needs")):
            page = Swaps(self.client.get(path).text)
            cards = page.all("article")
            self.assertEqual(len(cards), 3, path)
            heads = [page.text(card) for card in cards]
            pressed = next(card for card in cards if 'value="%s"' % html.escape(self.key) in page.html(card))
            form = next(node for node in page.inside(pressed) if node.get("hx-post", "").startswith("/access/step"))
            holder = page.target_of(form)                      # where the answer to the press goes
            self.assertEqual((holder.get("class"), form["hx-swap"]), ("access-step", "innerHTML"))
            self.assertIn(holder, page.inside(pressed))
            # 1. The press: the answer goes inside this card's own holder.
            answer = self.client.post(form["hx-post"], data={"job_key": self.key, "at": "x"}, headers=HTMX).text
            page.swap(holder, "innerHTML", answer)
            loader = next(node for node in page.inside(holder) if node.get("hx-trigger") == "load")
            # 2. The block that loads the live view replaces ITSELF, not what its parent points at.
            self.assertIs(page.target_of(loader), loader, "the live view would replace %r" % page.target_of(loader).get("id"))
            self.assertEqual(loader["hx-swap"], "outerHTML")
            fragment = self.client.get(html.unescape(loader["hx-get"])).text
            self.assertEqual(len(Swaps(fragment).roots()), 1)
            self.assertEqual({name for node in Swaps(fragment).all() for name in node if name.startswith("hx-")}, set())
            page.swap(loader, "outerHTML", fragment)
            # 3. With the live view open: the same cards, the same heading, the view inside the pressed card only.
            self.same_page(page, section_id, heads, pressed, "data-live-view")
            target_id = page.first("section", "data-live-view")["data-target"]
            # 4. A start that fails, Cancel, the time running out: the view says its sentence in place (its own
            #    script, no request for markup), so the page keeps this shape. Nothing in it asks for a redraw.
            self.assertNotIn("live-view-closed", page.source)
            self.same_page(page, section_id, heads, pressed, "data-live-view")
            # 5. Done: the one redraw. It is asked for by the pressed card's holder and replaces exactly the section.
            holder = next(node for node in page.inside(pressed) if node.get("class") == "access-step")
            self.assertEqual((holder["hx-trigger"], holder["hx-swap"], page.target_of(holder).get("id")),
                             ("live-view-done", "outerHTML", section_id))
            redraw = self.client.get(html.unescape(holder["hx-get"])).text
            roots = [node for node in Swaps(redraw).roots() if node.get("hx-swap-oob") is None]
            self.assertEqual([(node.tag, node.get("id")) for node in roots],
                             # Overview's part is one wrapper holding the count tile and the cards block (E15.T14 L7).
                             [("div" if section_id == "overview-needs" else "section", section_id)], path)
            page.swap(page.target_of(holder), "outerHTML", redraw)
            self.assertEqual([page.text(card) for card in page.all("article")], heads)   # nothing was done: the same three cards
            self.assertIn("Done. Sidedoor carries on from here.", page.source)
            self.assertIsNotNone(page.by_id(section_id))
            self.view.withdraw(target_id)
        # A press that cannot start (the step is gone): its sentence also stays inside the card.
        page = Swaps(self.client.get("/approvals").text)
        pressed = next(card for card in page.all("article") if 'value="%s"' % html.escape(self.key) in page.html(card))
        form = next(node for node in page.inside(pressed) if node.get("hx-post", "").startswith("/access/step"))
        self.post("/access/abandon", self.key)
        page.swap(page.target_of(form), "innerHTML", self.client.post(form["hx-post"], data={"job_key": self.key}, headers=HTMX).text)
        self.assertEqual(len(page.all("article")), 3)
        self.assertIn(access_cards.GONE, page.html(next(card for card in page.all("article") if "access-step" in page.html(card) and access_cards.GONE in page.html(card))))

    def step_controls(self, page, card, label):
        """The controls a card shows for its step: buttons that say the step's name and are not hidden."""
        return [node for node in page.inside(card) if node.tag == "button" and "hidden" not in node
                and page.text(node) == label]

    def test_a_card_offers_its_step_once_in_every_state(self):
        check = self.ingest("813")
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
        viewer = (Path(live_view.__file__).parent / "dashboard_assets" / "static" / "live_view.js").read_text()
        for path in ("/approvals", "/"):
            for key, label in ((self.key, "Sign in"), (check, "Do the human check")):
                page = Swaps(self.client.get(path).text)       # a fresh page (also: after a reload)
                find = lambda: next(card for card in page.all("article") if 'value="%s"' % html.escape(key) in page.html(card)
                                    and "/access/step" in page.html(card))
                self.assertEqual(len(self.step_controls(page, find(), label)), 1, (path, label, "fresh"))
                form = next(node for node in page.inside(find()) if node.get("hx-post", "").startswith("/access/step"))
                page.swap(page.target_of(form), "innerHTML",
                          self.client.post(form["hx-post"], data={"job_key": key}, headers=HTMX).text)
                loader = next(node for node in page.inside(find()) if node.get("hx-trigger") == "load")
                page.swap(page.target_of(loader), "outerHTML", self.client.get(html.unescape(loader["hx-get"])).text)
                # Opening, open, cancelled, timed out, a start that failed: the view changes its words and its
                # Done and Cancel in place and has no step button of its own, so in each the card has exactly one.
                card = find()
                self.assertIn("data-live-view", page.html(card))
                self.assertEqual(len(self.step_controls(page, card, label)), 1, (path, label, "with the view"))
                self.assertEqual([node for node in page.inside(card) if "data-live-start" in node], [])
                self.assertEqual(sorted(page.text(node) for node in page.inside(card) if "data-live-done" in node or "data-live-cancel" in node),
                                 ["Cancel", "Done"])
                self.assertEqual(len([node for node in page.inside(card) if node.tag == "button" and page.text(node) == "Abandon"
                                      and "aria-label" not in node]), 1)
                self.view.targets.clear()
        # The view holds the card's button while it is opening or open, and lets it go at every ending.
        self.assertIn('card.querySelector(\'form[action^="/access/step"] button\')', viewer)
        opening = viewer[viewer.index("function start()"):viewer.index("function end(")]
        self.assertIn("hold(true)", opening)
        ending = viewer[viewer.index("function finish("):viewer.index("function start()")]
        self.assertIn("hold(false)", ending)
        self.assertEqual(viewer.count("hold(true)"), 1)
        # The view's own page (not in a card) keeps its start button.
        target = self.view.offer(self.key, URL, "sign_in")
        self.assertIn("data-live-start", self.page("/live-view/" + target))
        self.assertIn("data-live-start", self.page("/live-view/%s/fragment" % target))

    def same_page(self, page, section_id, heads, pressed, marker):
        cards = page.all("article")
        self.assertEqual(len(cards), len(heads))
        self.assertIsNotNone(page.by_id(section_id))
        for card, head in zip(cards, heads):
            self.assertTrue(page.text(card).startswith(head.split(" Sign in")[0][:40]), head)
        holding = [card for card in cards if marker in page.html(card)]
        self.assertEqual(len(holding), 1)
        self.assertEqual(holding[0].get("id"), pressed.get("id"))
        self.assertEqual(page.source.count(marker + " "), 1)

    # --- L2: a session saved on Done is marked as one that got past what the site asked ---------------

    def test_l2_a_session_saved_on_done_expires_like_any_that_got_past_the_sign_in(self):
        soon = time.time() + 600
        self.cookies.write_text(json.dumps([cookie("session", SESSION_VALUE, expires=soon)]))
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.step()
        self.assertEqual(self.store()["sites"][SITE]["wall"], "AUTHENTICATION_REQUIRED")
        self.assertIsNone(site_sessions.before_session(SITE))                       # live: a read starts as usual
        # Expired: known without paying for a read, exactly as `site_sessions.before_session` rules.
        self.assertEqual(site_sessions.before_session(SITE, now=soon + 1), "AUTHENTICATION_REQUIRED")
        self.assertFalse(site_sessions.has(SITE))

    def test_l2_a_human_check_done_is_marked_with_the_check_and_cancel_marks_nothing(self):
        self.session(stop("CAPTCHA_REQUIRED"))
        self.step(reason="cancel")
        self.assertEqual(self.store()["sites"], {})
        self.step()
        self.assertEqual(self.store()["sites"][SITE]["wall"], "CAPTCHA_REQUIRED")

    # --- L3: only cookies kept in this step are ever saved --------------------------------------------

    def test_l3_a_capture_left_by_a_crashed_read_is_never_saved_by_a_step_that_kept_nothing(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        crashed = FakeJar(URL, [cookie("stale", "from-a-crashed-read")])
        self.assertEqual(site_sessions.save(SITE, crashed.call, pending=True), 1)   # a read that never settled
        self.assertIn(SITE, self.store()["pending"])
        self.cookies.write_text("[]")                          # this step's guard keeps nothing
        self.step()
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual((self.store()["sites"], self.store()["pending"]), ({}, {}))
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": False, "released": 1}])

    def test_l3_a_saved_session_is_kept_when_a_later_step_is_cancelled(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.step()
        self.session(stop("MFA_REQUIRED"), self.key)           # the site asks for a code; the saved sign-in stays
        self.step(reason="cancel")
        self.assertEqual([item["name"] for item in self.store()["sites"][SITE]["cookies"]], ["session"])

    # --- L4: a job that stops waiting takes its offered step with it ----------------------------------

    def until(self, check, seconds=15):
        deadline = time.monotonic() + seconds
        while not check() and time.monotonic() < deadline:
            self.client.get("/access/section?at=approvals")    # lets the app's loop run
            time.sleep(0.05)
        self.assertTrue(check())

    def test_l4_abandoning_withdraws_the_step_and_ends_an_open_view_without_saving_or_releasing(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        target, _ = self.press_step()
        self.start_live(target)
        self.post("/access/abandon", self.key)
        self.assertIsNone(self.view.target(target))
        self.until(lambda: self.view.session is None and self.view.last is not None)
        self.assertEqual((self.view.last.reason, self.view.last.words), ("cancel", "Cancelled."))
        self.assertEqual((self.row()["state"], self.row()["blocker"]), ("ABANDONED", "ACCESS_ABANDONED_BY_YOU"))
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [])
        self.assertFalse(site_sessions.has(SITE))
        self.assertEqual(self.store()["pending"], {})
        # A view left on an old page cannot start the step again.
        answer = self.client.post(f"/live-view/{target}/start", headers=ORIGIN)
        self.assertEqual((answer.status_code, answer.json()["words"]), (404, access_cards.GONE))             # F7: one sentence, card and view

    def test_l4_a_step_offered_and_not_open_is_withdrawn_by_abandon_done_and_the_skip_sweep(self):
        for number, act in enumerate((
                lambda key: self.post("/access/abandon", key),
                lambda key: self.client.post("/settings/account", data={"account": "SKIP"}, headers=ORIGIN))):
            key = self.ingest("90%d" % number)
            with self.database:
                access_outcomes.record(self.database, self.row(key), "AUTHENTICATION_REQUIRED", self.now, source="session")
            target, _ = self.press_step(key)
            self.assertIsNotNone(self.view.target(target))
            act(key)
            self.assertIsNone(self.view.target(target), number)
            self.assertEqual(self.row(key)["state"], "ABANDONED")

    def test_l4_a_wait_closed_by_the_applications_run_ends_the_open_view(self):
        """7 days, a closed posting: another process closes the wait. The open view notices within seconds."""
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.view.check_seconds = 0.2
        target, _ = self.press_step()
        self.start_live(target)
        with self.database:
            access_outcomes.expire(self.database, desktop_adapter.later(self.now, 8 * 86400))
        self.assertEqual(self.row()["blocker"], "ACCESS_WAITED_7_DAYS")
        self.until(lambda: self.view.session is None and self.view.last is not None)
        self.assertEqual(self.view.last.reason, "cancel")
        self.assertIsNone(self.view.target(target))
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [])
        self.assertFalse(site_sessions.has(SITE))

    def test_l4_when_the_lead_leaves_the_next_lead_offers_sign_in_again(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        second = self.ingest("456")
        with self.database:                                    # it met the same sign-in in a session of its own
            access_outcomes.record(self.database, self.row(second), "AUTHENTICATION_REQUIRED", self.now)
        cards = self.cards(self.page("/approvals"))
        self.assertEqual((len(cards), cards[0].count('action="/access/step')), (1, 1))   # one card, one Sign in: the lead's
        self.assertIn('name="job_key" value="%s"' % self.key, cards[0].split('action="/access/step')[1].split("</form>")[0])
        first_target, _ = self.press_step(self.key)
        self.post("/access/abandon", self.key)
        self.assertIsNone(self.view.target(first_target))
        self.assertEqual(self.open_waits(), {second})
        cards = self.cards(self.page("/approvals"))
        self.assertEqual(len(cards), 1)
        self.assertIn(">Sign in</button>", cards[0])
        target, _ = self.press_step(second)
        self.assertNotEqual(target, first_target)
        self.assertEqual(self.view.target(target).job, second)
        self.start_live(target)
        self.end_live("done")
        self.assertEqual(self.row(second)["state"], "RETRY_WAIT")

    # --- where the step cannot be shown ---------------------------------------------------------------

    def test_a_computer_that_cannot_show_the_step_has_no_step_and_no_done_button(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        other = self.ingest("456")
        with self.database:
            access_outcomes.record(self.database, self.row(other), "CAPTCHA_REQUIRED", self.now)
        self.supported = False                                 # Windows today
        for path in ("/", "/approvals"):
            cards = self.cards(self.page(path))
            self.assertEqual(len(cards), 2)
            for card in cards:
                self.assertNotIn('action="/access/step', card)
                self.assertEqual(card.count('action="/access/done'), 1)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
                self.assertNotIn(">Sign in</button>", card)
                self.assertIn("You can also apply yourself.", card)
                self.assertIn(">Open the posting</a>", card)
                self.assertIn(">Abandon</button>", card)
        self.assertIn("This step can't be done inside Sidedoor on this computer yet.", self.post("/access/step", self.key))
        self.assertEqual(self.view.targets, {})

    def test_the_page_opened_is_the_jobs_own_never_what_a_session_reported(self):
        result = dict(stop("AUTHENTICATION_REQUIRED", {"sign_in_offered": True}), observed_origin="https://evil.example.net")
        self.session(result)
        self.assertEqual(access_outcomes.description(self.waits()[0])["observed_origin"], "https://evil.example.net")
        target, _ = self.press_step()
        self.assertEqual((self.view.target(target).url, self.view.target(target).domain), (URL, SITE))
        # An address that is not https is never offered.
        payload = dict(json.loads(self.row()["payload"]), url="http://jobs.ashbyhq.com/synthetic/role-123")
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), self.key))
        self.database.commit()
        self.view.targets.clear()
        self.assertEqual(user_step.start(self.key, user_step.SIGN_IN), {"available": False})
        self.assertEqual(self.view.targets, {})
        # A job with no card is never offered.
        self.assertEqual(user_step.start("no-such-job", user_step.SIGN_IN), {"available": False})

    def test_the_real_guard_command_is_given_the_site(self):
        """Without the stand-in: the live view's own guard command carries the site, as a read's does."""
        view = live_view.LiveView(root=self.root, config={"browser_driver": {"profile": str(self.root / "profile")}},
                                  is_supported=lambda config: True)
        target = view.target(view.offer(self.key, URL, "sign_in", domain=SITE))
        command = view.guard_command(view.session_config(target), self.root / "reports")
        self.assertEqual(command[command.index("--site-session") + 1], SITE)
        self.assertEqual(view.config.get(site_sessions.CONFIG_KEY), None)   # the product's config is not changed

    # --- passkey, Skip --------------------------------------------------------------------------------

    def test_a_passkey_card_offers_only_apply_yourself_and_abandon(self):
        self.session(stop("AUTHENTICATION_REQUIRED", PASSKEY_WALL))
        self.assertEqual(self.waits()[0]["reason"], "PASSKEY")
        for path in ("/", "/approvals"):
            cards = self.cards(self.page(path))
            self.assertEqual(len(cards), 1)
            self.assertIn(PASSKEY_WORDS, cards[0])
            self.assertNotIn("You can also apply yourself.", cards[0])
            self.assertIn(">Open the posting</a>", cards[0])
            self.assertIn(">Abandon</button>", cards[0])
            self.assertNotIn('action="/access/step', cards[0])
            self.assertNotIn('action="/access/done', cards[0])
        self.assertNotIn("data-live-view", self.post("/access/step", self.key))
        self.assertEqual(self.view.targets, {})
        todo = self.app_job()["todo"]
        self.assertTrue(todo.startswith(PASSKEY_WORDS + " Closes on ") and todo.endswith(" unless you abandon it first."), todo)
        card = self.cards(self.page("/approvals"))[0]
        self.assertIn("unless you abandon it first.", card)   # F5: nothing the user does in Sidedoor ends it
        self.assertNotIn("Waits for you", card + todo)
        self.assertNotIn(access_cards.SETTING_LABEL, card)     # F8a: no link to the setting on a passkey card

    def test_skip_closes_an_account_wall_and_never_a_human_check(self):
        with self.database:
            access_outcomes.set_account_setting(self.database, "SKIP", self.now)
        check = self.ingest("456")
        request = self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        self.assertEqual((self.row()["state"], self.row()["blocker"]), ("ABANDONED", "ACCESS_SKIPPED_BY_SETTING"))
        self.session(stop("CAPTCHA_REQUIRED"), check)
        self.assertEqual(self.open_waits(), {check})
        cards = self.cards(self.page("/approvals"))
        self.assertEqual(len(cards), 1)
        self.assertIn(CHECK_WORDS, cards[0])
        closed = self.app_job()
        self.assertEqual((closed["closed"], closed["todo"]), ("Closed by your setting.",
                                                              "If you want this job, open the posting and apply yourself."))
        self.assertIsNotNone(request)

    # --- owner's pick M2 a: a job closed by Skip leaves its own row -----------------------------------

    def skipped_row(self, key, since):
        rows = self.waits(key)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["source"], row["host"], row["wall"], row["state"], row["outcome"], row["since"], row["ended_at"] is not None),
                         ("session", SITE, "AUTHENTICATION_REQUIRED", "CLOSED", "SKIPPED", since, True))
        return row

    def test_m2_a_skip_at_a_wall_with_no_wait_open_leaves_one_closed_row(self):
        with self.database:
            access_outcomes.set_account_setting(self.database, "SKIP", self.now)
        self.cycle()
        request = self.claim()
        desktop_adapter.complete(self.database, dict(request, result=stop("AUTHENTICATION_REQUIRED")), self.now)
        self.advance()
        self.cycle()
        self.assertEqual(self.row()["state"], "ABANDONED")
        row = self.skipped_row(self.key, self.waits()[0]["since"])
        self.assertLessEqual(row["since"], row["ended_at"])
        # No card, no site hold: the next job on the site gets its own session.
        self.assertEqual((self.cards(self.page("/approvals")), self.needs()["sign_ins"]), ([], 0))
        self.assertIsNone(access_outcomes.host_holder(self.database, SITE, self.now))
        self.assertIsNone(access_outcomes.open_wait(self.database, self.key))
        with self.database:                                    # and the days ahead close nothing more
            access_outcomes.expire(self.database, desktop_adapter.later(self.now, 30 * 86400))
        self.skipped_row(self.key, row["since"])
        self.assertEqual(self.row()["blocker"], "ACCESS_SKIPPED_BY_SETTING")

    def test_m2_a_the_skip_sweep_closes_the_open_row_and_writes_no_second_one(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held = self.ingest("456")
        self.cycle()
        self.assertEqual(self.open_waits(), {self.key, held})
        since, holder = self.waits()[0]["since"], self.waits(held)[0]["source"]
        self.assertEqual(holder, "host:" + self.key)
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=ORIGIN)
        self.skipped_row(self.key, since)
        self.assertEqual(self.row()["state"], "ABANDONED")
        # The job merely held behind the site met nothing itself: it is released, its row keeps its source.
        rows = self.waits(held)
        self.assertEqual([(row["source"], row["state"], row["outcome"]) for row in rows], [(holder, "RESOLVED", "host_released")])
        self.assertEqual(self.row(held)["state"], "RETRY_WAIT")

    def test_m2_a_skip_met_by_a_job_once_held_behind_the_site_adds_its_own_session_row(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held = self.ingest("456")
        self.cycle()
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=ORIGIN)
        self.advance()
        self.session(stop("AUTHENTICATION_REQUIRED"), held)    # its own session meets the sign-in under Skip
        rows = self.waits(held)
        self.assertEqual([(row["source"], row["state"], row["outcome"]) for row in rows],
                         [("host:" + self.key, "RESOLVED", "host_released"), ("session", "CLOSED", "SKIPPED")])
        self.assertEqual((rows[1]["host"], rows[1]["wall"]), (SITE, "AUTHENTICATION_REQUIRED"))
        self.assertEqual(self.row(held)["blocker"], "ACCESS_SKIPPED_BY_SETTING")
        self.assertEqual(self.open_waits(), set())

    # --- C1: the session is kept before the browser is let go ---------------------------------------

    def test_c1_a_read_queued_on_the_site_cannot_drop_the_users_capture(self):
        """A human check holds no site, so a read on it can be waiting for the browser. It takes the
        browser the moment the live view lets go and starts as every read does (`before_session`, which
        drops any capture not yet settled). The step's capture is settled before that."""
        self.session(stop("CAPTCHA_REQUIRED"))
        seen, drop = [], live_view.Hold.drop

        def let_go(hold):
            seen.append(("kept" if site_sessions.has(SITE) else "not kept", bool(self.store()["pending"])))
            drop(hold)
            site_sessions.before_session(SITE)                 # the queued read, at once
        with mock.patch.object(live_view.Hold, "drop", let_go):
            self.step()
        self.assertEqual(seen[-1], ("kept", False))            # settled while the browser was still held
        self.assertTrue(site_sessions.has(SITE))
        self.assertEqual(self.events(user_step.FINISHED_EVENT),
                         [{"purpose": "human_check", "site_session_saved": True, "released": 1}])
        self.assertTrue(self.view.last.site_session_saved)

    def test_c1_a_settle_that_takes_too_long_is_never_recorded_as_saved(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        slow = site_sessions.settle

        def settle(*arguments, **options):
            if options.get("signed_in"):
                time.sleep(0.6)
            return slow(*arguments, **options)
        with mock.patch.object(live_view, "SETTLE_SECONDS", 0.2), mock.patch.object(site_sessions, "settle", settle):
            self.step()
        self.assertFalse(self.view.last.site_session_saved)
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": False, "released": 1}])
        time.sleep(1.0)                                        # D13: and nothing is written once the time is up
        self.assertFalse(site_sessions.has(SITE))

    # --- C4: a posting the live view cannot open ----------------------------------------------------

    def test_c4_a_posting_that_cannot_be_opened_here_has_no_step_button(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.assertIn(">Sign in</button>", self.cards(self.page("/approvals"))[0])
        for address in ("http://jobs.ashbyhq.com/synthetic/role-123", "https://jobs.ashbyhq.com:8443/synthetic/role-123",
                        "https://user:pw@jobs.ashbyhq.com/synthetic/role-123", "https://elsewhere.example.com/synthetic/role-123"):
            payload = dict(json.loads(self.row()["payload"]), url=address)
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), self.key))
            self.database.commit()
            for path in ("/", "/approvals"):
                [card] = self.cards(self.page(path))
                self.assertNotIn('action="/access/step', card, address)
                self.assertEqual(card.count('action="/access/done'), 1, address)   # owner, 2026-11-14: a sign-in or human-check card always has its done button
                self.assertIn("You can also apply yourself.", card)
                self.assertIn(">Abandon</button>", card)
            self.assertIn("This posting can't be opened here. Apply yourself with the link on the card.",
                          self.post("/access/step", self.key), address)
            self.assertEqual(self.view.targets, {})

    # --- C5: only the site the step was started for is saved ----------------------------------------

    def test_c5_done_on_a_sign_in_providers_page_stores_nothing_for_the_provider(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.ends_on = "https://accounts.provider.example/signin/done"
        self.cookies.write_text(json.dumps([cookie("session", SESSION_VALUE), cookie("sso", "provider-secret", "accounts.provider.example")]))
        self.step()
        self.assertEqual(sorted(self.store()["sites"]), [SITE])
        self.assertEqual([item["name"] for item in self.store()["sites"][SITE]["cookies"]], ["session"])
        self.assertFalse(site_sessions.has("accounts.provider.example"))
        self.assertEqual(self.store()["pending"], {})

    # --- C6: a step belongs to the wait it was started for ------------------------------------------

    def test_c6_a_late_done_never_releases_a_newer_wait(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        first = self.waits()[0]["id"]
        target, _ = self.press_step()
        self.start_live(target)
        with self.database:                                    # meanwhile: that wait ended, and the site asked again
            access_outcomes.resume(self.database, self.key, self.now)
            access_outcomes.record(self.database, self.row(), "AUTHENTICATION_REQUIRED", self.now)
        newer = access_outcomes.open_wait(self.database, self.key)
        self.assertNotEqual(newer["id"], first)
        before = (self.row(), self.waits())
        self.end_live("done")
        self.assertEqual((self.row(), self.waits()), before)
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [])
        # The card for the newer wait offers its own step; Done there releases it.
        again, _ = self.press_step()
        self.assertNotEqual(again, target)
        self.start_live(again)
        self.end_live("done")
        self.assertEqual(self.row()["state"], "RETRY_WAIT")
        self.assertEqual([wait["state"] for wait in self.waits()], ["RESOLVED", "RESOLVED"])

    def test_c6_a_step_offered_for_an_older_wait_is_withdrawn(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        target, _ = self.press_step()
        with self.database:
            access_outcomes.resume(self.database, self.key, self.now)
            access_outcomes.record(self.database, self.row(), "AUTHENTICATION_REQUIRED", self.now)
        user_step.sync()
        self.assertIsNone(self.view.target(target))

    # --- C7: the numbers count cards --------------------------------------------------------------------

    def test_c7_a_site_group_counts_once_in_needs_you_the_tray_and_the_notification(self):
        posted = []
        watcher = desktop_alerts.Watcher(home=None, root=None, read=lambda: dashboard.read_connection(None),
                                         provider=notifications.Desktop(post=lambda title, text, href: posted.append(text) or True),
                                         combine_over=desktop_alerts.COMBINE_OVER)
        watcher.tick()
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        for number in ("456", "457", "458", "459"):
            self.ingest(number)
        self.cycle()
        self.assertEqual(len(self.open_waits()), 5)
        [card] = self.cards(self.page("/"))
        self.assertEqual(card.count("<li>"), 4)                # the card still lists every job
        self.assertEqual(self.needs()["sign_ins"], 1)
        self.assertIn("sign-in or human check to do", self.page("/"))
        watcher.tick()
        self.assertEqual((posted, watcher.total), (["1 sign-in or human check to do"], 1))
        check = self.ingest("900")
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
        watcher.tick()
        self.assertEqual((posted[-1], watcher.total), ("2 sign-ins or human checks to do", 2))
        self.assertIn("sign-ins or human checks to do", self.page("/"))

    # --- D5 to D14 (owner, 2026-11-12) ----------------------------------------------------------------

    def chip(self, page):
        return int(re.search(r">Sign-ins and human checks <span class=\"app-count\">(\d+)</span>", page).group(1))

    def test_d5_an_uncertain_job_is_shown_but_never_counted_as_needing_the_user(self):
        posted = []
        watcher = desktop_alerts.Watcher(home=None, root=None, read=lambda: dashboard.read_connection(None),
                                         provider=notifications.Desktop(post=lambda title, text, href: posted.append(text) or True),
                                         combine_over=desktop_alerts.COMBINE_OVER)
        watcher.tick()
        self.session(stop("AUTHENTICATION_REQUIRED"))
        self.uncertain(self.key)
        watcher.tick()
        overview = self.page("/")
        self.assertEqual(len(self.cards(overview)), 1)         # still shown, with what is happening
        self.assertEqual((self.needs()["sign_ins"], self.chip(self.page("/approvals")), posted, watcher.total), (0, 0, [], 0))
        self.assertNotIn("human check to do", overview)

    def test_d6_the_approvals_chip_and_needs_you_count_the_same_cards(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        for number in ("456", "457"):
            self.ingest(number)
        self.cycle()
        check = self.ingest("900")
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
        page = self.page("/approvals")
        self.assertEqual(len(self.open_waits()), 4)
        self.assertEqual((self.chip(page), self.needs()["sign_ins"], len(self.cards(page)), len(self.cards(self.page("/")))), (2, 2, 2, 2))

    def test_d7_a_held_job_never_says_a_sign_in_is_waiting_where_its_card_offers_none(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        held = self.ingest("456")
        self.cycle()
        self.assertEqual(self.app_job(held)["todo"], access_outcomes.HOST_HELD_WORDS)   # the card offers Sign in
        # The job it waits behind has an address the live view cannot open: its card has no Sign in.
        payload = dict(json.loads(self.row()["payload"]), url="https://jobs.ashbyhq.com:8443/synthetic/role-123")
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), self.key))
        self.database.commit()
        self.assertNotIn(">Sign in</button>", self.page("/approvals"))
        self.assertEqual(self.app_job(held)["todo"], access_cards.HELD_NO_STEP_WORDS)
        for page in (self.page("/applications?job=" + held), self.page("/applications"), self.page("/approvals"), self.page("/")):
            self.assertNotIn("waiting for you to sign in", page)

    def test_d8_a_card_with_nothing_to_press_says_when_it_closes_not_that_it_waits_for_you(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        card = self.cards(self.page("/approvals"))[0]
        self.assertIn("Waits for you until ", card)            # it offers Sign in
        self.assertTrue(self.app_job()["todo"].endswith(", then closes."))
        self.supported = False
        for path in ("/", "/approvals"):
            card = self.cards(self.page(path))[0]
            self.assertIn("Waits for you until ", card)        # owner, 2026-11-14: a sign-in or human-check card always has its done button
        todo = self.app_job()["todo"]
        self.assertTrue(todo.startswith(ASK_WORDS + " Waits for you until ") and todo.endswith(", then closes."), todo)

    def test_d9_the_skip_message_counts_the_whole_group(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        for number in ("456", "457"):
            self.ingest(number)
        self.cycle()
        self.assertEqual(len(self.open_waits()), 3)
        answer = html.unescape(self.client.post("/settings/account", data={"account": "SKIP"}, headers=HTMX).text)
        self.assertIn("Saved: Skip jobs that need an account. 1 waiting application was closed. Bring it back with Try again "
                      "on Applications. 2 more on the same site will be tried on the next round.</p>", answer)
        self.assertEqual(sorted(self.row(key)["state"] for key in ("synthetic company:role-456", "synthetic company:role-457")),
                         ["RETRY_WAIT", "RETRY_WAIT"])
        self.assertEqual(self.open_waits(), set())

    def test_d9_fact_a_job_freed_by_the_skip_switch_is_closed_only_if_its_own_session_meets_the_sign_in(self):
        """What happens, over the following rounds, to the jobs that waited behind a job the switch to Skip
        closed: each gets one session of its own. One that meets the sign-in is closed then (by the Skip
        setting); one whose read goes through carries on and is never closed."""
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        walled, fine = self.ingest("456"), self.ingest("457")
        self.cycle()
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=ORIGIN)
        self.assertEqual((self.row()["state"], self.row(walled)["state"], self.row(fine)["state"]),
                         ("ABANDONED", "RETRY_WAIT", "RETRY_WAIT"))
        results = {walled: stop("AUTHENTICATION_REQUIRED"), fine: None}
        for _ in range(8):
            self.advance()
            self.cycle()
            request = self.claim()
            if request is None or request["payload"]["operation"] != "inspect":
                continue
            key = request["payload"]["job"]["job_key"]
            desktop_adapter.complete(self.database, dict(request, result=results[key] or self.review(self.row(key))), self.now)
        self.cycle()
        self.assertEqual((self.row(walled)["state"], self.row(walled)["blocker"]), ("ABANDONED", "ACCESS_SKIPPED_BY_SETTING"))
        self.assertNotIn(self.row(fine)["state"], ("ABANDONED", "EXHAUSTED"))
        self.assertNotIn(self.row(fine)["blocker"], ("ACCESS_SKIPPED_BY_SETTING", "AUTHENTICATION_REQUIRED"))
        self.assertEqual([wait["outcome"] for wait in self.waits(fine)], ["host_released"])

    def test_d10_no_sentence_says_next_applications_run(self):
        root = Path(access_cards.__file__).parent
        files = sorted(root.glob("*.py")) + sorted((root / "dashboard_assets").rglob("*.html")) + sorted((root / "dashboard_assets").rglob("*.js"))
        text = {path.name: path.read_text(encoding="utf-8") for path in files}
        # The scan reads the files it should (it must find the sentences that replaced the old ones).
        for name in ("access_cards.py", "applications_page.py", "approvals_page.py", "access_outcomes.py"):
            self.assertRegex(text[name], r"on (its|the) next\s*\"?\s*round", name)
        self.assertGreater(len(text), 100)
        pattern = re.compile(r"next\W+applications\W+run")
        self.assertIsNotNone(pattern.search('"on its next "\n   "applications run, which starts now."'))   # the control
        self.assertEqual({name for name, body in text.items() if pattern.search(body)}, set())
        queued = applications_page._what_to_do({"state": "QUEUED", "blocker": None, "next_at": None, "job_key": "k"},
                                               "Co", None, False, False, str)
        self.assertEqual(queued[0], "Sidedoor reads the posting on its next round.")

    def test_d11_a_stale_press_on_a_passkey_card_says_the_passkey_sentence(self):
        self.session(stop("AUTHENTICATION_REQUIRED", PASSKEY_WALL))
        answer = self.post("/access/step", self.key)
        self.assertIn('role="status">' + PASSKEY_WORDS + "</p>", answer)   # the owner's sentence, nothing appended
        self.assertNotIn("on this computer", answer)
        self.assertNotIn("link on the card", answer)
        self.assertFalse(hasattr(live_view, "APPLY_YOURSELF_WORDS"))   # D20: unused, deleted
        self.assertIs(access_cards.PASSKEY_PRESS, access_outcomes.WAIT_REASONS["PASSKEY"])
        self.assertEqual(access_outcomes.WAIT_REASONS["PASSKEY"], PASSKEY_WORDS)
        for wrong in ("fingerprint", "security key", "your own browser"):
            self.assertNotIn(wrong, " ".join(live_view.ALL_WORDS))

    def test_d12_a_store_write_that_fails_is_never_recorded_as_saved(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        kept = FakeJar(URL, [cookie("session", SESSION_VALUE)])
        self.assertEqual(site_sessions.save(SITE, kept.call, pending=True), 1)
        with mock.patch.object(site_sessions, "_write", side_effect=OSError("disk full")):
            self.assertEqual(site_sessions.settle(SITE, signed_in=True), "dropped")
        self.assertFalse(site_sessions.has(SITE))
        real = site_sessions._write

        def write(home, key, data):                            # the guard's own capture is written; the keep is not
            if data["sites"]:
                raise OSError("disk full")
            return real(home, key, data)
        with mock.patch.object(site_sessions, "_write", write):
            self.step()
        self.assertFalse(self.view.last.site_session_saved)
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": False, "released": 1}])

    def test_d14_done_is_refused_for_a_card_whose_posting_cannot_be_opened(self):
        with self.database:                                    # a sign-in card of the kind that has "I've signed in"
            access_outcomes.record(self.database, self.row(), "AUTHENTICATION_REQUIRED", self.now, reason="SIGN_IN")
        self.assertIn('action="/access/done', self.cards(self.page("/approvals"))[0])
        payload = dict(json.loads(self.row()["payload"]), url="http://jobs.ashbyhq.com/synthetic/role-123")
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), self.key))
        self.database.commit()
        before = (self.row(), self.waits())
        # Owner, 2026-11-14: the card still has I've signed in where its posting cannot be opened here.
        self.assertEqual(self.cards(self.page("/approvals"))[0].count('action="/access/done'), 1)
        self.assertEqual((self.row(), self.waits()), before)

    # --- owner's picks on review round 3: D15, D17, D18, D19 ------------------------------------------

    def next_read_restores(self):
        """The next read on the site, as its guard starts it: the saved cookies are put back, or nothing is."""
        self.assertIsNone(site_sessions.before_session(SITE))
        jar = FakeJar(URL)
        return site_sessions.restore(SITE, jar.call)["outcome"], jar.value("session")

    def test_d15_a_save_that_finishes_after_the_cap_is_recorded_as_saved(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        real = site_sessions._write

        def slow(home, key, data):                             # the keep is still being written when the cap passes
            if data["sites"]:
                time.sleep(0.8)
            return real(home, key, data)
        with mock.patch.object(live_view, "SETTLE_SECONDS", 0.2), mock.patch.object(site_sessions, "_write", slow):
            self.step()
        # The store, the live view's record, the saved event and the next read all say the same thing.
        self.assertTrue(site_sessions.has(SITE))
        self.assertTrue(self.view.last.site_session_saved)
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": True, "released": 1}])
        self.assertEqual(self.next_read_restores(), ("restored", SESSION_VALUE))

    def test_d22_a_press_during_a_slow_save_never_opens_a_second_view_on_a_finished_step(self):
        import threading
        self.session(stop("AUTHENTICATION_REQUIRED"))
        real, writing = site_sessions._write, threading.Event()

        def slow(home, key, data):
            if data["sites"]:
                writing.set()
                time.sleep(1.5)
            return real(home, key, data)
        answers = {}
        with mock.patch.object(live_view, "SETTLE_SECONDS", 0.2), mock.patch.object(site_sessions, "_write", slow):
            target, _ = self.press_step()
            self.start_live(target)
            done = threading.Thread(target=lambda: answers.update(first=self.end_live("done")))
            done.start()
            self.assertTrue(writing.wait(10))
            deadline = time.monotonic() + 5
            while self.view.session is not None and time.monotonic() < deadline:
                time.sleep(0.02)                               # the browser is free; the save is still being written
            self.assertIsNone(self.view.session)
            self.assertEqual(self.events(user_step.FINISHED_EVENT), [])
            # Sign in pressed again, on the card and on the view left on the page; Done pressed again.
            again = self.post("/access/step", self.key)
            self.assertIn(access_cards.GONE, again)
            self.assertNotIn("live-view", again)
            self.assertEqual(self.view.targets, {})
            stale = self.client.post(f"/live-view/{target}/start", headers=ORIGIN)
            self.assertEqual((stale.status_code, stale.json()["words"]), (404, access_cards.GONE))
            second = self.client.post("/live-view/end?reason=done", headers=ORIGIN)
            done.join(20)
        self.assertFalse(done.is_alive())
        self.assertEqual(len(self.guard_configs), 1)           # no second live session was ever started
        self.assertIsNone(self.view.session)
        self.assertEqual(answers["first"]["reason"], "done")
        self.assertIn(second.status_code, (200, 409))          # the same ending, or nothing left to end
        if second.status_code == 200:
            self.assertEqual(second.json()["reason"], "done")
        # One finished event, one release, and the record says what the store holds.
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": True, "released": 1}])
        self.assertTrue(site_sessions.has(SITE) and self.view.last.site_session_saved)
        self.assertEqual([(wait["state"], wait["outcome"]) for wait in self.waits()], [("RESOLVED", "released")])
        self.assertEqual(self.view.finishing, set())
        self.assertEqual(len(self.events(access_outcomes.RELEASED_EVENT)) if hasattr(access_outcomes, "RELEASED_EVENT") else 1, 1)

    def d21_state(self):
        """A met the account wall and is uncertain; B and C are held behind it."""
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        b, c = self.ingest("456"), self.ingest("457")
        self.cycle()
        self.assertEqual({self.waits(key)[0]["source"] for key in (b, c)}, {"host:" + self.key})
        self.uncertain(self.key)
        return b, c

    def frozen(self, key):
        return json.dumps([self.row(key), self.waits(key), self.events("ACCESS_WAIT_RECORDED", key)], sort_keys=True, default=str)

    def test_d21_signing_in_on_a_card_led_by_a_held_job_does_not_loop_back_behind_the_uncertain_job(self):
        posted = []
        watcher = desktop_alerts.Watcher(home=None, root=None, read=lambda: dashboard.read_connection(None),
                                         provider=notifications.Desktop(post=lambda title, text, href: posted.append(text) or True),
                                         combine_over=desktop_alerts.COMBINE_OVER)
        watcher.tick()
        b, c = self.d21_state()
        a_before = self.frozen(self.key)
        # The state the user sees: one card, led by a held job, counted once everywhere.
        approvals = self.page("/approvals")
        [card] = self.cards(approvals)
        self.assertIn(">Sign in</button>", card)
        watcher.tick()
        self.assertEqual((self.needs()["sign_ins"], self.chip(approvals), len(self.cards(self.page("/"))), watcher.total, posted),
                         (1, 1, 1, 1, ["1 sign-in or human check to do"]))
        lead = b if 'value="%s"' % b in card.split('action="/access/step')[1].split("</form>")[0] else c
        self.assertEqual(self.step(lead)["reason"], "done")
        self.assertEqual(self.events(user_step.FINISHED_EVENT, lead), [{"purpose": "sign_in", "site_session_saved": True, "released": 2}])
        # The next three rounds: B and C each get one session of their own, with the saved sign-in.
        seen = []
        for _ in range(3):
            self.advance()
            self.cycle()
            while True:
                request = self.claim()
                if request is None or request["payload"]["operation"] != "inspect":
                    break
                key = request["payload"]["job"]["job_key"]
                seen.append(key)
                self.assertEqual(site_sessions.restore(SITE, FakeJar(URL).call)["outcome"], "restored")
                desktop_adapter.complete(self.database, dict(request, result=self.review(self.row(key))), self.now)
        self.assertEqual(sorted(seen), sorted([b, c]))
        for key in (b, c):                                     # never held again behind A
            self.assertEqual([(wait["source"], wait["state"], wait["outcome"]) for wait in self.waits(key)],
                             [("host:" + self.key, "RESOLVED", "released")])
            self.assertNotIn(self.row(key)["blocker"], access_outcomes.WALL_REASONS)
        self.assertEqual(self.frozen(self.key), a_before)      # A: not retried, not released, not closed
        self.assertEqual(self.requests_for(self.key), 1)
        # The card does not come back; what is left (A, being checked) is shown and counted nowhere.
        watcher.tick()
        approvals = self.page("/approvals")
        self.assertEqual([access_cards.NOT_ABANDONED in card for card in self.cards(approvals)], [True])
        self.assertEqual((self.needs()["sign_ins"], self.chip(approvals), watcher.total, len(posted)), (0, 0, 0, 1))

    def requests_for(self, key):
        return self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE job_key=? AND operation<>'submit'", (key,)).fetchone()[0]

    def test_d21_the_card_comes_back_only_when_a_freed_job_meets_the_sign_in_itself(self):
        b, c = self.d21_state()
        a_before = self.frozen(self.key)
        self.advance()
        self.cycle()                                           # nothing holds the site: both are freed for a session
        first = self.claim()
        self.assertIsNotNone(first)
        met = first["payload"]["job"]["job_key"]
        self.assertIn(met, (b, c))
        desktop_adapter.complete(self.database, dict(first, result=stop("AUTHENTICATION_REQUIRED")), self.now)
        self.advance()
        self.cycle()
        self.assertEqual((self.waits(met)[-1]["source"], self.waits(met)[-1]["state"]), ("session", "WAITING"))
        self.assertEqual(access_outcomes.host_holder(self.database, SITE, self.now)["job_key"], met)   # its own wait holds the site
        self.assertTrue(any(">Sign in</button>" in card for card in self.cards(self.page("/approvals"))))
        self.assertEqual(self.frozen(self.key), a_before)

    def test_d21_control_a_certain_job_waiting_on_a_sign_in_still_holds_its_site(self):
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        b = self.ingest("456")
        self.assertEqual(access_outcomes.host_holder(self.database, SITE, self.now)["job_key"], self.key)
        for _ in range(3):
            self.advance()
            self.cycle()
            self.assertIsNone(self.claim())                    # no session for the job behind it
        self.assertEqual([(wait["source"], wait["state"]) for wait in self.waits(b)], [("host:" + self.key, "WAITING")])

    def test_d15_control_a_write_that_fails_is_recorded_as_not_saved_and_nothing_is_restored(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        real = site_sessions._write

        def failing(home, key, data):
            if data["sites"]:
                time.sleep(0.8)
                raise OSError("disk full")
            return real(home, key, data)
        with mock.patch.object(live_view, "SETTLE_SECONDS", 0.2), mock.patch.object(site_sessions, "_write", failing):
            self.step()
        self.assertFalse(site_sessions.has(SITE))
        self.assertFalse(self.view.last.site_session_saved)
        self.assertEqual(self.events(user_step.FINISHED_EVENT), [{"purpose": "sign_in", "site_session_saved": False, "released": 1}])
        self.assertEqual(self.next_read_restores(), ("none", None))

    def test_d17_a_held_job_that_leads_the_card_says_the_leads_sentence_and_done_follows_the_same_rule(self):
        with self.database:                                    # a sign-in of the kind whose card has "I've signed in"
            access_outcomes.record(self.database, self.row(), "AUTHENTICATION_REQUIRED", self.now, reason="SIGN_IN")
        second, third = self.ingest("456"), self.ingest("457")
        self.cycle()                                           # both held behind the first job
        self.uncertain(self.key)
        before = (self.row(), self.waits())
        [card] = self.cards(self.page("/approvals"))
        head = card.split("Also waiting on this site:")[0]
        self.assertIn("Senior Engineer", head)                 # a held job leads; it offers Sign in
        self.assertIn(">Sign in</button>", card)
        self.assertIn("The site needs you to sign in.", head)
        self.assertNotIn("Another application on this site", card)
        lead = second if 'value="%s"' % second in card.split('action="/access/step')[1].split("</form>")[0] else third
        self.assertEqual(self.app_job(lead)["todo"], "The site needs you to sign in.")
        answer = self.post("/access/done", lead)               # the button the card shows, by the card's own rule
        self.assertNotIn(access_cards.NOT_OFFERED, answer)
        self.assertEqual((self.row(second)["state"], self.row(third)["state"]), ("RETRY_WAIT", "RETRY_WAIT"))
        self.assertEqual((self.row(), self.waits()), before)   # the uncertain job is never released

    def test_d18_no_sentence_says_next_run(self):
        root = Path(access_cards.__file__).parent
        files = sorted(root.glob("*.py")) + sorted((root / "dashboard_assets").rglob("*.html")) + sorted((root / "dashboard_assets").rglob("*.js"))
        text = {path.name: path.read_text(encoding="utf-8") for path in files}
        pattern = re.compile(r"(its|the)\s+next\s+run\b", re.I)
        self.assertIsNotNone(pattern.search("Waiting for its next run. Nothing you need to do"))   # the control
        self.assertIsNotNone(pattern.search("Continues on the next run"))
        for name, words in (("applications_page.py", "Continues on the next round"), ("applications_page.py", "on its next round, and asks again"),
                            ("failures_page.py", "Waiting for its next round.")):
            self.assertIn(words, text[name])                   # the scan reads the files that held them
        self.assertEqual({name for name, body in text.items() if pattern.search(body)}, set())

    def test_d19_overviews_more_counts_the_cards_that_need_you_beyond_the_limit(self):
        for number in range(7):                                # seven sites, one waiting job each
            key = self.ingest("7%d" % number)
            payload = dict(json.loads(self.row(key)["payload"]), url="https://site%d.example.com/role-7%d" % (number, number))
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), key))
            self.database.commit()
            with self.database:
                access_outcomes.record(self.database, self.row(key), "AUTHENTICATION_REQUIRED", self.now)
        self.assertEqual(self.needs()["sign_ins"], 7)
        page = self.page("/")
        self.assertEqual(len(self.cards(page)), access_cards.OVERVIEW_LIMIT)
        self.assertIn("2 more are waiting.", page)
        for number in (5, 6):                                  # the two beyond the limit may already be with the employer
            self.uncertain("synthetic company:role-7%d" % number)
        self.assertEqual(self.needs()["sign_ins"], 5)
        self.assertNotIn("more are waiting", self.page("/"))
        self.assertNotIn("more is waiting", self.page("/"))

    # --- round 6: the words of a deadline, the confirmation hint, the full stop ----------------------

    def set_deadline(self, key, hours):
        deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()
        self.database.execute("UPDATE application_access_waits SET deadline=? WHERE job_key=? AND state='WAITING'", (deadline, key))
        self.database.execute("UPDATE application_jobs SET next_at=? WHERE job_key=?", (deadline, key))
        self.database.commit()
        local = dt.datetime.fromisoformat(deadline).astimezone(overview_page.ZoneInfo("America/Los_Angeles"))
        return "%s %d at %s" % (local.strftime("%b"), local.day, local.strftime("%H:%M"))

    def test_r6_a_deadline_is_always_a_date_and_a_time_and_a_passed_one_says_what_is_true(self):
        self.session(stop("AUTHENTICATION_REQUIRED"))
        for hours in (30, 20, 3):                              # the day after tomorrow, tomorrow, today
            words = self.set_deadline(self.key, hours)
            self.supported = True
            for page in (self.page("/"), self.page("/approvals")):
                self.assertIn("Waits for you until %s, then closes." % words, self.cards(page)[0])
            self.assertEqual(self.app_job()["todo"], ASK_WORDS + " Waits for you until %s, then closes." % words)
            self.supported = False
            for page in (self.page("/"), self.page("/approvals"), self.post("/access/step", "nobody")):
                for card in self.cards(page):
                    self.assertIn("Waits for you until %s, then closes." % words, card)
            self.assertEqual(self.app_job()["todo"], ASK_WORDS + " Waits for you until %s, then closes." % words)
            shown = " ".join(self.cards(self.page("/approvals"))) + self.app_job()["todo"]
            for wrong in ("tomorrow", "yesterday", "Closes on 1", "Closes on 0", "Closes on 2", "until 1", "until 0", "until 2"):
                self.assertNotIn(wrong, shown)
        # Past its deadline, before any round has closed it: nothing says it still waits until a past time.
        self.set_deadline(self.key, -20)
        for supported in (True, False):
            self.supported = supported
            card = self.cards(self.page("/approvals"))[0]
            self.assertIn("Closes on Sidedoor's next round.", card)
            for wrong in ("Waits for you until", "unless you abandon", "yesterday"):
                self.assertNotIn(wrong, card + self.app_job()["todo"])
            self.assertEqual(self.app_job()["todo"], ASK_WORDS + " Closes on Sidedoor's next round.")
        self.now = desktop_adapter.later(self.now, 60)
        with self.database:                                    # and the next round does close it
            self.assertEqual(access_outcomes.expire(self.database, applications.stamp()), [self.key])

    def test_r6_the_missing_confirmation_email_card_does_not_send_the_user_to_that_email(self):
        with self.database:
            access_outcomes.record(self.database, self.row(), "AUTHENTICATION_REQUIRED", self.now, reason="CONFIRMATION_EMAIL_MISSING")
        [card] = self.cards(self.page("/approvals"))
        self.assertIn("The site's confirmation email did not arrive. Confirm it yourself.", card)
        self.assertIn("Ask the site to send it again, or confirm your account on the site. Then press I've confirmed it.", card)
        self.assertIn(">I've confirmed it</button>", card)
        self.assertNotIn("Open the link in the site's email", card)

    def test_the_needs_you_line_is_the_same_in_the_row_and_the_notification(self):
        # Owner, 2026-11-14 (E15.T14 L7): the count lines are parallel in form ("N sign-ins or human checks to do",
        # as "N questions to answer"); this replaces R6's full sentence with its full stop.
        posted = []
        watcher = desktop_alerts.Watcher(home=None, root=None, read=lambda: dashboard.read_connection(None),
                                         provider=notifications.Desktop(post=lambda title, text, href: posted.append(text) or True),
                                         combine_over=desktop_alerts.COMBINE_OVER)
        watcher.tick()
        self.session(stop("AUTHENTICATION_REQUIRED"))
        watcher.tick()
        self.assertEqual(posted, ["1 sign-in or human check to do"])
        self.assertRegex(self.page("/"), r'<span class="overview-needs-what">sign-in or human check to do</span>')
        check = self.ingest("900")
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
        watcher.tick()
        self.assertEqual(posted[-1], "2 sign-ins or human checks to do")
        self.assertRegex(self.page("/"), r"sign-ins or human checks to do</span>")
        for row in overview_page.NEEDS_ROWS:                   # the noun rows keep no stop; no sentence has two
            self.assertFalse(row[1].endswith("..") or row[2].endswith(".."))

    def test_r6_the_chips_comment_says_what_the_code_counts(self):
        source = Path(access_cards.__file__).with_name("approvals_page.py").read_text(encoding="utf-8")
        note = source[source.index("# E15.T2 (owner, 2026-11-13): the applications waiting on a sign-in"):source.index('("sign-ins", "Sign-ins and human checks", None)')]
        self.assertIn("card_count", note)
        self.assertNotIn("waiting jobs, each once", note)

    # --- notifications --------------------------------------------------------------------------------

    def test_the_tray_count_and_a_notification_include_the_new_card(self):
        posted = []
        watcher = desktop_alerts.Watcher(home=None, root=None, read=lambda: dashboard.read_connection(None),
                                         provider=notifications.Desktop(post=lambda title, text, href: posted.append((title, text, href)) or True),
                                         combine_over=desktop_alerts.COMBINE_OVER)
        watcher.tick()                                         # the first check of an install: taken as known
        self.assertEqual((posted, watcher.total), ([], 0))
        self.session(stop("AUTHENTICATION_REQUIRED"))
        watcher.tick()
        self.assertEqual(posted, [("Needs you", "1 sign-in or human check to do", "/approvals#sign-ins")])
        self.assertEqual(watcher.total, 1)
        self.assertIn('id="sign-ins"', self.page("/approvals"))
        # The user's own actions: none of the new routes quiets a line, and a count going down never notifies.
        for path in ("/access/abandon", "/access/done", "/access/step", "/access/try-again", "/live-view/end",
                     "/live-view/abc/start", "/settings/account"):
            before = dict(desktop_alerts._acted)
            desktop_alerts.user_acted(path)
            self.assertEqual(desktop_alerts._acted, before, path)
        self.step()
        watcher.tick()
        self.assertEqual((len(posted), watcher.total), (1, 0))
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)   # the site asks again: told again
        watcher.tick()
        self.assertEqual((len(posted), watcher.total), (2, 1))

    # --- words ----------------------------------------------------------------------------------------

    def test_no_page_in_the_flow_shows_a_code_an_em_dash_or_automation(self):
        check, passkey = self.ingest("456"), self.ingest("789")
        self.session(stop("AUTHENTICATION_REQUIRED"), self.key)
        with self.database:
            access_outcomes.record(self.database, self.row(check), "CAPTCHA_REQUIRED", self.now)
            access_outcomes.record(self.database, self.row(passkey), "AUTHENTICATION_REQUIRED", self.now,
                                   result=stop("AUTHENTICATION_REQUIRED", PASSKEY_WALL))
        pages = [self.page(path) for path in ("/", "/approvals", "/applications", "/settings")]
        pages += [self.page("/applications?job=" + key) for key in (self.key, check, passkey)]
        target, fragment = self.press_step()
        pages += [fragment, self.page("/live-view/" + target)]
        said = [self.start_live(target)["words"], self.end_live("cancel")["words"]]
        self.start_live(self.press_step()[0])
        said.append(self.end_live("done")["words"])
        pages += [self.page(path) for path in ("/", "/approvals", "/applications", "/applications?job=" + self.key)]
        pages += [self.post("/access/step", self.key), self.post("/access/abandon", passkey), self.post("/access/try-again", passkey)]
        self.assert_plain(*pages)
        words = said + list(live_view.ALL_WORDS) + list(access_outcomes.WAIT_REASONS.values()) + [
            access_cards.GONE, access_cards.STEP_UNAVAILABLE, access_cards.APPLY_YOURSELF, access_cards.RELEASED_ONE,
            access_outcomes.HOST_HELD_WORDS, access_outcomes.HOST_RELEASED_WORDS]
        for sentence in words:
            self.assertEqual(RAW_CODE.findall(sentence), [], sentence)
            self.assertNotIn("—", sentence)
            self.assertIsNone(INTERNAL.search(sentence), sentence)
        # The cards and the live view's block themselves use no internal term either.
        for page in pages[:3] + [fragment]:
            for card in self.cards(page) + ([page] if "data-live-view" in page and "<article" not in page else []):
                text = re.sub(r"<[^>]+>", " ", re.sub(r'="[^"]*"', "", card))
                self.assertIsNone(INTERNAL.search(text), text)


class Swaps:
    """A page as htmx sees it, enough to answer "which element does this request replace, and how much of
    the page": elements with their attributes and place in the source, hx-target handed down from the
    nearest ancestor that has one ("this" is the element itself, "#id ..." a selector), and the swap applied
    to the source. Only what these pages use (id, tag and class selectors, innerHTML and outerHTML)."""
    VOID = {"input", "meta", "link", "br", "img", "hr"}

    class Node(dict):
        __hash__ = object.__hash__
        __eq__ = object.__eq__

    def __init__(self, source):
        self.load(source)

    def load(self, source):
        from html.parser import HTMLParser
        self.source, self.nodes = source, []
        starts = [0]
        for line in source.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        page, stack = self, []

        class Reader(HTMLParser):
            def place(reader):
                line, column = reader.getpos()
                return starts[line - 1] + column

            def handle_starttag(reader, tag, attributes):
                node = Swaps.Node((name, value or "") for name, value in attributes)
                node.tag, node.parent, node.start = tag, (stack[-1] if stack else None), reader.place()
                node.open_end = node.start + len(reader.get_starttag_text())
                node.end = node.close = node.open_end
                page.nodes.append(node)
                if tag not in Swaps.VOID:
                    stack.append(node)

            def handle_endtag(reader, tag):
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index].tag == tag:
                        node = stack[index]
                        del stack[index:]
                        node.close = reader.place()
                        node.end = source.index(">", node.close) + 1
                        return
        Reader(convert_charrefs=False).feed(source)

    def all(self, tag=None):
        return [node for node in self.nodes if tag is None or node.tag == tag]

    def roots(self):
        return [node for node in self.nodes if node.parent is None]

    def first(self, tag, attribute):
        return next(node for node in self.nodes if node.tag == tag and attribute in node)

    def by_id(self, identifier):
        return next((node for node in self.nodes if node.get("id") == identifier), None)

    def inside(self, node):
        return [other for other in self.nodes if node.start <= other.start < node.end]

    def html(self, node):
        return self.source[node.start:node.end]

    def text(self, node):
        return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(self.html(node))).split())

    def target_of(self, node):
        holder = node
        while holder is not None and "hx-target" not in holder:
            holder = holder.parent                             # htmx hands an hx-target down to its descendants
        if holder is None or holder["hx-target"] == "this":
            return node if holder is None else holder
        parts = holder["hx-target"].split()
        found = self.by_id(parts[0][1:])
        for part in parts[1:]:
            found = next(other for other in self.inside(found) if other is not found
                         and (other.get("class") == part[1:] if part.startswith(".") else other.tag == part))
        return found

    def swap(self, target, how, markup):
        start, end = (target.start, target.end) if how == "outerHTML" else (target.open_end, target.close)
        identifier = target.get("id")
        self.load(self.source[:start] + markup + self.source[end:])


class FakeJar:
    """What a session's Chrome answers to site_sessions' restore: a jar that starts empty (Chrome exited)."""

    def __init__(self, url, jar=()):
        self.url, self.jar = url, list(jar)

    def call(self, code):
        given = json.loads(re.search(r"const input = (.*);\n", code).group(1))
        if given["sidedoor"] == "site-session-capture":
            return [{"url": self.url, "cookies": list(self.jar)}]
        self.jar.extend(given["cookies"])
        return [{"restored": len(given["cookies"]), "kept": 0, "failed": 0}]

    def value(self, name):
        return next((item["value"] for item in self.jar if item["name"] == name), None)


if __name__ == "__main__":
    unittest.main()
