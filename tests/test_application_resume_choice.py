"""Settings > Résumé "Use for application" (owner, 2026-10-24): which file applications upload.

`user_profile.application_resume` is the one resolver; the submit path and the designated-résumé
checks follow it.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import hashlib
import unittest
from pathlib import Path

import applications
import profile_home as homes
import test_applications_e2e as fixtures
import test_ats_browser
import user_profile


def record(path):
    return {"path": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        (folder / "raw.docx").write_bytes(b"original")
        (folder / "ats.pdf").write_bytes(b"ats")
        self.policy = {"resume": record(folder / "ats.pdf"), "resume_raw": record(folder / "raw.docx")}

    def test_default_is_the_ats_version(self):
        self.assertEqual(user_profile.application_resume(self.policy), self.policy["resume"])
        self.assertFalse(user_profile.original_missing(self.policy))

    def test_original_when_chosen(self):
        self.policy["application_resume"] = "original"
        self.assertEqual(user_profile.application_resume(self.policy), self.policy["resume_raw"])
        answers, missing = applications.resolve_answers(
            [{"field": "cv", "semantic": "resume", "required": True}],
            dict(self.policy, answers={"resume": {"value": self.policy["resume"]["path"], "semantic": "resume", "source": "pointer"}}))
        self.assertEqual((list(answers.values()), missing), ([self.policy["resume_raw"]["path"]], []))

    def test_missing_original_keeps_the_ats_version_and_says_so(self):
        self.policy["application_resume"] = "original"
        Path(self.policy["resume_raw"]["path"]).unlink()
        self.assertEqual(user_profile.application_resume(self.policy), self.policy["resume"])
        self.assertTrue(user_profile.original_missing(self.policy))


class OriginalChosenBrowserTests(test_ats_browser.BrowserAdapterTests):
    """The ATS browser's designated-résumé check with the original chosen."""

    def setUp(self):
        super().setUp()
        (self.root / "raw-original.docx").write_bytes(b"the user's own upload")
        self.policy["resume_raw"] = record(self.root / "raw-original.docx")
        self.policy["application_resume"] = "original"
        homes.use(self, self.policy)
        self.browser = test_ats_browser.ats_browser.Adapter(self.root, test_ats_browser.FakePage)

    def test_chosen_original_uploads_and_ats_version_is_refused(self):
        payload = self.claim()   # carries the ATS version's path
        self.assertEqual(self.browser.call("submit", payload)["blocked"]["code"], "DESIGNATED_RESUME_MISMATCH")
        self.assertEqual(test_ats_browser.FakePage.submits, 0)
        payload["resume"] = self.policy["resume_raw"]["path"]
        self.assertTrue(self.browser.call("submit", payload)["requires_independent_receipt"])
        self.assertEqual(test_ats_browser.FakePage.submits, 1)


# Only the new tests run from these subclasses, not every inherited one again.
for name in [name for name in dir(test_ats_browser.BrowserAdapterTests) if name.startswith("test_")]:
    if name not in OriginalChosenBrowserTests.__dict__:
        setattr(OriginalChosenBrowserTests, name, None)


class OriginalChosenCycleTests(fixtures.ApplicationEndToEndTests):
    """applications.process: the submission carries the chosen file, hash-checked."""

    def setUp(self):
        super().setUp()
        (self.root / "raw-original.docx").write_bytes(b"the user's own upload")
        self.policy["resume_raw"] = record(self.root / "raw-original.docx")
        self.policy["application_resume"] = "original"
        homes.use(self, self.policy)

    def test_submission_uploads_the_original(self):
        self.discover()
        self.cycle()
        self.assertEqual([Path(payload["resume"]).name for operation, payload in self.adapter.calls if operation == "submit"],
                         ["raw-original.docx"])

    def test_changed_original_blocks_submission(self):
        self.discover()
        (self.root / "raw-original.docx").write_bytes(b"changed after it was chosen")
        self.cycle()
        self.assertEqual(self.job()["blocker"], "DESIGNATED_RESUME_MISMATCH")


for name in [name for name in dir(fixtures.ApplicationEndToEndTests) if name.startswith("test_")]:
    if name not in OriginalChosenCycleTests.__dict__:
        setattr(OriginalChosenCycleTests, name, None)


if __name__ == "__main__":
    unittest.main()
