"""E3.T7 option A (owner, 2026-10-21): the outbound guardrail suite on the Unipile path.

Every scenario drives only the scheduled entry points over `linkedin_port.FakePort`, with the real
database and a frozen clock:
- research ingestion: `research_workflow.process_research` (what the research step hands its result
  to), then `orchestrator.draft_promotion_cycle` and `orchestrator.new_contact_execution_cycle`
  for INVITE and the existing-chat INITIAL (with `orchestrator.extended_outreach_cycle`, the hourly
  guarded-message step, which also takes queued INITIAL/FIRST_MESSAGE actions);
- `orchestrator.followup_execution_cycle` for FOLLOWUP;
- FIRST_MESSAGE: no scheduled step queues one on Unipile (owner choice: after a Unipile INVITE the first
  DM stays follow-up 1), so the action is queued directly and executed only by the hourly steps above;
- REPLY: the Unipile inbox read (`orchestrator.unipile_engagement_cycle`), the routing in
  `orchestrator.conversion_cycle`, the reply AI (`reply_classification_cycle`) and
  `reply_execution_cycle`.
No sequence helper is called directly. Each scenario asserts the exact number of LinkedIn writes
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
from `port.calls` (WRITES), never only the final state.

Matrix (guardrail x purpose):

  guardrail                         INVITE                 FOLLOWUP             INITIAL (existing chat)   FIRST_MESSAGE              REPLY
  claim-before-send (claim          InviteClaim            FollowupClaim        InitialClaim              FirstMessageClaim          ReplyClaim
    committed first, changes
    between gate/claim/transport)
  never-resend (ack, timeout        InviteNeverResend      FollowupNeverResend  InitialNeverResend        FirstMessageNeverResend    ReplyNeverResend (at most
    landed / not, crash after                                                                                                        3 writes, each after a
    claim, restart, unreadable)                                                                                                      complete chat read)
  one reply per inbound             (n/a)                  (n/a)                (n/a)                     (one slot per person)      ReplyNeverResend
  3 new people per company per      InviteContactCap       (n/a)                InitialContactCap         (n/a: no new people)       (n/a)
    research run
  exclusions / suppressions /       InviteExclusions       FollowupExclusions*  InitialExclusions*        FirstMessageExclusions*    ReplyExclusions*,
    opt-out / reply / closure                                                                                                        ReplySkippedBeforeTheReplyAI
  already contacted                 InviteAlreadyContacted (n/a)                InitialClaim (our message (n/a)                       (n/a)
                                                                                in the chat),
                                                                                InitialExclusions
  later research run never          InviteContactCap /     (n/a)                InitialNeverResend        (n/a)                      (n/a)
    re-queues                       InviteAlreadyContacted
  * two classes each: the change lands after the send judgment (...AfterInspection, or between the
    promotion and the execution step), and after the claim at the start of the port send (...InTransport).

The INVITE and FOLLOWUP rows patch the inspection (`outreach_agent.Adapter.inspect`); the INITIAL,
FIRST_MESSAGE and REPLY rows run the real pre-send inspection over the port (merged from e3t3-presend
and e3t3-inbox; their own tests in tests/test_unipile_presend.py and tests/test_unipile_inbox.py) with
only the model scripted. Scenarios ported from the HeyReach suite
(tests/test_outbound_integration_safety.py) name their original in the docstring.

`tests/mutate_guardrails_unipile.py` disables each guard in turn and shows this suite fails.
"""

import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

# Every module on the outbound path is imported here, so `frozen` reaches the ones the cycles import lazily.
import applications
import engagement
import followup_execution
import inactivity_closure
import linkedin_port
import manual_followup_e2e
import orchestrator
import outreach
import outreach_agent
import outreach_campaign
import outreach_promotion
import outreach_provider
import owner_seed
import pacing
import reply_execution
import reply_worker
import research_workflow as research
import sequence
import workflow_tasks
from linkedin_port import FakePort

# The real pre-send inspection, before Base patches it for the INVITE/FOLLOWUP rows.
REAL_INSPECT = outreach_agent.Adapter.inspect


ROOT = Path(__file__).resolve().parents[1]
OWNER = "https://www.linkedin.com/in/guardrail-owner"
COMPANY = "Guardrail Company"
# A Tuesday, 10:00 in Los Angeles: inside working hours and the 09:00 service day.
START = dt.datetime(2026, 10, 20, 17, 0, tzinfo=dt.timezone.utc)
WRITES = ("send_invitation", "send_message", "start_chat")


def profile(slug):
    return "https://www.linkedin.com/in/" + slug


# --- frozen clock ----------------------------------------------------------------------------------
class Clock:
    def __init__(self, now):
        self.now = now

    def advance(self, **delta):
        self.now += dt.timedelta(**delta)


@contextlib.contextmanager
def frozen(clock):
    """Every loaded project module that does `import datetime as dt` sees `clock.now` as the
    current time (cycles called without `now` read the wall clock)."""
    class Meta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, dt.datetime)

        def __subclasscheck__(cls, other):
            return issubclass(other, dt.datetime)

    class Frozen(dt.datetime, metaclass=Meta):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz) if tz is not None else clock.now.astimezone().replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return clock.now.astimezone(dt.timezone.utc).replace(tzinfo=None)

    proxy = types.ModuleType("frozen_datetime")
    proxy.__dict__.update({name: getattr(dt, name) for name in dir(dt) if not name.startswith("__")})
    proxy.datetime = Frozen
    with contextlib.ExitStack() as stack:
        for module in list(sys.modules.values()):
            location = getattr(module, "__file__", None) or ""
            if Path(location).parent == ROOT and getattr(module, "dt", None) is dt:
                stack.enter_context(patch.object(module, "dt", proxy))
        yield


def evidence(field, value, at):
    return {"field": field, "value": value, "source": "https://example.test/jobs/123",
            "observed_at": at, "verified": True, "excerpt": "MOCK fixture only", "kind": "official"}


class Base(unittest.TestCase):
    """One real database file, a frozen clock, FakePort, and the scheduled cycles."""

    def setUp(self):
        self.clock = Clock(START)
        self.enterContext(frozen(self.clock))
        self.enterContext(manual_followup_e2e.offline())  # no sockets, subprocesses or URL opens
        # Pacing has its own tests (test_pacing); here it always says go.
        self.enterContext(patch("pacing.check", return_value={"go": True}))
        self.enterContext(patch("orchestrator.linkedin_port_for", side_effect=lambda config: self.port))
        self.enterContext(patch.object(orchestrator, "HeyReach", side_effect=lambda config: Mock(spec=["call", "_request"])))
        self.enterContext(patch("outreach_campaign.prepare_candidates", side_effect=AssertionError("No HeyReach campaign")))
        self.seed = {}
        self.enterContext(patch("owner_seed.load", side_effect=lambda: self.seed))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        (self.home / "state").mkdir()
        self.path = self.home / "state/orchestrator.sqlite3"
        self.database = self.connect()
        research.initialize(self.database)
        outreach.initialize_actions(self.database)
        research.seed_history(self.database, {"companies": []}, history_complete=True)
        research.register_claims(self.database, [{"text": "Built reliable APIs", "tags": ["backend"]}],
                                 designated_source_text="Built reliable APIs", source_sha256="a" * 64,
                                 designated_sha256="a" * 64)
        self.database.commit()
        self.port = FakePort(own_profile=OWNER)
        self.config = {"sender_id": 900001, "sender_profile": OWNER, "linkedin_provider": "unipile",
                       "outreach_actions_enabled": True, "outreach_mode": "guarded",
                       "outreach_allowed_purposes": ["INITIAL", "FIRST_MESSAGE", "INVITE", "FOLLOWUP"],
                       "outreach_history_max_age_seconds": 3600, "model": "synthetic", "codex": "NEVER",
                       "timezone": "America/Los_Angeles", "daily_compute_seconds": 10800,
                       "followup_execution": {"enabled": True, "dispatch_enabled": True, "timeout_seconds": 1,
                                              "max_items": 4},
                       "outreach_agent": {"enabled": True, "max_items": 4, "timeout_seconds": 1}}
        self.plans = 0
        self.enterContext(patch.object(outreach_agent.Adapter, "inspect",
                                       side_effect=lambda action, config: self.proof(action)))

    def connect(self):
        """The service's own connection (orchestrator.connect: WAL, its tables) to a temporary home."""
        database = orchestrator.connect(self.home)
        self.addCleanup(database.close)
        return database

    def restart(self):
        """A new process: a fresh connection to the same file (the old one is dropped mid-state)."""
        self.database = self.connect()
        return self.database

    # --- research -> promotion -> execution (INVITE) ---------------------------------------------
    def research_run(self, people, company=COMPANY):
        """One research run for `company` naming `people` (slugs); returns the drafts it made."""
        self.plans += 1
        plan = "plan-%d" % self.plans
        at = self.clock.now.isoformat()
        research.plan_research(self.database, plan, [company], now=at)
        found = []
        for slug in people:
            url = profile(slug)
            if outreach.profile_key(url) not in self.port.people:
                self.port.add_person(url)
            found.append({"recipient": slug.replace("-", " ").title(), "profile": url, "employer_matches_target": True,
                          "evidence": [evidence("identity", url, at), evidence("employment", company, at),
                                       evidence("team", "API team", at), evidence("ability_to_help", "Runs hiring", at),
                                       evidence("contact_type", "hiring_manager", at)]})
        role = {"company": company, "role": "Staff Software Engineer", "url": "https://example.test/jobs/123",
                "requisition_id": "123", "rationale": "MOCK only", "people": found,
                "evidence": [evidence("team_need", "reliable APIs", at), evidence("specialization", "backend", at)]}
        result = research.process_research(self.database, {"research_status": "COMPLETE", "summary": "Mock",
                                                           "blockers": [], "opportunities": [role], "drafts": []},
                                           plan_id=plan, now=at)
        self.database.commit()
        return result["drafts"]

    def proof(self, action):
        return {"identity": outreach.profile_key(action["profile"]), "sender_id": 900001,
                "checked_at": self.clock.now.isoformat(), "evidence": "mock:authenticated-history",
                "sender_ready": True, "entitlement_verified": True, "direct_messages_complete": True,
                "sent_invitations_complete": True, "pending_invitation": False, "cooldown": False,
                "prior_outreach": False, "has_reply": False, "negative_reply": False, "opt_out": False,
                "conversation_id": None, "route": "INVITATION", "company_contact_count": 0,
                "company_count_evidence": "mock:company-history", "followups_sent": 0, "initial_message_sent": False,
                "agent_decision": "READY", "provider_snapshot_complete": True,
                "next_action": "Use the shared action guard"}

    def promote(self):
        return orchestrator.draft_promotion_cycle(self.database, self.config, port=self.port)

    def execute(self):
        return orchestrator.new_contact_execution_cycle(ROOT, self.database, self.config, port=self.port)

    def hourly(self, cycles=1, minutes=60):
        """The two INVITE steps of the hourly service cycle, `cycles` times, `minutes` apart."""
        results = []
        for unused in range(cycles):
            results.append((self.promote(), self.execute()))
            self.clock.advance(minutes=minutes)
        return results

    # --- observations -----------------------------------------------------------------------------
    def writes(self, method=None):
        return sum(1 for call in self.port.calls if call in ((method,) if method else WRITES))

    def invites(self, slug=None):
        rows = self.database.execute("""SELECT * FROM outreach_actions WHERE json_extract(payload,'$.purpose')='INVITE'
            ORDER BY rowid""").fetchall()
        if slug is not None:
            rows = [row for row in rows if row["identity"] == outreach.profile_key(profile(slug))]
        return rows

    def invite_state(self, slug):
        rows = self.invites(slug)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["state"]

    def invitations_to(self, slug):
        provider_id = self.port.people[outreach.profile_key(profile(slug))]["provider_id"]
        return [item for item in self.port.invitations if item["invitee_provider_id"] == provider_id]

    def kinds(self, slug, kind=None):
        events = engagement.events_for(self.database, outreach.profile_key(profile(slug)))
        return [event["kind"] for event in events if kind is None or event["kind"] == kind]

    def event(self, slug, kind, source="heyreach", source_id=None, **changes):
        at = self.clock.now.isoformat()
        engagement.record_event(self.database, {
            "source": source, "source_id": source_id or (kind + ":" + slug), "kind": kind, "profile": profile(slug),
            "occurred_at": at, "observed_at": at, "verified": True, "evidence": "mock-independent-evidence",
            "raw": {"mock": True}, **changes})

    def gates(self, slug):
        """Every refusal recorded on the person's INVITE row (promotion hold, action gates, the
        port check at send)."""
        row = self.invites(slug)[0]
        result = json.loads(row["result"]) if row["result"] else []
        if isinstance(result, dict):
            return result.get("promotion_hold", []) + result.get("provider_guard", {}).get("gates", [])
        return result


