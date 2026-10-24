"""Private workspace scheduler metadata, always locked before analysis/run.

The initialization lock also protects the absent-queue case: a source rebind
cannot miss a concurrently created queue and its committed active request.
Long numerical/GUI phases never hold either scheduler lock.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import secure_fs
from .analysis_protocol import canonical_record, parse_record
from .analysis_scratch import _create_private, _require_private
from .analysis_store import _read
from .analysis_types import AnalysisError
from .workspace_bootstrap import read_workspace_manifest


class Scheduler:
    def __init__(self, workspace, root, state):
        self.workspace, self.root, self.state = workspace, root, state

    def replace(self, candidate):
        if candidate['workspace_id'] != self.state['workspace_id'] or candidate['queue_revision'] != self.state['queue_revision']:
            raise AnalysisError('ANALYSIS_QUEUE_CONFLICT', 'scheduler identity or revision changed')
        identities = [(entry['design_id'], entry['analysis_id']) for entry in candidate['entries']]
        if len(identities) != len(set(identities)):
            raise AnalysisError('ANALYSIS_BUSY', 'each analysis may have only one queued or claimed run')
        next_state = {**candidate, 'queue_revision': candidate['queue_revision'] + 1}
        encoded = canonical_record(next_state, schema_name='queue')
        secure_fs.atomic_replace(self.root / 'queue.json', encoded)
        self.state = parse_record(encoded.decode('utf-8'), schema_name='queue')
        return self.state

    def claim(self):
        identifier = self.state['claim_id']
        if identifier is None: return None
        _, data = _read(self.root / 'claims' / (identifier + '.json'))
        record = parse_record(data.content.decode('utf-8'), schema_name='worker-claim')
        if record['worker_uuid'] != identifier or record['workspace_id'] != self.state['workspace_id']:
            raise AnalysisError('ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'scheduler claim identity changed')
        return record

    def write_claim(self, record, *, new=False):
        if record['workspace_id'] != self.state['workspace_id']:
            raise AnalysisError('ANALYSIS_WORKER_IDENTITY_UNPROVEN', 'worker claim belongs to a different workspace')
        encoded = canonical_record(record, schema_name='worker-claim')
        path = self.root / 'claims' / (record['worker_uuid'] + '.json')
        (secure_fs.atomic_publish_new if new else secure_fs.atomic_replace)(path, encoded)


def pending(scheduler, design_id, analysis_id):
    return scheduler is not None and any(entry['design_id'] == design_id and entry['analysis_id'] == analysis_id for entry in scheduler.state['entries'])


@contextmanager
def scheduler_guard(workspace: Path, *, create=False):
    workspace = secure_fs.validate_managed_path(workspace, allow_missing_leaf=False).path
    data = secure_fs.ensure_managed_directory(workspace / 'data', parents=False, exist_ok=True).path
    root = data / 'analysis-scheduler'
    with ExitStack() as lifetime:
        with secure_fs.exclusive_file_lock(data / '.analysis-scheduler-init.lock'):
            checked = secure_fs.validate_managed_path(root, allow_missing_leaf=True)
            if checked.identity is None:
                if not create:
                    yield None
                    return
                _create_private(checked.path)
            root = secure_fs.validate_managed_path(root, allow_missing_leaf=False).path
            _require_private(root)
            for name in ('claims', 'logs'):
                secure_fs.ensure_managed_directory(root / name, parents=False, exist_ok=True)
            lifetime.enter_context(secure_fs.exclusive_file_lock(root / 'queue.lock'))
        path = root / 'queue.json'
        if not path.exists():
            if not create:
                raise AnalysisError('ANALYSIS_QUEUE_INVALID', 'scheduler exists without its initialized queue')
            manifest = read_workspace_manifest(workspace)
            initial = {'schema_version': 'AnalysisQueue/v1', 'queue_revision': 0, 'workspace_id': str(manifest.workspace_id), 'entries': [], 'claim_id': None, 'recovery_required': False, 'recovery_diagnostic': None}
            secure_fs.atomic_publish_new(path, canonical_record(initial, schema_name='queue'))
        _, contents = _read(path)
        state = parse_record(contents.content.decode('utf-8'), schema_name='queue')
        manifest = read_workspace_manifest(workspace)
        if state['workspace_id'] != str(manifest.workspace_id):
            raise AnalysisError('ANALYSIS_QUEUE_INVALID', 'scheduler belongs to a different initialized workspace')
        yield Scheduler(workspace, root, state)
