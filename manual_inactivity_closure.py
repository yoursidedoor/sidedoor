"""Isolated, synthetic acceptance checks for permanent 27-calendar-day closure."""

import contextlib
import datetime as dt
import importlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import engagement
import manual_recipient_closure
import orchestrator
import outreach
import research_workflow


ROOT = Path(__file__).resolve().parent
NOW = dt.datetime(2026, 9, 17, 19, tzinfo=dt.timezone.utc)
PROFILE = "https://www.linkedin.com/in/synthetic-inactivity/"
ALIAS = "https://www.linkedin.com/in/synthetic-inactivity-alias/"
START = NOW - dt.timedelta(days=27)
CONFIG = {"outreach_inactivity_days": 27, "sender_id": 900001}


class ForbiddenClient:
    def call(self, *args, **kwargs):
        raise AssertionError("Injected-refresh checks must not access a provider")


def complete_refresh(events=()):
    return {"state": "COMPLETE", "events": list(events),
            "evidence": "synthetic complete final recipient refresh",
            "raw": {"fixture": True, "complete": True}}


@contextlib.contextmanager
def offline():
    with contextlib.ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen", "urllib.request.urlopen"):
            stack.enter_context(patch(target, side_effect=AssertionError("External execution forbidden")))
        yield


@contextlib.contextmanager
def fixture():
    with offline(), tempfile.TemporaryDirectory(prefix="inactivity-manual-", dir=ROOT) as directory:
        root = Path(directory)
        (root / "state").mkdir()
        with contextlib.closing(orchestrator.connect(root)) as database:
            engagement.initialize(database)
            outreach.initialize(database)
            outreach.initialize_actions(database)
            research_workflow.initialize(database)
            with database:
                database.execute("DELETE FROM outreach_attempts WHERE identity='linkedin.com/in/jrivera'")
            managed(database, PROFILE)
            yield database, root


def managed(database, profile):
    with database:
        database.execute("INSERT OR IGNORE INTO rw_contacts VALUES (?,?,?,?,?)",
                         ("synthetic company", orchestrator.profile_key(profile),
                          "Synthetic Recipient", 0, "synthetic-managed-plan"))


def event(kind, occurred, *, profile=PROFILE, source_id=None, observed=None, **extra):
    return {"source": "synthetic-inactivity", "source_id": source_id or kind,
            "profile": profile, "kind": kind, "occurred_at": occurred.isoformat(),
            "observed_at": (observed or occurred).isoformat(), "evidence": "synthetic independent observation",
            "verified": True, "raw": {"fixture": True}, **extra}


def record(database, kind, occurred, **extra):
    return engagement.record_event(database, event(kind, occurred, **extra))


def expect_plan(runtime, database, state, anchor=None, *, profile=PROFILE, now=NOW):
    result = runtime.plan(database, profile, now)
    assert result["state"] == state, result
    if anchor is not None:
        assert {"anchor_at", "due_at"}.issubset(result), result
        assert engagement.timestamp(result["anchor_at"]) == anchor, result
        assert engagement.timestamp(result["due_at"]) == anchor + dt.timedelta(days=27), result
    elif state == "NOT_STARTED":
        assert result.get("anchor_at") is None and result.get("due_at") is None, result
    return result


def check_unstarted(runtime, database, root):
    expect_plan(runtime, database, "NOT_STARTED")
    record(database, "accepted", START)
    record(database, "followup_sent", START)
    expect_plan(runtime, database, "NOT_STARTED")


def check_exact_boundary(runtime, database, root):
    record(database, "initial_sent", START)
    expect_plan(runtime, database, "ACTIVE", START, now=NOW - dt.timedelta(microseconds=1))
    expect_plan(runtime, database, "DUE", START)
    expect_plan(runtime, database, "DUE", START, now=NOW + dt.timedelta(microseconds=1))
    expect_plan(runtime, database, "ACTIVE", START, now=START + dt.timedelta(days=14))
    assert engagement.closure_status(database, PROFILE) is None


def check_acceptance_reset(runtime, database, root):
    record(database, "initial_sent", START)
    accepted = START + dt.timedelta(days=19)
    record(database, "accepted", accepted)
    expect_plan(runtime, database, "ACTIVE", accepted)
    expect_plan(runtime, database, "DUE", accepted, now=accepted + dt.timedelta(days=27))


def check_inbound_reset(runtime, database, root):
    record(database, "initial_sent", START)
    inbound = START + dt.timedelta(days=22)
    record(database, "inbound", inbound)
    record(database, "accepted", START + dt.timedelta(days=8))
    expect_plan(runtime, database, "ACTIVE", inbound)
    expect_plan(runtime, database, "DUE", inbound, now=inbound + dt.timedelta(days=27))


