"""E7.T1 review fixes the owner approved (2026-10-22): one focused test per item, offline.

Item numbers are the review's. Item 11 was rejected; item 1 was first rejected, then reinstated
by the owner ("if live blocks it, block it too").
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import application_connection  # noqa: E402
import applications_page  # noqa: E402
import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import failures_page  # noqa: E402
import linkedin_contacts  # noqa: E402
import log_retention  # noqa: E402
import outreach_history_browser  # noqa: E402
import run_locks  # noqa: E402
import setup_steps  # noqa: E402
import test_part_c_read_gating as part_c  # noqa: E402
from browser_fixtures import Client, fake_runtime, real_runtime, serve  # noqa: E402
from manual_application_browser import Fixture, event, request_at, write_output  # noqa: E402

PAGES = Path(__file__).resolve().parent / "fixtures" / "browser"


def scratch(testcase):
    path = Path(tempfile.mkdtemp(prefix="e7t1-review-"))
    testcase.addCleanup(shutil.rmtree, path, True)
    return path


def fixture(testcase):
    root = scratch(testcase)
    made = Fixture(root / "fixture")
    testcase.addCleanup(made.database.close)
    return made


class InProcessGuard:
    """browser_guard.Proxy in this process, in front of the fake @playwright/mcp, over two pipes."""

    def __init__(self, testcase, operation, snapshot, **options):
        self.directory = scratch(testcase)
        (self.directory / "snapshot.yaml").write_text(snapshot)
        (self.directory / "calls.jsonl").write_text("")
        to_proxy, from_client = os.pipe()
        to_client, from_proxy = os.pipe()
        self.proxy = browser_guard.Proxy(operation, fake_runtime(self.directory / "snapshot.yaml", self.directory / "calls.jsonl"),
                                         audit=self.directory / "guard.jsonl", stdin=os.fdopen(to_proxy, encoding="utf-8"),
                                         stdout=os.fdopen(from_proxy, "w", encoding="utf-8"), **options)
        self.thread = threading.Thread(target=self.proxy.run, daemon=True)
        self.thread.start()
        self.client = Client(streams=(os.fdopen(from_client, "w", encoding="utf-8"), os.fdopen(to_client, encoding="utf-8")))
        testcase.addCleanup(self.close)

    def close(self):
        self.client.close()
        self.thread.join(15)

    def forwarded(self):
        return [json.loads(line) for line in (self.directory / "calls.jsonl").read_text().splitlines()]


def reconcile(testcase, items, result, evidence):
    """Consume one reconcile request whose Codex trace is `items` and whose answer is `result` with
    `evidence`; returns the consume summary."""
    made = fixture(testcase)
    submit = made.prepare_submit()

    def answered(config, prompt, directory, timeout):
        application_browser.ArmBinding(config["_application_guard_claim"]).arm({"observed_form_revision": "fixture-form-v1"})
        write_output(directory, request_at(directory), {"acknowledged": False})
    assert application_browser.consume(made.root, made.database, made.config, submit, answered)["state"] == "COMPLETE"
    request_id = made.queue("reconcile")

    def scripted(config, prompt, directory, timeout):
        request = request_at(directory)
        (directory / "trace.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items))
        (directory / "output.json").write_text(json.dumps({"request_id": request["request_id"], "binding": request["binding"],
                                                           "result_json": json.dumps(result), "evidence": evidence}))
    return application_browser.consume(made.root, made.database, made.config, request_id, scripted)


class Item1EchoedArgumentsAreNeverEvidence(unittest.TestCase):
    ABSENT = "No application found"
    RESULT = {"status": "NOT_SUBMITTED", "authoritative_absence": True, "no_inflight": True,
              "source": "The employer's candidate portal shows no application", "independent_read": True}

    def evidence(self, item_id, quote):
        return [{"pointer": pointer, "tool_call_id": item_id, "quote": quote}
                for pointer in ("/status", "/authoritative_absence", "/no_inflight")]

    def test_a_find_for_the_models_own_words_cannot_prove_absence(self):
        find = event("f", "### Result\nNo matches found for %s." % self.ABSENT, tool="browser_find",
                     arguments={"text": self.ABSENT})
        summary = reconcile(self, [find], self.RESULT, self.evidence("f", self.ABSENT))
        self.assertEqual(summary.get("code"), "APPLICATION_UNOBSERVED_QUOTE", summary)
        # Control: the same words on the page itself do bind (whatever else the read then decides).
        page = event("p", '### Snapshot\n```yaml\n- paragraph [ref=e1]: %s\n```' % self.ABSENT)
        control = reconcile(self, [page], self.RESULT, self.evidence("p", self.ABSENT))
        self.assertNotEqual(control.get("code"), "APPLICATION_UNOBSERVED_QUOTE", control)

    def test_run_code_literals_wait_for_text_and_page_urls_are_not_page_text(self):
        literal = event("c", "### Result\n\"%s\"" % self.ABSENT, tool="browser_run_code_unsafe",
                        arguments={"code": "async (page) => %r" % self.ABSENT})
        summary = reconcile(self, [literal], self.RESULT, self.evidence("c", self.ABSENT))
        self.assertEqual(summary.get("code"), "APPLICATION_UNOBSERVED_QUOTE", summary)
        waited = {"content": [{"type": "text", "text": "### Result\nWaited for %s" % self.ABSENT}]}
        self.assertNotIn(self.ABSENT, application_browser.observed_text(waited, "browser_wait_for"))
        page = {"content": [{"type": "text", "text": "### Page\n- Page URL: https://x.test/?q=No+application+found\n"
                                                     "- Page Title: Portal\n### Open tabs\n- 0: (current) [Portal](https://x.test/?q=secret)"}]}
        text = application_browser.observed_text(page, "browser_navigate")
        self.assertNotIn("x.test", text)
        self.assertIn("Page Title: Portal", text)
        found = {"content": [{"type": "text", "text": "### Result\nFound 1 match for status:\n\n- paragraph [ref=e2]: Submitted"}]}
        text = application_browser.observed_text(found, "browser_find")
        self.assertNotIn("match for status", text)
        self.assertIn("Submitted", text)                  # the matched page lines stay


class Item2UnavailableBrowserIsACapacityWait(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def adapter(self, consumer, driver):
        config = dict(part_c.RECOVERY, application_browser={"enabled": True, "timeout_seconds": 60},
                      timezone="America/Los_Angeles", browser_driver=driver)
        return application_connection.HostedDesktopAdapter(self.database, self.root, config,
                                                           clock=lambda: part_c.NOW, consumer=consumer)

    def test_runtime_missing_or_profile_in_use_spends_nothing(self):
        for code, driver, in_use in (("APPLICATION_BROWSER_RUNTIME_MISSING", {"runtime_dir": str(self.root / "none")}, False),
                                     ("APPLICATION_BROWSER_PROFILE_IN_USE", {}, True)):
            with self.subTest(code), mock.patch.object(browser_runtime, "profile_in_use", lambda config=None, run=None: in_use):
                adapter = self.adapter(lambda *args: self.fail("no session may start"), driver)
                with self.assertRaises(application_connection.CapacityWait) as raised:
                    adapter.call("inspect", {"job": self.job(), "read_only": True})
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)
                self.assertEqual(self.database.execute(
                    "SELECT COUNT(*) FROM application_events WHERE event='BROWSER_CAPACITY_RESERVED'").fetchone()[0], 0)
            self.assertIn(code, failures_page.WAIT_CODES)
            self.assertIn(code, applications_page.CODE_WORDS)

    def test_consume_and_the_finder_do_not_claim(self):
        made = fixture(self)
        request_id = made.queue("inspect")
        config = dict(made.config, browser_driver={"runtime_dir": str(made.root / "none")})
        summary = application_browser.consume(made.root, made.database, config, request_id, lambda *a: self.fail("ran"))
        self.assertEqual(summary["state"], "BROWSER_UNAVAILABLE")
        self.assertEqual(summary["code"], "APPLICATION_BROWSER_RUNTIME_MISSING")
        self.assertEqual(made.database.execute("SELECT state,claims FROM desktop_requests WHERE request_id=?",
                                               (request_id,)).fetchone()[0:2], ("PENDING", 0))
        item = {"company": "Gate Systems", "company_key": "gate systems", "slots": 3}
        for code, driver, in_use in (("LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING", {"runtime_dir": str(made.root / "none")}, False),
                                     ("LINKEDIN_CONTACTS_BROWSER_PROFILE_IN_USE", {}, True)):
            with mock.patch.object(browser_runtime, "profile_in_use", lambda config=None, run=None: in_use):
                # database=None: a deferred read touches no record at all (no run row, no compute, no try).
                ran = []
                summary = linkedin_contacts.run_one(made.root, None, {"browser_driver": driver}, item, None, executor=ran.append)
            self.assertEqual(summary, {"company": "Gate Systems", "state": linkedin_contacts.BROWSER_BUSY, "code": code})
            self.assertEqual(ran, [])
            self.assertIn(code, applications_page.CODE_WORDS)


LINKEDIN = """- main [ref=e1]:
  - button "Invite Jane Doe to connect" [active] [ref=e3] [cursor=pointer]: Connect
  - button "Message" [ref=e4] [cursor=pointer]
  - link "Jane Doe" [ref=e8] [cursor=pointer]
  - searchbox "Search employees" [ref=e9]
