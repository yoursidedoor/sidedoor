"""Sidedoor's diagnostic log (E2.T6): one small layer on the standard library's `logging`.

Product modules write diagnostics through `log.get(__name__)` instead of a bare `print`. Each line
carries the time (UTC, ISO 8601), the level, the job name and the module, then the message:

    2026-10-22T14:03:11Z WARNING applications login_item: log rotation deferred: OSError

Where it goes: the process's own stderr, read at the moment each line is written (so the per-job
`logs/<job>.stderr.log` the scheduler opens for a child, the login entry's sidedoor.stderr.log, or
a test's redirected stderr). After `configure(job=..., stdout_info=True)` (the orchestrator's job
commands) INFO lines go to stdout instead, where those status lines always went, and WARNING and
above stay on stderr. A process that speaks a protocol on stdout (reply_tools, browser_guard: MCP
JSON-RPC) never calls that, so nothing of this module ever reaches its stdout.

Machine-read stdout (a job's JSON result line, the dashboard URL `serve` announces, `doctor --json`,
the MCP servers) is never written through here: those stay `print`, each marked as a contract.

Redaction: every record passes `RedactFilter`, which runs the Failures page's redaction
(`failures_page.redact_text`, i.e. `reply_worker.redact_line` with the install's known secrets,
read for every record from `failures_page.secrets_of`) over the fully formatted message before any
handler sees it. If redaction cannot run, the secrets cannot be read, or the credential store says
redaction is not complete, the message is replaced (WITHHELD), never written as given.
"""

import logging
import os
import sys
import threading
import time

ROOT = "sidedoor"
FORMAT = "%(asctime)s %(levelname)s %(job)s %(component)s: %(message)s"
WITHHELD = "Not shown: it may contain a secret."

# Before `configure` (a library caller, a test) only the pattern redaction applies (URLs, sk- keys,
# JWTs, cookies, token/key/secret/password fields, Bearer values). After it (the orchestrator's
# commands and `serve`) every record also reads the install's known secrets afresh
# (`failures_page.secrets_of`, which uses the credential store's own per-process cache), so a key
# saved after the first line is still redacted; no snapshot is kept here.
_state = {"job": None, "extra": (), "configured": False, "stdout_info": False, "config": None}
_lock = threading.Lock()
_secrets_lock = threading.Lock()   # one read of the secrets at a time (a first load racing a line)
_busy = threading.local()


class SecretsUnavailable(Exception):
    """The known secrets cannot be read (or the store is waiting): the line is withheld."""


def job_name():
    return _state["job"] or os.environ.get("SIDEDOOR_JOB") or "sidedoor"


def _config():
    if _state["config"] is None:
        import resources
        try:
            _state["config"] = resources.read_json("config.json")
        except Exception:
            return {}
    return _state["config"]


def _combine(found, extra):
    """`found` (a failures_page.Secrets, fragments kept) plus the `add_secrets` values."""
    if not extra:
        return found
    import failures_page
    added = failures_page.Secrets(extra)
    combined = failures_page.Secrets()
    combined.extend(sorted(set(found) | set(added), key=len, reverse=True))
    combined.fragments = set(getattr(found, "fragments", ())) | set(added.fragments)
    return combined


def _secrets():
    """The secrets for this record: read now, under a lock. Raises SecretsUnavailable when they
    cannot be read or the credential store says redaction is not complete; the next record retries."""
    import failures_page
    if not _state["configured"]:
        return _combine(failures_page.Secrets(), _state["extra"])
    with _secrets_lock:
        try:
            import user_profile
            home = user_profile.home()
            found = failures_page.secrets_of(home, _config())
            try:
                import credentials
            except ImportError:   # before the keychain store (E2.T4): files only
                credentials = None
            complete = getattr(credentials, "redaction_complete", None)
            if complete is not None and not complete(home):
                raise SecretsUnavailable("redaction not complete")
        except SecretsUnavailable:
            raise
        except Exception as error:
            raise SecretsUnavailable(type(error).__name__) from None
    return _combine(found, _state["extra"])


def add_secrets(values):
    """Also redact these exact values (e.g. a token the caller holds) from every later line."""
    with _secrets_lock:
        _state["extra"] = tuple(_state["extra"]) + tuple(value for value in values if value)


def redact(text, secrets=None):
    """`text` with secrets removed (the Failures page's rule), or WITHHELD when that cannot run or
    the secrets cannot be read."""
    try:
        import failures_page
        return failures_page.redact_text(text, _secrets() if secrets is None else secrets)
    except Exception:
        return WITHHELD


class RedactFilter(logging.Filter):
    """Adds `job` and `component` and redacts the formatted message (args folded in first, so a
    secret passed as an argument is redacted too). Exception text is reduced to its type."""

    def filter(self, record):
        if getattr(record, "_sidedoor_redacted", False):   # the logger's filter already ran
            return True
        record._sidedoor_redacted = True
        record.job = job_name()
        name = record.name
        record.component = name[len(ROOT) + 1:] if name.startswith(ROOT + ".") else name
        if getattr(_busy, "on", False):  # a log line written while collecting the secrets
            record.msg, record.args = WITHHELD, ()
            return True
        _busy.on = True
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            if record.exc_info and record.exc_info[1] is not None:
                message += " (%s)" % type(record.exc_info[1]).__name__
            record.msg, record.args = redact(message), ()
            record.exc_info = record.exc_text = None
            record.stack_info = None
        finally:
            _busy.on = False
        return True


class _Current(logging.StreamHandler):
    """A stream handler writing to whatever sys.stdout / sys.stderr is now (never a stale copy)."""

    def __init__(self, which, accept):
        self._which, self._accept = which, accept
        super().__init__()

    @property
    def stream(self):
        return getattr(sys, self._which)

    @stream.setter
    def stream(self, value):
        pass

    def filter(self, record):
        return self._accept(record) and super().filter(record)


class _Formatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", self.converter(record.created))


def _logger():
    root = logging.getLogger(ROOT)
    with _lock:
        if not getattr(root, "_sidedoor", False):
            root.setLevel(logging.INFO)
            root.propagate = False
            root.addFilter(RedactFilter())   # records logged on "sidedoor" itself too (LG5)
            formatter = _Formatter(FORMAT)
            redaction = RedactFilter()
            out = _Current("stdout", lambda record: _state["stdout_info"] and record.levelno < logging.WARNING)
            err = _Current("stderr", lambda record: not (_state["stdout_info"] and record.levelno < logging.WARNING))
            for handler in (out, err):
                handler.setFormatter(formatter)
                handler.addFilter(redaction)
                root.addHandler(handler)
            root._sidedoor = True
    return root


def configure(job=None, stdout_info=None):
    """Name this process's job (the scheduler's run/research/applications) and, for the job
    commands, send INFO lines to stdout. Each job child names itself (`sidedoor job <name>`);
    SIDEDOOR_JOB, when set, names a process that never calls this."""
    _state["configured"] = True
    if job:
        _state["job"] = job
    if stdout_info is not None:
        _state["stdout_info"] = bool(stdout_info)
    return _logger()


def get(name):
    """The logger for module `name` (pass `__name__`)."""
    _logger()
    if not name or name == "__main__":
        name = os.path.splitext(os.path.basename(sys.argv[0] or ""))[0] or "main"
    return logging.getLogger(ROOT + "." + name.rsplit(".", 1)[-1])
