import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import sqlite3
import unittest
from unittest.mock import patch

import engagement
import outreach
import outreach_campaign as campaign
from manual_campaign_e2e import Fixture, scheduled_delivery_case


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")).start()
        patch("subprocess.Popen", side_effect=AssertionError("NO EXECUTOR")).start()

    def test_scheduled_proof_refresh_delivery_and_restart(self):
        result = scheduled_delivery_case()
        self.assertEqual(result["initial_events"], 1)
        self.assertEqual(result["writes"], 4)

    def test_preparation_claims_block_other_message_and_campaign_aliases(self):
        fixture = self.fixture
        fixture.prepare()
        alias = "https://www.linkedin.com/in/mock-alias"
        with fixture.database:
            fixture.database.execute("INSERT INTO rw_aliases VALUES ('contact',?,?,'mock-verified-alias')", (alias, fixture.action["profile"]))
        for purpose in ("INITIAL", "FIRST_MESSAGE", "FOLLOWUP"):
            with self.subTest(purpose=purpose):
                action = dict(fixture.action, action_id="other:" + purpose, purpose=purpose,
                              profile=alias, conversation_id="mock-thread", ordinal=1)
                with self.assertRaisesRegex(ValueError, "CAMPAIGN_PREPARATION_OWNS_RECIPIENT"):
                    outreach.enqueue_action(fixture.database, action)
        with self.assertRaisesRegex(ValueError, "CAMPAIGN_PREPARATION_OWNS_RECIPIENT"):
            outreach.enqueue(fixture.database, dict(fixture.action, profile=alias, campaign_id=777))
        self.assertEqual(len(fixture.writes), 2)

    def test_immutable_old_claim_blocks_preparation_despite_cancelled_action(self):
        fixture = self.fixture
        action = dict(fixture.action, purpose="INITIAL", action_id="old-initial", conversation_id="mock-thread")
        outreach.enqueue_action(fixture.database, action)
        row = fixture.database.execute("SELECT * FROM outreach_actions").fetchone()
        with fixture.database:
            fixture.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                (row["action_id"], row["payload"], row["payload_hash"], row["identity"], fixture.config["sender_id"],
                 json.dumps(fixture.proof), fixture.now.isoformat()))
            fixture.database.execute("UPDATE outreach_actions SET state='CANCELLED'")
        with self.assertRaisesRegex(ValueError, "IMMUTABLE_OUTREACH_CLAIM_EXISTS"):
            fixture.prepare()
        self.assertFalse(fixture.writes)

    def test_intent_and_owner_cannot_be_replaced_or_rewritten(self):
        fixture = self.fixture
        fixture.prepare()
        for statement in (
                "DELETE FROM outreach_campaign_preparations",
                "UPDATE outreach_campaign_preparations SET sender_id=777",
                "INSERT OR REPLACE INTO outreach_campaign_preparations SELECT * FROM outreach_campaign_preparations",
                "DELETE FROM outreach_campaign_intents",
                "UPDATE outreach_campaign_intents SET arguments='{}'",
                "UPDATE outreach_campaign_intents SET preflight='{}'",
                "INSERT OR REPLACE INTO outreach_campaign_intents SELECT * FROM outreach_campaign_intents"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with fixture.database:
                    fixture.database.execute(statement)

    def test_failed_fresh_inspection_cannot_launch_previously_queued_proof(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.proof["pending_invitation"] = True
        result = fixture.run_campaign_cycle()
        self.assertEqual(result["dispatch"]["launch_attempts"], 0)
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_queue").fetchone()[0], "HELD_INSPECTION")
        self.assertEqual(len(fixture.writes), 3)
        fixture.restart()
        self.assertEqual(fixture.run_campaign_cycle()["items"], [])
        self.assertEqual(len(fixture.writes), 3)

    def test_failed_list_write_retries_three_times_then_fails(self):
        # Owner, 2026-09-30: "for campaign: a failed write can be retried. retry limit of 3 with
        # retry back off." Retries at 15 minutes and then an hour; the third failure is terminal.
        fixture = self.fixture
        fixture.failure, fixture.applied = "create_empty_list", False
        fixture.prepare()
        states = []
        for minutes in (0, 16, 61, 61):
            fixture.now += dt.timedelta(minutes=minutes)
            fixture.proof["checked_at"] = fixture.now.isoformat()
            states.append(fixture.prepare()["state"])
        self.assertEqual(states, ["RECONCILIATION_REQUIRED", "RECONCILIATION_REQUIRED", "FAILED", "FAILED"])
        self.assertEqual(len(fixture.writes), 3)
        state, result = fixture.database.execute("SELECT state,result FROM outreach_campaign_intents").fetchone()
        self.assertEqual(state, "WRITE_FAILED")
        self.assertEqual([error["attempt"] for error in json.loads(result)["errors"]], [1, 2, 3])

    def test_failed_list_write_is_not_repeated_when_heyreach_shows_it(self):
        fixture = self.fixture
        fixture.failure, fixture.applied = "create_empty_list", False
        fixture.prepare()
        # The write reached HeyReach after all: the retry reads the list by name and uses it.
        fixture.failure = None
        name = fixture.writes[0][1]["listName"]
        fixture.lists[101], fixture.leads[101] = {"id": 101, "name": name, "listType": "USER_LIST"}, []
        fixture.now += dt.timedelta(minutes=16)
        fixture.proof["checked_at"] = fixture.now.isoformat()
        self.assertEqual(fixture.prepare()["state"], "READY")
        self.assertEqual([write[0] for write in fixture.writes], ["create_empty_list", "add_leads_to_list_v2"])

    def test_failed_list_write_retry_succeeds(self):
        fixture = self.fixture
        fixture.failure, fixture.applied = "add_leads_to_list_v2", False
        fixture.prepare()
        fixture.failure = None
        fixture.now += dt.timedelta(minutes=16)
        fixture.proof["checked_at"] = fixture.now.isoformat()
        self.assertEqual(fixture.prepare()["state"], "READY")
        self.assertEqual([write[0] for write in fixture.writes],
                         ["create_empty_list", "add_leads_to_list_v2", "add_leads_to_list_v2"])

    def test_failed_campaign_create_retries_after_reading_by_name(self):
        fixture = self.fixture
        fixture.failure, fixture.applied = "create_campaign", False
        fixture.create(fixture.prepare_scheduled())
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "WRITE_RETRY_WAIT")
        fixture.failure = None
        adapter = outreach.HeyReachActionAdapter(fixture.create_transport, lambda action, config: fixture.proof, fixture.reconcile)
        fixture.now += dt.timedelta(minutes=16)
        outreach.cycle(fixture.database, fixture.config, adapter, fixture.now, purpose="PREPARE_CAMPAIGN")
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "ACKNOWLEDGED_UNCONFIRMED")
        fixture.now += dt.timedelta(minutes=61)
        outreach.cycle(fixture.database, fixture.config, adapter, fixture.now, purpose="PREPARE_CAMPAIGN")
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "PREPARED")
        self.assertEqual([write[0] for write in fixture.writes].count("create_campaign"), 2)

    def test_failed_campaign_create_that_landed_is_not_written_again(self):
        fixture = self.fixture
        fixture.failure, fixture.applied = "create_campaign", True
        fixture.create(fixture.prepare_scheduled())
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_actions").fetchone()[0], "PREPARED")
        self.assertEqual([write[0] for write in fixture.writes].count("create_campaign"), 1)

    def test_failed_launch_retries_under_the_same_claim(self):
        # Owner, 2026-09-30: "For Launch remove the any error become uncertain and never retries."
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", False
        first = fixture.run_campaign_cycle()
        self.assertEqual(first["dispatch"]["items"][0]["state"], "LAUNCH_RETRY_WAIT")
        self.assertFalse(fixture.database.execute("SELECT 1 FROM history").fetchone())
        fixture.failure = None
        fixture.now += dt.timedelta(minutes=16)
        fixture.proof["checked_at"] = fixture.now.isoformat()
        retried = fixture.run_campaign_cycle()
        self.assertEqual(retried["launch_retries"][0]["state"], "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
        self.assertEqual([write[0] for write in fixture.writes].count("start_campaign"), 2)
        self.assertEqual(fixture.database.execute("SELECT COUNT(*) FROM outreach_launch_claims").fetchone()[0], 1)

    def test_failed_launch_that_started_is_recorded_not_relaunched(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", True
        self.assertEqual(fixture.run_campaign_cycle()["dispatch"]["items"][0]["state"], "LAUNCH_RETRY_WAIT")
        fixture.failure = None
        fixture.delivered = True
        fixture.now += dt.timedelta(minutes=16)
        result = fixture.run_campaign_cycle()
        self.assertEqual(result["launch_retries"][0]["state"], "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
        self.assertEqual([write[0] for write in fixture.writes].count("start_campaign"), 1)
        fixture.now += dt.timedelta(minutes=16)
        fixture.run_campaign_cycle()
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=201").fetchone()[0],
                         "CONFIRMED_SENT")

    def test_failed_launch_is_terminal_after_three_attempts(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", False
        for minutes in (0, 16, 61, 61):
            fixture.now += dt.timedelta(minutes=minutes)
            fixture.run_campaign_cycle()
        state, result = fixture.database.execute("SELECT state,result FROM outreach_attempts WHERE campaign_id=201").fetchone()
        self.assertEqual(state, "LAUNCH_FAILED")
        self.assertEqual(len(json.loads(result)["launch_errors"]), 3)
        self.assertEqual([write[0] for write in fixture.writes].count("start_campaign"), 3)

    def test_third_launch_that_started_is_recorded_not_failed(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", False
        for minutes in (0, 16):
            fixture.now += dt.timedelta(minutes=minutes)
            fixture.run_campaign_cycle()
        fixture.applied = True          # the third launch reaches HeyReach but times out
        fixture.now += dt.timedelta(minutes=61)
        fixture.run_campaign_cycle()
        state = fixture.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=201").fetchone()[0]
        self.assertEqual(state, "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")

    def test_launch_failed_campaign_that_later_sends_is_confirmed(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", False
        for minutes in (0, 16, 61):
            fixture.now += dt.timedelta(minutes=minutes)
            fixture.run_campaign_cycle()
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=201").fetchone()[0], "LAUNCH_FAILED")
        fixture.campaigns[201].update(status="IN_PROGRESS", startedAt=fixture.now.isoformat())
        fixture.delivered = True
        fixture.now += dt.timedelta(minutes=16)
        fixture.run_campaign_cycle()
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_attempts WHERE campaign_id=201").fetchone()[0], "CONFIRMED_SENT")
        self.assertEqual([write[0] for write in fixture.writes].count("start_campaign"), 3)

    def test_unreadable_launch_state_is_bounded_and_never_relaunched(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.failure, fixture.applied = "start_campaign", False
        fixture.run_campaign_cycle()
        reads = fixture.call

        def failing(operation, arguments):
            if operation == "get_overall_stats":
                raise TimeoutError("mock unreadable")
            return reads(operation, arguments)
        fixture.call = failing
        for unused in range(8):
            fixture.now += dt.timedelta(hours=13)
            fixture.run_campaign_cycle()
        state, result = fixture.database.execute("SELECT state,result FROM outreach_attempts WHERE campaign_id=201").fetchone()
        self.assertEqual(state, "LAUNCH_FAILED")
        self.assertEqual(json.loads(result)["unreadable_checks"], 6)
        self.assertEqual([write[0] for write in fixture.writes].count("start_campaign"), 1)

    def test_exhausted_handoff_does_not_starve_ready_preparation(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.config["outreach_agent"] = {"max_items": 1}
        with fixture.database:
            placeholder = dict(fixture.action, action_id="000-old", profile="linkedin.com/in/old", recipient="Old Person", company="Other Company")
            fixture.database.execute("INSERT INTO outreach_actions VALUES ('000-old','linkedin.com/in/old','PREPARE_CAMPAIGN',?,'old','PREPARED',NULL,NULL)", (json.dumps(placeholder),))
            fixture.database.execute("INSERT INTO outreach_prepared_campaigns VALUES (1,'000-old','linkedin.com/in/old')")
            fixture.database.execute("INSERT INTO outreach_read_retries(action_id,phase,state,attempts,updated_at) VALUES ('campaign-handoff:000-old','INSPECTION','READ_RETRY_EXHAUSTED',3,?)", (fixture.now.isoformat(),))
        result = fixture.run_campaign_cycle()
        self.assertEqual(result["dispatch"]["launch_attempts"], 1, result)

    def test_delivery_unknown_exhaustion_does_not_resend(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        for minutes in (0, 16, 61, 61):
            fixture.now += dt.timedelta(minutes=minutes)
            fixture.run_campaign_cycle()
        row = fixture.database.execute("SELECT state,attempts FROM outreach_read_retries WHERE action_id='campaign-delivery:201'").fetchone()
        self.assertEqual(tuple(row), ("RETRY_WAIT", 3))
        self.assertEqual(len(fixture.writes), 4)
        self.assertFalse(engagement.events_for(fixture.database, fixture.action["profile"]))

    def test_final_guard_rechecks_closure_after_provider_reads(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        candidate = json.loads(fixture.database.execute("SELECT payload FROM outreach_queue").fetchone()[0])
        self.assertTrue(outreach.claim_send(fixture.database, candidate, fixture.config["pipeline"], fixture.config))
        original = fixture.call

        def close_after_read(operation, arguments):
            response = original(operation, arguments)
            if operation == "get_conversations_v2":
                engagement.close_outreach(fixture.database, fixture.action["profile"], "mock late closure", fixture.now)
            return response

        with patch.object(fixture, "call", side_effect=close_after_read):
            with self.assertRaisesRegex(ValueError, "OUTREACH_CLOSED"):
                outreach.final_campaign_guard(fixture.database, fixture.config, fixture, 201, fixture.now)
        self.assertEqual(len(fixture.writes), 3)

    def test_changed_membership_cannot_turn_campaign_stats_into_delivery(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.run_campaign_cycle()
        fixture.delivered = True
        fixture.now += dt.timedelta(minutes=16)
        fixture.leads[101].append({"profileUrl": "https://www.linkedin.com/in/other-person"})
        result = fixture.run_campaign_cycle()
        self.assertEqual(result["delivery"][0]["state"], "UNKNOWN")
        self.assertFalse(engagement.events_for(fixture.database, fixture.action["profile"]))
        self.assertEqual(len(fixture.writes), 4)

    def test_exhausted_delivery_does_not_starve_later_recipient(self):
        fixture = self.fixture
        fixture.create(fixture.prepare_scheduled())
        fixture.run_campaign_cycle()
        fixture.config["outreach_agent"] = {"max_items": 1}
        fixture.delivered = True
        fixture.now += dt.timedelta(minutes=16)
        with fixture.database:
            fixture.database.execute("INSERT INTO outreach_attempts VALUES ('linkedin.com/in/old',1,'UNCERTAIN_NO_RETRY','2026-01-01T00:00:00+00:00',NULL)")
            fixture.database.execute("INSERT INTO outreach_launch_claims VALUES (1,'linkedin.com/in/old',777,'{}','old','2026-01-01T00:00:00+00:00')")
            fixture.database.execute("INSERT INTO outreach_read_retries(action_id,phase,state,attempts,updated_at) VALUES ('campaign-delivery:1','RECONCILIATION','READ_RETRY_EXHAUSTED',3,?)", (fixture.now.isoformat(),))
        result = fixture.run_campaign_cycle()
        self.assertEqual(result["delivery"][0]["state"], "CONFIRMED_SENT", result)
        self.assertEqual(result["delivery"][0]["campaign_id"], 201)

    def test_provider_null_defaults_preserve_exact_sequence_but_withdrawal_does_not(self):
        fixture = self.fixture
        expected = fixture.action["sequence"]
        observed = json.loads(json.dumps(expected))
        observed["payload"].update(fallbackMessage=fixture.action["message"], toBeWithdrawnAfterDays=None)
        for branch in ("conditionalNode", "unconditionalNode"):
            observed[branch].update(payload=None, conditionalNode=None, unconditionalNode=None)
        self.assertTrue(campaign._same_sequence(observed, expected))
        observed["payload"]["toBeWithdrawnAfterDays"] = 14
        self.assertFalse(campaign._same_sequence(observed, expected))


if __name__ == "__main__":
    unittest.main()
