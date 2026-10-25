import json

import pytest

from mechanical_design_agent.cli import _parser
from mechanical_design_agent.analysis_cli import dispatch
from mechanical_design_agent.analysis_types import AnalysisError


def test_analysis_cli_and_mcp_share_service_contract_for_run():
    calls=[]
    class Service:
        def run(self,**kwargs):calls.append(kwargs);return {'status':'queued'}
    args=_parser().parse_args(['analysis','run','--design-id','fixture','--analysis-id','static','--spec-sha256','a'*64,'--idempotency-key','once','--expected-revision','4'])
    assert dispatch(args,Service())['status']=='queued'
    assert calls==[dict(design_id='fixture',analysis_id='static',spec_sha256='a'*64,idempotency_key='once',expected_revision=4,retry_run_id=None)]


def test_cli_input_duplicates_fail_before_any_service_call(tmp_path):
    path=tmp_path/'request 中文.json';path.write_text('{"schema_version":"x","schema_version":"y"}',encoding='utf-8')
    args=_parser().parse_args(['analysis','prepare','--design-id','fixture','--analysis-id','static','--request',str(path)])
    with pytest.raises(AnalysisError) as error:dispatch(args,object())
    assert error.value.code=='ANALYSIS_JSON_DUPLICATE'


def test_cli_rejects_unknown_report_actions_and_passes_exact_reference_list(tmp_path):
    calls=[]
    class Service:
        def report(self,**kwargs):calls.append(kwargs);return {'status':'completed'}
    refs=tmp_path/'refs 中文.json';refs.write_text(json.dumps(['gui/g-fixture/outputs/u.png']),encoding='utf-8')
    prefix=['analysis','report','--design-id','fixture','--analysis-id','static','--run-id','run-fixture']
    args=_parser().parse_args(prefix+['--action','finalize_visual','--evidence-refs',str(refs),'--expected-revision','8'])
    assert dispatch(args,Service())['status']=='completed'
    assert calls[0]['evidence_refs']==['gui/g-fixture/outputs/u.png'] and calls[0]['expected_revision']==8
    with pytest.raises(SystemExit):_parser().parse_args(prefix+['--action','upload_results'])


@pytest.mark.parametrize('value,expected',[
    ({'status':'queued'},0),({'status':'needs_input'},2),
    ({'status':'setup_required'},3),({'run_status':'failed'},4),
    ({'run_status':'cancelled'},6),({'run_status':'timed_out'},6),
    ({'run_status':'interrupted'},6),
    ({'status':'completed','criteria_status':'failed'},0),
    ({'approval_state':'CONDITIONAL','spec_status':'ready_for_review'},2),
    (AnalysisError('ANALYSIS_REVISION_CONFLICT','fixture conflict'),5),
    (AnalysisError('ANALYSIS_SPEC_NOT_APPROVED','fixture missing approval'),2),
])
def test_analysis_main_exit_codes_do_not_change_legacy_commands(monkeypatch,value,expected):
    from mechanical_design_agent import cli
    from mechanical_design_agent.analysis_service import AnalysisService
    monkeypatch.setattr('sys.argv',['mechanical-design','analysis','capabilities'])
    monkeypatch.setattr(cli,'_runtime',lambda workspace:object())
    def respond(*args,**kwargs):
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(AnalysisService,'capabilities',respond)
    with pytest.raises(SystemExit) as error:cli.main()
    assert error.value.code==expected
    assert cli._exit_code({'status':'setup_required'})==2
    assert cli._exit_code({'status':'warning'})==1


@pytest.mark.parametrize('newline',['\n','\r\n'])
def test_decision_file_passes_exact_utf8_text_without_shell_interpolation(tmp_path,newline):
    path=tmp_path/'decision 中文.txt';text='确认'+newline+'固定原载荷；保留 "逐节点" 结果。'
    path.write_bytes(text.encode('utf-8'))
    calls=[]
    class Service:
        def approve(self,**kwargs):calls.append(kwargs);return {'status':'approved'}
    args=_parser().parse_args(['analysis','approve','--design-id','fixture','--analysis-id','static','--spec-sha256','a'*64,'--expected-revision','4','--decision-file',str(path)])
    dispatch(args,Service())
    assert calls[0]['decision_text']==text
