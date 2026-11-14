"""FU1: a follow-up its send judge blocks goes back to its writer, and the rewrite replaces it in place.

Every test drives `followup_execution.cycle`, the function the hourly round calls, with the real
FollowupAdapter, writer step, queue and dispatcher; only the provider and the two models are fakes.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import json
import re
import unittest
from unittest.mock import patch

import approvals
import approvals_page
import engagement
import failures_page
import followup_execution as followup
import manual_followup_e2e as manual
import networking_page
import outreach
import outreach_agent
import overview_page
import workflow_tasks

FIRST = "Following up on my platform note; happy to share relevant details."
CORRECTION = "Rewrite the note so that its last sentence is complete."
DAY = 60 * 24                                # minutes
CODE = re.compile(r"[A-Z]{2,}_[A-Z_]+")      # an internal code name
NO_FIELD = object()                          # a judge answer without block_about
SITUATION = ("BLOCKED", "SITUATION")


def rewrite(number):
    return "Rewritten %d after the objection: following up on my platform note, glad to share details." % number


def moving_clock(moment):
    """A stand-in for a module's `dt` whose datetime.now() is `moment()`; everything else is the real one."""
    class Meta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, dt.datetime)

    class Moving(dt.datetime, metaclass=Meta):
        @classmethod
        def now(cls, tz=None):
            return moment()

    class Clock:
        datetime = Moving

        def __getattr__(self, name):
            return getattr(dt, name)

    return Clock()


class RepairFixture(manual.ProviderFixture):
    def __init__(self):
        super().__init__()
        self.judge = lambda message: "BLOCKED" if message == FIRST else "READY"
        self.findings = {}
        self.writer = None            # (packet, answer) -> answer, for a writer that holds, fails or stops
        self.handbacks, self.judged, self.writer_prompts, self.judge_schemas = [], [], [], []

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "action" not in packet:
            answer = json.loads(super().execute(config, prompt, schema, timeout))
            self.handbacks.append(packet.get("repair_feedback"))
            self.writer_prompts.append(prompt)
            if packet.get("repair_feedback"):
                answer["message"] = rewrite(sum(1 for entry in self.handbacks if entry))
            if self.writer:
                answer = self.writer(packet, answer)
            return json.dumps(answer)
        if packet["phase"] != "INSPECTION":
            return super().execute(config, prompt, schema, timeout)
        verdict = self.judge(packet["action"]["message"])
        # A judge answering (decision, block_about) gives the new field; a bare decision leaves it out,
        # as every answer did before the field existed.
        self.judge_decision, about = verdict if isinstance(verdict, tuple) else (verdict, NO_FIELD)
        self.judged.append((packet["action"]["message"], self.judge_decision))
        self.judge_schemas.append(schema)
        answer = json.loads(super().execute(config, prompt, schema, timeout))
        answer["findings"].update(self.findings)
        if answer["decision"] == "BLOCKED":
            answer.update(reason="TEXT_UNFINISHED: the note ends mid sentence", next_action=CORRECTION)
        if about == "SITUATION":
            answer.update(reason="The earlier message is not confirmed as sent in this read.", next_action="Check again later.")
        if about is not NO_FIELD:
            answer["block_about"] = about
        return json.dumps(answer)

    def round(self, first=True, minutes=0, dispatch=True):
        """One hourly round. `first`: the person's sent first message is the fixture's candidate (the
        CONFIRMED_SENT action production has); otherwise the default candidates, their own sent rows."""
        self.now += dt.timedelta(minutes=minutes)
        self.config["followup_execution"]["dispatch_enabled"] = dispatch
        with patch.object(followup, "_candidates", (lambda database: [self.candidate]) if first else followup._candidates), \
                patch("outreach_provider._now", side_effect=lambda: self.now.isoformat()), \
                patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(self.now if value is None else value)):
            return followup.cycle(manual.ROOT, self.database, self.config, self.adapter, execute=self.execute, now=self.now)

    def round_on_a_moving_clock(self, first=True):
        """As the scheduler runs it: no fixed `now`, and every clock read, in every module, is later than the last."""
        ticks = {"n": 0}

        def moment():
            ticks["n"] += 1
            return self.now + dt.timedelta(seconds=ticks["n"])

        self.config["followup_execution"]["dispatch_enabled"] = True
        with patch.object(followup, "_candidates", (lambda database: [self.candidate]) if first else followup._candidates), \
                patch.object(outreach, "dt", moving_clock(moment)), \
                patch("outreach_provider._now", side_effect=lambda: moment().isoformat()), \
                patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(moment() if value is None else value)):
            return followup.cycle(manual.ROOT, self.database, self.config, self.adapter, execute=self.execute, now=None)

    def task(self, ordinal=1):
        row = self.database.execute("SELECT * FROM workflow_tasks WHERE worker='message_writer' AND "
                                    "json_extract(inputs,'$[0].ordinal')=?", (ordinal,)).fetchone()
        return (row["status"], row["attempts"], row["max_attempts"], row["blocker"])

    def slots(self):
        return [(row["slot"], row["state"], json.loads(row["payload"])["message"])
                for row in self.database.execute("SELECT * FROM outreach_actions ORDER BY slot")]

    def sent(self):
        return [send["message"] for send in self.sends]

    def claimed(self):
        return [json.loads(row[0])["message"] for row in self.database.execute("SELECT payload FROM outreach_action_claims")]


