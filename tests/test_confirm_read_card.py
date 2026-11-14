"""A sign-in or a human check met while Sidedoor confirms an application it may already have sent
(owner's pick S7 a, 2026-11-13): a card, the user's step, and then ONLY the confirming read again.
Never a new application session, a re-arm or a second submit."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import datetime as dt
import json
import unittest
from unittest import mock

import access_cards
import access_outcomes
import applications
import confirm_read
import desktop_adapter
import live_view
import overview_page
import application_browser
import site_sessions
import test_site_sessions as sessions
import time
import test_access_cards_e15_t2 as cards_fixture
import test_access_outcomes_e15_t1 as walls
import user_step

CHECK = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "A person has to do the human check"}}
SIGN_IN = {"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "A person has to sign in"}}
CONFIG = {"timezone": "America/Los_Angeles"}


class TheSequence(unittest.TestCase):
    """Through the real applications processing loop (E15.T1's harness)."""
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def sent_with_outcome_unknown(self):
        """Submit was pressed (armed) and the session ended without a known outcome."""
        request = self.run_until_submit()
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result={"acknowledged": False}), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertTrue(applications.uncertain_submission(self.database, self.key))

    def next_read(self, tries=8):
        """Cycle until a request is claimable and return it (None when nothing starts)."""
        for _ in range(tries):
            self.cycle()
            request = desktop_adapter.claim(self.database, self.root, "confirm-fixture", self.now)
            if request:
                return request
            self.advance(1200)
        return None

    def blocked_read(self, result=CHECK):
        read = self.next_read()
        self.assertEqual(read["payload"]["operation"], "reconcile")
        self.assertTrue(desktop_adapter.complete(self.database, dict(read, result=result), self.now)["accepted"])
        self.advance(60)
        self.cycle()

    def counts(self):
        operations = [row["operation"] for row in self.requests()]
        row = self.row()
        return {"reconcile": operations.count("reconcile"), "submit": operations.count("submit"),
                "inspect": operations.count("inspect"), "other": len([o for o in operations if o not in ("reconcile", "submit", "inspect", "verify")]),
                "attempts": row["attempts"], "reads": row["reconcile_attempts"],
                "tries": self.database.execute("SELECT COUNT(*) FROM application_attempts WHERE job_key=?", (self.key,)).fetchone()[0],
                "armed": self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE job_key=? AND armed_at IS NOT NULL",
                                               (self.key,)).fetchone()[0]}

    def view(self):
        reader = overview_page.Reader(self.database)
        with mock.patch.object(user_step, "available", return_value=True), \
                mock.patch.object(user_step, "_live", {"view": mock.Mock(), "database": None, "home": None, "offered": {}}):
            shown = access_cards.view(reader, CONFIG, None, now=dt.datetime.fromisoformat(self.now))
            return shown, access_cards.card_count(reader)

    def finish_step(self, wait_id=None, purpose=user_step.HUMAN_CHECK):
        """What the live view's Done does (`user_step._ended` -> `access_cards.step_finished`)."""
        return access_cards.step_finished(self.database, confirm_read.key(self.key), purpose, self.now, True, wait_id)

    def test_the_users_step_leads_only_to_the_confirming_read_again(self):
        self.sent_with_outcome_unknown()
        # Control: uncertain, no blocked read yet: no card of this kind, nothing counted.
        self.assertEqual(confirm_read.waiting(self.database), [])
        self.assertEqual(self.view()[1], 0)
        before = self.counts()
        self.blocked_read()
        # The card appears and is counted; the job stays in RECONCILE and the blocked read spent nothing.
        row = self.row()
        [wait] = confirm_read.waiting(self.database)
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("RECONCILE", "CAPTCHA_REQUIRED", wait["deadline"]))
        self.assertEqual(wait["deadline"], desktop_adapter.later(wait["since"], access_outcomes.WAIT_SECONDS))
        after = self.counts()
        self.assertEqual(after, dict(before, reads=before["reads"] + 1))   # the one read ran and counted as one
        self.assertEqual(self.waits(), [])                                           # never an access wait
        self.assertEqual(access_outcomes.waiting(self.database), [])
        shown, count = self.view()
        [card] = shown["cards"]
        self.assertEqual((count, shown["count"], card["counted"], card["confirm"]), (1, 1, True, True))
        self.assertEqual(card["key"], confirm_read.key(self.key))
        self.assertEqual(card["need"], "The site needs a human check. Do not apply again.")   # the owner's words, all of them
        self.assertEqual(card["step"], {"purpose": user_step.HUMAN_CHECK, "label": "Do the human check"})
        self.assertEqual((card["abandon"], card["apply"], card["url"], card["done_label"], card["others"]), (False, None, None, None, []))
        self.assertIsNone(card["closes"])
        # While it waits nothing starts, on any day before the deadline.
        for hours in (1, 24, 24 * 5):
            self.advance(hours * 3600)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "confirm-fixture", self.now))
        self.assertEqual(self.counts(), after)
        # No button or rule of an application card can act on it, by either key.
        for name in (self.key, confirm_read.key(self.key)):
            self.assertEqual(access_cards.abandon(self.database, name, self.now)["text"], access_cards.GONE)
            self.assertEqual(access_cards.done(self.database, name, self.now)["text"], access_cards.GONE)
            self.assertFalse(access_cards.try_again(self.database, name, self.now)["reopened"])
            with self.database:
                self.assertEqual(access_outcomes.resume(self.database, name, self.now), [])
                self.assertFalse(access_outcomes.abandon(self.database, name, self.now))
        self.assertEqual((dict(self.row()), self.counts()), (dict(row), after))
        # A late Done for another wait changes nothing.
        self.assertEqual(self.finish_step(wait_id=wait["id"] + 100), [])
        self.assertEqual(dict(self.row()), dict(row))
        # The user does the step and presses Done: only the next read's time changes.
        self.assertEqual(self.finish_step(wait_id=wait["id"]), [confirm_read.key(self.key)])
        due = self.row()
        self.assertEqual({name: due[name] for name in due.keys() if due[name] != row[name]}, {"next_at": self.now})
        self.assertEqual(self.view()[1], 0)
        self.assertEqual(self.finish_step(wait_id=wait["id"]), [])                    # a second Done: nothing left
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        # Next round: exactly one reconcile read, zero application sessions, zero submits.
        read = self.next_read(tries=1)
        self.assertEqual(read["payload"]["operation"], "reconcile")
        self.assertEqual(self.counts(), dict(after, reconcile=after["reconcile"] + 1))
        self.assertIsNone(desktop_adapter.claim(self.database, self.root, "confirm-fixture", self.now))
        # That read counts as one too: here the site asks again, and the card comes back.
        self.assertTrue(desktop_adapter.complete(self.database, dict(read, result=CHECK), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.counts(), dict(after, reconcile=after["reconcile"] + 1, reads=after["reads"] + 1))
        self.assertEqual(self.row()["state"], "RECONCILE")
        self.assertEqual(self.view()[1], 1)
        self.assertEqual(len(confirm_read.waiting(self.database)), 1)

    def test_if_the_user_never_acts_the_confirming_reads_carry_on_and_run_out_as_before(self):
        self.sent_with_outcome_unknown()
        self.blocked_read()
        start = self.counts()
        waits = 0
        for _ in range(applications.RECONCILE_MAX_ATTEMPTS + 2):
            wait = confirm_read.waiting(self.database)
            if not wait:
                break
            waits += 1
            self.now = wait[0]["deadline"]                      # seven days pass, nobody acts
            read = self.next_read(tries=1)
            if read is None:                                    # the reads have run out: no further read
                break
            self.assertEqual(read["payload"]["operation"], "reconcile")
            self.assertTrue(desktop_adapter.complete(self.database, dict(read, result=CHECK), self.now)["accepted"])
            self.advance(60)
            self.cycle()
        self.cycle()
        end, row = self.counts(), self.row()
        # Every read counted as one, and the reads ran out by the reconcile path's own limit (in this
        # fixture recovery is off, so its own earlier limit applies; never more than the 10).
        self.assertEqual(end["reads"], waits)
        self.assertEqual(end["reconcile"], end["reads"])
        self.assertTrue(1 <= end["reads"] <= applications.RECONCILE_MAX_ATTEMPTS, end)
        self.assertEqual((end["submit"], end["inspect"], end["armed"], end["attempts"], end["tries"]),
                         (start["submit"], start["inspect"], start["armed"], start["attempts"], start["tries"]))
        # Out of reads: held as the reconcile path always holds it, never closed, no card left.
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", None))
        self.assertEqual(len(self.events(applications.RECONCILE_SPENT_EVENT)), 1)
        self.assertEqual(confirm_read.waiting(self.database), [])
        self.assertEqual(self.view()[1], 0)
        self.assertTrue(applications.uncertain_submission(self.database, self.key))

    def test_a_sign_in_met_by_the_confirming_read_gets_the_same_card(self):
        self.sent_with_outcome_unknown()
        self.blocked_read(SIGN_IN)
        [card] = self.view()[0]["cards"]
        self.assertEqual(card["need"], "The site needs you to sign in. Do not apply again.")
        self.assertEqual(card["step"], {"purpose": user_step.SIGN_IN, "label": "Sign in"})
        self.assertEqual(self.waits(), [])
        # It holds no site: another job there still gets its session.
        self.assertIsNone(access_outcomes.host_holder(self.database, "jobs.ashbyhq.com", self.now))
        # Where the step cannot be shown the card says only what is true and offers nothing.
        reader = overview_page.Reader(self.database)
        with mock.patch.object(user_step, "available", return_value=False):
            [plain] = access_cards.view(reader, CONFIG, None, now=dt.datetime.fromisoformat(self.now))["cards"]
        self.assertEqual((plain["need"], plain["step"], plain["abandon"], plain["apply"]),
                         (access_cards.CONFIRM_WORDS["SIGN_IN"], None, False, None))

    def test_the_confirming_reads_use_the_saved_site_session(self):
        self.assertIn("reconcile", site_sessions.OPERATIONS)
        self.assertIn("verify", site_sessions.OPERATIONS)
        self.sent_with_outcome_unknown()
        read = self.next_read()
        self.assertEqual(read["payload"]["operation"], "reconcile")
        self.assertEqual(site_sessions.site_key(read["payload"].get("url")), "jobs.ashbyhq.com")

    def test_the_sentences_are_plain(self):
        for words in (list(access_cards.CONFIRM_WORDS.values())
                      + [access_cards.CONFIRM_GONE, confirm_read.action("CAPTCHA_REQUIRED"), confirm_read.action("MFA_REQUIRED")]):
            self.assertNotIn(chr(0x2014), words)
            self.assertNotIn("automation", words.lower())
            self.assertNotRegex(words, cards_fixture.RAW_CODE)
            self.assertNotIn("apply yourself", words.lower())


def forbidden(words):
    """What no sentence on this card may tell the user: to apply (other than "Do not apply again"), to
    abandon, or to open the posting."""
    low = words.lower().replace("do not apply again", "")
    return [word for word in ("apply", "abandon", "posting", "link on the card") if word in low]


class WhatTheLiveViewSaysOnThisCard(unittest.TestCase):
    """The live view's sentences are written for application cards. On this card (no posting link; the
    application may already be with the employer) none of them may say to apply."""

    def target(self, apply_link=False):
        return live_view.Target("id", confirm_read.key("job"), "https://jobs.example.com/job", user_step.HUMAN_CHECK,
                                "jobs.example.com", apply_link=apply_link)

    def test_a_failed_start_says_only_try_again(self):
        self.assertEqual(live_view.said(self.target(), live_view.NOT_SHOWN_WORDS), "Sidedoor couldn't open its browser. Try again.")
        # An application card keeps its sentence.
        self.assertEqual(live_view.said(self.target(apply_link=True), live_view.NOT_SHOWN_WORDS), live_view.NOT_SHOWN_WORDS)
        self.assertEqual(live_view.said(None, live_view.NOT_SHOWN_WORDS), live_view.NOT_SHOWN_WORDS)
        # Through `start`, whichever failure the attempt ended on.
        for failure in (live_view.Failed(live_view.NOT_SHOWN, live_view.NOT_SHOWN_WORDS),
                        live_view.Failed(live_view.NOT_SHOWN, live_view.NO_BACKGROUND_WORDS, final=True),
                        live_view.Failed(live_view.UNAVAILABLE, live_view.NOT_AVAILABLE_WORDS),
                        live_view.Failed(live_view.BUSY, live_view.BUSY_WORDS)):
            for apply_link in (False, True):
                view = mock.Mock(targets={"id": self.target(apply_link)}, _start=mock.AsyncMock(return_value=failure))
                answered = asyncio.run(live_view.LiveView.start(view, "id"))
                self.assertEqual((answered.reason, answered.final), (failure.reason, failure.final))
                if apply_link:
                    self.assertEqual(answered.words, failure.words)
                else:
                    self.assertEqual(forbidden(answered.words), [])

    def test_no_sentence_the_view_can_show_here_says_to_apply_abandon_or_open_the_posting(self):
        target = self.target()
        told = []
        for words in live_view.ALL_WORDS:
            if words == live_view.FAILURE_TODO:      # the Failures page's line: its own form is checked below
                continue
            shown = live_view.said(target, words)
            if forbidden(shown):
                told.append(shown)
        self.assertEqual(told, [])
        self.assertEqual(forbidden(live_view.FAILURE_TODO_NO_LINK), [])
        for words in (access_cards.CONFIRM_GONE, access_cards.STEP_UNAVAILABLE_HERE):
            self.assertEqual(forbidden(words), [])

    def test_the_failures_page_never_says_apply_yourself_for_this_cards_step(self):
        import sqlite3
        import failures_page
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        database.execute(live_view.TABLE)
        now = dt.datetime.now(dt.timezone.utc)
        for job, domain in ((confirm_read.key("sent"), "jobs.example.com"), ("ordinary", "boards.example.org")):
            database.execute("INSERT INTO live_view_failures(at,reason,domain,job_key,purpose) VALUES (?,?,?,?,?)",
                             (now.isoformat(), live_view.NOT_SHOWN, domain, job, user_step.HUMAN_CHECK))
        items = failures_page.live_view_failures(overview_page.Reader(database), now, lambda moment: "now")
        told = {site: next(json.dumps(item, default=str) for item in items if site in json.dumps(item, default=str))
                for site in ("jobs.example.com", "boards.example.org")}
        self.assertIn(live_view.FAILURE_TODO_NO_LINK, told["jobs.example.com"])
        self.assertNotIn("apply yourself", told["jobs.example.com"])
        self.assertIn(live_view.FAILURE_TODO, told["boards.example.org"])       # an application card's step is unchanged


class TheConfirmingReadAlwaysOpensTheSite(unittest.TestCase):
    """Through the real `application_browser.consume`: a saved session kept from an earlier cleared wall
    has expired. An application read is answered with that wall without a session, as before; a
    confirming read never is, so its card is raised only when that read itself met a wall."""
    setUp = sessions.Sessions.setUp
    read = sessions.Sessions.read

    def expired(self, wall="AUTHENTICATION_REQUIRED"):
        past = time.time() - 10 * sessions.DAY
        expiring = sessions.Chrome(jar=[sessions.cookie("auth", sessions.SECRET, sessions.SITE, expires=past + sessions.DAY)])
        self.assertEqual(site_sessions.user_step_ended(sessions.SITE, expiring.call, cleared=wall, now=past), 1)
        self.assertTrue(site_sessions.has(sessions.SITE))

    def confirming_read(self, result):
        request_id = self.fixture.queue("reconcile")

        def session(config, prompt, directory, timeout):
            self.started.append(config.get(site_sessions.CONFIG_KEY))
            sessions.write_output(directory, sessions.request_at(directory), result)
        summary = application_browser.consume(self.fixture.root, self.fixture.database, self.fixture.config, request_id, session)
        row = self.fixture.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        return summary, json.loads(row["result"]) if row["result"] else None

    def test_an_expired_saved_session_never_answers_a_confirming_read(self):
        self.expired()
        summary, result = self.confirming_read(CHECK)
        self.assertEqual(self.started, [sessions.SITE])              # the site was really opened, with the site's session in use
        self.assertEqual(summary["state"], "COMPLETE", summary)
        # What the read itself met is what is reported: never the old wall of the expired entry.
        self.assertEqual(result["blocked"]["code"], "CAPTCHA_REQUIRED")
        self.assertNotEqual(result["blocked"].get("next_action"), site_sessions.EXPIRED_ACTION)
        self.assertFalse(site_sessions.has(sessions.SITE))           # the expired entry is still dropped

    def test_an_expired_saved_session_and_no_wall_on_the_page_is_no_wall(self):
        self.expired("CAPTCHA_REQUIRED")
        summary, result = self.confirming_read({"blocked": {"code": "INSPECTION_INCOMPLETE", "next_action": "Read the page again"}})
        self.assertEqual(self.started, [sessions.SITE])
        self.assertNotIn((result or {}).get("blocked", {}).get("code"), confirm_read.WALLS)

    def test_control_an_application_read_still_takes_the_shortcut(self):
        self.expired()
        summary, result = self.read()
        self.assertEqual(self.started, [])                           # no session was started
        self.assertEqual((summary["state"], result["blocked"]["code"], result["blocked"]["next_action"]),
                         ("COMPLETE", "AUTHENTICATION_REQUIRED", site_sessions.EXPIRED_ACTION))


@unittest.skipUnless(cards_fixture.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class OnTheDashboard(unittest.TestCase):
    """The installed app's pages and routes (dashboard.create_app with its defaults)."""
    for _name in ("setUp", "job", "row", "wall", "page", "post", "step_available", "needs", "cards"):
        locals()[_name] = getattr(cards_fixture.AccessCardsTest, _name)
    del _name

    def confirming(self, key="sent", code="CAPTCHA_REQUIRED"):
        """A sent application whose confirming read met `code` (as `applications.reconcile` leaves it)."""
        self.job(key, "Acme", "Designer")
        with self.database:
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                                  "expires_at,armed_at,result,attempt_id) VALUES (?,'b',?,'submit','{}','CONSUMED',?,?,?,'{}',?)",
                                  ("armed-" + key, key, self.now, self.now, self.now, "attempt-" + key))
            deadline = confirm_read.record(self.database, self.row(key), code, self.now)
            self.assertTrue(applications.hold(self.database, self.row(key), code, confirm_read.action(code), self.now, "RECONCILE", deadline))
        self.assertTrue(applications.uncertain_submission(self.database, key))
        return key

    def test_the_card_is_shown_counted_and_offers_only_the_step(self):
        key = self.confirming()
        before = self.row(key)
        with self.step_available():
            for path in ("/", "/approvals"):
                [card] = self.cards(self.page(path))
                for shown in ("Acme", "Designer", "The site needs a human check. Do not apply again.", "Do the human check",
                              'action="/access/step"',
                              'value="%s"' % confirm_read.key(key)):
                    self.assertIn(shown, card, path)
                for absent in ("Abandon", "Apply yourself", "apply yourself", "Open the posting", "jobs.example.com",
                               'action="/access/done"', 'action="/access/abandon"', "closes", "may already have reached",
                               "If you do nothing", "while Sidedoor was", "finish checking"):
                    self.assertNotIn(absent, card, path)
                self.assertNotRegex(card, cards_fixture.RAW_CODE)
            self.assertEqual(self.needs()["sign_ins"], 1)
            # The application cards' routes change nothing, under either key.
            for name in (key, confirm_read.key(key)):
                for route in ("/access/abandon", "/access/done", "/access/try-again"):
                    self.client.post(route, data={"job_key": name, "at": "approvals"}, headers=cards_fixture.HTMX)
            self.assertEqual(self.row(key), before)
            self.assertEqual(len(confirm_read.waiting(self.database)), 1)
        # After the live view's Done every card says the same (owner, 2026-11-13), and it never says to apply.
        import live_view
        done = self.page("/access/section?at=approvals&ended=done")
        self.assertIn(live_view.ENDED_WORDS[live_view.DONE], done)

    def test_applications_never_says_nothing_to_do_while_the_card_waits(self):
        key = self.confirming()
        app_job = cards_fixture.AccessCardsTest.app_job
        with self.step_available():
            shown = app_job(self, key)
        self.assertEqual((shown["todo"], shown["todo_href"], shown["todo_label"]),
                         ("The site needs a human check. Do not apply again.", access_cards.APPROVALS_HREF, "Open on Approvals"))
        self.assertNotIn("Nothing to do", json.dumps(shown, default=str))
        self.assertEqual(shown["state"], cards_fixture.applications_page.STATE_WORDS["RECONCILE"])   # still being confirmed
        # Control: once the step is done (no card), the line is the ordinary one again.
        with self.database:
            self.assertTrue(confirm_read.done(self.database, key, self.now))
        self.assertIn("Nothing to do.", app_job(self, key)["todo"])

    def test_the_uncertain_card_with_no_blocked_read_is_unchanged_beside_it(self):
        """Control: a job that may already be with the employer and has an ordinary wait keeps its card
        with no buttons, not counted; the confirming read's card is counted on its own."""
        self.wall(self.job("old", "Globex", "Analyst"))
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,armed_at,result,attempt_id) VALUES ('armed','b','old','submit','{}','CONSUMED',?,?,?,'{}','gone')",
                              (self.now, self.now, self.now))
        self.database.commit()
        with self.step_available():
            [alone] = self.cards(self.page("/approvals"))
            self.assertEqual(self.needs()["sign_ins"], 0)
            self.confirming()
            both = self.cards(self.page("/approvals"))
            self.assertEqual(self.needs()["sign_ins"], 1)
        self.assertEqual(len(both), 2)
        [old] = [card for card in both if "Globex" in card]
        self.assertEqual(old, alone)
        self.assertIn(access_cards.NOT_ABANDONED, old)
        for absent in ("<button", 'action="/access/'):
            self.assertNotIn(absent, old)

    def test_the_step_route_opens_the_jobs_own_address_and_done_only_makes_the_read_due(self):
        key = self.confirming()
        card_key = confirm_read.key(key)
        before = self.row(key)
        view = mock.Mock()
        view.available.return_value = True
        view.offer.return_value = "target-1"
        view.finishing = set()
        user_step.connect(view, database=lambda: cards_fixture.database_with_schema(self.root), home=self.home)
        self.addCleanup(user_step.connect, None)
        fragment = self.post("/access/step", card_key)
        self.assertIn("/live-view/target-1/fragment", fragment)
        (job, url, purpose), options = view.offer.call_args
        self.assertIs(options["apply_link"], False)       # the view never says "apply yourself" on this card
        self.assertEqual((job, url, purpose, options["domain"], options["cleared"]),
                         (card_key, "https://jobs.example.com/sent", user_step.HUMAN_CHECK, "jobs.example.com", "CAPTCHA_REQUIRED"))
        # The live view's Done, for the wait the step was started for.
        import live_view
        ended = mock.Mock(reason=live_view.DONE, job=card_key, purpose=user_step.HUMAN_CHECK, site_session_saved=True)
        options["on_ended"](ended)
        after = self.row(key)
        changed = {name for name in after if after[name] != before[name]}
        self.assertEqual(changed, {"next_at"})
        self.assertLessEqual(after["next_at"], applications.stamp())
        self.assertEqual(confirm_read.waiting(self.database), [])
        self.assertEqual(self.cards(self.page("/approvals")), [])
        # A press from a page drawn before: said plainly, nothing started.
        self.assertIn(access_cards.CONFIRM_GONE, self.post("/access/step", card_key))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests WHERE job_key=?", (key,)).fetchone()[0], 1)
        events = [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=?", (key, user_step.FINISHED_EVENT))]
        self.assertEqual([(item["released"], item["confirming_read_due"]) for item in events], [(0, True)])


if __name__ == "__main__":
    unittest.main()
