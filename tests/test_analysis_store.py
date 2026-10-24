from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from test_design_session import _fcstd,_service,_start


def design(tmp_path):
    service=_service(tmp_path);_start(service,design_id='fixture-design')
    root=tmp_path/'designs/fixture-design'
    model=root/'model.FCStd';output=root/'output/part.FCStd';output.write_bytes(model.read_bytes())
    return root,output,hashlib.sha256(output.read_bytes()).hexdigest()


def test_snapshot_does_not_touch_design_state_and_is_content_addressed(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path)
    before={name:(root/name).read_bytes() for name in ('design.json','model.FCStd','output/part.FCStd')}
    store=AnalysisStore(tmp_path)
    state=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    assert state['schema_version']=='AnalysisSession/v1' and state['state_revision']==0
    snapshot=root/'analyses/static'/state['source']['snapshot_ref']['relative_path']
    assert snapshot.name==sha+'.FCStd' and snapshot.read_bytes()==source.read_bytes()
    assert before=={name:(root/name).read_bytes() for name in before}
    assert store.currentness('fixture-design','static')['currentness']=='current'
    assert store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)==state


def test_unchanged_output_changed_design_is_stale_without_destroying_history(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path);store=AnalysisStore(tmp_path)
    state=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    (root/'model.FCStd').write_bytes(_fcstd(object_name='NewRevision'))
    observed=store.currentness('fixture-design','static')
    assert observed['currentness']=='source_changed' and observed['origin_unchanged'] is True
    assert observed['snapshot_integrity']=='passed'
    assert source.read_bytes()==(root/'analyses/static'/state['source']['snapshot_ref']['relative_path']).read_bytes()


def test_compare_and_replace_has_one_winner_and_cannot_rebind_source(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    from mechanical_design_agent.analysis_types import AnalysisError
    root,source,sha=design(tmp_path);store=AnalysisStore(tmp_path)
    state=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    candidate={**state,'status':'needs_input'}
    def update(_):
        try:return store.compare_and_replace(design_id='fixture-design',analysis_id='static',expected_revision=0,next_state=candidate)['state_revision']
        except AnalysisError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:outcomes=list(executor.map(update,range(2)))
    assert outcomes.count(1)==1
    assert set(outcomes)-{1}<={'ANALYSIS_REVISION_CONFLICT','ANALYSIS_BUSY'}
    current=store.read_state('fixture-design','static');assert current['state_revision']==1
    rebound=deepcopy(current);rebound['source']['origin_sha256']='b'*64
    with pytest.raises(AnalysisError) as caught:store.compare_and_replace(design_id='fixture-design',analysis_id='static',expected_revision=1,next_state=rebound)
    assert caught.value.code=='ANALYSIS_SOURCE_IMMUTABLE'


def test_wrong_source_digest_never_publishes_partial_session(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    from mechanical_design_agent.analysis_types import AnalysisError
    root,source,sha=design(tmp_path)
    with pytest.raises(AnalysisError):AnalysisStore(tmp_path).prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256='b'*64)
    assert not (root/'analyses/static').exists()
    assert not list(root.glob('analyses/.creating-*'))


def test_source_change_after_copy_cannot_publish_a_false_snapshot_binding(tmp_path,monkeypatch):
    from mechanical_design_agent import analysis_store as module
    from mechanical_design_agent.analysis_types import AnalysisError
    root,source,sha=design(tmp_path)
    original=module.secure_fs.ingest_cas_file
    def change(*args,**kwargs):
        result=original(*args,**kwargs)
        source.write_bytes(_fcstd(object_name='ChangedDuringCopy'))
        return result
    monkeypatch.setattr(module.secure_fs,'ingest_cas_file',change)
    with pytest.raises(AnalysisError) as caught:module.AnalysisStore(tmp_path).prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    assert caught.value.code=='ANALYSIS_SOURCE_CHANGED'
    assert not (root/'analyses/static').exists() and not list(root.glob('analyses/.creating-*'))


@pytest.mark.parametrize('unsafe_id',['../outside','/absolute','a/b',''])
def test_analysis_identifiers_cannot_escape_a_design(tmp_path,unsafe_id):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path)
    with pytest.raises(ValueError):AnalysisStore(tmp_path).prepare_source(design_id='fixture-design',analysis_id=unsafe_id,source_path=source,expected_sha256=sha)


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX symlink attack; Windows junction contract has a native counterpart')
def test_snapshot_ancestor_symlink_cannot_make_foreign_bytes_look_managed(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path);store=AnalysisStore(tmp_path)
    store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    first=root/'analyses/static/source'/sha[:2]
    foreign=tmp_path/'foreign';shutil.copytree(first,foreign)
    first.rename(first.parent/'preserved')
    first.symlink_to(foreign,target_is_directory=True)
    assert store.currentness('fixture-design','static')['currentness']=='artifact_integrity_failed'


def test_initial_output_that_disagrees_with_design_is_never_current(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path)
    source.write_bytes(_fcstd(object_name='OldOutput'));sha=hashlib.sha256(source.read_bytes()).hexdigest()
    store=AnalysisStore(tmp_path);state=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    assert state['status']=='needs_input'
    observation=store.currentness('fixture-design','static')
    assert observation['currentness']=='source_changed' and observation['origin_unchanged'] is True and observation['design_model_unchanged'] is True


def test_analysis_ids_are_scoped_to_designs_and_never_search_other_designs(tmp_path):
    from mechanical_design_agent.analysis_store import AnalysisStore
    root,source,sha=design(tmp_path)
    _start(_service(tmp_path),design_id='fixture-two')
    store=AnalysisStore(tmp_path)
    first=store.prepare_source(design_id='fixture-design',analysis_id='static',source_path=source,expected_sha256=sha)
    second=store.prepare_source(design_id='fixture-two',analysis_id='static',source_path=tmp_path/'designs/fixture-two/model.FCStd',expected_sha256=sha)
    assert first['source']['origin_path']!=second['source']['origin_path']
    assert store.read_state('fixture-design','static')['design_id']=='fixture-design'
    assert store.read_state('fixture-two','static')['design_id']=='fixture-two'
