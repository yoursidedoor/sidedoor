"""The relay (relay/ and tests/test_relay.py) is not part of the public source tree. Tests that run
against a local relay import its harness from here and are skipped when it is absent; the license
strings stay the same, so tests that only need a key still run."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = (ROOT / "relay" / "server.py").is_file() and (ROOT / "tests" / "test_relay.py").is_file()

if AVAILABLE:
    try:
        from test_relay import LICENSE, MINE, UNLINKED_LICENSE, RelayHarness
    except ImportError:   # run as tests.<module> from the repo root
        from tests.test_relay import LICENSE, MINE, UNLINKED_LICENSE, RelayHarness
    from relay import server
else:
    LICENSE, UNLINKED_LICENSE, MINE = "LIC-AAAA-1111", "LIC-CCCC-3333", "acct-mine"
    server = None

    class RelayHarness(unittest.TestCase):
        """Stand-in base; every subclass is skipped by `needs_relay`."""

needs_relay = unittest.skipUnless(AVAILABLE, "relay/ is not part of this source tree")
