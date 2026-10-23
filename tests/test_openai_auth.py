"""E0.T8: Codex signed in with an OpenAI API key, next to Sign in with ChatGPT (the default).

ChatGPT mode must stay exactly as before: every Codex command and environment is returned
untouched. API-key mode is exercised with fakes only: no real `codex`, no OpenAI call.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_worker
import doctor
import openai_auth
import orchestrator
import outreach_history_browser
import provider_limits
import reply_worker
import research_recovery
import setup_steps
import user_profile


KEY = "sk-proj-test-never-shown-1234"
CONFIG = {"codex": "/mock-only/codex", "model": "gpt-test-astra", "browser_read_model": "gpt-test-sol"}
MODELS = json.dumps({"data": [{"id": "gpt-test-astra"}, {"id": "gpt-test-sol"}, {"id": "other"}]}).encode()


class Transport:
    """openai_auth.test_key's network, faked: answers per (method, path)."""

    def __init__(self, models=(200, MODELS), responses=(200, b'{"output": []}')):
        self.answers = {"GET /models": models, "POST /responses": responses}
        self.calls = []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append((method, url, json.loads(data) if data else None, headers["Authorization"]))
        answer = self.answers[method + " " + url[len(openai_auth.API_URL):]]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Home(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        patch = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def api_mode(self, key=KEY):
        setup_steps.update_state(lambda state: state.update(codex_auth="api_key"), self.home)
        if key:
            openai_auth.save_key(key, self.home)

    def commands(self, directory):
        directory = Path(directory)
        return {
            "agent_worker": agent_worker._command(CONFIG, directory),
            "reply_worker": reply_worker._command(CONFIG, directory / "schema.json", directory / "out.json"),
            "browser": outreach_history_browser.command(CONFIG, directory),
        }


class ChatGPTModeUnchanged(Home):
    def test_every_command_is_returned_untouched_and_keeps_the_forced_login(self):
        with tempfile.TemporaryDirectory() as directory:
            built = self.commands(directory)
            with mock.patch.object(openai_auth, "command", side_effect=lambda arguments, home=None: arguments):
                before = self.commands(directory)
        self.assertEqual(built, before)
        for name, arguments in built.items():
            self.assertIn(openai_auth.FORCED_LOGIN, arguments, name)
            self.assertEqual(arguments[arguments.index(openai_auth.FORCED_LOGIN) - 1], "-c", name)
        arguments = ["codex", "exec", "-c", openai_auth.FORCED_LOGIN]
        self.assertIs(openai_auth.command(arguments), arguments)

    def test_environment_is_returned_untouched(self):
        variables = {"HOME": "/h", "PATH": "/p"}
        self.assertIs(openai_auth.environment(["codex"], variables), variables)

    def test_research_still_strips_the_shell_keys(self):
        captured = {}
        def popen(command, **options):
            captured.update(options["env"])
            raise RuntimeError("stop before codex")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "shell", "CODEX_API_KEY": "shell"}), \
                mock.patch.object(orchestrator.subprocess, "Popen", side_effect=popen), \
                tempfile.TemporaryDirectory() as directory, self.assertRaises(RuntimeError):
            orchestrator.run_research_worker(Path(directory), ["codex"], "", Path(directory) / "log", 5)
        self.assertNotIn("OPENAI_API_KEY", captured)
        self.assertNotIn("CODEX_API_KEY", captured)

    def test_api_billing_lines_are_not_waits_with_chatgpt(self):
        line = "ERROR: unexpected status 429 Too Many Requests: insufficient_quota\n"
        self.assertIsNone(provider_limits.check_output(1, line, command=["codex", "exec"]))
        plan = "ERROR: You've hit your usage limit. Upgrade to Pro, or try again at Sep 24th, 2026 3:23 PM.\n"
        limit = provider_limits.check_output(1, plan)
        self.assertIsNone(limit.kind)
        self.assertNotIn("kind", limit.detail())
        self.assertFalse((self.home / openai_auth.WAIT_FILE).exists())

    def test_doctor_and_banner_are_unchanged(self):
        self.assertIsNone(openai_auth.banner(self.home))
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "codex"
            program.write_text("#!/bin/sh\n")
            program.chmod(0o755)
            setup_steps.update_state(lambda state: state.update(codex_path=str(program)), self.home)
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in using ChatGPT\n", ""))
            line = doctor.check_codex(self.home, run)
        self.assertEqual(line["status"], doctor.OK)
        self.assertEqual(line["line"], "Installed and signed in with ChatGPT.")


