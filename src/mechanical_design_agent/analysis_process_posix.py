"""POSIX ownership guardian; EOF cleans descendants if the worker crashes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import shutil


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

    def memory_bytes(self):
        if self.process.poll() is not None:return 0
        rows=_ps(['-axo','pgid=,rss='])
        total=sum(int(row[1])*1024 for row in (line.split() for line in rows.splitlines()) if len(row)==2 and int(row[0])==self.pid)
        return total if self.process.poll() is None else 0


def _ps(arguments):
    executable=shutil.which('ps',path=os.defpath)
    if executable is None:raise RuntimeError('owned RSS observer requires the platform ps utility')
    result=subprocess.run([executable,*arguments],capture_output=True,text=True,timeout=1,env={'PATH':os.defpath,'LC_ALL':'C'})
    if result.returncode:raise RuntimeError('owned RSS query failed')
    return result.stdout


def current_memory_bytes():
    return int(_ps(['-p',str(os.getpid()),'-o','rss=']).strip())*1024


def start(request, token, stdout, stderr):
    receipt = Path(request.cwd) / (".process-" + token + ".json")
    command = [sys.executable, str(Path(__file__).with_name("analysis_process_guardian.py")), str(receipt), token, *request.argv]
    process = subprocess.Popen(command, cwd=request.cwd, env=dict(request.env), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
    return Child(process, receipt, token)
