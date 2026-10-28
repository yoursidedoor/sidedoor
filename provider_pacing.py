"""Pacing of AI session starts, per model provider, shared by every Sidedoor process (owner, 2026-10-27).

Before each AI session launch the caller takes a slot (`slot`); the session runs; the slot is released.
Two rules, nothing else:

(a) Adaptive concurrency. A provider starts unlimited (no fixed cap: the owner rejected fixed caps). When
    the provider answers "429 Too Many Requests" (`provider_limits` recognises it and calls
    `rate_limited`), the allowed concurrency drops to max(1, sessions running // 2) and a cool-down
    applies: no new session starts for that provider until it ends. The cool-down is the same short
    retry the waiting work gets (30 s, 60 s, 120 s, 240 s, capped at 300 s, or the provider's stated
    retry time). Each session that succeeds (`succeeded`, through `openai_auth.succeeded`) raises the
    allowed concurrency by 1 and ends the 429 streak; an hour with no 429 returns it to unlimited.
(b) Spaced starts. At least MIN_GAP_SECONDS between two session starts for the same provider.

The state is a small JSON file in the Sidedoor home's state folder, read and written under
state/provider-pacing.lock (run_locks.held: the repo's portable flock / msvcrt lock). Each running session
holds a lease naming its process and an expiry, so a crashed process never keeps a slot: a lease whose
process is gone, or whose expiry passed, is dropped. Waiting for a slot happens before the session's own
clock starts (it never counts against a session's time limit, only the wall clock) and the lock is held
only for the read-modify-write, never while waiting. Pacing is best effort: when the state cannot be read
or written the session starts as before, so pacing never turns into a failure.

Providers are keys ("openai", "openrouter"; another engine such as Claude or Antigravity adds its own key).
"""

import contextlib
import json
import os
import time
import uuid

import paths
import portable

STATE_FILE = "provider-pacing.json"
LOCK_FILE = "provider-pacing.lock"
MIN_GAP_SECONDS = 3.0
GAP_VARIABLE = "SIDEDOOR_PACING_GAP_SECONDS"   # the tests' homes set 0 (tests/profile_home.py)
RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 300
CALM_SECONDS = 3600            # an hour with no 429: unlimited again
LEASE_MARGIN_SECONDS = 600     # a lease outlives its session's own limit by this much
DEFAULT_LEASE_SECONDS = 4 * 3600
POLL_SECONDS = 0.5
FAILURES = (OSError, TypeError, ValueError)   # unreadable or unwritable state: pacing steps aside
_clock = time.time   # the wall clock (tests replace it)
_root = None   # tests point the pacer at their own folder; None: the Sidedoor home


def provider_of(command):
    """The pacing key for an engine command: the key of the engine that built it (engines.for_command,
    Engine.provider_key; Codex: "openrouter" when it is pointed at OpenRouter, else "openai")."""
    import engines
    return engines.for_command(command).provider_key(command)


def retry_seconds(streak):
    """The short retry after the `streak`-th 429 in a row: 30, 60, 120, 240, then 300 s."""
    return min(RETRY_SECONDS * 2 ** max(0, streak - 1), MAX_RETRY_SECONDS)


def _gap():
    try:
        return max(0.0, float(os.environ.get(GAP_VARIABLE, MIN_GAP_SECONDS)))
    except ValueError:
        return MIN_GAP_SECONDS


def _path(root):
    return paths.state(root) / STATE_FILE


def _load(root):
    try:
        value = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(root, state):
    path = _path(root)
    temporary = path.with_name(path.name + ".%d.tmp" % os.getpid())
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _lease(lease, now):
    """A lease cleaned against `now` (a start in the future, from a clock moved back, is moved to now), or
    None when it is malformed, expired or its process is gone."""
    if not isinstance(lease, dict):
        return None
    started, seconds = _number(lease.get("started"), None), _number(lease.get("seconds"), None)
    expires = _number(lease.get("expires"), None)
    if started is None or expires is None:
        return None
    if seconds is None or seconds <= 0:
        seconds = expires - started
    if started > now:
        started = now
    expires = min(expires, started + seconds)
    if expires <= now or not _alive(lease.get("pid")):
        return None
    return dict(lease, started=started, expires=expires, seconds=seconds)


