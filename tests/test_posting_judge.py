import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import tempfile
import unittest

import champion_composition
import conversion
import pipeline_routing as routing
import posting_judge
import research_workflow as research
import workflow_tasks
import test_pipeline_routing as fixtures


class Runtime:
    @staticmethod
    def research_command(root, config, output, schema):
        return ["synthetic-model"]


def model(verdict, quotes, calls=None):
    def executor(working, command, prompt, log, timeout):
        if calls is not None:
            calls.append(prompt)
        (Path(working) / "decision.json").write_text(json.dumps({"verdict": verdict, "reason": "judged", "quotes": quotes}))
    return executor


CONFIG = {"model": "synthetic", "codex": "synthetic"}


class PostingJudgeTests(unittest.TestCase):
    """The model's "same posting?" check that replaced a Python title comparison (2026-09-24)."""

    def scenario(self, title_on_page):
        case = fixtures.PipelineRoutingTests("test_official_raw_research_creates_context_and_claims_are_exact")
        case.setUp()
        self.addCleanup(case.doCleanups)
        role = fixtures.opportunity()
        proof = next(item for item in role["evidence"] if item["field"] == "role_title")
        proof.update(value=title_on_page, excerpt="Now hiring: " + title_on_page + ".")
        case.research(role)
        case.cycle()
        conversion.cycle(case.database, {}, fixtures.NOW)
        evidence_id = case.database.execute("SELECT evidence_id FROM conversion_evidence WHERE kind='ROLE_CONTEXT'").fetchone()[0]
        return case.database, role, evidence_id

    def judge(self, database, executor, **options):
        return posting_judge.cycle(Path(tempfile.mkdtemp()), database, CONFIG, executor=executor, runtime=Runtime, **options)

    def test_identical_evidence_spends_no_model_call(self):
        database, role, evidence_id = self.scenario(fixtures.opportunity()["role"])
        calls = []
        self.judge(database, model("SAME", [], calls))
        self.assertEqual([], calls)
        self.assertIsNone(posting_judge.verdict_for(database, evidence_id))

    def test_only_a_quoted_different_withholds_the_referral_summary_evidence(self):
        for verdict, withheld in (("DIFFERENT", True), ("SAME", False), ("UNSURE", False)):
            with self.subTest(verdict=verdict):
                database, role, evidence_id = self.scenario("Principal Firmware Engineer")
                rows = list(database.execute("SELECT * FROM rw_results"))
                binding = routing._bindings(database, rows, research.profile_key(role["people"][0]["profile"]), fixtures.NOW)[0]
                self.assertTrue(champion_composition.role_context(database, binding, fixtures.NOW)["evidence_id"])
                self.judge(database, model(verdict, ["Principal Firmware Engineer"]))
                evidence = champion_composition.role_context(database, binding, fixtures.NOW)["evidence_id"]
                self.assertEqual(withheld, evidence is None)

    def test_a_judged_input_is_never_asked_again(self):
        database, role, evidence_id = self.scenario("Principal Firmware Engineer")
        self.judge(database, model("SAME", ["Principal Firmware Engineer"]))
        calls = []
        self.judge(database, model("DIFFERENT", ["Principal Firmware Engineer"], calls))
        self.assertEqual([], calls)
        self.assertEqual("SAME", posting_judge.verdict_for(database, evidence_id))

    def test_a_compute_wait_is_not_an_attempt(self):
        database, role, evidence_id = self.scenario("Principal Firmware Engineer")
        with database:
            workflow_tasks.reserve_compute(database, "spend-the-day", fixtures.NOW, 10800)
        calls = []
        result = self.judge(database, model("SAME", [], calls), now=fixtures.NOW)
        self.assertEqual("WAITING_COMPUTE", result["items"][0]["state"])
        self.assertEqual([], calls)
        self.assertEqual(0, database.execute("SELECT COUNT(*) FROM posting_judgment_attempts").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
