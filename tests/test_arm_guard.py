"""E7.T2 close-out: the arm guard's guarantees, in the guard proxy as built (owner, 2026-10-22: option a).

Each test runs a real `application_browser.consume` of a prepared submit request (manual_application_browser
Fixture) and, inside the session, arms through today's `ArmBinding` (what `browser_guard.Proxy.call_arm` calls):
G1 one arm per request across processes, G2 the gate (pause switch) at arm, G3 the model cannot redirect the
claim, G4 deadline / lease / crash edges never leave a request armable, G5 the arm is bound to the claimed
request. The orphan-proxy window (an arm until the lease ends) is kept as live (decision 2 = No).
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import application_browser  # noqa: E402
import applications  # noqa: E402
import desktop_adapter  # noqa: E402
from manual_application_browser import Fixture  # noqa: E402

FORM = {"observed_form_revision": "fixture-form-v1"}


class ArmGuardGuarantees(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="e7t2-arm-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.fixture = Fixture(root / "fixture")
        self.addCleanup(self.fixture.database.close)
        self.request_id = self.fixture.prepare_submit()

    def session(self, body):
        """Consume the submit request; `body(claim_path)` runs inside the session, then it stops."""
        seen = {}

        def scripted(config, prompt, directory, timeout):
            (directory / "trace.jsonl").write_text("")
            seen.update(body(Path(config["_application_guard_claim"])) or {})
            raise RuntimeError("session ends")
        application_browser.consume(self.fixture.root, self.fixture.database, self.fixture.config,
                                    self.request_id, scripted)
        return seen

    def row(self):
        return self.fixture.database.execute("SELECT * FROM desktop_requests WHERE request_id=?",
                                             (self.request_id,)).fetchone()

    def denials(self):
        return [json.loads(detail)["reason"] for (detail,) in self.fixture.database.execute(
            "SELECT detail FROM desktop_events WHERE event='SUBMIT_ARM_DENIED'")]

    def test_1_arm_after_the_deadline_is_denied_and_writes_nothing(self):
        def body(claim):
            private = json.loads(claim.read_text())
            private["deadline"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
            claim.write_text(json.dumps(private))
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                application_browser.ArmBinding(claim).arm(FORM)
        self.session(body)
        self.assertIsNone(self.row()["armed_at"])

    def test_2_a_second_guard_process_on_the_same_request_cannot_arm_again(self):
        def body(claim):
            self.assertTrue(application_browser.ArmBinding(claim).arm(FORM)["may_submit_once"])
            # A fresh binding (another proxy process: its own `attempted` flag) is refused by armed_at.
            with self.assertRaisesRegex(ValueError, "SUBMIT_INTENT_ALREADY_CONSUMED_OR_READ_ONLY"):
                application_browser.ArmBinding(claim).arm(FORM)
        self.session(body)
        self.assertIsNotNone(self.row()["armed_at"])
        self.assertEqual(self.fixture.database.execute(
            "SELECT count(*) FROM desktop_events WHERE request_id=? AND event='SUBMIT_POSSIBLE'",
            (self.request_id,)).fetchone()[0], 1)

    def test_3_the_pause_switch_is_honoured_at_arm_and_the_denial_recorded(self):
        real = desktop_adapter.resources.read_json

        def paused(name, *args, **kwargs):
            value = real(name, *args, **kwargs)
            return dict(value, applications_enabled=False) if name == "config.json" else value

        def body(claim):
            binding = application_browser.ArmBinding(claim)
            with mock.patch.object(desktop_adapter.resources, "read_json", paused):
                with self.assertRaisesRegex(ValueError, "DESKTOP_SUBMISSION_NOT_ENABLED") as caught:
                    binding.arm(FORM)
            binding.record_denial(caught.exception)       # as browser_guard.Proxy.call_arm does
        self.session(body)
        self.assertIsNone(self.row()["armed_at"])
        self.assertEqual(self.denials(), ["DESKTOP_SUBMISSION_NOT_ENABLED"])

    def test_4_tool_arguments_cannot_redirect_the_claim(self):
        def body(claim):
            for key, value in (("request_id", "another-request"), ("binding", "another-binding")):
                with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_CLAIM_MISMATCH"):
                    application_browser.ArmBinding(claim).arm(dict(FORM, **{key: value}))
            with self.assertRaisesRegex(ValueError, "APPLICATION_ARM_INVALID_OR_EXPIRED"):
                application_browser.ArmBinding(claim).arm({"token": "x", **FORM})   # unknown key
        self.session(body)
        self.assertIsNone(self.row()["armed_at"])

    def test_5_a_lapsed_lease_cannot_be_armed(self):
        def body(claim):
            past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
            with self.fixture.database:
                self.fixture.database.execute("UPDATE desktop_requests SET lease_until=? WHERE request_id=?",
                                              (past, self.request_id))
            with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED_DESKTOP_CLAIM"):
                application_browser.ArmBinding(claim).arm(FORM)
        self.session(body)
        self.assertIsNone(self.row()["armed_at"])

    def test_6_a_crash_after_arming_is_never_handed_out_or_armed_again(self):
        def body(claim):
            application_browser.ArmBinding(claim).arm(FORM)
            return {"claim": json.loads(claim.read_text())}
        seen = self.session(body)                         # the session dies right after arming
        self.assertIsNotNone(self.row()["armed_at"])
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, "STALE_OR_MISMATCHED_DESKTOP_CLAIM|SUBMIT_INTENT_ALREADY_CONSUMED"):
            desktop_adapter.arm(self.fixture.database, self.fixture.root, seen["claim"]["envelope"], later)
        handed = desktop_adapter.claim(self.fixture.database, self.fixture.root, "next-consumer", later,
                                       request_id=self.request_id)
        self.assertIsNone(handed)                         # never claimed again for a second submit
        self.assertNotIn(self.row()["state"], ("PENDING", "CLAIMED"))
        self.assertIsNotNone(self.row()["armed_at"])      # the arm record survives for reconcile


if __name__ == "__main__":
    unittest.main()
