"""A start-date question shown as a menu goes to the answering AI (review round 7, 2026-10-06:
the computed date came back after its menu was recorded, so Woodgrove's menu never reached the AI)."""
import json
from pathlib import Path
import unittest

import answering_agent
import applications
import manual_application_receipts as manual
import profile_home
import user_profile

MENU = {"id": "start", "label": "When could you start a new role?", "semantic": "earliest_start_date",
        "required": True, "answer_type": "select",
        "choices": ["Right away", "In about 2 weeks", "In over a month", "Open"]}


class StartMenuTests(unittest.TestCase):
    def setUp(self):
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def scoped(self):
        policy = json.loads(json.dumps(self.fixture.policy))
        policy.setdefault("answers", {})
        recorded = json.loads(user_profile.EXAMPLE_PROFILE.read_text())
        policy["answers"]["earliest_start_date_rule"] = recorded["answers"]["earliest_start_date_rule"]
        return policy

    def test_no_computed_date_is_written_for_a_menu(self):
        fixture, policy = self.fixture, self.scoped()
        if applications.earliest_start_floor(fixture.now, policy) is None:
            self.skipTest("fixture policy records no start rule")
        with fixture.database:
            applications.materialize_computed_answers(fixture.database, fixture.job(), [MENU], policy, fixture.now)
        field = applications.question_field(MENU)
        self.assertIsNone(fixture.database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?",
                                                   (fixture.job()["job_key"], field)).fetchone())
        plain = dict(MENU, choices=None)
        with fixture.database:
            applications.materialize_computed_answers(fixture.database, fixture.job(), [plain], policy, fixture.now)
        self.assertIsNotNone(fixture.database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?",
                                                      (fixture.job()["job_key"], field)).fetchone())

    def test_a_date_computed_before_the_menu_was_known_is_removed(self):
        fixture, policy = self.fixture, self.scoped()
        plain = dict(MENU, choices=None)
        field = applications.question_field(MENU)
        with fixture.database:
            applications.materialize_computed_answers(fixture.database, fixture.job(), [plain], policy, fixture.now)
        self.assertIn(field, policy["answers"])
        with fixture.database:
            applications.materialize_computed_answers(fixture.database, fixture.job(), [MENU], policy, fixture.now)
        self.assertNotIn(field, policy["answers"])
        self.assertIsNone(fixture.database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?",
                                                   (fixture.job()["job_key"], field)).fetchone())

    def test_a_menu_never_drops_an_owner_answer(self):
        fixture = self.fixture
        job = fixture.job()
        with fixture.database:
            fixture.database.execute("""INSERT OR IGNORE INTO application_requisition_records
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (job["job_key"], job["employer"], job["requisition"], "x", "x",
                "[]", None, "[]", "{}", 1, None, "[]", "x", fixture.now, fixture.now))
        question = {"id": "relocate", "label": "Are you willing to relocate?", "semantic": "willing_to_relocate",
                    "required": True, "answer_type": "choice"}
        field = applications.question_field(question)
        with fixture.database:
            fixture.database.execute("UPDATE application_requisition_records SET conditional_questions=? WHERE job_key=?",
                                     (json.dumps([question]), fixture.job()["job_key"]))
            fixture.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                                     (fixture.job()["job_key"], field, "willing_to_relocate", json.dumps("Yes"),
                                      "authenticated-owner-decision:test"))
            applications.record_conditional_questions(fixture.database, fixture.job(),
                                                      [dict(question, choices=["Yes", "No"])], fixture.now)
        self.assertIsNotNone(fixture.database.execute("SELECT 1 FROM application_answers WHERE job_key=? AND field=?",
                                                      (fixture.job()["job_key"], field)).fetchone())

    def test_another_fields_computed_date_never_answers_a_menu(self):
        policy = self.scoped()
        plain = dict(MENU, label="When can you start?", choices=None)
        policy["answers"][applications.question_field(plain)] = {
            "semantic": MENU["semantic"], "value": "2026-11-04", "source": applications.COMPUTED_ANSWER_SOURCE_PREFIX + "x"}
        answers, missing = applications.resolve_answers([MENU], policy)
        self.assertNotIn(applications.question_field(MENU), answers)

    def test_the_menu_is_asked_with_the_start_date(self):
        fixture, policy = self.fixture, self.scoped()
        floor = applications.earliest_start_floor(fixture.now, policy)
        if floor is None:
            self.skipTest("fixture policy records no start rule")
        result = answering_agent.answer(fixture.database, fixture.job(), [MENU], policy, fixture.now,
                                        root=fixture.root, config={"application_answering": {"enabled": False}})
        key = applications.question_field(MENU)
        self.assertEqual((result["unknown"], result["computed"]), ([key], {}))
        packed = answering_agent.packet_question(dict(MENU, earliest_start_date=floor), key)
        self.assertEqual((packed["earliest_start_date"], packed["choices"]), (floor, MENU["choices"]))
        self.assertNotIn("earliest_start_date", answering_agent.packet_question(MENU, key))


if __name__ == "__main__":
    unittest.main()
