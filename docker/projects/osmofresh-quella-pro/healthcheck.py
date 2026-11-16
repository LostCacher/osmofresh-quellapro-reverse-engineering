from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from urllib.request import urlopen


mode = sys.argv[1] if len(sys.argv) > 1 else "receiver"
if mode == "web":
    with urlopen("http://127.0.0.1:8001/api/state", timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)
elif mode == "exporter":
    with urlopen("http://127.0.0.1:8000/metrics", timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)
else:
    state = json.loads(Path("data/live_state.json").read_text(encoding="utf-8"))
    if time.time() - float(state.get("written_at", 0)) > 5:
        raise SystemExit(1)
