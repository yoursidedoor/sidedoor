import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import doctor
import model_calls
import reply_worker
import text_model


class ModelCallTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.path = folder / "orchestrator.sqlite3"
        (folder / "key").write_text("sk-synthetic-key-0000")
        self.config = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(folder / "key")},
                       "model": "z-ai/glm-5.3", "codex": "NEVER_EXECUTED", "timezone": "UTC"}
        previous = model_calls._DATABASE
        self.addCleanup(setattr, model_calls, "_DATABASE", previous)
        model_calls.use(self.path)

    def rows(self):
        database = sqlite3.connect(self.path)
        database.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in database.execute("SELECT * FROM model_calls ORDER BY id")]
        finally:
            database.close()

    def test_direct_calls_record_usage_cost_provider_and_outcome(self):
        answer = {"id": "gen-123", "model": "z-ai/glm-5.3", "provider": "Parasail",
                  "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 9314, "completion_tokens": 325, "cost": 0.0039,
                            "completion_tokens_details": {"reasoning_tokens": 36}}}
        transport = lambda url, data, headers, timeout: (200, json.dumps(answer).encode())
        with model_calls.context("outreach-send-judge", "linkedin.com/in/someone"):
            text_model.complete(self.config, "prompt", transport=transport)
        with self.assertRaises(text_model.TextModelHTTPError):
            text_model.complete(self.config, "prompt", transport=lambda *a: (402, b'{"error": "credits"}'))
        ok, refused = self.rows()
        self.assertEqual((ok["worker"], ok["subject"], ok["route"], ok["provider"], ok["generation_id"]),
                         ("outreach-send-judge", "linkedin.com/in/someone", "openrouter_direct", "Parasail", "gen-123"))
        self.assertEqual((ok["prompt_tokens"], ok["completion_tokens"], ok["reasoning_tokens"], ok["cost"], ok["outcome"]),
                         (9314, 325, 36, 0.0039, "ok"))
        self.assertEqual((refused["outcome"], refused["worker"], refused["cost"]), ("refused", "test_model_calls_c4", None))

    def test_text_workers_record_one_direct_row_per_call_under_their_own_name(self):
        # 2026-10-27: the reply, follow-up and outreach text workers are direct calls (no codex row).
        answer = {"id": "gen-7", "model": "z-ai/glm-5.3", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        with patch.object(text_model, "_post", return_value=(200, json.dumps(answer).encode())):
            reply_worker.text_executor("followup")(self.config, "prompt", "{}", 5)
        import provider_limits
        self.addCleanup(provider_limits.clear, provider_limits.CREDIT_WAIT_FILE)
        with patch.object(text_model, "_post", return_value=(402, b'{"error": "credits"}')):
            with self.assertRaises(reply_worker.Held):
                reply_worker.text_executor("followup")(self.config, "prompt", "{}", 5)
        ok, refused = self.rows()
        self.assertEqual((ok["route"], ok["worker"], ok["prompt_tokens"], ok["outcome"]),
                         ("openrouter_direct", "test_model_calls_c4", 10, "ok"))
        self.assertEqual((refused["route"], refused["worker"], refused["outcome"]),
                         ("openrouter_direct", "test_model_calls_c4", "refused"))

    def test_no_database_records_nothing(self):
        model_calls._DATABASE = None
        model_calls.record("codex", "ok")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
