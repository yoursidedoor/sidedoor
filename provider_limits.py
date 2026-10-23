"""The one place that recognises the model provider's usage-limit rejection.

When the ChatGPT subscription behind `codex exec` is out of usage, the CLI refuses the turn
before any model work and exits non-zero, printing exactly one error line: plainly on
stderr ("ERROR: You've hit your usage limit. ... try again at Sep 24th, 2026 3:23 PM.")
or, under --json, as {"type": "error"} / {"type": "turn.failed"} events carrying the same
message. That is a CAPACITY WAIT, never a failed investigation (AGENTS.md: "Compute waits
are not failed investigations"): it is not counted toward attempt, claim, decision or
abandonment budgets, it notifies nobody, and the affected work resumes at the provider's
stated reset (interpreted in America/Los_Angeles, the machine's zone) or, when the time
cannot be read, after a bounded backoff.

Detection is necessarily a match on the provider's error output. It is kept narrow and
structural - the exact error line or error event, together with a non-zero exit - and it
lives only here. Executors keep raising the exception they always raised and chain a
ProviderUsageLimit as its cause (`raise X from check(...)`), so every caller that has not
been taught about waits behaves exactly as before; the callers that must distinguish a
wait use `wait_of(error)`.

E0.T8: for a Codex call that went to OpenAI with the user's API key (`command=` given and
`openai_auth.openai_bound(command)`: never ChatGPT, never a command pointed at OpenRouter), the
call's TERMINAL error (its last error line or error/turn.failed event; retried errors before it do
not count) naming insufficient_quota, a 429 or a 401 is the same capacity wait, with `kind` naming
which (no stated reset: BACKOFF_SECONDS; a refused key KEY_REFUSED_SECONDS, while the scheduler is
held, `openai_auth.note_wait`). The classification is made here, when the failure is recorded,
and stored with it (the CODE on the saved result); saved artifacts are never re-read under it
(`artifact_limited` stays the plan's line only). In ChatGPT mode nothing here changes.
"""

import datetime as dt
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


CODE = "PROVIDER_USAGE_LIMIT_WAIT"
EVENT = CODE
TIMEZONE = "America/Los_Angeles"
BACKOFF_SECONDS = 3600
KEY_REFUSED_SECONDS = 300   # E0.T8: a refused key holds the scheduler; the work is due again once it resumes
MAX_WAIT = dt.timedelta(days=14)
MAX_ARTIFACT_BYTES = 64 * 1024
NEXT_ACTION = ("Provider usage limit reached: wait for the stated reset, then resume the same bounded work; "
               "not a failed attempt, nothing is abandoned and no retry budget is spent")

_PREFIX = "You've hit your usage limit."
_RESET = re.compile(r"try again at (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4}) "
                    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<meridiem>AM|PM)\.?$")


class ProviderUsageLimit(RuntimeError):
    """The provider refused the turn for usage. `next_at` is an ISO UTC instant."""

    code = CODE
    kind = None   # E0.T8: None for the ChatGPT plan limit; openai_auth.OUT_OF_CREDIT/RATE_LIMIT/KEY_REFUSED

    def __init__(self, message, now=None, kind=None):
        now = _utc(now)
        if kind is not None:
            import openai_auth
            self.kind, message = kind, openai_auth.redact(message)
        self.message = message[:500]
        self.observed_at = now.isoformat()
        stated = reset_time(message)
        self.stated_reset = stated.isoformat() if stated else None
        if stated is not None and now < stated <= now + MAX_WAIT:
            self.resume_at = stated
        elif kind == "api_key_refused":
            self.resume_at = now + dt.timedelta(seconds=KEY_REFUSED_SECONDS)
        else:
            self.resume_at = now + dt.timedelta(seconds=BACKOFF_SECONDS)
        self.next_at = self.resume_at.isoformat()
        super().__init__(CODE)

    def detail(self):
        value = {"code": CODE, "resume_at": self.next_at, "stated_reset": self.stated_reset,
                 "observed_at": self.observed_at, "provider_message": self.message,
                 "counts_as_attempt": False, "counts_as_failure": False}
        if self.kind is not None:
            value["kind"] = self.kind
        return value


def _utc(now):
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    return now.astimezone(dt.timezone.utc)


def reset_time(message):
    """The provider's stated reset as an aware UTC datetime, or None when it cannot be read."""
    match = _RESET.search(message.strip()) if isinstance(message, str) else None
    if not match:
        return None
    try:
        local = dt.datetime.strptime("{month} {day} {year} {hour}:{minute} {meridiem}".format(**match.groupdict()),
                                     "%b %d %Y %I:%M %p")
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo(TIMEZONE)).astimezone(dt.timezone.utc)


def message_in(text):
    """The provider's usage-limit message when `text` contains the CLI's error line or event."""
    found = match_in(text)
    return found[0] if found else None


