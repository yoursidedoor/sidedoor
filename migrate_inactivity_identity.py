#!/usr/bin/env python3.13
"""Carry pre-fix `outreach_inactivity_checks` rows into the canonical key space.

Background
----------
Before the `inactivity_closure.cycle` fix, the durable check row was written under
the raw candidate spelling that `candidates()` returned — in production the
`research_workflow.profile_key` form `https://www.linkedin.com/in/<slug>` — while
the lookup one line later read `engagement.identity_keys`, which yields the
`orchestrator.profile_key` form `linkedin.com/in/<slug>`. The two never matched,
so `attempts` never advanced past 1 and the retry backoff never engaged.

After the fix the loop keys everything canonically, so the old rows simply become
unreachable and a fresh canonical row is created. Convergence therefore does NOT
require this migration (see
`tests/test_inactivity_identity.py::test_legacy_rows_are_preserved_and_the_fix_converges_without_them`).

What it IS required for is honesty about spent work: the legacy row records one
already-charged provider attempt. Letting the canonical row restart at zero would
silently hand each affected recipient three fresh shared-compute reads, which
`AGENTS.md` forbids ("Retain original failed attempts and compute charges";
"never erase usage or silently raise normal daily limits").

Guarantees
----------
* **Legacy spend counts toward the canonical cap.** When the canonical row does
  not exist, a verbatim canonical copy is inserted (as before). When it DOES exist
  under the same attempt key (anchor_at), the legacy row's charged attempts are
  added to it: carried = max(legacy.attempts, legacy failure entries recorded in
  outreach_inactivity_audit), capped at 3. At the cap the canonical row becomes
  EXHAUSTED (next_at NULL for an undated recipient; the next 27-day boundary for a
  dated one). Before this change such rows were skipped, so production never
  counted the https-keyed attempts (the 2026-09-18 legacy rows have attempts=0
  because every capacity wait rolled the counter back, while their audit rows
  record the reviews that actually ran).
* **Legacy evidence untouched.** It never UPDATEs, DELETEs or renames a legacy
  `outreach_inactivity_checks` row and never touches `outreach_inactivity_audit`.
  The only rows it changes are canonical check rows, and every merge is recorded
  in `outreach_inactivity_identity_merges` (before/after attempts, carried count).
* **Idempotent.** A legacy row is merged at most once (keyed by the merge table);
  re-running after an apply is a no-op.
* **Dry run by default.** `--apply` is required to write anything.
* **No side effects beyond those tables.** It never closes, reopens or
  reclassifies a recipient and never contacts a provider.

Usage
-----
    python3.13 migrate_inactivity_identity.py --database <path>              # dry run
    python3.13 migrate_inactivity_identity.py --database <path> --apply      # write
    python3.13 migrate_inactivity_identity.py --database <path> --receipt out.json

Run it from the project root (it imports `engagement` and `orchestrator`), and run
it against a COPY first. It is not to be executed against
`state/orchestrator.sqlite3` without an explicit instruction, a held runner lock
and a backup. It was NOT executed against production by its author.
"""

import argparse
import contextlib
import datetime as dt
import json
import sqlite3
import sys

import engagement
from orchestrator import profile_key


TABLE = "outreach_inactivity_checks"


def canonical(value):
    """The one canonical recipient key: orchestrator.profile_key form."""
    return profile_key(value if "://" in value else "https://" + value)


MERGES = "outreach_inactivity_identity_merges"


def _legacy_failures(database, identity):
    count = 0
    for (payload,) in database.execute("SELECT payload FROM outreach_inactivity_audit WHERE identity=?", (identity,)):
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and "attempts" in entry and entry.get("state") in {"RETRY_WAIT", "EXHAUSTED"}:
            count += 1
    return count


