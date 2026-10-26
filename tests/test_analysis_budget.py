from pathlib import Path
import os

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def policy():return {'wall_seconds':10,'max_disk_bytes':1024,'max_memory_mb':128,'memory_mode':'observed_only'}


def test_budget_cancellation_deadline_and_actual_disk_are_independent(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    clock=[0.];cancel=[False]
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:cancel[0],memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},clock=lambda:clock[0])
    guard.checkpoint(force=True)
    (tmp_path/'large').write_bytes(b'x'*1025)
    with pytest.raises(AnalysisError) as error:guard.checkpoint(force=True)
    assert error.value.code=='ANALYSIS_BUDGET_EXCEEDED'
    (tmp_path/'large').unlink();clock[0]=11
    with pytest.raises(AnalysisError) as error:guard.checkpoint(force=True)
    assert error.value.code=='ANALYSIS_TIMED_OUT'
    cancel[0]=True
    with pytest.raises(AnalysisError) as error:guard.checkpoint(force=True)
    assert error.value.code=='ANALYSIS_CANCELLED'


def test_memory_observation_is_not_a_hard_os_quota_or_a_missing_probe_pass(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    for observed,code in [({'bytes':129*1024*1024,'measurement':'synthetic'},'ANALYSIS_BUDGET_EXCEEDED'),(None,'ANALYSIS_MEMORY_UNAVAILABLE')]:
        guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:False,memory_observer=lambda:observed)
        with pytest.raises(AnalysisError) as error:guard.checkpoint(force=True)
        assert error.value.code==code


def test_fast_checkpoints_are_throttled_but_cancellation_is_immediate(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    counter=[];cancel=[False]
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:cancel[0],memory_observer=lambda:(counter.append(1) or {'bytes':10,'measurement':'synthetic'}),clock=lambda:0.)
    for _ in range(1000):guard.checkpoint()
    assert len(counter)==1
    cancel[0]=True
    with pytest.raises(AnalysisError):guard.checkpoint()


def test_slow_integrity_probe_does_not_restart_for_every_runtime_file(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    ticks=[0.];probes=[];cancel=[False]
    def integrity():
        probes.append(ticks[0]);ticks[0]+=1.
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:cancel[0],memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},clock=lambda:ticks[0],integrity_checkpoint=integrity)
    guard.checkpoint()
    for _ in range(1000):guard.checkpoint()
    assert probes==[0.]
    ticks[0]+=0.5;guard.checkpoint()
    assert probes==[0.,1.5]
    guard.checkpoint(force=True)
    assert probes==[0.,1.5,2.5]
    cancel[0]=True
    with pytest.raises(AnalysisError) as error:guard.checkpoint()
    assert error.value.code=='ANALYSIS_CANCELLED'


@pytest.mark.parametrize('interrupt',['deadline','cancel'])
def test_interrupt_during_slow_probe_is_reported_before_return(tmp_path,interrupt):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    ticks=[0.];cancel=[False]
    def integrity():
        ticks[0]=11.
        cancel[0]=interrupt=='cancel'
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:cancel[0],memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},clock=lambda:ticks[0],integrity_checkpoint=integrity)
    with pytest.raises(AnalysisError) as error:guard.checkpoint()
    assert error.value.code==('ANALYSIS_CANCELLED' if interrupt=='cancel' else 'ANALYSIS_TIMED_OUT')


def test_bounded_verification_scans_hash_executor_at_both_boundaries(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    ticks=[0.];probes=[];samples=[]
    def integrity():probes.append(ticks[0]);ticks[0]+=.6
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:False,memory_observer=lambda:(samples.append(ticks[0]) or {'bytes':10,'measurement':'synthetic'}),clock=lambda:ticks[0],integrity_checkpoint=integrity)
    with guard.verification_batch():
        for _ in range(1000):
            ticks[0]+=.001;guard.checkpoint()
    assert len(probes)==2
    assert len(samples)>=3,'memory/disk observation must continue inside the scan'
    assert 2.<guard.receipt()['wall_seconds']<3.
    ticks[0]+=.5;guard.checkpoint()
    assert len(probes)==3,'ordinary process checkpoints retain full integrity checks'


def test_source_changed_during_verification_cannot_cross_success_boundary(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    source=tmp_path/'source';source.write_bytes(b'original');published=[]
    def integrity():
        if source.read_bytes()!=b'original':raise AnalysisError('ANALYSIS_EXECUTOR_CHANGED','synthetic source mutation')
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:False,memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},integrity_checkpoint=integrity)
    with pytest.raises(AnalysisError) as error:
        with guard.verification_batch():source.write_bytes(b'mutated');guard.checkpoint()
        published.append('native start or publication')
    assert error.value.code=='ANALYSIS_EXECUTOR_CHANGED' and published==[]


@pytest.mark.parametrize('interrupt',['deadline','cancel','memory','disk'])
def test_verification_batch_keeps_all_resource_and_interrupt_checks(tmp_path,interrupt):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    ticks=[0.];cancel=[False];memory=[10]
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:cancel[0],memory_observer=lambda:{'bytes':memory[0],'measurement':'synthetic'},clock=lambda:ticks[0])
    with pytest.raises(AnalysisError) as error:
        with guard.verification_batch():
            ticks[0]=11. if interrupt=='deadline' else 1.
            cancel[0]=interrupt=='cancel'
            if interrupt=='memory':memory[0]=129*1024*1024
            if interrupt=='disk':(tmp_path/'large').write_bytes(b'x'*1025)
            guard.checkpoint()
    assert error.value.code=={'deadline':'ANALYSIS_TIMED_OUT','cancel':'ANALYSIS_CANCELLED','memory':'ANALYSIS_BUDGET_EXCEEDED','disk':'ANALYSIS_BUDGET_EXCEEDED'}[interrupt]


def test_verification_batch_nesting_forced_checks_and_exception_reset(tmp_path):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    probes=[]
    guard=BudgetGuard(policy(),roots=lambda:[tmp_path],cancelled=lambda:False,memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},integrity_checkpoint=lambda:probes.append(1))
    with pytest.raises(ValueError,match='original failure'):
        with guard.verification_batch():
            with guard.verification_batch():guard.checkpoint(force=True)
            assert len(probes)==4
            raise ValueError('original failure')
    guard.checkpoint(force=True)
    assert len(probes)==5


@pytest.mark.skipif(os.name!='posix',reason='FreeCAD Unix local IPC exists only on POSIX')
def test_only_the_owned_native_root_can_contain_freecads_transient_socket(tmp_path,monkeypatch):
    import socket
    from mechanical_design_agent.analysis_budget import BudgetGuard,disk_bytes
    from fea_evaluation_fixture import evaluation_spec
    root=tmp_path/'native';root.mkdir(mode=0o700)
    monkeypatch.chdir(root)
    with socket.socket(socket.AF_UNIX) as ipc:
        ipc.bind('FreeCAD')
        with pytest.raises(AnalysisError):disk_bytes([root])
        guard=BudgetGuard(evaluation_spec()['budget'],roots=lambda:[],cancelled=lambda:False,memory_observer=lambda:{'bytes':1,'measurement':'fixture'})
        guard.track_root(root,native_ipc=True);guard.checkpoint(force=True)
        with socket.socket(socket.AF_UNIX) as foreign:
            foreign.bind('unexpected')
            with pytest.raises(AnalysisError):guard.checkpoint(force=True)
