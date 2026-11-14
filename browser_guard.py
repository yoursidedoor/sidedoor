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
* `wait_for_human_check`, attended submit only (E16.T2, form_check_hold.py): given --human-check with
  --claim, the session holds the tool; one call is held until the user's Done or the time limit, and it
  is refused once the arm was called. Every other application session is as E16.T1 left it:
* `wait_for_human_check`: retired for every application session (E16.T1, owner 2026-11-10): a session
  that meets a human check stops at once and returns blocked CAPTCHA_REQUIRED; there is no in-session
  wait and no window is shown. The tool is not listed, and a call to it is refused by name (NOT_OFFERED).
  Only support_email (given --human-check) has a tool of that name, support_email.SignInWait (owner,
  2026-10-25): the user signs in to their webmail in the draft window; no claim, database or notification.
* every operation: nothing brings Sidedoor's Chrome in front of the user's screen (owner, 2026-10-22:
  "is it possible to stop the sidedoor browser from just snatching control of my screen?", option a).
  Code that calls bringToFront (browser_evaluate) is refused with
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
  off). A focus keeper (FocusKeeper, every 50 ms; owner, 2026-10-24: no guessing) gives the front
  back to the app that was in front just before, every time Sidedoor's Chrome (by pid; never the user's
  own Chrome) comes forward, and minimizes a window of it as soon as one shows. Nothing un-minimizes
  the window (E16.T1: the CAPTCHA wait that did is retired). A read-back that is not minimized is recorded
  (WINDOW_NOT_MINIMIZED); a failed placement never stops the call.
* support_email (owner, 2026-10-24: the "Email support" button on Failures and logs), the one exception to
  the above: the window is the user's. Its Chrome starts headed and on-screen (browser_runtime
  SHOWN_OPERATIONS: no --offscreen, no headless), and the guard leaves it running when the session ends,
  so the draft the AI started stays open for the user to send (support_email brings it to the front and
  closes it once the user has closed the window). bringToFront in code is still refused. Owner,
  2026-10-25: while the AI works the window stays behind the user's app: the focus keeper runs from
  before Chrome starts, giving the front back whenever Chrome takes it, but never minimizes (the window
  stays on-screen, behind). It stops for good once support_email.SignInWait.front_released (the sign-in
  wait has begun, or the dashboard is bringing the finished draft forward).
Every other @playwright/mcp tool is listed and forwarded unjudged in every operation; the trace checks
that live ran on cua_repl code run in `application_browser.tool_item`, on the Codex trace, as before.

* every operation, given --headless-user-agent with --background-chrome (macOS; owner, 2026-10-24:
  "sometimes the browser still comes to the front and doesn't return me to my last screen", option A;
  browser_runtime.headless_user_agent): the guard starts the profile's Chrome headless (--headless=new,
  the installed Chrome's headed user agent), so there is no window to minimize and nothing to keep
  from the front: no placement, no focus keeper. A headless session stays headless to its end (E16.T1:
  the reopen in a window for a CAPTCHA is retired). Operations left out of
  config["browser_driver"]["headless_operations"] get --offscreen as before.

* every operation (E15.T4 Q1, owner 2026-11-12: "remove it"): browser_run_code_unsafe is not the model's.
  It runs arbitrary JavaScript in the browser server's own Node process (its authors call it
  RCE-equivalent; a page that talked the model into calling it would own the machine), so it is neither
  listed nor forwarded in any operation and a call to it is refused by name (RUN_CODE_REFUSED, the name
  recorded, never the code). Only the tools in TOOLS and the tools a session holds are listed and
  forwarded (an allow-list). The guard's own calls to that server tool (window placement, the headed
  reopen, the site session) are unchanged. In-page code (browser_evaluate, where an operation has it) that
  names document.cookie or cookieStore is refused (SESSION_SECRET_REFUSED: a text check, which indirect
  spelling gets around; cookies marked httpOnly are not readable in the page at all), and
  browser_network_request answers lose their Cookie, Set-Cookie and Authorization headers.
* inspect and submit, given --site-session <host> (E15.T4, site_sessions.py): the cookies saved for that
  site which Chrome dropped when it last exited are put back right after the client's `initialized`
  (never over a cookie Chrome holds, never a navigation), and the site's cookies are captured, encrypted,
  while the session runs. The guard's own calls only: the model has no tool for it and never sees a value.

