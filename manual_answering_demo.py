"""Manual worked examples for the AI answering step. Offline, no live AI call, no writes.

Run from an isolated copy of ROOT:

    /opt/homebrew/bin/python3.13 manual_answering_demo.py

It builds a throwaway root and database in a temporary directory, drives
`answering_agent.answer` with a local deterministic fake executor, and prints the six
results the fix report quotes verbatim. It performs no submission, no browser call, no
Telegram send and no provider request, and it touches nothing under the real project root.
"""

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import answering_agent
import applications
import orchestrator
import workflow_tasks
import user_profile


NOW = "2026-09-17T16:00:00+00:00"
ROOT = Path(orchestrator.__file__).resolve().parent
POLICY = user_profile.load()
MARKER = "\n\nUNTRUSTED_APPLICATION_PACKET_JSON:\n"


def question(identifier, semantic, label, answer_type="boolean", choices=("Yes", "No")):
    return {"id": identifier, "semantic": semantic, "label": label, "required": True,
            "answer_type": answer_type, "choices": list(choices)}


GERMANY = question("q-de", "currently_authorized_to_work_in_germany",
                   "Are you legally authorized to work in Germany?")
SPONSORSHIP = question("q-eu", "requires_visa_sponsorship_in_eu",
                       "Will you now or in the future require visa sponsorship to work in the EU?")
RESIDENCE = question("q-res", "authorized_where_you_reside",
                     "Are you authorized to work in the country where you reside?")
OFFICE = {"id": None, "semantic": "office_attendance_2_3_days_per_week_in_springfield_or_austin",
          "label": "Can you work from our office 2-3 days a week? Our offices are in Springfield, IL and Austin.",
          "required": True, "answer_type": "boolean", "choices": ["Yes", "No"]}
KUBERNETES = question("q-k8s", "years_of_kubernetes_experience",
                      "How many years of Kubernetes experience do you have?", "number", ())
START = question("q-start", "earliest_start_date", "What is your earliest start date?", "text", ())


def entry(packet_question, **overrides):
    value = {"question_key": packet_question["question_key"],
             "question_semantic": packet_question["semantic"], "question_class": "OTHER",
             "status": "UNKNOWN", "answer_type": packet_question["answer_type"], "value": None,
             "jurisdiction": None, "assertion": None, "affirmative_value": None,
             "negative_value": None, "attribution": [], "derivation": "", "unknown_reason": "none"}
    value.update(overrides)
    return value


def judge_accepting(prompt):
    """The answer judge's stand-in: accepts every reviewed answer (2026-10-21)."""
    review = json.loads(prompt.split(answering_agent.JUDGE_MARKER, 1)[1])
    return json.dumps({"verdicts": [{"question_key": item["question"]["question_key"], "verdict": "ACCEPT",
                                     "reason": "stand-in judge", "correction": None,
                                     "untrue_fact": False}
                                    for item in review["answers_to_review"]]})


def model(respond=None, raw=None, error=None):
    def executor(prompt):
        if answering_agent.JUDGE_MARKER in prompt:
            return judge_accepting(prompt)
        if error is not None:
            raise error
        packet = json.loads(prompt.split(MARKER, 1)[1])
        if raw is not None:
            return raw
        return json.dumps({"packet_sha256": packet["packet_sha256"],
                           "answers": [respond(item, packet) for item in packet["questions"]]})
    return executor


def fixture():
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    for folder in ("state", "reports", "logs"):
        (root / folder).mkdir()
    for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
        (root / name).write_text((ROOT / name).read_text())
    # The designated resume, so the packet actually carries the document the experience
    # computation reads. Copied, never uploaded anywhere.
    import reply_resume
    target = root / reply_resume.source()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((ROOT / reply_resume.source()).read_bytes())
    database = orchestrator.connect(root)
    applications.initialize(database)
    workflow_tasks.initialize(database)
    job = {"job_key": "demo employer:demo-req", "employer": "demo employer",
           "requisition": "demo-req", "candidate_email": POLICY["answers"]["email"]["value"]}
    database.execute("INSERT INTO application_jobs VALUES (?,?,?,?,?,0,0,NULL,NULL,NULL,NULL,?)",
                     (job["job_key"], job["employer"], job["requisition"],
                      json.dumps({"company": "Demo", "role": "Staff Software Engineer",
                                  "url": "https://example.invalid/demo",
                                  "requisition_id": "demo-req"}), "READY", NOW))
    database.commit()
    return directory, root, database, job


