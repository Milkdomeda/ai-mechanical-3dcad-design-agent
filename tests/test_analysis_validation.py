"""Numerical unit checks with an injected mesh-quality collaborator.

Native quality/thickness certification is independently covered by mesh-live;
this single-tetra fixture isolates equilibrium, RF correction, ROI and scope.
"""
from copy import deepcopy

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from fea_evaluation_fixture import evaluation_spec


def fixture(monkeypatch,*,gravity=False):
    from mechanical_design_agent import analysis_mesh
    from mechanical_design_agent.analysis_loads import build_load_ledger
    from mechanical_design_agent.analysis_protocol import record_sha256
    from mechanical_design_agent.analysis_equivalent_loads import recover_support_reactions
    from test_analysis_loads import load_fixture
    spec=evaluation_spec();_,geometry=load_fixture(gravity=gravity)
    if gravity:
        weighted,_=load_fixture(gravity=True)
        spec['frame_mass']=weighted['frame_mass'];spec['cases']=weighted['cases']
    spec['material']['youngs_modulus']['value']='200000'
    ledger=build_load_ledger(spec,geometry)
    corners=[(0,0,0),(100,5,5),(0,10,0),(0,0,10)]
    from mechanical_design_agent.resources.freecad.analysis_mesh_math import EDGES
    points=[*corners,*[tuple((corners[a][i]+corners[b][i])/2 for i in range(3)) for a,b in EDGES]]
    nodes={i:list(point) for i,point in enumerate(points,1)}
    fixed=[1,3,4,7,8,10]
    total=50. if gravity else 40.;couple=450. if gravity else 400.
    corrected={node:[0.,0.,0.] for node in fixed}
    corrected.update({1:[couple,0.,total/2],3:[0.,0.,total/2],4:[-couple,0.,0.]})
    applied={2:[0.,0.,-40.]}
    if gravity:applied.update({2:[0.,0.,-41.],6:[0.,0.,-4.],9:[0.,0.,-4.],10:[0.,0.,-1.]})
    rf={node:[a+b for a,b in zip(corrected[node],applied.get(node,[0.,0.,0.]))] for node in fixed}
    stress=[[1000. if node in fixed else 10.,0.,0.,0.,0.,0.] for node in nodes]
    raw={'schema_version':'AnalysisFieldBundle/v1','unit_system':'N-mm-s-tonne','coordinate_system':'global','location':'nodal','stress_recovery':'native_extrapolated_averaged_nodal','step':{'number':1,'increment':1,'time':1.},'mesh_sha256':'a'*64,'node_ids':list(nodes),'displacement':{'unit':'mm','values':[[0.,0.,0.] if node in fixed else [0.,0.,-.1 if node==2 else -.05] for node in nodes]},'stress':{'unit':'MPa','values':stress},'von_mises':{'unit':'MPa','values':[row[0] for row in stress]},'principal_stress':{'unit':'MPa','values':[row[:3] for row in stress]},'raw_rf':{'unit':'N','node_ids':fixed,'values':[rf[node] for node in fixed],'reported_total':[0.,0.,49. if gravity else 40.],'rounding_bound':[0.,0.,0.]}}
    fields={'spec_sha256':record_sha256(spec,schema_name='spec'),'case_id':'static','fields':raw,'solver_nodes':[[node,*point] for node,point in nodes.items()],'applied_nodal_forces_N':applied,'moment_origin_mm':['0','0','0'],'reactions':recover_support_reactions(nodes=nodes,fixed_node_ids=fixed,raw_rf=rf,applied_nodal_forces=applied,moment_origin=[0.,0.,0.])}
    level=analysis_mesh.plan_mesh_levels(spec)[0]
    mesh={'receipt':{'level':0,'node_count':10,'element_count':1,'geometry_sha256':spec['source']['geometry_sha256'],'region_bindings_sha256':spec['source']['regions_sha256'],'artifacts':{'native_mesh':{'sha256':'a'*64}}},'level':level,'data':{'nodes':[[node,*point] for node,point in nodes.items()],'elements':[[1,*nodes]]},'mapping':{'geometry_sha256':spec['source']['geometry_sha256'],'regions':[{'region_id':'support','node_ids':fixed},{'region_id':'payload','node_ids':[2]}]}}
    monkeypatch.setattr(analysis_mesh,'validate_mesh',lambda *args:{'status':'passed'})
    return spec,ledger,mesh,fields


