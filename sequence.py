"""E3.T3 option A: the per-lead LinkedIn sequence over the LinkedInPort (owner, 2026-10-17: "Lets go
with A").

No new engine and no new table. A connection invitation is one more purpose (INVITE) on the
existing `outreach_actions` rows; the caller takes the claim and runs the existing gates, as for any
send, then hands the committed REQUEST_STARTED row to `send_invite`. Acceptance is read with
`acceptance` (degree 1, or the invitation still pending), and the timing that follows (first
message, follow-ups, stop on reply, 27-day closure) stays in `engagement.next_action`,
`followup_execution` and `inactivity_closure`, fed by the same events as before. `sequence_view`
derives where a lead stands for status and reports.

Wiring (piece 2A): with `config["linkedin_provider"] == "unipile"` the invitation-route candidates
that HeyReach would turn into a prepared campaign become INVITE contexts (`invite_contexts`), are
promoted, claimed and gated by `outreach.cycle` exactly as a new contact, and
`HeyReachActionAdapter.execute_claimed` hands the committed row to `send_invite`;
`reconcile_invites` settles them each cycle (`orchestrator.new_contact_execution_cycle`).
Sender: the claim's `sender_id` (INTEGER NOT NULL) stays `config["sender_id"]`, the one configured
sending identity that the claim, `action_gates` and `execute_claimed` already compare; the Unipile
account is not a second column but the port's own (`unipile_port.for_install`: the license's own
account, which the relay sets; the only LINKEDIN account under the owner's local-dev setting), so
one config value still names the sender.

Rules carried over unchanged:
- An acknowledged write is not delivery: `initial_sent` is recorded only when a read shows the
  invitation (pending in the sent listing, or the person already connected).
- One pending invitation per person: `invite_gates` refuses a person who is connected, already has
  a pending invitation from us, or has invited the owner (a RECEIVED invitation is theirs, not ours).
  On a first send an unclear pending/distance value decides nothing (as live); before a retry it
  still refuses (PROFILE_INVITATION_STATE_UNKNOWN).
- An uncertain invitation is retried at most 3 times in all (`linkedin_port.INVITATION_MAX_ATTEMPTS`),
  each only after `linkedin_port.invitation_absent` shows it absent; a read that cannot decide waits.
"""

import datetime as dt
import json

import engagement
import linkedin_port
from orchestrator import profile_key


INVITE = "INVITE"
SOURCE = "unipile"
# Action states (the existing outreach_actions vocabulary plus one wait state for an uncertain invite).
STARTED, ACKED, CONFIRMED, NOT_SENT, RETRY_WAIT = (
    "REQUEST_STARTED", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "CONFIRMED_SENT", "CONFIRMED_NOT_SENT",
    "INVITE_RETRY_WAIT")
ALREADY_CONTACTED_ELSEWHERE = "ALREADY_CONTACTED_ELSEWHERE"


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _jsonable(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return repr(value)


# --- reads ---------------------------------------------------------------------------------------
def received_invitation(person):
    """True when the person has invited the owner (Unipile's profile `invitation.type` RECEIVED)."""
    raw = person.raw if isinstance(person.raw, dict) else {}
    invitation = raw.get("invitation")
    return isinstance(invitation, dict) and str(invitation.get("type") or "").upper() == "RECEIVED"


def acceptance(port, profile):
    """(state, person) for our invitation to `profile`:
    ACCEPTED     connected (network_distance 1);
    PENDING      our invitation is still pending;
    NOT_PENDING  not connected and no pending invitation (never sent, withdrawn, declined or expired);
    UNKNOWN      the read does not say (never read as absent).
    Port errors (NotFound, SessionInvalid, IncompletePage, timeouts) propagate."""
    person = port.lookup(profile)
    if person.network_distance == 1:
        return "ACCEPTED", person
    if received_invitation(person):
        return "UNKNOWN", person
    if person.pending_invitation is True:
        return "PENDING", person
    if (person.pending_invitation is False and type(person.network_distance) is int
            and person.network_distance >= 2):
        return "NOT_PENDING", person
    return "UNKNOWN", person


def invite_gates(person, retry=False):
    """Why this person must not be invited now (empty: may be invited).

    What the read shows refuses (connected, a pending invitation of ours, one they sent the owner).
    On a first send an unclear value (`pending_invitation` not a bool, `network_distance` not an
    int) decides nothing, as in the live harness (AGENTS "Outreach retries and incomplete
    snapshots", 2026-09-30: an incomplete snapshot no longer holds an invitation; owner,
    2026-10-22: "keep guards consistent with what exists in live harness today"). Before a retry
    (`retry=True`, `reconcile_invite`) it still refuses with PROFILE_INVITATION_STATE_UNKNOWN
    (read-before-retry, E3.T1, owner-approved 2026-10-15)."""
    gates = []
    if person.network_distance == 1:
        gates.append("ALREADY_CONNECTED")
    if received_invitation(person):
        gates.append("INVITATION_RECEIVED_FROM_THEM")
    if person.pending_invitation is True:
        gates.append("INVITATION_ALREADY_PENDING")
    if retry and (type(person.pending_invitation) is not bool or type(person.network_distance) is not int):
        gates.append("PROFILE_INVITATION_STATE_UNKNOWN")
    if not person.provider_id:
        gates.append("PROVIDER_ID_UNKNOWN")
    return gates


# --- the INVITE action ---------------------------------------------------------------------------
def _row(database, action_id):
    row = database.execute("SELECT payload,state,result FROM outreach_actions WHERE action_id=?",
                           (action_id,)).fetchone()
    if not row:
        raise ValueError("INVITE_ACTION_NOT_FOUND")
    action = json.loads(row[0])
    if action.get("purpose") != INVITE or action.get("provider") != SOURCE:
        raise ValueError("NOT_A_UNIPILE_INVITE_ACTION")
    result = json.loads(row[2]) if row[2] else {}
    return action, row[1], result if isinstance(result, dict) else {}


def _save(database, action_id, state, result, expected):
    cursor = database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=? AND state=?",
                              (state, json.dumps(result, sort_keys=True), action_id, expected))
    if cursor.rowcount != 1:
        database.rollback()
        raise ValueError("INVITE_ACTION_STATE_CHANGED")
    database.commit()


