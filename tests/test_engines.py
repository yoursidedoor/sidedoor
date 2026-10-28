"""U1 phase 2: the engine layer with Codex as its one engine changes nothing.

The REFERENCE_* functions below are verbatim copies of the pre-change builders and readers (main
baaf09c: agent_worker._command, reply_worker._command, outreach_history_browser.command,
application_browser.command, the call sites' environments, and the trace readers). Each test runs the
engine-layer code and its reference on the same input and asserts identical results.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_worker
import application_browser
import browser_guard
import browser_runtime
import engines
import job_description
import model_calls
import openai_auth
import outreach_history_browser
import provider_pacing
import reply_tools
import reply_worker
import setup_steps
import text_model
import user_profile


# --- pre-change builders (baaf09c), verbatim ---

def REFERENCE_agent_command(config, directory):
    command = [config["codex"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"], "--json"]
    for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
                    'web_search="live"', "features.shell_tool=false", "features.apps=false",
                    "features.skills=false", "features.apply_patch_freeform=false", "features.unified_exec=false",
                    "features.multi_agent=false", "mcp_servers={}"):
        command.extend(["-c", setting])
    return openai_auth.command(command + ["--output-schema", str(directory / "schema.json"), "--output-last-message",
                                          str(directory / "output.json"), "--color", "never", "-"])


def REFERENCE_history_command(config, directory):
    result = [config["codex"], "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
              "--sandbox", "read-only", "--model", config.get("browser_read_model") or config["model"], "--json"]
    for setting in ('approval_policy="never"', 'forced_login_method="chatgpt"',
                    'web_search="disabled"', "features.apps=false",
                    "features.shell_tool=false", "features.unified_exec=false",
                    "features.apply_patch_freeform=false", "features.multi_agent=false"):
        result.extend(["-c", setting])
    operation = config.get("_browser_operation")
    if operation:
        for setting in browser_runtime.codex_options(config, directory, operation,
                                                     claim=config.get("_application_guard_claim"),
                                                     human_check=config.get("_application_human_check")):
            result.extend(["-c", setting])
    else:
        result.extend(["-c", "mcp_servers={}"])
    return openai_auth.command(result + ["--output-schema", str(directory / "schema.json"), "--output-last-message",
                                         str(directory / "output.json"), "-"])


def REFERENCE_application_command(config, directory):
    config = dict(config, _browser_operation=config.get("_application_operation") or "inspect")
    result = REFERENCE_history_command(config, directory)
    if config.get("_application_operation") in {"submit", "verify", "reconcile"} or config.get("_application_guard_claim"):
        result[result.index("--model") + 1] = config["model"]
    for option in ('model_provider="openai"', "features.skills=false"):
        result[-1:-1] = ["-c", option]
    return result


def REFERENCE_environment():
    allowed = {"HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    return {key: value for key, value in os.environ.items() if key in allowed}


# --- pre-change readers (baaf09c), verbatim loops ---

def REFERENCE_tool_item(item, submit, investigate=False):
    kind = item.get("type")
    if kind in {"command_execution", "web_search", "web_search_call"}:
        raise ValueError("APPLICATION_UNAUTHORIZED_TOOL")
    if kind != "mcp_tool_call":
        return False
    if item.get("server") != browser_runtime.SERVER:
        raise ValueError("APPLICATION_UNAUTHORIZED_TOOL")
    tool = item.get("tool")
    if tool == browser_guard.ARM_TOOL and submit:
        return True
    if tool == browser_guard.HUMAN_CHECK_TOOL:
        return False
    if tool not in browser_guard.TOOLS:
        raise ValueError("APPLICATION_UNAUTHORIZED_TOOL")
    arguments = item.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool == "browser_evaluate":
        raise ValueError("APPLICATION_UNSUPPORTED_BROWSER_CODE")
    if tool in application_browser.CODE_TOOLS:
        code = arguments.get(application_browser.CODE_TOOLS[tool], "")
        if not isinstance(code, str) or application_browser.unsupported_browser_code(code):
            raise ValueError("APPLICATION_UNSUPPORTED_BROWSER_CODE")
        if investigate and application_browser.investigation_writes(code):
            raise ValueError("APPLICATION_INVESTIGATION_READ_ONLY")
    if investigate and tool in application_browser.INVESTIGATION_WRITE_TOOLS:
        raise ValueError("APPLICATION_INVESTIGATION_READ_ONLY")
    if investigate:
        for value in arguments.values():
            for url in re.findall(r"https?://[^\s\"'`<>]+", value if isinstance(value, str) else ""):
                application_browser.application_destination.public_url(url)
    return True


def REFERENCE_live(lines, submit, investigate):
    """The live kill switch's loop: (counted calls, the line index and error that killed it)."""
    counted = 0
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
            if event.get("type") not in {"item.started", "item.completed"}:
                continue
            counted += bool(REFERENCE_tool_item(event.get("item", {}), submit, investigate))
        except Exception as error:
            return counted, index, str(error)
    return counted, None, None


