"""Acyclic immutable FEA artifacts and completion-last verification."""
from collections import deque
import json
from pathlib import Path,PurePosixPath

from . import secure_fs
from .analysis_gui import verify_native_receipt
from .analysis_protocol import parse_record
from .analysis_revision import read_artifact
from .analysis_types import AnalysisError


def _name(value):
    if type(value) is not str or not value or len(value)>1024 or PurePosixPath(value).is_absolute() or any(part in ('','.','..') for part in value.split('/')) or any(char in value for char in '\\:\x00'):raise AnalysisError('ANALYSIS_MANIFEST_INVALID','artifact names must be distinct contained portable paths')
    return value


def validate_dependency_graph(artifacts,dependencies):
    if type(artifacts) is not dict or type(dependencies) is not dict:raise AnalysisError('ANALYSIS_MANIFEST_INVALID','artifact dependency maps are required')
    names={_name(name) for name in artifacts}
    if names&{'manifest.json','completion.json'}:raise AnalysisError('ANALYSIS_MANIFEST_CYCLE','manifest and completion hashes cannot appear in their own artifact inventory')
    indegree={name:0 for name in names};reverse={name:[] for name in names}
    for name,targets in dependencies.items():
        if name not in names or type(targets) is not list or len(set(targets))!=len(targets) or not set(targets)<=names:raise AnalysisError('ANALYSIS_MANIFEST_INVALID','every distinct dependency must name a listed artifact')
        indegree[name]=len(targets)
        for target in targets:reverse[target].append(name)
    ready=deque(name for name,count in indegree.items() if count==0);ordered=[]
    while ready:
        name=ready.popleft();ordered.append(name)
        for child in reverse[name]:
            indegree[child]-=1
            if indegree[child]==0:ready.append(child)
    if len(ordered)!=len(names):raise AnalysisError('ANALYSIS_MANIFEST_CYCLE','artifact dependency graph must be acyclic')
    return {'status':'passed','nodes':len(names)}


def _load(root,reference,schema=None):
    content=read_artifact(root,reference).content.decode('utf-8')
    return parse_record(content,schema_name=schema) if schema else json.loads(content)


def _inventory(root):
    entries={}
    for path in sorted(root.rglob('*')):
        located=secure_fs.validate_managed_path(path,allow_missing_leaf=False)
        if located.path.is_dir():continue
        name=_name(located.path.relative_to(root).as_posix())
        if name in ('manifest.json','completion.json'):continue
        data=secure_fs.read_managed_file(located.path)
        if data.identity!=located.identity or data.link_count!=1:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','delivery file identity changed or is multiply linked')
        entries[name]=data.sha256
    return entries


