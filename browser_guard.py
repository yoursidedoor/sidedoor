"""`sidedoor browser-guard`: the one stdio MCP server Codex starts for every browser job (E7.T1, option A).

Owner, 2026-10-22: "Option A. Ship the spike: bundled Node and @playwright/mcp behind the guard proxy
(`sidedoor browser-guard`). The `arm` and `wait_for_human_check` tools move into that proxy." This is
the E0.T2 spike's guard_proxy.py (spikes/browser/, three clean live-parity review rounds, FINDINGS §4h)
ported as a subcommand of the same program, so a frozen build starts it as
`[<binary>, "browser-guard", ...]` (scheduler_loop.program) and never a .py file (browser_runtime.guard_command).

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
* every operation: nothing brings Sidedoor's Chrome in front of the user's screen (owner, 2026-10-22:
  "is it possible to stop the sidedoor browser from just snatching control of my screen?", option a).
  Code that calls bringToFront (browser_run_code_unsafe / browser_evaluate) is refused with
  BROWSER_FOCUS_REFUSED: no flow needs it. `browser_tabs` select stays allowed (owner, CK1): returning
  to the application tab after reading an emailed code, sign-in popups and one-time Apply links need
  it; the background launch below is what keeps Chrome behind the user's windows. Live had no such tool to refuse: its
  sessions drove Chrome through the codex plugin and its focus steps were removed (AGENTS.md,
  2026-09-30 "Uploads"). Given --background-chrome (macOS, browser_runtime.background_launch) the
  guard also starts Chrome itself without activating it and points @playwright/mcp at it.
* every operation, given --offscreen (macOS, browser_runtime.offscreen; FG1 = b, then owner, 2026-10-23,
  FG2 = a: macOS clamped the off-screen window to a visible strip, so it is minimized instead): right
  after the client's `initialized` and before each forwarded call (at most once a second) the guard
  minimizes every window of the session's Chrome, parked at the off-screen bounds first, through its
  own browser_run_code_unsafe call to @playwright/mcp (CDP Browser.getWindowForTarget / setWindowBounds
  windowState "minimized", hide_code): a window Chrome un-minimized itself, a popup or a new window is
  minimized again. A minimized window still loads, renders and is snapshotted (a page in it is hidden
  exactly as a background tab is, which Playwright reads and screenshots; the throttling switches are
  off). A focus keeper (FocusKeeper, ~every 200 ms) gives the front back to the user's last app
  whenever Sidedoor's Chrome (by pid; never the user's own Chrome) activates itself, except during the
  CAPTCHA wait and while the user is deliberately looking at it. The CAPTCHA wait un-minimizes the
  window on-screen and in front for the owner's tick (the one place the guard brings Chrome forward)
  and minimizes it when the wait ends. A read-back that is not minimized is recorded
  (WINDOW_NOT_MINIMIZED); a failed placement never stops the call.
Every other @playwright/mcp tool is listed and forwarded unjudged in every operation; the trace checks
that live ran on cua_repl code run in `application_browser.tool_item`, on the Codex trace, as before.

Usage: sidedoor browser-guard --operation <op> [--audit <file.jsonl>] [--claim <file>]
                              [--human-check <file>] [--background-chrome <profile>] [--offscreen]
                              -- <node> <@playwright/mcp cli.js> <args...>
"""

import argparse
import json
import os
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
# Nothing activates Sidedoor's Chrome (owner, 2026-10-22, option a): the refusal code and its answer.
FOCUS_CODE = "BROWSER_FOCUS_REFUSED"
BRING_TO_FRONT = re.compile(r"bringToFront")
# The off-screen window (FG1 = b): placed by the guard's own call, never offered to or judged for the model.
PLACE_TOOL = "browser_run_code_unsafe"
PLACE_INTERVAL_SECONDS = 1.0
# The CAPTCHA wait's on-screen placement, when its first try fails: retried after these pauses (seconds)
# while the wait lasts (fg1-captcha, Moderate 2).
ONSCREEN_RETRY_SECONDS = (5, 15, 30, 60)
# The focus keeper (FG2 = a): poll period; how long a correction may stay unanswered (Chrome still in
# front) before it counts as the user's own choice; how soon a second activation after a correction
# counts as the user clicking again; how long after a forwarded call ends Chrome's own activation may
# still arrive; how often FOCUS_RESTORED is written.
FOCUS_POLL_SECONDS = 0.2
FOCUS_STAY_SECONDS = 2.0
FOCUS_RECLICK_SECONDS = 5.0
FOCUS_CALL_GRACE_SECONDS = 1.5
FOCUS_LOG_SECONDS = 10.0
# This many corrections within FOCUS_STAY_SECONDS: they are not holding (the user keeps Chrome in front,
# or macOS declines the switch back), so the keeper stops instead of fighting.
FOCUS_MAX_CORRECTIONS = 4


