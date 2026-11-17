"""A résumé file control is filled with the designated résumé under any of its names (owner, 2026-11-16).

The real case: Databricks' form has a required file control "Resume/CV*". The read named it `resume_cv`;
the saved pointer is kept under `resume`; the exact-name lookup found nothing, the answering step skips
résumé fields by design, and after three empty tries the owner was asked to TYPE his résumé into a card.
"""
import json
import unittest

import applications
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import test_applications_e2e as fixtures
import test_owner_questions as owner_questions


def control(semantic, label="Resume/CV*", required=True):
    return {"id": None, "required": required, "semantic": semantic, "answer_type": "file", "control": "file",
            "label": label, "choices": []}


@unittest.skipUnless(owner_questions.DEPENDENCIES, "dashboard dependencies not installed")
class ResumeFieldNames(owner_questions.OwnerQuestions):
    def test_owner_resume_cv_is_filled_with_the_designated_resume_and_nobody_is_asked(self):
        databricks = self.discover(fixtures.role("Databricks", "databricks-1"))
        self.form({"databricks": [control("resume_cv")]})
        for hours in (0, 1.01, 2.02, 3.03):
            self.run_cycle(owner_questions.Model(), days=hours / 24)
            job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (databricks,)).fetchone())
            self.assertNotIn("ANSWERING_RETRY", job["blocker"] or "")
            self.assertNotIn("UNKNOWN_REQUIRED_ANSWERS", job["blocker"] or "")
            self.assertEqual(self.pending(databricks), [])

    def test_owner_every_resume_name_resolves_to_the_file_whatever_the_saved_pointer_is_called(self):
        policy = self.policy
        for semantic in sorted(applications.RESUME_SEMANTICS):
            question = control(semantic)
            answers, missing = applications.resolve_answers([question], policy)
            self.assertEqual(([], policy["resume"]["path"]), (missing, answers[applications.question_field(question)]), semantic)
        # A file control that is not the résumé (a cover letter) is still an ordinary question.
        letter = dict(control("cover_letter", "Cover letter*"))
        self.assertEqual(applications.resolve_answers([letter], policy), ({}, [applications.question_field(letter)]))

    def test_owner_a_job_already_waiting_on_a_resume_card_is_released_and_goes_on(self):
        databricks = self.discover(fixtures.role("Databricks", "databricks-1"))
        question = control("resume_cv")
        field = applications.question_field(question)
        self.form({"databricks": [question]})
        now = self.clock.isoformat()
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (databricks,)).fetchone()
        with self.database:   # the saved form read, as every job that reached a card has one
            self.database.execute("INSERT INTO application_reviews VALUES (?,?,?,?,?)",
                                  ("r1", databricks, now, "ELIGIBLE", json.dumps({"questions": [question]})))
        with self.database:   # the state 1.0.2 and earlier left behind: a text card for the file control
            applications.ask_owner(self.database, job, {field: dict(question, site_reason=applications.ANSWERING_CARD_REASON)},
                                   self.policy, now)
            cards = applications.open_cards(self.database, job, [field], now)
        applications.hold_for_owner_cards(self.database, job, [field], cards, [field], self.policy, now)
        self.assertEqual(len(self.pending(databricks)), 1)
        self.run_cycle(owner_questions.Model(), days=0.01)
        self.assertEqual(self.pending(databricks), [])
        state = self.database.execute("SELECT state FROM user_decisions WHERE job_key=?", (databricks,)).fetchone()[0]
        self.assertEqual(state, "SUPERSEDED")
        job = dict(self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (databricks,)).fetchone())
        self.assertNotIn("UNKNOWN_REQUIRED_ANSWERS", job["blocker"] or "")
        self.assertNotIn("ANSWERING_RETRY", job["blocker"] or "")

    def test_owner_a_text_box_under_a_resume_name_stays_a_question(self):
        # Review C1: "Paste your CV" is a text control; it never gets the file's path, and a card asked
        # for it is not withdrawn.
        paste = {"id": None, "required": True, "semantic": "cv", "answer_type": "text", "label": "Paste your CV*", "choices": []}
        policy = json.loads(json.dumps(self.policy))
        policy["answers"].pop("resume", None)
        self.assertEqual(applications.resolve_answers([paste], policy), ({}, [applications.question_field(paste)]))
        acme = self.discover(fixtures.role("Acme", "acme-1"))
        field = applications.question_field(paste)
        now = self.clock.isoformat()
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key=?", (acme,)).fetchone()
        with self.database:
            self.database.execute("INSERT INTO application_reviews VALUES (?,?,?,?,?)",
                                  ("r2", acme, now, "ELIGIBLE", json.dumps({"questions": [paste]})))
            applications.ask_owner(self.database, job, {field: paste}, self.policy, now)
            self.assertEqual(applications.withdraw_resume_cards(self.database, now), [])
        self.assertEqual(len(self.pending(acme)), 1)


def load_tests(loader, tests, pattern):
    """Only this file's own cases: the parent's are run from its own module."""
    own = [name for name in loader.getTestCaseNames(ResumeFieldNames) if name in vars(ResumeFieldNames)]
    return unittest.TestSuite(ResumeFieldNames(name) for name in own)


if __name__ == "__main__":
    unittest.main()
