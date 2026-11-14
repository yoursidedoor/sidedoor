"""E16.T1 (owner, 2026-11-10): a session that meets a human check stops at once and returns blocked
CAPTCHA_REQUIRED. No 10-minute wait, no window, no wait tool; the job waits on the user (E15.T1).
Kept dormant for a later task (owner, 2026-11-12): the guard's hold of one call, the claim extension
for a session that has not armed, and the session-deadline move through a file.
Offline: the browser is the fake @playwright/mcp; no Chrome, no AI, no network."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import access_outcomes  # noqa: E402
import application_browser  # noqa: E402
import applications  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import desktop_adapter  # noqa: E402
import engines  # noqa: E402
import test_access_outcomes_e15_t1 as walls  # noqa: E402
from browser_fixtures import Client, fake_runtime  # noqa: E402
from manual_application_browser import Fixture, event, request_at  # noqa: E402
from test_browser_driver import CAPTCHA_SNAPSHOT, GuardProcess, submit_fixture  # noqa: E402

WAIT = browser_guard.HUMAN_CHECK_TOOL
REFUSAL = "Tool wait_for_human_check is not available in this session."
CHECK = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "A person has to do the human check"}}


def step(identifier, tool, text, error=False, phase="item.completed"):
    """One Codex trace line for a call to the guarded browser server."""
    return {"type": phase, "item": {
        "type": "mcp_tool_call", "id": identifier, "server": "browser", "tool": tool, "arguments": {},
        "status": "failed" if error else "completed",
        "result": {"content": [{"type": "text", "text": text}], "isError": error}}}


class TheWaitToolIsRetired(unittest.TestCase):
    def context(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "human-check.json").write_text(json.dumps({"root": str(folder), "request_id": "r", "directory": str(folder),
                                                             "job_key": "k", "operation": "inspect"}))
        return folder / "human-check.json"

    def test_no_application_operation_lists_it_and_a_call_is_refused_by_name(self):
        for operation in ("inspect", "investigate", "submit", "verify", "reconcile"):
            with self.subTest(operation):
                # Even handed the old private context file, the guard builds no wait for an application.
                guard = GuardProcess(self, operation, CAPTCHA_SNAPSHOT, human_check=self.context())
                names = {tool["name"] for tool in guard.client.request("tools/list")["tools"]}
                self.assertEqual(names, set(browser_guard.TOOLS))
                started = time.monotonic()
                self.assertEqual(guard.client.call(WAIT), (True, REFUSAL))
                self.assertLess(time.monotonic() - started, 10)          # at once: nothing waits
                self.assertEqual(guard.forwarded(), [])                   # never reaches the browser
                self.assertEqual([(entry["decision"], entry["tool"]) for entry in guard.decisions()],
                                 [("NOT_OFFERED", WAIT)])                 # one audit row

    def test_the_prompt_says_stop_at_once_and_never_names_the_tool(self):
        prompt = application_browser.PROMPT + application_browser.SUBMIT_SUPPLEMENT + "".join(
            application_browser.OPERATION_PROMPTS.values())
        self.assertNotIn("wait_for_human_check", prompt)
        self.assertNotIn("13 minutes", prompt)
        self.assertIn("never click, tick, solve\nor otherwise touch it", application_browser.PROMPT)
        self.assertIn("do not interact with it in any way and do not\nwait for it to clear: stop at once and return "
                      "blocked with code CAPTCHA_REQUIRED and next_action.", application_browser.PROMPT)

    def test_nothing_of_the_wait_or_the_reopen_is_left(self):
        for name in ("HUMAN_CHECK_SECONDS", "HUMAN_CHECK_INTERVAL", "HUMAN_CHECK_FINISH", "HumanCheckWait"):
            self.assertFalse(hasattr(application_browser, name), name)
        for name in ("STATE_CODE", "restore_code", "REOPENED_TEXT", "human_check_tool", "place_code", "ONSCREEN_RETRY_SECONDS"):
            self.assertFalse(hasattr(browser_guard, name), name)
        for name in ("show_headed", "_show_headed", "retry_onscreen", "respawn_child"):
            self.assertFalse(hasattr(browser_guard.Proxy, name), name)

    def test_a_session_at_a_check_ends_blocked_with_no_wait_no_extension_and_the_normal_tool_time(self):
        root = Path(tempfile.mkdtemp(prefix="e16t1-"))
        self.addCleanup(shutil.rmtree, root, True)
        fixture = Fixture(root / "fixture")
        self.addCleanup(fixture.database.close)
        request_id = fixture.queue("inspect")
        seen = {}

        def lease():
            return tuple(fixture.database.execute("SELECT lease_until,expires_at FROM desktop_requests WHERE request_id=?",
                                                  (request_id,)).fetchone())

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            seen.update(directory=directory, config=dict(config), prompt=prompt, lease=lease(), timeout=timeout,
                        command=application_browser.command(config, directory))
            (directory / "snapshot.yaml").write_text(CAPTCHA_SNAPSHOT, encoding="utf-8")
            (directory / "calls.jsonl").write_text("")
            argv = browser_runtime.guard_command(config, directory, "inspect", None, config.get("_application_human_check"))
            argv = argv[:argv.index("--") + 1] + fake_runtime(directory / "snapshot.yaml", directory / "calls.jsonl")
            client = Client(argv, cwd=directory)
            try:
                seen["tools"] = {tool["name"] for tool in client.request("tools/list")["tools"]}
                client.call("browser_snapshot")                 # the page shows the check
                seen["wait"] = client.call(WAIT)                 # a model that still asks for the old tool
            finally:
                client.close()
            seen["forwarded"] = [json.loads(line)["tool"] for line in (directory / "calls.jsonl").read_text().splitlines()]
            seen["live"] = [application_browser.tool_item(entry["item"], False, live=True) for entry in client.trace]
            (directory / "output.json").write_text(json.dumps({"request_id": request["request_id"], "binding": request["binding"],
                                                               "result_json": json.dumps(CHECK), "evidence": []}))
            client.write_trace(directory / "trace.jsonl")

        started = time.monotonic()
        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertLess(time.monotonic() - started, 60)
        # The session's result stands: blocked on the human check.
        result = json.loads((seen["directory"] / "result.json").read_text())
        self.assertEqual(result["blocked"]["code"], "CAPTCHA_REQUIRED")
        self.assertEqual(seen["wait"], (True, REFUSAL))
        self.assertEqual(seen["tools"], set(browser_guard.TOOLS))
        self.assertEqual(seen["forwarded"], ["browser_snapshot"])       # no click or type ever acted on the check
        self.assertEqual(seen["live"], [True, False])                    # the refused call is a non-event
        # No wait context, no wait tool for the model, the ordinary tool time, the ordinary session limit.
        self.assertNotIn("_application_human_check", seen["config"])
        self.assertNotIn("--human-check", seen["command"])
        self.assertFalse([part for part in seen["command"] if WAIT in str(part)])
        self.assertTrue([part for part in seen["command"] if str(part).endswith(
            "tool_timeout_sec=%d" % browser_runtime.TOOL_TIMEOUT_SECONDS)])
        self.assertNotIn(WAIT, seen["prompt"])
        self.assertLessEqual(seen["timeout"], application_browser.MAX_SECONDS)
        # Nothing was extended, nobody was told to tick a box, and no hold file was written.
        self.assertEqual(lease(), seen["lease"])
        events = [row[0] for row in fixture.database.execute("SELECT event FROM desktop_events WHERE request_id=?", (request_id,))]
        self.assertNotIn("HUMAN_CHECK_WAIT", events)
        self.assertFalse((seen["directory"] / application_browser.HOLD_FILE).exists())
        self.assertEqual(fixture.database.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_key LIKE '%:HUMAN_CHECK:%'").fetchone()[0], 0)

    def test_no_session_time_is_sized_for_a_ten_minute_wait(self):
        # One claim's lease covers one ordinary session; a request's life covers a late claim plus one.
        self.assertEqual(application_browser.MAX_SECONDS, 900)
        self.assertLess(desktop_adapter.LEASE_SECONDS - application_browser.MAX_SECONDS, 600)
        self.assertLess(desktop_adapter.REQUEST_SECONDS - application_browser.MAX_SECONDS, 900)
        self.assertEqual(browser_runtime.TOOL_TIMEOUT_SECONDS, 120)
        for operation in ("inspect", "investigate", "submit", "verify", "reconcile"):
            options = browser_runtime.codex_options({}, Path(tempfile.gettempdir()), operation)
            self.assertTrue([option for option in options if option.endswith("tool_timeout_sec=120")], operation)
            self.assertFalse([option for option in options if WAIT in option], operation)


class TraceRule(unittest.TestCase):
    def judged(self, line, submit=False, live=False):
        (found,) = engines.CODEX.trace_reader().feed(json.dumps(line))
        return application_browser.tool_event(found, submit, live=live)

    def test_a_refused_call_is_a_non_event_and_one_that_ran_stops_a_running_session(self):
        refused = step("w", WAIT, REFUSAL, error=True)
        ran = step("w", WAIT, "Waited for the owner.")
        begun = step("w", WAIT, "", phase="item.started")
        begun["item"].update(status="in_progress", result=None)
        for live in (False, True):
            self.assertFalse(self.judged(refused, live=live))
            self.assertFalse(self.judged(begun, live=live))
        for submit in (False, True):
            with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
                self.judged(ran, submit, live=True)
        # A finished trace from before the change (the wait ran) reads as it always did.
        self.assertFalse(self.judged(ran))

    def normalized(self, lines, raw_result):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        fixture = Fixture(folder / "fixture")
        self.addCleanup(fixture.database.close)
        request_id = fixture.queue("inspect")
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            seen["directory"] = directory
            (directory / "output.json").write_text(json.dumps({"request_id": request["request_id"], "binding": request["binding"],
                                                               "result_json": json.dumps(raw_result), "evidence": []}))
            (directory / "trace.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

        summary = application_browser.consume(fixture.root, fixture.database, fixture.config, request_id, scripted)
        return summary, seen["directory"]

    def test_an_old_recorded_trace_with_the_wait_replays_to_the_verdict_it_had(self):
        """The owner's history has sessions whose wait ran ten minutes and ended CAPTCHA_REQUIRED."""
        old = [event("s1", CAPTCHA_SNAPSHOT),
               step("w1", WAIT, "", phase="item.started"),
               step("w1", WAIT, "Waited for the owner. Re-read the page once: if the human check is cleared, continue; "
                                "if it is still there, return blocked with code CAPTCHA_REQUIRED."),
               event("s2", CAPTCHA_SNAPSHOT),
               step("w2", WAIT, "WAIT_EXPIRED: the owner did not clear the human check in time.")]
        summary, directory = self.normalized(old, CHECK)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        result = json.loads((directory / "result.json").read_text())
        self.assertEqual(result["blocked"]["code"], "CAPTCHA_REQUIRED")
        self.assertEqual(result["observed_browser_calls"], 2)             # the wait was never a counted call
        # Whatever re-reads a session folder later still reads this one.
        carried = application_browser.session_carry_forward(directory)
        self.assertFalse(carried["verified"])
        reader = engines.for_run(directory).trace_reader()
        tools = [item.call.tool for line in (directory / "trace.jsonl").read_text().splitlines()
                 for item in reader.feed(line) if item.kind == "tool" and item.phase == "completed"]
        self.assertEqual(tools, ["browser_snapshot", WAIT, "browser_snapshot", WAIT])

    def test_a_new_trace_with_the_refused_call_keeps_the_sessions_result(self):
        new = [event("s1", CAPTCHA_SNAPSHOT), step("w1", WAIT, REFUSAL, error=True)]
        summary, directory = self.normalized(new, CHECK)
        self.assertEqual(summary["state"], "COMPLETE", summary)
        self.assertEqual(json.loads((directory / "result.json").read_text())["blocked"]["code"], "CAPTCHA_REQUIRED")


