"""A failed AI call about a problem never abandons an application (owner, 2026-11-13, G1 to G4).

Before: two failed coordinator calls 15 minutes apart closed the job for good, whatever the failure
(the 2026-10-13 incident: credits ran out, five jobs closed; again on 2026-11-13 with no key). Now:
- the AI cannot be reached (timeout, network, any HTTP refusal, key missing/locked/unreadable): a wait,
  not counted, tried again;
- the AI answers and the answer cannot be used: counted, longer waits, then the job is parked for the
  user with Try again, never closed;
- only the AI's own ABANDON decision abandons;
- jobs the old rule closed say what is true and can be tried again;
- nothing here reopens or parks a job whose submission may have reached the employer.

The errors are the ones the real executor raises (`research_recovery._direct_decision` with the text
model's own exception types); the lifecycle is `applications.cycle` on the rehearsal fixture. No network.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import access_outcomes
import application_connection
import application_recovery
import applications
import applications_page
import credentials
import manual_application_recovery_lifecycle as rehearsal
import research_recovery
import text_model

MINUTE = 60
ABANDON = {"action": "ABANDON", "method": "", "instructions": "", "progress": False,
           "reason": "Official posting removed and no observed replacement links"}


def executor_error(raised):
    """The error `research_recovery._direct_decision` raises when the text model call raises `raised`."""
    folder = Path(tempfile.mkdtemp(prefix="recovery-waits-executor-"))
    try:
        with mock.patch.object(text_model, "complete", side_effect=raised):
            research_recovery._direct_decision({}, "prompt", {"type": "object"}, folder / "decision.json",
                                               folder / "executor.log", 30, [])
    except Exception as error:
        return error
    finally:
        shutil.rmtree(folder, True)
    raise AssertionError("the call did not fail")


def transport(name="ConnectionResetError"):
    error = text_model.TextModelResponseError("the call never got an answer")
    error.transport_error = name
    return error


def no_content():
    error = text_model.TextModelResponseError("the provider returned no content")
    error.finish_reason = "length"
    return error


UNREACHABLE = {
    "a timeout": lambda: TimeoutError("timed out"),
    "the network is down": lambda: transport(),
    "a server error": lambda: text_model.TextModelHTTPError(503, "unavailable"),
    "a request timeout (408)": lambda: text_model.TextModelHTTPError(408, "timeout"),
    "a rejected key (401)": lambda: text_model.TextModelHTTPError(401, "no auth"),
    "a forbidden key (403)": lambda: text_model.TextModelHTTPError(403, "forbidden"),
    "the key is missing": lambda: FileNotFoundError("no key"),
    "the key store is locked": lambda: credentials.Locked("locked"),
    "the key store is unavailable": lambda: credentials.Unavailable("unavailable"),
}
UNREADABLE = {
    "an answer with no content": lambda: executor_error(no_content()),
    "the request is wrong (400)": lambda: executor_error(text_model.TextModelHTTPError(400, "too long")),
    "the request is wrong (404)": lambda: executor_error(text_model.TextModelHTTPError("404", "no such model")),
    "the request is wrong (413)": lambda: executor_error(text_model.TextModelHTTPError(413, "too large")),
    "the request is wrong (422)": lambda: executor_error(text_model.TextModelHTTPError(422, "unprocessable")),
    "an answer that is not the contract": lambda: ValueError("Application recovery cannot repeat unchanged instructions"),
    "a failure nothing can classify": lambda: RuntimeError("Synthetic coordinator output malformed"),
}


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="recovery-waits-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fixture = rehearsal.Fixture(self.root)
        self.addCleanup(self.fixture.database.close)
        self.db = self.fixture.database
        self.key, _ = self.fixture.discover()
        self.at = 0

    def cycle(self, minutes=0):
        self.at += minutes * MINUTE + (1 if minutes else 0)
        self.fixture.cycle(self.at)

    def job(self):
        return self.fixture.job(self.key)

    def failed(self):
        return self.fixture.events(self.key, "APPLICATION_RECOVERY_DECISION_FAILED")

    def counted(self):
        return application_recovery.counted_failures(self.db, self.key, "APPLICATION_RECOVERY_DECISION_FAILED", None)

    def gap_minutes(self):
        job = self.job()
        return round((dt.datetime.fromisoformat(job["next_at"]) - dt.datetime.fromisoformat(job["updated_at"])).total_seconds() / 60)

    def view(self):
        row = self.db.execute("SELECT * FROM application_jobs WHERE job_key=?", (self.key,)).fetchone()
        owner = {self.key} if applications.job_route(str(row["blocker"] or ""), row["state"]) == "OWNER" else set()
        return applications_page.job_view(row, owner, set(), set(), str)

    def try_again(self):
        with self.db:
            return access_outcomes.try_again(self.db, self.key, self.fixture.adapter.now)


class TheAICannotBeReached(Case):
    def test_each_kind_is_a_wait_that_is_not_counted_and_two_outages_do_not_abandon(self):
        for name, raised in UNREACHABLE.items():
            with self.subTest(kind=name):
                self.setUp()
                errors = [executor_error(raised()) for _ in range(4)]
                self.assertEqual({application_recovery.failure_kind(item) for item in errors}, {"UNREACHABLE"}, name)
                self.fixture.adapter.decisions = list(errors)
                self.cycle()
                for gap in (15, 60, 360):                                # four outages in a row: the gaps grow
                    self.assertEqual((self.job()["state"], self.job()["blocker"], self.gap_minutes()),
                                     ("BLOCKED", "REQUISITION_MISMATCH", gap), name)
                    self.cycle(gap)
                self.assertEqual(len(self.fixture.adapter.decision_packets), 4)
                self.assertEqual((self.job()["state"], self.job()["blocker"]), ("BLOCKED", "REQUISITION_MISMATCH"))
                self.assertFalse(self.fixture.events(self.key, "APPLICATION_ABANDONED"))
                self.assertEqual([(item["kind"], item["counts"]) for item in self.failed()], [("UNREACHABLE", False)] * 4)
                self.assertEqual(self.counted(), [])                     # nothing is held against the job
                self.assertEqual(self.view()["state"], "Waiting to retry")
                # The AI is back: the job goes on exactly where it was.
                self.fixture.adapter.responses = [{"status": "RETRY", "reason": "Failed route"}]
                self.assertEqual(self.gap_minutes(), 1440)
                self.cycle(1440)
                self.assertEqual(len(self.fixture.adapter.investigations), 1)

    def test_the_gaps_grow_to_once_a_day_and_another_outcome_starts_them_again(self):
        timeout = lambda: executor_error(TimeoutError("the packet always exceeds the limit"))
        self.fixture.adapter.decisions = [timeout() for _ in range(6)] + [ValueError("bad"), timeout(), timeout()]
        self.cycle()
        seen = []
        for _ in range(6):
            seen.append(self.gap_minutes())
            self.cycle(seen[-1])
        self.assertEqual(seen, [15, 60, 360, 1440, 1440, 1440])              # about one call a day from the fourth on
        self.assertEqual(len(self.fixture.adapter.decision_packets), 7)
        self.assertEqual((self.counted().__len__(), self.gap_minutes()), (1, 15))   # an answer that could not be used
        self.cycle(15)
        self.assertEqual(self.gap_minutes(), 15)                              # ... starts the outage gaps again
        self.cycle(15)
        self.assertEqual(self.gap_minutes(), 60)
        self.assertEqual(application_recovery.consecutive_unreachable(self.db, self.key), 2)
        self.assertFalse(self.fixture.events(self.key, "APPLICATION_ABANDONED"))
        self.assertEqual(self.job()["blocker"], "REQUISITION_MISMATCH")

    def test_a_job_is_not_asked_before_its_gap_is_over(self):
        self.fixture.adapter.decisions = [executor_error(TimeoutError("t")) for _ in range(3)]
        self.cycle()
        self.cycle(15)
        self.cycle(15)                                                        # the hour is not over
        self.assertEqual(len(self.fixture.adapter.decision_packets), 2)

    def test_an_unreachable_call_leaves_the_days_compute_as_it_was(self):
        import application_recovery_decision
        import workflow_tasks
        at = applications.stamp()
        adapter = application_connection.HostedDesktopAdapter(self.db, self.root, {"application_browser": {"enabled": True}},
                                                              clock=lambda: at)
        day = workflow_tasks.budget_day(at, "America/Los_Angeles")
        packet = {"job": self.job()}
        for raised, spent in ((executor_error(TimeoutError("t")), 0), (executor_error(text_model.TextModelHTTPError(401, "x")), 0),
                              (executor_error(text_model.TextModelHTTPError(400, "x")), 30), (ValueError("unusable"), 60)):
            with mock.patch.object(application_recovery_decision, "decide", side_effect=raised), self.assertRaises(Exception):
                adapter.recovery_decision(packet)
            self.assertEqual(workflow_tasks.compute_used(self.db, day), spent, raised)   # only an answered call keeps its 30 seconds

    def test_a_bare_timeout_or_dropped_connection_from_any_route_is_unreachable_too(self):
        for error in (TimeoutError("Research attempt time budget exhausted"), ConnectionResetError("reset")):
            self.assertEqual(application_recovery.failure_kind(error), "UNREACHABLE")
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = TimeoutError("inner")
        self.assertEqual(application_recovery.failure_kind(wrapped), "UNREACHABLE")

    def test_records_from_before_kinds_do_not_count_and_do_not_spend_admissions(self):
        old = {"attempt": 1, "reason": "RECOVERY_DECISION_EXECUTOR_UNAVAILABLE", "exception": {"exception_type": "RuntimeError"}}
        timeout = {"attempt": 1, "reason": "TimeoutError", "exception": {"exception_type": "TimeoutError", "code": None}}
        invalid = {"attempt": 1, "reason": "APPLICATION_RECOVERY_DECISION_INVALID", "exception": {"exception_type": "ValueError"}}
        self.assertEqual([application_recovery.unreachable_record(item) for item in (old, timeout, invalid, None)],
                         [True, True, False, False])
        connection = application_connection.HostedDesktopAdapter.__new__(application_connection.HostedDesktopAdapter)
        at = applications.stamp()                                        # inside the job's current scope
        connection.database, connection.clock = self.db, lambda: at
        admit = lambda: applications.event(self.db, self.key, "BROWSER_CAPACITY_RESERVED", {"purpose": "orchestrator_recovery"}, at)
        with self.db:
            admit()
        self.assertEqual(connection.coordinator_admissions(self.key, at), 1)   # the control: an admission is counted
        with self.db:                                                    # that call and four more were outages
            applications.event(self.db, self.key, "APPLICATION_RECOVERY_DECISION_FAILED", dict(old), at)
            for index in range(4):
                admit()
                applications.event(self.db, self.key, "APPLICATION_RECOVERY_DECISION_FAILED", dict(old, kind="UNREACHABLE"), at)
        self.assertEqual(connection.coordinator_admissions(self.key, at), 0)
        self.assertFalse(connection.coordinator_cap_reached(self.key, at))
        with self.db:                                                    # an answer that could not be used did spend one
            admit()
            applications.event(self.db, self.key, "APPLICATION_RECOVERY_DECISION_FAILED", dict(invalid, kind="UNREADABLE"), at)
        self.assertEqual(connection.coordinator_admissions(self.key, at), 1)


class TheEngineRoute(Case):
    """No direct text model: the decision runs through the engine (`orchestrator.run_research_worker`).
    A run that did not complete got no answer back; only a completed run's unusable answer is counted."""

    def decide(self, worker):
        artifacts = research_recovery.create_artifacts(self.root, scope="application-recovery")
        command = ["engine", "--output", str(Path(artifacts["directory"]) / "decision.json")]
        with mock.patch("engines.engine") as engine, mock.patch("orchestrator.research_command", return_value=command):
            engine.return_value.research_recovery_command.return_value = command
            try:
                research_recovery.bounded_decision(self.root, {}, "prompt", artifacts, 30, scope="application-recovery",
                                                   executor=worker)
            except Exception as error:
                return error
        raise AssertionError("the decision did not fail")

    def test_a_run_that_did_not_complete_is_a_wait(self):
        import provider_limits
        import subprocess

        def raising(error, cause=None):
            def worker(working, command, prompt, log, timeout):
                raise error from cause
            return worker
        limit = provider_limits.ProviderUsageLimit("signed out")
        stopped = {
            "a non-zero exit": raising(RuntimeError("Research worker exited 1; see executor.log")),
            "signed out or out of usage": raising(RuntimeError("Research worker exited 1; see executor.log"), limit),
            "interrupted by a shutdown": raising(InterruptedError("Research interrupted by service shutdown")),
            "its time limit": raising(subprocess.TimeoutExpired(["engine"], 30)),
            "not installed": raising(FileNotFoundError("engine")),
        }
        for name, worker in stopped.items():
            error = self.decide(worker)
            self.assertEqual(application_recovery.failure_kind(error), "UNREACHABLE", name)
        self.assertIs(provider_limits.wait_of(self.decide(stopped["signed out or out of usage"])), limit)   # still the provider wait

    def test_a_completed_run_with_an_unusable_answer_is_counted(self):
        def nothing(working, command, prompt, log, timeout):
            pass

        def not_json(working, command, prompt, log, timeout):
            (Path(working) / "decision.json").write_text("not json")
        for worker in (nothing, not_json):
            self.assertEqual(application_recovery.failure_kind(self.decide(worker)), "UNREADABLE")

    def test_three_engine_outages_wait_and_never_park_the_job(self):
        def down(working, command, prompt, log, timeout):
            raise RuntimeError("Research worker exited 1; see executor.log")
        self.fixture.adapter.decisions = [self.decide(down) for _ in range(3)]
        self.cycle()
        self.cycle(15)
        self.cycle(60)
        self.assertEqual((self.job()["blocker"], self.counted(), self.gap_minutes()), ("REQUISITION_MISMATCH", [], 360))
        self.assertFalse(self.fixture.events(self.key, application_recovery.NEEDS_YOU_EVENT))