* given --saved-sign-in <host> (E15.T7, saved_sign_ins.py): the session holds `fill_saved_sign_in`. The
  model names the fields (snapshot refs) and the guard types the site's saved email and password into them
  through its own call to the browser server, whose answer is never relayed. The model never sees the
  value: the tool's answers are fixed sentences, the audit row carries the site and booleans, and from the
  first fill on every message to the client and every audit row has the value (as typed, JSON-escaped,
  URL-encoded, HTML-escaped) replaced by "[saved sign-in]" (a snapshot, a console line or a network body
  that shows it). Only on the site: the page's own address and the field's own frame, read from the live
  page inside the same browser call that types, must both be <host> (site_sessions.site_key's rule), and the
  password goes only into a password field; otherwise nothing is typed.
* given --single-sign-on with --access-step (E15.T9): the step holds the `single_sign_on` tool (SSO_TOOL).

Usage: sidedoor browser-guard --operation <op> [--audit <file.jsonl>] [--claim <file>]
                              [--human-check <file>] [--background-chrome <profile>] [--site-session <host>]
                              [--saved-sign-in <host>]
                              [--headless-user-agent <ua> | --offscreen]
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

import portable


OPERATIONS = ("linkedin", "webmail", "inspect", "investigate", "submit", "verify", "reconcile", "support_email")
# The tools of @playwright/mcp 0.0.83 a session may be given (an allow-list: nothing else is listed or
# forwarded), every one approved in every operation. browser_run_code_unsafe is not among them (REMOVED_TOOL).
TOOLS = ("browser_click", "browser_close", "browser_console_messages", "browser_drag", "browser_drop",
         "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_fill_form", "browser_find",
         "browser_handle_dialog", "browser_hover", "browser_navigate", "browser_navigate_back",
         "browser_network_request", "browser_network_requests", "browser_press_key", "browser_resize",
         "browser_select_option", "browser_snapshot", "browser_tabs",
         "browser_take_screenshot", "browser_type", "browser_wait_for")
ARM_TOOL = "arm"
HUMAN_CHECK_TOOL = "wait_for_human_check"
# Claude browser jobs only (U1 3e, owner pick E4 b): Claude 2.1.301 saves a tool result over 50,000 characters
# under ~/.claude/projects and shows the model a 2 KB preview. Given --piece-chars, the guard cuts a longer
# text answer into pieces at line ends, answers with the first and a CONTINUATION_TITLE section, and hands out
# the rest through this read-only tool (no arguments, touches no page). Codex sessions never get either.
CONTINUE_TOOL = "browser_continue"
CONTINUATION_TITLE = "Sidedoor continuation"
# E15.T7: the guard types the site's saved sign-in itself; the model never sees the value.
SAVED_SIGN_IN_TOOL = "fill_saved_sign_in"
SAVED_SIGN_IN_MARK = "[saved sign-in]"
SAVED_SIGN_IN_TEXT = {
    "filled": "SAVED_SIGN_IN_FILLED: the saved sign-in was typed into the page. Its value is never shown to you. "
              "Go on with the page's own sign-in button.",
    "none": "NO_SAVED_SIGN_IN: Sidedoor has no saved sign-in for this site. Nothing was typed.",
    "wrong_site": "REFUSED SAVED_SIGN_IN_WRONG_SITE: this page or field is not on the site this session's saved "
                  "sign-in belongs to. Nothing was typed.",
    "not_password_field": "REFUSED SAVED_SIGN_IN_NOT_A_PASSWORD_FIELD: the password is typed only into a password "
                          "field. Nothing was typed.",
    "not_found": "SAVED_SIGN_IN_FIELD_NOT_FOUND: a field you named is not in the page. Nothing was typed. Take a "
                 "fresh snapshot and name the fields again.",
    "malformed": "REFUSED %s: name the email field, the password field or both by their snapshot refs."
                 % "MALFORMED_TOOL_CALL",
    "failed": "SAVED_SIGN_IN_FAILED: the page did not take the saved sign-in.",
    "unconfirmed": "SAVED_SIGN_IN_UNCONFIRMED: the browser did not answer in time, so the saved sign-in may or may "
                   "not have been typed. Take a fresh snapshot and look at the fields before going on.",
}
# E15.T8: the access step's two tools (the link tool with --access-step, the password tool with
# --create-account <email>, both beside --saved-sign-in <host>). The guard makes the new account's password and types it; the model never sees it.
CREATE_TOOL = "type_new_account_password"
LINK_TOOL = "open_confirmation_link"
CREATE_TEXT = {
    "filled": "NEW_PASSWORD_TYPED: Sidedoor made a password for the new account and typed it into the fields you "
              "named. Its value is never shown to you. Sidedoor types it once only: press the sign-up button once, "
              "and never start the sign-up again.",
    "already": "NEW_PASSWORD_ALREADY_TYPED: the password for this account was already typed once and is not typed "
               "again, so a second sign-up cannot start. Report what the page shows.",
    "exists": "NEW_PASSWORD_NOT_MADE: Sidedoor may already have an account on this site, so it makes no second one. "
              "Use fill_saved_sign_in to sign in.",
    "wrong_site": "REFUSED NEW_PASSWORD_WRONG_SITE: this page or field is not on the site this session is for. "
                  "Nothing was typed.",
    "not_password_field": "REFUSED NEW_PASSWORD_NOT_A_PASSWORD_FIELD: the password is typed only into password "
                          "fields. Nothing was typed.",
    "not_found": "NEW_PASSWORD_FIELD_NOT_FOUND: a field you named is not in the page. Nothing was typed. Take a "
                 "fresh snapshot and name the fields again.",
    "malformed": "REFUSED MALFORMED_TOOL_CALL: name one to three password fields by their snapshot refs.",
    "unavailable": "NEW_PASSWORD_NOT_MADE: Sidedoor could not keep a new password on this computer, so none was "
                   "typed. Report SIGN_UP_NOT_TRIED.",
    "failed": "NEW_PASSWORD_FAILED: the page did not take the password.",
    "unconfirmed": "NEW_PASSWORD_UNCONFIRMED: the browser did not answer in time, so the password may or may not "
                   "have been typed. Take a fresh snapshot and look at the fields before going on.",
}
LINK_TEXT = {
    "elsewhere": "LINK_NOT_OPENED: this link is not on the site the account is for, so Sidedoor does not open it. "
                 "Report LINK_NOT_OPENED.",
    "malformed": "REFUSED MALFORMED_TOOL_CALL: give the link's full address as url.",
    "failed": "LINK_FAILED: the browser did not open the link.",
}
# Single sign-on (E15.T9), one of the access step's ways in (access_step.py; owner's pick R1, 2026-11-12: the
# setting decides "which tools the step is offered at all"). Held only by an access step whose user's mailbox
# in Sidedoor has a sign-in provider (--single-sign-on beside --access-step). The model names the site's own
# sign-in button for that provider by its ref and the guard clicks it (a browser_click it forwards). Nothing
# is judged here: which button, and every later page of the provider's own sign-in (the account already
# signed in, its permission screen), are the model's, with the ordinary page tools (owner, 2026-11-13: "DO
# NOT CONSTRAIN THE AGENT"). No other session holds it: a call there is refused by name (NOT_OFFERED).
SSO_TOOL = "single_sign_on"
NO_CODE_CONFIG_KEY = "_application_no_page_code"   # access_step.session -> browser_runtime.guard_command (--no-page-code)
SSO_CONFIG_KEY = "_application_single_sign_on"   # access_step.run -> browser_runtime.guard_command (--single-sign-on)
# Owner, 2026-11-14: the site's own password reset, only on a site where Sidedoor itself made the account
# and no sign-in with its password was ever confirmed (the vault's pending record for the site).
RESET_TOOL = "type_reset_password"
RESET_TEXT = {
    "filled": "RESET_PASSWORD_TYPED: Sidedoor made a new password and typed it into the fields you named. Its value "
              "is never shown to you. Sidedoor types it once only: send the reset form once.",
    "already": "RESET_PASSWORD_ALREADY_TYPED: a new password was already typed once in this session and is not typed "
               "again. Report what the page shows.",
    "none": "REFUSED RESET_NOT_PERMITTED: Sidedoor has no record of making an account on this site that is still "
            "unconfirmed, so no password is reset here. Nothing was typed.",
    "wrong_site": "REFUSED RESET_PASSWORD_WRONG_SITE: this page or field is not on the site this session is for. "
                  "Nothing was typed.",
    "not_password_field": "REFUSED RESET_PASSWORD_NOT_A_PASSWORD_FIELD: the password is typed only into password "
                          "fields. Nothing was typed.",
    "not_found": "RESET_PASSWORD_FIELD_NOT_FOUND: a field you named is not in the page. Nothing was typed. Take a "
                 "fresh snapshot and name the fields again.",
    "malformed": "REFUSED MALFORMED_TOOL_CALL: name one to three password fields by their snapshot refs.",
    "failed": "RESET_PASSWORD_FAILED: the page did not take the password.",
    "unconfirmed": "RESET_PASSWORD_UNCONFIRMED: the browser did not answer in time, so the password may or may not "
                   "have been typed. Take a fresh snapshot and look at the fields before going on.",
}
OWN_TOOLS = (ARM_TOOL, HUMAN_CHECK_TOOL, SAVED_SIGN_IN_TOOL, CREATE_TOOL, LINK_TOOL, SSO_TOOL, RESET_TOOL)
# The access step's tools (E15.T7 to T9), each held by the guard only under its own flag.
STEP_TOOLS = (SAVED_SIGN_IN_TOOL, CREATE_TOOL, LINK_TOOL, SSO_TOOL, RESET_TOOL)


def held_step_tools(argv):
    """The STEP_TOOLS a guard started as `argv` (browser_runtime.guard_command) holds, by the flags
    `Proxy.__init__` reads: the one list every engine's allow list for the run is built from, so a tool the
    guard lists is never one the engine refuses (tests/test_guard_tools_reach_every_engine.py)."""
    flags = list(argv[:argv.index("--")] if "--" in argv else argv)
    if "--saved-sign-in" not in flags:
        return []
    held = [SAVED_SIGN_IN_TOOL]
    if "--create-account" in flags:
        held.append(CREATE_TOOL)
    if "--access-step" in flags:
        held.append(LINK_TOOL)
        if "--reset-recorded" in flags:
            held.append(RESET_TOOL)
        if "--single-sign-on" in flags:
            held.append(SSO_TOOL)
    return held
# Live rule 1's refusal code (linkedin_contacts.validate refused the read with it after the session).
CONTACT_CONTROL_CODE = "LINKEDIN_CONTACTS_CONTACT_CONTROL_CLICKED"
# E16 Y1 c (owner, 2026-11-13: "stop the running job and requeue it"): the user pressed a human check's card
# while this session holds the browser. The dashboard writes YIELD_FILE into this session's folder
# (attended_submit.ask_to_yield); the guard sees it within YIELD_POLL_SECONDS and, in its own message loop
# (so never in the middle of handling a call, and never between an arm call and what follows it), stops the
# session with YIELD_REASON. Its starter hands the request back with no try counted
# (application_browser.consume). Only an application read or a submit that has not called arm yields: never
# once `ArmBinding.attempted` is set, never an access step, an attended submit, or any other operation; a
# refusal is recorded in YIELD_REFUSED_FILE and the session runs on untouched.
YIELD_FILE = "yield-request.json"
YIELD_REFUSED_FILE = "yield-refused.json"
YIELD_REASON = "YIELDED_TO_ATTENDED_SUBMIT"
YIELD_METHOD = "sidedoor/yield"          # the guard's own message to itself; never forwarded, never from a client
YIELD_OPERATIONS = ("inspect", "submit")
YIELD_POLL_SECONDS = 0.5
START_URL = re.compile(r"https?://[^\s\x00-\x1f]{1,2000}")   # an attended submit's start page (--start-url)
ARM_DENIED_TEXT = "APPLICATION_ARM_DENIED; stop, never submit or retry"
# A forwarded browser call the browser has not answered by then is answered by the guard with an error
# (review fix 6), under Codex's ordinary 120 s tool timeout: only support_email's sign-in wait runs longer.
CALL_TIMEOUT_SECONDS = 110
# The code tools are neither listed nor run in the webmail read (review fix 8) or the support email
# (browser_runtime.CODE_FREE_OPERATIONS).
WEBMAIL_HIDDEN = ("browser_evaluate",)
# Owner, 2026-11-12 ("remove it"): never the model's, in any operation. Refused by name before anything else,
# so a later edit to TOOLS cannot quietly bring it back. The guard itself still calls it (PLACE_TOOL).
REMOVED_TOOL = "browser_run_code_unsafe"
REMOVED_CODE = "RUN_CODE_REFUSED"
MALFORMED_CODE = "MALFORMED_TOOL_CALL"
REMOVED_TEXT = ("browser_run_code_unsafe is not available. Use the page tools (browser_snapshot, browser_click, "
                "browser_type, browser_fill_form and the rest) instead.")
REF = re.compile(r"^(?:f\d+)?e\d+$")
# Nothing activates Sidedoor's Chrome (owner, 2026-10-22, option a): the refusal code and its answer.
FOCUS_CODE = "BROWSER_FOCUS_REFUSED"
BRING_TO_FRONT = re.compile(r"bringToFront")
# The off-screen window (FG1 = b): placed by the guard's own call, never offered to or judged for the model.
PLACE_TOOL = "browser_run_code_unsafe"
PLACE_INTERVAL_SECONDS = 1.0
# The focus keeper (owner, 2026-10-24: "please ensure it doesn't make any guesses about which screen I'm on
# and it minimizes itself extremely quickly and returns me back to the screen I was on"): poll period (one
# `lsappinfo front`, ~3 ms, and one on-screen window list per poll); the shortest gap between two minimize
# requests the keeper itself makes; how often a correction that keeps failing is written again.
FOCUS_POLL_SECONDS = 0.05
HIDE_RETRY_SECONDS = 0.25
FOCUS_FAILED_LOG_SECONDS = 1.0


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


# Windows (Proxy.close_own_chrome): Chrome is asked to close itself; the answer is not waited for (the
# connection ends with it).
CLOSE_CODE = ("async (page) => {\n"
              "  try {\n"
              "    const s = await page.context().browser().newBrowserCDPSession();\n"
              "    s.send('Browser.close').catch(() => {});\n"
              "    return [{ closing: true }];\n"
              "  } catch (e) { return [{ error: String(e && e.message || e).slice(0, 200) }]; }\n"
              "}")


def client_hints_code(user_agent, metadata, screen=None):
    """Playwright code that sets, on every open page and on each page the context opens later (its CDP
    session stays attached: the overrides last while it is), Emulation.setUserAgentOverride (userAgent +
    userAgentMetadata, when given) and, given `screen`, Emulation.setDeviceMetricsOverride with only the
    screen size: width/height/deviceScaleFactor 0 leave the viewport (the 1280x900 window) and the
    ratio (--force-device-scale-factor) as they are. Answers [{url} | {error}] for the pages open now."""
    metrics = screen_override(screen)
    return ("async (page) => {\n"
            "  const override = { userAgent: %s, userAgentMetadata: %s }; const metrics = %s;\n"
            "  const ctx = page.context();\n"
            "  const apply = async (p) => {\n"
            "    try { const s = await ctx.newCDPSession(p);\n"
            "      if (override.userAgentMetadata) await s.send('Emulation.setUserAgentOverride', override);\n"
            "      if (metrics) await s.send('Emulation.setDeviceMetricsOverride', metrics);\n"
            "      return { url: p.url() }; }\n"
            "    catch (e) { return { error: String(e && e.message || e).slice(0, 200) }; }\n"
            "  };\n"
            "  if (!ctx.__sidedoorClientHints) { ctx.__sidedoorClientHints = true; ctx.on('page', (p) => { apply(p); }); }\n"
            "  const done = [];\n"
            "  for (const p of ctx.pages()) done.push(await apply(p));\n"
            "  return done;\n"
            "}" % (json.dumps(user_agent), json.dumps(metadata), json.dumps(metrics)))


def screen_override(screen):
    """Emulation.setDeviceMetricsOverride's parameters for the real display (only the screen size is set)."""
    return None if not screen else {
        "width": 0, "height": 0, "deviceScaleFactor": 0, "mobile": False,
        "screenWidth": int(screen["width"]), "screenHeight": int(screen["height"]), "positionX": 0, "positionY": 0}


class TabDetails:
    """Windows (VM run, 2026-11-13: a hidden new tab's first document reported an 800x600 screen and empty
    client hints; only its second load was right). The guard's own DevTools client on the hidden Chrome's
    browser socket asks Chrome to hold every new tab before its first document starts
    (Target.setAutoAttach with waitForDebuggerOnStart), then on the tab's own session sends the user agent
    with its client hints, the real screen, and the release (Runtime.runIfWaitingForDebugger), in that
    order and without waiting for answers: Chrome takes a session's calls in order, so the details are in
    place when the tab runs. (Waiting for the answers first held a tab with no opener for ever, VM pass 2:
    a `target=_blank` link's tab answers them only once it is let run.)

    A tab whose details are refused, or not answered within ANSWER_SECONDS, is not left to go on presenting
    what a visible Chrome never would: `on_failed` is called (the guard stops the session, and Chrome with
    it). The overrides last while this client stays connected; closing it lets every held tab go.

    Not a second way to drive pages: it sends only those four calls. Nothing it reads is logged."""

    ANSWER_SECONDS = 30

    def __init__(self, endpoint, user_agent, metadata, screen, on_failed, connect=None, opener=None, timer=None):
        self.endpoint, self.on_failed, self.connect, self.opener = endpoint, on_failed, connect, opener
        self.override = {"userAgent": user_agent, "userAgentMetadata": metadata}
        self.metrics = screen_override(screen)
        self.timer = timer or threading.Timer
        self.socket, self.thread, self.counter = None, None, 0
        self.waiting = {}    # command id -> the tab's session it was sent on
        self.tabs = {}       # session -> {"left": the details calls not yet answered, "timer": its time limit}
        self.closed = False

    def start(self):
        """Connect and arm. Raises OSError when Chrome's browser socket cannot be reached."""
        import browser_runtime
        try:
            with (self.opener or browser_runtime.local_open)(self.endpoint + "/json/version", timeout=5) as answer:
                url = json.loads(answer.read(65536)).get("webSocketDebuggerUrl")
            if not isinstance(url, str) or not url.startswith("ws://127.0.0.1:"):
                raise OSError("no browser socket")
            connect = self.connect
            if connect is None:
                from websockets.sync.client import connect as dial
                connect = lambda address: dial(address, proxy=None, open_timeout=10, max_size=16 * 1024 * 1024)  # noqa: E731
            self.socket = connect(url)
            self.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        except OSError:
            raise
        except Exception as error:
            raise OSError("the browser's own socket could not be used: " + type(error).__name__) from None
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()
        return self

    def send(self, method, params, session=None):
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.socket.send(json.dumps(message))
        return self.counter

    def read(self):
        try:
            for raw in self.socket:
                try:
                    self.handle(json.loads(raw))
                except ValueError:
                    continue
        except Exception:
            pass
        if not self.closed and any(tab["left"] for tab in self.tabs.values()):
            self.fail()   # the client was lost while a tab's details were still unconfirmed

    def handle(self, message):
        """One message from Chrome. A new tab: its details and its release are sent on its own session, in
        order. An answer to a details call: counted; an error stops the session."""
        if not isinstance(message, dict):
            return
        if message.get("method") == "Target.attachedToTarget":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            session, info = params.get("sessionId"), params.get("targetInfo") or {}
            if not isinstance(session, str):
                return
            held = bool(params.get("waitingForDebugger"))
            if info.get("type") == "page":
                sent = [self.send("Emulation.setUserAgentOverride", self.override, session)]
                if self.metrics:
                    sent.append(self.send("Emulation.setDeviceMetricsOverride", self.metrics, session))
                limit = self.timer(self.ANSWER_SECONDS, lambda: self.unanswered(session))
                limit.daemon = True
                self.tabs[session] = {"left": set(sent), "timer": limit}
                for ident in sent:
                    self.waiting[ident] = session
                limit.start()
            if held:   # every held target is released at once: a page after its details, anything else as it is
                self.send("Runtime.runIfWaitingForDebugger", {}, session)
            return
        if message.get("method") == "Target.detachedFromTarget":
            tab = self.tabs.pop((message.get("params") or {}).get("sessionId"), None)
            if tab is not None:
                tab["timer"].cancel()
                for ident in tab["left"]:
                    self.waiting.pop(ident, None)
            return
        session = self.waiting.pop(message.get("id"), None) if "id" in message else None
        tab = self.tabs.get(session)
        if tab is None:
            return
        if "error" in message:
            tab["timer"].cancel()
            self.fail()
            return
        tab["left"].discard(message["id"])
        if not tab["left"]:
            tab["timer"].cancel()

    def unanswered(self, session):
        tab = self.tabs.get(session)
        if tab is not None and tab["left"]:
            self.fail()

    def fail(self):
        if not self.closed:
            self.closed = True
            self.on_failed()

    def close(self):
        self.closed = True
        try:
            if self.socket is not None:
                self.socket.close()
        except Exception:
            pass


def parse_screen(value):
    """"1512x982@2" -> {width, height, scale}; None for no or a malformed value."""
    match = re.fullmatch(r"(\d{3,5})x(\d{3,5})@(\d(?:\.\d+)?)", value or "")
    if not match:
        return None
    return {"width": int(match.group(1)), "height": int(match.group(2)), "scale": float(match.group(3))}


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


def single_sign_on_tool():
    return {"name": SSO_TOOL,
            "description": "Press the site's own \"Sign in with <provider>\" button for the provider named in "
                           "request.access.single_sign_on. Give the button's ref from the latest snapshot.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["target"],
                            "properties": {"target": {"type": "string"}, "element": {"type": "string"}}}}


