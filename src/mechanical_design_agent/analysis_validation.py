"""Declared engineering ROIs, nonzero equilibrium scales and convergence.

No physical tolerance, allowable or applicability limit is invented here.
Raw global peaks remain visible even when a pre-approved ROI is evaluated.
"""
from __future__ import annotations

from decimal import Decimal
import math

from .analysis_protocol import canonical_record, parse_record, record_sha256
from .analysis_types import AnalysisError

_DISPLACEMENT={'displacement_magnitude','displacement_x','displacement_y','displacement_z'}


def _number(quantity,unit,*,positive=False):
    if quantity['unit']!=unit:raise AnalysisError('EVALUATION_UNIT_INVALID','evaluation units must match the declared quantity')
    value=float(quantity['value'])
    if not math.isfinite(value) or value<0 or positive and value==0:raise AnalysisError('EVALUATION_VALUE_INVALID','evaluation limits must be finite and nonnegative')
    return value


def _policy(spec):
    if spec.get('evaluation') is None:raise AnalysisError('ANALYSIS_EVALUATION_UNRESOLVED','declare observation regions, applicability bounds and reference length before review')
    return parse_record(canonical_record(spec['evaluation'],schema_name='evaluation-policy').decode(),schema_name='evaluation-policy')


def _box(box):
    lo,hi=([float(v['value']) for v in box[key]] for key in ('minimum','maximum'))
    if any(a>=b for a,b in zip(lo,hi)):raise AnalysisError('OBSERVABLE_ROI_INVALID','each engineering ROI must have positive finite spatial extent')
    _number(box['coordinate_atol'],'mm')


def validate_evaluation_policy(spec,regions=None):
    policy=_policy(spec)
    _number(policy['reference_length'],'mm',positive=True)
    app=policy['applicability']
    for name,unit in [('maximum_displacement','mm'),('maximum_abs_principal_elastic_strain','1'),('maximum_displacement_to_thickness','1')]:_number(app[name],unit,positive=True)
    rays={ray['id'] for ray in spec['mesh']['critical_rays']} if spec.get('mesh') else set()
    if not set(app['thickness_ray_ids'])<=rays:raise AnalysisError('APPLICABILITY_THICKNESS_UNBOUND','applicability thickness references must name approved full-span mesh rays')
    if any(not text.strip() for text in [*app['evidence'],*app['limitations']]):raise AnalysisError('APPLICABILITY_EVIDENCE_MISSING','applicability assumptions and limitations must be explicit')
    cases={case['case_id'] for case in spec['cases']}
    observations=policy['observables']
    if len({obs['id'] for obs in observations})!=len(observations):raise AnalysisError('OBSERVABLE_DUPLICATE','observation IDs must be unique')
    names=None if regions is None else {region['region_id'] for region in regions['bindings']}
    for obs in observations:
        if obs['case_id'] not in cases or any(not text.strip() for text in obs['evidence']):raise AnalysisError('OBSERVABLE_UNBOUND','every observation needs an explicit case and evidence')
        if names is not None and not set(obs['reference_region_ids'])<=names:raise AnalysisError('OBSERVABLE_REFERENCE_MISSING','relative displacement needs exact reviewed physical reference regions')
        if obs['field'] not in _DISPLACEMENT and (obs['roi'] is None or obs['reference_region_ids']):raise AnalysisError('OBSERVABLE_STRESS_ROI_REQUIRED','engineering stress needs a fixed finite spatial ROI and no displacement reference')
        if obs['roi'] is not None:
            _box(obs['roi'])
            for box in obs['roi']['exclusions']:_box(box)
    for case in cases:
        if not any(obs['case_id']==case and obs['field'] in _DISPLACEMENT and obs['mandatory'] for obs in observations):raise AnalysisError('OBSERVABLE_DISPLACEMENT_REQUIRED','every case requires a declared mandatory displacement observation')
    validation=spec.get('validation')
    if validation is None:raise AnalysisError('ANALYSIS_VALIDATION_UNRESOLVED','explicit absolute and relative numerical tolerances are required')
    for name,unit in [('force','N'),('moment','N*mm'),('displacement','mm'),('stress','MPa')]:
        if _number(validation[name+'_atol'],unit)+_number(validation[name+'_rtol'],'1')==0:raise AnalysisError('EVALUATION_TOLERANCE_INVALID','declare a nonzero numerical tolerance without a fallback')
    ids=[criterion['id'] for criterion in spec['criteria']]
    if len(ids)!=len(set(ids)):raise AnalysisError('CRITERION_DUPLICATE','engineering criterion IDs must be unique')
    for criterion in spec['criteria']:
        field=criterion['field'];unit='mm' if field in _DISPLACEMENT else 'MPa'
        _number(criterion['limit'],unit);_number(criterion['absolute_tolerance'],unit)
        if not criterion['applicability_evidence'].strip():raise AnalysisError('CRITERION_EVIDENCE_MISSING','engineering limits need applicability evidence')
        if field not in _DISPLACEMENT and app['material_basis']!='qualified_homogeneous_isotropic':raise AnalysisError('EQUIVALENT_MATERIAL_STRENGTH_UNSUPPORTED','equivalent isotropic screening cannot establish printed or interlayer strength')
        for case in cases:
            matches=[obs for obs in observations if obs['case_id']==case and obs['field']==field and obs['region_id']==criterion['region_id'] and obs['aggregation']=='max' and obs['mandatory']]
            if len(matches)!=1:raise AnalysisError('CRITERION_OBSERVABLE_MISSING','each criterion needs one mandatory converged maximum in its exact ROI for every case')
    return policy


