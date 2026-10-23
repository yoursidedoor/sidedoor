"""Suppression and exclusion keys must fail CLOSED at the send-authorizing gate.

Normalization-audit finding #2: when `action["recipient"]` holds a LinkedIn
profile key instead of a name, `"name:" + name_key(recipient)` becomes
`name:linkedin.com/in/<slug>` — a key no suppression row and no exclusion
literal can ever equal — so every name-keyed check silently misses at once.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
from pathlib import Path
import tempfile
import unittest

import engagement
import orchestrator
import outreach


PROFILE = "https://www.linkedin.com/in/mock-person/"
IDENTITY = "linkedin.com/in/mock-person"
DEGENERATE = IDENTITY  # exactly what reply_execution._action used to substitute


class SuppressionKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        outreach.initialize(self.database)
        outreach.initialize_actions(self.database)
        engagement.initialize(self.database)
        self.now = dt.datetime(2026, 9, 21, 17, tzinfo=dt.timezone.utc)
        self.config = {"outreach_mode": "guarded", "outreach_actions_enabled": True, "sender_id": 424242}
        self.action = {"action_id": "initial-1", "profile": PROFILE, "recipient": "Mock Person",
            "company": "Mock Company", "purpose": "INITIAL", "message": "Verified fixture text",
            "channel": "linkedin", "provider": "heyreach", "conversation_id": "thread-1",
            "verification": {"content_verified": True, "content_sources": ["mock-resume"],
                "identity_verified": True, "identity_source": "mock-identity",
                "team_relevance_verified": True, "team_source": "mock-team",
                "verified_at": self.now.isoformat()}}
        self.proof = {"identity": IDENTITY, "sender_id": 424242, "sender_ready": True,
            "entitlement_verified": True, "direct_messages_complete": True, "sent_invitations_complete": True,
            "pending_invitation": False, "has_reply": False, "negative_reply": False, "opt_out": False,
            "cooldown": False, "checked_at": self.now.isoformat(), "evidence": "mock-direct-history",
            "prior_outreach": False, "company_contact_count": 0, "company_count_evidence": "mock-history",
            "conversation_id": "thread-1", "followups_sent": 0, "initial_message_sent": False}

    # helpers ---------------------------------------------------------------
    def gates(self):
        return outreach.action_gates(self.database, self.config, self.action, self.proof, self.now)

    def suppress(self, identity, reason):
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (identity, reason))

    def bind_name(self, name, table="history"):
        """Durably bind PROFILE to a human name without touching action['recipient']."""
        with self.database:
            if table == "history":
                self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                      ("name:" + name.casefold(), name, "Mock Company", PROFILE, "{}"))
            else:
                self.database.execute("INSERT OR IGNORE INTO outreach_queue VALUES (?,?,?,?,?,?,NULL)",
                                      (IDENTITY, 4242, name, "Mock Company", "CANCELLED", "{}"))

    def record(self, kind, occurred=None):
        return engagement.record_event(self.database, {"source": "heyreach", "source_id": kind,
            "profile": PROFILE, "kind": kind, "occurred_at": (occurred or self.now).isoformat(),
            "observed_at": self.now.isoformat(), "evidence": "mock-independent",
            "raw": {"fixture": True}, "verified": True})

    # the defect ------------------------------------------------------------
    def test_no_false_positive_for_a_normally_resolvable_recipient(self):
        self.assertEqual(self.gates(), [])

    def test_degenerate_recipient_with_no_resolvable_name_fails_closed(self):
        self.suppress("name:taylor quinn", "Existing user exclusion")
        self.action["recipient"] = DEGENERATE
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_empty_name_exclusion_space_still_holds_an_unresolvable_recipient(self):
        """Blanket form: an unidentified recipient waits whatever the table holds.

        This replaces the conditional form's precision assertion. Under the
        blanket gate the suppression table's current contents no longer decide
        whether an unidentified recipient may be contacted; an unresolvable
        identity is unknown history and the send waits for AI investigation.
        """
        self.action["recipient"] = DEGENERATE
        self.assertEqual(outreach.unattributable_exclusion_names(self.database), set(),
                         "precondition: nothing a name lookup could miss")
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_unattributable_exclusion_names_ignores_profile_bound_names(self):
        self.suppress("name:taylor quinn", "Existing user exclusion")
        self.suppress("name:mock person", "Existing user exclusion")
        self.bind_name("Mock Person")
        self.assertEqual(outreach.unattributable_exclusion_names(self.database), {"taylor quinn"})

    def test_degenerate_recipient_no_longer_misses_a_name_keyed_exclusion(self):
        """The live exposure: `name:taylor quinn`, `Existing user exclusion`, no profile row."""
        self.suppress("name:taylor quinn", "Existing user exclusion")
        self.bind_name("Taylor Quinn")
        self.action["recipient"] = DEGENERATE
        gates = self.gates()
        self.assertIn("SUPPRESSED_CONTACT", gates)
        self.assertIn("EXCLUDED_CONTACT", gates)

    def test_provider_observed_name_also_resolves_the_exclusion(self):
        self.suppress("name:taylor quinn", "Existing user exclusion")
        self.action["recipient"] = DEGENERATE
        self.proof["recipient_name"] = "Taylor Quinn"
        gates = self.gates()
        self.assertIn("SUPPRESSED_CONTACT", gates)
        self.assertIn("EXCLUDED_CONTACT", gates)
        self.assertNotIn("RECIPIENT_IDENTITY_UNRESOLVED", gates)

    def test_queue_bound_name_resolves_an_exclusion_the_raw_recipient_misses(self):
        self.suppress("name:rob vance", "Existing user exclusion")
        self.bind_name("Rob Vance", table="outreach_queue")
        self.action["recipient"] = DEGENERATE
        self.assertIn("EXCLUDED_CONTACT", self.gates())

    def test_degenerate_recipient_does_not_bypass_the_named_no_followup_rule(self):
        self.bind_name("Jamie Rivera")
        self.action["recipient"] = DEGENERATE
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates())

    # preserved protections -------------------------------------------------
    def test_seeded_exclusion_set_still_blocks_a_plain_name(self):
        for name in ("Taylor Quinn", "Robert Vance", "Rob", "Rob Vance"):
            with self.subTest(name=name):
                self.action["recipient"] = name
                self.assertIn("EXCLUDED_CONTACT", self.gates())

    def test_no_automatic_contact_rule_still_blocks_by_name_profile_and_campaign(self):
        self.action["recipient"] = "Jamie Rivera"
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates())
        self.action["recipient"] = "Mock Person"
        self.action["campaign_id"] = 700001
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates())

    def test_opt_out_still_blocks(self):
        self.proof["opt_out"] = True
        self.assertIn("OPT_OUT_PRESENT", self.gates())

    def test_stop_on_reply_still_blocks(self):
        self.record("inbound")
        self.assertIn("REPLY_REQUIRES_CONTEXT", self.gates())

    def test_closed_recipient_still_blocks(self):
        engagement.close_outreach(self.database, PROFILE, "Synthetic closure", self.now)
        self.assertIn("OUTREACH_CLOSED", self.gates())

    def test_name_keyed_suppression_still_blocks_a_plain_name(self):
        self.suppress("name:mock person", "Existing user exclusion")
        self.assertIn("SUPPRESSED_CONTACT", self.gates())

    def test_profile_keyed_suppression_still_blocks(self):
        self.suppress(IDENTITY, "Existing user exclusion")
        self.assertIn("SUPPRESSED_CONTACT", self.gates())

    # normalizer contract ---------------------------------------------------
    def test_unresolved_recipient_name_recognizes_profile_shaped_values(self):
        for value in ("linkedin.com/in/morgan-hale", "https://www.linkedin.com/in/jrivera/",
                      "https://br.linkedin.com/in/remy-ger%C3%B3nimo-vale-moraes-0000a1b2",
                      "", "   ", None):
            with self.subTest(value=value):
                self.assertTrue(outreach.unresolved_recipient_name(value))
        for value in ("Taylor Quinn", "Jamie Rivera", "Remy Gerónimo Vale Moraes",
                      "casey.moreau@moneypenny.example", "Rob"):
            with self.subTest(value=value):
                self.assertFalse(outreach.unresolved_recipient_name(value))

    def test_recipient_names_never_admits_a_profile_shaped_value(self):
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                  ("name:" + DEGENERATE, DEGENERATE, "Mock Company", PROFILE, "{}"))
        self.assertEqual(outreach.recipient_names(self.database, [IDENTITY], extra=(DEGENERATE,)), set())

    def test_legacy_queue_path_rejects_an_unresolvable_recipient(self):
        candidate = {"recipient": DEGENERATE, "company": "Mock Company", "profile": PROFILE,
                     "message": "Exact note", "campaign_id": 4243}
        with self.assertRaises(ValueError) as caught:
            outreach.enqueue(self.database, candidate)
        self.assertEqual(str(caught.exception), "RECIPIENT_IDENTITY_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
