"""portable.py, E2.T3 option A (owner, 2026-10-22): locks and process control on both systems.

The worked examples of design/E2.T3-portable-locks-options.md, each run on the POSIX branch for
real and on the Windows branch with msvcrt and kernel32 faked (tests/fake_windows.py). The real
Windows proof (two processes on one lock, Task Manager kills, Job Objects, Chrome's lockfile) is
the VM pass.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import fake_windows
import log_retention
import orchestrator
import portable
import run_locks
import scheduler_loop
import setup_steps

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
POSIX = os.name == "posix"

HOLDER = """
import sys, time
sys.path[:0] = [{root!r}, {tests!r}]
from pathlib import Path
import fake_windows, orchestrator
if {windows!r}:
    fake_windows.install()
with orchestrator.single_instance(Path({home!r})) as acquired:
    print("held" if acquired else "busy", flush=True)
    time.sleep(120)
"""


class Home(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()

    def branches(self):
        """Both branches where they can run here: the real one, and the faked Windows one on
        POSIX (the fake is built on flock)."""
        yield "native", contextlib.nullcontext
        if POSIX:
            yield "windows", self.windows

    @contextlib.contextmanager
    def windows(self):
        with fake_windows.windows():
            yield
            self.assertTrue(fake_windows.FakeMsvcrt.calls, "the Windows branch was taken")


class RunnerLockTests(Home):
    """Example 1: the hourly job holding runner.lock is killed; the next run takes it, and two
    live holders never both hold it."""

    def holder(self, windows):
        child = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), tests=str(TESTS),
                                                                       windows=windows, home=str(self.root))],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual("held", child.stdout.readline().strip())
        return child

    def check(self, windows):
        child = self.holder(windows)
        with (fake_windows.windows() if windows else contextlib.nullcontext()):
            with orchestrator.single_instance(self.root) as acquired:
                self.assertFalse(acquired, "a second live run must skip")
            with run_locks.held(self.root, run_locks.RUNNER) as acquired:
                self.assertFalse(acquired)
            child.kill()                           # macOS kill -9; Windows Task Manager "End task"
            child.wait()
            with orchestrator.single_instance(self.root) as acquired:
                self.assertTrue(acquired, "the dead holder's lock is released by the OS")
                with run_locks.held(self.root, run_locks.RUNNER) as again:
                    self.assertFalse(again, "and while this run holds it, nobody else does")
        if windows:
            self.assertTrue(fake_windows.FakeMsvcrt.calls, "the Windows branch was taken")
            self.assertTrue(all(nbytes == 1 and position == 0 for _, nbytes, position in fake_windows.FakeMsvcrt.calls))

    @unittest.skipUnless(POSIX, "POSIX branch")
    def test_posix(self):
        self.check(False)

    @unittest.skipUnless(POSIX, "the Windows fake is built on flock")
    def test_windows_branch(self):
        self.check(True)

    def test_held_is_all_or_none_on_both_branches(self):
        for name, branch in self.branches():
            with self.subTest(name), branch():
                with run_locks.held(self.root, run_locks.APPLICATIONS) as first:
                    self.assertTrue(first)
                    with run_locks.held(self.root, run_locks.RUNNER, run_locks.APPLICATIONS) as both:
                        self.assertFalse(both)
                    with run_locks.held(self.root, run_locks.RUNNER) as runner:
                        self.assertTrue(runner, "RUNNER was released when APPLICATIONS could not be taken")


class BrowserLockTests(Home):
    """Example 2: two opens of browser.lock in one process exclude each other (the dashboard's
    hold on one thread, a job on another)."""

    def test_two_threads_exclude_each_other_on_both_branches(self):
        for name, branch in self.branches():
            with self.subTest(name), branch():
                taken, release, seen = threading.Event(), threading.Event(), []

                def dashboard_hold():
                    with run_locks.browser_session(self.root) as acquired:
                        seen.append(acquired)
                        taken.set()
                        release.wait(10)

                thread = threading.Thread(target=dashboard_hold)
                thread.start()
                self.assertTrue(taken.wait(10))
                with run_locks.browser_session(self.root) as acquired:
                    self.assertFalse(acquired, "a second handle in the same process is refused")
                with run_locks.browser_session(self.root) as acquired:
                    self.assertFalse(acquired)
                release.set()
                thread.join(10)
                self.assertEqual([True], seen)
                with run_locks.browser_session(self.root) as acquired:
                    self.assertTrue(acquired)
                    with run_locks.browser_session(self.root) as inner:
                        self.assertTrue(inner, "re-entrant within one thread, as before")

    def test_two_raw_opens_in_one_thread_exclude_each_other(self):
        for name, branch in self.branches():
            with self.subTest(name), branch():
                first = portable.open_lock(self.root / "state" / run_locks.BROWSER)
                second = portable.open_lock(self.root / "state" / run_locks.BROWSER)
                try:
                    self.assertTrue(portable.try_lock(first))
                    self.assertFalse(portable.try_lock(second))
                    portable.unlock(first)
                    self.assertTrue(portable.try_lock(second))
                    portable.unlock(second)
                finally:
                    os.close(first)
                    os.close(second)

    @unittest.skipUnless(POSIX, "symlinks")
    def test_a_symlinked_lock_is_refused_on_both_branches(self):
        target = self.root / "elsewhere"
        target.write_text("")
        os.symlink(target, self.root / "state" / run_locks.BROWSER)
        with self.assertRaises(OSError):                       # O_NOFOLLOW: ELOOP
            with run_locks.held(self.root, run_locks.BROWSER):
                pass
        with fake_windows.windows(), self.assertRaisesRegex(OSError, "Lock file is a link"):   # lstat check
            with run_locks.held(self.root, run_locks.BROWSER):
                pass


class RotationTests(Home):
    """Example 3: on Windows a held log cannot be renamed; rotation skips it and the next spawn
    rotates it. No data is lost."""

    def test_held_job_log_is_skipped_then_rotated_before_the_next_spawn(self):
        stdout = self.root / "logs" / "applications.stdout.log"
        stdout.write_bytes(b"x" * log_retention.ROTATE_BYTES)
        held = {str(stdout)}
        replace = os.replace

        def sharing(source, destination):
            if str(source) in held:
                raise PermissionError(32, "The process cannot access the file because it is being used")
            return replace(source, destination)

        with fake_windows.windows(), mock.patch.object(os, "replace", sharing), \
                mock.patch.object(scheduler_loop.subprocess, "Popen") as popen:
            scheduler_loop.spawn("applications", self.root, windows=True)
            self.assertEqual(b"x" * log_retention.ROTATE_BYTES, stdout.read_bytes())
            self.assertEqual([], list((self.root / "logs").glob("*.retained.*")))
            held.clear()                               # the old writer exited
            scheduler_loop.spawn("applications", self.root, windows=True)
        self.assertEqual(2, popen.call_count)
        archives = list((self.root / "logs").glob("applications.stdout.log.retained.*"))
        self.assertEqual(1, len(archives))
        self.assertTrue(log_retention.ARCHIVE_NAME.fullmatch(archives[0].name))
        self.assertEqual(b"x" * log_retention.ROTATE_BYTES, archives[0].read_bytes())
        self.assertEqual(0, stdout.stat().st_size)

    def test_posix_spawn_leaves_rotation_to_the_child(self):
        stdout = self.root / "logs" / "applications.stdout.log"
        stdout.write_bytes(b"x" * log_retention.ROTATE_BYTES)
        with mock.patch.object(scheduler_loop.subprocess, "Popen"):
            scheduler_loop.spawn("applications", self.root, windows=False)
        self.assertEqual([], list((self.root / "logs").glob("*.retained.*")))

    def test_retain_skips_held_files_without_lsof_on_windows(self):
        worker = self.root / "logs" / "12345678-1234-1234-1234-123456789abc.log"
        worker.write_bytes(b"w")
        os.utime(worker, (0, 0))
        stream = self.root / "logs" / "research.stdout.log"
        stream.write_bytes(b"s" * 10)
        held = {str(worker), str(stream)}
        unlink, replace = os.unlink, os.replace

        def remove(path, *args, **kwargs):
            if str(path) in held:
                raise PermissionError(32, "in use")
            return unlink(path, *args, **kwargs)

        def rename(source, destination):
            if str(source) in held:
                raise PermissionError(32, "in use")
            return replace(source, destination)

        with fake_windows.windows(), mock.patch.object(os, "unlink", remove), mock.patch.object(os, "replace", rename), \
                mock.patch.object(log_retention.subprocess, "run", side_effect=AssertionError("no lsof on Windows")):
            outcome = log_retention.retain(self.root, rotate_bytes=1, retain_seconds=1)
            self.assertEqual({"state": "COMPLETE", "rotated": 0, "removed": 0, "skipped_open": 2}, outcome)
            self.assertEqual(b"w", worker.read_bytes())
            held.clear()
            outcome = log_retention.retain(self.root, rotate_bytes=1, retain_seconds=1)
        self.assertEqual((1, 1, 0), (outcome["rotated"], outcome["removed"], outcome["skipped_open"]))
        self.assertFalse(worker.exists())

    def test_any_rotation_oserror_never_blocks_the_spawn(self):
        """P2: a FileNotFoundError (renamed by another process in between) or any other OSError
        from the rename skips that file; the other file still rotates and the child still starts."""
        for error in (FileNotFoundError(2, "gone"), OSError(5, "I/O error")):
            with self.subTest(error=type(error).__name__):
                stdout = self.root / "logs" / "applications.stdout.log"
                stderr = self.root / "logs" / "applications.stderr.log"
                for path in (stdout, stderr):
                    path.write_bytes(b"x" * log_retention.ROTATE_BYTES)
                for archive in (self.root / "logs").glob("*.retained.*"):
                    archive.unlink()
                replace = os.replace

                def failing(source, destination):
                    if str(source) == str(stdout):
                        raise error
                    return replace(source, destination)

                with fake_windows.windows(), mock.patch.object(os, "replace", failing), \
                        mock.patch.object(scheduler_loop.subprocess, "Popen") as popen:
                    scheduler_loop.spawn("applications", self.root, windows=True)
                self.assertEqual(1, popen.call_count)
                self.assertEqual([], list((self.root / "logs").glob("applications.stdout.log.retained.*")))
                self.assertEqual(1, len(list((self.root / "logs").glob("applications.stderr.log.retained.*"))))

    def test_posix_errors_still_raise(self):
        with mock.patch.object(portable, "WINDOWS", False), \
                mock.patch.object(os, "replace", side_effect=PermissionError(13, "denied")), self.assertRaises(PermissionError):
            portable.try_rotate(self.root / "a", self.root / "b")


class LockHolderAliveTests(Home):
    """Example 4: on Windows lock_holder_alive probes with OpenProcess/GetExitCodeProcess and never
    reaches os.kill(pid, 0), which on Windows is TerminateProcess."""

    def profile(self, pid):
        profile = self.root / "profile"
        profile.mkdir()
        os.symlink("host-%d" % pid, profile / "SingletonLock")
        return profile

    @unittest.skipUnless(POSIX, "symlinks")
    def test_windows_branch_probes_without_terminating(self):
        profile = self.profile(4242)
        for exit_code, alive in ((portable.STILL_ACTIVE, True), (0, False)):
            kernel32 = fake_windows.FakeKernel32(exit_code=exit_code)
            with self.subTest(exit_code=exit_code), fake_windows.windows(kernel32), \
                    mock.patch.object(os, "kill", side_effect=AssertionError("os.kill terminates on Windows")):
                self.assertIs(alive, setup_steps.lock_holder_alive(str(profile)))
            self.assertEqual(["OpenProcess", "GetExitCodeProcess", "CloseHandle"], kernel32.names())
            self.assertEqual((portable.PROCESS_QUERY_LIMITED_INFORMATION, 4242), kernel32.calls[0][1:])

    def test_windows_open_failure_maps_access_denied_to_alive(self):
        for error, alive in ((portable.ERROR_ACCESS_DENIED, True), (87, False)):    # 87: no such process
            kernel32 = fake_windows.FakeKernel32(open_process=0)
            with self.subTest(error=error), fake_windows.windows(kernel32, last_error=error), \
                    mock.patch.object(os, "kill", side_effect=AssertionError("os.kill terminates on Windows")):
                self.assertIs(alive, portable.pid_alive(4242))
            self.assertEqual(["OpenProcess"], kernel32.names())

    def test_windows_without_singleton_lock_reads_chrome_lockfile(self):
        profile = self.root / "profile"
        profile.mkdir()
        (profile / "lockfile").write_bytes(b"")
        real_open = os.open

        def chrome_holds(path, *args, **kwargs):
            if str(path) == str(profile / "lockfile"):
                raise PermissionError(32, "in use")
            return real_open(path, *args, **kwargs)

        with fake_windows.windows(fake_windows.FakeKernel32()):
            self.assertFalse(setup_steps.lock_holder_alive(str(profile)))
            with mock.patch.object(os, "open", chrome_holds):
                self.assertTrue(setup_steps.lock_holder_alive(str(profile)))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_posix_branch_is_unchanged(self):
        self.assertTrue(setup_steps.lock_holder_alive(str(self.profile(os.getpid()))))


class ImportTests(unittest.TestCase):
    """Example 5: with fcntl and resource unavailable (as on Windows) the import chain works."""

    def test_entry_modules_import_without_fcntl_resource_or_killpg(self):
        program = ("import sys, os\nsys.path[:0] = [%r, %r]\nsys.modules['fcntl'] = None\n"
                   "sys.modules['resource'] = None\nfor name in ('killpg', 'getuid'):\n"
                   "    if hasattr(os, name):\n        delattr(os, name)\n"
                   "import orchestrator, run_locks, reply_worker, agent_worker, notifications, log_retention\n"
                   "import application_browser, outreach_history_browser, setup_steps, scheduler_loop, login_item\n"
                   "import browser_runtime, application_connection, linkedin_contacts, external_history, portable\n"
                   "assert portable.fcntl is None and portable.resource is None and reply_worker.resource is None\n"
                   "print('ok')\n") % (str(ROOT), str(TESTS))
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=120)
        self.assertEqual("ok", result.stdout.strip(), result.stderr[-2000:])


class WorkerTests(unittest.TestCase):
    """P1/P2: the Windows branch's Job Object plumbing (faked), and the POSIX tree kill (real)."""

    def worker(self):
        return mock.Mock(pid=4242, _handle=0x99)

    def test_windows_spawn_options_and_crash_dialogs(self):
        kernel32 = fake_windows.FakeKernel32()
        with fake_windows.windows(kernel32):
            options = portable.worker_spawn_options()
            self.assertEqual({"creationflags": portable.CREATE_NEW_PROCESS_GROUP | portable.CREATE_NO_WINDOW
                              | portable.CREATE_SUSPENDED}, options)
            portable.worker_spawn_options(core=False)
        self.assertEqual([("SetErrorMode", portable.SEM_FAILCRITICALERRORS | portable.SEM_NOGPFAULTERRORBOX)],
                         kernel32.calls, "once per process")

    def test_posix_spawn_options_are_todays(self):
        with mock.patch.object(portable, "WINDOWS", False):
            self.assertEqual({"start_new_session": True, "preexec_fn": portable.no_core}, portable.worker_spawn_options())
            self.assertEqual({"start_new_session": True}, portable.worker_spawn_options(core=False))

    def test_windows_contain_assigns_then_resumes_and_kill_tree_ends_the_job(self):
        kernel32 = fake_windows.FakeKernel32(threads=[(7, 1), (8, 4242), (9, 3)])
        worker = self.worker()
        with fake_windows.windows(kernel32):
            portable.contain(worker)
            names = kernel32.names()
            self.assertLess(names.index("AssignProcessToJobObject"), names.index("ResumeThread"))
            self.assertEqual(portable.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | portable.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
                             kernel32.limits)
            job = kernel32.calls[names.index("AssignProcessToJobObject")][1]
            self.assertEqual(("AssignProcessToJobObject", job, 0x99), kernel32.calls[names.index("AssignProcessToJobObject")])
            self.assertEqual([("ResumeThread", 8 + 0x1000)], [call for call in kernel32.calls if call[0] == "ResumeThread"])
            kernel32.calls.clear()
            portable.kill_tree(4242)
            self.assertEqual([("TerminateJobObject", job, 1), ("CloseHandle", job)], kernel32.calls)
            kernel32.calls.clear()
            portable.kill_tree(4242)              # already reaped: nothing, never raises
            self.assertEqual([], kernel32.calls)
        worker.kill.assert_not_called()

    def test_windows_contain_failure_terminates_the_worker_and_raises(self):
        for failing in ("AssignProcessToJobObject", "ResumeThread"):
            kernel32 = fake_windows.FakeKernel32(threads=[(8, 4242)], fail={failing})
            worker = self.worker()
            with self.subTest(failing), fake_windows.windows(kernel32), self.assertRaises(OSError):
                portable.contain(worker)
            worker.kill.assert_called_once_with()
            self.assertIn("TerminateJobObject", kernel32.names())
            self.assertNotIn(4242, portable._jobs)

    def test_kill_tree_ends_worker_and_grandchild(self):
        """A real tree on either system: POSIX through the process group, Windows through the Job
        Object `contain` made (WR3, run on the Windows VM)."""
        program = ("import subprocess, sys, time\n"
                   "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                   "print(child.pid, flush=True)\ntime.sleep(60)\n")
        worker = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True,
                                  **portable.worker_spawn_options())
        portable.contain(worker)
        try:
            grandchild = int(worker.stdout.readline())
            portable.kill_tree(worker.pid)
            worker.wait(timeout=10)
            deadline = time.monotonic() + 10
            while portable.pid_alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(portable.pid_alive(grandchild))
            portable.kill_tree(worker.pid)        # already gone: never raises
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
            worker.stdout.close()


