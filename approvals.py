"""Review mode: the approvals hold and the owner's decisions (E6.T2, option C; owner, 2026-10-22).

docs/product/design/E6.T2-approvals-transport-options.md, "Decision": an `AWAITING_APPROVAL` state on
`outreach_actions`, and every decision an insert-only row in `outreach_owner_records` (kinds
MESSAGE_APPROVAL, MESSAGE_REVISION, MESSAGE_REJECTION). Controls: Approve, Revise and Approve, Reject.
"The approval should be enough": approved text (edited or not) is not judged again, while every
deterministic guard and LinkedIn's 300-character connection-note limit still apply.

Where things happen:
- The mode is read by the send path inside the transaction that would write the claim (`mode`). It is
  stored twice: the `approval_mode` row in the database (written first by every writer here) and
  `setup-state.json` in the Sidedoor home (`setup_steps.set_mode`). A readable Review in either means
  hold. A store that cannot be read (a corrupt or unknown value, a failed database read) decides
  nothing on its own: the other store decides, and with neither readable the mode is Automatic, the
  fresh-install default (G4, owner 2026-10-22, matching the live harness, which has no such hold). A
  home with no setup file and a database with no row (an install that never ran Setup) is Automatic.
  The dashboard's chip and Settings > Mode show the same reading (`install_mode`).
- Point H of the design (`hold_reason` / `mark_held`, in `outreach.cycle`): after the send judge and
  the second `action_gates`, before the claim. A held row has no claim, so no executor can reach it,
  and `outreach.cycle` never selects AWAITING_APPROVAL, so it is not judged again every cycle.
- A HeyReach campaign launch (`outreach.claim_send`) needs an approval of the PREPARED action's payload
  in Review mode too (review fix P1): its queue row waits as AWAITING_APPROVAL and is listed by
  `waiting` like any other message.
- An unreadable mode no longer holds anything (G4). A row an earlier version held for that reason
  (tagged UNREADABLE_MODE) is released on the next cycle (`release_unreadable_holds`), whatever the
  mode; the cycle then holds it again only for a readable Review. A waiting message to a closed or
  opted-out person is closed (`close_stopped`, P4).
- After a decision the row is PENDING again and goes back through `outreach.cycle`: the fresh
  provider read, every gate, the claim and the send. Only the model call is skipped, by the adapters'
  approved route (`approval_for` / `decision`).
- `approve`, `revise_and_approve`, `reject`, `release_all`, `keep_for_review`: one transaction each,
  a compare-and-swap on the waiting row (its state and the payload hash the owner saw, which is
  required); zero rows changed means the row changed meanwhile and nothing is written.
- Reject of a research note frees its slot (`REJECTED:<action_id>`), so a later research draft of the
  same person can become a new action (`outreach_promotion.action_id_for`; owner, 2026-10-22).

Applications (owner, 2026-10-24: the one Review switch holds everything that goes out, job applications
and email applications included): `applications.process` holds a filled application before its
SUBMITTING claim (AWAITING_APPLICATION_APPROVAL, the card in `application_approvals`).
`approve_application` saves exactly the answers on the card (edited or not) to `application_answers`
under the owner's source and makes the job due; the next applications pass reads the form afresh and
submits them, holding the job again if a question appears that the approval does not cover.
`reject_application` closes the job, never submitted. Same compare-and-swap on the payload hash the
card was shown with.

Never touched here: the CAPTCHA wait, and every executor after the claim.
"""

import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import engagement
import paths


AWAITING = "AWAITING_APPROVAL"
APPROVAL, REVISION, REJECTION = "MESSAGE_APPROVAL", "MESSAGE_REVISION", "MESSAGE_REJECTION"
DECISION_KINDS = (APPROVAL, REVISION, REJECTION)
REVIEW, AUTOMATIC = "review", "automatic"
MODES = (AUTOMATIC, REVIEW)
UNREADABLE = "unreadable"
HELD_BY_REVIEW = "REVIEW"
# The tag an earlier version gave a row held because the mode could not be read; no row gets it now,
# and `release_unreadable_holds` releases the ones on record (G4).
LEGACY_HELD_BY_UNREADABLE_MODE = "UNREADABLE_MODE"
STATE_FILE = "setup-state.json"   # setup_steps.STATE_FILE (read here without importing the Setup module)
DATABASE = Path(paths.STATE) / paths.DATABASE   # dashboard.DATABASE
# LinkedIn's limit on a connection note (owner, 2026-10-22: the platform's limit, not a judge).
CONNECTION_NOTE_LIMIT = 300
CONNECTION_NOTE_PURPOSES = frozenset({"INVITE", "PREPARE_CAMPAIGN"})
# What an owner revision may change in an action (anything else is not the owner's edit).
REVISABLE_KEYS = frozenset({"message", "note", "sequence", "verification", "followup_action_ref"})
RESEARCH_SOURCE = "rw_drafts"   # outreach_promotion.SOURCE_KIND
REJECTED_SLOT = "REJECTED:"
QUEUE_REJECTED = "REJECTED_BY_OWNER"


class ModeError(ValueError):
    """An unknown mode value: refused, nothing written (review fix P6)."""


def _now(now=None):
    return engagement.timestamp(now or dt.datetime.now(dt.timezone.utc)).isoformat()


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _has_table(database, name):
    return database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# --- The mode -------------------------------------------------------------------------------------

def _initialize_mode(database):
    database.execute("""CREATE TABLE IF NOT EXISTS approval_mode (
        id INTEGER PRIMARY KEY CHECK (id=1), mode TEXT NOT NULL, set_at TEXT NOT NULL)""")


def _initialize_dismissals(database):
    database.execute("""CREATE TABLE IF NOT EXISTS approval_dismissals (
        action_id TEXT NOT NULL, payload_hash TEXT NOT NULL, dismissed_at TEXT NOT NULL,
        PRIMARY KEY (action_id, payload_hash))""")


def initialize(database):
    """Tables the decisions write. Never inside a caller's transaction (initialize_actions commits)."""
    import outreach
    import outreach_agent
    import outreach_promotion
    outreach.initialize_actions(database)
    outreach_promotion.initialize_owner_records(database)
    outreach_agent.initialize(database)
    _initialize_mode(database)
    _initialize_dismissals(database)
    database.commit()


def _stored(database):
    try:
        row = database.execute("SELECT mode FROM approval_mode WHERE id=1").fetchone()
    except sqlite3.OperationalError as error:
        return None if "no such table" in str(error) else UNREADABLE
    except sqlite3.DatabaseError:
        return UNREADABLE
    if row is None:
        return None
    return row[0] if row[0] in MODES else UNREADABLE


