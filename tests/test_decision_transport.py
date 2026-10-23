import datetime as dt
import os
import json
import unittest
from unittest import mock

import decision_transport
import notifications
import orchestrator
import test_applications_e2e as fixtures
from test_applications_e2e import MockBridge, MockTelegram, NOW
import profile_home
import user_profile


class DecisionTransportTests(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job
    submissions = fixtures.ApplicationEndToEndTests.submissions

    def test_bridge_identifies_itself_without_weakening_authentication(self):
        credential = self.root / "transport-token"
        credential.write_text("fixture-only-token")
        credential.chmod(0o600)
        settings = {"url": "https://bridge.example", "owner_id": "mock-owner", "credential_file": str(credential)}
        with mock.patch("decision_transport.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"mode":"transport-only"}'
            self.assertEqual(decision_transport.Bridge(settings).call("health")["mode"], "transport-only")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("User-agent"), "job-search-orchestrator/1.0")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-only-token")
            self.assertIs(opener.call_args.args[0], decision_transport.NoRedirect)

    def prepare(self):
        self.discover()
        self.adapter.customize = lambda result, job: result["questions"].append({"id": "citizenship", "semantic": "citizenship_country", "required": True, "answer_type": "text"})
        self.cycle()
        self.bridge = MockBridge()
        self.config = {"bridge_transport": {"owner_id": "mock-owner"}}
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        notifications.dispatch(self.database, {}, MockTelegram(), NOW)
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.decision = dict(self.database.execute("SELECT * FROM user_decisions").fetchone())

    def answer(self, **overrides):
        return {"seq": 1, "decision_id": self.decision["id"], "owner": "mock-owner", "action_id": self.decision["job_key"], "payload_hash": self.decision["payload_hash"], "response": {"status": "answered", "raw": "Mock country"}, **overrides}




    def install(self, **changes):
        policy = user_profile.load()
        policy.update(changes)
        profile_home.use(self, policy)
        return policy

    def record(self, key, **answer):
        policy = user_profile.load()
        policy["answers"][key] = answer
        profile_home.use(self, policy)
        return policy








    def test_harmless_transport_test_never_creates_application_or_answer(self):
        with self.database:
            identifier = decision_transport.request_transport_test(self.database, self.policy, NOW)
        bridge = MockBridge()
        config = {"bridge_transport": {"owner_id": "mock-owner"}}
        decision_transport.cycle(self.database, config, self.root, bridge, NOW)
        notifications.dispatch(self.database, {}, MockTelegram(), NOW)
        decision_transport.cycle(self.database, config, self.root, bridge, NOW)
        decision = self.database.execute("SELECT * FROM user_decisions WHERE id=?", (identifier,)).fetchone()
        bridge.events = [{"seq": 1, "decision_id": identifier, "owner": "mock-owner", "action_id": "system:transport-smoke", "payload_hash": decision["payload_hash"], "response": {"status": "answered", "raw": "transport-ok"}}]
        decision_transport.cycle(self.database, config, self.root, bridge, NOW)
        self.assertEqual(self.database.execute("SELECT state FROM user_decisions").fetchone()[0], "TEST_CONFIRMED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_answers").fetchone()[0], 0)

    def test_relay_lost_ack_reconciles_without_second_send(self):
        class Endpoint:
            sends = 0

            def call(self, operation, body=None):
                if operation == "notifications":
                    self.sends += 1
                    raise TimeoutError("Persisted relay receipt, response lost")
                return {"state": "DELIVERED", "message_id": 123, "chat_id": "mock-owner"}

        with self.database:
            self.database.execute("INSERT INTO notification_outbox(event_key,message,state,updated_at) VALUES ('relay-test','Harmless mock','PENDING',?)", (NOW,))
        relay = notifications.Relay({})
        relay.bridge = Endpoint()
        notifications.dispatch(self.database, {}, relay, NOW)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "UNCERTAIN")
        notifications.dispatch(self.database, {}, relay, NOW)
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "DELIVERED")
        self.assertEqual(relay.bridge.sends, 1)











class CredentialFileOwnershipTests(unittest.TestCase):
    """E2.T3 P3: the credential-file check goes through portable.owned_by_me; the mode-bit check is
    POSIX only (Windows reports 0o666 for every file)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.credential = Path(temporary.name) / "transport-token"
        self.credential.write_text("fixture-only-token")
        self.settings = {"url": "https://bridge.example", "owner_id": "mock-owner",
                         "credential_file": str(self.credential)}

    def call(self):
        with mock.patch("decision_transport.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"mode":"ok"}'
            return decision_transport.Bridge(self.settings).call("health")

    def refused(self):
        with self.assertRaises(decision_transport.PreSendUnavailable) as caught:
            self.call()
        self.assertEqual("BRIDGE_CREDENTIAL_FILE_PERMISSIONS_INVALID", str(caught.exception))

    def test_posix_refuses_group_or_other_bits_and_a_foreign_owner(self):
        with mock.patch.object(decision_transport.portable, "WINDOWS", False):
            self.credential.chmod(0o644)
            self.refused()
            self.credential.chmod(0o600)
            self.assertEqual("ok", self.call()["mode"])
            with mock.patch.object(decision_transport.portable.os, "getuid", return_value=os.getuid() + 1):
                self.refused()

    def test_windows_relies_on_the_owner_check_alone(self):
        self.credential.chmod(0o644)                 # what Windows reports as 0o666
        with mock.patch.object(decision_transport.portable, "WINDOWS", True):
            self.assertEqual("ok", self.call()["mode"])
            with mock.patch.object(decision_transport.portable, "owned_by_me", return_value=False):
                self.refused()
