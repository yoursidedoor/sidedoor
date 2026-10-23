"""A test signing key and a fake relay for the license clock (E8.T2). Offline: no test reaches
the real relay. Signing is RFC 8032 Ed25519 in pure Python, the inverse of
`license_clock.ed25519_verify` (checked against `cryptography` once when this was written)."""

import base64
import datetime as dt
import hashlib
import json
from unittest import mock

import license_clock as lc
import setup_steps


SECRET = hashlib.sha256(b"sidedoor test signing key, never shipped").digest()


def _expand(secret):
    digest = hashlib.sha512(secret).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def _compress(point):
    inverse = pow(point[2], lc._P - 2, lc._P)
    x, y = point[0] * inverse % lc._P, point[1] * inverse % lc._P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def public_key(secret=SECRET):
    scalar, _ = _expand(secret)
    return _compress(lc._multiply(scalar, lc._G))


def sign_bytes(message, secret=SECRET):
    scalar, prefix = _expand(secret)
    public = _compress(lc._multiply(scalar, lc._G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % lc._Q
    point_r = _compress(lc._multiply(r, lc._G))
    h = int.from_bytes(hashlib.sha512(point_r + public + message).digest(), "little") % lc._Q
    return point_r + int.to_bytes((r + h * scalar) % lc._Q, 32, "little")


PUBLIC = base64.b64encode(public_key()).decode("ascii")


HARDWARE = "00000000-TEST-4000-8000-000000000000"


def use_test_key(hardware=HARDWARE):
    """Patch the shipped public key with the test key and this computer's hardware id with a fixed
    one (a context manager / `start()`able patch)."""
    return mock.patch.multiple(lc, PUBLIC_KEY=PUBLIC, hardware_id=lambda: hardware)


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signed(key, machine, server_time, status="active", valid=True, valid_until=None, **extra):
    """What the relay's `licenses.entitlement` + `Signer.sign` return, signed with the test key."""
    if isinstance(server_time, str):
        server_time = dt.datetime.fromisoformat(server_time.replace("Z", "+00:00"))
    until = valid_until or (server_time + dt.timedelta(hours=72) if valid else server_time)
    if isinstance(until, dt.datetime):
        until = iso(until)
    value = {"license_sha256": hashlib.sha256(key.encode()).hexdigest(), "machine": machine, "status": status,
             "valid": valid, "server_time": iso(server_time), "valid_until": until}
    value.update({name: iso(item) if isinstance(item, dt.datetime) else item for name, item in extra.items()})
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return {"entitlement": text, "signature": base64.b64encode(sign_bytes(text.encode())).decode("ascii")}


def install(home, key="SD-TESTKEY", status="active", valid=True, server_time=None,
            received_at=None, valid_until=None, **extra):
    """Store a key, a machine id and a signed entitlement in `home`, as Setup step 5 does
    (server time and receipt default to this computer's now)."""
    server_time = server_time or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    (home / "license.key").write_text(key + "\n")
    machine = setup_steps.machine_id(home)
    value = signed(key, machine, server_time, status=status, valid=valid, valid_until=valid_until, **extra)
    when = received_at or server_time
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    lc.store_signed(home, value, when)
    return value


class FakeRelay:
    """`post(url, body)` for `/license/heartbeat` and `/license/activate`, with a settable clock
    (the relay's own time) and answer. Records every call; never touches the network."""

    def __init__(self, clock, status="active", trial_end=None):
        self.clock = clock          # callable -> aware datetime: the relay's (true) time
        self.status = status
        self.trial_end = trial_end
        self.mode = "ok"            # ok | down | invalid | machine | garbage | 500 | limit | proxy401 | incomplete
        self.calls = []
        self.slots = None           # a set: the relay's activated machines for the key, as relay/licenses.py keeps them

    def __call__(self, url, body, timeout=None):
        self.calls.append((url, dict(body, key="<redacted>")))
        if self.mode == "down":
            raise OSError("unreachable")
        if self.mode == "invalid":
            return 401, b'{"error": "LICENSE_INVALID"}'
        if self.mode == "machine":
            return 403, b'{"error": "MACHINE_NOT_ACTIVATED"}'
        if self.mode == "limit" and url.endswith("/license/activate"):
            return 409, b'{"error": "ACTIVATION_LIMIT"}'
        if self.mode == "proxy401":
            return 401, b'<html>Unauthorized</html>'
        if self.mode == "incomplete":
            import http.client
            raise http.client.IncompleteRead(b"{")
        if self.mode == "garbage":
            return 200, b'{"entitlement": "{}", "signature": "AAAA"}'
        if self.mode == "500":
            return 500, b'{"error": "RELAY_ERROR"}'
        if self.slots is not None:
            machine, replaces = body["machine"], body.get("replaces")
            if url.endswith("/license/activate"):
                if replaces and replaces != machine:
                    if machine in self.slots:
                        self.slots.discard(replaces)
                    elif replaces not in self.slots:
                        return 409, b'{"error": "REPLACES_NOT_ACTIVATED"}'
                    else:
                        self.slots.discard(replaces)
                        self.slots.add(machine)
                elif machine not in self.slots:
                    if len(self.slots) >= 2:
                        return 409, b'{"error": "ACTIVATION_LIMIT"}'
                    self.slots.add(machine)
            elif machine not in self.slots:
                return 403, b'{"error": "MACHINE_NOT_ACTIVATED"}'
        now = self.clock()
        status = self.status(now) if callable(self.status) else self.status
        extra = {"trial_end": self.trial_end} if self.trial_end and status == "trialing" else {}
        valid = status in ("active", "trialing", "past_due")
        return 200, json.dumps(signed(body["key"], body["machine"], now, status=status, valid=valid, **extra)).encode()
