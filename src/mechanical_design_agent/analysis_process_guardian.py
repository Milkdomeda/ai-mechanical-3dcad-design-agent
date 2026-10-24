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
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    code = None
    while True:
        code = process.poll()
        if code is not None:
            Path(receipt).write_text(json.dumps({"token": token, "returncode": code}), encoding="utf-8")
            break
        readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.05)
        if readable and not os.read(sys.stdin.fileno(), 1):
            break
    # The session leader is still alive, so this PGID cannot be reused.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    time.sleep(0.2)
    os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
