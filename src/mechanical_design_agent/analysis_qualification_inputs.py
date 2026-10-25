"""Fixed synthetic software benchmarks. Never defaults for a user's design."""
from copy import deepcopy

from .analysis_geometry import resolve_regions
from .analysis_protocol import canonical_record,parse_record
from .analysis_session import _policy
from .analysis_types import AnalysisError


def q(value,unit):return {'value':str(value),'unit':unit}


def make_inputs(geometry,settings,*,curved=False):
    selectors=[{'region_id':'surface-'+str(face['index']),'object_name':'Solid','surface_type':face['surface_type'],'centroid':face['centroid'],'centroid_tolerance':q('.000001','mm'),'area':face['area'],'area_tolerance':q('.000001','mm^2'),'expected_faces':1,'normal':None} for face in geometry['faces']]
    if curved:
        faces=[face for face in geometry['faces'] if face['surface_type']=='cylinder']
        if len(faces)!=2:raise AnalysisError('FEA_QUALIFICATION_FAILED','the synthetic partition must expose exactly two half-cylinder references')
        selectors.append({'region_id':'curved-payload','object_name':'Solid','surface_type':'cylinder','centroid':[q(v,'mm') for v in (50,5,5)],'centroid_tolerance':q(4,'mm'),'area':faces[0]['area'],'area_tolerance':q('.000001','mm^2'),'expected_faces':2,'normal':None})
    regions=resolve_regions(geometry,selectors)
    end=lambda x:[face for face in geometry['faces'] if face['surface_type']=='plane' and abs(float(face['centroid'][0]['value'])-x)<1e-8]
    fixed=['surface-'+str(face['index']) for face in end(0)];loaded=end(100)
    if not fixed or not loaded:raise AnalysisError('FEA_QUALIFICATION_FAILED','synthetic end-cap identities changed')
    cases=[{'case_id':'static','fixed_regions':fixed,'free_regions':[],'operating_condition':'supported','load_meaning':'direct_applied_loads','screening_force':None,'loads':[{'type':'total_force','id':'force','region_id':'curved-payload' if curved else 'surface-'+str(loaded[0]['index']),'components':[q(v,'N') for v in ((0,0,-40) if curved else (40,0,0))],'distribution':'native_uniform_surface','factor':q(1,'1'),'basis':{'type':'specified_force','evidence':['Fixed synthetic 40 N software fixture.']}}]}]
    if not curved:
        reverse=deepcopy(cases[0]);reverse['case_id']='reverse';reverse['loads'][0]['components'][0]=q(-40,'N');cases.append(reverse)
    rays=[]
    pairs=[((0,y,z),(100,y,z)) for y,z in ((4.3,4.1),(5.7,5.3),(3.7,6.1))] if curved else [((x,y,0),(x,y,10)) for x,y in ((37,4.3),(51,5.7),(73,3.7))]
    for index,(start,end_point) in enumerate(pairs):rays.append({'id':'span-'+str(index),'start':[q(v,'mm') for v in start],'end':[q(v,'mm') for v in end_point],'coverage_atol':q('.000001','mm'),'evidence':'Exact synthetic full boundary span; box thickness or cylinder axial compatibility only.'})
    observations=[]
    for case in cases:
        for name,field,roi in [('deflection','displacement_magnitude',None),('midspan-stress','von_mises',{'minimum':[q(v,'mm') for v in (20,-1,-1)],'maximum':[q(v,'mm') for v in (80,11,11)],'coordinate_atol':q('.000001','mm'),'exclusions':[]})]:
            observations.append({'id':name+'-'+case['case_id'],'case_id':case['case_id'],'field':field,'region_id':'whole-model' if roi is None else 'midspan-roi','roi':roi,'reference_region_ids':[],'reference_recovery':'arithmetic_mean_of_native_nodal_displacements','aggregation':'max','mandatory':True,'evidence':['Fixed synthetic software observation selected before meshing.']})
    spec={'schema_version':'AnalysisSpec/v1','design_id':'software-qualification','analysis_id':'curved-input' if curved else 'axial','revision':1,'status':'approved','analysis_type':'linear_static','conclusion_level':'screening_only','unit_system':'N-mm-s-tonne','source':{'sha256':geometry['source_sha256'],'design_model_sha256':geometry['source_sha256'],'object_names':['Solid'],'excluded_object_names':[],'geometry_sha256':geometry['geometry_sha256'],'regions_sha256':regions['bindings_sha256']},'material':{'name':'Synthetic homogeneous software benchmark','youngs_modulus':q(200000,'MPa'),'poisson_ratio':q(0,'1'),'density':None,'evidence':['Artificial E and nu for an analytic software check, not production material approval.']},'frame_mass':None,'cases':cases,'mesh':{'element_type':'C3D10','initial_size':q(4,'mm'),'refinement_ratio':q('.7','1'),'levels':3,'minimum_scaled_jacobian':q('.000001','1'),'minimum_thickness_elements':2,'critical_rays':rays,'local_sizes':[]},'validation':{name:q(value,unit) for name,value,unit in [('force_rtol','.001','1'),('force_atol','.000001','N'),('moment_rtol','.001','1'),('moment_atol','.000001','N*mm'),('displacement_rtol','.02','1'),('displacement_atol','.000001','mm'),('stress_rtol','.02','1'),('stress_atol','.000001','MPa')]},'evaluation':{'schema_version':'AnalysisEvaluationPolicy/v1','reference_length':q(100,'mm'),'observables':observations,'applicability':{'mode':'limited_screening','material_basis':'equivalent_isotropic_screening','maximum_displacement':q(1,'mm'),'maximum_abs_principal_elastic_strain':q('.01','1'),'maximum_displacement_to_thickness':q('.1','1'),'thickness_ray_ids':[ray['id'] for ray in rays],'evidence':['Fixed synthetic finite linear-equivalent-model bounds.'],'limitations':['No printed layer strength, fatigue, creep, buckling or contact conclusion.']}},'budget':{'wall_seconds':1800,'stage_seconds':300,'threads':1,'max_nodes':100000,'max_elements':100000,'max_disk_bytes':2000000000,'max_attempts':1,'memory_mode':'observed_only','max_memory_mb':4096},'tool_constraints':None,'assumptions':['Generated software qualification only; no user engineering approval is recorded.'],'unresolved':[],'criteria':[]}
    spec['validation']['moment_origin']=[q(0,'mm') for _ in range(3)]
    spec['tool_constraints']={'runtime_manifest_sha256':settings['runtime']['manifest_sha256'],'policy_sha256':_policy(spec),'adapter_version':'native-solid-static-v1'}
    return parse_record(canonical_record(spec,schema_name='spec').decode('utf-8'),schema_name='spec'),regions
