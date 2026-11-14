"""E18 (one LinkedIn sign-in), the computer side: connect outreach with the LinkedIn session of the
browser Sidedoor controls, and keep it connected.

Owner's picks (2026-11-12):
- A1. `send` reads the session (`linkedin_session.current`) and posts it to the relay over HTTPS
  (`POST /link/session`), which passes it to Unipile. Here it lives in `send`'s locals only. It is
  never written to setup-state.json, a log, the database, a step message or an exception's text:
  `send` returns one of the outcome words below and nothing else, and reads only `connected`, `error`
  and nothing more from the relay's answer.
- A2. The country (two letters) is the only location sent: `country`.
- A3. The current session is sent again when it changed since this process last sent it (`keep`; what
  was sent is remembered as a digest in this process's memory, never on disk, so a restarted Sidedoor
  sends once more) or when the account reads as disconnected (`disconnected`, reached from
  `setup_steps.linkedin_status`).
- A4. An install connected the old way (no `linkedin_connected_by` in setup-state.json) is never sent
  for while its account reads connected. It moves to this way at its next reconnect, on the same
  account (the relay reconnects the license's own account).
- A5. While the account reads disconnected the home carries a hold (`linkedin_hold`); every outreach
  step asks `orchestrator.unipile_ready`, which then raises `unipile_port.LinkedInDisconnected`, a
  SetupHold: a wait that spends no try and drops nothing.
- The user is asked (the LinkedIn step turns "needs you") only when a re-send fails because the browser
  is not signed in or the sign-in was refused. A browser that is busy right now (`linkedin_session.Busy`),
  or a relay that cannot be reached, is tried again at the next check without asking.
- Review round 1 (B2). A browser that cannot be read at all (no reader, or the reader failed) is never
  called "reconnecting": the step says so and what to do, and from the UNREADABLE_TRIES-th failed try in
  a row it asks the user ("needs you"). The count is kept in setup-state.json (`linkedin_unreadable_tries`)
  and ends when LinkedIn reads connected. 3 is an assumption for the owner, not a pick.
- A9 (owner). The hold is also asked at the write itself (`unipile_port.UnipilePort._write`), so a pass
  already past the readiness check makes no request once LinkedIn reads as disconnected.
- A12 (owner). A browser that is busy or a relay that cannot be reached is retried every hour; once
  that has lasted UNREACHED_SECONDS (24 hours, the owner's number) the step asks the user and one
  notification is queued (`pending_notice`), which the hourly round hands to the product's
  notification outbox (`orchestrator.deliver_linkedin_notice`).
- Review B3/B4. The relay's answers that waiting cannot clear (the LinkedIn account is linked to another
  license, the browser is signed in to another account, the linked account no longer exists) each ask
  the user with their own sentence.
- Review B5/B6. A page only reads: `disconnected` sends only when the hourly check asks (`resend`).
  Nothing here says "connected" or releases the hold; only an account read whose status is OK does
  (`setup_steps.linkedin_status`).
"""

import datetime as dt
import hashlib
import threading

import linkedin_session
import setup_steps
import unipile_port


SESSION_PATH = "/link/session"
SEND_TIMEOUT = 120   # the relay waits up to 90 s for Unipile
MODE_KEY = "linkedin_connected_by"
SESSION_MODE = "session"
HOLD_KEY = "linkedin_hold"

