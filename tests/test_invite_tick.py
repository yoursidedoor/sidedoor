"""The invitation tick (owner, 2026-10-24, option c: "run the invitation step every 15 minutes instead
of hourly"): `sidedoor job invites` runs only the hourly round's invitation step (sequence.reconcile_invites
and the INVITE pass of outreach.cycle) every 15 minutes, under state/runner.lock, with every gate,
claim and pacing rule unchanged. FakePort only; nothing reaches a provider."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import datetime as dt
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import orchestrator
import outreach
import outreach_agent
import pacing
import research_workflow as research
import scheduler_control
import scheduler_loop
import sequence
from linkedin_port import FakePort


ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("America/Los_Angeles")
UNIPILE = {"timezone": "America/Los_Angeles", "research_dispatch_interval_seconds": 300, "applications_minute": 30,
           "linkedin_provider": "unipile"}
REAL_CHECK = pacing.check


def wall():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def evidence(field, value, at, source):
    return {"field": field, "value": value, "source": source, "observed_at": at, "verified": True,
            "excerpt": "MOCK fixture only", "kind": "official"}


class Clock:
    """A simulated time for pacing and the invitation records (`sequence._now`); gates keep the wall clock."""
    def __init__(self, start):
        self.now = start

    def iso(self):
        return self.now.isoformat()


class TickBase(unittest.TestCase):
    PEOPLE = 1

    def setUp(self):
        self.enterContext(patch("orchestrator.linkedin_port_for", side_effect=lambda config: self.port))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = sqlite3.connect(self.root / "fixture.sqlite", check_same_thread=False)
        self.addCleanup(lambda: self.database.close())
        self.database.row_factory = sqlite3.Row
        at = wall()
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        companies = ["Mock Company %d" % index for index in range((self.PEOPLE + 2) // 3)]
        research.plan_research(self.database, "plan", companies, now=at)
        self.port = FakePort()
        self.profiles = []
        roles = []
        for number, company in enumerate(companies):
            source = "https://example.test/jobs/%d" % number
            people = []
            for index in range(min(3, self.PEOPLE - 3 * number)):
                profile = "https://www.linkedin.com/in/mock-invitee-%d-%d" % (number, index)
                self.profiles.append(profile)
                self.port.add_person(profile)
                people.append({"recipient": "Mock Invitee%d %s" % (index, "ABCDEFGH"[number]), "profile": profile,
                               "employer_matches_target": True, "evidence": [
                                   evidence("identity", profile, at, source), evidence("employment", company, at, source),
                                   evidence("team", "API team", at, source),
                                   evidence("ability_to_help", "Runs team hiring", at, source),
                                   evidence("contact_type", "hiring_manager", at, source)]})
            roles.append({"company": company, "role": "Staff Software Engineer", "url": source,
                          "requisition_id": str(number), "rationale": "MOCK only", "people": people,
                          "evidence": [evidence("team_need", "reliable APIs", at, source),
                                       evidence("specialization", "backend", at, source)]})
        research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock", "blockers": [],
                                                  "opportunities": roles, "drafts": []}, plan_id="plan", now=at)
        self.config = {"sender_id": 900001, "sender_profile": "https://www.linkedin.com/in/mock-sender",
                       "linkedin_provider": "unipile", "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "INVITE"],
                       "outreach_history_max_age_seconds": 3600, "timezone": "America/Los_Angeles",
                       "outreach_agent": {"enabled": True, "max_items": 7}}
        self.database.commit()
        self.client = Mock(spec=["call", "_request"])
        self.enterContext(patch.object(orchestrator, "HeyReach", return_value=self.client))
        self.enterContext(patch.object(outreach_agent.Adapter, "inspect",
                                       side_effect=lambda action, config: self.proof(action)))

    def proof(self, action):
        return {"identity": outreach.profile_key(action["profile"]), "sender_id": 900001, "checked_at": wall(),
                "evidence": "mock:authenticated-history", "sender_ready": True, "entitlement_verified": True,
                "direct_messages_complete": True, "sent_invitations_complete": True, "pending_invitation": False,
                "cooldown": False, "prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False,
                "conversation_id": None, "route": "INVITATION", "company_contact_count": 0,
                "company_count_evidence": "mock:company-history", "followups_sent": 0, "initial_message_sent": False,
                "agent_decision": "READY", "provider_snapshot_complete": True, "next_action": "Use the shared action guard"}

    def promote(self):
        with patch("outreach_campaign.prepare_candidates", side_effect=AssertionError("No HeyReach campaign")):
            for unused in range(5):
                orchestrator.draft_promotion_cycle(self.database, self.config, port=self.port)
                if self.queued() >= self.PEOPLE:
                    break
        self.assertEqual(self.queued(), self.PEOPLE)

    def queued(self):
        return self.database.execute("SELECT COUNT(*) FROM outreach_actions WHERE "
                                     "json_extract(payload,'$.purpose')='INVITE'").fetchone()[0]

    def hourly_round(self):
        """The round's own step, as `run` runs it under state/runner.lock."""
        return orchestrator.new_contact_execution_cycle(ROOT, self.database, self.config, port=self.port)

    def tick(self):
        return orchestrator.invite_tick(ROOT, self.config, self.database, port=self.port)

    def sends(self):
        return self.port.calls.count("send_invitation")


