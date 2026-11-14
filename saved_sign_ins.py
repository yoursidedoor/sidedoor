"""Saved sign-ins (E15.T7; R4, R8): the password vault and the site accounts, one store.

What is kept, per site: the sign-in email, how the account was made, when it was made and last used, and
the password. One row per site (R5), keyed exactly as site sessions are (`site_sessions.site_key`: the
page's host, lower-cased, without a leading "www."; no other subdomain is merged).

Where: `<Sidedoor home>/saved-sign-ins.enc`, one file, sealed with `site_sessions.seal` under the ONE random
key credentials.py keeps as "vault" (the same keychain item site sessions use; purpose label
"saved-sign-ins", so neither file opens as the other). Adding a site never adds a keychain entry. The key is
never made here: `site_sessions.before_session` is the one place that makes it, and the key's four states
(the table above `site_sessions._key_state`) mean the same here: never made or unreadable, nothing is
read, written or deleted; readable but the file does not authenticate, the store is empty.

Bounds (owner, 2026-11-10):
- at most MAX_SIGN_INS (200) rows; over that, the one unused for longest goes first, whole;
- a password is removed once its site has had no open job for PRUNE_SECONDS (90 days). The row stays,
  marked (`removed_at`; the page says "Password removed"). `prune(open_sites)` runs with the daily log
  retention: a site with an open job now has its clock restarted, the others run on.
Nothing here reads the license: saved sign-ins are kept when a subscription ends (decision D4).

The password is a secret. It exists in three places only: the sealed file, this module's memory for the
length of one call, and the browser guard's own call that types it into the page
(`browser_guard.Proxy.call_saved_sign_in`). It is never logged, never in a prompt, a tool answer, an
audit row, a database row or an exception message. The one other way out is the user's own Show password
on the Saved sign-ins page (`reveal`), after the computer's own unlock, in that one answer only.
`add` is for a password Sidedoor generated (E15.T8). Sidedoor never reads or keeps what the user types:
when the user signs in themselves, what is saved is the site session (owner's design, 2026-11-10).

Interface (E15.T8 builds on it):
    add(site, email, password, made_by)   save or replace the site's sign-in; "saved" or why not
    has(site)                             True when a password is saved for the site
    listing()                             {"rows": [...], "unavailable": bool}: never a password
    remove(site) / remove_all()           the user's own control; the number of rows removed
    prune(open_sites, now=None)           the 90-day rule; the number of passwords removed
    open_sites(root)                      the sites of the jobs that are open now, or None (unreadable)
    used(site)                            mark the sign-in used now (the guard, after a fill)
    for_fill(site)                        the guard only: (email, password) or None
    pend(site, email, password)           the guard only (E15.T8): a password it generated, kept as PENDING
                                          before the sign-up button is pressed (owner's pick R7 b)
    pending(site)                         True while a generated password waits for its sign-up's outcome
    confirm(site)                         the sign-up was seen to succeed: the pending one becomes the saved
                                          sign-in ("Sidedoor created it")
    drop_pending(site)                    the sign-up was refused, or nothing was typed: forgotten
    reset_pending(site, password)         the guard only: a new password for a reset, only where one is pending
    reveal(site)                          the page's Show password only, after the computer's own unlock
    clean_output(folder, site, audit)     the application job: a session's saved pages hold no value
"""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import log
import portable
import site_sessions


LOG = log.get(__name__)
STORE_FILE = "saved-sign-ins.enc"
LOCK_FILE = "saved-sign-ins.lock"
PURPOSE = b"saved-sign-ins"
MAX_SIGN_INS = 200                 # owner, 2026-11-10
PRUNE_SECONDS = 90 * 86400         # owner, 2026-11-10: no open job on the site for 90 days
LOCK_WAIT_SECONDS = 2
USER, SIDEDOOR = "USER", "SIDEDOOR"          # how the account was made
MADE_BY = (USER, SIDEDOOR)
CONFIG_KEY = "_application_saved_sign_in"    # worker config -> browser_runtime.guard_command (--saved-sign-in)
CREATE_CONFIG_KEY = "_application_create_account"   # the sign-up email -> --create-account (E15.T8)
ACCESS_CONFIG_KEY = "_application_access_step"      # True -> --access-step (E15.T8)
RESET_CONFIG_KEY = "_application_reset_recorded"    # the email of Sidedoor's own recorded sign-up -> --reset-recorded
SAVED, UNAVAILABLE, INVALID = "saved", "unavailable", "invalid"


def _home(home):
    return site_sessions._home(home)


def _site(site):
    return site if isinstance(site, str) and site_sessions.SITE.fullmatch(site) else None


def _empty():
    return {"version": 1, "sites": {}, "pending": {}}


