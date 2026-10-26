"""Portable command surface using the same lazy analysis service as MCP."""
from pathlib import Path

from .analysis_protocol import parse_record,parse_json_value
from .analysis_types import AnalysisError


def add_analysis_parser(commands):
    parser=commands.add_parser('analysis',help='prepare, run and inspect isolated FreeCAD static FEA')
    subs=parser.add_subparsers(dest='analysis_command',required=True)
    for name in ('capabilities','schema','skill','setup','doctor','prepare','approve','run','status','cancel','report','confirm','open','open-status','close'):
        command=subs.add_parser(name);command.add_argument('--workspace',type=Path)
        if name=='schema':command.add_argument('--name',required=True);continue
        if name=='skill':command.add_argument('--destination',type=Path,required=True);continue
        if name=='setup':
            for key in ('source-root','destination','native-scratch-root'):command.add_argument('--'+key,type=Path,required=True)
            for role in ('freecadcmd','freecadgui','gmsh','ccx'):command.add_argument('--'+role,type=Path)
            command.add_argument('--software-opengl',action='store_true');continue
        if name=='doctor':command.add_argument('--qualify',action='store_true');continue
        if name=='capabilities':
            command.add_argument('--design-id');continue
        command.add_argument('--design-id',required=True);command.add_argument('--analysis-id',required=True)
        if name in ('open-status','close'):command.add_argument('--session-id',required=True);continue
        if name=='open':
            command.add_argument('--run-id',required=True);command.add_argument('--case-id',required=True);continue
        if name=='prepare':
            command.add_argument('--request','--request-file',dest='request',type=Path,required=True)
            command.add_argument('--expected-revision',type=int,default=-1)
        elif name in ('approve','run'):
            command.add_argument('--spec-sha256',required=True)
        if name in ('approve','run','cancel','confirm'):
            command.add_argument('--expected-revision',type=int,required=True)
        if name in ('approve','confirm'):
            decision=command.add_mutually_exclusive_group(required=True)
            decision.add_argument('--decision');decision.add_argument('--decision-file',type=Path)
        if name=='run':
            command.add_argument('--idempotency-key',required=True);command.add_argument('--retry-run-id')
        if name in ('cancel','report','confirm'):command.add_argument('--run-id',required=True)
        if name=='cancel':command.add_argument('--reason',required=True)
        if name=='status':
            command.add_argument('--run-id');command.add_argument('--cursor',type=int,default=0)
        if name=='report':
            command.add_argument('--action',choices=('get','rebuild_figures','finalize_visual'),default='get')
            command.add_argument('--evidence-refs',type=Path)
            command.add_argument('--expected-revision',type=int,default=-1)
        if name=='confirm':command.add_argument('--manifest-sha256',required=True)


def _text(path):
    # Bound the file read before parsing. JSON input is data, never Python code.
    try:
        with Path(path).open('rb') as stream:data=stream.read(4*1024*1024+1)
        if len(data)>4*1024*1024:raise AnalysisError('ANALYSIS_JSON_LIMIT','analysis input exceeds 4 MiB')
        return data.decode('utf-8')
    except (OSError,UnicodeError) as exc:raise AnalysisError('ANALYSIS_INPUT_UNREADABLE','analysis input must be an accessible UTF-8 file') from exc


def dispatch(arguments,service):
    name=arguments.analysis_command
    if name=='capabilities':return service.capabilities(design_id=arguments.design_id)
    if name=='schema':return service.schema(name=arguments.name)
    if name=='skill':return service.skill(destination=arguments.destination)
    if name=='doctor':return service.doctor(qualify=arguments.qualify)
    if name=='setup':return service.setup(source_root=arguments.source_root,destination=arguments.destination,native_scratch_root=arguments.native_scratch_root,tools={role:getattr(arguments,role) for role in ('freecadcmd','freecadgui','gmsh','ccx') if getattr(arguments,role) is not None},software_opengl=arguments.software_opengl)
    values={'design_id':arguments.design_id,'analysis_id':arguments.analysis_id}
    if name in ('approve','confirm'):
        decision=arguments.decision if arguments.decision_file is None else _text(arguments.decision_file)
        if not decision.strip() or len(decision)>4096:raise AnalysisError('ANALYSIS_INPUT_INVALID','a bounded nonempty UTF-8 decision is required')
    if name in ('open-status','close'):return getattr(service,name.replace('-','_'))(**values,session_id=arguments.session_id)
    if name=='open':return service.open(**values,run_id=arguments.run_id,case_id=arguments.case_id)
    if name=='prepare':
        values.update(request=parse_record(_text(arguments.request),schema_name='prepare-request'),expected_revision=None if arguments.expected_revision==-1 else arguments.expected_revision)
    elif name in ('approve','run'):
        values.update(spec_sha256=arguments.spec_sha256,expected_revision=arguments.expected_revision)
        if name=='approve':values['decision_text']=decision
        else:values.update(idempotency_key=arguments.idempotency_key,retry_run_id=arguments.retry_run_id)
    elif name=='status':values.update(run_id=arguments.run_id,cursor=arguments.cursor)
    elif name=='cancel':values.update(run_id=arguments.run_id,reason=arguments.reason,expected_revision=arguments.expected_revision)
    elif name=='report':
        refs=[] if arguments.evidence_refs is None else parse_json_value(_text(arguments.evidence_refs),expected=list)
        if any(type(ref) is not str for ref in refs):raise AnalysisError('ANALYSIS_VISUAL_UNVERIFIED','evidence references must be registered relative path strings')
        values.update(run_id=arguments.run_id,action=arguments.action,evidence_refs=refs,expected_revision=None if arguments.expected_revision==-1 else arguments.expected_revision)
    elif name=='confirm':values.update(run_id=arguments.run_id,manifest_sha256=arguments.manifest_sha256,decision_text=decision,expected_revision=arguments.expected_revision)
    return getattr(service,name)(**values)


def analysis_exit_code(value):
    """Analysis-only automation contract; engineering criteria stay in JSON."""
    if type(value) is not dict:return 0
    state=value.get('run_status',value.get('status',value.get('spec_status')))
    code=value.get('code','')
    if state in ('cancelled','timed_out','interrupted') or code in ('ANALYSIS_CANCELLED','ANALYSIS_TIMED_OUT','ANALYSIS_INTERRUPTED'):return 6
    if code.endswith('_CONFLICT') or code in ('ANALYSIS_SOURCE_CHANGED','ANALYSIS_SPEC_CHANGED','ANALYSIS_REVIEW_CHANGED','ANALYSIS_BUSY'):return 5
    if any(key in value and value[key]!='APPROVE' for key in ('approval_state','acceptance_state')):return 2
    if state=='needs_input' or value.get('spec_status')=='needs_input' or code in ('ANALYSIS_SPEC_NOT_APPROVED','ANALYSIS_REVIEW_NOT_READY','ANALYSIS_INPUT_UNREADABLE','ANALYSIS_INPUT_INVALID') or code.startswith(('ANALYSIS_JSON_','ANALYSIS_SCHEMA_')):return 2
    if state=='failed' or code.startswith(('SOLVER_','RESULT_','MESH_')):return 4
    if state in ('blocked','setup_required','unavailable'):return 3
    return 0