def check_outbound_does_not_reset(runtime, database, root):
    record(database, "initial_sent", START)
    record(database, "followup_sent", NOW - dt.timedelta(days=1))
    record(database, "outbound_observed", NOW)
    expect_plan(runtime, database, "DUE", START)


def check_verified_outbound_starts(runtime, database, root):
    record(database, "outbound_observed", START)
    record(database, "outbound_observed", NOW, source_id="later-outbound")
    expect_plan(runtime, database, "DUE", START)


def check_earliest_verified_outbound(runtime, database, root):
    record(database, "initial_sent", NOW - dt.timedelta(days=2))
    record(database, "outbound_observed", START)
    expect_plan(runtime, database, "DUE", START)


def check_observation_is_not_anchor(runtime, database, root):
    record(database, "initial_sent", START, observed=NOW)
    expect_plan(runtime, database, "DUE", START)
    inbound = NOW - dt.timedelta(days=5)
    observed_event = event("inbound", inbound)
    engagement.record_event(database, observed_event)
    engagement.record_event(database, dict(observed_event, observed_at=(NOW + dt.timedelta(days=3)).isoformat()))
    expect_plan(runtime, database, "ACTIVE", inbound, now=NOW + dt.timedelta(days=3))


def check_calendar_and_timezone(runtime, database, root):
    start = dt.datetime(2026, 8, 21, 12, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
    record(database, "initial_sent", start)
    expect_plan(runtime, database, "DUE", START)
    expect_plan(runtime, database, "ACTIVE", START, now=NOW - dt.timedelta(seconds=1))


def check_aliases_and_restart(runtime, database, root):
    research_workflow.register_alias(database, "contact", ALIAS, PROFILE, {"source": "synthetic verified alias"})
    record(database, "initial_sent", START)
    accepted = NOW - dt.timedelta(days=2)
    record(database, "accepted", accepted, profile=ALIAS)
    for profile in (PROFILE, ALIAS, "https://uk.linkedin.com/in/SYNTHETIC-INACTIVITY/?tracking=fixture"):
        expect_plan(runtime, database, "ACTIVE", accepted, profile=profile)
    with contextlib.closing(orchestrator.connect(root)) as restarted:
        expect_plan(runtime, restarted, "ACTIVE", accepted, profile=ALIAS)


def check_closed_never_reopens(runtime, database, root):
    record(database, "initial_sent", START)
    closed = engagement.close_outreach(database, PROFILE, "Synthetic permanent closure", NOW)
    late = NOW + dt.timedelta(days=40)
    for kind in ("accepted", "inbound", "initial_sent"):
        record(database, kind, late, source_id="late-" + kind, observed=late)
    expect_plan(runtime, database, "CLOSED", now=late)
    assert engagement.closure_status(database, PROFILE) == closed
    research_workflow.register_alias(database, "contact", ALIAS, PROFILE, {"source": "synthetic later verified alias"})
    with database:
        database.execute("DELETE FROM rw_aliases")
    with contextlib.closing(orchestrator.connect(root)) as restarted:
        expect_plan(runtime, restarted, "CLOSED", profile=ALIAS, now=late + dt.timedelta(days=365))
        assert engagement.stop_state(restarted, ALIAS) == "OUTREACH_CLOSED"


def check_unverified_delivery_rejected(runtime, database, root):
    try:
        record(database, "initial_sent", START, verified=False)
    except ValueError:
        pass
    else:
        raise AssertionError("Unverified delivery was accepted as an initial_sent event")
    expect_plan(runtime, database, "NOT_STARTED")


def check_inbound_only_anchor(runtime, database, root):
    record(database, "inbound", START)
    expect_plan(runtime, database, "DUE", START)
    recent = NOW - dt.timedelta(days=3)
    record(database, "inbound", recent, source_id="later-inbound")
    expect_plan(runtime, database, "ACTIVE", recent)


def check_unverified_events_do_not_anchor_or_reset(runtime, database, root):
    record(database, "outbound_observed", START, verified=False)
    record(database, "inbound", NOW, verified=False)
    expect_plan(runtime, database, "NOT_STARTED")
    record(database, "initial_sent", START)
    expect_plan(runtime, database, "DUE", START)


def check_unknown_activity_does_not_hold_send(runtime, database, root):
    """Owner, 2026-09-23: "A 'dont know' also shouldnt be evidence to block an outreach."

    This asserted the opposite. `send_gates` is the FIRST gate on every message and `plan`
    returns UNKNOWN whenever the prior-outreach timestamp cannot be reconstructed - true for all
    12 tracked contacts, every one anchor_at='UNKNOWN' - so not knowing when someone was last
    contacted stopped every send. The plan still reports UNKNOWN, and the conversation is still
    not closed; what changed is that UNKNOWN no longer gates. A real DUE date still does
    (`check_due_send_gate`), and an explicitly CLOSED conversation still does.
    """
    record(database, "initial_sent", START, observed=NOW + dt.timedelta(hours=1))
    expect_plan(runtime, database, "UNKNOWN")
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW) == []
    assert engagement.closure_status(database, PROFILE) is None


