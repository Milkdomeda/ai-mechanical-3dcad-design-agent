import hashlib
import json
from pathlib import Path
import zipfile
import pytest
import sys
from test_fea_gui_input import INPUT


def pin(path):return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def source_fixture(tmp_path):
    if sys.platform!='darwin':pytest.skip('macOS GUI preparation; Windows interactive acceptance remains blocked')
    root=tmp_path/'source';root.mkdir()
    model=root/'analysis.FCStd'
    with zipfile.ZipFile(model,'w') as archive:
        archive.writestr('Document.xml','<Document><Objects><Object name="Solid" type="Part::Feature"/></Objects><ObjectData/></Document>')
    inp=root/'Mesh.inp';inp.write_text(INPUT)
    report=root/'probe.json';report.write_text(json.dumps({'schema_version':'FEANativeProbe/v1','automated_status':'passed','status':'blocked','artifacts':{p.name:pin(p)['sha256'] for p in (model,inp)}}))
    manifest=root/'manifest.json';manifest.write_text(json.dumps({'schema_version':'P0GuiAcceptancePreparation/v1','status':'planning_only','profile':'freecad-1.1.3-fem-faceforce-r1','native_module_sha256':'ca4af36e48f0b1cb6b3b57e7c63e61ed7a3de768df497d50d4fa171493634c53','platforms':{'macOS':{'references':{'p0':pin(report)},'cases':[{'case_id':'axial_bar','source_report_key':'p0','source_fcstd':pin(model),'reference_inp':pin(inp),'object_inventory':[{'name':'Solid','type_id':'Part::Feature'}],'proxy_metadata_inventory_only':[],'expected_mesh_inventory':{'nodes':10,'elements':1},'saved_result_present':False,'expected_declared_force_N':40,'expected_direction':'-Z','expected_reversed':False}]}}}))
    return manifest,root


def test_preparation_copies_exact_inputs_and_keeps_human_gate_pending(tmp_path):
    from fea_gui_evidence import prepare
    manifest,source=source_fixture(tmp_path)
    path=prepare(manifest,tmp_path/'GUI 中文','macOS')
    session=json.loads(path.read_text());case=session['cases'][0]
    assert Path(case['input_fcstd']['path']).read_bytes()==(source/'analysis.FCStd').read_bytes()
    assert session['status']=='awaiting_gui_startup'
    assert not list((tmp_path/'GUI 中文/gui-output/axial_bar').iterdir())


@pytest.mark.parametrize('defect',['source_hash','report_binding','unknown_proxy','existing_output'])
def test_changed_or_unreviewed_sources_cannot_prepare(tmp_path,defect):
    from fea_gui_evidence import prepare
    manifest,root=source_fixture(tmp_path);value=json.loads(manifest.read_text());case=value['platforms']['macOS']['cases'][0]
    target=tmp_path/'GUI'
    if defect=='source_hash':(root/'Mesh.inp').write_text(INPUT+'** changed\n')
    if defect=='report_binding':case['source_fcstd']['sha256']='0'*64
    if defect=='unknown_proxy':
        with zipfile.ZipFile(root/'analysis.FCStd','w') as z:z.writestr('Document.xml','<Document><Objects/><ObjectData><Python module="evil" class="Run" value="x"/></ObjectData></Document>')
        case['source_fcstd']=pin(root/'analysis.FCStd')
        p=root/'probe.json';r=json.loads(p.read_text());r['artifacts']['analysis.FCStd']=case['source_fcstd']['sha256'];p.write_text(json.dumps(r));value['platforms']['macOS']['references']['p0']=pin(p)
    if defect=='existing_output':target.mkdir();(target/'keep').write_text('keep')
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):prepare(manifest,target,'macOS')
    if defect=='existing_output':assert (target/'keep').read_text()=='keep'


def test_export_without_user_actions_never_satisfies_manual_gate(tmp_path):
    from fea_gui_evidence import prepare,validate_case
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS')
    session=json.loads(path.read_text());output=Path(session['cases'][0]['output_directory'])/'Mesh.inp';output.write_text(INPUT)
    result=validate_case(path,'axial_bar',[])
    assert result['status']=='blocked'
    assert result['checks']['human_actions'] is False


