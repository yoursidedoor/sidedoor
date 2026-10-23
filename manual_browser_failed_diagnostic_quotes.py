"""Offline quote-binding rehearsal; optional immutable production-artifact replay."""

import argparse
import copy
import datetime as dt
import json
from pathlib import Path

import application_browser as browser
from manual_application_browser import event


OBSERVED_AT = "2026-09-18T06:29:25+00:00"
FAILURE = "Browser Use could not load https://synthetic.example/: net::ERR_CONNECTION_REFUSED."


def fixture():
    request = {"request_id": "synthetic-diagnostic", "binding": "synthetic-binding", "max_tool_calls": 40,
               "payload": {"operation": "inspect", "job": {"job_key": "synthetic:role-123"}}, "context": {}}
    result = {"blocked": {"code": "INSPECTION_INCOMPLETE", "next_action": "Read the company page again later"},
              "partial_review": {"verified": False, "official_destination_verified": False,
                                 "all_required_questions_enumerated": False, "employer": "Synthetic Company"}}
    successful = event("read", "Synthetic Company official role page")
    failed = event("failed", FAILURE)
    failed["item"].update(status="failed")
    failed["item"]["result"].update(isError=True, _meta={"codex/browserUse": True,
        "browser_use": {"url": "https://synthetic.example/"}})
    trace = (json.dumps(successful) + "\n" + json.dumps(failed) + "\n").encode()
    evidence = [{"pointer": pointer, "tool_call_id": None, "quote": FAILURE} for pointer in
        ("/blocked/code", "/blocked/next_action", "/partial_review/verified",
         "/partial_review/official_destination_verified", "/partial_review/all_required_questions_enumerated")]
    evidence.append({"pointer": "/partial_review/employer", "tool_call_id": "read", "quote": "Synthetic Company"})
    raw = {"request_id": request["request_id"], "binding": request["binding"],
           "result_json": json.dumps(result), "evidence": evidence}
    return request, raw, trace


def normalize(request, raw, trace):
    return browser.normalize(request, raw, trace, OBSERVED_AT, Path("synthetic-offline-trace.jsonl"))


def rejected(action, pointer, claimed=None):
    """The cited quote is refused: the read fails, or (inspect) that one evidence entry is
    recorded as rejected. Since 2026-10-07 (owner, item 2) an inspect claim is no longer
    withdrawn for it: the AI's report stands and only the refused quote is recorded, so
    `claimed` is kept for the call sites and no longer asserted."""
    try:
        result = action()
    except ValueError as error:
        assert str(error) == "APPLICATION_UNOBSERVED_QUOTE", str(error)
        return
    reasons = {entry["pointer"]: entry["reason"] for entry in result.get("rejected_evidence", [])}
    if reasons.get(pointer) != "APPLICATION_UNOBSERVED_QUOTE":
        raise AssertionError("Failed diagnostics were accepted as positive/absence evidence")


def run(artifact_directory=None):
    request, raw, trace = fixture()
    result = normalize(request, raw, trace)
    assert result["blocked"]["code"] == "INSPECTION_INCOMPLETE"
    assert result["partial_review"]["employer"] == "Synthetic Company"
    assert result["partial_review"]["verified"] is False
    assert result["partial_review"]["official_destination_verified"] is False
    assert all(entry.get("source_kind") == "failed_tool_diagnostic"
               for entry in result["browser_evidence"] if entry["tool_call_id"] == "failed")
    checks = ["verbatim failed diagnostic quotes bind while successful partial facts and negative verification remain unchanged"]
    explicit = copy.deepcopy(raw)
    for entry in explicit["evidence"]:
        if entry["quote"] == FAILURE:
            entry["tool_call_id"] = "failed"
    assert normalize(request, explicit, trace)["blocked"]["code"] == "INSPECTION_INCOMPLETE"
    checks.append("both unavailable and exact tool-call IDs preserve failed diagnostic provenance")
    for field in ("verified", "official_destination_verified", "all_required_questions_enumerated"):
        forged = copy.deepcopy(raw)
        claimed = json.loads(forged["result_json"])
        claimed["partial_review"][field] = True
        forged["result_json"] = json.dumps(claimed)
        rejected(lambda: normalize(request, forged, trace), "/partial_review/" + field, True)
    checks.append("positive verification citing failed browser output is recorded as rejected evidence")
    for field in ("application", "interview"):
        forged = copy.deepcopy(raw)
        claimed = json.loads(forged["result_json"])
        claimed["partial_review"]["history"] = {field: False}
        forged["result_json"] = json.dumps(claimed)
        forged["evidence"].append({"pointer": "/partial_review/history/" + field, "tool_call_id": "failed", "quote": FAILURE})
        rejected(lambda: normalize(request, forged, trace), "/partial_review/history/" + field, False)
    checks.append("failed browser output citing application or interview absence is recorded as rejected evidence")
    fabricated = copy.deepcopy(raw)
    fabricated["evidence"][0]["quote"] = "This diagnostic was never observed"
    rejected(lambda: normalize(request, fabricated, trace), "/blocked/code")
    wrong_call = copy.deepcopy(explicit)
    wrong_call["evidence"][0]["tool_call_id"] = "read"
    rejected(lambda: normalize(request, wrong_call, trace), "/blocked/code")
    checks.append("invented diagnostic quotes and incorrect explicit call IDs remain rejected")
    replay = None
    if artifact_directory:
        directory = Path(artifact_directory)
        raw_saved = json.loads((directory / "output.json").read_text())
        observed_at = dt.datetime.fromtimestamp((directory / "output.json").stat().st_mtime, dt.timezone.utc).isoformat()
        replayed = browser.normalize(json.loads((directory / "request.json").read_text()), raw_saved,
            (directory / "trace.jsonl").read_bytes(), observed_at, directory / "trace.jsonl")
        original = json.loads(raw_saved["result_json"])
        assert replayed["blocked"]["code"] == (original.get("blocked") or original.get("blocker"))["code"]
        original_partial = original["partial_review"]
        assert replayed["partial_review"]["official_destination_verified"] == original_partial["official_destination_verified"]
        assert replayed["partial_review"]["verified"] is False
        replay = {"request_id": raw_saved["request_id"], "blocked": replayed["blocked"]["code"],
                  "failed_diagnostic_pointers": [entry["pointer"] for entry in replayed["browser_evidence"]
                                                 if entry.get("source_kind") == "failed_tool_diagnostic"],
                  "observed_browser_calls": replayed["observed_browser_calls"],
                  "official_destination_verified_preserved": replayed["partial_review"]["official_destination_verified"],
                  "replay_time_basis": "Original output artifact mtime, never current time", "observed_at": observed_at}
        checks.append("actual saved failed inspection replays its own blocker without inventing or verifying missing facts")
    return {"mode": "OFFLINE_READ_ONLY", "passed": len(checks), "checks": checks, "saved_replay": replay,
            "production_writes": 0, "database_writes": 0, "browser_calls": 0, "launches": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-directory", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.artifact_directory), indent=2))
