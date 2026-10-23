"""The daily report goes out in parts, and a failed send keeps the transport's own error code
(owner, 2026-10-06: "fix daily report")."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest
from unittest import mock

import applications
import notifications
import reporting
import test_applications_e2e as fixtures
from test_applications_e2e import NOW

REPORT_AT = "2026-09-18T01:00:00+00:00"


def units(text):
    return len(text.encode("utf-16-le")) // 2


class Relay:
    """A bridge relay that fails the send with a given error and reconciles with another."""
    def __init__(self, send_error=None, reconcile_error=None):
        self.send_error, self.reconcile_error, self.sent = send_error, reconcile_error, []

    def send_notification(self, row):
        if self.send_error:
            raise self.send_error
        self.sent.append(row["event_key"])
        return {"provider": "telegram", "chat_id": "mock-owner", "message_id": len(self.sent)}

    def reconcile_notification(self, row):
        raise self.reconcile_error


class DailyReportPartsTests(unittest.TestCase):
    def setUp(self):
        fixtures.ApplicationEndToEndTests.setUp(self)

    def payload(self, blockers):
        payload = reporting.snapshot(self.database, {}, NOW)
        payload["day"] = "2026-09-17"
        payload["outstanding_actions"] = blockers
        return payload

    def test_parts_keep_every_line_whole_and_in_order(self):
        blockers = [{"job_key": f"job-{index:03d}", "code": "MISSING_FACT", "next_action": "x" * 600} for index in range(60)]
        payload = self.payload(blockers)
        parts = reporting.telegram_parts(payload)
        lines = reporting.telegram_lines(payload)
        self.assertGreater(len(parts), 1)
        for index, part in enumerate(parts, 1):
            self.assertTrue(part.startswith(f"(part {index}/{len(parts)})\n"))
            self.assertLessEqual(units(part), reporting.PART_LIMIT)
        joined = "\n".join(part.split("\n", 1)[1] for part in parts)
        self.assertEqual(joined.split("\n")[:len(lines)], lines)
        self.assertNotIn("[summary truncated]", joined)
        self.assertTrue(parts[-1].endswith("Telegram API receipt does not prove human read."))

    def test_one_long_line_is_cut_across_parts_and_the_cap_truncates(self):
        payload = self.payload([{"job_key": "long", "required_input": "😀" * 3000}])
        parts = reporting.telegram_parts(payload)
        self.assertEqual("".join(parts).count("😀"), 3000)
        self.assertTrue(all(units(part) <= reporting.PART_LIMIT for part in parts))
        payload = self.payload([{"job_key": "huge", "required_input": "y" * (reporting.PART_LIMIT * (reporting.MAX_PARTS + 2))}])
        parts = reporting.telegram_parts(payload)
        self.assertEqual(len(parts), reporting.MAX_PARTS)
        self.assertIn("[summary truncated]", parts[-1])
        self.assertIn("Full unabridged report", parts[-1])
        self.assertTrue(all(units(part) <= reporting.PART_LIMIT for part in parts))

    def test_short_report_is_one_message_under_the_historical_key(self):
        reporting.daily(self.database, {"reporting_owner_confirmed": True}, REPORT_AT)
        rows = self.database.execute("SELECT event_key, message FROM notification_outbox").fetchall()
        self.assertEqual([row[0] for row in rows], ["daily-report:2026-09-17"])
        self.assertFalse(rows[0][1].startswith("(part"))

    def test_telegram_gets_the_short_summary_and_the_full_report_stays_saved(self):
        # Owner, 2026-10-06: "short summary. Leave in some key details if there are any load
        # bearing decisions".
        applications.event(self.database, "job-x", "INELIGIBLE",
                           {"code": "OUTSIDE_CONFIRMED_SCOPE", "next_action": "Location IL is outside the owner's locations"}, NOW)
        reporting.daily(self.database, {"reporting_owner_confirmed": True}, REPORT_AT)
        sent = self.database.execute("SELECT message FROM notification_outbox").fetchone()[0]
        self.assertTrue(sent.startswith("Job search, last 24 hours (2026-09-17)"))
        self.assertIn("job-x: not eligible. Location IL is outside the owner's locations", sent)
        self.assertNotIn("Blockers:", sent)
        self.assertIn("Blockers:", reporting.full_report(self.database, "2026-09-17"))

    def test_each_part_is_its_own_outbox_row_sent_in_order(self):
        for index in range(40):
            self.database.execute("INSERT INTO application_blockers VALUES (?,?,?,?,?,?,?)",
                                  (str(index), "MISSING_FACT", "user", "q" * 400, 2, NOW, NOW))
        payload = reporting.daily(self.database, {}, REPORT_AT)
        rows = self.database.execute("SELECT event_key, state FROM notification_outbox ORDER BY rowid").fetchall()
        keys = [row[0] for row in rows]
        self.assertGreater(len(keys), 1)
        self.assertEqual(keys, ["daily-report:2026-09-17"] + [f"daily-report:2026-09-17:{n}" for n in range(2, len(keys) + 1)])
        self.assertEqual({row[1] for row in rows}, {"WAITING_OWNERSHIP"})
        self.assertEqual([part["event_key"] for part in payload["report_parts"]], keys)
        reporting.daily(self.database, {"reporting_owner_confirmed": True}, REPORT_AT)
        self.assertEqual({row[0] for row in self.database.execute("SELECT state FROM notification_outbox")}, {"PENDING"})
        relay = Relay()
        notifications.dispatch(self.database, {}, relay, REPORT_AT)
        self.assertEqual(relay.sent, keys)

    def test_send_error_code_is_recorded_and_state_unchanged(self):
        with self.database:
            applications.notify(self.database, "daily-report:2026-09-17", "report", NOW)
        notifications.dispatch(self.database, {}, Relay(RuntimeError("BRIDGE_HTTP_400")), NOW)
        row = self.database.execute("SELECT * FROM notification_outbox").fetchone()
        self.assertEqual((row["state"], row["last_error"], row["send_error"], row["attempts"]),
                         ("UNCERTAIN", "TELEGRAM_SEND_UNCERTAIN_RECONCILE_BEFORE_RESEND", "BRIDGE_HTTP_400", 1))
        # A reconcile failure keeps the send's code and records its own.
        notifications.dispatch(self.database, {}, Relay(reconcile_error=RuntimeError("RELAY_SEND_UNCERTAIN_NOT_FOUND")), NOW)
        row = self.database.execute("SELECT * FROM notification_outbox").fetchone()
        self.assertEqual((row["state"], row["last_error"], row["send_error"], row["reconcile_error"]),
                         ("UNCERTAIN", "NOTIFICATION_RECONCILIATION_UNAVAILABLE", "BRIDGE_HTTP_400", "RELAY_SEND_UNCERTAIN_NOT_FOUND"))
        status = reporting.notification_status(self.database, "daily-report:2026-09-17")
        self.assertEqual(status["send_error"], "BRIDGE_HTTP_400")

    def test_error_text_that_is_not_a_code_is_reduced_to_its_type(self):
        with self.database:
            applications.notify(self.database, "mock", "hello", NOW)
        notifications.dispatch(self.database, {}, Relay(ValueError("https://bridge.example/api?token=secret")), NOW)
        row = self.database.execute("SELECT * FROM notification_outbox").fetchone()
        self.assertEqual((row["state"], row["send_error"]), ("UNCERTAIN", "ValueError"))
        self.assertNotIn("secret", json.dumps(dict(row)))

    def test_relay_names_the_bridge_state(self):
        relay = notifications.Relay({})
        with self.assertRaisesRegex(RuntimeError, "^RELAY_SEND_UNCERTAIN_NOT_FOUND$"):
            relay.result({"state": "NOT_FOUND"})
        with self.assertRaisesRegex(RuntimeError, "^RELAY_SEND_UNCERTAIN$"):
            relay.result({"state": "weird text http://x"})
        with self.assertRaises(notifications.Terminal):
            relay.result({"state": "FAILED"})


if __name__ == "__main__":
    unittest.main()
