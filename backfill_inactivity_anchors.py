#!/usr/bin/env python3.13
"""One-time, idempotent backfill of conservative 27-day inactivity anchors.

Why
---
`inactivity_closure.plan()` built its clock only from `engagement_events`. Manually
sent invitations have none, so every such recipient planned as
PRIOR_OUTREACH_TIMESTAMP_UNVERIFIED and was sent to a live browser review on every
cycle, although the send date is already recorded durably in the tracker.

What it records (owner decision D2)
-----------------------------------
One immutable row per contacted identity in `outreach_inactivity_anchors`. The
anchor is an UPPER BOUND on the original outreach instant, so it can only delay a
closure, never bring one forward:

* HeyReach first. When an `outreach_attempts` row binds the identity to a HeyReach
  campaign in ALREADY_SENT/CONFIRMED_SENT state, the anchor is the earliest
  recorded HeyReach observation of that single-contact campaign reporting
  `connectionsSent == 1` (HeyReach records no exact per-lead send instant;
  `startedAt` is kept as the lower bound). Sources: `workflow_tasks`
  provider-observation results, and the delivery evidence file named by the
  attempt row itself.
* Otherwise the recorded send date. Candidates are the `history.evidence`
  send_date / sent_at, the recovered ledger row in `reconciled_sources`, the
  spreadsheet tracker cell for the same recipient and message ("... sent MM/DD"),
  and the `imported_at` instant of those rows. The LATEST candidate wins. A bare
  calendar date becomes the following day at 12:00Z (end of that date in every
  civil timezone, the same rule as EXACT_DAY_UPPER_BOUND); a minute-precision UI
  time becomes that minute + 60 seconds.

No date is invented: an identity without such a durable source is left without a
row and keeps planning as UNKNOWN.

Guarantees: insert-only (an existing anchor is never changed; a differing
candidate is reported as CONFLICT), dry run by default (`--apply` writes), no
provider/browser/network access, and no table other than the anchor table is
written. Run against a COPY first; do not run against state/orchestrator.sqlite3
without an explicit instruction, a held runner lock and a backup.

    python3.13 backfill_inactivity_anchors.py --database <copy>            # dry run
    python3.13 backfill_inactivity_anchors.py --database <copy> --apply
"""

import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import sys

import engagement
import inactivity_closure
from orchestrator import name_key
import paths


SENT_STATES = ("ALREADY_SENT", "CONFIRMED_SENT")
TRACKER_CELL = re.compile(r"^(?:Connection invitation|Direct message) sent (\d{2})/(\d{2})$")


def _tables(database):
    return {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _day_bound(day):
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(12), dt.timezone.utc)


def _identity(value):
    try:
        return inactivity_closure.key(value) if isinstance(value, str) and "linkedin.com/in/" in value else None
    except ValueError:
        return None


