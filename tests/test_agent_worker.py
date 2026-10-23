import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_worker
import applications
import orchestrator
import reply_worker
import workflow_tasks


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-17T20:25:00+00:00"
DEADLINE = "2026-09-18T20:25:00+00:00"
SOURCE = "https://example.com/jobs/role?gh_jid=123"


class AgentWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("agent-worker-prompt.md", "agent-worker-schema.json"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        self.path = self.root / "tasks.sqlite3"
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        self.addCleanup(lambda: self.database.close())
        workflow_tasks.initialize(self.database)
        self.config = {"codex": "/mock-only/codex", "model": "mock-model", "timezone": "America/Los_Angeles",
                       "daily_compute_seconds": 60,
                       "agent_worker": {"enabled": True, "timeout_seconds": 10, "max_items": 1}}
        self.enterContext(patch.object(agent_worker.time, "monotonic", return_value=0))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("No live process allowed")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("No network allowed")))

    def enqueue(self, action="one", question="Read the existing public role", source=SOURCE):
        with self.database:
            return agent_worker.enqueue(self.database, action, "manual-parent", question,
                                        [{"source": source, "observed_at": NOW}], DEADLINE, NOW)

    def task(self, task_id):
        return dict(self.database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone())

    def reservations(self):
        return [tuple(row) for row in self.database.execute("SELECT * FROM worker_compute_reservations ORDER BY action_id")]

    def emit(self, directory, resolved=True, events=True):
        result = {"status": "RESOLVED" if resolved else "UNRESOLVED", "summary": "Mock public-role finding",
                  "citations": [{"source": SOURCE, "claim": "Mock role title"}],
                  "unresolved_reason": None if resolved else "Current availability is unreadable",
                  "next_action": None if resolved else "Read the official public vacancy listing"}
        (directory / "output.json").write_text(json.dumps(result))
        event = {"type": "item.completed", "item": {"type": "web_search", "status": "completed"}}
        (directory / "trace.jsonl").write_text(json.dumps(event) + "\n" if events else "")

    def resolved(self, config, prompt, directory, timeout):
        self.emit(directory)

    def cycle(self, execute=None, now=NOW):
        return agent_worker.cycle(self.root, self.database, self.config, now=now,
                                  execute=execute if execute is not None else self.resolved)

    def test_resolved_evidence_survives_restart_without_repeat_or_charge(self):
        task_id = self.enqueue()
        self.assertEqual({"COMPLETE": 1}, self.cycle()["counts"])
        saved = self.task(task_id)
        result = json.loads(saved["result"])
        self.assertEqual("COMPLETE", saved["status"])
        self.assertEqual(1, saved["attempts"])
        self.assertFalse(result["can_apply"] or result["can_send"])
        self.assertEqual("INJECTED_EXECUTOR_TRACE", result["trace_basis"])
        self.assertEqual([1], result["web_search_event_lines"])
        self.assertEqual("MODEL_CLAIMS_NOT_INDEPENDENTLY_VERIFIED", result["citation_basis"])
        artifacts = [item for item in json.loads(saved["evidence"]) if item["kind"] == "ATTEMPT_ARTIFACT"]
        self.assertEqual(4, len(artifacts))
        for item in artifacts:
            self.assertEqual(hashlib.sha256(Path(item["source"]).read_bytes()).hexdigest(), item["sha256"])
        budget = self.reservations()
        self.database.close()
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        executor = Mock(side_effect=AssertionError("Completed work cannot replay"))
        self.cycle(executor)
        executor.assert_not_called()
        self.assertEqual(saved, self.task(task_id))
        self.assertEqual(budget, self.reservations())

    def test_unresolved_backoff_exhaustion_preserves_every_attempt(self):
        task_id = self.enqueue()
        executor = Mock(side_effect=lambda config, prompt, directory, timeout: self.emit(directory, resolved=False))
        now = NOW
        for attempt in range(1, 4):
            self.cycle(executor, now)
            task = self.task(task_id)
            self.assertEqual(attempt, task["attempts"])
            self.assertEqual("RETRY_WAIT" if attempt < 3 else "EXHAUSTED", task["status"])
            self.assertEqual("INVESTIGATION_UNRESOLVED", task["blocker"])
            self.assertTrue(json.loads(task["result"])["model_result"]["unresolved_reason"])
            self.cycle(executor, now)
            self.assertEqual(attempt, executor.call_count)
            now = task["next_at"] or now
        self.assertEqual(3, len(list((self.root / "reports/agent-worker" / task_id).glob("*/output.json"))))
        saved = self.database.execute("SELECT COUNT(*) FROM workflow_task_events WHERE event IN ('RETRY_WAIT','EXHAUSTED')").fetchone()[0]
        self.assertEqual(3, saved)
        self.assertEqual(30, sum(row[2] for row in self.reservations()))

    def test_timeout_is_durable_redacted_and_keeps_reserved_budget(self):
        task_id = self.enqueue()
        executor = Mock(side_effect=subprocess.TimeoutExpired("private-command", 10, stderr="private-secret"))
        self.cycle(executor)
        task = self.task(task_id)
        self.assertEqual("RETRY_WAIT", task["status"])
        self.assertEqual("TimeoutExpired", json.loads(task["result"])["failure_type"])
        self.assertNotIn("private-secret", task["result"] + task["evidence"])
        budget = self.reservations()
        self.assertEqual(10, sum(row[2] for row in budget))
        self.cycle(executor)
        self.assertEqual(1, executor.call_count)
        self.assertEqual(budget, self.reservations())

    def test_untrusted_outputs_cannot_fabricate_completion_or_authority(self):
        for failure in ("invalid-json", "no-web-event", "extra-authority", "forbidden-citation"):
            with self.subTest(failure=failure):
                task_id = self.enqueue(failure)

                def invalid(config, prompt, directory, timeout):
                    self.emit(directory, events=failure != "no-web-event")
                    output = directory / "output.json"
                    if failure == "invalid-json":
                        output.write_text("not-json")
                    elif failure in {"extra-authority", "forbidden-citation"}:
                        model = json.loads(output.read_text())
                        if failure == "extra-authority":
                            model["can_send"] = True
                        else:
                            model["citations"][0]["source"] = "https://app.aiapply.co/jobs"
                        output.write_text(json.dumps(model))

                self.cycle(invalid)
                task = self.task(task_id)
                self.assertEqual("RETRY_WAIT", task["status"])
                self.assertFalse(json.loads(task["result"])["can_send"])

    def test_interruption_is_persisted_and_handlers_are_restored(self):
        task_id = self.enqueue()
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
        report = self.cycle(Mock(side_effect=InterruptedError("mock interruption")))
        self.assertEqual("INTERRUPTED", report["state"])
        self.assertEqual("RETRY_WAIT", self.task(task_id)["status"])
        self.assertEqual(handlers, {signum: signal.getsignal(signum) for signum in handlers})

    def test_one_failed_investigation_does_not_stop_the_next(self):
        failed = self.enqueue("failed", question="Mock failure")
        successful = self.enqueue("successful")
        self.config["agent_worker"]["max_items"] = 2

        def executor(config, prompt, directory, timeout):
            if "Mock failure" in prompt:
                raise RuntimeError("mock offline")
            self.emit(directory)

        report = self.cycle(executor)
        self.assertEqual({"RETRY_WAIT": 1, "COMPLETE": 1}, report["counts"])
        self.assertEqual("RETRY_WAIT", self.task(failed)["status"])
        self.assertEqual("COMPLETE", self.task(successful)["status"])

    def test_lease_blocks_second_connection_then_recovers_expired_read(self):
        task_id = self.enqueue()
        claim = workflow_tasks.claim(self.database, task_id, NOW, lease_seconds=10)
        with contextlib.closing(sqlite3.connect(self.path)) as other:
            other.row_factory = sqlite3.Row
            self.assertEqual("RUNNING", workflow_tasks.claim(other, task_id, NOW)["status"])
        executor = Mock(side_effect=self.resolved)
        self.cycle(executor)
        executor.assert_not_called()
        self.assertEqual([], self.reservations())
        later = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=11)).isoformat()
        self.cycle(executor, later)
        self.assertEqual("COMPLETE", self.task(task_id)["status"])
        self.assertEqual(2, self.task(task_id)["attempts"])
        with self.assertRaises(ValueError):
            workflow_tasks.finish(self.database, task_id, claim["token"], later, {}, [])

    def test_shared_budget_exhaustion_preserves_queue_and_prior_reservations(self):
        task_id = self.enqueue()
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "another-worker", NOW, 60,
                                                          daily_seconds=60))
        before = self.task(task_id), self.reservations()
        executor = Mock(side_effect=AssertionError("Budget cannot launch a process"))
        for unused in range(2):
            self.assertEqual("BUDGET_WAIT", self.cycle(executor)["state"])
            self.assertEqual(before, (self.task(task_id), self.reservations()))
        executor.assert_not_called()

    def test_write_and_other_worker_tasks_are_never_consumed(self):
        sources = [{"source": SOURCE, "observed_at": NOW}]
        with self.database:
            write_id = workflow_tasks.enqueue(self.database, agent_worker.WORKER, "write", "parent", sources,
                                              DEADLINE, NOW, effect="WRITE")
            other_id = workflow_tasks.enqueue(self.database, "another-worker", "read", "parent", sources,
                                              DEADLINE, NOW)
        before = self.task(write_id), self.task(other_id)
        executor = Mock(side_effect=AssertionError("Wrong task owner or effect"))
        self.cycle(executor)
        executor.assert_not_called()
        self.assertEqual(before, (self.task(write_id), self.task(other_id)))
        self.assertEqual([], self.reservations())

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_subprocess_is_web_only_filters_environment_and_does_not_cap_cli_state(self):
        process = Mock(returncode=0, pid=987654321)
        environment = {"HOME": "/mock-home", "PATH": "/mock-path", "CODEX_HOME": "/mock-auth",
                       "OPENAI_API_KEY": "never-copy", "CODEX_CONFIG_OVERRIDES": "never-copy"}
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess, "Popen", return_value=process) as spawn, \
                patch.object(agent_worker.os, "killpg") as kill:
            agent_worker._execute(self.config, "Public question", self.root, 10)
        command, options = spawn.call_args.args[0], spawn.call_args.kwargs
        for setting in ('web_search="live"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
                        "features.shell_tool=false", "features.apps=false", "features.skills=false",
                        "features.apply_patch_freeform=false", "features.unified_exec=false",
                        "features.multi_agent=false", "mcp_servers={}"):
            self.assertIn(setting, command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertNotIn("shell", options)
        self.assertEqual({key: environment[key] for key in ("HOME", "PATH", "CODEX_HOME")}, options["env"])
        self.assertNotEqual(self.root, Path(options["cwd"]))
        self.assertFalse(Path(options["cwd"]).exists())
        process.communicate.assert_called_once_with("Public question", timeout=10)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        with patch.object(agent_worker.resource, "setrlimit") as limits:
            options["preexec_fn"]()
        self.assertNotIn(agent_worker.resource.RLIMIT_FSIZE, [call.args[0] for call in limits.call_args_list])
        artifact = self.root / "bounded-artifact"
        artifact.write_bytes(b"12345")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_TOO_LARGE"):
            agent_worker._read(artifact, limit=4)

    @unittest.skipIf(os.name == "nt", "POSIX-only (E2.T3: process groups, rlimits, flock, lsof or symlinks)")
    def test_both_workers_disable_core_dumps_without_limiting_cli_state_files(self):
        for worker in (agent_worker, reply_worker):
            with self.subTest(worker=worker.__name__), patch.object(worker.resource, "setrlimit") as limits:
                worker._limits()
                limits.assert_called_once_with(worker.resource.RLIMIT_CORE, (0, 0))
                self.assertNotIn(worker.resource.RLIMIT_FSIZE, [call.args[0] for call in limits.call_args_list])

    def test_public_query_urls_work_but_excluded_or_private_urls_are_rejected(self):
        task_id = self.enqueue()
        self.assertEqual(SOURCE, json.loads(self.task(task_id)["inputs"])[0]["source"])
        for source in ("https://aiapply.co/jobs", "https://app.aiapply.co/jobs", "https://AIAPPLY.CO./jobs",
                       "https://127.0.0.1/jobs", "https://host.local/jobs", "https://user:pass@example.com/jobs",
                       "file:///tmp/role", "https://example.com/jobs#private"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.enqueue(source=source)
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0])

    def test_disabled_service_hook_does_not_touch_worker_or_database(self):
        with patch.object(agent_worker, "cycle", side_effect=AssertionError("Disabled worker")) as worker, \
                patch.object(agent_worker, "enqueue_pending_applications") as producer:
            for config in ({}, {"agent_worker": {"enabled": False}}):
                self.assertEqual({"state": "DISABLED", "external_writes": 0},
                                 orchestrator.agent_cycle(self.root, config, None))
        worker.assert_not_called()
        producer.assert_not_called()

    def test_enabled_hook_forwards_to_actual_worker_without_other_stages(self):
        task_id = self.enqueue()
        with patch.object(agent_worker, "cycle", wraps=agent_worker.cycle) as worker, \
                patch.object(agent_worker, "_execute", side_effect=self.resolved) as executor, \
                patch.object(agent_worker, "enqueue_pending_applications", return_value=[task_id]) as producer, \
                patch.object(orchestrator, "utcnow", return_value=NOW), \
                patch.object(orchestrator, "service_cycle", side_effect=AssertionError("No other service stages")):
            with self.database:
                self.database.execute("UPDATE workflow_tasks SET deadline=? WHERE task_id=?",
                                      ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), task_id))
            report = orchestrator.agent_cycle(self.root, self.config, self.database)
        worker.assert_called_once_with(self.root, self.database, self.config)
        producer.assert_called_once_with(self.database, NOW, limit=1)
        executor.assert_called_once()
        self.assertEqual({"COMPLETE": 1}, report["counts"])
        self.assertEqual([task_id], report["queued_from_applications"])

    def test_service_hook_failure_is_sanitized_and_other_stages_continue(self):
        self.config["scope"] = "monitor_existing_pipeline"
        with patch.object(agent_worker, "cycle", side_effect=RuntimeError("private failure detail")), \
                patch.object(agent_worker, "enqueue_pending_applications", return_value=[]), \
                patch.object(orchestrator, "worker_step", return_value={"state": "MOCKED"}) as stage, \
                patch.object(orchestrator, "write_json"):
            report = orchestrator.service_cycle(self.root, self.config, self.database)
        self.assertEqual("FAILED", report["agent_investigations"]["state"])
        self.assertEqual("RuntimeError", report["agent_investigations"]["error_type"])
        self.assertNotIn("private failure detail", json.dumps(report))
        self.assertIn("notification-dispatch", [call.args[2] for call in stage.call_args_list])
        self.assertEqual({"state": "MOCKED"}, report["notifications"])

    def test_existing_application_handoff_is_public_only_deduplicated_and_nonmutating(self):
        applications.initialize(self.database)
        self.database.execute("CREATE TABLE opportunities (url TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL)")
        private_values = ["candidate-private-marker", "canonical-private-marker"]
        with self.database:
            self.database.execute("INSERT INTO opportunities VALUES (?,?,?)",
                                  (SOURCE, json.dumps({"candidate_notes": private_values[1]}), NOW))
            for job_key, state, source in (("known-role", "BLOCKED", SOURCE), ("queued-role", "QUEUED", SOURCE),
                                           ("unjoined-role", "BLOCKED", "https://example.com/jobs/unjoined")):
                self.database.execute("""INSERT INTO application_jobs
                    (job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)""",
                    (job_key, "Mock employer", job_key, json.dumps({"url": source, "candidate": private_values[0]}), state, NOW))
        jobs = [tuple(row) for row in self.database.execute("SELECT * FROM application_jobs ORDER BY job_key")]
        opportunities = [tuple(row) for row in self.database.execute("SELECT * FROM opportunities")]
        queued = agent_worker.enqueue_pending_applications(self.database, NOW, limit=4)
        self.assertEqual(1, len(queued))
        task = self.task(queued[0])
        self.assertEqual(("known-role", "READ", "QUEUED", 0),
                         (task["role_id"], task["effect"], task["status"], task["attempts"]))
        self.assertEqual("application-public-evidence:known-role", task["action_id"])
        inputs = json.loads(task["inputs"])
        self.assertEqual(SOURCE, inputs[0]["source"])
        self.assertEqual(NOW, inputs[0]["observed_at"])
        for private in private_values:
            self.assertNotIn(private, json.dumps(task))
        self.database.close()
        self.database = sqlite3.connect(self.path)
        self.database.row_factory = sqlite3.Row
        later = (dt.datetime.fromisoformat(NOW) + dt.timedelta(minutes=1)).isoformat()
        self.assertEqual([], agent_worker.enqueue_pending_applications(self.database, later, limit=4))
        self.assertEqual(task, self.task(queued[0]))
        self.assertEqual(1, self.database.execute("SELECT COUNT(*) FROM workflow_tasks").fetchone()[0])
        self.assertEqual(jobs, [tuple(row) for row in self.database.execute("SELECT * FROM application_jobs ORDER BY job_key")])
        self.assertEqual(opportunities, [tuple(row) for row in self.database.execute("SELECT * FROM opportunities")])
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0])
        self.assertEqual([], self.reservations())

    def test_isolated_harness_without_application_tables_queues_nothing(self):
        before = list(self.database.iterdump())
        self.assertEqual([], agent_worker.enqueue_pending_applications(self.database, NOW))
        self.assertEqual(before, list(self.database.iterdump()))
        applications.initialize(self.database)
        before = list(self.database.iterdump())
        self.assertEqual([], agent_worker.enqueue_pending_applications(self.database, NOW))
        self.assertEqual(before, list(self.database.iterdump()))


if __name__ == "__main__":
    unittest.main()
