"""BY: the per-job cover letter is a real PDF, and only the file we generated can be attached."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import cover_letter


@unittest.skipUnless(importlib.util.find_spec("fpdf"), "fpdf2 not installed (the PDF writer)")
class CoverLetterTests(unittest.TestCase):
    TEXT = ("Dear team,\n\nI am applying for the platform role. At Fabrikam I introduced a schema "
            "registry and contract tests for 40 producer teams.\n\nSincerely,\nAlex Example")

    def test_rendered_pdf_is_readable_and_only_it_can_be_attached(self):
        root = Path(tempfile.mkdtemp())
        prepared = cover_letter.prepare(root, "aperture:a0000001", {
            "cover_letter_file": {"value": self.TEXT, "attachment": True},
            "typed_answer": "not an attachment"})
        self.assertEqual(list(prepared), ["cover_letter_file"])
        path = Path(prepared["cover_letter_file"]["path"])
        self.assertEqual(path.parent, root / cover_letter.DIRECTORY)
        self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), prepared["cover_letter_file"]["sha256"])
        cover_letter.verify(root, prepared)   # the generated file passes

        extracted = (subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True)
                     if shutil.which("pdftotext") else None)
        if extracted and extracted.returncode == 0:   # only assert where a PDF reader exists
            self.assertIn("40 producer teams", " ".join(extracted.stdout.split()))

        path.write_bytes(path.read_bytes() + b"%tampered")
        with self.assertRaises(ValueError):
            cover_letter.verify(root, prepared)

        elsewhere = root / "elsewhere.pdf"
        elsewhere.write_bytes(b"%PDF-1.4\n")
        with self.assertRaises(ValueError):
            cover_letter.verify(root, {"x": {"path": str(elsewhere),
                                             "sha256": hashlib.sha256(elsewhere.read_bytes()).hexdigest()}})

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf not installed (reads the text back)")
    def test_non_latin_text_comes_back_out_of_the_pdf(self):
        import pypdf
        text = "Łukasz Wójcik … Ğ Ş Ж Ελληνικά čřž"
        path = Path(tempfile.mkdtemp()) / "unicode.pdf"
        cover_letter.render(text, path)
        self.assertEqual(text, pypdf.PdfReader(str(path)).pages[0].extract_text().strip())

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf not installed (reads the text back)")
    def test_tabs_and_control_characters_print_as_spaces(self):
        import pypdf
        path = Path(tempfile.mkdtemp()) / "tabs.pdf"
        cover_letter.render("Skills:\tGo\x0bSQL\nNext line", path)
        self.assertEqual("Skills: Go SQL\nNext line", pypdf.PdfReader(str(path)).pages[0].extract_text().strip())

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf not installed (reads the text back)")
    def test_wrapped_lines_are_not_justified_with_extra_spaces(self):
        import pypdf
        text = "\n".join(["- Led the migration of a large billing system to a new event pipeline, cutting "
                          "nightly batch time and giving finance same-day numbers for every region, plus more "
                          "words here to wrap."] * 8)
        path = Path(tempfile.mkdtemp()) / "left.pdf"
        cover_letter.render(text, path)
        extracted = "".join(page.extract_text() for page in pypdf.PdfReader(str(path)).pages)
        self.assertGreater(len(extracted.splitlines()), 8, "the sample must wrap")
        self.assertFalse([line for line in extracted.splitlines() if "  " in line.strip()])


if __name__ == "__main__":
    unittest.main()
