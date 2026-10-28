"""UnipilePort: the LinkedInPort adapter over Unipile's REST API (E3.T2).

Built on the E0.T1 facts (BACKLOG E0.T1.S1-S4a): base `https://<dsn>/api/v1`, header `X-API-KEY`;
`GET /accounts` (items, type LINKEDIN); `GET /users/{slug or provider id}?account_id`
(provider_id, network_distance as a string); `POST /users/invite` {provider_id, account_id,
message} -> 201; `GET /users/invite/sent` (id, date, parsed_datetime, invitation_text,
invited_user_id, invited_user_public_id); `GET /chats?account_id&limit&cursor` (newest activity
first); `GET /chats/{id}/messages` (id, is_sender, timestamp, text); `POST /chats/{id}/messages`
{text} -> 201 MessageSent; `POST /chats` multipart {account_id, text, attendees_ids} -> 201
ChatStarted {chat_id, message_id} (an existing 1:1 thread is reused).

Decisions this adapter makes (recorded in BACKLOG E3.T2):

- Transport. `transport(method, path, *, query=None, body=None, multipart=None, timeout=None)`
  returns `(status, parsed)` and never raises on an HTTP status (the spike's `call` shape);
  network errors and timeouts raise. `HttpTransport` is the standard-library implementation
  (`spikes/unipile/unipile.call` delegates to it); tests inject a scripted one. The token and the
  dsn are never printed, logged or put in an exception message.
- Reads. A LinkedIn session the account read reports as not OK makes `account()` return
  `valid=False`, and every other call then raises SessionInvalid until an account read reports
  OK again (`_session_valid`, the only state the port keeps besides its account id). HTTP 401/403
  on any read raises SessionInvalid; 404 on a person (`lookup`, `chats(person=)`) or a chat
  (`messages`) raises NotFound; any other non-2xx status (429, 5xx, ...) raises
  `UnipileHTTPError` (a PortError; no page was returned, so it is not IncompletePage); a network
  error or timeout propagates as raised (like FakePort's TimeoutError on a read). A listing
  answer that is not a dict with a list `items` of dicts and a `cursor` key raises IncompletePage:
  an absent cursor key is never read as "last page", because a silently short listing would
  make `invitation_absent` wrongly True.
- Paging (the E3.T1 note). Unipile pages with an opaque continuation `cursor` (top-level key of
  every listing answer, null on the last page), not an offset, so a listing that shrinks between
  two page reads cannot shift entries past the reader: `all_items` over this port skips nothing
  without a page-total check, and `Page.total` is None (Unipile reports no total).
- Writes. `send_invitation`, `send_message` and `start_chat` each make exactly one request
  (the account id is resolved before it, and a failure there raises as a read: nothing was sent).
  A 2xx dict answer is an Ack (ACKNOWLEDGED_UNCONFIRMED). Anything else, a non-2xx status (401
  and 403 included: the request may still have reached LinkedIn through Unipile's queue, and
  `account()` tells session state separately), an unparsed body or any exception, raises
  UncertainWrite carrying the status (`status`) and a bounded, key-free detail: the write may
  have landed, and the caller reads before any retry. One exception (GL-I1): a 4xx refusal of an
  invitation whose error `type` says it already exists or the people are already connected
  (INVITATION_EXISTS_TYPES) raises linkedin_port.InvitationExists, nothing having been sent.
  These type strings are Unipile's documented invitation errors, not yet seen live.
- `lookup`. The profile answer's `invitation` field (live, 2026-10-15: `{"type": "SENT",
  "status": "PENDING"}` for the S2 invitee, null for a connection; E0.T1.S4a had looked for
  `pending_invitation` / `invitation_sent`, which do not exist) says pending when it is exactly
  SENT + PENDING: `Person.pending_invitation` is then True with no listing read. Otherwise the
  complete sent-invitations listing decides: True when an entry names this person
  (`invited_user_id` equals the provider id, or `invited_user_public_id` is the same /in/ profile
  key as the asked URL, or the id equals a member-id slug exactly), False only when the whole
  listing has no such entry, None when the listing cannot be read completely (any error but
  SessionInvalid, which propagates). The listing is LinkedIn's own pending-invitation list for
  the account (147 entries live on 2026-10-15, every one with an invitee id and public id), not
  only what Unipile sent, so it also covers invitations sent by hand or through another tool.
  `network_distance`: FIRST_DEGREE 1, SECOND_DEGREE 2, THIRD_DEGREE 3; anything else
  (OUT_OF_NETWORK, null, another string) None, so `invitation_absent` raises rather than invites.
  Unipile resolves a member-id slug itself (`GET /users/{provider id}` answered with the person's
  `public_identifier`, live 2026-10-15), so a member-id URL is looked up as the provider resolves
  it, and a listed `invited_user_id` equal to that member-id slug names this person too.
- `Invitation.sent_at` is approximate. Unipile's `parsed_datetime` is its fetch-time rendering of
  LinkedIn's relative `date` text ("Sent today", "Sent yesterday", "Sent 5 months ago"): the time
  of day is the read's own, so it moves between reads and is day-accurate at best (month-accurate
  for old entries). Callers never compare `sent_at` values exactly across reads.
- `chats(person=)` resolves the person (`GET /users/{slug}`; NotFound when unknown or when the
  answer carries no provider id) and keeps only chats whose `attendee_provider_id` is that id;
  the page's cursor is the listing's own, so a page with no match still continues. Order is
  Unipile's (newest activity first, E0.T1.S4a), kept as returned.
- `messages` follows the cursor to the end (at most `limit_pages` pages) and returns the thread
  oldest first, ordered by each message's timestamp (stable, so equal times keep Unipile's
  order); a message without a readable timestamp makes the thread unreadable (IncompletePage
  MESSAGE_TIMESTAMP_UNREADABLE). `sent_at` is the timestamp re-expressed in UTC ISO 8601
  (`+00:00`). `is_sender` 1/0 or a bool maps to a bool, anything else None. `Thread.raw` is the
  list of page answers read.
- `relations` (`GET /users/relations`): shaped from Unipile's documented fields (member_id,
  public_identifier / public_profile_url); not live-observed (E0.T1.S4), marked here.
- Account state. `GET /accounts` is read for the LINKEDIN account (`account_id` from the
  constructor, else the only LINKEDIN account; several without a choice raise PortError
  ACCOUNT_AMBIGUOUS, none NotFound). `valid` is True when every status string on the account
  (top-level `status`, and each `sources[*].status`) is "OK", None when the item carries no
  status, False otherwise (CREDENTIALS, ERROR, STOPPED, CONNECTING, ...). `own_profile` is
  `connection_params.im.publicIdentifier` as a profile URL when present.
- Premium (owner, 2026-10-27). `own_premium()` reads `GET /users/me?account_id` (Unipile's
  account-owner profile) and returns its `premium` boolean, None when absent or not a boolean. The
  relay forwards exactly this route (relay/server.py) and local-dev calls Unipile directly, as for
  every other read. `send_invitation(provider_id, None)` sends no `message` key (an invitation
  without a note).

Credentials: `settings()` reads unipile.json ({"dsn", "token"}) from `user_profile.home()`
(the spike's reader, moved here); `UnipilePort.from_settings()` builds a live port. That file holds
Sidedoor's master Unipile key and is for the owner's own development only.

Which port a job uses (RL1, owner 2026-10-22, option a): `for_install(config, home)`, the one
factory every product caller uses. It returns `from_relay(setup_steps.RELAY_URL, <the license key
in the home, as setup_steps.license_key reads it>)` with no account id (the relay resolves the
license's own account and overwrites any account id sent). Only `config["linkedin_transport"] ==
"local-dev"` returns `from_settings()` (unipile.json); a file that merely exists is never picked.
No license key (or an unusable one) raises LicenseRequired, a LicenseHold the hourly step records
as a hold (`orchestrator.worker_step`), not a crash. A relay-made 401 LICENSE_INVALID (the license
lapsed) on a read raises LicenseRefused, never SessionInvalid (it is not a LinkedIn session
failure); on a write it raises RelayRefusedWrite, an UncertainWrite whose `never_forwarded` says
the relay refused it before forwarding (RB6: sequence records such an invitation as not sent and
spends no try; every other caller still treats it as uncertain). Either way RelayTransport has already
run "Refused now" (`refused_now(home)`: `license_clock.request_check`, which from a job child
leaves the request file the dashboard's heartbeat takes). A relay-made 403 LICENSE_NOT_LINKED
(license fine, LinkedIn not connected yet) on a read raises LinkedInNotConnected, a LicenseHold,
never SessionInvalid.

Relay (E3.T5, option B): `RelayTransport(base_url, license_key)` sends the same requests to the
outreach relay (`relay/server.py`) instead of Unipile, with the license key in `X-License-Key`
and no Unipile key; the relay sets the account id itself and returns Unipile's status and body
unchanged, so every rule above holds as written. `UnipilePort.from_relay()` builds that port.
It does not follow redirects (a 30x is returned as its status, so a write answered 30x is
UncertainWrite), and the license key is never put in a message or repr.
"""

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import linkedin_port
from linkedin_port import (Ack, AccountState, Chat, IncompletePage, Invitation, Message, NotFound, Page, Person,
                           InvitationExists, PortError, SessionInvalid, Thread, UncertainWrite, _text)
