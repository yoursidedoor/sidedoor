"""The Setup page and Overview's not-running state (E4.T1 option C, E6.T0 spec 3.2 and 5).

Needs the pinned dashboard packages and httpx (`pip install -e '.[test]'`); skipped without them.
Every network call is faked (text_model transport, relay, Unipile, Codex, Chrome).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
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
DONE = '<span class="chip chip-success">Done</span>'
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

    def test_setup_lists_the_ten_steps_in_order_with_tab_and_banner(self):
        page = self.client.get("/setup").text
        positions = [page.index('id="step-%d"' % number) for number in range(1, 11)]
        self.assertEqual(positions, sorted(positions))
        # S1a: the license key comes first; L2: the engine step is titled "Codex".
        titles = [page.split('id="step-%d"' % number)[1].split("<h2>")[1].split("</h2>")[0] for number in (1, 2, 6)]
        self.assertEqual(titles, ["License key", "Create your OpenRouter key", "Choose your AI"])   # round 1 (9)
        self.assertIn("<legend>Which AI Sidedoor uses</legend>", self.client.get("/setup?open=6").text)   # L1
        self.assertNotIn("Codex and ChatGPT", page)
        self.assertIn('href="/setup"', page)
        self.assertIn("Setup is not finished: 7 steps left.", self.client.get("/settings").text)
        self.assertIn("Waiting for: Create your OpenRouter key", page)   # S1: a locked step names what comes first
        self.assertNotIn("Setup is not finished", page.split("<main")[0] + page.split("</main>")[1])   # round 1 (6)
        self.assertIn("No license yet", page)
        self.assertNotIn("<script>", page)
        self.assertIn('value="automatic" checked', self.client.get("/setup?open=5").text)

    def test_openrouter_step_unlocks_the_resume_and_never_shows_the_key(self):
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)) as post:
            response = self.client.post("/setup/openrouter", data={"key": OPENROUTER_KEY}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args[0][2]["Authorization"], "Bearer " + OPENROUTER_KEY)
        step2 = response.text.split('id="step-2"')[1].split("</li>")[0]
        self.assertIn(DONE, step2)
        self.assertIn('hx-get="/setup/step/2?collapse=1" hx-trigger="load delay:4s"', step2)   # round 2 (5): shown, then collapses
        self.assertIn("Key works.", step2)
        collapsed = self.client.get("/setup/step/2?collapse=1", headers={"hx-request": "true"}).text
        self.assertIn('step-success step-collapsed" id="step-2"', collapsed)
        self.assertIn('id="step-3" hx-swap-oob="true"', response.text)   # the résumé, step 3 since S1a
        self.assertNotIn("Waiting for: Create your OpenRouter key", response.text)   # unlocked
        self.assertIn('name="resume"', self.client.get("/setup/step/3", headers={"hx-request": "true"}).text)
        page = self.client.get("/setup").text
        self.assertNotIn(OPENROUTER_KEY, page + response.text)

    def test_openrouter_step_shows_how_to_create_a_key_and_names_a_pasted_license_key(self):
        page = self.client.get("/setup?open=2").text
        step = page[page.index('id="step-2"'):page.index('id="step-3"')]
        # B6, round 1 (14): no list and no OpenRouter button on the page (the guide has the steps).
        self.assertNotIn('class="key-how"', step)
        self.assertNotIn('id="open-openrouter-keys"', step)
        with mock.patch.object(setup_steps.text_model, "_post") as post:
            response = self.client.post("/setup/openrouter", data={"key": "SD-SECRETKEY"}, headers=HTMX)
        post.assert_not_called()   # S2: never sent to OpenRouter
        self.assertIn("That&#39;s your Sidedoor license key. It goes in the License key step.", response.text)
        self.assertIn("License key", page.split('id="step-1"')[1].split("</h2>")[0])

    def test_a_license_key_pasted_over_a_working_openrouter_key_changes_nothing(self):
        # U1: step 2 stays done, the résumé and Start keep their locks as they were, the saved key is shown.
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            self.client.post("/setup/openrouter", data={"key": OPENROUTER_KEY}, headers=HTMX)
        before = setup_steps.progress(self.home)
        with mock.patch.object(setup_steps.text_model, "_post") as post:
            response = self.client.post("/setup/openrouter", data={"key": "SD-ABC"}, headers=HTMX).text
        post.assert_not_called()
        after = setup_steps.progress(self.home)
        self.assertEqual((after["done"], after["locked"]), (before["done"], before["locked"]))
        self.assertEqual(setup_steps.saved_openrouter_key(self.home), OPENROUTER_KEY)
        step = response.split('id="step-2"')[1].split('id="step-3"')[0]
        notice = step.index("That&#39;s your Sidedoor license key. It goes in the License key step.")
        # Next to the saved key, not in the top message slot; no [Test saved key]: this key passed (B3).
        self.assertLess(step.index("A key is saved, ending in <code>"), notice)
        self.assertNotIn(">Test saved key</button>", step)
        self.assertLess(notice, step.index('name="key"'))
        self.assertNotIn("Key works.", step)
        self.assertIn('<span class="chip chip-success">Done</span>', step)
        self.assertNotIn(OPENROUTER_KEY, response)

    def test_test_saved_key_on_a_saved_license_key_says_so_on_setup_and_settings(self):
        # A 0.3.0 user whose saved OpenRouter key is their SD- key: [Test saved key] must answer.
        setup_steps._save_key("openrouter", "SD-SAVEDLICENSE", self.home, False)
        with mock.patch.object(setup_steps.text_model, "_post") as post:
            setup_page = self.client.post("/setup/openrouter/test", headers=HTMX).text
            settings = self.client.post("/settings/openrouter/test", headers=HTMX).text
        post.assert_not_called()
        self.assertIn("That&#39;s your Sidedoor license key. It goes in the License key step.", setup_page.split('id="step-3"')[0])
        self.assertIn("That&#39;s your Sidedoor license key. It goes in License, below.", settings)
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["openrouter"]["status"], "failed")

    def test_gr1_a_saved_untested_key_is_tested_without_retyping(self):
        # A key saved before the tested mark existed (e.g. after [Reset setup file]).
        (self.home / setup_steps.OPENROUTER_KEY_FILE).write_text(OPENROUTER_KEY + "\n")
        page = self.client.get("/setup?open=2").text
        self.assertIn("Test saved key", page)
        self.assertIn(OPENROUTER_KEY[-4:], page)
        self.assertNotIn(OPENROUTER_KEY, page)
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)) as post:
            response = self.client.post("/setup/openrouter/test", headers=HTMX)
        self.assertEqual(post.call_args[0][2]["Authorization"], "Bearer " + OPENROUTER_KEY)
        self.assertIn(DONE, response.text.split('id="step-2"')[1].split("</li>")[0])
        self.assertTrue(setup_steps.openrouter_tested(self.home))
        self.assertNotIn("Test saved key", self.client.get("/setup?open=2").text)

    def test_resume_is_locked_until_openrouter_then_reads_in_the_background(self):
        files = {"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
        self.client.post("/setup/resume", files=files, headers=HTMX)
        self.assertFalse((self.home / "resume").exists())
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        spawned = []
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            response = self.client.post("/setup/resume", files=files, headers=HTMX)
            self.assertIn("Reading your résumé", response.text)
            self.assertIn('hx-get="/setup/resume/status?open=1" hx-trigger="every 3s"', response.text)
            self.assertIn("cv.pdf", response.text)
            self.assertNotIn('name="resume"', response.text.split('id="step-3"')[0])  # no upload while reading
            busy = self.client.post("/setup/resume", files={"resume": ("b.pdf", b"%PDF-b", "application/pdf")}, headers=HTMX)
            self.assertIn(setup_steps.STILL_READING, busy.text)
            poll = self.client.get("/setup/resume/status?open=1", headers={"hx-request": "true"}).text
            self.assertIn("Still reading", poll)
            self.assertNotIn('id="step-7"', poll)
        self.assertEqual(len(spawned), 1)
        self.assertEqual((self.home / "resume" / "original.pdf").read_bytes(), b"%PDF-1.4")
        with mock.patch.object(setup_steps.resume_intake, "ingest",
                               return_value={"state": "FAILED", "code": "X", "reason": "Your OpenRouter account has no credits left."}):
            spawned[0]()
        failed = self.client.get("/setup/resume/status?open=1", headers={"hx-request": "true"}).text
        self.assertIn("no credits left", failed)
        self.assertIn('hx-post="/setup/resume/retry"', failed)
        self.assertIn('id="setup-progress" hx-swap-oob="true"', failed)
        with mock.patch.object(setup_steps, "_spawn", spawned.append):
            self.client.post("/setup/resume/retry", headers=HTMX)
        with mock.patch.object(setup_steps.resume_intake, "ingest", return_value={"state": "READY", "claims": 4, "page_count": 1}):
            spawned[1]()
        done = self.client.get("/setup/resume/status?open=1", headers={"hx-request": "true"}).text
        self.assertIn(DONE, done.split('id="step-3"')[1].split("</li>")[0])   # round 1 (1): done collapses
        self.assertIn("Upload a different résumé", self.client.get("/setup?open=3").text)

    def test_an_interrupted_read_offers_try_again(self):
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        (self.home / "resume").mkdir()
        (self.home / "resume" / "original.pdf").write_bytes(b"%PDF")
        setup_steps.record(self.home, "resume", "reading", "x", read_id="lost",
                           raw={"path": str(self.home / "resume" / "original.pdf"), "file_name": "cv.pdf", "kind": "PDF"})
        page = self.client.get("/setup?open=3").text
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
        page = self.client.get("/setup?open=4").text
        self.assertIn('id="linkedin" type="text" inputmode="url"', page)
        self.assertNotIn('id="linkedin" type="url"', page)

    def test_mode_toggle(self):
        response = self.client.post("/setup/mode", data={"mode": "review"}, headers=HTMX)
        self.assertIn('value="review" checked', response.text)
        self.assertIn(">Review</a>", response.text)  # the header chip, out of band

    def test_license_step_with_buy_link_and_guide(self):
        page = self.client.get("/setup").text
        self.assertIn('id="buy-license" href="https://yoursidedoor.com" target="_blank"', page)
        step7 = page[page.index('id="step-7"'):page.index('id="step-8"')]   # locked until the license (step 1)
        self.assertIn("Waiting for: License key", step7)   # S1: a locked step is one line
        self.assertNotIn("<form", step7)
        guide = self.client.get("/setup/guide/license", headers={"hx-request": "true"}).text
        self.assertIn("your key isn't ready yet", guide)
        now = license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)
        answer = license_fixtures.signed("SD-SECRETKEY", setup_steps.machine_id(self.home), now, status="trialing")
        with mock.patch.object(setup_steps, "_post_json", return_value=(200, json.dumps(answer).encode())) as post:
            response = self.client.post("/setup/license", data={"key": "SD-SECRETKEY"}, headers=HTMX)
        self.assertEqual(post.call_args[0][0], "https://relay.test/license/activate")
        self.assertIn("Active: Trial", self.client.get("/setup?open=1").text)
        self.assertIn(">Trial</a>", response.text)
        self.assertNotIn("SD-SECRETKEY", response.text + self.client.get("/setup").text)

    def test_linkedin_connects_with_the_browsers_sign_in(self):
        # E18: one sign-in, in Sidedoor's browser; [I'm signed in] sends that session through the relay.
        import linkedin_session
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        answers = {"accounts": (403, {"error": "LICENSE_NOT_LINKED"})}
        calls = []

        def transport(method, path, **named):
            calls.append((method, path))
            if path == "/link/session":
                answers["accounts"] = (200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "OK"}]})
                return 200, {"connected": True, "kind": "create"}
            return answers["accounts"]
        self.addCleanup(linkedin_session.set_reader, linkedin_session.set_reader(lambda home: None))
        with mock.patch.object(setup_steps.unipile_port, "RelayTransport", return_value=transport):
            status = self.client.get("/setup/linkedin/status", headers={"hx-request": "true"})
            self.assertIn("I'm signed in</button>", status.text)
            self.assertIn("Open Sidedoor's browser</button>", status.text)
            self.assertNotIn("Unipile", status.text)   # owner, 2026-11-12: no sentence about where the session goes
            self.assertIn("LinkedIn Premium lets you add a note to invitations.", status.text)   # round 2 (14)
            self.assertNotIn("sign-in page", status.text)   # no second sign-in anywhere
            # Not signed in yet: the step says so, keeps its buttons, and nothing is sent.
            missing = self.client.post("/setup/linkedin/connect", headers=HTMX).text
            self.assertIn("is not signed in to LinkedIn", missing)
            self.assertIn("I'm signed in</button>", missing)
            self.assertNotIn(("POST", "/link/session"), calls)
            with mock.patch.object(self.browser, "open", return_value="opened"):
                opened = self.client.post("/setup/linkedin/open", headers=HTMX).text
            self.assertIn("Sign in to LinkedIn there, then click I&#39;m signed in.", opened)
            linkedin_session.set_reader(lambda home: linkedin_session.Session("COOKIE-MARKER-123", "Agent/1"))
            done = self.client.post("/setup/linkedin/connect", headers=HTMX).text
            self.assertIn(DONE, done.split('id="step-7"')[1].split("</li>")[0])
            self.assertIn(("POST", "/link/session"), calls)
            self.assertNotIn("COOKIE-MARKER-123", done + (self.home / "setup-state.json").read_text())
        step8 = self.client.get("/setup?open=8").text.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertNotIn("LinkedIn", step8)   # A6: one LinkedIn step

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
        self.assertIn('href="/setup?open=8#step-8">Open Sidedoor', self.client.get("/sent").text)   # round 1 (5)
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
            self.assertIn('hx-trigger="click, load"', self.client.get("/setup?open=7").text)  # the page load does
            transport.return_value = (403, {"error": "LICENSE_NOT_LINKED"})
            again = self.client.get("/setup/linkedin/status", headers={"hx-request": "true"}).text
            self.assertIn("I'm signed in</button>", again)
            # Step 5 activating a key re-reads /accounts too.
            transport.return_value = (401, {"error": "LICENSE_INVALID"})
            self.client.get("/setup/linkedin/status", headers={"hx-request": "true"})
            transport.return_value = (200, {"items": [{"type": "LINKEDIN", "name": "Alex", "status": "OK"}]})
            signed = json.dumps(license_fixtures.signed("SD-NEWKEY", setup_steps.machine_id(self.home),
                                                        license_fixtures.dt.datetime.now(license_fixtures.dt.timezone.utc)))
            with mock.patch.object(setup_steps, "_post_json", return_value=(200, signed.encode())):
                activated = self.client.post("/setup/license", data={"key": "SD-NEWKEY"}, headers=HTMX).text
            self.assertIn('<span class="chip chip-success">Done</span>', activated.split('id="step-7"')[1].split("</li>")[0])

    def test_codex_step(self):
        with mock.patch.object(setup_steps, "check_codex",
                               side_effect=lambda home: setup_steps.record(home, "codex", "failed", "Codex is not installed.")):
            response = self.client.post("/setup/codex", headers=HTMX)
        self.assertIn("Codex is not installed.", response.text)
        self.assertIn('<details class="guide-inline" id="guide-codex">', response.text)   # S2: opens in place
        self.assertIn(">Check again</button>", response.text)   # B2: only after a check did not pass

    def test_browser_busy_and_email_check_waits(self):
        # Round 1 (12): the browser step is never locked; only its email check waits for the profile and the AI.
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            response = self.client.post("/setup/browser/open", headers=HTMX)
        self.assertIn(setup_steps.BUSY_LINE, response.text)
        setup_steps.update_state(lambda state: state.update(webmail={"label": "Gmail", "url": "x"}), self.home)
        with mock.patch.object(setup_steps, "run_email_check") as check, \
                mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            self.client.post("/setup/email", headers=HTMX)
            done = self.client.post("/setup/browser/done", headers=HTMX).text
        check.assert_not_called()
        self.assertIn("Finish Profile and search and Choose your AI first.", done)
        self.assertNotIn(">Check</button>", done)

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
            self.assertIn(DONE, self.client.post("/setup/login", data={"enabled": "on"}, headers=HTMX).text)
        self.assertEqual([call[:2] for call in calls[:2]], [["launchctl", "enable"], ["launchctl", "bootstrap"]])
        self.assertIn("Waiting for: License key", self.client.post("/setup/start", headers=HTMX).text)
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
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/setup?open=5#step-5"))
        self.assertEqual(self.client.post("/setup/mode", data={"mode": "automatic"}).status_code, 403)

    def test_guides(self):
        for name in dashboard.GUIDES:
            self.assertEqual(self.client.get("/setup/guide/" + name).status_code, 200)
        self.assertEqual(self.client.get("/setup/guide/../base").status_code, 404)
        # G2: only this computer's commands; G3: only the sign-in way the step shows.
        codex = self.client.get("/setup/guide/codex", headers={"hx-request": "true"}).text
        self.assertIn("<details class=\"guide-inline\" id=\"guide-codex\" open>", codex)   # S2: opens in place
        self.assertIn(">Sign in with ChatGPT</strong>", codex)
        self.assertNotIn("Create new secret key", codex)
        self.assertEqual(("install.sh" in codex, "install.ps1" in codex), (dashboard.GUIDE_PLATFORM == "mac",
                                                                             dashboard.GUIDE_PLATFORM == "windows"))
        setup_steps.update_state(lambda state: state.update(codex_auth_view="api_key"), self.home)
        codex = self.client.get("/setup/guide/codex", headers={"hx-request": "true"}).text
        self.assertIn("Create new secret key", codex)
        self.assertNotIn("Start Codex now", codex)
        for name in dashboard.GUIDES:   # G1, G4: at most five numbered steps, no step numbers
            guide = self.client.get("/setup/guide/" + name, headers={"hx-request": "true"}).text
            self.assertLessEqual(guide.split("<ol>")[1].split("</ol>")[0].count("<li>"), 5, name)
            self.assertNotRegex(guide, r"[Ss]tep \d", name)
            self.assertNotIn("Unipile", guide)

    def test_g2_windows_shows_only_windows_commands(self):
        with mock.patch.object(dashboard, "GUIDE_PLATFORM", "windows"):
            app = dashboard.create_app(SECRET, PORT, home=self.home, config={}, browser=self.browser,
                                       relay_url="https://relay.test")
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        codex = client.get("/setup/guide/codex", headers={"hx-request": "true"}).text
        self.assertIn("install.ps1", codex)
        self.assertNotIn("install.sh", codex)

    # --- Setup simplification (owner picks, 2026-11-08) ---

    def unlock_browser_step(self):
        setup_steps.update_state(lambda state: state.update(codex_path=sys.executable,
                                                            webmail={"label": "Gmail", "url": "https://mail.google.com/"}),
                                 self.home)
        for key in ("profile", "codex"):
            setup_steps.record(self.home, key, "done", "ok")

    def test_s1_an_old_state_file_with_every_step_done_reads_done_with_only_start_open(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        self.unlock_browser_step()
        for key in ("openrouter", "resume", "linkedin", "browser", "email", "login", "start"):
            setup_steps.record(self.home, key, "done", "ok")
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-10-01T00:00:00Z",
                                                            locked_email="a@example.com"), self.home)
        progress = setup_steps.progress(self.home)
        self.assertEqual(progress["left"], 0)
        page = self.client.get("/setup").text
        self.assertIn("All required steps are done", page)
        for number in (1, 2, 3, 4, 5, 6, 7, 8, 10):
            step = page.split('id="step-%d"' % number)[1].split("</li>")[0]
            self.assertIn('<span class="chip chip-success">Done</span>', step, number)
            self.assertEqual("step-collapsed" in page.split('id="step-%d"' % number)[0][-80:], number not in (5, 10), number)
        self.assertIn(">Change</a>", page.split('id="step-2"')[1].split("</li>")[0])
        self.assertIn(">Start Sidedoor</button>", page)   # the current step: Start
        opened = self.client.get("/setup/step/2", headers={"hx-request": "true"}).text
        self.assertIn('name="key"', opened)   # [Change] opens it

    def test_s1_only_the_current_step_is_open_and_finishing_it_opens_the_next(self):
        page = self.client.get("/setup").text
        self.assertEqual(page.count('class="card step '), 10)
        self.assertEqual(page.count("step-collapsed"), 8)   # the license (current) and the mode (always) are open
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            response = self.client.post("/setup/openrouter", data={"key": OPENROUTER_KEY}, headers=HTMX).text
        resume = response.split('id="step-3"')[1].split("</li>")[0]
        self.assertIn('name="resume"', resume)   # the résumé is current now: sent open
        self.assertIn('hx-get="/setup/step/2?collapse=1"', response)   # round 1 (1), round 2 (5): it collapses after a moment

    def test_round1_a_step_open_on_the_page_is_not_sent_again(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        plain = self.client.post("/setup/mode", data={"mode": "automatic"}, headers=HTMX).text
        self.assertIn('id="step-7" hx-swap-oob="true"', plain)
        kept = self.client.post("/setup/mode", data={"mode": "automatic", "open_step": ["7", "5"]}, headers=HTMX).text
        self.assertNotIn('id="step-7"', kept)   # open on the page: its typed input stays
        page = self.client.get("/setup?open=7").text
        self.assertIn('name="open_step" value="7"', page)
        self.assertIn('hx-include=".step-open-marker"', page)

    def test_b4_im_done_closes_the_browser_and_runs_the_email_check(self):
        self.unlock_browser_step()
        page = self.client.get("/setup?open=8").text
        step = page.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertNotIn("Run check", page)
        self.assertNotIn("Save webmail", step)
        self.assertIn('hx-trigger="submit, webmail-picked"', step)   # the webmail saves when picked
        picked = self.client.post("/setup/pick/webmail/add", data={"pick_value": "Gmail"}, headers=HTMX)
        self.assertEqual(picked.headers["hx-trigger-after-swap"], "webmail-picked")
        def passed(home):
            setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-11-08T00:00:00Z",
                                                                locked_email="a@example.com"), home)
            return setup_steps.record(home, "email", "done", "Your browser is signed in to a@example.com.")
        with mock.patch.object(setup_steps, "run_email_check", side_effect=passed) as check:
            response = self.client.post("/setup/browser/done", headers=HTMX).text
        check.assert_called_once()
        self.assertEqual(self.browser.calls, ["close"])
        step = response.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertIn('<span class="chip chip-success">Done</span>', step)
        self.assertNotIn(">Check again</button>", step)

    def test_round1_a_webmail_picked_after_im_done_is_checked_at_once(self):
        self.unlock_browser_step()
        setup_steps.update_state(lambda state: state.pop("webmail"), self.home)
        self.client.post("/setup/browser/done", headers=HTMX)   # no webmail yet: nothing to check
        with mock.patch.object(setup_steps, "run_email_check",
                               side_effect=lambda home: setup_steps.record(home, "email", "failed", "Other mailbox.")) as check:
            response = self.client.post("/setup/webmail", data={"webmail": "Gmail"}, headers=HTMX).text
        check.assert_called_once()
        self.assertIn(">Check again</button>", response.split('id="step-8"')[1].split('id="step-9"')[0])

    def test_round1_before_any_email_check_the_step_says_click_check(self):
        self.unlock_browser_step()
        setup_steps.record(self.home, "browser", "done", "Sidedoor's browser is closed.")
        step = self.client.get("/setup?open=8").text.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertNotIn("Click <strong>Check</strong>.", step)   # round 3 (18): the button says it
        self.assertIn(">Check</button>", step)
        self.assertNotIn(">Check again</button>", step)

    def test_round1_step_6_chip_follows_the_chosen_engine(self):
        setup_steps.update_state(lambda state: state.update(engine="claude", claude_path=sys.executable), self.home)
        setup_steps.record(self.home, "claude", "done", "ok")
        setup_steps.record(self.home, "codex", "failed", "Codex is not installed.")
        progress = setup_steps.progress(self.home)
        step = self.client.get("/setup").text.split('id="step-6"')[1].split("</li>")[0]
        self.assertEqual(progress["done"][6], DONE in step)

    def test_b4_a_failed_email_check_offers_check_again(self):
        self.unlock_browser_step()
        failed = lambda home: setup_steps.record(home, "email", "failed", "Your browser is signed in to b@example.com.")
        with mock.patch.object(setup_steps, "run_email_check", side_effect=failed):
            response = self.client.post("/setup/browser/done", headers=HTMX).text
        step = response.split('id="step-8"')[1].split('id="step-9"')[0]
        self.assertIn("signed in to b@example.com", step)
        self.assertIn('hx-post="/setup/email"', step)
        self.assertIn(">Check again</button>", step)

    def test_b1_b3_claude_choice_saves_on_change_and_its_key_is_saved_and_tested(self):
        page = self.client.get("/setup?open=6").text
        choice = page.split('class="engine-choice"')[0].rsplit("<form", 1)[1]
        self.assertIn('hx-trigger="change"', choice)   # B1
        self.assertIn("<noscript><button", page.split('class="engine-choice"')[1].split("</form>")[0])
        with mock.patch.object(setup_steps, "check_claude",
                               side_effect=lambda home, run=None: setup_steps.record(home, "claude", "failed", "Claude is not installed.")):
            response = self.client.post("/setup/engine", data={"engine": "claude"}, headers=HTMX).text
            self.assertIn(">Check again</button>", response)
            self.client.post("/setup/claude/auth", data={"choice": "api_key"}, headers=HTMX)
        def works(home, run=None):
            setup_steps.update_state(lambda state: state.update(claude_path=sys.executable), home)
            return setup_steps.record(home, "claude", "done", "Claude is installed and uses your Anthropic API key.")
        with mock.patch.object(setup_steps, "check_claude", side_effect=works) as check:
            response = self.client.post("/setup/claude/key", data={"key": "sk-ant-fake-key"}, headers=HTMX).text
        check.assert_called_once()
        self.assertIn(DONE, response.split('id="step-6"')[1].split("</li>")[0])
        step = self.client.get("/setup?open=6").text.split('id="step-6"')[1].split('id="step-7"')[0]
        self.assertNotIn(">Check again</button>", step)
        self.assertNotIn(">Check</button>", step)
        self.assertIn(">Save and test</button>", step)
        self.assertIn('class="button-link" type="submit">Remove key</button>', step)
        self.assertNotIn("sk-ant-fake-key", response + step)

    def test_round2_is_open_never_waits_for_a_running_check(self):
        import threading, time
        class Process:
            pid = 1
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        session = setup_steps.BrowserSession(self.home)
        session.process = Process()
        release, started = threading.Event(), threading.Event()
        def slow():
            started.set()
            release.wait(10)
        worker = threading.Thread(target=session.close, kwargs={"then": slow})
        worker.start()
        started.wait(5)
        begin = time.monotonic()
        session.is_open()
        self.assertLess(time.monotonic() - begin, 1)
        release.set()
        worker.join(10)

    def test_round2_a_webmail_check_is_skipped_while_one_runs_and_busy_only_when_due(self):
        self.unlock_browser_step()
        with setup_steps._email_check_running, mock.patch.object(setup_steps, "run_email_check") as check:
            self.assertIsNone(setup_steps.run_email_check_if_still_due(self.home))
        check.assert_not_called()
        setup_steps.update_state(lambda state: state.pop("webmail"), self.home)
        with mock.patch.object(self.browser, "close", return_value=None):
            self.client.post("/setup/browser/done", headers=HTMX)
        self.assertNotIn("email", setup_steps.load_state(self.home)["steps"])   # nothing was due: not "busy"

    def test_round2_saving_the_profile_clears_a_stale_email_check_failure(self):
        setup_steps.record(self.home, "email", "failed", "Your browser is signed in to b@example.com.")
        with mock.patch.object(setup_steps, "validate_profile", return_value=({}, {})), \
                mock.patch.object(setup_steps, "profile_document", return_value={}):
            setup_steps.save_profile({}, self.home)
        self.assertNotIn("email", setup_steps.load_state(self.home)["steps"])

    def test_round2_the_linkedin_guide_names_the_button_showing(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        setup_steps.record(self.home, "linkedin", "not_started", None, offer="connect")
        step = self.client.get("/setup/step/7", headers={"hx-request": "true"}).text
        guide = step.split('id="guide-linkedin"')[1]
        # E18 join: the live view's Done where it can be shown (macOS), else [I'm signed in] after the window.
        self.assertIn("click <strong>I'm signed in</strong>." if dashboard.GUIDE_PLATFORM == "windows"
                      else "Press <strong>Done</strong>.", guide)
        self.assertNotIn("Check again", guide)

    def test_round2_antigravity_says_check_until_a_check_failed(self):
        setup_steps.update_state(lambda state: state.update(engine="antigravity"), self.home)
        self.assertIn(">Check</button>", self.client.get("/settings").text)
        setup_steps.record(self.home, "antigravity", "failed", "Antigravity is not installed.")
        self.assertIn(">Check again</button>", self.client.get("/setup?open=6").text)
        self.assertIn(">Check again</button>", self.client.get("/settings").text)

    def test_round3_a_step_already_done_or_warned_does_not_collapse_by_itself(self):
        with mock.patch.object(setup_steps.text_model, "_post", return_value=(200, OK_ANSWER)):
            again = self.client.post("/setup/openrouter", data={"key": OPENROUTER_KEY, "done_step": "2"}, headers=HTMX).text
        self.assertNotIn("step-autocollapse", again.split('id="step-2"')[1].split("</li>")[0])   # it was done before
        page = self.client.get("/setup?open=2").text
        self.assertIn('data-reveal="openrouter-key"', page)   # round 3 (9): the eye button
        self.assertIn("/static/setup.js", page)
        self.assertIn('name="done_step" value="2"', page)

    def test_round3_settings_webmail_save_refreshes_the_browser_part(self):
        response = self.client.post("/settings/webmail", data={"webmail": "Gmail"}, headers=HTMX).text
        self.assertIn('id="connection-browser" hx-swap-oob="true"', response)

    def test_l1_start_rechecks_the_license_once_and_a_failed_check_never_blocks_it(self):
        import license_clock
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        self.unlock_browser_step()
        for key in ("openrouter", "resume", "linkedin", "browser"):
            setup_steps.record(self.home, key, "done", "ok")
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-10-01T00:00:00Z"), self.home)
        started = lambda home: setup_steps.record(home, "start", "done", "Sidedoor is running.")
        with mock.patch.object(setup_steps, "login_on_continue", return_value=None), \
                mock.patch.object(setup_steps, "start_scheduler", side_effect=started):
            with mock.patch.object(license_clock, "check_now", return_value="ok") as check:
                self.client.post("/setup/start", headers=HTMX)
            check.assert_called_once_with(self.home, "https://relay.test")
            with mock.patch.object(license_clock, "check_now", side_effect=OSError("offline")):
                response = self.client.post("/setup/start", headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<span class="chip chip-success">Done</span>', response.text.split('id="step-10"')[1].split("</li>")[0])

    def unlock_start_step(self):
        license_fixtures.install(self.home, valid_until="2999-01-01T00:00:00Z")
        self.unlock_browser_step()
        for key in ("openrouter", "resume", "linkedin", "browser"):
            setup_steps.record(self.home, key, "done", "ok")
        setup_steps.update_state(lambda state: state.update(email_check_passed_at="2026-10-01T00:00:00Z"), self.home)

    def start_step(self, text):
        return text.split('id="step-10"')[1].split("</li>")[0]

    def start_patches(self):
        return (mock.patch.object(setup_steps, "login_on_continue", return_value=None),
                mock.patch.object(setup_steps, "start_scheduler", return_value=None),
                mock.patch.object(dashboard, "sync_license_pause", return_value=None),
                mock.patch("license_clock.check_now", return_value="ok"))

    def test_start_step_share_progress_box_ticked_by_default_and_saved_like_settings(self):
        import progress_share
        self.unlock_start_step()
        step = self.start_step(self.client.get("/setup?open=10").text)
        self.assertIn(progress_share.LABEL, step)
        self.assertIn('id="setup-share-progress" checked', step)          # no choice saved yet: ticked
        self.assertEqual(self.client.get("/setup").text.count('class="step-number"'), 10)   # step count unchanged
        with contextlib.ExitStack() as stack:
            for patch in self.start_patches():
                stack.enter_context(patch)
            self.client.post("/setup/start", headers=HTMX)               # the box untouched: what it showed is saved
            self.assertIs(setup_steps.load_state(self.home)[progress_share.SETTING], True)
            self.assertIn('id="s-share-progress" checked', self.client.get("/settings").text)
            self.client.post("/setup/progress", data={}, headers=HTMX)   # unticked, then Start again
            self.client.post("/setup/start", data={"share_progress": "on"}, headers=HTMX)   # a stray field changes nothing
        self.assertIs(setup_steps.load_state(self.home)[progress_share.SETTING], False)
        self.assertIn('id="s-share-progress">', self.client.get("/settings").text)
        self.assertIn('id="setup-share-progress">', self.start_step(self.client.get("/setup?open=10").text))

    def test_share_progress_box_has_its_own_form_so_enter_never_presses_start(self):
        from html.parser import HTMLParser

        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms, self.current = [], None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.current = dict(attrs, inputs=[])
                    self.forms.append(self.current)
                elif tag == "input" and self.current is not None:
                    self.current["inputs"].append(attrs)
                elif tag == "button" and self.current is not None:
                    self.current.setdefault("buttons", []).append(attrs)

            def handle_endtag(self, tag):
                if tag == "form":
                    self.current = None

        self.unlock_start_step()
        parser = Forms()
        parser.feed(self.start_step(self.client.get("/setup?open=10").text))
        by_action = {form["action"]: form for form in parser.forms}
        box = by_action["/setup/progress"]
        self.assertEqual([field["id"] for field in box["inputs"]], ["setup-share-progress"])
        # M4: Enter on the box does nothing (settings.js, loaded by Setup, skips boxes marked data-enter-inert);
        # M3: the one line under it comes from progress_share.HELP and describes the box.
        import progress_share
        page = self.client.get("/setup?open=10").text
        self.assertIn("data-enter-inert", box["inputs"][0])
        self.assertEqual(box["inputs"][0]["aria-describedby"], "setup-share-progress-help")
        self.assertIn('<p class="muted choice-help" id="setup-share-progress-help">%s</p>' % progress_share.HELP,
                      self.start_step(page))
        self.assertIn("/static/settings.js", page)
        script = self.client.get("/static/settings.js").text
        self.assertIn('event.target.matches("input[type=checkbox][data-enter-inert]")) event.preventDefault()', script)
        self.assertIn("<noscript><button", self.start_step(page).split('action="/setup/progress"')[1].split("</form>")[0])
        self.assertEqual((box["hx-post"], box["hx-trigger"]), ("/setup/progress", "change"))
        self.assertEqual(by_action["/setup/start"]["inputs"], [])     # Start carries no field of its own
        self.assertEqual(by_action["/setup/start"]["buttons"][0]["type"], "submit")

    def test_start_step_share_progress_box_shows_a_saved_choice_and_saves_on_change(self):
        import progress_share
        self.unlock_start_step()
        progress_share.set_enabled(False, self.home)
        self.assertIn('id="setup-share-progress">', self.start_step(self.client.get("/setup?open=10").text))
        response = self.client.post("/setup/progress", data={"share_progress": "on"}, headers=HTMX)
        self.assertIn('id="setup-share-progress" checked', self.start_step(response.text))
        self.assertTrue(progress_share.enabled(self.home))
        self.client.post("/setup/progress", data={}, headers=HTMX)
        self.assertFalse(progress_share.enabled(self.home))

    def test_finished_install_without_a_saved_choice_shows_the_box_off_everywhere(self):
        import progress_share
        self.unlock_start_step()
        setup_steps.record(self.home, "start", "done", "Sidedoor is running.")
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-21T00:00:00Z"), self.home)
        self.assertNotIn(progress_share.SETTING, setup_steps.load_state(self.home))
        self.assertFalse(progress_share.shown(self.home))
        self.assertIn('id="setup-share-progress">', self.start_step(self.client.get("/setup?open=10").text))
        self.assertIn('id="s-share-progress">', self.client.get("/settings").text)
        self.assertEqual(progress_share.heartbeat_fields(self.home), {"milestones_opt_out": True})
        # Review 2026-11-10 (4): Start pressed again on a finished install writes no choice.
        with contextlib.ExitStack() as stack:
            for patch in self.start_patches():
                stack.enter_context(patch)
            self.client.post("/setup/start", headers=HTMX)
        self.assertNotIn(progress_share.SETTING, setup_steps.load_state(self.home))
        self.assertEqual(progress_share.heartbeat_fields(self.home), {"milestones_opt_out": True})

    def test_setup_file_that_cannot_be_read_shows_the_box_off_and_start_leaves_the_file(self):
        # Review round 4 (Low): unreadable is not "no choice yet"; the box shows off, nothing is repaired.
        import progress_share
        self.unlock_start_step()
        path = self.home / setup_steps.STATE_FILE
        path.write_bytes(b"{not json")
        self.assertFalse(progress_share.shown(self.home))
        self.assertEqual(progress_share.heartbeat_fields(self.home), {"milestones_opt_out": True})
        page = self.client.get("/setup?open=10").text   # no steps can be read, so Start waits: no box, never a ticked one
        self.assertNotIn('id="setup-share-progress" checked', page)
        self.assertNotIn("checked", self.start_step(page))
        self.assertIn('id="s-share-progress">', self.client.get("/settings").text)
        with contextlib.ExitStack() as stack:
            for patch in self.start_patches():
                stack.enter_context(patch)
            self.client.post("/setup/start", headers=HTMX)
            self.client.post("/setup/progress", data={"share_progress": "on"}, headers=HTMX)
        self.assertEqual(path.read_bytes(), b"{not json")

    def test_start_saves_the_shown_choice_in_one_write_so_an_untick_in_between_is_kept(self):
        # Review 2026-11-10 (1): the box's change POST lands between Start's read (no choice yet)
        # and its write; the save happens inside the state lock, so the untick is what stays.
        import progress_share
        self.unlock_start_step()
        self.assertTrue(setup_steps.progress(self.home)["finished"])   # latched now, so Start's save is the first write
        real = setup_steps.update_state
        calls = []

        def untick_first(change, home=None):
            if not calls:
                calls.append(change)
                self.client.post("/setup/progress", data={}, headers=HTMX)   # the untick, with the real writer
            return real(change, home)

        with contextlib.ExitStack() as stack:
            for patch in self.start_patches():
                stack.enter_context(patch)
            with mock.patch.object(setup_steps, "update_state", side_effect=untick_first):
                self.client.post("/setup/start", headers=HTMX)
        self.assertEqual(len(calls), 1)
        self.assertIs(setup_steps.load_state(self.home)[progress_share.SETTING], False)
        self.assertIn('id="setup-share-progress">', self.start_step(self.client.get("/setup?open=10").text))

    def test_locked_start_never_saves_the_share_progress_choice(self):
        self.client.post("/setup/start", data={"share_progress": "on"}, headers=HTMX)
        self.assertNotIn("share_progress", setup_steps.load_state(self.home))

    def test_locked_start_refuses_the_share_progress_box_too(self):
        # Review 2026-11-10 (5): POST /setup/progress while Start is locked saves nothing, like Start.
        response = self.client.post("/setup/progress", data={"share_progress": "on"}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("share_progress", setup_steps.load_state(self.home))
        response = self.client.post("/setup/progress", data={}, headers=HTMX)
        self.assertNotIn("share_progress", setup_steps.load_state(self.home))


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