def check_due_send_gate(runtime, database, root):
    record(database, "initial_sent", START)
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW - dt.timedelta(microseconds=1)) == []
    assert "INACTIVITY_CLOSURE_REVIEW_REQUIRED" in runtime.send_gates(database, CONFIG, PROFILE, NOW)
    assert runtime.send_gates(database, {}, PROFILE, NOW) == []


def check_canonical_identity(runtime, database, root):
    identity = orchestrator.profile_key(PROFILE)
    expect_plan(runtime, database, "NOT_STARTED", profile=identity)
    record(database, "initial_sent", START)
    expect_plan(runtime, database, "DUE", START, profile=identity)
    assert "INACTIVITY_CLOSURE_REVIEW_REQUIRED" in runtime.send_gates(database, CONFIG, identity, NOW)


def check_existing_outreach_send_gates(runtime, database, root):
    record(database, "initial_sent", START)
    config = dict(manual_recipient_closure.CONFIG, **CONFIG)
    for purpose in ("INITIAL", "FIRST_MESSAGE", "FOLLOWUP", "URGENT", "REPLY", "PREPARE_CAMPAIGN"):
        candidate = manual_recipient_closure.action("due-" + purpose, PROFILE, purpose)
        assert "INACTIVITY_CLOSURE_REVIEW_REQUIRED" in outreach.action_gates(database, config, candidate, {}, NOW), purpose
    candidate = dict(manual_recipient_closure.action("due-invitation", PROFILE), campaign_id=900123)
    pipeline = {"companies": [], "rules": {"max_unique_contacts_per_company": 3}}
    assert "INACTIVITY_CLOSURE_REVIEW_REQUIRED" in outreach.local_gates(database, config, pipeline, candidate, NOW)


PLAN_CHECKS = (
    check_unstarted,
    check_exact_boundary,
    check_acceptance_reset,
    check_inbound_reset,
    check_outbound_does_not_reset,
    check_verified_outbound_starts,
    check_earliest_verified_outbound,
    check_observation_is_not_anchor,
    check_calendar_and_timezone,
    check_aliases_and_restart,
    check_closed_never_reopens,
    check_unverified_delivery_rejected,
    check_inbound_only_anchor,
    check_unverified_events_do_not_anchor_or_reset,
    check_unknown_activity_does_not_hold_send,
    check_due_send_gate,
    check_canonical_identity,
    check_existing_outreach_send_gates,
)


def check_cycle_closes_only_after_refresh(runtime, database, root):
    record(database, "initial_sent", START)
    calls = []

    def refresh(profile, now):
        assert engagement.closure_status(database, profile) is None
        assert engagement.timestamp(now) == NOW
        calls.append(profile)
        return complete_refresh()

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    closed = engagement.closure_status(database, PROFILE)
    assert closed is not None, "Due recipient did not close after complete refresh"
    assert engagement.timestamp(closed["closed_at"]) == NOW, closed
    assert closed["reason"] and closed["source"], closed
    audit = json.loads(database.execute("SELECT payload FROM outreach_inactivity_audit").fetchone()[0])
    assert audit["observation"] == complete_refresh(), audit
    assert audit["plan"]["state"] == "DUE", audit
    assert engagement.timestamp(audit["plan"]["anchor_at"]) == START, audit
    assert len(calls) == 1, calls
    expect_plan(runtime, database, "CLOSED")
    with contextlib.closing(orchestrator.connect(root)) as restarted:
        runtime.cycle(restarted, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=60), refresh=refresh)
        assert engagement.closure_status(restarted, PROFILE) == closed
    assert len(calls) == 1, "Closed recipient was refreshed again"


