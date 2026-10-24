"""Private POSIX guardian executable; no product session or CAD imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def main():
    receipt, token, *argv = sys.argv[1:]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        while True:
            code = process.poll()
            if code is not None:
                Path(receipt).write_text(json.dumps({"token": token, "returncode": code}), encoding="utf-8")
                break
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.05)
            if readable and not os.read(sys.stdin.fileno(), 1):
                break
    finally:
        # Even failed receipt I/O must clean descendants before ownership ends.
        # This session leader is alive, so its PGID cannot have been reused.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(0.2)
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