class NothingOnScreen(unittest.TestCase):
    """A hidden session that meets a check stays hidden: no Chrome is stopped, started or shown."""

    def proxy(self, **options):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.events, self.answers = [], []
        output = io.StringIO()
        with mock.patch.object(browser_guard.subprocess, "Popen"), \
                mock.patch.object(browser_runtime, "end_session", lambda config: self.events.append(("stopped", config))), \
                mock.patch.object(browser_runtime, "launch_background",
                                  lambda *args, **kwargs: self.events.append(("window", args, kwargs)) or "http://127.0.0.1:1"):
            proxy = browser_guard.Proxy("inspect", ["node"], audit=folder / "guard.jsonl", stdin=io.StringIO(""),
                                        stdout=output, focus_keeper=False, **options)
        proxy.child_call = lambda method, params: self.events.append(("own", params)) or {"result": {"content": [
            {"type": "text", "text": "### Result\n" + json.dumps([{"windowId": 1, "state": "minimized", "was": "normal"}])}]}}
        proxy.to_child = lambda message: self.events.append(("forwarded", message["params"]["name"]))
        proxy.audit_path, proxy.output = folder / "guard.jsonl", output
        return proxy

    def refused(self, proxy):
        with mock.patch.object(browser_runtime, "end_session", lambda config: self.events.append(("stopped", config))), \
                mock.patch.object(browser_runtime, "launch_background",
                                  lambda *args, **kwargs: self.events.append(("window", args, kwargs))):
            proxy.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": WAIT, "arguments": {}}})
        answer = json.loads(proxy.output.getvalue().splitlines()[-1])
        self.assertEqual((answer["id"], answer["result"]["isError"], answer["result"]["content"][0]["text"]),
                         (7, True, REFUSAL))
        return [json.loads(line)["decision"] for line in proxy.audit_path.read_text().splitlines()]

    def test_a_headless_session_is_never_reopened_in_a_window(self):
        proxy = self.proxy(background_chrome="/profile", headless_user_agent="Mozilla/5.0 Chrome/141.0.0.0",
                           launch_headless=lambda path, agent: "http://127.0.0.1:53111")
        self.assertTrue(proxy.headless)
        self.events.clear()
        decisions = self.refused(proxy)
        self.assertEqual(self.events, [])            # no Chrome stopped or started, no page call, nothing forwarded
        self.assertTrue(proxy.headless)
        self.assertEqual(decisions[-1], "NOT_OFFERED")
        self.assertFalse([d for d in decisions if d.startswith("HEADED_") or d == "WINDOW_ONSCREEN"])

    def test_a_minimized_session_is_never_brought_on_screen(self):
        proxy = self.proxy(offscreen=True)
        proxy.initialized = True
        decisions = self.refused(proxy)
        self.assertEqual([kind for kind, *_ in self.events if kind in {"window", "stopped", "forwarded"}], [])
        self.assertFalse([d for d in decisions if d == "WINDOW_ONSCREEN"])
        # The only placement the guard can still make is the minimize.
        proxy.place_window()
        codes = [params["arguments"]["code"] for kind, params in self.events if kind == "own"]
        self.assertTrue(codes and all("minimized" in code for code in codes))
        self.assertFalse([code for code in codes if "bringToFront" in code or "left: 80, top: 60" in code])


