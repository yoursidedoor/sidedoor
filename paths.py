"""Where Sidedoor keeps what it writes: the Sidedoor home (E9.T1, owner decision D1 option a).

Everything writable lives in the Sidedoor home (`user_profile.home()`: `$SIDEDOOR_HOME`, else
`~/.config/sidedoor`), next to the keys and the profile, never next to the code:

- `state/`: orchestrator.sqlite3, the run/applications/browser locks, ats-browser, ats-evidence
  and ats-receipts;
- `reports/`; `logs/` (job stdout/stderr and the login entry's sidedoor.*.log); `backups/`;
- `tmp/`: temporary working folders;
- the owner's data read from there: research-source-observations.json and the reconciliation
  `handoff/` folder.

Every module takes these locations from here. A function that receives a `root` (tests pass a
temporary folder) uses that folder in place of the home: `state(root)` is `<root>/state`; with
no root it is `<home>/state`. So every process of one install (the dashboard, its scheduler's
job children, the CLI) resolves the same files: the children inherit `SIDEDOOR_HOME`.

Moving in (`migrate`): until E9.T1 these folders sat in the code folder. Each entry point
(`prepare`: `orchestrator.py` commands and `serve`) first moves what the code folder still holds
into the home, item by item (state/, reports/, logs/, backups/, handoff/, the observations file):

1. never while an older Sidedoor dashboard is still open (something answers on its port; it holds
   no lock and could open the old database), and only under the code folder's runner, applications
   and browser locks (no job of the old layout runs); otherwise BUSY: the entry point waits or
   holds, and never starts on an empty home;
2. `data-migration.json` in the home records each item's progress (MOVING before it moves, DONE
   after), so an interrupted move resumes;
3. a database is checkpointed first (a busy one is BUSY, retried later) and moves with any -wal or
   -shm left; a folder is renamed whole when it can be, else file by file: renamed, or (another
   volume) copied, compared by SHA-256 and only then deleted at the source;
4. nothing in the home is ever replaced. An item the home already holds, a file present in both
   places with different contents, or a source that reappears after its item was moved (an older
   copy of the code ran in between) is a CONFLICT for that item alone; every other item still
   moves. A conflict on state/ holds all work (`MigrationHeld`): the dashboard explains where the
   data is and offers [Move my data now] (`resolve`), which sets the wrong copy aside (renamed,
   never deleted) and finishes the move;
5. the moved database is opened to check it reads, and each moved folder is left in the code
   folder as a link to its new place (best effort), so absolute paths saved in older records, and
   an older copy of the code, still reach the one database (`relocated` maps such paths when the
   link is missing).

Tests set `SIDEDOOR_LEGACY_DATA` to "" (no code-folder data to move) or to a temporary folder.
"""

import datetime as dt
import errno
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
from pathlib import Path

import user_profile
import log

LOG = log.get(__name__)


CODE = Path(__file__).resolve().parent
STATE = "state"
REPORTS = "reports"
LOGS = "logs"
BACKUPS = "backups"
TEMP = "tmp"
DATABASE = "orchestrator.sqlite3"
OBSERVATIONS = "research-source-observations.json"   # research_sources, application_browser
HANDOFF = "handoff"                                   # reconciliation.HANDOFF_DIR
# What moves from the code folder, in this order.
MOVED = (STATE, REPORTS, LOGS, BACKUPS, HANDOFF, OBSERVATIONS)
LINKED = (STATE, REPORTS, LOGS, BACKUPS, HANDOFF)   # folders left as a link in the code folder
LEGACY_VARIABLE = "SIDEDOOR_LEGACY_DATA"   # the folder to move data from; "" means none (tests)
MARKER = "data-migration.json"
WAIT_FILE = "data-migration-wait.json"     # why the latest entry point could not move the data yet
WAIT_SECONDS_SHOWN = 2 * 3600              # a wait older than this is no longer shown
MOVING, DONE, CONFLICT, NONE = "MOVING", "DONE", "CONFLICT", "NONE"
COPY_SUFFIX = ".sidedoor-move"
ASIDE = ".set-aside-"
DASHBOARD_PORT = 47823   # dashboard.PORT
# A job that cannot start because the data is not moved yet exits with this code (EX_TEMPFAIL): the
# scheduler shows it as a wait, never as a failure.
HOLD_EXIT = 75
CHECKPOINT_TIMEOUT_SECONDS = 30   # how long the checkpoint waits for a connection still open on the old database
# Why an item was not moved.
HOME_HAS_DATA = "HOME_HAS_DATA"   # the home already held this item before it was moved
REAPPEARED = "REAPPEARED"         # moved, then the code folder got this item again
FILES_IN_BOTH = "FILES_IN_BOTH"   # part moved; a file is in both places with different contents
# What the two databases of a state/ conflict are compared on (read-only): rows of the tables that
# carry the duplicate checks, and their latest activity (HM1).
KEY_TABLES = ("application_jobs", "outreach_actions", "outreach_action_claims", "outreach_attempts",
              "engagement_events")