CONNECTED = "CONNECTED"
NO_LICENSE = "NO_LICENSE"
NOT_SIGNED_IN = "NOT_SIGNED_IN"   # the browser was read: no LinkedIn sign-in in it
UNAVAILABLE = "UNAVAILABLE"       # the browser cannot be read at all (no reader, or the reader failed)
BUSY = "BUSY"                     # the browser is busy with a task right now
REFUSED = "REFUSED"               # the sign-in was not accepted
LICENSE = "LICENSE"               # the relay refused the license
UNREACHABLE = "UNREACHABLE"       # no answer from the relay, or the relay could not reach what it needs now
CERTIFICATE = "CERTIFICATE"       # no answer: this computer could not verify the relay's certificate
TEMPORARY = "TEMPORARY"           # any other answer: the same request would be answered the same way again
LATER = "LATER"                   # answered "not now": a rate limit, or the provider's own server error
ELSEWHERE = "ELSEWHERE"           # this LinkedIn account is linked to another license
MISMATCH = "MISMATCH"             # the browser is signed in to another LinkedIn account than the one linked
GONE = "GONE"                     # the license's linked account no longer exists at the provider
ASKS_USER = (NOT_SIGNED_IN, REFUSED)
USER_MUST_ACT = (ELSEWHERE, MISMATCH, GONE)   # waiting never clears these (review B3, B4)
WAITS = (BUSY, UNREACHABLE, LATER, CERTIFICATE, TEMPORARY)   # retried hourly; asked after UNREACHED_SECONDS (A12)
# Review round 2: of those, the ones the next hourly try can get past with nothing done by the user. Only
# these may be shown as "reconnecting"; the others show the sentence the button shows.
RETRY_CAN_WORK = (BUSY, UNREACHABLE, LATER)
# Review round 3: answers that pass by themselves. The relay's code for the provider's rate limit or
# server error, a rate limit of the relay's own, and the HTTP statuses that mean "not now" whoever sent them.
LATER_CODES = ("SESSION_UPSTREAM_FAILED", "TOO_MANY_REQUESTS", "SESSION_IN_PROGRESS")
LATER_STATUSES = (408, 425, 429)
# The relay's own answers that mean "could not be reached now" (its license check or Unipile did not answer).
UNREACHED_CODES = ("UPSTREAM_UNAVAILABLE", "UPSTREAM_TIMEOUT", "LICENSE_CHECK_UNAVAILABLE")
OUTCOMES = (CONNECTED, NO_LICENSE, NOT_SIGNED_IN, UNAVAILABLE, BUSY, REFUSED, LICENSE, UNREACHABLE, CERTIFICATE,
            TEMPORARY, LATER, ELSEWHERE, MISMATCH, GONE)
RELAY_CODES = {"SESSION_REFUSED": REFUSED, "ACCOUNT_LINKED_ELSEWHERE": ELSEWHERE,
               "SESSION_ACCOUNT_MISMATCH": MISMATCH, "SESSION_ACCOUNT_GONE": GONE}

SIGN_IN_AGAIN = ("LinkedIn needs you to sign in again. Click Open Sidedoor's browser, sign in to LinkedIn, then "
                 "click I'm signed in.")
MESSAGES = {
    NOT_SIGNED_IN: ("needs_you", "Sidedoor's browser is not signed in to LinkedIn. Click Open Sidedoor's browser, "
                    "sign in to LinkedIn, then click I'm signed in."),
    REFUSED: ("needs_you", "LinkedIn did not accept this sign-in. Click Open Sidedoor's browser, sign in to "
              "LinkedIn again, then click I'm signed in."),
    BUSY: ("failed", "Sidedoor's browser is busy right now. Try again in a few minutes."),
    UNAVAILABLE: ("failed", "Sidedoor could not read its browser. Quit Sidedoor and open it again, then click I'm "
                  "signed in. If you still see this message, contact %s." % setup_steps.SUPPORT_EMAIL),
    UNREACHABLE: ("failed", "Could not reach Sidedoor's LinkedIn service. Check your internet and try again."),
    LATER: ("failed", "Sidedoor's LinkedIn service could not connect LinkedIn just now. Try again in a few minutes."),
    TEMPORARY: ("failed", "Sidedoor's LinkedIn service could not connect LinkedIn with this sign-in. Click Open "
                "Sidedoor's browser, sign in to LinkedIn again, then click I'm signed in. If you still see this "
                "message, contact %s." % setup_steps.SUPPORT_EMAIL),
    ELSEWHERE: ("needs_you", "This LinkedIn account is already connected to another Sidedoor license. Click Open "
                "Sidedoor's browser, sign in to a different LinkedIn account, then click I'm signed in. If you have "
                "only one license, contact %s." % setup_steps.SUPPORT_EMAIL),
    MISMATCH: ("needs_you", "Sidedoor's browser is signed in to a different LinkedIn account than the one connected. "
               "Click Open Sidedoor's browser, sign in to the LinkedIn account you connected, then click I'm signed "
               "in."),
    GONE: ("needs_you", "Sidedoor could not reconnect your LinkedIn account because its connection no longer exists. "
           "Contact %s to connect it again." % setup_steps.SUPPORT_EMAIL),
}
# Review B6: a page read found LinkedIn disconnected. It only says so; the button sends.
DISCONNECTED_READ = "LinkedIn is disconnected. Click I'm signed in to reconnect."
# Review B5: the session was accepted, but the account does not read connected (yet).
SENT_NOT_CONNECTED = ("LinkedIn does not show as connected yet. Click Check again in a minute. If it still does "
                      "not connect, click I'm signed in again.")
