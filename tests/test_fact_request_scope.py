"""What a saved Telegram decision is, and is not, invalidated by.

`user_decisions.policy_hash` and `user_decisions.job_hash` used to be digests of the whole
`profile.json` and of the whole job payload. On 2026-09-18 the owner expanded the
policy from 9 to 24 recorded answers and corrected `location_priority`; every outstanding
operational question (a question about the state of the owner's own machine) was immediately
invalidated and cancelled, although no owner answer, notice period or salary band can change
its answer. The browser-access questions that were the original case are deleted with the
browser permission handling (owner, 2026-09-30); the unreadable-form escalation is the
operational question that remains.

These tests pin both directions of the fix:

  * an unrelated policy or job-payload edit leaves an outstanding question answerable, and
  * an edit that genuinely bears on the question still invalidates it.

The second direction is the one that matters, so each "still invalidates" case names a concrete
mechanism: the policy now answers this exact question itself, the floor an answered start date
is validated against moved, the designated resume moved, or the posting behind the job_key was
replaced.
"""

import copy
import datetime as dt
import json
import unittest

import applications
import decision_transport
import notifications
import orchestrator
import test_applications_e2e as fixtures
from test_applications_e2e import MockBridge, MockTelegram, NOW
import profile_home
import user_profile


# The exact 2026-09-18 policy edit that caused the reported defect, reconstructed from the
# shipped policy file: fifteen new recorded answers, one new top-level block, reworded
# provenance and a corrected `location_priority`. `policy_before_the_20260918_edit` subtracts
# it, so "before" and "after" here are the two real documents, not an invented pair.
ANSWERS_ADDED_20260918 = ("earliest_start_date_rule", "eu_work_authorized", "notice_period",
                          "open_to_visa_sponsorship", "preferred_work_countries_outside_us",
                          "relocation_destinations", "remote_work_from_us_for_foreign_employer",
                          "requires_visa_sponsorship_outside_us", "requires_visa_sponsorship_us",
                          "resume", "uk_work_authorized", "willing_to_relocate",
                          "work_authorization_by_jurisdiction", "work_location_declaration_rule",
                          "working_timezone")
LOCATION_PRIORITY_BEFORE = ["Springfield, IL hybrid/onsite", "US remote",
                            "US except Washington state/San Francisco hybrid/onsite",
                            "Europe remote: country authorization unresolved"]
SOURCE_BEFORE = "handoff/resolved-agent-config.yaml; fieldwise reconciliation 2026-09-17"


def policy_before_the_20260918_edit(policy):
    older = copy.deepcopy(policy)
    older.pop("onsite_location_exclusions", None)
    for key in ANSWERS_ADDED_20260918:
        older["answers"].pop(key, None)
    older["location_priority"] = LOCATION_PRIORITY_BEFORE
    older["source"] = SOURCE_BEFORE
    return older


OPERATIONAL_FIELD = decision_transport.FORM_NOT_READABLE_FIELD
OPERATIONAL_SEMANTIC = decision_transport.FORM_NOT_READABLE_SEMANTIC
OPERATIONAL_QUESTION = decision_transport.FORM_NOT_READABLE_QUESTION