def _record_sent(database, action, attempt, person, observed_at, attempts=()):
    """initial_sent, verified by the read that showed the invitation (pending or already accepted)."""
    engagement.record_event(database, {
        "source": SOURCE, "source_id": "invite:" + action["action_id"], "profile": action["profile"],
        "kind": "initial_sent", "purpose": INVITE, "invitation": True, "verified": True,
        "occurred_at": attempt["started_at"], "observed_at": observed_at,
        "evidence": "unipile-read:" + observed_at,
        "raw": {"ack": attempt.get("ack"), "lookup": _jsonable(person.raw), "note": action.get("note"),
                "note_deferred": note_deferred(action, attempts),
                "occurred_at_basis": "REQUEST_STARTED_UPPER_BOUND"}})


def note_deferred(action, attempts):
    """True only when the note certainly did not go with the invitation: the action has a note, and
    every attempt that may have landed carried none. An attempt the provider refused as already
    existing (REFUSED_INVITATION_EXISTS) never landed. Any other attempt that carried the note may
    have landed (an absence read can lag), so the note is then never sent again (no double send
    takes priority over a lost note; owner, 2026-10-27)."""
    landed = [item for item in attempts if item.get("outcome") != "REFUSED_INVITATION_EXISTS"]
    return bool(action.get("note")) and bool(landed) and all(item.get("note") is None for item in landed)


def deferred_invite(event):
    """True for a verified Unipile INVITE initial_sent whose note waits for acceptance."""
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return (event.get("kind") == "initial_sent" and event.get("source") == SOURCE and event.get("purpose") == INVITE
            and raw.get("note_deferred") is True)


def _read_failure(error, **extra):
    """A failed read as recorded: its type, and for a setup hold (RB4, RL1: license, LinkedIn
    connection) the hold code and what the user does about it ("Check Settings > License")."""
    import unipile_port
    entry = dict(extra, error_type=type(error).__name__)
    if isinstance(error, unipile_port.SetupHold):
        entry.update(hold=error.code, next_action=error.next_action)
    return entry


