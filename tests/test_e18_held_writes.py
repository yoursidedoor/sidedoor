"""E18 review round 2, B8: a message, follow-up or reply whose write was held at the port (LinkedIn read
as disconnected after the readiness check; nothing was sent) spends no try, is never ended for it, and
goes out exactly once after the reconnect. Each write path through the port, end to end over the
guardrail fixtures (FakePort; "held" refuses the write before any request, as UnipilePort._write does)."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest

try:
    from test_guardrails_unipile import FirstMessageBase, FollowupBase, InitialBase, ReplyBase
except ImportError:   # run as tests.test_e18_held_writes from the repo root
    from tests.test_guardrails_unipile import FirstMessageBase, FollowupBase, InitialBase, ReplyBase


class HeldInitial(InitialBase):
    """outreach_agent.Adapter._send_port, send_message into an existing chat."""

    def test_a_held_initial_spends_no_try_and_is_sent_once(self):
        self.port.fail("send_message", "held", times=4)   # held on the first send and on three more tries
        self.messages_hourly(14, minutes=360)   # reads back off up to 12 hours
        self.assertEqual(self.port.held_writes, ["send_message"] * 4)   # more than the 3 tries, none spent
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes(), len(self.ours()), len(self.initials())), (1, 1, 1))   # one request, one message
        self.messages_hourly(4)
        self.assertEqual((self.writes(), len(self.ours())), (1, 1))   # never twice

    def test_a_held_resend_is_not_one_of_the_three_tries(self):
        # followup_execution._resend_after_check: the first try timed out without landing, the resend is held.
        self.port.fail("send_message", "timeout")
        self.port.fail("send_message", "held", times=3)
        self.messages_hourly(14, minutes=360)   # reads back off up to 12 hours
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual(len(self.port.held_writes), 3)
        self.assertEqual((self.writes(), len(self.ours())), (2, 1))   # the timeout and the one that landed


class HeldFirstMessage(FirstMessageBase):
    """outreach_agent.Adapter._send_port, start_chat with no chat known: the case that was dropped."""

    def test_a_held_start_chat_is_not_dropped_and_is_sent_once(self):
        self.port.fail("start_chat", "held")
        self.messages_hourly(8)
        self.assertEqual(self.port.held_writes, ["start_chat"])
        self.assertEqual(self.state(), "CONFIRMED_SENT")
        self.assertEqual((self.writes(), self.ours()), (1, [self.TEXT]))
        self.messages_hourly(4)
        self.assertEqual((self.writes(), self.ours()), (1, [self.TEXT]))


class HeldFollowup(FollowupBase):
    """followup_execution.FollowupAdapter._send_unipile, start_chat with no chat known."""

    def test_a_held_followup_is_not_dropped_and_is_sent_once(self):
        self.port.fail("start_chat", "held")
        self.due_first()
        self.followups(1)
        self.assertEqual(self.port.held_writes, ["start_chat"])
        self.assertEqual(self.followup_writes(), 0)   # no request was made
        result = json.loads(self.database.execute("SELECT result FROM outreach_actions WHERE slot='FOLLOWUP:1'").fetchone()[0])
        self.assertNotIn("provider_request", result)   # back to "nothing written": no try spent
        self.assertNotIn("check_final", result)
        self.followups(8)
        self.assertEqual(self.state(1), "CONFIRMED_SENT")
        self.assertEqual((self.followup_writes(), len(self.ours())), (1, 1))
        self.followups(4)
        self.assertEqual((self.followup_writes(), len(self.ours())), (1, 1))


class HeldReply(ReplyBase):
    """reply_execution: the first send (_send_unipile_reply) and a retry (retry_reply)."""

    def test_a_held_reply_spends_no_try_and_is_sent_once(self):
        self.port.fail("send_message", "held", times=4)   # the first send and three retries: more than the 3 tries
        self.asked()
        self.assertEqual(self.port.held_writes, ["send_message"])
        self.assertEqual(self.reply_writes(), 0)
        self.replies(16)
        self.assertEqual(len(self.port.held_writes), 4)
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")
        self.assertEqual((self.reply_writes(), self.replies_sent()), (1, [self.TEXT]))
        self.replies(4)
        self.assertEqual((self.reply_writes(), self.replies_sent()), (1, [self.TEXT]))

    def test_held_retries_are_not_among_the_three_tries(self):
        self.port.fail("send_message", "timeout", times=2)   # two real tries that did not land
        self.port.fail("send_message", "held", times=4)       # then held four times: none of them a try
        self.asked()
        self.replies(14)
        self.assertEqual(len(self.port.held_writes), 4)
        self.assertEqual(self.reply_state(), "CONFIRMED_SENT")   # the third real try landed
        self.assertEqual((self.reply_writes(), self.replies_sent()), (3, [self.TEXT]))


if __name__ == "__main__":
    unittest.main()
