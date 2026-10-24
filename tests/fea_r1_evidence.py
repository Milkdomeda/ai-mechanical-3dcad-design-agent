"""Assess R1 evidence without changing the original P0 assessment policy."""
import math
from fea_native_probe import _near,_number
from fea_r1_patch import PROFILE
from fea_r1_reference import graph_reference
from fea_r1_runtime import approved_module_digest


def assess_r1(worker):
    def mapping(v):return v if isinstance(v,dict) else {}
    def positive(v):return type(v) is int and v>0
    def vector(v):return isinstance(v,list) and len(v)==3 and all(_number(n) for n in v)
    def close(actual,expected,rtol):return vector(actual) and vector(expected) and all(_near(a,b,rtol) for a,b in zip(actual,expected))
    def numbers(v,expected):return isinstance(v,list) and len(v)==len(expected) and all(_near(a,b,1e-9) for a,b in zip(v,expected))
    worker=mapping(worker);checks={}
    before=mapping(worker.get('runtime_before'));after=mapping(worker.get('runtime_after'))
    checks['runtime']=before==after and before.get('profile')==PROFILE and before.get('module_sha256')==approved_module_digest()
    checks['worker']='error' not in worker and worker.get('auto_reopen_count')==0
    expected={'flat':[0,0,0,1/6,1/6,1/6]}
    for k in (.1,.5,1.):expected['graph_'+str(k)]=graph_reference(k)['weights']
    for scale in (1e-8,1.,1e8):expected['scaled_'+str(scale)]=expected['graph_0.5']
    basis=mapping(worker.get('basis'))
    checks['native_basis']=set(basis)==set(expected) and all(numbers(basis.get(k),v) for k,v in expected.items())
    legacy=mapping(worker.get('legacy'))
    checks['legacy_faces']=all(numbers(legacy.get(k),v) for k,v in {'T3':[1/6]*3,'Q4':[.25]*4,'Q8':[-1/12]*4+[1/3]*4}.items())
    rejections=mapping(worker.get('rejections'))
    checks['rejections']=all(isinstance(rejections.get(k),str) and rejections[k].startswith('R1_') for k in ('empty','duplicate','nonfinite','folded','misordered','duplicate_reference'))
    upstream=mapping(worker.get('upstream_tests'))
    checks['native_unittest']=type(upstream.get('run')) is int and upstream['run']>=5 and upstream.get('failures')==upstream.get('errors')==0
    selftest=mapping(worker.get('reference_selftest'))
    checks['independent_reference_selftest']=close(selftest.get('reference_moment_Nmm'),[-40/3,40/3,0],1e-9) and _near(selftest.get('virtual_work_Nmm'),-1/15,1e-9) and _near(selftest.get('reference_virtual_work_Nmm'),-1/15,1e-9) and _number(selftest.get('max_nodal_force_error_N')) and 0<=selftest['max_nodal_force_error_N']<=1e-9
    cases=mapping(worker.get('cases'))
    names={'mesh_5','mesh_4','mesh_3','reversed','permuted'}
    checks['case_inventory']=set(cases)==names
    elements=[];refinement=[];support_reactions={}
    for label,size,sign in (('mesh_5',5,-1),('mesh_4',4,-1),('mesh_3',3,-1),('reversed',4,1),('permuted',4,-1)):
        case=mapping(cases.get(label));reference=mapping(case.get('independent'));response=mapping(case.get('response'));mesh=mapping(case.get('mesh_inventory'))
        checks[label+'_force']=case.get('declared_force_N')==40 and case.get('direction_reversed') is (sign==1) and close(case.get('force_N'),[0,0,40*sign],1e-9)
        checks[label+'_moment']=close(case.get('moment_Nmm'),[200*sign,-2000*sign,0],.01) and vector(reference.get('reference_moment_Nmm')) and close(case.get('moment_Nmm'),reference['reference_moment_Nmm'],1e-9)
        error=reference.get('max_nodal_force_error_N')
        work,target=reference.get('virtual_work_Nmm'),reference.get('reference_virtual_work_Nmm')
        checks[label+'_independent']=_number(error) and 0<=error<=1e-9 and positive(reference.get('boundary_faces')) and positive(reference.get('compared_nodes')) and reference.get('reference_quadrature_order')==20 and _number(work) and _number(target) and _near(work,target,1e-9)
        checks[label+'_region']=case.get('load_region_valid') is True and positive(case.get('faces')) and case['faces']>=2 and case.get('mesh_size_mm')==size and positive(mesh.get('nodes')) and positive(mesh.get('elements'))
        displacement=response.get('tip_displacement_z_mm');inventory=mapping(response.get('result_inventory'))
        accounting=mapping(response.get('input_force_accounting'));raw=response.get('rf_N');fixed=accounting.get('constrained_cload_N');applied=accounting.get('applied_cload_N')
        reaction=[a-b for a,b in zip(raw,fixed)] if vector(raw) and vector(fixed) else None
        free=[a-b for a,b in zip(applied,fixed)] if vector(applied) and vector(fixed) else None
        support_reactions[label]=reaction
        checks[label+'_rf_accounting']=positive(accounting.get('constrained_nodes')) and isinstance(accounting.get('rf_nset'),str) and bool(accounting['rf_nset']) and close(applied,[0,0,40*sign],1e-9) and close(accounting.get('free_cload_N'),free,1e-9)
        checks[label+'_response']=response.get('finite') is True and _number(displacement) and displacement*sign>0 and _number(response.get('max_von_mises_MPa')) and response['max_von_mises_MPa']>0 and close(reaction,[0,0,-40*sign],.001) and inventory.get('complete') is True and inventory.get('nodes')==mesh.get('nodes')
        objects=case.get('native_objects',[])
        checks[label+'_native_objects']=bool(objects) and isinstance(objects,list) and all(isinstance(o,dict) and isinstance(o.get('type_id'),str) for o in objects) and any(o.get('type_id')=='Fem::ConstraintForce' for o in objects) and any(o['type_id'].startswith('Fem::FemMesh') for o in objects) and all(o.get('proxy_source') is None or (isinstance(o.get('proxy_source'),str) and o.get('source_within_runtime') is True) for o in objects)
        if label.startswith('mesh_'):
            elements.append(mesh.get('elements',0));refinement.append({'mesh_size_mm':size,'elements':mesh.get('elements'),'tip_displacement_z_mm':displacement,'max_von_mises_MPa':response.get('max_von_mises_MPa')})
    checks['refinement_counts']=all(type(v) is int for v in elements) and 0<elements[0]<elements[1]<elements[2]
    # Frozen, independent circular cantilever reference for this software fixture:
    # uniform transverse resultant F=40 N over L=100 mm, R=5 mm, E=200000 MPa.
    beam_deflection=40*100**3/(8*200000*(math.pi*5**4/4))
    checks['refinement_displacement_reference']=all(_near(abs(v['tip_displacement_z_mm']),beam_deflection,.05) if _number(v['tip_displacement_z_mm']) else False for v in refinement) and len(refinement)==3
    comparisons=[]
    for previous,current in zip(refinement,refinement[1:]):
        dz=[v['tip_displacement_z_mm'] for v in (previous,current)]
        stress=[v['max_von_mises_MPa'] for v in (previous,current)]
        change=abs(dz[1]-dz[0]) if all(_number(v) for v in dz) else None
        stress_change=abs(stress[1]-stress[0]) if all(_number(v) for v in stress) else None
        comparisons.append({'from_mesh_size_mm':previous['mesh_size_mm'],'to_mesh_size_mm':current['mesh_size_mm'],'displacement_change_mm':change,'displacement_change_over_reference':change/beam_deflection if change is not None else None,'max_von_mises_change_MPa':stress_change})
    checks['refinement_response']=len(comparisons)==2 and all(_number(v['displacement_change_mm']) and v['displacement_change_mm']<=.05*beam_deflection for v in comparisons)
    normal=mapping(mapping(cases.get('mesh_4')).get('response'))
    for label,sign in (('reversed',-1),('permuted',1)):
        response=mapping(mapping(cases.get(label)).get('response'))
        reference=normal.get('tip_displacement_z_mm')
        checks[label+'_response_invariance']=_number(reference) and _near(response.get('tip_displacement_z_mm'),sign*reference,1e-9) and _number(normal.get('max_von_mises_MPa')) and _near(response.get('max_von_mises_MPa'),normal['max_von_mises_MPa'],1e-9)
    passed=all(checks.values())
    return {'schema_version':'FEAR1Evidence/v1','automated_status':'passed' if passed else 'failed','status':'blocked' if passed else 'failed','native_gui_roundtrip':'not_executed','checks':checks,'refinement':refinement,'refinement_comparisons':comparisons,'displacement_reference_mm':beam_deflection,'convergence_status':'not_established','support_reactions':support_reactions,'rf_accounting_formula':'support reaction = raw CalculiX RF - native CLOAD at the printed constrained nodes','worker':worker}