import user_profile


PROFILE_PREFIX = "https://www.linkedin.com/in/"
DISTANCES = {"FIRST_DEGREE": 1, "SECOND_DEGREE": 2, "THIRD_DEGREE": 3}
VALID_STATUSES = {"OK"}
DETAIL_LIMIT = 300
DEFAULT_PAGE_SIZE = 50
DEFAULT_TIMEOUT = 60
PREMIUM_TIMEOUT = 10   # the Premium read never holds an invitation long (owner, 2026-10-27)
# Unipile's explicit refusals of POST /users/invite (GL-I1). Matched exactly; any other answer is
# UncertainWrite. Not yet observed live (no live call was made for this change).
INVITATION_EXISTS_TYPES = {"errors/already_invited_recently": "ALREADY_INVITED",
                           "errors/already_connected": "ALREADY_CONNECTED"}


TRANSPORT_SETTING = "linkedin_transport"
LOCAL_DEV_TRANSPORT = "local-dev"
LICENSE_REFUSED_CODE = "LICENSE_INVALID"


class SetupHold(PortError):
    """LinkedIn work waits on the user's setup (license, LinkedIn connection, sender identity): a
    hold the user clears, never a LinkedIn failure. `orchestrator.worker_step` records it as a
    BLOCKED hold with `code` and `next_action`; an outreach read retries it without spending a try."""
    code = "SETUP_HOLD"
    next_action = "Finish Setup."


