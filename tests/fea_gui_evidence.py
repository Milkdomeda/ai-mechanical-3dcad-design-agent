"""P0-only immutable sources and explicit human evidence; no FreeCAD import."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile

from mechanical_design_agent import secure_fs
from fea_gui_input import compare_inputs
from fea_r1_patch import PROFILE
from fea_r1_runtime import approved_module_digest


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def pin(path):return {'path':str(Path(path).absolute()),'sha256':digest(path)}


def read_json(path):
    def reject(value):raise ValueError('nonfinite JSON '+value)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'),parse_constant=reject,object_pairs_hook=unique)


def write_json(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def write_report(path,value):
    path=Path(path);write_json(path,value)
    cases=value.get('cases',{value.get('case_id','case'):value})
    lines=['# P0 GUI evidence','',f"Status: {value['status']}",'','Windows GUI: not_executed. Overall P0 remains blocked.','',
           '| Case | Status | Failed or missing checks |','|---|---|---|']
    for name,case in cases.items():
        missing=', '.join(k for k,v in case.get('checks',{}).items() if not v)
        lines.append(f"| {name} | {case['status']} | {missing} |")
        lines.extend(['',f"## {name}",'','Bindings and measured differences:','', '```json',json.dumps({k:case[k] for k in ('bindings','errors','comparison') if k in case},ensure_ascii=False,indent=2),'```',''])
    with path.with_suffix('.md').open('x',encoding='utf-8') as stream:stream.write('\n'.join(lines)+'\n')


def checked(reference):
    path=secure_fs.validate_managed_path(Path(reference['path']),allow_missing_leaf=False).path
    if not path.is_file() or digest(path)!=reference['sha256']:raise ValueError('evidence hash/path mismatch')
    return path


def checked_session(path):
    path=Path(path).resolve(strict=True);session=read_json(path)
    root=path.parent
    if Path(session['workspace']).resolve(strict=True)!=root:raise ValueError('session workspace binding')
    if checked(read_json(root/'source-manifest.json')['session'])!=path:raise ValueError('session path binding')
    for source in session['helper_sources']:checked(source)
    return session


def inspect_archive(path,case):
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or len(names)>1000:raise ValueError('archive inventory')
        for entry in archive.infolist():
            name=entry.filename
            if name.startswith(('/','\\')) or '\\' in name or ':' in name or '..' in Path(name).parts or stat.S_ISLNK(entry.external_attr>>16):raise ValueError('archive path')
            if name.lower().endswith(('.py','.pyc','.fcmacro')):raise ValueError('executable archive member')
        if sum(v.file_size for v in archive.infolist())>256*1024*1024:raise ValueError('archive size')
        documents=[]
        for name in ('Document.xml','GuiDocument.xml'):
            if name not in names:
                if name=='Document.xml':raise ValueError('missing document')
                continue
            raw=archive.read(name)
            if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValueError('XML declaration')
            documents.append(ET.fromstring(raw))
        inventory=[{'name':v.get('name'),'type_id':v.get('type')} for v in documents[0].findall('./Objects/Object')]
        if inventory!=case['object_inventory']:raise ValueError('object inventory mismatch')
        proxies=[]
        for doc in documents:
            for node in doc.iter('Python'):
                module=node.get('module','');cls=node.get('class','')
                if not module.startswith(('femobjects.','femviewprovider.')) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',cls):raise ValueError('unreviewed Proxy')
                # These frozen native fixtures persist JSON null and restore
                # standard native classes. No arbitrary serialized state allowed.
                if node.get('encoded')!='yes' or node.get('value')!='bnVsbA==':raise ValueError('unreviewed Proxy state')
                proxies.append({k:v for k,v in node.attrib.items() if k in ('module','class','encoded')})
        if proxies!=case['proxy_metadata_inventory_only']:raise ValueError('Proxy inventory mismatch')
    return inventory


def prepare(manifest_path,workspace,platform):
    if sys.platform!='darwin' or platform!='macOS':raise ValueError('interactive Windows preparation remains blocked')
    manifest_path=secure_fs.validate_managed_path(Path(manifest_path),allow_missing_leaf=False).path
    manifest=read_json(manifest_path)
    if manifest.get('schema_version')!='P0GuiAcceptancePreparation/v1' or manifest.get('profile')!=PROFILE or manifest.get('native_module_sha256')!=approved_module_digest():raise ValueError('preparation profile')
    target=secure_fs.validate_managed_path(Path(workspace),allow_missing_leaf=True).path
    repository=Path(__file__).resolve().parents[1]
    if target.exists() or target.is_relative_to(repository):raise ValueError('new external workspace required')
    selected=manifest['platforms'][platform];sources=[pin(manifest_path)];reports={}
    for key,reference in selected['references'].items():
        path=checked(reference);report=read_json(path)
        if report.get('schema_version') not in ('FEANativeProbe/v1','FEAR1Evidence/v1') or report.get('automated_status')!='passed' or report.get('status')!='blocked':raise ValueError('automated source evidence required')
        sources.append(pin(path));reports[key]=(path,report)
        for name,sha in report['artifacts'].items():
            artifact=path.parent/name
            if not artifact.resolve().is_relative_to(path.parent.resolve()):raise ValueError('source artifact escape')
            checked({'path':str(artifact),'sha256':sha});sources.append(pin(artifact))
    cases=[];ids=set()
    for case in selected['cases']:
        case_id=case['case_id']
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',case_id) or case_id in ids:raise ValueError('case identity')
        ids.add(case_id)
        report_path,report=reports[case['source_report_key']]
        for name in ('source_fcstd','reference_inp'):
            path=checked(case[name])
            if target.is_relative_to(path.parent):raise ValueError('workspace inside source')
            try:relative=path.relative_to(report_path.parent).as_posix()
            except ValueError as exc:raise ValueError('source report binding') from exc
            if report['artifacts'].get(relative)!=case[name]['sha256']:raise ValueError('source report binding')
        inspect_archive(case['source_fcstd']['path'],case)
        comparison=compare_inputs(case['reference_inp']['path'],case['reference_inp']['path'])
        if comparison['status']!='passed':raise ValueError('unsupported native reference input')
        if comparison['reference']['nodes']!=case['expected_mesh_inventory']['nodes'] or comparison['reference']['elements']!=case['expected_mesh_inventory']['elements']:raise ValueError('mesh source binding')
        cases.append(case)
    if not cases:raise ValueError('no cases')
    # Nothing has been created before all source checks succeed.
    target.mkdir(mode=0o700)
    for name in ('input','gui-output','screenshots','logs','receipts','comparison'):(target/name).mkdir()
    prepared=[]
    for case in cases:
        folder=target/'input'/case['case_id'];folder.mkdir()
        copy=folder/'analysis.FCStd';shutil.copyfile(case['source_fcstd']['path'],copy);copy.chmod(0o444)
        reference=folder/'reference.inp';shutil.copyfile(case['reference_inp']['path'],reference);reference.chmod(0o444)
        if digest(copy)!=case['source_fcstd']['sha256'] or digest(reference)!=case['reference_inp']['sha256']:raise ValueError('copy changed')
        output=target/'gui-output'/case['case_id'];output.mkdir()
        prepared.append(case|{'input_fcstd':pin(copy),'reference_copy':pin(reference),'output_directory':str(output)})
        folder.chmod(0o555)
    (target/'input').chmod(0o555)
    helpers=[pin(Path(__file__).with_name(name)) for name in ('fea_gui_input.py','fea_gui_evidence.py','fea_gui_session.py','fea_gui_identity.py')]+[pin(repository/'scripts/fea_gui_acceptance.py')]
    session={'schema_version':'P0GuiSession/v1','session_id':uuid.uuid4().hex,'platform':platform,'workspace':str(target),'status':'awaiting_gui_startup','created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'profile':PROFILE,'native_module_sha256':approved_module_digest(),'preparation':pin(manifest_path),'sources':sources,'helper_sources':helpers,'cases':prepared,'windows_gui_status':'not_executed','overall_p0_status':'blocked'}
    path=target/'session.json';write_json(path,session)
    write_json(target/'source-manifest.json',{'sources':sources,'session':pin(path)})
    return path


def validate_case(session_path,case_id,actions):
    result={'schema_version':'P0GuiCaseEvidence/v1','case_id':case_id,'status':'blocked','checks':{},'errors':[],'overall_p0_status':'blocked','windows_gui_status':'not_executed'}
    try:session=checked_session(session_path)
    except (ValueError,OSError,KeyError) as exc:
        result.update(status='failed',errors=[str(exc)],checks={'prepared_session':False});return result
    root=Path(session['workspace']);case=next(c for c in session['cases'] if c['case_id']==case_id)
    result['bindings']={'session':pin(session_path),'preparation':session['preparation'],'input_fcstd':case['input_fcstd'],'reference_input':case['reference_copy'],'actions_sha256':hashlib.sha256(json.dumps(actions,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    checks=result['checks'];pid=None
    def attempt(name,operation):
        try:checks[name]=bool(operation())
        except (ValueError,OSError,KeyError,TypeError) as exc:checks[name]=False;result['errors'].append(name+': '+str(exc))
    attempt('source_hashes',lambda:all(checked(p) for p in session['sources']))
    attempt('input_copy',lambda:bool(checked(case['input_fcstd']) and checked(case['reference_copy'])))
    try:
        launch=read_json(root/'receipts/launch.json');startup=read_json(checked(launch['startup_receipt']))
        pid=launch['gui_pid']
        checks['gui_identity']=launch['status']=='ready' and launch['session_id']==startup['session_id']==session['session_id'] and type(pid) is int and pid>0 and startup['pid']==pid and startup['native_module_sha256']==session['native_module_sha256'] and startup['documents']==[] and startup['startup_verified'] is True
        result['bindings']['launch']=pin(root/'receipts/launch.json')
        result['bindings']['startup']=launch['startup_receipt']
    except (ValueError,OSError,KeyError,TypeError) as exc:checks['gui_identity']=False;result['errors'].append(str(exc))
    def snapshot():
        value=read_json(root/'receipts'/f'{case_id}-gui.json')
        result['bindings']['snapshot']=pin(root/'receipts'/f'{case_id}-gui.json')
        directories=value.get('working_directory_from_panel',[])
        return value['session_id']==session['session_id'] and value['pid']==pid and value['case_id']==case_id and os.path.samefile(value['file_path'],case['input_fcstd']['path']) and value['input_fcstd_sha256']==case['input_fcstd']['sha256'] and value['mesh_inventory']==case['expected_mesh_inventory'] and value['objects']==case['object_inventory'] and value['errors']==[] and len(directories)==1 and os.path.samefile(directories[0],case['output_directory']) and (not case['saved_result_present'] or value.get('result_inventory',{}).get('complete') is True)
    attempt('native_snapshot',snapshot)
    required=['file_open','object_and_mesh_review']+(['saved_result_review_if_present'] if case['saved_result_present'] else [])+['document_recompute','select_new_working_directory','native_write_inp_button','close_without_saving','operator_review']
    def human_actions():
        relevant=[a for a in actions if a.get('case_id')==case_id]
        if [a.get('action') for a in relevant]!=required:return False
        times=[]
        for event in relevant:
            if event['actor']!='human' or not event.get('operator') or not event.get('user_statement') or event.get('session_id')!=session['session_id'] or event['gui_pid']!=pid or event['source_sha256']!=case['input_fcstd']['sha256']:return False
            time=dt.datetime.fromisoformat(event['at'])
            if time.tzinfo is None:return False
            times.append(time)
            image=checked(event['evidence'])
            if not image.resolve().is_relative_to((root/'screenshots').resolve()) or image.read_bytes()[:8]!=b'\x89PNG\r\n\x1a\n':return False
        return times==sorted(times)
    attempt('human_actions',human_actions)
    output=Path(case['output_directory'])/'Mesh.inp'
    def export():
        checked(pin(output))
        if output.is_symlink() or os.path.samefile(output,case['reference_copy']['path']) or not output.resolve().is_relative_to((root/'gui-output'/case_id).resolve()):return False
        comparison=compare_inputs(case['reference_copy']['path'],output,curved=case_id in ('curved_multiface','r1_mesh_4'))
        result['comparison']=comparison
        return comparison['status']=='passed'
    attempt('input_equivalence',export)
    result['status']='passed' if all(checks.values()) else 'failed' if checks.get('source_hashes') is False or checks.get('input_copy') is False or result.get('comparison',{}).get('status')=='failed' else 'blocked'
    return result


def summarize(session_path,action_files):
    session=checked_session(session_path);cases={}
    for case in session['cases']:
        key=case['case_id'];actions=read_json(action_files[key]) if key in action_files else []
        if not isinstance(actions,list):raise ValueError('action list required')
        cases[key]=validate_case(session_path,key,actions)
        if key in action_files:cases[key]['actions_file']=pin(action_files[key])
    status='failed' if any(c['status']=='failed' for c in cases.values()) else 'passed' if all(c['status']=='passed' for c in cases.values()) else 'blocked'
    return {'schema_version':'P0GuiAcceptance/v1','session':pin(session_path),'macOS_gui_status':status,'windows_gui_status':'not_executed','status':'failed' if status=='failed' else 'blocked','eligible_for_p1':False,'cases':cases}
