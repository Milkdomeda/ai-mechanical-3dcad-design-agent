import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_manifest_graph_rejects_cycles_unknown_targets_and_its_own_hash():
    from mechanical_design_agent.analysis_delivery import validate_dependency_graph
    artifacts={'report.json':'a'*64,'report.md':'b'*64,'visual.json':'c'*64}
    assert validate_dependency_graph(artifacts,{'report.md':['report.json'],'report.json':['visual.json']})['status']=='passed'
    for files,deps in [(artifacts,{'report.json':['report.md'],'report.md':['report.json']}),
                       (artifacts,{'report.json':['unknown.json']}),
                       ({**artifacts,'manifest.json':'d'*64},{}),
                       ({**artifacts,'completion.json':'d'*64},{}),
                       ({'../escape':'a'*64},{}),
                       ({'safe//alias':'a'*64},{}),
                       ({'file:stream':'a'*64},{})]:
        with pytest.raises(AnalysisError):validate_dependency_graph(files,deps)


def test_incomplete_completion_never_reads_as_completed(tmp_path):
    from mechanical_design_agent.analysis_delivery import verify_completion
    with pytest.raises((AnalysisError,FileNotFoundError)):
        verify_completion(tmp_path,{'relative_path':'completion.json','sha256':'a'*64})
    assert not (tmp_path/'manifest.json').exists()


def test_report_failure_keeps_the_paused_run_and_never_calls_a_solver(tmp_path,monkeypatch):
    """Stub domain context isolates publication failure, not native proof."""
    import hashlib
    import json
    from mechanical_design_agent.analysis_delivery_service import AnalysisDeliveryService
    from mechanical_design_agent.analysis_runs import change_run,read_run,run_root
    from mechanical_design_agent.analysis_revision import read_artifact,read_frozen_revision
    from mechanical_design_agent import analysis_solver
    from test_analysis_coordinator import setup,submit
    root,service,store,request,approved,coordinator,launcher=setup(tmp_path)
    queued=submit(coordinator,approved);identity={'design_id':'fixture-design','analysis_id':'static','run_id':queued['run_id']}
    folder=run_root(store,**identity);data=b'unit context, not real execution evidence';(folder/'stub.json').write_bytes(data)
    change_run(store,**identity,changes={'status':'awaiting_visual_review','worker':None,'execution_ref':{'relative_path':'stub.json','sha256':hashlib.sha256(data).hexdigest()}},message='Synthetic publisher-only unit fixture.')
    run,frozen=read_run(store,**identity);revision_root,revision=read_frozen_revision(store,'fixture-design','static',frozen['revision_ref'])
    spec=json.loads(read_artifact(revision_root,revision['artifacts']['spec']).content)
    state=store.read_state('fixture-design','static')
    delivery=AnalysisDeliveryService(store)
    monkeypatch.setattr(delivery,'_pending',lambda *args:{'run':run,'request':frozen,'root':folder,'spec':spec,'state':state,'execution':{'budget':{'wall_seconds':0}}})
    images=[{'relative_path':'gui/fixed-image.png','sha256':'a'*64}]
    monkeypatch.setattr(delivery,'_images',lambda *args,**kwargs:images)
    solver_calls=[];monkeypatch.setattr(analysis_solver,'solve_native',lambda *args,**kwargs:solver_calls.append(1))
    builds=[]
    def failed(stage,*args,**kwargs):builds.append(stage);raise OSError('Synthetic report serializer failure.')
    monkeypatch.setattr(delivery,'_build',failed)
    before=(folder/'run.json').read_bytes()
    for _ in range(2):
        with pytest.raises(AnalysisError) as error:delivery.finalize_visual(**identity,evidence_refs=['gui/fixed-image.png'],expected_revision=state['state_revision'])
        assert error.value.code=='ANALYSIS_REPORT_PUBLICATION_FAILED'
    assert len(builds)==2 and solver_calls==[]
    assert (folder/'run.json').read_bytes()==before
    assert not list(folder.glob('deliveries/v*/completion.json'))
