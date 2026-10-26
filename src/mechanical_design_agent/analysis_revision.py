"""Immutable review artifacts and short revision-pointer CAS publication."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import hashlib
import re
import uuid

from . import secure_fs
from .analysis_protocol import canonical_record, parse_record
from .analysis_queue import pending, scheduler_guard
from .analysis_store import _read, _timestamp
from .analysis_types import AnalysisError


def read_artifact(root, reference):
    root=secure_fs.validate_managed_path(Path(root),allow_missing_leaf=False).path
    value = reference.get('relative_path') if type(reference) is dict else None
    if type(value) is not str or not value or len(value) > 1024 or any(part in ('', '.', '..') for part in value.split('/')) or any(char in value for char in '\\:\x00') or PurePosixPath(value).is_absolute():
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'artifact reference must be a contained portable relative path')
    located, data = _read(root / value)
    if not located.is_relative_to(root) or data.link_count != 1 or data.sha256 != reference.get('sha256'):
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'analysis artifact identity or checksum changed')
    return data


def inventory(root,*,checkpoint=None):
    root=secure_fs.validate_managed_path(Path(root),allow_missing_leaf=False).path
    result = []
    for path in sorted(root.rglob('*')):
        if checkpoint is not None:checkpoint()
        located = secure_fs.validate_managed_path(path, allow_missing_leaf=False).path
        if located.is_dir(): continue
        relative = located.relative_to(root).as_posix()
        if relative == 'revision.json': continue
        _, data = _read(located)
        if data.link_count != 1:
            raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'review artifacts cannot be multiply linked')
        result.append({'relative_path': relative, 'sha256': data.sha256})
    if checkpoint is not None:checkpoint()
    return result


def new_stage(store, design_id, analysis_id):
    root = store._root(design_id, analysis_id) / 'revisions'
    return secure_fs.ensure_managed_directory(root / ('.staging-' + uuid.uuid4().hex), parents=False, exist_ok=False).path


def read_revision(store, design_id, analysis_id, state=None):
    state = state or store.read_state(design_id, analysis_id)
    reference = state['current_revision']
    if reference is None or re.fullmatch(r'revisions/r[0-9]{6}-[0-9a-f]{32}/revision\.json', reference['relative_path']) is None:
        raise AnalysisError('ANALYSIS_REVIEW_MISSING', 'prepare the current analysis revision before review')
    folder, record = read_frozen_revision(store, design_id, analysis_id, reference)
    if record['spec_revision'] != state['spec_revision']:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'current specification revision differs from its selected artifact')
    if store.read_source_binding(design_id, analysis_id, record['source_binding_ref']) != state['source']:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'current review is bound to a different source capture')
    return folder, record


def read_frozen_revision(store, design_id, analysis_id, reference):
    if type(reference) is not dict or type(reference.get('relative_path')) is not str or re.fullmatch(r'revisions/r[0-9]{6}-[0-9a-f]{32}/revision\.json', reference['relative_path']) is None:
        raise AnalysisError('ANALYSIS_REVIEW_MISSING', 'a frozen revision requires its complete content-addressed reference')
    root = store._root(design_id, analysis_id)
    data = read_artifact(root, reference)
    record = parse_record(data.content.decode('utf-8'), schema_name='revision')
    folder = root / Path(reference['relative_path']).parent
    if record['design_id'] != design_id or record['analysis_id'] != analysis_id or inventory(folder) != record['files']:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'frozen review revision identity or complete inventory changed')
    for artifact in record['artifacts'].values():
        if artifact is not None:
            read_artifact(folder, artifact)
            if artifact not in record['files']:
                raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'review artifact is outside the immutable file inventory')
    captured = store.read_source_binding(design_id, analysis_id, record['source_binding_ref'])
    spec = parse_record(read_artifact(folder, record['artifacts']['spec']).content.decode('utf-8'), schema_name='spec')
    if spec['source']['sha256'] != captured['origin_sha256'] or spec['source']['design_model_sha256'] != captured['design_model_sha256']:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY', 'frozen review source differs from its immutable source capture')
    return folder, record


def publish_revision(store, design_id, analysis_id, stage, record, expected_revision):
    root = store._root(design_id, analysis_id)
    stage = secure_fs.validate_managed_path(stage, allow_missing_leaf=False).path
    if stage.parent != root / 'revisions' or re.fullmatch(r'\.staging-[0-9a-f]{32}', stage.name) is None:
        raise AnalysisError('ANALYSIS_STORAGE_UNSAFE', 'revision publication requires its own controlled staging directory')
    record = {**record, 'files': inventory(stage)}
    if record['design_id'] != design_id or record['analysis_id'] != analysis_id:
        raise AnalysisError('ANALYSIS_IDENTITY_MISMATCH', 'review revision identity differs from its publication destination')
    if record['spec_status'] == 'approved' and (record['issues'] or record['artifacts']['approval'] is None):
        raise AnalysisError('ANALYSIS_REVIEW_NOT_READY', 'approved revision requires complete approval evidence and no unresolved input')
    encoded = canonical_record(record, schema_name='revision')
    secure_fs.atomic_publish_new(stage / 'revision.json', encoded)
    with scheduler_guard(store.workspace) as scheduler, secure_fs.exclusive_file_lock(root / '.analysis.lock'):
        state = store._state(root, design_id, analysis_id)
        if state['state_revision'] != expected_revision:
            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'analysis changed while its review artifacts were being prepared')
        if pending(scheduler, design_id, analysis_id) or state['active_run_id'] is not None or state['status'] in ('queued', 'running'):
            raise AnalysisError('ANALYSIS_BUSY', 'an active analysis run prevents a new review revision')
        if store.read_source_binding(design_id, analysis_id, record['source_binding_ref']) != state['source']:
            raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'source changed before review publication')
        approved = record['spec_status'] == 'approved'
        expected_spec = state['spec_revision'] if approved else state['spec_revision'] + 1
        if record['spec_revision'] != expected_spec:
            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'logical specification revision is inconsistent')
        target = stage.with_name('r' + str(record['spec_revision']).zfill(6) + '-' + uuid.uuid4().hex)
        relative = target.relative_to(root).as_posix()
        reference = {'relative_path': relative + '/revision.json', 'sha256': hashlib.sha256(encoded).hexdigest()}
        approval = record['artifacts']['approval']
        candidate = {**state, 'state_revision': expected_revision + 1, 'updated_at': _timestamp(), 'spec_revision': record['spec_revision'], 'status': record['spec_status'], 'current_revision': reference, 'approval': None if approval is None else {'relative_path': relative + '/' + approval['relative_path'], 'sha256': approval['sha256']}}
        next_state = canonical_record(candidate, schema_name='session')
        # Directory is published before its pointer. A crash between these two
        # atomic operations leaves an unselected immutable revision, not a false
        # cross-file transaction or an overwritten prior approval/delivery.
        secure_fs.atomic_publish_directory(stage, target)
        secure_fs.atomic_replace(root / 'analysis.json', next_state)
    return candidate, record
