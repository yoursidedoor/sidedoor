"""Research's INELIGIBLE verdict closes the job on the apply side too (owner, 2026-10-25).

AuxoAI: research judged an India-only role outside the search, outreach skipped it, but the job
was queued to apply, read five times (CAPTCHA, an unclickable button) and parked as an
investigation failure. The apply side now has the matching check: such a job is closed as not
eligible, with research's words, before any browser read; one already queued or parked closes on
the next cycle. A role research judged in scope is applied to as before.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest

import applications
import applications_page
import test_applications_e2e as e2e

INDIA = "All listed work locations are in India, outside the policy's US-only country allowance."


def ruled_out(company="Auxo Example", requisition="role-777"):
    return dict(e2e.role(company, requisition), eligibility="INELIGIBLE",
                policy_fit={"funding_stage": {"in_scope": True, "reason": "Any stage is allowed."},
                            "location": {"in_scope": False, "reason": INDIA}},
                assessment=["INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE", "AI judged location out of scope: research policy_fit: " + INDIA])


class ResearchVerdictApplyTests(unittest.TestCase):
    def setUp(self):
        self.case = e2e.ApplicationEndToEndTests(next(name for name in dir(e2e.ApplicationEndToEndTests)
                                                       if name.startswith("test_")))
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.database, self.adapter = self.case.database, self.case.adapter
        self.discover, self.cycle = self.case.discover, self.case.cycle

    def job(self, key):
        return self.database.execute("SELECT state,blocker,next_action FROM application_jobs WHERE job_key=?", (key,)).fetchone()

    def test_new_research_ineligible_role_closes_before_any_read(self):
        key = self.discover(ruled_out())
        self.assertEqual(tuple(self.job(key))[:2], ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))
        self.assertIn(INDIA, self.job(key)["next_action"])
        self.cycle()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.job(key)["state"], "INELIGIBLE")

    def test_already_parked_job_closes_on_next_cycle(self):
        # Queued before this check existed and parked where the ordinary loop never looks.
        payload = ruled_out()
        employer, requisition, key = applications.identity(payload)
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                                  "VALUES (?,?,?,?,'BLOCKED','APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED',?)",
                                  (key, employer, requisition, json.dumps(payload), e2e.NOW))
        self.cycle()
        self.assertEqual(self.job(key)["state"], "INELIGIBLE")
        self.assertEqual(self.adapter.calls, [])
        reason = applications_page.closed_reason(applications_page.overview.Reader(self.database), key,
                                                 "INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE")
        self.assertEqual(reason, "Research judged: " + INDIA)

    def test_in_flight_submission_is_left_alone(self):
        payload = ruled_out()
        employer, requisition, key = applications.identity(payload)
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) "
                                  "VALUES (?,?,?,?,'RECONCILE',?)", (key, employer, requisition, json.dumps(payload), e2e.NOW))
            closed = applications.close_research_ineligible(self.database, e2e.NOW)
        self.assertEqual(closed, [])
        self.assertEqual(self.job(key)["state"], "RECONCILE")

    def test_possible_submission_is_left_alone(self):
        payload = ruled_out()
        employer, requisition, key = applications.identity(payload)
        with self.database:
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                                  "VALUES (?,?,?,?,'BLOCKED','SUBMISSION_OUTCOME_UNKNOWN',?)",
                                  (key, employer, requisition, json.dumps(payload), e2e.NOW))
            closed = applications.close_research_ineligible(self.database, e2e.NOW)
        self.assertEqual(closed, [])
        self.assertEqual(tuple(self.job(key))[:2], ("BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN"))

    def test_in_scope_role_is_applied_to_as_before(self):
        key = self.discover(dict(e2e.role(), eligibility="ELIGIBLE"))
        self.cycle()
        self.assertEqual(self.job(key)["state"], "SUBMITTED")


if __name__ == "__main__":
    unittest.main()
