import difflib
import hashlib
from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_r2_changes_only_the_reviewed_fixed_branch_and_rejects_other_sources(monkeypatch):
    from mechanical_design_agent import analysis_runtime_patch as module
    source = b'fixed branch\nTOTALS=ONLY\ndisplacement branch\nTOTALS=ONLY\n'
    target = source.replace(b'TOTALS=ONLY', b'TOTALS=YES', 1)
    monkeypatch.setattr(module, 'BASE_SHA256', hashlib.sha256(source).hexdigest())
    monkeypatch.setattr(module, 'PATCHED_SHA256', hashlib.sha256(target).hexdigest())
    monkeypatch.setattr(module, 'ORIGINAL_LINE', b'TOTALS=ONLY')
    monkeypatch.setattr(module, 'PATCHED_LINE', b'TOTALS=YES')
    assert module.patch_output_source(source) == target
    assert module.patch_output_source(source.replace(b'\n',b'\r\n')) == target
    for bad in (source+b'changed', target, source.replace(b'\n',b'\r')):
        with pytest.raises(AnalysisError): module.patch_output_source(bad)


def test_packaged_r2_diff_and_licenses_match_approved_scope():
    from mechanical_design_agent.analysis_runtime_patch import patch_assets
    value = patch_assets()
    assert value['provenance']['changed_lines'] == 1
    assert value['provenance']['patch_sha256'] == hashlib.sha256(value['patch']).hexdigest()
    changes = [line for line in value['patch'].decode().splitlines() if line[:1] in ('+','-') and not line.startswith(('+++','---'))]
    assert len(changes) == 2
    assert changes[0][1:].replace('TOTALS=ONLY', 'TOTALS=YES') == changes[1][1:]
    assert b'GNU' in value['license']


@pytest.mark.parametrize('target', ['native','native/nested'])
def test_runtime_preparation_never_patches_a_source_or_reuses_a_target(tmp_path,monkeypatch,target):
    from test_analysis_configuration import configured
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_runtime import prepare_nodal_rf_runtime
    selected,_,_ = configured(tmp_path,monkeypatch)
    settings = load_analysis_configuration(selected)
    before = {p:p.read_bytes() for p in (tmp_path/'native').iterdir()}
    with pytest.raises(AnalysisError) as error:
        prepare_nodal_rf_runtime(settings,tmp_path/target)
    assert error.value.code == 'FEA_RUNTIME_TARGET_UNSAFE'
    assert before == {p:p.read_bytes() for p in (tmp_path/'native').iterdir()}
