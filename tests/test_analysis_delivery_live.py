"""Static verification of a real already visually reviewed synthetic bundle."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


@pytest.mark.live_fea
def test_real_bundle_is_portable_and_each_changed_core_artifact_invalidates_it(tmp_path):
    selected=os.environ.get('FEA_DELIVERY_LIVE_ROOT')
    if not selected:pytest.skip('requires explicit real synthetic delivery after actual assistant visual inspection')
    from mechanical_design_agent.analysis_delivery import verify_completion
    source=Path(selected)
    reference={'relative_path':'completion.json','sha256':hashlib.sha256((source/'completion.json').read_bytes()).hexdigest()}
    original=verify_completion(source,reference)
    copied=tmp_path/'portable bundle 中文';shutil.copytree(source,copied)
    checked=verify_completion(copied,reference)
    assert checked['manifest']==original['manifest']
    files=checked['manifest']['artifacts']
    selected=['report.md',next(name for name in files if name.endswith('/von_mises-figure.png')),next(name for name in files if name.endswith('/analysis.FCStd')),next(name for name in files if name.endswith('/Mesh.frd')),'completion.json']
    for name in selected:
        path=copied/name;content=path.read_bytes();path.write_bytes(content+b'\nsynthetic integrity fault')
        with pytest.raises(AnalysisError):verify_completion(copied,reference)
        path.write_bytes(content)
    assert verify_completion(copied,reference)['result']['numerical_status']=='passed'
    assert hashlib.sha256((source/'completion.json').read_bytes()).hexdigest()==reference['sha256']