class FollowupRepairTests(unittest.TestCase):
    def setUp(self):
        offline = manual.offline()
        offline.__enter__()
        self.addCleanup(offline.__exit__, None, None, None)
        self.fixture = RepairFixture()
        self.addCleanup(lambda: self.fixture.close())

    def failures(self):
        reader = overview_page.Reader(self.fixture.database)
        return (failures_page.task_failures(reader, self.fixture.now, "%Y-%m-%d %H:%M")
                + failures_page.outreach_failures(reader, self.fixture.now))

    def one_send(self, message):
        """Exactly one follow-up went out for the slot, it is the text that was claimed, and one row holds the slot."""
        fixture = self.fixture
        self.assertEqual((fixture.sent(), fixture.claimed()), ([message], [message]))
        self.assertEqual([(slot, text) for slot, state, text in fixture.slots()], [("FOLLOWUP:1", message)])

    # --- fault (a): the rejected text holds its own slot ---------------------------------------------------
    def test_a_blocked_follow_up_is_rewritten_in_place_judged_and_sent(self):
        result = self.fixture.round()
        self.one_send(rewrite(1))
        self.assertEqual(self.fixture.judged, [(FIRST, "BLOCKED"), (rewrite(1), "READY")])
        self.assertEqual(self.fixture.task(), ("COMPLETE", 2, 3, None))
        self.assertEqual(result["execution"]["items"][0]["repair"]["state"], "RECOMPOSED")
        # The writer got the objection in the existing hand-back shape, for the text that was rejected.
        first, (handback,) = self.fixture.handbacks
        self.assertIsNone(first)
        self.assertEqual({key: handback[key] for key in ("agent_decision", "required_correction", "previous_message", "purpose")},
                         {"agent_decision": "BLOCKED", "required_correction": CORRECTION, "previous_message": FIRST,
                          "purpose": "FOLLOWUP"})
        # The queued rewrite binds to its own writer output and recorded action.
        self.fixture.round(minutes=5)
        self.assertEqual(self.fixture.slots(), [("FOLLOWUP:1", "CONFIRMED_SENT", rewrite(1))])
        self.assertEqual(len(self.fixture.sends), 1)

    # --- fault (c): the rewrite's thread read is later than the instant the judge answered -----------------
    def test_the_rewrite_is_sent_on_a_moving_clock_too(self):
        self.fixture.round_on_a_moving_clock()
        self.one_send(rewrite(1))
        self.assertEqual(self.fixture.task(), ("COMPLETE", 2, 3, None))

    # --- fault (b): a person who already has a send claim --------------------------------------------------
    def test_a_blocked_second_follow_up_is_rewritten_for_a_person_with_a_sent_first(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"
        for minutes in (0, 5, 60):
            fixture.round(minutes=minutes)
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CONFIRMED_SENT", FIRST)])
        fixture.now = engagement.business_days_after(manual.DUE, 5)
        fixture.config["outreach_content_max_age_seconds"] = 14 * 86400
        fixture.judge = lambda message: "READY" if message.startswith("Rewritten") else "BLOCKED"
        fixture.round(first=False)
        self.assertEqual(fixture.sent(), [FIRST, rewrite(1)])
        self.assertEqual((fixture.task(1), fixture.task(2)), (("COMPLETE", 1, 3, None), ("COMPLETE", 2, 3, None)))
        fixture.round(first=False, minutes=5)
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CONFIRMED_SENT", FIRST), ("FOLLOWUP:2", "CONFIRMED_SENT", rewrite(1))])
        # Two follow-ups, never a third: the plan ends the sequence.
        for minutes in (60, 60 * 24 * 10):
            result = fixture.round(first=False, minutes=minutes)
            self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_LIMIT")
        self.assertEqual((len(fixture.sends), len(fixture.claimed())), (2, 2))

    def test_the_second_follow_up_is_not_handed_the_first_ones_objection(self):
        fixture = self.fixture
        for minutes in (0, 5, 60):                    # the first follow-up: rejected once, rewritten, sent
            fixture.round(minutes=minutes)
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CONFIRMED_SENT", rewrite(1))])
        fixture.now = engagement.business_days_after(manual.DUE, 5)
        fixture.config["outreach_content_max_age_seconds"] = 14 * 86400
        fixture.round(first=False)
        self.assertEqual([bool(entry) for entry in fixture.handbacks][:3], [False, True, False])   # a first writing, not a rewrite

    # --- the tries: each rewrite is one try of the writer task; the last rejection ends it ------------------
    def test_a_follow_up_the_judge_keeps_blocking_ends_exhausted_and_failures_says_so(self):
        fixture = self.fixture
        fixture.judge = lambda message: "BLOCKED"
        for minutes in (0, 60, 60):
            fixture.round(minutes=minutes)
        self.assertEqual(fixture.task(), ("EXHAUSTED", 3, 3, "DEADLINE_OR_RETRY_LIMIT"))
        self.assertEqual([bool(entry) for entry in fixture.handbacks], [False, True, True])   # 3 writings in all
        self.assertEqual(fixture.handbacks[2][0]["previous_message"], rewrite(1))            # each answers the newest objection
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "BLOCKED", rewrite(2))])
        judged = len(fixture.judged)
        for minutes in (60, 60 * 24, 60 * 24 * 3):   # and nothing after: no writer call, no judge call, no send
            fixture.round(minutes=minutes)
        self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent(), fixture.claimed()), (3, judged, [], []))
        # Three writings, three verdicts: no text was put to the judge twice (E2).
        self.assertEqual(fixture.judged, [(FIRST, "BLOCKED"), (rewrite(1), "BLOCKED"), (rewrite(2), "BLOCKED")])
        self.assertIn("AGENT_DECISION_NOT_READY", json.loads(fixture.database.execute("SELECT result FROM outreach_actions").fetchone()[0]))
        # Failures lists the exhausted writer task with the three rejections as its causes.
        reader = overview_page.Reader(fixture.database)
        self.assertEqual(failures_page.outreach_failures(reader, fixture.now), [])
        (item,) = failures_page.task_failures(reader, fixture.now, "%Y-%m-%d %H:%M")
        self.assertEqual((item["code"], item["count"]), ("GAVE_UP:message_writer", 3))
        self.assertEqual(item["text"], "Writing a follow-up message kept failing, so Sidedoor stopped trying.")
        # Networking still shows nothing for the unsent follow-up (reported, not changed here).
        page = networking_page.gather(fixture.database, fixture.config, now=fixture.now)
        (contact,) = page["list"]["contacts"]
        self.assertEqual([entry["text"] for entry in contact["conversation"] if entry["who"] == "you"],
                         ["I saw your team's platform work and would like to connect."])

    def test_a_second_rejection_is_repaired_on_the_next_round_and_then_sent(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY" if message == rewrite(2) else "BLOCKED"
        fixture.round()
        self.assertEqual((fixture.sent(), fixture.task()), ([], ("COMPLETE", 2, 3, None)))   # one repair per person per round
        fixture.round(minutes=60)
        self.one_send(rewrite(2))
        self.assertEqual(fixture.task(), ("COMPLETE", 3, 3, None))

    # --- "do not contact" is never a rewrite-and-send loop --------------------------------------------------
    def test_a_block_because_the_contact_replied_ends_with_no_send_when_the_writer_holds(self):
        fixture = self.fixture
        fixture.judge = lambda message: "BLOCKED"
        fixture.findings = {"has_reply": True}
        fixture.writer = lambda packet, answer: dict(answer, decision="HOLD", message=None) if packet.get("repair_feedback") else answer
        result = fixture.round()
        self.assertIn("HAS_REPLY_PRESENT", result["execution"]["items"][0]["gates"])
        self.assertEqual(fixture.task(), ("COMPLETE", 2, 3, None))
        self.assertEqual(json.loads(fixture.database.execute("SELECT result FROM workflow_tasks").fetchone()[0])["decision"], "HOLD")
        for minutes in (60, 60, 60 * 24, 60 * 24 * 7):
            fixture.round(minutes=minutes)
        # The hold ends it: one rewrite turn, the rejected text is never judged again, nothing is claimed or sent.
        self.assertEqual((len(fixture.handbacks), fixture.judged, fixture.sent(), fixture.claimed()),
                         (2, [(FIRST, "BLOCKED")], [], []))
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "BLOCKED", FIRST)])

    def test_a_block_because_the_contact_replied_is_never_sent_even_if_the_writer_rewrites_and_the_judge_relents(self):
        fixture = self.fixture
        fixture.findings = {"has_reply": True}        # the judge passes the rewrites; the reply it reports still refuses
        for minutes in (0, 60, 60, 60, 60 * 24, 60 * 24 * 7):
            fixture.round(minutes=minutes)
        self.assertEqual((fixture.sent(), fixture.claimed()), ([], []))
        self.assertLessEqual(sum(1 for entry in fixture.handbacks if entry), 2)   # bounded by the task's tries

    def test_a_reply_on_record_stops_the_rewrite_before_the_writer(self):
        fixture = self.fixture
        fixture.judge = lambda message: "BLOCKED"
        fixture.after_judge = lambda: fixture.event("inbound")
        for minutes in (0, 60, 60 * 24):
            fixture.round(minutes=minutes)
        self.assertEqual((len(fixture.handbacks), fixture.sent(), fixture.claimed()), (1, [], []))

    # --- no double send across a stop at either point -------------------------------------------------------
    def resume(self):
        """A restart: the interrupted writer try's lease ends, then the next rounds run."""
        self.fixture.restart()
        for minutes in (11, 61):
            self.fixture.round(minutes=minutes)

    def test_a_stop_between_the_reopen_and_the_replace_resumes_the_rewrite_with_its_objection(self):
        fixture = self.fixture

        def stop(packet, answer):
            if packet.get("repair_feedback"):
                raise KeyboardInterrupt
            return answer

        fixture.writer = stop
        with self.assertRaises(KeyboardInterrupt):
            fixture.round()
        self.assertEqual((fixture.sent(), fixture.slots(), fixture.task()[0]), ([], [("FOLLOWUP:1", "PENDING", FIRST)], "RUNNING"))
        fixture.writer = None
        self.resume()
        self.one_send(rewrite(2))                      # the second hand-back: the stopped try was one try
        self.assertEqual(fixture.handbacks[-1][0]["previous_message"], FIRST)   # the stored objection, not a fresh compose
        self.assertEqual(fixture.task(), ("COMPLETE", 3, 3, None))
        self.assertNotIn((FIRST, "READY"), fixture.judged)

    def test_a_stop_between_the_replace_and_the_judgment_leaves_only_the_rewrite(self):
        fixture = self.fixture
        real = outreach._repair_in_run

        def stop(*arguments):
            real(*arguments)                           # the rewrite is swapped in and its task is COMPLETE
            raise KeyboardInterrupt

        with patch.object(outreach, "_repair_in_run", stop):
            with self.assertRaises(KeyboardInterrupt):
                fixture.round()
        self.assertEqual((fixture.sent(), fixture.slots(), fixture.task()),
                         ([], [("FOLLOWUP:1", "PENDING", rewrite(1))], ("COMPLETE", 2, 3, None)))
        self.resume()
        self.one_send(rewrite(1))
        self.assertEqual(len(fixture.handbacks), 2)    # judged as an ordinary queued follow-up: no further writing

    def test_a_claimed_follow_up_is_never_rewritten(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"
        fixture.round()
        self.one_send(FIRST)
        action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        row = fixture.database.execute("SELECT * FROM outreach_actions").fetchone()
        with patch.object(followup, "_candidates", lambda database: [fixture.candidate]):
            self.assertIsNone(outreach._repair_in_run(fixture.database, row, action, {"agent_decision": "BLOCKED", "next_action": "x"},
                                                      fixture.now, lambda *unused: self.fail("repair called"), set()))
            # Even a direct repair call cannot swap a claimed row: the compare-and-swap refuses it.
            followup.repair_rejected(manual.ROOT, fixture.database, fixture.config, fixture.adapter, action, {}, fixture.now,
                                     fixture.execute)
        self.one_send(FIRST)

    def test_a_writer_try_that_fails_keeps_the_rejected_text_unsendable_and_retries_within_the_tries(self):
        fixture = self.fixture
        fixture.writer = lambda packet, answer: "not an answer" if packet.get("repair_feedback") and len(fixture.handbacks) == 2 else answer
        fixture.round()
        self.assertEqual((fixture.sent(), fixture.task()[:3]), ([], ("RETRY_WAIT", 2, 3)))
        fixture.round(minutes=5)                       # the rejected text cannot be judged while its task is open
        self.assertEqual((fixture.sent(), fixture.judged), ([], [(FIRST, "BLOCKED")]))
        fixture.round(minutes=60)                      # the retry: rewritten, its parked inspection starts again, sent
        self.one_send(rewrite(2))
        self.assertEqual(fixture.task(), ("COMPLETE", 3, 3, None))

    # --- upgrade: a follow-up the old code stranded -------------------------------------------------------
    def strand(self):
        """The state 0.4.3 left: the task reopened for good, the rejected text in the slot, its read parked."""
        fixture = self.fixture

        def reopen_only(root, database, config, adapter, action, proof, now=None, execute=None):
            with database:
                database.execute("""UPDATE workflow_tasks SET status='RETRY_WAIT',blocker='FOLLOWUP_JUDGE_REJECTED',
                    next_action='Rewrite',next_at=?,updated_at=? WHERE status='COMPLETE'""", (fixture.now.isoformat(),) * 2)
            return action["action_id"]

        with patch.object(followup, "repair_rejected", reopen_only):
            fixture.round()
        # 0.4.3's next round put the rejected text to the judge's read again, which failed on the open
        # task and parked the read for good.
        with fixture.database:
            fixture.database.execute("UPDATE outreach_actions SET result=?", (json.dumps(["INSPECTION_INVALID_PROOF"]),))
            fixture.database.execute("UPDATE outreach_read_retries SET state='INVALID_PROOF',last_error='MALFORMED_INSPECTION_PROOF'")
        self.assertEqual(fixture.task(), ("RETRY_WAIT", 1, 3, "FOLLOWUP_JUDGE_REJECTED"))
        self.assertEqual(fixture.database.execute("SELECT state,result FROM outreach_actions").fetchone()[:],
                         ("BLOCKED", json.dumps(["INSPECTION_INVALID_PROOF"])))
        self.assertEqual(fixture.database.execute("SELECT state FROM outreach_read_retries").fetchone()[0], "INVALID_PROOF")

    def test_a_stranded_follow_up_resumes_with_its_objection_inside_the_14_days(self):
        for days in (2, 10):
            with self.subTest(days=days):
                self.fixture.close()
                self.fixture = RepairFixture()
                self.strand()
                self.fixture.round(minutes=60 * 24 * days)
                self.one_send(rewrite(1))
                self.assertEqual(self.fixture.handbacks[-1][0]["previous_message"], FIRST)
                self.assertEqual(self.fixture.task(), ("COMPLETE", 2, 3, None))

    def test_a_stranded_follow_up_45_days_late_is_closed_not_rewritten(self):
        fixture = self.fixture
        self.strand()
        calls = (len(fixture.handbacks), len(fixture.judged))
        for minutes in (60 * 24 * 45, 60, 60 * 24):
            result = fixture.round(minutes=minutes)
        self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent(), fixture.claimed()), (*calls, [], []))
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])
        self.assertEqual(result["composition"]["items"][0]["blocker"], "FOLLOWUP_TOO_LATE")
        self.assertEqual(fixture.task(), ("CANCELLED", 1, 3, "FOLLOWUP_TOO_LATE"))
        self.assertEqual(self.failures(), [])

    def test_a_stranded_follow_up_is_not_sent_once_the_plan_no_longer_allows_it(self):
        for name, change in (("a reply on record", lambda fixture: fixture.event("inbound")),
                             ("judge says no longer timely", lambda fixture: setattr(fixture, "judge", lambda message: "BLOCKED"))):
            with self.subTest(name):
                self.fixture.close()
                self.fixture = RepairFixture()
                self.strand()
                change(self.fixture)
                for minutes in (60 * 24 * 10, 60, 60, 60):
                    self.fixture.round(minutes=minutes)
                self.assertEqual((self.fixture.sent(), self.fixture.claimed()), ([], []))


    # === The six fixes the owner picked after review round 3 (2026-11-12) ==================================

    # --- E1: never send a follow-up more than 14 days after it was due --------------------------------------
    def test_a_follow_up_due_13_days_ago_is_composed_and_sent(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"
        fixture.round(minutes=13 * DAY)
        self.one_send(FIRST)

    def test_a_follow_up_due_15_days_ago_is_not_composed_judged_or_sent(self):
        fixture = self.fixture
        for minutes in (15 * DAY, 60, DAY, 30 * DAY):
            result = fixture.round(minutes=minutes)
            item = result["composition"]["items"][0]
            self.assertEqual((item["state"], item["blocker"], item["ordinal"]), ("BLOCKED", "FOLLOWUP_TOO_LATE", 1))
        # No model call of either kind, no task, no message row, nothing on Failures; and with the first
        # never sent the plan never reaches a second follow-up.
        self.assertEqual((fixture.handbacks, fixture.judged, fixture.sent(), fixture.slots()), ([], [], [], []))
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM workflow_tasks").fetchone()[0], 0)
        self.assertEqual(self.failures(), [])

    def test_the_14_days_count_from_when_that_follow_up_became_due(self):
        self.assertEqual(engagement.FOLLOWUP_SEND_WINDOW_DAYS, 14)
        database, due = self.fixture.database, manual.DUE
        state = lambda now: engagement.next_action(database, manual.PROFILE, now).get("blocker")
        self.assertEqual((state(due + dt.timedelta(days=14)), state(due + dt.timedelta(days=14, seconds=1))),
                         (None, "FOLLOWUP_TOO_LATE"))
        # The second follow-up has its own 14 days, from its own due time: the original message is 40 days old here.
        self.fixture.anchor(kind="followup_sent", at=manual.INITIAL + dt.timedelta(days=20), body="First follow-up.")
        second = engagement.business_days_after(manual.INITIAL + dt.timedelta(days=20), 5)
        planned = engagement.next_action(database, manual.PROFILE, second + dt.timedelta(days=13))
        self.assertEqual((planned["state"], planned["ordinal"], planned["due_at"]), ("DUE", 2, second.isoformat()))
        self.assertEqual(state(second + dt.timedelta(days=15)), "FOLLOWUP_TOO_LATE")

    def test_a_second_follow_up_due_15_days_ago_is_dropped(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"
        for minutes in (0, 5, 60):
            fixture.round(minutes=minutes)
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CONFIRMED_SENT", FIRST)])
        calls = (len(fixture.handbacks), len(fixture.judged))
        fixture.now = engagement.business_days_after(manual.DUE, 5) + dt.timedelta(days=15)
        for minutes in (0, 60, DAY):
            result = fixture.round(first=False, minutes=minutes)
            item = result["composition"]["items"][0]
            self.assertEqual((item["blocker"], item["ordinal"]), ("FOLLOWUP_TOO_LATE", 2))
        self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent()), (*calls, [FIRST]))

    def test_the_free_account_note_sent_after_acceptance_counts_from_its_own_due_time(self):
        # Not Premium: the invitation goes without its note and the note is the first message after the
        # acceptance, so the follow-up's due time is already after the acceptance.
        database = self.fixture.database
        profile = "https://www.linkedin.com/in/synthetic-free-invite"
        invited = manual.INITIAL
        accepted, noted = invited + dt.timedelta(days=20), invited + dt.timedelta(days=20, hours=1)
        base = {"source": "unipile", "profile": profile, "verified": True, "evidence": "synthetic"}
        for source_id, kind, at, more in (
                ("invite:free", "initial_sent", invited, {"purpose": "INVITE", "raw": {"note_deferred": True}}),
                ("acceptance:free", "accepted", accepted, {"raw": {}}),
                ("note:free", "initial_sent", noted, {"purpose": "FIRST_MESSAGE", "raw": {}})):
            engagement.record_event(database, dict(base, source_id=source_id, kind=kind, occurred_at=at.isoformat(),
                                                   observed_at=at.isoformat(), **more))
        database.commit()
        due = engagement.business_days_after(noted, 3)
        planned = lambda now: engagement.next_action(database, profile, now)
        self.assertEqual((planned(due)["state"], planned(due)["due_at"]), ("DUE", due.isoformat()))
        self.assertEqual((planned(due + dt.timedelta(days=14))["state"], planned(due + dt.timedelta(days=14, seconds=1))["blocker"]),
                         ("DUE", "FOLLOWUP_TOO_LATE"))

    def test_a_follow_up_queued_inside_the_14_days_is_not_sent_after_them(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"
        fixture.round(minutes=13 * DAY, dispatch=False)                     # written and queued on day 13, not sent
        self.assertEqual((fixture.slots(), fixture.judged), ([("FOLLOWUP:1", "PENDING", FIRST)], []))
        action = json.loads(fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        late = fixture.now + dt.timedelta(days=2)
        # The guard behind the close: the plan refuses this exact action at every gate on day 15.
        self.assertIn("FOLLOWUP_TOO_LATE", outreach.action_gates(fixture.database, fixture.config, action, {}, late))
        self.assertTrue(followup.binding_gates(fixture.database, fixture.config, action, late))
        for minutes in (2 * DAY, 60, DAY):
            fixture.round(minutes=minutes)
        self.assertEqual((fixture.judged, fixture.sent(), fixture.claimed()), ([], [], []))
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])
        closed = json.loads(fixture.database.execute("SELECT result FROM outreach_actions").fetchone()[0])["closed_before_send"]
        self.assertEqual(closed["reason"], "FOLLOWUP_TOO_LATE")
        self.assertEqual((len(fixture.handbacks), self.failures()), (1, []))

    def test_a_late_follow_up_waiting_for_approval_leaves_approvals(self):
        fixture = self.fixture
        fixture.round(minutes=13 * DAY, dispatch=False)
        with fixture.database:
            fixture.database.execute("UPDATE outreach_actions SET state='AWAITING_APPROVAL'")
        self.assertEqual(approvals.close_stopped(fixture.database, fixture.now + dt.timedelta(days=1)), [])
        (closed,) = approvals.close_stopped(fixture.database, fixture.now + dt.timedelta(days=2))
        state, result = fixture.database.execute("SELECT state,result FROM outreach_actions WHERE action_id=?", (closed,)).fetchone()
        self.assertEqual((state, json.loads(result)["closed_while_awaiting_approval"]["reason"]), ("CLOSED", "FOLLOWUP_TOO_LATE"))
        self.assertNotRegex(approvals_page.GATE_WORDS["FOLLOWUP_TOO_LATE"], CODE)

    # --- E2: a text is judged once; after a block only a rewrite is judged ---------------------------------
    def test_a_text_the_judge_blocked_is_never_put_to_the_judge_again(self):
        fixture = self.fixture
        verdicts = iter(("BLOCKED", "BLOCKED"))
        fixture.judge = lambda message: next(verdicts, "READY")             # after two blocks it would pass anything
        fixture.round()
        self.assertEqual((fixture.judged, fixture.sent()), ([(FIRST, "BLOCKED"), (rewrite(1), "BLOCKED")], []))
        fixture.round(minutes=60)
        # The second text is not judged again (it would have passed and been sent): a third writing is, once.
        self.assertEqual(fixture.judged, [(FIRST, "BLOCKED"), (rewrite(1), "BLOCKED"), (rewrite(2), "READY")])
        self.one_send(rewrite(2))
        self.assertEqual((len(fixture.handbacks), fixture.task()), (3, ("COMPLETE", 3, 3, None)))
        self.assertEqual(self.failures(), [])

    def test_a_writer_that_repeats_the_blocked_text_does_not_get_it_judged_again(self):
        fixture = self.fixture
        verdicts = iter(("BLOCKED",))
        fixture.judge = lambda message: next(verdicts, "READY")
        fixture.writer = lambda packet, answer: dict(answer, message=FIRST) if answer.get("decision") == "COMPOSE" else answer
        for minutes in (0, 60, 60, 60, DAY):
            fixture.round(minutes=minutes)
        self.assertEqual((fixture.judged, fixture.sent(), fixture.claimed()), ([(FIRST, "BLOCKED")], [], []))
        self.assertLessEqual(len(fixture.handbacks), 3)

    def test_a_judgment_that_was_interrupted_is_made_again(self):
        fixture = self.fixture
        tries = []

        def judge(message):
            tries.append(message)
            if len(tries) == 1:
                raise TimeoutError("synthetic judge timeout")               # no verdict: the read waits and comes back
            return "READY"

        fixture.judge = judge
        fixture.round()
        self.assertEqual((fixture.judged, fixture.sent()), ([], []))
        self.assertFalse(outreach_agent.judge_blocked_text(fixture.database, json.loads(
            fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])))
        fixture.round(minutes=16)
        self.assertEqual((tries, fixture.judged), ([FIRST, FIRST], [(FIRST, "READY")]))
        self.one_send(FIRST)

    # --- E3: the writer is told why, and that it may hold -------------------------------------------------
    def test_the_writer_reads_the_judges_reason_and_the_hold_instruction(self):
        fixture = self.fixture
        fixture.round()
        self.one_send(rewrite(1))
        instructions, source = fixture.writer_prompts[1].split("UNTRUSTED_SOURCE_JSON:\n", 1)
        (handback,) = json.loads(source)["repair_feedback"]
        self.assertEqual((handback["rejection_reason"], handback["required_correction"], handback["previous_message"]),
                         ("TEXT_UNFINISHED: the note ends mid sentence", CORRECTION, FIRST))
        self.assertIn("When repair_feedback is present", instructions)
        self.assertIn("return HOLD", instructions)
        self.assertNotIn("FOLLOWUP_SEND_JUDGE", instructions)
        writer, judge = (manual.ROOT / "followup-prompt.md").read_text().split("\nFOLLOWUP_SEND_JUDGE:\n", 1)
        self.assertNotIn("repair_feedback", judge)                          # no stray copy addressed to the writer
        paragraph = writer[writer.index("When repair_feedback is present"):].split("\n\n", 1)[0]
        self.assertNotRegex(paragraph, "[\u2013\u2014]")

    def test_a_proof_saved_before_the_reason_was_carried_still_reads_unspecified(self):
        old = {"agent_decision": "BLOCKED", "next_action": CORRECTION}
        self.assertEqual(outreach_agent._judge_text_rejection(old)["rejection_reason"], "UNSPECIFIED")
        self.assertEqual(outreach_agent._judge_text_rejection(dict(old, agent_reason=None))["rejection_reason"], "UNSPECIFIED")

    # --- E4: a writing task nobody needs any more leaves Failures -----------------------------------------
    def test_a_rejected_follow_up_whose_contact_then_replies_is_not_left_on_failures(self):
        fixture = self.fixture
        fixture.judge = lambda message: "BLOCKED"
        fixture.after_judge = lambda: fixture.event("inbound")
        for minutes in (0, 60, DAY, 7 * DAY):
            fixture.round(minutes=minutes)
            self.assertEqual(self.failures(), [])
        status, attempts, unused, blocker = fixture.task()
        self.assertEqual((status, attempts), ("CANCELLED", 1))
        self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent(), fixture.claimed()), (1, 1, [], []))

    def test_a_wait_that_clears_by_itself_keeps_the_writing_task(self):
        fixture = self.fixture
        self.strand()
        waiting = {"state": "BLOCKED", "blocker": "OBSERVED_OUTBOUND_REQUIRES_PURPOSE_RECONCILIATION"}
        with patch.object(followup, "plan", lambda *arguments, **keywords: waiting):
            fixture.round(minutes=60)
        self.assertEqual(fixture.task()[0], "RETRY_WAIT")
        fixture.round(minutes=60)
        self.one_send(rewrite(1))

    def test_an_ordinary_rejection_that_is_rewritten_and_sent_shows_nothing(self):
        self.fixture.round()
        self.one_send(rewrite(1))
        self.assertEqual(self.failures(), [])

    # --- E5: plain words for an exhausted follow-up ------------------------------------------------------
    def test_failures_gives_plain_words_for_a_follow_up_that_kept_being_rejected(self):
        fixture = self.fixture
        fixture.judge = lambda message: "BLOCKED"
        for minutes in (0, 60, 60):
            fixture.round(minutes=minutes)
        (item,) = self.failures()
        (reason,) = item["reasons"]
        self.assertEqual((reason["code"], reason["count"]), ("FOLLOWUP_JUDGE_REJECTED", 3))
        self.assertEqual(reason["words"], "Sidedoor's check rejected the follow-up before it was sent. Nothing went out.")
        for words in (item["text"], item["todo"], reason["words"]):
            self.assertNotRegex(words, CODE)
            self.assertNotRegex(words.casefold(), "judge|task|automation|[\u2013\u2014]")

    # --- E6: a rewrite whose old read had spent its tries is still judged ----------------------------------
    def test_a_rewrite_is_judged_even_when_the_parked_read_had_three_tries(self):
        fixture = self.fixture
        self.strand()
        with fixture.database:
            fixture.database.execute("UPDATE outreach_read_retries SET attempts=3")
        fixture.round(minutes=2 * DAY)
        self.one_send(rewrite(1))
        self.assertEqual(fixture.judged[-1], (rewrite(1), "READY"))


