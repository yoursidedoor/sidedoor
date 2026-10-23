import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import user_profile  # noqa: E402


os.umask(0o077)
folder = user_profile.home()
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
if folder.stat().st_mode & 0o077:
    raise RuntimeError("Private credential directory permissions required")
path = folder / "bridge-transport-token"
if not path.exists():
    with path.open("x") as output:
        output.write(secrets.token_urlsafe(48) + "\n")
        output.flush()
        os.fsync(output.fileno())
if path.is_symlink() or path.stat().st_mode & 0o077:
    raise RuntimeError("Private credential file permissions required")
# Machine contract (E2.T6): the JSON result; stays print.
print(json.dumps({"credential_file": str(path), "sha256_verifier": hashlib.sha256(path.read_text().strip().encode()).hexdigest(), "secret_printed": False}))