@pytest.mark.parametrize('defect',['actor','pid','hash','screenshot'])
def test_stale_or_assistant_generated_manual_receipts_fail(tmp_path,defect):
    from fea_gui_evidence import prepare,validate_case
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS')
    session=json.loads(path.read_text());case=session['cases'][0]
    screenshot=Path(session['workspace'])/'screenshots/one.png';screenshot.write_bytes(b'screenshot fixture')
    actions=[{'case_id':'axial_bar','action':action,'actor':'human','operator':'tester','gui_pid':123,'source_sha256':case['input_fcstd']['sha256'],'evidence':pin(screenshot),'at':'2026-10-24T12:00:00+00:00'} for action in ('file_open','object_and_mesh_review','document_recompute','select_new_working_directory','native_write_inp_button','close_without_saving','operator_review')]
    if defect=='actor':actions[0]['actor']='assistant'
    if defect=='pid':actions[0]['gui_pid']=999
    if defect=='hash':actions[0]['source_sha256']='0'*64
    if defect=='screenshot':screenshot.unlink()
    assert validate_case(path,'axial_bar',actions)['status']!='passed'


def complete_receipts(path):
    session=json.loads(path.read_text());case=session['cases'][0];root=path.parent
    identity={'session_id':session['session_id'],'pid':123,'native_module_sha256':session['native_module_sha256'],'documents':[],'startup_verified':True}
    (root/'receipts/gui-startup.json').write_text(json.dumps(identity))
    (root/'receipts/launch.json').write_text(json.dumps({'session_id':session['session_id'],'status':'ready','gui_pid':123,'startup_receipt':pin(root/'receipts/gui-startup.json')}))
    (root/'receipts/axial_bar-gui.json').write_text(json.dumps({'session_id':session['session_id'],'pid':123,'case_id':'axial_bar','file_path':case['input_fcstd']['path'],'input_fcstd_sha256':case['input_fcstd']['sha256'],'mesh_inventory':{'nodes':10,'elements':1},'objects':case['object_inventory'],'errors':[],'working_directory_from_panel':[case['output_directory']]}))
    (Path(case['output_directory'])/'Mesh.inp').write_text(INPUT)
    actions=[]
    for index,action in enumerate(('file_open','object_and_mesh_review','document_recompute','select_new_working_directory','native_write_inp_button','close_without_saving','operator_review')):
        screenshot=root/'screenshots'/f'{index}.png';screenshot.write_bytes(b'\x89PNG\r\n\x1a\n'+bytes([index]))
        actions.append({'session_id':session['session_id'],'case_id':'axial_bar','action':action,'actor':'human','operator':'test-operator','user_statement':'Synthetic test attestation; never live evidence.','gui_pid':123,'source_sha256':case['input_fcstd']['sha256'],'evidence':pin(screenshot),'at':f'2026-10-24T12:00:0{index}+00:00'})
    return actions


def test_complete_bound_manual_receipts_allow_only_this_platform_case(tmp_path):
    from fea_gui_evidence import prepare,validate_case
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS')
    actions=complete_receipts(path);result=validate_case(path,'axial_bar',actions)
    assert result['status']=='passed'
    assert result['overall_p0_status']=='blocked'
    assert result['windows_gui_status']=='not_executed'


@pytest.mark.parametrize('defect',['wrong_pid','old_hash','missing_review','missing_snapshot','missing_output','copied_baseline','changed_source','changed_input','same_force_wrong_distribution','wrong_working_directory'])
def test_bound_gate_rejects_each_independent_failure(tmp_path,defect):
    from fea_gui_evidence import prepare,validate_case
    manifest,source=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS');actions=complete_receipts(path)
    session=json.loads(path.read_text());case=session['cases'][0];output=Path(case['output_directory'])/'Mesh.inp'
    if defect=='wrong_pid':actions[0]['gui_pid']=999
    if defect=='old_hash':actions[0]['source_sha256']='0'*64
    if defect=='missing_review':actions.pop()
    if defect=='missing_snapshot':(path.parent/'receipts/axial_bar-gui.json').unlink()
    if defect=='missing_output':output.unlink()
    if defect=='copied_baseline':output.unlink();output.symlink_to(source/'Mesh.inp')
    if defect=='changed_source':(source/'Mesh.inp').write_text(INPUT+'** altered\n')
    if defect=='changed_input':
        p=Path(case['input_fcstd']['path']);p.chmod(0o644);p.write_bytes(b'changed')
    if defect=='same_force_wrong_distribution':output.write_text(INPUT.replace('5,3,-20\n7,3,-20','5,3,-10\n7,3,-30'))
    if defect=='wrong_working_directory':
        p=path.parent/'receipts/axial_bar-gui.json';v=json.loads(p.read_text());v['working_directory_from_panel']=[str(source)];p.write_text(json.dumps(v))
    assert validate_case(path,'axial_bar',actions)['status']!='passed'


