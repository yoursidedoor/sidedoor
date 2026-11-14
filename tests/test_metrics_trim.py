"""The history behind the Metrics page (owner, 2026-11-13, T7 b): rows older than 24 months are deleted once
nothing can need them, and every "All time" number the page shows stays what it was. By outcome: what the
trim deletes, what survives it, and that every reader of the waits table behaves as before."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, the OS credential store off)
import datetime as dt
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import access_cards
import access_outcomes
import applications
import desktop_adapter
import doctor
import metrics_page
import overview_page
import test_e15_join as join

ZONE = ZoneInfo("America/Los_Angeles")


def months_ago(months, now=None, day=15):
    """Noon local time on `day` of the month `months` before now's month, as a UTC ISO time."""
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZONE)
    index = local.year * 12 + local.month - 1 - months
    return dt.datetime(index // 12, index % 12 + 1, day, 12, tzinfo=ZONE).astimezone(dt.timezone.utc).isoformat()


@unittest.skipUnless(join.DEPENDENCIES, "dashboard packages (fastapi, httpx, websockets) are not installed")
class Trim(unittest.TestCase):
    setUp = join.Flow.setUp
    ingest, cycle, advance, claim, row, waits, page, cards, needs, post, app_job = (
        join.Flow.ingest, join.Flow.cycle, join.Flow.advance, join.Flow.claim, join.Flow.row, join.Flow.waits,
        join.Flow.page, join.Flow.cards, join.Flow.needs, join.Flow.post, join.Flow.app_job)

    def old_wall(self, number, months, code="AUTHENTICATION_REQUIRED", host=None, day=15):
        host = host or "old-%s.example.com" % number           # a site of its own: nothing else waits on it
        key = self.ingest(number)
        payload = dict(json.loads(self.row(key)["payload"]), url="https://%s/role-%s" % (host, number))
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), key))
        self.database.commit()
        met = months_ago(months, day=day)
        with self.database:
            self.assertEqual(access_outcomes.record(self.database, self.row(key), code, met), access_outcomes.WAITING)
        return key, met

    def finished_for_good(self, key):
        """The job went on and ended another way (submitted): nothing can stop or reopen it behind this row."""
        self.database.execute("UPDATE application_jobs SET state='SUBMITTED',blocker=NULL,next_at=NULL WHERE job_key=?", (key,))
        self.database.commit()

    def ids(self):
        return [row[0] for row in self.database.execute("SELECT id FROM application_access_waits ORDER BY id")]

    def trim(self):
        with self.database:
            return access_outcomes.trim_history(self.database, applications.stamp(), ZONE)

    def readers(self):
        """What every reader of the table shows: the cards, the counts, the open waits, the site holds, Metrics."""
        reader = overview_page.Reader(self.database)
        fmt = lambda moment: ""
        metrics = metrics_page.read(reader, {})
        return json.dumps({
            "cards": access_cards.view(reader, {}, fmt), "by_job": access_cards.by_job(reader, fmt),
            "needs": self.needs(), "waiting": access_outcomes.waiting(self.database),
            "open": {key: (access_outcomes.open_wait(self.database, key) or {}).get("id") for key in self.keys},
            "holder": {host: (access_outcomes.host_holder(self.database, host, applications.stamp()) or {}).get("job_key")
                       for host in ("lead.example.com", "old-w30.example.com", "old-t25.example.com")},
            "metrics": {name: metrics[name] for name in ("total", "recent", "months", "sites")},
            "doctor": metrics_page.support_line(self.root, {})}, sort_keys=True, default=str)

    def test_only_a_finished_row_older_than_24_months_that_nothing_needs_is_deleted(self):
        waiting30, _ = self.old_wall("w30", 30)                                   # WAITING, 30 months old
        closed25, _ = self.old_wall("c25", 25)                                    # CLOSED 25 months ago, job ended another way
        with self.database:
            self.assertTrue(access_outcomes.abandon(self.database, closed25, months_ago(25, day=16)))
        self.finished_for_good(closed25)
        resolved23, _ = self.old_wall("r23", 23)                                  # RESOLVED, 23 months old
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, resolved23, months_ago(23, day=16)), [resolved23])
        self.finished_for_good(resolved23)
        lead, met = self.old_wall("lead", 25, host="lead.example.com")            # still WAITING, with a job held behind it
        held = self.ingest("held")
        payload = dict(json.loads(self.row(held)["payload"]), url="https://lead.example.com/role-held")
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), held))
        self.database.commit()
        with self.database:
            self.assertEqual(access_outcomes.record(self.database, self.row(held), "AUTHENTICATION_REQUIRED", met,
                                                    reason="ASK_EACH_TIME", source="host:" + lead,
                                                    deadline=access_outcomes.open_wait(self.database, lead)["deadline"]),
                             access_outcomes.WAITING)
        tried25, _ = self.old_wall("t25", 25)                                     # CLOSED 25 months ago; Try again can still reopen it
        with self.database:
            self.assertTrue(access_outcomes.abandon(self.database, tried25, months_ago(25, day=16)))
        live25, _ = self.old_wall("l25", 25)                                      # RESOLVED 25 months ago; its job is still in progress
        with self.database:
            self.assertEqual(access_outcomes.resume(self.database, live25, months_ago(25, day=16)), [live25])
        self.keys = [waiting30, closed25, resolved23, lead, held, tried25, live25]
        only = {key: [wait["id"] for wait in self.waits(key)] for key in self.keys}
        before, rows = self.readers(), self.ids()

        self.assertEqual(self.trim(), 1)
        self.assertEqual(sorted(set(rows) - set(self.ids())), only[closed25])     # only the 25-month CLOSED row is gone
        self.assertEqual(self.readers(), before)                                  # cards, counts, holds, Metrics: as before
        self.assertEqual(self.trim(), 0)                                          # a second run changes nothing more
        self.assertEqual(self.readers(), before)
        # The WAITING rows are untouched, whatever their age; the held job is still behind its lead.
        self.assertEqual([wait["state"] for key in (waiting30, lead, held) for wait in self.waits(key)], ["WAITING"] * 3)
        self.assertEqual(self.waits(held)[0]["source"], "host:" + lead)
        # Kept because it is still needed: Try again reads the closed job's row, and it works after the trim.
        with self.database:
            self.assertTrue(access_outcomes.try_again(self.database, tried25, applications.stamp())["reopened"])
        # The deleted row is still in "All time", from the roll-up; it is in the stored rows no more.
        self.assertEqual(self.database.execute("SELECT site,ending,times FROM application_access_history").fetchall()[0][:],
                         ("old-c25.example.com", "you_closed", 1))

    def test_a_month_is_wholly_kept_or_wholly_dropped_in_the_installs_time_zone(self):
        now = dt.datetime(2026, 11, 13, 20, 0, tzinfo=dt.timezone.utc)
        start = access_outcomes.history_start(now, ZONE)
        self.assertEqual(dt.datetime.fromisoformat(start), dt.datetime(2024, 12, 1, 0, 0, tzinfo=ZONE))   # 24 months: Dec 2024 to Nov 2026
        self.assertEqual(access_outcomes.HISTORY_MONTHS, 24)
        last_dropped = dt.datetime(2024, 11, 30, 23, 30, tzinfo=ZONE).astimezone(dt.timezone.utc).isoformat()   # December already, in UTC
        first_kept = dt.datetime(2024, 12, 1, 0, 30, tzinfo=ZONE).astimezone(dt.timezone.utc).isoformat()
        keys = []
        for number, met in (("drop", last_dropped), ("keep", first_kept)):
            key = self.ingest(number)
            with self.database:
                access_outcomes.record(self.database, self.row(key), "CAPTCHA_REQUIRED", met)
                access_outcomes.resume(self.database, key, met)
            self.finished_for_good(key)
            keys.append(key)
        with self.database:
            self.assertEqual(access_outcomes.trim_history(self.database, now, ZONE), 1)
        self.assertEqual([len(self.waits(key)) for key in keys], [0, 1])
        view = metrics_page.read(overview_page.Reader(self.database), {}, now=now)
        self.assertEqual((view["total"]["met"], [month["key"] for month in view["months"]]), (2, ["2024-12"]))

    # --- "All time" stays true (the owner's pick): the invariant ----------------------------------------

    def all_time(self):
        """Every number the page shows that covers all time, and the lines that say the same counts."""
        view = metrics_page.read(overview_page.Reader(self.database), {})
        page = self.page("/metrics")
        return {"total": view["total"], "sites": {site["site"]: site["counts"] for site in view["sites"]},
                "could_not_show": view["could_not_show"], "doctor": metrics_page.support_line(self.root, {}),
                "page_rows": re.findall(r'<tr><th scope="row">(?:All time|[a-z0-9.-]+\.example)</th>.*?</tr>', page, re.S)}

    def other(self):
        view = metrics_page.read(overview_page.Reader(self.database), {})
        return {"recent": view["recent"], "months": view["months"]}

    def seed_forty_months(self):
        """One site that asked every month for 40 months, each ended a different way, plus two more sites; the jobs
        of the odd months ended for good, the others can still be reopened by Try again or are still in progress."""
        endings = ("you", "you_closed", "closed_7_days", "skip_job", "live")
        keys = []
        for months in range(40, 0, -1):
            host = ("jobs.alpha.example", "careers.beta.example", "apply.gamma.example")[months % 3]
            key, met = self.old_wall("m%d" % months, months, "CAPTCHA_REQUIRED" if months % 4 == 0 else "AUTHENTICATION_REQUIRED", host=host)
            ending = endings[months % len(endings)]
            later = months_ago(months, day=16)
            with self.database:
                if ending in ("you", "live"):
                    access_outcomes.resume(self.database, key, later)
                elif ending == "you_closed":
                    access_outcomes.abandon(self.database, key, later)
                elif ending == "closed_7_days":
                    access_outcomes.expire(self.database, months_ago(months, day=25), only={key})
                else:
                    access_outcomes.posting_closed(self.database, key, later)
            if months % 2 and ending != "live":
                self.finished_for_good(key)
            keys.append(key)
        return keys

    def test_every_all_time_number_is_unchanged_by_the_trim_and_a_new_row_adds_one(self):
        self.seed_forty_months()
        before, others, rows = self.all_time(), self.other(), len(self.ids())
        self.assertEqual(before["total"]["met"], 40)
        self.assertIn("All time: a site asked 40 times", before["doctor"])
        self.assertEqual(len(before["page_rows"]), 4)          # the All time row and the three sites

        deleted = self.trim()
        self.assertGreater(deleted, 3)
        self.assertEqual(len(self.ids()), rows - deleted)      # the old rows are gone
        old = [row for row in self.database.execute("SELECT since,job_key FROM application_access_waits")
               if row[0] < access_outcomes.history_start(applications.stamp(), ZONE)]
        self.assertTrue(all(self.row(job)["state"] not in applications.FINAL or self.row(job)["blocker"] in access_outcomes.CLOSE_CODES
                            for _, job in old))                # what is left that old is still needed
        self.assertEqual(self.all_time(), before)              # every All time number, the page's rows, the doctor line
        self.assertEqual(self.other(), others)                 # Last 30 days and the month rows
        self.assertEqual(sum(row[0] for row in self.database.execute("SELECT times FROM application_access_history")), deleted)
        self.assertLessEqual(self.database.execute("SELECT COUNT(*) FROM application_access_history").fetchone()[0],
                             3 * len(metrics_page.BUCKETS))   # sites times endings, whatever the months

        self.assertEqual(self.trim(), 0)                       # again: nothing more
        self.assertEqual((self.all_time(), self.other()), (before, others))

        key = self.ingest("new")                               # a new one: the right numbers rise by one
        payload = dict(json.loads(self.row(key)["payload"]), url="https://jobs.alpha.example/role-new")
        self.database.execute("UPDATE application_jobs SET payload=? WHERE job_key=?", (json.dumps(payload), key))
        self.database.commit()
        with self.database:
            access_outcomes.record(self.database, self.row(key), "CAPTCHA_REQUIRED", self.now)
        after = self.all_time()
        self.assertEqual((after["total"]["met"], after["total"]["waiting"]), (41, before["total"]["waiting"] + 1))
        self.assertEqual(after["sites"]["jobs.alpha.example"]["met"], before["sites"]["jobs.alpha.example"]["met"] + 1)
        self.assertEqual({site: counts for site, counts in after["sites"].items() if site != "jobs.alpha.example"},
                         {site: counts for site, counts in before["sites"].items() if site != "jobs.alpha.example"})
        self.assertEqual(self.trim(), 0)

    def test_a_trim_that_is_interrupted_counts_nothing_twice_and_loses_nothing(self):
        self.seed_forty_months()
        before, rows = self.all_time(), self.ids()
        forget, calls = access_outcomes._forget, []

        def fails_on_the_third(database, identifier):
            calls.append(identifier)
            if len(calls) == 3:
                raise sqlite3.OperationalError("disk I/O error")   # after that row's count was written
            return forget(database, identifier)
        with mock.patch.object(access_outcomes, "_forget", fails_on_the_third), self.assertRaises(sqlite3.OperationalError):
            self.trim()
        # The count and the delete are one transaction: neither was saved.
        self.assertEqual(self.ids(), rows)
        self.assertEqual(self.database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='application_access_history'").fetchone()[0]
            and self.database.execute("SELECT COALESCE(SUM(times),0) FROM application_access_history").fetchone()[0], 0)
        self.assertEqual(self.all_time(), before)
        self.assertGreater(self.trim(), 3)                     # the next round does it whole
        self.assertEqual(self.all_time(), before)

    def test_the_applications_round_runs_the_trim_and_the_labels_are_as_they_were(self):
        gone, _ = self.old_wall("c26", 26)
        with self.database:
            access_outcomes.abandon(self.database, gone, months_ago(26, day=16))
        self.finished_for_good(gone)
        recent = self.ingest("now")
        with self.database:
            access_outcomes.record(self.database, self.row(recent), "CAPTCHA_REQUIRED", self.now)
        self.cycle()                                           # the ordinary round, beside `expire`
        self.assertEqual((len(self.waits(gone)), len(self.waits(recent))), (0, 1))
        page = self.page("/metrics")
        self.assertIn('<th scope="row">All time</th><td>2</td>', page)
        self.assertIn("By site: All time", page)
        self.assertIn("old-c26.example.com", page)             # a site whose only row was trimmed is still listed
        self.assertNotIn("24 months", page)
        self.assertIn("Show all time", self.page("/metrics?month=" + dt.datetime.now(dt.timezone.utc).astimezone(ZONE).strftime("%Y-%m")))
        self.assertIn("All time: a site asked 2 times (", metrics_page.support_line(self.root, {}))
        source = Path(metrics_page.__file__).read_text() + (Path(metrics_page.__file__).parent / "dashboard_assets/templates/metrics.html").read_text()
        self.assertNotIn("Last 24 months", source)


if __name__ == "__main__":
    unittest.main()
