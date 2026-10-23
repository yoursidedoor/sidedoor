"""Owner-approved fixes of 2026-09-19, each checked against the behaviour it replaces.

1. Inspect evidence: a whitespace-only difference binds; one unobserved quote is dropped
   (and the field it alone supported is withdrawn) instead of discarding the whole read.
2. The 90-day history gate holds a job before any path can ask the owner a question.
3. A posting silent on authorization/clearance is not an unknown for a US role.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
from pathlib import Path
import unittest

import application_browser
import applications
from manual_application_browser import event
import test_applications_e2e as fixtures


OBSERVED_AT = "2026-09-19T10:00:00+00:00"
PAGE = "Synthetic Company\tSenior Engineer\tRequisition role-123\n\t\t191 checkbox Helsinki\n\t\t192 checkbox Kuopio"


def inspect(result, evidence, operation="inspect"):
    request = {"request_id": "synthetic", "binding": "synthetic-binding", "max_tool_calls": 40,
               "payload": {"operation": operation, "job": {"job_key": "synthetic company:role-123"}}, "context": {}}
    raw = {"request_id": "synthetic", "binding": "synthetic-binding", "result_json": json.dumps(result),
           "evidence": [{"pointer": pointer, "tool_call_id": "read", "quote": quote} for pointer, quote in evidence]}
    trace = (json.dumps(event("read", PAGE)) + "\n").encode()
    return application_browser.normalize(request, raw, trace, OBSERVED_AT, Path("synthetic-trace.jsonl"))


class InspectEvidenceBinding(unittest.TestCase):
    def test_tabs_read_back_as_spaces_still_bind(self):
        result = inspect({"partial_review": {"verified": False, "employer": "Synthetic Company",
                                             "facts": {"city": "Helsinki, Kuopio"}}},
                         [("/partial_review/employer", "Synthetic Company"),
                          ("/partial_review/facts/city", "191 checkbox Helsinki 192 checkbox Kuopio")])
        self.assertNotIn("rejected_evidence", result)
        self.assertEqual(result["partial_review"]["facts"]["city"], "Helsinki, Kuopio")

    def test_one_unobserved_identity_quote_is_recorded_and_the_read_stays_verified(self):
        # Owner, 2026-10-02: a rejected identity quote no longer withdraws the read's top-level
        # verification; 2026-10-07 (item 2): on inspect it no longer unbinds its field either.
        result = inspect({"verified": True, "employer": "Synthetic Company", "role": "Senior Engineer",
                          "requisition": "role-123", "official_url": "https://jobs.example/role-123",
                          "action_scope": "application_only"},
                         [("/employer", "Synthetic Company"), ("/role", "Senior Engineer"),
                          ("/requisition", "Requisition role-123"),
                          # The model's own navigation command, never returned by a tool.
                          ("/official_url", 'cua.getBrowser({ url: "https://jobs.example/role-123" })')])
        self.assertEqual(result["official_url"], "https://jobs.example/role-123")
        self.assertIs(result["verified"], True)
        self.assertEqual(result["employer"], "Synthetic Company")
        self.assertEqual([(entry["pointer"], entry["reason"]) for entry in result["rejected_evidence"]],
                         [("/official_url", "APPLICATION_UNOBSERVED_QUOTE")])
        self.assertNotIn("/official_url", {entry["pointer"] for entry in result["browser_evidence"]})

    def test_top_level_blocked_status_becomes_blocked(self):
        # Yoyodyne's 2026-09-30 shape: the stop at the top level, not in blocked.code.
        result = inspect({"status": "BLOCKED", "code": "IDENTITY_MISMATCH", "next_action": "Re-record under R302",
                          "requisition": "R302", "partial_review": {"facts": {"open": True}}}, [])
        self.assertEqual(result["blocked"], {"code": "IDENTITY_MISMATCH", "next_action": "Re-record under R302"})

    def test_string_blocked_and_a_wrapped_review_keep_their_stop(self):
        self.assertEqual(inspect({"blocked": "ROLE_CLOSED", "reason": "Closed"}, [])["blocked"],
                         {"code": "ROLE_CLOSED", "next_action": "Closed"})
        wrapped = inspect({"status": "BLOCKED", "review": {"verified": True, "facts": {"open": True}}}, [])
        self.assertEqual(wrapped["blocked"]["code"], "INSPECTION_INCOMPLETE")

    def test_a_stop_without_a_code_is_refused_outside_inspect(self):
        self.assertEqual(inspect({"blocked": {"next_action": "Read again"}}, [])["blocked"]["code"], "INSPECTION_INCOMPLETE")
        for operation in ("submit", "verify", "reconcile"):
            with self.assertRaisesRegex(ValueError, "APPLICATION_BLOCKER_INVALID"):
                inspect({"status": "BLOCKED", "next_action": "x"}, [], operation)

    def test_evidence_kind_needs_a_quote_unless_submitted(self):
        for status, pointer in (("NOT_SUBMITTED", "/evidence_kind"), ("SUBMITTED", "/status")):
            with self.assertRaises(ValueError) as refused:
                inspect({"evidence_kind": "employer_confirmation_email", "status": status}, [], "verify")
            self.assertEqual(getattr(refused.exception, "pointer", None), pointer)

    def test_words_inside_string_literals_are_not_browser_code(self):
        guard = application_browser.unsupported_browser_code
        # Kramerica Power's real fill (2026-09-30) and typical fill code: prose and URLs in strings.
        for code in ('await tab.click(71); await tab.typeText(71,"I rebuilt the nightly inventory reconciliation job as a streaming service and '
                     'introduced contract tests and a schema registry for the producer teams.\\n\\n2. Next");',
                     'await tab.typeText(52,"https://www.linkedin.com/in/someone/"); await tab.typeText(63,"10/28/2026");',
                     "await tab.typeText(3, 'We import, fetch and require data in a process'); await tab.click(4);"):
            self.assertFalse(guard(code), code)
        for code in (
                # round 1
                'const token = process.env.KEY', 'x = a++ / process.env.X / 2', 'x = {} / 1; process.exit() / 2',
                'y = a-- / 2; fetch(u) / 1', 'new Function("return process.env.KEY")()',
                '[].constructor.constructor("return fetch(1)")()', 'setTimeout("fetch(1)", 0)',
                '<!-- x\nprocess.env', 'x = 1 --> process.env', "#!`\nprocess.exit(); `'${`'`}`",
                # round 2: regex, comment and template tricks
                'x = /\\"/; process.exit(); //"', "x = /\\'/; process.exit(); //'", 'x = /\\`/; process.exit(); //`',
                'x = /\\//; process.exit()', 'x = /\\/*/; process.exit(); /*/ */', '`${/}/.test(process.exit())}`',
                's = `${"a".replace(/}/, fetch("u"))}`', '`${ "}" + process.exit() }`', 'a = b / c; // "\nprocess.exit()',
                'x = /[\\/]/; process.exit()',
                # round 2: running text as code
                'const s = "process.exit()"; setTimeout(s, 0)', 'setTimeout(("fetch(\'u\')"), 0)',
                'setTimeout("" + "fetch(1)", 0)', 'globalThis["Function"]("return process.env")()',
                'globalThis["setTimeout"]("fetch(1)")',
                'const e = document.createElement("script"); e.textContent = "fetch(1)"; document.body.append(e)',
                'location.href = "javascript:fetch(1)"', 'new Worker(URL.createObjectURL(new Blob(["fetch(1)"])))',
                'document.body.setAttribute("onclick", "fetch(1)"); document.body.click()', 'Function`return process`',
                # round 2: any `/` in code means the whole code is checked
                's.replace(/\\//g, 1); fetch(u)', 'r = /[/*]/; fetch(1); q = /a*/'):
            self.assertTrue(guard(code), code)
        self.assertIsNone(application_browser.code_outside_literals('u.split(/\\//)'))
        self.assertTrue(application_browser.investigation_writes('p = u.split(/\\//); await tab.fill(1)'))

    def test_all_null_compensation_is_recorded_as_not_published(self):
        review = {"compensation": {"currency": None, "minimum": None, "maximum": None, "period": None, "verified": None}}
        record = applications.compensation_assessment(review, {"minimum_base_salary_usd": 180000}, OBSERVED_AT)
        self.assertIn("SALARY_NOT_PUBLISHED", record["warnings"])
        self.assertIs(record["blocks"], False)


class HistoryGateBeforeOwnerQuestions(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job
    submissions = fixtures.ApplicationEndToEndTests.submissions

    def test_prior_application_history_holds_before_an_owner_hold(self):
        key = self.discover()
        with self.database:
            # This job's own recorded application. (The 90-day employer window this test used to
            # reach is gone, owner 2026-10-06: "we should never block employers unless the jobs
            # are the same"; the hold ORDER is what is tested.)
            self.database.execute("INSERT INTO application_history VALUES (?,?,?,?)",
                                  ("example ai", "role-123", "APPLICATION", "same-job evidence"))
            # Already held on a user-owned hold.
            self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker='AUTHENTICATION_REQUIRED' WHERE job_key=?", (key,))
        self.cycle()
        self.assertEqual((self.job(key)["state"], self.job(key)["blocker"]), ("DUPLICATE", "EXISTING_APPLICATION"))
        self.assertEqual(self.database.execute("SELECT count(*) FROM application_blockers WHERE job_key=? AND owner='user'",
                                               (key,)).fetchone()[0], 0)
        self.assertEqual(self.submissions(), [])


class SilentPostingEligibility(unittest.TestCase):
    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    cycle = fixtures.ApplicationEndToEndTests.cycle
    job = fixtures.ApplicationEndToEndTests.job
    submissions = fixtures.ApplicationEndToEndTests.submissions

    def test_silent_us_posting_proceeds_and_a_stated_citizenship_requirement_still_holds(self):
        def silent(result, job):
            result["facts"].update(authorization=None, citizenship_clearance_requirements_met=None,
                                   requirements_met=True, requirements_met_reason="Resume shows backend Python/AWS at senior level")
            result.update(browser_evidence=[], observed_browser_calls=1)
        self.adapter.customize = silent
        key = self.discover()
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        stored = json.loads(self.database.execute("SELECT evidence FROM application_reviews WHERE job_key=?", (key,)).fetchone()[0])
        self.assertEqual({field: value["basis"] for field, value in stored["eligibility_fact_basis"].items()},
                         {"authorization": "POSTING_SILENT_US_ROLE_OWNER_US_PERMIT",
                          "citizenship_clearance_requirements_met": "POSTING_SILENT_NO_CLEARANCE_REQUIREMENT",
                          "requirements_met": "AI_JUDGED_AGAINST_RESUME"})

        def citizens_only(result, job):
            silent(result, job)
            result["facts"]["citizenship_clearance_requirements_met"] = False
            result["browser_evidence"] = [{"pointer": "/facts/citizenship_clearance_requirements_met",
                                           "tool_call_id": "read", "quote": "US citizens only"}]
        self.adapter.customize = citizens_only
        other = self.discover(fixtures.role("Other AI", "role-456"))
        self.cycle()
        self.assertEqual(self.job(other)["blocker"], "CLEARANCE_OR_CITIZENSHIP_REQUIRED")


if __name__ == "__main__":
    unittest.main()
