"""P0-only owned native GUI fixture automation; never claims human actions."""
import json
import os
from pathlib import Path
import sys
import uuid

from mechanical_design_agent import secure_fs
from fea_gui_checks import assess_gui_objects,assess_display_fields
from fea_gui_evidence import digest,pin,read_json,write_json
from fea_gui_input import compare_inputs
from fea_gui_native import CASE_IDS
from fea_gui_session import launch_spec,owned_gui_process,descendant
from fea_r1_runtime import canonical_path,verify_runtime,tree_inventory


def assess_case(value,reference,exported):
    key=value.get('case_id')
    comparison=compare_inputs(reference,exported,curved=key in ('curved_multiface','r1_mesh_4'))
    parsed=comparison.get('reference',{})
    inventory={k:parsed.get(v) for k,v in (('nodes','nodes'),('elements','elements'))}
    objects=assess_gui_objects(value,inventory,has_results=key in ('axial_bar','cantilever','r1_mesh_4'))
    valid=key in CASE_IDS and objects['status']==comparison['status']=='passed' and value.get('source_unchanged') is True and value.get('recompute_errors')==[] and value.get('closed_without_saving') is True
    return {'native':value,'gui_objects':objects,'input_comparison':comparison,'status':'passed' if valid else 'failed'}


def _run_gui_pass(config,workspace,scratch_root,*,case_ids=CASE_IDS,producer=None,software_opengl=False,gui_environment=None):
    if not case_ids or len(case_ids)!=len(set(case_ids)) or any(key not in CASE_IDS for key in case_ids):raise ValueError('fixed GUI cases required')
    if sys.platform not in ('darwin','win32'):raise ValueError('native GUI platform not supported')
    target=secure_fs.validate_managed_path(Path(workspace),allow_missing_leaf=True).path
    if target.exists() or target.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('new external GUI workspace required')
    native=verify_runtime(config)
    environment=None
    if gui_environment is not None:
        from fea_gui_environment import verify_environment
        environment=verify_environment(gui_environment,config)
        if software_opengl:raise ValueError('prepared native Mesa profile requires a single desktop OpenGL loader')
    manifest=read_json(config['runtime']['manifest_path']);runtime=Path(manifest['runtime_root'])
    relative='Contents/MacOS/FreeCAD' if sys.platform=='darwin' else 'bin/freecad.exe'
    gui=runtime/relative
    if digest(gui)!=manifest['tree'][relative]['sha256']:raise ValueError('GUI executable identity')
    target.mkdir(mode=0o700);(target/'logs').mkdir();(target/'cases').mkdir()
    request_id=uuid.uuid4().hex
    report={'schema_version':'P0NativeGuiAutomation/v1','actor':'assistant','request_id':request_id,'automated_status':'failed','visual_review_status':'pending','manual_gui_status':'not_executed','overall_p0_status':'blocked','cases':{},'runtime_unchanged':False,'platform':sys.platform,'gui_executable':pin(gui)}
    here=Path(__file__).parent;request_path=target/'request.json'
    sources=[pin(here/name) for name in ('fea_gui_native.py','fea_gui_checks.py','fea_gui_plot.py','fea_gui_identity.py','fea_gui_session.py','fea_gui_automation.py','fea_gui_environment.py','fea_gui_compile.py')]
    sources.extend(pin(here/'fixtures'/'fea'/(name+'.json')) for name in ('axial_bar','cantilever'))
    startup_verified=False
    report['producer']=producer
    def build(owned):
        home=owned.path/'user-home';home.mkdir()
        argv,env=launch_spec(gui,home,owned.path,config,software_opengl=software_opengl,desktop_opengl=environment is not None)
        report['requested_opengl']=environment['profile'] if environment else ('software' if software_opengl else 'default')
        report['gui_environment']=pin(gui_environment) if environment else None
        request={'schema_version':'P0NativeGuiRequest/v1','request_id':request_id,'workspace':str(target),'home':str(canonical_path(home)),'scratch':str(canonical_path(owned.path)),'config':config,'native':native,'case_ids':list(case_ids),'worker_sources':sources,'mode':'restore_generated_fixtures' if producer else 'build_fixtures','producer':producer}
        write_json(request_path,request)
        macro=owned.path/'P0GuiAutomation.FCMacro'
        macro.write_text('import sys; sys.dont_write_bytecode=True; sys.path.insert(0,'+ascii(str(here))+'); import fea_gui_native; fea_gui_native.run('+ascii(str(request_path))+')\n',encoding='ascii')
        report['macro']=pin(macro);report['request']=pin(request_path)
        return (*argv,str(macro)),env
    def observe(identity,controller,owned):
        report['guardian']={'pid':identity.pid,'worker_uuid':identity.worker_uuid,'created_ns':identity.created_ns}
        def tick():
            nonlocal startup_verified
            path=target/'startup.json'
            if path.exists() and not startup_verified:
                startup=read_json(path);pid=startup['pid']
                owned_pid=descendant(pid,identity.pid) if sys.platform=='darwin' else pid==identity.pid
                if not owned_pid or startup['request_id']!=request_id or startup['documents']!=[] or startup['native']['module_sha256']!=native['module_sha256'] or not os.path.samefile(startup['scratch'],owned.path):raise ValueError('native GUI startup identity')
                startup_verified=True;report['startup']=pin(path);report['startup_pid']=pid
        return tick
    def finalize(*args):
        try:
            if verify_runtime(config)!=native:raise ValueError('runtime changed during GUI automation')
            if environment is not None and verify_environment(gui_environment,config)!=environment:raise ValueError('GUI environment evidence changed during execution')
        except Exception:
            actual=tree_inventory(runtime);expected=manifest['tree']
            report['runtime_changes']={'added':sorted(set(actual)-set(expected)),'removed':sorted(set(expected)-set(actual)),'changed':sorted(k for k in set(actual)&set(expected) if actual[k]!=expected[k])}
            raise
        report['runtime_unchanged']=True
    try:
        outcome=owned_gui_process(target/'logs',scratch_root,build,observe,timeout_seconds=600,finalize=finalize)
        report['owner']=outcome
        worker=read_json(target/'native.json');report['native']=pin(target/'native.json')
        identity_ok=startup_verified and worker['request_id']==request_id and worker.get('native_identity')==native['native_identity'] and worker.get('native_before')==worker.get('native_after') and 'error' not in worker and 'cleanup_error' not in worker
        for key in case_ids:
            value=worker.get('cases',{}).get(key,{})
            reference_root=Path(producer['path']).parent if producer else target
            result=assess_case(value,reference_root/'cases'/key/'native'/'Mesh.inp',target/'cases'/key/'gui-export'/'Mesh.inp')
            if key in ('axial_bar','cantilever','r1_mesh_4'):
                result['display_consistency']=assess_display_fields(value.get('display_fields',{}))
                if result['display_consistency']['status']!='passed':result['status']='failed'
            source=secure_fs.validate_managed_path(reference_root/'cases'/key/'native'/'analysis.FCStd',allow_missing_leaf=False).path
            if not os.path.samefile(source,value['source_path']) or digest(source)!=value['source_sha256']:result['status']='failed'
            report['cases'][key]=result
        if any(digest(Path(reference['path']))!=reference['sha256'] for reference in sources):raise ValueError('GUI helper source changed')
        if identity_ok and outcome['outcome']=='exited' and outcome['returncode']==0 and all(v['status']=='passed' for v in report['cases'].values()):report['automated_status']='passed'
        if worker.get('error'):report['error']=worker['error']
    except Exception as exc:report['error']=str(exc)
    report['artifacts']={path.relative_to(target).as_posix():digest(path) for path in sorted(target.rglob('*')) if path.is_file() and not path.is_symlink()}
    write_json(target/'gui-pass.json',report)
    return report


