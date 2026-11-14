"""E15.T5 the live view: a person acts in Sidedoor's hidden Chrome from inside the dashboard, for a sign-in or
a human check that only a person may do (design: docs/product/design/E15-E16-access-walls-design.md, R14, R13,
R12; the spike it productises: spikes/livecheck/REPORT.md, E15.T0, six real cases).

What runs. Chrome is started the product's way and only that way: the browser guard for the `inspect`
operation (`browser_runtime.guard_command`), spoken to over stdio as a session's client speaks to it. Nothing
here adds to or changes that launch (no user agent, switch, client hint or screen value). This module then
attaches as a second DevTools client to the port that Chrome opened (the guard's BACKGROUND_LAUNCHED record),
asks for screen frames, and passes the person's own mouse, wheel, keys and paste back as they happen. It
never clicks, types, solves or waits on anything by itself, runs no model and gives a model no tool.

The seam the cards call (E15.T2; site sessions are E15.T4):
    view = app.state.live_view                      # the dashboard's one LiveView
    target_id = view.offer(job, url, purpose, domain=None, check=None, on_ended=None)
    view.withdraw(target_id)
    GET  /live-view/<target_id>/fragment            # what a card embeds (static/live_view.js on the page)
    POST /live-view/<target_id>/start, POST /live-view/end?reason=done|cancel, WS /live-view/ws?s=<session>
There is no route that takes a URL: a page is opened only for a target the server offered, by its id.
`on_ended(Ended)` gets the result record; `check(LivePage)` (optional) says the page reached its target state;
`on_user_step_finished(domain, job)` is called, while Chrome is still running, where E15.T4 will save the site
session.

Rules kept here, each with a test (tests/test_live_view.py):
- One live session at a time, holding state/browser.lock for its whole life (`run_locks.browser_session`, on a
  thread of its own): other browser work queues behind it as it does behind any session; research and drafting
  never take that lock. Lease: 10 minutes, renewed by the person's input, 30 minutes at most.
- Ends on Done, Cancel, the lease, the person leaving (no viewer for LEAVE_GRACE_SECONDS), the `check` hook, or
  Chrome going away. Every ending stops the frames, closes the second client, stops the guard and its Chrome
  (`browser_runtime.end_session`), releases the lock and returns an `Ended` record.
- A crash: the lock is an OS file lock, gone with the process; the Chrome left behind is closed by
  `browser_runtime.unavailable` (close_orphan + the stale-profile recovery) before the next session starts.
- The spike's seven lessons: input is sent in order and never waited for; the shown page is kept the front
  (visible) window; chrome:// pages are never shown or followed; key codes come from the physical key; the
  DevTools reader never waits; Cmd/Ctrl combinations and function keys stay away from Chrome; a viewer gets a
  frame on joining, reconnects, and has a status line in plain words.
- Privacy: what is typed goes from the dashboard's socket to Chrome on this computer and nowhere else. Only
  event kinds and counts are logged; no frame or screenshot is written to disk or sent anywhere but the
  authenticated socket.

OS-specific code is `supported` only. Windows (owner, 2026-11-12, option A): the guard starts Chrome without a
window on a DevTools port there too, so `supported` says yes whenever this computer's Chrome version, platform
version and display can be read (browser_runtime.headless_user_agent); otherwise the card gets the "could not
show" failure.
"""

import asyncio
import base64
import collections
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import browser_runtime
import form_check_hold
import log
import paths
import run_locks

LOG = log.get(__name__)
# The websocket library writes every frame it sends to its logger at DEBUG, and the frames to Chrome carry what
# the person types. This connection's logger is therefore one that can never write, whatever the log level.
# It is not registered with `logging`, so no logging configuration can switch it back on (a registered logger's
# `disabled` flag was reset by another module's configuration in the full test suite).


class _Silent(logging.Logger):
    def isEnabledFor(self, level):
        return False

    def handle(self, record):
        return None


WIRE_LOG = _Silent("live-view-wire")

OPERATION = "inspect"              # the guard operation a live session starts, exactly as an application read
LEASE_SECONDS = 600                # 10 minutes, renewed while the person is active
LEASE_MAX_SECONDS = 1800           # 30 minutes at most
# Owner, 2026-11-13 ("5mins"): a view the person has stopped using closes by itself AS DONE after this long
# without input: the site's session is kept from the live browser first, the wait is released, the next read
# verifies, and the card comes back if the wall is still there. Never the old "Timed out" that dropped a
# sign-in they had made. Long enough to fetch a code from a phone or an email. Not for an attached view
# (E16.T2), which has its own rule.
IDLE_SECONDS = 300
LEAVE_GRACE_SECONDS = 15           # no viewer for this long: the person left
LOCK_RETRY_SECONDS = 3             # the one short retry for the browser
ENDPOINT_WAIT_SECONDS = 75         # the guard's own Chrome launch allows 60 (BACKGROUND_LAUNCH_SECONDS)
GUARD_CALL_SECONDS = 120
MAX_OUTSTANDING = 64               # input events Chrome has not answered yet; above it mouse moves are dropped
OUTSTANDING_FORGET_SECONDS = 30    # an unanswered event stops counting (its page went away)
FRONT_WATCH_SECONDS = 1.0
STATUS_SECONDS = 1.0
CHECK_SECONDS = 2.0
VIEWER_QUEUE = 8
# What the viewer may send (G6). A paste is a password, a code or a short answer; longer is dropped whole (a cut
# password would be worse than none). Anything that fails a check is dropped and the session goes on.
PASTE_MAX_CHARACTERS = 10000
KEY_MAX_CHARACTERS = 32            # event.key and event.code are short names
WHEEL_MAX = 10000.0                # one wheel event's scroll, either way, in page pixels
MESSAGE_MAX_CHARACTERS = 131072    # one message from the viewer (a full paste, escaped, fits)
SOCKET_MAX_BYTES = 262144          # the dashboard server's websocket frame limit (uvicorn's default is 16 MB)
SCREENCAST = {"format": "jpeg", "quality": 75, "everyNthFrame": 1}
FOLDER = "live-view"               # reports/live-view/session: the guard's own records of the latest session

DONE, CANCEL, LEASE, LEFT, FINISHED, CLOSED = "done", "cancel", "lease", "left", "finished", "closed"
STEP_FINISHED = (DONE, FINISHED)   # endings after which the site session is worth saving (E15.T4)
USER_REASONS = (DONE, CANCEL)      # what the buttons may send

# --- Every sentence a person can see (house style: plain words, the action only) ---------------------------------
READY_WORDS = "Opening the page."
# Owner, 2026-11-13: no "Connected." in front. In a LinkedIn step it read as "LinkedIn is connected".
LIVE_WORDS = "This is the page in Sidedoor's browser."
SLOW_WORDS = "The page is slow to respond. Your clicks and keys are still being sent."
NO_PICTURE_WORDS = "No new picture for %d seconds. The page may be loading."
NO_PAGE_WORDS = "The page closed. Press Done or Cancel."
RECONNECTING_WORDS = "Connection lost. Reconnecting."
# "%s" in a sentence is the button the person presses to open the step again: the step's own name (F7).
OVER_WORDS = "This page has closed. Press %s when you're ready."
BUSY_WORDS = "Sidedoor's browser is busy with other work. Try again shortly."
ONE_AT_A_TIME_WORDS = "Another page is already open in Sidedoor. Finish or cancel it first."
GONE_WORDS = "This application is no longer waiting for you."   # the card's own sentence for a stale press
NOT_AVAILABLE_WORDS = ("This step can't be done inside Sidedoor on this computer yet. "
                       "Apply yourself with the link on the card.")
SITE_WORDS = "Now showing: %s"
SITE_OTHER_WORDS = "Now showing: %s (opened from %s)"
SITE_NOT_SECURE_WORDS = "Not secure. "
SITE_BLANK_WORDS = "Now showing: a blank page"
SITE_OPENED_BY_WORDS = "Now showing: a window opened by %s"
NO_BACKGROUND_WORDS = ("Sidedoor couldn't open its browser in the background on this computer. "
                       "Apply yourself with the link on the card.")
PASTE_TOO_LONG_WORDS = "That paste is too long for this view. Type it, or paste it in smaller parts."
QUIT_SECONDS = 10   # Quit ends a live session first, and never waits longer than this for it
SETTLE_SECONDS = 10   # how long an ending waits to keep or drop the site's captured session before it lets the browser go
HOOK_SECONDS = 60   # how long an ending waits for a card's hook (on_user_step_finished, on_ended); then it goes on
NOT_SHOWN_WORDS = "Sidedoor couldn't open its browser. Try again, or apply yourself with the link on the card."
ENDED_WORDS = {
    DONE: "Done. Sidedoor carries on from here.",
    FINISHED: "Done. Sidedoor carries on from here.",
    CANCEL: "Cancelled.",
    LEASE: "Timed out. Press %s when you're ready.",
    LEFT: "Closed because you left the page. Press %s when you're ready.",
    CLOSED: "Sidedoor's browser closed. Press %s when you're ready.",
}


# An attached view (E16.T2) whose session's Chrome went away: the session is over (sent, stopped or failed),
# so pressing the button again is not what comes next. PROPOSED wording (a part of the sentence above).
ATTACHED_CLOSED_WORDS = "Sidedoor's browser closed."


def ended_words(reason, purpose=None, attached=False):
    """What an ending says, naming the button that opens the step again where it mentions one."""
    if attached and reason == CLOSED:
        return ATTACHED_CLOSED_WORDS
    words = ENDED_WORDS[reason]
    return words % START_LABELS.get(purpose, START_LABEL) if "%s" in words else words


FAILURE_WORDS = "Sidedoor couldn't open its browser for a step that needs you."
FAILURE_TODO = "Open the card and try again, or apply yourself with the link on the card."
# A card with no posting link, for an application that may already have reached the employer
# (`Target.apply_link` false; confirm_read), and the LinkedIn card, which has no posting: no sentence
# the view can show there may tell the person to apply, so each one that does has a form without it.
# Everything else the view says is shared.
NO_LINK_WORDS = {NOT_SHOWN_WORDS: "Sidedoor couldn't open its browser. Try again.",
                 NOT_AVAILABLE_WORDS: "This step can't be done inside Sidedoor on this computer yet.",
                 NO_BACKGROUND_WORDS: "Sidedoor couldn't open its browser in the background on this computer."}
FAILURE_TODO_NO_LINK = "Open the card and try again."


def said(target, words):
    """`words` as this target's card may show them: without "apply yourself" where it has no posting link."""
    return NO_LINK_WORDS.get(words, words) if target is not None and not target.apply_link else words


