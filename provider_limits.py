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

Out of credit at OpenRouter (2026-10-23; on 2026-10-13 OpenRouter's 402s were counted as failed tries
and five jobs were abandoned): a Codex call pointed at OpenRouter (`openai_auth.OPENROUTER_PROVIDER` in
its command) whose TERMINAL error names a 402 with "Payment Required" or credits, and a direct call
(`text_model.complete`) refused with HTTP 402, are the same capacity wait with kind OUT_OF_CREDIT
(resume after BACKOFF_SECONDS). The direct call keeps raising its TextModelHTTPError, chained to the
wait (`raise error from limit`), so `wait_of` finds it through every caller's wrapping.

Too many requests (owner, 2026-10-27; on 2026-10-26 a research discovery session ended "ERROR: exceeded
retry limit, last status: 429 Too Many Requests" and its industry failed for the day): a Codex call signed in
with ChatGPT or pointed at OpenRouter whose TERMINAL error names an HTTP 429 status is the same capacity wait
with kind RATE_LIMITED, but with a short retry: the provider's stated retry time when it gives one, else
30 s, 60 s, 120 s, 240 s, capped at 300 s, by the provider's 429 streak (`provider_pacing.rate_limited`,
which also slows new session starts for that provider). It spends no try and shows no banner: the
provider asked Sidedoor to slow down and the work resumes by itself within minutes. The same holds for a
Codex call to OpenAI with the user's API key whose terminal error is a rate limit (openai_auth.billing_kind
RATE_LIMIT: no 1 h backoff, no API wait streak or day-long hold any more; owner 2026-10-27) and for a
direct OpenRouter call refused with HTTP 429 (`text_model.complete`, key "openrouter").

Banners (dashboard.layout, `banners`): the latest ChatGPT plan usage-limit wait (CHATGPT_WAIT_FILE)
until its reset passes or a later Codex call to OpenAI succeeds, and the latest OpenRouter credit wait
(CREDIT_WAIT_FILE) until a later OpenRouter call (direct or through Codex) succeeds. Both files are
written best effort, in the Sidedoor home, beside `openai_auth.WAIT_FILE`; a failed write never turns a
wait into a failure.
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


OUT_OF_CREDIT = "openrouter_out_of_credit"
RATE_LIMITED = "rate_limited"                # HTTP 429 Too Many Requests: a short wait (2026-10-27)
_TOO_MANY = re.compile(r"(?:\bstatus:?\s*|\bhttp\s*|\"status\"\s*:\s*|\"code\"\s*:\s*)429\b|\b429 too many requests\b",
                       re.IGNORECASE)
_RETRY_IN = re.compile(r"(?:try again|retry) (?:in|after) (?P<amount>\d+(?:\.\d+)?) ?(?P<unit>ms|s|sec|secs|seconds?|m|min|mins|minutes?)\b",
                       re.IGNORECASE)
CHATGPT_WAIT_FILE = "chatgpt-usage-wait.json"
CODEX_NOT_FOUND = "codex_not_found"          # setup_steps.with_codex found no usable Codex (2026-10-23)
CODEX_WAIT_FILE = "codex-not-found.json"
CLAUDE_NOT_FOUND = "claude_not_found"        # setup_steps.with_claude found no usable Claude (U1 3c)
CLAUDE_SIGNED_OUT = "claude_signed_out"      # Claude is not signed in, or refused its sign-in (401) (U1 3c)
CLAUDE_KEY_REFUSED = "claude_key_refused"    # API-key mode: Anthropic refused the saved key (401) (U1 3c)
# U1 3d (review C4): each Claude wait has its own file, written by `noted` and removed once it is fixed
# (Claude found again: setup_steps.with_claude; a sign-in probe that says signed in, or a Claude run that
# worked: `claude_fixed`).
CLAUDE_WAIT_FILES = {CLAUDE_NOT_FOUND: "claude-not-found.json", CLAUDE_SIGNED_OUT: "claude-signed-out.json",
                     CLAUDE_KEY_REFUSED: "claude-key-refused.json"}