class ScopeProjectionTests(unittest.TestCase):
    """Fingerprint sensitivity, at the projection level, with no database involved."""

    def setUp(self):
        self.policy = user_profile.load()

    def policy_hash(self, field, semantic, policy=None):
        return decision_transport.scoped_policy_hash("fact", field, semantic, policy or self.policy)

    def edited(self, mutate):
        policy = copy.deepcopy(self.policy)
        mutate(policy)
        return policy

    def test_the_semantic_sets_still_agree_with_applications(self):
        # Copied rather than imported, because `applications` imports `decision_transport`.
        # If they ever diverge, a decision's scope silently stops matching what
        # `resolve_answers` and `materialize_computed_answers` actually consult.
        self.assertEqual(decision_transport.RESUME_SEMANTICS, applications.RESUME_SEMANTICS)
        self.assertEqual(decision_transport.EARLIEST_START_SEMANTICS, applications.EARLIEST_START_SEMANTICS)
        self.assertEqual(decision_transport.EARLIEST_START_RULE_SEMANTIC, applications.EARLIEST_START_RULE_SEMANTIC)

    def test_every_decision_class_is_recognised_from_its_own_identity(self):
        self.assertEqual(decision_transport.decision_scope("test", "transport_ok", "harmless_transport_test"), "transport")
        self.assertEqual(decision_transport.decision_scope(
            "fact", decision_transport.FORM_NOT_READABLE_FIELD, decision_transport.FORM_NOT_READABLE_SEMANTIC), "operational")
        self.assertEqual(decision_transport.decision_scope("fact", "citizenship", "citizenship_country"), "form_answer")

    def test_no_policy_edit_at_all_can_invalidate_an_operational_question(self):
        # The categorical claim the operational scope makes, exercised against the real
        # 2026-09-18 edit and against every top-level key the policy file carries.
        base = self.policy_hash(OPERATIONAL_FIELD, OPERATIONAL_SEMANTIC)
        self.assertEqual(base, self.policy_hash(OPERATIONAL_FIELD, OPERATIONAL_SEMANTIC, policy_before_the_20260918_edit(self.policy)))
        for key in sorted(self.policy):
            with self.subTest(key=key):
                self.assertEqual(base, self.policy_hash(OPERATIONAL_FIELD, OPERATIONAL_SEMANTIC,
                                                        self.edited(lambda policy, key=key: policy.pop(key))))
        self.assertEqual(base, self.policy_hash(OPERATIONAL_FIELD, OPERATIONAL_SEMANTIC,
                                                self.edited(lambda policy: policy.update(a_brand_new_policy_block={"x": 1}))))

    def test_a_form_answer_question_is_stable_across_the_real_20260918_edit(self):
        for field, semantic in (("citizenship", "citizenship_country"), ("why_us", "motivation_free_text"),
                                ("gender", "gender_identity")):
            with self.subTest(field=field):
                self.assertEqual(self.policy_hash(field, semantic),
                                 self.policy_hash(field, semantic, policy_before_the_20260918_edit(self.policy)))

    def test_a_form_answer_question_ignores_application_only_and_targeting_edits(self):
        field, semantic = "citizenship", "citizenship_country"
        base = self.policy_hash(field, semantic)
        unrelated = {
            "new unrelated answer": lambda policy: policy["answers"].update(
                pets={"value": "two", "semantic": "number_of_pets", "source": "owner"}),
            "notice_period value": lambda policy: policy["answers"]["notice_period"].update(value="4 weeks"),
            "current_location value": lambda policy: policy["answers"]["current_location"].update(value="Seattle, WA, USA"),
            "answer provenance reworded": lambda policy: policy["answers"]["current_location"].update(source="reworded"),
            "top-level source reworded": lambda policy: policy.update(source="reworded provenance"),
            "location_priority": lambda policy: policy.update(location_priority=["Mars remote"]),
            "onsite_location_exclusions": lambda policy: policy.update(onsite_location_exclusions={"states": ["XX"]}),
            "industries": lambda policy: policy.update(industries=["Underwater basket weaving"]),
            "levels": lambda policy: policy.update(levels=["Intern"]),
            # Same bucket as `levels`: a scope target, not an answer to any form question.
            "minimum_years_experience": lambda policy: policy.update(minimum_years_experience=99),
            "funding_stages": lambda policy: policy.update(funding_stages=["Series Z"]),
            "minimum_base_salary_usd": lambda policy: policy.update(minimum_base_salary_usd=1),
            "desired_base_salary_usd": lambda policy: policy.update(desired_base_salary_usd=1),
            "max_attempts": lambda policy: policy.update(max_attempts=99),
            "max_reconcile_attempts": lambda policy: policy.update(max_reconcile_attempts=99),
            "retry_delays_seconds": lambda policy: policy.update(retry_delays_seconds=[1, 2, 3]),
            "preapproved_applications": lambda policy: policy.update(preapproved_applications=False),
            "referrals_block_applications": lambda policy: policy.update(referrals_block_applications=True),
            "resume for a non-resume question": lambda policy: policy["resume"].update(path="elsewhere.pdf"),
        }
        for name, mutate in unrelated.items():
            with self.subTest(edit=name):
                self.assertEqual(base, self.policy_hash(field, semantic, self.edited(mutate)))

    def test_a_form_answer_question_still_rotates_on_an_edit_that_bears_on_it(self):
        relevant = {
            # The policy now answers this exact question itself, by semantic or by field key.
            ("citizenship", "citizenship_country"): lambda policy: policy["answers"].update(
                nationality={"value": "United States", "semantic": "citizenship_country", "source": "owner"}),
            ("notice_period", "notice_period"): lambda policy: policy["answers"]["notice_period"].update(value="4 weeks"),
            # Dropping the provenance makes `resolve_answers` treat a recorded answer as absent.
            ("working_timezone", "working_timezone"): lambda policy: policy["answers"]["working_timezone"].pop("source"),
            # The floor an answered start date is validated against moved.
            ("start", "earliest_start_date"): lambda policy: policy["answers"]["earliest_start_date_rule"]["value"].update(offset_days=90),
            # The only value a resume answer may take moved.
            ("cv_upload", "resume"): lambda policy: policy["resume"].update(path="another.pdf"),
            # A future policy field nobody has classified yet: fail closed.
            ("citizenship", "citizenship_country"): lambda policy: policy.update(new_required_answer_rule={"x": 1}),
        }
        for (field, semantic), mutate in relevant.items():
            with self.subTest(field=field, semantic=semantic):
                self.assertNotEqual(self.policy_hash(field, semantic),
                                    self.policy_hash(field, semantic, self.edited(mutate)))

    def test_every_key_in_the_shipped_policy_is_deliberately_classified(self):
        """A release guard, not a unit test.

        The fail-closed bucket is correct - a policy field nobody has classified must rotate the
        hash rather than be silently ignored - but its cost is that adding a key to
        `profile.json` invalidates every outstanding required-answer question until
        somebody classifies it here. That is the defect this whole patch exists to fix, so it
        must not be reintroduced by the next policy edit.

        This caught exactly that once already: `salary_relaxation`, added for the euro/pound
        salary relaxation, was unclassified and would have invalidated every pending question the
        moment it landed in the policy file. If this test fails it will name the new key; decide
        whether it can change the answer to a required question, then add it to
        `CLASSIFIED_POLICY_KEYS` (and to the EXCLUDED table) or handle it in `decision_policy`.
        """
        unclassified = sorted(set(self.policy) - set(decision_transport.CLASSIFIED_POLICY_KEYS))
        self.assertEqual(unclassified, [], "unclassified profile.json keys: "
                         + ", ".join(unclassified) + " - see the docstring of this test")

    def test_the_salary_relaxation_knob_does_not_invalidate_a_pending_question(self):
        # The specific regression: tuning the salary tolerance is an eligibility change, enforced
        # by `applications.assess` on every pass, and must leave an outstanding question alone.
        field, semantic = "citizenship", "citizenship_country"
        base = self.policy_hash(field, semantic)
        for tolerance in (0.0, 0.25, 0.5):
            with self.subTest(tolerance=tolerance):
                self.assertEqual(base, self.policy_hash(field, semantic, self.edited(
                    lambda policy, tolerance=tolerance: policy.update(
                        salary_relaxation={"tolerance_below_minimum": tolerance}))))

    def test_an_unknown_top_level_policy_key_fails_closed_for_a_form_answer(self):
        base = self.policy_hash("citizenship", "citizenship_country")
        self.assertNotEqual(base, self.policy_hash("citizenship", "citizenship_country",
                                                   self.edited(lambda policy: policy.update(new_gate={"required": True}))))

    def test_job_hash_follows_the_posting_identity_not_the_research_prose(self):
        payload = {"company": "Wernham", "role": "Staff Software Engineer, Distributed Systems",
                   "url": "https://jobs.ashbyhq.com/wernham/a0000002", "requisition_id": "a0000002",
                   "rationale": "long research prose", "assessment": {"fit": "strong"},
                   "evidence": ["a"], "people": [], "ranking": 1, "unknowns": ["x"], "review": {}, "source": "careers page"}
        for field, semantic in ((OPERATIONAL_FIELD, OPERATIONAL_SEMANTIC), ("citizenship", "citizenship_country")):
            base = decision_transport.scoped_job_hash("fact", field, semantic, payload)
            for key in ("rationale", "assessment", "evidence", "people", "ranking", "unknowns", "review", "source"):
                with self.subTest(field=field, stable=key):
                    self.assertEqual(base, decision_transport.scoped_job_hash(
                        "fact", field, semantic, dict(payload, **{key: "rewritten by a later pass"})))
            for key in ("company", "role", "url", "requisition_id"):
                with self.subTest(field=field, rotates=key):
                    self.assertNotEqual(base, decision_transport.scoped_job_hash(
                        "fact", field, semantic, dict(payload, **{key: "replaced posting"})))


