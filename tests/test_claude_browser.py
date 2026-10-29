"""U1 3e: browser reads on Claude, and review item C2 (the live monitor stops a Claude session).

The streams are real Claude 2.1.301 browser runs through the guarded browser against a localhost fake
site (tests/fixtures/claude/browser_inspect.jsonl: an application inspect on Sonnet, two guard calls;
browser_webmail.jsonl: the email check), home paths replaced. Bad sessions are those streams with one
line changed the way Claude writes it (a Bash tool_use, an MCP server that failed to start)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import application_browser
import browser_guard
import browser_runtime
import engines
import model_calls
import outreach_history_browser
import setup_steps

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
CLAUDE = engines.CLAUDE
INSPECT = (FIXTURES / "browser_inspect.jsonl").read_text().splitlines(keepends=True)
WEBMAIL = (FIXTURES / "browser_webmail.jsonl").read_text().splitlines(keepends=True)


def with_bash(lines):
    """The stream with its first tool_use turned into a Bash call (a built-in tool outside the list)."""
    result = list(lines)
    for index, line in enumerate(result):
        event = json.loads(line)
        blocks = (event.get("message") or {}).get("content") or [] if event.get("type") == "assistant" else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block.update(name="Bash", input={"command": "cat ~/.ssh/id_rsa"})
                result[index] = json.dumps(event) + "\n"
                return result, index
    raise AssertionError("no tool_use")


def with_failed_server(lines):
    event = json.loads(lines[0])
    event["mcp_servers"] = [{"name": "browser", "status": "failed"}]
    return [json.dumps(event) + "\n"] + list(lines[1:])


def config(folder, operation="inspect", **extra):
    return dict({"claude": str(folder / "claude"), "engine": "claude", "_browser_operation": operation,
                 "browser_driver": {"isolated": True, "headless": True, "runtime_dir": str(folder / "runtime")}},
                **extra)


def live(lines, allowed, investigate=False):
    """The live monitor's loop (application_browser.execute) over `lines`: (counted, index it stopped at)."""
    reader = CLAUDE.trace_reader(allowed)
    counted = 0
    for index, line in enumerate(lines):
        try:
            for event in reader.feed(line):
                if event.phase is None and event.kind != "error":
                    continue
                counted += bool(application_browser.tool_event(event, False, investigate))
        except (ValueError, RuntimeError) as error:
            return counted, index, error
    return counted, None, None


