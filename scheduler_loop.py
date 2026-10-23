"""The scheduler: a standard-library loop inside the Sidedoor (dashboard) process (E5.T1 option B;
owner, 2026-10-22: "lets go with your recommedation"). It replaces the three launchd jobs the
owner's `install.py` wrote; the product has no install.py (E5.T2 removed it) and registers one
login entry for `sidedoor serve --start --no-browser` instead (`login_item`).

What it does, and nothing more:

- Three cadences, as launchd ran them: the hourly cycle at minute 0 of every hour (`sidedoor job
  run`), applications at minute `applications_minute` (30; the old install.py's APPLICATIONS_MINUTE) of
  every hour (`sidedoor job applications`), and the research tick every
  `research_dispatch_interval_seconds` (300; `sidedoor job research`), skipping the research
  instant on the hourly :00 except at `day_start_hour` (see `schedule`). Minutes are read in
  `config["timezone"]`; the 9:00 research boundary is not computed here: the research child's
  `daily_schedule.claim`/`due_day` decide the service day, as today.
- Per job it remembers the latest scheduled instant it fired. A job whose latest due instant is
  newer is due; however many instants a sleep or a clock jump skipped, it fires once.
- A due job whose previous child is still alive is skipped and that instant is spent, as launchd's
  skip was (the child's own lock would refuse anyway).
- While a pause flag is set (`scheduler_control`: the user's or the license pause) nothing starts
  and no instant is spent, so the next tick after Resume starts what is due, once.
- Each job starts as a child process of the same program (`job_command`), in its own session or
  process group, and is never waited on, signalled or killed: Pause and Quit (owner, 2026-10-22,
  option (a)) stop new starts only; a running child finishes on its own.
- The loop never sleeps more than `TICK_SECONDS` (30 s, under the 60 s cap) and reads the wall
  clock each tick, so a sleeping machine costs at most one tick after wake. Each tick sets an
  in-memory heartbeat (`status()["last_tick"]`).

The loop adds no guarantee of its own: `runs.slot`, `research_days`, `research_turn_pending`,
`state/runner.lock`, `state/applications.lock` and `state/browser.lock` remain the guarantees (C1-C6).

The license heartbeat (E8.T2, `license_clock`) is its own timer outside this loop, so it runs while
paused; `start_license_heartbeat` starts it. The license pause (E8.T3) is synced from the license
state (`sync_license_pause`) after every heartbeat step and, through `license_sync`, before this
loop starts a due job, so an expiry pauses before the next job; a running child finishes on its own.
"""

import resources
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import scheduler_control
import paths
import portable


ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "orchestrator.py"
DATABASE = Path(paths.STATE) / paths.DATABASE
# Research first: on a wake every due child starts in this order, and the children themselves yield
# to research (research_turn_pending, research_holds_turn), as under launchd.
JOBS = ("research", "run", "applications")
TICK_SECONDS = 30
HOURLY_MINUTE = 0
APPLICATIONS_MINUTE = 30
RESEARCH_INTERVAL_SECONDS = 300
READ_TIMEOUT_SECONDS = 10


