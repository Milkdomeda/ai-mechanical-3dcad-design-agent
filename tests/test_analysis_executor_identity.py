from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_executor_identity_covers_fixed_native_resources_and_excludes_caches():
    from mechanical_design_agent.analysis_executor_identity import executor_identity
    result=executor_identity()
    names={row['relative_path'] for row in result['record']['files']}
    assert 'analysis_worker.py' in names
    assert 'resources/freecad/analysis_build.py' in names
    assert 'resources/analysis/schemas/evaluation-policy.schema.json' in names
    assert not any('__pycache__' in name or name.endswith('.pyc') for name in names)
    assert len(result['sha256'])==64
    assert executor_identity()['sha256']==result['sha256']


def test_changed_execution_code_cannot_pass_a_later_integrity_checkpoint(tmp_path,monkeypatch):
    from mechanical_design_agent import analysis_executor_identity as module
    root=tmp_path/'package';root.mkdir();source=root/'worker.py';source.write_text('version one')
    monkeypatch.setattr(module,'_package_root',lambda:root)
    first=module.executor_identity()
    source.write_text('version two')
    with pytest.raises(AnalysisError) as error:module.verify_executor_identity(first['sha256'])
    assert error.value.code=='ANALYSIS_EXECUTOR_CHANGED'
