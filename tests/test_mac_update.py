"""Mac updates in place (owner, 2026-10-23, A and B), the 30-minute update check and the version on
Overview. Every OS command is a fake: no codesign, spctl, ditto, osascript or open ever runs against a
real app, and the swap helper runs only on folders in a temporary directory."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import hashlib
import importlib.util
import json
import os
import plistlib
import signal
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import mac_update
import update_check

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient
    import dashboard

TEAM = "WX74LWM685"
SHA = "a" * 64
ARM = "https://github.com/yoursidedoor/sidedoor/releases/download/v2.0.0/Sidedoor-mac-apple-silicon.zip"
INTEL = "https://github.com/yoursidedoor/sidedoor/releases/download/v2.0.0/Sidedoor-mac-intel.zip"


def make_app(folder, version="1.1.0", identifier="Sidedoor", name="Sidedoor.app"):
    app = Path(folder) / name
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version, "CFBundleExecutable": "sidedoor"}))
    (app / "Contents" / "MacOS" / "sidedoor").write_text("binary")
    return app


class FakeMac:
    """The runner: answers for codesign, spctl, sysctl, ditto, osascript, ps and lsof, and records."""

    def __init__(self, button="Replace", teams=None, failing=(), ps="", lsof="", unpack=None):
        self.calls, self.button, self.teams, self.failing = [], button, teams or {}, set(failing)
        self.ps, self.lsof, self.unpack = ps, lsof, unpack

    def __call__(self, argv):
        self.calls.append(list(argv))
        tool = Path(argv[0]).name
        key = tool if tool != "codesign" else ("codesign-dv" if argv[1] == "-dv" else "codesign-verify")
        if key in self.failing:
            return 1, "refused"
        if key == "codesign-dv":
            team = self.teams.get(argv[2], TEAM)
            return 0, "Identifier=Sidedoor\nTeamIdentifier=%s\n" % team
        if tool == "sysctl":
            return 0, "1\n"
        if tool == "ditto":
            if self.unpack:
                self.unpack(Path(argv[4]))
            return 0, ""
        if tool == "osascript":
            return 0, "button returned:%s, gave up:false" % self.button
        if tool == "ps":
            return 0, self.ps
        if tool == "lsof":
            return (0, self.lsof) if self.lsof else (1, "")
        return 0, ""

    def used(self, tool):
        return [call for call in self.calls if Path(call[0]).name == tool]


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        mac_update._set()
        self.addCleanup(mac_update._set)
        patcher = mock.patch("version.__version__", "1.1.0")
        patcher.start()
        self.addCleanup(patcher.stop)


class VersionJsonTest(Base):
    def test_interval_is_30_minutes(self):
        self.assertEqual(update_check.INTERVAL_SECONDS, 30 * 60)
        self.assertEqual(update_check.Checker().interval, 30 * 60)

    def test_mac_assets_kept_only_when_valid(self):
        body = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/download", "notes": "",
                "mac": {"arm64": {"url": ARM, "sha256": SHA.upper()},
                        "x86_64": {"url": "https://evil.example/Sidedoor-mac-intel.zip", "sha256": SHA}}}
        parsed = update_check.parse(json.dumps(body))
        self.assertEqual(parsed["mac"], {"arm64": {"url": ARM, "sha256": SHA}})
        body["mac"]["arm64"]["sha256"] = "123"
        self.assertNotIn("mac", update_check.parse(json.dumps(body)))
        # The file without "mac" (and an old app's parse, which picks three keys) is unchanged.
        plain = update_check.parse(json.dumps({"latest": "2.0.0", "download_url": "https://yoursidedoor.com/d"}))
        self.assertEqual(set(plain), {"latest", "download_url", "notes"})

    def test_asset_urls(self):
        self.assertTrue(mac_update.allowed_asset(ARM))
        for url in ("http://github.com/yoursidedoor/sidedoor/releases/download/v1/x.zip",
                    "https://github.com/someone/sidedoor/releases/download/v1/x.zip",
                    "https://github.com/yoursidedoor/sidedoor/releases/tag/v1",
                    "https://release-assets.githubusercontent.com/x.zip"):  # only as GitHub's redirect
            self.assertFalse(mac_update.allowed_asset(url), url)

    def test_redirects_only_to_the_asset_host(self):
        handler = mac_update._AllowedRedirects()
        request = urllib.request.Request(ARM)
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {},
                                                      "https://release-assets.githubusercontent.com/a?b=c"))
        for bad in ("https://evil.example/x.zip", "http://release-assets.githubusercontent.com/x"):
            with self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(request, None, 302, "Found", {}, bad)


class BannerTest(Base):
    def store(self, mac=True):
        body = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/download", "notes": ""}
        if mac:
            body["mac"] = {"arm64": {"url": ARM, "sha256": SHA}, "x86_64": {"url": INTEL, "sha256": SHA}}
        update_check.check(self.home, lambda _url: json.dumps(body).encode())

    def banner(self, can=True):
        with mock.patch.object(mac_update, "can_install", return_value=can), \
                mock.patch.object(mac_update, "RUNNER", FakeMac()):
            return update_check.banner(self.home)

    def test_update_now_beside_download_on_the_mac_app(self):
        self.store()
        banner = self.banner()
        self.assertEqual(banner["forms"][0]["label"], "Update now")
        self.assertEqual(banner["forms"][0]["fields"], {"version": "2.0.0"})
        self.assertEqual(banner["download_href"], "https://yoursidedoor.com/download")

    def test_unchanged_elsewhere_or_without_the_zip(self):
        self.store()
        self.assertNotIn("forms", self.banner(can=False))  # Windows, source, outside Applications
        self.store(mac=False)
        banner = self.banner()
        self.assertNotIn("forms", banner)
        self.assertEqual(banner["action_href"], "https://yoursidedoor.com/download")

    def test_progress_and_failure_show_on_the_banner(self):
        self.store()
        mac_update._set(state="working", version="2.0.0", message="Downloading and checking Sidedoor 2.0.0…")
        banner = self.banner()
        self.assertNotIn("forms", banner)
        self.assertEqual(banner["action_href"], "https://yoursidedoor.com/download")  # never a dead end
        mac_update._set(state="failed", version="2.0.0", message="Sidedoor 2.0.0 didn't install: X.")
        banner = self.banner()
        self.assertEqual((banner["text"], banner["forms"][0]["label"]), ("Sidedoor 2.0.0 didn't install: X.", "Update now"))

    def test_helper_notice_stays_until_dismissed(self):
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 2.0.0 didn't install: why.\n")
        self.assertEqual(self.banner()["text"], "Sidedoor 2.0.0 didn't install: why.")
        self.assertEqual(self.banner()["text"], "Sidedoor 2.0.0 didn't install: why.")  # a second layout build
        self.assertTrue(self.banner()["dismiss_notice"])
        self.assertTrue(update_check.dismiss_notice(self.home))
        self.assertIsNone(self.banner())

    def test_dismissing_the_failure_keeps_the_offer(self):
        self.store()
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 2.0.0 didn't install: why.\n")
        banner = self.banner()
        self.assertTrue(banner["text"].startswith("Sidedoor 2.0.0 didn't install: why. "))
        self.assertEqual((banner["dismiss"], banner["dismiss_notice"]), ("2.0.0", True))  # two separate buttons
        update_check.dismiss_notice(self.home)
        banner = self.banner()
        self.assertEqual(banner["text"], "Sidedoor 2.0.0 is available.")
        self.assertEqual(banner["forms"][0]["label"], "Update now")  # never a dead end
        self.assertNotIn("dismiss_notice", banner)

    def test_a_retry_clears_the_old_notice(self):
        self.store()
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 2.0.0 didn't install: why.\n")
        with mock.patch.object(mac_update, "install", side_effect=lambda *a, **k: __import__("time").sleep(0.2)
                               or {"state": "failed", "message": "new why"}):
            self.assertTrue(mac_update.start_install("2.0.0", self.home))
            self.assertFalse((self.home / mac_update.NOTICE_FILE).exists())
            self.assertEqual(self.banner()["text"], "Downloading and checking Sidedoor 2.0.0…")
            for _ in range(100):
                if mac_update.status().get("state") == "failed":
                    break
                time.sleep(0.01)
        self.assertEqual(self.banner()["text"], "new why")

    def test_helper_notice_goes_once_that_version_is_installed(self):
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 1.1.0 didn't install: why.\n")
        self.assertIsNone(self.banner())  # 1.1.0 is installed now
        self.assertFalse((self.home / mac_update.NOTICE_FILE).exists())


class VerifyTest(Base):
    def setUp(self):
        super().setUp()
        self.app = make_app(self.tmp / "new", version="2.0.0")

    def check(self, runner, **kwargs):
        options = dict(identifier="Sidedoor", team=TEAM, expected_version="2.0.0", installed_version="1.1.0")
        options.update(kwargs)
        return mac_update.verify(self.app, runner=runner, **options)

    def test_accepts_a_good_copy(self):
        runner = FakeMac()
        self.assertIsNone(self.check(runner))
        self.assertEqual([call[1:] for call in runner.used("spctl")], [["--assess", "--type", "execute", str(self.app)]])
        self.assertIn(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.app)], runner.calls)

    def test_each_check_refuses(self):
        self.assertIn("signature", self.check(FakeMac(failing={"codesign-verify"})))
        self.assertIn("notarized", self.check(FakeMac(failing={"spctl"})))
        self.assertIn("same developer", self.check(FakeMac(teams={str(self.app): "ABCDE12345"})))
        self.assertIn("same developer", self.check(FakeMac(), team=None))
        self.assertIn("same app", self.check(FakeMac(), identifier="Other"))
        self.assertIn("not 2.0.1", self.check(FakeMac(), expected_version="2.0.1"))
        self.assertIn("isn't newer", self.check(FakeMac(), installed_version="2.0.0"))

    def test_extract_needs_exactly_one_app(self):
        def one(folder):
            make_app(folder)

        def two(folder):
            make_app(folder)
            (folder / "extra.txt").write_text("x")
        for unpack, ok in ((one, True), (two, False)):
            target = Path(tempfile.mkdtemp(dir=self.tmp))
            app, why = mac_update.extract(self.tmp / "z.zip", target, FakeMac(unpack=unpack))
            self.assertEqual(app is not None, ok, why)
        target = Path(tempfile.mkdtemp(dir=self.tmp))
        self.assertIsNone(mac_update.extract(self.tmp / "z.zip", target, FakeMac(failing={"ditto"}))[0])

    def test_download_checks_the_sha256(self):
        payload = b"zip bytes"
        good = hashlib.sha256(payload).hexdigest()
        fetch = lambda _url, handle, _deadline: handle.write(payload)  # noqa: E731
        path, why = mac_update.download(ARM, good, self.tmp, fetch)
        self.assertEqual((path.read_bytes(), why), (payload, None))
        self.assertIn("checksum", mac_update.download(ARM, SHA, self.tmp, fetch)[1])

        def offline(_url, _handle, _deadline):
            raise OSError("down")
        self.assertIn("connection", mac_update.download(ARM, good, self.tmp, offline)[1])


class InstallTest(Base):
    """B: [Update now] from the running app in (a stand-in for) Applications."""

    def setUp(self):
        super().setUp()
        self.current = make_app(self.tmp / "Applications")
        self.payload = b"the zip"
        self.available = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/d",
                          "mac": {"arm64": {"url": ARM, "sha256": hashlib.sha256(self.payload).hexdigest()}}}
        self.spawned, self.quits = [], []
        for name, value in (("PROGRAM", str(self.current / "Contents" / "MacOS" / "sidedoor")),
                            ("QUIT_DELAY_SECONDS", 0)):
            patcher = mock.patch.object(mac_update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(mac_update, "can_install", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, runner):
        return mac_update.install("2.0.0", self.home, runner=runner, available=self.available,
                                  fetch=lambda _url, handle, _deadline: handle.write(self.payload),
                                  spawn=lambda argv, env: self.spawned.append((argv, env)),
                                  quit_self=lambda: self.quits.append(True))

    def unpack(self, folder):
        make_app(folder, version="2.0.0")

    def test_replace_starts_the_helper_then_quits(self):
        result = self.install(FakeMac(unpack=self.unpack))
        self.assertEqual(result["state"], "replacing")
        (argv, env), = self.spawned
        self.assertEqual(argv[0], "/bin/sh")
        self.assertFalse(argv[1].startswith(str(self.current)))  # never from inside the bundle
        self.assertEqual(env["SD_TARGET"], str(self.current))
        self.assertTrue(env["SD_NEW"].endswith("/unpacked/Sidedoor.app"))
        self.assertTrue(env["SD_TRASH"].endswith("/.Trash/Sidedoor 1.1.0.app"))
        self.assertEqual(env["SD_PIDS"], str(os.getpid()))
        self.assertIn("/sidedoor-download-", env["SD_CLEANUP"])  # the zip goes when the helper ends
        for path in (Path(argv[1]).parent, Path(env["SD_CLEANUP"])):
            self.addCleanup(__import__("shutil").rmtree, path, True)
        for _ in range(100):
            if self.quits:
                break
            time.sleep(0.01)
        self.assertEqual(self.quits, [True])

    def test_not_now_touches_nothing(self):
        runner = FakeMac(button="Not now", unpack=self.unpack)
        self.assertEqual(self.install(runner)["state"], "declined")
        self.assertEqual((self.spawned, self.quits), ([], []))
        self.assertTrue((self.current / "Contents" / "Info.plist").is_file())

    def test_a_failed_check_never_asks_and_never_swaps(self):
        runner = FakeMac(failing={"spctl"}, unpack=self.unpack)
        result = self.install(runner)
        self.assertEqual(result["state"], "failed")
        self.assertIn("notarized", result["message"])
        self.assertIn("Use Download", result["message"])
        self.assertEqual((runner.used("osascript"), self.spawned, self.quits), ([], [], []))

    def test_wrong_version_inside_the_zip_is_refused(self):
        result = self.install(FakeMac(unpack=lambda folder: make_app(folder, version="1.9.0")))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.spawned, [])

    def test_background_thread_records_the_result(self):
        with mock.patch.object(mac_update, "install", return_value={"state": "failed", "message": "why"}):
            self.assertTrue(mac_update.start_install("2.0.0", self.home))
            for _ in range(100):
                if mac_update.status().get("state") == "failed":
                    break
                time.sleep(0.01)
        self.assertEqual(mac_update.status(), {"state": "failed", "version": "2.0.0", "message": "why"})


class HelperTest(Base):
    """The real HELPER script, on temporary folders, with a recorder in place of /usr/bin/open."""

    def setUp(self):
        super().setUp()
        self.old = make_app(self.tmp / "Applications", version="1.1.0")
        self.new = make_app(self.tmp / "download", version="2.0.0")
        (self.tmp / "user" / ".Trash").mkdir(parents=True)
        self.opened = self.tmp / "opened.txt"
        self.recorder = self.tmp / "open"
        self.recorder.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >>"%s"\n' % self.opened)
        # Never the real osascript: a stand-in that records the dialog and clicks Show in Finder.
        self.dialogs = self.tmp / "dialogs.txt"
        self.dialog = self.tmp / "osascript"
        self.dialog.write_text('#!/bin/sh\nfor a; do last=$a; done\nprintf "%%s\\n" "$last" >>"%s"\n'
                               'echo "button returned:Show in Finder, gave up:false"\n' % self.dialogs)
        self.dialog.chmod(0o755)
        self.recorder.chmod(0o755)
        patcher = mock.patch.object(mac_update, "USER_HOME", str(self.tmp / "user"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_helper(self, open_command=None, pids=(), bundles=(), **env):
        captured = []
        mac_update.swap(self.home, self.new, self.old, "1.1.0", "2.0.0", list(pids), list(bundles) or [self.old],
                        spawn=lambda argv, environment: captured.append((argv, environment)),
                        open_command=open_command or str(self.recorder))
        argv, environment = captured[0]
        environment["SD_DIALOG"] = str(self.dialog)
        environment.update(env)
        code = subprocess.run(argv, env=environment, timeout=60).returncode
        self.assertFalse(Path(argv[1]).exists())  # the helper removes its own folder
        return code

    def opens(self):
        return self.opened.read_text().splitlines() if self.opened.exists() else []

    def log(self):
        return (self.home / "logs" / mac_update.LOG_NAME).read_text()

    def test_swap_moves_old_to_trash_and_opens_new(self):
        sleeper = subprocess.Popen(["/bin/sleep", "1"])
        self.assertEqual(self.run_helper(pids=[sleeper.pid]), 0)
        sleeper.wait()
        trashed = self.tmp / "user" / ".Trash" / "Sidedoor 1.1.0.app"
        self.assertEqual(mac_update.bundle_version(trashed), "1.1.0")  # never deleted
        self.assertEqual(mac_update.bundle_version(self.old), "2.0.0")
        self.assertFalse(self.new.exists())
        self.assertEqual(self.opens(), [str(self.old)])
        self.assertFalse((self.home / mac_update.NOTICE_FILE).exists())
        self.assertIn("opened the new Sidedoor 2.0.0", self.log())

    def test_waits_for_jobs_running_from_the_bundle(self):
        job = subprocess.Popen(["/bin/sh", "-c", "sleep 3; true", str(self.old / "Contents" / "MacOS" / "sidedoor")])
        self.addCleanup(job.wait)
        self.addCleanup(job.kill)
        started = time.monotonic()
        self.assertEqual(self.run_helper(SD_JOB_WAIT="30"), 0)
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(mac_update.bundle_version(self.old), "2.0.0")

    def test_a_job_that_outlasts_the_wait_leaves_the_old_app(self):
        job = subprocess.Popen(["/bin/sh", "-c", "sleep 20; true", str(self.old / "Contents" / "MacOS" / "sidedoor")])
        self.addCleanup(job.wait)
        self.addCleanup(job.kill)
        self.assertEqual(self.run_helper(SD_JOB_WAIT="2"), 1)
        self.assertEqual(mac_update.bundle_version(self.old), "1.1.0")
        self.assertEqual(self.opens(), [str(self.old)])  # reopened
        self.assertIn("still running", (self.home / mac_update.NOTICE_FILE).read_text())

    def test_open_failure_puts_the_old_app_back(self):
        self.assertEqual(self.run_helper(open_command="/usr/bin/false"), 1)
        self.assertEqual(mac_update.bundle_version(self.old), "1.1.0")
        self.assertEqual(mac_update.bundle_version(self.new), "2.0.0")
        self.assertIn("wouldn't open", (self.home / mac_update.NOTICE_FILE).read_text())
        self.assertIn("put the old app back", self.log())

    def fake_mv(self, fail_source, partial=True, refuse_aside=False, fail_destination="*"):
        """/bin/mv, except a move of `fail_source` fails after leaving a partial copy at its
        destination (a cross-volume copy cut short), and with `refuse_aside` any move of a copy out
        of the way fails too."""
        script = self.tmp / "mv"
        script.write_text("""#!/bin/sh
