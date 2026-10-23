"""RL1: the product default is Unipile through the relay. A fresh root's first hourly run with the
shipped config.json, a temporary home holding only a license key and no HeyReach endpoint, and the
relay (in front of a fake Unipile on 127.0.0.1) records no failure: HeyReach-only steps say
NOT_USED, every other step completes. No network beyond 127.0.0.1, no subprocess."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock

import applications
import daily_schedule
import orchestrator
import outreach
import resources
import user_profile
try:
    from relay_optional import LICENSE, RelayHarness, needs_relay
except ImportError:   # run as tests.test_unipile_fresh_root from the repo root
    from tests.relay_optional import LICENSE, RelayHarness, needs_relay


REAL_OPEN = urllib.request.OpenerDirector.open


def local_only(self, request, *args, **named):
    url = getattr(request, "full_url", request)
    if not str(url).startswith("http://127.0.0.1"):
        raise AssertionError("external call in a fresh-root run")
    return REAL_OPEN(self, request, *args, **named)


@needs_relay
class FreshRootUnipile(RelayHarness):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="sidedoor-rl1-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        shutil.copyfile(user_profile.EXAMPLE_PROFILE, self.home / "profile.json")
        self.root = Path(tempfile.mkdtemp(prefix="sidedoor-fresh-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for folder in ("state", "reports"):
            (self.root / folder).mkdir()
        self.config = resources.read_json("config.json")
        self.config["credential_config"] = str(self.root / "no-heyreach.toml")   # a user's install

    def run_cycle(self):
        database = orchestrator.connect(self.root)
        self.addCleanup(database.close)
        with mock.patch.dict("os.environ", {user_profile.HOME_VARIABLE: str(self.home)}), \
                mock.patch("setup_steps.RELAY_URL", self.base), \
                mock.patch.object(urllib.request.OpenerDirector, "open", local_only), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")), \
                mock.patch.object(orchestrator, "HeyReach", side_effect=AssertionError("HeyReach built")), \
                mock.patch.object(orchestrator, "research_turn_pending", return_value=False), \
                mock.patch.object(orchestrator.owner_seed, "items", return_value=[]), \
                mock.patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "COMPLETE"}), \
                mock.patch.object(orchestrator.reporting, "daily_due", return_value=False), \
                mock.patch.object(orchestrator.notifications, "dispatch", return_value={"state": "COMPLETE"}):
            outreach.initialize(database)
            applications.initialize(database)
            daily_schedule.initialize(database, self.config["timezone"])
            pipeline = orchestrator.snapshot_sources(self.root, self.config)
            orchestrator.import_history(database, self.root, pipeline)
            code = orchestrator.run(self.root, self.config, database, pipeline)
        latest = json.loads((self.root / "reports/latest.json").read_text())
        held = [tuple(row) for row in database.execute(
            "SELECT worker,status,blocker FROM workflow_tasks WHERE status!='COMPLETE' ORDER BY worker")]
        return code, latest, held

    def test_the_shipped_default_is_unipile(self):
        self.assertEqual(self.config["linkedin_provider"], "unipile")
        self.assertIn("INVITE", self.config["outreach_allowed_purposes"])

    def test_first_run_records_no_failure_and_skips_heyreach_only_steps(self):
        (self.home / "license.key").write_text(LICENSE + "\n")
        code, latest, held = self.run_cycle()
        self.assertEqual((code, latest["state"]), (0, "COMPLETE"))
        self.assertEqual(held, [])
        self.assertEqual(latest["observation"]["state"], "NOT_USED")
        self.assertEqual(latest["outreach"]["state"], "NOT_USED")
        self.assertEqual(latest["engagement"]["heyreach"]["state"], "NOT_USED")
        self.assertEqual(latest["engagement"]["unipile"]["state"], "PROCESSED")
        # The Unipile inbox was read through the relay with the license key, never the master key.
        self.assertIn("/api/v1/accounts/acct-mine", [request["path"] for request in self.upstream.requests])

    def test_without_a_license_linkedin_steps_hold_and_nothing_fails(self):
        code, latest, held = self.run_cycle()
        self.assertEqual(latest["state"], "COMPLETE")
        self.assertTrue(held)
        self.assertEqual({blocker for _, _, blocker in held}, {"LICENSE_REQUIRED"})
        self.assertEqual({status for _, status, _ in held}, {"BLOCKED"})
        self.assertEqual(self.upstream.requests, [])

    def test_the_heyreach_slot_on_unipile_never_reaches_heyreach(self):
        client = orchestrator.heyreach_for(self.config)
        self.assertIsInstance(client, orchestrator.NoHeyReach)
        with self.assertRaises(orchestrator.HeyReachNotUsed):
            client.call("get_conversations_v2", {})
