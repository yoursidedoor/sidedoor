"""Stored send-date anchors stop redundant inactivity reviews (owner decision D2)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import copy
import datetime as dt
import json
import unittest

import engagement
import inactivity_closure
import manual_inactivity_browser
import manual_inactivity_closure as manual
import orchestrator
import outreach_history_browser
import workflow_tasks


NOW, START, PROFILE, CONFIG = manual.NOW, manual.START, manual.PROFILE, manual.CONFIG
IDENTITY = orchestrator.profile_key(PROFILE)


def manual_invitation(database, send_date="2026-09-16"):
    """Production shape of a manually sent invitation: history row, no engagement event."""
    evidence = {"recipient": "Synthetic Recipient", "profile": PROFILE, "message": "Hi, synthetic note.",
                "status": "Invitation sent; pending", "send_date": send_date,
                "timestamp_precision": "Relative UI time only"}
    with database:
        database.execute("INSERT INTO history VALUES (?,?,?,?,?)", ("name:synthetic recipient", "Synthetic Recipient",
                         "Synthetic Company", PROFILE, json.dumps(evidence)))


class AnchorAndBackoffTests(unittest.TestCase):
    def setUp(self):
        self.database, self.root = self.enterContext(manual.fixture())
        self.calls = []

    def refresh(self, blocker="synthetic unavailable history"):
        def run(profile, now):
            self.calls.append(now)
            return {"state": "UNKNOWN", "blocker": blocker}
        return run

    def test_stored_upper_bound_anchor_plans_active_and_makes_no_review_call(self):
        manual_invitation(self.database)
        anchor = NOW - dt.timedelta(days=2)
        # Raw SQL on purpose: the schema is part of the contract, not a helper under test.
        with self.database:
            self.database.execute("""CREATE TABLE IF NOT EXISTS outreach_inactivity_anchors (
                identity TEXT PRIMARY KEY, anchor_at TEXT NOT NULL, basis TEXT NOT NULL,
                source TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)""")
            self.database.execute("INSERT INTO outreach_inactivity_anchors VALUES (?,?,?,?,?,?)",
                (IDENTITY, anchor.isoformat(), "SEND_DATE_UPPER_BOUND", "history:name:synthetic recipient",
                 NOW.isoformat(), "{}"))
        planned = inactivity_closure.plan(self.database, PROFILE, NOW)
        self.assertEqual(planned["state"], "ACTIVE", planned)
        self.assertEqual(engagement.timestamp(planned["due_at"]), anchor + dt.timedelta(days=27), planned)
        inactivity_closure.cycle(self.database, CONFIG, manual.ForbiddenClient(), NOW, refresh=self.refresh())
        self.assertEqual(self.calls, [])

    def test_backfill_takes_latest_recorded_date_is_idempotent_and_never_invents_one(self):
        import backfill_inactivity_anchors as backfill
        manual_invitation(self.database)
        undated = "https://www.linkedin.com/in/synthetic-undated/"
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  ("name:undated", "Undated", "Synthetic Company", undated, "prior outreach, no date"))
            self.database.execute("""CREATE TABLE IF NOT EXISTS reconciled_sources (source_key TEXT PRIMARY KEY,
                source TEXT NOT NULL, payload TEXT NOT NULL, imported_at TEXT NOT NULL)""")  # reconciliation.py schema
            self.database.execute("INSERT INTO reconciled_sources VALUES (?,?,?,?)", ("tracker-row", "tracker.xlsx",
                json.dumps({"sheet": "Tracker", "row": 7, "cells": {"E7": "Synthetic Recipient", "G7": "Hi, synthetic note.",
                            "H7": "Connection invitation sent 09/17"}}), "2026-09-17T09:24:05+00:00"))
        first = backfill.backfill(self.database, apply=True, now=NOW)
        rows = {row["identity"]: row for row in first["rows"]}
        self.assertEqual(rows[IDENTITY]["anchor_at"], "2026-09-18T12:00:00+00:00", rows[IDENTITY])
        self.assertEqual(rows[orchestrator.profile_key(undated)]["action"], "NO_ANCHOR_RECORDED")
        second = backfill.backfill(self.database, apply=True, now=NOW)
        self.assertEqual({row["action"] for row in second["rows"]}, {"UNCHANGED", "NO_ANCHOR_RECORDED"})
        self.assertEqual(inactivity_closure.plan(self.database, undated, NOW)["anchor_at"], "UNKNOWN")

    def test_identical_blocker_backs_off_a_day_and_exhausted_review_rearms_next_period(self):
        manual.record(self.database, "initial_sent", START)
        refresh = self.refresh()
        for offset in (dt.timedelta(0), dt.timedelta(minutes=15), dt.timedelta(minutes=76)):
            inactivity_closure.cycle(self.database, CONFIG, manual.ForbiddenClient(), NOW + offset, refresh=refresh)
        self.assertEqual(len(self.calls), 2, "identical failure retried within the hour")
        inactivity_closure.cycle(self.database, CONFIG, manual.ForbiddenClient(),
                                 NOW + dt.timedelta(minutes=15, hours=24), refresh=refresh)
        state, next_at = self.database.execute("SELECT state,next_at FROM outreach_inactivity_checks").fetchone()
        self.assertEqual((len(self.calls), state), (3, "EXHAUSTED"))
        self.assertEqual(engagement.timestamp(next_at), NOW + dt.timedelta(days=27))
        inactivity_closure.cycle(self.database, CONFIG, manual.ForbiddenClient(), NOW + dt.timedelta(days=27), refresh=refresh)
        self.assertEqual(len(self.calls), 4, "exhausted review never re-armed")

    def test_one_unverifiable_quote_keeps_the_verified_pending_observation(self):
        current = dt.datetime.now(dt.timezone.utc)
        cutoff = (current - dt.timedelta(days=27)).isoformat()
        request = {"profile": manual_inactivity_browser.PROFILE, "sender_id": 123, "known_accepted": False,
                   "sender_profile": manual_inactivity_browser.SENDER, "read_only": True, "max_reads": 32,
                   "aliases": [orchestrator.profile_key(manual_inactivity_browser.PROFILE)],
                   "window_start": cutoff, "window_end": current.isoformat()}
        result, trace = manual_inactivity_browser.fixture(request)
        result = copy.deepcopy(result)
        result["evidence"].append({"kind": "MESSAGE_MAIN", "tool_call_id": None, "quote": "never observed on screen"})
        checked = outreach_history_browser.normalize_inactivity(request, result, trace, current.isoformat(), "browser-history:t")
        self.assertEqual(checked["state"], "UNKNOWN")
        self.assertEqual([entry["kind"] for entry in checked["observations"]], ["PENDING_INVITATION"])


if __name__ == "__main__":
    unittest.main()
