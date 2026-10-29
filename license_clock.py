"""The license clock (E8.T2): the daily heartbeat, the trial countdown, the 72-hour offline grace
and the clock-tamper check, and the one license state every page reads (E6.T0 sections 2, 5, 10, 11).

What is stored, in the Sidedoor home next to Setup step 1's files (each 0600):
- `license-entitlement.json`: {"entitlement": <the exact JSON text the relay signed>,
  "signature": <base64 Ed25519>, "clock": <the clock record>}. The relay signs the entitlement
  (`relay/licenses.py`, `Signer`); the app ships the public half (`PUBLIC_KEY`, the same value as
  relay/license-public-key.txt) and checks the signature, the key's SHA-256 and this computer's
  id every time it reads the file. One file, written atomically: a page reads the entitlement
  and its clock in one consistent read.
- `license-heartbeat.json`: a second copy of the clock record. Each record names the entitlement
  it belongs to (`for`, a digest of its signature) and carries a sequence number (`seq`); a read
  takes the newest record of either copy that belongs to the stored entitlement, so deleting one
  file clears nothing.

The clock record: `received_at` (this computer's clock when the entitlement was stored),
`virtual` (a time that never goes back: the latest of this computer's clock and the previous
`virtual` plus the monotonic time the running heartbeat measured since; moved on every
`POLL_SECONDS`), `last_attempt_at`, `last_result` ("ok", "offline", "invalid" for 401
LICENSE_INVALID, "machine" for 403 MACHINE_NOT_ACTIVATED, "activation_limit" for a 409 on the
one automatic re-activation), `last_error`, `refused_key` (the SHA-256 of a key the heartbeat got
401 LICENSE_INVALID for) and `reactivated_from` (the machine id the automatic re-activation
moved from).

Time: "now" for the trial and the grace is the signed `server_time` plus (`virtual` -
`received_at`), so setting the clock back never adds time, however often and however little. The
clock is wrong (state "license_problem", problem "clock": no trial or grace, nothing usable) when
it reads more than `CLOCK_TOLERANCE` before the signed server time or before `virtual`, or when it
differed from the signed server time by more than `CLOCK_TOLERANCE` at the check. A successful
heartbeat with a right clock clears it. With an entitlement but no clock record (the first run
after an upgrade, or both copies gone) the state is "checking" until the first check finishes:
nothing is extended (now is at least the signed server time) and the check runs at once.

Machine id (`machine_id`): the hardware's own id, salted and hashed: macOS IOPlatformUUID
(`ioreg -rd1 -c IOPlatformExpertDevice`), Windows MachineGuid (HKLM\\SOFTWARE\\Microsoft\\Cryptography),
Linux /etc/machine-id. Once read it is kept in the home (`machine-hardware-id`) for a launch whose
read fails; the random `machine-id` file only where it was never read. An install activated
before this (its entitlement names the random file's id) is re-activated once, automatically,
with `replaces` = that id, so the relay hands its slot to the hardware id and no new slot is used
(relay/licenses.py `activate`); if that slot is gone already, a plain activation follows, and a
409 ACTIVATION_LIMIT then says so in plain words (support frees one with relay/admin.py). The
user's [Activate] sends the same `replaces`, so it can take the old slot after that refusal (LA1).
Only an old file id ("sd-") is ever replaced: an entitlement naming another hardware id (a home
copied from another Mac) gets a plain activation and never takes that Mac's slot (LA3).

The heartbeat (`Heartbeat`, started by `scheduler_loop.start_license_heartbeat`): its own daemon
thread, outside the scheduler loop and blind to both pause flags, so it keeps running while
Sidedoor is paused (E5.T1 C10). It checks once at once when the dashboard starts, then once a day
(`INTERVAL_SECONDS`, measured with `time.monotonic()` so a clock change cannot stop it), every
hour (`RETRY_SECONDS`) while the relay cannot be reached or the clock looks wrong (so a corrected
clock clears within the hour), and when woken (`request_check`). The
[Check again] buttons run `check_now`: never two checks at once and at most one a minute. A page
render never calls the relay: `view` reads the stored files only.

`POST /license/heartbeat` {key, machine} (relay/server.py): 200 with a signed entitlement; 401
LICENSE_INVALID (unknown key); 403 MACHINE_NOT_ACTIVATED. `POST /license/activate` likewise
counts a 401 as refused only with LICENSE_INVALID in its body. Any other answer (a 5xx, a 401 with
another body, a malformed or unverifiable answer, a broken connection, a timeout) is "offline"
(section 11).

The license pause (E8.T3): `pause_reason(view)` names it ("expired", "grace_spent", or "machine"
for a 403 once `valid_until` passes) and `scheduler_loop.sync_license_pause` writes it
(`scheduler_control.sync_license_pause`): after every heartbeat step (`after_tick`), before the
scheduler starts a due job, and after the dashboard's license actions. A usable license (a
renewal, a new key, a successful heartbeat) clears it; the user's own pause is never touched.

"Refused now" from another process (a job child): with no heartbeat in this process,
`request_check` leaves `CHECK_REQUEST_FILE` in the home; the dashboard's heartbeat takes it at its
next step (at most `POLL_SECONDS` later) and checks at once, under the same limits.
"""

