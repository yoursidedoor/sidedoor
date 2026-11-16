"""E4.T2 résumé intake (option A, owner 2026-10-21): text saved once, AI-extracted claims, AI-written ATS PDF.

The model is faked through text_model's transport seam; nothing reaches the network.
"""
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import applications
import profile_home
import reply_resume
import research_workflow
import resume_intake
import synthetic_resume
import user_profile


CONFIG = {"text_model": {"model": "test/model"}}
# The ATS résumé is written with fpdf2 (cover_letter.render); without it these tests cannot reach READY.
NEEDS_FPDF = unittest.skipUnless(importlib.util.find_spec("fpdf"), "fpdf2 not installed (the PDF writer)")
CLAIMS = [{"text": "Built a job scheduler that let workers fail over between data centers", "tags": ["backend"],
           "page": 1},
          {"text": "Wrote the runbooks for the on-call team", "tags": ["infrastructure"], "page": 2}]
ATS = "ALEX EXAMPLE\n\nEXPERIENCE\n- Built a job scheduler that let workers fail over between data centers"


def docx(path, paragraphs):
    body = "".join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>' % text for text in paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml",
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body>%s</w:body></w:document>' % body)
    return path


LINKED_DOCX = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
    '<w:p><w:r><w:t>Alex Example</w:t></w:r></w:p>'
    '<w:p><w:hyperlink r:id="rId7"><w:r><w:t>Port</w:t></w:r><w:r><w:t>folio</w:t></w:r></w:hyperlink>'
    '<w:r><w:t> and </w:t></w:r>'
    '<w:hyperlink r:id="rId8"><w:r><w:t>github.com/alex-example</w:t></w:r></w:hyperlink>'
    '<w:hyperlink w:anchor="top"><w:r><w:t> (top of page)</w:t></w:r></w:hyperlink></w:p>'
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://example.com/talk" </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>My talk</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    '<w:p><w:r><w:t>Built a job scheduler that let workers fail over between data centers</w:t></w:r></w:p>'
    '</w:body></w:document>')
LINKED_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="https://example.com/alex/" TargetMode="External"/>'
    '<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="https://github.com/alex-example" TargetMode="External"/>'
    '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>')
HIDDEN, WRITTEN, FIELD = "https://example.com/alex/", "https://github.com/alex-example", "https://example.com/talk"


def linked_docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", LINKED_DOCX)
        archive.writestr("word/_rels/document.xml.rels", LINKED_RELS)
    return path


def linked_pdf(path):
    from fpdf import FPDF
    pdf = FPDF(unit="pt", format="letter")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for words, url in (("Alex Example | ", ""), ("Portfolio", HIDDEN), (" | ", ""), ("github.com/alex-example", WRITTEN),
                       ("\nBuilt a job scheduler that let workers fail over between data centers", "")):
        pdf.write(15, words, link=url)
    pdf.output(str(path))
    return path


def pdf_links(path):
    """{address: the words inside its clickable rectangle} of a PDF, read back with the intake's own reader."""
    return {link["url"]: link["text"] for link in resume_intake._pdf_text(path)[3]}