def review(country="US", city="Springfield, IL; Austin, TX", work_mode="hybrid"):
    return {"facts": {"country": country, "city": city, "work_mode": work_mode},
            "compensation": {}, "official_url": "https://example.invalid/demo",
            "role": "Staff Software Engineer", "form_revision": "demo-v1"}


def run(questions, executor, facts=None, now=NOW, policy=None):
    directory, root, database, job = fixture()
    try:
        return answering_agent.answer(
            database, job, questions, policy or json.loads(json.dumps(POLICY)), now, executor,
            root=root, config={"application_answering": {"enabled": True, "timeout_seconds": 5},
                               "timezone": "UTC"},
            review=facts or review())
    finally:
        database.close()
        directory.cleanup()


def show(title, questions, result):
    print("=" * 78)
    print(title)
    print("=" * 78)
    for item in questions:
        key = applications.question_field(item)
        if key in result["answers"]:
            record = result["answers"][key]
            print("  " + item["label"])
            print("    ANSWER      " + json.dumps(record["value"]))
            print("    basis       " + record["basis"])
            print("    attribution " + ", ".join(
                citation["pointer"] + " -> " + json.dumps(citation["quote"])
                for citation in record["attribution"]))
        elif key in result["unknown"]:
            verdict = next((item for item in result["audit"]["verdicts"]
                            if item["question_key"] == key), {})
            print("  " + item["label"])
            print("    UNKNOWN     reason " + str(verdict.get("reason") or result["audit"]["outcome"]))
        else:
            reason = next(item["reason"] for item in result["refused"] if item["question_key"] == key)
            print("  " + item["label"])
            print("    REFUSED     " + reason)
    print("  audit.outcome " + result["audit"]["outcome"])
    print()


def three_way():
    def respond(item, packet):
        if item["semantic"] == GERMANY["semantic"]:
            return entry(item, question_class="WORK_AUTHORIZATION_HELD",
                         status="DEFER_TO_POLICY_COMPUTATION", jurisdiction="DE",
                         assertion="HOLDS_AUTHORIZATION_IN_JURISDICTION",
                         affirmative_value="Yes", negative_value="No", unknown_reason=None,
                         attribution=[{"pointer": "/questions/0/label", "quote": "Germany"}],
                         derivation="The question names Germany.")
        if item["semantic"] == SPONSORSHIP["semantic"]:
            return entry(item, question_class="SPONSORSHIP_REQUIREMENT",
                         status="DEFER_TO_POLICY_COMPUTATION", jurisdiction="EU",
                         assertion="REQUIRES_SPONSORSHIP_IN_JURISDICTION",
                         affirmative_value="Yes", negative_value="No", unknown_reason=None,
                         attribution=[{"pointer": "/questions/1/label", "quote": "EU"}],
                         derivation="The question names the EU.")
        return entry(item, question_class="WORK_AUTHORIZATION_HELD",
                     status="DEFER_TO_POLICY_COMPUTATION", jurisdiction="PLACE_OF_RESIDENCE",
                     assertion="AUTHORIZED_AT_PLACE_OF_RESIDENCE", affirmative_value="Yes",
                     negative_value="No", unknown_reason=None,
                     attribution=[{"pointer": "/questions/2/label", "quote": "where you reside"}],
                     derivation="The question asks about the country of residence.")
    questions = [GERMANY, SPONSORSHIP, RESIDENCE]
    show("1. The EU three-way, from one recorded fact set (EU-remote role)", questions,
         run(questions, model(respond), review("DE", "Berlin", "remote")))


