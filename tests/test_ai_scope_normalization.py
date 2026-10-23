"""Owner decision 2026-09-19: the AI normalizes scope; Python compares codes and verdicts.

Before this change a verified read reporting country "United States" or a level such as
"mid level to senior level Data Engineer" ended the job INELIGIBLE (final) because scope
was an exact string comparison. Now: the read reports an ISO alpha-2 country code and a
{in_scope, reason} verdict per level/specialization; only an explicit AI out-of-scope
verdict (or an observed structured fact) is final; since 2026-09-27 representation gaps
proceed rather than being held.
"""

import json
from pathlib import Path
import unittest

import application_browser
import applications
from manual_application_browser import event
import profile_home
import user_profile


NOW = "2026-09-19T12:00:00+00:00"
JOB = {"employer": "pawnee health", "requisition": "role-1"}
POLICY = user_profile.load()
SOURCE = {"source": "synthetic-official-posting", "verified": True, "observed_at": NOW}
IN_SCOPE = {"level": {"in_scope": True, "reason": "Senior-level data engineering role"},
            "specialization": {"in_scope": True, "reason": "Backend data platform work"}}


def review(country="US", scope_fit=IN_SCOPE):
    return dict(SOURCE, employer="Pawnee Health", requisition="role-1", official_destination_verified=True,
                action_scope="application_only", employee_count_evidence=dict(SOURCE, value="51-200"),
                scope_fit=json.loads(json.dumps(scope_fit)),
                facts={"open": True, "funding_stage": "Series A", "industry": "Healthcare",
                       "level": "mid level to senior level Data Engineer",
                       "specialization": "Healthcare data engineering", "country": country, "city": "Durham",
                       "work_mode": "remote", "requirements_met": True, "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
                       "hiring_geography_includes_candidate": True, "citizenship_clearance_requirements_met": True})


class ScopeIsJudgedNotStringCompared(unittest.TestCase):
    def test_free_text_level_country_name_or_missing_verdict_proceed(self):
        self.assertIsNone(applications.assess(review(), JOB, POLICY, NOW))
        # Owner 2026-09-27: a non-code country or a missing verdict no longer holds.
        self.assertIsNone(applications.assess(review(country="United States"), JOB, POLICY, NOW))
        self.assertIsNone(applications.assess(review(scope_fit={}), JOB, POLICY, NOW))

    def test_only_an_explicit_ai_out_of_scope_verdict_is_final_and_carries_its_reason(self):
        verdicts = dict(IN_SCOPE, level={"in_scope": False, "reason": "Mid-level role below the Senior target"})
        refused = applications.assess(review(scope_fit=verdicts), JOB, POLICY, NOW)
        self.assertEqual(refused[:2], ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))
        self.assertIn("Mid-level role below the Senior target", refused[2])


class InspectReadCarriesCodesAndVerdicts(unittest.TestCase):
    def test_country_name_is_withdrawn_and_verdicts_need_no_quote(self):
        page = "Pawnee Health\tData Engineer\tEngineering Remote (United States) Share on:"
        result = {"partial_review": {"verified": False, "employer": "Pawnee Health",
                                     "facts": {"country": "United States", "work_mode": "remote"},
                                     "scope_fit": IN_SCOPE}}
        evidence = [("/partial_review/employer", "Pawnee Health"),
                    ("/partial_review/facts/country", "Remote (United States)"),
                    ("/partial_review/facts/work_mode", "Remote (United States)")]
        request = {"request_id": "synthetic", "binding": "synthetic-binding", "max_tool_calls": 40,
                   "payload": {"operation": "inspect", "job": {"job_key": "pawnee health:role-1"}}, "context": {}}
        raw = {"request_id": "synthetic", "binding": "synthetic-binding", "result_json": json.dumps(result),
               "evidence": [{"pointer": pointer, "tool_call_id": "read", "quote": quote} for pointer, quote in evidence]}
        normalized = application_browser.normalize(request, raw, (json.dumps(event("read", page)) + "\n").encode(),
                                                    NOW, Path("synthetic-trace.jsonl"))
        self.assertIsNone(normalized["partial_review"]["facts"]["country"])
        self.assertEqual([(entry["pointer"], entry["reason"]) for entry in normalized["rejected_values"]],
                         [("/partial_review/facts/country", "APPLICATION_COUNTRY_NOT_ISO_ALPHA2")])
        self.assertEqual(normalized["partial_review"]["scope_fit"], IN_SCOPE)


if __name__ == "__main__":
    unittest.main()
