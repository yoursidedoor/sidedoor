"""Posting judge: a direct OpenRouter call with low reasoning (owner, 2026-10-13). No network."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import applications
import posting_judge
import text_model
import workflow_tasks

NOW = "2026-10-13T20:00:00+00:00"
SECRET = "sk-or-v1-synthetic-secret-0123456789"
CONFIG = {"model": "synthetic", "codex": "synthetic", "text_model": {"model": "z-ai/glm-5.3"}}
TITLE = "Backend Engineer (.NET)"


class DirectPostingJudgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.database = sqlite3.connect(":memory:"); self.database.row_factory = sqlite3.Row
        applications.initialize(self.database); workflow_tasks.initialize(self.database)
        for key, state, url in (("mooby:af18", "SUBMITTED", "https://jobs.ashbyhq.com/mooby/af18"),
                                ("mooby:unresolved:8b1a", "QUEUED", "https://www.linkedin.com/jobs/view/1")):
            self.database.execute("INSERT INTO application_jobs(job_key,employer,requisition,payload,state,updated_at) VALUES (?,?,?,?,?,?)",
                                  (key, "mooby", key.split(":", 1)[1], json.dumps({"company": "Mooby", "role": TITLE, "url": url}), state, NOW))
        self.database.commit()
        self.requests = []
        patcher = mock.patch.object(text_model, "key", return_value=SECRET)
        patcher.start(); self.addCleanup(patcher.stop)

    def judge(self, status, body):
        def transport(url, data, headers, timeout):
            self.requests.append((url, json.loads(data), headers, timeout))
            return status, json.dumps(body).encode()
        job = self.database.execute("SELECT * FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone()
        with mock.patch.object(text_model, "_post", transport):
            return posting_judge.same_job(self.root, self.database, CONFIG, job, NOW)

    def saved_files(self):
        return "".join(path.read_text() for path in (self.root / "reports" / "posting-judge").rglob("*") if path.is_file())

    def test_wrapped_answer_is_judged_with_low_effort_and_usage_recorded(self):
        content = "```json\n" + json.dumps({"verdict": "SAME", "same_as": "mooby:af18", "reason": "same role", "quotes": []}) + "\n```"
        usage = {"prompt_tokens": 492, "completion_tokens": 122, "cost": 0.00058,
                 "completion_tokens_details": {"reasoning_tokens": 1}}
        judged = self.judge(200, {"model": "z-ai/glm-5.3", "usage": usage,
                                  "choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        self.assertEqual((judged["verdict"], judged["same_as"]), ("SAME", "mooby:af18"))
        url, body, headers, timeout = self.requests[0]
        self.assertEqual(url, text_model.BASE_URL + "/chat/completions")
        self.assertEqual((body["model"], body["reasoning"], body["max_tokens"], timeout),
                         ("z-ai/glm-5.3", {"effort": "low"}, 4000, 60))
        value = posting_judge.same_job_packet(self.database, self.database.execute(
            "SELECT * FROM application_jobs WHERE job_key='mooby:unresolved:8b1a'").fetchone())
        self.assertEqual(body["messages"][-1], {"role": "user", "content": posting_judge.same_job_prompt(value)})
        provenance = json.loads(self.database.execute("SELECT provenance FROM same_job_judgments").fetchone()[0])
        self.assertEqual(provenance["usage"], {"prompt_tokens": 492, "completion_tokens": 122, "reasoning_tokens": 1, "cost": 0.00058})
        self.assertEqual((provenance["model"], provenance["reasoning_effort"]), ("z-ai/glm-5.3", "low"))
        self.assertNotIn(SECRET, self.saved_files())

    def test_a_402_is_a_counted_failure_with_its_status_and_never_the_key(self):
        error = {"error": {"message": "This request requires more credits, or fewer max_tokens. key " + SECRET, "code": 402}}
        self.assertIsNone(self.judge(402, error))
        self.assertEqual(self.database.execute("SELECT outcome FROM same_job_attempts").fetchone()[0],
                         "FAILED:TextModelHTTPError:402")
        self.assertIsNone(self.database.execute("SELECT 1 FROM same_job_judgments").fetchone())
        saved = self.saved_files()
        self.assertIn("HTTP 402", saved); self.assertIn("requires more credits", saved)
        self.assertNotIn(SECRET, saved)


class KeyCheckTests(unittest.TestCase):
    def test_a_key_given_to_complete_is_used_instead_of_the_saved_one_and_a_refusal_keeps_its_status(self):
        seen = []

        def transport(url, data, headers, timeout):
            seen.append(headers["Authorization"])
            return 401, json.dumps({"error": {"message": "No auth credentials found", "code": 401}}).encode()
        with mock.patch.object(text_model, "key", side_effect=AssertionError("saved key read")):
            with self.assertRaises(text_model.TextModelHTTPError) as raised:
                text_model.complete(CONFIG, "ping", max_tokens=1, timeout=10, transport=transport, api_key=SECRET)
        self.assertEqual((seen, raised.exception.status), (["Bearer " + SECRET], 401))
        self.assertNotIn(SECRET, raised.exception.record())


if __name__ == "__main__":
    unittest.main()