def office():
    def respond(item, packet):
        return entry(item, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value="Yes",
                     unknown_reason=None,
                     derivation="Springfield, IL is one of the offices and the top-ranked hybrid/onsite preference.",
                     attribution=[{"pointer": "/policy/location_priority/0",
                                   "quote": "Springfield, IL hybrid/onsite"},
                                  {"pointer": "/review/facts/city", "quote": "Springfield, IL"}])
    show("2. The employer-invented semantic no fixed policy key could match", [OFFICE],
         run([OFFICE], model(respond)))


def no_basis():
    show("3. A question with no basis in the inputs", [KUBERNETES],
         run([KUBERNETES], model(lambda item, packet: entry(item, unknown_reason="Not recorded."))))


def fabrication():
    def respond(item, packet):
        return entry(item, question_class="CANDIDATE_HISTORY_FACT", status="ANSWERED", value=9,
                     unknown_reason=None,
                     derivation="Nine years, per the recorded experience answer.",
                     attribution=[{"pointer": "/policy/answers/years_of_kubernetes_experience/value",
                                   "quote": "9"}])
    show("4. A plausible fabrication: an answer citing a policy key that does not exist",
         [KUBERNETES], run([KUBERNETES], model(respond)))

    def relabel(item, packet):
        return entry(item, question_class="LOCATION_PREFERENCE", status="ANSWERED", value="Yes",
                     unknown_reason=None, derivation="The owner would relocate to Germany.",
                     attribution=[{"pointer": "/policy/answers/relocation_destinations/value/2",
                                   "quote": "Germany"}])
    show("4b. The same fabrication routed around the computed class by relabelling it",
         [GERMANY], run([GERMANY], model(relabel), review("DE", "Berlin", "remote")))


def degradation():
    for title, executor, in (("5a. Model unavailable", model(error=OSError("no executor"))),
                             ("5b. Model timeout", model(error=TimeoutError("deadline"))),
                             ("5c. Malformed output", model(raw="not json at all"))):
        show(title, [OFFICE], run([OFFICE], executor))


def start_date():
    def respond(item, packet):
        return entry(item, question_class="AVAILABILITY", status="ANSWERED", value="2026-09-18",
                     unknown_reason=None, derivation="Next week.",
                     attribution=[{"pointer": "/policy/answers/notice_period/value",
                                   "quote": "2 weeks"}])
    for day in ("2026-09-17T16:00:00+00:00", "2026-10-01T00:00:00+00:00"):
        show("6. Start date computed in Python from application date " + day[:10],
             [START], run([START], model(respond), now=day))


TAILSPIN_ROLE = "Backend Engineer II | September 2016 \u2013 February 2021"
KUBERNETES_BULLET = "Kubernetes operators"
FABRIKAM_ROLE = "Staff Platform Engineer | March 2021 \u2013 August 2025"
SKILLS_LINE = "Infrastructure: Kubernetes, Terraform, Helm, Linux"

TOTAL_EXPERIENCE = question("q-total", "years_of_experience",
                            "How many years of professional software engineering experience "
                            "do you have?", "number", ())
COBOL = question("q-cobol", "years_of_cobol_experience",
                 "How many years of COBOL experience do you have?", "number", ())
ORCHESTRATION = question("q-adjacent", "years_of_container_orchestration_experience",
                         "How many years of container orchestration experience do you have?",
                         "number", ())


def experience_entry(item, assertion, quotes):
    return entry(item, question_class="COMPUTED_EXPERIENCE",
                 status="DEFER_TO_POLICY_COMPUTATION", assertion=assertion, unknown_reason=None,
                 attribution=[{"pointer": "/candidate_resume/text", "quote": quote}
                              for quote in quotes],
                 derivation="the dated resume sections that bear on this question")


