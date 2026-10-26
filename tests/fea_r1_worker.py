"""Native R1 tests and standard FEM cases; no persisted document restore."""
import sys
sys.dont_write_bytecode=True
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import traceback
import unittest
sys.path.insert(0,str(Path(__file__).parent))
from fea_helpers import build_case,validate_result_fields,_reaction
from fea_r1_patch import ASSETS
from fea_r1_reference import graph_points,independent_input_reference,support_load_accounting
from fea_r1_runtime import loaded_module_receipt,native_identity,startup_receipt,verify_runtime,canonical_path


def native_weights(points):
    import FreeCAD as App
    from femmesh import meshtools
    nodes={i:App.Vector(*p) for i,p in enumerate(points,1)}
    return [float(w) for _,w in meshtools.get_ref_facenodes_areas(nodes,{1:tuple(nodes)})]


def basis_evidence():
    data={'flat':native_weights(graph_points(0))}
    for k in (.1,.5,1.):data['graph_'+str(k)]=native_weights(graph_points(k))
    for scale in (1e-8,1.,1e8):
        points=[tuple(scale*(v+offset) for v,offset in zip((p[2],p[0],p[1]),(3.,-7.,2.))) for p in graph_points()]
        data['scaled_'+str(scale)]=[v/scale**2 for v in native_weights(points)]
    square=((0,0,0),(1,0,0),(1,1,0),(0,1,0))
    legacy={'T3':native_weights(square[:3]),'Q4':native_weights(square),'Q8':native_weights(square+((.5,0,0),(1,.5,0),(.5,1,0),(0,.5,0)))}
    errors={}
    for defect in ('empty','duplicate','nonfinite','folded','misordered'):
        points=list(graph_points())
        if defect=='empty':points=[]
        if defect=='duplicate':points[1]=points[0]
        if defect=='nonfinite':points[4]=(math.nan,.5,0)
        if defect=='folded':points[3]=(2.,2.,0.)
        if defect=='misordered':points[3],points[4]=points[4],points[3]
        try:
            # The native helper is called directly for the intentionally empty
            # fixture: legacy empty-area API logs and returns [] by design.
            from femmesh import meshtools
            meshtools.t6_weights(points);errors[defect]='accepted'
        except ValueError as exc:errors[defect]=str(exc)
    return data,legacy,errors


