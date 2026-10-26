"""The Approvals page (E6.T4; E6.T0 spec section 6 and the Review -> Automatic question of section 10).

Five lists, each read from the record the rest of the product already writes:
- Messages waiting: `approvals.waiting_read` (outreach_actions AWAITING_APPROVAL, and held HeyReach
  launches in outreach_queue), 25 a page. A card shows the recipient, why research picked them (the
  research draft's person evidence, `rw_drafts`), the text and its length (300 for a connection note),
  the send judge's verdict and reason (the DECISION row of `outreach_agent_audit` behind the hold's
  evidence, read by action id), the conversation so far for a reply (`engagement_events` by identity)
  and "Edited by you" (`approvals.decisions`). A revisable card has ONE text box and one button: what
  the box says is what is approved (`approvals.revise_and_approve`, which approves unchanged text as
  written); a launch card has Approve only. Reject takes an optional reason. Every action carries the
  payload hash the card was rendered with. In Automatic mode the list is replaced by one line;
  messages still waiting (kept for review) stay listed.
- Applications waiting: applications Review mode held before submitting (owner, 2026-10-24;
  `approvals.held_application_keys`, `application_approvals`), 25 a page. A card shows where it goes,
  the résumé and every answer, read-only until "Edit answers" turns each into the form's own control
  (`approvals.application_control`); Approve saves exactly what the card shows
  (`approvals.approve_application`) and the job is submitted on the next applications pass, which the
  scheduler is asked to start now (`scheduler_loop.run_soon`); Reject closes it (`reject_application`).
- Couldn't send: approved messages a sending rule then refused (`approvals.refused_after_approval`;
  owner, 2026-10-22, D3), with the reason in plain words and [Dismiss] (`approvals.dismiss_refused`).
- Questions for you: `user_decisions` PENDING and unexpired (not the transport test), answered through
  `decision_transport.answer_locally` against the dashboard home's policy. An APPLIED answer is also
  kept in profile.json for later jobs (`setup_steps.save_owner_answer`; owner, 2026-10-22, "a"), except
  the "form not readable" note; other questions that answer makes stale are then closed and their
  jobs made due again (`decision_transport.refresh_stale`).
- Notes for you: `reply_tools.open_flags` (by rowid), with the person's name when one is on record.
- Recent notices: `notification_outbox` rows the Dashboard transport delivered. Each is shown in plain words
  (`notice_view`: company and role from the job, a link to it or the right page); the stored text is unchanged.

A page load (GET) writes nothing (`overview_page.snapshot`). Every action is a POST behind the
dashboard's token cookie and Origin check; with htmx it answers with its own card only (the other
cards, and anything typed in them, stay as they are), without JavaScript with the whole page. A
malformed row is never listed (counted as "could not be read").

`register(app, ...)` is called from `dashboard.create_app` before the placeholder routes, so its
GET /approvals is the one that answers.
"""

import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import approvals
import overview_page
import paths

DATABASE = Path(paths.STATE) / paths.DATABASE   # dashboard.DATABASE
CONVERSATION_KINDS = ("inbound", "outbound_observed", "initial_sent", "followup_sent")
CONVERSATION_LIMIT = 9      # the reply AI's own context: the invitation plus eight other messages
PAGE_SIZE = 25
QUESTION_LIMIT = 25         # a page of each list; every list pages (owner, 2026-10-24), none is capped
NOTE_LIMIT = 25
REFUSED_LIMIT = 25
NOTICE_LIMIT = 20
# Each list's own page parameter (and #anchor), so paging one list never resets another.
PAGE_PARAMS = {"messages": "page", "applications": "applications_page", "refused": "refused_page",
               "questions": "questions_page", "notes": "notes_page", "notices": "notices_page"}
APPLICATION_LIMIT = 25
LIST_SIZES = {"messages": PAGE_SIZE, "applications": APPLICATION_LIMIT, "refused": REFUSED_LIMIT, "questions": QUESTION_LIMIT,
              "notes": NOTE_LIMIT, "notices": NOTICE_LIMIT}
AUDIT_SCAN = 50             # DECISION rows read per card (newest first) to find the hold's verdict
PURPOSE_WORDS = {"INVITE": "Invitation note", "PREPARE_CAMPAIGN": "Invitation note",
                 "INITIAL": "First message", "FIRST_MESSAGE": "First message after they accepted",
                 "FOLLOWUP": "Follow-up", "URGENT": "Follow-up", "REPLY": "Reply"}
VERDICT_WORDS = {"READY": "Ready to send", "BLOCKED": "Not ready", "UNKNOWN": "Could not decide"}
# Plain words for the sending rules that most often refuse an approved message; any other code shows
# the generic line (codes never show).
GATE_WORDS = {
    "PENDING_INVITATION_PRESENT": "An invitation to this person is already pending.",
    "INVITATION_ALREADY_PENDING": "An invitation to this person is already pending.",
    "ALREADY_CONNECTED": "You are already connected with this person.",
    "OUTREACH_CLOSED": "Outreach to this person is closed.",
    "RECIPIENT_CLOSED": "Outreach to this person is closed.",
    "OPT_OUT": "This person asked not to be contacted.",
    "NEGATIVE_REPLY": "This person replied that they are not interested.",
    "REPLY_STOPS_OUTREACH": "This person replied, so new outreach stops.",
    "EXCLUDED_CONTACT": "This person is on your exclusion list.",
    "VERIFIED_MESSAGE_ALREADY_SENT": "A message to this person was already sent.",
    "OTHER_OUTREACH_ACTION_RESERVED": "Another message to this person is already on its way.",
}
GENERIC_GATE = "A sending safety rule stopped it. Sidedoor tries it again on later rounds while the rule allows."
FORM_NOT_READABLE_WORDS = ("Sidedoor cannot read this application form. Make it readable (sign in to the employer's "
                           "application site in Sidedoor's browser, or check the form loads there), then answer with "
                           "a short note and Sidedoor reads the form again.")
