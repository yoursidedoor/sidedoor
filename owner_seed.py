"""Optional install-specific seed data, kept outside the repo (E1.T4).

The product ships no person's contacts, campaigns, interview history or résumé claims. An
install migrated from a personal setup may keep that data in `owner-seed.json` in the user's
config directory (`user_profile.home()`), or at the path in `$SIDEDOOR_OWNER_SEED`
(`$JOBAGENT_OWNER_SEED` for one release, E2.T1). It is read only when present; without it every
list below is empty and nothing is seeded.

Keys (all optional):
- `excluded_contact_names`: names (any case) never contacted by any route (EXCLUDED_CONTACT).
- `no_automatic_outreach`: `{"names": [...], "profiles": ["linkedin.com/in/x"], "campaign_ids":
  [n]}`, contacts who get no automatic outreach, follow-up or reply
  (NO_AUTOMATIC_OUTREACH_CONTACT / NO_AUTOMATIC_FOLLOWUP_CONTACT). A campaign id listed here is
  also never accepted as a newly prepared campaign.
- `prior_outreach_attempts`: `[{"identity", "campaign_id", "state", "evidence"}]`, written into
  `outreach_attempts` at initialization (INSERT OR IGNORE).
- `delivery_evidence_contacts`: `[{"recipient", "company", "profile", "campaign_id"}]`, imported
  as contacted history with the status in `backups/delivery-evidence.json`, which must be
  reconciled first.
- `baseline_priorities`: `{"pattern": regex the R09 paragraph must contain, "entries":
  [[company, "YYYY-MM-DD", scheduled_at or null], ...]}` for `conversion.seed_baseline_priorities`.
- `designated_claims`: résumé claim mappings for `research_workflow.seed_designated_claims`
  (`text`, `invitation_text`, `tags`, `page`).
"""

import json
import os
from pathlib import Path

import user_profile


VARIABLE = "SIDEDOOR_OWNER_SEED"
# Pre-rename name, read as a fallback for one release (E2.T1); remove after it.
LEGACY_VARIABLE = "JOBAGENT_OWNER_SEED"
FILENAME = "owner-seed.json"
_cache = {}


def path():
    configured = os.environ.get(VARIABLE) or os.environ.get(LEGACY_VARIABLE)
    return Path(configured).expanduser() if configured else user_profile.home() / FILENAME


def load():
    """The seed as a dict; {} when the file is absent."""
    location = path()
    try:
        status = location.stat()
    except OSError:
        return {}
    key = (str(location), status.st_mtime_ns, status.st_size)
    if key not in _cache:
        _cache.clear()
        seed = json.loads(location.read_text())
        if not isinstance(seed, dict):
            raise ValueError("OWNER_SEED_INVALID: " + str(location))
        _cache[key] = seed
    return _cache[key]


def _names(values):
    from orchestrator import name_key
    return frozenset(name_key(value) for value in values or () if isinstance(value, str) and value.strip())


def excluded_names():
    return _names(load().get("excluded_contact_names"))


def _no_automatic():
    value = load().get("no_automatic_outreach") or {}
    return value if isinstance(value, dict) else {}


def no_automatic_names():
    return _names(_no_automatic().get("names"))


def no_automatic_profiles():
    """Bare profile keys ("linkedin.com/in/x"), the spelling `orchestrator.profile_key` gives."""
    from orchestrator import profile_key
    return frozenset(profile_key(value if "://" in value else "https://" + value)
                     for value in _no_automatic().get("profiles") or () if isinstance(value, str) and value)


def no_automatic_campaigns():
    return frozenset(value for value in _no_automatic().get("campaign_ids") or () if type(value) is int)


def no_automatic(identities=(), names=(), campaign_id=None):
    """True when any identity, name or the campaign belongs to a no-automatic-outreach contact."""
    return bool(set(identities) & no_automatic_profiles() or set(names) & no_automatic_names()
                or (campaign_id is not None and campaign_id in no_automatic_campaigns()))


def items(key):
    value = load().get(key)
    return list(value) if isinstance(value, list) else []


def mapping(key):
    value = load().get(key)
    return dict(value) if isinstance(value, dict) else {}