import base64
import datetime as dt
import hashlib
import http.client
import json
import math
import re
import subprocess
import sys
import threading
import time

import credentials
import setup_steps


PUBLIC_KEY = "ldJeJ2uVGLp9UFwQJ55erjOKejXbkxFw6N0S5NrMFQc="  # relay/license-public-key.txt
HEARTBEAT_FILE = "license-heartbeat.json"
HARDWARE_ID_FILE = "machine-hardware-id"
CHECK_REQUEST_FILE = "license-check-request"
INTERVAL_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
POLL_SECONDS = 60
MIN_CHECK_GAP_SECONDS = 60          # [Check again] and "Refused now": at most one check a minute
REQUEST_TIMEOUT_SECONDS = 15
CLOCK_TOLERANCE = dt.timedelta(hours=1)
GRACE_WARNING = dt.timedelta(hours=24)  # section 11: amber with the hours left from 24 hours before valid_until
MACHINE_SALT = b"sidedoor-machine-v1:"
SETTINGS_LICENSE = "/settings#license"
CHECK_PATH = "/license/check"
NETWORK_ERRORS = (OSError, TimeoutError, ValueError, http.client.HTTPException)

_write_lock = threading.RLock()
_check_lock = threading.Lock()
_last_check = {"mono": None}


# --- Ed25519 verification (RFC 8032 section 5.1.7; the standard library cannot verify) -----------

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(raw):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(public, message, signature):
    """True when `signature` (64 bytes) signs `message` (bytes) under `public` (32 bytes)."""
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a, point_r = _decompress(public), _decompress(signature[:32])
    scalar = int.from_bytes(signature[32:], "little")
    if point_a is None or point_r is None or scalar >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _Q
    return _equal(_multiply(scalar, _G), _add(point_r, _multiply(h, point_a)))


_verified = {}
_verified_lock = threading.Lock()


def _signature_ok(text, signature):
    try:
        public = base64.b64decode(PUBLIC_KEY, validate=True)
        raw = base64.b64decode(signature, validate=True)
        message = text.encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        return False
    cache_key = (PUBLIC_KEY, text, signature)
    with _verified_lock:
        known = _verified.get(cache_key)
    if known is None:
        known = ed25519_verify(public, message, raw)
        with _verified_lock:
            if len(_verified) > 32:
                _verified.clear()
            _verified[cache_key] = known
    return known


# --- this computer's id -------------------------------------------------------------------------

def _read_hardware_id(platform=None, run=subprocess.run, registry=None, read_file=None):
    """The raw hardware id (macOS IOPlatformUUID, Windows MachineGuid, Linux machine-id) or None."""
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            completed = run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                            text=True, timeout=10)
            found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{16,64})"', completed.stdout or "")
            return found.group(1).upper() if found else None
        if platform.startswith("win"):
            if registry is None:
                import winreg
                registry = winreg
            access = registry.KEY_READ | getattr(registry, "KEY_WOW64_64KEY", 0)
            with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as handle:
                value, _ = registry.QueryValueEx(handle, "MachineGuid")
            return value.strip().lower() if isinstance(value, str) and value.strip() else None
        reader = read_file or (lambda name: open(name, encoding="ascii").read())
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = reader(path).strip()
            except (OSError, ValueError):
                continue
            if value:
                return value
    except (OSError, ValueError, subprocess.SubprocessError, ImportError, AttributeError):
        return None
    return None


_hardware = {}


def hardware_id():
    """The raw hardware id, read once per process; a failed read is not remembered (tried again)."""
    if "value" not in _hardware:
        value = _read_hardware_id()
        if not value:
            return None
        _hardware["value"] = value
    return _hardware["value"]


def machine_id(home=None):
    """This computer's id: "sdm-" + SHA-256(salt + hardware id). The id is kept in the home
    (`HARDWARE_ID_FILE`) once read, so a launch whose hardware read fails uses it, never the
    random file; a fresh read always wins (a home copied to another computer gets that computer's
    id). The random `machine-id` file only where the hardware id was never read."""
    raw = hardware_id()
    path = setup_steps._home(home) / HARDWARE_ID_FILE
    if raw:
        value = "sdm-" + hashlib.sha256(MACHINE_SALT + raw.encode("utf-8")).hexdigest()[:40]
        try:
            if path.read_text(encoding="utf-8").strip() != value:
                raise ValueError
        except (OSError, ValueError):
            try:
                setup_steps._write_private(path, value + "\n")
            except OSError:
                pass
        return value
    try:
        kept = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        kept = ""
    return kept if kept.startswith("sdm-") else setup_steps.file_machine_id(home)


# --- stored files ---------------------------------------------------------------------------------

def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _iso(moment):
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now(now=None):
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)


def _key_digest(key):
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()