class ProfileTests(unittest.TestCase):
    def test_wait_profile_free_on_windows(self):
        with tempfile.TemporaryDirectory() as directory, fake_windows.windows():
            lockfile = os.path.join(directory, "lockfile")
            Path(lockfile).write_bytes(b"")
            real_open, held = os.open, [True]

            def chrome(path, *args, **kwargs):
                if str(path) == lockfile and held[0]:
                    raise PermissionError(32, "in use")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(os, "open", chrome):
                self.assertFalse(portable.wait_profile_free(directory, 0.2, 0.01), "still held")
                threading.Timer(0.05, lambda: held.__setitem__(0, False)).start()
                self.assertTrue(portable.wait_profile_free(directory, 5, 0.01))
                self.assertFalse(portable.wait_profile_free(directory, 5, 0.01), "free: nothing to wait for")

    def test_posix_profile_busy_is_left_to_singleton_lock(self):
        with mock.patch.object(portable, "WINDOWS", False):
            self.assertFalse(portable.profile_busy("/nonexistent"))


class KillTreeR1Tests(unittest.TestCase):
    @unittest.skipIf(portable.WINDOWS, "POSIX process groups")
    def test_r1_an_exited_unreaped_leader_never_raises(self):
        """macOS answers EPERM for a group whose only member is its exited, unreaped leader."""
        worker = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        time.sleep(0.1)
        portable.kill_tree(worker.pid)
        worker.wait()
        with mock.patch.object(os, "killpg", side_effect=PermissionError(1, "Operation not permitted")):
            portable.kill_tree(worker.pid)


