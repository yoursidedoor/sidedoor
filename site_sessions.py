"""Site sessions (E15.T4; R5, R6): a site's cookies, kept across Chrome exits, so a sign-in or a cleared
human check done in one browser session is still there in the next (the read and the submit are separate
sessions, and Chrome drops session cookies when it exits).

What is kept, per site: the cookies Chrome holds for that site (session cookies included), nothing else.
The page's URL and the context's cookies; no localStorage or sessionStorage.

The key (`site_key`): the page's host, lower-cased, without a leading "www.". No public-suffix list is
bundled, so it is the host and never a guessed "registrable domain":
- boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com are one site each, whatever the employer in the
  path (their cookies and a check cleared there belong to the host, not to an employer);
- acme.wd5.myworkdayjobs.com is its own site (each Workday tenant has its own accounts);
- careers.example.com and apply.example.com are two sites: a sign-in on one is not restored for the
  other (a missed reuse, never a wrong one).
An entry holds every cookie Chrome would send to that host: the host's own and its parent domains'.

Where: `<Sidedoor home>/site-sessions.enc`, one file, encrypted and authenticated under one random
256-bit key kept by credentials.py as the name "vault" (the OS keychain / Credential Manager; a 0600 file
where there is no OS store). E15.T7's password vault is meant to use the same key and `seal` / `unseal`.
The desktop app has no encryption library, so the cipher is built from the standard library: a SHAKE-256
keystream (key, fresh 16-byte nonce) XORed over the JSON, then HMAC-SHA256 over nonce and ciphertext
(encrypt-then-MAC, separate derived keys; the purpose label "site-sessions" is part of both keys and of
the MAC, so a file sealed for another purpose under the same key does not open here). The key is made in
one place only, `before_session` (called by the application job and by the contact finder before their
sessions, `linkedin_contacts._run_one`), and only when none was ever made; the guard and
`save` never make one. The four states (the table above `_key_state`): never made: nothing is kept until
`before_session` makes it; should exist but is missing or cannot be read (a locked keychain, a removed
item): unavailable, and nothing is made, replaced, read or deleted; readable but the file does not
authenticate (corrupt, an older format): empty, replaced by the next save, not kept aside; both fine: in
use. At most MAX_SITES (200) sites are kept, least recently used first out.

Cookie values are secrets. They exist in three places only: Chrome, this module's memory for the length
of one call, and the encrypted file. They are never logged, never put in a prompt, a database row, a
request or result file, the guard's audit, or an exception message. The model has no tool that reads this.

Two processes use the store. The browser guard (the one process that talks to the running Chrome) restores
at the start of a session and captures while it runs; the application job decides what a capture means:

    guard, after the client's `initialized`, before any page work:   restore(site, call)
    guard, before a forwarded call (at most every CAPTURE_SECONDS)
           and once more when the session ends:                      save(site, call, pending=True)
    application job, before a session is paid for:                   before_session(site) -> wall code or None
    application job, when the session's result is known:             settle(site, signed_in, sign_in_again)

`call(code)` runs Playwright code in the session's Chrome and answers its list result (the guard's own
browser_run_code_unsafe call, the channel the window placement uses), or None.

Interface for later tasks:
    site_key(url)                         the key for a page URL, or None
    save(site, call, wall=None, job=None, pending=False)     from the running Chrome into the store
    restore(site, call, job=None)         from the store into the running Chrome (absent cookies only);
                                          {outcome: none | restored | failed | unavailable, counts}
    has(site, job=None)                   True when a saved session exists
    discard(site, job=None)               forget it
    discard_job(job)                      forget every job-scoped session of that job (E16.T2: "discarded
                                          when the application is done"; nothing calls it yet)
    prune(now=None)                       forget sessions not saved or restored for PRUNE_SECONDS (90 days)
    check(site, job=None)                 "none", "live" or "dead" (see below); a dead one is discarded
    user_step_ended(site, call, cleared=None, job=None)
                                          E15.T5 / E16.T2: the user's step ended signed in, or with a check
                                          cleared (`cleared`: the wall code it resolved, e.g.
                                          "CAPTCHA_REQUIRED"); saved at once, not pending
`job` scopes an entry to one job (E16.T2); None is the site's shared session (R5). A restore with a job
restores the site's session and that job's.

Signed in (`settle`): a session's capture is kept when its result was accepted and is not an access wall
(access_outcomes.WALL_REASONS). No page text is read. Only the sign-in wall (SIGN_IN_AGAIN,
AUTHENTICATION_REQUIRED: the site asks for the sign-in itself again) forgets the saved session. A human
check (CAPTCHA_REQUIRED) or a second-step code (MFA_REQUIRED: the site challenges a session it otherwise
knows) keeps it, so the user is not made to sign in twice (R6); any other failure drops only the capture.

Restore never overwrites: a saved cookie is added only when Chrome holds no cookie of that name, domain
and path (the jar is read and filtered inside the same call); Chrome's own value always wins. Saved
cookies whose expiry has passed are not sent, and neither is a cookie Chrome would not send to the site
(its domain must be the site's host or a parent domain of it, never a single label). Restore never navigates, clicks or submits.

The check before a model session (`check`, `before_session`): no generic, reliable way to ask a site "am I
still signed in" exists without site rules, so this is the weakest honest version. A saved session is
"dead" when nothing in it could still be sent: it holds no session cookie and every persistent cookie's
expiry has passed. It cannot detect a session the site ended early (sign-out elsewhere, server-side
timeout, a revoked or rotated cookie) or the lifetime of a session cookie; those are found by the session
itself, which then ends on the sign-in wall and discards the entry (`settle`). A dead session is discarded; when
it was saved by `user_step_ended` with a wall code (the site is known to wall), `before_session` returns
that code and the job takes the wall path without a model session.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import uuid
from pathlib import Path

import credentials
import log
import portable


LOG = log.get(__name__)
STORE_FILE = "site-sessions.enc"
LOCK_FILE = "site-sessions.lock"
KEY_NAME = "vault"                 # credentials.NAMES: one key for this store and E15.T7's vault
MAGIC = b"SDV2"                    # the sealed format; anything else (SDV1 files too) is unreadable, so empty
PURPOSE = b"site-sessions"         # bound into the keys and the MAC: another file under the same key is not this one
KEY_MADE_FILE = "vault-key-made"   # written when the key is first made: from then on a missing key is never re-made
MAX_SITES = 200                    # the design doc's bound for saved sign-ins; the least recently used goes first
SIGN_IN_AGAIN = "AUTHENTICATION_REQUIRED"   # the one wall that says the saved sign-in is no longer accepted
PRUNE_SECONDS = 90 * 86400         # owner, 2026-11-10: "pruned on the same 90 days"
CAPTURE_SECONDS = 15               # the guard's captures while a session runs
LOCK_WAIT_SECONDS = 2
CONFIG_KEY = "_application_site_session"   # worker config -> browser_runtime.guard_command (--site-session)
# "the read, the submit", and the reads that confirm a sent application (verify, reconcile; owner's pick
# S7 a, 2026-11-13): after the user's step on a confirming read's card, that read is tried again with the
# session the step saved.
OPERATIONS = ("inspect", "submit", "verify", "reconcile")
# The reads that confirm a sent application always really open the site: they restore a saved session
# when there is one, but are never answered from `before_session`'s dead-session shortcut (a wall
# code with no session). A card is raised only when that read itself met the wall.
ALWAYS_OPEN = ("verify", "reconcile")
EXPIRED_ACTION = "The saved sign-in for this site has expired; sign in on the site again"
SITE = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
JOB = re.compile(r"[^|]{1,300}")
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey")


# --- the key of a page -----------------------------------------------------------------------------

def site_key(url):
    """The site of a page URL: its host, lower-cased, without a leading "www."; None when it has none."""
    try:
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = host[4:] if host.startswith("www.") else host
    return host if host and SITE.fullmatch(host) else None


def _bare(domain):
    domain = str(domain or "").lower().lstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def sent_to(cookie, site):
    """True when Chrome would send `cookie` to `site`: the cookie's domain is the site or a parent of it."""
    domain = _bare(cookie.get("domain") if isinstance(cookie, dict) else None)
    if not domain or not isinstance(site, str):
        return False
    # The host itself, or a parent domain of it; never a single label ("com"). A multi-label public
    # suffix ("co.uk") is Chrome's own to refuse: no public-suffix list is bundled here.
    return site == domain or ("." in domain and site.endswith("." + domain))


