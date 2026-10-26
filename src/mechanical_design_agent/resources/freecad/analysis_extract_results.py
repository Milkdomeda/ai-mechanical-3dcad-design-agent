"""FreeCAD1.1.3 importer in a fresh document, never a customer FEM restore."""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import struct
import re
import traceback


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frd_coordinate(value):
    # ccx2.22/2.23 frd.c explicitly casts co[] to float before %12.5E.
    # Reproduce that serialization exactly; no geometric tolerance fallback.
    return float(format(struct.unpack('f',struct.pack('f',value))[0],'.5E'))


def inspect_frd_envelope(text,version,node_ids,element_ids):
    """Bounded fixed-profile inventory guard around the native importer.

    Native readers use dictionaries and can silently overwrite duplicate IDs.
    This guard only counts records/sections; it never interprets field values.
    """
    lines=text.splitlines()
    if not lines or lines[0].strip()!='1C' or lines[-1].strip()!='9999' or version not in ('2.22','2.23'):
        raise ValueError('RESULT_FRD_FORMAT_OR_TRAILER')
    versions=[line.strip() for line in lines if line.strip().startswith('1UVERSION')]
    if len(versions)!=1 or versions[0].split()!=['1UVERSION','Version',version]:raise ValueError('RESULT_FRD_SOLVER_VERSION')
    sections={};active=None
    for line in lines:
        token=line.strip()
        if token.startswith(('2C','3C')) or line[1:3]=='-4':
            if active is not None:raise ValueError('RESULT_FRD_UNCLOSED_SECTION')
            name=token[:2] if token.startswith(('2C','3C')) else token.split()[1]
            if name not in ('2C','3C','DISP','STRESS','TOSTRAIN','ERROR') or name in sections:raise ValueError('RESULT_FRD_DUPLICATE_OR_UNKNOWN_SECTION')
            sections[name]=set();active=name
        elif line[1:3]=='-1':
            if active is None:raise ValueError('RESULT_FRD_RECORD_OUTSIDE_SECTION')
            node=int(line[3:13])
            if node in sections[active]:raise ValueError('RESULT_FRD_DUPLICATE_ID')
            sections[active].add(node)
        elif line[1:3]=='-3':
            if active is None:raise ValueError('RESULT_FRD_UNEXPECTED_SECTION_END')
            active=None
    if active is not None or not {'2C','3C','DISP','STRESS','TOSTRAIN'}<=set(sections):raise ValueError('RESULT_FRD_INCOMPLETE_SECTIONS')
    for name,ids in sections.items():
        if ids!=set(element_ids if name=='3C' else node_ids):raise ValueError('RESULT_FRD_INCOMPLETE_IDS')


def values(obj,name,unit):
    data=getattr(obj,name)
    data=getattr(data,'Values',data)
    result=[]
    for value in data:
        if hasattr(value,'getValueAs'): value=value.getValueAs(unit).Value
        number=float(value)
        if not math.isfinite(number):raise ValueError('RESULT_NONFINITE_NATIVE_FIELD')
        result.append(number)
    return result


