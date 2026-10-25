"""The user's profile and the system settings, merged into the one application policy.

Until E1.T3 the whole policy lived in `application-policy.json` at the repo root, mixing the
owner's facts with system knobs. It is now two files:

- `profile.json`: the user's facts and preferences (`PROFILE_KEYS`). It lives in the user's
  config directory and never in git: `$SIDEDOOR_HOME/profile.json`, else
  `~/.config/sidedoor/profile.json`. `profile.example.json` at the repo root shows its shape for
  a fictional user. For one release the pre-rename names still work (E2.T1): `$JOBAGENT_HOME`
  when `$SIDEDOOR_HOME` is unset, and `~/.config/jobagent` when `~/.config/sidedoor` does not
  exist.
- `settings.json`: the system knobs (`SETTINGS_KEYS`). The repo ships the defaults
  (`settings.json` at the repo root); an optional `settings.json` in the same config directory
  overrides individual keys.

`load()` returns ONE dict with exactly the structure (keys and key order) the old file had, so
every caller and every hash over the policy is unchanged.

Two résumés (E4.T2, owner 2026-10-21, "Keep Both"): `resume_raw` is the user's own upload and its
text saved once at upload (`reply_resume` reads it for claims, replies, packets, research and the
composer); `resume` is the ATS version written from that text. `resume_intake.ingest` writes both.

Which of the two applications upload (owner, 2026-10-24, Settings > Résumé "Use for application"):
`application_resume` is "original" for the user's own upload, else (absent, "ats") the ATS version.
`application_resume(policy)` returns the chosen file's record ({"path", "sha256", ...}); every
upload and every designated-résumé check (`applications`, `desktop_adapter`, `ats_browser`) reads
it, so the strict checks follow the choice. A re-upload or retry keeps the choice (ingest writes
only `resume` and `resume_raw`).
"""

import resources
import json
import os
from pathlib import Path


DEFAULT_SETTINGS = resources.path("settings.json")
EXAMPLE_PROFILE = resources.path("profile.example.json")
HOME_VARIABLE = "SIDEDOOR_HOME"
# Pre-rename names, read as fallbacks for one release (E2.T1); remove after it.
LEGACY_HOME_VARIABLE = "JOBAGENT_HOME"
CONFIG_DIRECTORY = "sidedoor"
LEGACY_CONFIG_DIRECTORY = "jobagent"

SETTINGS_KEYS = ("max_attempts", "max_reconcile_attempts", "retry_delays_seconds",
                 "preapproved_applications", "referrals_block_applications")
PROFILE_KEYS = ("source", "minimum_base_salary_usd", "desired_base_salary_usd", "salary_relaxation",
                "funding_stages", "industries", "levels", "minimum_years_experience",
                "location_priority", "onsite_location_exclusions", "countries",
                "target_specializations", "resume", "resume_raw", "application_resume", "answers")
# Added by E1.T7 (countries, target_specializations) and E4.T2 (resume_raw). Optional: a profile
# without them restricts and ranks nothing, and merge() adds
# a key only when the profile has it, so an older profile's merged dict and hashes are unchanged.
OPTIONAL_PROFILE_KEYS = ("countries", "target_specializations", "resume_raw", "application_resume")
# The key order of the old application-policy.json, kept so the merged dict serializes as before.
KEY_ORDER = ("source", "minimum_base_salary_usd", "desired_base_salary_usd", "salary_relaxation",
             "funding_stages", "industries", "levels", "minimum_years_experience",
             "location_priority", "onsite_location_exclusions", "countries",
             "target_specializations", "max_attempts",
             "max_reconcile_attempts", "retry_delays_seconds", "preapproved_applications",
             "referrals_block_applications", "resume", "resume_raw", "answers")


APPLICATION_RESUME = "application_resume"
ORIGINAL = "original"
ATS = "ats"


def application_resume(policy):
    """The résumé record applications upload: the user's original when they chose it, else `resume`.

    When the original was chosen but its file is no longer on this computer, the ATS version is
    used; Settings > Résumé says so (`original_missing`).
    """
    raw = policy.get("resume_raw")
    if (policy.get(APPLICATION_RESUME) == ORIGINAL and isinstance(raw, dict) and isinstance(raw.get("path"), str)
            and raw.get("sha256") and Path(raw["path"]).is_file()):
        return raw
    return policy.get("resume") if isinstance(policy.get("resume"), dict) else {}


def original_missing(policy):
    """True when the original was chosen for applications but the ATS version is used instead."""
    return policy.get(APPLICATION_RESUME) == ORIGINAL and application_resume(policy) is not policy.get("resume_raw")


class ProfileMissing(FileNotFoundError):
    pass


def home():
    configured = os.environ.get(HOME_VARIABLE) or os.environ.get(LEGACY_HOME_VARIABLE)
    if configured:
        return Path(configured).expanduser()
    default = Path.home() / ".config" / CONFIG_DIRECTORY
    legacy = Path.home() / ".config" / LEGACY_CONFIG_DIRECTORY
    return legacy if not default.exists() and legacy.is_dir() else default


def profile_path():
    return home() / "profile.json"


def settings_path():
    return home() / "settings.json"


def merge(profile, settings):
    combined = dict(profile)
    combined.update(settings)
    merged = {key: combined[key] for key in KEY_ORDER if key in combined}
    merged.update((key, value) for key, value in combined.items() if key not in merged)
    return merged


def split(policy):
    """(profile, settings) for one merged policy dict; the inverse of `merge`."""
    settings = {key: policy[key] for key in SETTINGS_KEYS if key in policy}
    profile = {key: value for key, value in policy.items() if key not in SETTINGS_KEYS}
    return profile, settings


def load():
    path = profile_path()
    if not path.is_file():
        raise ProfileMissing(f"No user profile at {path}. Run setup to create it (until the setup "
                             f"wizard exists: copy profile.example.json there and edit it, or set "
                             f"{HOME_VARIABLE} to the directory that holds your profile.json).")
    settings = json.loads(DEFAULT_SETTINGS.read_text())
    override = settings_path()
    if override.is_file():
        settings.update(json.loads(override.read_text()))
    return merge(json.loads(path.read_text()), settings)


def write_home(policy, directory):
    """Write one merged policy dict as profile.json + settings.json into `directory`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    profile, settings = split(policy)
    (directory / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    (directory / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    return directory


def use_policy(policy, directory):
    """Write `policy` into `directory` and point this process's SIDEDOOR_HOME at it."""
    write_home(policy, directory)
    os.environ[HOME_VARIABLE] = str(directory)
    return Path(directory)