TIME_COLUMNS = ("updated_at", "created_at", "observed_at", "claimed_at", "occurred_at", "recorded_at",
                "completed_at", "sent_at")
HOME, CODE_SIDE = "home", "code"   # the two copies [Keep this one] / [Keep that one] choose between
# The dashboard process exports its pid; its own job children then know the port is held by the
# current Sidedoor, not an older one (HM4).
DASHBOARD_PID_VARIABLE = "SIDEDOOR_DASHBOARD_PID"


class MigrationPending(RuntimeError):
    """The move into the home cannot finish now (an older dashboard is open, a job of the old layout
    holds its locks, the database is busy); nothing may use the data until it has."""


class MigrationHeld(MigrationPending):
    """state/ is in both places (`status`): no work starts until the owner chooses (`resolve`)."""


# --- Locations ------------------------------------------------------------------------------------

def data(root=None, home=None):
    """The folder that holds state/, reports/, logs/ and backups/: `root` when given (tests, and
    callers handing on the root they were given), else `home`, else the Sidedoor home."""
    if root is not None:
        return Path(root)
    if home is not None:
        return Path(home)
    return user_profile.home()


def state(root=None, home=None):
    return data(root, home) / STATE


def reports(root=None, home=None):
    return data(root, home) / REPORTS


def logs(root=None, home=None):
    return data(root, home) / LOGS


def backups(root=None, home=None):
    return data(root, home) / BACKUPS


def database(root=None, home=None):
    return state(root, home) / DATABASE


def temp(root=None, home=None):
    """A folder for temporary working folders (created, mode 0700)."""
    folder = data(root, home) / TEMP
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def observations(root=None, home=None):
    return data(root, home) / OBSERVATIONS


def legacy_root():
    """The folder older versions wrote into: this code folder, or `SIDEDOOR_LEGACY_DATA` ("" none)."""
    value = os.environ.get(LEGACY_VARIABLE)
    if value is None:
        return CODE
    return Path(value).expanduser() if value else None


def relocated(path, root=None):
    """A path saved before the move (an absolute path into the code folder's data) as it is now: the
    same path when it exists, else its place under the data folder when it was under the code
    folder (the link left there may be missing, e.g. on Windows). Anything else is returned as is."""
    path = Path(path)
    if path.exists():
        return path
    for old in legacy_roots(root):
        try:
            relative = path.relative_to(old)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in MOVED:
            return data(root) / relative
    return path


def legacy_roots(root=None, recorded=True):
    """Every spelling of the folder older versions wrote into (this code folder and, with `recorded`,
    the one the move record names, as given and resolved), for paths saved before the move."""
    found = []
    for folder in (legacy_root(), _moved_from(root) if recorded else None):
        if folder is not None:
            for candidate in (Path(folder), _resolved(folder)):
                if candidate not in found:
                    found.append(candidate)
    return found


def _moved_from(root=None):
    record = _read_marker(data(root)) or {}
    return Path(record["from"]) if isinstance(record.get("from"), str) else None


def _resolved(path):
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


# --- Moving in ------------------------------------------------------------------------------------

def _warn(text):
    LOG.warning("%s", text)


def _same(first, second):
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return False


