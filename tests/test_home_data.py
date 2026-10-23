import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import ast
import contextlib
import datetime as dt
import errno
import io
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings

import orchestrator
import paths
import run_locks
import scheduler_loop
import user_profile


CODE = Path(__file__).resolve().parent.parent
DATA_NAMES = (paths.STATE, paths.REPORTS, paths.LOGS, paths.BACKUPS)


def data_literal(value):
    return isinstance(value, str) and (value in DATA_NAMES or value.startswith(tuple(name + "/" for name in DATA_NAMES)))


def built_paths(text):
    """(line, literal) for each data-folder name used to build a path: `x / "state..."`,
    `Path("state...")`, or a "state/..." string constant (docstrings aside)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(id(node.body[0].value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and data_literal(side.value):
                    found.append((node.lineno, side.value))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Path":
            found += [(node.lineno, arg.value) for arg in node.args if isinstance(arg, ast.Constant) and data_literal(arg.value)]
        elif isinstance(node, ast.Constant) and id(node) not in docstrings and isinstance(node.value, str) \
                and node.value.startswith(tuple(name + "/" for name in DATA_NAMES)):
            found.append((node.lineno, node.value))
    return sorted(set(found))


class OnePlaceTest(unittest.TestCase):
    def test_no_product_module_builds_a_data_folder_path_itself(self):
        # Must-hit control: the checker sees the patterns it is meant to refuse.
        self.assertEqual(len(built_paths('a = root / "state"\nb = Path("reports/x")\nc = "logs/y.log"\n')), 3)
        offenders = {}
        for path in sorted(CODE.glob("*.py")):
            if path.name.startswith("manual_") or path.name == "paths.py":
                continue
            hits = built_paths(path.read_text())
            if hits:
                offenders[path.name] = hits
        self.assertEqual(offenders, {}, "build data paths with paths.state()/reports()/logs()/backups()/database()")

    def test_locations_are_in_the_home_or_the_given_root(self):
        home = user_profile.home()
        self.assertEqual(paths.database(), home / "state" / "orchestrator.sqlite3")
        self.assertEqual(paths.reports(), home / "reports")
        self.assertEqual(paths.logs(), home / "logs")
        self.assertEqual(paths.backups(), home / "backups")
        self.assertEqual(paths.state("/x"), Path("/x/state"))
        self.assertEqual(paths.logs(None, "/h"), Path("/h/logs"))
        self.assertEqual(scheduler_loop.Loop().root, home)
        self.assertNotEqual(paths.data(), CODE)


class Folders(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.legacy, self.home = base / "code", base / "home"
        self.legacy.mkdir()
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {paths.LEGACY_VARIABLE: str(self.legacy),
                                                   user_profile.HOME_VARIABLE: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        # No dashboard of this machine's own Sidedoor may decide these tests (HD2 has its own test).
        dashboard_check = mock.patch.object(paths, "_old_dashboard_running", return_value=False)
        dashboard_check.start()
        self.addCleanup(dashboard_check.stop)

    def old_layout(self, rows=50):
        """The code folder as an older version left it: a database whose last rows are only in its
        WAL (a process that stopped without checkpointing), reports, logs, backups, owner data."""
        state = self.legacy / "state"
        state.mkdir()
        live = Path(tempfile.mkdtemp(dir=self.legacy.parent))
        database = sqlite3.connect(live / "orchestrator.sqlite3")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("CREATE TABLE runs (slot TEXT PRIMARY KEY, started_at TEXT NOT NULL, state TEXT NOT NULL, "
                         "completed_at TEXT, result TEXT)")
        database.commit()
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for number in range(rows):
            database.execute("INSERT INTO runs VALUES (?, ?, ?, NULL, NULL)", ("slot-%d" % number, "2026-10-01", "COMPLETE"))
        database.commit()
        for name in ("orchestrator.sqlite3", "orchestrator.sqlite3-wal", "orchestrator.sqlite3-shm"):
            if (live / name).exists():
                (state / name).write_bytes((live / name).read_bytes())
        database.close()
        self.assertGreater((state / "orchestrator.sqlite3-wal").stat().st_size, 0, "the rows must be in the WAL")
        (state / "runner.lock").write_text("")
        (state / "ats-evidence").mkdir()
        (state / "ats-evidence" / "job.json").write_text("{}")
        (self.legacy / "reports" / "application-browser" / "request-1").mkdir(parents=True)
        (self.legacy / "reports" / "application-browser" / "request-1" / "trace.jsonl").write_text("{}\n")
        (self.legacy / "reports" / "latest.json").write_text('{"state": "COMPLETE"}')
        (self.legacy / "logs").mkdir()
        (self.legacy / "logs" / "launchd.stdout.log").write_text("hello\n")
        (self.legacy / "backups").mkdir()
        (self.legacy / "backups" / "manifest.json").write_text("{}")
        (self.legacy / "handoff").mkdir()
        (self.legacy / "handoff" / "REQUIREMENTS.md").write_text("# r\n")
        (self.legacy / paths.OBSERVATIONS).write_text("[]")

    def rows(self, database):
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return connection.execute("SELECT count(*) FROM runs").fetchone()[0]

    def quiet(self, function, *args, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            return function(*args, **kwargs), errors.getvalue()

    def crash_on(self, name):
        """migrate() stopping (a crash) when it reaches item `name`."""
        real = paths._move

        def move(source, target, copied, clashes, top_state=False):
            if source.name == name:
                raise OSError("the process stopped here")
            return real(source, target, copied, clashes, top_state)
        with mock.patch.object(paths, "_move", move), self.assertRaises(OSError):
            self.quiet(paths.migrate)

    def items(self):
        return json.loads((self.home / paths.MARKER).read_text())["items"]


class MigrationTest(Folders):
    def test_moves_everything_once_with_its_wal_rows(self):
        self.old_layout()
        root, _ = self.quiet(paths.prepare)
        self.assertEqual(root, self.home)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertEqual((self.home / "state" / "ats-evidence" / "job.json").read_text(), "{}")
        self.assertTrue((self.home / "reports" / "application-browser" / "request-1" / "trace.jsonl").is_file())
        self.assertEqual((self.home / "logs" / "launchd.stdout.log").read_text(), "hello\n")
        self.assertTrue((self.home / "backups" / "manifest.json").is_file())
        self.assertTrue((self.home / "handoff" / "REQUIREMENTS.md").is_file())
        self.assertEqual((self.home / paths.OBSERVATIONS).read_text(), "[]")
        self.assertFalse((self.home / "state" / "runner.lock").exists(), "lock files stay behind")
        self.assertFalse([path for path in self.legacy.rglob("*.sqlite3*") if not path.parent.is_symlink()
                          and not any(parent.is_symlink() for parent in path.parents)],
                         "no second database left in the code folder")
        marker = json.loads((self.home / paths.MARKER).read_text())
        self.assertEqual(marker["state"], paths.DONE)
        self.assertEqual({name: marker["items"][name] for name in paths.MOVED}, dict.fromkeys(paths.MOVED, paths.DONE))
        if os.name != "nt":
            # An absolute path saved by the old layout still reaches the moved file.
            self.assertTrue((self.legacy / "reports").is_symlink())
            self.assertTrue((self.legacy / "reports" / "latest.json").is_file())
            self.assertEqual(self.rows(self.legacy / "state" / "orchestrator.sqlite3"), 50)
        before = sorted(str(path) for path in self.home.rglob("*"))
        self.assertEqual(paths.migrate()["state"], "DONE_BEFORE")
        self.assertEqual(paths.prepare(), self.home)
        self.assertEqual(sorted(str(path) for path in self.home.rglob("*")), before)
        self.assertEqual(paths.status()["state"], paths.DONE)

    def test_HD1_a_database_written_in_the_code_folder_after_its_move_never_replaces_the_moved_one(self):
        # Reviewer's run C: the database moves, the run stops on reports/, an older copy of the code
        # opens code/state/orchestrator.sqlite3 read-write (a new, empty database), the next start resumes.
        self.old_layout(rows=1000)
        self.crash_on(paths.REPORTS)
        self.assertEqual(self.items()[paths.STATE], paths.DONE)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 1000)
        (self.legacy / "state").mkdir(exist_ok=True)
        sqlite3.connect(self.legacy / "state" / "orchestrator.sqlite3").close()   # the old code's empty database
        with self.assertRaises(paths.MigrationHeld) as held:
            self.quiet(paths.prepare)
        self.assertIn("two places", str(held.exception))
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 1000, "the moved database is kept")
        self.assertTrue((self.legacy / "state" / "orchestrator.sqlite3").is_file(), "and so is the other one")
        self.assertTrue((self.home / "reports" / "latest.json").is_file(), "the other items still moved")
        status = paths.status()
        self.assertEqual((status["state"], status["held"]), (paths.CONFLICT, True))
        self.assertEqual([(c["item"], c["reason"], c["keep"]) for c in status["conflicts"]],
                         [(paths.STATE, paths.REAPPEARED, str(self.home / "state"))])
        # [Move my data now]: the later copy is set aside, nothing deleted, work may start.
        after = paths.resolve()
        self.assertEqual(after["state"], paths.DONE)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 1000)
        self.assertEqual(len(list(self.legacy.glob("state" + paths.ASIDE + "*"))), 1)
        self.assertEqual(paths.prepare(), self.home)

    def test_HD10_resume_with_a_recreated_source_file_keeps_the_home_copy(self):
        self.old_layout()
        self.crash_on(paths.REPORTS)   # reports/ is MOVING when the process stops
        items = json.loads((self.home / paths.MARKER).read_text())
        items["items"][paths.REPORTS] = paths.MOVING
        (self.home / "reports").mkdir()
        (self.home / "reports" / "latest.json").write_text('{"state": "MOVED"}')   # already moved
        (self.legacy / "reports" / "latest.json").write_text('{"state": "NEWER"}')  # written again since
        (self.home / paths.MARKER).write_text(json.dumps(items))
        result, _ = self.quiet(paths.migrate)
        self.assertEqual(result["conflicts"], {paths.REPORTS: paths.FILES_IN_BOTH})
        self.assertEqual((self.home / "reports" / "latest.json").read_text(), '{"state": "MOVED"}')
        self.assertEqual((self.legacy / "reports" / "latest.json").read_text(), '{"state": "NEWER"}')
        self.assertTrue((self.home / "reports" / "application-browser" / "request-1" / "trace.jsonl").is_file())
        self.assertEqual(paths.prepare(), self.home, "a reports/ conflict does not hold the jobs")
        after = paths.resolve()
        self.assertEqual(after["state"], paths.DONE)
        self.assertEqual(len(list((self.legacy / "reports").glob("latest.json" + paths.ASIDE + "*"))), 1)

    def test_HD3_a_stray_home_log_still_lets_the_database_move(self):
        self.old_layout()
        (self.home / "logs").mkdir()
        (self.home / "logs" / "sidedoor.stdout.log").write_text("made before the move\n")
        root, warning = self.quiet(paths.prepare)
        self.assertEqual(root, self.home)
        self.assertIn("not moved: logs", warning)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertTrue((self.home / "reports" / "latest.json").is_file())
        status = paths.status()
        self.assertEqual((status["state"], status["held"]), (paths.CONFLICT, False))
        self.assertEqual(status["conflicts"][0]["keep"], str(self.legacy / "logs"))
        self.assertEqual(paths.prepare(), self.home, "jobs go on")
        resolved = paths.resolve()
        self.assertEqual(resolved["state"], paths.DONE, resolved)
        self.assertEqual((self.home / "logs" / "launchd.stdout.log").read_text(), "hello\n")
        self.assertEqual(len(list(self.home.glob("logs" + paths.ASIDE + "*"))), 1)

    def test_HD10_a_home_database_strands_state_alone_and_holds_the_jobs(self):
        self.old_layout()
        (self.home / "state").mkdir()
        with contextlib.closing(sqlite3.connect(self.home / "state" / "orchestrator.sqlite3")) as database:
            database.execute("CREATE TABLE runs (slot TEXT PRIMARY KEY)")
            database.execute("INSERT INTO runs VALUES ('home')")
            database.commit()
        with self.assertRaises(paths.MigrationHeld):
            self.quiet(paths.prepare)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 1)
        self.assertEqual(self.rows(self.legacy / "state" / "orchestrator.sqlite3"), 50)
        self.assertTrue((self.home / "reports" / "latest.json").is_file(), "only state/ is refused")
        status = paths.status()
        self.assertTrue(status["held"])
        self.assertEqual(status["conflicts"][0]["reason"], paths.HOME_HAS_DATA)
        import doctor
        line = doctor.check_home(self.home)
        self.assertEqual(line["status"], doctor.FAIL)
        self.assertIn("Move my data now", line["todo"])
        self.assertNotIn("remove the old folders", line["todo"])
        import dashboard
        banner = dashboard.data_move_banner(self.home)
        self.assertEqual((banner["form_action"], banner["tone"]), ("/data/move", "error"))
        self.assertTrue(any("Rename" in step for step in banner["steps"]))
        self.assertEqual(paths.resolve()["state"], paths.DONE)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        aside = list(self.home.glob("state" + paths.ASIDE + "*"))
        self.assertEqual(self.rows(aside[0] / "orchestrator.sqlite3"), 1, "the home's copy is kept aside")
        self.assertIsNone(dashboard.data_move_banner(self.home))

    def test_empty_folders_and_lock_files_in_the_home_are_not_data(self):
        self.old_layout()
        (self.home / "state").mkdir()
        (self.home / "state" / "browser.lock").write_text("")
        (self.home / "reports").mkdir()
        self.quiet(paths.prepare)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)

    @unittest.skipIf(os.name == "nt", "flock")
    def test_a_running_old_job_holding_its_lock_means_wait_not_a_second_database(self):
        self.old_layout()
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, time, portable\nlock = open(sys.argv[1], 'a+')\n"
             "assert portable.try_lock(lock)\nprint('held', flush=True)\ntime.sleep(60)",
             str(self.legacy / "state" / "runner.lock")],
            cwd=CODE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(paths.MigrationPending):
            paths.prepare()
        self.assertFalse((self.home / "state" / "orchestrator.sqlite3").exists())
        self.assertFalse((self.home / paths.MARKER).exists())
        self.assertEqual(self.rows(self.legacy / "state" / "orchestrator.sqlite3"), 50)
        self.assertEqual(paths.status()["state"], "WAITING")
        self.assertIn("Waiting to move your data", paths.message(paths.status()))
        holder.kill()
        holder.wait()
        self.quiet(paths.prepare)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertIsNone(paths.status()["wait"])

    def test_HD2_an_open_dashboard_of_the_old_version_stops_the_move(self):
        self.old_layout()
        with mock.patch.object(paths, "_old_dashboard_running", return_value=True):
            with self.assertRaises(paths.MigrationPending) as pending:
                paths.prepare()
            self.assertIn("Quit Sidedoor", str(pending.exception))
            self.assertFalse((self.home / "state").exists())
            # The dashboard itself holds the port: no other dashboard can be open.
            self.quiet(paths.prepare, serving=True)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)

    def test_HD6_a_busy_old_database_is_a_wait_not_a_crash(self):
        self.old_layout()
        reader = sqlite3.connect(self.legacy / "state" / "orchestrator.sqlite3")
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM runs").fetchone()   # an open read pins the WAL
        with mock.patch.object(paths, "CHECKPOINT_TIMEOUT_SECONDS", 0.1), self.assertRaises(paths.MigrationPending):
            paths.prepare()
        self.assertFalse((self.home / "state" / "orchestrator.sqlite3").exists())
        self.assertEqual(paths.status()["wait"]["reason"], "DATABASE_BUSY")
        reader.rollback()
        reader.close()
        self.quiet(paths.prepare)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)

    def test_across_volumes_each_file_is_copied_verified_then_deleted(self):
        self.old_layout()
        real_rename = os.rename

        def other_volume(source, target, *args, **kwargs):
            if str(source).startswith(str(self.legacy)):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(source, target, *args, **kwargs)
        with mock.patch.object(os, "rename", other_volume):
            result, _ = self.quiet(paths.migrate)
        self.assertEqual(result["state"], "MOVED")
        self.assertGreaterEqual(len(result["copied"]), 8)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertFalse(list(self.home.rglob("*" + paths.COPY_SUFFIX)))
        self.assertTrue((self.legacy / "logs").is_symlink() or not (self.legacy / "logs").exists(),
                        "the copied source is deleted")

    def test_a_move_that_stopped_part_way_resumes(self):
        self.old_layout()
        self.crash_on(paths.REPORTS)
        self.assertEqual(self.items()[paths.REPORTS], paths.MOVING)
        self.assertEqual(paths.status()["state"], paths.MOVING)
        result, _ = self.quiet(paths.migrate)
        self.assertEqual(result["state"], "MOVED")
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertTrue((self.home / "reports" / "latest.json").is_file())

    def test_nothing_to_move(self):
        self.assertEqual(paths.migrate()["state"], "NONE")
        with mock.patch.dict(os.environ, {paths.LEGACY_VARIABLE: ""}):
            self.assertEqual(paths.migrate()["state"], "NONE")
        self.assertEqual(paths.prepare(), self.home)
        self.assertTrue((self.home / "state").is_dir() and (self.home / "logs").is_dir())

    def test_the_orchestrator_entry_point_moves_the_data_before_it_opens_the_database(self):
        self.old_layout()
        (self.legacy / "backups" / "manifest.json").unlink()  # a fresh install's backups: none registered
        shutil.rmtree(self.legacy / "handoff")  # no reconciliation handoff on file
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(orchestrator.owner_seed, "items", return_value=[]):  # no seeded contacts
            self.assertEqual(orchestrator.main(["init"]), 0)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)
        self.assertEqual(json.loads((self.home / paths.MARKER).read_text())["state"], paths.DONE)

    def test_HD8_a_job_that_must_wait_exits_with_a_hold_the_scheduler_shows_as_a_wait(self):
        self.old_layout()
        errors = io.StringIO()
        with mock.patch.object(paths, "_old_dashboard_running", return_value=True), contextlib.redirect_stderr(errors):
            self.assertEqual(orchestrator.main(["run"]), paths.HOLD_EXIT)
        self.assertIn("Waiting to move your data", errors.getvalue())
        loop = scheduler_loop.Loop(self.home, clock=lambda: dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc))
        loop.children["run"] = type("Child", (), {"poll": lambda self: paths.HOLD_EXIT})()
        loop._reap(dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc))
        self.assertEqual(loop.job_failures, {}, "a wait is not a failure")
        self.assertIn("Waiting to move your data", loop.data_hold)
        import failures_page
        scheduler = {"running": True, "data_hold": loop.data_hold, "job_failures": {}}
        self.assertEqual(failures_page.scheduler_failures(scheduler, {}, None, str), [], "HM5: not a Failures entry")
        import overview_page
        item = overview_page.scheduler_item(scheduler, None, {}, str)
        self.assertIn("Waiting to move your data.", json.dumps(item))

    def test_HD7_paths_saved_before_the_move_still_resolve(self):
        self.old_layout()
        old = self.legacy / "reports" / "application-browser" / "request-1" / "trace.jsonl"
        self.quiet(paths.prepare)
        for link in self.legacy.iterdir():
            if link.is_symlink():
                link.unlink()   # as on Windows: no link left in the code folder
        self.assertEqual(paths.relocated(old), self.home / "reports" / "application-browser" / "request-1" / "trace.jsonl")
        import application_recovery
        self.assertEqual(application_recovery.trace_reference(str(old), self.home),
                         "reports/application-browser/request-1/trace.jsonl")
        import provider_limits
        (self.home / "reports" / "application-browser" / "request-1" / "stderr.log").write_text("x")
        with mock.patch.object(provider_limits, "message_in", side_effect=lambda text: bool(text)):
            self.assertTrue(provider_limits.artifact_limited(str(old.with_name("stderr.log"))))


class LocksAgreeTest(Folders):
    CHILD = ("import sys, time, paths, run_locks\n"
             "print(paths.database(), flush=True)\n"
             "with run_locks.held(paths.data(), run_locks.RUNNER, run_locks.APPLICATIONS, run_locks.BROWSER) as held:\n"
             "    print('held' if held else 'busy', flush=True)\n"
             "    time.sleep(60 if held else 0)\n")

    @unittest.skipIf(os.name == "nt", "flock")
    def test_every_process_resolves_the_same_lock_files(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        environment = dict(os.environ, PYTHONPATH=str(CODE))
        child = subprocess.Popen([sys.executable, "-c", self.CHILD], cwd=elsewhere.name, env=environment,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), str(paths.database()))
        self.assertEqual(child.stdout.readline().strip(), "held")
        # The orchestrator's job lock, the scheduler's data folder and the applications/browser locks
        # all name the child's files.
        with orchestrator.single_instance(paths.data()) as acquired:
            self.assertFalse(acquired)
        with run_locks.held(scheduler_loop.Loop().root, run_locks.APPLICATIONS) as acquired:
            self.assertFalse(acquired)
        with run_locks.browser_session(paths.data()) as acquired:
            self.assertFalse(acquired)
        second = subprocess.run([sys.executable, "-c", self.CHILD], cwd=CODE, env=environment,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(second.stdout.split(), [str(paths.database()), "busy"])
        child.kill()
        child.wait()
        with orchestrator.single_instance(paths.data()) as acquired:
            self.assertTrue(acquired)


try:
    import dashboard
except ImportError:   # the dashboard packages (fastapi, uvicorn) are not installed
    dashboard = None


@unittest.skipUnless(dashboard, "dashboard packages are not installed")
class WaitingDashboardTest(Folders):
    def test_HD5_the_port_shows_the_waiting_page_and_answers_a_second_launch(self):
        self.old_layout()
        listener = dashboard.bind(port=0)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        page = dashboard.WaitingPage(listener, "secret", self.home).start()
        try:
            self.assertTrue(dashboard.running_here("secret", port=port), "a second launch finds this install")
            self.assertFalse(dashboard.running_here("other", port=port))
            import urllib.request
            body = urllib.request.urlopen("http://127.0.0.1:%d/?t=x" % port, timeout=5).read().decode()
            self.assertIn("Moving your data", body)
            self.assertIn('http-equiv="refresh"', body)
            self.assertTrue(dashboard.start_there("secret", port=port))
            self.assertTrue(page.start_requested)
        finally:
            page.stop()
        self.assertEqual(listener.fileno() >= 0, True, "the listener stays open for the dashboard")

    def test_HD5_HD6_serve_waits_and_retries_instead_of_crashing(self):
        self.old_layout()
        attempts = []

        def prepare(home, serving=False, **keywords):
            attempts.append(serving)
            if len(attempts) < 3:
                raise paths.MigrationPending("The old database is in use")
            return self.home
        announced = []
        with mock.patch.object(paths, "prepare", prepare), contextlib.redirect_stdout(io.StringIO()):
            root, start = dashboard.prepare_data(self.home, None, "secret", lambda: announced.append(1),
                                                 sleep=lambda seconds: None)
        self.assertEqual((root, start, attempts, announced), (self.home, False, [True, True, True], [1]))

    def test_HD9_serve_exports_the_home_it_was_given(self):
        other = self.home.parent / "other-home"
        with mock.patch.object(dashboard, "_serve", return_value=0), \
                mock.patch.dict(os.environ, {user_profile.HOME_VARIABLE: str(self.home)}):
            self.assertEqual(dashboard.serve(open_browser=False, home=other), 0)
            self.assertEqual(os.environ[user_profile.HOME_VARIABLE], str(other))
            self.assertEqual(paths.database(), other / "state" / "orchestrator.sqlite3")


class ReappearedDatabaseTest(Folders):
    """HM1/HM2: a real code-folder database after the move finished; the two copies are compared."""

    def jobs(self, path, rows, latest):
        with contextlib.closing(sqlite3.connect(path)) as database:
            database.execute("CREATE TABLE IF NOT EXISTS application_jobs (job_key TEXT PRIMARY KEY, updated_at TEXT)")
            database.execute("DELETE FROM application_jobs")
            database.executemany("INSERT INTO application_jobs VALUES (?, ?)",
                                 [("job-%d" % number, (latest - dt.timedelta(minutes=number)).isoformat())
                                  for number in range(rows)])
            database.commit()

    def count(self, path):
        with contextlib.closing(sqlite3.connect(path)) as database:
            return database.execute("SELECT count(*) FROM application_jobs").fetchone()[0]

    def split_then_old_code_runs(self, home_latest, code_latest):
        self.old_layout()
        self.quiet(paths.prepare)
        split = paths._moment(self.items()[paths.STATE + ":at"])
        self.jobs(self.home / "state" / "orchestrator.sqlite3", 1000, split + home_latest)
        if (self.legacy / "state").is_symlink():
            (self.legacy / "state").unlink()   # an older copy of the code, with its own state/
        (self.legacy / "state").mkdir(exist_ok=True)
        self.jobs(self.legacy / "state" / "orchestrator.sqlite3", 1500, split + code_latest)
        return split

    def test_HM2_a_code_folder_database_after_done_holds_the_jobs(self):
        self.old_layout()
        self.quiet(paths.prepare)
        self.assertEqual(json.loads((self.home / paths.MARKER).read_text())["state"], paths.DONE)
        if os.name != "nt":
            with mock.patch.object(os, "lstat", wraps=os.lstat) as lstat:
                self.assertEqual(paths.migrate()["state"], "DONE_BEFORE")
            self.assertEqual(lstat.call_count, 1, "one lstat while the link is in place")
            (self.legacy / "state").unlink()
        (self.legacy / "state").mkdir(exist_ok=True)
        sqlite3.connect(self.legacy / "state" / "orchestrator.sqlite3").close()
        self.assertEqual(paths.status()["state"], paths.CONFLICT)
        with self.assertRaises(paths.MigrationHeld):
            self.quiet(paths.prepare)
        self.assertEqual(self.rows(self.home / "state" / "orchestrator.sqlite3"), 50)

    def asks(self):
        status = paths.status()
        conflict = status["conflicts"][0]
        self.assertTrue(conflict["choice_required"])
        self.assertIsNone(conflict["keep_side"])
        self.assertIn("will not pick", paths.message(status))
        self.assertIn("Choose", paths.resolve()["error"], "Move my data now alone never picks")
        return conflict

    def test_HC1_one_job_against_501_asks(self):
        self.old_layout()
        (self.home / "state").mkdir()
        self.jobs(self.home / "state" / "orchestrator.sqlite3", 1, dt.datetime.now(dt.timezone.utc))
        self.jobs(self.legacy / "state" / "orchestrator.sqlite3", 501, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        with self.assertRaises(paths.MigrationHeld):
            self.quiet(paths.prepare)
        conflict = self.asks()
        self.assertEqual(conflict["reason"], paths.HOME_HAS_DATA)
        self.assertIn("501 application jobs", conflict["code_detail"])
        self.assertIn("1 application jobs", conflict["home_detail"])
        self.assertEqual(paths.resolve(keep=paths.CODE_SIDE)["state"], paths.DONE)
        self.assertEqual(self.count(self.home / "state" / "orchestrator.sqlite3"), 501)
        self.assertEqual(self.count(next(self.home.glob("state" + paths.ASIDE + "*")) / "orchestrator.sqlite3"), 1)

    def test_HC2_500_idle_against_one_new_asks(self):
        self.split_then_old_code_runs(home_latest=-dt.timedelta(days=1), code_latest=dt.timedelta(hours=1))
        self.jobs(self.home / "state" / "orchestrator.sqlite3", 500, paths._moment(self.items()[paths.STATE + ":at"])
                  - dt.timedelta(days=1))
        self.jobs(self.legacy / "state" / "orchestrator.sqlite3", 1, dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
        self.assertEqual(self.asks()["reason"], paths.REAPPEARED)
        self.assertEqual(self.count(self.home / "state" / "orchestrator.sqlite3"), 500, "nothing changed")

    def test_HC1_an_empty_copy_never_wins_over_a_full_one(self):
        split = self.split_then_old_code_runs(home_latest=-dt.timedelta(days=1), code_latest=dt.timedelta(hours=1))
        self.jobs(self.legacy / "state" / "orchestrator.sqlite3", 0, split + dt.timedelta(hours=1))   # newer, empty
        conflict = paths.status()["conflicts"][0]
        self.assertEqual((conflict["keep_side"], conflict["choice_required"]), (paths.HOME, False))
        self.assertEqual(paths.resolve()["state"], paths.DONE)
        self.assertEqual(self.count(self.home / "state" / "orchestrator.sqlite3"), 1000)
        self.assertEqual(len(list(self.legacy.glob("state" + paths.ASIDE + "*"))), 1, "set aside, not deleted")

    def test_HM1_both_used_after_the_split_is_never_picked_automatically(self):
        self.split_then_old_code_runs(home_latest=dt.timedelta(hours=1), code_latest=dt.timedelta(hours=2))
        status = paths.status()
        conflict = status["conflicts"][0]
        self.assertTrue(conflict["choice_required"])
        self.assertIsNone(conflict["keep_side"])
        self.assertIn("will not pick", paths.message(status))
        refused = paths.resolve()
        self.assertIn("Choose", refused["error"])
        self.assertEqual(self.count(self.home / "state" / "orchestrator.sqlite3"), 1000, "nothing changed")
        self.assertEqual(self.count(self.legacy / "state" / "orchestrator.sqlite3"), 1500)
        if dashboard:
            banner = dashboard.data_move_banner(self.home)
            self.assertEqual([form["fields"]["keep"] for form in banner["forms"]], [paths.HOME, paths.CODE_SIDE])
            self.assertNotIn("form_action", banner)
        self.assertEqual(paths.resolve(keep=paths.CODE_SIDE)["state"], paths.DONE)
        self.assertEqual(self.count(self.home / "state" / "orchestrator.sqlite3"), 1500)
        self.assertEqual(len(list(self.home.glob("state" + paths.ASIDE + "*"))), 1)


class OpenDashboardTest(Folders):
    def test_HM3_a_conflict_settled_by_hand_clears_at_the_next_tick(self):
        self.old_layout()
        (self.home / "logs").mkdir()
        (self.home / "logs" / "sidedoor.stdout.log").write_text("made before the move\n")
        self.quiet(paths.prepare, serving=True)
        os.rename(self.home / "logs", self.home / "logs-old")   # settled by hand
        self.assertEqual(paths.status()["state"], "PENDING")
        started = []
        loop = scheduler_loop.Loop(self.home, spawner=started.append, is_paused=lambda: False, config={},
                                   before_start=lambda: self.quiet(paths.migrate, self.home, serving=True))
        loop.tick()
        self.assertTrue(started)
        self.assertEqual(paths.status()["state"], paths.DONE)
        self.assertEqual((self.home / "logs" / "launchd.stdout.log").read_text(), "hello\n")

    def test_HM4_a_job_of_the_open_sidedoor_is_told_the_truth(self):
        self.old_layout()
        with mock.patch.object(paths, "_old_dashboard_running", return_value=True), \
                mock.patch.dict(os.environ, {paths.DASHBOARD_PID_VARIABLE: str(os.getppid())}):
            with self.assertRaises(paths.MigrationPending) as pending:
                paths.prepare()
        self.assertIn("The open Sidedoor moves your data", str(pending.exception))
        self.assertNotIn("previous version", str(pending.exception))


if __name__ == "__main__":
    unittest.main()
