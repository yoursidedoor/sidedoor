"""user_profile: profile.json + settings.json merge back into the old application policy.

The hash test is the proof that splitting the policy changes nothing downstream: every
fingerprint the code takes of the policy (parallel_research's policy_hash and scope_id, the
desktop adapter's policy_sha256, decision_transport's scoped policy hashes) is the same for the
merged dict as for the single file it replaced.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import decision_transport
import desktop_adapter
import parallel_research
import user_profile


ROOT = Path(__file__).resolve().parents[1]
# The last commit that carried the single application-policy.json (E1.T2): the split
# profile.json + settings.json must merge back to that file with identical fingerprints. (The
# file's own fingerprints are not pinned here: they are hashes of the owner's policy.)
ORIGINAL_COMMIT = "c630751"


def original_policy_text():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "show", ORIGINAL_COMMIT + ":application-policy.json"],
                              capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def fingerprints(policy):
    prints = {"text": json.dumps(policy),
              "policy_hash": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
              "scope_id": parallel_research.scope_id(policy),
              "policy_sha256": desktop_adapter.digest(policy)}
    for key, answer in sorted(policy["answers"].items()):
        semantic = answer.get("semantic") if isinstance(answer, dict) else None
        for kind in ("fact", "test"):
            prints[kind + ":" + key] = decision_transport.scoped_policy_hash(kind, key, semantic, policy)
    return prints


class UserProfileTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def round_trip(self, policy):
        user_profile.write_home(policy, self.home)
        return user_profile.load()

    def test_paths_follow_sidedoor_home(self):
        self.assertEqual(user_profile.profile_path(), self.home / "profile.json")
        self.assertEqual(user_profile.settings_path(), self.home / "settings.json")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=self.home):
            self.assertEqual(user_profile.home(), self.home / ".config" / "sidedoor")

    def test_pre_rename_names_still_work_for_one_release(self):
        legacy = self.home / "legacy"
        with mock.patch.dict(os.environ, {"JOBAGENT_HOME": str(legacy)}, clear=True):
            self.assertEqual(user_profile.home(), legacy)
        with mock.patch.dict(os.environ, {"JOBAGENT_HOME": str(legacy),
                                          "SIDEDOOR_HOME": str(self.home)}, clear=True):
            self.assertEqual(user_profile.home(), self.home)
        config = self.home / ".config"
        (config / "jobagent").mkdir(parents=True)
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=self.home):
            self.assertEqual(user_profile.home(), config / "jobagent")
            (config / "sidedoor").mkdir()
            self.assertEqual(user_profile.home(), config / "sidedoor")
        import owner_seed
        with mock.patch.dict(os.environ, {"JOBAGENT_OWNER_SEED": str(legacy / "seed.json")}, clear=True):
            self.assertEqual(owner_seed.path(), legacy / "seed.json")
        with mock.patch.dict(os.environ, {"JOBAGENT_OWNER_SEED": str(legacy / "seed.json"),
                                          "SIDEDOOR_OWNER_SEED": str(self.home / "seed.json")}, clear=True):
            self.assertEqual(owner_seed.path(), self.home / "seed.json")

    def test_openrouter_key_defaults_to_the_sidedoor_home(self):
        import text_model
        (self.home / "openrouter.key").write_text("sk-test\n", encoding="utf-8")
        self.assertEqual(text_model.key({"text_model": {"model": "m"}}), "sk-test")
        self.assertNotIn("job-search", (Path(__file__).resolve().parents[1] / "config.json").read_text(
            encoding="utf-8"))

    def test_missing_profile_says_to_run_setup(self):
        with self.assertRaisesRegex(user_profile.ProfileMissing, "Run setup"):
            user_profile.load()

    def test_profile_and_default_settings_merge_in_the_old_key_order(self):
        (self.home / "profile.json").write_text(user_profile.EXAMPLE_PROFILE.read_text())
        policy = user_profile.load()
        self.assertEqual(tuple(policy), user_profile.KEY_ORDER + ("setup_picks",))   # a key KEY_ORDER doesn't name goes last
        self.assertEqual(policy["max_attempts"], 3)
        self.assertEqual(len(policy["answers"]), 34)

    def test_user_settings_override_single_keys(self):
        (self.home / "profile.json").write_text(user_profile.EXAMPLE_PROFILE.read_text())
        (self.home / "settings.json").write_text(json.dumps({"max_attempts": 5}))
        policy = user_profile.load()
        self.assertEqual(policy["max_attempts"], 5)
        self.assertEqual(policy["retry_delays_seconds"], [900, 3600])

    def test_example_round_trips_with_identical_fingerprints(self):
        single = user_profile.merge(json.loads(user_profile.EXAMPLE_PROFILE.read_text()),
                                    json.loads(user_profile.DEFAULT_SETTINGS.read_text()))
        self.assertEqual(fingerprints(self.round_trip(single)), fingerprints(single))

    def test_original_policy_split_and_merged_has_identical_fingerprints(self):
        text = original_policy_text()
        if text is None:
            self.skipTest("git history with application-policy.json is not available")
        original = json.loads(text)
        profile, settings = user_profile.split(original)
        # The E1.T7 keys are optional: the old file has none, and merging adds none.
        self.assertEqual(set(profile), set(user_profile.PROFILE_KEYS) - set(user_profile.OPTIONAL_PROFILE_KEYS))
        self.assertEqual(settings, json.loads(user_profile.DEFAULT_SETTINGS.read_text()))
        (self.home / "profile.json").write_text(json.dumps(profile))
        merged = user_profile.load()
        self.assertEqual(merged, original)
        self.assertFalse(set(merged) & set(user_profile.OPTIONAL_PROFILE_KEYS))
        self.assertEqual(fingerprints(merged), fingerprints(original))


if __name__ == "__main__":
    unittest.main()