# =====================================================================================================
# INVITE
# =====================================================================================================
class InviteClaim(Base):
    """Claim-before-send on INVITE: the claim and REQUEST_STARTED are committed before the one write,
    and anything that changes between the gate, the claim and the transport refuses the write."""

    def setUp(self):
        super().setUp()
        self.research_run(["ada-claim"])

    def test_claim_and_request_started_are_committed_before_the_write(self):
        seen = []
        original = self.port.send_invitation

        def observe_then_send(provider_id, note):
            other = sqlite3.connect(self.path)  # another process's view: only committed rows
            try:
                claims = other.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0]
                state = other.execute("SELECT state FROM outreach_actions").fetchone()[0]
            finally:
                other.close()
            seen.append((claims, state))
            return original(provider_id, note)

        self.port.send_invitation = observe_then_send
        self.hourly(2)
        self.assertEqual(seen, [(1, "REQUEST_STARTED")])
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-claim"), "CONFIRMED_SENT")

    def test_existing_claim_for_the_person_refuses(self):
        """Ports test_legacy_campaign_claim_blocks_new_action_after_restart."""
        self.promote()
        self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                              ("other-action", "{}", "x", outreach.profile_key(profile("ada-claim")), 900001, "{}",
                               self.clock.now.isoformat()))
        self.database.commit()
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("IMMUTABLE_OUTREACH_CLAIM_EXISTS", self.gates("ada-claim"))

    def test_reply_between_gate_and_claim_refuses(self):
        """Ports test_reply_between_new_gate_and_claim_blocks_send."""
        original, calls = outreach.action_gates, []

        def gate_then_reply(database, config, action, proof, now):
            result = original(database, config, action, proof, now)
            calls.append(True)
            if len(calls) == 1:
                self.event("ada-claim", "inbound")
            return result

        self.promote()
        with patch.object(outreach, "action_gates", side_effect=gate_then_reply):
            self.execute()
        self.hourly(2)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)

    def test_cancel_between_gate_and_claim_refuses(self):
        """Ports test_cancel_between_new_gate_and_claim_blocks_send."""
        original, calls = outreach.action_gates, []

        def gate_then_cancel(database, config, action, proof, now):
            result = original(database, config, action, proof, now)
            calls.append(True)
            if len(calls) == 1:
                with database:
                    database.execute("UPDATE outreach_actions SET state='CANCELLED'")
            return result

        self.promote()
        with patch.object(outreach, "action_gates", side_effect=gate_then_cancel):
            self.execute()
        self.hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_reply_after_claim_before_transport_refuses(self):
        """Ports test_reply_after_dispatch_claim_before_provider_call_blocks_send."""
        original = outreach.HeyReachActionAdapter.execute_claimed

        def reply_then_execute(adapter, database, action_id, config, now=None):
            self.event("ada-claim", "inbound")
            return original(adapter, database, action_id, config, now=now)

        self.promote()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", reply_then_execute):
            self.execute()
        self.hourly(2)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.invite_state("ada-claim"), "CANCELLED_BEFORE_SEND")

    def test_payload_replaced_after_inspection_refuses(self):
        """Ports test_inspected_payload_cannot_be_replaced_before_claim. The replacement is written (the
        strategy-binding trigger that used to refuse it went with learning, 2026-10-22); the send path's
        own checks refuse the changed payload, so nothing is claimed or sent."""
        def replace_then_prove(action, config):
            changed = dict(action, note="Different unverified note", message="Different unverified note")
            with self.database:
                self.database.execute("UPDATE outreach_actions SET payload=? WHERE action_id=?",
                                      (json.dumps(changed, sort_keys=True), action["action_id"]))
            return self.proof(action)

        self.promote()
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=replace_then_prove):
            self.execute()
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)

    def test_stale_history_proof_refuses(self):
        """Ports test_stale_direct_history_blocks_without_provider_write."""
        stale = lambda action, config: dict(self.proof(action), checked_at=(self.clock.now - dt.timedelta(hours=2)).isoformat())
        self.promote()
        with patch.object(outreach_agent.Adapter, "inspect", side_effect=stale):
            self.execute()
        self.assertEqual(self.writes(), 0)
        self.assertIn("DIRECT_HISTORY_MISSING_OR_STALE", self.gates("ada-claim"))

    def test_stale_identity_facts_refuse(self):
        """Ports test_stale_content_and_identity_facts_require_refresh: a draft verified eight days
        ago is not sent on that verification."""
        self.promote()
        self.clock.advance(days=8)
        self.execute()
        self.assertEqual(self.writes(), 0)
        self.assertIn("CONTENT_OR_IDENTITY_VERIFICATION_STALE", self.gates("ada-claim"))

    def test_cycle_without_the_port_never_claims(self):
        """The hourly guarded-message step (no port) runs before the INVITE step."""
        self.promote()
        calls = len(self.port.calls)
        with patch("orchestrator.linkedin_port_for", return_value=None):
            orchestrator.extended_outreach_cycle(self.database, self.config)
        self.assertEqual(len(self.port.calls), calls)
        self.assertEqual(self.invite_state("ada-claim"), "PENDING")
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.assertEqual(self.writes(), 0)
        self.execute()
        self.assertEqual(self.writes(), 1)


