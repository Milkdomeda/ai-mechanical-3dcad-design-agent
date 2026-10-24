"""Native NTFS analysis storage contracts; no FreeCAD process is launched."""
import hashlib
import shutil
import subprocess
import sys

import pytest

from test_analysis_store import design
from test_windows_secure_fs import native_ntfs_roots

pytestmark=pytest.mark.skipif(sys.platform!='win32',reason='native Windows analysis store contract')


def test_native_analysis_snapshot_is_readonly_and_scoped_on_both_ntfs_volumes(native_ntfs_roots):
    import win32file
    from mechanical_design_agent.analysis_store import AnalysisStore
    for parent in native_ntfs_roots:
        workspace=parent/'analysis workspace 中文';workspace.mkdir()
        root,source,sha=design(workspace);store=AnalysisStore(workspace)
        state=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
        snapshot=root/'analyses/static'/state['source']['snapshot_ref']['relative_path']
        assert snapshot.read_bytes()==source.read_bytes()
        assert win32file.GetFileAttributes(str(snapshot))&1
        assert store.currentness('fixture-design','static')['currentness']=='current'
        updated=store.compare_and_replace(design_id='fixture-design',analysis_id='static',expected_revision=0,next_state={**state,'status':'needs_input'})
        assert updated['state_revision']==1


def test_native_analysis_rejects_source_and_snapshot_junctions(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    from mechanical_design_agent.analysis_types import AnalysisError
    workspace=tmp_path/'analysis 中文';workspace.mkdir()
    root,source,sha=design(workspace);store=AnalysisStore(workspace)
    foreign=tmp_path/'foreign';foreign.mkdir();(foreign/'part.FCStd').write_bytes(source.read_bytes())
    junction=root/'output/alias-dir'
    linked=subprocess.run(['cmd','/c','mklink','/J',str(junction),str(foreign)],capture_output=True,text=True)
    assert linked.returncode==0,linked.stderr
    try:
        with pytest.raises(AnalysisError):store.prepare_source(design_id='fixture-design',analysis_id='blocked',source_path=junction/'part.FCStd',expected_sha256=sha)
        assert not (root/'analyses/blocked').exists()
    finally:junction.rmdir()
    store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    first=root/'analyses/static/source'/sha[:2]
    foreign_snapshot=tmp_path/'foreign snapshot';shutil.copytree(first,foreign_snapshot)
    first.rename(first.parent/'preserved')
    linked=subprocess.run(['cmd','/c','mklink','/J',str(first),str(foreign_snapshot)],capture_output=True,text=True)
    assert linked.returncode==0,linked.stderr
    try:assert store.currentness('fixture-design','static')['currentness']=='artifact_integrity_failed'
    finally:first.rmdir()
    assert (foreign/'part.FCStd').read_bytes()==source.read_bytes()
