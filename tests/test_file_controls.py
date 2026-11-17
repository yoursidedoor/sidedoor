"""File controls and start dates outside the recorded names (owner picks, 2026-11-16: S1 to S4).

S1: a résumé upload the read named outside the six names is filled with the designated résumé from the
answering AI's `file_role`, never asked of the user. S2: a required document Sidedoor does not have gets a
FILE card (never a text box), the uploaded file is attached as it is and hash-checked, never rendered.
S3: a start-date question under any semantic is answered from the recorded earliest start. S4: a
certificate answered with the résumé pointer attaches the file the user chose in Settings (the original),
never a PDF of the pointer's path.
"""
import hashlib
import json
import unittest
from pathlib import Path

import applications
import decision_transport
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import user_profile
import user_uploads
import test_applications_e2e as fixtures
import test_owner_questions as owner_questions
from answering_fixtures import is_judge_prompt
from test_answering_agent import PACKET_MARKER, entry


def control(semantic, label, answer_type="file", required=True):
    return {"id": None, "required": required, "semantic": semantic, "answer_type": answer_type,
            "control": "file" if answer_type == "file" else "text", "label": label, "choices": []}


START_SEMANTICS = ("earliest_start", "start_availability", "desired_start_date")


class FileModel(owner_questions.Model):
    """The answering AI with a structured verdict per file control (`roles`: {semantic: (file_role,
    status)}), and a start date for any start question that carries `earliest_start_date`."""

    def __init__(self, roles=None, **kwargs):
        super().__init__(**kwargs)
        self.roles = roles or {}

    def __call__(self, prompt):
        if is_judge_prompt(prompt):
            return super().__call__(prompt)
        packet = json.loads(prompt.split(PACKET_MARKER, 1)[1])
        self.packets.append(packet)
        answers = []
        for question in packet["questions"]:
            if question["semantic"] in self.roles:
                role, status = self.roles[question["semantic"]]
                answers.append(entry(question, file_role=role, status=status, value=None,
                                     unknown_reason=None if status == "ANSWERED" else "The owner has not supplied this document.",
                                     derivation="fixture: what the control is, by the form's meaning"))
            elif question["semantic"] in START_SEMANTICS and question.get("earliest_start_date"):
                answers.append(entry(question, status="ANSWERED", value=question["earliest_start_date"], unknown_reason=None,
                                     question_class="AVAILABILITY", derivation="fixture: the recorded earliest start"))
            else:
                answers.append(entry(question, unknown_reason="The packet does not record this."))
        return json.dumps({"packet_sha256": packet["packet_sha256"], "answers": answers})


