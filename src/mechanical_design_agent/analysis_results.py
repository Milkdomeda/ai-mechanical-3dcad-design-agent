"""Strict single-static-step fields and native RF; no missing-field fallback."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re

from .analysis_types import AnalysisError
from .analysis_units import normalize_quantity

_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?'
_HEADER = re.compile(r'(forces|total force) \(fx,fy,fz\) for set ([A-Za-z0-9_]+) and time\s+('+_NUMBER+')',re.I)


def read_nodal_rf(text, *, solver_version, fixed_node_ids, set_name, step, increment, time):
    try:
        if solver_version not in ('2.22','2.23') or type(text) is not str or len(text.encode('utf-8')) > 64*1024*1024: raise ValueError('unsupported native DAT identity or size')
        if not fixed_node_ids or len(set(fixed_node_ids)) != len(fixed_node_ids) or any(type(node) is not int or node <= 0 for node in fixed_node_ids): raise ValueError('fixed node inventory')
        rows, precision, total, mode, actual_step, actual_increment = {}, [[],[],[]], None, None, None, None
        def vector(tokens):
            if len(tokens)!=3 or any(not re.fullmatch(_NUMBER,value) for value in tokens):raise ValueError('native vector format')
            values=[Decimal(value.replace('d','e').replace('D','E')) for value in tokens]
            if any(not value.is_finite() or not math.isfinite(float(value)) for value in values):raise ValueError('nonfinite native RF')
            return values
        for line in text.splitlines():
            line=line.strip()
            if not line:continue
            match=re.fullmatch(r'S\s*T\s*E\s*P\s+(\d+)',line)
            if match:
                if actual_step is not None:raise ValueError('repeated step')
                actual_step=int(match[1]);continue
            match=re.fullmatch(r'INCREMENT\s+(\d+)',line)
            if match:
                if actual_increment is not None:raise ValueError('repeated increment')
                actual_increment=int(match[1]);continue
            header=_HEADER.fullmatch(line)
            if header:
                if actual_step!=step or actual_increment!=increment or header[2].upper()!=set_name.upper() or Decimal(header[3].replace('D','E').replace('d','e'))!=Decimal(str(time)):raise ValueError('RF belongs to another set or step')
                if header[1].lower()=='forces':
                    if mode is not None:raise ValueError('repeated nodal RF')
                    mode='nodal'
                else:
                    if mode!='nodal' or set(rows)!=set(fixed_node_ids):raise ValueError('missing nodal RF before total')
                    mode='total'
                continue
            tokens=line.split()
            if mode=='nodal':
                if len(tokens)!=4 or not tokens[0].isdigit():raise ValueError('nodal RF record')
                node=int(tokens[0]);values=vector(tokens[1:])
                if node not in fixed_node_ids or node in rows:raise ValueError('RF IDs differ from the fixed mesh')
                rows[node]=list(map(float,values))
                for i,value in enumerate(values):precision[i].append(float(Decimal(10)**value.as_tuple().exponent/2))
            elif mode=='total':
                values=vector(tokens);total=list(map(float,values));mode='done'
                for i,value in enumerate(values):precision[i].append(float(Decimal(10)**value.as_tuple().exponent/2))
            else:raise ValueError('unknown or trailing DAT record')
        if mode!='done' or total is None:raise ValueError('truncated DAT RF')
        bounds=[math.fsum(values) for values in precision]
        summed=[math.fsum(row[i] for row in rows.values()) for i in range(3)]
        if any(abs(actual-expected)>bound+32*math.ulp(max(abs(actual),abs(expected),1.)) for actual,expected,bound in zip(summed,total,bounds)):raise ValueError('nodal RF does not match its native printed total')
        ids=sorted(rows)
        return {'unit':'N','node_ids':ids,'values':[rows[node] for node in ids],'reported_total':total,'rounding_bound':bounds}
    except (ValueError,TypeError,OverflowError,InvalidOperation,UnicodeError) as exc:
        raise AnalysisError('RESULT_RF_INCOMPLETE','native per-node RF, its total and the exact requested static step are required') from exc


def validate_fields(bundle, mesh):
    try:
        return _validate_fields(bundle,mesh)
    except AnalysisError:
        raise
    except (TypeError,ValueError,KeyError,ArithmeticError,OverflowError) as exc:
        raise AnalysisError('RESULT_FIELD_INVALID','field bundle is malformed, incomplete or nonfinite') from exc


def _validate_fields(bundle, mesh):
    value=deepcopy(bundle)
    required={'schema_version','unit_system','coordinate_system','location','stress_recovery','step','mesh_sha256','node_ids','displacement','stress','von_mises','principal_stress','raw_rf'}
    if type(value) is not dict or set(value)-{'validity'}!=required or value['schema_version']!='AnalysisFieldBundle/v1' or value['unit_system']!='N-mm-s-tonne' or value['coordinate_system']!='global' or value['location']!='nodal' or value['stress_recovery']!='native_extrapolated_averaged_nodal':
        raise AnalysisError('RESULT_FORMAT_UNSUPPORTED','only the explicit native nodal field bundle is supported; integration peaks are not inferred')
    ids=value['node_ids']
    if type(ids) is not list or not ids or len(set(ids))!=len(ids) or any(type(node) is not int or node<=0 for node in ids) or set(ids)!=set(mesh['node_ids']) or value['mesh_sha256']!=mesh['mesh_sha256']:
        raise AnalysisError('RESULT_MESH_MISMATCH','every field needs the exact frozen mesh node inventory')
    if value['step']!={'number':1,'increment':1,'time':1.} or type(value['step']['number']) is not int or type(value['step']['increment']) is not int or type(value['step']['time']) not in (int,float):
        raise AnalysisError('RESULT_STEP_UNSUPPORTED','exactly the requested single static increment is required')
    def normalize(field,dimension,width,count):
        if type(field) is not dict or set(field)!={'unit','values'} or type(field['values']) is not list:
            raise AnalysisError('RESULT_FIELD_INVALID','field unit and finite array are required')
        if not field['values']:raise AnalysisError('RESULT_FIELD_EMPTY','a zero field is valid; an empty field is not')
        if len(field['values'])!=count:raise AnalysisError('RESULT_FIELD_INCOMPLETE','every node needs each requested field')
        result=[]
        for row in field['values']:
            values=[row] if width==1 else row
            if type(values) is not list or len(values)!=width or any(type(number) not in (int,float) or not math.isfinite(number) for number in values):raise AnalysisError('RESULT_FIELD_INVALID','native field values must be finite and have the declared arity')
            converted=[float(normalize_quantity(str(number),field['unit'],dimension=dimension)['value']) for number in values]
            result.append(converted[0] if width==1 else converted)
        return {'unit':{'length':'mm','stress':'MPa','force':'N'}[dimension],'values':result}
    for name,dimension,width in [('displacement','length',3),('stress','stress',6),('von_mises','stress',1),('principal_stress','stress',3)]:value[name]=normalize(value[name],dimension,width,len(ids))
    rf=value['raw_rf']
    if type(rf) is not dict or set(rf)!={'unit','node_ids','values','reported_total','rounding_bound'} or type(rf['node_ids']) is not list or any(type(node) is not int or node<=0 for node in rf['node_ids']) or len(set(rf['node_ids']))!=len(rf['node_ids']) or set(rf['node_ids'])!=set(mesh['fixed_node_ids']):
        raise AnalysisError('RESULT_RF_INCOMPLETE','all and only fixed nodes need native RF')
    normalized=normalize({'unit':rf['unit'],'values':rf['values']},'force',3,len(rf['node_ids']))
    rf.update(normalized)
    for name in ('reported_total','rounding_bound'):
        converted=normalize({'unit':bundle['raw_rf']['unit'],'values':[rf[name]]},'force',3,1)['values'][0]
        if name=='rounding_bound' and any(number<0 for number in converted):raise AnalysisError('RESULT_RF_INCOMPLETE','native rounding resolution must be nonnegative')
        rf[name]=converted
    for tensor,vm,principal in zip(value['stress']['values'],value['von_mises']['values'],value['principal_stress']['values']):
        xx,yy,zz,xy,xz,yz=tensor
        expected=math.sqrt(max(0.,((xx-yy)**2+(yy-zz)**2+(zz-xx)**2)/2+3*(xy*xy+xz*xz+yz*yz)))
        scale=max(1.,max(map(abs,tensor)),max(map(abs,principal)))
        tolerance=2e-12*scale
        # These are field consistency/roundoff checks, not strength tolerances.
        determinant=xx*yy*zz+2*xy*xz*yz-xx*yz*yz-yy*xz*xz-zz*xy*xy
        if vm<0 or abs(vm-expected)>tolerance or not principal[0]>=principal[1]>=principal[2] or abs(math.fsum(principal)-xx-yy-zz)>tolerance or abs(sum(p*p for p in principal)-(xx*xx+yy*yy+zz*zz+2*(xy*xy+xz*xz+yz*yz)))>tolerance*scale*4 or abs(math.prod(principal)-determinant)>tolerance*scale*scale*8:
            raise AnalysisError('RESULT_DERIVED_FIELD_MISMATCH','native principal or von Mises fields disagree with the full stress tensor')
    value['validity']='passed'
    return value


def extract_fields(deck, solver_outputs, settings, output_dir, *, transaction=None):
    from .analysis_native_transaction import NativeTransaction
    from .analysis_runtime_patch import PROFILE
    if settings['configuration']['runtime']['profile']!=PROFILE or deck['inspection']['rf_output']!='nodal_and_totals':
        raise AnalysisError('RESULT_RF_PROFILE_REQUIRED','actual per-node RF needs the reviewed R2 output profile')
    if transaction is None:
        with NativeTransaction(settings) as native:
            return _extract_fields(deck,solver_outputs,settings,output_dir,native)
    if transaction.settings!=settings:raise AnalysisError('FEA_RUNTIME_CHANGED','result extraction belongs to another runtime')
    return _extract_fields(deck,solver_outputs,settings,output_dir,transaction)


def _extract_fields(deck, solver_outputs, settings, output_dir, native):
    import hashlib
    import json
    from pathlib import Path
    from . import secure_fs
    from .analysis_deck import _parse_input
    from .analysis_native import _read
    from .analysis_equivalent_loads import recover_support_reactions
    if solver_outputs['input_sha256']!=deck['input_ref']['sha256'] or solver_outputs['outcome']['reason']!='exited' or solver_outputs['outcome']['returncode']!=0:
        raise AnalysisError('RESULT_SOLVER_NOT_SUCCESSFUL','failed or stale solver output cannot become a result')
    inp=Path(deck['directory'])/deck['input_ref']['relative_path']
    if _read(inp).sha256!=deck['input_ref']['sha256']:raise AnalysisError('DECK_INPUT_CHANGED','frozen native input changed before result extraction')
    parsed=_parse_input(inp,allowed_root=Path(deck['directory']))
    if parsed['_files'] != [deck['input_ref']]:raise AnalysisError('DECK_INPUT_CHANGED','result extraction requires the exact frozen single native input file')
    inverse={point:node for node,point in parsed['_nodes'].items()}
    fixed=sorted({inverse[point] for point,axis in parsed['boundary']})
    if len(parsed['_rf_sets'])!=1 or parsed['_rf_sets'][0]['node_ids']!=fixed:raise AnalysisError('RESULT_RF_SET_UNSUPPORTED','one exact global fixed-node RF set is required')
    root=secure_fs.validate_managed_path(Path(solver_outputs['directory']),allow_missing_leaf=False).path
    inputs={'mesh.unv':Path(solver_outputs['mesh_path'])}
    if _read(inputs['mesh.unv']).sha256!=deck['contract']['mesh_sha256']:raise AnalysisError('RESULT_MESH_MISMATCH','serialized mesh changed before extraction')
    for key,name in [('frd','Mesh.frd'),('dat','Mesh.dat')]:
        ref=solver_outputs['files'][key]
        if ref['relative_path']!=name:raise AnalysisError('RESULT_OUTPUT_INVALID','fixed native output names are required')
        path=root/name
        if _read(path).sha256!=ref['sha256']:raise AnalysisError('RESULT_OUTPUT_CHANGED','solver output identity or checksum changed')
        inputs[name]=path
    step={'number':1,'increment':1,'time':1.}
    contract={'mesh_sha256':deck['contract']['mesh_sha256'],'node_count':deck['inspection']['node_count'],'element_count':deck['inspection']['element_count'],'step':step,'source_input_sha256':deck['input_ref']['sha256'],'solver_nodes':[[node,*map(float,point)] for node,point in sorted(parsed['_nodes'].items())],'solver_version':settings['tools']['ccx']['version'],'raw_outputs_sha256':{'Mesh.frd':solver_outputs['files']['frd']['sha256'],'Mesh.dat':solver_outputs['files']['dat']['sha256']}}
    result=native.run('extract',contract,inputs,output_dir,timeout_seconds=deck['budget']['stage_seconds'],max_log_bytes=deck['budget']['max_disk_bytes'])
    if result['source_input_sha256']!=deck['input_ref']['sha256'] or set(result['artifacts'])!={'fields'}:raise AnalysisError('RESULT_OUTPUT_INVALID','native result belongs to another solver input')
    ref=result['artifacts']['fields'];content=_read(Path(output_dir)/ref['relative_path'])
    if content.sha256!=ref['sha256']:raise AnalysisError('RESULT_OUTPUT_CHANGED','native extracted fields changed')
    bundle=json.loads(content.content)
    bundle['raw_rf']=read_nodal_rf(_read(inputs['Mesh.dat']).content.decode('ascii'),solver_version=settings['tools']['ccx']['version'],fixed_node_ids=fixed,set_name=parsed['_rf_sets'][0]['set_name'],step=1,increment=1,time=1.)
    fields=validate_fields(bundle,{'mesh_sha256':contract['mesh_sha256'],'node_ids':list(parsed['_nodes']),'fixed_node_ids':fixed})
    applied_ref=deck['applied_loads_ref'];applied=_read(Path(deck['directory'])/applied_ref['relative_path'])
    if applied.sha256!=applied_ref['sha256']:raise AnalysisError('RESULT_APPLIED_LOADS_CHANGED','RF correction requires exact frozen discrete applied loads')
    table=json.loads(applied.content)['integrals']['native']['nodal_forces_N']
    rf=fields['raw_rf']
    reactions=recover_support_reactions(nodes={node:list(map(float,point)) for node,point in parsed['_nodes'].items()},fixed_node_ids=fixed,raw_rf=dict(zip(rf['node_ids'],rf['values'])),applied_nodal_forces={int(node):force for node,force in table.items()},moment_origin=list(map(float,deck['moment_origin_mm'])))
    data={'schema_version':'AnalysisExtractedFields/v1','source_input_sha256':deck['input_ref']['sha256'],'solver_version':settings['tools']['ccx']['version'],'fields':fields,'reactions':reactions,'numerical_acceptance':'not_executed'}
    encoded=json.dumps(data,sort_keys=True,allow_nan=False).encode()
    path=Path(output_dir)/'fields.json';secure_fs.atomic_publish_new(path,encoded)
    return {**data,'artifact':{'relative_path':path.name,'sha256':hashlib.sha256(encoded).hexdigest()}}
