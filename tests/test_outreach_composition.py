"""Outreach text comes from the AI composer or an exact owner approval, never the template.

Python enforces only hard lines on structured output: the 300-character note cap,
every cited fact found in its source, and every number cited.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import hashlib
import json
import unittest
from unittest.mock import patch

import orchestrator
import outreach_agent
import outreach_owner_approval
import outreach_promotion as promotion
import workflow_tasks
from tests import test_outreach_promotion as promotion_fixture

NOW, PROFILE = promotion_fixture.NOW, promotion_fixture.PROFILE


RESUME = "Senior engineer\nBuilt reliable APIs serving 40k requests per second at Example Cloud\n"
FAKE_RESUME = {"text": RESUME, "source_sha256": "a" * 64, "text_sha256": hashlib.sha256(RESUME.encode()).hexdigest()}
GOOD = "Hi Mock, I built reliable APIs serving 40k requests per second at Example Cloud. What is the hardest part of your API work?"
FACTS = [{"note_span": "reliable APIs serving 40k requests per second at Example Cloud", "source_kind": "resume",
          "source_ref": "L2", "source_excerpt": "reliable APIs serving 40k requests per second at Example Cloud"}]


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = promotion_fixture.PromotionTests("run_cycle")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database = self.fixture.database
        workflow_tasks.initialize(self.database)
        self.database.commit()
        patcher = patch.object(promotion, "resume_context", return_value=dict(FAKE_RESUME))
        patcher.start()
        self.addCleanup(patcher.stop)

    def scaffold(self):
        return self.database.execute("SELECT * FROM rw_drafts ORDER BY version DESC LIMIT 1").fetchone()

    def compose(self, invitation, facts):
        def execute(config, prompt, schema, timeout):
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            return json.dumps({"output_version": "outreach-compose-v1", "source_sha256": packet["source_sha256"],
                               "decision": "COMPOSED", "reason": "stub", "invitation": invitation, "first_message": None,
                               "proof_point": "L2", "question": "What is the hardest part of your API work?", "facts": facts})
        return outreach_agent.compose_draft(promotion.ROOT, self.database, {"model": "stub"}, self.scaffold(), execute, NOW)

    def good_execute(self, first_raw=None):
        calls = []

        def execute(config, prompt, schema, timeout):
            calls.append(prompt)
            if first_raw is not None and len(calls) == 1:
                return first_raw
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            return json.dumps({"output_version": "outreach-compose-v1", "source_sha256": packet["source_sha256"],
                               "decision": "COMPOSED", "reason": "stub", "invitation": GOOD, "first_message": None,
                               "proof_point": "L2", "question": "What is the hardest part of your API work?", "facts": FACTS})
        return execute, calls

    def test_a_passed_deadline_never_ends_note_writing(self):
        """Owner, 2026-10-02: "Remove the deadline from note writing, so only real tries can end it,
        and reopen the 5 expired drafts". A compute wait spends no try; the old 24 h deadline then
        closed the task anyway. Such a task is reopened with its attempts kept and composes."""
        now = dt.datetime.fromisoformat(NOW)
        config = {"model": "stub"}
        with self.database:
            self.assertTrue(workflow_tasks.reserve_compute(self.database, "synthetic-full", NOW, 10800))
        execute, calls = self.good_execute()
        waited = outreach_agent._compose_step(promotion.ROOT, self.database, config, self.scaffold(), execute, now)
        self.assertEqual((waited["state"], waited["attempts"], waited["max_attempts"]), ("RETRY_WAIT", 1, 8))
        self.assertEqual(calls, [])
        # What the deadline did on 2026-10-03 (five drafts): EXHAUSTED with tries left.
        with self.database:
            self.database.execute("UPDATE workflow_tasks SET status='EXHAUSTED',blocker='DEADLINE_OR_RETRY_LIMIT',next_at=NULL "
                                  "WHERE task_id=?", (waited["task_id"],))
        later = now + dt.timedelta(days=2)
        with patch("outreach_provider._now", return_value=later.isoformat()):
            done = outreach_agent._compose_step(promotion.ROOT, self.database, config, self.scaffold(), execute, later)
        self.assertEqual((done["state"], done["attempts"], done["max_attempts"]), ("COMPLETE", 2, 8), done)
        self.assertEqual(done["composition"], "COMPOSED")
        events = [row[0] for row in self.database.execute("SELECT event FROM workflow_task_events WHERE task_id=? ORDER BY id",
                                                          (waited["task_id"],))]
        self.assertIn("REOPENED_NO_DEADLINE", events)

    def test_a_successful_repair_turn_returns_its_unused_compute(self):
        """2026-10-02: repair turns were booked at the full timeout and never settled, unlike the
        main call of the same completed composition (6 x 600 s kept on 09-29..10-02)."""
        execute, calls = self.good_execute(first_raw=json.dumps({"not": "valid"}))
        composed = outreach_agent.compose_draft(promotion.ROOT, self.database, {"model": "stub"}, self.scaffold(), execute, NOW)
        self.assertEqual(composed["state"], "COMPOSED", composed)
        self.assertEqual(len(calls), 2)
        refunds = self.database.execute("SELECT seconds FROM worker_compute_refunds WHERE action_id LIKE 'outreach-compose-repair:%'").fetchall()
        self.assertEqual(len(refunds), 1)
        self.assertGreater(refunds[0][0], 0)
        # The decision is still filed under the draft's own audit key, not a repair booking's.
        observation, decision = (self.database.execute(
            "SELECT action_id FROM outreach_agent_audit WHERE kind=?", (kind,)).fetchone()[0] for kind in ("OBSERVATION", "DECISION"))
        self.assertEqual(decision, observation)

    def test_a_retry_on_a_later_budget_day_calls_the_model(self):
        """2026-10-02 review: the booking key was the observation digest alone, so a retry of an
        unchanged scaffold on a later budget day was refused by reserve_compute ("Compute
        reservation identity cannot change"): a try spent, no model call, until EXHAUSTED."""
        now = dt.datetime.fromisoformat(NOW)
        config = {"model": "stub"}
        calls = []

        def failing(config, prompt, schema, timeout):
            calls.append("fail")
            raise outreach_agent.reply_worker.Held("SUBSCRIPTION_EXECUTOR_FAILED", retry=True)

        first = outreach_agent._compose_step(promotion.ROOT, self.database, config, self.scaffold(), failing, now)
        self.assertEqual((first["state"], first["attempts"], calls), ("RETRY_WAIT", 1, ["fail"]))
        execute, good = self.good_execute()
        later = now + dt.timedelta(days=1)
        with patch("outreach_provider._now", return_value=later.isoformat()):
            done = outreach_agent._compose_step(promotion.ROOT, self.database, config, self.scaffold(), execute, later)
        self.assertEqual(len(good), 1, done)
        self.assertEqual((done["state"], done["composition"], done["attempts"]), ("COMPLETE", "COMPOSED", 2))

    @patch.object(promotion, "OWNER_APPROVAL_PER_NOTE", True)  # the per-note hold, when re-enabled
    def test_an_untraceable_metric_is_recorded_and_no_longer_refuses_the_note(self):
        """Owner, 2026-09-23: "remove every single piece of python code that compares things
        and forces the OUTREACH TO FAIL ... Remove it all. I dont care."

        The trace is still computed and stored with the decision, so what the model claimed and
        which claims Python could not line up stay auditable. It simply no longer decides.
        """
        metric = GOOD.replace("What is", "We served 10M users. What is")
        noted = self.compose(metric, FACTS)
        self.assertEqual(noted["state"], "COMPOSED", noted)
        self.assertEqual(noted.get("gates", []), [], "an untraceable number no longer refuses the note")
        self.assertEqual(json.loads(self.scaffold()["payload"])["invitation"], metric)
        # Owner 2026-09-19: an AI-written note to a new (research-found) person is never sent
        # without the owner's approval of its exact text - refused before history checks and
        # at action creation.
        with self.assertRaisesRegex(ValueError, "^OWNER_APPROVAL_REQUIRED$"):
            promotion._validate(self.database, self.scaffold(), promotion.research.timestamp(NOW), require_composition=True)
        with self.assertRaisesRegex(ValueError, "^OWNER_APPROVAL_REQUIRED$"):
            promotion._action(self.database, {"outreach_promotion_contexts": {PROFILE: {"purpose": "PREPARE_CAMPAIGN"}}},
                              json.loads(self.scaffold()["payload"]), PROFILE, {}, {}, promotion.research.timestamp(NOW))
        self.fixture.config = {"outreach_agent": {"compose": True}}
        self.assertEqual(self.fixture.run_cycle()["items"][0]["gates"], ["OWNER_APPROVAL_REQUIRED"])

    def test_generated_dashes_are_corrected_and_the_note_still_goes_out(self):
        """Owner, 2026-09-23: "Remove it all. I dont care."

        The owner's style rule of 2026-09-19 is that generated outreach carries no em or en
        dash. Refusing the whole note over one character spent an attempt and sent nothing, so
        the character is corrected and the note proceeds. The rule is about what goes out.
        """
        composed = self.compose(GOOD.replace("Mock, I", "Mock\u2014I"), FACTS)
        self.assertEqual(composed["state"], "COMPOSED", composed)
        text = json.loads(self.scaffold()["payload"])["invitation"]
        self.assertNotIn("\u2014", text, "the em dash is corrected, not sent")
        self.assertNotIn("\u2013", text)
        # Both dash characters are corrected the same way.
        self.assertNotIn("\u2013", promotion.without_dashes("Mock \u2013 I"))
        self.assertNotIn("\u2014", promotion.without_dashes("Mock\u2014I"))
        self.assertEqual(promotion.dash_gates({"invitation": text}), [],
                         "no dash remains for the old rule to fire on")

    def test_composer_sees_whether_each_recipient_record_was_verified(self):
        """Owner, 2026-10-01: research people are drafted without verified evidence, so the
        composer is told which recipient records are verified. Recorded, never a gate."""
        row = self.scaffold()
        draft = json.loads(row["payload"])
        draft["person_evidence"]["employment"] = dict(draft["person_evidence"]["employment"], verified=False)
        packet = outreach_agent.composition_packet(self.database, draft, row["identity"], promotion.ROOT)[0]
        seen = packet["recipient"]
        self.assertNotIn("verified_evidence", seen)
        self.assertIs(seen["evidence"]["employment"]["verified"], False)
        self.assertTrue(all(record["verified"] for field, record in seen["evidence"].items() if field != "employment"))

    def test_own_unsent_reserved_draft_is_not_seeded_as_prior_contact(self):
        """The drafts mirror of our own research draft must not make the recipient historical."""
        self.database.executescript("""CREATE TABLE history (identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            company TEXT NOT NULL, profile TEXT, evidence TEXT NOT NULL);
            CREATE TABLE drafts (identity TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            company TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);""")
        mirror = json.loads(self.scaffold()["payload"])
        self.database.execute("INSERT INTO drafts VALUES (?,?,?,'HELD_VERIFICATION',?)",
                              (outreach_agent.profile_key(PROFILE), "Mock Person", "Mock Company", json.dumps(mirror)))
        self.database.execute("INSERT INTO history VALUES ('name:sent person','Sent Person','Mock Company',"
                              "'https://www.linkedin.com/in/sent-person','{}')")
        replied = {"recipient": "Replied Person", "company": "Other Co", "profile": "https://www.linkedin.com/in/replied"}
        self.database.execute("INSERT INTO outreach_actions VALUES ('reply:x','linkedin.com/in/replied','REPLY:x',?,'h',"
                              "'CONFIRMED_SENT',NULL,NULL)", (json.dumps(dict(replied, purpose="REPLY")),))
        self.database.commit()
        promotion.research.seed_history(self.database, {"companies": []}, orchestrator.prior_contacts(self.database),
                                        history_complete=True)
        flags = dict(self.database.execute("SELECT name,historical FROM rw_contacts"))
        self.assertEqual(flags, {"mock person": 0, "sent person": 1, "replied person": 1})
        # 1 historical (Sent Person) + 1 reserved (Mock Person still uses a slot) + 2 held plan slots.
        self.assertEqual(promotion.research.contact_usage(self.database, "mock company"), 4)

    def test_template_text_is_not_sendable_once_composition_is_enabled(self):
        self.fixture.config = {"outreach_agent": {"compose": True}}
        item = self.fixture.run_cycle()["items"][0]
        self.assertEqual(item["gates"], ["AI_COMPOSITION_REQUIRED"])

    def test_campaign_preparation_refuses_template_text_when_composition_is_required(self):
        import outreach_campaign
        row = self.scaffold()
        draft, unused, verification = promotion._validate(self.database, row, promotion.research.timestamp(NOW))
        candidate = {"route": "INVITATION", "profile": PROFILE, "draft_source": promotion._source(row),
                     "draft": draft, "verification": verification, "proof": {}}
        prepared = outreach_campaign.prepare_candidates(self.database, {"outreach_agent": {"compose": True}}, None, [candidate],
                                                        promotion.research.timestamp(NOW))
        self.assertEqual(prepared["items"][0]["blocker"], "AI_COMPOSITION_REQUIRED")

    def test_owner_approval_dry_run_writes_nothing_and_apply_binds_the_exact_text(self):
        spec = {"profile": PROFILE, "recipient": "Mock Person", "company": "Mock Company", "title": "Engineering manager",
                "attestation": {"instruction": "Owner: verified; never contacted", "attested_at": NOW,
                                "verified_identity": True, "never_contacted": True},
                "approval": {"instruction": "Yes", "approved_at": NOW, "field": "invitation", "text": GOOD, "facts": FACTS},
                "correct_self_reference": False}
        now = promotion.research.timestamp(NOW)
        dry = outreach_owner_approval.run(self.database, spec, {}, now)
        self.assertEqual(dry["state"], "WOULD_APPROVE_AND_QUEUE", dry.get("blocker"))
        self.assertEqual(self.scaffold()["version"], 1)
        self.assertIsNone(self.database.execute("SELECT 1 FROM sqlite_master WHERE name='outreach_owner_records'").fetchone())
        applied = outreach_owner_approval.run(self.database, spec, {}, now, apply=True)
        self.assertEqual(applied["state"], "APPROVED_AND_QUEUED_FOR_GUARDED_PATH", applied.get("blocker"))
        draft, unused, verification = promotion._validate(self.database, self.scaffold(), now, require_composition=True)
        self.assertEqual((draft["invitation"], draft["owner_approval"]["text_sha256"]),
                         (GOOD, hashlib.sha256(GOOD.encode()).hexdigest()))
        self.assertTrue(verification["identity_source"].startswith("outreach_owner_records:"))


if __name__ == "__main__":
    unittest.main()
