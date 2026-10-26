"""Durable submission metadata with an injected supervisor launcher."""
from copy import deepcopy
import json

import pytest

from mechanical_design_agent.analysis_process import ProcessController
from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_session import session


class Launcher:
    def __init__(self): self.calls = []
    def __call__(self, workspace, claim_id):
        self.calls.append((workspace, claim_id))


def setup(tmp_path):
    from mechanical_design_agent.analysis_coordinator import AnalysisCoordinator
    root, service, store, request, settings = session(tmp_path)
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=None)
    approved = service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    launcher = Launcher()
    coordinator = AnalysisCoordinator(store, ProcessController(), supervisor_launcher=launcher)
    return root, service, store, request, approved, coordinator, launcher


def submit(coordinator, approved, *, key='request-1', analysis_id='static'):
    return coordinator.submit(design_id='fixture-design', analysis_id=analysis_id, spec_sha256=approved['spec_sha256'], idempotency_key=key, expected_revision=approved['state_revision'])


def test_duplicate_submit_creates_one_run(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    before = (root / 'design.json').read_bytes()
    first = submit(coordinator, approved)
    repeated = submit(coordinator, approved)
    assert first['run_id'] == repeated['run_id']
    assert repeated['idempotent'] is True
    assert first['run_status'] == 'queued'
    assert len(launcher.calls) == 1
    assert len(list((root / 'analyses/static/runs').glob('run-*/request.json'))) == 1
    assert (root / 'design.json').read_bytes() == before


def test_workspace_has_one_active_worker_for_multiple_queued_analyses(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    other = deepcopy(request)
    other['draft_spec']['analysis_id'] = 'other'
    prepared = service.prepare(design_id='fixture-design', analysis_id='other', request=other, expected_revision=None)
    other_approval = service.approve(design_id='fixture-design', analysis_id='other', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    first = submit(coordinator, approved)
    second = submit(coordinator, other_approval, key='request-2', analysis_id='other')
    assert first['run_id'] != second['run_id']
    assert len(launcher.calls) == 1
    assert second['run_status'] == 'queued'


def test_cancel_during_startup_reaches_terminal(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    cancelled = coordinator.cancel(design_id='fixture-design', analysis_id='static', run_id=queued['run_id'], reason='User cancelled before native startup.', expected_revision=queued['state_revision'])
    assert cancelled['run_status'] == 'cancelled'
    assert store.read_state('fixture-design', 'static')['active_run_id'] is None
    observed = coordinator.status(design_id='fixture-design', analysis_id='static', run_id=queued['run_id'])
    assert observed['run_status'] == 'cancelled'
    assert any(event['stage'] == 'cancelled' for event in observed['events'])


def test_submission_request_pins_frozen_source_tools_policy_and_approval(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    record = json.loads((root / 'analyses/static/runs' / queued['run_id'] / 'request.json').read_text())
    assert record['spec_sha256'] == approved['spec_sha256']
    assert record['revision_ref'] == approved['current_revision']
    assert record['source_binding_ref']['sha256']
    assert record['approval_ref']['sha256']
    assert record['policy_sha256'] == approved['approval']['policy_sha256']
    assert record['tool_bindings_sha256'] == approved['approval']['tool_bindings_sha256']
    assert record['idempotency_sha256'] and 'idempotency_key' not in record


def test_same_key_with_changed_specification_never_creates_another_solver(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    cancelled = coordinator.cancel(design_id='fixture-design', analysis_id='static', run_id=queued['run_id'], reason='New approved material.', expected_revision=queued['state_revision'])
    request['draft_spec']['material']['youngs_modulus']['value'] = '2000'
    prepared = service.prepare(design_id='fixture-design', analysis_id='static', request=request, expected_revision=cancelled['state_revision'])
    changed = service.approve(design_id='fixture-design', analysis_id='static', spec_sha256=prepared['spec_sha256'], decision_text='确认', expected_revision=prepared['state_revision'])
    with pytest.raises(AnalysisError) as error: submit(coordinator, changed)
    assert error.value.code == 'ANALYSIS_IDEMPOTENCY_CONFLICT'
    assert len(launcher.calls) == 1


def test_same_analysis_cannot_queue_two_distinct_active_runs(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    current = {**approved, 'state_revision': queued['state_revision']}
    with pytest.raises(AnalysisError) as error: submit(coordinator, current, key='another-request')
    assert error.value.code == 'ANALYSIS_BUSY'
    assert len(launcher.calls) == 1