def _entry_key(site, job=None):
    if not isinstance(site, str) or not SITE.fullmatch(site):
        return None
    if job is None:
        return site
    return site + "|" + job if isinstance(job, str) and JOB.fullmatch(job) else None


# --- sealing (shared with E15.T7) ------------------------------------------------------------------

def _derived(key, label, purpose):
    return hmac.new(key, b"sidedoor-vault|" + purpose + b"|" + label, hashlib.sha256).digest()


def _xor(data, stream):
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def seal(key, plain, purpose=PURPOSE):
    """Encrypt and authenticate `plain` (bytes) under the 32-byte `key`, for `purpose` (a label: data
    sealed for one purpose does not open as another's)."""
    nonce = os.urandom(16)
    body = _xor(plain, hashlib.shake_256(_derived(key, b"enc", purpose) + nonce).digest(len(plain)))
    tag = hmac.new(_derived(key, b"mac", purpose), MAGIC + purpose + b"|" + nonce + body, hashlib.sha256).digest()
    return MAGIC + nonce + body + tag


def unseal(key, blob, purpose=PURPOSE):
    """The plain bytes, or None when `blob` is not this key's untampered data sealed for `purpose`."""
    if not isinstance(blob, bytes) or len(blob) < len(MAGIC) + 48 or not blob.startswith(MAGIC):
        return None
    nonce, body, tag = blob[4:20], blob[20:-32], blob[-32:]
    expected = hmac.new(_derived(key, b"mac", purpose), MAGIC + purpose + b"|" + nonce + body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return None
    return _xor(body, hashlib.shake_256(_derived(key, b"enc", purpose) + nonce).digest(len(body)))


# The key, and what each state means (X3; the same rules are E15.T7's to keep):
#   never made (no key, no KEY_MADE_FILE, no store file)   only `before_session` makes one; everyone else does nothing
#   should exist but reads "not found", or cannot be read  unavailable: nothing is made, replaced, read or deleted
#   readable, the store does not authenticate              the sessions store is empty and the next save replaces it
#   readable, the store authenticates                      in use
OK, NEVER_MADE, KEY_MISSING, KEY_UNREADABLE = "ok", "no key yet", "key missing", "key unreadable"
_reported = set()


def _key_state(home, make=False):
    """(key or None, state). `make`: the one place a key may be made (`before_session`, in the
    application job, never the guard), and only when none was ever made."""
    try:
        value = credentials.get(KEY_NAME, home)
    except (OSError, ValueError):
        return None, KEY_UNREADABLE
    if value is not None:
        valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        return (bytes.fromhex(value), OK) if valid else (None, KEY_UNREADABLE)
    try:
        made = (_home(home) / KEY_MADE_FILE).exists() or (_home(home) / STORE_FILE).exists()
    except OSError:
        return None, KEY_UNREADABLE
    if made:
        return None, KEY_MISSING
    if not make:
        return None, NEVER_MADE
    try:
        value = secrets.token_hex(32)
        credentials.put(KEY_NAME, value, home)
        import setup_steps
        setup_steps._write_private(_home(home) / KEY_MADE_FILE, "1\n")
        return bytes.fromhex(value), OK
    except (OSError, ValueError):
        return None, KEY_UNREADABLE


def _unavailable(state):
    """Record, once per process and state, why saved site sessions are not in use."""
    if state in (KEY_MISSING, KEY_UNREADABLE) and state not in _reported:
        _reported.add(state)
        LOG.warning("Saved site sessions are not in use (%s); the stored file is left as it is.", state)


def key_removed(home=None):
    """The vault key was deleted on purpose (credentials.delete: Settings > "Remove Sidedoor's saved keys"):
    the store sealed under it and the "key made" marker go too, so the next `before_session` makes a fresh
    key and store. Never called for a key that merely cannot be read (that stays unavailable)."""
    for name in (STORE_FILE, KEY_MADE_FILE):
        (_home(home) / name).unlink(missing_ok=True)
    _reported.clear()
    import saved_sign_ins   # E15.T7: the saved sign-ins were sealed under the same key
    saved_sign_ins.key_removed(home)


def _key(home):
    return _key_state(home)[0]


# --- the file --------------------------------------------------------------------------------------

def _home(home):
    return credentials._home(home)


def _empty():
    return {"version": 1, "sites": {}, "pending": {}}


def _read(home, key):
    try:
        plain = unseal(key, (_home(home) / STORE_FILE).read_bytes())
        data = json.loads(plain.decode("utf-8")) if plain is not None else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("sites"), dict) or not isinstance(data.get("pending"), dict):
        return _empty()
    return data