def _attempt(database, port, action_id, action, result, expected_state):
    """One invitation request: written as started before the transport, then its outcome."""
    # Premium is read before every invitation (owner, 2026-10-27). Not Premium: the invitation goes
    # without its note, and the note is sent as the first message once they accept
    # (`queue_deferred_notes`). This also covers a note queued while Premium that is sent after it ended.
    import setup_steps
    premium = setup_steps.check_premium(port, getattr(getattr(port, "transport", None), "home", None))
    note = action.get("note") if premium else None
    attempt = {"started_at": _now(), "provider_id": action["provider_id"], "note": note, "premium": premium,
               "note_deferred": note is None and bool(action.get("note"))}
    result.setdefault("attempts", []).append(attempt)
    _save(database, action_id, STARTED, result, expected_state)
    try:
        ack = port.send_invitation(action["provider_id"], note)
    except linkedin_port.InvitationExists as error:
        # The provider said explicitly that an invitation exists or the people are connected:
        # nothing was sent. The reads that follow settle it (`_contacted_elsewhere`).
        attempt.update(outcome="REFUSED_INVITATION_EXISTS", reason=error.reason, finished_at=_now())
        _save(database, action_id, RETRY_WAIT, result, STARTED)
        return RETRY_WAIT
    except Exception as error:  # an exception from a write is UNKNOWN (the port contract)
        if getattr(error, "never_forwarded", False) is True:
            # RB6 (RL1): the relay itself refused it (license or LinkedIn connection) before
            # forwarding, so nothing reached Unipile: recorded as not sent, and it is not one of
            # the 3 sends. The row waits; the next pass reads before anything is sent.
            result["attempts"].remove(attempt)
            result.setdefault("relay_refusals", []).append(
                _read_failure(error.hold, at=attempt["started_at"], status=getattr(error, "status", None)))
            _save(database, action_id, RETRY_WAIT, result, STARTED)
            return RETRY_WAIT
        attempt.update(outcome="UNCERTAIN", error_type=type(error).__name__, finished_at=_now())
        _save(database, action_id, RETRY_WAIT, result, STARTED)
        return RETRY_WAIT
    attempt.update(outcome="ACKNOWLEDGED", ack=_jsonable(ack.raw), finished_at=_now())
    _save(database, action_id, ACKED, result, STARTED)
    return ACKED


def send_invite(database, port, action_id):
    """Send the claimed INVITE action. The caller has committed it as REQUEST_STARTED after the
    existing claim and gates; this re-reads the profile, refuses on `invite_gates`, makes the one
    request, and returns the new state (ACKED, RETRY_WAIT or CANCELLED_BEFORE_SEND)."""
    if database.in_transaction:
        raise ValueError("Parent must commit REQUEST_STARTED before transport")
    action, state, result = _row(database, action_id)
    if state != STARTED or result.get("attempts"):
        raise ValueError("INVITE_REQUEST_ALREADY_CONSUMED_OR_NOT_STARTED")
    person = port.lookup(action["profile"])
    gates = invite_gates(person)
    if person.provider_id and person.provider_id != action["provider_id"]:
        gates.append("PROVIDER_ID_CHANGED")
    if gates:
        result["provider_guard"] = {"checked_at": _now(), "gates": sorted(set(gates)),
                                    "lookup": _jsonable(person.raw)}
        _save(database, action_id, "CANCELLED_BEFORE_SEND", result, STARTED)
        return "CANCELLED_BEFORE_SEND"
    if invite_gates(person, retry=True):
        # Sent on an unclear read (G1): recorded for the audit only (GM1: `_contacted_elsewhere`
        # decides from the provider's explicit refusal, whatever this read showed).
        result["presend_read_unclear"] = {"checked_at": _now(), "lookup": _jsonable(person.raw)}
    return _attempt(database, port, action_id, action, result, STARTED)


def _contacted_elsewhere(result):
    """GL1, narrowed by GL-I1 and GM1 (owner, 2026-10-22): our only attempt was refused by the
    provider saying explicitly that an invitation already exists or the people are already
    connected (linkedin_port.InvitationExists), whatever the pre-send read showed. A later read
    showing the invitation pending or the person connected then shows someone else's contact (the
    owner's hand invite, an existing connection), not ours. A timeout, connection error or any other
    error may have reached LinkedIn, so it stays ours, as live: a later PENDING or ACCEPTED is our send."""
    attempts = result.get("attempts") or []
    return len(attempts) == 1 and attempts[0].get("outcome") == "REFUSED_INVITATION_EXISTS"