def test_prepared_session_cannot_redirect_reference_to_a_changed_input(tmp_path):
    from fea_gui_evidence import prepare,validate_case
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS');actions=complete_receipts(path)
    session=json.loads(path.read_text());case=session['cases'][0]
    wrong=Path(case['output_directory'])/'wrong-reference.inp';wrong.write_text(INPUT.replace('200000,.3','100000,.3'))
    (wrong.parent/'Mesh.inp').write_bytes(wrong.read_bytes())
    case['reference_copy']=pin(wrong);path.write_text(json.dumps(session))
    assert validate_case(path,'axial_bar',actions)['status']=='failed'


def test_summary_revalidates_bound_case_evidence_and_keeps_windows_blocked(tmp_path):
    from fea_gui_evidence import prepare,summarize
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS')
    actions=complete_receipts(path);file=tmp_path/'actions.json';file.write_text(json.dumps(actions))
    launch=path.parent/'receipts/launch.json';value=json.loads(launch.read_text());value['guardian']={'pid':100,'token':'owner'};launch.write_text(json.dumps(value))
    session=json.loads(path.read_text())
    (path.parent/'receipts/exit.json').write_text(json.dumps({'session_id':session['session_id'],'session':pin(path),'launch':pin(launch),'guardian':value['guardian'],'status':'completed','outcome':'exited','returncode':0,'runtime_verification':'passed','scratch_cleanup':'owned_contents_cleared_after_process_exit'}))
    report=summarize(path,{'axial_bar':file})
    assert report['macOS_gui_status']=='passed' and report['status']=='blocked'
    assert report['windows_gui_status']=='not_executed'
    assert report['cases']['axial_bar']['bindings']['actions_sha256']==hashlib.sha256(json.dumps(actions,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    file.write_text('[]')
    assert summarize(path,{'axial_bar':file})['macOS_gui_status']=='blocked'


@pytest.mark.parametrize('defect',['missing','timeout','runtime_failed','wrong_session','wrong_guardian','missing_cleanup'])
def test_summary_requires_successful_bound_owner_finalization(tmp_path,defect):
    from fea_gui_evidence import prepare,summarize
    manifest,_=source_fixture(tmp_path);path=prepare(manifest,tmp_path/'GUI','macOS')
    actions=complete_receipts(path);file=tmp_path/'actions.json';file.write_text(json.dumps(actions))
    session=json.loads(path.read_text());launch=path.parent/'receipts/launch.json'
    value=json.loads(launch.read_text());value['guardian']={'pid':100,'token':'owner'};launch.write_text(json.dumps(value))
    final={'session_id':session['session_id'],'session':pin(path),'launch':pin(launch),'guardian':value['guardian'],'status':'completed','outcome':'exited','returncode':0,'runtime_verification':'passed','scratch_cleanup':'owned_contents_cleared_after_process_exit'}
    if defect=='timeout':final.update(outcome='timed_out',returncode=-9)
    if defect=='runtime_failed':final.update(runtime_verification='failed')
    if defect=='wrong_session':final['session_id']='other'
    if defect=='wrong_guardian':final['guardian']={'pid':101}
    if defect=='missing_cleanup':final.pop('scratch_cleanup')
    if defect!='missing':(path.parent/'receipts/exit.json').write_text(json.dumps(final))
    assert summarize(path,{'axial_bar':file})['macOS_gui_status']!='passed'