class _UnacceptedInvitation(manual.CampaignFollowupFixture):
    def __init__(self):
        super().__init__(accepted=False)


class InviteFixture(RepairFixture, _UnacceptedInvitation):
    """A note sent with the invitation (a HeyReach campaign invitation), not accepted yet. The hourly
    round runs every six hours on a moving clock; days are counted from the invitation."""

    def __init__(self):
        super().__init__()
        self.judge = lambda message: "READY"
        self.writer = lambda packet, answer: dict(answer, message="Follow-up %d: glad to share details." % packet["plan"]["ordinal"])
        self.config["outreach_content_max_age_seconds"] = 14 * 86400
        self.accepted_in_provider = False
        self.now = self.initial + dt.timedelta(hours=1)
        self.sent_on, self.reads_on, self.blockers = [], [], set()

    def day(self):
        return round((self.now - self.initial) / dt.timedelta(days=1), 2)

    def _request(self, operation, arguments):
        # LinkedIn stamps the message when it lands, which on this moving clock is after its claim.
        result = super()._request(operation, arguments)
        self.room["messages"][-1]["createdAt"] = (self.now + dt.timedelta(minutes=5)).isoformat()
        return result

    def accept(self, day):
        """Sidedoor records the acceptance at this moment (the record its own acceptance read writes)."""
        at = self.initial + dt.timedelta(days=day)
        engagement.record_event(self.database, {"source": "mock-authenticated-acceptance", "source_id": "accepted-201",
            "profile": self.candidate["profile"], "kind": "accepted", "verified": True, "occurred_at": at.isoformat(),
            "observed_at": at.isoformat(), "evidence": "mock:independently-observed-acceptance", "raw": {"accepted": True}})
        self.database.commit()
        self.accepted_in_provider = True

    def live(self, until, *, accepted_on_linkedin=None, seen=None, off=None):
        """Six-hourly rounds up to day `until`. `accepted_on_linkedin`: LinkedIn shows the acceptance from
        that day and Sidedoor learns of it at its next acceptance read. `seen`: Sidedoor records it on that
        day. `off`: (from, to) days with no rounds."""
        while self.day() + 0.25 <= until:
            self.now += dt.timedelta(hours=6)
            if accepted_on_linkedin is not None and self.day() >= accepted_on_linkedin:
                self.accepted_in_provider = True
            if seen is not None and self.day() >= seen and not self.accepted_in_provider:
                self.accept(seen)
            if off and off[0] <= self.day() < off[1]:
                continue
            sends, reads = len(self.sends), self.provider_reads.count("get_overall_stats")
            result = self.round_on_a_moving_clock(first=False)
            self.sent_on += [self.day()] * (len(self.sends) - sends)
            self.reads_on += [self.day()] * (self.provider_reads.count("get_overall_stats") - reads)
            self.blockers |= {item.get("blocker") for item in result["composition"]["items"]}