def match_in(text, api=False):
    """(message, kind): the plan's usage-limit line (kind None, anywhere, as before E0.T8) or, with
    `api`, an API billing refusal in the call's terminal error, its last error line or event (kind
    from openai_auth.billing_kind); else None."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return None
    terminal = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ERROR: " + _PREFIX):
            return line[len("ERROR: "):], None
        if line.startswith("ERROR: "):
            terminal = line[len("ERROR: "):]
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = (event.get("message") if event.get("type") == "error"
                   else (event.get("error") or {}).get("message") if event.get("type") == "turn.failed"
                   and isinstance(event.get("error"), dict) else None)
        if isinstance(message, str) and message.startswith(_PREFIX):
            return message, None
        if isinstance(message, str):
            terminal = message
    if not api or terminal is None:
        return None
    import openai_auth
    kind = openai_auth.billing_kind(terminal)
    return (terminal, kind) if kind else None


def _api(command):
    if command is None:
        return False
    import openai_auth
    return openai_auth.openai_bound(command)


def _limit(found, now):
    limit = ProviderUsageLimit(found[0], now, kind=found[1])
    if limit.kind is not None:
        import openai_auth
        openai_auth.note_wait(limit.kind, limit.next_at, now=dt.datetime.fromisoformat(limit.observed_at))
    return limit


def _read(path):
    """The last MAX_ARTIFACT_BYTES of a regular file: the CLI's rejection is its final output."""
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_ARTIFACT_BYTES))
            data = stream.read(MAX_ARTIFACT_BYTES)
        if size > MAX_ARTIFACT_BYTES:
            data = data.split(b"\n", 1)[-1]
        return data.decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return None


def check(returncode, *paths, now=None, command=None):
    """A ProviderUsageLimit when a non-zero exit left the usage-limit line in one of `paths`.
    `command` (E0.T8): the Codex command that ran, so an OpenAI API billing refusal is told apart."""
    if not returncode:
        return None
    api = _api(command)
    for path in paths:
        if not api:  # ChatGPT sign-in: exactly as before E0.T8
            message = message_in(_read(path))
            if message:
                return ProviderUsageLimit(message, now)
            continue
        found = match_in(_read(path), True)
        if found:
            return _limit(found, now)
    return None


def check_output(returncode, *texts, now=None, command=None):
    """`check` for output held only in memory (never written anywhere)."""
    if not returncode:
        return None
    api = _api(command)
    for text in texts:
        tail = text[-MAX_ARTIFACT_BYTES:] if isinstance(text, (str, bytes)) else None
        if not api:  # ChatGPT sign-in: exactly as before E0.T8
            message = message_in(tail)
            if message:
                return ProviderUsageLimit(message, now)
            continue
        found = match_in(tail, True)
        if found:
            return _limit(found, now)
    return None


def wait_of(error, depth=8):
    """The ProviderUsageLimit behind `error` (itself, or its cause/context chain), else None."""
    seen = 0
    while error is not None and seen < depth:
        if isinstance(error, ProviderUsageLimit):
            return error
        error = error.__cause__ or error.__context__
        seen += 1
    return None


def artifact_limited(*paths):
    """True when a saved executor artifact records the usage-limit rejection.

    Used only for records that were already persisted as failures (so the process exited
    non-zero) before this classification existed; the artifact's tail must carry the exact
    error line or error event.
    """
    import paths as locations  # E9.T1: a path saved before the data moved into the home
    # E0.T8 AK4: the plan's line only, in every mode; an API wait is stored with its record (CODE).
    return any(message_in(_read(locations.relocated(path))) for path in paths if path)


def stored_result_limited(result):
    """True for a saved desktop read result that was a usage-limit wait, not a read."""
    try:
        value = json.loads(result) if isinstance(result, str) else result
        error = value.get("error") if isinstance(value, dict) else None
    except ValueError:
        return False
    if not isinstance(error, dict):
        return False
    if error.get("code") == CODE:
        return True
    trace = error.get("trace_path")
    if error.get("code") in {"APPLICATION_BROWSER_EXECUTOR_FAILED", "APPLICATION_BROWSER_FAILED"} and isinstance(trace, str):
        return artifact_limited(trace, str(Path(trace).with_name("stderr.log")))
    return False


def stored_wait(result):
    """`resume_at` of a saved desktop read result recorded as a usage-limit wait, else None."""
    try:
        value = json.loads(result) if isinstance(result, str) else result
        error = value.get("error") if isinstance(value, dict) else None
    except ValueError:
        return None
    if isinstance(error, dict) and error.get("code") == CODE and isinstance(error.get("resume_at"), str):
        return error["resume_at"]
    return None


def decision_limited(detail):
    """True for a recorded coordinator failure whose saved executor log shows the rejection."""
    artifacts = detail.get("artifacts") if isinstance(detail, dict) else None
    if not isinstance(artifacts, dict):
        return False
    return artifact_limited(*(artifacts.get(key) for key in ("log", "trace") if isinstance(artifacts.get(key), str)))
