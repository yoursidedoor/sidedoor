"""A provider tool that answers in prose has still succeeded (owner, 2026-09-23).

Captured from the live endpoint on 2026-09-24: the transport is SSE (`text/event-stream`), each
packet arrives on a `data:` line, `result` carries only `content`, and `content[0].text` holds a
JSON *string* for read tools. So every tool passes through the final `json.loads(text)`, and the
only thing that varies is whether that text is JSON.

start_campaign is not JSON, and json.loads raised on it AFTER the campaign had started: campaigns
800001 (Avery) and 800002 (Jules) both read back IN_PROGRESS while dispatch recorded
UNCERTAIN_NO_RETRY. No retry is permitted after an uncertain outcome, so a successful send was
denied by the ledger. These tests use the real SSE envelope, not a simplified one.
"""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import io
import json
import unittest
import urllib.request
from unittest.mock import patch

import orchestrator


class _Response(io.BytesIO):
    def __init__(self, body, content_type="text/event-stream"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class ProviderResponseParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = orchestrator.HeyReach.__new__(orchestrator.HeyReach)
        self.client.url = "https://example.invalid/mcp"
        self.client.config = {}

    def call(self, text, tool="start_campaign", envelope=None):
        """Answer one request with the real SSE shape observed from the provider."""
        def urlopen(request, timeout=None):
            identifier = json.loads(request.data.decode())["id"]
            packet = envelope(identifier) if envelope else {
                "result": {"content": [{"type": "text", "text": text}]},
                "id": identifier, "jsonrpc": "2.0"}
            return _Response(("event: message\ndata: " + json.dumps(packet) + "\n\n").encode())
        with patch.object(urllib.request, "urlopen", urlopen):
            return self.client._request(tool, {"campaignId": 800002})

    def test_a_prose_confirmation_is_returned_not_raised(self):
        outcome = self.call("Campaign 800002 started successfully.")
        self.assertEqual(outcome["text"], "Campaign 800002 started successfully.")
        self.assertFalse(outcome["parsed"])

    def test_an_empty_acknowledgment_is_not_a_failure(self):
        self.assertEqual(self.call("")["text"], "")

    def test_a_json_read_body_still_parses(self):
        body = '{"id":800002,"name":"outreach-00000000000000000000000000000002","status":"IN_PROGRESS"}'
        self.assertEqual(self.call(body, tool="get_campaign")["status"], "IN_PROGRESS")

    def test_a_provider_error_is_still_an_error(self):
        def envelope(identifier):
            return {"result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
                    "id": identifier, "jsonrpc": "2.0"}
        with self.assertRaises(RuntimeError):
            self.call("", envelope=envelope)

    def test_a_malformed_envelope_still_fails_without_leaking_the_url(self):
        def urlopen(request, timeout=None):
            return _Response(b"event: message\ndata: {this is not json\n\n")
        with patch.object(urllib.request, "urlopen", urlopen):
            with self.assertRaises(RuntimeError) as caught:
                self.client._request("start_campaign", {"campaignId": 1})
        self.assertNotIn("example.invalid", str(caught.exception))
        self.assertIn("start_campaign failed", str(caught.exception))
