"""The user's raw résumé text, as saved once at upload (E4.T2), for every résumé-grounded reader.

Owner, 2026-10-21 (E4.T2, option A, "Keep Both"): profile.json keeps two résumés. `resume_raw` is
the user's own upload {path, sha256, text_path, text_sha256, page_count, format, extraction}; its
text was extracted once by `resume_intake.ingest` and saved at `text_path`. Claims, replies,
answering packets, research, the recovery coordinator and the composer read that saved text
here; nothing runs an extractor on read. `resume` is the application résumé (the ATS version),
the only file applications upload. No comparison is made on read ("NO python checks or
comparisons").
"""

import hashlib
from pathlib import Path

import user_profile


def _raw():
    try:
        raw = user_profile.load().get("resume_raw") or {}
    except user_profile.ProfileMissing:
        raise ValueError("REPLY_RESUME_UNAVAILABLE") from None
    if not isinstance(raw, dict) or not raw.get("path") or not raw.get("text_path"):
        raise ValueError("REPLY_RESUME_UNAVAILABLE")
    return raw


def designated():
    """(path, sha256) of the user's raw uploaded résumé: profile.json `resume_raw`."""
    raw = _raw()
    return raw["path"], raw.get("sha256")


def source():
    return designated()[0]


def sha256():
    return designated()[1]


def context(root=None):
    """The saved raw résumé text, read from `resume_raw.text_path` (relative to root unless absolute).

    Returns a fresh packet so callers cannot mutate shared evidence.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    raw = _raw()
    try:
        content = (root / raw["text_path"]).read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError):
        raise ValueError("REPLY_RESUME_UNAVAILABLE") from None
    return {"source": raw["path"], "source_sha256": raw.get("sha256"), "page_count": raw.get("page_count"),
            "extraction": raw.get("extraction"), "text": text,
            "text_sha256": hashlib.sha256(content).hexdigest(), "text_bytes": len(content)}
