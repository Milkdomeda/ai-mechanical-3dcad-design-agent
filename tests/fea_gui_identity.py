"""Trusted GUI-console receipt collector. No document restore or mutation."""
import datetime as dt
import os
import json
import hashlib
from pathlib import Path
import sys
import traceback


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(reference):
    path=Path(reference['path'])
    if not path.is_file() or path.is_symlink() or digest(path)!=reference['sha256']:raise ValueError('native evidence hash/path mismatch')
    return path


def read_json(path):
    def reject(value):raise ValueError('nonfinite JSON '+value)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'),parse_constant=reject,object_pairs_hook=unique)


def write_json(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def collect(request):
    import FreeCAD as App
    from femmesh import meshtools
    from fea_r1_runtime import loaded_module_receipt,native_identity
    before=loaded_module_receipt(request['native_expected'],meshtools)
    if not os.path.samefile(App.getUserAppDataDir(),request['home']) or not os.path.samefile(App.getTempPath(),request['scratch']):raise ValueError('GUI user/temp directory mismatch')
    documents=list(App.listDocuments())
    if request['case_id'] is None and documents:raise ValueError('startup must be empty')
    result={'session_id':request['session_id'],'pid':os.getpid(),'at':dt.datetime.now(dt.timezone.utc).isoformat(),'documents':documents,'native_module_sha256':before['module_sha256'],'native_module_receipt':before,'native_identity':native_identity(),'actual_user_directory':App.getUserAppDataDir(),'actual_temp_directory':App.getTempPath(),'startup_verified':True}
    if request['case_id'] is not None:
        from fea_helpers import validate_result_fields
        session=read_json(checked(request['session']));case=next(c for c in session['cases'] if c['case_id']==request['case_id'])
        if os.getpid()!=request['gui_pid']:raise ValueError('GUI PID changed')
        doc=App.activeDocument()
        if doc is None or not os.path.samefile(doc.FileName,checked(case['input_fcstd'])):raise ValueError('wrong open document')
        mesh=doc.getObject('Mesh');fem=mesh.FemMesh
        errors=[obj.Name for obj in doc.Objects if any(v in ('Invalid','Error') for v in obj.State)]
        result.update(case_id=case['case_id'],file_path=doc.FileName,input_fcstd_sha256=digest(doc.FileName),mesh_inventory={'nodes':fem.NodeCount,'elements':fem.VolumeCount},objects=[{'name':obj.Name,'type_id':obj.TypeId} for obj in doc.Objects],errors=errors)
        load=doc.getObject('Load')
        result['load']={'force_N':float(load.Force.getValueAs('N').Value),'reversed':bool(load.Reversed),'references':[(o.Name,list(names)) for o,names in load.References]}
        if case['saved_result_present']:
            native=doc.getObject('CCX_Results');vectors=native.DisplacementVectors
            result['result_inventory']=validate_result_fields(fem.Nodes,list(native.NodeNumbers),[(v.x,v.y,v.z) for v in vectors],list(native.NodeStressXX),list(native.vonMises))
        import FreeCADGui as Gui
        from PySide import QtGui
        widgets=Gui.getMainWindow().findChildren(QtGui.QLineEdit,'le_working_dir')
        result['working_directory_from_panel']=[widget.text() for widget in widgets if widget.isVisible()]
    if loaded_module_receipt(request['native_expected'],meshtools)!=before:raise ValueError('loaded native code changed')
    return result


def main(request_path):
    request=read_json(request_path)
    try:value=collect(request)
    except Exception:value={'session_id':request['session_id'],'error':traceback.format_exc()}
    write_json(request['response_path'],value)


if __name__=='__main__':
    main(sys.argv[1])
