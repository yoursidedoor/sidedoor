"""Credentials in the OS store (E2.T4, option B; owner, 2026-10-22: K1 B, K2 delete after a
verified move, K4 keep the 0600 file when there is no store, K8 each job reads the store).

The two secrets every user has, the OpenRouter key and the license key, live in the OS store:
- macOS: generic passwords in the login keychain through `/usr/bin/security` (service
  `Sidedoor`, account `<install id>:<name>`). A write goes through `security -i` with the command
  on stdin and the value hex-encoded (`-X`), so the value is never on argv. The item trusts
  `/usr/bin/security` (Apple-signed, stable path), so no per-app access prompt appears.
- Windows: Credential Manager (`CredWriteW` / `CredReadW` / `CredDeleteW`, ctypes, generic
  credential `Sidedoor/<install id>/<name>`, persisted on the local machine).
- No store (Linux for now, or `/usr/bin/security` missing): the 0600 file (K4).

What this protects: the key is out of the home folder (backups, synced or copied folders, a home
sent to support). It does not protect against software running as the same user: any such
process can run the same `security` command, or call CredReadW, and read the item, which is the
same exposure the 0600 file has (A10).

Never a prompt (KR5, A1): on a locked keychain `security` puts up the SecurityAgent unlock dialog
and blocks. So before every `security` call the keychain's lock state is read in-process with
`SecKeychainGetStatus` (Security.framework through ctypes, which never shows UI); a locked or
unreadable state raises `Locked` / `Unavailable` at once and no `security` process is started.
Reads are cached for `CACHE_SECONDS` (values, "not found" and a locked state alike), so a page
render or a scheduler tick costs no `security` call and no lock check inside that window.

Waiting (R3, KR4): any store error (`Unavailable`, of which `Locked` is one) with no file to fall
back on is a wait: `hold_reason` names it, the scheduler starts nothing, the dashboard shows a
banner and the key UI says so in place of the form, doctor says "waiting for the keychain". No
step reopens, no paste is offered, no failure is recorded, and a save is refused (`put` raises).

Which value wins (KR3, A3): the store's value always wins over a file. A file is only ever
written when the store has just been read as holding nothing for that key (no store at all; or a
store write that failed or read back wrong while the item is confirmed absent), and `put`
removes a stale file before it writes the store, so a crash can never leave an older file to be
moved back over a newer stored key. `migrate` copies a file into the store only when the store
has no value for it; a file next to a different stored value is left alone and reported.

The install id (R4, A6, A7) is a random 32-hex id kept in the home (`credentials-id`, JSON with
the home's real path). A home copied or moved to another path keeps reading its items under the
recorded id; at the next start `migrate` gives it a fresh id, copies the values across (no
re-entry), and deletes the old id's items only when the recorded path no longer exists (a move),
so a copy and its original never share items afterwards. An id that is not 32 hex characters is
refused (Unavailable), never put in a `security -i` line.

Tests never reach a real store (C1): `tests/profile_home.py` installs `NO_STORE` through
`use_backend` (environment changes cannot undo it), and the real backends refuse while unittest
is loaded unless SIDEDOOR_ALLOW_REAL_KEYCHAIN_IN_TESTS=1. `SIDEDOOR_CREDENTIAL_STORE=file` turns
the store off for child processes.

Rolling back to a build older than E2.T4 after the move finds no files and asks for both keys
once (K3; accepted for the beta). `openrouter.key.tested` stays a file: it holds the key's
SHA-256, which still matches the key in the store. `dashboard-token`, `linkedin-sender.json` and
`unipile.json` stay files (K5-K7).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


NAMES = {"openrouter": "openrouter.key", "license": "license.key", "openai": "openai.key"}   # openai: E0.T8
SERVICE = "Sidedoor"
ID_FILE = "credentials-id"
LOCK_FILE = "credentials.lock"
STORE_VARIABLE = "SIDEDOOR_CREDENTIAL_STORE"   # "file": no OS store (child processes of the tests)
ALLOW_IN_TESTS_VARIABLE = "SIDEDOOR_ALLOW_REAL_KEYCHAIN_IN_TESTS"
TIMEOUT_SECONDS = 5
CACHE_SECONDS = 30
LOCK_WAIT_SECONDS = 5
SECURITY = "/usr/bin/security"
SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
UNLOCKED_STATUS = 1   # kSecUnlockStateStatus
# `security` exits with the low byte of the OSStatus: -25300 errSecItemNotFound -> 44.
NOT_FOUND_EXIT = 44
LOCKED_EXITS = {36, 51, 128}   # interaction not allowed, auth failed, user cancelled
INSTALL_ID = re.compile(r"[0-9a-f]{32}")

LOCKED_TEXT = {
    "darwin": "Keychain locked: unlock your Mac's keychain and Sidedoor carries on.",
    "win32": "Windows Credential Manager isn't answering: sign in to Windows again and Sidedoor carries on.",
}
UNAVAILABLE_TEXT = ("Waiting for your Mac's keychain: Sidedoor can't read your keys from it right now. "
                    "Sidedoor carries on by itself once it answers.")


class Unavailable(OSError):
    """The OS store could not be used. With no file to fall back on, Sidedoor waits."""


class Locked(Unavailable):
    """The store is locked, or did not answer in time. Never prompted for, never waited on."""


def _real_store_allowed():
    # The built app never runs the unit tests, and a library it ships imports unittest (fpdf.sign
    # imports unittest.mock), which made every keychain read refused once a PDF had been written or
    # doctor had checked the PDF writer (2026-10-23: "Waiting for keychain", scheduler held).
    if bool(getattr(sys, "frozen", False)) or "__compiled__" in globals():
        return True
    return "unittest" not in sys.modules or os.environ.get(ALLOW_IN_TESTS_VARIABLE) == "1"


def _refuse_in_tests():
    if not _real_store_allowed():
        raise Unavailable("REAL_STORE_REFUSED_IN_TESTS")


# --- backends --------------------------------------------------------------------------------------

def keychain_unlocked(keychain=None):
    """(unlocked, path) for the keychain `keychain`, or for the default keychain when None, read
    with SecKeychainGetStatus and SecKeychainGetPath, which never show UI. The path is passed to
    every `security` call, so a find never searches another (locked, prompting) keychain on the
    search list (KC4). Raises Unavailable when either cannot be read."""
    _refuse_in_tests()
    import ctypes
    try:
        security = ctypes.cdll.LoadLibrary(SECURITY_FRAMEWORK)
        foundation = ctypes.cdll.LoadLibrary(CORE_FOUNDATION)
    except OSError:
        raise Unavailable("KEYCHAIN_FRAMEWORK_UNAVAILABLE") from None
    reference = ctypes.c_void_p()
    if keychain:
        security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        security.SecKeychainOpen.restype = ctypes.c_int32
        code = security.SecKeychainOpen(os.fsencode(str(keychain)), ctypes.byref(reference))
    else:
        security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        security.SecKeychainCopyDefault.restype = ctypes.c_int32
        code = security.SecKeychainCopyDefault(ctypes.byref(reference))
    if code != 0 or not reference.value:
        raise Unavailable("KEYCHAIN_STATUS_UNREADABLE: %d" % code)
    try:
        status = ctypes.c_uint32()
        security.SecKeychainGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        security.SecKeychainGetStatus.restype = ctypes.c_int32
        code = security.SecKeychainGetStatus(reference, ctypes.byref(status))
        if code != 0:
            raise Unavailable("KEYCHAIN_STATUS_UNREADABLE: %d" % code)
        size = ctypes.c_uint32(4096)
        buffer = ctypes.create_string_buffer(4096)
        security.SecKeychainGetPath.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]
        security.SecKeychainGetPath.restype = ctypes.c_int32
        code = security.SecKeychainGetPath(reference, ctypes.byref(size), buffer)
        if code != 0 or not buffer.value:
            raise Unavailable("KEYCHAIN_PATH_UNREADABLE: %d" % code)
        path = os.fsdecode(buffer.value)
    finally:
        foundation.CFRelease.argtypes = [ctypes.c_void_p]
        foundation.CFRelease(reference)
    return bool(status.value & UNLOCKED_STATUS), path


def _decode(output):
    """`find-generic-password -w` prints a value that is not printable ASCII as hex (A8): hex
    whose bytes are UTF-8 with a non-ASCII or control character is decoded; anything else is
    the value as printed (a put reads back and compares, so a misread never goes unnoticed)."""
    if len(output) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", output):
        try:
            text = bytes.fromhex(output).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return output
        if any(ord(character) > 126 or ord(character) < 32 for character in text):
            return text
    return output


class MacKeychain:
    name = "Keychain"
    waiting_text = LOCKED_TEXT["darwin"]
    unavailable_text = UNAVAILABLE_TEXT

    def __init__(self, security=SECURITY, keychain=None, timeout=TIMEOUT_SECONDS, unlocked=None):
        self.security, self.keychain, self.timeout = str(security), keychain, timeout
        # The non-prompting lock check, injectable for tests (default: keychain_unlocked). It
        # returns (unlocked, keychain path) or a bare bool (then `keychain`, if any, is the path).
        self.unlocked = unlocked or (lambda: keychain_unlocked(self.keychain))

    def probe(self):
        """The keychain every call names: its path once the non-prompting check says it is
        unlocked (KR5, A1). Raises Locked while locked (no `security` process starts), Unavailable
        when the check fails."""
        if self.security == SECURITY:
            _refuse_in_tests()
        state = self.unlocked()
        unlocked, path = state if isinstance(state, tuple) else (state, self.keychain)
        if not unlocked:
            raise Locked("KEYCHAIN_LOCKED")
        if path is not None and ('"' in str(path) or "\\" in str(path) or "\n" in str(path)):
            raise Unavailable("KEYCHAIN_PATH_UNSUPPORTED")
        return str(path) if path else None

    def _run(self, arguments, stdin=None):
        timed_out = failed = None
        try:
            return subprocess.run([self.security] + arguments, input=stdin, capture_output=True, text=True,
                                  timeout=self.timeout, stdin=None if stdin is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as error:
            failed = type(error).__name__
        # Raised outside the except blocks, so no partial output survives in __context__ (A9).
        if timed_out:
            raise Locked("KEYCHAIN_TIMEOUT")
        raise Unavailable("KEYCHAIN_TOOL_UNAVAILABLE: %s" % failed)

    @staticmethod
    def _check(done, what):
        if done.returncode in LOCKED_EXITS:
            raise Locked("KEYCHAIN_LOCKED: %s exit %d" % (what, done.returncode))
        raise Unavailable("KEYCHAIN_REFUSED: %s exit %d" % (what, done.returncode))

    def read(self, account):
        path = self.probe()
        done = self._run(["find-generic-password", "-s", SERVICE, "-a", account, "-w"] + ([path] if path else []))
        if done.returncode == 0:
            return _decode(done.stdout[:-1] if done.stdout.endswith("\n") else done.stdout)
        if done.returncode == NOT_FOUND_EXIT:
            return None
        self._check(done, "read")

    def write(self, account, value):
        if not re.fullmatch(r"[0-9a-f]{32}:[a-z]+", account):
            raise Unavailable("CREDENTIALS_ID_INVALID")   # A7: nothing else goes into a -i line
        path = self.probe()
        command = "add-generic-password -U -s %s -a %s -X %s%s\n" % (
            SERVICE, account, value.encode("utf-8").hex(), ' "%s"' % path if path else "")
        done = self._run(["-i"], stdin=command)
        if done.returncode != 0:
            self._check(done, "write")

    def delete(self, account):
        path = self.probe()
        done = self._run(["delete-generic-password", "-s", SERVICE, "-a", account] + ([path] if path else []))
        if done.returncode not in (0, NOT_FOUND_EXIT):
            self._check(done, "delete")


class _WindowsApi:
    """advapi32's Credential Manager calls through ctypes (bytes in, bytes out)."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                        ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME),
                        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        self.CREDENTIAL = CREDENTIAL
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CredReadW = advapi32.CredReadW
        self.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.CredReadW.restype = wintypes.BOOL
        self.CredWriteW = advapi32.CredWriteW
        self.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.CredWriteW.restype = wintypes.BOOL
        self.CredDeleteW = advapi32.CredDeleteW
        self.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.CredDeleteW.restype = wintypes.BOOL
        self.CredFree = advapi32.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]
        self.CredFree.restype = None

    def read(self, target):
        ctypes = self.ctypes
        pointer = ctypes.POINTER(self.CREDENTIAL)()
        if not self.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == self.ERROR_NOT_FOUND:
                return None
            # Any other error (e.g. 1312 ERROR_NO_SUCH_LOGON_SESSION at a headless start) waits (A2).
            raise Unavailable("CREDENTIAL_READ_FAILED: %d" % code)
        try:
            size = pointer.contents.CredentialBlobSize
            return ctypes.string_at(pointer.contents.CredentialBlob, size) if size else b""
        finally:
            self.CredFree(pointer)

    def write(self, target, blob):
        ctypes = self.ctypes
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self.CREDENTIAL()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = SERVICE
        if not self.CredWriteW(ctypes.byref(credential), 0):
            raise Unavailable("CREDENTIAL_WRITE_FAILED: %d" % ctypes.get_last_error())

    def delete(self, target):
        if not self.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            code = self.ctypes.get_last_error()
            if code != self.ERROR_NOT_FOUND:
                raise Unavailable("CREDENTIAL_DELETE_FAILED: %d" % code)


