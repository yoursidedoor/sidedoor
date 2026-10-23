"""Fail-closed recipient identity on the REPLY send path — BLANKET variant.

Covers normalization-audit finding #2: `reply_execution._action` substituted the
LinkedIn profile key for the recipient's name, so every `name:`-keyed suppression
and both hardcoded exclusion sets silently missed while the send proceeded.

This is agent S's variant of agent L's suite. The resolver and the additive
name-anchored gates are L's; the difference is that the hold is UNCONDITIONAL —
an unresolvable recipient identity is unknown history, and unknown history makes
the send wait regardless of what the suppression table happens to contain today.

The synthetic correspondent now carries the name a real correspondent has
(`manual_reply_execution.NAMES`), bound durably in `history` and observed on the
provider room. `manual_reply_execution.UNNAMED_PROFILE` deliberately models the
opposite, equally real shape observed live as `linkedin.com/in/morgan-hale`:
an inbound-only correspondent in no durable record whose provider room carries
no name.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import unittest

import manual_reply_execution as manual
import outreach
import reply_execution
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


EXCLUDED_NAME = "name:taylor quinn"
EXCLUSION_REASON = "Existing user exclusion"


def bind_history(database, profile, name, company="Mock Company"):
    """Durably bind a profile to a human name, as `reconciliation.py:57` does."""
    database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                     ("name:" + name.casefold(), name, company, profile, "{}"))
    database.commit()


def rebind_history(database, profile, name, company="Mock Company"):
    """Replace every name the fixture bound to this profile with exactly one."""
    database.execute("DELETE FROM history WHERE profile=?", (profile,))
    bind_history(database, profile, name, company)


def setUpModule():
    # Fixture inbound messages are dated before the 2026-09-30 backlog cutoff, which applies
    # only to the real backlog on file (owner, 2026-09-30: "dont send these replies").
    global _BACKLOG_PATCH
    import datetime as _dt
    from unittest import mock as _mock
    import reply_execution as _reply_execution
    _BACKLOG_PATCH = _mock.patch.object(_reply_execution, "BACKLOG_CUTOFF",
                                        _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))
    _BACKLOG_PATCH.start()


def tearDownModule():
    _BACKLOG_PATCH.stop()


class ReplyRecipientResolutionTests(unittest.TestCase):
    """Unit contract for the resolver that replaced the degenerate fallback."""

    def setUp(self):
        self.stack = manual.isolated_boundaries()
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.fixture = manual.Fixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__)
        self.database = self.fixture.database

    def exclude(self, identity=EXCLUDED_NAME, reason=EXCLUSION_REASON):
        """A name-only exclusion with nothing binding that name to a profile."""
        self.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (identity, reason))
        self.database.commit()

    def test_history_row_resolves_the_human_name(self):
        self.assertEqual(reply_execution._recipient(self.database, manual.PROFILE), "Mock Contact")

    # Since 2026-09-30 (owner: replies to managed contacts must be sent) a person with no local
    # name is resolved from the name HeyReach reports at inspection. The profile stands in as the
    # recipient meanwhile, and it is still never treated as a name.
    def test_unresolvable_identity_defers_to_the_heyreach_name(self):
        self.assertEqual(outreach.unattributable_exclusion_names(self.database, reply=True), set())
        recipient = reply_execution._recipient(self.database, manual.UNNAMED_PROFILE)
        self.assertEqual(manual.UNNAMED_PROFILE, recipient)
        self.assertTrue(outreach.unresolved_recipient_name(recipient))

    def test_unresolvable_identity_defers_even_with_an_orphan_exclusion_present(self):
        self.exclude()
        self.assertTrue(outreach.unresolved_recipient_name(
            reply_execution._recipient(self.database, manual.UNNAMED_PROFILE)))

    def test_profile_shaped_history_recipient_is_never_accepted_as_a_name(self):
        # The exact degenerate value observed live in outreach_actions.
        self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                              ("name:linkedin.com/in/unbound-correspondent",
                               "linkedin.com/in/unbound-correspondent",
                               "Mock Company", manual.UNNAMED_PROFILE, "{}"))
        self.database.commit()
        self.assertTrue(outreach.unresolved_recipient_name(
            reply_execution._recipient(self.database, manual.UNNAMED_PROFILE)))

    def test_conflicting_names_hold_instead_of_guessing(self):
        outreach.initialize(self.database)
        identity = "linkedin.com/in/unbound-correspondent"
        self.database.execute("INSERT OR IGNORE INTO rw_contacts(identity,name,company,historical) VALUES (?,?,?,0)",
                              (identity, "First Spelling", "Mock Company"))
        self.database.execute("INSERT OR IGNORE INTO outreach_queue VALUES (?,?,?,?,?,?,NULL)",
                              (identity, 4242, "Other Name", "Mock Company", "PENDING_VALIDATION", "{}"))
        self.database.commit()
        with self.assertRaises(ValueError) as caught:
            reply_execution._recipient(self.database, manual.UNNAMED_PROFILE)
        self.assertEqual(str(caught.exception), "REPLY_RECIPIENT_IDENTITY_AMBIGUOUS")

    def test_binding_gates_surface_the_explicit_blocker(self):
        def raising(*unused, **unused_keywords):
            raise ValueError("REPLY_RECIPIENT_IDENTITY_UNRESOLVED")

        original = reply_execution._action
        reply_execution._action = raising
        try:
            gates = reply_execution.binding_gates(self.database, self.fixture.config,
                                                  {"reply_task_id": "t", "reply_inbound_at": "2026-10-01T00:00:00+00:00"},
                                                  self.fixture.now)
        finally:
            reply_execution._action = original
        self.assertEqual(gates, ["REPLY_RECIPIENT_IDENTITY_UNRESOLVED"])


class ReplyScheduledPathTests(unittest.TestCase):
    """The scheduled entry point, through orchestrator.service_cycle."""

    def run_cycle(self, prepare=None, profile=manual.PROFILE):
        with manual.isolated_boundaries():
            with manual.Fixture() as fixture:
                if prepare:
                    prepare(fixture)
                fixture.seed(profile)
                result = fixture.cycle()
                return fixture, result, [dict(row) for row in
                                         fixture.database.execute("SELECT * FROM outreach_actions")]

    def blockers(self, result):
        reply = result["reply_execution"]
        items = list(reply.get("promotion", {}).get("items", []))
        items.extend(reply.get("execution", {}).get("items", []))
        found = []
        for item in items:
            found.extend([item["blocker"]] if item.get("blocker") else item.get("gates", []))
        return found

    def test_excluded_recipient_without_a_profile_row_is_not_replied_to(self):
        """The live exposure: five `Existing user exclusion` name rows, no profile row."""
        def prepare(fixture):
            fixture.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                                     (EXCLUDED_NAME, EXCLUSION_REASON))
            fixture.database.commit()

        fixture, result, actions = self.run_cycle(prepare, manual.UNNAMED_PROFILE)
        self.assertEqual(fixture.requests, [], "Replied to an explicitly excluded recipient")
        # The room carries no name either, so the send waits at inspection.
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.blockers(result))

    def test_unresolvable_recipient_holds_with_an_empty_name_exclusion_space(self):
        """The blanket difference: L's conditional form sends here; this one waits."""
        def prepare(fixture):
            self.assertEqual(outreach.unattributable_exclusion_names(fixture.database, reply=True), set(),
                             "the conditional gate would be OPEN here")

        fixture, result, actions = self.run_cycle(prepare, manual.UNNAMED_PROFILE)
        self.assertEqual(fixture.requests, [], "Sent to an unidentified recipient")
        self.assertIn("RECIPIENT_IDENTITY_UNRESOLVED", self.blockers(result))

    def heyreach_named(self, name, listing_name=None, exclusion=False):
        """No local name; HeyReach's room (and optionally its listing) carries one."""
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            if exclusion:
                fixture.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                                         (EXCLUDED_NAME, EXCLUSION_REASON))
                fixture.database.commit()
            fixture.seed(manual.UNNAMED_PROFILE)
            fixture.rooms["thread-1"]["correspondentProfile"]["fullName"] = name
            original = fixture.call

            def call(operation, arguments):
                result = original(operation, arguments)
                if listing_name and operation == "get_conversations_v2":
                    for item in result["items"]:
                        item["correspondentProfile"]["fullName"] = listing_name
                return result

            fixture.call = call
            result = fixture.cycle()
            fixture.claims = [row[0] for row in fixture.database.execute("SELECT preflight FROM outreach_action_claims")]
            return fixture, result

    def test_heyreach_name_resolves_a_recipient_with_no_local_name(self):
        fixture, result = self.heyreach_named("Pat Example")
        self.assertEqual(1, len(fixture.requests), self.blockers(result))
        self.assertIn('"recipient_name": "pat example"', fixture.claims[0])

    def test_heyreach_name_matching_a_user_exclusion_blocks(self):
        fixture, result = self.heyreach_named("Taylor Quinn", exclusion=True)
        self.assertEqual([], fixture.requests)

    def test_disagreeing_heyreach_names_block(self):
        fixture, result = self.heyreach_named("Pat Example", listing_name="Someone Else")
        self.assertEqual([], fixture.requests)
        self.assertIn("REPLY_RECIPIENT_NAME_CONFLICT", self.blockers(result))

    def test_resolvable_recipient_still_sends_exactly_once(self):
        """No false positive: an ordinary resolvable recipient is unaffected."""
        fixture, result, actions = self.run_cycle()
        self.assertEqual(len(fixture.requests), 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertIn('"recipient": "Mock Contact"', actions[0]["payload"])

    def test_resolved_name_matching_an_exclusion_blocks_before_the_provider(self):
        def prepare(fixture):
            rebind_history(fixture.database, manual.PROFILE, "Taylor Quinn")
            fixture.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                                     (EXCLUDED_NAME, EXCLUSION_REASON))
            fixture.database.commit()

        fixture, result, actions = self.run_cycle(prepare)
        self.assertEqual(fixture.requests, [], "Replied to an explicitly excluded recipient")
        self.assertEqual(actions, [])
        blocked = {"EXCLUDED_CONTACT", "SUPPRESSED_CONTACT", "REPLY_EXCLUDED_OR_SUPPRESSED"}
        self.assertTrue(any(code in (blocker or "") for blocker in self.blockers(result) for code in blocked),
                        self.blockers(result))

    def test_additional_bound_name_matching_an_exclusion_still_blocks(self):
        """Additive: a second durable spelling of the same person still finds the row."""
        def prepare(fixture):
            bind_history(fixture.database, manual.PROFILE, "Taylor Quinn")
            fixture.database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)",
                                     (EXCLUDED_NAME, EXCLUSION_REASON))
            fixture.database.commit()

        fixture, result, actions = self.run_cycle(prepare)
        self.assertEqual(fixture.requests, [], "Replied to an explicitly excluded recipient")
        self.assertEqual(actions, [])

    def test_named_no_followup_constraint_still_blocks_the_reply_path(self):
        def prepare(fixture):
            rebind_history(fixture.database, manual.PROFILE, "Jamie Rivera")

        fixture, result, actions = self.run_cycle(prepare)
        self.assertEqual(fixture.requests, [])
        self.assertTrue(any("NO_AUTOMATIC_OUTREACH_CONTACT" in (blocker or "")
                            for blocker in self.blockers(result)), self.blockers(result))


if __name__ == "__main__":
    unittest.main()