class Model:
    """Answers each call by its response_format name; records every prompt."""

    def __init__(self, claims=CLAIMS, ats=ATS, status=200, finish_reason="stop", content=None):
        self.claims, self.ats, self.status, self.calls, self.providers = claims, ats, status, [], []
        self.finish_reason, self.content = finish_reason, content

    def __call__(self, url, data, headers, timeout):
        body = json.loads(data)
        name = body["response_format"]["json_schema"]["name"]
        self.calls.append((name, body["messages"][-1]["content"]))
        self.providers.append(body.get("provider"))
        self.limits = (body["max_tokens"], timeout)
        if self.status != 200:
            return self.status, b'{"error": {"message": "Insufficient credits"}}'
        answer = {"resume_claims": {"claims": self.claims},
                  "ats_resume": {"resume_text": self.ats}}[name]
        content = self.content if self.content is not None else json.dumps(answer)
        return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": self.finish_reason}],
                                "usage": {}}).encode()


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.home = profile_home.use(self, user_profile.load())
        (self.home / "openrouter.key").write_text("test-key")
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        research_workflow.initialize(self.database)
        self.folder = Path(tempfile.mkdtemp())

    def ingest(self, path, model):
        return resume_intake.ingest(CONFIG, path, database=self.database, transport=model)

    @NEEDS_FPDF
    def test_a_scanned_pdf_gets_its_own_message_before_any_model_call(self):
        # Owner, 2026-11-16 (M5 a): pages that are pictures have no text; no paid call is made for them.
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.rect(10, 10, 100, 100)
        pdf.output(str(self.folder / "scan.pdf"))
        model = Model()
        result = self.ingest(self.folder / "scan.pdf", model)
        self.assertEqual(("FAILED", "PDF_HAS_NO_TEXT", resume_intake.PICTURE_PDF), (result["state"], result["code"], result["reason"]))
        self.assertEqual([], model.calls)

    def test_a_long_sentence_is_asked_for_whole_and_used_in_outreach(self):
        # Owner, 2026-11-16 (M4 b): a sentence over 240 characters was saved and then silently never used.
        import linkedin_contacts
        long = "Led the redesign of the scheduling service " + "across three regions " * 14
        self.assertGreater(len(long), 240)
        model = Model(claims=[{"text": long.strip(), "tags": ["backend"], "page": 1}])
        self.ingest(docx(self.folder / "resume.docx", [long]), model)
        self.assertNotIn("240", dict(model.calls)["resume_claims"])
        self.assertEqual([long.strip()], [claim["text"] for claim in linkedin_contacts._claims(self.database)])

    def test_the_ats_prompt_keeps_every_section(self):
        self.assertIn("Keep everything the résumé says: every section", resume_intake.ATS_PROMPT)

    def test_other_formats_get_a_plain_message_and_no_model_call(self):
        model = Model()
        path = self.folder / "resume.doc"
        path.write_bytes(b"old word file")
        result = self.ingest(path, model)
        self.assertEqual(("FAILED", "UNSUPPORTED_FORMAT"), (result["state"], result["code"]))
        self.assertIn("PDF and Word (.docx)", result["reason"])
        self.assertEqual([], model.calls)

    @NEEDS_FPDF
    def test_docx_upload_saves_text_every_claim_and_application_pdf(self):
        model = Model()
        path = docx(self.folder / "Resume.docx", ["Alex Example", CLAIMS[0]["text"]])
        result = self.ingest(path, model)
        self.assertEqual("READY", result["state"], result)
        self.assertEqual(2, result["claims"], "Every extracted claim is registered; no judge")
        self.assertEqual(["resume_claims", "ats_resume"], [name for name, _ in model.calls])
        self.assertIn(CLAIMS[0]["text"], model.calls[1][1], "The ATS version is written from the saved raw text")
        profile = json.loads(user_profile.profile_path().read_text())
        raw, application = profile["resume_raw"], profile["resume"]
        self.assertEqual("docx", raw["format"])
        self.assertEqual(Path(raw["text_path"]).read_text(), "Alex Example\n" + CLAIMS[0]["text"] + "\n\f")
        self.assertTrue(Path(application["path"]).read_bytes().startswith(b"%PDF-"))
        self.assertEqual(raw["sha256"], application["from_raw_sha256"])
        # Every raw reader now reads the saved text; claims are keyed by the raw hash.
        self.assertEqual(Path(raw["text_path"]).read_text(), reply_resume.context()["text"])
        self.assertEqual(raw["sha256"], research_workflow.designated_resume_sha256())
        saved = resume_intake.claims(self.database)
        self.assertEqual(sorted(claim["text"] for claim in CLAIMS), sorted(claim["text"] for _, claim in saved))

    @NEEDS_FPDF
    def test_answers_pointing_at_the_resume_move_to_the_new_application_resume(self):
        before = json.loads(user_profile.profile_path().read_text())
        old = before["resume"]["path"]
        pointing = sorted(key for key, answer in before["answers"].items() if answer.get("value") == old)
        self.assertIn("resume", pointing)
        result = self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), Model())
        after = json.loads(user_profile.profile_path().read_text())
        self.assertEqual(result["application"]["path"], after["resume"]["path"])
        for key in pointing:
            self.assertEqual(after["resume"]["path"], after["answers"][key]["value"], key)
        others = {key: answer for key, answer in before["answers"].items() if key not in pointing}
        self.assertEqual(others, {key: after["answers"][key] for key in others})

    @NEEDS_FPDF
    def test_a_fresh_profile_gets_the_resume_answer_every_form_needs(self):
        # 2026-10-22, the first real run: a Setup-made profile had no answers.resume, so the résumé
        # question on every form stayed unanswered (ANSWERING_RETRY:...:resume).
        profile = json.loads(user_profile.profile_path().read_text())
        profile["answers"].pop("resume", None)
        user_profile.profile_path().write_text(json.dumps(profile))
        result = self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), Model())
        after = json.loads(user_profile.profile_path().read_text())
        self.assertEqual({"value": result["application"]["path"], "semantic": "resume"},
                         {key: after["answers"]["resume"][key] for key in ("value", "semantic")})
        question = {"id": "q1", "label": "Resume/CV*", "required": True, "semantic": "resume", "answer_type": "file"}
        answers, missing = applications.resolve_answers([question], user_profile.load())
        self.assertEqual(([], result["application"]["path"]), (missing, answers[applications.question_field(question)]))

    @NEEDS_FPDF
    def test_a_failed_re_upload_leaves_the_application_file_matching_its_hash(self):
        path = docx(self.folder / "r.docx", [CLAIMS[0]["text"]])
        self.assertEqual("READY", self.ingest(path, Model())["state"])
        before = json.loads(user_profile.profile_path().read_text())
        with patch.object(resume_intake.research_workflow, "register_claims", side_effect=ValueError("synthetic")):
            result = self.ingest(path, Model(ats=ATS + "\n- A different rewrite"))
        self.assertEqual(("FAILED", "CLAIMS_NOT_SAVED"), (result["state"], result["code"]))
        after = json.loads(user_profile.profile_path().read_text())
        self.assertEqual(before, after)
        self.assertEqual(after["resume"]["sha256"], hashlib.sha256(Path(after["resume"]["path"]).read_bytes()).hexdigest())

    @NEEDS_FPDF
    def test_database_errors_are_a_plain_failure(self):
        path = docx(self.folder / "r.docx", [CLAIMS[0]["text"]])
        with patch.object(resume_intake.research_workflow, "register_claims",
                          side_effect=sqlite3.OperationalError("database is locked")):
            locked = self.ingest(path, Model())
        model = Model()
        with patch.object(resume_intake, "_database", side_effect=sqlite3.OperationalError("unable to open database file")):
            unopened = resume_intake.ingest(CONFIG, path, transport=model)
        self.assertEqual(("FAILED", "CLAIMS_NOT_SAVED"), (locked["state"], locked["code"]))
        self.assertTrue(locked["reason"].startswith("Sidedoor could not save the claims: OperationalError"))
        # A database that cannot be opened at all fails before the two paid model calls.
        self.assertEqual(("FAILED", "SAVE_FAILED"), (unopened["state"], unopened["code"]))
        self.assertTrue(unopened["reason"].startswith("Sidedoor could not open its database: OperationalError"))
        self.assertEqual(model.calls, [])

    def test_a_fresh_install_without_state_opens_the_database(self):
        fresh = Path(tempfile.mkdtemp())
        self.assertFalse((fresh / "state").exists())
        resume_intake._database(fresh).close()  # raised "unable to open database file" before the fix
        self.assertTrue((fresh / "state" / "orchestrator.sqlite3").is_file())

    @NEEDS_FPDF
    def test_a_fresh_install_reads_a_resume_end_to_end(self):
        fresh = Path(tempfile.mkdtemp())
        path = docx(self.folder / "r.docx", [CLAIMS[0]["text"]])
        result = resume_intake.ingest(CONFIG, path, root=fresh, transport=Model())
        self.assertEqual(result["state"], "READY", result)

    def test_only_the_current_resume_claims_reach_research(self):
        research_workflow.register_claims(self.database, [{"text": "Old claim", "tags": ["backend"]}],
                                          designated_source_text="", source_sha256="a" * 64, designated_sha256="a" * 64)
        research_workflow.register_claims(self.database, [{"text": "New claim", "tags": ["backend"]}],
                                          designated_source_text="", source_sha256="b" * 64, designated_sha256="b" * 64)
        self.assertEqual(["New claim"], [claim["text"] for _, claim in research_workflow.current_claims(self.database)])

    @NEEDS_FPDF
    def test_a_job_answer_naming_the_old_resume_moves_and_the_next_attempt_uploads_the_resume(self):
        applications.initialize(self.database)
        old = json.loads(user_profile.profile_path().read_text())["resume"]["path"]
        self.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                              ("job:1", "certificate_upload", "certificate", json.dumps(old), "ai-answer:synthetic"))
        self.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                              ("job:1", "why_us", "motivation", json.dumps("Because."), "ai-answer:synthetic"))
        self.database.commit()
        result = self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), Model())
        new = result["application"]["path"]
        stored = dict(self.database.execute("SELECT field,value FROM application_answers WHERE job_key='job:1'").fetchall())
        self.assertEqual({"certificate_upload": json.dumps(new), "why_us": json.dumps("Because.")}, stored)
        # The next attempt's file controls: the résumé answer is uploaded as the résumé, never
        # rendered into a cover-letter PDF (applications.attachment_uploads).
        question = {"label": "Certificate", "semantic": "certificate", "answer_type": "file"}
        key = applications.question_field(question)
        moved, left = json.loads(stored["certificate_upload"]), old
        self.assertEqual({}, applications.attachment_uploads([question], {key: moved}, Path(new)))
        self.assertEqual({key: left}, applications.attachment_uploads([question], {key: left}, Path(new)),
                         "control: an answer left on the old path would be rendered as text")

    @NEEDS_FPDF
    def test_a_profile_write_failure_switches_nothing(self):
        applications.initialize(self.database)
        before = user_profile.profile_path().read_text()
        old = json.loads(before)["resume"]["path"]
        self.database.execute("INSERT INTO application_answers VALUES (?,?,?,?,?)",
                              ("job:1", "certificate_upload", "certificate", json.dumps(old), "ai-answer:synthetic"))
        self.database.commit()
        with patch.object(resume_intake, "_write_profile", side_effect=OSError("disk full")):
            result = self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), Model())
        self.assertEqual(("FAILED", "CLAIMS_NOT_SAVED"), (result["state"], result["code"]))
        self.assertEqual(before, user_profile.profile_path().read_text())
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM rw_claims").fetchone()[0])
        self.assertIsNone(self.database.execute("SELECT value FROM rw_meta WHERE key='designated_resume'").fetchone())
        self.assertEqual(json.dumps(old), self.database.execute("SELECT value FROM application_answers").fetchone()[0])

    def test_a_word_header_reaches_the_raw_text_and_the_ats_rewrite(self):
        path = self.folder / "header.docx"
        word = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<w:document %s><w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p>'
                                                  '</w:body></w:document>' % (word, CLAIMS[0]["text"]))
            archive.writestr("word/header1.xml", '<w:hdr %s><w:p><w:r><w:t>Alex Example</w:t></w:r></w:p>'
                                                 '<w:p><w:r><w:t>alex@example.com | +1-555-010-0100</w:t></w:r></w:p></w:hdr>' % word)
            archive.writestr("word/footer1.xml", '<w:ftr %s><w:p><w:r><w:t>Page footer</w:t></w:r></w:p></w:ftr>' % word)
        text, _, _, _ = resume_intake._docx_text(path)
        self.assertEqual("Alex Example\nalex@example.com | +1-555-010-0100\n" + CLAIMS[0]["text"] + "\nPage footer\n\f", text)
        model = Model()
        self.ingest(path, model)
        self.assertIn("alex@example.com | +1-555-010-0100", dict(model.calls)["ats_resume"])

    def test_an_unreadable_header_is_skipped_but_a_broken_body_fails(self):
        path = self.folder / "broken-header.docx"
        word = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<w:document %s><w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p>'
                                                  '</w:body></w:document>' % (word, CLAIMS[0]["text"]))
            archive.writestr("word/header1.xml", "<!not xml")
            archive.writestr("word/footer1.xml", '<w:ftr %s><w:p><w:r><w:t>Page footer</w:t></w:r></w:p></w:ftr>' % word)
        text, _, _, _ = resume_intake._docx_text(path)
        self.assertEqual(CLAIMS[0]["text"] + "\nPage footer\n\f", text)
        broken = self.folder / "broken-body.docx"
        with zipfile.ZipFile(broken, "w") as archive:
            archive.writestr("word/document.xml", "<!not xml")
        with self.assertRaises(resume_intake.IntakeError):
            resume_intake._docx_text(broken)

    @NEEDS_FPDF
    def test_a_claim_unusable_in_shape_is_dropped_and_the_rest_are_saved(self):
        claims = CLAIMS + [{"text": "No tags here", "tags": [], "page": 1},
                           {"text": "  ", "tags": ["backend"], "page": 1},
                           {"text": "Blank tag", "tags": [" "], "page": None}]
        model = Model(claims=claims)
        result = self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), model)
        self.assertEqual(("READY", 2), (result["state"], result["claims"]))
        self.assertEqual(sorted(claim["text"] for claim in CLAIMS),
                         sorted(claim["text"] for _, claim in resume_intake.claims(self.database)))
        self.assertEqual([{"sort": "price", "require_parameters": True}] * 2, model.providers)

    def test_a_truncated_answer_says_the_resume_is_too_long(self):
        path = docx(self.folder / "r.docx", ["Alex Example"])
        truncated = Model(finish_reason="length", content='{"claims": [{"text": "Built a sched')
        oversized = Model(content=json.dumps({"claims": [{"text": "x" * 200, "tags": ["backend"], "page": 1}] * 1400}))
        for model in (truncated, oversized):
            result = self.ingest(path, model)
            self.assertEqual(("FAILED", "RESUME_TOO_LONG", resume_intake.TOO_LONG),
                             (result["state"], result["code"], result["reason"]))
        self.assertEqual(32000, json.loads(json.dumps(resume_intake.MAX_TOKENS)))

    def test_intake_calls_allow_a_long_answer_and_the_time_it_takes(self):
        model = Model(claims=[])
        self.ingest(docx(self.folder / "r.docx", ["Alex Example"]), model)
        self.assertEqual(32000, model.limits[0])
        self.assertGreater(model.limits[1], 500, "a 32000-token answer needs about ten minutes at worst")

    def test_the_ats_prompt_keeps_company_names_inside_role_lines(self):
        model = Model(claims=CLAIMS[:1])
        self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), model)
        prompt = dict(model.calls)["ats_resume"]
        self.assertIn('"Title | Company | Dates"', prompt)
        self.assertIn("A company or team name appears only inside its role line", prompt)

    def test_only_unusable_claims_fail_with_no_claims(self):
        result = self.ingest(docx(self.folder / "r.docx", ["Alex Example"]),
                             Model(claims=[{"text": "No tags", "tags": [], "page": None}]))
        self.assertEqual(("FAILED", "NO_CLAIMS"), (result["state"], result["code"]))

    def test_no_claim_fails_and_keeps_the_previous_resume(self):
        before = user_profile.profile_path().read_text()
        result = self.ingest(docx(self.folder / "r.docx", ["Alex Example"]), Model(claims=[]))
        self.assertEqual(("FAILED", "NO_CLAIMS"), (result["state"], result["code"]))
        self.assertEqual(resume_intake.NO_CLAIMS, result["reason"])
        self.assertEqual(before, user_profile.profile_path().read_text())
        self.assertEqual(0, self.database.execute("SELECT COUNT(*) FROM rw_claims").fetchone()[0])

    def test_no_credits_is_a_plain_reason(self):
        result = self.ingest(docx(self.folder / "r.docx", ["Alex Example"]), Model(status=402))
        self.assertEqual("AI_NO_CREDITS", result["code"])
        self.assertEqual("The AI call failed: your OpenRouter account has no credits left.", result["reason"])

    @NEEDS_FPDF
    def test_edited_wording_is_a_user_reviewed_mapping(self):
        self.ingest(docx(self.folder / "r.docx", [CLAIMS[0]["text"]]), Model(claims=CLAIMS[:1]))
        claim_id = resume_intake.claims(self.database)[0][0]
        resume_intake.edit_wording(self.database, claim_id, "I built a failover scheduler")
        [(new_id, claim)] = resume_intake.claims(self.database)
        self.assertNotEqual(claim_id, new_id)
        self.assertEqual({"text": "I built a failover scheduler", "kind": "user_reviewed"},
                         {key: claim["invitation_mapping"][key] for key in ("text", "kind")})
        resume_intake.remove_claim(self.database, new_id)
        self.assertEqual([], resume_intake.claims(self.database))

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf not installed")
    def test_pdf_text_is_read_with_pypdf(self):
        text, pages, extraction, _ = resume_intake._pdf_text(synthetic_resume.PATH)
        self.assertEqual(5, pages)
        self.assertEqual(5, text.count("\f"))
        self.assertTrue(extraction.startswith("pypdf "))


