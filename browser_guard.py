"""`sidedoor browser-guard`: the one stdio MCP server Codex starts for every browser job (E7.T1, option A).

Owner, 2026-10-22: "Option A. Ship the spike: bundled Node and @playwright/mcp behind the guard proxy
(`sidedoor browser-guard`). The `arm` and `wait_for_human_check` tools move into that proxy." This is
the E0.T2 spike's guard_proxy.py (spikes/browser/, three clean live-parity review rounds, FINDINGS §4h)
ported as a subcommand of the same program, so a frozen build starts it as
`[sys.executable, "browser-guard", ...]` and never a .py file (browser_runtime.guard_command).

Codex starts this process; it starts @playwright/mcp (bundled Node) as its child and relays every
JSON-RPC message both ways. What it adds, all at the live harness's positions (owner, 2026-10-17:
"make sure it maps 1:1 with Live harness"):
* linkedin: live rule 1 (owner, 2026-10-01; linkedin_contacts.CONTACT_CONTROL): the contact finder
  never clicks a control that is itself Connect, Message, Follow, Invite, Send or Withdraw, judged on
  the clicked element's own line (role, name) of the guard's own fresh snapshot, BEFORE the click
  (live judged the same line after the session; owner-accepted difference). A click that cannot be
  labelled (no ref, a ref the snapshot does not show, a snapshot that fails) is not judged, as live.
  The refusal carries live's code, LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED.
* every other operation: nothing is judged here (live has no click guard there: a read is
  prompt-only, a submit's gate is the arm step). Every call is forwarded and logged.
* `arm` (submit only, given --claim): today's application_browser.ArmBinding, one shot, the same
  checks (desktop_adapter.arm), the same "APPLICATION_ARM_DENIED; stop" answer and recorded reason.
  This is the only arm route (E7.T2, owner decision 2; tests/test_arm_guard.py covers its guarantees).
* `wait_for_human_check` (given --human-check): today's application_browser.HumanCheckWait: notify,
  extend the claim, write human_check.json (which `application_browser.execute` reads to move its
  deadline), wait 600 s, then one re-read. This is the only CAPTCHA-wait route.
Every @playwright/mcp tool is listed and forwarded unjudged in every operation; the trace checks
that live ran on cua_repl code run in `application_browser.tool_item`, on the Codex trace, as before.

Usage: sidedoor browser-guard --operation <op> [--audit <file.jsonl>] [--claim <file>]
                              [--human-check <file>] -- <node> <@playwright/mcp cli.js> <args...>
"""

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time


OPERATIONS = ("linkedin", "webmail", "inspect", "investigate", "submit", "verify", "reconcile")
# Every tool @playwright/mcp 0.0.83 lists; every one is approved in every operation (live: any cua_repl call).
TOOLS = ("browser_click", "browser_close", "browser_console_messages", "browser_drag", "browser_drop",
         "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_fill_form", "browser_find",
         "browser_handle_dialog", "browser_hover", "browser_navigate", "browser_navigate_back",
         "browser_network_request", "browser_network_requests", "browser_press_key", "browser_resize",
         "browser_run_code_unsafe", "browser_select_option", "browser_snapshot", "browser_tabs",
         "browser_take_screenshot", "browser_type", "browser_wait_for")
ARM_TOOL = "arm"
HUMAN_CHECK_TOOL = "wait_for_human_check"
OWN_TOOLS = (ARM_TOOL, HUMAN_CHECK_TOOL)
# Live rule 1's refusal code (linkedin_contacts.validate refused the read with it after the session).
CONTACT_CONTROL_CODE = "LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED"
ARM_DENIED_TEXT = "APPLICATION_ARM_DENIED; stop, never submit or retry"
# A forwarded browser call the browser has not answered by then is answered by the guard with an error
# (review fix 6), under Codex's ordinary 120 s tool timeout: only the CAPTCHA wait runs longer.
CALL_TIMEOUT_SECONDS = 110
# The code tools are neither listed nor run in the webmail read (review fix 8).
WEBMAIL_HIDDEN = ("browser_evaluate", "browser_run_code_unsafe")
REF = re.compile(r"^(?:f\d+)?e\d+$")
# An aria-ref locator clicked, double-clicked or pressed in browser_run_code_unsafe code (live judged
# indexed .click(N) / .doubleClick(N) / .press(N) inside cua_repl code the same way).
CODE_ACTION = re.compile(r"""aria-ref=((?:f\d+)?e\d+)['"`]\s*\)\s*\.(?:click|dblclick|press)\s*\(""")
# Live rule 1's name part (linkedin_contacts.CONTACT_CONTROL after its role prefix).
CONTACT_NAME = re.compile(
    r"^(?:Value: [^,]*, )?(?:Description: )?(?:(?:connect|message|follow|following|unfollow|invite|send|withdraw)\b"
    r"|pending, click to withdraw)", re.I)


