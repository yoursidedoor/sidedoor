"""Owner, 2026-11-13 ("Same thing for signs ... some background process should auto close it if there is no
longer activity"; "5mins"): an ordinary live view with no input for 5 minutes closes by itself AS DONE: the
ending Done has (the site's session kept, the card's hook told it finished), never the "Timed out" that
dropped a sign-in. Fakes only (tests/live_view_fakes.py): no browser."""
import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import unittest

import live_view
import test_live_view as lv


@unittest.skipUnless(lv.DEPENDENCIES, "the live view's packages (fastapi, httpx, websockets) are not installed")
class IdleClose(lv.Case):
    def test_the_idle_close_is_five_minutes(self):
        self.assertEqual(live_view.IDLE_SECONDS, 300)
        self.assertEqual(live_view.LiveView(root=self.home, config=self.config).idle_seconds, 300)
        self.assertLess(live_view.IDLE_SECONDS, live_view.LEASE_SECONDS)       # idleness never reaches "Timed out" now

    async def test_no_input_for_that_long_ends_the_view_as_done_and_input_keeps_it_open(self):
        ended = []
        view, session = await self.live(self.view(idle_seconds=0.6), on_ended=ended.append)
        for _ in range(5):                                                    # active for a second: longer than the idle time
            await asyncio.sleep(0.2)
            view.handle(session, lv.CLICK)
        self.assertIs(view.session, session)
        self.assertEqual(ended, [])
        self.assertTrue(await lv.until(lambda: ended, 4.0))                   # then the person stops
        self.assertEqual((ended[0].reason, ended[0].words), (live_view.FINISHED, "Done. Sidedoor carries on from here."))
        self.assertIn(ended[0].reason, live_view.STEP_FINISHED)               # kept and released exactly as Done is
        self.assertTrue(self.lock_free())
        self.assertIsNone(view.target(session.target.id))                     # a finished step: no longer offered

    async def test_the_viewer_is_told_the_done_line_and_the_time_left_counts_to_the_idle_close(self):
        view, session = await self.live(self.view(idle_seconds=120))
        socket = lv.FakeSocket()
        serving = asyncio.create_task(view.serve(session, socket))
        self.assertTrue(await lv.until(lambda: socket.messages("status")))
        self.assertEqual(socket.messages("status")[0]["minutes_left"], 1)     # the idle close, sooner than the lease
        view.idle_seconds = 0.2
        self.assertTrue(await lv.until(lambda: socket.messages("ended"), 4.0))
        self.assertEqual(socket.messages("ended")[0]["words"], "Done. Sidedoor carries on from here.")
        await serving

    async def test_timed_out_remains_only_for_the_thirty_minute_cap(self):
        ended = []
        view, session = await self.live(self.view(lease_seconds=0.4, lease_max_seconds=0.9, idle_seconds=5), on_ended=ended.append)
        for _ in range(8):                                                    # active the whole time: the cap ends it
            await asyncio.sleep(0.2)
            if view.session is session:
                view.handle(session, lv.CLICK)
        self.assertTrue(await lv.until(lambda: ended))
        self.assertEqual(ended[0].reason, live_view.LEASE)
        self.assertIn("Timed out.", ended[0].words)

    async def test_an_attached_view_has_no_idle_close(self):
        import json
        from pathlib import Path
        import browser_runtime
        folder = Path(self.temp.name) / "submit-session"
        folder.mkdir()
        (folder / browser_runtime.GUARD_AUDIT).write_text(json.dumps(
            {"decision": "BACKGROUND_LAUNCHED", "endpoint": self.chrome.endpoint, "headless": True}) + "\n")
        view = self.view(idle_seconds=0.2)
        session = await view.start(view.offer_attached("job-1", folder, "jobs.example.test"))
        self.addAsyncCleanup(view.end, live_view.CANCEL)
        await asyncio.sleep(0.8)
        self.assertIs(view.session, session)                                  # its own rule: the hold's 9 minutes


if __name__ == "__main__":
    unittest.main()
