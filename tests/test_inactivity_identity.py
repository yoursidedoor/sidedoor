"""Identity-key regression tests for permanent 27-day inactivity closure.

These reproduce the production shape that `tests/test_inactivity_closure.py` never
exercised: `rw_contacts.identity` / `history.profile` hold `research_workflow`
spellings (`https://www.linkedin.com/in/<slug>`), while every read in the closure,
engagement and suppression machinery uses the `orchestrator.profile_key` spelling
(`linkedin.com/in/<slug>`).  Against the unfixed module the convergence and
starvation tests below fail; the collision, review-hold and never-reopen tests are
the guards against fixing the mismatch in the wrong direction.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import datetime as dt
import unittest

import engagement
import inactivity_closure
import manual_inactivity_closure as manual
import orchestrator
import research_workflow


CONFIG = manual.CONFIG
NOW = manual.NOW
START = manual.START

# The exact shape research_workflow writes and inactivity_closure.candidates() reads.
STUCK_ONE = "https://www.linkedin.com/in/aaa-stuck-one"
STUCK_TWO = "https://www.linkedin.com/in/aaa-stuck-two"
HEALTHY = "https://www.linkedin.com/in/zzz-healthy"


class Forbidden:
    def call(self, *args, **kwargs):
        raise AssertionError("Injected-refresh checks must not reach a provider")


def raw_contact(database, profile, *, historical=1, company=None):
    """Insert rw_contacts exactly as research_workflow.py:190/350 does."""
    identity = research_workflow.profile_key(profile)
    with database:
        database.execute("INSERT OR IGNORE INTO rw_contacts VALUES (?,?,?,?,?)",
                         (company or ("company-" + identity.rsplit("/", 1)[-1]), identity,
                          research_workflow.name_key(identity.rsplit("/", 1)[-1]),
                          historical, None if historical else "synthetic-plan"))


def raw_history(database, profile, *, name=None):
    """Insert history exactly as the recovered tracker does: raw URL in `profile`."""
    slug = profile.rstrip("/").rsplit("/", 1)[-1]
    with database:
        database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                         ("name:" + (name or slug), name or slug, "Synthetic Company",
                          profile, "synthetic prior outreach without verified date"))


def unknown_history_recipient(database, profile):
    """Production shape of kmorales / lena-voss before their anchors: prior contact, no verified date."""
    raw_contact(database, profile)
    raw_history(database, profile)


def stuck_recipient(database, profile):
    """Raw-spelling prior contact that is DUE and whose reads keep failing. Undated people are
    no longer read at all (owner, 2026-09-30), so the cap/identity cases use a dated one."""
    unknown_history_recipient(database, profile)
    manual.record(database, "initial_sent", START, profile=profile,
                  source_id="stuck-initial-" + profile.rsplit("/", 1)[-1])


def due_recipient(database, profile, *, source_id=None):
    raw_contact(database, profile, historical=0)
    manual.record(database, "initial_sent", START, profile=profile,
                  source_id=source_id or ("initial-" + profile.rsplit("/", 1)[-1]))


def rows(database):
    return [tuple(row) for row in database.execute(
        "SELECT identity,anchor_at,attempts,checked_at,next_at,state FROM outreach_inactivity_checks ORDER BY identity")]


def audit_identities(database):
    return sorted({row[0] for row in database.execute("SELECT identity FROM outreach_inactivity_audit")})


class InactivityIdentityTests(unittest.TestCase):
    def setUp(self):
        self.database, self.root = self.enterContext(manual.fixture())
        self.runtime = inactivity_closure

    # ------------------------------------------------------------------
    # 1. Write and read agree; the check converges.
    # ------------------------------------------------------------------
    def test_write_and_read_agree_so_attempts_advance_and_cap_is_reachable(self):
        stuck_recipient(self.database, STUCK_ONE)
        canonical = orchestrator.profile_key(STUCK_ONE)
        self.assertIn(research_workflow.profile_key(STUCK_ONE), self.runtime.candidates(self.database))

        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        progression = []
        for offset in range(5):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
            progression.append(rows(self.database))

        # Exactly one durable row, keyed in the same space the SELECT reads.
        for snapshot in progression:
            self.assertEqual(len(snapshot), 1, snapshot)
            self.assertEqual(snapshot[0][0], canonical, snapshot)
        self.assertEqual([snapshot[0][2] for snapshot in progression], [1, 2, 3, 3, 3], progression)
        self.assertEqual([snapshot[0][5] for snapshot in progression],
                         ["RETRY_WAIT", "RETRY_WAIT", "EXHAUSTED", "EXHAUSTED", "EXHAUSTED"], progression)
        # Exhausted re-arms at the next 27-day boundary (the recipient is dated and due).
        self.assertEqual(engagement.timestamp(progression[-1][0][4]), NOW + dt.timedelta(days=27), progression[-1])
        # The three-attempt cap actually engages: no fourth or fifth provider read.
        self.assertEqual(calls, [canonical] * 3, calls)
        self.assertEqual(audit_identities(self.database), [canonical])
        # Not closed on failed reads; a DUE-but-unresolved recipient stays held.
        self.assertIsNone(engagement.closure_status(self.database, STUCK_ONE))
        self.assertEqual(["INACTIVITY_CLOSURE_REVIEW_REQUIRED"],
                         self.runtime.send_gates(self.database, CONFIG, STUCK_ONE, NOW))

    def test_retry_backoff_engages_between_attempts(self):
        stuck_recipient(self.database, STUCK_ONE)
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(len(calls), 1, calls)
        stored = rows(self.database)[0]
        self.assertEqual(engagement.timestamp(stored[4]), NOW + dt.timedelta(minutes=15), stored)

        result = self.runtime.cycle(self.database, CONFIG, Forbidden(),
                                    NOW + dt.timedelta(minutes=5), refresh=refresh)
        self.assertEqual(len(calls), 1, calls)
        waiting = [item for item in result["items"] if item["state"] == "RETRY_WAIT"]
        self.assertEqual(len(waiting), 1, result)
        self.assertEqual(waiting[0]["blocker"], "INACTIVITY_FRESH_ACTIVITY_REVIEW_REQUIRED", waiting)

        self.runtime.cycle(self.database, CONFIG, Forbidden(),
                           NOW + dt.timedelta(minutes=16), refresh=refresh)
        self.assertEqual(len(calls), 2, calls)
        stored = rows(self.database)[0]
        self.assertEqual(stored[2], 2, stored)
        self.assertEqual(engagement.timestamp(stored[4]),
                         NOW + dt.timedelta(minutes=16) + dt.timedelta(hours=24), stored)

    # ------------------------------------------------------------------
    # 2. The two per-cycle slots are no longer permanently occupied.
    # ------------------------------------------------------------------
    def test_stuck_recipients_release_the_two_per_cycle_slots(self):
        stuck_recipient(self.database, STUCK_ONE)
        stuck_recipient(self.database, STUCK_TWO)
        due_recipient(self.database, HEALTHY)
        healthy_key = orchestrator.profile_key(HEALTHY)

        calls = []

        def refresh(profile, now):
            calls.append(profile)
            if profile == healthy_key:
                return manual.complete_refresh()
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        # The two unknown-history recipients sort first on the very first cycle.
        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(sorted(calls), [orchestrator.profile_key(STUCK_ONE),
                                         orchestrator.profile_key(STUCK_TWO)], calls)
        self.assertIsNone(engagement.closure_status(self.database, HEALTHY))

        # Because their checked_at is now recorded, they no longer sort to "".
        for offset in range(1, 4):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        self.assertIn(healthy_key, calls, calls)
        closed = engagement.closure_status(self.database, HEALTHY)
        self.assertIsNotNone(closed, rows(self.database))
        self.assertEqual(closed["state"], "CLOSED", closed)
        self.assertEqual(closed["source"], "inactivity-policy-27-days", closed)
        # And the stuck pair terminated on the cap instead of looping forever.
        stuck = [row for row in rows(self.database) if row[0] != healthy_key]
        self.assertEqual([row[5] for row in stuck], ["EXHAUSTED", "EXHAUSTED"], stuck)
        self.assertEqual(calls.count(orchestrator.profile_key(STUCK_ONE)), 3, calls)
        self.assertEqual(calls.count(orchestrator.profile_key(STUCK_TWO)), 3, calls)

    def test_reachable_closure_is_bounded_not_unbounded_rechecking(self):
        stuck_recipient(self.database, STUCK_ONE)
        stuck_recipient(self.database, STUCK_TWO)
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        for offset in range(20):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        self.assertEqual(len(calls), 6, calls)

    # ------------------------------------------------------------------
    # 3. Negative: distinct recipients never collide.
    # ------------------------------------------------------------------
    def test_confusable_identities_produce_distinct_canonical_keys(self):
        distinct = ["https://www.linkedin.com/in/kmorales",
                    "https://www.linkedin.com/in/kmorales-2",
                    "https://www.linkedin.com/in/kmorales-santa-cruz",
                    "https://www.linkedin.com/in/lena-voss",
                    "https://www.linkedin.com/in/lena-voss-100000001",
                    "https://www.linkedin.com/in/lena-voss-10000000"]
        keys = [self.runtime.key(value) for value in distinct]
        self.assertEqual(len(set(keys)), len(distinct), keys)
        for value in distinct:
            alias_set = set(engagement.identity_keys(self.database, value))
            others = {self.runtime.key(other) for other in distinct if other != value}
            self.assertFalse(alias_set & others, (value, alias_set, others))

    def test_two_distinct_recipients_get_separate_rows_and_separate_closures(self):
        first = "https://www.linkedin.com/in/lena-voss"
        second = "https://www.linkedin.com/in/lena-voss-100000001"
        due_recipient(self.database, first, source_id="first-initial")
        stuck_recipient(self.database, second)
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            if profile == orchestrator.profile_key(first):
                return manual.complete_refresh()
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(sorted(calls), sorted([orchestrator.profile_key(first),
                                                orchestrator.profile_key(second)]), calls)
        stored = rows(self.database)
        self.assertEqual([row[0] for row in stored],
                         sorted([orchestrator.profile_key(first), orchestrator.profile_key(second)]), stored)
        # Closing one must not close, suppress or key over the other.
        self.assertEqual(self.runtime.plan(self.database, first, NOW)["state"], "CLOSED")
        self.assertIsNone(engagement.closure_status(self.database, second))
        self.assertEqual(self.runtime.plan(self.database, second, NOW)["state"], "DUE")
        closures = {row[0] for row in self.database.execute("SELECT identity FROM engagement_closures")}
        self.assertEqual(closures, {orchestrator.profile_key(first)}, closures)

    def test_similar_recipients_never_share_an_audit_or_check_row(self):
        for profile in ("https://www.linkedin.com/in/kmorales",
                        "https://www.linkedin.com/in/kmorales-2",
                        "https://www.linkedin.com/in/kmorales-santa-cruz"):
            stuck_recipient(self.database, profile)

        def refresh(profile, now):
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        for offset in range(12):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        stored = rows(self.database)
        self.assertEqual(len(stored), 3, stored)
        self.assertEqual(len({row[0] for row in stored}), 3, stored)
        self.assertEqual(len(audit_identities(self.database)), 3)

    # ------------------------------------------------------------------
    # 4. Positive: case / URL-form / alias variants of ONE recipient match.
    # ------------------------------------------------------------------
    def test_case_and_url_form_variants_resolve_to_one_recipient(self):
        canonical = "linkedin.com/in/variant-case"
        spellings = ["https://www.linkedin.com/in/Variant-Case",
                     "https://uk.linkedin.com/in/variant-case/",
                     "http://linkedin.com/in/VARIANT-CASE",
                     "https://www.linkedin.com/in/variant-case"]
        raw_contact(self.database, spellings[0], historical=0)
        for index, spelling in enumerate(spellings[1:]):
            with self.database:
                self.database.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                      ("name:variant-" + str(index), "Variant Case", "Synthetic Company",
                                       spelling, "synthetic prior outreach"))
        manual.record(self.database, "initial_sent", START, profile=spellings[0], source_id="variant-initial")

        found = self.runtime.candidates(self.database)
        self.assertGreaterEqual(len([value for value in found if "variant" in value.lower()]), 3, found)

        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return manual.complete_refresh()

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(calls, [canonical], calls)
        stored = [row for row in rows(self.database) if "variant" in row[0]]
        self.assertEqual([row[0] for row in stored], [canonical], stored)
        for spelling in spellings + [canonical, "linkedin.com/in/Variant-Case"]:
            self.assertEqual(self.runtime.plan(self.database, spelling, NOW)["state"], "CLOSED", spelling)

    def test_percent_encoded_and_query_variants_match_the_same_recipient(self):
        profile = "https://www.linkedin.com/in/remy-ger%C3%B3nimo-vale-moraes-0000a1b2"
        canonical = orchestrator.profile_key(profile)
        raw_contact(self.database, profile, historical=0)
        raw_history(self.database, "https://br.linkedin.com/in/remy-ger%C3%B3nimo-vale-moraes-0000a1b2")
        manual.record(self.database, "initial_sent", START, profile=profile, source_id="encoded-initial")
        calls = []

        def refresh(target, now):
            calls.append(target)
            return manual.complete_refresh()

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(calls, [canonical], calls)
        self.assertEqual([row[0] for row in rows(self.database)], [canonical])

    def test_verified_alias_variants_share_one_check_row_and_one_closure(self):
        primary = "https://www.linkedin.com/in/alias-primary"
        alias = "https://www.linkedin.com/in/alias-secondary"
        raw_contact(self.database, primary, historical=0)
        research_workflow.register_alias(self.database, "contact", alias, primary,
                                         {"source": "synthetic verified alias"})
        raw_history(self.database, alias)
        manual.record(self.database, "initial_sent", START, profile=primary, source_id="alias-initial")
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return manual.complete_refresh()

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertEqual(len(calls), 1, calls)
        stored = [row for row in rows(self.database) if "alias-" in row[0]]
        self.assertEqual(len(stored), 1, stored)
        for profile in (primary, alias):
            self.assertEqual(self.runtime.plan(self.database, profile, NOW)["state"], "CLOSED", profile)

    def test_alias_registered_after_first_check_does_not_restart_the_cap(self):
        primary = "https://www.linkedin.com/in/late-alias-primary"
        alias = "https://www.linkedin.com/in/aaa-late-alias-secondary"
        stuck_recipient(self.database, primary)
        calls = []

        def refresh(profile, now):
            calls.append(profile)
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        research_workflow.register_alias(self.database, "contact", alias, primary,
                                         {"source": "synthetic verified alias"})
        raw_history(self.database, alias)
        for offset in range(1, 6):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        stored = rows(self.database)
        self.assertEqual(len(stored), 1, stored)
        self.assertEqual(stored[0][2], 3, stored)
        self.assertEqual(stored[0][5], "EXHAUSTED", stored)
        self.assertEqual(len(calls), 3, calls)

    # ------------------------------------------------------------------
    # 5. Unknown / incomplete history stays an explicit review hold.
    # ------------------------------------------------------------------
    def test_unknown_history_recipient_is_held_not_closed(self):
        unknown_history_recipient(self.database, STUCK_ONE)
        planned = self.runtime.plan(self.database, STUCK_ONE, NOW)
        self.assertEqual(planned["state"], "UNKNOWN", planned)
        self.assertEqual(planned["anchor_at"], "UNKNOWN", planned)
        self.assertEqual(planned["blocker"], "PRIOR_OUTREACH_TIMESTAMP_UNVERIFIED", planned)

        def refresh(profile, now):
            raise AssertionError("An undated recipient has no due date and is never read (owner, 2026-09-30)")

        for offset in range(4):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
            self.assertIsNone(engagement.closure_status(self.database, STUCK_ONE))
        self.assertEqual(self.runtime.plan(self.database, STUCK_ONE, NOW)["state"], "UNKNOWN")
        # UNKNOWN is still reported and still not closed; it simply no longer gates.
        self.assertEqual([], self.runtime.send_gates(self.database, CONFIG, STUCK_ONE, NOW))
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM engagement_closures").fetchone()[0], 0)
        self.assertEqual(rows(self.database), [])

    def test_incomplete_provider_read_never_closes_a_due_recipient(self):
        due_recipient(self.database, HEALTHY)

        def refresh(profile, now):
            return {"state": "UNKNOWN", "blocker": "CURRENT_INBOX_OR_ACCEPTANCE_COVERAGE_UNKNOWN"}

        for offset in range(4):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        self.assertIsNone(engagement.closure_status(self.database, HEALTHY))
        self.assertIn("INACTIVITY_CLOSURE_REVIEW_REQUIRED",
                      self.runtime.send_gates(self.database, CONFIG, HEALTHY, NOW))

    def test_fresh_inbound_from_a_raw_form_recipient_reconciles_before_closure(self):
        due_recipient(self.database, HEALTHY)
        fresh = NOW - dt.timedelta(days=1)

        def refresh(profile, now):
            return manual.complete_refresh([manual.event("inbound", fresh, profile=HEALTHY,
                                                         source_id="fresh-inbound")])

        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW, refresh=refresh)
        self.assertIsNone(engagement.closure_status(self.database, HEALTHY))
        planned = self.runtime.plan(self.database, HEALTHY, NOW)
        self.assertEqual(planned["state"], "ACTIVE", planned)
        self.assertEqual(engagement.timestamp(planned["anchor_at"]), fresh, planned)

    # ------------------------------------------------------------------
    # 6. A closed recipient is never reopened.
    # ------------------------------------------------------------------
    def test_closed_recipient_is_never_reopened_by_any_spelling(self):
        profile = "https://www.linkedin.com/in/already-closed"
        raw_contact(self.database, profile, historical=0)
        raw_history(self.database, "https://uk.linkedin.com/in/Already-Closed/")
        manual.record(self.database, "initial_sent", START, profile=profile, source_id="closed-initial")
        closed = engagement.close_outreach(self.database, profile, "Synthetic permanent closure", NOW)
        late = NOW + dt.timedelta(days=60)
        for kind in ("accepted", "inbound", "initial_sent"):
            manual.record(self.database, kind, late, profile=profile,
                          source_id="late-" + kind, observed=late)

        def refresh(target, now):
            raise AssertionError("A permanently closed recipient was selected for refresh")

        result = self.runtime.cycle(self.database, CONFIG, Forbidden(), late, refresh=refresh)
        self.assertEqual(result["closed"], 0, result)
        self.assertEqual(engagement.closure_status(self.database, profile), closed)
        for spelling in (profile, "linkedin.com/in/already-closed",
                         "https://uk.linkedin.com/in/Already-Closed/",
                         "http://linkedin.com/in/ALREADY-CLOSED"):
            self.assertEqual(self.runtime.plan(self.database, spelling, late)["state"], "CLOSED", spelling)
            self.assertEqual(self.runtime.send_gates(self.database, CONFIG, spelling, late), ["OUTREACH_CLOSED"])
        with contextlib.closing(orchestrator.connect(self.root)) as restarted:
            self.assertEqual(engagement.closure_status(restarted, profile), closed)

    def test_closure_written_by_the_cycle_survives_restart_under_every_spelling(self):
        profile = "https://www.linkedin.com/in/closed-by-cycle"
        due_recipient(self.database, profile, source_id="cycle-initial")
        self.runtime.cycle(self.database, CONFIG, Forbidden(), NOW,
                           refresh=lambda target, now: manual.complete_refresh())
        closed = engagement.closure_status(self.database, profile)
        self.assertIsNotNone(closed)
        with contextlib.closing(orchestrator.connect(self.root)) as restarted:
            for spelling in (profile, "linkedin.com/in/closed-by-cycle",
                             "https://uk.linkedin.com/in/Closed-By-Cycle/"):
                self.assertEqual(self.runtime.plan(restarted, spelling, NOW + dt.timedelta(days=400))["state"],
                                 "CLOSED", spelling)
            self.runtime.cycle(restarted, CONFIG, Forbidden(), NOW + dt.timedelta(days=400),
                               refresh=lambda target, now: (_ for _ in ()).throw(
                                   AssertionError("Closed recipient refreshed after restart")))
            self.assertEqual(engagement.closure_status(restarted, profile), closed)

    # ------------------------------------------------------------------
    # 7. Legacy rows written under the unmatched key are never rewritten away.
    # ------------------------------------------------------------------
    def test_legacy_rows_are_preserved_and_the_fix_converges_without_them(self):
        stuck_recipient(self.database, STUCK_ONE)
        legacy = research_workflow.profile_key(STUCK_ONE)
        canonical = orchestrator.profile_key(STUCK_ONE)
        self.runtime.initialize(self.database)
        with self.database:
            self.database.execute("INSERT INTO outreach_inactivity_checks VALUES (?,?,?,?,?,?,?)",
                                  (legacy, "UNKNOWN", 1, "2026-09-17T08:53:47.777773+00:00",
                                   "2026-09-17T09:08:49.777776+00:00", "RETRY_WAIT", "{}"))
            self.database.execute("INSERT INTO outreach_inactivity_audit VALUES (?,?,?)",
                                  (legacy, "2026-09-17T08:56:44.777779+00:00", "{}"))

        def refresh(profile, now):
            return {"state": "UNKNOWN", "blocker": "synthetic incomplete provider read"}

        for offset in range(5):
            self.runtime.cycle(self.database, CONFIG, Forbidden(),
                               NOW + dt.timedelta(days=offset), refresh=refresh)
        stored = dict((row[0], row) for row in rows(self.database))
        self.assertIn(legacy, stored)
        self.assertEqual(stored[legacy][2], 1, stored[legacy])
        self.assertEqual(stored[legacy][3], "2026-09-17T08:53:47.777773+00:00", stored[legacy])
        self.assertIn(canonical, stored)
        self.assertEqual(stored[canonical][2], 3, stored[canonical])
        self.assertEqual(stored[canonical][5], "EXHAUSTED", stored[canonical])
        self.assertIn(legacy, audit_identities(self.database))


if __name__ == "__main__":
    unittest.main()
