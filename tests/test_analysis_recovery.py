from pathlib import Path

import pytest

from test_analysis_coordinator import setup, submit


def stale_claim(store, *, claimed=False, run_id=None):
    from mechanical_design_agent.analysis_queue import scheduler_guard
    from mechanical_design_agent.analysis_store import _timestamp
    with scheduler_guard(store.workspace) as scheduler:
        claim = scheduler.claim()
        claim.update(requested_at='2000-01-01T00:00:00+00:00')
        scheduler.write_claim(claim)
        if claimed:
            scheduler.replace({**scheduler.state, 'entries': [{**entry, 'status': 'claimed', 'worker_uuid': claim['worker_uuid']} for entry in scheduler.state['entries']]})
        return scheduler.root / 'worker.lock'


def test_published_queue_repairs_missing_analysis_summary(tmp_path, monkeypatch):
    from mechanical_design_agent import secure_fs
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    original = secure_fs.atomic_replace
    def fail(path, *args, **kwargs):
        if Path(path).name == 'analysis.json': raise OSError('synthetic lost summary publication')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(secure_fs, 'atomic_replace', fail)
        with pytest.raises(OSError): submit(coordinator, approved)
    recovered = submit(coordinator, approved)
    assert recovered['run_status'] == 'queued' and recovered['idempotent']
    assert store.read_state('fixture-design', 'static')['active_run_id'] == recovered['run_id']
    assert len(launcher.calls) == 1
    cancelled = coordinator.cancel(design_id='fixture-design', analysis_id='static', run_id=recovered['run_id'], reason='Recovered request cancelled.', expected_revision=recovered['state_revision'])
    assert cancelled['run_status'] == 'cancelled'


def test_request_without_committed_queue_never_starts_implicitly(tmp_path, monkeypatch):
    from mechanical_design_agent.analysis_queue import Scheduler
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    def fail(*args, **kwargs): raise OSError('synthetic failed queue publication')
    with monkeypatch.context() as patch:
        patch.setattr(Scheduler, 'replace', fail)
        with pytest.raises(OSError): submit(coordinator, approved)
    recovered = submit(coordinator, approved)
    assert recovered['run_status'] == 'interrupted'
    assert len(launcher.calls) == 0
    assert recovered['diagnostic']['code'] == 'ANALYSIS_SUBMISSION_INTERRUPTED'


def test_definite_launcher_failure_reaches_terminal_without_poll_respawn(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    calls = []
    def fail(*args):
        calls.append(args)
        raise OSError('synthetic blocked detached launch')
    coordinator._launcher = fail
    result = submit(coordinator, approved)
    assert result['run_status'] == 'failed'
    assert result['diagnostic']['code'] == 'ANALYSIS_WORKER_START_FAILED'
    for _ in range(2):
        assert coordinator.status(design_id='fixture-design', analysis_id='static', run_id=result['run_id'])['run_status'] == 'failed'
    assert len(calls) == 1


def test_incomplete_claim_does_not_duplicate_unproved_living_owner(tmp_path):
    from mechanical_design_agent import secure_fs
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    lock = stale_claim(store)
    with secure_fs.exclusive_file_lock(lock):
        observed = coordinator.status(design_id='fixture-design', analysis_id='static')
        assert observed['worker_state'] == 'needs_attention'
        assert observed['diagnostic']['code'] == 'ANALYSIS_WORKER_IDENTITY_UNPROVEN'
        assert observed['run_status'] == 'queued'
        assert len(launcher.calls) == 1


def test_lost_claim_interrupts_and_explicit_retry_has_new_identity(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    stale_claim(store, claimed=True)
    coordinator.reconcile()  # explicit recovery mutation; status is read-only
    observed = coordinator.status(design_id='fixture-design', analysis_id='static')
    assert observed['run_status'] == 'interrupted'
    assert observed['diagnostic']['code'] == 'ANALYSIS_WORKER_LOST'
    assert len(launcher.calls) == 1
    retried = coordinator.submit(design_id='fixture-design', analysis_id='static', spec_sha256=approved['spec_sha256'], idempotency_key='explicit-retry', expected_revision=observed['state_revision'], retry_run_id=queued['run_id'])
    assert retried['run_id'] != queued['run_id']
    assert retried['run_status'] == 'queued'
    assert len(launcher.calls) == 2
    previous = coordinator.status(design_id='fixture-design', analysis_id='static', run_id=queued['run_id'])
    assert previous['run_status'] == 'interrupted'


def test_status_observes_a_lost_claim_without_mutating_or_restarting_it(tmp_path):
    from mechanical_design_agent.analysis_queue import scheduler_guard
    root,service,store,request,approved,coordinator,launcher=setup(tmp_path)
    queued=submit(coordinator,approved);stale_claim(store,claimed=True)
    with scheduler_guard(store.workspace) as scheduler:queue_path=scheduler.root/'queue.json'
    files=[queue_path,root/'analyses/static/analysis.json',root/'analyses/static/runs'/queued['run_id']/'run.json']
    before={path:path.read_bytes() for path in files}
    observed=coordinator.status(design_id='fixture-design',analysis_id='static')
    assert observed['worker_state']=='needs_attention'
    assert observed['run_status']=='queued'
    assert {path:path.read_bytes() for path in files}==before
    assert len(launcher.calls)==1