class ApiKeyMode(Home):
    def test_every_command_drops_the_forced_login_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            chatgpt = self.commands(directory)
            self.api_mode()
            api = self.commands(directory)
        for name in chatgpt:
            self.assertNotIn(openai_auth.FORCED_LOGIN, api[name], name)
            index = chatgpt[name].index(openai_auth.FORCED_LOGIN)
            self.assertEqual(api[name], chatgpt[name][:index - 1] + chatgpt[name][index + 1:], name)
            self.assertFalse(any(argument.startswith("forced_login_method") for argument in api[name]), name)

    def test_research_command_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = orchestrator.research_command(Path(directory), CONFIG, Path(directory) / "o", "research-schema.json")
            self.assertEqual(openai_auth.command(plain), plain)
            self.api_mode()
            api = openai_auth.command(plain)
        self.assertEqual(api, plain)   # it never forced a login: nothing to drop, nothing added

    def test_openrouter_commands_are_left_alone(self):
        self.api_mode()
        arguments = ["codex", "exec", "-c", openai_auth.OPENROUTER_PROVIDER]
        self.assertIs(openai_auth.command(arguments), arguments)

    def test_mcp_servers_never_receive_the_key(self):
        # Codex starts MCP servers with a filtered environment plus each server's own env; no server
        # config here passes CODEX_API_KEY or asks for inherited variables (env_vars).
        import browser_runtime
        import reply_tools
        with tempfile.TemporaryDirectory() as directory:
            options = reply_tools.options(Path(directory)) + browser_runtime.codex_options(
                {}, Path(directory), "inspect", human_check="/h.json")
        text = "\n".join(options)
        self.assertNotIn(openai_auth.ENV_VARIABLE, text)
        self.assertNotIn("env_vars", text)

    def test_the_saved_key_goes_in_as_codex_api_key(self):
        self.api_mode()
        variables = {"HOME": "/h"}
        self.assertEqual(openai_auth.environment(["codex"], variables), {"HOME": "/h", "CODEX_API_KEY": KEY})
        self.assertEqual(variables, {"HOME": "/h"})
        openrouter = ["codex", "-c", openai_auth.OPENROUTER_PROVIDER]
        self.assertIs(openai_auth.environment(openrouter, variables), variables)

    def test_missing_key_is_an_os_error_before_codex(self):
        self.api_mode(key=None)
        with self.assertRaises(OSError):
            openai_auth.environment(["codex"], {})

    def test_research_gets_the_saved_key_not_the_shells(self):
        self.api_mode()
        captured = {}
        def popen(command, **options):
            captured.update(options["env"])
            raise RuntimeError("stop before codex")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "shell", "CODEX_API_KEY": "shell"}), \
                mock.patch.object(orchestrator.subprocess, "Popen", side_effect=popen), \
                tempfile.TemporaryDirectory() as directory, self.assertRaises(RuntimeError):
            orchestrator.run_research_worker(Path(directory), ["codex"], "", Path(directory) / "log", 5)
        self.assertNotIn("OPENAI_API_KEY", captured)
        self.assertEqual(captured["CODEX_API_KEY"], KEY)

    def test_research_recovery_drops_the_forced_login(self):
        seen = []
        def executor(working, command, prompt, log, timeout):
            seen.append([argument.replace(str(working), "<dir>") for argument in command])
            raise RuntimeError("stop")
        with tempfile.TemporaryDirectory() as root:
            for api in (False, True):
                if api:
                    self.api_mode()
                artifacts = research_recovery.create_artifacts(root)
                with self.assertRaises(RuntimeError):
                    research_recovery.bounded_decision(root, CONFIG, "prompt", artifacts, 5, runtime=orchestrator,
                                                       executor=executor)
        chatgpt, api = seen
        self.assertIn(openai_auth.FORCED_LOGIN, chatgpt)
        index = chatgpt.index(openai_auth.FORCED_LOGIN)
        self.assertEqual(api, chatgpt[:index - 1] + chatgpt[index + 1:])


