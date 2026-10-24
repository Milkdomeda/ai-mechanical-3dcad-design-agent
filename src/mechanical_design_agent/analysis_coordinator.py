"""Durable local FEA submission and one authenticated workspace supervisor."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import uuid

from . import secure_fs
from .analysis_protocol import canonical_record, parse_record
from .analysis_queue import pending, scheduler_guard
from .analysis_revision import read_artifact, read_revision
from .analysis_runs import TERMINAL, change_run_unlocked, fingerprint, initial_run, read_run, read_run_unlocked, run_root, synchronize_analysis
from .analysis_store import _read, _timestamp
from .analysis_types import AnalysisError
from .analysis_worker_control import contact_worker
from .workspace_bootstrap import read_workspace_manifest


def _load(folder, reference, schema=None):
    text = read_artifact(folder, reference).content.decode('utf-8')
    return parse_record(text, schema_name=schema) if schema else json.loads(text)


def _launch(workspace, claim_id):
    from .analysis_worker import launch_supervisor
    return launch_supervisor(workspace, claim_id)


class AnalysisCoordinator:
    def __init__(self, store, process_controller, *, supervisor_launcher=None, identity_probe=contact_worker):
        self.store, self.process_controller = store, process_controller
        # The MCP instance owns no solver Job. The detached supervisor creates
        # its own coordinator/controller; only that living owner runs children.
        self._launcher = supervisor_launcher or _launch
        self._probe = identity_probe

    def _authorize(self, request):
        manifest = read_workspace_manifest(self.store.workspace)
        identity = manifest.raw['identity']
        if any(request['scope'][key] != identity[key] for key in ('organization_id', 'design_group_id')):
            raise AnalysisError('ANALYSIS_AUTHORITY_CHANGED', 'run belongs to a different authorized workspace knowledge scope')

    def _lookup(self, scheduler, root, design_id, analysis_id, key_sha, spec_sha, retry_run_id):
        index_folder = root / 'idempotency'
        path = index_folder / (key_sha + '.json')
        candidates = []
        if path.exists():
            _, contents = _read(path)
            index = parse_record(contents.content.decode('utf-8'), schema_name='idempotency')
            candidates = [index['run_id']]
        else:
            index = None
            candidates = [p.name for p in (root / 'runs').glob('run-' + key_sha[:16] + '-*') if p.is_dir()]
        matches = []
        for run_id in candidates:
            state, request = read_run(self.store, design_id, analysis_id, run_id)
            if request['idempotency_sha256'] != key_sha: continue
            self._authorize(request)
            if request['spec_sha256'] != spec_sha or request['retry_run_id'] != retry_run_id:
                raise AnalysisError('ANALYSIS_IDEMPOTENCY_CONFLICT', 'idempotency key is already bound to another immutable execution request')
            if index is not None and (index['idempotency_sha256'] != key_sha or index['fingerprint_sha256'] != request['fingerprint_sha256'] or index['request_sha256'] != state['request_ref']['sha256']):
                raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'idempotency index disagrees with its immutable request')
            matches.append((state, request))
        if len(matches) > 1:
            raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'multiple published requests claim the same idempotency key')
        if index is not None and not matches:
            raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'idempotency index has no matching immutable request')
        if matches:
            state, request = matches[0]
            entry = next((entry for entry in scheduler.state['entries'] if entry['run_id'] == state['run_id'] and entry['design_id'] == design_id and entry['analysis_id'] == analysis_id), None)
            if entry is not None and entry['request_sha256'] != state['request_ref']['sha256']:
                raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'queued request checksum changed')
            if state['status'] not in TERMINAL:
                if entry is None:
                    diagnostic = {'code': 'ANALYSIS_SUBMISSION_INTERRUPTED', 'message': 'Request publication was interrupted before queue commit; use an explicit retry.'}
                    run_folder = run_root(self.store, design_id, analysis_id, state['run_id'])
                    with secure_fs.exclusive_file_lock(run_folder / '.run.lock'):
                        state, request = read_run_unlocked(run_folder, design_id, analysis_id, state['run_id'])
                        if state['status'] not in TERMINAL:
                            state = change_run_unlocked(run_folder, state, {'status': 'interrupted', 'diagnostic': diagnostic}, code=diagnostic['code'], message=diagnostic['message'])
                current = self.store._state(root, design_id, analysis_id)
                if current['active_run_id'] in (None, state['run_id']):
                    synchronize_analysis(self.store, design_id, analysis_id, state)
            matches[0] = (state, request)
        if matches and index is None:
            state, request = matches[0]
            secure_fs.ensure_managed_directory(index_folder, parents=False, exist_ok=True)
            value = {'schema_version': 'AnalysisIdempotency/v1', 'idempotency_sha256': key_sha, 'fingerprint_sha256': request['fingerprint_sha256'], 'run_id': state['run_id'], 'request_sha256': state['request_ref']['sha256']}
            secure_fs.atomic_publish_new(path, canonical_record(value, schema_name='idempotency'))
        return matches[0] if matches else None

    def submit(self, *, design_id, analysis_id, spec_sha256, idempotency_key, expected_revision, retry_run_id=None):
        if type(idempotency_key) is not str or not idempotency_key.strip() or len(idempotency_key) > 256 or type(expected_revision) is not int or expected_revision < 0 or type(spec_sha256) is not str or re.fullmatch('[0-9a-f]{64}', spec_sha256) is None:
            raise AnalysisError('ANALYSIS_SUBMISSION_INVALID', 'submission requires a bounded key, exact specification checksum and state revision')
        key_sha = hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()
        root = self.store._root(design_id, analysis_id)
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            with secure_fs.exclusive_file_lock(root / '.analysis.lock'):
                existing = self._lookup(scheduler, root, design_id, analysis_id, key_sha, spec_sha256, retry_run_id)
        if existing:
            self._ensure_worker()
            return {**self._status(design_id, analysis_id, existing[0]['run_id'], 0), 'idempotent': True}
        state = self.store.read_state(design_id, analysis_id)
        if state['state_revision'] != expected_revision:
            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'analysis changed before submission')
        folder, revision = read_revision(self.store, design_id, analysis_id, state)
        if revision['spec_status'] != 'approved' or state['approval'] is None or revision['artifacts']['spec']['sha256'] != spec_sha256:
            raise AnalysisError('ANALYSIS_SPEC_NOT_APPROVED', 'only the exact frozen approved specification can run')
        spec = _load(folder, revision['artifacts']['spec'], 'spec')
        approval = _load(folder, revision['artifacts']['approval'], 'approval')
        knowledge = _load(folder, revision['artifacts']['knowledge_receipt'], 'knowledge-receipt')
        self._authorize({'scope': knowledge['scope']})
        if self.store.currentness_for_source(design_id, analysis_id, revision['source_binding_ref'])['currentness'] != 'current':
            raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'new submission requires the currently reviewed source revision')
        prefix = Path(state['current_revision']['relative_path']).parent.as_posix() + '/'
        approval_ref = {'relative_path': prefix + revision['artifacts']['approval']['relative_path'], 'sha256': revision['artifacts']['approval']['sha256']}
        if approval['spec_sha256'] != spec_sha256 or approval_ref != state['approval']:
            raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'approval pointer does not match the frozen review')
        request = {'schema_version': 'AnalysisRunRequest/v1', 'design_id': design_id, 'analysis_id': analysis_id, 'run_id': 'run-' + key_sha[:16] + '-' + uuid.uuid4().hex, 'spec_sha256': spec_sha256, 'revision_ref': state['current_revision'], 'spec_ref': {'relative_path': prefix + revision['artifacts']['spec']['relative_path'], 'sha256': spec_sha256}, 'approval_ref': approval_ref, 'source_binding_ref': revision['source_binding_ref'], 'tool_bindings_sha256': approval['tool_bindings_sha256'], 'policy_sha256': approval['policy_sha256'], 'runtime_manifest_sha256': spec['tool_constraints']['runtime_manifest_sha256'], 'scope': knowledge['scope'], 'idempotency_sha256': key_sha, 'fingerprint_sha256': '0' * 64, 'retry_run_id': retry_run_id, 'created_at': _timestamp()}
        request['fingerprint_sha256'] = fingerprint(request)
        if retry_run_id is not None:
            parent, old = read_run(self.store, design_id, analysis_id, retry_run_id)
            if parent['status'] not in {'failed', 'cancelled', 'timed_out', 'interrupted'} or fingerprint({**request, 'retry_run_id': old['retry_run_id']}) != old['fingerprint_sha256']:
                raise AnalysisError('ANALYSIS_RETRY_MISMATCH', 'technical retry requires a terminal unsuccessful run with identical frozen inputs and tool policy')
        encoded = canonical_record(request, schema_name='run-request')
        request_sha = hashlib.sha256(encoded).hexdigest()
        stage = None
        try:
            with scheduler_guard(self.store.workspace, create=True) as scheduler:
                with secure_fs.exclusive_file_lock(root / '.analysis.lock'):
                    existing = self._lookup(scheduler, root, design_id, analysis_id, key_sha, spec_sha256, retry_run_id)
                    if existing:
                        selected_id = existing[0]['run_id']
                    else:
                        current = self.store._state(root, design_id, analysis_id)
                        if current['state_revision'] != expected_revision:
                            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'analysis changed while immutable submission inputs were checked')
                        if pending(scheduler, design_id, analysis_id) or current['active_run_id'] is not None:
                            raise AnalysisError('ANALYSIS_BUSY', 'this analysis already has a queued or active run')
                        stage = secure_fs.ensure_managed_directory(root / 'runs' / ('.creating-' + uuid.uuid4().hex), parents=False, exist_ok=False).path
                        secure_fs.atomic_publish_new(stage / 'request.json', encoded)
                        run = initial_run(request, request_sha)
                        secure_fs.atomic_publish_new(stage / 'run.json', canonical_record(run, schema_name='run'))
                        final = root / 'runs' / request['run_id']
                        secure_fs.atomic_publish_directory(stage, final)
                        stage = None
                        entry = {'design_id': design_id, 'analysis_id': analysis_id, 'run_id': request['run_id'], 'request_sha256': request_sha, 'status': 'queued', 'worker_uuid': None}
                        queue = {**scheduler.state, 'entries': [*scheduler.state['entries'], entry]}
                        if retry_run_id is not None and parent['status'] == 'interrupted':
                            queue.update(recovery_required=False, recovery_diagnostic=None)
                        scheduler.replace(queue)
                        synchronize_analysis(self.store, design_id, analysis_id, run)
                        index_folder = secure_fs.ensure_managed_directory(root / 'idempotency', parents=False, exist_ok=True).path
                        index = {'schema_version': 'AnalysisIdempotency/v1', 'idempotency_sha256': key_sha, 'fingerprint_sha256': request['fingerprint_sha256'], 'run_id': request['run_id'], 'request_sha256': request_sha}
                        secure_fs.atomic_publish_new(index_folder / (key_sha + '.json'), canonical_record(index, schema_name='idempotency'))
                        selected_id = request['run_id']
            self._ensure_worker()
            return {**self._status(design_id, analysis_id, selected_id, 0), 'idempotent': existing is not None}
        finally:
            if stage is not None and stage.exists():
                secure_fs.remove_owned_tree(stage, expected_parent=stage.parent, label='unpublished analysis submission')

    def _ensure_worker(self):
        launch = None
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            if not scheduler.state['entries']:
                return {'worker_state': 'idle'}
            if scheduler.state['recovery_required']:
                return {'worker_state': 'needs_attention', 'diagnostic': scheduler.state['recovery_diagnostic']}
            claim = scheduler.claim()
            if claim is not None:
                if claim['state'] == 'starting' and (datetime.now(UTC) - datetime.fromisoformat(claim['requested_at'])).total_seconds() < 30:
                    return {'worker_state': 'starting', 'worker_uuid': claim['worker_uuid']}
                if claim['state'] == 'ready':
                    verified = self._probe(claim)
                    if verified is not None:
                        return {'worker_state': 'ready', 'worker': {key: verified[key] for key in ('worker_uuid', 'pid', 'created_ns', 'stage', 'active_run_id', 'heartbeat_ns')}}
            with secure_fs.try_exclusive_file_lock(scheduler.root / 'worker.lock') as available:
                if not available:
                    return {'worker_state': 'needs_attention', 'diagnostic': {'code': 'ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'message': 'An owner holds the workspace lock but did not authenticate; no PID attach or replacement worker is allowed.'}}
                interrupted = []
                finished = []
                for entry in scheduler.state['entries']:
                    if entry['status'] != 'claimed': continue
                    analysis = self.store._root(entry['design_id'], entry['analysis_id'])
                    root = run_root(self.store, entry['design_id'], entry['analysis_id'], entry['run_id'])
                    with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                        with secure_fs.exclusive_file_lock(root / '.run.lock'):
                            run, request = read_run_unlocked(root, entry['design_id'], entry['analysis_id'], entry['run_id'])
                            if run['status'] not in TERMINAL:
                                diagnostic = {'code': 'ANALYSIS_WORKER_LOST', 'message': 'Authenticated worker ownership was lost; preserve evidence and request an explicit retry.'}
                                run = change_run_unlocked(root, run, {'status': 'interrupted', 'numerical_status': 'inconclusive', 'diagnostic': diagnostic}, code=diagnostic['code'], message=diagnostic['message'])
                                interrupted.append(entry['run_id'])
                            synchronize_analysis(self.store, entry['design_id'], entry['analysis_id'], run)
                    finished.append(entry['run_id'])
                if finished:
                    scheduler.replace({**scheduler.state, 'entries': [entry for entry in scheduler.state['entries'] if entry['run_id'] not in finished]})
                if interrupted:
                    diagnostic = {'code': 'ANALYSIS_WORKER_LOST', 'message': 'Interrupted claimed runs require explicit retry; no unproved process was attached or killed.'}
                    scheduler.replace({**scheduler.state, 'entries': [entry for entry in scheduler.state['entries'] if entry['run_id'] not in interrupted], 'recovery_required': True, 'recovery_diagnostic': diagnostic})
                    return {'worker_state': 'needs_attention', 'diagnostic': diagnostic}
                if not scheduler.state['entries']:
                    return {'worker_state': 'idle'}
            identifier = uuid.uuid4().hex
            now = _timestamp()
            next_claim = {'schema_version': 'AnalysisWorkerClaim/v1', 'workspace_id': scheduler.state['workspace_id'], 'worker_uuid': identifier, 'secret': secrets.token_hex(32), 'state': 'starting', 'pid': None, 'created_ns': None, 'port': None, 'requested_at': now, 'updated_at': now, 'diagnostic': None}
            scheduler.write_claim(next_claim, new=True)
            scheduler.replace({**scheduler.state, 'claim_id': identifier})
            launch = identifier
        # The lifetime owner lock is released before the child attempts it.
        # No living MCP-owned Job/guardian controls this detached supervisor.
        try:
            self._launcher(self.store.workspace, launch)
        except Exception as exc:
            with scheduler_guard(self.store.workspace, create=True) as scheduler:
                claim = scheduler.claim()
                if claim is not None and claim['worker_uuid'] == launch:
                    if claim['state'] == 'ready' and self._probe(claim) is not None:
                        return {'worker_state': 'ready', 'worker_uuid': launch}
                    with secure_fs.try_exclusive_file_lock(scheduler.root / 'worker.lock') as available:
                        if not available:
                            return {'worker_state': 'needs_attention', 'diagnostic': {'code': 'ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'message': 'Startup outcome is uncertain and an owner holds the workspace lock.'}}
                        diagnostic = {'code': 'ANALYSIS_WORKER_START_FAILED', 'message': 'Detached supervisor startup failed (' + type(exc).__name__ + '); immutable inputs and logs are retained.'}
                        claim.update(state='failed', updated_at=_timestamp(), diagnostic=diagnostic)
                        scheduler.write_claim(claim)
                        for entry in scheduler.state['entries']:
                            analysis = self.store._root(entry['design_id'], entry['analysis_id'])
                            root = run_root(self.store, entry['design_id'], entry['analysis_id'], entry['run_id'])
                            with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                                with secure_fs.exclusive_file_lock(root / '.run.lock'):
                                    run, request = read_run_unlocked(root, entry['design_id'], entry['analysis_id'], entry['run_id'])
                                    if run['status'] not in TERMINAL:
                                        run = change_run_unlocked(root, run, {'status': 'failed', 'diagnostic': diagnostic}, code=diagnostic['code'], message=diagnostic['message'])
                                    synchronize_analysis(self.store, entry['design_id'], entry['analysis_id'], run)
                        scheduler.replace({**scheduler.state, 'entries': []})
            return {'worker_state': 'needs_attention', 'diagnostic': {'code': 'ANALYSIS_WORKER_START_FAILED', 'message': 'Detached supervisor could not start; queued inputs are retained.'}}
        return {'worker_state': 'starting', 'worker_uuid': launch}

    def reconcile(self):
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            remaining = []
            for entry in scheduler.state['entries']:
                run, request = read_run(self.store, entry['design_id'], entry['analysis_id'], entry['run_id'])
                if run['request_ref']['sha256'] != entry['request_sha256']:
                    raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'queue request binding changed')
                if run['status'] in TERMINAL:
                    analysis = self.store._root(entry['design_id'], entry['analysis_id'])
                    with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                        synchronize_analysis(self.store, entry['design_id'], entry['analysis_id'], run)
                else:
                    remaining.append(entry)
            if remaining != scheduler.state['entries']:
                scheduler.replace({**scheduler.state, 'entries': remaining})
        return self._ensure_worker()

    def _status(self, design_id, analysis_id, run_id, cursor):
        state = self.store.read_state(design_id, analysis_id)
        run_id = run_id or state['active_run_id'] or state['latest_run_id']
        if run_id is None:
            return {'design_id': design_id, 'analysis_id': analysis_id, 'run_id': None, 'run_status': None, 'state_revision': state['state_revision'], 'events': [], 'cursor': cursor}
        run, request = read_run(self.store, design_id, analysis_id, run_id)
        self._authorize(request)
        currentness = self.store.currentness_for_source(design_id, analysis_id, request['source_binding_ref'])
        if currentness['currentness'] == 'current' and (state['current_revision'] is None or state['current_revision'] != request['revision_ref']):
            currentness = {**currentness, 'currentness': 'spec_changed'}
        return {'design_id': design_id, 'analysis_id': analysis_id, 'run_id': run_id, 'run_status': run['status'], 'state_revision': state['state_revision'], 'run_revision': run['state_revision'], 'spec_sha256': request['spec_sha256'], 'cancel_requested': run['cancel_requested'], 'numerical_status': run['numerical_status'], 'criteria_status': run['criteria_status'], 'applicability_status': run['applicability_status'], 'currentness': currentness['currentness'], 'snapshot_integrity': currentness['snapshot_integrity'], 'events': [event for event in run['events'] if event['sequence'] > cursor], 'cursor': len(run['events']), 'diagnostic': run['diagnostic']}

    def status(self, *, design_id, analysis_id, run_id=None, cursor=0):
        if type(cursor) is not int or cursor < 0:
            raise AnalysisError('ANALYSIS_CURSOR_INVALID', 'event cursor must be a nonnegative integer')
        owner = self.reconcile()
        return {**self._status(design_id, analysis_id, run_id, cursor), **owner}

    def cancel(self, *, design_id, analysis_id, run_id, reason, expected_revision):
        if type(reason) is not str or not reason.strip() or len(reason) > 4096:
            raise AnalysisError('ANALYSIS_CANCEL_INVALID', 'a bounded explicit cancellation reason is required')
        analysis = self.store._root(design_id, analysis_id)
        root = run_root(self.store, design_id, analysis_id, run_id)
        claim = None
        with scheduler_guard(self.store.workspace, create=True) as scheduler:
            with secure_fs.exclusive_file_lock(analysis / '.analysis.lock'):
                with secure_fs.exclusive_file_lock(root / '.run.lock'):
                    run, request = read_run_unlocked(root, design_id, analysis_id, run_id)
                    self._authorize(request)
                    if run['status'] not in TERMINAL:
                        state = self.store._state(analysis, design_id, analysis_id)
                        if type(expected_revision) is not int or state['state_revision'] != expected_revision or state['active_run_id'] != run_id:
                            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'analysis active run changed before cancellation')
                        queued = run['status'] == 'queued' and run['worker'] is None
                        changes = {'cancel_requested': True, 'cancel_reason': reason.strip()}
                        if queued: changes['status'] = 'cancelled'
                        run = change_run_unlocked(root, run, changes, code='ANALYSIS_CANCEL_REQUESTED', message=reason.strip())
                        synchronize_analysis(self.store, design_id, analysis_id, run)
                        if queued:
                            scheduler.replace({**scheduler.state, 'entries': [entry for entry in scheduler.state['entries'] if entry['run_id'] != run_id]})
                        else:
                            claim = scheduler.claim()
        if claim is not None:
            self._probe(claim, action='cancel', run_id=run_id)
        return self._status(design_id, analysis_id, run_id, 0)
