"""E17.T2: the person's contact type reaches the follow-up and reply writers; it is never a gate."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import sqlite3
import unittest

import manual_followup_e2e as manual
import reply_worker
import research_workflow as research
import test_reply_worker
from backlog_fixture import setUpModule, tearDownModule  # noqa: F401 (fixture messages predate the reply cutoff)
from test_pipeline_routing import PROFILE as REPLY_PROFILE

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("followup-prompt.md", "reply-prompt.md")


def label(database, profile, value, version=1):
    """A research draft for this person whose person_evidence carries `value` as the contact type."""
    evidence = {} if value is None else {"contact_type": {"field": "contact_type", "value": value}}
    database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                     (research.profile_key(profile), version, "hash-%d" % version,
                      json.dumps({"recipient": "Synthetic Person", "company": "Synthetic Company", "profile": profile,
                                  "person_evidence": evidence})))
    database.commit()


class FollowupPacketTests(unittest.TestCase):
    def setUp(self):
        self.boundaries = manual.offline()
        self.boundaries.__enter__()
        self.addCleanup(self.boundaries.__exit__, None, None, None)
        self.fixture = manual.Fixture()
        self.addCleanup(self.fixture.close)

    def composed(self):
        self.fixture.run()
        self.assertEqual(len(self.fixture.inferences), 1)
        action = json.loads(self.fixture.database.execute("SELECT payload FROM outreach_actions").fetchone()[0])
        self.assertEqual(action["purpose"], "FOLLOWUP")   # composed and queued: nothing held
        self.assertNotIn("contact_type", json.dumps(action))   # the label never reaches the action
        return self.fixture.inferences[0]["recipient"]

    def test_packet_carries_the_label(self):
        label(self.fixture.database, manual.PROFILE, "hiring_manager")
        self.assertEqual(self.composed(), {"contact_type": "hiring_manager"})

    def test_legacy_label_maps(self):
        label(self.fixture.database, manual.PROFILE, "staff_engineer")
        self.assertEqual(self.composed(), {"contact_type": "senior_peer"})

    def test_missing_label_is_unknown_and_still_composes(self):
        self.assertEqual(self.composed(), {"contact_type": "UNKNOWN"})   # no research draft at all

    def test_draft_without_the_record_is_unknown_and_still_composes(self):
        label(self.fixture.database, manual.PROFILE, None)
        self.assertEqual(self.composed(), {"contact_type": "UNKNOWN"})


class ReplyPacketTests(unittest.TestCase):
    def setUp(self):
        self.reply = test_reply_worker.ReplyWorkerTests("run_cycle")
        self.reply.setUp()
        self.addCleanup(self.reply.doCleanups)

    def decided(self):
        task_id = self.reply.enqueue()
        item = self.reply.run_cycle()["items"][0]
        self.assertEqual((item["workflow_status"], item["blocker"]), ("COMPLETE", None))   # nothing held
        return json.loads(self.reply.prompts[0].split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])["recipient"], task_id

    def test_packet_carries_the_label(self):
        label(self.reply.database, REPLY_PROFILE, "recruiter")
        self.assertEqual(self.decided()[0], {"contact_type": "recruiter"})

    def test_legacy_label_maps(self):
        label(self.reply.database, REPLY_PROFILE, "technical_executive")
        self.assertEqual(self.decided()[0], {"contact_type": "functional_executive"})

    def test_person_research_never_labelled_is_unknown_and_still_decides(self):
        self.assertEqual(self.decided()[0], {"contact_type": "UNKNOWN"})

    def test_a_later_label_does_not_change_a_saved_read(self):
        recipient, task_id = self.decided()
        label(self.reply.database, REPLY_PROFILE, "peer")
        task = dict(self.reply.task(task_id))
        request = reply_worker._source(self.reply.database, task, self.reply.config, test_reply_worker.NOW)[0]
        self.assertEqual(request["recipient"], recipient)

    def test_an_input_saved_before_the_field_keeps_its_shape(self):
        label(self.reply.database, REPLY_PROFILE, "peer")
        task_id = self.reply.enqueue()
        task = dict(self.reply.task(task_id))
        older = reply_worker._source(self.reply.database, task, self.reply.config, test_reply_worker.NOW)[0]
        self.assertEqual(older.pop("recipient"), {"contact_type": "peer"})
        prompt = "saved before E17\n\nUNTRUSTED_SOURCE_JSON:\n" + reply_worker.routing.encoded(older)
        reply_worker.initialize(self.reply.database)
        self.reply.database.execute("INSERT INTO reply_worker_inputs VALUES (?,?,?,?,?)",
                                    (task_id, reply_worker.sha(prompt), prompt, older["source_sha256"], "{}"))
        request = reply_worker._source(self.reply.database, task, self.reply.config, test_reply_worker.NOW)[0]
        self.assertEqual(request, older)


class PromptGuideTests(unittest.TestCase):
    def test_every_contact_type_has_a_row_in_both_guides(self):
        for name in PROMPTS:
            guide = (ROOT / name).read_text().split("CONTACT_TYPE_GUIDE", 1)[1].split("\n\n", 1)[0]
            for kind in research.CONTACT_TYPES:
                self.assertIn("\n- " + kind + ": ", guide, name)
            self.assertIn("recipient.contact_type", (ROOT / name).read_text())
            self.assertIn("\nUNKNOWN", guide, name)
            self.assertFalse({"–", "—"} & set(guide), name)

    def test_the_followup_guide_is_in_the_writer_half_only(self):
        writer, judge = (ROOT / "followup-prompt.md").read_text().split("\nFOLLOWUP_SEND_JUDGE:\n", 1)
        self.assertIn("CONTACT_TYPE_GUIDE", writer)
        self.assertNotIn("CONTACT_TYPE_GUIDE", judge)
        # Owner, 2026-11-12: the judge may check type fit and say so, advisory only. Its half names the
        # label once, inside that advisory paragraph (tests/test_e17_judge_advisory.py), nowhere else.
        self.assertEqual(judge.count("contact_type"), 1)
        self.assertIn("contact_type", judge.split("\nTYPE_FIT_NOTE (advisory only).", 1)[1].split("\n\n", 1)[0])


class UnreadableLabelTests(unittest.TestCase):
    """An unreadable label is still UNKNOWN (never a gate) and is logged once; no label logs nothing."""

    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        research.initialize(self.database)

    def test_malformed_draft_is_unknown_and_logged_once(self):
        self.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                              (research.profile_key(REPLY_PROFILE), 1, "hash-1", "{not json"))
        with self.assertLogs("sidedoor.reply_worker", "WARNING") as captured:
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")
        self.assertEqual(len(captured.records), 1)
        line = captured.records[0].getMessage()
        self.assertRegex(line, r"^contact type not read for person [0-9a-f]{12} \(JSONDecodeError\); treated as UNKNOWN$")
        self.assertNotIn(research.profile_key(REPLY_PROFILE), line)   # a short hash, never the profile

    def test_database_error_is_unknown_and_logged_once(self):
        self.database.close()
        with self.assertLogs("sidedoor.reply_worker", "WARNING") as captured:
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["contact type not read for person unknown (ProgrammingError); treated as UNKNOWN"])

    def test_a_record_of_the_wrong_shape_is_unknown_and_logged_once(self):
        cases = (("person_evidence is str", {"person_evidence": "a recruiter"}),
                 ("person_evidence is list", {"person_evidence": [{"contact_type": {"value": "peer"}}]}),
                 ("contact_type is str", {"person_evidence": {"contact_type": "peer"}}),
                 ("contact_type value is list", {"person_evidence": {"contact_type": {"value": ["peer"]}}}),
                 ("contact_type value is int", {"person_evidence": {"contact_type": {"value": 3}}}),
                 ("contact_type value is bool", {"person_evidence": {"contact_type": {"value": False}}}))
        for version, (kind, payload) in enumerate(cases, 1):
            with self.subTest(kind):
                self.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                                      (research.profile_key(REPLY_PROFILE), version, "hash-%d" % version, json.dumps(payload)))
                with self.assertLogs("sidedoor.reply_worker", "WARNING") as captured:
                    self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")
                (line,) = [record.getMessage() for record in captured.records]
                self.assertRegex(line, r"^contact type not read for person [0-9a-f]{12} \(%s\); treated as UNKNOWN$" % kind)
                self.assertNotIn(research.profile_key(REPLY_PROFILE), line)

    def test_an_absent_record_at_any_level_logs_nothing(self):
        payloads = ({}, {"person_evidence": None}, {"person_evidence": {}}, {"person_evidence": {"contact_type": None}},
                    {"person_evidence": {"contact_type": {}}}, {"person_evidence": {"contact_type": {"value": None}}},
                    {"person_evidence": {"contact_type": {"value": " "}}})
        with self.assertNoLogs("sidedoor.reply_worker", "WARNING"):
            for version, payload in enumerate(payloads, 1):
                self.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                                      (research.profile_key(REPLY_PROFILE), version, "hash-%d" % version, json.dumps(payload)))
                self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")
            self.database.execute("INSERT INTO rw_drafts VALUES (?,?,?,?)",
                                  (research.profile_key(REPLY_PROFILE), 99, "hash-99",
                                   json.dumps({"person_evidence": {"contact_type": {"value": "peer"}}})))
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "peer")

    def test_no_draft_and_no_label_log_nothing(self):
        with self.assertNoLogs("sidedoor.reply_worker", "WARNING"):
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")   # no draft
            label(self.database, REPLY_PROFILE, None)
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [REPLY_PROFILE]), "UNKNOWN")   # no label
            self.assertEqual(reply_worker.recipient_contact_type(self.database, [None, ""]), "UNKNOWN")   # no person


if __name__ == "__main__":
    unittest.main()
