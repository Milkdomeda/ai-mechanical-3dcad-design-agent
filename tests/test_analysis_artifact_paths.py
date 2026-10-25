import hashlib

import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_ordinary_unicode_root_and_secure_normalized_root_share_exact_artifact_identity(tmp_path):
    from mechanical_design_agent import secure_fs
    from mechanical_design_agent.analysis_revision import read_artifact,inventory
    root=tmp_path/'run 中文';root.mkdir();content='fixed UTF-8 数据'.encode()
    path=root/'data 中文.json';path.write_bytes(content)
    ref={'relative_path':path.name,'sha256':hashlib.sha256(content).hexdigest()}
    canonical=secure_fs.validate_managed_path(root,allow_missing_leaf=False).path
    assert read_artifact(root,ref).content==read_artifact(canonical,ref).content==content
    assert inventory(root)==inventory(canonical)==[ref]
    path.write_bytes(b'replaced')
    with pytest.raises(AnalysisError):read_artifact(root,ref)