# (words in Setup, words once Setup is finished, Overview > Health's words) per Claude wait, most urgent first.
CLAUDE_WORDS = {
    CLAUDE_NOT_FOUND: ("Sidedoor can't find Claude. Open Setup to find it again.",
                       "Sidedoor can't find Claude. Open Settings to find it again.", "Can't find Claude"),
    CLAUDE_KEY_REFUSED: ("Anthropic refused your API key. Sidedoor's AI work is paused until you add a key that works.",
                         "Anthropic refused your API key. Sidedoor's AI work is paused until you add a key that works.",
                         "Anthropic refused your key"),
    CLAUDE_SIGNED_OUT: ("Claude is signed out. Sidedoor's AI work is paused until you sign in to Claude again.",
                        "Claude is signed out. Sidedoor's AI work is paused until you sign in to Claude again.",
                        "Claude is signed out"),
}
ANTIGRAVITY_NOT_FOUND = "antigravity_not_found"    # setup_steps.with_antigravity found no usable agy (U1 4)
ANTIGRAVITY_SIGNED_OUT = "antigravity_signed_out"  # `agy models` or a run says agy is not signed in (U1 4)
ANTIGRAVITY_WAIT_FILES = {ANTIGRAVITY_NOT_FOUND: "antigravity-not-found.json",
                          ANTIGRAVITY_SIGNED_OUT: "antigravity-signed-out.json"}
ANTIGRAVITY_WORDS = {
    ANTIGRAVITY_NOT_FOUND: ("Sidedoor can't find Antigravity. Open Setup to find it again.",
                            "Sidedoor can't find Antigravity. Open Settings to find it again.", "Can't find Antigravity"),
    ANTIGRAVITY_SIGNED_OUT: ("Antigravity is signed out. Sidedoor's AI work is paused until you sign in to Antigravity again.",
                             "Antigravity is signed out. Sidedoor's AI work is paused until you sign in to Antigravity again.",
                             "Antigravity is signed out"),
}
CODEX_WORDS = "Sidedoor can't find Codex. Open Setup to find it again."
CODEX_WORDS_FINISHED = "Sidedoor can't find Codex. Open Settings to find it again."
CREDIT_WAIT_FILE = "openrouter-credit-wait.json"
CREDITS_URL = "https://openrouter.ai/settings/credits"
CHATGPT_USAGE_URL = "https://chatgpt.com/codex/settings/usage"
_CREDIT = re.compile(r"\b402\b(?=.*(?:payment required|credit))|(?:payment required|credit).*\b402\b", re.IGNORECASE)


class ProviderUsageLimit(RuntimeError):
    """The provider refused the turn for usage. `next_at` is an ISO UTC instant."""

    code = CODE
    kind = None   # E0.T8: None for the ChatGPT plan limit; openai_auth.OUT_OF_CREDIT/RATE_LIMIT/KEY_REFUSED

    def __init__(self, message, now=None, kind=None, delay=None, reset_at=None):
        now = _utc(now)
        if kind is not None:
            import openai_auth
            self.kind, message = kind, openai_auth.redact(message)
        message = str(message)
        self.message = message[:500]
        self.observed_at = now.isoformat()
        # The reset the engine reported (engines.Engine.reset_at, an aware datetime), else the one Codex's
        # usage-limit words state.
        stated = _utc(reset_at) if reset_at is not None else reset_time(message)
        self.stated_reset = stated.isoformat() if stated else None
        if stated is not None and now < stated <= now + MAX_WAIT:
            self.resume_at = stated
        elif delay is not None:   # RATE_LIMITED: the short retry (`rate_limit`)
            self.resume_at = now + dt.timedelta(seconds=delay)
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