class ExactlyOneSend(TickBase):
    def test_lock_and_call_inventory(self):
        """Every caller of the INVITE dispatch, and the lock each holds: the hourly round
        (`run` -> new_contact_execution_cycle, inside `single_instance`, state/runner.lock) and the tick
        (`job invites` -> invite_tick, inside the same `single_instance`)."""
        source = Path(orchestrator.__file__).read_text()
        self.assertEqual(source.count('outreach.cycle(database, config, adapter, purpose="INVITE")'), 1)  # the tick
        self.assertIn('for purpose in ("INITIAL", "FIRST_MESSAGE", "PREPARE_CAMPAIGN") + (("INVITE",) if unipile else ())',
                      source)  # the round
        self.assertEqual(source.count("sequence.reconcile_invites(database, port, config=config)"), 2)
        held = []
        real = orchestrator.single_instance

        @contextlib.contextmanager
        def spy(root):
            with real(root) as acquired:
                held.append(acquired)
                yield acquired
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(self.root)}), patch.object(orchestrator, "single_instance", spy), \
                patch.object(orchestrator, "invite_tick", return_value={"state": "NOTHING_DUE"}) as tick:
            self.assertEqual(orchestrator.main(["job", "invites"]), 0)
        self.assertEqual(held, [True])
        tick.assert_called_once()

    def test_round_and_tick_at_the_same_moment_make_one_provider_call(self):
        self.promote()
        start = threading.Event()
        release = threading.Event()
        home = self.root / "home"
        home.mkdir()
        out = io.StringIO()

        def round_under_lock():
            import paths
            with orchestrator.single_instance(paths.data(home)) as acquired:
                self.assertTrue(acquired)
                start.set()
                release.wait(10)
                with patch("pacing.check", return_value={"go": True}):
                    self.hourly_round()
        with patch.dict(os.environ, {"SIDEDOOR_HOME": str(home)}):
            import paths
            paths.prepare()
            worker = threading.Thread(target=round_under_lock)
            worker.start()
            self.assertTrue(start.wait(10))
            # The tick starts while the round holds state/runner.lock: it skips, quietly.
            with patch.object(orchestrator, "invite_tick", side_effect=AssertionError("ran beside the round")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                self.assertEqual(orchestrator.main(["job", "invites"]), 0)
            release.set()
            worker.join(10)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.sends(), 1)
        # And with no lock at all, back to back: the claim and the pending-invitation read refuse a second send.
        with patch("pacing.check", return_value={"go": True}):
            self.tick()
            self.tick()
            self.hourly_round()
        self.assertEqual(self.sends(), 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 1)

    def test_tick_waits_for_pause_and_research_and_never_sends_an_approval_hold(self):
        self.promote()
        with patch("pacing.check", return_value={"go": True}):
            scheduler_control.set_user_pause(self.database, True, "test")
            self.assertEqual(self.tick()["state"], "PAUSED")
            scheduler_control.set_user_pause(self.database, False)
            with patch.object(orchestrator, "research_holds_turn", return_value=True):
                self.assertEqual(self.tick()["state"], "YIELDED_TO_DAILY_RESEARCH")
            self.assertEqual(self.sends(), 0)
            with patch("approvals.hold_reason", return_value="REVIEW_MODE"):
                self.tick()
            self.assertEqual(self.sends(), 0)
            self.assertEqual(self.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "AWAITING_APPROVAL")
            self.tick()   # never selected again while it waits for approval
            self.assertEqual(self.sends(), 0)

    def test_nothing_due_builds_no_port_and_logs_nothing(self):
        with patch.object(orchestrator, "unipile_ready", side_effect=AssertionError("port built")):
            self.assertEqual(self.tick(), {"state": "NOTHING_DUE", "queued": 0})
        self.promote()
        with patch("pacing.check", return_value={"go": False, "reason": "JITTER", "wait_until": wall()}), \
                patch.object(orchestrator, "unipile_ready", side_effect=AssertionError("port built")):
            self.assertEqual(self.tick(), {"state": "NOTHING_DUE", "queued": 1})
        self.assertFalse(orchestrator.invite_tick(ROOT, dict(self.config, linkedin_provider="heyreach"),
                                                  self.database).get("logged"))


