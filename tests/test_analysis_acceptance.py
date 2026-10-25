"""Lifecycle-only fixtures; completion verification is stubbed, never native proof."""
import hashlib
import json

import pytest

from mechanical_design_agent import secure_fs
from mechanical_design_agent.analysis_delivery_service import AnalysisDeliveryService
from mechanical_design_agent.analysis_protocol import canonical_record
from mechanical_design_agent.analysis_runs import change_run,read_run,run_root
from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_coordinator import setup,submit


def completed(tmp_path,monkeypatch):
    root,_,store,_,approved,coordinator,_=setup(tmp_path)
    queued=submit(coordinator,approved);identity={'design_id':'fixture-design','analysis_id':'static','run_id':queued['run_id']}
    folder=run_root(store,**identity);reference={'relative_path':'stub-completion.json','sha256':'b'*64}
    change_run(store,**identity,changes={'status':'completed','completion_ref':reference,'execution_ref':{'relative_path':'stub-execution.json','sha256':'c'*64}},message='Synthetic metadata fixture; not a native completion.')
    analysis=store._root('fixture-design','static');state=store.read_state('fixture-design','static')
    state.update(status='completed',active_run_id=None,latest_delivery='a'*64)
    secure_fs.atomic_replace(analysis/'analysis.json',canonical_record(state,schema_name='session'))
    monkeypatch.setattr('mechanical_design_agent.analysis_delivery_service.verify_completion',lambda *args:{'completion':{'manifest_ref':{'sha256':'a'*64},'delivery_version':1},'result':{},'directory':str(folder)})
    return root,store,identity,folder,AnalysisDeliveryService(store),state


def test_exact_manifest_acceptance_has_immutable_scoped_receipt_and_no_cad_side_effect(tmp_path,monkeypatch):
    root,store,identity,folder,delivery,state=completed(tmp_path,monkeypatch)
    cad=(root/'design.json').read_bytes();run=(folder/'run.json').read_bytes()
    response=delivery.confirm(**identity,manifest_sha256='a'*64,decision_text='确认',expected_revision=state['state_revision'])
    assert response['acceptance']=='accepted' and not response['engineering_certification']
    analysis=store._root('fixture-design','static');receipt_path=analysis/response['acceptance_ref']['relative_path'];data=receipt_path.read_bytes()
    assert hashlib.sha256(data).hexdigest()==response['acceptance_ref']['sha256']
    from mechanical_design_agent.workspace_bootstrap import read_workspace_manifest
    receipt=json.loads(data);assert receipt['actor_id']==read_workspace_manifest(store.workspace).actor_id and receipt['scope']
    assert delivery.get_report(**identity)['acceptance']=='accepted'
    assert (root/'design.json').read_bytes()==cad and (folder/'run.json').read_bytes()==run
    assert not list(root.glob('**/lessons/*.json'))
    # A new presentation does not inherit acceptance of the old manifest.
    monkeypatch.setattr('mechanical_design_agent.analysis_delivery_service.verify_completion',lambda *args:{'completion':{'manifest_ref':{'sha256':'c'*64},'delivery_version':2},'result':{},'directory':str(folder)})
    assert delivery.get_report(**identity)['acceptance']=='not_requested'
    assert receipt_path.read_bytes()==data


@pytest.mark.parametrize('case',['wrong_manifest','stale_revision','source_changed','conditional'])
def test_unconfirmed_or_stale_report_cannot_mutate_acceptance(tmp_path,monkeypatch,case):
    root,store,identity,folder,delivery,state=completed(tmp_path,monkeypatch)
    analysis=store._root('fixture-design','static');before=(analysis/'analysis.json').read_bytes()
    if case=='source_changed':
        monkeypatch.setattr(store,'currentness_for_source',lambda *args:{'snapshot_integrity':'passed','currentness':'source_changed'})
    values=dict(manifest_sha256='d'*64 if case=='wrong_manifest' else 'a'*64,decision_text='确认，但还需要更改载荷' if case=='conditional' else '确认',expected_revision=state['state_revision']-1 if case=='stale_revision' else state['state_revision'])
    if case=='conditional':assert delivery.confirm(**identity,**values)['acceptance']!='accepted'
    else:
        with pytest.raises(AnalysisError):delivery.confirm(**identity,**values)
    assert (analysis/'analysis.json').read_bytes()==before
    assert not (analysis/'acceptances').exists()


def test_completion_recovery_repairs_only_selected_metadata_and_authorizes_first(tmp_path,monkeypatch):
    _,store,identity,folder,delivery,state=completed(tmp_path,monkeypatch)
    analysis=store._root('fixture-design','static');state.update(status='running',active_run_id=identity['run_id'],latest_delivery=None)
    secure_fs.atomic_replace(analysis/'analysis.json',canonical_record(state,schema_name='session'));run=(folder/'run.json').read_bytes()
    response=delivery.finalize_visual(**identity,evidence_refs=[],expected_revision=state['state_revision'])
    assert response['status']=='completed' and response['manifest_sha256']=='a'*64
    assert store.read_state('fixture-design','static')['active_run_id'] is None
    assert (folder/'run.json').read_bytes()==run