def check_cycle_replans_fresh_inbound(runtime, database, root):
    record(database, "initial_sent", START)
    inbound = event("inbound", NOW - dt.timedelta(hours=1), source_id="fresh-inbound")
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh([inbound])

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert len(calls) == 1, calls
    assert engagement.closure_status(database, PROFILE) is None
    expect_plan(runtime, database, "ACTIVE", NOW - dt.timedelta(hours=1))
    stored = engagement.events_for(database, PROFILE)
    assert any(item["source_id"] == inbound["source_id"] for item in stored), stored


def check_cycle_replans_fresh_acceptance(runtime, database, root):
    record(database, "initial_sent", START)
    accepted = NOW - dt.timedelta(days=1)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: complete_refresh([event("accepted", accepted)]))
    assert engagement.closure_status(database, PROFILE) is None
    expect_plan(runtime, database, "ACTIVE", accepted)


def check_cycle_fresh_outbound_does_not_delay(runtime, database, root):
    record(database, "initial_sent", START)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: complete_refresh([event("outbound_observed", NOW)]))
    expect_plan(runtime, database, "CLOSED")


def check_cycle_default_disabled(runtime, database, root):
    record(database, "initial_sent", START)

    def refresh(profile, now):
        raise AssertionError("Default-disabled policy called refresh")

    runtime.cycle(database, {}, ForbiddenClient(), NOW, refresh=refresh)
    assert engagement.closure_status(database, PROFILE) is None


def check_cycle_active_no_refresh(runtime, database, root):
    record(database, "initial_sent", START + dt.timedelta(microseconds=1))

    def refresh(profile, now):
        raise AssertionError("Recipient refreshed before the exact 27-day boundary")

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert engagement.closure_status(database, PROFILE) is None


def check_cycle_failure_bound_survives_restart(runtime, database, root):
    record(database, "initial_sent", START)
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return {"state": "UNKNOWN", "blocker": "synthetic incomplete history"}

    for offset in range(5):
        with contextlib.closing(orchestrator.connect(root)) as restarted:
            runtime.cycle(restarted, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=offset), refresh=refresh)
            assert engagement.closure_status(restarted, PROFILE) is None
    assert len(calls) == 3, {"refresh_attempts": len(calls), "expected": 3}
    audit = [json.loads(row[0]) for row in database.execute(
        "SELECT payload FROM outreach_inactivity_audit ORDER BY checked_at")]
    failures = [entry for entry in audit if "attempts" in entry]
    observations = [entry for entry in audit if "observation" in entry]
    assert [entry["attempts"] for entry in failures] == [1, 2, 3], audit
    assert all(entry["blocker"] == "synthetic incomplete history" for entry in failures), audit
    assert len(observations) == 3, audit
    assert all(entry["observation"] == {"state": "UNKNOWN", "blocker": "synthetic incomplete history"}
               for entry in observations), audit
    assert database.execute("SELECT attempts,state,next_at FROM outreach_inactivity_checks").fetchone()[:] == (3, "EXHAUSTED", (NOW + dt.timedelta(days=27)).isoformat())
    # The send-gate assertion here is gone (owner, 2026-09-23): UNKNOWN no longer gates, and
    # what this case exists to prove - the three-attempt bound and that both audits survive a
    # restart - is asserted above and is unchanged.


def check_cycle_failures_are_isolated(runtime, database, root):
    healthy = "https://www.linkedin.com/in/synthetic-inactivity-healthy/"
    record(database, "initial_sent", START)
    record(database, "initial_sent", START, profile=healthy, source_id="healthy-initial")
    managed(database, healthy)
    calls = []

    def refresh(profile, now):
        identity = orchestrator.profile_key(profile if "://" in profile else "https://" + profile)
        calls.append(identity)
        if identity == orchestrator.profile_key(PROFILE):
            raise TimeoutError("synthetic refresh timeout")
        return complete_refresh()

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert set(calls) == {orchestrator.profile_key(PROFILE), orchestrator.profile_key(healthy)}, calls
    assert engagement.closure_status(database, PROFILE) is None
    expect_plan(runtime, database, "CLOSED", profile=healthy)


