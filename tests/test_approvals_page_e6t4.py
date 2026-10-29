"""E6.T4, the Approvals page (E6.T0 spec section 6 and the Review -> Automatic question of section
10), with the back end of E6.T2 (approvals.py). Fakes only: no provider, no model, no scheduler.

Worked examples (the brief's numbering): 1 Review mode with messages waiting (approve, revise to
291/300, refuse 312/300 keeping the edit, a stale hash); 2 a campaign LAUNCH has Approve and Reject
and no edit; 3 a question answered in the dashboard goes through decision_transport like a Telegram
reply and the job is due again, and the answer is kept in profile.json for later jobs (owner,
2026-10-22, "a"); 4 a note marked done leaves the list and Needs you; 5 Settings asks once when
switching Review -> Automatic and [Send them] releases exactly the messages it listed. Then the owner's
decisions D1 (kept answers count for eligibility, newer wins) and D3 (a "couldn't send" card), and
the review fixes (named R<n> after the review's item numbers).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import applications
import approvals
import decision_transport
import notifications
import outreach
import outreach_agent
import overview_page
import reply_tools
import setup_steps
import user_profile

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import approvals_page
    import dashboard

PORT = 47823
SECRET = "test-token-value"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
NOW = dt.datetime(2026, 10, 22, 18, 0, tzinfo=dt.timezone.utc)


def note(length, word="note"):
    text = ("Hi, I would like to connect about the platform " + word + " work. ") * 10
    return text[:length - 1] + "."


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def invite(action_id, person, text):
    return {"action_id": action_id, "profile": "https://www.linkedin.com/in/" + person, "recipient": person.title() + " Example",
            "company": "Acme", "purpose": "INVITE", "channel": "linkedin", "provider": "unipile",
            "provider_id": "provider-" + person, "note": text, "message": text,
            "verification": {"content_verified": True, "content_sources": ["mock"], "identity_verified": True,
                             "identity_source": "mock", "team_relevance_verified": True, "team_source": "mock",
                             "verified_at": NOW.isoformat()}}


class Store:
    """The install's database at <root>/state/orchestrator.sqlite3, as the dashboard opens it."""

    def open_store(self, root):
        (root / "state").mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / approvals.DATABASE)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        approvals.initialize(self.db)
        outreach.initialize(self.db)
        self.db.commit()

    def hold(self, action_id, person, text, reason="Fits the team and the note is specific."):
        action = invite(action_id, person, text)
        outreach.enqueue_action(self.db, action)
        self.db.commit()
        row = dict(self.db.execute("SELECT * FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone())
        reference = outreach_agent.audit(self.db, action_id, "INSPECTION", "OBSERVATION", NOW.isoformat(), {"packet": action_id})
        outreach_agent.audit(self.db, action_id, "INSPECTION", "DECISION", NOW.isoformat(),
                             {"observation_id": reference, "decision": {"decision": "READY", "reason": reason}})
        with self.db:
            self.assertTrue(approvals.mark_held(self.db, row, {"evidence": "outreach_agent_audit:" + reference,
                                                                "agent_decision": "READY"}, NOW))
        return row

    def shown(self, action_id):
        return self.db.execute("SELECT payload_hash FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0]

    def state(self, action_id):
        return self.db.execute("SELECT state FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0]

    def payload(self, action_id):
        return json.loads(self.db.execute("SELECT payload FROM outreach_actions WHERE action_id=?", (action_id,)).fetchone()[0])


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class PageBase(Store, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "home"
        self.root = Path(directory.name) / "root"
        self.home.mkdir()
        self.root.mkdir()
        shutil.copyfile(user_profile.EXAMPLE_PROFILE, self.home / "profile.json")
        environment = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.open_store(self.root)
        self.client = self.client_for()

    def client_for(self):
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config={"text_model": {"model": "test/model"}},
                                   browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        return client

    def review(self):
        approvals.set_install_mode(approvals.REVIEW, root=self.root, home=self.home)

    def page(self, path="/approvals"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return html.unescape(response.text)

    def post(self, path, data, headers=ORIGIN):
        response = self.client.post(path, data=data, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def card(self, page, action_id):
        """The card that carries this action id (its forms' hidden fields)."""
        for card in page.split('<article class="approval-card')[1:]:
            if 'value="%s"' % action_id in card:
                return card.split("</article>")[0]
        return None

    def send(self, action_id, text, shown=None, headers=ORIGIN, recipient=None):
        data = {"action_id": action_id, "payload_hash": shown or self.shown(action_id), "text": text}
        if recipient:
            data["recipient"] = recipient
        return self.post("/approvals/messages/send", data, headers)


class MessagesWaiting(PageBase):
    # Worked example 1
    def test_example_1_approve_revise_refuse_and_stale_hash(self):
        self.review()
        self.hold("invite-a", "alice", note(120))
        self.hold("invite-b", "bob", note(278))
        page = self.page()
        card_a, card_b = self.card(page, "invite-a"), self.card(page, "invite-b")
        for card, action_id, length in ((card_a, "invite-a", 120), (card_b, "invite-b", 278)):
            self.assertIn('name="payload_hash" value="%s"' % self.shown(action_id), card)
            self.assertIn("%d / 300 characters" % length, card)
            self.assertIn("Fits the team and the note is specific.", card)       # the judge's reason
            self.assertIn('href="https://www.linkedin.com/in/', card)
        self.assertIn("Alice Example", card_a)

        # Approve A as written (its box unchanged) -> APPROVED, and it leaves the list.
        page = self.send("invite-a", note(120), recipient="Alice Example")
        self.assertIn("Approved: the message to Alice Example goes out on the next round", page)
        self.assertIsNone(self.card(page, "invite-a"))
        self.assertEqual(self.state("invite-a"), "PENDING")
        self.assertEqual(approvals.decisions(self.db, "invite-a")[-1]["kind"], approvals.APPROVAL)

        # Revise B to 312/300 -> refused, the edit kept and the button off; nothing changed.
        before = self.shown("invite-b")
        too_long = note(312, "infrastructure")
        card = self.card(self.send("invite-b", too_long), "invite-b")
        self.assertIn("Shorten it by 12", card)
        self.assertIn(">" + too_long + "</textarea>", card)
        self.assertRegex(card, r'id="send-[0-9a-z-]+" disabled')
        self.assertIn("312 / 300 characters", card)
        self.assertEqual((self.state("invite-b"), self.shown("invite-b")), (approvals.AWAITING, before))

        # Revise B to 291/300 -> REVISED (approved with the edit, not judged again).
        edited = note(291, "infrastructure")
        self.assertIn("Saved your edit and approved it", self.send("invite-b", edited, before))
        self.assertEqual(self.state("invite-b"), "PENDING")
        self.assertEqual((self.payload("invite-b")["message"], self.payload("invite-b")["note"]), (edited, edited))
        self.assertEqual(approvals.decisions(self.db, "invite-b")[-1]["kind"], approvals.REVISION)

    def rewrite_meanwhile(self, action_id, text):
        changed = dict(self.payload(action_id), message=text, note=text)
        encoded = json.dumps(changed, sort_keys=True)
        with self.db:
            self.db.execute("UPDATE outreach_actions SET payload=?,payload_hash=? WHERE action_id=?", (encoded, sha(encoded), action_id))
        return sha(encoded)

    def test_example_1_stale_hash_shows_the_new_card(self):
        self.review()
        self.hold("invite-c", "carol", note(150))
        stale = self.shown("invite-c")
        new_hash = self.rewrite_meanwhile("invite-c", note(160, "new"))
        card = self.card(self.send("invite-c", note(150), stale), "invite-c")
        self.assertIn("This message changed; review the new text.", card)
        self.assertIn(note(160, "new"), card)
        self.assertIn('name="payload_hash" value="%s"' % new_hash, card)
        self.assertEqual(self.state("invite-c"), approvals.AWAITING)

    def test_a2_an_unedited_box_comes_back_with_the_new_text(self):
        self.review()
        self.hold("invite-c2", "cleo", note(150))
        stale, original = self.shown("invite-c2"), note(150)
        self.rewrite_meanwhile("invite-c2", note(160, "new"))
        card = self.card(self.post("/approvals/messages/send", {"action_id": "invite-c2", "payload_hash": stale,
                                                                "text": original.replace("\n", "\r\n"), "shown_text": original}), "invite-c2")
        self.assertIn("This message changed; review the new text.", card)
        self.assertIn(">" + note(160, "new") + "</textarea>", card)
        self.assertNotIn("Your edit", card)
        self.assertNotIn("The message now reads:", card)
        self.assertIn(">Approve and send</button>", card)

    def test_r1_after_changed_the_current_text_shows_and_only_the_box_is_sent(self):
        self.review()
        self.hold("invite-d", "dana", note(150))
        stale = self.shown("invite-d")
        current = note(160, "rewritten")
        new_hash = self.rewrite_meanwhile("invite-d", current)
        mine = note(140, "mine")
        card = self.card(self.send("invite-d", mine, stale), "invite-d")
        self.assertIn("The message now reads:", card)
        self.assertIn('<blockquote class="approval-text approval-current">' + current + "</blockquote>", card)
        self.assertIn(">" + mine + "</textarea>", card)               # the owner's edit kept, visible
        self.assertIn(">Approve my edit</button>", card)
        self.assertNotIn("/approvals/messages/approve", card)          # no button that sends unseen text
        self.send("invite-d", mine, new_hash)
        self.assertEqual(self.payload("invite-d")["message"], mine)     # what the box said, nothing else

    def test_r2_one_box_feeds_the_action(self):
        self.review()
        self.hold("invite-e", "erin", note(100))
        card = self.card(self.page(), "invite-e")
        self.assertEqual(card.count("<textarea"), 1)
        self.assertNotIn("/approvals/messages/approve", card)
        self.assertIn('data-original="%s"' % note(100), card)
        self.send("invite-e", note(90, "edited"))
        self.assertEqual(self.payload("invite-e")["message"], note(90, "edited"))   # the edit, not the original

    def test_another_tab_decided_first_says_so_and_keeps_the_edit(self):
        self.review()
        self.hold("invite-f", "fern", note(150))
        shown = self.shown("invite-f")
        approvals.revise_and_approve(self.db, "invite-f", note(155, "other"), shown_payload_hash=shown)
        mine = note(140, "mine")
        page = self.send("invite-f", mine, shown)
        self.assertIn("is no longer waiting for you", page)
        self.assertIn(mine + "</textarea>", page)
        self.assertIsNone(self.card(page, "invite-f"))

    def test_reject_with_a_reason_is_never_sent(self):
        self.review()
        self.hold("invite-g", "gil", note(100))
        page = self.post("/approvals/messages/reject", {"action_id": "invite-g", "payload_hash": self.shown("invite-g"),
                                                        "reason": "Wrong team", "recipient": "Gil Example"})
        self.assertIn("will not be sent", page)
        self.assertEqual(self.state("invite-g"), "CANCELLED_BEFORE_SEND")
        self.assertEqual(approvals.decisions(self.db, "invite-g")[-1]["reason"], "Wrong team")

    # Worked example 2
    def test_example_2_launch_item_has_approve_and_reject_and_no_edit(self):
        self.review()
        text = note(200)
        payload = json.dumps({"action_id": "prep-1", "message": text, "purpose": "PREPARE_CAMPAIGN"}, sort_keys=True)
        with self.db:
            self.db.execute("INSERT INTO outreach_actions VALUES ('prep-1','linkedin.com/in/fay','PREPARE_CAMPAIGN',?,?,'PREPARED',NULL,NULL)",
                            (payload, sha(payload)))
            candidate = {"preparation_action_id": "prep-1", "preparation_payload_hash": sha(payload), "recipient": "Fay Example",
                         "company": "Acme", "profile": "linkedin.com/in/fay", "message": text}
            self.db.execute("INSERT INTO outreach_queue VALUES ('linkedin.com/in/fay',77,'Fay Example','Acme',?,?,?)",
                            (approvals.AWAITING, json.dumps(candidate), json.dumps({"held_by": "REVIEW", "held_at": NOW.isoformat()})))
        card = self.card(self.page(), "prep-1")
        self.assertIn("Approve and send", card)
        self.assertIn("Reject, never send", card)
        self.assertNotIn("<textarea", card)
        page = self.post("/approvals/messages/approve", {"action_id": "prep-1", "payload_hash": sha(payload)})
        self.assertIn("Approved", page)
        self.assertEqual(self.db.execute("SELECT state FROM outreach_queue").fetchone()[0], "PENDING_VALIDATION")

    def test_automatic_mode_replaces_the_list_but_kept_messages_stay(self):
        self.review()
        self.hold("invite-h", "hana", note(100))
        approvals.keep_for_review(self.db, home=self.home)
        page = self.page()
        self.assertIn("Automatic mode is on: messages are sent without waiting. See them on", page)
        self.assertIn("These were kept for review", page)
        self.assertIsNotNone(self.card(page, "invite-h"))

    def test_get_writes_nothing_and_shows_no_secret(self):
        self.review()
        self.hold("invite-i", "ivan", note(100))
        files = sorted(path.name for path in (self.root / "state").iterdir())
        before = (self.root / approvals.DATABASE).read_bytes()
        self.client.get("/approvals")
        self.assertEqual(sorted(path.name for path in (self.root / "state").iterdir()), files)
        self.assertEqual((self.root / approvals.DATABASE).read_bytes(), before)
        self.assertNotIn(SECRET, self.client.get("/approvals").text)

    def test_actions_need_the_dashboard_origin(self):
        self.review()
        self.hold("invite-j", "jo", note(100))
        response = self.client.post("/approvals/messages/send", data={"action_id": "invite-j", "payload_hash": self.shown("invite-j"),
                                                                      "text": note(100)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.state("invite-j"), approvals.AWAITING)

    def test_recent_notices_list_dashboard_deliveries(self):
        applications.initialize(self.db)
        with self.db:
            self.db.execute("INSERT INTO notification_outbox(event_key,message,state,delivery,updated_at) VALUES (?,?,?,?,?)",
                            ("job:HUMAN_CHECK:r1", "A human check is waiting on Acme's form.", "DELIVERED",
                             json.dumps({"provider": "dashboard", "message_id": "job:HUMAN_CHECK:r1", "chat_id": "local"}), NOW.isoformat()))
            self.db.execute("INSERT INTO notification_outbox(event_key,message,state,delivery,updated_at) VALUES (?,?,?,?,?)",
                            ("other", "Sent by Telegram.", "DELIVERED", json.dumps({"provider": "telegram"}), NOW.isoformat()))
        page = self.page()
        self.assertIn("Sidedoor's browser asked for a human check (a CAPTCHA) and waited for you to tick it.", page)
        self.assertNotIn("Sent by Telegram.", page)

    def test_a1_notices_read_in_plain_words_with_a_link(self):
        """Owner, 2026-10-24 (A1): the Telegram-era rows, shaped as the writers write them, read as
        plain words with the job's company and role and a link; what is stored never changes."""
        import reporting
        applications.initialize(self.db)
        reporting.initialize(self.db)
        applications.ingest(self.db, {"company": "Nimbus Labs", "role": "Platform Engineer",
                                            "url": "https://jobs.example.test/nimbus/platform"}, NOW.isoformat())
        key = self.db.execute("SELECT job_key FROM application_jobs").fetchone()[0]
        self.db.execute("UPDATE application_jobs SET payload=json_set(payload,'$.company','Nimbus Labs') WHERE job_key=?", (key,))
        self.db.execute("INSERT INTO daily_reports(day,generated_at,notification_key,payload) VALUES (?,?,?,?)",
                        ("2026-10-21", NOW.isoformat(), "daily-report:2026-10-21", json.dumps(
            {"outcomes_24h": {"submitted": [{"job": "x"}], "discovered": {"research": 16}, "invitations": [], "inbound": []}})))
        rows = [(key + ":AUTHENTICATION_REQUIRED", "Job search: nimbus labs / " + key.split(":", 1)[1] + ": AUTHENTICATION_REQUIRED. "
                 "Attempts: 0. Owner: user. Next: Sign in. Deadline: 2026-10-23T06:57:57.151191+00:00."),
                (key + ":submitted", "Application verified: nimbus labs / x. Receipt: 9516eca4-da18-4ec4-8aa9-40bb11d4c39b"),
                (key + ":HUMAN_CHECK:530a7330-7969-4bfe-b48e-52aa7cbd04b7", "Job search: the nimbus labs application is waiting on a CAPTCHA."),
                ("daily-report:2026-10-21", "Job search, last 24 hours (2026-10-21)\nTelegram API receipt does not prove human read"),
                ("mystery:abc", "Something odd at 2026-10-23T06:57:57+00:00 for 0d6a1b86156205b01cf22a37 SOME_CODE_HERE")]
        with self.db:
            for number, (event_key, message) in enumerate(rows):
                self.db.execute("INSERT INTO notification_outbox(event_key,message,state,delivery,updated_at) VALUES (?,?,?,?,?)",
                                (event_key, message, "DELIVERED", json.dumps({"provider": "dashboard"}),
                                 (NOW + dt.timedelta(minutes=number)).isoformat()))
        before = self.db.execute("SELECT event_key,message FROM notification_outbox ORDER BY event_key").fetchall()
        page = self.page()
        notices = page.split('id="notices"')[1].split("</section>")[0]
        self.assertIn("Nimbus Labs (Platform Engineer): The application site asked for a sign-in.", notices)
        self.assertIn("Nimbus Labs (Platform Engineer): Application submitted and confirmed.", notices)
        self.assertIn("Nimbus Labs (Platform Engineer): Sidedoor's browser asked for a human check", notices)
        self.assertIn("Daily summary for Oct 21: 1 application submitted, 16 new jobs found, 0 invitations sent, "
                      "0 messages received.", notices)
        self.assertIn('<a href="/">See Overview</a>', notices)
        self.assertIn('href="/applications?job=' + urllib.parse.quote_plus(key) + '"', html.unescape(notices))
        self.assertIn("Something odd some code here", notices)
        for raw in ("AUTHENTICATION_REQUIRED", "Attempts:", "Owner: user", "2026-10-23T", "Receipt:", "unresolved:",
                    "0d6a1b86", "Telegram API receipt", "9516eca4"):
            self.assertNotIn(raw, notices)
        self.assertEqual(self.db.execute("SELECT event_key,message FROM notification_outbox ORDER BY event_key").fetchall(), before)
        response = self.client.get("/applications", params={"job": key})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Platform Engineer", html.unescape(response.text))

    def test_a_reply_card_reads_the_conversation_under_the_profile_key(self):
        """A reply action carries the inbox's profile URL; engagement_events store the profile key."""
        import engagement
        engagement.record_event(self.db, {"source": "unipile", "source_id": "m1", "profile": "https://www.linkedin.com/in/ines",
                                          "kind": "inbound", "occurred_at": NOW.isoformat(), "observed_at": NOW.isoformat(),
                                          "verified": True, "evidence": "test", "raw": {"text": "Would Tuesday work?"}})
        self.db.commit()
        thread = approvals_page.conversation(self.db, {"profile": "https://www.linkedin.com/in/ines"})
        self.assertEqual([(line["who"], line["text"]) for line in thread], [("Them", "Would Tuesday work?")])

    def test_question_controls_fit_the_question(self):
        """Owner, 2026-10-24: radios for yes/no and a few choices, a select for many, one line for a short
        value, a three-row box only for prose (a long question or a long-text type)."""
        control = approvals_page.answer_control
        self.assertEqual(control("boolean", ["Yes", "No"], "US citizen?"), "radio")
        self.assertEqual(control("choice", ["a", "b", "c"], "Pick"), "radio")
        self.assertEqual(control("choice", list("abcdef"), "Pick"), "select")
        self.assertEqual(control("text", [], "GitHub profile URL"), "text")
        self.assertEqual(control("long_text", [], "Why us?"), "textarea")
        self.assertEqual(control("text", [], "Describe a time you led a migration end to end, what went wrong and what you learned."), "textarea")
        self.assertEqual(control("text", [], "x", operational=True), "textarea")

    def test_a2_empty_lists_are_one_compact_line(self):
        page = self.page()
        for name in ("questions", "notes", "notices", "messages"):
            self.assertRegex(page, r'class="card approvals-section approvals-empty" id="%s"' % name)

    # D3
    def test_d3_an_approved_message_a_rule_refused_shows_why_and_can_be_dismissed(self):
        self.review()
        self.hold("invite-k", "kim", note(100))
        self.send("invite-k", note(100))
        with self.db:   # outreach.cycle: a sending rule refused it after the approval
            self.db.execute("UPDATE outreach_actions SET state='BLOCKED',result=? WHERE action_id='invite-k'",
                            (json.dumps(["PENDING_INVITATION_PRESENT"]),))
        card = self.card(self.page(), "invite-k")
        self.assertIn("Couldn't send", card)
        self.assertIn("An invitation to this person is already pending.", card)
        self.assertNotIn("PENDING_INVITATION_PRESENT", card)
        response = self.post("/approvals/refused/dismiss", {"action_id": "invite-k", "payload_hash": self.shown("invite-k")}, HTMX)
        self.assertIn("Dismissed.", response)
        self.assertIsNone(self.card(self.page(), "invite-k"))
        self.assertEqual(self.state("invite-k"), "BLOCKED")                   # dismissing never sends

    def test_d3_an_unapproved_blocked_message_is_not_listed(self):
        action = invite("invite-l", "lou", note(100))
        outreach.enqueue_action(self.db, action)
        with self.db:
            self.db.execute("UPDATE outreach_actions SET state='BLOCKED',result='[\"X\"]' WHERE action_id='invite-l'")
        self.assertIsNone(self.card(self.page(), "invite-l"))

    # Review fixes
    def test_r4_an_action_answers_with_its_own_card_only(self):
        self.review()
        self.hold("invite-m", "mia", note(100))
        self.hold("invite-n", "ned", note(110))
        response = self.send("invite-m", note(400), headers=HTMX)     # refused: over 300
        self.assertTrue(response.lstrip().startswith('<article class="approval-card"'))
        self.assertNotIn("<html", response)
        self.assertNotIn("invite-n", response)
        self.assertIn("Shorten it by 100", response)
        done = self.send("invite-m", note(100), headers=HTMX)
        self.assertIn("approval-done", done)
        self.assertNotIn("invite-n", done)

    def test_r11_a_malformed_row_is_counted_not_a_500(self):
        self.review()
        self.hold("invite-o", "olu", note(100))
        with self.db:
            self.db.execute("INSERT INTO outreach_actions VALUES ('bad','linkedin.com/in/bad','INVITE','{not json','x',?,NULL,'{also bad')",
                            (approvals.AWAITING,))
        page = self.page()
        self.assertIn("1 waiting message could not be read", page)
        self.assertIsNotNone(self.card(page, "invite-o"))
        response = self.client.post("/settings/mode", data={"mode": "automatic"}, headers=HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertIn("You have 1 message waiting", html.unescape(response.text))

    def test_r12_one_page_of_cards_and_the_indexes_the_reads_use(self):
        self.review()
        for index in range(approvals_page.PAGE_SIZE + 3):
            self.hold("invite-p%02d" % index, "person%02d" % index, note(100))
        page = self.page()
        self.assertEqual(page.count('<article class="approval-card"'), approvals_page.PAGE_SIZE)
        self.assertIn("Page 1 of 2 (28 waiting)", page)
        self.assertEqual(self.page("/approvals?page=2").count('<article class="approval-card"'), 3)
        applications.initialize(self.db)
        notifications.initialize(self.db)
        decision_transport.initialize(self.db)
        indexes = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertLessEqual({"outreach_agent_audit_action", "outreach_owner_records_identity", "notification_outbox_updated",
                              "user_decisions_state"}, indexes)
        plan = " ".join(str(row[-1]) for row in self.db.execute(
            "EXPLAIN QUERY PLAN SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION'", ("x",)))
        self.assertIn("outreach_agent_audit_action", plan)

    def test_r18_an_edited_card_says_it_sends_the_edited_text(self):
        self.review()
        self.hold("invite-q", "quin", note(100))
        record = {"action_id": "invite-q", "kind": approvals.REVISION, "edited_payload_hash": self.shown("invite-q")}
        with mock.patch.object(approvals, "decisions", return_value=[record]):
            card = self.card(self.page(), "invite-q")
        self.assertIn("Edited by you", card)
        self.assertIn("sends exactly what the box says: your edited text", card)

    def test_r19_busy_keeps_the_text_and_the_reject_reason(self):
        self.review()
        self.hold("invite-r", "rui", note(100))
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(dashboard, "write_connection", side_effect=locked):
            card = self.card(self.send("invite-r", note(90, "mine")), "invite-r")
            self.assertIn("Your text is still in the box.", card)
            self.assertIn(">" + note(90, "mine") + "</textarea>", card)
            card = self.card(self.post("/approvals/messages/reject", {"action_id": "invite-r", "payload_hash": self.shown("invite-r"),
                                                                      "reason": "Not now"}), "invite-r")
            self.assertIn("Your reason is still in the box.", card)
            self.assertIn('value="Not now"', card)
            card = self.card(self.post("/approvals/messages/approve", {"action_id": "invite-r", "payload_hash": self.shown("invite-r")}),
                             "invite-r")
            self.assertNotIn("still in the box", card)

    def test_r21_crlf_counts_and_stores_as_lf(self):
        self.review()
        self.hold("invite-s", "sam", note(100))
        lines = ("x" * 99 + "\r\n") * 3     # 300 characters with LF, 303 with CRLF
        self.assertIn("Saved your edit", self.send("invite-s", lines[:-2]))
        self.assertEqual(self.payload("invite-s")["message"], ("x" * 99 + "\n") * 2 + "x" * 99)


class NotesForYou(PageBase):
    def flag(self, task_id, reason):
        with mock.patch("workflow_tasks.record"), self.db:
            reply_tools.apply_intents(self.db, task_id, "source-" + task_id, [{"tool": "flag_owner", "intent": {"reason": reason}}],
                                      NOW.isoformat())
        return self.db.execute("SELECT rowid FROM reply_owner_flags WHERE task_id=?", (task_id,)).fetchone()[0]

    def needs(self):
        return overview_page.snapshot(self.root / approvals.DATABASE,
                                      lambda database: overview_page.needs_you(overview_page.Reader(database), NOW),
                                      lambda: dashboard.read_connection(self.root))

    # Worked example 4
    def test_example_4_mark_done_leaves_the_list_and_needs_you(self):
        reason = "Sam Example (Acme) suggests emailing tim@acme.com about the SRE role."
        note_id = self.flag("task-1", reason)
        self.assertEqual(self.needs()["notes"], 1)
        self.assertIn(reason, self.page())
        page = self.post("/approvals/notes/done", {"note_id": str(note_id)})
        self.assertIn("Marked done.", page)
        self.assertNotIn(reason, page)
        self.assertEqual(self.needs()["notes"], 0)
        self.assertEqual(reply_tools.owner_flags(self.db, "2000-01-01")[0]["reason"], reason)   # the daily report still has it

    def test_r5_a_multi_line_note_is_marked_done_by_its_id(self):
        note_id = self.flag("task-2", "Call Pat Example.\r\nTheir number is 555 0100.")
        self.post("/approvals/notes/done", {"note_id": str(note_id)})
        self.assertEqual(self.needs()["notes"], 0)

    def test_r14_a_note_names_the_person(self):
        workflow = __import__("workflow_tasks")
        workflow.initialize(self.db)
        outreach.enqueue_action(self.db, invite("invite-t", "tess", note(100)))
        with self.db:
            self.db.execute("INSERT INTO workflow_tasks(task_id,worker,action_id,parent_run_id,contact_id,requirement_revision,inputs,"
                            "deadline,status,max_attempts,owner,effect,updated_at) VALUES ('task-3','w','a','p',?,'r','[]','d','DONE',3,'o','READ','u')",
                            ("https://www.linkedin.com/in/tess",))
        self.flag("task-3", "Tess Example suggests a call.")
        page = self.page()
        self.assertIn("From your conversation with ", page)
        self.assertIn(">Tess Example</a>", page)

    def test_overview_needs_you_links_each_count_to_its_section(self):
        import test_dashboard_overview
        test_dashboard_overview.finished_home(self.home)
        self.review()
        self.hold("invite-u", "uma", note(100))
        self.flag("task-4", "Call Pat.")
        page = html.unescape(self.client.get("/").text)
        for count, what, href, label in (("1", "message to approve", "/approvals#messages", "Review"),
                                         ("1", "note for you", "/approvals#notes", "Read")):
            self.assertRegex(page, r'<span class="overview-number">%s</span>\s*<span class="overview-needs-what">%s</span>'
                                   r'\s*<a class="button overview-needs-go" href="%s">%s</a>' % (count, what, href, label))


class EveryListPages(PageBase):
    """Every list on Approvals pages like the messages list (owner, 2026-10-24): its own page parameter
    and #anchor, the real total counted, and paging one list never resets another."""

    flag, needs = NotesForYou.flag, NotesForYou.needs

    def questions(self, count):
        decision_transport.initialize(self.db)
        expires = int(NOW.timestamp() * 1000) + 30 * 86400000
        with self.db:
            for index in range(count):
                self.db.execute("INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,"
                                "policy_hash,expires_at,state,summary,event_key,label) VALUES (?,?,?,?,'text','[]','h','j','p',?,"
                                "'PENDING','s',?,?)", ("q%02d" % index, "job:%02d" % index, "f", "github_profile",
                                                       expires + index, "decision:q%02d" % index, "Question %02d?" % index))

    def test_sixty_questions_page_by_twenty_five_with_the_real_total(self):
        self.questions(60)
        page = self.page()
        section = page.split('id="questions"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(section.count('<article class="approval-card"'), 25)
        self.assertIn("Page 1 of 3 (60)", section)
        self.assertIn('href="/approvals?questions_page=2#questions">Next</a>', section)
        self.assertNotIn(">Previous</a>", section)
        last = self.page("/approvals?questions_page=3").split('id="questions"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(last.count('<article class="approval-card"'), 10)
        self.assertIn("Question 59?", last)
        self.assertIn('action="/approvals/questions/answer?questions_page=3"', last)   # a plain post comes back here
        self.assertEqual(self.needs()["questions"], 60)

    def test_sixty_notes_page_and_paging_one_list_keeps_the_other(self):
        for index in range(60):
            self.flag("task-n%02d" % index, "Note %02d." % index)
        self.questions(30)
        page = self.page("/approvals?notes_page=2&questions_page=2")
        notes = page.split('id="notes"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(notes.count('<article class="approval-card"'), 25)
        self.assertIn("Page 2 of 3 (60)", notes)
        self.assertIn('href="/approvals?questions_page=2&notes_page=3#notes">Next</a>', notes)
        questions = page.split('id="questions"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Page 2 of 2 (30)", questions)
        self.assertEqual(questions.count('<article class="approval-card"'), 5)
        self.assertEqual(self.needs()["notes"], 60)


class SettingsModeQuestion(PageBase):
    def held_ids(self):
        return [row[0] for row in self.db.execute("SELECT action_id FROM outreach_actions WHERE state=? ORDER BY action_id",
                                                  (approvals.AWAITING,))]

    # Worked example 5
    def test_example_5_send_them_releases_exactly_the_messages_asked_about(self):
        self.review()
        for index, person in enumerate(("kim", "lee", "max")):
            self.hold("invite-%d" % index, person, note(100 + index))
        page = self.post("/settings/mode", {"mode": "automatic"}, HTMX)
        self.assertIn("You have 3 messages waiting. Send them, or keep them for review?", page)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)   # nothing switched yet
        shown = list(zip(re.findall(r'name="action_id" value="([^"]+)"', page), re.findall(r'name="payload_hash" value="([^"]+)"', page)))
        self.assertEqual(len(shown), 3)
        self.hold("invite-late", "ned", note(110))     # held after the question was shown
        page = self.post("/settings/mode/release", {"action_id": [a for a, _ in shown], "payload_hash": [h for _, h in shown]}, HTMX)
        self.assertIn("3 messages will go out", page)
        self.assertEqual(self.held_ids(), ["invite-late"])
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.AUTOMATIC)
        self.assertEqual([record["decided_via"] for record in approvals.decisions(self.db, "invite-0")], ["mode_switch"])

    def test_keep_for_review_switches_and_leaves_them_waiting(self):
        self.review()
        self.hold("invite-k", "kay", note(100))
        self.post("/settings/mode", {"mode": "automatic"}, HTMX)
        page = self.post("/settings/mode/keep", {}, HTMX)
        self.assertIn("1 message kept for review on Approvals", page)
        self.assertEqual((self.held_ids(), approvals.install_mode(self.root, self.home)), (["invite-k"], approvals.AUTOMATIC))

    def test_no_question_when_nothing_waits(self):
        self.review()
        page = self.post("/settings/mode", {"mode": "automatic"}, HTMX)
        self.assertNotIn("messages waiting", page)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.AUTOMATIC)

    def test_r6_stay_in_review_dismisses_the_question(self):
        self.review()
        self.hold("invite-v", "vic", note(100))
        page = self.post("/settings/mode", {"mode": "automatic"}, HTMX)
        self.assertIn('hx-post="/settings/mode/stay"', page)
        page = self.post("/settings/mode/stay", {}, HTMX)
        self.assertIn("Still in Review. Nothing changed.", page)
        self.assertNotIn("Send them, or keep them for review?", page)
        self.assertIn('value="review" checked', page)
        self.assertEqual((approvals.install_mode(self.root, self.home), self.held_ids()), (approvals.REVIEW, ["invite-v"]))

    def test_r7_without_javascript_the_answer_lands_at_the_question(self):
        self.review()
        self.hold("invite-w", "wes", note(100))
        response = self.client.post("/settings/mode", data={"mode": "automatic"}, headers=ORIGIN)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/settings?ask=switch#mode"))
        page = self.page("/settings?ask=switch")
        self.assertIn("You have 1 message waiting. Send them, or keep them for review?", page)
        self.assertEqual(approvals.install_mode(self.root, self.home), approvals.REVIEW)
        response = self.client.post("/settings/mode/keep", headers=ORIGIN)
        self.assertEqual(response.headers["location"], "/settings?mode_done=kept&n=1&changed=0#mode")
        self.assertIn("1 message kept for review on Approvals", self.page(response.headers["location"].split("#")[0]))

    def test_a3_settings_shows_which_value_is_in_force_and_use_this_value_removes_the_kept_one(self):
        setup_steps.save_owner_answer("d9", "us_lawful_permanent_resident", "No", "Your answer for Job A", self.home)
        profile = self.page("/settings").split('id="profile"', 1)[1].split('id="resume"', 1)[0]
        self.assertIn("Your answer on Approvals is used instead of this: <strong>No</strong>", profile)
        self.assertIn("Used instead of your answer in Profile and search (Yes).", profile)
        response = self.post("/settings/answers/use-setup", {"key": "owner-answer:d9", "phone": "typed but unsaved"}, HTMX)
        self.assertIn("Now using this value.", response)
        self.assertIn('hx-swap-oob="true"', response)
        self.assertNotIn("Save profile", response)
        self.assertEqual(setup_steps.owner_answers(self.home), [])
        self.assertNotIn("is used instead of this", self.page("/settings"))

    def test_item4_use_this_value_shows_the_answer_now_in_force(self):
        setup_steps.save_owner_answer("d1", "us_lawful_permanent_resident", "No", "Your answer for Job A", self.home)
        setup_steps.save_owner_answer("d2", "us_lawful_permanent_resident", "No", "Your answer for Job B", self.home)
        response = self.post("/settings/answers/use-setup", {"key": "owner-answer:d2"}, HTMX)
        self.assertNotIn("Now using this value", response)
        self.assertIn("An earlier answer you gave there is used now, not this value.", response)
        self.assertIn('value="owner-answer:d1"', response)                         # its override note and Use this value
        self.assertIn('id="kept-owner-answer-d1" hx-swap-oob="true"', response)
        response = self.post("/settings/answers/use-setup", {"key": "owner-answer:d1"}, HTMX)
        self.assertIn("Now using this value.", response)                            # none left: the Profile value
        self.assertEqual(setup_steps.owner_answers(self.home), [])

    def test_item5_a_kept_answer_not_in_force_says_why(self):
        setup_steps.save_owner_answer("d1", "requires_visa_sponsorship_now_or_future_in_us", "Only after my OPT ends",
                                      "Your answer for Job A", self.home)
        setup_steps.save_owner_answer("d2", "relocation_city", "Austin", "Your answer for Job B", self.home)
        setup_steps.save_owner_answer("d3", "relocation_city", "Denver", "Your answer for Job C", self.home)
        page = self.page("/settings")
        self.assertIn("Not used: it is not a plain yes or no, and your profile records a yes or no for this", page)
        kept = page.split('id="kept-owner-answer-d2"', 1)[1].split('class="kept-answer"', 1)[0]
        self.assertIn("Not used: a newer answer you gave on Approvals to the same question is used.", kept)
        newest = page.split('id="kept-owner-answer-d3"', 1)[1].split('class="kept-answer"', 1)[0]
        self.assertNotIn("Not used", newest)

    def test_kept_answer_names_the_company_as_its_posting_writes_it(self):
        # Owner, 2026-10-24: "insforge (instacloud)" is the stored employer key; the posting says
        # "InsForge (InstaCloud)", as Applications shows it. An answer whose job is gone keeps the key.
        for decision_id, employer in (("d1", "insforge (instacloud)"), ("d2", "asi")):
            setup_steps.save_owner_answer(decision_id, "github_profile", "x", "Your answer", self.home,
                                          asked_for={"question": "GitHub profile", "employer": employer, "role": "Engineer"})
        import applications
        import decision_transport
        applications.initialize(self.db)
        decision_transport.initialize(self.db)
        self.db.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                        "VALUES ('j1','insforge (instacloud)','r',?, 'BLOCKED','2026-10-24T00:00:00Z')",
                        (json.dumps({"company": "InsForge (InstaCloud)"}),))
        self.db.execute("INSERT INTO user_decisions(id,job_key,field,semantic,answer_type,choices,payload_hash,job_hash,"
                        "policy_hash,expires_at,state,summary,event_key) VALUES ('d1','j1','f','s','text','[]','h','h','h',0,"
                        "'APPLIED','','e')")
        self.db.commit()
        page = self.page("/settings")
        self.assertIn("First asked for InsForge (InstaCloud), Engineer", page)
        self.assertIn("First asked for asi, Engineer", page)
        response = self.post("/settings/answers/change", {"key": "owner-answer:d1", "value": "y"}, HTMX)
        self.assertIn("First asked for InsForge (InstaCloud), Engineer", response)

    def test_r13_kept_answer_save_and_remove_swap_only_that_answer(self):
        setup_steps.save_owner_answer("d1", "relocation_city", "Austin, for this role", "Your answer to \"Relocation city\" for Acme",
                                      self.home)
        page = self.page("/settings")
        self.assertIn("Your saved answers", page)
        self.assertIn('Your answer to "Relocation city" for Acme', page)
        response = self.post("/settings/answers/change", {"key": "owner-answer:d1", "value": "Denver"}, HTMX)
        self.assertTrue(response.lstrip().startswith('<div class="kept-answer"'))
        self.assertNotIn("Save profile", response)
        self.assertIn("Saved the answer.", response)
        self.assertEqual(setup_steps.owner_answers(self.home)[0][1]["value"], "Denver")
        self.assertIn("Removed.", self.post("/settings/answers/remove", {"key": "owner-answer:d1"}, HTMX))
        self.assertEqual(setup_steps.owner_answers(self.home), [])
        self.client.post("/settings/answers/remove", data={"key": "email"}, headers=ORIGIN)   # only kept answers
        self.assertIn("email", setup_steps.read_profile(self.home)["answers"])


# --- Questions for you (worked example 3) and the answer kept for later jobs ----------------------

if DEPENDENCIES:
    import test_applications_e2e as fixtures
    from test_applications_e2e import MockBridge

    CLEARANCE = {"id": "q-secret", "label": "Do you hold an active Secret clearance?", "semantic": "active_secret_clearance",
                 "answer_type": "boolean", "choices": ["Yes", "No"], "required": True}
    ASKED = dict(CLEARANCE, id=applications.question_field(CLEARANCE))   # as applications files a question
    WHY = {"id": None, "label": "Why us?", "semantic": "why_us", "answer_type": "text", "choices": [], "required": True}

    class QuestionsForYou(unittest.TestCase):
        """The dashboard answer reaches decision_transport's own processing, like a Telegram reply."""

        fixture_setup = fixtures.ApplicationEndToEndTests.setUp
        discover = fixtures.ApplicationEndToEndTests.discover
        job = fixtures.ApplicationEndToEndTests.job

        def setUp(self):
            self.fixture_setup()
            self.discover()
            self.decision_id = self.ask(self.job(), ASKED)
            self.home = user_profile.home()
            app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config={},
                                       browser=mock.Mock(is_open=lambda: False), relay_url="https://relay.test")
            self.client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
            self.client.get("/", params={"t": SECRET})
            self.decision = self.row(self.decision_id)

        def ask(self, job, question):
            with self.database:
                applications.hold(self.database, job, "REQUIRED_ELIGIBILITY_ANSWER", "Answer the question",
                                  fixtures.NOW, "BLOCKED", "2026-09-25T16:00:00+00:00")
                identifier = decision_transport.request_fact(self.database, self.job(job["job_key"]), question, self.policy, fixtures.NOW)
                # The fixture's clock is in the past; the question is still open today.
                self.database.execute("UPDATE user_decisions SET expires_at=? WHERE id=?",
                                      (int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 7 * 86400000, identifier))
            return identifier

        def row(self, identifier):
            return dict(self.database.execute("SELECT * FROM user_decisions WHERE id=?", (identifier,)).fetchone())

        def answer(self, identifier, value, shown=None, headers=ORIGIN):
            return html.unescape(self.client.post("/approvals/questions/answer", headers=headers, data={
                "decision_id": identifier, "payload_hash": shown or self.row(identifier)["payload_hash"], "answer": value}).text)

        def test_example_3_dashboard_answer_is_processed_like_a_telegram_reply(self):
            page = html.unescape(self.client.get("/approvals").text)
            self.assertIn("Do you hold an active Secret clearance?", page)   # the form's own label (2026-10-24)
            self.assertIn('name="decision_id" value="%s"' % self.decision_id, page)
            self.assertIn("Answer saved.", self.answer(self.decision_id, "No"))
            saved = self.database.execute("SELECT value,source FROM application_answers WHERE field=?", (self.decision["field"],)).fetchone()
            self.assertEqual((json.loads(saved["value"]), saved["source"]), (False, "authenticated-owner-decision:" + self.decision_id))
            self.assertEqual(self.row(self.decision_id)["state"], "ANSWERED")
            self.assertIsNone(self.job()["next_at"])                               # due again: the job goes on
            inbox = self.database.execute("SELECT seq,state,acked,payload FROM transport_inbox").fetchone()
            self.assertEqual((inbox["seq"] < 0, inbox["state"], inbox["acked"], json.loads(inbox["payload"])["via"]),
                             (True, "APPLIED", 1, "dashboard"))
            # The relay never sees the dashboard's inbox row (acked=1), and its cursor is untouched.
            bridge = MockBridge()
            decision_transport.cycle(self.database, {"bridge_transport": {"owner_id": "mock-owner"}}, self.root, bridge, fixtures.NOW)
            self.assertEqual(bridge.acked, set())
            self.assertEqual(self.database.execute("SELECT seq FROM transport_cursor").fetchone()[0], 0)
            # The job unblocks: the next applications pass resolves the question from the answer.
            policy = dict(self.policy, answers=dict(self.policy["answers"], **{self.decision["field"]: {
                "semantic": CLEARANCE["semantic"], "value": False, "source": saved["source"]}}))
            self.assertEqual(applications.resolve_answers([CLEARANCE], policy), ({self.decision["field"]: False}, []))
            # Owner, 2026-10-22 ("a"): the answer is also kept in profile.json for later jobs.
            [(key, record)] = setup_steps.owner_answers(self.home)
            self.assertEqual((key, record["semantic"], record["value"]),
                             ("owner-answer:" + self.decision_id, "active_secret_clearance", False))
            self.assertTrue(record["source"].startswith(decision_transport.OWNER_ANSWER_SOURCE + self.decision_id))
            self.assertEqual(user_profile.load()["answers"][key]["value"], False)

        def test_same_effect_as_the_telegram_reply(self):
            """The Telegram path for the same question writes the same answer (the shared `_apply_answer`)."""
            config = {"bridge_transport": {"owner_id": "mock-owner"}}
            bridge = MockBridge()
            decision_transport.cycle(self.database, config, self.root, bridge, fixtures.NOW)
            with self.database:
                self.database.execute("UPDATE user_decisions SET message_id=7 WHERE id=?", (self.decision_id,))
            bridge.events = [{"seq": 1, "decision_id": self.decision_id, "owner": "mock-owner", "action_id": self.decision["job_key"],
                              "payload_hash": self.decision["payload_hash"], "response": {"status": "answered", "raw": "No"}}]
            decision_transport.cycle(self.database, config, self.root, bridge, fixtures.NOW)
            saved = self.database.execute("SELECT value,source FROM application_answers WHERE field=?", (self.decision["field"],)).fetchone()
            self.assertEqual((json.loads(saved["value"]), saved["source"]), (False, "authenticated-owner-decision:" + self.decision_id))

        def test_r15_a_refused_text_answer_is_kept(self):
            other = self.ask(self.job(), dict(WHY, id=applications.question_field(WHY)))
            page = self.answer(other, "Because of the mission.", shown="not-the-shown-one")
            self.assertIn("This question changed since it was shown", page)
            self.assertIn('name="answer" maxlength="4000" aria-labelledby="label-', page)
            self.assertIn('value="Because of the mission." required>', page)   # a short question: one line
            self.assertEqual(self.row(other)["state"], "PENDING")
            self.assertEqual(setup_steps.owner_answers(self.home), [])

        def test_r3_another_job_asking_the_same_thing_is_closed_and_made_due(self):
            job_b = self.discover(fixtures.role("Other AI", "role-456"))
            with self.database:
                self.database.execute("UPDATE application_jobs SET next_at='2099-01-01T00:00:00+00:00' WHERE job_key=?", (job_b,))
            other = self.ask(self.job(job_b), ASKED)
            with self.database:
                self.database.execute("UPDATE application_jobs SET next_at='2099-01-01T00:00:00+00:00' WHERE job_key=?", (job_b,))
            self.assertIn("Answer saved.", self.answer(self.decision_id, "No"))
            self.assertEqual((self.row(other)["state"], self.row(other)["last_error"]),
                             ("STALE", "FACT_REQUEST_REQUIRES_FRESH_JOB_REVIEW"))
            self.assertIsNone(self.job(job_b)["next_at"])                         # job B is re-evaluated now
            self.assertNotIn('value="%s"' % other, html.unescape(self.client.get("/approvals").text))

        def test_a4_a_malformed_job_payload_never_stops_the_run(self):
            job_b = self.discover(fixtures.role("Other AI", "role-789"))
            other = self.ask(self.job(job_b), ASKED)
            with self.database:
                self.database.execute("UPDATE application_jobs SET payload='{not json' WHERE job_key=?", (job_b,))
                self.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (self.decision["job_key"],))
            self.assertEqual(decision_transport.refresh_stale(self.database, self.policy, fixtures.NOW), [self.decision_id])
            self.assertEqual(self.row(other)["state"], "PENDING")
            result = decision_transport.cycle(self.database, {}, self.root, MockBridge(), fixtures.NOW)
            self.assertEqual(result["state"], "BLOCKED")                           # reported, not raised

        def test_item7_any_error_reading_one_job_skips_only_that_question(self):
            job_b = self.discover(fixtures.role("Other AI", "role-790"))
            other = self.ask(self.job(job_b), ASKED)
            with self.database:
                self.database.execute("UPDATE application_jobs SET state='SUBMITTED' WHERE job_key=?", (self.decision["job_key"],))
                self.database.execute("UPDATE application_jobs SET payload='{\"company\": 1}' WHERE job_key=?", (job_b,))
            real = decision_transport.row_job_hash

            def shaped(decision, payload):
                if decision["id"] == other:
                    raise KeyError("company")          # parses, but not the shape the hash needs
                return real(decision, payload)
            with mock.patch.object(decision_transport, "row_job_hash", side_effect=shaped), \
                    self.assertLogs("sidedoor.decision_transport", "WARNING") as logged:
                self.assertEqual(decision_transport.refresh_stale(self.database, self.policy, fixtures.NOW), [self.decision_id])
            self.assertIn(other, logged.output[0])
            self.assertEqual(self.row(other)["state"], "PENDING")

        def test_a1_a_yes_no_choice_answered_in_the_dashboard_is_kept_as_a_boolean(self):
            choice = {"id": None, "label": "Open to visa sponsorship?", "semantic": "open_to_employer_visa_sponsorship",
                      "answer_type": "choice", "choices": ["Yes", "No"], "required": True}
            identifier = self.ask(self.job(), dict(choice, id=applications.question_field(choice)))
            self.assertIn("Answer saved.", self.answer(identifier, "Yes"))
            self.assertIs(user_profile.load()["answers"]["owner-answer:" + identifier]["value"], True)
            self.assertIs(applications.policy_answer(user_profile.load(), "open_to_employer_visa_sponsorship"), True)

        def test_r16_answer_locally_with_no_profile_refuses_in_plain_words(self):
            with mock.patch.object(user_profile, "load", side_effect=user_profile.ProfileMissing("none")):
                result = decision_transport.answer_locally(self.database, self.decision_id, "No",
                                                           shown_payload_hash=self.decision["payload_hash"])
            self.assertEqual((result["state"], result["reason"]), ("REFUSED", "PROFILE_MISSING"))
            (self.home / "profile.json").rename(self.home / "profile.json.away")
            try:
                self.assertIn("Your profile is missing", self.answer(self.decision_id, "No"))
            finally:
                (self.home / "profile.json.away").rename(self.home / "profile.json")

        def test_r16_the_dashboard_answers_against_its_own_home(self):
            elsewhere = Path(tempfile.mkdtemp(prefix="sidedoor-elsewhere-"))
            self.addCleanup(shutil.rmtree, elsewhere, True)
            with mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(elsewhere)}):   # no profile there
                self.assertIn("Answer saved.", self.answer(self.decision_id, "No"))
            self.assertEqual(len(setup_steps.owner_answers(self.home)), 1)

        def test_r20_the_negative_seq_is_taken_by_the_insert_itself(self):
            statements = []
            self.database.set_trace_callback(statements.append)
            self.addCleanup(self.database.set_trace_callback, None)
            for _ in range(2):
                decision_transport.answer_locally(self.database, self.decision_id, "No", shown_payload_hash="stale", policy=self.policy)
            self.assertEqual([row[0] for row in self.database.execute("SELECT seq FROM transport_inbox ORDER BY seq")], [-2, -1])
            self.assertTrue(any(statement.startswith("BEGIN IMMEDIATE") for statement in statements))
            self.assertFalse(any("MIN(seq)" in statement and not statement.startswith("INSERT") for statement in statements))


# --- Later jobs: the AI, not Python, fills a form from a kept answer; eligibility counts it (D1) ----

import test_answering_agent as answering_base
from test_answering_agent import FakeModel, entry

LEVEL = {"id": "q-level", "required": True, "answer_type": "text", "choices": [],
         "semantic": "security_clearance_level", "label": "Security clearance level?"}


class KeptAnswerForLaterJobs(answering_base.ResumeFixture):
    def kept(self, value, semantic="active_secret_clearance", policy=None, decision="d1"):
        home = Path(tempfile.mkdtemp(prefix="sidedoor-kept-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "profile.json").write_text(json.dumps(policy or self.policy))
        setup_steps.save_owner_answer(decision, semantic, value, "Your answer to \"Active secret clearance\" for Job A", home)
        return setup_steps.read_profile(home)

    @staticmethod
    def reader(question, packet):
        """A fake answering AI: it answers the clearance level from a recorded clearance fact, else UNKNOWN."""
        recorded = [answer for answer in packet["policy"]["answers"].values()
                    if answer["semantic"] == "active_secret_clearance"]
        if recorded and recorded[0]["value"] is False:
            return entry(question, status="ANSWERED", unknown_reason=None, value="None", question_class="OTHER",
                         derivation="The owner recorded that they hold no active Secret clearance.")
        return entry(question)

    def test_worked_example_job_b_is_answered_by_the_ai_from_the_kept_fact(self):
        model = FakeModel(self.reader)
        result = self.run_answer([LEVEL], model, policy=self.kept(False))
        self.assertEqual(result["answers"][self.key(LEVEL)]["value"], "None")
        self.assertEqual(result["unknown"], [])                                      # not raised to the owner again
        recorded = model.packets[0]["policy"]["answers"]["owner-answer:d1"]
        self.assertTrue(recorded["source"].startswith(decision_transport.OWNER_ANSWER_SOURCE))

    def test_without_the_kept_answer_job_b_goes_back_to_the_owner(self):
        result = self.run_answer([LEVEL], FakeModel(self.reader))
        self.assertEqual(result["unknown"], [self.key(LEVEL)])

    def test_python_never_copies_a_kept_text_answer_onto_another_question(self):
        city = {"id": "q-city", "required": True, "answer_type": "text", "choices": [], "semantic": "relocation_city",
                "label": "Which city would you relocate to?"}
        policy = self.kept("Austin, for this role", "relocation_city")
        self.assertEqual(applications.resolve_answers([city], policy), ({}, [applications.question_field(city)]))
        self.assertTrue(applications.recorded_fact_for_ai(dict(city, required=False), policy))

    def test_d1_a_kept_answer_counts_for_eligibility_and_the_newer_wins(self):
        semantic = "requires_visa_sponsorship_now_or_future_in_us"
        self.assertIs(applications.policy_answer(self.policy, semantic), False)          # Setup's answer
        policy = self.kept(True, semantic)
        self.assertIs(applications.policy_answer(policy, semantic), True)                # the kept answer is newer
        later = self.kept(False, semantic, policy=policy, decision="d2")
        self.assertIs(applications.policy_answer(later, semantic), False)                # the latest kept answer wins
        # An eligibility reader built on policy_answer: a kept "not a permanent resident" changes it.
        self.assertEqual(applications.us_authorization_label(self.policy), applications.US_PERMANENT_RESIDENT_ACCEPTED)
        resident = self.kept(False, "us_lawful_permanent_resident")
        self.assertNotEqual(applications.us_authorization_label(resident), applications.US_PERMANENT_RESIDENT_ACCEPTED)

    def test_a1_a_kept_yes_or_no_is_a_boolean_and_a_non_boolean_never_flips_eligibility(self):
        sponsorship = "open_to_employer_visa_sponsorship"
        policy = self.kept("Yes", sponsorship, decision="d1")
        self.assertIs(policy["answers"]["owner-answer:d1"]["value"], True)          # from a Yes/No choice, by the Setup boolean
        self.assertIs(applications.policy_answer(policy, sponsorship), True)        # a string "Yes" would block EU jobs
        resident = self.kept("No", "us_lawful_permanent_resident", decision="d2")
        self.assertIs(applications.policy_answer(resident, "us_lawful_permanent_resident"), False)
        self.assertNotEqual(applications.us_authorization_label(resident), applications.US_PERMANENT_RESIDENT_ACCEPTED)
        needs = "requires_visa_sponsorship_now_or_future_in_us"
        unclear = self.kept("Only after my OPT ends", needs, decision="d3")
        self.assertEqual(unclear["answers"]["owner-answer:d3"]["value"], "Only after my OPT ends")
        self.assertIs(applications.policy_answer(unclear, needs), False)            # Setup's boolean still decides
        self.assertNotIn("owner-answer:d3", applications.answers_in_force(unclear["answers"]))

    def test_item1_a_newer_non_boolean_falls_back_to_the_newest_kept_boolean(self):
        needs = "requires_visa_sponsorship_now_or_future_in_us"
        self.assertIs(applications.policy_answer(self.policy, needs), False)                  # Setup: False
        policy = self.kept(True, needs, decision="d1")                                       # kept True (earlier)
        policy = self.kept("Yes, after 2027", needs, policy=policy, decision="d2")          # kept text (later)
        self.assertEqual(policy["answers"]["owner-answer:d2"]["value"], "Yes, after 2027")
        self.assertIs(applications.policy_answer(policy, needs), True)                       # the newest kept boolean
        self.assertEqual(applications.kept_answer_status(policy["answers"]),
                         {"owner-answer:d1": None, "owner-answer:d2": applications.NOT_USED_NOT_YES_NO})

    def test_a1_a_yes_no_choice_is_kept_as_a_boolean_for_a_new_semantic(self):
        home = Path(tempfile.mkdtemp(prefix="sidedoor-kept-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "profile.json").write_text(json.dumps(self.policy))
        setup_steps.save_owner_answer("d4", "holds_secret_clearance", "No", "Job A", home, yes_no=True)
        self.assertIs(setup_steps.read_profile(home)["answers"]["owner-answer:d4"]["value"], False)
        setup_steps.save_owner_answer("d5", "relocation_city", "No", "Job A", home)      # not a yes/no question: kept as written
        self.assertEqual(setup_steps.read_profile(home)["answers"]["owner-answer:d5"]["value"], "No")

    def test_r9_a_kept_and_a_setup_answer_are_not_two_conflicting_matches(self):
        location = {"id": "q-loc", "required": True, "answer_type": "text", "choices": [], "semantic": "current_location",
                    "label": "Where are you based?"}
        policy = self.kept("Austin, Texas", "current_location")
        matches = [answer for answer in applications.answers_in_force(policy["answers"]).values()
                   if answer.get("semantic") == "current_location"]
        self.assertEqual([answer["value"] for answer in matches], ["Austin, Texas"])
        self.assertEqual(applications.policy_answer(policy, "current_location"), "Austin, Texas")
        self.assertTrue(applications.recorded_fact_for_ai(dict(location, required=False), policy))   # the AI applies it


class SetupChangeWins(unittest.TestCase):
    """R10: a Setup save that changes a semantic's value removes the kept answers it contradicts."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="sidedoor-setup-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        import test_settings_page
        self.form = dict(test_settings_page.FORM)
        state, errors = setup_steps.save_profile(self.form, self.home)
        self.assertEqual(errors, {})

    def test_r10_setup_authorization_change_removes_the_contradicting_kept_answer(self):
        semantic = "requires_visa_sponsorship_now_or_future_in_us"
        setup_steps.save_owner_answer("d1", semantic, True, "Your answer for Job A", self.home)
        setup_steps.save_profile(self.form, self.home)                        # Setup unchanged: the kept answer stays
        self.assertEqual(len(setup_steps.owner_answers(self.home)), 1)
        setup_steps.save_profile(dict(self.form, work_authorization=["United States: allowed to work now, will need sponsorship"]),
                                 self.home)                                     # Setup now says True too: no contradiction
        self.assertEqual(len(setup_steps.owner_answers(self.home)), 1)
        setup_steps.save_owner_answer("d2", "currently_authorized_to_work_in_us", False, "Your answer for Job B", self.home)
        setup_steps.save_profile(dict(self.form, work_authorization=["United States: allowed to work, no sponsorship needed"]),
                                 self.home)                                     # changed to False: d1 (True) is contradicted
        self.assertEqual([key for key, unused in setup_steps.owner_answers(self.home)], ["owner-answer:d2"])


if __name__ == "__main__":
    unittest.main()


class NamesLikeNetworking(unittest.TestCase):
    """Owner, 2026-10-24: message cards say "Title at Company" / "Works at Company" (never "Upwind, Upwind"),
    and question cards keep the posting's spelling of the company."""

    def test_message_and_couldnt_send_lines_and_question_company(self):
        import approvals_page
        self.assertEqual(approvals_page._role_line("Upwind", "Upwind"), "Works at Upwind")
        self.assertEqual(approvals_page._role_line("Quarry Staff SRE", "Quarry"), "Staff SRE at Quarry")
        self.assertEqual(approvals_page._role_line(None, "Velo"), "Works at Velo")
        self.assertEqual(approvals_page.job_names("harborai", {"company": "HarborAI", "role": "AI Engineer"}),
                         ("HarborAI", "AI Engineer"))
