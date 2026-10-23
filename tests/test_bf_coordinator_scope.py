"""Patch BF: the coordinator admission cap is per scope, and reaching it is an orchestrator hold.

virtucon:9000000007 was reopened by the owner (grant user-authorized-reopen-example-2) and
abandoned 68 minutes later with RECOVERY_COORDINATOR_FAILED: both recorded "failures" were
APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED, because the cap of five counted every coordinator
admission the job ever had. Both tests fail on the pre-BF code.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest.mock import patch

import application_connection
import application_recovery
import application_recovery_decision
import applications
import reopen_grants
import test_part_c_read_gating as part_c

BASE = part_c.at(86400)  # after applications.READ_CONTRACT_REVISION_AT


def hours(value):
    return part_c.at(86400 + int(value * 3600))


class CappedAdapter:
    """The coordinator refuses on its admission cap (an orchestrator-side limit)."""

    root = None
    calls = 0

    def coordinator_cap_reached(self, job_key, now=None):
        return True

    def recovery_decision(self, packet):
        self.calls += 1
        raise RuntimeError("APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED")


class CoordinatorScopeTests(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def events(self, kind):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id", (self.key, kind))]

    def admit(self, at, count):
        with self.database:
            for _ in range(count):
                applications.event(self.database, self.key, "BROWSER_CAPACITY_RESERVED",
                                   {"operations": 1, "seconds": 30, "purpose": "orchestrator_recovery"}, at)

    def test_owner_reopen_starts_a_new_coordinator_admission_scope(self):
        adapter = application_connection.HostedDesktopAdapter(
            self.database, self.root, {"application_browser": {"enabled": True}}, clock=lambda: BASE)
        self.admit(hours(-3), 5)  # spent before the owner's reopen
        with self.database:
            self.database.executescript(reopen_grants.SCHEMA)
            self.database.execute("INSERT INTO application_reopen_grants VALUES (?,?,?,?,?,?,?,?)",
                                  ("grant-1", "owner", json.dumps([self.key]), 3, 48, hours(-1), hours(47), "reopen"))
            self.database.execute("INSERT INTO application_reopen_grant_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                                  ("grant-1", self.key, hours(-1), 1, "[]", hours(-2), "RULES_CHANGED", 3, hours(47)))
        decision = {"action": "RETRY"}
        with patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None), \
                patch.object(application_recovery_decision, "decide", lambda *a, **k: decision):
            self.assertIs(adapter.recovery_decision({"job": self.job()}), decision)
            self.admit(hours(-0.5), 5)  # the cap of five still holds inside the reopened scope
            with self.assertRaisesRegex(RuntimeError, "APPLICATION_COORDINATOR_ATTEMPTS_EXHAUSTED"):
                adapter.recovery_decision({"job": self.job()})

    def test_cap_refusal_is_an_orchestrator_hold_never_an_abandonment(self):
        adapter = CappedAdapter()
        for moment in (BASE, hours(1), hours(49)):  # the last one is past the 48 h deadline
            application_recovery.recover(self.database, self.job(), adapter, part_c.RECOVERY,
                                         "REQUISITION_MISMATCH", "Resolve the requisition", moment)
        job = self.job()
        self.assertEqual((job["state"], job["blocker"]), ("BLOCKED", "APPLICATION_RECOVERY_COORDINATOR_CAP_HELD"))
        self.assertEqual(self.events("APPLICATION_RECOVERY_DECISION_FAILED"), [])
        self.assertEqual(self.events("APPLICATION_ABANDONED"), [])
        self.assertEqual(len(self.events("APPLICATION_RECOVERY_COORDINATOR_CAP_REACHED")), 1)
        self.assertEqual(adapter.calls, 1)  # while the cap persists the coordinator is not re-asked
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