def _for(signed):
    return hashlib.sha256(str(signed.get("signature")).encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _claims(signed):
    """The entitlement's fields as written, unverified (or None)."""
    try:
        value = json.loads(signed["entitlement"])
    except (ValueError, TypeError, KeyError):
        return None
    return value if isinstance(value, dict) else None


def check_signed(signed, key, machine):
    """The entitlement's fields when `signed` ({"entitlement", "signature"}) is signed by the relay
    for this key and this machine; else None."""
    try:
        if not (isinstance(signed, dict) and isinstance(signed.get("entitlement"), str)
                and isinstance(signed.get("signature"), str) and key and machine):
            return None
        if not _signature_ok(signed["entitlement"], signed["signature"]):
            return None
        value = _claims(signed)
        if not (value and value.get("license_sha256") == _key_digest(key) and value.get("machine") == machine
                and _parse_time(value.get("server_time")) and _parse_time(value.get("valid_until"))
                and isinstance(value.get("valid"), bool)):
            return None
        return value
    except UnicodeError:
        return None


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _load(home):
    """(stored, record): the entitlement file's contents (None when missing or unreadable) and the
    newest clock record that belongs to it (or None)."""
    folder = setup_steps._home(home)
    stored = _read_json(folder / setup_steps.ENTITLEMENT_FILE)
    if stored is None or not isinstance(stored.get("signature"), str):
        return stored, None
    belongs = _for(stored)
    candidates = [record for record in (stored.get("clock"), _read_json(folder / HEARTBEAT_FILE))
                  if isinstance(record, dict) and record.get("for") == belongs and isinstance(record.get("seq"), int)]
    return stored, (max(candidates, key=lambda item: item["seq"]) if candidates else None)


def entitlement(home=None):
    """The stored entitlement's fields when its signature and binding check out; else None."""
    return check_signed(_load(home)[0], setup_steps.license_key(home), machine_id(home))


def heartbeat_record(home=None):
    return _load(home)[1] or {}


def _write(home, signed, record):
    """Both copies: the entitlement file (entitlement + record, atomically), then the heartbeat file."""
    folder = setup_steps._home(home)
    record = dict(record, **{"for": _for(signed)})
    setup_steps._write_private(folder / setup_steps.ENTITLEMENT_FILE, json.dumps(
        {"entitlement": signed["entitlement"], "signature": signed["signature"], "clock": record}, sort_keys=True) + "\n")
    setup_steps._write_private(folder / HEARTBEAT_FILE, json.dumps(record, sort_keys=True) + "\n")
    return record


def _update_record(home, change):
    """Apply `change(record)` to the clock record of the stored entitlement; no-op without one."""
    with _write_lock:
        stored, record = _load(home)
        if not isinstance(stored, dict) or not isinstance(stored.get("entitlement"), str):
            return None
        record = dict(record or {})
        change(record)
        record["seq"] = int(record.get("seq") or 0) + 1
        return _write(home, stored, record)


def store_signed(home, signed, now=None, **extra):
    """Store a verified entitlement (activation and every good heartbeat) and mark it received
    now: the countdown and the refused state restart. `virtual` restarts at now when the clock
    agrees with the signed server time, else it keeps the latest time seen."""
    now = _now(now)
    with _write_lock:
        previous = _load(home)[1] or {}
        server = _parse_time((_claims(signed) or {}).get("server_time"))
        virtual = _parse_time(previous.get("virtual"))
        if server and abs(now - server) <= CLOCK_TOLERANCE:
            virtual = now
        else:
            virtual = max(now, virtual) if virtual else now
        record = {"seq": int(previous.get("seq") or 0) + 1, "received_at": _iso(now), "virtual": _iso(virtual),
                  "last_attempt_at": _iso(now), "last_result": "ok", "last_error": None, "refused_key": None,
                  "reactivated_from": previous.get("reactivated_from")}
        record.update(extra)
        _write(home, {"entitlement": signed["entitlement"], "signature": signed["signature"]}, record)


def advance_virtual(home, elapsed_seconds, now=None):
    """The running heartbeat's step: virtual = max(virtual + monotonic elapsed, this clock)."""
    now = _now(now)

    def change(record):
        virtual = _parse_time(record.get("virtual"))
        moved = virtual + dt.timedelta(seconds=max(0.0, elapsed_seconds)) if virtual else now
        record["virtual"] = _iso(max(moved, now))
    with _write_lock:
        if _load(home)[1] is None:
            return None  # nothing received yet: the first check makes the record
        return _update_record(home, change)


# --- the heartbeat ------------------------------------------------------------------------------

def _post(url, body):
    return setup_steps._post_json(url, body, timeout=REQUEST_TIMEOUT_SECONDS)


CERTIFICATE = "CERTIFICATE"   # T7: last_error when the relay's secure connection could not be verified
_CERTIFICATE_FAILURE = {"error": CERTIFICATE}   # `_call`'s answer for that (never a relay answer: status None)


def _call(post, url, body):
    """(status, parsed JSON or None); status None when the relay could not be reached, with parsed
    `_CERTIFICATE_FAILURE` when that was a certificate failure (`tls_setup.is_certificate_error`)."""
    try:
        status, raw = post(url, body)
    except NETWORK_ERRORS as error:
        import tls_setup
        return None, (_CERTIFICATE_FAILURE if tls_setup.is_certificate_error(error) else None)
    try:
        parsed = json.loads(raw) if raw is not None else None
    except (ValueError, TypeError):
        parsed = None
    return status, parsed


def _error(parsed):
    return parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) else None


