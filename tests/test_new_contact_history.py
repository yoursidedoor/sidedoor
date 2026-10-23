import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import engagement
import outreach
import outreach_agent
import outreach_history
import outreach_history_browser as browser
import manual_new_contact_e2e as manual


class NewContactHistoryTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            boundary = patch(target, side_effect=AssertionError("Offline test only"))
            boundary.start()
            self.addCleanup(boundary.stop)

    def fixture(self, case="clear"):
        fixture = manual.Fixture(case)
        self.addCleanup(fixture.close)
        return fixture

    def test_manual_end_to_end_contracts(self):
        for case in ("clear", "cold", "prior", "pending", "ambiguous"):
            with self.subTest(case=case):
                result = manual.scenario(case)
                self.assertLessEqual(result["writes"], 1)

    def test_full_cold_campaign_uses_existing_provider_claim_and_dispatcher(self):
        result = manual.campaign_scenario()
        self.assertEqual(result["guarded_writes"], 4)
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(result["restart"], "NO_DUPLICATE")

    def test_scheduled_cold_orchestrator_entrypoints_and_recurring_receipt(self):
        result = manual.campaign_scenario(scheduled=True)
        self.assertEqual(result["state"], "CONFIRMED_SENT")
        self.assertEqual(result["guarded_writes"], 4)
        self.assertEqual(result["collector_calls"], 0)  # no LinkedIn browser read (owner, 2026-09-29)
        self.assertGreater(result["missing_provider_lead_reads"], 0)
        self.assertFalse(result["live_verified"])

    def test_missing_provider_lead_is_not_clearance_and_mismatch_blocks(self):
        result = manual.lead_identity_scenario()
        self.assertTrue(result["missing_is_not_history_clearance"])
        self.assertTrue(result["mismatched_observed_lead_blocks"])

    def test_both_routes_use_browser_only_history_and_provider_observed_restrictions(self):
        for route in ("INVITATION", "EXISTING_THREAD"):
            with self.subTest(route=route):
                result = manual.browser_fields_scenario(route)
                self.assertEqual(result["state"], "READY")
                self.assertEqual(result["quota_basis"], "UNKNOWN_PROVIDER_ENFORCED")

    def test_observed_restrictions_and_missing_required_provider_fields_hold(self):
        for route, restriction in (("INVITATION", "account"), ("EXISTING_THREAD", "account"),
                ("EXISTING_THREAD", "thread"), ("EXISTING_THREAD", "missing_thread_status"),
                ("INVITATION", "missing_invitation_status")):
            with self.subTest(route=route, restriction=restriction):
                result = manual.browser_fields_scenario(route, restriction)
                self.assertEqual(result["state"], "HELD")
                self.assertEqual(result["writes"], 0)

    def test_closed_recipient_does_not_invoke_collector(self):
        fixture = self.fixture()
        engagement.close_outreach(fixture.database, manual.PROFILE, "Explicit closure", fixture.now.isoformat())
        self.assertFalse(fixture.prepare()["contexts"])
        self.assertEqual(fixture.collects, 0)
        fixture.client.call.assert_not_called()

    def test_cold_missing_heyreach_lead_does_not_require_existing_conversation(self):
        fixture = self.fixture("cold")
        original = fixture.read
        fixture.client.call.side_effect = lambda operation, arguments: None if operation == "get_lead" else original(operation, arguments)
        result = fixture.prepare()
        self.assertEqual(len(result["campaign_candidates"]), 1, result)
        self.assertFalse(result["contexts"])
        fixture.client._request.assert_not_called()