class WindowsCredentials:
    name = "Credential Manager"
    waiting_text = LOCKED_TEXT["win32"]
    unavailable_text = LOCKED_TEXT["win32"]

    def __init__(self, api=None):
        self._api = api

    def api(self):
        if self._api is None:
            _refuse_in_tests()
            try:
                self._api = _WindowsApi()
            except (OSError, AttributeError, ImportError) as error:
                raise Unavailable("CREDENTIAL_MANAGER_UNAVAILABLE: %s" % type(error).__name__) from None
        return self._api

    @staticmethod
    def target(account):
        return SERVICE + "/" + account.replace(":", "/")

    def read(self, account):
        blob = self.api().read(self.target(account))
        if blob is None:
            return None
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            raise Unavailable("CREDENTIAL_UNDECODABLE") from None

    def write(self, account, value):
        self.api().write(self.target(account), value.encode("utf-8"))

    def delete(self, account):
        self.api().delete(self.target(account))


NO_STORE = object()   # use_backend(NO_STORE): no OS store, as SIDEDOOR_CREDENTIAL_STORE=file
_override = None
_platform_backend = None


def use_backend(backend):
    """Tests: make `backend` the store (NO_STORE: none; None: back to the platform's). Clears the
    cache. Returns the previous override, for restoring."""
    global _override
    previous, _override = _override, backend
    clear_cache()
    return previous


