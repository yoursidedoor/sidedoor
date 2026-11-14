"""E7.T1 option A: every browser job on the guarded @playwright/mcp route (owner, 2026-10-22).

Offline only. The guard (`sidedoor browser-guard`, browser_guard.py) runs for real; behind it is
either the fake @playwright/mcp (tests/fake_playwright_mcp.py) or, when this machine has Node,
@playwright/mcp 0.0.83 and Google Chrome, the real server driving headless Chrome with an in-memory
profile against pages served from a temporary directory on 127.0.0.1 (tests/fixtures/browser).
Nothing is downloaded, no real site is opened, nobody signs in, and the user's Chrome profile and
Sidedoor's are never used. A scripted "model" stands in for Codex and writes the trace Codex would.

Worked examples (BACKLOG E7.T1 brief):
1. upload through the file chooser, arm once before Submit, a second arm denied
   (RealBrowserTests.test_1_..., ArmThroughTheGuardTests)
2. rule 1 refuses Connect / Message / Follow with live's code (RealBrowserTests.test_2_..., GuardTests)
3. a human check stops the session at once: no wait tool, no window (tests/test_stop_at_once_e16_t1.py)
4. frozen: the server is `[sys.executable, "browser-guard", ...]`, never a .py path (FrozenCommandTests)
5. a 40-field form finishes under 70 calls with browser_fill_form (RealBrowserTests.test_5_...)
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import portable
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import engines  # noqa: E402
import linkedin_contacts  # noqa: E402
import outreach_history_browser  # noqa: E402
import setup_steps  # noqa: E402
from browser_fixtures import Client, fake_runtime, real_runtime, serve  # noqa: E402
from manual_application_browser import Fixture, event, request_at  # noqa: E402

PAGES = Path(__file__).resolve().parent / "fixtures" / "browser"
LINKEDIN_SNAPSHOT = """- main [ref=e1]:
  - heading "Jane Doe" [level=1] [ref=e2]
  - button "Invite Jane Doe to connect" [ref=e3] [cursor=pointer]: Connect
  - button "Message" [ref=e4] [cursor=pointer]
  - button "Follow" [ref=e5] [cursor=pointer]
  - button "Pending, click to withdraw invitation sent to Jane Doe" [ref=e6] [cursor=pointer]: Pending
  - link "Connect" [ref=e7] [cursor=pointer]
  - link "Jane Doe" [ref=e8] [cursor=pointer]
  - button "Compose message" [ref=e9] [cursor=pointer]
  - link "Ada and 4 other connections follow this page" [ref=e10] [cursor=pointer]
  - link "Ada Lovelace · 2nd Follow for daily notes on robotics" [ref=e11] [cursor=pointer]
"""
CONTACT_REFS = ("e3", "e4", "e5", "e6", "e7")
BENIGN_REFS = ("e8", "e9", "e10", "e11")
CAPTCHA_SNAPSHOT = '- heading "Before you apply" [level=1] [ref=e1]\n- checkbox "I am human" [ref=e2]\n'


class GuardProcess:
    """The real `sidedoor browser-guard` subprocess in front of the fake @playwright/mcp."""

    def __init__(self, testcase, operation, snapshot, claim=None, human_check=None):
        self.directory = Path(tempfile.mkdtemp(prefix="guard-"))
        testcase.addCleanup(shutil.rmtree, self.directory, True)
        self.snapshot, self.calls = self.directory / "snapshot.yaml", self.directory / "calls.jsonl"
        self.snapshot.write_text(snapshot, encoding="utf-8")   # the fake reads UTF-8, as Node writes it
        self.calls.write_text("")
        self.audit = self.directory / "guard.jsonl"
        argv = browser_runtime.sidedoor_command() + ["browser-guard", "--operation", operation, "--audit", str(self.audit)]
        argv += ["--claim", str(claim)] if claim else []
        argv += ["--human-check", str(human_check)] if human_check else []
        self.client = Client(argv + ["--"] + fake_runtime(self.snapshot, self.calls), cwd=self.directory)
        testcase.addCleanup(self.client.close)

    def forwarded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def decisions(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]


class GuardTests(unittest.TestCase):
    """Worked example 2 (offline half) and the guard's tool listing."""

    def test_rule_1_refuses_contact_controls_before_the_click_with_the_live_code(self):
        guard = GuardProcess(self, "linkedin", LINKEDIN_SNAPSHOT)
        for ref in CONTACT_REFS:
            error, text = guard.client.call("browser_click", {"element": ref, "target": ref})
            self.assertTrue(error, ref)
            self.assertTrue(text.startswith("REFUSED LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED:"), text)
        for ref in BENIGN_REFS:
            error, text = guard.client.call("browser_click", {"element": ref, "target": ref})
            self.assertFalse(error, (ref, text))
        # Only the benign clicks reached the "browser"; the guard's own snapshots are not calls.
        self.assertEqual([call["arguments"]["target"] for call in guard.forwarded()], list(BENIGN_REFS))
        refused = [entry for entry in guard.decisions() if entry["decision"] == "REFUSED"]
        self.assertEqual([entry["code"] for entry in refused], [browser_guard.CONTACT_CONTROL_CODE] * len(CONTACT_REFS))
        # The finder's run summary counts them (linkedin_contacts.guard_refusals).
        self.assertEqual(len(linkedin_contacts.guard_refusals(guard.directory)), len(CONTACT_REFS))

    def test_unlabelled_clicks_and_other_operations_are_not_judged(self):
        guard = GuardProcess(self, "linkedin", LINKEDIN_SNAPSHOT)
        for target in ("text=Connect", "e999"):   # a selector, a ref the snapshot does not show: as live
            self.assertFalse(guard.client.call("browser_click", {"element": "x", "target": target})[0], target)
        for operation in ("inspect", "submit", "verify", "reconcile", "investigate", "webmail"):
            with self.subTest(operation):
                guard = GuardProcess(self, operation, LINKEDIN_SNAPSHOT)
                self.assertFalse(guard.client.call("browser_click", {"element": "Connect", "target": "e3"})[0])
                self.assertEqual(len(guard.forwarded()), 1)

    def test_tools_list_is_every_browser_tool_and_never_the_retired_captcha_wait(self):
        guard = GuardProcess(self, "inspect", CAPTCHA_SNAPSHOT)
        names = {tool["name"] for tool in guard.client.request("tools/list")["tools"]}
        self.assertEqual(names, set(browser_guard.TOOLS))
        # E16.T1: even given the old wait context, an application session is offered no wait tool
        # (tests/test_stop_at_once_e16_t1.py covers the refusal of a call to it).
        context = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, context, True)
        (context / "human-check.json").write_text(json.dumps({"root": str(context), "request_id": "r", "directory": str(context),
                                                              "job_key": "k", "operation": "inspect"}))
        guard = GuardProcess(self, "inspect", CAPTCHA_SNAPSHOT, human_check=context / "human-check.json")
        names = {tool["name"] for tool in guard.client.request("tools/list")["tools"]}
        self.assertEqual(names, set(browser_guard.TOOLS))
        # An arm call to a server that holds no claim is refused by the guard and never reaches the
        # browser (a read session can never arm; E15.T4 Q5: only the tools a session holds are forwarded).
        guard.client.call("arm", {"observed_form_revision": "x"})
        self.assertEqual([call["tool"] for call in guard.forwarded()], [])


