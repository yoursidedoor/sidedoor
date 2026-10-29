"""`sidedoor doctor` (E4.T4): a healthy home, a broken home, --json, --online with fakes, no secret in the
output, and the dashboard's Run checks page. No test makes a network call (urlopen is refused)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import credential_fixtures
import credentials
import doctor
import license_fixtures
import setup_steps
import user_profile
import test_dashboard_overview as base

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
REPO = Path(__file__).resolve().parent.parent
LICENSE = "SD-FAKELICENSE0123456789TLUS"
OPENROUTER = "sk-or-v1-fakefakefake0123456789abcdefabcdef9z9z"
TOKEN = "dashboard-token-fake-0123456789-abcdefghij"
SIGNED_IN = mock.Mock(return_value=mock.Mock(returncode=0, stdout="Logged in using ChatGPT", stderr=""))
REAL_RUN = doctor.run
CONFIG = {"timezone": "America/Los_Angeles", "text_model": {"model": "test/model"}}
OK_ANSWER = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}}).encode()
PLENTY = lambda path: mock.Mock(free=100 << 30)


def no_network(*arguments, **keywords):
    raise AssertionError("doctor made a network call")


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "root"
        self.home = Path(self.directory.name) / "home"
        self.root.mkdir()
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        for patch in (mock.patch("urllib.request.urlopen", no_network),
                      mock.patch("http.client.HTTPConnection.connect", no_network),
                      license_fixtures.use_test_key(),
                      mock.patch.object(setup_steps, "chrome_path", lambda *a, **k: "/Applications/Chrome")):
            patch.start()
            self.addCleanup(patch.stop)

    def healthy(self):
        profile = json.loads(user_profile.EXAMPLE_PROFILE.read_text())
        for name in ("resume", "resume_raw"):
            profile[name]["path"] = str(REPO / profile[name]["path"])
        profile["resume_raw"]["text_path"] = str(REPO / profile["resume_raw"]["text_path"])
        (self.home / "profile.json").write_text(json.dumps(profile))
        (self.home / "openrouter.key").write_text(OPENROUTER + "\n")
        (self.home / "openrouter.key.tested").write_text(json.dumps(
            {"key_sha256": hashlib.sha256(OPENROUTER.encode()).hexdigest(), "tested_at": "2026-10-22T00:00:00Z"}))
        (self.home / "dashboard-token").write_text(TOKEN + "\n")
        codex = self.home / "codex"
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)
        (self.home / "setup-state.json").write_text(json.dumps({"codex_path": str(codex), "steps": {
            "linkedin": {"status": "done", "message": "LinkedIn connected: Alex Example.", "at": "2026-10-22T00:00:00Z"}}}))
        license_fixtures.install(self.home, key=LICENSE)
        base.database_with_schema(self.root).close()

    def run_doctor(self, **extra):
        keywords = dict(home=self.home, root=self.root, config=CONFIG, running=True,
                        codex_run=SIGNED_IN, browser_run=lambda *a, **k: mock.Mock(returncode=1, stdout=""),
                        disk_usage=PLENTY)
        keywords.update(extra)
        return REAL_RUN(**keywords)

    def by_name(self, lines):
        return {line["name"]: line for line in lines}


class HealthyHomeTest(DoctorBase):
    def test_every_check_passes(self):
        store = credential_fixtures.use_fake(self)   # E2.T4: the keys are read from the store
        self.healthy()
        result = credentials.migrate(self.home)
        self.assertEqual((result["openrouter"], result["license"]), ("moved", "moved"))
        self.assertEqual(store.values(), sorted([LICENSE, OPENROUTER]))
        with mock.patch.object(setup_steps, "validate_profile", lambda form, state: ({}, {})):
            lines = self.run_doctor()
        names = [line["name"] for line in lines]
        self.assertEqual(names, ["Python and Sidedoor", "Home folder", "Setup file", "Profile", "Résumé", "Credentials", "OpenRouter",
                                 "Codex", "Google Chrome", "Browser runtime", "Secure connection", "License", "LinkedIn",
                                 "Database", "Recent failures", "Model cost", "Scheduler", "Tray icon", "PDF writer", "App location",
                                 "Disk space", "Computer"])
        self.assertEqual({line["name"]: line["status"] for line in lines if line["status"] != "OK"}, {})
        found = self.by_name(lines)
        self.assertIn("SD-...TLUS", found["License"]["line"])
        self.assertIn("...9z9z", found["OpenRouter"]["line"])
        text = doctor.report_text(lines)
        self.assertIn("OK   — Codex — Installed and signed in with ChatGPT.", text)
        self.assertFalse(list(self.home.glob(".doctor-*")))   # the write probe is removed

    def test_online_runs_each_network_check_once_through_fakes(self):
        self.healthy()
        relay = license_fixtures.FakeRelay(lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        gets = []
        tested = mock.Mock(return_value=(200, OK_ANSWER))
        linkedin = mock.Mock(return_value=(200, {"items": [{"name": "Alex Example", "sources": [{"status": "OK"}]}]}))
        before = (self.home / "setup-state.json").read_text()
        with mock.patch.object(setup_steps, "validate_profile", lambda form, state: ({}, {})):
            lines = self.run_doctor(online=True, relay=lambda url: gets.append(url) or 200, license_post=relay,
                                    linkedin_transport=linkedin, openrouter_transport=tested)
        self.assertEqual((self.home / "setup-state.json").read_text(), before)   # setup progress untouched
        found = self.by_name(lines)
        self.assertEqual(gets, [setup_steps.RELAY_URL + "/healthz"])
        self.assertEqual(len(relay.calls), 1)
        self.assertTrue(relay.calls[0][0].endswith("/license/heartbeat"))
        tested.assert_called_once()
        linkedin.assert_called_once()
        for name in ("Relay", "License", "OpenRouter", "LinkedIn"):
            self.assertEqual(found[name]["status"], "OK", found[name])
        self.assertIn("Checked just now", found["License"]["line"])


class OnlineIsReadOnlyTest(DoctorBase):
    """UD2: --online never changes setup progress; a failure never overwrites "done"."""

    def test_good_key_and_network_blip_leaves_step_done_and_marker(self):
        self.healthy()
        setup_steps.record(self.home, "openrouter", "done", "Key works.")
        marker = (self.home / "openrouter.key.tested").read_text()
        before = (self.home / "setup-state.json").read_text()
        blip = mock.Mock(side_effect=TimeoutError("blip"))
        lines = self.run_doctor(online=True, relay=lambda url: 200, openrouter_transport=blip,
                                license_post=license_fixtures.FakeRelay(lambda: __import__("datetime").datetime.now(
                                    __import__("datetime").timezone.utc)),
                                linkedin_transport=mock.Mock(side_effect=OSError("offline")))
        found = self.by_name(lines)
        self.assertEqual(found["OpenRouter"]["status"], "WARN")
        self.assertEqual((self.home / "openrouter.key.tested").read_text(), marker)
        self.assertTrue(setup_steps.openrouter_tested(self.home))
        self.assertEqual((self.home / "setup-state.json").read_text(), before)
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["openrouter"]["status"], "done")

    def test_linkedin_done_and_offline_stays_done(self):
        self.healthy()
        lines = self.run_doctor(online=True, relay=lambda url: 200, openrouter_transport=mock.Mock(return_value=(200, OK_ANSWER)),
                                license_post=mock.Mock(side_effect=OSError("offline")),
                                linkedin_transport=mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(self.by_name(lines)["LinkedIn"]["status"], "OK")   # the saved step is reported
        self.assertEqual(setup_steps.load_state(self.home)["steps"]["linkedin"]["status"], "done")

    def test_refused_relay_read_records_nothing(self):
        self.healthy()
        before = (self.home / "setup-state.json").read_text()
        lines = self.run_doctor(online=True, relay=lambda url: 200, openrouter_transport=mock.Mock(return_value=(200, OK_ANSWER)),
                                license_post=mock.Mock(side_effect=OSError("offline")),
                                linkedin_transport=mock.Mock(return_value=(200, {"items": []})))
        self.assertEqual(self.by_name(lines)["LinkedIn"]["status"], "FAIL")
        self.assertEqual((self.home / "setup-state.json").read_text(), before)
        self.assertFalse((self.home / "license-check-request").exists())


class SchedulerAndPathsTest(DoctorBase):
    def test_closed_app_is_warn_not_fail(self):
        line = doctor.check_scheduler(False, {})
        self.assertEqual((line["status"], line["line"], line["todo"]), ("WARN", "Sidedoor isn't running.", "Open it and click Start."))

    def test_cli_asks_the_dashboard_for_the_loop_state(self):
        (self.home / "dashboard-token").write_text(TOKEN + "\n")
        import dashboard
        with mock.patch.object(dashboard, "scheduler_there", return_value="stopped") as there:
            self.assertFalse(doctor.cli_running(self.home))
        there.assert_called_once_with(TOKEN)
        with mock.patch.object(dashboard, "scheduler_there", return_value="running"):
            self.assertTrue(doctor.cli_running(self.home))
        with mock.patch.object(dashboard, "scheduler_there", return_value=None):
            self.assertFalse(doctor.cli_running(self.home))

    def test_home_is_shown_under_tilde(self):
        with mock.patch.object(Path, "home", return_value=self.home.parent):
            line = doctor.check_home(self.home)
        self.assertIn("~/home", line["line"])
        self.assertNotIn(str(self.home.parent), line["line"])

    def test_a_windows_home_outside_the_user_folder_is_a_warning(self):   # WR5
        user = r"C:\Users\alex"
        for home, outside in ((r"C:\Sidedoor", True), (r"D:\Data\Sidedoor", True), (r"C:\Users\alexander\sd", True),
                              (r"C:\Users\alex\.config\sidedoor", False), (r"c:\users\ALEX\Side Door", False)):
            with self.subTest(home=home):
                self.assertEqual(doctor.outside_user_folder(home, "win32", user), outside)
        self.assertFalse(doctor.outside_user_folder(r"C:\Sidedoor", "darwin", user), "Windows only")
        with mock.patch.object(doctor, "outside_user_folder", return_value=True):
            line = doctor.check_home(self.home)
        self.assertEqual(line["status"], doctor.WARN)
        self.assertIn("outside your user folder", line["line"])
        self.assertIn("only you can open", line["todo"])
        self.assertEqual(doctor.check_home(self.home)["status"], doctor.OK, "a home inside the user folder")

    def test_profile_and_resume_use_the_home_passed_in(self):
        self.healthy()
        with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.root / "elsewhere")}), \
                mock.patch.object(setup_steps, "validate_profile", lambda form, state: ({}, {})):
            self.assertEqual(doctor.check_profile(self.home)["status"], "OK")
            self.assertEqual(doctor.check_resume(self.home, self.root)["status"], "OK")
            self.assertEqual(os.environ[user_profile.HOME_VARIABLE], str(self.root / "elsewhere"))

    def test_help_lists_doctor(self):
        import orchestrator
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            orchestrator.main(["--help"])
        self.assertIn("doctor", output.getvalue())


class BrokenHomeTest(DoctorBase):
    def test_missing_key_unreadable_state_no_license(self):
        (self.home / "setup-state.json").write_text("{ not json")
        lines = self.run_doctor(running=False)
        found = self.by_name(lines)
        for name in ("Setup file", "OpenRouter", "License", "Profile", "Résumé"):
            self.assertEqual(found[name]["status"], "FAIL", found[name])
        self.assertEqual(found["Scheduler"]["status"], "WARN")
        self.assertIn("Reset setup file", found["Setup file"]["todo"])
        self.assertIn("No OpenRouter key", found["OpenRouter"]["line"])
        self.assertIn("No license key", found["License"]["line"])
        self.assertEqual(found["Database"]["status"], "WARN")   # no records yet
        # The unreadable setup file is left as it was.
        self.assertEqual((self.home / "setup-state.json").read_text(), "{ not json")

    def test_a_check_that_raises_is_a_fail_line_and_the_rest_still_run(self):
        with mock.patch.object(doctor, "check_codex", side_effect=RuntimeError("boom")):
            lines = self.run_doctor()
        found = self.by_name(lines)
        self.assertEqual(found["Codex"]["status"], "FAIL")
        self.assertIn("RuntimeError", found["Codex"]["line"])
        self.assertIn("Computer", found)

    def test_untested_key_and_runtime_missing_and_low_disk(self):
        (self.home / "openrouter.key").write_text(OPENROUTER + "\n")
        lines = self.run_doctor(config={"browser_driver": {"runtime_dir": str(self.home / "nothing")}},
                                disk_usage=lambda path: mock.Mock(free=1 << 20))
        found = self.by_name(lines)
        self.assertEqual(found["OpenRouter"]["status"], "WARN")
        self.assertEqual(found["Browser runtime"]["status"], "FAIL")
        self.assertEqual(found["Disk space"]["status"], "FAIL")


class JsonAndCliTest(DoctorBase):
    def test_json_output(self):
        self.healthy()
        with mock.patch.object(doctor, "run", lambda online=False: self.run_doctor(online=online)), \
                mock.patch.object(setup_steps, "validate_profile", lambda form, state: ({}, {})):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = doctor.main(["--json"])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertEqual(set(value["checks"][0]), {"status", "name", "line", "todo"})
        self.assertIn("License", [check["name"] for check in value["checks"]])

    def test_exit_code_is_1_on_a_fail(self):
        output = io.StringIO()
        with mock.patch.object(doctor, "run", lambda online=False: self.run_doctor()), contextlib.redirect_stdout(output):
            self.assertEqual(doctor.main([]), 1)
        self.assertTrue(output.getvalue().startswith("Sidedoor doctor, "))

    def test_orchestrator_dispatches_doctor(self):
        import orchestrator
        with mock.patch.object(doctor, "main", return_value=0) as main:
            self.assertEqual(orchestrator.main(["doctor", "--online", "--json"]), 0)
        main.assert_called_once_with(["--online", "--json"])


class NoSecretsTest(DoctorBase):
    def test_planted_secrets_never_appear(self):
        self.healthy()
        # A stored message that carries a key (as a provider error might) is redacted too.
        state = json.loads((self.home / "setup-state.json").read_text())
        state["steps"]["linkedin"] = {"status": "failed", "message": "refused key " + OPENROUTER + " and " + TOKEN}
        (self.home / "setup-state.json").write_text(json.dumps(state))
        (self.home / "unipile.json").write_text(json.dumps({"dsn": "api99.unipile.com:14999", "token": "unipile-fake-token-0123456789"}))
        with mock.patch.object(setup_steps, "validate_profile", lambda form, state: ({}, {})):
            lines = self.run_doctor()
        outputs = doctor.report_text(lines) + doctor.report_json(lines)
        for secret in (LICENSE, OPENROUTER, TOKEN, LICENSE[3:-4], OPENROUTER[:-4], TOKEN[:12], "unipile-fake-token"):
            self.assertNotIn(secret, outputs)
        self.assertIn("SD-...TLUS", outputs)


@unittest.skipUnless(DEPENDENCIES, "the dashboard packages (fastapi, httpx) are not installed")
class RunChecksPageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        base.finished_home(self.home)
        (self.home / "openrouter.key").write_text(OPENROUTER + "\n")
        for patch in (mock.patch("urllib.request.urlopen", no_network), mock.patch.object(setup_steps, "codex_login_status",
                                                                                           lambda *a, **k: "SIGNED_IN")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_failures_page_links_run_checks_and_the_report_has_copy(self):
        from fastapi.testclient import TestClient
        import dashboard
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        client.get("/", params={"t": base.SECRET})
        page = client.get("/failures")
        self.assertIn('href="/failures/doctor"', page.text)
        report = client.get("/failures/doctor")
        self.assertEqual(report.status_code, 200)
        self.assertIn("Copy report", report.text)
        self.assertIn("Sidedoor doctor, ", report.text)
        self.assertIn("OpenRouter", report.text)
        self.assertNotIn(OPENROUTER, report.text)
        self.assertNotIn(base.SECRET, report.text)

    def test_scheduler_endpoint_answers_the_loops_state_signed(self):
        from fastapi.testclient import TestClient
        import dashboard
        import scheduler_loop
        app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
        client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
        nonce = "a" * 32
        for running, state in ((False, "stopped"), (True, "running")):
            with mock.patch.object(scheduler_loop, "running", return_value=running):
                answer = client.get(dashboard.SCHEDULER_PATH, params={"n": nonce})   # no cookie needed
            self.assertEqual(answer.text, dashboard.scheduler_answer(base.SECRET, nonce, state))
            self.assertNotIn(base.SECRET, answer.text)


if __name__ == "__main__":
    unittest.main()


def answered(stdout, returncode=0):
    return mock.Mock(return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=""))


class ChosenEngineTest(DoctorBase):
    """C1: doctor checks the engine chosen in Setup, with Setup's own probes, and no other engine."""

    def choose(self, engine, **saved):
        self.healthy()
        program = self.home / engine
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        state = json.loads((self.home / "setup-state.json").read_text())
        state.pop("codex_path")
        state.update(engine=engine, **{engine + "_path": str(program)}, **saved)
        (self.home / "setup-state.json").write_text(json.dumps(state))
        return program

    def engine_lines(self, run):
        with mock.patch.object(setup_steps, "find_codex", side_effect=AssertionError("Codex was checked")):
            lines = self.run_doctor(codex_run=run)
        return [line for line in lines if line["name"] in ("Codex", "Claude", "Antigravity")]

    def test_claude_signed_in(self):
        program = self.choose("claude")
        run = answered(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertEqual(self.engine_lines(run), [{"status": "OK", "name": "Claude", "line": "Installed and signed in.",
                                                   "todo": ""}])
        self.assertEqual(run.call_args[0][0], [str(program), "auth", "status"])

    def test_claude_signed_out_and_missing(self):
        self.choose("claude")
        [line] = self.engine_lines(answered(json.dumps({"loggedIn": False})))
        self.assertEqual((line["status"], line["line"]), ("FAIL", "Claude is installed but not signed in."))
        with mock.patch.object(setup_steps, "find_claude", return_value=None):
            [line] = self.engine_lines(answered(""))
        self.assertEqual((line["name"], line["line"]), ("Claude", "Claude is not installed."))

    def test_claude_with_an_api_key_runs_nothing(self):
        credential_fixtures.use_fake(self)
        self.choose("claude", claude_auth="api_key")
        credentials.put("anthropic", "sk-ant-fake-0123456789abcdWXYZ", self.home)
        run = answered("")
        [line] = self.engine_lines(run)
        self.assertEqual((line["status"], line["line"]), ("OK", "Installed; uses Anthropic API key ...WXYZ."))
        run.assert_not_called()

    def test_antigravity(self):
        program = self.choose("antigravity")
        run = answered("claude-sonnet\tClaude Sonnet\n")
        self.assertEqual(self.engine_lines(run), [{"status": "OK", "name": "Antigravity",
                                                   "line": "Installed and signed in.", "todo": ""}])
        self.assertEqual(run.call_args[0][0], [str(program), "models"])
        [line] = self.engine_lines(answered("Open https://accounts.google.com/o/oauth2 to sign in"))
        self.assertEqual((line["status"], line["line"]), ("FAIL", "Antigravity is installed but not signed in."))


class AppLocationRedactionTest(DoctorBase):
    def test_the_app_path_is_shown_and_web_addresses_are_still_hidden(self):
        # C2/D1: Sidedoor's own program path stays readable on the App location line; the URL rule itself
        # is unchanged, so a web address elsewhere on that line, or on any other line, is still hidden.
        import scheduler_loop
        program = "/Applications/Sidedoor.app/Contents/MacOS/sidedoor"
        lines = [doctor._line(doctor.OK, "App location", "Fine for start at login (Sidedoor runs from %s)." % program,
                              "See openrouter.ai/settings/keys?k=abc"),
                 doctor._line(doctor.OK, "Computer", "Ran %s." % program)]
        with mock.patch("failures_page.secrets_of", return_value=[TOKEN]), \
                mock.patch.object(scheduler_loop, "program", return_value=program):
            app, other = doctor.redacted(lines, self.home, {})
        self.assertEqual(app["line"], "Fine for start at login (Sidedoor runs from %s)." % program)
        self.assertEqual(app["todo"], "See <url>")
        self.assertNotIn(program, other["line"])


class ExampleProfileTest(DoctorBase):
    def test_the_example_profile_is_complete(self):
        # C5: profile.example.json carries every field Setup's profile form needs.
        shutil.copy(user_profile.EXAMPLE_PROFILE, self.home / "profile.json")
        self.assertEqual(doctor.check_profile(self.home)["status"], "OK", doctor.check_profile(self.home))