def upstream_tests(output):
    spec=importlib.util.spec_from_file_location('native_faceforce_unittest',ASSETS/'native_faceforce_unittest.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    stream=io.StringIO();result=unittest.TextTestRunner(stream=stream).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    (output/'native-unittest.log').write_text(stream.getvalue(),encoding='utf-8')
    return {'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}


def reference_selftest(output):
    points=((0,0,0),(1,0,0),(0,1,0),(0,0,1),(.5,0,0),(.5,.5,0),(0,.5,0),(0,0,.5),(.5,0,.5),(0,.5,.5))
    text='*NODE, NSET=Nall\n'+''.join(f'{i},{p[0]},{p[1]},{p[2]}\n' for i,p in enumerate(points,1))
    text+='*ELEMENT, TYPE=C3D10\n1,1,2,3,4,5,6,7,8,9,10\n*CLOAD\n5,3,-13.333333333333333\n6,3,-13.333333333333333\n7,3,-13.333333333333333\n'
    path=output/'reference-flat.inp';path.write_text(text,encoding='utf-8')
    return independent_input_reference(path,{1,2,3,5,6,7},-40.)


def case_hook(*,doc,analysis,mesh,fea,solid,load,load_faces,inp,output,config):
    import FreeCAD as App
    from femmesh import meshtools
    allowed={node for name in load_faces for node in mesh.FemMesh.getNodesByFace(solid.Shape.getElement(name))}
    sign=1 if load.Reversed else -1
    reference=independent_input_reference(inp,allowed,40.*sign)
    references=load.References
    alias=doc.addObject('Part::Feature','ReferenceAlias');alias.Shape=solid.Shape
    try:
        load.References=[(solid,list(load_faces)),(alias,[load_faces[0]])]
        table={e:mesh.FemMesh.getElementNodes(e) for e in mesh.FemMesh.Volumes}
        try:
            meshtools.get_force_obj_face_nodeload_table(mesh.FemMesh,table,mesh.FemMesh.Nodes,load)
            duplicate='accepted'
        except ValueError as exc:duplicate=str(exc)
    finally:
        load.References=references
        doc.removeObject(alias.Name)
    with (output/'ccx.log').open('w',encoding='utf-8') as stream:
        result=subprocess.run([config['tools']['ccx']['path'],'-i',inp.stem],cwd=output,stdout=stream,stderr=subprocess.STDOUT,timeout=120)
    if result.returncode:raise RuntimeError('R1_NATIVE_CCX_FAILED')
    fea.load_results()
    native=next(o for o in analysis.Group if hasattr(o,'vonMises'))
    ids=list(native.NodeNumbers);vectors=list(native.DisplacementVectors)
    inventory=validate_result_fields(mesh.FemMesh.Nodes,ids,[(v.x,v.y,v.z) for v in vectors],list(native.NodeStressXX),list(native.vonMises))
    tips=[v.z for n,v in zip(ids,vectors) if abs(mesh.FemMesh.getNodeById(n).x-100)<1e-6]
    if not tips:raise RuntimeError('R1_NO_TIP_RESULTS')
    response={'tip_displacement_z_mm':math.fsum(tips)/len(tips),'max_von_mises_MPa':max(native.vonMises),'rf_N':_reaction(inp.with_suffix('.dat').read_text(encoding='utf-8')),'input_force_accounting':support_load_accounting(inp),'finite':True,'result_inventory':inventory}
    root=canonical_path(json.loads(Path(config['runtime']['manifest_path']).read_text(encoding='utf-8'))['runtime_root'])
    objects=[]
    for obj in analysis.Group:
        proxy=getattr(obj,'Proxy',None);module=sys.modules.get(type(proxy).__module__) if proxy is not None else None
        source=getattr(module,'__file__',None)
        objects.append({'name':obj.Name,'type_id':obj.TypeId,'proxy_source':source,'source_within_runtime':bool(source) and canonical_path(source).is_relative_to(root)})
    doc.recompute();doc.saveAs(str(output/'analysis.FCStd'))
    return {'independent':reference,'response':response,'duplicate_reference':duplicate,'native_objects':objects,'input_file':inp.name,'declared_force_N':float(App.Units.Quantity(load.Force.getValueAs('N')).Value),'direction_reversed':bool(load.Reversed)}


def main(request,response):
    import FreeCAD as App
    from femmesh import meshtools
    config=json.loads(request.read_text(encoding='utf-8'))
    output=response.parent
    report={'schema_version':'FEAR1Worker/v1','auto_reopen_count':0,'cases':{}}
    original=App.openDocument
    def deny(*args,**kwargs):
        report['auto_reopen_count']+=1
        raise RuntimeError('R1 forbids persisted document restore')
    App.openDocument=deny
    try:
        expected=verify_runtime(config,full_tree=False)
        report['runtime_before']=loaded_module_receipt(expected,meshtools)
        if native_identity()!=expected['native_identity']:raise ValueError('R1_NATIVE_VERSION_IDENTITY')
        report['startup_paths']=startup_receipt(output)
        report['native_temp_path']=App.getTempPath()
        import os
        if not os.path.samefile(report['native_temp_path'],config['_expected_native_temp']):raise ValueError('R1_NATIVE_TEMP_MISMATCH')
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/Gmsh').SetString('gmshBinaryPath',config['tools']['gmsh']['path'])
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/Gmsh').SetInt('NumOfThreads',1)
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/Ccx').SetString('ccxBinaryPath',config['tools']['ccx']['path'])
        report['basis'],report['legacy'],report['rejections']=basis_evidence()
        report['upstream_tests']=upstream_tests(output)
        report['reference_selftest']=reference_selftest(output)
        for name,size,reversed_force,permuted in (('mesh_5',5,False,False),('mesh_4',4,False,False),('mesh_3',3,False,False),('reversed',4,True,False),('permuted',4,False,True)):
            spec={'name':'curved_multiface','length_mm':100,'width_mm':10,'height_mm':10,'youngs_modulus_MPa':200000,'poisson_ratio':.3,'force_N':40,'mesh_size_mm':size}
            value=build_case(spec,config,output/name,evidence_hook=case_hook,reverse_force=reversed_force,permute_references=permuted)
            extras=value.pop('r1');duplicate=extras.pop('duplicate_reference')
            if name=='mesh_5':report['rejections']['duplicate_reference']=duplicate
            if not duplicate.startswith('R1_'):raise ValueError('R1_DUPLICATE_REFERENCE_ACCEPTED')
            value.update(extras,mesh_size_mm=size)
            report['cases'][name]=value
        report['runtime_after']=loaded_module_receipt(expected,meshtools)
    except Exception:report['error']=traceback.format_exc()
    finally:App.openDocument=original
    response.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


if __name__=='__main__':main(*map(Path,sys.argv[1:]))
