import json
from pathlib import Path
import subprocess
import sys
import time

from test_fea_gui_input import INPUT
from test_fea_gui_checks import native_snapshot


def test_gui_inventory_is_bound_to_parsed_native_input_not_its_own_claim(tmp_path):
    from fea_gui_automation import assess_case
    reference=tmp_path/'reference.inp';exported=tmp_path/'exported.inp'
    reference.write_text(INPUT);exported.write_text(INPUT)
    value=native_snapshot();value.update(case_id='axial_bar',source_unchanged=True,recompute_errors=[],closed_without_saving=True)
    assert assess_case(value,reference,exported)['status']=='passed'
    value['mesh_inventory']['nodes']=11;value['result_inventory']['nodes']=11
    assert assess_case(value,reference,exported)['status']=='failed'


def test_gui_automation_cli_is_explicit_and_rejects_arbitrary_source_restore():
    script=Path(__file__).resolve().parents[1]/'scripts'/'fea_gui_automation.py'
    help_run=subprocess.run([sys.executable,str(script),'--help'],capture_output=True,text=True)
    assert help_run.returncode==0,help_run.stderr
    assert '--native-scratch-root' in help_run.stdout
    invalid=subprocess.run([sys.executable,str(script),'--source','customer.FCStd'],capture_output=True,text=True)
    assert invalid.returncode==2
    assert 'Traceback' not in invalid.stderr


def test_gui_summary_roundtrips_chinese_paths_through_legacy_windows_console():
    import runpy
    module=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts'/'fea_gui_automation.py'))
    path=Path('GUI automation 中文')
    result={'automated_status':'failed','visual_review_status':'pending','overall_p0_status':'blocked','error':'路径错误'}
    text=module['result_summary'](path,result)
    restored=json.loads(text.encode('cp1252').decode('cp1252'))
    assert restored['report']==str(path/'gui-automation.json')
    assert restored['error']=='路径错误'


def test_gui_matrix_uses_a_fresh_process_and_bound_producer_for_every_case(tmp_path,monkeypatch):
    import fea_gui_automation as module
    calls=[];ticks=[0.]
    monkeypatch.setattr(module,'verify_runtime',lambda config:{})
    monkeypatch.setattr(module,'time',time,raising=False)
    monkeypatch.setattr(module.time,'monotonic',lambda:ticks[0])
    def native(config,workspace,scratch,*,case_ids,producer=None,deadline=None,**kwargs):
        assert len(case_ids)==1,'native Qt forms must not survive between distinct fixture cases'
        assert deadline is not None and 0<deadline-ticks[0]<=600
        key=case_ids[0];workspace.mkdir();ticks[0]+=1
        path=workspace/'native.json';path.write_text(json.dumps({'case':key}))
        reference={'path':str(path),'sha256':module.digest(path)}
        calls.append((key,producer,reference))
        return {'automated_status':'passed','runtime_unchanged':True,'startup_pid':len(calls),'native':reference,'producer':producer,'cases':{key:{'status':'passed'}}}
    monkeypatch.setattr(module,'_run_gui_pass',native)
    report=module.run_gui_automation({},tmp_path/'new matrix',tmp_path/'scratch')
    assert report['automated_status']=='passed' and set(report['cases'])==set(module.CASE_IDS)
    assert len(calls)==10
    for index,key in enumerate(module.CASE_IDS):
        assert calls[index][0]==calls[index+5][0]==key
        assert calls[index+5][1]==calls[index][2]
    assert report['manual_gui_status']=='not_executed' and report['overall_p0_status']=='blocked'


def test_isolated_cases_share_the_original_600_second_phase_budget(tmp_path,monkeypatch):
    import fea_gui_automation as module
    ticks=[0.];calls=[]
    monkeypatch.setattr(module,'verify_runtime',lambda config:{})
    monkeypatch.setattr(module,'time',time,raising=False)
    monkeypatch.setattr(module.time,'monotonic',lambda:ticks[0])
    def native(config,workspace,scratch,*,case_ids,deadline=None,**kwargs):
        calls.append(case_ids);ticks[0]+=130
        return {'automated_status':'passed','runtime_unchanged':True,'startup_pid':len(calls),'native':{},'cases':{case_ids[0]:{'status':'passed'}}}
    monkeypatch.setattr(module,'_run_gui_pass',native)
    report=module.run_gui_automation({},tmp_path/'new matrix',tmp_path/'scratch')
    assert report['automated_status']=='failed'
    assert report['passes']['build']['budget']['status']=='failed'
    assert report['passes']['build']['budget']['wall_seconds']==600
    assert len(calls)==5 and 'roundtrip' not in report['passes']


def test_case_isolation_cannot_claim_roundtrip_in_the_producer_process(tmp_path,monkeypatch):
    import fea_gui_automation as module
    monkeypatch.setattr(module,'verify_runtime',lambda config:{})
    def native(config,workspace,scratch,*,case_ids,producer=None,**kwargs):
        return {'automated_status':'passed','runtime_unchanged':True,'startup_pid':42,'native':{'path':'fixed synthetic reference','sha256':'0'*64},'cases':{case_ids[0]:{'status':'passed'}}}
    monkeypatch.setattr(module,'_run_gui_pass',native)
    report=module.run_gui_automation({},tmp_path/'new matrix',tmp_path/'scratch')
    assert report['automated_status']=='failed'
    assert not report['passes']['roundtrip']['independent_producer_consumer_processes']
    assert set(report['cases'])==set(module.CASE_IDS)