def reconcile_invite(database, port, action_id, config=None, now=None):
    """Settle an ACKED or RETRY_WAIT invite from reads; never resends what a read shows.

    ACKED: shown pending or connected -> CONFIRMED (initial_sent recorded); not shown -> RETRY_WAIT
    (the acknowledgement did not land). RETRY_WAIT: shown -> CONFIRMED; `invitation_absent` True ->
    another attempt while fewer than 3 were made, else CONFIRMED_NOT_SENT; a read that cannot decide
    leaves the state as it is. Returns the state after this pass."""
    action, state, result = _row(database, action_id)
    if state not in {ACKED, RETRY_WAIT}:
        return state
    try:
        status, person = acceptance(port, action["profile"])
    except Exception as error:
        result.setdefault("reads", []).append(_read_failure(error, at=_now()))
        _save(database, action_id, state, result, state)
        return state
    observed = _now()
    if status in {"PENDING", "ACCEPTED"} and not result.get("attempts"):
        # A recovered row that never sent: the invitation or connection is not ours to record.
        result["provider_guard"] = {"checked_at": observed, "gates": ["ALREADY_CONNECTED" if status == "ACCEPTED"
                                    else "INVITATION_ALREADY_PENDING"], "lookup": _jsonable(person.raw)}
        _save(database, action_id, "CANCELLED_BEFORE_SEND", result, state)
        return "CANCELLED_BEFORE_SEND"
    if status in {"PENDING", "ACCEPTED"} and _contacted_elsewhere(result):
        # Never recorded as our initial_sent, so it is never followed up.
        result["not_sent"] = {"at": observed, "attempts": len(result["attempts"]),
                              "reason": ALREADY_CONTACTED_ELSEWHERE, "by": status, "lookup": _jsonable(person.raw)}
        _save(database, action_id, NOT_SENT, result, state)
        return NOT_SENT
    if status in {"PENDING", "ACCEPTED"}:
        _record_sent(database, action, result["attempts"][-1], person, observed, result["attempts"])
        result["confirmed"] = {"at": observed, "by": status}
        _save(database, action_id, CONFIRMED, result, state)
        return CONFIRMED
    if state == ACKED and status == "NOT_PENDING":
        result.setdefault("reads", []).append({"at": observed, "status": status})
        _save(database, action_id, RETRY_WAIT, result, ACKED)
        return RETRY_WAIT
    try:
        absent = linkedin_port.invitation_absent(port, action["provider_id"], action["profile"])
    except Exception as error:
        absent = None
        result.setdefault("reads", []).append(_read_failure(error, at=observed))
    if absent is not True:
        _save(database, action_id, state, result, state)
        return state
    made = len(result.get("attempts", []))
    if linkedin_port.may_retry_invitation(made, absent):
        # Every retry re-runs the first send's checks (owner-approved, review round 1): a person who
        # connected, has a pending invitation or invited the owner since is never invited again.
        gates = invite_gates(person, retry=True)
        if person.provider_id and person.provider_id != action["provider_id"]:
            gates.append("PROVIDER_ID_CHANGED")
        if gates:
            result["provider_guard"] = {"checked_at": observed, "gates": sorted(set(gates)),
                                        "lookup": _jsonable(person.raw)}
            _save(database, action_id, "CANCELLED_BEFORE_SEND", result, state)
            return "CANCELLED_BEFORE_SEND"
        # A retry is an invitation request too: the same pacing as a first send (owner-approved,
        # review round 2). A wait keeps the row as it is; the next pass reads it again.
        import pacing
        pace = pacing.check(database, config or {}, now)
        if not pace["go"]:
            result["pacing_wait"] = {"at": observed, "reason": pace["reason"], "until": pace["wait_until"]}
            _save(database, action_id, state, result, state)
            return state
        return _attempt(database, port, action_id, action, result, state)
    result["not_sent"] = {"at": observed, "attempts": made}
    _save(database, action_id, NOT_SENT, result, state)
    return NOT_SENT


# --- wiring into the existing outreach cycle --------------------------------------------------
def invite_contexts(port, candidates):
    """Promotion contexts for `config["linkedin_provider"] == "unipile"`: each invitation-route
    candidate (outreach_agent.prepare_drafts' `campaign_candidates`, which HeyReach turns into a
    prepared campaign) becomes an INVITE context carrying the inspection's proof and the provider id
    the port reports. A failed lookup leaves that person without a context (promotion holds them).
    Returns (contexts, lookup failures)."""
    contexts, failures = {}, []
    for candidate in candidates:
        try:
            person = port.lookup(candidate["profile"])
        except Exception as error:
            failures.append(_read_failure(error, profile=candidate["profile"]))
            continue
        contexts[candidate["profile"]] = {"purpose": INVITE, "proof": candidate["proof"],
                                          "provider_id": person.provider_id}
    return contexts, failures


STUCK_AFTER = dt.timedelta(minutes=15)


