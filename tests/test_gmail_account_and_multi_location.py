"""Owner fixes, 2026-09-19.

1. Gmail is addressed by the candidate mailbox's email (authuser), not /mail/u/0/, and the
   browser prompts permit switching to an already signed-in account (no sign-in). Stored
   legacy /u/0 records still validate.
2. A multi-location role is not excluded for onsite willingness when any place is allowed.

Both tests fail on the pre-fix code.
"""

import datetime as dt
import json
from pathlib import Path
import unittest

import answering_agent
import application_browser
import applications
import external_history
import profile_home
import user_profile


EMAIL = "candidate@example.invalid"
MESSAGE_ID = "19f0a1b2c3d4e5f6"
POLICY = user_profile.load()


class GmailAccountSelectionTest(unittest.TestCase):
    def test_gmail_urls_name_the_candidate_account_and_legacy_records_still_validate(self):
        # The Gmail history SEARCH was deleted (owner, 2026-09-27); saved records and the
        # candidate-account link form it produced are still read, so they still validate.
        text = application_browser.PROMPT
        for phrase in ("authuser=<", "ALREADY signed in", "Never enter a password", "AUTHENTICATION_REQUIRED"):
            self.assertTrue(phrase in text, phrase)
        self.assertFalse("mail.google.com/mail/u/0" in text)
        now = applications.stamp()
        received = (dt.datetime.fromisoformat(now) - dt.timedelta(days=3)).isoformat()
        current = external_history.gmail_url(EMAIL, "all/" + MESSAGE_ID)
        self.assertEqual(current, "https://mail.google.com/mail/?authuser=" + EMAIL + "#all/" + MESSAGE_ID)
        legacy = "https://mail.google.com/mail/u/0/#all/" + MESSAGE_ID
        self.assertEqual(external_history.gmail_account_url(legacy, EMAIL), current)


class MultiLocationOnsiteTest(unittest.TestCase):
    def test_a_multi_office_role_is_excluded_only_when_every_office_is(self):
        """`willing_to_work_onsite` was deleted on 2026-09-22 with the rest of the question
        parser. It decided a multi-office posting by reading place names out of the
        employer's sentence, and it read "Our offices are in Springfield, IL and Austin" as the single place
        "Springfield" because it joined alternatives on "or" and "/" alone.

        The surviving rule is the owner's own, unchanged (2026-09-19): a role with several
        places is excluded only when EVERY place is. It is now applied to the read's
        STRUCTURED `location_options` by ISO code, which is what `judged_preference` vetoes
        from, so that is what is pinned here.
        """
        def verdicts(options):
            return [applications.option_verdict(option, "onsite", POLICY)[0] for option in options]

        nyc_and_sf = [{"city": "New York", "country": "US", "state": "NY"},
                      {"city": "San Francisco", "country": "US", "state": "CA"}]
        self.assertIn("ALLOWED", verdicts(nyc_and_sf),
                      "one allowed office must keep a multi-office posting answerable")
        self.assertEqual(verdicts([{"city": "San Francisco", "country": "US", "state": "CA"}]),
                         ["EXCLUDED"])
        self.assertEqual(verdicts([{"city": "Seattle", "country": "US", "state": "WA"}]),
                         ["EXCLUDED"])
        self.assertEqual(verdicts([{"city": "Bellevue", "country": "US", "state": "WA"}]),
                         ["EXCLUDED"], "the rule is the whole of Washington state, not Seattle")
        self.assertEqual(verdicts([{"city": "Berlin", "country": "DE", "state": None}]),
                         ["ALLOWED"])


if __name__ == "__main__":
    unittest.main()
