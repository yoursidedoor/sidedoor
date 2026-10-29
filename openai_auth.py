"""Codex signed in with an OpenAI API key (E0.T8, an option next to Sign in with ChatGPT).

Sign in with ChatGPT stays the default and is left exactly as it was: every Codex command keeps
`forced_login_method="chatgpt"`, research keeps stripping OPENAI_API_KEY / CODEX_API_KEY from its
environment, and `provider_limits` reads only the ChatGPT plan's usage-limit line.

When the user chose "Use an OpenAI API key" in Setup step 6 (or Settings > Connections), and only
then, for a Codex command that goes to OpenAI (not one `text_model.command` pointed at OpenRouter):
- `command` drops the `-c forced_login_method="chatgpt"` pair (owner, 2026-10-22, OA1 rejected:
  "ChatGPT login or API is fine and both are supported"), so either credential can work; nothing
  is added where a command had no forced login (research);
- `environment` gives that Codex process the saved key as CODEX_API_KEY (the variable `codex exec`
  reads). Ambient OPENAI_* variables of the user's shell are still stripped by research. Codex
  starts its MCP servers (the browser guard, reply_tools) with a filtered environment (its default
  list: HOME, PATH, LANG, TMPDIR, ... plus the server's own `env`), and no server config here names
  CODEX_API_KEY, so the key reaches codex only (tests/test_openai_auth.py checks the configs);
- `provider_limits` classifies the terminal error of that call: insufficient_quota is the plan
  limit's capacity wait (no attempt spent); a rate limit (429) is the short "too many requests" wait
  (provider_limits.RATE_LIMITED, owner 2026-10-27), never recorded here. Owner OA2 (revised, option a):
  when the key is refused (401), or out of credit past ESCALATE_HOURS of waits with no success
  between them, and `codex login status` (local, no network) reports a ChatGPT login, Sidedoor
  falls back to it: `fallback` is recorded (FALLBACK_FILE: reason and time, the audit of the
  switch), and every later call is the ChatGPT call exactly (forced ChatGPT login kept, no
  CODEX_API_KEY) until a key passes its test (`release`), when the key is used again; `codex_auth`
  stays "api_key" throughout. Only when no ChatGPT login works either is the scheduler held
  (HOLD_FILE): every job waits until a key passes its test. Out of credit with no ChatGPT login
  waits like a rate limit until the day bound. No attempt is spent on any of these paths. A
  successful call to OpenAI ends a wait streak (`succeeded`).

The choice is `codex_auth` in setup-state.json ("chatgpt" or "api_key"). The key is `openai.key`
(0600) in the Sidedoor home, beside `openrouter.key`; a candidate key is tested in memory and
written only once it passes (AK1), so a working key is never replaced by a refused one.

E2.T4 (credentials.py): the key is kept like the OpenRouter key, through
`credentials.get/put/delete("openai", home)` (the OS store, else the 0600 `openai.key`), so it is
moved into the store at start, redacted with the other keys and removed by "Remove Sidedoor's saved
keys". While the store is locked or unavailable `saved_key` raises (credentials.Locked /
Unavailable, never "no key": the scheduler holds on `credentials.hold_reason`, Setup and Settings
show the waiting line), a save is refused with nothing written and a removal leaves the key as it
was. `openai.key.tested` stays a file.
"""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid

import user_profile


CHATGPT, API_KEY = "chatgpt", "api_key"
STATE_FILE = "setup-state.json"          # setup_steps.STATE_FILE
KEY_FILE = "openai.key"
TESTED_FILE = "openai.key.tested"
WAIT_FILE = "openai-api-wait.json"       # the last API billing wait, for the banner and doctor
ENV_VARIABLE = "CODEX_API_KEY"
FORCED_LOGIN = 'forced_login_method="chatgpt"'
HOLD_FILE = "openai-api-hold.json"       # the scheduler hold: the key failed and no ChatGPT login works
FALLBACK_FILE = "openai-api-fallback.json"   # the key failed; Codex calls use the ChatGPT sign-in
ESCALATE_HOURS = 24
OPENROUTER_PROVIDER = 'model_provider="openrouter"'
API_URL = "https://api.openai.com/v1"
BANNER_HOURS = 24

