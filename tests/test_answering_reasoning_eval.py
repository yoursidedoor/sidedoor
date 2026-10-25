"""tools/answering_reasoning_eval.py: packets come from the production builder; --dry-run calls nothing."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import answering_agent
import answering_reasoning_eval as tool
import text_model


class DryRunTests(unittest.TestCase):
    def test_dry_run_builds_production_packets_without_network(self):
        out = Path(tempfile.mkdtemp()) / "report.json"
        refuse = AssertionError("network")
        with mock.patch("answering_agent.build_packet", wraps=answering_agent.build_packet) as built, \
                mock.patch("socket.create_connection", side_effect=refuse), \
                mock.patch.object(text_model, "key", side_effect=AssertionError("key read")) as key, \
                mock.patch.object(text_model, "complete", side_effect=AssertionError("model call")) as complete, \
                contextlib.redirect_stdout(io.StringIO()):
            tool.main(["--dry-run", "--cases", "c01,c07", "--out", str(out)])
        self.assertEqual(built.call_count, 2)
        key.assert_not_called()
        complete.assert_not_called()
        packets = {item["id"]: item["packet"] for item in json.loads(out.read_text())["cases"]}
        self.assertEqual(packets["c01"]["packet_version"], answering_agent.PACKET_VERSION)
        self.assertEqual(packets["c01"]["policy"]["location_priority"],
                         ["New York, NY hybrid/onsite", "London hybrid/onsite"])
        self.assertEqual(packets["c07"]["policy"]["onsite_location_exclusions"]["states"], ["WA"])
        self.assertIn("Northwind Analytics", packets["c01"]["candidate_resume"]["text"])

    def test_grading(self):
        result = {"answers": {"a": {"value": "Yes"}, "n": {"value": "150000"}}, "unknown": ["u"]}
        self.assertEqual(tool.grade({"value": "Yes"}, "a", result), (True, "Yes"))
        self.assertEqual(tool.grade({"unknown": True}, "u", result), (True, "UNKNOWN"))
        self.assertEqual(tool.grade({"number": [120000, 175000]}, "n", result), (True, "150000"))
        self.assertFalse(tool.grade({"value": "No"}, "a", result)[0])


if __name__ == "__main__":
    unittest.main()
