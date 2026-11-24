"""Q7 a (owner, 2026-11-23): web-page character codes the AI copied go out as plain characters."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import unittest

import outreach_promotion
import reply_execution


class EntitiesAreDecoded(unittest.TestCase):
    def test_a_reply_keeps_its_line_breaks_and_loses_the_codes(self):
        text = "Thank you, Adam.\nThe Product &amp; Engineering team &#8212; great."
        self.assertEqual("Thank you, Adam.\nThe Product & Engineering team, great.", reply_execution.without_dashes(text))
        self.assertEqual("No codes here.\nTwo lines.", reply_execution.without_dashes("No codes here.\nTwo lines."))

    def test_an_outreach_note_loses_the_codes(self):
        self.assertEqual("R&D at AT&T", outreach_promotion.without_dashes("R&amp;D at AT&T"))


if __name__ == "__main__":
    unittest.main()
