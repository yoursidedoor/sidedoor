"""Profile-anchored reply exclusion, and the protections it must not weaken.

`pipeline_routing._reply_excluded` is the only exclusion gate between a
contextual reply decision and a recorded referral agreement, and it is also the
gate `reply_execution.binding_gates` calls (with `bindings=[]`) immediately
before a REPLY send. It resolved the correspondent's name only from `history`
rows carrying a profile, and only in `research_workflow.name_key`'s word-split
spelling. Suppression rows are written in `orchestrator.name_key`'s display
spelling, and a name may be bound durably in `rw_contacts`, `outreach_queue`,
`rw_drafts` or a draft payload with no `history` row at all. Either way the
name set came back empty or wrongly spelled, the seeded exclusion literals
could not match, the `name:` suppression expansion covered nothing, and an
explicitly excluded correspondent was waved through.

These tests use the shapes the production database actually holds: research
contact records stored under `research_workflow.profile_key`'s absolute
identity with no `history` companion, and `Existing user exclusion` rows that
bind to no profile at all.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import applications
import conversion
import engagement
import orchestrator
import outreach
import pipeline_routing as routing
import reply_execution
import reply_worker
import research_workflow as research


NOW = "2026-09-17T16:00:00+00:00"
LIVE_NAME_ONLY_EXCLUSIONS = ("Ana Casey Moreau", "Casey Moreau",
                             "casey.moreau@moneypenny.example", "Taylor Quinn", "Robert Vance")
REQUEST = {"job_scope": [], "source_snapshot": {"source": "heyreach", "source_id": "message-1"}}


def memory():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    for module in (applications, research, engagement, conversion, routing, reply_worker, outreach):
        module.initialize(database)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            company TEXT NOT NULL, profile TEXT, evidence TEXT NOT NULL);""")
    return database


def exclude(database, recipient, reason="Existing user exclusion"):
    """Exactly the row orchestrator.py:163,175 writes from the pipeline rules."""
    database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                     ("name:" + orchestrator.name_key(recipient), reason))


