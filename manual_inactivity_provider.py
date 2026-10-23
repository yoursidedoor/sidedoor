import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

import engagement
import inactivity_closure
import orchestrator
import outreach_history_browser
import outreach_provider
import reporting

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from test_heyreach_schema import PROFILE, SENDER, message, room


NOW = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
CONFIG = {"outreach_inactivity_days": 27, "sender_id": 424242, "sender_profile": SENDER,
          "timezone": "America/Los_Angeles"}


def scenario(case):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state").mkdir()
        database = orchestrator.connect(root)
        inactivity_closure.initialize(database)
        with database:
            database.execute("INSERT INTO history VALUES (?,?,?,?,?)", (orchestrator.profile_key(PROFILE), "Synthetic", "Synthetic", PROFILE, "synthetic only"))
        if case == "pending_no_room":
            # HeyReach-sent single-contact campaign, invitation still pending, no chatroom.
            inactivity_closure.record_anchor(database, PROFILE, "2026-08-01T00:00:00+00:00",
                "HEYREACH_SEND_OBSERVED_UPPER_BOUND", "heyreach-campaign:7",
                {"heyreach": {"campaign_id": 7, "list_id": 8, "started_at": "2026-08-01T00:00:00Z"}}, NOW)
            database.commit()
        for kind in ("initial_sent", "accepted"):
            if kind == "accepted" and case in {"unknown_acceptance", "pending_no_room"}:
                continue
            if kind == "initial_sent" and case == "pending_no_room":
                continue
            engagement.record_event(database, {"source": "manual", "source_id": kind, "profile": PROFILE,
                "kind": kind, "occurred_at": "2026-08-01T00:00:00+00:00", "observed_at": "2026-08-01T00:00:00+00:00",
                "verified": True, "evidence": "mock verified event", "raw": {"synthetic": True}})
        messages = [message("ME", "Original", "2026-08-01T00:00:00.000Z")]
        if case == "recent_reply":
            messages.append(message("CORRESPONDENT", "Recent response", "2026-09-17T00:00:00.000Z"))
        if case == "own_followup":
            messages.append(message("ME", "Own followup", "2026-09-17T00:00:00.000Z"))
        thread = room(messages)
        thread["correspondentProfile"] = dict(thread["correspondentProfile"], linkedin_id="115000001")
        if case == "partial":
            thread["totalMessages"] += 1
        account = {"id": 424242, "profileUrl": SENDER, "isActive": True, "authIsValid": True}
        if case == "wrong_account":
            account["id"] = 1
        responses = {"get_linked_in_account_by_id": account, "get_conversations_v2": {"totalCount": 1, "items": [thread]},
                     "get_chatroom": thread, "get_campaigns_for_lead": {"totalCount": 0, "items": []},
                     "get_campaign": {"id": 7, "campaignAccountIds": [424242], "linkedInUserListId": 8,
                                      "startedAt": "2026-08-01T00:00:00Z"},
                     "get_leads_from_list": {"totalCount": 1, "items": [{"profileUrl": PROFILE}]},
                     "get_overall_stats": {"overallStats": {"connectionsSent": 1, "connectionsAccepted": 0}},
                     "get_lead": {"profileUrl": PROFILE, "linkedin_id": "115000001"}}
        if case == "pending_no_room":
            responses["get_conversations_v2"] = {"totalCount": 0, "items": []}
        client = Mock()
        client.call.side_effect = lambda operation, arguments: responses[operation]
        browser = Mock(compute_seconds=240)
        browser.review_inactivity.return_value = {"state": "UNKNOWN", "blocker": "MOCK_BROWSER_UNKNOWN", "events": []}
        with patch.object(outreach_provider, "_now", return_value=NOW.isoformat()), \
                patch.object(outreach_history_browser, "Collector", return_value=browser), \
                patch("socket.socket.connect", side_effect=AssertionError("NO NETWORK")):
            result = inactivity_closure.cycle(database, CONFIG, client, NOW)
            closed = engagement.closure_status(database, PROFILE)
            if case in {"inactive", "own_followup", "pending_no_room"}:
                assert closed and result["closed"] == 1, result
                reads = client.call.call_count
                inactivity_closure.cycle(database, CONFIG, client, NOW + dt.timedelta(days=1))
                assert client.call.call_count == reads
            elif case == "recent_reply":
                assert not closed and inactivity_closure.plan(database, PROFILE, NOW)["state"] == "ACTIVE", result
            elif case == "unknown_acceptance":
                # Hand-sent: the first clean read starts one more 27-day window; the second closes.
                assert not closed and result["items"][0]["state"] == "ACTIVE", result
                assert result["items"][0]["due_at"] == (NOW + dt.timedelta(days=27)).isoformat(), result
                with patch.object(outreach_provider, "_now", return_value=(NOW + dt.timedelta(days=27)).isoformat()):
                    later = inactivity_closure.cycle(database, CONFIG, client, NOW + dt.timedelta(days=27))
                assert later["closed"] == 1, later
                closed = engagement.closure_status(database, PROFILE)
            else:
                assert not closed and result["items"][0]["state"] == "RETRY_WAIT", result
            assert not client._request.called
            payload = reporting.snapshot(database, CONFIG, NOW.isoformat())
            payload["day"] = "2026-09-17"
            assert payload["inactivity_closure"]["days"] == 27
            assert "Inactivity closure: 27 days" in reporting.telegram_body(payload)
        database.close()
        return {"case": case, "state": "PASS", "closed": bool(closed), "live_calls": 0}


if __name__ == "__main__":
    print(json.dumps({"evidence": "MOCKED_PROVIDER_IO_REAL_DECODER_AND_CLOSURE",
        "cases": [scenario(case) for case in ("inactive", "own_followup", "recent_reply", "partial", "wrong_account", "unknown_acceptance", "pending_no_room")]}, indent=2))