def submit_fixture(testcase):
    root = Path(tempfile.mkdtemp(prefix="e7t1-submit-"))
    testcase.addCleanup(shutil.rmtree, root, True)
    fixture = Fixture(root / "fixture")
    testcase.addCleanup(fixture.database.close)
    return fixture, fixture.prepare_submit()


def confirmed_output(directory, request, client, confirmation):
    """Codex's last message for a submit whose page confirmed it, quoting the final snapshot."""
    item = client.trace[-1]["item"]["id"]
    result = {"acknowledged": True, "confirmation_text": confirmation}
    evidence = [{"pointer": pointer, "tool_call_id": item, "quote": confirmation}
                for pointer in ("/acknowledged", "/confirmation_text")]
    (directory / "output.json").write_text(json.dumps({"request_id": request["request_id"], "binding": request["binding"],
                                                       "result_json": json.dumps(result), "evidence": evidence}))
    client.write_trace(directory / "trace.jsonl")


class ArmThroughTheGuardTests(unittest.TestCase):
    """Worked example 1 (offline half): `arm` is the proxy's own tool, today's ArmBinding, one shot."""

    def test_arm_once_through_the_guard_then_a_second_arm_is_denied(self):
        fixture, request_id = submit_fixture(self)
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            snapshot = '- button "Submit application" [ref=e1] [cursor=pointer]\n'
            guard_dir = directory / "fake"
            guard_dir.mkdir()
            (guard_dir / "snapshot.yaml").write_text(snapshot, encoding="utf-8")
            (guard_dir / "calls.jsonl").write_text("")
            self.assertNotIn("_application_human_check", config)   # E16.T1: no wait context for a session
            argv = browser_runtime.guard_command(config, directory, "submit", config["_application_guard_claim"])
            argv = argv[:argv.index("--") + 1] + fake_runtime(guard_dir / "snapshot.yaml", guard_dir / "calls.jsonl")
            client = Client(argv, cwd=directory)
            try:
                names = {tool["name"] for tool in client.request("tools/list")["tools"]}
                seen["tools"] = names
                seen["arm"] = client.call("arm", {"observed_form_revision": "fixture-form-v1"})
                client.call("browser_click", {"element": "Submit application", "target": "e1"})
                client.call("browser_snapshot")
                confirmed_output(directory, request, client, "Submit application")
                seen["second"] = client.call("arm", {"observed_form_revision": "fixture-form-v1"})
            finally:
                client.close()
            seen["token"] = json.loads(Path(config["_application_guard_claim"]).read_text())["envelope"]["token"]
            seen["token_in_prompt"] = seen["token"] in prompt
            seen["audit"] = [json.loads(line)["decision"] for line in (directory / "guard.jsonl").read_text().splitlines()]

        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertIn("arm", seen["tools"])
        self.assertFalse(seen["arm"][0], seen["arm"])
        self.assertTrue(json.loads(seen["arm"][1])["may_submit_once"])
        self.assertEqual(seen["second"], (True, browser_guard.ARM_DENIED_TEXT))
        self.assertFalse(seen["token_in_prompt"])
        self.assertEqual(seen["audit"][:2], ["ARMED", "FORWARDED"])   # arm, then the one Submit click
        self.assertEqual(seen["audit"][-1], "ARM_DENIED")
        row = fixture.database.execute("SELECT armed_at FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        self.assertIsNotNone(row["armed_at"])
        denials = [json.loads(detail) for (detail,) in fixture.database.execute(
            "SELECT detail FROM desktop_events WHERE event='SUBMIT_ARM_DENIED'")]
        self.assertEqual([denial["reason"] for denial in denials], ["APPLICATION_ARM_ALREADY_ATTEMPTED"])
        # The claim's token never reaches a report file (as live, application_browser.consume).
        for path in (fixture.root / "reports").rglob("*"):
            if path.is_file():
                self.assertNotIn(seen["token"].encode(), path.read_bytes(), str(path))


class ARefusedRunCodeCallIsANonEvent(unittest.TestCase):
    """Owner, 2026-11-12: "treat a refused call to the removed tool as a non-event, so the session's result
    stands". Real `consume`, the real guard process in front of the fake browser server: the model calls
    browser_run_code_unsafe once, is refused, and goes on."""
    RUN_CODE = ("browser_run_code_unsafe", {"code": "async (page) => { await page.getByLabel('Email').fill('x'); }"})
    SNAPSHOT = '- button "Submit application" [ref=e1] [cursor=pointer]\n- textbox "Email" [ref=e2]\n'

    def session(self, operation, steps, finish, fixture=None, request_id=None, forge=None):
        if fixture is None:
            root = Path(tempfile.mkdtemp(prefix="run-code-"))
            self.addCleanup(shutil.rmtree, root, True)
            fixture = Fixture(root / "fixture")
            self.addCleanup(fixture.database.close)
            request_id = fixture.queue(operation)
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            fake = directory / "fake"
            fake.mkdir()
            (fake / "snapshot.yaml").write_text(self.SNAPSHOT, encoding="utf-8")
            (fake / "calls.jsonl").write_text("")
            argv = browser_runtime.guard_command(config, directory, operation, config.get("_application_guard_claim"),
                                                 config.get("_application_human_check"))
            argv = argv[:argv.index("--") + 1] + fake_runtime(fake / "snapshot.yaml", fake / "calls.jsonl")
            client = Client(argv, cwd=directory)
            try:
                seen["answers"] = [step(client) if callable(step) else client.call(*step) for step in steps]
                if forge:
                    forge(client.trace)
                finish(directory, request, client)
            finally:
                client.close()
            seen["child"] = [json.loads(line)["tool"] for line in (fake / "calls.jsonl").read_text().splitlines()]
            seen["audit"] = [json.loads(line) for line in (directory / "guard.jsonl").read_text().splitlines()]
            seen["trace"] = (directory / "trace.jsonl").read_bytes()
            seen["directory"] = directory
        seen["summary"] = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        seen["result"] = json.loads(fixture.database.execute("SELECT result FROM desktop_requests WHERE request_id=?",
                                                             (request_id,)).fetchone()[0] or "null")
        seen["events"] = [row[0] for row in fixture.database.execute("SELECT event FROM desktop_events ORDER BY rowid")]
        return seen

    @staticmethod
    def blocked(directory, request, client):
        result = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Ask the owner to clear the check"}}
        (directory / "output.json").write_text(json.dumps({"request_id": request["request_id"], "binding": request["binding"],
                                                           "result_json": json.dumps(result), "evidence": []}))
        client.write_trace(directory / "trace.jsonl")

    READ = [("browser_navigate", {"url": "https://jobs.ashbyhq.com/synthetic/role-123"}), ("browser_snapshot", {})]

    def comparable(self, seen):
        # What the session decided and what it counted and bound. (Times, and `source`, the hash of the
        # whole trace file with the refused call's own lines in it, differ between any two runs.)
        result = {key: seen["result"].get(key) for key in ("blocked", "status", "reason", "observed_browser_calls",
                                                           "browser_evidence", "rejected_evidence", "error")}
        return seen["summary"]["state"], seen["summary"].get("code"), result

    def test_a_read_that_called_it_ends_as_the_same_read_without_the_call(self):
        for operation in ("inspect", "verify", "reconcile", "investigate"):
            plain = self.session(operation, self.READ, self.blocked)
            called = self.session(operation, self.READ[:1] + [self.RUN_CODE] + self.READ[1:], self.blocked)
            self.assertTrue(called["answers"][1][0], operation)                              # the model was refused
            self.assertTrue(called["answers"][1][1].startswith("REFUSED " + browser_guard.REMOVED_CODE))
            self.assertNotIn("browser_run_code_unsafe", called["child"])                     # it never ran
            self.assertEqual(plain["summary"]["state"], "COMPLETE", (operation, plain["summary"]))
            self.assertEqual(self.comparable(called), self.comparable(plain), operation)     # result, call count, evidence
            self.assertEqual(called["result"]["observed_browser_calls"], plain["result"]["observed_browser_calls"])
            self.assertEqual(called["events"], plain["events"], operation)

    @staticmethod
    def raw(name, arguments):
        """A step whose arguments go out exactly as given (Client.call turns falsy ones into {}), recorded
        in the trace as Codex records a step."""
        def step(client):
            client.calls += 1
            result = client.request("tools/call", {"name": name, "arguments": arguments})
            error = bool(result.get("isError"))
            client.trace.append({"type": "item.completed", "item": {
                "id": "item_%d" % client.calls, "type": "mcp_tool_call", "server": "browser", "tool": name,
                "arguments": arguments, "error": None, "status": "failed" if error else "completed",
                "result": {"content": result.get("content", []), "structured_content": None}}})
            return error, "\n".join(block.get("text", "") for block in result.get("content", []))
        return step

    def test_whatever_shape_its_arguments_have_the_call_is_the_removed_tools_refusal_and_a_non_event(self):   # X7
        plain = self.session("inspect", self.READ, self.blocked)
        shapes = ["async page => 1", "", [], {"code": "async (page) => 1"}]
        called = self.session("inspect", self.READ[:1] + [self.raw("browser_run_code_unsafe", shape) for shape in shapes]
                              + self.READ[1:], self.blocked)
        for error, text in called["answers"][1:5]:
            self.assertTrue(error)
            self.assertTrue(text.startswith("REFUSED " + browser_guard.REMOVED_CODE + ":"), text)   # as the removed tool
        self.assertNotIn("browser_run_code_unsafe", called["child"])                               # never forwarded
        self.assertEqual([(row["decision"], row.get("code")) for row in called["audit"] if row.get("tool") == "browser_run_code_unsafe"],
                         [("REFUSED", browser_guard.REMOVED_CODE)] * 4)
        self.assertEqual(called["summary"]["state"], "COMPLETE", called["summary"])                # the session stands
        self.assertEqual(self.comparable(called), self.comparable(plain))                          # same result, same count
        self.assertEqual(called["events"], plain["events"])                                        # nothing against the job
        # Controls: another tool with malformed arguments is still malformed (and, being a tool call the
        # guard refused, an ordinary failed step); a well-formed click passes unchanged.
        click = {"element": "Submit application", "target": "e1"}
        control = self.session("inspect", self.READ + [self.raw("browser_click", []), ("browser_click", click)], self.blocked)
        self.assertTrue(control["answers"][2][1].startswith("REFUSED " + browser_guard.MALFORMED_CODE + ":"))
        self.assertEqual(control["answers"][3][0], False)
        self.assertEqual(control["child"].count("browser_click"), 1)
        forwarded = [row for row in control["audit"] if row["decision"] == "FORWARDED" and row["tool"] == "browser_click"]
        self.assertEqual([row["arguments"] for row in forwarded], [click])

    def test_a_submit_that_called_it_first_is_still_the_one_confirmed_submission(self):
        submit = [("arm", {"observed_form_revision": "fixture-form-v1"}), ("browser_snapshot", {}),
                  ("browser_click", {"element": "Submit application", "target": "e1"}), ("browser_snapshot", {})]
        outcomes = []
        for steps in (submit, [self.RUN_CODE] + submit):
            fixture, request_id = submit_fixture(self)
            seen = self.session("submit", steps, lambda d, r, c: confirmed_output(d, r, c, "Submit application"),
                                fixture, request_id)
            self.assertEqual(seen["summary"]["state"], "COMPLETE", seen["summary"])
            self.assertEqual(seen["child"].count("browser_click"), 1)                        # exactly one submit
            self.assertIs(seen["result"]["acknowledged"], True)
            self.assertEqual(seen["result"]["confirmation_text"], "Submit application")
            outcomes.append((seen["result"], seen["events"]))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_a_trace_showing_it_as_run_or_another_unauthorized_tool_still_fails_the_session(self):
        def ran(trace):                                    # a forged trace: the call shown as having run
            for entry in trace:
                if entry["item"].get("tool") == "browser_run_code_unsafe":
                    entry["item"].update(status="completed", result={
                        "content": [{"type": "text", "text": '### Result\n"ok"'}], "structured_content": None})
        forged = self.session("inspect", self.READ[:1] + [self.RUN_CODE] + self.READ[1:], self.blocked, forge=ran)
        self.assertEqual((forged["summary"]["state"], forged["summary"]["code"]), ("FAILED", "APPLICATION_UNAUTHORIZED_TOOL"))
        other = self.session("inspect", self.READ + [("arm", {"observed_form_revision": "x"})], self.blocked)   # arm in a read
        self.assertEqual((other["summary"]["state"], other["summary"]["code"]), ("FAILED", "APPLICATION_UNAUTHORIZED_TOOL"))

    def test_a_step_that_started_and_was_never_seen_refused_fails_the_finished_session(self):   # D3
        started = {"type": "item.started", "item": {"type": "mcp_tool_call", "id": "rc1", "server": "browser",
                                                    "tool": "browser_run_code_unsafe", "arguments": {"code": "x"},
                                                    "status": "in_progress"}}
        self.assertIs(application_browser.tool_event(engines.CODEX.item_event(started["item"], "started"), False), False)

        def never_completed(trace):
            trace[:] = [entry for entry in trace if entry["item"].get("tool") != "browser_run_code_unsafe"] + [started]
        steps = self.READ[:1] + [self.RUN_CODE] + self.READ[1:]
        unseen = self.session("inspect", steps, self.blocked, forge=never_completed)
        self.assertEqual((unseen["summary"]["state"], unseen["summary"]["code"]), ("FAILED", "APPLICATION_UNAUTHORIZED_TOOL"))

        def started_then_refused(trace):                   # as a real engine writes it: started, then the refusal
            at = next(index for index, entry in enumerate(trace) if entry["item"].get("tool") == "browser_run_code_unsafe")
            trace.insert(at, {"type": "item.started", "item": dict(trace[at]["item"], status="in_progress", result=None)})
        seen = self.session("inspect", steps, self.blocked, forge=started_then_refused)
        self.assertEqual(seen["summary"]["state"], "COMPLETE", seen["summary"])

    def test_the_live_monitor_does_not_stop_a_session_at_the_refused_call(self):
        refusal = {"content": [{"type": "text", "text": "REFUSED %s: %s" % (browser_guard.REMOVED_CODE, browser_guard.REMOVED_TEXT)}],
                   "structured_content": None}                               # the Codex shape: no isError
        item = {"type": "mcp_tool_call", "id": "c2", "server": "browser", "tool": "browser_run_code_unsafe",
                "arguments": {"code": "async (page) => 1"}}
        lines = [json.dumps({"type": "item.started", "item": dict(item, status="in_progress")}),
                 json.dumps({"type": "item.completed", "item": dict(item, status="failed", result=refusal)}),
                 json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "c3", "server": "browser",
                                                                "tool": "browser_snapshot", "arguments": {}, "status": "completed",
                                                                "result": {"content": [{"type": "text", "text": "ok"}]}}})]
        script = "for line in %r:\n    print(line, flush=True)" % lines
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(application_browser.browser_runtime, "require"), \
                mock.patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                mock.patch.object(application_browser, "command", return_value=[sys.executable, "-c", script]), \
                mock.patch.object(application_browser.reply_worker, "_environment", return_value={}):
            application_browser.execute({}, "prompt", Path(root), 30)          # runs to its end: nothing raised
            self.assertEqual(len((Path(root) / "trace.jsonl").read_text().splitlines()), 3)
        ran = lines[:1] + [json.dumps({"type": "item.completed", "item": dict(item, status="completed", result={
            "content": [{"type": "text", "text": "1"}], "structured_content": None})})]
        script = "import time\nfor line in %r:\n    print(line, flush=True)\ntime.sleep(60)" % ran
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(application_browser.browser_runtime, "require"), \
                mock.patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                mock.patch.object(application_browser, "command", return_value=[sys.executable, "-c", script]), \
                mock.patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            application_browser.execute({}, "prompt", Path(root), 30)          # shown as run: stopped at once