def activate(key, home=None, relay_url=None, post=None, now=None, to_file=False):
    """`POST /license/activate {key, machine}` under the check lock, so an older key's heartbeat in
    flight can never store over the new key. Returns (outcome, entitlement fields or None): "ok"
    (key and entitlement stored), "invalid" (401), "activation_limit" (409), "certificate" (the relay's
    secure connection could not be verified, T7) or "offline"."""
    with _check_lock:
        stored = _load(home)[0]
        claimed = (_claims(stored) or {}).get("machine") if isinstance(stored, dict) else None
        # The user's [Activate] after the automatic move was refused (LA1): it too hands the old
        # file id's slot to this computer, under the same rule as the automatic move (`_replaces`).
        return _activate(key, home, relay_url, post, now, replaces=_replaces(claimed, machine_id(home)),
                         to_file=to_file)


def _replaces(claimed, machine):
    """The id to send as `replaces`: only an old random file id ("sd-<uuid>", activated before the
    hardware id) other than this computer's. Another hardware id ("sdm-", e.g. a Sidedoor home
    copied from another Mac) never takes that computer's slot: a plain activation (LA3)."""
    if (isinstance(claimed, str) and claimed != machine and claimed.startswith("sd-")
            and not claimed.startswith("sdm-")):
        return claimed
    return None


def _activate(key, home, relay_url, post, now, replaces=None, to_file=False, **extra):
    """`replaces`: the id the stored entitlement names (the automatic move to the hardware id):
    the relay hands that id's slot to this computer, so no new slot is used."""
    relay_url = (relay_url or setup_steps.RELAY_URL).rstrip("/")
    machine = machine_id(home)
    body = {"key": key, "machine": machine}
    if replaces:
        body["replaces"] = replaces
    status, parsed = _call(post or _post, relay_url + "/license/activate", body)
    value = check_signed(parsed, key, machine) if status == 200 else None
    _last_check["mono"] = time.monotonic()
    if value is not None:
        with _write_lock:
            try:   # E2.T4: the OS store; the folder only when the user chose it (KC1)
                if to_file:
                    credentials.put_file("license", key, setup_steps._home(home))
                else:
                    credentials.put("license", key, setup_steps._home(home))
            except credentials.Unavailable:
                return "keychain", None   # KC2: nothing stored; the caller says why
            store_signed(home, {"entitlement": parsed["entitlement"], "signature": parsed["signature"]}, now, **extra)
        return "ok", value
    if status == 401 and _error(parsed) == "LICENSE_INVALID":
        return "invalid", None
    if status == 409 and _error(parsed) == "REPLACES_NOT_ACTIVATED" and replaces:
        # The old id's slot is gone already (support freed it): a plain activation, same attempt.
        return _activate(key, home, relay_url, post, now, to_file=to_file, **extra)
    if status == 409 and _error(parsed) == "ACTIVATION_LIMIT":
        return "activation_limit", None
    if status is None and parsed is _CERTIFICATE_FAILURE:
        return "certificate", None
    return "offline", None


def check(home=None, relay_url=None, post=None, now=None, wait=True):
    """One heartbeat now: "ok", "offline", "invalid", "machine", "activation_limit", "no_key"
    (nothing is sent) or "busy" (`wait` false and a check is running). Never raises for a network
    or answer problem; the key is never logged or returned."""
    if not _check_lock.acquire(blocking=wait):
        return "busy"
    try:
        return _check(home, relay_url, post, _now(now))
    finally:
        _last_check["mono"] = time.monotonic()
        _check_lock.release()


def _check(home, relay_url, post, now):
    key = setup_steps.license_key(home)
    try:
        if not key or not key.isascii():
            return "no_key"
    except UnicodeError:
        return "no_key"
    machine = machine_id(home)
    relay_url = (relay_url or setup_steps.RELAY_URL).rstrip("/")
    post = post or _post
    stored, record = _load(home)
    record = record or {}
    claimed = (_claims(stored) or {}).get("machine") if isinstance(stored, dict) else None
    if claimed and claimed != machine:
        if record.get("reactivated_from") == claimed and record.get("last_result") == "activation_limit":
            return "activation_limit"  # waits for the user's [Activate] once support frees a computer
        # Activated before the hardware id (E8.T2): one automatic re-activation under this
        # computer's id, taking the old file id's slot; an entitlement naming another hardware id
        # gets a plain activation (LA3). A relay that cannot be reached is tried again later.
        outcome, _ = _activate(key, home, relay_url, post, now, replaces=_replaces(claimed, machine),
                               reactivated_from=claimed)
        if outcome in ("invalid", "activation_limit"):
            _record(home, now, outcome, key, reactivated_from=claimed)
        elif outcome in ("offline", "certificate"):   # a certificate failure is offline, said plainly (T7)
            _record(home, now, "offline", key, code=CERTIFICATE if outcome == "certificate" else None)
            return "offline"
        return outcome
    status, parsed = _call(post, relay_url + "/license/heartbeat", {"key": key, "machine": machine})
    if status == 200 and check_signed(parsed, key, machine) is not None:
        with _write_lock:
            if setup_steps.license_key(home) != key:
                return "no_key"  # the key changed while this check was out
            store_signed(home, {"entitlement": parsed["entitlement"], "signature": parsed["signature"]}, now)
        return "ok"
    if status == 401 and _error(parsed) == "LICENSE_INVALID":
        result = "invalid"
    elif status == 403:
        result = "machine"
    else:
        result = "offline"
    _record(home, now, result, key, code=_error(parsed) or ("HTTP_%s" % status if status else "UNREACHABLE"))
    return result


