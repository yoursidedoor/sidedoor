"""Raw HeyReach observations and a single-use, claimed message transport.

The parent saves collect() envelopes before AI inspection/reconciliation and
supplies the local-records history proof separately (owner, 2026-09-29). No observation here is
history clearance, sender readiness, message delivery, or permission to send.

The injected orchestrator.HeyReach owns authentication and its 25-second socket
timeout / 8 MB response bound. Collection schedules at most 21 ordinary reads,
24 positive-history reads or 29 preparation reads, with five pages per listing
and ten chatrooms; a 120-second scheduling deadline prevents
starting further reads (an in-flight read retains the client's timeout).

send_claimed requires the parent's committed REQUEST_STARTED transition and
outreach_allowed_purposes explicitly containing INITIAL, FIRST_MESSAGE or REPLY. Its
operation is fixed; arguments come only from the immutable claim payload. It
commits result.provider_request before transport, consuming that attempt even
on failure. PREPARE_CAMPAIGN delegates to outreach_campaign's existing claimed
preparer; it never launches a campaign. The parent preserves every request marker
and reconciles uncertain effects without retry, except a REPLY: its provider errors
are retried by reply_execution.Adapter.retry_reply, at most 3 attempts in all, each
after a HeyReach thread read shows the earlier attempt did not go through (owner,
2026-09-30).
"""

import datetime as dt
import hashlib
import json
import time

import engagement
import linkedin_port
import outreach
from heyreach_schema import _profile
from orchestrator import LeadNotFoundError, known_provider_lead, name_key, profile_key