class PacingOverAnHour(TickBase):
    PEOPLE = 6

    def test_six_queued_invites_over_an_hour_of_ticks(self):
        self.promote()
        clock = Clock(dt.datetime(2026, 10, 22, 17, 0, tzinfo=dt.timezone.utc))   # 10:00 Pacific, the round
        self.enterContext(patch.object(sequence, "_now", side_effect=clock.iso))
        self.enterContext(patch("pacing.check", side_effect=lambda database, config, now=None:
                                REAL_CHECK(database, config, clock.now)))
        hour = []
        for minutes in range(0, 61, 15):
            clock.now = dt.datetime(2026, 10, 22, 17, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
            if minutes in (0, 60):
                self.hourly_round()
            # The ticks at :02:30, :17:30, :32:30 and :47:30 (scheduler_loop.schedule).
            clock.now += dt.timedelta(seconds=150)
            self.tick()
            hour.append(self.sends())
        times = pacing.request_times(self.database)
        for earlier, later in zip(times, times[1:]):
            low, high = pacing._settings(self.config)["jitter_minutes"]
            import random
            gap = random.Random(earlier.isoformat()).uniform(low, high)
            self.assertGreaterEqual(later - earlier, dt.timedelta(minutes=gap))
        # 10:00 the round, 10:02:30 a tick inside the jitter (nothing), 10:17:30, 10:32:30 and 10:47:30 the
        # ticks, 11:00 the round: 4 in the hour where the round alone made 1; the sixth still queued.
        self.assertEqual(hour, [1, 2, 3, 4, 5])
        first_hour = [moment for moment in times if moment < times[0] + dt.timedelta(hours=1)]
        self.assertEqual(len(first_hour), 4)
        self.assertLessEqual(len(times), pacing.daily_cap(pacing._settings(self.config), times, clock.now))
        self.assertEqual(len(times), self.sends())
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_actions WHERE state='PENDING'").fetchone()[0], 1)


class Schedule(unittest.TestCase):
    def at(self, hour, minute, second=0):
        return dt.datetime(2026, 10, 22, hour, minute, second, tzinfo=ZONE)

    def test_quarter_hour_instants_off_the_round_and_research(self):
        instants, periods = scheduler_loop.schedule(self.at(10, 20), UNIPILE)
        self.assertEqual(instants["invites"], self.at(10, 17, 30).timestamp())
        self.assertEqual(periods["invites"], 900)
        self.assertEqual(scheduler_loop.schedule(self.at(10, 2, 29), UNIPILE)[0]["invites"], self.at(9, 47, 30).timestamp())
        self.assertEqual(scheduler_loop.jobs(UNIPILE), ("research", "run", "applications", "invites"))
        self.assertEqual(scheduler_loop.jobs({"linkedin_provider": "heyreach"}), scheduler_loop.JOBS)
        self.assertEqual(scheduler_loop.job_command("invites")[-2:], ["job", "invites"])

    def test_loop_starts_the_tick_four_times_an_hour_and_never_while_paused(self):
        clock = {"now": self.at(9, 59, 50)}
        calls = []

        class Child:
            def poll(self):
                return 0
        loop = scheduler_loop.Loop(tempfile.mkdtemp(), clock=lambda: clock["now"], config=UNIPILE,
                                   spawner=lambda name: calls.append(name) or Child(), is_paused=lambda: False)
        first = loop.tick()   # the first tick starts research and the round, never the tick beside them
        self.assertEqual(first["started"], ["research", "run", "applications"])
        self.assertEqual(first["skipped_lock_holder"], ["invites"])
        calls.clear()
        moment = self.at(10, 0)
        while moment < self.at(11, 0):
            clock["now"] = moment
            loop.tick()
            moment += dt.timedelta(seconds=30)
        self.assertEqual(calls.count("invites"), 4)
        self.assertEqual(calls.count("run"), 1)
        self.assertEqual(calls.count("applications"), 1)
        paused = scheduler_loop.Loop(tempfile.mkdtemp(), clock=lambda: clock["now"], config=UNIPILE,
                                     spawner=lambda name: calls.append(name) or Child(), is_paused=lambda: True)
        calls.clear()
        paused.tick()
        self.assertEqual(calls, [])

    def test_overview_next_run_for_outreach_is_the_next_tick(self):
        import overview_page
        reader = Mock(database=None)
        scheduler = {"running": True}
        config = dict(UNIPILE, research_enabled=False)
        shown = overview_page.next_run(reader, "run", config, self.at(10, 20), scheduler, {})
        self.assertEqual(shown["at"], self.at(10, 32, 30).astimezone(dt.timezone.utc))
        shown = overview_page.next_run(reader, "run", dict(config, linkedin_provider="heyreach"), self.at(10, 20), scheduler, {})
        self.assertEqual(shown["at"], self.at(11, 0).astimezone(dt.timezone.utc))
        self.assertEqual(overview_page.next_run(reader, "run", config, self.at(10, 20), scheduler,
                                                {"user_paused_at": "x"}), {"label": "Paused"})


if __name__ == "__main__":
    unittest.main()
