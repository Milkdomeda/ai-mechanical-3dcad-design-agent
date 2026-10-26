"""Owned native FEM GUI reconstruction; no persistent FEM document restore."""
from dataclasses import asdict
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import uuid
import xml.etree.ElementTree as ET

from . import secure_fs
from .analysis_deck import compare_deck_semantics,inspect_native_deck
from .analysis_native import _environment,_native_path,_read,verify_tool_identity,verify_native_modules
from .analysis_native_transaction import NativeTransaction
from .analysis_process import ProcessRequest
from .analysis_revision import read_artifact
from .analysis_types import AnalysisError
from .resources.freecad.analysis_gui_checks import assess_display_fields,inspect_native_archive,VIEW_PROVIDERS

_SOURCES=('analysis_gui_rebuild.py','analysis_gui_controls.py','analysis_gui_pixels.py','analysis_gui_plot.py','analysis_build.py','analysis_extract_results.py','analysis_runtime_identity.py')


def _bytes(value):return json.dumps(value,ensure_ascii=True,sort_keys=True,allow_nan=False).encode()


def _configuration(home,settings):
    root=ET.Element('FCParameters');base=ET.SubElement(ET.SubElement(root,'FCParamGroup',Name='Root'),'FCParamGroup',Name='BaseApp')
    prefs=ET.SubElement(base,'FCParamGroup',Name='Preferences')
    general=ET.SubElement(prefs,'FCParamGroup',Name='General');ET.SubElement(general,'FCText',Name='AutoloadModule').text='FemWorkbench'
    document=ET.SubElement(prefs,'FCParamGroup',Name='Document')
    for name in ('AutoSaveEnabled','SaveBackup'):ET.SubElement(document,'FCBool',Name=name,Value='0')
    fem=ET.SubElement(ET.SubElement(prefs,'FCParamGroup',Name='Mod'),'FCParamGroup',Name='Fem')
    for group,key,tool in [('Gmsh','gmshBinaryPath','gmsh'),('Ccx','ccxBinaryPath','ccx')]:
        child=ET.SubElement(fem,'FCParamGroup',Name=group)
        ET.SubElement(child,'FCText',Name=key).text=_native_path(settings['tools'][tool]['path'])
        if tool=='gmsh':ET.SubElement(child,'FCInt',Name='NumOfThreads',Value='1')
    secure_fs.atomic_publish_new(home/'user.cfg',ET.tostring(root,encoding='utf-8',xml_declaration=True))
    secure_fs.atomic_publish_new(home/'system.cfg',b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')


def verify_native_receipt(report,*,request,root,deck,fields):
    """Validate only the owned producer's response, never caller result claims."""
    def reject():raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','native GUI identity, fields, export or artifact evidence is incomplete or changed')
    if report.get('status')!='passed' or report.get('request_id')!=request.get('request_id'):reject()
    if report.get('request_sha256')!=hashlib.sha256(_bytes(request)).hexdigest() or report.get('identity')!=request['identity'] or report.get('modules_before')!=report.get('modules_after') or 'error' in report or 'cleanup_error' in report:reject()
    if report.get('source_fcstd_open_count')!=0 or report.get('native_write_button') is not True or report.get('result_task') is not True or report.get('solver_task') is not True or report.get('deformation_scale')!=0 or report.get('step')!={'number':1,'increment':1,'time':1.}:reject()
    if report.get('saved_result_visible') is not True or report.get('saved_result_mesh_visible') is not True or not report.get('graphics_contexts') or any(item.get('valid') is not True or item['version'][0]<2 for item in report['graphics_contexts']):reject()
    for name,pin in request['modules'].items():
        if report['modules_before'][name]['sha256']!=pin['sha256']:reject()
    expected=fields['fields'];actual={'node_ids':expected['node_ids'],**{key:expected[key]['values'] for key in ('displacement','stress','von_mises','principal_stress')}}
    if report.get('field_values_sha256')!=hashlib.sha256(json.dumps(actual,sort_keys=True,allow_nan=False).encode()).hexdigest():reject()
    if report.get('mesh_inventory')!={'nodes':deck['contract']['node_count'],'elements':deck['contract']['element_count']}:reject()
    if report.get('mesh_viewport_pixels',{}).get('status')!='passed':reject()
    objects=report.get('objects',[]);index={item['name']:item for item in objects}
    if len(index)!=len(objects):reject()
    for name,expected_vp in VIEW_PROVIDERS.items():
        if name not in index or tuple(index[name][key] for key in ('type_id','proxy_module','proxy_class'))!=expected_vp:reject()
    display=report.get('display_fields',{})
    if assess_display_fields(display)['status']!='passed':reject()
    expected_scalars={'displacement_x':[row[0] for row in expected['displacement']['values']],
                      'displacement_magnitude':[sum(v*v for v in row)**.5 for row in expected['displacement']['values']],
                      'von_mises':expected['von_mises']['values']}
    required={'analysis.FCStd','native-export.inp','mesh-native.png'}
    for name,data in display.items():
        values=expected_scalars[name]
        if name!='displacement_magnitude' and data['nodal_values_sha256']!=hashlib.sha256(json.dumps(values,allow_nan=False).encode()).hexdigest():reject()
        # The native C++ length calculation and Python sqrt may round by one
        # ULP. Native data extrema are checked at representation resolution.
        import math
        for key,value in [('raw_min',min(values)),('raw_max',max(values))]:
            if abs(data[key]-value)>math.ulp(value)*2:reject()
        if data.get('step')!=report['step'] or data.get('stress_recovery')!='native_extrapolated_averaged_nodal':reject()
        for key in ('native_image','figure','panel'):required.add(data[key])
    if set(report['artifacts'])!=required:reject()
    for name,reference in report['artifacts'].items():
        if reference['relative_path']!=name or Path(name).name!=name:reject()
        data=read_artifact(root,reference)
        if not data.content:reject()
        if name.endswith('.png'):
            if len(data.content)<24 or data.content[:8]!=b'\x89PNG\r\n\x1a\n' or data.content[12:16]!=b'IHDR':reject()
            width,height=struct.unpack('>II',data.content[16:24])
            if width<10 or height<10:reject()
    audit=inspect_native_archive(root/'analysis.FCStd',[{'name':item['name'],'type_id':item['type_id']} for item in objects])
    comparison=compare_deck_semantics(deck['inspection'],inspect_native_deck(root/'native-export.inp',allowed_root=root))
    if not comparison['equivalent']:reject()
    producer={name:{'relative_path':name,'sha256':_read(root/name).sha256} for name in ('request.json','response.json','process.json','stdout.log','stderr.log')}
    return {'schema_version':'AnalysisGuiEvidence/v1','status':'awaiting_visual_review','native_automation_status':'passed','actor':'assistant','identity':request['identity'],'producer':producer,'artifacts':report['artifacts'],'display_fields':display,'archive_audit':audit,'native_input_comparison':comparison,'source_fcstd_open_count':0,'human_actions':'not_claimed'}


def rebuild_native(deck,mesh,fields,solver_outputs,geometry,settings,output_dir,*,controller=None,checkpoint=None,transaction=None,guard=None,binding=None,interactive=False,on_ready=None):
    """One fresh owned GUI process; returns evidence, never delivery acceptance."""
    output=secure_fs.ensure_managed_directory(Path(output_dir),parents=False,exist_ok=False).path
    checkpoint=checkpoint or (guard.checkpoint if guard is not None else lambda:None)
    if transaction is not None and (transaction.settings!=settings or transaction.owned is None or transaction._sealed):raise AnalysisError('ANALYSIS_NATIVE_PHASE_INVALID','GUI must share the same living unsealed owner')
    with (NativeTransaction(settings,controller=controller,checkpoint=checkpoint,budget_guard=guard) if transaction is None else nullcontext(transaction)) as native:
        work=secure_fs.ensure_managed_directory(native.owned.path/uuid.uuid4().hex,parents=False,exist_ok=False).path
        home=secure_fs.ensure_managed_directory(work/'home',parents=False,exist_ok=False).path
        _configuration(home,settings)
        sources={}
        for name in _SOURCES:
            data=_read(native.resources/name);secure_fs.atomic_publish_new(work/name,data.content);sources[name]=data.sha256
        inputs={}
        for name,path,sha in [('geometry.brep',Path(geometry),deck['contract']['geometry_sha256']),('mesh.unv',Path(mesh['directory'])/mesh['receipt']['artifacts']['native_mesh']['relative_path'],deck['contract']['mesh_sha256']),('Mesh.frd',Path(solver_outputs['directory'])/'Mesh.frd',solver_outputs['files']['frd']['sha256'])]:
            data=_read(path)
            if data.sha256!=sha:raise AnalysisError('ANALYSIS_GUI_INPUT_CHANGED','native GUI needs the exact frozen geometry, mesh and solver output')
            secure_fs.atomic_publish_new(work/name,data.content);inputs[name]=data.sha256
        for name,value in [('fields.json',fields),('deck.json',{'contract':deck['contract']})]:
            content=_bytes(value);secure_fs.atomic_publish_new(work/name,content);inputs[name]=hashlib.sha256(content).hexdigest()
        manifest=json.loads(_read(Path(settings['runtime']['manifest_path'])).content)
        modules={key:{'path':_native_path(manifest[field+'_path']),'sha256':manifest[field+'_sha256']} for key,field in [('faceforce','module'),('output','output_module')]}
        request={'schema_version':'AnalysisNativeGuiRequest/v1','request_id':uuid.uuid4().hex,'expected_temp':_native_path(native.owned.path),'home':_native_path(home),'inputs':inputs,'modules':modules,'identity':{'spec_sha256':deck['spec_sha256'],'case_id':deck['case_id'],'mesh_sha256':deck['contract']['mesh_sha256'],'input_sha256':deck['input_ref']['sha256']}}
        if binding is not None:request['identity']['registration']=binding
        secure_fs.atomic_publish_new(work/'request.json',_bytes(request))
        report=_launch_gui(native,work,home,settings,deck['budget'],'analysis_gui_rebuild',request,inputs,sources,output,checkpoint,interactive=interactive,on_ready=on_ready)
        # Native filenames are checked before crossing out of W1.
        for name,ref in report.get('artifacts',{}).items():
            if type(name) is not str or Path(name).name!=name or name in ('.','..') or '\\' in name or ':' in name:raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','GUI artifacts must be fixed local files')
            secure_fs.atomic_publish_new(output/name,read_artifact(work,ref).content)
        evidence=verify_native_receipt(report,request=request,root=output,deck=deck,fields=fields)
        if transaction is None:native.seal()
        checkpoint()
        evidence['native_integrity_sealed']=native._sealed
        name='evidence.json' if native._sealed else 'provisional-evidence.json'
        secure_fs.atomic_publish_new(output/name,_bytes(evidence))
        return {**evidence,'evidence_ref':{'relative_path':name,'sha256':_read(output/name).sha256}}


def _launch_gui(native,work,home,settings,budget,module,request,inputs,sources,output,checkpoint,*,interactive=False,on_ready=None):
    if module not in ('analysis_gui_rebuild','analysis_gui_preview'):raise AnalysisError('ANALYSIS_GUI_PHASE_INVALID','only fixed package GUI phases are allowed')
    if interactive and (module!='analysis_gui_rebuild' or native.viewer is not True):raise AnalysisError('ANALYSIS_GUI_PHASE_INVALID','interactive reconstruction needs its separately owned viewer role')
    suffix=',interactive=True' if interactive else ''
    statement='import sys;sys.dont_write_bytecode=True;sys.path.insert(0,'+ascii(_native_path(work))+');import '+module+';'+module+'.run('+ascii(_native_path(work/'request.json'))+suffix+')\n'
    secure_fs.atomic_publish_new(work/'AnalysisGui.FCMacro',statement.encode('ascii'))
    secure_fs.atomic_publish_new(output/'request.json',_bytes(request))
    env=_environment(home,native.owned.path);env.pop('QT_QPA_PLATFORM',None)
    for key in ('DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'):
        if key in os.environ:env[key]=os.environ[key]
    env['QT_OPENGL']='software' if settings['configuration']['software_opengl'] else 'desktop'
    verify_tool_identity(settings,'freecadgui');verify_native_modules(settings);checkpoint()
    argv=(_native_path(settings['tools']['freecadgui']['path']),'-u',_native_path(home/'user.cfg'),'-s',_native_path(home/'system.cfg'),_native_path(work/'AnalysisGui.FCMacro'))
    identity=native.controller.start(ProcessRequest(argv=argv,cwd=work,stdout_path=work/'stdout.log',stderr_path=work/'stderr.log',env=env,timeout_seconds=14400 if interactive else budget['stage_seconds'],max_log_bytes=budget['max_disk_bytes']))
    outcome=None;notified=False
    try:
        while (outcome:=native.controller.poll(identity)) is None:
            checkpoint()
            if interactive and not notified and (work/'response.json').is_file():
                ready=json.loads(_read(work/'response.json').content)
                if ready.get('interactive_ready') is True:
                    if ready.get('status')!='passed' or ready.get('request_sha256')!=hashlib.sha256(_bytes(request)).hexdigest() or ready.get('identity')!=request['identity'] or not native.controller.owns_pid(identity,ready.get('pid')):raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','interactive readiness belongs to another owned native process tree')
                    if ready.get('interactive_document_path')!=_native_path(work/'analysis.FCStd') or ready.get('interactive_window',{}).get('relative_path')!='interactive-window.png':raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','interactive working copy must remain in its separately owned W1 directory')
                    read_artifact(work,ready['interactive_window'])
                    if [item.get('relative_path') for item in ready.get('interactive_images',[])]!=['interactive-native.png','interactive-figure.png']:raise AnalysisError('ANALYSIS_GUI_RECEIPT_INVALID','interactive result requires a separately captured native cloud')
                    for reference in ready['interactive_images']:read_artifact(work,reference)
                    if on_ready is not None:on_ready(ready,identity,work)
                    notified=True
            time.sleep(.1)
    finally:
        stopped=native.controller.terminate(identity,reason='interrupted')
        if outcome is None:outcome=stopped
        for name in ('stdout.log','stderr.log','response.json'):
            if (work/name).exists():secure_fs.atomic_publish_new(output/name,_read(work/name).content)
        secure_fs.atomic_publish_new(output/'process.json',_bytes({'outcome':asdict(outcome),'request_sha256':hashlib.sha256(_bytes(request)).hexdigest(),'fixed_script_hashes':sources,'input_hashes':inputs}))
    verify_tool_identity(settings,'freecadgui');verify_native_modules(settings)
    from .analysis_process import require_process_exit
    require_process_exit(outcome,failure_code='ANALYSIS_GUI_PROCESS_FAILED',message='owned native GUI failed; inspect retained process evidence')
    if _read(work/'request.json').content!=_bytes(request) or _read(work/'AnalysisGui.FCMacro').content!=statement.encode('ascii') or any(_read(work/name).sha256!=sha for name,sha in {**inputs,**sources}.items()):raise AnalysisError('ANALYSIS_GUI_INPUT_CHANGED','native GUI input or fixed source changed during execution')
    return json.loads(_read(output/'response.json').content)


def preview_native(payload,*,transaction=None,guard=None,geometry_directory=None):
    """Trusted prepare collaborator; native preview binds the whole candidate."""
    from .analysis_loads import build_load_ledger
    from .analysis_protocol import record_sha256,canonical_record,parse_record
    settings,spec,geometry,regions=(payload[key] for key in ('settings','spec','geometry','regions'))
    if record_sha256(spec,schema_name='spec')!=payload['spec_sha256']:raise AnalysisError('ANALYSIS_PREVIEW_INVALID','preview needs the exact candidate specification')
    output=secure_fs.ensure_managed_directory(Path(payload['output_dir']),parents=False,exist_ok=False).path
    checkpoint=guard.checkpoint if guard is not None else lambda:None
    if transaction is not None and (transaction.settings!=settings or transaction.owned is None or transaction._sealed):raise AnalysisError('ANALYSIS_NATIVE_PHASE_INVALID','preview must share the same living unsealed owner')
    with (NativeTransaction(settings,checkpoint=checkpoint,budget_guard=guard) if transaction is None else nullcontext(transaction)) as native:
        work=secure_fs.ensure_managed_directory(native.owned.path/uuid.uuid4().hex,parents=False,exist_ok=False).path
        home=secure_fs.ensure_managed_directory(work/'home',parents=False,exist_ok=False).path
        _configuration(home,settings);inputs={};sources={}
        for name in ('analysis_gui_preview.py','analysis_gui_pixels.py'):
            data=_read(native.resources/name);secure_fs.atomic_publish_new(work/name,data.content);sources[name]=data.sha256
        source=read_artifact(Path(geometry_directory) if geometry_directory is not None else output.parent/'geometry',geometry['artifacts']['brep'])
        if source.sha256!=geometry['geometry_sha256']:raise AnalysisError('ANALYSIS_PREVIEW_INVALID','preview BREP differs from the prepared geometry')
        secure_fs.atomic_publish_new(work/'geometry.brep',source.content);inputs['geometry.brep']=source.sha256
        ledger=build_load_ledger(spec,{'geometry':geometry,'regions':regions})
        for name,value in [('spec.json',spec),('geometry.json',geometry),('regions.json',regions),('ledger.json',ledger)]:
            content=_bytes(value);secure_fs.atomic_publish_new(work/name,content);inputs[name]=hashlib.sha256(content).hexdigest()
        identity={'design_id':spec['design_id'],'analysis_id':spec['analysis_id'],'spec_sha256':payload['spec_sha256'],'source_sha256':spec['source']['sha256'],'geometry_sha256':spec['source']['geometry_sha256'],'regions_sha256':spec['source']['regions_sha256'],'runtime_manifest_sha256':settings['runtime']['manifest_sha256']}
        request={'schema_version':'AnalysisNativePreviewRequest/v1','request_id':uuid.uuid4().hex,'expected_temp':_native_path(native.owned.path),'home':_native_path(home),'inputs':inputs,'identity':identity}
        secure_fs.atomic_publish_new(work/'request.json',_bytes(request))
        report=_launch_gui(native,work,home,settings,spec['budget'],'analysis_gui_preview',request,inputs,sources,output,checkpoint)
        expected_regions=sorted(item['region_id'] for item in regions['bindings']);expected_observations=sorted(item['id'] for item in spec['evaluation']['observables'])
        if report.get('status')!='passed' or 'error' in report or report.get('identity')!=identity or report.get('request_sha256')!=hashlib.sha256(_bytes(request)).hexdigest() or report.get('request_id')!=request['request_id'] or report.get('visible_region_ids')!=expected_regions or report.get('visible_observable_ids')!=expected_observations or report.get('source_fcstd_open_count')!=0 or [item['case_id'] for item in report['cases']]!=[case['case_id'] for case in spec['cases']]:raise AnalysisError('ANALYSIS_PREVIEW_INVALID','native preview must cover the same candidate, all cases, regions and observations')
        expected_names={'case-'+str(index).zfill(3)+'.png' for index in range(len(spec['cases']))}
        if set(report['artifacts'])!=expected_names or any(item['viewport_pixels']['status']!='passed' for item in report['cases']):raise AnalysisError('ANALYSIS_PREVIEW_INVALID','native preview has missing images or a blank viewport')
        images=[]
        for name,reference in sorted(report['artifacts'].items()):
            if reference['relative_path']!=name:raise AnalysisError('ANALYSIS_PREVIEW_INVALID','preview image reference changed')
            data=read_artifact(work,reference)
            if data.content[:8]!=b'\x89PNG\r\n\x1a\n':raise AnalysisError('ANALYSIS_PREVIEW_INVALID','preview is not a native PNG')
            secure_fs.atomic_publish_new(output/name,data.content);images.append(reference)
        if transaction is None:native.seal()
        checkpoint()
        receipt={'schema_version':'AnalysisPreviewReceipt/v1','status':'completed','operator':'assistant',**identity,'visible_region_ids':expected_regions,'visible_observable_ids':expected_observations,'images':images}
        content=canonical_record(receipt,schema_name='preview-receipt')
        secure_fs.atomic_publish_new(output/'preview.json',content)
        return parse_record(content.decode(),schema_name='preview-receipt')
