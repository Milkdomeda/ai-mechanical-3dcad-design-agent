import json

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.workspace_bootstrap import initialize_workspace
from test_analysis_store import design


def setup(tmp_path, loader):
    from mechanical_design_agent.analysis_knowledge import AnalysisKnowledgeService
    initialize_workspace(workspace=tmp_path, actor_id='reviewer', organization_id='org', design_group_id='group', dry_run=False)
    root, source, sha = design(tmp_path)
    runtime = BootstrapRuntime.from_process(cwd=tmp_path, environ={}, workspace=tmp_path)
    service = AnalysisKnowledgeService(runtime, loader)
    return root, service, {'design_id': 'fixture-design', 'analysis_id': 'static', 'source_sha256': sha, 'query': 'static stiffness', 'features': {'material': 'synthetic'}, 'required_ids': [], 'requested_family_id': None}


def test_unavailable_knowledge_does_not_write_design_json(tmp_path):
    def unavailable(*args): raise RuntimeError('private database credentials must not be echoed')
    root, service, request = setup(tmp_path, unavailable)
    before = (root / 'design.json').read_bytes()
    result = service.retrieve(**request)
    assert result['receipt']['status'] == 'unavailable'
    assert result['receipt']['blocking'] is False
    assert result['context'] is None
    assert 'credentials' not in json.dumps(result)
    assert (root / 'design.json').read_bytes() == before


def test_explicit_required_named_knowledge_blocks(tmp_path):
    root, service, request = setup(tmp_path, lambda *_: {'schema_version': 'DesignContext/v2', 'approved_facts': []})
    request['required_ids'] = ['required-fact']
    result = service.retrieve(**request)
    assert result['receipt']['blocking'] is True
    assert result['receipt']['missing_required_ids'] == ['required-fact']


def test_matched_knowledge_receipt_is_independent_and_workspace_scoped(tmp_path):
    def load(query, features):
        assert query == 'static stiffness'
        assert features == {'material': 'synthetic'}
        return {'schema_version': 'DesignContext/v2', 'approved_facts': [{'assertion_id': 'fact-1'}]}
    root, service, request = setup(tmp_path, load)
    request['required_ids'] = ['fact-1']
    result = service.retrieve(**request)
    assert result['receipt']['status'] == 'completed_matches'
    assert result['receipt']['blocking'] is False
    assert result['receipt']['scope'] == {'organization_id': 'org', 'design_group_id': 'group', 'requested_family_id': None}
    assert result['receipt']['actor_id'] == 'reviewer'
    assert result['receipt']['context_sha256']


@pytest.mark.parametrize('defect', ['scope_override', 'family_override', 'malformed_context'])
def test_query_cannot_override_scope_or_accept_nonfinite_context(tmp_path, defect):
    root, service, request = setup(tmp_path, lambda *_: {'approved_facts': [], 'bad': float('nan')})
    if defect == 'scope_override': request['features']['organization_id'] = 'another-org'
    if defect == 'family_override': request['features']['requested_family_id'] = 'another-family'
    if defect != 'malformed_context':
        with pytest.raises(AnalysisError): service.retrieve(**request)
    else:
        result = service.retrieve(**request)
        assert result['receipt']['status'] == 'unavailable' and result['context'] is None
