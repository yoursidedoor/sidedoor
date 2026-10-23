import copy
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
from unittest import mock

import outreach_history_browser as browser


PROFILE = "https://www.linkedin.com/in/inactivity-fixture/"
SENDER = "https://www.linkedin.com/in/sender-fixture/"


def fixture(request, outcome="INACTIVE", acceptance="PENDING", event_kind=None, date_text=None):
    evidence = [{"kind": "ACCOUNT", "tool_call_id": None, "quote": "Account " + SENDER},
                {"kind": "RECIPIENT", "tool_call_id": None, "quote": "Recipient " + PROFILE}]
    evidence.extend({"kind": kind, "tool_call_id": None,
                     "quote": kind + " target " + PROFILE + " search complete through present; no other messages"}
                    for kind in browser.KINDS[2:6])
    if acceptance == "PENDING":
        evidence.append({"kind": "PENDING_INVITATION", "tool_call_id": None,
                         "quote": "Sent pending original invitation to " + PROFILE})
    events = []
    if event_kind:
        evidence.append({"kind": "INBOUND" if event_kind == "inbound" else "ACCEPTANCE",
                         "tool_call_id": None,
                         "quote": PROFILE + " " + event_kind + " Thanks, fixture text, at " + date_text})
        events.append({"kind": event_kind, "occurred_at": browser._quoted_time(date_text).isoformat(),
                       "date_text": date_text, "evidence_indices": [len(evidence) - 1], "timing_basis": "EXACT_TIME"})
    result = {"version": browser.INACTIVITY_VERSION, "source_sha256": browser.digest(request),
              "sender_profile": SENDER, "recipient_profile": PROFILE, "outcome": outcome,
              "reason": "Synthetic offline review", "next_action": "Return evidence to parent",
              "coverage": {kind: True for kind in browser.KINDS[2:6]}, "evidence": evidence,
              "window_start": request["window_start"], "window_end": request["window_end"],
              "acceptance": acceptance, "events": events}
    trace = browser.encoded({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "fixture-observation",
        "server": "cua_repl", "tool": "js", "arguments": {"code": "await tab.getState()"}, "status": "completed",
        "result": {"content": [{"type": "text", "text": "\n".join(entry["quote"] for entry in evidence)}]}}}).encode()
    return result, trace


