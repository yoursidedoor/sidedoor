"""The Networking & Referrals page (E6.T5; E6.T0 spec section 7, route /sent): one entry per contact.

The page reuses Approvals' readers (approvals_page imports fastapi), so these tests need the dashboard
packages (fastapi, httpx) and are skipped without them.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import html
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import approvals
import engagement
import outreach
import outreach_agent
import overview_page
import referrals
import reply_tools
import setup_steps
from tests.test_dashboard_overview import CONFIG, CSS, PORT, SECRET, database_with_schema, finished_home

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard
    import networking_page

ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
HTMX = dict(ORIGIN, **{"hx-request": "true"})
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")
START = dt.datetime(2026, 10, 15, 17, 0, tzinfo=dt.timezone.utc)
SENDER = 4242


def at(hours):
    return (START + dt.timedelta(hours=hours)).isoformat()


def url(slug):
    return "https://www.linkedin.com/in/" + slug


class Records:
    """Rows written the way the product's own writers write them."""

    def open_store(self, root):
        self.db = database_with_schema(root)
        self.addCleanup(self.db.close)
        outreach.initialize(self.db)
        outreach_agent.initialize(self.db)
        approvals.initialize(self.db)
        referrals.initialize(self.db)
        reply_tools.initialize(self.db)
        self.db.commit()

    def action(self, action_id, slug, purpose, state, message, claimed=None, result=None, company="Acme", **extra):
        payload = dict({"action_id": action_id, "profile": url(slug), "recipient": slug.split("-")[0].title() + " Example",
                        "company": company, "purpose": purpose, "message": message,
                        "provider": "unipile"}, **extra)
        if purpose == "INVITE":
            payload.setdefault("note", message)
        text = json.dumps(payload, sort_keys=True)
        self.db.execute("INSERT INTO outreach_actions VALUES (?,?,?,?,?,?,?,?)",
                        (action_id, "linkedin.com/in/" + slug, purpose + ":" + action_id, text,
                         hashlib.sha256(text.encode()).hexdigest(), state, claimed, json.dumps(result) if result else None))
        self.db.commit()

    def verdict(self, action_id, decision="READY", reason="Specific, short and about their team.", source=None):
        reference = outreach_agent.audit(self.db, action_id, "INSPECTION", "OBSERVATION", at(0), {"packet": action_id})
        record = {"observation_id": reference, "decision": {"decision": decision, "reason": reason}}
        if source:
            record["decision_source"] = source
        outreach_agent.audit(self.db, action_id, "INSPECTION", "DECISION", at(0), record)

    def event(self, slug, kind, hours, source="unipile", source_id=None, raw=None, **extra):
        event = dict({"source": source, "source_id": source_id or "%s:%s:%s" % (kind, slug, hours), "profile": url(slug),
                      "kind": kind, "occurred_at": at(hours), "observed_at": at(hours), "verified": True,
                      "evidence": "test", "raw": raw or {}}, **extra)
        engagement.record_event(self.db, event)

    def message(self, slug, sender, body, hours, chat="chat-1", lead=None):
        event = engagement.unipile_message_event(chat, {"sender": sender, "body": body, "createdAt": at(hours)}, url(slug),
                                                 SENDER, url("me"), at(hours))
        if lead:
            event["raw"]["correspondent_id"] = lead
        engagement.record_event(self.db, event)

    def invited(self, slug, note, hours, action_id=None):
        action_id = action_id or "invite-" + slug
        self.action(action_id, slug, "INVITE", "CONFIRMED_SENT", note, claimed=at(hours))
        self.verdict(action_id)
        self.event(slug, "initial_sent", hours, source_id="invite:" + action_id, raw={"note": note}, purpose="INVITE",
                   invitation=True)

    def flag(self, task_id, slug, reason):
        with self.db:
            self.db.execute("INSERT INTO workflow_tasks(task_id,worker,action_id,parent_run_id,contact_id,requirement_revision,"
                            "inputs,deadline,status,max_attempts,owner,effect,updated_at) "
                            "VALUES (?,'w','a','p',?,'r','[]','d','DONE',3,'o','READ','u')", (task_id, url(slug)))
        with mock.patch("workflow_tasks.record"), self.db:
            reply_tools.apply_intents(self.db, task_id, "source-" + task_id, [{"tool": "flag_owner", "intent": {"reason": reason}}],
                                      at(30))


