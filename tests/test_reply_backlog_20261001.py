"""Owner, 2026-10-01: a backlog inbound message gets no reply AI run, and a stuck-reply alert is
only for someone the harness or the owner contacted."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import unittest
from unittest.mock import patch

import engagement
import manual_reply_execution as manual
import orchestrator
import pipeline_routing as routing
import reply_execution
import reply_worker
import synthetic_resume  # noqa: F401 (synthetic designated resume; the owner's is not in the repo)


OWNER_CUTOFF = dt.datetime(2026, 9, 30, 18, 0, tzinfo=dt.timezone.utc)
STRANGER = "https://www.linkedin.com/in/cold-recruiter"


def review_task(database):
    rows = routing.rows(database, "SELECT * FROM workflow_tasks WHERE worker='pipeline_routing_review'")
    assert len(rows) == 1, rows
    return rows[0]


class BacklogNeverReachesTheReplyAI(unittest.TestCase):
    def test_backlog_review_is_closed_without_compute_attempt_or_alert(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture, \
                patch.object(reply_execution, "BACKLOG_CUTOFF", OWNER_CUTOFF):
            fixture.seed()
            task = review_task(fixture.database)
            with fixture.clock(), patch.object(reply_worker, "_execute") as execute:
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([("BACKLOG_NOT_SENT", reply_execution.BACKLOG_BLOCKER)],
                             [(item["state"], item["blocker"]) for item in result["items"]])
            closed = review_task(fixture.database)
            self.assertEqual(("CANCELLED", reply_execution.BACKLOG_BLOCKER, task["attempts"]),
                             (closed["status"], closed["blocker"], closed["attempts"]))
            source = routing.one(fixture.database, "SELECT * FROM pipeline_routing_sources WHERE source_key=?",
                                 (json.loads(task["inputs"])[0]["source_key"],))
            self.assertEqual(("ROUTED", "backlog"), (source["status"], json.loads(source["result"])["review_closed_by"]))
            for table in ("worker_compute_reservations", "reply_worker_attempts", "notification_outbox"):
                self.assertEqual(0, fixture.database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], table)
            # The inbound's own stop on outreach is unchanged.
            self.assertEqual("REPLY_REQUIRES_CONTEXT", engagement.stop_state(fixture.database, manual.PROFILE))
            # The duplicate engagement-review bookkeeping retires against the finished source.
            handed = engagement.handoff_reviews(fixture.database, fixture.config, fixture.now)
            self.assertEqual(["HANDED_OFF"], [item["state"] for item in handed["items"]])
            # A second cycle finds nothing to do.
            with fixture.clock(), patch.object(reply_worker, "_execute") as execute:
                again = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            execute.assert_not_called()
            self.assertEqual([], again["items"])

    def test_post_cutoff_message_still_reaches_the_reply_ai(self):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed()
            fixture.classify()  # requires an actual reply worker result
            self.assertIn("COMPLETE", [row[0] for row in fixture.database.execute(
                "SELECT status FROM workflow_tasks WHERE worker='pipeline_routing_review'")])


class BacklogDoesNotTakeTheReplyWorkersPlaces(unittest.TestCase):
    def test_post_cutoff_reply_runs_in_the_one_place_behind_older_backlog_tasks(self):
        cutoff = dt.datetime(2026, 9, 17, 15, 30, tzinfo=dt.timezone.utc)  # backlog below 15:40
        original = manual.inbound_message

        def dated(occurred_at):
            def build(*arguments, **keywords):
                event = original(*arguments, **keywords)
                event["occurred_at"] = occurred_at
                return event
            return build

        with manual.isolated_boundaries(), manual.Fixture() as fixture, \
                patch.object(reply_execution, "BACKLOG_CUTOFF", cutoff):
            fixture.config["reply_worker"]["max_items"] = 1
            for index in range(3):
                with patch.object(manual, "inbound_message", dated("2026-09-17T15:00:00+00:00")):
                    fixture.seed(profile="https://www.linkedin.com/in/backlog-sender-" + str(index),
                                 source_id="backlog-" + str(index), thread="backlog-thread-" + str(index))
            with patch.object(manual, "inbound_message", dated("2026-09-17T16:00:00+00:00")):
                fixture.seed()
            # The backlog reviews were queued earlier, so they come first in the worker's order.
            fixture.database.execute("""UPDATE workflow_tasks SET updated_at='2026-09-17T15:00:00+00:00'
                WHERE worker='pipeline_routing_review' AND inputs LIKE '%backlog-%'""")
            fixture.database.commit()
            seen = []

            def execute(config, prompt, schema, timeout):
                request = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
                seen.append(request["source_snapshot"]["identity"])
                return fixture.execute(config, prompt, schema, timeout)

            with fixture.clock(), patch.object(reply_worker, "_execute", side_effect=execute):
                result = orchestrator.reply_classification_cycle(fixture.root, fixture.database, fixture.config)
            self.assertEqual(["linkedin.com/in/mock-contact"], seen)
            self.assertEqual(3, sum(item["state"] == "BACKLOG_NOT_SENT" for item in result["items"]))
            self.assertEqual(3, fixture.database.execute("""SELECT COUNT(*) FROM workflow_tasks
                WHERE worker='pipeline_routing_review' AND status='CANCELLED' AND blocker=?""",
                (reply_execution.BACKLOG_BLOCKER,)).fetchone()[0])


class StuckReplyAlertsOnlyForContactedPeople(unittest.TestCase):
    def alert(self, profile, cutoff=None):
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            fixture.seed(profile=profile)
            task = review_task(fixture.database)
            with patch.object(reply_execution, "BACKLOG_CUTOFF", cutoff or reply_execution.BACKLOG_CUTOFF):
                event = reply_worker.notify_failed_attempt(fixture.database, task, None, "SOME_BLOCKER", "BLOCKED",
                                                           fixture.now.isoformat())
            return event, fixture.database.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]

    def test_contacted_person_alerts(self):
        event, count = self.alert(manual.PROFILE)
        self.assertIsNotNone(event)
        self.assertEqual(1, count)

    def test_cold_inbound_from_a_stranger_never_alerts(self):
        self.assertEqual((None, 0), self.alert(STRANGER))

    def test_owner_answered_person_counts_as_contacted(self):
        # Only our own message in the thread (the owner answering by hand): outbound_observed.
        with manual.isolated_boundaries(), manual.Fixture() as fixture:
            database = fixture.database
            self.assertFalse(reply_worker.contacted(database, STRANGER))
            database.execute("""INSERT INTO engagement_events (source,source_id,identity,kind,occurred_at,observed_at,raw,inference,payload)
                VALUES ('heyreach','owner-answer','linkedin.com/in/cold-recruiter','outbound_observed',?,?,'{}',NULL,'{}')""",
                             (manual.BASE.isoformat(), manual.BASE.isoformat()))
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in set(reply_worker.CONTACT_TABLES + ("history", "outreach_action_claims")) & tables:
                self.assertEqual(0, database.execute("SELECT COUNT(*) FROM " + table + " WHERE identity=?",
                                                     ("linkedin.com/in/cold-recruiter",)).fetchone()[0], table)
            self.assertTrue(reply_worker.contacted(database, STRANGER))

    def test_backlog_message_never_alerts(self):
        self.assertEqual((None, 0), self.alert(manual.PROFILE, OWNER_CUTOFF))


if __name__ == "__main__":
    unittest.main()
