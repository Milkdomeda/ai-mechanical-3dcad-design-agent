"""Fixed supervisor-only static execution; no arbitrary commands or CAD edits.

The result is provisional until native GUI evidence and the final publisher
verify it. This module never writes a completion marker or accepts delivery.
"""
from __future__ import annotations

from dataclasses import dataclass,field
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from . import secure_fs
from .analysis_budget import BudgetGuard
from .analysis_deck import build_native_deck
from .analysis_executor_identity import verify_executor_identity
from .analysis_geometry import validate_geometry_manifest,validate_region_bindings
from .analysis_mesh import generate_mesh,plan_mesh_levels
from .analysis_native import _read
from .analysis_native_transaction import NativeTransaction
from .analysis_protocol import canonical_record,parse_record,record_sha256
from .analysis_results import extract_fields
from .analysis_revision import read_artifact,read_frozen_revision,inventory
from .analysis_runs import read_run,run_root
from .analysis_runtime_patch import PROFILE
from .analysis_session import _tools
from .analysis_solver import solve_native
from .analysis_store import AnalysisStore
from .analysis_types import AnalysisError
from .analysis_validation import evaluate_convergence,evaluate_criteria,validate_evaluation_policy,validate_level
from .workspace_bootstrap import read_workspace_manifest


def _put(root,name,record):
    content=json.dumps(record,ensure_ascii=True,sort_keys=True,allow_nan=False).encode()
    secure_fs.atomic_publish_new(root/name,content)
    return {'relative_path':name,'sha256':hashlib.sha256(content).hexdigest()}


def _load_inputs(request,settings):
    request=parse_record(canonical_record(request,schema_name='run-request').decode(),schema_name='run-request')
    if 'executor_sha256' not in request:raise AnalysisError('ANALYSIS_EXECUTOR_UNBOUND','legacy beta metadata is historical only; create an explicit qualified retry')
    verify_executor_identity(request['executor_sha256'])
    store=AnalysisStore(Path(settings['workspace']))
    identity={key:request[key] for key in ('design_id','analysis_id','run_id')}
    run,stored=read_run(store,**identity)
    if stored!=request or run['worker'] is None or run['worker']['pid']!=os.getpid() or run['status'] not in ('preparing','meshing','solving','validating','awaiting_visual_review'):
        raise AnalysisError('ANALYSIS_EXECUTION_OWNER_UNPROVEN','only the authenticated living supervisor may execute its claimed request')
    workspace=read_workspace_manifest(store.workspace)
    if any(request['scope'][key]!=workspace.raw['identity'][key] for key in ('organization_id','design_group_id')):raise AnalysisError('ANALYSIS_AUTHORITY_CHANGED','workspace authority changed after queueing')
    folder,revision=read_frozen_revision(store,identity['design_id'],identity['analysis_id'],request['revision_ref'])
    def load(name,schema=None):
        text=read_artifact(folder,revision['artifacts'][name]).content.decode('utf-8')
        return parse_record(text,schema_name=schema) if schema else json.loads(text)
    spec=load('spec','spec');approval=load('approval','approval')
    if spec['status']!='approved' or record_sha256(spec,schema_name='spec')!=request['spec_sha256'] or approval['spec_sha256']!=request['spec_sha256'] or approval['policy_sha256']!=request['policy_sha256'] or approval['tool_bindings_sha256']!=request['tool_bindings_sha256'] or revision['source_binding_ref']!=request['source_binding_ref']:raise AnalysisError('ANALYSIS_EXECUTION_BINDING_CHANGED','frozen specification, approval or source differs from the claimed request')
    tools=load('tools')
    if tools!=_tools(settings,spec) or request['runtime_manifest_sha256']!=settings['runtime']['manifest_sha256'] or settings['configuration']['runtime']['profile']!=PROFILE:raise AnalysisError('ANALYSIS_TOOL_CONSTRAINT_CHANGED','exact reviewed R2 tool, configuration and numerical policy bindings are required')
    geometry=validate_geometry_manifest(load('geometry','geometry'));regions=validate_region_bindings(load('regions','region-bindings'))
    validate_evaluation_policy(spec,regions)
    captured=store.read_source_binding(identity['design_id'],identity['analysis_id'],request['source_binding_ref'])
    store.snapshot_path(identity['design_id'],identity['analysis_id'],request['source_binding_ref'])
    if geometry['source_sha256']!=captured['origin_sha256'] or geometry['geometry_sha256']!=spec['source']['geometry_sha256'] or regions['bindings_sha256']!=spec['source']['regions_sha256']:raise AnalysisError('ANALYSIS_GEOMETRY_CHANGED','prepared geometry and regions do not represent the frozen source')
    geometry_path=folder/revision['artifacts']['geometry_brep']['relative_path']
    if _read(geometry_path).sha256!=geometry['geometry_sha256']:raise AnalysisError('ANALYSIS_GEOMETRY_CHANGED','frozen BREP checksum changed')
    return {'spec':spec,'geometry':geometry,'regions':regions,'geometry_path':geometry_path,'ledger':load('load_ledger','load-ledger'),'root':run_root(store,**identity)}


