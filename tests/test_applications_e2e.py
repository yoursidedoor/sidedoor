import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import applications
import decision_transport
import notifications
import orchestrator
from fake_codex import opportunity as research_opportunity
import profile_home
import fake_executables
import user_profile


NOW = "2026-09-17T16:00:00+00:00"
PIPELINE = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}


def role(company="Example AI", requisition="role-123"):
    return {"company": company, "role": "Staff Software Engineer", "url": "https://jobs.ashbyhq.com/example/" + requisition,
            "rationale": "MOCK FIXTURE ONLY", "requisition_id": requisition}


def evidence(now=NOW):
    return {"source": "mock-independent-official-provider", "verified": True, "observed_at": now}


def review(job, now=NOW):
    return dict(evidence(now), employer=job["employer"], requisition=job["requisition"],
                official_url=json.loads(job["payload"])["url"], role=json.loads(job["payload"])["role"],
                candidate_email=job["candidate_email"],
                official_destination_verified=True, action_scope="application_only",
                employee_count_evidence=dict(evidence(now), value="51–200"),
                facts={"open": True, "funding_stage": "Series B", "industry": "AI", "level": "Staff Software Engineer",
                       "country": "US", "city": "Seattle", "work_mode": "remote", "specialization": "backend", "requirements_met": True,
                       "authorization": "US_PERMANENT_RESIDENT_ACCEPTED", "hiring_geography_includes_candidate": True,
                       "citizenship_clearance_requirements_met": True},
                scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
                compensation=dict(evidence(now), currency="USD", minimum=200000, maximum=250000, period="annual_base"),
                history=dict(evidence(now), job_key=job["job_key"], application=False, interview=False, referral="NONE"),
                questions=[{"id": "email", "semantic": "contact_email", "required": True}],
                all_required_questions_enumerated=True, form_revision="mock-form-v1")


class MockATS:
    def __init__(self, root):
        self.root = root
        self.now = NOW
        self.calls = []
        self.customize = lambda result, job: None
        self.inspect_failures = 0
        self.timeout_after_submit = False
        self.lose_submission = False
        self.unknown_reconcile = False
        self.access_blocked_reconcile = False

    def call(self, operation, payload):
        self.calls.append((operation, payload))
        job = payload["job"]
        receipt_path = self.root / (hashlib.sha256(job["job_key"].encode()).hexdigest() + ".receipt")
        if operation == "inspect":
            if self.inspect_failures:
                self.inspect_failures -= 1
                raise ConnectionError("mock preflight outage, no submission")
            result = review(job, self.now)
            self.customize(result, job)
            return result
        if operation == "submit":
            receipt = dict(evidence(self.now), status="SUBMITTED", independent_read=True,
                           employer=job["employer"], requisition=job["requisition"], candidate_email=job["candidate_email"],
                           receipt_id="mock-confirmation-" + job["requisition"])
            if not self.lose_submission:
                if receipt_path.exists():
                    raise AssertionError("Duplicate provider submission")
                receipt_path.write_text(json.dumps(receipt))
            if self.timeout_after_submit:
                raise TimeoutError("mock timeout after provider may have accepted")
            return {"acknowledged": True}
        if operation == "reconcile" and self.access_blocked_reconcile:
            return {"status": "UNKNOWN", "blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Owner ticks the check"}}
        if operation in {"verify", "reconcile"}:
            if self.unknown_reconcile:
                return {"status": "UNKNOWN"}
            if receipt_path.exists():
                result = json.loads(receipt_path.read_text())
                result["observed_at"] = self.now
                return result
            return dict(evidence(self.now), status="NOT_SUBMITTED", authoritative_absence=True, no_inflight=True, job_key=job["job_key"])
        raise AssertionError(operation)


class MockTelegram:
    def __init__(self, failures=None):
        self.failures = list(failures or [])
        self.messages = []

    def send(self, message):
        if self.failures:
            raise self.failures.pop(0)
        self.messages.append(message)
        return {"provider": "telegram", "chat_id": "mock-owner", "message_id": len(self.messages)}