def check_cycle_preserves_delivery_claims(runtime, database, root):
    profile = manual_recipient_closure.PROFILE
    record(database, "initial_sent", START, profile=profile)
    outreach.enqueue_action(database, manual_recipient_closure.action("ready"))
    states = ("CONFIRMED_SENT", "CONFIRMED_NOT_SENT", "UNCERTAIN_NO_RETRY",
              "ACKNOWLEDGED_UNCONFIRMED", "DISPATCHING", "REQUEST_STARTED")
    for state in states:
        manual_recipient_closure.seed_claim(database, state, state)
    snapshot = manual_recipient_closure.delivery_snapshot(database)
    claims_before = [tuple(row) for row in database.execute(
        "SELECT * FROM outreach_actions WHERE action_id<>'ready' ORDER BY action_id")]
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: complete_refresh())
    expect_plan(runtime, database, "CLOSED", profile=profile)
    assert manual_recipient_closure.delivery_snapshot(database) == snapshot
    assert [tuple(row) for row in database.execute(
        "SELECT * FROM outreach_actions WHERE action_id<>'ready' ORDER BY action_id")] == claims_before
    assert database.execute("SELECT state FROM outreach_actions WHERE action_id='ready'").fetchone()[0] == "CLOSED"


def check_cycle_retry_schedule(runtime, database, root):
    record(database, "initial_sent", START)
    calls = []

    def refresh(profile, now):
        calls.append(now)
        return {"state": "UNKNOWN", "blocker": "synthetic unavailable history"}

    for minutes, expected in ((0, 1), (14, 1), (15, 2), (75, 2), (1454, 2), (1455, 3), (1900, 3)):
        runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(minutes=minutes), refresh=refresh)
        assert len(calls) == expected, {"minutes": minutes, "calls": calls}
    record(database, "outbound_observed", NOW, source_id="own-outbound-after-exhaustion")
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=2), refresh=refresh)
    assert len(calls) == 3, "Own outbound reset an exhausted refresh budget"
    assert engagement.closure_status(database, PROFILE) is None


def check_cycle_managed_inbound_only(runtime, database, root):
    record(database, "inbound", START)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: complete_refresh())
    expect_plan(runtime, database, "CLOSED")


def check_cycle_conversation_only_identity_is_candidate(runtime, database, root):
    # HeyReach inbox people with no outreach record are candidates too (owner, 2026-09-30).
    inbox = "https://www.linkedin.com/in/synthetic-inbox-only/"
    record(database, "inbound", START, profile=inbox, source_id="inbox-only")
    assert orchestrator.profile_key(inbox) in runtime.candidates(database)
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        # No campaign source: acceptance is unobservable, so the two-read rule applies.
        return dict(complete_refresh(), acceptance_observable=False)

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert calls == [orchestrator.profile_key(inbox)], calls
    expect_plan(runtime, database, "ACTIVE", profile=inbox)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=27), refresh=refresh)
    assert len(calls) == 2, calls
    expect_plan(runtime, database, "CLOSED", profile=inbox, now=NOW + dt.timedelta(days=27))


def check_cycle_provider_id_identity_is_unknown(runtime, database, root):
    # A HeyReach member id cannot be read by profile URL; without a verified alias it is
    # reported, never read and never closed.
    member = "https://www.linkedin.com/in/ACoAAsyntheticMemberId/"
    record(database, "inbound", START, profile=member, source_id="member-id-inbound")

    def refresh(profile, now):
        raise AssertionError("Provider-id identity was read")

    result = runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    item = next(item for item in result["items"] if item["identity"] == orchestrator.profile_key(member))
    assert item["state"] == "UNKNOWN" and item["blocker"] == "PROVIDER_ID_IDENTITY_UNMAPPED", item
    assert engagement.closure_status(database, member) is None


def check_cycle_unobservable_acceptance_needs_two_clean_reads(runtime, database, root):
    record(database, "initial_sent", START)
    calls = []

    def refresh(profile, now):
        calls.append(now)
        return dict(complete_refresh(), acceptance_observable=False)

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert engagement.closure_status(database, PROFILE) is None
    planned = expect_plan(runtime, database, "ACTIVE", now=NOW + dt.timedelta(days=1))
    assert engagement.timestamp(planned["due_at"]) == NOW + dt.timedelta(days=27), planned
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW + dt.timedelta(days=1)) == []
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=26), refresh=refresh)
    assert len(calls) == 1, calls
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(days=27), refresh=refresh)
    assert len(calls) == 2, calls
    expect_plan(runtime, database, "CLOSED", now=NOW + dt.timedelta(days=27))


def check_cycle_inbound_in_second_window_resets(runtime, database, root):
    record(database, "initial_sent", START)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: dict(complete_refresh(), acceptance_observable=False))
    inbound = NOW + dt.timedelta(days=10)
    record(database, "inbound", inbound, source_id="second-window-inbound")
    expect_plan(runtime, database, "ACTIVE", inbound, now=NOW + dt.timedelta(days=27))
    runtime.cycle(database, CONFIG, ForbiddenClient(), inbound + dt.timedelta(days=27),
                  refresh=lambda profile, now: dict(complete_refresh(), acceptance_observable=False))
    # A new anchor needs its own first clean read before a second one may close.
    assert engagement.closure_status(database, PROFILE) is None


