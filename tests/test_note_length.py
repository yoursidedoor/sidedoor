"""Owner, 2026-10-23: Lambert Nguyen's invitation went out cut mid-sentence ("... focus more on agents
or"). The composer wrote 301 characters; the 300 limit cut the last word. Now an over-long note goes
back to the composer to shorten, and a cut that is still needed ends at a full sentence."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import json
import unittest

import outreach_agent
import outreach_promotion

LAMBERT = ("Hi Lambert, I saw you're the hiring contact for this AI Engineer opening. I'm a senior software "
           "development engineer with 8+ years at Microsoft and Amazon Web Services. I shipped a multi-agent LLM "
           "system that automated launch operations. Would the first few months focus more on agents or reliability?")
SHORT = ("Hi Lambert, I saw you're hiring for the AI Engineer role. I have 8+ years at Microsoft and AWS and "
         "shipped a multi-agent LLM system. Would the first few months focus more on agents or reliability?")


class FitTextTests(unittest.TestCase):
    def test_a_cut_ends_at_the_last_full_sentence(self):
        self.assertEqual(len(LAMBERT), 301)
        cut = outreach_promotion.fit_text(LAMBERT, 300)
        self.assertTrue(cut.endswith("automated launch operations."), cut)
        self.assertLessEqual(len(cut), 300)

    def test_a_cut_never_keeps_a_stub_or_stops_at_an_abbreviation(self):
        stub = "Hi Lambert. " + "I build agents and platforms " * 12 + "and would value your view?"
        self.assertGreater(len(outreach_promotion.fit_text(stub, 300)), 200)
        text = ("I spent 8+ years at Microsoft. on search, e.g. ranking and retrieval, " * 6).strip()
        self.assertFalse(outreach_promotion.fit_text(text, 300).endswith("e.g."))

    def test_text_within_the_limit_is_unchanged(self):
        self.assertEqual(outreach_promotion.fit_text(SHORT, 300), SHORT)

    def test_text_with_no_sentence_end_is_cut_at_a_word(self):
        text = "word " * 80
        cut = outreach_promotion.fit_text(text, 300)
        self.assertLessEqual(len(cut), 300)
        self.assertTrue(cut.endswith("word"))


class ComposerShortensTests(unittest.TestCase):
    CONFIG = {"outreach_agent": {"max_prompt_bytes": 400000}}
    PACKET = {"recipient": {"name": "Lambert"}}

    def output(self, packet, invitation):
        return json.dumps({"output_version": outreach_agent.COMPOSE_VERSION, "source_sha256": outreach_agent.digest(packet),
                           "decision": "COMPOSED", "reason": "", "invitation": invitation,
                           "first_message": "Thanks for connecting.", "proof_point": "p", "question": "q", "facts": []})

    def run_turns(self, invitations):
        prompts, produced = [], iter(invitations)

        def execute(config, prompt, schema, timeout):
            prompts.append(prompt)
            packet = json.loads(prompt.split("UNTRUSTED_SOURCE_JSON:\n", 1)[1])
            packet.pop("source_sha256")
            value = next(produced)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, bytes):   # a malformed answer, returned as is
                return value.decode()
            return self.output(packet, value)
        result, _ = outreach_agent._output_with_repair_turns(
            execute, self.CONFIG, "T", "{}", 60, self.PACKET, self.output(self.PACKET, LAMBERT),
            outreach_agent._validate_composition)
        return result, prompts

    def test_an_over_long_note_goes_back_to_the_composer_with_its_size(self):
        result, prompts = self.run_turns([SHORT])
        self.assertEqual(result["invitation"], SHORT)
        self.assertEqual(len(prompts), 1)
        self.assertIn("invitation is 301 characters; the limit is 300", prompts[0])

    def test_a_composer_that_cannot_shorten_still_sends_a_complete_note(self):
        result, prompts = self.run_turns([LAMBERT + " Thanks.", LAMBERT + " Thanks again."])
        self.assertEqual(len(prompts), 2)   # LENGTH_REPAIR_TURNS, then accepted and cut at a sentence
        cut = outreach_promotion.fit_text(result["invitation"], 300)
        self.assertTrue(cut.endswith("."), cut)

    def test_a_malformed_shortening_turn_keeps_the_long_note(self):
        # Review: turn 1 malformed, turn 2 a failed call; the valid long note from turn 0 is used.
        result, _ = self.run_turns([b"{not json", RuntimeError("provider refused")])
        self.assertEqual(result["invitation"], LAMBERT)

    def test_the_same_text_again_or_a_failed_call_keeps_the_note(self):
        result, _ = self.run_turns([LAMBERT])
        self.assertEqual(result["invitation"], LAMBERT)
        result, _ = self.run_turns([RuntimeError("provider refused")])
        self.assertEqual(result["invitation"], LAMBERT)


if __name__ == "__main__":
    unittest.main()
