"""Bounded per-run state and immutable execution requests; no native launch."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from . import secure_fs
from .analysis_protocol import canonical_record, parse_record
from .analysis_store import _read, _timestamp
from .analysis_types import AnalysisError
from .models import require_safe_id

TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'})
FINGERPRINT_FIELDS = ('design_id', 'analysis_id', 'spec_sha256', 'revision_ref', 'spec_ref', 'approval_ref', 'source_binding_ref', 'tool_bindings_sha256', 'policy_sha256', 'runtime_manifest_sha256', 'scope', 'retry_run_id', 'executor_sha256')


def fingerprint(request):
    # Older beta metadata remains readable; executable requests created now
    # always include the new source/dependency fingerprint.
    body = {key: request[key] for key in FINGERPRINT_FIELDS if key!='executor_sha256' or key in request}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def run_root(store, design_id, analysis_id, run_id):
    require_safe_id(run_id, 'run_id')
    return secure_fs.validate_managed_path(store._root(design_id, analysis_id) / 'runs' / run_id, allow_missing_leaf=False).path


def read_run_unlocked(root, design_id, analysis_id, run_id):
    _, data = _read(root / 'run.json')
    state = parse_record(data.content.decode('utf-8'), schema_name='run')
    _, data = _read(root / 'request.json')
    request = parse_record(data.content.decode('utf-8'), schema_name='run-request')
    identity = {'design_id': design_id, 'analysis_id': analysis_id, 'run_id': run_id}
    if any(record[key] != value for record in (state, request) for key, value in identity.items()) or state['request_ref'] != {'relative_path': 'request.json', 'sha256': data.sha256} or state['spec_sha256'] != request['spec_sha256'] or request['fingerprint_sha256'] != fingerprint(request):
        raise AnalysisError('ANALYSIS_RUN_INTEGRITY', 'run identity, request checksum or immutable input fingerprint changed')
    if 'executor_sha256' in request:
        _,code=_read(root/'executor.json')
        if request['executor_identity_ref']!={'relative_path':'executor.json','sha256':code.sha256} or code.sha256!=request['executor_sha256']:
            raise AnalysisError('ANALYSIS_RUN_INTEGRITY','execution source receipt differs from its immutable request')
    return state, request


def read_run(store, design_id, analysis_id, run_id):
    root = run_root(store, design_id, analysis_id, run_id)
    with secure_fs.exclusive_file_lock(root / '.run.lock'):
        return read_run_unlocked(root, design_id, analysis_id, run_id)


def initial_run(request, request_sha):
    now = _timestamp()
    return {'schema_version': 'AnalysisRun/v1', 'design_id': request['design_id'], 'analysis_id': request['analysis_id'], 'run_id': request['run_id'], 'status': 'queued', 'spec_sha256': request['spec_sha256'], 'request_ref': {'relative_path': 'request.json', 'sha256': request_sha}, 'state_revision': 0, 'worker': None, 'cancel_requested': False, 'cancel_reason': None, 'attempt': 0, 'numerical_status': 'not_evaluated', 'criteria_status': 'not_evaluated', 'applicability_status': 'not_evaluated', 'completion_ref': None, 'diagnostic': None, 'events': [{'sequence': 1, 'at': now, 'stage': 'queued', 'code': None, 'message': 'Immutable analysis request queued.', 'attempt': 0}], 'created_at': now, 'updated_at': now}


def change_run_unlocked(root, state, changes, *, code=None, message):
    permitted = {'status', 'worker', 'cancel_requested', 'cancel_reason', 'attempt', 'numerical_status', 'criteria_status', 'applicability_status', 'completion_ref', 'diagnostic'}
    if set(changes) - permitted:
        raise AnalysisError('ANALYSIS_RUN_IMMUTABLE', 'run input identity and history cannot be changed')
    if state['status'] in TERMINAL:
        raise AnalysisError('ANALYSIS_RUN_TERMINAL', 'terminal run history is immutable; create an explicit retry')
    candidate = {**deepcopy(state), **deepcopy(changes), 'state_revision': state['state_revision'] + 1, 'updated_at': _timestamp()}
    if len(candidate['events']) >= 511 and candidate['status'] not in TERMINAL:
        candidate.update(status='failed', diagnostic={'code': 'ANALYSIS_EVENT_BUDGET_EXCEEDED', 'message': 'Bounded phase event budget exhausted.'})
        code, message = 'ANALYSIS_EVENT_BUDGET_EXCEEDED', 'Bounded phase event budget exhausted.'
    if len(candidate['events']) >= 512:
        raise AnalysisError('ANALYSIS_EVENT_BUDGET_EXCEEDED', 'run event history is already complete')
    candidate['events'].append({'sequence': len(candidate['events']) + 1, 'at': candidate['updated_at'], 'stage': candidate['status'], 'code': code, 'message': message, 'attempt': candidate['attempt']})
    encoded = canonical_record(candidate, schema_name='run')
    secure_fs.atomic_replace(root / 'run.json', encoded)
    return parse_record(encoded.decode('utf-8'), schema_name='run')


def change_run(store, design_id, analysis_id, run_id, changes, *, code=None, message):
    root = run_root(store, design_id, analysis_id, run_id)
    with secure_fs.exclusive_file_lock(root / '.run.lock'):
        state, _ = read_run_unlocked(root, design_id, analysis_id, run_id)
        return change_run_unlocked(root, state, changes, code=code, message=message)


def synchronize_analysis(store, design_id, analysis_id, run):
    """Caller holds scheduler then analysis locks; terminal run is authority."""
    root = store._root(design_id, analysis_id)
    state = store._state(root, design_id, analysis_id)
    if state['active_run_id'] not in (None, run['run_id']):
        raise AnalysisError('ANALYSIS_BUSY', 'a different active run owns this analysis')
    terminal = run['status'] in TERMINAL
    status = ('completed' if run['status'] == 'completed' else ('cancelled' if run['status'] == 'cancelled' else 'needs_attention')) if terminal else ('queued' if run['status'] == 'queued' else 'running')
    active = None if terminal else run['run_id']
    if state['active_run_id'] == active and state['status'] == status and state['latest_run_id'] == run['run_id']:
        return state
    candidate = {**state, 'active_run_id': active, 'latest_run_id': run['run_id'], 'status': status, 'state_revision': state['state_revision'] + 1, 'updated_at': _timestamp()}
    secure_fs.atomic_replace(root / 'analysis.json', canonical_record(candidate, schema_name='session'))
    return candidate
