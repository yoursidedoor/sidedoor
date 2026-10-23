import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import outreach
import outreach_provider
import engagement
from orchestrator import profile_key


class OutreachProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "provider.sqlite3"
        self.database = self.connect()
        outreach.initialize_actions(self.database)
        self.database.commit()
        self.config = {"sender_id": 900001,
                       "sender_profile": "https://www.linkedin.com/in/synthetic-sender",
                       "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE"]}
        self.action = {"action_id": "synthetic-action", "profile": "https://www.linkedin.com/in/synthetic-recipient",
                       "recipient": "Synthetic Recipient", "company": "Synthetic Company",
                       "purpose": "INITIAL", "provider": "heyreach", "channel": "linkedin",
                       "conversation_id": "synthetic-room", "message": "Synthetic exact message", "subject": ""}
        self.account = {"id": self.config["sender_id"], "profileUrl": self.config["sender_profile"],
                        "authIsValid": True, "isActive": True, "accountLimits": {"observedUnknownField": 7}}
        self.room = self.make_room("synthetic-room")
        self.responses = {"get_linked_in_account_by_id": self.account,
                          "get_conversations_v2": {"totalCount": 1, "items": [self.room]},
                          "get_chatroom": self.room,
                          "get_campaigns_for_lead": {"totalCount": 0, "items": []}}
        self.client = Mock(spec=["call", "_request"])
        self.client.call.side_effect = self.read
        self.client._request.return_value = {"success": True, "opaque": {"retained": [1, 2]}}
        self.provider = outreach_provider.Provider(self.database, self.config, self.client)
        self.claim_count = 0
        self.now = dt.datetime.now(dt.timezone.utc)
        self.action["verification"] = {"verified_at": self.now.isoformat(), "content_verified": True,
            "content_sources": ["mock-resume"], "identity_verified": True, "identity_source": "mock-identity",
            "team_relevance_verified": True, "team_source": "mock-team"}

    def connect(self):
        database = sqlite3.connect(self.database_path)
        database.row_factory = sqlite3.Row
        self.addCleanup(database.close)
        return database

    def make_room(self, identifier):
        return {"id": identifier, "linkedInAccountId": self.config["sender_id"],
                "linkedInAccount": copy.deepcopy(self.account), "groupChat": False,
                "correspondentProfile": {"profileUrl": self.action["profile"], "linkedin_id": "saved-lead"},
                "messages": [], "totalMessages": 0, "opaqueProviderField": {"preserve": True}}

    def read(self, operation, arguments):
        response = self.responses[operation]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def collect(self, action=None, claim=None):
        return self.provider.collect(action or self.action, "RECONCILIATION" if claim else "INSPECTION", claim)

    def seed_claim(self, action_changes=None, claim_changes=None, row_changes=None):
        self.claim_count += 1
        action = dict(self.action, action_id="synthetic-action-" + str(self.claim_count))
        action.update(action_changes or {})
        payload = json.dumps(action, sort_keys=True)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        preflight = {"identity": profile_key(action["profile"]), "sender_id": self.config["sender_id"],
            "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
            "sent_invitations_complete": True, "pending_invitation": False, "has_reply": False,
            "negative_reply": False, "opt_out": False, "cooldown": False, "prior_outreach": False,
            "checked_at": self.now.isoformat(), "evidence": "mock-independent-direct-history",
            "conversation_id": action.get("conversation_id"), "company_contact_count": 0,
            "company_count_evidence": "mock-company-history", "followups_sent": 0, "initial_message_sent": False,
            "agent_decision": "READY", "provider_snapshot_complete": True}
        claim = {"action_id": action["action_id"], "payload": payload, "payload_hash": payload_hash,
                 "identity": profile_key(action["profile"]), "sender_id": self.config["sender_id"],
                 "preflight": json.dumps(preflight), "claimed_at": self.now.isoformat()}
        claim.update(claim_changes or {})
        row = {"action_id": action["action_id"], "identity": profile_key(action["profile"]),
               "slot": action["action_id"], "payload": payload, "payload_hash": payload_hash,
               "state": "REQUEST_STARTED", "claimed_at": claim["claimed_at"],
               "result": json.dumps({"preflight": {"retained": True}})}
        row.update(row_changes or {})
        with self.database:
            self.database.execute("INSERT INTO outreach_actions VALUES (:action_id,:identity,:slot,:payload,:payload_hash,:state,:claimed_at,:result)", row)
            self.database.execute("INSERT INTO outreach_action_claims VALUES (:action_id,:payload,:payload_hash,:identity,:sender_id,:preflight,:claimed_at)", claim)
        return action, claim

    def test_construction_does_not_call_provider(self):
        self.client.call.assert_not_called()
        self.client._request.assert_not_called()

    def test_raw_envelope_aliases_exact_arguments_and_timestamps(self):
        result = self.collect()
        expected = [call("get_linked_in_account_by_id", {"accountId": 900001}),
                    call("get_conversations_v2", {"linkedInAccountIds": [900001],
                         "leadProfileUrl": self.action["profile"], "offset": 0, "limit": 100}),
                    call("get_chatroom", {"accountId": 900001, "conversationId": "synthetic-room"}),
                    call("get_campaigns_for_lead", {"profileUrl": self.action["profile"], "offset": 0, "limit": 100})]
        self.assertEqual(self.client.call.call_args_list, expected)
        self.assertEqual(result["reads"], result["observations"])
        self.assertEqual(result["observed_at"], result["finished_at"])
        self.assertEqual(result["selected_conversation_id"], "synthetic-room")
        self.assertEqual(json.loads(json.dumps(result)), result)
        for observation, expected_call in zip(result["reads"], expected):
            self.assertEqual(observation["arguments"], expected_call.args[1])
            self.assertEqual(observation["operation"], observation["tool"])
            self.assertEqual(observation["source"], "heyreach:" + observation["tool"])
            self.assertEqual(observation["raw"], self.responses[observation["tool"]])
            self.assertEqual(observation["result"], observation["raw"])
            self.assertEqual(observation["status"], "OBSERVED")
            self.assertTrue(observation["authenticated"])
            self.assertLessEqual(dt.datetime.fromisoformat(observation["started_at"]),
                                 dt.datetime.fromisoformat(observation["observed_at"]))
        self.room["opaqueProviderField"]["preserve"] = False
        self.assertTrue(result["reads"][2]["raw"]["opaqueProviderField"]["preserve"])
        self.client._request.assert_not_called()

    def test_empty_inbox_never_clears_direct_history_or_entitlement(self):
        self.responses["get_conversations_v2"] = {"totalCount": 0, "items": []}
        result = self.collect()
        self.assertEqual(result["coverage"]["get_conversations_v2"]["status"], "BOUNDED_SCAN_COMPLETE")
        for field in ("direct_messages_complete", "sent_invitations_complete", "full_linkedin_history_clearance"):
            self.assertIs(result[field], False)
        self.assertEqual(result["direct_history_status"], "UNKNOWN")
        self.assertEqual(result["invitation_history_status"], "UNKNOWN")
        self.assertNotIn("entitlement_verified", result)
        self.assertIsNone(result["selected_conversation_id"])
        self.assertFalse(any(item.args[0] == "get_chatroom" for item in self.client.call.call_args_list))

    def test_wrong_account_id_or_sender_profile_cannot_advance(self):
        for changes in ({"id": 900002}, {"profileUrl": self.action["profile"]}, {"id": "900001"}):
            with self.subTest(changes=changes):
                self.responses["get_linked_in_account_by_id"] = dict(self.account, **changes)
                self.client.call.reset_mock()
                result = self.collect()
                self.assertFalse(result["identity_verified"])
                self.assertEqual(result["coverage"]["account"]["status"], "UNKNOWN")
                self.assertIsNone(result["selected_conversation_id"])
                self.assertFalse(any(item.args[0] == "get_chatroom" for item in self.client.call.call_args_list))

    def test_listing_and_room_bind_account_sender_recipient_and_thread(self):
        variants = [{"linkedInAccountId": 900002}, {"linkedInAccount": dict(self.account, id=900002)},
                    {"linkedInAccount": dict(self.account, profileUrl=self.action["profile"])},
                    {"correspondentProfile": {"profileUrl": self.config["sender_profile"]}}, {"groupChat": True}]
        for location in ("listing", "chatroom"):
            for changes in variants + ([{"id": "wrong-thread"}] if location == "chatroom" else []):
                with self.subTest(location=location, changes=changes):
                    changed = dict(self.room, **changes)
                    self.responses["get_conversations_v2"] = {"totalCount": 1, "items": [changed if location == "listing" else self.room]}
                    self.responses["get_chatroom"] = changed if location == "chatroom" else self.room
                    self.client.call.reset_mock()
                    result = self.collect()
                    self.assertFalse(result["identity_verified"])
                    self.assertIsNone(result["selected_conversation_id"])
                    self.assertEqual(result["coverage"]["chatrooms"]["status"], "UNKNOWN")
                    if location == "listing":
                        self.assertFalse(any(item.args[0] == "get_chatroom" for item in self.client.call.call_args_list))

    def test_absent_action_thread_selects_only_one_exact_match(self):
        action = dict(self.action)
        del action["conversation_id"]
        self.assertEqual(self.collect(action)["selected_conversation_id"], "synthetic-room")
        rooms = [self.room, self.make_room("second-room")]
        self.responses["get_conversations_v2"] = {"totalCount": 2, "items": rooms}
        self.client.call.side_effect = lambda operation, arguments: next(
            copy.deepcopy(room) for room in rooms if room["id"] == arguments["conversationId"]
        ) if operation == "get_chatroom" else self.read(operation, arguments)
        result = self.collect(action)
        self.assertIsNone(result["selected_conversation_id"])
        self.assertIn("EXACT_CONVERSATION_ABSENT_OR_AMBIGUOUS", result["blockers"])

    def test_saved_lead_alias_preserves_profile_binding_and_page_budget(self):
        action = dict(self.action, profile="https://www.linkedin.com/in/ant%C3%B3nio")
        room = copy.deepcopy(self.room)
        room["correspondentProfile"]["profileUrl"] = "https://www.linkedin.com/in/antónio/"
        self.responses["get_chatroom"] = room

        def read(operation, arguments):
            if operation == "get_conversations_v2":
                return {"totalCount": 1, "items": [room]} if "leadLinkedInId" in arguments else {"totalCount": 0, "items": []}
            return self.read(operation, arguments)

        self.client.call.side_effect = read
        with patch.object(outreach_provider, "known_provider_lead", return_value="saved-lead") as lookup:
            result = self.collect(action)
        lookup.assert_called_once_with(self.database, action["profile"], 900001)
        self.assertEqual(result["selected_conversation_id"], "synthetic-room")
        self.assertEqual(result["coverage"]["get_conversations_v2"]["pages"], 2)
        self.client.call.assert_any_call("get_conversations_v2", {
            "linkedInAccountIds": [900001], "leadLinkedInId": "saved-lead", "offset": 0, "limit": 100})
        self.client.call.assert_any_call("get_campaigns_for_lead", {
            "linkedinId": "saved-lead", "offset": 0, "limit": 100})
        self.assertFalse(any(item.args[0] == "get_lead" for item in self.client.call.call_args_list))
        room["correspondentProfile"]["linkedin_id"] = "unrelated-lead"
        with patch.object(outreach_provider, "known_provider_lead", return_value="saved-lead"):
            self.assertFalse(self.collect(action)["identity_verified"])

    def fresh_lead_reader(self, lead):
        self.room["correspondentProfile"]["linkedin_id"] = "900009"

        def read(operation, arguments):
            if operation == "get_lead":
                if isinstance(lead, Exception):
                    raise lead
                return copy.deepcopy(lead)
            if operation == "get_conversations_v2":
                return {"totalCount": 1, "items": [copy.deepcopy(self.room)]} if "leadLinkedInId" in arguments else {"totalCount": 0, "items": []}
            return self.read(operation, arguments)

        return read

    def test_fresh_lead_binds_canonical_profile_and_preserves_raw_without_clearance(self):
        action = dict(self.action, profile="https://www.linkedin.com/in/ant%C3%B3nio")
        self.room["correspondentProfile"]["profileUrl"] = "https://www.linkedin.com/in/antónio/"
        lead = {"profileUrl": self.room["correspondentProfile"]["profileUrl"],
                "linkedin_id": "900009", "username": "antónio", "opaque": {"preserve": True}}
        self.client.call.side_effect = self.fresh_lead_reader(lead)
        original = copy.deepcopy(action)
        result = self.collect(action)
        self.assertEqual(self.client.call.call_args_list, [
            call("get_linked_in_account_by_id", {"accountId": 900001}),
            call("get_conversations_v2", {"linkedInAccountIds": [900001], "leadProfileUrl": action["profile"], "offset": 0, "limit": 100}),
            call("get_lead", {"profileUrl": action["profile"]}),
            call("get_conversations_v2", {"linkedInAccountIds": [900001], "leadLinkedInId": "900009", "offset": 0, "limit": 100}),
            call("get_chatroom", {"accountId": 900001, "conversationId": "synthetic-room"}),
            call("get_campaigns_for_lead", {"linkedinId": "900009", "offset": 0, "limit": 100})])
        self.assertEqual(result["selected_conversation_id"], "synthetic-room")
        self.assertEqual(result["coverage"]["get_conversations_v2"]["pages"], 2)
        observation = next(item for item in result["observations"] if item["operation"] == "get_lead")
        self.assertEqual(observation["raw"], lead)
        self.assertTrue(observation["identity_matches"])
        self.assertTrue(observation["authenticated"])
        self.assertEqual(action, original)
        lead["opaque"]["preserve"] = False
        self.assertTrue(observation["raw"]["opaque"]["preserve"])
        for field in ("direct_messages_complete", "sent_invitations_complete", "full_linkedin_history_clearance"):
            self.assertIs(result[field], False)
        self.assertEqual(result["direct_history_status"], "UNKNOWN")
        self.assertEqual(result["invitation_history_status"], "UNKNOWN")
        self.assertNotIn("entitlement_verified", result)
        self.client._request.assert_not_called()

    def test_fresh_lead_invalid_identity_never_substitutes_lookup_arguments(self):
        valid = {"profileUrl": self.action["profile"], "linkedin_id": "900009"}
        variants = [None, [], {}, dict(valid, profileUrl=self.config["sender_profile"]),
                    dict(valid, profileUrl=self.action["profile"] + "?other=1")]
        variants.extend(dict(valid, linkedin_id=value) for value in
                        (None, True, 900009, "", " ", "900009 ", "ACo A", "ACo\tA", "ACo\nA",
                         "ACo\x00A", "ACo\x7fA", "ACo\u200bA", "ACo\u00a0A", "A" * 2049))
        for lead in variants:
            with self.subTest(lead=lead):
                self.client.call.reset_mock()
                self.client.call.side_effect = self.fresh_lead_reader(lead)
                result = self.collect()
                self.assertFalse(result["identity_verified"])
                self.assertIsNone(result["selected_conversation_id"])
                self.assertEqual(result["coverage"]["get_lead"]["status"], "UNKNOWN")
                self.assertIn("LEAD_UNVERIFIED_OR_MISMATCHED", result["blockers"])
                self.assertFalse(any("leadLinkedInId" in item.args[1] or "linkedinId" in item.args[1]
                                     for item in self.client.call.call_args_list))
                self.client.call.assert_any_call("get_campaigns_for_lead", {
                    "profileUrl": self.action["profile"], "offset": 0, "limit": 100})
        self.client._request.assert_not_called()

    def test_fresh_lead_preserves_opaque_identifiers_without_format_assumptions(self):
        for identifier in ("900009", "ACoA-Synthetic_123", "urn:li:member:900009", "saved-lead", "0", "９００００９", "A" * 2048):
            with self.subTest(identifier=identifier):
                self.client.call.reset_mock()
                lead = {"profileUrl": self.action["profile"], "linkedin_id": identifier}
                self.client.call.side_effect = self.fresh_lead_reader(lead)
                self.room["correspondentProfile"]["linkedin_id"] = identifier
                result = self.collect()
                self.assertEqual(result["selected_conversation_id"], "synthetic-room")
                self.client.call.assert_any_call("get_conversations_v2", {
                    "linkedInAccountIds": [900001], "leadLinkedInId": identifier, "offset": 0, "limit": 100})
                self.client.call.assert_any_call("get_campaigns_for_lead", {
                    "linkedinId": identifier, "offset": 0, "limit": 100})
                self.assertFalse(result["full_linkedin_history_clearance"])
                self.assertNotIn("entitlement_verified", result)
        self.client._request.assert_not_called()

    def test_fresh_opaque_id_requires_exact_listing_and_chatroom_binding(self):
        for location in ("listing", "chatroom"):
            with self.subTest(location=location):
                lead = {"profileUrl": self.action["profile"], "linkedin_id": "ACoA-Synthetic_123"}
                base_read = self.fresh_lead_reader(lead)
                self.room["correspondentProfile"]["linkedin_id"] = lead["linkedin_id"]

                def read(operation, arguments):
                    response = copy.deepcopy(base_read(operation, arguments))
                    if location == "listing" and operation == "get_conversations_v2" and "leadLinkedInId" in arguments:
                        response["items"][0]["correspondentProfile"]["linkedin_id"] = "other-id"
                    if location == "chatroom" and operation == "get_chatroom":
                        response["correspondentProfile"]["linkedin_id"] = "other-id"
                    return response

                self.client.call.side_effect = read
                result = self.collect()
                self.assertFalse(result["identity_verified"])
                self.assertIsNone(result["selected_conversation_id"])
        self.client._request.assert_not_called()

    def test_fresh_lead_failed_read_remains_unknown(self):
        for failure in (NotImplementedError("unsupported"), TimeoutError("credential-bearing detail")):
            with self.subTest(failure=type(failure).__name__):
                self.client.call.reset_mock()
                self.client.call.side_effect = self.fresh_lead_reader(failure)
                result = self.collect()
                self.assertEqual(result["coverage"]["get_lead"]["status"], "UNKNOWN")
                self.assertIn("get_lead:PROVIDER_READ_FAILED", result["blockers"])
                self.assertIsNone(result["selected_conversation_id"])
                self.assertFalse(result["full_linkedin_history_clearance"])
                self.assertNotIn("credential-bearing", json.dumps(result))
                self.assertEqual(sum(item.args[0] == "get_conversations_v2" for item in self.client.call.call_args_list), 1)
        self.client._request.assert_not_called()

    def test_fresh_lead_requires_verified_account_and_complete_empty_profile_lookup(self):
        cases = [(dict(self.account, id=900002), {"totalCount": 0, "items": []}),
                 (self.account, {"totalCount": 1, "items": []}),
                 (self.account, {"totalCount": 1, "items": [dict(self.room, groupChat=True)]}),
                 (self.account, {"totalCount": 1, "items": [self.room]})]
        for account, conversations in cases:
            with self.subTest(account=account["id"], conversations=conversations):
                self.responses["get_linked_in_account_by_id"] = account
                self.responses["get_conversations_v2"] = conversations
                self.client.call.reset_mock()
                with patch.object(outreach_provider, "known_provider_lead", return_value=None):
                    self.collect()
                self.assertFalse(any(item.args[0] == "get_lead" for item in self.client.call.call_args_list))

    def test_fresh_lead_fallback_retains_shared_page_and_read_limits(self):
        base_read = self.fresh_lead_reader({"profileUrl": self.action["profile"], "linkedin_id": "900009"})

        def read(operation, arguments):
            if operation == "get_conversations_v2" and "leadLinkedInId" in arguments:
                rooms = [dict(self.room, id="room-" + str(index)) for index in range(arguments["offset"], arguments["offset"] + 100)]
                return {"totalCount": 401, "items": rooms}
            if operation == "get_chatroom":
                return dict(self.room, id=arguments["conversationId"])
            if operation == "get_campaigns_for_lead":
                return {"totalCount": 501, "items": [{"id": index} for index in range(arguments["offset"], arguments["offset"] + 100)]}
            return base_read(operation, arguments)

        self.client.call.side_effect = read
        result = self.collect()
        self.assertEqual(self.client.call.call_count, 21)
        self.assertEqual(result["coverage"]["get_conversations_v2"]["pages"], 5)
        self.assertEqual(result["coverage"]["get_conversations_v2"]["status"], "UNKNOWN")
        fallback = [item.args[1] for item in self.client.call.call_args_list if "leadLinkedInId" in item.args[1]]
        self.assertEqual([arguments["offset"] for arguments in fallback], [0, 100, 200, 300])
        self.assertEqual(result["coverage"]["get_campaigns_for_lead"]["reason"], "READ_BUDGET_EXHAUSTED")
        self.assertFalse(result["full_linkedin_history_clearance"])
        self.client._request.assert_not_called()

    def test_fresh_lead_read_does_not_reset_scheduling_deadline(self):
        clock = [0]
        base_read = self.fresh_lead_reader({"profileUrl": self.action["profile"], "linkedin_id": "900009"})

        def read(operation, arguments):
            if operation == "get_lead":
                clock[0] = 121
            return base_read(operation, arguments)

        self.client.call.side_effect = read
        with patch.object(outreach_provider.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.collect()
        self.assertEqual(self.client.call.call_count, 3)
        self.assertEqual(result["coverage"]["get_conversations_v2"]["reason"], "READ_BUDGET_EXHAUSTED")
        self.assertIsNone(result["selected_conversation_id"])
        self.client._request.assert_not_called()

    def test_listing_and_chatroom_caps_leave_unknown_coverage(self):
        def read(operation, arguments):
            if operation in {"get_conversations_v2", "get_campaigns_for_lead"}:
                offset = arguments["offset"]
                items = [self.make_room("room-" + str(index)) if operation == "get_conversations_v2" else {"id": index}
                         for index in range(offset, offset + 100)]
                return {"totalCount": 501, "items": items}
            if operation == "get_chatroom":
                return self.make_room(arguments["conversationId"])
            return self.read(operation, arguments)

        self.client.call.side_effect = read
        result = self.collect()
        for operation in ("get_conversations_v2", "get_campaigns_for_lead"):
            arguments = [item.args[1] for item in self.client.call.call_args_list if item.args[0] == operation]
            self.assertEqual([item["offset"] for item in arguments], [0, 100, 200, 300, 400])
            self.assertTrue(all(item["limit"] == 100 for item in arguments))
            self.assertEqual(result["coverage"][operation]["status"], "UNKNOWN")
            self.assertTrue(result["coverage"][operation]["truncated"])
        self.assertEqual(sum(item.args[0] == "get_chatroom" for item in self.client.call.call_args_list), 10)
        self.assertEqual(self.client.call.call_count, 21)
        self.assertEqual(result["coverage"]["chatrooms"]["status"], "UNKNOWN")
        self.client._request.assert_not_called()

    def test_unsupported_failed_and_incomplete_pages_are_unknown(self):
        responses = [None, [], {"items": []}, {"totalCount": True, "items": []},
                     {"totalCount": 1, "items": []}, {"totalCount": 0, "items": [self.room]},
                     TimeoutError("credential-bearing detail must not be exposed")]
        for operation in ("get_conversations_v2", "get_campaigns_for_lead"):
            original = self.responses[operation]
            for response in responses:
                with self.subTest(operation=operation, response=response):
                    self.responses[operation] = response
                    result = self.collect()
                    self.assertEqual(result["coverage"][operation]["status"], "UNKNOWN")
                    self.assertTrue(result["blockers"])
                    self.assertNotIn("credential-bearing", json.dumps(result))
            self.responses[operation] = original

    def test_repeated_or_changing_pages_cannot_prove_coverage(self):
        for total in (2, 3):
            with self.subTest(second_total=total):
                def read(operation, arguments):
                    if operation == "get_campaigns_for_lead":
                        return {"totalCount": 2 if arguments["offset"] == 0 else total, "items": [{"id": 1}]}
                    return self.read(operation, arguments)
                self.client.call.side_effect = read
                result = self.collect()
                self.assertEqual(result["coverage"]["get_campaigns_for_lead"]["status"], "UNKNOWN")

    def test_truncated_room_preserves_raw_without_history_clearance(self):
        self.responses["get_chatroom"] = dict(self.room, totalMessages=2)
        result = self.collect()
        observation = next(item for item in result["reads"] if item["tool"] == "get_chatroom")
        self.assertTrue(observation["identity_matches"])
        self.assertEqual(observation["message_coverage"], "UNKNOWN")
        self.assertEqual(observation["raw"]["totalMessages"], 2)
        self.assertEqual(result["coverage"]["chatrooms"]["status"], "UNKNOWN")
        self.assertFalse(result["direct_messages_complete"])

    def test_expired_scheduling_budget_starts_no_reads(self):
        with patch.object(outreach_provider.time, "monotonic", side_effect=itertools.chain([0], itertools.repeat(121))):
            result = self.collect()
        self.client.call.assert_not_called()
        self.assertTrue(all(item["status"] == "UNKNOWN" and item["attempted"] is False for item in result["reads"]))

    def test_reconciliation_claim_identity_and_hash_are_checked_before_reads(self):
        action, claim = self.seed_claim()
        result = self.collect(action, claim)
        self.assertEqual(result["claim_payload_hash"], claim["payload_hash"])
        for changes in ({"sender_id": 900002}, {"action_id": "wrong"}, {"payload_hash": "0" * 64},
                        {"identity": "linkedin.com/in/wrong"}, {"payload": "{}"}):
            with self.subTest(changes=changes):
                self.client.call.reset_mock()
                with self.assertRaises(ValueError):
                    self.collect(action, dict(claim, **changes))
                self.client.call.assert_not_called()

    def test_send_commits_exact_claim_arguments_once_across_connections(self):
        action, claim = self.seed_claim({"subject": "Exact subject", "purpose": "FIRST_MESSAGE"})
        other = self.connect()
        restarted = outreach_provider.Provider(other, self.config, self.client)
        expected = {"conversationId": action["conversation_id"], "linkedInAccountId": 900001,
                    "message": action["message"], "subject": action["subject"]}

        def send(operation, arguments):
            self.assertEqual((operation, arguments), ("send_message", expected))
            row = other.execute("SELECT state,result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
            self.assertEqual(row["state"], "REQUEST_STARTED")
            result = json.loads(row["result"])
            self.assertEqual(result["preflight"], {"retained": True})
            self.assertEqual(result["provider_request"]["arguments"], expected)
            self.assertEqual(result["provider_request"]["payload_hash"], claim["payload_hash"])
            with self.assertRaises(ValueError):
                restarted.send_claimed(action["action_id"], self.config)
            return {"success": True, "opaque": [1, 2]}

        self.client._request.side_effect = send
        result = self.provider.send_claimed(action["action_id"], self.config)
        self.assertEqual(result["status"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(result["raw"], {"success": True, "opaque": [1, 2]})
        with self.assertRaises(ValueError):
            restarted.send_claimed(action["action_id"], self.config)
        self.client._request.assert_called_once_with("send_message", expected)
        self.client.call.assert_not_called()

    def test_transport_timeout_consumes_claim_without_retry(self):
        action, unused_claim = self.seed_claim()
        self.client._request.side_effect = TimeoutError("synthetic uncertainty")
        with self.assertRaises(TimeoutError):
            self.provider.send_claimed(action["action_id"], self.config)
        restarted = outreach_provider.Provider(self.connect(), self.config, self.client)
        with self.assertRaises(ValueError):
            restarted.send_claimed(action["action_id"], self.config)
        self.client._request.assert_called_once()

    def test_transport_requires_matching_durable_action_claim_and_started_state(self):
        with self.assertRaises(ValueError):
            self.provider.send_claimed("missing", self.config)
        for row_changes in ({"state": "DISPATCHING"}, {"payload": "{}"}, {"payload_hash": "bad"},
                            {"identity": "linkedin.com/in/wrong"}):
            with self.subTest(row_changes=row_changes):
                action, unused_claim = self.seed_claim(row_changes=row_changes)
                with self.assertRaises(ValueError):
                    self.provider.send_claimed(action["action_id"], self.config)
        action, unused_claim = self.seed_claim(claim_changes={"payload_hash": "bad"}, row_changes={"payload_hash": "bad"})
        with self.assertRaises(ValueError):
            self.provider.send_claimed(action["action_id"], self.config)
        action, unused_claim = self.seed_claim(claim_changes={"sender_id": 900002})
        with self.assertRaises(ValueError):
            self.provider.send_claimed(action["action_id"], self.config)
        self.client._request.assert_not_called()

    def test_transport_requires_explicit_scope_and_matching_configured_account(self):
        action, unused_claim = self.seed_claim()
        for changes in ({"outreach_allowed_purposes": None}, {"outreach_allowed_purposes": []},
                        {"outreach_allowed_purposes": "INITIAL"}, {"outreach_actions_enabled": False},
                        {"outreach_mode": "unguarded"}, {"sender_id": 900002},
                        {"sender_profile": self.action["profile"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.provider.send_claimed(action["action_id"], dict(self.config, **changes))
        config = dict(self.config)
        del config["outreach_allowed_purposes"]
        with self.assertRaises(ValueError):
            self.provider.send_claimed(action["action_id"], config)
        self.client._request.assert_not_called()

    def test_transport_rejects_campaign_followup_new_scope_and_changed_arguments(self):
        variants = [{"purpose": purpose} for purpose in ("PREPARE_CAMPAIGN", "FOLLOWUP", "URGENT", "NEW_SCOPE")]
        variants += [{"operation": "create_campaign"}, {"arguments": {"message": "substituted"}},
                     {"sender_id": 900002}, {"provider": "other"}, {"channel": "email"},
                     {"conversation_id": ""}, {"message": " "}, {"subject": None}]
        for changes in variants:
            with self.subTest(changes=changes):
                action, unused_claim = self.seed_claim(changes)
                config = dict(self.config, outreach_allowed_purposes=[action["purpose"]])
                with self.assertRaises(ValueError):
                    self.provider.send_claimed(action["action_id"], config)
        self.client._request.assert_not_called()

    def test_transport_refuses_uncommitted_parent_transaction(self):
        action, unused_claim = self.seed_claim()
        self.database.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            self.provider.send_claimed(action["action_id"], self.config)
        self.assertTrue(self.database.in_transaction)
        self.database.rollback()
        self.client._request.assert_not_called()

    def assert_final_guard_cancelled(self, action, claim, gate):
        with self.assertRaisesRegex(ValueError, "Final provider stop or freshness check failed"):
            self.provider.send_claimed(action["action_id"], self.config)
        row = self.database.execute("SELECT state,result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
        self.assertEqual(row["state"], "CANCELLED_BEFORE_SEND")
        result = json.loads(row["result"])
        self.assertNotIn("provider_request", result)
        self.assertEqual(result["preflight"], {"retained": True})
        self.assertIn(gate, result["provider_guard"]["gates"])
        saved_claim = self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()
        self.assertEqual(dict(saved_claim), claim)
        restarted = outreach_provider.Provider(self.connect(), self.config, self.client)
        with self.assertRaises(ValueError):
            restarted.send_claimed(action["action_id"], self.config)
        self.client._request.assert_not_called()

    def add_suppressions(self, action, reason):
        with self.database:
            self.database.execute("CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
            for identity in (profile_key(action["profile"]), "name:" + outreach.name_key(action["recipient"])):
                self.database.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?)", (identity, reason))

    def test_final_guard_allows_own_initial_reservation_suppressions(self):
        action, unused_claim = self.seed_claim()
        self.add_suppressions(action, "Handoff historical outreach; never repeat initial contact")
        with patch("outreach_provider._now", return_value=self.now.isoformat()):
            self.provider.send_claimed(action["action_id"], self.config)
        self.client._request.assert_called_once()
        self.assertEqual(self.database.execute("SELECT count(*) FROM suppressions").fetchone()[0], 2)

    def test_final_guard_late_reply_or_optout_wins_over_own_suppression(self):
        for kind, gate in (("inbound", "REPLY_REQUIRES_CONTEXT"), ("opt_out", "OPTED_OUT")):
            with self.subTest(kind=kind):
                action, claim = self.seed_claim({"profile": self.action["profile"] + "-" + kind})
                self.add_suppressions(action, "Handoff historical outreach; never repeat initial contact")
                other = self.connect()
                engagement.record_event(other, {"source": "heyreach", "source_id": "late-" + kind,
                    "profile": action["profile"], "kind": kind, "occurred_at": self.now.isoformat(),
                    "observed_at": self.now.isoformat(), "evidence": "mock-late-source", "raw": {"fixture": True}})
                with patch("outreach_provider._now", return_value=self.now.isoformat()):
                    self.assert_final_guard_cancelled(action, claim, gate)

    def test_final_guard_honors_late_explicit_suppression(self):
        action, claim = self.seed_claim()
        self.add_suppressions(action, "Existing user exclusion")
        self.assert_final_guard_cancelled(action, claim, "SUPPRESSED_CONTACT")

    def test_final_guard_uses_immutable_preflight_and_actual_history_age(self):
        action, claim = self.seed_claim()
        self.add_suppressions(action, "Handoff historical outreach; never repeat initial contact")
        expired = self.now + dt.timedelta(seconds=3601)
        with self.database:
            self.database.execute("UPDATE outreach_actions SET result=? WHERE action_id=?",
                (json.dumps({"preflight": {"retained": True, "checked_at": expired.isoformat()}}), action["action_id"]))
        with patch("outreach_provider._now", return_value=expired.isoformat()):
            with self.assertRaisesRegex(ValueError, "Final provider stop or freshness check failed"):
                self.provider.send_claimed(action["action_id"], self.config)
        row = self.database.execute("SELECT state,result FROM outreach_actions WHERE action_id=?", (action["action_id"],)).fetchone()
        self.assertEqual(row["state"], "CANCELLED_BEFORE_SEND")
        self.assertIn("DIRECT_HISTORY_MISSING_OR_STALE", json.loads(row["result"])["provider_guard"]["gates"])
        self.assertNotIn("provider_request", json.loads(row["result"]))
        self.assertEqual(dict(self.database.execute("SELECT * FROM outreach_action_claims WHERE action_id=?", (action["action_id"],)).fetchone()), claim)
        self.client._request.assert_not_called()

    def test_final_guard_missing_future_and_malformed_preflight_cannot_send(self):
        for proof in ({}, {"checked_at": (self.now + dt.timedelta(seconds=1)).isoformat()}, []):
            with self.subTest(proof=proof):
                action, claim = self.seed_claim(claim_changes={"preflight": json.dumps(proof)})
                with patch("outreach_provider._now", return_value=self.now.isoformat()):
                    self.assert_final_guard_cancelled(action, claim, "DIRECT_HISTORY_MISSING_OR_STALE")

    def test_final_guard_rechecks_content_freshness(self):
        action, claim = self.seed_claim({"verification": {"verified_at": (self.now - dt.timedelta(days=8)).isoformat()}})
        with patch("outreach_provider._now", return_value=self.now.isoformat()):
            self.assert_final_guard_cancelled(action, claim, "CONTENT_OR_IDENTITY_VERIFICATION_STALE")

    def test_final_guard_checks_age_after_obtaining_marker_transaction(self):
        action, claim = self.seed_claim()
        self.config["outreach_history_max_age_seconds"] = 10
        def locked_now():
            self.assertTrue(self.database.in_transaction)
            return (self.now + dt.timedelta(seconds=11)).isoformat()
        with patch("outreach_provider._now", side_effect=locked_now):
            self.assert_final_guard_cancelled(action, claim, "DIRECT_HISTORY_MISSING_OR_STALE")


if __name__ == "__main__":
    unittest.main()