def _scan(text):
    """(the plan's usage-limit message or None, the call's terminal error or None)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return None, None
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
    return None, terminal


def match_in(text, api=False):
    """(message, kind): the plan's usage-limit line (kind None, anywhere, as before E0.T8) or, with
    `api`, an API billing refusal in the call's terminal error, its last error line or event (kind
    from openai_auth.billing_kind); else None."""
    plan, terminal = _scan(text)
    if plan:
        return plan, None
    if not api or terminal is None:
        return None
    import openai_auth
    kind = openai_auth.billing_kind(terminal)
    return (terminal, kind) if kind else None


def too_many_requests(message):
    """True when an error names an HTTP 429 status ("last status: 429 Too Many Requests", "HTTP 429")."""
    return isinstance(message, str) and bool(_TOO_MANY.search(message))


def stated_retry_seconds(message, now=None):
    """The provider's stated retry time as seconds from `now` ("try again in 20s", "retry after 2 minutes",
    or an absolute "try again at ..."), else None."""
    if not isinstance(message, str):
        return None
    match = _RETRY_IN.search(message)
    if match:
        amount, unit = float(match["amount"]), match["unit"].lower()
        return amount / 1000 if unit == "ms" else amount * 60 if unit.startswith("m") else amount
    stated = reset_time(message)
    if stated is not None:
        seconds = (stated - _utc(now)).total_seconds()
        if 0 < seconds <= MAX_WAIT.total_seconds():
            return seconds
    return None


def rate_limit(message, now=None, command=None, provider=None):
    """The short wait for a 429 from the provider behind `command` (or the pacing key `provider`), recorded
    with the pacer."""
    import provider_pacing
    stated = stated_retry_seconds(message, now)
    delay = provider_pacing.rate_limited(provider or provider_pacing.provider_of(command), stated)
    return ProviderUsageLimit(message, now, kind=RATE_LIMITED, delay=delay)


def slot_wait(busy, now=None):
    """The short rate-limit wait for a session that found no pacing slot within its allowed wait
    (provider_pacing.SlotWaitTooLong): resume when the pacer expects a slot, at least RETRY 30 s and at most
    300 s away. Not a 429, so the provider's streak is not raised."""
    import provider_pacing
    delay = min(max(busy.left, provider_pacing.RETRY_SECONDS), provider_pacing.MAX_RETRY_SECONDS)
    return ProviderUsageLimit("PROVIDER_SLOT_WAIT_TOO_LONG: Sidedoor is pacing its AI sessions", now,
                              kind=RATE_LIMITED, delay=delay)


def credit_refusal(message):
    """True when an error names OpenRouter's out-of-credit refusal: a 402 beside "Payment Required"
    or "credit" (e.g. "402 Payment Required: This request requires more credits ...")."""
    return isinstance(message, str) and bool(_CREDIT.search(message))


def _openrouter(command):
    if command is None:
        return False
    import openai_auth
    return openai_auth.OPENROUTER_PROVIDER in command


def _api(command):
    if command is None:
        return False
    import openai_auth
    return openai_auth.openai_bound(command)


def _limit(found, now):
    limit = ProviderUsageLimit(found[0], now, kind=found[1])
    if limit.kind is None:
        return noted(limit)
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
    return _classify(command, now, (_read(path) for path in paths))


def check_output(returncode, *texts, now=None, command=None):
    """`check` for output held only in memory (never written anywhere)."""
    if not returncode:
        return None
    return _classify(command, now, (text[-MAX_ARTIFACT_BYTES:] if isinstance(text, (str, bytes)) else None
                                     for text in texts))


def _classify(command, now, texts):
    api, routed = _api(command), _openrouter(command)
    for text in texts:
        if not api:  # ChatGPT sign-in: exactly as before E0.T8
            plan, terminal = _scan(text)
            if plan:
                return noted(ProviderUsageLimit(plan, now))
            if routed and credit_refusal(terminal):   # Codex pointed at OpenRouter, out of credit
                return noted(ProviderUsageLimit(terminal, now, kind=OUT_OF_CREDIT))
            if too_many_requests(terminal):   # 429: slow down and retry shortly (2026-10-27)
                return rate_limit(terminal, now, command)
            continue
        found = match_in(text, True)
        if found and found[1] == _api_rate_limit():   # a 429 with the API key: the short wait (2026-10-27)
            return rate_limit(found[0], now, command)
        if found:
            return _limit(found, now)
    return None