def check_cycle_candidate_bound(runtime, database, root):
    profiles = [PROFILE] + ["https://www.linkedin.com/in/synthetic-limit-" + str(index) for index in range(4)]
    for index, profile in enumerate(profiles):
        managed(database, profile)
        record(database, "initial_sent", START, profile=profile, source_id="bounded-" + str(index))
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    for expected in (2, 4, 5):
        runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
        assert len(calls) == expected, calls
    assert len(set(calls)) == 5, calls


def check_cycle_alias_deduplication(runtime, database, root):
    managed(database, ALIAS)
    research_workflow.register_alias(database, "contact", ALIAS, PROFILE, {"source": "synthetic verified alias"})
    record(database, "initial_sent", START)
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert len(calls) == 1, calls
    for profile in (PROFILE, ALIAS):
        expect_plan(runtime, database, "CLOSED", profile=profile)


def check_cycle_refresh_atomic_on_invalid_recipient(runtime, database, root):
    record(database, "initial_sent", START)
    original = engagement.events_for(database, PROFILE)
    packet = [event("inbound", NOW, source_id="valid-fresh"),
              event("inbound", NOW, profile=ALIAS, source_id="wrong-recipient")]
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: complete_refresh(packet))
    assert engagement.events_for(database, PROFILE) == original
    assert engagement.events_for(database, ALIAS) == []
    assert engagement.closure_status(database, PROFILE) is None
    stored = database.execute("SELECT attempts,state,result FROM outreach_inactivity_checks").fetchone()
    assert stored[0] == 1 and stored[1] == "RETRY_WAIT", dict(stored)
    assert "ACTIVITY_RECIPIENT_MISMATCH" in stored[2], dict(stored)


def check_cycle_rejects_unauthorized_threshold(runtime, database, root):
    for value in (14, 28, 27.0, "27", True):
        try:
            runtime.cycle(database, {"outreach_inactivity_days": value}, ForbiddenClient(), NOW,
                          refresh=lambda profile, now: complete_refresh())
        except ValueError:
            continue
        raise AssertionError("Unauthorized inactivity setting was accepted: " + repr(value))


def check_cycle_capacity_wait_preserves_attempts(runtime, database, root):
    record(database, "initial_sent", START)
    calls = []
    reset = NOW + dt.timedelta(hours=5)

    def refresh(profile, now):
        calls.append(now)
        return {"state": "UNKNOWN", "capacity_wait": True,
                "next_at": reset.isoformat(), "blocker": "synthetic shared compute exhausted"}

    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    stored = database.execute("SELECT attempts,state,next_at FROM outreach_inactivity_checks").fetchone()
    assert stored[:] == (0, "BUDGET_WAIT", reset.isoformat()), tuple(stored)
    assert engagement.closure_status(database, PROFILE) is None
    with contextlib.closing(orchestrator.connect(root)) as restarted:
        runtime.cycle(restarted, CONFIG, ForbiddenClient(), reset - dt.timedelta(seconds=1), refresh=refresh)
        assert len(calls) == 1, calls
        runtime.cycle(restarted, CONFIG, ForbiddenClient(), reset,
                      refresh=lambda profile, now: complete_refresh())
        expect_plan(runtime, restarted, "CLOSED", now=reset)
        assert restarted.execute("SELECT attempts FROM outreach_inactivity_checks").fetchone()[0] == 1
        audit = [json.loads(row[0]) for row in restarted.execute("SELECT payload FROM outreach_inactivity_audit")]
        assert len(audit) == 2, audit
        assert audit[0]["observation"]["capacity_wait"] is True, audit


def check_cycle_unknown_coverage_retains_positive_engagement(runtime, database, root):
    record(database, "initial_sent", START)
    inbound = NOW - dt.timedelta(hours=1)
    observation = dict(complete_refresh([event("inbound", inbound)]),
                       state="UNKNOWN", blocker="synthetic incomplete other history")
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW,
                  refresh=lambda profile, now: observation)
    assert engagement.closure_status(database, PROFILE) is None
    expect_plan(runtime, database, "ACTIVE", inbound)
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW) == []
    assert database.execute("SELECT state FROM outreach_inactivity_checks").fetchone()[0] == "ACTIVE"
    audit = json.loads(database.execute("SELECT payload FROM outreach_inactivity_audit").fetchone()[0])
    assert audit["observation"] == observation, audit


