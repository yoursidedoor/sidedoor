import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_applications_e2e import MockATS

root = Path(sys.argv[1])
if not (root / ".mock-only").is_file() or root == Path(__file__).resolve().parent.parent:
    raise RuntimeError("Isolated fixture directory required")
adapter = MockATS(root)
adapter.now = dt.datetime.now(dt.timezone.utc).isoformat()
print(json.dumps(adapter.call(sys.argv[2], json.load(sys.stdin))))