def _record(home, now, result, key, code=None, **extra):
    def change(value):
        virtual = _parse_time(value.get("virtual"))
        value.update(last_attempt_at=_iso(now), last_result=result, last_error=(code or result.upper())[:64],
                     virtual=_iso(max(now, virtual) if virtual else now), **extra)
        if result == "invalid":
            value["refused_key"] = _key_digest(key)
    _update_record(home, change)


def check_now(home=None, relay_url=None, post=None, now=None):
    """[Check again]: one check, unless one is running ("busy") or one finished less than
    `MIN_CHECK_GAP_SECONDS` ago ("recent"); never queued."""
    last = _last_check["mono"]
    if last is not None and time.monotonic() - last < MIN_CHECK_GAP_SECONDS:
        return "recent"
    return check(home, relay_url, post, now, wait=False)


def check_message(result, value):
    """What [Check again] says afterwards, in plain words."""
    if result == "busy":
        return "A license check is already running. Its result shows in a moment."
    if result == "offline" and value.get("certificate"):   # T7
        return ("Still can't verify the license server's secure connection on this computer. Run Report a "
                "problem so we can see why.")
    if result == "offline":
        return "Still can't reach the license server. Check your internet; Sidedoor tries again every hour."
    if result in ("ok", "recent") and value["state"] in ("trial", "active", "payment_retrying"):
        return "License OK: %s." % value["label"]
    return value["text"]


class Heartbeat:
    """The heartbeat's own timer, scheduled on `time.monotonic()`. `run` checks once at once, then
    when the interval (an hour while offline) has passed or when woken."""

    def __init__(self, home=None, relay_url=None, post=None, clock=None, monotonic=None, poll_seconds=POLL_SECONDS,
                 after_tick=None):
        self.home, self.relay_url, self.post = home, relay_url, post
        self.after_tick = after_tick  # E8.T3: the license pause sync, after every step
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._next = None
        self._stepped = None
        self._checked = None  # this timer's clock at its latest check
        self.last_result = None

    def tick(self, force=False):
        """One timer step: move `virtual` on by the monotonic time since the last step, then check
        when forced or due. Returns the check's result, or None when not due."""
        mono = self.monotonic()
        try:
            if self._stepped is not None:
                advance_virtual(self.home, mono - self._stepped, self.clock())
        except Exception:
            pass  # a failed step only means `virtual` moves on at the next one
        self._stepped = mono
        requested = check_requested(self.home)
        if requested and not force:
            # "Refused now" from a job child; at most one check a minute, on this timer's own
            # clock (PW7) and after any [Check again] or activation in this process.
            last = _last_check["mono"]
            force = ((self._checked is None or mono - self._checked >= MIN_CHECK_GAP_SECONDS)
                     and (last is None or time.monotonic() - last >= MIN_CHECK_GAP_SECONDS))
        if not force and self._next is not None and mono < self._next:
            self._after()
            return None  # PW5: a refused request stays pending until a check runs
        if requested:
            take_check_request(self.home)
        self._checked = mono
        try:
            self.last_result = check(self.home, self.relay_url, self.post, self.clock())
        except Exception as error:  # a bad home or disk never kills the timer
            self.last_result = "error:%s" % type(error).__name__
        settled = self.last_result in ("ok", "invalid", "machine", "activation_limit", "no_key")
        if settled and self.last_result == "ok":
            try:  # a clock found wrong is checked again within the hour, not the next day
                settled = view(self.home, self.clock())["problem"] != "clock"
            except Exception:
                settled = False
        self._next = self.monotonic() + (INTERVAL_SECONDS if settled else RETRY_SECONDS)
        self._after()
        return self.last_result

    def _after(self):
        """E8.T3: grace runs out and `valid_until` passes with no check at all, so the license
        pause is synced on every step, not only after a check. Never kills the timer."""
        if self.after_tick is None:
            return
        try:
            self.after_tick()
        except Exception:
            pass  # e.g. the database is busy: the next step syncs again

    def run(self):
        self.tick(force=True)  # the dashboard opened: one check at once
        while not self._stop.is_set():
            woken = self._wake.wait(self.poll_seconds)
            if self._stop.is_set():
                break
            self._wake.clear()
            self.tick(force=woken)

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name="sidedoor-license-heartbeat", daemon=True)
            self._thread.start()
        return self

    def wake(self):
        self._wake.set()

    def stop(self, wait_seconds=5):
        """Stop the timer; waits for a check in progress (at most `wait_seconds`)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait_seconds)

    def alive(self):
        return self._thread is not None and self._thread.is_alive()


_current = None


def start(home=None, relay_url=None, post=None, after_tick=None):
    """Start (once per process) the heartbeat; returns it."""
    global _current
    if _current is None or not _current.alive():
        _current = Heartbeat(home, relay_url, post, after_tick=after_tick).start()
    return _current


def current():
    return _current


def stop():
    global _current
    if _current is not None:
        _current.stop()
    _current = None


def request_check(home=None):
    """Section 11 "Refused now": a relay call answered 401 LICENSE_INVALID, so run the heartbeat
    at once. Not when a check ran in the last minute (several 401s share one), not after the
    heartbeat itself answered 401 for this key (until the key changes or is activated again), and
    not for a key whose signed entitlement already says it lapsed. True when woken."""
    heartbeat = _current
    home = home if home is not None or heartbeat is None else heartbeat.home
    last = _last_check["mono"]
    if last is not None and time.monotonic() - last < MIN_CHECK_GAP_SECONDS:
        return False
    key = setup_steps.license_key(home)
    stored, record = _load(home)
    if key and (record or {}).get("refused_key") == _key_digest(key):
        return False
    claims = _claims(stored) if isinstance(stored, dict) else None
    if claims and claims.get("valid") is False:
        return False
    if heartbeat is None:
        # A job child: the heartbeat runs in the Sidedoor process; leave it a request (E8.T3).
        # PW6: one request a minute, by the request file's own time: a pending request, or one
        # left less than a minute ago, is shared by every later 401 (any child).
        if not key:
            return False
        path = setup_steps._home(home) / CHECK_REQUEST_FILE
        try:
            if time.time() - path.stat().st_mtime < MIN_CHECK_GAP_SECONDS:
                return True
        except OSError:
            pass
        try:
            setup_steps._write_private(path, _iso(_now()) + "\n")
        except OSError:
            return False
        return True
    heartbeat.wake()
    return True


def check_requested(home=None):
    """True while a job child's "Refused now" request is pending."""
    return (setup_steps._home(home) / CHECK_REQUEST_FILE).exists()