def recover_stuck(database, now=None):
    """Treat an INVITE stranded by a crash or an exception as uncertain (owner-approved): a
    REQUEST_STARTED row older than STUCK_AFTER (its last attempt has no outcome, or it has no
    attempt at all), a DISPATCHING row whose claim (`claimed_at`) is older than STUCK_AFTER (the
    process died between the claim and `execute_claimed`; owner, 2026-10-21: "recover it"; a live
    cycle moves its fresh claim to REQUEST_STARTED in the same pass), or an UNCERTAIN_NO_RETRY row with no attempt or whose last attempt has no
    outcome (the send may have gone out before its state could be saved), moves to RETRY_WAIT, so it is
    read before anything is sent again and the 3-send rule applies. Returns the action ids moved."""
    now = now or dt.datetime.now(dt.timezone.utc)
    moved = []
    for action_id, state, result, claimed_at in database.execute("""SELECT action_id,state,result,claimed_at
            FROM outreach_actions WHERE state IN ('DISPATCHING','REQUEST_STARTED','UNCERTAIN_NO_RETRY')
            AND json_extract(payload,'$.purpose')=?""", (INVITE,)).fetchall():
        result = json.loads(result) if result else {}
        result = result if isinstance(result, dict) else {"previous_result": result}
        attempts = result.get("attempts") or []
        if state == "UNCERTAIN_NO_RETRY" and attempts and attempts[-1].get("outcome"):
            continue
        started = attempts[-1].get("started_at") if attempts else claimed_at
        try:
            started = dt.datetime.fromisoformat(started)
        except (TypeError, ValueError):
            started = None
        if state in {"DISPATCHING", "REQUEST_STARTED"} and (started is None or now - started < STUCK_AFTER
                                           or (attempts and attempts[-1].get("outcome"))):
            continue
        if attempts:
            attempts[-1].update(outcome="INTERRUPTED", finished_at=_now())
        result["recovered"] = {"at": _now(), "from": state}
        _save(database, action_id, RETRY_WAIT, result, state)
        moved.append(action_id)
    return moved


def reconcile_invites(database, port, now=None, config=None):
    """Run `reconcile_invite` on every INVITE action awaiting a read (ACKED or RETRY_WAIT), after
    `recover_stuck`."""
    recovered = recover_stuck(database, now)
    rows = database.execute("""SELECT action_id FROM outreach_actions WHERE state IN (?,?)
        AND json_extract(payload,'$.purpose')=? ORDER BY action_id""", (ACKED, RETRY_WAIT, INVITE)).fetchall()
    items = []
    for (action_id,) in rows:
        try:
            items.append({"action_id": action_id, "state": reconcile_invite(database, port, action_id, config, now)})
        except Exception as error:
            if database.in_transaction:
                database.rollback()
            items.append(_read_failure(error, action_id=action_id))
    return {"state": "PROCESSED", "items": items, "recovered": recovered}


# --- acceptance as an event ----------------------------------------------------------------------
def record_acceptance(database, port, profile):
    """Read acceptance once; when ACCEPTED, record a verified `accepted` event (observation time, not
    LinkedIn's acceptance time). Returns the state."""
    status, person = acceptance(port, profile)
    if status == "ACCEPTED":
        observed = _now()
        engagement.record_event(database, {
            "source": SOURCE, "source_id": "acceptance:" + profile_key(profile), "profile": profile,
            "kind": "accepted", "verified": True, "occurred_at": observed, "observed_at": observed,
            "evidence": "unipile-lookup:" + observed,
            "raw": {"lookup": _jsonable(person.raw),
                    "occurred_at_basis": "FIRST_INDEPENDENT_ACCEPTANCE_OBSERVATION_NOT_ACTUAL_ACCEPTANCE_TIME"}})
    return status


# --- read-only view ------------------------------------------------------------------------------
def sequence_view(database, profile, now=None, recipient=""):
    """Where this lead stands, derived from the records the sequence already writes (no new state):
    the latest INVITE action, the invite/acceptance/follow-up events and `engagement.next_action`."""
    if isinstance(profile, str) and "://" not in profile:
        profile = "https://" + profile   # a stored identity ("linkedin.com/in/x"), as engagement.identity_keys reads it
    identity = profile_key(profile)
    invite = database.execute("""SELECT action_id,state FROM outreach_actions WHERE identity=?
        AND json_extract(payload,'$.purpose')=? ORDER BY rowid DESC LIMIT 1""", (identity, INVITE)).fetchone()
    events = engagement.events_for(database, identity)
    kinds = [event["kind"] for event in events]
    if "inbound" in kinds or "opt_out" in kinds:
        stage = "REPLIED"
    elif kinds.count("followup_sent"):
        stage = "FOLLOWUP_" + str(kinds.count("followup_sent"))
    elif "accepted" in kinds:
        stage = "ACCEPTED"
    elif "initial_sent" in kinds:
        stage = "INVITED"
    elif invite:
        stage = "INVITE_" + invite[1]
    else:
        stage = "NOT_STARTED"
    following = engagement.next_action(database, profile, now, recipient)
    return {"profile": profile, "stage": stage, "invite_action": invite[0] if invite else None,
            "next": following, "steps": _steps(events), "due": _due(following)}