def continue_tool():
    return {"name": CONTINUE_TOOL,
            "description": "Read the next piece of the last browser answer that was cut into pieces (its last "
                           "section says so). Read-only: it touches no page. Call it until the last piece.",
            "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}}


def pieces(text, size):
    """`text` cut into pieces of at most `size` characters, at line ends (a longer line is cut at `size`)."""
    found, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if current:
                found.append(current)
                current = ""
            found.append(line[:size])
            line = line[size:]
        if len(current) + len(line) > size:
            found.append(current)
            current = ""
        current += line
    if current or not found:
        found.append(current)
    return found


PAGE_LINE = re.compile(r"^- Page URL: (\S+)", re.M)   # @playwright/mcp's own page line (application_browser.PAGE_URL)


def answer_page(text):
    """The page an answer is about, as application_browser.normalize binds a whole answer: its last
    "- Page URL:" line, else None."""
    found = PAGE_LINE.findall(text or "")
    return found[-1] if found else None


def continuation(number, total, answer, page, dropped=0):
    """The guard's note at the END of every piece of cut answer `answer` (a counter, 1 for the session's
    first cut answer): which answer and piece this is and, structurally, the page the whole answer is about
    ("- Page URL:", so every piece binds that page as the whole answer does; review E4 b fix 1)."""
    lines = ["- Cut answer: %d" % answer, "- Piece: %d of %d" % (number, total)]
    if page:
        lines.append("- Page URL: %s" % page)
    note = ("This answer was cut into pieces to fit; this is piece %d of %d, the last." % (number, total)
            if number == total else
            "This answer was cut into pieces to fit; this is piece %d of %d. The rest of this answer (the rest "
            "of the page) is in the next pieces: call %s for piece %d before you judge the page."
            % (number, total, CONTINUE_TOOL, number + 1))
    if dropped:
        note += (" %d unread pieces of the earlier cut answer %d were dropped: %s now returns this answer's "
                 "pieces only." % (dropped[0], dropped[1], CONTINUE_TOOL))
    return "\n### %s\n%s\n%s" % (CONTINUATION_TITLE, "\n".join(lines), note)


def continuation_page(text):
    """The page a piece's guard note names (its "- Page URL:" line inside the CONTINUATION_TITLE section,
    the last section of every piece), else None."""
    head = "\n### %s\n" % CONTINUATION_TITLE
    if head not in (text or ""):
        return None
    found = PAGE_LINE.findall(text.rsplit(head, 1)[1])
    return found[-1] if found else None


def saved_sign_in_tool():
    return {"name": SAVED_SIGN_IN_TOOL,
            "description": "Type the saved sign-in for this site into the page. Give the snapshot ref of the email "
                           "field, of the password field, or both. Sidedoor types the values itself and never "
                           "shows them to you; it types only on the site the sign-in was saved for.",
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": {"email_target": {"type": "string"}, "password_target": {"type": "string"}}}}


def create_tool():
    return {"name": CREATE_TOOL,
            "description": "Creating an account: Sidedoor makes the new account's password and types it into the "
                           "password field (and its confirmation field) you name by snapshot ref. You never see it. "
                           "Fill every other field yourself first. If the page states password rules, pass them: "
                           "max_length (a number) and symbols (false when symbols are not allowed). It is typed "
                           "once only.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["password_targets"],
                            "properties": {"password_targets": {"type": "array", "items": {"type": "string"},
                                                                "minItems": 1, "maxItems": 3},
                                           "max_length": {"type": "integer"}, "symbols": {"type": "boolean"}}}}


def reset_tool():
    return {"name": RESET_TOOL,
            "description": "On the site's own password-reset form: Sidedoor makes a new password and types it into "
                           "the password field (and its confirmation field) you name by snapshot ref. You never see "
                           "it. Only on a site where Sidedoor itself made the account and its password is not known "
                           "to work; anywhere else it refuses. If the page states password rules, pass max_length and "
                           "symbols. It is typed once only.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["password_targets"],
                            "properties": {"password_targets": {"type": "array", "items": {"type": "string"},
                                                                "minItems": 1, "maxItems": 3},
                                           "max_length": {"type": "integer"}, "symbols": {"type": "boolean"}}}}


def link_tool():
    return {"name": LINK_TOOL,
            "description": "Open a confirmation link taken from an email. Sidedoor opens it only when it is on the "
                           "site the account is for. Always use this for a link from an email; never click a link "
                           "inside the mail page and never navigate to it yourself.",
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["url"],
                            "properties": {"url": {"type": "string"}}}}


PASSWORD_LENGTH = 20
PASSWORD_SYMBOLS = "!@#$%*-_+="


# The address of the page the browser is on (the guard's own call): where a confirmation link really led.
ADDRESS_CODE = ("async (page) => {\n"
                "  const input = %s;\n"
                "  return [{ url: page.url() }];\n"
                "}" % json.dumps({"sidedoor": "page-address"}))


def generate_password(max_length=None, symbols=True):
    """A random password for one site: letters and digits, with symbols unless the page forbids them, at
    least one of each kind, PASSWORD_LENGTH long unless the page states a shorter limit (never under 8)."""
    import secrets
    import string
    length = PASSWORD_LENGTH
    if isinstance(max_length, int) and not isinstance(max_length, bool) and 8 <= max_length < length:
        length = max_length
    kinds = [string.ascii_lowercase, string.ascii_uppercase, string.digits] + ([PASSWORD_SYMBOLS] if symbols is not False else [])
    while True:
        value = "".join(secrets.choice("".join(kinds)) for _ in range(length))
        if all(any(character in kind for character in value) for kind in kinds):
            return value


def password_code(site, refs, password):
    """Playwright code that types `password` into every field of `refs` and answers [{outcome}], never a
    value. The checks of `fill_code`, all before anything is typed: the page and each field's own frame on
    `site`, each field in the page, each a password field."""
    return ("async (page) => {\n"
            "  const input = %s;\n"
            "  const site = (u) => { try { const x = new URL(u); if (x.protocol !== 'http:' && x.protocol !== 'https:') return null;\n"
            "    return x.hostname.toLowerCase().replace(/\\.+$/, '').replace(/^www\\./, '') || null; } catch (e) { return null; } };\n"
            "  if (site(page.url()) !== input.site) return [{ outcome: 'wrong_site' }];\n"
            "  const fields = [];\n"
            "  for (const ref of input.refs) {\n"
            "    let handle = null;\n"
            "    try { handle = await page.locator('aria-ref=' + ref).elementHandle({ timeout: 3000 }); } catch (e) {}\n"
            "    if (!handle) return [{ outcome: 'not_found' }];\n"
            "    let frame = null;\n"
            "    try { frame = await handle.ownerFrame(); } catch (e) {}\n"
            "    if (!frame || site(frame.url()) !== input.site) return [{ outcome: 'wrong_site' }];\n"
            "    let secret = false;\n"
            "    try { secret = await handle.evaluate((e) => e.tagName === 'INPUT' && e.type === 'password'); } catch (e) {}\n"
            "    if (!secret) return [{ outcome: 'not_password_field' }];\n"
            "    fields.push(handle);\n"
            "  }\n"
            "  if (site(page.url()) !== input.site) return [{ outcome: 'wrong_site' }];\n"
            "  let typed = 0;\n"
            "  for (const handle of fields) {\n"
            "    try { await handle.fill(input.password, { timeout: 5000 }); typed += 1; }\n"
            "    catch (e) { return [{ outcome: typed ? 'unconfirmed' : 'failed', typed }]; }\n"
            "  }\n"
            "  return [{ outcome: 'filled', typed }];\n"
            "}" % json.dumps({"sidedoor": "new-account-password", "site": site, "refs": refs, "password": password}))


def fill_code(site, email_ref, password_ref, email, password):
    """Playwright code that types the saved sign-in and answers [{outcome, ...}], never a value. Everything is
    checked before anything is typed: the page's own address and each named field's own frame must be on
    `site` (site_sessions.site_key's rule, on the live page), each field must be in the page, and the password
    field must be a password field. The page's address is read again just before typing."""
    return ("async (page) => {\n"
            "  const input = %s;\n"
            "  const site = (u) => { try { const x = new URL(u); if (x.protocol !== 'http:' && x.protocol !== 'https:') return null;\n"
            "    return x.hostname.toLowerCase().replace(/\\.+$/, '').replace(/^www\\./, '') || null; } catch (e) { return null; } };\n"
            "  if (site(page.url()) !== input.site) return [{ outcome: 'wrong_site' }];\n"
            "  const fields = [];\n"
            "  for (const kind of ['email', 'password']) {\n"
            "    const ref = input[kind + '_ref'];\n"
            "    if (!ref) continue;\n"
            "    let handle = null;\n"
            "    try { handle = await page.locator('aria-ref=' + ref).elementHandle({ timeout: 3000 }); } catch (e) {}\n"
            "    if (!handle) return [{ outcome: 'not_found', field: kind }];\n"
            "    let frame = null;\n"
            "    try { frame = await handle.ownerFrame(); } catch (e) {}\n"
            "    if (!frame || site(frame.url()) !== input.site) return [{ outcome: 'wrong_site', field: kind }];\n"
            "    if (kind === 'password') {\n"
            "      let secret = false;\n"
            "      try { secret = await handle.evaluate((e) => e.tagName === 'INPUT' && e.type === 'password'); } catch (e) {}\n"
            "      if (!secret) return [{ outcome: 'not_password_field' }];\n"
            "    }\n"
            "    fields.push([kind, handle]);\n"
            "  }\n"
            "  if (site(page.url()) !== input.site) return [{ outcome: 'wrong_site' }];\n"
            "  const done = { outcome: 'filled', email: false, password: false };\n"
            "  for (const [kind, handle] of fields) {\n"
            "    try { await handle.fill(input[kind], { timeout: 5000 }); done[kind] = true; }\n"
            "    catch (e) { return [{ outcome: 'failed', field: kind, email: done.email, password: done.password }]; }\n"
            "  }\n"
            "  return [done];\n"
            "}" % json.dumps({"sidedoor": "saved-sign-in-fill", "site": site, "email_ref": email_ref,
                              "password_ref": password_ref, "email": email, "password": password}))


def secret_forms(value):
    """The spellings of a typed value a page or the browser server may show it in: as typed, JSON-escaped
    (a snapshot's quoted text), HTML-escaped, URL-encoded (Python's two, the browser's form body, which
    leaves `*` and encodes `~`, and encodeURIComponent, which leaves !~*'(); each with upper- and
    lower-case hex), and base64 of the value by itself (standard and URL-safe, with and without padding).
    Longest first."""
    import base64
    import html
    import urllib.parse
    encoded = {urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value, safe=""),
               urllib.parse.quote_plus(value, safe="*").replace("~", "%7E"), urllib.parse.quote(value, safe="!~*'()")}
    encoded |= {re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), form) for form in encoded}
    raw = value.encode("utf-8")
    based = {base64.b64encode(raw).decode("ascii"), base64.urlsafe_b64encode(raw).decode("ascii")}
    based |= {form.rstrip("=") for form in based}
    forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], html.escape(value),
             html.escape(value, quote=False),
             html.escape(value).replace("&quot;", "&#34;").replace("&#x27;", "&#39;")} | encoded | based
    return sorted((form for form in forms if form), key=len, reverse=True)


def clean_folder(folder, forms, since=None):
    """The session's output folder (@playwright/mcp's --output-dir: what the page tools' `filename` writes)
    after a saved sign-in was typed. Every text file there loses the value (`without_secrets`). A file that
    is not text (a screenshot) cannot be cleaned: given `since` (the session has ended; when the first fill
    happened), each one written from then on is deleted. Returns (files rewritten, files deleted). Never
    raises; never follows a link."""
    import pathlib
    scrubbed = removed = 0
    if not forms or not folder:
        return 0, 0
    try:
        files = [path for path in pathlib.Path(folder).rglob("*") if path.is_file() and not path.is_symlink()]
    except OSError:
        return 0, 0
    for path in files:
        try:
            data = path.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                if since is not None and path.stat().st_mtime >= since - 2:
                    path.unlink()
                    removed += 1
                continue
            cleaned = without_secrets(text, forms)
            if cleaned != text:
                path.write_bytes(cleaned.encode("utf-8"))
                scrubbed += 1
        except OSError:
            continue
    return scrubbed, removed


def without_secrets(value, forms):
    """`value` (any JSON value) with every one of `forms` replaced by SAVED_SIGN_IN_MARK in its strings."""
    if not forms:
        return value
    if isinstance(value, str):
        for form in forms:
            if form in value:
                value = value.replace(form, SAVED_SIGN_IN_MARK)
        return value
    if isinstance(value, list):
        return [without_secrets(item, forms) for item in value]
    if isinstance(value, dict):
        return {without_secrets(key, forms): without_secrets(item, forms) for key, item in value.items()}
    return value


def focus_refusal(name, args):
    """A call that would bring Chrome to the front: a Refusal, else None. Every operation."""
    if not isinstance(args, dict):
        return None
    if name == "browser_evaluate" and BRING_TO_FRONT.search(
            " ".join(str(args.get(key) or "") for key in ("function", "filename"))):
        return Refusal(FOCUS_CODE, "page.bringToFront() brings Sidedoor's browser in front of the user's screen, "
                       "which is never done. Leave it out; the page works the same behind other windows.")
    return None


