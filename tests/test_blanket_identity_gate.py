"""The BLANKET recipient-identity gate, and the fixture remedy that makes it honest.

Agent L's conditional gate holds an unresolvable recipient only when the database
also holds a name-only exclusion that binds to no profile. That makes a safety
property depend on the mutable contents of the `suppressions` table: the same send,
to the same unidentified person, proceeds or waits according to whether some
unrelated row happens to be orphaned today. This suite pins the blanket contract —
an unresolvable recipient identity ALWAYS waits — and pins the two fixture
deficiencies that previously hid it, so neither can silently return.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
from pathlib import Path
import tempfile
import unittest

import engagement
import heyreach_schema
import manual_reply_execution as manual
import orchestrator
import outreach
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


PROFILE = "https://www.linkedin.com/in/mock-person/"
IDENTITY = "linkedin.com/in/mock-person"
DEGENERATE = IDENTITY  # exactly what reply_execution._action used to substitute


class BlanketActionGateTests(unittest.TestCase):
    """`outreach.action_gates` — the last gate before the provider."""

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

    def gates(self):
        return outreach.action_gates(self.database, self.config, self.action, self.proof, self.now)

    def suppress(self, identity, reason):
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (identity, reason))

    def bind_name(self, name):
        with self.database:
            self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                  ("name:" + name.casefold(), name, "Mock Company", PROFILE, "{}"))

    # the blanket contract --------------------------------------------------
    def test_empty_name_exclusion_space_still_holds_an_unresolvable_recipient(self):
        """THE blanket difference. L's conditional form returns no hold here."""
        self.action["recipient"] = DEGENERATE
        self.assertEqual(outreach.unattributable_exclusion_names(self.database), set(),
                         "precondition: nothing a name lookup could miss")
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_hold_survives_the_orphan_exclusion_becoming_profile_bound(self):
        """The conditional gate silently reopens at exactly this moment."""
        self.suppress("name:taylor quinn", "Existing user exclusion")
        self.action["recipient"] = DEGENERATE
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())
        with self.database:  # identity reconciliation binds the exclusion to a profile
            self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                  ("name:taylor quinn", "Taylor Quinn", "Other Company",
                                   "https://www.linkedin.com/in/taylor-quinn", "{}"))
        self.assertEqual(outreach.unattributable_exclusion_names(self.database), set(),
                         "the conditional gate is now disarmed")
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_every_unresolvable_shape_holds(self):
        for value in (DEGENERATE, "https://www.linkedin.com/in/mock-person/", "", "   "):
            with self.subTest(value=value):
                self.action["recipient"] = value
                self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_a_null_recipient_aborts_the_gate_rather_than_passing_it(self):
        """Pre-existing fail-closed-by-abort at `name_key`; left byte-identical."""
        self.action["recipient"] = None
        with self.assertRaises(TypeError):
            self.gates()

    def test_hold_applies_to_every_purpose_not_only_reply(self):
        for purpose in ("INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "REPLY", "PREPARE_CAMPAIGN"):
            with self.subTest(purpose=purpose):
                self.action["purpose"] = purpose
                self.action["recipient"] = DEGENERATE
                self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    # no legitimate recipient is newly blocked ------------------------------
    def test_resolvable_recipient_is_not_blocked(self):
        self.assertEqual(self.gates(), [])

    def test_resolvable_recipient_is_not_blocked_even_with_orphan_exclusions_present(self):
        for name in ("taylor quinn", "robert vance", "ana casey moreau",
                     "casey moreau", "casey.moreau@moneypenny.example"):
            self.suppress("name:" + name, "Existing user exclusion")
        self.assertEqual(self.gates(), [], "a fully identified recipient must be unaffected")

    def test_durably_bound_name_clears_the_hold_for_a_degenerate_recipient(self):
        self.bind_name("Mock Person")
        self.action["recipient"] = DEGENERATE
        self.assertNotIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_provider_observed_name_clears_the_hold(self):
        self.action["recipient"] = DEGENERATE
        self.proof["recipient_name"] = "Mock Person"
        self.assertNotIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    def test_binding_a_name_adds_only_the_pre_existing_prior_contact_gate(self):
        """Strictly additive: a resolved name introduces no new gate of its own.

        The imported history no longer refuses an INITIAL at send time (owner, 2026-10-01: it is
        checked once, before the draft), so binding a name adds nothing at all here.
        """
        self.bind_name("Mock Person")
        self.assertEqual(self.gates(), [])
        self.action["purpose"] = "REPLY"
        self.assertNotIn("RECIPIENT_IDENTITY_UNRESOLVED", self.gates())

    # preserved protections, re-asserted under the blanket form --------------
    def test_preserved_protections_still_block(self):
        cases = {
            "EXCLUDED_CONTACT": lambda: self.action.update(recipient="Taylor Quinn"),
            "NO_AUTOMATIC_OUTREACH_CONTACT": lambda: self.action.update(recipient="Jamie Rivera"),
            "OPT_OUT_PRESENT": lambda: self.proof.update(opt_out=True),
            "SUPPRESSED_CONTACT": lambda: self.suppress("name:mock person", "Existing user exclusion"),
        }
        for gate, prepare in cases.items():
            with self.subTest(gate=gate):
                self.setUp()
                prepare()
                self.assertIn(gate, self.gates())

    def test_seeded_campaign_still_blocks_regardless_of_the_name(self):
        self.action["campaign_id"] = 700001
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates())
        self.action["recipient"] = DEGENERATE
        self.assertIn("NO_AUTOMATIC_OUTREACH_CONTACT", self.gates())

    def test_stop_on_reply_and_closure_still_block(self):
        engagement.record_event(self.database, {"source": "heyreach", "source_id": "inbound",
            "profile": PROFILE, "kind": "inbound", "occurred_at": self.now.isoformat(),
            "observed_at": self.now.isoformat(), "evidence": "mock-independent",
            "raw": {"fixture": True}, "verified": True})
        self.assertIn("REPLY_REQUIRES_CONTEXT", self.gates())
        engagement.close_outreach(self.database, PROFILE, "Synthetic closure", self.now)
        self.assertIn("OUTREACH_CLOSED", self.gates())