def check_cycle_undated_history_remains_unknown(runtime, database, root):
    with database:
        database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                         (orchestrator.profile_key(PROFILE), "Synthetic Recipient", "Synthetic Company",
                          PROFILE, "synthetic prior outreach without verified date"))
    expect_plan(runtime, database, "UNKNOWN")
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    # Undated people have no due date and are not read (owner, 2026-09-30).
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert calls == [], calls
    expect_plan(runtime, database, "UNKNOWN")
    assert engagement.closure_status(database, PROFILE) is None
    # UNKNOWN no longer gates a send (owner, 2026-09-23).
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW) == []
    # Once delivery reconciliation dates the outreach, the next cycle reads and closes.
    record(database, "outbound_observed", START)
    runtime.cycle(database, CONFIG, ForbiddenClient(), NOW + dt.timedelta(minutes=15), refresh=refresh)
    assert len(calls) == 1, calls
    expect_plan(runtime, database, "CLOSED", now=NOW + dt.timedelta(minutes=15))


def check_orchestrator_service_integration(runtime, database, root):
    record(database, "initial_sent", START)
    (root / "reports").mkdir()
    actual_cycle = runtime.cycle
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    def injected_cycle(database, config, client):
        return actual_cycle(database, config, client, NOW, refresh=refresh)

    config = dict(CONFIG, scope="monitor_existing_pipeline")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orchestrator, "ROOT", root))
        stack.enter_context(patch.object(orchestrator, "HeyReach", return_value=ForbiddenClient()))
        stack.enter_context(patch.object(runtime, "cycle", side_effect=injected_cycle))
        stack.enter_context(patch.object(orchestrator.decision_transport, "cycle", return_value={"state": "DISABLED"}))
        stack.enter_context(patch.object(orchestrator.reporting, "daily", return_value={"state": "DISABLED"}))
        stack.enter_context(patch.object(orchestrator.notifications, "dispatch", return_value={"state": "DISABLED"}))
        result = orchestrator.service_cycle(root, config, database)
    assert result["inactivity_closure"]["closed"] == 1, result["inactivity_closure"]
    assert len(calls) == 1, calls
    expect_plan(runtime, database, "CLOSED")
    task = database.execute("SELECT status FROM workflow_tasks WHERE worker='inactivity-closure'").fetchone()
    assert task is not None and task[0] == "COMPLETE", tuple(task) if task else None
    saved = json.loads((root / "reports/service-status.json").read_text())
    assert saved["inactivity_closure"]["closed"] == 1, saved["inactivity_closure"]


def check_legacy_final_guard_preserves_claim(runtime, database, root):
    current = dt.datetime.now(dt.timezone.utc)
    record(database, "initial_sent", current - dt.timedelta(days=27, seconds=1))
    identity = orchestrator.profile_key(PROFILE)
    candidate = dict(manual_recipient_closure.action("legacy-review", PROFILE), campaign_id=999999)
    payload = json.dumps(candidate, sort_keys=True)
    with database:
        database.execute("INSERT INTO outreach_attempts VALUES (?,?,?,?,?)",
                         (identity, candidate["campaign_id"], "CLAIMED", current.isoformat(), None))
        database.execute("INSERT INTO outreach_launch_claims VALUES (?,?,?,?,?,?)",
                         (candidate["campaign_id"], identity, CONFIG["sender_id"], payload,
                          orchestrator.digest(payload.encode()), current.isoformat()))
    client = object.__new__(orchestrator.HeyReach)
    client.config = CONFIG
    before = {table: [tuple(row) for row in database.execute("SELECT * FROM " + table)]
              for table in ("outreach_attempts", "outreach_launch_claims")}
    # DUE only (owner, 2026-09-23). UNKNOWN was in this loop and no longer gates a send: not
    # knowing when someone was last contacted is absence of evidence, and it was the first gate
    # on every message with all 12 tracked contacts sitting at anchor_at='UNKNOWN'. A real DUE
    # date - the 27-day policy having actually elapsed - still blocks the legacy launch, which
    # is what this case proves, along with the claim surviving untouched.
    for state in ("DUE",):
        assert runtime.plan(database, PROFILE)["state"] == state
        with patch.object(client, "_request", side_effect=AssertionError("No provider request allowed")) as request:
            try:
                client.start_claimed_campaign(database, candidate["campaign_id"])
            except ValueError as error:
                assert "INACTIVITY_CLOSURE_REVIEW_REQUIRED" in str(error), str(error)
            else:
                raise AssertionError("Legacy campaign launch accepted a " + state + " recipient")
            request.assert_not_called()
        assert {table: [tuple(row) for row in database.execute("SELECT * FROM " + table)]
                for table in before} == before