class BrowserCommand(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / "schema.json").write_text('{"type": "object"}')

    def allowed(self, command):
        return command[command.index("--allowedTools") + 1].split(",")

    def test_inspect_reads_on_sonnet_with_the_codex_approval_list_and_no_arm(self):
        human = self.folder / "human.json"
        command = application_browser.command(config(self.folder, _application_operation="inspect",
                                                     _application_human_check=str(human)), self.folder)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        allowed = self.allowed(command)
        self.assertIn("mcp__browser__browser_click", allowed)
        self.assertIn("mcp__browser__browser_run_code_unsafe", allowed)
        self.assertIn("mcp__browser__wait_for_human_check", allowed)
        self.assertNotIn("mcp__browser__arm", allowed)
        # The same list Codex approves (browser_runtime.codex_options), name for name.
        with patch.object(browser_runtime, "guard_command", return_value=["/x/sidedoor", "browser-guard"]):
            codex = browser_runtime.codex_options({}, self.folder, "inspect", human_check=str(human))
        approved = [option.split(".")[3] for option in codex if option.endswith('approval_mode="approve"')]
        # ... plus browser_continue, the pieces of a long answer (Claude only, E4 b).
        self.assertEqual(sorted("mcp__browser__" + tool for tool in approved + ["browser_continue"]), sorted(allowed))
        self.assertNotIn("browser_continue", approved)
        server = json.loads((self.folder / "browser.mcp.json").read_text())["mcpServers"]["browser"]
        self.assertEqual(server["args"][server["args"].index("--operation") + 1], "inspect")
        self.assertIn("--human-check", server["args"])
        self.assertEqual(server["args"][server["args"].index("--piece-chars") + 1], "40000")
        self.assertLess(server["args"].index("--piece-chars"), server["args"].index("--"))
        self.assertIn("SIDEDOOR_HOME", server["env"])
        environment = CLAUDE.environment(command, {"HOME": "/h", "USER": "u", "ANTHROPIC_API_KEY": "k"})
        self.assertEqual(environment["MCP_TOOL_TIMEOUT"], "630000")   # the CAPTCHA wait is there
        self.assertEqual(environment["MCP_TIMEOUT"], "60000")
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_main_model_operations_and_code_free_reads(self):
        command = application_browser.command(config(self.folder, _application_operation="verify"), self.folder)
        self.assertEqual(command[command.index("--model") + 1], "opus")
        for operation in ("webmail", "support_email"):
            command = outreach_history_browser.command(config(self.folder, operation), self.folder)
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            allowed = self.allowed(command)
            self.assertNotIn("mcp__browser__browser_evaluate", allowed)
            self.assertNotIn("mcp__browser__browser_run_code_unsafe", allowed)
            self.assertNotIn("mcp__browser__wait_for_human_check", allowed)
            self.assertEqual(CLAUDE.environment(command, {})["MCP_TOOL_TIMEOUT"], "110000")
        signin = outreach_history_browser.command(config(self.folder, "support_email",
                                                         _application_human_check=str(self.folder / "s")), self.folder)
        self.assertIn("mcp__browser__wait_for_human_check", self.allowed(signin))

    def test_submit_without_its_claim_a_claim_outside_submit_and_unknown_operations_refuse(self):
        # Submit itself is wired in U1 3f (tests/test_claude_submit.py).
        for value in (config(self.folder, "submit"), config(self.folder, _application_guard_claim="/c"),
                      config(self.folder, None)):
            with self.assertRaisesRegex(NotImplementedError, "CLAUDE_BROWSER_SESSION_REFUSED"):
                CLAUDE.browser_command(value, self.folder, application=True)


class TestLaunch(unittest.TestCase):
    """Review E1: test sessions reach only this computer and use the mock keychain; users' are unchanged."""

    def test_test_sessions_carry_the_host_block_and_mock_keychain(self):
        for driver in ({"isolated": True, "headless": True}, {"isolated": True}):
            options = browser_runtime.launch_config({"browser_driver": driver}, "inspect")["browser"]["launchOptions"]
            self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost", options["args"])
            self.assertIn("--use-mock-keychain", options["args"])
            self.assertNotIn("--use-mock-keychain", options["ignoreDefaultArgs"])

    def test_the_users_real_browser_is_unchanged(self):
        import setup_steps
        with patch.object(setup_steps, "chrome_path", return_value=None):   # E7's named Chrome: test_browser_offscreen
            for allowed in (False, True):
                with patch.object(browser_runtime, "BACKGROUND_ALLOWED", allowed), \
                        patch.object(browser_runtime.sys, "platform", "darwin"):
                    value = browser_runtime.launch_config({"browser_driver": {"profile": "/p"}}, "inspect")
                    expected = json.loads(json.dumps(browser_runtime.LAUNCH_CONFIG))
                    if allowed:
                        expected["browser"]["launchOptions"]["args"] = list(browser_runtime.WINDOW_SWITCHES)
                    self.assertEqual(value, expected)
                    self.assertIn("--use-mock-keychain", value["browser"]["launchOptions"]["ignoreDefaultArgs"])

    def test_browser_jobs_take_any_result_the_trace_can_hold(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "schema.json").write_text('{"type": "object"}')
        command = outreach_history_browser.command(config(folder, "webmail"), folder)
        self.assertEqual(int(CLAUDE.environment(command, {})["MAX_MCP_OUTPUT_TOKENS"]), application_browser.MAX_TRACE)
        self.assertNotIn("MAX_MCP_OUTPUT_TOKENS", CLAUDE.environment(CLAUDE.command({"claude": "/x/claude"}), {}))


