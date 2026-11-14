"""E15.T4: what the AI can reach of the cookie jar, and of the machine, through the guard.

Q1 (owner, 2026-11-12, "remove it"): browser_run_code_unsafe is no session's tool: not listed for any
operation or engine, refused by name and audited (the name, never the code) when called. The guard's own
calls to that server tool still work. What stays for the model: the allow-list of known tools (Q5), the
text refusal of in-page code that names the page's cookies, and the header scrub of the network tools (Q4).
No browser, no Node.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_guard  # noqa: E402
import browser_runtime  # noqa: E402
import engines  # noqa: E402

SECRET = "s3cr3t-jar-value-51c0ffee"
RUN_CODE = "browser_run_code_unsafe"
# The security review's two ways out of the old membrane (round 2): neither can run, the call is never made.
ESCAPES = {
    "the context's own constructor": "async (page) => globalThis.constructor.constructor('return process')().mainModule.require('child_process').execSync('id').toString()",
    "launching a program as a browser": "async (page) => await page.context().browser().browserType().launch({ executablePath: '/bin/sh', args: ['-c', 'id > /tmp/pwned'] })",
    "the cookie jar": "async (page) => await page.context().cookies()",
}


class TextCheck(unittest.TestCase):
    def test_in_page_code_that_names_the_pages_cookies_is_refused(self):
        for function in ("() => document.cookie", "() => document['cookie']", "async () => await cookieStore.getAll()",
                         "() => { const { cookie } = document; return cookie; }", "() => { /* cookie */ return 1 }"):
            refusal = browser_guard.cookie_refusal("browser_evaluate", {"function": function})
            self.assertEqual(refusal and refusal.code, browser_guard.JAR_CODE, function)
        # What a text check cannot see, and nothing else stops in the page (owner accepted, Q6 a): cookies
        # NOT marked httpOnly, spelled indirectly.
        self.assertIsNone(browser_guard.cookie_refusal("browser_evaluate", {"function": "() => document['coo' + 'kie']"}))

    def test_ordinary_in_page_code_and_words_in_text_pass(self):
        for function in ("() => document.title", "() => document.querySelector('button').innerText === 'Accept cookies'",
                         "(el) => el.value"):
            self.assertIsNone(browser_guard.cookie_refusal("browser_evaluate", {"function": function}), function)
        self.assertIsNone(browser_guard.cookie_refusal("browser_click", {"target": "e5", "element": "Accept cookies"}))


class RunCodeIsNoSessionsTool(unittest.TestCase):
    def test_it_is_in_no_operations_list_for_any_engine(self):
        self.assertNotIn(RUN_CODE, browser_guard.TOOLS)
        directory = Path(tempfile.mkdtemp(prefix="run-code-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for operation in browser_guard.OPERATIONS:
            codex = browser_runtime.codex_options({}, directory, operation)
            self.assertFalse([option for option in codex if RUN_CODE in option], operation)          # Codex approvals
            self.assertNotIn(RUN_CODE, engines.CLAUDE.browser_tools(operation, human_check="x"), operation)   # Claude, agy
        claude_submit = engines.CLAUDE.browser_tools("submit", claim="x")
        self.assertNotIn(RUN_CODE, claude_submit)
        self.assertIn("browser_click", claude_submit)
        self.assertNotIn(RUN_CODE, [tool for tools in engines.ANTIGRAVITY.sidedoor_tools().values() for tool in tools])
        self.assertIn("browser_evaluate", engines.CLAUDE.browser_tools("linkedin"))                  # the in-page tool stays
        self.assertNotIn("browser_evaluate", engines.CLAUDE.browser_tools("webmail"))


class ClaudeDeniesItItself(unittest.TestCase):
    """On Claude the tool is outside --allowedTools, so Claude denies the call before it reaches the guard:
    its own permission_denied event for that call id is the proof the call never ran."""
    NAME = "mcp__browser__browser_run_code_unsafe"

    def events(self, name, denied=True, error=True):
        reader = engines.CLAUDE.trace_reader(allowed=["mcp__browser__browser_snapshot"])
        lines = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": {"code": "x"}}]}}]
        if denied:
            lines.append({"type": "system", "subtype": "permission_denied", "tool_use_id": "t1", "tool_name": name, "message": "denied"})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": error,
                                                                 "content": "Permission to use it has been denied"}]}})
        return [event for line in lines for event in reader.feed(json.dumps(line))]

    def judged(self, events):
        import application_browser
        return [application_browser.tool_event(event, False) for event in events if event.phase or event.kind == "error"]

    def test_a_denied_call_is_a_non_event_and_anything_else_still_fails(self):
        import application_browser
        events = self.events(self.NAME)
        self.assertNotIn(engines.ClaudeEngine.FORBIDDEN_TOOL, [event.raw_kind for event in events])   # not the kill switch
        self.assertIn(engines.ClaudeEngine.PERMISSION_DENIED, [event.raw_kind for event in events])   # the answer is kept
        self.assertEqual(self.judged(events), [False, False, False])                                # started, denial, completed
        for kwargs in ({"denied": False}, {"denied": False, "error": False}, {"error": False}):     # not provably denied
            with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):
                self.judged(self.events(self.NAME, **kwargs))
        with self.assertRaisesRegex(ValueError, "APPLICATION_UNAUTHORIZED_TOOL"):                   # any other tool: unchanged
            self.judged(self.events("mcp__browser__browser_storage_state"))
        self.assertIn(engines.ClaudeEngine.FORBIDDEN_TOOL,
                      [event.raw_kind for event in self.events("mcp__browser__browser_storage_state")])


class Guard(unittest.TestCase):
    """The real guard proxy: what is forwarded to the browser server and what the model gets back."""

    def guard(self, operation="submit"):
        self.directory = Path(tempfile.mkdtemp(prefix="jar-guard-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        with mock.patch.object(browser_guard.subprocess, "Popen"):
            self.proxy = browser_guard.Proxy(operation, ["node", "cli.js"], audit=self.directory / "guard.jsonl",
                                             stdin=io.StringIO(""), stdout=io.StringIO(), focus_keeper=False)
        self.addCleanup(self.proxy.audit.close)
        self.forwarded = []
        self.proxy.to_child = self.forwarded.append
        return self.proxy

    def setUp(self):
        self.guard()

    def call(self, ident, tool, arguments):
        self.proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
        with self.proxy.idle:
            self.proxy.in_flight.clear()          # answered below: the next call does not wait for it

    def answers(self, *messages):
        """The browser server's answers, through the guard's own pump, as the model receives them."""
        child = mock.Mock(stdout=[json.dumps(message) + "\n" for message in messages])
        self.proxy.child = mock.Mock()            # another server: the pump ends nothing
        self.proxy.pump_child(child)
        return [json.loads(line) for line in self.proxy.stdout.getvalue().splitlines()]

    def said(self):
        return [json.loads(line) for line in self.proxy.stdout.getvalue().splitlines()]

    def audit(self):
        return (self.directory / "guard.jsonl").read_text()

    def test_a_run_code_call_is_refused_by_name_in_every_operation_and_only_its_name_is_recorded(self):
        for operation in browser_guard.OPERATIONS:
            self.guard(operation)
            for ident, code in enumerate(ESCAPES.values(), 1):
                self.call(ident, RUN_CODE, {"code": code + " /* %s */" % SECRET})
            self.call(9, RUN_CODE, {"filename": "steal.js"})
            self.assertEqual(self.forwarded, [], operation)                 # the browser server is never asked
            for answer in self.said():
                self.assertTrue(answer["result"]["isError"])
                self.assertIn("REFUSED " + browser_guard.REMOVED_CODE, answer["result"]["content"][0]["text"])
            rows = [json.loads(line) for line in self.audit().splitlines()]
            self.assertEqual([(row["decision"], row["code"], row["tool"]) for row in rows],
                             [("REFUSED", browser_guard.REMOVED_CODE, RUN_CODE)] * 4, operation)
            self.assertNotIn(SECRET, self.audit())                          # never the code
            self.assertNotIn("constructor", self.audit())

    def test_a_call_with_a_name_that_is_not_a_string_is_refused_and_the_guard_goes_on(self):   # W2
        bad = [["browser_click"], {"name": "browser_click"}, 7, None]
        for ident, name in enumerate(bad, 1):
            self.proxy.requests.put({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                     "params": {"name": name, "arguments": {}}})
        shapes = [["e1"], [], "", 0, False]                            # arguments present and not an object (X3: the falsy ones too)
        for ident, arguments in enumerate(shapes, 5):
            self.proxy.requests.put({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                     "params": {"name": "browser_click", "arguments": arguments}})
        self.proxy.requests.put({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                                 "params": {"name": "browser_snapshot", "arguments": {}}})
        self.proxy.requests.put(None)
        self.proxy.loop()                                              # the real loop: it does not raise or stop early
        self.assertEqual([m["params"]["name"] for m in self.forwarded], ["browser_snapshot"])   # still serving
        answers = self.said()
        self.assertEqual([answer["id"] for answer in answers], list(range(1, 10)))
        for answer in answers:
            self.assertTrue(answer["result"]["isError"])
            self.assertIn("REFUSED " + browser_guard.MALFORMED_CODE, answer["result"]["content"][0]["text"])
        self.assertEqual(self.audit().count(browser_guard.MALFORMED_CODE), 9)

    def test_refusing_malformed_arguments_puts_nothing_new_in_the_way_of_a_submit(self):        # X3
        click = {"element": "Submit application", "target": "e1"}
        for operation in ("inspect", "submit"):
            self.guard(operation)
            if operation == "submit":                                  # an armed submit: the session holds the claim
                self.proxy.arm_binding = mock.Mock(arm=lambda arguments: {"may_submit_once": True})
                self.call(1, "arm", {"observed_form_revision": "fixture-form-v1"})
                self.assertFalse(self.said()[0]["result"]["isError"])
            well_formed = [{"name": "browser_click", "arguments": click},      # the ordinary call
                           {"name": "browser_click"},                          # no arguments key
                           {"name": "browser_click", "arguments": None}]       # null arguments
            for ident, params in enumerate(well_formed, 10):
                self.proxy.handle({"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": params})
                with self.proxy.idle:
                    self.proxy.in_flight.clear()
            self.assertEqual([message["params"] for message in self.forwarded], well_formed, operation)   # unchanged
            self.forwarded.clear()
            before = len(self.said())
            self.call(20, "browser_click", [])                         # a malformed call: refused, nothing forwarded
            self.assertEqual(self.forwarded, [])
            self.assertIn("REFUSED " + browser_guard.MALFORMED_CODE, self.said()[before]["result"]["content"][0]["text"])
            self.call(21, "browser_click", click)                      # the next well-formed click goes through, once
            self.assertEqual([message["params"] for message in self.forwarded], [{"name": "browser_click", "arguments": click}])
            self.assertEqual(len(self.said()), before + 1)             # no refusal for it
            rows = [json.loads(line) for line in self.audit().splitlines()]
            self.assertEqual([(row["decision"], row.get("tool")) for row in rows][-2:],
                             [("REFUSED", "'browser_click'"), ("FORWARDED", "browser_click")], operation)

    def test_putting_it_back_in_the_list_does_not_bring_it_back(self):
        with mock.patch.object(browser_guard, "TOOLS", browser_guard.TOOLS + (RUN_CODE,)):
            self.assertNotIn(RUN_CODE, self.proxy.offered())
            self.call(1, RUN_CODE, {"code": "async (page) => 1"})
            self.proxy.listing.add(5)
            listed = self.answers({"jsonrpc": "2.0", "id": 5, "result": {"tools": [{"name": "browser_click"}, {"name": RUN_CODE}]}})[-1]
        self.assertEqual(self.forwarded, [])
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["browser_click"])

    def test_the_guards_own_calls_to_the_server_tool_still_go_through(self):
        self.assertEqual(browser_guard.PLACE_TOOL, RUN_CODE)
        self.proxy.child_call = mock.Mock(return_value={"result": {"content": [{"type": "text", "text": "### Result\n[{}]"}]}})
        code = "async (page) => { return [{}]; }"
        self.assertEqual(self.proxy.site_call(code), [{}])
        method, params = self.proxy.child_call.call_args[0]
        self.assertEqual((method, params["name"], params["arguments"]["code"]), ("tools/call", RUN_CODE, code))
        self.assertNotIn("REFUSED", self.audit())

    def test_in_page_code_naming_cookies_and_unknown_tools_never_reach_the_browser(self):
        self.guard("linkedin")
        self.call(1, "browser_evaluate", {"function": "() => document.cookie"})
        self.call(2, "browser_storage_state", {})
        self.call(3, "browser_cookie_list", {})
        self.assertEqual(self.forwarded, [])
        texts = [answer["result"]["content"][0]["text"] for answer in self.said()]
        self.assertIn("REFUSED " + browser_guard.JAR_CODE, texts[0])
        self.assertIn("not available in this session", texts[1])
        self.assertIn("not available in this session", texts[2])
        self.call(4, "browser_evaluate", {"function": "() => document.title"})     # the in-page tool stays
        self.assertEqual([m["params"]["name"] for m in self.forwarded], ["browser_evaluate"])

    def test_secret_header_lines_are_removed_from_the_network_tools_answers_only(self):
        self.call(1, "browser_network_request", {"index": 3})
        self.call(2, "browser_snapshot", {})
        headers = ("### Request\n  Request headers\n    accept: */*\n    Cookie: sid=%s; other=1\n    authorization: Bearer %s\n"
                   "  Response headers\n    content-type: text/html\n    set-cookie: sid=%s; HttpOnly\nset-cookie: b=%s\n"
                   % (SECRET, SECRET, SECRET, SECRET))
        page = '- textbox "Cookie: preferences" [ref=e2]\n- text: "set-cookie: is a header"'
        first, second = self.answers(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": headers}]}},
            {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": page}]}})
        text = first["result"]["content"][0]["text"]
        self.assertNotIn(SECRET, text)
        self.assertIn("    accept: */*", text)
        self.assertIn("    content-type: text/html", text)
        self.assertEqual(text.count("[removed]"), 3)                    # the second Set-Cookie value runs on (Q4)
        self.assertEqual(second["result"]["content"][0]["text"], page)     # a page's own words are not touched

    def test_every_value_of_a_repeated_or_multi_line_secret_header_is_removed(self):            # Q4
        details = ("#3 [GET] https://jobs.example.com/api\n\n  General\n    status:    [200] OK\n\n"
                   "  Request headers\n    accept: */*\n    Cookie: a=1; sid=%s; b=2\n    AUTHORIZATION: Bearer %s\n\n"
                   "  Response headers\n    content-type: text/html\n    Set-Cookie: pref=1\nsid=%s; HttpOnly\n"
                   "other=%s; Expires=Wed, 21 Oct 2026 07:28:00 GMT\n    x-request-id: 7\n\n"
                   "Call browser_network_request with part=\"response-body\" to read the response body."
                   % (SECRET, SECRET, SECRET, SECRET))
        part = "content-type: text/html\nset-cookie: pref=1\nsid=%s; HttpOnly\nx-request-id: 7\nSet-Cookie: last=%s\ntail=%s" % (
            SECRET, SECRET, SECRET)                                    # a single part: no indent, block at the end
        clean_details, clean_part = (browser_guard.scrub_text(text) for text in (details, part))
        for text in (clean_details, clean_part):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("sid=", text)
        self.assertIn("    accept: */*\n    Cookie: [removed]\n    AUTHORIZATION: [removed]\n\n  Response headers", clean_details)
        self.assertIn("    Set-Cookie: [removed]\n    x-request-id: 7\n\nCall browser_network_request", clean_details)
        self.assertIn("    status:    [200] OK", clean_details)
        self.assertEqual(clean_part, "content-type: text/html\nset-cookie: [removed]\nx-request-id: 7\nSet-Cookie: [removed]")
        page = '- textbox "Cookie: preferences" [ref=e2]\nCookie banner text'
        self.assertEqual(browser_guard.scrub_text(page), page)       # not a header line: untouched

    def test_a_body_keeps_its_harmless_lines_and_a_cookie_on_a_continuation_line_still_goes(self):   # I1
        body = ("Dear candidate,\nSet-Cookie: pref=1\nsid=%s; HttpOnly\nThank you for applying.\n"
                "We will be in touch.\nAuthorization: Bearer %s\nBest regards" % (SECRET, SECRET))
        self.call(1, "browser_network_request", {"index": 2, "part": "response-body"})
        self.call(2, "browser_network_request", {"index": 2, "part": "response-headers"})
        self.call(3, "browser_network_request", {"index": 2})
        headers = "content-type: text/html\nset-cookie: pref=1\nsid=%s; HttpOnly\nan odd continuation %s\nx-request-id: 7" % (SECRET, SECRET)
        answers = self.answers(*({"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}]}}
                                 for ident, text in ((1, body), (2, headers), (3, headers))))
        kept = answers[0]["result"]["content"][0]["text"]
        self.assertEqual(kept, "Dear candidate,\nSet-Cookie: [removed]\nThank you for applying.\n"
                               "We will be in touch.\nAuthorization: [removed]\nBest regards")
        for answer in answers[1:]:                                    # header listings: as before, every continuation goes
            self.assertEqual(answer["result"]["content"][0]["text"],
                             "content-type: text/html\nset-cookie: [removed]\nx-request-id: 7")
        self.assertNotIn(SECRET, self.proxy.stdout.getvalue())

    def test_only_the_known_tools_are_forwarded_or_listed(self):                                # Q5
        self.call(1, "browser_install_extension", {"id": "x"})       # a tool a later server might add
        self.call(2, "browser_cookie_list", {})
        self.call(3, "wait_for_human_check", {})                     # the guard's own tool, not held here
        self.assertEqual(self.forwarded, [])
        answers = [json.loads(line) for line in self.proxy.stdout.getvalue().splitlines()]
        self.assertEqual([(a["id"], a["result"]["isError"]) for a in answers], [(1, True), (2, True), (3, True)])
        self.assertIn("not available in this session", answers[0]["result"]["content"][0]["text"])
        for tool in browser_guard.TOOLS:                             # every known tool still goes through
            if tool != "browser_evaluate":
                self.call(10, tool, {})
        self.assertEqual([m["params"]["name"] for m in self.forwarded],
                         [tool for tool in browser_guard.TOOLS if tool != "browser_evaluate"])
        self.proxy.listing.add(5)
        listed = self.answers({"jsonrpc": "2.0", "id": 5, "result": {"tools": [
            {"name": "browser_click"}, {"name": "browser_install_extension"}, {"name": "browser_storage_state"}]}})[-1]
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["browser_click"])


if __name__ == "__main__":
    unittest.main()
