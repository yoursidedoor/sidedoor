import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import unittest

import engagement
import outreach
import test_engagement


class ObservedOutboundTests(unittest.TestCase):
    setUp = test_engagement.EngagementTests.setUp

    def test_unknown_outgoing_purpose_never_becomes_initial_or_followup(self):
        observed = test_engagement.event("outbound_observed", "observed-outgoing")
        engagement.record_event(self.database, observed)
        engagement.record_event(self.database, test_engagement.event(
            "accepted", "accepted", test_engagement.NOW - dt.timedelta(hours=15)))
        result = engagement.next_action(self.database, test_engagement.PROFILE, test_engagement.NOW)
        self.assertEqual(result["blocker"], "OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION")
        self.assertEqual(self.database.execute("SELECT count(*) FROM engagement_events WHERE kind IN ('initial_sent','followup_sent')").fetchone()[0], 0)
        self.assertTrue(outreach._new_path_reservations(self.database, test_engagement.PROFILE))
        self.assertFalse(engagement.record_event(self.database, observed)["inserted"])

    def test_incoming_observation_preserves_original_exclusion_reason(self):
        identity = engagement.profile_key(test_engagement.PROFILE)
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (identity, "Explicit original user exclusion"))
        engagement.record_event(self.database, test_engagement.event())
        self.assertEqual(self.database.execute("SELECT reason FROM suppressions WHERE identity=?", (identity,)).fetchone()[0],
                         "Explicit original user exclusion")
        self.assertEqual(engagement.stop_state(self.database, identity), "REPLY_REQUIRES_CONTEXT")


if __name__ == "__main__":
    unittest.main()