def frozen():
    """True in a frozen build: PyInstaller-style `sys.frozen`, or Nuitka's `__compiled__`."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def program():
    """The frozen binary's own path, which every child Sidedoor starts of itself runs (E9.T1).

    Not `sys.executable` under Nuitka: in a standalone build that names a `python` next to the
    binary that does not exist (observed with Nuitka 4.3.1: Contents/MacOS/python), kept for
    multiprocessing. Nuitka records the real one, symlinks resolved, as `__compiled__.process_exe`.
    A PyInstaller-style build (`sys.frozen`) and a source run use `sys.executable`."""
    compiled = globals().get("__compiled__")
    chosen = getattr(compiled, "process_exe", None) or sys.executable
    if frozen() and not (chosen and os.path.isfile(chosen)):
        raise RuntimeError("FROZEN_BINARY_NOT_FOUND: Sidedoor cannot find its own program (%r) to start "
                           "a part of itself" % (chosen,))
    return chosen


def job_command(name):
    """The command that runs one job as a child of this program: `[<binary>, "job", name]` when
    frozen, `[<python>, <orchestrator.py>, "job", name]` from source. Both enter the existing
    `orchestrator.main` paths unchanged (`sidedoor job run|research|applications`)."""
    if name not in JOBS:
        raise ValueError("Unknown job: %r" % (name,))
    if frozen():
        return [program(), "job", name]
    return [sys.executable, *portable.UTF8_FLAGS, str(ENTRY), "job", name]


# Each child's output goes to the file launchd used for that job, so log_retention (STREAMS,
# ARCHIVE_NAME) rotates and retains it exactly as before: a `run` or `research` child sweeps at its
# start under state/runner.lock, rotating its own stdout/stderr and skipping files another live
# child holds open.
LOG_NAMES = {"run": "launchd", "research": "research", "applications": "applications"}


# Windows process creation flags (subprocess has them only on Windows): a new process group, no
# console of its own and none inherited, so no console event (Ctrl-C, a closed window) reaches a child.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def detach_options(windows=None):
    """Popen options that detach a child from this process: its own session on POSIX; on Windows
    a new process group with no console (CREATE_NO_WINDOW | DETACHED_PROCESS)."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def spawn(name, root=None, windows=None):
    """Start one job child, detached from this process's session or process group (so a Ctrl-C or
    a Quit here never reaches it), output appended to logs/<LOG_NAMES[name]>.{stdout,stderr}.log.
    Never waited on; this process keeps no handle on the log files."""
    root = paths.data(root)
    logs = paths.logs(root)
    logs.mkdir(exist_ok=True, mode=0o700)
    windows = os.name == "nt" if windows is None else windows
    detach = detach_options(windows)
    stem = LOG_NAMES[name]
    if windows:
        # No process rotates its own stdout/stderr on Windows (a held file cannot be renamed): the
        # job's previous child has exited (tick skips a job whose child is alive), so its two files
        # are archived here, before the next child opens them; a file still held is left for a
        # later spawn (E2.T3 R2).
        import log_retention
        log_retention.rotate_for_spawn([logs / f"{stem}.stdout.log", logs / f"{stem}.stderr.log"])
    with open(logs / f"{stem}.stdout.log", "ab") as out, open(logs / f"{stem}.stderr.log", "ab") as err:
        return subprocess.Popen(job_command(name), cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                close_fds=True, **detach)


def load_config(root=None):
    try:
        value = resources.read_json("config.json")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_connection(root):
    path = paths.database(root).resolve()
    if not path.exists():
        return None
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=READ_TIMEOUT_SECONDS)


def pause_state(root=None):
    """The pause flags (`scheduler_control.state`), read-only; all None before the database exists."""
    database = _read_connection(root)
    if database is None:
        return dict.fromkeys(("user_paused_at", "user_pause_reason", "license_paused_at", "license_pause_reason"))
    try:
        return scheduler_control.state(database)
    finally:
        database.close()


def paused(root=None):
    value = pause_state(root)
    return bool(value["user_paused_at"] or value["license_paused_at"])


def sync_license_pause(root=None, home=None, now=None):
    """E8.T3: set or clear the license pause from the stored license state (`license_clock.view`,
    `pause_reason`; `scheduler_control.sync_license_pause`). Reads files only, never the relay.
    Returns the flags, or None when there is no database yet (nothing is created, PW7; the loop
    still stops on `license_pause_reason`, read from the files).

    PW4: the view is read and the flag written under `license_clock._write_lock`, the lock every
    store of a new entitlement (activation, [Change key], a good heartbeat) holds while it writes
    the files. So a decision is never made from a view older than a renewal that finished first: a
    renewal either lands before the read (and is seen) or waits until the write is done, and its own
    sync, after it, clears the pause."""
    import license_clock
    path = paths.database(root)
    if not path.exists():
        return None
    with license_clock._write_lock:
        value = license_clock.view(home, now)
        database = sqlite3.connect(path, timeout=READ_TIMEOUT_SECONDS)
        try:
            return scheduler_control.sync_license_pause(database, license_clock.pause_reason(value), value["usable"])
        finally:
            database.close()


