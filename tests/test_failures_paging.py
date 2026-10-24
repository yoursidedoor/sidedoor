"""Failures lists page past their first 50, and every log stream and run file loads its older lines
(owner, 2026-10-24: "do we dump everything on the webpage? or is there some kind of next page / load
more mechanism?"). Older chunks go through the same redaction and prompt-echo rules as the tail."""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one)
import datetime as dt
import importlib.util
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import failures_page
import setup_steps
import test_dashboard_overview as base

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if DEPENDENCIES:
    from fastapi.testclient import TestClient

    import dashboard

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
RESEARCH = "7feefb1b-f6b3-45d2-8d2d-69d916574ff4"


def walk_stream(path, secrets=()):
    """Every chunk of a stream, newest first, as the Older lines links walk it."""
    chunks, end = [], None
    while True:
        found = failures_page.stream_chunk(path, list(secrets), end)
        chunks.append(found["lines"])
        if found["older"] is None:
            return chunks
        assert end is None or found["older"] < end
        end = found["older"]
        assert len(chunks) < 1000


def codex_log(path, echo_lines=3000):
    """A research step log shaped like the real ones (~150 KB): codex's header, "user", the prompt echo
    (the user's profile), then the model's working and codex's own lines."""
    header = ["OpenAI Codex v0.0.0 (research preview)", "--------", "workdir: /tmp/x", "model: gpt-x",
              "provider: openai", "approval: never", "sandbox: read-only", "reasoning effort: medium",
              "session id: 0000", "--------"]
    echo = ["Jane Doe lives at 12 Main St and wants role %d with salary 180000 and profile text" % n
            for n in range(echo_lines)]
    echo.insert(echo_lines // 2, "Error: Jane Doe's own line inside the prompt")
    echo.insert(echo_lines // 3, "ERROR: stream disconnected before completion")
    tail = ["thinking", "Jane's model reasoning about the profile", "codex", "Jane's answer text",
            "ERROR: final turn failed", "tokens used: 1234"]
    path.write_text("\n".join(header + ["user"] + echo + tail) + "\n")


class ChunkTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.logs = Path(self.directory.name) / "logs"
        self.logs.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_older_chunks_reach_every_line_once_in_order(self):
        path = self.logs / "research.stdout.log"
        path.write_text("".join("line %05d %s\n" % (n, "x" * 40) for n in range(5000)))
        chunks = walk_stream(path)
        self.assertEqual(chunks[0], failures_page.tail(self.logs, "research.stdout.log", []))   # the tail is unchanged
        every = [line for chunk in reversed(chunks) for line in chunk]
        self.assertEqual(every, ["line %05d %s" % (n, "x" * 40) for n in range(5000)])
        self.assertTrue(all(len(chunk) <= failures_page.LOG_LINES for chunk in chunks))

    def test_offsets_are_validated_and_clamped(self):
        for value in ("-5", "abc", "1e3", "12.5", " 7", "9" * 19, "", None, "0x10", "١٢"):
            self.assertIsNone(failures_page.offset_of(value), value)
        self.assertEqual(failures_page.offset_of("123"), 123)
        path = self.logs / "launchd.stderr.log"
        path.write_text("a\nb\n")
        self.assertEqual(failures_page.stream_chunk(path, [], 10 ** 17)["lines"], ["a", "b"])   # clamped to the size
        self.assertEqual(failures_page.stream_chunk(path, [], 0), {"state": "ok", "lines": [], "older": None})

    def test_a_chunk_ending_inside_a_line_never_shows_half_a_secret(self):
        path = self.logs / "launchd.stderr.log"
        body = "".join("filler %04d\n" % n for n in range(10)) + "key " + KEY + " end\n" + "after\n"
        path.write_bytes(body.encode())
        inside = body.index(KEY) + 6     # an offset someone typed, inside the key's line
        for end in range(body.index("key "), body.index("after")):
            shown = "\n".join(failures_page.stream_chunk(path, [KEY], end)["lines"])
            self.assertNotIn(KEY[:6], shown)
        self.assertEqual(failures_page.stream_chunk(path, [KEY], inside)["lines"][-1], "filler 0009")

    def test_a_line_longer_than_a_chunk_still_moves_back(self):
        path = self.logs / "launchd.stdout.log"
        path.write_text("first\n" + "y" * 100000 + "\nlast\n")
        chunks = walk_stream(path)
        self.assertEqual(chunks[0], ["last"])
        self.assertEqual(chunks[-1], ["first"])

    def test_every_older_chunk_of_a_codex_log_keeps_the_prompt_hidden(self):
        path = self.logs / ("%s.ai-11fb682be0.2.qualification.log" % RESEARCH)
        codex_log(path)
        self.assertGreater(path.stat().st_size, 150000)
        end, shown, chunks = None, [], 0
        while True:
            found = failures_page._file(path, [], None, (path.name, end) if end is not None else None)
            chunks += 1
            shown += found["lines"]
            self.assertTrue(found["lines"])
            if found["older"] is None:
                break
            end = found["older"]
        self.assertGreater(chunks, 3)
        text = "\n".join(shown)
        self.assertNotIn("Jane", text)
        self.assertNotIn("Main St", text)
        self.assertIn("ERROR: stream disconnected before completion", text)
        self.assertIn("ERROR: final turn failed", text)
        self.assertIn("model: gpt-x", text)                 # the header, from the head and the first chunk

    def test_an_older_chunk_entirely_inside_the_echo_shows_only_the_note(self):
        path = self.logs / ("%s.ai-11fb682be0.2.qualification.log" % RESEARCH)
        codex_log(path, echo_lines=6000)
        middle = path.stat().st_size // 2 + 40000          # well inside the echo, past the inserted lines
        found = failures_page._file(path, [], None, (path.name, middle))
        self.assertEqual(found["lines"], [failures_page.PROMPT_WITHHELD])
        self.assertIsNotNone(found["older"])

    def test_an_older_trace_chunk_shows_only_error_events(self):
        path = self.logs / "trace.jsonl"
        path.write_text("".join('{"type":"item.completed","item":{"type":"agent_message","text":"Jane %d"}}\n' % n
                                for n in range(3000)) + '{"type":"error","message":"boom"}\n')
        found = failures_page._file(path, [], "dir/trace.jsonl", ("dir/trace.jsonl", path.stat().st_size // 2))
        self.assertEqual(found["lines"], [failures_page.TRACE_NOTE])


class PagingTest(unittest.TestCase):
    def test_pages_clamp(self):
        entries = list(range(120))
        self.assertEqual(failures_page.page_of(entries, 2), (list(range(50, 100)), {"page": 2, "pages": 3, "total": 120}))
        self.assertEqual(failures_page.page_of(entries, 99)[1]["page"], 3)
        self.assertEqual(failures_page.page_of(entries, 0)[1]["page"], 1)
        self.assertEqual(failures_page.page_of([], 5), ([], {"page": 1, "pages": 1, "total": 0}))

    def test_links_keep_the_choices_and_never_the_token(self):
        params = {"component": "research", "day": "2026-10-22", "q": "boom", "t": "secret-token", "level": "all"}
        link = failures_page.href(params, anchor="outreach", section="outreach", page=2)
        self.assertEqual(link, "/failures?component=research&q=boom&day=2026-10-22&section=outreach&page=2#outreach")


def run_part(html):
    start = html.index('<section id="run"')
    return html[start:html.index("</section>", start)]


def many(group, count, you=False):
    start = dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc)
    return [failures_page._item(group, "%s failure %03d" % (group, n), "Sidedoor tries again.",
                                at=start - dt.timedelta(minutes=n), you=you) for n in range(count)]


@unittest.skipUnless(DEPENDENCIES, "dashboard packages (fastapi, httpx) are not installed")
class PageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / "home"
        base.finished_home(self.home)
        base.database_with_schema(self.root).close()
        self.client = None

    def tearDown(self):
        self.directory.cleanup()

    def get(self, path, params=None, headers=None):
        if self.client is None:
            app = dashboard.create_app(base.SECRET, base.PORT, home=self.home, root=self.root, config=base.CONFIG)
            self.client = TestClient(app, base_url=f"http://127.0.0.1:{base.PORT}", follow_redirects=False)
            self.client.get("/", params={"t": base.SECRET})
        with mock.patch.object(setup_steps, "chrome_path", return_value="/chrome"), \
                mock.patch.object(failures_page, "application_failures", lambda *a: many("applications", 120)), \
                mock.patch.object(failures_page, "finder_failures", lambda *a: many("outreach", 60) + many("outreach", 3, True)):
            return self.client.get(path, params=params, headers=headers)

    def hrefs(self, html, word):
        return [urllib.parse.unquote(m.replace("&amp;", "&")) for m in re.findall(r'<a href="([^"]+)"[^>]*>%s</a>' % word, html)]

    def test_every_section_links_to_its_full_list_and_the_pages_resolve(self):
        page = self.get("/failures", {"component": "research", "day": "2026-10-22"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("And 70 more.", page.text)
        self.assertIn("And 10 more.", page.text)
        links = self.hrefs(page.text, "See them")
        self.assertIn("/failures?component=research&day=2026-10-22&section=applications&page=2#applications", links)
        self.assertIn("/failures?component=research&day=2026-10-22&section=outreach&page=2#outreach", links)
        second = self.get(links[[l for l in links].index(
            "/failures?component=research&day=2026-10-22&section=applications&page=2#applications")].split("#")[0])
        self.assertIn("Page 2 of 3 (120)", second.text)
        self.assertIn("applications failure 050", second.text)
        self.assertNotIn("applications failure 049", second.text)
        for word in ("Previous", "Next", "Back to the summary"):
            [target] = self.hrefs(second.text, word)
            self.assertIn("component=research", target)
            self.assertEqual(self.get(target.split("#")[0]).status_code, 200)
        last = self.get("/failures", {"section": "applications", "page": "99"})
        self.assertIn("Page 3 of 3 (120)", last.text)
        self.assertIn("applications failure 119", last.text)
        self.assertEqual(self.hrefs(last.text, "Next"), [])
        for junk in ("abc", "-1", "9" * 30):
            self.assertIn("Page 1 of 3 (120)", self.get("/failures", {"section": "applications", "page": junk}).text)
        self.assertNotIn("Page 1 of", self.get("/failures", {"section": "nope"}).text)

    def write_log(self):
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "research.stderr.log").write_text(
            "".join("2026-10-22T10:%02d:00Z line %05d %s\n" % (n % 60, n, "x" * 40) for n in range(3000))
            + "key " + KEY + "\n")
        return logs

    def test_older_lines_without_js_and_with_htmx_until_the_start_of_the_file(self):
        self.write_log()
        page = self.get("/failures", {"component": "research", "level": "stderr"})
        self.assertNotIn(KEY, page.text)
        [older] = self.hrefs(page.text, "Older lines")
        self.assertIn("log=research.stderr.log", older)
        self.assertTrue(older.endswith("#log-research-stderr"))
        full = self.get(older.split("#")[0])
        self.assertEqual(full.status_code, 200)
        self.assertIn("an older part", full.text)
        self.assertIn("Back to the newest lines", full.text)
        self.assertIn("line 02799", full.text)
        hx = re.search(r'hx-get="([^"]+)"', page.text).group(1).replace("&amp;", "&")
        seen, steps = set(), 0
        while True:
            fragment = self.get(hx, headers={"hx-request": "true"})
            self.assertEqual(fragment.status_code, 200)
            self.assertNotIn("<html", fragment.text)
            self.assertNotIn(KEY, fragment.text)
            seen.update(re.findall(r"line (\d{5})", fragment.text))
            steps += 1
            found = re.search(r'hx-get="([^"]+)"', fragment.text)
            if not found:
                self.assertIn("Start of file", fragment.text)
                break
            nojs = re.search(r'<a href="([^"]+)"', fragment.text).group(1).replace("&amp;", "&")
            self.assertEqual(self.get(nojs.split("#")[0]).status_code, 200)
            hx = found.group(1).replace("&amp;", "&")
        first = int(re.findall(r"line (\d{5})", page.text)[0])   # where the tail's 200 lines begin
        self.assertEqual(seen, {"%05d" % n for n in range(first)})
        redirect = self.get(hx.replace("&amp;", "&"))
        self.assertEqual(redirect.status_code, 303)
        self.assertTrue(redirect.headers["location"].startswith("/failures?"))

    def test_search_and_day_apply_to_the_older_chunk(self):
        self.write_log()
        page = self.get("/failures", {"component": "research", "level": "stderr", "q": "line 0010"})
        self.assertIn("(no matches)", page.text)
        hx = re.search(r'hx-get="([^"]+)"', page.text).group(1).replace("&amp;", "&")
        self.assertIn("q=line+0010", hx)
        fragment = self.get(hx, headers={"hx-request": "true"})
        self.assertIn("No matching lines in this older part.", fragment.text)
        for bad in ("-4", "abc", "9" * 40):
            response = self.get("/failures", {"log": "research.stderr.log", "before": bad})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("an older part", response.text)
        self.assertEqual(self.get("/failures", {"log": "../../etc/passwd", "before": "10"}).status_code, 200)
        fragment = self.get("/failures/older", {"log": "../x", "before": "5"}, headers={"hx-request": "true"})
        self.assertIn("Nothing older can be shown here.", fragment.text)

    def test_a_run_file_loads_its_older_lines_without_the_prompt(self):
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / ("%s.ai-11fb682be0.2.qualification.log" % RESEARCH)
        codex_log(path)
        run = "research:" + RESEARCH
        page = self.get("/failures", {"run": run})
        self.assertNotIn("Jane", page.text)
        [older] = self.hrefs(page.text, "Older lines")
        self.assertIn("file=" + path.name, older)
        self.assertTrue(older.endswith("#run"))
        full = self.get(older.split("#")[0])
        self.assertIn("an older part", full.text)
        self.assertNotIn("Jane", full.text)
        hx = re.search(r'hx-get="([^"]+)"', page.text).group(1).replace("&amp;", "&")
        while hx:
            fragment = self.get(hx, headers={"hx-request": "true"})
            self.assertNotIn("Jane", fragment.text)
            found = re.search(r'hx-get="([^"]+)"', fragment.text)
            hx = found.group(1).replace("&amp;", "&") if found else None
        self.assertIn("Start of file", fragment.text)
        self.assertIn("model: gpt-x", fragment.text)


    def test_a_runs_records_and_files_page(self):
        import os
        import sqlite3
        import test_dashboard_failures as failures_tests
        database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        start = dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc)
        failures_tests.task(database, "t-many", "outreach-draft-composition", "RETRY_WAIT", start,
                            events=[("RETRY_WAIT", '{"blocker":"EVENT_%03d"}' % n, start + dt.timedelta(minutes=n))
                                    for n in range(95)])
        database.commit()
        database.close()
        page = self.get("/failures", {"run": "task:t-many", "day": "2026-10-22"})
        self.assertIn("EVENT_094", run_part(page.text))
        self.assertNotIn("EVENT_054", run_part(page.text))
        self.assertIn("And 55 older records.", run_part(page.text))
        self.assertIn("Page 1 of 3 (95)", run_part(page.text))
        [following] = self.hrefs(run_part(page.text), "Next")
        self.assertIn("records_page=2", following)
        self.assertIn("day=2026-10-22", following)
        second = self.get(following.split("#")[0])
        self.assertIn("EVENT_054", run_part(second.text))
        self.assertNotIn("EVENT_094", run_part(second.text))
        [back] = self.hrefs(run_part(second.text), "Previous")
        self.assertEqual(self.get(back.split("#")[0]).status_code, 200)
        last = self.get("/failures", {"run": "task:t-many", "records_page": "40"})
        self.assertIn("Page 3 of 3 (95)", run_part(last.text))
        self.assertIn("EVENT_000", run_part(last.text))

        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        for n in range(15):
            path = logs / ("%s.step-%02d.1.discovery.log" % (RESEARCH, n))
            path.write_text("step %02d\n" % n)
            os.utime(path, (1_700_000_000 + n, 1_700_000_000 + n))
        page = self.get("/failures", {"run": "research:" + RESEARCH})
        self.assertIn("And 3 more files.", run_part(page.text))
        self.assertIn("step-14", run_part(page.text))
        self.assertNotIn("step-02", run_part(page.text))
        [following] = self.hrefs(run_part(page.text), "Next")
        second = self.get(following.split("#")[0])
        self.assertIn("Page 2 of 2 (15)", run_part(second.text))
        self.assertIn("step-00", run_part(second.text))
        self.assertNotIn("step-14", run_part(second.text))

    def test_each_part_of_the_runs_list_pages_keeping_the_day(self):
        import os
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        moment = dt.datetime(2026, 10, 22, 18, tzinfo=dt.timezone.utc).timestamp()
        for n in range(35):
            path = logs / ("%08x-0000-4000-8000-000000000000.ai-1.1.discovery.log" % n)
            path.write_text("x\n")
            os.utime(path, (moment + n, moment + n))
        page = self.get("/failures", {"day": "2026-10-22"})
        self.assertIn("And 5 more Research runs.", page.text)
        [more] = self.hrefs(page.text, "See them")[-1:]
        self.assertIn("runs_part=research", more)
        self.assertIn("day=2026-10-22", more)
        second = self.get(more.split("#")[0])
        self.assertIn("Page 2 of 2 (35)", second.text)
        self.assertIn("Research run 00000000", second.text)
        self.assertNotIn("Research run 00000022", second.text)
        [back] = self.hrefs(second.text, "Previous")
        self.assertIn("day=2026-10-22", back)
        self.assertIn("Research run 00000022", self.get(back.split("#")[0]).text)
        self.assertNotIn("more Research runs", self.get("/failures", {"day": "2026-10-23"}).text)


    def test_browser_sessions_leave_the_runs_list_but_open_from_their_own_runs(self):
        import json
        import sqlite3
        reports = self.root / "reports"
        browser = reports / "application-browser" / "request-7vesc5ha"
        finder = reports / "linkedin-contacts" / "run-abc123"
        recovery = reports / "application-recovery" / "decision-1"
        for folder, line in ((browser, "browser said hello"), (finder, "finder said hello"), (recovery, "recovery said hello")):
            folder.mkdir(parents=True)
            (folder / "stderr.log").write_text(line + "\n")
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / ("%s.ai-1.1.discovery.log" % RESEARCH)).write_text("x\n")
        database = sqlite3.connect(self.root / "state" / "orchestrator.sqlite3")
        database.execute("CREATE TABLE IF NOT EXISTS desktop_requests (request_id TEXT, job_key TEXT, operation TEXT, "
                         "state TEXT, created_at TEXT, result TEXT)")
        database.execute("CREATE TABLE IF NOT EXISTS linkedin_contact_runs (run_id TEXT, company TEXT, started_at TEXT, "
                         "state TEXT, result TEXT)")
        database.execute("INSERT INTO desktop_requests VALUES ('request-7vesc5ha','acme:1','submit','FAILED',"
                         "'2026-10-22T10:00:00+00:00',?)", (json.dumps({"directory": str(browser)}),))
        database.execute("INSERT INTO linkedin_contact_runs VALUES ('f1','Acme','2026-10-22T10:00:00+00:00','FAILED',?)",
                         (json.dumps({"company": "Acme", "directory": str(finder)}),))
        database.commit()
        database.close()
        page = self.get("/failures")
        runs = page.text[page.text.index('id="runs-title"'):]
        self.assertNotIn("Browser session", runs)
        self.assertNotIn("Contact finder run run-abc123", runs)
        self.assertIn("Application recovery decision decision-1", runs)   # other run entries stay
        self.assertIn("Research run 7feefb1b", runs)
        job = self.get("/failures", {"run": "job:acme:1"})
        self.assertIn("request-7vesc5ha/stderr.log", run_part(job.text))
        self.assertIn("browser said hello", run_part(job.text))
        found = self.get("/failures", {"run": "finder:f1"})
        self.assertIn("run-abc123/stderr.log", run_part(found.text))
        self.assertIn("finder said hello", run_part(found.text))
        folder = self.get("/failures", {"run": "dir:application-browser/request-7vesc5ha"})   # the dir: route stays
        self.assertIn("browser said hello", run_part(folder.text))


if __name__ == "__main__":
    unittest.main()
