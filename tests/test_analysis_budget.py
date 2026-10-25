from pathlib import Path

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
