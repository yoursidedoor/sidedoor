import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import subprocess
import unittest
from unittest import mock

import applications
import decision_transport
import notifications
import test_applications_e2e as fixtures
from test_applications_e2e import MockTelegram, NOW


class NotificationRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ApplicationEndToEndTests.setUp(self)
        with self.database:
            applications.notify(self.database, "mock-notification", "Mock user fact needed", NOW)

    def row(self):
        return self.database.execute("SELECT * FROM notification_outbox WHERE event_key='mock-notification'").fetchone()

    def test_missing_relay_configuration_is_known_presend_failure(self):
        with mock.patch("decision_transport.urllib.request.build_opener") as network:
            # A relay with its URL set but no owner or credential (E6.T2 P3: without a URL there is
            # no relay at all, and the dashboard shows the row).
            notifications.dispatch(self.database, {"telegram": {"transport": "bridge_relay"},
                                                   "bridge_transport": {"url": "https://relay.example.test"}}, now=NOW)
        network.assert_not_called()
        self.assertEqual(self.row()["state"], "PENDING")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.row()["presend_attempts"], 1)
        self.assertIn("credentials", self.row()["next_action"])

    def test_unavailable_credentials_have_bounded_separate_retry_budget(self):
        sender = MockTelegram([notifications.Unavailable()] * 5)
        for offset in range(8):
            now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(hours=offset)).isoformat()
            notifications.dispatch(self.database, {}, sender, now)
        self.assertEqual(self.row()["state"], "FAILED")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.row()["presend_attempts"], 5)
        self.assertIsNone(self.row()["next_at"])
        self.assertEqual(self.row()["message"], "Mock user fact needed")
        self.assertEqual(sender.messages, [])

    def test_credentials_recovered_during_backoff_send_once(self):
        sender = MockTelegram([notifications.Unavailable()])
        notifications.dispatch(self.database, {}, sender, NOW)
        notifications.dispatch(self.database, {}, sender, NOW)
        self.assertEqual(self.row()["presend_attempts"], 1)
        notifications.dispatch(self.database, {}, sender, "2026-09-17T17:00:00+00:00")
        self.assertEqual(self.row()["state"], "DELIVERED")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(len(sender.messages), 1)

    def test_keychain_timeout_and_empty_token_never_reach_network(self):
        config = {"telegram": {"keychain_service": "mock", "keychain_account": "mock", "chat_id": "mock-owner"}}
        for result in (subprocess.TimeoutExpired("mock-keychain", 15), mock.Mock(returncode=0, stdout=""), OSError("mock unavailable")):
            with self.subTest(result=type(result).__name__):
                with mock.patch("notifications.subprocess.run") as lookup, mock.patch("notifications.urllib.request.urlopen") as network:
                    if isinstance(result, Exception):
                        lookup.side_effect = result
                    else:
                        lookup.return_value = result
                    with self.assertRaises(notifications.Unavailable):
                        notifications.Telegram(config).send("Mock message")
                network.assert_not_called()

    def test_bridge_missing_file_and_keychain_timeout_are_presend(self):
        settings = {"url": "https://bridge.example", "owner_id": "mock-owner", "credential_file": str(self.root / "missing-mock-token")}
        with mock.patch("decision_transport.urllib.request.build_opener") as network:
            with self.assertRaises(decision_transport.PreSendUnavailable):
                decision_transport.Bridge(settings).call("notifications", {})
            settings.pop("credential_file")
            settings.update(keychain_service="mock", keychain_account="mock")
            with mock.patch("decision_transport.subprocess.run", side_effect=subprocess.TimeoutExpired("mock", 15)):
                with self.assertRaises(decision_transport.PreSendUnavailable):
                    decision_transport.Bridge(settings).call("notifications", {})
        network.assert_not_called()

    def test_timeout_after_request_is_uncertain_and_never_resent(self):
        sender = MockTelegram([TimeoutError("mock send outcome unknown")])
        notifications.dispatch(self.database, {}, sender, NOW)
        notifications.dispatch(self.database, {}, sender, "2026-09-18T16:00:00+00:00")
        self.assertEqual(self.row()["state"], "UNCERTAIN")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertIn("unsupported", self.row()["next_action"])
        self.assertEqual(sender.messages, [])

    def test_reconciliation_receipt_is_validated_and_budget_bounded(self):
        transport = mock.Mock()
        transport.send_notification.side_effect = TimeoutError("Mock lost ack")
        transport.reconcile_notification.return_value = {"acknowledged": True}
        notifications.dispatch(self.database, {}, transport, NOW)
        for offset in range(7):
            now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(days=offset)).isoformat()
            notifications.dispatch(self.database, {}, transport, now)
        self.assertEqual(self.row()["state"], "UNCERTAIN")
        self.assertEqual(self.row()["reconcile_attempts"], 5)
        self.assertEqual(self.row()["last_error"], "NOTIFICATION_RECONCILIATION_EXHAUSTED")
        self.assertIsNone(self.row()["delivery"])
        transport.send_notification.assert_called_once()

    def test_reconciliation_backoff_prevents_hot_polling(self):
        transport = mock.Mock()
        transport.send_notification.side_effect = TimeoutError()
        transport.reconcile_notification.side_effect = TimeoutError()
        notifications.dispatch(self.database, {}, transport, NOW)
        notifications.dispatch(self.database, {}, transport, NOW)
        notifications.dispatch(self.database, {}, transport, NOW)
        transport.reconcile_notification.assert_called_once()
        self.assertEqual(self.row()["next_at"], "2026-09-17T16:05:00+00:00")

    def test_relay_confirmed_failure_is_retained(self):
        transport = mock.Mock()
        transport.send_notification.side_effect = TimeoutError()
        transport.reconcile_notification.side_effect = notifications.Terminal()
        notifications.dispatch(self.database, {}, transport, NOW)
        notifications.dispatch(self.database, {}, transport, NOW)
        self.assertEqual(self.row()["state"], "FAILED")
        self.assertEqual(self.row()["last_error"], "RELAY_CONFIRMED_FAILED")
        self.assertIn("blocker remains unresolved", self.row()["next_action"])
        transport.send_notification.assert_called_once()

    def test_relay_confirmed_pending_honors_retry_after(self):
        transport = mock.Mock()
        transport.send_notification.side_effect = TimeoutError()
        transport.reconcile_notification.side_effect = notifications.Transient(900)
        notifications.dispatch(self.database, {}, transport, NOW)
        notifications.dispatch(self.database, {}, transport, NOW)
        self.assertEqual(self.row()["state"], "PENDING")
        self.assertEqual(self.row()["next_at"], "2026-09-17T16:15:00+00:00")
        transport.send_notification.assert_called_once()

    def test_interrupted_send_reconciles_receipt_without_resending(self):
        with self.database:
            self.database.execute("UPDATE notification_outbox SET state='SENDING',attempts=1")
        transport = mock.Mock()
        transport.reconcile_notification.return_value = {"provider": "telegram", "chat_id": "mock-owner", "message_id": 45}
        notifications.dispatch(self.database, {}, transport, NOW)
        self.assertEqual(self.row()["state"], "DELIVERED")
        self.assertEqual(json.loads(self.row()["delivery"])["message_id"], 45)
        transport.send_notification.assert_not_called()

    def test_failed_notification_does_not_stop_independent_notification(self):
        with self.database:
            applications.notify(self.database, "second", "Other mock user blocker", NOW)
        sender = MockTelegram([notifications.Terminal()])
        notifications.dispatch(self.database, {}, sender, NOW)
        self.assertEqual(self.row()["state"], "FAILED")
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox WHERE event_key='second'").fetchone()[0], "DELIVERED")