class TheAnswerCannotBeUsed(Case):
    def test_each_kind_waits_longer_then_is_parked_for_the_user_and_is_never_closed(self):
        for name, raised in UNREADABLE.items():
            with self.subTest(kind=name):
                self.setUp()
                self.fixture.adapter.decisions = [raised() for _ in range(3)]
                self.cycle()
                self.assertEqual((self.job()["blocker"], self.gap_minutes(), len(self.counted())), ("REQUISITION_MISMATCH", 15, 1))
                self.cycle(15)
                self.assertEqual((self.job()["state"], self.job()["blocker"], self.gap_minutes(), len(self.counted())),
                                 ("BLOCKED", "REQUISITION_MISMATCH", 60, 2))    # two failures no longer abandon
                self.cycle(15)
                self.assertEqual(len(self.fixture.adapter.decision_packets), 2)  # not asked again before the longer wait is over
                self.cycle(45)
                job = self.job()
                self.assertEqual((job["state"], job["blocker"], job["next_at"]),
                                 ("BLOCKED", application_recovery.NEEDS_YOU_BLOCKER, None))
                self.assertFalse(self.fixture.events(self.key, "APPLICATION_ABANDONED"))
                self.assertEqual(self.fixture.events(self.key, application_recovery.NEEDS_YOU_EVENT)[0]["failures"], 3)
                self.assertEqual({item["kind"] for item in self.failed()}, {"UNREADABLE"})
                # The compute is bounded: parked, the job is asked nothing more, however long it sits.
                for _ in range(3):
                    self.cycle(600)
                self.assertEqual(len(self.fixture.adapter.decision_packets), application_recovery.UNREADABLE_LIMIT)
                self.assertEqual(self.job()["blocker"], application_recovery.NEEDS_YOU_BLOCKER)
                # What the user sees: needs you, the sentence, Try again. Never "Given up".
                view = self.view()
                self.assertEqual((view["state"], view["needs_you"], view["given_up"], view["try_again"]),
                                 ("Blocked, needs you", True, False, "Try again"))
                # The sentence is the row's one line (owner, 2026-11-14, E15.T14 L2): never a generic line beside it.
                self.assertEqual((view["todo"], view["problem"]), (application_recovery.NEEDS_YOU_WORDS, None))

    def test_outages_between_them_are_not_counted_toward_the_card(self):
        self.fixture.adapter.decisions = [ValueError("bad"), executor_error(TimeoutError("t")), executor_error(TimeoutError("t")),
                                          ValueError("bad"), executor_error(text_model.TextModelHTTPError(500, "x"))]
        self.cycle()
        for minutes in (15, 15, 60, 60):
            self.cycle(minutes)
        self.assertEqual(len(self.fixture.adapter.decision_packets), 5)
        self.assertEqual((len(self.counted()), self.job()["blocker"]), (2, "REQUISITION_MISMATCH"))

    def test_try_again_on_the_card_starts_a_new_scope_with_its_own_bound(self):
        self.fixture.adapter.decisions = [ValueError("bad")] * 3
        self.cycle()
        self.cycle(15)
        self.cycle(60)
        self.assertEqual(self.job()["blocker"], application_recovery.NEEDS_YOU_BLOCKER)
        self.assertEqual(self.try_again(), {"reopened": True})
        self.assertEqual((self.job()["state"], self.job()["blocker"]), ("QUEUED", None))
        self.assertEqual(self.try_again()["why"], "NOT_CLOSED")              # a second press finds nothing to do
        since = application_recovery.reopen_scope(self.db, self.key, self.fixture.adapter.now)
        self.assertEqual(application_recovery.counted_failures(self.db, self.key, "APPLICATION_RECOVERY_DECISION_FAILED", since), [])

    def test_the_sentences_are_the_same_in_the_page_and_the_record(self):
        self.assertEqual(applications_page.CODE_WORDS[application_recovery.NEEDS_YOU_BLOCKER], application_recovery.NEEDS_YOU_WORDS)
        self.assertEqual(applications_page.CODE_WORDS[application_recovery.NOT_DECIDED_BLOCKER], application_recovery.NOT_DECIDED_WORDS)
        # The owner's sentences, exactly (2026-11-13, T14 a): a parked job is stuck, an old closure was closed.
        self.assertEqual(application_recovery.NEEDS_YOU_WORDS, "This application is stuck. Press Try again.")
        self.assertEqual(application_recovery.NOT_DECIDED_WORDS, "This application was closed. Press Try again if you want this job.")
        for words in (application_recovery.NEEDS_YOU_WORDS, application_recovery.NOT_DECIDED_WORDS):
            self.assertNotIn("—", words)
            self.assertNotIn("OpenRouter", words)


