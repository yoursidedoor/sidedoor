"""E17.T1: the compose prompt's question guide by contact type and its worked reasoning examples.

Prompt content only. The examples are parsed by their stable marker lines:
`EXAMPLE <contact_type> <n>`, then one line per reasoning step; `Invitation: "..."` and `First message: "..."` or `null`.
The one question is read from the end of the invitation; `Ask:` only labels it.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import re
import unittest
from pathlib import Path

import research_workflow
from outreach_promotion import INVITATION_LIMIT

PROMPT = (Path(__file__).resolve().parent.parent / "outreach-compose-prompt.md").read_text(encoding="utf-8")
STEPS = ("Who", "Knows", "Unlocks", "Why", "Appreciation", "Pitch", "Ask", "Fit", "Invitation", "First message")
# The prompt's own words for what a first contact never contains.
BANNED = ("strong fit", "strongly matches", "perfect match", "well suited", "exactly what you need",
          "I'd value your perspective")
# Ways of asking for a referral, an introduction or a call, checked case-insensitively.
REFERRAL_ASKS = ("refer", "referral", "put my name forward", "put in a word", "recommend me", "introduction",
                 "introduce", "pass my resume along", "pass my résumé along", "a call", "a quick chat", "hop on",
                 "15 minutes")
# A guide row's three parts, in order: what they know, the fitting question, how an answer moves toward a referral.
GUIDE_ROW = r"%s\b[^:]*: knows (\S.*?)\. Ask (\S.*?)\. Toward a referral: (\S.*)"


def guide_row(kind, prompt=None):
    """One contact type's row of the guide as a single line, or None when it is not there."""
    row = re.search(r"(?ms)^  - (%s\b.*?)(?=^  - |^  \S|^\S)" % kind, PROMPT if prompt is None else prompt)
    return " ".join(row.group(1).split()) if row else None


def examples(prompt=None):
    found = []
    for block in re.split(r"^(?=EXAMPLE )", PROMPT if prompt is None else prompt, flags=re.M)[1:]:
        lines = block.split("\n\n", 1)[0].splitlines()
        kind, number = re.fullmatch(r"EXAMPLE (\w+) (\d+)", lines[0]).groups()
        # A list of pairs, so a step line written twice shows up in the order and is not silently overwritten.
        pairs = [tuple(line.split(": ", 1)) for line in lines[1:]]
        found.append({"type": kind, "number": int(number), "order": tuple(label for label, _ in pairs),
                      "steps": dict(pairs)})
    return found


def text(value):
    return None if value == "null" else re.fullmatch(r'"(.*)"', value).group(1)


class ComposePromptGuide(unittest.TestCase):
    def test_guide_has_a_row_for_each_contact_type(self):
        for kind in research_workflow.CONTACT_TYPES:
            self.assertTrue(re.search(r"(?m)^  - %s\b" % kind, PROMPT), kind)

    def test_each_guide_row_keeps_its_three_parts(self):
        for kind in research_workflow.CONTACT_TYPES:
            row = guide_row(kind)
            self.assertTrue(row and re.fullmatch(GUIDE_ROW % kind, row), "%s: %r" % (kind, row))

    def test_unknown_type_keeps_the_generic_question_rule(self):
        flat = " ".join(PROMPT.split())
        self.assertIn("recipient.contact_type is null, missing or not one of the five types", flat)
        self.assertIn("exactly one specific, easy-to-answer question about their work or the role", flat)

    def test_no_em_or_en_dash_anywhere(self):
        self.assertNotIn("—", PROMPT)
        self.assertNotIn("–", PROMPT)


class ComposePromptExamples(unittest.TestCase):
    def test_three_examples_per_type_in_the_stated_order(self):
        found = examples()
        self.assertEqual(sorted((item["type"], item["number"]) for item in found),
                         sorted((kind, number) for kind in research_workflow.CONTACT_TYPES for number in (1, 2, 3)))
        for item in found:
            # Each step label exactly once, in order: a duplicated or missing line fails here.
            self.assertEqual(item["order"], STEPS, item)

    def test_example_texts_obey_the_prompts_mechanical_rules(self):
        for item in examples():
            invitation, first = text(item["steps"]["Invitation"]), text(item["steps"]["First message"])
            label = "%s %d" % (item["type"], item["number"])
            # The prompt's own margin: at least 20 characters under LinkedIn's cap.
            self.assertLessEqual(len(invitation), INVITATION_LIMIT - 20, label)
            # "Hi <first name>," exactly, the first name being the first word of the Who line.
            greeting = re.match(r"Hi ([A-Z][^\W\d_]*), ", invitation)
            self.assertTrue(greeting, label)
            self.assertEqual(greeting.group(1), item["steps"]["Who"].split()[0], label)
            self.assertTrue(invitation.endswith("?"), label)
            # One question for the pair: it ends the invitation, and the first message adds none.
            self.assertEqual((invitation + (first or "")).count("?"), 1, label)
            for phrase in BANNED + REFERRAL_ASKS:
                self.assertNotIn(phrase.lower(), (invitation + " " + (first or "")).lower(), label)

    def test_the_examples_show_the_people_first_shape(self):
        """Owner, 2026-11-12: each example reasons why this person, the one verified appreciation, the pitch,
        the question and the fit, including the steps whose answer is "nothing". Read from the reasoning
        lines' own markers, never from the wording of a note."""
        found = examples()
        for item in found:
            # The Fit line opens with the note's real length.
            self.assertEqual(int(re.match(r"(\d+) characters", item["steps"]["Fit"]).group(1)),
                             len(text(item["steps"]["Invitation"])), item)
        none = lambda step: [item for item in found if item["steps"][step].startswith("none")]
        self.assertTrue(none("Why") and len(none("Why")) < 3, none("Why"))          # nothing verified: no why-you
        self.assertTrue(3 <= len(found) - len(none("Appreciation")) <= 6)          # a few appreciate, most do not
        fits = " ".join(item["steps"]["Fit"] for item in found)
        self.assertIn("the appreciation was dropped first", fits)
        self.assertIn("so the why-you sentence went", fits)
        self.assertIn("unverified lead, left out", " ".join(item["steps"]["Why"] for item in found))
        peers = [item for item in found if item["type"] in ("peer", "senior_peer")]
        own = [item for item in peers if "own experience" in item["steps"]["Ask"]]
        self.assertTrue(own and len(own) < len(peers) - len(own), (len(own), len(peers)))
        self.assertFalse([item for item in found if item not in peers and "own experience" in item["steps"]["Ask"]])

    def test_first_message_never_carries_the_full_name(self):
        for item in examples():
            first = text(item["steps"]["First message"])
            if first is None:
                continue
            label = "%s %d" % (item["type"], item["number"])
            # The recipient's name is the first two capitalised words of the Who line.
            first_name, surname = re.findall(r"\b[A-Z][^\W\d_]*", item["steps"]["Who"])[:2]
            self.assertNotIn(surname, first, label)
            greeting = re.match(r"(?:Hi|Hello|Hey|Dear) ([^,.!]+)", first)
            if greeting:
                self.assertEqual(greeting.group(1), first_name, label)


if __name__ == "__main__":
    unittest.main()