START_LABELS = {"sign_in": "Sign in", "human_check": "Do the human check"}   # the card's own button words
ATTACHED_PURPOSE = "human_check"   # E16.T2: the purpose of an attached view (`LiveView.offer_attached`)
# E16.T2: the one instruction shown beside an attached view, the owner's words (2026-11-13, "C4: a"). It is the
# only protection against the user pressing the site's own Submit there (his decision "C3: C", AGENTS.md).
ATTACHED_WORDS = "Do the human check. Do not press the site's Submit button."
START_LABEL = "Open the page"
ALL_WORDS = (READY_WORDS, LIVE_WORDS, OVER_WORDS, NOT_AVAILABLE_WORDS, SITE_WORDS, SITE_OTHER_WORDS,
             SITE_NOT_SECURE_WORDS, SITE_BLANK_WORDS, SITE_OPENED_BY_WORDS, NO_BACKGROUND_WORDS, PASTE_TOO_LONG_WORDS, SLOW_WORDS, NO_PICTURE_WORDS, NO_PAGE_WORDS, RECONNECTING_WORDS, BUSY_WORDS,
             ONE_AT_A_TIME_WORDS, GONE_WORDS, NOT_SHOWN_WORDS, FAILURE_WORDS, FAILURE_TODO, FAILURE_TODO_NO_LINK,
             *NO_LINK_WORDS.values(),
             START_LABEL, *ENDED_WORDS.values(), *START_LABELS.values(),
             *browser_runtime.UNAVAILABLE_WORDS.values(), *browser_runtime.UNAVAILABLE_TODO.values())

# Why a start failed. BUSY is a wait that clears on its own (never a Failures row, as that page's own rule says);
# the others are "the step could not be shown" (R12): a Failures row and a count for Metrics.
BUSY, WINDOW_OPEN, COMPONENT_MISSING, NOT_SHOWN, OPEN, UNKNOWN = ("busy", "window_open", "component_missing",
                                                                  "not_shown", "open", "unknown")
UNAVAILABLE = "unavailable"   # this computer cannot show the step at all (G3): never recorded, never "try again"
RECORDED_REASONS = (BUSY, WINDOW_OPEN, COMPONENT_MISSING, NOT_SHOWN)
FAILURE_REASONS = (WINDOW_OPEN, COMPONENT_MISSING, NOT_SHOWN)


def on_user_step_finished(domain, job):
    """Called when a person finished their step (Done, or the `check` hook said so), while the session's Chrome
    is still running. Nothing is done here (E15 join): the site's cookies are kept by the browser guard itself,
    which is the only process that can read them, when the session ends (`LiveView.session_config`), and the
    card's `on_ended` (`user_step._ended`) makes them the saved session once the browser has stopped.

    For whoever fills this in (and `on_ended`): the hook runs on a thread and is waited for at most HOOK_SECONDS.
    One that takes longer is not stopped; it keeps running on its thread AFTER Chrome is stopped and the
    browser's lock is free. It must therefore give up quickly by itself, and must never record a session as
    saved once that point may have passed (check its own deadline before it records anything)."""


def supported(config=None):
    """The one OS-specific question: does the guard start this session's Chrome itself, with a DevTools port a
    second client can reach? macOS and Windows on the real profile: yes. Linux: no (@playwright/mcp starts
    Chrome over a pipe).
    The answer is the guard's own rule for a windowless launch of this operation (`headless_user_agent`): the
    background launch, `inspect` among the configured `headless_operations`, and a readable Chrome version
    (Windows: also its client hints and the real display, read from this computer).
    Anything else is the guard's fallback, a Chrome with a window, which a live view never attaches to (P6)."""
    return browser_runtime.headless_user_agent(config, OPERATION) is not None


# --- The lease ---------------------------------------------------------------------------------------------------

class Lease:
    """10 minutes from the last input, never past 30 minutes from the start."""

    def __init__(self, now, seconds=LEASE_SECONDS, most=LEASE_MAX_SECONDS):
        self.started, self.seconds, self.most = now, seconds, most
        self.ends = min(now + most, now + seconds)

    def touch(self, now):
        self.ends = min(self.started + self.most, now + self.seconds)

    def left(self, now):
        return max(0.0, self.ends - now)


# --- Input: the person's own events, turned into DevTools input calls (pure functions) ---------------------------

# Windows virtual key codes. Named keys by e.key; every character key by its PHYSICAL key (e.code), as Playwright
# maps them, never by the character: "." is 46, the Delete key's code, so it deleted text (spike lesson 4).
KEY_VK = {"Backspace": 8, "Tab": 9, "Enter": 13, "Shift": 16, "Control": 17, "Alt": 18, "Escape": 27, " ": 32,
          "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39,
          "ArrowDown": 40, "Delete": 46, "Meta": 91}
CODE_VK = {"Period": 190, "Comma": 188, "Minus": 189, "Equal": 187, "Slash": 191, "Backslash": 220, "Semicolon": 186,
           "Quote": 222, "BracketLeft": 219, "BracketRight": 221, "Backquote": 192, "Space": 32, "IntlBackslash": 226}
_LETTER, _DIGIT, _NUMPAD = re.compile(r"Key[A-Z]\Z"), re.compile(r"Digit[0-9]\Z"), re.compile(r"Numpad[0-9]\Z")
_FUNCTION_KEY = re.compile(r"F[0-9]{1,2}\Z")
# The only Cmd/Ctrl combinations that reach Chrome (lesson 6); paste arrives as its own event with the text.
COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "z": "undo"}
ALT, CTRL, META, SHIFT = 1, 2, 4, 8
COUNT_NAMES = {"mouse": "mouse", "wheel": "wheel", "key": "keys", "paste": "pastes"}   # what the log may say
MOUSE_TYPES = ("mouseMoved", "mousePressed", "mouseReleased")
BUTTONS = ("none", "left", "middle", "right")


def virtual_key(key, code):
    if _LETTER.match(code):
        return ord(code[3])
    if _DIGIT.match(code):
        return ord(code[5])
    if _NUMPAD.match(code):
        return 96 + int(code[6])
    if code in CODE_VK:
        return CODE_VK[code]
    return KEY_VK.get(key, 0)