# What the released code (main, a2e981b) did in the same six-hourly rounds, from the side-by-side runs of
# 2026-11-12: the days its two follow-ups went out, and the days it read the acceptance.
MAIN_SENDS = {("linkedin", 2): [5.04, 12.04], ("linkedin", 10): [11.29, 18.3], ("linkedin", 16): [16.79, 21.8],
              ("linkedin", 17): [23.79, 28.8], ("seen", 20): [20.04, 27.05], ("seen", 30): [30.05, 36.05]}
MAIN_READS_NEVER_ACCEPTED = [5.04, 5.29, 5.54, 5.79, 6.29, 7.04, 8.54, 11.29, 16.79, 23.79, 30.8, 37.8]
TICK = 0.26


class InvitationWindowTests(unittest.TestCase):
    """H1 (owner, 2026-11-12): "for an invitation, count the 14 days from when they accept." """

    def setUp(self):
        offline = manual.offline()
        offline.__enter__()
        self.addCleanup(offline.__exit__, None, None, None)
        self.fixture = InviteFixture()
        self.addCleanup(lambda: self.fixture.close())

    def as_main(self, how, day):
        fixture = self.fixture
        self.assertEqual(len(fixture.sent_on), 2, (how, day, fixture.sent_on, fixture.blockers))
        for ours, mains in zip(fixture.sent_on, MAIN_SENDS[(how, day)]):
            self.assertLessEqual(abs(ours - mains), TICK, (how, day, fixture.sent_on))
        self.assertEqual(fixture.sent(), ["Follow-up 1: glad to share details.", "Follow-up 2: glad to share details."])
        self.assertNotIn("FOLLOWUP_TOO_LATE", fixture.blockers)
        self.assertEqual(len(fixture.claimed()), 3)          # the invitation and one claim per follow-up

    def test_an_acceptance_before_or_soon_after_the_due_time_sends_both_follow_ups_as_before(self):
        for day in (2, 10, 16):
            with self.subTest(day=day):
                self.fixture.close()
                self.fixture = InviteFixture()
                self.fixture.live(40, accepted_on_linkedin=day)
                self.as_main("linkedin", day)

    def test_an_acceptance_inside_the_read_back_off_gap_still_gets_its_follow_up(self):
        self.fixture.live(40, accepted_on_linkedin=17)       # 12 days after the follow-up became due; next read on day 23.79
        self.as_main("linkedin", 17)

    def test_an_acceptance_on_day_20_or_30_sends_both_follow_ups(self):
        for day in (20, 30):
            with self.subTest(day=day):
                self.fixture.close()
                self.fixture = InviteFixture()
                self.fixture.live(41, seen=day)
                self.as_main("seen", day)

    def test_14_days_after_the_acceptance_the_follow_up_is_dropped(self):
        fixture = self.fixture
        fixture.live(41, seen=20, off=(19.9, 40))           # accepted on day 20, Sidedoor off until day 40
        self.assertEqual((fixture.sent(), fixture.handbacks, fixture.judged), ([], [], []))
        self.assertIn("FOLLOWUP_TOO_LATE", fixture.blockers)
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM workflow_tasks WHERE worker='message_writer'").fetchone()[0], 0)

    def test_an_invitation_nobody_accepted_is_read_as_before_and_is_never_too_late(self):
        fixture = self.fixture
        fixture.live(40)
        self.assertEqual(len(fixture.reads_on), len(MAIN_READS_NEVER_ACCEPTED))
        for ours, mains in zip(fixture.reads_on, MAIN_READS_NEVER_ACCEPTED):
            self.assertAlmostEqual(ours, mains, delta=0.02)
        self.assertEqual((fixture.blockers, fixture.sent(), fixture.handbacks), ({None}, [], []))
        planned = followup.plan(fixture.database, fixture.candidate, fixture.now)
        self.assertEqual((planned["state"], planned.get("blocker")), ("DUE", None))

    def test_the_window_starts_at_the_later_of_the_due_time_and_the_acceptance(self):
        fixture = self.fixture
        due = engagement.business_days_after(fixture.initial, 3)
        blocker = lambda now: engagement.next_action(fixture.database, fixture.candidate["profile"], now).get("blocker")
        self.assertIsNone(blocker(due + dt.timedelta(days=200)))                    # not accepted: the plan as it was
        fixture.accept(1)                                                           # accepted before it became due
        self.assertEqual((blocker(due + dt.timedelta(days=14)), blocker(due + dt.timedelta(days=14, seconds=1))),
                         (None, "FOLLOWUP_TOO_LATE"))

    def test_an_acceptance_after_the_due_time_starts_the_window(self):
        fixture = self.fixture
        fixture.accept(20)
        seen = fixture.initial + dt.timedelta(days=20)
        planned = lambda now: engagement.next_action(fixture.database, fixture.candidate["profile"], now)
        self.assertEqual(planned(seen + dt.timedelta(days=14))["state"], "DUE")
        self.assertEqual(planned(seen + dt.timedelta(days=14))["due_at"], engagement.business_days_after(fixture.initial, 3).isoformat())
        self.assertEqual(planned(seen + dt.timedelta(days=14, seconds=1))["blocker"], "FOLLOWUP_TOO_LATE")


