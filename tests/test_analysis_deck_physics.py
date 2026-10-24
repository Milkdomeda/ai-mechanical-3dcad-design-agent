from copy import deepcopy
from pathlib import Path

import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_fea_gui_input import INPUT


def parsed(tmp_path, text=INPUT):
    from mechanical_design_agent.analysis_deck import _parse_input
    path=tmp_path/'input.inp'; path.write_text(text,encoding='utf-8')
    return _parse_input(path,allowed_root=tmp_path)


def expected():
    return {'material': {'youngs_modulus': {'value': '200000'}, 'poisson_ratio': {'value': '.3'}, 'density': None}, 'expected_fixed_nodes': {1,2,3}, 'allowed_force_nodes': {5,7}, 'expected_case': {'native_objects': [{'type': 'total_force'}], 'total_force_N': ['0','0','-40'], 'total_moment_Nmm': ['-10','10','0']}, 'validation': {'moment_origin': [{'value':'0'}]*3, **{key:{'value':value} for key,value in [('force_atol','1e-9'),('force_rtol','1e-12'),('moment_atol','1e-9'),('moment_rtol','1e-12')]}}, 'expected_pressures': {}, 'minimum_scaled_jacobian': .1}


def test_native_cload_force_moment_and_fixed_dofs_match(tmp_path):
    from mechanical_design_agent.analysis_deck_physics import check_declared_physics
    result=check_declared_physics(parsed(tmp_path),**expected())
    assert result['status']=='passed' and result['checked_solver_elements']==1
    assert result['integrals']['native']['force_N']==[0.,0.,-40.]


@pytest.mark.parametrize('old,new,code', [('5,3,-20','5,3,20','DECK_LOAD_LEDGER_MISMATCH'), ('FIXED,1,3','FIXED,1,2','DECK_SUPPORT_MISMATCH'), ('200000,.3','100000,.3','DECK_MATERIAL_MISMATCH'), ('5,3,-20','9,3,-20','DECK_FORCE_REGION_MISMATCH')])
def test_changed_native_physics_never_enters_solver(tmp_path,old,new,code):
    from mechanical_design_agent.analysis_deck_physics import check_declared_physics
    with pytest.raises(AnalysisError) as error:
        check_declared_physics(parsed(tmp_path,INPUT.replace(old,new)),**expected())
    assert error.value.code==code


def test_writer_rounding_has_no_engineering_tolerance_fallback():
    from mechanical_design_agent.analysis_deck_physics import writer_number_matches
    assert writer_number_matches('1.234567890123','1.2345678901234')
    assert not writer_number_matches('1.234567890122','1.2345678901234')
    assert not writer_number_matches('1e-15','0')