BUSY = "Sidedoor is busy with another task. Try again in a moment."


def _json(text):
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _object(text):
    value = _json(text)
    return value if isinstance(value, dict) else None


def _has(database, table):
    return database is not None and database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def dom(kind, key):
    return kind + "-" + hashlib.sha1(str(key).encode()).hexdigest()[:16]


def linkedin_url(profile):
    """An https LinkedIn profile link for a stored profile key, or None (nothing else becomes a link)."""
    if not isinstance(profile, str) or not profile.strip():
        return None
    text = profile.strip()
    if text.startswith("http://"):
        text = text[len("http://"):]
    if not text.startswith("https://"):
        text = "https://" + text.lstrip("/")
    host = text[len("https://"):].split("/", 1)[0].lower()
    return text if host == "linkedin.com" or host.endswith(".linkedin.com") else None


def _value(record):
    value = record.get("value") if isinstance(record, dict) else record
    return value.strip() if isinstance(value, str) and value.strip() else None


def research_reason(database, action):
    """(title, why) from the research draft behind a research note; (None, None) when there is none."""
    source = action.get("source") if isinstance(action.get("source"), dict) else {}
    if source.get("kind") != approvals.RESEARCH_SOURCE or not _has(database, "rw_drafts"):
        return None, None
    row = database.execute("SELECT payload FROM rw_drafts WHERE identity=? AND version=?",
                           (source.get("identity"), source.get("version"))).fetchone()
    draft = _object(row[0]) if row else None
    if draft is None:
        return None, None
    evidence = draft.get("person_evidence") if isinstance(draft.get("person_evidence"), dict) else {}
    why = [text for text in (_value(evidence.get("ability_to_help")), _value(evidence.get("team")),
                             _value(draft.get("team_need_evidence"))) if text]
    return _value(evidence.get("employment")), why


def judge_verdict(database, action_id, evidence, held_decision=None):
    """The send judge's verdict and reason for this hold: the DECISION row (read by action id, the
    audit table's index) whose observation the hold's evidence names."""
    verdict, reason = held_decision, None
    if isinstance(evidence, str) and evidence.startswith("outreach_agent_audit:") and _has(database, "outreach_agent_audit"):
        reference = evidence.split(":", 1)[1]
        for (record,) in database.execute("SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION' "
                                          "ORDER BY observed_at DESC LIMIT ?", (action_id, AUDIT_SCAN)):
            record = _object(record) or {}
            decision = record.get("decision")
            if record.get("observation_id") == reference and isinstance(decision, dict):
                verdict = decision.get("decision") or verdict
                reason = decision.get("reason") if isinstance(decision.get("reason"), str) else None
                break
    words = VERDICT_WORDS.get(str(verdict or "").upper()) if verdict else None
    return {"verdict": words or "Not on record", "reason": reason}


def conversation(database, action):
    """The conversation so far for a reply: the person's recorded messages, oldest first, at most nine."""
    people = [person for person in (action.get("profile"), action.get("resolved_profile")) if isinstance(person, str)]
    # engagement_events store a person under the profile key ("linkedin.com/in/x"); a reply action
    # carries the inbox's profile URL, so both spellings are read (else the thread never showed).
    import orchestrator
    for person in list(people):
        try:
            people.append(orchestrator.profile_key(person))
        except ValueError:
            pass
    people = list(dict.fromkeys(people))
    if not people or not _has(database, "engagement_events"):
        return []
    rows = database.execute("SELECT kind,occurred_at,payload FROM engagement_events WHERE identity IN (%s) AND kind IN (%s) "
                            "ORDER BY occurred_at DESC LIMIT ?" % (",".join("?" * len(people)), ",".join("?" * len(CONVERSATION_KINDS))),
                            (*people, *CONVERSATION_KINDS, CONVERSATION_LIMIT)).fetchall()
    found = []
    for kind, occurred_at, payload in reversed(rows):
        value = _object(payload) or {}
        text = next((value.get(key) for key in ("text", "message", "body") if isinstance(value.get(key), str)), None) \
            or _event_text(value)
        found.append({"who": "Them" if kind == "inbound" else "You", "at": overview_page._utc(occurred_at),
                      "text": text or "(message text not on record)"})
    return found


def _event_text(payload):
    import networking_page   # where the inbox reader and the decoder put a message's text (inbox.text, raw...)
    return networking_page._message_text(payload, None)


def card_of(database, item):
    row = database.execute("SELECT identity,payload,result FROM outreach_actions WHERE action_id=?", (item["action_id"],)).fetchone() \
        if _has(database, "outreach_actions") and isinstance(item["action_id"], str) else None
    action = (_object(row[1]) if row else None) or {}
    held = ((_object(row[2]) or {}).get("awaiting_approval") if row else None)
    held = held if isinstance(held, dict) else {}
    title, why = research_reason(database, action)
    text = item["message"] if isinstance(item["message"], str) else ""
    identity = row[0] if row else None
    edited = any(record["kind"] == approvals.REVISION and record.get("edited_payload_hash") == item["payload_hash"]
                 for record in approvals.decisions(database, item["action_id"], identity)) if identity else False
    return {"dom": dom("message", item["action_id"]), "action_id": item["action_id"], "payload_hash": item["payload_hash"],
            "stage": item["stage"], "revisable": bool(item["revisable"]) and item["stage"] == "MESSAGE",
            "kind": "Invitation in a prepared campaign" if item["stage"] == "LAUNCH" else PURPOSE_WORDS.get(item["purpose"], "Message"),
            "recipient": item["recipient"] or "Someone", "company": item["company"], "title": title,
            "role": _role_line(title, item["company"]),
            "linkedin": linkedin_url(item["profile"]), "why": why, "text": text, "length": len(text), "limit": item["limit"],
            "judge": judge_verdict(database, item["action_id"], item["evidence"] if item["stage"] == "MESSAGE" else None,
                                   held.get("agent_decision")),
            "conversation": conversation(database, action) if item["purpose"] == "REPLY" else [],
            "edited": edited, "held_at": overview_page._utc(item["held_at"]) if item["held_at"] else None}


