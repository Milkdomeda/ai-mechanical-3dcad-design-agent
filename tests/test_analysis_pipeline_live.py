"""Actual two-case, three-level product pipeline on synthetic geometry only."""
from copy import deepcopy
import hashlib
import json

import pytest


@pytest.mark.live_fea
def test_native_pipeline_two_cases_three_levels(native_product_session):
    from mechanical_design_agent.analysis_geometry import inspect_geometry
    from mechanical_design_agent.analysis_loads import build_load_ledger
    from mechanical_design_agent.analysis_pipeline import ExecutionState, _run_cases
    from mechanical_design_agent.analysis_protocol import record_sha256
    from mechanical_design_agent.analysis_budget import BudgetGuard
    from mechanical_design_agent.analysis_runtime_patch import PROFILE
    from test_analysis_mesh_live import mesh_inputs
    from test_analysis_deck_live import declared_spec
    directory,fixtures,settings,native=native_product_session
    if settings['configuration']['runtime']['profile']!=PROFILE:
        pytest.skip('requires separately qualified nodal RF runtime')
    source=fixtures/'box.FCStd'; geometry_dir=directory/'pipeline geometry 中文'
    request={'schema_version':'AnalysisGeometryRequest/v1','source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'included_object':'Solid','excluded_objects':['Ball1','Ball2','Ball3','Ball4'],'partitions':[],'volume_tolerance':{'value':'.000001','unit':'mm^3'}}
    geometry=inspect_geometry(source,request,settings,geometry_dir,transaction=native)
    regions,levels=mesh_inputs(geometry,'box')
    spec=declared_spec({'geometry_manifest':geometry,'region_bindings':regions},levels[0],'force',settings)
    # Explicit homogeneous nu=0 axial benchmark has an analytic uniform
    # solution. These fixture values are never customer defaults.
    spec['material']['youngs_modulus']['value']='200000'
    spec['material']['poisson_ratio']['value']='0'
    spec['cases'][0]['loads'][0]['components']=[{'value':str(v),'unit':'N'} for v in (40,0,0)]
    reverse=deepcopy(spec['cases'][0]);reverse['case_id']='reverse'
    reverse['loads'][0]['components'][0]['value']='-40'
    spec['cases'].append(reverse)
    for observation in deepcopy(spec['evaluation']['observables']):
        observation.update(id=observation['id']+'-reverse',case_id='reverse')
        spec['evaluation']['observables'].append(observation)
    spec['budget']['wall_seconds']=1200
    original=deepcopy(spec);sha=record_sha256(spec,schema_name='spec')
    root=directory/'pipeline execution 中文';root.mkdir();(root/'meshes').mkdir()
    guard=BudgetGuard(spec['budget'],roots=lambda:[root,native.owned.path],cancelled=lambda:False,memory_observer=native.controller.observed_memory_bytes)
    events=[]
    state=ExecutionState({'spec_sha256':sha},settings,{'spec':spec,'geometry_path':geometry_dir/'geometry.brep','geometry':geometry,'regions':regions,'ledger':build_load_ledger(spec,{'geometry':geometry,'regions':regions})},root,native,guard,lambda stage,*args,**kwargs:events.append(stage))
    result=_run_cases(state)
    (root/'convergence.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    assert result['numerical_status']=='passed'
    assert len(state.meshes)==3 and len(state.levels)==6
    for level in state.levels:
        assert level['numerical_status']=='passed'
        assert level['applicability_status']=='limited_screening'
        observations={item['field']:item['value'] for item in level['observables']}
        assert observations['displacement_magnitude']==pytest.approx(.0002,rel=.02)
        assert observations['von_mises']==pytest.approx(.4,rel=.02)
    assert spec==original
    assert events.count('meshing')==3
    assert not list(root.rglob('completion.json'))