def _norm(vector):return math.sqrt(math.fsum(value*value for value in vector))


def _binding(spec):
    return {'spec_sha256':record_sha256(spec,schema_name='spec'),'geometry_sha256':spec['source']['geometry_sha256'],'regions_sha256':spec['source']['regions_sha256'],'evaluation_sha256':record_sha256(_policy(spec),schema_name='evaluation-policy')}


def _inside(point,box):
    tolerance=float(box['coordinate_atol']['value'])
    return all(float(lo['value'])-tolerance<=value<=float(hi['value'])+tolerance for value,lo,hi in zip(point,box['minimum'],box['maximum']))


def _scope(spec,fields,policy):
    raw=fields['fields'];app=policy['applicability']
    maximum=max(_norm(vector) for vector in raw['displacement']['values'])
    young=float(spec['material']['youngs_modulus']['value']);poisson=float(spec['material']['poisson_ratio']['value'])
    strains=[abs(((1+poisson)*value-poisson*math.fsum(principal))/young) for principal in raw['principal_stress']['values'] for value in principal]
    rays={ray['id']:ray for ray in spec['mesh']['critical_rays']}
    thickness=min(_norm([float(b['value'])-float(a['value']) for a,b in zip(rays[name]['start'],rays[name]['end'])]) for name in app['thickness_ray_ids'])
    if thickness<=0:raise AnalysisError('APPLICABILITY_THICKNESS_UNBOUND','positive approved geometric thickness spans are required')
    measures={'maximum_displacement':maximum,'maximum_abs_principal_elastic_strain':max(strains),'maximum_displacement_to_thickness':maximum/thickness}
    checks=[{'id':name,'actual':actual,'limit':float(app[name]['value']),'passed':actual<=float(app[name]['value'])} for name,actual in measures.items()]
    return {'status':app['mode'] if all(check['passed'] for check in checks) else 'outside_scope','checks':checks,'basis':'Infinitesimal elastic principal strains derived from the same native averaged nodal stress and declared E/nu; not plastic, contact or printed-layer strain.','limitations':app['limitations']}


