"""Installed-wheel acceptance on fixed synthetic inputs only.

Copy this standalone harness outside the checkout and run its installed Python.
There are no test imports, mock native producers, automatic image-review claims
or customer engineering approvals. It stops for actual assistant image review.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def progress(stage,**details):
    # Bounded synthetic CI diagnostics; never claim image review or approval.
    print(json.dumps({"stage":stage,**details},ensure_ascii=True,allow_nan=False),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--workspace',type=Path,required=True);args=parser.parse_args()
    import mechanical_design_agent
    from mechanical_design_agent import secure_fs
    from mechanical_design_agent.analysis_configuration import load_analysis_configuration
    from mechanical_design_agent.analysis_executor_identity import executor_identity
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_qualification import require_qualification
    from mechanical_design_agent.analysis_qualification_inputs import make_inputs,q
    from mechanical_design_agent.analysis_service import AnalysisService
    from mechanical_design_agent.analysis_runs import read_run
    from mechanical_design_agent.analysis_store import AnalysisStore
    from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
    from mechanical_design_agent.design_session import DesignSessionService
    from mechanical_design_agent.workspace_bootstrap import read_workspace_manifest
    package=Path(mechanical_design_agent.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):raise RuntimeError('installed package must live inside this clean virtual environment')
    workspace=secure_fs.validate_managed_path(args.workspace,allow_missing_leaf=False).path
    manifest=read_workspace_manifest(workspace)
    if manifest.actor_id!='fixture-engineer' or manifest.raw['identity']['organization_id']!='synthetic-org' or manifest.raw['identity']['design_group_id']!='synthetic-group':raise RuntimeError('only the explicitly disposable synthetic acceptance workspace is permitted')
    runtime=BootstrapRuntime.from_process(cwd=workspace,environ=dict(os.environ),workspace=workspace)
    progress("verify_installed_qualification")
    settings=load_analysis_configuration(runtime);qualification=require_qualification(settings)
    progress("installed_qualification_verified")
    qualified=Path(qualification['qualification_ref']['path']).parent
    fixture=qualified/'fixtures/box.FCStd'
    design_id='software-qualification-e2e';analysis_id='static';job=workspace/'designs'/design_id
    if job.exists():raise RuntimeError('never reuse or overwrite an existing synthetic acceptance job')
    native_runtime=BootstrapRuntime.from_process(cwd=workspace,environ={**os.environ,'MECH_DESIGN_FREECADCMD':settings['tools']['freecadcmd']['path'],'MECH_DESIGN_FREECADCMD_SHA256':settings['tools']['freecadcmd']['sha256']},workspace=workspace)
    design=DesignSessionService(native_runtime.design_settings())
    progress("normalize_synthetic_source")
    design.start(design_id=design_id,title='Installed FEA synthetic software acceptance',model_classification='existing_model',requirements={'software_fixture':True,'units':'mm'},proposal_summary='Only the fixed synthetic box from this exact installation qualification; no product engineering decision.',approval_text='approved',source_path=str(fixture))
    source_dir=secure_fs.ensure_managed_directory(job/'output',parents=False,exist_ok=True).path
    model=secure_fs.read_managed_file(job/'model.FCStd');source=source_dir/'software-fixture.FCStd';secure_fs.atomic_publish_new(source,model.content)
    before=secure_fs.read_managed_file(job/'design.json').content
    geometry_request={'schema_version':'AnalysisGeometryRequest/v1','source_sha256':model.sha256,'included_object':'Solid','excluded_objects':[],'partitions':[],'volume_tolerance':q('.000001','mm^3')}
    progress("discover_synthetic_selectors")
    geometry=inspect_geometry(source,geometry_request,settings,workspace/'e2e-selector-discovery')
    spec,regions=make_inputs(geometry,settings)
    spec.update(design_id=design_id,analysis_id=analysis_id,status='draft',source=None,tool_constraints=None)
    request={'schema_version':'AnalysisPrepareRequest/v1','source_path':str(source),'expected_source_sha256':model.sha256,'draft_spec':spec,'geometry_request':geometry_request,'region_selectors':[item['selector'] for item in regions['bindings']],'knowledge':{'query':'Fixed installed synthetic axial software check','required_ids':[],'requested_family_id':None}}
    def no_knowledge(*args):raise RuntimeError('No knowledge service is used by this synthetic software fixture.')
    service=AnalysisService(runtime,no_knowledge)
    progress("prepare_review_and_native_preview")
    prepared=service.prepare(design_id=design_id,analysis_id=analysis_id,request=request,expected_revision=None)
    if prepared['spec_status']!='ready_for_review':raise RuntimeError('native synthetic preparation did not reach review: '+json.dumps(prepared['issues']))
    # The deliberately named fixture actor approves only the fixed benchmark.
    progress("approve_fixed_software_benchmark")
    approved=service.approve(design_id=design_id,analysis_id=analysis_id,spec_sha256=prepared['spec_sha256'],decision_text='确认',expected_revision=prepared['state_revision'])
    progress("queue_default_installed_executor")
    queued=service.run(design_id=design_id,analysis_id=analysis_id,spec_sha256=approved['spec_sha256'],idempotency_key='installed-synthetic-software-acceptance-v1',expected_revision=approved['state_revision'])
    store=AnalysisStore(workspace);identity={'design_id':design_id,'analysis_id':analysis_id,'run_id':queued['run_id']};deadline=time.monotonic()+spec['budget']['wall_seconds']+120;cursor=0;owner=None
    last_progress=0.;last_status=None
    while time.monotonic()<deadline:
        status=service.status(**identity,cursor=cursor);cursor=status['cursor'];run,_=read_run(store,**identity)
        if run['worker'] is not None:owner=run['worker']
        now=time.monotonic()
        if status['run_status']!=last_status or now-last_progress>=30:
            progress("default_worker_status",run_id=identity['run_id'],run_status=status['run_status'],cursor=cursor)
            last_progress=now;last_status=status['run_status']
        if status['run_status'] in ('completed','failed','cancelled','timed_out','interrupted'):break
        if status['run_status']=='awaiting_visual_review' and run['worker'] is None and run.get('execution_ref') is not None:break
        time.sleep(1)
    else:raise RuntimeError('installed owner did not reach a bounded outcome')
    (workspace/'installed-e2e-status.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
    if status['run_status']!='awaiting_visual_review':raise RuntimeError('installed default execution did not pause for visual review: '+json.dumps(status))
    if owner is None or owner['pid']==os.getpid():raise RuntimeError('the detached installed supervisor ownership was not observed')
    progress("verify_pending_image_report")
    report=service.report(**identity)
    if len(report['images'])!=8 or run['completion_ref'] is not None:raise RuntimeError('the fixed two-case image gate must remain pending')
    if secure_fs.read_managed_file(job/'design.json').content!=before or secure_fs.read_managed_file(source).sha256!=model.sha256:raise RuntimeError('analysis changed CAD lifecycle or source bytes')
    result={'schema_version':'InstalledFEAAcceptance/v1','workspace':str(workspace),**identity,'status':'awaiting_visual_review','package_path':str(package),'package_version':mechanical_design_agent.__version__,'executor_sha256':executor_identity()['sha256'],'detached_owner':owner,'report':report,'source_unchanged':True,'cad_state_unchanged':True,'visual_review':'not_executed','customer_engineering_approval':'not_claimed','acceptance':'not_requested'}
    path=workspace/'installed-e2e-ready.json';path.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps({'status':result['status'],'report':str(path),'run_id':identity['run_id']}))


if __name__=='__main__':main()