def _number(value, low, high):
    """`value` as a number held within [low, high], or None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return min(max(float(value), low), high)


def _strict(name):
    raise ValueError("not a number")   # NaN and Infinity are not JSON; Chrome would refuse the whole message


def read_message(text):
    """One message from the viewer as a dict. Raises ValueError for anything else, too long, or not strict JSON."""
    if not isinstance(text, str) or len(text) > MESSAGE_MAX_CHARACTERS:
        raise ValueError("too long")
    event = json.loads(text, parse_constant=_strict)
    if not isinstance(event, dict):
        raise ValueError("not a message")
    return event


def _modifiers(event):
    value = event.get("mod", 0)
    return value & 15 if isinstance(value, int) and not isinstance(value, bool) else 0


def mouse_params(event, size=(1280, 900)):
    """Input.dispatchMouseEvent, the point held inside the picture (`size`), or None for a malformed event."""
    x, y = _number(event.get("x"), 0, size[0]), _number(event.get("y"), 0, size[1])
    if event.get("type") not in MOUSE_TYPES or x is None or y is None:
        return None
    button = event.get("button") if event.get("button") in BUTTONS else "none"
    buttons, clicks = event.get("buttons", 0), event.get("clicks", 0)
    if isinstance(buttons, bool) or isinstance(clicks, bool):
        return None
    return {"type": event["type"], "x": x, "y": y, "modifiers": _modifiers(event), "button": button,
            "buttons": buttons if isinstance(buttons, int) and 0 <= buttons < 32 else 0,
            "clickCount": clicks if isinstance(clicks, int) and 0 <= clicks < 4 else 0}


def wheel_params(event, size=(1280, 900)):
    values = (_number(event.get("x"), 0, size[0]), _number(event.get("y"), 0, size[1]),
              _number(event.get("dx"), -WHEEL_MAX, WHEEL_MAX), _number(event.get("dy"), -WHEEL_MAX, WHEEL_MAX))
    if None in values:
        return None
    return {"type": "mouseWheel", "x": values[0], "y": values[1], "deltaX": values[2], "deltaY": values[3],
            "modifiers": _modifiers(event)}


def key_params(event):
    """Input.dispatchKeyEvent for one key going down or up, or None when it must not reach Chrome: a function
    key, or Cmd/Ctrl with anything but select-all, copy, cut and undo (they would reach Chrome's own menus)."""
    key, code, modifiers = event.get("key"), event.get("code"), _modifiers(event)
    if not isinstance(key, str) or not isinstance(code, str) or event.get("type") not in ("down", "up"):
        return None
    if not key or len(key) > KEY_MAX_CHARACTERS or len(code) > KEY_MAX_CHARACTERS:
        return None
    if _FUNCTION_KEY.match(key) or _FUNCTION_KEY.match(code):
        return None
    command = None
    if modifiers & (CTRL | META):
        command = COMMANDS.get(key.lower())
        if command is None:
            return None
    code_value = virtual_key(key, code)
    params = {"key": key, "code": code, "windowsVirtualKeyCode": code_value, "nativeVirtualKeyCode": code_value,
              "modifiers": modifiers}
    if event["type"] == "up":
        return dict(params, type="keyUp")
    text = "" if command else key if len(key) == 1 else "\r" if key == "Enter" else ""
    params["type"] = "keyDown" if text else "rawKeyDown"
    if text:
        params["text"] = params["unmodifiedText"] = text
    if command:
        params["commands"] = [command]
    return params


def paste_params(event):
    text = event.get("text")
    return {"text": text} if isinstance(text, str) and 0 < len(text) <= PASTE_MAX_CHARACTERS else None


HOST_SHOWN_CHARACTERS = 100


def host_of(url):
    """(scheme, host) of an address, read the way a browser reads it, the host in ASCII: a backslash is a
    slash, tabs and line ends are not there, and a label with other letters is shown in its xn-- form (never
    letters that look like other letters). ("", "") when there is no host. A host too long for the line keeps
    its END, which is the part that says whose site it is."""
    try:
        text = "".join(character for character in str(url or "") if character not in "\t\r\n").strip().replace("\\", "/")
        parts = urllib.parse.urlsplit(text)
        host = unicodedata.normalize("NFKC", parts.hostname or "").lower()
        labels = [label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
                  for label in host.split(".")]
    except (ValueError, UnicodeError):
        return "", ""
    host = "".join(character for character in ".".join(labels) if 32 < ord(character) < 127)
    if len(host) > HOST_SHOWN_CHARACTERS:
        host = "..." + host[-HOST_SHOWN_CHARACTERS:]
    return parts.scheme.lower(), host


def site_words(url, opened_for, opener_url=None):
    """The line above the picture (G2): the host of the page shown now (`host_of`; never the path or query,
    which can carry tokens), marked when the page is not https, and the host the step was opened for when it is
    another one. A blank page that another page opened (a pop-up its opener writes into) is named by that
    page's host (P4). Plain text: the page shows it with textContent."""
    scheme, host = host_of(url)
    if not host:
        opener_scheme, opener = host_of(opener_url) if opener_url else ("", "")
        return SITE_OPENED_BY_WORDS % opener if opener and opener_scheme in ("https", "http") else SITE_BLANK_WORDS
    words = SITE_WORDS % host if host == (opened_for or "").lower() else SITE_OTHER_WORDS % (host, opened_for)
    return words if scheme == "https" else SITE_NOT_SECURE_WORDS + words


def shown_url(url):
    """True for an address the view may show or follow: a web page, or a page not yet loaded. Never chrome://
    (the profile opens chrome://help by itself), devtools:// or an extension page (lesson 3)."""
    return isinstance(url, str) and (url in ("", "about:blank") or url.startswith(("https://", "http://")))


def showable(info):
    return isinstance(info, dict) and info.get("type") == "page" and shown_url(info.get("url", ""))


# --- The guard, spoken to as a session's client speaks to it -----------------------------------------------------

class StartFailed(Exception):
    pass


class WindowedChrome(StartFailed):
    """The guard's Chrome has a window (its fallback launch): not something a live view may attach to."""


def session_endpoint(directory):
    """The DevTools endpoint of the Chrome a RUNNING session's guard started without a window (the
    BACKGROUND_LAUNCHED record in that session's own guard audit, headless true), or None: no record yet, or a
    Chrome with a window, which is no endpoint for a live view (as `Guard.endpoint`)."""
    try:
        lines = (Path(directory) / browser_runtime.GUARD_AUDIT).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("decision") == "BACKGROUND_LAUNCHED" and isinstance(row.get("endpoint"), str):
            return row["endpoint"] if row.get("headless") is True else None
    return None


def session_page(endpoint, timeout=2):
    """The address of a web page (http or https) the Chrome at `endpoint` now has open, or None: nothing but
    blank or internal pages yet, or the Chrome did not answer. An attached view is offered only once there is
    one (owner, 2026-11-13: never a blank page with "Now showing: a blank page")."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint + "/json/list", timeout=timeout) as answer:
            pages = json.loads(answer.read())
    except (OSError, ValueError):
        return None
    for page in pages if isinstance(pages, list) else []:
        if isinstance(page, dict) and page.get("type") == "page" and str(page.get("url", "")).startswith(("http://", "https://")):
            return page["url"]
    return None


class Guard:
    """`sidedoor browser-guard --operation inspect -- <@playwright/mcp>`: the product's own launch. MCP JSON-RPC
    over stdio. Its answers (page snapshots) are dropped unread; nothing typed ever passes through it."""

    def __init__(self, command, directory):
        self.command, self.directory, self.process = list(command), Path(directory), None
        self.next_id, self.pending, self.reader, self.stderr = 1, {}, None, None

    async def start(self):
        self.stderr = open(self.directory / "guard.stderr", "wb")
        # This guard's own exit may wait for its Chrome to close itself and write its cookies (`stop` waits for
        # the guard; no session time limit runs here): browser_guard.close_own_chrome.
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.stderr,
            limit=64 * 1024 * 1024, start_new_session=True,
            env=dict(os.environ, **{browser_runtime.CLEAN_CLOSE_VARIABLE: "1"}))
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "sidedoor-live-view", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())

    async def call(self, method, params, timeout=GUARD_CALL_SECONDS):
        ident, self.next_id = self.next_id, self.next_id + 1
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, OSError) as error:
            raise StartFailed(type(error).__name__) from None
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        while True:
            try:
                line = await self.process.stdout.readline()
            except (ValueError, OSError):
                line = b""
            if not line:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(OSError("the browser stopped"))
                return
            try:
                message = json.loads(line)
            except ValueError:
                continue
            future = self.pending.get(message.get("id")) if isinstance(message, dict) and "method" not in message else None
            if future is not None and not future.done():
                future.set_result(message)

    def endpoint(self):
        """The DevTools endpoint of the Chrome the guard started without a window (its BACKGROUND_LAUNCHED record
        with headless true), or None. A record without it is the guard's fallback: Chrome in a minimized window
        that the guard itself keeps minimized. Bringing a page to the front there would fight that and put a
        window on the desktop, so it is not an endpoint for a live view (G1)."""
        try:
            lines = (self.directory / browser_runtime.GUARD_AUDIT).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            with contextlib.suppress(ValueError, AttributeError):
                row = json.loads(line)
                if row.get("decision") == "BACKGROUND_LAUNCHED" and isinstance(row.get("endpoint"), str):
                    return row["endpoint"] if row.get("headless") is True else False
                if row.get("decision") == "HEADLESS_LAUNCH_FAILED":
                    # The windowless launch failed: whatever follows has a window (macOS: the minimized
                    # fallback; Windows: @playwright/mcp's own window, opened only when a page is asked
                    # for, which never happens here). No endpoint will come: answered at once.
                    return False
        return None

    async def wait_endpoint(self, seconds):
        """The endpoint, or None when there is none in time, the guard ended, or its Chrome has a window."""
        deadline = time.monotonic() + seconds
        while True:
            found = self.endpoint()
            if found is not None or time.monotonic() >= deadline or self.process.returncode is not None:
                return (found if found is not None else self.endpoint()) or None
            await asyncio.sleep(0.1)

    async def navigate(self, url):
        answer = await self.call("tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
        result = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(result, dict) or result.get("isError"):
            raise StartFailed("navigate")

    async def stop(self, grace=15.0):
        """Close its input (it then stops the browser server and its Chrome), wait, then end that one process."""
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), grace)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()   # the exact guard process this session started
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
        if self.reader is not None:
            self.reader.cancel()
        if self.stderr is not None:
            self.stderr.close()


# --- The second DevTools client ----------------------------------------------------------------------------------

def debugger_url(endpoint):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # this computer only: never a proxy
    with opener.open(endpoint + "/json/version", timeout=10) as answer:
        return json.load(answer)["webSocketDebuggerUrl"]


# --- An attached view never presses the site's Submit (owner, 2026-11-13, "a+b") -----------------------------------
# The control is identified by STRUCTURE at the moment of the press, in the page itself, never by its words:
# what is under the point (through open shadow roots and same-origin frames, a label counted as its control) is an
# <input type=submit|image>, or a <button> whose type is submit and that belongs to a form. And by key: Enter or
# Space on such a control, or Enter in an <input> that belongs to a form (the browser's implicit submission).
# Everything else is passed on as ever. Not covered (AGENTS.md): a control that submits by script alone (no form,
# or not a submit control), and a form inside a frame of another site.
SUBMIT_MARK = "/*sidedoor-submit-check*/"
_SUBMIT_JS = ("function sub(e){if(!e||!e.closest)return false;var l=e.closest('label');if(l&&l.control)e=l.control;"
              "var c=e.closest('button,input');if(!c)return false;"
              "if(c.tagName==='INPUT')return c.type==='submit'||c.type==='image';"
              "return c.type==='submit'&&!!c.form;}")
SUBMIT_AT_POINT = ("(function(x,y){" + _SUBMIT_JS + "var e=document.elementFromPoint(x,y);"
                   "for(var i=0;i<8&&e;i++){"
                   "if(e.shadowRoot){var s=e.shadowRoot.elementFromPoint(x,y);if(s&&s!==e){e=s;continue;}}"
                   "if(e.tagName==='IFRAME'||e.tagName==='FRAME'){var d=null;try{d=e.contentDocument;}catch(_){}"
                   "if(!d)return false;var r=e.getBoundingClientRect();x-=r.left+e.clientLeft;y-=r.top+e.clientTop;"
                   "e=d.elementFromPoint(x,y);continue;}break;}"
                   "return sub(e);})(%d,%d)" + SUBMIT_MARK)
SUBMIT_BY_KEY = ("(function(k){" + _SUBMIT_JS + "var a=document.activeElement;"
                 "for(var i=0;i<8&&a;i++){"
                 "if(a.shadowRoot&&a.shadowRoot.activeElement){a=a.shadowRoot.activeElement;continue;}"
                 "if(a.tagName==='IFRAME'||a.tagName==='FRAME'){var d=null;try{d=a.contentDocument;}catch(_){}"
                 "if(!d)return false;a=d.activeElement;continue;}break;}"
                 "if(!a)return false;if(sub(a))return true;if(k!=='Enter')return false;"
                 "return a.tagName==='INPUT'&&!!a.form&&['button','reset','file'].indexOf(a.type)<0;})(%s)" + SUBMIT_MARK)
SUBMIT_KEYS = ("Enter", " ")
RESUME_WATCH_SECONDS = 1.0   # how often an attached view looks whether the AI has resumed
ATTACHED_CLOSE_SECONDS = 0.5   # an attached view never waits longer on a Chrome the AI is busy in
SUBMIT_CHECK_SECONDS = 1.5   # no answer in this time: the press is not passed on (the safe side)


class Chrome:
    """A second DevTools client on the guard's Chrome: frames of one page out, the person's input in.

    Two ways to send. `call` is awaited and is used only from tasks of its own (attach, bring to front, start the
    frames). `fire` writes and returns: every input event goes through it, in order, and its answer is only
    counted when it arrives (lesson 1). The reader (`_on_message`) is a plain function: it cannot wait on
    anything, it only records and starts tasks (lesson 5)."""

    def __init__(self, endpoint, on_frame=None, on_lost=None, connect=None, clock=None, on_change=None,
                 protect_submit=False, on_refused=None, input_open=None, on_acted=None):
        self.endpoint, self.on_frame, self.on_lost, self.on_change = endpoint, on_frame, on_lost, on_change
        # An attached view: presses and Enter/Space are checked first, in order, by one task (`_gate`).
        self.protect_submit, self.on_refused, self.gate = protect_submit, on_refused, None
        # An attached view: `input_open()` False means nothing of the person's is passed on (the AI has resumed);
        # `on_acted()` is told of every press and key that was.
        self.input_open, self.on_acted = input_open, on_acted
        self.size = browser_runtime.WINDOW_SIZE   # the picture's size in page pixels, from the latest frame
        self.connect, self.clock = connect, clock or time.monotonic
        self.socket, self.next_id = None, 1
        self.pending, self.fired = {}, {}      # awaited calls; input events not answered yet (id -> sent at)
        self.outbox = asyncio.Queue()
        self.pages, self.sessions = {}, {}     # every page target (chrome:// ones too); targetId -> sessionId
        self.followed, self.order = set(), []  # pages already followed once; pages shown, latest last
        self.active = None                     # the targetId being shown
        self.counts, self.tasks, self.closed = {}, set(), False
        self.showing = asyncio.Lock()

    def count(self, key, by=1):
        self.counts[key] = self.counts.get(key, 0) + by

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        if self.connect is None:
            from websockets.asyncio.client import connect
            self.connect = lambda url: connect(url, max_size=64 * 1024 * 1024, proxy=None, logger=WIRE_LOG)
        url = await asyncio.to_thread(debugger_url, self.endpoint)
        self.socket = await self.connect(url)
        self.spawn(self._read())
        self.spawn(self._write())
        await self.call("Target.setDiscoverTargets", {"discover": True})
        found = await self.call("Target.getTargets", {}) or {}
        for info in found.get("targetInfos", []):
            if isinstance(info, dict) and info.get("type") == "page":
                self.pages[info["targetId"]] = info
        self.followed.update(self.pages)
        first = self._best()
        if first:
            await self.show(first)
        self.spawn(self._front_watch())

    def _best(self):
        """The page to show when none is chosen: the latest shown that is still there, else the latest web page,
        else the latest page that may be shown."""
        for target in reversed(self.order):
            if showable(self.pages.get(target)):
                return target
        candidates = [target for target, info in self.pages.items() if showable(info)]
        web = [target for target in candidates if self.pages[target].get("url", "").startswith("http")]
        return (web or candidates or [None])[-1]

    # --- sending ---
    def _post(self, method, params, session=None):
        ident, self.next_id = self.next_id, self.next_id + 1
        message = {"id": ident, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.outbox.put_nowait(json.dumps(message))
        return ident

    async def call(self, method, params, session=None, timeout=20):
        """An awaited DevTools call: its result, or None on an error or after `timeout`."""
        if self.closed:
            return None
        future = asyncio.get_running_loop().create_future()
        ident = self._post(method, params, session)
        self.pending[ident] = future
        try:
            message = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.count("timeouts")
            return None
        finally:
            self.pending.pop(ident, None)
        return message.get("result") if isinstance(message.get("result"), dict) else None

    def fire(self, method, params, session):
        """Send and return at once; the answer is consumed by the reader. Never waited for."""
        self.fired[self._post(method, params, session)] = self.clock()

    @property
    def outstanding(self):
        return len(self.fired)

    async def _write(self):
        while True:
            text = await self.outbox.get()
            try:
                await self.socket.send(text)
            except Exception:
                return

    async def _read(self):
        try:
            async for raw in self.socket:
                try:
                    self._on_message(json.loads(raw))
                except Exception as error:   # one odd message never stops the reader; never its content
                    self.count("reader_errors")
                    LOG.warning("LIVE_VIEW_READER_ERROR (%s)", type(error).__name__)
        except Exception:
            pass
        if not self.closed:
            self.closed = True
            if self.on_lost is not None:
                self.on_lost()

    def _on_message(self, message):
        """Plain function on purpose: the reader records and starts tasks, it never waits (lesson 5)."""
        if "id" in message:
            future = self.pending.get(message["id"])
            if future is not None:
                if not future.done():
                    future.set_result(message)
            else:
                self.fired.pop(message["id"], None)
            return
        method, params = message.get("method"), message.get("params") or {}
        if method == "Page.screencastFrame":
            session = message.get("sessionId")
            self._post("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, session)
            if session and session == self.sessions.get(self.active) and self.on_frame is not None:
                self.count("frames")
                meta = params.get("metadata") or {}
                width, height = _number(meta.get("deviceWidth"), 1, 16384), _number(meta.get("deviceHeight"), 1, 16384)
                if width and height:
                    self.size = (width, height)
                self.on_frame(base64.b64decode(params.get("data") or ""), params.get("metadata") or {})
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") != "page":
                return
            self.pages[info["targetId"]] = info
            self._maybe_follow(info)
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            target = info.get("targetId")
            if target not in self.pages:
                return
            self.pages[target] = info
            if target == self.active and showable(info):
                self._changed()
            if target == self.active and not showable(info):
                self.count("left_internal_page")
                self.active = None   # first: a page of Chrome's own is never named, not even for a moment
                self._changed()
                if target in self.sessions:
                    self.spawn(self.call("Page.stopScreencast", {}, self.sessions[target], timeout=3))
                self.spawn(self.show_other())
            else:
                self._maybe_follow(info)
        elif method == "Target.targetDestroyed":
            gone = params.get("targetId")
            self.pages.pop(gone, None)
            self.sessions.pop(gone, None)
            if gone in self.order:
                self.order.remove(gone)
            if gone == self.active:
                self.active = None
                self._changed()
                self.spawn(self.show_other())

    def _changed(self):
        """The page shown, or its address, changed: the line that names the site is sent again at once."""
        if self.on_change is not None:
            self.on_change()

    def _maybe_follow(self, info):
        """A page that opened during the session is shown once: a pop-up (it has an opener; "Sign in with Google"
        opens one) as soon as it exists, any other new page once it holds a web address. Never a chrome:// page."""
        target = info["targetId"]
        if target in self.followed or not showable(info):
            return
        if info.get("openerId") or info.get("url", "").startswith("http"):
            self.followed.add(target)
            self.count("pages_followed")
            self.spawn(self.show(target))

    # --- showing a page ---
    async def show(self, target):
        async with self.showing:
            if self.closed or not showable(self.pages.get(target)):
                return False
            session = self.sessions.get(target)
            if session is None:
                attached = await self.call("Target.attachToTarget", {"targetId": target, "flatten": True}) or {}
                session = attached.get("sessionId")
                if not session or not showable(self.pages.get(target)):
                    return False
                self.sessions[target] = session
            previous = self.sessions.get(self.active) if self.active != target else None
            if previous:
                await self.call("Page.stopScreencast", {}, previous, timeout=3)
            self.active = target
            self._changed()
            if target in self.order:
                self.order.remove(target)
            self.order.append(target)
            # Headless Chrome treats only the front window as visible; a hidden one answers each mouse event
            # after 5 s and keys queue behind it (lesson 2). Pop-ups start hidden.
            await self.front(target, session)
            await self.call("Page.enable", {}, session)
            await self.call("Page.startScreencast", SCREENCAST, session)
            return True

    async def show_other(self):
        target = self._best()
        if target and self.active is None:
            await self.show(target)

    async def front(self, target, session):
        await self.call("Target.activateTarget", {"targetId": target})
        await self.call("Page.bringToFront", {}, session)

    async def _front_watch(self):
        """Every second: when the shown page is not the visible one any more (Chrome opened a page of its own
        and took the front), put it back. Also forgets input events whose page never answered."""
        while not self.closed:
            await asyncio.sleep(FRONT_WATCH_SECONDS)
            now = self.clock()
            for ident in [ident for ident, sent in self.fired.items() if now - sent > OUTSTANDING_FORGET_SECONDS]:
                del self.fired[ident]
            target, session = self.active, self.sessions.get(self.active)
            if not session:
                continue
            answer = await self.call("Runtime.evaluate", {"expression": "document.visibilityState",
                                                          "returnByValue": True}, session, timeout=3) or {}
            state = (answer.get("result") or {}).get("value")
            if state and state != "visible" and self.active == target and not self.closed:
                await self.front(target, session)
                self.count("refronted")

    async def refresh(self):
        """A still page sends no frames: restart them so a viewer that joins gets one at once (lesson 7)."""
        session = self.sessions.get(self.active)
        if session:
            await self.call("Page.stopScreencast", {}, session, timeout=3)
            await self.call("Page.startScreencast", SCREENCAST, session)

    async def stop_frames(self):
        session = self.sessions.get(self.active)
        if session and not self.closed:
            await self.call("Page.stopScreencast", {}, session, timeout=2)

    def url(self):
        info = self.pages.get(self.active)
        return info.get("url") if showable(info) else None

    def opener_url(self):
        """The address of the page that opened the shown one, when that page is still there and may be shown."""
        opener = self.pages.get((self.pages.get(self.active) or {}).get("openerId"))
        return opener.get("url") if showable(opener) else None

    # --- the person's input ---
    def input(self, event):
        """One event from the viewer, passed on as it came. Returns its kind when it was the person acting (it
        renews the lease), else None. A plain function: it never waits for Chrome. Never logged with content."""
        session = self.sessions.get(self.active)
        kind = event.get("t") if isinstance(event, dict) else None
        if not session or self.closed or kind not in ("mouse", "wheel", "key", "paste"):
            return None
        if self.input_open is not None and not (kind == "mouse" and event.get("type") == "mouseMoved"):
            try:
                if not self.input_open():
                    self.count("input_closed")   # the AI is working on the page: nothing of the person's reaches it
                    return None
            except Exception:
                return None
        if kind == "mouse":
            params = mouse_params(event, self.size)
            if params is None:
                return None
            if params["type"] == "mouseMoved" and self.outstanding >= MAX_OUTSTANDING:
                self.count("moves_dropped")   # only pointer moves are ever dropped; clicks and keys never
                return kind
            if self.protect_submit and params["type"] != "mouseMoved":
                self._gated("mouse", "Input.dispatchMouseEvent", params, session)
            else:
                self.fire("Input.dispatchMouseEvent", params, session)
        elif kind == "wheel":
            params = wheel_params(event, self.size)
            if params is None:
                return None
            self.fire("Input.dispatchMouseEvent", params, session)
        elif kind == "key":
            params = key_params(event)
            if params is None:
                self.count("keys_not_sent")
                return None
            if self.protect_submit:
                self._gated("key", "Input.dispatchKeyEvent", params, session)
            else:
                self.fire("Input.dispatchKeyEvent", params, session)
        else:
            params = paste_params(event)
            if params is None:
                return None
            self.fire("Input.insertText", params, session)
        self.count(COUNT_NAMES[kind])
        return kind

    # --- an attached view: the site's Submit is never pressed from here ---
    def _gated(self, kind, method, params, session):
        if self.gate is None:
            self.gate = asyncio.Queue()
            self.spawn(self._gate())
        self.gate.put_nowait((kind, method, params, session))

    async def _would_submit(self, expression, session):
        """True unless the page itself answers false: no answer, an error or anything else is not passed on."""
        answer = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session,
                                 timeout=SUBMIT_CHECK_SECONDS)
        return not (isinstance(answer, dict) and (answer.get("result") or {}).get("value") is False)

    def _refuse(self):
        self.count("submit_refused")
        if self.on_refused is not None:
            with contextlib.suppress(Exception):
                self.on_refused()

    async def _gate(self):
        """The presses and keys of an attached view, in the order they came. A press on the site's submit
        control is dropped with its release; Enter or Space that would submit is dropped until that key comes up."""
        press_refused, keys_refused = False, set()
        while not self.closed:
            kind, method, params, session = await self.gate.get()
            try:
                if kind == "mouse":
                    if params["type"] == "mousePressed":
                        press_refused = await self._would_submit(SUBMIT_AT_POINT % (params["x"], params["y"]), session)
                        if press_refused:
                            self._refuse()
                            continue
                    elif press_refused:
                        press_refused = params["type"] != "mouseReleased" and press_refused
                        continue
                elif params.get("key") in SUBMIT_KEYS:
                    key = params["key"]
                    if params["type"] == "keyUp":
                        if key in keys_refused:
                            keys_refused.discard(key)
                            continue
                    elif key in keys_refused or await self._would_submit(SUBMIT_BY_KEY % json.dumps(key), session):
                        if key not in keys_refused:
                            self._refuse()
                        keys_refused.add(key)
                        continue
                if self.input_open is not None and not self.input_open():
                    self.count("input_closed")   # closed while this event waited its turn
                    continue
                if not self.closed:
                    self.fire(method, params, session)
                    if self.on_acted is not None and params.get("type") in ("mousePressed", "keyDown", "rawKeyDown"):
                        with contextlib.suppress(Exception):
                            self.on_acted()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.count("submit_check_failed")   # nothing is passed on for an event that could not be checked

    async def close(self, wait=5):
        self.closed = True
        for task in list(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        if self.socket is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.socket.close(), wait)