@unittest.skipUnless(owner_questions.DEPENDENCIES, "dashboard dependencies not installed")
class FileControls(owner_questions.OwnerQuestions):
    def saved(self, key, field):
        row = self.database.execute("SELECT value,source FROM application_answers WHERE job_key=? AND field=?",
                                    (key, field)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    # --- S1 ---------------------------------------------------------------------------------------------

    def test_owner_a_resume_upload_under_any_name_is_filled_with_the_resume(self):
        question = control("resume_file", "Resume *")
        field = applications.question_field(question)
        key = self.discover(fixtures.role("Hooli", "hooli-1"))
        self.form({"hooli": [question]})
        self.run_cycle(FileModel(roles={"resume_file": ("resume", "ANSWERED")}))
        self.assertEqual(self.pending(key), [])
        self.assertEqual((self.job(key)["state"], self.job(key)["blocker"]), ("SUBMITTED", None))
        [submission] = self.submissions()
        self.assertEqual(submission["answers"][field], self.policy["resume"]["path"])   # the designated résumé
        self.assertNotIn("attachments", submission)                                     # nothing rendered
        value, source = self.saved(key, field)
        self.assertTrue(source.startswith(applications.FILE_ROLE_SOURCE_PREFIX + "resume:"), source)
        self.assertEqual(value, self.policy["resume"]["path"])

    def test_owner_a_cv_upload_marked_resume_is_the_resume_whatever_its_status(self):
        question = control("curriculum_vitae", "Upload your CV*")
        field = applications.question_field(question)
        key = self.discover(fixtures.role("Initech", "initech-1"))
        self.form({"initech": [question]})
        self.run_cycle(FileModel(roles={"curriculum_vitae": ("resume", "UNKNOWN")}))
        self.assertEqual(self.pending(key), [])
        self.assertEqual((self.job(key)["state"], self.job(key)["blocker"]), ("SUBMITTED", None))
        [submission] = self.submissions()
        self.assertEqual(submission["answers"][field], self.policy["resume"]["path"])
        # A later pass finds the recorded answer by field and asks the AI nothing about it.
        answers, missing = applications.resolve_answers(
            [question], dict(self.policy, answers=dict(self.policy["answers"], **{field: {
                "semantic": "curriculum_vitae", "value": "stale.pdf", "source": applications.FILE_ROLE_SOURCE_PREFIX + "resume:x"}})))
        self.assertEqual((missing, answers[field]), ([], self.policy["resume"]["path"]))

    # --- S2 ---------------------------------------------------------------------------------------------

    def test_owner_a_required_transcript_gets_a_file_card_and_the_file_is_attached_as_it_is(self):
        question = control("transcript_upload", "Unofficial transcript*")
        field = applications.question_field(question)
        key = self.discover(fixtures.role("Vandelay", "vandelay-1"))
        self.form({"vandelay": [question]})
        # The AI's UNKNOWN with the judge's ACCEPT (as today's fixture gives it), or its `file_role`
        # "other": either way the question is the user's, and the card is a FILE card.
        self.run_cycle(owner_questions.Model())
        [decision] = self.pending(key)
        self.assertEqual(decision["answer_type"], "file")
        self.run_cycle(FileModel(roles={"transcript_upload": ("other", "UNKNOWN")}), days=0.001)
        self.assertEqual([row["id"] for row in self.pending(key)], [decision["id"]])   # the same open card
        self.assertEqual(self.job(key)["blocker"], "UNKNOWN_REQUIRED_ANSWERS:" + field)
        # What the user sees: a file input, the instruction, Abandon; no text box.
        card = self.card(self.page(), question["label"])
        self.assertIn('type="file"', card)
        self.assertIn("The form asks for this document. Choose the file to upload, or press Abandon if you do not have one.", card)
        self.assertIn("Upload file", card)
        self.assertIn("Abandon application", card)
        self.assertNotIn("<textarea", card)
        self.assertNotIn('type="text"', card)
        self.assertIn('enctype="multipart/form-data"', card)
        # Typed text is refused: nothing recorded, no file saved, the same card stays.
        page = self.answer(decision, "My GPA was 3.9; transcript available on request.")
        self.assertIn(decision_transport.NOT_FITTING["file"], page)
        self.assertEqual([row["id"] for row in self.pending(key)], [decision["id"]])
        self.assertIsNone(self.saved(key, field))
        self.assertFalse((self.home / user_uploads.DIRECTORY).exists())
        # The file is saved under the Sidedoor home, hashed, and recorded as a user-supplied file.
        content = b"%PDF-1.4 mock transcript, never a real document"
        response = self.client.post("/approvals/questions/answer", headers=owner_questions.ORIGIN,
                                    data={"decision_id": decision["id"], "payload_hash": decision["payload_hash"]},
                                    files={"file": ("Transcript 2024.pdf", content, "application/pdf")})
        self.assertEqual(response.status_code, 200)
        self.assertIn("File saved.", response.text)
        self.assertEqual(self.pending(key), [])
        marker, source = self.saved(key, field)
        self.assertTrue(user_uploads.is_marker(marker), marker)
        self.assertEqual(marker["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(marker["file_name"], "Transcript 2024.pdf")
        path = Path(marker[user_uploads.MARKER_KEY])
        self.assertEqual(path.parent.parent, self.home / user_uploads.DIRECTORY)
        self.assertEqual(path.read_bytes(), content)
        self.assertTrue(source.startswith("authenticated-owner-decision:"))
        # Submit: the file itself is attached, hash-checked, and nothing is rendered to a PDF.
        self.run_cycle(FileModel(), days=0.01)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        [submission] = self.submissions()
        self.assertNotIn(field, submission["answers"])
        attached = submission["attachments"][field]
        self.assertEqual((attached["path"], attached["sha256"], attached["user_supplied"]), (str(path), marker["sha256"], True))
        self.assertFalse((self.root / "state" / "cover-letters").exists())
        evidence = json.loads(self.database.execute("SELECT evidence FROM application_attempts WHERE job_key=?", (key,)).fetchone()[0])
        user_uploads.verify_attachments({field: attached}, evidence["answers"])          # the gate's check passes
        path.write_bytes(content + b" tampered")
        with self.assertRaises(ValueError):
            user_uploads.verify_attachments({field: attached}, evidence["answers"])      # and refuses a changed file
        self.assertIn("file", decision_transport.ANSWER_TYPES)

    # --- S3 ---------------------------------------------------------------------------------------------

    def test_owner_a_start_date_under_any_name_is_answered_from_the_recorded_date(self):
        questions = [control("earliest_start", "Earliest start date", "date"),
                     control("start_availability", "When could you start?", "text")]
        key = self.discover(fixtures.role("Globex", "globex-1"))
        self.form({"globex": questions})
        model = FileModel()
        self.run_cycle(model)
        self.assertEqual(self.pending(key), [])
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        floor = applications.earliest_start_floor(self.adapter.now, user_profile.load())
        self.assertIsNotNone(floor)
        packet_questions = {question["semantic"]: question for question in model.packets[0]["questions"]}
        for question in questions:
            self.assertEqual(packet_questions[question["semantic"]]["earliest_start_date"], floor)
        [submission] = self.submissions()
        for question in questions:
            self.assertEqual(submission["answers"][applications.question_field(question)], floor)

    # --- S4 ---------------------------------------------------------------------------------------------

    def test_owner_a_certificate_answered_with_the_resume_attaches_the_chosen_original(self):
        original = self.root / "My Resume.docx"
        original.write_bytes(b"mock original resume; never uploaded anywhere")
        self.policy["resume_raw"] = {"path": str(original), "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                                     "format": "docx", "file_name": "My Resume.docx"}
        self.policy[user_profile.APPLICATION_RESUME] = user_profile.ORIGINAL
        user_profile.write_home(self.policy, self.home)
        self.assertEqual(user_profile.application_resume(user_profile.load())["path"], str(original))
        question = control("certificate_upload", "Certification document*")
        field = applications.question_field(question)
        key = self.discover(fixtures.role("Umbrella", "umbrella-1"))
        self.form({"umbrella": [question]})
        pointer = self.policy["answers"]["resume"]["value"]                               # the ATS path
        # A recorded answer naming the pointer (an earlier pass, or a kept answer) resolves to the chosen file.
        policy = dict(user_profile.load())
        policy["answers"] = dict(policy["answers"], **{field: {"semantic": "certificate_upload", "value": pointer, "source": "ai-answer:x"}})
        self.assertEqual(applications.resolve_answers([question], policy), ({field: str(original)}, []))
        self.run_cycle(FileModel(roles={"certificate_upload": ("resume_copy_ok", "ANSWERED")}))
        self.assertEqual(self.pending(key), [])
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        [submission] = self.submissions()
        self.assertNotIn("attachments", submission)                                     # no PDF of a path
        self.assertFalse((self.root / "state" / "cover-letters").exists())
        self.assertEqual(submission["resume"], str(original))
        self.assertEqual(submission["answers"][field], str(original))                  # the chosen file, as the résumé

    # --- Review fixes F1, F2, F4, F5 (owner picks) -------------------------------------------------------

    def test_owner_a_refused_second_upload_never_removes_the_first_cards_file(self):
        first, second = control("transcript_upload", "Transcript*"), control("diploma_upload", "Diploma*")
        key = self.discover(fixtures.role("Vandelay", "vandelay-2"))
        self.form({"vandelay": [first, second]})
        self.run_cycle(owner_questions.Model())
        cards = {card["field"]: card for card in self.pending(key)}
        self.assertEqual(len(cards), 2)
        content = b"%PDF-1.4 one document for both cards"
        post = lambda card, shown: self.client.post("/approvals/questions/answer", headers=owner_questions.ORIGIN,
                                                    data={"decision_id": card["id"], "payload_hash": shown},
                                                    files={"file": ("Transcript.pdf", content, "application/pdf")})
        one = cards[applications.question_field(first)]
        self.assertIn("File saved.", post(one, one["payload_hash"]).text)
        marker, _source = self.saved(key, applications.question_field(first))
        two = cards[applications.question_field(second)]
        self.assertIn("This question changed since it was shown.", post(two, "stale-hash").text)   # F1: refused
        self.assertTrue(Path(marker[user_uploads.MARKER_KEY]).is_file())                           # the first file stays
        self.assertEqual(user_uploads.attachments([first], {applications.question_field(first): marker})
                         [applications.question_field(first)]["sha256"], marker["sha256"])
        self.assertIn("File saved.", post(two, two["payload_hash"]).text)
        self.run_cycle(owner_questions.Model(), days=0.01)
        self.assertEqual(self.job(key)["state"], "SUBMITTED")
        [submission] = self.submissions()
        self.assertEqual(submission["attachments"][applications.question_field(first)]["path"], marker[user_uploads.MARKER_KEY])
        self.assertEqual(len({entry["path"] for entry in submission["attachments"].values()}), 2)   # one path per card

    def test_owner_a_file_control_with_no_file_role_is_a_file_card_never_text(self):
        import answering_agent
        for role_given in (True, False):
            question = control("transcript_upload", "Transcript*")
            key = self.discover(fixtures.role("Typed Co", "typed-" + str(role_given)))
            self.form({"typed co": [question]})

            class Typed(owner_questions.Model):
                def __call__(self, prompt):
                    if is_judge_prompt(prompt):
                        return super().__call__(prompt)
                    packet = json.loads(prompt.split(PACKET_MARKER, 1)[1])
                    answers = [entry(item, status="ANSWERED", value="See attached transcript.", unknown_reason=None,
                                     **({"file_role": None} if role_given else {})) for item in packet["questions"]]
                    return json.dumps({"packet_sha256": packet["packet_sha256"], "answers": answers})
            self.run_cycle(Typed())
            [decision] = self.pending(key)
            self.assertEqual(decision["answer_type"], "file")                         # F2: a file card
            self.assertIsNone(self.saved(key, applications.question_field(question)))  # no text recorded
            self.assertFalse((self.root / "state" / "cover-letters").exists())         # no PDF
            self.assertEqual(self.submissions(), [])
            verdicts = json.loads(self.database.execute("SELECT audit FROM application_answering_attempts WHERE job_key=?",
                                                         (key,)).fetchone()[0])["verdicts"]
            self.assertEqual([verdict["reason"] for verdict in verdicts], ["FILE_ROLE_MISSING"])
        # `written` is unchanged: the AI's own text to a file control is rendered to a PDF.
        self.assertIn("written", answering_agent.FILE_ROLES)

    def test_owner_the_review_card_shows_the_resume_in_words_never_its_path(self):
        import approvals
        import approvals_page
        question = control("certificate_upload", "Certification document*")
        key = self.discover(fixtures.role("Hooli", "hooli-2"))
        self.form({"hooli": [question]})
        approvals.set_install_mode(approvals.REVIEW, root=self.root, home=self.home)
        self.run_cycle(FileModel(roles={"certificate_upload": ("resume_copy_ok", "ANSWERED")}))
        self.assertEqual(self.job(key)["blocker"], applications.AWAITING_APPLICATION_APPROVAL)
        [card] = approvals_page.applications_waiting(self.database, only=key)
        [answer] = [answer for answer in card["answers"] if answer["field"] == applications.question_field(question)]
        self.assertEqual((answer["control"], answer["answer"]), ("file", approvals.RESUME_UPLOADED))
        page = self.page()
        html = page.split('id="' + card["dom"] + '"', 1)[1].split("</article>", 1)[0]
        self.assertIn(approvals.RESUME_UPLOADED, html)
        self.assertNotIn(self.policy["resume"]["path"], html)                           # F4: no internal path
        self.assertNotIn('name="answer:' + applications.question_field(question) + '"', html)   # not editable
        snapshot = json.loads(self.database.execute("SELECT snapshot FROM application_approvals WHERE job_key=?", (key,)).fetchone()[0])
        item = next(item for item in snapshot["questions"] if item["field"] == applications.question_field(question))
        self.assertEqual(approvals._fitted(item, ["typed over it"]), item["value"])     # the file as it is

    def test_owner_upload_paths_stay_short_for_windows(self):
        long_key = "a" * 300
        long_name = "b" * 200 + ".pdf"
        marker = user_uploads.save(long_key, long_name, b"x", self.home, "d" * 40)
        path = Path(marker[user_uploads.MARKER_KEY])
        self.assertLessEqual(len(path.parent.name), 40 + 1 + 8)
        self.assertLessEqual(len(path.name), 16 + 1 + 12 + 1 + 60 + 4)
        self.assertTrue(path.name.endswith(".pdf"))
        self.assertEqual(marker["file_name"], long_name)
        self.assertTrue(user_uploads.verify(marker, self.home))
        self.assertEqual(len(user_uploads.saved_name("x" * 100 + ".verylongextension", "0" * 64, "id").rsplit(".", 1)[1]), 9)
        self.assertNotEqual(user_uploads.folder("job:1", self.home), user_uploads.folder("job_1", self.home))   # the hash tells them apart


def load_tests(loader, tests, pattern):
    """Only this file's own cases: the parent's are run from its own module."""
    own = [name for name in loader.getTestCaseNames(FileControls) if name in vars(FileControls)]
    return unittest.TestSuite(FileControls(name) for name in own)


if __name__ == "__main__":
    unittest.main()
