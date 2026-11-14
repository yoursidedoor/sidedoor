"""One LinkedIn card (owner, 2026-11-13): a user who needs LinkedIn in Sidedoor's browser sees a single card,
whichever need raised it (outreach disconnected, the contact finder met the sign-in page, or both), counted
once, and one sign-in plus Done satisfies both. The fixture is the contact finder card's own
(tests/test_e15_linkedin_sign_in.py: the app as it builds itself, a stand-in guard and Chrome). No browser,
no model, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import unittest
from unittest import mock

import access_cards
import linkedin_connection
import linkedin_contacts as lc
import linkedin_session
import setup_steps
import unipile_port
import test_e15_join as join
import test_e15_linkedin_sign_in as finder_card

# Owner, 2026-11-13: one sentence whichever need raised the card, the action only.
WORDS = "Sidedoor's browser needs you to sign in to LinkedIn."
CHECK_WORDS = "LinkedIn needs you to do a human check in Sidedoor's browser."
OUTREACH_WORDS = BOTH_WORDS = WORDS
MARKER = "AQEDAT-ONE-CARD-MARKER-77"


class _Fixture(finder_card.LinkedInSignIn):
    """The finder card's fixture without its tests (they run in their own module)."""


for _name in [name for name in dir(finder_card.LinkedInSignIn) if name.startswith("test_")]:
    setattr(_Fixture, _name, None)


