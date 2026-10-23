"""E5.T3: the tray icon, with pystray faked (no real menu bar icon is ever shown here)."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import contextlib
import importlib.util
import io
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import doctor
import scheduler_control
import scheduler_loop
import tray

DASHBOARD = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DASHBOARD:
    import dashboard


class FakeItem:
    def __init__(self, text, action, default=False, visible=True):
        self.text, self.action, self.default, self._visible = text, action, default, visible

    @property
    def visible(self):
        return self._visible(self) if callable(self._visible) else self._visible


class FakeMenu:
    def __init__(self, *items):
        self.items = items


class FakeIcon:
    fail_with = None

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.stopped = threading.Event()
        self.updates = 0

    def run(self, setup=None):
        if self.fail_with is not None:
            raise self.fail_with
        threading.Thread(target=setup, args=(self,)).start()
        self.stopped.wait(20)

    def stop(self):
        self.stopped.set()

    def update_menu(self):
        self.updates += 1

    def item(self, text):
        return next(item for item in self.menu.items if item.text == text)


class FakePystray:
    Menu, MenuItem = FakeMenu, FakeItem

    def __init__(self, fail_with=None):
        self.icons = []
        self.fail_with = fail_with

    def Icon(self, *args):
        icon = FakeIcon(*args)
        icon.fail_with = self.fail_with
        self.icons.append(icon)
        return icon


def fake_modules(fail_with=None):
    from PIL import Image
    return FakePystray(fail_with), Image


class FakeServer:
    def __init__(self, crash=None):
        self.should_exit = False
        self.crash = crash
        self.ran = threading.Event()

    def run(self, sockets=None):
        self.ran.set()
        if self.crash is not None:
            raise self.crash
        while not self.should_exit:
            time.sleep(0.01)


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class MenuTest(unittest.TestCase):
    def actions(self, flags=None):
        actions = mock.Mock()
        actions.flags.return_value = flags or {}
        return actions

    def test_visibility_follows_overview(self):
        self.assertEqual(tray.menu_visibility({}), (True, False))
        self.assertEqual(tray.menu_visibility({"user_paused_at": "t"}), (False, True))
        self.assertEqual(tray.menu_visibility({"license_paused_at": "t"}), (True, False))  # Pause stays available
        self.assertEqual(tray.menu_visibility({"user_paused_at": "t", "license_paused_at": "t"}), (False, False))
        self.assertEqual(tray.menu_visibility(None), (True, False))

    def test_each_item_calls_its_action_and_refreshes_visibility(self):
        actions = self.actions()
        icon = tray.Tray(actions, modules=fake_modules(), call_on_main=lambda function: function())
        icon._refresh_flags()
        menu = icon.menu()
        labels = [(item.text, item.visible) for item in menu.items]
        self.assertEqual(labels, [("Open dashboard", True), ("Pause", True), ("Resume", False), ("Quit", True)])
        self.assertTrue(menu.items[0].default)
        for item, name in zip(menu.items, ("open_dashboard", "pause", "resume", "quit")):
            item.action(None, item)
            getattr(actions, name).assert_called_once_with()
        actions.flags.return_value = {"user_paused_at": "2026-10-22T10:00:00+00:00"}
        menu.items[1].action(None, menu.items[1])
        self.assertEqual([item.visible for item in menu.items], [True, False, True, True])

    def test_a_failing_action_does_not_break_the_menu(self):
        actions = self.actions()
        actions.pause.side_effect = RuntimeError("database is locked")
        icon = tray.Tray(actions, modules=fake_modules(), call_on_main=lambda function: function())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            icon.menu().items[1].action(None, None)
        self.assertIn("failed (RuntimeError)", err.getvalue())

    def test_a_pause_set_elsewhere_reaches_the_menu(self):
        actions = self.actions()
        icon = tray.Tray(actions, modules=fake_modules(), call_on_main=lambda function: function())
        with mock.patch.object(tray, "REFRESH_SECONDS", 0.01):
            worker = threading.Thread(target=icon.run)
            worker.start()
            self.assertTrue(wait_for(lambda: icon.icon is not None and icon.icon.visible))
            actions.flags.return_value = {"user_paused_at": "t"}
            self.assertTrue(wait_for(lambda: icon.icon.updates >= 1))
            icon.stop()
            worker.join(5)
        self.assertFalse(worker.is_alive())


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class ActionsTest(unittest.TestCase):
    def test_open_dashboard_opens_the_tokened_link_and_pause_uses_overviews_function(self):
        with tempfile.TemporaryDirectory() as root:
            actions = dashboard.TrayActions("SECRET", root=Path(root))
            with mock.patch.object(dashboard, "announce") as announce:
                actions.open_dashboard()
            announce.assert_called_once_with("http://127.0.0.1:47823/?t=SECRET", True)
            with mock.patch.object(dashboard, "set_user_pause", wraps=dashboard.set_user_pause) as shared:
                actions.pause()
                self.assertTrue(actions.flags()["user_paused_at"])
                actions.resume()
                self.assertIsNone(actions.flags()["user_paused_at"])
            self.assertEqual(shared.call_args_list, [mock.call(Path(root), True, "tray"), mock.call(Path(root), False, "tray")])
            # Overview's Pause goes through the same function.
            database = dashboard.write_connection(root)
            try:
                scheduler_control.set_license_pause(database, True, "expired")
            finally:
                database.close()
            self.assertEqual(tray.menu_visibility(actions.flags()), (True, False))

    def test_make_tray_falls_back_and_records_why(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(tray, "unavailable", return_value="No desktop session."):
                self.assertIsNone(dashboard.make_tray("S", home, Path(home)))
            self.assertEqual(tray.read_state(home)["state"], "unavailable")
            with mock.patch.object(tray, "unavailable", return_value=None), \
                    mock.patch.object(tray, "load", side_effect=ImportError("No module named 'pystray'")):
                self.assertIsNone(dashboard.make_tray("S", home, Path(home)))
            self.assertIn("pystray", tray.read_state(home)["reason"])
            line = doctor.check_tray(home, platform="darwin")
            self.assertEqual(line["status"], "WARN")
            self.assertIn("pystray", line["line"])
            self.assertEqual(doctor.check_tray(home, platform="linux")["status"], "OK")


@unittest.skipUnless(DASHBOARD, "dashboard packages (fastapi, httpx) are not installed")
class ServeWithTrayTest(unittest.TestCase):
    def serve(self, server, modules, act):
        """dashboard.serve(tray=True) with a fake server and fake pystray; `act(icon, home)` runs in a
        thread once serving. Returns (exit code or the exception, home's tray state, loop)."""
        loop = mock.Mock()
        with tempfile.TemporaryDirectory() as home:
            made = []

            def make(secret, home_path, root):
                made.append(tray.Tray(dashboard.TrayActions(secret, root), modules=modules,
                                      call_on_main=lambda function: function()))
                return made[0]
            with mock.patch.object(dashboard, "bind", return_value=mock.Mock()), \
                    mock.patch.object(dashboard.uvicorn, "Server", return_value=server), \
                    mock.patch.object(dashboard, "make_tray", make), \
                    mock.patch.object(tray, "mac_signals"), \
                    mock.patch.object(scheduler_loop, "start_license_heartbeat") as heartbeat, \
                    mock.patch.object(scheduler_loop, "stop_license_heartbeat") as stop_heartbeat, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                actor = threading.Thread(target=lambda: wait_for(lambda: made and server.ran.is_set()) and act(made[0], home))
                actor.start()
                before = {number: signal.getsignal(number) for number in dashboard.STOP_SIGNALS}
                try:
                    result = dashboard.serve(open_browser=False, home=home, scheduler=loop, tray=True)
                except BaseException as error:
                    result = error
                actor.join(10)
                self.assertEqual({number: signal.getsignal(number) for number in dashboard.STOP_SIGNALS}, before)
                heartbeat.assert_called_once()
                stop_heartbeat.assert_called_once()
                return result, tray.read_state(home), loop

    def test_quit_stops_the_server_and_the_loop_with_exit_0(self):
        server = FakeServer()

        def quit_(icon, home):
            wait_for(lambda: (tray.read_state(home) or {}).get("state") == "shown")
            icon.icon.item("Quit").action(icon.icon, None)
        result, state, loop = self.serve(server, fake_modules(), quit_)
        self.assertEqual(result, 0)
        self.assertEqual(state["state"], "shown")
        self.assertTrue(server.should_exit)
        loop.stop.assert_called_once_with()
        self.assertIsNone(scheduler_loop.current())

    def test_sigterm_stops_serve_with_the_tray_running(self):
        server = FakeServer()

        def sigterm(icon, home):
            wait_for(lambda: (tray.read_state(home) or {}).get("state") == "shown")
            os.kill(os.getpid(), signal.SIGTERM)
        result, state, loop = self.serve(server, fake_modules(), sigterm)
        self.assertEqual(result, 0)
        self.assertTrue(server.should_exit)
        loop.stop.assert_called_once_with()

    def test_a_tray_that_fails_to_start_leaves_the_dashboard_running(self):
        server = FakeServer()
        seen = {}

        def later_sigint(icon, home):
            wait_for(lambda: (tray.read_state(home) or {}).get("state") == "unavailable")
            time.sleep(0.2)
            seen["still serving"] = not server.should_exit
            os.kill(os.getpid(), signal.SIGINT)
        result, state, loop = self.serve(server, fake_modules(fail_with=RuntimeError("no status bar")), later_sigint)
        self.assertEqual(result, 0)
        self.assertTrue(seen["still serving"])
        self.assertEqual(state["state"], "unavailable")
        self.assertIn("no status bar", state["reason"])

    def test_a_server_crash_still_exits_non_zero(self):
        result, state, loop = self.serve(FakeServer(crash=RuntimeError("boom")), fake_modules(), lambda icon, home: None)
        self.assertIsInstance(result, RuntimeError)
        loop.stop.assert_called_once_with()


class UnavailableTest(unittest.TestCase):
    def test_where_no_tray_is_shown(self):
        self.assertIn("Linux", tray.unavailable(platform="linux"))
        self.assertIn("desktop session", tray.unavailable(platform="darwin", gui_session=lambda: False))
        self.assertIsNone(tray.unavailable(platform="darwin", gui_session=lambda: True))
        self.assertIsNone(tray.unavailable(platform="win32"))

    def test_the_icon_is_the_brand_icon(self):
        self.assertTrue(tray.icon_path().is_file())
        self.assertEqual(tray.icon_path().name, "yoursidedoor-icon-512.png")


if __name__ == "__main__":
    unittest.main()