class OutcomesAfterTheStop(unittest.TestCase):
    """The three places a check can appear, through the real applications processing loop (E15.T1's harness)."""
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events", "blocked_session", "assert_waiting", "second_job"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    del _name

    def test_a_check_during_the_read_is_one_session_then_a_wait_then_a_close_with_try_again(self):
        self.blocked_session("CAPTCHA_REQUIRED")
        wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        card = access_outcomes.waiting(self.database)
        self.assertEqual([item["words"] for item in card], [access_outcomes.WAIT_REASONS["HUMAN_CHECK"]])
        # A human check never holds its site: another job there still gets its session.
        other = self.second_job()
        with mock.patch.object(applications, "same_posting_duplicate", return_value=False):
            self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["job"]["job_key"], other)
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=self.review()), self.now)["accepted"])
        # No retry on its own, on any day of the wait.
        for hours in (1, 24, 24 * 5):
            self.advance(hours * 3600)
            self.cycle()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.row()["state"], "BLOCKED")
        # Seven days with no action: closed with that reason, and Try again reopens it.
        self.now = wait["deadline"]
        self.cycle()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", "ACCESS_WAITED_7_DAYS"))
        self.assertEqual(len(self.requests()), 1)
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, self.key, self.now)["reopened"])
        self.assertEqual(self.row()["state"], "QUEUED")

    def test_a_check_in_the_submit_session_before_submit_is_pressed_waits_at_once(self):
        request = self.run_until_submit()
        self.assertTrue(desktop_adapter.complete(self.database, dict(
            request, result=dict(CHECK, acknowledged=False, prearm_blocked=True)), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        wait = self.assert_waiting("HUMAN_CHECK", "CAPTCHA_REQUIRED")
        self.assertFalse(applications.uncertain_submission(self.database, self.key))
        sessions = len(self.requests())
        for _ in range(6):
            self.advance(1200)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "access-fixture", self.now))
        self.assertEqual(len(self.requests()), sessions)
        self.assertEqual([item["id"] for item in self.waits()], [wait["id"]])

    def test_a_check_that_fires_on_submit_goes_to_reconcile_and_never_to_a_wait(self):
        """Submit was pressed (the request armed): whether the application went through is not known,
        so the job is reconciled as before. A wait here could end in a second application."""
        request = self.run_until_submit()
        desktop_adapter.arm(self.database, self.root, dict(request, observed_form_revision="fixture-form-v1"), self.now)
        self.assertTrue(desktop_adapter.complete(
            self.database, dict(request, result=dict(CHECK, acknowledged=False)), self.now)["accepted"])
        self.advance(60)
        self.cycle()
        self.assertEqual(self.row()["state"], "RECONCILE")
        self.assertEqual(self.waits(), [])
        self.assertTrue(applications.uncertain_submission(self.database, self.key))
        self.assertEqual(access_outcomes.waiting(self.database), [])
        submits = [row for row in self.requests() if row["operation"] == "submit"]
        for _ in range(3):
            self.advance(1200)
            self.cycle()
        self.assertEqual([row for row in self.requests() if row["operation"] == "submit"], submits)   # never a second submit

    def test_the_browser_window_words_claim_a_card_only_for_a_check_before_sending(self):
        """A check met while confirming a sent application has its own card (tests/test_confirm_read_card.py),
        but one that fires as Submit is pressed has none until a confirming read meets it."""
        for words in (browser_runtime.OFFSCREEN_WORDS, browser_runtime.HEADLESS_WORDS, browser_runtime.MIXED_WORDS):
            self.assertTrue(words.endswith("When a site asks for a human check before an application is sent, "
                                           "the job waits for you under Needs you."), words)
            self.assertNotIn("\u2014", words)