# --- @playwright/mcp's YAML snapshot (spikes/browser/snapshot_refs.py, unchanged) -------------------

REF_TOKEN = re.compile(r"\[ref=([a-z0-9]+)\]")
YAML_ESCAPES = {"\\": "\\", '"': '"', "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def yaml_scalar(value):
    """A value as Playwright's yamlEscapeValueIfNeeded wrote it: plain, or double-quoted with
    JSON-style escapes plus \\xNN for other control characters."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        body = value[1:-1]
        if re.search(r'(?<!\\)(?:\\\\)*"', body):
            raise ValueError("unescaped quote")
        return re.sub(r"\\(x[0-9a-fA-F]{2}|[\\\"bfnrt])",
                      lambda m: YAML_ESCAPES.get(m.group(1)) or chr(int(m.group(1)[1:], 16)), body)
    return value


def split_line(rest):
    """'key: value' as Playwright's ariaSnapshot renderer wrote it -> (key, value or None)."""
    if rest.startswith("'"):
        i = 1
        while True:
            j = rest.index("'", i)  # ValueError when unterminated: the caller marks the line unparsed
            if rest[j + 1:j + 2] == "'":
                i = j + 2
                continue
            key, after = rest[1:j].replace("''", "'"), rest[j + 1:]
            break
    else:
        cut = rest.find(": ")
        if cut >= 0:
            key, after = rest[:cut], rest[cut:]
        elif rest.endswith(":"):
            key, after = rest[:-1], ":"
        else:
            key, after = rest, ""
    if after.startswith(": "):
        return key, yaml_scalar(after[2:])
    if after in ("", ":"):
        return key, None
    raise ValueError("unexpected text after the key")


def parse_key(key):
    """'button "Name" [attr] [ref=e5] [cursor=pointer]' -> (role, name, attrs text)."""
    match = re.match(r"[a-zA-Z/]+", key)
    if not match:
        raise ValueError("no role")
    role, position, name = match.group(0), match.end(), ""
    if key[position:position + 2] == ' "':
        name, position = json.JSONDecoder().raw_decode(key, position + 1)
    return role, name, key[position:]


def parse_snapshot(text):
    """ref -> node {"role", "name", "texts", "pointer", "line", "parent", "children", "unparsed"}."""
    nodes, stack = {}, []  # stack of (indent, ref)
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        if not stripped.startswith("- "):
            continue
        indent = len(line) - len(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        try:
            key, value = split_line(stripped[2:])
            role, name, attrs = parse_key(key)
            ref_match = REF_TOKEN.search(attrs)
        except ValueError:
            refs = REF_TOKEN.findall(stripped)
            if not refs:
                continue
            ref = refs[-1]
            node = {"role": None, "name": "", "texts": [], "pointer": False, "line": stripped,
                    "parent": parent, "children": [], "unparsed": True}
        else:
            if ref_match is None:
                if role == "text" and value and parent is not None:
                    nodes[parent]["texts"].append(value)
                continue
            ref = ref_match.group(1)
            node = {"role": role, "name": name if isinstance(name, str) else "",
                    "texts": [value] if value else [], "pointer": "[cursor=pointer]" in attrs,
                    "line": stripped, "parent": parent, "children": [], "unparsed": False}
        nodes[ref] = node
        if parent is not None:
            nodes[parent]["children"].append(ref)
        stack.append((indent, ref))
    return nodes


def snapshot_text(result):
    """The YAML of a browser_snapshot result: from the '```yaml' fence to the LAST closing fence."""
    texts = [c.get("text", "") for c in (result or {}).get("content") or [] if isinstance(c, dict)]
    body = "\n".join(texts)
    start = body.find("```yaml\n")
    end = body.rfind("\n```")
    return body[start + len("```yaml\n"):end + 1] if 0 <= start < end else None


def contact_label(node):
    """Rule 1 on one snapshot node: its name (or, when it has none, its own text, which is the name
    the live AX line shows), if that starts with a contact word; else None. Any role, as live."""
    for label in [node["name"]] if node["name"] else node["texts"][:1]:
        if CONTACT_NAME.match((label or "").strip()):
            return label
    return None


# --- the guard's own tools -------------------------------------------------------------------------

def arm_tool():
    """The arm tool's listing (name, description and input schema as live listed it)."""
    question = {"type": "object", "properties": {"id": {"type": ["string", "null"]}, "label": {"type": "string"},
                                                 "required": {"type": "boolean"}}}
    return {"name": ARM_TOOL,
            "description": "Final one-shot application guard. Call immediately before Submit; never retry.",
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": {"observed_form_revision": {"type": "string"},
                                           "observed_questions": {"type": "array", "items": question},
                                           "observed_candidate_email": {"type": "string"}}}}


