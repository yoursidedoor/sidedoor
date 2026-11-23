"""K20 (owner, 2026-11-23: "sidedoor should auto drop it from the count/card when the retry verification
check is exhausted"): an application whose outcome checks are spent needs nobody, is not retrying, shows one
plain line with nothing to do, and is never submitted again."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import unittest

import applications
import applications_page
import desktop_adapter
import overview_page
import test_access_outcomes_e15_t1 as walls
import test_confirm_read_card as confirm_card

UNKNOWN = {"status": "UNKNOWN", "next_action": "Investigate unknown outcome"}


class SpentOutcomeChecks(unittest.TestCase):
    for _name in ("setUp", "connect", "write_config", "review", "job", "cycle", "advance", "run_until_submit", "row",
                  "requests", "waits", "events"):
        locals()[_name] = getattr(walls.AccessOutcomeTests, _name)
    for _name in ("sent_with_outcome_unknown", "next_read", "counts"):
        locals()[_name] = getattr(confirm_card.TheSequence, _name)
    del _name

    def test_the_tenth_read_drops_it_from_every_count_and_it_is_never_submitted_again(self):
        self.sent_with_outcome_unknown()
        with self.database:
            self.database.execute("UPDATE application_jobs SET reconcile_attempts=? WHERE job_key=?",
                                  (applications.RECONCILE_MAX_ATTEMPTS - 1, self.key))
        read = self.next_read()
        self.assertEqual(read["payload"]["operation"], "reconcile")
        self.assertTrue(desktop_adapter.complete(self.database, dict(read, result=UNKNOWN), self.now)["accepted"])
        for _ in range(3):
            self.advance(1200)
            self.cycle()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"], row["next_at"]), ("BLOCKED", "SUBMISSION_OUTCOME_UNKNOWN", None))
        self.assertTrue(overview_page.outcome_check_spent(row["blocker"], row["reconcile_attempts"]))
        before = self.counts()
        for _ in range(6):                                                   # never submitted again
            self.advance(3600)
            self.cycle()
            self.assertIsNone(desktop_adapter.claim(self.database, self.root, "k20-fixture", self.now))
        self.assertEqual(self.counts()["submit"], before["submit"])
        self.assertEqual(self.counts()["armed"], before["armed"])

        reader = overview_page.Reader(self.database)
        now = dt.datetime.fromisoformat(self.now)
        self.assertEqual(overview_page._blocked(reader), ([], []))           # neither a hold nor a retry wait
        self.assertEqual(overview_page.needs_you(reader, now)["applications"], 0)   # Overview, alerts, tray
        self.assertEqual(overview_page.right_now(reader, now)["blocked"], 0)
        self.assertNotIn(self.key, overview_page.retrying_keys(reader, now))   # not "Waiting to retry"
        self.assertEqual(applications_page.needs_you_keys(reader, now), [])
        view = applications_page.job_view(applications_page._rows_for(reader, [self.key])[0], set(), set(), set(),
                                          lambda moment: str(moment))
        self.assertEqual((view["state"], view["todo"], view["todo_href"], view["needs_you"], view["group"], view["problem"]),
                         ("Not confirmed", applications_page.SPENT_TODO, None, False, "progress", None))
        self.assertEqual(view["todo"], "Sidedoor could not confirm whether this application went through. "
                                       "It will not submit it again.")
        self.assertNotIn("—", view["todo"])


if __name__ == "__main__":
    unittest.main()