def take_check_request(home=None):
    """True (and the request is removed) when a job child left a "Refused now" request."""
    path = setup_steps._home(home) / CHECK_REQUEST_FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


PAUSE_STATES = {"expired": "expired", "grace_spent": "grace_spent"}


def pause_reason(value):
    """E8.T3 (E6.T0 section 11): the license pause's reason for a `view` result, or None.
    Expired (a signed `valid: false`, or a 401 once `valid_until` passed) and grace spent pause;
    so does a 403 MACHINE_NOT_ACTIVATED once `valid_until` passes (banner 3 stays the action)."""
    if value["state"] in PAUSE_STATES:
        return PAUSE_STATES[value["state"]]
    if value["state"] == "license_problem" and value["problem"] == "machine" and value["hours_left"] == 0:
        return "machine"
    return None


# --- the state the pages show ------------------------------------------------------------------

def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def view(home=None, now=None):
    """The license state for the header chip, Overview health, Settings > License and the banners.

    {"state": no_license | checking | trial | active | payment_retrying | grace | grace_spent |
     expired | license_problem, "problem": None | invalid | machine | activation_limit | clock |
     signature, "usable": bool (signed, valid, before valid_until by server time, clock right:
     Setup step 1's "done"), "label", "tone", "href" (the chip), "text" (plain words), "action"
     (the main way forward: {"label", "href"} or {"label", "post"}; None when nothing is needed),
     "actions" (every way forward, for Settings > License), "support" (a support line or None),
     "trial_days_left", "trial_end", "offline", "hours_left", "valid_until", "last_check" (signed
     server time of the last good check), "last_attempt", "status" (Stripe's)}. Reads files only;
     never calls the relay."""
    local = _now(now)
    key = setup_steps.license_key(home)
    stored, record = _load(home)
    result = {"state": "no_license", "problem": None, "usable": False, "trial_days_left": None, "trial_end": None,
              "offline": False, "hours_left": None, "valid_until": None, "last_check": None, "last_attempt": None,
              "status": None}
    if not key:
        waiting = setup_steps.keys_waiting(home)
        if waiting:
            # E2.T4 (KR1): the key is there but the keychain can't be read now: never "No license
            # yet". The license step (step 1) stays as it was (its saved record), nothing pauses on it (the scheduler
            # already holds every job while the keys wait).
            done = setup_steps.load_state(home)["steps"].get("license", {}).get("status") == "done"
            return _dress(dict(result, state="keychain", usable=done, waiting=waiting))
        return _dress(result)
    if not isinstance(stored, dict):  # a key with a missing, truncated or unreadable entitlement
        return _dress(dict(result, state="license_problem", problem="signature"))
    value = check_signed(stored, key, machine_id(home))
    migrating = False
    if value is None:
        claimed = (_claims(stored) or {}).get("machine")
        legacy = setup_steps.file_machine_id(home, create=False)
        if claimed and claimed == legacy:
            value = check_signed(stored, key, claimed)  # activated before the hardware id
            migrating = value is not None
        if value is None:
            record = record or {}
            if (claimed and record.get("reactivated_from") == claimed
                    and record.get("last_result") == "activation_limit"
                    and check_signed(stored, key, claimed) is not None):
                # A home copied from another Mac (LG1): its plain activation got 409 ACTIVATION_LIMIT.
                # The same limit message and [Activate] as the 409 path, not "couldn't verify".
                return _dress(dict(result, state="license_problem", problem="activation_limit",
                                   last_attempt=record.get("last_attempt_at"), _copied=True))
            return _dress(dict(result, state="license_problem", problem="signature"))
    server = _parse_time(value.get("server_time"))
    valid_until = _parse_time(value.get("valid_until"))
    result.update(valid_until=_iso(valid_until), last_check=_iso(server), status=value.get("status"))
    record = record or {}
    result["last_attempt"] = record.get("last_attempt_at")
    if migrating and record.get("last_result") == "activation_limit":
        return _dress(dict(result, state="license_problem", problem="activation_limit"))
    received = _parse_time(record.get("received_at"))
    virtual = _parse_time(record.get("virtual"))
    if (local < server - CLOCK_TOLERANCE or (virtual and local < virtual - CLOCK_TOLERANCE)
            or (received and abs(received - server) > CLOCK_TOLERANCE)):
        return _dress(dict(result, state="license_problem", problem="clock"))
    if received is None or migrating:
        # No anchor of our own (the first run after an upgrade, both record copies gone, or a
        # re-activation pending): nothing is extended (now is at least the signed server time).
        estimated = max(local, server, virtual or local)
        last_result = record.get("last_result")
        if not last_result:  # no check has finished yet: the heartbeat runs one at once
            left = (valid_until - estimated).total_seconds()
            return _dress(dict(result, state="checking", usable=value["valid"] is True and left > 0,
                               hours_left=max(0, math.ceil(left / 3600))))
    else:
        estimated = server + max(max(virtual or local, local) - received, dt.timedelta(0))
        attempt = _parse_time(record.get("last_attempt_at"))
        last_result = record.get("last_result") if attempt and attempt >= received else None
    left = (valid_until - estimated).total_seconds()
    result["hours_left"] = max(0, math.ceil(left / 3600))
    result["offline"] = last_result == "offline"
    result["certificate"] = result["offline"] and record.get("last_error") == CERTIFICATE   # T7
    result["usable"] = value["valid"] is True and left > 0
    if last_result == "machine":
        return _dress(dict(result, state="license_problem", problem="machine"))
    if value["valid"] is not True or (last_result == "invalid" and left <= 0):
        return _dress(dict(result, state="expired", usable=False))
    if last_result == "invalid":
        return _dress(dict(result, state="license_problem", problem="invalid"))
    if left <= 0:
        return _dress(dict(result, state="grace_spent"))
    if left <= GRACE_WARNING.total_seconds():
        return _dress(dict(result, state="grace"))
    status = value.get("status")
    if status == "trialing":
        trial_end = _parse_time(value.get("trial_end"))
        if trial_end:
            result.update(trial_end=_iso(trial_end),
                          trial_days_left=max(0, math.ceil((trial_end - estimated).total_seconds() / 86400)))
        return _dress(dict(result, state="trial"))
    if status == "past_due":
        return _dress(dict(result, state="payment_retrying"))
    return _dress(dict(result, state="active"))