def _write(home, key, data):
    path = _home(home) / STORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(seal(key, json.dumps(data, separators=(",", ":")).encode("utf-8")))
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def _change(home, change, create=False, make_key=False):
    """Run `change(data)` on the store under its lock and write it back when it returns True. Returns
    "written", "unchanged", or why not: "none" (no store yet and not `create`), a key state
    (NEVER_MADE, KEY_MISSING, KEY_UNREADABLE), "busy" or "failed". Never raises."""
    try:
        key, state = _key_state(home, make_key)
        if key is None:
            _unavailable(state)
            return state
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
    except Exception as error:   # a saved session is a convenience: it never stops a browser session
        LOG.warning("Saved site sessions could not be updated (%s).", type(error).__name__)
        return "failed"


def _bound(data):
    """At most MAX_SITES saved sessions and as many captures: the least recently used go first."""
    for table, stamp in ((data["sites"], "used_at"), (data["pending"], "at")):
        if len(table) > MAX_SITES:
            def age(name):
                at = table[name].get(stamp) if isinstance(table[name], dict) else None
                return at if isinstance(at, (int, float)) else 0
            for name in sorted(table, key=age)[:len(table) - MAX_SITES]:
                del table[name]


def _load(home):
    """The store's contents (a copy to read from); empty when there is none or it cannot be read."""
    try:
        if not (_home(home) / STORE_FILE).exists():
            return _empty()
        key, state = _key_state(home)
        if key is None:
            _unavailable(state)
            return _empty()
        return _read(home, key)
    except Exception:
        return _empty()


