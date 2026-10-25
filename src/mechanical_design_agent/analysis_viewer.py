"""Detached interactive reconstruction from pinned safe inputs, never FEM restore."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from . import secure_fs
from .analysis_budget import BudgetGuard
from .analysis_executor_identity import executor_identity,verify_executor_identity
from .analysis_gui import rebuild_native
from .analysis_gui_service import AnalysisGuiService,_authorize
from .analysis_native_transaction import NativeTransaction
from .analysis_process import ProcessController
from .analysis_protocol import parse_json_value
from .analysis_revision import read_artifact
from .analysis_runs import read_run
from .analysis_store import AnalysisStore,_timestamp
from .analysis_types import AnalysisError


def _bytes(value):return json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False).encode()


def _state(root,value):
    path=root/'status.json';data=_bytes(value)
    if path.exists():secure_fs.atomic_replace(path,data)
    else:secure_fs.atomic_publish_new(path,data)


def _request(store,design_id,analysis_id,session_id):
    if type(session_id) is not str or re.fullmatch('[0-9a-f]{32}',session_id) is None:raise AnalysisError('ANALYSIS_GUI_SESSION_INVALID','select an exact owned viewer session identifier')
    root=secure_fs.validate_managed_path(store._root(design_id,analysis_id)/'viewers'/session_id,allow_missing_leaf=False).path
    request=parse_json_value(secure_fs.read_managed_file(root/'request.json').content.decode('utf-8'),expected=dict)
    if set(request)!={'schema_version','design_id','analysis_id','run_id','case_id','session_id','executor_sha256','envelope','created_at'} or request['schema_version']!='AnalysisViewerRequest/v1' or any(request[key]!=value for key,value in {'design_id':design_id,'analysis_id':analysis_id,'session_id':session_id}.items()):raise AnalysisError('ANALYSIS_GUI_SESSION_INVALID','viewer session identity changed')
    _,run=read_run(store,design_id,analysis_id,request['run_id']);_authorize(store,run['scope'])
    return root,request


def viewer_status(store,*,design_id,analysis_id,session_id):
    root,request=_request(store,design_id,analysis_id,session_id)
    state=parse_json_value(secure_fs.read_managed_file(root/'status.json').content.decode('utf-8'),expected=dict)
    if state.get('session_id')!=session_id:raise AnalysisError('ANALYSIS_GUI_SESSION_INVALID','viewer status belongs to another session')
    return state


def close_viewer(store,*,design_id,analysis_id,session_id):
    root,request=_request(store,design_id,analysis_id,session_id)
    state=viewer_status(store,design_id=design_id,analysis_id=analysis_id,session_id=session_id)
    if state['status'] in ('closed','failed'):return state
    with secure_fs.exclusive_file_lock(root/'.request.lock'):
        if not (root/'close-request.json').exists():secure_fs.atomic_publish_new(root/'close-request.json',_bytes({'session_id':session_id,'requested_at':_timestamp()}))
    return {**state,'close_requested':True}


def open_viewer(store,*,design_id,analysis_id,run_id,case_id):
    # Registered final-result requests verify the full sealed numerical attempt
    # before any child starts. A viewer never writes their completion receipt.
    run,request=read_run(store,design_id,analysis_id,run_id)
    gui=AnalysisGuiService(store)
    envelope=gui.issue_request(design_id=design_id,analysis_id=analysis_id,run_id=run_id,spec_sha256=request['spec_sha256'],kind='final_result')
    _,_,_,_,_,spec=gui._registered(envelope)
    if case_id not in [case['case_id'] for case in spec['cases']]:raise AnalysisError('ANALYSIS_GUI_CASE_CHANGED','select an explicit declared case')
    session_id=uuid.uuid4().hex;root=secure_fs.ensure_managed_directory(store._root(design_id,analysis_id)/'viewers'/session_id,parents=True,exist_ok=False).path
    record={'schema_version':'AnalysisViewerRequest/v1','design_id':design_id,'analysis_id':analysis_id,'run_id':run_id,'case_id':case_id,'session_id':session_id,'executor_sha256':executor_identity()['sha256'],'envelope':envelope,'created_at':_timestamp()}
    secure_fs.atomic_publish_new(root/'request.json',_bytes(record))
    state={'status':'starting','session_id':session_id,'case_id':case_id,'source_fcstd_open_count':0,'engineering_certification':False,'verification_scope':'Interactive safe reconstruction of sealed results; no new delivery or visual acceptance.','next_action':'Poll analysis open-status; the isolated FreeCAD window opens with the native result task.','mutable_working_directory':str(root/'outputs')}
    _state(root,state)
    argv=[sys.executable,'-I','-B','-m','mechanical_design_agent.analysis_viewer','--workspace',str(store.workspace),'--design-id',design_id,'--analysis-id',analysis_id,'--session-id',session_id]
    environment={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    options={'start_new_session':True} if os.name=='posix' else {'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_BREAKAWAY_FROM_JOB}
    try:
        with (root/'launcher.stdout.log').open('xb') as stdout,(root/'launcher.stderr.log').open('xb') as stderr:subprocess.Popen(argv,cwd=store.workspace,env=environment,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,close_fds=True,**options)
    except OSError:
        state={**state,'status':'failed','code':'ANALYSIS_GUI_LAUNCH_FAILED'};_state(root,state)
    return state


def run_viewer(store,*,design_id,analysis_id,session_id):
    from .analysis_execution import execution_settings
    from types import SimpleNamespace
    root,request=_request(store,design_id,analysis_id,session_id)
    with secure_fs.exclusive_file_lock(root/'.viewer.lock'):
        state=viewer_status(store,design_id=design_id,analysis_id=analysis_id,session_id=session_id)
        if state['status']!='starting':raise AnalysisError('ANALYSIS_GUI_SESSION_INVALID','a viewer session cannot be relaunched')
        controller=ProcessController()
        try:
            verify_executor_identity(request['executor_sha256'])
            _,execution=read_run(store,design_id,analysis_id,request['run_id'])
            identity={key:request[key] for key in ('design_id','analysis_id','run_id')}
            settings=execution_settings(SimpleNamespace(store=store,request=execution,_identity=lambda:identity))
            gui=AnalysisGuiService(store);registered,_,_,folder,revision,spec=gui._registered(request['envelope'])
            deck,mesh,fields,solver,geometry=gui._case_inputs(registered,request['case_id'],folder,revision)
            budget={**spec['budget'],'wall_seconds':14400,'stage_seconds':14400}
            guard=BudgetGuard(budget,roots=lambda:[root],cancelled=lambda:(root/'close-request.json').exists(),memory_observer=controller.observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(request['executor_sha256']))
            def ready(report,native_identity,work):
                secure_fs.atomic_publish_new(root/'interactive-window.png',read_artifact(work,report['interactive_window']).content)
                for reference in report['interactive_images']:secure_fs.atomic_publish_new(root/reference['relative_path'],read_artifact(work,reference).content)
                _state(root,{**state,'status':'opened','native_gui_pid':report['pid'],'owned_tree':{'pid':native_identity.pid,'created_ns':native_identity.created_ns,'worker_uuid':native_identity.worker_uuid},'native_request_sha256':report['request_sha256'],'native_result_task':True,'field':'von_mises','unit':'MPa','mutable_document_path':report['interactive_document_path'],'retention':'Private viewer working files are retained after close; immutable delivery files remain unchanged.','window_image_path':str(root/'interactive-window.png'),'native_figure_path':str(root/'interactive-figure.png'),'currentness':store.currentness_for_source(design_id,analysis_id,execution['source_binding_ref'])['currentness']})
            with NativeTransaction(settings,controller=controller,checkpoint=guard.checkpoint,budget_guard=guard,viewer=True) as native:
                rebuild_native(deck,mesh,fields,solver,geometry,settings,root/'outputs',transaction=native,guard=guard,interactive=True,on_ready=ready)
                native.seal()
            _state(root,{**viewer_status(store,design_id=design_id,analysis_id=analysis_id,session_id=session_id),'status':'closed'})
        except BaseException as exc:
            closed=(root/'close-request.json').exists()
            last=viewer_status(store,design_id=design_id,analysis_id=analysis_id,session_id=session_id)
            _state(root,{**last,'status':'closed' if closed else 'failed','code':'ANALYSIS_GUI_CLOSED' if closed else exc.code if isinstance(exc,AnalysisError) else 'ANALYSIS_GUI_PROCESS_FAILED','diagnostic':'Preserved isolated viewer diagnostics and saved working files; the source and completed delivery were not edited.'})
            if not closed:return 1
        finally:controller.terminate_owned(reason='cancelled')
    return 0


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workspace',type=Path,required=True)
    for key in ('design-id','analysis-id','session-id'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    return run_viewer(AnalysisStore(args.workspace),design_id=args.design_id,analysis_id=args.analysis_id,session_id=args.session_id)


if __name__=='__main__':raise SystemExit(main())
