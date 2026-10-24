"""E8.T2: trial clock, daily heartbeat, 72-hour grace, clock-tamper check (license_clock).

Every relay call goes to `license_fixtures.FakeRelay`; no test reaches yoursidedoor-relay. Times
are a fake clock: `self.true` is the relay's (true) time, `self.local` this computer's clock."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import license_clock
import license_fixtures
import scheduler_control
import scheduler_loop
import setup_steps


UTC = dt.timezone.utc
REAL_HARDWARE_ID = license_clock.hardware_id  # before any test patches it
KEY = "SD-CLOCKTESTKEY"
ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "jinja2"))


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


class ClockCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        signing = license_fixtures.use_test_key()
        signing.start()
        self.addCleanup(signing.stop)
        self.addCleanup(license_clock.stop)
        license_clock._last_check["mono"] = None  # the one-check-a-minute limit starts clear
        self.true = at("2026-10-22T10:00:00")
        self.skew = dt.timedelta(0)          # local clock minus true time
        self.relay = license_fixtures.FakeRelay(lambda: self.true)

    @property
    def local(self):
        return self.true + self.skew

    def activate(self, status="active", trial_end=None):
        self.relay.status, self.relay.trial_end = status, trial_end
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
        self.assertEqual(state["steps"]["license"]["status"], "done")
        # Setup step 5 stored it now by the real clock; re-mark it received at the fake local time.
        license_clock.store_signed(self.home, license_clock._load(self.home)[0], self.local)

    def heartbeat(self):
        return license_clock.check(self.home, "https://relay.test", post=self.relay, now=self.local)

    def view(self):
        return license_clock.view(self.home, self.local)


def strip_clock(home):
    """Both clock-record copies gone: the entitlement file without "clock", no heartbeat file."""
    path = home / setup_steps.ENTITLEMENT_FILE
    stored = json.loads(path.read_text())
    stored.pop("clock", None)
    path.write_text(json.dumps(stored))
    (home / license_clock.HEARTBEAT_FILE).unlink(missing_ok=True)


class WorkedExamples(ClockCase):
    def test_1_trial_counts_down_from_the_signed_dates_then_expires(self):
        self.activate("trialing", trial_end=self.true + dt.timedelta(days=14))
        self.assertEqual(self.view()["label"], "Trial: 14 days left")
        self.true = at("2026-11-04T10:00:00")
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.view()["label"], "Trial: 1 day left")
        self.assertEqual(self.view()["state"], "trial")
        # 11-06: the trial ended and the subscription did not start; the relay signs valid: false.
        self.true, self.relay.status = at("2026-11-06T10:00:00"), "canceled"
        self.assertEqual(self.heartbeat(), "ok")
        value = self.view()
        self.assertEqual((value["state"], value["label"]), ("expired", "Expired"))
        self.assertFalse(value["usable"])
        banner = license_clock.banner(value)
        self.assertEqual(banner["text"], "Sidedoor is paused. Renew to continue.")
        self.assertEqual((banner["action_label"], banner["action_href"], banner["external"]),
                         ("Renew", "https://yoursidedoor.com", True))
        self.assertEqual(banner["second"]["label"], "Bought a new key? Paste it here")

    def test_1b_without_a_signed_trial_end_the_chip_says_trial(self):
        self.activate("trialing")
        self.assertEqual(self.view()["label"], "Trial")
        self.assertIsNone(self.view()["trial_days_left"])

    def test_2_offline_keeps_working_72_hours_then_grace_is_spent(self):
        self.true = at("2026-10-25T09:00:00")
        self.activate()                      # the last successful check
        self.relay.mode = "down"
        self.true = at("2026-10-26T09:00:00")
        self.assertEqual(self.heartbeat(), "offline")
        self.true = at("2026-10-27T08:00:00")
        self.assertEqual(self.heartbeat(), "offline")
        value = self.view()
        self.assertEqual((value["state"], value["hours_left"], value["offline"]), ("active", 25, True))
        self.assertIn("Couldn't check the license server; 25 hours left.", value["text"])
        self.assertIsNone(license_clock.banner(value))
        self.true = at("2026-10-27T10:00:00")  # within 24 hours of valid_until: amber with the hours left
        self.assertEqual(self.view()["label"], "Offline, 23 hours left")
        self.assertEqual(self.view()["tone"], "warning")
        self.true = at("2026-10-28T09:01:00")
        value = self.view()
        self.assertEqual((value["state"], value["label"]), ("grace_spent", "Offline, paused"))
        self.assertFalse(value["usable"])
        banner = license_clock.banner(value)
        self.assertIn("could not reach the license server for 3 days", banner["text"])
        self.assertEqual((banner["action_label"], banner["action_post"]), ("Check again", "/license/check"))
        # [Check again] once the relay answers again: back to Active, a fresh 72 hours.
        self.relay.mode = "ok"
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual((self.view()["state"], self.view()["hours_left"]), ("active", 72))

    def test_offline_includes_a_5xx_and_a_malformed_or_unsigned_answer(self):
        self.activate()
        for mode in ("500", "garbage"):
            self.relay.mode = mode
            self.assertEqual(self.heartbeat(), "offline")
            self.assertTrue(self.view()["offline"])
        before = (self.home / setup_steps.ENTITLEMENT_FILE).read_text()
        self.assertEqual(before, (self.home / setup_steps.ENTITLEMENT_FILE).read_text())  # nothing unsigned stored

    def test_3_clock_set_back_30_days_never_extends_the_trial(self):
        self.activate("trialing", trial_end=self.true + dt.timedelta(days=14))
        self.true = at("2026-11-01T10:00:00")
        self.heartbeat()
        self.assertEqual(self.view()["label"], "Trial: 4 days left")
        self.skew = -dt.timedelta(days=30)   # the user sets the clock back 30 days
        value = self.view()
        self.assertEqual((value["state"], value["problem"], value["label"]), ("license_problem", "clock", "License problem"))
        self.assertEqual(value["text"], license_clock.CLOCK_TEXT)
        self.assertIn("your computer's clock looks wrong", value["text"])
        self.assertIsNone(value["trial_days_left"])
        self.assertFalse(value["usable"])
        self.assertEqual(license_clock.banner(value)["action_post"], "/license/check")  # the way forward
        # Still wrong while online: the relay's signed time shows the 30 days.
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.view()["problem"], "clock")
        # Offline it stays wrong too: no grace counted from the wrong clock.
        self.relay.mode = "down"
        self.heartbeat()
        self.assertEqual(self.view()["problem"], "clock")
        # The way forward: fix the clock, Check again.
        self.skew, self.relay.mode = dt.timedelta(0), "ok"
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.view()["label"], "Trial: 4 days left")

    def test_a_clock_jump_forward_then_back_is_caught_and_a_fixed_clock_recovers(self):
        self.activate()
        self.skew = dt.timedelta(days=200)   # far ahead, offline: grace only shortens
        self.relay.mode = "down"
        self.heartbeat()
        self.assertEqual(self.view()["state"], "grace_spent")
        self.skew = dt.timedelta(days=1)     # set back towards the truth: below the time last seen
        self.assertEqual(self.view()["problem"], "clock")
        self.skew, self.relay.mode = dt.timedelta(0), "ok"
        self.heartbeat()
        self.assertEqual(self.view()["state"], "active")

    def test_a_clock_far_ahead_at_a_check_is_wrong_too(self):
        self.skew = dt.timedelta(days=30)   # a large jump forward, seen by the next successful check
        self.activate()
        self.assertEqual(self.view()["problem"], "clock")

    def test_an_entitlement_without_any_clock_record_is_anchored_on_its_server_time(self):
        self.activate()
        strip_clock(self.home)
        self.assertEqual(self.view()["state"], "checking")
        self.skew = -dt.timedelta(days=2)
        self.assertEqual(self.view()["problem"], "clock")

    def test_grace_counts_server_time_not_a_slightly_slow_local_clock(self):
        self.skew = -dt.timedelta(minutes=50)  # within the tolerance: not "wrong", but not trusted either
        self.true = at("2026-10-25T09:00:00")
        self.activate()
        self.relay.mode = "down"
        self.true = at("2026-10-28T09:01:00")  # local still reads 08:11
        self.heartbeat()
        self.assertEqual(self.view()["state"], "grace_spent")

    def test_a_clock_going_back_past_the_signed_server_time_is_wrong(self):
        self.skew = -dt.timedelta(minutes=50)  # slow but accepted at the check
        self.activate()
        self.skew = -dt.timedelta(minutes=80)  # now more than an hour before the signed server time
        self.assertEqual(self.view()["problem"], "clock")

    def test_an_entitlement_copied_from_another_computer_is_refused(self):
        self.activate()
        with mock.patch.object(license_clock, "hardware_id", lambda: "ANOTHER-COMPUTER"):
            self.assertEqual(self.view()["problem"], "signature")

    def test_4_paused_the_heartbeat_still_runs_on_its_own_timer(self):
        self.activate()
        root = self.home / "root"
        (root / "state").mkdir(parents=True)
        database = sqlite3.connect(root / scheduler_loop.DATABASE)
        scheduler_control.set_user_pause(database, True)
        scheduler_control.set_license_pause(database, True)
        database.commit()
        database.close()
        flags = scheduler_loop.pause_state(root)
        self.assertTrue(flags["user_paused_at"] and flags["license_paused_at"])
        called = threading.Event()
        relay = self.relay

        def post(url, body):
            called.set()
            return relay(url, body)
        heartbeat = scheduler_loop.start_license_heartbeat(root, home=self.home, post=post)
        self.addCleanup(scheduler_loop.stop_license_heartbeat)
        self.assertTrue(called.wait(10), "the heartbeat did not run while paused")
        self.assertTrue(heartbeat.alive())
        self.assertIsNot(heartbeat, scheduler_loop.current())  # not the job loop
        # The timer: due again after a day (an hour while offline), paused or not.
        # The timer runs on monotonic time: due again after a day (an hour while offline).
        clock = {"now": self.local, "mono": 1000.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: clock["now"],
                                        monotonic=lambda: clock["mono"])
        count = len(self.relay.calls)
        self.assertEqual(timer.tick(force=True), "ok")
        self.assertIsNone(timer.tick())
        clock["mono"] += 24 * 3600
        self.assertEqual(timer.tick(), "ok")
        self.relay.mode = "down"
        clock["mono"] += 24 * 3600
        self.assertEqual(timer.tick(), "offline")
        clock["mono"] += 59 * 60
        self.assertIsNone(timer.tick())
        clock["mono"] += 60
        self.assertEqual(timer.tick(), "offline")
        self.assertEqual(len(self.relay.calls), count + 4)

    def test_no_key_no_call(self):
        self.assertEqual(self.heartbeat(), "no_key")
        self.assertEqual(self.relay.calls, [])
        value = self.view()
        self.assertEqual((value["state"], value["label"]), ("no_license", "No license yet"))
        self.assertIsNone(license_clock.banner(value))


class Refusals(ClockCase):
    def test_401_is_a_license_problem_then_expired_after_valid_until(self):
        self.activate()
        self.relay.mode = "invalid"
        self.assertEqual(self.heartbeat(), "invalid")
        value = self.view()
        self.assertEqual((value["state"], value["problem"], value["label"]), ("license_problem", "invalid", "License problem"))
        self.assertFalse(value["offline"])  # a refusal starts no countdown
        banner = license_clock.banner(value)
        self.assertEqual(banner["text"], "Your subscription isn't active. Check Settings > License.")
        self.assertEqual((banner["action_label"], banner["action_href"]), ("Open License", "/settings#license"))
        self.true += dt.timedelta(hours=73)
        self.assertEqual(self.view()["state"], "expired")

    def test_refused_now_runs_one_heartbeat_but_never_after_a_heartbeat_401_for_the_same_key(self):
        self.activate()
        heartbeat = license_clock.Heartbeat(self.home, "https://relay.test", self.relay)
        license_clock._last_check["mono"] = None
        with mock.patch.object(license_clock, "_current", heartbeat):
            self.assertTrue(license_clock.request_check(self.home))
            self.assertTrue(heartbeat._wake.is_set())
            heartbeat._wake.clear()
            self.relay.mode = "invalid"
            self.heartbeat()
            license_clock._last_check["mono"] = None
            self.assertFalse(license_clock.request_check(self.home))
            self.assertFalse(heartbeat._wake.is_set())
            # A new key (or activating again) resets it.
            self.relay.mode = "ok"
            self.activate()
            license_clock._last_check["mono"] = None
            self.assertTrue(license_clock.request_check(self.home))

    def test_a_relay_401_on_linkedin_wakes_the_heartbeat(self):
        self.activate()
        heartbeat = license_clock.Heartbeat(self.home, "https://relay.test", self.relay)
        license_clock._last_check["mono"] = None
        with mock.patch.object(license_clock, "_current", heartbeat):
            setup_steps.linkedin_status(self.home, transport=lambda method, path: (401, {"error": "LICENSE_INVALID"}))
        self.assertTrue(heartbeat._wake.is_set())

    def test_403_shows_activate_and_stays_past_valid_until_never_grace_spent(self):
        self.activate()
        self.relay.mode = "machine"
        self.assertEqual(self.heartbeat(), "machine")
        self.true += dt.timedelta(hours=80)
        self.heartbeat()
        value = self.view()
        self.assertEqual((value["state"], value["problem"]), ("license_problem", "machine"))
        banner = license_clock.banner(value)
        self.assertEqual(banner["text"], "This computer is not activated on your key.")
        self.assertEqual(banner["action_label"], "Activate")

    def test_payment_retrying(self):
        self.activate("past_due")
        value = self.view()
        self.assertEqual((value["state"], value["label"], value["tone"]), ("payment_retrying", "Payment retrying", "warning"))
        self.assertTrue(value["usable"])

    def test_an_edited_entitlement_file_is_a_license_problem_not_an_extension(self):
        self.activate()
        path = self.home / setup_steps.ENTITLEMENT_FILE
        stored = json.loads(path.read_text())
        value = json.loads(stored["entitlement"])
        value["valid_until"] = "2999-01-01T00:00:00Z"
        stored["entitlement"] = json.dumps(value, sort_keys=True, separators=(",", ":"))
        path.write_text(json.dumps(stored))
        view = self.view()
        self.assertEqual((view["state"], view["problem"]), ("license_problem", "signature"))
        self.assertFalse(view["usable"])
        self.assertIsNone(setup_steps.entitlement(self.home))
        self.assertEqual(self.heartbeat(), "ok")  # [Check again] fetches a signed one
        self.assertEqual(self.view()["state"], "active")

    def test_the_key_never_reaches_the_stored_records_or_the_view(self):
        self.activate()
        self.relay.mode = "invalid"
        self.heartbeat()
        for name in (license_clock.HEARTBEAT_FILE, setup_steps.ENTITLEMENT_FILE, setup_steps.STATE_FILE):
            self.assertNotIn(KEY, (self.home / name).read_text())
        self.assertNotIn(KEY, json.dumps(self.view()))


class Shipping(unittest.TestCase):
    @unittest.skipUnless((ROOT / "relay").is_dir(), "relay/ is not part of this source tree")
    def test_the_shipped_public_key_is_the_relays(self):
        lines = (ROOT / "relay" / "license-public-key.txt").read_text().splitlines()
        self.assertEqual(license_clock.PUBLIC_KEY, next(line for line in lines if line and not line.startswith("#")))

    def test_rfc8032_vector_1(self):
        public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        signature = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33ba"
                                  "cc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(license_clock.ed25519_verify(public, b"", signature))
        self.assertFalse(license_clock.ed25519_verify(public, b"x", signature))

    @unittest.skipUnless((ROOT / "relay").is_dir(), "relay/ is not part of this source tree")
    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "the relay's cryptography package is not installed")
    def test_the_relays_own_signer_is_accepted(self):
        from relay import licenses
        with tempfile.TemporaryDirectory() as directory:
            store = licenses.LicenseStore(Path(directory) / "licenses.sqlite3")
            key = store.checkout_completed("cs_test", "sub_test", "cus_test")
            signer = licenses.Signer(licenses.new_signing_key())
            signed = signer.sign(licenses.entitlement(store, key, "sd-machine"))
            with mock.patch.object(license_clock, "PUBLIC_KEY", signer.public_base64()):
                self.assertIsNotNone(license_clock.check_signed(signed, key, "sd-machine"))
                self.assertIsNone(license_clock.check_signed(signed, key, "sd-other-machine"))


class ReviewFixes(ClockCase):
    """The owner-approved review fixes on top of 55b67be (numbers as in the review)."""

    def test_3_a_broken_http_answer_is_offline_not_an_exception(self):
        self.activate()
        self.relay.mode = "incomplete"   # http.client.IncompleteRead, an HTTPException
        self.assertEqual(self.heartbeat(), "offline")
        self.assertTrue(self.view()["offline"])

    def test_4_small_set_backs_every_day_never_extend_the_trial_and_the_heartbeat_keeps_its_day(self):
        self.activate("trialing", trial_end=self.true + dt.timedelta(days=14))
        mono = {"value": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: mono["value"])
        self.relay.mode = "down"            # offline: only our own clock counts
        timer.tick(force=True)
        checks = len(self.relay.calls)
        for day in range(3):
            for _ in range(24):              # an hour at a time, the clock set back 50 seconds each hour
                self.true += dt.timedelta(hours=1)
                self.skew -= dt.timedelta(seconds=50)
                mono["value"] += 3600
                timer.tick()
        # 60 minutes of set-backs in all (within the tolerance, so not "wrong"), and nothing gained:
        self.assertEqual(self.skew, -dt.timedelta(hours=1))
        value = self.view()
        self.assertEqual(value["state"], "grace_spent")  # 72 hours of true time since the last good check
        self.assertGreaterEqual(len(self.relay.calls) - checks, 72)  # hourly offline retries never stopped
        # A restart (new monotonic origin) keeps the persisted time: no extension either.
        restarted = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                            monotonic=lambda: 5.0)
        restarted.tick()
        self.assertEqual(self.view()["state"], "grace_spent")

    def test_4_the_daily_check_runs_on_monotonic_time_even_when_the_clock_goes_back(self):
        self.activate()
        mono = {"value": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: mono["value"])
        timer.tick(force=True)
        count = len(self.relay.calls)
        self.skew = -dt.timedelta(hours=2)
        mono["value"] += 24 * 3600
        timer.tick()
        self.assertEqual(len(self.relay.calls), count + 1)

    def test_5_deleting_one_copy_clears_nothing_and_no_record_extends_nothing(self):
        self.activate()
        self.relay.mode = "invalid"
        self.heartbeat()
        (self.home / license_clock.HEARTBEAT_FILE).unlink()
        self.assertEqual(self.view()["problem"], "invalid")      # the copy inside the entitlement file
        self.relay.mode = "ok"
        self.activate()
        self.relay.mode = "machine"
        self.heartbeat()
        path = self.home / setup_steps.ENTITLEMENT_FILE
        stored = json.loads(path.read_text())
        stored.pop("clock")
        path.write_text(json.dumps(stored))
        self.assertEqual(self.view()["problem"], "machine")      # the heartbeat file's copy
        strip_clock(self.home)
        self.true += dt.timedelta(hours=80)                      # past valid_until, both copies gone
        value = self.view()
        self.assertEqual(value["state"], "checking")
        self.assertFalse(value["usable"])                         # the entitlement's own valid_until, no extension
        self.assertTrue(license_clock.Heartbeat(self.home, post=self.relay).tick() is not None)  # due at once

    def test_6_the_machine_id_is_the_hardwares_salted_hash(self):
        ioreg = mock.Mock(return_value=mock.Mock(stdout='  "IOPlatformUUID" = "564d1a2b-0000-4000-8000-00000000abcd"\n'))
        self.assertEqual(license_clock._read_hardware_id("darwin", run=ioreg), "564D1A2B-0000-4000-8000-00000000ABCD")
        self.assertEqual(ioreg.call_args[0][0], ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])

        class Registry:
            HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_64KEY = "HKLM", 1, 256
            opened = []

            def OpenKey(self, root, path, reserved, access):
                self.opened.append((root, path, access))
                return mock.MagicMock()

            def QueryValueEx(self, handle, name):
                return ("8E1F0C2A-AAAA-BBBB-CCCC-123456789ABC", 1) if name == "MachineGuid" else (None, 0)
        registry = Registry()
        self.assertEqual(license_clock._read_hardware_id("win32", registry=registry),
                         "8e1f0c2a-aaaa-bbbb-cccc-123456789abc")
        self.assertEqual(registry.opened, [("HKLM", r"SOFTWARE\Microsoft\Cryptography", 257)])
        self.assertEqual(license_clock._read_hardware_id("linux", read_file=lambda path: "abc123\n"), "abc123")
        self.assertIsNone(license_clock._read_hardware_id("darwin", run=mock.Mock(side_effect=OSError("no ioreg"))))
        machine = license_clock.machine_id(self.home)
        self.assertTrue(machine.startswith("sdm-"))
        self.assertNotIn(license_fixtures.HARDWARE.lower(), machine.lower())
        self.assertFalse((self.home / setup_steps.MACHINE_FILE).exists())  # nothing copyable written
        with mock.patch.object(license_clock, "hardware_id", lambda: None):
            self.assertEqual(license_clock.machine_id(self.home), machine)  # L5: the id kept from a good read
            with tempfile.TemporaryDirectory() as fresh:
                self.assertTrue(license_clock.machine_id(fresh).startswith("sd-"))  # never read: the random file

    def legacy_install(self):
        """An install activated before E8.T2's hardware id: its entitlement names the random file id."""
        (self.home / "license.key").write_text(KEY + "\n")
        legacy = setup_steps.file_machine_id(self.home)
        signed = license_fixtures.signed(KEY, legacy, self.true)
        (self.home / setup_steps.ENTITLEMENT_FILE).write_text(json.dumps(signed))
        return legacy

    def test_6_an_activated_install_moves_to_the_hardware_id_with_one_reactivation(self):
        self.legacy_install()
        self.assertEqual(self.view()["state"], "checking")  # not stuck, not a problem, while it moves
        self.assertTrue(self.view()["usable"])
        self.assertEqual(self.heartbeat(), "ok")
        url, body = self.relay.calls[-1]
        self.assertTrue(url.endswith("/license/activate"))
        self.assertEqual(body["machine"], license_clock.machine_id(self.home))
        self.assertEqual(self.view()["state"], "active")
        self.heartbeat()
        self.assertTrue(self.relay.calls[-1][0].endswith("/license/heartbeat"))

    def test_6_a_full_key_says_so_once_and_never_retries_by_itself(self):
        legacy = self.legacy_install()
        self.relay.slots = {"test", "other-computer"}   # the old id's slot was freed and both are taken
        self.assertEqual(self.heartbeat(), "activation_limit")
        self.assertEqual(self.relay.calls[-2][1]["replaces"], legacy)   # tried to move the slot first
        self.assertNotIn("replaces", self.relay.calls[-1][1])           # then a plain activation: full
        value = self.view()
        self.assertEqual((value["state"], value["problem"]), ("license_problem", "activation_limit"))
        self.assertIn("already used on 2 computers", value["text"])
        self.assertIn("support@yoursidedoor.com", value["text"])
        self.relay.slots = None
        calls = len(self.relay.calls)
        self.assertEqual(self.heartbeat(), "activation_limit")
        self.assertEqual(len(self.relay.calls), calls)  # one automatic re-activation only
        # The way forward: support frees a computer, the user clicks Activate.
        self.relay.mode = "ok"
        self.assertEqual(setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
                         ["steps"]["license"]["status"], "done")
        license_clock.store_signed(self.home, license_clock._load(self.home)[0], self.local)
        self.assertEqual(self.view()["state"], "active")

    def test_i1_moving_to_the_hardware_id_swaps_the_slot_instead_of_using_a_third(self):
        legacy = self.legacy_install()
        self.relay.slots = {"test", legacy}            # the owner's key: "test" and the file id
        self.assertEqual(self.heartbeat(), "ok")       # no 409
        hardware = license_clock.machine_id(self.home)
        self.assertEqual(self.relay.slots, {"test", hardware})
        self.assertEqual(self.relay.calls[-1][1]["replaces"], legacy)
        self.assertEqual(self.view()["state"], "active")
        self.assertEqual(self.heartbeat(), "ok")       # the heartbeat now runs under the hardware id
        self.assertEqual(self.relay.calls[-1][1]["machine"], hardware)

    def test_la1_activate_after_a_refused_move_takes_the_old_slot(self):
        legacy = self.legacy_install()
        self.relay.mode = "limit"                       # the automatic move is refused
        self.assertEqual(self.heartbeat(), "activation_limit")
        self.assertEqual(license_clock.heartbeat_record(self.home)["reactivated_from"], legacy)
        self.relay.mode, self.relay.slots = "ok", {"other", legacy}   # key full: the old id still holds a slot
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
        self.assertEqual(state["steps"]["license"]["status"], "done")  # [Activate] swaps, no third slot
        self.assertEqual(self.relay.calls[-1][1]["replaces"], legacy)
        self.assertEqual(self.relay.slots, {"other", license_clock.machine_id(self.home)})

    def test_la1_activate_falls_back_to_a_plain_activation_when_the_old_slot_is_gone(self):
        legacy = self.legacy_install()
        self.relay.slots = {"other"}
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
        self.assertEqual(state["steps"]["license"]["status"], "done")
        self.assertEqual(self.relay.calls[-2][1]["replaces"], legacy)   # 409 REPLACES_NOT_ACTIVATED
        self.assertNotIn("replaces", self.relay.calls[-1][1])

    @unittest.skipUnless((ROOT / "relay").is_dir(), "relay/ is not part of this source tree")
    def test_la2_the_runbook_says_how_to_roll_back_past_the_hardware_id(self):
        runbook = (ROOT / "relay" / "RUNBOOK.md").read_text()
        section = runbook[runbook.index("Rolling the app back past the hardware id"):]
        for words in ("86bc3e4", "403", "ACTIVATION_LIMIT", "free-slot --machine sdm-"):
            self.assertIn(words, section[:800])

    def copied_home(self, other):
        """A Sidedoor home copied from another Mac: its entitlement names that Mac's hardware id."""
        (self.home / "license.key").write_text(KEY + "\n")
        (self.home / setup_steps.ENTITLEMENT_FILE).write_text(json.dumps(license_fixtures.signed(KEY, other, self.true)))

    def test_la3_a_home_copied_from_another_mac_never_takes_its_slot(self):
        other = "sdm-" + "a" * 40
        self.copied_home(other)
        self.relay.slots = {other}
        self.assertEqual(self.heartbeat(), "ok")              # a free slot: a plain activation
        self.assertNotIn("replaces", self.relay.calls[-1][1])
        self.assertEqual(self.relay.slots, {other, license_clock.machine_id(self.home)})  # the other Mac keeps its slot
        self.copied_home(other)
        self.relay.slots = {other, "third"}
        state = setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
        self.assertNotIn("replaces", self.relay.calls[-1][1])  # [Activate] too: the normal limit message
        self.assertIn("already used on 2 computers", state["steps"]["license"]["message"])
        self.assertEqual(self.relay.slots, {other, "third"})

    def test_lg1_a_copied_home_refused_with_409_shows_the_limit_and_activate(self):
        other = "sdm-" + "a" * 40
        self.copied_home(other)
        self.relay.slots = {other, "third"}
        self.assertEqual(self.heartbeat(), "activation_limit")
        value = self.view()
        self.assertEqual((value["state"], value["problem"], value["usable"]),
                         ("license_problem", "activation_limit", False))
        self.assertIn("already used on 2 computers", value["text"])
        self.assertIn("support@yoursidedoor.com", value["text"])
        self.assertNotIn("couldn't", value["text"])
        self.assertEqual(value["action"]["label"], "Activate")
        self.assertNotIn("Check again", [action["label"] for action in value["actions"]])
        banner = license_clock.banner(value)
        self.assertEqual((banner["text"], banner["action_label"]), (value["text"], "Activate"))
        self.assertEqual(license_clock.chip(self.home, self.local)["label"], "License problem")
        self.assertFalse(setup_steps.license_done(self.home, self.local))
        # [Activate] once support frees a computer: a plain activation, then Active.
        self.relay.mode, self.relay.slots = "ok", {other}
        self.assertEqual(setup_steps.activate_license(KEY, self.home, "https://relay.test", post=self.relay)
                         ["steps"]["license"]["status"], "done")
        self.assertNotIn("replaces", self.relay.calls[-1][1])
        license_clock.store_signed(self.home, license_clock._load(self.home)[0], self.local)
        self.assertEqual(self.view()["state"], "active")

    def test_la3_only_an_old_file_id_is_replaced(self):
        machine = "sdm-" + "b" * 40
        self.assertEqual(license_clock._replaces("sd-1234", machine), "sd-1234")
        for claimed in ("sdm-" + "a" * 40, machine, None, "", "xx-1"):
            self.assertIsNone(license_clock._replaces(claimed, machine))

    def test_l5_a_failed_hardware_read_is_not_remembered(self):
        reads = iter([None, "564D1A2B-0000-4000-8000-00000000ABCD"])
        with mock.patch.dict(license_clock._hardware, clear=True), \
                mock.patch.object(license_clock, "_read_hardware_id", lambda: next(reads)):
            self.assertIsNone(REAL_HARDWARE_ID())
            self.assertEqual(REAL_HARDWARE_ID(), "564D1A2B-0000-4000-8000-00000000ABCD")

    def test_l5_one_failed_read_keeps_the_hardware_id_so_the_license_stays_put(self):
        self.activate()
        machine = license_clock.machine_id(self.home)
        with mock.patch.object(license_clock, "hardware_id", lambda: None):
            self.assertEqual(license_clock.machine_id(self.home), machine)
            self.assertEqual(self.view()["state"], "active")
            self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.relay.calls[-1][1]["machine"], machine)

    def test_l3_an_activation_401_without_license_invalid_is_offline(self):
        self.relay.mode = "proxy401"
        self.assertEqual(license_clock.activate(KEY, self.home, "https://relay.test", self.relay)[0], "offline")
        self.relay.mode = "invalid"
        self.assertEqual(license_clock.activate(KEY, self.home, "https://relay.test", self.relay)[0], "invalid")

    def test_m1_a_clock_problem_is_checked_again_within_the_hour_and_clears(self):
        self.activate()
        mono = {"value": 0.0}
        timer = license_clock.Heartbeat(self.home, "https://relay.test", self.relay, clock=lambda: self.local,
                                        monotonic=lambda: mono["value"])
        self.skew = dt.timedelta(days=3)               # briefly far ahead at a good check
        timer.tick(force=True)
        self.assertEqual(self.view()["problem"], "clock")
        self.skew = dt.timedelta(0)                    # corrected
        self.assertEqual(self.view()["problem"], "clock")
        calls = len(self.relay.calls)
        mono["value"] += 3600
        self.assertEqual(timer.tick(), "ok")           # within the hour, not the next day
        self.assertEqual(len(self.relay.calls), calls + 1)
        self.assertIsNone(self.view()["problem"])
        mono["value"] += 3600
        self.assertIsNone(timer.tick())                # back to daily once it is right

    def test_6_an_unreachable_relay_retries_the_move_later(self):
        self.legacy_install()
        self.relay.mode = "down"
        self.assertEqual(self.heartbeat(), "offline")
        self.relay.mode = "ok"
        self.assertEqual(self.heartbeat(), "ok")
        self.assertTrue(self.relay.calls[-1][0].endswith("/license/activate"))

    def test_7_a_refused_key_offers_renew_check_again_and_support(self):
        self.activate()
        self.relay.mode = "invalid"
        self.heartbeat()
        value = self.view()
        self.assertEqual([action["label"] for action in value["actions"]], ["Renew", "Check again"])
        self.assertIn("support@yoursidedoor.com", value["support"])

    def test_8_right_after_an_upgrade_it_says_checking_not_offline(self):
        self.activate()
        strip_clock(self.home)
        self.true += dt.timedelta(hours=75)
        value = self.view()
        self.assertEqual((value["state"], value["label"]), ("checking", "Checking your license…"))
        self.assertIsNone(license_clock.banner(value))
        self.relay.mode = "down"
        self.heartbeat()                     # once a check finished, the true state shows
        self.assertEqual(self.view()["state"], "grace_spent")

    def test_11_a_401_without_license_invalid_is_offline(self):
        self.activate()
        self.relay.mode = "proxy401"
        self.assertEqual(self.heartbeat(), "offline")
        self.assertIsNone(self.view()["problem"])

    def test_12_activation_waits_for_a_check_in_flight(self):
        self.activate()
        finished = threading.Event()
        license_clock._check_lock.acquire()
        try:
            worker = threading.Thread(target=lambda: (setup_steps.activate_license(
                KEY, self.home, "https://relay.test", post=self.relay), finished.set()))
            worker.start()
            self.assertFalse(finished.wait(0.3))
        finally:
            license_clock._check_lock.release()
        self.assertTrue(finished.wait(5))
        worker.join(5)

    def test_13_a_truncated_entitlement_is_a_license_problem_with_a_way_forward(self):
        self.activate()
        path = self.home / setup_steps.ENTITLEMENT_FILE
        path.write_text(path.read_text()[:40])
        value = self.view()
        self.assertEqual((value["state"], value["problem"]), ("license_problem", "signature"))
        self.assertEqual(value["action"], license_clock.CHECK_AGAIN)
        path.unlink()
        self.assertEqual(self.view()["state"], "license_problem")
        self.assertEqual(self.heartbeat(), "ok")
        self.assertEqual(self.view()["state"], "active")

    def test_14_one_file_holds_the_entitlement_and_its_clock(self):
        self.activate()
        stored = json.loads((self.home / setup_steps.ENTITLEMENT_FILE).read_text())
        self.assertEqual(stored["clock"]["for"], license_clock._for(stored))
        stale = dict(stored["clock"], seq=0, received_at="2000-01-01T00:00:00Z")
        (self.home / license_clock.HEARTBEAT_FILE).write_text(json.dumps(stale))
        self.assertEqual(self.view()["state"], "active")  # an older copy is never mixed in

    def test_15_a_cache_cleared_by_another_thread_never_raises(self):
        class Clearing(dict):
            def __setitem__(self, name, value):
                super().__setitem__(name, value)
                self.clear()             # another thread cleared it right after
        signed = license_fixtures.signed(KEY, "m", self.true)
        with mock.patch.object(license_clock, "_verified", Clearing()):
            self.assertTrue(license_clock._signature_ok(signed["entitlement"], signed["signature"]))

    def test_16_check_again_never_queues_and_runs_at_most_once_a_minute(self):
        self.activate()
        license_clock._last_check["mono"] = None
        license_clock._check_lock.acquire()
        try:
            self.assertEqual(license_clock.check_now(self.home, post=self.relay), "busy")
        finally:
            license_clock._check_lock.release()
        calls = len(self.relay.calls)
        license_clock._last_check["mono"] = None
        self.assertEqual(license_clock.check_now(self.home, post=self.relay, now=self.local), "ok")
        self.assertEqual(license_clock.check_now(self.home, post=self.relay, now=self.local), "recent")
        self.assertEqual(len(self.relay.calls), calls + 1)
        self.assertIn("License OK", license_clock.check_message("recent", self.view()))

    def test_16_a_lapsed_key_does_not_wake_the_heartbeat_and_401s_share_a_minute(self):
        self.activate()
        heartbeat = license_clock.Heartbeat(self.home, "https://relay.test", self.relay)
        with mock.patch.object(license_clock, "_current", heartbeat):
            license_clock._last_check["mono"] = None
            self.assertTrue(license_clock.request_check(self.home))
            heartbeat._wake.clear()
            self.heartbeat()                                   # a check just ran
            self.assertFalse(license_clock.request_check(self.home))
            self.relay.status = "canceled"
            self.heartbeat()                                   # signed valid: false
            license_clock._last_check["mono"] = None
            self.assertFalse(license_clock.request_check(self.home))
            self.assertFalse(heartbeat._wake.is_set())

    def test_17_a_lone_surrogate_in_the_stored_entitlement_is_a_license_problem_not_a_crash(self):
        self.activate()
        path = self.home / setup_steps.ENTITLEMENT_FILE
        stored = json.loads(path.read_text())
        stored["entitlement"] = stored["entitlement"][:-1] + ',"x":"\ud800"}'  # a real lone surrogate
        path.write_text(json.dumps(stored))
        value = self.view()
        self.assertEqual((value["state"], value["problem"]), ("license_problem", "signature"))
        self.assertIsNone(license_clock.entitlement(self.home))