class FrozenCommandTests(unittest.TestCase):
    """Worked example 4."""

    def test_frozen_build_starts_itself_with_the_subcommand_never_a_py_path(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        config = {"codex": "codex", "model": "m", "_application_operation": "submit",
                  "_application_guard_claim": str(directory / "claim.json"),
                  "_application_human_check": str(directory / "human-check.json")}
        with mock.patch.object(sys, "frozen", True, create=True):
            argv = browser_runtime.guard_command(config, directory, "submit", directory / "claim.json")
            command = application_browser.command(config, directory)
        self.assertEqual(argv[:3], [sys.executable, "browser-guard", "--operation"])
        guard = argv[:argv.index("--")]
        self.assertFalse([part for part in guard if part.endswith(".py")], guard)
        options = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "-c"]
        server = tomllib.loads("\n".join(o for o in options if o.startswith("mcp_servers.browser.")))["mcp_servers"]["browser"]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"][:1], ["browser-guard"])
        self.assertFalse([part for part in server["args"][:server["args"].index("--")] if part.endswith(".py")])
        # From source the same subcommand runs through orchestrator.py.
        source = browser_runtime.guard_command(config, directory, "submit")
        flags = len(portable.UTF8_FLAGS)   # -X utf8 on Windows (W1 a)
        self.assertEqual(source[1:1 + flags], list(portable.UTF8_FLAGS))
        self.assertEqual(source[1 + flags:3 + flags], [str(browser_runtime.ENTRY), "browser-guard"])