def _entry(state, provider, now):
    """The provider's record cleaned against `now`: malformed fields reset, dead or expired leases dropped,
    times a clock moved back left in the future pulled to now, and the calm hour applied."""
    entry = state.setdefault(provider, {})
    if not isinstance(entry, dict):
        entry = state[provider] = {}
    leases = entry.get("leases") if isinstance(entry.get("leases"), dict) else {}
    entry["leases"] = {key: cleaned for key, cleaned in ((key, _lease(lease, now)) for key, lease in leases.items())
                       if cleaned is not None}
    entry["streak"] = int(_number(entry.get("streak")))
    entry["last_start"] = min(_number(entry.get("last_start")), now)
    last_429 = _number(entry.get("last_429"))
    cooldown = max(0.0, _number(entry.get("cooldown_seconds")))
    if last_429 > now:
        last_429 = now
    entry["last_429"] = last_429
    entry["cooldown_until"] = min(_number(entry.get("cooldown_until")), last_429 + cooldown)
    limit = entry.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        limit = None
    entry["limit"] = None if limit is None or now - last_429 >= CALM_SECONDS else limit
    return entry


def _alive(pid):
    try:
        return pid == os.getpid() or portable.pid_alive(int(pid))
    except (TypeError, ValueError, OSError):
        return False


@contextlib.contextmanager
def _locked(root):
    import run_locks
    with run_locks.held(root, LOCK_FILE, wait_seconds=30, poll_seconds=0.05) as held:
        if not held:
            raise OSError("PROVIDER_PACING_LOCK_BUSY")
        yield


def _update(root, change):
    with _locked(root):
        state = _load(root)
        result = change(state, _clock())
        _save(root, state)
        return result


def _try_take(provider, lease_seconds):
    """A lease id when a session may start now, else (None, seconds to wait before looking again)."""
    def change(state, now):
        entry = _entry(state, provider, now)
        waits = [entry["cooldown_until"] - now, entry["last_start"] + _gap() - now]
        limit = entry.get("limit")
        if limit is not None and len(entry["leases"]) >= int(limit):
            waits.append(POLL_SECONDS)
        wait = max(waits)
        if wait > 0:
            return None, wait
        lease = uuid.uuid4().hex
        entry["leases"][lease] = {"pid": os.getpid(), "started": now, "expires": now + lease_seconds, "seconds": lease_seconds}
        entry["last_start"] = now
        return lease, 0
    return _update(_root, change)


class SlotWaitTooLong(Exception):
    """No slot within `max_wait`: the caller treats it as the short rate-limit wait (`provider_limits.
    slot_wait`), never as a failed try. `left` is the pacer's own estimate of the wait still ahead."""

    def __init__(self, provider, left):
        super().__init__("PROVIDER_SLOT_WAIT_TOO_LONG")
        self.provider, self.left = provider, left


def acquire(provider, lease_seconds=None, stop=None, sleep=time.sleep, max_wait=None):
    """Wait for a slot for `provider`; the lease id (None when pacing could not be used: start anyway).
    `stop`: a callable; when it turns true the wait ends with InterruptedError. `max_wait`: seconds; past
    it the wait ends with SlotWaitTooLong."""
    lease_seconds = (lease_seconds or DEFAULT_LEASE_SECONDS) + LEASE_MARGIN_SECONDS
    waited = 0.0
    while True:
        if stop is not None and stop():
            raise InterruptedError("Stopped while waiting for a provider slot")
        try:
            lease, wait = _try_take(provider, lease_seconds)
        except FAILURES:
            return None
        if lease is not None:
            return lease
        if max_wait is not None and waited >= max_wait:
            raise SlotWaitTooLong(provider, wait)
        step = min(wait, POLL_SECONDS)
        sleep(step)
        waited += step