def _read(home, key):
    try:
        plain = site_sessions.unseal(key, (_home(home) / STORE_FILE).read_bytes(), PURPOSE)
        data = json.loads(plain.decode("utf-8")) if plain is not None else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("sites"), dict):
        return _empty()
    data["sites"] = {site: row for site, row in data["sites"].items() if _site(site) and isinstance(row, dict)}
    held = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    data["pending"] = {site: row for site, row in held.items() if _site(site) and isinstance(row, dict)}
    return data


def _write(home, key, data):
    path = _home(home) / STORE_FILE
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(site_sessions.seal(key, json.dumps(data, separators=(",", ":")).encode("utf-8"), PURPOSE))
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _stamp(row, name):
    value = row.get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _bound(data):
    """At most MAX_SIGN_INS rows: the one unused for longest goes first."""
    for sites, stamp in ((data["sites"], "used_at"), (data["pending"], "at")):
        for site in sorted(sites, key=lambda name: _stamp(sites[name], stamp))[:max(0, len(sites) - MAX_SIGN_INS)]:
            del sites[site]


def _change(home, change, create=False):
    """Run `change(data)` on the store under its lock and write it back when it returns True. Returns
    "written", "unchanged", "none" (no store yet and not `create`), UNAVAILABLE (the key cannot be used),
    "busy" or "failed". Never raises, and never puts a value in a log line."""
    try:
        key = site_sessions._key_state(home)[0]
        if key is None:
            return UNAVAILABLE
        if not create and not (_home(home) / STORE_FILE).exists():
            return "none"
        _home(home).mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = portable.open_lock(_home(home) / LOCK_FILE)
        try:
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while not portable.try_lock(lock):
                if time.monotonic() >= deadline:
                    return "busy"
                time.sleep(0.02)
            try:
                data = _read(home, key)
                if not change(data):
                    return "unchanged"
                _bound(data)
                _write(home, key, data)
                return "written"
            finally:
                portable.unlock(lock)
        finally:
            os.close(lock)
    except Exception as error:
        LOG.warning("Saved sign-ins could not be updated (%s).", type(error).__name__)
        return "failed"


def _load(home):
    """(the store's contents, readable). Not readable: the key cannot be used while a store exists."""
    try:
        if not (_home(home) / STORE_FILE).exists():
            return _empty(), True
        key = site_sessions._key_state(home)[0]
        if key is None:
            return _empty(), False
        return _read(home, key), True
    except Exception:
        return _empty(), False


# --- the interface ---------------------------------------------------------------------------------

def add(site, email, password, made_by, home=None, now=None):
    """Save the sign-in for `site` (a `site_sessions.site_key` value), replacing any row it has. `made_by`:
    USER or SIDEDOOR. Returns SAVED, INVALID (nothing written), or why the store could not be written
    (UNAVAILABLE: the vault key was never made or cannot be read; "busy"; "failed"). Never raises."""
    if not (_site(site) and isinstance(email, str) and email.strip() and isinstance(password, str) and password
            and made_by in MADE_BY):
        return INVALID
    now = time.time() if now is None else now

    def change(data):
        previous = data["sites"].get(site) or {}
        data["sites"][site] = {"email": email.strip(), "made_by": made_by, "password": password,
                               "created_at": _stamp(previous, "created_at") or now, "used_at": now,
                               "open_job_at": now, "removed_at": None}
        return True
    done = _change(home, change, create=True)
    return SAVED if done == "written" else done


def has(site, home=None):
    row = _load(home)[0]["sites"].get(site) if _site(site) else None
    return isinstance(row, dict) and isinstance(row.get("password"), str) and bool(row["password"])


def listing(home=None):
    """What the Saved sign-ins page shows: {"rows": [{site, email, made_by, created_at, used_at,
    has_password, removed_at}], "unavailable": bool}, by site. Never a password."""
    data, readable = _load(home)
    rows = []
    for site in sorted(data["sites"]):
        row = data["sites"][site]
        rows.append({"site": site, "email": row.get("email") if isinstance(row.get("email"), str) else "",
                     "made_by": row.get("made_by") if row.get("made_by") in MADE_BY else None,
                     "created_at": _stamp(row, "created_at") or None, "used_at": _stamp(row, "used_at") or None,
                     "has_password": isinstance(row.get("password"), str) and bool(row["password"]),
                     "removed_at": _stamp(row, "removed_at") or None})
    return {"rows": rows, "unavailable": not readable}


def remove(site, home=None):
    """The user's Remove: the site's whole row. Returns the number removed (0 or 1)."""
    if not _site(site):
        return 0
    def change(data):
        data["pending"].pop(site, None)
        return data["sites"].pop(site, None) is not None
    return 1 if _change(home, change) == "written" else 0


