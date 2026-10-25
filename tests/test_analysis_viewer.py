import pytest

from mechanical_design_agent.analysis_types import AnalysisError
from test_analysis_coordinator import setup,submit


def test_interactive_viewer_rejects_unsolved_runs_before_starting_a_process(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_viewer import open_viewer
    from mechanical_design_agent import analysis_viewer
    _,_,store,_,approved,coordinator,_=setup(tmp_path);queued=submit(coordinator,approved)
    monkeypatch.setattr(analysis_viewer.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('unsolved input must not start a GUI'))
    with pytest.raises(AnalysisError) as error:open_viewer(store,design_id='fixture-design',analysis_id='static',run_id=queued['run_id'],case_id='static')
    assert error.value.code=='ANALYSIS_GUI_RUN_UNVALIDATED'


def test_viewer_command_requires_an_explicit_case_instead_of_selecting_the_first():
    from mechanical_design_agent.cli import _parser
    with pytest.raises(SystemExit):_parser().parse_args(['analysis','open','--design-id','fixture','--analysis-id','static','--run-id','run-fixture'])
