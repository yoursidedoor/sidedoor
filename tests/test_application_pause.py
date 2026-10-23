import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from pathlib import Path
import unittest
from unittest.mock import patch

import orchestrator


class ApplicationPauseTests(unittest.TestCase):
    def test_disabled_applications_do_not_open_policy_or_execute(self):
        with patch.object(orchestrator.applications, "cycle") as worker, patch.object(orchestrator.notifications, "dispatch") as sender:
            result = orchestrator.application_cycle(Path("/nonexistent"), {"applications_enabled": False}, None)
            self.assertEqual(result["state"], "PAUSED_FOR_REVISED_REQUIREMENTS")
            worker.assert_not_called()
            sender.assert_not_called()

    def test_missing_flag_defaults_to_disabled(self):
        with patch.object(orchestrator.applications, "cycle") as worker:
            self.assertEqual(orchestrator.application_cycle(Path("/nonexistent"), {}, None)["submissions"], 0)
            worker.assert_not_called()
