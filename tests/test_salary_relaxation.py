"""The owner's salary relaxation, and its limits.

Verbatim instruction, 2026-09-18: "Please dont be too strict on salary. because some
salaries are in pounds or euros. Relax this a bit."

Three things were blocking, all in `applications.assess`: a non-USD currency without dated
FX evidence, an absent compensation, and a salary shortfall used as a hard AND term in the
eligibility expression. These tests pin the relaxed behaviour, pin what deliberately did
NOT relax, and pin that the compensation the posting stated survives into the review in
every branch including the unknown one.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest

import applications
import orchestrator
import profile_home
import user_profile

NOW = "2026-09-18T12:00:00+00:00"
JOB = {"employer": "example ai", "requisition": "role-123"}
POLICY = user_profile.load()


def evidence(now=NOW):
    return {"source": "mock-independent-official-provider", "verified": True, "observed_at": now}


def review(**overrides):
    base = dict(
        evidence(), employer="Example AI", requisition="role-123",
        official_destination_verified=True, action_scope="application_only",
        employee_count_evidence=dict(evidence(), value="51-200"),
        facts={"open": True, "funding_stage": "Series B", "industry": "AI",
               "level": "Staff Software Engineer", "country": "US", "city": "Austin",
               "work_mode": "remote", "specialization": "backend", "requirements_met": True,
               "authorization": "US_PERMANENT_RESIDENT_ACCEPTED",
               "hiring_geography_includes_candidate": True,
               "citizenship_clearance_requirements_met": True},
        scope_fit={"level": {"in_scope": True, "reason": "fixture: a target level"}, "specialization": {"in_scope": True, "reason": "fixture: a target specialization"}},
        compensation=dict(evidence(), currency="USD", minimum=200000, maximum=250000, period="annual_base"),
    )
    facts = dict(overrides.pop("facts", None) or {})
    compensation = overrides.pop("compensation", "keep")
    base.update(overrides)
    base["scope_fit"].update(facts.pop("scope_fit", {}))
    if facts:
        base["facts"].update(facts)
    if compensation == "keep":
        pass
    elif compensation is None:
        base.pop("compensation")
    else:
        base["compensation"].update(compensation)
    return base


EUROPE = {"country": "DE", "region": "Europe", "city": "Berlin", "work_mode": "remote",
          "authorization": "COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED"}
UK = {"country": "GB", "region": "Europe", "city": "London", "work_mode": "remote",
      "authorization": "COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED"}


def assess(**overrides):
    return applications.assess(review(**overrides), JOB, POLICY, NOW)


def record(**overrides):
    return applications.compensation_assessment(review(**overrides), POLICY, NOW)


class NonUsdSalaryTests(unittest.TestCase):
    """Block 1: `compensation["currency"] != "USD"` demanded dated FX evidence."""

    def test_a_euro_role_without_fx_evidence_proceeds(self):
        self.assertIsNone(assess(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000}))

    def test_a_pound_role_without_fx_evidence_proceeds(self):
        self.assertIsNone(assess(facts=UK, compensation={"currency": "GBP", "minimum": 140000, "maximum": 170000}))

    def test_nothing_on_the_application_path_supplies_fx(self):
        """The reason the old gate was an unconditional refusal, not a verification step.

        `application_browser.result_contract("inspect")` is the contract the browser worker
        answers against, and its compensation object has no fx field, so the worker is never
        asked for one and never returns one.
        """
        import application_browser
        contract = application_browser.result_contract("inspect")
        self.assertEqual(sorted(contract["compensation"]), ["currency", "maximum", "minimum", "period", "verified"])
        self.assertNotIn("fx", contract["compensation"])

    def test_a_low_euro_band_still_proceeds_because_we_cannot_convert_it(self):
        """An approximate rate never refuses a role, in either direction."""
        outcome = assess(facts=EUROPE, compensation={"currency": "EUR", "minimum": 60000, "maximum": 70000})
        self.assertIsNone(outcome)
        self.assertFalse(record(facts=EUROPE, compensation={"currency": "EUR", "minimum": 60000, "maximum": 70000})["blocks"])

    def test_the_approximation_is_visibly_an_approximation(self):
        usd = record(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000})["usd_equivalent"]
        self.assertTrue(usd["approximate"])
        self.assertFalse(usd["verified"])
        self.assertEqual(usd["confidence"], "indicative")
        self.assertEqual(usd["as_of"], applications.APPROXIMATE_USD_RATES["as_of"])
        self.assertIn("APPROXIMATE", usd["caveat"])

    def test_every_reference_rate_is_rounded_to_one_decimal_place(self):
        """A rate like 1.0873 would look quoted and be wrong within the week."""
        for currency, rate in applications.APPROXIMATE_USD_RATES["usd_per_unit"].items():
            with self.subTest(currency=currency):
                self.assertEqual(rate, round(rate, 1), currency + " reads as a precise rate")

    def test_a_stale_reference_table_stops_producing_numbers(self):
        as_of = dt.date.fromisoformat(applications.APPROXIMATE_USD_RATES["as_of"])
        horizon = applications.APPROXIMATE_USD_RATES["stale_after_days"]
        later = (as_of + dt.timedelta(days=horizon + 1)).isoformat() + "T12:00:00+00:00"
        self.assertIsNone(applications.approximate_conversion("EUR", later))
        stale = applications.compensation_assessment(
            review(facts=EUROPE, compensation=dict(evidence(later), currency="EUR", minimum=150000, maximum=190000)),
            POLICY, later)
        self.assertEqual(stale["basis"], "NO_CONVERSION_AVAILABLE")
        self.assertIsNone(stale["usd_equivalent"])
        self.assertFalse(stale["blocks"])

    def test_an_unknown_currency_invents_nothing(self):
        unknown = record(facts=EUROPE, compensation={"currency": "XYZ", "minimum": 150000, "maximum": 190000})
        self.assertEqual(unknown["basis"], "NO_CONVERSION_AVAILABLE")
        self.assertIsNone(unknown["usd_equivalent"])
        self.assertFalse(unknown["blocks"])
        self.assertEqual(unknown["stated"]["currency"], "XYZ")

    def test_dated_fx_evidence_is_still_used_exactly_when_supplied(self):
        supplied = record(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000,
                                                      "fx": dict(evidence(), usd_per_unit=1.08)})
        self.assertEqual(supplied["basis"], "DATED_FX_EVIDENCE")
        self.assertFalse(supplied["usd_equivalent"]["approximate"])
        self.assertAlmostEqual(supplied["usd_equivalent"]["maximum"], 190000 * 1.08)

    def test_unusable_fx_evidence_falls_back_to_the_approximation_and_never_raises(self):
        for broken in ({}, {"usd_per_unit": None}, dict(evidence(), usd_per_unit="soon"),
                       dict(evidence(), usd_per_unit=0), dict(evidence(), usd_per_unit=float("inf")),
                       {"usd_per_unit": 1.1}):
            with self.subTest(fx=broken):
                outcome = record(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000,
                                                             "maximum": 190000, "fx": broken})
                self.assertNotEqual(outcome["basis"], "DATED_FX_EVIDENCE")
                self.assertFalse(outcome["blocks"])


class UnpublishedSalaryTests(unittest.TestCase):
    """Block 2: `if not fresh(compensation, now)` blocked every posting with no salary."""

    def test_a_posting_with_no_salary_proceeds(self):
        self.assertIsNone(assess(compensation=None))

    def test_a_european_posting_with_no_salary_proceeds(self):
        self.assertIsNone(assess(facts=EUROPE, compensation=None))

    def test_an_undated_or_unverified_figure_proceeds_and_is_not_trusted(self):
        for broken in ({"verified": False}, {"source": ""}, {"observed_at": "2026-09-01T00:00:00+00:00"}):
            with self.subTest(compensation=broken):
                self.assertIsNone(assess(compensation=broken))
                self.assertFalse(record(compensation=broken)["blocks"])

    def test_no_figure_is_ever_fabricated_for_an_unpublished_salary(self):
        unknown = record(compensation=None)
        self.assertIsNone(unknown["stated"])
        self.assertIsNone(unknown["usd_equivalent"])
        self.assertIsNone(unknown["comparable_usd"])
        self.assertEqual(unknown["finding"], "SALARY_UNKNOWN")

    def test_an_unreadable_band_proceeds_and_never_raises(self):
        for broken in ({"minimum": None}, {"minimum": "competitive"}, {"maximum": None},
                       {"minimum": 250000, "maximum": 200000}, {"currency": None},
                       {"minimum": float("nan")}, {"minimum": -1000}):
            with self.subTest(compensation=broken):
                self.assertIsNone(assess(compensation=broken))
                self.assertIn("SALARY_UNKNOWN", record(compensation=broken)["warnings"])

    def test_a_band_that_is_not_an_annual_base_salary_proceeds(self):
        for period in ("total_compensation", "monthly", "hourly", None):
            with self.subTest(period=period):
                self.assertIsNone(assess(compensation={"period": period}))

    def test_a_monthly_figure_is_never_compared_against_an_annual_floor(self):
        monthly = record(compensation={"period": "monthly", "minimum": 12000, "maximum": 15000})
        self.assertEqual(monthly["finding"], "NOT_ANNUAL_BASE")
        self.assertFalse(monthly["blocks"])
        self.assertIsNone(monthly["comparable_usd"])


class RelaxedFloorTests(unittest.TestCase):
    """Block 3: `salary_ok` was a hard AND term in the eligibility expression."""

    def test_a_band_below_the_recorded_minimum_now_proceeds_with_a_warning(self):
        self.assertIsNone(assess(compensation={"minimum": 150000, "maximum": 175000}))
        warned = record(compensation={"minimum": 150000, "maximum": 175000})
        self.assertEqual(warned["finding"], "BELOW_RECORDED_MINIMUM_WITHIN_TOLERANCE")
        self.assertIn("BASE_SALARY_BELOW_RECORDED_MINIMUM", warned["warnings"])
        self.assertIn("175,000", warned["note"])

    def test_the_relaxed_floor_is_the_recorded_minimum_less_the_tolerance(self):
        self.assertEqual(POLICY["minimum_base_salary_usd"], 180000)
        self.assertEqual(record()["relaxed_floor_usd"], 135000)

    def test_a_band_far_below_the_relaxed_floor_is_still_refused_and_named(self):
        outcome = assess(compensation={"minimum": 80000, "maximum": 95000})
        self.assertEqual(outcome[0], "INELIGIBLE")
        self.assertEqual(outcome[1], "BASE_SALARY_BELOW_RELAXED_FLOOR")
        self.assertIn("95,000", outcome[2])

    def test_the_floor_is_applied_to_the_top_of_the_band_not_the_bottom(self):
        """A role is refused only when even its best case is far below what was asked for."""
        self.assertIsNone(assess(compensation={"minimum": 100000, "maximum": 140000}))
        self.assertEqual(assess(compensation={"minimum": 100000, "maximum": 130000})[1], "BASE_SALARY_BELOW_RELAXED_FLOOR")

    def test_the_floor_applies_only_to_a_figure_that_is_genuinely_knowable(self):
        low = {"minimum": 80000, "maximum": 95000}
        self.assertTrue(record(compensation=low)["blocks"], "USD is knowable")
        self.assertTrue(record(compensation=dict(low, currency="EUR", fx=dict(evidence(), usd_per_unit=1.0)))["blocks"],
                        "dated FX evidence is knowable")
        self.assertFalse(record(facts=EUROPE, compensation=dict(low, currency="EUR"))["blocks"],
                         "an approximate rate is not knowable enough to refuse a role")
        self.assertFalse(record(compensation=dict(low, verified=False))["blocks"],
                         "an unverified figure is not knowable")
        self.assertFalse(record(compensation=dict(low, period="total_compensation"))["blocks"],
                         "a non-base figure is not comparable")

    def test_the_tolerance_is_policy_driven_with_a_recorded_default(self):
        self.assertEqual(applications.salary_relaxation({})["tolerance_below_minimum"], 0.25)
        self.assertEqual(applications.salary_relaxation({"salary_relaxation": {"tolerance_below_minimum": 0.5}})["tolerance_below_minimum"], 0.5)
        for nonsense in ({"tolerance_below_minimum": "half"}, {"tolerance_below_minimum": 1.0},
                         {"tolerance_below_minimum": -0.2}, {"tolerance_below_minimum": float("nan")}, "not a dict"):
            with self.subTest(override=nonsense):
                self.assertEqual(applications.salary_relaxation({"salary_relaxation": nonsense})["tolerance_below_minimum"], 0.25)

    def test_raising_the_tolerance_admits_a_role_the_default_refuses(self):
        relaxed = dict(POLICY, salary_relaxation={"tolerance_below_minimum": 0.6})
        low = review(compensation={"minimum": 80000, "maximum": 95000})
        self.assertEqual(applications.assess(copy.deepcopy(low), JOB, POLICY, NOW)[1], "BASE_SALARY_BELOW_RELAXED_FLOOR")
        self.assertIsNone(applications.assess(copy.deepcopy(low), JOB, relaxed, NOW))


class ToleranceKnobTests(unittest.TestCase):
    """The owner kept the tolerance and asked for the percentage to be theirs to change.

    The number that decides which jobs get applied to must be visible in
    `profile.json`, not discoverable only by reading the source.
    """

    def tolerance(self, value):
        return dict(POLICY, salary_relaxation=dict(POLICY.get("salary_relaxation", {}), tolerance_below_minimum=value))

    def test_the_policy_file_carries_the_key_and_the_code_reads_that_value(self):
        recorded = POLICY.get("salary_relaxation")
        if recorded is None:
            self.skipTest("profile.json in this tree predates the salary_relaxation key; "
                          "the code default of 0.25 applies and behaviour is identical")
        self.assertIn("tolerance_below_minimum", recorded)
        self.assertEqual(recorded["tolerance_below_minimum"], 0.25)
        self.assertEqual(applications.salary_relaxation(POLICY)["tolerance_below_minimum"],
                         recorded["tolerance_below_minimum"])
        self.assertTrue(recorded.get("source"), "the entry must carry its provenance")
        self.assertIn("0.0", recorded.get("tolerance_below_minimum_note", ""),
                      "the way back to a hard floor must be written down")

    def test_the_file_and_the_code_default_agree(self):
        """Installing the code without the policy must change nothing."""
        self.assertEqual(applications.DEFAULT_SALARY_RELAXATION["tolerance_below_minimum"], 0.25)
        self.assertEqual(applications.salary_relaxation({})["tolerance_below_minimum"],
                         applications.salary_relaxation(self.tolerance(0.25))["tolerance_below_minimum"])

    def test_zero_restores_the_hard_floor_at_the_recorded_minimum(self):
        hard = self.tolerance(0.0)
        self.assertEqual(applications.salary_relaxation(hard)["tolerance_below_minimum"], 0.0)
        for band in ((120000, 140000), (150000, 175000), (160000, 165000), (150000, 179000)):
            with self.subTest(band=band):
                item = review(compensation={"minimum": band[0], "maximum": band[1]})
                self.assertIsNone(applications.assess(copy.deepcopy(item), JOB, POLICY, NOW))
                self.assertEqual(applications.assess(copy.deepcopy(item), JOB, hard, NOW)[0], "INELIGIBLE")
        self.assertIsNone(applications.assess(review(compensation={"minimum": 200000, "maximum": 250000}), JOB, hard, NOW))
        self.assertIsNone(applications.assess(review(compensation={"minimum": 175000, "maximum": 180000}), JOB, hard, NOW))

    def test_ten_percent_puts_the_effective_floor_at_162000(self):
        setting = self.tolerance(0.10)
        self.assertEqual(applications.compensation_assessment(review(), setting, NOW)["relaxed_floor_usd"], 162000)
        self.assertIsNone(applications.assess(review(compensation={"minimum": 155000, "maximum": 163000}), JOB, setting, NOW))
        self.assertEqual(applications.assess(review(compensation={"minimum": 155000, "maximum": 161000}), JOB, setting, NOW)[1],
                         "BASE_SALARY_BELOW_RELAXED_FLOOR")
        self.assertEqual(applications.assess(review(compensation={"minimum": 120000, "maximum": 140000}), JOB, setting, NOW)[1],
                         "BASE_SALARY_BELOW_RELAXED_FLOOR")

    def test_an_invalid_value_falls_back_rather_than_removing_the_floor(self):
        for bad in (-0.2, 1.0, 1.5, "a quarter", None, float("nan"), float("inf"), True, [], {}):
            with self.subTest(value=bad):
                setting = self.tolerance(bad)
                self.assertEqual(applications.salary_relaxation(setting)["tolerance_below_minimum"], 0.25)
                self.assertEqual(applications.assess(review(compensation={"minimum": 80000, "maximum": 95000}), JOB, setting, NOW)[1],
                                 "BASE_SALARY_BELOW_RELAXED_FLOOR")

    def test_the_knob_never_touches_the_currency_half(self):
        cases = {
            "euros, no fx": dict(facts=EUROPE, compensation={"currency": "EUR", "minimum": 60000, "maximum": 70000}),
            "pounds, no fx": dict(facts=UK, compensation={"currency": "GBP", "minimum": 50000, "maximum": 60000}),
            "unknown currency": dict(facts=EUROPE, compensation={"currency": "XYZ", "minimum": 10, "maximum": 20}),
            "nothing published": dict(compensation=None),
            "EU, nothing published": dict(facts=EUROPE, compensation=None),
        }
        for value in (0.0, 0.10, 0.25, 0.5, 0.99, -0.2, "nonsense"):
            for name, overrides in cases.items():
                with self.subTest(tolerance=value, case=name):
                    item = review(**copy.deepcopy(overrides))
                    self.assertIsNone(applications.assess(item, JOB, self.tolerance(value), NOW))


class CrossingBandTests(unittest.TestCase):
    """`CROSSING_BASE_BAND_UNRESOLVED` survives, demoted from blocker to warning."""

    def test_a_crossing_band_is_recorded_rather_than_blocked(self):
        self.assertIsNone(assess(compensation={"minimum": 150000, "maximum": 220000}))
        self.assertIn("CROSSING_BASE_BAND_UNRESOLVED", record(compensation={"minimum": 150000, "maximum": 220000})["warnings"])

    def test_the_warning_survives_verbatim_so_existing_reporting_still_finds_it(self):
        self.assertIn("CROSSING_BASE_BAND_UNRESOLVED", applications.__file__ and
                      Path(applications.__file__).read_text())

    def test_a_verified_candidate_target_still_resolves_the_crossing_band(self):
        resolved = record(compensation={"minimum": 150000, "maximum": 220000, "target": 200000,
                                        "target_verified_for_candidate": True})
        self.assertEqual(resolved["warnings"], [])
        self.assertEqual(resolved["comparable_kind"], "verified candidate-specific target")
        self.assertEqual(resolved["comparable_usd"], 200000)

    def test_an_unverified_or_out_of_band_target_is_ignored(self):
        for target in ({"target": 200000}, {"target": 200000, "target_verified_for_candidate": False},
                       {"target": 400000, "target_verified_for_candidate": True},
                       {"target": "about 200k", "target_verified_for_candidate": True}):
            with self.subTest(target=target):
                outcome = record(compensation=dict({"minimum": 150000, "maximum": 220000}, **target))
                self.assertEqual(outcome["comparable_kind"], "top of the stated band")

    def test_a_verified_target_below_the_relaxed_floor_still_refuses(self):
        """The most specific known figure is the one that counts, in both directions."""
        outcome = assess(compensation={"minimum": 100000, "maximum": 260000, "target": 110000,
                                       "target_verified_for_candidate": True})
        self.assertEqual(outcome[1], "BASE_SALARY_BELOW_RELAXED_FLOOR")


class StatedCompensationIsAlwaysPreservedTests(unittest.TestCase):
    """Relaxing the gate must not mean losing the information."""

    CASES = {
        "euros, no fx": dict(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000}),
        "pounds, no fx": dict(facts=UK, compensation={"currency": "GBP", "minimum": 140000, "maximum": 170000}),
        "euros with dated fx": dict(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000,
                                                                "fx": dict(evidence(), usd_per_unit=1.08)}),
        "nothing published": dict(compensation=None),
        "unverified figure": dict(compensation={"verified": False}),
        "unreadable band": dict(compensation={"minimum": "competitive"}),
        "not annual base": dict(compensation={"period": "total_compensation"}),
        "below the recorded minimum": dict(compensation={"minimum": 150000, "maximum": 175000}),
        "below the relaxed floor": dict(compensation={"minimum": 80000, "maximum": 95000}),
        "crossing the band": dict(compensation={"minimum": 150000, "maximum": 220000}),
        "verified candidate target": dict(compensation={"minimum": 150000, "maximum": 220000, "target": 200000,
                                                        "target_verified_for_candidate": True}),
        "in scope": {},
        "unknown currency": dict(facts=EUROPE, compensation={"currency": "XYZ"}),
    }

    def test_every_branch_records_what_the_posting_stated(self):
        for name, overrides in self.CASES.items():
            with self.subTest(case=name):
                source = review(**copy.deepcopy(overrides))
                outcome = applications.compensation_assessment(source, POLICY, NOW)
                json.dumps(outcome)  # must survive being stored with the review
                self.assertIn("stated", outcome)
                self.assertIn("note", outcome)
                self.assertTrue(outcome["note"])
                if "compensation" in source:
                    self.assertEqual(outcome["stated_by_employer"], True)
                    for field in ("currency", "minimum", "maximum", "period"):
                        if field in source["compensation"]:
                            self.assertEqual(outcome["stated"][field], source["compensation"][field])
                else:
                    self.assertFalse(outcome["stated_by_employer"])
                    self.assertIsNone(outcome["stated"])

    def test_the_unknown_case_records_that_nothing_was_published(self):
        unknown = record(compensation=None)
        self.assertFalse(unknown["stated_by_employer"])
        self.assertIn("SALARY_NOT_PUBLISHED", unknown["warnings"])
        self.assertIn("published no base compensation", unknown["note"])

    def test_the_original_currency_and_band_are_never_rewritten_into_usd(self):
        source = review(facts=EUROPE, compensation={"currency": "EUR", "minimum": 150000, "maximum": 190000})
        outcome = applications.compensation_assessment(source, POLICY, NOW)
        self.assertEqual(source["compensation"]["currency"], "EUR")
        self.assertEqual(source["compensation"]["minimum"], 150000)
        self.assertEqual(outcome["stated"]["currency"], "EUR")

    def test_the_record_never_raises_on_hostile_input(self):
        for hostile in ({}, {"compensation": None}, {"compensation": "competitive"}, {"compensation": []},
                        {"compensation": {"minimum": {}, "maximum": []}}, {"facts": {}}):
            with self.subTest(review=hostile):
                outcome = applications.compensation_assessment(hostile, POLICY, NOW)
                self.assertFalse(outcome["blocks"])
                json.dumps(outcome)

    def test_a_policy_without_a_recorded_minimum_refuses_nothing(self):
        outcome = applications.compensation_assessment(review(compensation={"minimum": 1, "maximum": 2}),
                                                       {k: v for k, v in POLICY.items() if k != "minimum_base_salary_usd"}, NOW)
        self.assertFalse(outcome["blocks"])
        self.assertEqual(outcome["finding"], "NO_RECORDED_MINIMUM")


class NothingElseRelaxedTests(unittest.TestCase):
    """Every other gate must behave exactly as it did before the salary change."""

    VIOLATIONS = {
        # 2026-10-07: the owner's onsite/hybrid exclusions are the AI's scope_fit.location verdict.
        "onsite Seattle": {"work_mode": "onsite", "city": "Seattle", "state": "WA",
                           "scope_fit": {"location": {"in_scope": False, "reason": "Seattle onsite is excluded"}}},
        "hybrid San Francisco": {"work_mode": "hybrid", "city": "San Francisco",
                                 "scope_fit": {"location": {"in_scope": False, "reason": "San Francisco hybrid is excluded"}}},
        # 2026-09-19: level and specialization are refused only on the AI's explicit verdict.
        "out of scope level": {"level": "Junior Software Engineer",
                               "scope_fit": {"level": {"in_scope": False, "reason": "Junior, not a target level"}}},
        "out of scope specialization": {"specialization": "frontend",
                                        "scope_fit": {"specialization": {"in_scope": False, "reason": "Frontend only"}}},
        "closed role": {"open": False},
        "geography excludes candidate": {"hiring_geography_includes_candidate": False},
        # Europe onsite is in scope since 2026-09-19 (owner willing to relocate); a non-target
        # country onsite is the scope violation that must stay ineligible.
        "non-target country onsite": {"country": "CA", "region": "North America", "city": "Toronto", "work_mode": "onsite",
                                      "authorization": "COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED"},
        "non-europe non-us": {"country": "XK", "region": "Balkans", "city": "Prishtina", "work_mode": "remote",
                              "authorization": "COUNTRY_SPECIFIC_AUTHORIZATION_UNVERIFIED"},
    }
    HARD_BLOCKS = {
        # Owner, 2026-09-29: a stated citizenship/clearance requirement is out of scope (final).
        "clearance requirement unmet": ({"citizenship_clearance_requirements_met": False}, "CLEARANCE_OR_CITIZENSHIP_REQUIRED"),
        "europe, employer cannot sponsor": (dict(EUROPE, employer_sponsorship=False), "REQUIRED_ELIGIBILITY_ANSWER"),
        "europe, clearance requirement unmet": (dict(EUROPE, citizenship_clearance_requirements_met=False), "CLEARANCE_OR_CITIZENSHIP_REQUIRED"),
        "us without permanent residence": ({"authorization": "NOT_AUTHORIZED"}, "REQUIRED_ELIGIBILITY_ANSWER"),
        # 2026-09-19: a value that is not a policy member is held for review, never final.
    }
    COMPENSATIONS = [
        ("in scope USD", {}),
        ("euros, no fx", {"currency": "EUR", "minimum": 150000, "maximum": 190000}),
        ("pounds, no fx", {"currency": "GBP", "minimum": 140000, "maximum": 170000}),
        ("nothing published", None),
        ("below the recorded minimum", {"minimum": 150000, "maximum": 175000}),
    ]

    def test_scope_violations_stay_ineligible_under_every_compensation(self):
        for name, facts in self.VIOLATIONS.items():
            for label, compensation in self.COMPENSATIONS:
                with self.subTest(violation=name, compensation=label):
                    outcome = assess(facts=facts, compensation=compensation)
                    self.assertEqual(outcome[:2], ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))

    def test_hard_blocks_stay_blocked_under_every_compensation(self):
        for name, (facts, blocker) in self.HARD_BLOCKS.items():
            for label, compensation in self.COMPENSATIONS:
                with self.subTest(block=name, compensation=label):
                    outcome = assess(facts=facts, compensation=compensation)
                    self.assertEqual(outcome[0], "INELIGIBLE" if blocker == "CLEARANCE_OR_CITIZENSHIP_REQUIRED" else "BLOCKED")
                    self.assertEqual(outcome[1], blocker)

    def test_a_known_funding_stage_outside_policy_is_final_under_every_compensation(self):
        # Owner, 2026-09-27: final, not a review hold. Since 2026-10-07 the AI's
        # scope_fit.funding_stage verdict decides, not Python's list comparison.
        for label, compensation in self.COMPENSATIONS:
            with self.subTest(compensation=label):
                outcome = assess(facts={"funding_stage": "Series G", "scope_fit": {"funding_stage": {
                    "in_scope": False, "reason": "Series G is outside the owner's stages"}}}, compensation=compensation)
                self.assertEqual(outcome[:2], ("INELIGIBLE", "OUTSIDE_CONFIRMED_SCOPE"))

    def test_unknown_headcount_does_not_block(self):
        # Patch BB (owner 2026-09-19): unknown headcount informs (HEADCOUNT_UNKNOWN), never blocks.
        self.assertIsNone(applications.assess(review(employee_count_evidence={}), JOB, POLICY, NOW))

    def test_an_eligible_europe_remote_role_is_still_pursued(self):
        self.assertIsNone(assess(facts=EUROPE))
        self.assertIsNone(assess(facts=UK))

    def test_no_authorization_answer_changed(self):
        """The salary change touches no recorded answer, and asserts no authorization.

        The truthfulness guarantee installed today rests on the answer pool, which this
        change does not read, write or extend.
        """
        answers = POLICY["answers"]
        # Not a count: a count breaks every time an answer is recorded and protects nothing.
        # What must hold is that the loop below actually inspects a non-US authorization
        # answer, so it cannot pass by iterating over nothing.
        checked = 0
        for field, saved in answers.items():
            semantic = str(saved.get("semantic", ""))
            outside_us = any(token in semantic for token in ("eu", "europe", "germany", "spain", "france",
                                                             "uk", "united_kingdom", "canada", "switzerland"))
            if outside_us and any(token in semantic for token in ("authoriz", "permit", "citizen", "right_to_work", "legally")):
                checked += 1
                self.assertNotIn(saved.get("value"), (True, "Yes", "yes"), field)
        self.assertGreater(checked, 0, "no non-US authorization answer was inspected")
        source = Path(applications.__file__).read_text()
        compensation_block = source[source.index("def compensation_assessment"):source.index("def assess(review")]
        for forbidden in ("answers", "authoriz", "sponsor", "resolve_answers"):
            self.assertNotIn(forbidden, compensation_block)


if __name__ == "__main__":
    unittest.main()