def release(provider, lease):
    if lease is None:
        return
    def change(state, now):
        _entry(state, provider, now)["leases"].pop(lease, None)
    try:
        _update(_root, change)
    except FAILURES:
        pass


class Slot:
    """One session's slot: `waited` seconds were spent waiting for it; `release` is safe to call twice."""

    def __init__(self, provider, lease, waited):
        self.provider, self.lease, self.waited = provider, lease, waited

    def release(self):
        lease, self.lease = self.lease, None
        release(self.provider, lease)


def take(command, lease_seconds=None, stop=None, provider=None, max_wait=None):
    """Wait for and hold a pacing slot for one AI session started with `command` (or for the pacing key
    `provider`): a Slot to release. A wait longer than `max_wait` raises the short rate-limit wait
    (provider_limits.ProviderUsageLimit, kind RATE_LIMITED) chained from SlotWaitTooLong."""
    provider = provider or provider_of(command)
    started = time.monotonic()
    try:
        lease = acquire(provider, lease_seconds, stop, max_wait=max_wait)
    except SlotWaitTooLong as busy:
        import provider_limits
        raise provider_limits.slot_wait(busy) from busy
    return Slot(provider, lease, time.monotonic() - started)


@contextlib.contextmanager
def slot(command, lease_seconds=None, stop=None, provider=None):
    """`take` as a context: the slot is released however the session ends."""
    held = take(command, lease_seconds, stop, provider=provider)
    try:
        yield held
    finally:
        held.release()


# An application browser session (application_browser / application_connection) is paced under its engine's
# own key (engines.Engine.pacing_provider; Codex: its command always sets model_provider="openai"); it waits
# at most BROWSER_SLOT_WAIT_SECONDS, never holding the browser lock.
BROWSER_SLOT_WAIT_SECONDS = 60
# While a browser session holds its slot it waits only this long for state/browser.lock; a busy browser then
# releases the slot at once and the application retries later as a capacity wait (no try spent), so a held
# browser never holds off every other session of the provider (2026-10-27).
BROWSER_LOCK_WAIT_SECONDS = 5
BROWSER_LEASE_SECONDS = 3600   # past any browser session's ceiling; only a crashed holder ever reaches it


def browser_slot(config):
    """A slot for one application browser session started with `config` (a Slot), or the short rate-limit
    wait raised."""
    import engines
    return take(None, BROWSER_LEASE_SECONDS, provider=engines.engine(config).pacing_provider,
                max_wait=BROWSER_SLOT_WAIT_SECONDS)


def rate_limited(provider, stated_seconds=None):
    """Record a 429 from `provider`: halve the allowed concurrency (at least 1) and start the cool-down.
    Returns the retry delay in seconds: the provider's stated wait when given, else the streak's backoff."""
    def change(state, now):
        entry = _entry(state, provider, now)
        entry["streak"] += 1
        delay = stated_seconds if stated_seconds is not None else retry_seconds(entry["streak"])
        entry["limit"] = max(1, len(entry["leases"]) // 2)
        entry["last_429"] = now
        if now + delay >= entry["cooldown_until"]:
            entry["cooldown_until"], entry["cooldown_seconds"] = now + delay, delay
        return delay
    try:
        return _update(_root, change)
    except FAILURES:
        return stated_seconds if stated_seconds is not None else RETRY_SECONDS


def succeeded(provider):
    """A session for `provider` ended well: one more allowed at once (when limited); the 429 streak ends."""
    def change(state, now):
        entry = _entry(state, provider, now)
        entry["streak"] = 0
        if entry.get("limit") is not None:
            entry["limit"] = int(entry["limit"]) + 1
    try:
        _update(_root, change)
    except FAILURES:
        pass


def status(provider):
    """{"limit": None (unlimited) or int, "running": int, "streak": int, "cooldown_until": epoch or 0}."""
    try:
        entry = _entry(_load(_root), provider, _clock())
    except FAILURES:
        entry = {"leases": {}}
    return {"limit": entry.get("limit"), "running": len(entry["leases"]), "streak": int(entry.get("streak") or 0),
            "cooldown_until": float(entry.get("cooldown_until") or 0)}
