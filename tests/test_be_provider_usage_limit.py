"""Patch BE: the provider's usage-limit refusal is a capacity WAIT, never a failure.

From midday 2026-09-19 every `codex exec` turn failed with the provider line below. The
coordinator recorded two APPLICATION_RECOVERY_DECISION_FAILED events for Tessier Bio and
abandoned it; browser reads became failed reads and workflow attempts were spent. The fake
executor here prints the exact captured text (stderr form and --json event form) and exits
non-zero, and each test runs the REAL executor code path. Every test fails on the pre-BE code.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import stat
import unittest
from unittest.mock import patch

import application_connection
import application_recovery
import applications
import desktop_adapter
import provider_limits
import reply_worker
import workflow_tasks
import fake_executables
import test_part_c_read_gating as part_c

# The shape of a provider usage-limit failure as codex reports it (executor.log stderr and
# the trace's JSON events); the thread id and reset time are synthetic.
MESSAGE = ("You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
           "credits or try again at Sep 22nd, 2026 11:47 AM.")
STDERR = "ERROR: " + MESSAGE + "\n" + "ERROR: " + MESSAGE + "\n"
EVENTS = "\n".join(json.dumps(event) for event in (
    {"type": "thread.started", "thread_id": "01900000-0000-7000-8000-000000000001"}, {"type": "turn.started"},
    {"type": "error", "message": MESSAGE}, {"type": "turn.failed", "error": {"message": MESSAGE}})) + "\n"
RESET = "2026-09-22T18:47:00+00:00"  # Sep 22nd, 2026 11:47 AM America/Los_Angeles


def fake_codex(directory):
    """An executable that behaves like the refusing CLI: consume stdin, print, exit 1."""
    (directory / "stderr.txt").write_text(STDERR)
    (directory / "events.txt").write_text(EVENTS)
    source = ("import sys\nsys.stdin.read()\n"
              "if '--json' in sys.argv[1:]:\n"
              "    sys.stdout.buffer.write(open(%r, 'rb').read())\n"
              "else:\n"
              "    sys.stderr.buffer.write(open(%r, 'rb').read())\n"
              "sys.exit(1)\n") % (str(directory / "events.txt"), str(directory / "stderr.txt"))
    return str(fake_executables.python(directory / "fake-codex", source))


class ProviderUsageLimitTests(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def config(self):
        return dict(part_c.RECOVERY, codex=fake_codex(self.root), model="gpt-test",
                    application_browser={"enabled": True, "timeout_seconds": 60}, timezone="America/Los_Angeles")

    def events(self, kind):
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT detail FROM application_events WHERE job_key=? AND event=? ORDER BY id", (self.key, kind))]

    def test_coordinator_refusal_is_a_wait_not_a_decision_failure(self):
        config = self.config()
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, config, clock=lambda: part_c.NOW)
        # The refusal is classified against the REAL clock when no `now` is passed, which is right
        # in production. This test pins everything else to part_c.NOW and a fixed stated reset, so
        # without pinning this too it passed only while real time was before RESET and failed from
        # 2026-09-22 18:47Z onward - the stated reset looked past, and a backoff from real now won.
        real = provider_limits._utc
        with patch.object(provider_limits, "_utc", lambda now: real(part_c.NOW if now is None else now)):
            for _ in range(2):  # the pre-BE code abandoned the job on the second refusal
                application_recovery.recover(self.database, self.job(), adapter, config, "REQUISITION_MISMATCH",
                                             "Resolve the requisition", part_c.NOW)
        job = self.job()
        self.assertEqual((job["state"], job["blocker"]), ("BLOCKED", provider_limits.CODE))
        # No recovery deadline clamps next_at any more (owner, 2026-09-24), so the provider's
        # own reset time is the whole answer.
        self.assertEqual(job["next_at"], RESET)
        self.assertEqual(self.events("APPLICATION_RECOVERY_DECISION_FAILED"), [])
        self.assertEqual(self.events("APPLICATION_ABANDONED"), [])
        waits = self.events(provider_limits.EVENT)
        self.assertEqual([wait["resume_at"] for wait in waits], [RESET])  # the second call was gated, not re-run
        self.assertTrue(provider_limits.decision_limited({"artifacts": waits[0]["artifacts"]}))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    def test_browser_read_refusal_is_a_wait_and_spends_no_inspect_claim(self):
        config = self.config()
        # The real consumer leases against the wall clock, so this adapter uses it too.
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, config, clock=applications.stamp)
        with self.assertRaises(desktop_adapter.ProviderUsageWait) as raised:
            adapter.call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(raised.exception.code, provider_limits.CODE)
        if applications.stamp() < RESET:
            self.assertEqual(raised.exception.next_at, RESET)
        request = self.database.execute("SELECT state,claims,result FROM desktop_requests").fetchone()
        self.assertEqual(request["state"], "CONSUMED")
        self.assertEqual(json.loads(request["result"])["error"]["code"], provider_limits.CODE)
        self.assertTrue(provider_limits.stored_result_limited(request["result"]))
        self.assertEqual([wait["resume_at"] for wait in self.events(provider_limits.EVENT)], [raised.exception.next_at])
        # A second call is gated: no new read is claimed and the saved refusal stays a wait.
        with self.assertRaises(desktop_adapter.ProviderUsageWait):
            adapter.call("inspect", {"job": self.job(), "read_only": True})
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 1)
        # The same refusal recorded before BE (a generic executor failure whose trace holds the
        # provider line) is also not a read.
        trace = self.root / "legacy-trace.jsonl"
        trace.write_text(EVENTS)
        self.assertTrue(provider_limits.stored_result_limited(
            {"error": {"code": "APPLICATION_BROWSER_EXECUTOR_FAILED", "trace_path": str(trace)}}))

    def test_workflow_attempt_refused_by_provider_is_not_counted(self):
        with self.assertRaises(reply_worker.Held) as raised:
            reply_worker._execute(self.config(), "prompt", "{}", 30)
        wait = provider_limits.wait_of(raised.exception)
        self.assertEqual(raised.exception.code, "SUBSCRIPTION_EXECUTOR_FAILED")  # unchanged for other callers
        self.assertEqual(wait.stated_reset, RESET)
        workflow_tasks.initialize(self.database)
        with self.database:
            task = workflow_tasks.enqueue(self.database, "outreach-draft-inspection", "a1", "parent",
                                          [{"source": "s", "observed_at": part_c.NOW}], part_c.at(86400), part_c.NOW,
                                          effect="READ", max_attempts=3)
        for attempt in range(3):
            claim = workflow_tasks.claim(self.database, task, part_c.at(attempt * 10))
            self.assertEqual(claim["status"], "CLAIMED")
            with self.database:
                workflow_tasks.provider_wait(self.database, task, claim["token"], part_c.at(attempt * 10 + 1), wait)
            with self.database:  # skip ahead past the reset
                self.database.execute("UPDATE workflow_tasks SET next_at=NULL WHERE task_id=?", (task,))
        row = self.database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task,)).fetchone()
        self.assertEqual((row["status"], row["blocker"], row["attempts"], row["max_attempts"]),
                         ("RETRY_WAIT", provider_limits.CODE, 3, 6))
        self.assertEqual(workflow_tasks.claim(self.database, task, part_c.at(60))["status"], "CLAIMED")


if __name__ == "__main__":
    unittest.main()