def _has_data(path, top_state=False):
    """A file, a link, or a folder with anything in it (lock files at the top of state/ aside)."""
    if path.is_symlink() or path.is_file():
        return True
    if not path.is_dir():
        return False
    return any(not (top_state and child.name.endswith(".lock")) for child in path.iterdir())


def _read_marker(home):
    try:
        value = json.loads((Path(home) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def _items(record):
    value = record.get("items")
    return dict(value) if isinstance(value, dict) else {}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _plan(legacy, home, items):
    """{name: action} for the items still to settle: "move" (fresh), "resume" (part moved),
    "settled" (an unfinished item with nothing left in the code folder) or a conflict reason."""
    plan = {}
    for name in MOVED:
        source, target, top = legacy / name, home / name, name == STATE
        status_, reason = items.get(name), items.get(name + ":reason")
        unfinished = status_ in (MOVING, CONFLICT)
        if (source.is_symlink() and _same(source, target)) or not _has_data(source, top):
            if unfinished:
                plan[name] = "settled"
            continue
        if status_ == DONE or (status_ == CONFLICT and reason == REAPPEARED):
            plan[name] = REAPPEARED   # never let a later copy replace what was moved
        elif status_ == CONFLICT and reason == FILES_IN_BOTH:
            clashing = [legacy / Path(path).relative_to(home) for path in items.get(name + ":files", [])]
            # Resumed once every clashing copy in the code folder has been set aside.
            plan[name] = FILES_IN_BOTH if any(path.exists() for path in clashing) else "resume"
        elif status_ == MOVING:
            plan[name] = "resume"
        elif _has_data(target, top):
            plan[name] = HOME_HAS_DATA
        else:
            plan[name] = "move"
    return plan


def _old_dashboard_running(port=DASHBOARD_PORT):
    """Something answers on the dashboard's port: an open Sidedoor, possibly the previous version,
    which holds no lock and could open the old database while it moves."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _note_wait(home, reason, text):
    try:
        _write_json(Path(home) / WAIT_FILE, {"reason": reason, "text": text, "at": _now()})
    except OSError:
        pass


def _clear_wait(home):
    try:
        (Path(home) / WAIT_FILE).unlink()
    except OSError:
        pass


def _digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _move_file(source, target, copied, clashes):
    """Move `source` to `target`, never replacing a file already there: an identical one means an
    interrupted copy (the source is then removed), a different one is a clash (both kept)."""
    if target.exists() or target.is_symlink():
        if (not source.is_symlink() and not target.is_symlink() and target.is_file()
                and _digest(source) == _digest(target)):
            source.unlink()
        else:
            clashes.append(str(target))
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.rename(source, target)  # the target is absent: nothing is replaced
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
        source.unlink()
        copied.append(str(source))
        return
    temporary = target.with_name(target.name + COPY_SUFFIX)
    temporary.unlink(missing_ok=True)
    shutil.copy2(source, temporary)
    if _digest(source) != _digest(temporary):
        temporary.unlink()
        raise OSError("DATA_MIGRATION_COPY_MISMATCH: " + str(source))
    os.rename(temporary, target)
    source.unlink()
    copied.append(str(source))


def _move(source, target, copied, clashes, top_state=False):
    if source.is_symlink() or not source.is_dir():
        _move_file(source, target, copied, clashes)
        return
    if not top_state and not target.exists() and not target.is_symlink():
        try:
            os.rename(source, target)
            return
        except OSError:
            pass  # another volume, or a folder that cannot be renamed whole: file by file below
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in sorted(source.iterdir()):
        if (top_state and child.name.endswith(".lock")) or child.name.endswith(COPY_SUFFIX):
            continue  # lock files are held by this move and removed after it
        _move(child, target / child.name, copied, clashes)
    if not top_state:
        try:
            source.rmdir()
        except OSError:
            pass


def _checkpoint(folder):
    """Fold every WAL in `folder` into its database and close it. Raises MigrationPending when a
    connection still holds the database (it is moved on a later start)."""
    if not folder.is_dir():
        return
    for wal in sorted(folder.glob("*-wal")):
        database_file = wal.with_name(wal.name[:-len("-wal")])
        if not database_file.is_file():
            continue
        try:
            connection = sqlite3.connect(database_file, timeout=CHECKPOINT_TIMEOUT_SECONDS)
            try:
                busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.OperationalError as error:
            raise MigrationPending("%s is in use (%s)" % (database_file, error)) from None
        if busy:
            raise MigrationPending("%s is in use" % database_file)


def _check_database(path):
    if not path.is_file():
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


def _link_back(legacy, home, names):
    """Leave each moved folder in the code folder as a link to its new place (best effort)."""
    linked = []
    for name in names:
        source = legacy / name
        if name not in LINKED or source.exists() or source.is_symlink() or not (home / name).exists():
            continue
        try:
            os.symlink(home / name, source, target_is_directory=True)
            linked.append(name)
        except OSError:
            pass
    return linked


def _clean_legacy_state(legacy):
    """After the locks are released: the lock files a finished move left, and the then-empty state/."""
    folder = legacy / STATE
    if folder.is_symlink() or not folder.is_dir() or any(not child.name.endswith(".lock") for child in folder.iterdir()):
        return
    for child in folder.glob("*.lock"):
        try:
            child.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass


def _state_reappeared(legacy, home):
    """HM2, after a finished move: a real (not linked) <code folder>/state/orchestrator.sqlite3, i.e. an
    older copy of the code made a database again. One lstat while the link is in place."""
    if legacy is None:
        return False
    try:
        if stat.S_ISLNK(os.lstat(legacy / STATE).st_mode):
            return False
        found = stat.S_ISREG(os.lstat(legacy / STATE / DATABASE).st_mode)
    except OSError:
        return False
    return found and not _same(legacy, home)


def _reopened(record):
    """The finished record with state/ marked REAPPEARED (the usual conflict path takes it from there)."""
    items = _items(record)
    items[STATE], items[STATE + ":reason"] = CONFLICT, REAPPEARED
    return dict(record, state=CONFLICT, items=items, reopened_at=_now())


def _busy(home, reason, text):
    _note_wait(home, reason, text)
    return {"state": "BUSY", "reason": reason, "text": text}


def migrate(home=None, legacy=None, wait_seconds=0, poll_seconds=2, serving=False):
    """Move what the code folder still holds into the home (see the module notes). Returns
    {"state": ...}: DONE_BEFORE, NONE (nothing to move), MOVED or CONFLICT (with "items" moved now
    and "conflicts" {name: reason}), or BUSY ("reason", "text"; nothing moved). `serving`: the
    caller is the dashboard holding the port, so no other dashboard can be open. Raises when a move
    fails part way; the marker then says MOVING for that item and the next start resumes it."""
    import run_locks
    home = data(None, home)
    legacy = legacy_root() if legacy is None else Path(legacy)
    record = _read_marker(home) or {}
    if record.get("state") == DONE:
        if not _state_reappeared(legacy, home):
            return {"state": "DONE_BEFORE"}
        record = _reopened(record)
        _write_json(home / MARKER, record)
    if legacy is None or not legacy.is_dir() or _same(legacy, home):
        return {"state": NONE}
    plan = _plan(legacy, home, _items(record))
    if not plan:
        if record:
            record.update(state=DONE, finished_at=_now())
            _write_json(home / MARKER, record)
        _clear_wait(home)
        return {"state": NONE}
    if not any(action in ("move", "resume", "settled") for action in plan.values()):
        # Only conflicts: nothing to move, so no lock and no port check; work goes on unless
        # state/ is one of them (prepare holds it).
        return {"state": CONFLICT, "items": [], "conflicts": {name: action for name, action in plan.items()}}
    if not serving and _old_dashboard_running():
        if os.environ.get(DASHBOARD_PID_VARIABLE) == str(os.getppid()):
            # HM4: this job was started by the open Sidedoor, which finishes the move itself.
            return _busy(home, "DASHBOARD_MOVES_IT", "The open Sidedoor moves your data into %s at its next "
                         "check (within a minute); this job runs after that." % home)
        return _busy(home, "DASHBOARD_OPEN", "Sidedoor is open (something answers on port %d), possibly the "
                     "previous version. Quit Sidedoor, then open it again: it moves your data into %s before "
                     "it starts any work." % (DASHBOARD_PORT, home))
    with run_locks.held(legacy, run_locks.RUNNER, run_locks.APPLICATIONS, run_locks.BROWSER,
                        wait_seconds=wait_seconds, poll_seconds=poll_seconds) as acquired:
        if not acquired:
            return _busy(home, "OLD_JOB_RUNNING", "A job of the previous version is still running in %s; your "
                         "data moves into %s when it has finished." % (legacy, home))
        try:
            result = _migrate_locked(home, legacy)
        except MigrationPending as error:
            return _busy(home, "DATABASE_BUSY", "The old database is in use (%s); Sidedoor tries again shortly." % error)
    _clean_legacy_state(legacy)
    items = _items(_read_marker(home) or {})
    result["linked"] = _link_back(legacy, home, [name for name in MOVED if items.get(name) == DONE])
    _clear_wait(home)
    if result["conflicts"]:
        _warn("moved %s into %s; not moved: %s. Open the dashboard: its banner says what to do." % (
            ", ".join(result["items"]) or "nothing", home,
            ", ".join("%s (%s)" % item for item in sorted(result["conflicts"].items()))))
    elif result["items"]:
        _warn("moved %s from %s into %s." % (", ".join(result["items"]), legacy, home))
    return result


def _migrate_locked(home, legacy):
    record = _read_marker(home) or {}
    if record.get("state") == DONE:
        return {"state": "DONE_BEFORE", "items": [], "conflicts": {}, "copied": []}
    items = _items(record)
    record = dict(record, state=MOVING, items=items, started_at=record.get("started_at") or _now())
    record["from"] = str(legacy)
    moved, copied = [], []
    for name, action in _plan(legacy, home, items).items():
        if action == "settled":
            items[name] = DONE
        elif action in (HOME_HAS_DATA, REAPPEARED, FILES_IN_BOTH):
            items[name], items[name + ":reason"] = CONFLICT, action
        else:
            if name == STATE:
                _checkpoint(legacy / name)  # before MOVING: a busy database moves nothing
            items[name] = MOVING
            _write_json(home / MARKER, record)
            clashes = []
            _move(legacy / name, home / name, copied, clashes, top_state=name == STATE)
            if clashes:
                items[name], items[name + ":reason"], items[name + ":files"] = CONFLICT, FILES_IN_BOTH, clashes[:20]
                _write_json(home / MARKER, record)
                continue
            items[name] = DONE
            moved.append(name)
        if items[name] == DONE:
            items.pop(name + ":reason", None)
            items.pop(name + ":files", None)
            items[name + ":at"] = _now()   # when the copies split (HM1)
        _write_json(home / MARKER, record)
    if STATE in moved:
        _check_database(database(home=home))
    conflicts = {name: items.get(name + ":reason") for name in MOVED if items.get(name) == CONFLICT}
    unfinished = any(items.get(name) == MOVING for name in MOVED)
    record["state"] = CONFLICT if conflicts else MOVING if unfinished else DONE
    record["updated_at"] = _now()
    if record["state"] == DONE:
        record["finished_at"] = record["updated_at"]
    _write_json(home / MARKER, record)
    return {"state": CONFLICT if conflicts else "MOVED", "items": moved, "conflicts": conflicts, "copied": copied}


# --- What the owner sees, and the way out -------------------------------------------------------

def _describe(path):
    """'N files, last changed T' for a file or folder, best effort."""
    try:
        files = [path] if path.is_file() else [child for child in path.rglob("*") if child.is_file()]
        latest = max((child.stat().st_mtime for child in files), default=None)
    except OSError:
        return ""
    when = dt.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M") if latest else "never"
    return "%d file%s, last changed %s" % (len(files), "" if len(files) == 1 else "s", when)


def _aside_name(path):
    return path.with_name(path.name + ASIDE + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))


def _moment(value):
    """An aware datetime for a stored ISO time, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def database_summary(path):
    """HM1, read-only: {"rows": {table: count}, "latest": ISO time of the latest activity or None} of
    the tables that carry the duplicate checks; None when there is no database at `path`."""
    path = Path(path)
    if not path.is_file():
        return None
    rows, latest = {}, None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in KEY_TABLES:
                if table not in tables:
                    continue
                rows[table] = connection.execute('SELECT count(*) FROM "%s"' % table).fetchone()[0]
                for column in [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)]:
                    if column in TIME_COLUMNS:
                        moment = _moment(connection.execute('SELECT max("%s") FROM "%s"' % (column, table)).fetchone()[0])
                        if moment and (latest is None or moment > latest):
                            latest = moment
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"rows": rows, "latest": None, "error": type(error).__name__}
    return {"rows": rows, "latest": latest.isoformat(timespec="seconds") if latest else None}


