"""Patch BB (owner decisions 2026-09-19): per-scope inspect cap; silent postings and
informational facts no longer block; stated requirements still decide.

Each test fails on the pre-BB code: the cap counted every inspect claim ever made for the
job, a quote attached to a "requirement stated" marker disabled the silent-posting rule
and unknown funding stage/headcount/industry/city blocked, and the stated-requirement enum
did not exist (a posting stating "US citizens only" was derived as silent).
"""

import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import applications
import application_browser
import application_connection
import desktop_adapter
import reopen_grants
import test_part_c_read_gating as part_c
import profile_home
import user_profile

ROOT = Path(__file__).resolve().parent.parent
POLICY = user_profile.load()
NOW = "2026-09-20T09:00:00+00:00"


def later(hours):
    return (dt.datetime.fromisoformat(NOW) + dt.timedelta(hours=hours)).isoformat()


def review(country="US", work_mode="remote", city=None, state=None, stated=("NONE_STATED",), **facts):
    """A verified read of a posting silent on everything except what the arguments add."""
    values = {"open": True, "funding_stage": None, "industry": None, "level": "Staff Software Engineer",
              "specialization": "backend", "country": country, "state": state, "city": city, "work_mode": work_mode,
              "requirements_met": True, "requirements_met_reason": "fixture", "authorization": None,
              "hiring_geography_includes_candidate": None, "citizenship_clearance_requirements_met": None,
              "work_authorization_requirement_stated": False, "stated_eligibility_requirements": list(stated)}
    values.update(facts)
    return {"verified": True, "source": "mock-browser", "observed_at": NOW, "employer": "Gate Systems",
            "requisition": "4700000001", "official_destination_verified": True, "action_scope": "application_only",
            "observed_browser_calls": 4, "employee_count_evidence": {"verified": False, "value": None},
            # The live Dharma read: a quote bound to the marker that says nothing about authorization.
            "browser_evidence": [{"pointer": "/facts/work_authorization_requirement_stated", "tool_call_id": "t1",
                                  "quote": "5+ years writing production-quality code"}],
            "scope_fit": {"level": {"in_scope": True, "reason": "fixture"},
                          "specialization": {"in_scope": True, "reason": "fixture"}},
            "facts": values, "compensation": {}}


JOB = {"employer": "gate systems", "requisition": "4700000001"}


def decide(read):
    applications.derive_silent_eligibility_facts(read, POLICY)
    return applications.assess(read, JOB, POLICY, NOW)