def test_force_and_moment_balance_include_gravity_and_fixed_node_dload(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_level
    spec,ledger,mesh,fields=fixture(monkeypatch,gravity=True)
    result=validate_level(spec,ledger,mesh,fields)
    assert result['numerical_status']=='passed'
    assert result['reactions']['force_N']==[0.,0.,50.]
    assert result['reactions']['moment_Nmm']==[250.,-4500.,0.]
    fields['applied_nodal_forces_N'].pop(10)
    assert validate_level(spec,ledger,mesh,fields)['numerical_status']=='failed'


def test_fixed_spatial_stress_roi_keeps_raw_global_peak(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_level
    result=validate_level(*fixture(monkeypatch))
    assert result['raw_global_peaks']['maximum_von_mises_MPa']==1000.
    stress=next(obs for obs in result['observables'] if obs['field']=='von_mises')
    assert stress['value']==10. and stress['node_count']==3
    assert result['applicability_status']=='limited_screening'


def test_large_response_is_outside_scope_without_becoming_a_strength_failure(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_level
    args=fixture(monkeypatch)
    args[3]['fields']['displacement']['values'][1]=[0.,0.,2.]
    result=validate_level(*args)
    assert result['numerical_status']=='passed'
    assert result['applicability_status']=='outside_scope'


def test_roi_cannot_be_empty_or_silently_include_constraint_peak(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_level
    from mechanical_design_agent.analysis_protocol import record_sha256
    spec,ledger,mesh,fields=fixture(monkeypatch)
    for x,code in [(200,'OBSERVABLE_ROI_EMPTY'),(-1,'OBSERVABLE_SINGULAR_REGION')]:
        changed=deepcopy(spec)
        changed['evaluation']['observables'][1]['roi']['minimum'][0]['value']=str(x)
        changed['evaluation']['observables'][1]['roi']['maximum'][0]['value']=str(x+101)
        sha=record_sha256(changed,schema_name='spec');ledger['spec_sha256']=sha;fields['spec_sha256']=sha
        with pytest.raises(AnalysisError) as error:validate_level(changed,ledger,mesh,fields)
        assert error.value.code==code


def test_equivalent_material_does_not_gain_a_strength_allowable(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_evaluation_policy
    spec,_,_,_=fixture(monkeypatch)
    spec['criteria']=[{'id':'strength','field':'von_mises','region_id':'midspan-roi','operator':'lte','limit':{'value':'50','unit':'MPa'},'absolute_tolerance':{'value':'.000001','unit':'MPa'},'applicability_evidence':'Synthetic attempted strength criterion.'}]
    with pytest.raises(AnalysisError) as error:validate_evaluation_policy(spec)
    assert error.value.code=='EQUIVALENT_MATERIAL_STRENGTH_UNSUPPORTED'


def test_near_zero_resultant_keeps_a_nonzero_declared_physical_scale(monkeypatch):
    from mechanical_design_agent.analysis_validation import validate_level
    from mechanical_design_agent.analysis_protocol import record_sha256
    from mechanical_design_agent.analysis_loads import build_load_ledger
    from test_analysis_loads import load_fixture
    spec,_,mesh,fields=fixture(monkeypatch)
    positive=deepcopy(spec['cases'][0]['loads'][0]);positive['id']='push';positive['components']=[{'value':str(v),'unit':'N'} for v in (40,0,0)]
    negative=deepcopy(positive);negative['id']='pull';negative['components'][0]['value']='-40'
    spec['cases'][0]['loads']=[positive,negative]
    _,geometry=load_fixture();ledger=build_load_ledger(spec,geometry)
    fields['spec_sha256']=record_sha256(spec,schema_name='spec');fields['applied_nodal_forces_N']={}
    rf=fields['fields']['raw_rf'];rf['values']=[[1e-8,0.,0.],*[[0.,0.,0.]]*5];rf['reported_total']=[1e-8,0.,0.]
    result=validate_level(spec,ledger,mesh,fields)
    assert result['numerical_status']=='passed'
    force=next(check for check in result['checks'] if check['id']=='force_balance')
    assert 80<=force['scale']<81
    rf['values'][0][0]=1.;rf['reported_total'][0]=1.
    assert validate_level(spec,ledger,mesh,fields)['numerical_status']=='failed'