class OnlyTheAIsDecisionAbandons(Case):
    def abandon(self):
        self.fixture.adapter.decisions = [dict(ABANDON)]
        self.cycle()

    def test_an_explicit_abandon_still_abandons_and_keeps_its_words_and_no_try_again(self):
        self.abandon()
        self.assertEqual((self.job()["state"], self.job()["blocker"]), ("ABANDONED", "APPLICATION_RECOVERY_ABANDONED"))
        self.assertEqual(self.fixture.events(self.key, "APPLICATION_ABANDONED")[0]["reason"], ABANDON["reason"])
        self.cycle(15)                                                       # the relabel never touches a real decision
        view = self.view()
        self.assertEqual((view["state"], view["closed"], view["try_again"]),
                         ("Given up", "Sidedoor looked into a problem and decided it cannot apply.", None))
        self.assertEqual(self.try_again()["why"], "NOT_CLOSED_BY_A_WALL")
        self.assertEqual(self.job()["state"], "ABANDONED")

    def test_an_abandon_after_failed_calls_is_still_the_ais_own(self):
        self.fixture.adapter.decisions = [executor_error(TimeoutError("t")), ValueError("bad"), dict(ABANDON)]
        self.cycle()
        self.cycle(15)
        self.cycle(15)
        self.assertEqual((self.job()["state"], self.fixture.events(self.key, "APPLICATION_ABANDONED")[0]["reason"]),
                         ("ABANDONED", ABANDON["reason"]))


