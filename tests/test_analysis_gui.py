"""Product GUI gates; a result file alone is insufficient evidence."""
from copy import deepcopy
import hashlib
import json
import struct
import pytest

from mechanical_design_agent.analysis_types import AnalysisError


def test_unknown_or_other_run_gui_receipt_is_rejected_before_archive_access(tmp_path,monkeypatch):
    from mechanical_design_agent.analysis_gui import verify_native_receipt
    with pytest.raises(AnalysisError) as error:
        verify_native_receipt({'status':'passed','request_id':'other'},request={'request_id':'ours'},root=tmp_path,deck={},fields={})
    assert error.value.code=='ANALYSIS_GUI_RECEIPT_INVALID'
    assert list(tmp_path.iterdir())==[]


def test_product_gui_rebuild_has_no_fcstd_restore_or_solver_call():
    import ast
    from mechanical_design_agent.package_resources import freecad_scripts_directory
    with freecad_scripts_directory() as root:tree=ast.parse((root/'analysis_gui_rebuild.py').read_text())
    methods={node.func.attr for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)}
    assert not methods&{'openDocument','load_results','run','run_ccx','setup_ccx'}
    assert {'saveAs','click','setEdit'}<=methods


@pytest.mark.parametrize('changed',['step','unit','averaging','field_values','request','blank'])
def test_wrong_gui_metadata_is_rejected_before_any_archive_audit(tmp_path,monkeypatch,changed):
    from mechanical_design_agent import analysis_gui as module
    fields={'fields':{'node_ids':[1],'displacement':{'values':[[0.,0.,0.]]},'stress':{'values':[[0.]*6]},'von_mises':{'values':[0.]},'principal_stress':{'values':[[0.,0.,0.]]}}}
    identity={'spec_sha256':'a'*64,'case_id':'static','mesh_sha256':'b'*64,'input_sha256':'c'*64}
    request={'request_id':'ours','identity':identity,'modules':{'faceforce':{'sha256':'d'*64},'output':{'sha256':'e'*64}}}
    expected={'node_ids':[1],**{name:fields['fields'][name]['values'] for name in ('displacement','stress','von_mises','principal_stress')}}
    step={'number':1,'increment':1,'time':1.}
    displays={}
    for name,unit in [('displacement_x','mm'),('displacement_magnitude','mm'),('von_mises','MPa')]:
        displays[name]={'unit':unit,'deformation_scale':0,'legend_policy':'freecad-1.1.3-native-nodal','nodal_values_sha256':hashlib.sha256(json.dumps([0.],allow_nan=False).encode()).hexdigest(),'viewport_pixels':{'status':'passed','colored_pixels':100,'colored_fraction':.1},'raw_min':0.,'raw_max':0.,'le_min':{'normalized_value':0.,'display_quantum':.001},'le_max':{'normalized_value':0.,'display_quantum':.001},'step':step.copy(),'stress_recovery':'native_extrapolated_averaged_nodal','native_image':name+'-native.png','figure':name+'-figure.png','panel':name+'-panel.png'}
    report={'status':'passed','request_id':'ours','request_sha256':hashlib.sha256(module._bytes(request)).hexdigest(),'identity':identity,'modules_before':deepcopy(request['modules']),'modules_after':deepcopy(request['modules']),'source_fcstd_open_count':0,'native_write_button':True,'result_task':True,'solver_task':True,'deformation_scale':0,'step':step,'saved_result_mesh_visible':True,'graphics_contexts':[{'valid':True,'version':[3,2]}],'mesh_inventory':{'nodes':1,'elements':1},'objects':[dict(zip(('name','type_id','proxy_module','proxy_class'),(name,*value))) for name,value in module.VIEW_PROVIDERS.items()],'field_values_sha256':hashlib.sha256(json.dumps(expected,sort_keys=True,allow_nan=False).encode()).hexdigest(),'display_fields':displays}
    report['mesh_viewport_pixels']={'status':'passed'}
    report['saved_result_visible']=True
    if changed=='step':displays['von_mises']['step']['increment']=2
    elif changed=='unit':displays['von_mises']['unit']='Pa'
    elif changed=='averaging':displays['von_mises']['stress_recovery']='integration_point'
    elif changed=='field_values':report['field_values_sha256']='f'*64
    elif changed=='request':report['request_sha256']='f'*64
    else:displays['von_mises']['viewport_pixels']['colored_fraction']=0
    calls=[];monkeypatch.setattr(module,'inspect_native_archive',lambda *args:calls.append(args))
    with pytest.raises(AnalysisError):module.verify_native_receipt(report,request=request,root=tmp_path,deck={'contract':{'node_count':1,'element_count':1}},fields=fields)
    assert calls==[]
