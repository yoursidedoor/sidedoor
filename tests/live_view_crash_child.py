"""The process tests/test_live_view.py kills (CrashTest): a live session on fakes, holding the browser lock.
Its fake guard leaves a stand-in "Chrome" process behind on the profile. Usage: <home> <profile>."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import asyncio
import sys

import live_view
import live_view_fakes as fakes


async def main(home, profile):
    chrome = fakes.FakeChrome().start()
    view = live_view.LiveView(root=home, config={"browser_driver": {"profile": profile}},
                              guard_command=fakes.guard_command(chrome, "--orphan-chrome", profile),
                              is_supported=lambda config: True)
    session = await view.start(view.offer("job-1", "https://jobs.example.test/apply", "sign_in"))
    print("LIVE" if isinstance(session, live_view.Session) else "FAILED", flush=True)
    await asyncio.sleep(120)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