def experience():
    """The owner's own example: years of Kubernetes, computed rather than quoted."""
    for title, item, assertion, quotes in (
            ("9. Kubernetes, computed from the dated section that names it", KUBERNETES,
             "EXPERIENCE_WITH_TECHNOLOGY_IN_RESUME", [TAILSPIN_ROLE, KUBERNETES_BULLET]),
            ("9b. Total professional experience, from the union of every dated section",
             TOTAL_EXPERIENCE, "TOTAL_PROFESSIONAL_EXPERIENCE", [TAILSPIN_ROLE]),
            ("9c. A technology the resume never mentions", COBOL,
             "EXPERIENCE_WITH_TECHNOLOGY_IN_RESUME", [TAILSPIN_ROLE, KUBERNETES_BULLET]),
            ("9d. An adjacent technology, bounded by the section it derives from",
             ORCHESTRATION, "EXPERIENCE_WITH_ADJACENT_TECHNOLOGY", [KUBERNETES_BULLET]),
            ("9e. Inflation attempt: a second role's dates cited alongside one Kubernetes bullet",
             KUBERNETES, "EXPERIENCE_WITH_TECHNOLOGY_IN_RESUME",
             [TAILSPIN_ROLE, KUBERNETES_BULLET, FABRIKAM_ROLE]),
            ("9f. Inflation attempt: the undated core-skills line as the only evidence",
             KUBERNETES, "EXPERIENCE_WITH_TECHNOLOGY_IN_RESUME", [SKILLS_LINE]),
            ("9g. Fabrication attempt: the model supplies the number itself", TOTAL_EXPERIENCE,
             "TOTAL_PROFESSIONAL_EXPERIENCE", [TAILSPIN_ROLE])):
        def respond(packet_question, packet, assertion=assertion, quotes=quotes, title=title):
            value = experience_entry(packet_question, assertion, quotes)
            if title.startswith("9g"):
                value["value"] = 12
            return value
        show(title, [item], run([item], model(respond)))


WHY = question("q-why", "why_this_company", "Why do you want to work here?", "text", ())
GROUNDED = ("I have spent the last nine years building freight and warehouse software at "
            "Tailspin and Fabrikam, most recently owning the streaming inventory pipeline "
            "for Fabrikam's warehouse data platform. That work maps directly onto the reliability problems this team is "
            "solving, and I am looking for a role where deep ownership of a hard backend "
            "problem is the job.")


def free_text():
    """Composed prose: answered when it asserts nothing the context does not carry."""
    cases = [("10. A grounded composition, with no citation at all", GROUNDED),
             ("10b. An employer the owner never worked for",
              GROUNDED.replace("Tailspin and Fabrikam", "Globex and Initech")),
             ("10c. A degree the owner does not hold",
              GROUNDED + " I hold an MBA from the University of Washington."),
             ("10d. More years than the resume supports",
              GROUNDED + " I have 12 years in payments."),
             ("10e. A salary history", GROUNDED + " My current base is $310,000."),
             ("10f. A work-authorization claim inside a paragraph",
              GROUNDED + " I am legally authorized to work in the EU.")]
    for title, value in cases:
        def respond(item, packet, value=value):
            return entry(item, question_class="ROLE_SPECIFIC_FREE_TEXT", status="ANSWERED",
                         value=value, unknown_reason=None,
                         derivation="composed from the resume and the role", attribution=[])
        show(title, [WHY], run([WHY], model(respond)))
    # The wordings the owner named, submitted directly: nothing about length, an unresolvable
    # citation, a negated question or a question no net can parse routes them to the owner.
    behavioural = question("q-star", "behavioural_example",
                           "Tell me about a time when you had to ship something hard.", "text", ())
    story = ("At Tailspin I moved the booking database from a single primary to a replicated "
             "cluster. The hard part was keeping carriers able to book during every cutover, so "
             "I moved it in stages and no booking was lost.")
    def tell(item, packet):
        return entry(item, question_class="ROLE_SPECIFIC_FREE_TEXT", status="ANSWERED",
                     value=story, unknown_reason=None, derivation="paraphrases a real project",
                     attribution=[{"pointer": "/policy/answers/nope/value", "quote": "gone"}])
    show("10g. \"Tell me about a time\", with an unresolvable citation and a long answer",
         [behavioural], run([behavioural], model(tell)))


