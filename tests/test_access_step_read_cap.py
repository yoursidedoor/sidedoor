"""The read cap and the access step (E15.T8, after the real re-run of 2026-11-14): the cap of three reads of
one posting counted the access step's own request and the read that met the wall, so wall + step + wall was
three and the next step was refused with "ran out of tries" and no card.
"""

import profile_home  # noqa: F401
import json
import unittest
from unittest.mock import patch

import access_cards
import access_outcomes
import access_step
import application_connection
import applications
import desktop_adapter
import test_bb_inspect_scope_and_silent_eligibility as bb
import test_part_c_read_gating as part_c

WALL = json.dumps({"blocked": {"code": "AUTHENTICATION_REQUIRED", "next_action": "sign in"}})
FAILED = json.dumps({"error": {"code": "APPLICATION_BROWSER_FAILED"}})
EXHAUSTED = "APPLICATION_BROWSER_INVESTIGATION_EXHAUSTED"


class ReadCap(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def adapter(self, hours=0):
        self.made = application_connection.HostedDesktopAdapter(
            self.database, self.root, {"application_browser": {"enabled": True}}, clock=lambda: bb.later(hours))
        self.body = self.made.request_body("inspect", {"job": self.job()})
        return self.made

    def request(self, name, hours, result=WALL):
        """One finished inspect request of the job, as a session leaves it."""
        with self.database:
            self.database.execute("""INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,
                state,created_at,expires_at,claims,result) VALUES (?,?,?,?,?,'DONE',?,?,1,?)""",
                (name, "b-" + name, self.key, "inspect", desktop_adapter.encode(self.body), bb.later(hours), bb.later(hours + 1), result))

    def next_call(self, hours):
        """What the job's next read request gets: the session (allowed) or the cap's answer."""
        allowed = {"allowed": True}
        self.made.clock = lambda: bb.later(hours)
        with patch.object(application_connection.HostedDesktopAdapter, "cached_available", return_value=False), \
                patch.object(desktop_adapter.DesktopAdapter, "call", lambda *a, **k: allowed), \
                patch.object(application_connection.run_locks, "browser_session") as lock, \
                patch.object(application_connection.browser_runtime, "check_available", return_value=None), \
                patch.object(application_connection.HostedDesktopAdapter, "reserve"), \
                patch("provider_pacing.browser_slot"), patch("engines.engine") as engine:
            lock.return_value.__enter__.return_value = True
            engine.return_value.wait.return_value = None
            answer = self.made.call("inspect", {"job": self.job(), "read_only": True})
        return "allowed" if answer is allowed else answer["blocked"]["code"]

    def wall_met(self, hours):
        """The applications run reads the newest wall result, as `process` does."""
        result = json.loads(WALL)
        with self.database:
            return applications.hold_access_block(self.database, self.job(), "AUTHENTICATION_REQUIRED", "sign in", bb.later(hours), {}, result)

    def step_ran(self, name, hours, outcome):
        """The job's due step was run by request `name` and settled, as `access_step.run` leaves it."""
        self.request(name, hours)
        with self.database:
            step = self.database.execute("SELECT id FROM %s WHERE state='DUE' ORDER BY id DESC" % access_step.TABLE).fetchone()
            self.database.execute("UPDATE %s SET state='DONE',outcome=?,ended_at=? WHERE id=?" % access_step.TABLE,
                                  (outcome, bb.later(hours), step[0]))
            desktop_adapter.record(self.database, name, access_step.STEP_REQUEST_EVENT, bb.later(hours), {"step": step[0]})

    def choose(self, hours=-9):
        with self.database:
            access_outcomes.set_account_setting(self.database, "SIGN_IN", bb.later(hours))

    def blocker(self):
        return self.database.execute("SELECT state,blocker FROM application_jobs WHERE job_key=?", (self.key,)).fetchone()

    def test_the_real_run_wall_step_card_the_users_press_wall_and_the_second_step_runs(self):
        self.adapter()
        self.choose()
        self.request("r1", -8)                                # request 1: the read met the wall
        self.assertEqual(self.wall_met(-7.9), access_step.PLANNED)
        self.step_ran("r2", -7.5, "SIGN_UP_REFUSED")          # request 2: the step, settled as that build settled it
        self.assertEqual(self.wall_met(-7.4), "WAITING")      # the card
        with self.database:                                   # that build marked neither request
            self.database.execute("DELETE FROM desktop_events WHERE event IN (?,?)",
                                  (access_step.STEP_REQUEST_EVENT, access_step.WALL_ANSWERED_EVENT))
        with patch.object(access_cards.user_step, "available", return_value=True), \
                patch.object(access_cards.user_step, "openable", return_value=True):
            said = access_cards.done(self.database, self.key, bb.later(-6))       # [I've signed in]
        self.assertEqual(said["text"], "Sidedoor tries this application on its next round.")
        self.assertEqual(self.next_call(-5.5), "allowed")
        self.request("r3", -5)                                # request 3: read again, the wall again
        self.assertEqual(self.wall_met(-4.9), access_step.PLANNED)
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT kind,state FROM %s ORDER BY id" % access_step.TABLE)],
                         [("ACCESS", "DONE"), ("ACCESS", "DUE")])
        self.assertEqual(self.next_call(-4.8), "allowed")     # request 4 runs step 2: it was refused here, with no card
        self.assertNotEqual(self.blocker()["blocker"], EXHAUSTED)

    def test_a_step_and_the_wall_read_it_answered_spend_none_of_the_three_reads(self):
        self.adapter()
        self.choose()
        self.request("r1", -8)
        self.wall_met(-7.9)                                   # the wall read is answered by a step
        self.step_ran("r2", -7.5, "EMAIL_PENDING")            # the step, its email not there yet
        self.assertEqual(self.wall_met(-7.4), access_step.PLANNED)
        self.assertEqual(self.next_call(-7.3), "allowed")     # the CONFIRM step can run
        self.step_ran("r3", -7.2, "SIGNED_IN")
        self.assertEqual(self.wall_met(-7.1), access_step.PLANNED)
        marked = {row[0]: row[1] for row in self.database.execute(
            "SELECT request_id,event FROM desktop_events WHERE event IN (?,?) ORDER BY id", (access_step.STEP_REQUEST_EVENT, access_step.WALL_ANSWERED_EVENT))}
        self.assertEqual(set(marked), {"r1", "r2", "r3"})
        self.assertEqual(access_step.not_reads(self.database, self.key), {"r1", "r2", "r3"})
        # The posting itself still has its three reads, and only three.
        for index in range(3):
            self.assertEqual(self.next_call(-7 + index), "allowed", index)
            self.request("read%d" % index, -7 + index, FAILED)
        self.assertEqual(self.next_call(-3), EXHAUSTED)
        # A wall read that became a CARD (Ask me each time) counts as it always has.
        self.setUp()
        self.adapter()
        for index in range(3):
            self.request("ask%d" % index, -8 + index)
        self.assertEqual(self.wall_met(-4.9), "WAITING")
        self.assertEqual((access_step.not_reads(self.database, self.key), self.next_call(-4)), (set(), EXHAUSTED))

    def test_each_of_the_users_actions_on_the_card_starts_a_new_scope_and_a_freed_site_does_not(self):
        for outcome, fresh in (("released", True), (access_outcomes.SETTING_RELEASED, True), (access_outcomes.HOST_RELEASED, False)):
            self.setUp()
            self.adapter()
            for index in range(3):
                self.request("old%d" % index, -8 + index, FAILED)
            self.assertEqual(self.next_call(-4), EXHAUSTED)
            with self.database:
                applications.event(self.database, self.key, access_outcomes.RELEASED_EVENT, {"outcome": outcome}, bb.later(-3))
            self.assertEqual(self.next_call(-2), "allowed" if fresh else EXHAUSTED, outcome)
        # Resend and I've confirmed it release through the same call (`access_outcomes.resume`).
        self.setUp()
        self.adapter()
        self.choose()
        self.request("r1", -8)
        self.wall_met(-7.9)
        self.step_ran("r2", -7.5, "EMAIL_PENDING")
        self.wall_met(-7.4)
        self.step_ran("r3", -7.2, "EMAIL_PENDING")
        self.assertEqual(self.wall_met(-7.1), "WAITING")
        for index in range(3):
            self.request("old%d" % index, -7 + 0.1 * index, FAILED)
        self.assertEqual(access_cards.resend(self.database, self.key, bb.later(-6))["tone"], "success")
        self.assertEqual(self.next_call(-5), "allowed")

    def test_the_cap_with_a_step_still_to_run_is_a_card_never_ran_out_of_tries(self):
        self.adapter()
        self.choose()
        self.request("r1", -8)
        self.wall_met(-7.9)                                   # the step is due
        for index in range(3):                                # its sessions keep dying before they reach the page
            self.request("died%d" % index, -7 + index, FAILED)
        self.assertEqual(self.next_call(-3), "AUTHENTICATION_REQUIRED")   # not EXHAUSTED
        self.assertEqual([tuple(row) for row in self.database.execute("SELECT state,outcome FROM %s" % access_step.TABLE)], [("DONE", "UNKNOWN")])
        self.assertEqual(self.wall_met(-2.9), "WAITING")
        wait = access_outcomes.open_wait(self.database, self.key)
        self.assertEqual((wait["reason"], tuple(self.blocker())), ("ASK_EACH_TIME", ("BLOCKED", "AUTHENTICATION_REQUIRED")))
        self.assertEqual(access_outcomes.WAIT_REASONS[wait["reason"]], "This site needs an account.")

    def test_a_job_parked_with_its_reads_spent_says_so_and_try_again_gives_it_a_new_scope(self):   # owner, 2026-11-14: "fix"
        import applications_page
        import overview_page
        self.adapter()
        for index in range(3):
            self.request("old%d" % index, -8 + index, FAILED)
        self.assertEqual(self.next_call(-4), EXHAUSTED)
        with self.database:   # as the applications run parks it: BLOCKED on that code, a time left behind in next_at
            self.database.execute("UPDATE application_jobs SET state='BLOCKED',blocker=?,next_action='x',next_at=? WHERE job_key=?",
                                  (EXHAUSTED, bb.later(1), self.key))

        def shown():
            view = applications_page.gather(self.database, {"timezone": "America/Los_Angeles"}, self.root)
            return next(item for item in view["list"]["jobs"] if item["key"] == self.key)
        job = shown()
        self.assertEqual((job["state"], job["group"], job["problem"], job["todo"], job["try_again"]),
                         ("Blocked, needs you", "needs_you", "Sidedoor ran out of tries reading this posting.",
                          "Press Try again if you want this job.", "Try again"))
        self.assertNotIn("Will try again automatically", json.dumps(job, default=str))
        reader = overview_page.Reader(self.database)
        self.assertEqual((self.key in overview_page._blocked(reader)[0], overview_page.retrying_keys(reader, bb.dt.datetime.fromisoformat(bb.later(0)))), (True, []))
        # Try again is the reopen that releases it: queued, and the posting has its reads again.
        said = access_cards.try_again(self.database, self.key, bb.later(-3))
        self.assertEqual((said["reopened"], said["text"]), (True, "Reopened. Sidedoor reads the posting again on its next round."))
        self.assertEqual(tuple(self.blocker()), ("QUEUED", None))
        self.assertEqual(self.next_call(-2), "allowed")
        self.assertEqual(access_cards.try_again(self.database, self.key, bb.later(-1))["reopened"], False)   # a second press
        self.assertIsNone(shown()["try_again"])


if __name__ == "__main__":
    unittest.main()