class SenderNotConfigured(SetupHold):
    """No saved sending identity (`setup_steps.saved_sender`): LinkedIn has not been confirmed yet."""
    code = "SENDER_NOT_CONFIGURED"
    next_action = "Connect LinkedIn in Setup step 7 (Settings > LinkedIn)."


class LicenseHold(SetupHold):
    """LinkedIn work waits on the user's license: a hold for the step, not a LinkedIn failure."""
    code = "LICENSE_HOLD"
    next_action = "Check Settings > License."


class LicenseRequired(LicenseHold):
    """No usable license key in this install, so the relay cannot be used."""
    code = "LICENSE_REQUIRED"
    next_action = ("Activate your license first (Setup > License): Sidedoor's LinkedIn work goes through its "
                   "relay with your license key.")


class LicenseRefused(LicenseHold):
    """The relay answered 401 LICENSE_INVALID: the license is not (or no longer) active."""
    code = "LICENSE_INVALID"
    next_action = "Your subscription isn't active. Check Settings > License."


class LinkedInNotConnected(LicenseHold):
    """The relay answered 403 LICENSE_NOT_LINKED: the license is fine, no LinkedIn account is linked."""
    code = "LINKEDIN_NOT_CONNECTED"
    next_action = "Connect LinkedIn in Setup step 7 / Settings > LinkedIn."


