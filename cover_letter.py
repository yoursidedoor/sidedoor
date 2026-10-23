"""Per-job cover letter: the answering AI writes the text, this renders it to a PDF file.

Owner decision 2026-09-19 (asked whether a cover letter can be uploaded when a form requires
one as a file): generate one per job rather than keep a standing letter.

The PDF is a single-font text document wrapped to the page, written with fpdf2 and the bundled
DejaVu Sans font (assets/fonts, owner 2026-10-21, E4.T2 review: "option (a)"), so Latin
extended, Greek and Cyrillic letters and typographic marks print as themselves. DejaVu Sans has
no CJK glyphs, so Chinese, Japanese and Korean characters are not rendered (owner, 2026-10-21:
no CJK font). The ATS résumé (`resume_intake`) is rendered the same way. Nothing is
invented - the text is exactly what the answering step produced for that question, and the
file's sha256 is recorded so `desktop_adapter`'s submission gate can verify the file the browser
attaches is this one and nothing else.
"""
import resources
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import paths

DIRECTORY = paths.STATE + "/cover-letters"   # under the data folder (paths.state)
FONT = resources.path("assets/fonts/DejaVuSans.ttf")
FONT_SIZE = 11
LEADING = 15
LEFT = 72
TOP = 720
BOTTOM = 72
WIDTH = 468          # 6.5 inches at 72 dpi
MAX_CHARACTERS = 6000


def render(text, path, max_characters=MAX_CHARACTERS):
    """Write `text` as a PDF at `path`; returns its sha256.

    US Letter, 1-inch margins, 11 pt DejaVu Sans on 15 pt lines, 6.5-inch text width (the layout
    the previous built-in writer used). The ATS résumé (`resume_intake`, E4.T2) passes a larger
    `max_characters` than a cover letter. fpdf2 is imported here, so the rest of the product
    loads without it; without it no PDF can be written (PDF_WRITER_MISSING).
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("COVER_LETTER_TEXT_EMPTY")
    if len(text) > max_characters:
        raise ValueError("COVER_LETTER_TEXT_TOO_LONG")
    try:
        from fpdf import FPDF
    except ImportError:
        raise ValueError("PDF_WRITER_MISSING: fpdf2 is not installed") from None
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_margins(LEFT, 792 - TOP, 612 - LEFT - WIDTH)
    pdf.set_auto_page_break(True, margin=BOTTOM)
    # Tabs and other control characters print as spaces, as the previous writer's word wrap did.
    text = "".join(" " if character != "\n" and unicodedata.category(character) == "Cc" else character
                   for character in text.replace("\r\n", "\n").replace("\r", "\n"))
    pdf.add_font("DejaVuSans", fname=str(FONT))
    pdf.set_font("DejaVuSans", size=FONT_SIZE)
    pdf.add_page()
    # Left-aligned: justified lines are stretched with extra spaces, which a text extractor (and
    # so an applicant tracking system) reads as double spaces between words.
    pdf.multi_cell(WIDTH, LEADING, text, align="L")
    output = bytes(pdf.output())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)
    return hashlib.sha256(output).hexdigest()


def prepare(root, job_key, answers):
    """Render every attachment answer to a PDF. Returns {question_key: {path, sha256, text_sha256}}.

    An attachment answer is one the answering step marked `attachment` because the employer's
    control takes a file and the AI wrote its text (a cover letter). The resume is never one of
    these: it is the designated file, carried out of band.
    """
    prepared = {}
    for key, value in sorted((answers or {}).items()):
        if not isinstance(value, dict) or not value.get("attachment") or not isinstance(value.get("value"), str):
            continue
        text = value["value"]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", job_key)[:80] + "-" + digest[:16] + ".pdf"
        path = Path(root) / DIRECTORY / name
        prepared[key] = {"path": str(path), "sha256": render(text, path), "text_sha256": digest}
    return prepared


def verify(root, attachments):
    """Every attachment is a file this product generated, unchanged. Raises on anything else."""
    directory = (Path(root) / DIRECTORY).resolve()
    for key, entry in sorted((attachments or {}).items()):
        path = Path(entry["path"]).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError("COVER_LETTER_ATTACHMENT_OUTSIDE_GENERATED_DIRECTORY")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("COVER_LETTER_ATTACHMENT_CHANGED")
    return json.dumps(sorted(attachments or {}), sort_keys=True)