def license_pause_reason(home=None, now=None):
    """PW2: the license pause's reason read straight from the license files (no database), so the
    loop stops on an expiry even when the flag cannot be written or no database exists yet."""
    import license_clock
    return license_clock.pause_reason(license_clock.view(home, now))


DEFAULT_TIMEZONE = "America/Los_Angeles"  # the zone the product's modules fall back to
DEFAULT_DAY_START_HOUR = 9
FAILURE_TRACE_SECONDS = 3600  # at most one spawn traceback per job per hour
RESEARCH_SHIFT_SECONDS = 60  # a research instant on the hourly :00 fires this much later
EVENT_SECONDS = 3600  # a one-off problem (clock moved back, a failed tick) stays in last_error this long


def _settings(config, problems):
    """The loop's settings from config.json. A bad value affects only its own job: it falls back to
    the default and is named in `problems` (shown as last_error)."""
    try:
        zone = ZoneInfo(config.get("timezone") or DEFAULT_TIMEZONE)
    except Exception:
        problems.append("timezone %r is not valid; using %s" % (config.get("timezone"), DEFAULT_TIMEZONE))
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    minute = config.get("applications_minute", APPLICATIONS_MINUTE)
    if type(minute) is not int or not 0 <= minute < 60:
        problems.append("applications_minute %r is not a minute 0-59; using %d" % (minute, APPLICATIONS_MINUTE))
        minute = APPLICATIONS_MINUTE
    interval = config.get("research_dispatch_interval_seconds", RESEARCH_INTERVAL_SECONDS)
    if type(interval) is not int or interval <= 0:
        problems.append("research_dispatch_interval_seconds %r is not a positive whole number; using %d"
                        % (interval, RESEARCH_INTERVAL_SECONDS))
        interval = RESEARCH_INTERVAL_SECONDS
    start_hour = config.get("day_start_hour", DEFAULT_DAY_START_HOUR)
    if type(start_hour) is not int or not 0 <= start_hour < 24:
        problems.append("day_start_hour %r is not an hour 0-23; using %d" % (start_hour, DEFAULT_DAY_START_HOUR))
        start_hour = DEFAULT_DAY_START_HOUR
    return zone, minute, interval, start_hour


def schedule(now, config, problems=None):
    """({job: POSIX time of its latest scheduled instant at or before `now`}, {job: its period in
    seconds}) for an aware datetime `now`.

    Hourly minutes are taken at the zone's current UTC offset, so they stay on the local :00 and :30
    across daylight-saving changes. Research runs every interval seconds since the epoch, except
    that a research instant falling on the hourly cycle's :00 is moved `RESEARCH_SHIFT_SECONDS`
    (60 s, two ticks) later, so research never takes state/runner.lock at the second the hourly
    cycle starts and makes it skip ("Another orchestrator cycle is active"), yet keeps its turn
    whatever the interval (with 3600 or 7200 every research instant is on a :00). The one exception is :00 of `day_start_hour`
    (9:00), where research goes first and the hourly cycle yields through research_turn_pending,
    as under launchd. Applications at :30 take state/applications.lock, never runner.lock, so a
    research instant at :30 does not collide with them."""
    problems = [] if problems is None else problems
    zone, minute, interval, start_hour = _settings(config, problems)
    moment = now.timestamp()
    offset = now.astimezone(zone).utcoffset().total_seconds()

    def at_minute(value):
        base = value * 60 - offset
        return moment - ((moment - base) % 3600)

    hourly = at_minute(HOURLY_MINUTE)
    research = moment - (moment % interval)
    if research == hourly and dt.datetime.fromtimestamp(hourly, zone).hour != start_hour:
        # Not yet a minute past :00: the previous instant stands (already fired, or moved itself).
        research = hourly + RESEARCH_SHIFT_SECONDS if moment >= hourly + RESEARCH_SHIFT_SECONDS else research - interval
    return ({"research": research, "run": hourly, "applications": at_minute(minute)},
            {"research": interval, "run": 3600, "applications": 3600})


