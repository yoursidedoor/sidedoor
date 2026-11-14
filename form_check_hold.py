"""The hold of an attended submit session (E16.T2 phase 1; owner, 2026-11-12, Y1 to Y4).

A human check on the application form gives a one-time pass that counts only in the browser session that
presses Submit. So the user does the check while that session is running: the session's AI calls the
guard's hold tool (`browser_guard.HUMAN_CHECK_TOOL`), the guard keeps that one call open (`Proxy.
call_human_check`, on a thread of its own), and the user does the check in the live view attached to the
session's Chrome (live_view's attach mode). The call returns when the user presses Done (`finish`) or when
the time is up. The AI never touches the check.

Offered to an ATTENDED submit session only: `prepare` writes the hold's context and sets the config key
every engine reads for the long tool-call allowance (`_application_human_check`) only when the session's
config carries ATTENDED_KEY and its operation is submit. A later phase sets that key (the card-started
submit); nothing sets it today.

Never after Submit is armed, read in two places: in the guard's own process, `ArmBinding.attempted` (the
one arm was called, whatever it answered); in the database, `desktop_requests.armed_at` (through
`desktop_adapter.extend_for_human_check`, which refuses an armed request, one that is no longer claimed,
and one whose lease has run out).

The time held (`held`) is what moves the session: the session's deadline (`application_browser.execute`)
and the arm's deadline (`application_browser.ArmBinding.arm`) are each their own value plus the seconds
held so far, and nothing else. All holds of one session together last LIMIT_SECONDS at most (Y4: "longest
pause 9 minutes"). The claim's lease is extended once, to its own end plus LIMIT_SECONDS.

At the time limit the call answers WAIT_EXPIRED and the request records TIMED_OUT_EVENT; the session then
ends never-armed on CAPTCHA_REQUIRED, which is the existing wall path (`applications.
hold_submission_blocker`: the try is not counted, the job waits on the user) and the card says TIMED_OUT_WORDS.
"""
import contextlib
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

ATTENDED_KEY = "_application_attended"        # config: this submit session is attended (set by a later phase)
CONFIG_KEY = "_application_human_check"       # config: the hold's context file (engines, browser_runtime)
CONTEXT = "form-check-hold.json"              # the guard's --human-check file for an attended submit
DONE_FILE = "human_check_done.json"           # written in the session's folder when the user presses Done
USER_DONE_FILE = "human_check_user_done.json"  # when the user last pressed Done (kept; the card reads it)
ACT_FILE = "human_check_user_acted.json"       # when the user last clicked or typed on the page in the view
# Owner, 2026-11-13: "If the user forgets to click Done, the AI carries on with its job". The hold also
# returns when the user has ACTED on the page in the view since the AI last read it (also before the hold
# opened: with the page shown first, that is the common case) and then stopped for SETTLE_SECONDS; the AI reads
# the page and carries on, or holds again when the check is not done. At most ACTED_RETURNS such returns in
# a session (each is one more turn of the user's AI); after that only Done or the time limit ends a hold.
SETTLE_SECONDS = 4
ACTED_RETURNS = 6
# The engine's calls that only read the page: any other call after a hold means the AI has resumed its work.
READ_TOOLS = frozenset({"browser_snapshot", "browser_take_screenshot", "browser_tabs", "browser_wait_for", "browser_find",
                        "browser_console_messages", "browser_network_requests", "browser_continue"})
LIMIT_SECONDS = 540                           # Y4: the longest pause, all holds of one session together
POLL_SECONDS = 0.5
TIMED_OUT_EVENT = "HUMAN_CHECK_HOLD_TIMED_OUT"
TIMED_OUT_WORDS = "Timed out. Open again when you're ready."   # Y4, the owner's words

DONE_TEXT = ("USER_DONE: the user says the human check is done. The user could click and type on this page "
             "while you waited, so the page may have changed: read it again. Your deadline is later by the "
             "time this call took.")
ACTED_TEXT = ("USER_ACTED: the user has clicked or typed on this page since you last read it, and has not said they are "
              "done. The page may have changed: read it again. When the human check is done, carry on. When it is not, "
              "call wait_for_human_check again. Never touch the check. Your deadline is later by the time this call took.")
EXPIRED_TEXT = ("WAIT_EXPIRED: the user did not finish the human check in time. Do not touch the check. Stop "
                "and return blocked with code CAPTCHA_REQUIRED.")
REFUSED_TEXT = ("HOLD_REFUSED: no wait is available now (it is never available after arm). Go on without it. "
                "If a human check is still in the way before arm, stop and return blocked with code CAPTCHA_REQUIRED.")


def hold_file(directory):
    import application_browser
    return Path(directory) / application_browser.HOLD_FILE


def prepare(config, directory, root, request_id, operation):
    """The session's config, with the hold's context written and named when this is an attended submit;
    otherwise `config` unchanged (no hold, no long tool allowance, the tool not offered)."""
    if operation != "submit" or not (config or {}).get(ATTENDED_KEY):
        return config
    path = Path(directory) / CONTEXT
    path.write_text(json.dumps({"directory": str(directory), "root": str(root), "request_id": request_id}),
                    encoding="utf-8")
    return dict(config, **{CONFIG_KEY: str(path)})


