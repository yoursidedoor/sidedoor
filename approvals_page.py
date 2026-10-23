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
- Couldn't send: approved messages a sending rule then refused (`approvals.refused_after_approval`;
  owner, 2026-10-22, D3), with the reason in plain words and [Dismiss] (`approvals.dismiss_refused`).
- Questions for you: `user_decisions` PENDING and unexpired (not the transport test), answered through
  `decision_transport.answer_locally` against the dashboard home's policy. An APPLIED answer is also
  kept in profile.json for later jobs (`setup_steps.save_owner_answer`; owner, 2026-10-22, "a"), except
  the "form not readable" note; other questions that answer makes stale are then closed and their
  jobs made due again (`decision_transport.refresh_stale`).
- Notes for you: `reply_tools.open_flags` (by rowid), with the person's name when one is on record.
- Recent notices: `notification_outbox` rows the Dashboard transport delivered.

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
import sqlite3
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
QUESTION_LIMIT = 50
NOTE_LIMIT = 50
REFUSED_LIMIT = 25
NOTICE_LIMIT = 20
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
    if not people or not _has(database, "engagement_events"):
        return []
    rows = database.execute("SELECT kind,occurred_at,payload FROM engagement_events WHERE identity IN (%s) AND kind IN (%s) "
                            "ORDER BY occurred_at DESC LIMIT ?" % (",".join("?" * len(people)), ",".join("?" * len(CONVERSATION_KINDS))),
                            (*people, *CONVERSATION_KINDS, CONVERSATION_LIMIT)).fetchall()
    found = []
    for kind, occurred_at, payload in reversed(rows):
        value = _object(payload) or {}
        text = next((value.get(key) for key in ("text", "message", "body") if isinstance(value.get(key), str)), None)
        found.append({"who": "Them" if kind == "inbound" else "You", "at": overview_page._utc(occurred_at),
                      "text": text or "(message text not on record)"})
    return found


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
            "linkedin": linkedin_url(item["profile"]), "why": why, "text": text, "length": len(text), "limit": item["limit"],
            "judge": judge_verdict(database, item["action_id"], item["evidence"] if item["stage"] == "MESSAGE" else None,
                                   held.get("agent_decision")),
            "conversation": conversation(database, action) if item["purpose"] == "REPLY" else [],
            "edited": edited, "held_at": overview_page._utc(item["held_at"]) if item["held_at"] else None}


def messages(database, page=1, only=None):
    """(cards, total, page, unreadable): one page of waiting messages (or just `only`)."""
    items, unreadable = approvals.waiting_read(database)
    if only is not None:
        return [card_of(database, item) for item in items if item["action_id"] == only], 0, 1, unreadable
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(1, page), pages)
    return [card_of(database, item) for item in items[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]], len(items), page, unreadable


def refused(database, only=None):
    found = []
    for item in approvals.refused_after_approval(database, REFUSED_LIMIT if only is None else 500):
        if only is not None and item["action_id"] != only:
            continue
        reasons = list(dict.fromkeys(GATE_WORDS.get(gate, GENERIC_GATE) for gate in item["gates"])) or [GENERIC_GATE]
        found.append(dict(item, dom=dom("refused", item["action_id"]), reasons=reasons,
                          kind=PURPOSE_WORDS.get(item["purpose"], "Message"), linkedin=linkedin_url(item["profile"]),
                          approved=overview_page._utc(item["approved_at"]) if item["approved_at"] else None))
    return found


def _words(code):
    return str(code or "").replace("_", " ").strip().capitalize() or "Answer"


