"""A failed `codex exec` leaves a diagnosable record (2026-10-02).

reply_worker._execute read a failed run's stderr into memory and dropped it, so the outreach
SUBSCRIPTION_EXECUTOR_FAILED audits of 2026-10-03/04 say only "Held". The posting-judge logs,
which keep stderr, show OpenRouter 402 Payment Required from 2026-10-03T19:15Z on; the fake
executor below prints those captured lines (with a URL and the key planted in them) and exits
1, and the REAL executor path runs.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import outreach_agent
import outreach_promotion as promotion
import reply_worker
from tests import test_outreach_composition as composition

KEY = "sk-or-v1-plantedsecretvalue0123456789"
STDERR = ("OpenAI Codex v0.155.1\nuser\nprompt text\n" + "".join("ERROR: Reconnecting... %d/5\n" % n for n in range(1, 6))
          + ("ERROR: unexpected status 402 Payment Required: This request requires more credits, or fewer max_tokens. "
             "You requested up to 131072 tokens, but can only afford 66659. To increase, visit "
             "https://openrouter.ai/settings/keys?k=" + KEY + " and adjust the key's total limit\n") * 2 + "ERROR: auth header " + KEY + "\n")


class ExecutorFailureRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "stderr.txt").write_text(STDERR)
        (self.directory / "key").write_text(KEY + "\n")
        codex = self.directory / "fake-codex"
        codex.write_text("#!/bin/sh\ncat >/dev/null\ncat '%s' >&2\nexit 1\n" % (self.directory / "stderr.txt"))
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
        self.config = {"codex": str(codex), "model": "stub",
                       "text_model": {"model": "z-ai/glm-5.3", "key_file": str(self.directory / "key")}}

    def test_exit_code_duration_and_redacted_stderr_are_kept(self):
        with self.assertRaises(reply_worker.Held) as raised:
            reply_worker._execute(self.config, "prompt", "{}", 30)
        self.assertEqual(raised.exception.code, "SUBSCRIPTION_EXECUTOR_FAILED")
        record = reply_worker.executor_detail(raised.exception)
        self.assertEqual(record["exit_code"], 1)
        self.assertGreaterEqual(record["duration_seconds"], 0)
        tail = "\n".join(record["stderr_tail"])
        self.assertIn("402 Payment Required", tail)
        self.assertNotIn(KEY, json.dumps(record))
        self.assertNotIn("openrouter.ai", tail)
        self.assertNotIn("prompt text", tail)  # only the error lines are kept
        self.assertLessEqual(len(record["stderr_tail"]), 8)

    def test_composition_failure_audit_carries_the_record(self):
        fixture = composition.CompositionTests("test_a_passed_deadline_never_ends_note_writing")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.assertRaises(reply_worker.Held):
            outreach_agent.compose_draft(promotion.ROOT, fixture.database, self.config, fixture.scaffold(),
                                         reply_worker._execute, composition.NOW)
        failure = json.loads(fixture.database.execute(
            "SELECT record FROM outreach_agent_audit WHERE kind='FAILURE'").fetchone()[0])
        self.assertEqual(failure["executor"]["exit_code"], 1)
        self.assertIn("402 Payment Required", "\n".join(failure["executor"]["stderr_tail"]))


class RedactionTests(unittest.TestCase):
    SECRET = "sk-or-v1-0123456789abcdefSECRET"

    def lines(self, text, prompt=None):
        return reply_worker._redacted_lines(text, [self.SECRET], prompt)

    def test_authorization_headers_in_any_form(self):
        tail = "\n".join(self.lines("ERROR: Authorization: Bearer abcdefghijkl\nERROR: AUTHORIZATION: basic dXNlcjpwYXNz\n"
                                     "ERROR: authorization=xyz123456 next\nERROR: x-api-key: kkkkkkkk1\n"
                                     "ERROR: proxy said bearer Zm9vYmFyYmF6"))
        for value in ("abcdefghijkl", "dXNlcjpwYXNz", "xyz123456", "kkkkkkkk1", "Zm9vYmFyYmF6"):
            self.assertNotIn(value, tail)
        self.assertIn("next", tail)

    def test_scheme_less_urls_and_secret_queries(self):
        tail = "\n".join(self.lines("ERROR: visit openrouter.ai/settings/keys?token=zzzz1 or api.example.com/v1?api_key=qq2\n"
                                     "ERROR: retry key=abc3&x=1 token=ttt4"))
        for value in ("zzzz1", "qq2", "abc3", "ttt4", "openrouter.ai", "api.example.com"):
            self.assertNotIn(value, tail)

    def test_a_key_split_across_lines_drops_the_lines(self):
        tail = self.lines("ERROR: header sk-or-v1-01234\nERROR: 56789abcdefSE\nERROR: 402 Payment Required")
        self.assertEqual(tail, ["ERROR: 402 Payment Required"])

    def test_json_quoted_cookie_prose_and_jwt_secrets(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz"
        tail = "\n".join(self.lines('ERROR: body {"api_key": "tok1", "x": 1}\nERROR: api_key=\'tok2\' next\n'
                                    "ERROR: Cookie: session=tok3; other=tok4\nERROR: set-cookie: sid=tok5\n"
                                    "ERROR: the password is tok6\nERROR: jwt " + jwt))
        for value in ("tok1", "tok2", "tok3", "tok4", "tok5", "tok6", jwt, "eyJ"):
            self.assertNotIn(value, tail)
        self.assertIn("next", tail)

    def test_a_partial_first_line_of_the_bounded_tail_is_dropped(self):
        # 70 KiB of one echoed prompt line with no newline in the tail window: cut mid-line.
        stderr = "OpenAI Codex v0.155.1\nuser\n" + "secretprompt " * 6000 + "\nERROR: unexpected status 402 Payment Required\n"
        tail = self.lines(stderr)
        self.assertEqual(tail, ["ERROR: unexpected status 402 Payment Required"])
        fallback = self.lines("x" * 70000 + "piece of prompt\nmodel: z-ai/glm-5.3\n")
        self.assertEqual(fallback, ["model: z-ai/glm-5.3"])

    def test_model_transcript_lines_are_not_codex_errors(self):
        stderr = ("OpenAI Codex v0.155.1\nuser\nsome prompt\nError: the candidate wrote this\nWarning: also prompt\n"
                  "ERROR: unexpected status 402 Payment Required: This request requires more credits\n"
                  "codex\nERROR: the model typed this in its answer\nWarning: model prose\ntokens used\n12345\n")
        self.assertEqual(self.lines(stderr),
                         ["ERROR: unexpected status 402 Payment Required: This request requires more credits"])

    def test_the_prompt_is_never_stored(self):
        prompt = "Private message from Jane\nERROR: she wrote this line"
        stderr = "OpenAI Codex v0.155.1\nmodel: z-ai/glm-5.3\nuser\nPrivate message from Jane\nERROR: she wrote this line\nmore private text\n"
        tail = "\n".join(self.lines(stderr, prompt))
        self.assertNotIn("Jane", tail)
        self.assertNotIn("she wrote", tail)
        self.assertNotIn("more private text", tail)  # after the "user" echo, in the no-ERROR fallback
        self.assertIn("OpenAI Codex v0.155.1", tail)


if __name__ == "__main__":
    unittest.main()