# --- the running Chrome ----------------------------------------------------------------------------

def capture_code(site):
    """Playwright code answering [{url, cookies}]: the current page's URL and the cookies Chrome holds for
    `site` and for the current page's own host."""
    return ("async (page) => {\n"
            "  const input = %s;\n"
            "  const bare = (d) => String(d || '').toLowerCase().replace(/^\\./, '').replace(/^www\\./, '');\n"
            "  const hosts = [input.site];\n"
            "  try { const u = new URL(page.url()); if (u.protocol === 'http:' || u.protocol === 'https:') hosts.push(bare(u.hostname)); }\n"
            "  catch (e) {}\n"
            "  const sent = (c) => { const d = bare(c.domain); return !!d && hosts.some((h) => h === d || h.endsWith('.' + d)); };\n"
            "  return [{ url: page.url(), cookies: (await page.context().cookies()).filter(sent) }];\n"
            "}" % json.dumps({"sidedoor": "site-session-capture", "site": site}))


def restore_code(cookies):
    """Playwright code that adds each of `cookies` Chrome does not already hold (same name, domain and
    path: Chrome's own value wins) and answers [{restored, kept, failed}]: counts only, never a value.
    No navigation."""
    return ("async (page) => {\n"
            "  const input = %s;\n"
            "  const done = { restored: 0, kept: 0, failed: 0 };\n"
            "  const id = (c) => c.name + '\\n' + String(c.domain || '').replace(/^\\./, '') + '\\n' + (c.path || '/');\n"
            "  const held = new Set((await page.context().cookies()).map(id));\n"
            "  const absent = input.cookies.filter((c) => !held.has(id(c)));\n"
            "  done.kept = input.cookies.length - absent.length;\n"
            "  try { if (absent.length) await page.context().addCookies(absent); done.restored = absent.length; }\n"
            "  catch (e) {\n"
            "    for (const c of absent) {\n"
            "      try { await page.context().addCookies([c]); done.restored += 1; } catch (one) { done.failed += 1; }\n"
            "    }\n"
            "  }\n"
            "  return [done];\n"
            "}" % json.dumps({"sidedoor": "site-session-restore", "cookies": cookies}))


def _cookie(value):
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not isinstance(value.get("value"), str) \
            or not isinstance(value.get("domain"), str):
        return None
    return {key: value[key] for key in COOKIE_KEYS if key in value}