class RelayRefusedWrite(UncertainWrite):
    """RB6: a write the relay itself refused before forwarding (its own 401 LICENSE_INVALID or 403
    LICENSE_NOT_LINKED body): the request never reached Unipile. Still an UncertainWrite for every
    caller that does not know better; `never_forwarded` is True and `hold` is the SetupHold."""
    code = "RELAY_REFUSED_WRITE"
    never_forwarded = True

    def __init__(self, hold, status):
        super().__init__("RELAY_REFUSED_" + hold.code)
        self.hold = hold
        self.status = status


class UnipileHTTPError(PortError):
    """A read Unipile refused with a status this port does not classify (429, 5xx, ...)."""
    code = "UNIPILE_HTTP_ERROR"

    def __init__(self, status, detail=None):
        super().__init__("HTTP_" + str(status) + ((": " + detail) if detail else ""))
        self.status = status
        self.detail = detail


# -- settings and transport -----------------------------------------------------------------------

def config_path():
    return user_profile.home() / "unipile.json"


def settings(path=None):
    """{"base": "https://<dsn>/api/v1", "token": ...} from unipile.json. Neither value is ever
    printed by this module."""
    with open(Path(path) if path is not None else config_path(), encoding="utf-8") as handle:
        value = json.load(handle)
    dsn = value["dsn"].strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    return {"base": "https://" + dsn + "/api/v1", "token": value["token"].strip()}


def local_dev(config):
    """True only when the config explicitly opts in to the owner's local unipile.json (RL1)."""
    return isinstance(config, dict) and config.get(TRANSPORT_SETTING) == LOCAL_DEV_TRANSPORT


def for_install(config, home=None, relay_url=None, timeout=DEFAULT_TIMEOUT, **named):
    """The LinkedIn port for this install's jobs (RL1): the relay with the home's license key;
    unipile.json only under `"linkedin_transport": "local-dev"`. Raises LicenseRequired without a
    usable license key."""
    if local_dev(config):
        values = settings(Path(home) / "unipile.json" if home is not None else None)
        return UnipilePort(HttpTransport(values["base"], values["token"], timeout), **named)
    import setup_steps
    key = setup_steps.license_key(home)
    if not key:
        raise LicenseRequired("LICENSE_KEY_MISSING")
    try:
        return UnipilePort.from_relay(relay_url or setup_steps.RELAY_URL, key, timeout=timeout, home=home, **named)
    except ValueError:   # RelayTransport's LICENSE_KEY_INVALID: an unusable key file
        raise LicenseRequired("LICENSE_KEY_INVALID") from None


def _relay_code(parsed):
    return parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) else None


def encode_multipart(fields, boundary=None):
    """multipart/form-data body and content type for string fields (what POST /chats takes)."""
    boundary = boundary or uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n"
                      + str(value) + "\r\n").encode("utf-8"))
    parts.append(("--" + boundary + "--\r\n").encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


class HttpTransport:
    """Standard-library transport: `(method, path, *, query, body, multipart, timeout)` ->
    (status, parsed JSON or bounded text). Never raises on an HTTP status; network errors and
    timeouts raise as urllib raises them. Holds the base URL and token privately (no repr)."""

    def __init__(self, base, token, timeout=DEFAULT_TIMEOUT):
        self._base = base
        self._token = token
        self.timeout = timeout

    def __repr__(self):
        return "HttpTransport(<redacted>)"

    def __call__(self, method, path, *, query=None, body=None, multipart=None, timeout=None):
        url = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"X-API-KEY": self._token, "accept": "application/json"}
        data = None
        if multipart is not None:
            data, headers["content-type"] = encode_multipart(multipart)
        elif body is not None:
            data, headers["content-type"] = json.dumps(body).encode("utf-8"), "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode("utf-8", "replace")[:2000]


LICENSE_HEADER = "X-License-Key"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **named):
        return None


