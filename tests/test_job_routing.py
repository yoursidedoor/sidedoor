"""`applications.job_route` is the one authority for where a held job goes next.

Before 2026-09-22 `process` made that decision in three places, each reading a different half
of the signal, so no single place knew a job's destination and three subsystems could bounce
it between them. These tests pin the properties that make one function safe to rely on.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import applications
import application_recovery


class JobRouteTests(unittest.TestCase):
    def test_every_answer_is_a_declared_route(self):
        """Total: no input produces something `process` cannot dispatch on."""
        codes = (sorted(application_recovery.RECOVERABLE) + sorted(application_recovery.USER_HOLDS)
                 + ["SUBMISSION_OUTCOME_UNKNOWN", "ANSWERING_RETRY:x", "UNKNOWN_REQUIRED_ANSWERS:y",
                    "SCOPE_REVIEW_REQUIRED", "FILE_UPLOAD_FAILED", "", None])
        states = ["QUEUED", "BLOCKED", "RETRY_WAIT", "REFERRAL_HOLD", "SUBMITTING", "RECONCILE",
                  "WAITING_BROWSER"]
        for code in codes:
            for state in states:
                for outstanding in (False, True):
                    with self.subTest(code=code, state=state, outstanding=outstanding):
                        self.assertIn(applications.job_route(code, state, outstanding),
                                      applications.ROUTES)

    def test_an_outstanding_submission_always_reconciles_first(self):
        """A job that may already have reached an employer establishes that before anything
        else, whatever its blocker says. This is the double-submission protection."""
        for code in sorted(application_recovery.RECOVERABLE) + sorted(application_recovery.USER_HOLDS):
            with self.subTest(code=code):
                self.assertEqual(applications.job_route(code, "BLOCKED", True), "RECONCILE")

    def test_the_uncertain_states_always_reconcile(self):
        for state in sorted(applications.UNCERTAIN):
            self.assertEqual(applications.job_route("ANYTHING", state), "RECONCILE")

    def test_no_code_is_both_recoverable_and_an_owner_hold(self):
        """A code in both sets would have no single destination - the ambiguity this function
        exists to remove."""
        overlap = application_recovery.RECOVERABLE & application_recovery.USER_HOLDS
        self.assertEqual(overlap, frozenset(), "codes in both sets: %s" % sorted(overlap))

    def test_state_is_what_disambiguates_a_code_used_by_two_paths(self):
        """The survey of every call site found codes that legitimately route differently by
        where they were set: AUTHENTICATION_REQUIRED is RECONCILE from the reconcile path and
        ORDINARY from the review path (OWNER before 2026-10-11: access blocks are now retried).
        The state held alongside the code is what separates them,
        which is why the route is a function of the PAIR and not of the code alone."""
        self.assertEqual(applications.job_route("AUTHENTICATION_REQUIRED", "RECONCILE"), "RECONCILE")
        self.assertEqual(applications.job_route("AUTHENTICATION_REQUIRED", "BLOCKED"), "ORDINARY")

    def test_recoverable_codes_route_to_recovery_when_nothing_is_outstanding(self):
        for code in sorted(application_recovery.RECOVERABLE):
            with self.subTest(code=code):
                self.assertEqual(applications.job_route(code, "BLOCKED"), "RECOVER")

    def test_an_unknown_code_takes_the_ordinary_path_rather_than_stalling(self):
        """Absence of a rule is not a reason to hold a job - the defect class this project
        spent 2026-09-22 removing."""
        self.assertEqual(applications.job_route("SOMETHING_NOBODY_CLASSIFIED", "BLOCKED"), "ORDINARY")

    def test_process_asks_the_authority_rather_than_re_deriving(self):
        import inspect
        source = inspect.getsource(applications.process)
        self.assertIn("job_route(", source)
        # Short messages on purpose: asserting against a 300-line function body dumps the whole
        # thing into the failure output otherwise.
        self.assertFalse('application_recovery.user_hold(job["blocker"])' in source,
                         "process re-derives the OWNER route instead of asking job_route")
        self.assertFalse("in application_recovery.RECOVERABLE" in source,
                         "process re-derives the RECOVER route instead of asking job_route")


if __name__ == "__main__":
    unittest.main()


# RouteCycleTests lived here on 2026-09-22 and went with the detector they covered. Two
# designs were tried - counting route flips, then bounding elapsed time - and both fired on
# healthy behaviour: alternation IS how recovery works, and the rehearsals simulate days. The
# reasoning is preserved in applications.py where the detector used to be.
