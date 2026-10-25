"""Detached, authenticated workspace supervisor. Native jobs belong here only.

No solver code or executable name is accepted from queue/control requests.
The native executor is a fixed package collaborator, separate from this owner.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import secure_fs
from .analysis_process import ProcessController
from .analysis_queue import scheduler_guard
from .analysis_runs import TERMINAL, change_run_unlocked, read_run_unlocked, run_root, synchronize_analysis
from .analysis_store import AnalysisStore, _timestamp
from .analysis_types import AnalysisError
from .analysis_worker_control import WorkerControlServer, contact_worker
from .models import require_safe_id


def _execute(context):
    # This explicit gate remains until the qualified deterministic executor is
    # installed. Queue/control acceptance alone must never imply a solve passed.
    raise AnalysisError('FEA_EXECUTOR_UNAVAILABLE', 'The qualified native execution adapter is not available in this build.')


class RunContext:
    def __init__(self, owner, entry, request):
        self.owner, self.entry, self.request = owner, entry, request
        self.store, self.controller = owner.store, owner.controller
        self.root = run_root(self.store, entry['design_id'], entry['analysis_id'], entry['run_id'])
        self._cancel_read_at=0.
        self._cancel_cached=False

    def cancelled(self):
        if self.owner._cancel.is_set(): return True
        now=time.monotonic()
        if now-self._cancel_read_at<.1:return self._cancel_cached
        with secure_fs.exclusive_file_lock(self.root / '.run.lock'):
            state, _ = read_run_unlocked(self.root, **self._identity())
        self._cancel_read_at=now;self._cancel_cached=state['cancel_requested']
        return self._cancel_cached

    def _identity(self):
        return {key: self.entry[key] for key in ('design_id', 'analysis_id', 'run_id')}

    def phase(self, stage, message, *, attempt=None):
        if stage not in {'preparing', 'meshing', 'solving', 'validating', 'awaiting_visual_review'}:
            raise AnalysisError('ANALYSIS_PHASE_INVALID', 'only declared nonterminal execution stages may be published')
        changes = {'status': stage}
        if attempt is not None: changes['attempt'] = attempt
        with secure_fs.exclusive_file_lock(self.root / '.run.lock'):
            state, _ = read_run_unlocked(self.root, **self._identity())
            if state['cancel_requested']:
                raise AnalysisError('ANALYSIS_CANCELLED', 'Cancellation was requested before the next phase.')
            state = change_run_unlocked(self.root, state, changes, message=message)
        with self.owner._metadata_lock:
            self.owner._stage = state['status']
        if state['status'] in TERMINAL:
            raise AnalysisError('ANALYSIS_EVENT_BUDGET_EXCEEDED', 'Phase event budget exhausted.')


class AnalysisSupervisor:
    def __init__(self, store, claim_id, *, execute=None):
        if len(claim_id) != 32 or any(c not in '0123456789abcdef' for c in claim_id):
            raise ValueError('fixed worker claim identity required')
        self.store, self.claim_id = store, claim_id
        self.controller = ProcessController()
        self._execute = execute or _execute
        self._cancel = threading.Event()
        self._metadata_lock = threading.RLock()
        self._stage, self._active = 'starting', None
        self._identity = None

    def snapshot(self):
        with self._metadata_lock:
            return {'stage': self._stage, 'active_run_id': self._active}

    def request_cancel(self, run_id):
        with self._metadata_lock:
            if self._active != run_id: return False
            self._cancel.set()
            return True

    def _take(self, scheduler):
        for entry in scheduler.state['entries']:
            if entry['status'] != 'queued':
                raise AnalysisError('ANALYSIS_WORKER_LOST', 'A previous claimed run requires coordinator recovery.')
            analysis = self.store._root(entry['design_id'], entry['analysis_id'])
            root = run_root(self.store, entry['design_id'], entry['analysis_id'], entry['run_id'])
            with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                with secure_fs.exclusive_file_lock(root / '.run.lock'):
                    run, request = read_run_unlocked(root, entry['design_id'], entry['analysis_id'], entry['run_id'])
                    if run['request_ref']['sha256'] != entry['request_sha256']:
                        raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'queue request checksum changed before claim')
                    if run['status'] in TERMINAL:
                        synchronize_analysis(self.store, entry['design_id'], entry['analysis_id'], run)
                        scheduler.replace({**scheduler.state, 'entries': [other for other in scheduler.state['entries'] if other != entry]})
                        return self._take(scheduler)
                    with self._metadata_lock:
                        self._cancel.clear()
                        self._active, self._stage = entry['run_id'], 'preparing'
                    claimed = {**entry, 'status': 'claimed', 'worker_uuid': self.claim_id}
                    # Queue ownership commits before phase publication. A crash
                    # anywhere after this point requires an explicit retry.
                    scheduler.replace({**scheduler.state, 'entries': [claimed if other == entry else other for other in scheduler.state['entries']]})
                    run = change_run_unlocked(root, run, {'status': 'preparing', 'worker': self._identity, 'attempt': 1}, message='Authenticated supervisor claimed immutable inputs.')
                    synchronize_analysis(self.store, entry['design_id'], entry['analysis_id'], run)
            return RunContext(self, claimed, request)
        return None

    def _finish(self, context, outcome):
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            analysis = self.store._root(context.entry['design_id'], context.entry['analysis_id'])
            with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                with secure_fs.exclusive_file_lock(context.root / '.run.lock'):
                    run, _ = read_run_unlocked(context.root, **context._identity())
                    if run['status'] not in TERMINAL:
                        # Cancellation and completion linearize at this lock.
                        if run['cancel_requested'] or self._cancel.is_set():
                            outcome = {'status': 'cancelled', 'diagnostic': {'code': 'ANALYSIS_CANCELLED', 'message': 'Owned execution stopped after cancellation.'}}
                        if outcome.get('status') == 'completed':
                            # Completion verification is owned by the manifest
                            # publisher, added with the numerical executor.
                            outcome = {'status': 'failed', 'diagnostic': {'code': 'ANALYSIS_COMPLETION_UNVERIFIED', 'message': 'No verified atomic completion record was provided.'}}
                        if outcome.get('status') not in TERMINAL:
                            raise AnalysisError('ANALYSIS_OUTCOME_INVALID', 'executor must return a terminal result')
                        run = change_run_unlocked(context.root, run, outcome, code=(outcome.get('diagnostic') or {}).get('code'), message='Owned execution reached a terminal result.')
                    synchronize_analysis(self.store, context.entry['design_id'], context.entry['analysis_id'], run)
            scheduler.replace({**scheduler.state, 'entries': [entry for entry in scheduler.state['entries'] if entry['run_id'] != context.entry['run_id']]})
        with self._metadata_lock:
            self._active, self._stage = None, 'idle'

    def run(self):
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            lock = scheduler.root / 'worker.lock'
        with secure_fs.try_exclusive_file_lock(lock) as owned:
            if not owned: return 2
            with scheduler_guard(self.store.workspace, create=True) as scheduler:
                claim = scheduler.claim()
                if claim is None or claim['worker_uuid'] != self.claim_id or claim['state'] != 'starting' or scheduler.state['recovery_required']:
                    return 2
            with WorkerControlServer(snapshot=self.snapshot, cancel=self.request_cancel, worker_uuid=self.claim_id, secret=claim['secret']) as control:
                self._identity = {key: control.private_claim()[key] for key in ('worker_uuid', 'pid', 'created_ns')}
                with scheduler_guard(self.store.workspace, create=True) as scheduler:
                    current = scheduler.claim()
                    if current is None or current['worker_uuid'] != self.claim_id: return 2
                    scheduler.write_claim({**current, **control.private_claim(), 'state': 'ready', 'updated_at': _timestamp()})
                while True:
                    with scheduler_guard(self.store.workspace, create=True) as scheduler:
                        context = self._take(scheduler)
                        if context is None:
                            current = scheduler.claim()
                            scheduler.write_claim({**current, 'state': 'stopped', 'updated_at': _timestamp()})
                            return 0
                    try:
                        outcome = self._execute(context)
                    except Exception as exc:
                        code = exc.code if isinstance(exc, AnalysisError) else 'ANALYSIS_EXECUTOR_FAILED'
                        # Exception text may contain private paths/credentials;
                        # only bounded reviewed diagnostics enter public state.
                        outcome = {'status': 'failed', 'diagnostic': {'code': code, 'message': 'Execution stopped (' + type(exc).__name__ + '); preserve evidence and inspect the declared diagnostic.'}}
                    self._finish(context, outcome)


def launch_supervisor(workspace, claim_id):
    """Launch fixed installed code outside MCP-owned solver/guardian jobs."""
    if len(claim_id) != 32 or any(c not in '0123456789abcdef' for c in claim_id):
        raise ValueError('fixed worker claim identity required')
    with scheduler_guard(workspace, create=True) as scheduler:
        claim = scheduler.claim()
        if claim is None or claim['worker_uuid'] != claim_id or claim['state'] != 'starting':
            raise AnalysisError('ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'launch claim is no longer current')
        logs = scheduler.root / 'logs'
    environment = dict(os.environ)
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    options = {'start_new_session': True} if os.name == 'posix' else {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB}
    argv = [sys.executable, '-I', '-B', '-m', 'mechanical_design_agent.analysis_worker', '--workspace', str(workspace), '--claim-id', claim_id]
    with (logs / (claim_id + '.stdout.log')).open('xb') as stdout, (logs / (claim_id + '.stderr.log')).open('xb') as stderr:
        process = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True, **options)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with scheduler_guard(workspace, create=True) as scheduler:
            current = scheduler.claim()
            if current is None or current['worker_uuid'] != claim_id:
                raise AnalysisError('ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'launch identity changed')
            if current['state'] == 'stopped' or (current['state'] == 'ready' and contact_worker(current) is not None):
                # Reap only our launcher child when it eventually exits. This
                # thread owns no Job handle that could kill detached work.
                threading.Thread(target=process.wait, name='fea-supervisor-reaper', daemon=True).start()
                return
        if process.poll() is not None:
            raise AnalysisError('ANALYSIS_WORKER_START_FAILED', 'supervisor exited before publishing authenticated readiness')
        time.sleep(.03)
    # Do not PID-kill an uncertain start; the coordinator checks the owner lock.
    threading.Thread(target=process.wait, name='fea-supervisor-reaper', daemon=True).start()
    raise AnalysisError('ANALYSIS_WORKER_START_FAILED', 'supervisor readiness deadline expired')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--claim-id', required=True)
    args = parser.parse_args(argv)
    return AnalysisSupervisor(AnalysisStore(args.workspace), args.claim_id).run()


if __name__ == '__main__':
    raise SystemExit(main())