if __name__ == "__main__":
    unittest.main()


@NEEDS_FPDF
class LinkTests(unittest.TestCase):
    """A beta user's feedback, 2026-11-16: a link attached to a word was lost, and the ATS PDF had no links."""
    setUp, ingest = IntakeTests.setUp, IntakeTests.ingest

    def check(self, source, model):
        result = self.ingest(source, model)
        self.assertEqual("READY", result["state"], result)
        self.assertEqual(2 if source.suffix == ".pdf" else 3, result["links"])
        saved = {link["url"]: link for link in user_profile.load()["resume_raw"]["links"]}
        self.assertEqual(("Portfolio", False), (saved[HIDDEN]["text"], saved[HIDDEN]["written_out"]))
        self.assertEqual(("github.com/alex-example", True), (saved[WRITTEN]["text"], saved[WRITTEN]["written_out"]))
        prompt = dict(model.calls)["ats_resume"]
        self.assertIn(HIDDEN, prompt)
        self.assertIn(WRITTEN, prompt)
        application = Path(result["application"]["path"])
        import pypdf
        return saved, pypdf.PdfReader(str(application)).pages[0].extract_text(), pdf_links(application)

    def test_a_pdf_with_a_hidden_address_and_a_written_out_one_keeps_both_clickable(self):
        # The model did as told: the hidden address is written next to its word, the other left as written.
        model = Model(ats="ALEX EXAMPLE\nPortfolio (%s) | github.com/alex-example\n\nEXPERIENCE\n- Built a job scheduler" % HIDDEN)
        saved, text, clickable = self.check(linked_pdf(self.folder / "resume.pdf"), model)
        self.assertEqual({HIDDEN: HIDDEN, WRITTEN: "github.com/alex-example"}, clickable)
        self.assertIn("Portfolio (%s)" % HIDDEN, text)
        self.assertNotIn("LINKS", text)

    def test_a_word_file_keeps_its_links_even_when_the_model_leaves_them_out(self):
        # ATS (the default answer) carries no address at all: each is added with the words it was on.
        saved, text, clickable = self.check(linked_docx(self.folder / "resume.docx"), Model())
        self.assertEqual("My talk", saved[FIELD]["text"])   # a HYPERLINK field, not a relationship
        self.assertEqual({HIDDEN: HIDDEN, WRITTEN: WRITTEN, FIELD: FIELD}, clickable)
        self.assertIn("LINKS\n- Portfolio: " + HIDDEN, text)
        self.assertIn("- My talk: " + FIELD, text)
        self.assertNotIn("top of page:", text)   # a jump inside the document is not an address

    def test_a_link_the_pdf_stores_by_reference_is_read(self):
        # Found on a real résumé (macOS Quartz PDF): the link's action and address are separate objects.
        import pypdf
        from pypdf.generic import NameObject
        writer = pypdf.PdfWriter(clone_from=str(linked_pdf(self.folder / "direct.pdf")))
        for annotation in writer.pages[0]["/Annots"]:
            annotation = annotation.get_object()
            action = annotation["/A"]
            action[NameObject("/URI")] = writer._add_object(action["/URI"])
            annotation[NameObject("/A")] = writer._add_object(action)
        writer.write(str(self.folder / "by-reference.pdf"))
        self.assertEqual({HIDDEN: "Portfolio", WRITTEN: "github.com/alex-example"}, pdf_links(self.folder / "by-reference.pdf"))

    def test_a_resume_uploaded_before_links_were_read_gets_them_without_a_step(self):
        # Owner, 2026-11-16 (K2 b). The saved profile has no `links`, as every upload before this change.
        import parallel_research
        self.ingest(linked_docx(self.folder / "resume.docx"), Model())
        profile = user_profile.load()
        old = dict(profile["resume_raw"])
        old.pop("links")
        resume_intake._write_profile({"resume_raw": old})
        scope = parallel_research.scope_id(user_profile.load())
        model = Model(ats="ALEX EXAMPLE\nPortfolio (%s)" % HIDDEN)
        done = resume_intake.keep_links_of_saved_resume(CONFIG, database=self.database, transport=model)
        self.assertEqual({"state": "READY", "links": 3}, done)
        self.assertEqual(["ats_resume"], [name for name, _ in model.calls])   # one call; the sentences are not read again
        saved = user_profile.load()
        self.assertEqual(3, len(saved["resume_raw"]["links"]))
        self.assertEqual({HIDDEN, WRITTEN, FIELD}, set(pdf_links(saved["resume"]["path"])))
        self.assertEqual(saved["resume"]["path"], saved["answers"]["resume"]["value"])
        self.assertEqual(scope, parallel_research.scope_id(saved))   # research does not start over
        self.assertIsNone(resume_intake.keep_links_of_saved_resume(CONFIG, database=self.database, transport=model))

    def test_a_saved_resume_without_links_costs_no_model_call_and_a_failed_call_changes_nothing(self):
        self.ingest(docx(self.folder / "plain.docx", ["Built a job scheduler"]), Model())
        old = dict(user_profile.load()["resume_raw"])
        old.pop("links")
        resume_intake._write_profile({"resume_raw": old})
        model = Model()
        self.assertEqual({"state": "READY", "links": 0},
                         resume_intake.keep_links_of_saved_resume(CONFIG, database=self.database, transport=model))
        self.assertEqual(([], []), (model.calls, user_profile.load()["resume_raw"]["links"]))
        self.ingest(linked_docx(self.folder / "resume.docx"), Model())
        old = dict(user_profile.load()["resume_raw"])
        old.pop("links")
        resume_intake._write_profile({"resume_raw": old})
        before = user_profile.load()
        failed = resume_intake.keep_links_of_saved_resume(CONFIG, database=self.database, transport=Model(status=402))
        self.assertEqual("AI_NO_CREDITS", failed["code"])
        self.assertEqual(before, user_profile.load())
        # Not asked again at once (an hour apart), so a broken key does not mean a call every round.
        self.assertIsNone(resume_intake.keep_links_of_saved_resume(CONFIG, database=self.database, transport=Model()))

    def test_an_address_inside_a_longer_one_does_not_count_as_kept(self):
        # Review R1: the profile link is not "there" because a project under it, or an email at that site, is.
        links = resume_intake._links([
            {"text": "GitHub", "near": "", "url": "https://github.com/jane", "page": 1},
            {"text": "Scheduler", "near": "", "url": "https://github.com/jane/proj", "page": 1},
            {"text": "Portfolio", "near": "", "url": "https://janedoe.com", "page": 1}])
        text, placed = resume_intake.with_every_link("Scheduler (https://github.com/jane/proj). Mail jane@janedoe.com", links)
        self.assertIn("LINKS\n- GitHub: https://github.com/jane\n- Portfolio: https://janedoe.com", text)
        pieces = {url: piece for piece, url in resume_intake.cover_letter._pieces(text, placed) if url}
        self.assertEqual({"https://github.com/jane/proj": "https://github.com/jane/proj",
                          "https://github.com/jane": "https://github.com/jane", "https://janedoe.com": "https://janedoe.com"}, pieces)
        # An address that ends a sentence, or is written with other capitals, is the same address.
        text, placed = resume_intake.with_every_link("See GitHub.com/Jane. Also https://janedoe.com, and more", links[:1] + links[2:])
        self.assertNotIn("LINKS", text)
        self.assertEqual([("GitHub.com/Jane", "https://github.com/jane"), ("https://janedoe.com", "https://janedoe.com")], placed)

    def test_a_phone_link_the_resume_shows_its_own_way_is_not_repeated(self):
        phone = resume_intake._links([{"text": "(555) 123-4567", "near": "", "url": "tel:+15551234567", "page": 1}])
        self.assertEqual(("Call (555) 123-4567", []), resume_intake.with_every_link("Call (555) 123-4567", phone))
        self.assertIn("LINKS\n- (555) 123-4567: +15551234567", resume_intake.with_every_link("No number here", phone)[0])

    def test_a_pdf_link_without_a_rectangle_is_kept_and_costs_the_page_nothing(self):
        import pypdf
        from pypdf.generic import NameObject
        writer = pypdf.PdfWriter(clone_from=str(linked_pdf(self.folder / "whole.pdf")))
        del writer.pages[0]["/Annots"][0].get_object()[NameObject("/Rect")]
        writer.write(str(self.folder / "no-rectangle.pdf"))
        self.assertEqual({HIDDEN: "", WRITTEN: "github.com/alex-example"}, pdf_links(self.folder / "no-rectangle.pdf"))

    def test_a_word_link_field_with_switches_before_its_address(self):
        self.assertEqual("https://x.example/a", resume_intake._field_address(' HYPERLINK \\o "tip" "https://x.example/a" \\t "_blank" '))
        self.assertEqual("https://x.example/a#part", resume_intake._field_address('HYPERLINK "https://x.example/a" \\l "part"'))
        self.assertIsNone(resume_intake._field_address('HYPERLINK \\l "bookmark"'))
        self.assertIsNone(resume_intake._field_address('PAGEREF _Toc1 \\h'))

    def test_an_address_the_model_made_up_is_never_clickable_and_a_file_without_links_is_unchanged(self):
        model = Model(ats=ATS + "\nhttps://invented.example/me")
        result = self.ingest(docx(self.folder / "resume.docx", ["Built a job scheduler"]), model)
        self.assertEqual((0, []), (result["links"], user_profile.load()["resume_raw"]["links"]))
        self.assertEqual({}, pdf_links(result["application"]["path"]))
        self.assertIn("(this résumé file has no links)", dict(model.calls)["ats_resume"])