def _verify_completion(root,reference):
    """Read-only static verification, with no FreeCAD or solver invocation."""
    completion=_load(root,reference,'completion')
    folder=Path(root)/Path(reference['relative_path']).parent
    manifest=_load(folder,completion['manifest_ref'],'manifest')
    if completion['manifest_ref']['relative_path']!='manifest.json' or manifest['result_ref']!=completion['result_ref'] or completion['result_ref']['relative_path']!='report.json':raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','completion must point to this exact manifest and result')
    validate_dependency_graph(manifest['artifacts'],manifest['dependencies'])
    if _inventory(folder)!=manifest['artifacts']:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','delivery inventory or artifact SHA-256 changed')
    result=_load(folder,completion['result_ref'],'result')
    if any(any(record[key]!=completion[key] for key in ('design_id','analysis_id','run_id','spec_sha256')) for record in (manifest,result)) or manifest['delivery_version']!=completion['delivery_version'] or result['status']!='validated' or result['numerical_status']!='passed' or result['applicability_status']=='outside_scope':raise AnalysisError('ANALYSIS_COMPLETION_UNVERIFIED','invalid numerical evidence cannot become completed delivery')
    for ref in (completion['result_ref'],result['spec_ref'],result['attempt_ref'],result['gui_ref'],result['visual_ref']):
        if manifest['artifacts'].get(ref['relative_path'])!=ref['sha256']:raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','result reference is outside the exact manifest inventory')
    spec=_load(folder,result['spec_ref'],'spec');attempt=_load(folder,result['attempt_ref'])
    gui=_load(folder,result['gui_ref'],'gui-receipt');visual=_load(folder,result['visual_ref'],'visual-review')
    from .analysis_protocol import record_sha256
    if record_sha256(spec,schema_name='spec')!=result['spec_sha256'] or spec['status']!='approved' or attempt.get('native_integrity_sealed') is not True or attempt['spec_sha256']!=result['spec_sha256'] or attempt['numerical']['numerical_status']!=result['numerical_status'] or attempt['criteria']['criteria_status']!=result['criteria_status'] or attempt['numerical']['applicability_status']!=result['applicability_status']:raise AnalysisError('ANALYSIS_COMPLETION_UNVERIFIED','published summary differs from the immutable approved numerical evidence')
    conclusion='criteria_evaluated' if attempt['criteria']['checks'] and attempt['criteria']['criteria_status'] in ('passed','failed') else 'screening_only'
    if result['conclusion_level']!=conclusion:raise AnalysisError('ANALYSIS_COMPLETION_UNVERIFIED','no allowable means screening_only; numerical success cannot invent an engineering criterion')
    if gui['status']!='completed' or gui['kind']!='final_result' or gui['spec_sha256']!=result['spec_sha256'] or gui['run_id']!=result['run_id'] or visual['gui_receipt_sha256']!=result['gui_ref']['sha256'] or any(visual[key]!=result[key] for key in ('design_id','analysis_id','run_id','spec_sha256')):raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','exact registered native figures need a separate assistant visual review')
    case_ids=[case['case_id'] for case in spec['cases']]
    if manifest['case_ids']!=case_ids or [case['case_id'] for case in gui['cases']]!=case_ids:raise AnalysisError('ANALYSIS_DELIVERY_INCOMPLETE','all declared cases need native GUI evidence')
    if 'report.md' not in manifest['artifacts'] or not (folder/'report.md').stat().st_size:raise AnalysisError('ANALYSIS_DELIVERY_INCOMPLETE','both machine-readable and Markdown reports are required')
    images=[]
    attempt_root=folder/Path(result['attempt_ref']['relative_path']).parent
    for case in gui['cases']:
        case_id=case['case_id'];directory=folder/'gui/outputs'/case_id
        row=next(item for item in attempt['numerical']['finest'] if item['case_id']==case_id)
        deck=_load(attempt_root,row['artifacts']['deck']);fields=_load(attempt_root,row['artifacts']['fields'])
        report=json.loads(secure_fs.read_managed_file(directory/'response.json').content)
        native_request=json.loads(secure_fs.read_managed_file(directory/'request.json').content)
        evidence=verify_native_receipt(report,request=native_request,root=directory,deck=deck,fields=fields)
        saved=json.loads(secure_fs.read_managed_file(directory/'evidence.json').content)
        if saved!={**evidence,'native_integrity_sealed':True} or manifest['artifacts'].get('gui/outputs/'+case_id+'/evidence.json')!=case['evidence_ref']['sha256']:raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','native fields, export or saved FEM evidence changed')
        for field in evidence['display_fields'].values():
            name='gui/outputs/'+case_id+'/'+field['figure']
            images.append({'relative_path':name,'sha256':manifest['artifacts'][name]})
        name='gui/outputs/'+case_id+'/mesh-native.png';images.append({'relative_path':name,'sha256':manifest['artifacts'][name]})
    if sorted(visual['images'],key=lambda ref:ref['relative_path'])!=sorted(images,key=lambda ref:ref['relative_path']):raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','visual review must cover the exact native field figures and meshes')
    return {'completion':completion,'manifest':manifest,'result':result,'directory':str(folder)}


def verify_completion(root,reference):
    try:return _verify_completion(secure_fs.validate_managed_path(Path(root),allow_missing_leaf=False).path,reference)
    except AnalysisError:raise
    except (OSError,secure_fs.SecureFilesystemError,ValueError,KeyError,TypeError,StopIteration) as exc:
        raise AnalysisError('ANALYSIS_DELIVERY_INTEGRITY','completion or one of its exact artifacts is missing, malformed or changed') from exc