class Pieces(unittest.TestCase):
    """E4 b: the guard cuts a long answer for Claude; Codex's guard passes it whole."""

    def proxy(self, piece_chars):
        proxy = browser_guard.Proxy.__new__(browser_guard.Proxy)
        proxy.piece_chars, proxy.waiting_pieces, proxy.piece_count, proxy.audit = piece_chars, [], 0, None
        proxy.piece_page, proxy.cut_answers, proxy.piece_lock = None, 0, threading.Lock()
        proxy.idle, proxy.in_flight, proxy.call_timeout = threading.Condition(), set(), 5
        proxy.arm_binding = proxy.human_check_wait = None
        proxy.sent = []
        proxy.to_client = proxy.sent.append
        return proxy

    def page(self, lines=3000):
        return "### Page\n- Page URL: http://127.0.0.1/p\n### Snapshot\n```yaml\n" + "".join(
            '  - listitem [ref=e%d]: "Archive note %d: ledger payments"\n' % (index, index) for index in range(lines)) + "```"

    def answer(self, text):
        return {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": text}]}}

    def test_pieces_cut_at_line_ends_and_join_back(self):
        text = self.page()
        found = browser_guard.pieces(text, 1000)
        self.assertEqual("".join(found), text)
        self.assertTrue(all(len(piece) <= 1000 for piece in found))
        self.assertTrue(all(piece.endswith("\n") for piece in found[:-1]))
        self.assertEqual(browser_guard.pieces("x" * 25, 10), ["x" * 10, "x" * 10, "x" * 5])

    def test_claude_reads_the_whole_page_through_browser_continue(self):
        proxy, text = self.proxy(40000), self.page()
        first = proxy.in_pieces(self.answer(text))
        shown = [first["result"]["content"][0]["text"]]
        while "the last." not in shown[-1]:
            proxy.call_continue({"id": len(shown)})
            shown.append(proxy.sent[-1]["result"]["content"][0]["text"])
        self.assertTrue(all(len(piece) <= 40000 for piece in shown))   # under Claude's 50,000 limit
        self.assertGreater(len(shown), 2)
        # The pieces, less the guard's own note (never evidence), are the page.
        observed = [application_browser.observed_text({"content": [{"type": "text", "text": piece}]}, tool)
                    for piece, tool in zip(shown, ["browser_snapshot"] + ["browser_continue"] * len(shown))]
        self.assertNotIn("Sidedoor continuation", "".join(observed))
        self.assertIn('"Archive note 2999: ledger payments"', observed[-1])
        self.assertIn("browser_continue", proxy.own_tools()[0]["name"])
        proxy.call_continue({"id": 99})
        self.assertIn("No piece is waiting", proxy.sent[-1]["result"]["content"][0]["text"])
        self.assertTrue(application_browser.tool_event(engines.TraceEvent("completed", "tool", engines.ToolCall(
            id="c", server="browser", tool="browser_continue", args={}, result={}, status="completed", error=None)), False))

    def read_all(self, proxy, message):
        shown = [proxy.in_pieces(message)["result"]["content"][0]["text"]]
        while "the last." not in shown[-1]:
            proxy.call_continue({"id": len(shown)})
            shown.append(proxy.sent[-1]["result"]["content"][0]["text"])
        return shown

    def test_every_piece_names_its_answer_index_and_page(self):
        shown = self.read_all(self.proxy(40000), self.answer(self.page()))
        for number, piece in enumerate(shown, 1):
            self.assertIn("- Piece: %d of %d" % (number, len(shown)), piece)
            self.assertIn("- Cut answer: 1", piece)
            self.assertEqual(browser_guard.continuation_page(piece), "http://127.0.0.1/p")

    def test_a_new_long_answer_replaces_the_waiting_pieces_and_says_so(self):
        proxy = self.proxy(40000)
        proxy.in_pieces(self.answer(self.page()))
        other = self.page().replace("http://127.0.0.1/p", "http://127.0.0.1/q")
        first = proxy.in_pieces(self.answer(other))["result"]["content"][0]["text"]
        self.assertIn("unread pieces of the earlier cut answer 1 were dropped", first)
        proxy.call_continue({"id": 1})
        piece = proxy.sent[-1]["result"]["content"][0]["text"]
        self.assertIn("- Cut answer: 2", piece)
        self.assertEqual(browser_guard.continuation_page(piece), "http://127.0.0.1/q")

    def test_continue_waits_for_a_cut_answer_still_on_its_way(self):
        proxy = self.proxy(40000)
        proxy.in_flight.add(5)   # a snapshot forwarded and not yet answered
        caller = threading.Thread(target=proxy.call_continue, args=({"id": 6},))
        caller.start()
        time.sleep(0.2)
        self.assertEqual(proxy.sent, [])   # continue waits
        proxy.in_pieces(self.answer(self.page()))   # pump_child cuts it before the call counts as answered
        with proxy.idle:
            proxy.in_flight.discard(5)
            proxy.idle.notify_all()
        caller.join(5)
        self.assertIn("- Piece: 2 of", proxy.sent[-1]["result"]["content"][0]["text"])

    def test_a_webmail_absence_quote_from_piece_three_meets_the_absence_rule(self):
        import test_webmail_receipts as receipts
        req, result = receipts.request(receipts.webmail("Outlook.com")), receipts.absence()
        page = "https://outlook.live.com/mail/0/inbox"
        quote = "SYNTHETIC BROWSER OBSERVATION ONLY " + application_browser.encoded(result)
        text = ("### Page\n- Page URL: " + page + "\n- Page Title: Mail\n### Snapshot\n" + "".join(
            "  - listitem [ref=e%d]: older mail %d\n" % (index, index) for index in range(2400)) + quote + "\n")
        shown = self.read_all(self.proxy(40000), self.answer(text))
        self.assertGreaterEqual(len(shown), 3)
        holder = next(number for number, piece in enumerate(shown, 1) if quote in piece)
        self.assertGreaterEqual(holder, 3)
        events = [{"type": "item.completed", "item": {
            "type": "mcp_tool_call", "id": "browser-%d" % number, "server": "browser",
            "tool": "browser_snapshot" if number == 1 else "browser_continue", "arguments": {}, "status": "completed",
            "result": {"content": [{"type": "text", "text": piece}], "isError": False}}}
            for number, piece in enumerate(shown, 1)]
        raw = {"request_id": req["request_id"], "binding": req["binding"], "result_json": application_browser.encoded(result),
               "evidence": [{"pointer": pointer, "tool_call_id": "browser-%d" % holder, "quote": quote}
                            for pointer, _value in application_browser.leaves(result)]}
        trace = "".join(application_browser.encoded(event) + "\n" for event in events).encode()
        found = application_browser.normalize(req, raw, trace, receipts.OBSERVED, Path("request-1") / "trace.jsonl")
        # Exactly as the same quote in one whole answer (piece 1): a first inbox page is not absence.
        expected = ("UNKNOWN", False, application_browser.MAILBOX_ABSENCE_DOWNGRADE)
        self.assertEqual(expected, (found["status"], found["authoritative_absence"], found.get("absence_downgraded")))
        whole = receipts.normalize_on(req, receipts.absence(), page)
        self.assertEqual(expected, (whole["status"], whole["authoritative_absence"], whole.get("absence_downgraded")))

    def test_continue_is_a_page_read_but_there_is_no_call_limit_to_reach(self):
        # The browser sessions have no tool-call limit (owner, 2026-10-25): a counted continue cannot push a
        # Claude session over one; it touches no page, and its answer is page text, so it is an observation.
        self.assertIn("No tool-call limit", Path(application_browser.__file__).read_text())

    def test_short_answers_and_codex_pass_whole(self):
        message = self.answer(self.page())
        self.assertIs(self.proxy(10 ** 9).in_pieces(message), message)
        codex = self.proxy(None)
        self.assertEqual(codex.own_tools(), [])
        with patch.object(browser_runtime, "guard_command", return_value=["/x/sidedoor", "browser-guard", "--", "node"]):
            self.assertFalse(any("browser_continue" in option for option in browser_runtime.codex_options({}, "/tmp", "inspect")))
        self.assertNotIn("--piece-chars", browser_runtime.guard_command(
            {"browser_driver": {"isolated": True, "headless": True}}, Path(tempfile.mkdtemp()), "inspect"))


class LiveMonitor(unittest.TestCase):
    """Review C2: a Claude failure event stops the session at its line, as a refused tool stops Codex."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "schema.json").write_text('{"type": "object"}')
        self.allowed = CLAUDE.allowed_of(application_browser.command(
            config(folder, _application_operation="inspect", _application_human_check=str(folder / "h")), folder))

    def test_the_real_inspect_run_passes_and_counts_its_two_guard_calls(self):
        # Two calls, each counted when it starts and when it completes (as Codex's item.started/completed).
        self.assertEqual(live(INSPECT, self.allowed), (4, None, None))

    def test_a_bash_call_stops_the_session_at_its_line(self):
        lines, index = with_bash(INSPECT)
        counted, stopped, error = live(lines, self.allowed)
        self.assertEqual((counted, stopped, str(error)), (0, index, "APPLICATION_UNAUTHORIZED_TOOL"))

    def test_an_mcp_tool_outside_the_list_stops_the_session(self):
        lines = [line.replace("mcp__browser__browser_navigate", "mcp__browser__arm") for line in INSPECT]
        counted, stopped, error = live(lines, self.allowed)
        self.assertEqual(str(error), "APPLICATION_UNAUTHORIZED_TOOL")
        self.assertEqual(counted, 0)

    def test_a_failed_browser_server_stops_the_session_on_init(self):
        counted, stopped, error = live(with_failed_server(INSPECT), self.allowed)
        self.assertEqual((stopped, str(error)), (0, "APPLICATION_BROWSER_EXECUTOR_FAILED"))
        self.assertEqual(error.__cause__.code, CLAUDE.MCP_SERVER_FAILED)

    def test_web_fetch_and_other_builtins_are_refused_but_codex_steps_are_not(self):
        call = engines.ToolCall(id="t", server=None, tool="Read", args={}, result=None, status="in_progress", error=None)
        for event in (engines.TraceEvent("started", "web_fetch", call), engines.TraceEvent("started", "other", call)):
            with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
                application_browser.tool_event(event, False)
        # Codex "other" items (reasoning, todo lists) carry no call and are never refused.
        self.assertFalse(application_browser.tool_event(engines.CODEX.item_event({"type": "reasoning"}, "completed"), False))
        self.assertFalse(application_browser.tool_event(
            engines.TraceEvent(None, "error", text="slow", raw_kind=CLAUDE.TOOL_TIMEOUT), False))


def fake_claude(folder, lines, delay=0.05):
    """An executable named claude that reads its prompt and writes `lines` as its stream."""
    stream = folder / "stream.jsonl"
    stream.write_text("".join(lines))
    path = folder / "claude"
    path.write_text("#!%s\nimport sys, time\nsys.stdin.read()\nfor line in open(%r):\n"
                    "    sys.stdout.write(line); sys.stdout.flush(); time.sleep(%s)\n" % (sys.executable, str(stream), delay))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Sessions(unittest.TestCase):
    """application_browser.execute and outreach_history_browser.execute on a fake claude."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.directory = self.folder / "session"
        self.directory.mkdir()
        (self.directory / "schema.json").write_text('{"type": "object"}')
        model_calls.use(self.folder / "calls.sqlite3")
        self.addCleanup(model_calls.use, None)
        for target in (patch.object(browser_runtime, "require"), patch.object(browser_runtime, "end_session"),
                       patch.object(engines.ClaudeEngine, "wait", return_value=None),
                       patch.object(engines.ClaudeEngine, "api_key_mode", return_value=False)):
            target.start()
            self.addCleanup(target.stop)

    def calls(self):
        return [tuple(row) for row in sqlite3.connect(self.folder / "calls.sqlite3").execute(
            "SELECT route,outcome,worker FROM model_calls")]

    def application(self, lines):
        fake_claude(self.folder, lines)
        return application_browser.execute(config(self.folder, _application_operation="inspect",
                                                  _application_human_check=str(self.folder / "h")),
                                           "prompt", self.directory, 60)

    def test_a_clean_inspect_writes_its_answer_record_and_model_call(self):
        self.application(INSPECT)
        answer = json.loads((self.directory / "output.json").read_text())
        self.assertEqual(answer["request_id"], "req-inspect")
        record = json.loads((self.directory / "engine.json").read_text())
        self.assertEqual(record["engine"], "claude")
        self.assertIn("mcp__browser__browser_snapshot", record["allowed"])
        self.assertEqual(self.calls(), [("claude", "ok", "application_browser")])
        # The saved trace is read back by its own engine (job description reader, carry-forward).
        carried = application_browser.session_carry_forward(self.directory)
        self.assertEqual(carried["steps"], 2)
        self.assertTrue(carried["pages_opened"][0].startswith("http://127.0.0.1"))

    def test_a_bash_call_ends_the_live_session_before_the_run_finishes(self):
        lines, index = with_bash(INSPECT)
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            self.application(lines)
        self.assertFalse((self.directory / "output.json").exists())
        self.assertEqual(self.calls(), [("claude", "refused", "application_browser")])

    def test_history_worker_judges_the_run_and_its_readers_use_the_engine(self):
        fake_claude(self.folder, WEBMAIL)
        outreach_history_browser.execute(config(self.folder, "webmail"), "prompt", self.directory, 60)
        answer = json.loads((self.directory / "output.json").read_text())
        self.assertEqual(answer["signed_in_account"], "alex.tester@example.com")
        engine = engines.for_run(self.directory)
        text = (self.directory / "trace.jsonl").read_text()
        self.assertEqual(setup_steps.email_check_trace(text, engine), [])
        items = list(outreach_history_browser.completed_items(text.encode(), engine))
        self.assertEqual([(item["server"], item["tool"]) for item in items],
                         [("browser", "browser_navigate"), ("browser", "browser_snapshot")])
        self.assertIn("alex.tester@example.com", "".join(outreach_history_browser._observed_texts(text.encode(), engine).values()))
        bad, _ = with_bash(WEBMAIL)
        with self.assertRaisesRegex(ValueError, "HISTORY_UNAUTHORIZED_TOOL_TRACE"):
            list(outreach_history_browser.completed_items("".join(bad).encode(), engine))
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            setup_steps.email_check_trace("".join(bad), engine)

    def test_history_worker_names_a_forbidden_tool_as_codex_does(self):
        fake_claude(self.folder, with_bash(WEBMAIL)[0])
        with self.assertRaisesRegex(ValueError, "HISTORY_UNAUTHORIZED_TOOL_TRACE") as caught:
            outreach_history_browser.execute(config(self.folder, "webmail"), "prompt", self.directory, 60)
        self.assertEqual(caught.exception.__cause__.code, CLAUDE.FORBIDDEN_TOOL)

    def test_a5_history_worker_stopped_at_its_time_limit_writes_its_model_call(self):
        import subprocess
        fake_claude(self.folder, WEBMAIL[:-1], delay=5)   # one line, then still running at the limit
        with self.assertRaises(subprocess.TimeoutExpired):
            outreach_history_browser.execute(config(self.folder, "webmail"), "prompt", self.directory, 1)
        self.assertEqual(self.calls(), [("claude", "timeout", "webmail")])
        seconds = sqlite3.connect(self.folder / "calls.sqlite3").execute("SELECT seconds FROM model_calls").fetchone()[0]
        self.assertGreaterEqual(seconds, 1)   # R4

    def test_r3_application_worker_stopped_at_its_time_limit_records_timeout(self):
        fake_claude(self.folder, INSPECT[:-1], delay=5)
        with self.assertRaisesRegex(TimeoutError, "APPLICATION_BROWSER_TIMEOUT"):
            application_browser.execute(config(self.folder, _application_operation="inspect",
                                               _application_human_check=str(self.folder / "h")),
                                        "prompt", self.directory, 1)
        self.assertEqual(self.calls(), [("claude", "timeout", "application_browser")])

    def test_history_worker_fails_a_run_with_a_failed_server(self):
        fake_claude(self.folder, with_failed_server(WEBMAIL))
        with self.assertRaisesRegex(RuntimeError, "HISTORY_BROWSER_EXECUTOR_FAILED") as caught:
            outreach_history_browser.execute(config(self.folder, "webmail"), "prompt", self.directory, 60)
        self.assertEqual(caught.exception.__cause__.code, CLAUDE.MCP_SERVER_FAILED)
        self.assertEqual(self.calls(), [("claude", "refused", "webmail")])


if __name__ == "__main__":
    unittest.main()
