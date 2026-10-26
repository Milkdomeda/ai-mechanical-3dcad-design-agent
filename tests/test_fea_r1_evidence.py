import copy
import pytest


def valid_worker():
    # Hand-authored boundary data, never presented as live FEA evidence.
    weights={
        'flat': [0.,0.,0.,1/6,1/6,1/6],
        'graph_0.1': [-2.77480900158604e-05,8.31850422878741e-05,-2.77480900158604e-05,0.16683318486985302,0.16683318486985302,0.16672219250978956],
    }
    # Filled literal golden values below are from the analytic 1-D reference,
    # not from the native implementation or the assessor.
    weights['graph_0.5']=[-0.0006769129788454595,0.0019976474249444806,-0.0006769129788454595,0.17074533398872924,0.17074533398872924,0.1680376820733474]
    weights['graph_1.0']=[-0.0025359589638058837,0.007209170989181252,-0.0025359589638058837,0.18210814974708103,0.18210814974708103,0.17196431389185748]
    for label in ('scaled_1e-08','scaled_1.0','scaled_100000000.0'):weights[label]=weights['graph_0.5'][:]
    cases={}
    for label,size,sign in (('mesh_5',5,-1),('mesh_4',4,-1),('mesh_3',3,-1),('reversed',4,1),('permuted',4,-1)):
        node_count,element_count={5:(200,100),4:(300,160),3:(500,300)}[size]
        cases[label]={'mesh_size_mm':size,'force_N':[0,0,40*sign],'moment_Nmm':[200*sign,-2000*sign,0],
            'load_region_valid':True,'mesh_inventory':{'nodes':node_count,'elements':element_count},'faces':2,
            'declared_force_N':40,'direction_reversed':sign==1,
            'independent':{'reference_moment_Nmm':[200*sign,-2000*sign,0],'virtual_work_Nmm':-1000*sign,'reference_virtual_work_Nmm':-1000*sign,'max_nodal_force_error_N':0.,'boundary_faces':100,'compared_nodes':100,'reference_quadrature_order':20},
            'response':{'tip_displacement_z_mm':.05*sign,'max_von_mises_MPa':10.,'rf_N':[0,0,-40*sign],'input_force_accounting':{'constrained_cload_N':[0,0,0],'free_cload_N':[0,0,40*sign],'applied_cload_N':[0,0,40*sign],'rf_nset':'FIXED','constrained_nodes':10},'finite':True,'result_inventory':{'complete':True,'nodes':node_count}},
            'native_objects':[{'type_id':'Fem::ConstraintForce','proxy_source':None},{'type_id':'Fem::FemMeshObjectPython','proxy_source':'runtime/Mod/Fem/femobjects/mesh_gmsh.py','source_within_runtime':True}]}
    identity={'profile':'freecad-1.1.3-fem-faceforce-r1','module_sha256':'ca4af36e48f0b1cb6b3b57e7c63e61ed7a3de768df497d50d4fa171493634c53'}
    return {'runtime_before':identity,'runtime_after':copy.deepcopy(identity),'auto_reopen_count':0,
            'reference_selftest':{'reference_moment_Nmm':[-40/3,40/3,0],'virtual_work_Nmm':-1/15,'reference_virtual_work_Nmm':-1/15,'max_nodal_force_error_N':0},
            'basis':weights,'legacy':{'T3':[1/6]*3,'Q4':[.25]*4,'Q8':[-1/12]*4+[1/3]*4},
            'rejections':{v:'R1_EXPECTED' for v in ('empty','duplicate','nonfinite','folded','misordered','duplicate_reference')},'upstream_tests':{'run':5,'failures':0,'errors':0},'cases':cases}


def test_complete_independent_evidence_preserves_operator_gate():
    from fea_r1_evidence import assess_r1
    value=assess_r1(valid_worker())
    assert value['automated_status']=='passed'
    assert value['status']=='blocked' and value['native_gui_roundtrip']=='not_executed'


def test_raw_rf_includes_loads_on_constrained_nodes_without_changing_tolerance():
    from fea_r1_evidence import assess_r1
    worker=valid_worker();response=worker['cases']['mesh_4']['response']
    response['rf_N']=[0,0,39.8]
    response['input_force_accounting']['constrained_cload_N']=[0,0,-.2]
    response['input_force_accounting']['free_cload_N']=[0,0,-39.8]
    value=assess_r1(worker)
    assert value['automated_status']=='passed'
    assert value['support_reactions']['mesh_4']==pytest.approx([0,0,40])


@pytest.mark.parametrize('defect',['nodal','work','moment','force','reaction','response','region','proxy','missing_level','runtime','reopen','corner_weights','legacy','duplicate_accept','missing_work_reference','missing_boundary_count','bad_object','bad_reference_selftest'])
def test_incomplete_or_compensated_numerics_cannot_pass(defect):
    from fea_r1_evidence import assess_r1
    value=valid_worker();case=value['cases']['mesh_4']
    if defect=='nodal':case['independent']['max_nodal_force_error_N']=.001
    if defect=='work':case['independent']['virtual_work_Nmm']=999.
    if defect=='moment':case['moment_Nmm']=[-200,1800,0]
    if defect=='force':case['force_N'][2]=-39.825
    if defect=='reaction':case['response']['rf_N'][2]=35.
    if defect=='response':case['response']['tip_displacement_z_mm']=float('nan')
    if defect=='region':case['load_region_valid']=False
    if defect=='proxy':case['native_objects'][1]['source_within_runtime']=False
    if defect=='missing_level':del value['cases']['mesh_3']
    if defect=='runtime':value['runtime_after']['module_sha256']='stale'
    if defect=='reopen':value['auto_reopen_count']=1
    if defect=='corner_weights':value['basis']['graph_0.5']=[0,0,0,.18,.18,.18]
    if defect=='legacy':value['legacy']['Q8']=[.125]*8
    if defect=='duplicate_accept':value['rejections']['duplicate_reference']='accepted'
    if defect=='missing_work_reference':del case['independent']['reference_virtual_work_Nmm']
    if defect=='missing_boundary_count':case['independent']['boundary_faces']=None
    if defect=='bad_object':case['native_objects']=[None]
    if defect=='bad_reference_selftest':value['reference_selftest']['reference_virtual_work_Nmm']=0
    assert assess_r1(value)['automated_status']=='failed'


@pytest.mark.parametrize('case,field,value',[
    ('mesh_3','tip_displacement_z_mm',-1e100),
    ('reversed','max_von_mises_MPa',1e100),
    ('reversed','tip_displacement_z_mm',.051),
    ('permuted','tip_displacement_z_mm',-.051),
    ('permuted','max_von_mises_MPa',11.),
])
def test_physical_response_and_same_mesh_invariants_are_assessed(case,field,value):
    from fea_r1_evidence import assess_r1
    worker=valid_worker();worker['cases'][case]['response'][field]=value
    assert assess_r1(worker)['automated_status']=='failed'


def test_refinement_records_response_changes_without_claiming_convergence():
    from fea_r1_evidence import assess_r1
    report=assess_r1(valid_worker())
    assert report['checks']['refinement_response'] is True
    assert len(report['refinement_comparisons'])==2
    assert report['convergence_status']=='not_established'
    assert report['refinement_comparisons'][0]['displacement_change_mm']==0.
