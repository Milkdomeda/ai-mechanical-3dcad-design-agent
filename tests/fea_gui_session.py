"""Owned visible GUI session. Never opens or restores a model."""
from dataclasses import asdict
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from mechanical_design_agent.analysis_process import ProcessController,ProcessRequest
from fea_native_scratch import native_scratch
from fea_gui_evidence import checked,checked_session,digest,pin,read_json,write_json
from fea_r1_runtime import canonical_path,verify_runtime


class StopSession(Exception):
    pass


def launch_spec(gui,home,scratch,config):
    home=canonical_path(home);scratch=canonical_path(scratch)
    root=ET.Element('FCParameters');group=ET.SubElement(root,'FCParamGroup',Name='Root')
    base=ET.SubElement(group,'FCParamGroup',Name='BaseApp')
    prefs=ET.SubElement(base,'FCParamGroup',Name='Preferences')
    general=ET.SubElement(prefs,'FCParamGroup',Name='General')
    ET.SubElement(general,'FCText',Name='AutoloadModule').text='FemWorkbench'
    document=ET.SubElement(prefs,'FCParamGroup',Name='Document')
    ET.SubElement(document,'FCBool',Name='AutoSaveEnabled',Value='0')
    ET.SubElement(document,'FCBool',Name='SaveBackup',Value='0')
    fem=ET.SubElement(ET.SubElement(prefs,'FCParamGroup',Name='Mod'),'FCParamGroup',Name='Fem')
    for group_name,key,tool in (('Gmsh','gmshBinaryPath','gmsh'),('Ccx','ccxBinaryPath','ccx')):
        child=ET.SubElement(fem,'FCParamGroup',Name=group_name)
        ET.SubElement(child,'FCText',Name=key).text=config['tools'][tool]['path']
        if tool=='gmsh':ET.SubElement(child,'FCInt',Name='NumOfThreads',Value='1')
    general=ET.SubElement(fem,'FCParamGroup',Name='General')
    ET.SubElement(general,'FCBool',Name='OverwriteSolverWorkingDirectory',Value='1')
    ET.ElementTree(root).write(home/'user.cfg',encoding='utf-8',xml_declaration=True)
    (home/'system.cfg').write_text('<FCParameters><FCParamGroup Name="Root"/></FCParameters>',encoding='utf-8')
    env={k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'}}
    env.update(HOME=str(home),USERPROFILE=str(home),FREECAD_USER_HOME=str(home),FREECAD_USER_DATA=str(home),FREECAD_USER_TEMP=str(scratch),TMPDIR=str(scratch),TEMP=str(scratch),TMP=str(scratch),PATH=os.defpath,PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1',LANG='en_US.UTF-8')
    return (str(gui),'-u',str(home/'user.cfg'),'-s',str(home/'system.cfg')),env


def stop_requested(path,session_id):
    if not Path(path).is_file():return False
    value=read_json(path)
    return value.get('session_id')==session_id and value.get('action')=='stop'


def owned_gui_process(logs,scratch_root,build,observe,*,timeout_seconds=7200,finalize=None):
    with native_scratch(scratch_root) as owned:
        argv,env=build(owned)
        controller=ProcessController()
        identity=controller.start(ProcessRequest(argv=tuple(argv),cwd=logs,env=env,stdout_path=logs/'stdout.log',stderr_path=logs/'stderr.log',timeout_seconds=timeout_seconds))
        try:
            tick=observe(identity,controller,owned)
            while (outcome:=controller.poll(identity)) is None:
                if callable(tick):tick()
                time.sleep(.1)
        except StopSession:
            outcome=controller.terminate(identity,reason='cancelled')
        finally:
            controller.terminate(identity,reason='interrupted')
        if finalize:finalize(owned,outcome)
        result={'guardian':asdict(identity),'outcome':outcome.reason,'returncode':outcome.returncode,'scratch_path':str(owned.path)}
    result['scratch_cleanup']='owned_contents_cleared_after_process_exit'
    return result


def descendant(pid,ancestor):
    if sys.platform!='darwin':raise ValueError('GUI live ownership only validated on macOS')
    seen=set()
    for _ in range(64):
        if type(pid) is not int or pid<=1 or pid in seen:return False
        if pid==ancestor:return True
        seen.add(pid)
        result=subprocess.run(['ps','-p',str(pid),'-o','ppid='],capture_output=True,text=True,timeout=5)
        if result.returncode or not result.stdout.strip():return False
        pid=int(result.stdout.strip())
    return False


def serve(session_path,config_path,scratch_root,*,timeout_seconds=7200):
    if sys.platform!='darwin':raise ValueError('Windows GUI remains blocked')
    session=checked_session(session_path);root=Path(session['workspace']);config=read_json(config_path)
    checked(read_json(root/'source-manifest.json')['session'])
    for source in session['sources']:checked(source)
    native=verify_runtime(config)
    preparation=read_json(checked(session['preparation']))
    expected_manifest=preparation['platforms']['macOS']['runtime_manifest']
    if config['runtime']['manifest_sha256']!=expected_manifest['sha256']:raise ValueError('GUI runtime source binding')
    manifest=read_json(checked(expected_manifest));runtime=Path(manifest['runtime_root'])
    # Launch the actual GUI binary directly so its PID remains a descendant
    # of our ownership guardian, rather than a Launch Services handoff.
    relative='Contents/Resources/bin/freecad';gui=runtime/relative
    if digest(gui)!=manifest['tree'][relative]['sha256']:raise ValueError('GUI executable identity')
    receipts=root/'receipts';home=None;started=time.time()
    def build(owned):
        nonlocal home
        home=owned.path/'user-home';home.mkdir()
        return launch_spec(gui,home,owned.path,config)
    def observe(identity,controller,owned):
        request={'session_id':session['session_id'],'session_path':str(Path(session_path).resolve()),'native_expected':native,'runtime_root':str(runtime),'home':str(canonical_path(home)),'scratch':str(canonical_path(owned.path)),'gui_executable':pin(gui),'guardian_pid':identity.pid,'case_id':None,'response_path':str(receipts/'gui-startup.json')}
        write_json(receipts/'identity-request.json',request)
        write_json(receipts/'launching.json',{'session_id':session['session_id'],'guardian':asdict(identity),'owner_pid':os.getpid(),'gui_executable':pin(gui),'runtime_manifest':expected_manifest,'session':pin(session_path),'scratch':str(owned.path),'user_home':str(home),'timeout_seconds':timeout_seconds,'status':'awaiting_native_identity'})
        def tick():
            if stop_requested(root/'stop.json',session['session_id']):raise StopSession()
            path=receipts/'gui-startup.json'
            if path.is_file() and not (receipts/'launch.json').exists():
                value=read_json(path)
                if value.get('error') or value.get('session_id')!=session['session_id'] or value.get('documents')!=[] or value.get('native_module_sha256')!=native['module_sha256'] or value.get('native_identity')!=native['native_identity'] or value.get('startup_verified') is not True or not descendant(value.get('pid'),identity.pid):raise ValueError('GUI startup identity failed')
                write_json(receipts/'launch.json',{'session_id':session['session_id'],'status':'ready','gui_pid':value['pid'],'guardian':asdict(identity),'session':pin(session_path),'runtime_manifest':expected_manifest,'startup_receipt':pin(path),'gui_executable':pin(gui),'created_at':value['at']})
                print('GUI_READY '+str(value['pid']),flush=True)
            if not path.exists() and time.time()-started>600:raise ValueError('GUI identity collection timed out')
        return tick
    def finalize(owned,outcome):
        for name in ('user.cfg','system.cfg'):
            if (home/name).is_file():shutil.copyfile(home/name,receipts/('final-'+name))
        if verify_runtime(config)!=native:raise ValueError('GUI runtime changed')
    outcome=owned_gui_process(root/'logs',scratch_root,build,observe,timeout_seconds=timeout_seconds,finalize=finalize)
    write_json(receipts/'exit.json',outcome)
    return outcome
