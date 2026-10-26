"""Owned native R1 validation process, W1 scratch and immutable output evidence."""
import json
import os
from pathlib import Path
import time
from fea_native_probe import ProbeError,_validate_config,console_statement,verify_tool
from fea_native_scratch import native_scratch,verify_native_temp
from fea_path_matrix import inspect_mesh_archive
from fea_r1_evidence import assess_r1
from fea_r1_patch import digest
from fea_r1_runtime import verify_runtime
from fea_r1_reference import support_load_accounting


def run_r1(config,workspace,scratch_root):
    _validate_config(config)
    if config['schema_version']!='FEAP0Configuration/v2':raise ProbeError('R1_EXPLICIT_PROFILE_REQUIRED')
    expected=verify_runtime(config)
    pins={name:verify_tool(pin) for name,pin in config['tools'].items()}
    workspace=Path(workspace).absolute()
    if workspace.exists() or any(p.is_symlink() for p in (workspace,*workspace.parents)) or workspace.is_relative_to(Path(__file__).resolve().parents[1]):raise ProbeError('R1_NEW_EXTERNAL_WORKSPACE_REQUIRED')
    workspace.mkdir(parents=True)
    with native_scratch(scratch_root) as owned:
        request=workspace/'request.json';response=workspace/'native-r1.json'
        request.write_text(json.dumps(config|{'_expected_native_temp':str(owned.path)},ensure_ascii=False),encoding='utf-8')
        worker=Path(__file__).with_name('fea_r1_worker.py')
        env={k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
        env.update(HOME=str(workspace),USERPROFILE=str(workspace),FREECAD_USER_HOME=str(workspace),FREECAD_USER_DATA=str(workspace),TMPDIR=str(workspace),TEMP=str(workspace),TMP=str(workspace),FREECAD_USER_TEMP=str(owned.path),PATH=os.defpath,QT_QPA_PLATFORM='offscreen',PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1')
        argv=(config['tools']['freecadcmd']['path'],'-u',str(workspace/'user.cfg'),'-s',str(workspace/'system.cfg'),'-c',console_statement([str(worker),str(request),str(response)]))
        from mechanical_design_agent.analysis_process import ProcessController,ProcessRequest
        controller=ProcessController();identity=controller.start(ProcessRequest(argv=argv,cwd=workspace,env=env,stdout_path=workspace/'stdout.log',stderr_path=workspace/'stderr.log',timeout_seconds=config.get('timeout_seconds',300)))
        try:
            while (outcome:=controller.poll(identity)) is None:time.sleep(.05)
        finally:controller.terminate(identity,reason='interrupted')
        if outcome.reason!='exited' or outcome.returncode or not response.is_file():raise ProbeError('R1_WORKER_FAILED: see isolated logs')
        result=json.loads(response.read_text(encoding='utf-8'))
        verify_native_temp(result.get('native_temp_path'),owned)
        report=assess_r1(result)
        input_accounting=True
        for name in ('mesh_5','mesh_4','mesh_3','reversed','permuted'):
            case=result.get('cases',{}).get(name,{})
            filename=case.get('input_file','')
            if not filename or Path(filename).name!=filename:input_accounting=False;continue
            try:actual=support_load_accounting(workspace/name/filename)
            except (OSError,ValueError):input_accounting=False;continue
            if actual!=case.get('response',{}).get('input_force_accounting'):input_accounting=False
        report['checks']['native_input_accounting']=input_accounting
        if not input_accounting:report['status']=report['automated_status']='failed'
        archives={}
        for name in ('mesh_5','mesh_4','mesh_3','reversed','permuted'):
            actual=inspect_mesh_archive(workspace/name/'analysis.FCStd')
            inventory=result.get('cases',{}).get(name,{}).get('mesh_inventory',{})
            actual['valid']=actual.get('valid') is True and actual.get('nodes')==inventory.get('nodes') and actual.get('tet10')==inventory.get('elements')
            archives[name]=actual
        report['checks']['native_archives']=all(v['valid'] for v in archives.values())
        report['archives']=archives
        if not report['checks']['native_archives']:report['status']=report['automated_status']='failed'
        if verify_runtime(config)!=expected or any(verify_tool(pin)!=pins[name] for name,pin in config['tools'].items()):raise ProbeError('R1_RUNTIME_CHANGED')
        report['runtime']=expected
        report['native_scratch']={'path':str(owned.path),'actual_temp_verified':True}
    report['native_scratch']['cleanup']='contents_cleared_shell_and_lock_retained_after_owned_process_exit'
    report['artifacts']={p.relative_to(workspace).as_posix():digest(p.read_bytes()) for p in sorted(workspace.rglob('*')) if p.is_file() and not p.is_symlink()}
    (workspace/'r1-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return report
