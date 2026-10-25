"""Compose sealed numerical and native GUI evidence; never claim vision."""
import hashlib
import json
import os
from pathlib import Path

from . import secure_fs
from .analysis_budget import BudgetGuard
from .analysis_configuration import load_analysis_configuration
from .analysis_executor_identity import verify_executor_identity
from .analysis_gui_service import AnalysisGuiService
from .analysis_pipeline import execute_attempt
from .analysis_revision import read_artifact,read_frozen_revision
from .analysis_runs import read_run,run_root
from .analysis_types import AnalysisError
from .analysis_validation import evaluate_convergence,evaluate_criteria
from .bootstrap_runtime import BootstrapRuntime


def verify_execution(store,design_id,analysis_id,run_id,reference,*,allow_unpublished=False):
    root=run_root(store,design_id,analysis_id,run_id);run,request=read_run(store,design_id,analysis_id,run_id)
    if reference['relative_path']!='execution.json':raise AnalysisError('ANALYSIS_EXECUTION_UNVERIFIED','only the registered sealed execution record may be used')
    if run.get('execution_ref')!=reference and not (allow_unpublished and run['worker'] is not None and run['worker']['pid']==os.getpid()):raise AnalysisError('ANALYSIS_EXECUTION_UNVERIFIED','execution evidence is not selected by its living owner or paused run')
    execution=json.loads(read_artifact(root,reference).content)
    if execution.get('schema_version')!='AnalysisExecutionEvidence/v1' or execution.get('request_sha256')!=run['request_ref']['sha256'] or execution.get('spec_sha256')!=request['spec_sha256'] or execution.get('attempt_ref')!=run.get('attempt_ref'):raise AnalysisError('ANALYSIS_EXECUTION_UNVERIFIED','execution inputs differ from the exact immutable request')
    gui=AnalysisGuiService(store);envelope=execution['gui_request']
    registered,analysis,gui_root,folder,revision,spec=gui._registered(envelope)
    if any(registered[key]!=request[key] for key in ('design_id','analysis_id','run_id','spec_sha256')) or registered['attempt_ref']!=execution['attempt_ref']:raise AnalysisError('ANALYSIS_EXECUTION_UNVERIFIED','GUI registration belongs to another run or numerical attempt')
    checked=gui.verify_receipt(envelope,execution['gui_receipt_ref'])
    attempt_root,attempt=gui._attempt(design_id,analysis_id,run_id,execution['attempt_ref'],complete=False)
    numerical=evaluate_convergence(spec,attempt['levels']);criteria=evaluate_criteria(spec,numerical)
    if numerical!=attempt['numerical'] or criteria!=attempt['criteria'] or numerical['numerical_status']!='passed' or numerical['applicability_status']=='outside_scope':raise AnalysisError('ANALYSIS_EXECUTION_UNVERIFIED','numerical convergence, scope or engineering criteria differ from sealed evidence')
    return {'run':run,'request':request,'root':root,'execution':execution,'attempt_root':attempt_root,'attempt':attempt,'gui_root':gui_root,'gui':checked['receipt'],'revision_root':folder,'revision':revision,'spec':spec}


def execution_settings(context,*,checkpoint=None):
    identity=context._identity()
    folder,revision=read_frozen_revision(context.store,identity['design_id'],identity['analysis_id'],context.request['revision_ref'])
    tools=json.loads(read_artifact(folder,revision['artifacts']['tools']).content)
    environment={**os.environ,'MECH_DESIGN_FEA_CONFIG':tools['configuration_path']}
    runtime=BootstrapRuntime.from_process(cwd=context.store.workspace,environ=environment,workspace=context.store.workspace)
    settings=load_analysis_configuration(runtime,checkpoint=checkpoint)
    if settings['configuration_sha256']!=tools['configuration_sha256']:raise AnalysisError('ANALYSIS_TOOL_CONSTRAINT_CHANGED','the exact approved configuration changed before execution')
    return settings


def execution_guard(context):
    identity=context._identity();analysis=context.store._root(identity['design_id'],identity['analysis_id'])
    spec=json.loads(read_artifact(analysis,context.request['spec_ref']).content)
    return BudgetGuard(spec['budget'],roots=lambda:[context.root],cancelled=context.cancelled,memory_observer=context.controller.observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(context.request['executor_sha256']))


def execute_run(context,*,settings=None,guard=None):
    """Keep one owner deadline across verification, native solve and GUI."""
    identity=context._identity()
    guard=guard or execution_guard(context)
    guard.checkpoint(force=True)
    settings=settings or execution_settings(context,checkpoint=guard.checkpoint)
    attempt=execute_attempt(context.request,settings=settings,control=context.controller,cancelled=context.cancelled,phase=context.phase,guard=guard)
    context.publish_attempt(attempt['attempt_ref'])
    if attempt['status']!='awaiting_visual_review':
        return {key:attempt[key] for key in ('status','numerical_status','criteria_status','applicability_status')}
    gui=AnalysisGuiService(context.store,configuration_loader=lambda:settings,controller=context.controller,guard=guard)
    envelope=gui.issue_request(design_id=identity['design_id'],analysis_id=identity['analysis_id'],kind='final_result',spec_sha256=context.request['spec_sha256'],run_id=identity['run_id'])
    receipt=gui.execute_request(envelope);guard.checkpoint(force=True)
    execution={'schema_version':'AnalysisExecutionEvidence/v1','request_sha256':secure_fs.read_managed_file(context.root/'request.json').sha256,'spec_sha256':context.request['spec_sha256'],'attempt_ref':attempt['attempt_ref'],'gui_request':envelope,'gui_receipt_ref':receipt['receipt_ref'],'budget':guard.receipt(),'visual_review':'not_executed','completion':'not_published'}
    content=json.dumps(execution,ensure_ascii=True,sort_keys=True,allow_nan=False).encode();secure_fs.atomic_publish_new(context.root/'execution.json',content)
    reference={'relative_path':'execution.json','sha256':hashlib.sha256(content).hexdigest()}
    guard.checkpoint(force=True)
    verify_execution(context.store,**identity,reference=reference,allow_unpublished=True)
    return {'status':'awaiting_visual_review','numerical_status':attempt['numerical_status'],'criteria_status':attempt['criteria_status'],'applicability_status':attempt['applicability_status'],'execution_ref':reference,'worker':None}