OUT_OF_CREDIT, RATE_LIMIT, KEY_REFUSED = "api_out_of_credit", "api_rate_limit", "api_key_refused"
WORDS = {OUT_OF_CREDIT: "Your OpenAI API key is out of credit.",
         RATE_LIMIT: "OpenAI is rate-limiting your API key.",
         KEY_REFUSED: "OpenAI did not accept your API key."}
TODO = {OUT_OF_CREDIT: "Add credit to your OpenAI account (platform.openai.com > Billing); Sidedoor carries on by itself.",
        "held_" + OUT_OF_CREDIT: "Add credit to your OpenAI account, then click Test in Settings > Connections.",
        "held_" + RATE_LIMIT: "Click Test in Settings > Connections to carry on.",
        RATE_LIMIT: "Nothing to do: Sidedoor waits and carries on by itself.",
        KEY_REFUSED: "Replace the key in Settings > Connections."}


def _home(home=None):
    return Path(home) if home is not None else user_profile.home()


_mode_cache = {}


def mode(home=None):
    """CHATGPT unless the user chose the API key. Unreadable or missing state is CHATGPT."""
    path = _home(home) / STATE_FILE
    try:
        stat = path.stat()
    except OSError:
        return CHATGPT
    # The file id too: setup_steps replaces the file atomically, and on Windows two writes inside one
    # clock tick keep the same mtime ("api_key" and "chatgpt" are the same length; VM pass, 2026-10-23).
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _mode_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CHATGPT
    chosen = API_KEY if isinstance(value, dict) and value.get("codex_auth") == API_KEY else CHATGPT
    _mode_cache[str(path)] = (stamp, chosen)
    return chosen


def api_key_mode(home=None):
    try:
        return mode(home) == API_KEY
    except Exception:  # a home that cannot be resolved decides nothing: ChatGPT, as before
        return False


def openai_bound(arguments, home=None):
    """True for a Codex command that goes to OpenAI with the API key: the key is chosen, the command is
    not pointed at OpenRouter, and no ChatGPT fallback is in force (that call is a ChatGPT call)."""
    return (arguments is not None and OPENROUTER_PROVIDER not in arguments and api_key_mode(home)
            and fallback(home) is None)


def command(arguments, home=None):
    """The Codex command for the chosen sign-in. ChatGPT (or the ChatGPT fallback): `arguments` itself."""
    if not openai_bound(arguments, home):
        return arguments
    result = []
    for argument in arguments:
        if argument == FORCED_LOGIN and result and result[-1] == "-c":
            result.pop()
            continue
        result.append(argument)
    return result


def environment(arguments, variables, home=None):
    """`variables` plus the saved key as CODEX_API_KEY in API-key mode (OSError when it is missing).
    ChatGPT mode, the ChatGPT fallback, or a command pointed at OpenRouter: `variables` itself."""
    if not openai_bound(arguments, home):
        return variables
    key = saved_key(home)
    if not key:
        raise OSError("OPENAI_API_KEY_MISSING: no OpenAI API key is saved (Settings > Connections)")
    return dict(variables, **{ENV_VARIABLE: key})


def succeeded(arguments, home=None):
    """A Codex call to OpenAI exited 0: its billing-wait streak and banner end (AK5). Best effort.
    Every Codex call that exited 0 also ends the ChatGPT usage-limit banner, or, pointed at OpenRouter,
    the OpenRouter out-of-credit one (`provider_limits.succeeded`)."""
    try:
        import provider_limits
        provider_limits.succeeded(arguments, home)
    except Exception:
        pass
    try:
        if openai_bound(arguments, home):
            clear_wait(home)
    except Exception:
        pass


# --- the key -----------------------------------------------------------------------------------------