class Page(Records):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        finished_home(self.home)

    def client(self):
        app = dashboard.create_app(SECRET, PORT, home=self.home, root=self.root, config=CONFIG)
        client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
        client.get("/", params={"t": SECRET})
        return client

    def get(self, path="/sent", **params):
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"):
            response = self.client().get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return html.unescape(response.text)

    def view(self, **arguments):
        return overview_page.snapshot(self.root / networking_page.DATABASE,
                                      lambda database: networking_page.gather(database, CONFIG, **arguments),
                                      lambda: dashboard.read_connection(self.root))

    def entries(self, **arguments):
        return self.view(**arguments)["list"]["contacts"]


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class NetworkingPageTest(Page, unittest.TestCase):

    # --- the worked examples -------------------------------------------------------------------

    def test_example_1_invitation_acceptance_followup_and_reply_are_one_entry(self):
        self.open_store(self.root)
        self.invited("jane-doe", "Hi Jane, I build SRE tooling and would value your view of Acme's platform team.", 0)
        self.event("jane-doe", "accepted", 20, source_id="acceptance:jane")
        self.action("follow-1", "jane-doe", "FOLLOWUP", "CONFIRMED_SENT", "Thanks for connecting, Jane. Is the SRE role open?",
                    claimed=at(90))
        self.verdict("follow-1", reason="A polite first follow-up.")
        self.event("jane-doe", "followup_sent", 91, source_id="receipt-follow-1", raw={"action_id": "follow-1"},
                   purpose="FOLLOWUP")
        self.message("jane-doe", "CORRESPONDENT", "Happy to refer you", 100)
        referrals.record(self.db, "linkedin.com/in/jane-doe", "AGREED", "source-1", "Happy to refer you",
                         "Send Jane your résumé", None, at(101), {})
        self.db.commit()

        found = self.entries()
        self.assertEqual(len(found), 1)
        jane = found[0]
        self.assertEqual(jane["name"], "Jane Example")
        self.assertEqual(jane["company"], "Acme")
        self.assertEqual([item["label"] for item in jane["conversation"]],
                         ["Invitation note", "Accepted your invitation", "Follow-up 1", "Their message"])
        self.assertEqual(jane["conversation"][3]["text"], "Happy to refer you")
        self.assertEqual(jane["conversation"][2]["text"], "Thanks for connecting, Jane. Is the SRE role open?")
        self.assertEqual(jane["conversation"][2]["judge"]["verdict"], "Ready to send")
        self.assertEqual(jane["referral"]["words"], "Referral agreed")
        self.assertEqual(jane["status"], "Replied")
        page = self.get()
        self.assertEqual(page.count('class="card net-row"'), 1)
        for text in ("Invitation note", "Accepted your invitation", "Follow-up 1", "Happy to refer you", "Referral agreed",
                     "Send Jane your résumé", "A polite first follow-up.", "Sent automatically"):
            self.assertIn(text, page)
        self.assertIn('href="https://linkedin.com/in/jane-doe"', page)
        self.assertEqual(len(self.entries(show="referral_agreed")), 1)
        self.assertEqual(len(self.entries(show="replied")), 1)
        self.assertEqual(len(self.entries(show="waiting")), 0)

    def test_example_2_a_note_shows_on_its_entry_and_mark_done_closes_it_everywhere(self):
        self.open_store(self.root)
        self.invited("sam-b", "Hi Sam, I would value your view of the platform team.", 0)
        self.message("sam-b", "CORRESPONDENT", "Email tim@acme.com about SRE, he runs hiring.", 50)
        reason = "Sam suggests you email tim@acme.com about SRE."
        self.flag("task-1", "sam-b", reason)
        self.db.commit()
        client = self.client()
        page = html.unescape(client.get("/sent").text)
        entry = page.split('class="card net-row"', 1)[1].split("</section>", 1)[0]
        self.assertIn(reason, entry)
        self.assertIn('action="/sent/notes/done"', entry)
        self.assertEqual(len(self.entries(show="needs_you")), 1)
        self.assertIn(reason, html.unescape(client.get("/approvals").text))
        note_id = self.db.execute("SELECT rowid FROM reply_owner_flags").fetchone()[0]
        answer = client.post("/sent/notes/done", data={"note_id": str(note_id), "back": "/sent?show=needs_you"}, headers=HTMX)
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Marked done.", answer.text)
        self.assertNotIn(reason, html.unescape(client.get("/sent").text))
        self.assertNotIn(reason, html.unescape(client.get("/approvals").text))
        needs = overview_page.snapshot(self.root / networking_page.DATABASE,
                                       lambda database: overview_page.needs_you(overview_page.Reader(database), START),
                                       lambda: dashboard.read_connection(self.root))
        self.assertEqual(needs["notes"], 0)
        self.assertEqual(len(self.entries(show="needs_you")), 0)
        # Without JavaScript the form goes back to the page it came from.
        again = client.post("/sent/notes/done", data={"note_id": str(note_id), "back": "https://evil.example/"}, headers=ORIGIN)
        self.assertEqual((again.status_code, again.headers["location"]), (303, "/sent?noted=0"))

    def test_example_3_aliases_join_one_person_and_unsafe_member_ids_stay_apart(self):
        self.open_store(self.root)
        # HeyReach era: a campaign invitation under the old spelling; Unipile: the same person under the new one.
        self.action("prep-1", "jane-old", "PREPARE_CAMPAIGN", "PREPARED", "Hi Jane, campaign note.", provider="heyreach")
        self.event("jane-old", "initial_sent", 0, source="heyreach", source_id="campaign-invitation:7", purpose="INITIAL",
                   campaign_id=7, preparation_action_id="prep-1", raw={"correspondent_id": "777"})
        with self.db:
            self.db.execute("INSERT INTO rw_aliases VALUES ('contact',?,?,'owner-verified')", (url("jane-old"), url("jane-new")))
        self.action("follow-j", "jane-new", "FOLLOWUP", "CONFIRMED_SENT", "Following up, Jane.", claimed=at(80))
        self.event("jane-new", "followup_sent", 81, source_id="receipt-j", raw={"action_id": "follow-j"}, purpose="FOLLOWUP")
        # A member-id spelling whose lead id only Jane's records carry: Jane.
        self.message("acoaajane1", "CORRESPONDENT", "Thanks, Jane here from my other profile.", 90, chat="chat-j", lead="777")
        # A member-id spelling two different tracked people's records carry: kept apart.
        self.invited("bob-x", "Hi Bob.", 1)
        self.invited("carol-y", "Hi Carol.", 2)
        self.event("bob-x", "accepted", 3, source_id="acceptance:bob", raw={"correspondent_id": "888"})
        self.event("carol-y", "accepted", 4, source_id="acceptance:carol", raw={"correspondent_id": "888"})
        self.message("acoaaboth2", "CORRESPONDENT", "Which of us did you mean?", 95, chat="chat-b", lead="888")
        self.message("acoaaboth2", "ME", "Sorry, wrong person.", 96, chat="chat-b")
        self.db.commit()

        found = self.entries()
        jane = [entry for entry in found if "Following up, Jane." in json.dumps(entry["conversation"], default=str)]
        self.assertEqual(len(jane), 1)
        labels = [item["label"] for item in jane[0]["conversation"]]
        self.assertEqual(labels, ["Invitation note", "Follow-up 1", "Their message"])
        self.assertEqual(jane[0]["conversation"][0]["text"], "Hi Jane, campaign note.")
        self.assertEqual(jane[0]["name"], "Jane Example")
        apart = [entry for entry in found if "Which of us did you mean?" in json.dumps(entry["conversation"], default=str)]
        self.assertEqual(len(apart), 1)
        self.assertNotIn("Hi Bob.", json.dumps(apart[0]["conversation"], default=str))
        self.assertNotIn("Hi Carol.", json.dumps(apart[0]["conversation"], default=str))
        self.assertEqual(len(found), 4)   # Jane, Bob, Carol and the unresolved member id

    def test_example_4_an_acknowledged_invitation_shows_sending_until_a_read_confirms_it(self):
        self.open_store(self.root)
        self.action("invite-k", "kim-l", "INVITE", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "Hi Kim, a short note.", claimed=at(5),
                    result={"attempts": [{"started_at": at(5), "outcome": "ACKNOWLEDGED"}]})
        self.db.commit()
        kim = self.entries()[0]
        self.assertEqual(kim["status"], "Sending…")
        self.assertTrue(kim["conversation"][0]["pending"])
        page = self.get()
        self.assertIn("Sending…", page)
        self.assertNotIn("Invited, waiting for acceptance", page)
        self.assertEqual(len(self.entries(show="waiting")), 1)
        with self.db:
            self.db.execute("UPDATE outreach_actions SET state='CONFIRMED_SENT' WHERE action_id='invite-k'")
        self.event("kim-l", "initial_sent", 6, source_id="invite:invite-k", raw={"note": "Hi Kim, a short note."},
                   purpose="INVITE", invitation=True)
        kim = self.entries()[0]
        self.assertEqual(kim["status"], "Invited, waiting for acceptance")
        self.assertEqual([item["state"] for item in kim["conversation"]], ["Sent"])

    def test_example_5_overview_replies_link_lands_on_the_replied_filter(self):
        self.open_store(self.root)
        self.invited("rae-r", "Hi Rae.", 0)
        self.message("rae-r", "CORRESPONDENT", "Sure, let's talk.", 10)
        self.invited("quiet-q", "Hi Quinn.", 1)
        self.db.commit()
        self.assertIn('href="/sent?show=replied_today#conversations"', self.get("/"))
        page = self.get(show="replied")
        self.assertIn('id="conversations"', page)
        self.assertEqual(page.count('class="card net-row"'), 1)
        self.assertIn("Sure, let's talk.", page)
        self.assertNotIn("Hi Quinn.", page)

    # --- the rules ------------------------------------------------------------------------------

    def test_fresh_install_says_why_and_writes_nothing(self):
        page = self.get()
        self.assertIn("No one contacted yet. Outreach starts after research finds contacts", page)
        self.assertNotIn("This page is not built yet.", page)
        self.assertFalse((self.root / "state").exists())

    def test_get_writes_nothing_and_never_fails_on_odd_input(self):
        self.open_store(self.root)
        self.invited("odd-one", "Hi <script>x</script>", 0)
        self.event("odd-two", "initial_sent", 1, source_id="invite:missing", purpose="INVITE", invitation=True)
        with self.db:
            self.db.execute("INSERT INTO engagement_events VALUES ('unipile','bad','linkedin.com/in/odd-three','inbound',?,?,"
                            "'{}','null','{not json')", (at(2), at(2)))
            self.db.execute("INSERT INTO engagement_events VALUES ('unipile','bad2','not a profile','initial_sent',?,?,"
                            "'{}','null','{}')", (at(2), at(2)))
            self.db.execute("INSERT INTO referrals VALUES ('linkedin.com/in/odd-one','SOMETHING_NEW','s','said','next',NULL,?,'[]')",
                            (at(3),))
        self.action("odd-act", "odd-one", "FOLLOWUP", "UNCERTAIN_NO_RETRY", "Hi again", claimed="not a time")
        self.db.close()
        path = self.root / "state" / "orchestrator.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for params in ({}, {"show": "replied"}, {"show": "bogus", "page": "-4"}, {"q": "%_"}, {"page": "x" * 50},
                       {"show": "closed"}, {"q": "odd"}):
            page = self.get(**params)
            self.assertIsNone(RAW_CODE.search(page.split("<main", 1)[1]), params)
        page = self.client().get("/sent").text
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("Referral in progress", html.unescape(page))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["orchestrator.sqlite3"])

    def test_a_junk_or_old_database_shows_the_page(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "orchestrator.sqlite3").write_bytes(os.urandom(4096))
        page = self.get()
        self.assertIn("Sidedoor couldn't read its records.", page)
        self.assertIn("Not available", page)
        (self.root / "state" / "orchestrator.sqlite3").unlink()
        old = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        old.execute("CREATE TABLE engagement_events (identity TEXT, kind TEXT)")
        old.execute("INSERT INTO engagement_events VALUES ('linkedin.com/in/x','inbound')")
        old.commit()
        old.close()
        self.assertIn("Not available", self.get())

    def test_search_filters_and_pages(self):
        self.open_store(self.root)
        for number in range(30):
            self.invited("person-%02d" % number, "Hi number %d." % number, number)
        self.action("invite-z", "zed-q", "INVITE", "CONFIRMED_SENT", "Hi Zed.", company="Zephyr Labs")
        self.event("zed-q", "initial_sent", 40, source_id="invite:invite-z", raw={"note": "Hi Zed."}, purpose="INVITE",
                   invitation=True)
        self.db.commit()
        first = self.view()
        self.assertEqual((first["list"]["total"], first["list"]["pages"], len(first["list"]["contacts"])), (31, 2, 25))
        self.assertEqual(first["list"]["contacts"][0]["name"], "Zed Example")      # newest activity first
        self.assertEqual(len(self.entries(page=2)), 6)
        self.assertEqual([entry["name"] for entry in self.entries(query="zephyr")], ["Zed Example"])
        self.assertEqual([entry["name"] for entry in self.entries(query="ZED")], ["Zed Example"])
        page = self.get(page="2")
        self.assertIn("Page 2 of 2", page)
        self.assertIn('href="/sent">Newer</a>', page)

    def test_closed_and_opted_out_people(self):
        self.open_store(self.root)
        self.invited("gone-g", "Hi Gale.", 0)
        engagement.close_outreach(self.db, url("gone-g"), "INACTIVITY_27_DAYS", at(700))
        self.invited("stop-s", "Hi Stan.", 1)
        self.event("stop-s", "opt_out", 5, source_id="optout:stan")
        self.db.commit()
        statuses = sorted(entry["status"] for entry in self.entries(show="closed"))
        self.assertEqual(statuses, ["Closed", "Opted out"])

    def test_the_summary_reads_through_an_index(self):
        self.open_store(self.root)
        plan = " ".join(str(row[3]) for row in self.db.execute(
            "EXPLAIN QUERY PLAN SELECT kind,identity,COUNT(*),MAX(occurred_at) FROM engagement_events WHERE kind IN (%s) "
            "GROUP BY kind,identity" % ",".join("?" * len(networking_page.KINDS)), networking_page.KINDS))
        self.assertIn("engagement_kind_identity", plan)
        plan = " ".join(str(row[3]) for row in self.db.execute(
            "EXPLAIN QUERY PLAN SELECT name,company FROM rw_contacts WHERE identity IN (?,?)", ("a", "b")))
        self.assertIn("rw_contacts_identity", plan)


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class ReviewFixesTest(Page, unittest.TestCase):
    """The owner-approved review fixes (N1-N18), one focused test each."""

    def test_n1_aliases_are_read_once_and_gather_is_bounded_at_50k_people(self):
        self.open_store(self.root)
        events = []
        for number in range(50000):
            slug = "p%05d" % number
            payload = json.dumps({"kind": "initial_sent", "purpose": "INVITE", "invitation": True, "verified": True,
                                  "source_id": "invite:x" + slug, "raw": {"note": "Hi."}})
            events.append(("unipile", "invite:x" + slug, "linkedin.com/in/" + slug, "initial_sent", at(number / 1000),
                           at(number / 1000), "{}", "null", payload))
        with self.db:
            self.db.executemany("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)", events)
            self.db.executemany("INSERT INTO rw_aliases VALUES ('contact',?,?,'test')",
                                [("linkedin.com/in/p%05d" % number, "linkedin.com/in/p%05d" % (number + 1))
                                 for number in range(0, 1000, 2)])
        self.db.close()
        database = dashboard.read_connection(self.root)
        self.addCleanup(database.close)
        statements = []
        database.set_trace_callback(statements.append)
        started = dt.datetime.now()
        view = networking_page.gather(database, CONFIG)
        elapsed = (dt.datetime.now() - started).total_seconds()
        self.assertEqual(view["list"]["total"], 50000 - 500)          # 500 alias pairs became 500 people
        self.assertLess(len(statements), 600)                          # not one alias read per identity
        self.assertEqual(sum("FROM rw_aliases" in sql for sql in statements), 1)   # the alias closure, read once
        self.assertLess(elapsed, 30)

    def test_n2_member_ids_read_the_sender_links_once_and_skip_what_cannot_be_listed(self):
        self.open_store(self.root)
        self.invited("jane-doe", "Hi Jane.", 0)
        self.event("jane-doe", "accepted", 1, source_id="acceptance:jane", raw={"correspondent_id": "777"})
        for number in range(5):
            self.message("acoaajane%d" % number, "CORRESPONDENT", "Hello %d" % number, 10 + number, chat="c%d" % number,
                         lead="777")
        self.message("acoaastranger", "CORRESPONDENT", "Who is this?", 20, chat="cs", lead="555")
        self.db.commit()
        import outreach
        links, tracked = mock.Mock(wraps=outreach._sender_links), mock.Mock(wraps=outreach.tracked_person)
        with mock.patch.object(outreach, "_sender_links", links), mock.patch.object(outreach, "tracked_person", tracked):
            found = self.entries()
        self.assertEqual(links.call_count, 1)
        asked = [call.args[1] for call in tracked.call_args_list]
        self.assertNotIn("https://linkedin.com/in/acoaastranger", asked)   # tied to nobody listed: not resolved
        self.assertEqual(len(asked), len(set(asked)))                      # each identity asked once
        self.assertEqual(len(found), 1)
        self.assertEqual(sum(item["label"] == "Their message" for item in found[0]["conversation"]), 5)

    def test_n3_research_names_companies_and_reasons_are_found_under_research_spelling(self):
        self.open_store(self.root)
        self.action("invite-r", "rita-r", "INVITE", "CONFIRMED_SENT", "Hi Rita.", recipient=None, company=None)
        self.event("rita-r", "initial_sent", 0, source_id="invite:invite-r", raw={"note": "Hi Rita."}, purpose="INVITE",
                   invitation=True)
        draft = {"recipient": "Rita Research", "company": "Draft Co", "profile": url("rita-r"),
                 "person_evidence": {"employment": {"value": "Staff SRE"},
                                     "ability_to_help": {"value": "Leads hiring for the SRE team"}}}
        with self.db:
            self.db.execute("INSERT INTO rw_drafts VALUES (?,1,'h',?)", ("https://www.linkedin.com/in/rita-r", json.dumps(draft)))
        rita = self.entries()[0]
        self.assertEqual((rita["name"], rita["company"], rita["title"]), ("Rita Research", "Draft Co", "Staff SRE"))
        self.assertIn("Leads hiring for the SRE team", rita["why"])
        with self.db:
            self.db.execute("DELETE FROM rw_drafts")
            self.db.execute("INSERT INTO rw_contacts VALUES ('Contact Co',?,'Rita Contact',0,'plan')",
                            ("https://www.linkedin.com/in/rita-r",))
        rita = self.entries()[0]
        self.assertEqual((rita["name"], rita["company"]), ("Rita Contact", "Contact Co"))
        self.assertIn("Rita Contact", self.get())

    def many_with_a_note_on_page_two(self):
        self.open_store(self.root)
        for number in range(26):
            self.invited("person-%02d" % number, "Hi %d." % number, number + 1)
        self.flag("task-old", "person-00", "Call Pat about the SRE role.")
        with self.db:   # the note is older than every invitation, so person-00 stays on page 2
            self.db.execute("UPDATE reply_owner_flags SET flagged_at=?", (at(0),))
        return self.db.execute("SELECT rowid FROM reply_owner_flags").fetchone()[0]

    def test_n4_n8_mark_done_without_javascript_returns_to_the_same_page_with_a_flash_and_anchor(self):
        note_id = self.many_with_a_note_on_page_two()
        client = self.client()
        page = html.unescape(client.get("/sent", params={"page": "2"}).text)
        self.assertIn('name="back" value="/sent?page=2"', page)
        anchor = re.search(r'name="anchor" value="(contact-[0-9a-f]{16})"', page).group(1)
        self.assertIn('id="%s"' % anchor, page)
        answer = client.post("/sent/notes/done", data={"note_id": str(note_id), "back": "/sent?page=2", "anchor": anchor},
                             headers=ORIGIN)
        self.assertEqual(answer.status_code, 303)
        self.assertEqual(answer.headers["location"], "/sent?page=2&noted=1#" + anchor)
        landed = html.unescape(client.get("/sent?page=2&noted=1").text)
        self.assertIn("Marked done.", landed)
        self.assertIn("Page 2 of 2", landed)
        self.assertNotIn("Call Pat about the SRE role.", landed)

    def test_n5_busy_without_javascript_keeps_the_filter_search_and_page(self):
        self.open_store(self.root)
        self.invited("sam-b", "Hi Sam.", 0)
        self.flag("task-1", "sam-b", "Email Tim.")
        note_id = self.db.execute("SELECT rowid FROM reply_owner_flags").fetchone()[0]
        client = self.client()
        with mock.patch.object(dashboard, "write_connection", side_effect=sqlite3.OperationalError("database is locked")):
            answer = client.post("/sent/notes/done", data={"note_id": str(note_id), "back": "/sent?show=needs_you&q=sam"},
                                 headers=ORIGIN)
        self.assertEqual(answer.status_code, 200)
        page = html.unescape(answer.text)
        self.assertIn(networking_page.BUSY, page)
        self.assertIn('aria-current="true">Needs you', page)
        self.assertIn('value="sam"', page)

    def test_n6_status_order_and_only_shown_inbox_messages_make_messaged(self):
        self.open_store(self.root)
        self.invited("ivy-i", "Hi Ivy.", 0)
        # Our own invitation seen again in the inbox (reconciled), and a reply receipt: neither is "Messaged".
        self.message("ivy-i", "ME", "Hi Ivy.", 0.05, chat="chat-ivy")
        source_id = self.db.execute("SELECT source_id FROM engagement_events WHERE kind='outbound_observed'").fetchone()[0]
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS engagement_outbound_reconciliations (source TEXT NOT NULL, "
                            "source_id TEXT NOT NULL, matched_source TEXT NOT NULL, reconciled_at TEXT NOT NULL, "
                            "PRIMARY KEY(source, source_id))")
            self.db.execute("INSERT INTO engagement_outbound_reconciliations VALUES ('unipile',?,'x',?)", (source_id, at(1)))
        self.assertEqual(self.entries()[0]["status"], "Invited, waiting for acceptance")
        # A message the user wrote by hand: shown, so the person is Messaged, which outranks Invited.
        self.message("ivy-i", "ME", "Ivy, are you hiring?", 5, chat="chat-ivy")
        self.assertEqual(self.entries()[0]["status"], "Messaged")

    def test_n7_filter_counts_follow_the_search(self):
        self.open_store(self.root)
        self.invited("zed-q", "Hi Zed.", 0)
        self.invited("amy-a", "Hi Amy.", 1)
        self.message("amy-a", "CORRESPONDENT", "Hi!", 2)
        counts = self.view(query="zed")["list"]["counts"]
        self.assertEqual((counts["all"], counts["replied"], counts["waiting"]), (1, 0, 1))

    def test_n9_busy_with_htmx_and_the_note_gone_answers_a_note_sized_fragment(self):
        self.open_store(self.root)
        client = self.client()
        with mock.patch.object(dashboard, "write_connection", side_effect=sqlite3.OperationalError("database is locked")):
            answer = client.post("/sent/notes/done", data={"note_id": "999"}, headers=HTMX)
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.text.lstrip().startswith("<article"))
        self.assertNotIn("<!doctype", answer.text.lower())
        self.assertIn(networking_page.BUSY, html.unescape(answer.text))

    def test_n10_overview_waiting_link_lists_exactly_the_invitations_it_counts(self):
        self.open_store(self.root)
        self.action("invite-k", "kim-l", "INVITE", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "Hi Kim.", claimed=at(5))
        self.invited("ann-a", "Hi Ann.", 1)                                   # invited, waiting for acceptance
        self.db.commit()
        overview = self.get("/")
        self.assertIn('href="/sent?show=sending#conversations">1 waiting for confirmation', overview)
        self.assertEqual([entry["name"] for entry in self.entries(show="sending")], ["Kim Example"])
        self.assertEqual(len(self.entries(show="waiting")), 2)

    def test_n11_overview_replies_count_the_people_the_page_lists(self):
        self.open_store(self.root)
        now = dt.datetime.now(dt.timezone.utc)
        self.invited("rae-r", "Hi Rae.", 0)
        for slug in ("rae-r", "cold-stranger"):
            event = engagement.unipile_message_event("chat-" + slug, {"sender": "CORRESPONDENT", "body": "Hello",
                                                                       "createdAt": now.isoformat()},
                                                     url(slug), SENDER, url("me"), now.isoformat())
            engagement.record_event(self.db, event)
        self.db.commit()
        self.assertIn('href="/sent?show=replied_today#conversations">1</a>', self.get("/"))
        self.assertEqual([entry["name"] for entry in self.entries(show="replied_today")], ["Rae Example"])

    def test_n12_uncertain_sends_say_so_and_are_not_waiting(self):
        self.open_store(self.root)
        self.action("follow-u", "una-u", "FOLLOWUP", "UNCERTAIN_NO_RETRY", "Following up.", claimed=at(3))
        with self.db:
            self.db.execute("INSERT INTO outreach_attempts VALUES ('linkedin.com/in/old-o',11,'UNCERTAIN_NO_RETRY',?,NULL)",
                            (at(2),))
        found = {entry["name"]: entry for entry in self.entries()}
        self.assertEqual(found["Una Example"]["status"], "Couldn't confirm it went out")
        self.assertEqual(found["Una Example"]["conversation"][0]["state"], networking_page.UNCERTAIN_WORDS)
        self.assertEqual(found["Someone on LinkedIn"]["status"], "Couldn't confirm it went out")
        self.assertEqual(self.entries(show="waiting"), [])
        self.assertIn("won't send it again", self.get())

    def test_n13_a_refused_message_needs_you(self):
        self.open_store(self.root)
        self.invited("rob-r", "Hi Rob.", 0)
        item = {"action_id": "follow-r", "payload_hash": "h", "recipient": "Rob Example", "company": "Acme",
                "purpose": "FOLLOWUP", "message": "Following up, Rob.", "profile": url("rob-r"), "approved_at": at(4),
                "gates": ["OPT_OUT"]}
        with mock.patch.object(approvals, "refused_after_approval", return_value=[item]):
            found = self.entries(show="needs_you")
            page = self.get(show="needs_you")
        self.assertEqual([entry["name"] for entry in found], ["Rob Example"])
        self.assertIn('class="chip chip-error">Needs you', page)
        self.assertIn("This person asked not to be contacted.", page)

    def test_n14_a_message_that_did_not_go_out_says_what_happens_next(self):
        self.open_store(self.root)
        self.action("invite-n", "ned-n", "INVITE", "CONFIRMED_NOT_SENT", "Hi Ned.", claimed=at(1))
        ned = self.entries()[0]
        self.assertEqual(ned["status"], "Couldn't send")
        self.assertIn("won't invite them again on its own", ned["conversation"][0]["state"])

    def test_n15_a_purpose_that_is_not_text_never_fails_the_page(self):
        self.open_store(self.root)
        self.event("pat-p", "initial_sent", 0, source_id="odd-1", purpose=["INITIAL"])
        self.action("odd-a", "pat-p", "FOLLOWUP", "CONFIRMED_NOT_SENT", "Hi Pat.", claimed=at(1))
        with self.db:
            self.db.execute("UPDATE outreach_actions SET payload=json_set(payload,'$.purpose',json('[\"FOLLOWUP\"]'))")
        item = {"action_id": "x", "payload_hash": "h", "recipient": "Pat", "company": "Acme", "purpose": {"a": 1},
                "message": "m", "profile": url("pat-p"), "approved_at": at(4), "gates": []}
        with mock.patch.object(approvals, "refused_after_approval", return_value=[item]):
            page = self.get()
        self.assertIn("Message", page)

    def test_n16_an_inbox_message_without_text_is_not_hidden_by_a_note_without_text(self):
        self.open_store(self.root)
        self.action("invite-e", "eve-e", "INVITE", "CONFIRMED_SENT", "", claimed=at(0), note="")
        self.event("eve-e", "initial_sent", 0, source_id="invite:invite-e", purpose="INVITE", invitation=True)
        self.event("eve-e", "outbound_observed", 0.01, source_id="seen-1", raw={})
        labels = [item["label"] for item in self.entries()[0]["conversation"]]
        self.assertEqual(labels, ["Invitation (no note)", networking_page.SEEN_IN_INBOX])

    def test_n18_a_date_out_of_range_is_not_a_time(self):
        self.assertIsNone(overview_page._utc("0001-01-01T00:00:00+14:00"))
        self.open_store(self.root)
        self.action("odd-t", "tim-t", "FOLLOWUP", "UNCERTAIN_NO_RETRY", "Hi Tim.", claimed="0001-01-01T00:00:00+14:00")
        self.assertIn("Tim Example", self.get())


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class CalendarEdgeTest(Page, unittest.TestCase):
    """NL1: a UTC time at the calendar's ends cannot be shown in local time; it is no time, never a 500."""

    EDGES = ("0001-01-01T00:00:00Z", "9999-12-31T23:59:00Z")

    def test_nl1_edge_times_are_no_time(self):
        tokyo, pacific = ZoneInfo("Asia/Tokyo"), ZoneInfo("America/Los_Angeles")
        now = dt.datetime.now(dt.timezone.utc)
        for text in self.EDGES:
            self.assertIsNone(overview_page._utc(text), text)
        # Raw times that overflow in that zone (west of UTC for year 1, east of it for year 9999).
        for text, zone in ((self.EDGES[0], pacific), (self.EDGES[1], tokyo)):
            raw = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            self.assertIsNone(overview_page.when(raw, now, zone), (text, zone))

    def test_nl1_the_page_shows_a_contact_with_edge_times(self):
        self.open_store(self.root)
        for number, text in enumerate(self.EDGES):
            self.action("edge-%d" % number, "edge-%d" % number, "FOLLOWUP", "UNCERTAIN_NO_RETRY", "Hi.", claimed=text)
        for zone in ("Asia/Tokyo", "America/Los_Angeles"):
            with mock.patch.dict(CONFIG, {"timezone": zone}):
                page = self.get()
            self.assertIn("Edge Example", page)
            self.assertNotIn(">None<", page)


class PhoneWidthTest(unittest.TestCase):
    def test_networking_rules_fit_a_phone(self):
        css = CSS.read_text()
        rules = css[css.index("/* Networking & Referrals (E6.T5)"):]
        for needed in (".net-filters { display: flex; flex-wrap: wrap;", "overflow-wrap: anywhere", "min-width: 0",
                       "@media (max-width: 640px)", "white-space: pre-wrap"):
            self.assertIn(needed, rules)


if __name__ == "__main__":
    unittest.main()