def attended(config):
    """True for a session that was given the hold (`prepare`)."""
    return bool((config or {}).get(ATTENDED_KEY) and (config or {}).get(CONFIG_KEY))


def read(path):
    """The hold record of a session folder's hold file, or None (no hold yet, or another kind of file)."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or "held" not in record:
        return None
    return record


def held(record, now=None):
    """Seconds held so far by this record: the finished holds plus the one now open, never more than
    LIMIT_SECONDS, never less than 0. A record that cannot be read as numbers counts as 0."""
    if not isinstance(record, dict):
        return 0.0
    try:
        total = float(record.get("held") or 0)
        began = record.get("began")
        if began:
            now = now or dt.datetime.now(dt.timezone.utc)
            total += max(0.0, (now - dt.datetime.fromisoformat(began)).total_seconds())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(float(LIMIT_SECONDS), total))


def held_in(path, now=None):
    """Seconds held so far in the session whose hold file is `path` (None or missing: 0)."""
    return held(read(path), now) if path else 0.0


def open_now(directory):
    """True while a hold of the session in `directory` is open (its call is waiting on the user)."""
    record = read(hold_file(directory))
    return bool(record and record.get("began"))


def finish(directory):
    """The user pressed Done in the attached view: the open hold of this session returns."""
    stamp = json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat()})
    (Path(directory) / USER_DONE_FILE).write_text(stamp, encoding="utf-8")
    (Path(directory) / DONE_FILE).write_text(stamp, encoding="utf-8")


def mark_done(directory):
    """The check is behind the user without a Done press (the AI resumed its work): the card says it is
    done. Unlike `finish` this ends no hold."""
    (Path(directory) / USER_DONE_FILE).write_text(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat()}),
                                                 encoding="utf-8")


def acted(directory):
    """The attached view passed a press or a key of the user's on to the page, now."""
    try:
        (Path(directory) / ACT_FILE).write_text(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat()}),
                                                encoding="utf-8")
    except OSError:
        pass