# --- Targets, results, failures ----------------------------------------------------------------------------------

@dataclasses.dataclass
class Target:
    """A step that is waiting on the person: offered by the server (a card), found by its id only."""
    id: str
    job: object
    url: str
    purpose: str
    domain: str
    check: object = None       # optional: check(LivePage) -> truthy when the page reached its target state
    on_ended: object = None    # optional: on_ended(Ended)
    cleared: object = None     # optional: what the site asked for (a stop code), kept with a session saved on Done
    before_stop: object = None  # optional: before_stop(endpoint), on Done only, while this step's Chrome still runs
    apply_link: bool = True    # False: the card has no posting link and must never say "apply yourself" (`said`)
    attach: object = None      # E16.T2: the folder of a RUNNING session whose Chrome this view shows (attach mode)

    @property
    def start_label(self):
        return START_LABELS.get(self.purpose, START_LABEL)


@dataclasses.dataclass
class LivePage:
    """What a `check` hook is given: the shown page's address and the guard's own call (awaitable
    `guard(method, params)`), for E15.T4. Never the picture, never what was typed."""
    url: object
    domain: str
    job: object
    guard: object


@dataclasses.dataclass
class Ended:
    reason: str
    final_url: object
    domain: str
    job: object
    purpose: str
    seconds: float
    counts: dict
    words: str
    site_session_saved: bool = False   # the site's cookies kept in this step are now its saved session


