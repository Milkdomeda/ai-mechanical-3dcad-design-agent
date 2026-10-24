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
