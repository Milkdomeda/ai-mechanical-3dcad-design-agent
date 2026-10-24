import os
from types import SimpleNamespace
import sys
import pytest


@pytest.mark.parametrize('wrong_temp',[False,True])
def test_identity_collects_only_current_context_and_requires_actual_private_temp(tmp_path,monkeypatch,wrong_temp):
    import fea_r1_runtime
    from fea_gui_identity import collect
    home=tmp_path/'home';home.mkdir();scratch=tmp_path/'temp';scratch.mkdir()
    module=SimpleNamespace()
    app=SimpleNamespace(listDocuments=lambda:{},getUserAppDataDir=lambda:str(home),getTempPath=lambda:str(home if wrong_temp else scratch))
    monkeypatch.setitem(sys.modules,'FreeCAD',app)
    monkeypatch.setitem(sys.modules,'femmesh',SimpleNamespace(meshtools=module))
    monkeypatch.setattr(fea_r1_runtime,'loaded_module_receipt',lambda expected,actual:{'module_sha256':'a'*64})
    monkeypatch.setattr(fea_r1_runtime,'native_identity',lambda:{'app_version':['1','1','3']})
    request={'session_id':'ours','case_id':None,'home':str(home),'scratch':str(scratch),'native_expected':{}}
    if wrong_temp:
        with pytest.raises(ValueError,match='directory'):collect(request)
    else:
        value=collect(request)
        assert value['documents']==[] and value['pid']==os.getpid()
        assert value['session_id']=='ours' and value['startup_verified'] is True


def test_startup_identity_does_not_accept_an_already_open_document(tmp_path,monkeypatch):
    import fea_r1_runtime
    from fea_gui_identity import collect
    app=SimpleNamespace(listDocuments=lambda:{'unexpected':object()},getUserAppDataDir=lambda:str(tmp_path),getTempPath=lambda:str(tmp_path))
    monkeypatch.setitem(sys.modules,'FreeCAD',app)
    monkeypatch.setitem(sys.modules,'femmesh',SimpleNamespace(meshtools=object()))
    monkeypatch.setattr(fea_r1_runtime,'loaded_module_receipt',lambda expected,actual:{'module_sha256':'a'*64})
    monkeypatch.setattr(fea_r1_runtime,'native_identity',lambda:{})
    with pytest.raises(ValueError,match='empty'):collect({'session_id':'ours','case_id':None,'home':str(tmp_path),'scratch':str(tmp_path),'native_expected':{}})
