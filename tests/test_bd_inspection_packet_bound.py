"""BD: new-contact inspection packets stay under the prompt bound; ValueError codes are recorded."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

import outreach_agent
import test_outreach_agent


class InspectionPacketBoundTests(unittest.TestCase):
    def test_network_listing_is_trimmed_to_recipient_candidates(self):
        adapter = outreach_agent.Adapter.__new__(outreach_agent.Adapter)
        adapter.database = sqlite3.connect(":memory:")
        profile = "https://www.linkedin.com/in/dana-brook-11111a111"
        stranger = {"firstName": "Joy", "lastName": "Other", "linkedin_id": "1", "profileUrl": "https://www.linkedin.com/in/ACoAAstranger", "about": "x" * 600}
        match = {"firstName": "Dana", "lastName": "Brook", "linkedin_id": "2", "profileUrl": "https://www.linkedin.com/in/ACoAAdana"}
        network = {"items": [stranger] * 99 + [match], "totalCount": 1735}
        observation = {"observations": [
            {"operation": "get_lead", "identity_matches": True, "raw": {"firstName": "Dana", "lastName": "Brook", "linkedin_id": "9", "profileUrl": profile}},
            {"operation": "get_my_network_for_sender", "raw": network, "result": network}]}
        trimmed = adapter.judgment_observation({"purpose": "INITIAL", "profile": profile}, observation)
        read = trimmed["observations"][1]
        self.assertEqual(read["raw"]["items"], [match])
        self.assertEqual(read["result"], read["raw"])
        self.assertEqual(read["original_item_count"], 100)
        self.assertEqual(len(observation["observations"][1]["raw"]["items"]), 100)
        # The model-facing packet carries the read log ONCE (owner, 2026-09-23). The provider
        # envelope still exposes both names; re-applying that alias here sent the whole log twice
        # and was 41% of the largest inspection packets, which is what pushed Robin Vale and
        # Sasha Lind past the prompt bound. Nothing reads the packet's `reads`.
        self.assertNotIn("reads", trimmed)
        self.assertEqual(len(trimmed["observations"]), 2)

    def test_investigation_value_error_message_is_recorded(self):
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=ValueError("OUTREACH_AGENT_INPUT_OR_TIME_BOUND")):
            fixture = test_outreach_agent.DraftCapacityTests("run_cycle")
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            item = fixture.run_cycle()
        self.assertEqual(item["blocker"], "OUTREACH_INVESTIGATION_ValueError:OUTREACH_AGENT_INPUT_OR_TIME_BOUND")