# A12 (owner): a day of hourly tries that could not reach the browser or the LinkedIn service.
UNREACHED_A_DAY = ("Sidedoor has not been able to reconnect LinkedIn for a day. Check your internet, then click Open "
                   "Sidedoor's browser, sign in to LinkedIn, and click I'm signed in.")
# B11 (owner): a day of hourly sends that were accepted while the account never read connected.
NOT_CONNECTED_A_DAY = ("Sidedoor has not been able to reconnect LinkedIn for a day. Click Open Sidedoor's browser, sign "
                       "in to LinkedIn again, then click I'm signed in. If you still see this message, contact %s."
                       % setup_steps.SUPPORT_EMAIL)
UNREACHED_SECONDS = 24 * 3600
UNREACHED_KEY = "linkedin_unreached_since"
NOTIFIED_KEY = "linkedin_unreached_notified"
NOTICE_KEY = "linkedin_notice"
RECONNECTING = "LinkedIn is reconnecting. Sidedoor tries again within the hour."
# Review round 1 (B2): disconnected, and the browser cannot be read at all. No promise of a reconnect.
UNREADABLE = ("LinkedIn is disconnected, and Sidedoor could not read its browser to reconnect it. Quit Sidedoor and "
              "open it again, then click I'm signed in. If you still see this message, contact %s."
              % setup_steps.SUPPORT_EMAIL)
UNREADABLE_TRIES = 3   # failed tries in a row before the user is asked (an assumption, for the owner to set)
UNREADABLE_KEY = "linkedin_unreadable_tries"
OPEN_MESSAGE = "Sidedoor's browser is open. Sign in to LinkedIn there, then click I'm signed in."
NO_CHROME = "Sidedoor needs Google Chrome. Install it, then click Open Sidedoor's browser again."
BROWSER_BUSY = "Sidedoor's browser is busy with a scheduled task. Try again in a few minutes."
STILL_CONNECTED = "LinkedIn is still connected."

_sent = {}   # home -> digest of the session this process last sent; memory only, never written
_sent_lock = threading.Lock()
# Review round 4: one send at a time in this process. [I'm signed in] and the hourly check each take
# this turn for their whole send and its recording; whoever finds it taken waits for the one in flight
# and then sends nothing itself.
_turn = threading.Lock()


def _take_turn():
    """True when the turn was free and is now held. False after waiting out the send in flight (the
    turn is not held then)."""
    if _turn.acquire(blocking=False):
        return True
    with _turn:
        return False


def _home_key(home):
    return str(setup_steps._home(home))


def _mark(session):
    return hashlib.sha256((session.cookie + "\n" + session.user_agent).encode("utf-8")).digest()


def country(home=None):
    """The two-letter code of the profile's country, when the saved country is one of Sidedoor's own
    listed countries (the same label, nothing looser); else None and no country is sent."""
    try:
        answer = (setup_steps.read_profile(home).get("answers") or {}).get("current_country")
        label = answer.get("value") if isinstance(answer, dict) else None
        entry = setup_steps.option("countries", label) if isinstance(label, str) and label else None
        code = entry.get("code") if isinstance(entry, dict) else None
        return code if isinstance(code, str) and len(code) == 2 and code.isalpha() else None
    except Exception:
        return None


