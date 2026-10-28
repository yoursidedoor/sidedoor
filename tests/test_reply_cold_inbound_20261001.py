"""Owner, 2026-10-01: "cold strangers should be fully skipped. Dont generate draft for me. People
I've answered by hand who are also being tracked in the harness can get auto replies in the
future." """

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import sqlite3
import unittest
from unittest.mock import patch

import engagement
import manual_reply_execution as manual
import orchestrator
import outreach
import pipeline_routing as routing
import reply_execution
import referrals
import reply_worker
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


STRANGER = "https://www.linkedin.com/in/cold-recruiter"
TRACKED = "https://www.linkedin.com/in/research-contact"


def answer_by_hand(database, profile, thread):
    """The owner's own message in the thread, an hour before the person's inbound message."""
    engagement.record_event(database, {
        "source": "heyreach", "source_id": "owner-hand-" + thread, "profile": profile, "kind": "outbound_observed",
        "raw": {"conversation_id": thread, "message": {"sender": "ME", "body": "Happy to chat.",
                                                       "createdAt": "2026-09-17T15:00:00+00:00"}},
        "occurred_at": "2026-09-17T15:00:00+00:00", "observed_at": "2026-09-17T15:00:00+00:00",
        "evidence": "owner-hand-fixture"})


def track(database, profile):
    """A research contact on file (rw_contacts), never contacted by the harness."""
    database.execute("CREATE TABLE IF NOT EXISTS rw_contacts (company TEXT, identity TEXT, name TEXT, historical INTEGER, plan_id TEXT)")
    database.execute("INSERT INTO rw_contacts VALUES ('mock company',?,'Research Contact',0,NULL)", (profile,))
    database.commit()