def extract(contract,paths):
    import FreeCAD as App
    import Fem
    import ObjectsFem
    from feminout.importCcxFrdResults import importFrd,read_frd_result
    if set(contract)!={'mesh_sha256','node_count','element_count','step','source_input_sha256','solver_nodes','solver_version','raw_outputs_sha256'} or digest(paths['mesh.unv'])!=contract['mesh_sha256'] or contract['step']!={'number':1,'increment':1,'time':1.}:
        raise ValueError('RESULT_CONTRACT_INVALID')
    if set(contract['raw_outputs_sha256'])!={'Mesh.frd','Mesh.dat'} or any(digest(paths[name])!=sha for name,sha in contract['raw_outputs_sha256'].items()):raise ValueError('RESULT_SOLVER_OUTPUT_CHANGED')
    mesh=Fem.read(str(paths['mesh.unv']))
    inspect_frd_envelope(paths['Mesh.frd'].read_text(encoding='ascii').rstrip(),contract['solver_version'],mesh.Nodes,mesh.Volumes)
    raw=read_frd_result(str(paths['Mesh.frd']))
    if set(raw['Nodes'])!=set(mesh.Nodes) or len(raw['Nodes'])!=contract['node_count'] or len(raw['Tetra10Elem'])!=contract['element_count'] or set(raw['Tetra10Elem'])!=set(mesh.Volumes):raise ValueError('RESULT_MESH_INVENTORY_MISMATCH')
    if any(tuple(mesh.getElementNodes(element))!=tuple(ids) for element,ids in raw['Tetra10Elem'].items()):raise ValueError('RESULT_ELEMENT_CONNECTIVITY_MISMATCH')
    expected_nodes={row[0]:row[1:] for row in contract['solver_nodes']}
    if set(expected_nodes)!=set(mesh.Nodes) or len(expected_nodes)!=len(contract['solver_nodes']):raise ValueError('RESULT_EXPECTED_NODE_INVENTORY')
    for node,point in raw['Nodes'].items():
        expected=expected_nodes[node]
        if len(expected)!=3:raise ValueError('RESULT_EXPECTED_COORDINATE_ARITY')
        for actual,value in zip((point.x,point.y,point.z),expected):
            if not math.isfinite(actual) or actual!=frd_coordinate(value):raise ValueError('RESULT_FRD_COORDINATE_REPRESENTATION_MISMATCH')
    if len(raw['Results'])!=1:raise ValueError('RESULT_STEP_COUNT_MISMATCH')
    step=raw['Results'][0]
    if step.get('time')!=1. or math.isfinite(step.get('number',float('nan'))) and step['number']!=0 or any(set(step.get(field,{}))!=set(mesh.Nodes) for field in ('disp','stress')):raise ValueError('RESULT_STEP_OR_FIELD_INCOMPLETE')
    doc=App.newDocument('AnalysisResults')
    try:
        analysis=ObjectsFem.makeAnalysis(doc,'Analysis')
        result=importFrd(str(paths['Mesh.frd']),analysis=analysis,result_name_prefix='CCX_',result_analysis_type='static')
        if result is None:raise ValueError('RESULT_NATIVE_IMPORT_EMPTY')
        ids=list(result.NodeNumbers)
        if len(ids)!=mesh.NodeCount or set(ids)!=set(mesh.Nodes):raise ValueError('RESULT_NATIVE_NODE_IDS_CHANGED')
        displacement=[[vector.x,vector.y,vector.z] for vector in result.DisplacementVectors]
        stress=list(map(list,zip(*(values(result,name,'MPa') for name in ('NodeStressXX','NodeStressYY','NodeStressZZ','NodeStressXY','NodeStressXZ','NodeStressYZ')))))
        principal=list(map(list,zip(*(values(result,name,'MPa') for name in ('PrincipalMax','PrincipalMed','PrincipalMin')))))
        return {'schema_version':'AnalysisFieldBundle/v1','unit_system':'N-mm-s-tonne','coordinate_system':'global','location':'nodal','stress_recovery':'native_extrapolated_averaged_nodal','step':contract['step'],'mesh_sha256':contract['mesh_sha256'],'node_ids':ids,'displacement':{'unit':'mm','values':displacement},'stress':{'unit':'MPa','values':stress},'von_mises':{'unit':'MPa','values':values(result,'vonMises','MPa')},'principal_stress':{'unit':'MPa','values':principal}}
    finally:App.closeDocument(doc.Name)


def main(request_path,output_path):
    import FreeCAD as App
    request=json.loads(Path(request_path).read_text(encoding='utf-8'))
    result={'schema_version':'AnalysisNativePhase/v1','phase':'extract','request_id':request['request_id'],'request_sha256':digest(request_path),'script_sha256':digest(__file__),'app_version':App.Version()[:3],'native_temp_path':App.getTempPath(),'status':'failed'}
    try:
        if set(request)!={'request_id','expected_temp','inputs','contract'} or set(request['inputs'])!={'mesh.unv','Mesh.frd','Mesh.dat'} or App.Version()[:3]!=['1','1','3'] or not os.path.samefile(App.getTempPath(),request['expected_temp']):raise ValueError('RESULT_REQUEST_INVALID')
        paths={name:Path(reference['path']) for name,reference in request['inputs'].items()}
        if any(digest(paths[name])!=reference['sha256'] for name,reference in request['inputs'].items()):raise ValueError('RESULT_INPUT_HASH_CHANGED')
        bundle=extract(request['contract'],paths)
        path=Path(output_path).parent/'native-fields.json'
        path.write_text(json.dumps(bundle,allow_nan=False),encoding='utf-8')
        result.update(status='passed',source_input_sha256=request['contract']['source_input_sha256'],artifacts={'fields':{'relative_path':path.name,'sha256':digest(path)}})
    except Exception:result['error']=traceback.format_exc()
    Path(output_path).write_text(json.dumps(result,allow_nan=False,indent=2),encoding='utf-8')


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
