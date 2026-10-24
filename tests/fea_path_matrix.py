"""Causal P0 path matrix; expected failures are diagnostic evidence, never P0 PASS."""
from pathlib import Path
import hashlib
import json
import math
import os
import platform
import time
import zipfile
from fea_native_probe import _validate_config, verify_tool, console_statement, ProbeError


def matrix_cases(root: Path) -> list[dict]:
    return [{'name':f'external_{e}_temp_{t}',
             'external':root/(f'external_{e}_temp_{t}')/('external' if e=='ascii' else '外部 空格'),
             'native_temp':root/(f'external_{e}_temp_{t}')/('native-temp' if t=='ascii' else '原生 缓存')}
            for e in ('ascii','unicode') for t in ('ascii','unicode')]


def unv_inventory(text):
    lines=text.splitlines();datasets={};i=0
    while i<len(lines)-1:
        if lines[i].strip()=='-1' and lines[i+1].strip().isdigit():
            key=int(lines[i+1]);j=i+2
            while j<len(lines) and lines[j].strip()!='-1':j+=1
            datasets[key]=lines[i+2:j];i=j
        else:i+=1
    rows=datasets.get(2411,[])
    if not rows or len(rows)%2 or not lines or lines[-1].strip()!='-1':raise ValueError('UNV_NODE_DATA')
    nodes={}
    for i in range(0,len(rows),2):
        key=int(rows[i].split()[0]);xyz=tuple(float(v.replace('D','E')) for v in rows[i+1].split())
        if key in nodes or len(xyz)!=3 or not all(math.isfinite(v) for v in xyz):raise ValueError('UNV_NODE_DATA')
        nodes[key]=xyz
    elements=datasets.get(2412,[]);i=0;tet10=0
    while i<len(elements):
        header=list(map(int,elements[i].split()));kind,count=header[1],header[-1];i+=1
        if kind in (11,21,22,23,24):i+=1
        connectivity=[]
        while len(connectivity)<count:connectivity.extend(map(int,elements[i].split()));i+=1
        if len(connectivity)!=count or any(n not in nodes for n in connectivity):raise ValueError('UNV_ELEMENT_DATA')
        if kind==118:
            if count!=10:raise ValueError('UNV_TET10_DATA')
            tet10+=1
    return {'nodes':len(nodes),'tet10':tet10}


def inspect_mesh_archive(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            entries=[v for v in z.infolist() if v.filename=='FemMesh.unv']
            if len(entries)!=1 or not 0<entries[0].file_size<=2_000_000 or z.testzip() is not None:return {'valid':False}
            return {'valid':True,**unv_inventory(z.read(entries[0]).decode('ascii'))}
    except (OSError,ValueError,IndexError,KeyError,zipfile.BadZipFile,UnicodeError):return {'valid':False}


def assess_case(raw: dict, external: Path, native_temp: Path, *, system: str) -> dict:
    def expected(operation, success):
        result=raw.get(operation,{})
        return result.get('ok') is success and (success or 'Output file not good' in result.get('error',''))
    mesh_ok=system!='Windows' or str(native_temp).isascii()
    direct_ok=system!='Windows' or str(external).isascii()
    archive=raw.get('mesh_archive',{})
    checks={'worker': 'error' not in raw,
            'version':raw.get('version')=='1.1.3',
            'actual_temp':os.path.normcase(os.path.abspath(raw.get('temp_path',''))) == os.path.normcase(os.path.abspath(native_temp)),
            'inventory':raw.get('mesh_nodes')==10 and raw.get('mesh_volumes')==1,
            'no_restore':raw.get('auto_reopen_count')==0,
            'mesh_save':expected('mesh_save',mesh_ok),
            'plain_control':raw.get('plain_save',{}).get('ok') is True,
            'direct_unv':expected('direct_unv',direct_ok),
            'archive':not mesh_ok or (archive.get('valid') is True and archive.get('nodes')==10 and archive.get('tet10')==1)}
    return {'diagnostic_status':'passed' if all(checks.values()) else 'failed','p0_status':'blocked','checks':checks,'expected_mesh_save':mesh_ok,'expected_direct_unv':direct_ok,'observed':raw}


def run_matrix(config: dict, root: Path) -> dict:
    from mechanical_design_agent.analysis_process import ProcessController,ProcessRequest
    _validate_config(config)
    pin=config['tools']['freecadcmd'];identity=verify_tool(pin)
    root=Path(root).absolute()
    if not str(root).isascii() or root.exists() or any(p.is_symlink() for p in (root,*root.parents)):raise ProbeError('MATRIX_REQUIRES_NEW_ASCII_ROOT')
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ProbeError('MATRIX_MUST_BE_OUTSIDE_SOURCE')
    root.mkdir(parents=True)
    worker=Path(__file__).with_name('fea_path_worker.py')
    report={'schema_version':'FEAP0PathMatrix/v1','platform':platform.system(),'p0_status':'blocked','native_gui_roundtrip':'not_executed','tool':identity,'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(),'cells':{}}
    for cell in matrix_cases(root):
        external,temp=cell['external'],cell['native_temp'];external.mkdir(parents=True);temp.mkdir()
        request,response=external/'request.json',external/'response.json'
        request.write_text(json.dumps({'external':str(external)}),encoding='utf-8')
        env={k:v for k,v in os.environ.items() if k in {'SYSTEMROOT','WINDIR','COMSPEC','PATHEXT'}}
        env.update(HOME=str(external),USERPROFILE=str(external),TMPDIR=str(temp),TMP=str(temp),TEMP=str(temp),FREECAD_USER_TEMP=str(temp),PATH=os.defpath,LANG='C.UTF-8',QT_QPA_PLATFORM='offscreen')
        argv=(pin['path'],'-u',str(external/'user.cfg'),'-s',str(external/'system.cfg'),'-c',console_statement([str(worker),str(request),str(response)]))
        controller=ProcessController();owned=controller.start(ProcessRequest(argv=argv,cwd=external,env=env,stdout_path=external/'stdout.log',stderr_path=external/'stderr.log',timeout_seconds=60))
        try:
            while (outcome:=controller.poll(owned)) is None:time.sleep(.05)
        finally:controller.terminate(owned,reason='interrupted')
        raw=json.loads(response.read_text(encoding='utf-8')) if response.is_file() else {'error':'Worker response missing'}
        if outcome.reason!='exited' or outcome.returncode:raw['error']=f'Worker {outcome.reason}/{outcome.returncode}'
        raw['mesh_archive']=inspect_mesh_archive(external/'mesh.FCStd')
        value=assess_case(raw,external,temp,system=platform.system())
        value.update(external=str(external),requested_temp=str(temp))
        report['cells'][cell['name']]=value
    if verify_tool(pin)!=identity:raise ProbeError('TOOL_IDENTITY_CHANGED')
    report['diagnostic_status']='passed' if all(v['diagnostic_status']=='passed' for v in report['cells'].values()) else 'failed'
    report['artifacts']={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}
    (root/'matrix.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