class ReplyExclusionResolutionTests(unittest.TestCase):
    """`bindings=[]` -- the exact shape reply_execution.binding_gates uses."""

    def setUp(self):
        self.database = memory()
        self.addCleanup(self.database.close)

    def contact(self, profile, name, company="vandelay security", historical=1):
        """A research contact row, stored as production stores it: the absolute
        `research_workflow.profile_key` identity, not the bare identity key."""
        self.database.execute("INSERT INTO rw_contacts VALUES (?,?,?,?,NULL)",
                              (company, research.profile_key(profile), research.name_key(name), historical))

    def excluded(self, profile):
        return routing._reply_excluded(self.database, research.profile_key(profile), [])

    def test_name_bound_only_to_a_research_contact_record_is_resolved(self):
        profile = "https://www.linkedin.com/in/dana-brook-11111a111"
        self.contact(profile, "Dana Brook", historical=0)
        exclude(self.database, "Dana Brook")
        self.assertTrue(self.excluded(profile))

    def test_seeded_literal_bound_only_to_a_research_contact_record(self):
        profile = "https://www.linkedin.com/in/rob-vance-1a2b3c4"
        self.contact(profile, "Rob Vance", company="moneypenny")
        self.assertTrue(self.excluded(profile))

    def test_display_name_punctuation_is_not_lost_by_the_word_split_key(self):
        profile = "https://www.linkedin.com/in/siobh"
        self.database.execute("INSERT INTO history VALUES (?,?,?,?,'{}')",
                              ("name:siobhán o neill", "Siobhán O'Neill", "litware", profile + "/"))
        exclude(self.database, "Siobhán O'Neill")
        self.assertEqual("name:siobhán o'neill",
                         self.database.execute("SELECT identity FROM suppressions").fetchone()[0])
        self.assertNotEqual(orchestrator.name_key("Siobhán O'Neill"), research.name_key("Siobhán O'Neill"))
        self.assertTrue(self.excluded(profile))

    def test_name_bound_through_the_legacy_queue_is_resolved(self):
        profile = "https://www.linkedin.com/in/lferri"
        self.database.execute("INSERT INTO outreach_queue VALUES (?,?,?,?,?,?,NULL)",
                              (orchestrator.profile_key(profile), 700001, "Luca Ferri", "sent",
                               "PENDING_VALIDATION", json.dumps({"recipient": "Luca Ferri", "profile": profile})))
        exclude(self.database, "Luca Ferri")
        self.assertTrue(self.excluded(profile))

    def test_unresolvable_identity_fails_closed_against_name_only_exclusions(self):
        """The live shape: five name-only exclusions binding to no profile."""
        for recipient in LIVE_NAME_ONLY_EXCLUSIONS:
            exclude(self.database, recipient)
        profile = "https://www.linkedin.com/in/morgan-hale"
        self.assertEqual(routing.REPLY_IDENTITY_UNRESOLVED, routing._reply_exclusion(self.database, profile, []))
        with self.assertRaises(ValueError) as raised:
            self.excluded(profile)
        self.assertEqual("REPLY_RECIPIENT_IDENTITY_UNRESOLVED", str(raised.exception))

    def test_profile_shaped_stored_name_is_not_mistaken_for_a_resolved_identity(self):
        """The live `outreach_actions` row stores the profile key as the name."""
        profile = "https://www.linkedin.com/in/morgan-hale"
        self.database.execute("INSERT INTO history VALUES (?,?,?,?,'{}')",
                              ("name:linkedin.com/in/morgan-hale", "linkedin.com/in/morgan-hale",
                               "unknown", profile + "/"))
        for recipient in LIVE_NAME_ONLY_EXCLUSIONS:
            exclude(self.database, recipient)
        self.assertEqual(routing.REPLY_IDENTITY_UNRESOLVED, routing._reply_exclusion(self.database, profile, []))

    def test_binding_gates_defer_an_unnamed_recipient_to_the_heyreach_name(self):
        """Since 2026-09-30 the name-keyed check waits for inspection's HeyReach name."""
        for recipient in LIVE_NAME_ONLY_EXCLUSIONS:
            exclude(self.database, recipient)
        action = {"profile": "https://www.linkedin.com/in/morgan-hale", "sender_id": 424242,
                  "reply_task_id": "task-1", "action_id": "reply:mock"}
        with patch.object(reply_execution, "_action", return_value=action), \
                patch.object(reply_execution, "context", return_value=(REQUEST, None, None)):
            gates = reply_execution.binding_gates(self.database, {"sender_id": 424242}, action, dt.datetime.now(dt.timezone.utc))
        self.assertNotIn("REPLY_RECIPIENT_IDENTITY_UNRESOLVED", gates)

    def test_binding_gates_block_the_send_to_an_excluded_recipient(self):
        """The send path itself: an `Existing user exclusion` whose name is
        bound only to the durable research contact record, the exact shape the
        production database holds for every unattributable exclusion."""
        profile = "https://www.linkedin.com/in/dana-brook-11111a111"
        self.contact(profile, "Dana Brook", historical=0)
        exclude(self.database, "Dana Brook")
        action = {"profile": profile, "sender_id": 424242, "reply_task_id": "task-1", "action_id": "reply:mock",
                  "reply_inbound_at": "2026-10-01T00:00:00+00:00"}
        with patch.object(reply_execution, "_action", return_value=action), \
                patch.object(reply_execution, "context", return_value=(REQUEST, None, None)):
            gates = reply_execution.binding_gates(self.database, {"sender_id": 424242}, action, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(["REPLY_EXCLUDED_OR_SUPPRESSED"], gates,
                         "The send path did not block an explicitly excluded recipient")

    def test_opt_out_still_blocks(self):
        profile = "https://www.linkedin.com/in/mock-contact"
        self.contact(profile, "Mock Contact")
        self.database.execute("INSERT INTO engagement_stops VALUES (?,'OPTED_OUT',?,?)",
                              (orchestrator.profile_key(profile), NOW, "mock-opt-out"))
        self.assertTrue(self.excluded(profile))

    def test_profile_keyed_exclusion_still_blocks(self):
        profile = "https://www.linkedin.com/in/mock-contact"
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              (orchestrator.profile_key(profile), "Existing user exclusion"))
        self.assertTrue(self.excluded(profile))

    def test_reply_allowed_history_reasons_are_preserved(self):
        profile = "https://www.linkedin.com/in/jrivera"
        self.database.execute("INSERT INTO history VALUES (?,?,?,?,'{}')",
                              ("name:jamie rivera", "Jamie Rivera", "globodyne", profile + "/"))
        for reason in ("Previously contacted; no repeat initial outreach",
                       "Handoff historical outreach; never repeat initial contact",
                       "LinkedIn reply stops blind outreach: REPLY_REQUIRES_CONTEXT",
                       "Outreach reserved; never automatically retry"):
            with self.subTest(reason=reason):
                self.database.execute("DELETE FROM suppressions")
                self.database.execute("INSERT INTO suppressions VALUES (?,?)", (orchestrator.profile_key(profile), reason))
                self.assertFalse(self.excluded(profile))

    def test_no_false_positive_for_an_ordinary_resolvable_correspondent(self):
        profile = "https://www.linkedin.com/in/nilsberg"
        self.contact(profile, "Nils Berg Hansen", company="wingtip", historical=0)
        self.assertFalse(self.excluded(profile))

    def test_no_false_positive_when_nothing_name_keyed_can_be_missed(self):
        """Empty name-only exclusion space: an unresolvable name is not a hold."""
        profile = "https://www.linkedin.com/in/morgan-hale"
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              (orchestrator.profile_key(profile), "Previously contacted; no repeat initial outreach"))
        self.assertIs(False, routing._reply_excluded(self.database, profile, []))

    def test_someone_elses_exclusion_does_not_block_this_correspondent(self):
        profile = "https://www.linkedin.com/in/nilsberg"
        self.contact(profile, "Nils Berg Hansen", company="wingtip", historical=0)
        self.database.execute("INSERT INTO history VALUES ('name:taylor quinn','Taylor Quinn','other',"
                              "'https://www.linkedin.com/in/taylor-quinn/','{}')")
        exclude(self.database, "Taylor Quinn")
        self.assertIs(False, self.excluded(profile))

    def test_gate_contract_stays_boolean_for_every_exclusion_finding(self):
        """reply_execution.binding_gates consumes this in a boolean test; only
        an unresolvable identity is signalled out of band, because it is a hold
        for investigation, not a finding that the recipient is excluded."""
        profile = "https://www.linkedin.com/in/taylor-quinn"
        self.contact(profile, "Taylor Quinn", company="moneypenny")
        self.assertIs(False, routing._reply_excluded(self.database, "https://www.linkedin.com/in/someone-else", []))
        exclude(self.database, "Taylor Quinn")
        self.assertIs(True, self.excluded(profile))


