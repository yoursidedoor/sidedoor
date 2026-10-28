"""429 Too Many Requests is a short wait, and AI session starts are paced per provider (owner, 2026-10-27)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import application_connection
import applications
import daily_schedule
import desktop_adapter
import orchestrator
import provider_limits
import provider_pacing
import fake_executables
import test_be_provider_usage_limit as usage
import test_daily_schedule
import test_agent_worker
import agent_worker
import application_browser
import workflow_tasks

# The line codex printed on 2026-10-26 when a research discovery session failed for the day.
LINE = "ERROR: exceeded retry limit, last status: 429 Too Many Requests\n"
NOW = dt.datetime(2026, 10, 26, 16, 0, tzinfo=dt.timezone.utc)


def refusing_codex(directory, stderr=LINE * 2):
    (directory / "stderr.txt").write_text(stderr)
    source = ("import sys\nsys.stdin.read()\nsys.stderr.buffer.write(open(%r, 'rb').read())\nsys.exit(1)\n"
              % str(directory / "stderr.txt"))
    return str(fake_executables.python(directory / "fake-codex", source))


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def time(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class RateLimitWaitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.log = self.root / "discovery.log"
        self.log.write_text("prompt echo\n" + LINE + LINE)

    def test_the_429_line_is_a_short_wait_that_backs_off_to_five_minutes(self):
        delays = []
        for _ in range(6):
            limit = provider_limits.check(1, self.log, now=NOW)
            self.assertEqual((limit.kind, limit.detail()["counts_as_attempt"]), (provider_limits.RATE_LIMITED, False))
            delays.append((dt.datetime.fromisoformat(limit.next_at) - NOW).total_seconds())
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300])
        provider_limits.succeeded(["codex", "exec"])   # a good session ends the streak
        self.assertEqual((dt.datetime.fromisoformat(provider_limits.check(1, self.log, now=NOW).next_at) - NOW).seconds, 30)
        # No ChatGPT "usage limit reached" banner: nothing is shown for a short slow-down.
        self.assertEqual(provider_limits.banners(), [])

    def test_a_stated_retry_time_is_used(self):
        self.log.write_text("ERROR: 429 Too Many Requests: Rate limit reached. Please try again in 20s.\n")
        limit = provider_limits.check(1, self.log, now=NOW)
        self.assertEqual(dt.datetime.fromisoformat(limit.next_at) - NOW, dt.timedelta(seconds=20))

    def test_the_research_worker_launch_reports_the_wait_and_frees_its_slot(self):
        command = [refusing_codex(self.root), "exec"]
        with self.assertRaises(RuntimeError) as raised, \
                patch.object(provider_pacing, "acquire", wraps=provider_pacing.acquire) as paced:
            orchestrator.run_research_worker(self.root, command, "prompt", self.root / "worker.log", 30)
        self.assertEqual(paced.call_args.args[0], "openai")   # the launch took a slot first
        limit = provider_limits.wait_of(raised.exception)
        self.assertEqual(limit.kind, provider_limits.RATE_LIMITED)
        status = provider_pacing.status("openai")
        self.assertEqual((status["limit"], status["running"], status["streak"]), (1, 0, 1))


class DirectOpenRouterTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "key").write_text("sk-synthetic-key-0000")
        self.config = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(folder / "key")},
                       "model": "z-ai/glm-5.3", "codex": "NEVER_EXECUTED", "timezone": "UTC"}

    def test_a_direct_429_is_the_short_wait_and_the_call_holds_an_openrouter_slot(self):
        import text_model
        seen = []
        def transport(url, data, headers, timeout):
            seen.append(provider_pacing.status("openrouter")["running"])
            return 429, b'{"error": {"code": 429, "message": "Rate limit exceeded"}}'
        before = dt.datetime.now(dt.timezone.utc)
        with self.assertRaises(text_model.TextModelHTTPError) as raised:
            text_model.complete(self.config, "prompt", transport=transport)
        limit = provider_limits.wait_of(raised.exception)
        self.assertEqual(limit.kind, provider_limits.RATE_LIMITED)
        self.assertTrue(25 <= (dt.datetime.fromisoformat(limit.next_at) - before).total_seconds() <= 40)
        self.assertEqual(seen, [1])   # the call ran inside its slot
        status = provider_pacing.status("openrouter")
        self.assertEqual((status["running"], status["streak"], status["limit"]), (0, 1, 1))
        # A key under test (Setup, Settings, doctor) is neither paced nor turned into a wait.
        with self.assertRaises(text_model.TextModelHTTPError) as raised:
            text_model.complete(self.config, "prompt", transport=transport, api_key="sk-test")
        self.assertIsNone(provider_limits.wait_of(raised.exception))
        self.assertEqual(seen[-1], 0)

    def test_a_direct_call_waits_at_most_its_cap_then_is_the_short_wait(self):
        import text_model
        provider_pacing.rate_limited("openrouter", 1000)
        with patch.object(text_model, "SLOT_WAIT_SECONDS", 0), \
                self.assertRaises(text_model.TextModelHTTPError) as raised:
            text_model.complete(self.config, "prompt", transport=lambda *a: self.fail("the call was sent"))
        limit = provider_limits.wait_of(raised.exception)
        self.assertEqual((limit.kind, (limit.resume_at - dt.datetime.fromisoformat(limit.observed_at)).seconds),
                         (provider_limits.RATE_LIMITED, 300))


class ResearchDayTests(unittest.TestCase):
    setUp = test_daily_schedule.ScheduleTests.setUp
    at = test_daily_schedule.ScheduleTests.at

    def test_rate_limit_waits_never_spend_the_days_tries(self):
        at = self.at("2026-10-26T08:00:12")
        for _ in range(8):   # past research_max_attempts
            claim = daily_schedule.claim(self.database, self.config, at)
            self.assertEqual(claim["state"], "CLAIMED")
            retry = at + dt.timedelta(seconds=30)
            wait = {"reason": provider_limits.CODE, "provider": {"kind": provider_limits.RATE_LIMITED}}
            state = daily_schedule.finish(self.database, self.config, claim["id"], {
                "state": "FAILED", "retryable": True, "compute_wait": True, "next_retry_at": retry.isoformat(),
                "industries": [{"state": "COMPUTE_WAIT", "compute_wait": wait}]}, at)
            self.assertEqual((state["state"], state["investigation_attempts"]), ("RETRY_WAIT", 0))
            self.assertEqual(state["next_retry_at"], retry.isoformat())
            at = retry


    def test_a_429_retry_already_passed_when_the_run_finishes_is_retry_now(self):
        # 2026-10-26: industry A got a 429 at 10:00:00 (resume 10:00:30); the run finished at 10:12.
        for resume in (self.at("2026-10-26T10:00:30"), self.at("2026-10-26T10:00:00.020000")):   # also a stated 20 ms
            with self.database:
                self.database.execute("DELETE FROM research_days")
            claim = daily_schedule.claim(self.database, self.config, self.at("2026-10-26T10:00:00"))
            finished = self.at("2026-10-26T10:12:00")
            wait = {"reason": provider_limits.CODE, "provider": {"kind": provider_limits.RATE_LIMITED}}
            state = daily_schedule.finish(self.database, self.config, claim["id"], {
                "state": "FAILED", "retryable": True, "compute_wait": True, "next_retry_at": resume.isoformat(),
                "industries": [{"state": "COMPUTE_WAIT", "compute_wait": wait}]}, finished)
            self.assertEqual((state["state"], state["investigation_attempts"], state["next_retry_at"]),
                             ("RETRY_WAIT", 0, finished.isoformat()))


class SlotBeforeClaimTests(unittest.TestCase):
    """The pacing slot is taken before a lease starts, so a slot wait never eats a claim's lease."""

    def record(self, events):
        real_take = provider_pacing.take

        def take(*args, **kwargs):
            events.append("slot")
            held = real_take(*args, **kwargs)
            real_release = held.release
            held.release = lambda: (events.append("release"), real_release())
            return held
        return patch.object(provider_pacing, "take", take)

    def test_an_application_read_claims_only_after_its_slot(self):
        case = ApplicationJobTests("test_browser_read_refusal_is_a_wait_and_spends_no_inspect_claim")
        case.setUp()
        self.addCleanup(case.doCleanups)
        events = []
        real_claim = desktop_adapter.claim
        def claim(*args, **kwargs):
            events.append("claim")
            return real_claim(*args, **kwargs)
        def executor(config, prompt, directory, timeout):
            events.append("session")
            raise RuntimeError("stop here")
        request = application_connection.HostedDesktopAdapter(case.database, case.root, case.config(),
                                                              clock=applications.stamp)
        with self.record(events), patch.object(desktop_adapter, "claim", claim), \
                patch.object(application_browser, "execute", executor):
            try:
                request.call("inspect", {"job": case.job(), "read_only": True})
            except Exception:
                pass
        self.assertEqual(events[:3], ["slot", "claim", "session"])
        self.assertEqual(events[-1], "release")

    def test_an_agent_task_claims_only_after_its_slot(self):
        case = test_agent_worker.AgentWorkerTests("test_resolved_evidence_survives_restart_without_repeat_or_charge")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.enqueue()
        events = []
        real_claim = workflow_tasks.claim
        def claim(*args, **kwargs):
            events.append("claim")
            return real_claim(*args, **kwargs)
        def execute(config, prompt, directory, timeout):
            events.append("session")
            case.emit(directory)
        with self.record(events), patch.object(workflow_tasks, "claim", claim):
            self.assertEqual({"COMPLETE": 1}, case.cycle(execute)["counts"])
        self.assertEqual(events, ["slot", "claim", "session", "release"])


