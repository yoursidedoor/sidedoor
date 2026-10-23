"""A recovery lifecycle never continues on a blocker the current rules no longer raise."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from pathlib import Path
import tempfile
import unittest

import application_recovery
import applications
import manual_application_recovery_lifecycle as rehearsal
from test_applications_e2e import NOW

STALE = "PRIOR_PROCESS_REQUISITION_UNRESOLVED"


class SupersededRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="recovery-superseded-")
        self.addCleanup(temporary.cleanup)
        self.fixture = rehearsal.Fixture(Path(temporary.name))
        self.addCleanup(self.fixture.database.close)
        self.key, _ = self.fixture.discover(unresolved=False)
        database = self.fixture.database
        with database:
            applications.hold(database, self.fixture.job(self.key), STALE, "Map the prior requisition", NOW, "BLOCKED", NOW)
            applications.event(database, self.key, "APPLICATION_RECOVERY_STARTED", {
                "blocker": STALE, "started_at": NOW, "deadline": "2026-09-30T00:00:00+00:00", "max_attempts": 3}, NOW)

    def assert_superseded(self):
        adapter = self.fixture.adapter
        self.assertEqual((adapter.decision_packets, adapter.investigations, adapter.calls), ([], [], []))
        detail = self.fixture.events(self.key, application_recovery.SUPERSEDED_EVENT)[-1]
        self.assertEqual((detail["old_blocker"], detail["current_gate"]["result"]), (STALE, "PASS"))
        job = self.fixture.job(self.key)
        self.assertEqual((job["state"], job["blocker"]), ("QUEUED", None))
        self.assertFalse(application_recovery.active(self.fixture.database, self.key))
        self.assertFalse(self.fixture.events(self.key, "APPLICATION_ABANDONED"))

    def test_lifecycle_step_supersedes_blocker_the_gate_no_longer_raises(self):
        self.fixture.cycle(60)
        self.assert_superseded()


if __name__ == "__main__":
    unittest.main()