def latest_instants(now, config):
    return schedule(now, config)[0]


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Loop:
    """One scheduler. `clock`, `spawner`, `is_paused` and `config` are injectable for tests; the
    defaults are the wall clock, `spawn`, the database flags and config.json read each tick."""

    def __init__(self, root=None, clock=None, spawner=None, is_paused=None, config=None, tick_seconds=TICK_SECONDS,
                 license_sync=None, license_reason=None, before_start=None, credentials_hold=None):
        self.root = paths.data(root)
        # HM3: the dashboard's own data-move check (paths.migrate as the port holder), run before due
        # jobs start, so a conflict settled by hand clears without a restart. None: no check.
        self.before_start = before_start
        # E2.T4 (R3): why the saved keys cannot be read now (`credentials.hold_reason`: the OS store
        # is locked or not answering), asked before a due job starts; a reason starts nothing and
        # spends nothing, and the reason is the Overview line. None: no check here.
        self.credentials_hold = credentials_hold
        # E8.T3: syncs the license pause before a due job starts (`sync_license_pause`; the
        # dashboard passes it, tests may inject one). None: no sync here.
        self.license_sync = license_sync
        # PW2: the license pause's reason from the files (`license_pause_reason`), asked before a
        # due job starts whatever the database says; a reason, or a failed read, starts nothing.
        self.license_reason = license_reason
        self.clock = clock or _utcnow
        self.spawner = spawner or (lambda name: spawn(name, self.root))
        self.is_paused = is_paused or (lambda: paused(self.root))
        self.config = config
        self.tick_seconds = tick_seconds
        self.last_fired = {}
        self.children = {}
        # When each job's current child was started (aware UTC). last_fired also moves when a due
        # instant is passed over (the previous child still alive), so it cannot say which start a
        # failure belongs to; Overview compares a failure with the job's own records by this.
        self.started = {}
        self.last_tick = None
        # last_error is built from: this tick's config notes (recomputed every tick, so a fixed
        # value clears at once), problems that hold until their cause goes (a failed start until
        # that job starts, a failed pause check until one succeeds), one-off events (clock moved
        # back, a failed tick) kept EVENT_SECONDS, and why the loop thread stopped (kept).
        self.notes = []
        self._problems = {}
        self._events = {}
        self._fatal = None
        # Per job, its latest child that exited non-zero: {"exit_code", "at"} (for Overview);
        # dropped when a later child of that job exits 0.
        self.job_failures = {}
        self.data_hold = None  # why the latest job waited for the data move (paths.HOLD_EXIT), or None
        self._traced = {}  # job -> monotonic time of its latest printed spawn traceback
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def _reap(self, now):
        """Polls every child (reaps an ended one; never waits) and records a non-zero exit once. A
        clean exit (0) of a later child of the same job clears that job's entry, so Overview shows a
        crash only until the job next runs cleanly."""
        for name, child in list(self.children.items()):
            code = child.poll()
            if code is None or getattr(child, "_sidedoor_reaped", False):
                continue
            try:
                child._sidedoor_reaped = True
            except AttributeError:
                pass
            if code == paths.HOLD_EXIT:
                # E9.T1: the job did not start because the data is not moved into the home yet. A
                # wait, shown as such (data_hold), never a failure.
                self.job_failures.pop(name, None)
                try:
                    self.data_hold = paths.message(paths.status(self.root)) or "Waiting to move your data."
                except Exception:
                    self.data_hold = "Waiting to move your data."
            elif code != 0:
                failure = {"exit_code": code, "at": now.isoformat(timespec="seconds")}
                started = self.started.get(name)
                if started is not None:
                    failure["started"] = started.isoformat(timespec="seconds")
                self.job_failures[name] = failure
            else:
                self.job_failures.pop(name, None)
                self.data_hold = None

    def tick(self):
        """One pass: start every due job whose previous child has ended. Returns what it did:
        {"started": [...], "skipped_alive": [...], "paused": bool}."""
        with self._lock:
            now = self.clock()
            self.last_tick = now
            self._reap(now)
            config = self.config if self.config is not None else load_config(self.root)
            notes = []
            instants, periods = schedule(now, config, notes)
            self.notes = notes
            for name in JOBS:
                last = self.last_fired.get(name)
                if last is not None and instants[name] < last - periods[name]:
                    # The wall clock went back (a fast clock corrected): start counting again from
                    # now, without firing, so no job stops waiting for a time that already passed.
                    self.last_fired[name] = instants[name]
                    self._events["clock:" + name] = ("clock moved back; %s rescheduled from %s" % (
                        name, dt.datetime.fromtimestamp(instants[name], dt.timezone.utc).isoformat(timespec="seconds")),
                        now.timestamp())
            due = [name for name in JOBS if self.last_fired.get(name) is None or instants[name] > self.last_fired[name]]
            result = {"started": [], "skipped_alive": [], "paused": False}
            if not due:
                return result
            if self.credentials_hold is not None:
                try:
                    hold = self.credentials_hold()
                except Exception as error:  # the children read the store themselves; a failed check holds nothing
                    hold = None
                    self._problems["credentials_check"] = "credentials check failed: %s" % type(error).__name__
                else:
                    self._problems.pop("credentials_check", None)
                if hold:
                    self._problems["credentials"] = hold
                    result["paused"] = True
                    return result
                self._problems.pop("credentials", None)
            if self.license_sync is not None:
                try:
                    self.license_sync()
                    self._problems.pop("license", None)
                except Exception as error:  # the files decide below (PW2), then the stored flags
                    self._problems["license"] = "license pause check failed: %s" % type(error).__name__
                    if self.license_reason is None:  # nothing else can tell: fail closed
                        result["paused"] = None
                        return result
            if self.license_reason is not None:
                # Fails closed, like an unreadable pause flag: nothing starts, nothing is spent.
                try:
                    reason = self.license_reason()
                except Exception as error:
                    self._problems["license_read"] = "license read failed: %s" % type(error).__name__
                    result["paused"] = None
                    return result
                self._problems.pop("license_read", None)
                if reason:
                    result["paused"] = True
                    return result
            try:
                # E0.T8 (owner OA2): OpenAI refused the user's API key, or its billing waits ran past a
                # day: every job uses Codex, so nothing starts and nothing is spent until a key passes
                # its test (openai_auth.release). ChatGPT sign-in never holds here.
                import openai_auth
                held = openai_auth.hold()
            except Exception:
                held = None
            if held:
                result["paused"] = True
                result["held"] = "openai_api_key:" + held["kind"]
                return result
            try:
                if self.is_paused():
                    result["paused"] = True
                    return result
            except Exception as error:
                # Unknown pause state (e.g. the database is busy): start nothing, spend nothing;
                # the next tick asks again.
                self._problems["pause"] = "pause check failed: %s" % type(error).__name__
                result["paused"] = None
                return result
            self._problems.pop("pause", None)
            if self.before_start is not None:
                try:
                    self.before_start()
                    self._problems.pop("data-move", None)
                except Exception as error:   # the jobs themselves still hold if the data is not ready
                    self._problems["data-move"] = "data move check failed: %s" % type(error).__name__
            alive = {name for name, child in self.children.items() if child.poll() is None}
            for name in due:
                if name in alive:
                    self.last_fired[name] = instants[name]
                    result["skipped_alive"].append(name)
                    continue
                try:
                    self.children[name] = self.spawner(name)
                    self.started[name] = now
                except Exception as error:
                    # Not spent: the next tick tries again. The traceback is printed at most once
                    # an hour per job; the error itself is always kept in last_error.
                    self._problems["start:" + name] = "start %s failed: %s: %s" % (name, type(error).__name__, error)
                    moment = time.monotonic()
                    if moment - self._traced.get(name, -FAILURE_TRACE_SECONDS) >= FAILURE_TRACE_SECONDS:
                        self._traced[name] = moment
                        traceback.print_exc()
                    continue
                self._problems.pop("start:" + name, None)
                self.last_fired[name] = instants[name]
                result["started"].append(name)
            return result

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as error:
                    moment = self.clock()
                    self._events["tick"] = ("tick failed: %s: %s" % (type(error).__name__, error), moment.timestamp())
                    traceback.print_exc()
                self._stop.wait(self.tick_seconds)
        except BaseException as error:
            # Anything else ends the loop thread; it is recorded so status() shows running False
            # with the reason, never a silent stop.
            self._fatal = "scheduler stopped: %s: %s" % (type(error).__name__, error)

    @property
    def last_error(self):
        """One line for Overview, or None when nothing is wrong now."""
        try:
            now = self.clock().timestamp()
        except Exception:
            now = None
        events = [text for text, at in self._events.values() if now is None or abs(now - at) < EVENT_SECONDS]
        parts = list(self.notes) + list(self._problems.values()) + events + ([self._fatal] if self._fatal else [])
        return "; ".join(parts) or None

    def start(self):
        """Start the loop thread (idempotent). The first tick fires every job, as launchd's
        RunAtLoad did; the children's locks and claims decide whether each does anything."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="sidedoor-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        """Quit (owner, 2026-10-22, option (a)): the loop stops at once. Running children are left
        alone: no signal, no wait."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()