def place_code(left, top, width, height, front=False):
    """Playwright code (run by @playwright/mcp's browser_run_code_unsafe) that moves every window of the
    session's Chrome to (left, top) at width x height and answers [{windowId, left, top}] as read back.
    A minimized or maximized window is made normal first (Chrome sets a position on a normal window only).
    A window that throws is answered as {error} and the others are still placed (fg1-captcha, Moderate 2).
    `front`: then bring the current page's window in front (the CAPTCHA wait only; its failure is an
    {error, front: true} entry, never a lost placement)."""
    return ("async (page) => {\n"
            "  const seen = new Set(); const placed = [];\n"
            "  for (const p of page.context().pages()) {\n"
            "    let s = null;\n"
            "    try {\n"
            "      s = await page.context().newCDPSession(p);\n"
            "      const { windowId, bounds } = await s.send('Browser.getWindowForTarget');\n"
            "      if (seen.has(windowId)) continue;\n"
            "      seen.add(windowId);\n"
            "      if (bounds.windowState && bounds.windowState !== 'normal')\n"
            "        await s.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });\n"
            "      await s.send('Browser.setWindowBounds', { windowId, bounds: { left: %d, top: %d, width: %d, height: %d } });\n"
            "      const after = (await s.send('Browser.getWindowBounds', { windowId })).bounds;\n"
            "      placed.push({ windowId, left: after.left, top: after.top });\n"
            "    } catch (e) {\n"
            "      placed.push({ error: String(e && e.message || e).slice(0, 200) });\n"
            "    } finally {\n"
            "      if (s) await s.detach().catch(() => {});\n"
            "    }\n"
            "  }\n"
            "%s"
            "  return placed;\n"
            "}" % (left, top, width, height,
                   "  try { await page.bringToFront(); } catch (e) {\n"
                   "    placed.push({ error: String(e && e.message || e).slice(0, 200), front: true });\n"
                   "  }\n" if front else ""))


def hide_code(left, top, width, height):
    """Playwright code that minimizes every window of the session's Chrome (FG2 = a) and answers
    [{windowId, state, was}] as read back. A window not yet minimized is first made normal and parked at
    (left, top) at width x height (a failed park still minimizes), so a later un-minimize by Chrome itself
    shows only macOS's clamped strip. A window already minimized is left alone. A window that throws is
    answered as {error}; the others are still minimized."""
    return ("async (page) => {\n"
            "  const seen = new Set(); const placed = [];\n"
            "  for (const p of page.context().pages()) {\n"
            "    let s = null;\n"
            "    try {\n"
            "      s = await page.context().newCDPSession(p);\n"
            "      const { windowId, bounds } = await s.send('Browser.getWindowForTarget');\n"
            "      if (seen.has(windowId)) continue;\n"
            "      seen.add(windowId);\n"
            "      if (bounds.windowState === 'minimized') { placed.push({ windowId, state: 'minimized', was: 'minimized' }); continue; }\n"
            "      try {\n"
            "        if (bounds.windowState !== 'normal')\n"
            "          await s.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });\n"
            "        await s.send('Browser.setWindowBounds', { windowId, bounds: { left: %d, top: %d, width: %d, height: %d } });\n"
            "      } catch (e) {}\n"
            "      await s.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });\n"
            "      const after = (await s.send('Browser.getWindowBounds', { windowId })).bounds;\n"
            "      placed.push({ windowId, state: after.windowState, was: bounds.windowState });\n"
            "    } catch (e) {\n"
            "      placed.push({ error: String(e && e.message || e).slice(0, 200) });\n"
            "    } finally {\n"
            "      if (s) await s.detach().catch(() => {});\n"
            "    }\n"
            "  }\n"
            "  return placed;\n"
            "}" % (left, top, width, height))


def placed_windows(answer):
    """The [{windowId, left, top}] list a placement answered, or None (an error, or no list in its text)."""
    result = answer.get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict) or result.get("isError"):
        return None
    text = "\n".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)
                     and isinstance(c.get("text"), str))
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character == "[":
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return None


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