def _heyreach_candidates(database, root, aliases):
    """Earliest HeyReach observation confirming the single-contact campaign sent."""
    if "outreach_attempts" not in _tables(database):
        return []
    found = []
    for identity, campaign_id, state, claimed_at, result in database.execute(
            "SELECT identity,campaign_id,state,claimed_at,result FROM outreach_attempts WHERE state IN (?,?)", SENT_STATES):
        if _identity(identity) not in aliases or type(campaign_id) is not int:
            continue
        observations = []
        if "workflow_tasks" in _tables(database):
            for task_id, text in database.execute(
                    "SELECT task_id,result FROM workflow_tasks WHERE worker='provider-observation' AND status='COMPLETE'"):
                value = _json(text)
                if value and value.get("observed_at"):
                    observations.append(("workflow_tasks:" + task_id, value["observed_at"], value))
        receipt = (_json(result) or {}).get("source")
        if isinstance(receipt, str) and receipt.startswith(paths.BACKUPS + "/") and ".." not in receipt:
            path = root / receipt
            if path.is_file():
                value = _json(path.read_text())
                if value and value.get("verified_at"):
                    observed = dt.datetime.strptime(value["verified_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=dt.timezone.utc)
                    observations.append((receipt, observed.isoformat(), value))
        for source, observed, value in observations:
            campaign = value.get("campaign") or {}
            totals = ((value.get("stats") or {}).get("overallStats")) or {}
            if (campaign.get("id") != campaign_id or (campaign.get("progressStats") or {}).get("totalUsers") != 1
                    or totals.get("connectionsSent") != 1 or not campaign.get("startedAt")
                    or type(campaign.get("linkedInUserListId")) is not int):
                continue
            started = engagement.timestamp(campaign["startedAt"].replace("Z", "+00:00"))
            instant = engagement.timestamp(observed)
            if instant < started:
                continue
            found.append({"anchor_at": instant, "source": source, "campaign_id": campaign_id,
                          "list_id": campaign["linkedInUserListId"], "started_at": campaign["startedAt"],
                          "sender_ids": campaign.get("campaignAccountIds"), "attempt_state": state,
                          "attempt_claimed_at": claimed_at})
    return sorted(found, key=lambda entry: entry["anchor_at"])


def _manual_candidates(database, aliases):
    """Every recorded send-date candidate for a manually sent outreach."""
    tables = _tables(database)
    candidates, names, messages = [], set(), set()
    if "history" in tables:
        for identity, recipient, profile, evidence in database.execute("SELECT identity,recipient,profile,evidence FROM history"):
            if aliases.isdisjoint({_identity(profile), _identity(identity)}):
                continue
            value = _json(evidence)
            if not value or not str(value.get("status", "")).lower().startswith(("invitation sent", "sent")):
                continue
            names.add(name_key(recipient or ""))
            if isinstance(value.get("message"), str):
                messages.add(value["message"])
            candidates.extend(_dated(value, "history:" + identity))
    if "reconciled_sources" in tables:
        for source_key, source, payload, imported_at in database.execute(
                "SELECT source_key,source,payload,imported_at FROM reconciled_sources"):
            value = _json(payload)
            if not value:
                continue
            reference = "reconciled_sources:" + source_key
            if _identity(value.get("profile")) in aliases and str(value.get("status", "")).lower().startswith(("invitation sent", "sent")):
                dated = _dated(value, reference)
                if dated:
                    candidates.extend(dated)
                    candidates.append({"anchor_at": engagement.timestamp(imported_at), "basis": "IMPORTED_AT_INSTANT",
                                       "source": reference, "text": imported_at})
                continue
            cells = value.get("cells") or {}
            row = value.get("row")
            if value.get("sheet") != "Tracker" or not isinstance(row, int):
                continue
            recipient, message, status = (cells.get(column + str(row)) for column in "EGH")
            if not names or name_key(recipient or "") not in names or message not in messages or not isinstance(status, str):
                continue
            matched = TRACKER_CELL.match(status.strip())
            imported = engagement.timestamp(imported_at)
            if not matched:
                continue
            day = dt.date(imported.year, int(matched[1]), int(matched[2]))
            if day > imported.date() + dt.timedelta(days=1):
                continue
            candidates.append({"anchor_at": _day_bound(day), "basis": "TRACKER_DATE_DAY_UPPER_BOUND",
                               "source": reference, "text": status})
            candidates.append({"anchor_at": imported, "basis": "IMPORTED_AT_INSTANT", "source": reference, "text": imported_at})
    return candidates


def _dated(value, source):
    if isinstance(value.get("sent_at"), str):
        try:
            sent = engagement.timestamp(value["sent_at"])
        except ValueError:
            sent = None
        if sent:
            return [{"anchor_at": sent + dt.timedelta(minutes=1), "basis": "UI_MINUTE_UPPER_BOUND",
                     "source": source, "text": value["sent_at"]}]
    if isinstance(value.get("send_date"), str):
        try:
            day = dt.date.fromisoformat(value["send_date"])
        except ValueError:
            return []
        return [{"anchor_at": _day_bound(day), "basis": "SEND_DATE_DAY_UPPER_BOUND", "source": source,
                 "text": value["send_date"]}]
    return []


def contacted(database):
    """Canonical identities with durable prior contact (the inactivity candidates)."""
    result = {}
    for candidate in inactivity_closure.candidates(database):
        identity = _identity(candidate)
        if identity and inactivity_closure.known_prior(database, identity):
            result.setdefault(identity, candidate)
    return sorted(result)


def backfill(database, *, apply=False, now=None, root=None):
    now = engagement.timestamp(now or dt.datetime.now(dt.timezone.utc))
    if apply:
        inactivity_closure.initialize_anchors(database)
        database.commit()
    present = "outreach_inactivity_anchors" in _tables(database)
    rows = []
    for identity in contacted(database):
        aliases = set(engagement.identity_keys(database, identity))
        heyreach = _heyreach_candidates(database, paths.data(root), aliases)
        manual = _manual_candidates(database, aliases)
        entry = {"identity": identity}
        if heyreach:
            chosen = heyreach[0]
            basis = "HEYREACH_SEND_OBSERVED_UPPER_BOUND"
            payload = {"rule": "EARLIEST_HEYREACH_OBSERVATION_CONFIRMING_CONNECTION_SENT",
                       "heyreach": {key: chosen[key] for key in ("campaign_id", "list_id", "started_at", "sender_ids",
                                                                  "attempt_state", "attempt_claimed_at")},
                       "lower_bound": chosen["started_at"], "chosen_source": chosen["source"],
                       "heyreach_candidates": [dict(item, anchor_at=item["anchor_at"].isoformat()) for item in heyreach[:5]],
                       "manual_candidates_not_used": [dict(item, anchor_at=item["anchor_at"].isoformat()) for item in manual]}
        elif manual:
            chosen = max(manual, key=lambda item: item["anchor_at"])
            basis = "SEND_DATE_UPPER_BOUND"
            payload = {"rule": "LATEST_RECORDED_SEND_DATE_CANDIDATE", "chosen_basis": chosen["basis"],
                       "chosen_source": chosen["source"], "chosen_text": chosen["text"],
                       "candidates": [dict(item, anchor_at=item["anchor_at"].isoformat()) for item in manual]}
        else:
            rows.append(dict(entry, action="NO_ANCHOR_RECORDED", reason="NO_DURABLE_SEND_DATE_SOURCE"))
            continue
        # A bound later than now is still a valid (merely looser) upper bound: the
        # send certainly happened before this record existed, so closure is only delayed.
        entry.update(anchor_at=chosen["anchor_at"].isoformat(), basis=basis, source=chosen["source"],
                     due_at=(chosen["anchor_at"] + dt.timedelta(days=inactivity_closure.DAYS)).isoformat(), payload=payload)
        previous = database.execute("SELECT anchor_at,basis,source FROM outreach_inactivity_anchors WHERE identity=?",
                                    (identity,)).fetchone() if present else None
        if previous:
            entry["action"] = "UNCHANGED" if tuple(previous) == (entry["anchor_at"], basis, chosen["source"]) else "CONFLICT_KEPT_EXISTING"
        elif apply:
            with database:
                entry["action"] = inactivity_closure.record_anchor(database, identity, chosen["anchor_at"], basis,
                                                                   chosen["source"], payload, now)
        else:
            entry["action"] = "WOULD_INSERT"
        rows.append(entry)
    return {"state": "APPLIED" if apply else "DRY_RUN", "generated_at": now.isoformat(), "rows": rows,
            "inserted": sum(row["action"] == "INSERTED" for row in rows),
            "not_recorded": sum(row["action"] == "NO_ANCHOR_RECORDED" for row in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="Path to the SQLite database (use a COPY first)")
    parser.add_argument("--apply", action="store_true", help="Insert missing anchors (default: dry run)")
    parser.add_argument("--receipt", help="Also write the JSON receipt here")
    options = parser.parse_args(argv)
    uri = "file:" + options.database + ("" if options.apply else "?mode=ro")
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as database:
        report = backfill(database, apply=options.apply)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    # Machine contract (E2.T6): the JSON report; stays print.
    print(text)
    if options.receipt:
        Path(options.receipt).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