CHECK_AGAIN = {"label": "Check again", "post": CHECK_PATH}
OPEN_LICENSE = {"label": "Open License", "href": SETTINGS_LICENSE}
RENEW = {"label": "Renew", "href": setup_steps.WEBSITE_URL, "external": True}
CLOCK_TEXT = ("License problem: your computer's clock looks wrong. Set your date and time to update automatically, "
              "then click Check again.")
SUPPORT_LINE = "Paid and still seeing this? Email %s." % setup_steps.SUPPORT_EMAIL


def _dress(result):
    state, problem = result["state"], result["problem"]
    copied = result.pop("_copied", False)
    hours = result["hours_left"]
    offline_note = (" Couldn't check the license server; %s left." % _plural(hours, "hour")
                    if result["offline"] and hours is not None else "")
    actions, support = None, None
    if state == "keychain":
        text = result.pop("waiting")
        label = "Keychain locked" if text.startswith("Keychain locked") else "Waiting for keychain"
        tone, href, action = "warning", SETTINGS_LICENSE, None
    elif state == "no_license":
        label, tone, href = "No license yet", "warning", "/setup#step-1"
        text, action = "No license yet.", {"label": "Enter license key", "href": SETTINGS_LICENSE}
    elif state == "checking":
        label, tone, href, action = "Checking your license…", "info", SETTINGS_LICENSE, None
        text = "Checking your license…"
    elif state == "trial":
        days = result["trial_days_left"]
        if days is None:
            label = "Trial"
        elif days == 0:
            label = "Trial: ends today"
        else:
            label = "Trial: %s left" % _plural(days, "day")
        tone, href, action = "info", SETTINGS_LICENSE, None
        text = label + "." + offline_note
    elif state == "active":
        label, tone, href, action = "Active", "success", SETTINGS_LICENSE, None
        text = "Active." + offline_note
    elif state == "payment_retrying":
        label, tone, href, action = "Payment retrying", "warning", SETTINGS_LICENSE, OPEN_LICENSE
        text = ("Your last payment didn't go through and Stripe is retrying it. Sidedoor keeps working meanwhile; "
                "update your card with Manage subscription in Settings > License to keep it running." + offline_note)
        actions = []
    elif state == "grace":
        label, tone, href, action = "Offline, %s left" % _plural(hours, "hour"), "warning", SETTINGS_LICENSE, CHECK_AGAIN
        if result.get("certificate"):   # T7: not the internet
            text = ("Sidedoor couldn't verify the license server's secure connection on this computer. It keeps "
                    "working for %s more. Run Report a problem so we can see why." % _plural(hours, "hour"))
        else:
            text = ("Sidedoor couldn't reach the license server. It keeps working for %s more; check your internet."
                    % _plural(hours, "hour"))
    elif state == "grace_spent":
        label, tone, href, action = "Offline, paused", "error", SETTINGS_LICENSE, CHECK_AGAIN
        if result.get("certificate"):   # T7
            text = ("Sidedoor is paused: it couldn't verify the license server's secure connection on this computer "
                    "for 3 days. Run Report a problem so we can see why; Sidedoor resumes on its own once it connects.")
        else:
            text = ("Sidedoor could not reach the license server for 3 days. Check your internet; Sidedoor resumes on "
                    "its own once it connects.")
    elif state == "expired":
        label, tone, href, action = "Expired", "error", SETTINGS_LICENSE, RENEW
        text = "Sidedoor is paused. Renew to continue."
    elif problem == "invalid":
        label, tone, href, action = "License problem", "error", SETTINGS_LICENSE, OPEN_LICENSE
        text = "Your subscription isn't active. Check Settings > License."
        actions, support = [RENEW, CHECK_AGAIN], SUPPORT_LINE
    elif problem == "machine":
        label, tone, href = "License problem", "error", SETTINGS_LICENSE
        text = "This computer is not activated on your key. Activate it again in Settings > License."
        action = {"label": "Activate", "href": SETTINGS_LICENSE}
        actions = []  # the section's own [Activate] button
    elif problem == "activation_limit":
        label, tone, href = "License problem", "error", SETTINGS_LICENSE
        text = ("This key is already used on 2 computers. Email %s to free a computer, then click Activate in "
                "Settings > License." if copied else
                "Sidedoor now recognises this computer by its hardware, and your key is already used on 2 computers. "
                "Email %s to free a computer, then click Activate in Settings > License.") % setup_steps.SUPPORT_EMAIL
        action, actions = {"label": "Activate", "href": SETTINGS_LICENSE}, []
    elif problem == "clock":
        label, tone, href, action = "License problem", "error", SETTINGS_LICENSE, CHECK_AGAIN
        text = CLOCK_TEXT
    else:  # signature: an entitlement that is missing, unreadable or not signed for this key and computer
        label, tone, href, action = "License problem", "error", SETTINGS_LICENSE, CHECK_AGAIN
        text = ("License problem: Sidedoor couldn't read or verify the license saved on this computer. Click Check "
                "again to fetch it from the license server.")
    if actions is None:
        actions = [action] if action else []
    result.update(label=label, tone=tone, href=href, text=text, action=action, actions=actions, support=support)
    return result