def backend():
    """The store in use, or None (no OS store: the 0600 file)."""
    global _platform_backend
    if _override is NO_STORE:
        return None
    if _override is not None:
        return _override
    if os.environ.get(STORE_VARIABLE, "").strip().lower() == "file":
        return None
    if _platform_backend is None:
        if sys.platform == "darwin" and Path(SECURITY).exists():
            _platform_backend = MacKeychain()
        elif sys.platform == "win32":
            _platform_backend = WindowsCredentials()
    return _platform_backend


def where():
    """The store's name for doctor: "Keychain", "Credential Manager" or "file"."""
    chosen = backend()
    return chosen.name if chosen is not None else "file"


# --- cache, install id -----------------------------------------------------------------------------

_cache = {}      # (home, name) -> (value or None, monotonic time)
_locked = {}     # home -> (the Unavailable raised, monotonic time): fail fast inside the window
_seen = {}       # (home, name) -> values read or saved in this process: redaction only (A4), never written
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()
        _locked.clear()


def _forget(home, name):
    with _cache_lock:
        _cache.pop((str(home), name), None)
        _locked.pop(str(home), None)


def _remember(home, name, value):
    with _cache_lock:
        _cache[(str(home), name)] = (value, time.monotonic())
        if value:
            _seen.setdefault((str(home), name), set()).add(value)