def _expired(cookie, now):
    expires = cookie.get("expires")
    return isinstance(expires, (int, float)) and not isinstance(expires, bool) and 0 < expires <= now


def _first(answer):
    return answer[0] if isinstance(answer, list) and answer and isinstance(answer[0], dict) else None


# --- the interface ---------------------------------------------------------------------------------

def _same(one, other):
    """True when two {entry key: entry} captures hold the same cookies (their times aside)."""
    def cookies(sites):
        return {name: (entry.get("cookies"), entry.get("wall")) for name, entry in sites.items() if isinstance(entry, dict)}
    return isinstance(one, dict) and isinstance(other, dict) and cookies(one) == cookies(other)


def save(site, call, wall=None, job=None, pending=False, home=None, now=None):
    """Read `site`'s cookies from the running Chrome and keep them: for `site`, and for the site of the
    page the browser is on when that is another one. `pending`: held as this session's capture until
    `settle` decides. Returns the number of sites kept (0: nothing to keep, or the store is not
    available; the store is only rewritten when a capture differs from the last). Never makes the key
    (`before_session` does). Never raises."""
    try:
        key = _entry_key(site, job)
        if key is None:
            return 0
        if _key_state(home)[0] is None:      # before Chrome is asked: no key, nothing to do
            _unavailable(_key_state(home)[1])
            return 0
        state = _first(call(capture_code(site)))
        if state is None:
            return 0
        now = time.time() if now is None else now
        cookies = [cookie for cookie in map(_cookie, state.get("cookies") if isinstance(state.get("cookies"), list) else [])
                   if cookie is not None and not _expired(cookie, now)]
        found = {}
        for name in dict.fromkeys([site, site_key(state.get("url")) or site]):
            mine = [cookie for cookie in cookies if sent_to(cookie, name)]
            if mine:
                found[_entry_key(name, job)] = {"cookies": mine, "saved_at": now, "used_at": now,
                                                "wall": wall if isinstance(wall, str) and wall else None}
        if not found:
            return 0

        def change(data):
            if pending:
                previous = data["pending"].get(key)
                if isinstance(previous, dict) and _same(previous.get("sites"), found):
                    return False
                data["pending"][key] = {"at": now, "sites": found}
            else:
                data["sites"].update(found)
                data["pending"].pop(key, None)
            return True
        return len(found) if _change(home, change, create=True) in ("written", "unchanged") else 0
    except Exception as error:
        LOG.warning("A site session could not be saved (%s).", type(error).__name__)
        return 0


def from_devtools(cookie):
    """A cookie as Chrome's DevTools gives it (Storage.getCookies), in the form the guard's captures hold
    (Playwright's `context.cookies()`, which is made from the same answer): no `sameSite` is "Lax", and a
    partition key is its top-level site. None when it is not a cookie."""
    if not isinstance(cookie, dict):
        return None
    made = {key: cookie[key] for key in COOKIE_KEYS if key in cookie and key not in ("sameSite", "partitionKey")}
    made["sameSite"] = cookie["sameSite"] if isinstance(cookie.get("sameSite"), str) else "Lax"
    partition = cookie.get("partitionKey")
    if isinstance(partition, dict) and isinstance(partition.get("topLevelSite"), str):
        made["partitionKey"] = partition["topLevelSite"]
    elif isinstance(partition, str):
        made["partitionKey"] = partition
    return _cookie(made)