def _summary_text(summary):
    if summary is None:
        return "no database"
    counts = ", ".join("%d %s" % (count, table.replace("_", " ")) for table, count in sorted(summary["rows"].items()))
    when = summary["latest"] or "no recorded activity"
    return "%s; last activity %s" % (counts or "no records", when)


def _choose(reason, split, databases):
    """(side to keep or None, why), the same for HOME_HAS_DATA and REAPPEARED (HC1/HC2): only a copy
    that is the only one with records is kept automatically. When both hold records, however few and
    however old, Sidedoor never picks: the owner chooses (a newer timestamp says nothing about which
    copy is complete). When neither does, the reason's default."""
    holds = {side: bool(sum(((databases.get(side) or {}).get("rows") or {}).values())) for side in (CODE_SIDE, HOME)}
    if holds[CODE_SIDE] and holds[HOME]:
        return None, "both copies hold records"
    if holds[CODE_SIDE] or holds[HOME]:
        return (CODE_SIDE if holds[CODE_SIDE] else HOME), "only it holds records"
    return (CODE_SIDE if reason == HOME_HAS_DATA else HOME), "neither holds records"


def _conflict(legacy, home, name, reason, files=(), split=None):
    old, new = legacy / name, home / name
    if reason == HOME_HAS_DATA:
        why = ("%s already existed in the home (made before your data moved there), while your earlier "
               "data is still in %s" % (new, old))
    elif reason == REAPPEARED:
        why = ("your data was already moved from %s to %s, and then an older copy of Sidedoor wrote a new %s"
               % (old, new, old))
    else:
        why = ("part of %s was moved to %s, and %s file%s exist in both places with different contents"
               % (old, new, len(files), "" if len(files) == 1 else "s"))
    databases, side, chosen_because = None, (HOME if reason == FILES_IN_BOTH else
                                             CODE_SIDE if reason == HOME_HAS_DATA else HOME), None
    if name == STATE and reason != FILES_IN_BOTH:
        databases = {HOME: database_summary(new / DATABASE), CODE_SIDE: database_summary(old / DATABASE)}
        side, chosen_because = _choose(reason, split if reason == REAPPEARED else None, databases)
    keep, aside = (old, new) if side == CODE_SIDE else (new, old)
    detail = (lambda path, which: _summary_text(databases[which])) if databases else (lambda path, which: _describe(path))
    result = {"item": name, "reason": reason, "why": why, "files": list(files), "databases": databases,
              "keep_side": side, "choice_required": side is None, "chosen_because": chosen_because,
              "home": str(new), "code": str(old),
              "home_detail": detail(new, HOME), "code_detail": detail(old, CODE_SIDE)}
    if side is None:
        result.update(keep=None, aside=None, keep_detail=None, aside_detail=None, steps=[
            "Quit Sidedoor.",
            "Decide which copy holds the records you want: %s (%s) or %s (%s)." % (
                new, result["home_detail"], old, result["code_detail"]),
            "Rename the other one: add %s and today's date to its name. Keep it until you are sure nothing in "
            "it is needed." % ASIDE.strip("."),
            "Open Sidedoor again."])
        return result
    result.update(keep=str(keep), aside=str(aside), keep_detail=detail(keep, side),
                  aside_detail=detail(aside, HOME if side == CODE_SIDE else CODE_SIDE))
    shown = ", ".join(files) if files else str(aside)
    last = ("Open Sidedoor again: it moves %s into %s." % (old, new) if side == CODE_SIDE else
            "Open Sidedoor again: it moves the rest of %s." % old if reason == FILES_IN_BOTH else "Open Sidedoor again.")
    result["steps"] = ["Quit Sidedoor.",
                       "Rename %s: add %s and today's date to the name. Keep it until you are sure nothing in it is "
                       "needed." % (shown, ASIDE.strip(".")), last]
    return result