class CorrespondentNameSchemaTests(unittest.TestCase):
    """`heyreach_schema` now models the correspondent's name instead of ignoring it."""

    def test_full_name_is_decoded(self):
        self.assertEqual(heyreach_schema.correspondent_name(
            {"profileUrl": "x", "fullName": "Taylor Quinn"}), "Taylor Quinn")

    def test_first_and_last_name_are_joined(self):
        self.assertEqual(heyreach_schema.correspondent_name(
            {"firstName": "Jamie", "lastName": "Rivera"}), "Jamie Rivera")

    def test_absent_name_stays_absent_rather_than_invented(self):
        self.assertIsNone(heyreach_schema.correspondent_name({"profileUrl": "x", "linkedin_id": "y"}))
        self.assertIsNone(heyreach_schema.correspondent_name({"fullName": None, "firstName": None}))

    def test_profile_supplied_as_a_name_is_rejected(self):
        for value in ("linkedin.com/in/morgan-hale", "https://www.linkedin.com/in/jrivera/",
                      "br.linkedin.com/in/remy"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                heyreach_schema.correspondent_name({"fullName": value})

    def test_non_string_name_is_rejected(self):
        for value in (5, [], {"a": 1}, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                heyreach_schema.correspondent_name({"fullName": value})

    def test_decoded_name_is_never_admitted_as_a_recipient_name_when_profile_shaped(self):
        self.assertTrue(outreach.unresolved_recipient_name("linkedin.com/in/morgan-hale"))
        self.assertFalse(outreach.unresolved_recipient_name("Morgan Hale"))


class ReplyFixtureRealismTests(unittest.TestCase):
    """The synthetic reply correspondent models a real person, not a nameless one."""

    def setUp(self):
        self.stack = manual.isolated_boundaries()
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.fixture = manual.Fixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__)

    def test_each_named_profile_is_durably_bound_to_exactly_one_name(self):
        for profile, name in manual.NAMES.items():
            with self.subTest(profile=profile):
                identities = engagement.identity_keys(self.fixture.database, profile)
                self.assertEqual(outreach.recipient_names(self.fixture.database, identities),
                                 {orchestrator.name_key(name)})

    def test_the_provider_room_reports_the_same_name(self):
        self.fixture.seed()
        room = self.fixture.rooms["thread-1"]
        self.assertEqual(heyreach_schema.correspondent_name(room["correspondentProfile"]),
                         manual.NAMES[manual.PROFILE])

    def test_the_unbound_correspondent_is_modelled_and_stays_unresolvable(self):
        """The live `linkedin.com/in/morgan-hale` shape: no record, no provider name."""
        self.fixture.seed(manual.UNNAMED_PROFILE, "message-9", "thread-9")
        room = self.fixture.rooms["thread-9"]
        self.assertIsNone(heyreach_schema.correspondent_name(room["correspondentProfile"]))
        identities = engagement.identity_keys(self.fixture.database, manual.UNNAMED_PROFILE)
        self.assertEqual(outreach.recipient_names(self.fixture.database, identities), set())


if __name__ == "__main__":
    unittest.main()
