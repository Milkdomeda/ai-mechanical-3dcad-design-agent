from copy import deepcopy
import hashlib

import pytest

from mechanical_design_agent.analysis_store import AnalysisStore
from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_store import design
from test_design_session import _fcstd


def test_source_rebind_preserves_old_snapshot_and_capture_without_editing_cad_state(tmp_path):
    root, source, sha = design(tmp_path)
    store = AnalysisStore(tmp_path)
    old = store.prepare_source(design_id='fixture-design', analysis_id='static', source_path=source, expected_sha256=sha)
    reference = store.source_reference('fixture-design', 'static')
    assert store.read_source_binding('fixture-design', 'static', reference) == old['source']
    old_snapshot = (root / 'analyses/static' / old['source']['snapshot_ref']['relative_path']).read_bytes()
    cad_before = (root / 'design.json').read_bytes()
    source.write_bytes(_fcstd(object_name='SecondSource'))
    new_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    (root / 'model.FCStd').write_bytes(source.read_bytes())
    rebound = store.rebind_source(design_id='fixture-design', analysis_id='static', source_path=source, expected_sha256=new_sha, expected_revision=0)
    assert rebound['state_revision'] == 1 and rebound['source']['origin_sha256'] == new_sha
    assert rebound['status'] == 'draft'
    assert store.currentness('fixture-design', 'static')['currentness'] == 'current'
    historical = store.currentness_for_source('fixture-design', 'static', reference)
    assert historical['currentness'] == 'source_changed' and historical['snapshot_integrity'] == 'passed'
    assert store.read_source_binding('fixture-design', 'static', reference) == old['source']
    assert (root / 'analyses/static' / old['source']['snapshot_ref']['relative_path']).read_bytes() == old_snapshot
    assert (root / 'design.json').read_bytes() == cad_before


@pytest.mark.parametrize('defect', ['wrong_revision', 'active_run', 'queued'])
def test_rebind_requires_exact_revision_and_no_active_worker(tmp_path, defect):
    root, source, sha = design(tmp_path)
    store = AnalysisStore(tmp_path)
    state = store.prepare_source(design_id='fixture-design', analysis_id='static', source_path=source, expected_sha256=sha)
    revision = 0
    if defect != 'wrong_revision':
        candidate = deepcopy(state)
        if defect == 'active_run': candidate['active_run_id'] = 'run-1'
        if defect == 'queued': candidate['status'] = 'queued'
        state = store.compare_and_replace(design_id='fixture-design', analysis_id='static', expected_revision=0, next_state=candidate)
        revision = state['state_revision']
    else: revision = 1
    source.write_bytes(_fcstd(object_name='NewBody'))
    with pytest.raises(AnalysisError):
        store.rebind_source(design_id='fixture-design', analysis_id='static', source_path=source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), expected_revision=revision)
    assert store.read_state('fixture-design', 'static') == state


def test_source_capture_tamper_cannot_be_used_for_historical_currentness(tmp_path):
    root, source, sha = design(tmp_path)
    store = AnalysisStore(tmp_path)
    store.prepare_source(design_id='fixture-design', analysis_id='static', source_path=source, expected_sha256=sha)
    reference = store.source_reference('fixture-design', 'static')
    capture = root / 'analyses/static' / reference['relative_path']
    capture.write_bytes(b'{}')
    with pytest.raises(AnalysisError): store.read_source_binding('fixture-design', 'static', reference)