def check_malformed_event_does_not_starve_candidates(runtime, database, root):
    healthy = "https://linkedin.com/in/synthetic-review-healthy"
    managed(database, healthy)
    record(database, "initial_sent", START)
    record(database, "initial_sent", START, profile=healthy, source_id="healthy-start")
    with database:
        database.execute("UPDATE engagement_events SET payload='invalid json' WHERE source_id='initial_sent'")
    expect_plan(runtime, database, "UNKNOWN")
    # UNKNOWN no longer gates a send (owner, 2026-09-23); the plan still reports it
    # and the conversation is still not closed, which is what these cases check.
    assert runtime.send_gates(database, CONFIG, PROFILE, NOW) == []
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    result = runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert result["closed"] == 1, result
    assert calls == [orchestrator.profile_key(healthy)], calls
    assert engagement.closure_status(database, PROFILE) is None
    expect_plan(runtime, database, "CLOSED", profile=healthy)
    assert database.execute("SELECT payload FROM engagement_events WHERE source_id='initial_sent'").fetchone()[0] == "invalid json"


def check_history_identity_without_profile_recovers(runtime, database, root):
    identity = orchestrator.profile_key(PROFILE)
    with database:
        database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                         (identity, "Synthetic Recipient", "Synthetic Company", None,
                          "synthetic historical evidence"))
    planned = expect_plan(runtime, database, "UNKNOWN")
    assert planned["anchor_at"] == "UNKNOWN", planned
    calls = []

    def refresh(profile, now):
        calls.append(profile)
        return complete_refresh()

    result = runtime.cycle(database, CONFIG, ForbiddenClient(), NOW, refresh=refresh)
    assert calls == [], calls
    assert any(item.get("blocker") == "PRIOR_OUTREACH_TIMESTAMP_UNVERIFIED" for item in result["items"]), result
    assert engagement.closure_status(database, PROFILE) is None
    assert database.execute("SELECT COUNT(*) FROM outreach_inactivity_checks").fetchone()[0] == 0
    expect_plan(runtime, database, "UNKNOWN")


CYCLE_CHECKS = (
    check_cycle_closes_only_after_refresh,
    check_cycle_replans_fresh_inbound,
    check_cycle_replans_fresh_acceptance,
    check_cycle_fresh_outbound_does_not_delay,
    check_cycle_default_disabled,
    check_cycle_active_no_refresh,
    check_cycle_failure_bound_survives_restart,
    check_cycle_failures_are_isolated,
    check_cycle_preserves_delivery_claims,
    check_cycle_retry_schedule,
    check_cycle_managed_inbound_only,
    check_cycle_conversation_only_identity_is_candidate,
    check_cycle_provider_id_identity_is_unknown,
    check_cycle_unobservable_acceptance_needs_two_clean_reads,
    check_cycle_inbound_in_second_window_resets,
    check_cycle_candidate_bound,
    check_cycle_alias_deduplication,
    check_cycle_refresh_atomic_on_invalid_recipient,
    check_cycle_rejects_unauthorized_threshold,
    check_cycle_capacity_wait_preserves_attempts,
    check_cycle_unknown_coverage_retains_positive_engagement,
    check_cycle_undated_history_remains_unknown,
    check_orchestrator_service_integration,
    check_legacy_final_guard_preserves_claim,
    check_malformed_event_does_not_starve_candidates,
    check_history_identity_without_profile_recovers,
)


def validate():
    try:
        runtime = importlib.import_module("inactivity_closure")
    except ModuleNotFoundError as error:
        if error.name != "inactivity_closure":
            raise
        return {"state": "BLOCKED", "synthetic_only": True, "external_calls": 0,
                "blocker": "Parent-owned inactivity_closure.py is not available",
                "planned_checks": [check.__name__ for check in PLAN_CHECKS + CYCLE_CHECKS]}
    checks, failures = [], []
    for check in PLAN_CHECKS + CYCLE_CHECKS:
        try:
            with fixture() as (database, root):
                check(runtime, database, root)
        except Exception as error:
            failures.append({"check": check.__name__, "error": type(error).__name__ + ": " + str(error)})
        else:
            checks.append(check.__name__)
    return {"state": "FAIL" if failures else "PASS", "synthetic_only": True,
            "external_calls": 0, "checks": checks, "failures": failures}


if __name__ == "__main__":
    result = validate()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["state"] == "PASS" else 2)
