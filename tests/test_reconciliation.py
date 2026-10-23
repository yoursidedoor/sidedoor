import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import zipfile

import orchestrator
import reconciliation
import synthetic_resume


FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRACKER_ROWS = [  # sheet "Tracker" from row 8: A company, B role, L links, M known contact count
    ("Fixture Robotics", "Senior Software Engineer", "https://jobs.fixture-robotics.example/senior-swe", 3),
    ("Example Grid", "Staff Software Engineer", "https://jobs.example-grid.example/staff-swe", 3),
    ("Sample AI", "Senior Backend Engineer", "https://jobs.sample-ai.example/backend", 4),
]


def write_tracker_workbook(path):
    """A minimal synthetic .xlsx in the shape reconciliation.workbook_rows reads."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def text(reference, value):
        return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (reference, value)

    rows = ['<row r="1">%s</row>' % text("A1", "Synthetic tracker")]
    for number, (company, role, link, count) in enumerate(TRACKER_ROWS, 8):
        rows.append('<row r="%d">%s%s%s<c r="M%d"><v>%d</v></c></row>' % (
            number, text("A%d" % number, company), text("B%d" % number, role), text("L%d" % number, link), number, count))
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                         '<sheet name="Tracker" sheetId="1" r:id="rId1"/></sheets></workbook>' % (main, relationship))
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (main, "".join(rows)))


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        # Synthetic handoff and backups (the owner's are not in the repo): tests/fixtures/.
        shutil.copytree(FIXTURES / "handoff", self.root / "handoff")
        write_tracker_workbook(self.root / "handoff/originals/linkedin_interview_pipeline.xlsx")
        (self.root / "handoff/resume").mkdir()
        shutil.copyfile(synthetic_resume.PATH, self.root / "handoff/resume/alex-example-resume.pdf")
        shutil.copytree(FIXTURES / "saved-backups", self.root / "backups")  # backups/ is git-ignored
        self.database = orchestrator.connect(self.root)
        self.addCleanup(self.database.close)
        self.pipeline = json.loads((self.root / "backups/pipeline.json").read_text())
        orchestrator.import_history(self.database, self.root, self.pipeline)

    def test_handoff_idempotency_preserves_ten_contacts_and_newer_records(self):
        before = [tuple(row) for row in self.database.execute("SELECT * FROM history ORDER BY identity")]
        first = reconciliation.merge(self.database, self.root, orchestrator.profile_key, orchestrator.name_key)
        second = reconciliation.merge(self.database, self.root, orchestrator.profile_key, orchestrator.name_key)
        after = [tuple(row) for row in self.database.execute("SELECT * FROM history ORDER BY identity")]
        self.assertEqual(before, after)
        self.assertEqual(first["added_contacts"], 0)
        self.assertEqual(second["preserved_contacts"], 10)
        self.assertEqual(first["historical_rows"], second["historical_rows"])
        self.assertEqual(first["interview_holds"], 4)
        self.assertGreater(self.database.execute("SELECT COUNT(*) FROM application_jobs").fetchone()[0], 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)

    def test_original_resume_and_workbook_remain_byte_identical(self):
        names = ["originals/linkedin_interview_pipeline.xlsx", "resume/alex-example-resume.pdf"]
        before = {name: hashlib.sha256((self.root / "handoff" / name).read_bytes()).hexdigest() for name in names}
        reconciliation.merge(self.database, self.root, orchestrator.profile_key, orchestrator.name_key)
        for name in names:
            self.assertEqual(before[name], hashlib.sha256((self.root / "handoff" / name).read_bytes()).hexdigest())

    def test_global_contact_baselines_do_not_reduce_existing_counts(self):
        reconciliation.merge(self.database, self.root, orchestrator.profile_key, orchestrator.name_key)
        reconciliation.apply_baselines(self.database, self.pipeline, orchestrator.name_key)
        for name in ["Fixture Robotics", "Example Grid", "Sample AI"]:
            company = next(row for row in self.pipeline["companies"] if orchestrator.name_key(row["company"]) == orchestrator.name_key(name))
            self.assertGreaterEqual(company["conservative_known_contact_count"], 3)