class BrowserHistoryBindingTests(unittest.TestCase):
    def setUp(self):
        self.request, self.result, self.trace = manual.browser_sample()

    def normalize(self):
        return browser.normalize(self.request, self.result, self.trace, manual.NOW, "mock:trace")

    def test_unique_exact_null_references_resolved_without_mutating_raw(self):
        response = self.normalize()
        self.assertTrue(response["proof"]["direct_messages_complete"])
        self.assertIsNone(response["raw"]["result"]["evidence"][0]["tool_call_id"])
        self.assertEqual(response["raw"]["source_excerpts"][0]["tool_call_id"], "item_0")
        self.assertNotIn("entitlement_verified", response["proof"])
        self.assertNotIn("company_contact_count", response["proof"])

    def test_source_windows_keep_exact_quotes_with_offsets_and_bounded_context(self):
        self.assertEqual(manual.browser_scenario()["state"], "MOCK_TESTED")
        response = self.normalize()
        observed = response["raw"]["observed_text"][0]
        self.assertTrue(observed["complete_text_included"])
        self.assertEqual(observed["windows"][0]["text"], self.result["evidence"][0]["quote"])
        self.assertEqual(observed["windows"][0]["start"], 0)
        self.assertEqual(len(observed["source_sha256"]), 64)

    def test_other_positive_control_keeps_all_three_source_observations(self):
        result = manual.browser_control_scenario(True)
        self.assertEqual(result["state"], "CLEAR")
        self.assertEqual(result["source_bound_observations"], 3)
        self.assertTrue(result["missing_control_source_rejected"])

    def test_inconsistent_other_control_keeps_mocked_ai_unknown(self):
        result = manual.browser_control_scenario(False)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["semantic_judgment"], "MOCKED_AI")
        self.assertFalse(result["live_verified"])

    def test_wrong_reference_is_rejected_even_when_quote_matches_elsewhere(self):
        self.result["evidence"][0]["tool_call_id"] = "item_5"
        with self.assertRaisesRegex(ValueError, "UNOBSERVED_QUOTE"):
            self.normalize()

    def test_ambiguous_null_reference_rejected(self):
        event = json.loads(self.trace.splitlines()[0])
        event["item"]["id"] = "item_10"
        self.trace += b"\n" + json.dumps(event).encode()
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS"):
            self.normalize()

    def test_narration_and_failed_tool_are_not_evidence(self):
        events = [json.loads(line) for line in self.trace.splitlines()]
        events[0]["item"]["status"] = "failed"
        events.append({"type": "item.completed", "item": {"type": "agent_message", "text": self.result["evidence"][0]["quote"]}})
        self.trace = b"\n".join(json.dumps(event).encode() for event in events)
        with self.assertRaisesRegex(ValueError, "ABSENT_OR_AMBIGUOUS"):
            self.normalize()

    def test_unknown_folder_cannot_be_clear(self):
        self.result["coverage"]["MESSAGE_SPAM"] = False
        with self.assertRaisesRegex(ValueError, "CLEARANCE_INCOMPLETE"):
            self.normalize()
        self.result["outcome"] = "UNKNOWN"
        response = self.normalize()
        self.assertFalse(response["proof"]["direct_messages_complete"])
        self.assertIsNone(response["proof"]["prior_outreach"])

    def test_unobserved_folder_cannot_be_complete(self):
        self.result["evidence"] = [entry for entry in self.result["evidence"] if entry["kind"] != "MESSAGE_ARCHIVE"]
        with self.assertRaisesRegex(ValueError, "COVERAGE_UNOBSERVED"):
            self.normalize()

    def test_name_only_account_and_wrong_recipient_fail_binding(self):
        self.result["sender_profile"] = "Alex Me"
        with self.assertRaisesRegex(ValueError, "ACCOUNT_OR_RECIPIENT_UNVERIFIED"):
            self.normalize()

    def test_unreconciled_ledger_cannot_clear_ui(self):
        self.request["ledger_reconciled"] = False
        self.result["source_sha256"] = browser.digest(self.request)
        with self.assertRaisesRegex(ValueError, "CLEARANCE_INCOMPLETE"):
            self.normalize()

    def test_non_browser_tool_or_fabricated_browser_output_rejected(self):
        for change in ({"server": "heyreach"}, {"arguments": {"code": 'nodeRepl.write("invented");'}}):
            with self.subTest(change=change):
                events = [json.loads(line) for line in self.trace.splitlines()]
                events[0]["item"].update(change)
                with self.assertRaisesRegex(ValueError, "UNAUTHORIZED_TOOL|UNSUPPORTED_BROWSER_CODE"):
                    browser.normalize(self.request, self.result, b"\n".join(json.dumps(event).encode() for event in events), manual.NOW, "mock:trace")

    def test_command_ignores_plugin_config_and_keeps_permission_boundaries(self):
        # E7.T1: the user's Codex config (and with it the Chrome plugin, computer-use and any other
        # server) never loads into a browser session; without an operation it has no server at all.
        command = browser.command({"codex": "codex", "model": "mock"}, Path("/mock"))
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--ignore-rules", command)
        self.assertIn("read-only", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("features.apps=false", command)
        self.assertIn('web_search="disabled"', command)
        self.assertEqual(browser.Collector.compute_seconds, 240)


if __name__ == "__main__":
    unittest.main()
