"""WB3: on Windows every console program the windowed sidedoor.exe starts gets no console window of its own
(CREATE_NO_WINDOW): schtasks (start at login), `codex login status` (doctor and setup), `node --version`
(the runtime check), the browser guard's Node server and a configured application adapter. Elsewhere
nothing changes. The calls are made with Windows switched on in `portable`; nothing is started."""
import profile_home  # noqa: F401
import json
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

import applications
import browser_guard
import browser_runtime
import login_item
import portable
import setup_steps

HIDDEN = {"creationflags": portable.CREATE_NO_WINDOW}


class Recorder:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


class NoWindowTests(unittest.TestCase):
    def windows(self, on=True):
        return mock.patch.object(portable, "WINDOWS", on)

    def test_helper(self):
        with self.windows():
            self.assertEqual(portable.no_window(), HIDDEN)
        with self.windows(False):
            self.assertEqual(portable.no_window(), {})

    def test_schtasks(self):
        run = Recorder()
        with self.windows(), mock.patch.object(login_item.subprocess, "run", run):
            login_item._run(["schtasks", "/Query", "/TN", "Sidedoor"])
        self.assertEqual(run.calls[0][1]["creationflags"], portable.CREATE_NO_WINDOW)

    def test_codex_login_status(self):
        run = Recorder("Logged in using ChatGPT")
        with self.windows():
            self.assertEqual(setup_steps.codex_login_status("codex.exe", run), "SIGNED_IN")
        self.assertEqual(run.calls[0][1]["creationflags"], portable.CREATE_NO_WINDOW)

    def test_node_version(self):
        run = Recorder("v" + browser_runtime.NODE_VERSION)
        with self.windows(), mock.patch.object(browser_runtime, "missing", lambda config=None: []):
            browser_runtime.verify({"browser_driver": {"runtime_dir": "/nowhere"}}, run=run)
        self.assertEqual(run.calls[0][0][1:], ["--version"])
        self.assertEqual(run.calls[0][1]["creationflags"], portable.CREATE_NO_WINDOW)

    def test_application_adapter(self):
        run = Recorder(json.dumps({"ok": True}))
        adapter = applications.CommandAdapter({"application_adapter_command": [str(Path("/bin/adapter").resolve())]})
        with self.windows(), mock.patch.object(applications.subprocess, "run", run):
            adapter.call("inspect", {})
        self.assertEqual(run.calls[0][1]["creationflags"], portable.CREATE_NO_WINDOW)

    def test_browser_guard_node_child(self):
        source = Path(browser_guard.__file__).read_text(encoding="utf-8")
        start = source.index("self.child = subprocess.Popen(")
        self.assertIn("**portable.no_window()", source[start:source.index("\n\n", start)])

    def test_other_windows_children_already_have_no_window(self):
        import scheduler_loop
        self.assertTrue(scheduler_loop.detach_options(True)["creationflags"] & portable.CREATE_NO_WINDOW)
        with self.windows(), mock.patch.object(portable, "no_crash_dialogs"):
            self.assertTrue(portable.worker_spawn_options()["creationflags"] & portable.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
