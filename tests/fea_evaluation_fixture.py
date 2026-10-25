"""Explicit synthetic numerical policy; never a customer engineering default."""


def quantity(value,unit): return {'value':str(value),'unit':unit}


def evaluation_spec():
    from test_analysis_loads import load_fixture
    value,_=load_fixture()
    value.update(status='approved',unresolved=[],evaluation=evaluation_policy())
    value['mesh']={'element_type':'C3D10','initial_size':quantity(4,'mm'),'refinement_ratio':quantity('.7','1'),'levels':3,'minimum_scaled_jacobian':quantity('.000001','1'),'minimum_thickness_elements':2,'critical_rays':[], 'local_sizes':[]}
    for index,(x,y) in enumerate([(37,4.3),(51,5.7),(73,3.7)]):
        value['mesh']['critical_rays'].append({'id':'ray-'+str(index),'start':[quantity(v,'mm') for v in (x,y,0)],'end':[quantity(v,'mm') for v in (x,y,10)],'coverage_atol':quantity('.000001','mm'),'evidence':'Explicit synthetic full 10mm thickness span.'})
    value['budget']={'wall_seconds':300,'stage_seconds':120,'threads':1,'max_nodes':10000,'max_elements':10000,'max_disk_bytes':1000000000,'max_attempts':1,'memory_mode':'observed_only','max_memory_mb':1024}
    value['tool_constraints']={'runtime_manifest_sha256':'c'*64,'policy_sha256':'d'*64,'adapter_version':'native-solid-static-v1'}
    return value


def evaluation_policy(case_id='static'):
    return {'schema_version':'AnalysisEvaluationPolicy/v1','reference_length':quantity(100,'mm'),
            'observables':[
                {'id':'deflection','case_id':case_id,'field':'displacement_magnitude','region_id':'whole-model','roi':None,'reference_region_ids':[],'reference_recovery':'arithmetic_mean_of_native_nodal_displacements','aggregation':'max','mandatory':True,'evidence':['Synthetic absolute nodal displacement metric.']},
                {'id':'midspan-stress','case_id':case_id,'field':'von_mises','region_id':'midspan-roi','roi':{'minimum':[quantity(v,'mm') for v in (20,-1,-1)],'maximum':[quantity(v,'mm') for v in (80,11,11)],'coordinate_atol':quantity('.000001','mm'),'exclusions':[]},'reference_region_ids':[],'reference_recovery':'arithmetic_mean_of_native_nodal_displacements','aggregation':'max','mandatory':True,'evidence':['Fixed synthetic interior spatial ROI, approved before mesh generation.']}],
            'applicability':{'mode':'limited_screening','material_basis':'equivalent_isotropic_screening','maximum_displacement':quantity(1,'mm'),'maximum_abs_principal_elastic_strain':quantity('.01','1'),'maximum_displacement_to_thickness':quantity('.1','1'),'thickness_ray_ids':['ray-0','ray-1','ray-2'],'evidence':['Synthetic finite linear-equivalent-model bounds, not a material standard.'],'limitations':['No printed interlayer strength, fatigue, creep, buckling or contact conclusion.']}}
