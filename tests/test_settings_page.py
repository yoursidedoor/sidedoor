"""The Settings page (E6.T8, E6.T0 spec section 10).

Needs the pinned dashboard packages and httpx (`pip install -e '.[test]'`); skipped without them.
Every network call is faked (OpenRouter, relay, Codex, Chrome). Each section must save through the
same `setup_steps` function as its Setup step, so a value saved here is read identically.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import license_fixtures
import linkedin_contacts
import research_workflow
import resume_intake
import setup_steps
import user_profile

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard
    import settings_page


PORT = 47823
SECRET = "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
OPENROUTER_KEY = "sk-or-v1-never-shown-9z8y"
LICENSE_KEY = "SD-SECRETLICENSEKEY-AB12"
OK_ANSWER = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}}).encode()
FORM = {"given_name": "Alex", "family_name": "Example", "email": "alex@example.com", "phone": "+1 202 555 0143",
        "linkedin": "linkedin.com/in/alex-example", "current_location": "Austin, Texas, United States",
        "roles": ["Software Engineering"], "levels": ["Senior"], "industries": ["AI"], "countries": ["United States"],
        "funding_stages": ["Seed", "Series A"], "minimum_salary": "$150,000", "work_modes": ["Remote"],
        "places": ["Anywhere in Europe"], "work_authorization": ["United States: allowed to work, no sponsorship needed"],
        "notice_period": "2 weeks", "start_weeks": "4", "gender": "Prefer not to say", "race": "Prefer not to say",
        "veteran": "Prefer not to say", "disability": "Prefer not to say"}
RAW_SHA = hashlib.sha256(b"raw resume").hexdigest()


class FakeBrowser:
    def __init__(self, open_outcome="busy", close_result="run"):
        self.calls, self.open_outcome, self.close_result, self.open_now = [], open_outcome, close_result, False

    def is_open(self):
        return self.open_now

    def open(self):
        self.calls.append("open")
        if self.open_outcome in ("opened", "brought_forward"):
            self.open_now = True
        return self.open_outcome

    def close(self, then=None):
        self.calls.append("close")
        if self.close_result is None:
            return None  # a scheduled job holds the browser lock
        self.open_now = False
        return then() if then else "closed"


def entitlement_file(home, valid_until="2999-01-01T00:00:00Z", status="active", valid=True):
    """A relay-signed entitlement (test key) for the key already in `home`."""
    license_fixtures.install(home, key=(home / "license.key").read_text().strip(), status=status, valid=valid,
                             valid_until=valid_until if valid else None, server_time="2026-10-01T10:00:00Z")


def relay_answer(home, valid_until="2999-02-01T00:00:00Z"):
    """The relay's 200 answer to /license/activate for LICENSE_KEY on this home's machine."""
    now = license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)
    return json.dumps(license_fixtures.signed(LICENSE_KEY, setup_steps.machine_id(home), now, valid_until=valid_until))


def outside_box(html, key, box):
    """The page with the one password box `box` holding `key` taken out (asserted present, hidden):
    the key must appear nowhere else (messages, labels, other fragments)."""
    tag = '<input id="%s" type="password" name="key" value="%s"' % (box, key)
    assert html.count(tag) == 1, "the key is not in its hidden box"
    return html.replace(tag, "")


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class SettingsPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown: it then runs after the connections a test closes with addCleanup
        # (an open database keeps its file in use on Windows).
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.root = Path(self.directory.name) / "root"
        self.home.mkdir()
        self.root.mkdir()
        environment = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        self.browser = FakeBrowser()
        self.client = self.client_for(self.home)

    def client_for(self, home):
        app = dashboard.create_app(SECRET, PORT, home=home, root=self.root, config={"text_model": {"model": "test/model"}},
                                   browser=self.browser, relay_url="https://relay.test")
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        return client

    def profile(self):
        return json.loads((self.home / "profile.json").read_text())

    # --- page -------------------------------------------------------------------------------------

    def test_settings_replaces_the_placeholder_with_the_five_sections(self):
        page = self.client.get("/settings").text
        positions = [page.index('id="%s"' % name) for name in settings_page.SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("This page is not built yet.", page)
        self.assertNotIn("<script>", page)
        self.assertIn("Start Sidedoor when I log in", page)     # E5.T2 (tests/test_login_item.py)
        self.assertNotIn("Telegram", page)                       # E6.T9, out of scope
        self.assertEqual(1, sum(1 for route in self.client.app.router.routes if getattr(route, "path", None) == "/settings"))

    # --- Profile and search -----------------------------------------------------------------------

    def test_profile_saved_from_settings_is_exactly_what_setup_saves(self):
        other = Path(self.directory.name) / "other"
        other.mkdir()
        self.client_for(other).post("/setup/profile", data=FORM, headers=HTMX)
        response = self.client.post("/settings/profile", data=FORM, headers=HTMX)
        self.assertIn("Saved.", response.text)
        self.assertEqual(json.loads((other / "profile.json").read_text()), self.profile())
        # Level Senior -> Staff / Principal writes the same setup_picks entry and the combined `levels` research reads.
        self.client.post("/settings/profile", data=dict(FORM, levels=["Staff / Principal"]), headers=HTMX)
        profile = self.profile()
        self.assertEqual(["Staff / Principal"], profile["setup_picks"]["levels"])
        self.assertEqual(["Staff / Principal Software Engineering"], profile["levels"])
        self.assertEqual(["Seed", "Series A"], profile["funding_stages"])
        self.assertEqual(["Staff / Principal Software Engineering"], user_profile.load()["levels"])
        page = self.client.get("/settings").text
        self.assertIn('name="levels" value="Staff / Principal"', page)

    def test_a_profile_error_keeps_what_was_typed_and_the_saved_file(self):
        self.client.post("/settings/profile", data=FORM, headers=HTMX)
        before = self.profile()
        response = self.client.post("/settings/profile", data=dict(FORM, phone="call me", given_name="Alexandra"),
                                    headers=HTMX).text
        self.assertIn("Enter a phone number", response)
        self.assertIn('value="call me"', response)
        self.assertIn('value="Alexandra"', response)
        self.assertEqual(before, self.profile())
        self.assertIn('value="Alexandra"', self.client.get("/settings").text)  # still there after a reload

    def test_locked_email_is_read_only_and_a_crafted_post_cannot_change_it(self):
        self.client.post("/settings/profile", data=FORM, headers=HTMX)
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-10-22T00:00:00Z",
                                                            locked_email="alex@example.com"), self.home)
        page = self.client.get("/settings").text
        self.assertIn('<p class="locked-value" id="s-email"><strong>alex@example.com</strong></p>', page)
        self.assertNotIn('name="email"', page.split('id="profile"')[1].split('</section>')[0])
        self.assertIn(setup_steps.LOCKED_LINE, page)
        self.client.post("/settings/profile", data=dict(FORM, email="someone@else.com"), headers=HTMX)
        self.assertEqual("alex@example.com", self.profile()["answers"]["email"]["value"])

    # --- Résumé -----------------------------------------------------------------------------------

    def test_resume_needs_openrouter_first(self):
        files = {"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
        response = self.client.post("/settings/resume", files=files, headers=HTMX).text
        self.assertIn("needs your OpenRouter key first", response)
        self.assertFalse((self.home / "resume").exists())

    def test_replace_reads_in_the_background_then_fails_keeping_the_old_resume_with_try_again(self):
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        (self.home / "profile.json").write_text(json.dumps({"resume_raw": {"file_name": "old.pdf", "page_count": 2,
                                                                           "path": "/nowhere", "sha256": RAW_SHA}}))
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            reading = self.client.post("/settings/resume", files={"resume": ("new.pdf", b"%PDF-new", "application/pdf")},
                                       headers=HTMX).text
            self.assertIn("Reading your résumé", reading)
            self.assertIn('hx-get="/settings/resume/status" hx-trigger="every 3s"', reading)
            self.assertNotIn('name="resume"', reading)  # no second upload while reading
            self.assertIn("old.pdf", reading)            # the résumé in use is still shown
        with mock.patch.object(setup_steps.resume_intake, "ingest",
                               return_value={"state": "FAILED", "code": "AI_NO_CREDITS",
                                             "reason": "The AI call failed: your OpenRouter account has no credits left."}):
            spawned[0]()
        failed = self.client.get("/settings/resume/status", headers={"hx-request": "true"}).text
        self.assertIn("no credits left", failed)
        self.assertIn("Your previous résumé is still in use.", failed)
        self.assertIn('hx-post="/settings/resume/retry"', failed)
        self.assertIn('name="resume"', failed)
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            self.client.post("/settings/resume/retry", headers=HTMX)
        self.assertEqual(2, len(spawned))

    def test_a_read_lost_to_a_restart_shows_interrupted_with_try_again(self):
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        (self.home / "resume").mkdir()
        (self.home / "resume" / "original.pdf").write_bytes(b"%PDF")
        setup_steps.record(self.home, "resume", "reading", "x", read_id="lost",
                           raw={"path": str(self.home / "resume" / "original.pdf"), "file_name": "cv.pdf", "kind": "PDF"})
        page = self.client.get("/settings").text
        self.assertIn(setup_steps.INTERRUPTED, page)
        self.assertIn('hx-post="/settings/resume/retry"', page)
        self.assertNotIn("Still reading", page)

    def claims_database(self, texts):
        (self.root / "state").mkdir()
        database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        research_workflow.initialize(database)
        ids = research_workflow.register_claims(database, [{"text": text, "tags": ["backend"], "page": 1} for text in texts],
                                                designated_source_text="", source_sha256=RAW_SHA, designated_sha256=RAW_SHA)
        database.commit()
        return database, ids

    def test_resume_files_open_from_the_home_only(self):
        folder = self.home / "resume"
        folder.mkdir()
        (folder / "raw-1.pdf").write_bytes(b"%PDF raw")
        (folder / "application-1.pdf").write_bytes(b"%PDF ats")
        outside = Path(self.directory.name) / "outside.pdf"
        outside.write_bytes(b"secret")
        (self.home / "profile.json").write_text(json.dumps({
            "resume_raw": {"path": str(folder / "raw-1.pdf"), "file_name": "cv.pdf", "sha256": RAW_SHA},
            "resume": {"path": str(outside), "sha256": "x"}}))
        page = self.client.get("/settings").text
        self.assertIn('href="/settings/resume/file/raw"', page)
        self.assertNotIn('href="/settings/resume/file/application"', page)
        raw = self.client.get("/settings/resume/file/raw")
        self.assertEqual((200, b"%PDF raw"), (raw.status_code, raw.content))
        self.assertEqual(404, self.client.get("/settings/resume/file/application").status_code)
        self.assertEqual(404, self.client.get("/settings/resume/file/other").status_code)

    # --- Mode -------------------------------------------------------------------------------------

    def test_mode_toggle_is_setup_step_4s(self):
        response = self.client.post("/settings/mode", data={"mode": "review"}, headers=HTMX).text
        self.assertEqual("review", setup_steps.load_state(self.home)["mode"])
        self.assertIn('value="review" checked', response)
        self.assertIn(">Review</a>", response)  # header chip, out of band
        self.assertIn(settings_page.NOT_RECALLED, response)

    # --- Connections ------------------------------------------------------------------------------

    def test_openrouter_key_is_hidden_in_its_box_tested_without_retyping_and_replaceable(self):
        (self.home / "openrouter.key").write_text(OPENROUTER_KEY + "\n")
        page = self.client.get("/settings").text
        self.assertIn("...9z8y", page)
        self.assertNotIn(OPENROUTER_KEY, outside_box(page, OPENROUTER_KEY, "s-openrouter-key"))
        self.assertIn('data-reveal="s-openrouter-key" aria-controls="s-openrouter-key" aria-label="Show key"', page)
        self.assertIn('<script src="/static/settings.js" defer></script>', page)
        self.assertIn('aria-label", show ? "Hide key" : "Show key"', self.client.get("/static/settings.js").text)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)) as post:
            tested = self.client.post("/settings/openrouter/test", headers=HTMX).text
        self.assertEqual("Bearer " + OPENROUTER_KEY, post.call_args[0][2]["Authorization"])
        self.assertIn("Key works.", tested)
        self.assertNotIn(OPENROUTER_KEY, outside_box(tested, OPENROUTER_KEY, "s-openrouter-key"))
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            replaced = self.client.post("/settings/openrouter", data={"key": "sk-or-v1-newkey-4444"}, headers=HTMX).text
        self.assertIn("...4444", replaced)
        self.assertEqual("sk-or-v1-newkey-4444", (self.home / "openrouter.key").read_text().strip())

    def test_a_refused_replacement_restores_the_previous_key_and_keeps_the_typo_in_its_box(self):
        (self.home / "openrouter.key").write_text(OPENROUTER_KEY + "\n")
        typo = "sk-or-v1-typo-0000"
        refused = (401, json.dumps({"error": {"message": "No auth credentials found " + typo}}).encode())
        with mock.patch.object(setup_steps.text_model, "_post", return_value=refused):
            response = self.client.post("/settings/openrouter", data={"key": typo}, headers=HTMX).text
        self.assertEqual(OPENROUTER_KEY, (self.home / "openrouter.key").read_text().strip())
        self.assertIn("OpenRouter did not accept this key. " + settings_page.KEY_KEPT, response)
        rest = outside_box(response, typo, "s-openrouter-key")
        self.assertNotIn(typo, rest)
        self.assertNotIn(OPENROUTER_KEY, rest)
        self.assertNotIn(typo, json.dumps(setup_steps.load_state(self.home)))
        # A reload shows the key in use (the old one), hidden in its box.
        self.assertNotIn(OPENROUTER_KEY, outside_box(self.client.get("/settings").text, OPENROUTER_KEY, "s-openrouter-key"))

    def test_with_no_previous_key_a_refused_key_is_kept_as_setup_does(self):
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(401, b"{}")):
            response = self.client.post("/settings/openrouter", data={"key": "sk-or-v1-first-1111"}, headers=HTMX).text
        self.assertEqual("sk-or-v1-first-1111", (self.home / "openrouter.key").read_text().strip())
        self.assertNotIn(settings_page.KEY_KEPT, response)

    def test_codex_check(self):
        with mock.patch.object(setup_steps, "check_codex",
                               side_effect=lambda home: setup_steps.record(home, "codex", "failed", "Codex is not installed.")) as check:
            response = self.client.post("/settings/codex", headers=HTMX).text
        check.assert_called_once()
        self.assertIn("Codex is not installed.", response)

    def ready_for_email_check(self, email_locked=False):
        """Setup's step-9 prerequisites done: profile saved, Codex signed in, webmail, browser closed."""
        self.client.post("/settings/profile", data=FORM, headers=HTMX)
        setup_steps.record(self.home, "codex", "done", "Codex is installed and signed in.")
        setup_steps.record(self.home, "browser", "done", "Sidedoor's browser is closed.")
        values = {"codex_path": "/codex", "webmail": {"label": "Gmail", "url": "https://mail.google.com/"}}
        if email_locked:
            values.update(email_check_passed_at="2026-10-22T00:00:00Z", locked_email="alex@example.com")
        setup_steps.update_state(lambda state: state.update(values), self.home)
        usable = mock.patch.object(setup_steps, "_usable", return_value=True)
        usable.start()
        self.addCleanup(usable.stop)

    def test_browser_open_while_a_job_holds_the_lock_is_busy_and_does_nothing_else(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            response = self.client.post("/settings/browser/open", headers=HTMX).text
            self.assertIn(setup_steps.BUSY_LINE, response)
            self.assertEqual(["open"], self.browser.calls)
            self.assertNotIn("I'm done", response)
            self.ready_for_email_check()
            self.browser.close_result = None
            self.client.post("/settings/email", headers=HTMX)
        self.assertEqual(setup_steps.BUSY_LINE, setup_steps.load_state(self.home)["steps"]["email"]["message"])
        self.assertEqual(["open", "close"], self.browser.calls)

    def test_email_check_needs_setups_prerequisites_and_uses_the_saved_email_only(self):
        # T1: a failed save of b@x.com never reaches the check; profile.json keeps alex@example.com.
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            self.ready_for_email_check()
            self.client.post("/settings/profile", data=dict(FORM, email="b@x.com", phone="abc"), headers=HTMX)
            setup_steps.record(self.home, "codex", "failed", "Codex is not installed.")
            refused = self.client.post("/settings/email", headers=HTMX).text
            self.assertIn('finish <a href="#profile">your profile</a>, <a href="#connection-codex">Codex</a> first', refused)
            self.assertEqual([], self.browser.calls)
            self.client.post("/settings/profile", data=FORM, headers=HTMX)
            setup_steps.record(self.home, "codex", "done", "Codex is installed and signed in.")
            setup_steps.record(self.home, "profile", "failed", "Some fields need a fix.",
                               form=dict(FORM, email="b@x.com"), errors={"phone": "x"})
            setup_steps.record(self.home, "profile", "done", "Saved.", form=dict(FORM, email="b@x.com"))  # stale form
            with mock.patch.object(setup_steps, "read_signed_in_account", return_value="b@x.com") as read:
                checked = self.client.post("/settings/email", headers=HTMX).text
        read.assert_called_once()
        state = setup_steps.load_state(self.home)
        self.assertNotEqual("b@x.com", state.get("locked_email"))
        self.assertIn("your application email is alex@example.com", checked)

    def test_a_first_passing_check_locks_email_and_webmail_out_of_band(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            self.ready_for_email_check()
            with mock.patch.object(setup_steps, "read_signed_in_account", return_value="Alex@Example.com"):
                response = self.client.post("/settings/email", headers=HTMX).text
        self.assertEqual("alex@example.com", setup_steps.load_state(self.home)["locked_email"])
        self.assertIn('id="connection-webmail" hx-swap-oob="true"', response)
        self.assertIn('id="profile" hx-swap-oob="true"', response)
        self.assertIn(setup_steps.LOCKED_LINE, response)

    def test_a_second_open_brings_the_dashboards_chrome_forward_and_im_done_closes_it(self):
        self.browser.open_outcome = "opened"
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            first = self.client.post("/settings/browser/open", headers=HTMX).text
            self.assertIn("I'm done", first)
            self.browser.open_outcome = "brought_forward"
            second = self.client.post("/settings/browser/open", headers=HTMX).text
            self.assertIn("Sidedoor&#39;s browser is in front.", second)
            self.client.post("/settings/browser/done", headers=HTMX)
        self.assertEqual(["open", "open", "close"], self.browser.calls)

    def test_email_check_runs_under_the_browser_rule_and_webmail_is_read_only_once_locked(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            self.ready_for_email_check(email_locked=True)
            with mock.patch.object(setup_steps, "read_signed_in_account", return_value="other@example.com"):
                response = self.client.post("/settings/email", headers=HTMX).text
            self.assertIn("Sign in to alex@example.com in Sidedoor&#39;s browser.", response)
            self.assertIn("different mailbox", response)  # banner 5, out of band
            page = self.client.get("/settings").text
            self.assertIn(setup_steps.LOCKED_LINE, page.split('id="connection-webmail"')[1])
            self.assertNotIn("Save webmail", page)
            self.client.post("/settings/webmail", data={"webmail": "Outlook.com"}, headers=HTMX)
        self.assertEqual("Gmail", setup_steps.load_state(self.home)["webmail"]["label"])
        self.assertEqual(["close"], self.browser.calls)

    def test_locked_with_no_webmail_saved_hides_the_empty_line(self):
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-10-22T00:00:00Z",
                                                            locked_email="alex@example.com"), self.home)
        webmail = self.client.get("/settings").text.split('id="connection-webmail"')[1].split("</div>")[0]
        self.assertNotIn("Your webmail: <strong></strong>", webmail)
        self.assertIn(setup_steps.LOCKED_LINE, webmail)

    def test_a_refused_webmail_save_says_what_to_do(self):
        response = self.client.post("/settings/webmail", data={"webmail": "Other"}, headers=HTMX).text
        self.assertIn("Pick your webmail from the list.", response)
        self.assertNotIn("If you picked Other", response)
        self.assertNotIn('id="connection-openrouter"', response)

    def test_claims_are_not_shown_in_settings(self):
        # Owner, 2026-10-22: the claims and their outreach wording are clutter for the user.
        page = self.client.get("/settings").text
        self.assertNotIn("Sentences Sidedoor can use in outreach", page)
        self.assertNotIn("Outreach wording", page)
        self.assertEqual(self.client.post("/settings/resume/claims/x/remove", headers=HTMX).status_code, 404)

    def test_each_connections_action_swaps_only_its_own_part(self):
        # T6: the LinkedIn link and typing in the other parts survive a Codex check.
        with mock.patch.object(setup_steps, "check_codex",
                               side_effect=lambda home: setup_steps.record(home, "codex", "failed", "Codex is not installed.")):
            response = self.client.post("/settings/codex", headers=HTMX).text
        self.assertIn('id="connection-codex"', response)
        for other in ("connection-linkedin", "connection-openrouter", "connection-browser", "connection-webmail"):
            self.assertNotIn('id="%s"' % other, response)
        self.assertIn('hx-target="#connection-codex"', response)

    def test_settings_messages_name_settings_buttons(self):
        # T7: Setup's wording names Setup's buttons; Settings says its own. Setup is unchanged.
        for outcome in ("opened", "unclean", "failed"):
            self.browser.open_outcome = outcome
            with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
                response = self.client.post("/settings/browser/open", headers=HTMX).text
            self.assertNotIn("click Try again", response)
            self.assertNotIn("then click I&#39;m done.", response)
            self.browser.open_now = False
        self.assertIn("Click Open to try again.", response)
        setup_steps.record(self.home, "email", "failed", "We could not see which mailbox is signed in. Open Sidedoor's "
                           "browser and check you are signed in to your inbox, then click I'm done.", reason="unreadable")
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            page = self.client.get("/settings").text
            self.assertIn("signed in to your inbox, then click Run email check.", page)
        # Setup's own wording is unchanged (the stored message and Setup's step 8).
        self.assertIn("then click I'm done.", setup_steps.load_state(self.home)["steps"]["email"]["message"])
        self.assertIn("Sidedoor could not start Google Chrome. Try again.", self.client.get("/setup").text)

    def test_linkedin_reconnect_shown_while_connected(self):
        (self.home / "license.key").write_text(LICENSE_KEY + "\n")
        entitlement_file(self.home)
        transport = mock.Mock(return_value=(200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "OK"}]}))
        with mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport), \
                mock.patch.object(setup_steps.unipile_port, "hosted_link", return_value="https://unipile.test/r") as link:
            status = self.client.get("/settings/linkedin/status", headers={"hx-request": "true"})
            self.assertEqual("#connection-linkedin", status.headers["hx-retarget"])
            self.assertNotIn('id="connection-openrouter"', status.text)  # typing there is kept
            self.assertIn("LinkedIn connected: Alex.", status.text)
            self.assertIn('hx-post="/settings/linkedin/reconnect"', status.text)
            response = self.client.post("/settings/linkedin/reconnect", headers=HTMX).text
            self.assertEqual("reconnect", link.call_args[0][2])
            self.assertIn('href="https://unipile.test/r"', response)
            link.reset_mock()
            self.client.post("/settings/linkedin/connect", headers=HTMX)  # listed: never a second paid account
            link.assert_not_called()

    def test_linkedin_without_a_license_points_to_the_license_section(self):
        self.assertIn('Needs your license first (<a href="#license">', self.client.get("/settings").text)

    # --- License ----------------------------------------------------------------------------------

    def test_license_shows_the_key_masked_and_reactivates_with_the_stored_key(self):
        (self.home / "license.key").write_text(LICENSE_KEY + "\n")
        entitlement_file(self.home, status="trialing")
        page = self.client.get("/settings").text
        self.assertIn("SD-...AB12", page)
        self.assertIn("<dt>Good until</dt><dd>Dec 31, ", page)  # local, readable (E8.T2 review fix 9)
        self.assertIn("<dt>Last successful check</dt><dd>Oct 1, ", page)  # the signed server time
        self.assertIn(settings_page.NO_PORTAL, page)  # no billing_portal_url in this config
        self.assertIn("Trial.", page)  # no trial end date signed: no countdown
        self.assertNotIn(LICENSE_KEY, outside_box(page, LICENSE_KEY, "s-license-key"))
        self.assertIn('data-reveal="s-license-key"', page)
        signed = relay_answer(self.home)
        with mock.patch.object(setup_steps, "_post_json", return_value=(200, signed.encode())) as post, \
                mock.patch.object(setup_steps, "linkedin_status") as linkedin:
            response = self.client.post("/settings/license/activate", headers=HTMX).text
        self.assertEqual(LICENSE_KEY, post.call_args[0][1]["key"])
        linkedin.assert_called_once()
        self.assertIn("Active: Active.", response)
        self.assertNotIn(LICENSE_KEY, outside_box(response, LICENSE_KEY, "s-license-key"))

    def test_manage_subscription_opens_the_billing_portal_or_falls_back_to_support(self):
        portal = "https://billing.stripe.com/p/login/test"
        for url in (portal, ""):
            with self.subTest(url=url):
                app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root,
                                           config={"text_model": {"model": "test/model"}, "billing_portal_url": url},
                                           browser=self.browser, relay_url="https://relay.test")
                client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
                client.get("/", params={"t": SECRET})
                page = client.get("/settings").text
                self.assertNotIn("coming later", page)
                if url:
                    self.assertIn('id="s-manage-subscription" href="%s" target="_blank" rel="noopener noreferrer">'
                                  "Manage subscription</a>" % portal, page)
                    self.assertIn(settings_page.PORTAL_LINE, page)
                else:
                    self.assertNotIn("s-manage-subscription", page)
                    self.assertIn("To update your card, see invoices or cancel, email support@yoursidedoor.com.", page)

    def test_a_refused_new_key_keeps_the_old_one(self):
        (self.home / "license.key").write_text(LICENSE_KEY + "\n")
        entitlement_file(self.home)
        with mock.patch.object(setup_steps, "_post_json", return_value=(409, b'{"error": "ACTIVATION_LIMIT"}')):
            response = self.client.post("/settings/license/change", data={"key": "SD-NEWKEY-9999"}, headers=HTMX).text
        self.assertIn("already used on 2 computers", response)
        self.assertEqual(LICENSE_KEY, (self.home / "license.key").read_text().strip())
        rest = outside_box(response, "SD-NEWKEY-9999", "s-license-key")  # kept, hidden, to fix
        self.assertNotIn("SD-NEWKEY-9999", rest)
        self.assertNotIn(LICENSE_KEY, rest)
        self.assertNotIn("SD-NEWKEY-9999", json.dumps(setup_steps.load_state(self.home)))

    def test_expired_license_offers_renew_and_paste_a_new_key(self):
        (self.home / "license.key").write_text(LICENSE_KEY + "\n")
        entitlement_file(self.home, valid=False)  # the heartbeat's signed valid: false
        page = self.client.get("/settings").text
        self.assertIn('id="renew-license" href="https://yoursidedoor.com"', page)
        self.assertIn("Bought a new key? Paste it here", page)

    def test_no_key_yet_offers_activate_and_buy(self):
        page = self.client.get("/settings").text
        self.assertIn('id="s-buy-license"', page)
        response = self.client.post("/settings/license/activate", headers=HTMX).text
        self.assertIn("No license key is saved yet.", response)

    # --- review fixes ------------------------------------------------------------------------------

    def test_removals_and_rewordings_survive_reading_the_same_resume_again(self):
        # T3: a re-read of the same file keeps removals and rewordings and adds no duplicates.
        (self.root / "state").mkdir()
        database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        self.addCleanup(database.close)
        research_workflow.initialize(database)
        application = self.home / "application.pdf"
        application.write_bytes(b"%PDF")
        updates = {"resume_raw": {"sha256": RAW_SHA, "file_name": "cv.pdf"}, "resume": {"path": str(application)}}
        extracted = [{"text": text, "tags": ["backend"], "page": 1}
                     for text in ("Built a payments API.", "Led a team of five.", "Wrote the on-call guide.")]
        self.assertIsNone(resume_intake._save(database, None, dict(updates), claims=extracted[:2], text=""))
        ids = {claim["text"]: claim_id for claim_id, claim in research_workflow.current_claims(database)}
        resume_intake.remove_claim(database, ids["Built a payments API."]); database.commit()
        resume_intake.edit_wording(database, ids["Led a team of five."], "I led five engineers"); database.commit()
        counted = []
        reread = [dict(claim, tags=["leadership"]) for claim in extracted]  # the AI tags differently this time
        self.assertIsNone(resume_intake._save(database, None, dict(updates), claims=reread, text="", counted=counted))
        claims = research_workflow.current_claims(database)
        self.assertEqual(["Led a team of five.", "Wrote the on-call guide."], sorted(claim["text"] for _, claim in claims))
        self.assertEqual(["I led five engineers"], [claim["invitation_mapping"]["text"] for _, claim in claims
                                                    if claim["text"] == "Led a team of five."])
        self.assertEqual([2], counted)
        # Every sentence removed: a re-read says so instead of bringing them back.
        for claim_id, _ in claims:
            resume_intake.remove_claim(database, claim_id); database.commit()
        failed = resume_intake._save(database, None, dict(updates), claims=reread, text="")
        self.assertEqual("ALL_CLAIMS_REMOVED", failed["code"])
        self.assertEqual([], research_workflow.current_claims(database))

    def test_gr1_settings_offers_test_saved_key_for_an_untested_key(self):
        (self.home / setup_steps.OPENROUTER_KEY_FILE).write_text(OPENROUTER_KEY + "\n")
        page = self.client.get("/settings").text
        self.assertIn("It hasn't been tested yet.", page)
        self.assertIn("Test saved key", page)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            tested = self.client.post("/settings/openrouter/test", headers=HTMX).text
        self.assertIn("Key works.", tested)
        self.assertNotIn("Test saved key", tested)

    def test_a_fixed_openrouter_key_unlocks_the_resume_out_of_band(self):
        # T4
        self.assertIn("needs your OpenRouter key first", self.client.get("/settings").text)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            response = self.client.post("/settings/openrouter", data={"key": OPENROUTER_KEY}, headers=HTMX).text
        resume = response.split('id="resume" hx-swap-oob="true"')[1].split("</section>")[0]
        self.assertIn('name="resume"', resume)
        self.assertNotIn("needs your OpenRouter key first", resume)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            again = self.client.post("/settings/openrouter/test", headers=HTMX).text
        self.assertNotIn('id="resume"', again)  # nothing changed there: not sent

    def test_an_activated_license_unlocks_linkedin_out_of_band(self):
        # T4
        signed = relay_answer(self.home)
        transport = mock.Mock(return_value=(403, {"error": "LICENSE_NOT_LINKED"}))
        with mock.patch.object(setup_steps, "_post_json", return_value=(200, signed.encode())), \
                mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport):
            response = self.client.post("/settings/license/change", data={"key": LICENSE_KEY}, headers=HTMX).text
        linkedin = response.split('id="connection-linkedin" hx-swap-oob="true"')[1]
        self.assertIn("Connect LinkedIn</button>", linkedin)
        self.assertNotIn("Needs your license first", linkedin)

    def test_e8t3_a_new_key_clears_the_license_pause_at_once_and_keeps_the_users(self):
        import scheduler_control
        import scheduler_loop
        (self.home / "license.key").write_text(LICENSE_KEY + "\n")
        entitlement_file(self.home, valid=False)                     # expired
        (self.root / "state").mkdir(exist_ok=True)
        database = sqlite3.connect(self.root / scheduler_loop.DATABASE)
        self.addCleanup(database.close)
        scheduler_control.initialize(database)
        database.commit()
        scheduler_loop.sync_license_pause(self.root, self.home)
        scheduler_control.set_user_pause(database, True)
        self.assertEqual(scheduler_control.state(database)["license_pause_reason"], "expired")
        transport = mock.Mock(return_value=(403, {"error": "LICENSE_NOT_LINKED"}))
        with mock.patch.object(setup_steps, "_post_json", return_value=(200, relay_answer(self.home).encode())), \
                mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport):
            self.client.post("/settings/license/change", data={"key": LICENSE_KEY}, headers=HTMX)
        flags = scheduler_control.state(database)
        self.assertIsNone(flags["license_paused_at"])
        self.assertTrue(flags["user_paused_at"])

    def test_notices_and_saved_show_only_in_the_response_to_their_action(self):
        # T9
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        response = self.client.post("/settings/resume", data={}, files={"other": ("x", b"x")}, headers=HTMX).text
        self.assertIn("Choose your résumé file first.", response)
        self.assertNotIn("Choose your résumé file first.", self.client.get("/settings").text)
        setup_steps.record(self.home, "profile", "done", "Saved.", form=FORM)  # left over from Setup
        self.assertNotIn(">Saved.<", self.client.get("/settings").text)
        saved = self.client.post("/settings/profile", data=FORM, headers=HTMX).text
        self.assertIn(">Saved.<", saved)
        self.assertNotIn(">Saved.<", self.client.get("/settings").text)

    def test_reading_an_earlier_resume_again_keeps_its_own_wording_once(self):
        # T15: A, reword, remove, B, A again: A shows the user's wording once, no extracted duplicate.
        (self.root / "state").mkdir()
        database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        self.addCleanup(database.close)
        research_workflow.initialize(database)
        application = self.home / "application.pdf"
        application.write_bytes(b"%PDF")
        other_sha = hashlib.sha256(b"resume b").hexdigest()
        a = {"resume_raw": {"sha256": RAW_SHA, "file_name": "a.pdf"}, "resume": {"path": str(application)}}
        b = {"resume_raw": {"sha256": other_sha, "file_name": "b.pdf"}, "resume": {"path": str(application)}}
        extracted = [{"text": text, "tags": ["backend"], "page": 1} for text in ("Built X.", "Ran Y.", "Led Z.")]
        self.assertIsNone(resume_intake._save(database, None, dict(a), claims=extracted, text=""))
        ids = {claim["text"]: claim_id for claim_id, claim in research_workflow.current_claims(database)}
        resume_intake.edit_wording(database, ids["Built X."], "I built X"); database.commit()
        resume_intake.remove_claim(database, ids["Ran Y."]); database.commit()
        self.assertIsNone(resume_intake._save(database, None, dict(b), claims=[{"text": "Other.", "tags": ["x"]}], text=""))
        self.assertIsNone(resume_intake._save(database, None, dict(a), claims=extracted, text=""))
        claims = [claim for _, claim in research_workflow.current_claims(database)]
        self.assertEqual(["Built X.", "Led Z."], sorted(claim["text"] for claim in claims))
        self.assertEqual("I built X", next(claim["invitation_mapping"]["text"] for claim in claims if claim["text"] == "Built X."))

    def test_every_sentence_removed_offers_a_new_upload_not_try_again(self):
        # T16: Settings and Setup step 2.
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        (self.home / "profile.json").write_text(json.dumps({"resume_raw": {"file_name": "cv.pdf", "path": "/x", "sha256": RAW_SHA}}))
        raw = {"path": "/x", "file_name": "cv.pdf", "kind": "PDF"}
        setup_steps.record(self.home, "resume", "failed", resume_intake.ALL_REMOVED, code="ALL_CLAIMS_REMOVED", raw=raw)
        settings = self.client.get("/settings").text.split('id="resume"')[1].split("</section>")[0]
        setup = self.client.get("/setup").text.split('id="step-2"')[1].split("</li>")[0]
        for page in (settings, setup):
            self.assertNotIn("/resume/retry", page)
            self.assertIn("Upload a different résumé", page)
            self.assertIn('name="resume"', page)
        setup_steps.record(self.home, "resume", "failed", "The AI call took too long. Try again.", code="AI_TIMEOUT", raw=raw)
        self.assertIn('hx-post="/settings/resume/retry"', self.client.get("/settings").text)
        self.assertIn('hx-post="/setup/resume/retry"', self.client.get("/setup").text)

    def test_setup_step_8_shows_the_saved_email_not_a_failed_form(self):
        # T17
        self.client.post("/setup/profile", data=FORM, headers=HTMX)
        self.client.post("/setup/profile", data=dict(FORM, email="b@x.com", phone="abc"), headers=HTMX)
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            step = self.client.get("/setup").text.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertIn("<strong>alex@example.com</strong>", step)
        self.assertNotIn("b@x.com", step)


if __name__ == "__main__":
    unittest.main()