if [ "$1" = "%s" ]; then case "$2" in %s)
  %s
  exit 1 ;; esac
fi
case "$2" in *"(not installed)"*) %s ;; esac
exec /bin/mv "$@"
""" % (fail_source, fail_destination if fail_destination == "*" else '"%s"' % fail_destination, 'mkdir -p "$2/Contents/MacOS"' if partial else ":", "exit 1" if refuse_aside else ":"))
        script.chmod(0o755)
        return str(script)

    def assert_old_back_not_nested(self):
        self.assertEqual(mac_update.bundle_version(self.old), "1.1.0")
        self.assertEqual([path.name for path in self.old.iterdir()], ["Contents"])  # nothing nested inside
        self.assertEqual(sorted(path.name for path in (self.old / "Contents").iterdir()), ["Info.plist", "MacOS"])

    def test_new_move_cut_short_puts_the_old_app_back_not_nested(self):
        code = self.run_helper(SD_MV=self.fake_mv(str(self.new)))
        self.assertEqual(code, 1)
        self.assert_old_back_not_nested()
        aside = [path.name for path in (self.tmp / "user" / ".Trash").iterdir()]
        self.assertEqual(len(aside), 1)
        self.assertIn("(not installed)", aside[0])  # the partial copy, moved out of the way
        self.assertIn("was not changed", (self.home / mac_update.NOTICE_FILE).read_text())
        self.assertEqual(self.opens(), [str(self.old)])

    def test_open_failure_with_a_failed_move_back_puts_the_old_app_back(self):
        code = self.run_helper(open_command="/usr/bin/false", SD_MV=self.fake_mv(str(self.old), partial=False,
                                                                               fail_destination=str(self.new)))
        self.assertEqual(code, 1)
        self.assert_old_back_not_nested()
        aside = [path for path in (self.tmp / "user" / ".Trash").iterdir() if "(not installed)" in path.name]
        self.assertEqual(mac_update.bundle_version(aside[0]), "2.0.0")  # the new copy, kept in the Bin
        self.assertIn("was not changed", (self.home / mac_update.NOTICE_FILE).read_text())

    def test_when_nothing_can_go_back_the_notice_says_where_the_old_app_is(self):
        code = self.run_helper(SD_MV=self.fake_mv(str(self.new), refuse_aside=True))
        self.assertEqual(code, 1)
        trashed = self.tmp / "user" / ".Trash" / "Sidedoor 1.1.0.app"
        self.assertEqual(mac_update.bundle_version(trashed), "1.1.0")  # left where it is, never nested
        self.assertFalse((self.old / trashed.name).exists())
        notice = (self.home / mac_update.NOTICE_FILE).read_text()
        self.assertNotIn("not changed", notice)
        self.assertIn("Your old Sidedoor is at %s" % trashed, notice)
        # Nothing of Sidedoor runs to show the notice: the helper says it on screen and, on Show in
        # Finder, reveals the old app. Nothing is reopened (the target is not the old app).
        self.assertIn("Your previous Sidedoor is in the Bin. Drag it back into", self.dialogs.read_text())
        self.assertEqual(self.opens(), ["-R %s" % trashed])

    def test_success_clears_an_earlier_notice(self):
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 2.0.0 didn't install: earlier.\n")
        self.assertEqual(self.run_helper(), 0)
        self.assertFalse((self.home / mac_update.NOTICE_FILE).exists())

    def test_trash_refusal_parks_the_old_app_in_the_home(self):
        self.assertEqual(self.run_helper(SD_TRASH=str(self.tmp / "no-such-folder" / "x.app")), 0)
        parked = self.home / mac_update.PREVIOUS_VERSIONS / "Sidedoor 1.1.0.app"
        self.assertEqual(mac_update.bundle_version(parked), "1.1.0")
        self.assertEqual(mac_update.bundle_version(self.old), "2.0.0")

    def test_a_dashboard_that_never_quits_changes_nothing(self):
        sleeper = subprocess.Popen(["/bin/sleep", "20"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        self.assertEqual(self.run_helper(pids=[sleeper.pid], SD_WAIT="1"), 1)
        self.assertEqual(mac_update.bundle_version(self.old), "1.1.0")
        self.assertEqual(self.opens(), [])  # it is still open: nothing to reopen
        self.assertFalse(self.dialogs.exists())
        self.assertIn("didn't quit", (self.home / mac_update.NOTICE_FILE).read_text())


class OfferMoveTest(Base):
    """A: a downloaded copy opened from outside Applications."""

    def setUp(self):
        super().setUp()
        self.applications = self.tmp / "Applications"
        self.applications.mkdir()
        self.copy = make_app(self.tmp / "Downloads", version="1.1.0")
        self.spawned, self.kills = [], []
        for name, value in (("PROGRAM", str(self.copy / "Contents" / "MacOS" / "sidedoor")), ("PLATFORM", "darwin"),
                            ("FROZEN", True), ("APPLICATIONS", self.applications),
                            ("USER_HOME", str(self.tmp / "user"))):
            patcher = mock.patch.object(mac_update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"SIDEDOOR_STARTED_AT_LOGIN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def offer(self, runner, running=False):
        return mac_update.offer_move(self.home, running=lambda: running, port=47823, runner=runner,
                                     spawn=lambda argv, env: self.spawned.append(env),
                                     kill=lambda pid, number: self.kills.append((pid, number)))

    def test_replace_quits_the_running_old_copy_and_hands_over(self):
        old = make_app(self.applications, version="1.0.0")
        command = "%s serve --start" % (old / "Contents" / "MacOS" / "sidedoor")
        runner = FakeMac(lsof="4242\n", ps=command)
        self.assertTrue(self.offer(runner, running=True))
        dialog = runner.used("osascript")[0]
        self.assertIn("Replace Sidedoor 1.0.0 with 1.1.0", dialog[3])
        self.assertEqual(dialog[4:], ["Not now", "Replace"])
        env, = self.spawned
        self.assertEqual((env["SD_TARGET"], env["SD_NEW"]), (str(old), str(self.copy)))
        self.assertEqual(set(env["SD_PIDS"].split()), {str(os.getpid()), "4242"})
        self.assertIn(str(old), env["SD_BUNDLES"].splitlines())
        self.assertEqual(self.kills, [(4242, signal.SIGTERM)])

    def test_not_now_keeps_todays_banner(self):
        make_app(self.applications, version="1.0.0")
        self.assertFalse(self.offer(FakeMac(button="Not now")))
        self.assertEqual((self.spawned, self.kills), ([], []))

    def test_no_installed_copy_offers_a_move(self):
        runner = FakeMac(button="Move")
        self.assertTrue(self.offer(runner))
        self.assertIn("Move Sidedoor to", runner.used("osascript")[0][3])
        self.assertEqual(self.spawned[0]["SD_HAD_OLD"], "0")
        self.assertEqual(self.kills, [])

    def test_a_newer_installed_copy_is_opened_instead(self):
        installed = make_app(self.applications, version="1.2.0")
        runner = FakeMac(button="Open Sidedoor 1.2.0")
        self.assertTrue(self.offer(runner))
        self.assertIn("newer than this copy", runner.used("osascript")[0][3])
        self.assertEqual(runner.used("open"), [["/usr/bin/open", str(installed)]])
        self.assertEqual(self.spawned, [])

    def test_unverified_copy_is_never_moved(self):
        make_app(self.applications, version="1.0.0")
        runner = FakeMac(failing={"spctl"})
        self.assertFalse(self.offer(runner))
        self.assertEqual(len(runner.used("osascript")), 1)  # the message only, no Replace dialog
        self.assertIn("notarized", runner.used("osascript")[0][3])
        self.assertEqual(self.spawned, [])

    def test_another_developers_installed_copy_is_not_replaced(self):
        old = make_app(self.applications, version="1.0.0")
        self.assertFalse(self.offer(FakeMac(teams={str(old): "ZZZZZ99999"})))
        self.assertEqual(self.spawned, [])

    def test_inside_applications_windows_and_source_do_nothing(self):
        runner = FakeMac()
        with mock.patch.object(mac_update, "FROZEN", False):
            self.assertFalse(self.offer(runner))
        with mock.patch.object(mac_update, "PLATFORM", "win32"):
            self.assertFalse(self.offer(runner))
        with mock.patch.object(mac_update, "PROGRAM", "/Applications/Sidedoor.app/Contents/MacOS/sidedoor"):
            self.assertFalse(self.offer(runner))
        self.assertEqual(runner.calls, [])


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class DashboardTest(Base):
    def client(self):
        client = TestClient(dashboard.create_app("test-token-value", 54321, home=self.home),
                            base_url="http://127.0.0.1:54321", follow_redirects=False)
        client.get("/", params={"t": "test-token-value"})
        return client

    def test_overview_shows_the_installed_version(self):
        self.assertIn("Sidedoor 1.1.0</p>", self.client().get("/").text)

    def test_banner_renders_update_now_and_download(self):
        body = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/download", "notes": "",
                "mac": {"arm64": {"url": ARM, "sha256": SHA}, "x86_64": {"url": INTEL, "sha256": SHA}}}
        update_check.check(self.home, lambda _url: json.dumps(body).encode())
        with mock.patch.object(mac_update, "can_install", return_value=True), \
                mock.patch.object(mac_update, "RUNNER", FakeMac()), \
                mock.patch.object(mac_update, "start_install", return_value=True) as start:
            client = self.client()
            page = client.get("/applications").text
            self.assertIn('action="/update/install"', page)
            self.assertIn(">Update now</button>", page)
            self.assertIn('href="https://yoursidedoor.com/download" target="_blank"', page)
            response = client.post("/update/install", data={"version": "2.0.0", "next": "/applications"},
                                   headers={"origin": "http://127.0.0.1:54321"})
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/applications"))
        self.assertEqual(start.call_args[0][0], "2.0.0")

    def test_failure_message_has_its_own_dismiss(self):
        body = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/download", "notes": "",
                "mac": {"arm64": {"url": ARM, "sha256": SHA}, "x86_64": {"url": INTEL, "sha256": SHA}}}
        update_check.check(self.home, lambda _url: json.dumps(body).encode())
        (self.home / mac_update.NOTICE_FILE).write_text("Sidedoor 2.0.0 didn't install: why.\n")
        origin = {"origin": "http://127.0.0.1:54321"}
        with mock.patch.object(mac_update, "can_install", return_value=True), \
                mock.patch.object(mac_update, "RUNNER", FakeMac()):
            client = self.client()
            page = client.get("/applications").text
            self.assertIn('name="notice" value="1"', page)
            self.assertIn(">Hide the message</button>", page)
            client.post("/update/dismiss", data={"notice": "1", "next": "/applications"}, headers=origin)
            page = client.get("/applications").text
        self.assertNotIn("didn't install", page)
        self.assertIn(">Update now</button>", page)  # the offer stays

    def test_banner_polls_while_updating_and_stops_at_the_end(self):
        body = {"latest": "2.0.0", "download_url": "https://yoursidedoor.com/download", "notes": "",
                "mac": {"arm64": {"url": ARM, "sha256": SHA}, "x86_64": {"url": INTEL, "sha256": SHA}}}
        update_check.check(self.home, lambda _url: json.dumps(body).encode())
        with mock.patch.object(mac_update, "can_install", return_value=True), \
                mock.patch.object(mac_update, "RUNNER", FakeMac()):
            client = self.client()
            self.assertNotIn("hx-trigger=\"every 3s\"", client.get("/applications").text)  # nothing in progress
            mac_update._set(state="working", version="2.0.0", message="Downloading and checking Sidedoor 2.0.0…")
            page = client.get("/applications").text
            self.assertIn('hx-get="/update/banner?next=/applications" hx-trigger="every 3s" hx-swap="outerHTML"', page)
            fragment = client.get("/update/banner", params={"next": "/applications"}).text
            self.assertIn("every 3s", fragment)  # still working: keeps polling
            mac_update._set(state="failed", version="2.0.0", message="Sidedoor 2.0.0 didn't install: <b>x</b>.")
            fragment = client.get("/update/banner", params={"next": "/applications"}).text
        self.assertNotIn("hx-trigger", fragment)  # the end state: polling stops
        self.assertIn("didn&#39;t install: &lt;b&gt;x&lt;/b&gt;.", fragment)  # escaped as before
        self.assertIn(">Update now</button>", fragment)
        self.assertIn('name="next" value="/applications"', fragment)
        self.assertNotIn('class="banners"', fragment)  # the one banner only
        with mock.patch.object(mac_update, "can_install", return_value=True), \
                mock.patch.object(mac_update, "RUNNER", FakeMac()):
            self.assertIn('name="next" value="/"', client.get("/update/banner", params={"next": "//evil"}).text)

    def test_install_route_does_nothing_off_the_mac_app(self):
        with mock.patch.object(mac_update, "start_install") as start:
            self.client().post("/update/install", data={"version": "2.0.0"}, headers={"origin": "http://127.0.0.1:54321"})
        start.assert_not_called()

    def test_serve_hands_over_before_binding_the_port(self):
        with mock.patch.object(mac_update, "offer_move", return_value=True) as offer, \
                mock.patch.object(dashboard, "bind") as bind:
            self.assertEqual(dashboard._serve(False, self.home, "/", False, None), 0)
        offer.assert_called_once()
        bind.assert_not_called()


if __name__ == "__main__":
    unittest.main()
