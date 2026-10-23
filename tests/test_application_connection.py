import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import manual_application_connection as manual


class ApplicationConnectionTests(unittest.TestCase):
    def setUp(self):
        # A temporary root: the test never writes into the checkout's reports/.
        base = Path(tempfile.mkdtemp(prefix="application-connection-"))
        self.addCleanup(shutil.rmtree, base, True)
        root = base / self._testMethodName
        self.rehearsal = manual.Rehearsal(root)
        self.rehearsal.enable_hosted()
        self.guards = contextlib.ExitStack()
        self.addCleanup(self.guards.close)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.getaddrinfo", "subprocess.Popen", "builtins.input"):
            self.guards.enter_context(patch(target, manual.forbidden))
        self.addCleanup(self.rehearsal.finish)

    def run_case(self, scenario):
        try:
            scenario(self.rehearsal)
        except Exception as error:
            self.rehearsal.check("regression scenario completed", False,
                                 {"type": type(error).__name__, "message": str(error)})
            raise
        self.assertTrue(self.rehearsal.checks)
        for check in self.rehearsal.checks:
            with self.subTest(check=check["check"]):
                self.assertTrue(check["passed"], json.dumps(check, sort_keys=True))

    def test_cycle_capacity_waits_before_claim_and_resumes(self):
        self.run_case(manual.hosted_capacity)

    def test_three_operation_limit_defers_then_progresses_next_cycle(self):
        # The submit page confirms nothing here, so each job spends inspect/submit/verify: an
        # armed submit whose page confirmed the application is itself the verified submission
        # (owner, 2026-10-06) and would spend no verify operation.
        provider = self.rehearsal.provider
        call = provider.call

        def unconfirmed(operation, payload):
            result = call(operation, payload)
            return dict(result, acknowledged=False) if operation == "submit" else result

        provider.call = unconfirmed
        self.run_case(manual.hosted_three_operation_fairness)

    def test_submission_capacity_is_reserved_before_attempt(self):
        self.run_case(manual.hosted_shared_budget)

    def test_spent_budget_and_original_failure_survive_restart(self):
        self.run_case(manual.hosted_spent_budget_restart)

    def test_cached_inspection_survives_restart_under_exhausted_budget(self):
        self.run_case(manual.hosted_cache_exhausted_budget)


if __name__ == "__main__":
    unittest.main()
