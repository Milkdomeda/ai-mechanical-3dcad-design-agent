"""Owned fresh W1 solver qualification; never mutates frozen native input."""
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid

from mechanical_design_agent import secure_fs
from mechanical_design_agent.analysis_native import _environment, _native_path, _read
from mechanical_design_agent.analysis_process import ProcessRequest


def solve_and_extract(deck, bundle, settings, native, output):
    from mechanical_design_agent.analysis_results import extract_fields
    output=Path(output);output.mkdir()
    work=native.owned.path/uuid.uuid4().hex;work.mkdir()
    inp=Path(deck['directory'])/deck['input_ref']['relative_path']
    original=_read(inp)
    assert original.sha256==deck['input_ref']['sha256']
    secure_fs.atomic_publish_new(work/'Mesh.inp',original.content)
    identity=native.controller.start(ProcessRequest(argv=(_native_path(settings['tools']['ccx']['path']),'-i','Mesh'),cwd=work,stdout_path=work/'stdout.log',stderr_path=work/'stderr.log',env=_environment(work,native.owned.path),timeout_seconds=deck['budget']['stage_seconds'],max_log_bytes=deck['budget']['max_disk_bytes']))
    try:
        while (outcome:=native.controller.poll(identity)) is None:
            native.checkpoint();time.sleep(.05)
    finally:native.controller.terminate(identity,reason='interrupted')
    (output/'process.json').write_text(json.dumps(asdict(outcome),indent=2))
    for name in ('stdout.log','stderr.log'):secure_fs.atomic_publish_new(output/name,_read(work/name).content)
    assert outcome.reason=='exited' and outcome.returncode==0
    assert _read(inp).sha256==original.sha256
    files={}
    for extension in ('frd','dat','sta'):
        raw=_read(work/('Mesh.'+extension))
        secure_fs.atomic_publish_new(output/('Mesh.'+extension),raw.content)
        files[extension]={'relative_path':'Mesh.'+extension,'sha256':raw.sha256}
    mesh_ref=bundle['receipt']['artifacts']['native_mesh']
    outputs={'directory':str(output),'input_sha256':original.sha256,'mesh_path':str(Path(bundle['directory'])/mesh_ref['relative_path']),'outcome':asdict(outcome),'files':files}
    return extract_fields(deck,outputs,settings,output.parent/(output.name+' fields'),transaction=native)


def verify_synthetic_balance(result, deck, validation):
    import math
    for name,unit in [('force','N'),('moment','Nmm')]:
        actual=result['reactions'][name+'_'+unit]
        expected=list(map(float,deck['contract']['case']['total_'+name+'_'+unit]))
        residual=math.sqrt(sum((a+b)**2 for a,b in zip(actual,expected)))
        scale=max(math.sqrt(sum(v*v for v in actual)),math.sqrt(sum(v*v for v in expected)))
        assert residual<=float(validation[name+'_atol']['value'])+float(validation[name+'_rtol']['value'])*scale, (name,actual,expected,residual,scale)