def _components(data,fixed):
    from .resources.freecad.analysis_mesh_math import FACES,cross,sub
    elements={row[0]:row[1:] for row in data['elements']}
    parents={key:key for key in elements};owners={}
    def root(key):
        while parents[key]!=key:parents[key]=parents[parents[key]];key=parents[key]
        return key
    for element,ids in elements.items():
        for face in FACES:
            key=tuple(sorted(ids[i] for i in face))
            if key in owners:parents[root(element)]=root(owners[key])
            else:owners[key]=element
    if len({root(key) for key in parents})!=1:raise AnalysisError('MESH_DISCONNECTED','the first solid profile requires one full-face-connected volume mesh')
    points={row[0]:row[1:] for row in data['nodes']}
    if len(fixed)<3:raise AnalysisError('SUPPORT_RIGID_MOTION_UNRESOLVED','fixed translations must constrain the actual connected component')
    p=points[min(fixed)];vectors=[sub(points[node],p) for node in sorted(fixed) if points[node]!=p]
    if not vectors:raise AnalysisError('SUPPORT_RIGID_MOTION_UNRESOLVED','fixed nodes are coincident')
    first=max(vectors,key=_norm);scale=_norm(first)
    if not any(_norm(cross(first,vector))>1e-12*scale*max(_norm(vector),scale) for vector in vectors):raise AnalysisError('SUPPORT_RIGID_MOTION_UNRESOLVED','fixed nodes are collinear or numerically rank deficient')


