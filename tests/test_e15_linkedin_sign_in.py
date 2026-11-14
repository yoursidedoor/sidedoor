"""The contact finder meets LinkedIn's sign-in page or a human check (owner, 2026-11-12, picks L6 a, L7 A):
the run waits on its own run row, one card appears under Needs you, the card's button opens the live view on
LinkedIn, Done saves LinkedIn's session and releases the wait, and the finder's next round runs with that
session put back. The fixture is test_e15_join's (the app as it builds itself, a stand-in guard and Chrome);
the finder's session is a stand-in that writes the result a session would. No browser, no model.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import datetime as dt
import json
import unittest
from unittest import mock

import access_cards
import browser_runtime
import desktop_alerts
import failures_page
import linkedin_contacts as lc
import notifications
import outreach_history_browser as browser
import overview_page
import research_workflow as research
import site_sessions
import strategy_execution
import test_e15_join as join
import test_linkedin_contacts as finder
import workflow_tasks

# Owner, 2026-11-13: the action only, never what Sidedoor does with it; Done says what every card says.
SIGN_IN_WORDS = "Sidedoor's browser needs you to sign in to LinkedIn."
CHECK_WORDS = "LinkedIn needs you to do a human check in Sidedoor's browser."
DONE_WORDS = "Done. Sidedoor carries on from here."
LINKEDIN_VALUE = "l1nk3d1n-signed-in-cookie-91ab"
CONFIG = {"linkedin_contacts": {"enabled": True}}
ADA = "https://www.linkedin.com/in/ada"


def session(outcome="FOUND", stopped_on=None, candidates=(), seen=None):
    """A finder session that ends with `outcome` and, when given, the structured `stopped_on`."""
    def run(config, prompt, directory, timeout):
        if seen is not None:
            seen.append(config)
        request = json.loads((directory / "request.json").read_text())
        (directory / "trace.jsonl").write_text("")
        result = {"version": lc.VERSION, "source_sha256": browser.digest(request), "outcome": outcome,
                  "reason": "The page asked for a sign in.", "company_page_url": None, "candidates": list(candidates)}
        if stopped_on:
            result["stopped_on"] = stopped_on
        (directory / "output.json").write_text(json.dumps(result))
    return run


@unittest.skipUnless(join.DEPENDENCIES, "dashboard packages (fastapi, httpx, websockets) are not installed")
class LinkedInSignIn(join.Flow):
    def setUp(self):
        super().setUp()
        strategy_execution.initialize(self.database)
        workflow_tasks.initialize(self.database)
        self.at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        source = finder.fixture()                      # the finder's own fixture rows, into the app's database
        with self.database:
            for table in ("rw_meta", "rw_claims", "rw_results"):
                for row in source.execute("SELECT * FROM " + table):
                    self.database.execute("INSERT OR REPLACE INTO %s VALUES (%s)" % (table, ",".join("?" * len(row))), tuple(row))
        source.close()
        available = mock.patch.object(lc, "step_available", side_effect=lambda config: self.supported)
        available.start()
        self.addCleanup(available.stop)
        self.cookies.write_text(json.dumps([join.cookie("li_at", LINKEDIN_VALUE, ".linkedin.com"),
                                            join.cookie("elsewhere", "x", "other.example.com")]))
        self.sessions = 0

    def find(self, run):
        """One finder round with the product's own session path (`linkedin_contacts.execute` stood in for)."""
        def counted(*arguments):
            self.sessions += 1
            return run(*arguments)
        self.at += dt.timedelta(minutes=61)
        with mock.patch.object(lc, "execute", counted):
            return lc.cycle(self.root, self.database, CONFIG, now=self.at)

    def wall(self, what="SIGN_IN"):
        return self.find(session("UNKNOWN", what))

    def runs(self):
        return [(row["state"], json.loads(row["result"]).get("waiting_on")) for row in self.database.execute(
            "SELECT state,result FROM linkedin_contact_runs ORDER BY started_at")]

    def failures(self):
        return failures_page.finder_failures(overview_page.Reader(self.database), dt.datetime.now(dt.timezone.utc), lambda moment: "")

    def tries(self):
        return sum(lc._finder_runs(self.database)[1].values())

    def watcher(self, posted):
        return desktop_alerts.Watcher(
            home=None, root=None, read=lambda: join.dashboard.read_connection(None),
            provider=notifications.Desktop(post=lambda title, text, href: posted.append((title, text, href)) or True),
            combine_over=desktop_alerts.COMBINE_OVER)

    def test_the_sign_in_page_is_one_card_counted_alerted_and_not_a_failure_or_a_try(self):
        posted = []
        watcher = self.watcher(posted)
        watcher.tick()
        output = self.wall()
        self.assertEqual(output["runs"][0]["state"], "USER_WAIT")
        self.assertEqual(self.runs(), [("USER_WAIT", "SIGN_IN")])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_drafts").fetchone()[0], 0)
        self.assertEqual(self.failures(), [])                  # no "didn't work" beside the card
        self.assertEqual(self.tries(), 0)                      # a wait, not a try
        self.assertEqual(len(lc._counted_runs(self.database, "")), 0)
        for path in ("/", "/approvals"):
            page = self.page(path)
            cards = self.cards(page)
            self.assertEqual(len(cards), 1, path)
            self.assertIn(SIGN_IN_WORDS, cards[0])
            self.assertIn("<strong>LinkedIn</strong>", cards[0])
            self.assertEqual(cards[0].count("<form"), 1)       # one control: Sign in
            self.assertIn(">Sign in</button>", cards[0])
            self.assertNotIn("Abandon", cards[0])
            self.assertNotIn("closes", cards[0].lower())
            self.assert_plain(page)
        self.assertEqual(self.needs()["sign_ins"], 1)
        self.assertIn('Sign-ins and human checks <span class="app-count">1</span>', self.page("/approvals"))   # the chip
        watcher.tick()
        self.assertEqual(posted, [("Needs you", "1 sign-in or human check to do", "/approvals#sign-ins")])
        self.assertEqual(watcher.total, 1)
        # While the card waits: no session, round after round, and still one card and one alert.
        for _ in range(3):
            output = self.find(session())
            self.assertEqual((output["runs"], output["user_wait"]["waiting_on"]), ([], "SIGN_IN"))
        self.assertEqual(self.sessions, 1)
        watcher.tick()
        self.assertEqual((len(posted), len(self.cards(self.page("/")))), (1, 1))

    def test_done_saves_linkedins_session_the_card_goes_and_the_next_round_runs_signed_in(self):
        self.assertEqual(site_sessions._key_state(None)[1], site_sessions.NEVER_MADE)   # a brand-new user
        self.wall()
        self.assertEqual(site_sessions._key_state(None)[1], site_sessions.OK)           # made before the step
        target, fragment = self.press_step(lc.CARD_KEY)
        offered = self.view.target(target)
        self.assertEqual((offered.job, offered.url, offered.purpose, offered.domain),
                         (lc.CARD_KEY, "https://www.linkedin.com/login", "sign_in", lc.SITE))
        self.assertNotIn("data-live-start", fragment)          # the card's own Sign in is the one control
        self.assertEqual(self.start_live(target)["state"], "live")
        self.assertEqual(self.guard_configs[-1][site_sessions.CONFIG_KEY], lc.SITE)
        self.assertEqual(self.runs(), [("USER_WAIT", "SIGN_IN")])       # nothing moves while the user is there
        self.assertEqual(self.end_live("done")["reason"], "done")
        self.assertTrue(site_sessions.has(lc.SITE))
        self.assertEqual([item["name"] for item in self.store()["sites"][lc.SITE]["cookies"]], ["li_at"])
        self.assertEqual(self.store()["sites"][lc.SITE]["wall"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "SIGN_IN")])
        for path in ("/", "/approvals"):
            self.assertEqual(self.cards(self.page(path)), [])
        self.assertEqual(self.needs()["sign_ins"], 0)
        # The redraw after Done says the finder's own sentence.
        redrawn = self.page("/access/section?at=approvals&ended=done&what=contacts")
        self.assertIn(DONE_WORDS, redrawn)
        self.assertIn(access_cards.FINDER_GONE, self.post("/access/step", lc.CARD_KEY))   # a press from an older page
        # The next ordinary round runs, with LinkedIn's site given to its session, and the guard of that
        # session puts the saved cookie back (site_sessions.restore, as the guard calls it).
        jar = join.FakeJar("https://www.linkedin.com/company/acme/people/")
        self.assertEqual(site_sessions.restore(lc.SITE, jar.call)["outcome"], "restored")
        self.assertEqual(jar.value("li_at"), LINKEDIN_VALUE)
        output = self.find(session("FOUND", candidates=[finder.candidate(ADA, "Ada Lovelace")]))
        self.assertEqual(output["runs"][0]["state"], "COMPLETE")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM rw_drafts").fetchone()[0], 1)
        self.assertTrue(site_sessions.has(lc.SITE))            # a read that went through keeps the session
        self.assertEqual((self.failures(), self.tries()), ([], 0))

    def test_the_finders_session_is_started_with_linkedins_site(self):
        seen = []
        with mock.patch.object(browser_runtime, "require"), mock.patch.object(browser_runtime, "prepare_profile"), \
                mock.patch.object(browser, "execute", lambda config, *rest: seen.append(config)):
            lc.execute({}, "p", self.root, 10)
        self.assertEqual((seen[0][site_sessions.CONFIG_KEY], seen[0]["_browser_operation"]), (lc.SITE, "linkedin"))
        command = browser_runtime.guard_command(dict(seen[0], browser_driver={"isolated": True}), self.root, "linkedin")
        self.assertEqual(command[command.index("--site-session") + 1], lc.SITE)

    def test_cancel_and_a_withdrawn_step_leave_the_card_and_save_nothing(self):
        self.wall()
        for reason in ("cancel", "withdrawn"):
            target, _ = self.press_step(lc.CARD_KEY)
            self.start_live(target)
            if reason == "cancel":
                self.end_live(reason)
            else:
                self.view.withdraw(target)                     # any ending that is not Done
                self.client.post("/live-view/end?reason=cancel", headers=join.ORIGIN)
            self.assertFalse(site_sessions.has(lc.SITE))
            self.assertEqual(self.store()["pending"], {})
            self.assertEqual(self.runs(), [("USER_WAIT", "SIGN_IN")])
            self.assertEqual(len(self.cards(self.page("/approvals"))), 1)
        self.assertEqual(self.find(session())["runs"], [])
        self.assertEqual(self.sessions, 1)

    def test_if_linkedin_asks_again_the_card_comes_back_and_the_old_session_is_forgotten(self):
        posted = []
        watcher = self.watcher(posted)
        watcher.tick()
        self.wall()
        watcher.tick()
        self.step(lc.CARD_KEY)
        watcher.tick()
        self.assertTrue(site_sessions.has(lc.SITE))
        self.wall()                                            # the next round still meets the sign-in page
        self.assertFalse(site_sessions.has(lc.SITE))           # LinkedIn no longer accepts it
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "SIGN_IN"), ("USER_WAIT", "SIGN_IN")])
        self.assertEqual(len(self.cards(self.page("/"))), 1)
        self.assertEqual(self.find(session())["runs"], [])     # and no loop without the user
        self.assertEqual(self.sessions, 2)
        watcher.tick()
        self.assertEqual((len(posted), watcher.total), (2, 1))
        self.assertEqual((self.failures(), self.tries()), ([], 0))

    def test_a_human_check_is_the_same_card_worded_as_a_check(self):
        self.wall("HUMAN_CHECK")
        self.assertEqual(self.runs(), [("USER_WAIT", "HUMAN_CHECK")])
        card = self.cards(self.page("/approvals"))[0]
        self.assertIn(CHECK_WORDS, card)
        self.assertIn(">Do the human check</button>", card)
        self.assertEqual(card.count("<form"), 1)
        target, _ = self.press_step(lc.CARD_KEY)
        self.assertEqual(self.view.target(target).purpose, "human_check")
        self.start_live(target)
        self.end_live("done")
        self.assertEqual(self.store()["sites"][lc.SITE]["wall"], "CAPTCHA_REQUIRED")
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "HUMAN_CHECK")])
        self.assertEqual(self.cards(self.page("/approvals")), [])

    def test_any_other_stop_and_a_computer_that_cannot_show_the_step_behave_as_before(self):
        self.find(session("UNKNOWN", "OTHER"))
        self.assertEqual(self.runs(), [("COMPLETE", None)])
        self.supported = False                                 # the live view is not available here
        self.wall()
        self.assertEqual([state for state, _ in self.runs()], ["COMPLETE", "COMPLETE"])
        self.assertEqual(self.tries(), 2)
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.cards(self.page("/approvals")), [])
        self.assertEqual(self.needs()["sign_ins"], 0)

    def test_an_applications_card_is_unchanged_beside_the_finders(self):
        self.session(join.stop("AUTHENTICATION_REQUIRED"), self.key)
        alone = self.cards(self.page("/approvals"))
        self.wall()
        both = self.cards(self.page("/approvals"))
        self.assertEqual(len(both), 2)
        self.assertIn(SIGN_IN_WORDS, both[0])
        self.assertEqual(both[1], alone[0])                    # the application's card, byte for byte
        self.assertEqual(self.needs()["sign_ins"], 2)
        self.step(lc.CARD_KEY)                                 # Done on LinkedIn's card releases only the finder
        self.assertEqual(self.cards(self.page("/approvals")), alone)
        self.assertEqual(self.open_waits(), {self.key})


for _name in dir(join.Flow):                                   # the join's own tests run in their own file only
    if _name.startswith("test_"):
        setattr(LinkedInSignIn, _name, getattr(LinkedInSignIn, _name) if _name in vars(LinkedInSignIn) else None)
del _name


if __name__ == "__main__":
    unittest.main()
