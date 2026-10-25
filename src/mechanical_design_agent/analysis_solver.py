"""Owned fixed CalculiX invocation; exact frozen input is never rewritten."""
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import re
import time
import uuid

from . import secure_fs
from .analysis_native import _environment,_native_path,_read,verify_tool_identity,verify_native_modules
from .analysis_process import ProcessRequest
from .analysis_types import AnalysisError


def inspect_solver_completion(stdout,stderr,sta,*,version):
    if version not in ('2.22','2.23'):raise AnalysisError('SOLVER_OUTPUT_UNSUPPORTED','solver version needs separate compatibility qualification')
    text=stdout+'\n'+stderr
    forbidden=r'\*\s*ERROR\b|\bzero\s+pivot\b|\bnegative\s+pivot\b|\b(?:numerically\s+)?singular\s+matrix\b|\bnot\s+positive\s+definite\b|\b(?:nonpositive|negative|zero)\s+jacobian\b|\b(?:NaN|infinity|inf)\b|\bnot\s+converged\b'
    if re.search(forbidden,text,re.I) or len(re.findall(r'\bJob finished\b',stdout))!=1:raise AnalysisError('SOLVER_DIAGNOSTIC_FAILED','native solver did not finish cleanly or reported a reviewed numerical failure')
    lines=[line.strip() for line in sta.splitlines() if line.strip()]
    try:
        if len(lines)!=3 or lines[0]!='SUMMARY OF JOB INFORMATION' or lines[1].split()!=['STEP','INC','ATT','ITRS','TOT','TIME','STEP','TIME','INC','TIME']:raise ValueError('unknown STA header')
        row=lines[2].split()
        if len(row)!=7 or row[:4]!=['1','1','1','1'] or any(Decimal(value.replace('D','E'))!=1 for value in row[4:]):raise ValueError('not one successful linear increment')
    except (ValueError,ArithmeticError) as exc:raise AnalysisError('SOLVER_INCREMENT_INCOMPLETE','exactly one completed native static increment is required') from exc
    return {'status':'passed','solver_version':version,'step':1,'increment':1,'iterations':1,'diagnostics_scope':'Reviewed explicit numerical-error markers and single native STA increment; not a condition-number certification.'}


def solve_native(deck,mesh,settings,output_dir,*,native):
    from .analysis_deck import _parse_input
    if native.owned is None or native._sealed or native.settings!=settings:raise AnalysisError('ANALYSIS_NATIVE_PHASE_INVALID','solver requires its living unsealed native owner')
    inp=Path(deck['directory'])/deck['input_ref']['relative_path'];original=_read(inp)
    if original.sha256!=deck['input_ref']['sha256']:raise AnalysisError('DECK_INPUT_CHANGED','frozen native input changed before solve')
    parsed=_parse_input(inp,allowed_root=Path(deck['directory']))
    if parsed['_files']!=[deck['input_ref']]:raise AnalysisError('DECK_REFERENCE_UNSUPPORTED','this solver profile requires one frozen native input file')
    output=secure_fs.ensure_managed_directory(Path(output_dir),parents=False,exist_ok=False).path
    work=secure_fs.ensure_managed_directory(native.owned.path/uuid.uuid4().hex,parents=False,exist_ok=False).path
    secure_fs.atomic_publish_new(work/'Mesh.inp',original.content)
    native.checkpoint();verify_tool_identity(settings,'ccx');verify_native_modules(settings)
    request=ProcessRequest(argv=(_native_path(settings['tools']['ccx']['path']),'-i','Mesh'),cwd=work,stdout_path=work/'stdout.log',stderr_path=work/'stderr.log',env=_environment(work,native.owned.path),timeout_seconds=deck['budget']['stage_seconds'],max_log_bytes=deck['budget']['max_disk_bytes'])
    identity=native.controller.start(request);outcome=None;reason='interrupted';files={}
    try:
        while (outcome:=native.controller.poll(identity)) is None:
            native.checkpoint();time.sleep(.05)
    except AnalysisError as exc:
        reason={'ANALYSIS_CANCELLED':'cancelled','ANALYSIS_TIMED_OUT':'timed_out','ANALYSIS_BUDGET_EXCEEDED':'budget_exceeded'}.get(exc.code,'interrupted')
        raise
    finally:
        stopped=native.controller.terminate(identity,reason=reason)
        if outcome is None:outcome=stopped
        for name in ('stdout.log','stderr.log','Mesh.frd','Mesh.dat','Mesh.sta','Mesh.cvg','Mesh.inp'):
            if (work/name).exists():
                data=_read(work/name);secure_fs.atomic_publish_new(output/name,data.content)
                files[name]={'relative_path':name,'sha256':data.sha256}
        record={'schema_version':'AnalysisSolverProcess/v1','input_sha256':original.sha256,'outcome':asdict(outcome),'files':files}
        secure_fs.atomic_publish_new(output/'process.json',json.dumps(record,sort_keys=True).encode())
    native.checkpoint();verify_tool_identity(settings,'ccx');verify_native_modules(settings)
    if _read(inp).sha256!=original.sha256 or _read(work/'Mesh.inp').sha256!=original.sha256:raise AnalysisError('DECK_INPUT_CHANGED','native solver input changed during execution')
    if outcome.reason!='exited' or outcome.returncode!=0:
        code={'timed_out':'ANALYSIS_TIMED_OUT','budget_exceeded':'ANALYSIS_BUDGET_EXCEEDED','cancelled':'ANALYSIS_CANCELLED'}.get(outcome.reason,'SOLVER_PROCESS_FAILED')
        raise AnalysisError(code,'owned native solver failed; preserve raw logs and outputs')
    required=('stdout.log','stderr.log','Mesh.frd','Mesh.dat','Mesh.sta')
    if any(name not in files for name in required):raise AnalysisError('SOLVER_OUTPUT_INCOMPLETE','successful exit without all required fresh files is not a result')
    diagnostics=inspect_solver_completion(_read(output/'stdout.log').content.decode('utf-8'),_read(output/'stderr.log').content.decode('utf-8'),_read(output/'Mesh.sta').content.decode('ascii'),version=settings['tools']['ccx']['version'])
    secure_fs.atomic_publish_new(output/'diagnostics.json',json.dumps(diagnostics,sort_keys=True).encode())
    reference=mesh['receipt']['artifacts']['native_mesh']
    return {'directory':str(output),'input_sha256':original.sha256,'outcome':asdict(outcome),'mesh_path':str(Path(mesh['directory'])/reference['relative_path']),'files':{'frd':files['Mesh.frd'],'dat':files['Mesh.dat'],'sta':files['Mesh.sta']},'diagnostics':diagnostics}