RAW_TIME = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)")


def local_message(saved, config, now=None):
    """The license step's saved record with any raw UTC time in its message in the user's time
    zone, as Overview writes times (LA4: a message an older build stored read "Active: Trial. Good
    until 2026-10-26T01:06:27Z."). Setup step 1 and Settings > License show it through this."""
    message = (saved or {}).get("message")
    if not isinstance(message, str) or not RAW_TIME.search(message):
        return saved
    from zoneinfo import ZoneInfo
    import overview_page
    try:
        zone = ZoneInfo((config or {}).get("timezone") or "America/Los_Angeles")
    except (ValueError, KeyError, OSError):
        zone = ZoneInfo("America/Los_Angeles")
    now = _now(now)

    def local(match):
        return overview_page.when(overview_page._utc(match.group(0)), now, zone) or match.group(0)
    return dict(saved, message=RAW_TIME.sub(local, message))


def chip(home=None, now=None):
    """The header's License chip: {"label", "tone", "href"}."""
    value = view(home, now)
    return {"label": value["label"], "tone": value["tone"], "href": value["href"]}


BANNERS = {"expired", "grace_spent", "license_problem"}


def banner(value):
    """Banner 1, 1b, 2 or 3 of E6.T0 section 2 (and the clock, signature and activation-limit
    problems) for a `view` result, or None. Never while no key is stored (Setup's banner covers
    that time) or while the first check is running."""
    if value["state"] not in BANNERS:
        return None
    second = None
    if value["state"] == "expired":
        second = {"label": "Bought a new key? Paste it here", "href": SETTINGS_LICENSE}
    text = "This computer is not activated on your key." if value["problem"] == "machine" else value["text"]
    action = value["action"]
    return {"tone": "error", "text": text, "action_label": action["label"], "action_href": action.get("href"),
            "action_post": action.get("post"), "external": bool(action.get("external")), "second": second}
