"""E3.T3 (d) and (f) over FakePort with a fake clock: a Unipile invite counts as an independently
verified send (source_records.execution, which reporting counts) only once its verified initial_sent
is recorded, and a NOT_PENDING Unipile person (declined,
withdrawn or expired invitation) closes at the 27-day due date on one complete read."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

import engagement
import source_records
import outreach
import sequence
from linkedin_port import FakePort
from orchestrator import profile_key

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_inactivity_read_completeness as completeness
import test_unipile_followups as unipile


ALICE = "https://www.linkedin.com/in/alice"
CLAIMED = dt.datetime(2026, 10, 5, 16, tzinfo=dt.timezone.utc)


class InviteExposure(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        outreach.initialize_actions(self.db)
        self.port = FakePort()
        provider_id = self.port.add_person(ALICE)
        payload = {"action_id": "invite-1", "purpose": "INVITE", "provider": "unipile", "channel": "linkedin",
                   "profile": ALICE, "recipient": "Alice Example", "company": "Example Co", "provider_id": provider_id,
                   "note": "Hi Alice, a short note."}
        text = json.dumps(payload, sort_keys=True)
        self.hash = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute("INSERT INTO outreach_actions(action_id,identity,slot,payload,payload_hash,state,claimed_at) "
                        "VALUES (?,?,?,?,?,?,?)", ("invite-1", profile_key(ALICE), "INVITE", text, self.hash,
                                                   "REQUEST_STARTED", CLAIMED.isoformat()))
        self.db.commit()

    def delivered(self, now):
        return source_records.execution(self.db, {"table": "outreach_actions", "key": "invite-1"}, now.isoformat())

    def test_delivered_only_after_initial_sent_is_recorded(self):
        with patch.object(sequence, "_now", return_value=(CLAIMED + dt.timedelta(minutes=1)).isoformat()):
            self.assertEqual(sequence.send_invite(self.db, self.port, "invite-1"), sequence.ACKED)
        self.assertIsNone(self.delivered(CLAIMED + dt.timedelta(hours=1)))  # the acknowledgement is not delivery
        with patch.object(sequence, "_now", return_value=(CLAIMED + dt.timedelta(hours=2)).isoformat()):
            self.assertEqual(sequence.reconcile_invite(self.db, self.port, "invite-1"), sequence.CONFIRMED)
        delivered = self.delivered(CLAIMED + dt.timedelta(hours=3))
        self.assertEqual(delivered["kind"], "OUTREACH_SENT")
        self.assertEqual(delivered["at"], (CLAIMED + dt.timedelta(minutes=1)).isoformat())
        evidence = delivered["evidence"]
        self.assertEqual((evidence["provider_message_id"], evidence["invite_payload_hash"]), ("invite:invite-1", self.hash))


class NotPendingClosure(unittest.TestCase):
    setUp = completeness.ZeroRoomReadCompleteness.setUp
    call = completeness.ZeroRoomReadCompleteness.call
    port_cycle = unipile.UnipileInactivity.port_cycle

    def invited(self, distance=2):
        with self.database:
            self.database.execute("DELETE FROM engagement_events")
        sent = (completeness.NOW - dt.timedelta(days=27)).isoformat()
        engagement.record_event(self.database, {"source": "unipile", "source_id": "invite:invite-1",
            "profile": completeness.PROFILE, "kind": "initial_sent", "purpose": "INVITE", "invitation": True,
            "verified": True, "occurred_at": sent, "observed_at": sent, "evidence": "unipile-read:" + sent, "raw": {}})
        self.port = FakePort()
        self.port.add_person(completeness.PROFILE, distance=distance)  # no invitation in the sent listing

    def test_not_pending_closes_at_the_due_date_on_one_complete_read(self):
        self.invited()
        self.assertEqual(sequence.acceptance(self.port, completeness.PROFILE)[0], "NOT_PENDING")
        self.port.calls.clear()
        early = self.port_cycle(completeness.NOW - dt.timedelta(days=1), self.port)
        self.assertEqual(early["closed"], 0, early)
        self.assertIsNone(engagement.closure_status(self.database, completeness.PROFILE))
        result = self.port_cycle(completeness.NOW, self.port)
        self.assertEqual(result["closed"], 1, result)
        self.assertEqual(self.port.calls.count("lookup"), 1)
        self.assertIsNotNone(engagement.closure_status(self.database, completeness.PROFILE))

    def test_unknown_never_closes(self):
        self.invited(distance=None)
        self.assertEqual(sequence.acceptance(self.port, completeness.PROFILE)[0], "UNKNOWN")
        self.port_cycle(completeness.NOW, self.port)
        self.port_cycle(completeness.NOW + dt.timedelta(days=27), self.port)
        self.assertIsNone(engagement.closure_status(self.database, completeness.PROFILE))


if __name__ == "__main__":
    unittest.main()
