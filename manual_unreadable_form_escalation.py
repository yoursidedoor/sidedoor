"""Read-only rehearsal: what an owner actually sees when a form cannot be enumerated.

Runs entirely in a throwaway temp root with a mock adapter. No network, no browser, no
Telegram, no provider, no submission, and it never touches the live database. It prints
the same observable facts for ROOT and for the patched module, so the difference is not a
matter of reading a diff: run it with the two versions of `applications.py` in place.

    /opt/homebrew/bin/python3.13 manual_unreadable_form_escalation.py
"""

import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile

import applications
import orchestrator
import user_profile


NOW = "2026-09-18T09:00:00+00:00"
CYCLES = 12
RECOVERY = {"application_recovery": {"enabled": True, "max_attempts": 3, "deadline_hours": 48}}

ROLE = {"company": "Readable Systems", "role": "Staff Software Engineer", "requisition_id": "role-901",
        "url": "https://jobs.ashbyhq.com/readable/role-901", "rationale": "MOCK FIXTURE ONLY"}


def evidence(now):
    return {"source": "mock-independent-official-provider", "verified": True, "observed_at": now}


class Adapter:
    """Reads the official posting fine; the application form never enumerates."""

    can_submit = True

    def __init__(self):
        self.now = NOW
        self.recovery_decisions = 0

    def call(self, operation, payload):
        job = payload["job"]
        if operation != "inspect":
            raise AssertionError("This rehearsal never submits, verifies or reconciles: " + operation)
        payload = json.loads(job["payload"])
        return dict(evidence(self.now), employer=job["employer"], requisition=job["requisition"],
                    official_url=payload["url"], role=payload["role"], candidate_email=job["candidate_email"],
                    official_destination_verified=True, action_scope="application_only",
                    employee_count_evidence=dict(evidence(self.now), value="51-200"),
                    facts={"open": True, "funding_stage": "Series B", "industry": "AI",
                           "level": "Staff Software Engineer", "country": "US", "city": "Austin",
                           "work_mode": "remote", "specialization": "backend", "requirements_met": True,
                           "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                           "hiring_geography_includes_candidate": True,
                           "citizenship_clearance_requirements_met": True},
                    scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
                    compensation=dict(evidence(self.now), currency="USD", minimum=220000, maximum=260000,
                                      period="annual_base"),
                    history=dict(evidence(self.now), job_key=job["job_key"], application=False,
                                 interview=False, referral="NONE"),
                    questions=None, all_required_questions_enumerated=False, form_revision=None)

    def recovery_decision(self, packet):
        self.recovery_decisions += 1
        return {"action": "RETRY", "reason": "mock", "progress": False, "method": "official_posting",
                "instructions": "mock instruction " + str(self.recovery_decisions)}


def rehearse(config, label):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for folder in ["state", "reports", "logs"]:
            (root / folder).mkdir()
        policy = user_profile.load()
        (root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        policy["resume"] = {"path": "mock-resume.pdf",
                            "sha256": hashlib.sha256((root / "mock-resume.pdf").read_bytes()).hexdigest()}
        user_profile.use_policy(policy, root / "profile-home")
        database = orchestrator.connect(root)
        try:
            applications.initialize(database)
            key = applications.ingest(database, ROLE)
            database.commit()
            adapter = Adapter()
            timeline = []
            for index in range(CYCLES):
                adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=index * 7200)).isoformat()
                applications.cycle(database, config, root, adapter, adapter.now)
                job = database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (key,)).fetchone()
                owner = database.execute("SELECT owner FROM application_blockers WHERE job_key=?", (key,)).fetchone()
                asked = database.execute("SELECT COUNT(*) FROM user_decisions WHERE job_key=? AND state='PENDING'",
                                         (key,)).fetchone()[0]
                timeline.append((index + 1, job["state"], job["blocker"], owner[0] if owner else None, asked))
            print(f"--- {label} ---")
            print(f"{'cycle':>5} {'state':<10} {'blocker':<52} {'owner':<13} outstanding questions")
            for entry in timeline:
                print(f"{entry[0]:>5} {entry[1]:<10} {str(entry[2]):<52} {str(entry[3]):<13} {entry[4]}")
            asked = [dict(row) for row in database.execute(
                "SELECT field,state,summary FROM user_decisions WHERE job_key=?", (key,))]
            print(f"questions ever asked: {len(asked)}")
            for entry in asked:
                print(f"  field={entry['field']} state={entry['state']}")
                print(f"  message: {entry['summary']}")
            print(f"recovery investigation claims spent: {adapter.recovery_decisions}")
            print(f"submission attempt rows: {database.execute('SELECT COUNT(*) FROM application_attempts').fetchone()[0]}")
            print()
        finally:
            database.close()


if __name__ == "__main__":
    rehearse({}, f"{CYCLES} cycles, recovery disabled")
    rehearse(RECOVERY, f"{CYCLES} cycles, installed production recovery settings")
