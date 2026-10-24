"""Two-phase static-analysis review with source and evidence-bound approval.

Approval freezes a copy differing only in lifecycle status. Its receipt binds
both the exact reviewed hash and the resulting approved hash; no hash fields
are dropped and no prior geometry/material approval is inherited.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from . import secure_fs
from .analysis_geometry import resolve_regions, validate_geometry_manifest, validate_geometry_request
from .analysis_knowledge import _json_bytes
from .analysis_loads import build_load_ledger, validate_materials
from .analysis_protocol import canonical_record, parse_record, record_sha256
from .analysis_revision import new_stage, publish_revision, read_artifact, read_revision
from .analysis_types import AnalysisError
from .approval_semantics import APPROVE, classify_approval
from .design_knowledge import _knowledge_ids
from .workspace_bootstrap import read_workspace_manifest


def _hash(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _put(folder, name, value, schema=None):
    data = canonical_record(value, schema_name=schema) if schema else _json_bytes(value)
    secure_fs.atomic_publish_new(folder / name, data)
    return {'relative_path': name, 'sha256': hashlib.sha256(data).hexdigest()}


def _load(folder, reference, schema=None):
    data = read_artifact(folder, reference).content.decode('utf-8')
    return parse_record(data, schema_name=schema) if schema else json.loads(data)


def _policy(spec):
    return _hash({'adapter_version': 'native-solid-static-v1', 'mesh': spec['mesh'], 'validation': spec['validation'], 'budget': spec['budget'], 'distribution': 'native_uniform_surface'})


def _tools(settings, spec):
    return {'configuration_sha256': settings['configuration_sha256'], 'runtime_manifest_sha256': settings['runtime']['manifest_sha256'], 'adapter_version': 'native-solid-static-v1', 'policy_sha256': _policy(spec), 'tools': settings['tools']}


class AnalysisSessionService:
    def __init__(self, store, *, configuration_loader, geometry_inspector, knowledge_loader, gui_previewer):
        self.store = store
        self.configuration_loader = configuration_loader
        self.geometry_inspector = geometry_inspector
        self.knowledge_loader = knowledge_loader
        self.gui_previewer = gui_previewer

    def prepare(self, *, design_id, analysis_id, request, expected_revision):
        request = parse_record(canonical_record(request, schema_name='prepare-request').decode('utf-8'), schema_name='prepare-request')
        spec = deepcopy(request['draft_spec'])
        if spec['design_id'] != design_id or spec['analysis_id'] != analysis_id or spec['status'] not in ('draft', 'needs_input') or request.get('preview_receipt_ref') is not None:
            raise AnalysisError('ANALYSIS_PREPARE_INVALID', 'prepare requires the matching draft identity and its own trusted preview producer')
        geometry_request = validate_geometry_request(request['geometry_request'])
        if geometry_request['source_sha256'] != request['expected_source_sha256']:
            raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'geometry and source snapshot requests disagree')
        selected_root = self.store._root(design_id, analysis_id, allow_missing=True)
        if not selected_root.exists():
            if expected_revision is not None:
                raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'new analysis requires expected_revision=null') from None
            state = self.store.prepare_source(design_id=design_id, analysis_id=analysis_id, source_path=Path(request['source_path']), expected_sha256=request['expected_source_sha256'])
            if state['spec_revision'] != 0:
                raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'another request already prepared this analysis')
        else:
            state = self.store.read_state(design_id, analysis_id)
            if type(expected_revision) is not int or expected_revision != state['state_revision']:
                raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'existing analysis requires its exact current state revision')
            state = self.store.rebind_source(design_id=design_id, analysis_id=analysis_id, source_path=Path(request['source_path']), expected_sha256=request['expected_source_sha256'], expected_revision=expected_revision)
        if state['active_run_id'] is not None or state['status'] in ('queued', 'running'):
            raise AnalysisError('ANALYSIS_BUSY', 'cannot revise an analysis with an active run')
        stage = new_stage(self.store, design_id, analysis_id)
        try:
            return self._prepare(design_id, analysis_id, request, spec, geometry_request, state, stage)
        except BaseException:
            if stage.exists():
                secure_fs.remove_owned_tree(stage, expected_parent=stage.parent, label='unpublished analysis review')
            raise

    def _prepare(self, design_id, analysis_id, request, spec, geometry_request, state, stage):
        artifacts = dict.fromkeys(('spec', 'geometry', 'geometry_brep', 'geometry_fcstd', 'regions', 'load_ledger', 'summary', 'tools', 'knowledge_receipt', 'knowledge_context', 'preview_receipt', 'approval'))
        issues = []
        def issue(code, message):
            issues.append({'code': code, 'message': message})
        spec['revision'] = state['spec_revision'] + 1
        spec['status'] = 'draft'
        source_reference = self.store.source_reference(design_id, analysis_id)
        captured = self.store.read_source_binding(design_id, analysis_id, source_reference)
        spec['source'] = {'sha256': captured['origin_sha256'], 'design_model_sha256': captured['design_model_sha256'], 'object_names': [geometry_request['included_object']], 'excluded_object_names': geometry_request['excluded_objects'], 'geometry_sha256': None, 'regions_sha256': None}
        if self.store.currentness_for_source(design_id, analysis_id, source_reference)['currentness'] != 'current':
            issue('ANALYSIS_SOURCE_CHANGED', 'selected output and current CAD model do not represent the same captured geometry revision')
        settings = None
        geometry = regions = ledger = None
        try:
            settings = self.configuration_loader()
        except AnalysisError as exc:
            issue(exc.code, str(exc))
        if settings is not None:
            try:
                geometry = validate_geometry_manifest(self.geometry_inspector({'source_path': str(self.store.snapshot_path(design_id, analysis_id, source_reference)), 'request': geometry_request, 'settings': settings, 'output_dir': str(stage / 'geometry')}))
                if geometry['source_sha256'] != captured['origin_sha256'] or geometry['included_objects'] != spec['source']['object_names'] or geometry['excluded_objects'] != spec['source']['excluded_object_names'] or geometry['partitions'] != geometry_request['partitions']:
                    raise AnalysisError('GEOMETRY_BINDING_CHANGED', 'native geometry producer returned a different source or selection')
                if abs(Decimal(geometry['volume']['value']) - Decimal(geometry['original_volume']['value'])) > Decimal(geometry_request['volume_tolerance']['value']):
                    raise AnalysisError('GEOMETRY_VOLUME_CHANGED', 'derived partition changed volume beyond the declared numerical tolerance')
                for key, reference in geometry['artifacts'].items():
                    read_artifact(stage / 'geometry', reference)
                    artifacts['geometry_' + key] = {'relative_path': 'geometry/' + reference['relative_path'], 'sha256': reference['sha256']}
                artifacts['geometry'] = _put(stage, 'geometry.json', geometry, 'geometry')
                regions = resolve_regions(geometry, request['region_selectors'])
                artifacts['regions'] = _put(stage, 'regions.json', regions, 'region-bindings')
                spec['source'].update(geometry_sha256=geometry['geometry_sha256'], regions_sha256=regions['bindings_sha256'])
            except AnalysisError as exc:
                issue(exc.code, str(exc))
        try:
            validate_materials(spec)
        except AnalysisError as exc:
            issue(exc.code, str(exc))
        for field in ('mesh', 'validation', 'budget'):
            if spec[field] is None:
                issue('ANALYSIS_' + field.upper() + '_UNRESOLVED', 'declare ' + field + ' before review')
        if settings is not None:
            tools = _tools(settings, spec)
            constraints = {'runtime_manifest_sha256': tools['runtime_manifest_sha256'], 'policy_sha256': tools['policy_sha256'], 'adapter_version': tools['adapter_version']}
            if spec['tool_constraints'] is not None and spec['tool_constraints'] != constraints:
                issue('ANALYSIS_TOOL_CONSTRAINT_CHANGED', 'draft tool or numerical policy pins differ from the current configured revision')
            spec['tool_constraints'] = constraints
            artifacts['tools'] = _put(stage, 'tools.json', tools)
        knowledge = self.knowledge_loader({'design_id': design_id, 'analysis_id': analysis_id, 'source_sha256': captured['origin_sha256'], 'query': request['knowledge']['query'], 'features': {'analysis_type': 'linear_static', 'material': None if spec['material'] is None else spec['material']['name'], 'included_objects': spec['source']['object_names']}, 'required_ids': request['knowledge']['required_ids'], 'requested_family_id': request['knowledge']['requested_family_id']})
        receipt = parse_record(canonical_record(knowledge['receipt'], schema_name='knowledge-receipt').decode('utf-8'), schema_name='knowledge-receipt')
        manifest = read_workspace_manifest(self.store.workspace)
        if receipt['design_id'] != design_id or receipt['analysis_id'] != analysis_id or receipt['source_sha256'] != captured['origin_sha256'] or receipt['actor_id'] != manifest.actor_id:
            raise AnalysisError('ANALYSIS_KNOWLEDGE_BINDING_INVALID', 'analysis knowledge receipt identity changed')
        identity = manifest.raw['identity']
        scope = {'organization_id': identity['organization_id'], 'design_group_id': identity['design_group_id'], 'requested_family_id': request['knowledge']['requested_family_id']}
        required = sorted(set(request['knowledge']['required_ids']))
        available = [] if knowledge['context'] is None else sorted(set(_knowledge_ids(knowledge['context'])))
        missing = [item for item in required if item not in available]
        status = 'unavailable' if knowledge['context'] is None else ('completed_matches' if available else 'completed_no_match')
        if receipt['scope'] != scope or receipt['required_ids'] != required or receipt['available_ids'] != available or receipt['missing_required_ids'] != missing or receipt['blocking'] is not bool(missing) or receipt['status'] != status:
            raise AnalysisError('ANALYSIS_KNOWLEDGE_BINDING_INVALID', 'knowledge scope or required-item policy differs from the reviewed request')
        artifacts['knowledge_receipt'] = _put(stage, 'knowledge.json', receipt, 'knowledge-receipt')
        if knowledge['context'] is not None:
            artifacts['knowledge_context'] = _put(stage, 'knowledge-context.json', knowledge['context'])
            if artifacts['knowledge_context']['sha256'] != receipt['context_sha256']:
                raise AnalysisError('ANALYSIS_KNOWLEDGE_BINDING_INVALID', 'knowledge context checksum disagrees with its receipt')
        elif receipt['context_sha256'] is not None:
            raise AnalysisError('ANALYSIS_KNOWLEDGE_BINDING_INVALID', 'knowledge receipt requires a missing context')
        if receipt['blocking']:
            issue('ANALYSIS_REQUIRED_KNOWLEDGE_MISSING', 'required named knowledge is unavailable: ' + ', '.join(receipt['missing_required_ids']))
        for unresolved in spec['unresolved']:
            issue('ANALYSIS_PHYSICAL_INPUT_UNRESOLVED', unresolved)
        if geometry is not None and regions is not None and spec['material'] is not None and spec['validation'] is not None:
            try:
                ledger = build_load_ledger(spec, {'geometry': geometry, 'regions': regions})
            except AnalysisError as exc:
                issue(exc.code, str(exc))
        if not spec['cases']:
            issue('LOAD_CASE_UNRESOLVED', 'at least one explicit load case is required')
        spec['status'] = 'needs_input' if issues else 'ready_for_review'
        spec_sha = record_sha256(spec, schema_name='spec')
        if ledger is not None:
            ledger['spec_sha256'] = spec_sha
            artifacts['load_ledger'] = _put(stage, 'loads.json', ledger, 'load-ledger')
        summary = {'schema_version': 'AnalysisReviewSummary/v1', 'spec_sha256': spec_sha, 'source': spec['source'], 'material': spec['material'], 'frame_mass': None if ledger is None else ledger['frame_mass'], 'load_cases': [] if ledger is None else ledger['cases'], 'regions': [] if regions is None else regions['bindings'], 'mesh': spec['mesh'], 'validation': spec['validation'], 'budget': spec['budget'], 'conclusion_level': spec['conclusion_level'], 'criteria': spec['criteria'], 'assumptions': spec['assumptions'], 'unresolved': spec['unresolved'], 'knowledge_status': receipt['status'], 'issues': issues}
        artifacts['summary'] = _put(stage, 'summary.json', summary)
        if not issues:
            try:
                preview = self.gui_previewer({'spec': spec, 'spec_sha256': spec_sha, 'geometry': geometry, 'regions': regions, 'settings': settings, 'output_dir': str(stage / 'preview')})
                preview = parse_record(canonical_record(preview, schema_name='preview-receipt').decode('utf-8'), schema_name='preview-receipt')
                expected = {'status': 'completed', 'design_id': design_id, 'analysis_id': analysis_id, 'spec_sha256': spec_sha, 'source_sha256': spec['source']['sha256'], 'geometry_sha256': spec['source']['geometry_sha256'], 'regions_sha256': spec['source']['regions_sha256'], 'runtime_manifest_sha256': spec['tool_constraints']['runtime_manifest_sha256'], 'visible_region_ids': sorted(item['region_id'] for item in regions['bindings'])}
                if any(preview[key] != value for key, value in expected.items()):
                    raise AnalysisError('ANALYSIS_PREVIEW_INVALID', 'GUI preview does not cover the exact reviewed specification and regions')
                for reference in preview['images']: read_artifact(stage / 'preview', reference)
                artifacts['preview_receipt'] = _put(stage, 'preview.json', preview, 'preview-receipt')
            except AnalysisError as exc:
                issue(exc.code, str(exc))
                # The failed review keeps its own diagnostic summary and cannot
                # be approved using the earlier ready hash or preview claim.
                spec['status'] = 'needs_input'
                spec_sha = record_sha256(spec, schema_name='spec')
                summary.update(spec_sha256=spec_sha, issues=issues)
                secure_fs.atomic_replace(stage / 'summary.json', _json_bytes(summary))
                artifacts['summary']['sha256'] = _hash(summary)
                if ledger is not None:
                    ledger['spec_sha256'] = spec_sha
                    encoded = canonical_record(ledger, schema_name='load-ledger')
                    secure_fs.atomic_replace(stage / 'loads.json', encoded)
                    artifacts['load_ledger']['sha256'] = hashlib.sha256(encoded).hexdigest()
        artifacts['spec'] = _put(stage, 'spec.json', spec, 'spec')
        record = {'schema_version': 'AnalysisRevision/v1', 'design_id': design_id, 'analysis_id': analysis_id, 'spec_revision': spec['revision'], 'spec_status': spec['status'], 'source_binding_ref': source_reference, 'artifacts': artifacts, 'files': [], 'issues': issues}
        committed, record = publish_revision(self.store, design_id, analysis_id, stage, record, state['state_revision'])
        return {'spec_status': spec['status'], 'spec_revision': spec['revision'], 'spec_sha256': spec_sha, 'state_revision': committed['state_revision'], 'issues': issues, 'summary': summary, 'current_revision': committed['current_revision']}

    def approve(self, *, design_id, analysis_id, spec_sha256, decision_text, expected_revision):
        state = self.store.read_state(design_id, analysis_id)
        if type(expected_revision) is not int or expected_revision != state['state_revision']:
            raise AnalysisError('ANALYSIS_REVISION_CONFLICT', 'analysis state changed before approval')
        folder, record = read_revision(self.store, design_id, analysis_id, state)
        if record['artifacts']['spec']['sha256'] != spec_sha256:
            raise AnalysisError('ANALYSIS_SPEC_CHANGED', 'approval must quote the exact reviewed specification checksum')
        decision = classify_approval(decision_text)
        if decision != APPROVE:
            return {'approval_state': decision, 'spec_status': record['spec_status'], 'spec_sha256': spec_sha256, 'state_revision': state['state_revision']}
        if record['spec_status'] != 'ready_for_review' or record['issues'] or record['artifacts']['preview_receipt'] is None:
            raise AnalysisError('ANALYSIS_REVIEW_NOT_READY', 'resolve physical inputs and obtain the matching native region preview before approval')
        if self.store.currentness_for_source(design_id, analysis_id, record['source_binding_ref'])['currentness'] != 'current':
            raise AnalysisError('ANALYSIS_SOURCE_CHANGED', 'current CAD source no longer matches the reviewed snapshot')
        reviewed = _load(folder, record['artifacts']['spec'], 'spec')
        tools = _load(folder, record['artifacts']['tools'])
        if tools != _tools(self.configuration_loader(), reviewed):
            raise AnalysisError('ANALYSIS_TOOL_CONSTRAINT_CHANGED', 'native tool identities or numerical policy changed after review')
        frozen = {**reviewed, 'status': 'approved'}
        frozen_sha = record_sha256(frozen, schema_name='spec')
        geometry = _load(folder, record['artifacts']['geometry'], 'geometry')
        regions = _load(folder, record['artifacts']['regions'], 'region-bindings')
        ledger = build_load_ledger(frozen, {'geometry': geometry, 'regions': regions})
        prior_ledger = _load(folder, record['artifacts']['load_ledger'], 'load-ledger')
        if {**ledger, 'spec_sha256': spec_sha256} != prior_ledger:
            raise AnalysisError('ANALYSIS_REVIEW_CHANGED', 'load or material interpretation changed after preview')
        workspace_manifest = read_workspace_manifest(self.store.workspace)
        knowledge_receipt = _load(folder, record['artifacts']['knowledge_receipt'], 'knowledge-receipt')
        identity = workspace_manifest.raw['identity']
        if any(knowledge_receipt['scope'][key] != identity[key] for key in ('organization_id', 'design_group_id')):
            raise AnalysisError('ANALYSIS_KNOWLEDGE_BINDING_INVALID', 'workspace knowledge authority changed after review')
        actor = workspace_manifest.actor_id
        approval = {'schema_version': 'AnalysisApproval/v1', 'actor_id': actor, 'decision_text': decision_text, 'approval_state': APPROVE, 'reviewed_spec_sha256': spec_sha256, 'spec_sha256': frozen_sha, 'source_binding_ref': record['source_binding_ref'], 'geometry_sha256': frozen['source']['geometry_sha256'], 'regions_sha256': frozen['source']['regions_sha256'], 'material_sha256': _hash(frozen['material']), 'policy_sha256': frozen['tool_constraints']['policy_sha256'], 'tool_bindings_sha256': record['artifacts']['tools']['sha256'], 'summary_sha256': record['artifacts']['summary']['sha256'], 'preview_receipt_sha256': record['artifacts']['preview_receipt']['sha256'], 'knowledge_receipt_sha256': record['artifacts']['knowledge_receipt']['sha256'], 'approved_at': datetime.now(UTC).isoformat()}
        stage = new_stage(self.store, design_id, analysis_id)
        try:
            for reference in record['files']:
                destination = stage / reference['relative_path']
                secure_fs.ensure_managed_directory(destination.parent, parents=True, exist_ok=True)
                secure_fs.atomic_publish_new(destination, read_artifact(folder, reference).content)
            for name, value, schema in (('spec.json', frozen, 'spec'), ('loads.json', ledger, 'load-ledger')):
                secure_fs.atomic_replace(stage / name, canonical_record(value, schema_name=schema))
            artifacts = deepcopy(record['artifacts'])
            artifacts['spec']['sha256'] = frozen_sha
            artifacts['load_ledger']['sha256'] = record_sha256(ledger, schema_name='load-ledger')
            artifacts['approval'] = _put(stage, 'approval.json', approval, 'approval')
            candidate = {**record, 'spec_status': 'approved', 'artifacts': artifacts}
            committed, _ = publish_revision(self.store, design_id, analysis_id, stage, candidate, expected_revision)
        except BaseException:
            if stage.exists(): secure_fs.remove_owned_tree(stage, expected_parent=stage.parent, label='unpublished analysis approval')
            raise
        return {'spec_status': 'approved', 'spec_revision': frozen['revision'], 'spec_sha256': frozen_sha, 'state_revision': committed['state_revision'], 'approval_state': APPROVE, 'approval': approval, 'current_revision': committed['current_revision']}