_READ = object()   # `send(session=_READ)`: read the browser now


def send(home=None, relay_url=None, transport=None, session=_READ):
    """Read the browser's current LinkedIn session and send it to the relay. Returns an outcome word
    (CONNECTED, NO_LICENSE, NOT_SIGNED_IN, UNAVAILABLE, BUSY, REFUSED, LICENSE, UNREACHABLE, CERTIFICATE,
    TEMPORARY). Never raises, and nothing of the session leaves this function except in the request.
    `session`: what was read from the live view's Chrome at Done (a `Session`, or None for no sign-in),
    used in place of a read."""
    key = setup_steps.license_key(home)
    if not key:
        return NO_LICENSE
    if session is _READ:
        try:
            session = linkedin_session.current(home)
        except linkedin_session.Busy:
            return BUSY
        except linkedin_session.Unavailable:
            return UNAVAILABLE
    if session is None:
        return NOT_SIGNED_IN
    body = {"session": session.cookie, "user_agent": session.user_agent}
    code = country(home)
    if code:
        body["country"] = code
    mark = _mark(session)
    del session
    outcome = status = parsed = None
    try:
        call = transport or unipile_port.RelayTransport(relay_url or setup_steps.RELAY_URL, key, SEND_TIMEOUT, home=home)
        status, parsed = call("POST", SESSION_PATH, body=body, timeout=SEND_TIMEOUT)
    except Exception as error:   # only what kind of failure it was is kept, never the error or its text
        import tls_setup
        outcome = CERTIFICATE if tls_setup.is_certificate_error(error) else UNREACHABLE
    del body
    if outcome is not None:
        return outcome
    relay_code = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) else None
    if status == 200 and isinstance(parsed, dict) and parsed.get("connected") is True:
        with _sent_lock:
            _sent[_home_key(home)] = mark
        return CONNECTED
    if status == 401 or relay_code == "LICENSE_INVALID":
        unipile_port.refused_now(home)
        return LICENSE
    if relay_code in RELAY_CODES:
        return RELAY_CODES[relay_code]
    if relay_code in UNREACHED_CODES or (relay_code is None and type(status) is int and status >= 500):
        return UNREACHABLE   # the service, or what stands in front of it, cannot be reached now
    if relay_code in LATER_CODES or status in LATER_STATUSES:
        return LATER   # never "sign in again": the sign-in was not what was refused
    return TEMPORARY


def held(home=None):
    """True while LinkedIn reads as disconnected (owner A5: outreach waits). Never raises."""
    try:
        return bool(setup_steps.load_state(home).get(HOLD_KEY))
    except Exception:
        return False


def hold(home=None):
    """Mark LinkedIn disconnected (kept from the first time it was seen). Never raises."""
    try:
        if not held(home):
            setup_steps.update_state(lambda value: value.setdefault(HOLD_KEY, {"since": setup_steps.now_iso()}), home)
    except Exception:
        pass


def release(home=None):
    try:
        if held(home):
            setup_steps.update_state(lambda value: value.pop(HOLD_KEY, None), home)
    except Exception:
        pass


def _sent_ok(home):
    """After the relay accepted a session: this install is connected the new way, and the next
    account read confirms it as a first connect does."""
    def change(value):
        value[MODE_KEY] = SESSION_MODE
        value.pop(UNREADABLE_KEY, None)   # the day's clock (UNREACHED_KEY) ends only when the account reads connected (B11)
        value["linkedin_premium_due"] = True   # Premium is read when LinkedIn connects (owner, 2026-10-27)
    setup_steps.update_state(change, home)