@unittest.skipUnless(join.DEPENDENCIES, "dashboard packages (fastapi, httpx, websockets) are not installed")
class OneLinkedInCard(_Fixture):
    def setUp(self):
        super().setUp()
        (self.home / "license.key").write_text("SD-TESTKEY\n")
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(
            lambda home: linkedin_session.Session(MARKER, "Agent/1")))
        self.addCleanup(linkedin_connection._sent.clear)
        self.connected = True
        self.calls = []

        def transport(method, path, **named):
            self.calls.append((method, path))
            if path == "/link/session":
                self.connected = True
                return 200, {"connected": True, "kind": "reconnect"}
            if path == "/users/me":
                return 200, {"premium": False}
            return 200, {"items": [{"type": "LINKEDIN", "id": "acct-1", "name": "Alex",
                                    "status": "OK" if self.connected else "CREDENTIALS",
                                    "connection_params": {"im": {"publicIdentifier": "alex"}}}]}
        patch = mock.patch.object(unipile_port, "RelayTransport", return_value=transport)
        patch.start()
        self.addCleanup(patch.stop)
        setup_steps.save_sender(self.home, "acct-1", "https://www.linkedin.com/in/alex")
        setup_steps.record(self.home, "linkedin", "done", "LinkedIn connected: Alex.", offer="reconnect")

    def disconnect(self):
        """Outreach is disconnected and the hourly check has asked the user (its re-send found no sign-in)."""
        self.connected = False
        linkedin_connection.hold(self.home)
        setup_steps.record(self.home, "linkedin", "needs_you", linkedin_connection.SIGN_IN_AGAIN, offer="reconnect")

    def step(self):
        return setup_steps.load_state(self.home)["steps"]["linkedin"]["status"]

    def sent(self):
        return [call for call in self.calls if call[0] == "POST"]

    def one_card(self, words):
        """Exactly one LinkedIn card, with `words`, counted once wherever cards are counted."""
        for path in ("/", "/approvals"):
            cards = self.cards(self.page(path))
            self.assertEqual(len(cards), 1, path)
            self.assertIn(words, cards[0])
            self.assertEqual(cards[0].count("<strong>LinkedIn</strong>"), 1)
            self.assertIn(">Sign in</button>", cards[0])
            self.assertEqual(cards[0].count("<form"), 1)
            self.assertNotIn("Abandon", cards[0])
        self.assertEqual(self.needs()["sign_ins"], 1)
        self.assertIn('Sign-ins and human checks <span class="app-count">1</span>', self.page("/approvals"))
        posted = []
        watcher = self.watcher(posted)
        watcher.tick()
        self.assertEqual(watcher.total, 1)   # the tray's number

    def no_card(self):
        for path in ("/", "/approvals"):
            self.assertEqual(self.cards(self.page(path)), [])
        self.assertEqual(self.needs()["sign_ins"], 0)

    def sign_in_and_done(self, key):
        target, unused = self.press_step(key)
        self.assertEqual(self.view.target(target).url, "https://www.linkedin.com/login")
        self.assertEqual(self.start_live(target)["state"], "live")
        self.assertEqual(self.end_live("done")["reason"], "done")

    def test_only_outreach_disconnected(self):
        self.disconnect()
        self.one_card(OUTREACH_WORDS)
        self.sign_in_and_done(linkedin_connection.CARD_KEY)
        self.assertEqual(self.sent(), [("POST", "/link/session")])   # Done connected outreach
        self.assertEqual(self.step(), "done")
        self.assertFalse(linkedin_connection.held(self.home))
        ran = self.database.execute("SELECT 1 FROM sqlite_master WHERE name='linkedin_contact_runs'").fetchone()
        self.assertEqual(self.runs() if ran else [], [])   # the finder: untouched
        self.no_card()

    def test_only_the_contact_finder_waiting(self):
        self.wall()
        self.one_card(finder_card.SIGN_IN_WORDS)
        self.sign_in_and_done(lc.CARD_KEY)
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "SIGN_IN")])   # Done released the finder
        self.assertEqual(self.calls, [])   # connected already: nothing is read from or sent to the service
        self.assertEqual(self.step(), "done")
        self.no_card()

    def test_both_needs_are_one_card_and_one_done_does_both(self):
        self.wall()
        self.disconnect()
        self.one_card(BOTH_WORDS)
        # Whichever key an older page was drawn with, it is the same one step.
        first, unused = self.press_step(linkedin_connection.CARD_KEY)
        second, unused = self.press_step(lc.CARD_KEY)
        self.assertEqual(first, second)
        self.assertEqual(self.start_live(second)["state"], "live")
        self.assertEqual(self.end_live("done")["reason"], "done")
        self.assertEqual(self.runs(), [("USER_WAIT_DONE", "SIGN_IN")])   # the finder goes on
        self.assertEqual(self.sent(), [("POST", "/link/session")])       # and outreach is connected, once
        self.assertEqual(self.step(), "done")
        self.assertFalse(linkedin_connection.held(self.home))
        self.no_card()
        self.assertEqual((access_cards.LINKEDIN_WORDS, finder_card.SIGN_IN_WORDS), (WORDS, WORDS))
        # After Done the redraw says what every card says.
        self.assertIn("Done. Sidedoor carries on from here.", self.page("/access/section?at=approvals&ended=done&what=contacts"))

    def test_no_shown_sentence_says_what_sidedoor_does_with_the_users_action(self):
        # Owner, 2026-11-13 ("overexposure"): name the action only. Every sentence the LinkedIn card, step,
        # Overview line and outage notice can show, and the templates, with a must-hit control.
        import re
        from pathlib import Path
        banned = ("so it can", "so sidedoor can", "sidedoor uses that", "find contacts", "invitations and messages",
                  "looks for contacts")

        def hits(texts):
            return sorted({word for text in texts for word in banned if word in text.lower()})
        self.assertEqual(hits(["Sidedoor's browser needs you to sign in to LinkedIn so it can find contacts.",
                               "Sidedoor uses that sign-in to find contacts and to send your invitations and messages.",
                               "Sidedoor looks for contacts again on its next round."]),
                         ["find contacts", "invitations and messages", "looks for contacts", "sidedoor uses that", "so it can"])
        sentences = [value for module in (linkedin_connection, access_cards) for name, value in vars(module).items()
                     if name.isupper() and isinstance(value, str) and " " in value]
        sentences += [message for unused, message in linkedin_connection.MESSAGES.values()]
        sentences += list(access_cards.FINDER_WORDS.values())
        self.assertGreater(len(sentences), 40)
        self.assertEqual(hits(sentences), [])
        root = Path(setup_steps.ROOT) / "dashboard_assets" / "templates"
        shown = {path.name: re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
                 for path in sorted(root.rglob("*.html"))}
        for name in ("setup_step.html", "settings_linkedin.html", "access_cards.html", "linkedin.html", "overview_needs.html"):
            self.assertEqual(hits([shown[name]]), [], name)
        import live_view
        self.assertEqual(live_view.LIVE_WORDS, "This is the page in Sidedoor's browser.")

    def test_both_with_a_human_check_is_worded_for_both(self):
        self.wall("HUMAN_CHECK")
        self.disconnect()
        cards = self.cards(self.page("/"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(access_cards.LINKEDIN_CHECK_WORDS, CHECK_WORDS)
        self.assertIn(CHECK_WORDS, cards[0])
        self.assertIn(">Do the human check</button>", cards[0])
        self.assertEqual(self.needs()["sign_ins"], 1)


class WhatTheLiveViewSaysOnTheLinkedInCard(unittest.TestCase):
    """The LinkedIn card has no posting link and no application: no sentence the live view can show on it
    may say to apply, to abandon or to open a posting. The confirming read's card has the same rule and the
    same mechanism (`Target.apply_link` false, `live_view.said`; tests/test_confirm_read_card.py)."""

    def targets(self):
        """The live view's target for each way the card offers its step, as the product offers it."""
        import contextlib
        import sqlite3
        import live_view
        import user_step
        offered = []

        def offer(job, url, purpose, domain=None, **options):
            offered.append(live_view.Target("id", job, url, purpose, domain, apply_link=options.get("apply_link", True)))
            return "id"
        view = mock.Mock(offer=offer, finishing=set())
        view.available.return_value = True
        # Outreach's sign-in step (setup, Settings and the card's [Sign in]).
        linkedin_connection.attach(view)
        self.addCleanup(linkedin_connection.attach, None)
        self.assertIsNotNone(linkedin_connection.sign_in_view())
        # The contact finder's step, through the card's own route.
        user_step.connect(view, database=lambda: sqlite3.connect(":memory:"), home=None)
        self.addCleanup(user_step.connect, None)
        with contextlib.ExitStack() as patched:
            patched.enter_context(mock.patch.object(user_step, "sync"))
            patched.enter_context(mock.patch.object(user_step, "finishing", return_value=False))
            patched.enter_context(mock.patch("applications.uncertain_submission", return_value=False))
            patched.enter_context(mock.patch.object(
                user_step, "_page", return_value=("https://www.linkedin.com/login", "linkedin.com", "LOGIN_REQUIRED", 7)))
            self.assertTrue(user_step.start(lc.CARD_KEY, user_step.SIGN_IN)["available"])
        self.assertEqual([target.job for target in offered], [linkedin_connection.CARD_KEY, lc.CARD_KEY])
        return offered

    def test_a_failed_start_says_only_try_again(self):
        import live_view
        for target in self.targets():
            self.assertEqual(live_view.said(target, live_view.NOT_SHOWN_WORDS), "Sidedoor couldn't open its browser. Try again.")

    def test_no_sentence_the_view_can_show_here_says_to_apply_abandon_or_open_the_posting(self):
        import live_view
        import test_confirm_read_card as confirm_card
        for target in self.targets():
            told = [live_view.said(target, words) for words in live_view.ALL_WORDS
                    if words != live_view.FAILURE_TODO]      # the Failures page's line: checked below
            self.assertEqual([words for words in told if confirm_card.forbidden(words)], [], target.job)
        for words in (access_cards.LINKEDIN_WORDS, access_cards.LINKEDIN_CHECK_WORDS, access_cards.LINKEDIN_GONE,
                      access_cards.STEP_UNAVAILABLE_HERE):
            self.assertEqual(confirm_card.forbidden(words), [])

    def test_the_failures_page_never_says_apply_yourself_for_this_cards_step(self):
        import datetime as dt
        import json
        import sqlite3
        import failures_page
        import live_view
        import overview_page
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        database.execute(live_view.TABLE)
        now = dt.datetime.now(dt.timezone.utc)
        for job, domain in ((linkedin_connection.CARD_KEY, "linkedin.com"), (lc.CARD_KEY, "www.linkedin.com"),
                            ("ordinary", "boards.example.org")):
            database.execute("INSERT INTO live_view_failures(at,reason,domain,job_key,purpose) VALUES (?,?,?,?,?)",
                             (now.isoformat(), live_view.NOT_SHOWN, domain, job, "sign_in"))
        items = [json.dumps(item, default=str)
                 for item in failures_page.live_view_failures(overview_page.Reader(database), now, lambda moment: "now")]
        for site in ("Site: linkedin.com.", "Site: www.linkedin.com."):
            told = next(item for item in items if site in item)
            self.assertIn(live_view.FAILURE_TODO_NO_LINK, told)
            self.assertNotIn("apply yourself", told)
        self.assertIn(live_view.FAILURE_TODO, next(item for item in items if "boards.example.org" in item))


if __name__ == "__main__":
    unittest.main()
