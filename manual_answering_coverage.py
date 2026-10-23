"""How much of a real form does the answering step actually answer? Offline, no side effects.

The safety redesign routes several families of question to the owner on purpose. A step that
routes everything to the owner is safe and useless, so this measures which side of that line
we are on, against the nine real employer forms recovered from the saved browser artefacts.

It needs the adversarial agent's fixture corpus, which is read-only and not modified here:

    cp tests/answering_fixtures.py <isolated-root>/tests/
    cd <isolated-root> && PYTHONPATH=tests /opt/homebrew/bin/python3.13 manual_answering_coverage.py

The model is a local deterministic stand-in that classifies each question the way a competent
model would: it reads the question, picks the class Python has a computation for, and cites
the obvious source. That is the optimistic case on purpose - the number it produces is the
ceiling on coverage, and the ceiling is the thing worth knowing.
"""

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import answering_agent
import applications
import orchestrator

import answering_fixtures as fx
import user_profile


NOW = "2026-09-18T16:00:00+00:00"
ROOT = Path(orchestrator.__file__).resolve().parent
POLICY = user_profile.load()
MARKER = "\n\nUNTRUSTED_APPLICATION_PACKET_JSON:\n"

FORMS = [name for name in dir(fx) if name.endswith("_QUESTIONS")]

US_REVIEW = {"facts": {"country": "US", "city": "Springfield, IL; Austin, TX",
                       "work_mode": "hybrid"}, "compensation": {},
             "official_url": "https://example.invalid/apply", "role": "Senior Software Engineer",
             "form_revision": "coverage-v1"}
EU_REVIEW = {"facts": {"country": "DE", "city": "Leipzig", "work_mode": "onsite"},
             "compensation": {}, "official_url": "https://example.invalid/apply",
             "role": "Senior Software Engineer", "form_revision": "coverage-v1"}


def blank(question, **overrides):
    record = {"question_key": question["question_key"],
              "question_semantic": question.get("semantic"), "question_class": "OTHER",
              "status": "UNKNOWN", "answer_type": question.get("answer_type", "text"),
              "value": None, "jurisdiction": None, "assertion": None, "affirmative_value": None,
              "negative_value": None, "attribution": [], "derivation": "coverage fixture",
              "unknown_reason": "coverage fixture"}
    record.update(overrides)
    return record


def cite(pointer, quote):
    return {"pointer": pointer, "quote": quote}


def competent(question, packet):
    """A model doing its job: classify honestly and cite the obvious source."""
    text = " ".join(str(question.get(field) or "") for field in ("label", "semantic")).lower()
    named = sorted(applications.jurisdictions_named(str(question.get("label") or ""),
                                                    str(question.get("semantic") or "")))
    index = next((position for position, item in enumerate(packet["questions"])
                  if item["question_key"] == question["question_key"]), 0)
    label = str(question.get("label") or "")
    label_cite = ([cite("/questions/" + str(index) + "/label", label)] if label.strip()
                  else [cite("/questions/" + str(index) + "/semantic",
                             str(question.get("semantic") or ""))])
    if "sponsor" in text:
        return blank(question, question_class="SPONSORSHIP_REQUIREMENT",
                     status="DEFER_TO_POLICY_COMPUTATION", unknown_reason=None,
                     jurisdiction=named[0] if named else "US",
                     assertion="REQUIRES_SPONSORSHIP_IN_JURISDICTION", attribution=label_cite)
    if any(word in text for word in ("authoriz", "authoris", "eligible to work", "permitted to work",
                                     "right to work", "legally")):
        return blank(question, question_class="WORK_AUTHORIZATION_HELD",
                     status="DEFER_TO_POLICY_COMPUTATION", unknown_reason=None,
                     jurisdiction=named[0] if named else "US",
                     assertion="HOLDS_AUTHORIZATION_IN_JURISDICTION", attribution=label_cite)
    if "relocat" in text:
        return blank(question, question_class="LOCATION_PREFERENCE", status="ANSWERED",
                     unknown_reason=None, value="Yes",
                     attribution=[cite("/policy/location_priority/0", "Springfield, IL hybrid/onsite")])
    if any(word in text for word in ("office", "on-site", "onsite", "in-person", "in person",
                                     "hybrid", "remote")):
        return blank(question, question_class="WORK_MODE_PREFERENCE", status="ANSWERED",
                     unknown_reason=None, value="Yes",
                     attribution=[cite("/policy/location_priority/0", "Springfield, IL hybrid/onsite")])
    if "salary" in text or "compensation" in text:
        return blank(question, question_class="COMPENSATION_EXPECTATION", status="ANSWERED",
                     unknown_reason=None, value="200000",
                     attribution=[cite("/policy/desired_base_salary_usd/0", "200000")])
    if any(word in text for word in ("notice", "start", "available")):
        return blank(question, question_class="AVAILABILITY", status="ANSWERED",
                     unknown_reason=None, value="2026-10-16",
                     attribution=[cite("/policy/answers/notice_period/value", "2 weeks")])
    if any(word in text for word in ("why", "tell us", "describe", "cover letter")):
        return blank(question, question_class="ROLE_SPECIFIC_FREE_TEXT", status="ANSWERED",
                     unknown_reason=None, value="I am excited about this team.",
                     attribution=[cite("/review/role", "Senior Software Engineer")])
    for key, recorded in packet["policy"]["answers"].items():
        value = recorded.get("value")
        if isinstance(value, str) and value and any(
                word in text for word in ("name", "email", "phone", "linkedin", "location",
                                          "city", "country", "company", "title")):
            return blank(question, question_class="IDENTITY_OR_CONTACT", status="ANSWERED",
                         unknown_reason=None, value=value,
                         attribution=[cite("/policy/answers/" + key + "/value", value)])
    return blank(question)