class WordReadingTests(unittest.TestCase):
    """Sweep item 7 (owner, 2026-11-16, "7: fix"): what a Word résumé lost or doubled on the way in."""

    def read(self, document, **others):
        path = Path(tempfile.mkdtemp()) / "resume.docx"
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<w:document %s xmlns:mc=\"http://schemas.openxmlformats.org/"
                             "markup-compatibility/2006\"><w:body>%s</w:body></w:document>" % (w, document))
            for name, body in others.items():
                archive.writestr("word/%s.xml" % name, "<w:x %s>%s</w:x>" % (w, body))
        return resume_intake._docx_text(path)[0].split("\n")

    def test_a_text_box_is_read_once_on_its_own_line(self):
        box = "<w:txbxContent><w:p><w:r><w:t>Python, Go</w:t></w:r></w:p></w:txbxContent>"
        lines = self.read("<w:p><w:r><w:t>Skills</w:t></w:r><w:r><mc:AlternateContent><mc:Choice>%s</mc:Choice>"
                          "<mc:Fallback>%s</mc:Fallback></mc:AlternateContent></w:r></w:p>" % (box, box))
        self.assertEqual(["Skills", "Python, Go"], lines[:2])
        self.assertEqual(1, "\n".join(lines).count("Python, Go"))

    def test_an_icon_kept_only_in_the_older_copy_is_still_read(self):
        # Review R2: an emoji is a symbol element in mc:Choice and plain text in mc:Fallback.
        lines = self.read("<w:p><w:r><mc:AlternateContent><mc:Choice><w:symEx/></mc:Choice><mc:Fallback>"
                          "<w:t>\u2709</w:t></mc:Fallback></mc:AlternateContent><w:t> jane@example.com</w:t></w:r></w:p>")
        self.assertEqual("\u2709 jane@example.com", lines[0])

    def test_a_non_breaking_hyphen_stays_and_a_deleted_word_does_not(self):
        lines = self.read("<w:p><w:r><w:t>555</w:t><w:noBreakHyphen/><w:t>1234 co</w:t><w:softHyphen/><w:t>op</w:t></w:r>"
                          "<w:del><w:r><w:delText> gone</w:delText></w:r></w:del></w:p>")
        self.assertEqual("555-1234 coop", lines[0])

    def test_notes_are_read_and_a_repeated_header_is_read_once(self):
        header = "<w:p><w:r><w:t>Alex Example</w:t></w:r></w:p>"
        lines = self.read("<w:p><w:r><w:t>Body</w:t></w:r></w:p>", header1=header, header2=header,
                          footnotes="<w:footnote><w:p><w:r><w:t>Award shared with two colleagues</w:t></w:r></w:p></w:footnote>")
        self.assertEqual(["Alex Example", "Body", "Award shared with two colleagues"], [line for line in lines if line.strip("\f")])
