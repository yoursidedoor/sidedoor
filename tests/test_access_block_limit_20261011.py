"""Owner, 2026-10-11: a login/2FA/CAPTCHA stop is retried an hour later; the third abandons."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import unittest

import applications
import desktop_adapter
import test_desktop_adapter_safety as fixtures
import test_upload_failure_limit as uploads


CAPTCHA_STOP = {"blocked": {"code": "CAPTCHA_REQUIRED", "next_action": "Owner ticks the check"}}


class AccessBlockLimitTests(unittest.TestCase):
    setUp = fixtures.DesktopAdapterSafetyTests.setUp
    connect = fixtures.DesktopAdapterSafetyTests.connect
    write_config = fixtures.DesktopAdapterSafetyTests.write_config
    cycle = uploads.UploadFailureLimitTests.cycle
    advance = uploads.UploadFailureLimitTests.advance
    row = uploads.UploadFailureLimitTests.row
    attempt = uploads.UploadFailureLimitTests.attempt

    def blocked_inspect(self):
        next_at = self.row()["next_at"]
        if next_at and next_at > self.now:
            self.now = next_at
        self.cycle()
        request = desktop_adapter.claim(self.database, self.root, "access-fixture", self.now)
        self.assertEqual(request["payload"]["operation"], "inspect")
        self.assertTrue(desktop_adapter.complete(self.database, dict(request, result=dict(CAPTCHA_STOP)), self.now)["accepted"])
        self.advance(60)
        self.cycle()

    def test_inspect_access_block_retries_in_an_hour_and_the_third_abandons(self):
        self.blocked_inspect()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"], row["attempts"]), ("BLOCKED", "CAPTCHA_REQUIRED", 0))
        self.assertEqual(row["next_at"], desktop_adapter.later(self.now, applications.ACCESS_BLOCK_RETRY_SECONDS))
        self.assertEqual(applications.job_route(row["blocker"], row["state"]), "ORDINARY")
        self.blocked_inspect()
        self.assertEqual(self.row()["state"], "BLOCKED")
        self.blocked_inspect()
        row = self.row()
        self.assertEqual((row["state"], row["blocker"]), ("ABANDONED", applications.ACCESS_GIVE_UP_CODE))
        spent = self.database.execute("SELECT detail FROM application_events WHERE job_key=? AND event=?",
                                      (self.key, applications.ACCESS_GIVE_UP_EVENT)).fetchall()
        self.assertEqual(len(spent), 1)
        self.assertEqual([block["code"] for block in json.loads(spent[0][0])["blocks"]], ["CAPTCHA_REQUIRED"] * 3)

    def test_an_uncertain_or_reconciling_job_is_never_abandoned(self):
        for request_id in ("a-1", "a-2", "a-3"):
            self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,"
                                  "created_at,expires_at,result) VALUES (?,?,?,'inspect','{}','CONSUMED',?,?,?)",
                                  (request_id, request_id, self.key, self.now, self.now, json.dumps(CAPTCHA_STOP)))
        self.database.execute("UPDATE application_jobs SET state='RECONCILE',blocker='CAPTCHA_REQUIRED' WHERE job_key=?", (self.key,))
        self.database.commit()
        self.assertEqual(len(applications.access_blocks(self.database, self.key)), 3)
        with self.database:
            self.assertFalse(applications.give_up_on_access_blocks(self.database, dict(self.row()), self.now))
        self.attempt("live", "SUBMITTING")
        self.database.execute("UPDATE application_jobs SET state='BLOCKED' WHERE job_key=?", (self.key,))
        self.database.commit()
        with self.database:
            self.assertFalse(applications.give_up_on_access_blocks(self.database, dict(self.row()), self.now))
        self.assertNotEqual(self.row()["state"], "ABANDONED")


if __name__ == "__main__":
    unittest.main()