class OutboundProtectionTests(unittest.TestCase):
    """The shared send gate still refuses every protected recipient class."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        outreach.initialize_actions(self.database)
        research.initialize(self.database)
        self.now = dt.datetime(2026, 9, 21, 17, tzinfo=dt.timezone.utc)
        self.config = {"outreach_mode": "guarded", "outreach_actions_enabled": True, "sender_id": 424242}
        self.profile = "https://www.linkedin.com/in/mock-person/"
        self.action = {"action_id": "initial-1", "profile": self.profile,
            "recipient": "Mock Person", "company": "Mock Company", "purpose": "INITIAL",
            "message": "Verified fixture text", "channel": "linkedin", "provider": "heyreach",
            "conversation_id": "thread-1",
            "verification": {"content_verified": True, "content_sources": ["mock-resume"], "identity_verified": True,
                "identity_source": "mock-identity", "team_relevance_verified": True, "team_source": "mock-team",
                "verified_at": self.now.isoformat()}}
        self.proof = {"identity": "linkedin.com/in/mock-person", "sender_id": 424242, "sender_ready": True,
            "entitlement_verified": True, "direct_messages_complete": True, "sent_invitations_complete": True,
            "pending_invitation": False, "has_reply": False, "negative_reply": False, "opt_out": False,
            "cooldown": False, "checked_at": self.now.isoformat(), "evidence": "mock-direct-history",
            "prior_outreach": False, "company_contact_count": 0, "company_count_evidence": "mock-history",
            "conversation_id": "thread-1", "followups_sent": 0, "initial_message_sent": False}

    def gates(self, **overrides):
        action = dict(self.action, **overrides.pop("action", {}))
        proof = dict(self.proof, **overrides.pop("proof", {}))
        return outreach.action_gates(self.database, self.config, action, proof, self.now)

    def test_clean_recipient_is_not_newly_blocked(self):
        self.assertEqual([], self.gates())

    def test_opt_out_still_blocks(self):
        self.assertIn("OPT_OUT_PRESENT", self.gates(proof={"opt_out": True}))

    def test_stop_on_reply_still_blocks(self):
        self.assertIn("HAS_REPLY_PRESENT", self.gates(proof={"has_reply": True}))
        self.database.execute("INSERT INTO engagement_stops VALUES (?,'REPLY_REQUIRES_CONTEXT',?,?)",
                              (orchestrator.profile_key(self.profile), self.now.isoformat(), "mock-reply"))
        self.assertIn("REPLY_REQUIRES_CONTEXT", self.gates())

    def test_closed_recipient_still_blocks(self):
        engagement.close_outreach(self.database, self.profile, "27-day inactivity", self.now)
        self.assertIn("OUTREACH_CLOSED", self.gates())

    def test_seeded_exclusion_set_still_blocks(self):
        for recipient in ("Taylor Quinn", "Robert Vance", "Rob", "Rob Vance"):
            with self.subTest(recipient=recipient):
                self.assertIn("EXCLUDED_CONTACT", self.gates(action={"recipient": recipient}))

    def test_named_no_followup_constraint_still_blocks(self):
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates(action={"recipient": "Jamie Rivera"}))
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates(action={"campaign_id": 700001}))
        no_automatic = dict(self.action, profile="https://www.linkedin.com/in/jrivera", recipient="Mock Person")
        proof = dict(self.proof, identity="linkedin.com/in/jrivera")
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT",
                      outreach.action_gates(self.database, self.config, no_automatic, proof, self.now))

    def test_name_keyed_suppression_still_blocks(self):
        self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                              ("name:" + orchestrator.name_key("Mock Person"), "Existing user exclusion"))
        self.assertIn("SUPPRESSED_CONTACT", self.gates())

    def test_previously_contacted_is_refused_before_the_draft_not_at_send(self):
        # Owner, 2026-10-01: "We shouldn't be checking at send time. that's too late."
        self.database.execute("INSERT INTO history VALUES ('name:mock person','Mock Person','Mock Company',?,'{}')",
                              (self.profile,))
        self.assertEqual(research.prior_contact_record(self.database, self.profile, "Mock Person"), "history")
        self.assertNotIn("PREVIOUSLY_CONTACTED_NO_INITIAL", self.gates())


if __name__ == "__main__":
    unittest.main()
