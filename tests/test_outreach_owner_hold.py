"""Owner 2026-09-19: AI-written first-contact notes wait for the owner's exact-text approval,
and a role with no recorded specialization can still get an AI-composed draft."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import linkedin_contacts
import outreach_agent
import outreach_owner_approval
import outreach_promotion as promotion
import research_workflow as research
import workflow_tasks
from tests import test_outreach_composition as composition
from tests import test_outreach_promotion as promotion_fixture

NOW, PROFILE = promotion_fixture.NOW, promotion_fixture.PROFILE


def make_draft_without_specialization(self, need="reliable APIs", profile=PROFILE, recipient="Mock Person"):
    """The promotion fixture's draft, but research recorded no specialization for the role."""
    evidence = promotion_fixture.evidence
    person = {"recipient": recipient, "profile": profile, "employer_matches_target": True, "evidence": [
        evidence("identity", profile), evidence("employment", "Mock Company"),
        evidence("team", "API team"), evidence("ability_to_help", "Runs team hiring"),
        evidence("contact_type", "hiring_manager")]}
    role = {"company": "Mock Company", "role": "Staff Software Engineer",
            "url": "https://example.test/jobs/123", "requisition_id": "123", "rationale": "MOCK only",
            "people": [person], "evidence": [evidence("team_need", need)]}
    output = research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock",
                                       "blockers": [], "opportunities": [role], "drafts": []}, plan_id="plan", now=NOW)
    self.output = output
    return output["drafts"][0] if output["drafts"] else None


class OwnerHoldTests(unittest.TestCase):
    def setUp(self):
        self.fixture = promotion_fixture.PromotionTests("run_cycle")
        self.fixture.make_draft = types.MethodType(make_draft_without_specialization, self.fixture)
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database = self.fixture.database
        workflow_tasks.initialize(self.database)
        self.database.commit()
        patcher = patch.object(promotion, "resume_context", return_value=dict(composition.FAKE_RESUME))
        patcher.start()
        self.addCleanup(patcher.stop)

    def latest(self):
        return self.database.execute("SELECT * FROM rw_drafts ORDER BY version DESC LIMIT 1").fetchone()

    def test_draft_is_created_for_an_opportunity_without_specialization(self):
        opportunity = self.fixture.output["opportunities"][0]
        self.assertIsNone(opportunity["review"]["facts"]["specialization"])
        self.assertIsNotNone(self.fixture.draft, self.fixture.output["action_blockers"])
        self.assertEqual(self.fixture.draft["state"], "HELD_VERIFICATION")
        self.assertTrue(linkedin_contacts.draft_capable(opportunity, linkedin_contacts._claims(self.database),
                                                        research.timestamp(NOW)))

    @patch.object(promotion, "OWNER_APPROVAL_PER_NOTE", True)  # the per-note hold, when re-enabled
    def test_held_note_is_listed_and_only_the_owner_approved_exact_text_proceeds(self):
        def execute(config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            return json.dumps({"output_version": "outreach-compose-v1", "source_sha256": packet["source_sha256"],
                               "decision": "COMPOSED", "reason": "stub", "invitation": composition.GOOD,
                               "first_message": None, "proof_point": "L2",
                               "question": "What is the hardest part of your API work?", "facts": composition.FACTS})
        now = research.timestamp(NOW)
        composed = outreach_agent.compose_draft(promotion.ROOT, self.database, {"model": "stub"}, self.latest(), execute, now)
        self.assertEqual(composed["state"], "COMPOSED", composed)
        self.fixture.config = {"outreach_agent": {"compose": True}}
        self.assertEqual(self.fixture.run_cycle()["items"][0]["gates"], ["OWNER_APPROVAL_REQUIRED"])
        with tempfile.TemporaryDirectory() as root:
            pending = promotion.write_pending_owner_approvals(root, self.database)
            self.assertEqual(json.loads((Path(root) / promotion.PENDING_APPROVAL_REPORT).read_text()), pending)
        self.assertEqual([(item["recipient"], item["invitation"], item["invitation_sha256"], item["invitation_characters"])
                          for item in pending],
                         [("Mock Person", composition.GOOD, hashlib.sha256(composition.GOOD.encode()).hexdigest(),
                           len(composition.GOOD))])
        spec = dict(pending[0]["approval_template"], title="Engineering manager",
                    attestation={"instruction": "Owner: verified; never contacted", "attested_at": NOW,
                                 "verified_identity": True, "never_contacted": True})
        spec["approval"] = dict(spec["approval"], instruction="Yes, send this", approved_at=NOW)
        applied = outreach_owner_approval.run(self.database, spec, {}, now, apply=True)
        self.assertEqual(applied["state"], "APPROVED_AND_QUEUED_FOR_GUARDED_PATH", applied.get("blocker"))
        draft, unused, unused = promotion._validate(self.database, self.latest(), now, require_composition=True)
        self.assertEqual(draft["invitation"], composition.GOOD)
        self.assertEqual(promotion.pending_owner_approvals(self.database), [])
        # Past the owner hold: only the ordinary purpose/history proof is still missing.
        self.assertEqual(self.fixture.run_cycle()["items"][0]["gates"], ["OUTREACH_PURPOSE_UNPROVEN"])


if __name__ == "__main__":
    unittest.main()