def main():
    current = dt.datetime.now(dt.timezone.utc)
    request = {"profile": PROFILE, "profile_aliases": [browser.profile_key(PROFILE)], "sender_id": 123,
               "sender_profile": SENDER, "cutoff_at": (current - dt.timedelta(days=27)).isoformat(),
               "known_accepted": False, "read_only": True}
    normalized = dict(request, aliases=request["profile_aliases"], window_start=request["cutoff_at"],
                      window_end=current.isoformat(), max_reads=32)
    checks = []

    def review(result, trace):
        return browser.normalize_inactivity(normalized, result, trace, current.isoformat(), "browser-history:fixture")

    result, trace = fixture(normalized)
    checked = review(result, trace)
    assert checked["state"] == "COMPLETE" and checked["events"] == []
    assert checked["raw"]["observed_text"] and checked["raw"]["source_excerpts"]
    checks.append("pending plus complete folders")
    for folder in browser.KINDS[2:6]:
        partial = copy.deepcopy(result)
        partial["coverage"][folder] = False
        assert review(partial, trace)["state"] == "UNKNOWN"
    checks.append("each missing folder holds")
    unknown = copy.deepcopy(result)
    unknown["acceptance"] = "UNKNOWN"
    assert review(unknown, trace)["state"] == "UNKNOWN"
    checks.append("unknown acceptance holds")
    recent = (current - dt.timedelta(days=1)).isoformat()
    engaged, engaged_trace = fixture(normalized, "ENGAGED", "PENDING", "inbound", recent)
    confirmed = review(engaged, engaged_trace)
    assert confirmed["state"] == "COMPLETE" and confirmed["events"][0]["verified"] is True
    assert confirmed["events"][0]["kind"] == "inbound"
    checks.append("dated inbound produces verified event")
    accepted, accepted_trace = fixture(normalized, "ENGAGED", "ACCEPTED", "accepted", recent)
    assert review(accepted, accepted_trace)["state"] == "COMPLETE"
    checks.append("dated recent acceptance")
    older = (current - dt.timedelta(days=40)).isoformat()
    accepted, accepted_trace = fixture(normalized, "INACTIVE", "ACCEPTED", "accepted", older)
    assert review(accepted, accepted_trace)["state"] == "COMPLETE"
    checks.append("dated older acceptance")
    normalized["known_accepted"] = True
    known, known_trace = fixture(normalized, acceptance="KNOWN_ACCEPTED")
    assert review(known, known_trace)["state"] == "COMPLETE"
    conflicting, conflicting_trace = fixture(normalized)
    assert review(conflicting, conflicting_trace)["state"] == "UNKNOWN"
    checks.append("known accepted and pending conflict")
    normalized["known_accepted"] = False
    mutations = []
    for label in ("unobserved quotation", "unquoted timestamp", "future date", "ambiguous date", "wrong account", "wrong window"):
        altered = copy.deepcopy(engaged)
        altered_trace = engaged_trace
        if label == "unobserved quotation":
            altered["evidence"][-1]["quote"] = "fabricated UI quotation"
        elif label == "unquoted timestamp":
            altered["events"][0]["occurred_at"] = older
        elif label == "future date":
            altered, altered_trace = fixture(normalized, "ENGAGED", "PENDING", "inbound", (current + dt.timedelta(days=1)).isoformat())
        elif label == "ambiguous date":
            altered["events"][0]["date_text"] = "yesterday"
            altered["evidence"][-1]["quote"] += " yesterday"
            altered_trace = engaged_trace.replace(recent.encode(), (recent + " yesterday").encode())
        elif label == "wrong account":
            altered["sender_profile"] = PROFILE
        else:
            altered["window_start"] = older
        try:
            review(altered, altered_trace)
        except ValueError:
            mutations.append(label)
        else:
            raise AssertionError(label)
    checks.extend(mutations)
    assert browser._quoted_time("September 17, 2026 at 3:45 PM PDT").isoformat() == "2026-09-17T22:45:00+00:00"
    checks.append("explicit readable timezone date")
    normalized["requires_outreach_anchor"] = True
    missing, missing_trace = fixture(normalized)
    assert review(missing, missing_trace)["state"] == "UNKNOWN"
    anchored = copy.deepcopy(missing)
    day = (current - dt.timedelta(days=45)).date().isoformat()
    quote = "Original outbound invitation sent to " + PROFILE + " on " + day
    anchored["evidence"].append({"kind": "OUTBOUND_INITIAL", "tool_call_id": None, "quote": quote})
    anchored["events"] = [{"kind": "initial_sent", "occurred_at": browser._quoted_day_upper_bound(day).isoformat(),
                           "date_text": day, "timing_basis": "EXACT_DAY_UPPER_BOUND",
                           "evidence_indices": [len(anchored["evidence"]) - 1]}]
    recorded = json.loads(missing_trace)
    recorded["item"]["result"]["content"][0]["text"] += "\n" + quote
    anchor_trace = browser.encoded(recorded).encode()
    first = review(anchored, anchor_trace)
    later = browser.normalize_inactivity(normalized, anchored, anchor_trace,
                                        (current + dt.timedelta(seconds=30)).isoformat(), "browser-history:later")
    assert first["state"] == later["state"] == "COMPLETE"
    assert first["events"][0]["source_id"] == later["events"][0]["source_id"]
    assert first["events"][0]["occurred_at"] == later["events"][0]["occurred_at"]
    assert first["events"][0]["occurred_at_basis"] == "EXACT_DAY_UPPER_BOUND"
    checks.append("required initial anchor and stable conservative exact-day bound")
    for ambiguous in ("4 months ago", "September 17", "yesterday"):
        try:
            browser._quoted_day_upper_bound(ambiguous)
        except ValueError:
            pass
        else:
            raise AssertionError(ambiguous)
    checks.append("relative and yearless dates rejected by exact-day parser")
    relative = copy.deepcopy(anchored)
    relative["evidence"][-1]["quote"] = "Original outbound invitation to " + PROFILE + " Sent4monthsago"
    relative["events"][0].update(date_text="Sent4monthsago", timing_basis="RELATIVE_AGE_UPPER_BOUND",
                                  occurred_at=(current - dt.timedelta(days=84)).isoformat())
    relative_recorded = json.loads(anchor_trace)
    relative_recorded["item"]["result"]["content"][0]["text"] = "\n".join(entry["quote"] for entry in relative["evidence"])
    relative_trace = browser.encoded(relative_recorded).encode()
    relative_result = review(relative, relative_trace)
    assert relative_result["state"] == "COMPLETE"
    relative_event = relative_result["events"][0]
    assert relative_event["occurred_at_basis"] == "RELATIVE_AGE_UPPER_BOUND"
    assert relative_event["occurred_at"] == (current - dt.timedelta(days=84)).isoformat()
    replay = review(relative, relative_trace)
    assert replay["events"] == relative_result["events"]
    checks.append("quoted four-month relative initial qualifies with stable replay")
    for text, days in (("28 days ago", 27), ("5 weeks ago", 28), ("4 months ago", 84), ("2 years ago", 365)):
        assert browser._relative_age_upper_bound(text, current - dt.timedelta(days=27), current) == current - dt.timedelta(days=days)
    checks.append("relative minimum unit bounds")
    for label in ("one month", "recent weeks", "unquoted relative", "wrong relative bound", "relative inbound", "relative acceptance"):
        altered = copy.deepcopy(relative)
        altered_recorded = copy.deepcopy(relative_recorded)
        if label in {"one month", "recent weeks"}:
            value = "1 month ago" if label == "one month" else "4 weeks ago"
            altered["events"][0].update(date_text=value, occurred_at=(current if label == "one month" else current - dt.timedelta(days=21)).isoformat())
            altered["evidence"][-1]["quote"] = altered["evidence"][-1]["quote"].replace("Sent4monthsago", value)
            altered_recorded["item"]["result"]["content"][0]["text"] = "\n".join(entry["quote"] for entry in altered["evidence"])
        elif label == "unquoted relative":
            altered["events"][0]["date_text"] = "5 months ago"
        elif label == "wrong relative bound":
            altered["events"][0]["occurred_at"] = (current - dt.timedelta(days=85)).isoformat()
        else:
            altered["events"][0]["kind"] = "inbound" if label == "relative inbound" else "accepted"
        try:
            review(altered, browser.encoded(altered_recorded).encode())
        except ValueError:
            pass
        else:
            raise AssertionError(label)
    checks.append("recent unquoted wrong-bound and recipient-relative dates rejected")
    normalized["requires_outreach_anchor"] = False

    def executor(config, prompt, directory, timeout):
        assert timeout == 240 and "Stop further history scans" not in prompt
        assert "Never stop on prior outreach" in prompt
        assert browser.INACTIVITY_NAVIGATION in browser.PROMPT and browser.INACTIVITY_NAVIGATION in prompt
        assert "evidence for ALL THREE steps" in prompt
        assert "same Other conversation with matching participant, date AND preview" in prompt
        assert "Do not pretend the control proves archive/spam" in prompt
        assert "Do not expand to an all-time folder scan" in prompt
        supplied = json.loads((directory / "request.json").read_text())
        output, recorded = fixture(supplied)
        (directory / "output.json").write_text(browser.encoded(output))
        (directory / "trace.jsonl").write_bytes(recorded)

    with tempfile.TemporaryDirectory(prefix="manual-inactivity-browser-", dir=Path(__file__).parent) as root:
        collector = browser.Collector(root, {}, executor=executor)
        assert collector.review_inactivity(request)["state"] == "COMPLETE"
        bad_request = dict(request, read_only=False)
        assert collector.review_inactivity(bad_request)["state"] == "UNKNOWN"
        assert collector.review_inactivity(dict(request, cutoff_at="yesterday"))["state"] == "UNKNOWN"
    checks.append("parent contract and same runner offline")
    with tempfile.TemporaryDirectory(prefix="manual-inactivity-cli-", dir=Path(__file__).parent) as root:
        request_path, output_path = Path(root) / "request.json", Path(root) / "output.json"
        request_path.write_text(browser.encoded(request))
        arguments = ["outreach_history_browser.py", "--root", root, "--request", str(request_path),
                     "--output", str(output_path), "--codex", "unused-fixture", "--model", "unused-fixture"]
        offline = browser.Collector(root, {}, executor=executor)
        with mock.patch.object(browser, "Collector", return_value=offline), mock.patch("sys.argv", arguments + ["--inactivity"]), contextlib.redirect_stdout(io.StringIO()):
            browser.main()
        saved = json.loads(output_path.read_text())
        assert saved["summary"]["state"] == "COMPLETE" and saved["response"]["events"] == []
        normal = mock.Mock(return_value={"raw": {"result": {"outcome": "CLEAR"}}, "source": "fixture", "coverage": {}})
        with mock.patch.object(browser, "Collector", return_value=normal), mock.patch("sys.argv", arguments), contextlib.redirect_stdout(io.StringIO()):
            browser.main()
        assert json.loads(output_path.read_text())["summary"]["state"] == "OBSERVED"
        normal.assert_called_once_with(request)
        normal.review_inactivity.assert_not_called()
    checks.append("inactivity CLI writes structured output; normal CLI unchanged")
    print(json.dumps({"state": "PASS", "checks": checks, "live_calls": 0}, indent=2))


if __name__ == "__main__":
    main()