def _steps(events):
    """[{"label", "at"}] in order: Invited, Accepted, First message (or Message), each Follow-up, Replied,
    Opted out. An invitation versus a message is Overview's rule (`overview_page._invitation`), so a
    HeyReach-era INITIAL with a campaign is an invitation and a Unipile INVITE is one too. Each kind
    once (its first time), follow-ups each."""
    import overview_page
    steps, seen = [], set()
    for event in events:
        kind, purpose = event.get("kind"), event.get("purpose")
        if kind == "initial_sent":
            label = "Invited" if overview_page._invitation(kind, event) else \
                "First message" if purpose == "FIRST_MESSAGE" else "Message"
        else:
            label = {"accepted": "Accepted", "followup_sent": "Follow-up", "inbound": "Replied",
                     "opt_out": "Opted out"}.get(kind)
        if label is None or (label != "Follow-up" and label in seen):
            continue
        seen.add(label)
        steps.append({"label": label, "at": event.get("occurred_at")})
    return steps


def _due(following):
    """{"label", "at"} for the next planned message while it waits or is due, else None."""
    if not isinstance(following, dict) or following.get("state") not in ("WAITING", "DUE") or not following.get("due_at"):
        return None
    label = "First message due" if following.get("purpose") == "FIRST_MESSAGE" else "Follow-up due"
    return {"label": label, "at": following["due_at"]}


# A person invited by an action whose invitation is not on record as sent yet (no step), by its state:
# every state the product writes to an outreach_actions row (sequence, outreach, approvals, engagement,
# outreach_campaign, the provider adapters), so none falls to the generic words.
INVITE_STAGE_WORDS = {
    "PENDING": "Invitation queued",
    "AWAITING_APPROVAL": "Invitation waiting for your approval",
    "BLOCKED": "Invitation on hold",
    STARTED: "Invitation sending", "DISPATCHING": "Invitation sending",
    ACKED: "Invitation sent, delivery not confirmed", "ACKNOWLEDGED_UNCONFIRMED": "Invitation sent, delivery not confirmed",
    RETRY_WAIT: "Invitation will be tried again", "RETRY_WAIT": "Invitation will be tried again",
    "WRITE_RETRY_WAIT": "Invitation will be tried again",
    CONFIRMED: "Invitation sent", "PREPARED": "Invitation prepared, not sent yet",
    NOT_SENT: "Invitation did not go out", "WRITE_FAILED": "Invitation could not be sent",
    "UNCERTAIN_NO_RETRY": "Couldn't confirm the invitation went out",
    "CANCELLED_BEFORE_SEND": "Invitation cancelled before sending", "CANCELLED": "Invitation cancelled before sending",
    "CLOSED": "Closed, no invitation sent"}


def position(view, zone):
    """One line for a person's place in the sequence: "Invited 10-21 · Accepted 10-22 · Follow-up due
    10-27" (local dates in `zone`); "" when nothing is on record yet."""
    def day(stamp):
        try:
            return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(zone).strftime("%m-%d")
        except (TypeError, ValueError, OverflowError):
            return None
    parts = []
    steps = list(view.get("steps") or ())
    stage = view.get("stage") or ""
    if not steps and stage.startswith("INVITE_"):
        parts.append(INVITE_STAGE_WORDS.get(stage[len("INVITE_"):], "Invitation in progress"))
    for step in steps + ([view["due"]] if view.get("due") else []):
        moment = day(step.get("at"))
        parts.append(step["label"] + (" " + moment if moment else ""))
    if view.get("due") and view["due"]["label"] == "Follow-up due" and \
            not any(step["label"] in ("Accepted", "First message", "Message") for step in steps):
        parts[-1] += " (once they accept)"   # a follow-up waits for acceptance (followup_execution)
    return " · ".join(parts)


def positions(database, zone, now=None, limit=25):
    """`orchestrator.py status`'s view: everyone with an invitation action or a sent/accepted/reply event,
    latest activity first, each with sequence_view's stage, next state and `position` line (read only)."""
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    latest = {}
    if "engagement_events" in tables:
        for identity, moment in database.execute(
                "SELECT identity,MAX(occurred_at) FROM engagement_events WHERE kind IN "
                "('initial_sent','accepted','followup_sent','inbound','opt_out') GROUP BY identity"):
            latest[identity] = max(latest.get(identity) or "", moment or "")
    if "outreach_actions" in tables:
        for identity, moment in database.execute(
                "SELECT identity,MAX(claimed_at) FROM outreach_actions WHERE json_extract(payload,'$.purpose')=? "
                "GROUP BY identity", (INVITE,)):
            latest[identity] = max(latest.get(identity) or "", moment or "")
    people, covered, total = [], set(), 0
    with engagement.aliases_read_once(database):
        for identity in sorted(latest, key=lambda key: (latest[key], key), reverse=True):
            if identity in covered:
                continue   # another spelling of someone already listed (rw_aliases): one line per person
            try:
                covered.update(engagement.identity_keys(database, identity))
            except ValueError:
                continue   # not a profile key
            total += 1
            if len(people) < limit:
                view = sequence_view(database, identity, now)
                people.append({"profile": identity, "stage": view["stage"], "next": (view["next"] or {}).get("state"),
                               "position": position(view, zone)})
    return {"total": total, "people": people}