@dataclasses.dataclass
class Failed:
    reason: str
    words: str
    final: bool = False   # trying once more cannot help (and would start a Chrome with a window again)


TABLE = """CREATE TABLE IF NOT EXISTS live_view_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, reason TEXT NOT NULL, domain TEXT, job_key TEXT,
    purpose TEXT)"""


def record_failure(database, reason, domain=None, job=None, purpose=None, now=None):
    """One row per start that could not show the step (after its one retry). Never raises."""
    if database is None:
        return
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        with database:
            database.execute(TABLE)
            database.execute("INSERT INTO live_view_failures(at,reason,domain,job_key,purpose) VALUES (?,?,?,?,?)",
                             (now, reason, domain, job if isinstance(job, str) else None, purpose))
    except sqlite3.Error as error:
        LOG.warning("LIVE_VIEW_FAILURE_NOT_RECORDED (%s)", type(error).__name__)


def failure_counts(database, since=None):
    """For the Metrics tab (E15.T11): how often the step could not be shown, {"total", "busy", "by_reason",
    "by_domain"}. `total` and `by_domain` count real failures only; a busy browser is counted apart."""
    counts = {"total": 0, "busy": 0, "by_reason": {}, "by_domain": {}}
    try:
        if database is None or not database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_view_failures'").fetchone():
            return counts
        rows = database.execute("SELECT reason,domain,COUNT(*) FROM live_view_failures WHERE at>=? GROUP BY reason,domain",
                                (since or "",)).fetchall()
    except sqlite3.Error:
        return counts
    for reason, domain, number in rows:
        counts["by_reason"][reason] = counts["by_reason"].get(reason, 0) + number
        if reason == BUSY:
            counts["busy"] += number
            continue
        counts["total"] += number
        counts["by_domain"][domain or ""] = counts["by_domain"].get(domain or "", 0) + number
    return counts


def status_words(live, outstanding, picture_age):
    """The status line, in plain words, so a stall is visible to the person (lesson 7)."""
    if not live:
        return NO_PAGE_WORDS
    if outstanding >= MAX_OUTSTANDING // 2:
        return SLOW_WORDS
    if picture_age is not None and picture_age >= 20:
        return NO_PICTURE_WORDS % int(picture_age)
    return LIVE_WORDS


# --- The browser lock, held on a thread of its own ---------------------------------------------------------------

class Hold:
    """state/browser.lock for one live session, through `run_locks.browser_session` on one thread for the whole
    hold: that context is per thread, and `browser_runtime.unavailable` (run on the same thread, inside the
    lock, as every session does before it starts Chrome) closes a Chrome an earlier crash left only when the
    calling thread is the one holding the lock."""

    def __init__(self, root, config, wait_seconds=0, run=None):
        self.root, self.config, self.wait_seconds, self.run = root, config, wait_seconds, run
        self.release, self.taken = threading.Event(), concurrent.futures.Future()
        self.thread = threading.Thread(target=self._run, daemon=True, name="live-view-browser")

    def _run(self):
        try:
            with run_locks.browser_session(self.root, wait_seconds=self.wait_seconds) as held:
                if not held:
                    self.taken.set_result(BUSY)
                    return
                reason = browser_runtime.unavailable(self.config, self.run, self.root)
                self.taken.set_result(reason)
                if reason is None:
                    self.release.wait()
        except Exception as error:
            if not self.taken.done():
                self.taken.set_exception(error)

    def take(self):
        """None when the lock is held and the browser can start; else "busy", "PROFILE_IN_USE" or
        "RUNTIME_MISSING" (nothing is held then)."""
        self.thread.start()
        return self.taken.result()

    def drop(self):
        self.release.set()
        if self.thread.is_alive():
            self.thread.join(10)


# --- One live session at a time ----------------------------------------------------------------------------------

class Session:
    def __init__(self, target, lease, hold, clock):
        self.id = secrets.token_urlsafe(16)
        self.target, self.lease, self.hold, self.clock = target, lease, hold, clock
        self.guard = self.chrome = None
        self.viewers = {}            # viewer -> its queue of messages (text or picture bytes)
        self.tasks, self.closing = set(), False
        self.teardown = None         # the one task that stops everything this session started (P1)
        self.ending = None           # the one task that ends this session
        self.started, self.last_frame, self.meta = clock(), None, None
        self.last_input = self.started   # the person's latest input (the idle close counts from it)
        self.counts = {"input": 0, "viewers": 0}
        self.leaving = None
        self.reason, self.saved = None, False   # how it ended; whether its capture became the saved session
        self.settling = None                    # a settle still writing when its wait ran out (D15)

    def on_frame(self, data, meta):
        """A picture of the page: to the viewers' queues only. Never kept, never written anywhere."""
        self.last_frame = self.clock()
        shape = {"t": "meta", "width": meta.get("deviceWidth"), "height": meta.get("deviceHeight")}
        if shape != self.meta:
            self.meta = shape
            self.say(shape)
        for queue in self.viewers.values():
            _offer(queue, data)

    def site(self):
        if self.chrome is None:
            return SITE_BLANK_WORDS
        return site_words(self.chrome.url(), self.target.domain, self.chrome.opener_url())

    def say_site(self):
        self.say({"t": "site", "words": self.site()})

    def say(self, message):
        text = json.dumps(message)
        for queue in self.viewers.values():
            _offer(queue, text)


class Outbox:
    """What one viewer is still to be sent, in memory only. Full: the oldest picture goes first (a slow viewer
    loses old pictures and never holds up the page), then the oldest message."""

    def __init__(self, size=VIEWER_QUEUE):
        self.items, self.size, self.ready = collections.deque(), size, asyncio.Event()

    def put(self, item):
        if len(self.items) >= self.size:
            for index, old in enumerate(self.items):
                if isinstance(old, bytes):
                    del self.items[index]
                    break
            else:
                self.items.popleft()
        self.items.append(item)
        self.ready.set()

    async def get(self):
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        return self.items.popleft()


