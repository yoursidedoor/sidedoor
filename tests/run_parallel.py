"""Run the unittest suite with its test files in parallel (owner pick S1 a).

Each test file (the modules `unittest discover` would load: test*.py in tests/) runs in its own
`python -m unittest <module>` process, from tests/ with PYTHONPATH=..:. as the serial command does,
and with its own TMPDIR. Files are scheduled longest first from the timings of the last run
(tests/.parallel_timings.json, machine-local and gitignored; a file never timed falls back to its
size). One summary is printed in unittest's shape, with the full output of failing files only.

    cd tests && python run_parallel.py            # min(12, cpu count) workers
    cd tests && python run_parallel.py -j 8 test_a test_b

The serial command (`cd tests && PYTHONPATH=..:. python -m unittest discover -q`) is unchanged.
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIMINGS = TESTS / ".parallel_timings.json"
PATTERN = "test*.py"   # unittest discover's default pattern
SIZE_SECONDS = 1 / 2000   # first run: a file's size in bytes as a proxy for its seconds

RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
RESULT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.M)


def discover():
    return sorted(p.stem for p in TESTS.glob(PATTERN)
                  if p.is_file() and fnmatch.fnmatch(p.name, PATTERN) and p.name != Path(__file__).name)


def load_timings():
    try:
        return {k: float(v) for k, v in json.loads(TIMINGS.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_timings(timings):
    tmp = TIMINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(timings.items())), indent=1) + "\n")
    tmp.replace(TIMINGS)


def estimate(module, timings):
    if module in timings:
        return timings[module]
    return (TESTS / (module + ".py")).stat().st_size * SIZE_SECONDS


def run_module(module, python, timeout):
    """Run one test file in its own process; returns (module, seconds, counts, output)."""
    tmp = tempfile.mkdtemp(prefix="sdp")   # short: unix socket paths under it are capped at 104 bytes
    env = dict(os.environ)
    env.pop("SIDEDOOR_BROWSER_RUNTIME", None)   # never the real Chrome
    env["PYTHONPATH"] = os.pathsep.join(["..", "."])
    env["TMPDIR"] = tmp + os.sep
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    start = time.monotonic()
    try:
        proc = subprocess.run([python, "-m", "unittest", "-q", module], cwd=TESTS, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        output, code = proc.stdout.decode("utf-8", "replace"), proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", "replace") + "\n[timed out after %ss]\n" % timeout
        code = None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    seconds = time.monotonic() - start
    return module, seconds, parse(output, code), output


def parse(output, code):
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
              "expected failures": 0, "unexpected successes": 0}
    ran = RAN.findall(output)
    result = RESULT.findall(output)
    if ran:
        counts["tests"] = int(ran[-1])
    if result:
        for part in filter(None, (s.strip() for s in result[-1][1].split(","))):
            key, _, value = part.rpartition("=")
            if key in counts and value.isdigit():
                counts[key] = int(value)
    ok = code == 0 and bool(ran) and bool(result) and result[-1][0] == "OK"
    if not ok and not counts["failures"] and not counts["errors"]:
        counts["errors"] = 1   # crashed, timed out or printed no result: count it as one error
    counts["ok"] = ok
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modules", nargs="*", help="test modules to run (default: all test*.py)")
    parser.add_argument("-j", "--jobs", type=int, default=min(12, os.cpu_count() or 1))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=900, help="seconds per test file")
    args = parser.parse_args(argv)

    modules = [m[:-3] if m.endswith(".py") else m for m in args.modules] or discover()
    timings = load_timings()
    modules.sort(key=lambda m: estimate(m, timings), reverse=True)

    start = time.monotonic()
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
             "expected failures": 0, "unexpected successes": 0}
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_module, m, args.python, args.timeout) for m in modules]
        for future in as_completed(futures):
            module, seconds, counts, output = future.result()
            timings[module] = round(seconds, 2)
            for key in total:
                total[key] += counts[key]
            if not counts["ok"]:
                failed.append((module, seconds, output))
                print("FAIL %s (%.1fs)" % (module, seconds), file=sys.stderr, flush=True)
    elapsed = time.monotonic() - start
    save_timings(timings)

    for module, seconds, output in sorted(failed):
        print("\n" + "#" * 70 + "\n# %s (%.1fs)\n" % (module, seconds) + "#" * 70, file=sys.stderr)
        print(output.rstrip(), file=sys.stderr)

    print("-" * 70, file=sys.stderr)
    print("Ran %d tests in %.3fs (%d files, %d workers)"
          % (total["tests"], elapsed, len(modules), args.jobs), file=sys.stderr)
    print(file=sys.stderr)
    extras = ["%s=%d" % (k, total[k]) for k in ("skipped", "expected failures", "unexpected successes")
              if total[k]]
    if failed:
        parts = ["%s=%d" % (k, total[k]) for k in ("failures", "errors") if total[k]] + extras
        print("FAILED (%s)" % ", ".join(parts), file=sys.stderr)
        print("Failing files: " + " ".join(sorted(m for m, _, _ in failed)), file=sys.stderr)
        return 1
    print("OK" + (" (%s)" % ", ".join(extras) if extras else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