def _from_file(home=None):
    try:
        import user_profile
        path = (Path(home) if home is not None else user_profile.home()) / STATE_FILE
        try:
            path.stat()
        except FileNotFoundError:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return UNREADABLE
    if not isinstance(value, dict):
        return UNREADABLE
    mode = value.get("mode", AUTOMATIC)
    return mode if mode in MODES else UNREADABLE


def stored_mode(database):
    """The database's mode: None when no row (or no table) is on record, or when it cannot be read
    (an unreadable store decides nothing, G4)."""
    value = _stored(database)
    return None if value == UNREADABLE else value


def file_mode(home=None):
    """setup-state.json's mode: None when the file does not exist, or exists but cannot be read or
    parsed, or names no known mode (an unreadable store decides nothing, G4). A file without a mode
    (Setup started, step 5 not reached) is the default, Automatic (D23)."""
    value = _from_file(home)
    return None if value == UNREADABLE else value


def mode_reading(database, home=None):
    """(mode, why): (REVIEW, HELD_BY_REVIEW) when a store reads Review; otherwise (AUTOMATIC, None),
    an unreadable store deciding nothing (G4: with neither readable, the fresh-install default)."""
    if REVIEW in (stored_mode(database), file_mode(home)):
        return REVIEW, HELD_BY_REVIEW
    return AUTOMATIC, None


def mode(database, home=None):
    """REVIEW or AUTOMATIC as the send path sees it: Review when either store reads Review."""
    return mode_reading(database, home)[0]


def install_mode(root, home=None):
    """The mode as the send path reads it, for the dashboard's chip and Settings > Mode (P13): the
    install's database read only (when it exists) and the file."""
    import overview_page  # its snapshot read leaves no -wal/-shm side files behind (a page load writes nothing)
    path = paths.database(root)

    def read(database):
        if database is None:
            with contextlib.closing(sqlite3.connect(":memory:")) as empty:
                return mode(empty, home)
        return mode(database, home)
    try:
        return overview_page.snapshot(path, read, lambda: sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5))
    except sqlite3.DatabaseError:
        return read(None)   # an unreadable database decides nothing (G4): the file alone


def _checked(value):
    if value not in MODES:
        raise ModeError("MODE_UNKNOWN")
    return value


def store_mode(database, value, now=None):
    """Write the database's mode (inside the caller's transaction when there is one)."""
    _checked(value)
    _initialize_mode(database)
    database.execute("INSERT INTO approval_mode VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,set_at=excluded.set_at",
                     (value, _now(now)))


def _write_file_mode(value, home=None):
    import setup_steps
    return setup_steps.set_mode(value, home)


def _check_file_writable(home=None):
    """GL-M1: refuse before the database row changes when the file half cannot be written (an
    unreadable setup-state.json), so "nothing was saved" stays true and both stores agree."""
    import setup_steps
    setup_steps.check_state_writable(home)


def set_mode(database, value, *, home=None, now=None):
    """Set the mode: the database first, then the file (design 3.3). An unknown value is refused, and
    so is a setup file that cannot be read (StateFileUnreadable), both before anything is written."""
    _checked(value)
    _check_file_writable(home)
    with engagement.transaction(database):
        store_mode(database, value, now)
    return _write_file_mode(value, home)