# --- the cookie jar is not the model's to read (E15.T4, owner 2026-11-12) ---------------------------
# With browser_run_code_unsafe gone (REMOVED_TOOL) the model has no Playwright object, so no path to the
# jar itself. What is left: in-page code (browser_evaluate) can read the page's own document.cookie
# (cookies NOT marked httpOnly; owner accepted, Q6 a), which `cookie_refusal` refuses when the code names
# it (a text check: indirect spelling gets around it), and the header lines of the network tools.
PAGE_JAR_NAMES = ("cookie", "cookieStore")     # document.cookie, the Cookie Store API
SECRET_HEADERS = ("cookie", "set-cookie", "authorization", "proxy-authorization")
JAR_CODE = "SESSION_SECRET_REFUSED"
JAR_TEXT = "Cookies are not available to this session: they hold the user's sign-ins. Leave that out."
JAR_PATTERN = re.compile(r"(?<![\w$])(?:%s)(?![\w$])" % "|".join(PAGE_JAR_NAMES))
HEADER_TOOLS = ("browser_network_request", "browser_network_requests")
# @playwright/mcp 0.0.83 prints a header as `<indent><name>: <value>` (four spaces inside
# browser_network_request's full details, none for a single `part`), and Playwright joins the values of a
# repeated header (Set-Cookie) with "\n", so a value may run on over lines of its own that start at column 0.
# A header line: optional indent, an HTTP token, ":" and a space. A cookie line never looks like one (its
# first token ends at "="), so a removed header's value runs until a blank line, a line starting "#", a
# line indented less than the header but not at column 0 (the next section's title), or the next header.
HEADER_START = re.compile(r"^([ \t]*)([!#$%&'*+.^_`|~0-9A-Za-z-]+):(?:[ \t]|$)")

def cookie_refusal(name, args):
    """A Refusal for in-page code (browser_evaluate) that names the page's cookies; else None."""
    code = args.get("function") if name == "browser_evaluate" and isinstance(args, dict) else None
    if not isinstance(code, str):
        return None
    # Words inside a longer string literal are data ("Accept cookies"); a literal that is exactly one of
    # the names is an access (document["cookie"]). Code the small lexer will not read is checked whole.
    import application_browser
    lexed = application_browser.code_outside_literals(code)
    if (JAR_PATTERN.search(code) if lexed is None else JAR_PATTERN.search(lexed[0])
            or any(literal.strip() in PAGE_JAR_NAMES for literal in lexed[1])):
        return Refusal(JAR_CODE, JAR_TEXT)
    return None


# A further value of a repeated Set-Cookie header, on a line of its own: `<cookie name>=...`.
COOKIE_PAIR = re.compile(r"^[ \t]*[!#$%&'*+.^_`|~0-9A-Za-z-]+=")
BODY_PARTS = ("request-body", "response-body")   # browser_network_request `part`: the answer is a body, not headers


def scrub_text(text, body=False):
    """`text` with the values of its Cookie, Set-Cookie and Authorization headers, and their continuation
    lines, replaced by "[removed]". In header listings every line up to the next header is a continuation.
    `body` (the answer is a request or response body, where a line that looks like one of those headers
    is still removed): only the lines that follow it and look like further cookies (`name=...`) go with
    it; the body's other lines are kept (I1: a body lost every line after such a line)."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        start = HEADER_START.match(line)
        if not start or start.group(2).lower() not in SECRET_HEADERS:
            out.append(line)
            i += 1
            continue
        indent = len(start.group(1))
        out.append(start.group(1) + start.group(2) + ": [removed]")
        i += 1
        while i < len(lines):
            following = lines[i]
            lead = len(following) - len(following.lstrip(" \t"))
            if (not following.strip() or following.lstrip().startswith("#") or 0 < lead < indent
                    or (lead == indent and HEADER_START.match(following))
                    or (body and not COOKIE_PAIR.match(following))):
                break
            i += 1                     # a continuation of the removed value
    return "\n".join(out)


def scrub_headers(message, body=False):
    """Layer 3: a tool answer with the values of Cookie, Set-Cookie and Authorization headers removed."""
    result = message.get("result") if isinstance(message, dict) else None
    for block in (result.get("content") if isinstance(result, dict) else None) or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"] = scrub_text(block["text"], body)
    return message


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

    def shown(self, pid):
        """True when app `pid` has a normal window on screen (Quartz's on-screen window list: owner pid
        and layer only, no screen-recording permission needed; a minimized window is not on screen)."""
        try:
            import Quartz
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []
            return any(item.get("kCGWindowOwnerPID") == pid and item.get("kCGWindowLayer") == 0 for item in windows)
        except Exception:
            return False

    def activate(self, pid):
        """Give the front back to app `pid` (the app the user was in, on the Space they are on). True when
        a route accepted the request. `lsappinfo setfront` asks LaunchServices to make that process the
        front one; NSRunningApplication.activateWithOptions_(0) (no NSApplicationActivateAllWindows: only its
        key window comes forward) is the fallback. Neither picks a Space: macOS shows the Space holding the
        activated app's key window, and that app was frontmost on the user's current Space (or is the
        full-screen app whose Space it is) a moment ago, so the user stays where they were. When Chrome's
        launch already moved macOS off a full-screen app's Space, activating that app's process is what
        brings its Space back (to be confirmed on a real Mac with a full-screen Terminal)."""
        if self._out(["/usr/bin/lsappinfo", "setfront", "#%d" % int(pid)]) is not None and self.front() == pid:
            return True
        try:
            from AppKit import NSRunningApplication
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            return bool(app is not None and app.activateWithOptions_(0))
        except Exception:
            return False


def pid_alive(pid):
    """True while process `pid` runs (signal 0: nothing is sent). Windows: os.kill(pid, 0) is
    CTRL_C_EVENT there (it interrupted the CI test run), so portable's OpenProcess probe instead."""
    if os.name == "nt":
        try:
            return portable.pid_alive(int(pid))
        except (OSError, ValueError):
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError):
        return True
    return True


class FocusKeeper:
    """Owner, 2026-10-24: "please ensure it doesn't make any guesses about which screen I'm on and it
    minimizes itself extremely quickly and returns me back to the screen I was on". Every
    FOCUS_POLL_SECONDS while a headed session runs, the guard reads the frontmost app (`front()`, by pid):
    - any app that is not Sidedoor's own Chrome (`chrome_pid()`: the process on Sidedoor's profile; the
      user's everyday Chrome is another pid and is one of the user's apps) is recorded with the time it was
      seen: the app in front just before Chrome comes forward is the last one recorded, at most one poll
      earlier. While the user is there, a Sidedoor Chrome window on screen (`chrome_shown(pid)`: a window
      un-minimized by Chrome itself or newly opened) is minimized at once (`hide()`);
    - Sidedoor's Chrome in front: the front goes back to that recorded app at once (`activate`), every
      time, and its windows are minimized (`hide()`). There is no "the user is watching" exception; the
      one exception is `suspended()`: support_email's window once it may come forward.
    Each correction is written as FOCUS_RESTORED (to_pid, restored, seen_ms: how long before the
    correction that app was last seen in front, an upper bound on Chrome's activation to its detection;
    correct_ms: how long the switch back took); one that keeps failing at most once per
    FOCUS_FAILED_LOG_SECONDS with a count. Setup step 8's sign-in window runs without the guard, so the
    keeper never sees it. `poll()` never raises."""

    def __init__(self, chrome_pid, front, activate, suspended=None, log=None, clock=None, chrome_shown=None,
                 hide=None, alive=None):
        self.chrome_pid, self.front, self.activate = chrome_pid, front, activate
        self.suspended = suspended or (lambda: False)
        self.log = log or (lambda **entry: None)
        self.clock = clock or time.monotonic
        self.chrome_shown = chrome_shown or (lambda pid: False)
        self.hide = hide or (lambda: None)
        self.alive = alive or pid_alive
        self.last_other = None     # the frontmost app last seen that is not Sidedoor's Chrome
        self.last_other_at = None  # when it was last seen in front
        self.history = []          # the apps the user was in, most recent last (distinct pids, bounded)
        self.failed_logged_at = None
        self.failed_unlogged = 0
        self.lock = threading.Lock()   # one poll at a time

    def poll(self):
        try:
            with self.lock:
                self._poll()
        except Exception as error:   # never stops the guard
            try:
                self.log(decision="FOCUS_KEEPER_ERROR", error=(str(error) or type(error).__name__)[:200])
            except Exception:
                pass

    def record_front(self):
        """Record the app in front now (when it is not Sidedoor's Chrome): called before the guard itself
        starts a headed Chrome, so the user is returned there. Never raises."""
        try:
            with self.lock:
                pid = self.front()
                if pid is not None and pid != self.chrome_pid():
                    self._seen(pid, self.clock())
        except Exception as error:
            self.log(decision="FOCUS_KEEPER_ERROR", error=(str(error) or type(error).__name__)[:200])

    def _seen(self, pid, now):
        self.last_other, self.last_other_at = pid, now
        if not self.history or self.history[-1] != pid:
            self.history = [item for item in self.history if item != pid][-15:] + [pid]

    def target(self):
        """The last app the user was actually in that still runs (a quit one is dropped), or None."""
        while self.history and not self.alive(self.history[-1]):
            self.history.pop()
        return self.history[-1] if self.history else None

    def _hide_if_shown(self, chrome):
        if self.chrome_shown(chrome):       # every window already minimized: no CDP call, nothing logged
            self.hide()

    def _poll(self):
        now = self.clock()
        pid = self.front()
        if pid is None:
            return
        chrome = self.chrome_pid()
        if chrome is None or pid != chrome:
            self._seen(pid, now)
            if chrome is not None and not self.suspended():
                self._hide_if_shown(chrome)
            return
        if self.suspended():
            return                          # support_email: the user's own window
        target = self.target()
        if target is None:
            self._hide_if_shown(chrome)
            return                          # no app the user was in still runs: nothing to return to
        if target != self.last_other:
            self.last_other_at = None       # the recorded app quit: the one before it, never a guess
        self.last_other = target
        started = self.clock()
        restored = bool(self.activate(target))
        done = self.clock()
        self._hide_if_shown(chrome)
        entry = dict(to_pid=self.last_other, restored=restored,
                     seen_ms=round((started - self.last_other_at) * 1000, 1) if self.last_other_at is not None else None,
                     correct_ms=round((done - started) * 1000, 1))
        if restored:
            self.failed_logged_at, self.failed_unlogged = None, 0
            self.log(decision="FOCUS_RESTORED", **entry)
            return
        self.failed_unlogged += 1
        if self.failed_logged_at is None or done - self.failed_logged_at >= FOCUS_FAILED_LOG_SECONDS:
            self.log(decision="FOCUS_RESTORED", count=self.failed_unlogged, **entry)
            self.failed_logged_at, self.failed_unlogged = done, 0


class Refusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


