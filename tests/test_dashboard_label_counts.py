"""Every count is the number of items whose own label it names (owner, 2026-10-24): one function maps
an item to its label, and chips, headings and Overview numbers count by it. One test per audit item."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import re
import unittest
from unittest import mock

import applications_page
import approvals
import approvals_page
import failures_page
import networking_page
import overview_page
import test_dashboard_applications as apps_base
import test_dashboard_failures as fail_base
import test_dashboard_networking as net_base

# Filters whose name is the badge their jobs carry (Applications).
BADGE_OF = {"confirming": "Waiting for confirmation", "retrying": "Waiting to retry", "reading": "Reading now",
            "queued": "Queued", "waiting_browser": "Waiting for the browser"}


def assert_counted_by_label(test, counter, items, label=None, wanted=None):
    """The shared check: the counter equals the items listed, and (when given) every item carries `wanted`."""
    test.assertEqual(counter, len(items), items)
    if wanted is not None:
        test.assertEqual({label(item) for item in items} - {wanted}, set(), items)


class ApplicationsCounts(unittest.TestCase):
    setUp, tearDown, gather, overview = (apps_base.GatherTest.setUp, apps_base.GatherTest.tearDown,
                                         apps_base.GatherTest.gather, apps_base.GatherTest.overview)

    def read_now(self, key):
        lease = (self.now + dt.timedelta(minutes=5)).isoformat()
        self.database.execute("INSERT INTO desktop_requests(request_id,binding,job_key,operation,payload,state,created_at,"
                              "expires_at,lease_until) VALUES (?,'b',?,'inspect','{}','CLAIMED',?,?,?)",
                              (key, key, lease, lease, lease))

    def fixture(self):
        db, job = self.database, apps_base.job
        job(db, "submitting", "SUBMITTING")
        job(db, "reconcile", "RECONCILE")
        job(db, "retry", "RETRY_WAIT", "ANSWERING_RETRY")
        job(db, "wall", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "wall-read", "BLOCKED", "CAPTCHA_REQUIRED")
        job(db, "retry-read", "RETRY_WAIT", "ANSWERING_RETRY")
        job(db, "owner-read", "BLOCKED", "DESIGNATED_RESUME_MISMATCH")
        job(db, "queued-read", "QUEUED")
        job(db, "done", "SUBMITTED")
        for number in range(applications_page.PAGE_SIZE + 5):
            job(db, "q%02d" % number, "QUEUED", updated="2026-10-20T10:00:00+00:00")
        for key in ("wall-read", "retry-read", "owner-read", "queued-read"):
            self.read_now(key)
        apps_base.app_event(db, "done", "SUBMISSION_VERIFIED", self.inside)
        apps_base.app_event(db, "done", "SUBMISSION_VERIFIED", self.inside + dt.timedelta(minutes=5))   # one job, two records
        db.commit()

    def every_job(self, show):
        first = self.gather(show=show)
        jobs = list(first["list"]["jobs"])
        for page in range(2, first["list"]["pages"] + 1):
            jobs += self.gather(show=show, page=page)["list"]["jobs"]
        return first, jobs

    def test_2_6_9_every_chip_counts_its_badge(self):
        self.fixture()
        counts = self.gather()["counts"]
        for show in applications_page.FILTER_KEYS:
            _, jobs = self.every_job(show)
            assert_counted_by_label(self, counts[show], jobs, lambda job: job["state"], BADGE_OF.get(show))
        self.assertEqual(counts["confirming"], 2)    # 2: SUBMITTING badges "Waiting for confirmation" too
        self.assertEqual(counts["reading"], 3)       # 6: an owner hold being read badges "Blocked, needs you"
        self.assertEqual(counts["retrying"], 2)      # 9: a wait being read badges "Reading now"
        right_now = self.overview()["right_now"]
        self.assertEqual((right_now["confirming"], right_now["retrying"]), (counts["confirming"], counts["retrying"]))

    def test_3_group_headings_count_the_whole_list(self):
        self.fixture()
        first, jobs = self.every_job("all")
        self.assertGreater(first["list"]["pages"], 1)
        for item in first["list"]["groups"]:
            assert_counted_by_label(self, item["count"], [job for job in jobs if job["group"] == item["key"]])

    def test_5_submitted_today_counts_unique_jobs(self):
        self.fixture()
        view = self.gather(show="submitted_today")
        assert_counted_by_label(self, view["counts"]["submitted_today"], view["list"]["jobs"])
        self.assertEqual((self.overview()["submitted"], view["counts"]["submitted_today"]), (1, 1))


    def test_held_applications_count_by_their_label_everywhere(self):
        """Review holds applications (owner, 2026-10-24): Overview's "applications to approve", Approvals'
        "Applications waiting" and the Applications badge "Waiting for your approval" count the same jobs,
        and none of them is "Blocked, needs you" or "Waiting to retry"; nor is an approved one."""
        import applications
        db = self.database
        self.fixture()
        snapshot = json.dumps({"route": "ats", "destination": "https://jobs.example.com/x", "subject": None,
                               "resume": "ATS version (PDF)", "questions": []})
        for key in ("held-1", "held-2"):
            apps_base.job(db, key, "BLOCKED", applications.AWAITING_APPLICATION_APPROVAL)
            db.execute("INSERT INTO application_approvals(job_key,approval_id,state,held_at,snapshot,payload_hash) "
                       "VALUES (?,?,'WAITING','2026-10-21T17:00:00+00:00',?,'h')", (key, "a-" + key, snapshot))
        apps_base.job(db, "approved", "BLOCKED", applications.APPLICATION_APPROVED)
        db.commit()
        _, jobs = self.every_job("all")
        held = [job for job in jobs if job["state"] == applications_page.HELD_WORDS]
        needs = self.overview()["needs"]
        assert_counted_by_label(self, needs["applications_held"], held)
        self.assertEqual((2, 2), (needs["applications_held"], approvals_page.application_count(db)))
        for show in ("needs_you", "retrying"):
            _, listed = self.every_job(show)
            self.assertFalse({"held-1", "held-2", "approved"} & {job["key"] for job in listed}, show)
        # Failures says they wait for approval, never "your answers are in" (pre-release pass, 2026-10-25).
        now = dt.datetime.now(dt.timezone.utc)
        items = failures_page.application_failures(overview_page.Reader(db), now, str)
        codes = {item["refs"]["job_key"]: item["code"] for item in items if item.get("refs")}
        self.assertEqual(("AWAITING_APPLICATION_APPROVAL", "AWAITING_APPLICATION_APPROVAL"), (codes.get("held-1"), codes.get("held-2")))
        self.assertNotIn("ANSWERS_IN", codes.values())


class FailuresCounts(fail_base.GatherBase):
    def test_4_earlier_counts_the_whole_section(self):
        def retry(key, hours):
            moment = (self.now - dt.timedelta(hours=hours)).isoformat()
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,blocker,updated_at) "
                                  "VALUES (?,?,?,?,?,?,?)", (key, "acme", key, json.dumps({"company": "Acme", "role": key}),
                                                             "RETRY_WAIT", "ANSWERING_RETRY", moment))
            self.database.execute("INSERT INTO application_events(job_key,event,at,detail) VALUES (?,?,?,?)",
                                  (key, "RETRY_WAIT", moment, json.dumps({"code": "ANSWERING_RETRY"})))
        for number in range(failures_page.LIST_LIMIT + 5):
            retry("old%02d" % number, 30 + number / 60)
        retry("new", 2)
        first = [s for s in self.gather(section="applications")["sections"] if s["key"] == "applications"][0]
        earlier = []
        for page in range(1, first["paging"]["pages"] + 1):
            [section] = [s for s in self.gather(section="applications", page=page)["sections"] if s["key"] == "applications"]
            earlier += section["earlier"]
        self.assertGreater(first["paging"]["pages"], 1)
        assert_counted_by_label(self, first["earlier_total"], earlier)
        self.assertEqual(first["earlier_total"], failures_page.LIST_LIMIT + 5)
        self.assertEqual(len(first["recent"]), 1)   # recent first, on page 1


class NetworkingCounts(net_base.Page, unittest.TestCase):
    def test_1_sending_counts_the_sending_badge_everywhere(self):
        self.open_store(self.root)
        self.action("invite-a", "ack-a", "INVITE", "ACKNOWLEDGED_DELIVERY_UNCONFIRMED", "Hi.", claimed=net_base.at(1))
        self.action("invite-b", "busy-b", "INVITE", "REQUEST_STARTED", "Hi.", claimed=net_base.at(2))
        self.action("follow-c", "cal-c", "FOLLOWUP", "REQUEST_STARTED", "Following up.", claimed=net_base.at(3))
        self.invited("dee-d", "Hi Dee.", 4)
        self.action("invite-d2", "dee-d", "INVITE", "REQUEST_STARTED", "Hi again.", claimed=net_base.at(5))   # badge: invited
        counts = self.view()["list"]["counts"]
        for show in networking_page.FILTER_KEYS - {"replied_today"}:
            entries = self.entries(show=show)
            assert_counted_by_label(self, counts[show], entries, lambda entry: entry["status"],
                                    "Sending…" if show == "sending" else None)
        self.assertEqual(counts["sending"], 2)
        overview = self.get("/")
        self.assertEqual(int(re.search(r"(\d+) still sending", overview).group(1)), counts["sending"])
        self.assertIn('href="/sent?show=sending#conversations">2 still sending', overview)

    def test_7_couldnt_send_needs_you_on_overview(self):
        self.open_store(self.root)
        self.action("refused-r", "ray-r", "FOLLOWUP", "BLOCKED", "Following up.", claimed=net_base.at(2))
        row = self.db.execute("SELECT payload_hash FROM outreach_actions WHERE action_id='refused-r'").fetchone()
        approval = [{"kind": approvals.APPROVAL, "approved_payload_hash": row[0], "decided_at": net_base.at(1)}]
        with mock.patch.object(approvals, "decisions", return_value=approval):
            contacts = networking_page.contacts(overview_page.Reader(self.db))[0]
            total = approvals.refused_count(self.db)
            overview = self.get("/")
        self.assertEqual(sum(contact.refused for contact in contacts), total)
        self.assertEqual(total, 1)
        self.assertRegex(overview, r'1</span>\s*<span class="overview-needs-what">message couldn\'t send</span>\s*'
                                   r'<a class="button overview-needs-go" href="/approvals#refused">Review</a>')


class ApprovalsCounts(net_base.Page, unittest.TestCase):
    def test_8_messages_to_approve_counts_the_cards(self):
        self.open_store(self.root)
        self.action("held-h", "hal-h", "FOLLOWUP", approvals.AWAITING, "Following up.")
        self.db.execute("INSERT INTO outreach_actions VALUES ('bad','linkedin.com/in/bad','FOLLOWUP:bad','{not json','x',?,NULL,NULL)",
                        (approvals.AWAITING,))
        self.db.commit()
        cards, total, _, unreadable = approvals_page.messages(self.db)
        self.assertEqual(unreadable, 1)
        assert_counted_by_label(self, approvals.waiting_count(self.db), cards)
        self.assertEqual(total, len(cards))
        self.assertRegex(self.get("/"), r'1</span>\s*<span class="overview-needs-what">message to approve</span>')


if __name__ == "__main__":
    unittest.main()