class InviteNeverResend(Base):
    """Never-resend on INVITE: an acknowledgement is not delivery, a landed timeout is confirmed and
    not resent, an unlanded one is resent only after a read shows it absent (at most 3 writes in
    all), a crash is read before anything else, and an unreadable read waits."""

    def setUp(self):
        super().setUp()
        self.research_run(["ada-resend"])

    def test_ack_is_not_delivery_and_restart_never_resends(self):
        """Ports test_acknowledgment_is_not_delivery_and_restart_never_resends."""
        self.promote()
        self.execute()
        self.assertEqual(self.invite_state("ada-resend"), "ACKNOWLEDGED_DELIVERY_UNCONFIRMED")
        self.assertEqual(self.kinds("ada-resend"), [])
        self.restart()
        self.clock.advance(hours=1)
        self.hourly(4)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_SENT")
        self.assertEqual(self.kinds("ada-resend"), ["initial_sent"])

    def test_timeout_after_the_write_landed_is_confirmed_not_resent(self):
        """Ports test_send_timeout_is_not_retried_after_restart (the write landed)."""
        self.port.fail("send_invitation", "timeout_after_write")
        self.promote()
        self.execute()
        self.assertEqual(self.invite_state("ada-resend"), "INVITE_RETRY_WAIT")
        self.restart()
        self.hourly(4)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.invitations_to("ada-resend")), 1)
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_SENT")

    def test_unlanded_timeouts_resend_only_after_absence_at_most_three_writes(self):
        self.port.fail("send_invitation", "timeout", times=10)
        self.hourly(8)
        self.assertEqual(self.writes(), 3)
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_NOT_SENT")
        self.assertEqual(self.kinds("ada-resend"), [])
        # Every write after the first followed a read of the sent-invitation listing.
        calls = [call for call in self.port.calls if call in ("send_invitation", "sent_invitations")]
        for index, call in enumerate(calls):
            if call == "send_invitation" and index:
                self.assertEqual(calls[index - 1], "sent_invitations", calls)

    def test_a_later_research_run_never_requeues_a_not_sent_person(self):
        self.port.fail("send_invitation", "timeout", times=10)
        self.hourly(8)
        self.clock.advance(days=1)
        self.assertEqual(self.research_run(["ada-resend"]), [])
        self.hourly(3)
        self.assertEqual(self.writes(), 3)
        self.assertEqual(len(self.invites("ada-resend")), 1)

    def crash_inside_the_write(self, lands):
        original = self.port.send_invitation

        def crash(provider_id, note):
            if lands:
                original(provider_id, note)
            else:
                self.port.calls.append("send_invitation")
            raise SystemExit("process killed during the request")

        self.port.send_invitation = crash
        self.promote()
        with self.assertRaises(SystemExit):
            self.execute()
        self.port.send_invitation = original
        self.restart()

    def test_crash_after_claim_with_the_write_landed_is_read_not_resent(self):
        self.crash_inside_the_write(lands=True)
        self.assertEqual(self.invite_state("ada-resend"), "REQUEST_STARTED")
        self.clock.advance(minutes=5)
        self.hourly(1, minutes=5)  # restarted within the stuck window: nothing is touched
        self.assertEqual(self.invite_state("ada-resend"), "REQUEST_STARTED")
        self.hourly(4)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.invitations_to("ada-resend")), 1)
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_SENT")

    def test_crash_after_claim_without_the_write_is_resent_once_after_absence(self):
        self.crash_inside_the_write(lands=False)
        self.hourly(5)
        self.assertEqual(self.writes(), 2)  # the crashed attempt (nothing landed) and one retry
        self.assertEqual(len(self.invitations_to("ada-resend")), 1)
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_SENT")

    def test_restart_between_claim_and_send_is_read_after_15_minutes_then_sent_once(self):
        """A DISPATCHING invite stranded by a crash after the claim (owner: "recover it"): left alone
        within STUCK_AFTER, then read first, found absent and sent exactly once."""
        def crash(adapter, database, action_id, config, now=None):
            raise SystemExit("process killed after the claim")

        self.promote()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", crash), self.assertRaises(SystemExit):
            self.execute()
        self.restart()
        self.assertEqual(self.invite_state("ada-resend"), "DISPATCHING")
        self.hourly(2, minutes=5)  # restarted within the stuck window: nothing is read or sent
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.invite_state("ada-resend"), "DISPATCHING")
        self.clock.advance(minutes=10)
        self.hourly(5)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.invitations_to("ada-resend")), 1)
        self.assertIn("sent_invitations", self.port.calls[:self.port.calls.index("send_invitation")])
        self.assertEqual(self.invite_state("ada-resend"), "CONFIRMED_SENT")

    def test_unreadable_profile_lookup_waits_and_never_resends(self):
        self.port.fail("send_invitation", "timeout")
        self.promote()
        self.execute()
        self.port.fail("lookup", "timeout", times=6)
        self.hourly(3)
        self.assertEqual(len(self.port.failures["lookup"]), 3)  # three reconcile reads, all failed
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-resend"), "INVITE_RETRY_WAIT")

    def test_incomplete_sent_invitation_listing_waits_and_never_resends(self):
        """The profile reads fine (not connected, nothing pending), so the absence check reads the
        sent-invitation listing, which comes back incomplete: that is never absence."""
        self.port.fail("send_invitation", "timeout")
        self.promote()
        self.execute()
        self.port.fail("sent_invitations", "incomplete_page", times=6)
        self.hourly(3)
        self.assertEqual(len(self.port.failures["sent_invitations"]), 3)  # the listing was read 3 times
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-resend"), "INVITE_RETRY_WAIT")

    def test_undecidable_absence_read_never_resends(self):
        """The sent listing holds an invitation whose invitee the provider does not identify."""
        self.port.fail("send_invitation", "timeout")
        self.promote()
        self.execute()
        self.port.add_invitation(None, None, "someone")
        self.hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-resend"), "INVITE_RETRY_WAIT")

    def test_invitation_sent_by_hand_between_attempts_stops_the_retry(self):
        self.port.fail("send_invitation", "timeout")
        self.promote()
        self.execute()
        provider_id = self.port.people[outreach.profile_key(profile("ada-resend"))]["provider_id"]
        self.port.add_invitation(provider_id, profile("ada-resend"), "sent by hand")
        self.hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertNotIn(self.invite_state("ada-resend"), {"PENDING", "BLOCKED"})

    def test_person_who_invited_the_owner_between_attempts_is_not_invited_again(self):
        """After an unlanded attempt the listing shows it absent, but the profile now shows the
        person's own invitation to the owner: the retry re-runs the send checks and stops."""
        self.port.fail("send_invitation", "timeout")
        self.promote()
        self.execute()
        self.port.people[outreach.profile_key(profile("ada-resend"))]["invitation"] = {"type": "RECEIVED"}
        self.hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.invite_state("ada-resend"), "CANCELLED_BEFORE_SEND")
        self.assertIn("INVITATION_RECEIVED_FROM_THEM", self.gates("ada-resend"))


class InviteContactCap(Base):
    """At most 3 new people per company per research run, carried through to the INVITE writes; a
    later run does not re-queue anyone already invited."""

    def test_fourth_person_in_one_run_is_never_invited(self):
        drafts = self.research_run(["cap-one", "cap-two", "cap-three", "cap-four"])
        self.assertEqual(len(drafts), 3)
        self.hourly(3)
        self.assertEqual(self.writes(), 3)
        self.assertEqual(self.invites("cap-four"), [])
        self.assertEqual(self.invitations_to("cap-four"), [])

    def test_later_run_never_requeues_invited_people_and_gets_its_own_three(self):
        self.research_run(["cap-one", "cap-two", "cap-three"])
        self.hourly(3)
        self.assertEqual(self.writes(), 3)
        self.clock.advance(days=1)
        drafts = self.research_run(["cap-one", "cap-two", "cap-three", "cap-four"])
        self.assertEqual([draft["profile"] for draft in drafts], [profile("cap-four")])
        self.hourly(3)
        self.assertEqual(self.writes(), 4)
        for slug in ("cap-one", "cap-two", "cap-three", "cap-four"):
            self.assertEqual(len(self.invites(slug)), 1)
            self.assertEqual(len(self.invitations_to(slug)), 1)

    def test_later_run_never_requeues_a_person_whose_invite_is_still_uncertain(self):
        self.research_run(["cap-one"])
        self.port.fail("send_invitation", "timeout_after_write")
        self.promote()
        self.execute()
        self.clock.advance(days=1)
        self.assertEqual(self.research_run(["cap-one"]), [])
        self.hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.invites("cap-one")), 1)


class InviteExclusions(Base):
    """Exclusions, suppressions, opt-out, a reply and closure recorded after the draft refuse the
    INVITE at send time (0 writes)."""

    SLUG = "ada-excluded"

    def setUp(self):
        super().setUp()
        self.research_run([self.SLUG])
        self.promote()

    def assertRefused(self, gate=None):
        """The change lands between the promotion step and the execution step of one hourly cycle,
        so the send-time guard (not the next promotion pass) must refuse; later cycles too."""
        self.execute()
        if gate:
            self.assertIn(gate, self.gates(self.SLUG))
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.port.invitations, [])

    def test_owner_excluded_name_refuses(self):
        self.seed = {"excluded_contact_names": ["Ada Excluded"]}
        self.assertRefused("EXCLUDED_CONTACT")

    def test_no_automatic_profile_refuses(self):
        """Ports test_no_automatic_identity_name_and_campaign_each_block_new_path (identity)."""
        self.seed = {"no_automatic_outreach": {"profiles": ["linkedin.com/in/" + self.SLUG]}}
        self.assertRefused("NO_AUTOMATIC_OUTREACH_CONTACT")

    def test_no_automatic_name_refuses(self):
        """Ports test_no_automatic_identity_name_and_campaign_each_block_new_path (name)."""
        self.seed = {"no_automatic_outreach": {"names": [" Ada  Excluded "]}}
        self.assertRefused("NO_AUTOMATIC_OUTREACH_CONTACT")

    def test_suppression_refuses(self):
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                                  (outreach.profile_key(profile(self.SLUG)), "Owner asked: do not contact"))
        self.assertRefused("SUPPRESSED_CONTACT")

    def test_suppression_on_a_verified_alias_refuses(self):
        """Ports test_verified_alias_cannot_evade_existing_contact_suppression."""
        old = profile("ada-previous-identity")
        research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        self.assertRefused("SUPPRESSED_CONTACT")

    def test_opt_out_refuses(self):
        self.event(self.SLUG, "opt_out")
        self.assertRefused("OPTED_OUT")

    def test_opt_out_on_an_alias_refuses(self):
        """Ports test_alias_of_opted_out_person_blocks_without_history_mirror."""
        old = profile("ada-previous-identity")
        research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
        engagement.record_event(self.database, {
            "source": "heyreach", "source_id": "opt-out-alias", "kind": "opt_out", "profile": old,
            "occurred_at": self.clock.now.isoformat(), "observed_at": self.clock.now.isoformat(), "verified": True,
            "evidence": "mock", "raw": {}})
        self.assertRefused()

    def test_inbound_reply_refuses(self):
        self.event(self.SLUG, "inbound")
        self.assertRefused("REPLY_REQUIRES_CONTEXT")

    def test_closure_after_the_claim_refuses_before_the_transport(self):
        """close_outreach moves only PENDING/BLOCKED actions to CLOSED; a closure landing after the
        claim leaves the DISPATCHING row to the send-time gates in execute_claimed."""
        original = outreach.HeyReachActionAdapter.execute_claimed

        def close_then_execute(adapter, database, action_id, config, now=None):
            engagement.close_outreach(database, profile(self.SLUG), "Owner closed", self.clock.now)
            return original(adapter, database, action_id, config, now=now)

        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", close_then_execute):
            self.execute()
        self.assertEqual(self.invite_state(self.SLUG), "CANCELLED_BEFORE_SEND")
        self.assertIn("OUTREACH_CLOSED", self.gates(self.SLUG))
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.port.invitations, [])

    def test_closure_before_the_execution_step_closes_the_queued_invite(self):
        engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now)
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.invite_state(self.SLUG), "CLOSED")


