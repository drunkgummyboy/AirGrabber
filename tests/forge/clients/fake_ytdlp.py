"""Stands in for yt-dlp.exe in tests. Controlled with environment variables:

FAKE_ARGV   append this call's arguments (one JSON list per line) to this file
FAKE_FAIL   "1": exit with an error instead of "downloading"
FAKE_SLEEP  seconds to wait before "downloading"
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    log = os.environ.get("FAKE_ARGV")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\n")
    if "-U" in args:
        return 0
    if os.environ.get("FAKE_FAIL") == "1":
        return 1
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    template = args[args.index("-o") + 1]
    Path(template.replace("%(ext)s", "mp4")).write_bytes(b"fake trailer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