def _role_line(title, company):
    import networking_page   # "Title at Company", the Networking page's rule (owner, 2026-10-24)
    return networking_page.role_line(title, company)


def messages(database, page=1, only=None):
    """(cards, total, page, unreadable): one page of waiting messages (or just `only`)."""
    items, unreadable = approvals.waiting_read(database)
    if only is not None:
        return [card_of(database, item) for item in items if item["action_id"] == only], 0, 1, unreadable
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(1, page), pages)
    return [card_of(database, item) for item in items[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]], len(items), page, unreadable


def pages_of(params):
    """{list: page number} from a request's query (a missing or bad number is page 1)."""
    found = {}
    for name, param in PAGE_PARAMS.items():
        try:
            found[name] = max(1, int(params.get(param) or 1))
        except (TypeError, ValueError):
            found[name] = 1
    return found


def query_of(pages, **change):
    """The query string for these pages with `change` applied ({list: number}); page 1 is left out."""
    from urllib.parse import urlencode
    merged = dict(pages or {}, **change)
    return urlencode([(PAGE_PARAMS[name], merged[name]) for name in PAGE_PARAMS if merged.get(name, 1) > 1])


def _clamp(page, total, size):
    pages = max(1, -(-total // size))
    return min(max(1, page), pages), pages


def refused(database, only=None, page=1):
    found = []
    offset = 0 if only is not None else (page - 1) * REFUSED_LIMIT
    for item in approvals.refused_after_approval(database, REFUSED_LIMIT if only is None else 500, offset):
        if only is not None and item["action_id"] != only:
            continue
        reasons = list(dict.fromkeys(GATE_WORDS.get(gate, GENERIC_GATE) for gate in item["gates"])) or [GENERIC_GATE]
        found.append(dict(item, dom=dom("refused", item["action_id"]), reasons=reasons, role=_role_line(None, item["company"]),
                          kind=PURPOSE_WORDS.get(item["purpose"], "Message"), linkedin=linkedin_url(item["profile"]),
                          approved=overview_page._utc(item["approved_at"]) if item["approved_at"] else None))
    return found


def _words(code):
    return str(code or "").replace("_", " ").strip().capitalize() or "Answer"


def question_words(label, semantic, field):
    """The question as the form asks it (its label, owner 2026-10-24), else its meaning in words."""
    return " ".join(label.split()) if isinstance(label, str) and label.strip() else _words(semantic or field)


def _iso_date(value):
    """True for an empty answer or a YYYY-MM-DD date, the only values a date picker can show."""
    if value == "":
        return True
    try:
        return isinstance(value, str) and len(value) == 10 and bool(dt.date.fromisoformat(value))
    except ValueError:
        return False


def answer_control(answer_type, options, question, operational=False):
    """The Questions card's control: the one mapping the held-application card uses too
    (`approvals.application_control`), from the stored answer type, the form's options and the
    question as asked. The unreadable-form note is always a box of several lines."""
    import approvals
    return approvals.application_control({"answer_type": answer_type, "choices": options, "label": question,
                                          "operational": operational})[0]


def question_count(database, now):
    if not _has(database, "user_decisions"):
        return 0
    columns = {row[1] for row in database.execute("PRAGMA table_info(user_decisions)")}
    test = " AND kind!='test'" if "kind" in columns else ""
    return database.execute("SELECT COUNT(*) FROM user_decisions WHERE state='PENDING' AND expires_at>?" + test,
                            (int(now.timestamp() * 1000),)).fetchone()[0]


def questions(database, now, only=None, page=1):
    if not _has(database, "user_decisions"):
        return []
    columns = {row[1] for row in database.execute("PRAGMA table_info(user_decisions)")}
    test = " AND d.kind!='test'" if "kind" in columns else ""
    offset = 0 if only is not None else (page - 1) * QUESTION_LIMIT
    jobs = _has(database, "application_jobs")
    label = ",d.label" if "label" in columns else ",NULL"
    label += "".join(",d." + name if name in columns else ",NULL" for name in ("required", "hint", "site_reason"))
    rows = database.execute(
        "SELECT d.id,d.job_key,d.field,d.semantic,d.answer_type,d.choices,d.payload_hash,d.expires_at"
        + (",j.employer,j.payload" if jobs else ",NULL,NULL") + label
        + " FROM user_decisions d" + (" LEFT JOIN application_jobs j ON j.job_key=d.job_key" if jobs else "")
        + " WHERE d.state='PENDING' AND d.expires_at>?" + test + (" AND d.id=?" if only is not None else "")
        + " ORDER BY d.expires_at,d.id LIMIT ? OFFSET ?",
        (int(now.timestamp() * 1000),) + ((only,) if only is not None else ()) + (QUESTION_LIMIT, offset)).fetchall()
    import decision_transport
    found = []
    for (identifier, job_key, field, semantic, answer_type, choices, payload_hash, expires_at, employer, payload, label,
         required, hint, site_reason) in rows:
        role = _object(payload) or {}
        operational = field == decision_transport.FORM_NOT_READABLE_FIELD
        choices = _json(choices)
        try:
            expires = dt.datetime.fromtimestamp(int(expires_at) / 1000, dt.timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            expires = None
        company, role_name = job_names(employer, role) if employer or role.get("company") else ("An employer", None)
        question = FORM_NOT_READABLE_WORDS if operational else question_words(label, semantic, field)
        kind = "text" if operational else answer_type
        options = ["Yes", "No"] if kind == "boolean" else [choice for choice in choices if isinstance(choice, str)] \
            if kind in ("choice", "multi_choice") and isinstance(choices, list) else []
        found.append({"dom": dom("question", identifier), "id": identifier, "payload_hash": payload_hash, "job_key": job_key,
                      "employer": company, "role": role_name or role.get("role") or role.get("title"),
                      "control": answer_control(kind, options, question, operational), "options": options,
                      "url": role.get("url") if str(role.get("url") or "").startswith("https://") else None,
                      "question": question, "answer_type": kind,
                      "choices": [choice for choice in choices if isinstance(choice, str)] if isinstance(choices, list) else [],
                      "required": bool(required) and not operational, "hint": None if operational else hint,
                      "site_reason": None if operational else site_reason,
                      "expires": expires})
    return found


def person_name(database, profile):
    """The person's name when one is on record for this profile: an outreach action's recipient, else
    the research draft's (E6.T4 review 14)."""
    if not isinstance(profile, str) or not profile.strip():
        return None
    import orchestrator   # the identity key outreach_actions and rw_drafts are stored under
    try:
        identities = list(dict.fromkeys((orchestrator.profile_key(profile), profile)))
    except ValueError:
        identities = [profile]
    marks = ",".join("?" * len(identities))
    for table, sql in (("outreach_actions", "SELECT payload FROM outreach_actions WHERE identity IN (%s) LIMIT 5" % marks),
                       ("rw_drafts", "SELECT payload FROM rw_drafts WHERE identity IN (%s) ORDER BY version DESC LIMIT 1" % marks)):
        if _has(database, table):
            for (payload,) in database.execute(sql, identities):
                name = (_object(payload) or {}).get("recipient")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return None


def notes(database, only=None, page=1):
    import reply_tools
    found = [note for note in reply_tools.open_flags(database, None if only is not None else NOTE_LIMIT,
                                                     0 if only is not None else (page - 1) * NOTE_LIMIT)
             if only is None or str(note["note_id"]) == str(only)]
    tasks = _has(database, "workflow_tasks")
    for note in found:
        note["dom"] = dom("note", note["note_id"])
        note["at"] = overview_page._utc(note["flagged_at"])
        contact = None
        if tasks:
            row = database.execute("SELECT contact_id FROM workflow_tasks WHERE task_id=?", (note["task_id"],)).fetchone()
            contact = row[0] if row and row[0] else None
        note["linkedin"] = linkedin_url(contact)
        note["contact"] = person_name(database, contact) or ("this person" if note["linkedin"] else contact)
    return found


# A notice the Dashboard transport delivered (its delivery record names the "dashboard" provider).
DASHBOARD_NOTICE = ("state='DELIVERED' AND json_valid(delivery) AND json_type(delivery)='object' "
                    "AND json_extract(delivery,'$.provider')='dashboard'")


def notice_count(database):
    if not _has(database, "notification_outbox"):
        return 0
    return database.execute("SELECT COUNT(*) FROM notification_outbox WHERE " + DASHBOARD_NOTICE).fetchone()[0]


def notices(database, page=1):
    if not _has(database, "notification_outbox"):
        return []
    rows = database.execute("SELECT event_key,message,updated_at FROM notification_outbox WHERE " + DASHBOARD_NOTICE
                            + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", (NOTICE_LIMIT, (page - 1) * NOTICE_LIMIT)).fetchall()
    return [notice_view(database, event_key, message, overview_page._utc(updated_at)) for event_key, message, updated_at in rows]


# --- Recent notices in plain words (owner, 2026-10-24, A1) ---------------------------------------
# Display only: the stored message (what Telegram or the relay got) is never changed. Each notice is
# read by its event key's shape, the keys the writers use: "<job key>:submitted" and
# "<job key>:<CODE>" (applications.notify), "<job key>:HUMAN_CHECK:<request>" (application_browser),
# "daily-report:<day>[:<part>]" (reporting), "settled-page-confirmations:<time>" (applications),
# "reply:<task>:..." (reply_worker) and "decision:<id>" (decision_transport). The job is looked up in
# application_jobs by the key's own prefixes (a job key holds colons: "<employer>:unresolved:<hash>").
# Any other shape shows its message cleaned of ids and timestamps and shortened.
NOTICE_CHARACTERS = 180
APPLICATIONS = "/applications"
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b(?:unresolved:|url-identity:)?[0-9a-f]{16,}\b")
_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def _job_of(database, event_key):
    """(job key, company, role) of the longest application_jobs key that prefixes `event_key` with a
    colon after it, or None."""
    if not _has(database, "application_jobs"):
        return None
    parts = event_key.split(":")
    candidates = [":".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)]
    if not candidates:
        return None
    rows = database.execute("SELECT job_key,employer,payload FROM application_jobs WHERE job_key IN (%s)"
                            % ",".join("?" * len(candidates)), candidates).fetchall()
    if not rows:
        return None
    key, employer, payload = max(rows, key=lambda row: len(row[0]))
    return (key,) + job_names(employer, _object(payload) or {})


def _capitalized(name):
    return " ".join(word[:1].upper() + word[1:] for word in name.split()) if name == name.lower() else name


def job_names(employer, payload):
    """(company, role) as the Applications page shows them: the role ingest recorded, else the employer
    key capitalized; the role may be None."""
    def text(value):
        return value.strip() if isinstance(value, str) and value.strip() else None
    company = text(payload.get("company")) or (_capitalized(employer.strip()) if text(employer) else None)
    return company or "A company", text(payload.get("role")) or text(payload.get("title"))


def _job_href(key):
    return APPLICATIONS + "?" + urllib.parse.urlencode({"job": key})


def _code_words(code):
    import applications_page
    import failures_page
    return failures_page.cause_words(code) or applications_page.code_words(code)


def cleaned(text):
    """A message with links, timestamps, ids and codes taken out, at most NOTICE_CHARACTERS long."""
    text = " ".join(str(text or "").split())
    for pattern in (_URL, _ISO, _UUID, _HEX):   # with the little word that pointed at it ("at <time>")
        text = re.compile(r"(?:\b(?:at|on|for|since|until|by|from)\s+)?(?:" + pattern.pattern + ")").sub("", text)
    text = _CODE.sub(lambda match: match.group(0).replace("_", " ").lower(), text)
    text = re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s{2,}", " ", text)).strip(" /:;,")
    text = re.sub(r"(?:[/:]\s*){2,}", ": ", text)
    if len(text) > NOTICE_CHARACTERS:
        text = text[:NOTICE_CHARACTERS - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"
    return text or "A notice from Sidedoor."


def _daily(database, day):
    """The daily summary in one line, from its saved report (daily_reports.payload), else just its day."""
    try:
        label = dt.date.fromisoformat(day)
        label = "%s %d" % (label.strftime("%b"), label.day)
    except ValueError:
        label = None
    head = "Daily summary" + (" for " + label if label else "")
    row = database.execute("SELECT payload FROM daily_reports WHERE day=?", (day,)).fetchone() \
        if _has(database, "daily_reports") else None
    data = ((_object(row[0]) or {}).get("outcomes_24h") if row else None) or {}
    if not isinstance(data, dict) or not data:
        return head + "."
    def count(value):
        return len(value) if isinstance(value, list) else sum(value.values()) if isinstance(value, dict) else 0
    submitted = sum(1 for item in data.get("submitted") or [] if isinstance(item, dict) and not item.get("submitted_at"))
    parts = [(submitted, "application submitted", "applications submitted"),
             (count(data.get("discovered")), "new job found", "new jobs found"),
             (count(data.get("invitations")), "invitation sent", "invitations sent"),
             (count(data.get("inbound")), "message received", "messages received")]
    return head + ": " + ", ".join("%d %s" % (number, one if number == 1 else many) for number, one, many in parts) + "."


def notice_view(database, event_key, message, at):
    """{"text", "href", "link", "at"}: one Recent notice in plain words."""
    key = str(event_key or "")
    found = {"at": at, "href": None, "link": None}
    if key.startswith("daily-report:"):
        day = key.split(":")[1] if key.count(":") >= 1 else ""
        return dict(found, text=_daily(database, day), href="/", link="See Overview")
    if key.startswith("settled-page-confirmations:"):
        return dict(found, text="Applications were confirmed by their own confirmation pages.",
                    href=APPLICATIONS + "?show=submitted", link="See submitted applications")
    if key.startswith("reply:"):
        return dict(found, text="A reply got stuck, so no reply was sent.", href="/sent", link="See Networking")
    if key.startswith("decision:"):
        row = database.execute("SELECT job_key FROM user_decisions WHERE id=?", (key.split(":", 1)[1],)).fetchone() \
            if _has(database, "user_decisions") else None
        job = _job_of(database, row[0] + ":") if row and row[0] else None
        what = " about " + _named(job) if job else ""
        return dict(found, text="Sidedoor asked you a question" + what + ".", href="/approvals#questions", link="See questions")
    job = _job_of(database, key)
    if job is None:
        if ":HUMAN_CHECK:" in key:
            return dict(found, text="Sidedoor's browser asked for a human check (a CAPTCHA) and waited for you to tick it.")
        return dict(found, text=cleaned(message))
    rest = key[len(job[0]) + 1:]
    if rest == "submitted":
        text = "Application submitted and confirmed."
    elif rest.startswith("HUMAN_CHECK:"):
        text = "Sidedoor's browser asked for a human check (a CAPTCHA) and waited for you to tick it."
    else:
        text = _code_words(rest)
    return dict(found, text=_named(job) + ": " + text, href=_job_href(job[0]), link="See the application")


def application_count(database):
    return len(approvals.held_application_keys(database)) if database is not None else 0


def _held_at(text):
    try:
        moment = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def applications_waiting(database, only=None, page=1):
    """The held applications' cards (`approvals_application.html`), oldest first, a page at a time."""
    keys = approvals.held_application_keys(database) if database is not None else []
    if only is not None:
        keys = [key for key in keys if key == only]
    else:
        keys = keys[(page - 1) * APPLICATION_LIMIT:page * APPLICATION_LIMIT]
    found = []
    for key in keys:
        row = database.execute("SELECT a.snapshot,a.payload_hash,a.held_at,j.employer,j.payload FROM application_approvals a "
                               "JOIN application_jobs j ON j.job_key=a.job_key WHERE a.job_key=?", (key,)).fetchone()
        snapshot = _object(row[0]) if row else None
        if not snapshot or not isinstance(snapshot.get("questions"), list):
            continue   # a malformed card is never listed
        role = _object(row[4]) or {}
        company, role_name = job_names(row[3], role)
        route = "email" if snapshot.get("route") == "email" else "ats"
        answers, cover, subject, body = [], None, snapshot.get("subject"), None
        for item in snapshot["questions"]:
            if not isinstance(item, dict) or not isinstance(item.get("field"), str):
                continue
            name = approvals.input_name(item, route)
            if name == "cover_letter" and cover is None:
                cover = approvals.shown_answer(item)
            elif name == "body" and body is None:
                body = approvals.shown_answer(item)
            elif name == "subject" and route == "email":
                subject = approvals.shown_answer(item)
            else:
                control, options = approvals.application_control(item)
                if control == "date" and not _iso_date(approvals.shown_answer(item)):
                    control = "text"   # a date picker holds only YYYY-MM-DD: any other stored answer is kept as written
                answers.append({"field": item["field"], "question": question_words(item.get("label"), item.get("semantic"), item["field"]),
                                "control": control, "options": options, "answer": approvals.shown_answer(item)})
        destination = snapshot.get("destination") if isinstance(snapshot.get("destination"), str) else ""
        parts = urllib.parse.urlsplit(destination) if route == "ats" else None
        found.append({"dom": dom("application", key), "id": key, "payload_hash": row[1], "company": company,
                      "role": role_name or "Role not recorded",
                      "url": role.get("url") if str(role.get("url") or "").startswith("https://") else None,
                      "route": route, "site": parts.hostname if parts and parts.hostname else destination,
                      "destination": ((parts.hostname or "") + parts.path.rstrip("/")) if parts else destination,
                      "resume": snapshot.get("resume") or "Your résumé", "answers": answers, "cover_letter": cover,
                      "subject": subject, "body": body, "held_at": _held_at(row[2])})
    return found


def _named(job):
    return job[1] + (" (" + job[2] + ")" if job[2] else "")


def _when(config):
    now = dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    return lambda moment: overview_page.when(moment, now, zone) if moment else None


def gather(database, config, page=1, pages=None):
    """Everything the page lists, one page of each list (`pages`: {list: number}, `pages_of`; `page`
    is the messages' page). Each list has `paging`: {page, pages, total} counted by its own COUNT, so
    "Page X of Y (N)" is the real total. `database` is a read-only connection or None (no file yet)."""
    now = dt.datetime.now(dt.timezone.utc)
    pages = dict(pages or {}, messages=(pages or {}).get("messages", page))
    paging = {}
    if database is None:
        found = {"messages": [], "total": 0, "page": 1, "unreadable": 0, "applications": [], "refused": [], "questions": [],
                 "notes": [], "notices": []}
        paging = {name: {"page": 1, "pages": 1, "total": 0} for name in PAGE_PARAMS}
    else:
        import reply_tools
        cards, total, number, unreadable = messages(database, pages["messages"])
        found = {"messages": cards, "total": total, "page": number, "unreadable": unreadable}
        paging["messages"] = {"page": number, "pages": max(1, -(-total // PAGE_SIZE)), "total": total}
        for name, count, load in (
                ("applications", application_count, lambda number: applications_waiting(database, page=number)),
                ("refused", approvals.refused_count, lambda number: refused(database, page=number)),
                ("questions", lambda database: question_count(database, now), lambda number: questions(database, now, page=number)),
                ("notes", reply_tools.open_flag_count, lambda number: notes(database, page=number)),
                ("notices", notice_count, lambda number: notices(database, page=number))):
            total = count(database)
            number, last = _clamp(pages.get(name, 1), total, LIST_SIZES[name])
            paging[name] = {"page": number, "pages": last, "total": total}
            found[name] = load(number)
    found["pages"] = paging["messages"]["pages"]
    found["paging"] = paging
    current = {name: paging[name]["page"] for name in PAGE_PARAMS}
    found["query"] = query_of(current)
    found["link"] = lambda name, number: query_of(current, **{name: number})
    found["when"] = _when(config)
    return found


def policy_for(home):
    """The policy as the dashboard's home holds it (profile.json and settings.json), or None when the
    profile is missing (E6.T4 review 16: the dashboard's home, never SIDEDOOR_HOME; never a 500)."""
    import setup_steps
    import user_profile
    profile = setup_steps.read_profile(home)
    if not profile:
        return None
    settings = json.loads(user_profile.DEFAULT_SETTINGS.read_text())
    override = Path(home if home is not None else user_profile.home()) / "settings.json"
    try:
        if override.is_file():
            settings.update(json.loads(override.read_text()))
    except (OSError, ValueError):
        pass
    return user_profile.merge(profile, settings)


# --- Routes -----------------------------------------------------------------------------------------

def register(app, templates, layout, *, home, root, config):
    import dashboard

    def read(use):
        return overview_page.snapshot(paths.database(root), use, lambda: dashboard.read_connection(root))

    def view(request, flash=None, page=None):
        context = layout("approvals", "Approvals")
        # Every list's page from the query (a card's form posts back to the page it was on, so a plain
        # post without JavaScript keeps the pages too).
        pages = pages_of(request.query_params)
        if page is not None:
            pages["messages"] = page
        context["view"] = read(lambda database: gather(database, config, pages=pages))
        context.update(review=approvals.install_mode(root, home) == approvals.REVIEW, flash=flash or {})
        return templates.TemplateResponse(request, "approvals.html", context)

    def answer_card(request, kind, key, finder, template, flash, gone):
        """One card's action answered: with htmx that card alone, as it now stands and carrying
        `flash`, or a closing note (`gone`) in its place once it is no longer listed; without htmx the
        whole page, with the flash on the card or at the top."""
        found = read(lambda database: finder(database) if database is not None else [])
        if request.headers.get("hx-request") == "true":
            if found:
                return HTMLResponse(templates.get_template(template).render(
                    item=found[0], f=flash, v={"when": _when(config), "query": query_of(pages_of(request.query_params))}))
            return HTMLResponse(templates.get_template("approvals_done.html").render(dom=dom(kind, key), f=gone))
        return view(request, {"cards": {found[0]["dom"]: flash}} if found else {"top": gone})

    def write(change):
        """`change(database)` on a write connection; None when the database stayed locked."""
        paths.state(root).mkdir(exist_ok=True, mode=0o700)
        try:
            database = dashboard.write_connection(root)
            try:
                return change(database)
            finally:
                database.close()
        except sqlite3.OperationalError as error:
            if "locked" in str(error) or "busy" in str(error):
                return None
            raise

    @app.get("/approvals", name="approvals")
    def approvals_page(request: Request):
        return view(request)

    # --- Messages -----------------------------------------------------------------------------------

    async def decide(request, act):
        form = await request.form()
        action_id, shown = form.get("action_id") or "", form.get("payload_hash") or ""
        text = form.get("text") if isinstance(form.get("text"), str) else None
        reason = form.get("reason") if isinstance(form.get("reason"), str) else None
        recipient = form.get("recipient") or "this person"
        result = await run_in_threadpool(write, lambda database: act(database, action_id, shown, text, reason)) if shown else \
            {"state": "REFUSED", "message": "This page is out of date. Reload it and try again."}
        state = (result or {}).get("state")
        finder = lambda database: messages(database, only=action_id)[0]

        def reply(flash, gone):
            return answer_card(request, "message", action_id, finder, "approvals_message.html", flash, gone)
        if result is None:
            kept = " Your text is still in the box." if text is not None else (" Your reason is still in the box." if reason else "")
            return reply({"tone": "error", "text": BUSY + kept, "kept": text, "kept_reason": reason},
                         {"tone": "error", "text": BUSY, "kept": text})
        done = {"APPROVED": "Approved: the message to %s goes out on the next round, through the usual sending checks "
                            "(one message per person, no repeat contact, stops and exclusions)." % recipient,
                "REVISED": "Saved your edit and approved it. The message to %s goes out on the next round, through the "
                           "usual sending checks." % recipient,
                "REJECTED": "Rejected. The message to %s will not be sent. A later research run may draft this person "
                            "again." % recipient}
        if state in done:
            return reply({}, {"tone": "success", "text": done[state]})
        if state == "CHANGED":
            shown_text = form.get("shown_text")
            if text is not None and isinstance(shown_text, str) and _lf(text) == _lf(shown_text):
                text = None   # the box was not edited: it comes back with the new text, not as "Your edit" (A2)
            return reply({"tone": "warning", "kept": text, "changed": True, "kept_reason": reason,
                          "text": "This message changed; review the new text."
                                  + (" Your edit is in the box: approving sends exactly what the box says." if text is not None else "")},
                         {"tone": "warning", "kept": text, "text": "This message changed since it was shown and is no longer "
                          "waiting for you. Nothing was sent or decided by this click."})
        message = (result or {}).get("message") or "This could not be done. Reload the page and try again."
        return reply({"tone": "error", "text": message, "kept": text, "kept_reason": reason},
                     {"tone": "error", "text": message, "kept": text})

    @app.post("/approvals/messages/send")
    async def send(request: Request):
        """The revisable card's one button: the box's text is what is approved (unchanged text as written)."""
        return await decide(request, lambda database, action_id, shown, text, reason:
                            approvals.revise_and_approve(database, action_id, text or "", shown_payload_hash=shown))

    @app.post("/approvals/messages/approve")
    async def approve(request: Request):
        """A launch card's Approve (its text cannot be edited)."""
        return await decide(request, lambda database, action_id, shown, text, reason:
                            approvals.approve(database, action_id, shown_payload_hash=shown))

    @app.post("/approvals/messages/reject")
    async def reject(request: Request):
        return await decide(request, lambda database, action_id, shown, text, reason:
                            approvals.reject(database, action_id, shown_payload_hash=shown, reason=reason))

    @app.post("/approvals/refused/dismiss")
    async def dismiss(request: Request):
        form = await request.form()
        action_id, shown = form.get("action_id") or "", form.get("payload_hash") or ""
        result = await run_in_threadpool(write, lambda database: approvals.dismiss_refused(database, action_id, shown))
        finder = lambda database: refused(database, only=action_id)
        if result is None:
            return answer_card(request, "refused", action_id, finder, "approvals_refused.html", {"tone": "error", "text": BUSY},
                               {"tone": "error", "text": BUSY})
        return answer_card(request, "refused", action_id, finder, "approvals_refused.html", {},
                           {"tone": "success", "text": "Dismissed. The message stays unsent."})

    # --- Applications -------------------------------------------------------------------------------

    async def decide_application(request, act):
        form = await request.form()
        job_key, shown = form.get("application_id") or "", form.get("payload_hash") or ""
        posted = {name: [value for value in form.getlist(name) if isinstance(value, str)] for name in form.keys()}
        result = await run_in_threadpool(write, lambda database: act(database, job_key, shown, posted)) if shown else \
            {"state": "REFUSED", "message": "This page is out of date. Reload it and try again."}
        finder = lambda database: applications_waiting(database, only=job_key)

        def reply(flash, gone):
            return answer_card(request, "application", job_key, finder, "approvals_application.html", flash, gone)
        if result is None:
            return reply({"tone": "error", "text": BUSY}, {"tone": "error", "text": BUSY})
        state, email = result.get("state"), result.get("route") == "email"
        if state == "APPROVED":
            # The next applications pass submits it; the scheduler starts one now rather than at its
            # next hourly turn (a run already going takes it after its current list).
            import scheduler_loop
            scheduler_loop.run_soon("applications")
            return reply({}, {"tone": "success", "text": ("Approved. Sidedoor sends this email application on its next "
                                                          "applications run, which starts now." if email else
                                                          "Approved. Sidedoor submits this application with these answers "
                                                          "on its next applications run, which starts now.")})
        if state == "REJECTED":
            return reply({}, {"tone": "success", "text": "Rejected. This application will never be "
                                                         + ("sent." if email else "submitted.")})
        message = result.get("message") or "This could not be done. Reload the page and try again."
        return reply({"tone": "warning" if state == "CHANGED" else "error", "text": message},
                     {"tone": "warning", "text": message})

    @app.post("/approvals/applications/approve")
    async def approve_application(request: Request):
        """Approve (my edits) and submit/send: exactly the answers on the card are saved and submitted."""
        return await decide_application(request, lambda database, job_key, shown, posted:
                                        approvals.approve_application(database, job_key, posted, shown_payload_hash=shown))

    @app.post("/approvals/applications/reject")
    async def reject_application(request: Request):
        return await decide_application(request, lambda database, job_key, shown, posted:
                                        approvals.reject_application(database, job_key, shown_payload_hash=shown,
                                                                     reason=(posted.get("reason") or [None])[0]))

    # --- Questions ----------------------------------------------------------------------------------

    @app.post("/approvals/questions/answer")
    async def answer(request: Request):
        form = await request.form()
        decision_id, shown = form.get("decision_id") or "", form.get("payload_hash") or ""
        raw = form.get("answer")
        raw = raw.replace("\r\n", "\n").strip() if isinstance(raw, str) else ""
        ticked = [value for value in form.getlist("answer_multi") if isinstance(value, str) and value]
        if ticked:
            raw = json.dumps(ticked)   # a multi-select's ticked options, as the list it is stored as
        finder = lambda database: questions(database, dt.datetime.now(dt.timezone.utc), only=decision_id)

        def reply(flash, gone):
            return answer_card(request, "question", decision_id, finder, "approvals_question.html", flash, gone)
        if not raw:
            return reply({"tone": "error", "text": "Write or choose an answer first."},
                         {"tone": "error", "text": "Write or choose an answer first."})
        policy = await run_in_threadpool(policy_for, home)

        def act(database):
            import decision_transport
            if policy is None:
                return {"state": "REFUSED", "message": decision_transport.DASHBOARD_REFUSALS["PROFILE_MISSING"]}, None
            result = decision_transport.answer_locally(database, decision_id, raw, shown_payload_hash=shown, policy=policy)
            # A database from before the label column (Q3, review round 1): NULL, never an error after the
            # answer was recorded.
            columns = {column[1] for column in database.execute("PRAGMA table_info(user_decisions)")}
            row = database.execute("SELECT d.field,d.semantic,d.answer_type,j.employer,j.payload,d.choices,"
                                   + ("d.label" if "label" in columns else "NULL") + " FROM user_decisions d "
                                   "LEFT JOIN application_jobs j ON j.job_key=d.job_key WHERE d.id=?", (decision_id,)).fetchone()
            return result, row
        outcome = await run_in_threadpool(write, act)
        if outcome is None:
            return reply({"tone": "error", "text": BUSY + " Your answer is still in the box.", "kept": raw},
                         {"tone": "error", "text": BUSY, "kept": raw})
        result, row = outcome
        if result["state"] != "APPLIED":
            # A refusal (expired, answered, changed, stale) or an answer that does not fit: the reason in
            # plain words, the typed answer kept (E6.T4 review 15).
            return reply({"tone": "warning", "text": result["message"], "kept": raw, "kept_list": ticked},
                         {"tone": "warning", "text": result["message"], "kept": raw, "kept_list": ticked})
        import decision_transport
        import setup_steps
        field, semantic, answer_type, employer, payload, row_choices, label = row
        text = "Answer saved. The application goes on at its next turn."
        if field != decision_transport.FORM_NOT_READABLE_FIELD:
            role = _object(payload) or {}
            value = (raw.casefold() in {"yes", "true"} if answer_type == "boolean"
                     else json.loads(raw) if answer_type == "multi_choice" else raw)
            title = role.get("role") or role.get("title")
            asked = question_words(label, semantic, field)
            # The company as the posting writes it, as Applications shows it (owner, 2026-10-24), then
            # the stored employer key.
            company = (role.get("company") if isinstance(role.get("company"), str) and role.get("company").strip()
                       else None) or employer or "a job"
            context = "Your answer to \"%s\" for %s%s" % (asked, company, ", " + str(title) if title else "")
            try:
                choices = _json(row_choices) if row_choices else None
                choices = [choice for choice in choices if isinstance(choice, str)] if isinstance(choices, list) else []
                yes_no = len(choices) == 2 and {choice.strip().casefold() for choice in choices} == {"yes", "no"}
                # What was asked, where, and the form's own options, for Settings > Your saved answers
                # (owner, 2026-10-24). A choice answer is the chosen option's own text, so the answering
                # AI can match it to a later form's differently worded options.
                asked_for = {"question": asked, "employer": company, "role": str(title) if title else None,
                             "choices": choices or (["Yes", "No"] if answer_type == "boolean" else [])}
                await run_in_threadpool(setup_steps.save_owner_answer, decision_id, semantic, value, context, home, yes_no,
                                        asked_for)
                text += " It is also kept in Settings > Your saved answers for later applications that ask the same thing."
            except OSError:
                text += " It could not be kept for later applications (your profile file could not be written)."
            else:
                # Other questions this kept answer now settles are closed and their jobs re-evaluated
                # (E6.T4 review 3), with no relay needed.
                fresh = await run_in_threadpool(policy_for, home)
                if fresh is not None:
                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    await run_in_threadpool(write, lambda database: decision_transport.refresh_stale(database, fresh, now))
        return reply({}, {"tone": "success", "text": text})

    # --- Notes --------------------------------------------------------------------------------------

    @app.post("/approvals/notes/done")
    async def note_done(request: Request):
        form = await request.form()
        note_id = form.get("note_id") or ""
        import reply_tools
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        changed = await run_in_threadpool(write, lambda database: reply_tools.mark_done(database, note_id, now))
        finder = lambda database: notes(database, only=note_id)
        if changed is None:
            return answer_card(request, "note", note_id, finder, "approvals_note.html", {"tone": "error", "text": BUSY},
                               {"tone": "error", "text": BUSY})
        return answer_card(request, "note", note_id, finder, "approvals_note.html", {},
                           {"tone": "success", "text": "Marked done." if changed else "That note was already done."})
