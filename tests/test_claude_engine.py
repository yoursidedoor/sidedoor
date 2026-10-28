"""U1 phase 3c: the Claude engine core, read against scrubbed excerpts of the real spike runs.

tests/fixtures/claude/*.jsonl are cut from the phase-1 (u1spike/runs) and spike-2 (u1spike2/runs) stream-json
logs of Claude 2.1.301, with session ids, paths, timestamps and long texts removed or shortened. A rejected
rate_limit_event and 429 / 529 / 401 results were never observed live: those cases change one field of a
real event (named in each test).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engines
import model_calls
import provider_limits
import provider_pacing
import setup_steps

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
CLAUDE = engines.CLAUDE


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def lines(name):
    return [json.loads(line) for line in fixture(name).splitlines()]


def joined(events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def api_key_mode(on=True):
    return patch.object(setup_steps, "claude_api_key_mode", return_value=on)


def executable(folder, name="claude"):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def tearDownModule():
    # The wait tests write Claude's wait files into the example home (U1 3d C4): never left for later banners.
    provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))


class SelectionTests(unittest.TestCase):
    def test_only_the_dev_key_picks_claude(self):
        self.assertIs(engines.engine({"engine": "claude"}), CLAUDE)
        for config in (None, {}, {"engine": "codex"}, {"engine": "Claude"}, {"model": "m"}):
            self.assertIs(engines.engine(config), engines.CODEX)

    def test_record_run_names_claude_and_readers_pick_it(self):
        folder = Path(tempfile.mkdtemp())
        engines.record_run(folder, CLAUDE)
        self.assertEqual(json.loads((folder / "engine.json").read_text()), {"engine": "claude"})
        self.assertIs(engines.for_run(folder), CLAUDE)
        self.assertIs(engines.for_run(folder / "trace.jsonl"), CLAUDE)

    def test_for_command_and_pacing_key(self):
        command = CLAUDE.command({"claude": "/x/claude"})
        self.assertIs(engines.for_command(command), CLAUDE)
        self.assertEqual(provider_pacing.provider_of(command), "anthropic")
        self.assertIs(engines.for_command(["/x/codex", "exec", "-"]), engines.CODEX)
        self.assertEqual(CLAUDE.pacing_provider, "anthropic")

    def test_not_wired_workers_refuse_clearly(self):
        with self.assertRaisesRegex(NotImplementedError, "CLAUDE_BROWSER_SESSION_REFUSED"):
            CLAUDE.browser_command({"claude": "/x/claude", "_browser_operation": "submit"}, "/tmp")


class CommandTests(unittest.TestCase):
    def test_spike_flags_and_never_the_unsafe_ones(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "schema.json").write_text('{"type": "object", "properties": {"n": {"type": "integer"}}}')
        command = CLAUDE.command({"claude": "/x/claude"}, folder, tools=("WebSearch",),
                                 allowed=("mcp__browser__browser_snapshot",), mcp_config=folder / "mcp.json",
                                 schema=folder / "schema.json", read=True)
        self.assertEqual(command[:16], [
            "/x/claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--setting-sources", "project",
            "--strict-mcp-config", "--restricted", "--disable-slash-commands", "--model"])
        self.assertEqual(command[16], "sonnet")
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch")
        self.assertEqual(command[command.index("--allowedTools") + 1], "WebSearch,mcp__browser__browser_snapshot")
        self.assertEqual(command[command.index("--mcp-config") + 1], str(folder / "mcp.json"))
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1])["properties"]["n"]["type"], "integer")
        for unsafe in ("--bare", "--safe-mode", "--dangerously-skip-permissions"):
            self.assertNotIn(unsafe, command)

    def test_models_from_config_keys(self):
        self.assertEqual(CLAUDE.model({}), "opus")
        self.assertEqual(CLAUDE.model({}, read=True), "sonnet")
        self.assertEqual(CLAUDE.model({"claude_model": "m1", "claude_read_model": "m2"}, read=True), "m2")
        bare = CLAUDE.command({"claude": "/x/claude"})
        self.assertEqual(bare[bare.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", bare)



class EnvironmentTests(unittest.TestCase):
    BASE = {"HOME": "/h", "USER": "u", "PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "shell-key",
            "ANTHROPIC_BASE_URL": "http://x", "OPENAI_API_KEY": "o", "SHELL": "/bin/zsh"}

    def test_subscription_strips_anthropic_and_keeps_user(self):
        with patch.dict(os.environ, self.BASE, clear=True):
            environment = CLAUDE.environment(None, config={}, tool_timeout=630)
        self.assertEqual(environment, {"HOME": "/h", "USER": "u", "PATH": "/usr/bin:/bin",
                                       "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "MCP_TOOL_TIMEOUT": "630000"})
        with patch.dict(os.environ, self.BASE, clear=True):
            self.assertEqual(CLAUDE.environment(None, config={})["MCP_TOOL_TIMEOUT"], "110000")

    def test_api_key_mode_takes_the_saved_key(self):
        with patch.dict(os.environ, self.BASE, clear=True), api_key_mode(), \
                patch("credentials.get", return_value="saved-key") as get:
            environment = CLAUDE.environment(None, config={})
        get.assert_called_once_with("anthropic", None)
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "saved-key")
        self.assertNotIn("ANTHROPIC_BASE_URL", environment)
        with patch.dict(os.environ, self.BASE, clear=True), api_key_mode(), patch("credentials.get", return_value=None):
            with self.assertRaisesRegex(OSError, "ANTHROPIC_API_KEY_MISSING"):
                CLAUDE.environment(None, config={})
        # A config key no longer switches it: setup-state is the one source.
        with patch.dict(os.environ, self.BASE, clear=True):
            self.assertNotIn("ANTHROPIC_API_KEY", CLAUDE.environment(None, config={"claude_auth": "api_key"}))

    def test_engine_and_credentials_read_the_same_source(self):
        # C5: setup-state claude_auth decides both the engine's mode and whether the key counts for holds.
        import credentials
        home = Path(tempfile.mkdtemp())
        self.assertFalse(CLAUDE.api_key_mode(home))
        self.assertFalse(credentials._wanted("anthropic", home))
        (home / setup_steps.STATE_FILE).write_text(json.dumps({"claude_auth": "api_key"}))
        self.assertTrue(CLAUDE.api_key_mode(home))
        self.assertTrue(credentials._wanted("anthropic", home))

    def test_credentials_know_the_anthropic_name(self):
        import credentials
        self.assertEqual(credentials.NAMES["anthropic"], "anthropic.key")


class FindAndProbeTests(unittest.TestCase):
    def test_every_install_location_is_searched(self):
        home = Path.home()
        candidates = setup_steps.claude_candidates(search_path="")
        for folder in (home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                       home / ".npm-global" / "bin", home / ".claude" / "local"):
            self.assertIn(str(folder / "claude"), candidates)

    def test_found_in_a_dock_launch_with_no_path(self):
        folder = tempfile.mkdtemp()
        path = executable(Path(folder) / ".claude" / "local")
        with patch.object(Path, "home", return_value=Path(folder)), patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertEqual(setup_steps.find_claude(search_path="/usr/bin:/bin"), path)
            config = setup_steps.with_claude({"model": "m"}, home=folder)
        self.assertEqual(config["claude"], path)

    def test_not_found_is_a_no_try_wait(self):
        with patch.object(setup_steps, "find_claude", return_value=None), \
                patch.object(setup_steps, "load_state", return_value={}):
            config = setup_steps.with_claude({"claude": "/nowhere/claude", "model": "m"})
            wait = CLAUDE.wait(config)
        self.assertTrue(config[setup_steps.CLAUDE_MISSING])
        self.assertIsInstance(wait, provider_limits.ProviderUsageLimit)
        self.assertEqual(wait.kind, provider_limits.CLAUDE_NOT_FOUND)
        self.assertFalse(wait.detail()["counts_as_attempt"])
        self.assertIsNone(setup_steps.claude_wait({"claude": "/x"}))

    def probe(self, stdout):
        return lambda *args, **options: subprocess.CompletedProcess(args[0], 0, stdout, "")

    def test_probe_reads_logged_in_not_the_exit_code(self):
        signed_in = '{"loggedIn": true, "authMethod": "claude.ai", "email": "a@b.c"}'
        self.assertEqual(setup_steps.claude_auth_status("/x/claude", {}, run=self.probe(signed_in)),
                         {"loggedIn": True, "authMethod": "claude.ai"})
        self.assertIsNone(setup_steps.claude_auth_status("/x/claude", {}, run=self.probe("not json")))
        folder = tempfile.mkdtemp()
        config = {"claude": executable(folder)}
        signed_out = self.probe('{"loggedIn": false}')
        wait = CLAUDE.wait(config, run=signed_out)
        self.assertEqual(wait.kind, provider_limits.CLAUDE_SIGNED_OUT)
        self.assertIsNone(CLAUDE.wait(config, run=self.probe(signed_in)))
        with api_key_mode():
            self.assertIsNone(CLAUDE.wait(config, run=signed_out))

    def test_probe_runs_with_the_job_environment(self):
        seen = {}
        def run(argv, **options):
            seen.update(argv=argv, env=options["env"])
            return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true}', "")
        folder = tempfile.mkdtemp()
        with patch.dict(os.environ, {"HOME": "/h", "USER": "u", "PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "k"},
                        clear=True):
            CLAUDE.wait({"claude": executable(folder)}, run=run)
        self.assertEqual(seen["argv"][1:], ["auth", "status"])
        self.assertEqual(seen["env"]["USER"], "u")
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])


class ReaderTests(unittest.TestCase):
    def test_structured_answer_cost_and_output_json(self):
        record = CLAUDE.read_trace(fixture("r15_structured.jsonl"))
        self.assertEqual(record.answer, {"n": 7})
        self.assertEqual(record.tool_calls, [])   # StructuredOutput is the answer, not a tool call
        self.assertIsNone(record.error_kind)
        self.assertEqual(record.cost, 0.010814)
        self.assertEqual(record.model, "claude-sonnet-5-5")
        self.assertEqual(record.usage["output_tokens"], 51)
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(json.loads(CLAUDE.write_answer(folder, record).read_text()), {"n": 7})
        self.assertIsNone(CLAUDE.check_run(0, fixture("r15_structured.jsonl")))

    def test_mcp_calls_pair_across_lines(self):
        reader = CLAUDE.trace_reader()
        events = [event for line in fixture("run2_mcp_schema.jsonl").splitlines() for event in reader.feed(line)]
        started = [event for event in events if event.kind == "tool" and event.phase == "started"]
        completed = [event for event in events if event.kind == "tool" and event.phase == "completed"]
        self.assertEqual(len(started), 3)
        self.assertEqual([event.call.id for event in started], [event.call.id for event in completed])
        first = completed[0].call
        self.assertEqual((first.server, first.tool, first.args), ("notes", "save_note", {"text": "apple"}))
        self.assertEqual(first.result["content"], [{"type": "text", "text": "saved: apple"}])
        self.assertIs(first.is_error, False)
        record = CLAUDE.read_trace(fixture("run2_mcp_schema.jsonl"))
        self.assertEqual(record.answer["items"], ["apple", "bicycle", "lantern"])

    def test_several_tool_uses_on_one_line_then_results_later(self):
        # A real assistant line with two tool_use blocks (r01 line 2 joined to line 3's block).
        events = lines("r01_web.jsonl")
        double = json.loads(json.dumps(events[1]))
        double["message"]["content"] += events[2]["message"]["content"]
        reader = CLAUDE.trace_reader(allowed=("WebSearch", "WebFetch"))
        started = reader.feed(json.dumps(double))
        self.assertEqual([event.phase for event in started], ["started", "started"])
        reader.feed(json.dumps(events[4]))   # the second search's result comes first
        done = reader.feed(json.dumps(events[5]))
        self.assertEqual(done[0].call.id, started[0].call.id)
        self.assertEqual(done[0].call.args["query"], started[0].call.args["query"])

    def test_web_urls_and_http_codes_come_from_tool_use_result(self):
        record = CLAUDE.read_trace(fixture("r01_web.jsonl"), allowed=("WebSearch", "WebFetch"))
        self.assertIsNone(record.error_kind)
        self.assertEqual(CLAUDE.check_run(0, fixture("r01_web.jsonl"), allowed=("WebSearch", "WebFetch"),
                                          answer_expected=False), None)
        self.assertEqual(len(record.web_searches), 3)   # 2 searches + the one 2xx fetch (U1 3d: a web read)
        searches = [call for call in record.tool_calls if call.tool == "WebSearch"]
        self.assertTrue(all(url.startswith("http") for url in searches[0].result["urls"]))
        self.assertEqual([fetch["code"] for fetch in record.web_fetches], [404, 200])
        self.assertEqual(record.web_fetches[1]["url"], "https://jobs.ashbyhq.com/modal")
        missing = [call for call in record.tool_calls if call.tool == "WebFetch"][0]
        self.assertIs(missing.is_error, False)   # a 404 fetch is not is_error: the code says it

    def test_mcp_server_failed_at_init(self):
        # spike2 r13: init mcp_servers [{name: slow, status: failed}], exit 0.
        head = fixture("r13_mcp_failed.jsonl").splitlines()[:3]
        reader = CLAUDE.trace_reader()
        events = [event for line in head for event in reader.feed(line)]
        self.assertEqual([(event.kind, event.raw_kind) for event in events if event.kind == "error"],
                         [("error", CLAUDE.MCP_SERVER_FAILED)])
        found = CLAUDE.check_run(0, fixture("r13_mcp_failed.jsonl"))
        self.assertIsInstance(found, engines.ClaudeRunFailure)
        self.assertEqual(found.code, CLAUDE.MCP_SERVER_FAILED)
        self.assertIsNone(CLAUDE.limit_output(0, fixture("r13_mcp_failed.jsonl")))   # not a wait

    def test_permission_denials(self):
        record = CLAUDE.read_trace(fixture("run3_denied.jsonl"))
        self.assertEqual(record.error_kind, CLAUDE.PERMISSION_DENIED)
        self.assertEqual([code for code, unused in record.errors], [CLAUDE.PERMISSION_DENIED])   # once per call
        self.assertEqual(record.denials[0]["tool_name"], "mcp__notes__save_note")
        self.assertIs(record.tool_calls[0].is_error, True)
        # 3d R1: the run still gave its answer, which is kept; with no answer the denial fails the run.
        self.assertIsNone(CLAUDE.check_run(0, fixture("run3_denied.jsonl")))
        unanswered = lines("run3_denied.jsonl")
        unanswered[-1]["structured_output"] = None
        self.assertEqual(CLAUDE.check_run(0, joined(unanswered)).code, CLAUDE.PERMISSION_DENIED)
        # result.permission_denials alone (the system event dropped) is still caught.
        only_result = [event for event in lines("run3_denied.jsonl") if event.get("subtype") != "permission_denied"]
        self.assertEqual(CLAUDE.read_trace(joined(only_result)).error_kind, CLAUDE.PERMISSION_DENIED)

    def test_tool_timeout(self):
        record = CLAUDE.read_trace(fixture("r10_tool_timeout.jsonl"))
        self.assertEqual(record.error_kind, CLAUDE.TOOL_TIMEOUT)
        self.assertEqual(record.tool_calls[0].status, "failed")
        self.assertEqual(CLAUDE.check_run(0, fixture("r10_tool_timeout.jsonl")).code, CLAUDE.TOOL_TIMEOUT)

    def test_output_too_large_is_a_failed_call(self):
        record = CLAUDE.read_trace(fixture("r12_too_large.jsonl"))
        self.assertEqual(record.error_kind, CLAUDE.TOOL_OUTPUT_TOO_LARGE)
        self.assertIs(record.tool_calls[0].is_error, True)

    def test_output_too_large_words_in_a_web_result_are_not_checked(self):
        # C9: r01's real WebFetch result (line 9) with its text changed to the overflow words.
        events = lines("r01_web.jsonl")
        events[8]["message"]["content"][0]["content"] = "a page that exceeds maximum allowed tokens, says the page"
        record = CLAUDE.read_trace(joined(events), allowed=("WebSearch", "WebFetch"))
        self.assertIsNone(record.error_kind)

    def test_kill_switch_on_any_tool_outside_the_list(self):
        # r01 ran WebSearch and WebFetch; a run allowed only WebSearch must flag the WebFetch calls.
        record = CLAUDE.read_trace(fixture("r01_web.jsonl"), allowed=("WebSearch",))
        self.assertEqual(record.error_kind, CLAUDE.FORBIDDEN_TOOL)
        self.assertIn("WebFetch", record.errors[0][1])
        # No list: any built-in tool other than StructuredOutput is forbidden; MCP tools are not.
        self.assertEqual(CLAUDE.read_trace(fixture("r01_web.jsonl")).error_kind, CLAUDE.FORBIDDEN_TOOL)
        self.assertIsNone(CLAUDE.read_trace(fixture("run2_mcp_schema.jsonl")).error_kind)
        self.assertIsNone(CLAUDE.read_trace(fixture("r15_structured.jsonl"), allowed=()).error_kind)

    def test_the_run_record_carries_the_allowed_tools(self):
        # C3: record_run saves the command's tools; for_run's engine reads the run without being told.
        folder = Path(tempfile.mkdtemp())
        command = CLAUDE.command({"claude": "/x/claude"}, tools=("WebSearch", "WebFetch"))
        engines.record_run(folder, CLAUDE, command)
        self.assertEqual(json.loads((folder / "engine.json").read_text()),
                         {"engine": "claude", "allowed": ["WebFetch", "WebSearch"]})
        engine = engines.for_run(folder)
        self.assertEqual(engine.name, "claude")
        self.assertIsNone(engine.check_run(0, fixture("r01_web.jsonl"), answer_expected=False))
        pairs = list(enumerate(fixture("r01_web.jsonl").splitlines()))
        self.assertEqual(list(engine.error_events(pairs)), [])
        self.assertIsNone(CLAUDE.check_run(0, fixture("r01_web.jsonl"), answer_expected=False, command=command))
        # Recorded with WebSearch only: the WebFetch calls are still the kill switch.
        engines.record_run(folder, CLAUDE, CLAUDE.command({"claude": "/x/claude"}, tools=("WebSearch",)))
        self.assertEqual(engines.for_run(folder).check_run(0, fixture("r01_web.jsonl")).code, CLAUDE.FORBIDDEN_TOOL)
        (folder / "engine.json").write_text('{"engine": "claude", "allowed": "WebSearch"}')
        with self.assertRaises(engines.EngineRecordError):
            engines.for_run(folder)
        engines.record_run(folder, engines.CODEX, ["/x/codex", "exec", "-"])
        self.assertEqual(json.loads((folder / "engine.json").read_text()), {"engine": "codex"})

    def test_error_events_show_codes_only(self):
        pairs = [(index, line) for index, line in enumerate(fixture("r13_mcp_failed.jsonl").splitlines())]
        self.assertEqual([(offset, code) for offset, code, unused in CLAUDE.error_events(pairs)],
                         [(0, CLAUDE.MCP_SERVER_FAILED)])

    def test_bad_lines(self):
        reader = CLAUDE.trace_reader()
        with self.assertRaises(ValueError):
            reader.feed("not json")
        with self.assertRaises(engines.TraceShapeError):
            reader.feed("[1]")
        self.assertEqual(CLAUDE.check_run(0, "not json\n" + fixture("r15_structured.jsonl")), None)


class LimitTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 28, 3, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        patcher = patch.object(provider_pacing, "rate_limited", side_effect=lambda provider, stated=None: stated or 30)
        self.rate_limited = patcher.start()
        self.addCleanup(patcher.stop)

    def rejected(self):
        # r15's real rate_limit_event with status "allowed" changed to "rejected".
        events = lines("r15_structured.jsonl")
        events[3]["rate_limit_info"]["status"] = "rejected"
        return joined(events[:1] + events[3:4])

    def errored(self, status):
        # run4a's real is_error result (api_error_status 404) with the status changed.
        events = lines("run4a_badmodel.jsonl")
        events[-1]["api_error_status"] = status
        return joined(events)

    def test_rejected_rate_limit_event_waits_until_its_reset(self):
        wait = CLAUDE.check_run(0, self.rejected(), now=self.NOW)
        self.assertEqual(wait.kind, provider_limits.RATE_LIMITED)
        self.assertEqual(wait.stated_reset, "2026-10-28T06:20:00+00:00")   # resetsAt 1793168400
        self.assertEqual(wait.next_at, "2026-10-28T06:20:00+00:00")
        self.rate_limited.assert_called_once_with("anthropic", 12000.0)
        self.assertEqual(CLAUDE.limit_output(0, self.rejected()).kind, provider_limits.RATE_LIMITED)

    def test_rejected_event_with_an_answer_is_no_wait(self):
        # C6: r15's real run (it answered {"n": 7}) with the rate_limit_event status changed to "rejected".
        events = lines("r15_structured.jsonl")
        events[3]["rate_limit_info"]["status"] = "rejected"
        self.assertIsNone(CLAUDE.check_run(0, joined(events), now=self.NOW))
        self.assertIsNone(CLAUDE.read_trace(joined(events)).error_kind)
        self.rate_limited.assert_not_called()

    def test_429_and_529_are_the_short_wait(self):
        for status in (429, 529):
            wait = CLAUDE.check_run(1, self.errored(status), now=self.NOW)
            self.assertEqual(wait.kind, provider_limits.RATE_LIMITED)
            self.assertEqual(wait.next_at, (self.NOW + dt.timedelta(seconds=30)).isoformat())
        self.assertEqual(self.rate_limited.call_args.args[0], "anthropic")

    def test_401_is_a_sign_in_wait(self):
        wait = CLAUDE.check_run(1, self.errored(401), now=self.NOW)
        self.assertEqual(wait.kind, provider_limits.CLAUDE_SIGNED_OUT)
        self.assertFalse(wait.detail()["counts_as_failure"])
        # Phase 1: no USER in the environment, exit 1 "Not logged in" and no stream.
        self.assertEqual(CLAUDE.check_run(1, "", "Not logged in · Please run /login").kind,
                         provider_limits.CLAUDE_SIGNED_OUT)

    def test_401_in_api_key_mode_is_a_refused_key(self):
        # C8
        with api_key_mode():
            wait = CLAUDE.check_run(1, self.errored(401), now=self.NOW)
        self.assertEqual(wait.kind, provider_limits.CLAUDE_KEY_REFUSED)
        self.assertEqual(wait.message, "Your Anthropic key was refused.")

    def test_other_api_errors_and_exits_are_failures(self):
        self.assertEqual(CLAUDE.check_run(1, fixture("run4a_badmodel.jsonl")).code, CLAUDE.API_ERROR)
        self.assertEqual(CLAUDE.check_run(1, "", "Error: bad --mcp-config").code, CLAUDE.EXITED)
        self.assertIsNone(CLAUDE.limit_output(1, fixture("run4a_badmodel.jsonl")))
        no_answer = [event for event in lines("r15_structured.jsonl") if event["type"] != "result"]
        self.assertEqual(CLAUDE.check_run(0, joined(no_answer)).code, CLAUDE.NO_ANSWER)

    def test_reset_at(self):
        self.assertEqual(CLAUDE.reset_at({"resetsAt": 1793168400}).isoformat(), "2026-10-28T06:20:00+00:00")
        self.assertIsNone(CLAUDE.reset_at({"resetsAt": None}))
        self.assertIsNone(CLAUDE.reset_at("try again later"))


class ModelCallTests(unittest.TestCase):
    def test_one_row_named_claude_with_the_cost(self):
        rows = []
        with patch.object(model_calls, "record", side_effect=lambda route, outcome, **fields: rows.append(
                dict(fields, route=route, outcome=outcome))):
            with api_key_mode():
                CLAUDE.record_model_call(CLAUDE.read_trace(fixture("r15_structured.jsonl")), worker="agent_worker")
            CLAUDE.record_model_call(CLAUDE.read_trace(fixture("r15_structured.jsonl")), worker="agent_worker")
        self.assertEqual(rows[0], {"route": "claude", "outcome": "ok", "worker": "agent_worker",
                                   "model": "claude-sonnet-5-5", "provider": "anthropic", "prompt_tokens": 2577,
                                   "completion_tokens": 51, "total_tokens": 2628, "cost": 0.010814, "seconds": rows[0]["seconds"]})
        self.assertIsInstance(rows[0]["seconds"], float)
        # C7: on the user's subscription total_cost_usd is not money the user pays: NULL.
        self.assertIsNone(rows[1]["cost"])
        self.assertEqual(rows[1]["total_tokens"], 2628)

    def test_the_row_is_written(self):
        import sqlite3
        path = Path(tempfile.mkdtemp()) / "jobs.sqlite3"
        with patch.object(model_calls, "_DATABASE", str(path)), api_key_mode():
            CLAUDE.record_model_call(CLAUDE.read_trace(fixture("run3_denied.jsonl")), worker="w")
        row = sqlite3.connect(path).execute("SELECT route, provider, outcome, cost FROM model_calls").fetchone()
        self.assertEqual(row[:3], ("claude", "anthropic", "refused"))
        self.assertAlmostEqual(row[3], 0.0161784)


if __name__ == "__main__":
    unittest.main()