def _split(items, record, name):
    return _moment(items.get(name + ":at")) or _moment(record.get("finished_at")) or _moment(record.get("started_at"))


def status(home=None, legacy=None):
    """Where the move stands, without changing anything (dashboard banner, doctor, logs):
    {"state": DONE | NONE | PENDING | MOVING | CONFLICT | WAITING, "home", "legacy", "conflicts":
    [{"item", "reason", "keep", "aside", "why", "steps", ...}], "held": state/ is in conflict (no
    work starts), "wait": {"reason", "text", "at"} or None, "items": the items still to settle}."""
    home = data(None, home)
    legacy = legacy_root() if legacy is None else Path(legacy)
    record = _read_marker(home) or {}
    result = {"home": str(home), "legacy": str(legacy) if legacy else None, "conflicts": [], "held": False,
              "wait": None, "items": []}
    if record.get("state") == DONE:
        if not _state_reappeared(legacy, home):
            return dict(result, state=DONE)
        record = _reopened(record)
    if legacy is None or not legacy.is_dir() or _same(legacy, home):
        return dict(result, state=NONE)
    items = _items(record)
    plan = _plan(legacy, home, items)
    for name, action in plan.items():
        if action in (HOME_HAS_DATA, REAPPEARED):
            result["conflicts"].append(_conflict(legacy, home, name, action, split=_split(items, record, name)))
        elif action == FILES_IN_BOTH:
            files = [str(legacy / Path(path).relative_to(home)) for path in items.get(name + ":files", [])]
            result["conflicts"].append(_conflict(legacy, home, name, FILES_IN_BOTH, files))
    result["held"] = any(conflict["item"] == STATE for conflict in result["conflicts"])
    result["items"] = list(plan)
    try:
        wait = json.loads((home / WAIT_FILE).read_text())
        if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(wait["at"])).total_seconds() < WAIT_SECONDS_SHOWN:
            result["wait"] = wait
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if result["conflicts"]:
        return dict(result, state=CONFLICT)
    if result["wait"] and plan:
        return dict(result, state="WAITING")
    if plan and record.get("state") == MOVING:
        return dict(result, state=MOVING)
    return dict(result, state="PENDING" if plan else NONE)