@dataclass
class ExecutionState:
    request:dict
    settings:dict
    inputs:dict
    root:Path
    native:object
    guard:object
    phase:object
    meshes:dict=field(default_factory=dict)
    mesh_bindings:dict=field(default_factory=dict)
    levels:list=field(default_factory=list)

    def __post_init__(self):
        self.root=secure_fs.validate_managed_path(Path(self.root),allow_missing_leaf=False).path


def _mesh(state,level):
    index=level['level']
    binding={'spec_sha256':record_sha256(state.inputs['spec'],schema_name='spec'),'geometry_sha256':_read(state.inputs['geometry_path']).sha256,'level':deepcopy(level),'runtime_manifest_sha256':state.settings.get('runtime',{}).get('manifest_sha256'),'tools':deepcopy(state.settings.get('tools',{}))}
    if binding['spec_sha256']!=state.request['spec_sha256']:
        raise AnalysisError('ANALYSIS_SPEC_CHANGED','mesh reuse requires the same immutable physical specification')
    if index in state.meshes:
        if state.mesh_bindings[index]!=binding:raise AnalysisError('ANALYSIS_MESH_REUSE_INVALID','mesh upstream geometry, policy or exact tools changed')
        mesh=state.meshes[index]
        for reference in mesh['receipt']['artifacts'].values():read_artifact(Path(mesh['directory']),reference)
        return mesh
    state.guard.checkpoint(force=True)
    state.phase('meshing','Generating one qualified mesh shared by declared cases at this level.',attempt=1)
    path=state.root/'meshes'/('level-'+str(index).zfill(3))
    receipt=generate_mesh(state.inputs['geometry_path'],state.inputs['regions'],level,state.settings,path,transaction=state.native)
    bundle={'directory':str(path),'receipt':receipt,'geometry_manifest':state.inputs['geometry'],'region_bindings':state.inputs['regions']}
    state.meshes[index]=bundle
    state.mesh_bindings[index]=binding
    return bundle


def run_case_level(state,*,case_id,level_id):
    spec=state.inputs['spec'];level=plan_mesh_levels(spec)[level_id]
    mesh=_mesh(state,level)
    root=secure_fs.ensure_managed_directory(state.root/'cases'/case_id/('level-'+str(level_id).zfill(3)),parents=True,exist_ok=False).path
    state.guard.checkpoint(force=True)
    state.phase('preparing','Building and independently checking the frozen native FEM case.',attempt=1)
    deck=build_native_deck(deepcopy(spec),state.inputs['geometry_path'],mesh,root/'deck',state.settings,case_id=case_id,transaction=state.native)
    if deck['spec_sha256']!=state.request['spec_sha256'] or deck['case_id']!=case_id or deck['contract']['spec_sha256']!=state.request['spec_sha256']:raise AnalysisError('ANALYSIS_SPEC_CHANGED','native case changed the frozen physical specification')
    _put(root,'deck.json',deck)
    state.guard.checkpoint(force=True);state.phase('solving','Solving exactly the frozen input in a fresh owned W1 directory.',attempt=1)
    outputs=solve_native(deck,mesh,state.settings,root/'solver',native=state.native)
    _put(root,'solver.json',outputs)
    state.guard.checkpoint(force=True)
    fields=extract_fields(deck,outputs,state.settings,root/'fields',transaction=state.native)
    state.phase('validating','Checking complete fields, equilibrium, declared ROIs and applicability.',attempt=1)
    loaded={name:json.loads(read_artifact(Path(mesh['directory']),mesh['receipt']['artifacts'][name]).content) for name in ('data','mapping')}
    validation=validate_level(spec,state.inputs['ledger'],{'receipt':mesh['receipt'],'level':level,**loaded},fields)
    if record_sha256(spec,schema_name='spec')!=state.request['spec_sha256']:raise AnalysisError('ANALYSIS_SPEC_CHANGED','execution cannot mutate approved material, load or constraint data')
    validation['artifacts']={'deck':{'relative_path':(root/'deck.json').relative_to(state.root).as_posix(),'sha256':_read(root/'deck.json').sha256},'solver':{'relative_path':(root/'solver.json').relative_to(state.root).as_posix(),'sha256':_read(root/'solver.json').sha256},'fields':{'relative_path':(root/'fields'/fields['artifact']['relative_path']).relative_to(state.root).as_posix(),'sha256':fields['artifact']['sha256']}}
    _put(root,'validation.json',validation)
    state.levels.append(validation)
    state.guard.checkpoint(force=True)
    return validation