class InviteAlreadyContacted(Base):
    """Someone already contacted (HeyReach-era history, our own records, or LinkedIn showing a
    pending invitation or a connection) is never invited through Unipile."""

    def test_imported_history_is_refused_before_a_draft(self):
        """Ports test_history_only_contact_is_refused_before_a_draft."""
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  (outreach.profile_key(profile("hist-person")), "Hist Person", COMPANY,
                                   profile("hist-person"), json.dumps({"source": "heyreach-handoff"})))
        self.assertEqual(self.research_run(["hist-person"]), [])
        self.hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_history_by_name_under_another_url_is_refused(self):
        """Ports test_name_only_history_matches_profile_once_without_freeing_unknown_slots."""
        with self.database:
            self.database.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                                  ("name:" + orchestrator.name_key("Hist Person"), "Hist Person", COMPANY, None,
                                   json.dumps({"source": "heyreach-handoff"})))
        self.assertEqual(self.research_run(["hist-person"]), [])
        self.hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_excluded_name_is_never_drafted(self):
        self.seed = {"excluded_contact_names": ["Bea Excluded"]}
        self.assertEqual(self.research_run(["bea-excluded"]), [])
        self.hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_heyreach_invitation_recorded_before_research_is_refused(self):
        """Ports test_legacy_dispatch_honors_engagement_initial_delivery."""
        self.event("hr-person", "initial_sent", purpose="INITIAL")
        self.assertEqual(self.research_run(["hr-person"]), [])
        self.hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_heyreach_invitation_recorded_after_the_draft_refuses(self):
        self.research_run(["hr-person"])
        self.promote()
        self.event("hr-person", "initial_sent", purpose="INITIAL")
        self.execute()
        self.assertIn("PREVIOUSLY_CONTACTED_NO_INITIAL", self.gates("hr-person"))
        self.hourly(3)
        self.assertEqual(self.writes(), 0)

    def test_heyreach_uncertain_attempt_refuses(self):
        """Ports test_new_uncertain_action_blocks_legacy_campaign_after_restart (other direction):
        an unreconciled HeyReach action for the person holds the Unipile invite."""
        self.research_run(["hr-person"])
        self.promote()
        outreach.enqueue_action(self.database, {
            "action_id": "heyreach-initial", "recipient": "Hr Person", "company": COMPANY, "profile": profile("hr-person"),
            "message": "Earlier note", "purpose": "INITIAL", "provider": "heyreach", "channel": "linkedin",
            "conversation_id": "c-1", "verification": {}})
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY' WHERE action_id='heyreach-initial'")
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("UNRESOLVED_ACTION_NO_RETRY", self.gates("hr-person"))

    def test_alias_of_an_uncertain_action_refuses(self):
        """Ports test_alias_of_uncertain_action_blocks_second_initial."""
        self.research_run(["hr-person"])
        self.promote()
        old = profile("hr-person-old")
        research.register_alias(self.database, "contact", old, profile("hr-person"), {"source": "mock-verified-mapping"})
        outreach.enqueue_action(self.database, {
            "action_id": "heyreach-initial", "recipient": "Hr Person", "company": COMPANY, "profile": old,
            "message": "Earlier note", "purpose": "INITIAL", "provider": "heyreach", "channel": "linkedin",
            "conversation_id": "c-1", "verification": {}})
        with self.database:
            self.database.execute("UPDATE outreach_actions SET state='UNCERTAIN_NO_RETRY' WHERE action_id='heyreach-initial'")
        self.hourly(3)
        self.assertEqual(self.writes(), 0)

    def test_pending_invitation_on_linkedin_refuses_at_send(self):
        self.research_run(["li-pending"])
        self.promote()
        self.port.add_invitation(None, profile("li-pending"), "sent from the LinkedIn app")
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.invite_state("li-pending"), "CANCELLED_BEFORE_SEND")
        self.assertIn("INVITATION_ALREADY_PENDING", self.gates("li-pending"))

    def test_person_who_invited_the_owner_refuses_at_send(self):
        self.research_run(["li-received"])
        self.promote()
        self.port.people[outreach.profile_key(profile("li-received"))]["invitation"] = {"type": "RECEIVED"}
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("INVITATION_RECEIVED_FROM_THEM", self.gates("li-received"))

    def test_already_connected_refuses_at_send(self):
        self.research_run(["li-connected"])
        self.promote()
        self.port.people[outreach.profile_key(profile("li-connected"))]["distance"] = 1
        self.hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("ALREADY_CONNECTED", self.gates("li-connected"))

    def test_confirmed_person_is_never_requeued_by_a_later_run(self):
        self.research_run(["li-done"])
        self.hourly(2)
        self.assertEqual(self.invite_state("li-done"), "CONFIRMED_SENT")
        for unused in range(2):
            self.clock.advance(days=1)
            self.assertEqual(self.research_run(["li-done"]), [])
            self.hourly(2)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.invites("li-done")), 1)


# =====================================================================================================
# FOLLOWUP
# =====================================================================================================
class FollowupBase(Base):
    """A person invited and confirmed through the INVITE cycles, then accepted; follow-ups run only
    through `orchestrator.followup_execution_cycle`. The model is a scripted executor."""

    SLUG = "fu-person"

    def setUp(self):
        super().setUp()
        self.enterContext(patch("reply_worker.execute_text", side_effect=self.model))
        self.compositions = []
        self.research_run([self.SLUG])
        self.hourly(2, minutes=30)
        self.assertEqual(self.invite_state(self.SLUG), "CONFIRMED_SENT")
        self.provider_id = self.port.people[outreach.profile_key(profile(self.SLUG))]["provider_id"]
        self.invited_at = self.clock.now
        self.base_writes = self.writes()

    def model(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "action" not in packet:
            ordinal = packet["plan"]["ordinal"]
            self.compositions.append(ordinal)
            return json.dumps({"output_version": followup_execution.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "COMPOSE", "message": "Follow-up number %d about the platform role." % ordinal,
                               "reason": "Grounded in the invitation.", "next_action": "Send through the guard."})
        return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                           "decision": "READY", "reason": "The follow-up fits the thread.", "next_action": "Send it.",
                           "conversation_id": packet["action"]["conversation_id"], "receipt_observation": None,
                           "receipt_message": None, "route": "EXISTING_THREAD",
                           "findings": {"prior_outreach": True, "has_reply": False, "negative_reply": False,
                                        "opt_out": False}})

    def followups(self, cycles=1, minutes=20):
        results = []
        for unused in range(cycles):
            results.append(orchestrator.followup_execution_cycle(ROOT, self.database, self.config))
            self.clock.advance(minutes=minutes)
        return results

    def due_first(self):
        """Accepted, and three business days after the invitation."""
        self.port.accept(self.provider_id)
        self.clock.now = engagement.business_days_after(self.invited_at, 3) + dt.timedelta(hours=1)

    def followup_writes(self):
        return self.writes() - self.base_writes

    def state(self, ordinal):
        row = self.database.execute("SELECT state FROM outreach_actions WHERE slot=?", ("FOLLOWUP:%d" % ordinal,)).fetchone()
        return row[0] if row else None

    def ours(self):
        return [message["text"] for chat in self.port.chat_log.values() for message in chat["messages"]
                if message["is_sender"]]


class FollowupNeverResend(FollowupBase):

    def test_both_followups_each_written_once(self):
        self.due_first()
        self.followups(4)
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.followup_writes(), 1)
        sent = engagement.events_for(self.database, outreach.profile_key(profile(self.SLUG)))
        first = [event for event in sent if event["kind"] == "followup_sent"][0]["occurred_at"]
        self.clock.now = engagement.business_days_after(first, 5) + dt.timedelta(hours=1)
        self.followups(4)
        self.assertEqual(self.state(2), "CONFIRMED_SENT")
        self.clock.advance(days=10)
        self.followups(3)
        self.assertEqual(self.followup_writes(), 2)
        self.assertEqual((self.writes("start_chat"), self.writes("send_message")), (1, 1))
        self.assertEqual(len(self.ours()), 2)
        self.assertEqual(self.state(3), None)

    def test_ack_is_not_delivery_and_restart_never_resends(self):
        self.due_first()
        self.followups(1)
        self.assertEqual(self.state(1), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.kinds(self.SLUG, "followup_sent"), [])
        self.restart()
        self.followups(3)
        self.assertEqual(self.followup_writes(), 1)
        self.assertEqual(len(self.kinds(self.SLUG, "followup_sent")), 1)

    def test_timeout_after_the_write_landed_is_recorded_not_resent(self):
        self.port.fail("start_chat", "timeout_after_write")
        self.due_first()
        self.followups(1)
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        self.restart()
        self.followups(4)
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual(self.followup_writes(), 1)
        self.assertEqual(len(self.ours()), 1)

    def test_timeout_that_did_not_land_is_never_resent(self):
        self.port.fail("start_chat", "timeout")
        self.due_first()
        self.followups(5)
        self.assertEqual(self.state(1), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.followup_writes(), 1)
        self.assertEqual(self.ours(), [])

    def test_crash_after_claim_with_the_write_landed_is_read_not_resent(self):
        original = self.port.start_chat

        def crash(provider_id, text):
            original(provider_id, text)
            raise SystemExit("process killed during the request")

        self.port.start_chat = crash
        self.due_first()
        with self.assertRaises(SystemExit):
            self.followups(1)
        self.port.start_chat = original
        self.restart()
        self.followups(4)
        self.assertEqual(self.followup_writes(), 1)
        self.assertEqual(len(self.ours()), 1)
        self.assertEqual(self.state(1), "CONFIRMED_SENT")

    def test_crash_after_claim_without_the_write_is_never_resent(self):
        def crash(provider_id, text):
            self.port.calls.append("start_chat")
            raise SystemExit("process killed during the request")

        original, self.port.start_chat = self.port.start_chat, crash
        self.due_first()
        with self.assertRaises(SystemExit):
            self.followups(1)
        self.port.start_chat = original
        self.restart()
        self.followups(4)
        self.assertEqual(self.followup_writes(), 1)
        self.assertEqual(self.ours(), [])
        self.assertNotIn(self.state(1), {"CONFIRMED_SENT", "PENDING"})

    def test_restart_between_claim_and_send_never_sends(self):
        def crash(adapter, database, action_id, config, now=None):
            raise SystemExit("process killed after the claim")

        self.due_first()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", crash), self.assertRaises(SystemExit):
            self.followups(1)
        self.restart()
        self.assertEqual(self.state(1), "DISPATCHING")
        self.followups(4)
        self.assertEqual(self.followup_writes(), 0)

    def test_unreadable_chats_wait_without_sending(self):
        self.due_first()
        self.port.fail("chats", "incomplete_page", times=20)
        self.followups(3)
        self.assertEqual(self.followup_writes(), 0)


class FollowupClaim(FollowupBase):

    def test_claim_and_request_started_are_committed_before_the_write(self):
        seen = []
        original = self.port.start_chat

        def observe_then_send(provider_id, text):
            other = sqlite3.connect(self.path)
            try:
                seen.append(other.execute("""SELECT action.state, COUNT(claim.action_id) FROM outreach_actions action
                    JOIN outreach_action_claims claim USING(action_id) WHERE action.slot='FOLLOWUP:1'""").fetchone())
            finally:
                other.close()
            return original(provider_id, text)

        self.port.start_chat = observe_then_send
        self.due_first()
        self.followups(2)
        self.assertEqual(seen, [("REQUEST_STARTED", 1)])
        self.assertEqual(self.followup_writes(), 1)

    def test_reply_after_claim_before_transport_refuses(self):
        original = outreach.HeyReachActionAdapter.execute_claimed

        def reply_then_execute(adapter, database, action_id, config, now=None):
            self.event(self.SLUG, "inbound", source_id="late-reply")
            return original(adapter, database, action_id, config, now=now)

        self.due_first()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", reply_then_execute):
            self.followups(1)
        self.followups(3)
        self.assertEqual(self.followup_writes(), 0)

    def test_reply_in_the_chat_after_inspection_refuses_at_the_final_read(self):
        original = followup_execution.FollowupAdapter.inspect

        def inspect_then_reply(adapter, action, config):
            proof = original(adapter, action, config)
            self.port.add_chat(self.provider_id, [(False, "Thanks, happy to talk.")])
            return proof

        self.due_first()
        with patch.object(followup_execution.FollowupAdapter, "inspect", inspect_then_reply):
            self.followups(1)
        self.followups(3)
        self.assertEqual(self.followup_writes(), 0)