class ColdStrangersAreSkipped(unittest.TestCase):
    def assert_cold_skipped(self, fixture):
        with fixture.clock(), patch.object(reply_worker, "execute_text") as execute:
            result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
        execute.assert_not_called()
        self.assertEqual([("COLD_INBOUND_SKIPPED", reply_worker.COLD_BLOCKER)],
                         [(item["state"], item["blocker"]) for item in result["items"]])
        task = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
        self.assertEqual(("CANCELLED", reply_worker.COLD_BLOCKER, 0), (task["status"], task["blocker"], task["attempts"]))
        source = routing.one(fixture.database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?",
                             (json.loads(task["inputs"])[0]["source_key"],))
        self.assertEqual(("ROUTED", "cold_inbound"), (source["status"], json.loads(source["result"])["review_closed_by"]))
        for table in ("worker_compute_reservations", "reply_worker_attempts", "reply_worker_results", "notification_outbox"):
            self.assertEqual(0, fixture.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], table)
        # Their inbound's own stop on outreach stays.
        self.assertEqual("REPLY_REQUIRES_CONTEXT", engagement.stop_state(fixture.database, STRANGER))
        handed = engagement.handoff_reviews(fixture.database, fixture.config, fixture.now)
        self.assertEqual(["HANDED_OFF"], [item["state"] for item in handed["items"]])

    def test_cold_inbound_is_closed_before_the_reply_ai(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(profile=STRANGER, thread="thread-cold")
            self.assertIsNone(outreach.tracked_person(fixture.database, STRANGER))
            self.assert_cold_skipped(fixture)

    def test_untracked_person_the_owner_answered_by_hand_is_still_skipped(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            answer_by_hand(fixture.database, STRANGER, "thread-cold")
            fixture.seed(profile=STRANGER, thread="thread-cold")
            self.assertIsNone(outreach.reply_conversation(fixture.database, STRANGER))
            self.assert_cold_skipped(fixture)


class TrackedPeopleTheOwnerAnsweredGetReplies(unittest.TestCase):
    def test_tracked_hand_answered_person_is_promoted(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            track(fixture.database, TRACKED)
            answer_by_hand(fixture.database, TRACKED, "thread-tracked")
            # HeyReach names the correspondent (after the fixture's own history rows are written).
            with patch.dict(manual.NAMES, {TRACKED: "Research Contact"}):
                fixture.seed(profile=TRACKED, thread="thread-tracked")
                self.assertEqual("rw_contacts", outreach.tracked_person(fixture.database, TRACKED))
                self.assertIsNone(outreach.managed_conversation(fixture.database, TRACKED))
                self.assertEqual("TRACKED_OWNER_ANSWERED", outreach.reply_conversation(fixture.database, TRACKED))
                fixture.classify()
                promoted = reply_execution.promote(fixture.database, fixture.config, fixture.now)
                self.assertEqual(["PENDING"], [item["state"] for item in promoted["items"]], promoted)
                fixture.cycle()
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("ACKNOWLEDGED_UNCONFIRMED", fixture.actions()[0]["state"])

    def test_tracked_person_without_a_reply_basis_is_skipped_before_the_reply_ai(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            track(fixture.database, TRACKED)
            fixture.seed(profile=TRACKED, thread="thread-tracked")
            self.assertIsNone(outreach.reply_conversation(fixture.database, TRACKED))
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute:
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([("NO_REPLY_BASIS_SKIPPED", reply_worker.NO_BASIS_BLOCKER)],
                             [(item["state"], item["blocker"]) for item in result["items"]])
            task = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
            source = routing.one(fixture.database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?",
                                 (json.loads(task["inputs"])[0]["source_key"],))
            self.assertEqual(("CANCELLED", "ROUTED", "no_reply_basis"),
                             (task["status"], source["status"], json.loads(source["result"])["review_closed_by"]))
            self.assertEqual(0, fixture.database.execute("SELECT COUNT(*) FROM reply_worker_attempts").fetchone()[0])

    def test_bare_and_url_profiles_agree(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            self.assertEqual("OWNER_CONTACTED", outreach.reply_conversation(fixture.database, manual.PROFILE))
            self.assertEqual("OWNER_CONTACTED", outreach.reply_conversation(fixture.database, "linkedin.com/in/mock-contact"))


MEMBER_ID = "https://www.linkedin.com/in/ACoAAB1234567890abcdefXYZ"


def with_lead(lead_id):
    """manual.inbound_message, with the HeyReach member number the inbox reader records."""
    original = manual.inbound_message

    def build(*arguments, **keywords):
        event = original(*arguments, **keywords)
        event["raw"]["provider_lead_id"] = event["raw"]["correspondent_id"] = lead_id
        return event
    return patch.object(manual, "inbound_message", build)


def harness_invitation(database, profile, lead_id):
    """Our own launch receipt for this person, carrying HeyReach's lead object."""
    outreach.initialize(database)
    database.execute("INSERT INTO outreach_attempts (identity,campaign_id,state,claimed_at,result) VALUES (?,?,?,?,?)",
                     (orchestrator.profile_key(profile), 4242, "LAUNCHED", manual.BASE.isoformat(),
                      json.dumps({"leads": {"items": [{"id": "9", "linkedin_id": lead_id, "profileUrl": profile}]}})))
    database.commit()


class MemberIdSpellings(unittest.TestCase):
    def test_member_id_sender_resolved_by_lead_id_is_answered_as_the_tracked_person(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            harness_invitation(fixture.database, TRACKED, "777")
            with with_lead("777"):
                fixture.seed(profile=MEMBER_ID, thread="thread-member")
            resolved = outreach.resolve_sender(fixture.database, MEMBER_ID, "777", "thread-member")
            self.assertEqual(("TRACKED", "linkedin.com/in/research-contact"), (resolved["state"], resolved["person"]))
            fixture.classify()      # not skipped: the reply AI runs
            self.assertEqual("HARNESS_OUTREACH",
                             outreach.reply_conversation(fixture.database, MEMBER_ID, "777", "thread-member"))
            promoted = reply_execution.promote(fixture.database, fixture.config, fixture.now)
            self.assertEqual(["PENDING"], [item["state"] for item in promoted["items"]], promoted)
            action = json.loads(fixture.actions()[0]["payload"])
            self.assertEqual("https://linkedin.com/in/research-contact", action["resolved_profile"])
            # The resolved person's own opt-out stops the reply.
            engagement.record_event(fixture.database, {
                "source": "heyreach", "source_id": "opt-out", "profile": TRACKED, "kind": "opt_out", "raw": {},
                "occurred_at": manual.BASE.isoformat(), "observed_at": manual.BASE.isoformat(), "evidence": "fixture"})
            self.assertIn("OPTED_OUT", reply_execution.stop_gates(fixture.database, fixture.config, action))

    def test_member_id_sender_resolved_by_conversation(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            track(fixture.database, TRACKED)
            answer_by_hand(fixture.database, TRACKED, "thread-member")
            resolved = outreach.resolve_sender(fixture.database, MEMBER_ID, None, "thread-member")
            self.assertEqual(("TRACKED", "linkedin.com/in/research-contact"), (resolved["state"], resolved["person"]))
            self.assertEqual("TRACKED_OWNER_ANSWERED",
                             outreach.reply_conversation(fixture.database, MEMBER_ID, None, "thread-member"))

    def test_unmatched_member_id_is_held_not_skipped(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            with with_lead("555"):
                fixture.seed(profile=MEMBER_ID, thread="thread-member")
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute:
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([("SENDER_IDENTITY_UNRESOLVED", reply_worker.UNRESOLVED_BLOCKER)],
                             [(item["state"], item["blocker"]) for item in result["items"]])
            task = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
            self.assertEqual(("BLOCKED", reply_worker.UNRESOLVED_BLOCKER, 0), (task["status"], task["blocker"], task["attempts"]))
            source = routing.one(fixture.database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?",
                                 (json.loads(task["inputs"])[0]["source_key"],))
            self.assertNotEqual("ROUTED", source["status"])
            self.assertEqual(0, fixture.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0])

    def test_held_sender_is_rechecked_and_released_once_it_resolves(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            with with_lead("555"):
                fixture.seed(profile=MEMBER_ID, thread="thread-member")
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute:
                orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
                held = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
                self.assertEqual([MEMBER_ID.split("://www.")[1].casefold()],
                                 [entry["sender"] for entry in reply_worker.held_senders(fixture.database)])
                # Still unresolved: left exactly as it is.
                again = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([], again["items"])
            self.assertEqual(held["updated_at"], routing.one(
                fixture.database, "SELECT updated_at FROM workflow_tasks WHERE task_id=?", (held["task_id"],))["updated_at"])
            # Our launch receipt for that member number now ties the sender to a harness contact.
            harness_invitation(fixture.database, TRACKED, "555")
            result = fixture.classify()
            self.assertIn("SENDER_IDENTITY_RESOLVED", [item["state"] for item in result["items"]])
            self.assertEqual("COMPLETE", routing.one(fixture.database, "SELECT status FROM workflow_tasks WHERE task_id=?",
                                                     (held["task_id"],))["status"])
            self.assertEqual([], reply_worker.held_senders(fixture.database))

    def test_unreadable_sender_record_is_held(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(profile=STRANGER, thread="thread-cold")
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute, \
                    patch.object(reply_worker, "_sender", side_effect=ValueError("unreadable")):
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([reply_worker.UNRESOLVED_BLOCKER], [item["blocker"] for item in result["items"]])

    def test_database_error_in_the_skip_check_is_reported_per_task(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(profile=STRANGER, thread="thread-cold")
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute, \
                    patch.object(reply_worker, "_skip_kind", side_effect=sqlite3.OperationalError("locked")):
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual({"SKIP_CHECK_FAILED"}, {item["state"] for item in result["items"]})

    def test_resolved_person_supplies_context_and_alert_check(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            harness_invitation(fixture.database, TRACKED, "777")
            referrals.initialize(fixture.database)
            referrals.record(fixture.database, "linkedin.com/in/research-contact", "AGREED", "earlier-source",
                             "Agreed to refer", "Send the summary", None, manual.BASE.isoformat(), [])
            fixture.database.commit()
            with with_lead("777"):
                fixture.seed(profile=MEMBER_ID, thread="thread-member")
            task = routing.one(fixture.database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
            request = reply_worker._source(fixture.database, task, fixture.config, fixture.now.isoformat())[0]
            self.assertEqual(["earlier-source"], [row["source_key"] for row in request["referral_context"]])
            # The member-id spelling itself was never contacted; the resolved person was.
            self.assertFalse(reply_worker.contacted(fixture.database, MEMBER_ID))
            event = reply_worker.notify_failed_attempt(fixture.database, task, None, "SOME_BLOCKER", "BLOCKED",
                                                       fixture.now.isoformat())
            self.assertIsNotNone(event)

    def test_events_in_the_same_conversation_count_for_newer_activity(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            answer_by_hand(fixture.database, TRACKED, "thread-shared")
            action = {"profile": MEMBER_ID, "conversation_id": "thread-shared"}
            self.assertEqual(["owner-hand-thread-shared"],
                             [event["source_id"] for event in reply_execution.conversation_events(fixture.database, action)])
            action = {"profile": MEMBER_ID, "resolved_profile": TRACKED, "conversation_id": "other"}
            self.assertEqual(["owner-hand-thread-shared"],
                             [event["source_id"] for event in reply_execution.conversation_events(fixture.database, action)])

    def test_several_tracked_people_on_one_lead_is_unresolved(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            harness_invitation(fixture.database, TRACKED, "888")
            fixture.database.execute("INSERT INTO outreach_attempts (identity,campaign_id,state,claimed_at,result) VALUES (?,?,?,?,?)",
                                      ("linkedin.com/in/second-contact", 4343, "LAUNCHED", manual.BASE.isoformat(),
                                       json.dumps({"leads": {"items": [{"linkedin_id": "888",
                                                                        "profileUrl": "https://www.linkedin.com/in/second-contact"}]}})))
            resolved = outreach.resolve_sender(fixture.database, MEMBER_ID, "888", None)
            self.assertEqual(("UNRESOLVED", "SENDER_MATCHES_SEVERAL_TRACKED_PEOPLE"), (resolved["state"], resolved["basis"]))

    def test_lead_object_profile_url_wins_over_the_record_owner(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            outreach.initialize(fixture.database)
            # Filed under another identity, but the lead object names the research contact.
            fixture.database.execute("INSERT INTO outreach_attempts (identity,campaign_id,state,claimed_at,result) VALUES (?,?,?,?,?)",
                                      ("linkedin.com/in/record-owner", 4444, "LAUNCHED", manual.BASE.isoformat(),
                                       json.dumps({"leads": {"items": [{"linkedin_id": "999", "profileUrl": TRACKED}]}})))
            track(fixture.database, TRACKED)
            self.assertEqual({"linkedin.com/in/research-contact"}, outreach._sender_links(fixture.database)[("lead", "999")])
            resolved = outreach.resolve_sender(fixture.database, MEMBER_ID, "999", None)
            self.assertEqual(("TRACKED", "linkedin.com/in/research-contact"), (resolved["state"], resolved["person"]))

    def test_hand_answer_under_the_member_id_counts_for_the_resolved_person(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            track(fixture.database, TRACKED)       # tracked, not managed
            # An earlier inbound filed under the vanity URL carries the member number.
            engagement.record_event(fixture.database, {
                "source": "heyreach", "source_id": "vanity-inbound", "profile": TRACKED, "kind": "inbound",
                "raw": {"conversation_id": "thread-vanity", "provider_lead_id": "321"},
                "occurred_at": "2026-09-17T14:00:00+00:00", "observed_at": "2026-09-17T14:00:00+00:00",
                "evidence": "fixture"})
            self.assertIsNone(outreach.reply_conversation(fixture.database, MEMBER_ID, "321", "thread-member"))
            answer_by_hand(fixture.database, MEMBER_ID, "thread-member")
            self.assertEqual("TRACKED_OWNER_ANSWERED",
                             outreach.reply_conversation(fixture.database, MEMBER_ID, "321", "thread-member"))

    def test_member_id_sender_is_sent_end_to_end(self):
        # Promotion writes the reply's own action and claim under the member-id spelling; the
        # resolved person stays pinned through the send (review, 2026-10-01).
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            harness_invitation(fixture.database, TRACKED, "777")
            with with_lead("777"), patch.dict(manual.NAMES, {MEMBER_ID: "Research Contact"}):
                fixture.seed(profile=MEMBER_ID, thread="thread-member")
                fixture.classify()
                promoted = reply_execution.promote(fixture.database, fixture.config, fixture.now)
                self.assertEqual(["PENDING"], [item["state"] for item in promoted["items"]], promoted)
                # After promotion our records change so that re-deriving would no longer give the
                # same person (a second contact on the same member number): the pinned one is used.
                fixture.database.execute("INSERT INTO outreach_attempts (identity,campaign_id,state,claimed_at,result) VALUES (?,?,?,?,?)",
                                          ("linkedin.com/in/second-contact", 4343, "LAUNCHED", manual.BASE.isoformat(),
                                           json.dumps({"leads": {"items": [{"linkedin_id": "777",
                                                                            "profileUrl": "https://www.linkedin.com/in/second-contact"}]}})))
                fixture.database.commit()
                self.assertEqual("UNRESOLVED", outreach.resolve_sender(fixture.database, MEMBER_ID, "777", "thread-member")["state"])
                fixture.cycle()
            self.assertEqual(1, len(fixture.requests), fixture.actions())
            payload = json.loads(fixture.actions()[0]["payload"])
            self.assertEqual("https://linkedin.com/in/research-contact", payload["resolved_profile"])
            self.assertEqual("ACKNOWLEDGED_UNCONFIRMED", fixture.actions()[0]["state"])
            # The reply's own action, claim and sent-suppression rows never make the spelling
            # tracked: it is still unresolved (two contacts), not taken for a tracked sender.
            self.assertIsNone(outreach.tracked_person(fixture.database, MEMBER_ID))

    def test_missing_or_unkeyable_identity_is_unresolved_not_cold(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            for profile in (None, "", "https://example.com/not-linkedin"):
                self.assertEqual("UNRESOLVED", outreach.resolve_sender(fixture.database, profile)["state"], profile)
            self.assertEqual("COLD", outreach.resolve_sender(fixture.database, STRANGER)["state"])


OTHER_SPELLING = "linkedin.com/in/mock-contact-other-spelling"


def record_raw(database, source_id, identity, kind, stamp, raw, **extra):
    event = {"source": "heyreach", "source_id": source_id, "profile": "https://" + identity, "kind": kind,
             "occurred_at": stamp, "observed_at": stamp, "evidence": "synthetic", "verified": True, "raw": raw, **extra}
    database.execute("INSERT INTO engagement_events VALUES (?,?,?,?,?,?,?,?,?)",
                     ("heyreach", source_id, identity, kind, stamp, stamp, json.dumps(raw), "null", json.dumps(event)))
    database.commit()


class AlreadyAnsweredAndNewerActivity(unittest.TestCase):
    def test_same_conversation_answer_under_another_spelling_stands_the_reply_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.classify()
            reply_execution.promote(fixture.database, fixture.config, fixture.now)
            action = json.loads(fixture.actions()[0]["payload"])
            self.assertEqual([], reply_execution.binding_gates(fixture.database, fixture.config, action, fixture.now))
            record_raw(fixture.database, "ours-elsewhere", OTHER_SPELLING, "outbound_observed", "2026-09-17T16:05:00+00:00",
                       {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Answered by hand",
                                                                   "createdAt": "2026-09-17T16:05:00+00:00"}})
            self.assertEqual(["REPLY_NEWER_CONVERSATION_ACTIVITY"],
                             reply_execution.binding_gates(fixture.database, fixture.config, action, fixture.now))

    def test_same_conversation_newer_inbound_under_another_spelling_stands_a_retry_down(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.provider_denied = True
            fixture.cycle()
            fixture.provider_denied = False
            self.assertEqual("UNCERTAIN_NO_RETRY", fixture.actions()[0]["state"])
            record_raw(fixture.database, "newer-elsewhere", OTHER_SPELLING, "inbound", "2026-09-17T16:00:05+00:00",
                       {"conversation_id": "thread-1", "message": {"sender": "CORRESPONDENT", "body": "Something else"}})
            fixture.cycle(seconds=601)
            self.assertEqual(1, len(fixture.requests))
            self.assertEqual("REPLY_NEWER_CONVERSATION_ACTIVITY", json.loads(fixture.actions()[0]["result"])["reply_terminal"])

    def test_late_observed_invitation_is_not_an_answer(self):
        # Elio, 2026-10-01: invitation sent 14:04 (provider time), initial_sent recorded 17:21
        # (when reconciliation observed it). A reply between the two is still answered.
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            identity = "linkedin.com/in/mock-contact"
            record_raw(fixture.database, "invitation-observed", identity, "outbound_observed", "2026-09-17T15:00:00+00:00",
                       {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Invitation note",
                                                                   "createdAt": "2026-09-17T15:00:00+00:00"}})
            fixture.seed()       # inbound at 16:00
            record_raw(fixture.database, "initial-sent", identity, "initial_sent", "2026-09-17T16:00:09+00:00",
                       {"campaign": {"id": 1}, "message": "Invitation note"})
            snapshot = {"identity": identity, "occurred_at": "2026-09-17T16:00:00+00:00", "source": "heyreach",
                        "source_id": "message-1"}
            action = {"profile": identity, "conversation_id": "thread-1", "reply_inbound_at": snapshot["occurred_at"],
                      "reply_source": "heyreach", "reply_source_id": "message-1"}
            self.assertIsNone(reply_execution.newer_activity(fixture.database, action))
            self.assertEqual("REPLY", reply_worker._reply_state(fixture.database, identity, None, "thread-1", snapshot))
            fixture.cycle()
            self.assertEqual(1, len(fixture.requests))

    def answered(self, fixture):
        action = {"profile": "linkedin.com/in/mock-contact", "conversation_id": "thread-1",
                  "reply_inbound_at": "2026-09-17T16:00:00+00:00", "reply_source": "heyreach", "reply_source_id": "message-1"}
        return reply_execution.newer_activity(fixture.database, action)

    def test_first_direct_message_counts_as_our_answer_and_the_invitation_does_not(self):
        identity = "linkedin.com/in/mock-contact"
        for purpose, expected in (("FIRST_MESSAGE", "REPLY_NEWER_CONVERSATION_ACTIVITY"), ("INITIAL", None)):
            with self.subTest(purpose=purpose), manual.isolated_boundaries(), manual.Fixture() as fixture:
                fixture.seed()
                record_raw(fixture.database, "first-dm", identity, "initial_sent", "2026-09-17T16:02:00+00:00",
                           {"conversation_id": "thread-1", "message": "Thanks for connecting"}, purpose=purpose)
                self.assertEqual(expected, self.answered(fixture))
                # Its inbox observation, reconciled to that send, decides the same way.
                fixture.database.execute("DELETE FROM engagement_events WHERE source_id='first-dm'")
                record_raw(fixture.database, "first-dm", identity, "initial_sent", "2026-09-17T15:00:00+00:00",
                           {"conversation_id": "thread-1", "message": "Thanks for connecting"}, purpose=purpose)
                record_raw(fixture.database, "first-dm-observed", identity, "outbound_observed", "2026-09-17T16:02:00+00:00",
                           {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Thanks for connecting",
                                                                       "createdAt": "2026-09-17T16:02:00+00:00"}})
                fixture.database.execute("""CREATE TABLE IF NOT EXISTS engagement_outbound_reconciliations (
                    source TEXT NOT NULL, source_id TEXT NOT NULL, matched_source TEXT NOT NULL,
                    reconciled_at TEXT NOT NULL, PRIMARY KEY(source, source_id))""")
                fixture.database.execute("INSERT INTO engagement_outbound_reconciliations VALUES (?,?,?,?)",
                                         ("heyreach", "first-dm-observed", 'engagement_events:["heyreach", "first-dm"]',
                                          "2026-09-17T16:03:00+00:00"))
                self.assertEqual(expected, self.answered(fixture))

    def test_send_time_boundary_fallback_and_invitation_message_type(self):
        identity = "linkedin.com/in/mock-contact"
        cases = (
            # exactly at the inbound time counts (>=)
            ("at-boundary", {"createdAt": "2026-09-17T16:00:00+00:00"}, "2026-09-17T17:00:00+00:00", {}, True),
            # provider time before the inbound wins over a later observation time
            ("sent-before", {"createdAt": "2026-09-17T15:59:59+00:00"}, "2026-09-17T17:00:00+00:00", {}, False),
            # no provider time: the event time is used
            ("fallback-after", {}, "2026-09-17T16:00:01+00:00", {}, True),
            ("fallback-before", {}, "2026-09-17T15:59:00+00:00", {}, False),
            # the observed invitation note never answers
            ("invitation", {"createdAt": "2026-09-17T16:05:00+00:00"}, "2026-09-17T16:05:00+00:00",
             {"inbox": {"message_type": "invitation"}}, False),
        )
        for name, times, occurred, extra, expected in cases:
            with self.subTest(case=name), manual.isolated_boundaries(), manual.Fixture() as fixture:
                fixture.seed()
                record_raw(fixture.database, name, identity, "outbound_observed", occurred,
                           {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Ours", **times}}, **extra)
                self.assertEqual("REPLY_NEWER_CONVERSATION_ACTIVITY" if expected else None, self.answered(fixture))

    def test_message_already_answered_by_hand_is_skipped_before_the_reply_ai(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            record_raw(fixture.database, "hand-answer", "linkedin.com/in/mock-contact", "outbound_observed",
                       "2026-09-17T16:01:00+00:00",
                       {"conversation_id": "thread-1", "message": {"sender": "ME", "body": "Answered by hand",
                                                                   "createdAt": "2026-09-17T16:01:00+00:00"}})
            with fixture.clock(), patch.object(reply_worker, "execute_text") as execute:
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([("ALREADY_ANSWERED_SKIPPED", reply_worker.ANSWERED_BLOCKER)],
                             [(item["state"], item["blocker"]) for item in result["items"]])


if __name__ == "__main__":
    unittest.main()