def validate_level(spec,ledger,mesh,fields):
    from .analysis_mesh import validate_mesh,plan_mesh_levels
    from .analysis_results import validate_fields
    from .analysis_equivalent_loads import recover_support_reactions
    from .analysis_deck_physics import writer_number_matches
    policy=validate_evaluation_policy(spec)
    if spec['status']!='approved':raise AnalysisError('ANALYSIS_APPROVAL_REQUIRED','numerical evaluation requires the exact approved specification')
    sha=record_sha256(spec,schema_name='spec')
    if fields['spec_sha256']!=sha or ledger['spec_sha256']!=sha:raise AnalysisError('VALIDATION_SPEC_CHANGED','fields and loads need the exact approved specification')
    selected=[case for case in ledger['cases'] if case['case_id']==fields['case_id']]
    if len(selected)!=1:raise AnalysisError('VALIDATION_CASE_CHANGED','fields belong to another load case')
    case=selected[0];receipt,level=mesh['receipt'],mesh['level']
    planned=plan_mesh_levels(spec)
    if type(receipt['level']) is not int or not 0<=receipt['level']<len(planned) or level!=planned[receipt['level']] or receipt['geometry_sha256']!=spec['source']['geometry_sha256'] or receipt['region_bindings_sha256']!=spec['source']['regions_sha256'] or mesh['mapping']['geometry_sha256']!=spec['source']['geometry_sha256'] or len(mesh['data']['nodes'])!=receipt['node_count'] or len(mesh['data']['elements'])!=receipt['element_count']:raise AnalysisError('VALIDATION_MESH_BINDING_CHANGED','evaluation needs the exact approved mesh policy, geometry, counts and regions')
    quality=validate_mesh(receipt,level)
    if quality['status']!='passed':return {'case_id':case['case_id'],'level':receipt['level'],'numerical_status':'inconclusive','code':quality['code']}
    regions={region['region_id']:region for region in mesh['mapping']['regions']}
    fixed={node for name in case['fixed_regions'] for node in regions[name]['node_ids']}
    _components(mesh['data'],fixed)
    raw=validate_fields(fields['fields'],{'mesh_sha256':receipt['artifacts']['native_mesh']['sha256'],'node_ids':[row[0] for row in mesh['data']['nodes']],'fixed_node_ids':sorted(fixed)})
    points={row[0]:row[1:] for row in fields['solver_nodes']};native_points={row[0]:row[1:] for row in mesh['data']['nodes']}
    if len(points)!=len(fields['solver_nodes']) or set(points)!=set(native_points) or any(len(point)!=3 or any(type(value) not in (int,float) or not math.isfinite(value) for value in point) for point in points.values()) or any(not writer_number_matches(a,b) for node,point in points.items() for a,b in zip(point,native_points[node])):raise AnalysisError('VALIDATION_SOLVER_MESH_CHANGED','solver coordinate representations differ from the qualified mesh')
    origin=[float(value['value']) for value in spec['validation']['moment_origin']]
    if list(map(float,fields['moment_origin_mm']))!=origin:raise AnalysisError('VALIDATION_MOMENT_ORIGIN_CHANGED','reaction moments use another reference origin')
    rf=raw['raw_rf'];reaction=recover_support_reactions(nodes=points,fixed_node_ids=sorted(fixed),raw_rf=dict(zip(rf['node_ids'],rf['values'])),applied_nodal_forces={int(node):force for node,force in fields['applied_nodal_forces_N'].items()},moment_origin=origin)
    checks=[]
    force_scale=sum(_norm(list(map(float,load.get('components_N',load.get('resultant_N'))))) for load in case['native_objects'])+_norm(reaction['force_N'])
    moment_scale=max(sum(_norm(list(map(float,load['moment_Nmm']))) for load in case['native_objects'])+_norm(reaction['moment_Nmm']),float(policy['reference_length']['value'])*force_scale)
    if not all(math.isfinite(value) for value in (force_scale,moment_scale)):raise AnalysisError('VALIDATION_NONFINITE','nonfinite physical scales cannot pass equilibrium')
    for name,unit,scale in [('force','N',force_scale),('moment','Nmm',moment_scale)]:
        applied=list(map(float,case['total_'+name+'_'+unit]));actual=reaction[name+'_'+unit]
        residual=_norm([a+b for a,b in zip(actual,applied)])
        tolerance=float(spec['validation'][name+'_atol']['value'])+float(spec['validation'][name+'_rtol']['value'])*scale
        if not math.isfinite(residual) or not math.isfinite(tolerance):raise AnalysisError('VALIDATION_NONFINITE','nonfinite residual or tolerance cannot pass equilibrium')
        checks.append({'id':name+'_balance','mandatory':True,'status':'passed' if residual<=tolerance else 'failed','residual':residual,'scale':scale,'tolerance':tolerance,'unit':unit})
    by_node={node:index for index,node in enumerate(raw['node_ids'])}
    loaded={node for load in case['native_objects'] if load['type']!='gravity' for node in regions[load['region_id']]['node_ids']}
    observables=[]
    for observation in policy['observables']:
        if observation['case_id']!=case['case_id']:continue
        roi=observation['roi']
        ids=sorted(node for node,point in points.items() if roi is None or _inside(point,roi) and not any(_inside(point,excluded) for excluded in roi['exclusions']))
        if not ids:raise AnalysisError('OBSERVABLE_ROI_EMPTY','approved engineering ROI contains no nodes at this mesh level')
        field=observation['field']
        if field not in _DISPLACEMENT and (set(ids)&(fixed|loaded)):raise AnalysisError('OBSERVABLE_SINGULAR_REGION','engineering stress ROI includes constrained or loaded nodes; revise its explicit scope before approval')
        references={node for name in observation['reference_region_ids'] for node in regions[name]['node_ids']}
        reference=[math.fsum(raw['displacement']['values'][by_node[node]][axis] for node in sorted(references))/len(references) if references else 0. for axis in range(3)]
        values=[]
        for node in ids:
            index=by_node[node]
            if field in _DISPLACEMENT:
                vector=[a-b for a,b in zip(raw['displacement']['values'][index],reference)]
                value=_norm(vector) if field=='displacement_magnitude' else vector[{'displacement_x':0,'displacement_y':1,'displacement_z':2}[field]]
            elif field=='von_mises':value=raw['von_mises']['values'][index]
            else:value=raw['principal_stress']['values'][index][0 if field=='principal_max' else 2]
            values.append(value)
        aggregation=observation['aggregation'];value=max(values) if aggregation=='max' else min(values) if aggregation=='min' else max(map(abs,values))
        observables.append({'id':observation['id'],'field':field,'region_id':observation['region_id'],'value':value,'unit':'mm' if field in _DISPLACEMENT else 'MPa','mandatory':observation['mandatory'],'node_count':len(ids),'roi':roi,'displacement_reference_mm':reference,'recovery':'native_extrapolated_averaged_nodal'})
    scope=_scope(spec,{'fields':raw},policy)
    binding=_binding(spec)
    return {'schema_version':'AnalysisLevelValidation/v1','case_id':case['case_id'],'level':receipt['level'],'binding':binding,'mesh':{'sha256':receipt['artifacts']['native_mesh']['sha256'],'node_count':receipt['node_count'],'element_count':receipt['element_count'],'size_mm':level['size']['value']},'numerical_status':'passed' if all(check['status']=='passed' for check in checks) else 'failed','checks':checks,'observables':observables,'raw_global_peaks':{'maximum_displacement_mm':max(_norm(vector) for vector in raw['displacement']['values']),'maximum_von_mises_MPa':max(raw['von_mises']['values']),'maximum_principal_MPa':max(row[0] for row in raw['principal_stress']['values']),'minimum_principal_MPa':min(row[2] for row in raw['principal_stress']['values'])},'applicability_status':scope['status'],'applicability':scope,'reactions':reaction}


