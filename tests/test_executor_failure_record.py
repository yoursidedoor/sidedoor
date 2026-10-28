"""Secrets never reach a saved line (`reply_worker.redact_line`, shared with the dashboard's log view).

The codex text executor whose stderr these lines came from is gone (2026-10-27: the text workers
are direct OpenRouter calls, whose failure record is the key-free HTTP status and body); the
line redaction stays for the logs and problem reports that still show program output.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import unittest

import reply_worker


class RedactionTests(unittest.TestCase):
    SECRET = "sk-or-v1-0123456789abcdefSECRET"

    def lines(self, text):
        fragments = reply_worker.secret_fragments([self.SECRET])
        kept = (reply_worker.redact_line(line, [self.SECRET], fragments) for line in text.splitlines())
        return [line for line in kept if line is not None]

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


if __name__ == "__main__":
    unittest.main()