def focus_refusal(name, args):
    """A call that would bring Chrome to the front: a Refusal, else None. Every operation."""
    if not isinstance(args, dict):
        return None
    if name in ("browser_run_code_unsafe", "browser_evaluate") and BRING_TO_FRONT.search(
            " ".join(str(args.get(key) or "") for key in ("code", "function", "filename"))):
        return Refusal(FOCUS_CODE, "page.bringToFront() brings Sidedoor's browser in front of the user's screen, "
                       "which is never done. Leave it out; the page works the same behind other windows.")
    return None


class MacFrontmost:
    """macOS's frontmost app, read and set through LaunchServices' own tool (`lsappinfo front` / `info` /
    `setfront`: one short process per poll, ~4 ms, no window-server connection or run loop needed in
    the guard), with NSRunningApplication (pyobjc, a product dependency) as the activation fallback.
    Every method answers None/False on any failure; nothing here raises."""

    def __init__(self, run=None):
        self.run = run or subprocess.run
        self.pids = {}   # ASN -> pid (an ASN names one running app instance for its whole life)

    def _out(self, argv):
        try:
            done = self.run(argv, capture_output=True, text=True, timeout=2)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    def front(self):
        """The frontmost app's pid, or None."""
        out = self._out(["/usr/bin/lsappinfo", "front"])
        match = re.search(r"ASN:0x[0-9a-fA-F]+-0x[0-9a-fA-F]+:?", out or "")
        if not match:
            return None
        asn = match.group(0)
        if asn not in self.pids:
            info = re.search(r'"pid"\s*=\s*(\d+)', self._out(["/usr/bin/lsappinfo", "info", "-only", "pid", asn]) or "")
            if not info:
                return None
            if len(self.pids) > 256:
                self.pids.clear()
            self.pids[asn] = int(info.group(1))
        return self.pids[asn]

    def activate(self, pid):
        """Bring app `pid` to the front. True when a route accepted the request."""
        if self._out(["/usr/bin/lsappinfo", "setfront", "#%d" % int(pid)]) is not None and self.front() == pid:
            return True
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            return bool(app is not None and app.activateWithOptions_(0))
        except Exception:
            return False


class FocusKeeper:
    """FG2 = a, part 2 (owner, 2026-10-23): Chrome activates itself when Playwright creates or selects a
    page, so while a session runs the guard watches the frontmost app and, whenever Sidedoor's own Chrome
    (`chrome_pid()`: the process on Sidedoor's profile; the user's everyday Chrome is another pid and is
    only ever treated as one of the user's apps) comes to the front, gives the front back to the last
    other app at once. It does not correct:
    - while `suspended()` (the CAPTCHA wait: the owner has to tick the box);
    - while the user is looking at it on purpose ("watching"), until the front moves to another app.
      Watching starts when Chrome stays in front FOCUS_STAY_SECONDS after a correction, comes back in
      front within FOCUS_RECLICK_SECONDS of a correction while no browser call is running or just ended
      (`busy()`: the user clicked it again, e.g. its window in the Dock, after being sent back), or when
      FOCUS_MAX_CORRECTIONS corrections within FOCUS_STAY_SECONDS did not hold.
    Setup step 8's sign-in window runs without the guard, so the keeper never sees it.
    `poll()` never raises; FOCUS_RESTORED is written at most once per FOCUS_LOG_SECONDS with a count."""

    def __init__(self, chrome_pid, front, activate, busy=None, suspended=None, log=None, clock=None,
                 on_watching=None):
        self.chrome_pid, self.front, self.activate = chrome_pid, front, activate
        self.busy = busy or (lambda now: False)
        self.suspended = suspended or (lambda: False)
        self.log = log or (lambda **entry: None)
        self.clock = clock or time.monotonic
        self.on_watching = on_watching or (lambda watching: None)
        self.last_other = None     # the last frontmost app that is not Sidedoor's Chrome
        self.front_since = None    # when Sidedoor's Chrome last came to the front (None: not in front)
        self.corrected_at = None   # the last correction
        self.recent = []           # times of the corrections within FOCUS_STAY_SECONDS
        self.watching = False
        self.logged_at = None
        self.unlogged = 0

    def poll(self):
        try:
            self._poll()
        except Exception as error:   # never stops the guard
            try:
                self.log(decision="FOCUS_KEEPER_ERROR", error=(str(error) or type(error).__name__)[:200])
            except Exception:
                pass

    def _set_watching(self, value, now):
        if self.watching == value:
            return
        self.watching = value
        self.log(decision="FOCUS_USER_WATCHING" if value else "FOCUS_KEEPER_RESUMED")
        self.on_watching(value)

    def _poll(self):
        now = self.clock()
        pid = self.front()
        if pid is None:
            return
        chrome = self.chrome_pid()
        if chrome is None or pid != chrome:
            self.last_other = pid
            self.front_since = None
            self._set_watching(False, now)
            return
        new = self.front_since is None
        if new:
            self.front_since = now
        if self.suspended():
            self.corrected_at = None       # the CAPTCHA wait's time in front is not the user's choice
            self.front_since = now
            return
        if self.watching or self.last_other is None:
            return
        if self.corrected_at is not None:
            since = now - self.corrected_at
            if not new:
                if now - self.front_since >= FOCUS_STAY_SECONDS:
                    self._set_watching(True, now)   # the correction did not take, or the user insists
                return                              # one correction per stay in front
            if since < FOCUS_RECLICK_SECONDS and not self.busy(now):
                self._set_watching(True, now)
                return
        self.recent = [at for at in self.recent if now - at < FOCUS_STAY_SECONDS]
        if len(self.recent) >= FOCUS_MAX_CORRECTIONS:
            self._set_watching(True, now)
            return
        self.recent.append(now)
        self.corrected_at = now
        restored = bool(self.activate(self.last_other))
        if restored:
            # Chrome left the front: its next activation is a new one, even one the next poll sees.
            self.front_since = None
        self.unlogged += 1
        if self.logged_at is None or now - self.logged_at >= FOCUS_LOG_SECONDS:
            self.log(decision="FOCUS_RESTORED", to_pid=self.last_other, restored=restored, count=self.unlogged)
            self.logged_at, self.unlogged = now, 0


class Refusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


class Proxy:
    def __init__(self, operation, child_argv, audit=None, claim=None, human_check=None, snapshot_timeout=30.0,
                 stdin=None, stdout=None, arm_binding=None, human_check_wait=None, uploads=None,
                 call_timeout=CALL_TIMEOUT_SECONDS, background_chrome=None, launch_background=None,
                 offscreen=False, clock=None, sleep=None, focus_keeper=None):
        self.operation = operation
        self.offscreen = offscreen
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.placed_at = None      # when the windows were last minimized
        self.showing = False       # the CAPTCHA wait has the window on-screen: no minimizing
        self.shown_for_user = False   # the window was put on-screen for the user watching it (FocusKeeper)
        self.last_answer_at = None    # when the browser last answered a forwarded call (FocusKeeper.busy)
        self.stopped = threading.Event()
        self.focus_keeper = focus_keeper   # None: built in run() (macOS, --offscreen); False: none
        self.place_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.audit = open(audit, "a", encoding="utf-8") if audit else None
        self.audit_lock = threading.Lock()
        self.background_chrome = background_chrome
        # Sidedoor's profile, whichever way Chrome starts: the focus keeper finds its Chrome by it.
        self.profile_path = background_chrome or (child_argv[child_argv.index("--user-data-dir") + 1]
                                                  if "--user-data-dir" in child_argv[:-1] else None)
        if background_chrome:
            child_argv = self.start_background_chrome(list(child_argv), background_chrome, launch_background)
        self.snapshot_timeout = snapshot_timeout
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        # The browser server (and the Chrome it starts) gets this server's whole environment, which carries
        # the user's session variables codex was given (browser_runtime.session_environment).
        self.child = subprocess.Popen(child_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                      bufsize=1, encoding="utf-8", env=dict(os.environ))
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

    def start_background_chrome(self, child_argv, path, launch=None):
        """Start Sidedoor's Chrome on `path` without activating it and attach @playwright/mcp to it
        (`--cdp-endpoint`). If that cannot start, @playwright/mcp launches Chrome itself on the same
        profile as before (it may then come to the front; recorded as BACKGROUND_LAUNCH_FAILED)."""
        import browser_runtime
        try:
            if launch is None:
                import setup_steps
                app = browser_runtime.chrome_app(setup_steps.chrome_path() or "")
                if app is None:
                    raise OSError("Google Chrome.app not found")
                endpoint = browser_runtime.launch_background(app, path)
            else:
                endpoint = launch(path)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.log(decision="BACKGROUND_LAUNCH_FAILED", reason=str(error)[:200] or type(error).__name__)
            self.background_chrome = None
            return child_argv + ["--user-data-dir", str(path)]
        self.log(decision="BACKGROUND_LAUNCHED", endpoint=endpoint)
        return child_argv + ["--cdp-endpoint", endpoint]

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
                self.last_answer_at = self.clock()
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
        with self.counter_lock:
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

    # --- the hidden window (FG1 = b, then FG2 = a: minimized) ------------------------------------
    @property
    def user_watching(self):
        keeper = self.focus_keeper
        return bool(keeper and keeper.watching)

    def place_window(self, onscreen=False):
        """Minimize every window of the session's Chrome, or put it on-screen: in front for the CAPTCHA
        wait, or (not in front) for a user who opened it on purpose. Recorded in the audit; never raises.
        Returns the read-back list or None."""
        import browser_runtime
        if not self.offscreen:
            return None
        with self.place_lock:
            # Re-checked under the lock (fg1-captcha, Moderate 1): no minimizing while the CAPTCHA wait or
            # the user shows the window, and no on-screen placement once neither does.
            shown = self.showing or self.user_watching
            if shown != onscreen:
                return None
            front = self.showing
            if onscreen:
                left, top = browser_runtime.ONSCREEN_POSITION
                code = place_code(left, top, *browser_runtime.WINDOW_SIZE, front=front)
            else:
                code = hide_code(*browser_runtime.OFFSCREEN_POSITION, *browser_runtime.WINDOW_SIZE)
            try:
                answer = self.child_call("tools/call", {"name": PLACE_TOOL, "arguments": {"code": code}})
            except (OSError, ValueError) as error:
                answer = {"error": str(error)[:200]}
            placed = placed_windows(answer)
            if not onscreen:
                self.placed_at = self.clock()
        errors = [item for item in placed or [] if "error" in item]
        placed = [item for item in placed or [] if "error" not in item] if placed is not None else None
        if not placed:
            self.log(decision="WINDOW_PLACEMENT_FAILED", onscreen=onscreen, errors=errors)
            return None
        self.log(decision=("WINDOW_ONSCREEN" if front else "WINDOW_SHOWN_FOR_USER") if onscreen else "WINDOW_MINIMIZED",
                 windows=placed, **({"errors": errors} if errors else {}))
        stuck = [item for item in placed if not onscreen and item.get("state") != "minimized"]
        if stuck:
            self.log(decision="WINDOW_NOT_MINIMIZED", windows=stuck)
        return placed

    def keep_hidden(self, force=False):
        """After the client's `initialized` (force) and before a forwarded call: minimize the windows
        again, at most once a second; never while the CAPTCHA wait shows them. While the user watches the
        window on purpose it is not minimized; it is put on-screen once (it was parked off-screen)."""
        if not self.offscreen or self.showing:
            return
        if self.user_watching:
            if not self.shown_for_user and self.place_window(onscreen=True) is not None:
                self.shown_for_user = True
            return
        self.shown_for_user = False
        if not force and self.placed_at is not None and self.clock() - self.placed_at < PLACE_INTERVAL_SECONDS:
            return
        self.place_window()

    # --- the focus keeper (FG2 = a) ----------------------------------------------------------------
    def chrome_pid(self):
        """The pid of the Chrome on Sidedoor's profile (its SingletonLock, checked against the process's
        own --user-data-dir), or None. Cached per pid: one `ps` when the pid changes."""
        import browser_runtime
        if not self.profile_path:
            return None
        pid = browser_runtime.lock_holder(self.profile_path)
        if pid is None:
            return None
        checked = getattr(self, "_chrome_checked", None)
        if checked is None or checked[0] != pid:
            checked = self._chrome_checked = (pid, browser_runtime.holds_profile(pid, self.profile_path))
        return pid if checked[1] else None

    def busy(self, now):
        """A forwarded call is running, or the browser answered one under FOCUS_CALL_GRACE_SECONDS ago:
        Chrome's own activation (a new or selected page) is expected now."""
        return bool(self.in_flight) or (self.last_answer_at is not None
                                        and now - self.last_answer_at < FOCUS_CALL_GRACE_SECONDS)

    def start_focus_keeper(self):
        if self.focus_keeper is None:
            import browser_runtime
            # Never in the test suite (BACKGROUND_ALLOWED False there): a test's guard never reads or
            # changes the real desktop's front app.
            if not (self.offscreen and sys.platform == "darwin" and browser_runtime.BACKGROUND_ALLOWED):
                return None
            mac = MacFrontmost()
            self.focus_keeper = FocusKeeper(self.chrome_pid, mac.front, mac.activate, busy=self.busy,
                                            suspended=lambda: self.showing, log=self.log, clock=self.clock)
        if not self.focus_keeper:
            return None

        def run():
            while not self.stopped.wait(FOCUS_POLL_SECONDS):
                self.focus_keeper.poll()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    # --- judging ------------------------------------------------------------------------------
    def judged_targets(self, name, args):
        r"""The refs a call acts on, exactly where live's rule 1 looked (owner, 2026-10-22: "keep this
        clicking guards consistent with what exists in live harness today. Anything more? remove it").
        Live judged every cua_repl call by INDEXED_ACTION = `\.(?:click|doubleClick|press)\(\s*(\d+)`
        against the element's own AX line (the live harness's linkedin_contacts.contact_control_clicks):
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
            # FG1 = b / FG2 = a: the owner has to tick the box, so the window is un-minimized on-screen and
            # in front for the wait (the focus keeper does not correct meanwhile), and minimized again when
            # it ends, whatever the outcome. A failed first try is
            # retried on a thread of its own while the wait lasts (Moderate 2).
            if self.offscreen and self.place_window(onscreen=True) is None:
                threading.Thread(target=self.retry_onscreen, daemon=True).start()
            try:
                text, error = self.human_check_wait.call(), False
            except Exception:
                text, error = "WAIT_EXPIRED: the wait could not start. Return blocked with code CAPTCHA_REQUIRED.", True
            finally:
                self.showing = False
                self.place_window()
            self.log(decision="HUMAN_CHECK_RETURNED", tool=HUMAN_CHECK_TOOL, text=text[:200])
            self.answer(message, text, error)
        self.log(decision="HUMAN_CHECK_STARTED", tool=HUMAN_CHECK_TOOL)
        # Set before the thread starts (Moderate 1): a call handled meanwhile never hides the window.
        self.showing = True
        threading.Thread(target=run, daemon=True).start()

    def retry_onscreen(self):
        """The CAPTCHA wait's on-screen placement after its first try failed: tried again after each of
        ONSCREEN_RETRY_SECONDS while the wait lasts; recorded WINDOW_PLACEMENT_FAILED (gave_up) if none
        works. The owner's Telegram message also says to click Chrome in the Dock."""
        for pause in ONSCREEN_RETRY_SECONDS:
            self.sleep(pause)
            if not self.showing:
                return
            if self.place_window(onscreen=True) is not None:
                return
        if self.showing:
            self.log(decision="WINDOW_PLACEMENT_FAILED", onscreen=True, gave_up=True)

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
            if method == "notifications/initialized":
                self.keep_hidden(force=True)   # FG2 = a: minimized right after launch, before any page work
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
            refusal = focus_refusal(name, args)
            if refusal is not None:
                raise refusal
            self.judge(name, args)
        except Refusal as refusal:
            self.log(decision="REFUSED", code=refusal.code, tool=name if isinstance(name, str) else repr(name),
                     arguments=args if isinstance(args, dict) else repr(args), detail=refusal.detail)
            self.answer(message, f"REFUSED {refusal.code}: {refusal.detail}", True)
            return
        self.keep_hidden()
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
        self.start_focus_keeper()
        try:
            self.loop()
        finally:
            self.stopped.set()
            try:
                self.stop_child()
            finally:
                self.stop_background_chrome()
        return 0

    def stop_background_chrome(self):
        """The Chrome the guard started is not @playwright/mcp's to close (it only attached): stop it on
        this profile only (browser_runtime.end_session, which checks the command line's profile)."""
        if self.background_chrome:
            import browser_runtime
            browser_runtime.end_session({"browser_driver": {"profile": self.background_chrome}})

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
    parser.add_argument("--background-chrome", help="macOS: start Chrome on this profile without activating it")
    parser.add_argument("--offscreen", action="store_true",
                        help="keep the session's Chrome windows minimized, and the front with the user's app")
    parser.add_argument("child", nargs=argparse.REMAINDER, help="-- then the @playwright/mcp command")
    options = parser.parse_args(argv)
    child = options.child[1:] if options.child[:1] == ["--"] else options.child
    if not child:
        parser.error("give the @playwright/mcp command after --")
    if options.claim and options.operation != "submit":
        parser.error("--claim is for the submit operation only")
    return Proxy(options.operation, child, options.audit, options.claim, options.human_check,
                 uploads=options.uploads, background_chrome=options.background_chrome,
                 offscreen=options.offscreen).run()


if __name__ == "__main__":
    sys.exit(main())