def _record_failure(home, outcome):
    if outcome == NO_LICENSE:
        return setup_steps.record(home, "linkedin", "needs_you", "Needs your license first.", offer="none")
    if outcome == LICENSE:
        return setup_steps.record(home, "linkedin", "failed", "Your subscription isn't active. Check Settings > "
                                  "License.", offer="retry")
    if outcome == CERTIFICATE:
        return setup_steps.record(home, "linkedin", "failed", setup_steps.CERTIFICATE_FAILED % "the LinkedIn service's",
                                  offer="signin")
    status, message = MESSAGES[outcome]
    return setup_steps.record(home, "linkedin", status, message, offer="signin")


# --- On the live view and the Needs you cards (the join with E15) --------------------------------------
CARD_KEY = "linkedin-sign-in"                       # this step's name where a card or the live view has a job key
SIGN_IN_URL = "https://www.linkedin.com/login"     # the server's own constant: never an address from a request
SITE = "linkedin.com"
_app = {}   # the dashboard's live view and install (`attach`)


def attach(view, home=None, relay_url=None):
    """The dashboard's one live view and where its install is (`dashboard.create_app`). None detaches."""
    _app.clear()
    if view is not None:
        _app.update(view=view, home=home, relay_url=relay_url)


def sign_in_view():
    """[Open Sidedoor's browser] in the LinkedIn step, and the card's [Sign in]: the block that shows
    LinkedIn's sign-in page in the dashboard's live view, where the user signs in themselves. None where
    this computer cannot show it. One step, whichever page offers it; pressing Done there is the user's
    "I'm signed in" (`_signed_in`)."""
    view = _app.get("view")
    if view is None:
        return None
    try:
        # The LinkedIn card has no posting link: the view never says "apply yourself" there (`live_view.said`).
        target = view.offer(CARD_KEY, SIGN_IN_URL, "sign_in", domain=SITE, on_ended=_signed_in, before_stop=capture,
                            apply_link=False)
    except Exception:
        return None
    if not target:
        return None
    import user_step
    return user_step.FRAGMENT % target


# What `capture` read from the live view's Chrome at Done, for the one `_connect_after_done` that follows:
# {"session": Session or None}. Memory only, taken (and so forgotten) by that call or by the next step's end.
_captured = {}
_captured_lock = threading.Lock()


def capture(endpoint):
    """The live view's `before_stop` (Done, a worker thread, the step's Chrome still running): read the
    LinkedIn session from that Chrome now. The link then does not depend on what Chrome has written to its
    profile by the time it is stopped (2026-11-13: a sign-in made seconds before Done was lost). A read
    that fails leaves nothing here and the ordinary read follows. Never raises."""
    import linkedin_reader
    with _captured_lock:
        _captured.clear()
    try:
        session = linkedin_reader.read_running(endpoint)
    except Exception:
        return
    with _captured_lock:
        _captured["session"] = session


def capture_if_due(endpoint):
    """`capture` for a LinkedIn step another need opened (the contact finder's card): only while outreach
    waits on this sign-in too. Otherwise nothing is read."""
    if card_due():
        capture(endpoint)


def _take_captured():
    """(True, Session or None) once after a `capture` that read the browser, else (False, None)."""
    with _captured_lock:
        found = "session" in _captured
        session = _captured.pop("session", None)
    return found, session


def _signed_in(ended):
    """The live view's `on_ended` (a worker thread, after its browser has stopped and is free): Done
    connects outreach with the sign-in, exactly as [I'm signed in] does. Any other ending does nothing."""
    import live_view
    if getattr(ended, "reason", None) in live_view.STEP_FINISHED:
        _connect_after_done()
    else:
        _take_captured()   # nothing read at an earlier Done is kept past this step


def after_linkedin_step():
    """The user finished a LinkedIn step in Sidedoor's browser that another need opened (the contact
    finder's card): if outreach was waiting on the user's sign-in too, connect it with the same sign-in.
    Never raises."""
    try:
        if card_due():
            _connect_after_done()
        else:
            _take_captured()
    except Exception:
        pass


