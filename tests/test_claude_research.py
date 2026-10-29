"""U1 phase 3d: research (discovery, qualification, recovery) and the agent worker on Claude.

The streams are the real spike-2 r01 web run (tests/fixtures/claude/r01_web.jsonl: WebSearch results, a
404 and a 200 WebFetch) and r15's result event carrying a structured answer; run3_denied is a real
permission denial that exited 0. A fake `claude` executable prints them, so the whole worker path runs.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_worker
import daily_schedule
import engines
import model_calls
import orchestrator
import provider_limits
import research_tools
import research_workflow
import setup_steps

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
CLAUDE = engines.CLAUDE
WEB = ("WebSearch", "WebFetch")


def events(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]


def stream(answer, fetch_codes=None):
    """r01's real web events, then r15's real result event carrying `answer`. `fetch_codes` keeps only the
    WebFetch results with those HTTP codes (their tool_use lines too)."""
    web = events("r01_web.jsonl")[:-1]
    if fetch_codes is not None:
        dropped = {event["message"]["content"][0]["tool_use_id"] for event in web if event["type"] == "user"
                   and isinstance(event.get("tool_use_result"), dict) and "code" in event["tool_use_result"]
                   and event["tool_use_result"]["code"] not in fetch_codes}
        web = [event for event in web if not (
            event["type"] == "user" and event["message"]["content"][0]["tool_use_id"] in dropped)
            and not (event["type"] == "assistant" and any(block.get("id") in dropped
                                                          for block in event["message"]["content"]))]
    result = events("r15_structured.jsonl")[-1]
    result["structured_output"] = answer
    return "\n".join(json.dumps(event) for event in web + [result]) + "\n"


def fake_claude(folder, text, code=0):
    """An executable named claude that prints `text` (the stream) and exits `code`."""
    folder = Path(folder)
    (folder / "stream.jsonl").write_text(text, encoding="utf-8")
    path = folder / "bin" / "claude"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat > /dev/null\ncat '%s'\nexit %d\n" % (folder / "stream.jsonl", code))
    path.chmod(0o755)
    return str(path)


def slow_claude(folder, text):
    """An executable named claude that prints `text` (a stream with no result yet) and then hangs."""
    folder = Path(folder)
    (folder / "stream.jsonl").write_text(text, encoding="utf-8")
    path = folder / "bin" / "claude"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat '%s'\nexec sleep 30\n" % (folder / "stream.jsonl"))
    path.chmod(0o755)
    return str(path)


def started_stream():
    """r01's real web events with no result event: a session cut off before it ended."""
    return "\n".join(json.dumps(event) for event in events("r01_web.jsonl")[:-1]) + "\n"


def subscription():
    return patch.object(setup_steps, "claude_api_key_mode", return_value=False)