def message(value):
    """One plain paragraph for a `status(...)`, or None when there is nothing to say."""
    state_ = value["state"]
    if state_ == CONFLICT:
        lead = ("Sidedoor has not started any work: your database is in two places. " if value["held"]
                else "Some of your earlier data was not moved into %s. " % value["home"])
        parts = []
        for c in value["conflicts"]:
            if c["choice_required"]:
                parts.append("%s: %s, and %s. Sidedoor will not pick one: %s holds %s; %s holds %s. Choose the copy to "
                             "keep; the other is set aside (renamed, nothing is deleted)." % (
                                 c["item"], c["why"], c["chosen_because"], c["home"], c["home_detail"], c["code"],
                                 c["code_detail"]))
            else:
                parts.append("%s: %s. Move my data now keeps %s (%s%s) and sets %s aside (%s; renamed, nothing is "
                             "deleted)." % (c["item"], c["why"], c["keep"], c["keep_detail"],
                                            "; " + c["chosen_because"] if c["chosen_because"] else "",
                                            ", ".join(c["files"]) or c["aside"], c["aside_detail"]))
        return lead + " ".join(parts)
    if state_ == "WAITING":
        return "Waiting to move your data: " + value["wait"]["text"]
    if state_ == MOVING:
        return "Moving your data into %s stopped part way; Sidedoor finishes it before it starts any work." % value["home"]
    if state_ == "PENDING":
        return "Your data moves into %s the next time Sidedoor starts." % value["home"]
    return None


