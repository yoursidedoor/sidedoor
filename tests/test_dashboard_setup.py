"""The Setup page and Overview's not-running state (E4.T1 option C, E6.T0 spec 3.2 and 5).

Needs the pinned dashboard packages and httpx (`pip install -e '.[test]'`); skipped without them.
Every network call is faked (text_model transport, relay, Unipile, Codex, Chrome).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import license_fixtures
import setup_steps

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard
    import orchestrator


PORT = 47823
SECRET = "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
OPENROUTER_KEY = "sk-or-v1-never-shown"
OK_ANSWER = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}}).encode()


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def is_open(self):
        return False

    def open(self):
        self.calls.append("open")
        return "busy"

    def close(self, then=None):
        self.calls.append("close")
        return then() if then else "closed"


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class SetupPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        self.browser = FakeBrowser()
        app = dashboard.create_app(SECRET, PORT, home=self.home, config={"text_model": {"model": "test/model"}},
                                   browser=self.browser, relay_url="https://relay.test")
        self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        self.client.get("/", params={"t": SECRET})

    def tearDown(self):
        self.directory.cleanup()

    def test_setup_lists_the_eleven_steps_in_order_with_tab_and_banner(self):
        page = self.client.get("/setup").text
        positions = [page.index('id="step-%d"' % number) for number in range(1, 12)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('href="/setup"', page)
        self.assertIn("Setup is not finished: 8 steps left.", page)
        self.assertIn("Needs OpenRouter first", page)
        self.assertIn("No license yet", page)
        self.assertNotIn("<script>", page)
        self.assertIn('value="automatic" checked', page)

    def test_openrouter_step_unlocks_the_resume_and_never_shows_the_key(self):
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)) as post:
            response = self.client.post("/setup/openrouter", data={"key": OPENROUTER_KEY}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args[0][2]["Authorization"], "Bearer " + OPENROUTER_KEY)
        self.assertIn("Key works.", response.text)
        self.assertIn('id="step-2" hx-swap-oob="true"', response.text)
        self.assertIn('name="resume"', response.text)
        page = self.client.get("/setup").text
        self.assertNotIn(OPENROUTER_KEY, page + response.text)

    def test_gr1_a_saved_untested_key_is_tested_without_retyping(self):
        # A key saved before the tested mark existed (e.g. after [Reset setup file]).
        (self.home / setup_steps.OPENROUTER_KEY_FILE).write_text(OPENROUTER_KEY + "\n")
        page = self.client.get("/setup").text
        self.assertIn("Test saved key", page)
        self.assertIn(OPENROUTER_KEY[-4:], page)
        self.assertNotIn(OPENROUTER_KEY, page)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)) as post:
            response = self.client.post("/setup/openrouter/test", headers=HTMX)
        self.assertEqual(post.call_args[0][2]["Authorization"], "Bearer " + OPENROUTER_KEY)
        self.assertIn("Key works.", response.text)
        self.assertTrue(setup_steps.openrouter_tested(self.home))
        self.assertNotIn("Test saved key", self.client.get("/setup").text)

    def test_resume_is_locked_until_openrouter_then_reads_in_the_background(self):
        files = {"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
        self.client.post("/setup/resume", files=files, headers=HTMX)
        self.assertFalse((self.home / "resume").exists())
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            response = self.client.post("/setup/resume", files=files, headers=HTMX)
            self.assertIn("Reading your résumé", response.text)
            self.assertIn('hx-get="/setup/resume/status" hx-trigger="every 3s"', response.text)
            self.assertIn("cv.pdf", response.text)
            self.assertNotIn('name="resume"', response.text.split('id="step-3"')[0])  # no upload while reading
            busy = self.client.post("/setup/resume", files={"resume": ("b.pdf", b"%PDF-b", "application/pdf")}, headers=HTMX)
            self.assertIn(setup_steps.STILL_READING, busy.text)
            poll = self.client.get("/setup/resume/status", headers={"hx-request": "true"}).text
            self.assertIn("Still reading", poll)
            self.assertNotIn('id="step-7"', poll)
        self.assertEqual(len(spawned), 1)
        self.assertEqual((self.home / "resume" / "original.pdf").read_bytes(), b"%PDF-1.4")
        with mock.patch.object(setup_steps.resume_intake, "ingest",
                               return_value={"state": "FAILED", "code": "X", "reason": "Your OpenRouter account has no credits left."}):
            spawned[0]()
        failed = self.client.get("/setup/resume/status", headers={"hx-request": "true"}).text
        self.assertIn("no credits left", failed)
        self.assertIn('hx-post="/setup/resume/retry"', failed)
        self.assertIn('id="setup-progress" hx-swap-oob="true"', failed)
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            self.client.post("/setup/resume/retry", headers=HTMX)
        with mock.patch.object(setup_steps.resume_intake, "ingest", return_value={"state": "READY", "claims": 4, "page_count": 1}):
            spawned[1]()
        done = self.client.get("/setup/resume/status", headers={"hx-request": "true"}).text
        self.assertIn("Résumé read: 4 sentences", done)
        self.assertIn("Upload a different résumé", done)

    def test_an_interrupted_read_offers_try_again(self):
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        (self.home / "resume").mkdir()
        (self.home / "resume" / "original.pdf").write_bytes(b"%PDF")
        setup_steps.record(self.home, "resume", "reading", "x", read_id="lost",
                           raw={"path": str(self.home / "resume" / "original.pdf"), "file_name": "cv.pdf", "kind": "PDF"})
        page = self.client.get("/setup").text
        self.assertIn(setup_steps.INTERRUPTED, page)
        self.assertIn('hx-post="/setup/resume/retry"', page)

    def test_pick_list_prefix_search_and_choice(self):
        options = self.client.get("/setup/pick/roles", params={"q_roles": "sof"}, headers={"hx-request": "true"}).text
        self.assertIn(">Software Engineering</button>", options)
        self.assertNotIn("Backend Engineering", options)
        chosen = self.client.post("/setup/pick/roles/add", data={"pick_value": "Software Engineering",
                                                                 "roles": ["Backend Engineering"]}, headers=HTMX).text
        self.assertIn('name="roles" value="Backend Engineering"', chosen)
        self.assertIn('name="roles" value="Software Engineering"', chosen)
        typed = self.client.post("/setup/pick/levels/add", data={"pick_value": "Typed by hand"}, headers=HTMX).text
        self.assertNotIn("Typed by hand", typed)
        single = self.client.post("/setup/pick/current_location/add", data={
            "pick_value": "Washington, DC, United States", "current_location": "Berlin, Germany"}, headers=HTMX).text
        self.assertNotIn("Berlin", single)
        self.assertEqual(self.client.get("/setup/pick/nothing").status_code, 404)

    def test_profile_form_saves_and_outlines_errors(self):
        response = self.client.post("/setup/profile", data={"given_name": "Alex"}, headers=HTMX)
        self.assertIn("field has-error", response.text)
        self.assertIn("Some fields need a fix.", response.text)
        self.assertFalse((self.home / "profile.json").exists())

    def test_linkedin_box_lets_the_browser_send_any_paste(self):
        page = self.client.get("/setup").text
        self.assertIn('id="linkedin" type="text" inputmode="url"', page)
        self.assertNotIn('id="linkedin" type="url"', page)

    def test_mode_toggle(self):
        response = self.client.post("/setup/mode", data={"mode": "review"}, headers=HTMX)
        self.assertIn('value="review" checked', response.text)
        self.assertIn(">Review</a>", response.text)  # the header chip, out of band

    def test_license_step_with_buy_link_and_guide(self):
        page = self.client.get("/setup").text
        self.assertIn('id="buy-license" href="https://yoursidedoor.com" target="_blank"', page)
        step7 = page[page.index('id="step-7"'):page.index('id="step-8"')]   # locked until the license (step 5)
        self.assertIn("sends the note after they accept. To send notes with invitations, upgrade your LinkedIn account to Premium.", step7)
        guide = self.client.get("/setup/guide/license", headers={"hx-request": "true"}).text
        self.assertIn("CLAIM_NOT_FOUND", guide)
        now = license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)
        answer = license_fixtures.signed("SD-SECRETKEY", setup_steps.machine_id(self.home), now, status="trialing")
        with mock.patch.object(setup_steps, "_post_json", return_value=(200, json.dumps(answer).encode())) as post:
            response = self.client.post("/setup/license", data={"key": "SD-SECRETKEY"}, headers=HTMX)
        self.assertEqual(post.call_args[0][0], "https://relay.test/license/activate")
        self.assertIn("Active: Trial", response.text)
        self.assertIn(">Trial</a>", response.text)
        self.assertNotIn("SD-SECRETKEY", response.text + self.client.get("/setup").text)

    def test_linkedin_connect_reads_accounts_first_and_shows_the_link_once(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        transport = mock.Mock(return_value=(403, {"error": "LICENSE_NOT_LINKED"}))
        with mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport), \
                mock.patch.object(setup_steps.unipile_port, "hosted_link", return_value="https://unipile.test/link") as link:
            status = self.client.get("/setup/linkedin/status", headers={"hx-request": "true"})
            self.assertIn("Connect LinkedIn</button>", status.text)
            self.assertIn("sends the note after they accept. To send notes with invitations, upgrade your LinkedIn account to Premium.", status.text)  # owner, 2026-10-27
            response = self.client.post("/setup/linkedin/connect", headers=HTMX)
            self.assertEqual(link.call_args[0][2], "create")
            self.assertIn('href="https://unipile.test/link" target="_blank"', response.text)
            self.assertIn('hx-trigger="every 5s"', response.text)
            poll = self.client.get("/setup/linkedin/status", params={"poll": "1"}, headers={"hx-request": "true"})
            self.assertIn("Waiting for LinkedIn", poll.text)
            self.assertNotIn("unipile.test", self.client.get("/setup").text)
            transport.return_value = (200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "OK"}]})
            done = self.client.get("/setup/linkedin/status", params={"poll": "1"}, headers={"hx-request": "true"})
            self.assertEqual(done.headers["hx-retarget"], "#step-7")
            self.assertIn("LinkedIn connected: Alex.", done.text)
            # Listed: a second create would add a paid Unipile account, so Connect does nothing.
            link.reset_mock()
            self.client.post("/setup/linkedin/connect", headers=HTMX)
            link.assert_not_called()

    def test_waiting_view_always_offers_a_fresh_link(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        transport = mock.Mock(return_value=(403, {"error": "LICENSE_NOT_LINKED"}))
        with mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport), \
                mock.patch.object(setup_steps.unipile_port, "hosted_link", side_effect=["https://u.test/1", "https://u.test/2"]) as link:
            self.client.post("/setup/linkedin/connect", headers=HTMX)
            other = self.client.post("/setup/mode", data={"mode": "automatic"}, headers=HTMX).text
            step7 = other[other.index('id="step-7"'):]
            self.assertIn('hx-post="/setup/linkedin/connect"', step7)
            self.assertIn("Connect again", step7)
            again = self.client.post("/setup/linkedin/connect", headers=HTMX).text
            self.assertEqual(link.call_count, 2)
            self.assertIn('href="https://u.test/2"', again)
            self.assertIn("Get a new link", again)

    def test_typed_pick_values(self):
        offer = self.client.get("/setup/pick/roles", params={"q_roles": "Quantum Whisperer"}, headers={"hx-request": "true"}).text
        self.assertIn('Use "Quantum Whisperer"', offer)
        self.assertNotIn("Use ", self.client.get("/setup/pick/levels", params={"q_levels": "Grandmaster"},
                                                 headers={"hx-request": "true"}).text)
        added = self.client.post("/setup/pick/roles/add", data={"pick_value": "Quantum Whisperer"}, headers=HTMX).text
        self.assertIn('name="roles" value="Quantum Whisperer"', added)
        refused = self.client.post("/setup/pick/levels/add", data={"pick_value": "Grandmaster"}, headers=HTMX).text
        self.assertNotIn("Grandmaster", refused)

    def test_a_step_that_breaks_after_setup_shows_a_banner_not_the_setup_tab(self):
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-21T00:00:00Z",
                                                            codex_path="/gone/codex"), self.home)
        setup_steps.record(self.home, "codex", "done", "ok")
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            page = self.client.get("/sent").text
        self.assertIn("Codex stopped working.", page)
        self.assertIn('href="/settings#connections">Fix', page)
        self.assertNotIn('href="/setup"', page)
        self.assertNotIn("Google Chrome stopped working.", page)

    def test_mailbox_banner_points_to_settings_once_setup_is_finished(self):
        setup_steps.record(self.home, "email", "done", "Sign in to a@example.com.", mismatch=True)
        setup_steps.update_state(lambda state: state.update(locked_email="a@example.com"), self.home)
        self.assertIn('href="/setup#step-8">Open Sidedoor', self.client.get("/sent").text)
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-21T00:00:00Z"), self.home)
        page = self.client.get("/sent").text
        self.assertIn('href="/settings#connections">Open Sidedoor', page)
        self.assertNotIn('href="/setup', page)

    def test_linkedin_recovers_after_a_refused_subscription(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        transport = mock.Mock(return_value=(401, {"error": "LICENSE_INVALID"}))
        with mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport):
            stuck = self.client.get("/setup/linkedin/status", headers={"hx-request": "true"}).text
            self.assertIn("Your subscription isn", stuck)
            self.assertIn(">Check again</button>", stuck)
            self.assertNotIn('hx-trigger="click, load"', stuck)  # a re-rendered step never re-reads by itself
            self.assertIn('hx-trigger="click, load"', self.client.get("/setup").text)  # the page load does
            transport.return_value = (403, {"error": "LICENSE_NOT_LINKED"})
            again = self.client.get("/setup/linkedin/status", headers={"hx-request": "true"}).text
            self.assertIn("Connect LinkedIn</button>", again)
            # Step 5 activating a key re-reads /accounts too.
            transport.return_value = (401, {"error": "LICENSE_INVALID"})
            self.client.get("/setup/linkedin/status", headers={"hx-request": "true"})
            transport.return_value = (200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "OK"}]})
            signed = json.dumps(license_fixtures.signed("SD-NEWKEY", setup_steps.machine_id(self.home),
                                                        license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)))
            with mock.patch.object(setup_steps, "_post_json", return_value=(200, signed.encode())):
                activated = self.client.post("/setup/license", data={"key": "SD-NEWKEY"}, headers=HTMX).text
            self.assertIn("LinkedIn connected: Alex.", activated)

    def test_codex_step(self):
        with mock.patch.object(setup_steps, "check_codex",
                               side_effect=lambda home: setup_steps.record(home, "codex", "failed", "Codex is not installed.")):
            response = self.client.post("/setup/codex", headers=HTMX)
        self.assertIn("Codex is not installed.", response.text)
        self.assertIn('hx-get="/setup/guide/codex"', response.text)

    def test_browser_busy_and_email_check_locked(self):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            response = self.client.post("/setup/browser/open", headers=HTMX)
        self.assertIn(setup_steps.BUSY_LINE, response.text)
        self.client.post("/setup/email", headers=HTMX)
        self.assertNotIn("close", self.browser.calls)  # step 9 is locked until steps 3, 6 and 8

    def test_email_check_runs_through_the_browser_hold(self):
        setup_steps.update_state(lambda state: state.update(codex_path=sys.executable, webmail={"label": "Gmail", "url": "x"}),
                                 self.home)
        for key in ("profile", "codex", "browser"):
            setup_steps.record(self.home, key, "done", "ok")
        # Without Sidedoor's browser runtime (E7.T1) on this machine the check says it is not there.
        with mock.patch.dict(os.environ, {"SIDEDOOR_BROWSER_RUNTIME": str(self.home / "no-runtime")}):
            response = self.client.post("/setup/email", headers=HTMX)
        self.assertEqual(self.browser.calls, ["close"])
        self.assertIn("Not built yet", response.text)
        self.assertIn("not installed on this computer", response.text)

    def test_start_at_login_registers_through_the_runner_and_start_is_locked(self):
        import login_item
        calls = []
        with mock.patch.object(login_item, "RUNNER", lambda argv: calls.append(argv) or (0, "")), \
                mock.patch.object(login_item, "USER_HOME", self.home / "user"), \
                mock.patch.object(login_item, "PLATFORM", "darwin"), mock.patch.object(login_item, "UID", 501), \
                mock.patch.object(dashboard.scheduler_loop, "ROOT", self.home / "root"):
            self.assertIn("Registered", self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertEqual([call[:2] for call in calls[:2]], [["launchctl", "enable"], ["launchctl", "bootstrap"]])
        self.assertIn("Needs <a href=\"#step-1\">step 1</a> first.", self.client.post("/setup/start", headers=HTMX).text)
        self.assertNotIn("start", setup_steps.load_state(self.home)["steps"])

    def test_overview_not_running_with_start(self):
        self.assertNotIn("Sidedoor is not running", self.client.get("/").text)
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-21T00:00:00Z"), self.home)
        page = self.client.get("/").text
        self.assertIn("Sidedoor is not running", page)
        self.assertIn('action="/start"', page)
        self.assertNotIn('href="/setup"', page)
        self.assertNotIn("Setup is not finished", page)
        # No scheduler loop in this process (only `sidedoor serve` installs one): it says so in plain words.
        self.assertIn("start from this window", self.client.post("/start", headers=ORIGIN).text)

    def test_plain_form_post_redirects_back_to_its_step(self):
        response = self.client.post("/setup/mode", data={"mode": "automatic"}, headers=ORIGIN)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/setup#step-4"))
        self.assertEqual(self.client.post("/setup/mode", data={"mode": "automatic"}).status_code, 403)

    def test_guides(self):
        for name in dashboard.GUIDES:
            self.assertEqual(self.client.get("/setup/guide/" + name).status_code, 200)
        self.assertEqual(self.client.get("/setup/guide/../base").status_code, 404)
        self.assertIn("~/.local/bin", self.client.get("/setup/guide/codex", headers={"hx-request": "true"}).text)


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class SetupCommandTest(unittest.TestCase):
    def test_setup_starts_the_dashboard_on_the_setup_page(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        for command, path in (("setup", "/setup"), ("dashboard", "/")):
            with mock.patch.object(sys, "argv", ["sidedoor", command, "--no-browser"]), \
                    mock.patch.object(dashboard, "serve", return_value=0) as serve:
                self.assertEqual(orchestrator.main(), 0)
            serve.assert_called_once_with(open_browser=False, path=path, start=False, tray=True, resume=False)


if __name__ == "__main__":
    unittest.main()
