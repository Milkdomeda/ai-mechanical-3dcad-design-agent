"""Orchestration tests with replaceable module collaborators, not native proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from fea_evaluation_fixture import evaluation_spec


def execution(tmp_path,monkeypatch,*,fail=False,change_material=False):
    from mechanical_design_agent import analysis_pipeline as module
    from mechanical_design_agent.analysis_protocol import record_sha256
    value=evaluation_spec();other=deepcopy(value['cases'][0]);other['case_id']='reverse';other['loads'][0]['components'][2]['value']='40'
    value['cases']=[other,*value['cases']]
    for obs in deepcopy(value['evaluation']['observables']):
        obs.update(id=obs['id']+'-reverse',case_id='reverse');value['evaluation']['observables'].append(obs)
    sha=record_sha256(value,schema_name='spec');trace=[]
    (tmp_path/'safe.brep').write_bytes(b'synthetic unit geometry; not native proof')
    (tmp_path/'meshes').mkdir()
    def generated(geometry,regions,level,settings,output,**kwargs):
        output.mkdir();artifacts={}
        for name in ('data','mapping'):
            content=b'{}';(output/(name+'.json')).write_bytes(content);artifacts[name]={'relative_path':name+'.json','sha256':hashlib.sha256(content).hexdigest()}
        artifacts['native_mesh']={'relative_path':'mesh.unv','sha256':str(level['level']+1)*64}
        mesh_bytes=('synthetic mesh '+str(level['level'])).encode();(output/'mesh.unv').write_bytes(mesh_bytes)
        artifacts['native_mesh']['sha256']=hashlib.sha256(mesh_bytes).hexdigest()
        trace.append(('mesh',level['level']))
        return {'level':level['level'],'artifacts':artifacts}
    def built(spec,geometry,mesh,output,settings,*,case_id,**kwargs):
        trace.append(('build',mesh['receipt']['level'],case_id,deepcopy(spec['material'])))
        if change_material:spec['material']['youngs_modulus']['value']='999'
        actual=record_sha256(spec,schema_name='spec')
        return {'spec_sha256':actual,'case_id':case_id,'level':mesh['receipt']['level'],'contract':{'spec_sha256':actual}}
    def solved(deck,mesh,settings,output,**kwargs):
        trace.append(('solve',deck['level'],deck['case_id']))
        if fail:raise AnalysisError('SOLVER_DIAGNOSTIC_FAILED','Synthetic solver failure, no altered inputs.')
        return deck
    def extracted(deck,outputs,settings,output,**kwargs):
        output.mkdir();content=json.dumps(outputs).encode();(output/'fields.json').write_bytes(content)
        return {**outputs,'artifact':{'relative_path':'fields.json','sha256':hashlib.sha256(content).hexdigest()}}
    def validated(spec,ledger,mesh,fields):
        index=fields['level'];case=fields['case_id']
        obs=[{'id':declaration['id'],'field':declaration['field'],'region_id':declaration['region_id'],'unit':'mm' if declaration['field'].startswith('displacement') else 'MPa','value':(.1 if declaration['field'].startswith('displacement') else 10.)*(1+.01/(index+1)),'mandatory':declaration['mandatory']} for declaration in spec['evaluation']['observables'] if declaration['case_id']==case]
        return {'case_id':case,'level':index,'binding':module._binding(spec) if hasattr(module,'_binding') else __import__('mechanical_design_agent.analysis_validation',fromlist=['_binding'])._binding(spec),'mesh':{'sha256':str(index+1)*64,'size_mm':('4','2.8','1.96')[index],'node_count':100*(index+1),'element_count':50*(index+1)},'numerical_status':'passed','applicability_status':'limited_screening','observables':obs}
    monkeypatch.setattr(module,'generate_mesh',generated);monkeypatch.setattr(module,'build_native_deck',built);monkeypatch.setattr(module,'solve_native',solved);monkeypatch.setattr(module,'extract_fields',extracted);monkeypatch.setattr(module,'validate_level',validated)
    guard=SimpleNamespace(checkpoint=lambda **kwargs:None)
    inputs={'spec':value,'geometry_path':tmp_path/'safe.brep','geometry':{},'regions':{},'ledger':{}}
    state=module.ExecutionState({'spec_sha256':sha},{},inputs,tmp_path,object(),guard,lambda *args,**kwargs:None)
    return module,state,trace


@pytest.mark.parametrize('changed',['geometry','policy','artifact'])
def test_mesh_reuse_requires_matching_upstream_and_unchanged_artifacts(tmp_path,monkeypatch,changed):
    module,state,trace=execution(tmp_path,monkeypatch)
    level=module.plan_mesh_levels(state.inputs['spec'])[0]
    first=module._mesh(state,level)
    if changed=='geometry':state.inputs['geometry_path'].write_bytes(b'replaced')
    elif changed=='policy':level['size']['value']='3'
    else:(Path(first['directory'])/'mesh.unv').write_bytes(b'replaced')
    with pytest.raises(AnalysisError):module._mesh(state,level)
    assert [item for item in trace if item[0]=='mesh']==[('mesh',0)]


def test_pipeline_preserves_declared_case_order_and_shares_only_the_same_level_mesh(tmp_path,monkeypatch):
    module,state,trace=execution(tmp_path,monkeypatch)
    original=deepcopy(state.inputs['spec'])
    result=module._run_cases(state)
    assert result['numerical_status']=='passed'
    assert [item for item in trace if item[0]=='solve']==[('solve',index,case) for index in range(3) for case in ('reverse','static')]
    assert [item for item in trace if item[0]=='mesh']==[('mesh',0),('mesh',1),('mesh',2)]
    assert state.inputs['spec']==original
    assert not (tmp_path/'completion.json').exists()


def test_failure_never_reduces_load_adds_support_or_changes_material(tmp_path,monkeypatch):
    module,state,trace=execution(tmp_path,monkeypatch,fail=True)
    original=deepcopy(state.inputs['spec'])
    with pytest.raises(AnalysisError):module._run_cases(state)
    assert state.inputs['spec']==original
    assert len([item for item in trace if item[0]=='solve'])==1
    assert not list(tmp_path.rglob('completion.json'))


def test_mutating_native_builder_is_rejected_before_a_solver_can_run(tmp_path,monkeypatch):
    module,state,trace=execution(tmp_path,monkeypatch,change_material=True)
    original=deepcopy(state.inputs['spec'])
    with pytest.raises(AnalysisError) as error:module._run_cases(state)
    assert error.value.code=='ANALYSIS_SPEC_CHANGED'
    assert not any(item[0]=='solve' for item in trace)
    assert state.inputs['spec']==original
