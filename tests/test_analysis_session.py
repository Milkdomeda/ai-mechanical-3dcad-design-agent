"""Session governance with injected geometry/GUI producers, not native proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.workspace_bootstrap import initialize_workspace
from test_analysis_loads import load_fixture
from test_analysis_store import design


def sha(data):
    return hashlib.sha256(data).hexdigest()


def session(tmp_path, *, unavailable=False):
    from mechanical_design_agent.analysis_knowledge import AnalysisKnowledgeService
    from mechanical_design_agent.analysis_session import AnalysisSessionService
    from mechanical_design_agent.analysis_store import AnalysisStore
    initialize_workspace(workspace=tmp_path, actor_id='reviewer', organization_id='org', design_group_id='group', dry_run=False)
    root, source, source_sha = design(tmp_path)
    runtime = BootstrapRuntime.from_process(cwd=tmp_path, environ={}, workspace=tmp_path)
    spec, bundle = load_fixture()
    spec.update(design_id='fixture-design', analysis_id='static', source=None, unresolved=[], status='draft')
    spec['mesh'] = {'element_type': 'C3D10', 'initial_size': {'value': '4', 'unit': 'mm'}, 'refinement_ratio': {'value': '.7', 'unit': '1'}, 'levels': 3, 'minimum_scaled_jacobian': {'value': '.1', 'unit': '1'}, 'minimum_thickness_elements': 2}
    spec['budget'] = {'wall_seconds': 300, 'stage_seconds': 120, 'threads': 1, 'max_nodes': 100000, 'max_elements': 100000, 'max_disk_bytes': 1000000000, 'max_attempts': 1, 'memory_mode': 'observed_only', 'max_memory_mb': 1024}
    settings = {'runtime': {'manifest_sha256': 'c' * 64}, 'configuration_sha256': 'd' * 64, 'tools': {
        name: {'path': str(tmp_path / name), 'version': version, 'sha256': sha(name.encode()), 'identity': {'volume': 1, 'file_index': index}}
        for index, (name, version) in enumerate([('freecadcmd', '1.1.3'), ('freecadgui', '1.1.3'), ('gmsh', '4.15.0'), ('ccx', '2.23')], 1)}}
    def inspect(context):
        folder = Path(context['output_dir']); folder.mkdir()
        assert Path(context['source_path']).read_bytes() == source.read_bytes()
        result = deepcopy(bundle['geometry'])
        result['source_sha256'] = context['request']['source_sha256']
        brep = b'synthetic injected BREP; native acceptance runs elsewhere'
        (folder / 'geometry.brep').write_bytes(brep)
        fcstd = Path(context['source_path']).read_bytes()
        (folder / 'geometry.FCStd').write_bytes(fcstd)
        result['geometry_sha256'] = sha(brep)
        result['artifacts']['brep']['sha256'] = sha(brep)
        result['artifacts']['fcstd']['sha256'] = sha(fcstd)
        return result
    def load(query, features):
        if unavailable: raise RuntimeError('synthetic unavailable backend')
        return {'schema_version': 'DesignContext/v2', 'approved_facts': []}
    knowledge = AnalysisKnowledgeService(runtime, load)
    def preview(context):
        folder = Path(context['output_dir']); folder.mkdir()
        image = b'\x89PNG\r\n\x1a\nsynthetic unit-test producer image'
        (folder / 'regions.png').write_bytes(image)
        spec = context['spec']
        return {'schema_version': 'AnalysisPreviewReceipt/v1', 'status': 'completed', 'operator': 'assistant', 'design_id': 'fixture-design', 'analysis_id': 'static', 'spec_sha256': context['spec_sha256'], 'source_sha256': spec['source']['sha256'], 'geometry_sha256': spec['source']['geometry_sha256'], 'regions_sha256': spec['source']['regions_sha256'], 'runtime_manifest_sha256': settings['runtime']['manifest_sha256'], 'visible_region_ids': sorted(entry['region_id'] for entry in context['regions']['bindings']), 'images': [{'relative_path': 'regions.png', 'sha256': sha(image)}]}
    store = AnalysisStore(tmp_path)
    service = AnalysisSessionService(store, configuration_loader=lambda: deepcopy(settings), geometry_inspector=inspect, knowledge_loader=lambda context: knowledge.retrieve(**context), gui_previewer=preview)
    request = {'schema_version': 'AnalysisPrepareRequest/v1', 'source_path': str(source), 'expected_source_sha256': source_sha, 'draft_spec': spec,
               'geometry_request': {'schema_version': 'AnalysisGeometryRequest/v1', 'source_sha256': source_sha, 'included_object': 'Solid', 'excluded_objects': ['Ball1', 'Ball2', 'Ball3', 'Ball4'], 'partitions': [], 'volume_tolerance': {'value': '.000001', 'unit': 'mm^3'}},
               'region_selectors': [entry['selector'] for entry in bundle['regions']['bindings']],
               'knowledge': {'query': 'static stiffness', 'required_ids': [], 'requested_family_id': None}}
    return root, service, store, request, settings


def test_approval_covers_regions_material_policy_and_toolpins(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    cad_before = (root / 'design.json').read_bytes()
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    assert prepared['spec_status'] == 'ready_for_review'
    from mechanical_design_agent.analysis_revision import read_revision, read_artifact
    reviewed_folder, reviewed_record = read_revision(store, 'fixture-design', 'static')
    reviewed_spec = json.loads(read_artifact(reviewed_folder, reviewed_record['artifacts']['spec']).content)
    approved = service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    assert approved['spec_status'] == 'approved'
    assert approved['approval']['actor_id'] == 'reviewer'
    assert approved['approval']['reviewed_spec_sha256'] == prepared['spec_sha256']
    assert approved['approval']['spec_sha256'] == approved['spec_sha256']
    frozen_folder, frozen_record = read_revision(store, 'fixture-design', 'static')
    frozen_spec = json.loads(read_artifact(frozen_folder, frozen_record['artifacts']['spec']).content)
    assert {**frozen_spec, 'status': 'ready_for_review'} == reviewed_spec
    for key in ('regions_sha256', 'geometry_sha256', 'material_sha256', 'policy_sha256', 'tool_bindings_sha256', 'summary_sha256', 'preview_receipt_sha256'):
        assert len(approved['approval'][key]) == 64
    assert (root / 'design.json').read_bytes() == cad_before


@pytest.mark.parametrize('decision,state', [('如果材料改好就同意', 'UNCLEAR'), ('不同意', 'REJECT')])
def test_conditional_decision_does_not_approve(tmp_path, decision, state):
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    before = store.read_state('fixture-design', 'static')
    result = service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text=decision, expected_revision=prepared['state_revision'])
    assert result['approval_state'] == state
    assert result['spec_status'] != 'approved'
    assert store.read_state('fixture-design', 'static') == before


def test_revision_change_clears_new_approval_only(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    approved = service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    old = store.read_state('fixture-design', 'static')
    old_record = (root / 'analyses/static' / old['current_revision']['relative_path']).read_bytes()
    request['draft_spec']['material']['youngs_modulus']['value'] = '2000'
    next_spec = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=approved['state_revision'])
    assert next_spec['spec_revision'] == 2 and next_spec['spec_status'] == 'ready_for_review'
    assert next_spec['spec_sha256'] != approved['spec_sha256']
    assert store.read_state('fixture-design', 'static')['approval'] is None
    assert (root / 'analyses/static' / old['current_revision']['relative_path']).read_bytes() == old_record


def test_unavailable_knowledge_does_not_write_cad_state_and_named_requirement_blocks(tmp_path):
    root, service, store, request, settings = session(tmp_path, unavailable=True)
    before = (root / 'design.json').read_bytes()
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    assert prepared['spec_status'] == 'ready_for_review'
    request['knowledge']['required_ids'] = ['named-required-fact']
    blocked = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=prepared['state_revision'])
    assert blocked['spec_status'] == 'needs_input'
    assert 'ANALYSIS_REQUIRED_KNOWLEDGE_MISSING' in [item['code'] for item in blocked['issues']]
    assert (root / 'design.json').read_bytes() == before


@pytest.mark.parametrize('defect', ['wrong_hash', 'stale_revision', 'source_changed', 'preview_tamper'])
def test_approval_fails_for_stale_or_changed_review_evidence(tmp_path, defect):
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    digest, revision = prepared['spec_sha256'], prepared['state_revision']
    if defect == 'wrong_hash': digest = 'f' * 64
    if defect == 'stale_revision': revision -= 1
    if defect == 'source_changed': (root / 'model.FCStd').write_bytes(b'changed current source')
    if defect == 'preview_tamper':
        images = list((root / 'analyses/static/revisions').rglob('regions.png'))
        assert len(images) == 1
        images[0].write_bytes(b'tampered')
    with pytest.raises(AnalysisError): service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=digest, decision_text='确认', expected_revision=revision)


def test_missing_physical_input_is_not_replaced_with_defaults(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    request['draft_spec']['material'] = None
    result = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    assert result['spec_status'] == 'needs_input'
    assert 'MATERIAL_UNRESOLVED' in [item['code'] for item in result['issues']]
    with pytest.raises(AnalysisError): service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=result['spec_sha256'], decision_text='确认', expected_revision=result['state_revision'])


def test_changed_configured_tool_after_preview_cannot_be_approved(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    settings['tools']['ccx']['sha256'] = 'f' * 64
    with pytest.raises(AnalysisError) as error:
        service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    assert error.value.code == 'ANALYSIS_TOOL_CONSTRAINT_CHANGED'


def test_workspace_knowledge_scope_change_requires_new_review(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    path = tmp_path / 'config/mechanical_design.json'
    manifest = json.loads(path.read_text())
    manifest['identity']['organization_id'] = 'other-org'
    path.write_text(json.dumps(manifest))
    with pytest.raises(AnalysisError) as error:
        service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    assert error.value.code == 'ANALYSIS_KNOWLEDGE_BINDING_INVALID'


@pytest.mark.parametrize('defect', ['foreign_scope', 'wrong_required_ids', 'false_nonblocking'])
def test_knowledge_receipt_cannot_change_authority_or_required_items(tmp_path, defect):
    root, service, store, request, settings = session(tmp_path)
    original = service.knowledge_loader
    request['knowledge']['required_ids'] = ['required-fact']
    def wrong(context):
        result = original(context)
        if defect == 'foreign_scope': result['receipt']['scope']['organization_id'] = 'other-org'
        if defect == 'wrong_required_ids': result['receipt']['required_ids'] = []
        if defect == 'false_nonblocking': result['receipt']['blocking'] = False
        return result
    service.knowledge_loader = wrong
    with pytest.raises(AnalysisError): service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)


def test_geometry_producer_cannot_exceed_declared_partition_volume_error(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    original = service.geometry_inspector
    def wrong(context):
        result = original(context)
        result['volume']['value'] = '11000'
        return result
    service.geometry_inspector = wrong
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    assert prepared['spec_status'] == 'needs_input'
    assert 'GEOMETRY_VOLUME_CHANGED' in [issue['code'] for issue in prepared['issues']]


def test_wrong_preview_producer_hash_never_makes_revision_ready(tmp_path):
    root, service, store, request, settings = session(tmp_path)
    original = service.gui_previewer
    def wrong(context):
        result = original(context)
        result['spec_sha256'] = 'f' * 64
        return result
    service.gui_previewer = wrong
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    assert prepared['spec_status'] == 'needs_input'
    assert 'ANALYSIS_PREVIEW_INVALID' in [issue['code'] for issue in prepared['issues']]


def test_concurrent_prepares_have_one_revision_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    original = service.geometry_inspector
    barrier = threading.Barrier(2)
    def inspect(context):
        barrier.wait(timeout=10)
        return original(context)
    service.geometry_inspector = inspect
    def prepare(_):
        try:
            return service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=prepared['state_revision'])['spec_revision']
        except AnalysisError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor: results = list(executor.map(prepare, range(2)))
    assert results.count(2) == 1
    assert set(results) - {2} == {'ANALYSIS_REVISION_CONFLICT'}
    assert store.read_state('fixture-design', 'static')['spec_revision'] == 2
    assert not list((root / 'analyses/static/revisions').glob('.staging-*'))


def test_crash_after_revision_publish_preserves_previous_pointer(tmp_path, monkeypatch):
    from mechanical_design_agent import secure_fs
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    old = store.read_state('fixture-design', 'static')
    original = secure_fs.atomic_replace
    def fail(path, *args, **kwargs):
        if Path(path).name == 'analysis.json': raise OSError('synthetic state publication failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(secure_fs, 'atomic_replace', fail)
    with pytest.raises(OSError): service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=prepared['state_revision'])
    assert store.read_state('fixture-design', 'static') == old
    assert len(list((root / 'analyses/static/revisions').glob('r*/revision.json'))) == 2
    from mechanical_design_agent.analysis_revision import read_revision
    _, selected = read_revision(store, 'fixture-design', 'static')
    assert selected['spec_revision'] == 1