# --- the note after acceptance (not Premium; owner, 2026-10-27) ------------------------------------
DEFERRED_NOTE_SOURCE = "deferred_note"


def deferred_note_action(invite, invite_action_id, now):
    """The FIRST_MESSAGE that carries an invitation's drafted note, exactly as written, once the
    person accepted. One per person: its slot is the person's INITIAL slot (outreach.enqueue_action)."""
    import hashlib
    verification = dict(invite.get("verification") or {})
    verification.update(verified_at=now.isoformat(), content_verified=True,
                        content_sources=[*(verification.get("content_sources") or []), "outreach_actions:" + invite_action_id])
    return {"action_id": "deferred-note:" + hashlib.sha256(invite_action_id.encode()).hexdigest()[:32],
            "profile": invite["profile"], "recipient": invite["recipient"], "company": invite["company"],
            "purpose": "FIRST_MESSAGE", "message": invite["note"], "channel": "linkedin", "provider": SOURCE,
            "conversation_id": None, "verification": verification,
            "source": {"kind": DEFERRED_NOTE_SOURCE, "invite_action_id": invite_action_id}}


NOTE_PHASE = "DEFERRED_NOTE"
# What counts as a refused note (owner pick P3, allowlist): the owner rejecting it in Review, the send
# judge's BLOCKED (its JUDGE_OBJECTION record), or a gate saying the person is no longer eligible.
# Anything else (a missing or stale proof, a read that could not finish, the judge not deciding, a
# switched-off path) stays retryable and is never marked done.
INELIGIBLE_GATES = frozenset({"INITIAL_ALREADY_SENT_OR_UNKNOWN", "OUTREACH_CLOSED", "OPTED_OUT", "HAS_REPLY_PRESENT",
                              "NEGATIVE_REPLY_PRESENT", "OPT_OUT_PRESENT", "SUPPRESSED_CONTACT", "EXCLUDED_CONTACT",
                              "NO_AUTOMATIC_OUTREACH_CONTACT", "REPLY_STOPS_OUTREACH", "INACTIVITY_CLOSURE_REVIEW_REQUIRED",
                              "REPLY_REQUIRES_CONTEXT", "INBOX_CONVERSATION_UNDECODABLE"})


def _judge_refused(database, note_action_id):
    """The send judge decided BLOCKED on this note (outreach_agent.record_judge_objection)."""
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_agent_audit'").fetchone():
        return False
    return any(json.loads(row[0]).get("agent_decision") == "BLOCKED" for row in database.execute(
        "SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='JUDGE_OBJECTION'", (note_action_id,)))


def _note_refused(database, note_action_id, state, result):
    """True only for a real refusal of the queued note (see INELIGIBLE_GATES)."""
    # Terminal and never dispatched again: CLOSED, CONFIRMED_NOT_SENT (reconciled as not sent) and
    # CANCELLED_BEFORE_SEND (a Review rejection, an ineligible person or a final check) all count as
    # done, so the follow-up clock starts at the acceptance instead of waiting for the closure.
    if state in {"CLOSED", "CONFIRMED_NOT_SENT", "CANCELLED_BEFORE_SEND"}:
        return True
    if state == "BLOCKED":
        gates = result if isinstance(result, list) else []
        return bool(INELIGIBLE_GATES.intersection(gates)) or (
            "AGENT_DECISION_NOT_READY" in gates and _judge_refused(database, note_action_id))
    return False


def _settle_refused(database, note_action_id, state, raw, now):
    """Mark a refused note done (`deferred_note_done`) and take it out of dispatch for good."""
    try:
        result = json.loads(raw) if raw else {}
    except ValueError:
        result = {}
    result = result if isinstance(result, dict) else {"previous_result": result}
    result["deferred_note_done"] = {"at": now.isoformat(), "from": state}
    # A terminal state is kept as recorded; a BLOCKED row is taken out of dispatch.
    final = state if state in {"CLOSED", "CONFIRMED_NOT_SENT", "CANCELLED_BEFORE_SEND"} else "CANCELLED_BEFORE_SEND"
    database.execute("UPDATE outreach_actions SET state=?,result=? WHERE action_id=? AND state=?",
                     (final, json.dumps(result, sort_keys=True), note_action_id, state))
    database.commit()


def _stops_pass(error):
    """A rate limit (429), an invalid LinkedIn session or a setup hold stops this cycle's reads."""
    import unipile_port
    return (isinstance(error, (linkedin_port.SessionInvalid, unipile_port.SetupHold))
            or getattr(error, "status", None) == 429)