class FollowupExclusionCases:
    """Each exclusion lands after the FOLLOWUP action was composed and queued, at the point the
    subclass names, so a send-time check (not composition) must refuse it."""

    def exclude(self, kind):
        if kind == "reply_in_chat":
            self.port.add_chat(self.provider_id, [(False, "Thanks, happy to talk.")])
        elif kind == "hand_message":
            self.port.add_chat(self.provider_id, [(True, "A message the owner wrote by hand.")])
        elif kind == "inbound":
            self.event(self.SLUG, "inbound", source_id="late-inbound")
        elif kind == "opt_out":
            self.event(self.SLUG, "opt_out", source_id="late-opt-out")
        elif kind == "closure":
            engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now)
        elif kind == "alias_suppression":
            old = profile("fu-person-old")
            research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
            with self.database:
                self.database.execute("INSERT INTO suppressions VALUES (?,?)",
                                      (outreach.profile_key(old), "Owner asked: do not contact"))
        elif kind == "excluded_name":
            self.seed = {"excluded_contact_names": ["Fu Person"]}
        elif kind == "no_automatic":
            self.seed = {"no_automatic_outreach": {"profiles": ["linkedin.com/in/" + self.SLUG]}}
        else:
            raise AssertionError(kind)

    def refused(self, kind):
        self.due_first()
        landed = []

        def hooked(original):
            def wrapper(adapter, *args, **kwargs):
                if self.HOOK == "inspect":
                    result = original(adapter, *args, **kwargs)
                    if not landed:
                        landed.append(kind)
                        self.exclude(kind)
                    return result
                if not landed:
                    landed.append(kind)
                    self.exclude(kind)
                return original(adapter, *args, **kwargs)
            return wrapper

        original = getattr(followup_execution.FollowupAdapter, self.HOOK)
        with patch.object(followup_execution.FollowupAdapter, self.HOOK, hooked(original)):
            self.followups(1)
        self.assertEqual(landed, [kind], "the exclusion never landed: the action did not reach " + self.HOOK)
        self.followups(3)
        self.assertEqual(self.followup_writes(), 0)
        self.assertFalse([text for text in self.ours() if text.startswith("Follow-up number")])
        self.assertIn(self.state(1), self.REFUSED_STATES)

    def test_reply_in_chat(self):
        self.refused("reply_in_chat")

    def test_unrecorded_message_of_ours(self):
        self.refused("hand_message")

    def test_recorded_inbound(self):
        self.refused("inbound")

    def test_opt_out(self):
        self.refused("opt_out")

    def test_closure(self):
        self.refused("closure")

    def test_suppression_on_a_verified_alias(self):
        self.refused("alias_suppression")

    def test_owner_excluded_name(self):
        self.refused("excluded_name")

    def test_no_automatic_profile(self):
        self.refused("no_automatic")


class FollowupExclusionsAfterInspection(FollowupExclusionCases, FollowupBase):
    """Lands after the send judgment, before the gates and the claim of the same cycle. A change in
    the chat itself is not a local record, so the final thread read refuses it (CANCELLED_BEFORE_SEND)."""
    HOOK = "inspect"
    REFUSED_STATES = {"BLOCKED", "CLOSED", "CANCELLED_BEFORE_SEND"}


class FollowupExclusionsInTransport(FollowupExclusionCases, FollowupBase):
    """Lands after the claim and REQUEST_STARTED, just before the final thread read and checks in
    `_send_unipile`: only those final checks stand between it and the write."""
    HOOK = "_send_unipile"
    REFUSED_STATES = {"CANCELLED_BEFORE_SEND"}


# =====================================================================================================
# INITIAL (existing chat) and FIRST_MESSAGE
# =====================================================================================================
class MessageBase(Base):
    """INITIAL and FIRST_MESSAGE over the port, with the real pre-send inspection
    (`outreach_agent.Adapter.inspect` reading the person through the port); only the judge model is
    scripted: READY, naming the person's one chat. `messages_hourly` runs the hourly steps that execute
    these actions, in their order: the guarded message step (`orchestrator.extended_outreach_cycle`,
    which takes every queued action whatever its purpose), then promotion and the new-contact step."""

    PURPOSE = "INITIAL"

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(outreach_agent.Adapter, "inspect", REAL_INSPECT))
        self.enterContext(patch("reply_worker.execute_text", side_effect=self.model))

    def model(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        chats = [read["chat_id"] for read in packet["observation"].get("observations", []) if read.get("chat_id")]
        thread = chats[0] if chats else None
        return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                           "decision": "READY", "reason": "Synthetic.", "next_action": "Use the guard.",
                           "findings": {"prior_outreach": False, "has_reply": False, "negative_reply": False,
                                        "opt_out": False},
                           "conversation_id": thread,
                           "route": "EXISTING_THREAD" if thread or packet["action"]["purpose"] == "FIRST_MESSAGE"
                                    else "INVITATION",
                           "receipt_observation": None, "receipt_message": None})

    def messages_hourly(self, cycles=1, minutes=60):
        for unused in range(cycles):
            orchestrator.extended_outreach_cycle(self.database, self.config)
            self.promote()
            self.execute()
            self.clock.advance(minutes=minutes)

    def rows(self, slug=None):
        rows = self.database.execute("SELECT * FROM outreach_actions WHERE json_extract(payload,'$.purpose')=? ORDER BY rowid",
                                     (self.PURPOSE,)).fetchall()
        if slug is not None:
            rows = [row for row in rows if row["identity"] == outreach.profile_key(profile(slug))]
        return rows

    def state(self, slug=None):
        rows = self.rows(slug or self.SLUG)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["state"]

    def refusals(self, slug=None):
        """Every refusal recorded on the person's action (inspection gates, the gates after the claim,
        the final checks in the port send)."""
        row = self.rows(slug or self.SLUG)[0]
        result = json.loads(row["result"]) if row["result"] else []
        if isinstance(result, dict):
            return result.get("provider_guard", {}).get("gates", [])
        return result

    def ours(self):
        return [message["text"] for chat in self.port.chat_log.values() for message in chat["messages"]
                if message["is_sender"]]

    def initials(self, slug=None):
        return self.kinds(slug or self.SLUG, "initial_sent")


class InitialBase(MessageBase):
    """A new contact research found who is already connected and has a chat with the owner: promotion
    queues an INITIAL into that chat (as HeyReach's existing-thread INITIAL), sent by send_message."""

    SLUG = "in-person"

    def setUp(self):
        super().setUp()
        self.provider_id = self.port.add_person(profile(self.SLUG), distance=1)
        self.chat = self.port.add_chat(self.provider_id, [])
        self.research_run([self.SLUG])


class InitialNeverResend(InitialBase):
    """Never-resend on the existing-chat INITIAL: an acknowledgement is not delivery, an uncertain write
    is reconciled from the chat and never resent, and a crash is read, never repeated."""

    def test_one_write_into_the_chat_and_restart_never_resends(self):
        self.promote()
        self.execute()
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.initials(), [])  # an acknowledgement is not delivery
        self.restart()
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual(len(self.initials()), 1)
        self.assertEqual((self.writes("send_message"), self.writes()), (1, 1))
        self.assertEqual(len(self.port.chat_log[self.chat]["messages"]), 1)

    def test_timeout_after_the_write_landed_is_confirmed_not_resent(self):
        self.port.fail("send_message", "timeout_after_write")
        self.promote()
        self.execute()
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.restart()
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.ours()), 1)

    def test_timeout_that_did_not_land_is_never_resent(self):
        self.port.fail("send_message", "timeout")
        self.messages_hourly(6)
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.writes(), 1)
        self.assertEqual((self.ours(), self.initials()), ([], []))

    def crash_inside_the_write(self, lands):
        original = self.port.send_message

        def crash(chat_id, text):
            if lands:
                original(chat_id, text)
            else:
                self.port.calls.append("send_message")
            raise SystemExit("process killed during the request")

        self.port.send_message = crash
        self.promote()
        with self.assertRaises(SystemExit):
            self.execute()
        self.port.send_message = original
        self.restart()

    def test_crash_after_claim_with_the_write_landed_is_read_not_resent(self):
        self.crash_inside_the_write(lands=True)
        self.assertEqual(self.state(), "REQUEST_STARTED")
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes(), len(self.ours())), (1, 1))

    def test_crash_after_claim_without_the_write_is_never_resent(self):
        self.crash_inside_the_write(lands=False)
        self.messages_hourly(5)
        self.assertEqual(self.writes(), 1)
        self.assertEqual((self.ours(), self.initials()), ([], []))
        self.assertNotIn(self.state(), {"CONFIRMED_SENT", "PENDING"})

    def test_restart_between_claim_and_send_never_sends(self):
        def crash(adapter, database, action_id, config, now=None):
            raise SystemExit("process killed after the claim")

        self.promote()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", crash), self.assertRaises(SystemExit):
            self.execute()
        self.restart()
        self.assertEqual(self.state(), "DISPATCHING")
        self.messages_hourly(4)
        self.assertEqual(self.writes(), 0)

    def test_unreadable_chats_never_resend(self):
        self.promote()
        self.execute()
        self.port.fail("chats", "incomplete_page", times=20)
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")

    def test_a_later_research_run_never_requeues_the_person(self):
        self.messages_hourly(2)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        for unused in range(2):
            self.clock.advance(days=1)
            self.assertEqual(self.research_run([self.SLUG]), [])
            self.messages_hourly(2)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.rows(self.SLUG)), 1)