def evaluate_convergence(spec,levels):
    from .analysis_mesh import plan_mesh_levels
    policy=validate_evaluation_policy(spec);planned=plan_mesh_levels(spec);binding=_binding(spec);comparisons=[];finest=[];cases={case['case_id'] for case in spec['cases']}
    result={'schema_version':'AnalysisConvergence/v1','spec_sha256':binding['spec_sha256'],'numerical_status':'inconclusive','comparisons':comparisons,'finest':finest,'applicability_status':'within_declared_scope'}
    if any(level['case_id'] not in cases for level in levels):return {**result,'numerical_status':'failed','code':'CONVERGENCE_CASE_CHANGED'}
    for case in sorted(cases):
        rows=sorted((level for level in levels if level['case_id']==case),key=lambda value:value['level'])
        if any(row['numerical_status']=='failed' for row in rows):return {**result,'numerical_status':'failed','code':'LEVEL_VALIDATION_FAILED'}
        if any(row['numerical_status']!='passed' for row in rows) or len(rows)<3:return {**result,'code':'CONVERGENCE_INCOMPLETE'}
        if [row['level'] for row in rows]!=list(range(len(rows))) or len(rows)>spec['mesh']['levels'] or any(row['binding']!=binding for row in rows):return {**result,'numerical_status':'failed','code':'CONVERGENCE_BINDING_CHANGED'}
        if len({row['mesh']['sha256'] for row in rows})!=len(rows) or any(Decimal(row['mesh']['size_mm'])!=Decimal(planned[row['level']]['size']['value']) or any(type(row['mesh'][name]) is not int or row['mesh'][name]<=0 for name in ('node_count','element_count')) for row in rows) or any(current['mesh']['element_count']<=previous['mesh']['element_count'] or current['mesh']['node_count']<=previous['mesh']['node_count'] for previous,current in zip(rows,rows[1:])):return {**result,'numerical_status':'failed','code':'CONVERGENCE_REFINEMENT_MISSING'}
        if any(row['applicability_status'] not in ('within_declared_scope','limited_screening','outside_scope') for row in rows):return {**result,'numerical_status':'failed','code':'APPLICABILITY_UNVERIFIED'}
        expected={obs['id']:obs for obs in policy['observables'] if obs['case_id']==case}
        for row in rows:
            if len(row['observables'])!=len(expected) or {obs['id'] for obs in row['observables']}!=set(expected):return {**result,'numerical_status':'failed','code':'CONVERGENCE_OBSERVABLE_CHANGED'}
            for obs in row['observables']:
                declaration=expected[obs['id']]
                if any(obs[key]!=declaration[key] for key in ('field','region_id','mandatory')) or obs['unit']!=('mm' if obs['field'] in _DISPLACEMENT else 'MPa') or type(obs['value']) not in (int,float) or not math.isfinite(obs['value']):return {**result,'numerical_status':'failed','code':'CONVERGENCE_OBSERVABLE_CHANGED'}
        for previous,current in zip(rows[-3:-1],rows[-2:]):
            old={obs['id']:obs for obs in previous['observables']}
            for obs in current['observables']:
                prefix='displacement' if obs['field'] in _DISPLACEMENT else 'stress'
                a,b=old[obs['id']]['value'],obs['value']
                tolerance=float(spec['validation'][prefix+'_atol']['value'])+float(spec['validation'][prefix+'_rtol']['value'])*max(abs(a),abs(b))
                comparisons.append({'case_id':case,'from_level':previous['level'],'to_level':current['level'],'observable_id':obs['id'],'mandatory':obs['mandatory'],'difference':abs(a-b),'tolerance':tolerance,'unit':obs['unit'],'passed':abs(a-b)<=tolerance})
        finest.append(rows[-1])
    if any(row['applicability_status']=='outside_scope' for row in finest):result['applicability_status']='outside_scope'
    elif any(row['applicability_status']=='limited_screening' for row in finest):result['applicability_status']='limited_screening'
    passed=bool(cases) and all(item['passed'] for item in comparisons if item['mandatory'])
    remaining=any(len([row for row in levels if row['case_id']==case])<spec['mesh']['levels'] for case in cases)
    return {**result,'numerical_status':'passed' if passed else 'inconclusive' if remaining else 'failed','code':None if passed else 'CONVERGENCE_MORE_LEVELS_REQUIRED' if remaining else 'CONVERGENCE_FAILED'}