def resolve(home=None, legacy=None, keep=None):
    """[Move my data now]: for each conflicting item set the copy `status` names aside (renamed with
    ASIDE and the time, never deleted), then move the rest. Only while nothing runs: the old and the
    new locks must all be free (the dashboard calls it while it holds the port). Returns the new
    `status`, with "error" when it could not run. `keep` ("home" or "code", [Keep this one] / [Keep that
    one]) chooses the copy of state/ to keep; without it, a state/ conflict whose two copies were both
    used after they split is not resolved (HM1)."""
    import run_locks
    if keep not in (None, HOME, CODE_SIDE):
        raise ValueError("keep must be 'home' or 'code'")
    home = data(None, home)
    legacy = legacy_root() if legacy is None else Path(legacy)
    current = status(home, legacy)
    if legacy is None or current["state"] not in (CONFLICT, MOVING, "PENDING", "WAITING"):
        return current
    sides = {}
    for conflict in current["conflicts"]:
        side = keep if keep and conflict["item"] == STATE and conflict["reason"] != FILES_IN_BOTH else conflict["keep_side"]
        if side is None:
            return dict(current, error="Both copies of your database were used after they split. Choose the one "
                                       "to keep with Keep this one or Keep that one.")
        sides[conflict["item"]] = side
    names = (run_locks.RUNNER, run_locks.APPLICATIONS, run_locks.BROWSER)
    # The code folder's state/ may already be the link to the home's: then one set of locks.
    old = home if _same(legacy / STATE, home / STATE) else legacy
    with run_locks.held(old, *names) as old_free, run_locks.held(home, *names) if old != home else \
            _held_already() as new_free:
        if not (old_free and new_free):
            return dict(current, error="A job is running. Click Pause on Overview, wait until it has finished, "
                                       "then click Move my data now again.")
        record = _read_marker(home) or {}
        items = _items(record)
        for conflict in current["conflicts"]:
            name, side = conflict["item"], sides[conflict["item"]]
            if conflict["reason"] == FILES_IN_BOTH:
                asides = conflict["files"]
            else:
                asides = [conflict["home"] if side == CODE_SIDE else conflict["code"]]
            for path in asides:
                path = Path(path)
                if path.exists() or path.is_symlink():
                    os.rename(path, _aside_name(path))
            for key in (":reason", ":files"):
                items.pop(name + key, None)
            if conflict["reason"] == FILES_IN_BOTH:
                items[name] = MOVING    # the clashing files are aside: move the rest
            elif side == CODE_SIDE:
                items.pop(name, None)   # the home's copy is aside: a fresh move of the code folder's
            else:
                items[name] = DONE      # the code folder's copy is aside: what is in the home stays
        record.update(items=items, state=MOVING, updated_at=_now())
        _write_json(home / MARKER, record)
    result = migrate(home, legacy, serving=True)
    if result["state"] == "BUSY":
        return dict(status(home, legacy), error=result["text"])
    return status(home, legacy)


class _held_already:
    def __enter__(self):
        return True

    def __exit__(self, *arguments):
        return False


def prepare(home=None, wait_seconds=0, poll_seconds=2, serving=False):
    """At an entry point, before anything reads or writes data: move what the code folder still holds
    into the home, then create state/, reports/, logs/ and backups/ there. Returns the data folder.
    Raises MigrationPending when the move cannot run now (BUSY), and MigrationHeld when state/ is in
    both places, so no work starts beside a database still in the code folder."""
    root = data(None, home)
    result = migrate(root, wait_seconds=wait_seconds, poll_seconds=poll_seconds, serving=serving)
    if result["state"] == "BUSY":
        raise MigrationPending(result["text"])
    if result["state"] not in ("DONE_BEFORE", NONE):
        current = status(root)
        if current["held"]:
            raise MigrationHeld(message(current))
    for name in (STATE, BACKUPS, REPORTS, LOGS):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    return root