def _acted_at(directory):
    try:
        return dt.datetime.fromisoformat(json.loads((Path(directory) / ACT_FILE).read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resumed(directory):
    """True once the AI has gone back to work after a hold (its first call that is not a read of the page),
    until a hold opens again."""
    record = read(hold_file(directory))
    return bool(record and not record.get("began") and record.get("resumed"))


def input_open(directory):
    """May the attached view pass the user's input on? Until the AI has resumed after a hold: before any
    hold (the user may do the check first), while one is open, and while the AI is only reading the page
    after one. Never once it has resumed (the user cannot interfere with the fill or reach Submit)."""
    return not resumed(directory)


def user_done(directory):
    """True from the user's Done until a hold opens after it (the pass expired, or the check is still not
    done: the user is needed again). While it is true the card offers nothing and says it is done."""
    try:
        at = json.loads((Path(directory) / USER_DONE_FILE).read_text(encoding="utf-8"))["at"]
        pressed = dt.datetime.fromisoformat(at)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    record = read(hold_file(directory))
    try:
        return not (record and record.get("began") and dt.datetime.fromisoformat(record["began"]) > pressed)
    except (TypeError, ValueError):
        return True


def timed_out(database, job_key):
    """True when the job's latest submit request ended a hold at its time limit (TIMED_OUT_EVENT)."""
    try:
        row = database.execute("SELECT request_id FROM desktop_requests WHERE job_key=? AND operation='submit' "
                               "ORDER BY rowid DESC LIMIT 1", (job_key,)).fetchone()
        return bool(row and database.execute("SELECT 1 FROM desktop_events WHERE request_id=? AND event=? LIMIT 1",
                                             (row[0], TIMED_OUT_EVENT)).fetchone())
    except sqlite3.Error:
        return False


class Refused(Exception):
    """The hold cannot begin (armed, not claimed, no time left)."""


class Hold:
    """The guard's hold for an attended submit session (browser_guard.Proxy.human_check_wait)."""

    FAILED_TEXT = REFUSED_TEXT

    def __init__(self, path, arm_binding=None, sleep=time.sleep, clock=None, connect=None):
        self.context = json.loads(Path(path).read_text(encoding="utf-8"))
        self.directory = Path(self.context["directory"])
        self.arm_binding = arm_binding
        self.sleep = sleep
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.connect = connect or self._connect
        import threading
        self.lock = threading.Lock()
        self.last_read = None   # when the engine last read the page (`note_call`)

    @staticmethod
    def listing():
        import browser_guard
        return {"name": browser_guard.HUMAN_CHECK_TOOL,
                "description": "Read the page first: a human check the page shows as done needs no call. "
                               "Call when the form is filled and its human check (a CAPTCHA, hCaptcha, reCAPTCHA, "
                               "Turnstile or an 'I am human' box) is all that is left before arm. The user does "
                               "the check themselves on this page; never click, tick, solve or otherwise touch "
                               "it. The call returns USER_DONE when the user says they are done, USER_ACTED when "
                               "the user clicked or typed on the page and then stopped (read the page: carry on "
                               "when the check is done, call this again when it is not), or WAIT_EXPIRED after "
                               "the time limit (%d minutes for the whole session). It is refused after arm."
                               % (LIMIT_SECONDS // 60),
                "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}}

    def _connect(self):
        import paths
        import run_locks
        database = sqlite3.connect(paths.database(Path(self.context["root"])),
                                   timeout=run_locks.SESSION_TOOL_BUSY_TIMEOUT_SECONDS)
        database.row_factory = sqlite3.Row
        return database

    def _write(self, record):
        hold_file(self.directory).write_text(json.dumps(record), encoding="utf-8")

    def _extend(self, record, now):
        """Extend the claim to its own lease plus LIMIT_SECONDS (once: the same end at every hold). False
        when the request is armed, no longer claimed, or its lease is over (`extend_for_human_check`)."""
        import desktop_adapter
        with contextlib.closing(self.connect()) as database:
            if not record.get("lease"):
                row = database.execute("SELECT lease_until FROM desktop_requests WHERE request_id=?",
                                       (self.context["request_id"],)).fetchone()
                if not row or not row[0]:
                    return False
                record["lease"] = row[0]
            return desktop_adapter.extend_for_human_check(
                database, self.context["request_id"], desktop_adapter.later(record["lease"], LIMIT_SECONDS),
                now.isoformat())

    def _record(self, event, detail):
        import desktop_adapter
        try:
            with contextlib.closing(self.connect()) as database:
                desktop_adapter.record(database, self.context["request_id"], event, self.clock().isoformat(), detail)
                database.commit()
        except Exception:
            pass

    def note_call(self, name):
        """The guard, for every tool call of the engine's that is not the hold itself: after a hold, the
        first call that is not a read of the page means the AI has resumed (`resumed`). Never raises."""
        if name in READ_TOOLS:
            self.last_read = self.clock()   # the AI's latest look at the page: what the user did after it, it has not seen
            return
        try:
            with self.lock:
                record = read(hold_file(self.directory))
                # Review, 2026-11-13: a tick can open an image challenge the user is still solving while the AI
                # reads and fills other fields. So the user's input is cut only when the AI cannot be wrong
                # about the check: at its ARM call (it arms only after reading the check as done), or at its
                # first other call after a hold the user ended with Done.
                import browser_guard
                armed = name == browser_guard.ARM_TOOL
                if record is None:
                    if armed:   # in every case, also when the check passed by itself and the user never clicked
                        self._write({"held": 0.0, "began": None, "resumed": True})
                elif not record.get("began") and not record.get("resumed") and (armed or record.get("ended_by") == "done"):
                    record["resumed"] = True
                    self._write(record)
        except Exception:
            pass

    def call(self):
        """One hold: returns DONE_TEXT, ACTED_TEXT or EXPIRED_TEXT; raises Refused when it may not begin (the guard
        then answers FAILED_TEXT). Never begins once the arm was called or the request is armed."""
        if self.arm_binding is not None and getattr(self.arm_binding, "attempted", False):
            raise Refused("ARM_ATTEMPTED")
        now = self.clock()
        record = read(hold_file(self.directory)) or {"held": 0.0, "began": None}
        if record.get("began"):
            raise Refused("HOLD_ALREADY_OPEN")
        left = LIMIT_SECONDS - held(record, now)
        if left <= 0:
            self._record(TIMED_OUT_EVENT, {"held": LIMIT_SECONDS})
            return EXPIRED_TEXT
        if not self._extend(record, now):
            raise Refused("ARMED_OR_NOT_CLAIMED")
        record["began"] = now.isoformat()
        record["resumed"] = False
        with self.lock:
            self._write(record)
        done = Path(self.directory) / DONE_FILE
        finished = moved = False
        may_return_on_action = int(record.get("acted_returns") or 0) < ACTED_RETURNS
        try:
            while (self.clock() - now).total_seconds() < left:
                if done.exists():
                    finished = True
                    break
                # What the user did on the page since the AI last read it counts, also before this hold opened
                # (2026-11-13, a real run: the tick came one second before the hold, and nothing resumed).
                last = _acted_at(self.directory) if may_return_on_action else None
                if last is not None and last > (self.last_read or now) \
                        and (self.clock() - last).total_seconds() >= SETTLE_SECONDS:
                    moved = True   # the user acted on the page after the AI's last look, then stopped
                    break
                self.sleep(POLL_SECONDS)
        finally:
            ended = self.clock()
            record["held"] = held(record, ended)
            record["began"] = None
            if finished or moved:
                # The user's Done, or their last action when Done was not pressed: attended_submit measures from it.
                record["done_at"] = ended.isoformat()
            record["ended_by"] = "done" if finished else "acted" if moved else "expired"
            if moved:
                record["acted_returns"] = int(record.get("acted_returns") or 0) + 1
            with self.lock:
                self._write(record)
            with contextlib.suppress(OSError):
                done.unlink()
        if finished:
            return DONE_TEXT
        if moved:
            return ACTED_TEXT
        self._record(TIMED_OUT_EVENT, {"held": record["held"]})
        return EXPIRED_TEXT
