"""Trusted native GUI fixture worker, importable without the host package.

Only fixed synthetic cases belong here. Product reconstruction must use safe
geometry, approved contracts and validated fields, not persisted Proxy restore.
"""
import json
from pathlib import Path


CASE_IDS=('axial_bar','cantilever','multiface','curved_multiface','r1_mesh_4')


def fixture_spec(case_id):
    if not isinstance(case_id,str) or case_id not in CASE_IDS:raise ValueError('unknown synthetic GUI fixture')
    if case_id in ('axial_bar','cantilever'):
        return json.loads((Path(__file__).parent/'fixtures'/'fea'/(case_id+'.json')).read_text(encoding='utf-8'))
    return {'name':'multiface' if case_id=='multiface' else 'curved_multiface','length_mm':100,'width_mm':10,'height_mm':10,'youngs_modulus_MPa':200000,'poisson_ratio':.3,'force_N':40,'mesh_size_mm':4}


def run(request_path):
    """Schedule fixed fixture checks on this fresh FreeCAD GUI main thread."""
    import hashlib
    import math
    import os
    import sys
    import traceback
    sys.dont_write_bytecode=True
    from fea_gui_identity import read_json,write_json,digest,checked
    request=read_json(request_path)
    required={'schema_version','request_id','workspace','home','scratch','config','native','case_ids','worker_sources','mode','producer'}
    if set(request)!=required or request['schema_version']!='P0NativeGuiRequest/v1':raise ValueError('GUI request contract')
    ids=request['case_ids']
    if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):raise ValueError('GUI fixed cases required')
    specs={key:fixture_spec(key) for key in ids}
    if request['mode'] not in ('build_fixtures','restore_generated_fixtures'):raise ValueError('GUI fixture mode')
    producer=read_json(checked(request['producer'])) if request['mode']=='restore_generated_fixtures' else None
    if producer is not None and (producer.get('schema_version')!='P0NativeGuiWorker/v1' or set(producer.get('cases',{}))!=set(ids) or producer.get('error')):raise ValueError('GUI fixture producer provenance')
    for reference in request['worker_sources']:checked(reference)
    root=Path(request['workspace'])
    if not root.is_absolute() or root.resolve(strict=True)!=root:raise ValueError('GUI workspace identity')
    import FreeCAD as App
    import FreeCADGui as Gui
    import FemGui
    from PySide import QtCore,QtGui
    from femmesh import meshtools
    from fea_r1_runtime import loaded_module_receipt,native_identity
    from fea_gui_checks import inspect_generated_archive
    from fea_helpers import build_case,validate_result_fields
    if not App.GuiUp or list(App.listDocuments()):raise ValueError('fresh GUI required')
    if not os.path.samefile(App.getTempPath(),request['scratch']) or not os.path.samefile(App.getUserAppDataDir(),request['home']):raise ValueError('GUI private directories')
    before=loaded_module_receipt(request['native'],meshtools)
    report={'schema_version':'P0NativeGuiWorker/v1','request_id':request['request_id'],'actor':'assistant','pid':os.getpid(),'native_before':before,'native_identity':native_identity(),'mode':request['mode'],'fixture_reopen_count':0,'producer':request['producer'],'cases':{}}
    write_json(root/'startup.json',{'request_id':request['request_id'],'pid':os.getpid(),'documents':list(App.listDocuments()),'native':before,'home':App.getUserAppDataDir(),'scratch':App.getTempPath()})
    Gui.activateWorkbench('FemWorkbench')
    state={'case_index':0,'doc':None,'field_index':0}

    def finish(error=None):
        if error:report['error']=error
        try:
            if Gui.Control.activeDialog():Gui.Control.closeDialog()
            for name in list(App.listDocuments()):App.closeDocument(name)
            report['native_after']=loaded_module_receipt(request['native'],meshtools)
        except Exception:report['cleanup_error']=traceback.format_exc()
        write_json(root/'native.json',report)
        QtCore.QTimer.singleShot(0,QtCore.QCoreApplication.quit)

    def later(operation,delay=200):
        def guarded():
            try:operation()
            except Exception:finish(traceback.format_exc())
        QtCore.QTimer.singleShot(delay,guarded)

    def snapshot_objects(doc):
        return [{'name':obj.Name,'type_id':obj.TypeId,'proxy_module':type(getattr(obj.ViewObject,'Proxy',None)).__module__,'proxy_class':type(getattr(obj.ViewObject,'Proxy',None)).__name__} for obj in doc.Objects]

    def start_case():
        if state['case_index']==len(ids):finish();return
        key=ids[state['case_index']];folder=root/'cases'/key;folder.mkdir()
        has_results=key in ('axial_bar','cantilever','r1_mesh_4')
        options={}
        if key=='r1_mesh_4':
            from fea_r1_worker import case_hook
            options['evidence_hook']=case_hook
        if producer is None:
            raw=build_case(specs[key],request['config'],folder/'native',**options)
            path=folder/'native'/'analysis.FCStd'
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(path) as archive:xml=ET.fromstring(archive.read('Document.xml'))
            inventory=[{'name':v.get('name'),'type_id':v.get('type')} for v in xml.findall('./Objects/Object')]
        else:
            previous=producer['cases'][key]
            path=checked({'path':previous['source_path'],'sha256':previous['source_sha256']})
            producer_root=Path(request['producer']['path']).parent
            if path.resolve()!=(producer_root/'cases'/key/'native'/'analysis.FCStd').resolve():raise ValueError('fixture source outside producer')
            raw=previous['fresh_native'];inventory=previous['archive_audit']['object_inventory']
        # Only fixed, generated, hash-bound fixtures enter this acceptance-only
        # restore path. Product code still rebuilds from safe geometry/fields.
        audit=inspect_generated_archive(path,inventory)
        sha=digest(path);doc=App.openDocument(str(path));state['doc']=doc
        report['fixture_reopen_count']+=1
        FemGui.setActiveAnalysis(doc.getObject('Analysis'))
        mesh=doc.getObject('Mesh').FemMesh
        case={'case_id':key,'actor':'assistant','source_path':str(path),'source_sha256':sha,'archive_audit':audit,'fresh_native':raw,'objects':snapshot_objects(doc),'mesh_inventory':{'nodes':mesh.NodeCount,'elements':mesh.VolumeCount},'result_present':has_results,'result_dialog_open':False,'solver_dialog_open':False,'visible_result_mesh':False,'result_inventory':None,'display_fields':{}}
        report['cases'][key]=case;state.update(case=case,folder=folder,field_index=0)
        doc.recompute()
        case['recompute_errors']=[obj.Name for obj in doc.Objects if any(v in ('Invalid','Error') for v in obj.State)]
        if has_results:
            result=doc.getObject('CCX_Results');vectors=list(result.DisplacementVectors)
            case['result_inventory']=validate_result_fields(mesh.Nodes,list(result.NodeNumbers),[(v.x,v.y,v.z) for v in vectors],list(result.NodeStressXX),list(result.vonMises))
            for obj in doc.Objects:
                if obj.Name!='Analysis' and hasattr(obj.ViewObject,'Visibility'):obj.ViewObject.Visibility=False
            Gui.activeDocument().setEdit(result.Name)
            case['result_dialog_open']=bool(Gui.Control.activeDialog())
            case['visible_result_mesh']=bool(result.Mesh.ViewObject.Visibility)
            state['field_values']=[('displacement_x','rb_x_displacement','mm',[v.x for v in vectors]),('displacement_magnitude','rb_abs_displacement','mm',list(result.DisplacementLengths)),('von_mises','rb_vm_stress','MPa',list(result.vonMises))]
            Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll()
            later(select_field)
        else:later(open_solver)

    def select_field():
        if state['field_index']==len(state['field_values']):
            Gui.activeDocument().resetEdit();later(open_solver);return
        key,radio,unit,values=state['field_values'][state['field_index']]
        button=Gui.getMainWindow().findChild(QtGui.QRadioButton,radio)
        if button is None or not button.isEnabled():raise ValueError('native field unavailable '+radio)
        button.setChecked(True)
        later(capture_field,400)

    def capture_field():
        key,radio,unit,values=state['field_values'][state['field_index']]
        if not values or not all(math.isfinite(v) for v in values):raise ValueError('invalid native scalar field')
        record={'radio_button':radio,'unit':unit,'raw_min':min(values),'raw_max':max(values),'nodal_values_sha256':hashlib.sha256(json.dumps(values,allow_nan=False).encode()).hexdigest()}
        for name in ('le_min','le_max'):
            widget=Gui.getMainWindow().findChild(QtGui.QWidget,name)
            quantity=widget.property('quantity')
            record[name]={'text':str(widget.property('text')),'normalized_value':float(quantity.getValueAs(unit).Value)}
        path=state['folder']/(key+'-native.png')
        Gui.activeDocument().activeView().saveImage(str(path),1200,800,'Black')
        record['image_path']=str(path);record['image_sha256']=digest(path)
        panel=Gui.getMainWindow().findChild(QtGui.QWidget,'ShowDisplacement')
        if panel:
            panel_path=state['folder']/(key+'-panel.png');panel.grab().save(str(panel_path));record['panel_path']=str(panel_path);record['panel_sha256']=digest(panel_path)
        state['case']['display_fields'][key]=record
        state['field_index']+=1;later(select_field)

    def open_solver():
        doc=state['doc'];output=state['folder']/'gui-export';output.mkdir()
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/General').SetBool('OverwriteSolverWorkingDirectory',False)
        doc.getObject('CalculiX').WorkingDir=str(output)
        Gui.activeDocument().setEdit('CalculiX')
        state['case']['solver_dialog_open']=bool(Gui.Control.activeDialog())
        later(write_input)

    def write_input():
        field=Gui.getMainWindow().findChild(QtGui.QLineEdit,'le_working_dir')
        output=state['folder']/'gui-export'
        if field is None or not os.path.samefile(field.text(),output):raise ValueError('native solver panel working directory mismatch')
        button=Gui.getMainWindow().findChild(QtGui.QPushButton,'pb_write_inp')
        if button is None or not button.isEnabled():raise ValueError('native Write input button unavailable')
        state['case']['working_directory_from_panel']=field.text()
        button.click()  # actual standard task-panel signal, not direct writer invocation
        later(end_case)

    def end_case():
        case=state['case'];path=Path(case['source_path']);output=state['folder']/'gui-export'/'Mesh.inp'
        if not output.is_file():raise ValueError('native button did not create Mesh.inp')
        case['exported_input']=str(output);case['exported_input_sha256']=digest(output)
        case['source_unchanged']=digest(path)==case['source_sha256']
        Gui.activeDocument().resetEdit();App.closeDocument(state['doc'].Name);state['doc']=None
        case['closed_without_saving']=True
        state['case_index']+=1;later(start_case)

    later(start_case,500)