class MockBridge:
    def __init__(self):
        self.events = []
        self.acked = set()
        self.lose_ack_once = False

    def call(self, operation, body=None):
        if operation == "decisions":
            return {"id": body["id"], "state": "pending", "telegram_message_id": body.get("telegram_message_id")}
        if operation == "acks":
            self.acked.update(body["event_ids"])
            if self.lose_ack_once:
                self.lose_ack_once = False
                raise TimeoutError("ack persisted but response lost")
            return {"acknowledged": body["event_ids"]}
        cursor = int(operation.split("after=")[1].split("&")[0])
        events = [event for event in self.events if event["seq"] > cursor and event["seq"] not in self.acked]
        return {"events": events, "next_cursor": events[-1]["seq"] if events else cursor}


class ApplicationEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ["state", "reports", "logs"]:
            (self.root / folder).mkdir()
        self.database = orchestrator.connect(self.root)
        self.addCleanup(lambda: self.database.close())
        applications.initialize(self.database)
        self.policy = user_profile.load()
        (self.root / "mock-resume.pdf").write_bytes(b"mock resume; never uploaded anywhere")
        self.policy["resume"] = {"path": "mock-resume.pdf", "sha256": hashlib.sha256((self.root / "mock-resume.pdf").read_bytes()).hexdigest()}
        if "resume" in self.policy["answers"]:
            # The recorded resume answer is a pointer to `policy["resume"]["path"]` and
            # `resolve_answers` refuses any other value, so the fixture's substitute resume
            # has to move both together.
            self.policy["answers"]["resume"]["value"] = self.policy["resume"]["path"]
        profile_home.use(self, self.policy)
        self.adapter = MockATS(self.root)
        self.network = patch("socket.create_connection", side_effect=AssertionError("Tests must never access network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def discover(self, opportunity=None):
        opportunity = opportunity or role()
        orchestrator.store_research(self.database, PIPELINE, {"summary": "Mock research", "blockers": [], "opportunities": [opportunity], "drafts": []})
        return applications.identity(opportunity)[2]

    def cycle(self, seconds=0):
        self.adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()
        result = applications.cycle(self.database, {}, self.root, self.adapter, self.adapter.now)
        # A cycle must leave no open transaction on the shared connection. Every write here is
        # meant to sit inside `with database:`; one that does not leaves an implicit transaction
        # that the NEXT job inherits, and the next per-job `BEGIN IMMEDIATE` dies with
        # "cannot start a transaction within a transaction". That cost rekall, dinoco and
        # example aerospace a cycle each on 2026-09-24, from a zero-credit browser settle whose commit was conditional. Asserted
        # on the shared helper so every scenario that calls it checks the invariant.
        assert not self.database.in_transaction, "cycle left an open transaction"
        return result

    def job(self, key=None):
        return self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (key or applications.identity(role())[2],)).fetchone()

    def submissions(self):
        return [payload for operation, payload in self.adapter.calls if operation == "submit"]

    def test_01_discovery_worker_receipt_tracker_report(self):
        key = self.discover()
        result = self.cycle()
        self.assertEqual([operation for operation, payload in self.adapter.calls], ["inspect", "submit", "verify"])
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(result["verified_submissions"], 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_history").fetchone()[0], 1)
        self.assertEqual([row[0] for row in self.database.execute("SELECT event FROM application_events WHERE job_key=?", (key,))], ["DISCOVERED", "ELIGIBILITY_CHECKED", "APPLICATION_READ_COMPLETE", "HISTORY_ANSWERS_RESUME_CLEARED", "WORKER_SUBMISSION_STARTED", "SUBMISSION_VERIFIED"])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_02_prior_application_or_interview_prevents_duplicate(self):
        for index, kind in enumerate(["APPLICATION", "INTERVIEW"]):
            candidate = role("Company " + kind, str(index))
            employer, requisition, key = applications.identity(candidate)
            with self.database:
                # Recorded under this job's own requisition: only the same job is a duplicate
                # (owner, 2026-10-06: "we should never block employers unless the jobs are the same").
                self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)", (employer, requisition, kind, "historical receipt"))
            self.discover(candidate)
            self.cycle()
            self.assertEqual(self.job(key)["state"], "DUPLICATE")
        self.assertEqual(self.submissions(), [])

    def test_03_pending_referral_does_not_hold_application(self):
        # Referrals are tracked independently (owner, 2026-09-25) and never hold a job.
        self.adapter.customize = lambda result, job: result["history"].update(referral="PENDING")
        self.discover()
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)

    def test_04_unknown_eligibility_or_answers_never_invented(self):
        first = self.discover(role("Unknown eligibility", "one"))
        second = self.discover(role("Missing answer", "two"))
        def customize(result, job):
            if job["employer"] == "unknown eligibility":
                result["facts"]["authorization"] = "UNKNOWN"
            else:
                result["questions"].append({"id": "citizenship", "semantic": "citizenship", "required": True})
        self.adapter.customize = customize
        self.cycle()
        self.assertEqual(self.job(first)["blocker"], "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertIn("ANSWERING_RETRY", self.job(second)["blocker"])
        self.assertEqual(self.submissions(), [])

    def test_05_transient_retries_bounded_and_backed_off(self):
        self.discover()
        self.adapter.inspect_failures = 9
        self.cycle()
        self.assertEqual(self.job()["state"], "RETRY_WAIT")
        self.cycle(899)
        self.assertEqual(self.job()["attempts"], 1)
        for seconds in [900, 4500, 9000]:
            self.cycle(seconds)
        self.assertEqual(self.job()["state"], "EXHAUSTED")
        self.assertEqual(self.job()["attempts"], 3)
        self.assertEqual(len(self.adapter.calls), 3)

    def test_06_ambiguous_submit_reconciled_not_repeated(self):
        self.discover()
        self.adapter.timeout_after_submit = True
        self.cycle()
        self.assertEqual(self.job()["state"], "RECONCILE")
        self.cycle(1)
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.adapter.calls[-1][0], "reconcile")

    def test_07_blocked_item_does_not_stop_other_application(self):
        first = self.discover(role("Blocked company", "one"))
        second = self.discover(role("Eligible company", "two"))
        self.adapter.customize = lambda result, job: result["facts"].update(authorization="UNKNOWN") if job["employer"] == "blocked company" else None
        self.cycle()
        self.assertEqual(self.job(first)["state"], "BLOCKED")
        self.assertEqual(self.job(second)["state"], "SUBMITTED")

    def test_08_preapproved_without_prompts_saves_exact_answers(self):
        self.discover()
        with patch("builtins.input", side_effect=AssertionError("Routine approvals forbidden")):
            self.cycle()
        submitted = self.submissions()[0]
        # Answers are keyed by applications.question_field - derived from what the question
        # IS, never from the id the read reported (DU, owner 2026-09-21).
        email_field = applications.question_field({"id": "email", "semantic": "contact_email", "required": True})
        self.assertEqual(submitted["answers"], {email_field: self.policy["answers"]["email"]["value"]})
        self.assertTrue(submitted["no_extra_answers"])
        saved = json.loads(self.database.execute("SELECT evidence FROM application_attempts").fetchone()[0])
        self.assertEqual(saved["answers"], submitted["answers"])
        self.assertEqual(saved["resume_sha256"], self.policy["resume"]["sha256"])


    def test_10_restart_preserves_queue_and_duplicate_protection(self):
        self.discover()
        self.adapter.timeout_after_submit = True
        self.cycle()
        self.database.close()
        self.database = orchestrator.connect(self.root)
        applications.initialize(self.database)
        self.discover(dict(role(), url=role()["url"] + "?utm_source=duplicate"))
        self.cycle(1)
        self.cycle(2)
        self.assertEqual(self.job()["state"], "SUBMITTED")
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)

    def test_confirmed_absence_allows_retry_only_after_backoff(self):
        self.discover()
        self.adapter.timeout_after_submit = True
        self.adapter.lose_submission = True
        self.cycle()
        self.cycle(1)
        self.assertEqual(self.job()["state"], "RETRY_WAIT")
        self.adapter.timeout_after_submit = False
        self.adapter.lose_submission = False
        self.cycle(900)
        self.assertEqual(len(self.submissions()), 1)
        self.cycle(901)
        self.assertEqual(self.job()["state"], "SUBMITTED")

    def test_unknown_receipt_stops_after_ten_reconciliations(self):
        # Owner, 2026-09-30: "Uncertain submission checks should have a 10 retry limit (with
        # retry backoff)". Three fast reads, then widening waits, then held for the owner.
        self.discover()
        self.adapter.timeout_after_submit = True
        self.adapter.unknown_reconcile = True
        config = {"application_recovery": {"enabled": True}}

        def cycle(seconds):
            self.adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=seconds)).isoformat()
            applications.cycle(self.database, config, self.root, self.adapter, self.adapter.now)

        for seconds in [0, 1, 901, 1801, 2701, 3601]:
            cycle(seconds)
        self.assertEqual(self.job()["reconcile_attempts"], 3)
        self.assertEqual(self.job()["state"], "RECONCILE")
        cycle(3601 + 15 * 3600)  # before the 16h wait: no read
        self.assertEqual(self.job()["reconcile_attempts"], 3)
        for day in range(1, 60):
            cycle(day * 86400)
        self.assertEqual(self.job()["reconcile_attempts"], 10)
        self.assertEqual(self.job()["state"], "BLOCKED")
        self.assertEqual(self.job()["blocker"], "SUBMISSION_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.submissions()), 1)

    def test_access_blocked_reconcile_reads_count_toward_the_ten(self):
        self.discover()
        self.adapter.timeout_after_submit = True
        self.adapter.unknown_reconcile = True
        self.adapter.access_blocked_reconcile = True
        config = {"application_recovery": {"enabled": True}}
        for day in range(0, 60):
            self.adapter.now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(days=day)).isoformat()
            applications.cycle(self.database, config, self.root, self.adapter, self.adapter.now)
        self.assertEqual(self.job()["reconcile_attempts"], 10)
        self.assertEqual(self.job()["state"], "BLOCKED")
        self.assertEqual(sum(operation == "reconcile" for operation, _ in self.adapter.calls), 10)
        self.assertEqual(len(self.submissions()), 1)

    def test_resume_tampering_blocks_submission(self):
        self.discover()
        (self.root / "mock-resume.pdf").write_bytes(b"replacement not designated")
        self.cycle()
        self.assertEqual(self.job()["blocker"], "DESIGNATED_RESUME_MISMATCH")
        self.assertEqual(self.submissions(), [])

    def test_crossing_salary_band_is_recorded_not_blocked(self):
        # This test changed meaning deliberately. It used to assert that a band straddling
        # the owner's recorded minimum is BLOCKED on CROSSING_BASE_BAND_UNRESOLVED. The
        # owner has since asked for salary not to be strict, and the blocker's own message
        # already said a crossing band "proves neither eligibility nor ineligibility" - so
        # it is now recorded as a warning on the stored review and the role proceeds. The
        # warning code itself survives, and a verified candidate target still resolves it.
        self.discover()
        self.adapter.customize = lambda result, job: result["compensation"].update(minimum=150000, maximum=220000)
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        saved = json.loads(self.database.execute("SELECT evidence FROM application_reviews ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertIn("CROSSING_BASE_BAND_UNRESOLVED", saved["compensation_assessment"]["warnings"])
        self.assertEqual(saved["compensation_assessment"]["stated"]["minimum"], 150000)
        self.assertEqual(saved["compensation_assessment"]["stated"]["maximum"], 220000)

    def test_verified_candidate_target_resolves_a_crossing_band(self):
        self.discover()
        self.adapter.customize = lambda result, job: result["compensation"].update(minimum=150000, maximum=220000, target=200000, target_verified_for_candidate=True)
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        saved = json.loads(self.database.execute("SELECT evidence FROM application_reviews ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        record = saved["compensation_assessment"]
        self.assertEqual(record["warnings"], [])
        self.assertEqual(record["comparable_kind"], "verified candidate-specific target")
        self.assertEqual(record["comparable_usd"], 200000)

    def test_a_known_usd_band_far_below_the_relaxed_floor_is_still_refused(self):
        # "Relax a bit" is not "ignore entirely". A role whose stated USD band tops out at
        # 95000 against a recorded 180000 minimum is refused - and named, so the owner can
        # see it was the pay and not the industry, level or location.
        self.discover()
        self.adapter.customize = lambda result, job: result["compensation"].update(minimum=80000, maximum=95000)
        self.cycle()
        self.assertEqual(self.job()["state"], "INELIGIBLE")
        self.assertEqual(self.job()["blocker"], "BASE_SALARY_BELOW_RELAXED_FLOOR")
        self.assertEqual(self.submissions(), [])
        saved = json.loads(self.database.execute("SELECT evidence FROM application_reviews ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(saved["compensation_assessment"]["stated"]["maximum"], 95000)

    def test_a_posting_that_publishes_no_salary_is_applied_to_and_recorded_as_unknown(self):
        self.discover()
        self.adapter.customize = lambda result, job: result.pop("compensation", None)
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        saved = json.loads(self.database.execute("SELECT evidence FROM application_reviews ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        record = saved["compensation_assessment"]
        self.assertIsNone(record["stated"])
        self.assertFalse(record["stated_by_employer"])
        self.assertIn("SALARY_NOT_PUBLISHED", record["warnings"])
        self.assertIsNone(record["usd_equivalent"])

    def test_alias_keeps_canonical_employer_requisition(self):
        key = self.discover()
        alias = role("Example AI Incorporated", "external-456")
        with self.database:
            applications.add_alias(self.database, alias, key, evidence())
            self.assertEqual(applications.ingest(self.database, alias), key)
        self.cycle()
        self.assertEqual(len(self.submissions()), 1)

    def test_same_ats_posting_with_different_company_spelling_deduplicates(self):
        self.discover()
        self.discover(role("Example AI Incorporated"))
        self.cycle()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 1)
        self.assertEqual(len(self.submissions()), 1)

    def test_geography_and_scope_violations_fail_closed(self):
        # The fourth case changed meaning deliberately. It used to be `country="DE",
        # region="Europe"`, asserting that a European role is blocked for want of verified
        # country authorization - which is exactly the gate the owner's instruction
        # contradicts, since they hold no EU authorization and want EU roles pursued anyway
        # so an employer can sponsor. A European role being pursued is now correct and is
        # covered in `tests/test_eligibility_and_answers.py`. What must still fail closed is
        # a European role whose employer was observed saying it cannot sponsor, and a
        # European role that is not remote; both replace it here.
        # Since 2026-10-07 the onsite exclusions are the read's scope_fit.location verdict and a
        # sponsorship refusal is a structured false flag, not matched free text.
        def excluded(result, city, mode):
            result["facts"].update(work_mode=mode, city=city)
            result.setdefault("scope_fit", {})["location"] = {"in_scope": False, "reason": city + " is excluded"}
        cases = [
            lambda result: excluded(result, "Seattle", "onsite"),
            lambda result: result["facts"].update(hiring_geography_includes_candidate=False),
            lambda result: result["facts"].update(country="DE", region="Europe", city="Berlin",
                                                  authorization="COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED",
                                                  employer_sponsorship=False),
            # Europe onsite/hybrid is now in scope (owner decision 2026-09-19: willing to
            # relocate); a country outside the owner's locations must still fail closed.
            lambda result: result["facts"].update(country="CA", region="North America", city="Toronto",
                                                  work_mode="onsite",
                                                  authorization="COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED"),
            lambda result: excluded(result, "Bellevue", "hybrid"),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                candidate = role("Restricted " + str(index), str(index))
                key = self.discover(candidate)
                self.adapter.customize = lambda result, job: mutate(result)
                self.cycle()
                self.assertIn(self.job(key)["state"], {"BLOCKED", "INELIGIBLE"})
        self.assertEqual(self.submissions(), [])

    def test_non_usd_base_proceeds_without_fx_and_keeps_the_original_band(self):
        # This test changed meaning deliberately. It used to assert that a non-USD band
        # without dated FX evidence is BLOCKED on SALARY_UNKNOWN. Nothing on the
        # application path has ever supplied that evidence - `result_contract("inspect")`
        # has no fx field - so the assertion encoded an unconditional refusal of every
        # EUR/GBP posting, which is exactly what the owner asked to be relaxed. What must
        # still hold is that the employer's own figures survive verbatim and that no
        # invented rate is presented as verified.
        self.discover()
        self.adapter.customize = lambda result, job: result["compensation"].update(currency="CAD", minimum=250000, maximum=300000)
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        saved = json.loads(self.database.execute("SELECT evidence FROM application_attempts").fetchone()[0])
        self.assertEqual(saved["review"]["compensation"]["currency"], "CAD")
        self.assertEqual(saved["review"]["compensation"]["minimum"], 250000)
        record = saved["review"]["compensation_assessment"]
        self.assertEqual(record["basis"], "INDICATIVE_REFERENCE_RATE")
        self.assertIn("NON_USD_WITHOUT_DATED_FX_EVIDENCE", record["warnings"])
        self.assertTrue(record["usd_equivalent"]["approximate"])
        self.assertFalse(record["usd_equivalent"]["verified"])

    def test_dated_fx_evidence_is_still_used_exactly_when_the_read_supplies_it(self):
        self.discover()
        self.adapter.customize = lambda result, job: result["compensation"].update(currency="CAD", minimum=250000, maximum=300000, fx=dict(evidence(self.adapter.now), usd_per_unit=0.8))
        self.cycle()
        self.assertEqual(self.job()["state"], "SUBMITTED")
        saved = json.loads(self.database.execute("SELECT evidence FROM application_attempts").fetchone()[0])
        record = saved["review"]["compensation_assessment"]
        self.assertEqual(record["basis"], "DATED_FX_EVIDENCE")
        self.assertFalse(record["usd_equivalent"]["approximate"])
        self.assertEqual(record["comparable_usd"], 240000)
        self.assertEqual(saved["review"]["compensation"]["currency"], "CAD")

    def test_service_lock_prevents_overlapping_process(self):
        with orchestrator.single_instance(self.root) as acquired:
            self.assertTrue(acquired)
            command = [sys.executable, "-c", "from pathlib import Path; import orchestrator;\nwith orchestrator.single_instance(Path(__import__('sys').argv[1])) as acquired: print(acquired)", str(self.root)]
            result = subprocess.run(command, cwd=orchestrator.ROOT, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "False")

    def test_notification_uncertainty_never_silently_retried(self):
        with self.database:
            applications.notify(self.database, "test", "Harmless mock", NOW)
        sender = MockTelegram([TimeoutError()])
        notifications.dispatch(self.database, {}, sender, NOW)
        notifications.dispatch(self.database, {}, sender, "2026-09-18T16:00:00+00:00")
        self.assertEqual(self.database.execute("SELECT state FROM notification_outbox").fetchone()[0], "UNCERTAIN")
        self.assertEqual(sender.messages, [])

    def test_notification_retries_bounded_and_no_credentials_pending(self):
        with self.database:
            applications.notify(self.database, "test", "Harmless mock", NOW)
        notifications.dispatch(self.database, {}, MockTelegram([notifications.Unavailable()]), NOW)
        self.assertEqual(tuple(self.database.execute("SELECT state,attempts FROM notification_outbox").fetchone()), ("PENDING", 0))
        sender = MockTelegram([notifications.Transient()] * 5)
        for day in range(18, 24):
            notifications.dispatch(self.database, {}, sender, f"2026-09-{day}T16:00:00+00:00")
        self.assertEqual(tuple(self.database.execute("SELECT state,attempts FROM notification_outbox").fetchone()), ("FAILED", 5))

    def test_subprocess_research_to_command_worker_independent_receipt_and_report(self):
        executable = fake_executables.copy(self.root / "mock-codex", Path(__file__).parent / "fake_codex.py")
        (self.root / "research-prompt.md").write_text("Isolated mock research")
        for filename in ("discovery-prompt.md", "discovery-schema.json", "research-schema.json"):
            shutil.copyfile(orchestrator.ROOT / filename, self.root / filename)
        self.policy["industries"] = ["AI"]
        profile_home.use(self, self.policy)
        (self.root / ".mock-only").touch()
        config = {"timezone": "America/Los_Angeles", "research_hour": 8, "research_max_attempts": 3,
                  "research_retry_delays_seconds": [900, 3600], "research_timeout_seconds": 10,
                  "model": "mock", "codex": str(executable), "applications_enabled": True, "execution_ownership_verified": True,
                  "research_company_candidates": ["Example AI"],
                  "application_adapter_command": [sys.executable, str(Path(__file__).parent / "mock_ats_process.py"), str(self.root)]}
        candidate = research_opportunity("Example AI", "AI", orchestrator.utcnow())
        # The policy's résumé is a mock file, not a PDF, so research is not given its claims.
        with patch.dict(os.environ, {"JOB_SEARCH_FIXTURE_OPPORTUNITY": json.dumps(candidate),
                                     "JOB_SEARCH_FIXTURE_STATUS": "COMPLETE"}), \
                patch.object(orchestrator.research_workflow, "seed_designated_claims",
                             return_value={"state": "DESIGNATED_RESUME_EXTRACTION_UNAVAILABLE"}):
            result = orchestrator.research(self.root, config, self.database, PIPELINE, {})
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(self.job(applications.identity(candidate)[2])["state"], "QUEUED")
        report = orchestrator.application_cycle(self.root, config, self.database)
        self.assertEqual(report["verified_submissions"], 1)
        saved = json.loads((self.root / "reports/applications.json").read_text())
        self.assertTrue(saved["drafts_are_not_submissions"])
        self.assertEqual(saved["applications"][0]["state"], "SUBMITTED")
        self.assertEqual(saved["notifications"][0]["state"], "PENDING")
