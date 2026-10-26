import json
from pathlib import Path
import subprocess
import sys
import time

from test_fea_gui_input import INPUT
from test_fea_gui_checks import native_snapshot


def test_each_mesa_process_boundary_hashes_the_complete_runtime_once_after_metadata(tmp_path,monkeypatch):
    import pytest
    import fea_gui_automation as automation
    import fea_gui_environment as environment
    identity={'module_sha256':'a'*64,'native_identity':{'version':'1.1.3'}};calls=[]
    references={name:{'path':str(tmp_path/name),'sha256':name} for name in ('config','base_manifest','base_config','compile_result')}
    value={'schema_version':'P0GuiEnvironment/v1','profile':environment.PROFILE,'status':'prepared','runtime_manifest_sha256':'b'*64,**references}
    manifest={'gui_environment':{'profile':environment.PROFILE},'tree':{'bin/opengl32.dll':{'sha256':environment.DRIVER_SHA256}}}
    config={'runtime':{'manifest_path':'manifest','manifest_sha256':'b'*64}}
    monkeypatch.setattr(environment,'read_json',lambda path:manifest if path=='manifest' else value)
    def metadata_digest(path):
        calls.append(('metadata',Path(path).name));return Path(path).name
    monkeypatch.setattr(environment,'digest',metadata_digest)
    def tools(config,*,full_tree=True):
        calls.append(('tools',full_tree));assert not full_tree
        return identity
    def complete(config):calls.append(('complete_tree',True));return identity
    monkeypatch.setattr(automation,'verify_runtime',tools)
    monkeypatch.setattr(environment,'verify_runtime',complete)
    for boundary in ('before_native','after_native','before_next_native','after_next_native'):
        calls.clear()
        assert automation._verify_gui_pass_runtime(config,'environment')==(identity,value)
        assert calls==[('tools',False),*(('metadata',name) for name in references),('complete_tree',True)],boundary
    monkeypatch.setattr(environment,'verify_runtime',lambda config:{'module_sha256':'changed'})
    with pytest.raises(ValueError,match='native identity'):
        automation._verify_gui_pass_runtime(config,'environment')


def test_unprepared_gui_boundaries_still_require_a_complete_runtime_check(monkeypatch):
    import fea_gui_automation as module
    calls=[]
    def verify(config):calls.append(config);return {'full':'verified'}
    monkeypatch.setattr(module,'verify_runtime',verify)
    assert module._verify_gui_pass_runtime({'config':'ours'},None)==({'full':'verified'},None)
    assert calls==[{'config':'ours'}]


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