def judge_accepting(prompt):
    """The answer judge's stand-in: accepts every reviewed answer (2026-10-21)."""
    review = json.loads(prompt.split(answering_agent.JUDGE_MARKER, 1)[1])
    return json.dumps({"verdicts": [{"question_key": item["question"]["question_key"], "verdict": "ACCEPT",
                                     "reason": "stand-in judge", "correction": None,
                                     "untrue_fact": False}
                                    for item in review["answers_to_review"]]})


def executor(prompt):
    if answering_agent.JUDGE_MARKER in prompt:
        return judge_accepting(prompt)
    packet = json.loads(prompt.split(MARKER, 1)[1])
    return json.dumps({"packet_sha256": packet["packet_sha256"],
                       "answers": [competent(question, packet) for question in packet["questions"]]})


def run(questions, review):
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    try:
        for folder in ("state", "reports", "logs"):
            (root / folder).mkdir()
        for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
            (root / name).write_text((ROOT / name).read_text())
        database = orchestrator.connect(root)
        applications.initialize(database)
        job = {"job_key": "coverage:demo", "employer": "coverage", "requisition": "demo"}
        database.execute("INSERT INTO application_jobs VALUES (?,?,?,?,?,0,0,NULL,NULL,NULL,NULL,?)",
                         (job["job_key"], job["employer"], job["requisition"],
                          json.dumps({"company": "Coverage", "role": "Senior Software Engineer",
                                      "url": "https://example.invalid/apply",
                                      "requisition_id": "demo"}), "READY", NOW))
        database.commit()
        result = answering_agent.answer(
            database, job, questions, json.loads(json.dumps(POLICY)), NOW, executor, root=root,
            config={"application_answering": {"enabled": True, "timeout_seconds": 5},
                    "timezone": "UTC"}, review=review)
        database.close()
        return result
    finally:
        directory.cleanup()


def main():
    totals = {"answered": 0, "unknown": 0, "refused": 0}
    print("%-26s %8s %9s %8s %8s" % ("form", "required", "answered", "owner", "refused"))
    print("-" * 64)
    for name in sorted(FORMS):
        questions = [dict(question) for question in getattr(fx, name)
                     if isinstance(question, dict) and question.get("required")]
        if not questions:
            continue
        review = EU_REVIEW if name.startswith("ALPINE") else US_REVIEW
        result = run(questions, review)
        answered = len(result["answers"])
        refused = len(result["refused"])
        unknown = len(result["unknown"])
        totals["answered"] += answered
        totals["unknown"] += unknown
        totals["refused"] += refused
        print("%-26s %8d %9d %8d %8d" % (name.replace("_QUESTIONS", "").title(),
                                          len(questions), answered, unknown, refused))
    print("-" * 64)
    total = sum(totals.values())
    print("%-26s %8d %9d %8d %8d" % ("TOTAL", total, totals["answered"], totals["unknown"],
                                     totals["refused"]))
    if total:
        print("\nanswered %.0f%%   routed to the owner %.0f%%   never auto-answered %.0f%%"
              % (100 * totals["answered"] / total, 100 * totals["unknown"] / total,
                 100 * totals["refused"] / total))
    print("\nNo submission, browser call, Telegram send, provider request or live AI call occurred.")


if __name__ == "__main__":
    main()