class InitialClaim(InitialBase):
    """Claim-before-send on the existing-chat INITIAL: the claim and REQUEST_STARTED are committed before
    the one write, and anything that changes between the inspection, the claim and the transport refuses."""

    def test_claim_and_request_started_are_committed_before_the_write(self):
        seen = []
        original = self.port.send_message

        def observe_then_send(chat_id, text):
            other = sqlite3.connect(self.path)
            try:
                seen.append(other.execute("""SELECT action.state, COUNT(claim.action_id) FROM outreach_actions action
                    JOIN outreach_action_claims claim USING(action_id)""").fetchone())
            finally:
                other.close()
            return original(chat_id, text)

        self.port.send_message = observe_then_send
        self.messages_hourly(2)
        self.assertEqual(seen, [("REQUEST_STARTED", 1)])
        self.assertEqual(self.writes(), 1)

    def test_existing_claim_for_the_person_refuses(self):
        self.promote()
        self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                              ("other-action", "{}", "x", outreach.profile_key(profile(self.SLUG)), 900001, "{}",
                               self.clock.now.isoformat()))
        self.database.commit()
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("IMMUTABLE_OUTREACH_CLAIM_EXISTS", self.refusals())

    def test_reply_after_claim_before_transport_refuses(self):
        original = outreach.HeyReachActionAdapter.execute_claimed

        def reply_then_execute(adapter, database, action_id, config, now=None):
            self.event(self.SLUG, "inbound")
            return original(adapter, database, action_id, config, now=now)

        self.promote()
        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", reply_then_execute):
            self.execute()
        self.assertEqual(self.state(), "CANCELLED_BEFORE_SEND")
        self.messages_hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_reply_in_the_chat_after_promotion_refuses_at_the_send_time_read(self):
        """Not yet recorded locally: only the inspection's own read of the chat sees it."""
        self.promote()
        self.port.chat_log[self.chat]["messages"].append(
            {"is_sender": False, "text": "Hi, thanks for connecting.", "sent_at": self.clock.now.isoformat()})
        self.execute()
        self.assertIn("HAS_REPLY_PRESENT", self.refusals())
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)

    def test_message_of_ours_in_the_chat_after_promotion_refuses_at_the_send_time_read(self):
        """The owner wrote in the chat by hand after the draft: the INITIAL is never added to it."""
        self.promote()
        self.port.chat_log[self.chat]["messages"].append(
            {"is_sender": True, "text": "A note the owner wrote by hand.", "sent_at": self.clock.now.isoformat()})
        self.execute()
        self.assertIn("PREVIOUSLY_CONTACTED_NO_INITIAL", self.refusals())
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.ours(), ["A note the owner wrote by hand."])

    def test_cycle_without_the_port_never_sends(self):
        """The guarded message step built with no port (HeyReach mode) never reaches a transport for a
        Unipile INITIAL; the port cycle later sends it once."""
        self.promote()
        client = Mock(spec=["call", "_request"])
        with patch("orchestrator.linkedin_port_for", return_value=None), patch.object(orchestrator, "HeyReach", return_value=client):
            orchestrator.extended_outreach_cycle(self.database, self.config)
        client._request.assert_not_called()
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM outreach_action_claims").fetchone()[0], 0)
        self.assertEqual(self.writes(), 0)
        self.clock.advance(hours=1)
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 1)


class InitialContactCap(MessageBase):
    """At most 3 new people per company per research run: a fourth connected person never gets an INITIAL."""

    def test_fourth_person_in_one_run_is_never_messaged(self):
        slugs = ["in-one", "in-two", "in-three", "in-four"]
        for slug in slugs:
            self.port.add_chat(self.port.add_person(profile(slug), distance=1), [])
        self.assertEqual(len(self.research_run(slugs)), 3)
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 3)
        self.assertEqual(self.rows("in-four"), [])
        self.assertEqual(len(self.ours()), 3)


class InitialExclusions(InitialBase):
    """Exclusions, suppressions, opt-out, a reply and closure recorded after the INITIAL was queued
    refuse it at send time (0 writes)."""

    def setUp(self):
        super().setUp()
        self.promote()

    def assertRefused(self, gate=None):
        self.execute()
        if gate:
            self.assertIn(gate, self.refusals())
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.ours(), [])

    def test_owner_excluded_name_refuses(self):
        self.seed = {"excluded_contact_names": ["In Person"]}
        self.assertRefused("EXCLUDED_CONTACT")

    def test_no_automatic_profile_refuses(self):
        self.seed = {"no_automatic_outreach": {"profiles": ["linkedin.com/in/" + self.SLUG]}}
        self.assertRefused("NO_AUTOMATIC_OUTREACH_CONTACT")

    def test_suppression_on_a_verified_alias_refuses(self):
        old = profile("in-person-old")
        research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        self.assertRefused("SUPPRESSED_CONTACT")

    def test_opt_out_refuses(self):
        self.event(self.SLUG, "opt_out")
        self.assertRefused("OPTED_OUT")

    def test_inbound_reply_refuses(self):
        self.event(self.SLUG, "inbound")
        self.assertRefused("REPLY_REQUIRES_CONTEXT")

    def test_heyreach_initial_recorded_after_the_draft_refuses(self):
        self.event(self.SLUG, "initial_sent", purpose="INITIAL")
        self.assertRefused("PREVIOUSLY_CONTACTED_NO_INITIAL")

    def test_closure_before_the_execution_step_closes_the_queued_initial(self):
        engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now)
        self.assertRefused()
        self.assertEqual(self.state(), "CLOSED")


class InitialExclusionsInTransport(InitialBase):
    """Lands after the claim and REQUEST_STARTED, at the start of the port send (`_send_port`): only its
    final checks (closure and inactivity, engagement stops, suppressions) stand between it and the write."""

    def refused(self, change, gate):
        original = outreach_agent.Adapter._send_port

        def change_then_send(adapter, action_id, config):
            change()
            return original(adapter, action_id, config)

        self.promote()
        with patch.object(outreach_agent.Adapter, "_send_port", change_then_send):
            self.execute()
        self.assertEqual(self.state(), "CANCELLED_BEFORE_SEND")
        self.assertEqual(self.refusals(), [gate])
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.ours(), [])

    def test_inbound_reply(self):
        self.refused(lambda: self.event(self.SLUG, "inbound", source_id="late-inbound"), "REPLY_REQUIRES_CONTEXT")

    def test_opt_out(self):
        self.refused(lambda: self.event(self.SLUG, "opt_out", source_id="late-opt-out"), "OPTED_OUT")

    def test_closure(self):
        self.refused(lambda: engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now), "OUTREACH_CLOSED")

    def test_suppression_on_a_verified_alias(self):
        """(The person's own key already holds the suppression row the claim writes for its own send.)"""
        def suppress():
            old = profile("in-person-old")
            research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
            with self.database:
                self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        self.refused(suppress, "SUPPRESSED_CONTACT")

class FirstMessageBase(MessageBase):
    """A FIRST_MESSAGE to an accepted person over the port (start_chat when there is no chat). No
    scheduled step queues one on Unipile today (owner choice: after a Unipile INVITE the first DM stays
    follow-up 1), so the action is queued as test_unipile_presend does and executed only by the hourly
    steps."""

    PURPOSE = "FIRST_MESSAGE"
    SLUG = "fm-person"
    TEXT = "Hi, thanks for connecting."

    def setUp(self):
        super().setUp()
        self.provider_id = self.port.add_person(profile(self.SLUG), distance=1)
        accepted = (self.clock.now - dt.timedelta(hours=13)).isoformat()
        engagement.record_event(self.database, {
            "source": "unipile", "source_id": "acceptance:" + self.SLUG, "profile": profile(self.SLUG),
            "kind": "accepted", "verified": True, "occurred_at": accepted, "observed_at": accepted,
            "evidence": "unipile-lookup:" + accepted, "raw": {}})
        self.queue()

    def queue(self, action_id="first-message-1"):
        outreach.enqueue_action(self.database, {
            "action_id": action_id, "profile": profile(self.SLUG), "recipient": "Fm Person", "company": COMPANY,
            "purpose": "FIRST_MESSAGE", "channel": "linkedin", "provider": "unipile", "conversation_id": None,
            "message": self.TEXT,
            "verification": {"content_verified": True, "content_sources": ["mock"], "identity_verified": True,
                             "identity_source": "mock", "verified_at": self.clock.now.isoformat()}})
        self.database.commit()


class FirstMessageNeverResend(FirstMessageBase):
    """Never-resend on FIRST_MESSAGE: one start_chat, confirmed from the chat; an uncertain write or a crash
    is reconciled, never repeated; a final profile read that fails sends nothing."""

    def test_one_write_and_restart_never_resends(self):
        self.execute()
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual(self.initials(), [])  # an acknowledgement is not delivery
        self.restart()
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual(len(self.initials()), 1)
        self.assertEqual((self.writes("start_chat"), self.writes()), (1, 1))
        self.assertEqual(self.ours(), [self.TEXT])

    def test_timeout_after_the_write_landed_is_confirmed_not_resent(self):
        self.port.fail("start_chat", "timeout_after_write")
        self.execute()
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.restart()
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes(), self.ours()), (1, [self.TEXT]))

    def test_timeout_that_did_not_land_is_never_resent(self):
        self.port.fail("start_chat", "timeout")
        self.messages_hourly(6)
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.assertEqual(self.writes(), 1)
        self.assertEqual((self.ours(), self.initials()), ([], []))

    def crash_inside_the_write(self, lands):
        original = self.port.start_chat

        def crash(provider_id, text):
            if lands:
                original(provider_id, text)
            else:
                self.port.calls.append("start_chat")
            raise SystemExit("process killed during the request")

        self.port.start_chat = crash
        with self.assertRaises(SystemExit):
            self.execute()
        self.port.start_chat = original
        self.restart()

    def test_crash_after_claim_with_the_write_landed_is_read_not_resent(self):
        self.crash_inside_the_write(lands=True)
        self.assertEqual(self.state(), "REQUEST_STARTED")
        self.messages_hourly(4)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes(), self.ours()), (1, [self.TEXT]))

    def test_crash_after_claim_without_the_write_is_never_resent(self):
        self.crash_inside_the_write(lands=False)
        self.messages_hourly(5)
        self.assertEqual(self.writes(), 1)
        self.assertEqual((self.ours(), self.initials()), ([], []))
        self.assertNotIn(self.state(), {"CONFIRMED_SENT", "PENDING"})

    def test_restart_between_claim_and_send_never_sends(self):
        def crash(adapter, database, action_id, config, now=None):
            raise SystemExit("process killed after the claim")

        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", crash), self.assertRaises(SystemExit):
            self.execute()
        self.restart()
        self.assertEqual(self.state(), "DISPATCHING")
        self.messages_hourly(4)
        self.assertEqual(self.writes(), 0)

    def test_failed_final_profile_read_sends_nothing_and_is_never_resent(self):
        """With no chat, the port send reads the provider id first; a read that fails leaves the action
        uncertain before any write."""
        original = outreach_agent.Adapter._send_port

        def fail_lookup_then_send(adapter, action_id, config):
            self.port.fail("lookup", "timeout")
            return original(adapter, action_id, config)

        with patch.object(outreach_agent.Adapter, "_send_port", fail_lookup_then_send):
            self.execute()
        self.assertEqual(self.state(), "UNCERTAIN_NO_RETRY")
        self.messages_hourly(4)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.ours(), [])

    def test_unreadable_chats_never_resend(self):
        self.execute()
        self.port.fail("chats", "incomplete_page", times=20)
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.state(), "ACKNOWLEDGED_UNCONFIRMED")

    def test_second_first_message_for_the_person_is_refused(self):
        """One initial-message slot per person (outreach_actions UNIQUE(identity, slot))."""
        self.messages_hourly(2)
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        with self.assertRaisesRegex(ValueError, "slot already reserved"):
            self.queue("first-message-2")
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 1)
        self.assertEqual(len(self.rows()), 1)


