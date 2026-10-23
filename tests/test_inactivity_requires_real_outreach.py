"""An allowance-only contact (reserved, draft never sent) is not prior outreach.

rw_contacts.historical=1 protects a company's lifetime contact allowance and includes
reserved contacts whose drafts were never sent. On 2026-09-19 it made the inactivity
review browser-check a person the owner confirmed was never contacted.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import sqlite3
import unittest

import engagement
import inactivity_closure


class AllowanceFlagIsNotOutreach(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        engagement.initialize(self.database)
        inactivity_closure.initialize(self.database)
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS history (identity TEXT, recipient TEXT, company TEXT, profile TEXT, evidence TEXT);
            CREATE TABLE IF NOT EXISTS rw_contacts (company TEXT, identity TEXT, name TEXT, historical INTEGER NOT NULL, plan_id TEXT);""")
        self.profile = "https://www.linkedin.com/in/never-contacted-person"
        self.database.execute("INSERT INTO rw_contacts VALUES ('acme', ?, 'never contacted', 1, 'plan')", (self.profile,))

    def test_allowance_only_contact_is_not_prior_outreach(self):
        self.assertIs(inactivity_closure.known_prior(self.database, self.profile), False)

    def test_a_real_sent_record_still_is(self):
        self.database.execute("INSERT INTO history VALUES (?, 'x', 'acme', ?, '{}')", (self.profile, self.profile))
        self.assertIs(inactivity_closure.known_prior(self.database, self.profile), True)


if __name__ == "__main__":
    unittest.main()