def _api_rate_limit():
    import openai_auth
    return openai_auth.RATE_LIMIT


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


# --- the waits the dashboard shows (2026-10-23) ---------------------------------------------------------

def _home(home=None):
    if home is not None:
        return Path(home)
    import user_profile
    return user_profile.home()


def noted(limit, home=None):
    """Record `limit` for the banner when it is the ChatGPT plan limit (kind None) or OpenRouter out of
    credit; returns `limit`. Times only, never the provider's text. Best effort."""
    name = {None: CHATGPT_WAIT_FILE, OUT_OF_CREDIT: CREDIT_WAIT_FILE, CODEX_NOT_FOUND: CODEX_WAIT_FILE,
            **CLAUDE_WAIT_FILES, **ANTIGRAVITY_WAIT_FILES}.get(limit.kind)
    if name is None:
        return limit
    try:
        import openai_auth
        openai_auth._write_private(_home(home) / name, json.dumps(
            {"observed_at": limit.observed_at, "resume_at": limit.next_at, "stated_reset": limit.stated_reset}) + "\n")
    except Exception:
        pass
    return limit


def out_of_credit(message, now=None, home=None):
    """The wait for a direct OpenRouter call refused with HTTP 402 (`text_model.complete`), recorded."""
    return noted(ProviderUsageLimit(message, now, kind=OUT_OF_CREDIT), home)


def claude_fixed(home=None, kinds=(CLAUDE_SIGNED_OUT, CLAUDE_KEY_REFUSED)):
    """Claude's sign-in works again (a probe said signed in, or a run worked): those waits end."""
    for kind in kinds:
        clear(CLAUDE_WAIT_FILES[kind], home)


def clear(name, home=None):
    try:
        (_home(home) / name).unlink(missing_ok=True)
    except Exception:
        pass


def succeeded(command, home=None):
    """A Codex call exited 0: pointed at OpenRouter, the credit wait ends; otherwise the ChatGPT one. The
    provider's pacer counts one more good session (`provider_pacing.succeeded`)."""
    try:
        import provider_pacing
        provider_pacing.succeeded(provider_pacing.provider_of(command))
    except Exception:
        pass
    clear(CREDIT_WAIT_FILE if _openrouter(command) else CHATGPT_WAIT_FILE, home)


def _recorded(name, home):
    try:
        value = json.loads((_home(home) / name).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("observed_at"), str) else None
    except Exception:
        return None


def _local_clock(stamp):
    try:
        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return None
    text = moment.strftime("%I:%M %p").lstrip("0")
    today = dt.datetime.now(moment.tzinfo).date()
    return text if moment.date() == today else "%s on %s %s %d" % (text, moment.strftime("%a"), moment.strftime("%b"),
                                                                    moment.day)


def codex_missing(home=None):
    """The recorded Codex-not-found wait ({"observed_at", ...}) while it lasts, else None."""
    return _recorded(CODEX_WAIT_FILE, home)


def codex_fix(finished):
    """(words, the button's label and link): the Codex step of Setup, or Settings once Setup is finished
    (the Setup tab is hidden then, owner 2026-10-21)."""
    if finished:
        return CODEX_WORDS_FINISHED, "Open Settings", "/settings#connection-codex"
    return CODEX_WORDS, "Open Setup", "/setup#step-6"


def claude_fix(kind, finished):
    """(words, the button's label and link) for a Claude wait: always the Claude section of Settings
    (U1 3g, R4), during Setup too, so the words name Settings."""
    return CLAUDE_WORDS[kind][1], "Open Settings", "/settings#connection-claude"


def antigravity_fix(kind, finished):
    """(words, the button's label and link) for an Antigravity wait: always the Antigravity section of
    Settings, during Setup too, as for Claude (U1 3g R4)."""
    return ANTIGRAVITY_WORDS[kind][1], "Open Settings", "/settings#connection-antigravity"