def _home(home):
    if home is not None:
        return Path(home)
    import user_profile
    return user_profile.home()


def _path(name, home):
    return _home(home) / NAMES[name]


def _read_id(home):
    """(id, recorded home path) or (None, None). Raises Unavailable for an id that is not 32 hex."""
    path = _home(home) / ID_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    except OSError:
        raise Unavailable("CREDENTIALS_ID_UNREADABLE") from None
    if not text:
        return None, None
    try:
        record = json.loads(text) if text.startswith("{") else {"id": text}
    except ValueError:
        record = {}
    value = record.get("id") if isinstance(record, dict) else None
    if not isinstance(value, str) or not INSTALL_ID.fullmatch(value):
        raise Unavailable("CREDENTIALS_ID_INVALID")
    recorded = record.get("home") if isinstance(record.get("home"), str) else None
    return value, recorded


def _write_id(home, value):
    import setup_steps
    setup_steps._write_private(_home(home) / ID_FILE, json.dumps(
        {"id": value, "home": os.path.realpath(_home(home))}) + "\n")


def _account(name, home):
    install, _ = _read_id(home)
    return install + ":" + name if install else None


def _account_for_write(name, home):
    """(account, a new id to record or None). A new id is written only after the store took a
    value under it (`_commit_id`), so a refused first save leaves no id behind (KC2)."""
    install, _ = _read_id(home)
    if install is not None:
        return install + ":" + name, None
    fresh = uuid.uuid4().hex
    return fresh + ":" + name, fresh