class Builders(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.config = {"engine": "claude", "claude": "/x/claude", "codex": "/x/codex", "model": "m"}

    def tearDown(self):
        provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))

    def test_research_command_web_tools_schema_and_output(self):
        output = self.root / "r.discovery.json"
        command = orchestrator.research_command(self.root, self.config, output, "discovery-schema.json")
        self.assertIsInstance(command, engines.ClaudeCommand)
        self.assertIs(engines.for_command(command), CLAUDE)
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(command[command.index("--allowedTools") + 1], "WebSearch,WebFetch")
        self.assertIn("properties", json.loads(command[command.index("--json-schema") + 1]))
        self.assertNotIn("--mcp-config", command)
        self.assertEqual(command.output, output)

    def test_attach_adds_the_save_server_its_tools_and_its_timeouts(self):
        # parallel_research.submit: research_tools.attach(command, research_tools.prepare(...)).
        for phase, tool in (("discovery", "save_company"), ("qualification", "save_role")):
            output = self.root / ("r." + phase + ".json")
            command = orchestrator.research_command(self.root, self.config, output, "discovery-schema.json")
            command = research_tools.attach(command, research_tools.prepare(output, phase, "Fintech", 3, {}, self.config))
            allowed = command[command.index("--allowedTools") + 1].split(",")
            self.assertEqual(allowed, ["WebSearch", "WebFetch", "mcp__research_tools__" + tool])
            config = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())
            self.assertEqual(Path(command[command.index("--mcp-config") + 1]), output.with_name(output.name + ".mcp.json"))
            argv = [config["mcpServers"]["research_tools"]["command"]] + config["mcpServers"]["research_tools"]["args"]
            self.assertEqual(argv, research_tools.server_argv(research_tools.context_path(output)))
            with patch.dict(os.environ, {"HOME": "/h", "USER": "u", "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k",
                                         "OPENAI_API_KEY": "o"}, clear=True), subscription():
                environment = CLAUDE.research_environment(command)
            self.assertEqual(environment, {"HOME": "/h", "USER": "u", "PATH": "/usr/bin", "MCP_TOOL_TIMEOUT": "30000",
                                           "MCP_TIMEOUT": "20000", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
            self.assertEqual(CLAUDE.run_record(command)["allowed"], sorted(allowed))

    def test_recovery_turns_every_tool_off(self):
        output = self.root / "decision.json"
        (self.root / "s.json").write_text('{"type": "object", "properties": {"a": {"type": "string"}}}')
        research = orchestrator.research_command(self.root, self.config, output, str(self.root / "s.json"))
        command = CLAUDE.research_recovery_command(self.config, research)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", command)
        self.assertEqual(command[command.index("--json-schema") + 1], research[research.index("--json-schema") + 1])
        self.assertEqual(command.output, output)

    def test_investigation_command(self):
        (self.root / "schema.json").write_text('{"type": "object"}')
        command = agent_worker._command(self.config, self.root)
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(command.output, self.root / "output.json")
        self.assertNotIn("--mcp-config", command)

    def test_browser_jobs_still_refuse(self):
        with self.assertRaisesRegex(NotImplementedError, "CLAUDE_BROWSER_SESSION_REFUSED"):
            CLAUDE.browser_command(self.config, self.root)


class ResearchWorker(unittest.TestCase):
    def setUp(self):
        # U1 3g G2: Claude's waits show only while Claude is the chosen engine.
        chosen = patch.object(provider_limits, "chosen_engine", return_value="claude")
        chosen.start()
        self.addCleanup(chosen.stop)
        self.root = Path(tempfile.mkdtemp())
        self.output = self.root / "r.discovery.json"
        self.log = self.root / "r.discovery.log"

    def tearDown(self):
        provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))

    def run_with(self, text, code=0):
        config = {"engine": "claude", "claude": fake_claude(self.root, text, code), "model": "m"}
        command = orchestrator.research_command(self.root, config, self.output, "discovery-schema.json")
        rows = []
        with subscription(), patch.object(model_calls, "record", side_effect=lambda *a, **k: rows.append((a, k))):
            try:
                orchestrator.run_research_worker(self.root, command, "prompt", self.log, 30)
            finally:
                self.rows = rows

    def test_a_clean_run_writes_the_answer_and_one_model_calls_row(self):
        self.run_with(stream({"companies": [{"name": "Hume AI"}]}))
        self.assertEqual(json.loads(self.output.read_text()), {"companies": [{"name": "Hume AI"}]})
        record = json.loads(self.log.with_name(self.log.name + ".engine.json").read_text())
        self.assertEqual(record, {"engine": "claude", "allowed": ["WebFetch", "WebSearch"]})
        (route, outcome), fields = self.rows[0]
        self.assertEqual((route, outcome, fields["worker"], fields["provider"]), ("claude", "ok", "research", "anthropic"))
        self.assertIsNone(fields["cost"])   # the user's subscription: no money cost recorded

    def test_a5_a_run_stopped_at_its_time_limit_writes_its_model_calls_row(self):
        import subprocess
        config = {"engine": "claude", "claude": slow_claude(self.root, started_stream()), "model": "m"}
        command = orchestrator.research_command(self.root, config, self.output, "discovery-schema.json")
        rows = []
        with subscription(), patch.object(model_calls, "record", side_effect=lambda *a, **k: rows.append((a, k))):
            with self.assertRaises(subprocess.TimeoutExpired):   # still raised as before
                orchestrator.run_research_worker(self.root, command, "prompt", self.log, 1)
        self.assertEqual(len(rows), 1)
        (route, outcome), fields = rows[0]
        self.assertEqual((route, outcome, fields["worker"], fields["provider"]),
                         ("claude", "timeout", "research", "anthropic"))
        self.assertEqual(fields["model"], "claude-sonnet-5-5")   # what the stream showed before the stop
        self.assertGreaterEqual(fields["seconds"], 1)

    def test_r2_an_unreadable_stream_never_raises_from_the_time_out_record(self):
        CLAUDE.record_timed_out(self.root / "missing.log", worker="research", seconds=1)
        CLAUDE.record_timed_out(self.root, worker="research")   # a folder: IsADirectoryError, swallowed

    def test_an_exit_0_forbidden_tool_is_a_failure(self):
        text = (FIXTURES / "run3_denied.jsonl").read_text()
        with self.assertRaisesRegex(RuntimeError, "Research worker failed FORBIDDEN_TOOL") as caught:
            self.run_with(text)
        self.assertIsInstance(caught.exception.__cause__, engines.ClaudeRunFailure)
        self.assertTrue(self.output.exists())   # R1/R2: the answer is written; the failure still raises
        self.assertEqual(self.rows[0][0][1], "refused")

    def test_an_exit_0_rate_limit_is_a_wait(self):
        text = stream({"x": 1}).splitlines()
        result = json.loads(text[-1])
        result.update(is_error=True, api_error_status=429, result="API Error: 429", structured_output=None)
        with patch("provider_pacing.rate_limited", return_value=30):
            with self.assertRaises(RuntimeError) as caught:
                self.run_with("\n".join(text[:-1] + [json.dumps(result)]) + "\n")
        self.assertEqual(provider_limits.wait_of(caught.exception).kind, provider_limits.RATE_LIMITED)

    def test_a_signed_out_run_is_noted_for_the_banner_until_a_run_works(self):
        with self.assertRaisesRegex(RuntimeError, "Research worker exited 1"):
            self.run_with("Not logged in · Please run /login\n", code=1)
        found = provider_limits.waits()["claude"]
        self.assertEqual(found["kind"], provider_limits.CLAUDE_SIGNED_OUT)
        self.assertEqual(found["health"], "Claude is signed out")
        self.assertIn(found["banner"], provider_limits.banners())
        self.run_with(stream({"companies": []}))
        self.assertIsNone(provider_limits.waits()["claude"])

    def test_a_refused_schema_never_reached_the_model(self):
        # Probed 2026-10-27 on Claude 2.1.301: exit 1, no event, this line on stderr (stderr shares the log).
        refused = ("Error: --json-schema is not a valid JSON Schema: data/properties/a/type must be equal to one of "
                   "the allowed values\n")
        with self.assertRaisesRegex(RuntimeError, "Research worker exited 1"):
            self.run_with(refused, code=1)
        proof = research_workflow.worker_never_reached_model(self.log, self.output)
        self.assertEqual((proof["error_type"], proof["error_code"], proof["error_status"]),
                         ("invalid_request_error", "invalid_json_schema", 400))
        self.log.write_text(stream({"x": 1}) + refused)   # a session started: not proof
        self.assertIsNone(research_workflow.worker_never_reached_model(self.log, self.output))


