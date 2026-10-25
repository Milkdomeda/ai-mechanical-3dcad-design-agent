"""Worker metadata tests with a stub verifier; not numerical/vision proof."""
import hashlib

from test_analysis_coordinator import setup,submit


def test_verified_pause_releases_queue_and_can_be_cancelled_without_another_worker(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    from mechanical_design_agent.analysis_queue import scheduler_guard
    from mechanical_design_agent import analysis_execution
    root,service,store,request,approved,coordinator,launcher=setup(tmp_path)
    before=(root/'design.json').read_bytes();queued=submit(coordinator,approved)
    with scheduler_guard(store.workspace) as scheduler:claim=scheduler.claim()['worker_uuid']
    calls=[]
    monkeypatch.setattr(analysis_execution,'verify_execution',lambda *args,**kwargs:calls.append('verify'))
    def execute(context):
        data=b'unit orchestration stub; no numerical or visual proof'
        (context.root/'synthetic-execution.json').write_bytes(data)
        return {'status':'awaiting_visual_review','worker':None,'execution_ref':{'relative_path':'synthetic-execution.json','sha256':hashlib.sha256(data).hexdigest()}}
    assert AnalysisSupervisor(store,claim,execute=execute).run()==0
    observed=coordinator.status(design_id='fixture-design',analysis_id='static')
    assert observed['run_status']=='awaiting_visual_review' and observed['worker_state']=='idle'
    assert observed['numerical_status']=='not_evaluated'  # stub does not invent a solve
    repeated=coordinator.submit(design_id='fixture-design',analysis_id='static',spec_sha256=approved['spec_sha256'],idempotency_key='request-1',expected_revision=observed['state_revision'])
    assert repeated['run_id']==queued['run_id'] and repeated['run_status']=='awaiting_visual_review'
    assert calls==['verify'] and len(launcher.calls)==1
    cancelled=coordinator.cancel(design_id='fixture-design',analysis_id='static',run_id=queued['run_id'],reason='Stop synthetic paused orchestration fixture.',expected_revision=observed['state_revision'])
    assert cancelled['run_status']=='cancelled'
    assert store.read_state('fixture-design','static')['active_run_id'] is None
    assert (root/'design.json').read_bytes()==before


def test_unverified_pause_never_becomes_reviewable(tmp_path):
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    from mechanical_design_agent.analysis_queue import scheduler_guard
    root,service,store,request,approved,coordinator,launcher=setup(tmp_path)
    queued=submit(coordinator,approved)
    with scheduler_guard(store.workspace) as scheduler:claim=scheduler.claim()['worker_uuid']
    assert AnalysisSupervisor(store,claim,execute=lambda context:{'status':'awaiting_visual_review','execution_ref':{'relative_path':'invented.json','sha256':'a'*64}}).run()==0
    observed=coordinator.status(design_id='fixture-design',analysis_id='static')
    assert observed['run_status']=='failed'
    assert observed['diagnostic']['code']=='ANALYSIS_EXECUTION_UNVERIFIED'
