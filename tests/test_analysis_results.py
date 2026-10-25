from copy import deepcopy

import pytest

from mechanical_design_agent.analysis_types import AnalysisError

DAT = ''' S T E P 1
 INCREMENT 1

 forces (fx,fy,fz) for set FIXED and time 0.1000000E+01

 1 0.000000E+00 0.000000E+00 2.000000E+01
 2 0.000000E+00 0.000000E+00 2.000000E+01

 total force (fx,fy,fz) for set FIXED and time 0.1000000E+01

 0.000000E+00 0.000000E+00 4.000000E+01
'''


def test_nodal_rf_requires_exact_ids_step_and_native_total():
    from mechanical_design_agent.analysis_results import read_nodal_rf
    result=read_nodal_rf(DAT,solver_version='2.23',fixed_node_ids=[1,2],set_name='FIXED',step=1,increment=1,time=1.)
    assert result['node_ids']==[1,2]
    assert result['values']==[[0.,0.,20.],[0.,0.,20.]]
    assert result['reported_total']==[0.,0.,40.]


@pytest.mark.parametrize('old,new', [('S T E P 1','S T E P 2'),('INCREMENT 1','INCREMENT 2'),(' 2 0.',' 3 0.'),('4.000000E+01','3.000000E+01'),('0.1000000E+01','0.2000000E+01'),('2.000000E+01','NaN')])
def test_wrong_or_incomplete_rf_cannot_produce_balance_input(old,new):
    from mechanical_design_agent.analysis_results import read_nodal_rf
    with pytest.raises(AnalysisError):read_nodal_rf(DAT.replace(old,new),solver_version='2.23',fixed_node_ids=[1,2],set_name='FIXED',step=1,increment=1,time=1.)


def bundle():
    return {'schema_version':'AnalysisFieldBundle/v1','unit_system':'N-mm-s-tonne','coordinate_system':'global','location':'nodal','stress_recovery':'native_extrapolated_averaged_nodal','step':{'number':1,'increment':1,'time':1.},'mesh_sha256':'a'*64,'node_ids':[1,2], 'displacement':{'unit':'mm','values':[[0.,0.,0.],[0.,0.,0.]]},'stress':{'unit':'MPa','values':[[0.]*6,[0.]*6]},'von_mises':{'unit':'MPa','values':[0.,0.]},'principal_stress':{'unit':'MPa','values':[[0.,0.,0.],[0.,0.,0.]]},'raw_rf':{'unit':'N','node_ids':[1],'values':[[0.,0.,0.]],'reported_total':[0.,0.,0.],'rounding_bound':[0.,0.,0.]}}


def mesh():return {'mesh_sha256':'a'*64,'node_ids':[1,2],'fixed_node_ids':[1]}


def test_native_frd_coordinate_serialization_is_exact_not_a_physical_tolerance():
    from mechanical_design_agent.resources.freecad.analysis_extract_results import frd_coordinate
    assert frd_coordinate(9.107915117961)==9.10791
    assert float(format(9.107915117961,'.5E'))==9.10792
    assert frd_coordinate(8.60910495372)==8.60911
    assert frd_coordinate(0.)==0.


def test_frd_duplicate_truncated_or_wrong_version_records_never_reach_native_reader():
    from mechanical_design_agent.resources.freecad.analysis_extract_results import inspect_frd_envelope
    record=' -1'+str(1).rjust(10)+' 0.0\n'
    text='    1C\n    1UVERSION Version 2.23\n'
    for section in ('2C','3C','DISP','STRESS','TOSTRAIN'):
        text+=('    '+section if section in ('2C','3C') else ' -4 '+section)+'\n'+record+' -3\n'
    text+=' 9999\n'
    inspect_frd_envelope(text,'2.23',[1],[1])
    for changed in (text.replace(record,record+record,1),text.replace(' 9999',''),text.replace('Version 2.23','Version 2.22'),text.replace('STRESS','UNKNOWN')):
        with pytest.raises(ValueError):inspect_frd_envelope(changed,'2.23',[1],[1])


def test_zero_field_is_valid_but_empty_is_not():
    from mechanical_design_agent.analysis_results import validate_fields
    assert validate_fields(bundle(),mesh())['validity']=='passed'
    empty=bundle();empty['displacement']['values']=[]
    with pytest.raises(AnalysisError) as error:validate_fields(empty,mesh())
    assert error.value.code=='RESULT_FIELD_EMPTY'


def test_old_and_new_units_normalize_identically():
    from mechanical_design_agent.analysis_results import validate_fields
    first=bundle();first['displacement']['values'][1][0]=1.;first['stress']['values'][1][0]=1.;first['von_mises']['values'][1]=1.;first['principal_stress']['values'][1]=[1.,0.,0.]
    second=deepcopy(first);second['displacement']={'unit':'m','values':[[0.,0.,0.],[.001,0.,0.]]}
    for name in ('stress','von_mises','principal_stress'):
        second[name]['unit']='Pa'
        if name=='von_mises':second[name]['values'][1]=1000000.
        else:second[name]['values'][1][0]=1000000.
    assert validate_fields(first,mesh())==validate_fields(second,mesh())


@pytest.mark.parametrize('change',['ids','nonfinite','rf','location','principal','unknown'])
def test_missing_rf_unknown_locations_and_inconsistent_fields_reject(change):
    from mechanical_design_agent.analysis_results import validate_fields
    bad=bundle()
    if change=='ids':bad['node_ids']=[1,1]
    if change=='nonfinite':bad['stress']['values'][0][0]=float('nan')
    if change=='rf':bad['raw_rf']['values']=[]
    if change=='location':bad['stress_recovery']='integration_point_peak'
    if change=='principal':bad['principal_stress']['values'][1][0]=1.
    if change=='unknown':bad['units_assumed']=True
    with pytest.raises(AnalysisError):validate_fields(bad,mesh())


@pytest.mark.parametrize('returncode,input_sha', [(1,'a'*64),(0,'b'*64)])
def test_failed_solver_or_stale_input_never_starts_native_extraction(tmp_path,returncode,input_sha):
    from types import SimpleNamespace
    from mechanical_design_agent.analysis_runtime_patch import PROFILE
    from mechanical_design_agent.analysis_results import extract_fields
    settings={'configuration':{'runtime':{'profile':PROFILE}}}
    native=SimpleNamespace(settings=settings,run=lambda *args,**kwargs:pytest.fail('must not execute native reader'))
    deck={'inspection':{'rf_output':'nodal_and_totals'},'input_ref':{'sha256':'a'*64}}
    outputs={'input_sha256':input_sha,'outcome':{'reason':'exited','returncode':returncode}}
    with pytest.raises(AnalysisError) as error:extract_fields(deck,outputs,settings,tmp_path/'fields',transaction=native)
    assert error.value.code=='RESULT_SOLVER_NOT_SUCCESSFUL'
    assert list(tmp_path.iterdir())==[]