def _connect_after_done():
    """Done in the live view. Connect as [I'm signed in] does, except when nothing changed: the same
    sign-in was already accepted and the account only does not show as connected yet. Then nothing is sent
    again; the account is read once more."""
    home, relay_url = _app.get("home"), _app.get("relay_url")
    read, session = _take_captured()   # what Done read from the step's own Chrome, when it could
    try:
        step = setup_steps.load_state(home)["steps"].get("linkedin", {})
        if step.get("message") == SENT_NOT_CONNECTED:
            current = session if read else linkedin_session.current(home)
            with _sent_lock:
                same = current is not None and _sent.get(_home_key(home)) == _mark(current)
            del current
            if same:
                return setup_steps.linkedin_status(home, relay_url or setup_steps.RELAY_URL)
    except Exception:
        pass
    return connect(home, relay_url, session=session) if read else connect(home, relay_url)


def card_messages():
    """The outreach states a sign-in in Sidedoor's browser can clear (join review): only these raise the
    "needs you to sign in" card. Not: a sign-in already accepted that only does not show yet, a connection
    that no longer exists (contact support), a browser or service that cannot be reached or read, or a
    disconnect the hourly check has not tried to mend yet. Those keep their own sentence in the step and
    on Overview."""
    return {"LinkedIn is not connected.", SIGN_IN_AGAIN, NOT_CONNECTED_A_DAY} | {
        MESSAGES[outcome][1] for outcome in (NOT_SIGNED_IN, REFUSED, MISMATCH, ELSEWHERE)}


def card_due():
    """True while the LinkedIn step waits on a sign-in the user can do, after Setup is finished."""
    try:
        state = setup_steps.load_state(_app.get("home"))
    except Exception:
        return False
    step = state["steps"].get("linkedin", {})
    return (bool(state.get("setup_finished_at")) and step.get("status") == "needs_you"
            and step.get("message") in card_messages())


def shown(message):
    """A recorded sentence as the user reads it. The sentences that send the user to [Open Sidedoor's
    browser] end with what is pressed afterwards: where that button opens the live view it is Done; where
    it opens the browser's window it is [I'm signed in], as recorded. Every other sentence is unchanged."""
    if not isinstance(message, str) or "Open Sidedoor's browser" not in message:
        return message
    view = _app.get("view")
    try:
        live = view is not None and bool(view.available())
    except Exception:
        live = False
    if not live:
        return message
    return message.replace("then click I'm signed in", "then press Done").replace("and click I'm signed in", "and press Done")


def browser_opened(home, outcome):
    """[Open Sidedoor's browser] in the LinkedIn step: what `BrowserSession.open` answered, in the step's words."""
    if outcome in ("opened", "brought_forward", "already_open"):
        return setup_steps.record(home, "linkedin", "needs_you", OPEN_MESSAGE, offer="signin")
    message = {"no_chrome": NO_CHROME, "busy": BROWSER_BUSY}.get(outcome) or setup_steps.BROWSER_OPEN_MESSAGES[outcome][1]
    return setup_steps.record(home, "linkedin", "failed", message, offer="signin")


def connect(home=None, relay_url=None, transport=None, session=_READ):
    """[I'm signed in] / [Reconnect LinkedIn] in Setup and Settings: send the browser's session, then
    read the account. Returns the setup state. `session`: as `send`."""
    if not setup_steps.license_key(home) and setup_steps.keys_waiting(home):
        return setup_steps.load_state(home)   # KR7: the saved LinkedIn state stays as it is
    if not _take_turn():
        return setup_steps.load_state(home)   # another send just finished: show what it recorded
    try:
        before = setup_steps.load_state(home)["steps"].get("linkedin", {})
        outcome = send(home, relay_url, transport, session)
        del session
        if outcome in RETRY_CAN_WORK and before.get("status") == "done" and not held(home):
            # Round 5 (4): [Reconnect LinkedIn] on a connected row that has to wait. Nothing changed:
            # the step stays connected and carries a note for this one view (the next read drops it).
            return setup_steps.record(home, "linkedin", "done", before.get("message"), offer="reconnect",
                                      notice=MESSAGES[outcome][1] + " " + STILL_CONNECTED)
        if outcome != CONNECTED:
            return _record_failure(home, outcome)
        _sent_ok(home)
        return setup_steps.linkedin_status(home, relay_url or setup_steps.RELAY_URL, transport, just_sent=True)
    finally:
        _turn.release()