def NEW_live(lines, submit, investigate):
    counted = 0
    for index, line in enumerate(lines):
        try:
            event = engines.engine().parse_line(line)
            if event.phase is None:
                continue
            counted += bool(application_browser.tool_event(event, submit, investigate))
        except Exception as error:
            return counted, index, str(error)
    return counted, None, None


def REFERENCE_observations(trace, operation):
    """normalize's observation loop, verbatim from baaf09c (only the names it imports are qualified):
    (calls, completed ids, pages, observations, failed observations)."""
    observations, failed_observations, calls, sequence, pages = {}, {}, set(), [], {}
    for line in trace.splitlines():
        event = json.loads(line)
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item", {})
        if not REFERENCE_tool_item(item, operation == "submit", operation == "investigate"):
            continue
        identifier = item["id"]
        calls.add(identifier)
        if event["type"] != "item.completed":
            continue
        sequence.append(item)
        response = item.get("result", {})
        # Only the browser's own tools observe a page (E7.T1): the arm answer and the CAPTCHA wait are
        # never evidence, as live (they were other servers there).
        if item.get("tool") not in browser_guard.TOOLS or not isinstance(response, dict):
            continue
        shown = application_browser.PAGE_URL.findall(application_browser.result_text(response))
        if shown:
            pages[identifier] = shown[-1]
        if item.get("status") == "failed" or response.get("isError"):
            failed_observations[identifier] = application_browser.observed_text(response, item.get("tool"))
        elif item.get("status") == "completed" and not item.get("error"):
            observations[identifier] = application_browser.observed_text(response, item.get("tool"))
    return calls, [item["id"] for item in sequence], pages, observations, failed_observations


def REFERENCE_web_searches(trace):
    events = []
    for line_number, line in enumerate(trace.splitlines(), 1):
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("INVALID_TOOL_TRACE")
        item = event.get("item", {})
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") in {"web_search", "web_search_call"}
                and item.get("status") in {None, "completed"}):
            events.append(line_number)
    return events


def REFERENCE_email_check(trace):
    clicks = []
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or event.get("type") != "item.completed":
            continue
        if not REFERENCE_tool_item(item, False):
            continue
        if item.get("tool") in setup_steps.EMAIL_CHECK_WRITES:
            raise ValueError("EMAIL_CHECK_NOT_READ_ONLY")
        if item.get("tool") == "browser_click":
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            clicks.append({"target": arguments.get("target"), "element": str(arguments.get("element") or "")[:200]})
    return clicks


def outcome(function, *arguments):
    try:
        return "ok", function(*arguments)
    except Exception as error:
        return type(error).__name__, str(error)


def step(kind, identifier, tool=None, phase="item.completed", **fields):
    item = {"id": identifier, "type": kind}
    if tool:
        item.update(server=browser_runtime.SERVER, tool=tool)
    item.update(fields)
    return json.dumps({"type": phase, "item": item})


def page(text, url="https://jobs.example/role"):
    return {"content": [{"type": "text", "text": "### Page\n- Page URL: %s\n%s" % (url, text)}]}