def save_last(site, read, home=None, now=None):
    """The session's last capture, taken by the process that started the session just before it asks
    Chrome to close (browser_runtime.close_cleanly): the guard's own last capture never runs when its
    engine ends it first, so a sign-in made after the guard's last capture (they are CAPTURE_SECONDS
    apart) was kept nowhere. `read()` answers Chrome's cookies as DevTools gives them, and is only
    called when the store's key can be read. Kept as this session's capture exactly as `save(pending=True)`
    keeps one, for `site` and for every other site the session's capture already holds; `settle` decides
    as before. Returns the number of sites kept (0: nothing to keep), or a word for why nothing could
    be (a key state, "busy", "failed"). Never makes the key. Never raises."""
    try:
        key = _entry_key(site)
        if key is None:
            return 0
        state = _key_state(home)
        if state[0] is None:
            _unavailable(state[1])
            return state[1]
        now = time.time() if now is None else now
        answered = read()
        cookies = [cookie for cookie in map(from_devtools, answered if isinstance(answered, list) else [])
                   if cookie is not None and not _expired(cookie, now)]
        del answered
        kept = {}

        def change(data):
            previous = data["pending"].get(key)
            before = previous.get("sites") if isinstance(previous, dict) and isinstance(previous.get("sites"), dict) else {}
            found = {}
            for name in dict.fromkeys([key, *before]):
                mine = [cookie for cookie in cookies if sent_to(cookie, name)]
                if mine:
                    found[name] = {"cookies": mine, "saved_at": now, "used_at": now, "wall": None}
            kept["sites"] = len(found)
            if not found or _same(before, found):
                return False
            data["pending"][key] = {"at": now, "sites": found}
            return True
        done = _change(home, change, create=True)
        return kept.get("sites", 0) if done in ("written", "unchanged") else done
    except Exception as error:
        LOG.warning("A site session could not be saved (%s).", type(error).__name__)
        return "failed"


def restore(site, call, job=None, home=None, now=None):
    """Put `site`'s saved cookies back into the running Chrome (only those it does not hold, and only
    cookies Chrome would send to `site`) and mark the session used. Returns {outcome, restored, kept,
    failed}: outcome "none" (nothing saved), "restored", "failed" (the browser did not answer, or an
    error) or "unavailable" (the key cannot be read). Counts only. Never raises."""
    answer = {"outcome": "none", "restored": 0, "kept": 0, "failed": 0}
    try:
        keys = [key for key in dict.fromkeys([_entry_key(site), _entry_key(site, job)]) if key]
        if not keys:
            return answer
        if (_home(home) / STORE_FILE).exists() and _key_state(home)[0] is None:
            _unavailable(_key_state(home)[1])
            return dict(answer, outcome="unavailable")
        now = time.time() if now is None else now
        sites = _load(home)["sites"]
        cookies, seen = [], set()
        for key in reversed(keys):          # the job's own session first, then the site's
            entry = sites.get(key)
            for cookie in (entry.get("cookies") if isinstance(entry, dict) else None) or []:
                cookie = _cookie(cookie)
                identity = cookie and (cookie["name"], cookie["domain"].lstrip("."), cookie.get("path") or "/")
                if cookie is None or identity in seen or _expired(cookie, now) or not sent_to(cookie, site):
                    continue
                seen.add(identity)
                cookies.append(cookie)
        if not cookies:
            return answer
        done = _first(call(restore_code(cookies)))
        if done is None:
            return dict(answer, outcome="failed", failed=len(cookies))

        def change(data):
            touched = False
            for key in keys:
                if isinstance(data["sites"].get(key), dict):
                    data["sites"][key]["used_at"] = now
                    touched = True
            return touched
        _change(home, change)
        return dict({name: int(done.get(name) or 0) for name in ("restored", "kept", "failed")}, outcome="restored")
    except Exception as error:
        LOG.warning("A site session could not be restored (%s).", type(error).__name__)
        return dict(answer, outcome="failed")


def has(site, job=None, home=None):
    key = _entry_key(site, job)
    return bool(key) and isinstance(_load(home)["sites"].get(key), dict)


def discard(site, job=None, home=None):
    """Forget the saved session (and any capture waiting on `settle`). True when something was removed."""
    key = _entry_key(site, job)
    if key is None:
        return False

    def change(data):
        return (data["sites"].pop(key, None) is not None) | (data["pending"].pop(key, None) is not None)
    return _change(home, change) == "written"


def discard_job(job, home=None):
    """E16.T2's seam: forget every session saved against `job`. True when something was removed."""
    if not isinstance(job, str) or not JOB.fullmatch(job):
        return False

    def change(data):
        removed = False
        for table in (data["sites"], data["pending"]):
            for key in [key for key in table if key.endswith("|" + job)]:
                del table[key]
                removed = True
        return removed
    return _change(home, change) == "written"


def prune(now=None, home=None):
    """Forget every session neither saved nor restored for PRUNE_SECONDS, and captures as old. Returns
    the number removed. Runs with the daily log retention (log_retention.startup)."""
    cutoff = (time.time() if now is None else now) - PRUNE_SECONDS
    removed = []

    def change(data):
        for table, stamp in ((data["sites"], "used_at"), (data["pending"], "at")):
            for key, entry in list(table.items()):
                at = entry.get(stamp) if isinstance(entry, dict) else None
                if not isinstance(at, (int, float)) or at < cutoff:
                    del table[key]
                    removed.append(key)
        return bool(removed)
    return len(removed) if _change(home, change) == "written" else 0