def _write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def saved_key(home=None):
    """The saved OpenAI API key (credentials.get: the OS store, or openai.key), else None. Raises
    credentials.Locked / Unavailable (OSError) while the store cannot answer: never "missing"."""
    import credentials
    return credentials.get("openai", _home(home))


def shown_key(home=None):
    """For display only: the saved key, or None while the store waits (the waiting line is shown)."""
    try:
        return saved_key(home)
    except OSError:
        return None


def save_key(key, home=None, to_file=False):
    """credentials.put (refused with nothing written while the store is locked or unavailable:
    credentials.Unavailable raised), or the folder when the user chose it while it is offered (KC1)."""
    import credentials
    home = _home(home)
    if to_file and credentials.offer_file(home):
        credentials.put_file("openai", key, home)
    else:
        credentials.put("openai", key, home)
    (home / TESTED_FILE).unlink(missing_ok=True)


def remove_key(home=None):
    """credentials.delete: a refused store delete raises and leaves the key and its test mark."""
    import credentials
    home = _home(home)
    credentials.delete("openai", home)
    (home / TESTED_FILE).unlink(missing_ok=True)


def mark_tested(key, home=None, passed=True):
    _write_private(_home(home) / TESTED_FILE, json.dumps({"key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                                                         "passed": bool(passed)}) + "\n")


def test_state(home=None):
    """"passed", "failed" (the saved key's last test did not pass) or "never" (not tested yet)."""
    key = saved_key(home)
    try:
        marker = json.loads((_home(home) / TESTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "never"
    if not key or not isinstance(marker, dict) or marker.get("key_sha256") != hashlib.sha256(key.encode()).hexdigest():
        return "never"
    return "passed" if marker.get("passed", True) else "failed"


def tested(home=None):
    return test_state(home) == "passed"


# --- the one test call ---------------------------------------------------------------------------------

def models_used(config):
    """Every OpenAI model a Codex call may name: `model` and `browser_read_model`."""
    return sorted({value for value in ((config or {}).get("model"), (config or {}).get("browser_read_model"))
                   if isinstance(value, str) and value})


def _request(method, url, data, headers, timeout):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1 << 16)


def _error_code(body):
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError, TypeError):
        return None
    return error.get("code") or error.get("type") if isinstance(error, dict) else None


def test_key(key, config, transport=None, timeout=30):
    """(ok, kind, words). One models read (are the models Sidedoor names available to this key?)
    and one minimal Responses call (16 output tokens) on `model`, which also shows a key out of
    credit. `transport(method, url, data, headers, timeout) -> (status, body)` replaces the network."""
    send = transport or _request
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    needed = models_used(config)
    if not needed:
        return False, "config", "Sidedoor has no OpenAI model configured (config.json model)."
    try:
        status, body = send("GET", API_URL + "/models", None, headers, timeout)
        if status == 401:
            return False, KEY_REFUSED, "OpenAI did not accept this key."
        if status != 200:
            return False, "http", "OpenAI refused the test call (HTTP %s). Try again in a few minutes." % status
        try:
            listed = {item.get("id") for item in json.loads(body).get("data", []) if isinstance(item, dict)}
        except (ValueError, AttributeError, TypeError):
            return False, "http", "OpenAI gave an answer Sidedoor could not read. Try again in a few minutes."
        missing = [name for name in needed if name not in listed]
        if missing:
            return False, "models", ("This key can't use %s, which Sidedoor needs. Use a key from an OpenAI project "
                                     "with access to %s, or Sign in with ChatGPT." % (", ".join(missing), ", ".join(missing)))
        chosen = config["model"] if config.get("model") in needed else needed[0]
        payload = json.dumps({"model": chosen, "input": "Reply with the single word OK.", "max_output_tokens": 16}).encode()
        status, body = send("POST", API_URL + "/responses", payload, headers, timeout)
    except (OSError, TimeoutError) as error:
        import tls_setup
        if tls_setup.is_certificate_error(error):   # T7: not the internet
            return False, "network", ("Sidedoor couldn't verify OpenAI's secure connection on this computer. Run "
                                      "Report a problem so we can see why.")
        return False, "network", "Could not reach OpenAI. Check your internet and try again."
    if status == 200:
        return True, None, "Key works."
    code = _error_code(body)
    if status == 401:
        return False, KEY_REFUSED, "OpenAI did not accept this key."
    if code == "insufficient_quota":
        return False, OUT_OF_CREDIT, "Your OpenAI API key is out of credit. Add credit, then test again."
    if status == 429:
        return False, RATE_LIMIT, "OpenAI is rate-limiting this key. Try again in a few minutes."
    return False, "http", "OpenAI refused the test call (HTTP %s). Try again in a few minutes." % status


# --- billing waits seen at run time ----------------------------------------------------------------------

_KINDS = ((OUT_OF_CREDIT, re.compile(r"insufficient_quota|quota exceeded|exceeded your current quota|"
                                     r"check your plan and billing", re.IGNORECASE)),
          (KEY_REFUSED, re.compile(r"(?:status:?\s*|http\s*|\"status\"\s*:\s*|\"code\"\s*:\s*)401\b|\b401 unauthorized\b|"
                                   r"invalid_api_key|incorrect api key provided", re.IGNORECASE)),
          (RATE_LIMIT, re.compile(r"(?:status:?\s*|http\s*|\"status\"\s*:\s*|\"code\"\s*:\s*)429\b|\b429 too many requests\b|"
                                  r"rate_limit_exceeded", re.IGNORECASE)))


def billing_kind(message):
    """The API billing refusal an error names (OUT_OF_CREDIT, KEY_REFUSED, RATE_LIMIT), else None.
    A bare 401/429 counts only beside HTTP-status words ("status 401", "HTTP 429", "401 Unauthorized")."""
    if not isinstance(message, str):
        return None
    for kind, pattern in _KINDS:
        if pattern.search(message):
            return kind
    return None


def redact(text):
    text = re.sub(r"sk-[A-Za-z0-9_*.-]{4,}", "[key]", text)
    return re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)


def _now(now):
    return now or dt.datetime.now(dt.timezone.utc)


def _read_json(name, home):
    try:
        value = json.loads((_home(home) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def note_wait(kind, resume_at, home=None, now=None):
    """Record an API billing wait (kind and times; never the provider's text). A refused key or one
    out of credit switches to the ChatGPT sign-in when one works (OA2 a), else a refused key holds
    the scheduler at once; other waits extend the streak, which past ESCALATE_HOURS (AK3) does the
    same. Best effort: a failed write never turns a wait into a failure."""
    now = _now(now)
    try:
        if kind in (KEY_REFUSED, OUT_OF_CREDIT) and _fall_back(kind, now, home):
            return
        if kind == KEY_REFUSED:
            _hold(kind, now, home)
            return
        streak = _read_json(WAIT_FILE, home) or {}
        first = streak.get("first_observed_at") or now.isoformat()
        value = {"kind": kind, "first_observed_at": first, "observed_at": now.isoformat(), "resume_at": resume_at,
                 "count": int(streak.get("count") or 0) + 1}
        _write_private(_home(home) / WAIT_FILE, json.dumps(value) + "\n")
        if now - dt.datetime.fromisoformat(first) >= dt.timedelta(hours=ESCALATE_HOURS):
            if not _fall_back(kind, now, home):
                _hold(kind, now, home)
    except Exception:
        pass


def chatgpt_signed_in(home=None, run=None):
    """True when the saved Codex's local `codex login status` reports a ChatGPT login (no network)."""
    import setup_steps
    path = (_read_json(STATE_FILE, home) or {}).get("codex_path")
    if not setup_steps._usable(path):
        return False
    import subprocess
    return setup_steps.codex_login_status(path, run or subprocess.run) == "SIGNED_IN"


def _fall_back(kind, now, home):
    """Switch Codex calls to the ChatGPT sign-in when one works; True when in force."""
    if fallback(home):
        return True
    if not chatgpt_signed_in(home):
        return False
    _write_private(_home(home) / FALLBACK_FILE, json.dumps({"kind": kind, "since": now.isoformat()}) + "\n")
    clear_wait(home)
    return True


def fallback(home=None):
    """The ChatGPT fallback while API-key mode is on: {"kind", "since"}, else None."""
    if not api_key_mode(home):
        return None
    value = _read_json(FALLBACK_FILE, home)
    return value if value and value.get("kind") in WORDS else None


def _hold(kind, now, home):
    if _read_json(HOLD_FILE, home):
        return
    _write_private(_home(home) / HOLD_FILE, json.dumps({"kind": kind, "since": now.isoformat()}) + "\n")


def clear_wait(home=None):
    try:
        (_home(home) / WAIT_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def release(home=None):
    """A key passed its test: the hold, the ChatGPT fallback and the wait streak end (the key is used again)."""
    for name in (HOLD_FILE, FALLBACK_FILE, WAIT_FILE):
        try:
            (_home(home) / name).unlink(missing_ok=True)
        except OSError:
            pass


def hold(home=None):
    """The scheduler hold while API-key mode is on: {"kind", "since"}, else None."""
    if not api_key_mode(home):
        return None
    value = _read_json(HOLD_FILE, home)
    return value if value and value.get("kind") in WORDS else None


def last_wait(home=None, now=None):
    """The last API billing wait while it is recent (BANNER_HOURS) and API-key mode is on, else None."""
    if not api_key_mode(home):
        return None
    value = _read_json(WAIT_FILE, home)
    try:
        observed = dt.datetime.fromisoformat(value["observed_at"])
    except (TypeError, KeyError, ValueError):
        return None
    if value.get("kind") not in WORDS or _now(now) - observed > dt.timedelta(hours=BANNER_HOURS):
        return None
    return value


def local_time(stamp):
    """An ISO instant as the computer's local time ("2026-10-22 14:05"), else the text itself."""
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return stamp


# RATE_LIMIT's entries below serve the key test (`test_key`) and a wait or hold recorded before 2026-10-27;
# a run-time 429 is no longer recorded here (provider_limits.RATE_LIMITED).
HELD_WORDS = {KEY_REFUSED: "OpenAI did not accept your API key and no ChatGPT sign-in works, so Sidedoor is paused "
                           "until you replace the key.",
              OUT_OF_CREDIT: "Your OpenAI API key is out of credit and no ChatGPT sign-in works, so Sidedoor is paused. "
                             "Add credit, then test the key.",
              RATE_LIMIT: "OpenAI has rate-limited your API key for a day and no ChatGPT sign-in works, so Sidedoor "
                          "is paused. Test the key to carry on."}


REASONS = {KEY_REFUSED: "OpenAI refused it", OUT_OF_CREDIT: "it is out of credit", RATE_LIMIT: "it was rate-limited for a day"}


def fallback_words(kind):
    return ("Your OpenAI API key stopped working (%s), so Sidedoor is using your ChatGPT sign-in. Replace the key "
            "in Settings > Connections." % REASONS.get(kind, "unknown"))


def banner(home=None, now=None):
    used = fallback(home)
    if used:
        return {"tone": "warning", "text": fallback_words(used["kind"]), "action_label": "Replace key",
                "action_href": "/settings#connection-codex"}
    held = hold(home)
    if held:
        return {"tone": "error", "text": HELD_WORDS[held["kind"]],
                "action_label": "Replace key" if held["kind"] == KEY_REFUSED else "Test key",
                "action_href": "/settings#connection-codex"}
    value = last_wait(home, now)
    if not value:
        return None
    return {"tone": "error" if value["kind"] != RATE_LIMIT else "warning", "text": WORDS[value["kind"]],
            "action_label": "Open Settings", "action_href": "/settings#connection-codex"}
