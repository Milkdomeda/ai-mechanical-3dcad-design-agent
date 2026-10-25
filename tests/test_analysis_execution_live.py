"""Real governed synthetic run stops for actual external visual inspection.

No real engineering approval or automated vision claim is created here.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest


@pytest.mark.live_fea
def test_native_prepare_approval_queue_execution_and_gui_pause(native_product_session,monkeypatch):
    if os.environ.get('FEA_EXECUTION_LIVE')!='1':pytest.skip('requires explicit full isolated execution opt-in')
    from mechanical_design_agent.analysis_store import AnalysisStore
    from mechanical_design_agent.analysis_session import AnalysisSessionService
    from mechanical_design_agent.analysis_knowledge import AnalysisKnowledgeService
    from mechanical_design_agent.analysis_gui import preview_native
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_coordinator import AnalysisCoordinator
    from mechanical_design_agent.analysis_process import ProcessController
    from mechanical_design_agent.analysis_worker import AnalysisSupervisor
    from mechanical_design_agent.analysis_execution import execute_run,verify_execution
    from mechanical_design_agent.analysis_delivery_service import AnalysisDeliveryService
    from mechanical_design_agent.analysis_queue import scheduler_guard
    from mechanical_design_agent.analysis_runs import read_run
    from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
    from test_analysis_geometry_live import native_settings
    from test_analysis_mesh_live import mesh_inputs
    from test_analysis_deck_live import declared_spec
    from test_design_session import _service,_start
    directory,fixtures,base,native=native_product_session
    # The fixture owns the base W1 lock. The actual product supervisor uses
    # its independently private ASCII child root and all its real boundaries.
    workspace=directory/'execution workspace 中文'
    scratch=Path(os.environ['FEA_GUI_LIVE_SCRATCH'])/('execution-'+uuid.uuid4().hex)
    settings=native_settings(workspace,os.environ['FEA_GUI_LIVE_CONFIG'],scratch)
    monkeypatch.setenv('MECH_DESIGN_FEA_CONFIG',settings['configuration_path'])
    design_service=_service(workspace)
    _start(design_service,design_id='synthetic-axial',title='Synthetic axial software qualification',requirements={'software_fixture':True,'units':'mm'},proposal_summary='Generated homogeneous axial fixture for software qualification only.')
    job=workspace/'designs/synthetic-axial';source=job/'output/source.FCStd'
    original=(fixtures/'box.FCStd').read_bytes();(job/'model.FCStd').write_bytes(original);source.write_bytes(original)
    cad_before=(job/'design.json').read_bytes();source_sha=hashlib.sha256(original).hexdigest()
    geometry_request={'schema_version':'AnalysisGeometryRequest/v1','source_sha256':source_sha,'included_object':'Solid','excluded_objects':['Ball1','Ball2','Ball3','Ball4'],'partitions':[],'volume_tolerance':{'value':'.000001','unit':'mm^3'}}
    manifest=inspect_geometry(source,geometry_request,base,directory/'execution selector seed 中文',transaction=native)
    regions,levels=mesh_inputs(manifest,'box')
    spec=declared_spec({'geometry_manifest':manifest,'region_bindings':regions},levels[0],'force',settings)
    spec.update(design_id='synthetic-axial',analysis_id='static',status='draft',tool_constraints=None,source=None)
    spec['material']['youngs_modulus']['value']='200000';spec['material']['poisson_ratio']['value']='0'
    spec['cases'][0]['loads'][0]['components']=[{'value':str(v),'unit':'N'} for v in (40,0,0)]
    reverse=deepcopy(spec['cases'][0]);reverse['case_id']='reverse';reverse['loads'][0]['components'][0]['value']='-40';spec['cases'].append(reverse)
    for observation in deepcopy(spec['evaluation']['observables']):
        observation.update(id=observation['id']+'-reverse',case_id='reverse');spec['evaluation']['observables'].append(observation)
    spec['budget']['wall_seconds']=1200
    runtime=BootstrapRuntime.from_process(cwd=workspace,environ={},workspace=workspace)
    knowledge=AnalysisKnowledgeService(runtime,lambda *args:{'schema_version':'DesignContext/v2','approved_facts':[]})
    store=AnalysisStore(workspace)
    service=AnalysisSessionService(store,configuration_loader=lambda:settings,geometry_inspector=lambda ctx:inspect_geometry(Path(ctx['source_path']),ctx['request'],ctx['settings'],Path(ctx['output_dir'])),knowledge_loader=lambda ctx:knowledge.retrieve(**ctx),gui_previewer=preview_native)
    request={'schema_version':'AnalysisPrepareRequest/v1','source_path':str(source),'expected_source_sha256':source_sha,'draft_spec':spec,'geometry_request':geometry_request,'region_selectors':[item['selector'] for item in regions['bindings']],'knowledge':{'query':'synthetic axial software qualification','required_ids':[],'requested_family_id':None}}
    ready=service.prepare(design_id='synthetic-axial',analysis_id='static',request=request,expected_revision=None)
    assert ready['spec_status']=='ready_for_review',ready
    # Explicit synthetic test actor, never a customer/material approval.
    approved=service.approve(design_id='synthetic-axial',analysis_id='static',spec_sha256=ready['spec_sha256'],decision_text='确认',expected_revision=ready['state_revision'])
    assert approved['spec_status']=='approved',approved
    launches=[];coordinator=AnalysisCoordinator(store,ProcessController(),supervisor_launcher=lambda *args:launches.append(args))
    queued=coordinator.submit(design_id='synthetic-axial',analysis_id='static',spec_sha256=approved['spec_sha256'],idempotency_key='synthetic-full-native-run',expected_revision=approved['state_revision'])
    with scheduler_guard(workspace) as scheduler:claim=scheduler.claim()['worker_uuid']
    assert AnalysisSupervisor(store,claim,execute=execute_run).run()==0
    observed=coordinator.status(design_id='synthetic-axial',analysis_id='static',run_id=queued['run_id'])
    (directory/'execution-status.json').write_text(json.dumps(observed,indent=2),encoding='utf-8')
    assert observed['run_status']=='awaiting_visual_review',observed
    run,_=read_run(store,'synthetic-axial','static',queued['run_id'])
    context=verify_execution(store,'synthetic-axial','static',queued['run_id'],run['execution_ref'])
    assert len(context['attempt']['levels'])==6
    report=AnalysisDeliveryService(store).get_report('synthetic-axial','static',queued['run_id'])
    assert report['status']=='awaiting_visual_review' and len(report['images'])==8
    assert run['completion_ref'] is None
    assert (job/'design.json').read_bytes()==cad_before and source.read_bytes()==original
    output={'workspace':str(workspace),'design_id':'synthetic-axial','analysis_id':'static','run_id':queued['run_id'],'report':report,'qualification_scope':'real synthetic solve and GUI; assistant visual inspection and final publication still pending'}
    (directory/'execution-ready-for-visual.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
