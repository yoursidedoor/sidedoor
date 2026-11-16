"""Real-AI check of the place rule in research-prompt.md (owner picks M2 a and M3 a, 2026-11-16).

    python tools/place_rule_eval.py            runs `codex exec` once per profile and prompt (the signed-in Codex, its quota)

The verdict is the model's, so a unit test cannot prove the rule; this asks a real model to judge made-up
postings with the prompt's own policy_fit section and compares each location verdict (in_scope and
priority_index) with what the rule says, then asks the same of the application read's wording of the rule
(application_browser.py; in_scope only, the read gives no index), since both must mean the same. Exit 0 when
every case matches. `tests/test_place_rule.py` keeps the cases and the prompt wording in step.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
POSTINGS = {
    "hybrid_berlin": ("Backend Engineer", "Location: Berlin, Germany. Hybrid: 3 days a week in our Berlin office."),
    "remote_eu_or_berlin": ("Backend Engineer", "Location: Remote (EU) or our Berlin office, your choice."),
    "remote_us_only": ("Backend Engineer", "Location: Remote, US only. You must live and work in the United States."),
    "mode_not_stated": ("Backend Engineer", "Team: Engineering. Location: Berlin."),
    "onsite_austin": ("Backend Engineer", "Location: Austin, Texas. Onsite, 5 days a week in our Austin office."),
    "remote_us": ("Backend Engineer", "Location: Remote (United States). Work from anywhere in the US."),
    "onsite_chicago": ("Backend Engineer", "Location: Chicago, Illinois. Onsite, 5 days a week in our Chicago office."),
    "remote_canada_only": ("Backend Engineer", "Location: Remote, Canada only. You must live and work in Canada."),
}
# expected: posting -> (in_scope, priority_index)
PROFILES = {
    "remote-europe": {
        "location_priority": ["Anywhere in Europe remote"],
        "expected": {"hybrid_berlin": (False, None), "remote_eu_or_berlin": (True, 0),
                     "remote_us_only": (False, None), "mode_not_stated": (True, None)}},
    "austin-then-us-remote": {
        "location_priority": ["Austin, Texas, United States hybrid/onsite", "United States remote"],
        "expected": {"onsite_austin": (True, 0), "remote_us": (True, 1),
                     "onsite_chicago": (False, None), "remote_canada_only": (False, None)}},
    # No picks: the rule limits nothing.
    "no-places": {
        "location_priority": [],
        "expected": {"hybrid_berlin": (True, None), "onsite_chicago": (True, None)}},
}


def section():
    text = (ROOT / "research-prompt.md").read_text()
    return text[text.index("For every returned role also give policy_fit"):text.index("Also give in_user_home_area")]


def read_section():
    """The application read's wording: its scope_fit.location sentences and its place rule paragraph."""
    text = (ROOT / "application_browser.py").read_text()
    return (text[text.index("scope_fit.location: a place is excluded"):text.index("Research already judged this role")]
            + text[text.index("Place rule (context"):text.index("Level rule (context")])


def prompt(profile, read=False):
    roles = [{"id": key, "role": POSTINGS[key][0], "posting_text": POSTINGS[key][1]} for key in profile["expected"]]
    if read:
        data = {"scope_targets": {"location_priority": profile["location_priority"], "countries": []}, "roles": roles}
        return ("You are judging scope_fit.location only. Follow this part of the application read's instructions "
                "exactly, for every role in context.roles. Do not browse or run commands. Answer with only a JSON "
                "array of {id, in_scope, reason}.\n\n" + read_section() + "\n\ncontext:\n" + json.dumps(data, indent=1))
    data = {"policy": {"location_priority": profile["location_priority"]}, "roles": roles}
    return ("You are judging policy_fit.location only. Follow this section of the research instructions exactly, for "
            "every role in DATA.roles. Do not browse or run commands. Answer with only a JSON array of "
            "{id, in_scope, priority_index, reason}.\n\n" + section() + "\n\nDATA:\n" + json.dumps(data, indent=1))


def main():
    failed = 0
    for read in (False, True):
        for name, profile in PROFILES.items():
            done = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-"],
                                  input=prompt(profile, read), capture_output=True, text=True, timeout=600)
            found = re.findall(r"\[\s*\{.*\}\s*\]", done.stdout, re.S)
            verdicts = {item["id"]: (item.get("in_scope"), item.get("priority_index")) for item in json.loads(found[-1])} if found else {}
            for key, expected in profile["expected"].items():
                expected = (expected[0], None) if read else expected   # the read gives no index
                ok = verdicts.get(key) == expected
                failed += not ok
                print("%-8s %-22s %-20s %-14s %s" % ("read" if read else "research", name, key, verdicts.get(key),
                                                     "ok" if ok else "WRONG, expected " + str(expected)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