def questions(database, now, only=None):
    if not _has(database, "user_decisions"):
        return []
    columns = {row[1] for row in database.execute("PRAGMA table_info(user_decisions)")}
    test = " AND d.kind!='test'" if "kind" in columns else ""
    jobs = _has(database, "application_jobs")
    rows = database.execute(
        "SELECT d.id,d.job_key,d.field,d.semantic,d.answer_type,d.choices,d.payload_hash,d.expires_at"
        + (",j.employer,j.payload" if jobs else ",NULL,NULL")
        + " FROM user_decisions d" + (" LEFT JOIN application_jobs j ON j.job_key=d.job_key" if jobs else "")
        + " WHERE d.state='PENDING' AND d.expires_at>?" + test + (" AND d.id=?" if only is not None else "")
        + " ORDER BY d.expires_at,d.id LIMIT ?",
        (int(now.timestamp() * 1000),) + ((only,) if only is not None else ()) + (QUESTION_LIMIT,)).fetchall()
    import decision_transport
    found = []
    for identifier, job_key, field, semantic, answer_type, choices, payload_hash, expires_at, employer, payload in rows:
        role = _object(payload) or {}
        operational = field == decision_transport.FORM_NOT_READABLE_FIELD
        choices = _json(choices)
        try:
            expires = dt.datetime.fromtimestamp(int(expires_at) / 1000, dt.timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            expires = None
        found.append({"dom": dom("question", identifier), "id": identifier, "payload_hash": payload_hash, "job_key": job_key,
                      "employer": employer or role.get("company") or "An employer",
                      "role": role.get("role") or role.get("title"),
                      "url": role.get("url") if str(role.get("url") or "").startswith("https://") else None,
                      "question": FORM_NOT_READABLE_WORDS if operational else _words(semantic or field),
                      "answer_type": "text" if operational else answer_type,
                      "choices": [choice for choice in choices if isinstance(choice, str)] if isinstance(choices, list) else [],
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


def notes(database, only=None):
    import reply_tools
    found = [note for note in reply_tools.open_flags(database, None if only is not None else NOTE_LIMIT)
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


def notices(database):
    if not _has(database, "notification_outbox"):
        return []
    rows = database.execute("SELECT message,updated_at,delivery FROM notification_outbox WHERE state='DELIVERED' "
                            "ORDER BY updated_at DESC LIMIT 500").fetchall()
    found = []
    for message, updated_at, delivery in rows:
        if (_object(delivery) or {}).get("provider") == "dashboard":
            found.append({"text": message, "at": overview_page._utc(updated_at)})
            if len(found) >= NOTICE_LIMIT:
                break
    return found


def _when(config):
    now = dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(config.get("timezone") or "America/Los_Angeles")
    return lambda moment: overview_page.when(moment, now, zone) if moment else None


def gather(database, config, page=1):
    """Everything the page lists. `database` is a read-only connection or None (no file yet)."""
    now = dt.datetime.now(dt.timezone.utc)
    if database is None:
        found = {"messages": [], "total": 0, "page": 1, "unreadable": 0, "refused": [], "questions": [], "notes": [], "notices": []}
    else:
        cards, total, page, unreadable = messages(database, page)
        found = {"messages": cards, "total": total, "page": page, "unreadable": unreadable, "refused": refused(database),
                 "questions": questions(database, now), "notes": notes(database), "notices": notices(database)}
    found["pages"] = max(1, -(-found["total"] // PAGE_SIZE))
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

    def view(request, flash=None, page=1):
        context = layout("approvals", "Approvals")
        context["view"] = read(lambda database: gather(database, config, page=page))
        context.update(review=approvals.install_mode(root, home) == approvals.REVIEW, flash=flash or {})
        return templates.TemplateResponse(request, "approvals.html", context)

    def answer_card(request, kind, key, finder, template, flash, gone):
        """One card's action answered: with htmx that card alone, as it now stands and carrying
        `flash`, or a closing note (`gone`) in its place once it is no longer listed; without htmx the
        whole page, with the flash on the card or at the top."""
        found = read(lambda database: finder(database) if database is not None else [])
        if request.headers.get("hx-request") == "true":
            if found:
                return HTMLResponse(templates.get_template(template).render(item=found[0], f=flash, v={"when": _when(config)}))
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
    def approvals_page(request: Request, page: str = "1"):
        try:
            number = int(page)
        except ValueError:
            number = 1
        return view(request, page=number)

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

    # --- Questions ----------------------------------------------------------------------------------

    @app.post("/approvals/questions/answer")
    async def answer(request: Request):
        form = await request.form()
        decision_id, shown = form.get("decision_id") or "", form.get("payload_hash") or ""
        raw = form.get("answer")
        raw = raw.replace("\r\n", "\n").strip() if isinstance(raw, str) else ""
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
            row = database.execute("SELECT d.field,d.semantic,d.answer_type,j.employer,j.payload,d.choices FROM user_decisions d "
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
            return reply({"tone": "warning", "text": result["message"], "kept": raw},
                         {"tone": "warning", "text": result["message"], "kept": raw})
        import decision_transport
        import setup_steps
        field, semantic, answer_type, employer, payload, row_choices = row
        text = "Answer saved. The application goes on at its next turn."
        if field != decision_transport.FORM_NOT_READABLE_FIELD:
            role = _object(payload) or {}
            value = raw.casefold() in {"yes", "true"} if answer_type == "boolean" else raw
            title = role.get("role") or role.get("title")
            context = "Your answer to \"%s\" for %s%s" % (_words(semantic or field), employer or role.get("company") or "a job",
                                                         ", " + str(title) if title else "")
            try:
                choices = _json(row_choices) if row_choices else None
                yes_no = isinstance(choices, list) and len(choices) == 2 and {
                    str(choice).strip().casefold() for choice in choices} == {"yes", "no"}
                await run_in_threadpool(setup_steps.save_owner_answer, decision_id, semantic, value, context, home, yes_no)
                text += " It is also kept in Settings > Profile and search for later applications that ask the same thing."
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