class RelayTransport:
    """The `HttpTransport` contract against the outreach relay: `(method, path, *, query, body,
    multipart, timeout)` -> (status, parsed JSON or bounded text); never raises on an HTTP status,
    network errors and timeouts raise. Sends the license key, never a Unipile key; no repr of it."""

    def __init__(self, base_url, license_key, timeout=DEFAULT_TIMEOUT, home=None):
        key = license_key.strip() if isinstance(license_key, str) else ""
        if not key or any(ord(character) < 33 or ord(character) == 127 for character in key):
            raise ValueError("LICENSE_KEY_INVALID")
        self._base = base_url.rstrip("/")
        self._license_key = key
        self.timeout = timeout
        self.home = home   # where "Refused now" leaves its request (None: the profile home)
        self._opener = urllib.request.build_opener(_RefuseRedirects)

    def __repr__(self):
        return "RelayTransport(<redacted>)"

    def __call__(self, method, path, *, query=None, body=None, multipart=None, timeout=None):
        url = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {LICENSE_HEADER: self._license_key, "accept": "application/json"}
        data = None
        if multipart is not None:
            data, headers["content-type"] = encode_multipart(multipart)
        elif body is not None:
            data, headers["content-type"] = json.dumps(body).encode("utf-8"), "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            with error:
                raw, status = error.read(), error.code
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode("utf-8", "replace")[:2000]
        if status == 401 and isinstance(parsed, dict) and parsed.get("error") == "LICENSE_INVALID":
            refused_now(self.home)
        return status, parsed


def refused_now(home=None):
    """E6.T0 section 11 "Refused now" (E8.T3): any relay call that answers 401 LICENSE_INVALID runs
    the license heartbeat at once, in this process or (from a job child) through the dashboard's
    heartbeat (`license_clock.request_check`, with its limits). Never raises."""
    try:
        import license_clock
        license_clock.request_check(home)
    except Exception:
        pass


def hosted_link(base_url, license_key, kind="create", timeout=DEFAULT_TIMEOUT):
    """E3.T6: a Unipile hosted-auth URL for this license, made by the relay (`kind` "create" to link
    a LinkedIn account, "reconnect" when its session needs the user again). The user opens it in a
    browser; Unipile then tells the relay, which links the account to the license. Raises
    UnipileHTTPError with the relay's or Unipile's status otherwise."""
    status, parsed = RelayTransport(base_url, license_key, timeout)("POST", "/link", body={"type": kind})
    if status == 200 and isinstance(parsed, dict) and isinstance(parsed.get("url"), str):
        return parsed["url"]
    relay_code = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) else None
    raise UnipileHTTPError(status, relay_code or _detail(parsed))


# -- shaping helpers --------------------------------------------------------------------------------

def _detail(parsed):
    """A bounded, key-free description of an error body: Unipile's type/title/detail strings
    only, never the whole body."""
    if isinstance(parsed, dict):
        pieces = [parsed[key] for key in ("type", "title", "detail") if isinstance(parsed.get(key), str)]
        text = " | ".join(pieces)
    elif isinstance(parsed, str):
        text = parsed
    else:
        text = ""
    return text[:DETAIL_LIMIT] or None


def _iso_utc(value):
    """An ISO 8601 UTC string ("+00:00") from Unipile's timestamp string, None when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat()


def _distance(value):
    return DISTANCES.get(value) if isinstance(value, str) else None


def _is_sender(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _profile_url(public_id):
    public_id = _text(public_id)
    return PROFILE_PREFIX + public_id if public_id and public_id.strip() else None


def _account_statuses(item):
    statuses = []
    if isinstance(item.get("status"), str):
        statuses.append(item["status"])
    sources = item.get("sources")
    if isinstance(sources, list):
        statuses.extend(source["status"] for source in sources
                        if isinstance(source, dict) and isinstance(source.get("status"), str))
    return statuses


def _own_profile(item):
    params = item.get("connection_params")
    im = params.get("im") if isinstance(params, dict) else None
    return _profile_url(im.get("publicIdentifier")) if isinstance(im, dict) else None


def _invitation(item):
    return Invitation(invitation_id=_text(item.get("id")), invitee_provider_id=_text(item.get("invited_user_id")),
                      invitee_profile=_profile_url(item.get("invited_user_public_id")),
                      note=_text(item.get("invitation_text")), sent_at=_iso_utc(item.get("parsed_datetime")),
                      raw=item)


def _profile_pending(parsed):
    """True when the profile answer's `invitation` says an invitation we sent is pending."""
    invitation = parsed.get("invitation") if isinstance(parsed, dict) else None
    return isinstance(invitation, dict) and invitation.get("type") == "SENT" and invitation.get("status") == "PENDING"