class KeptForALaterTask(unittest.TestCase):
    """Owner, 2026-11-12: "task 1 keeps the hold and removes only the 10-minute wait and the window"."""

    def test_the_guard_holds_one_call_on_its_own_thread_and_releases_it(self):
        release, entered = threading.Event(), threading.Event()

        class Hold:
            def call(self):
                entered.set()
                release.wait(20)
                return "released"

        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "snapshot.yaml").write_text(CAPTCHA_SNAPSHOT, encoding="utf-8")
        (folder / "calls.jsonl").write_text("")
        to_proxy, from_client = os.pipe()
        to_client, from_proxy = os.pipe()
        proxy = browser_guard.Proxy("inspect", fake_runtime(folder / "snapshot.yaml", folder / "calls.jsonl"),
                                    audit=folder / "guard.jsonl", stdin=os.fdopen(to_proxy, encoding="utf-8"),
                                    stdout=os.fdopen(from_proxy, "w", encoding="utf-8"), human_check_wait=Hold())
        thread = threading.Thread(target=proxy.run, daemon=True)
        thread.start()
        client = Client(streams=(os.fdopen(from_client, "w", encoding="utf-8"), os.fdopen(to_client, encoding="utf-8")))
        try:
            names = {tool["name"] for tool in client.request("tools/list")["tools"]}
            self.assertEqual(names, set(browser_guard.TOOLS))             # a hold is no model-facing tool
            client.send({"jsonrpc": "2.0", "id": 900, "method": "tools/call", "params": {"name": WAIT, "arguments": {}}})
            self.assertTrue(entered.wait(10))
            self.assertFalse(client.call("browser_snapshot")[0])           # the relay keeps running meanwhile
            decisions = [json.loads(line)["decision"] for line in (folder / "guard.jsonl").read_text().splitlines()]
            self.assertIn("HUMAN_CHECK_STARTED", decisions)
            self.assertNotIn("HUMAN_CHECK_RETURNED", decisions)
            release.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                decisions = [json.loads(line) for line in (folder / "guard.jsonl").read_text().splitlines()]
                if any(entry["decision"] == "HUMAN_CHECK_RETURNED" for entry in decisions):
                    break
                time.sleep(0.05)
            returned = [entry for entry in decisions if entry["decision"] == "HUMAN_CHECK_RETURNED"]
            self.assertEqual([entry["text"] for entry in returned], ["released"])
            client.call("browser_snapshot")
            answers = [message for message in client.stray if message.get("id") == 900]
            self.assertEqual([(message["result"]["isError"], message["result"]["content"][0]["text"]) for message in answers],
                             [(False, "released")])
        finally:
            release.set()
            client.close()
            thread.join(10)

    def test_the_claim_extension_serves_a_claimed_session_and_refuses_one_that_armed(self):
        fixture, request_id = submit_fixture(self)
        database = fixture.database
        now = applications.stamp()
        request = desktop_adapter.claim(database, fixture.root, "hold-fixture", now, request_id=request_id)
        self.assertIsNotNone(request)
        until = desktop_adapter.later(request["lease_until"], 300)
        self.assertTrue(desktop_adapter.extend_for_human_check(database, request_id, until, now))
        row = database.execute("SELECT lease_until,expires_at FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()
        self.assertEqual(row["lease_until"], until)
        self.assertGreaterEqual(row["expires_at"], until)
        self.assertIn("HUMAN_CHECK_WAIT", [item[0] for item in database.execute(
            "SELECT event FROM desktop_events WHERE request_id=?", (request_id,))])
        # Once Submit may be pressed nothing gains time.
        desktop_adapter.arm(database, fixture.root, dict(request, observed_form_revision="fixture-form-v1"), now)
        later = desktop_adapter.later(until, 300)
        self.assertFalse(desktop_adapter.extend_for_human_check(database, request_id, later, now))
        self.assertEqual(database.execute("SELECT lease_until FROM desktop_requests WHERE request_id=?",
                                          (request_id,)).fetchone()[0], until)

    @unittest.skipIf(os.name == "nt", "POSIX-only, as the other sessions that run a real worker process")
    def test_a_hold_file_moves_the_session_deadline_up_to_the_limit_and_no_further(self):
        worker = [sys.executable, "-c", "import time; time.sleep(60)"]

        def run(until):
            started = time.monotonic()
            with tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(application_browser.browser_runtime, "require"), \
                    mock.patch.object(application_browser.browser_runtime, "settings", return_value={"isolated": True}), \
                    mock.patch.object(application_browser, "command", return_value=worker), \
                    mock.patch.object(application_browser.reply_worker, "_environment", return_value={}), \
                    mock.patch.object(application_browser, "HOLD_LIMIT_SECONDS", 2), \
                    mock.patch.object(application_browser, "HOLD_FINISH_SECONDS", 1), \
                    self.assertRaisesRegex(TimeoutError, "APPLICATION_BROWSER_TIMEOUT"):
                if until is not None:
                    (Path(root) / application_browser.HOLD_FILE).write_text(json.dumps({"until": until.isoformat()}))
                application_browser.execute({}, "prompt", Path(root), 1)
            return time.monotonic() - started

        now = dt.datetime.now(dt.timezone.utc)
        self.assertLess(run(None), 2.5)                                           # no hold: the session's own limit
        self.assertGreater(run(now + dt.timedelta(hours=1)), 3.5)                 # moved, and capped at limit + finish
        self.assertEqual((application_browser.HOLD_LIMIT_SECONDS, application_browser.HOLD_FINISH_SECONDS), (600, 180))


if __name__ == "__main__":
    unittest.main()