class ClosedByTheOldRule(Case):
    def close_the_old_way(self):
        """As 0.4.3 left such a job: abandoned by recovery with the reason RECOVERY_COORDINATOR_FAILED."""
        with self.db:
            job = self.job()
            applications.event(self.db, self.key, "APPLICATION_RECOVERY_STARTED", {"blocker": job["blocker"], "started_at": rehearsal.NOW, "max_attempts": 3}, rehearsal.NOW)
            lifecycle = {"blocker": job["blocker"], "started_at": rehearsal.NOW, "max_attempts": 3}
            self.assertTrue(application_recovery.abandon(self.db, job, lifecycle, [], application_recovery.OLD_FAILED_REASON, [], rehearsal.NOW))
        self.assertEqual(self.view()["closed"], "Sidedoor looked into a problem and decided it cannot apply.")   # the untrue line

    def test_it_says_what_is_true_and_can_be_tried_again(self):
        self.close_the_old_way()
        self.assertEqual(self.try_again()["why"], "NOT_CLOSED_BY_A_WALL")     # before the relabel: refused, as in 0.4.3
        before = self.job()
        self.cycle()                                                         # the next round relabels it, once
        after = self.job()
        self.assertEqual((after["state"], after["blocker"], after["updated_at"]),
                         ("ABANDONED", application_recovery.NOT_DECIDED_BLOCKER, before["updated_at"]))
        self.assertEqual(len(self.fixture.events(self.key, application_recovery.NOT_DECIDED_EVENT)), 1)
        view = self.view()
        self.assertEqual((view["closed"], view["try_again"]), (application_recovery.NOT_DECIDED_WORDS, "Try again"))
        self.cycle(15)
        self.assertEqual(len(self.fixture.events(self.key, application_recovery.NOT_DECIDED_EVENT)), 1)
        self.assertEqual(self.try_again(), {"reopened": True})
        self.assertEqual((self.job()["state"], self.job()["blocker"]), ("QUEUED", None))
        reopened = self.fixture.events(self.key, "APPLICATION_REOPENED")[0]
        self.assertEqual((reopened["authorized_by"], reopened["previous_state"]["blocker"]),
                         ("user:dashboard", application_recovery.NOT_DECIDED_BLOCKER))

    def test_a_job_parked_with_its_reads_spent_is_not_a_dead_end_either(self):
        with self.db:
            job = self.job()
            lifecycle = {"blocker": job["blocker"], "started_at": rehearsal.NOW, "max_attempts": 3}
            applications.event(self.db, self.key, "APPLICATION_RECOVERY_STARTED", lifecycle, rehearsal.NOW)
            self.assertTrue(application_recovery.hold_exhausted(self.db, job, lifecycle, [], rehearsal.NOW))
        self.assertEqual(self.job()["blocker"], application_recovery.CLAIMS_EXHAUSTED_BLOCKER)
        self.assertEqual(self.view()["try_again"], "Try again")
        self.assertEqual(self.try_again(), {"reopened": True})
        self.assertEqual((self.job()["state"], self.job()["blocker"]), ("QUEUED", None))


