import hashlib
from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_packaged_r1_assets_are_byte_identical_to_reviewed_source_patch():
    from mechanical_design_agent.analysis_setup import faceforce_assets,patch_faceforce_source
    root=Path(__file__).resolve().parents[1]/'runtime-patches/freecad-1.1.3-faceforce-r1'
    assets=faceforce_assets()
    for name in ('native_faceforce.py','meshtools.patch','provenance.json','COPYING.LESSER'):
        assert assets[name]==(root/name).read_bytes()
    assert hashlib.sha256(assets['native_faceforce.py']).hexdigest()==assets['identities']['native_faceforce.py']
    with pytest.raises(AnalysisError) as error:patch_faceforce_source(b'unknown upstream source')
    assert error.value.code=='FEA_PATCH_BASE_CHANGED'


@pytest.mark.parametrize('destination',['source','source/inside','existing'])
def test_setup_never_mutates_source_or_existing_destination(tmp_path,destination):
    from mechanical_design_agent.analysis_setup import prepare_runtime
    source=tmp_path/'source';source.mkdir();(source/'sentinel').write_bytes(b'protected')
    (tmp_path/'existing').mkdir()
    before={p:p.read_bytes() for p in source.iterdir()}
    with pytest.raises(AnalysisError) as error:
        prepare_runtime(source,tmp_path/destination,native_scratch_root=tmp_path/'scratch')
    assert error.value.code=='FEA_RUNTIME_TARGET_UNSAFE'
    assert {p:p.read_bytes() for p in source.iterdir()}==before


def test_setup_discovery_never_accepts_an_external_executable(tmp_path):
    from mechanical_design_agent.analysis_setup import discover_tools
    source=tmp_path/'source';source.mkdir();external=tmp_path/'external';external.write_bytes(b'not executable')
    with pytest.raises(AnalysisError):discover_tools(source,{'freecadcmd':external})
