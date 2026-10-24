"""POSIX ownership guardian; EOF cleans descendants if the worker crashes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys


class Child:
    def __init__(self, process, receipt, token):
        self.process = process
        self.pid = process.pid
        self.receipt = receipt
        self.token = token

    def poll(self):
        code = self.process.poll()
        if code is None:
            return None
        if self.receipt.is_file():
            record = json.loads(self.receipt.read_text(encoding="utf-8"))
            if record.get("token") == self.token:
                return record["returncode"]
        return code

    def terminate(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(self.pid, signal.SIGKILL)
            self.process.wait(timeout=4)
        return self.process.returncode

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()


def start(request, token, stdout, stderr):
    receipt = Path(request.cwd) / (".process-" + token + ".json")
    command = [sys.executable, str(Path(__file__).with_name("analysis_process_guardian.py")), str(receipt), token, *request.argv]
    process = subprocess.Popen(command, cwd=request.cwd, env=dict(request.env), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
    return Child(process, receipt, token)