def sensitive():
    questions = [question("q-gender", "gender_identity", "What is your gender identity?", "text", ()),
                 question("q-gender-choice", "gender", "Gender", "choice",
                          ("Male", "Female", "Decline to self-identify")),
                 question("q-vet", "veteran_status", "Protected veteran status", "text", ()),
                 question("q-cc305", "disability_status",
                          "Voluntary self-identification of disability (CC-305)", "text", ()),
                 question("q-certify", "certify_true",
                          "I certify that the information provided is true and complete", "text", ())]
    def respond(item, packet):
        return entry(item, question_class="IDENTITY_OR_CONTACT", status="ANSWERED", value="Yes",
                     unknown_reason=None,
                     attribution=[{"pointer": "/policy/answers/full_name/value", "quote": "Alex"}])
    show("7. Gender from the owner's recorded instruction; everything else never answered",
         questions,
         run(questions, model(respond)))


def seam_cycle(title, executor):
    """Drive applications.cycle end to end and report where the job actually stopped."""
    import hashlib
    from unittest.mock import patch
    # The e2e fixtures live in tests/, which is not on the path when this script is run
    # the documented way (python3.13 manual_answering_demo.py from the project root).
    tests = str(Path(__file__).resolve().parent / "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)
    import test_applications_e2e as e2e
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    try:
        for folder in ("state", "reports", "logs"):
            (root / folder).mkdir()
        for name in ("answering-prompt.md", "answering-schema.json", "answering-judge-prompt.md", "answering-judge-schema.json"):
            (root / name).write_text((ROOT / name).read_text())
        policy = json.loads(json.dumps(POLICY))
        (root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        policy["resume"] = {"path": "mock-resume.pdf", "sha256": hashlib.sha256(
            (root / "mock-resume.pdf").read_bytes()).hexdigest()}
        policy["answers"]["resume"]["value"] = policy["resume"]["path"]
        user_profile.use_policy(policy, root / "profile-home")
        database = orchestrator.connect(root)
        applications.initialize(database)
        workflow_tasks.initialize(database)
        adapter = e2e.MockATS(root)
        adapter.answering_executor = executor
        adapter.customize = lambda result, job: result.update(
            questions=[{"id": "email", "semantic": "contact_email", "required": True},
                       dict(OFFICE)],
            facts=dict(result["facts"], country="US", work_mode="hybrid",
                       city="Springfield, IL; Austin, TX"))
        orchestrator.store_research(database, e2e.PIPELINE,
                                    {"summary": "Mock", "blockers": [],
                                     "opportunities": [e2e.role()], "drafts": []})
        key = applications.identity(e2e.role())[2]
        with patch("socket.create_connection", side_effect=AssertionError("no network")):
            applications.cycle(database, {"application_answering": {"enabled": True,
                                                                    "timeout_seconds": 5},
                                          "timezone": "UTC"}, root, adapter, NOW)
        job = database.execute("SELECT state,blocker,next_action FROM application_jobs WHERE job_key=?",
                               (key,)).fetchone()
        decisions = database.execute(
            "SELECT field,state,summary FROM user_decisions WHERE job_key=? AND state='PENDING'",
            (key,)).fetchall()
        print("=" * 78)
        print(title)
        print("=" * 78)
        print("  job.state    " + str(job["state"]))
        print("  job.blocker  " + str(job["blocker"]))
        for row in decisions:
            print("  user_decisions.field  " + row["field"] + "  state " + row["state"])
            print("  summary               " + row["summary"][:150])
        print("  submissions  " + str([operation for operation, payload in adapter.calls
                                       if operation == "submit"]))
        print()
        database.close()
    finally:
        directory.cleanup()


if __name__ == "__main__":
    three_way()
    office()
    no_basis()
    fabrication()
    degradation()
    start_date()
    sensitive()
    experience()
    free_text()
    seam_cycle("8. Unknown answer through the real cycle: the owner is asked",
               model(lambda item, packet: entry(item, unknown_reason="Not recorded.")))
    seam_cycle("8b. Model unavailable through the real cycle: the owner is asked, never a guess",
               model(error=OSError("no executor")))
    seam_cycle("8c. An attributed answer through the real cycle: the application completes",
               model(lambda item, packet: entry(
                   item, question_class="WORK_MODE_PREFERENCE", status="ANSWERED", value="Yes",
                   unknown_reason=None,
                   attribution=[{"pointer": "/policy/location_priority/0",
                                 "quote": "Springfield, IL hybrid/onsite"}])))
    print("No submission, browser call, Telegram send, provider request or live AI call occurred.")
