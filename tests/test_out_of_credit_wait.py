"""Out of credit and usage limits are waits with a banner that clears by itself (2026-10-23).

On 2026-10-13 OpenRouter's credits ran out and every 402 was counted as a failed try, which
abandoned five jobs. Now a 402 (a direct OpenRouter call, or a Codex run pointed at OpenRouter whose
terminal error is the 402) is the same capacity wait as the ChatGPT plan's usage limit: no attempt
spent, nothing abandoned, a banner while it lasts.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_connection
import application_recovery
import openai_auth
import outreach_agent
import provider_limits
import reply_worker
import text_model
import user_profile
import workflow_tasks
import test_part_c_read_gating as part_c

CREDITS = b'{"error": {"message": "This request requires more credits, or fewer max_tokens.", "code": 402}}'
REFUSED = lambda url, data, headers, timeout: (402, CREDITS)
ANSWER = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
WORKED = lambda url, data, headers, timeout: (200, ANSWER)
OPENROUTER = ["codex", "exec", "-c", openai_auth.OPENROUTER_PROVIDER, "-"]
CHATGPT = ["codex", "exec", "-c", 'forced_login_method="chatgpt"', "-"]
CODEX_402 = ('ERROR: unexpected status 402 Payment Required: {"error":{"message":"This request requires more '
             'credits, or fewer max_tokens. You requested up to 131072 tokens, but can only afford 66659","code":402}}\n')


def texts(home):
    return [banner["text"] for banner in provider_limits.banners(home)]


class Banners(unittest.TestCase):
    def setUp(self):
        self.home = user_profile.home()
        for name in (provider_limits.CREDIT_WAIT_FILE, provider_limits.CHATGPT_WAIT_FILE):
            provider_limits.clear(name, self.home)
        folder = Path(tempfile.mkdtemp())
        (folder / "key").write_text("sk-synthetic-key-0000")
        self.config = {"text_model": {"model": "z-ai/glm-5.3", "key_file": str(folder / "key")}, "model": "m",
                       "codex": "NEVER_EXECUTED", "timezone": "UTC"}

    def test_a_direct_402_is_a_wait_with_a_banner_that_a_success_clears(self):
        with self.assertRaises(text_model.TextModelHTTPError) as raised:
            text_model.complete(self.config, "prompt", transport=REFUSED)
        limit = provider_limits.wait_of(raised.exception)
        self.assertEqual((limit.code, limit.kind), (provider_limits.CODE, provider_limits.OUT_OF_CREDIT))
        banner = provider_limits.banners(self.home)[0]
        self.assertEqual((banner["tone"], banner["action_href"], banner["external"]),
                         ("error", provider_limits.CREDITS_URL, True))
        self.assertEqual(banner["text"], "Your OpenRouter account is out of credits. Sidedoor's AI work is paused "
                                         "until you add credits.")
        text_model.complete(self.config, "prompt", transport=WORKED)
        self.assertEqual(texts(self.home), [])

    def test_a_passing_key_test_clears_the_banner_at_once(self):
        # Owner, 2026-10-23 (3b): Setup's and Settings' key test and doctor --online clear it right away.
        import doctor
        import setup_steps
        for clear in (lambda: setup_steps.save_and_test_openrouter("sk-or-v1-synthetic-0000", self.config, self.home,
                                                                     transport=WORKED, to_file=True),
                      lambda: doctor.check_openrouter(self.home, self.config, online=True, transport=WORKED)):
            with self.assertRaises(text_model.TextModelHTTPError):
                text_model.complete(self.config, "prompt", transport=REFUSED)
            self.assertEqual(len(texts(self.home)), 1)
            clear()
            self.assertEqual(texts(self.home), [])
        # Doctor's local check (Run checks on the dashboard) makes no call, so it cannot show credits
        # are back: the banner stays until a real call or key test works.
        with self.assertRaises(text_model.TextModelHTTPError):
            text_model.complete(self.config, "prompt", transport=REFUSED)
        doctor.check_openrouter(self.home, self.config)
        self.assertEqual(len(texts(self.home)), 1)

    def test_run_checks_tests_the_key_only_while_the_banner_shows(self):
        import doctor
        import setup_steps
        calls = []

        def counted(url, data, headers, timeout):
            calls.append(url)
            return WORKED(url, data, headers, timeout)
        root = Path(tempfile.mkdtemp())
        quiet = {"status": "ok", "name": "x", "line": "x", "todo": None}
        with patch.object(setup_steps, "saved_openrouter_key", return_value="sk-or-v1-synthetic-0000"), \
                patch.object(doctor, "check_codex", return_value=quiet), \
                patch.object(doctor, "check_browser_runtime", return_value=quiet), \
                patch.object(doctor, "check_chrome", return_value=quiet):
            doctor.dashboard_checks(self.home, root, self.config, False, {}, openrouter_transport=counted)
            self.assertEqual(calls, [])   # no banner: Run checks stays local
            with self.assertRaises(text_model.TextModelHTTPError):
                text_model.complete(self.config, "prompt", transport=REFUSED)
            lines = doctor.dashboard_checks(self.home, root, self.config, False, {}, openrouter_transport=counted)
        self.assertEqual(len(calls), 1)
        [line] = [line for line in lines if line["name"] == "OpenRouter"]
        self.assertIn("works (tested just now)", line["line"])
        self.assertEqual(texts(self.home), [])   # the banner cleared at once

    def test_a_key_under_test_records_nothing(self):
        with self.assertRaises(text_model.TextModelHTTPError) as raised:
            text_model.complete(self.config, "prompt", transport=REFUSED, api_key="sk-candidate-0000")
        self.assertIsNone(provider_limits.wait_of(raised.exception))
        self.assertEqual(texts(self.home), [])

    def test_codex_pointed_at_openrouter_402_is_a_wait_only_as_its_terminal_error(self):
        limit = provider_limits.check_output(1, CODEX_402, command=OPENROUTER)
        self.assertEqual(limit.kind, provider_limits.OUT_OF_CREDIT)
        self.assertEqual(len(texts(self.home)), 1)
        openai_auth.succeeded(OPENROUTER)   # every Codex success path calls this
        self.assertEqual(texts(self.home), [])
        self.assertIsNone(provider_limits.check_output(1, CODEX_402, command=CHATGPT))   # not OpenRouter
        self.assertIsNone(provider_limits.check_output(0, CODEX_402, command=OPENROUTER))
        self.assertIsNone(provider_limits.check_output(1, CODEX_402 + "ERROR: stream disconnected\n", command=OPENROUTER))

    def test_chatgpt_usage_limit_banner_until_success_or_reset(self):
        now = dt.datetime.now(dt.timezone.utc)
        reset = (now + dt.timedelta(hours=2)).astimezone(provider_limits.ZoneInfo(provider_limits.TIMEZONE))
        line = "ERROR: You've hit your usage limit. Try again at %s." % reset.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
        self.assertIsNone(provider_limits.check_output(1, line, command=CHATGPT).kind)
        banner = provider_limits.banners(self.home)[0]
        self.assertEqual(banner["tone"], "warning")
        self.assertTrue(banner["text"].startswith("Your ChatGPT usage limit is reached. Sidedoor pauses its AI work "
                                                  "and picks up again at about "), banner["text"])
        self.assertEqual(banner["action_href"], provider_limits.CHATGPT_USAGE_URL)
        openai_auth.succeeded(OPENROUTER)   # an OpenRouter success says nothing about ChatGPT
        self.assertEqual(len(texts(self.home)), 1)
        openai_auth.succeeded(CHATGPT)
        self.assertEqual(texts(self.home), [])
        provider_limits.check_output(1, line, command=CHATGPT)
        self.assertEqual(provider_limits.banners(self.home, now + dt.timedelta(hours=3)), [])   # the reset passed


class NoTriesSpent(unittest.TestCase):
    setUp = part_c.PartCTests.setUp
    job = part_c.PartCTests.job

    def test_three_402s_spend_no_workflow_attempt(self):
        config = {"text_model": {"model": "z-ai/glm-5.3"}}
        workflow_tasks.initialize(self.database)
        with self.database:
            task = workflow_tasks.enqueue(self.database, "outreach-draft-composition", "a1", "parent",
                                          [{"source": "s", "observed_at": part_c.NOW}], part_c.at(86400), part_c.NOW,
                                          effect="READ", max_attempts=3)
        with patch.object(text_model, "key", return_value="sk-synthetic"), patch.object(text_model, "_post", REFUSED):
            for attempt in range(3):
                claim = workflow_tasks.claim(self.database, task, part_c.at(attempt * 10))
                self.assertEqual(claim["status"], "CLAIMED")
                with self.assertRaises(reply_worker.Held) as raised:   # the real direct call and its wrapping
                    outreach_agent._direct_execute(config, "prompt", "{}", 30, [])
                wait = provider_limits.wait_of(raised.exception)    # what the worker asks (outreach_agent)
                self.assertIsNotNone(wait)
                with self.database:
                    workflow_tasks.provider_wait(self.database, task, claim["token"], part_c.at(attempt * 10 + 1), wait)
                with self.database:
                    self.database.execute("UPDATE workflow_tasks SET next_at=NULL WHERE task_id=?", (task,))
        row = self.database.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task,)).fetchone()
        self.assertEqual((row["status"], row["blocker"], row["attempts"] - (row["max_attempts"] - 3)),
                         ("RETRY_WAIT", provider_limits.CODE, 0))

    def test_the_recovery_coordinator_never_abandons_on_402(self):
        config = dict(part_c.RECOVERY, codex="NEVER_EXECUTED", model="gpt-test", timezone="America/Los_Angeles",
                      text_model={"model": "z-ai/glm-5.3"}, application_browser={"enabled": True, "timeout_seconds": 60})
        adapter = application_connection.HostedDesktopAdapter(self.database, self.root, config, clock=lambda: part_c.NOW)
        with patch.object(text_model, "key", return_value="sk-synthetic"), patch.object(text_model, "_post", REFUSED):
            for _ in range(3):
                application_recovery.recover(self.database, self.job(), adapter, config, "REQUISITION_MISMATCH",
                                             "Resolve the requisition", part_c.NOW)
        job = self.job()
        self.assertEqual((job["state"], job["blocker"]), ("BLOCKED", provider_limits.CODE))
        events = [row[0] for row in self.database.execute("SELECT event FROM application_events WHERE job_key=?", (self.key,))]
        self.assertNotIn("APPLICATION_ABANDONED", events)
        self.assertNotIn("APPLICATION_RECOVERY_DECISION_FAILED", events)


if __name__ == "__main__":
    unittest.main()
