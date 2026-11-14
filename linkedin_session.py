"""E18 (one LinkedIn sign-in): the one place the rest of Sidedoor asks for the LinkedIn session of the
browser Sidedoor controls.

`current(home)` answers one of three things:

- a `Session` (the session cookie's value and that browser's user agent): the browser is signed in to
  LinkedIn now;
- None: the browser was read and is not signed in to LinkedIn;
- raises `Busy`: the browser is busy with a task right now and can be read later;
- raises `Unavailable`: the browser cannot be read at all (no reader installed, the reader failed).
  `Busy` is an `Unavailable`, so a caller that only needs "no session now" catches the one class.

The reader itself is supplied by the browser code (`set_reader`): a callable `reader(home)` that returns
`Session(cookie, user_agent)` or None, or raises `Busy` (only when a later read can work) or
`Unavailable`. Until one is installed every read is `Unavailable`. Rules for a reader and for every caller (owner, 2026-11-12, A1): the session is held in
memory only, for as long as the one call needs it. It is never written to a file, a database, a log
line, an audit record, an error message or an exception's text. `Session` has no readable repr and
cannot be pickled or copied into JSON by accident; any exception a reader raises is replaced by a bare
`Unavailable`, so a reader's own error text can never carry it further.
"""


class Unavailable(Exception):
    """The browser's LinkedIn session could not be read now. Carries no detail."""


class Busy(Unavailable):
    """The browser is busy with a task right now; a later read can work. Carries no detail."""


class Session:
    """The LinkedIn session of Sidedoor's browser, in memory only."""

    __slots__ = ("_cookie", "_user_agent")

    def __init__(self, cookie, user_agent):
        if not (isinstance(cookie, str) and cookie.strip() and isinstance(user_agent, str) and user_agent.strip()):
            raise ValueError("SESSION_INCOMPLETE")
        self._cookie, self._user_agent = cookie.strip(), user_agent.strip()

    @property
    def cookie(self):
        return self._cookie

    @property
    def user_agent(self):
        return self._user_agent

    def __repr__(self):
        return "Session(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("SESSION_NOT_SERIALIZABLE")


_reader = None


def set_reader(reader):
    """Install the browser's reader (None removes it). Returns the one it replaced."""
    global _reader
    previous, _reader = _reader, reader
    return previous


def current(home=None):
    """The current `Session`, None when the browser is not signed in to LinkedIn; raises `Unavailable`."""
    reader = _reader
    if reader is None:
        raise Unavailable()
    failed = None
    try:
        value = reader(home)
    except Busy:
        failed = Busy
    except Exception:
        failed = Unavailable
    if failed:   # raised outside the handler: the reader's own error (and its text) is not kept, even as context
        raise failed()
    if value is None:
        return None
    if not isinstance(value, Session):
        raise Unavailable()
    return value