def check(site, job=None, home=None, now=None):
    """("none" | "live" | "dead", wall code or None) for the saved session, without a browser or a model
    (the module docstring says what "dead" can and cannot see). A dead session is discarded."""
    key = _entry_key(site, job)
    entry = _load(home)["sites"].get(key) if key else None
    if not isinstance(entry, dict):
        return "none", None
    now = time.time() if now is None else now
    cookies = [cookie for cookie in map(_cookie, entry.get("cookies") or []) if cookie is not None]
    if any(not _expired(cookie, now) for cookie in cookies):
        return "live", None
    discard(site, job, home)
    wall = entry.get("wall")
    return "dead", wall if isinstance(wall, str) and wall else None


def before_session(site, job=None, home=None, now=None):
    """Before a model session on `site` is paid for: make the store's key if none was ever made (the
    only place that does; the guard and `save` never do), drop any capture a crashed session left, and
    run `check`. Returns the wall code when a
    session saved from a cleared wall is dead (the caller takes the wall path, no model session), else
    None. Never raises."""
    try:
        key = _entry_key(site, job)
        if key is None:
            return None
        # The ONE place the store's key is made, and only when none was ever made (X3).
        _change(home, lambda data: data["pending"].pop(key, None) is not None, make_key=True)
        state, wall = check(site, job, home, now)
        if state == "dead":
            LOG.info("The saved session for %s has expired; it was removed.", site)
        return wall if state == "dead" else None
    except Exception as error:
        LOG.warning("A saved site session could not be checked (%s).", type(error).__name__)
        return None


def settle(site, signed_in, sign_in_again=False, job=None, home=None, wall=None, own_only=False, proceed=None):
    """The session on `site` has ended. `signed_in` (its result was accepted and is not an access wall):
    its capture becomes the saved session. `sign_in_again` (it ended on the sign-in wall, SIGN_IN_AGAIN):
    the capture and the saved session are both forgotten, since the site no longer accepts it. Any
    other ending (a human check, a second-step code, a failure) drops only the capture and keeps the
    saved session. `wall` (a user's step that got past it, E15 join): the wall code the kept session
    resolved, the marker `save(wall=...)` writes, so its expiry is handled the same way
    (`before_session`). `own_only` (a user's step): only `site`'s own entry is kept, never the entry
    of another site the browser was on when the session ended. `proceed` (optional, a callable): asked
    under the store's lock just before anything is changed; false means the caller gave up waiting,
    and nothing is written. Returns "saved" or "discarded" only when the store was really written
    (D12), else "dropped". Never raises."""
    key = _entry_key(site, job)
    if key is None:
        return "dropped"
    outcome = []

    def change(data):
        if proceed is not None and not proceed():
            return False
        capture = data["pending"].pop(key, None)
        if signed_in and not sign_in_again and isinstance(capture, dict) and isinstance(capture.get("sites"), dict):
            kept = False
            for name, entry in capture["sites"].items():
                if own_only and name != key:
                    continue
                kept = True
                previous = data["sites"].get(name)
                if isinstance(wall, str) and wall and isinstance(entry, dict):
                    entry["wall"] = wall               # the user's step got past this wall
                elif isinstance(previous, dict) and previous.get("wall") and isinstance(entry, dict):
                    entry["wall"] = previous["wall"]   # the site is still one known to wall
                data["sites"][name] = entry
            if kept:
                outcome.append("saved")
            return True
        if sign_in_again and data["sites"].pop(key, None) is not None:
            outcome.append("discarded")
            return True
        return capture is not None
    written = _change(home, change) == "written"
    return outcome[0] if outcome and written else "dropped"


def user_step_ended(site, call, cleared=None, job=None, home=None, now=None):
    """E15.T5 / E16.T2 call this when the user's step ends signed in or with a check cleared (`cleared`:
    the wall code it resolved). The running Chrome's cookies for the site are saved at once."""
    return save(site, call, wall=cleared, job=job, home=home, now=now)
