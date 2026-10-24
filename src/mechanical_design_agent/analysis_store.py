"""Independent analysis state, immutable FCStd snapshots and revision CAS.

All locks follow design → analysis order. No method updates CAD session data.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import uuid

from . import secure_fs
from .analysis_protocol import canonical_record, parse_record
from .analysis_types import AnalysisError, JsonObject
from .design_session import _validate_state as _validate_design_state
from .fcstd_security import FcstdSecurityError, inspect_fcstd_bytes
from .models import require_safe_id


def _timestamp():
    return datetime.now(UTC).isoformat()


def _identity(value):
    return {"volume": value.volume, "file_index": value.file_index}


def _source_relative(sha):
    return 'source/'+sha[:2]+'/'+sha[2:4]+'/'+sha+'.FCStd'


def _read(path):
    located=secure_fs.validate_managed_path(path,allow_missing_leaf=False)
    content=secure_fs.read_managed_file(located.path)
    if content.identity!=located.identity:
        raise AnalysisError('ANALYSIS_SOURCE_CHANGED','file identity changed during analysis read')
    return located.path,content


def _verify_snapshot(path,sha):
    located,first=_read(path)
    secure_fs.verify_cas_file(located,sha)
    _,after=_read(path)
    if first.sha256!=sha or first.link_count!=1 or after.identity!=first.identity or after.sha256!=sha:
        raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY','analysis source snapshot identity or checksum changed')


class AnalysisStore:
    def __init__(self,workspace: Path):
        self.workspace=secure_fs.validate_managed_path(Path(workspace),allow_missing_leaf=False).path

    def _design(self,design_id):
        require_safe_id(design_id,'design_id')
        return secure_fs.validate_managed_path(self.workspace/'designs'/design_id,allow_missing_leaf=False).path

    def _root(self,design_id,analysis_id,*,allow_missing=False):
        require_safe_id(analysis_id,'analysis_id')
        return secure_fs.validate_managed_path(self._design(design_id)/'analyses'/analysis_id,allow_missing_leaf=allow_missing).path

    @staticmethod
    def _state(root,design_id,analysis_id):
        _,read=_read(root/'analysis.json')
        state=parse_record(read.content.decode('utf-8'),schema_name='session')
        if state['design_id']!=design_id or state['analysis_id']!=analysis_id:
            raise AnalysisError('ANALYSIS_IDENTITY_MISMATCH','analysis state does not match its full design and analysis identity')
        source=state['source'];reference=source['snapshot_ref']
        if not Path(source['origin_path']).is_absolute():
            raise AnalysisError('ANALYSIS_SOURCE_INVALID','stored origin must remain an explicit absolute path')
        if reference!={'relative_path':_source_relative(source['origin_sha256']),'sha256':source['origin_sha256']}:
            raise AnalysisError('ANALYSIS_ARTIFACT_INTEGRITY','source snapshot reference is inconsistent')
        return state

    def prepare_source(self,*,design_id: str,analysis_id: str,source_path: Path,expected_sha256: str) -> JsonObject:
        require_safe_id(analysis_id,'analysis_id')
        if type(expected_sha256) is not str or re.fullmatch('[0-9a-f]{64}',expected_sha256) is None:
            raise AnalysisError('ANALYSIS_SOURCE_INVALID','a lowercase SHA-256 is required')
        requested=Path(source_path).expanduser()
        if not requested.is_absolute() or requested.suffix.lower()!='.fcstd':
            raise AnalysisError('ANALYSIS_SOURCE_INVALID','an explicit absolute FCStd source path is required')
        design=self._design(design_id)
        stage=None
        try:
            with secure_fs.exclusive_file_lock(design/'.design.lock'):
                _,design_read=_read(design/'design.json')
                design_state=_validate_design_state(json.loads(design_read.content.decode('utf-8')))
                if design_state['design_id']!=design_id:
                    raise AnalysisError('ANALYSIS_IDENTITY_MISMATCH','CAD session identity mismatch')
                origin,initial=_read(requested)
                if initial.sha256!=expected_sha256:
                    raise AnalysisError('ANALYSIS_SOURCE_CHANGED','source checksum differs from the requested revision')
                inspect_fcstd_bytes(initial.content)
                _,model=_read(design/'model.FCStd')
                analyses=secure_fs.ensure_managed_directory(design/'analyses',parents=False,exist_ok=True).path
                final=self._root(design_id,analysis_id,allow_missing=True)
                if final.exists():
                    with secure_fs.exclusive_file_lock(final/'.analysis.lock'):
                        state=self._state(final,design_id,analysis_id)
                    source=state['source']
                    if source['origin_path']!=str(origin) or source['origin_sha256']!=expected_sha256 or source['origin_identity']!=_identity(initial.identity) or source['design_model_sha256']!=model.sha256 or source['design_model_identity']!=_identity(model.identity):
                        raise AnalysisError('ANALYSIS_SOURCE_IMMUTABLE','use a new analysis for a different source revision')
                    _verify_snapshot(final/source['snapshot_ref']['relative_path'],expected_sha256)
                    return state
                stage=analyses/('.creating-'+analysis_id+'-'+uuid.uuid4().hex)
                secure_fs.ensure_managed_directory(stage,parents=False,exist_ok=False)
                for name in ('source','revisions','runs'):
                    secure_fs.ensure_managed_directory(stage/name,parents=False,exist_ok=False)
                relative=_source_relative(expected_sha256)
                secure_fs.ingest_cas_file(origin,stage/relative,expected_sha256,allowed_source_root=None)
                _verify_snapshot(stage/relative,expected_sha256)
                _,after=_read(origin);_,model_after=_read(design/'model.FCStd')
                if after.identity!=initial.identity or after.sha256!=initial.sha256 or model_after.identity!=model.identity or model_after.sha256!=model.sha256:
                    raise AnalysisError('ANALYSIS_SOURCE_CHANGED','source or design model changed during snapshot creation')
                now=_timestamp()
                source={
                    'origin_path':str(origin),'origin_sha256':initial.sha256,'origin_identity':_identity(initial.identity),
                    'design_model_sha256':model.sha256,'design_model_identity':_identity(model.identity),
                    'design_state_sha256':design_read.sha256,'cad_model_classification':design_state['model_classification'],
                    'snapshot_ref':{'relative_path':relative,'sha256':expected_sha256},
                }
                state={'schema_version':'AnalysisSession/v1','design_id':design_id,'analysis_id':analysis_id,'state_revision':0,'status':'draft' if initial.sha256==model.sha256 else 'needs_input','source':source,'spec_revision':0,'active_run_id':None,'latest_delivery':None,'accepted_delivery':None,'created_at':now,'updated_at':now}
                secure_fs.atomic_publish_new(stage/'analysis.json',canonical_record(state,schema_name='session'))
                secure_fs.atomic_publish_directory(stage,final)
                stage=None
                return self._state(final,design_id,analysis_id)
        except AnalysisError:
            raise
        except FcstdSecurityError as exc:
            raise AnalysisError(exc.code,'source FCStd failed the existing static security boundary') from exc
        except (OSError,secure_fs.SecureFilesystemError) as exc:
            raise AnalysisError('ANALYSIS_STORAGE_UNSAFE','analysis paths or source files could not be pinned safely') from exc
        finally:
            if stage is not None and stage.exists():
                secure_fs.remove_owned_tree(stage,expected_parent=stage.parent,label='analysis snapshot creation attempt')

    def read_state(self,design_id: str,analysis_id: str) -> JsonObject:
        root=self._root(design_id,analysis_id)
        with secure_fs.exclusive_file_lock(root/'.analysis.lock'):
            return self._state(root,design_id,analysis_id)

    def compare_and_replace(self,*,design_id: str,analysis_id: str,expected_revision: int,next_state: JsonObject) -> JsonObject:
        if type(expected_revision) is not int or expected_revision<0:
            raise AnalysisError('ANALYSIS_REVISION_INVALID','expected_revision must be a nonnegative integer')
        canonical_record(next_state,schema_name='session')
        root=self._root(design_id,analysis_id)
        with secure_fs.exclusive_file_lock(root/'.analysis.lock'):
            current=self._state(root,design_id,analysis_id)
            if current['state_revision']!=expected_revision:
                raise AnalysisError('ANALYSIS_REVISION_CONFLICT','analysis state revision changed')
            if next_state.get('source')!=current['source']:
                raise AnalysisError('ANALYSIS_SOURCE_IMMUTABLE','analysis source binding cannot be changed')
            if any(next_state.get(k)!=current[k] for k in ('design_id','analysis_id','schema_version','created_at')):
                raise AnalysisError('ANALYSIS_IDENTITY_MISMATCH','analysis identity cannot be changed')
            if next_state.get('state_revision')!=expected_revision:
                raise AnalysisError('ANALYSIS_REVISION_INVALID','next state must start from the expected revision')
            candidate={**deepcopy(next_state),'state_revision':expected_revision+1,'updated_at':_timestamp()}
            encoded=canonical_record(candidate,schema_name='session')
            secure_fs.atomic_replace(root/'analysis.json',encoded)
            return parse_record(encoded.decode('utf-8'),schema_name='session')

    def currentness(self,design_id: str,analysis_id: str) -> JsonObject:
        design=self._design(design_id);root=self._root(design_id,analysis_id)
        with secure_fs.exclusive_file_lock(design/'.design.lock'):
            with secure_fs.exclusive_file_lock(root/'.analysis.lock'):
                state=self._state(root,design_id,analysis_id)
                source=state['source'];reference=source['snapshot_ref']
                try:
                    _verify_snapshot(root/reference['relative_path'],reference['sha256'])
                except (OSError,secure_fs.SecureFilesystemError,AnalysisError):
                    return {'currentness':'artifact_integrity_failed','snapshot_integrity':'failed','origin_unchanged':False,'design_model_unchanged':False}
                def matches(path,sha,identity):
                    try:
                        _,value=_read(path)
                        return value.sha256==sha and _identity(value.identity)==identity
                    except (OSError,secure_fs.SecureFilesystemError,AnalysisError):return False
                origin_ok=matches(Path(source['origin_path']),source['origin_sha256'],source['origin_identity'])
                design_ok=matches(design/'model.FCStd',source['design_model_sha256'],source['design_model_identity'])
                current=origin_ok and design_ok and source['origin_sha256']==source['design_model_sha256']
                return {'currentness':'current' if current else 'source_changed','snapshot_integrity':'passed','origin_unchanged':origin_ok,'design_model_unchanged':design_ok}