def set_install_mode(value, *, root, home=None, now=None):
    """The dashboard's Setup step 5 and Settings > Mode. An unknown value raises ModeError before
    anything is written. The database row is written first, creating the install's database (and
    its approval_mode table) when no cycle has made it yet (GL2: Review never lives only in the
    file), then the file. A setup file that cannot be read raises StateFileUnreadable before the
    database is touched (GL-M1)."""
    _checked(value)
    _check_file_writable(home)
    path = paths.database(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.closing(sqlite3.connect(path, timeout=10)) as database:
        with engagement.transaction(database):
            store_mode(database, value, now)
    return _write_file_mode(value, home)


# --- Decision records -----------------------------------------------------------------------------

def decisions(database, action_id, identity=None):
    """The owner's decisions on one action, oldest first, each integrity-checked (payload_sha256).
    With the action's `identity` the read uses the (identity, kind) index (E6.T4 review 12)."""
    if not _has_table(database, "outreach_owner_records"):
        return []
    rows = database.execute("""SELECT record_id,kind,recorded_at,payload,payload_sha256 FROM outreach_owner_records
        WHERE """ + ("identity=? AND " if identity is not None else "") + """kind IN (?,?,?) AND json_valid(payload)
        AND json_extract(payload,'$.action_id')=? ORDER BY recorded_at,rowid""",
        ((identity,) if identity is not None else ()) + (*DECISION_KINDS, action_id)).fetchall()
    found = []
    for record_id, kind, recorded_at, payload, payload_sha256 in rows:
        if _sha(payload) != payload_sha256:
            continue
        found.append(dict(json.loads(payload), record_id=record_id, kind=kind, recorded_at=recorded_at))
    return found


def approval(database, action_id, payload_hash):
    """The owner's approval (plain or revised) of exactly this payload, else None."""
    for record in reversed(decisions(database, action_id)):
        if record["kind"] in (APPROVAL, REVISION) and record.get("approved_payload_hash") == payload_hash:
            return record
    return None


def approval_for(database, action):
    """The approval of this action as stored (its exact payload), else None."""
    if not isinstance(action, dict) or not isinstance(action.get("action_id"), str):
        return None
    if not _has_table(database, "outreach_actions"):
        return None
    row = database.execute("SELECT payload,payload_hash FROM outreach_actions WHERE action_id=?",
                           (action["action_id"],)).fetchone()
    if row is None or _sha(row[0]) != row[1] or json.loads(row[0]) != action:
        return None
    return approval(database, action["action_id"], row[1])


def revision_of(database, action_id, payload_hash):
    """The owner's MESSAGE_REVISION that produced exactly this payload, else None."""
    record = approval(database, action_id, payload_hash)
    return record if record is not None and record["kind"] == REVISION else None


def revised_binding(database, action_id, action, binding):
    """True when `action` is the owner's revision of a payload that matched `binding` (key by key),
    changing only what a revision may change (review fix P2: a campaign preparation's binding)."""
    try:
        record = revision_of(database, action_id, _sha(json.dumps(action, sort_keys=True)))
        if record is None or not isinstance(record.get("original_payload"), str):
            return False
        original = json.loads(record["original_payload"])
        if not all(original.get(key) == value for key, value in binding.items()):
            return False
        changed = {key for key in set(original) | set(action) if original.get(key) != action.get(key)}
        return changed <= REVISABLE_KEYS
    except Exception:
        return False


def verified_at(database, action):
    """When the message's content counts as checked: its verified_at, or the owner's approval of this
    exact text when that is later (owner, 2026-10-22: an approval after 7 days counts as fresh; the
    approval time becomes verified_at). An unreadable record leaves verified_at as stated."""
    stated = (action.get("verification") or {}).get("verified_at") if isinstance(action, dict) else None
    try:
        record = approval_for(database, action)
        if record is None:
            return stated
        decided = record["decided_at"]
        if stated is None or engagement.timestamp(decided) > engagement.timestamp(stated):
            return decided
    except Exception:
        pass
    return stated


def decision(action, record):
    """The send decision the approved route records in place of the model's (design 1.4): READY,
    the conversation bound in the action, the judge-only findings left null."""
    return {"output_version": "owner-approval-v1", "decision": "READY",
            "reason": "Approved by the owner (" + record["kind"] + "); the send judge is not run again.",
            "next_action": "Send through the shared action guard.",
            "findings": {"prior_outreach": None, "has_reply": None, "negative_reply": None, "opt_out": None},
            "conversation_id": action.get("conversation_id"),
            "route": "INVITATION" if action.get("purpose") in CONNECTION_NOTE_PURPOSES else "EXISTING_THREAD",
            "receipt_observation": None, "receipt_message": None,
            "decision_source": "OWNER_APPROVAL:" + record["record_id"]}


def _record(database, kind, identity, payload, now, record_id=None):
    record_id = record_id or "message-decision:" + uuid.uuid4().hex
    encoded = json.dumps(payload, sort_keys=True)
    database.execute("INSERT INTO outreach_owner_records VALUES (?,?,?,?,?,?)",
                     (record_id, kind, identity, now, encoded, _sha(encoded)))
    return record_id


# --- The hold (point H, inside outreach.cycle's claim transaction) ---------------------------------

def hold_reason(database, row, home=None):
    """Why this unclaimed, gate-passing row must wait (HELD_BY_REVIEW), or None: Review mode (read here, in the claim transaction) and no approval of this exact payload.
    Automatic mode reads nothing else."""
    current, why = mode_reading(database, home)
    if current != REVIEW:
        return None
    return why if approval(database, row["action_id"], row["payload_hash"]) is None else None


def should_hold(database, row, home=None):
    return hold_reason(database, row, home) is not None


def mark_held(database, row, proof, now, reason=HELD_BY_REVIEW):
    """Move the row to AWAITING_APPROVAL (no claim). The caller commits. True when the row changed."""
    held = {"awaiting_approval": {"held_at": engagement.timestamp(now).isoformat(), "held_by": reason,
                                  "evidence": proof.get("evidence"), "agent_decision": proof.get("agent_decision")}}
    changed = database.execute("""UPDATE outreach_actions SET state=?,result=? WHERE action_id=?
        AND state IN ('PENDING','BLOCKED') AND claimed_at IS NULL AND payload_hash=?""",
        (AWAITING, json.dumps(held, sort_keys=True), row["action_id"], row["payload_hash"]))
    return changed.rowcount == 1


def launch_hold_reason(database, candidate, home=None):
    """The campaign launch (outreach.claim_send, inside its transaction): in Review mode a launch
    needs the owner's approval of the PREPARED action's exact payload (P1). None means launch."""
    current, why = mode_reading(database, home)
    if current != REVIEW:
        return None
    action_id, payload_hash = candidate.get("preparation_action_id"), candidate.get("preparation_payload_hash")
    if isinstance(action_id, str) and isinstance(payload_hash, str) and approval(database, action_id, payload_hash):
        return None
    return why


def mark_launch_held(database, identity, reason, now=None):
    changed = database.execute("""UPDATE outreach_queue SET state=?,gates=? WHERE identity=?
        AND state IN ('PENDING_VALIDATION','BLOCKED')""",
        (AWAITING, json.dumps({"held_by": reason, "held_at": _now(now)}, sort_keys=True), identity))
    return changed.rowcount == 1


def _held_by(text, key=None):
    try:
        value = json.loads(text) if text else {}
        value = value.get(key) if key else value
        return value.get("held_by") if isinstance(value, dict) else None
    except (TypeError, ValueError, AttributeError):
        return None


def release_unreadable_holds(database, home=None, now=None):
    """G4: an unreadable mode no longer holds. A row an earlier version held for that reason (tagged
    UNREADABLE_MODE, not Review) goes back to PENDING (a campaign launch back to PENDING_VALIDATION)
    on the next cycle, whatever the mode reads now: the cycle's own hold then holds it again only for
    a readable Review, so none stays stuck. Returns the action ids and campaign ids released."""
    if not _has_table(database, "outreach_actions"):
        return []
    released = []
    rows = [(action_id, result) for action_id, result in database.execute(
        "SELECT action_id,result FROM outreach_actions WHERE state=?", (AWAITING,))
        if _held_by(result, "awaiting_approval") == LEGACY_HELD_BY_UNREADABLE_MODE]
    queued = [(identity, campaign_id, gates) for identity, campaign_id, gates in database.execute(
        "SELECT identity,campaign_id,gates FROM outreach_queue WHERE state=?", (AWAITING,))
        if _held_by(gates) == LEGACY_HELD_BY_UNREADABLE_MODE] if _has_table(database, "outreach_queue") else []
    if not rows and not queued:
        return []
    with engagement.transaction(database):
        for action_id, result in rows:
            changed = database.execute("UPDATE outreach_actions SET state='PENDING',result=NULL WHERE action_id=? AND state=? AND result IS ?",
                                       (action_id, AWAITING, result))
            if changed.rowcount:
                released.append(action_id)
        for identity, campaign_id, gates in queued:
            changed = database.execute("UPDATE outreach_queue SET state='PENDING_VALIDATION',gates=NULL WHERE identity=? AND state=? AND gates IS ?",
                                       (identity, AWAITING, gates))
            if changed.rowcount:
                released.append(campaign_id)
    return released


def _stopped(database, action):
    """Why a waiting message can no longer go: the person's outreach is closed, or they opted out."""
    if engagement.closure_status(database, action["profile"]):
        return "OUTREACH_CLOSED"
    if any(event["kind"] == "opt_out" or (event.get("inference") or {}).get("sentiment") in {"negative", "opt_out"}
           for event in engagement.events_for(database, action["profile"])):
        return "OPTED_OUT"
    return None


def close_stopped(database, now=None):
    """P4: a waiting message to a closed or opted-out person is CLOSED, with the reason recorded, so it
    leaves Approvals and Needs you. Returns the action ids closed."""
    if not _has_table(database, "outreach_actions"):
        return []
    rows = database.execute("SELECT action_id,payload,payload_hash,result FROM outreach_actions WHERE state=?", (AWAITING,)).fetchall()
    if not rows:
        return []
    closed = []
    for action_id, payload, payload_hash, result in rows:
        reason = _stopped(database, json.loads(payload))
        if reason is None:
            continue
        with engagement.transaction(database):
            changed = database.execute("""UPDATE outreach_actions SET state='CLOSED',result=? WHERE action_id=? AND state=?
                AND payload_hash=?""", (json.dumps({"closed_while_awaiting_approval": {"reason": reason, "closed_at": _now(now)}}),
                                        action_id, AWAITING, payload_hash))
        if changed.rowcount:
            closed.append(action_id)
    return closed


# --- What waits -------------------------------------------------------------------------------------

def _queue_holds(database):
    if not _has_table(database, "outreach_queue"):
        return []
    return database.execute("SELECT identity,campaign_id,payload,gates FROM outreach_queue WHERE state=? ORDER BY rowid",
                            (AWAITING,)).fetchall()


def waiting_count(database):
    """Messages waiting for the owner (Overview's "Needs you", Settings' "N messages waiting"): held
    messages and held campaign launches, exactly the cards Approvals shows (`waiting_read`; an unreadable
    row is never a card). Reads only; 0 when the outreach tables are not there yet."""
    return len(waiting_read(database)[0])


def waiting(database):
    """The waiting messages, oldest hold first: what the Approvals page and the mode-switch question
    list (action_id and payload_hash are what a decision must be given back). `stage` is MESSAGE, or
    LAUNCH for a prepared HeyReach campaign (approve or reject only: its text is already in it)."""
    return waiting_read(database)[0]


def _object(text):
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def waiting_read(database):
    """(items, unreadable): `waiting`'s list and how many waiting rows could not be read (malformed
    JSON; E6.T4 review 11). An unreadable row is never listed, so it can never be decided on."""
    items, unreadable = [], 0
    if _has_table(database, "outreach_actions"):
        for action_id, payload, payload_hash, result in database.execute(
                "SELECT action_id,payload,payload_hash,result FROM outreach_actions WHERE state=? ORDER BY rowid", (AWAITING,)):
            action = _object(payload)
            if action is None:
                unreadable += 1
                continue
            held = (_object(result) or {}).get("awaiting_approval")
            held = held if isinstance(held, dict) else {}
            items.append({"action_id": action_id, "payload_hash": payload_hash, "purpose": action.get("purpose"),
                          "stage": "MESSAGE", "revisable": True,
                          "recipient": action.get("recipient"), "company": action.get("company"),
                          "profile": action.get("profile"), "message": action.get("message"),
                          "limit": CONNECTION_NOTE_LIMIT if action.get("purpose") in CONNECTION_NOTE_PURPOSES else None,
                          "held_at": held.get("held_at"), "evidence": held.get("evidence")})
    for identity, campaign_id, payload, gates in _queue_holds(database):
        candidate = _object(payload)
        if candidate is None:
            unreadable += 1
            continue
        held = _object(gates) or {}
        items.append({"action_id": candidate.get("preparation_action_id"), "payload_hash": candidate.get("preparation_payload_hash"),
                      "purpose": "PREPARE_CAMPAIGN", "stage": "LAUNCH", "revisable": False, "campaign_id": campaign_id,
                      "recipient": candidate.get("recipient"), "company": candidate.get("company"),
                      "profile": candidate.get("profile"), "message": candidate.get("message"),
                      "limit": CONNECTION_NOTE_LIMIT, "held_at": held.get("held_at") if isinstance(held, dict) else None,
                      "evidence": (candidate.get("verification") or {}).get("history_evidence")})
    return sorted(items, key=lambda item: (str(item["held_at"] or ""), str(item["action_id"]))), unreadable


# --- Approved, then refused by a sending rule (E6.T4, owner D3) ------------------------------------

def refused_after_approval(database, limit=25, offset=0):
    """Messages the owner approved whose send a sending rule then refused: the row is BLOCKED with its
    gate codes (outreach.cycle) and the owner's approval is of exactly its payload. Newest first, at
    most `limit` after skipping `offset` (Approvals' paging), without those the owner dismissed. Reads only."""
    if not (_has_table(database, "outreach_actions") and _has_table(database, "outreach_owner_records")):
        return []
    dismissed = set()
    if _has_table(database, "approval_dismissals"):
        dismissed = {tuple(row) for row in database.execute("SELECT action_id,payload_hash FROM approval_dismissals")}
    found = []
    for action_id, identity, payload, payload_hash, result in database.execute(
            "SELECT action_id,identity,payload,payload_hash,result FROM outreach_actions WHERE state='BLOCKED' ORDER BY rowid DESC LIMIT 500"):
        if (action_id, payload_hash) in dismissed:
            continue
        action = _object(payload)
        if action is None:
            continue
        record = next((record for record in reversed(decisions(database, action_id, identity))
                       if record["kind"] in (APPROVAL, REVISION) and record.get("approved_payload_hash") == payload_hash), None)
        if record is None:
            continue
        try:
            gates = json.loads(result) if result else []
        except ValueError:
            gates = []
        if offset > 0:
            offset -= 1
            continue
        found.append({"action_id": action_id, "payload_hash": payload_hash, "recipient": action.get("recipient"),
                      "company": action.get("company"), "purpose": action.get("purpose"), "message": action.get("message"),
                      "profile": action.get("profile"), "approved_at": record.get("decided_at"),
                      "gates": [gate for gate in gates if isinstance(gate, str)] if isinstance(gates, list) else []})
        if len(found) >= limit:
            break
    return found


def refused_count(database):
    """How many cards `refused_after_approval` has in all (Approvals' "Page X of Y (N)"): the same
    bounded scan, counted without building the cards."""
    if not (_has_table(database, "outreach_actions") and _has_table(database, "outreach_owner_records")):
        return 0
    dismissed = set()
    if _has_table(database, "approval_dismissals"):
        dismissed = {tuple(row) for row in database.execute("SELECT action_id,payload_hash FROM approval_dismissals")}
    total = 0
    for action_id, identity, payload, payload_hash in database.execute(
            "SELECT action_id,identity,payload,payload_hash FROM outreach_actions WHERE state='BLOCKED' ORDER BY rowid DESC LIMIT 500"):
        if (action_id, payload_hash) in dismissed or _object(payload) is None:
            continue
        if any(record["kind"] in (APPROVAL, REVISION) and record.get("approved_payload_hash") == payload_hash
               for record in decisions(database, action_id, identity)):
            total += 1
    return total

def dismiss_refused(database, action_id, payload_hash, now=None):
    """[Dismiss] on a "couldn't send" card: only the card goes; the message stays as it is (never sent
    by this). True when a card was dismissed."""
    with engagement.transaction(database):
        _initialize_dismissals(database)
        changed = database.execute("INSERT OR IGNORE INTO approval_dismissals VALUES (?,?,?)", (action_id, payload_hash, _now(now)))
    return changed.rowcount == 1


# --- Rejected, never sent (research may draft the person again) ------------------------------------

def rejected_unsent(database, action_id):
    """The owner rejected this exact message and it was never claimed or sent."""
    if not (_has_table(database, "outreach_actions") and _has_table(database, "outreach_owner_records")):
        return False
    row = database.execute("SELECT state,payload_hash,claimed_at FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    if row is None or row[0] != "CANCELLED_BEFORE_SEND" or row[2] is not None:
        return False
    if _has_table(database, "outreach_action_claims") and database.execute(
            "SELECT 1 FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone():
        return False
    return any(record["kind"] == REJECTION and record.get("shown_payload_hash") == row[1]
               for record in decisions(database, action_id))


def rejected_unsent_ids(database, identities):
    """Of these identities' outreach actions, the ones rejected by the owner and never sent."""
    identities = sorted(identities)
    if not identities or not _has_table(database, "outreach_actions"):
        return set()
    rows = database.execute("SELECT action_id FROM outreach_actions WHERE state='CANCELLED_BEFORE_SEND' AND identity IN ("
                            + ",".join("?" for unused in identities) + ")", identities).fetchall()
    return {row[0] for row in rows if rejected_unsent(database, row[0])}


def only_rejected(database, identities):
    """The person has outreach actions and every one of them was rejected by the owner, never sent."""
    identities = sorted(identities)
    if not identities or not _has_table(database, "outreach_actions"):
        return False
    actions = {row[0] for row in database.execute("SELECT action_id FROM outreach_actions WHERE identity IN ("
                                                  + ",".join("?" for unused in identities) + ")", identities)}
    return bool(actions) and actions <= rejected_unsent_ids(database, identities)


# --- Decisions (E6.T4 calls these) -------------------------------------------------------------------

def _waiting_row(database, action_id, shown_payload_hash):
    row = database.execute("SELECT action_id,identity,slot,payload,payload_hash,state,claimed_at FROM outreach_actions WHERE action_id=?",
                           (action_id,)).fetchone()
    if row is None:
        return None
    row = dict(zip(("action_id", "identity", "slot", "payload", "payload_hash", "state", "claimed_at"), row))
    if (row["state"] != AWAITING or row["claimed_at"] is not None or _sha(row["payload"]) != row["payload_hash"]
            or row["payload_hash"] != shown_payload_hash):
        return None
    if database.execute("SELECT 1 FROM outreach_action_claims WHERE action_id=?", (action_id,)).fetchone():
        return None
    return row


def _launch_row(database, action_id, shown_payload_hash):
    """A campaign launch held for this PREPARED action and exactly the payload the owner saw."""
    if not _has_table(database, "outreach_queue"):
        return None
    row = database.execute("SELECT identity,payload,payload_hash,state FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    if row is None or row[3] != "PREPARED" or row[2] != shown_payload_hash or _sha(row[1]) != row[2]:
        return None
    for identity, campaign_id, payload, gates in _queue_holds(database):
        candidate = _object(payload) or {}
        if candidate.get("preparation_action_id") == action_id and candidate.get("preparation_payload_hash") == shown_payload_hash:
            return {"action_id": action_id, "identity": row[0], "payload": row[1], "payload_hash": row[2],
                    "queue_identity": identity, "campaign_id": campaign_id, "gates": gates}
    return None


def _changed(database, action_id, **extra):
    row = database.execute("SELECT state FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()
    return dict(extra, state="CHANGED", action_id=action_id, current_state=row[0] if row else None,
                message="This message changed since it was shown. Reload to see where it stands.")


class _Refused(Exception):
    def __init__(self, result):
        super().__init__(result.get("reason"))
        self.result = result


def _shown(shown_payload_hash):
    if not isinstance(shown_payload_hash, str) or not shown_payload_hash:
        raise ValueError("SHOWN_PAYLOAD_HASH_REQUIRED")   # P8: the compare-and-swap is never off


def _approval_payload(row, decided_via, now):
    action = json.loads(row["payload"])
    return {"action_id": row["action_id"], "purpose": action.get("purpose"), "recipient": action.get("recipient"),
            "shown_payload_hash": row["payload_hash"], "approved_payload_hash": row["payload_hash"],
            "original_message": action.get("message"), "decided_via": decided_via, "decided_at": now}


def _approve_row(database, row, now, decided_via):
    record_id = _record(database, APPROVAL, row["identity"], _approval_payload(row, decided_via, now), now)
    changed = database.execute("""UPDATE outreach_actions SET state='PENDING',result=NULL
        WHERE action_id=? AND state=? AND payload_hash=? AND claimed_at IS NULL""", (row["action_id"], AWAITING, row["payload_hash"]))
    if changed.rowcount != 1:
        raise _Refused(_changed(database, row["action_id"]))
    return {"state": "APPROVED", "action_id": row["action_id"], "record_id": record_id, "payload_hash": row["payload_hash"]}


def _approve_launch(database, launch, now, decided_via):
    record_id = _record(database, APPROVAL, launch["identity"], dict(_approval_payload(launch, decided_via, now), stage="LAUNCH",
                                                                     campaign_id=launch["campaign_id"]), now)
    changed = database.execute("UPDATE outreach_queue SET state='PENDING_VALIDATION',gates=NULL WHERE identity=? AND state=? AND gates IS ?",
                               (launch["queue_identity"], AWAITING, launch["gates"]))
    if changed.rowcount != 1:
        raise _Refused(_changed(database, launch["action_id"]))
    return {"state": "APPROVED", "action_id": launch["action_id"], "record_id": record_id,
            "payload_hash": launch["payload_hash"], "stage": "LAUNCH"}


def _begin(database):
    if not database.in_transaction:
        initialize(database)


def approve(database, action_id, *, shown_payload_hash, now=None, decided_via="dashboard"):
    """Approve: the message goes on exactly as written. One transaction: the MESSAGE_APPROVAL record
    and the state change AWAITING_APPROVAL -> PENDING (or, for a held campaign launch, the queue row
    back to PENDING_VALIDATION), or nothing."""
    _shown(shown_payload_hash)
    _begin(database)
    now = _now(now)
    try:
        with engagement.transaction(database):
            row = _waiting_row(database, action_id, shown_payload_hash)
            if row is not None:
                return _approve_row(database, row, now, decided_via)
            launch = _launch_row(database, action_id, shown_payload_hash)
            if launch is not None:
                return _approve_launch(database, launch, now, decided_via)
            raise _Refused(_changed(database, action_id))
    except _Refused as refusal:
        return refusal.result


def revise_and_approve(database, action_id, text, *, shown_payload_hash, now=None, decided_via="dashboard"):
    """Revise and Approve: the edited text replaces the original in the same row (one row, one claim,
    one send) through `outreach.enqueue_action(replacing=)`, and is approved in the same step. The
    text is kept byte for byte (no dash or length fitting: it is the owner's). A connection note over
    LinkedIn's 300 characters is refused before anything is written, and the edit comes back in the
    result (`text`) for the page to keep. The original stays in the MESSAGE_REVISION record."""
    _shown(shown_payload_hash)
    _begin(database)
    now = _now(now)
    if isinstance(text, str):   # one newline convention, as the page counts it (E6.T4 review 21)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept = text if isinstance(text, str) else ""
    if not isinstance(text, str) or not text.strip():
        return {"state": "REFUSED", "action_id": action_id, "reason": "MESSAGE_EMPTY", "text": kept,
                "message": "Write the message, or reject it."}
    try:
        with engagement.transaction(database):
            row = _waiting_row(database, action_id, shown_payload_hash)
            if row is None:
                if _launch_row(database, action_id, shown_payload_hash) is not None:
                    raise _Refused({"state": "REFUSED", "action_id": action_id, "reason": "CAMPAIGN_ALREADY_PREPARED",
                                    "text": kept, "message": "This invitation is already in a prepared campaign. Approve or reject it."})
                raise _Refused(_changed(database, action_id, text=kept))
            action = json.loads(row["payload"])
            purpose = action.get("purpose")
            if purpose in CONNECTION_NOTE_PURPOSES and len(text) > CONNECTION_NOTE_LIMIT:
                over = len(text) - CONNECTION_NOTE_LIMIT
                raise _Refused({"state": "REFUSED", "action_id": action_id, "reason": "INVITATION_NOTE_TOO_LONG",
                                "text": kept, "length": len(text), "limit": CONNECTION_NOTE_LIMIT,
                                "message": "LinkedIn allows 300 characters in a connection note. Shorten it by "
                                           + str(over) + "."})
            if text == action.get("message"):
                return _approve_row(database, row, now, decided_via)
            record_id = "message-decision:" + uuid.uuid4().hex
            edited = dict(action, message=text)
            if purpose == "INVITE":
                edited["note"] = text
            if purpose == "PREPARE_CAMPAIGN" and isinstance(action.get("sequence"), dict):
                # The draft campaign's connection request carries the note itself (outreach.action_gates
                # PREPARATION_SEQUENCE_MISMATCH compares it to the message).
                sequence = json.loads(json.dumps(action["sequence"]))
                payload = sequence.setdefault("payload", {})
                payload["messages"] = [text]
                if payload.get("fallbackMessage") == action.get("message"):
                    payload["fallbackMessage"] = text
                edited["sequence"] = sequence
            verification = dict(action.get("verification") or {})
            verification["verified_at"] = now
            verification["content_sources"] = list(verification.get("content_sources") or []) + ["outreach_owner_records:" + record_id]
            edited["verification"] = verification
            if purpose in {"FOLLOWUP", "URGENT"} and "followup_action_ref" in action:
                # followup_execution.binding_gates compares the action to its ACTION audit row
                # (FOLLOWUP_IMMUTABLE_ACTION_MISMATCH): the edit gets its own row, as a rewrite does.
                import followup_execution
                import outreach_agent
                bare = {key: value for key, value in edited.items() if key != "followup_action_ref"}
                reference = outreach_agent.digest([action_id, followup_execution.PHASE, "ACTION", bare])
                database.execute("INSERT OR IGNORE INTO outreach_agent_audit VALUES (?,?,?,?,?,?)",
                                 (reference, action_id, followup_execution.PHASE, "ACTION", now, outreach_agent.encoded(bare)))
                edited["followup_action_ref"] = reference
            edited_payload = json.dumps(edited, sort_keys=True)
            edited_hash = _sha(edited_payload)
            _record(database, REVISION, row["identity"], {
                "action_id": action_id, "purpose": purpose, "recipient": action.get("recipient"),
                "shown_payload_hash": row["payload_hash"], "original_message": action.get("message"),
                "original_payload": row["payload"], "edited_message": text, "edited_payload": edited_payload,
                "edited_payload_hash": edited_hash, "approved_payload_hash": edited_hash,
                "decided_via": decided_via, "decided_at": now}, now, record_id)
            import strategy_execution
            strategy_execution.initialize(database)  # drops legacy bound-action triggers
            import outreach
            try:
                swapped = outreach.enqueue_action(database, edited, replacing=row["payload_hash"])
            except ValueError as error:
                raise _Refused({"state": "REFUSED", "action_id": action_id, "reason": str(error)[:300], "text": kept,
                                "message": "This message cannot be changed now. Reload to see where it stands."}) from None
            if swapped.get("state") != "REPLACED":
                raise _Refused(_changed(database, action_id, text=kept))
            return {"state": "REVISED", "action_id": action_id, "record_id": record_id, "payload_hash": edited_hash,
                    "original_payload_hash": row["payload_hash"]}
    except _Refused as refusal:
        return refusal.result


def reject(database, action_id, *, shown_payload_hash, now=None, decided_via="dashboard", reason=None):
    """Reject: the message is closed and never sent (AWAITING_APPROVAL -> CANCELLED_BEFORE_SEND, a
    terminal state the cycle never selects; a held campaign launch's queue row -> REJECTED_BY_OWNER).
    A research note's slot is freed (REJECTED:<action_id>), so a later research run that drafts the
    person again can queue a new action (owner, 2026-10-22). `reason` is the owner's optional note
    (E6.T4), kept in the MESSAGE_REJECTION record as written (at most 1000 characters); nothing reads
    it to decide anything."""
    _shown(shown_payload_hash)
    noted = {"reason": reason.strip()[:1000]} if isinstance(reason, str) and reason.strip() else {}
    _begin(database)
    now = _now(now)
    try:
        with engagement.transaction(database):
            row = _waiting_row(database, action_id, shown_payload_hash)
            if row is None:
                launch = _launch_row(database, action_id, shown_payload_hash)
                if launch is None:
                    raise _Refused(_changed(database, action_id))
                action = json.loads(launch["payload"])
                record_id = _record(database, REJECTION, launch["identity"], {
                    "action_id": action_id, "purpose": action.get("purpose"), "recipient": action.get("recipient"),
                    "shown_payload_hash": launch["payload_hash"], "original_message": action.get("message"),
                    "stage": "LAUNCH", "campaign_id": launch["campaign_id"], "decided_via": decided_via, "decided_at": now,
                    **noted}, now)
                changed = database.execute("UPDATE outreach_queue SET state=?,gates=? WHERE identity=? AND state=? AND gates IS ?",
                                           (QUEUE_REJECTED, json.dumps({"owner_rejection": record_id}), launch["queue_identity"],
                                            AWAITING, launch["gates"]))
                if changed.rowcount != 1:
                    raise _Refused(_changed(database, action_id))
                return {"state": "REJECTED", "action_id": action_id, "record_id": record_id, "stage": "LAUNCH"}
            action = json.loads(row["payload"])
            record_id = _record(database, REJECTION, row["identity"], {
                "action_id": action_id, "purpose": action.get("purpose"), "recipient": action.get("recipient"),
                "shown_payload_hash": row["payload_hash"], "original_message": action.get("message"),
                "decided_via": decided_via, "decided_at": now, **noted}, now)
            research_note = (action.get("source") or {}).get("kind") == RESEARCH_SOURCE
            if research_note:
                import strategy_execution
                strategy_execution.initialize(database)  # drops legacy bound-action triggers
            changed = database.execute("""UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=?,slot=?
                WHERE action_id=? AND state=? AND payload_hash=? AND claimed_at IS NULL""",
                (json.dumps({"owner_rejection": record_id}), REJECTED_SLOT + action_id if research_note else row["slot"],
                 action_id, AWAITING, row["payload_hash"]))
            if changed.rowcount != 1:
                raise _Refused(_changed(database, action_id))
            return {"state": "REJECTED", "action_id": action_id, "record_id": record_id}
    except _Refused as refusal:
        return refusal.result


def release_all(database, shown, *, home=None, now=None):
    """Review -> Automatic, "Send them": approve exactly the messages the question counted (`shown`:
    [(action_id, payload_hash)] or the dicts `waiting` returned; required, P7), decided_via
    mode_switch, and set the mode, all in one database transaction; then the mode file (database
    first, design 3.3). A row that changed meanwhile stays as it is and is listed in `changed`."""
    if shown is None:
        raise ValueError("SHOWN_MESSAGES_REQUIRED")
    _check_file_writable(home)
    _begin(database)
    now = _now(now)
    released, changed = [], []
    with engagement.transaction(database):
        for item in shown:
            action_id, payload_hash = (item["action_id"], item["payload_hash"]) if isinstance(item, dict) else item
            row = _waiting_row(database, action_id, payload_hash) if isinstance(payload_hash, str) else None
            launch = None if row is not None or not isinstance(payload_hash, str) else _launch_row(database, action_id, payload_hash)
            if row is not None:
                released.append(_approve_row(database, row, now, "mode_switch"))
            elif launch is not None:
                released.append(_approve_launch(database, launch, now, "mode_switch"))
            else:
                changed.append(_changed(database, action_id))
        store_mode(database, AUTOMATIC, now)
    _write_file_mode(AUTOMATIC, home)
    return {"state": "RELEASED", "mode": AUTOMATIC, "released": released, "changed": changed}


def keep_for_review(database, *, home=None, now=None):
    """Review -> Automatic, "Keep for review": only the mode changes. Waiting messages stay waiting
    (AWAITING_APPROVAL is never selected; only a decision releases one) and new ones go out."""
    _check_file_writable(home)
    _begin(database)
    with engagement.transaction(database):
        store_mode(database, AUTOMATIC, now)
    _write_file_mode(AUTOMATIC, home)
    return {"state": "KEPT", "mode": AUTOMATIC, "waiting": waiting_count(database)}


# --- Applications held by Review (owner, 2026-10-24) ------------------------------------------------

def held_application_keys(database):
    """THE "application to approve" rule: a job held by Review (BLOCKED on AWAITING_APPLICATION_APPROVAL)
    with its card waiting, oldest first. Approvals' "Applications waiting", Overview's Needs you row and
    Applications' "Waiting for your approval" badge all count by it."""
    if database is None or not _has_table(database, "application_approvals") or not _has_table(database, "application_jobs"):
        return []
    return [row[0] for row in database.execute(HELD_APPLICATIONS_SQL)]


HELD_APPLICATIONS_SQL = ("SELECT a.job_key FROM application_approvals a JOIN application_jobs j ON j.job_key=a.job_key "
                         "WHERE a.state='WAITING' AND j.state='BLOCKED' AND j.blocker='AWAITING_APPLICATION_APPROVAL' "
                         "ORDER BY a.held_at,a.job_key")
MULTI_CHOICE_TYPES = frozenset({"multi_choice", "multi_select", "multiselect", "checkboxes"})
PROSE_LENGTH = 80   # an answer this long (or with a line break) is edited in a box of several lines


PROSE_TYPES = frozenset({"long_text", "textarea", "paragraph"})


def application_control(item):
    """(control, options) a card shows for one answer - a held application's answer, or an owner
    question on Questions for you (`approvals_page.answer_control`; one mapping for both, owner
    2026-10-24): the form's own control as the read reported it ("select", "radio", "checkbox" for
    several choices, "text", "textarea"), else the one its answer type and options imply. A yes/no
    question without options is "radio" with Yes and No; a date or number is "date" / "number" (a held
    card shows a date as a date picker and a number as a one-line box); prose (a long-text type, a long question, a long answer, the
    unreadable-form note) is "textarea"."""
    kind = str(item.get("answer_type") or "").strip().casefold().replace(" ", "_").replace("-", "_")
    control = str(item.get("control") or "").strip().casefold()
    choices = [choice for choice in item.get("choices") or () if isinstance(choice, str)]
    value = item.get("value")
    if item.get("operational"):
        return "textarea", []
    if kind in MULTI_CHOICE_TYPES or isinstance(value, list) or (control == "checkbox" and len(choices) > 1):
        return "checkbox", choices or [str(choice) for choice in value or ()]
    if choices:
        return (control if control in ("radio", "select") else "radio" if len(choices) <= 5 else "select"), choices
    if kind in ("boolean", "bool", "yes_no", "yesno") or isinstance(value, bool):
        return "radio", ["Yes", "No"]
    if kind in ("date", "number"):
        return kind, []
    label = item.get("label") if isinstance(item.get("label"), str) else ""
    if (control == "textarea" or kind in PROSE_TYPES or len(label) > PROSE_LENGTH
            or (isinstance(value, str) and (len(value) > PROSE_LENGTH or "\n" in value))):
        return "textarea", []
    return "text", []


def shown_answer(item):
    """The answer as the card shows it: a label, or the list of labels ticked."""
    value = item.get("value")
    if isinstance(value, list):
        return [str(choice) for choice in value]
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


def input_name(item, route):
    """The card's form field for one answer: its own `answer:<field>`, or the cover letter's and the
    email's subject and body boxes."""
    import applications
    if item.get("cover_letter"):
        return "cover_letter"
    if route == "email" and item.get("semantic") == applications.EMAIL_BODY_SEMANTIC:
        return "body"
    if route == "email" and item.get("semantic") == applications.EMAIL_SUBJECT_SEMANTIC:
        return "subject"
    return "answer:" + item["field"]


class _NotAnOption(ValueError):
    pass


def _fitted(item, values):
    """The value the card posted for one answer, fitted to its control (a choice must be one of the
    form's options; several for a multi-select). `values` is the form's list for that field."""
    control, options = application_control(item)
    original = item.get("value")
    if control == "checkbox":
        chosen = [value for value in values if isinstance(value, str)]
        if any(value not in options for value in chosen):
            raise _NotAnOption(item["field"])
        return [option for option in options if option in chosen]
    if not values:
        return original                      # not on the form: kept as shown
    raw = values[0] if isinstance(values[0], str) else ""
    if control in ("radio", "select"):
        if raw not in options:
            raise _NotAnOption(item["field"])
        if raw == shown_answer(item):
            return original                  # the option shown chosen: the stored answer, unchanged
        return raw                           # exactly the option picked, never re-derived
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(original, (int, float)) and not isinstance(original, bool):
        try:
            number = float(text.strip())
            return int(number) if number.is_integer() else number
        except ValueError:
            return text
    return text


def _application_row(database, job_key, shown_payload_hash):
    import applications
    if not _has_table(database, "application_approvals"):
        return None, None
    row = database.execute("SELECT * FROM application_approvals WHERE job_key=?", (job_key,)).fetchone()
    job = database.execute("SELECT * FROM application_jobs WHERE job_key=?", (job_key,)).fetchone()
    if (row is None or job is None or row["state"] != "WAITING" or row["payload_hash"] != shown_payload_hash
            or job["state"] != "BLOCKED" or job["blocker"] != applications.AWAITING_APPLICATION_APPROVAL):
        return None, None
    return row, job


APPLICATION_CHANGED = "This application changed since it was shown. Reload to see where it stands."


def approve_application(database, job_key, form, *, shown_payload_hash, now=None, decided_via="dashboard"):
    """"Approve (my edits) and submit/send": exactly what the card shows is saved, one transaction.
    `form` is {input name: [values]} as posted (`input_name`). Every answer on the card is written to
    `application_answers` under OWNER_APPROVED_SOURCE (the answering step never answers it again), the
    approval records them, and the job is due now (APPLICATION_APPROVED). A choice that is not one of
    the form's options is refused and nothing is written."""
    import applications
    _shown(shown_payload_hash)
    now = _now(now)
    form = form if isinstance(form, dict) else {}
    with engagement.transaction(database):
        row, job = _application_row(database, job_key, shown_payload_hash)
        if row is None:
            return {"state": "CHANGED", "message": APPLICATION_CHANGED}
        snapshot = json.loads(row["snapshot"])
        approved, edited = {}, []
        try:
            for item in snapshot.get("questions") or ():
                value = _fitted(item, list(form.get(input_name(item, snapshot.get("route")), [])))
                approved[item["field"]] = {"semantic": item.get("semantic"), "value": value}
                if value != item.get("value"):
                    edited.append(item["field"])
        except _NotAnOption:
            return {"state": "REFUSED", "message": "One of the choices is not an option on this form. Reload and try again."}
        subject = None
        route = snapshot.get("route")
        if route == "email" and not any(input_name(item, route) == "subject" for item in snapshot.get("questions") or ()):
            # The subject box when no answer carries it (the posting names one, or none was drafted):
            # what the box says is the subject sent; an empty box keeps the posting's.
            posted = form.get("subject") or []
            typed = posted[0].strip() if posted and isinstance(posted[0], str) else ""
            subject = typed or snapshot.get("subject")
            if subject != snapshot.get("subject"):
                edited.append("subject")
        source = applications.OWNER_APPROVED_SOURCE + row["approval_id"]
        for field, answer in approved.items():
            database.execute("""INSERT INTO application_answers VALUES (?,?,?,?,?) ON CONFLICT(job_key,field)
                DO UPDATE SET semantic=excluded.semantic,value=excluded.value,source=excluded.source""",
                             (job_key, field, answer["semantic"], json.dumps(answer["value"]), source))
        # The approval covers the résumé that will be uploaded and where the application goes too
        # (`applications.approved_application` holds the job again if either changes).
        record = {"answers": approved, "subject": subject, "edited": edited, "decided_via": decided_via,
                  "resume_sha256": snapshot.get("resume_sha256"), "destination": snapshot.get("destination")}
        changed = database.execute("""UPDATE application_approvals SET state='APPROVED',decided_at=?,approved=?
            WHERE job_key=? AND state='WAITING' AND payload_hash=?""",
                                   (now, json.dumps(record, sort_keys=True), job_key, shown_payload_hash))
        if changed.rowcount != 1:
            raise RuntimeError("APPLICATION_APPROVAL_CHANGED")   # rolled back; cannot happen inside the transaction
        applications.hold(database, dict(job), applications.APPLICATION_APPROVED,
                          "Approved on Approvals; submitted on the next applications pass", now, "BLOCKED", None, route="ORDINARY")
        applications.event(database, job_key, "APPLICATION_APPROVED",
                           {"approval_id": row["approval_id"], "edited": edited, "decided_via": decided_via}, now)
        return {"state": "APPROVED", "edited": bool(edited), "route": snapshot.get("route")}


def reject_application(database, job_key, *, shown_payload_hash, reason=None, now=None, decided_via="dashboard"):
    """"Reject, never submit": the job is closed (ABANDONED, APPLICATION_REJECTED_BY_YOU) and never
    submitted or retried. `reason` is the owner's optional note, kept as written (at most 1000
    characters); nothing reads it to decide anything."""
    import applications
    _shown(shown_payload_hash)
    now = _now(now)
    noted = reason.strip()[:1000] if isinstance(reason, str) and reason.strip() else None
    with engagement.transaction(database):
        row, job = _application_row(database, job_key, shown_payload_hash)
        if row is None:
            return {"state": "CHANGED", "message": APPLICATION_CHANGED}
        database.execute("UPDATE application_approvals SET state='REJECTED',decided_at=?,reason=? WHERE job_key=?",
                         (now, noted, job_key))
        applications.hold(database, dict(job), applications.APPLICATION_REJECTED,
                          "You rejected this application on Approvals" + (": " + noted if noted else ""),
                          now, "ABANDONED", None, route="OWNER")
        applications.event(database, job_key, "APPLICATION_REJECTED",
                           {"approval_id": row["approval_id"], "reason": noted, "decided_via": decided_via}, now)
        snapshot = json.loads(row["snapshot"])
        return {"state": "REJECTED", "route": snapshot.get("route")}
