import concurrent.futures
import contextlib
import json
from pathlib import Path
import shutil
import tempfile
import os
import unittest
from unittest.mock import patch

import orchestrator as runner
import profile_home
import user_profile


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in ("state", "backups", "reports", "logs"):
            (self.root / directory).mkdir()
        self.database = runner.connect(self.root)
        self.addCleanup(self.database.close)
        self.pipeline = {
            "verified_prior_outreach": [
                {"recipient": "Riley Park", "company": "Ollivanders", "profile": "https://www.linkedin.com/in/riley-park/"},
                {"recipient": "Remy", "company": "Moneypenny"},
            ],
            "companies": [
                {"company": "Moneypenny", "advocate": "Remy", "profile": "https://br.linkedin.com/in/remy", "conservative_known_contact_count": 2},
                {"company": "Globodyne", "conservative_known_contact_count": 1},
            ],
            "rules": {"exclude_all_outreach": ["Taylor Quinn"], "no_email": ["Ana Casey Moreau"], "max_unique_contacts_per_company": 3},
        }
        runner.write_json(self.root / "backups/delivery-evidence.json", {"delivery": {"status": "SENT_PROVIDER_CONFIRMED_AND_LINKEDIN_VERIFIED"}})
        runner.import_history(self.database, self.root, self.pipeline)

    def draft(self, recipient="New Person", profile="https://www.linkedin.com/in/new-person/", company="New Co"):
        return {"recipient": recipient, "profile": profile, "company": company,
                "message": "Draft for review", "source_url": "https://example.com/team"}

    def save_drafts(self, drafts):
        return runner.store_research(self.database, self.pipeline, {"summary": "Research", "blockers": [], "opportunities": [], "drafts": drafts})

    def test_import_is_idempotent_and_keeps_newer_local_records(self):
        self.database.execute("UPDATE history SET evidence='newer evidence' WHERE recipient='Riley Park'")
        self.database.commit()
        runner.import_history(self.database, self.root, self.pipeline)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM history").fetchone()[0], 3)
        self.assertEqual(self.database.execute("SELECT evidence FROM history WHERE recipient='Riley Park'").fetchone()[0], "newer evidence")
        self.assertTrue(self.database.execute("SELECT 1 FROM suppressions WHERE identity='linkedin.com/in/remy'").fetchone())

    def test_alternate_profile_and_name_cannot_repeat_initial_outreach(self):
        rejected = self.save_drafts([
            self.draft("Different spelling", "http://uk.linkedin.com/in/JRIVERA/?trk=abc"),
            self.draft("  riley   PARK  ", "https://linkedin.com/in/alternate"),
            self.draft("Taylor Quinn"),
        ])
        self.assertEqual(len(rejected), 3)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_unique_draft_survives_restart_and_rerun(self):
        self.save_drafts([self.draft()])
        with contextlib.closing(runner.connect(self.root)) as second:
            runner.store_research(second, self.pipeline, {"summary": "Again", "blockers": [], "opportunities": [], "drafts": [self.draft()]})
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)
        self.assertEqual(self.database.execute("SELECT state FROM drafts").fetchone()[0], "HELD_VERIFICATION")

    def test_at_most_three_new_people_per_company_per_research_run(self):
        """Owner, 2026-09-28: no lifetime limit; at most 3 new people per company per research run."""
        rejected = self.save_drafts([
            self.draft("New " + name, "https://linkedin.com/in/" + name, "Globodyne")
            for name in ("one", "two", "three", "four")])
        self.assertEqual([item["recipient"] for item in rejected], ["New four"])
        self.assertEqual(rejected[0]["reason"], "Research run contact limit")

    def test_simultaneous_launches_only_one_lock_holder(self):
        with runner.single_instance(self.root) as first:
            self.assertTrue(first)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                def attempt():
                    with runner.single_instance(self.root) as acquired:
                        return acquired
                self.assertFalse(pool.submit(attempt).result())
        with runner.single_instance(self.root) as restarted:
            self.assertTrue(restarted)

    def test_claimed_hour_is_never_replayed_after_crash(self):
        slot = runner.dt.datetime.now(runner.dt.timezone.utc).strftime("%Y-%m-%dT%H")
        self.database.execute("INSERT INTO runs(slot,started_at,state) VALUES (?,?,'RUNNING')", (slot, runner.utcnow()))
        self.database.commit()
        with patch.object(runner, "HeyReach") as provider:
            self.assertEqual(runner.run(self.root, {}, self.database, self.pipeline), 0)
            provider.assert_not_called()
        self.assertEqual(self.database.execute("SELECT state FROM runs").fetchone()[0], "INTERRUPTED")

    def test_provider_mutations_are_rejected_before_network(self):
        client = runner.HeyReach.__new__(runner.HeyReach)
        with patch.object(runner.urllib.request, "urlopen") as network:
            for tool in ("start_campaign", "send_message", "add_leads_to_campaign", "create_campaign"):
                with self.assertRaises(ValueError):
                    client.call(tool, {})
            network.assert_not_called()

    def test_missing_provider_history_is_unknown_not_permission(self):
        client = unittest.mock.Mock()
        client.call.side_effect = RuntimeError("offline")
        result = runner.observe(self.database, {"campaign_id": 700001, "sender_id": 424242}, client)
        self.assertTrue(result["errors"])
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in result["contacts"]))
        self.assertEqual(result["provider_mutations"], 0)

    def test_backup_integrity_and_original_preservation(self):
        source_root = self.root / "source"
        for target, relative in runner.SOURCES.items():
            source = source_root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(json.dumps(self.pipeline if target == "pipeline.json" else {"original": True}))
        (self.root / "backups/delivery-evidence.json").unlink()
        originals = {relative: (source_root / relative).read_bytes() for relative in runner.SOURCES.values()}
        runner.snapshot_sources(self.root, {"source_root": str(source_root)})
        runner.snapshot_sources(self.root, {"source_root": str(source_root)})
        for relative, content in originals.items():
            self.assertEqual((source_root / relative).read_bytes(), content)
        (self.root / "backups/pipeline.json").write_text("{}")
        with self.assertRaises(ValueError):
            runner.snapshot_sources(self.root, {"source_root": str(source_root)})

    def test_fresh_root_without_manifest_or_source_root_imports_nothing(self):
        root = Path(tempfile.mkdtemp(prefix="sidedoor-fresh-"))
        self.addCleanup(shutil.rmtree, root, True)
        for directory in ("backups", "state"):
            (root / directory).mkdir()
        pipeline = runner.snapshot_sources(root, {"source_root": None})
        self.assertEqual(pipeline, runner.EMPTY_PIPELINE)
        self.assertFalse((root / "backups/manifest.json").exists())
        database = runner.connect(root)
        self.addCleanup(database.close)
        with patch.object(runner.owner_seed, "items", return_value=[]):  # a stranger has no owner seed
            runner.import_history(database, root, pipeline)
        self.assertEqual(database.execute("SELECT count(*) FROM history").fetchone()[0], 0)
        self.assertEqual(database.execute("SELECT count(*) FROM suppressions").fetchone()[0], 0)

    def test_missing_source_root_path_still_raises(self):
        root = Path(tempfile.mkdtemp(prefix="sidedoor-fresh-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "backups").mkdir()
        with self.assertRaises(OSError):
            runner.snapshot_sources(root, {"source_root": str(root / "no-such-tracker")})
        self.assertFalse((root / "backups/manifest.json").exists())

    def test_invalid_profile_is_rejected(self):
        for profile in ("https://linkedin.com.evil.example/in/someone", "https://linkedin.com/company/example", ""):
            with self.assertRaises(ValueError):
                runner.profile_key(profile)

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_research_timeout_kills_worker_group_and_is_not_retried(self):
        (self.root / "research-prompt.md").write_text("Research only")
        for filename in ("discovery-prompt.md", "discovery-schema.json", "research-schema.json"):
            shutil.copyfile(runner.ROOT / filename, self.root / filename)
        policy = user_profile.load()
        policy["industries"] = ["AI"]
        profile_home.use(self, policy)
        config = {"research_hour": 8, "research_max_attempts": 3, "research_retry_delays_seconds": [900,3600], "research_timeout_seconds": 1,
                  "codex": "/fake/codex", "model": "test", "timezone": "America/Los_Angeles"}
        worker = unittest.mock.Mock(pid=123456)
        worker.poll.return_value = None
        worker.communicate.side_effect = runner.subprocess.TimeoutExpired("codex", 1)
        # A one-second starting run deadline (it is 900 s), so the run ends with this one session.
        with patch.object(runner.subprocess, "Popen", return_value=worker) as launch, patch.object(runner.os, "killpg") as terminate, \
                patch("parallel_research.initial_deadline_seconds", return_value=1):
            result = runner.research(self.root, config, self.database, self.pipeline, {})
            self.assertEqual(result["state"], "FAILED")
            # One research session: the discovery that timed out, never retried and never followed by a
            # qualification. When the timed-out discovery ends a moment before the one-second run deadline,
            # the recovery coordinator may still make its (tools-disabled) decision call, a session of its
            # own with its own process (2026-10-27: that timing race made this test fail alone ~1 in 6).
            # Every session started is killed once, by its own group, and reaped.
            commands = [call.args[0] for call in launch.call_args_list]
            research = [command for command in commands if not any("recovery-schema.json" in part for part in command)]
            self.assertEqual(len(research), 1)
            self.assertTrue(any("discovery-schema.json" in part for part in research[0]))
            self.assertLessEqual(len(commands), 2)
            self.assertEqual(terminate.call_args_list,
                             [unittest.mock.call(worker.pid, runner.signal.SIGKILL)] * len(commands))
            self.assertEqual(worker.wait.call_count, len(commands))
            command = research[0]
            self.assertIn("--ignore-user-config", command)
            self.assertIn("features.shell_tool=false", command)
            self.assertTrue(all(call.kwargs["start_new_session"] for call in launch.call_args_list))
            self.assertEqual(runner.research(self.root, config, self.database, self.pipeline, {})["state"], "EXHAUSTED")
            self.assertIsNone(self.database.execute("SELECT completed_at FROM research_days").fetchone()[0])
            self.assertFalse(result["retryable"])
            self.assertEqual(launch.call_count, len(commands))   # the EXHAUSTED day started nothing

    def test_missing_profile_never_triggers_unfiltered_inbox_read(self):
        self.database.execute("UPDATE history SET profile=NULL WHERE recipient='Remy'")
        self.database.commit()
        client = unittest.mock.Mock()
        client.call.return_value = {"totalCount": 0, "items": []}
        result = runner.observe(self.database, {"campaign_id": 700001, "sender_id": 424242}, client)
        remy = next(contact for contact in result["contacts"] if contact["recipient"] == "Remy")
        self.assertEqual(remy["status"], "UNKNOWN")
        self.assertTrue(any("Remy" in error for error in result["errors"]))
        inbox_calls = [call for call in client.call.call_args_list if call.args[0] == "get_conversations_v2"]
        self.assertEqual(len(inbox_calls), 2)


if __name__ == "__main__":
    unittest.main()