# A trace with every shape the readers distinguish (Codex --json lines).
TRACE = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t"}),
    json.dumps({"type": "turn.started"}),
    step("reasoning", "r0", text="thinking"),
    step("mcp_tool_call", "c1", "browser_navigate", "item.started", arguments={"url": "https://jobs.example/role"},
         status="in_progress"),
    step("mcp_tool_call", "c1", "browser_navigate", arguments={"url": "https://jobs.example/role"},
         status="completed", result=page("Senior Engineer")),
    step("mcp_tool_call", "c2", "browser_snapshot", status="failed", result=page("Error: timeout")),
    step("mcp_tool_call", "c3", "browser_snapshot", status="completed", result=dict(page("Oops"), isError=True)),
    step("mcp_tool_call", "c4", "browser_snapshot", status="completed", error={"message": "x"}, result=page("half")),
    step("mcp_tool_call", "c5", "browser_snapshot", status="completed"),
    step("mcp_tool_call", "c6", "browser_snapshot", status="completed", result=None),
    step("mcp_tool_call", "c7", "browser_click", arguments={"target": "e3", "element": "Apply"}, status="completed",
         result=page("clicked")),
    step("mcp_tool_call", "c8", browser_guard.HUMAN_CHECK_TOOL, status="completed", result=page("waited")),
    step("mcp_tool_call", "c9", "browser_type", arguments="not an object", status="completed", result=page("typed")),
    step("agent_message", "m1", text="Opened the posting"),
    json.dumps({"type": "item.updated", "item": {"id": "todo", "type": "todo_list"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
])


class CommandParityTests(unittest.TestCase):
    """(a) Every call site's argv and environment equal the pre-change builders' for each config."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        key = self.directory / "openrouter.key"
        key.write_text("or-test-key\n")
        self.base = {"codex": "/Applications/Sidedoor.app/codex", "model": "gpt-6", "browser_read_model": "gpt-6-sol"}
        self.text_model = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(key)}}
        variables = {"HOME": "/Users/kyle", "USER": "kyle", "LOGNAME": "kyle", "PATH": "/usr/bin:/bin",
                     "CODEX_HOME": "/Users/kyle/.codex", "TMPDIR": "/var/tmp/", "LANG": "en_US.UTF-8",
                     "OPENAI_API_KEY": "must-not-pass", "SIDEDOOR_HOME": os.environ["SIDEDOOR_HOME"]}
        patcher = patch.dict(os.environ, variables, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def modes(self):
        """ChatGPT sign-in, then API-key mode (the saved key goes in as CODEX_API_KEY)."""
        yield "chatgpt"
        state = user_profile.home() / openai_auth.STATE_FILE
        state.write_text(json.dumps({"codex_auth": openai_auth.API_KEY}))
        try:
            with patch.object(openai_auth, "saved_key", return_value="sk-test"):
                self.assertTrue(openai_auth.api_key_mode())
                yield "api_key"
        finally:
            state.unlink()

    def configs(self):
        tools = {"root": str(self.directory), "task_id": "task-1"}
        return [("plain", dict(self.base)),
                ("no read model", {"codex": "/opt/homebrew/bin/codex", "model": "gpt-6"}),
                ("openrouter", dict(self.base, **self.text_model)),
                ("reply tools", dict(self.base, _reply_tools=tools)),
                ("reply tools + openrouter", dict(self.base, _reply_tools=tools, **self.text_model))]

    def test_investigation_and_history_commands_and_environments(self):
        for mode in self.modes():
            for name, config in self.configs():
                with self.subTest(mode=mode, config=name):
                    # The reply worker's codex command is gone (2026-10-27: text workers are direct calls).
                    argv = agent_worker._command(config, self.directory)
                    self.assertEqual(argv, REFERENCE_agent_command(config, self.directory))
                    self.assertEqual(engines.CODEX.environment(argv, engines.CODEX.base_environment()),
                                     openai_auth.environment(argv, REFERENCE_environment()))
                    for history in (config, dict(config, _browser_operation="history_search")):
                        argv = outreach_history_browser.command(history, self.directory)
                        self.assertEqual(argv, REFERENCE_history_command(history, self.directory))
                        self.assertEqual(engines.CODEX.environment(argv, reply_worker._environment()),
                                         openai_auth.environment(argv, REFERENCE_environment()))
        self.assertNotIn("USER", reply_worker._environment())   # Codex keeps its list (phase 3 adds USER for Claude)

    def test_application_browser_commands_for_every_operation(self):
        claim, human = str(self.directory / "claim.json"), str(self.directory / "human-check.json")
        for mode in self.modes():
            for operation in (None, "inspect", "investigate", "submit", "verify", "reconcile", "history_search"):
                for extra in ({}, {"_application_human_check": human},
                              {"_application_human_check": human, "_application_guard_claim": claim}):
                    config = dict(self.base, _application_operation=operation, **extra)
                    with self.subTest(mode=mode, operation=operation, extra=sorted(extra)):
                        argv = application_browser.command(config, self.directory)
                        self.assertEqual(argv, REFERENCE_application_command(config, self.directory))
                        self.assertEqual(engines.CODEX.environment(argv, reply_worker._environment()),
                                         openai_auth.environment(argv, REFERENCE_environment()))

    def test_provider_key_and_usage_are_the_old_values(self):
        for argv in (None, [], ["codex"], ["codex", "-c", openai_auth.OPENROUTER_PROVIDER]):
            self.assertEqual(provider_pacing.provider_of(argv),
                             "openrouter" if argv and openai_auth.OPENROUTER_PROVIDER in argv else "openai")
        for stderr, total in ((None, None), ("", None), ("tokens used\n15,582\n", 15582),
                              ("tokens used: 12\nlater\ntokens used 3", 3)):
            self.assertEqual(model_calls.codex_tokens(stderr), total)
            self.assertEqual(engines.CODEX.usage(stderr).get("total_tokens"), total)


class ReaderParityTests(unittest.TestCase):
    """(b) The trace readers give the pre-change results on the same Codex traces."""

    def traces(self):
        yield "synthetic", TRACE
        lines = TRACE.split("\n")
        for kind in ("command_execution", "web_search", "web_search_call"):   # killed mid-stream
            yield kind, "\n".join(lines[:6] + [step(kind, "x", phase="item.started", status="in_progress")] + lines[6:])
        yield "id-less call", "\n".join(lines[:6] + [json.dumps({"type": "item.started", "item": {
            "type": "mcp_tool_call", "server": browser_runtime.SERVER, "tool": "browser_snapshot"}})] + lines[6:])
        yield "unknown item types", "\n".join(lines[:6] + [step(kind, "u") for kind in ("tool", "shell", "message", "malformed", "other")]
                                               + [json.dumps({"type": "item.completed", "item": {"id": "n"}})] + lines[6:])
        yield "unknown server", "\n".join(lines[:6] + [json.dumps({"type": "item.completed", "item": {
            "id": "s", "type": "mcp_tool_call", "server": "computer-use", "tool": "browser_snapshot"}})] + lines[6:])
        for path in sorted(Path(__file__).parent.glob("fixtures/application-browser/request-*/trace.jsonl")):
            yield path.parent.name, path.read_text()

    def test_live_kill_switch_and_observations(self):
        for name, trace in self.traces():
            lines = trace.splitlines(keepends=True)
            for submit in (False, True):
                for investigate in (False, True):
                    with self.subTest(trace=name, submit=submit, investigate=investigate):
                        self.assertEqual(NEW_live(lines, submit, investigate), REFERENCE_live(lines, submit, investigate))
            for operation in ("inspect", "submit", "investigate", "verify"):
                with self.subTest(trace=name, operation=operation):
                    expected = outcome(REFERENCE_observations, trace, operation)
                    observed = outcome(self.observations, trace, operation)
                    self.assertEqual(observed, expected)

    @staticmethod
    def observations(trace, operation):
        """normalize's loop as it runs now (application_browser.normalize, the lines after `pages = {}`)."""
        observations, failed_observations, calls, sequence, pages = {}, {}, set(), [], {}
        engine = engines.engine()
        for line in trace.splitlines():
            event = engine.parse_line(line)
            if event.phase is None:
                continue
            if not application_browser.tool_event(event, operation == "submit", operation == "investigate"):
                continue
            call = event.call
            if not call.has_id:
                raise KeyError("id")
            identifier = call.id
            calls.add(identifier)
            if event.phase != "completed":
                continue
            sequence.append(call)
            response = call.result
            if call.tool not in browser_guard.TOOLS or not isinstance(response, dict):
                continue
            shown = application_browser.PAGE_URL.findall(application_browser.result_text(response))
            if shown:
                pages[call.id] = shown[-1]
            if call.is_error:
                failed_observations[call.id] = application_browser.observed_text(response, call.tool)
            elif call.is_error is False:
                observations[call.id] = application_browser.observed_text(response, call.tool)
        return calls, [call.id for call in sequence], pages, observations, failed_observations

    def test_an_id_less_counted_call_raises_key_error_old_and_new(self):
        trace = dict(self.traces())["id-less call"]
        for operation in ("inspect", "submit"):
            self.assertEqual(outcome(REFERENCE_observations, trace, operation), ("KeyError", "'id'"))
            self.assertEqual(outcome(self.observations, trace, operation), ("KeyError", "'id'"))

    def test_unknown_item_types_are_other_with_the_raw_type_kept(self):
        for raw in ("tool", "shell", "message", "malformed", "web_search_result", "todo_list"):
            event = engines.CODEX.item_event({"id": "u", "type": raw}, "completed")
            self.assertEqual((event.kind, event.raw_kind, event.call), ("other", raw, None))
            self.assertIs(application_browser.tool_event(event, True), False)
        event = engines.CODEX.item_event({"id": "u"}, "completed")
        self.assertEqual((event.kind, event.raw_kind), ("other", None))

    def test_the_normalize_loop_in_this_test_is_the_one_normalize_runs(self):
        import inspect
        source = inspect.getsource(application_browser.normalize)
        for line in ("for event in (event for line in trace.splitlines() for event in reader.feed(line)):",
                     "if event.phase is None:", "if call.is_error:",
                     "if not call.has_id:", 'raise KeyError("id")', "identifier = call.id", "sequence.append(call)",
                     "elif call.is_error is False:", 'if not tool_event(event, operation == "submit", operation == "investigate"):'):
            self.assertIn(line, source)

    def test_web_searches_email_check_and_carry_forward(self):
        searches = "\n".join([step("web_search", "w1", status="completed"), step("web_search_call", "w2"),
                              step("web_search", "w3", status="failed"),
                              step("web_search", "w4", phase="item.started", status="in_progress")])
        for name, trace in list(self.traces()) + [("searches", searches)]:
            with self.subTest(trace=name):
                self.assertEqual(engines.CODEX.read_trace(trace).web_searches, REFERENCE_web_searches(trace))
                self.assertEqual(outcome(lambda text: setup_steps.email_check_trace(text, engines.CODEX), trace), outcome(REFERENCE_email_check, trace))
        record = engines.CODEX.read_trace(TRACE)
        self.assertEqual([call.id for call in record.tool_calls], ["c%d" % n for n in range(1, 10)])
        self.assertEqual(record.text, ["Opened the posting"])
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "trace.jsonl").write_text(TRACE)
            carried = application_browser.session_carry_forward(Path(folder))
        self.assertEqual(carried["steps"], 9)
        self.assertEqual(carried["pages_opened"], ["https://jobs.example/role"])
        self.assertEqual(carried["last_notes"], ["Opened the posting"])
        self.assertEqual(carried["last_step"], "browser_type {}")

    def test_saved_posting_references_still_resolve(self):
        """job_description: a saved trace.jsonl#<item id> reference re-extracts from an existing trace."""
        text = 'Browser tab: Role URL: "https://jobs.example/role"\n' + "\n".join(
            "%d AXStaticText Requirement %d" % (n, n) for n in range(30))
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / job_description.BROWSER_DIRECTORY / "request-abc"
            folder.mkdir(parents=True)
            (folder / "request.json").write_text(json.dumps({"payload": {"job": {
                "job_key": "job-1", "payload": json.dumps({"url": "https://jobs.example/role"})}}}))
            (folder / "trace.jsonl").write_text("\n".join([
                step("mcp_tool_call", "item_7", "browser_snapshot", "item.started", status="in_progress"),
                step("mcp_tool_call", "item_7", "browser_snapshot", status="completed",
                     result={"content": [{"type": "text", "text": text}]})]) + "\n")
            found = job_description.job_description(None, root, "job-1", "https://jobs.example/role")
            self.assertEqual(found["state"], "BROWSER_READ")
            self.assertEqual(found["source"], job_description.BROWSER_DIRECTORY + "/request-abc/trace.jsonl#item_7")
            self.assertIn("Requirement 29", found["text"])
            self.assertEqual(job_description.load_recorded(root, found), found["text"])