class FirstMessageClaim(FirstMessageBase):
    """Claim-before-send on FIRST_MESSAGE."""

    def test_claim_and_request_started_are_committed_before_the_write(self):
        seen = []
        original = self.port.start_chat

        def observe_then_send(provider_id, text):
            other = sqlite3.connect(self.path)
            try:
                seen.append(other.execute("""SELECT action.state, COUNT(claim.action_id) FROM outreach_actions action
                    JOIN outreach_action_claims claim USING(action_id)""").fetchone())
            finally:
                other.close()
            return original(provider_id, text)

        self.port.start_chat = observe_then_send
        self.messages_hourly(2)
        self.assertEqual(seen, [("REQUEST_STARTED", 1)])
        self.assertEqual(self.writes(), 1)

    def test_existing_claim_for_the_person_refuses(self):
        self.database.execute("INSERT INTO outreach_action_claims VALUES (?,?,?,?,?,?,?)",
                              ("other-action", "{}", "x", outreach.profile_key(profile(self.SLUG)), 900001, "{}",
                               self.clock.now.isoformat()))
        self.database.commit()
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertIn("IMMUTABLE_OUTREACH_CLAIM_EXISTS", self.refusals())

    def test_reply_after_claim_before_transport_refuses(self):
        original = outreach.HeyReachActionAdapter.execute_claimed

        def reply_then_execute(adapter, database, action_id, config, now=None):
            self.event(self.SLUG, "inbound")
            return original(adapter, database, action_id, config, now=now)

        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", reply_then_execute):
            self.execute()
        self.assertEqual(self.state(), "CANCELLED_BEFORE_SEND")
        self.messages_hourly(2)
        self.assertEqual(self.writes(), 0)

    def test_reply_in_a_chat_before_the_send_refuses_at_the_send_time_read(self):
        """A chat the person opened, not yet recorded locally: the inspection's own read sees it."""
        self.port.add_chat(self.provider_id, [(False, "Thanks for connecting!")])
        self.execute()
        self.assertIn("HAS_REPLY_PRESENT", self.refusals())
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)


class FirstMessageExclusions(FirstMessageBase):
    """Exclusions, suppressions, opt-out, a reply and closure recorded after the FIRST_MESSAGE was queued
    refuse it at send time (0 writes)."""

    def assertRefused(self, gate=None):
        self.execute()
        if gate:
            self.assertIn(gate, self.refusals())
        self.messages_hourly(3)
        self.assertEqual(self.writes(), 0)
        self.assertEqual(self.ours(), [])

    def test_owner_excluded_name_refuses(self):
        self.seed = {"excluded_contact_names": ["Fm Person"]}
        self.assertRefused("EXCLUDED_CONTACT")

    def test_no_automatic_profile_refuses(self):
        self.seed = {"no_automatic_outreach": {"profiles": ["linkedin.com/in/" + self.SLUG]}}
        self.assertRefused("NO_AUTOMATIC_OUTREACH_CONTACT")

    def test_suppression_on_a_verified_alias_refuses(self):
        old = profile("fm-person-old")
        research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
        with self.database:
            self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        self.assertRefused("SUPPRESSED_CONTACT")

    def test_opt_out_refuses(self):
        self.event(self.SLUG, "opt_out")
        self.assertRefused("OPTED_OUT")

    def test_inbound_reply_refuses(self):
        self.event(self.SLUG, "inbound")
        self.assertRefused("REPLY_REQUIRES_CONTEXT")

    def test_closure_before_the_execution_step_closes_the_queued_message(self):
        engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now)
        self.assertRefused()
        self.assertEqual(self.state(), "CLOSED")


class FirstMessageExclusionsInTransport(FirstMessageBase):
    """Lands after the claim and REQUEST_STARTED, at the start of the port send (`_send_port`)."""

    refused = InitialExclusionsInTransport.refused

    def promote(self):  # nothing to promote: the action is already queued
        return None

    def test_inbound_reply(self):
        self.refused(lambda: self.event(self.SLUG, "inbound", source_id="late-inbound"), "REPLY_REQUIRES_CONTEXT")

    def test_opt_out(self):
        self.refused(lambda: self.event(self.SLUG, "opt_out", source_id="late-opt-out"), "OPTED_OUT")

    def test_closure(self):
        self.refused(lambda: engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now),
                     "OUTREACH_CLOSED")

    def test_suppression_on_a_verified_alias(self):
        def suppress():
            old = profile("fm-person-old")
            research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
            with self.database:
                self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        self.refused(suppress, "SUPPRESSED_CONTACT")