@unittest.skipUnless((ROOT / "relay").is_dir(), "relay/ is not part of this source tree")
@unittest.skipUnless(importlib.util.find_spec("cryptography"), "the relay's cryptography package is not installed")
class AgainstTheRelay(unittest.TestCase):
    """I1's worked example end to end against relay/server.py on 127.0.0.1 (never the live relay)."""

    def test_slots_test_and_file_id_become_test_and_hardware_id(self):
        from relay import licenses, server
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = licenses.LicenseStore(folder / "relay.sqlite3")
            signer = licenses.Signer(licenses.new_signing_key())
            relay = server.Relay("http://127.0.0.1:9/api/v1", "unused", store, server.AccountStore(folder / "relay.sqlite3"),
                                 licenses_store=store, signer=signer, webhook_secret="whsec_test")
            httpd = server.make_server(relay, "127.0.0.1", 0)
            threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            home = folder / "home"
            home.mkdir()
            key = store.checkout_completed("cs_1", "sub_1", None)
            (home / "license.key").write_text(key + "\n")
            legacy = setup_steps.file_machine_id(home)
            store.activate(key, "test")
            with mock.patch.multiple(license_clock, PUBLIC_KEY=signer.public_base64(), hardware_id=lambda: None):
                self.assertEqual(setup_steps.activate_license(key, home, base)["steps"]["license"]["status"], "done")
            self.assertEqual({machine for machine, _ in store.slots(licenses.digest(key)[:12])}, {"test", legacy})
            license_clock._last_check["mono"] = None
            with mock.patch.multiple(license_clock, PUBLIC_KEY=signer.public_base64(),
                                     hardware_id=lambda: license_fixtures.HARDWARE):
                self.assertEqual(license_clock.check(home, base), "ok")   # no 409
                hardware = license_clock.machine_id(home)
                self.assertEqual({machine for machine, _ in store.slots(licenses.digest(key)[:12])}, {"test", hardware})
                self.assertEqual(license_clock.view(home)["state"], "active")
                self.assertEqual(license_clock.check(home, base), "ok")   # the heartbeat under the hardware id


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class DashboardTest(ClockCase):
    def client(self):
        import dashboard
        from fastapi.testclient import TestClient
        app = dashboard.create_app("s" * 43, 47823, home=self.home, root=self.home, config={},
                                   relay_url="https://relay.test")
        client = TestClient(app, base_url="http://127.0.0.1:47823", follow_redirects=False)
        client.get("/", params={"t": "s" * 43})
        return client

    def finished(self):
        setup_steps.update_state(lambda state: state.update(setup_finished_at="2026-10-21T00:00:00Z"), self.home)

    def test_5_pages_read_the_stored_state_and_never_call_the_relay(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        self.activate("trialing", trial_end=self.true + dt.timedelta(days=14))
        self.finished()
        client = self.client()
        with mock.patch.object(setup_steps, "_post_json", side_effect=AssertionError("relay called on render")):
            for path in ("/", "/settings", "/sent"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200, path)
                self.assertIn("Trial: 14 days left", page.text)
        # Opening the dashboard: one immediate check, on the heartbeat's own thread.
        called = threading.Event()

        def post(url, body):
            called.set()
            return self.relay(url, body)
        count = len(self.relay.calls)
        heartbeat = license_clock.start(self.home, "https://relay.test", post)
        self.assertTrue(called.wait(10))
        heartbeat.stop()
        self.assertEqual(len(self.relay.calls), count + 1)

    def test_banners_health_and_settings_show_each_problem_with_its_way_forward(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        self.activate()
        self.finished()
        client = self.client()
        cases = {
            "invalid": ("Your subscription isn&#39;t active. Check Settings &gt; License.", 'href="/settings#license">Open License'),
            "machine": ("This computer is not activated on your key.", 'href="/settings#license">Activate'),
        }
        for mode, (text, action) in cases.items():
            self.relay.mode = mode
            self.heartbeat()
            page = client.get("/sent").text
            self.assertIn(text, page)
            self.assertIn(action, page)
            self.assertIn(">License problem</a>", page)
        # Expired: Renew opens the checkout in a new tab; "Bought a new key?" leads to Settings.
        self.relay.mode, self.relay.status = "ok", "canceled"
        self.heartbeat()
        page = client.get("/sent").text
        self.assertIn("Sidedoor is paused. Renew to continue.", page)
        self.assertIn('href="https://yoursidedoor.com" target="_blank" rel="noopener noreferrer">Renew', page)
        self.assertIn("Bought a new key? Paste it here", page)
        overview = client.get("/").text
        self.assertIn('href="https://yoursidedoor.com" target="_blank"', overview)

    def test_check_again_runs_the_heartbeat_and_returns_to_the_page(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0) - dt.timedelta(hours=80)
        self.activate()
        self.finished()
        client = self.client()
        page = client.get("/sent").text
        self.assertIn("could not reach the license server for 3 days", page)
        self.assertIn('action="/license/check"', page)
        self.assertIn('name="next" value="/sent"', page)
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        license_clock._last_check["mono"] = None
        with mock.patch.object(setup_steps, "_post_json", self.relay):
            response = client.post("/license/check", data={"next": "/sent"},
                                   headers={"origin": "http://127.0.0.1:47823"})
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/sent"))
        page = client.get("/sent").text
        self.assertNotIn("could not reach the license server", page)
        self.assertIn(">Active</a>", page)
        with mock.patch.object(setup_steps, "_post_json", self.relay):
            for target in ("//evil.test", "/\\evil.test", "https://evil.test", "/pause", "/resume", "/start",
                           "/license/check"):  # review fix 1: GET-able pages only
                outside = client.post("/license/check", data={"next": target},
                                      headers={"origin": "http://127.0.0.1:47823"})
                self.assertEqual(outside.headers["location"], "/")

    def test_2_check_again_answers_in_place_with_a_disabled_button_and_never_queues(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0) - dt.timedelta(hours=80)
        self.activate()
        self.finished()
        client = self.client()
        page = client.get("/sent").text
        self.assertIn('hx-post="/license/check"', page)
        self.assertIn('hx-disabled-elt="find button"', page)
        self.assertIn('<span class="htmx-indicator muted">Checking…</span>', page)
        htmx = {"origin": "http://127.0.0.1:47823", "hx-request": "true"}
        license_clock._last_check["mono"] = None
        self.relay.mode = "down"
        with mock.patch.object(setup_steps, "_post_json", self.relay):
            answer = client.post("/license/check", data={"next": "/sent"}, headers=htmx)
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Still can&#x27;t reach the license server", answer.text)
        self.assertIn('id="chips" hx-swap-oob="true"', answer.text)
        self.true, self.relay.mode = dt.datetime.now(UTC).replace(microsecond=0), "ok"
        license_clock._last_check["mono"] = None
        with mock.patch.object(setup_steps, "_post_json", self.relay):
            answer = client.post("/license/check", data={"next": "/sent"}, headers=htmx)
        self.assertIn("License OK: Active.", answer.text)
        license_clock._check_lock.acquire()
        try:
            license_clock._last_check["mono"] = None
            with mock.patch.object(setup_steps, "_post_json", side_effect=AssertionError("queued")):
                answer = client.post("/license/check", data={"next": "/sent"}, headers=htmx)
        finally:
            license_clock._check_lock.release()
        self.assertIn("already running", answer.text)

    def test_3_a_broken_answer_on_check_again_is_not_a_500(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        self.activate()
        self.finished()
        client = self.client()
        license_clock._last_check["mono"] = None
        self.relay.mode = "incomplete"
        with mock.patch.object(setup_steps, "_post_json", self.relay):
            answer = client.post("/license/check", data={"next": "/"},
                                 headers={"origin": "http://127.0.0.1:47823", "hx-request": "true"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Still can&#x27;t reach", answer.text)

    def test_7_and_9_settings_shows_renew_check_again_support_and_local_times(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        self.activate()
        self.finished()
        self.relay.mode = "invalid"
        self.heartbeat()
        page = self.client().get("/settings").text
        section = page[page.index('id="license"'):]
        section = section[:section.index("</section>")]
        self.assertIn('id="renew-license" href="https://yoursidedoor.com" target="_blank"', section)
        self.assertIn('hx-post="/license/check"', section)
        self.assertIn("Paid and still seeing this? Email support@yoursidedoor.com.", section)
        self.assertNotRegex(section, r"\d{4}-\d\d-\d\dT\d\d:\d\d")  # no raw ISO times
        self.assertRegex(section, r"<dt>Good until</dt><dd>[A-Za-z]*\s?[A-Za-z]* ?\d?\d?,? ?\d\d:\d\d</dd>")

    def test_10_banners_stack_with_a_16px_gutter_on_phones(self):
        css = (ROOT / "dashboard_assets" / "static" / "dashboard.css").read_text()
        block = css[css.index("@media (max-width: 480px)"):]
        block = block[:block.index("\n}\n")]
        self.assertIn(".banners { padding: 0 16px; }", block)
        self.assertIn("flex-direction: column", block)

    def test_la4_a_saved_raw_utc_time_shows_in_local_time(self):
        self.true = dt.datetime.now(UTC).replace(microsecond=0)
        self.activate("trialing")
        raw = "2025-03-04T01:06:27Z"   # a message an older build stored (a far date: no "Sun")
        setup_steps.record(self.home, "license", "done", "Active: Trial. Good until %s." % raw)
        client = self.client()
        for path in ("/settings", "/setup"):
            page = client.get(path).text
            if path == "/setup":   # Settings shows only a refusal's saved line (one status line; owner, 2026-10-24)
                self.assertIn("Active: Trial. Good until Mar 3, 17:06.", page, path)  # America/Los_Angeles
            self.assertNotIn(raw, page, path)


if __name__ == "__main__":
    unittest.main()