def _commit_id(home, fresh):
    if fresh is not None:
        _write_id(home, fresh)


# --- an explicit "Save in Sidedoor's folder instead" (KC1) ------------------------------------------

CHOICE_FILE = "credentials-file-choice.json"   # {name: SHA-256 of the value the user chose to keep in a file}
BROKEN_READS = 3                              # store failures (not locked) before the file is offered


def _choices(home):
    try:
        value = json.loads((_home(home) / CHOICE_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _set_choice(home, name, value):
    import hashlib
    import setup_steps
    choices = _choices(home)
    if value is None:
        if name not in choices:
            return
        choices.pop(name)
    else:
        choices[name] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    path = _home(home) / CHOICE_FILE
    if choices:
        setup_steps._write_private(path, json.dumps(choices, sort_keys=True) + "\n")
    else:
        path.unlink(missing_ok=True)


def _chosen_file(name, home):
    """The value in the key file when the user chose to keep it there (the file wins), else None."""
    import hashlib
    expected = _choices(home).get(name)
    if not expected:
        return None
    value = _read_file(_path(name, home))
    if value and hashlib.sha256(value.encode("utf-8")).hexdigest() == expected:
        return value
    return None


_broken = {}   # home -> store failures that were not "locked" since the last good answer


def _note_failure(home, error):
    with _cache_lock:
        if isinstance(error, Locked):
            _broken.pop(str(home), None)
        else:
            _broken[str(home)] = _broken.get(str(home), 0) + 1


def _note_good(home):
    with _cache_lock:
        _broken.pop(str(home), None)


def offer_file(home=None):
    """True when the store is broken rather than locked (BROKEN_READS failed reads, or one failed
    write): Setup and Settings then offer "Save in Sidedoor's folder instead" (KC1). A locked
    keychain never offers it: it waits."""
    if backend() is None:
        return False
    with _cache_lock:
        return _broken.get(str(_home(home)), 0) >= BROKEN_READS


def put_file(name, value, home=None):
    """The K4 fallback the user chose: the key in the private 0600 file, used before the store's
    value until the store works again, when `migrate` moves this value into it (KC1)."""
    import setup_steps
    value = value.strip()
    home = _home(home)
    setup_steps._write_private(_path(name, home), value + "\n")
    if backend() is not None:
        _set_choice(home, name, value)
    _forget(home, name)
    with _cache_lock:
        _seen.setdefault((str(home), name), set()).add(value)
    return "file"


SAVE_LOCKED = "Unlock your keychain, then click Save."
SAVE_BROKEN = ("Sidedoor can't save to your keychain right now. You can save the key in Sidedoor's folder instead "
               "(a private file only you can read); Sidedoor moves it into the keychain once it works.")


def save_refusal(home=None):
    """Before a save: why the store can't take one now (SAVE_LOCKED while locked; SAVE_BROKEN, or
    the waiting line until the file is offered, while broken), else None. Asks only the
    non-prompting lock check and the cached reads, never writes anything (KC2)."""
    chosen = backend()
    if chosen is None:
        return None
    home = _home(home)
    probe = getattr(chosen, "probe", None)
    try:
        if probe is not None:
            probe()
    except Locked:
        return SAVE_LOCKED
    except Unavailable:
        with _cache_lock:   # KC1: the check behind a save failed: offer the folder at once
            _broken[str(home)] = max(_broken.get(str(home), 0), BROKEN_READS)
        return SAVE_BROKEN
    reason = hold_reason(home)
    if reason is None:
        return SAVE_BROKEN if offer_file(home) else None
    with _cache_lock:
        failed = _locked.get(str(home))
    if failed is not None and isinstance(failed[0], Locked):
        return SAVE_LOCKED
    with _cache_lock:   # KC1: a save tried while the store is broken counts as the failed write
        _broken[str(home)] = max(_broken.get(str(home), 0), BROKEN_READS)
    return SAVE_BROKEN



def _read_file(path):
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _store_read(name, home):
    """The stored value, None when the store has none (or no id exists yet), cached CACHE_SECONDS
    (a value, "not found" and an error alike, C2). Raises Locked / Unavailable."""
    chosen = backend()
    if chosen is None:
        return None
    key = (str(_home(home)), name)
    now = time.monotonic()
    with _cache_lock:
        failed = _locked.get(key[0])
        if failed is not None and now - failed[1] < CACHE_SECONDS:
            raise type(failed[0])(*failed[0].args)
        cached = _cache.get(key)
        if cached is not None and now - cached[1] < CACHE_SECONDS:
            return cached[0]
    try:
        account = _account(name, home)
        value = chosen.read(account) if account is not None else None
    except Unavailable as error:
        with _cache_lock:
            _locked[key[0]] = (error, time.monotonic())
        _note_failure(key[0], error)
        raise
    if account is not None:
        _note_good(key[0])
    value = value.strip() if isinstance(value, str) and value.strip() else None
    _remember(_home(home), name, value)
    return value


# --- the helper every caller uses ------------------------------------------------------------------

def get(name, home=None):
    """The credential, or None when none is saved. The store's value wins; with none stored, the
    file. Raises Locked / Unavailable (OSError) when the store cannot answer and no file is left."""
    path = _path(name, home)
    chosen_file = _chosen_file(name, home)
    if chosen_file is not None:
        return chosen_file   # KC1: the user saved it in the folder while the store was broken
    try:
        value = _store_read(name, home)
    except Unavailable:
        value = _read_file(path)   # only ever written while the store held nothing for it
        if value is None:
            raise
        return value
    return value if value is not None else _read_file(path)


def put(name, value, home=None):
    """Save `value`. With a store: refused (Locked / Unavailable raised, nothing written) unless
    the store answers; any stale file is removed first (A3), then the store is written, read back
    and compared. A file is written only while the store is confirmed to hold nothing for this
    key (no store; or a refused write or wrong read-back that leaves the item absent). Returns
    "store" or "file"."""
    import setup_steps
    value = value.strip()
    home = _home(home)
    path = _path(name, home)
    chosen = backend()
    if chosen is None:
        setup_steps._write_private(path, value + "\n")
        return "file"
    account, fresh = _account_for_write(name, home)
    try:
        chosen.read(account)             # raises while the store is locked or unavailable (KR3)
    except Unavailable as error:
        with _cache_lock:
            if not isinstance(error, Locked):   # KC1: a failed save offers the file at once
                _broken[str(home)] = max(_broken.get(str(home), 0), BROKEN_READS)
        raise
    path.unlink(missing_ok=True)         # A3: no older file can outlive the new key
    _set_choice(home, name, None)
    _forget(home, name)
    try:
        chosen.write(account, value)
        if chosen.read(account) == value:
            _commit_id(home, fresh)      # KC2: the id is recorded only now
            _note_good(home)
            _remember(home, name, value)
            return "store"
        chosen.delete(account)           # a wrong read-back: put the item back to absent first
    except Unavailable as error:
        with _cache_lock:
            if not isinstance(error, Locked):
                _broken[str(home)] = max(_broken.get(str(home), 0), BROKEN_READS)   # KC1: one failed write offers the file

    if chosen.read(account) is not None:  # not confirmed absent: never write a file beside it
        raise Unavailable("KEYCHAIN_WRITE_UNCONFIRMED")
    setup_steps._write_private(path, value + "\n")
    _remember(home, name, None)
    with _cache_lock:
        _seen.setdefault((str(home), name), set()).add(value)
    return "file"


def delete(name, home=None):
    """Remove the credential: the store item first, then the file. A refused store delete raises
    Locked / Unavailable and leaves the file too, so nothing half-removed reappears (A5)."""
    home = _home(home)
    chosen = backend()
    account = _account(name, home) if chosen is not None else None
    if account is not None:
        chosen.delete(account)
        if chosen.read(account) is not None:
            raise Unavailable("KEYCHAIN_DELETE_UNCONFIRMED")
    _path(name, home).unlink(missing_ok=True)
    _set_choice(home, name, None)
    _forget(home, name)


def remove_all(home=None):
    """Settings > "Remove Sidedoor's saved keys from this computer" (A5): every key, store and file."""
    for name in NAMES:
        delete(name, home)


def status(name, home=None):
    """For doctor: "store", "file" (no store, or not moved yet), "file_chosen" (the user saved it in
    the folder while the store was broken, KC1), "file_unread" (a file, used while
    the store does not answer), "conflict" (a file left beside a
    different stored key; the stored one is used), "missing", "locked" or "unavailable"."""
    if _chosen_file(name, home) is not None:
        return "file_chosen"
    try:
        file_value = _read_file(_path(name, home))
    except OSError:
        file_value = None
    try:
        stored = _store_read(name, home)
    except Locked:
        return "file_unread" if file_value else "locked"
    except Unavailable:
        return "file_unread" if file_value else "unavailable"
    if stored and file_value and stored != file_value:
        return "conflict"
    if stored:
        return "store"
    return "file" if file_value else "missing"


def values(home=None):
    """Every value to redact (failures_page.secrets_of): the files, the store, and every value
    this process read or saved (A4, memory only). Never raises."""
    home = _home(home)
    found = []
    for name in NAMES:
        try:
            found.append(_read_file(_path(name, home)))
        except OSError:
            pass
        try:
            found.append(_store_read(name, home))
        except OSError:
            pass
        with _cache_lock:
            found.extend(_seen.get((str(home), name), ()))
    return [value for value in found if value]


def _wanted(name, home):
    """The OpenAI API key (E0.T8) is optional: it counts for the waits below only while the user
    chose it (setup-state codex_auth "api_key") or one was saved (its file or `openai.key.tested`).
    Otherwise a locked store would hold every Sign-in-with-ChatGPT user on a key they never had.
    The other keys always count."""
    if name != "openai":
        return True
    home = _home(home)
    if (home / NAMES[name]).exists() or (home / "openai.key.tested").exists():
        return True
    try:
        import openai_auth
        return openai_auth.api_key_mode(home)
    except Exception:
        return True


def redaction_complete(home=None):
    """False while the store is waiting and some key's value is not known in this process: the
    Failures page then hides failure details rather than risk showing a key (A4)."""
    home = _home(home)
    for name in [name for name in NAMES if _wanted(name, home)]:
        try:
            get(name, home)
        except OSError:
            with _cache_lock:
                if not _seen.get((str(home), name)):
                    return False
    return True


def hold_reason(home=None):
    """While a saved key cannot be read because the store is locked or unavailable (and no file is
    left): the line for the banner, the scheduler and the key UI. Else None. Cached reads, so a
    render or a tick costs nothing inside CACHE_SECONDS."""
    chosen = backend()
    for name in [name for name in NAMES if _wanted(name, home)]:
        try:
            get(name, home)
        except Locked:
            return chosen.waiting_text if chosen is not None else LOCKED_TEXT["darwin"]
        except Unavailable:
            return chosen.unavailable_text if chosen is not None else UNAVAILABLE_TEXT
        except OSError:
            continue
    return None


# --- migration -------------------------------------------------------------------------------------

def migrate(home=None, log=None):
    """At the start of the Sidedoor process, under `credentials.lock` (a busy lock skips this
    start): give a copied or moved home its own id (A6), then move each key file into the store
    when the store holds nothing for it: copy, read back, compare, delete the file. Any failure
    keeps the file and the next start tries again. Returns {name: outcome} ("moved", "absent",
    "no_store", "same", "kept: <reason>"; plus "home") or {"skipped": "busy"}."""
    import portable
    chosen = backend()
    home = _home(home)
    if chosen is None:
        return {name: "no_store" if (home / file).exists() else "absent" for name, file in NAMES.items()}
    if not (home / ID_FILE).exists() and not any((home / file).exists() for file in NAMES.values()):
        return dict.fromkeys(NAMES, "absent")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = portable.open_lock(home / LOCK_FILE)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not portable.try_lock(descriptor):
            if time.monotonic() >= deadline:
                return {"skipped": "busy"}
            time.sleep(0.1)
        try:
            result = {}
            try:
                result["home"] = _rebind(home, chosen)
            except OSError as error:
                result["home"] = "kept: %s" % (str(error).split(":")[0] or type(error).__name__)
                return result   # the id stays as it is; nothing is moved under it this start
            for name in NAMES:
                result[name] = _move(name, home, chosen)
                if result[name].startswith("kept") and log is not None:
                    log("Sidedoor kept %s in its folder for now (%s); it tries again at the next start."
                        % (NAMES[name], result[name][6:]))
            return result
        finally:
            portable.unlock(descriptor)
    finally:
        os.close(descriptor)


def _rebind(home, chosen):
    """A6: an id recorded for another path (a copied or moved home) is replaced by a fresh one;
    the values are copied across; the old items are deleted only when that path is gone."""
    install, recorded = _read_id(home)
    here = os.path.realpath(home)
    if install is None:
        return "none"
    if recorded is None:
        _write_id(home, install)   # an id written before the path was recorded: this home's
        return "recorded"
    if recorded == here:
        return "same"
    fresh = uuid.uuid4().hex
    copied = []
    for name in NAMES:
        value = chosen.read(install + ":" + name)
        if value:
            chosen.write(fresh + ":" + name, value)
            if chosen.read(fresh + ":" + name) != value:
                raise Unavailable("KEYCHAIN_READ_BACK_DIFFERS")
            copied.append(name)
    _write_id(home, fresh)
    clear_cache()
    if not os.path.exists(recorded):
        for name in copied:
            try:
                chosen.delete(install + ":" + name)
            except OSError:
                pass
        return "moved"
    return "copied"


def _move(name, home, chosen):
    path = home / NAMES[name]
    try:
        value = _read_file(path)
    except OSError as error:
        return "kept: file unreadable (%s)" % type(error).__name__
    if value is None:
        return "absent"
    chosen_by_user = _chosen_file(name, home) is not None
    try:
        account, fresh = _account_for_write(name, home)
        stored = chosen.read(account)
        if stored == value:
            path.unlink(missing_ok=True)
            _set_choice(home, name, None)
            _remember(home, name, value)
            return "same"
        if stored is not None and not chosen_by_user:   # KR3: a file never overwrites a stored key
            return "kept: the keychain holds a different key"
        # Absent, or the user's own "Save in Sidedoor's folder instead" (newer by their choice, KC1).
        chosen.write(account, value)
        if chosen.read(account) != value:
            if stored is None:
                chosen.delete(account)
            return "kept: read-back differs"
        _commit_id(home, fresh)
    except OSError as error:
        return "kept: %s" % (str(error).split(":")[0] or type(error).__name__)
    path.unlink(missing_ok=True)
    _set_choice(home, name, None)
    _remember(home, name, value)
    return "moved"
