"""E17: the outreach judges may say a question is off the recipient's lane (TYPE_FIT_NOTE), on READY only.
Owner, 2026-11-12: "One rewrite, then send regardless ... Type fit alone can never hold or fail a message",
tried on the side: the rewrite is written and judged without being saved, replaces the original only if it
passes, and is otherwise thrown away while the original goes out untouched."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import datetime as dt
import itertools
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

import approvals
import approvals_page
import engagement
import failures_page
import followup_execution as followup
import manual_followup_e2e as manual
import networking_page
import outreach
import outreach_agent
import outreach_promotion
import overview_page
import provider_limits
import research_workflow as research
import test_followup_repair_fu1
import test_outreach_agent
import test_outreach_composition
import test_outreach_promotion
from test_e17_contact_type_packets import label

ROOT = Path(__file__).resolve().parents[1]
TAG = "TYPE_FIT_NOTE"
NOTE = (" TYPE_FIT_NOTE: this person is a recruiter and the question is about how the team works day to day;"
        " a question about level, location or the process would fit.")
JUDGE_PROMPTS = {
    "outreach-agent-prompt.md": lambda text: text,
    "followup-prompt.md": lambda text: text.split("\nFOLLOWUP_SEND_JUDGE:\n", 1)[1],
}


def advisory(name):
    """The advisory paragraph of one judge prompt (from its heading to the next blank line)."""
    text = JUDGE_PROMPTS[name]((ROOT / name).read_text())
    return (TAG + text.split("\n" + TAG, 1)[1]).split("\n\n", 1)[0]


class PromptTests(unittest.TestCase):
    def test_each_judge_prompt_carries_the_advisory_and_says_it_never_changes_the_decision(self):
        for name in JUDGE_PROMPTS:
            text = advisory(name)
            for kind in research.CONTACT_TYPES:
                self.assertIn("\n- " + kind + ": ", text, name)
            for words in ("advisory only", "recipient.contact_type", "never changes the decision",
                          "never\na reason for BLOCKED or UNKNOWN", "a READY message stays READY",
                          "only when your decision is READY", 'starting "TYPE_FIT_NOTE:"', "never in\nnext_action",
                          "say nothing about it"):
                self.assertIn(words, text, name)
            self.assertFalse({"–", "—"} & set(text), name)

    def test_the_followup_judge_reads_the_label_its_packet_carries(self):
        self.assertIn("followup_context.recipient.contact_type", advisory("followup-prompt.md"))

    def test_the_tag_is_read_in_one_place_and_the_note_is_used_only_by_the_two_turns(self):
        sources = {path.name: path.read_text() for path in ROOT.glob("*.py")}
        self.assertGreater(len(sources), 50)   # the product modules were really searched
        # The literal tag: one module, which splits a reason on it and never reads the words after it.
        self.assertEqual(sorted(name for name, text in sources.items() if TAG in text), ["approvals.py"])
        self.assertEqual(sources["approvals.py"].count("partition(TYPE_FIT_TAG)"), 2)
        # Everything that touches type fit at all: the helper, the two turns with the dispatcher's
        # hook for the follow-up one, and the two pages that hide the note.
        self.assertEqual(sorted(name for name, text in sources.items() if "type_fit" in text.casefold()),
                         ["approvals.py", "approvals_page.py", "followup_execution.py", "networking_page.py",
                          "outreach.py", "outreach_agent.py"])
        self.assertEqual({name for name in JUDGE_PROMPTS if TAG in (ROOT / name).read_text()}, set(JUDGE_PROMPTS))

    def test_each_judge_prompt_says_a_rewrite_gets_no_second_note(self):
        for name in JUDGE_PROMPTS:
            self.assertIn("When type_fit_rewrite is true in the packet, this text is already the one rewrite made after\n"
                          "such a note: write no TYPE_FIT_NOTE, and decide on every other rule exactly as usual.",
                          advisory(name), name)

    def test_the_compose_prompt_stays_inside_its_word_budget(self):
        self.assertLessEqual(len((ROOT / "outreach-compose-prompt.md").read_text().split()), 3900)


class FirstMessageJudgeTests(unittest.TestCase):
    def harness(self, reason=None, contact_type=None):
        case = test_outreach_agent.OutreachAgentTests("cycle")
        case.setUp()
        self.addCleanup(case.doCleanups)
        if contact_type:
            research.initialize(case.database)
            label(case.database, case.action["profile"], contact_type)
        if reason:
            case.decision_override["reason"] = reason
        return case

    def outcome(self, case):
        result = case.cycle()
        saved = case.database.execute("SELECT state,result FROM outreach_actions").fetchone()
        decision = json.loads(case.database.execute(
            "SELECT record FROM outreach_agent_audit WHERE kind='DECISION' AND phase='INSPECTION'").fetchone()[0])["decision"]
        sends = [call.args for call in case.client._request.call_args_list]
        return {"items": [{key: value for key, value in item.items() if key != "evidence"} for item in result["items"]],
                "state": saved["state"], "sends": sends,
                "claims": case.database.execute("SELECT count(*) FROM outreach_action_claims").fetchone()[0]}, decision

    def test_ready_with_a_note_sends_exactly_as_ready_without_one(self):
        plain, plain_decision = self.outcome(self.harness(contact_type="recruiter"))
        base = plain_decision["reason"]
        noted, noted_decision = self.outcome(self.harness(reason=base + NOTE, contact_type="recruiter"))
        self.assertEqual(noted, plain)
        self.assertEqual(len(noted["sends"]), 1)   # the message went out
        self.assertEqual(noted["state"], "ACKNOWLEDGED_UNCONFIRMED")
        self.assertEqual((noted_decision["decision"], noted_decision["reason"]), ("READY", base + NOTE))   # recorded only

    def test_the_first_message_judge_packet_carries_the_label(self):
        for value, expected in (("recruiter", "recruiter"), ("staff_engineer", "senior_peer"), (None, "UNKNOWN")):
            with self.subTest(value=value):
                case = self.harness(contact_type=value)
                proof = case.adapter.inspect(case.action, case.config)
                self.assertEqual(case.packets[-1]["recipient"], {"contact_type": expected})
                self.assertEqual(proof["agent_decision"], "READY")   # a missing label holds nothing
                self.assertNotIn("contact_type", json.dumps(proof))

    def test_only_the_first_note_inspection_packet_gains_the_key(self):
        case = self.harness(contact_type="peer")
        case.cycle()
        case.cycle()
        phases = {packet["phase"]: "recipient" in packet for packet in case.packets}
        self.assertEqual(phases, {"INSPECTION": True, "RECONCILIATION": False})
        self.assertFalse(any("type_fit_rewrite" in packet for packet in case.packets))   # an ordinary note: no marker

    def test_a_side_judgment_is_marked_and_kept_apart_from_the_message_record(self):
        case = self.harness(reason=BASE + NOTE, contact_type="recruiter")
        proof = case.adapter.inspect(case.action, case.config, type_fit_rewrite=True)
        self.assertIs(case.packets[-1]["type_fit_rewrite"], True)
        self.assertEqual(proof["agent_decision"], "READY")
        kinds = {tuple(row) for row in case.database.execute("SELECT action_id,kind FROM outreach_agent_audit")}
        side = "type-fit:" + case.action["action_id"]
        self.assertEqual(kinds, {(side, "OBSERVATION"), (side, "DECISION")})   # no HISTORY finding, nothing under the message
        self.assertIsNone(networking_page.judge(overview_page.Reader(case.database), case.action["action_id"]))

    def test_a_saved_rewrite_is_recognised_by_its_draft_version(self):
        case = self.harness()
        research.initialize(case.database)
        for version, composition in ((1, {"version": "x"}), (2, {"version": "x", "type_fit_rewrite": True})):
            case.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                                  ("person", version, "hash-%d" % version, json.dumps({"composition": composition})))
        source = {"kind": "rw_drafts", "identity": "person", "version": 2, "content_hash": "hash-2"}
        self.assertTrue(case.adapter._type_fit_rewrite({"source": source}))
        for other in (dict(source, version=1, content_hash="hash-1"), dict(source, content_hash="other"),
                      dict(source, kind="else"), None):
            self.assertFalse(case.adapter._type_fit_rewrite({"source": other}))

    def test_a_reused_judgment_still_carries_the_note_so_only_the_marker_stops_a_second_attempt(self):
        case = self.harness(reason=BASE + NOTE, contact_type="recruiter")
        for unused in range(2):
            case.adapter.inspect(case.action, case.config)
            self.assertEqual(case.adapter.type_fit_note(case.action), NOTE.split(TAG + ":", 1)[1].strip())
        routes = [json.loads(row[0]).get("route") for row in case.database.execute(
            "SELECT record FROM outreach_agent_audit WHERE kind='DECISION' ORDER BY rowid")]
        self.assertEqual((routes, case.executor.call_count), ([None, "REUSED"], 1))
        self.assertIsNone(case.adapter.type_fit_note(dict(case.action, message="another text")))   # only this exact action


class PageTests(unittest.TestCase):
    def test_the_pages_show_the_reason_without_the_note_and_the_record_keeps_it(self):
        self.assertEqual(approvals.without_type_fit_note(BASE + NOTE), BASE)
        self.assertEqual(approvals.without_type_fit_note(BASE), BASE)
        self.assertIsNone(approvals.without_type_fit_note(NOTE.strip()))   # only a note: as if there were no reason
        self.assertIsNone(approvals.type_fit_note({"decision": "BLOCKED", "reason": BASE + NOTE}))   # READY only
        case = FirstMessageJudgeTests.harness(self, reason=BASE + NOTE, contact_type="recruiter")
        case.adapter.inspect(case.action, case.config)
        record = json.loads(case.database.execute("SELECT record FROM outreach_agent_audit WHERE kind='DECISION'").fetchone()[0])
        self.assertEqual(record["decision"]["reason"], BASE + NOTE)   # stored whole
        shown = networking_page.judge(overview_page.Reader(case.database), case.action["action_id"])
        self.assertEqual(shown["reason"], BASE)
        card = approvals_page.judge_verdict(case.database, case.action["action_id"], "outreach_agent_audit:" + record["observation_id"])
        self.assertEqual(card["reason"], BASE)
        self.assertNotIn("TYPE_FIT", json.dumps([shown, card]))


ORIGINAL = "Hi Mock, I built reliable APIs serving 40k requests per second at Example Cloud. How does your team work day to day?"
REWRITE = "Hi Mock, I built reliable APIs serving 40k requests per second at Example Cloud. What does success look like in the first months?"
BASE = "The note is grounded and the history is clear."


class FirstNoteTurnTests(unittest.TestCase):
    """prepare_drafts with the real writer step, draft versions and checks; the model calls are fakes."""

    def setUp(self):
        self.fixture = test_outreach_promotion.PromotionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.history_config()
        self.config = dict(self.fixture.config, timezone="America/Los_Angeles", model="synthetic", codex="NEVER")
        self.config["outreach_agent"] = {"enabled": True, "max_items": 2, "compose": True}
        self.database = self.fixture.database
        self.now = dt.datetime.fromisoformat(test_outreach_promotion.NOW)
        self.writes, self.judged, self.realtime = [], [], False
        self.writer = self.write(REWRITE)                 # what a type-fit hand-back gets
        self.judge = lambda candidate, rewrite: ("READY", BASE + (NOTE if candidate["invitation"] == ORIGINAL else ""))
        for target in ("socket.socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            boundary = patch(target, side_effect=AssertionError("External execution forbidden"))
            boundary.start()
            self.addCleanup(boundary.stop)
        self.run_cycle()                                   # the writer's first note becomes version 2

    @staticmethod
    def write(invitation, decision="COMPOSED"):
        def answer(packet):
            return json.dumps({"output_version": "outreach-compose-v1", "source_sha256": packet["source_sha256"],
                               "decision": decision, "reason": "stub", "invitation": invitation, "first_message": invitation,
                               "proof_point": "L2", "question": "A question?", "facts": test_outreach_composition.FACTS})
        return answer

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        self.writes.append(packet.get("repair_feedback"))
        feedback = packet.get("repair_feedback") or [{}]
        if feedback[0].get("agent_decision") == "BLOCKED":
            return self.write("Hi Mock, a repaired note. What does success look like?")(packet)
        return (self.writer if feedback[0].get("agent_decision") == "READY" else self.write(ORIGINAL))(packet)

    def inspect(self, adapter, candidate, config, *, type_fit_rewrite=False):
        decision, reason = self.judge(candidate, type_fit_rewrite)
        self.judged.append((candidate["invitation"], type_fit_rewrite, decision))
        adapter._judged = (outreach_agent.digest(candidate), {"decision": decision, "reason": reason})
        proof = self.fixture.history_proof((dt.datetime.now(dt.timezone.utc) if self.realtime else self.now).isoformat())
        if decision != "READY":
            proof.update(agent_decision=decision, reason=reason, next_action="Rewrite the note")
        return proof

    def run_cycle(self, later=0):
        self.now += dt.timedelta(seconds=later)
        with patch.object(outreach_agent.Adapter, "inspect", lambda *args, **named: self.inspect(*args, **named)), \
                patch.object(outreach_promotion, "resume_context", return_value=dict(test_outreach_composition.FAKE_RESUME)), \
                patch("outreach_provider._now", return_value=self.now.isoformat()):
            return outreach_agent.prepare_drafts(ROOT, self.database, self.config, Mock(spec=["call", "_request"]),
                                                  self.execute, None if self.realtime else self.now)

    def latest(self):
        return json.loads(self.database.execute("SELECT payload FROM rw_drafts ORDER BY version DESC LIMIT 1").fetchone()[0])

    def tasks(self, worker="outreach-draft-inspection"):
        return [dict(row) for row in self.database.execute("SELECT * FROM workflow_tasks WHERE worker=? ORDER BY rowid", (worker,))]

    def passed(self, result):
        return list(result["contexts"].values()) + result["campaign_candidates"]

    def assert_original_goes_out(self, result):
        (passed,) = self.passed(result)
        self.assertEqual(passed["draft"]["invitation"], ORIGINAL)
        self.assertEqual((self.latest()["version"], self.latest()["invitation"]), (2, ORIGINAL))   # nothing was saved
        (task,) = self.tasks()
        self.assertEqual((task["status"], task["blocker"], task["attempts"]), ("COMPLETE", None, 1))
        (written,) = self.tasks("outreach-draft-composition")
        self.assertEqual((written["status"], written["attempts"]), ("COMPLETE", 1))   # the writer's own task: untouched
        self.assertNotIn("HELD", json.dumps(result["items"]))
        self.assertEqual(self.database.execute("SELECT count(*) FROM workflow_task_events WHERE event IN "
                                               "('PROVIDER_USAGE_LIMIT_WAIT','COMPUTE_WAIT','FAILED')").fetchone()[0], 0)
        self.database.commit()
        self.assertEqual(failures_page.task_failures(overview_page.Reader(self.database), self.now,
                                                     lambda moment: moment.isoformat()), [])

    def test_a_rewrite_the_judge_passes_becomes_the_only_note(self):
        result = self.run_cycle()
        self.assertEqual(self.judged, [(ORIGINAL, False, "READY"), (REWRITE, True, "READY")])   # the rewrite: judged once
        (passed,) = self.passed(result)
        self.assertEqual(passed["draft"]["invitation"], REWRITE)
        latest = self.latest()
        self.assertEqual((latest["version"], latest["invitation"]), (3, REWRITE))
        self.assertIs(latest["composition"]["type_fit_rewrite"], True)
        self.assertEqual(passed["draft_source"]["version"], 3)
        old, new = self.tasks()
        self.assertEqual((new["status"], new["attempts"], new["max_attempts"]), ("COMPLETE", 1, 7))   # a full budget
        self.assertEqual(json.loads(new["result"])["draft"]["invitation"], REWRITE)   # its proof is its own
        # The hand-back is the writer's repair shape and carries the judge's note as the objection.
        self.assertEqual(self.writes[-1], [{"rejection_reason": NOTE.split(TAG + ":", 1)[1].strip(), "required_correction": None,
                                            "agent_decision": "READY", "previous_invitation": ORIGINAL,
                                            "previous_first_message": ORIGINAL}])
        # Promotion queues exactly one message for the person, and it is the rewrite.
        self.config["outreach_promotion_contexts"] = result["contexts"]
        with patch.object(outreach_promotion, "resume_context", return_value=dict(test_outreach_composition.FAKE_RESUME)):
            outreach_promotion.cycle(self.database, self.config, self.now.isoformat())
        (queued,) = [json.loads(row[0]) for row in self.database.execute("SELECT payload FROM outreach_actions")]
        self.assertEqual((queued["message"], queued["source"]["version"]), (REWRITE, 3))
        # The next cycle reuses the rewrite's finished check: no writer, no judge, no second rewrite.
        calls = (len(self.writes), len(self.judged))
        self.run_cycle(60)
        self.assertEqual((len(self.writes), len(self.judged)), calls)

    def test_the_rewrite_passes_on_the_real_clock_too(self):
        self.realtime = True   # as the scheduler runs it: reads are stamped after the cycle's own start
        self.config["outreach_content_max_age_seconds"] = 10 ** 9   # the fixture's research is dated September
        result = self.run_cycle()
        self.assertEqual(self.passed(result)[0]["draft"]["invitation"], REWRITE)
        self.assertEqual(self.tasks()[-1]["status"], "COMPLETE")

    def test_a_rewrite_the_judge_blocks_is_thrown_away(self):
        self.judge = lambda candidate, rewrite: ("BLOCKED", "It overstates.") if rewrite else ("READY", BASE + NOTE)
        self.assert_original_goes_out(self.run_cycle())
        self.assertEqual(self.judged, [(ORIGINAL, False, "READY"), (REWRITE, True, "BLOCKED")])

    def test_a_writer_that_cannot_rewrite_leaves_the_original(self):
        def raises(packet):
            raise RuntimeError("synthetic writer failure")

        def limited(packet):
            raise TimeoutError("OUTREACH_AI_TRANSIENT_FAILURE") from provider_limits.ProviderUsageLimit("synthetic limit")

        for name, writer in (("raises", raises), ("provider limit", limited), ("insufficient facts", self.write(None, "INSUFFICIENT_FACTS")),
                             ("malformed", lambda packet: "{not json"), ("same text", self.write(ORIGINAL))):
            with self.subTest(name):
                self.setUp()
                self.writer = writer
                self.assert_original_goes_out(self.run_cycle())
                self.assertEqual(self.judged, [(ORIGINAL, False, "READY")])   # nothing reached the judge

    def test_a_note_on_the_rewrite_is_ignored(self):
        self.judge = lambda candidate, rewrite: ("READY", BASE + NOTE)   # the judge tags everything
        result = self.run_cycle()
        self.assertEqual(self.passed(result)[0]["draft"]["invitation"], REWRITE)
        self.run_cycle(3700)   # its check expired and it is judged again, tagged again
        self.assertEqual(len([feedback for feedback in self.writes if feedback]), 1)   # one hand-back, ever
        self.assertEqual(self.latest()["version"], 3)

    def test_the_attempt_is_used_once_across_passes_and_restarts(self):
        self.judge = lambda candidate, rewrite: ("BLOCKED", "It overstates.") if rewrite else ("READY", BASE + NOTE)
        self.run_cycle()
        self.fixture.database.close()
        self.database = self.fixture.database = sqlite3.connect(self.fixture.path)
        self.database.row_factory = sqlite3.Row
        result = self.run_cycle(3700)   # the original's check expired: judged READY with the note again
        self.assertEqual([entry[:2] for entry in self.judged], [(ORIGINAL, False), (REWRITE, True), (ORIGINAL, False)])
        self.assertEqual(len([feedback for feedback in self.writes if feedback]), 1)
        self.assertEqual(self.passed(result)[0]["draft"]["invitation"], ORIGINAL)
        self.assertEqual(self.latest()["version"], 2)

    def test_a_crash_after_the_marker_leaves_the_original_and_no_second_attempt(self):
        def crash(packet):
            raise KeyboardInterrupt
        self.writer = crash
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle()
        self.writer = self.write(REWRITE)
        result = self.run_cycle(60)   # the original's check was already complete: it goes on as it is
        self.assertEqual(self.passed(result)[0]["draft"]["invitation"], ORIGINAL)
        self.assertEqual(len([feedback for feedback in self.writes if feedback]), 1)
        self.assertEqual(self.latest()["version"], 2)

    def test_no_note_is_exactly_the_path_it_always_was(self):
        self.judge = lambda candidate, rewrite: ("READY", BASE)
        result = self.run_cycle()
        self.assert_original_goes_out(result)
        self.assertEqual((self.writes, self.judged), ([None], [(ORIGINAL, False, "READY")]))
        self.assertEqual(self.database.execute("SELECT count(*) FROM outreach_agent_audit WHERE kind LIKE 'TYPE_FIT%'").fetchone()[0], 0)

    def test_a_later_real_rejection_still_gets_its_ordinary_rewrite_and_tries(self):
        self.judge = lambda candidate, rewrite: ("BLOCKED", "It overstates.") if rewrite else ("READY", BASE + NOTE)
        self.run_cycle()
        self.judge = lambda candidate, rewrite: ("BLOCKED", "It overstates.") if candidate["invitation"] == ORIGINAL else ("READY", BASE)
        result = self.run_cycle(3700)
        self.assertEqual(result["items"][0]["repair"]["state"], "RECOMPOSED")
        old, new = self.tasks()
        # Two real checks of the original, so the repaired note has 5 of the 7 left: the side attempt spent none.
        self.assertEqual((old["attempts"], old["blocker"], new["status"], new["max_attempts"]), (2, "REPLACED_BY_REWRITE", "COMPLETE", 5))
        self.assertEqual(self.passed(result)[0]["draft"]["invitation"], "Hi Mock, a repaired note. What does success look like?")


FIRST = "Following up on my platform note; happy to share relevant details."
SECOND = "Following up on my platform note. Is the role still open, and what does the process look like?"
REPAIRED = "Following up on my platform note with one grounded detail."


class TurnFixture(manual.ProviderFixture):
    """The real FollowupAdapter, writer step, queue and dispatcher; only the provider and the two models are fakes."""

    def __init__(self):
        super().__init__()
        self.writer = lambda packet, raw: raw.replace(json.dumps(FIRST), json.dumps(SECOND))   # a type-fit hand-back
        self.judge = lambda message, rewrite: ("READY", NOTE if message == FIRST else "")
        self.handbacks, self.judged = [], []
        label(self.database, manual.PROFILE, "recruiter")

    def execute(self, config, prompt, schema, timeout):
        packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
        if "action" in packet:
            if packet["phase"] != "INSPECTION":
                return super().execute(config, prompt, schema, timeout)
            self.judge_decision, note = self.judge(packet["action"]["message"], packet.get("type_fit_rewrite") is True)
            self.judged.append((packet["action"]["message"], packet.get("type_fit_rewrite") is True, self.judge_decision))
            output = json.loads(super().execute(config, prompt, schema, timeout))
            output["reason"] += note if output["decision"] == "READY" else ""
            return json.dumps(output)
        raw = super().execute(config, prompt, schema, timeout)
        feedback = (packet.get("repair_feedback") or [{}])[0]
        if feedback.get("agent_decision") == "READY":
            self.handbacks.append(packet["repair_feedback"])
            return self.writer(packet, raw)
        return raw.replace(json.dumps(FIRST), json.dumps(REPAIRED)) if feedback.get("agent_decision") == "BLOCKED" else raw

    def run_on_a_moving_clock(self):
        """As the scheduler runs it: no fixed `now`, and every clock read is later than the one before."""
        ticks = itertools.count()
        moment = lambda: self.now + dt.timedelta(seconds=next(ticks))
        self.config.update(outreach_content_max_age_seconds=10 ** 9, outreach_history_max_age_seconds=10 ** 9)   # dated fixture
        self.config["followup_execution"]["dispatch_enabled"] = True
        # The dispatcher's clock moves with the others: by the real one this dated follow-up is past its 14 days.
        with patch("outreach_provider._now", side_effect=lambda: moment().isoformat()), \
                patch.object(outreach, "dt", test_followup_repair_fu1.moving_clock(moment)), \
                patch("followup_execution._now", side_effect=lambda value=None: engagement.timestamp(moment() if value is None else value)):
            return followup.cycle(ROOT, self.database, self.config, self.adapter, candidates=[self.candidate],
                                  execute=self.execute, now=None)

    def outcome(self):
        rows = [dict(row) for row in self.database.execute("SELECT * FROM outreach_actions")]
        task = dict(self.database.execute("SELECT * FROM workflow_tasks WHERE worker='message_writer'").fetchone())
        return {"sent": [send["message"] for send in self.sends], "states": [row["state"] for row in rows],
                "queued": [json.loads(row["payload"])["message"] for row in rows],
                "claimed": [json.loads(row[0])["message"] for row in self.database.execute("SELECT payload FROM outreach_action_claims")],
                "writer_task": (task["status"], task["attempts"], task["max_attempts"], task["blocker"]),
                "waits": self.database.execute("SELECT count(*) FROM workflow_task_events WHERE event IN "
                                               "('PROVIDER_USAGE_LIMIT_WAIT','COMPUTE_WAIT')").fetchone()[0],
                "objections": self.database.execute("SELECT count(*) FROM outreach_agent_audit WHERE kind='JUDGE_OBJECTION'").fetchone()[0]}


def went_out(message, attempts=1):
    return {"sent": [message], "states": ["ACKNOWLEDGED_UNCONFIRMED"], "queued": [message], "claimed": [message],
            "writer_task": ("COMPLETE", attempts, 3, None), "waits": 0, "objections": 0}


class FollowupTurnTests(unittest.TestCase):
    def setUp(self):
        offline = manual.offline()
        offline.__enter__()
        self.addCleanup(offline.__exit__, None, None, None)
        self.fixture = TurnFixture()
        self.addCleanup(lambda: self.fixture.close())

    def resume(self):
        """A restart after a crash mid-dispatch: the interrupted read's lease ends, then its backoff."""
        self.fixture.restart()
        for minutes in (11, 61):
            self.fixture.now += dt.timedelta(minutes=minutes)
            self.fixture.run(True)

    def proof_matches_text(self):
        """The queued follow-up still binds to its writer output and its recorded action (no PROPOSAL_CHANGED)."""
        action = json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        context = followup.judgment_context(self.fixture.database, action)
        self.assertEqual(context["followup_decision"]["message"], action["message"])
        self.assertEqual(followup.binding_gates(self.fixture.database, self.fixture.config, action, self.fixture.now), [])
        return context

    def test_a_rewrite_the_judge_passes_is_the_one_follow_up_sent(self):
        result = self.fixture.run(True)
        self.assertEqual(self.fixture.outcome(), went_out(SECOND))
        self.assertEqual(self.fixture.judged, [(FIRST, False, "READY"), (SECOND, True, "READY")])   # the rewrite: judged once
        self.assertIs(self.proof_matches_text()["type_fit_rewrite"], True)
        judged = [packet for packet in self.fixture.inferences if packet.get("phase") == "INSPECTION"]
        self.assertEqual(judged[0]["followup_context"]["recipient"], {"contact_type": "recruiter"})   # the label the judge reads
        self.assertNotIn("type_fit_rewrite", judged[0])
        self.assertEqual(result["execution"]["items"][0]["type_fit"], "REWRITTEN")
        (entry,) = self.fixture.handbacks[0]
        self.assertEqual({key: entry[key] for key in ("rejection_reason", "agent_decision", "previous_message", "purpose")},
                         {"rejection_reason": NOTE.split(TAG + ":", 1)[1].strip(), "agent_decision": "READY",
                          "previous_message": FIRST, "purpose": "FOLLOWUP"})

    def test_the_rewrite_passes_on_a_moving_clock_too(self):
        self.fixture.run_on_a_moving_clock()
        self.assertEqual(self.fixture.outcome(), went_out(SECOND))

    def test_a_rewrite_the_judge_blocks_is_thrown_away(self):
        self.fixture.judge = lambda message, rewrite: ("BLOCKED", "") if rewrite else ("READY", NOTE)
        self.fixture.run(True)
        self.assertEqual(self.fixture.outcome(), went_out(FIRST))
        self.assertEqual(self.fixture.judged, [(FIRST, False, "READY"), (SECOND, True, "BLOCKED")])
        self.assertNotIn("type_fit_rewrite", self.proof_matches_text())
        # The message's own record and the page show the original's READY, never the thrown-away BLOCKED.
        action_id = self.fixture.database.execute("SELECT action_id FROM outreach_actions").fetchone()[0]
        (stored,) = [json.loads(row[0])["decision"] for row in self.fixture.database.execute(
            "SELECT record FROM outreach_agent_audit WHERE action_id=? AND kind='DECISION' AND phase='INSPECTION'", (action_id,))]
        self.assertEqual((stored["decision"], stored["reason"].endswith(NOTE)), ("READY", True))   # the note stays on record
        shown = networking_page.judge(overview_page.Reader(self.fixture.database), action_id)
        self.assertEqual(shown["verdict"], approvals_page.VERDICT_WORDS["READY"])
        self.assertNotIn("TYPE_FIT", shown["reason"])

    def test_a_writer_that_cannot_rewrite_leaves_the_original(self):
        def raises(packet, raw):
            raise RuntimeError("synthetic writer failure")

        def limited(packet, raw):
            raise TimeoutError("SUBSCRIPTION_EXECUTOR_FAILED") from provider_limits.ProviderUsageLimit("synthetic limit")

        def hold(packet, raw):
            return json.dumps(dict(json.loads(raw), decision="HOLD", message=None))

        for name, writer in (("raises", raises), ("provider limit", limited), ("hold", hold),
                             ("invalid", lambda packet, raw: "{not json"), ("same text", lambda packet, raw: raw)):
            with self.subTest(name):
                self.fixture.close()
                self.fixture = TurnFixture()
                self.fixture.writer = writer
                self.fixture.run(True)
                self.assertEqual(self.fixture.outcome(), went_out(FIRST))
                self.assertEqual(self.fixture.judged, [(FIRST, False, "READY")])   # nothing reached the judge
                self.proof_matches_text()

    def test_a_note_on_the_rewrite_is_ignored(self):
        self.fixture.judge = lambda message, rewrite: ("READY", NOTE)   # the judge tags everything
        self.fixture.run(True)
        self.assertEqual(self.fixture.outcome(), went_out(SECOND))
        self.assertEqual(len(self.fixture.handbacks), 1)

    def test_a_crash_in_the_attempt_leaves_the_original_and_no_second_attempt(self):
        def crash(packet, raw):
            raise KeyboardInterrupt
        self.fixture.writer = crash
        with self.assertRaises(KeyboardInterrupt):
            self.fixture.run(True)
        self.assertEqual((self.fixture.sends, self.fixture.outcome()["queued"]), ([], [FIRST]))
        self.proof_matches_text()
        self.fixture.writer = lambda packet, raw: raw.replace(json.dumps(FIRST), json.dumps(SECOND))
        self.resume()   # judged READY with the note again: the marker is there, so no second attempt
        self.assertEqual(self.fixture.outcome(), went_out(FIRST))
        self.assertEqual(len(self.fixture.handbacks), 1)

    def test_a_crash_after_the_swap_leaves_only_the_rewrite(self):
        with patch.object(approvals, "hold_reason", side_effect=KeyboardInterrupt):   # inside the claim transaction
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.run(True)
        self.assertEqual((self.fixture.sends, self.fixture.outcome()["queued"], self.fixture.outcome()["claimed"]), ([], [SECOND], []))
        self.proof_matches_text()
        self.resume()
        self.assertEqual(self.fixture.outcome(), went_out(SECOND))
        self.assertEqual(len(self.fixture.handbacks), 1)
        self.assertEqual(self.fixture.judged[-1], (SECOND, True, "READY"))   # judged as the rewrite it is: no new note

    def read(self):
        return dict(self.fixture.database.execute("SELECT state,attempts,token,lease_until,next_at,last_error "
                                                  "FROM outreach_read_retries WHERE phase='INSPECTION'").fetchone())

    def test_a_turn_that_outlasts_the_read_lease_still_sends_in_the_same_pass(self):
        """The turn runs inside the inspection read's 600 s lease; the held token gets a full lease again after it."""
        self.fixture.run(True)
        usual = self.read()
        self.assertEqual((usual["state"], usual["attempts"]), ("COMPLETE", 1))
        rewrites = lambda packet, raw: raw.replace(json.dumps(FIRST), json.dumps(SECOND))
        for name, writer, message in (("the rewrite", rewrites, SECOND), ("the original", lambda packet, raw: raw, FIRST)):
            with self.subTest(name):
                self.fixture.close()
                self.fixture = TurnFixture()

                def slow(packet, raw, writer=writer):
                    with self.fixture.database:   # the judge call and this turn took longer than the lease
                        self.fixture.database.execute("UPDATE outreach_read_retries SET lease_until=?",
                                                      ((self.fixture.now - dt.timedelta(seconds=1)).isoformat(),))
                    return writer(packet, raw)

                self.fixture.writer = slow
                result = self.fixture.run(True)
                self.assertEqual(self.fixture.outcome(), went_out(message))
                self.assertNotIn("blocker", result["execution"]["items"][0])
                self.assertEqual(self.read(), usual)   # the same finished read: no try spent, no wait

    def test_a_read_lease_lost_during_the_turn_is_the_stale_read_it_would_have_been(self):
        lost = {"state": "RETRY_WAIT", "attempts": 1, "token": None, "lease_until": None,
                "next_at": (self.fixture.now + dt.timedelta(minutes=15)).isoformat(), "last_error": "INTERRUPTED_READ"}

        def taken(packet, raw):
            with self.fixture.database:   # as outreach._claim_read leaves a read whose lease ran out under another cycle
                self.fixture.database.execute("UPDATE outreach_read_retries SET state=:state,token=NULL,lease_until=NULL,"
                                              "next_at=:next_at,last_error=:last_error", lost)
            return raw   # the writer changed nothing: the original stays queued

        self.fixture.writer = taken
        result = self.fixture.run(True)
        self.assertEqual(result["execution"]["items"][0]["blocker"], "STALE_INSPECTION_READ")
        self.assertEqual((self.fixture.sends, self.fixture.outcome()["queued"], self.fixture.outcome()["claimed"]), ([], [FIRST], []))
        self.assertEqual(self.read(), lost)   # not renewed, not finished: exactly as the other cycle left it

    def test_no_note_is_exactly_the_path_it_always_was(self):
        self.fixture.judge = lambda message, rewrite: ("READY", "")
        self.fixture.run(True)
        self.assertEqual(self.fixture.outcome(), went_out(FIRST))
        self.assertEqual((self.fixture.handbacks, len(self.fixture.inferences)), ([], 2))   # one writer call, one judge call
        self.assertEqual(self.fixture.database.execute(
            "SELECT count(*) FROM outreach_agent_audit WHERE kind LIKE 'TYPE_FIT%' OR action_id LIKE 'type-fit:%'").fetchone()[0], 0)

    def test_a_real_rejection_gets_its_ordinary_rewrite_with_no_try_spent_by_the_attempt(self):
        def rejected(fixture):
            with patch.object(followup, "_candidates", return_value=[fixture.candidate]):   # as the sent first note gives
                fixture.run(True)
            return fixture.outcome()

        self.fixture.judge = lambda message, rewrite: ("BLOCKED", "")   # the baseline: no note, a plain rejection
        baseline = rejected(self.fixture)
        # FU1: the rejection goes back to the writer (the second try of three); its rewrite is rejected too.
        self.assertEqual((baseline["sent"], baseline["queued"], baseline["writer_task"][1:3], baseline["objections"]),
                         ([], [REPAIRED], (2, 3), 2))
        self.fixture.close()
        self.fixture = TurnFixture()
        self.fixture.judge = lambda message, rewrite: ("BLOCKED", "") if rewrite else ("READY", NOTE)
        with patch.object(approvals, "hold_reason", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.run(True)   # the attempt ran, its rewrite was thrown away, the original is still queued
        self.fixture.restart()
        self.fixture.now += dt.timedelta(minutes=11)
        self.fixture.run(True)
        self.fixture.now += dt.timedelta(minutes=61)
        self.fixture.judge = lambda message, rewrite: ("BLOCKED", "")   # now a real rejection of the original
        self.assertEqual(rejected(self.fixture), baseline)
        self.assertEqual(len(self.fixture.handbacks), 1)

    def test_a_swapped_in_rewrite_that_is_then_rejected_gets_its_ordinary_repair(self):
        """FU1 closes the reviewer's case: "once swapped in, a rewrite that is judged again and BLOCKED is never sent"."""
        with patch.object(approvals, "hold_reason", side_effect=KeyboardInterrupt):   # stopped after the swap, before the claim
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.run(True)
        self.assertEqual((self.fixture.sends, self.fixture.outcome()["queued"]), ([], [SECOND]))
        # Judged again, the rewrite is really rejected; the repair's text passes, and the judge tags it too.
        self.fixture.judge = lambda message, rewrite: ("BLOCKED", "") if message == SECOND else ("READY", NOTE)
        with patch.object(followup, "_candidates", return_value=[self.fixture.candidate]):
            self.resume()
        self.assertEqual(self.fixture.outcome(), dict(went_out(REPAIRED, attempts=2), objections=1))   # the side attempt spent no try
        self.assertEqual(self.fixture.judged[-2:], [(SECOND, True, "BLOCKED"), (REPAIRED, False, "READY")])
        self.assertEqual(len(self.fixture.handbacks), 1)   # one type-fit attempt per follow-up, ever: the repair gets none
        self.proof_matches_text()


if __name__ == "__main__":
    unittest.main()