def remove_all(home=None):
    """The user's Remove all: every row. Returns the number removed."""
    removed = []

    def change(data):
        removed.append(len(data["sites"]))
        pending, data["sites"], data["pending"] = bool(data["pending"]), {}, {}
        return bool(removed[0]) or pending
    return removed[0] if _change(home, change) == "written" else 0


def prune(open_sites, now=None, home=None):
    """The 90-day rule. `open_sites`: the sites that have an open job now (`open_sites(root)`); None (they
    could not be read) changes nothing. A site with an open job has its clock restarted; a password whose
    site has had none for PRUNE_SECONDS is removed and its row marked. Returns the passwords removed."""
    if open_sites is None:
        return 0
    now = time.time() if now is None else now
    removed = []

    def change(data):
        changed = False
        for site, row in data["sites"].items():
            if site in open_sites:
                row["open_job_at"] = now
                changed = True
            elif isinstance(row.get("password"), str) and now - _stamp(row, "open_job_at") >= PRUNE_SECONDS:
                row["password"], row["removed_at"] = None, now
                removed.append(site)
                changed = True
        return changed
    return len(removed) if _change(home, change) == "written" else 0


def open_sites(root=None):
    """The sites of the application jobs that are open now (not in `applications.FINAL`), read-only.
    None when the database cannot be read (then `prune` removes nothing); empty when there is none yet."""
    import applications
    import paths
    path = paths.database(root)
    if not path.exists():
        return set()
    try:
        database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = database.execute("SELECT payload, state FROM application_jobs").fetchall()
        finally:
            database.close()
    except sqlite3.OperationalError as error:
        return set() if "no such table" in str(error) else None
    except sqlite3.DatabaseError:
        return None
    found = set()
    for payload, state in rows:
        if state in applications.FINAL:
            continue
        try:
            site = site_sessions.site_key(json.loads(payload).get("url"))
        except (ValueError, AttributeError, TypeError):
            site = None
        if site:
            found.add(site)
    return found


def used(site, home=None, now=None):
    """The sign-in was filled just now: it is the most recently used."""
    now = time.time() if now is None else now

    def change(data):
        if not isinstance(data["sites"].get(site), dict):
            return False
        data["sites"][site]["used_at"] = now
        return True
    return _site(site) is not None and _change(home, change) == "written"


def for_fill(site, home=None):
    """The browser guard only: (email, password) for `site`, or None when none is saved (or its password
    was removed, or the store cannot be read). The one function that hands the value out."""
    data = _load(home)[0]
    for row in (data["sites"].get(site), data["pending"].get(site)) if _site(site) else ():
        # The saved sign-in; with none, the one whose sign-up's outcome is still unknown (E15.T8): signing
        # in with it is how Sidedoor learns the account exists, without creating a second one.
        if isinstance(row, dict) and isinstance(row.get("password"), str) and row["password"]:
            return (row.get("email") if isinstance(row.get("email"), str) else ""), row["password"]
    return None


def pend(site, email, password, home=None, now=None):
    """The guard, before it types a password it generated (owner's pick R7 b): kept as pending, so a
    sign-up that goes through is never an account nobody can sign in to. Not a saved sign-in: it is not
    on the page and `has` is False until `confirm`. One per site: a site that has one keeps it (returns
    "exists": nothing new is generated for a site Sidedoor may already have an account on)."""
    if not (_site(site) and isinstance(email, str) and email.strip() and isinstance(password, str) and password):
        return INVALID
    now = time.time() if now is None else now
    done = []

    def change(data):
        if isinstance(data["pending"].get(site), dict) or isinstance((data["sites"].get(site) or {}).get("password"), str):
            done.append("exists")
            return False
        data["pending"][site] = {"email": email.strip(), "password": password, "at": now}
        return True
    result = _change(home, change, create=True)
    return SAVED if result == "written" else done[0] if done else result


def reset_pending(site, password, home=None, now=None, recorded_email=None, restore=None):
    """The guard, before it types a new password on the site's own password-reset form (owner, 2026-11-14).
    The permission is the caller's (`browser_guard.Proxy.call_reset_password`: the application job's stored
    record of an earlier sign-up there, `access_step.own_account`); here only: never for a site that has a
    saved sign-in. The pending password is replaced and marked as a reset not yet seen to work (`reset_at`);
    with none pending, one is made under `recorded_email`. `restore`: the pending row as it was
    (`pending_row`), put back when nothing was typed. Returns SAVED, "none" or why the store could not be
    written."""
    if not (_site(site) and isinstance(password, str) and password):
        return INVALID
    now = time.time() if now is None else now
    done = []

    def change(data):
        held = data["pending"].get(site)
        if isinstance((data["sites"].get(site) or {}).get("password"), str):
            done.append("none")
            return False
        if isinstance(restore, dict):
            data["pending"][site] = dict(restore)
            return True
        if not isinstance(held, dict):
            if not (isinstance(recorded_email, str) and recorded_email.strip()):
                done.append("none")
                return False
            held = data["pending"][site] = {"email": recorded_email.strip(), "at": now}
        held["password"], held["reset_at"] = password, now
        held.pop("site_confirmed", None)   # what the site confirmed was the sign-up's password, not this one
        return True
    result = _change(home, change, create=bool(recorded_email))
    return SAVED if result == "written" else done[0] if done else "none" if result == "none" else result