def out_of_credit_shown(home=None):
    """True while the OpenRouter out-of-credit banner shows."""
    return _recorded(CREDIT_WAIT_FILE, home) is not None


def waits(home=None, now=None, finished=False):
    """The waits in force, from the recorded wait files: {"codex_missing", "claude", "out_of_credit", "usage_limit"},
    each None or {"banner": the dashboard banner, "health": Overview > Health's words for it}. The banners
    and the Health list both come from here, so they never disagree. A ChatGPT wait whose reset passed is
    over (its file is removed)."""
    now = _utc(now)
    found = dict.fromkeys(("codex_missing", "claude", "out_of_credit", "usage_limit"))
    for kind in CLAUDE_WORDS:   # one Claude wait at a time, the most urgent
        if _recorded(CLAUDE_WAIT_FILES[kind], home):
            words, label, href = claude_fix(kind, finished)
            found["claude"] = {"banner": {"tone": "error", "text": words, "action_label": label, "action_href": href},
                               "health": CLAUDE_WORDS[kind][2], "kind": kind}
            break
    for kind in ANTIGRAVITY_WORDS:   # U1 4: one Antigravity wait at a time, the most urgent
        if _recorded(ANTIGRAVITY_WAIT_FILES[kind], home):
            words, label, href = antigravity_fix(kind, finished)
            found["antigravity"] = {"banner": {"tone": "error", "text": words, "action_label": label,
                                               "action_href": href},
                                    "health": ANTIGRAVITY_WORDS[kind][2], "kind": kind}
            break
    if codex_missing(home):
        words, label, href = codex_fix(finished)
        found["codex_missing"] = {"banner": {"tone": "error", "text": words, "action_label": label, "action_href": href},
                                  "health": "Can't find Codex"}
    if _recorded(CREDIT_WAIT_FILE, home):
        found["out_of_credit"] = {"banner": {
            "tone": "error", "text": "Your OpenRouter account is out of credits. Sidedoor's AI work is paused "
            "until you add credits.", "action_label": "Add credits", "action_href": CREDITS_URL, "external": True},
            "health": "Out of credits"}
    usage = _recorded(CHATGPT_WAIT_FILE, home)
    if usage:
        try:
            reset = _utc(usage.get("resume_at"))
        except (AttributeError, TypeError, ValueError):
            reset = None
        if reset is not None and reset > now:
            until = _local_clock(reset.isoformat())
            found["usage_limit"] = {"banner": {
                "tone": "warning", "text": "Your ChatGPT usage limit is reached. Sidedoor pauses its AI work "
                "and picks up again at about %s." % until,
                "action_label": "See your usage", "action_href": CHATGPT_USAGE_URL, "external": True},
                "health": "Usage limit reached until %s" % until}
        elif reset is not None:
            clear(CHATGPT_WAIT_FILE, home)   # the reset passed: the wait is over
    # U1 3g G2: only the chosen engine's waits show; the other engine's files are kept (they show again
    # after a switch back). The OpenRouter credit wait is engine-independent.
    chosen = chosen_engine(home)
    if chosen != "codex":
        found["codex_missing"] = found["usage_limit"] = None
    if chosen != "claude":
        found["claude"] = None
    if chosen != "antigravity":   # U1 4: Antigravity's waits show only while it is chosen
        found.pop("antigravity", None)
    return found


def chosen_engine(home=None):
    """The engine chosen in Setup or Settings (setup_steps.chosen_engine), Codex when it can't be read."""
    try:
        import setup_steps
        return setup_steps.chosen_engine(setup_steps.load_state(home))
    except Exception:
        return "codex"


def banners(home=None, now=None, finished=False, found=None):
    """The dashboard banners for the waits above (`waits`, or `found` when the caller already read
    them), most urgent first. Each clears by itself."""
    found = waits(home, now, finished) if found is None else found
    return [found[name]["banner"] for name in ("codex_missing", "claude", "antigravity", "out_of_credit", "usage_limit")
            if found.get(name)]
