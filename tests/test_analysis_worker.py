"""Real owner lock/control transport; injected Python-only execution fixtures."""
from pathlib import Path
import threading
import time

import pytest

from mechanical_design_agent.analysis_coordinator import AnalysisCoordinator
from mechanical_design_agent.analysis_process import ProcessController
from mechanical_design_agent.analysis_queue import scheduler_guard
from test_analysis_coordinator import setup, submit
from test_analysis_process import wait_for


def launch_thread(store, execute):
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    owners, threads = [], []
    def launch(workspace, claim_id):
        owner = AnalysisSupervisor(store, claim_id, execute=execute)
        thread = threading.Thread(target=owner.run)
        owners.append(owner)
        threads.append(thread)
        thread.start()
    return launch, owners, threads


def test_new_mcp_attaches_to_living_owner_and_cancels(tmp_path):
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    entered = threading.Event()
    def execute(context):
        context.phase('meshing', 'Synthetic blocking phase, no native solver.')
        entered.set()
        while not context.cancelled(): time.sleep(.01)
        return {'status': 'cancelled'}
    launcher, owners, threads = launch_thread(store, execute)
    coordinator._launcher = launcher
    queued = submit(coordinator, approved)
    try:
        assert entered.wait(4)
        replacement = AnalysisCoordinator(store, ProcessController(), supervisor_launcher=lambda *args: pytest.fail('duplicate owner'))
        status = replacement.status(design_id='fixture-design', analysis_id='static')
        assert status['worker_state'] == 'ready' and status['run_status'] == 'meshing'
        assert 'secret' not in str(status) and 'port' not in status
        replacement.cancel(design_id='fixture-design', analysis_id='static', run_id=queued['run_id'], reason='Cancel from a new MCP owner.', expected_revision=status['state_revision'])
        threads[0].join(5)
        assert not threads[0].is_alive()
        final = replacement.status(design_id='fixture-design', analysis_id='static')
        assert final['run_status'] == 'cancelled'
        with scheduler_guard(store.workspace) as scheduler:
            assert scheduler.state['entries'] == [] and scheduler.claim()['state'] == 'stopped'
    finally:
        owners[0].request_cancel(queued['run_id'])
        threads[0].join(5)


def test_default_executor_cannot_claim_success(tmp_path):
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    owner = AnalysisSupervisor(store, launcher.calls[0][1])
    assert owner.run() == 0
    status = coordinator.status(design_id='fixture-design', analysis_id='static')
    assert status['run_status'] == 'failed'
    assert status['diagnostic']['code'] == 'FEA_EXECUTOR_UNAVAILABLE'


def test_duplicate_supervisor_never_enters_executor(tmp_path):
    from mechanical_design_agent import secure_fs
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    with scheduler_guard(store.workspace) as scheduler:
        lock = scheduler.root / 'worker.lock'
    with secure_fs.exclusive_file_lock(lock):
        assert AnalysisSupervisor(store, launcher.calls[0][1], execute=lambda context: pytest.fail('second executor')).run() == 2
    assert coordinator._status('fixture-design', 'static', queued['run_id'], 0)['run_status'] == 'queued'


def test_detached_packaged_entry_has_controlled_terminal_result(tmp_path):
    from mechanical_design_agent.analysis_worker import launch_supervisor
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    coordinator._launcher = launch_supervisor
    queued = submit(coordinator, approved)
    result = wait_for(lambda: (value if (value := coordinator.status(design_id='fixture-design', analysis_id='static'))['run_status'] == 'failed' else None), timeout=10)
    assert result['diagnostic']['code'] == 'FEA_EXECUTOR_UNAVAILABLE'
    assert result['run_id'] == queued['run_id']


def test_completed_without_verified_completion_is_rejected(tmp_path):
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    root, service, store, request, approved, coordinator, launcher = setup(tmp_path)
    queued = submit(coordinator, approved)
    owner = AnalysisSupervisor(store, launcher.calls[0][1], execute=lambda context: {'status': 'completed', 'numerical_status': 'passed'})
    owner.run()
    result = coordinator.status(design_id='fixture-design', analysis_id='static')
    assert result['run_status'] == 'failed'
    assert result['diagnostic']['code'] == 'ANALYSIS_COMPLETION_UNVERIFIED'
