"""Run question + profile cases through the real answering AI and its judge (owner, 2026-10-24).

    python tools/answering_reasoning_eval.py                 # every case, real model calls
    python tools/answering_reasoning_eval.py --cases c01,c13 # a subset
    python tools/answering_reasoning_eval.py --dry-run       # build and print packets, no model call
    python tools/answering_reasoning_eval.py --out report.json

Each case goes through `answering_agent.answer` exactly as an application does: the same prompt
files, schemas, packet builder (`build_packet`), judge/repair rounds, model and OpenRouter
routing, read from the bundled config.json (`text_model.model`, "z-ai/glm-5.3"). The OpenRouter key
is read the way production reads it (`text_model.key`: the saved key in the OS store), so a
real run reads the owner's own keychain. The profile of each case is built by Setup's own code
(`setup_steps.validate_profile` + `profile_document`, `save_owner_answer` for answers Setup has no
pick for) in a temporary folder; the owner's real profile, database and settings are not read or
written. Two things differ from an application, both outside the model's input shape:
- the résumé text is the case file's (`reply_resume.context` returns it in its usual shape);
- the database is a throwaway in-memory one, so no model_calls rows and no answers are saved.
Nothing is sent to any employer: no browser, no form, no submission.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import answering_agent  # noqa: E402
import applications  # noqa: E402
import reply_resume  # noqa: E402
import resources  # noqa: E402
import setup_steps  # noqa: E402
import text_model  # noqa: E402
import user_profile  # noqa: E402

import answering_eval_cases  # noqa: E402

DRY_RUN = "EVAL_DRY_RUN"


def production_config():
    """The config an application runs with (`answering_agent._config`), answering switched on."""
    config = resources.read_json("config.json")
    config["application_answering"] = dict(config.get("application_answering") or {}, enabled=True)
    return config


def build_policy(case, home):
    """The case's policy, written by Setup's producer into `home` and merged as user_profile.load does."""
    form = dict(answering_eval_cases.BASE_FORM, **case.get("form", {}))
    clean, errors = setup_steps.validate_profile(form, {})
    if errors:
        raise ValueError("case %s: Setup refused the form: %s" % (case["id"], errors))
    profile = setup_steps.profile_document(clean, {})
    profile.update(case.get("policy", {}))
    (home / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    for number, (semantic, value, question) in enumerate(case.get("owner_answers", [])):
        setup_steps.save_owner_answer("eval-%s-%d" % (case["id"], number), semantic, value,
                                      "Answering eval; question: " + question, home=home,
                                      yes_no=isinstance(value, bool), asked_for={"question": question})
    profile = setup_steps.read_profile(home)
    for key in case.get("drop_answers", []):
        profile["answers"].pop(key, None)
    settings = json.loads(user_profile.DEFAULT_SETTINGS.read_text())
    return user_profile.merge(profile, settings)


@contextlib.contextmanager
def case_resume(text):
    """`reply_resume.context` returning the case's résumé, in the shape it always returns."""
    content = text.encode("utf-8")
    original = reply_resume.context
    reply_resume.context = lambda root=None: {
        "source": "tools/answering_eval_cases.py", "source_sha256": None, "page_count": 1,
        "extraction": "answering eval fixture", "text": text,
        "text_sha256": hashlib.sha256(content).hexdigest(), "text_bytes": len(content)}
    try:
        yield
    finally:
        reply_resume.context = original


@contextlib.contextmanager
def no_network():
    """--dry-run: any attempt to reach the network fails the run instead."""
    def refuse(*arguments, **named):
        raise AssertionError("--dry-run made a network call")
    saved = (socket.create_connection, socket.socket.connect, text_model._post)
    socket.create_connection, socket.socket.connect, text_model._post = refuse, refuse, refuse
    try:
        yield
    finally:
        socket.create_connection, socket.socket.connect, text_model._post = saved


def question_of(case):
    return dict(case["question"], id="q-" + case["id"], required=True)


def run_case(case, config, now, dry_run):
    question = question_of(case)
    job = {"job_key": "answering-eval:" + case["id"], "employer": "Answering Eval",
           "requisition": case["id"]}
    captured = []

    def capture(prompt, packet):
        captured.append({"packet": packet, "prompt_bytes": len(prompt.encode("utf-8"))})
        raise answering_agent.AnsweringUnavailable(DRY_RUN)

    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    with tempfile.TemporaryDirectory() as folder, case_resume(answering_eval_cases.RESUME):
        policy = build_policy(case, Path(folder))
        result = answering_agent.answer(database, job, [question], policy, now,
                                        capture if dry_run else None, root=ROOT, config=config,
                                        review=case.get("review"))
    database.close()
    return question, result, captured


def latest_entry(result, key):
    """The answering AI's latest entry for `key`: the last repair that answered it, else the first call."""
    raws = [result["audit"].get("raw")] + [(round_.get("repair") or {}).get("raw")
                                           for round_ in result["audit"].get("rounds") or []]
    found = None
    for raw in raws:
        try:
            entries = json.loads(raw).get("answers") if raw else None
        except (TypeError, ValueError, AttributeError):
            continue
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("question_key") == key:
                found = entry
    return found


def latest_verdict(result, key):
    for round_ in reversed(result["audit"].get("rounds") or []):
        for verdict in (round_.get("judge") or {}).get("verdicts") or []:
            if verdict.get("question_key") == key:
                return dict(verdict, round=round_["round"])
    return None