class SlotOutsideTheBrowserLockTests(unittest.TestCase):
    def setUp(self):
        self.case = ApplicationJobTests("test_browser_read_refusal_is_a_wait_and_spends_no_inspect_claim")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def adapter(self):
        return application_connection.HostedDesktopAdapter(self.case.database, self.case.root, self.case.config(),
                                                           clock=applications.stamp)

    def test_the_slot_is_waited_for_before_the_browser_lock(self):
        import run_locks
        held = []
        real_take = provider_pacing.take
        def take(*args, **kwargs):
            held.append(run_locks.browser_held_here())
            return real_take(*args, **kwargs)
        with patch.object(provider_pacing, "take", take), self.assertRaises(desktop_adapter.ProviderUsageWait):
            self.adapter().call("inspect", {"job": self.case.job(), "read_only": True})
        self.assertEqual(held, [False])

    def test_a_slot_wait_past_its_bound_is_a_short_wait_with_nothing_claimed(self):
        provider_pacing.rate_limited("openai", 1000)
        with patch.object(provider_pacing, "BROWSER_SLOT_WAIT_SECONDS", 0), \
                self.assertRaises(desktop_adapter.ProviderUsageWait) as raised:
            self.adapter().call("inspect", {"job": self.case.job(), "read_only": True})
        self.assertTrue(raised.exception.next_at)
        self.assertEqual(self.case.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)

    def test_a_busy_browser_releases_the_slot_within_seconds_with_nothing_created(self):
        import run_locks
        import threading
        taken, done = threading.Event(), threading.Event()
        def contact_finder():   # another thread holds the one Chrome, as the contact finder would
            with run_locks.browser_session(self.case.root) as free:
                self.assertTrue(free)
                taken.set()
                done.wait(30)
        holder = threading.Thread(target=contact_finder)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(done.set)
        taken.wait(10)
        adapter = self.adapter()
        self.assertGreater(adapter.browser_wait_seconds, provider_pacing.BROWSER_LOCK_WAIT_SECONDS)
        with patch.object(provider_pacing, "BROWSER_LOCK_WAIT_SECONDS", 0.2), \
                self.assertRaises(application_connection.CapacityWait) as raised:
            adapter.call("inspect", {"job": self.case.job(), "read_only": True})
        self.assertEqual(raised.exception.code, application_connection.BROWSER_BUSY_CODE)
        self.assertEqual(provider_pacing.status("openai")["running"], 0)   # the slot is released
        self.assertEqual(self.case.database.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)
        self.assertEqual(self.case.database.execute("SELECT COUNT(*) FROM worker_compute_reservations").fetchone()[0], 0)

    def test_nothing_claimable_takes_no_slot(self):
        with patch.object(provider_pacing, "take", side_effect=AssertionError("a slot was taken")):
            result = application_browser.consume(self.case.root, self.case.database, self.case.config(), "no-such-request")
        self.assertEqual(result["state"], "NOT_CLAIMED")