class ExclusionLiftedTests(unittest.TestCase):
    """H3: a person excluded or suppressed while a rejected follow-up waits for its rewrite keeps the
    writing task, so the rewrite resumes if the user lifts it."""

    def setUp(self):
        offline = manual.offline()
        offline.__enter__()
        self.addCleanup(offline.__exit__, None, None, None)

    def waiting_for_its_rewrite(self):
        """The judge rejected the follow-up and the first rewrite try failed: the task waits for another try."""
        fixture = RepairFixture()
        self.addCleanup(fixture.close)
        fixture.writer = lambda packet, answer: "not an answer" if packet.get("repair_feedback") and len(fixture.handbacks) == 2 else answer
        fixture.round()
        self.assertEqual((fixture.task()[:3], fixture.slots()), (("RETRY_WAIT", 2, 3), [("FOLLOWUP:1", "BLOCKED", FIRST)]))
        fixture.writer = None                                              # from here the writer always composes
        return fixture

    def stops(self, fixture):
        """(the plan's code, how it is applied, how it is lifted) for an exclusion and for a suppression."""
        database = fixture.database
        excluded = patch.object(followup.owner_seed, "excluded_names", lambda: {followup.name_key("Synthetic Person")})

        def suppress():
            database.execute("INSERT INTO suppressions VALUES (?,?)", (followup.profile_key(manual.PROFILE), "User asked: do not contact"))
            database.commit()

        def release():
            database.execute("DELETE FROM suppressions")
            database.commit()

        return {"EXCLUDED_CONTACT": (excluded.start, excluded.stop), "SUPPRESSED_CONTACT": (suppress, release)}

    def failures(self, fixture):
        reader = overview_page.Reader(fixture.database)
        return (failures_page.task_failures(reader, fixture.now, "%Y-%m-%d %H:%M") + failures_page.outreach_failures(reader, fixture.now))

    def each(self):
        for code in ("EXCLUDED_CONTACT", "SUPPRESSED_CONTACT"):
            with self.subTest(code):
                fixture = self.waiting_for_its_rewrite()
                apply, lift = self.stops(fixture)[code]
                apply()
                self.addCleanup(lift)
                yield code, fixture, lift

    def test_while_excluded_nothing_is_written_judged_or_sent(self):
        for code, fixture, lift in self.each():
            calls = (len(fixture.handbacks), len(fixture.judged))
            for minutes in (60, 60, DAY, 3 * DAY):
                result = fixture.round(minutes=minutes)
                self.assertEqual(result["composition"]["items"][0]["blocker"], code)
            self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent(), fixture.claimed()), (*calls, [], []))
            self.assertEqual(fixture.task()[:3], ("RETRY_WAIT", 2, 3))    # kept, not cancelled
            lift()

    def test_lifted_inside_the_14_days_the_rewrite_is_written_once_and_sent_once(self):
        for code, fixture, lift in self.each():
            for minutes in (60, 2 * DAY):
                fixture.round(minutes=minutes)
            lift()
            for minutes in (60, 60, DAY):
                fixture.round(minutes=minutes)
            self.assertEqual((fixture.sent(), fixture.claimed()), ([rewrite(2)], [rewrite(2)]))
            self.assertEqual((len(fixture.handbacks), fixture.task()), (3, ("COMPLETE", 3, 3, None)))
            self.assertEqual(self.failures(fixture), [])

    def test_lifted_after_the_14_days_it_is_closed_quietly_and_nothing_is_sent(self):
        for code, fixture, lift in self.each():
            fixture.round(minutes=16 * DAY)
            lift()
            for minutes in (60, 60, DAY):
                fixture.round(minutes=minutes)
            self.assertEqual((len(fixture.handbacks), fixture.sent(), fixture.claimed()), (2, [], []))
            self.assertEqual((fixture.slots(), fixture.task()[0], fixture.task()[3]),
                             ([("FOLLOWUP:1", "CLOSED", FIRST)], "CANCELLED", "FOLLOWUP_TOO_LATE"))
            self.assertEqual(self.failures(fixture), [])

    def test_never_lifted_nothing_is_ever_sent_and_failures_lists_it_only_inside_its_14_day_window(self):
        for code, fixture, lift in self.each():
            fixture.round(minutes=DAY)
            # Reported, not changed here: while the task waits, Failures words it as any writing that will
            # be tried again, for as long as its last failed try is inside the page's 14-day window.
            (item,) = self.failures(fixture)
            self.assertEqual(item["text"], "Writing a follow-up message didn't work.")
            for minutes in (13 * DAY, DAY, 30 * DAY):
                fixture.round(minutes=minutes)
            self.assertEqual(self.failures(fixture), [])
            self.assertEqual((len(fixture.handbacks), len(fixture.judged), fixture.sent(), fixture.claimed()), (2, 1, [], []))
            self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])   # the 14-day rule ends the queued text
            lift()


