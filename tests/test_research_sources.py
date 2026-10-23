import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import research_sources


class ResearchSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "research-source-observations.json"
        self.now = dt.datetime(2026, 9, 17, 19, tzinfo=dt.timezone.utc)
        self.plan = {
            "plan_id": "industry-research-plan:2026-09-17:cybersecurity",
            "day": "2026-09-17",
            "scope_enforced": True,
            "assignments": [{"company": "Vandelay Security", "contact_slots": 0}],
        }

    def observation(self, **updates):
        source = "https://engineering.vandelaysec.com/"
        record = {
            "plan_id": self.plan["plan_id"],
            "company": "Vandelay Security",
            "source": source,
            "observed_at": "2026-09-17T11:21:04-07:00",
            "excerpt": "Fixture source excerpt",
            "provenance": {
                "method": "foreground_browser_read_only",
                "source": source,
                "reference": "Fixture foreground capture; no unattended execution proof",
            },
        }
        record.update(updates)
        return record

    def write_observations(self, *records):
        self.path.write_text(json.dumps({"version": 1, "observations": list(records)}))

    def test_missing_blank_and_empty_observations(self):
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), [])
        self.assertFalse(self.path.exists())
        for content in ("", " \n\t", '{"version": 1, "observations": []}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(research_sources.load(self.root, self.plan, self.now), [])

    def test_five_records_partition_by_plan_without_mutation(self):
        ai_plan = dict(self.plan, plan_id="industry-research-plan:2026-09-17:ai",
                       assignments=[{"company": "Litware"}])
        biotech_plan = dict(self.plan, plan_id="industry-research-plan:2026-09-17:biotech",
                            assignments=[{"company": "Example Biomedicine"}])
        vandelay_records = [self.observation(excerpt=f"Fixture Vandelay excerpt {index}") for index in range(3)]
        ai_records = [self.observation(company="Litware", plan_id=ai_plan["plan_id"],
                                       excerpt=f"Fixture Litware excerpt {index}") for index in range(2)]
        self.write_observations(*(vandelay_records + ai_records))
        original_bytes = self.path.read_bytes()
        original_plan = copy.deepcopy(self.plan)
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), vandelay_records)
        self.assertEqual(research_sources.load(self.root, ai_plan, self.now), ai_records)
        self.assertEqual(research_sources.load(self.root, biotech_plan, self.now), [])
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.plan, original_plan)

    def test_previous_day_same_company_skips_before_evidence_validation(self):
        old_record = self.observation(plan_id="industry-research-plan:2026-09-16:cybersecurity",
                                      source="http://localhost", observed_at="2026-09-16T00:00:00Z")
        other_plan_without_company = {"plan_id": "industry-research-plan:2026-09-16:ai"}
        current_record = self.observation()
        self.write_observations(old_record, other_plan_without_company, current_record)
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), [current_record])

    def test_unassigned_company_cannot_supply_evidence(self):
        self.write_observations(self.observation(company="Unassigned Company", source="invalid"))
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), [])

    def test_assignment_matching_preserves_original_company_text(self):
        record = self.observation(company=" VANDELAY   SECURITY ")
        self.write_observations(record)
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), [record])

    def test_invalid_roots_fail_closed(self):
        invalid = [None, [], {}, {"version": True, "observations": []},
                   {"version": 2, "observations": []}, {"version": 1, "observations": {}},
                   {"version": 1, "observations": [], "verified": True}]
        for document in invalid:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)
        self.path.write_text('{"version":')
        with self.assertRaises(ValueError):
            research_sources.load(self.root, self.plan, self.now)

    def test_file_and_record_limits(self):
        self.path.write_text(" " * 1_000_001)
        with self.assertRaises(ValueError):
            research_sources.load(self.root, self.plan, self.now)
        self.write_observations(*[self.observation() for unused in range(121)])
        with self.assertRaises(ValueError):
            research_sources.load(self.root, self.plan, self.now)

    def test_invalid_plan_scope_fails_closed(self):
        self.write_observations(self.observation())
        for change in ({"scope_enforced": False}, {"plan_id": ""}, {"day": "2026-09-16"},
                       {"assignments": None}, {"assignments": [{"company": ""}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                research_sources.load(self.root, dict(self.plan, **change), self.now)

    def test_missing_bindings_and_malformed_matching_records_fail_closed(self):
        records = [None, {}, {"plan_id": self.plan["plan_id"]},
                   self.observation(company=""), self.observation(verified=True),
                   self.observation(evidence=[])]
        for missing in ("plan_id", "source", "observed_at", "excerpt", "provenance"):
            record = self.observation()
            del record[missing]
            records.append(record)
        for record in records:
            with self.subTest(record=record):
                self.write_observations(self.observation(), record)
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)

    def test_original_timestamps_and_inclusive_freshness_boundaries(self):
        for observed_at in ("2026-09-16T19:00:00Z", "2026-09-17T19:00:00+00:00",
                            "2026-09-17T11:21:04-07:00"):
            with self.subTest(observed_at=observed_at):
                record = self.observation(observed_at=observed_at)
                self.write_observations(record)
                self.assertEqual(research_sources.load(self.root, self.plan, self.now), [record])

    def test_stale_future_naive_and_invalid_timestamps_fail_closed(self):
        for observed_at in ("2026-09-16T18:59:59Z", "2026-09-17T19:00:01Z",
                            "2026-09-17T18:59:00", "2026-09-17", "invalid", None,
                            "2026-09-31T18:59:00Z"):
            with self.subTest(observed_at=observed_at):
                self.write_observations(self.observation(observed_at=observed_at))
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)

    def test_sources_require_public_https(self):
        for source in ("http://engineering.vandelaysec.com/", "https://localhost/", "https://127.0.0.1/",
                       "https://example.com/", "https://user:password@engineering.vandelaysec.com/",
                       "https://engineering.vandelaysec.com:8443/", "https://private.internal/", ""):
            with self.subTest(source=source):
                record = self.observation(source=source)
                record["provenance"]["source"] = source
                self.write_observations(record)
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)

    def test_provenance_requires_exact_source_binding(self):
        for source in ("https://engineering.vandelaysec.com", "https://jobs.ashbyhq.com/Vandelay%20Security/"):
            with self.subTest(source=source):
                record = self.observation()
                record["provenance"]["source"] = source
                self.write_observations(record)
                with self.assertRaisesRegex(ValueError, "Exact-source"):
                    research_sources.load(self.root, self.plan, self.now)

    def test_provenance_requires_read_only_method_and_reference(self):
        changes = [{"method": "launchd"}, {"method": "browser_write"}, {"reference": " "},
                   {"reference": "x" * 2001}, {"unattended": True}]
        for change in changes:
            with self.subTest(change=change):
                record = self.observation()
                record["provenance"].update(change)
                self.write_observations(record)
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)

    def test_loading_does_not_promote_foreground_or_public_evidence(self):
        for method in ("foreground_browser_read_only", "public_page_read_only"):
            with self.subTest(method=method):
                record = self.observation(summary="Fixture facts only; history remains unknown")
                record["provenance"]["method"] = method
                self.write_observations(record)
                loaded = research_sources.load(self.root, self.plan, self.now)
                self.assertEqual(loaded, [record])
                self.assertEqual(loaded[0]["provenance"]["method"], method)
                for field in ("verified", "eligible", "history_cleared", "unattended"):
                    self.assertNotIn(field, loaded[0])
                    self.assertNotIn(field, loaded[0]["provenance"])

    def test_excerpt_and_optional_summary_limits(self):
        accepted = self.observation(excerpt="x" * 12000, summary="x" * 4000)
        self.write_observations(accepted)
        self.assertEqual(research_sources.load(self.root, self.plan, self.now), [accepted])
        for change in ({"excerpt": " "}, {"excerpt": "x" * 12001}, {"excerpt": None},
                       {"summary": " "}, {"summary": "x" * 4001}, {"summary": None}):
            with self.subTest(change=change):
                self.write_observations(self.observation(**change))
                with self.assertRaises(ValueError):
                    research_sources.load(self.root, self.plan, self.now)


if __name__ == "__main__":
    unittest.main()