def pending_row(site, home=None):
    """The guard only: the site's whole pending row (its password included), to put back when a reset typed
    nothing; None when none is pending."""
    row = _load(home)[0]["pending"].get(site) if _site(site) else None
    return dict(row) if isinstance(row, dict) else None


def pending_state(site, home=None):
    """What is known about the site's pending password, never the password: None when none is pending, else
    {"reset": it was typed into a reset form and no sign-in with it is known to have worked,
    "site_confirmed": the site itself confirmed the sign-up it was typed into}."""
    row = _load(home)[0]["pending"].get(site) if _site(site) else None
    if not isinstance(row, dict):
        return None
    return {"reset": bool(row.get("reset_at")), "site_confirmed": row.get("site_confirmed") is True}


def site_confirmed(site, home=None):
    """The site itself confirmed the sign-up the pending password was typed into (it asked for a
    confirmation, or signed in): a later way in may then save it. Not for a reset password."""
    def change(data):
        row = data["pending"].get(site) if _site(site) else None
        if not isinstance(row, dict) or row.get("reset_at") or row.get("site_confirmed") is True:
            return False
        row["site_confirmed"] = True
        return True
    return _change(home, change) == "written"


def pending(site, home=None):
    return bool(_site(site)) and isinstance(_load(home)[0]["pending"].get(site), dict)


def confirm(site, home=None, now=None):
    """The sign-up was seen to succeed: the pending password becomes the site's saved sign-in, made by
    Sidedoor. True when it did."""
    now = time.time() if now is None else now

    def change(data):
        held = data["pending"].pop(site, None) if _site(site) else None
        if not isinstance(held, dict) or not isinstance(held.get("password"), str):
            return False
        previous = data["sites"].get(site) or {}
        data["sites"][site] = {"email": held.get("email") or "", "made_by": SIDEDOOR, "password": held["password"],
                               "created_at": _stamp(previous, "created_at") or _stamp(held, "at") or now, "used_at": now,
                               "open_job_at": now, "removed_at": None}
        return True
    return _change(home, change) == "written"


def drop_pending(site, home=None):
    return bool(_site(site)) and _change(home, lambda data: data["pending"].pop(site, None) is not None) == "written"


FILL_OUTCOMES = ("filled", "failed", "unconfirmed")   # the guard's audit outcomes after which a value may be in the page


def clean_output(folder, site, audit=None, home=None):
    """The application job, when a session that held the fill tool for `site` has ended (the guard does the
    same while it runs and when it stops; this is the job's own pass, for a guard that was stopped hard).
    Every text file in the session's browser output folder loses the value; files that are not text
    (screenshots) written since the first fill the guard's `audit` records are deleted. Returns (files
    rewritten, files deleted). Never raises, and the value never leaves this call."""
    try:
        found = for_fill(site, home)
        if found is None:
            return 0, 0
        import browser_guard
        since = None
        try:
            for line in Path(audit).read_text(encoding="utf-8").splitlines() if audit else ():
                row = json.loads(line)
                if isinstance(row, dict) and row.get("decision") in ("SAVED_SIGN_IN", "ACCOUNT_PASSWORD", "RESET_PASSWORD") and row.get("outcome") in FILL_OUTCOMES \
                        and isinstance(row.get("began"), (int, float)):
                    since = row["began"]
                    break
        except (OSError, ValueError):
            since = 0                           # the audit cannot be read: every file that is not text goes
        return browser_guard.clean_folder(folder, browser_guard.secret_forms(found[1]), since)
    except Exception as error:
        LOG.warning("A session's saved pages could not be cleaned (%s).", type(error).__name__)
        return 0, 0


def reveal(site, home=None):
    """The Saved sign-ins page only, and only after the computer's own unlock succeeded
    (`device_unlock.verify`): the site's password, or None. Besides `for_fill`, nothing else hands it out."""
    found = for_fill(site, home)
    return found[1] if found else None


def key_removed(home=None):
    """The vault key was deleted on purpose (`site_sessions.key_removed`): what it sealed cannot be opened
    again, so the file goes with it."""
    (_home(home) / STORE_FILE).unlink(missing_ok=True)