def _relation(item):
    provider_id = _text(item.get("member_id")) or _text(item.get("provider_id"))
    profile = _text(item.get("public_profile_url")) or _profile_url(item.get("public_identifier"))
    return Person(profile=profile, provider_id=provider_id, network_distance=1, pending_invitation=False, raw=item)


def _chat(item):
    chat_id = _text(item.get("id"))
    if chat_id is None:
        raise IncompletePage("CHAT_ID_MISSING")
    return Chat(chat_id=chat_id, attendee_provider_id=_text(item.get("attendee_provider_id")),
                attendee_profile=None, raw=item)


def _uncertain(message, status):
    error = UncertainWrite(message)
    error.status = status
    return error


# -- the port ---------------------------------------------------------------------------------------

class UnipilePort:
    """LinkedInPort over Unipile. See the module docstring for every rule."""

    provider = "unipile"

    def __init__(self, transport, account_id=None, page_size=DEFAULT_PAGE_SIZE, limit_pages=50):
        self.transport = transport
        self.account_id = account_id
        self.page_size = page_size
        self.limit_pages = limit_pages
        self._session_valid = None

    @classmethod
    def from_settings(cls, account_id=None, timeout=DEFAULT_TIMEOUT, **named):
        values = settings()
        return cls(HttpTransport(values["base"], values["token"], timeout), account_id=account_id, **named)

    @classmethod
    def from_relay(cls, base_url, license_key, account_id=None, timeout=DEFAULT_TIMEOUT, home=None, **named):
        """A port whose requests go through the outreach relay (E3.T5) with this license key."""
        return cls(RelayTransport(base_url, license_key, timeout, home=home), account_id=account_id, **named)

    # -- requests -------------------------------------------------------------------------------
    def _read(self, path, query=None, *, not_found=None, check_session=True):
        """One GET. Raises SessionInvalid (cached invalid session unless `check_session` is False,
        as for the account read itself; 401/403), NotFound (404 when `not_found` names what was
        asked for), UnipileHTTPError (other non-2xx); network errors propagate. Returns the parsed
        body."""
        if check_session and self._session_valid is False:
            raise SessionInvalid("ACCOUNT_SESSION_INVALID")
        status, parsed = self.transport("GET", path, query=query)
        return self._classify(status, parsed, not_found)

    @staticmethod
    def _relay_hold(status, parsed):
        """The relay's own license refusals (not Unipile's): 401 LICENSE_INVALID, 403 LICENSE_NOT_LINKED."""
        code = _relay_code(parsed)
        if status == 401 and code == LICENSE_REFUSED_CODE:
            return LicenseRefused("HTTP_401")
        if status == 403 and code == "LICENSE_NOT_LINKED":
            return LinkedInNotConnected("HTTP_403")
        return None

    def _classify(self, status, parsed, not_found):
        hold = self._relay_hold(status, parsed)
        if hold is not None:
            raise hold
        if status in (401, 403):
            raise SessionInvalid("HTTP_" + str(status))
        if status == 404 and not_found is not None:
            raise NotFound(not_found)
        if not 200 <= status < 300:
            raise UnipileHTTPError(status, _detail(parsed))
        return parsed

    def _write(self, operation, request, path, *, body=None, multipart=None):
        """Exactly one request; any outcome but a 2xx dict answer is UncertainWrite."""
        if self._session_valid is False:
            raise SessionInvalid("ACCOUNT_SESSION_INVALID")
        try:
            status, parsed = self.transport("POST", path, body=body, multipart=multipart)
        except Exception as error:   # the request may have reached the provider
            raise _uncertain(type(error).__name__, None) from error
        if not (200 <= status < 300 and isinstance(parsed, dict)):
            detail = _detail(parsed)
            refused = (INVITATION_EXISTS_TYPES.get(parsed.get("type")) if operation == "send_invitation"
                       and type(status) is int and 400 <= status < 500 and isinstance(parsed, dict) else None)
            if refused:
                error = InvitationExists(refused, "HTTP_" + str(status) + ((": " + detail) if detail else ""))
                error.status = status
                raise error
            hold = self._relay_hold(status, parsed)
            if hold is not None:
                raise RelayRefusedWrite(hold, status)
            raise _uncertain("HTTP_" + str(status) + ((": " + detail) if detail else ""), status)
        return Ack(operation=operation, request=request, raw=parsed)

    def _listing(self, path, query, shape):
        """One page of a cursor listing: a dict with a list `items` of dicts and a `cursor` key,
        else IncompletePage. `shape(item)` builds the neutral item."""
        parsed = self._read(path, query)
        if not (isinstance(parsed, dict) and isinstance(parsed.get("items"), list) and "cursor" in parsed):
            raise IncompletePage("LISTING_SHAPE_INVALID")
        if not all(isinstance(item, dict) for item in parsed["items"]):
            raise IncompletePage("LISTING_ITEM_INVALID")
        cursor = parsed["cursor"]
        if cursor is not None and not (isinstance(cursor, str) and cursor):
            raise IncompletePage("LISTING_CURSOR_INVALID")
        return Page(items=tuple(shape(item) for item in parsed["items"]), next_cursor=cursor, total=None,
                    complete=True, raw=parsed)

    def _query(self, cursor=None, with_account=True):
        query = {"account_id": self._account_id()} if with_account else {}
        query["limit"] = self.page_size
        if cursor is not None:
            query["cursor"] = cursor
        return query

    def _account_id(self):
        if self.account_id is None:
            self.account()
        return self.account_id

    # -- port -----------------------------------------------------------------------------------
    def account(self):
        parsed = self._read("/accounts", check_session=False)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        linked = [item for item in items or () if isinstance(item, dict)
                  and isinstance(item.get("type"), str) and item["type"].upper() == "LINKEDIN"]
        if self.account_id is not None:
            linked = [item for item in linked if item.get("id") == self.account_id]
        if not linked:
            raise NotFound("LINKEDIN_ACCOUNT_NOT_FOUND")
        if len(linked) > 1:
            raise PortError("ACCOUNT_AMBIGUOUS")
        item = linked[0]
        statuses = _account_statuses(item)
        valid = None if not statuses else all(status in VALID_STATUSES for status in statuses)
        account_id = item.get("id") if type(item.get("id")) in (int, str) else None
        if account_id is not None:
            self.account_id = account_id
        self._session_valid = valid
        return AccountState(valid=valid, account_id=account_id, own_profile=_own_profile(item), raw=parsed)

    def _profile_read(self, profile_url):
        """(parsed profile answer, provider id, profile key, slug) for a profile URL in any
        spelling; an unreadable URL or an unknown profile raises NotFound."""
        key, slug = linkedin_port._profile(profile_url)
        if key is None:
            raise NotFound("PROFILE_NOT_FOUND")
        parsed = self._read("/users/" + urllib.parse.quote(slug, safe=""), {"account_id": self._account_id()},
                            not_found="PROFILE_NOT_FOUND")
        provider_id = _text(parsed.get("provider_id")) if isinstance(parsed, dict) else None
        return parsed, provider_id, key, slug

    def _pending(self, provider_id, key, slug):
        """True / False from the complete sent-invitations listing; None when it cannot be read
        completely (SessionInvalid propagates)."""
        try:
            listed = linkedin_port.all_items(self.sent_invitations, limit_pages=self.limit_pages)
        except (SessionInvalid, SetupHold):
            raise
        except Exception:
            return None
        for item in listed:
            invitee = linkedin_port._provider_id(item.invitee_provider_id)
            if invitee is not None and (invitee == provider_id or (linkedin_port._member_id(slug) and invitee == slug)):
                return True
            public_key = linkedin_port._profile(item.invitee_profile)[0]
            if public_key is not None and public_key == key:
                return True
        return False

    def lookup(self, profile_url):
        parsed, provider_id, key, slug = self._profile_read(profile_url)
        distance = _distance(parsed.get("network_distance")) if isinstance(parsed, dict) else None
        pending = True if _profile_pending(parsed) else self._pending(provider_id, key, slug)
        return Person(profile=profile_url, provider_id=provider_id, network_distance=distance,
                      pending_invitation=pending, raw=parsed)

    def own_premium(self):
        """True / False from the signed-in account's own profile (`GET /users/me`, its `premium`
        field; owner, 2026-10-27; at most PREMIUM_TIMEOUT seconds); None when the answer carries no boolean `premium`. Errors raise
        as for any read (the caller, `setup_steps.check_premium`, falls back to the last known value)."""
        if self._session_valid is False:
            raise SessionInvalid("ACCOUNT_SESSION_INVALID")
        status, parsed = self.transport("GET", "/users/me", query={"account_id": self._account_id()},
                                        timeout=PREMIUM_TIMEOUT)
        parsed = self._classify(status, parsed, None)
        value = parsed.get("premium") if isinstance(parsed, dict) else None
        return value if type(value) is bool else None

    def send_invitation(self, provider_id, note):
        # No note (not Premium, owner 2026-10-27): the `message` key is left out, not sent as null.
        body = {"provider_id": provider_id, "account_id": self._account_id()}
        if note is not None:
            body["message"] = note
        return self._write("send_invitation", dict(body), "/users/invite", body=body)

    def sent_invitations(self, cursor=None):
        return self._listing("/users/invite/sent", self._query(cursor), _invitation)

    def relations(self, cursor=None):
        return self._listing("/users/relations", self._query(cursor), _relation)

    def chats(self, cursor=None, person=None):
        wanted = None
        if person is not None:
            wanted = self._profile_read(person)[1]
            if wanted is None:
                raise NotFound("PERSON_PROVIDER_ID_UNKNOWN")
        page = self._listing("/chats", self._query(cursor), _chat)
        if wanted is None:
            return page
        return Page(items=tuple(chat for chat in page.items if chat.attendee_provider_id == wanted),
                    next_cursor=page.next_cursor, total=None, complete=True, raw=page.raw)

    def messages(self, chat_id):
        path = "/chats/" + urllib.parse.quote(str(chat_id), safe="") + "/messages"
        pages, cursor = [], None
        for unused in range(self.limit_pages):
            parsed = self._read(path, self._query(cursor, with_account=False), not_found="CHAT_NOT_FOUND")
            if not (isinstance(parsed, dict) and isinstance(parsed.get("items"), list) and "cursor" in parsed):
                raise IncompletePage("LISTING_SHAPE_INVALID")
            if not all(isinstance(item, dict) for item in parsed["items"]):
                raise IncompletePage("LISTING_ITEM_INVALID")
            pages.append(parsed)
            cursor = parsed["cursor"]
            if cursor is None:
                break
            if not (isinstance(cursor, str) and cursor):
                raise IncompletePage("LISTING_CURSOR_INVALID")
        else:
            raise IncompletePage("PAGE_LIMIT")
        found = []
        for page in pages:
            for item in page["items"]:
                sent_at = _iso_utc(item.get("timestamp"))
                if sent_at is None:
                    raise IncompletePage("MESSAGE_TIMESTAMP_UNREADABLE")
                found.append(Message(_is_sender(item.get("is_sender")), _text(item.get("text")), sent_at, item))
        found.sort(key=lambda message: message.sent_at)
        return Thread(chat_id=str(chat_id), messages=tuple(found), complete=True, raw=pages)

    def send_message(self, chat_id, text):
        path = "/chats/" + urllib.parse.quote(str(chat_id), safe="") + "/messages"
        return self._write("send_message", {"chat_id": chat_id, "text": text}, path, body={"text": text})

    def start_chat(self, provider_id, text):
        fields = {"account_id": self._account_id(), "text": text, "attendees_ids": provider_id}
        return self._write("start_chat", dict(fields), "/chats", multipart=fields)
