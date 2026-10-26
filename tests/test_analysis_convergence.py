from copy import deepcopy

import pytest

from fea_evaluation_fixture import evaluation_spec
from test_analysis_loads import load_fixture


def spec():
    return evaluation_spec()


def levels(displacement=(1.,1.01,1.015),stress=(10.,10.1,10.15),specification=None):
    from mechanical_design_agent.analysis_protocol import record_sha256
    value=specification or spec()
    binding={'spec_sha256':record_sha256(value,schema_name='spec'),'geometry_sha256':value['source']['geometry_sha256'],'regions_sha256':value['source']['regions_sha256'],'evaluation_sha256':record_sha256(value['evaluation'],schema_name='evaluation-policy')}
    return [{'case_id':'static','level':i,'numerical_status':'passed','applicability_status':'limited_screening','binding':deepcopy(binding),'mesh':{'sha256':str(i+1)*64,'node_count':100+100*i,'element_count':50+50*i,'size_mm':('4','2.8','1.96')[i]},'observables':[{'id':'deflection','field':'displacement_magnitude','region_id':'whole-model','unit':'mm','value':d,'mandatory':True},{'id':'midspan-stress','field':'von_mises','region_id':'midspan-roi','unit':'MPa','value':s,'mandatory':True}]} for i,(d,s) in enumerate(zip(displacement,stress))]


def test_three_levels_need_two_successive_comparisons_for_each_required_quantity():
    from mechanical_design_agent.analysis_validation import evaluate_convergence
    assert evaluate_convergence(spec(),levels())['numerical_status']=='passed'
    assert evaluate_convergence(spec(),levels()[:2])['numerical_status']=='inconclusive'
    assert evaluate_convergence(spec(),levels(displacement=(.8,1.01,1.015)))['numerical_status']=='failed'


def test_displacement_convergence_does_not_imply_engineering_stress_convergence():
    from mechanical_design_agent.analysis_validation import evaluate_convergence
    result=evaluate_convergence(spec(),levels(stress=(10.,14.,20.)))
    assert result['numerical_status']=='failed'
    assert not all(item['passed'] for item in result['comparisons'] if item['observable_id']=='midspan-stress')


def test_near_zero_observable_uses_declared_atol_and_changed_bindings_reject():
    from mechanical_design_agent.analysis_validation import evaluate_convergence
    value=spec()
    assert evaluate_convergence(value,levels(displacement=(0.,1e-8,2e-8)))['numerical_status']=='passed'
    bad=levels();bad[2]['binding']['geometry_sha256']='changed'
    assert evaluate_convergence(value,bad)['numerical_status']=='failed'


def test_budget_exhaustion_or_invalid_levels_cannot_be_replaced_by_a_coarse_pass():
    from mechanical_design_agent.analysis_validation import evaluate_convergence
    data=levels();data[2]['numerical_status']='inconclusive'
    assert evaluate_convergence(spec(),data)['numerical_status']=='inconclusive'


def test_no_allowable_does_not_become_a_strength_pass_and_failed_limit_is_valid_result():
    from mechanical_design_agent.analysis_validation import evaluate_convergence,evaluate_criteria
    value=spec();valid=evaluate_convergence(value,levels())
    assert evaluate_criteria(value,valid)['criteria_status']=='not_evaluated'
    value['criteria']=[{'id':'deflection-limit','field':'displacement_magnitude','region_id':'whole-model','operator':'lte','limit':{'value':'.5','unit':'mm'},'absolute_tolerance':{'value':'.000001','unit':'mm'},'applicability_evidence':'Synthetic software acceptance limit.'}]
    valid=evaluate_convergence(value,levels(specification=value))
    assert evaluate_criteria(value,valid)['criteria_status']=='failed'
    assert valid['numerical_status']=='passed'


def test_changed_limit_or_a_reused_coarse_mesh_cannot_inherit_validation():
    from mechanical_design_agent.analysis_validation import evaluate_convergence,evaluate_criteria
    from mechanical_design_agent.analysis_types import AnalysisError
    value=spec();data=levels();data[2]['mesh']=deepcopy(data[1]['mesh'])
    assert evaluate_convergence(value,data)['numerical_status']=='failed'
    valid=evaluate_convergence(value,levels())
    value['criteria']=[{'id':'new-limit','field':'displacement_magnitude','region_id':'whole-model','operator':'lte','limit':{'value':'2','unit':'mm'},'absolute_tolerance':{'value':'.000001','unit':'mm'},'applicability_evidence':'Changed synthetic limit needs a new approved binding.'}]
    with pytest.raises(AnalysisError):evaluate_criteria(value,valid)