class CommandTests(unittest.TestCase):
    def options(self, config):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        command = application_browser.command(config, directory)
        options = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "-c"]
        return command, options, directory

    def test_one_guarded_browser_server_and_no_plugin(self):
        for operation, claim in (("inspect", None), ("submit", "/private/claim.json")):
            config = {"codex": "codex", "model": "astra", "browser_read_model": "sol", "_application_operation": operation,
                      "_application_human_check": "/private/human-check.json"}
            if claim:
                config["_application_guard_claim"] = claim
            command, options, directory = self.options(config)
            self.assertIn("--ignore-user-config", command)
            servers = tomllib.loads("\n".join(o for o in options if o.startswith("mcp_servers.")))["mcp_servers"]
            self.assertEqual(set(servers), {"browser"}, operation)          # no application_guard, human_check, computer-use
            server = servers["browser"]
            args = server["args"]
            self.assertEqual(args[args.index("--operation") + 1], operation)
            self.assertEqual("--claim" in args, bool(claim))
            self.assertEqual(server["tool_timeout_sec"], browser_runtime.HUMAN_CHECK_TOOL_TIMEOUT_SECONDS)
            self.assertEqual(set(server["tools"]), set(browser_guard.TOOLS) | {browser_guard.HUMAN_CHECK_TOOL}
                             | ({browser_guard.ARM_TOOL} if claim else set()))
            mcp = args[args.index("--") + 1:]
            self.assertEqual(mcp[mcp.index("--output-dir") + 1], str(directory / "browser"))
            # Off in tests (profile_home); the macOS background form is tests/test_browser_focus.py's.
            self.assertEqual(mcp[mcp.index("--user-data-dir") + 1], str(setup_steps.BROWSER_PROFILE))
            self.assertNotIn("--headless", mcp)
            launch = json.loads((directory / "browser-config.json").read_text())
            if sys.platform == "darwin":   # E7: the Chrome Setup found is named on a Mac (test_browser_offscreen)
                self.assertEqual(launch["browser"]["launchOptions"].pop("executablePath", None), setup_steps.chrome_path())
            self.assertEqual(launch, browser_runtime.LAUNCH_CONFIG)
            self.assertEqual(command[command.index("--model") + 1], "astra" if claim else "sol")
            self.assertNotIn("cua_repl", " ".join(command))
            self.assertNotIn("computer-use", " ".join(command))

    def test_runtime_missing_fails_the_session_before_codex(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        config = {"codex": "/nonexistent/codex", "model": "m", "browser_driver": {"runtime_dir": str(empty)}}
        with self.assertRaisesRegex(ValueError, "APPLICATION_BROWSER_RUNTIME_MISSING"):
            application_browser.execute(config, "p", empty, 10)
        with self.assertRaisesRegex(ValueError, "LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING"):
            linkedin_contacts.execute(config, "p", empty, 10)
        self.assertEqual(sorted(Path(path).name for path in browser_runtime.missing(config)),
                         ["cli.js", "node.exe" if os.name == "nt" else "node"])

    def test_staged_uploads_leave_the_arm_envelope_untouched(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "Resume.pdf").write_bytes(b"resume")
        (directory / "letter.pdf").write_bytes(b"letter")
        payload = {"resume": str(directory / "Resume.pdf"), "attachments": {"cover": {"path": str(directory / "letter.pdf"), "sha256": "x"}}}
        staged, pairs = application_browser.staged_uploads(payload, directory / "session")
        self.addCleanup(browser_runtime.unstage, directory / "session")
        self.assertEqual(payload["resume"], str(directory / "Resume.pdf"))
        self.assertEqual(Path(staged["resume"]).parent, (directory / "session" / "uploads").resolve())
        self.assertEqual(pairs[staged["resume"]], str((directory / "Resume.pdf").resolve()))
        self.assertEqual(Path(staged["resume"]).name, "Resume.pdf")
        self.assertEqual(Path(staged["attachments"]["cover"]["path"]).read_bytes(), b"letter")
        self.assertEqual(payload["attachments"]["cover"]["path"], str(directory / "letter.pdf"))

    def staged_name(self, stored, file_name):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        raw = directory / "raw-0123456789abcdef.docx"
        ats = directory / "application-0123456789abcdef-fedcba9876543210.pdf"
        for path in (raw, ats):
            path.write_bytes(path.name.encode())
        profile = {"resume_raw": {"path": str(raw), "file_name": file_name}}
        with mock.patch.object(application_browser.user_profile, "load", return_value=profile):
            staged, pairs = application_browser.staged_uploads({"resume": str(raw if stored == "raw" else ats)},
                                                               directory / "session")
        self.addCleanup(browser_runtime.unstage, directory / "session")
        self.assertEqual(Path(staged["resume"]).read_bytes(), Path(pairs[staged["resume"]]).read_bytes())
        return Path(staged["resume"]).name

    def test_the_staged_resume_carries_the_users_name_not_the_storage_name(self):
        self.assertEqual(self.staged_name("raw", "Jordan_Sample_Resume.docx"), "Jordan_Sample_Resume.docx")
        self.assertEqual(self.staged_name("ats", "Jordan_Sample_Resume.pdf"), "Jordan_Sample_Resume.pdf")
        self.assertEqual(self.staged_name("ats", "Resume.docx"), "Resume.pdf")
        self.assertEqual(self.staged_name("ats", None), "Resume.pdf")
        self.assertEqual(self.staged_name("raw", None), "Resume.docx")   # no name: keep the real extension


class PackagingTests(unittest.TestCase):
    def test_lockfile_pins_what_the_runtime_expects(self):
        folder = browser_runtime.ROOT / browser_runtime.RUNTIME_FOLDER
        lock = json.loads((folder / "package-lock.json").read_text())
        self.assertEqual(lock["packages"]["node_modules/@playwright/mcp"]["version"], browser_runtime.PLAYWRIGHT_MCP_VERSION)
        self.assertEqual(json.loads((folder / "package.json").read_text())["dependencies"],
                         {"@playwright/mcp": browser_runtime.PLAYWRIGHT_MCP_VERSION})
        self.assertEqual(lock["packages"][""]["engines"]["node"], browser_runtime.NODE_VERSION)
        for name, package in lock["packages"].items():
            if name:
                self.assertTrue(package["resolved"].startswith("https://registry.npmjs.org/") and package["integrity"], name)

    def test_verify_reports_a_wrong_or_missing_runtime_without_fetching(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(len(browser_runtime.verify({"browser_driver": {"runtime_dir": str(empty)}})), 2)
        cli = empty / browser_runtime.MCP_CLI
        cli.parent.mkdir(parents=True)
        cli.write_text("")
        (cli.parent / "package.json").write_text(json.dumps({"version": "0.0.84"}))
        node = Path(browser_runtime.node_path({"browser_driver": {"runtime_dir": str(empty)}}))   # node\node.exe on Windows
        node.parent.mkdir(parents=True)
        node.write_text("")
        ran = lambda argv, **kwargs: mock.Mock(stdout="v" + browser_runtime.NODE_VERSION + "\n")
        self.assertEqual(browser_runtime.verify({"browser_driver": {"runtime_dir": str(empty)}}, run=ran),
                         ["@playwright/mcp is 0.0.84, not 0.0.83"])
        (cli.parent / "package.json").write_text(json.dumps({"version": "0.0.83"}))
        self.assertEqual(browser_runtime.verify({"browser_driver": {"runtime_dir": str(empty)}}, run=ran), [])

    def test_frozen_runtime_sits_beside_the_binary(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.dict(os.environ, {browser_runtime.RUNTIME_VARIABLE: ""}):
            self.assertEqual(browser_runtime.runtime_dir({}), Path(sys.executable).resolve().parent / "browser-runtime")


class SessionEndTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_only_a_chrome_on_sidedoors_profile_is_stopped_and_its_lock_removed(self):
        profile = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, profile, True)
        config = {"browser_driver": {"profile": str(profile)}}
        # Not on this profile (a reused number, or the user's own Chrome): left alone, the lock is stale.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--user-data-dir=/elsewhere"])
        self.addCleanup(other.kill)
        os.symlink("host-%d" % other.pid, profile / "SingletonLock")
        self.assertFalse(browser_runtime.profile_in_use(config))
        self.assertFalse(browser_runtime.end_session(config))
        self.assertIsNone(other.poll())
        browser_runtime.prepare_profile(profile)                          # stale: cleared, not "in use"
        self.assertFalse(os.path.lexists(profile / "SingletonLock"))
        # On this profile: in use, and stopped at the session's end with its Singleton files gone.
        ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--user-data-dir=" + str(profile)])
        self.addCleanup(ours.kill)
        os.symlink("host-%d" % ours.pid, profile / "SingletonLock")
        (profile / "SingletonCookie").write_text("")
        self.assertTrue(browser_runtime.profile_in_use(config))
        with self.assertRaises(browser_runtime.BrowserProfileBusy):
            browser_runtime.prepare_profile(profile)
        self.assertTrue(browser_runtime.end_session(config))
        self.assertIsNotNone(ours.wait(timeout=10))
        self.assertFalse(os.path.lexists(profile / "SingletonLock") or os.path.lexists(profile / "SingletonCookie"))
        self.assertFalse(browser_runtime.end_session({"browser_driver": {"profile": str(profile), "isolated": True}}))


class TraceReaderTests(unittest.TestCase):
    """Live's cua_repl trace checks mapped to the guarded tool names (the live-parity table)."""

    def item(self, tool, server="browser", **arguments):
        return {"type": "mcp_tool_call", "id": "i", "server": server, "tool": tool, "arguments": arguments}

    def test_unauthorized_tools(self):
        for item in ({"type": "command_execution"}, {"type": "web_search"}, self.item("js", server="cua_repl"),
                     self.item("arm", server="application_guard"), self.item("browser_magic")):
            with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
                application_browser.tool_item(item, True)
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            application_browser.tool_item(self.item("arm"), False)       # arm only in a submit session
        self.assertTrue(application_browser.tool_item(self.item("arm"), True))
        self.assertFalse(application_browser.tool_item(self.item("wait_for_human_check"), False))
        self.assertFalse(application_browser.tool_item({"type": "agent_message"}, False))

    def test_browser_code_is_checked_as_live_checked_cua_repl_code(self):
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNSUPPORTED_BROWSER_CODE"):
            application_browser.tool_item(self.item("browser_evaluate", function="() => 1"), True)
        # browser_run_code_unsafe is no session's tool (owner, 2026-11-12). A call the guard refused never
        # ran: a non-event (not counted, the session goes on). Shown with any other outcome it is
        # unauthorized, whatever its code, so a forged or malformed trace fails closed.
        refusal = "REFUSED %s: %s" % (browser_guard.REMOVED_CODE, browser_guard.REMOVED_TEXT)
        base = self.item("browser_run_code_unsafe", code="async (page) => page.title()")

        def codex(text, status, **more):       # a step as Codex records it: content and structured_content, no isError
            content = [] if text is None else [{"type": "text", "text": block} for block in ([text] if isinstance(text, str) else text)]
            return dict(base, status=status, error=None, result={"content": content, "structured_content": None}, **more)
        non_events = {"the guard's refusal, Codex shape": codex(refusal, "failed"),
                      "the guard's refusal with an MCP isError": dict(codex(refusal, "failed"), result={
                          "content": [{"type": "text", "text": refusal}], "isError": True}),
                      "the engine rejected the call itself": dict(base, status="failed", result=None, error={"message": "unknown tool"})}
        failures = {"it ran": codex('### Result\n"Apply"', "completed"),
                    "the marker on an answer that is not an error": codex(refusal, "completed"),
                    "an errored answer with nothing in it": codex(None, "failed"),
                    "an errored answer with other words": codex("failed", "failed"),
                    "another refusal code": codex("REFUSED SESSION_SECRET_REFUSED: x", "failed"),
                    "the marker not at the start": codex("x " + refusal, "failed"),
                    "the marker only in a second block": codex(["hello", refusal], "failed"),
                    "no answer and no error": dict(base, status="completed"),
                    "no answer, no error, failed": dict(base, status="failed", result=None, error=None),
                    "the same tool name on another server": dict(codex(refusal, "failed"), server="evil")}
        for submit, investigate in ((True, False), (False, False), (False, True)):
            for name, item in non_events.items():
                self.assertIs(application_browser.tool_event(engines.CODEX.item_event(item, "completed"), submit, investigate),
                              False, name)
            for name, item in failures.items():
                with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL", msg=name):
                    application_browser.tool_event(engines.CODEX.item_event(item, "completed"), submit, investigate)
        # A different tool whose answer starts with the marker is what it was: unknown ones fail, known ones count.
        marked = {"content": [{"type": "text", "text": refusal}], "structured_content": None}
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
            application_browser.tool_item(dict(self.item("browser_magic"), status="failed", result=marked), False)
        self.assertIs(application_browser.tool_item(dict(self.item("browser_snapshot"), status="failed", result=marked), False), True)
        self.assertTrue(application_browser.tool_item(self.item("browser_type", target="e1", text="fetch eval"), True))

    def test_investigate_reads_cannot_act_on_a_page_or_leave_public_urls(self):
        for tool in ("browser_click", "browser_type", "browser_press_key", "browser_fill_form", "browser_select_option",
                     "browser_file_upload", "browser_drag", "browser_drop"):
            with self.assertRaisesRegex(ValueError, "APPLICATION_INVESTIGATION_READ_ONLY"):
                application_browser.tool_item(self.item(tool, target="e1"), False, True)
            self.assertTrue(application_browser.tool_item(self.item(tool, target="e1"), True, False))
        for url in ("https://127.0.0.1/jobs", "http://careers.example/jobs", "https://aiapply.co/jobs"):
            with self.assertRaises(ValueError):
                application_browser.tool_item(self.item("browser_navigate", url=url), False, True)
            self.assertTrue(application_browser.tool_item(self.item("browser_navigate", url=url), False, False))
        self.assertTrue(application_browser.tool_item(self.item("browser_navigate", url="https://careers.example/jobs"), False, True))
        self.assertTrue(application_browser.tool_item(self.item("browser_snapshot"), False, True))

    def test_observations_are_the_page_text_never_the_echoed_call(self):
        text = ("### Ran Playwright code\n```js\nawait page.getByRole('textbox').fill('I hold a US clearance');\n```\n"
                "### Page\n- Page URL: https://jobs.example/apply\n### Snapshot\n```yaml\n- heading \"Apply\" [ref=e1]\n```")
        observed = application_browser.observed_text({"content": [{"type": "text", "text": text}]})
        self.assertNotIn("I hold a US clearance", observed)
        self.assertNotIn("Page URL", observed)                     # live kept the URL out of evidence (_meta)
        self.assertIn('heading "Apply"', observed)

    def test_carry_forward_reads_pages_from_tool_answers(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        events = [event("a", "### Page\n- Page URL: https://jobs.example/apply?x=1\n", tool="browser_navigate",
                        arguments={"url": "https://jobs.example/apply"}),
                  event("b", "### Page\n- Page URL: https://jobs.example/other\n", tool="browser_click",
                        arguments={"target": "e3"})]
        (directory / "trace.jsonl").write_text("\n".join(json.dumps(entry) for entry in events) + "\n")
        carried = application_browser.session_carry_forward(directory)
        self.assertEqual(carried["pages_opened"], ["https://jobs.example/apply", "https://jobs.example/apply?x=1",
                                                   "https://jobs.example/other"])
        self.assertEqual(carried["steps"], 2)
        self.assertTrue(carried["last_step"].startswith("browser_click "))
        self.assertFalse(carried["verified"])

    def test_no_prompt_names_the_plugin(self):
        prompts = (application_browser.PROMPT + json.dumps(application_browser.OPERATION_PROMPTS)
                   + application_browser.SUBMIT_SUPPLEMENT + linkedin_contacts.PROMPT + setup_steps.EMAIL_CHECK_PROMPT)
        for word in ("cua_repl", "cua.", "nodeRepl", "createBrowserTab", "getApp", "computer-use", "Command-Shift-G",
                     "application_guard", "tab.playwright"):
            self.assertNotIn(word, prompts)
        self.assertIn("browser_fill_form", application_browser.OPERATION_PROMPTS["submit"])
        self.assertIn("browser_file_upload", application_browser.OPERATION_PROMPTS["submit"])
        self.assertIn("FILE_UPLOAD_FAILED", application_browser.OPERATION_PROMPTS["submit"])


class EmailCheckTests(unittest.TestCase):
    """Setup step 9's mailbox read, built on the same guarded route (E7.T1 done criteria)."""

    def test_reads_the_signed_in_address_through_the_guarded_browser(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        seen = {}

        def scripted(config, prompt, directory, timeout):
            seen.update(operation=config["_browser_operation"], profile=config["browser_driver"]["profile"],
                        codex=config["codex"], prompt=prompt)
            command = outreach_history_browser.command(config, directory)
            seen["command"] = " ".join(command)
            (directory / "output.json").write_text(json.dumps({"signed_in_account": "Alex@Example.com",
                                                               "reason": "Account control"}))

        config = {"codex": "codex", "model": "m"}
        profile = root / "profile"
        codex = root / "bin" / "codex"   # a usable Codex: the email check never runs without one (2026-10-23)
        codex.parent.mkdir()
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)
        address = setup_steps.read_signed_in_account("https://mail.google.com/", str(codex), profile, config=config,
                                                     executor=scripted, root=root)
        self.assertEqual(address, "Alex@Example.com")
        self.assertEqual((seen["operation"], seen["profile"], seen["codex"]), ("webmail", str(profile), str(codex)))
        self.assertIn('"--operation","webmail"', seen["command"])
        self.assertIn('"webmail_url": "https://mail.google.com/"', seen["prompt"])
        self.assertTrue(profile.is_dir())

    def test_unreadable_or_missing_runtime(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        codex = root / "codex"
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)
        found = mock.patch.object(setup_steps, "find_codex", return_value=str(codex))
        found.start()
        self.addCleanup(found.stop)
        def nothing(config, prompt, directory, timeout):
            (directory / "output.json").write_text(json.dumps({"signed_in_account": None, "reason": "Sign-in page"}))
        self.assertIsNone(setup_steps.read_signed_in_account("https://mail.google.com/", None, root / "p",
                                                             config={"codex": "c", "model": "m"}, executor=nothing, root=root))
        def broken(config, prompt, directory, timeout):
            raise RuntimeError("codex failed")
        self.assertIsNone(setup_steps.read_signed_in_account("https://mail.google.com/", None, root / "p",
                                                             config={"codex": "c", "model": "m"}, executor=broken, root=root))
        with self.assertRaises(setup_steps.MailboxReadNotBuilt):
            setup_steps.read_signed_in_account("https://mail.google.com/", None, root / "p", root=root,
                                               config={"codex": "c", "model": "m", "browser_driver": {"runtime_dir": str(root / "none")}})


RUNTIME = real_runtime()


@unittest.skipUnless(RUNTIME, "needs Node, @playwright/mcp 0.0.83 (not downloaded) and Google Chrome on this machine")
class RealBrowserTests(unittest.TestCase):
    """Worked examples 1, 2 and 5 against the real @playwright/mcp and headless Chrome, offline."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = serve(PAGES)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        from browser_fixtures import Pages
        Pages.hits.clear()
        Pages.posts.clear()
        self.pages = Pages

    def submit(self, questions=0):
        fixture, request_id = submit_fixture(self)
        fixture.config["browser_driver"] = RUNTIME
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            resume = request["payload"]["resume"]
            seen["staged"] = resume
            client = Client(browser_runtime.guard_command(config, directory, "submit", config["_application_guard_claim"]), cwd=directory)
            try:
                client.call("browser_navigate", {"url": self.base + "application-form.html?questions=%d" % questions})
                nodes = client.nodes()
                ref = {node["name"]: key for key, node in nodes.items() if node["role"] in ("textbox", "button")}
                fields = [{"name": "Full name", "type": "textbox", "target": ref["Full name"], "value": "Alex Example"},
                          {"name": "Email", "type": "textbox", "target": ref["Email"], "value": "candidate@example.invalid"}]
                fields += [{"name": "Question %d" % i, "type": "textbox", "target": ref["Question %d" % i],
                            "value": "Answer %d" % i} for i in range(1, questions + 1)]
                seen["fill"] = client.call("browser_fill_form", {"fields": fields})
                seen["chooser"] = client.call("browser_click", {"element": "Attach", "target": ref["Attach"]})
                seen["upload"] = client.call("browser_file_upload", {"paths": [resume]})
                seen["shown"] = client.call("browser_snapshot")
                seen["arm"] = client.call("arm", {"observed_form_revision": "fixture-form-v1"})
                client.call("browser_click", {"element": "Submit application", "target": ref["Submit application"]})
                client.call("browser_wait_for", {"text": "Thank you for applying"})
                client.call("browser_snapshot")
                confirmed_output(directory, request, client, "Thank you for applying to Synthetic Company!")
                seen["calls"] = sum(application_browser.tool_item(entry["item"], True) for entry in client.trace)
                seen["second"] = client.call("arm", {"observed_form_revision": "fixture-form-v1"})
            finally:
                client.close()
            seen["audit"] = [json.loads(line) for line in (directory / "guard.jsonl").read_text().splitlines()]

        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        return fixture, request_id, summary, seen

    def test_1_resume_uploaded_through_the_chooser_armed_once_before_submit(self):
        fixture, request_id, summary, seen = self.submit()
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertIn("File chooser", seen["chooser"][1])
        self.assertFalse(seen["upload"][0], seen["upload"][1])
        self.assertIn("Resume.pdf", seen["shown"][1])                      # the page shows the human file name
        self.assertFalse(seen["arm"][0], seen["arm"][1])
        self.assertEqual(seen["second"], (True, browser_guard.ARM_DENIED_TEXT))
        order = [(entry["decision"], entry.get("tool"), (entry.get("arguments") or {}).get("element")) for entry in seen["audit"]]
        armed = order.index(("ARMED", "arm", None))
        submit = order.index(("FORWARDED", "browser_click", "Submit application"))
        self.assertLess(armed, submit)
        self.assertEqual([entry for entry in order if entry[0] == "ARMED"], [("ARMED", "arm", None)])
        self.assertEqual(order[-1][0], "ARM_DENIED")
        self.assertEqual(len(self.pages.posts), 1)
        body = json.loads(self.pages.posts[0]["body"])
        self.assertEqual((body["full_name"], body["resume"]), ("Alex Example", "Resume.pdf"))
        self.assertTrue(Path(seen["staged"]).resolve().is_relative_to((fixture.root / "reports" / "application-browser").resolve()))
        armed_at = fixture.database.execute("SELECT armed_at FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0]
        self.assertIsNotNone(armed_at)
        saved = json.loads(fixture.database.execute("SELECT result FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()[0])
        self.assertTrue(saved["acknowledged"])

    def test_5_forty_fields_finish_in_a_few_calls(self):
        fixture, request_id, summary, seen = self.submit(questions=40)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertFalse(seen["fill"][0], seen["fill"][1])
        self.assertLessEqual(seen["calls"], 12)
        body = json.loads(self.pages.posts[0]["body"])
        self.assertEqual([body["q%d" % i] for i in range(1, 41)], ["Answer %d" % i for i in range(1, 41)])

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_a_killed_session_does_not_leave_chrome_on_the_profile(self):
        directory = Path(tempfile.mkdtemp(prefix="e7t1-kill-"))
        self.addCleanup(shutil.rmtree, directory, True)
        config = {"browser_driver": dict(RUNTIME, isolated=False, profile=str(directory / "profile"))}
        browser_runtime.prepare_profile(directory / "profile")
        session = subprocess.Popen(browser_runtime.guard_command(config, directory, "inspect"), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True, cwd=directory, start_new_session=True)
        client = Client(streams=(session.stdin, session.stdout))
        self.assertFalse(client.call("browser_navigate", {"url": self.base + "captcha.html"})[0])
        os.killpg(session.pid, 9)            # as application_browser.execute ends a timed-out session
        session.wait(timeout=10)
        pid = int(os.readlink(directory / "profile" / "SingletonLock").rsplit("-", 1)[1])
        os.kill(pid, 0)                      # Playwright's Chrome is in its own group: it survived the kill
        self.assertTrue(browser_runtime.end_session(config))
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                import time
                time.sleep(0.1)

    def test_2_linkedin_contact_controls_never_reach_the_page(self):
        directory = Path(tempfile.mkdtemp(prefix="e7t1-linkedin-"))
        self.addCleanup(shutil.rmtree, directory, True)
        config = {"browser_driver": RUNTIME}
        client = Client(browser_runtime.guard_command(config, directory, "linkedin"), cwd=directory)
        self.addCleanup(client.close)
        self.assertFalse(client.call("browser_navigate", {"url": self.base + "linkedin-like.html"})[0])
        for role, name in (("button", "Invite Jane Doe to connect"), ("button", "Message"), ("button", "Follow"),
                           ("link", "Connect"), ("button", "Pending, click to withdraw invitation sent to Jane Doe")):
            error, text = client.call("browser_click", {"element": name, "target": client.ref(role, name)})
            self.assertTrue(error and text.startswith("REFUSED " + browser_guard.CONTACT_CONTROL_CODE + ":"), (name, text))
        for role, name in (("link", "Jane Doe"), ("button", "Compose message"), ("button", "Show all experiences")):
            self.assertFalse(client.call("browser_click", {"element": name, "target": client.ref(role, name)})[0], name)
        client.call("browser_wait_for", {"time": 1})
        kinds = {hit.get("kind") for hit in self.pages.hits}
        self.assertEqual(kinds, {"benign"}, self.pages.hits)
        self.assertEqual(len(linkedin_contacts.guard_refusals(directory)), 5)


if __name__ == "__main__":
    unittest.main()