class SituationBlockTests(unittest.TestCase):
    """H2 (owner, 2026-11-12): "The judge says which kind of block it is ... A situation block uses no
    writing. The same text waits and is checked again later, until it passes or the 14 days run out." """

    def setUp(self):
        offline = manual.offline()
        offline.__enter__()
        self.addCleanup(offline.__exit__, None, None, None)
        self.fixture = RepairFixture()
        self.addCleanup(lambda: self.fixture.close())
        self.start = self.fixture.now

    def hours(self, count, step=1):
        """Hourly rounds, as the scheduler runs them, on a moving clock."""
        for unused in range(count):
            self.fixture.now += dt.timedelta(hours=step)
            self.fixture.round_on_a_moving_clock()

    def clears_after(self, hours):
        fixture = self.fixture
        fixture.judge = lambda message: "READY" if fixture.now >= self.start + dt.timedelta(hours=hours) else SITUATION

    def quiet(self):
        """No writing was used, nothing was recorded as a failed try or an objection, nothing is on Failures."""
        fixture, reader = self.fixture, overview_page.Reader(self.fixture.database)
        self.assertEqual(failures_page.task_failures(reader, fixture.now, "%Y-%m-%d %H:%M")
                         + failures_page.outreach_failures(reader, fixture.now), [])
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM workflow_task_events WHERE event='RETRY_WAIT'").fetchone()[0], 0)
        self.assertEqual(fixture.database.execute("SELECT count(*) FROM outreach_agent_audit WHERE kind='JUDGE_OBJECTION'").fetchone()[0], 0)
        self.assertEqual((len(fixture.handbacks), fixture.task()), (1, ("COMPLETE", 1, 3, None)))

    def every_5_minutes(self, hours):
        """Rounds every 5 minutes; returns the hours (after the first verdict) at which the judge was asked."""
        fixture, asked = self.fixture, []
        for unused in range(hours * 12):
            fixture.now += dt.timedelta(minutes=5)
            before = len(fixture.judged)
            fixture.round_on_a_moving_clock()
            if len(fixture.judged) > before:
                asked.append(round((fixture.now - self.start) / dt.timedelta(hours=1)))
        return asked

    def due_line(self):
        """The person's sequence line on the Networking page."""
        page = networking_page.gather(self.fixture.database, self.fixture.config, now=self.fixture.now)
        (contact,) = page["list"]["contacts"]
        return contact["sequence"] or ""

    def test_a_situation_that_clears_is_sent_once_with_one_writing(self):
        for hours, asked, verdicts in ((1, [1], 2), (5, [1, 2, 6], 4), (26, [1, 2, 6, 18, 30], 6)):
            with self.subTest(clears_after_hours=hours):
                self.fixture.close()
                self.fixture = RepairFixture()
                self.start = self.fixture.now
                self.clears_after(hours)
                self.fixture.round_on_a_moving_clock()
                self.assertEqual((self.fixture.judged, self.fixture.sent()), ([(FIRST, "BLOCKED")], []))
                self.assertIn("Follow-up due", self.due_line())            # waiting inside its six checks
                self.assertEqual(self.every_5_minutes(48), asked)        # hour 5: sent at the hour-6 check
                self.assertEqual((self.fixture.sent(), self.fixture.claimed()), ([FIRST], [FIRST]))
                self.assertEqual(self.fixture.judged, [(FIRST, "BLOCKED")] * (verdicts - 1) + [(FIRST, "READY")])
                self.quiet()

    def test_the_sixth_situation_verdict_closes_it_and_the_judge_is_never_asked_a_seventh_time(self):
        fixture = self.fixture
        fixture.judge = lambda message: SITUATION
        fixture.round_on_a_moving_clock()
        self.assertEqual(self.every_5_minutes(29), [1, 2, 6, 18])
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "BLOCKED", FIRST)])
        self.assertIn("Follow-up due", self.due_line())
        fixture.restart()                                                  # the count is the saved verdicts: a restart keeps it
        self.assertEqual(self.every_5_minutes(2), [30])                  # six in all: hours 0, 1, 2, 6, 18, 30
        self.assertEqual(outreach.SITUATION_BLOCK_LIMIT, 6)
        self.assertEqual((fixture.sent(), fixture.claimed(), fixture.slots()), ([], [], [("FOLLOWUP:1", "CLOSED", FIRST)]))
        closed = json.loads(fixture.database.execute("SELECT result FROM outreach_actions").fetchone()[0])["closed_before_send"]
        self.assertEqual(closed["reason"], "FOLLOWUP_SITUATION_NOT_CLEARED")
        # The plan still answers DUE until day 14; the page no longer says a follow-up is due.
        self.assertEqual(followup.plan(fixture.database, fixture.candidate, fixture.now)["state"], "DUE")
        self.assertNotIn("Follow-up due", self.due_line())
        fixture.judge = lambda message: "READY"                            # even a judge that would now pass it
        self.assertEqual(self.every_5_minutes(24 * 3), [])
        self.hours(20, step=24)
        self.assertEqual((len(fixture.judged), fixture.sent(), fixture.claimed()), (6, [], []))
        self.assertNotIn("Follow-up due", self.due_line())
        self.quiet()

    def closed_after_six(self):
        fixture = self.fixture
        fixture.judge = lambda message: "READY"                            # a seventh answer would pass it and send
        self.every_5_minutes(60)
        self.hours(5, step=24)
        self.assertEqual((len(fixture.judged), fixture.sent(), fixture.claimed()), (6, [], []))
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])
        closed = json.loads(fixture.database.execute("SELECT result FROM outreach_actions").fetchone()[0])["closed_before_send"]
        self.assertEqual(closed["reason"], "FOLLOWUP_SITUATION_NOT_CLEARED")
        self.quiet()

    def test_a_lost_read_finish_on_the_sixth_situation_verdict_never_gets_a_seventh_judge_call(self):
        fixture = self.fixture
        fixture.judge = lambda message: SITUATION
        fixture.round_on_a_moving_clock()
        real = outreach._finish_read
        lost = lambda *arguments: False if len(fixture.judged) == 6 else real(*arguments)   # the lease was lost
        with patch.object(outreach, "_finish_read", lost):
            self.every_5_minutes(31)
        self.assertEqual(len(fixture.judged), 6)
        self.closed_after_six()

    def test_a_stop_right_after_the_sixth_situation_verdict_was_saved_never_gets_a_seventh_judge_call(self):
        fixture = self.fixture
        fixture.judge = lambda message: SITUATION
        fixture.round_on_a_moving_clock()

        def stop(*arguments):
            if len(fixture.judged) == 6:
                raise KeyboardInterrupt                                    # the verdict is saved; nothing after it ran
            return real(*arguments)

        real = outreach.action_gates
        with patch.object(outreach, "action_gates", stop):
            with self.assertRaises(KeyboardInterrupt):
                self.every_5_minutes(31)
        self.assertEqual((len(fixture.judged), fixture.slots()[0][1]), (6, "BLOCKED"))
        fixture.restart()
        self.closed_after_six()

    def test_a_situation_that_clears_at_hour_40_is_not_sent(self):
        fixture = self.fixture
        self.clears_after(40)
        fixture.round_on_a_moving_clock()
        self.every_5_minutes(72)
        self.assertEqual((fixture.judged, fixture.sent(), fixture.claimed()), ([(FIRST, "BLOCKED")] * 6, [], []))
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])
        self.quiet()

    def test_rechecks_are_at_least_an_hour_apart_and_back_off(self):
        fixture = self.fixture
        fixture.judge = lambda message: SITUATION
        fixture.round_on_a_moving_clock()
        asked = self.every_5_minutes(72)
        self.assertEqual([later - earlier for earlier, later in zip([0] + asked, asked)], [1, 1, 4, 12, 12])

    def test_the_due_line_goes_only_when_the_slot_was_closed_before_sending(self):
        fixture = self.fixture
        self.assertIn("Follow-up due", self.due_line())                    # genuinely due, nothing written yet
        fixture.judge = lambda message: "READY"
        fixture.now += dt.timedelta(days=13)
        fixture.config["followup_execution"]["dispatch_enabled"] = False
        fixture.round(dispatch=False)                                      # queued on day 13, not sent
        self.assertIn("Follow-up due", self.due_line())
        fixture.round(minutes=2 * DAY)                                     # day 15: closed, past its 14 days
        self.assertEqual(fixture.slots(), [("FOLLOWUP:1", "CLOSED", FIRST)])
        self.assertNotIn("Follow-up due", self.due_line())

    def test_a_situation_block_then_a_message_block_is_repaired_as_usual(self):
        fixture = self.fixture
        verdicts = iter((SITUATION, ("BLOCKED", "MESSAGE")))
        fixture.judge = lambda message: next(verdicts, "READY")
        fixture.round_on_a_moving_clock()
        self.hours(3)
        self.assertEqual(fixture.judged, [(FIRST, "BLOCKED"), (FIRST, "BLOCKED"), (rewrite(1), "READY")])
        self.assertEqual((fixture.sent(), fixture.claimed(), fixture.task()), ([rewrite(1)], [rewrite(1)], ("COMPLETE", 2, 3, None)))

    def test_a_situation_block_on_a_rewrite_uses_no_further_writing(self):
        fixture = self.fixture
        verdicts = iter((("BLOCKED", "MESSAGE"), SITUATION, SITUATION))
        fixture.judge = lambda message: next(verdicts, "READY")
        fixture.round_on_a_moving_clock()
        self.assertEqual((fixture.judged, fixture.task()), ([(FIRST, "BLOCKED"), (rewrite(1), "BLOCKED")], ("COMPLETE", 2, 3, None)))
        self.hours(4)
        self.assertEqual(fixture.judged[2:], [(rewrite(1), "BLOCKED"), (rewrite(1), "READY")])
        self.assertEqual((fixture.sent(), fixture.claimed(), fixture.task()), ([rewrite(1)], [rewrite(1)], ("COMPLETE", 2, 3, None)))

    def test_a_reply_while_waiting_on_a_situation_ends_it_at_once(self):
        fixture = self.fixture
        fixture.judge = lambda message: SITUATION
        fixture.round_on_a_moving_clock()
        fixture.event("inbound")
        fixture.judge = lambda message: "READY"                            # even a judge that would now pass it
        self.hours(30)
        self.hours(5, step=24)
        self.assertEqual((fixture.judged, fixture.sent(), fixture.claimed()), ([(FIRST, "BLOCKED")], [], []))
        self.quiet()

    def test_a_block_without_the_field_or_with_null_is_a_message_block(self):
        for answer in ("BLOCKED", ("BLOCKED", None)):
            with self.subTest(answer=answer):
                self.fixture.close()
                self.fixture = RepairFixture()
                verdicts = iter((answer,))
                self.fixture.judge = lambda message: next(verdicts, "READY")
                self.fixture.round_on_a_moving_clock()
                self.assertEqual(self.fixture.judged, [(FIRST, "BLOCKED"), (rewrite(1), "READY")])
                self.assertEqual(self.fixture.sent(), [rewrite(1)])

    def test_only_the_follow_up_judge_gets_the_field_and_only_the_enum_is_read(self):
        fixture = self.fixture
        fixture.judge = lambda message: ("READY", None)
        fixture.round_on_a_moving_clock()
        self.assertEqual(fixture.sent(), [FIRST])
        own, shared = (json.loads((manual.ROOT / name).read_text()) for name in ("followup-judge-schema.json", "outreach-agent-schema.json"))
        self.assertEqual(json.loads(fixture.judge_schemas[0]), own)
        self.assertNotIn("block_about", shared["properties"])
        self.assertEqual(own["properties"]["block_about"], {"type": ["string", "null"], "enum": ["MESSAGE", "SITUATION", None]})
        self.assertEqual({key: value for key, value in own["properties"].items() if key != "block_about"}, shared["properties"])
        self.assertEqual(own["required"], shared["required"] + ["block_about"])
        packet = {"any": "packet"}
        answer = {"output_version": outreach_agent.VERSION, "source_sha256": outreach_agent.digest(packet), "decision": "BLOCKED",
                  "reason": "The situation will clear by itself, wait for it.", "next_action": "x", "conversation_id": None,
                  "receipt_observation": None, "receipt_message": None, "route": "EXISTING_THREAD",
                  "findings": {"prior_outreach": True, "has_reply": False, "negative_reply": False, "opt_out": False}}
        about = lambda **changes: followup._validate_judgment(json.dumps(dict(answer, **changes)), packet)["block_about"]
        self.assertEqual((about(), about(block_about=None), about(block_about="MESSAGE"), about(block_about="SITUATION")),
                         ("MESSAGE", "MESSAGE", "MESSAGE", "SITUATION"))   # the wording of the reason decides nothing
        self.assertIsNone(about(decision="READY", block_about="SITUATION"))
        judge = (manual.ROOT / "followup-prompt.md").read_text().split("\nFOLLOWUP_SEND_JUDGE:\n", 1)[1]
        self.assertIn("block_about", judge)
        self.assertNotRegex(judge[judge.index("block_about"):].split("The supplied historical_context", 1)[0], "[\u2013\u2014]")


class RecordTests(unittest.TestCase):
    def test_a_rejection_is_recorded_on_the_writer_task_as_a_failed_try(self):
        with manual.offline():
            fixture = RepairFixture()
            self.addCleanup(fixture.close)
            fixture.round()
        events = [(row["event"], json.loads(row["detail"]).get("blocker")) for row in fixture.database.execute(
            "SELECT event,detail FROM workflow_task_events WHERE event='RETRY_WAIT' ORDER BY id")]
        self.assertEqual(events, [("RETRY_WAIT", "FOLLOWUP_JUDGE_REJECTED")])
        self.assertIn("message_writer", workflow_tasks.NO_DEADLINE_WORKERS)   # only the tries end it


if __name__ == "__main__":
    unittest.main()