def _run_cases(state):
    spec=state.inputs['spec'];result=None
    for level in plan_mesh_levels(spec):
        for case in spec['cases']:
            result=run_case_level(state,case_id=case['case_id'],level_id=level['level'])
            if result['numerical_status']!='passed':return evaluate_convergence(spec,state.levels)
            if result['applicability_status']=='outside_scope':return {'numerical_status':'inconclusive','applicability_status':'outside_scope','code':'ANALYSIS_OUTSIDE_SCOPE','finest':[result],'comparisons':[],'spec_sha256':record_sha256(spec,schema_name='spec')}
        if level['level']>=2:
            result=evaluate_convergence(spec,state.levels)
            if result['numerical_status']=='passed':return result
    return evaluate_convergence(spec,state.levels)


def execute_attempt(request,*,settings,control,cancelled=None,phase=None,guard=None):
    inputs=_load_inputs(request,settings)
    cancelled=cancelled or (lambda:False);phase=phase or (lambda *args,**kwargs:None)
    root=secure_fs.ensure_managed_directory(inputs['root']/'attempts'/'attempt-0001',parents=True,exist_ok=False).path
    secure_fs.ensure_managed_directory(root/'meshes',parents=False,exist_ok=False)
    native=None
    def roots():return [inputs['root'],None if native is None or native.owned is None else native.owned.path]
    guard=guard or BudgetGuard(inputs['spec']['budget'],roots=roots,cancelled=cancelled,memory_observer=control.observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(request['executor_sha256']))
    phase('preparing','Verifying immutable inputs and the complete isolated native runtime.',attempt=1)
    guard.checkpoint(force=True)
    native=NativeTransaction(settings,controller=control,checkpoint=guard.checkpoint,budget_guard=guard)
    try:
        with native:
            state=ExecutionState(request,settings,inputs,root,native,guard,phase)
            numerical=_run_cases(state)
            criteria=evaluate_criteria(inputs['spec'],numerical)
            native.seal()
            guard.checkpoint(force=True)
        result={'schema_version':'AnalysisAttemptEvidence/v1','request_sha256':_read(inputs['root']/'request.json').sha256,'spec_sha256':request['spec_sha256'],'executor_sha256':request['executor_sha256'],'runtime_manifest_sha256':settings['runtime']['manifest_sha256'],'native_integrity_sealed':True,'levels':state.levels,'numerical':numerical,'criteria':criteria,'budget':guard.receipt(),'meshes':state.meshes,'status':'awaiting_visual_review' if numerical['numerical_status']=='passed' and numerical['applicability_status']!='outside_scope' else 'failed','completion':'not_published'}
        result['files']=inventory(root)
        reference=_put(root,'attempt.json',result)
        guard.checkpoint(force=True)
        if result['status']=='awaiting_visual_review':phase('validating','Numerical evidence sealed; validating native GUI before opening visual review.',attempt=1)
        return {'status':result['status'],'numerical_status':numerical['numerical_status'],'criteria_status':criteria['criteria_status'],'applicability_status':numerical['applicability_status'],'attempt_ref':{'relative_path':(root/reference['relative_path']).relative_to(inputs['root']).as_posix(),'sha256':reference['sha256']}}
    except BaseException as exc:
        # Partial native files stay diagnostic. An exception does not create a
        # reusable mesh seal, completion marker or altered-physics retry.
        _put(root,'failure.json',{'schema_version':'AnalysisAttemptFailure/v1','spec_sha256':request['spec_sha256'],'executor_sha256':request['executor_sha256'],'code':exc.code if isinstance(exc,AnalysisError) else 'ANALYSIS_EXECUTOR_FAILED','exception_type':type(exc).__name__,'budget':guard.receipt(),'completion':'not_published','retry':'explicit_new_run_only'})
        raise
    finally:
        control.terminate_owned(reason='interrupted')
