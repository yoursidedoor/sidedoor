"""The note scaffold research drafts start from (learning removed, owner 2026-10-22).

Offline fixtures only: no provider, browser, model or production database."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from pathlib import Path
import sqlite3
import tempfile
import unittest

import outreach
import research_workflow as research
import strategy_execution as strategy
import test_research_workflow as fixtures


NOW = fixtures.NOW


class StrategyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture.sqlite3"
        self.database = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.database.close())
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        strategy.initialize(self.database)
        self.claim = "Built reliable APIs"
        research.register_claims(self.database, [{"text": self.claim, "tags": ["backend"]}],
                                 designated_source_text=self.claim, source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        self.companies = ["Example AI"] + [f"Fixture {number}" for number in range(24)]
        research.seed_history(self.database, {"companies": [{"company": company,
                              "conservative_known_contact_count": 0} for company in self.companies]},
                              history_complete=True)
        self.database.commit()
        research.plan_research(self.database, "plan", self.companies, now=NOW, limit=60)

    def draft(self, slug="fixture-person", company="Example AI", now=NOW, need=None):
        person = research.map_people([fixtures.person(name=slug + " Person", slug=slug, company=company)],
                                     company, now=now)[0]
        opportunity = fixtures.role(company)
        if need is not None:
            fixtures.change(opportunity, "team_need", need)
        opportunity = research.normalize_opportunity(opportunity, now=now)
        return research.create_draft(self.database, person, opportunity, plan_id="plan", now=now)

    def test_drafts_use_the_fixed_baseline_scaffold(self):
        draft = self.draft()
        self.assertEqual(draft["strategy_execution"]["variant"], strategy.BASELINE)
        self.assertIsNone(draft["strategy_execution"]["experiment_id"])
        self.assertEqual(draft["invitation"],
                         "Hi fixture-person—Built reliable APIs. I’m interested in your team’s work on reliable APIs. Open to connect?")
        self.assertEqual(draft["state"], "HELD_VERIFICATION")
        self.assertEqual(draft["history_status"], "UNKNOWN")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM strategy_drafts").fetchone()[0], 1)
        self.assertEqual(self.draft(), draft)  # the same input reuses the saved draft

    def test_size_limit_contact_cap_and_truthful_claim_mapping(self):
        for number in range(3):
            draft = self.draft("fixture-long-name-fixture-" + str(number) + "-0", need="verified need " * 14)
            self.assertIsNone(draft["invitation"])
            self.assertEqual(draft["invitation_state"], "BLOCKED_SIZE")
            self.assertEqual(draft["candidate_claim"]["text"], self.claim)
            self.assertEqual(draft["invitation_claim_mapping"]["source_claim_sha256"], research.digest(self.claim))
        blocked = self.draft("fourth-contact")
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM strategy_drafts").fetchone()[0], 3)

    def test_initialize_drops_legacy_bound_action_triggers(self):
        self.database.execute("""CREATE TRIGGER strategy_bound_action_no_delete BEFORE DELETE ON outreach_actions
            BEGIN SELECT RAISE(ABORT, 'Immutable bound strategy action'); END""")
        strategy.initialize(self.database)
        names = {row[0] for row in self.database.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertFalse(names & set(strategy.BOUND_ACTION_TRIGGERS))
        self.assertIn("strategy_drafts_no_update", names)


if __name__ == "__main__":
    unittest.main()