def queue_deferred_notes(database, port, now=None):
    """For each confirmed invitation sent without its note: read acceptance (`record_acceptance`) on
    followup_execution's acceptance poll backoff (`poll_wait`: 1h, 2h, 4h ... at most 7 days, counted
    from ACCEPTANCE_POLL audit rows, phase DEFERRED_NOTE) until they accept, then queue the note as a
    FIRST_MESSAGE when `engagement.next_action` says it is due. The existing FIRST_MESSAGE pass of
    `outreach.cycle` then reads the person's chats (a reply or a message of ours already there refuses
    it), runs the send judge, holds it in Review mode, and sends it over the port. A note that was
    rejected, refused or blocked is marked done and never sent (`_settle_refused`). A 429, an invalid
    session or a setup hold stops the reads for this cycle. Returns {"state", "items"}."""
    import followup_execution
    import outreach
    import outreach_agent
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    outreach_agent.initialize(database)
    database.commit()
    items, stopped = [], None
    rows = database.execute("""SELECT action_id,payload FROM outreach_actions WHERE state=?
        AND json_extract(payload,'$.purpose')=? ORDER BY action_id""", (CONFIRMED, INVITE)).fetchall()
    for action_id, payload in rows:
        invite = json.loads(payload)
        events = engagement.events_for(database, invite["profile"])
        if not any(deferred_invite(event) and event.get("source_id") == "invite:" + action_id for event in events):
            continue
        item = {"action_id": action_id}
        items.append(item)
        try:
            note = deferred_note_action(invite, action_id, now)
            queued = database.execute("SELECT state,result FROM outreach_actions WHERE action_id=?",
                                      (note["action_id"],)).fetchone()
            if queued:   # queued once; never again, whatever became of it (sent, held, rejected)
                result = json.loads(queued[1]) if queued[1] else None
                if _note_refused(database, note["action_id"], queued[0], result) and not (isinstance(result, dict) and "deferred_note_done" in result):
                    _settle_refused(database, note["action_id"], queued[0], queued[1], now)
                    item["settled"] = "NOTE_REFUSED_COUNTED_DONE"
                item.update(state="ALREADY_QUEUED", note_action_id=note["action_id"], note_state=queued[0])
                continue
            planned = engagement.next_action(database, invite["profile"], now, invite.get("recipient", ""))
            if planned.get("blocker") == "NOTE_WAITS_FOR_ACCEPTANCE":
                if stopped:
                    item.update(state="WAIT_ACCEPTANCE", blocker=stopped)
                    continue
                polls = [found[0] for found in database.execute(
                    "SELECT observed_at FROM outreach_agent_audit WHERE action_id=? AND phase=? AND kind='ACCEPTANCE_POLL'",
                    (action_id, NOTE_PHASE))]
                latest = max((engagement.timestamp(value) for value in polls), default=None)
                if latest and now < latest + followup_execution.poll_wait(len(polls)):
                    item.update(state="WAIT_ACCEPTANCE",
                                next_at=(latest + followup_execution.poll_wait(len(polls))).isoformat())
                    continue
                reference = outreach_agent.audit(database, action_id, NOTE_PHASE, "ACCEPTANCE_POLL", now.isoformat(),
                                                 {"started_at": now.isoformat(), "profile": invite["profile"]})
                database.commit()
                try:
                    item["acceptance"] = record_acceptance(database, port, invite["profile"])
                except Exception as error:
                    if database.in_transaction:
                        database.rollback()
                    outreach_agent.audit(database, action_id, NOTE_PHASE, "ACCEPTANCE_POLL_RESULT", now.isoformat(),
                                         {"poll_ref": reference, "state": "ERROR", "error_type": type(error).__name__})
                    database.commit()
                    if _stops_pass(error):
                        stopped = "ACCEPTANCE_READS_STOPPED_" + type(error).__name__
                    raise
                outreach_agent.audit(database, action_id, NOTE_PHASE, "ACCEPTANCE_POLL_RESULT", now.isoformat(),
                                     {"poll_ref": reference, "state": item["acceptance"]})
                database.commit()
                planned = engagement.next_action(database, invite["profile"], now, invite.get("recipient", ""))
            if planned.get("state") != "DUE" or planned.get("deferred_note") is not True:
                item.update(state=planned.get("state"), blocker=planned.get("blocker"))
                continue
            item.update(outreach.enqueue_action(database, note))
            database.commit()
        except Exception as error:
            if database.in_transaction:
                database.rollback()
            item.update(_read_failure(error), state="HELD", reason=str(error)[:200] if isinstance(error, ValueError) else None)
    return {"state": "STOPPED" if stopped else "PROCESSED", "items": items, "stopped": stopped}