class NpmShimTests(unittest.TestCase):
    """E10: an npm .cmd shim runs its program directly, so no argument goes through cmd.exe. The npm shims in
    tests/fixtures/cmd_shims were written by npm's own cmd-shim 8.0.0 (npm 11): node_script.cmd for a
    `#!/usr/bin/env node` package script (Claude, agy, Codex), own_exe.cmd for a package whose bin is an
    .exe, with_variables.cmd for a shebang that sets variables (left to cmd.exe)."""

    SCHEMA = '{"type":"object","properties":{"a":{"description":"100% & <x> ^ \\"q\\" !v!"}}}'

    def setUp(self):
        self.npm = Path(tempfile.mkdtemp()) / "npm"
        (self.npm / "node_modules" / "@anthropic-ai" / "claude-code").mkdir(parents=True)
        self.script = self.npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        self.script.write_text("#!/usr/bin/env node\n")
        self.patches = [mock.patch.object(portable, "WINDOWS", True),
                        mock.patch.object(setup_steps, "install_folders", return_value=[])]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def shim(self, fixture, name="claude.cmd"):
        path = self.npm / name
        path.write_bytes((TESTS / "fixtures" / "cmd_shims" / fixture).read_bytes())
        return str(path)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        path.chmod(0o755)
        return str(path)

    def argv(self, shim):
        return [shim, "-p", "--json-schema", self.SCHEMA, "--allowedTools", "mcp__browser__a,mcp__browser__b"]

    def test_node_next_to_the_shim_runs_the_script_with_every_argument_unchanged(self):
        node = self.executable(self.npm / "node.exe")
        argv = self.argv(self.shim("node_script.cmd"))
        self.assertEqual(portable.direct_argv(argv, {"PATH": ""}), [node, str(self.script)] + argv[1:])

    def test_node_on_the_childs_path(self):
        node = self.executable(Path(tempfile.mkdtemp()) / "nodejs" / "node.exe")
        argv = self.argv(self.shim("node_script.cmd", "agy.cmd"))
        self.assertEqual(portable.direct_argv(argv, {"PATH": str(Path(node).parent)}),
                         [node, str(self.script)] + argv[1:])

    def test_node_in_an_install_folder_when_the_path_lacks_it(self):
        node = self.executable(Path(tempfile.mkdtemp()) / "nodejs" / "node.exe")
        argv = self.argv(self.shim("node_script.cmd", "codex.cmd"))
        with mock.patch.object(setup_steps, "install_folders", return_value=[str(Path(node).parent)]):
            self.assertEqual(portable.direct_argv(argv, {"PATH": ""})[:2], [node, str(self.script)])

    def test_a_package_exe_runs_directly(self):
        program = self.executable(self.npm / "node_modules" / "pkg" / "bin" / "tool.exe")
        argv = self.argv(self.shim("own_exe.cmd", "tool.cmd"))
        self.assertEqual(portable.direct_argv(argv, {}), [program] + argv[1:])

    def test_left_as_it_is(self):
        no_node = self.argv(self.shim("node_script.cmd"))
        self.assertIs(portable.direct_argv(no_node, {"PATH": ""}), no_node)        # no node anywhere
        self.executable(self.npm / "node.exe")
        variables = self.argv(self.shim("with_variables.cmd", "vars.cmd"))
        self.assertIs(portable.direct_argv(variables, {}), variables)              # not handled: cmd.exe sets them
        exe = [str(self.npm / "claude.exe"), "-p"]
        self.assertIs(portable.direct_argv(exe, {}), exe)
        shim = self.argv(self.shim("node_script.cmd"))
        with mock.patch.object(portable, "WINDOWS", False):
            self.assertIs(portable.direct_argv(shim, {}), shim)                   # macOS: unchanged

    def test_r5_a_shim_with_any_line_outside_the_template_is_left_to_cmd_exe(self):
        self.executable(self.npm / "node.exe")
        text = (TESTS / "fixtures" / "cmd_shims" / "node_script.cmd").read_text()
        for extra in ("SET NODE_OPTIONS=--x", "@SET NODE_OPTIONS=--x", "set FOO=1", "echo hello", "call other.cmd"):
            shim = self.npm / "claude.cmd"
            shim.write_text(text.replace("CALL :find_dp0", "CALL :find_dp0\r\n" + extra))
            argv = self.argv(str(shim))
            self.assertIs(portable.direct_argv(argv, {}), argv, extra)
            shim.write_text(text + extra + "\r\n")
            self.assertIs(portable.direct_argv(argv, {}), argv, extra)

    def test_every_engine_launch_starts_the_direct_command(self):
        """The four places that start a Claude, agy or Codex session (research, the agent worker, the two
        browser workers) start what portable.direct_command returns, with its environment."""
        for name in ("orchestrator.py", "agent_worker.py", "application_browser.py", "outreach_history_browser.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            start = source.index("launch, ")
            self.assertRegex(source[start:start + 200],
                             r"^launch, (\w+) = portable\.direct_command\(\w+, \1\)\n\s+worker = subprocess\.Popen\(launch,",
                             name)
            popen = source[start:source.index("**portable.worker_spawn_options", start)]
            variable = popen.split(",")[1].split("=")[0].strip()
            self.assertIn("env=%s" % variable, popen, name)


class PnpmYarnShimTests(unittest.TestCase):
    """R8 (owner, 2026-10-29): pnpm's and yarn's shims too. pnpm_global.cmd, pnpm_node_path.cmd, yarn_global.cmd
    and yarn_inner.cmd were written on the Windows VM by pnpm 11.21.0 (`pnpm add -g`) and yarn 1.22.22
    (`yarn global add`) for a local fake package; pnpm_exec_path.cmd by @zkochan/cmd-shim 9.0.8 with
    nodeExecPath (pnpm's own shim library). The folders below repeat the VM's layout."""

    SCHEMA = NpmShimTests.SCHEMA

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        for patch in (mock.patch.object(portable, "WINDOWS", True),
                      mock.patch.object(setup_steps, "install_folders", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.node = Path(tempfile.mkdtemp()) / "nodejs" / "node.exe"
        self.file(self.node)
        self.environment = {"PATH": str(self.node.parent)}

    def file(self, path, data=b""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def shim(self, fixture, relative):
        return self.file(self.base / relative, (TESTS / "fixtures" / "cmd_shims" / fixture).read_bytes())

    def argv(self, shim):
        return [str(shim), "-p", "--json-schema", self.SCHEMA, "x" * 9000]

    def test_pnpm_global_bin(self):
        script = self.file(self.base / "pnhome/global/v11/2de0-1a18976653b-e0436bb23e66faec/node_modules/fake-cli/bin/cli.js")
        argv = self.argv(self.shim("pnpm_global.cmd", "pnhome/bin/fake-cli.CMD"))
        self.assertEqual(portable.direct_command(argv, self.environment),
                         ([str(self.node), str(script)] + argv[1:], self.environment))

    def test_pnpm_sets_node_path_on_the_child_as_its_shim_does(self):
        store = ("pnstore/v11/links/@/fake-cli/directory/"
                 "3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87/node_modules/fake-cli/bin/cli.js")
        script = self.file(self.base / store)
        argv = self.argv(self.shim("pnpm_node_path.cmd", "pnhome/global/v11/x/node_modules/.bin/fake-cli.CMD"))
        launch, environment = portable.direct_command(argv, dict(self.environment))
        self.assertEqual(launch, [str(self.node), str(script)] + argv[1:])
        value = environment["NODE_PATH"]
        self.assertTrue(value.startswith(r"C:\Users\tester\pm\pnstore") and value.endswith(r"\.pnpm\node_modules"))
        _, environment = portable.direct_command(argv, dict(self.environment, NODE_PATH="before"))
        self.assertEqual(environment["NODE_PATH"], value + ";before")   # the ELSE branch: prepended
        self.assertNotIn("NODE_PATH", self.environment)                   # the caller's dict is not changed

    def test_pnpm_node_exec_path(self):
        script = self.file(self.base / "bin/pkg/cli.js")
        text = (TESTS / "fixtures" / "cmd_shims" / "pnpm_exec_path.cmd").read_text().replace(
            r"C:\Program Files\nodejs\node.exe", str(self.node))
        shim = self.file(self.base / "bin/tool.cmd", text.encode())
        argv = self.argv(shim)
        self.assertEqual(portable.direct_command(argv, {})[0], [str(self.node), str(script)] + argv[1:])

    def test_yarn_global_bin_follows_its_next_shim(self):
        script = self.file(self.base / "yglobal/node_modules/fake-cli/bin/cli.js")
        self.shim("yarn_inner.cmd", "yglobal/node_modules/.bin/fake-cli.cmd")
        argv = self.argv(self.shim("yarn_global.cmd", "yprefix/bin/fake-cli.cmd"))
        self.assertEqual(portable.direct_command(argv, self.environment)[0], [str(self.node), str(script)] + argv[1:])

    def test_any_other_line_is_left_to_cmd_exe(self):
        self.file(self.base / "pnhome/global/v11/2de0-1a18976653b-e0436bb23e66faec/node_modules/fake-cli/bin/cli.js")
        text = (TESTS / "fixtures" / "cmd_shims" / "pnpm_global.cmd").read_text()
        for extra in ("@SET FOO=1", "SET FOO=1", '@SET "PATH=C:\\x:%PATH%"', "echo hi"):
            for changed in (text.replace("@SETLOCAL", "@SETLOCAL\r\n" + extra), text + extra + "\r\n"):
                argv = self.argv(self.file(self.base / "pnhome/bin/fake-cli.CMD", changed.encode()))
                self.assertEqual(portable.direct_command(argv, self.environment), (argv, self.environment), extra)


if __name__ == "__main__":
    unittest.main()
