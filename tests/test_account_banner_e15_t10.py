"""E15.T10 (R15): the one release banner and the same question inside Setup's step 5.

The banner's truth is stored state: a saved choice (the one row of `application_account_setting`) or
`account_banner_over` in setup-state.json (DISMISSED, SETUP, RESET). Through `dashboard.create_app`."""
import json
import re
import unittest
import unittest.mock

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)

import access_cards
import access_outcomes
import setup_steps
import test_settings_page as settings_tests

SENTENCE = "New: Sidedoor can sign in to job sites for you."
PAGES = ("/", "/approvals", "/sent", "/applications", "/failures", "/metrics", "/settings")


@unittest.skipUnless(settings_tests.DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class AccountBannerTests(unittest.TestCase):
    setUp = settings_tests.SettingsPageTest.setUp
    client_for = settings_tests.SettingsPageTest.client_for

    def state(self):
        return json.loads((self.home / setup_steps.STATE_FILE).read_text())

    def existing_install(self):
        """An install that finished Setup before this version: the latch alone, as its file has it."""
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-01T00:00:00+00:00"), self.home)

    def shown(self, path="/"):
        return SENTENCE in self.client.get(path).text

    # --- user 1: an existing install that updates ----------------------------------------------------

    def test_an_existing_install_sees_the_one_banner_on_every_page_until_it_saves_a_choice(self):
        self.existing_install()
        for path in PAGES:
            page = self.client.get(path).text
            self.assertEqual(page.count(SENTENCE), 1, path)
            self.assertIn('<a class="button" href="/settings#account-setting">Choose how</a>', page)
            self.assertIn('action="/account-banner/dismiss"', page)
        self.assertEqual(access_outcomes.BANNER_HREF, access_cards.SETTING_HREF)
        self.assertIn('id="account-setting"', self.client.get("/settings").text)
        # Until they choose, Sidedoor asks each time, and looking at the banner saved nothing.
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        self.assertNotIn(setup_steps.ACCOUNT_BANNER_OVER, self.state())
        # Saving what already applies ("Ask me each time") is a saved choice: the banner goes with the
        # save's own answer (out of band) and stays gone, on a new dashboard too.
        response = self.client.post("/settings/account", data={"account": "ASK"}, headers=settings_tests.HTMX)
        self.assertIn('id="banners" hx-swap-oob="true"', response.text)
        self.assertNotIn(SENTENCE, response.text)
        for client in (self.client, self.client_for(self.home)):
            for path in PAGES:
                self.assertNotIn(SENTENCE, client.get(path).text, path)

    def test_the_banner_sits_after_a_problem_banner(self):
        self.existing_install()
        page = self.client.get("/").text
        if "stopped working." in page:   # the fixture has no engine: the red banner comes first
            self.assertLess(page.index("stopped working."), page.index(SENTENCE))
        self.assertEqual(page.count("banner-dismissable"), 1)

    # --- user 2: they dismiss it ---------------------------------------------------------------------

    def test_dismissed_is_gone_for_good_across_restarts(self):
        self.existing_install()
        response = self.client.post("/account-banner/dismiss", data={"next": "/applications"}, headers=settings_tests.ORIGIN)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/applications"))
        self.assertEqual(self.state()[setup_steps.ACCOUNT_BANNER_OVER], "DISMISSED")
        for client in (self.client, self.client_for(self.home)):
            self.assertFalse(any(SENTENCE in client.get(path).text for path in PAGES))
        # Dismissing chose nothing: still "Ask me each time", and Settings still says not chosen.
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        self.assertIn("Not chosen yet.", self.client.get("/settings").text)
        # Another site is never the way back.
        away = self.client.post("/account-banner/dismiss", data={"next": "https://example.com/"}, headers=settings_tests.ORIGIN)
        self.assertEqual(away.headers["location"], "/")

    # --- user 3: a new user who finishes Setup ------------------------------------------------------

    def test_a_new_user_who_finishes_setup_never_sees_it_touched_or_not(self):
        self.assertFalse(self.shown("/setup"))            # Setup not finished: no banner
        with unittest.mock.patch.object(setup_steps, "step_done", return_value=True):
            progress = setup_steps.progress(self.home)    # the latch: Setup finished on this version
        self.assertTrue(progress["finished"])
        self.assertEqual(progress["state"][setup_steps.ACCOUNT_BANNER_OVER], "SETUP")
        self.assertEqual(self.state()[setup_steps.ACCOUNT_BANNER_OVER], "SETUP")
        self.assertIsNone(access_outcomes.install_account_setting(self.root))   # nothing saved for them
        for client in (self.client, self.client_for(self.home)):
            self.assertFalse(any(SENTENCE in client.get(path).text for path in PAGES))

    def test_a_new_user_who_answers_in_setup_never_sees_it(self):
        response = self.client.post("/setup/account", data={"account": "SKIP"}, headers=settings_tests.HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="account" value="SKIP" checked', response.text)
        self.assertEqual(access_outcomes.install_account_setting(self.root), "SKIP")
        self.existing_install()   # even without the Setup mark, the saved choice alone ends it
        self.assertFalse(any(self.shown(path) for path in PAGES))

    # --- user 4: chose before the update ------------------------------------------------------------

    def test_an_existing_user_who_had_already_chosen_sees_no_banner(self):
        for value in ("ASK", "SKIP", "SIGN_IN"):
            access_outcomes.set_install_account_setting(value, root=self.root)
            self.existing_install()
            self.assertFalse(any(self.shown(path) for path in PAGES), value)

    def test_a_setting_that_cannot_be_read_is_not_no_choice(self):
        self.existing_install()
        with unittest.mock.patch.object(access_outcomes, "install_account_setting",
                                        return_value=access_outcomes.UNREADABLE):
            self.assertFalse(self.shown())
        self.assertTrue(self.shown())

    def test_a_reset_setup_file_does_not_bring_the_banner_back(self):
        import sqlite3
        import paths
        self.existing_install()
        self.client.post("/account-banner/dismiss", data={"next": "/"}, headers=settings_tests.ORIGIN)
        database = paths.database(self.root)
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as write:
            write.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
            write.execute("INSERT INTO runs DEFAULT VALUES")
        write.close()
        (self.home / setup_steps.STATE_FILE).write_text("{not json")
        with unittest.mock.patch.object(setup_steps, "find_codex", return_value=None):
            self.assertTrue(setup_steps.reset_state(self.home, self.root)["reset"])
        self.assertEqual(self.state()[setup_steps.ACCOUNT_BANNER_OVER], "RESET")
        self.assertFalse(self.shown())

    # --- the Setup question -------------------------------------------------------------------------

    def step(self, page=None):
        page = page if page is not None else self.client.get("/setup").text
        start = page.index('id="step-5"')
        return page[start:page.index("</li>", start)]

    def test_the_same_question_is_inside_step_5_preselected_and_never_blocking(self):
        step = self.step()
        self.assertIn("<h2>Review or automatic</h2>", step)
        self.assertIn('<h3 id="account-question">When a site needs an account</h3>', step)
        # Exactly the choices Settings offers now, with the same words.
        settings = self.client.get("/settings").text
        offered = re.findall(r'name="account" value="(\w+)"', settings)
        self.assertEqual(re.findall(r'name="account" value="(\w+)"', step), offered)
        self.assertEqual(offered, list(access_outcomes.offered_settings()))
        for value in offered:
            self.assertIn("<strong>%s</strong>" % access_outcomes.ACCOUNT_SETTING_LABELS[value], step)
        # Preselected: what applies until a choice is saved. Nothing is saved by showing it.
        self.assertEqual(re.findall(r'name="account" value="(\w+)" checked', step), ["ASK"])
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        # Never blocking: the step is done without it and no step waits for it.
        progress = setup_steps.progress(self.home)
        self.assertTrue(progress["done"][5])
        # A saved choice shows ticked here, as in Settings.
        self.client.post("/settings/account", data={"account": "SKIP"}, headers=settings_tests.HTMX)
        self.assertEqual(re.findall(r'name="account" value="(\w+)" checked', self.step()), ["SKIP"])

    def test_step_5_shows_the_same_line_under_the_choices_as_settings(self):
        note = "The first choice accepts each site&#39;s terms for you. Passwords stay on this computer."
        step, settings = self.step(), self.client.get("/settings").text
        self.assertEqual((step.count(note), settings.count(note)), (1, 1))     # one sentence, one source
        self.assertLess(step.index('value="SKIP"'), step.index(note))           # under the choices
        with unittest.mock.patch.object(access_outcomes, "resolver_available", lambda: False):
            self.assertNotIn(note, self.step())                                # no first choice: no line about it

    def test_setup_accepts_only_the_offered_choices_and_leaves_the_mode_alone(self):
        # Joined with E15.T8: "Sign in and create accounts for me" is offered now, so a value that is no choice.
        response = self.client.post("/setup/account", data={"account": "NOT_A_CHOICE"}, headers=settings_tests.HTMX)
        self.assertIn("Choose one of the options shown.", response.text)
        self.assertIsNone(access_outcomes.install_account_setting(self.root))
        response = self.client.post("/setup/account", data={"account": "ASK"}, headers=settings_tests.HTMX)
        self.assertEqual(access_outcomes.install_account_setting(self.root), "ASK")
        self.assertIn('name="mode" value="automatic" checked', self.step(response.text))
        plain = self.client.post("/setup/account", data={"account": "SKIP"}, headers=settings_tests.ORIGIN)   # no JavaScript
        self.assertEqual((plain.status_code, plain.headers["location"]), (303, "/setup?open=5#step-5"))
        self.assertEqual(access_outcomes.install_account_setting(self.root), "SKIP")

    # --- guard rail (design R15) --------------------------------------------------------------------

    def test_guard_rail_no_new_setup_step_and_no_new_settings_control(self):
        self.assertEqual([key for _, key, _ in setup_steps.STEPS],
                         ["license", "openrouter", "resume", "profile", "mode", "codex", "linkedin", "browser", "login", "start"])
        setup = self.client.get("/setup").text
        self.assertEqual(len(re.findall(r'<li class="card step ', setup)), 10)
        self.assertEqual(setup.count('action="/setup/account"'), 1)
        self.existing_install()
        page = self.client.get("/settings").text
        start = page.index('<section class="card settings-section" id="mode"')
        section = page[start:page.index("</section>", start)]
        # The one control E15.T6 added, and nothing from this task: the banner is not in a section.
        self.assertEqual(sorted(set(re.findall(r'<input type="radio" name="(\w+)"', section))), ["account", "mode"])
        self.assertEqual(page.count('action="/settings/account"'), 1)
        self.assertEqual(len(re.findall(r'name="account" value="\w+"', page)), len(access_outcomes.offered_settings()))
        self.assertNotIn("account-banner", page[page.index("<main"):])
        self.assertNotIn("account-question", page)

    def test_words(self):
        for words in (access_outcomes.BANNER_TEXT, access_outcomes.BANNER_ACTION, access_outcomes.CHOICE_ERROR):
            self.assertNotIn("—", words)
            self.assertNotIn("automation", words.lower())
        self.assertEqual(access_outcomes.BANNER_TEXT + " " + access_outcomes.BANNER_ACTION + ".",
                         "New: Sidedoor can sign in to job sites for you. Choose how.")


if __name__ == "__main__":
    unittest.main()
