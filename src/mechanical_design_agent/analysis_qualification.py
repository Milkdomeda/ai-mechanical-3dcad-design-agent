"""Fixed installed-package qualification and read-only activation checks.

Qualification exercises synthetic inputs only. It does not approve a physical
model, fabricate image inspection, or create an engineering delivery.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from . import secure_fs
from .analysis_budget import BudgetGuard
from .analysis_executor_identity import executor_identity,verify_executor_identity
from .analysis_geometry import inspect_geometry
from .analysis_gui import rebuild_native,verify_native_receipt
from .analysis_loads import build_load_ledger
from .analysis_native import _native_path,_read,verify_settings
from .analysis_native_transaction import NativeTransaction
from .analysis_pipeline import ExecutionState,_run_cases,_put
from .analysis_protocol import record_sha256,parse_json_value,_pairs,_constant,_check_json
from .analysis_qualification_inputs import make_inputs,q
from .analysis_revision import read_artifact,inventory
from .analysis_runtime import _process
from .analysis_types import AnalysisError
from .analysis_validation import evaluate_convergence


def _json(root,ref):
    # Hash-bound native mesh/deck/field artifacts are not 4 MiB caller requests.
    # Their total is bounded by the fixed qualification count/disk budget.
    data=read_artifact(root,ref).content
    if len(data)>256*1024*1024:raise AnalysisError('FEA_QUALIFICATION_INVALID','native qualification artifact exceeds its bounded reader')
    try:value=json.loads(data.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_constant)
    except (ValueError,UnicodeError,RecursionError) as exc:raise AnalysisError('FEA_QUALIFICATION_INVALID','native qualification artifact is invalid JSON') from exc
    _check_json(value)
    if type(value) is not dict:raise AnalysisError('FEA_QUALIFICATION_INVALID','native qualification artifact must be an object')
    return value


def _assess(spec,levels,numerical):
    if len(levels)!=6 or {(row['case_id'],row['level']) for row in levels}!={(case,level) for case in ('static','reverse') for level in range(3)}:raise AnalysisError('FEA_QUALIFICATION_FAILED','qualification needs both axial cases at all three fixed mesh levels')
    actual=evaluate_convergence(spec,levels)
    if numerical!=actual or actual['numerical_status']!='passed' or actual['applicability_status']!='limited_screening':raise AnalysisError('FEA_QUALIFICATION_FAILED','qualification convergence, equilibrium or applicability failed')
    for row in levels:
        observations={value['field']:value['value'] for value in row['observables']}
        if set(observations)!={'displacement_magnitude','von_mises'} or abs(observations['displacement_magnitude']-.0002)>.0002*.02 or abs(observations['von_mises']-.4)>.4*.02:raise AnalysisError('FEA_QUALIFICATION_FAILED','synthetic native response differs from the analytic axial benchmark')
    return actual


def _case(root,state,case_id):
    rows=[row for row in state['numerical']['finest'] if row['case_id']==case_id]
    if len(rows)!=1:raise AnalysisError('FEA_QUALIFICATION_FAILED','each synthetic case needs one finest mesh')
    row=rows[0]
    deck=_json(root,row['artifacts']['deck']);solver=_json(root,row['artifacts']['solver']);fields=_json(root,row['artifacts']['fields']);mesh=state['meshes'][str(row['level'])]
    return deck,mesh,fields,solver


def _verify_receipt(root,receipt,*,checkpoint=None):
    checkpoint=checkpoint or (lambda:None);checkpoint()
    if receipt.get('schema_version')!='AnalysisQualification/v1' or receipt.get('status')!='passed' or receipt.get('native_integrity_sealed') is not True or receipt.get('visual_review')!='not_claimed' or receipt.get('engineering_approval')!='not_claimed':raise AnalysisError('FEA_QUALIFICATION_INVALID','a caller-provided pass flag is not native qualification evidence')
    actual=[ref for ref in inventory(root,checkpoint=checkpoint) if ref['relative_path']!='qualification.json']
    if actual!=receipt['files']:raise AnalysisError('FEA_QUALIFICATION_CHANGED','sealed native qualification artifacts changed')
    spec=_json(root,receipt['spec_ref']);state=_json(root,receipt['axial_ref']);_assess(spec,state['levels'],state['numerical'])
    checkpoint()
    if [item['case_id'] for item in receipt['gui']]!=['static','reverse']:raise AnalysisError('FEA_QUALIFICATION_INVALID','both native result cases must be exercised')
    axial_root=root/'axial'
    for item in receipt['gui']:
        checkpoint()
        directory=root/'gui'/item['case_id'];deck,mesh,fields,solver=_case(axial_root,state,item['case_id'])
        request=json.loads(_read(directory/'request.json').content);report=json.loads(_read(directory/'response.json').content)
        checked=verify_native_receipt(report,request=request,root=directory,deck=deck,fields=fields)
        evidence=_json(root,item['evidence_ref'])
        if evidence!={**checked,'native_integrity_sealed':True}:raise AnalysisError('FEA_QUALIFICATION_CHANGED','native GUI fields, task, output or producer identity changed')
        checkpoint()
    curve=_json(root,receipt['curved_ref']);deck=curve['deck']
    objects=deck['contract']['case']['native_objects']
    if len(objects)!=1 or len(objects[0]['face_indices'])!=2 or any(abs(float(v)-expected)>1e-9 for v,expected in zip(deck['inspection']['force_N'],[0,0,-40])):raise AnalysisError('FEA_QUALIFICATION_FAILED','curved multi-reference load must retain one exact global total')
    checkpoint()


def marker_path(settings):return Path(settings['configuration_path']).with_suffix('.qualification.json')


def require_qualification(settings,*,checkpoint=None):
    checkpoint=checkpoint or (lambda:None);checkpoint()
    path=marker_path(settings)
    if not path.exists():raise AnalysisError('FEA_QUALIFICATION_REQUIRED','run analysis doctor --qualify for this exact installed package and isolated runtime')
    try:
        marker=parse_json_value(_read(path).content.decode('utf-8'),expected=dict)
        if set(marker)!={'schema_version','configuration_sha256','runtime_manifest_sha256','executor_sha256','receipt_path','receipt_sha256'} or marker['schema_version']!='AnalysisQualificationBinding/v1':raise AnalysisError('FEA_QUALIFICATION_INVALID','qualification binding is incomplete')
        if marker['configuration_sha256']!=settings['configuration_sha256'] or marker['runtime_manifest_sha256']!=settings['runtime']['manifest_sha256']:raise AnalysisError('FEA_QUALIFICATION_STALE','runtime or configuration changed; run explicit qualification again')
        verify_executor_identity(marker['executor_sha256'])
        checkpoint()
        located=secure_fs.validate_managed_path(Path(marker['receipt_path']),allow_missing_leaf=False).path
        if located.name!='qualification.json':raise AnalysisError('FEA_QUALIFICATION_INVALID','select only the immutable fixed qualification receipt')
        record=_read(located)
        if record.sha256!=marker['receipt_sha256']:raise AnalysisError('FEA_QUALIFICATION_CHANGED','selected qualification receipt changed')
        receipt=parse_json_value(record.content.decode('utf-8'),expected=dict)
        if any(receipt.get(key)!=marker[key] for key in ('configuration_sha256','runtime_manifest_sha256','executor_sha256')):raise AnalysisError('FEA_QUALIFICATION_STALE','selected receipt belongs to a different installed executor or native runtime')
        _verify_receipt(located.parent,receipt,checkpoint=checkpoint)
        return {'status':'ready','code':'FEA_QUALIFIED','qualification_ref':{'path':str(located),'sha256':record.sha256},'executor_sha256':marker['executor_sha256'],'visual_review':'not_claimed','engineering_certification':False}
    except AnalysisError:raise
    except (OSError,secure_fs.SecureFilesystemError,ValueError,KeyError,TypeError,UnicodeError) as exc:raise AnalysisError('FEA_QUALIFICATION_INVALID','qualification receipt cannot be verified; preserve evidence and run explicit qualification again') from exc


def qualify_runtime(settings,output_dir):
    """Run fixed native benchmarks and publish activation only after full seal."""
    code=executor_identity();root=secure_fs.ensure_managed_directory(Path(output_dir),parents=False,exist_ok=False).path
    native=None;budget={'wall_seconds':1800,'stage_seconds':300,'threads':1,'max_nodes':100000,'max_elements':100000,'max_disk_bytes':2000000000,'max_attempts':1,'memory_mode':'observed_only','max_memory_mb':4096}
    from .analysis_process import ProcessController
    controller=ProcessController()
    guard=BudgetGuard(budget,roots=lambda:[root],cancelled=lambda:False,memory_observer=controller.observed_memory_bytes,integrity_checkpoint=lambda:verify_executor_identity(code['sha256']))
    native=NativeTransaction(settings,controller=controller,checkpoint=guard.checkpoint,budget_guard=guard)
    try:
        with native:
            work=secure_fs.ensure_managed_directory(native.owned.path/'fixture',parents=False,exist_ok=False).path
            home=secure_fs.ensure_managed_directory(work/'home',parents=False,exist_ok=False).path
            for name in ('user.cfg','system.cfg'):secure_fs.atomic_publish_new(home/name,b'<FCParameters><FCParamGroup Name="Root"/></FCParameters>')
            script=_read(native.resources/'analysis_qualification_geometry.py');secure_fs.atomic_publish_new(work/'fixture.py',script.content)
            args=[_native_path(work/'fixture.py'),_native_path(work)];statement='import sys,runpy;sys.dont_write_bytecode=True;sys.argv='+ascii(args)+";runpy.run_path(sys.argv[0],run_name='__main__')"
            logs=secure_fs.ensure_managed_directory(root/'fixture-process',parents=False,exist_ok=False).path
            _process([settings['tools']['freecadcmd']['path'],'-u',home/'user.cfg','-s',home/'system.cfg','-c',statement],logs,home,native.owned.path)
            if _read(work/'fixture.py').sha256!=script.sha256:raise AnalysisError('FEA_QUALIFICATION_CHANGED','fixed synthetic geometry source changed')
            fixtures=secure_fs.ensure_managed_directory(root/'fixtures',parents=False,exist_ok=False).path
            for name in ('box.FCStd','cylinder.FCStd','created.json'):secure_fs.atomic_publish_new(fixtures/name,_read(work/name).content)
            geometries={};inputs={}
            for kind in ('box','cylinder'):
                source=fixtures/(kind+'.FCStd');request={'schema_version':'AnalysisGeometryRequest/v1','source_sha256':_read(source).sha256,'included_object':'Solid','excluded_objects':[],'partitions':[{'axis':'y','coordinate':q(5,'mm')}] if kind=='cylinder' else [],'volume_tolerance':q('.000001','mm^3')}
                path=root/('geometry-'+kind);geometry=inspect_geometry(source,request,settings,path,transaction=native);geometries[kind]=path/'geometry.brep';inputs[kind]=make_inputs(geometry,settings,curved=kind=='cylinder')
                _put(path,'geometry.json',geometry)
            spec,regions=inputs['box'];geometry=json.loads(_read(root/'geometry-box/geometry.json').content)
            spec_ref=_put(root,'spec.json',spec);_put(root,'regions.json',regions)
            axial=secure_fs.ensure_managed_directory(root/'axial',parents=False,exist_ok=False).path;secure_fs.ensure_managed_directory(axial/'meshes',parents=False,exist_ok=False)
            execution=ExecutionState({'spec_sha256':record_sha256(spec,schema_name='spec')},settings,{'spec':spec,'geometry_path':geometries['box'],'geometry':geometry,'regions':regions,'ledger':build_load_ledger(spec,{'geometry':geometry,'regions':regions})},axial,native,guard,lambda *args,**kwargs:None)
            numerical=_run_cases(execution);_assess(spec,execution.levels,numerical)
            state={'levels':execution.levels,'numerical':numerical,'meshes':{str(k):v for k,v in execution.meshes.items()}}
            axial_ref=_put(root,'axial.json',state)
            curve_spec,curve_regions=inputs['cylinder'];curve_geometry=json.loads(_read(root/'geometry-cylinder/geometry.json').content)
            from .analysis_mesh import generate_mesh,plan_mesh_levels
            from .analysis_deck import build_native_deck
            level=plan_mesh_levels(curve_spec)[0];curve_root=secure_fs.ensure_managed_directory(root/'curved',parents=False,exist_ok=False).path
            mesh_receipt=generate_mesh(geometries['cylinder'],curve_regions,level,settings,curve_root/'mesh',transaction=native)
            mesh={'directory':str(curve_root/'mesh'),'receipt':mesh_receipt,'geometry_manifest':curve_geometry,'region_bindings':curve_regions}
            deck=build_native_deck(curve_spec,geometries['cylinder'],mesh,curve_root/'deck',settings,case_id='static',transaction=native)
            curved_ref=_put(root,'curved.json',{'spec':curve_spec,'regions':curve_regions,'mesh':mesh,'deck':deck,'scope':'curved multi-reference native input only; no curved strength certification'})
            gui_root=secure_fs.ensure_managed_directory(root/'gui',parents=False,exist_ok=False).path;gui=[]
            for case in spec['cases']:
                case_id=case['case_id'];deck,mesh,fields,solver=_case(axial,state,case_id)
                result=rebuild_native(deck,mesh,fields,solver,geometries['box'],settings,gui_root/case_id,transaction=native,guard=guard)
                gui.append((case_id,{key:value for key,value in result.items() if key!='evidence_ref'}))
            native.seal();guard.checkpoint(force=True)
        gui_refs=[]
        for case_id,evidence in gui:
            evidence['native_integrity_sealed']=True;ref=_put(root,'gui/'+case_id+'/evidence.json',evidence);gui_refs.append({'case_id':case_id,'evidence_ref':ref})
        receipt={'schema_version':'AnalysisQualification/v1','status':'passed','configuration_sha256':settings['configuration_sha256'],'runtime_manifest_sha256':settings['runtime']['manifest_sha256'],'executor_sha256':code['sha256'],'native_integrity_sealed':True,'spec_ref':spec_ref,'axial_ref':axial_ref,'curved_ref':curved_ref,'gui':gui_refs,'files':inventory(root),'budget':guard.receipt(),'visual_review':'not_claimed','engineering_approval':'not_claimed','engineering_certification':False}
        reference=_put(root,'qualification.json',receipt);_verify_receipt(root,receipt,checkpoint=guard.checkpoint);guard.checkpoint(force=True)
        marker={'schema_version':'AnalysisQualificationBinding/v1','configuration_sha256':settings['configuration_sha256'],'runtime_manifest_sha256':settings['runtime']['manifest_sha256'],'executor_sha256':code['sha256'],'receipt_path':str(root/'qualification.json'),'receipt_sha256':reference['sha256']}
        path=marker_path(settings);content=json.dumps(marker,sort_keys=True).encode()
        with secure_fs.exclusive_file_lock(path.with_suffix('.lock')):
            if path.exists():secure_fs.atomic_replace(path,content)
            else:secure_fs.atomic_publish_new(path,content)
        return require_qualification(settings)
    except BaseException as exc:
        _put(root,'qualification-failure.json',{'status':'failed','code':exc.code if isinstance(exc,AnalysisError) else type(exc).__name__,'activation':'not_published','engineering_certification':False})
        raise
    finally:controller.terminate_owned(reason='interrupted')
