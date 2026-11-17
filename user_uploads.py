"""A document the user supplies for one application's file control (owner, 2026-11-16, S2).

A required file control that is not the résumé (a transcript, a certificate, a portfolio PDF) is a
document Sidedoor does not have. It is asked on Approvals as a FILE question: the card takes a file,
never typed text, so nothing typed is ever printed into a PDF and uploaded as that document. The
file is saved under the Sidedoor home beside the résumé folder (`uploads/<job key, made safe>/`),
hashed, and the answer stored for the field is a marker naming the saved path, its sha256 and the
user's file name. At submit the file itself is attached (never rendered), and the submission gate
re-hashes it against the recorded marker, exactly as a generated cover letter is checked.
"""
import hashlib
import json
from pathlib import Path
import re

import user_profile

DIRECTORY = "uploads"                 # beside resume_intake.DIRECTORY ("resume") under the Sidedoor home
MARKER_KEY = "user_file"              # an answer carrying this key is a user-supplied file, never text
SAFE = re.compile(r"[^A-Za-z0-9._-]")
CHANGED = "USER_FILE_CHANGED"
INVALID = "USER_FILE_ATTACHMENT_INVALID"


def _home(home):
    return Path(home) if home is not None else user_profile.home()


FOLDER_CHARACTERS = 40     # of the safe job key, plus 8 of its hash (F5: Windows' 260-character paths)
STEM_CHARACTERS = 60       # of the file's safe name before its extension
EXTENSION_CHARACTERS = 10


def folder(job_key, home=None):
    key = str(job_key)
    return _home(home) / DIRECTORY / ((SAFE.sub("_", key)[:FOLDER_CHARACTERS].strip("._") or "job")
                                      + "-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8])


def saved_name(file_name, digest, decision_id):
    """`<16 of the sha256>-<12 of the card id>-<safe stem, at most 60><extension, at most 10>`: one path per
    card (F1: a discard can only ever touch its own file), short enough for Windows (F5)."""
    name = Path(str(file_name or "")).name
    suffix = SAFE.sub("", Path(name).suffix)[:EXTENSION_CHARACTERS]
    stem = SAFE.sub("_", Path(name).stem if suffix else name)[:STEM_CHARACTERS].strip("._") or "document"
    return digest[:16] + "-" + (SAFE.sub("", str(decision_id or ""))[:12] or "card") + "-" + stem + suffix


def save(job_key, file_name, data, home=None, decision_id=None):
    """Write the user's file for this card and return the answer marker for it."""
    name = Path(str(file_name or "")).name
    digest = hashlib.sha256(data).hexdigest()
    target = folder(job_key, home)
    target.mkdir(parents=True, exist_ok=True)
    path = target / saved_name(name, digest, decision_id)
    path.write_bytes(data)
    return {MARKER_KEY: str(path), "sha256": digest, "file_name": name or "document"}


def is_marker(value):
    """Is this stored answer a user-supplied file marker (`save`'s shape)?"""
    return (isinstance(value, dict) and set(value) == {MARKER_KEY, "sha256", "file_name"}
            and isinstance(value[MARKER_KEY], str) and isinstance(value["file_name"], str)
            and isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None)


def verify(marker, home=None):
    """True when the marker names a file under this home's uploads folder whose bytes still hash to it."""
    if not is_marker(marker):
        return False
    try:
        path = Path(marker[MARKER_KEY]).resolve()
        root = (_home(home) / DIRECTORY).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == marker["sha256"]
    except (OSError, ValueError):
        return False


def parse(raw):
    """(fits, marker) for a posted answer: the JSON marker of a verified saved file, else (False, None)."""
    try:
        marker = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return False, None
    return (True, marker) if verify(marker) else (False, None)


def discard(marker, home=None):
    """Remove a saved file whose answer was not recorded (a refused post). Never raises."""
    try:
        if is_marker(marker) and verify(marker, home):
            Path(marker[MARKER_KEY]).unlink()
    except OSError:
        pass


def attachments(questions, answers, home=None):
    """{field: {path, sha256, file_name, user_supplied: True}} for every file control whose answer is a
    user-supplied file marker. Raises ValueError(CHANGED) when such a file is missing or altered."""
    import applications
    found = {}
    for question in questions:
        if not applications.file_control(question):
            continue
        field = applications.question_field(question)
        value = answers.get(field)
        if not is_marker(value):
            continue
        if not verify(value, home):
            raise ValueError(CHANGED)
        found[field] = {"path": value[MARKER_KEY], "sha256": value["sha256"], "file_name": value["file_name"],
                        "user_supplied": True}
    return found


def verify_attachments(entries, recorded_answers, home=None):
    """The submission gate's check: every user-supplied attachment is the file the recorded answer for
    that field names (same path and sha256), under this home's uploads folder and unchanged. Raises."""
    for field, entry in sorted((entries or {}).items()):
        marker = (recorded_answers or {}).get(field)
        if (not isinstance(entry, dict) or not is_marker(marker) or entry.get("path") != marker[MARKER_KEY]
                or entry.get("sha256") != marker["sha256"] or not verify(marker, home)):
            raise ValueError(INVALID)
