"""No usable Codex is a wait, never a launch of a missing program (2026-10-23).

A user's research failed six industries on 0.0.7 with "[Errno 2] No such file or directory:
'/opt/homebrew/bin/codex'": with_codex kept the bundled path when nothing usable was found.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import failures_page
import orchestrator
import engines
import provider_limits
import reply_worker
import setup_steps
import subprocess
import user_profile
import workflow_tasks
import fake_executables
import test_part_c_read_gating as part_c

MISSING = "/nowhere/bin/codex"


def database():
    root = Path(tempfile.mkdtemp())
    (root / "state").mkdir()
    return orchestrator.connect(root)


def missing_config():
    with patch.object(setup_steps, "find_codex", return_value=None), \
            patch.object(setup_steps, "load_state", return_value={}):
        return setup_steps.with_codex({"codex": MISSING, "model": "m", "timezone": "UTC"})


class CodexNotFound(unittest.TestCase):
    def setUp(self):
        self.home = user_profile.home()
        provider_limits.clear(provider_limits.CODEX_WAIT_FILE, self.home)
        self.addCleanup(provider_limits.clear, provider_limits.CODEX_WAIT_FILE, self.home)

    def test_the_config_is_marked_and_codex_kept(self):
        config = missing_config()
        self.assertEqual((config["codex"], config[setup_steps.CODEX_MISSING]), (MISSING, True))   # provenance unchanged

    def test_text_workers_need_no_codex_and_a_codex_wait_spends_no_try(self):
        # The text workers are direct OpenRouter calls (2026-10-27): a missing Codex does not stop them.
        config = dict(missing_config(), text_model={"model": "z-ai/glm-5.3"})
        answer = {"content": "{}", "usage": {}, "finish_reason": "stop", "model": "z-ai/glm-5.3"}
        with patch.object(subprocess, "Popen", side_effect=AssertionError("launched a missing Codex")), \
                patch.object(reply_worker.text_model, "complete", return_value=answer):
            self.assertEqual("{}", reply_worker.execute_text(config, "prompt", "{}", 30))
        wait = engines.engine(config).wait(config)   # what every Codex session still raises
        self.assertEqual(wait.kind, provider_limits.CODEX_NOT_FOUND)
        db = database()
        workflow_tasks.initialize(db)
        with db:
            task = workflow_tasks.enqueue(db, "outreach-draft-composition", "a1", "parent",
                                          [{"source": "s", "observed_at": part_c.NOW}], part_c.at(86400), part_c.NOW,
                                          effect="READ", max_attempts=3)
        for attempt in range(3):   # what every worker does with wait_of (outreach_agent, followups, replies)
            claim = workflow_tasks.claim(db, task, part_c.at(attempt * 10))
            with db:
                workflow_tasks.provider_wait(db, task, claim["token"], part_c.at(attempt * 10 + 1), wait)
            with db:
                db.execute("UPDATE workflow_tasks SET next_at=NULL WHERE task_id=?", (task,))
        row = db.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task,)).fetchone()
        self.assertEqual((row["status"], row["attempts"] - (row["max_attempts"] - 3)), ("RETRY_WAIT", 0))

    def test_research_claims_no_attempt_but_has_its_turn(self):
        import daily_schedule
        db = database()
        config = dict(missing_config(), timezone="America/Los_Angeles", research_hour=9, research_enabled=True,
                      research_max_attempts=3)
        with patch.object(subprocess, "Popen", side_effect=AssertionError("launched a missing Codex")):
            result = orchestrator.research(Path(tempfile.mkdtemp()), config, db, None, None)
        self.assertEqual(result["state"], "CODEX_NOT_FOUND")
        row = db.execute("SELECT state,attempts FROM research_days").fetchone()
        self.assertEqual(tuple(row), ("CODEX_WAIT", 0))
        # The hourly cycle and the applications job no longer yield to research all day.
        self.assertFalse(orchestrator.research_turn_pending(config, db))
        self.assertFalse(orchestrator.research_holds_turn(config, db))
        # Codex found again the same day: research claims its first attempt.
        claim = daily_schedule.claim(db, config, dt.datetime.now(dt.timezone.utc))
        self.assertEqual((claim["state"], claim["attempt"]), ("CLAIMED", 1))

    def test_the_email_check_says_codex_is_missing(self):
        with patch.object(setup_steps, "find_codex", return_value=None), \
                patch.object(setup_steps, "load_state", return_value={}):
            with self.assertRaises(setup_steps.CodexMissingForCheck) as raised:
                setup_steps.read_signed_in_account("https://mail.google.com/", None, Path(tempfile.mkdtemp()),
                                                   config={"codex": MISSING, "model": "m"},
                                                   executor=lambda *a: self.fail("read without Codex"))
        self.assertEqual(str(raised.exception), provider_limits.CODEX_WORDS)

        def missing(*arguments):
            raise setup_steps.CodexMissingForCheck(provider_limits.CODEX_WORDS)
        home = Path(tempfile.mkdtemp())
        state = setup_steps.run_email_check(home, read=missing)
        self.assertEqual((state["steps"]["email"]["status"], state["steps"]["email"]["reason"],
                          state["steps"]["email"]["message"]), ("failed", "codex_missing", provider_limits.CODEX_WORDS))

    def test_banner_and_failure_item_until_a_usable_codex_is_found(self):
        reply = missing_config()
        self.assertIsNotNone(setup_steps.codex_wait(reply))
        [banner] = [entry for entry in provider_limits.banners(self.home) if "Codex" in entry["text"]]
        self.assertEqual((banner["text"], banner["action_href"]),
                         ("Sidedoor can't find Codex. Open Setup to find it again.", "/setup#step-6"))
        view = failures_page.gather(None, {"timezone": "UTC"}, {}, home=self.home)
        [item] = view["needs_you"]
        self.assertEqual((item["text"], item["fix"]["href"], item["code"]),
                         ("Sidedoor can't find Codex. Open Setup to find it again.", "/setup#step-6", "CODEX_NOT_FOUND"))
        found = fake_executables.python(Path(tempfile.mkdtemp()) / "codex", "import sys\nsys.exit(0)\n")
        with patch.object(setup_steps, "load_state", return_value={}):
            config = setup_steps.with_codex({"codex": str(found)}, self.home)
        self.assertNotIn(setup_steps.CODEX_MISSING, config)
        self.assertEqual([entry for entry in provider_limits.banners(self.home) if "Codex" in entry["text"]], [])
        self.assertEqual(failures_page.gather(None, {"timezone": "UTC"}, {}, home=self.home)["needs_you"], [])


class LongWaitsNeverSpendTheDay(unittest.TestCase):
    def test_provider_waits_past_the_cap_still_wait(self):
        import daily_schedule
        db = database()
        config = {"timezone": "America/Los_Angeles", "research_hour": 9, "research_max_attempts": 3,
                  "research_retry_delays_seconds": [900, 3600]}
        daily_schedule.initialize(db, config["timezone"])
        now = dt.datetime.now(dt.timezone.utc)
        for kind, final in ((provider_limits.OUT_OF_CREDIT, "RETRY_WAIT"), ("api_rate_limit", "EXHAUSTED")):
            with db:
                db.execute("DELETE FROM research_days")
                db.execute("INSERT INTO research_days(" + daily_schedule.DAY_COLUMNS + ") VALUES ('d','RUNNING',6,'x',NULL,NULL,3)")
            wait = {"reason": provider_limits.CODE, "provider": {"kind": kind}}
            result = {"state": "PARTIAL", "compute_wait": True, "retryable": True,
                      "next_retry_at": (now + dt.timedelta(hours=1)).isoformat(),
                      "industries": [{"industry": "Fusion", "state": "COMPUTE_WAIT", "compute_wait": wait}]}
            db.execute("INSERT OR IGNORE INTO research VALUES ('x',?,'RUNNING',NULL)", (now.isoformat(),))
            self.assertEqual(daily_schedule.finish(db, config, "x", result, now)["state"], final)


class MultiDayReset(unittest.TestCase):
    def test_a_four_day_usage_reset_spends_no_try(self):
        import daily_schedule
        db = database()
        config = {"timezone": "America/Los_Angeles", "research_hour": 9, "research_max_attempts": 3,
                  "research_retry_delays_seconds": [900, 3600]}
        daily_schedule.initialize(db, config["timezone"])
        now = dt.datetime.now(dt.timezone.utc)
        with db:
            db.execute("INSERT INTO research_days(" + daily_schedule.DAY_COLUMNS + ") VALUES ('d','RUNNING',1,'y',NULL,NULL,0)")
            db.execute("INSERT INTO research VALUES ('y',?,'RUNNING',NULL)", (now.isoformat(),))
        reset = now + dt.timedelta(days=4)
        result = {"state": "PARTIAL", "compute_wait": True, "retryable": True, "next_retry_at": reset.isoformat(),
                  "industries": [{"industry": "Fusion", "state": "COMPUTE_WAIT",
                                  "compute_wait": {"reason": provider_limits.CODE, "provider": {"kind": None}}}]}
        done = daily_schedule.finish(db, config, "y", result, now)
        self.assertEqual((done["state"], done["investigation_attempts"]), ("RETRY_WAIT", 0))
        self.assertEqual(done["next_retry_at"], daily_schedule.next_daily_time(now, config))   # sooner than the reset


class FoundCodexSaved(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.found = str(fake_executables.python(Path(tempfile.mkdtemp()) / "codex", "import sys\nsys.exit(0)\n"))
        setup_steps.update_state(lambda value: value.update(codex_path="/gone/codex"), self.home)

    def test_a_codex_found_elsewhere_is_saved_once(self):
        with patch.object(setup_steps, "find_codex", return_value=self.found):
            config = setup_steps.with_codex({"codex": MISSING}, self.home)
            self.assertEqual(config["codex"], self.found)
            self.assertEqual(setup_steps.load_state(self.home)["codex_path"], self.found)
            with patch.object(setup_steps, "_write_private", side_effect=AssertionError("wrote again")):
                self.assertEqual(setup_steps.with_codex({"codex": MISSING}, self.home)["codex"], self.found)

    def test_a_failed_save_never_stops_the_job(self):
        with patch.object(setup_steps, "find_codex", return_value=self.found), \
                patch.object(setup_steps, "_write_private", side_effect=OSError("disk full")):
            self.assertEqual(setup_steps.with_codex({"codex": MISSING}, self.home)["codex"], self.found)
        self.assertEqual(setup_steps.load_state(self.home)["codex_path"], "/gone/codex")

    def test_nothing_found_keeps_the_saved_value(self):
        with patch.object(setup_steps, "find_codex", return_value=None):
            self.assertTrue(setup_steps.with_codex({"codex": MISSING}, self.home)[setup_steps.CODEX_MISSING])
        self.assertEqual(setup_steps.load_state(self.home)["codex_path"], "/gone/codex")


if __name__ == "__main__":
    unittest.main()