@unittest.skipIf(os.name == "nt", "POSIX-only (a real child that prints its trace)")
class KillSwitchTests(unittest.TestCase):
    def test_a_forbidden_tool_mid_stream_ends_the_session_at_once(self):
        """Allowed browser calls, then browser_evaluate: execute raises at that event, while the
        worker is still running (it sleeps 60 s), as before the engine layer."""
        lines = [step("mcp_tool_call", "c1", "browser_navigate", "item.started", arguments={"url": "https://a.example"}),
                 step("mcp_tool_call", "c1", "browser_navigate", arguments={"url": "https://a.example"}, status="completed"),
                 step("mcp_tool_call", "c2", "browser_evaluate", "item.started", arguments={"function": "() => 1"})]
        self.assertEqual(REFERENCE_live([line + "\n" for line in lines], False, False)[1:],
                         (2, "APPLICATION_UNSUPPORTED_BROWSER_CODE"))
        script = "import sys, time\nfor line in %r:\n    print(line, flush=True)\n    time.sleep(0.3)\ntime.sleep(60)" % lines
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as root, \
                patch.object(application_browser.browser_runtime, "require"), \
                patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                patch.object(application_browser, "command", return_value=[sys.executable, "-c", script]), \
                patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                self.assertRaisesRegex(ValueError, "APPLICATION_UNSUPPORTED_BROWSER_CODE"):
            application_browser.execute({}, "prompt", Path(root), 30)
        self.assertLess(time.monotonic() - started, 15)


