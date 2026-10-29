"""Windows release gate (0.4.0, T5): the built sidedoor.exe makes HTTPS calls without the build
machine's certificate settings, through the Windows certificate store and through its own fallback.

    <x64 build venv>\\Scripts\\python.exe -X utf8 tools\\check_release_tls_windows.py <output>\\Sidedoor

tools/build_windows.py runs `check` on every build, after signing and before packing, so no zip or
installer is made from a build that fails it. Standalone it checks any built Sidedoor folder. It runs
the folder's own `sidedoor.exe net-check` (one HTTPS GET of OpenRouter's model list through
text_model's urllib path; orchestrator.net_check) twice, each with a temporary Sidedoor home and no OS
credential store, and fails on anything but these:

  1. SSL_CERT_FILE and SSL_CERT_DIR pointed at an empty folder: HTTPS works and the trust is
     "truststore" (tls_setup: the Windows certificate store).
  2. SIDEDOOR_TLS_SKIP_TRUSTSTORE=1 (release check only, tls_setup.SKIP_TRUSTSTORE) with SSL_CERT_FILE
     unset and SSL_CERT_DIR empty: truststore is skipped, so the app takes its real fallback,
     certifi.where(); HTTPS works, the trust is "bundled" and the file is the cacert.pem inside the
     build folder.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

EXE = "sidedoor.exe"
TIMEOUT = 90
CLEARED = ("SSL_CERT_FILE", "SSL_CERT_DIR", "SIDEDOOR_TLS_SKIP_TRUSTSTORE", "SIDEDOOR_BROWSER_RUNTIME")


def environment(home, empty, skip_truststore):
    """This environment without any certificate setting, a temporary Sidedoor home, no OS credential
    store, and the step's certificate settings."""
    values = {name: value for name, value in os.environ.items() if name.upper() not in CLEARED}
    values.update(SIDEDOOR_HOME=str(home), SIDEDOOR_CREDENTIAL_STORE="file", SSL_CERT_DIR=str(empty))
    if skip_truststore:
        values["SIDEDOOR_TLS_SKIP_TRUSTSTORE"] = "1"
    else:
        values["SSL_CERT_FILE"] = str(empty)
    return values


def net_check(app, skip_truststore=False, run=subprocess.run):
    """The JSON line `sidedoor.exe net-check` prints, with its exit code; SystemExit when there is none."""
    with tempfile.TemporaryDirectory(prefix="sidedoor-tls-empty-") as empty, \
            tempfile.TemporaryDirectory(prefix="sidedoor-tls-home-") as home:
        extra = {}
        if sys.platform == "win32":
            import portable
            extra = portable.no_window()
        result = run([str(Path(app) / EXE), "net-check"], capture_output=True, text=True, encoding="utf-8",
                     errors="replace", env=environment(home, empty, skip_truststore), timeout=TIMEOUT, **extra)
    lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
    try:
        answer = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise SystemExit("Windows release TLS gate: `sidedoor.exe net-check` gave no result (exit %s): %s"
                         % (result.returncode, ((result.stderr or "") + (result.stdout or ""))[-800:]))
    answer["exit"] = result.returncode
    return answer


def bundled_ca(app):
    found = sorted(path for path in Path(app).rglob("cacert.pem") if path.is_file() and "certifi" in path.parts)
    if not found:
        raise SystemExit("Windows release TLS gate: the bundled certifi cacert.pem is missing")
    return found[0]


def check(app, run=subprocess.run):
    """The gate (module docstring). Raises SystemExit on any failure."""
    app = Path(app)
    if not (app / EXE).is_file():
        raise SystemExit("Windows release TLS gate: no %s in %s" % (EXE, app))
    emptied = net_check(app, run=run)
    if not emptied.get("ok") or emptied.get("trust") != "truststore" or emptied["exit"]:
        raise SystemExit("Windows release TLS gate: HTTPS failed with the certificate settings emptied: %s" % emptied)
    print("Windows release TLS gate: net-check with empty certificate settings: OK (HTTP %s, trust %s)"
          % (emptied["status"], emptied["trust"]))
    fallback = net_check(app, skip_truststore=True, run=run)
    ca_file = fallback.get("ca_file")
    inside = bool(ca_file) and os.path.normcase(str(Path(ca_file).resolve())) == \
        os.path.normcase(str(bundled_ca(app).resolve()))
    if not fallback.get("ok") or fallback.get("trust") != "bundled" or not inside or fallback["exit"]:
        raise SystemExit("Windows release TLS gate: HTTPS failed through the certifi fallback (truststore "
                         "skipped): %s" % fallback)
    print("Windows release TLS gate: net-check through the bundled certifi file only: OK (HTTP %s, trust %s)"
          % (fallback["status"], fallback["trust"]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("usage: check_release_tls_windows.py <build folder holding sidedoor.exe>")
    check(Path(argv[0]).expanduser().resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