def _now():
    return dt.datetime.now(dt.timezone.utc)


# Review rounds 5 and 6: what a page read keeps on the step until the user acts again. Only the reason a
# connect was refused or could not be made: it stays true. A sentence about what the browser or the service
# is doing right now (open, in front, busy, not signed in, unreachable, "just now") is never kept, because
# it stops being true; nor is the result of a send, which the next read replaces.
REFUSAL_OUTCOMES = (REFUSED, ELSEWHERE, MISMATCH, GONE, TEMPORARY, UNAVAILABLE)


def kept_messages():
    """The refusal reasons a read keeps, connected-before or not."""
    return {MESSAGES[outcome][1] for outcome in REFUSAL_OUTCOMES} | {setup_steps.CERTIFICATE_FAILED % "the LinkedIn service's"}


def _kept_while_disconnected():
    """`kept_messages`, and what the hourly check itself wrote (it rewrites its own line every hour)."""
    # SENT_NOT_CONNECTED too: while the account is listed and not connected it stays true, and a Done with
    # the same sign-in sends nothing again (`_connect_after_done`).
    return kept_messages() | {SIGN_IN_AGAIN, RECONNECTING, UNREADABLE, UNREACHED_A_DAY, NOT_CONNECTED_A_DAY,
                              DISCONNECTED_READ, SENT_NOT_CONNECTED}


def _waited(home, message=None):
    """A12: a try could not reach the browser or the service (B11: or its session was accepted and the
    account still does not read connected). True once that has lasted UNREACHED_SECONDS since the first
    such try; the first time it is True one notification (`message`) is queued."""
    result = []

    def change(value):
        now = _now()
        since = value.get(UNREACHED_KEY)
        try:
            started = dt.datetime.fromisoformat(since)
        except (TypeError, ValueError):
            started, since = now, now.replace(microsecond=0).isoformat()
            value[UNREACHED_KEY] = since
        long = (now - started).total_seconds() >= UNREACHED_SECONDS
        if long and value.get(NOTIFIED_KEY) != since:
            value[NOTIFIED_KEY] = since
            value[NOTICE_KEY] = {"key": "linkedin-unreachable:" + since, "message": shown(message or UNREACHED_A_DAY)}
        result.append(long)
    setup_steps.update_state(change, home)
    return result[0]


def pending_notice(home=None):
    """{"key", "message"} of a notification not yet handed to the outbox, else None. Never raises."""
    try:
        value = setup_steps.load_state(home).get(NOTICE_KEY)
    except Exception:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("key"), str) and isinstance(value.get("message"), str) else None


def clear_notice(home, key):
    def change(value):
        if isinstance(value.get(NOTICE_KEY), dict) and value[NOTICE_KEY].get("key") == key:
            value.pop(NOTICE_KEY)
    setup_steps.update_state(change, home)