OPENAI_COMMAND = ["codex", "exec", "-c", 'model_provider="openai"']
OPENROUTER_COMMAND = ["codex", "exec", "-c", openai_auth.OPENROUTER_PROVIDER]


class BillingWaits(Home):
    CASES = {
        openai_auth.OUT_OF_CREDIT: 'ERROR: Quota exceeded. Check your plan and billing details. key sk-proj-abcdef123456',
        openai_auth.RATE_LIMIT: "ERROR: exceeded retry limit, last status: 429 Too Many Requests",
        openai_auth.KEY_REFUSED: 'ERROR: unexpected status 401 Unauthorized: {"code":"invalid_api_key"}',
    }
    NOW = dt.datetime(2026, 10, 22, 12, tzinfo=dt.timezone.utc)

    def check(self, text, command=OPENAI_COMMAND, now=None):
        return provider_limits.check_output(1, text, now=now or self.NOW, command=command)

    def test_credit_and_rate_limit_are_the_plan_limits_wait(self):
        self.api_mode()
        for kind in (openai_auth.OUT_OF_CREDIT, openai_auth.RATE_LIMIT):
            line = self.CASES[kind]
            with self.subTest(kind=kind):
                for limit in (self.check("WARN: retrying\n" + line + "\n"),
                              self.check(json.dumps({"type": "turn.failed", "error": {"message": line[7:]}}))):
                    self.assertEqual((limit.code, limit.kind), (provider_limits.CODE, kind))
                    detail = limit.detail()
                    self.assertEqual((detail["counts_as_attempt"], detail["counts_as_failure"], detail["kind"]),
                                     (False, False, kind))
                    self.assertEqual(limit.resume_at, self.NOW + dt.timedelta(seconds=provider_limits.BACKOFF_SECONDS))
                    self.assertNotIn("sk-proj-abcdef", limit.message)
                    error = RuntimeError("EXECUTOR_FAILED")
                    error.__cause__ = limit
                    self.assertIs(provider_limits.wait_of(error), limit)   # every caller's wait path
                self.assertIsNone(provider_limits.check_output(0, line, command=OPENAI_COMMAND))
                self.assertEqual(openai_auth.banner(self.home, self.NOW)["text"], openai_auth.WORDS[kind])
                self.assertIsNone(openai_auth.hold(self.home))
                openai_auth.clear_wait(self.home)

    def test_a_refused_key_holds_every_job_until_a_key_passes(self):
        self.api_mode()
        limit = self.check(self.CASES[openai_auth.KEY_REFUSED])
        self.assertEqual(limit.kind, openai_auth.KEY_REFUSED)
        self.assertFalse(limit.detail()["counts_as_attempt"])
        self.assertEqual(openai_auth.hold(self.home)["kind"], openai_auth.KEY_REFUSED)
        banner = openai_auth.banner(self.home)
        self.assertEqual(banner["action_label"], "Replace key")
        import scheduler_loop
        spawned = []
        loop = scheduler_loop.Loop(root=self.home, spawner=spawned.append, is_paused=lambda: False, config={})
        result = loop.tick()
        self.assertEqual((result["paused"], spawned), (True, []))
        self.assertEqual(result["held"], "openai_api_key:" + openai_auth.KEY_REFUSED)
        openai_auth.succeeded(OPENAI_COMMAND)   # a success elsewhere never lifts it: only a passing test
        self.assertIsNotNone(openai_auth.hold(self.home))
        setup_steps.update_state(lambda state: state.update(codex_path=None), self.home)
        setup_steps.test_saved_openai(CONFIG, self.home, mock.Mock(), Transport())
        self.assertIsNone(openai_auth.hold(self.home))
        self.assertIsNone(openai_auth.banner(self.home))

    def signed_in_with_chatgpt(self):
        program = self.home / "codex"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        setup_steps.update_state(lambda state: state.update(codex_path=str(program)), self.home)
        patch = mock.patch.object(setup_steps, "codex_login_status", return_value="SIGNED_IN")
        status = patch.start()
        self.addCleanup(patch.stop)
        return status

    def test_a_refused_key_falls_back_to_chatgpt_then_back_to_the_key(self):
        self.api_mode()
        openai_auth.mark_tested(KEY, self.home)
        status = self.signed_in_with_chatgpt()
        limit = self.check(self.CASES[openai_auth.KEY_REFUSED])
        self.assertFalse(limit.detail()["counts_as_attempt"])
        self.assertEqual(status.call_args[0][0], str(self.home / "codex"))   # local `codex login status` only
        self.assertIsNone(openai_auth.hold(self.home))
        self.assertEqual(openai_auth.fallback(self.home)["kind"], openai_auth.KEY_REFUSED)
        self.assertTrue(openai_auth.api_key_mode(self.home))   # codex_auth stays api_key
        banner = openai_auth.banner(self.home)
        self.assertEqual(banner["text"], "Your OpenAI API key stopped working (OpenAI refused it), so Sidedoor is "
                         "using your ChatGPT sign-in. Replace the key in Settings > Connections.")
        chatgpt = ["codex", "exec", "-c", openai_auth.FORCED_LOGIN]
        self.assertIs(openai_auth.command(chatgpt), chatgpt)            # per call: the ChatGPT call exactly
        variables = {"HOME": "/h"}
        self.assertIs(openai_auth.environment(chatgpt, variables), variables)   # no CODEX_API_KEY
        self.assertIsNone(self.check(self.CASES[openai_auth.KEY_REFUSED]))  # a ChatGPT call: plan rules only
        import scheduler_loop
        spawned = []
        loop = scheduler_loop.Loop(root=self.home, spawner=lambda name: spawned.append(name) or mock.Mock(),
                                   is_paused=lambda: False, config={})
        self.assertFalse(loop.tick()["paused"])
        line = doctor.check_openai_key(self.home, CONFIG)[-1]
        self.assertEqual((line["status"], line["name"]), (doctor.WARN, "OpenAI API"))
        self.assertIn("using your ChatGPT sign-in", line["line"])
        setup_steps.test_saved_openai(CONFIG, self.home, mock.Mock(), Transport())   # a key passes its test
        self.assertIsNone(openai_auth.fallback(self.home))
        self.assertEqual(openai_auth.command(chatgpt), ["codex", "exec"])
        self.assertEqual(openai_auth.environment(chatgpt, variables)["CODEX_API_KEY"], KEY)
        self.assertIsNone(openai_auth.banner(self.home))

    def test_out_of_credit_falls_back_at_once_with_chatgpt(self):
        self.api_mode()
        self.signed_in_with_chatgpt()
        self.check(self.CASES[openai_auth.OUT_OF_CREDIT])
        self.assertEqual(openai_auth.fallback(self.home)["kind"], openai_auth.OUT_OF_CREDIT)
        self.assertIsNone(openai_auth.hold(self.home))

    def test_a_refused_key_without_chatgpt_holds(self):
        self.api_mode()
        program = self.home / "codex"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        setup_steps.update_state(lambda state: state.update(codex_path=str(program)), self.home)
        with mock.patch.object(setup_steps, "codex_login_status", return_value="NOT_SIGNED_IN"):
            self.check(self.CASES[openai_auth.KEY_REFUSED])
        self.assertIsNone(openai_auth.fallback(self.home))
        self.assertEqual(openai_auth.hold(self.home)["kind"], openai_auth.KEY_REFUSED)

    def test_waits_past_a_day_escalate_to_a_hold_and_a_success_ends_the_streak(self):
        self.api_mode()
        line = self.CASES[openai_auth.OUT_OF_CREDIT]
        self.check(line, now=self.NOW)
        openai_auth.succeeded(OPENAI_COMMAND)
        self.assertIsNone(openai_auth.last_wait(self.home, self.NOW))   # AK5
        for hours in range(0, openai_auth.ESCALATE_HOURS):
            self.check(line, now=self.NOW + dt.timedelta(hours=hours))
        self.assertIsNone(openai_auth.hold(self.home))
        self.check(line, now=self.NOW + dt.timedelta(hours=openai_auth.ESCALATE_HOURS))
        self.assertEqual(openai_auth.hold(self.home)["kind"], openai_auth.OUT_OF_CREDIT)
        self.assertEqual(openai_auth.banner(self.home)["action_label"], "Test key")

    def test_only_the_terminal_error_counts(self):
        self.api_mode()
        retried = "ERROR: stream error: status 429 Too Many Requests; retrying\nERROR: something else broke\n"
        self.assertIsNone(self.check(retried))
        self.assertIsNone(self.check("ERROR: processed 401 files\n"))   # 401 needs HTTP-status context
        self.assertFalse((self.home / openai_auth.WAIT_FILE).exists())

    def test_openrouter_errors_are_never_openai_waits(self):
        self.api_mode()
        for line in self.CASES.values():
            self.assertIsNone(self.check(line, command=OPENROUTER_COMMAND))
            self.assertIsNone(self.check(line, command=None))
        self.assertIsNone(openai_auth.hold(self.home))
        self.assertIsNone(openai_auth.last_wait(self.home))

    def test_out_of_credit_doctor_line_in_local_time(self):
        self.api_mode()
        openai_auth.mark_tested(KEY, self.home)
        self.check(self.CASES[openai_auth.OUT_OF_CREDIT], now=dt.datetime.now(dt.timezone.utc))
        lines = doctor.check_openai_key(self.home, CONFIG)
        self.assertEqual(lines[0]["status"], doctor.OK)
        self.assertEqual(lines[1]["name"], "OpenAI API")
        self.assertIn("out of credit", lines[1]["line"])
        self.assertIn("no attempts spent", lines[1]["line"])
        resume = dt.datetime.fromisoformat(openai_auth.last_wait(self.home)["resume_at"]).astimezone()
        self.assertIn(resume.strftime("%Y-%m-%d %H:%M"), lines[1]["line"])
        self.assertNotIn(KEY, json.dumps(lines))
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=openai_auth.BANNER_HOURS + 1)
        self.assertIsNone(openai_auth.banner(self.home, later))

    def test_doctor_never_tested_wording(self):
        self.api_mode()
        self.assertIn("not tested yet", doctor.check_openai_key(self.home, CONFIG)[0]["line"])
        openai_auth.mark_tested(KEY, self.home, passed=False)
        self.assertIn("last test did not pass", doctor.check_openai_key(self.home, CONFIG)[0]["line"])

    def test_saved_artifacts_are_not_reclassified(self):
        # AK4: an old failure's stderr is read under the plan's rule only, in every mode.
        self.api_mode()
        log = self.home / "stderr.log"
        log.write_text(self.CASES[openai_auth.OUT_OF_CREDIT] + "\n")
        self.assertFalse(provider_limits.artifact_limited(str(log)))
        self.assertIsInstance(provider_limits.check(1, log, command=OPENAI_COMMAND), provider_limits.ProviderUsageLimit)