def test_missing_presentation_head_never_silently_selects_an_older_completion(tmp_path,monkeypatch):
    _,store,identity,folder,delivery,state=completed(tmp_path,monkeypatch)
    for name in ('v000001-first','v000002-later'):
        target=folder/'deliveries'/name;target.mkdir(parents=True)
        (target/'completion.json').write_text('{}',encoding='utf-8')
    before=(folder/'run.json').read_bytes()
    with pytest.raises(AnalysisError) as error:delivery.get_report(**identity)
    assert error.value.code=='ANALYSIS_DELIVERY_INTEGRITY'
    assert (folder/'run.json').read_bytes()==before


def test_presentation_head_rejects_duplicate_json_keys(tmp_path,monkeypatch):
    _,_,identity,folder,delivery,_=completed(tmp_path,monkeypatch)
    run,_=read_run(delivery.store,**identity)
    head={'schema_version':'AnalysisDeliveryHead/v1','run_id':identity['run_id'],'completion_ref':run['completion_ref'],'manifest_sha256':'a'*64}
    text=json.dumps(head).replace('"run_id":', '"run_id":"foreign", "run_id":')
    (folder/'delivery-head.json').write_text(text,encoding='utf-8')
    monkeypatch.setattr('mechanical_design_agent.analysis_delivery_service._load',lambda *args:{'manifest_ref':{'sha256':'a'*64},'run_id':identity['run_id'],'spec_sha256':run['spec_sha256']})
    with pytest.raises(AnalysisError) as error:delivery.get_report(**identity)
    assert error.value.code=='ANALYSIS_DELIVERY_INTEGRITY'


@pytest.mark.parametrize('crash',[False,True])
def test_presentation_revision_preserves_old_run_and_acceptance_and_recovers_summary_crash(tmp_path,monkeypatch,crash):
    """Isolate the publication transaction with a stub renderer/verifier."""
    from mechanical_design_agent.analysis_revision import read_frozen_revision,read_artifact
    from mechanical_design_agent.analysis_store import _timestamp
    _,store,identity,folder,delivery,state=completed(tmp_path,monkeypatch)
    delivery.confirm(**identity,manifest_sha256='a'*64,decision_text='确认',expected_revision=state['state_revision'])
    state=store.read_state('fixture-design','static');run,request=read_run(store,**identity)
    revision_root,revision=read_frozen_revision(store,'fixture-design','static',request['revision_ref'])
    spec=json.loads(read_artifact(revision_root,revision['artifacts']['spec']).content)
    context={'run':run,'request':request,'root':folder,'state':state,'spec':spec,'execution':{'budget':{'wall_seconds':0}}}
    old_run=(folder/'run.json').read_bytes();old_acceptance=state['acceptance_ref'];analysis=store._root('fixture-design','static')
    old_receipt=(analysis/old_acceptance['relative_path']).read_bytes()
    monkeypatch.setattr(delivery,'_images',lambda *args,**kwargs:[{'relative_path':'gui/registered.png','sha256':'1'*64}])
    def build(stage,ctx,version,**kwargs):
        completion={'schema_version':'AnalysisCompletion/v1',**identity,'spec_sha256':request['spec_sha256'],'status':'complete','manifest_ref':{'relative_path':'manifest.json','sha256':'d'*64},'result_ref':{'relative_path':'report.json','sha256':'e'*64},'delivery_version':version,'completed_at':_timestamp()}
        secure_fs.atomic_publish_new(stage/'completion.json',canonical_record(completion,schema_name='completion'))
    monkeypatch.setattr(delivery,'_build',build)
    monkeypatch.setattr('mechanical_design_agent.analysis_delivery_service.verify_completion',lambda *args:{'completion':{'manifest_ref':{'sha256':'d'*64},'delivery_version':1},'result':{},'directory':str(folder)})
    replace=secure_fs.atomic_replace
    def fault(path,*args,**kwargs):
        if path==analysis/'analysis.json':raise OSError('Synthetic crash after immutable directory and head publication.')
        return replace(path,*args,**kwargs)
    if crash:monkeypatch.setattr(secure_fs,'atomic_replace',fault)
    if crash:
        with pytest.raises(AnalysisError) as error:delivery._publish(context,['gui/registered.png'],state['state_revision'],revising=True)
        assert error.value.code=='ANALYSIS_REPORT_PUBLICATION_FAILED'
        monkeypatch.setattr(secure_fs,'atomic_replace',replace)
        response=delivery.finalize_visual(**identity,evidence_refs=[],expected_revision=state['state_revision'])
    else:response=delivery._publish(context,['gui/registered.png'],state['state_revision'],revising=True)
    assert response['manifest_sha256']=='d'*64 and response['acceptance']=='not_requested'
    assert (folder/'run.json').read_bytes()==old_run
    assert (analysis/old_acceptance['relative_path']).read_bytes()==old_receipt
    selected=store.read_state('fixture-design','static')
    assert selected['latest_delivery']=='d'*64 and selected['accepted_delivery']=='a'*64