def run_gui_automation(config,workspace,scratch_root,*,case_ids=CASE_IDS,software_opengl=False,gui_environment=None):
    """Build and then verify frozen deliveries in an independent GUI process."""
    target=secure_fs.validate_managed_path(Path(workspace),allow_missing_leaf=True).path
    if target.exists() or target.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('new external GUI workspace required')
    verify_runtime(config)
    target.mkdir(mode=0o700)
    first=_run_gui_pass(config,target/'build',scratch_root,case_ids=case_ids,software_opengl=software_opengl,gui_environment=gui_environment)
    report={'schema_version':'P0NativeGuiAutomation/v1','actor':'assistant','automated_status':'failed','visual_review_status':'pending','manual_gui_status':'not_executed','overall_p0_status':'blocked','passes':{'build':first},'cases':first['cases'],'runtime_unchanged':first['runtime_unchanged']}
    if first['automated_status']=='passed':
        second=_run_gui_pass(config,target/'roundtrip',scratch_root,case_ids=case_ids,producer=first['native'],software_opengl=software_opengl,gui_environment=gui_environment)
        report['passes']['roundtrip']=second;report['cases']=second['cases']
        report['runtime_unchanged']=first['runtime_unchanged'] and second['runtime_unchanged']
        if second['automated_status']=='passed' and first['startup_pid']!=second['startup_pid']:report['automated_status']='passed'
    report['artifacts']={path.relative_to(target).as_posix():digest(path) for path in sorted(target.rglob('*')) if path.is_file() and not path.is_symlink()}
    write_json(target/'gui-automation.json',report)
    return report