class Proxy:
    # E15.T7: none unless the session was started for a site (see __init__)
    saved_sign_in, typed, filled_at, output_folder = None, (), None, None
    # E15.T8: the sign-up email; the one typing of a new password; the session is an access step
    create_account, password_typed, access_step = None, False, False
    single_sign_on = False   # E15.T9: held only when given (a Proxy built without __init__ holds none)

    def __init__(self, operation, child_argv, audit=None, claim=None, human_check=None, snapshot_timeout=30.0,
                 stdin=None, stdout=None, arm_binding=None, human_check_wait=None, uploads=None,
                 call_timeout=CALL_TIMEOUT_SECONDS, background_chrome=None, launch_background=None,
                 offscreen=False, clock=None, sleep=None, focus_keeper=None, headless_user_agent=None,
                 launch_headless=None, client_hints=None, screen=None,
                 piece_chars=None, site_session=None, saved_sign_in=None, create_account=None,
                 access_step=False, make_tab_details=None, single_sign_on=False, starter_pid=None, no_page_code=False,
                 start_url=None,
                 reset_recorded=None):
        self.operation = operation
        # The process that started this session and stops its Chrome at the end (browser_runtime.STARTER_VARIABLE,
        # which every engine passes on to this server; tests give it).
        if starter_pid is None:
            import browser_runtime
            named = os.environ.get(browser_runtime.STARTER_VARIABLE, "")
            starter_pid = int(named) if named.isdigit() else None
        self.starter_pid = starter_pid
        self.stop_reason = None    # the guard stopped its own session (stop_session)
        self.closing = False
        self.requests = queue.Queue()   # before Chrome starts: stop_session may be called from then on
        self.tab_details, self.make_tab_details = None, make_tab_details   # Windows, hidden: TabDetails
        self.stop_file = None
        # E15.T7 (saved_sign_ins.py): the site whose saved sign-in this session may ask the guard to type.
        # `typed`: the spellings of every value the guard typed, removed from all that leaves the guard.
        self.saved_sign_in = saved_sign_in
        # E15.T8: the email a new account on that site is made under; only with a site.
        self.create_account = create_account if saved_sign_in and create_account else None
        self.password_typed = False
        self.access_step = bool(access_step and saved_sign_in)   # holds the confirmation-link tool
        self.no_page_code = bool(no_page_code)
        # The email of Sidedoor's own recorded sign-up on this site, when the application job found one
        # (access_step.own_account): a reset is then permitted with no pending password left.
        self.reset_recorded = reset_recorded if self.access_step and isinstance(reset_recorded, str) and reset_recorded else None
        self.single_sign_on = bool(single_sign_on and self.access_step)   # E15.T9: only the access step (SSO_TOOL)
        # E16.T2 (owner, 2026-11-13: "first there was a blank page"): an attended submit's guard opens the
        # application's own address itself, as soon as the session's browser is set up (`open_start_page`),
        # so the user sees the page before the engine's first step. Attended submit only (`call_human_check`'s
        # hold with the arm claim); an http(s) address only.
        self.start_url = (start_url if isinstance(start_url, str) and START_URL.fullmatch(start_url)
                          and operation == "submit" and human_check else None)
        self.typed = []
        self.filled_at = None      # wall-clock time of the first fill that may have typed
        # The browser server's output folder: what the page tools' `filename` writes (cleaned after a fill).
        self.output_folder = (child_argv[child_argv.index("--output-dir") + 1]
                              if "--output-dir" in list(child_argv)[:-1] else None)
        # E15.T4 (site_sessions.py): the site whose saved cookies are restored after `initialized` and
        # captured while the session runs. Only the guard's own calls carry them; none is ever logged.
        self.site_session = site_session
        self.site_captured_at = None
        self.offscreen = offscreen
        # Headless (owner, 2026-10-24, option A): given --headless-user-agent with --background-chrome the
        # guard starts the profile's Chrome headless (no window, nothing to minimize, no focus keeper);
        # `self.headless` is True only when that Chrome is the guard's own.
        self.headless_user_agent = headless_user_agent if background_chrome else None
        self.headless = False
        self.endpoint = None
        self.launch_headless = launch_headless
        self.client_hints = client_hints   # H3: userAgentMetadata for a headless session (browser_runtime)
        self.screen = screen               # {width, height, scale}: the real display a headless session reports
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.placed_at = None      # when the windows were last minimized
        self.unchanged_logged_at, self.unchanged_count = None, 0   # minimizes that changed nothing
        self.hiding = False        # a minimize the focus keeper asked for is running
        self.hide_asked_at = None
        self.stopped = threading.Event()
        self.focus_keeper = focus_keeper   # None: built when needed (macOS, headed); False: none
        self.keeper_thread = None
        self.child = None          # the browser server, once started (after Chrome)
        self.initialized = False   # the client's notifications/initialized has reached it
        self.place_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.audit = open(audit, "a", encoding="utf-8") if audit else None
        if audit:
            import browser_runtime
            import pathlib
            self.stop_file = pathlib.Path(audit).parent / browser_runtime.GUARD_STOPPED
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
        self.child_argv = list(child_argv)
        self.child = self.spawn_child()
        # The arm tool is built from the same private file the self-spawned server read.
        if arm_binding is None and claim:
            import application_browser
            arm_binding = application_browser.ArmBinding(claim)
        # E16.T1: no application session is given a wait (a human check ends it at once), so --human-check
        # builds one for support_email only (its sign-in wait). A hold object passed in (`human_check_wait`)
        # is still honoured in any operation: the dormant hold a later task turns on (`call_human_check`).
        if human_check_wait is None and human_check and operation == "support_email":
            import support_email
            human_check_wait = support_email.SignInWait(human_check)
        # E16.T2: an ATTENDED submit session is given the hold (form_check_hold.prepare writes its context
        # only for one): the user does the form's human check while this one call is held. Only with the
        # arm claim, so the hold can refuse itself once the arm was called.
        if human_check_wait is None and human_check and operation == "submit" and arm_binding is not None:
            import form_check_hold
            human_check_wait = form_check_hold.Hold(human_check, arm_binding)
        self.arm_binding = arm_binding
        self.human_check_wait = human_check_wait
        self.out_lock = threading.Lock()
        self.child_lock = threading.Lock()
        self.own = {}  # proxy-originated request id -> queue
        self.issued = set()  # every id the guard itself sent: a late answer to one is never relayed (fix 12)
        self.expired = set()  # forwarded call ids the guard answered on timeout: their late answers are dropped
        self.uploads = uploads  # the read-only upload folder announced as the browser's one root (fix 4)
        self.piece_chars = piece_chars   # Claude only (CONTINUE_TOOL); None: answers pass whole, as for Codex
        self.waiting_pieces, self.piece_count, self.piece_page, self.cut_answers = [], 0, None, 0
        self.piece_lock = threading.Lock()
        self.call_timeout = call_timeout
        self.listing = set()  # ids of the client's tools/list requests, answered with the guard's tools added
        self.counter = 0
        self.label = None
        # Forwarded tools/call ids still running. A click is judged only when none is running, so the
        # guard's snapshot shows the page the click will meet.
        self.in_flight = set()
        self.idle = threading.Condition()
        self.header_answers = {}     # network-tool call id -> True when its answer is a body (BODY_PARTS)

    def start_background_chrome(self, child_argv, path, launch=None):
        """Start Sidedoor's Chrome on `path` without activating it (headless given a user agent) and
        attach @playwright/mcp to it (`--cdp-endpoint`). If that cannot start, @playwright/mcp launches
        Chrome itself on the same profile as before (with the same headless or off-screen switches,
        browser_runtime.launch_config; recorded as BACKGROUND_LAUNCH_FAILED)."""
        import browser_runtime
        if self.headless_user_agent:
            try:
                if self.launch_headless is not None:
                    endpoint = self.launch_headless(path, self.headless_user_agent)
                else:
                    import setup_steps
                    binary = setup_steps.chrome_path()
                    if not binary:
                        raise OSError("Google Chrome not found")
                    if browser_runtime.windows_host():
                        # Windows (owner, 2026-11-12, option A): hidden only with everything the page is
                        # told read from this machine; the window fits the real display.
                        if not (self.client_hints and self.screen):
                            raise OSError("this computer's Chrome or display could not be read")
                        endpoint = browser_runtime.launch_hidden_windows(binary, path, self.headless_user_agent,
                                                                         screen=self.screen)
                        endpoint = self.confirm_version(binary, path, endpoint)
                        self.hold_new_tabs(path, endpoint)
                    else:
                        endpoint = browser_runtime.launch_headless(binary, path, self.headless_user_agent,
                                                                   scale=(self.screen or {}).get("scale"))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                # H2: a headless launch that fails runs this session headed and minimized (the --offscreen
                # rules, focus keeper included): `open -g` first, then Playwright's own launch, whose
                # switches are the off-screen ones (browser_runtime.launch_config).
                self.log(decision="HEADLESS_LAUNCH_FAILED", reason=str(error)[:200] or type(error).__name__)
                self.headless_user_agent = None
                if browser_runtime.windows_host():
                    # Windows has no minimized mode: @playwright/mcp starts Chrome itself on the profile,
                    # in an ordinary window with no off-screen switches (as before the hidden launch).
                    self.background_chrome = None
                    return child_argv + ["--user-data-dir", str(path)]
                self.offscreen = True
                return self.start_background_chrome(child_argv, path, launch)
            self.endpoint = endpoint
            self.headless = True
            self.log(decision="BACKGROUND_LAUNCHED", endpoint=endpoint, headless=True)
            return child_argv + ["--cdp-endpoint", endpoint]
        # Every headed launch (owner-approved, 2026-10-24): `open -g` still makes Chrome the frontmost app
        # a moment after it starts (and macOS may leave the user's full-screen Space for it), so the app in
        # front is recorded and the focus keeper runs BEFORE Chrome starts: its first activation is undone
        # at once, not left until a later poll finds nothing to return to.
        # support_email (SHOWN_OPERATIONS, owner 2026-10-25): the same, headed, without minimizing.
        shown = self.operation in browser_runtime.SHOWN_OPERATIONS
        if self.offscreen or shown:
            self.record_front(headed=shown)
            self.start_focus_keeper(headed=shown)
        try:
            if launch is None:
                import setup_steps
                app = browser_runtime.chrome_app(setup_steps.chrome_path() or "")
                if app is None:
                    raise OSError("Google Chrome.app not found")
                endpoint = browser_runtime.launch_background(
                    app, path, onscreen=self.operation in browser_runtime.SHOWN_OPERATIONS)
            else:
                endpoint = launch(path)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.log(decision="BACKGROUND_LAUNCH_FAILED", reason=str(error)[:200] or type(error).__name__)
            self.background_chrome = None
            return child_argv + ["--user-data-dir", str(path)]
        self.endpoint = endpoint
        self.log(decision="BACKGROUND_LAUNCHED", endpoint=endpoint)
        return child_argv + ["--cdp-endpoint", endpoint]

    def hold_new_tabs(self, path, endpoint):
        """Windows: every tab this hidden Chrome opens gets its details before its first document
        (`TabDetails`). If that cannot be armed, this Chrome is ended and OSError raised (no hidden launch)."""
        import browser_runtime
        try:
            self.tab_details = (self.make_tab_details or TabDetails)(
                endpoint, self.headless_user_agent, self.client_hints, self.screen,
                lambda: self.stop_session("TAB_DETAILS_FAILED")).start()
        except OSError:
            self.tab_details = None
            browser_runtime.end_session({"browser_driver": {"profile": path}})
            raise

    def confirm_version(self, binary, path, endpoint):
        """Windows: the version a page is told must be the running Chrome's own (its DevTools
        `/json/version`), not only the one read from chrome.exe before the launch: a Chrome update can
        take effect as Chrome starts. The same: `endpoint`. Different: that Chrome is ended and started
        once more telling the running version (user agent and client hints, `windows_retold`), and the
        new endpoint is returned. A version that cannot be read, or that differs again, is never
        presented: every Chrome started here is ended and OSError raised (Playwright's own launch follows)."""
        import browser_runtime
        for again in (False, True):
            running = browser_runtime.running_version(endpoint)
            told = (self.client_hints or {}).get("fullVersion")
            if running is not None and running == told:
                return endpoint
            browser_runtime.end_session({"browser_driver": {"profile": path}})
            if running is None or again:
                raise OSError("the running Chrome's version could not be confirmed")
            self.log(decision="CHROME_VERSION_CHANGED", told=told, running=running)
            self.headless_user_agent, self.client_hints = browser_runtime.windows_retold(self.client_hints, running)
            endpoint = browser_runtime.launch_hidden_windows(binary, path, self.headless_user_agent, screen=self.screen)

    # --- plumbing -----------------------------------------------------------------------------
    def to_client(self, message):
        message = without_secrets(message, self.typed)   # E15.T7: a typed value never leaves the guard
        with self.out_lock:
            self.stdout.write(json.dumps(message) + "\n")
            self.stdout.flush()

    def to_child(self, message):
        with self.child_lock:
            self.child.stdin.write(json.dumps(message) + "\n")
            self.child.stdin.flush()

    def log(self, **entry):
        if self.audit:
            entry = without_secrets(entry, self.typed)   # E15.T7: nor is it written to the audit
            entry["at"] = time.time()
            with self.audit_lock:
                self.audit.write(json.dumps(entry) + "\n")
                self.audit.flush()

    def code_free(self):
        """True when this session is neither listed nor run the in-page code tool: the webmail read and the
        support email (browser_runtime.CODE_FREE_OPERATIONS), and Sidedoor's own access and profile steps
        (`--no-page-code`): the starter's live check ends any application session at that tool
        (application_browser.tool_event), so a step that was offered it could be ended with no report."""
        import browser_runtime
        return self.operation in browser_runtime.CODE_FREE_OPERATIONS or bool(getattr(self, "no_page_code", False))

    def offered(self):
        """Every tool name this session forwards or answers: the known browser tools (less the code tools
        in a code-free operation) and the guard's own tools this session holds."""
        import browser_runtime
        hidden = WEBMAIL_HIDDEN if self.code_free() else ()
        own = {ARM_TOOL: self.arm_binding is not None, HUMAN_CHECK_TOOL: self.human_check_wait is not None,
               CONTINUE_TOOL: bool(self.piece_chars), SAVED_SIGN_IN_TOOL: bool(self.saved_sign_in),
               CREATE_TOOL: bool(self.create_account), LINK_TOOL: bool(self.access_step),
               RESET_TOOL: bool(getattr(self, "reset_recorded", None)),
               SSO_TOOL: self.single_sign_on}
        return (frozenset(tool for tool in TOOLS if tool not in hidden)
                | frozenset(name for name, held in own.items() if held)) - {REMOVED_TOOL}

    def own_tools(self):
        tools = []
        if self.arm_binding is not None:
            tools.append(arm_tool())
        # Listed only when the hold has a listing of its own (support_email's sign-in wait): the retired
        # CAPTCHA wait's listing is gone (E16.T1), so a hold without one is never offered to the model.
        listing = getattr(self.human_check_wait, "listing", None)
        if callable(listing):
            tools.append(listing())
        if self.single_sign_on:
            tools.append(single_sign_on_tool())
        if self.piece_chars:
            tools.append(continue_tool())
        if self.saved_sign_in:
            tools.append(saved_sign_in_tool())
        if self.create_account:
            tools.append(create_tool())
        if self.access_step:
            tools.append(link_tool())
            if getattr(self, "reset_recorded", None):
                tools.append(reset_tool())
        return tools

    def in_pieces(self, message):
        """A tools/call answer whose text is longer than piece_chars, cut (`pieces`): the first piece with its
        note (`continuation`); the rest wait for CONTINUE_TOOL. A new cut answer replaces the pieces still
        waiting, and its note says so. Any other message is returned as it is."""
        result = message.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        if not (isinstance(content, list) and content and all(
                isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                for block in content)):
            return message
        text = "\n".join(block["text"] for block in content)
        if len(text) <= self.piece_chars:
            return message
        page = answer_page(text)
        reserve = len(continuation(10 ** 6, 10 ** 6 + 1, 10 ** 6, page, (10 ** 6, 10 ** 6)))   # the longest note
        found = pieces(text, max(1000, self.piece_chars - reserve))
        with self.piece_lock:
            dropped = (len(self.waiting_pieces), self.cut_answers) if self.waiting_pieces else 0
            self.cut_answers += 1
            self.waiting_pieces, self.piece_count, self.piece_page = found[1:], len(found), page
            first = found[0] + continuation(1, len(found), self.cut_answers, page, dropped)
        self.log(decision="IN_PIECES", answer=self.cut_answers, pieces=len(found), chars=len(text),
                 dropped=dropped[0] if dropped else 0)
        return dict(message, result=dict(result, content=[{"type": "text", "text": first}]))

    def call_continue(self, message):
        """The next piece of the latest cut answer, after every forwarded call has been answered (so a cut
        answer still on its way is the one continued)."""
        with self.idle:
            self.idle.wait_for(lambda: not self.in_flight, timeout=self.call_timeout)
        with self.piece_lock:
            if self.waiting_pieces:
                number = self.piece_count - len(self.waiting_pieces) + 1
                text = self.waiting_pieces.pop(0) + continuation(number, self.piece_count, self.cut_answers,
                                                                 self.piece_page)
            else:
                number, text = None, "No piece is waiting: the last answer was not cut, or every piece was read."
        self.log(decision="CONTINUED", answer=self.cut_answers, piece=number)
        self.answer(message, text, False)

    def spawn_child(self):
        return subprocess.Popen(self.child_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                bufsize=1, encoding="utf-8", env=dict(os.environ), **portable.no_window())

    def pump_child(self, child=None):
        child = child or self.child
        for line in child.stdout:
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
            self.clean_output()   # E15.T7: a file this call wrote is clean before the client can ask for it
            message = without_secrets(message, self.typed)   # before an answer is cut into pieces
            if "method" not in message and self.hashable(ident) in self.header_answers:
                message = scrub_headers(message, self.header_answers.pop(self.hashable(ident)))
            if "method" not in message and self.piece_chars and ident not in self.listing:
                message = self.in_pieces(message)   # before the call counts as answered (call_continue waits)
            if "method" not in message:
                with self.idle:
                    self.in_flight.discard(ident)
                    self.idle.notify_all()
                if ident in self.listing:
                    self.listing.discard(ident)
                    result = message.get("result")
                    if isinstance(result, dict) and isinstance(result.get("tools"), list):
                        import browser_runtime
                        hidden = OWN_TOOLS + (WEBMAIL_HIDDEN if self.code_free() else ())
                        # Q5: only the allow-listed browser tools are listed (a tool not forwarded is not offered).
                        result["tools"] = [tool for tool in result["tools"] if isinstance(tool, dict)
                                           and tool.get("name") in TOOLS and tool.get("name") not in hidden
                                           and tool.get("name") != REMOVED_TOOL]
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
    def place_window(self):
        """Minimize every window of the session's Chrome. Recorded in the audit; never raises.
        Returns the read-back list or None."""
        import browser_runtime
        if not self.offscreen:
            return None
        with self.place_lock:
            began = self.clock()
            code = hide_code(*browser_runtime.OFFSCREEN_POSITION, *browser_runtime.WINDOW_SIZE)
            try:
                answer = self.child_call("tools/call", {"name": PLACE_TOOL, "arguments": {"code": code}})
            except (OSError, ValueError) as error:
                answer = {"error": str(error)[:200]}
            placed = placed_windows(answer)
            self.placed_at = self.clock()
        errors = [item for item in placed or [] if "error" in item]
        placed = [item for item in placed or [] if "error" not in item] if placed is not None else None
        if not placed:
            self.log(decision="WINDOW_PLACEMENT_FAILED", onscreen=False, errors=errors)
            return None
        unchanged = not errors and all(item.get("was") == "minimized" for item in placed)
        now = self.clock()
        if unchanged:   # nothing was un-minimized: written at most once per FOCUS_FAILED_LOG_SECONDS, counted
            self.unchanged_count += 1
            if self.unchanged_logged_at is not None and now - self.unchanged_logged_at < FOCUS_FAILED_LOG_SECONDS:
                return placed
            self.log(decision="WINDOW_MINIMIZED", windows=placed, ms=round((now - began) * 1000, 1),
                     unchanged=self.unchanged_count)
            self.unchanged_logged_at, self.unchanged_count = now, 0
            return placed
        self.log(decision="WINDOW_MINIMIZED", windows=placed,
                 ms=round((now - began) * 1000, 1), **({"errors": errors} if errors else {}))
        stuck = [item for item in placed if item.get("state") != "minimized"]
        if stuck:
            self.log(decision="WINDOW_NOT_MINIMIZED", windows=stuck)
        return placed

    def keep_hidden(self, force=False):
        """After the client's `initialized` (force) and before a forwarded call: minimize the windows
        again, at most once a second. Between calls the focus
        keeper minimizes a window as soon as it shows (`hide_now`)."""
        if not self.offscreen:
            return
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

    def hide_now(self):
        """The focus keeper's minimize: on a thread of its own (the CDP round trip must not slow the next
        poll), one at a time, at most once per HIDE_RETRY_SECONDS."""
        now = self.clock()
        if (not self.offscreen or self.hiding or self.child is None or not self.initialized
                or (self.hide_asked_at is not None and now - self.hide_asked_at < HIDE_RETRY_SECONDS)):
            return
        self.hiding, self.hide_asked_at = True, now

        def run():
            try:
                self.place_window()
            finally:
                self.hiding = False
        threading.Thread(target=run, daemon=True).start()

    def make_focus_keeper(self, headed=False):
        """The focus keeper (built once; macOS, a headed session or one about to be), or None. Not started."""
        if self.focus_keeper is None:
            import browser_runtime
            # Never in the test suite (BACKGROUND_ALLOWED False there): a test's guard never reads or
            # changes the real desktop's front app.
            if not ((self.offscreen or headed) and sys.platform == "darwin" and browser_runtime.BACKGROUND_ALLOWED):
                return None
            mac = MacFrontmost()
            self.focus_keeper = FocusKeeper(self.chrome_pid, mac.front, mac.activate,
                                            suspended=self.front_released,
                                            log=self.log, clock=self.clock,
                                            chrome_shown=mac.shown, hide=self.hide_now)
        return self.focus_keeper or None

    def front_released(self):
        """support_email: the window may now come forward (SignInWait.front_released). Never raises."""
        released = getattr(getattr(self, "human_check_wait", None), "front_released", None)   # set after Chrome starts
        try:
            return bool(callable(released) and released())
        except Exception:
            return False

    def start_focus_keeper(self, headed=False):
        """Start the focus keeper's poll (once per guard; later calls return the same thread)."""
        if self.keeper_thread is not None:
            return self.keeper_thread
        if not self.make_focus_keeper(headed):
            return None

        def run():
            while not self.stopped.wait(FOCUS_POLL_SECONDS):
                self.focus_keeper.poll()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.keeper_thread = thread
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
        Not judged, as live: drags, key presses with no element (browser_press_key), coordinates,
        selectors and other locator chains."""
        if name == "browser_click":
            return [args.get("target")]
        if name == "browser_type" and args.get("submit") is True:
            return [args.get("target")]
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
        """The hold: one call answered later, by the hold object (`human_check_wait.call()`), on a thread
        of its own so the relay keeps running meanwhile. Today only support_email's sign-in wait
        (SignInWait); no application session has one (E16.T1), and no window is shown or moved for it."""
        def run():
            try:
                text, error = self.human_check_wait.call(), False
            except Exception:
                text = getattr(self.human_check_wait, "FAILED_TEXT", None) or "WAIT_EXPIRED: the wait could not start."
                error = True
            self.log(decision="HUMAN_CHECK_RETURNED", tool=HUMAN_CHECK_TOOL, text=text[:200])
            self.answer(message, text, error)
        self.log(decision="HUMAN_CHECK_STARTED", tool=HUMAN_CHECK_TOOL)
        threading.Thread(target=run, daemon=True).start()

    def record_front(self, headed=False):
        """Before the guard itself starts a headed Chrome: the focus keeper records the user's app."""
        keeper = self.make_focus_keeper(headed)
        if keeper:
            keeper.record_front()

    def stop_session(self, reason):
        """The guard ends its own session (owner, 2026-11-12, V6): nothing more is forwarded to the browser,
        the reason is recorded (SESSION_STOPPED in the audit, and GUARD_STOPPED beside it for the session's
        launcher, which ends the session as a failed one), and `run` then stops the browser server and
        Chrome. Never raises."""
        if self.stop_reason:
            return
        self.stop_reason = reason
        self.log(decision="SESSION_STOPPED", reason=reason)
        try:
            if self.stop_file is not None:
                self.stop_file.write_text(json.dumps({"reason": reason, "at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        self.closing = True
        self.requests.put(None)

    # --- main loop ----------------------------------------------------------------------------
    def handle(self, message):
        method = message.get("method")
        if method == YIELD_METHOD:
            if message.get("id") is None and not message.get("params"):   # the guard's own (`watch_yield`); a client's is dropped
                self.yield_turn()
            return
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
                self.initialized = True
                self.keep_hidden(force=True)   # FG2 = a: minimized right after launch, before any page work
                if not self.apply_client_hints():   # H3: a headless session's UA client hints, before any page work
                    return                      # V6: the session is stopped (stop_session)
                self.restore_site_session()     # E15.T4: the site's saved cookies, before any page work
                self.open_start_page()          # E16.T2: an attended submit's page, in front of the user at once
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        if isinstance(name, str) and name == REMOVED_TOOL:
            # Q1: by name, before everything else (X7: before the shape of its arguments is looked at, so
            # the call is this refusal whatever it carries). The name is recorded, never the code.
            self.log(decision="REFUSED", code=REMOVED_CODE, tool=name)
            self.answer(message, f"REFUSED {REMOVED_CODE}: {REMOVED_TEXT}", True)
            return
        # Missing or null arguments are a call without arguments, as ever; anything else must be an object
        # (X3: `[]`, "", 0 and false used to slip past an `or {}` here).
        if not isinstance(name, str) or not isinstance(params.get("arguments", None), (dict, type(None))):
            # W2: a tool call whose name is not a string, or whose arguments are not an object, is not a
            # call any tool can take: refused here, and the guard goes on (a list name used to end the loop).
            self.log(decision="REFUSED", code=MALFORMED_CODE, tool=repr(name)[:80])
            self.answer(message, f"REFUSED {MALFORMED_CODE}: a tool call needs a tool name (a string) and "
                        "arguments (an object).", True)
            return
        import browser_runtime
        if self.code_free() and name in WEBMAIL_HIDDEN:
            self.log(decision="NOT_OFFERED", tool=name)
            self.answer(message, f"Tool {name} is not available in this session.", True)
            return
        args = params["arguments"] if params.get("arguments") is not None else {}
        if name not in self.offered():
            # Q5: an allow-list. Only the tools this guard knows (TOOLS, @playwright/mcp 0.0.83) and its own
            # tools for this session are forwarded; a tool a later server adds is refused until it is listed.
            self.log(decision="NOT_OFFERED", tool=name if isinstance(name, str) else repr(name))
            self.answer(message, f"Tool {name} is not available in this session.", True)
            return
        # E16.T2: an attended submit's hold is told of every other call (after a hold, the first call that
        # is not a read of the page means the AI has resumed: the attached view then passes no more input).
        noted = getattr(self.human_check_wait, "note_call", None)
        if callable(noted) and name != HUMAN_CHECK_TOOL:
            noted(name)
        if name == ARM_TOOL and self.arm_binding is not None:
            self.call_arm(message, args)
            return
        # E16.T1: no application session holds the wait, so a call to it by name was refused just above
        # (NOT_OFFERED, never forwarded). Only a session that holds one (support_email's sign-in wait) gets here.
        if name == HUMAN_CHECK_TOOL and self.human_check_wait is not None:
            self.call_human_check(message)
            return
        if name == CONTINUE_TOOL and self.piece_chars:
            self.call_continue(message)
            return
        if name == SAVED_SIGN_IN_TOOL and self.saved_sign_in:
            self.call_saved_sign_in(message, args)
            return
        if name == CREATE_TOOL and self.create_account:
            self.call_create_password(message, args)
            return
        if name == RESET_TOOL and getattr(self, "reset_recorded", None):
            self.call_reset_password(message, args)
            return
        if name == LINK_TOOL and self.access_step:
            self.call_confirmation_link(message, args)
            return
        if name == SSO_TOOL:
            # E15.T9: the guard presses the button the model named: the same call, forwarded as a click.
            if not isinstance(args.get("target"), str) or not args["target"]:
                self.log(decision="REFUSED", code=MALFORMED_CODE, tool=name)
                self.answer(message, f"REFUSED {MALFORMED_CODE}: {SSO_TOOL} needs the button's ref as target.", True)
                return
            self.log(decision="SINGLE_SIGN_ON_STARTED", tool=name, target=args["target"])
            name = "browser_click"
            args = {key: args[key] for key in ("target", "element") if isinstance(args.get(key), str)}
            message = dict(message, params=dict(params, name=name, arguments=args))
        self.label = None
        with self.idle:
            if not self.idle.wait_for(lambda: not self.in_flight, timeout=600):
                self.in_flight.clear()  # a call that never answered; judge against the page as it is now
        try:
            refusal = focus_refusal(name, args) or cookie_refusal(name, args)
            if refusal is not None:
                raise refusal
            self.judge(name, args)
        except Refusal as refusal:
            self.log(decision="REFUSED", code=refusal.code, tool=name if isinstance(name, str) else repr(name),
                     arguments=args if isinstance(args, dict) else repr(args), detail=refusal.detail)
            self.answer(message, f"REFUSED {refusal.code}: {refusal.detail}", True)
            return
        self.keep_hidden()
        self.capture_site_session()
        self.clean_output()   # E15.T7: before a call that could read a file back
        self.log(decision="FORWARDED", tool=name, arguments=args, label=self.label)
        if "id" in message:
            ident = message["id"]
            with self.idle:
                self.in_flight.add(ident)
            timer = threading.Timer(self.call_timeout, self.time_out, (message,))
            timer.daemon = True
            timer.start()
        if name in HEADER_TOOLS and "id" in message:
            self.header_answers[self.hashable(message["id"])] = isinstance(args, dict) and args.get("part") in BODY_PARTS
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

    def watch_yield(self):
        """Until the session ends: when the yield request appears in this session's folder, one message to
        the guard's own loop (`yield_turn` runs there, in order with the engine's calls). Never raises."""
        asked = self.stop_file.parent / YIELD_FILE if self.stop_file is not None else None
        if asked is None or self.operation not in YIELD_OPERATIONS:
            return
        while not self.stopped.wait(YIELD_POLL_SECONDS):
            try:
                if asked.exists():
                    self.requests.put({"method": YIELD_METHOD})
                    return
            except OSError:
                return

    def yield_turn(self):
        """The yield request, handled in the message loop. The session stops (YIELD_REASON) unless it may not
        be cut: the arm was called (`ArmBinding.attempted`: an armed submit runs to its end), it is an access
        step or holds a saved sign-in or sign-up (a password may be pending), or it is an attended submit."""
        why = ("ARMED" if self.arm_binding is not None and getattr(self.arm_binding, "attempted", False)
               else "ACCESS_STEP" if self.access_step or self.saved_sign_in or self.create_account
               else "ATTENDED" if self.human_check_wait is not None
               else "OPERATION" if self.operation not in YIELD_OPERATIONS else None)
        if why:
            self.log(decision="YIELD_REFUSED", reason=why)
            try:
                (self.stop_file.parent / YIELD_REFUSED_FILE).write_text(json.dumps({"reason": why}), encoding="utf-8")
            except (OSError, AttributeError):
                pass
            return
        self.log(decision="YIELDED")
        self.stop_session(YIELD_REASON)

    def run(self):
        threading.Thread(target=self.pump_child, daemon=True).start()
        threading.Thread(target=self.pump_client, daemon=True).start()
        threading.Thread(target=self.watch_yield, daemon=True).start()
        import browser_runtime
        self.start_focus_keeper(headed=self.operation in browser_runtime.SHOWN_OPERATIONS and not self.headless)
        try:
            self.loop()
            if not self.stop_reason:
                self.capture_site_session(final=True)   # E15.T4: the last state, while the browser still answers
            self.close_own_chrome()                 # asked to close itself (it writes its cookies), before it is stopped
        finally:
            self.stopped.set()
            try:
                self.stop_child()
            finally:
                self.clean_output(final=True)   # E15.T7: once the browser server can write no more
                self.stop_background_chrome()
        return 1 if self.stop_reason else 0

    def close_own_chrome(self, wait_seconds=None):
        """Ask the Chrome this guard started to close itself (CDP Browser.close on its own browser
        socket, else through the browser server) and wait for it to let go of the profile, so it writes
        its cookies and exits cleanly, as a Chrome a person closes does. `end_session` follows: on
        Windows it ends the Job Object whatever happened here; on macOS it finds no Chrome left, or
        stops one that did not close in time (SIGTERM, then SIGKILL). Never raises; never for the
        user's draft window (SHOWN_OPERATIONS).
        macOS since 2026-11-13: a sign-in made seconds before the session ended was not in the profile
        afterwards (Chrome writes its cookies on a timer and on a clean exit; a Chrome that is signalled
        and then killed may write neither). There the guard does this only when its starter says its own
        exit may wait for Chrome (browser_runtime.CLEAN_CLOSE_VARIABLE: the live view and the LinkedIn
        reader). A session an engine started is closed by its starter instead, after the engine has
        exited and outside the session's time limit (browser_runtime.close_cleanly), because an engine
        may wait for this process: a wait here could turn a finished session into a timeout."""
        import browser_runtime
        import portable
        if not self.background_chrome or Proxy.users_window(self):
            return
        windows = browser_runtime.windows_host()
        waits = windows or os.environ.get(browser_runtime.CLEAN_CLOSE_VARIABLE) == "1"
        wait_seconds = (5.0 if windows else browser_runtime.CLOSE_WAIT_SECONDS) if wait_seconds is None else wait_seconds
        try:
            if self.tab_details is not None:
                self.tab_details.close()   # its held tabs are let go; nothing more is set on a closing Chrome
                self.tab_details = None
            # First on Chrome's own browser socket (it needs no browser server); else through the server.
            asked = bool(self.endpoint) and browser_runtime.close_browser(self.endpoint)
            if not asked and self.child is not None and self.child.poll() is None:
                answer = placed_windows(self.child_call("tools/call", {"name": PLACE_TOOL, "arguments": {"code": CLOSE_CODE}}))
                asked = bool(answer) and "error" not in answer[0]
            self.log(decision="CHROME_CLOSE_ASKED", asked=asked)
            self.close_asked = asked
            if not waits:
                # macOS, a session an engine started: asked, and that is all. The engine may be waiting for
                # this process inside the session's time limit, so nothing waits here; whoever stops Chrome
                # (`stop_background_chrome`) does the waiting.
                return
            if asked and windows:
                portable.wait_profile_free(self.background_chrome, wait_seconds)
            elif asked:
                browser_runtime.wait_chrome_gone(self.background_chrome, wait_seconds)
        except Exception as error:
            self.log(decision="CHROME_CLOSE_FAILED", reason=type(error).__name__)

    def users_window(self):
        """True when this session's Chrome is the user's draft window (SHOWN_OPERATIONS), which the guard
        leaves running. Windows: not while it is the hidden first phase's Chrome, which only this guard's
        Job Object can end (macOS: support_email ends that one itself, as before)."""
        import browser_runtime
        return self.operation in browser_runtime.SHOWN_OPERATIONS and not (
            self.headless and browser_runtime.windows_host())

    def stop_background_chrome(self):
        """The Chrome the guard started is not @playwright/mcp's to close (it only attached): stop it on
        this profile only (browser_runtime.end_session, which checks the command line's profile)."""
        import browser_runtime
        if Proxy.users_window(self):
            return   # the user's draft window (owner, 2026-10-24): support_email closes it after the user does
        if not self.background_chrome:
            return
        if not browser_runtime.windows_host() and os.environ.get(browser_runtime.CLEAN_CLOSE_VARIABLE) != "1":
            # macOS, a session an engine started (2026-11-14). Chrome must get the request to close before any
            # signal, and nothing may wait while the engine may be waiting for this process. So:
            # - the starter is alive: Chrome is left to it. In its `finally`, after the session's outcome is
            #   decided, it asks again, waits, and stops what is left (browser_runtime.close_cleanly, end_session).
            # - the starter has died (the app was killed): nobody else will, and nothing judges this session
            #   any more, so this process waits for Chrome to close itself and then stops it.
            # - the starter is not known (the variable did not arrive): never a wait, because an engine may
            #   be waiting for this process inside the session's time limit; Chrome is stopped at once, as
            #   before this change.
            import portable
            starter = self.starter_pid
            known = type(starter) is int and starter > 0
            if known and portable.pid_alive(starter):
                self.log(decision="CHROME_LEFT_TO_STARTER")
                return
            if known and getattr(self, "close_asked", False):
                browser_runtime.wait_chrome_gone(self.background_chrome, browser_runtime.CLOSE_WAIT_SECONDS)
        browser_runtime.end_session({"browser_driver": {"profile": self.background_chrome}})

    def loop(self):
        while True:
            message = self.requests.get()
            if message is None or self.closing:
                return
            self.handle(message)

    def stop_child(self, grace=10.0, child=None):
        """Fix 7: close the browser server's input (it then closes Chrome), wait, terminate, wait, kill."""
        child = child or self.child
        try:
            child.stdin.close()
        except OSError:
            pass
        for step in (None, child.terminate, child.kill):
            if step is not None:
                try:
                    step()
                except OSError:
                    pass
            try:
                child.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    # --- the site's saved session (E15.T4, site_sessions.py) ----------------------------------------
    def site_call(self, code):
        """The guard's own browser_run_code_unsafe call (the channel the window placement uses): never logged,
        and its answer never relayed to the client."""
        return placed_windows(self.child_call("tools/call", {"name": PLACE_TOOL, "arguments": {"code": code}}))

    def open_start_page(self):
        """An attended submit only (`start_url`): the application's own address is opened by the guard itself,
        after the browser's details and the site's saved session are in place and before the engine's first
        step, on a thread of its own so the engine is not kept waiting. The page's answer is dropped (the
        engine reads the page itself). Recorded; never raises, never stops the session."""
        if not self.start_url:
            return

        def run():
            try:
                answer = self.child_call("tools/call", {"name": "browser_navigate", "arguments": {"url": self.start_url}})
                self.log(decision="START_PAGE_OPENED" if isinstance(answer, dict) and "result" in answer
                         else "START_PAGE_NOT_OPENED")
            except Exception as error:
                self.log(decision="START_PAGE_NOT_OPENED", reason=type(error).__name__)
        threading.Thread(target=run, daemon=True).start()

    def restore_site_session(self):
        """After the client's `initialized`, before any page work: the cookies saved for this session's
        site that Chrome no longer holds are put back (Chrome's own values win; no navigation). Recorded
        with counts only; never raises, never stops the session."""
        if not self.site_session:
            return
        try:
            import site_sessions
            done = site_sessions.restore(self.site_session, self.site_call)
            self.site_captured_at = self.clock()
            outcome = done.pop("outcome")
            if outcome == "restored":
                self.log(decision="SITE_SESSION_RESTORED", site=self.site_session, **done)
            elif outcome != "none":   # the browser did not answer, an error, or the key cannot be read
                self.log(decision="SITE_SESSION_RESTORE_FAILED", site=self.site_session, outcome=outcome, **done)
        except Exception as error:
            self.log(decision="SITE_SESSION_FAILED", step="restore", reason=type(error).__name__)

    def capture_site_session(self, final=False):
        """Before a forwarded call (at most every site_sessions.CAPTURE_SECONDS) and once when the
        session ends: the site's cookies as Chrome holds them now are kept as this session's capture
        (encrypted; the application job keeps or drops it when the result is known, site_sessions.settle).
        Recorded with a count of sites only; never raises, never stops the session."""
        if not self.site_session or self.site_captured_at is None:
            return
        try:
            import site_sessions
            if final and (self.child is None or self.child.poll() is not None):
                return
            if not final and self.clock() - self.site_captured_at < site_sessions.CAPTURE_SECONDS:
                return
            self.site_captured_at = self.clock()
            sites = site_sessions.save(self.site_session, self.site_call, pending=True)
            if sites:   # a capture that found nothing to keep is not recorded
                self.log(decision="SITE_SESSION_CAPTURED", site=self.site_session, sites=sites, final=final)
        except Exception as error:
            self.log(decision="SITE_SESSION_FAILED", step="capture", reason=type(error).__name__)

    # --- the site's saved sign-in (E15.T7, saved_sign_ins.py) ---------------------------------------
    def call_saved_sign_in(self, message, args):
        """Type this session's site's saved sign-in into the fields the model named. The value goes from
        the sealed store to the guard's own browser call and nowhere else: the answer is one of
        SAVED_SIGN_IN_TEXT, the audit row has the site, the outcome and booleans, and the browser server's
        answer to the call is dropped. Never raises."""
        site, outcome, done, began = self.saved_sign_in, "failed", {}, None
        try:
            refs = [args.get("email_target"), args.get("password_target")]
            if not any(refs) or any(ref is not None and not (isinstance(ref, str) and REF.match(ref)) for ref in refs):
                outcome = "malformed"
            else:
                with self.idle:   # the page as it is once every forwarded call has been answered
                    self.idle.wait_for(lambda: not self.in_flight, timeout=self.call_timeout)
                import saved_sign_ins
                found = saved_sign_ins.for_fill(site)
                if found is None:
                    outcome = "none"
                else:
                    # Before the value leaves this process: from here on it is removed from everything
                    # relayed or recorded, whether or not the fill succeeds.
                    self.typed = sorted(set(self.typed) | set(secret_forms(found[1])), key=len, reverse=True)
                    began = time.time()
                    outcome = "unconfirmed"   # until the browser says otherwise: the value has been sent
                    if self.filled_at is None:
                        self.filled_at = began
                    answer = self.site_call(fill_code(site, refs[0], refs[1], found[0], found[1]))
                    done = answer[0] if answer and isinstance(answer[0], dict) else {}
                    outcome = done.get("outcome") if done.get("outcome") in SAVED_SIGN_IN_TEXT else "unconfirmed"
                    if outcome not in ("filled", "failed", "unconfirmed") and self.filled_at == began:
                        self.filled_at = None   # refused before anything was typed
                    if outcome == "filled":
                        saved_sign_ins.used(site)
        except Exception as error:
            outcome = "unconfirmed" if outcome == "unconfirmed" else "failed"
            self.log(decision="SAVED_SIGN_IN_FAILED", site=site, reason=type(error).__name__)
        self.log(decision="SAVED_SIGN_IN", tool=SAVED_SIGN_IN_TOOL, site=site, outcome=outcome,
                 email=done.get("email") is True, password=done.get("password") is True, began=began)
        self.answer(message, SAVED_SIGN_IN_TEXT[outcome], outcome != "filled")

    # --- creating an account (E15.T8) ----------------------------------------------------------------
    def call_create_password(self, message, args):
        """Make the new account's password and type it into the password fields the model named. Typed once
        per session, and never for a site that already has a saved or pending sign-in: with no second
        typing there is no second sign-up. The password is kept as pending BEFORE it is typed (owner's
        pick R7 b); when nothing was typed the pending one is forgotten again. Fixed answers; the audit
        row has the site, the outcome and a count. Never raises."""
        site, outcome, typed, began, kept = self.saved_sign_in, "failed", 0, None, False
        try:
            import saved_sign_ins
            refs = args.get("password_targets")
            if not (isinstance(refs, list) and 1 <= len(refs) <= 3 and all(isinstance(ref, str) and REF.match(ref) for ref in refs)):
                outcome = "malformed"
            elif self.password_typed:
                outcome = "already"
            else:
                with self.idle:
                    self.idle.wait_for(lambda: not self.in_flight, timeout=self.call_timeout)
                password = generate_password(args.get("max_length"), args.get("symbols"))
                self.typed = sorted(set(self.typed) | set(secret_forms(password)), key=len, reverse=True)
                kept = saved_sign_ins.pend(site, self.create_account, password)
                if kept != saved_sign_ins.SAVED:
                    outcome, kept = ("exists" if kept == "exists" else "unavailable"), False
                else:
                    began = time.time()
                    outcome = "unconfirmed"
                    if self.filled_at is None:
                        self.filled_at = began
                    answer = self.site_call(password_code(site, refs, password))
                    done = answer[0] if answer and isinstance(answer[0], dict) else {}
                    outcome = done.get("outcome") if done.get("outcome") in CREATE_TEXT else "unconfirmed"
                    typed = done.get("typed") if isinstance(done.get("typed"), int) else 0
                    if outcome in ("filled", "unconfirmed"):
                        self.password_typed = True
                    else:
                        saved_sign_ins.drop_pending(site)   # nothing was typed: nothing to keep
                        if self.filled_at == began:
                            self.filled_at = None
                password = None
        except Exception as error:
            outcome = "unconfirmed" if outcome == "unconfirmed" else "failed"
            self.log(decision="ACCOUNT_PASSWORD_FAILED", site=site, reason=type(error).__name__)
        self.log(decision="ACCOUNT_PASSWORD", tool=CREATE_TOOL, site=site, outcome=outcome, fields=typed, began=began)
        self.answer(message, CREATE_TEXT[outcome], outcome != "filled")

    def call_reset_password(self, message, args):
        """Type a new password on the site's own reset form (owner's rule, 2026-11-14). Held, and permitted,
        only when the application job started this session with its stored record of an EARLIER finished
        step that submitted a sign-up on this site (`--reset-recorded`, `access_step.own_account`): never
        in the session that typed the sign-up password, never on a pending password alone, never for a
        site that has a saved sign-in, whatever the model asks. Kept pending (marked as a reset) before
        it is typed; when nothing was typed the pending row is put back as it was (or removed when there
        was none). Once per session, counted from before the browser is asked. Fixed answers; the audit
        row has the site, the outcome and a count."""
        site, outcome, typed, began = self.saved_sign_in, "failed", 0, None
        try:
            import saved_sign_ins
            refs = args.get("password_targets")
            if not (isinstance(refs, list) and 1 <= len(refs) <= 3 and all(isinstance(ref, str) and REF.match(ref) for ref in refs)):
                outcome = "malformed"
            elif getattr(self, "reset_typed", False):
                outcome = "already"
            elif not getattr(self, "reset_recorded", None) or self.password_typed or saved_sign_ins.has(site):
                outcome = "none"   # no stored record of an earlier sign-up, this session's own sign-up, or a confirmed sign-in
            else:
                with self.idle:
                    self.idle.wait_for(lambda: not self.in_flight, timeout=self.call_timeout)
                earlier = saved_sign_ins.pending_row(site)
                password = generate_password(args.get("max_length"), args.get("symbols"))
                self.typed = sorted(set(self.typed) | set(secret_forms(password))
                                    | set(secret_forms(earlier["password"]) if earlier and isinstance(earlier.get("password"), str) else ()),
                                    key=len, reverse=True)
                if saved_sign_ins.reset_pending(site, password, recorded_email=self.reset_recorded) != saved_sign_ins.SAVED:
                    outcome = "none"
                else:
                    self.reset_typed = True   # from before the browser is asked: an error below never allows a second
                    began = time.time()
                    outcome = "unconfirmed"
                    if self.filled_at is None:
                        self.filled_at = began
                    answer = self.site_call(password_code(site, refs, password))
                    done = answer[0] if answer and isinstance(answer[0], dict) else {}
                    outcome = done.get("outcome") if done.get("outcome") in RESET_TEXT else "unconfirmed"
                    typed = done.get("typed") if isinstance(done.get("typed"), int) else 0
                    if outcome not in ("filled", "unconfirmed"):   # the browser said nothing was typed
                        self.reset_typed = False
                        if earlier:
                            saved_sign_ins.reset_pending(site, password, restore=earlier)
                        else:
                            saved_sign_ins.drop_pending(site)
                password = earlier = None
        except Exception as error:
            outcome = "unconfirmed" if outcome == "unconfirmed" else "failed"
            self.log(decision="RESET_PASSWORD_FAILED", site=site, reason=type(error).__name__)
        self.log(decision="RESET_PASSWORD", tool=RESET_TOOL, site=site, outcome=outcome, fields=typed, began=began)
        self.answer(message, RESET_TEXT[outcome], outcome != "filled")

    def call_confirmation_link(self, message, args):
        """Open a link the model took from an email, only when the link's own host is this session's site
        (site_sessions.site_key on the address itself: a structured comparison, no page or mail text)."""
        import site_sessions
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            self.answer(message, LINK_TEXT["malformed"], True)
            return
        url = url.strip()
        # A backslash or a control character is read one way here and another way by the browser
        # (Chrome takes "\\" as "/": https://other\\@site/ loads other): never opened.
        odd = "\\" in url or any(ord(character) < 32 or ord(character) == 127 for character in url)
        target = None if odd else site_sessions.site_key(url)
        if target != self.saved_sign_in:
            self.log(decision="CONFIRMATION_LINK", outcome="elsewhere", site=self.saved_sign_in, link_site=target)
            self.answer(message, LINK_TEXT["elsewhere"], True)
            return
        with self.idle:
            self.idle.wait_for(lambda: not self.in_flight, timeout=self.call_timeout)
        self.log(decision="CONFIRMATION_LINK", outcome="opened", site=self.saved_sign_in)
        answer = self.child_call("tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
        result = answer.get("result") if isinstance(answer, dict) else None
        # Where the browser really is now, asked of the browser itself: a link that led off the site is
        # the same outcome as one that was never on it, and its page is not handed to the model.
        landed = (self.site_call(ADDRESS_CODE) or [{}])[0].get("url")
        landed = site_sessions.site_key(landed) if isinstance(landed, str) else None
        if landed != self.saved_sign_in:
            self.log(decision="CONFIRMATION_LINK", outcome="landed_elsewhere", site=self.saved_sign_in, link_site=landed)
            self.child_call("tools/call", {"name": "browser_navigate", "arguments": {"url": "about:blank"}})
            self.answer(message, LINK_TEXT["elsewhere"], True)
            return
        if not isinstance(result, dict):
            self.answer(message, LINK_TEXT["failed"], True)
        elif "id" in message:
            self.to_client({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def clean_output(self, final=False):
        """Once a saved sign-in was typed: the value is removed from every text file in the browser server's
        output folder (before each forwarded call and before each answer is relayed, so nothing read back
        from there carries it), and when the session ends (`final`) the files that are not text and were
        written since the first fill (screenshots) are deleted. Counts only in the audit; never raises."""
        if not self.typed or not self.output_folder:
            return
        try:
            scrubbed, removed = clean_folder(self.output_folder, self.typed,
                                             self.filled_at if final else None)
            if scrubbed or removed:
                self.log(decision="SAVED_SIGN_IN_OUTPUT_CLEANED", files=scrubbed, removed=removed, final=final)
        except Exception as error:
            self.log(decision="SAVED_SIGN_IN_FAILED", step="output", reason=type(error).__name__)

    def apply_client_hints(self):
        """H3: a headless session presents the installed Chrome's headed user agent AND its client hints
        (sec-ch-ua, navigator.userAgentData), through CDP's Emulation.setUserAgentOverride with
        userAgentMetadata on every page, and on each new page as it opens (client_hints_code). Recorded
        CLIENT_HINTS_SET / CLIENT_HINTS_RETRY / CLIENT_HINTS_FAILED; never raises. True when set (or
        nothing to set); False when it failed twice and the session was stopped (`stop_session`)."""
        if not (self.headless and (self.client_hints or self.screen)):
            return True
        # V14 (owner, 2026-11-12): this is the browser server's first call, and a slow first attach can
        # miss its limit; it is tried once more before the session is stopped. Nothing is forwarded to the
        # browser between the two (this runs inside the handling of `initialized`).
        for last in (False, True):
            try:
                answer = self.child_call("tools/call", {"name": PLACE_TOOL, "arguments": {
                    "code": client_hints_code(self.headless_user_agent, self.client_hints, self.screen)}})
            except (OSError, ValueError) as error:
                answer = {"error": str(error)[:200]}
            pages = placed_windows(answer)
            if pages is not None and not any("error" in item for item in pages):
                break
            if not last:
                self.log(decision="CLIENT_HINTS_RETRY", pages=pages)
                continue
            # V6 (owner, 2026-11-12): a page would be told "HeadlessChrome" and an 800x600 screen, which
            # the same Chrome in a window never says, so the session stops here, on macOS and Windows.
            self.log(decision="CLIENT_HINTS_FAILED", pages=pages)
            self.stop_session("CLIENT_HINTS_FAILED")
            return False
        self.log(decision="CLIENT_HINTS_SET", pages=len(pages))
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sidedoor browser-guard", description=__doc__.split("\n")[0])
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    parser.add_argument("--audit", help="append every decision to this JSONL file")
    parser.add_argument("--claim", help="submit only: the private arm claim (application_browser.consume)")
    parser.add_argument("--human-check", help="the hold's context: support_email's sign-in wait (support_email."
                        "SIGNIN_CONTEXT), or an attended submit's hold (form_check_hold.CONTEXT, with --claim)")
    parser.add_argument("--uploads", help="the session's read-only upload folder, announced as the browser's root")
    parser.add_argument("--background-chrome",
                        help="start Chrome on this profile without activating it (Windows: only without a window)")
    parser.add_argument("--headless-user-agent",
                        help="with --background-chrome: start that Chrome headless, presenting this user agent")
    parser.add_argument("--headless-client-hints", help="with --headless-user-agent: userAgentMetadata (JSON)")
    parser.add_argument("--headless-screen", help="with --headless-user-agent: the display to report, WxH@scale")
    parser.add_argument("--offscreen", action="store_true",
                        help="keep the session's Chrome windows minimized, and the front with the user's app")
    parser.add_argument("--site-session", help="the site (host) whose saved session is restored and captured")
    parser.add_argument("--saved-sign-in", help="the site (host) whose saved sign-in the guard may type (E15.T7)")
    parser.add_argument("--reset-recorded", help="with --access-step: the email of Sidedoor's own recorded sign-up on the site")
    parser.add_argument("--no-page-code", action="store_true", help="the in-page code tool is neither listed nor run (Sidedoor's own steps)")
    parser.add_argument("--access-step", action="store_true", help="with --saved-sign-in: the session is an access step (E15.T8)")
    parser.add_argument("--create-account", help="with --saved-sign-in: the email a new account there is made under (E15.T8)")
    parser.add_argument("--single-sign-on", action="store_true",
                        help="with --access-step: offer the single_sign_on tool (E15.T9)")
    parser.add_argument("--start-url", help="attended submit only (with --human-check and --claim): the "
                        "application's own address, opened by the guard before the engine's first step")
    parser.add_argument("--piece-chars", type=int,
                        help="Claude only: cut a longer text answer into pieces of this size (browser_continue)")
    parser.add_argument("child", nargs=argparse.REMAINDER, help="-- then the @playwright/mcp command")
    options = parser.parse_args(argv)
    child = options.child[1:] if options.child[:1] == ["--"] else options.child
    if not child:
        parser.error("give the @playwright/mcp command after --")
    if options.claim and options.operation != "submit":
        parser.error("--claim is for the submit operation only")
    return Proxy(options.operation, child, options.audit, options.claim, options.human_check,
                 uploads=options.uploads, background_chrome=options.background_chrome,
                 offscreen=options.offscreen, headless_user_agent=options.headless_user_agent,
                 client_hints=json.loads(options.headless_client_hints) if options.headless_client_hints else None,
                 screen=parse_screen(options.headless_screen), piece_chars=options.piece_chars,
                 site_session=options.site_session, saved_sign_in=options.saved_sign_in,
                 create_account=options.create_account, access_step=options.access_step,
                 reset_recorded=options.reset_recorded, no_page_code=options.no_page_code,
                 single_sign_on=options.single_sign_on, start_url=options.start_url).run()


if __name__ == "__main__":
    sys.exit(main())