class NothingReachesTheEmployerTwice(Case):
    close_the_old_way = ClosedByTheOldRule.close_the_old_way

    """The hard line: a job whose submission may have reached the employer is never reopened or parked by
    any of this; the existing guard (`applications.uncertain_submission`) decides, exactly as before."""

    def shapes(self):
        def old_closure():
            self.close_the_old_way()
            self.cycle()

        def parked_for_the_user():
            self.fixture.adapter.decisions = [ValueError("bad")] * 3
            self.cycle()
            self.cycle(15)
            self.cycle(60)

        def reads_spent():
            with self.db:
                job = self.job()
                lifecycle = {"blocker": job["blocker"], "started_at": rehearsal.NOW, "max_attempts": 3}
                applications.event(self.db, self.key, "APPLICATION_RECOVERY_STARTED", lifecycle, rehearsal.NOW)
                application_recovery.hold_exhausted(self.db, job, lifecycle, [], rehearsal.NOW)
        return {application_recovery.NOT_DECIDED_BLOCKER: old_closure, application_recovery.NEEDS_YOU_BLOCKER: parked_for_the_user,
                application_recovery.CLAIMS_EXHAUSTED_BLOCKER: reads_spent}

    def test_try_again_refuses_every_retryable_shape_while_a_submission_is_uncertain(self):
        for blocker, make in self.shapes().items():
            with self.subTest(shape=blocker):
                self.setUp()
                make()
                before = self.job()
                self.assertEqual(before["blocker"], blocker)
                with self.db:   # a live submit attempt: the real guard, not a patch
                    self.db.execute("INSERT INTO application_attempts(attempt_id,job_key,idempotency_key,state,started_at) VALUES (?,?,?,?,?)",
                                    ("attempt-1", self.key, "idem-1", "SUBMITTING", rehearsal.NOW))
                self.assertTrue(applications.uncertain_submission(self.db, self.key))
                result = self.try_again()
                self.assertEqual((result["reopened"], result["why"]), (False, "SUBMISSION_UNCERTAIN"))
                self.assertEqual(self.job(), before)                         # nothing changed
                self.assertFalse(self.fixture.events(self.key, "APPLICATION_REOPENED"))

    def test_a_job_with_an_uncertain_submission_is_never_parked_or_recovered(self):
        self.fixture.adapter.decisions = [ValueError("bad")] * 2
        self.cycle()
        self.cycle(15)
        with mock.patch.object(application_recovery, "outstanding", return_value=True):
            job, lifecycle = self.job(), {"blocker": "REQUISITION_MISMATCH", "started_at": rehearsal.NOW, "max_attempts": 3}
            with self.db:
                self.assertFalse(application_recovery.hold_needs_you(self.db, job, lifecycle, self.counted(), rehearsal.NOW))
            self.assertFalse(application_recovery.recover(self.db, job, self.fixture.adapter, rehearsal.CONFIG,
                                                          job["blocker"], job["next_action"], rehearsal.NOW))
        self.assertEqual(self.job()["blocker"], "REQUISITION_MISMATCH")
        self.assertEqual(len(self.fixture.adapter.decision_packets), 2)


if __name__ == "__main__":
    unittest.main()
