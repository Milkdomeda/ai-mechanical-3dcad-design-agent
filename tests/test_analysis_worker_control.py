import json
import threading

import pytest


def control():
    from mechanical_design_agent.analysis_worker_control import WorkerControlServer
    cancelled = threading.Event()
    def cancel(run_id):
        if run_id != 'owned-run': return False
        cancelled.set()
        return True
    return WorkerControlServer(snapshot=lambda: {'stage': 'solving', 'active_run_id': 'owned-run'}, cancel=cancel), cancelled


def test_new_client_authenticates_same_live_owner_without_pid_attach():
    from mechanical_design_agent.analysis_worker_control import contact_worker
    server, cancelled = control()
    with server:
        claim = server.private_claim()
        first = contact_worker(claim)
        second = contact_worker(dict(claim))
        assert first['worker_uuid'] == second['worker_uuid'] == claim['worker_uuid']
        assert first['pid'] == claim['pid'] and first['created_ns'] == claim['created_ns']
        assert first['active_run_id'] == 'owned-run'
        assert first['challenge'] != second['challenge']
        assert claim['secret'] not in json.dumps(first)
        assert not cancelled.is_set()


@pytest.mark.parametrize('defect', ['wrong_secret', 'wrong_pid', 'wrong_created', 'wrong_uuid', 'remote_host'])
def test_unproven_identity_never_cancels_an_owned_run(defect):
    from mechanical_design_agent.analysis_worker_control import contact_worker
    server, cancelled = control()
    with server:
        claim = server.private_claim()
        if defect == 'wrong_secret': claim['secret'] = '0' * 64
        if defect == 'wrong_pid': claim['pid'] += 1
        if defect == 'wrong_created': claim['created_ns'] += 1
        if defect == 'wrong_uuid': claim['worker_uuid'] = '0' * 32
        if defect == 'remote_host': claim['host'] = 'example.com'
        assert contact_worker(claim, action='cancel', run_id='owned-run') is None
        assert not cancelled.is_set()


def test_cancel_is_authenticated_and_matches_exact_run_identity():
    from mechanical_design_agent.analysis_worker_control import contact_worker
    server, cancelled = control()
    with server:
        claim = server.private_claim()
        ignored = contact_worker(claim, action='cancel', run_id='unrelated-run')
        assert ignored['status'] == 'cancel_ignored' and not cancelled.is_set()
        accepted = contact_worker(claim, action='cancel', run_id='owned-run')
        assert accepted['status'] == 'cancel_requested' and cancelled.is_set()


def test_closed_owner_cannot_be_recovered_from_persisted_pid_alone():
    from mechanical_design_agent.analysis_worker_control import contact_worker
    server, _ = control()
    with server: claim = server.private_claim()
    assert contact_worker(claim) is None


def test_control_channel_rejects_arbitrary_actions_before_callback():
    from mechanical_design_agent.analysis_worker_control import contact_worker
    server, cancelled = control()
    with server:
        assert contact_worker(server.private_claim(), action='execute_python', run_id='owned-run') is None
        assert not cancelled.is_set()