class InspectScopeTests(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def test_reopen_grant_and_supersession_start_a_new_inspect_scope(self):
        adapter = application_connection.HostedDesktopAdapter(
            self.database, self.root, {"application_browser": {"enabled": True}}, clock=lambda: later(0))
        body = adapter.request_body("inspect", {"job": self.job()})

        def claims(hours, count):
            with self.database:
                for index in range(count):
                    self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,
                        state,created_at,expires_at,claims,result) VALUES (?,?,?,?,?,'DONE',?,?,1,'{}')""",
                        (f"r{hours}-{index}", f"b{hours}-{index}", self.key, "inspect", desktop_adapter.encode(body),
                         later(hours), later(hours + 1)))
        claims(-3, 3)  # a spent scope before the owner's reopen
        with self.database:
            self.database.executescript(reopen_grants.SCHEMA)
            self.database.execute("INSERT INTO application_reopen_grants VALUES (?,?,?,?,?,?,?,?)",
                                  ("grant-1", "owner", json.dumps([self.key]), 3, 48, later(-1), later(47), "reopen"))
            self.database.execute("INSERT INTO application_reopen_grant_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                                  ("grant-1", self.key, later(-1), 1, "[]", later(-2), "RULES_CHANGED", 3, later(47)))
        allowed = {"reviewed": True}
        with patch.object(application_connection.HostedDesktopAdapter, "reserve", lambda *a, **k: None), \
                patch.object(desktop_adapter.DesktopAdapter, "call", lambda *a, **k: allowed):
            self.assertIs(adapter.call("inspect", {"job": self.job(), "read_only": True}), allowed)
            claims(-0.5, 3)  # the cap of three still holds inside the reopened scope
            exhausted = adapter.call("inspect", {"job": self.job(), "read_only": True})
            self.assertEqual(exhausted["blocked"]["code"], "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED")
            with self.database:
                applications.event(self.database, self.key, "APPLICATION_RECOVERY_SUPERSEDED", {}, later(-0.25))
            self.assertIs(adapter.call("inspect", {"job": self.job(), "read_only": True}), allowed)


class SilentEligibilityTests(unittest.TestCase):
    def test_silent_posting_with_unrelated_marker_quote_is_eligible(self):
        read = review()
        self.assertIsNone(decide(read))
        self.assertEqual(read["facts"]["authorization"], "US_PERMANENT_RESIDENT_ACCEPTED")
        self.assertEqual(read["eligibility_fact_basis"]["hiring_geography_includes_candidate"]["basis"],
                         "POSTING_SILENT_US_ROLE")
        self.assertEqual(applications.eligibility_warnings(read, NOW),
                         ["FUNDING_STAGE_UNKNOWN", "HEADCOUNT_UNKNOWN", "INDUSTRY_UNKNOWN", "CITY_UNKNOWN"])

    def test_stated_requirements_still_fail_and_unevaluable_locations_proceed(self):
        self.assertEqual(decide(review(stated=["US_CITIZENSHIP_REQUIRED"]))[:2], ("INELIGIBLE", "CLEARANCE_OR_CITIZENSHIP_REQUIRED"))
        self.assertEqual(decide(review(stated=["SECURITY_CLEARANCE_REQUIRED"]))[:2], ("INELIGIBLE", "CLEARANCE_OR_CITIZENSHIP_REQUIRED"))
        self.assertEqual(decide(review(country="DE", city="Berlin", work_mode="hybrid", stated=["NO_VISA_SPONSORSHIP"]))[1],
                         "REQUIRED_ELIGIBILITY_ANSWER")
        self.assertIsNone(decide(review(stated=["US_WORK_AUTHORIZATION_REQUIRED", "NO_VISA_SPONSORSHIP"])))
        self.assertIsNone(decide(review(country="DE", city="Berlin", work_mode="hybrid")))
        # Owner 2026-09-27: an undecidable location (onsite US, no city or state) proceeds.
        self.assertIsNone(decide(review(work_mode="onsite")))
        self.assertIsNone(decide(review(work_mode="onsite", state="DC")))
        # The browser contract withdraws a value outside the enum; NONE_STATED needs no quote.
        result = {"facts": {"stated_eligibility_requirements": ["NONE_STATED", "5+ years of experience"]}}
        invalid = application_browser.constrain_scope_values(result)
        self.assertEqual(result["facts"]["stated_eligibility_requirements"], ["NONE_STATED", None])
        self.assertEqual(invalid[0]["reason"], "APPLICATION_STATED_REQUIREMENT_NOT_ENUM")


class AiAuthorizationJudgmentTests(unittest.TestCase):
    """Owner, 2026-10-21 ("remove those blockers"): the inspect read judges the owner's map for
    the role's country (EU line included); Python no longer looks the country up."""

    def policy(self, mapping, open_to_sponsorship=False):
        policy = json.loads(json.dumps(POLICY))
        policy["answers"]["work_authorization_by_jurisdiction"]["value"] = mapping
        policy["answers"]["open_to_visa_sponsorship"]["value"] = open_to_sponsorship
        return policy

    def decide(self, read, policy):
        applications.derive_silent_eligibility_facts(read, policy)
        return applications.assess(read, JOB, policy, NOW)

    def test_an_eu_line_user_is_eligible_for_a_german_role_that_will_not_sponsor(self):
        policy = self.policy({"*": "NOT_AUTHORIZED", "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        read = review(country="DE", city="Berlin", work_mode="hybrid", stated=["NO_VISA_SPONSORSHIP"],
                      authorization="AUTHORIZED_NO_SPONSORSHIP_REQUIRED")
        self.assertIsNone(self.decide(read, policy))

    def test_the_reads_not_authorized_verdict_still_meets_a_sponsorship_refusal(self):
        policy = self.policy({"*": "NOT_AUTHORIZED", "EU": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                              "DE": "NOT_AUTHORIZED"}, open_to_sponsorship=True)
        read = review(country="DE", city="Berlin", work_mode="hybrid", stated=["NO_VISA_SPONSORSHIP"],
                      authorization="NOT_AUTHORIZED")
        self.assertEqual(self.decide(read, policy)[:2], ("BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER"))

    def test_a_us_role_requiring_canadian_authorization_follows_the_reads_judgment(self):
        policy = self.policy({"*": "NOT_AUTHORIZED", "US": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED",
                              "CA": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        met = review(stated=["OTHER_COUNTRY_AUTHORIZATION_REQUIRED"],
                     authorization="AUTHORIZED_NO_SPONSORSHIP_REQUIRED")
        self.assertIsNone(self.decide(met, policy))
        without_ca = self.policy({"*": "NOT_AUTHORIZED", "US": "AUTHORIZED_NO_SPONSORSHIP_REQUIRED"})
        not_met = review(stated=["OTHER_COUNTRY_AUTHORIZATION_REQUIRED"], authorization="STATED_REQUIREMENT_NOT_MET")
        self.assertEqual(self.decide(not_met, without_ca)[:2], ("BLOCKED", "REQUIRED_ELIGIBILITY_ANSWER"))
        self.assertIn("STATED_REQUIREMENT_NOT_MET", application_browser.PROMPT)

    def test_the_read_is_given_the_map_rules_and_the_submit_session_may_apply_eu(self):
        self.assertIn("work_authorization_by_jurisdiction", application_browser.PROMPT)
        self.assertIn('"EU" (covers every European Union member state', application_browser.PROMPT)
        use = applications.recorded_authorization_facts(POLICY)["use"]
        self.assertIn("'EU' entry", use)
        self.assertNotIn("Never combine", use)


if __name__ == "__main__":
    unittest.main()