"""


import test_dashboard_failures  # noqa: E402
import test_dashboard_overview  # noqa: E402


class ConfirmingFix1Overview(unittest.TestCase):
    """Confirming review fix 1: the browser waits are visible on Overview."""
    setUp = test_dashboard_overview.GatherTest.setUp
    tearDown = test_dashboard_overview.GatherTest.tearDown
    gather = test_dashboard_overview.GatherTest.gather

    def test_the_browser_item_says_why_sessions_cannot_start(self):
        test_dashboard_overview.finished_home(self.home)
        for reason, tone in (("RUNTIME_MISSING", "error"), ("PROFILE_IN_USE", "warning")):
            with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                    mock.patch.object(browser_runtime, "display_state", lambda config=None, root=None, run=None: reason):
                item = {entry["name"]: entry for entry in self.gather()["health"]}["Browser profile"]
            self.assertEqual(item["tone"], tone)
            self.assertEqual(item["text"], browser_runtime.UNAVAILABLE_WORDS[reason] + " " + browser_runtime.UNAVAILABLE_TODO[reason])
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            item = {entry["name"]: entry for entry in self.gather()["health"]}["Browser profile"]
        self.assertNotIn(item["text"], [words for words in browser_runtime.UNAVAILABLE_WORDS.values()])


class ConfirmingFix1Failures(unittest.TestCase):
    """Confirming review fix 1: listed on Failures once it has lasted over an hour; the finder's
    deferral is recorded where Overview and Failures read it, spending no try."""
    setUp = test_dashboard_failures.GatherBase.setUp
    tearDown = test_dashboard_failures.GatherBase.tearDown
    gather = test_dashboard_failures.GatherBase.gather

    def test_listed_after_an_hour_and_cleared_when_the_browser_starts(self):
        started = self.now - dt.timedelta(minutes=30)
        browser_runtime.note_unavailable(self.database, "APPLICATION_BROWSER_RUNTIME_MISSING", "applications", started.isoformat())
        texts = lambda view: [entry["text"] for entry in view["needs_you"]]
        self.assertEqual(texts(self.gather()), [])                           # under an hour: not yet
        self.now = started + dt.timedelta(hours=2)
        self.assertEqual(texts(self.gather()), [browser_runtime.UNAVAILABLE_WORDS["RUNTIME_MISSING"]])
        browser_runtime.clear_unavailable(self.database)
        self.assertEqual(texts(self.gather()), [])

    def test_a_close_the_window_record_goes_once_the_window_is_gone(self):
        # Orphan-chrome review: Sidedoor closed its own leftover Chrome, so the stale record is not shown.
        started = self.now - dt.timedelta(hours=2)
        browser_runtime.note_unavailable(self.database, "APPLICATION_BROWSER_PROFILE_IN_USE", "applications", started.isoformat())
        texts = lambda view: [entry["text"] for entry in view["needs_you"]]
        with mock.patch.object(browser_runtime, "display_state", return_value="PROFILE_IN_USE"):
            self.assertEqual(texts(self.gather()), [browser_runtime.UNAVAILABLE_WORDS["PROFILE_IN_USE"]])
        with mock.patch.object(browser_runtime, "display_state", return_value=None):
            self.assertEqual(texts(self.gather()), [])

    def test_the_finder_records_its_deferral_without_a_run(self):
        root = scratch(self)
        item = {"company": "Gate Systems", "company_key": "gate systems", "slots": 3}
        linkedin_contacts.initialize(self.database)
        summary = linkedin_contacts.run_one(root, self.database, {"browser_driver": {"runtime_dir": str(root / "none")}},
                                            item, None, executor=lambda *a: self.fail("ran"))
        self.assertEqual(summary["code"], "LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING")
        rows = [tuple(row) for row in self.database.execute("SELECT code,source FROM browser_unavailable")]
        self.assertEqual(rows, [("LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING", "linkedin-contact-discovery")])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM linkedin_contact_runs").fetchone()[0], 0)
        self.database.execute("UPDATE browser_unavailable SET first_seen=?", ((self.now - dt.timedelta(hours=3)).isoformat(),))
        view = self.gather()
        self.assertEqual([(entry["group"], entry["detail"]) for entry in view["needs_you"]],
                         [("outreach", "LINKEDIN_CONTACTS_BROWSER_RUNTIME_MISSING")])
        # The next check that finds the browser available clears it.
        self.assertIsNone(browser_runtime.check_available(self.database, {}, "LINKEDIN_CONTACTS_BROWSER_", "x"))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM browser_unavailable").fetchone()[0], 0)


class Item3Rule1MatchesLiveExactly(unittest.TestCase):
    """Owner, 2026-10-22: rule 1 judges exactly what live judged (INDEXED_ACTION: indexed click,
    double-click or press), nothing more."""

    def test_the_kept_forms_are_refused(self):
        guard = InProcessGuard(self, "linkedin", LINKEDIN)
        refused = [("browser_click", {"target": "e4"}),
                   ("browser_click", {"target": "e3", "doubleClick": True}),
                   ("browser_type", {"target": "e4", "text": "hi", "submit": True}),             # .press(N, "Enter")
                   ("browser_run_code_unsafe", {"code": "async (page) => page.locator('aria-ref=e4').click()"}),
                   ("browser_run_code_unsafe", {"code": "async (page) => page.locator('aria-ref=e3').dblclick()"}),
                   ("browser_run_code_unsafe", {"code": "async (page) => page.locator(\"aria-ref=e3\").press('Enter')"})]
        for name, arguments in refused:
            error, text = guard.client.call(name, arguments)
            self.assertTrue(error and text.startswith("REFUSED " + browser_guard.CONTACT_CONTROL_CODE), (name, text))
        self.assertEqual(guard.forwarded(), [])

    def test_what_live_never_judged_is_forwarded_unjudged(self):
        guard = InProcessGuard(self, "linkedin", LINKEDIN)
        forwarded = [("browser_press_key", {"key": "Enter"}),                                   # focus on Connect: as live
                     ("browser_drag", {"startTarget": "e3", "startElement": "x", "endTarget": "e3", "endElement": "x"}),
                     ("browser_type", {"target": "e4", "text": "x"}),                          # typing presses nothing
                     ("browser_type", {"target": "e9", "text": "cto", "submit": True}),        # not a contact control
                     ("browser_run_code_unsafe", {"code": "async (page) => page.locator('aria-ref=e3').first().tap()"}),
                     ("browser_run_code_unsafe", {"code": "async (page) => page.getByRole('button').click()"}),
                     ("browser_run_code_unsafe", {"code": "async (page) => page.locator('aria-ref=e8').click()"})]
        for name, arguments in forwarded:
            self.assertFalse(guard.client.call(name, arguments)[0], name)
        self.assertEqual([call["tool"] for call in guard.forwarded()], [name for name, _ in forwarded])


class Item4StagedUploadsAreReadOnlyAndCheckedAtArm(unittest.TestCase):
    def test_the_arm_denies_a_changed_staged_copy(self):
        made = fixture(self)
        request_id = made.prepare_submit()
        seen = {}

        def scripted(config, prompt, directory, timeout):
            request = request_at(directory)
            staged = Path(request["payload"]["resume"])
            seen["folder"] = staged.parent
            seen["modes"] = (oct(staged.stat().st_mode & 0o777), oct(staged.parent.stat().st_mode & 0o777))
            os.chmod(staged.parent, 0o700)
            os.chmod(staged, 0o600)
            staged.write_bytes(b"not the resume")
            binding = application_browser.ArmBinding(config["_application_guard_claim"])
            with self.assertRaisesRegex(ValueError, "APPLICATION_STAGED_UPLOAD_CHANGED"):
                binding.arm({"observed_form_revision": "fixture-form-v1"})
            (directory / "trace.jsonl").write_text("")
            raise RuntimeError("stop")
        application_browser.consume(made.root, made.database, made.config, request_id, scripted)
        self.assertEqual(seen["modes"], ("0o400", "0o500"))
        self.assertIsNone(made.database.execute("SELECT armed_at FROM desktop_requests WHERE request_id=?",
                                                (request_id,)).fetchone()[0])
        # Item 10: the copies are gone once the session ends.
        self.assertFalse(seen["folder"].exists())

    def test_the_guard_announces_the_upload_folder_as_the_browsers_root(self):
        folder = scratch(self) / "uploads"
        folder.mkdir()
        answer = folder.parent / "answer.json"
        # A stand-in browser server that asks its client for roots and records the answer it gets.
        child = [sys.executable, "-c", "import json, sys; print(json.dumps({'jsonrpc': '2.0', 'id': 'r1', 'method': "
                 "'roots/list'}), flush=True); open(sys.argv[1], 'w').write(sys.stdin.readline())", str(answer)]
        out = io.StringIO()
        proxy = browser_guard.Proxy("submit", child, stdin=io.StringIO(""), stdout=out, uploads=str(folder))
        proxy.pump_child()
        proxy.stop_child(grace=2)
        roots = json.loads(answer.read_text())["result"]["roots"]
        self.assertEqual([root["uri"] for root in roots], [folder.resolve().as_uri()])
        self.assertNotIn("roots/list", out.getvalue())                    # Codex is never asked
        argv = browser_runtime.guard_command({}, folder.parent, "submit")
        self.assertEqual(argv[argv.index("--uploads") + 1], str(folder.resolve()))


RUNTIME = real_runtime()


@unittest.skipUnless(RUNTIME, "needs a verified Sidedoor browser runtime (~/sidedoor-browser-runtime) and Google Chrome")
class Item4RealBrowser(unittest.TestCase):
    def test_a_screenshot_cannot_overwrite_the_staged_resume_and_the_upload_still_works(self):
        server, base = serve(PAGES)
        self.addCleanup(server.shutdown)
        directory = scratch(self)
        source = directory / "resume.pdf"
        source.write_bytes(b"%PDF-1.4 resume")
        staged = browser_runtime.stage(source, directory)
        browser_runtime.seal(directory)
        self.addCleanup(browser_runtime.unstage, directory)
        client = Client(browser_runtime.guard_command({"browser_driver": RUNTIME}, directory, "submit"), cwd=directory)
        self.addCleanup(client.close)
        client.call("browser_navigate", {"url": base + "application-form.html"})
        error, text = client.call("browser_take_screenshot", {"filename": staged})
        self.assertTrue(error, text)
        self.assertEqual(Path(staged).read_bytes(), b"%PDF-1.4 resume")
        attach = client.ref("button", "Attach")
        self.assertIn("File chooser", client.call("browser_click", {"element": "Attach", "target": attach})[1])
        error, text = client.call("browser_file_upload", {"paths": [staged]})
        self.assertFalse(error, text)
        self.assertIn("resume.pdf", client.call("browser_snapshot")[1])


class Item5ProfileOwnership(unittest.TestCase):
    def test_holds_profile_reads_the_command_line_not_the_name(self):
        profile = scratch(self)
        ps = lambda argv, **kwargs: mock.Mock(stdout="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --flag\n")
        self.assertFalse(browser_runtime.holds_profile(123, profile, ps))       # the user's own Chrome
        ours = lambda argv, **kwargs: mock.Mock(stdout="Google Chrome --user-data-dir=%s --x\n" % profile)
        self.assertTrue(browser_runtime.holds_profile(123, profile, ours))
        self.assertFalse(browser_runtime.holds_profile(None, profile, ours))


class Item6PerCallTimeout(unittest.TestCase):
    def test_an_unanswered_call_is_answered_by_the_guard_and_frees_it(self):
        with mock.patch.dict(os.environ, {"FAKE_HANG": "browser_hover", "FAKE_LATE": "browser_drag", "FAKE_DELAY": "2"}):
            guard = InProcessGuard(self, "inspect", '- button "A" [ref=e1]\n', call_timeout=0.5)
        started = time.monotonic()
        error, text = guard.client.call("browser_hover", {"target": "e1"})
        self.assertTrue(error and text.startswith("TOOL_TIMEOUT"), text)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(guard.client.call("browser_snapshot")[0])          # the guard is free again
        self.assertTrue(guard.client.call("browser_drag", {"startTarget": "e1", "endTarget": "e1"})[0])
        time.sleep(2.5)                                                     # the late answer arrives ...
        self.assertFalse(guard.client.call("browser_snapshot")[0])
        self.assertEqual(guard.client.stray, [])                            # ... and is never relayed

    def test_only_the_captcha_wait_gets_the_long_timeout(self):
        directory = scratch(self)
        options = browser_runtime.codex_options({}, directory, "inspect", human_check="/h.json")
        self.assertIn("mcp_servers.browser.tool_timeout_sec=630", options)
        self.assertLess(browser_guard.CALL_TIMEOUT_SECONDS, browser_runtime.TOOL_TIMEOUT_SECONDS)
        options = browser_runtime.codex_options({}, directory, "linkedin")
        self.assertIn("mcp_servers.browser.tool_timeout_sec=120", options)


class Item7EndOfInput(unittest.TestCase):
    def test_queued_requests_are_dropped_and_the_child_is_stopped(self):
        directory = scratch(self)
        (directory / "snapshot.yaml").write_text("- button \"A\" [ref=e1]\n")
        (directory / "calls.jsonl").write_text("")
        lines = "".join(json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                    "params": {"name": "browser_click", "arguments": {"target": "e1"}}}) + "\n"
                        for index in range(3))
        proxy = browser_guard.Proxy("inspect", fake_runtime(directory / "snapshot.yaml", directory / "calls.jsonl"),
                                    stdin=io.StringIO(lines), stdout=io.StringIO())
        proxy.pump_client()        # reads every line, then the end of input
        proxy.loop()
        proxy.stop_child(grace=2)
        self.assertEqual((directory / "calls.jsonl").read_text(), "")
        self.assertIsNotNone(proxy.child.poll())

    def test_a_child_that_ignores_its_input_and_sigterm_is_killed(self):
        stubborn = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
        proxy = browser_guard.Proxy("inspect", stubborn, stdin=io.StringIO(""), stdout=io.StringIO())
        started = time.monotonic()
        proxy.stop_child(grace=0.5)
        self.assertIsNotNone(proxy.child.poll())
        self.assertLess(time.monotonic() - started, 5)


class Item8WebmailIsReadOnly(unittest.TestCase):
    def test_code_tools_are_neither_listed_approved_nor_run(self):
        guard = InProcessGuard(self, "webmail", '- button "Account" [ref=e1]\n')
        names = {tool["name"] for tool in guard.client.request("tools/list")["tools"]}
        self.assertFalse(names & set(browser_guard.WEBMAIL_HIDDEN))
        self.assertTrue(guard.client.call("browser_run_code_unsafe", {"code": "async (page) => 1"})[0])
        self.assertEqual(guard.forwarded(), [])
        options = browser_runtime.codex_options({}, scratch(self), "webmail")
        self.assertFalse([option for option in options if "browser_run_code_unsafe" in option or "browser_evaluate" in option])

    def test_the_email_check_trace_is_checked_and_its_clicks_recorded(self):
        root = scratch(self)

        def run(trace_items, address="alex@example.com"):
            def scripted(config, prompt, directory, timeout):
                (directory / "trace.jsonl").write_text("".join(json.dumps(item) + "\n" for item in trace_items))
                (directory / "output.json").write_text(json.dumps({"signed_in_account": address, "reason": "r"}))
            found = setup_steps.read_signed_in_account("https://mail.example/", None, root / "p", root=root,
                                                       config={"codex": "c", "model": "m"}, executor=scripted)
            check = sorted((root / "reports" / "email-check").glob("check-*/check.json"), key=os.path.getmtime)[-1]
            return found, json.loads(check.read_text())
        click = event("a", "ok", tool="browser_click", arguments={"target": "e1", "element": "Account"})
        found, check = run([click])
        self.assertEqual(found, "alex@example.com")
        self.assertEqual(check["clicks"], [{"target": "e1", "element": "Account"}])
        for bad, code in ((event("t", "ok", tool="browser_type", arguments={"target": "e2", "text": "x"}), "EMAIL_CHECK_NOT_READ_ONLY"),
                          (event("c", "ok", tool="browser_run_code_unsafe", arguments={"code": "async (p) => 1"}), "EMAIL_CHECK_NOT_READ_ONLY"),
                          (event("s", "ok", server="cua_repl", tool="js"), "APPLICATION_UNAUTHORIZED_TOOL")):
            found, check = run([click, bad])
            self.assertIsNone(found)
            self.assertEqual(check["refused"], code)


class Item9EmailCheckTracesArePruned(unittest.TestCase):
    def test_old_email_check_folders_go_after_the_trace_ttl(self):
        root = scratch(self)
        old = root / "reports" / "email-check" / "check-old"
        new = root / "reports" / "email-check" / "check-new"
        for folder in (old, new):
            folder.mkdir(parents=True)
            (folder / "trace.jsonl").write_text("{}")
        ancient = time.time() - log_retention.TRACE_TTL_SECONDS - 3600
        for path in (old / "trace.jsonl", old):
            os.utime(path, (ancient, ancient))
        log_retention.prune_traces(root)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class Item12LateGuardAnswersAreDropped(unittest.TestCase):
    def test_a_late_answer_to_the_guards_own_snapshot_never_reaches_codex(self):
        with mock.patch.dict(os.environ, {"FAKE_GUARD_DELAY": "1.5"}):
            guard = InProcessGuard(self, "linkedin", '- button "Message" [ref=e4]\n', snapshot_timeout=0.3)
        # The guard's snapshot times out: the click cannot be labelled and goes on unjudged, as live.
        self.assertFalse(guard.client.call("browser_click", {"target": "e4"})[0])
        time.sleep(2)
        self.assertFalse(guard.client.call("browser_snapshot")[0])
        self.assertEqual(guard.client.stray, [])


if __name__ == "__main__":
    unittest.main()