class OperationalQuestionSurvivesPolicyGrowthTests(unittest.TestCase):
    """The reported defect, end to end, through the real `decision_transport.cycle`."""

    setUp = fixtures.ApplicationEndToEndTests.setUp
    discover = fixtures.ApplicationEndToEndTests.discover
    job = fixtures.ApplicationEndToEndTests.job

    def ask(self, policy, question=OPERATIONAL_QUESTION):
        profile_home.use(self, policy)
        self.discover()
        job = self.job()
        with self.database:
            applications.hold(self.database, job, applications.FORM_NOT_READABLE_BLOCKER,
                              applications.FORM_NOT_READABLE_ACTION, NOW, "BLOCKED",
                              "2026-09-25T16:00:00+00:00")
            identifier = decision_transport.request_fact(self.database, self.job(), question, policy, NOW)
        self.bridge = MockBridge()
        self.config = {"bridge_transport": {"owner_id": "mock-owner"}}
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        notifications.dispatch(self.database, {}, MockTelegram(), NOW)
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.decision = dict(self.database.execute("SELECT * FROM user_decisions WHERE id=?", (identifier,)).fetchone())
        self.assertEqual(self.decision["state"], "PENDING")
        self.assertTrue(self.decision["message_id"])
        return self.decision

    def install(self, policy):
        profile_home.use(self, policy)

    def state(self):
        return self.database.execute("SELECT state,last_error FROM user_decisions WHERE id=?", (self.decision["id"],)).fetchone()

    def notification(self):
        return self.database.execute("SELECT state FROM notification_outbox WHERE event_key=?",
                                     (self.decision["event_key"],)).fetchone()[0]

    def reply(self, raw="I signed in, try again"):
        self.bridge.events = [{"seq": 1, "decision_id": self.decision["id"], "owner": "mock-owner",
                               "action_id": self.decision["job_key"], "payload_hash": self.decision["payload_hash"],
                               "response": {"status": "answered", "raw": raw}}]
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        return self.database.execute("SELECT state FROM transport_inbox ORDER BY seq DESC LIMIT 1").fetchone()[0]

    def test_the_20260918_policy_growth_no_longer_cancels_an_operational_question(self):
        self.ask(policy_before_the_20260918_edit(self.policy))
        self.install(self.policy)
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.assertEqual(tuple(self.state()), ("PENDING", None))
        self.assertEqual(self.notification(), "DELIVERED")
        self.assertEqual(self.reply(), "APPLIED")
        answer = self.database.execute("SELECT value,source FROM application_answers WHERE field=?", (OPERATIONAL_FIELD,)).fetchone()
        self.assertEqual(answer["source"], "authenticated-owner-decision:" + self.decision["id"])

    def test_a_rewritten_research_rationale_no_longer_cancels_an_operational_question(self):
        self.ask(self.policy)
        with self.database:
            payload = dict(json.loads(self.job()["payload"]), rationale="Re-scraped; new evidence and ranking")
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?",
                                  (json.dumps(payload), self.decision["job_key"]))
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.assertEqual(tuple(self.state()), ("PENDING", None))
        self.assertEqual(self.reply(), "APPLIED")

    def test_a_replaced_posting_still_cancels_an_operational_question(self):
        # The converse for the operational scope. No policy edit can invalidate this question,
        # so the guard that must still fire is posting identity: a different role or url behind
        # the same job_key means the question was about something else.
        self.ask(self.policy)
        with self.database:
            payload = dict(json.loads(self.job()["payload"]), url="https://jobs.example.invalid/another-posting")
            self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?",
                                  (json.dumps(payload), self.decision["job_key"]))
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.assertEqual(tuple(self.state()), ("STALE", "FACT_REQUEST_REQUIRES_FRESH_JOB_REVIEW"))
        # The outbox cancel only moves PENDING/WAITING rows, so an already-delivered Telegram
        # message is never withdrawn - it just stops being answerable. That is exactly what the
        # owner saw for messages 23, 24, 25 and 33 in production.
        self.assertEqual(self.notification(), "DELIVERED")
        self.assertEqual(self.reply(), "REJECTED_STALE_OR_MISMATCH")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_answers WHERE field=?",
                                               (OPERATIONAL_FIELD,)).fetchone()[0], 0)

    def test_a_job_that_left_the_blocked_state_still_cancels_the_question(self):
        # The discriminator that correctly staled Telegram message 32 in production: the guard
        # is not only about hashes, and narrowing the hashes did not remove it.
        self.ask(self.policy)
        with self.database:
            self.database.execute("UPDATE application_jobs SET state='WAITING_BROWSER' WHERE job_key=?",
                                  (self.decision["job_key"],))
        decision_transport.cycle(self.database, self.config, self.root, self.bridge, NOW)
        self.assertEqual(tuple(self.state()), ("STALE", "FACT_REQUEST_REQUIRES_FRESH_JOB_REVIEW"))

    def test_an_unrelated_policy_edit_no_longer_asks_the_owner_the_same_thing_twice(self):
        # `request_fact` dedupes on the payload hash; when that carried the whole policy
        # document, the next pass after any edit inserted a second identical question.
        decision = self.ask(policy_before_the_20260918_edit(self.policy))
        self.install(self.policy)
        with self.database:
            repeated = decision_transport.request_fact(self.database, self.job(), OPERATIONAL_QUESTION, self.policy, NOW)
        self.assertEqual(repeated, decision["id"])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM user_decisions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
