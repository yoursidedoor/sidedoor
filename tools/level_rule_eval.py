"""Real-AI check of the level rule in research-prompt.md (a beta user's feedback, 2026-11-16; owner picks L1 a+b, L2 b).

    python tools/level_rule_eval.py            runs `codex exec` once per profile (the signed-in Codex, its quota)

The verdict is the model's, so a unit test cannot prove the rule; this asks a real model to judge made-up
postings with the prompt's own "Role fit" section and compares each fit with what the rule says. Exit 0 when
every case matches. `tests/test_level_rule.py` keeps the cases and the prompt wording in step.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
POSTINGS = {
    "senior": ("Senior Software Engineer, Backend", "6+ years building distributed systems. Own the design of services end to end, mentor engineers."),
    "manager": ("Engineering Manager, Platform", "Manage a team of 8 engineers. 3+ years of people management. Hiring, performance, roadmap."),
    "entry": ("Software Engineer I (New Grad)", "0-2 years. Work with a mentor to build and test backend features in Python."),
    "staff": ("Staff Software Engineer", "10+ years. Set technical direction across several teams."),
    "senior_by_years": ("Software Engineer", "5+ years of professional experience required; lead projects independently and review others' designs."),
    "unclear": ("Backend Engineer", "Join our backend team building APIs in Python. We hire at several levels."),
    "director": ("Director of Engineering", "Lead a 40-person organization through managers. 12+ years."),
    "principal": ("Principal Engineer", "15+ years. Company-wide technical strategy; reports to the CTO."),
}
KEPT, OUT = ("STRONG", "PARTIAL"), ("NONE",)
PROFILES = {
    "entry-level": {
        "levels": ["Entry-level / Junior Software Engineering"],
        "resume": "Ada Example. BSc Computer Science 2026. Internship at Northwind (summer 2025): built REST endpoints "
                  "in Python, wrote unit tests. Skills: Python, SQL, Git.",
        "expected": {"senior": OUT, "manager": OUT, "entry": KEPT, "staff": OUT, "senior_by_years": OUT,
                     "unclear": ("PARTIAL",), "director": OUT, "principal": OUT}},
    "senior": {
        "levels": ["Senior Software Engineering"],
        "resume": "Sam Example. Senior Software Engineer at Northwind 2021-2026 (7 years total experience): led design "
                  "of a job scheduler, mentored 3 engineers. Python, Go, Kubernetes, Postgres.",
        # One level above (Staff) is kept as PARTIAL; two or more above and anything below is out.
        "expected": {"senior": KEPT, "entry": OUT, "staff": ("PARTIAL",), "senior_by_years": KEPT,
                     "unclear": ("PARTIAL",), "director": OUT, "principal": OUT}},
}


def section():
    text = (ROOT / "research-prompt.md").read_text()
    return text[text.index("## Role fit"):text.index("For every returned role also give policy_fit")]


def prompt(profile):
    data = {"policy": {"levels": profile["levels"]}, "candidate_resume": {"text": profile["resume"]},
            "roles": [{"id": key, "role": title, "posting_text": text} for key, (title, text) in POSTINGS.items()]}
    return ("You are judging role fit only. Follow this section of the research instructions exactly, for every role "
            "in DATA.roles. Do not browse or run commands. Answer with only a JSON array of {id, fit, reason}.\n\n"
            + section() + "\n\nDATA:\n" + json.dumps(data, indent=1))


def main():
    failed = 0
    for name, profile in PROFILES.items():
        done = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-"],
                              input=prompt(profile), capture_output=True, text=True, timeout=600)
        found = re.findall(r"\[\s*\{.*\}\s*\]", done.stdout, re.S)
        fits = {item["id"]: item["fit"] for item in json.loads(found[-1])} if found else {}
        for key, allowed in profile["expected"].items():
            ok = fits.get(key) in allowed
            failed += not ok
            print("%-12s %-16s %-8s %s" % (name, key, fits.get(key), "ok" if ok else "WRONG, expected " + " or ".join(allowed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