def number_of(value):
    try:
        return float(str(value).replace(",", "").replace("£", "").replace("$", "").strip())
    except ValueError:
        return None


def grade(expected, key, result):
    """(passed, what was given)."""
    if key in result["answers"]:
        given = result["answers"][key]["value"]
    elif key in result["unknown"]:
        given = "UNKNOWN"
    else:
        given = "REFUSED"
    if expected.get("unknown"):
        return given == "UNKNOWN", given
    if "number" in expected:
        low, high = expected["number"]
        figure = number_of(given) if given not in ("UNKNOWN", "REFUSED") else None
        return figure is not None and low <= figure <= high, given
    return given == expected["value"], given


def expected_text(expected):
    if expected.get("unknown"):
        return "UNKNOWN"
    if "number" in expected:
        return "a number in %s-%s" % tuple(expected["number"])
    return expected["value"]


def cost_of(result):
    calls = result["audit"].get("calls") or []
    costs = [(call.get("usage") or {}).get("cost") for call in calls]
    priced = [cost for cost in costs if isinstance(cost, (int, float)) and not isinstance(cost, bool)]
    return len(calls), sum(priced), len(calls) - len(priced)


def packet_summary(case, packet, prompt_bytes):
    policy = packet["policy"]
    resume = packet.get("candidate_resume") or {}
    return {"case": case["id"], "packet_version": packet["packet_version"],
            "packet_sha256": packet["packet_sha256"], "prompt_bytes": prompt_bytes,
            "questions": packet["questions"], "location_priority": policy.get("location_priority"),
            "onsite_location_exclusions": policy.get("onsite_location_exclusions"),
            "desired_base_salary_usd": policy.get("desired_base_salary_usd"),
            "answers": {key: value["value"] for key, value in policy["answers"].items()},
            "review": packet["review"], "resume_bytes": resume.get("text_bytes")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="build and print packets; no model call")
    parser.add_argument("--out", default="answering-eval.json", help="JSON report path")
    options = parser.parse_args(argv)
    cases = answering_eval_cases.CASES
    if options.cases:
        wanted = [name.strip() for name in options.cases.split(",") if name.strip()]
        unknown = sorted(set(wanted) - {case["id"] for case in cases})
        if unknown:
            parser.error("no such case: " + ", ".join(unknown))
        cases = [case for case in cases if case["id"] in wanted]
    config = production_config()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    print("Model: %s via %s (reasoning %s); %d case(s)%s" % (
        text_model.model(config), text_model.auth(config), answering_agent.DIRECT_EFFORT, len(cases),
        " -- DRY RUN, no model call" if options.dry_run else ""))
    report = {"created_at": now, "model": text_model.model(config), "dry_run": options.dry_run, "cases": []}
    totals = {"passed": 0, "failed": 0, "calls": 0, "cost": 0.0, "unpriced": 0}
    guard = no_network() if options.dry_run else contextlib.nullcontext()
    with guard:
        for case in cases:
            question, result, captured = run_case(case, config, now, options.dry_run)
            key = applications.question_field(question)
            if options.dry_run:
                if not captured:
                    raise SystemExit("case %s built no packet (outcome %s)" % (case["id"], result["audit"]["outcome"]))
                summary = packet_summary(case, captured[0]["packet"], captured[0]["prompt_bytes"])
                print("\n=== %s: %s" % (case["id"], question["label"]))
                print(json.dumps(summary, indent=2, ensure_ascii=False))
                report["cases"].append({"id": case["id"], "packet": captured[0]["packet"]})
                continue
            entry = latest_entry(result, key) or {}
            verdict = latest_verdict(result, key) or {}
            passed, given = grade(case["expected"], key, result)
            calls, cost, unpriced = cost_of(result)
            totals["passed" if passed else "failed"] += 1
            totals["calls"] += calls
            totals["cost"] += cost
            totals["unpriced"] += unpriced
            print("\n%s  %s" % (case["id"], question["label"]))
            print("    expected: %s" % expected_text(case["expected"]))
            print("    answerer: %s %s" % (entry.get("status"), json.dumps(entry.get("value"), ensure_ascii=False)))
            print("    judge:    %s (round %s) %s" % (verdict.get("verdict"), verdict.get("round"),
                                                     verdict.get("reason") or result["audit"]["outcome"]))
            print("    result:   %s  -> %s" % (json.dumps(given, ensure_ascii=False), "PASS" if passed else "FAIL"))
            report["cases"].append({
                "id": case["id"], "question": question, "expected": case["expected"], "note": case.get("note"),
                "given": given, "passed": passed, "answerer": entry, "judge": verdict,
                "outcome": result["audit"]["outcome"], "calls": result["audit"].get("calls"),
                "rounds": result["audit"].get("rounds"), "raw": result["audit"].get("raw")})
    if not options.dry_run:
        report["summary"] = totals
        print("\nSummary: %d PASS, %d FAIL of %d; %d model calls; cost $%.4f%s" % (
            totals["passed"], totals["failed"], len(cases), totals["calls"], totals["cost"],
            " (%d calls without cost data)" % totals["unpriced"] if totals["unpriced"] else ""))
        failed = [item["id"] for item in report["cases"] if not item["passed"]]
        if failed:
            print("Failed: " + ", ".join(failed))
    Path(options.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print("Report: " + str(Path(options.out).resolve()))
    return report


if __name__ == "__main__":
    main()
