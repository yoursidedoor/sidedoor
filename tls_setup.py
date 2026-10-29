"""HTTPS trust for every Sidedoor process, set once at start (hotfix 2026-10-28).

The built Mac app carries the build machine's OpenSSL, whose default certificate locations are
developer folders (Homebrew's /opt/homebrew/etc/openssl@3 on the Apple-silicon build, python.org's
/Library/Frameworks/Python.framework/.../etc/openssl on the Intel build). A Mac without them had no
trusted roots at all, so every HTTPS call (OpenRouter, the relay, the license server, the update
check) failed with CERTIFICATE_VERIFY_FAILED.

`configure()` runs first in every entry point (tools/sidedoor.py, the frozen binary every child
process is; orchestrator.cli for an installed `sidedoor`):

  1. SSL_CERT_FILE naming a file, or SSL_CERT_DIR naming a folder that holds certificates, set
     before Sidedoor started: the user's own choice, honoured as it is (nothing below is done).
  2. Otherwise truststore.inject_into_ssl(): every ssl context verifies against the operating
     system's trust store (the macOS keychain, the Windows certificate store), so a corporate
     TLS-inspection root the user's computer trusts is trusted too.
  3. Only when that fails, and OpenSSL's default file is missing: SSL_CERT_FILE is set to the
     bundled certifi cacert.pem (replacing an SSL_CERT_FILE that names no file: not a real user
     choice, step 1's test). Children inherit it. It is not set while truststore works, so the
     other programs Sidedoor starts (Codex, Node) keep their own trust store.

SKIP_TRUSTSTORE (SIDEDOOR_TLS_SKIP_TRUSTSTORE=1) is for the release check only
(tools/check_release_tls.py): it skips step 2 and treats OpenSSL's default file as missing, so the
built app takes step 3's real certifi.where() path as on a Mac where truststore can't load. Nothing
else sets it; a user never needs it.

Every HTTPS client in the product goes through the ssl module's default context (urllib), so this
one call covers them all. `state()` says what was done, for doctor and `sidedoor net-check`.
"""

import os
import ssl


TRUSTSTORE = "truststore"     # the OS trust store
USER = "user"                 # the user's own SSL_CERT_FILE / SSL_CERT_DIR
BUNDLED = "bundled"           # Sidedoor's certifi file
DEFAULT = "default"           # OpenSSL's own default locations (they exist here)
NONE = "none"                 # nothing usable: HTTPS fails

SKIP_TRUSTSTORE = "SIDEDOOR_TLS_SKIP_TRUSTSTORE"   # release check only (module docstring)

_STATE = {"mode": None, "ca_file": None, "truststore_error": None}


def bundled_ca_file():
    """The bundled certifi cacert.pem, or None when it is missing."""
    try:
        import certifi
        path = certifi.where()
    except Exception:
        return None
    return path if path and os.path.isfile(path) else None


def _has_certificates(folder):
    try:
        with os.scandir(folder) as entries:
            return any(entry.is_file() for entry in entries)
    except OSError:
        return False


def user_choice(environ):
    """True when the user set SSL_CERT_FILE to a file or SSL_CERT_DIR to a folder holding certificates."""
    cert_file, cert_dir = environ.get("SSL_CERT_FILE"), environ.get("SSL_CERT_DIR")
    return bool(cert_file and os.path.isfile(cert_file)) or bool(cert_dir and _has_certificates(cert_dir))


def _inject_truststore():
    import truststore
    truststore.inject_into_ssl()


def configure(environ=None, inject=None, default_cafile=None, ca_file=None):
    """Set this process's HTTPS trust (see the module docstring); returns `state()`. Never raises.
    `environ`, `inject`, `default_cafile` and `ca_file` replace os.environ, truststore, OpenSSL's
    default file and the bundled file in tests."""
    environ = os.environ if environ is None else environ
    if _STATE["mode"] is not None and environ is os.environ and inject is None:
        return state()   # already done in this process
    try:
        if user_choice(environ):
            _STATE.update(mode=USER, ca_file=environ.get("SSL_CERT_FILE") or None)
            return state()
        skip = environ.get(SKIP_TRUSTSTORE) == "1"
        try:
            if skip:
                raise RuntimeError("truststore skipped (%s, release check)" % SKIP_TRUSTSTORE)
            (inject or _inject_truststore)()
            _STATE.update(mode=TRUSTSTORE, truststore_error=None)
            return state()
        except Exception as error:
            _STATE["truststore_error"] = type(error).__name__
        default = default_cafile if default_cafile is not None else ssl.get_default_verify_paths().openssl_cafile
        if skip:
            default = None
        # user_choice() was False above, so an SSL_CERT_FILE still set names no file: replaced.
        if not (default and os.path.isfile(default)):
            bundled = ca_file if ca_file is not None else bundled_ca_file()
            if bundled:
                environ["SSL_CERT_FILE"] = bundled
                _STATE.update(mode=BUNDLED, ca_file=bundled)
                return state()
            _STATE.update(mode=NONE)
            return state()
        if environ.get("SSL_CERT_FILE"):
            del environ["SSL_CERT_FILE"]   # names no file; left set, it would hide OpenSSL's default file
        _STATE.update(mode=DEFAULT)
    except Exception as error:   # never stop a start over this; doctor reports the mode
        _STATE.update(mode=NONE, truststore_error=_STATE["truststore_error"] or type(error).__name__)
    return state()


def state():
    """{"mode", "ca_file", "truststore_error"}: what `configure` did (mode None: not run in this process)."""
    return dict(_STATE)


def reset():
    """Forget `configure`'s outcome (tests)."""
    _STATE.update(mode=None, ca_file=None, truststore_error=None)


def is_certificate_error(error):
    """True when `error` (or what it wraps: URLError.reason, __cause__, __context__) is a failed
    certificate verification: ssl.SSLCertVerificationError (ssl.CertificateError), or an ssl.SSLError
    whose OpenSSL reason is CERTIFICATE_VERIFY_FAILED. Other SSL errors (SSLEOFError,
    SSLZeroReturnError: a connection cut mid-handshake) are network errors."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return True
        if isinstance(error, ssl.SSLError) and getattr(error, "reason", None) == "CERTIFICATE_VERIFY_FAILED":
            return True
        reason = getattr(error, "reason", None)
        if isinstance(reason, BaseException):
            error = reason
            continue
        error = error.__cause__ or error.__context__
    return False