class KeepWhatWasFound(unittest.TestCase):
    """R1-R3: a run that answered keeps its answer through a tool-level failure; parallel fetches count."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.output = self.root / "r.qualification.json"
        self.log = self.root / "r.qualification.log"

    def tearDown(self):
        provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))

    def run_with(self, text):
        config = {"engine": "claude", "claude": fake_claude(self.root, text), "model": "m"}
        command = orchestrator.research_command(self.root, config, self.output, "research-schema.json")
        with subscription(), patch.object(model_calls, "record"):
            orchestrator.run_research_worker(self.root, command, "prompt", self.log, 30)

    def with_failure(self, change):
        lines = [json.loads(line) for line in stream({"research_status": "COMPLETE"}).splitlines()]
        change(lines)
        return "\n".join(json.dumps(line) for line in lines) + "\n"

    def fetch_result(self, lines):
        return next(line for line in lines if line["type"] == "user" and (line.get("tool_use_result") or {}).get("code") == 200)

    def test_a_denial_keeps_the_answer(self):
        def deny(lines):
            lines[-1]["permission_denials"] = [{"tool_name": "WebFetch", "tool_use_id": "toolu_x", "tool_input": {}}]
        self.run_with(self.with_failure(deny))   # no raise: judged like Codex's answer
        self.assertEqual(json.loads(self.output.read_text()), {"research_status": "COMPLETE"})

    def test_a_built_in_fetch_that_timed_out_does_not_fail_the_run(self):
        def timed_out(lines):
            block = self.fetch_result(lines)["message"]["content"][0]
            block.update(is_error=True, content="Request timed out")
        text = self.with_failure(timed_out)
        self.assertNotIn(CLAUDE.TOOL_TIMEOUT, [code for code, _ in CLAUDE.read_trace(text, allowed=WEB).errors])
        self.run_with(text)
        self.assertTrue(self.output.is_file())

    def test_a_forbidden_tool_still_fails_but_the_cut_off_note_knows_it_answered(self):
        def bash(lines):
            lines[1]["message"]["content"][0]["name"] = "Bash"
        with self.assertRaisesRegex(RuntimeError, "Research worker failed FORBIDDEN_TOOL") as caught:
            self.run_with(self.with_failure(bash))
        self.assertTrue(self.output.is_file())   # R2: parallel_research's answered= reads this file
        note = research_tools.cut_off_note(caught.exception, [{}], "qualification",
                                           answered=self.output.is_file())
        self.assertIn("it ended without a usable final answer", note)

    def test_an_mcp_server_that_failed_still_fails(self):
        def failed(lines):
            lines[0]["mcp_servers"] = [{"name": "research_tools", "status": "failed"}]
        with self.assertRaisesRegex(RuntimeError, "MCP_SERVER_FAILED"):
            self.run_with(self.with_failure(failed))

    def test_parallel_fetches_each_carry_their_code(self):
        # A real run (3d probe, 2026-10-27): two WebFetch calls in one turn, one result per user line.
        record = CLAUDE.read_trace((FIXTURES / "r3d_parallel_fetch.jsonl").read_text(), allowed=("WebFetch",))
        self.assertEqual([fetch["code"] for fetch in record.web_fetches], [200, 200])
        self.assertEqual(len(record.web_searches), 2)

    def test_several_results_on_one_line_pair_by_position(self):
        lines = [json.loads(line) for line in (FIXTURES / "r3d_parallel_fetch.jsonl").read_text().splitlines()]
        users = [line for line in lines if line["type"] == "user"]
        users[0]["message"]["content"] += users[1]["message"]["content"]
        users[0]["tool_use_result"] = [users[0]["tool_use_result"], dict(users[1]["tool_use_result"], code=404)]
        lines.remove(users[1])
        record = CLAUDE.read_trace("\n".join(json.dumps(line) for line in lines), allowed=("WebFetch",))
        self.assertEqual([fetch["code"] for fetch in record.web_fetches], [200, 404])
        self.assertEqual(len(record.web_searches), 1)


class AgentWorker(unittest.TestCase):
    ANSWER = {"status": "RESOLVED", "summary": "Hume AI lists the role in New York.",
              "citations": [{"source": "https://www.builtinnyc.com/job/x/10075969", "claim": "Listed in New York"}],
              "unresolved_reason": None, "next_action": None}

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "schema.json").write_text('{"type": "object"}')

    def tearDown(self):
        provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))

    def execute(self, text):
        config = {"engine": "claude", "claude": fake_claude(tempfile.mkdtemp(), text), "model": "m"}
        with subscription(), patch.object(engines.ClaudeEngine, "wait", return_value=None), \
                patch.object(model_calls, "record"):
            agent_worker._execute(config, "prompt", self.directory, 30)
        return agent_worker._result(self.directory)

    def test_resolved_with_searches_and_a_2xx_fetch(self):
        model, lines = self.execute(stream(self.ANSWER))
        self.assertEqual(model, self.ANSWER)
        self.assertEqual(len(lines), 3)   # 2 WebSearch + the one fetch that answered 200 (not the 404)

    def test_a_2xx_fetch_alone_is_a_web_read(self):
        text = "\n".join(line for line in stream(self.ANSWER, fetch_codes=(200,)).splitlines()
                         if "WebSearch" not in line and '"results"' not in line) + "\n"
        model, lines = self.execute(text)
        self.assertEqual(model["status"], "RESOLVED")
        self.assertEqual(len(lines), 1)

    def test_only_non_2xx_fetches_are_no_evidence(self):
        text = "\n".join(line for line in stream(self.ANSWER, fetch_codes=(404, 429)).splitlines()
                         if "WebSearch" not in line and '"results"' not in line) + "\n"
        with self.assertRaisesRegex(ValueError, "OBSERVED_WEB_SEARCH_AND_CITATIONS_REQUIRED"):
            self.execute(text)

    def test_a_claude_wait_names_its_own_kind(self):
        # R8
        config = {"engine": "claude", "claude": "/x/claude", "model": "m"}
        wait = provider_limits.ProviderUsageLimit("x", kind=provider_limits.CLAUDE_SIGNED_OUT)
        with patch.object(engines.ClaudeEngine, "wait", return_value=wait):
            with self.assertRaisesRegex(RuntimeError, "^CLAUDE_SIGNED_OUT$") as caught:
                agent_worker._execute(config, "prompt", self.directory, 30)
        self.assertIs(provider_limits.wait_of(caught.exception), wait)

    def test_a5_a_run_stopped_at_its_time_limit_writes_its_model_calls_row(self):
        import subprocess
        config = {"engine": "claude", "claude": slow_claude(tempfile.mkdtemp(), started_stream()), "model": "m"}
        rows = []
        with subscription(), patch.object(engines.ClaudeEngine, "wait", return_value=None), \
                patch.object(model_calls, "record", side_effect=lambda *a, **k: rows.append((a, k))):
            with self.assertRaises(subprocess.TimeoutExpired):
                agent_worker._execute(config, "prompt", self.directory, 1)
        self.assertEqual([(a[0], a[1], k["worker"]) for a, k in rows], [("claude", "timeout", agent_worker.WORKER)])

    def test_a_denied_run_is_executor_failed(self):
        with self.assertRaisesRegex(RuntimeError, "EXECUTOR_FAILED"):
            self.execute((FIXTURES / "run3_denied.jsonl").read_text())   # FORBIDDEN_TOOL: the kill switch


class Waits(unittest.TestCase):
    def setUp(self):
        # U1 3g G2: Claude's waits show only while Claude is the chosen engine.
        chosen = patch.object(provider_limits, "chosen_engine", return_value="claude")
        chosen.start()
        self.addCleanup(chosen.stop)

    def tearDown(self):
        provider_limits.claude_fixed(kinds=tuple(provider_limits.CLAUDE_WAIT_FILES))

    def test_a_signed_in_probe_never_clears_signed_out(self):
        # R5: the probe reads local credentials; only a run that worked ends the wait.
        folder = tempfile.mkdtemp()
        path = Path(folder) / "claude"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        config = {"claude": str(path)}
        probe = lambda words: (lambda *a, **k: __import__("subprocess").CompletedProcess(a[0], 0, words, ""))
        with subscription():
            self.assertEqual(CLAUDE.wait(config, run=probe('{"loggedIn": false}')).kind, provider_limits.CLAUDE_SIGNED_OUT)
            self.assertEqual(provider_limits.waits()["claude"]["kind"], provider_limits.CLAUDE_SIGNED_OUT)
            self.assertIsNone(CLAUDE.wait(config, run=probe('{"loggedIn": true}')))
        self.assertEqual(provider_limits.waits()["claude"]["kind"], provider_limits.CLAUDE_SIGNED_OUT)

    def test_claude_waits_are_long_waits(self):
        for kind in (provider_limits.CLAUDE_SIGNED_OUT, provider_limits.CLAUDE_NOT_FOUND,
                     provider_limits.CLAUDE_KEY_REFUSED):
            self.assertIn(kind, daily_schedule.LONG_WAIT_KINDS)
            result = {"industries": [{"state": "COMPUTE_WAIT", "compute_wait": {
                "reason": provider_limits.CODE, "provider": {"kind": kind}}}]}
            self.assertTrue(daily_schedule.provider_wait(result))

    def test_not_found_banner_words_and_found_again_clears_it(self):
        with patch.object(setup_steps, "find_claude", return_value=None), \
                patch.object(setup_steps, "load_state", return_value={}):
            setup_steps.claude_wait(setup_steps.with_claude({"model": "m"}))
        # U1 3g R4: the Claude banners name and link to Settings' Claude section, during Setup too.
        self.assertEqual(provider_limits.banners()[0]["text"], "Sidedoor can't find Claude. Open Settings to find it again.")
        self.assertEqual(provider_limits.banners()[0]["action_href"], "/settings#connection-claude")
        self.assertEqual(provider_limits.waits(finished=True)["claude"]["banner"]["action_href"], "/settings#connection-claude")
        found = tempfile.mkdtemp()
        claude = Path(found) / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n")
        claude.chmod(0o755)
        with patch.object(setup_steps, "load_state", return_value={}):
            setup_steps.with_claude({"claude": str(claude)})
        self.assertIsNone(provider_limits.waits()["claude"])

    def test_refused_key_words_and_the_research_day_words(self):
        provider_limits.noted(provider_limits.ProviderUsageLimit("x", kind=provider_limits.CLAUDE_KEY_REFUSED))
        self.assertEqual(provider_limits.waits()["claude"]["health"], "Anthropic refused your key")
        import sqlite3
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE research(id TEXT, started_at TEXT, state TEXT)")
        daily_schedule.initialize(database, "UTC")
        import datetime as dt
        day = daily_schedule.codex_wait(database, {"timezone": "UTC", "research_hour": 6}, dt.datetime.now(dt.timezone.utc),
                                        kind=provider_limits.CLAUDE_SIGNED_OUT)
        self.assertEqual((day["state"], day["next_action"]), ("CLAUDE_SIGNED_OUT", "Sign in to Claude again"))
        codex = daily_schedule.codex_wait(database, {"timezone": "UTC", "research_hour": 6}, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(codex["state"], "CODEX_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