def evaluate_criteria(spec,validation):
    if not spec['criteria']:return {'criteria_status':'not_evaluated','checks':[]}
    if validation['spec_sha256']!=record_sha256(spec,schema_name='spec'):raise AnalysisError('ANALYSIS_SPEC_CHANGED','engineering criteria cannot reuse results from a different frozen specification')
    if validation['numerical_status']!='passed' or validation['applicability_status']=='outside_scope':return {'criteria_status':'inconclusive','checks':[]}
    policy=_policy(spec);checks=[]
    for criterion in spec['criteria']:
        if criterion['field'] not in _DISPLACEMENT and policy['applicability']['material_basis']!='qualified_homogeneous_isotropic':raise AnalysisError('EQUIVALENT_MATERIAL_STRENGTH_UNSUPPORTED','equivalent material screening does not establish strength')
        unit='mm' if criterion['field'] in _DISPLACEMENT else 'MPa';limit=_number(criterion['limit'],unit);atol=_number(criterion['absolute_tolerance'],unit)
        for case in spec['cases']:
            matches=[obs for level in validation['finest'] if level['case_id']==case['case_id'] for obs in level['observables'] if obs['field']==criterion['field'] and obs['region_id']==criterion['region_id']]
            if len(matches)!=1 or not matches[0]['mandatory']:raise AnalysisError('CRITERION_OBSERVABLE_MISSING','criterion has no unique mandatory converged observation')
            actual=matches[0]['value'];checks.append({'id':criterion['id'],'case_id':case['case_id'],'actual':actual,'limit':limit,'absolute_tolerance':atol,'unit':unit,'status':'passed' if actual<=limit+atol else 'failed','evidence':criterion['applicability_evidence']})
    return {'criteria_status':'passed' if all(check['status']=='passed' for check in checks) else 'failed','checks':checks}