def human_check_tool():
    """The CAPTCHA wait's listing (name, description and input schema as live listed it)."""
    return {"name": HUMAN_CHECK_TOOL,
            "description": "Call once when a CAPTCHA or 'I am human' check blocks the page. Asks the owner to clear it "
                           "himself and waits ten minutes; never touch the check yourself. It moves the request deadline "
                           "13 minutes past this call, so make no other calls and do not finish until it returns. "
                           "Then re-read the page once.",
            "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}}


class Refusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


class Proxy:
    def __init__(self, operation, child_argv, audit=None, claim=None, human_check=None, snapshot_timeout=30.0,
                 stdin=None, stdout=None, arm_binding=None, human_check_wait=None, uploads=None,
                 call_timeout=CALL_TIMEOUT_SECONDS):
        self.operation = operation
        self.audit = open(audit, "a", encoding="utf-8") if audit else None
        self.snapshot_timeout = snapshot_timeout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.child = subprocess.Popen(child_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                      bufsize=1, encoding="utf-8")
        # The arm and CAPTCHA tools are built from the same private files the self-spawned servers read.
        if arm_binding is None and claim:
            import application_browser
            arm_binding = application_browser.ArmBinding(claim)
        if human_check_wait is None and human_check:
            import application_browser
            human_check_wait = application_browser.HumanCheckWait(human_check)
        self.arm_binding = arm_binding
        self.human_check_wait = human_check_wait
        self.out_lock = threading.Lock()
        self.child_lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.own = {}  # proxy-originated request id -> queue
        self.issued = set()  # every id the guard itself sent: a late answer to one is never relayed (fix 12)
        self.expired = set()  # forwarded call ids the guard answered on timeout: their late answers are dropped
        self.uploads = uploads  # the read-only upload folder announced as the browser's one root (fix 4)
        self.call_timeout = call_timeout
        self.closing = False
        self.listing = set()  # ids of the client's tools/list requests, answered with the guard's tools added
        self.requests = queue.Queue()
        self.counter = 0
        self.label = None
        # Forwarded tools/call ids still running. A click is judged only when none is running, so the
        # guard's snapshot shows the page the click will meet.
        self.in_flight = set()
        self.idle = threading.Condition()

    # --- plumbing -----------------------------------------------------------------------------
    def to_client(self, message):
        with self.out_lock:
            self.stdout.write(json.dumps(message) + "\n")
            self.stdout.flush()

    def to_child(self, message):
        with self.child_lock:
            self.child.stdin.write(json.dumps(message) + "\n")
            self.child.stdin.flush()

    def log(self, **entry):
        if self.audit:
            entry["at"] = time.time()
            with self.audit_lock:
                self.audit.write(json.dumps(entry) + "\n")
                self.audit.flush()

    def own_tools(self):
        tools = []
        if self.arm_binding is not None:
            tools.append(arm_tool())
        if self.human_check_wait is not None:
            tools.append(human_check_tool())
        return tools

    def pump_child(self):
        for line in self.child.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                self.to_client(message)
                continue
            ident = message.get("id")
            if "method" not in message and ident in self.own:
                self.own.pop(ident).put(message)
                continue
            if "method" not in message and (self.hashable(ident) in self.issued or self.hashable(ident) in self.expired):
                continue  # a late answer to the guard's own call, or to a call already answered on timeout
            if message.get("method") == "roots/list" and self.uploads and "id" in message:
                # The browser's workspace root is the read-only upload folder, whatever the client has.
                import pathlib
                self.to_child({"jsonrpc": "2.0", "id": ident, "result": {"roots": [
                    {"uri": pathlib.Path(self.uploads).resolve().as_uri(), "name": "uploads"}]}})
                continue
            if "method" not in message:
                with self.idle:
                    self.in_flight.discard(ident)
                    self.idle.notify_all()
                if ident in self.listing:
                    self.listing.discard(ident)
                    result = message.get("result")
                    if isinstance(result, dict) and isinstance(result.get("tools"), list):
                        hidden = OWN_TOOLS + (WEBMAIL_HIDDEN if self.operation == "webmail" else ())
                        result["tools"] = [tool for tool in result["tools"]
                                           if not (isinstance(tool, dict) and tool.get("name") in hidden)]
                        result["tools"].extend(self.own_tools())
            self.to_client(message)
        self.requests.put(None)

    def pump_client(self):
        for line in self.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, list):
                # A JSON-RPC batch: each request goes through the same path as a single one.
                for item in message:
                    if isinstance(item, dict) and "method" in item:
                        self.requests.put(item)
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                self.requests.put(message)
            else:
                self.to_child(message)  # a client's answer to a child's request (e.g. roots/list)
        self.closing = True  # stdin closed: what is still queued is dropped, not forwarded (fix 7)
        self.requests.put(None)

    @staticmethod
    def hashable(ident):
        return ident if isinstance(ident, (str, int, float, type(None))) else json.dumps(ident)

    def child_call(self, method, params):
        self.counter += 1
        ident = f"guard-{self.counter}"
        box = queue.Queue()
        self.own[ident] = box
        self.issued.add(ident)
        self.to_child({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        try:
            return box.get(timeout=self.snapshot_timeout)
        except queue.Empty:
            self.own.pop(ident, None)
            return None

    # --- judging ------------------------------------------------------------------------------
    def judged_targets(self, name, args):
        """The refs a call acts on, exactly where live's rule 1 looked (owner, 2026-10-22: "keep this
        clicking guards consistent with what exists in live harness today. Anything more? remove it").
        Live judged every cua_repl call by INDEXED_ACTION = `\.(?:click|doubleClick|press)\(\s*(\d+)`
        against the element's own AX line (~/job-search linkedin_contacts.contact_control_clicks):
        - browser_click on a ref: live's `.click(N)` / `.doubleClick(N)` (doubleClick is an argument here);
        - browser_type with submit true on a ref: it fills, then presses Enter on THAT element, which is
          live's `.press(N, "Enter")`; typing without submit presses nothing (live never judged `.type`);
        - aria-ref locator `.click()` / `.dblclick()` / `.press()` in browser_run_code_unsafe code:
          live's indexed actions written in code.
        Not judged, as live: drags, key presses with no element (browser_press_key), coordinates,
        selectors and other locator chains."""
        if name == "browser_click":
            return [args.get("target")]
        if name == "browser_type" and args.get("submit") is True:
            return [args.get("target")]
        if name == "browser_run_code_unsafe" and isinstance(args.get("code"), str):
            return CODE_ACTION.findall(args["code"])
        return []

    def judge(self, name, args):
        """Live rule 1, LinkedIn only, on every ref the call acts on."""
        if self.operation != "linkedin" or not isinstance(args, dict):
            return
        targets = [target for target in self.judged_targets(name, args) if isinstance(target, str) and REF.match(target)]
        if not targets:
            return  # cannot be labelled: not judged (live)
        answer = self.child_call("tools/call", {"name": "browser_snapshot", "arguments": {}})
        result = answer.get("result") if isinstance(answer, dict) else None
        try:
            nodes = parse_snapshot(snapshot_text(result) or "")
        except (ValueError, KeyError, TypeError):
            return
        for target in targets:
            node = nodes.get(target)
            if node is None or node["unparsed"]:
                continue
            self.label = f'{node["role"]} "{node["name"]}"'
            label = contact_label(node)
            if label is not None:
                raise Refusal(CONTACT_CONTROL_CODE, f'{node["role"]} "{label[:120]}" [{target}] is a contact control. '
                              "The contact finder never clicks Connect, Message, Follow, Invite, Send or Withdraw.")

    # --- the guard's own tools ----------------------------------------------------------------
    def answer(self, message, text, error):
        if "id" in message:
            self.to_client({"jsonrpc": "2.0", "id": message["id"], "result": {
                "content": [{"type": "text", "text": text}], "isError": error}})

    def call_arm(self, message, args):
        """One arm through ArmBinding, as live: the model learns only "denied", the reason is recorded."""
        try:
            armed = self.arm_binding.arm(args if isinstance(args, dict) else {})
        except Exception as denial:
            self.arm_binding.record_denial(denial)
            self.log(decision="ARM_DENIED", tool=ARM_TOOL, reason=str(denial)[:300] or type(denial).__name__)
            self.answer(message, ARM_DENIED_TEXT, True)
            return
        self.log(decision="ARMED", tool=ARM_TOOL)
        self.answer(message, json.dumps(armed, sort_keys=True, separators=(",", ":")), False)

    def call_human_check(self, message):
        """The CAPTCHA wait through HumanCheckWait, as live, on a thread of its own so the
        relay keeps running while the owner's ten minutes pass."""
        def run():
            try:
                text, error = self.human_check_wait.call(), False
            except Exception:
                text, error = "WAIT_EXPIRED: the wait could not start. Return blocked with code CAPTCHA_REQUIRED.", True
            self.log(decision="HUMAN_CHECK_RETURNED", tool=HUMAN_CHECK_TOOL, text=text[:200])
            self.answer(message, text, error)
        self.log(decision="HUMAN_CHECK_STARTED", tool=HUMAN_CHECK_TOOL)
        threading.Thread(target=run, daemon=True).start()

    # --- main loop ----------------------------------------------------------------------------
    def handle(self, message):
        method = message.get("method")
        if method == "tools/list" and "id" in message:
            self.listing.add(message["id"])
        if method == "initialize" and self.uploads:
            # Ask the browser to read its workspace root from us (answered in pump_child).
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
            message = dict(message, params=dict(params, capabilities=dict(capabilities, roots={"listChanged": False})))
        if method != "tools/call":
            self.to_child(message)
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        if self.operation == "webmail" and name in WEBMAIL_HIDDEN:
            self.log(decision="NOT_OFFERED", tool=name)
            self.answer(message, f"Tool {name} is not available in this session.", True)
            return
        args = params["arguments"] if params.get("arguments") is not None else {}
        if name == ARM_TOOL and self.arm_binding is not None:
            self.call_arm(message, args)
            return
        if name == HUMAN_CHECK_TOOL and self.human_check_wait is not None:
            self.call_human_check(message)
            return
        self.label = None
        with self.idle:
            if not self.idle.wait_for(lambda: not self.in_flight, timeout=600):
                self.in_flight.clear()  # a call that never answered; judge against the page as it is now
        try:
            self.judge(name, args)
        except Refusal as refusal:
            self.log(decision="REFUSED", code=refusal.code, tool=name if isinstance(name, str) else repr(name),
                     arguments=args if isinstance(args, dict) else repr(args), detail=refusal.detail)
            self.answer(message, f"REFUSED {refusal.code}: {refusal.detail}", True)
            return
        self.log(decision="FORWARDED", tool=name, arguments=args, label=self.label)
        if "id" in message:
            ident = message["id"]
            with self.idle:
                self.in_flight.add(ident)
            timer = threading.Timer(self.call_timeout, self.time_out, (message,))
            timer.daemon = True
            timer.start()
        self.to_child(message)

    def time_out(self, message):
        """Fix 6: a forwarded call still unanswered after call_timeout is answered here with an error,
        frees the guard for the next call, and its late answer is dropped."""
        ident = message["id"]
        with self.idle:
            if ident not in self.in_flight:
                return
            self.in_flight.discard(ident)
            self.expired.add(self.hashable(ident))
            self.idle.notify_all()
        name = (message.get("params") or {}).get("name")
        self.log(decision="TIMED_OUT", tool=name, seconds=self.call_timeout)
        self.answer(message, f"TOOL_TIMEOUT: the browser did not answer {name} within {self.call_timeout} s. "
                    "Take a fresh snapshot before going on.", True)

    def run(self):
        threading.Thread(target=self.pump_child, daemon=True).start()
        threading.Thread(target=self.pump_client, daemon=True).start()
        self.loop()
        self.stop_child()
        return 0

    def loop(self):
        while True:
            message = self.requests.get()
            if message is None or self.closing:
                return
            self.handle(message)

    def stop_child(self, grace=10.0):
        """Fix 7: close the browser server's input (it then closes Chrome), wait, terminate, wait, kill."""
        try:
            self.child.stdin.close()
        except OSError:
            pass
        for step in (None, self.child.terminate, self.child.kill):
            if step is not None:
                try:
                    step()
                except OSError:
                    pass
            try:
                self.child.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sidedoor browser-guard", description=__doc__.split("\n")[0])
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    parser.add_argument("--audit", help="append every decision to this JSONL file")
    parser.add_argument("--claim", help="submit only: the private arm claim (application_browser.consume)")
    parser.add_argument("--human-check", help="the private CAPTCHA-wait context (application_browser.consume)")
    parser.add_argument("--uploads", help="the session's read-only upload folder, announced as the browser's root")
    parser.add_argument("child", nargs=argparse.REMAINDER, help="-- then the @playwright/mcp command")
    options = parser.parse_args(argv)
    child = options.child[1:] if options.child[:1] == ["--"] else options.child
    if not child:
        parser.error("give the @playwright/mcp command after --")
    if options.claim and options.operation != "submit":
        parser.error("--claim is for the submit operation only")
    return Proxy(options.operation, child, options.audit, options.claim, options.human_check,
                 uploads=options.uploads).run()


if __name__ == "__main__":
    sys.exit(main())