if __name__ == "__main__":
    unittest.main()


class RunRecordTests(unittest.TestCase):
    """U1 3a: each run records the engine that wrote it; readers ask for_run, and no record means Codex."""

    def test_record_and_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            run = folder / "request-x"
            run.mkdir()
            self.assertIs(engines.for_run(run), engines.CODEX)                  # an old folder: Codex
            self.assertIs(engines.for_run(run / "trace.jsonl"), engines.CODEX)
            engines.record_run(run, engines.CODEX)
            self.assertEqual(json.loads((run / "engine.json").read_text()), {"engine": "codex"})
            self.assertIs(engines.for_run(run / "stderr.log"), engines.CODEX)  # a file asks its folder
            log = folder / "worker.log"
            log.write_text("")
            engines.record_run(log, engines.CODEX)                              # a shared folder: a sidecar
            self.assertEqual(json.loads((folder / "worker.log.engine.json").read_text()), {"engine": "codex"})
            self.assertFalse((folder / "engine.json").exists())
            for bad in ("{", "[]", '{"engine": "nope"}', '{"engine": ["codex"]}'):
                (run / "engine.json").write_text(bad)
                with self.assertRaises(engines.EngineRecordError):
                    engines.for_run(run)

    def test_launchers_pass_config_and_pacing_key(self):
        self.assertEqual(engines.CODEX.pacing_provider, "openai")
        self.assertIs(engines.for_command(["codex", "exec"]), engines.CODEX)
        self.assertIs(engines.for_command(None), engines.CODEX)
        self.assertEqual(engines.CODEX.provider_key(None), engines.CODEX.pacing_provider)

    def test_record_is_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            engines.record_run(run, engines.CODEX)
            with patch.object(engines.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    engines.record_run(run, engines.CODEX)
            self.assertEqual(sorted(path.name for path in run.iterdir()), ["engine.json"])   # no partial, no temp
            self.assertIs(engines.for_run(run), engines.CODEX)

    def test_carry_forward_survives_a_broken_record(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "trace.jsonl").write_text(TRACE)
            (run / "output.json").write_text(json.dumps({"result_json": json.dumps({"note": "mine"})}))
            (run / "engine.json").write_text("{")
            carried = application_browser.session_carry_forward(run)
        self.assertEqual(carried["own_report"], {"note": "mine"})
        self.assertEqual((carried["steps"], carried["pages_opened"], carried["last_notes"]), (0, [], []))

    def test_reset_at(self):
        import provider_limits
        message = "You've hit your usage limit. Try again at Oct 27th, 2026 3:00 PM."
        self.assertEqual(engines.CODEX.reset_at(message), provider_limits.reset_time(message))
        stated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(hours=2)
        limit = provider_limits.ProviderUsageLimit("rate limited", reset_at=stated)
        self.assertEqual(limit.resume_at, stated)
        self.assertEqual(limit.stated_reset, stated.isoformat())

    def test_failures_page_unknown_engine_shows_only_the_note(self):
        import failures_page
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "trace.jsonl").write_text(TRACE + "\n")
            (run / "engine.json").write_text('{"engine": "later"}')
            shown = failures_page._file(run / "trace.jsonl", [])
            self.assertEqual(len(shown["lines"]), 1)
            self.assertIn(failures_page.TRACE_NOTE, str(shown["lines"][0]))