# --- the process's one scheduler (installed by dashboard.serve) -----------------------------------

_current = None


def install(loop):
    """Make `loop` this process's scheduler; `dashboard.serve` installs one, never started until
    [Start] or `serve --start`."""
    global _current
    _current = loop
    return loop


def current():
    return _current


def start():
    """[Start]: True when the scheduler is now running, False when this process has none (only
    `sidedoor serve`/`dashboard`/`setup` installs one)."""
    if _current is None:
        return False
    _current.start()
    return _current.running()


def stop():
    if _current is not None:
        _current.stop()


def running():
    return _current is not None and _current.running()


def status(root=None):
    """For Overview: {"running": bool, "paused": bool or None (unreadable), "user_paused_at",
    "license_paused_at", "last_tick": ISO time of the latest tick or None, "last_error" (a config
    fallback, a clock correction, a failed start, or why the loop thread stopped), "job_failures":
    {job: {"exit_code", "at"}} for each job's latest child that exited non-zero}."""
    loop = _current
    root = Path(root) if root is not None else (loop.root if loop is not None else paths.data())
    try:
        flags = pause_state(root)
        is_paused = bool(flags["user_paused_at"] or flags["license_paused_at"])
    except sqlite3.Error:
        flags, is_paused = {"user_paused_at": None, "license_paused_at": None}, None
    last_tick = loop.last_tick if loop is not None else None
    return {"running": running(), "paused": is_paused,
            "user_paused_at": flags["user_paused_at"], "license_paused_at": flags["license_paused_at"],
            "last_tick": last_tick.isoformat(timespec="seconds") if last_tick else None,
            "last_error": loop.last_error if loop is not None else None,
            "job_failures": dict(loop.job_failures) if loop is not None else {}}


def start_license_heartbeat(root=None, home=None, relay_url=None, post=None):
    """Start the daily license heartbeat (E8.T2, `license_clock.Heartbeat`): in the Sidedoor
    process but outside the job loop, on its own timer and blind to the pause flags, so it keeps
    running while paused (C10). It checks once at once (the dashboard opened), then daily, and syncs
the license pause after every step (E8.T3, `sync_license_pause`). Returns
    the heartbeat; a second call in the same process returns the one already running. `root` is
    kept for the hook's signature: the heartbeat's files live in the Sidedoor home."""
    import license_clock
    return license_clock.start(home=home, relay_url=relay_url, post=post,
                               after_tick=lambda: sync_license_pause(root, home))


def stop_license_heartbeat():
    """Stop this process's license heartbeat (the dashboard stopping)."""
    import license_clock
    license_clock.stop()
