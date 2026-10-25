import pytest

from mechanical_design_agent.analysis_types import AnalysisError

STA='SUMMARY OF JOB INFORMATION\n STEP INC ATT ITRS TOT TIME STEP TIME INC TIME\n 1 1 1 1 0.100000E+01 0.100000E+01 0.100000E+01\n'


def test_only_a_completed_single_linear_increment_has_solver_success():
    from mechanical_design_agent.analysis_solver import inspect_solver_completion
    assert inspect_solver_completion('Static analysis was selected\n Job finished\n','',STA,version='2.23')['status']=='passed'
    for text,sta in [('not finished',STA),('Job finished\n *ERROR singular matrix',STA),('Job finished\n zero pivot',STA),('Job finished',STA.replace(' 1 1 1 1',' 1 2 1 1')),('Job finished',STA+' 1 2 1 1 1 1 1\n')]:
        with pytest.raises(AnalysisError):inspect_solver_completion(text,'',sta,version='2.23')