def disconnected(home=None, relay_url=None, transport=None, resend=False, just_sent=False):
    """The account reads as disconnected (`setup_steps.linkedin_status`): outreach is held. With `resend`
    (the hourly check only) the current session is sent again, and the user is asked only when that
    fails for a reason waiting cannot clear, or after a day of tries that reached nothing. A page read
    sends nothing and keeps what an earlier try or button recorded."""
    hold(home)
    if just_sent:
        if _waited(home, NOT_CONNECTED_A_DAY):   # B11: the same 24 hour rule as A12
            return setup_steps.record(home, "linkedin", "needs_you", NOT_CONNECTED_A_DAY, offer="signin")
        # Round 5 (5): the message asks the user to act, so the step says "needs you", not "checking".
        return setup_steps.record(home, "linkedin", "needs_you", SENT_NOT_CONNECTED, offer="sent")
    if not resend:
        state = setup_steps.load_state(home)
        saved = state["steps"].get("linkedin", {})
        if saved.get("status") != "done" and saved.get("message") in _kept_while_disconnected():
            return state
        return setup_steps.record(home, "linkedin", "needs_you", DISCONNECTED_READ, offer="signin")
    outcome = send(home, relay_url, transport)
    if outcome != UNAVAILABLE:   # B12: the unreadable tries are counted only while they are in a row
        setup_steps.update_state(lambda value: value.pop(UNREADABLE_KEY, None), home)
    if outcome == CONNECTED:
        _sent_ok(home)
        return setup_steps.linkedin_status(home, relay_url or setup_steps.RELAY_URL, transport, just_sent=True)
    if outcome in WAITS:
        if _waited(home):
            return setup_steps.record(home, "linkedin", "needs_you", UNREACHED_A_DAY, offer="signin")
        if outcome in RETRY_CAN_WORK:
            return setup_steps.record(home, "linkedin", "checking", RECONNECTING, offer="signin")
        return _record_failure(home, outcome)   # round 2: what is true and what to do, as the button shows it
    setup_steps.update_state(lambda value: value.pop(UNREACHED_KEY, None), home)
    if outcome in (NO_LICENSE, LICENSE) or outcome in USER_MUST_ACT:
        return _record_failure(home, outcome)
    if outcome in ASKS_USER:
        return setup_steps.record(home, "linkedin", "needs_you", SIGN_IN_AGAIN, offer="reconnect")
    if outcome == UNAVAILABLE:
        # B2: nothing can reconnect it from here, so nothing says it is reconnecting. The user is asked
        # from the UNREADABLE_TRIES-th failed try in a row.
        tries = []

        def count(value):
            value[UNREADABLE_KEY] = (value.get(UNREADABLE_KEY) if type(value.get(UNREADABLE_KEY)) is int else 0) + 1
            tries.append(value[UNREADABLE_KEY])
        setup_steps.update_state(count, home)
        asked = tries[0] >= UNREADABLE_TRIES
        return setup_steps.record(home, "linkedin", "needs_you" if asked else "failed", UNREADABLE, offer="signin")
    return setup_steps.record(home, "linkedin", "failed", UNREADABLE, offer="signin")


def keep(home=None, relay_url=None, transport=None):
    """One check (the scheduler runs it before each hourly round): read the account, which re-sends on
    a disconnect; then, for an install connected the new way whose account reads connected, send the
    session again when it is not the one this process last sent. Returns a word for tests and logs.
    Never raises."""
    if not _take_turn():
        return "WAITED"   # [I'm signed in] was sending: nothing more is sent this hour
    try:
        return _keep(home, relay_url, transport)
    finally:
        _turn.release()


def _keep(home, relay_url, transport):
    try:
        state = setup_steps.linkedin_status(home, relay_url or setup_steps.RELAY_URL, transport, resend=True)
        step = state["steps"].get("linkedin", {})
        if step.get("status") != "done" or state.get(MODE_KEY) != SESSION_MODE:
            return "READ"
        try:
            session = linkedin_session.current(home)
        except linkedin_session.Unavailable:
            return "READ"
        if session is None:
            return "READ"
        with _sent_lock:
            same = _sent.get(_home_key(home)) == _mark(session)
        del session
        if same:
            return "UNCHANGED"
        return "RESENT" if send(home, relay_url, transport) == CONNECTED else "RESEND_FAILED"
    except Exception:
        return "FAILED"


_keeper = None


def keep_soon(home=None, relay_url=None, after=None):
    """Run `keep` on a thread of its own (it waits on the network), then `after()` (never raises); one at
    a time. True when started."""
    global _keeper
    if _keeper is not None and _keeper.is_alive():
        return False

    def run():
        keep(home, relay_url)
        if after is not None:
            try:
                after()
            except Exception:
                pass
    _keeper = threading.Thread(target=run, name="linkedin-keep", daemon=True)
    _keeper.start()
    return True