class KeyTest(Home):
    def test_works(self):
        transport = Transport()
        self.assertEqual(openai_auth.test_key(KEY, CONFIG, transport), (True, None, "Key works."))
        self.assertEqual([call[:2] for call in transport.calls],
                         [("GET", openai_auth.API_URL + "/models"), ("POST", openai_auth.API_URL + "/responses")])
        self.assertEqual(transport.calls[1][2]["model"], "gpt-test-astra")
        self.assertEqual(transport.calls[1][2]["max_output_tokens"], 16)

    def test_failures(self):
        quota = json.dumps({"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}).encode()
        cases = [(Transport(models=(401, b"{}")), openai_auth.KEY_REFUSED),
                 (Transport(models=(200, json.dumps({"data": [{"id": "gpt-test-astra"}]}).encode())), "models"),
                 (Transport(responses=(429, quota)), openai_auth.OUT_OF_CREDIT),
                 (Transport(responses=(429, b'{"error": {"code": "rate_limit_exceeded"}}')), openai_auth.RATE_LIMIT),
                 (Transport(models=OSError("down")), "network")]
        for transport, kind in cases:
            with self.subTest(kind=kind):
                ok, got, words = openai_auth.test_key(KEY, CONFIG, transport)
                self.assertFalse(ok)
                self.assertEqual(got, kind)
                self.assertNotIn(KEY, words)
        self.assertIn("gpt-test-sol", openai_auth.test_key(KEY, CONFIG, cases[1][0])[2])


class SetupStep6(Home):
    def setUp(self):
        super().setUp()
        program = self.home / "codex"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        setup_steps.update_state(lambda state: state.update(codex_path=str(program)), self.home)
        self.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in using ChatGPT\n", ""))

    def state(self):
        return setup_steps.load_state(self.home)

    def test_api_key_choice_save_test_switch_back_and_return_without_retyping(self):
        state = setup_steps.choose_codex_auth("api_key", CONFIG, self.home, self.run)
        self.assertEqual((setup_steps.codex_auth(state), setup_steps.codex_auth_view(state)), ("chatgpt", "api_key"))
        self.assertFalse(openai_auth.api_key_mode(self.home))   # no key yet: ChatGPT stays in use
        transport = Transport()
        state = setup_steps.save_and_test_openai(KEY, CONFIG, self.home, self.run, transport)
        self.assertEqual(state["steps"]["codex"]["status"], "done")
        self.assertTrue(setup_steps.codex_done(state))
        self.assertTrue(openai_auth.api_key_mode(self.home))
        self.assertEqual(oct((self.home / openai_auth.KEY_FILE).stat().st_mode & 0o777), "0o600")
        self.assertNotIn(KEY, json.dumps(state))
        self.run.assert_not_called()   # the API key does not ask Codex's own login
        state = setup_steps.choose_codex_auth("chatgpt", CONFIG, self.home, self.run)
        self.assertFalse(openai_auth.api_key_mode(self.home))
        self.assertEqual(openai_auth.saved_key(self.home), KEY)   # switching back keeps the key
        self.assertEqual(state["steps"]["codex"]["message"], "Codex is installed and signed in.")
        again = Transport()
        state = setup_steps.choose_codex_auth("api_key", CONFIG, self.home, self.run, again)
        self.assertEqual(again.calls, [])   # tested before: used at once, never retyped or retested
        self.assertTrue(openai_auth.api_key_mode(self.home))
        state = setup_steps.remove_openai_key(self.home, self.run)
        self.assertIsNone(openai_auth.saved_key(self.home))
        self.assertFalse(openai_auth.api_key_mode(self.home))

    def test_a_refused_key_is_never_written_and_leaves_the_working_one(self):
        state = setup_steps.save_and_test_openai(KEY, CONFIG, self.home, self.run, Transport(models=(401, b"{}")))
        self.assertEqual(state["steps"]["codex"]["message"], "OpenAI did not accept this key.")
        self.assertIsNone(openai_auth.saved_key(self.home))   # AK1: tested in memory, not written
        self.assertFalse(openai_auth.api_key_mode(self.home))
        setup_steps.save_and_test_openai(KEY, CONFIG, self.home, self.run, Transport())
        seen = []
        def test(key, config, transport=None, timeout=30):
            seen.append((openai_auth.saved_key(self.home), openai_auth.environment(["codex"], {})["CODEX_API_KEY"]))
            return False, openai_auth.KEY_REFUSED, "OpenAI did not accept this key."
        with mock.patch.object(openai_auth, "test_key", test):
            setup_steps.save_and_test_openai("sk-proj-new-candidate-0000", CONFIG, self.home, self.run)
        self.assertEqual(seen, [(KEY, KEY)])   # during the test the workers still get the working key
        self.assertEqual(openai_auth.saved_key(self.home), KEY)
        self.assertTrue(openai_auth.tested(self.home))
        self.assertTrue(openai_auth.api_key_mode(self.home))


DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class DashboardStep6(SetupStep6):
    PORT, SECRET = 47823, "test-token-value"

    def setUp(self):
        super().setUp()
        import license_fixtures
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        from fastapi.testclient import TestClient
        import dashboard
        app = dashboard.create_app(self.SECRET, self.PORT, home=self.home, config=dict(CONFIG, text_model={"model": "t/m"}),
                                   relay_url="https://relay.test")
        self.client = TestClient(app, base_url="http://127.0.0.1:%d" % self.PORT, follow_redirects=False)
        self.client.get("/", params={"t": self.SECRET})
        self.headers = {"origin": "http://127.0.0.1:%d" % self.PORT, "hx-request": "true"}

    def post(self, path, transport=None, **data):
        with mock.patch.object(openai_auth, "_request", transport or Transport()), \
                mock.patch.object(setup_steps.subprocess, "run", self.run):
            return self.client.post(path, data=data, headers=self.headers)

    def test_setup_offers_both_and_never_shows_the_saved_key(self):
        page = self.client.get("/setup").text
        self.assertIn("Sign in with ChatGPT", page)
        self.assertIn("Use an OpenAI API key", page)
        self.assertNotIn('id="openai-key"', page)   # ChatGPT is the default: no key box
        self.assertIn('id="openai-key"', self.post("/setup/codex/auth", choice="api_key").text)
        response = self.post("/setup/codex/key", key=KEY)
        self.assertIn("uses your OpenAI API key", response.text)
        page = self.client.get("/setup").text
        self.assertIn(KEY[-4:], page)
        self.assertNotIn(KEY, page + response.text)
        refused = self.post("/setup/codex/key", Transport(models=(401, b"{}")), key="sk-proj-typo-key-1111")
        self.assertIn('value="sk-proj-typo-key-1111"', refused.text)
        self.assertEqual(openai_auth.saved_key(self.home), KEY)

    def test_settings_box_follows_the_settings_rule_and_keeps_a_working_key(self):
        self.post("/settings/codex/auth", choice="api_key")
        self.post("/settings/codex/key", key=KEY)
        page = self.client.get("/settings").text
        self.assertIn('id="s-openai-key" type="password" name="key" value="%s"' % KEY, page)   # hidden, eye shows it
        self.assertIn('data-reveal="s-openai-key"', page)
        refused = self.post("/settings/codex/key", Transport(models=(401, b"{}")), key="sk-proj-other-key-9999")
        self.assertIn("Your previous key is still in use.", refused.text)
        self.assertIn('value="sk-proj-other-key-9999"', refused.text)   # kept in its hidden box, never retyped
        self.assertIn("uses a tiny amount of credit", refused.text)
        self.assertEqual(openai_auth.saved_key(self.home), KEY)
        self.assertTrue(openai_auth.tested(self.home))
        self.assertTrue(openai_auth.api_key_mode(self.home))

    def test_out_of_credit_banner(self):
        self.post("/settings/codex/auth", choice="api_key")
        self.post("/settings/codex/key", key=KEY)
        provider_limits.check_output(1, BillingWaits.CASES[openai_auth.OUT_OF_CREDIT], command=OPENAI_COMMAND)
        self.assertIn("Your OpenAI API key is out of credit.", self.client.get("/settings").text)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class KeptInTheCredentialStore(Home):
    """E2.T4 x E0.T8: the OpenAI key saved through Settings is kept like the OpenRouter key (the
    fake OS store here), redacted, and never read as "missing" while the store is locked."""
    PORT, SECRET = 47824, "test-token-value"

    def setUp(self):
        super().setUp()
        program = self.home / "codex"
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        setup_steps.update_state(lambda state: state.update(codex_path=str(program)), self.home)
        self.run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in using ChatGPT\n", ""))
        import credential_fixtures
        import license_fixtures
        self.store = credential_fixtures.use_fake(self)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        from fastapi.testclient import TestClient
        import dashboard
        app = dashboard.create_app(self.SECRET, self.PORT, home=self.home, config=dict(CONFIG, text_model={"model": "t/m"}),
                                   relay_url="https://relay.test")
        self.client = TestClient(app, base_url="http://127.0.0.1:%d" % self.PORT, follow_redirects=False)
        self.client.get("/", params={"t": self.SECRET})
        self.headers = {"origin": "http://127.0.0.1:%d" % self.PORT, "hx-request": "true"}

    def post(self, path, **data):
        with mock.patch.object(openai_auth, "_request", Transport()), \
                mock.patch.object(setup_steps.subprocess, "run", self.run):
            return self.client.post(path, data=data, headers=self.headers)

    def lock(self):
        import credentials
        error = credentials.Locked("KEYCHAIN_LOCKED")
        self.store.fail.update(read=error, write=error, delete=error)
        credentials.clear_cache()

    def test_a_key_saved_in_settings_is_in_the_store_and_redacted(self):
        import credentials
        import failures_page
        self.post("/settings/codex/auth", choice="api_key")
        self.assertIn("uses your OpenAI API key", self.post("/settings/codex/key", key=KEY).text)
        self.assertEqual(self.store.values(), [KEY])                    # in the OS store
        self.assertFalse((self.home / openai_auth.KEY_FILE).exists())   # not left in a file
        self.assertEqual(openai_auth.environment(["codex"], {})["CODEX_API_KEY"], KEY)
        self.assertTrue(openai_auth.tested(self.home))                  # openai.key.tested stays a file
        self.assertTrue((self.home / openai_auth.TESTED_FILE).exists())
        self.lock()
        secrets = failures_page.secrets_of(self.home, CONFIG)
        self.assertNotIn(KEY, failures_page.redact_text("codex said %s" % KEY, secrets))
        with self.assertRaises(credentials.Locked):   # never "no key" while locked
            openai_auth.saved_key(self.home)
        with self.assertRaises(credentials.Locked):
            openai_auth.environment(["codex"], {})
        page = self.client.get("/settings").text
        self.assertNotIn("No key saved yet", page)
        self.assertNotIn(KEY, page)
        refused = self.post("/settings/codex/key", key="sk-proj-while-locked-2222")
        self.assertIn("Unlock your keychain, then click Save.", refused.text)   # refused, nothing saved
        self.assertEqual(self.store.values(), [KEY])
        self.post("/settings/codex/key/remove")
        self.assertEqual(self.store.values(), [KEY])   # a refused delete leaves the key
        self.assertTrue(openai_auth.api_key_mode(self.home))
        self.store.fail.clear()
        credentials.clear_cache()
        self.post("/settings/codex/key/remove")
        self.assertEqual(self.store.values(), [])
        self.assertFalse((self.home / openai_auth.TESTED_FILE).exists())
