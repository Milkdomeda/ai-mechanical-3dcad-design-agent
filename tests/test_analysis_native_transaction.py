from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_configuration import configured


def test_exited_transaction_cannot_start_another_native_process(tmp_path, monkeypatch):
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_native_transaction import NativeTransaction
    selected, _, _ = configured(tmp_path, monkeypatch)
    settings = load_analysis_configuration(selected)
    native = NativeTransaction(settings)
    with native:
        assert native.owned is not None
    with pytest.raises(AnalysisError):
        native.run('build', {}, {'geometry.brep': tmp_path/'absent', 'mesh.unv': tmp_path/'absent'}, tmp_path/'output', timeout_seconds=2, max_log_bytes=1024)
    assert not (tmp_path/'output').exists()
    assert native.owned is None


def test_only_fixed_phase_and_input_inventory_are_accepted(tmp_path):
    from types import SimpleNamespace
    from mechanical_design_agent.analysis_native_transaction import NativeTransaction
    native=NativeTransaction({})
    native.owned=SimpleNamespace(path=tmp_path)
    for phase, inputs in [('user_code', {}), ('build', {'source.FCStd': tmp_path/'source.FCStd'})]:
        with pytest.raises(AnalysisError) as error:
            native.run(phase, {}, inputs, tmp_path/'output', timeout_seconds=2, max_log_bytes=1024)
        assert error.value.code=='ANALYSIS_NATIVE_PHASE_INVALID'
    assert list(tmp_path.iterdir())==[]


def test_seal_rechecks_runtime_before_publication_and_prevents_later_phases(tmp_path, monkeypatch):
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_native_transaction import NativeTransaction
    selected, _, _ = configured(tmp_path, monkeypatch)
    settings = load_analysis_configuration(selected)
    with NativeTransaction(settings) as native:
        # A changed executable must not become an authoritative result.
        tool = Path(settings['tools']['freecadcmd']['path'])
        original = tool.read_bytes()
        tool.write_bytes(original + b'changed')
        try:
            with pytest.raises(AnalysisError): native.seal()
            assert native._sealed is False
        finally:
            tool.write_bytes(original)
        native.seal()
        assert native._sealed is True
        with pytest.raises(AnalysisError):
            native.run('build', {}, {'geometry.brep': tmp_path/'absent', 'mesh.unv': tmp_path/'absent'}, tmp_path/'output', timeout_seconds=2, max_log_bytes=1024)
        assert not (tmp_path/'output').exists()


def test_viewer_keeps_saved_work_without_holding_the_solve_root_lock(tmp_path,monkeypatch):
    from mechanical_design_agent import secure_fs
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_native_transaction import NativeTransaction
    selected,_,_=configured(tmp_path,monkeypatch);settings=load_analysis_configuration(selected)
    root=Path(settings['native_scratch_root']);root.mkdir(mode=0o700)
    with NativeTransaction(settings,viewer=True) as native:
        saved=native.owned.path/'working.FCStd';saved.write_bytes(b'isolated viewer working-copy retention fixture')
        assert saved.is_relative_to(root)
        with secure_fs.try_exclusive_file_lock(root/'.native-root.lock') as owned:
            assert owned,'viewer must not occupy the solve root lifetime lock'
    assert saved.read_bytes()==b'isolated viewer working-copy retention fixture'


@pytest.mark.parametrize('changed',[False,True])
def test_complete_runtime_scan_cannot_start_native_work_with_changed_executor(tmp_path,monkeypatch,changed):
    from mechanical_design_agent.analysis_budget import BudgetGuard
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent import analysis_native_transaction as module
    selected,_,_=configured(tmp_path,monkeypatch);settings=load_analysis_configuration(selected)
    ticks=[0.];mutated=[False];probes=[];scans=[];original=module.verify_settings
    def integrity():
        probes.append(ticks[0])
        if mutated[0]:raise AnalysisError('ANALYSIS_EXECUTOR_CHANGED','synthetic executor mutation')
    def scan(settings,*,checkpoint):
        original(settings,checkpoint=checkpoint);scans.append('complete inventory checked')
        for _ in range(1000):ticks[0]+=.001;checkpoint()
        mutated[0]=changed
    monkeypatch.setattr(module,'verify_settings',scan)
    guard=BudgetGuard({'wall_seconds':10,'max_disk_bytes':1000000,'max_memory_mb':128,'memory_mode':'observed_only'},roots=lambda:[],cancelled=lambda:False,memory_observer=lambda:{'bytes':10,'measurement':'synthetic'},clock=lambda:ticks[0],integrity_checkpoint=integrity)
    native=module.NativeTransaction(settings,checkpoint=guard.checkpoint,budget_guard=guard)
    if changed:
        with pytest.raises(AnalysisError) as error:native.__enter__()
        assert error.value.code=='ANALYSIS_EXECUTOR_CHANGED'
        assert native.owned is None and not native._sealed
        assert len(scans)==1 and len(probes)==3
    else:
        with native:
            assert len(scans)==1 and len(probes)==3
            native.seal()
            assert len(scans)==2 and len(probes)==5