# =====================================================================================================
# REPLY
# =====================================================================================================
class ReplyBase(FollowupBase):
    """A person invited through the INVITE cycles and accepted writes to the owner in a Unipile chat.
    `replies` runs the hourly steps that carry it, in their order: the Unipile inbox read
    (`orchestrator.unipile_engagement_cycle`, what `engagement_cycle` runs for Unipile), the routing in
    `orchestrator.conversion_cycle`, the reply AI (`reply_classification_cycle`) and
    `reply_execution_cycle` (promotion, the real inspection over the port, the claim and the port send).
    The reply AI and the send judge are scripted."""

    TEXT = "Happy to share more about the platform team."

    def setUp(self):
        super().setUp()
        self.config = dict(self.config, engagement_enabled=True,
                           outreach_allowed_purposes=self.config["outreach_allowed_purposes"] + ["REPLY"],
                           reply_worker={"enabled": True, "timeout_seconds": 1, "max_items": 4},
                           reply_execution={"enabled": True}, pipeline_routing={"account_ids": [900001]})
        self.port.accept(self.provider_id)
        self.clock.advance(hours=2)

    def model(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "source_snapshot" in packet:  # the reply AI
            body = json.loads(packet["source_snapshot"]["payload"])["inbox"]["text"]
            quote = {"start": 0, "end": len(body), "text": body}
            return json.dumps({
                "output_version": reply_worker.VERSION, "source_sha256": packet["source_sha256"], "label": "QUESTION",
                "literal_quote": body, "quote_span": quote, "role_reference": None, "reason": "Synthetic question.",
                "unknown_factual_requirements": [], "reply_proposal": {"text": self.TEXT, "basis_quote": quote},
                "next_action": "Answer it.", "review_at": (self.clock.now + dt.timedelta(hours=12)).isoformat()})
        if packet.get("action", {}).get("purpose") == "REPLY":  # the send judge
            return json.dumps({"output_version": outreach_agent.VERSION, "source_sha256": packet["source_sha256"],
                               "decision": "READY", "reason": "The reply fits the thread.", "next_action": "Send it.",
                               "conversation_id": packet["action"]["conversation_id"], "receipt_observation": None,
                               "receipt_message": None, "route": "EXISTING_THREAD",
                               "findings": {"prior_outreach": True, "has_reply": True, "negative_reply": False,
                                            "opt_out": False}})
        return super().model(config, prompt, schema, timeout)

    def say(self, text, sender=False):
        """A message in the person's chat (theirs, or the owner's own by hand with `sender`)."""
        chats = [chat for chat, item in self.port.chat_log.items() if item["attendee"] == self.provider_id]
        chat = chats[0] if chats else self.port.add_chat(self.provider_id, [])
        self.port.chat_log[chat]["messages"].append({"is_sender": sender, "text": text, "sent_at": self.clock.now.isoformat()})
        return chat

    def replies(self, cycles=1, minutes=60):
        results = []
        for unused in range(cycles):
            applications.initialize(self.database)  # as orchestrator.service_cycle does first
            self.database.commit()
            results.append({
                "inbox": orchestrator.unipile_engagement_cycle(self.database, self.config, self.port),
                "conversion": orchestrator.conversion_cycle(ROOT, self.database, self.config, "guardrail-run"),
                "classification": orchestrator.reply_classification_cycle(ROOT, self.database, self.config),
                "execution": orchestrator.reply_execution_cycle(ROOT, self.database, self.config)})
            self.clock.advance(minutes=minutes)
        return results

    def asked(self, cycles=1, minutes=60):
        """The person asks a question; the cycles run until the reply action exists (at most 3)."""
        self.say("Could you tell me more about the role?")
        self.clock.advance(minutes=1)
        for unused in range(3):
            self.replies(1, minutes=minutes)
            if self.rows():
                break
        self.assertEqual(len(self.rows()), 1, "the reply was never queued")

    def rows(self):
        return self.database.execute("SELECT * FROM outreach_actions WHERE json_extract(payload,'$.purpose')='REPLY' ORDER BY rowid").fetchall()

    def reply_state(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["state"]

    def reply_result(self):
        result = self.rows()[0]["result"]
        return json.loads(result) if result else None

    def reply_writes(self):
        return self.writes() - self.base_writes

    def replies_sent(self):
        return [text for text in self.ours() if text == self.TEXT]


class ReplyNeverResend(ReplyBase):
    """Never-resend and one reply per inbound on REPLY: an acknowledgement is not delivery; an errored send
    is retried at most 3 times in all, each only after a complete read of the chat shows it absent; a
    landed one is confirmed from the chat; a crash is never repeated."""

    def test_one_reply_per_inbound_across_review_rounds(self):
        self.asked()
        self.replies(30)  # review rounds wake every 12 hours; each finds the message answered
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")
        self.assertEqual((self.reply_writes(), self.writes("send_message")), (1, 1))
        self.assertEqual(self.replies_sent(), [self.TEXT])

    def test_ack_is_not_delivery_and_restart_never_resends(self):
        self.asked()
        self.assertEqual(self.reply_state(), "ACKNOWLEDGED_UNCONFIRMED")
        self.restart()
        self.replies(4)
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")
        self.assertEqual((self.reply_writes(), len(self.replies_sent())), (1, 1))

    def test_timeout_after_the_write_landed_is_confirmed_from_the_chat_not_resent(self):
        self.port.fail("send_message", "timeout_after_write")
        self.asked()
        self.assertEqual(self.reply_state(), "UNCERTAIN_NO_RETRY")
        self.restart()
        self.replies(4)
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")
        self.assertEqual(self.reply_result()["basis"], "REPLY_RETRY_THREAD_CHECK")
        self.assertEqual((self.reply_writes(), len(self.replies_sent())), (1, 1))

    def test_unlanded_errors_resend_only_after_absence_at_most_three_writes(self):
        self.port.fail("send_message", "timeout", times=10)
        self.asked()
        self.replies(8)
        self.assertEqual(self.reply_writes(), 3)
        self.assertEqual(self.reply_state(), "CANCELLED")
        self.assertEqual(self.reply_result()["reply_terminal"], "REPLY_SEND_FAILED_AFTER_3_ATTEMPTS")
        self.assertEqual(self.replies_sent(), [])
        # Every resend followed a read of the chat's messages.
        calls = [call for call in self.port.calls[self.port.calls.index("send_message"):] if call in ("send_message", "messages")]
        for index, call in enumerate(calls):
            if call == "send_message" and index:
                self.assertEqual(calls[index - 1], "messages", calls)

    def crash_inside_the_write(self, lands):
        original = self.port.send_message

        def crash(chat_id, text):
            if lands:
                original(chat_id, text)
            else:
                self.port.calls.append("send_message")
            raise SystemExit("process killed during the request")

        self.port.send_message = crash
        with self.assertRaises(SystemExit):
            self.asked()
        self.port.send_message = original
        self.restart()

    def test_crash_after_claim_with_the_write_landed_is_read_not_resent(self):
        self.crash_inside_the_write(lands=True)
        self.assertEqual(self.reply_state(), "REQUEST_STARTED")
        self.replies(4)
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")
        self.assertEqual((self.reply_writes(), len(self.replies_sent())), (1, 1))

    def test_crash_after_claim_without_the_write_is_never_resent(self):
        self.crash_inside_the_write(lands=False)
        self.replies(5)
        self.assertEqual(self.reply_writes(), 1)
        self.assertEqual(self.replies_sent(), [])
        self.assertNotIn(self.reply_state(), {"CONFIRMED_SENT", "PENDING"})

    def test_restart_between_claim_and_send_never_sends(self):
        def crash(adapter, database, action_id, config, now=None):
            raise SystemExit("process killed after the claim")

        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", crash), self.assertRaises(SystemExit):
            self.asked()
        self.restart()
        self.assertEqual(self.reply_state(), "DISPATCHING")
        self.replies(4)
        self.assertEqual(self.reply_writes(), 0)

    def test_unreadable_chat_never_resends(self):
        self.port.fail("send_message", "timeout")
        self.asked()
        self.port.fail("messages", "incomplete_page", times=40)
        self.replies(4)
        self.assertEqual(self.reply_writes(), 1)
        self.assertEqual(self.reply_state(), "UNCERTAIN_NO_RETRY")

    def test_owner_answer_by_hand_between_attempts_ends_the_retries(self):
        self.port.fail("send_message", "timeout")
        self.asked()
        self.say("The owner answered this by hand.", sender=True)
        self.replies(4)
        self.assertEqual(self.reply_writes(), 1)
        self.assertEqual(self.reply_state(), "CANCELLED")
        self.assertEqual(self.reply_result()["reply_terminal"], "REPLY_THREAD_ALREADY_ANSWERED_BY_ANOTHER_MESSAGE")

    def test_newer_message_between_attempts_stands_the_old_reply_down_and_gets_its_own_reply(self):
        self.port.fail("send_message", "timeout")
        self.asked()
        self.say("Also, is the role remote?")
        self.replies(4)
        old, new = self.rows()
        self.assertEqual(old["state"], "CANCELLED")
        self.assertEqual(json.loads(old["result"])["reply_terminal"], "REPLY_NEWER_CONVERSATION_ACTIVITY")
        self.assertNotIn("reply_retries", json.loads(old["result"]))  # never resent
        # The newer inbound gets its own reply (owner-approved, reply-after-cancel): the old reply's
        # one failed write plus the new reply's one send, and the old reply is never written again.
        self.assertEqual(new["state"], "CONFIRMED_SENT")
        newest = max((event for event in engagement.events_for(self.database, outreach.profile_key(profile(self.SLUG)))
                      if event["kind"] == "inbound"), key=lambda event: engagement.timestamp(event["occurred_at"]))
        self.assertEqual(json.loads(new["payload"])["reply_source_id"], newest["source_id"])
        self.assertNotEqual(json.loads(old["payload"])["reply_source_id"], newest["source_id"])
        self.assertEqual((self.reply_writes(), self.writes("send_message")), (2, 2))
        self.assertEqual(self.replies_sent(), [self.TEXT])


class ReplyClaim(ReplyBase):
    """Claim-before-send on REPLY."""

    def test_claim_and_request_started_are_committed_before_the_write(self):
        seen = []
        original = self.port.send_message

        def observe_then_send(chat_id, text):
            other = sqlite3.connect(self.path)
            try:
                seen.append(other.execute("""SELECT action.state, COUNT(claim.action_id) FROM outreach_actions action
                    JOIN outreach_action_claims claim USING(action_id)
                    WHERE json_extract(action.payload,'$.purpose')='REPLY'""").fetchone())
            finally:
                other.close()
            return original(chat_id, text)

        self.port.send_message = observe_then_send
        self.asked()
        self.replies(2)
        self.assertEqual(seen, [("REQUEST_STARTED", 1)])
        self.assertEqual(self.reply_writes(), 1)

    def test_opt_out_after_claim_before_transport_refuses(self):
        original = outreach.HeyReachActionAdapter.execute_claimed

        def opt_out_then_execute(adapter, database, action_id, config, now=None):
            self.event(self.SLUG, "opt_out", source_id="late-opt-out")
            return original(adapter, database, action_id, config, now=now)

        with patch.object(outreach.HeyReachActionAdapter, "execute_claimed", opt_out_then_execute):
            self.asked()
        self.assertEqual(self.reply_state(), "CANCELLED_BEFORE_SEND")
        self.assertIn("OPTED_OUT", json.loads(self.rows()[0]["result"]))
        self.replies(3)
        self.assertEqual(self.reply_writes(), 0)


class ReplySkippedBeforeTheReplyAI(ReplyBase):
    """Inbound messages the reply AI never answers."""

    def test_message_the_owner_already_answered_by_hand_gets_no_reply(self):
        self.say("Could you tell me more about the role?")
        self.clock.advance(minutes=5)
        self.say("Sure, here is more.", sender=True)
        self.clock.advance(minutes=1)
        self.replies(4)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reply_writes(), 0)

    def test_stranger_who_writes_first_gets_no_reply(self):
        stranger = self.port.add_person(profile("cold-stranger"), distance=1)
        self.port.add_chat(stranger, [(False, "Hi, are you hiring?")])
        self.replies(4)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reply_writes(), 0)


class ReplyExclusionCases:
    """Each change lands after the REPLY action was queued, at the point the subclass names."""

    def change(self, kind):
        if kind == "opt_out":
            self.event(self.SLUG, "opt_out", source_id="late-opt-out")
        elif kind == "closure":
            engagement.close_outreach(self.database, profile(self.SLUG), "Owner closed", self.clock.now)
        elif kind == "alias_suppression":
            old = profile("fu-person-old")
            research.register_alias(self.database, "contact", old, profile(self.SLUG), {"source": "mock-verified-mapping"})
            with self.database:
                self.database.execute("INSERT INTO suppressions VALUES (?,?)", (outreach.profile_key(old), "Owner asked: do not contact"))
        elif kind == "excluded_name":
            self.seed = {"excluded_contact_names": ["Fu Person"]}
        elif kind == "no_automatic":
            self.seed = {"no_automatic_outreach": {"profiles": ["linkedin.com/in/" + self.SLUG]}}
        else:
            raise AssertionError(kind)

    def refused(self, kind):
        landed = []
        original = getattr(reply_execution.Adapter, self.HOOK)

        def hooked(adapter, *args, **kwargs):
            if self.HOOK == "inspect":
                result = original(adapter, *args, **kwargs)
                if not landed:
                    landed.append(kind)
                    self.change(kind)
                return result
            if not landed:
                landed.append(kind)
                self.change(kind)
            return original(adapter, *args, **kwargs)

        with patch.object(reply_execution.Adapter, self.HOOK, hooked):
            self.asked()
        self.assertEqual(landed, [kind], "the change never landed: the reply did not reach " + self.HOOK)
        self.replies(3)
        self.assertEqual(self.reply_writes(), 0)
        self.assertEqual(self.replies_sent(), [])
        result = self.reply_result()
        gates = result.get("provider_guard", {}).get("gates", []) if isinstance(result, dict) else result or []
        self.assertEqual(self.reply_state(), self.REFUSED[kind][0])
        if self.REFUSED[kind][1]:
            self.assertIn(self.REFUSED[kind][1], gates)

    def test_opt_out(self):
        self.refused("opt_out")

    def test_closure(self):
        self.refused("closure")

    def test_suppression_on_a_verified_alias(self):
        self.refused("alias_suppression")

    def test_owner_excluded_name(self):
        self.refused("excluded_name")

    def test_no_automatic_profile(self):
        self.refused("no_automatic")


class ReplyExclusionsAfterInspection(ReplyExclusionCases, ReplyBase):
    """Lands after the send judgment, before the gates and the claim of the same cycle."""
    HOOK = "inspect"
    REFUSED = {"opt_out": ("BLOCKED", "OPTED_OUT"), "closure": ("CLOSED", None),
               "alias_suppression": ("BLOCKED", "SUPPRESSED_CONTACT"), "excluded_name": ("BLOCKED", "EXCLUDED_CONTACT"),
               "no_automatic": ("BLOCKED", "NO_AUTOMATIC_OUTREACH_CONTACT")}


class ReplyExclusionsInTransport(ReplyExclusionCases, ReplyBase):
    """Lands after the claim and REQUEST_STARTED, at the start of the port send (`_send_unipile_reply`):
    only its final stop checks (reply_execution.stop_gates) stand between it and the write."""
    HOOK = "_send_unipile_reply"
    REFUSED = {"opt_out": ("CANCELLED_BEFORE_SEND", "OPTED_OUT"), "closure": ("CANCELLED_BEFORE_SEND", "OUTREACH_CLOSED"),
               "alias_suppression": ("CANCELLED_BEFORE_SEND", "SUPPRESSED_CONTACT"),
               "excluded_name": ("CANCELLED_BEFORE_SEND", "EXCLUDED_CONTACT"),
               "no_automatic": ("CANCELLED_BEFORE_SEND", "EXCLUDED_CONTACT")}


if __name__ == "__main__":
    unittest.main()