READ_TOOLS = frozenset({"get_linked_in_account_by_id", "get_conversations_v2",
                        "get_chatroom", "get_campaigns_for_lead", "get_lead",
                        "get_lists_for_lead", "get_my_network_for_sender", "get_all_campaigns",
                        "get_campaign", "get_campaign_sequence", "get_leads_from_list"})


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _snapshot(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _binding(raw, sender_id, sender_profile, profile, conversation_id=None, lead_id=None):
    if not isinstance(raw, dict):
        return False
    account = raw.get("linkedInAccount")
    correspondent = raw.get("correspondentProfile")
    if (type(raw.get("linkedInAccountId")) is not int
            or raw["linkedInAccountId"] != sender_id
            or not isinstance(account, dict) or type(account.get("id")) is not int
            or account["id"] != sender_id or not isinstance(correspondent, dict)
            or not isinstance(raw.get("id"), str) or not raw["id"]
            or raw.get("groupChat") is not False
            or conversation_id is not None and raw["id"] != conversation_id):
        return False
    try:
        return (_profile(account.get("profileUrl")) == sender_profile
                and _profile(correspondent.get("profileUrl")) == profile
                and (lead_id is None or correspondent.get("linkedin_id") == lead_id))
    except (TypeError, ValueError):
        return False


class Provider:
    def __init__(self, database, config, client):
        self.database = database
        self.config = _snapshot(config)
        self.client = client

    def collect(self, action, phase, claim=None, *, new_contact_history=False):
        """Return JSON-compatible raw observations, never a semantic decision."""
        if phase not in {"INSPECTION", "RECONCILIATION"}:
            raise ValueError("Unsupported observation phase")
        action = _snapshot(action)
        sender_id = self.config.get("sender_id")
        if type(sender_id) is not int or sender_id <= 0:
            raise ValueError("Positive configured sender ID required")
        if action.get("sender_id", sender_id) != sender_id:
            raise ValueError("Action account mismatch")
        profile = _profile(action["profile"])
        sender_profile = _profile(self.config.get("sender_profile"))
        if claim is not None:
            claim = dict(claim)
            if (claim.get("sender_id") != sender_id
                    or claim.get("action_id") != action.get("action_id")
                    or json.loads(claim["payload"]) != action
                    or hashlib.sha256(claim["payload"].encode()).hexdigest() != claim.get("payload_hash")
                    or claim.get("identity") != profile_key(profile)):
                raise ValueError("Observation claim identity or payload mismatch")
        envelope = {
            "provider": "heyreach", "phase": phase, "action_id": action.get("action_id"),
            "sender_id": sender_id, "sender_profile": sender_profile,
            "profile": profile, "started_at": _now(),
            "claim_payload_hash": claim["payload_hash"] if claim is not None else None,
            "claim": {key: claim[key] for key in ("action_id", "sender_id", "identity", "payload_hash", "claimed_at")
                      if key in claim} if claim is not None else None,
            "observations": [], "coverage": {}, "identity_issues": [],
            "blockers": [], "selected_conversation_id": None,
            "direct_messages_complete": False, "sent_invitations_complete": False,
            "full_linkedin_history_clearance": False,
            "direct_history_status": "UNKNOWN", "invitation_history_status": "UNKNOWN",
            "limits": {"pages_per_listing": 5, "page_size": 100, "chatrooms": 10,
                       "reads": 29 if action.get("purpose") == "PREPARE_CAMPAIGN" else 24 if new_contact_history else 21,
                       "scheduling_seconds": 120, "socket_timeout_seconds": 25},
        }
        # `reads` is the envelope's canonical accessor for the read log and callers/tests use it
        # (test_raw_envelope_aliases_exact_arguments_and_timestamps). It stays. What was wasteful
        # was carrying BOTH names into the model-facing packet, which the trim functions in
        # outreach_agent / followup_execution / reply_execution now strip instead of re-applying.
        envelope["reads"] = envelope["observations"]
        deadline = time.monotonic() + 120
        read_count = 0

        def read(operation, arguments):
            nonlocal read_count
            if operation not in READ_TOOLS:
                raise ValueError("Unsupported provider read")
            observation = {"operation": operation, "tool": operation,
                           "source": "heyreach:" + operation, "arguments": _snapshot(arguments),
                           "started_at": _now(), "status": "UNKNOWN",
                           "authenticated": False, "raw": None}
            envelope["observations"].append(observation)
            if read_count >= envelope["limits"]["reads"] or time.monotonic() >= deadline:
                observation.update(reason="READ_BUDGET_EXHAUSTED", attempted=False)
            else:
                read_count += 1
                observation["attempted"] = True
                started = time.monotonic()
                try:
                    observation["raw"] = _snapshot(self.client.call(operation, _snapshot(arguments)))
                    observation.update(status="OBSERVED", authenticated=True)
                except LeadNotFoundError:
                    observation.update(status="NOT_PRESENT_WITHIN_PROVIDER" if operation == "get_lead" else "UNKNOWN",
                                       reason="PROVIDER_LEAD_NOT_FOUND", authenticated=True,
                                       error_type="LeadNotFoundError")
                except Exception as error:
                    observation.update(reason="PROVIDER_READ_FAILED", error_type=type(error).__name__)
                observation["elapsed_seconds"] = round(time.monotonic() - started, 6)
            observation["finished_at"] = _now()
            observation["observed_at"] = observation["finished_at"]
            observation["result"] = observation["raw"]
            return observation

        def listing(operation, arguments):
            prior_pages = envelope["coverage"].get(operation, {}).get("pages", 0)
            coverage = {"status": "UNKNOWN", "scope": "HEYREACH_LISTING_ONLY",
                        "pages": prior_pages, "items_observed": 0, "truncated": False}
            envelope["coverage"][operation] = coverage
            records = []
            expected_total = None
            seen = set()
            for unused in range(5 - prior_pages):
                observation = read(operation, dict(arguments, offset=len(records), limit=100))
                if observation["status"] != "OBSERVED":
                    coverage["reason"] = observation["reason"]
                    return records
                coverage["pages"] += 1
                raw = observation["raw"]
                if (not isinstance(raw, dict) or type(raw.get("totalCount")) is not int
                        or raw["totalCount"] < 0 or not isinstance(raw.get("items"), list)):
                    coverage["reason"] = "UNSUPPORTED_PAGE_SHAPE"
                    return records
                items = raw["items"]
                total = raw["totalCount"]
                if expected_total is not None and total != expected_total:
                    coverage["reason"] = "LISTING_CHANGED_DURING_SCAN"
                    return records
                expected_total = total
                if len(items) > 100 or len(records) + len(items) > total or not items and len(records) < total:
                    coverage.update(reason="INCOMPLETE_OR_INCONSISTENT_PAGE", truncated=True)
                    return records
                for item in items:
                    signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if signature in seen:
                        coverage["reason"] = "REPEATED_LISTING_ITEM"
                        return records
                    seen.add(signature)
                records.extend(items)
                coverage["items_observed"] = len(records)
                if len(records) == total:
                    coverage["status"] = "BOUNDED_SCAN_COMPLETE"
                    return records
            coverage.update(reason="PAGE_LIMIT", truncated=True)
            return records

        account = read("get_linked_in_account_by_id", {"accountId": sender_id})
        raw_account = account["raw"]
        account_matches = (account["status"] == "OBSERVED" and isinstance(raw_account, dict)
                           and type(raw_account.get("id")) is int and raw_account["id"] == sender_id)
        try:
            account_matches = account_matches and _profile(raw_account.get("profileUrl")) == sender_profile
        except (TypeError, ValueError):
            account_matches = False
        envelope["coverage"]["account"] = {"status": "OBSERVED" if account_matches else "UNKNOWN"}
        account["identity_matches"] = account_matches
        if not account_matches:
            envelope["identity_issues"].append("ACCOUNT_UNVERIFIED_OR_MISMATCHED")
        conversations = listing("get_conversations_v2", {
            "linkedInAccountIds": [sender_id], "leadProfileUrl": action["profile"]})
        lead_id = None
        if (not any(_binding(item, sender_id, sender_profile, profile) for item in conversations) and account_matches
                and envelope["coverage"]["get_conversations_v2"]["status"] == "BOUNDED_SCAN_COMPLETE"):
            if conversations:
                envelope["identity_issues"].append("PROFILE_LOOKUP_RETURNED_NONMATCHING_CONVERSATIONS")
            try:
                lead_id = known_provider_lead(self.database, action["profile"], sender_id)
            except Exception as error:
                lead_id = None
                envelope["blockers"].append("PROVIDER_LEAD_ALIAS_LOOKUP_FAILED:" + type(error).__name__)
            if not conversations and not lead_id:
                lead = read("get_lead", {"profileUrl": action["profile"]})
                raw_lead = lead["raw"]
                lead_matches = False
                try:
                    lead_matches = (lead["status"] == "OBSERVED" and isinstance(raw_lead, dict)
                                    and _profile(raw_lead.get("profileUrl")) == profile
                                    and isinstance(raw_lead.get("linkedin_id"), str)
                                    and 0 < len(raw_lead["linkedin_id"]) <= 2048
                                    and all(character.isprintable() and not character.isspace()
                                            for character in raw_lead["linkedin_id"]))
                except (TypeError, ValueError):
                    pass
                lead["identity_matches"] = lead_matches
                envelope["coverage"]["get_lead"] = {
                    "status": "OBSERVED" if lead_matches else
                    "NOT_PRESENT_WITHIN_PROVIDER" if new_contact_history and lead["status"] == "NOT_PRESENT_WITHIN_PROVIDER"
                    else "UNKNOWN", "scope": "HEYREACH_LEAD_ONLY"}
                if lead_matches:
                    lead_id = raw_lead["linkedin_id"]
                else:
                    envelope["coverage"]["get_lead"]["reason"] = lead.get("reason", "LEAD_UNVERIFIED_OR_MISMATCHED")
                    if lead["status"] == "OBSERVED" and (not new_contact_history or
                            isinstance(raw_lead, dict) and raw_lead.get("profileUrl")):
                        envelope["identity_issues"].append("LEAD_UNVERIFIED_OR_MISMATCHED")
            if lead_id:
                conversations = listing("get_conversations_v2", {
                    "linkedInAccountIds": [sender_id], "leadLinkedInId": lead_id})
                envelope["coverage"]["get_conversations_v2"]["provider_lead_alias_used"] = True
        rooms = []
        for conversation in conversations:
            if not account_matches or not _binding(conversation, sender_id, sender_profile, profile, lead_id=lead_id):
                envelope["identity_issues"].append("CONVERSATION_UNVERIFIED_OR_MISMATCHED")
                continue
            rooms.append(conversation)
        requested_thread = action.get("conversation_id")
        candidates = {room["id"] for room in rooms
                      if requested_thread is None or room["id"] == requested_thread}
        if (len(candidates) == 1 and not envelope["identity_issues"]
                and envelope["coverage"]["get_conversations_v2"]["status"] == "BOUNDED_SCAN_COMPLETE"):
            envelope["selected_conversation_id"] = next(iter(candidates))
        else:
            envelope["blockers"].append("EXACT_CONVERSATION_ABSENT_OR_AMBIGUOUS")
        room_coverage = {"status": "UNKNOWN", "matching_conversations": len(rooms),
                         "rooms_observed": 0, "truncated": len(rooms) > 10,
                         "scope": "HEYREACH_CHATROOMS_ONLY"}
        envelope["coverage"]["chatrooms"] = room_coverage
        rooms_complete = True
        for conversation in rooms[:10]:
            observation = read("get_chatroom", {"accountId": sender_id, "conversationId": conversation["id"]})
            raw = observation["raw"]
            matched = observation["status"] == "OBSERVED" and _binding(
                raw, sender_id, sender_profile, profile, conversation["id"], lead_id=lead_id)
            observation["identity_matches"] = matched
            if not matched:
                rooms_complete = False
                envelope["identity_issues"].append("CHATROOM_UNVERIFIED_OR_MISMATCHED")
                continue
            room_coverage["rooms_observed"] += 1
            complete = (isinstance(raw.get("messages"), list) and type(raw.get("totalMessages")) is int
                        and raw["totalMessages"] >= 0 and len(raw["messages"]) == raw["totalMessages"])
            observation["message_coverage"] = "OBSERVED_COUNT_MATCHES" if complete else "UNKNOWN"
            rooms_complete = rooms_complete and complete
            room_coverage["truncated"] = room_coverage["truncated"] or not complete
        listing("get_campaigns_for_lead", {"linkedinId": lead_id} if lead_id else {"profileUrl": action["profile"]})
        if action.get("purpose") == "PREPARE_CAMPAIGN":
            if type(action.get("list_id")) is int and action["list_id"] > 0:
                read("get_leads_from_list", {"listId": action["list_id"], "offset": 0, "limit": 100})
            reference_id = action.get("sequence_reference_campaign_id")
            if type(reference_id) is int and reference_id > 0:
                read("get_campaign_sequence", {"campaignId": reference_id})
            if phase == "RECONCILIATION" and action.get("campaign_name"):
                campaigns = listing("get_all_campaigns", {"keyword": action["campaign_name"]})
                matches = [record for record in campaigns if isinstance(record, dict)
                           and record.get("name") == action["campaign_name"]]
                if len(matches) == 1 and type(matches[0].get("id")) is int and matches[0]["id"] > 0:
                    read("get_campaign", {"campaignId": matches[0]["id"]})
                    read("get_campaign_sequence", {"campaignId": matches[0]["id"]})
        if new_contact_history and phase == "INSPECTION":
            envelope["positive_history_only"] = True
            supplemental = [("get_lists_for_lead", {"profileUrl": action["profile"], "offset": 0, "limit": 100}),
                            ("get_my_network_for_sender", {"senderId": sender_id, "pageNumber": 0, "pageSize": 100})]
            if not any(item["operation"] == "get_lead" for item in envelope["observations"]):
                supplemental.insert(0, ("get_lead", {"profileUrl": action["profile"]}))
            for operation, arguments in supplemental:
                observation = read(operation, arguments)
                observation["scope"] = "POSITIVE_HISTORY_ONLY_NO_ABSENCE_CLEARANCE"
                observation["recipient_binding_required"] = True
                if operation == "get_lead" and observation["status"] == "OBSERVED" and observation["raw"]:
                    try:
                        matched = _profile(observation["raw"].get("profileUrl")) == profile
                    except (AttributeError, TypeError, ValueError):
                        matched = False
                    observation["identity_matches"] = matched
                    if not matched:
                        envelope["identity_issues"].append("LEAD_UNVERIFIED_OR_MISMATCHED")
            envelope["history_capability"] = {
                "all_time_sent_invitations": "UNAVAILABLE",
                "all_time_linkedin_messages": "NOT_ESTABLISHED_BY_HEYREACH",
                "pending_invitations_complete": "NOT_ESTABLISHED_BY_HEYREACH",
                "network_scope": "FIRST_100_CONNECTIONS_ONLY_NOT_INVITATION_HISTORY"}
        if (rooms_complete and not room_coverage["truncated"] and not envelope["identity_issues"]
                and envelope["coverage"]["get_conversations_v2"]["status"] == "BOUNDED_SCAN_COMPLETE"):
            room_coverage["status"] = "BOUNDED_SCAN_COMPLETE"
        if envelope["identity_issues"]:
            envelope["coverage"]["get_conversations_v2"]["status"] = "UNKNOWN"
            envelope["selected_conversation_id"] = None
        envelope["identity_issues"] = sorted(set(envelope["identity_issues"]))
        envelope["identity_verified"] = account_matches and not envelope["identity_issues"]
        if new_contact_history:
            transport = callable(getattr(self.client, "_request", None))
            invitation = transport and callable(getattr(self.client, "start_claimed_campaign", None))
            envelope["operation_capabilities"] = {
                "EXISTING_THREAD": {"supported": transport, "operations": ["send_message"],
                                    "requires_observed_conversation": True},
                "INVITATION": {"supported": invitation,
                               "operations": ["create_empty_list", "add_leads_to_list_v2", "create_campaign", "start_campaign"],
                               "execution_owner": "existing_claimed_campaign_dispatcher"},
                "permission": "EXISTING_ACCOUNT_PROVIDER_ENFORCED_AT_REQUEST",
                "remaining_quota": "UNKNOWN_PROVIDER_ENFORCED",
                "source": "enabled_HeyReach_tool_contract_and_installed_claimed_transport"}
        envelope["finished_at"] = _now()
        envelope["observed_at"] = envelope["finished_at"]
        envelope["blockers"].extend(envelope["identity_issues"])
        envelope["blockers"].extend(operation + ":" + coverage.get("reason", "COVERAGE_UNKNOWN")
                                    for operation, coverage in envelope["coverage"].items()
                                    if coverage["status"] == "UNKNOWN")
        envelope["blockers"] = sorted(set(envelope["blockers"]))
        return envelope

    def send_claimed(self, action_id, config):
        """Consume one committed claim; errors or interruptions must not retry."""
        if self.database.in_transaction:
            raise ValueError("Parent must commit REQUEST_STARTED before transport")
        reply_check = None
        preview = self.database.execute("""SELECT claim.payload,claim.payload_hash,claim.sender_id,action.result
            FROM outreach_action_claims claim JOIN outreach_actions action USING(action_id)
            WHERE claim.action_id=? AND action.state='REQUEST_STARTED'
            AND action.payload=claim.payload AND action.payload_hash=claim.payload_hash
            AND action.identity=claim.identity""", (action_id,)).fetchone()
        if preview and json.loads(preview[0]).get("purpose") == "PREPARE_CAMPAIGN":
            import outreach_campaign
            if config.get("sender_id") != self.config.get("sender_id"):
                raise ValueError("CAMPAIGN_CONFIGURED_SENDER_CHANGED")
            tables = {row[0] for row in self.database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"outreach_campaign_preparations", "outreach_campaign_intents"} <= tables:
                raise ValueError("CLAIMED_CAMPAIGN_PREPARATION_REQUIRED")
            return outreach_campaign.create_claimed(self.database, config, self.client, action_id, _now())
        # A REPLY no longer re-reads its thread here (owner, 2026-09-30: "for 'right before sending'
        # remove the fresh thread read. we dont need to reread it. and the other checks"). Only a
        # FOLLOWUP keeps the final thread read below.
        if preview and json.loads(preview[0]).get("purpose") in {"REPLY", "FOLLOWUP"}:
            pending_result = json.loads(preview[3]) if preview[3] is not None else {}
            if (hashlib.sha256(preview[0].encode()).hexdigest() != preview[1]
                    or preview[2] != config.get("sender_id") or preview[2] != self.config.get("sender_id")
                    or not isinstance(pending_result, dict) or "provider_request" in pending_result):
                raise ValueError("Reply claim changed or request already consumed")
        if preview and json.loads(preview[0]).get("purpose") == "FOLLOWUP":
            reply_action = json.loads(preview[0])
            arguments = {"accountId": preview[2], "conversationId": reply_action["conversation_id"]}
            reply_check = {"operation": "get_chatroom", "arguments": arguments, "started_at": _now(),
                           "payload_hash": preview[1], "authenticated": False}
            try:
                import followup_execution as thread_owner
                room = _snapshot(linkedin_port.HeyReachPort(self.client, preview[2]).messages(
                    reply_action["conversation_id"]).raw)
                reply_check.update(raw=room, authenticated=True, observed_at=_now())
                reply_check["gates"] = thread_owner.thread_gates(self.database, config, reply_action, room, _now())
            except Exception as error:
                reply_check.update(error_type=type(error).__name__, observed_at=_now())
        self.database.execute("BEGIN IMMEDIATE")
        try:
            row = self.database.execute(
                "SELECT payload,payload_hash,identity,state,result FROM outreach_actions WHERE action_id=?",
                (action_id,)).fetchone()
            claim = self.database.execute(
                "SELECT payload,payload_hash,identity,sender_id,preflight FROM outreach_action_claims WHERE action_id=?",
                (action_id,)).fetchone()
            if not row or not claim or row[3] != "REQUEST_STARTED":
                raise ValueError("Committed REQUEST_STARTED and durable claim required")
            if (tuple(row[:3]) != tuple(claim[:3])
                    or hashlib.sha256(claim[0].encode()).hexdigest() != claim[1]
                    or type(claim[3]) is not int or claim[3] <= 0
                    or config.get("sender_id") != claim[3] or self.config.get("sender_id") != claim[3]
                    or _profile(config.get("sender_profile")) != _profile(self.config.get("sender_profile"))):
                raise ValueError("Action no longer matches immutable claim/account/hash")
            action = json.loads(claim[0])
            allowed = config.get("outreach_allowed_purposes")
            if (config.get("outreach_actions_enabled") is not True or config.get("outreach_mode") != "guarded"
                    or not isinstance(allowed, list) or action.get("purpose") not in allowed
                    or action.get("purpose") not in {"INITIAL", "FIRST_MESSAGE", "REPLY", "FOLLOWUP"}
                    or action.get("action_id") != action_id or action.get("provider") != "heyreach"
                    or action.get("channel") != "linkedin" or profile_key(_profile(action.get("profile"))) != claim[2]
                    or action.get("sender_id", claim[3]) != claim[3]
                    or action.get("operation", "send_message") != "send_message"):
                raise ValueError("Claimed send is outside explicit enabled scope")
            arguments = {"conversationId": action.get("conversation_id"), "linkedInAccountId": claim[3],
                         "message": action.get("message"), "subject": action.get("subject", "")}
            if (any(not isinstance(arguments[field], str) or not arguments[field].strip()
                    for field in ("conversationId", "message")) or not isinstance(arguments["subject"], str)
                    or "arguments" in action and action["arguments"] != arguments):
                raise ValueError("Claimed send arguments missing or inconsistent")
            result = json.loads(row[4]) if row[4] is not None else {}
            if not isinstance(result, dict) or "provider_request" in result:
                raise ValueError("Request already consumed or unrecognized durable result")
            checked_at = _now()
            now = engagement.timestamp(checked_at)
            proof = json.loads(claim[4])
            gates = []
            reply = action["purpose"] == "REPLY"
            if reply:
                # Only the stops the owner kept (2026-09-30): opt-out or negative reply, closed
                # recipient, excluded name, one reply per inbound message.
                import reply_execution
                gates.extend(reply_execution.stop_gates(self.database, config, action,
                                                        proof if isinstance(proof, dict) else {}))
            else:
                import inactivity_closure
                gates.extend(inactivity_closure.send_gates(self.database, config, action["profile"], now))
            import outreach_history
            # INITIAL/PREPARE_CAMPAIGN: checked once before the draft (owner, 2026-10-01).
            if action["purpose"] not in {"REPLY", "FOLLOWUP"} | outreach_history.PRE_DRAFT_CHECKED:
                if outreach_history.blocking_positive(self.database, action["profile"], action["purpose"]):
                    gates.append("KNOWN_PRIOR_OUTREACH_OR_STOP")
            if action["purpose"] == "FOLLOWUP":
                if reply_check is None or reply_check["payload_hash"] != claim[1]:
                    gates.append(action["purpose"] + "_FINAL_THREAD_CHECK_MISSING")
                else:
                    result["provider_" + action["purpose"].lower() + "_check"] = reply_check
                    if "error_type" in reply_check:
                        self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                              (json.dumps(result, sort_keys=True), action_id))
                        self.database.commit()
                        raise ValueError("Final reply thread read unavailable; reconcile without retry")
                    gates.extend(reply_check["gates"])
            if not reply and (not isinstance(proof, dict) or not outreach._fresh(proof.get("checked_at"), now, config.get("outreach_history_max_age_seconds", 3600))):
                gates.append("DIRECT_HISTORY_MISSING_OR_STALE")
            if not reply and not outreach._fresh(action.get("verification", {}).get("verified_at"), now, config.get("outreach_content_max_age_seconds", 604800)):
                gates.append("CONTENT_OR_IDENTITY_VERIFICATION_STALE")
            stopped = engagement.stop_state(self.database, action["profile"])
            if stopped and not reply:
                gates.append(stopped)
            if action["purpose"] == "FOLLOWUP":
                gates.extend(outreach.action_gates(self.database, config, action, proof, now))
            own_suppressions = {claim[2], "name:" + name_key(action["recipient"])}
            identities = sorted(set(engagement.identity_keys(self.database, action["profile"])) | own_suppressions)
            if self.database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressions'").fetchone():
                placeholders = ",".join("?" for unused in identities)
                for suppression in self.database.execute("SELECT identity,reason FROM suppressions WHERE identity IN (" + placeholders + ")", identities):
                    if reply:
                        continue          # reply_execution.stop_gates checked suppressions above
                    if action["purpose"] == "FOLLOWUP" and outreach._followup_suppression_allowed(
                            self.database, action["profile"], suppression[1]):
                        continue
                    if (suppression[0] not in own_suppressions
                            or suppression[1] != "Handoff historical outreach; never repeat initial contact"):
                        gates.append("SUPPRESSED_CONTACT")
            if gates:
                result["provider_guard"] = {"checked_at": checked_at, "gates": sorted(set(gates))}
                self.database.execute("UPDATE outreach_actions SET state='CANCELLED_BEFORE_SEND',result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                      (json.dumps(result, sort_keys=True), action_id))
                self.database.commit()
                raise ValueError("Final provider stop or freshness check failed")
            request = {"operation": "send_message", "arguments": arguments, "started_at": checked_at,
                       "payload_hash": claim[1], "status": "REQUEST_STARTED"}
            result["provider_request"] = request
            self.database.execute("UPDATE outreach_actions SET result=? WHERE action_id=? AND state='REQUEST_STARTED'",
                                  (json.dumps(result, sort_keys=True), action_id))
            self.database.commit()
        except BaseException:
            self.database.rollback()
            raise
        sent = _snapshot(request["arguments"])
        raw = linkedin_port.HeyReachPort(self.client, sent["linkedInAccountId"]).send_message(
            sent["conversationId"], sent["message"], subject=sent["subject"]).raw
        return {**request, "finished_at": _now(), "status": "ACKNOWLEDGED_UNCONFIRMED", "raw": _snapshot(raw)}


def campaign_arguments(action, sender_id):
    if (type(action.get("list_id")) is not int or action["list_id"] <= 0
            or not isinstance(action.get("campaign_name"), str) or not 1 <= len(action["campaign_name"]) <= 50
            or not isinstance(action.get("sequence"), dict)):
        raise ValueError("CLAIMED_CAMPAIGN_ARGUMENTS_INVALID")
    return {"linkedInAccountIds": [sender_id], "linkedInUserListId": action["list_id"],
            "name": action["campaign_name"], "sequenceJson": json.dumps(action["sequence"]),
            "excludeContactedFromOtherCampaigns": True, "excludeContactedFromSenderInOtherCampaign": True,
            "excludeFromCompanyBlacklist": True, "excludeFromLeadBlacklist": True,
            "excludeHasOtherAccConversations": True}
