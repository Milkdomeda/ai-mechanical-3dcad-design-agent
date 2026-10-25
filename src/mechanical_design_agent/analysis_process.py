"""Owned subprocess lifecycle, independent of FEA session/protocol state."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import importlib
import os
from pathlib import Path
import stat
import threading
import time
from typing import Mapping
import uuid


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 300
    max_log_bytes: int | None = None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    worker_uuid: str
    created_ns: int


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int | None
    reason: str
    identity: ProcessIdentity


def require_process_exit(outcome,*,failure_code,message):
    """Preserve authenticated owned termination through every native adapter."""
    if outcome.reason=='exited' and outcome.returncode==0:return
    from .analysis_types import AnalysisError
    code={'timed_out':'ANALYSIS_TIMED_OUT','cancelled':'ANALYSIS_CANCELLED','interrupted':'ANALYSIS_INTERRUPTED','budget_exceeded':'ANALYSIS_BUDGET_EXCEEDED'}.get(outcome.reason,failure_code)
    raise AnalysisError(code,message)


class ProcessController:
    """Only this living owner may terminate its children; no PID-only attach.

    Later session recovery must establish a new authenticated ownership path,
    not reconstruct this registry from a user-provided PID.
    """

    def __init__(self):
        if os.name not in {"posix", "nt"}:
            raise RuntimeError("unsupported process platform")
        self._backend = importlib.import_module(f".analysis_process_{'windows' if os.name == 'nt' else 'posix'}", __package__)
        self._children = {}
        self._results = {}
        self._lock = threading.RLock()

    def observed_memory_bytes(self):
        """Only this owner's children; observation is not a hard memory quota."""
        with self._lock:
            used=sum(child.memory_bytes() for _,child,_,_ in self._children.values())
            return {'bytes':self._backend.current_memory_bytes()+used,'measurement':'supervisor RSS plus observed owned process-group RSS' if os.name=='posix' else 'supervisor working set plus owned Job peak committed bytes'}

    def terminate_owned(self,*,reason='interrupted'):
        with self._lock:
            identities=[record[0] for record in self._children.values()]
        return [self.terminate(identity,reason=reason) for identity in identities]

    def owns_pid(self,identity,pid):
        """Read-only membership proof for a living registered process tree."""
        if type(pid) is not int or pid<=0:return False
        with self._lock:
            record=self._owned(identity)
            return record is not None and record[1].poll() is None and record[1].owns_pid(pid)

    def start(self, request: ProcessRequest) -> ProcessIdentity:
        if not request.argv or not all(isinstance(value, str) and value and "\0" not in value for value in request.argv):
            raise ValueError("nonempty explicit argv required")
        if not 0 < request.timeout_seconds <= 86400:
            raise ValueError("bounded positive timeout required")
        if request.max_log_bytes is not None and (type(request.max_log_bytes) is not int or not 0 < request.max_log_bytes <= 2**40):
            raise ValueError('bounded positive integer log budget required')
        environment = dict(request.env)
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        request = replace(request, env=environment)
        directory = Path(request.cwd)
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("existing owned working directory required")
        paths = [Path(request.stdout_path), Path(request.stderr_path)]
        if paths[0].absolute() == paths[1].absolute():
            raise ValueError("stdout and stderr must be distinct")
        for path in paths:
            if path.parent.resolve() != directory.resolve() or path.is_symlink():
                raise ValueError("logs must be new files in the owned directory")
        token = uuid.uuid4().hex
        stdout = paths[0].open("xb")
        try:
            stderr = paths[1].open("xb")
        except BaseException:
            stdout.close()
            raise
        try:
            child = self._backend.start(request, token, stdout, stderr)
        finally:
            stdout.close()
            stderr.close()
        identity = ProcessIdentity(child.pid, token, time.time_ns())
        with self._lock:
            self._children[token] = (identity, child, time.monotonic(), request)
        return identity

    def _owned(self, identity):
        record = self._children.get(identity.worker_uuid)
        return record if record is not None and record[0] == identity else None

    def poll(self, identity: ProcessIdentity) -> ProcessOutcome | None:
        with self._lock:
            if identity in self._results:
                return self._results[identity]
            record = self._owned(identity)
            if record is None:
                return ProcessOutcome(None, "identity_mismatch", identity)
            _, child, started, request = record
            if request.max_log_bytes is not None:
                sizes = [Path(path).lstat() for path in (request.stdout_path, request.stderr_path)]
                if any(not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 for value in sizes) or sum(value.st_size for value in sizes) > request.max_log_bytes:
                    # This is a polling budget, not a filesystem quota. Retain
                    # the original bytes and report failure even if the child
                    # exited between writing the excess and this observation.
                    code = child.poll()
                    return self._finish(identity, child, code if code is not None else child.terminate(), 'budget_exceeded')
            code = child.poll()
            if code is not None:
                return self._finish(identity, child, code, "exited")
            if time.monotonic() - started >= request.timeout_seconds:
                return self.terminate(identity, reason="timed_out")
            return None

    def terminate(self, identity: ProcessIdentity, *, reason: str) -> ProcessOutcome:
        if reason not in {"cancelled", "timed_out", "interrupted", "budget_exceeded"}:
            raise ValueError("invalid termination reason")
        with self._lock:
            if identity in self._results:
                return self._results[identity]
            record = self._owned(identity)
            if record is None:
                return ProcessOutcome(None, "identity_mismatch", identity)
            _, child, _, _ = record
            code = child.poll()
            if code is not None:
                return self._finish(identity, child, code, "exited")
            return self._finish(identity, child, child.terminate(), reason)

    def _finish(self, identity, child, code, reason):
        child.close()
        result = ProcessOutcome(code, reason, identity)
        self._results[identity] = result
        self._children.pop(identity.worker_uuid)
        return result
