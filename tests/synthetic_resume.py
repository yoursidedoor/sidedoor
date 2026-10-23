"""The synthetic designated resume (Alex Example) the tests run with.

The raw résumé's path, SHA-256 and saved text come from the user's profile.json `resume_raw` block
(reply_resume.designated(), research_workflow.designated_resume_sha256(), E4.T2); `resume` is the
application résumé (here the same PDF). Importing this module loads tests/profile_home.py, whose
example profile points at tests/fixtures/resume/alex-example-resume.pdf with its hash and at its
saved text, alex-example-resume.txt (`pdftotext -layout -enc UTF-8` of the PDF). Regenerate the PDF
with tests/fixtures/make_synthetic_resume.py, then the text, and update profile.example.json.
"""

import hashlib
from pathlib import Path

import profile_home  # noqa: F401 (example profile home and fictional owner seed)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "tests/fixtures/resume/alex-example-resume.pdf"
PATH = ROOT / SOURCE
SHA256 = hashlib.sha256(PATH.read_bytes()).hexdigest()
TEXT_SOURCE = "tests/fixtures/resume/alex-example-resume.txt"
TEXT_PATH = ROOT / TEXT_SOURCE