async def _finish(task):
    """Wait for `task` to the end, whatever happens to the waiter: a cancellation of the waiter is kept until
    the task is done, then raised. The task itself is never cancelled from here. Returns its result."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


def _offer(queue, item):
    queue.put(item)


def domain_of(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class LiveView:
    """The dashboard's live view. `offer` is what a card calls; the routes (`register`) do the rest."""

    def __init__(self, home=None, root=None, config=None, guard_command=None, connect=None, clock=None,
                 lease_seconds=LEASE_SECONDS, lease_max_seconds=LEASE_MAX_SECONDS,
                 leave_grace_seconds=LEAVE_GRACE_SECONDS, lock_retry_seconds=LOCK_RETRY_SECONDS,
                 endpoint_wait_seconds=ENDPOINT_WAIT_SECONDS, check_seconds=CHECK_SECONDS, ps=None,
                 is_supported=None, idle_seconds=IDLE_SECONDS):
        self.home, self.root, self.config = home, paths.data(root, home), config
        self.guard_command = guard_command or (
            lambda config, directory: browser_runtime.guard_command(config, directory, OPERATION))
        self.connect, self.clock = connect, clock or time.monotonic
        self.lease_seconds, self.lease_max_seconds = lease_seconds, lease_max_seconds
        self.leave_grace_seconds, self.lock_retry_seconds = leave_grace_seconds, lock_retry_seconds
        self.endpoint_wait_seconds, self.check_seconds, self.ps = endpoint_wait_seconds, check_seconds, ps
        self.is_supported = is_supported or supported
        self.idle_seconds = idle_seconds
        self.targets, self.session, self.last = {}, None, None
        self._starting = self._ending = None
        self.endings = set()          # the tasks that end a session by itself (G7): held until they finish
        self._start_task, self._start_cancelled, self._start_target = None, False, None
        self._end_task = None
        self._loop = None             # the dashboard's event loop, known from the first start
        self.finishing = set()        # (job, purpose) of a finished step whose ending is still being recorded (D22)

    def available(self, purpose=None):
        """True when this computer can show a step inside the dashboard. False: a card shows no Sign in or Do
        the check button (`offer` returns None, and a start says NOT_AVAILABLE_WORDS). `purpose` is accepted for
        the card seam (user_step.available(purpose)); every purpose has the same answer today."""
        return bool(self.is_supported(self.config))

    # --- the seam for cards (E15.T2) ---
    def offer(self, job, url, purpose, domain=None, check=None, on_ended=None, cleared=None, before_stop=None,
              apply_link=True):
        """Register a step that waits on the person; returns the id the fragment and the routes use, or None
        when this computer cannot show it (`available`). The address comes from the server's own record of the
        job, never from a request."""
        if not self.available(purpose) or (job, purpose) in self.finishing:
            return None
        if not isinstance(url, str) or not url.startswith("https://") or not domain_of(url):
            raise ValueError("a live view opens an https page only")
        for known in self.targets.values():   # a card offers its step on every render: one target, one id
            if (known.job, known.url, known.purpose) == (job, url, purpose):
                known.check, known.on_ended, known.cleared, known.before_stop = check, on_ended, cleared, before_stop
                known.apply_link = apply_link
                return known.id
        target = Target(secrets.token_urlsafe(16), job, url, purpose, domain or domain_of(url), check, on_ended, cleared,
                        before_stop, apply_link)
        self.targets[target.id] = target
        return target.id

    def offer_attached(self, job, directory, domain, on_ended=None):
        """E16.T2 (owner, 2026-11-12): register the view of a RUNNING attended submit session, whose folder is
        `directory`. Attach mode: the view is a second DevTools client on THAT session's Chrome (`session_endpoint`).
        It takes no browser lock, starts no guard and no Chrome, saves no site session, and every ending (Done,
        Cancel, the lease, the person leaving, the session's Chrome going away) only disconnects this client: the
        session and its Chrome go on. Done also tells the session's hold that the user is done
        (form_check_hold.finish). The person's clicks and keys are passed on as in every other live view, except
        a press on the site's own submit control and the keys that would submit the form (owner, 2026-11-13,
        "a+b": `Chrome._gate`). Returns the target id, or None when this computer cannot show it."""
        if not self.available(ATTACHED_PURPOSE) or (job, ATTACHED_PURPOSE) in self.finishing:
            return None
        directory = Path(directory)
        for known in self.targets.values():
            if (known.job, known.purpose, known.attach) == (job, ATTACHED_PURPOSE, directory):
                known.on_ended = on_ended
                return known.id
        target = Target(secrets.token_urlsafe(16), job, "", ATTACHED_PURPOSE, domain, on_ended=on_ended,
                        apply_link=False, attach=directory)
        self.targets[target.id] = target
        return target.id

    def withdraw(self, target_id):
        """The step is no longer waiting (its job was abandoned, released or closed another way). If its
        view is open, it ends as a Cancel: nothing is saved and nothing released. From any thread."""
        target = self.targets.pop(target_id, None)
        session = self.session
        if target is None or session is None or session.target is not target or session.closing:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._end_soon(CANCEL, session)
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._end_soon, CANCEL, session)

    def _drop_stale_capture(self, target):
        """Before the guard starts, with the browser held (no read is running): a capture an earlier
        session left for this site is dropped, so only cookies kept in THIS step can become the saved
        session (`site_sessions.settle`: the saved session itself is kept)."""
        import site_sessions
        site = site_sessions.site_key(target.url)
        if site:
            site_sessions.settle(site, signed_in=False, home=self.home)

    def target(self, target_id):
        return self.targets.get(target_id)

    def session_config(self, target):
        """The config a step's guard is started with: the product's own, plus the site whose saved session the
        guard puts back at the start and keeps when the session ends (site_sessions.CONFIG_KEY, exactly as an
        application read is started). Cookies stay inside the guard and the encrypted store; none comes here."""
        import site_sessions
        site = site_sessions.site_key(target.url)
        return dict(self.config or {}, **{site_sessions.CONFIG_KEY: site}) if site else self.config

    # --- starting ---
    def _locks(self):
        if self._starting is None:
            self._starting, self._ending = asyncio.Lock(), asyncio.Lock()
        return self._starting, self._ending

    def directory(self):
        """One folder, emptied at each start: the guard's records of the latest session only (bounded)."""
        folder = paths.reports(self.root) / FOLDER / "session"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, mode=0o700)
        return folder

    async def start(self, target_id):
        """Start the live session for an offered target: the `Session`, or `Failed` (reason, plain words, as
        this target's card may show them: `said`)."""
        target = self.targets.get(target_id)
        outcome = await self._start(target_id)
        if isinstance(outcome, Failed):
            outcome = dataclasses.replace(outcome, words=said(target, outcome.words))
        return outcome

    async def _start(self, target_id):
        """`start`'s work. A step that cannot be shown is tried once more, then recorded (R12)."""
        self._loop = asyncio.get_running_loop()
        if not self.available():
            return Failed(UNAVAILABLE, NOT_AVAILABLE_WORDS)   # no retry, no Failures row, no count (G3)
        target = self.targets.get(target_id)
        if target is None:
            return Failed(UNKNOWN, GONE_WORDS)
        starting, _ = self._locks()
        async with starting:
            if self.session is not None:
                if self.session.target.id == target_id and not self.session.closing:
                    return self.session   # the same step opened again (a reload): the view rejoins it
                return Failed(OPEN, ONE_AT_A_TIME_WORDS)
            failure = Failed(NOT_SHOWN, NOT_SHOWN_WORDS)
            self._start_task, self._start_cancelled, self._start_target = asyncio.current_task(), False, target
            try:
                for attempt in (1, 2):
                    outcome = await self._attempt(target, attempt)
                    if isinstance(outcome, Session):
                        self.session = outcome
                        if self.targets.get(target_id) is not target:   # withdrawn while the page was opening
                            self._end_soon(CANCEL, outcome)
                            return Failed(UNKNOWN, GONE_WORDS)
                        LOG.info("LIVE_VIEW_STARTED purpose=%s attempt=%d", target.purpose, attempt)
                        return outcome
                    failure = outcome
                    if failure.final:
                        break
            except asyncio.CancelledError:
                if not self._start_cancelled:
                    raise
                # Cancel pressed while the page was still opening (G8): `_attempt` has stopped what it started
                # and released the browser. The person's own choice: no Failures row, no count.
                while asyncio.current_task().cancelling():   # Cancel may have been pressed more than once
                    asyncio.current_task().uncancel()
                LOG.info("LIVE_VIEW_START_CANCELLED")
                return Failed(CANCEL, ended_words(CANCEL))
            finally:
                self._start_task = None
            await asyncio.to_thread(self._record, target, failure.reason)
            LOG.warning("LIVE_VIEW_NOT_STARTED reason=%s", failure.reason)
            return failure

    async def _attach(self, target):
        """Attach mode's start (`offer_attached`): this client on the running session's Chrome, and nothing
        else. No lock (the session holds the browser), no guard, no Chrome, no navigation. Not tried twice: a
        session whose Chrome cannot be reached has ended or has a window."""
        session = Session(target, Lease(self.clock(), self.lease_seconds, self.lease_max_seconds), None, self.clock)
        try:
            endpoint = await asyncio.to_thread(session_endpoint, target.attach)
            if not endpoint:
                raise StartFailed("no endpoint")
            session.chrome = Chrome(endpoint, on_frame=session.on_frame, on_lost=lambda: self._lost(session),
                                    connect=self.connect, clock=self.clock, on_change=session.say_site,
                                    protect_submit=True, on_refused=lambda: session.say({"t": "refused"}),
                                    input_open=lambda: form_check_hold.input_open(target.attach),
                                    on_acted=lambda: form_check_hold.acted(target.attach))
            await session.chrome.start()
            if session.chrome.active is None:
                raise StartFailed("no page")
        except asyncio.CancelledError:
            await self._teardown(session)
            raise
        except Exception as error:
            LOG.warning("LIVE_VIEW_ATTACH_FAILED (%s)", type(error).__name__)
            await self._teardown(session)
            return Failed(NOT_SHOWN, NOT_SHOWN_WORDS, final=True)
        self._task(session, self._lease_watch(session))
        self._task(session, self._status(session))
        self._task(session, self._resume_watch(session))
        self._expect_viewer(session)
        return session

    async def _resume_watch(self, session):
        """An attached view closes by itself once the AI has resumed its work after a hold (owner, 2026-11-13:
        "If the user forgets to click Done, the AI carries on with its job and auto closes the Live view"): the
        ending a finished step has, without ending a hold (there is none open)."""
        import form_check_hold
        while not session.closing:
            await asyncio.sleep(RESUME_WATCH_SECONDS)
            if await asyncio.to_thread(form_check_hold.resumed, session.target.attach):
                self._end_soon(FINISHED, session)
                return

    async def _attempt(self, target, attempt):
        if target.attach is not None:
            return await self._attach(target)
        hold = Hold(self.root, self.config, self.lock_retry_seconds if attempt > 1 else 0, self.ps)
        try:
            reason = await asyncio.to_thread(hold.take)
        except asyncio.CancelledError:
            hold.release.set()   # its thread lets go of the browser the moment it has it
            raise
        except Exception as error:
            LOG.warning("LIVE_VIEW_LOCK_FAILED (%s)", type(error).__name__)
            reason = NOT_SHOWN
        if reason is not None:
            hold.drop()
            if reason == BUSY:
                return Failed(BUSY, BUSY_WORDS)
            if reason in browser_runtime.UNAVAILABLE_WORDS:
                words = browser_runtime.UNAVAILABLE_WORDS[reason] + " " + browser_runtime.UNAVAILABLE_TODO[reason]
                return Failed(WINDOW_OPEN if reason == "PROFILE_IN_USE" else COMPONENT_MISSING, words)
            return Failed(NOT_SHOWN, NOT_SHOWN_WORDS)
        session = Session(target, Lease(self.clock(), self.lease_seconds, self.lease_max_seconds), hold, self.clock)
        try:
            folder = await asyncio.to_thread(self.directory)
            await asyncio.to_thread(self._drop_stale_capture, target)
            session.guard = Guard(self.guard_command(self.session_config(target), folder), folder)
            await session.guard.start()
            endpoint = await session.guard.wait_endpoint(self.endpoint_wait_seconds)
            if not endpoint:
                raise (WindowedChrome if session.guard.endpoint() is False else StartFailed)("no endpoint")
            await session.guard.navigate(target.url)
            session.chrome = Chrome(endpoint, on_frame=session.on_frame, on_lost=lambda: self._lost(session),
                                    connect=self.connect, clock=self.clock, on_change=session.say_site)
            await session.chrome.start()
            if session.chrome.active is None:
                raise StartFailed("no page")
        except asyncio.CancelledError:
            await self._teardown(session)   # the guard, any Chrome it started, the browser's lock
            raise
        except Exception as error:
            LOG.warning("LIVE_VIEW_START_FAILED attempt=%d (%s)", attempt, type(error).__name__)
            await self._teardown(session)
            if isinstance(error, WindowedChrome):   # trying again would only open that window again (P5)
                return Failed(NOT_SHOWN, NO_BACKGROUND_WORDS, final=True)
            return Failed(NOT_SHOWN, NOT_SHOWN_WORDS)
        self._task(session, self._lease_watch(session))
        self._task(session, self._idle_watch(session))
        self._task(session, self._status(session))
        if target.check is not None:
            self._task(session, self._check_watch(session))
        self._expect_viewer(session)
        return session

    def _record(self, target, reason):
        if reason not in RECORDED_REASONS:
            return
        path = paths.database(self.root)
        if not path.is_file():
            return   # no database yet means no job, so no card: nothing to record against
        try:
            database = sqlite3.connect(path, timeout=run_locks.SESSION_TOOL_BUSY_TIMEOUT_SECONDS,
                                       factory=run_locks.WriteFirstConnection)
        except sqlite3.Error:
            return
        try:
            record_failure(database, reason, target.domain, target.job, target.purpose)
        finally:
            database.close()

    def _task(self, session, coroutine):
        task = asyncio.create_task(coroutine)
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)
        return task

    # --- what ends a session by itself ---
    async def _lease_watch(self, session):
        while True:
            left = session.lease.left(self.clock())
            if left <= 0:
                break
            await asyncio.sleep(min(left, 1.0))
        self._end_soon(LEASE, session)

    def _left(self, session, now):
        """Seconds until this view closes by itself: the lease, or (an ordinary view) the idle close if sooner."""
        left = session.lease.left(now)
        if session.target.attach is None:
            left = min(left, max(0.0, session.last_input + self.idle_seconds - now))
        return left

    async def _idle_watch(self, session):
        """An ordinary view with no input for `idle_seconds` ends as a finished step (IDLE_SECONDS' note): the
        same ending Done has, so the site's session is kept and the wait released."""
        while not session.closing:
            left = session.last_input + self.idle_seconds - self.clock()
            if left <= 0:
                self._end_soon(FINISHED, session)
                return
            await asyncio.sleep(min(left, 1.0))

    async def _check_watch(self, session):
        target = session.target
        while not session.closing:
            await asyncio.sleep(self.check_seconds)
            try:
                reached = target.check(LivePage(session.chrome.url(), target.domain, target.job, session.guard.call))
                if asyncio.iscoroutine(reached):
                    reached = await reached
            except Exception as error:
                LOG.warning("LIVE_VIEW_CHECK_FAILED (%s)", type(error).__name__)
                reached = False
            if reached:
                self._end_soon(FINISHED, session)
                return

    async def _status(self, session):
        while not session.closing:
            await asyncio.sleep(STATUS_SECONDS)
            now = self.clock()
            chrome = session.chrome
            age = None if session.last_frame is None else now - session.last_frame
            session.say({"t": "status", "words": status_words(chrome.active is not None, chrome.outstanding, age),
                         "site": session.site(), "waiting": chrome.outstanding, "minutes_left": int(self._left(session, now) // 60)})

    def _expect_viewer(self, session):
        """No viewer: the person left unless one (re)connects within the grace."""
        if session.leaving is None and not session.viewers and not session.closing:
            session.leaving = self._task(session, self._leave(session))

    async def _leave(self, session):
        await asyncio.sleep(self.leave_grace_seconds)
        if not session.viewers:
            self._end_soon(LEFT, session)

    def _lost(self, session):
        if not session.closing:
            self._end_soon(CLOSED, session)

    def _end_soon(self, reason, session):
        """End `session` from a task of its own (the caller is one of the session's tasks, which the ending
        cancels). The event loop keeps only a weak reference to a task, and this one is all that frees the
        browser, so it is held here until it has finished (G7)."""
        task = asyncio.create_task(self.end(reason, session))
        self.endings.add(task)
        task.add_done_callback(self.endings.discard)
        return task

    # --- the viewer's socket ---
    async def serve(self, session, socket):
        """One viewer: pictures and status out, the person's events in. `socket` has receive_text, send_text
        and send_bytes (the dashboard's authenticated websocket)."""
        queue = Outbox()
        session.viewers[socket] = queue
        session.counts["viewers"] += 1
        if session.leaving is not None:
            session.leaving.cancel()
            session.leaving = None
        if session.meta:
            _offer(queue, json.dumps(session.meta))
        _offer(queue, json.dumps({"t": "status", "words": LIVE_WORDS, "site": session.site(),
                                  "waiting": session.chrome.outstanding,
                                  "minutes_left": int(self._left(session, self.clock()) // 60)}))
        session.chrome.spawn(session.chrome.refresh())   # one picture at once, even of a still page
        sender = asyncio.create_task(self._send(socket, queue))
        try:
            while True:
                self.handle(session, await socket.receive_text())   # never waits on Chrome (lesson 1)
        except Exception:
            pass   # the socket closed; never the content of what it carried
        finally:
            sender.cancel()
            session.viewers.pop(socket, None)
            self._expect_viewer(session)

    async def _send(self, socket, queue):
        while True:
            item = await queue.get()
            if isinstance(item, bytes):
                await socket.send_bytes(item)
            else:
                await socket.send_text(item)

    def handle(self, session, text):
        """One message from the viewer. A plain function. Only its kind is ever counted or logged."""
        if session.closing:
            return
        try:
            kind = session.chrome.input(read_message(text))
        except Exception:
            session.counts["unreadable"] = session.counts.get("unreadable", 0) + 1
            return
        if kind:
            session.counts["input"] += 1
            session.lease.touch(self.clock())   # the person is active: 10 more minutes, 30 at most
            session.last_input = self.clock()

    # --- ending ---
    async def end(self, reason, session=None):
        """End the live session (`session`, or whichever is live). Returns its `Ended` record, or None when
        it is over already (two endings at once: the first one's record stands, in `self.last`)."""
        starting = self._start_task
        if self.session is None and session is None and reason == CANCEL and starting is not None and not starting.done():
            target = self._start_target
            self._start_cancelled = True
            starting.cancel()            # Cancel while the page is still opening (G8)
            await asyncio.wait({starting})
            if self.session is None:
                return Ended(CANCEL, None, target.domain, target.job, target.purpose, 0.0, {}, ended_words(CANCEL))
        current = self.session
        if current is None:
            # No session now. An ending still under way (its last hook) is the one a second Done or Cancel
            # meant: the same one, and its record (P1). An ending asked for a given session is never that.
            running = self._end_task
            if session is None and running is not None and not running.done():
                return await _finish(running)
            return None
        if session is not None and session is not current:
            return None
        if current.ending is not None:
            return await _finish(current.ending)   # this session is ending already: the same ending
        # One ending per SESSION, in a task of its own that nothing cancels: whoever asked for it (a request
        # that goes away, a second Cancel) only stops waiting. A newer session is never answered with an older
        # session's ending (an older one may still be waiting on its last hook).
        self._end_task = current.ending = running = asyncio.create_task(self._end(reason, current))
        self.endings.add(running)
        running.add_done_callback(self.endings.discard)
        return await _finish(running)

    async def _end(self, reason, current):
        current.closing = True   # from here no input is passed on
        current.reason = reason
        target, chrome = current.target, current.chrome
        for task in list(current.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        final_url = chrome.url()
        try:
            if target.attach is not None:
                # Owner, 2026-11-13 ("G1: a"): the view closes AT ONCE on Done. First the session's hold is told
                # (Done) or the card's state is set (the AI resumed by itself); then the viewers are told and let
                # go, before anything that waits on a Chrome the AI is busy in (`chrome.close`, in the teardown).
                import form_check_hold
                if reason == DONE:
                    await self._hook("LIVE_VIEW_ATTACHED_DONE_FAILED", form_check_hold.finish, target.attach)
                elif reason == FINISHED:
                    await self._hook("LIVE_VIEW_ATTACHED_DONE_FAILED", form_check_hold.mark_done, target.attach)
                told = json.dumps({"t": "ended", "reason": reason,
                                   "words": ended_words(reason, target.purpose, attached=True)})
                for socket in list(current.viewers):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(socket.send_text(told), 2)
            else:
                await chrome.stop_frames()
            if target.attach is not None:
                pass
            elif reason in STEP_FINISHED:
                await self._hook("LIVE_VIEW_STEP_HOOK_FAILED", on_user_step_finished, target.domain, target.job)
                if target.before_stop is not None:
                    # The step's own last look at the page it was done on, while its Chrome still runs: what is
                    # read here does not depend on what Chrome has written to disk when it is stopped.
                    endpoint = current.guard.endpoint() if current.guard is not None else None
                    if isinstance(endpoint, str) and endpoint:
                        await self._hook("LIVE_VIEW_BEFORE_STOP_HOOK_FAILED", target.before_stop, endpoint)
            counts = dict(chrome.counts, **current.counts)
            result = Ended(reason, final_url, target.domain, target.job, target.purpose,
                           round(self.clock() - current.started, 1), counts,
                           ended_words(reason, target.purpose, attached=target.attach is not None))
            ended = json.dumps({"t": "ended", "reason": reason, "words": result.words})
            for socket in list(current.viewers):
                if target.attach is None:   # an attached view's viewers were told above, once
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(socket.send_text(ended), 2)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), 2)
        finally:
            # Whatever happened above, a cancellation included (Quit while a hook was being waited for): the
            # guard and Chrome are stopped and the browser is let go.
            await self._teardown(current)
            if self.session is current:
                self.session = None
        finished = reason in STEP_FINISHED
        if finished:
            # The step is done: it stops being offered BEFORE the save is waited for (D22), and is not offered
            # again until its card's own hook has run (the wait released, or left as it is). A press on the
            # card during a slow save can never open a second view on a finished step.
            self.targets.pop(target.id, None)
            self.finishing.add((target.job, target.purpose))
        try:
            result.site_session_saved = await self._settled(current)
            self.last = result
            LOG.info("LIVE_VIEW_ENDED reason=%s seconds=%d counts=%s", reason, int(result.seconds),
                     json.dumps(counts, sort_keys=True))
            if target.on_ended is not None:
                await self._hook("LIVE_VIEW_ENDED_HOOK_FAILED", target.on_ended, result)
        finally:
            if finished:
                self.finishing.discard((target.job, target.purpose))
        return result

    async def _hook(self, name, function, *arguments):
        """A hook of the cards', on a thread, waited for at most HOOK_SECONDS: one that hangs or fails never
        keeps the browser held; the ending goes on and says so once (P8)."""
        try:
            await asyncio.wait_for(asyncio.to_thread(function, *arguments), HOOK_SECONDS)
        except Exception as error:
            LOG.warning("%s (%s)", name, type(error).__name__)

    async def _teardown(self, session):
        """Every ending and every failed start: the second client, the guard, its Chrome, and only then the
        browser's lock. Once per session, in a task of its own that nothing cancels; every caller waits for that
        same task, and a caller that is cancelled meanwhile still waits, then passes the cancellation on (P1)."""
        if session.teardown is None:
            session.teardown = asyncio.create_task(self._teardown_now(session))
            self.endings.add(session.teardown)
            session.teardown.add_done_callback(self.endings.discard)
        await _finish(session.teardown)

    async def _settle(self, session):
        """After the guard and its Chrome have stopped and BEFORE the browser is let go (C1): what the guard
        kept for the site becomes its saved session (the step ended Done or finished) or is dropped (any other
        ending, a failed start). No other browser session can run in between, so a read that was queued cannot
        drop the person's capture first. Only the site the step was started for is kept, never another site
        the person visited (C5). Waited for at most SETTLE_SECONDS: after that nothing new is started
        (`proceed`, D13), and a write already under way is left to finish: `session.settling` then holds it, and the
        ending reads its true result for the record (`_settled`, D15). True only when really written. Never raises."""
        import site_sessions
        site = site_sessions.site_key(session.target.url)
        if not site or session.guard is None:
            return False
        finished = session.reason in STEP_FINISHED

        expired = threading.Event()   # set at the cap: from then the thread writes nothing (D13)

        def settle():
            return site_sessions.settle(site, signed_in=finished, home=self.home, own_only=True,
                                        wall=session.target.cleared if finished else None,
                                        proceed=lambda: not expired.is_set()) == "saved"
        task = asyncio.ensure_future(asyncio.to_thread(settle))
        try:
            return bool(await asyncio.wait_for(asyncio.shield(task), SETTLE_SECONDS))
        except asyncio.TimeoutError:
            expired.set()             # nothing new is written from here; a write under way finishes
            session.settling = task   # its true result is read before anything is recorded
            LOG.warning("LIVE_VIEW_SESSION_SETTLED_LATE")
            return False
        except Exception as error:    # failed: never recorded as saved
            LOG.warning("LIVE_VIEW_SESSION_NOT_SETTLED (%s)", type(error).__name__)
            return False

    async def _settled(self, session):
        """What the store really holds for this step (D15): the settle's own result, waited for when it was
        still writing at the cap. The browser is free by then; only the record waits."""
        task, session.settling = session.settling, None
        if task is not None:
            try:
                session.saved = bool(await task)
            except Exception as error:
                LOG.warning("LIVE_VIEW_SESSION_NOT_SETTLED (%s)", type(error).__name__)
                session.saved = False
        return bool(session.saved)

    async def _teardown_now(self, session):
        try:
            await self._stop_all(session)
            session.saved = await self._settle(session)
        finally:
            if session.hold is not None:   # an attached view holds no lock (E16.T2)
                session.hold.drop()

    async def _stop_all(self, session):
        if session.target.attach is not None:
            # E16.T2: an attached view only disconnects its own client. The guard and the Chrome are the running
            # session's: neither is stopped here, on any ending.
            with contextlib.suppress(Exception):
                if session.chrome is not None:
                    await session.chrome.close(wait=ATTACHED_CLOSE_SECONDS)
            return
        try:
            if session.chrome is not None:
                await session.chrome.close()
            if session.guard is not None:
                await session.guard.stop()
            await asyncio.to_thread(self._stop_chrome)
        except asyncio.CancelledError:
            # The event loop is closing under a teardown (Quit ran out of time). Nothing can be waited for any
            # more, so the rest is done at once: the guard's input is closed, Chrome on Sidedoor's profile is
            # stopped the product's way, and the one guard process this session started is ended.
            LOG.warning("LIVE_VIEW_TEARDOWN_CUT_SHORT")
            process = session.guard.process if session.guard is not None else None
            with contextlib.suppress(Exception):
                if process is not None and process.returncode is None:
                    process.stdin.close()
            with contextlib.suppress(Exception):
                self._stop_chrome()
            with contextlib.suppress(Exception):
                if process is not None and process.returncode is None:
                    process.terminate()
            with contextlib.suppress(Exception):
                session.guard.stderr.close()
            raise
        except Exception as error:
            LOG.warning("LIVE_VIEW_TEARDOWN_FAILED (%s)", type(error).__name__)

    async def quit(self, seconds=None):
        """Sidedoor is quitting (the dashboard server's shutdown, before the process goes on to close what is
        left): end the live session, or the start under way, the ordinary way. Never longer than `seconds`."""
        if self._start_task is not None and not self._start_task.done():
            reason = CANCEL
        elif self.session is not None:
            reason = CLOSED
        else:
            return
        # The ending runs in a task of its own and is only waited for (P8): `end` keeps waiting when it is
        # cancelled, by design, so a wait_for around it would not be a bound. When the time is up Quit goes on:
        # the teardown's own `finally` still lets go of the browser, and dashboard._serve closes what is left.
        task = self._end_soon(reason, None)
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        _, pending = await asyncio.wait({task}, timeout=QUIT_SECONDS if seconds is None else seconds)
        if pending:
            LOG.warning("LIVE_VIEW_QUIT_NOT_FINISHED reason=%s", reason)

    def _stop_chrome(self):
        """The way a product session ends: only the Chrome on Sidedoor's profile, which this session started
        under the held lock. Then the guard's page files of the session go (its decision record stays)."""
        if browser_runtime.chrome_running(browser_runtime.profile(self.config)):
            browser_runtime.end_session(self.config)
        shutil.rmtree(paths.reports(self.root) / FOLDER / "session" / browser_runtime.OUTPUT_FOLDER, ignore_errors=True)


# --- Dashboard routes --------------------------------------------------------------------------------------------

def socket_allowed(headers, cookies, secret, allowed_hosts, allowed_origins, cookie):
    """The dashboard's own rules, for the websocket (its HTTP guard never sees one): the Host is this dashboard,
    the Origin is this dashboard's page (never another site's, never missing), and the session cookie is the
    install token."""
    import hmac
    return (headers.get("host") in allowed_hosts and headers.get("origin") in allowed_origins
            and hmac.compare_digest((cookies.get(cookie) or "").encode(), secret.encode()))


SERVER_LOGGER = "uvicorn.error"   # the logger uvicorn hands its websocket server (and logs through itself)


class NoFrameLog(logging.Filter):
    """The dashboard server's websocket library writes every frame it receives at DEBUG on uvicorn's logger,
    and a frame from the viewer is a key or a paste. Decision (G4): every record at DEBUG or below on that logger
    is dropped while the app runs, whatever level it is set to, instead of picking frame records out by their
    wording. The dashboard runs it at "warning" anyway, so nothing that was written before is lost."""

    def filter(self, record):
        return record.levelno > logging.DEBUG


def keep_frames_out_of_the_log():
    logger = logging.getLogger(SERVER_LOGGER)
    if not any(isinstance(known, NoFrameLog) for known in logger.filters):
        logger.addFilter(NoFrameLog())


def available(purpose=None, config=None):
    """For the card seam, without a dashboard at hand: can this computer show a step inside the dashboard?"""
    return bool(supported(config))


def register(app, templates, layout, home=None, root=None, config=None, secret=None, port=None, cookie=None,
             view=None):
    """The live view's routes, inside the dashboard app and behind its guard (cookie, Host, Origin on writes)."""
    from fastapi import Request, WebSocket
    from fastapi.responses import HTMLResponse, JSONResponse

    view = view or LiveView(home=home, root=root, config=config)
    app.state.live_view = view
    served = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(running):
        # Quit (P3): the server's own shutdown ends the live session, before `dashboard._serve` goes on to
        # close whatever browser is left; `quit` never takes longer than QUIT_SECONDS.
        async with served(running) as state:
            try:
                yield state
            finally:
                await view.quit()
    app.router.lifespan_context = lifespan
    keep_frames_out_of_the_log()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://" + host for host in allowed_hosts}

    def context(target, autostart, card=False):
        # `card`: the view is shown inside a card that has the step's button itself (E15 join): the view then
        # has no start button of its own, so the card offers the step once.
        return {"target": target, "autostart": autostart, "card": card, "ready_words": READY_WORDS,
                "reconnecting_words": RECONNECTING_WORDS, "not_shown_words": said(target, NOT_SHOWN_WORDS),
                "over_words": OVER_WORDS % target.start_label, "paste_long_words": PASTE_TOO_LONG_WORDS,
                # Owner, 2026-11-13: Done closes the view on the press itself; the page says this at once.
                "done_words": ENDED_WORDS[DONE],
                "paste_max": PASTE_MAX_CHARACTERS}

    def missing():
        return HTMLResponse('<p class="note note-info">%s</p>' % GONE_WORDS, status_code=404)

    @app.get("/live-view/{target_id}/fragment", name="live_view_fragment")
    def fragment(request: Request, target_id: str, start: str = "", card: str = ""):
        target = view.target(target_id)
        if target is None:
            return missing()
        return templates.TemplateResponse(request, "live_view_fragment.html", context(target, start == "1", card == "1"))

    @app.get("/live-view/{target_id}", name="live_view_page")
    def page(request: Request, target_id: str):
        target = view.target(target_id)
        if target is None:
            return missing()
        return templates.TemplateResponse(request, "live_view.html",
                                          dict(layout("applications", "A step for you"), **context(target, False)))

    @app.post("/live-view/{target_id}/start", name="live_view_start")
    async def start(target_id: str):
        outcome = await view.start(target_id)
        if isinstance(outcome, Failed):
            return JSONResponse({"state": "failed", "reason": outcome.reason, "words": outcome.words,
                                 "again": outcome.reason != UNAVAILABLE and not outcome.final},
                                status_code=404 if outcome.reason == UNKNOWN else 409)
        return JSONResponse({"state": "live", "session": outcome.id, "words": LIVE_WORDS})

    @app.post("/live-view/end", name="live_view_end")
    async def end(reason: str = ""):
        if reason not in USER_REASONS:
            return JSONResponse({"state": "refused"}, status_code=400)
        result = await view.end(reason)
        if result is None:
            return JSONResponse({"state": "none", "words": GONE_WORDS}, status_code=409)
        return JSONResponse({"state": "ended", "reason": result.reason, "words": result.words})

    @app.websocket("/live-view/ws")
    async def socket(websocket: WebSocket):
        session = view.session
        if not socket_allowed(websocket.headers, websocket.cookies, secret, allowed_hosts, allowed_origins,
                              cookie):
            await websocket.close(code=4403)   # before accept: the handshake is refused (HTTP 403)
            return
        if session is None or session.closing or not secrets.compare_digest(
                websocket.query_params.get("s", "").encode(), session.id.encode()):
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await view.serve(session, websocket)

    return view