def survey(database):
    """Classify every stored check row. Pure read; no writes, no provider access."""
    present = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if not present:
        return {"state": "NO_TABLE", "rows": []}
    audit = bool(database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach_inactivity_audit'").fetchone())
    merged = set()
    if database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MERGES,)).fetchone():
        merged = {row[0] for row in database.execute("SELECT legacy_identity FROM " + MERGES)}
    stored = {}
    for row in database.execute(
            "SELECT identity,anchor_at,attempts,checked_at,next_at,state,result FROM " + TABLE):
        stored[row[0]] = row
    findings = []
    for identity, row in sorted(stored.items()):
        entry = {"stored_identity": identity, "attempts": row[2], "state": row[5],
                 "anchor_at": row[1], "checked_at": row[3], "next_at": row[4]}
        try:
            target = canonical(identity)
        except (ValueError, TypeError) as error:
            entry.update(action="SKIP", reason="UNPARSEABLE_IDENTITY: " + str(error))
            findings.append(entry)
            continue
        entry["canonical_identity"] = target
        if target == identity:
            entry.update(action="SKIP", reason="ALREADY_CANONICAL")
            findings.append(entry)
            continue
        try:
            aliases = engagement.identity_keys(database, identity)
        except (ValueError, TypeError):
            aliases = [target]
        existing = [alias for alias in aliases if alias in stored and alias != identity and alias == canonical(alias)]
        carried = max(row[2], _legacy_failures(database, identity) if audit else 0)
        entry["legacy_charged_attempts"] = carried
        if identity in merged:
            entry.update(action="SKIP", reason="ALREADY_MERGED")
        elif existing:
            current = stored[existing[0]]
            if current[1] != row[1]:
                entry.update(action="SKIP", reason="DIFFERENT_ATTEMPT_KEY", existing_identity=existing[0])
            elif carried == 0:
                entry.update(action="SKIP", reason="NO_CHARGED_LEGACY_ATTEMPTS", existing_identity=existing[0])
            else:
                entry.update(action="MERGE_INTO_CANONICAL", reason="LEGACY_ATTEMPTS_COUNT_TOWARD_CANONICAL_CAP",
                             existing_identity=existing[0], canonical_attempts_before=current[2],
                             canonical_attempts_after=min(3, current[2] + carried))
                entry["_canonical"] = current
        else:
            entry.update(action="INSERT_CANONICAL_COPY",
                         reason="LEGACY_ROW_UNREACHABLE_BY_CANONICAL_LOOKUP",
                         carries_attempts=row[2])
        entry["_row"] = row
        findings.append(entry)
    return {"state": "SURVEYED", "rows": findings}


def _exhausted_next_at(anchor_at, now):
    if anchor_at == "UNKNOWN":
        return None
    due = engagement.timestamp(anchor_at) + dt.timedelta(days=27)
    periods = max(1, (now - due) // dt.timedelta(days=27) + 1)
    return (due + periods * dt.timedelta(days=27)).isoformat()


def migrate(database, *, apply=False, now=None):
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    result = survey(database)
    if result["state"] == "NO_TABLE":
        return dict(result, inserted=0, merged=0, skipped=0, applied=apply)
    inserted = merged = 0
    planned = [entry for entry in result["rows"] if entry["action"] in {"INSERT_CANONICAL_COPY", "MERGE_INTO_CANONICAL"}]
    if apply and planned:
        with database:
            database.execute("CREATE TABLE IF NOT EXISTS " + MERGES + """ (
                legacy_identity TEXT PRIMARY KEY, canonical_identity TEXT NOT NULL, carried_attempts INTEGER NOT NULL,
                canonical_attempts_before INTEGER, canonical_attempts_after INTEGER NOT NULL, merged_at TEXT NOT NULL)""")
            for entry in planned:
                row = entry["_row"]
                if entry["action"] == "INSERT_CANONICAL_COPY":
                    # Verbatim carry-forward of the recorded attempt, anchor, schedule and
                    # result. Nothing is recomputed, invented or reset.
                    database.execute(
                        "INSERT INTO " + TABLE + " VALUES (?,?,?,?,?,?,?)",
                        (entry["canonical_identity"], row[1], row[2], row[3], row[4], row[5], row[6]))
                    database.execute("INSERT INTO " + MERGES + " VALUES (?,?,?,?,?,?)",
                                     (entry["stored_identity"], entry["canonical_identity"], row[2], None, row[2], now.isoformat()))
                    inserted += 1
                    continue
                after = entry["canonical_attempts_after"]
                if after >= 3:
                    database.execute("UPDATE " + TABLE + " SET attempts=?,state='EXHAUSTED',next_at=? WHERE identity=?",
                                     (after, _exhausted_next_at(row[1], now), entry["existing_identity"]))
                else:
                    database.execute("UPDATE " + TABLE + " SET attempts=? WHERE identity=?", (after, entry["existing_identity"]))
                database.execute("INSERT INTO " + MERGES + " VALUES (?,?,?,?,?,?)",
                                 (entry["stored_identity"], entry["existing_identity"], entry["legacy_charged_attempts"],
                                  entry["canonical_attempts_before"], after, now.isoformat()))
                merged += 1
    for entry in result["rows"]:
        entry.pop("_row", None)
        entry.pop("_canonical", None)
    return {"state": "APPLIED" if apply else "DRY_RUN",
            "table": TABLE, "applied": apply, "inserted": inserted, "merged": merged,
            "planned": len(planned), "skipped": len(result["rows"]) - len(planned),
            "rows": result["rows"],
            "preserved": "No legacy check row and no outreach_inactivity_audit row was "
                         "modified, renamed or deleted.",
            "generated_at": now.isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="Path to the SQLite database (use a COPY first)")
    parser.add_argument("--apply", action="store_true", help="Write the canonical rows (default: dry run)")
    parser.add_argument("--receipt", help="Write the JSON receipt to this path as well as stdout")
    options = parser.parse_args(argv)

    uri = "file:" + options.database + ("" if options.apply else "?mode=ro")
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as database:
        report = migrate(database, apply=options.apply)
    text = json.dumps(report, indent=2, sort_keys=True)
    # Machine contract (E2.T6): the JSON report; stays print.
    print(text)
    if options.receipt:
        with open(options.receipt, "w") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