class AgentSlotAcrossDayRolloverTests(unittest.TestCase):
    def test_a_slot_wait_across_the_budget_day_rollover_still_runs_the_task(self):
        case = test_agent_worker.AgentWorkerTests("test_resolved_evidence_survives_restart_without_repeat_or_charge")
        case.setUp()
        self.addCleanup(case.doCleanups)
        task_id = case.enqueue()
        real_take = provider_pacing.take
        def take(*args, **kwargs):
            agent_worker.time.monotonic.return_value += 30   # the wait crosses the 09:00 Los Angeles budget-day rollover
            return real_take(*args, **kwargs)
        with patch.object(provider_pacing, "take", take):
            report = agent_worker.cycle(case.root, case.database, case.config, now="2026-09-18T15:59:50+00:00",
                                        execute=case.resolved)
        self.assertEqual(report["counts"], {"COMPLETE": 1})
        self.assertEqual(case.task(task_id)["attempts"], 1)


class ApplicationJobTests(unittest.TestCase):
    setUp = usage.ProviderUsageLimitTests.setUp
    job = usage.ProviderUsageLimitTests.job

    def config(self):
        return dict(usage.ProviderUsageLimitTests.config(self), codex=refusing_codex(self.root))

    def test_browser_read_refusal_is_a_wait_and_spends_no_inspect_claim(self):
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, self.config(),
                                                              clock=applications.stamp)
        before = dt.datetime.now(dt.timezone.utc)
        with self.assertRaises(desktop_adapter.ProviderUsageWait) as raised:
            adapter.call("inspect", {"job": self.job(), "read_only": True})
        seconds = (dt.datetime.fromisoformat(raised.exception.next_at) - before).total_seconds()
        self.assertTrue(25 <= seconds <= 120, seconds)
        request = self.database.execute("SELECT state,result FROM desktop_requests").fetchone()
        self.assertEqual(json.loads(request["result"])["error"]["code"], provider_limits.CODE)
        self.assertTrue(provider_limits.stored_result_limited(request["result"]))


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch.object(provider_pacing, "_clock", self.clock.time)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ[provider_pacing.GAP_VARIABLE] = "0"
        self.addCleanup(os.environ.__setitem__, provider_pacing.GAP_VARIABLE, "0")

    def take(self, count):
        return [provider_pacing.acquire("openai", 60, sleep=self.fail_sleep) for _ in range(count)]

    def fail_sleep(self, seconds):
        raise AssertionError("waited %s s" % seconds)

    def test_unlimited_until_a_429_then_halved_then_plus_one_per_success_then_unlimited_after_an_hour(self):
        leases = self.take(8)   # no cap: eight start at once
        self.assertEqual(provider_pacing.rate_limited("openai"), 30)
        self.assertEqual(provider_pacing.status("openai")["limit"], 4)
        for lease in leases:
            provider_pacing.release("openai", lease)
        self.clock.sleep(30)    # the cool-down
        held = self.take(4)
        with self.assertRaises(AssertionError):
            self.take(1)        # the fifth waits
        provider_pacing.succeeded("openai")
        self.assertEqual(provider_pacing.status("openai")["limit"], 5)
        held += self.take(1)
        self.clock.sleep(provider_pacing.CALM_SECONDS)
        self.assertIsNone(provider_pacing.status("openai")["limit"])
        self.take(10)

    def test_a_429_holds_new_starts_for_the_cool_down(self):
        provider_pacing.rate_limited("openai")
        self.assertEqual(provider_pacing.status("openai")["limit"], 1)
        started = self.clock.time()
        provider_pacing.acquire("openai", 60, sleep=self.clock.sleep)
        self.assertEqual(self.clock.time() - started, 30)
        provider_pacing.acquire("openrouter", 60, sleep=self.fail_sleep)   # another provider is not held

    def test_starts_are_three_seconds_apart(self):
        os.environ[provider_pacing.GAP_VARIABLE] = str(provider_pacing.MIN_GAP_SECONDS)
        times = []
        for _ in range(3):
            provider_pacing.acquire("openai", 60, sleep=self.clock.sleep)
            times.append(self.clock.time())
        self.assertEqual([later - earlier for earlier, later in zip(times, times[1:])], [3, 3])

    def test_a_clock_moved_back_does_not_stall_starts(self):
        provider_pacing.rate_limited("openai")   # 30 s cool-down
        self.clock.value -= 7200
        started = self.clock.time()
        provider_pacing.acquire("openai", 60, sleep=self.clock.sleep)
        self.assertLessEqual(self.clock.time() - started, 30)

    def test_a_malformed_field_is_dropped_not_pacing_disabled(self):
        provider_pacing.acquire("openai", 60, sleep=self.fail_sleep)   # creates the file
        path = provider_pacing._path(None)
        state = json.loads(path.read_text())
        lease = next(iter(state["openai"]["leases"]))
        state["openai"]["leases"][lease]["expires"] = "bad"
        state["openai"]["limit"] = "x"
        path.write_text(json.dumps(state))
        provider_pacing.rate_limited("openai")   # still recorded: pacing still works
        cleaned = json.loads(path.read_text())["openai"]
        self.assertEqual((cleaned["leases"], cleaned["limit"], cleaned["streak"]), ({}, 1, 1))

    def test_a_wait_past_max_wait_is_the_short_rate_limit_wait(self):
        provider_pacing.rate_limited("openai", 1000)   # a stated long retry
        with patch.object(provider_pacing.time, "sleep", self.clock.sleep), \
                self.assertRaises(provider_limits.ProviderUsageLimit) as raised:
            provider_pacing.take(["codex"], 60, max_wait=60)
        self.assertEqual(raised.exception.kind, provider_limits.RATE_LIMITED)
        self.assertEqual((raised.exception.resume_at - dt.datetime.fromisoformat(raised.exception.observed_at)).seconds, 300)

    def test_a_crashed_or_expired_lease_is_reclaimed(self):
        provider_pacing.rate_limited("openai")   # limit 1
        provider_pacing.acquire("openai", 60, sleep=self.clock.sleep)
        state = json.loads((provider_pacing._path(None)).read_text())
        lease = next(iter(state["openai"]["leases"]))
        state["openai"]["leases"][lease]["pid"] = 2 ** 22 + 12345   # a process that is gone
        provider_pacing._path(None).write_text(json.dumps(state))
        provider_pacing.acquire("openai", 60, sleep=self.fail_sleep)
        self.clock.sleep(60 + provider_pacing.LEASE_MARGIN_SECONDS + 1)   # this one's lease expired
        provider_pacing.acquire("openai", 60, sleep=self.fail_sleep)


if __name__ == "__main__":
    unittest.main()
