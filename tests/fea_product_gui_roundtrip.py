"""Acceptance-only restore of this test's exact generated axial fixture.

This exception is not imported by the product and never accepts a customer
document. The product always rebuilds from BREP, UNV and validated fields.
"""
import hashlib
import json
import os
from pathlib import Path
import traceback


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(request_path):
    import FreeCAD as App
    import FreeCADGui as Gui
    import FemGui
    from PySide import QtCore,QtGui
    from analysis_gui_checks import inspect_native_archive
    from analysis_gui_controls import visible_child,named_ancestor
    from analysis_gui_pixels import inspect_native_png
    from analysis_gui_rebuild import field_values
    root=Path(request_path).parent;request=json.loads(Path(request_path).read_text())
    report={'status':'failed'};state={}

    def finish(error=None):
        if error:report['error']=error
        if Gui.Control.activeDialog():Gui.Control.closeDialog()
        for name in list(App.listDocuments()):App.closeDocument(name)
        (root/'roundtrip.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        QtCore.QTimer.singleShot(0,QtCore.QCoreApplication.quit)

    def later(operation,delay=400):
        def guarded():
            try:operation()
            except Exception:finish(traceback.format_exc())
        QtCore.QTimer.singleShot(delay,guarded)

    def start():
        if request['case_id']!='synthetic-product-axial' or App.Version()[:3]!=['1','1','3'] or list(App.listDocuments()) or not os.path.samefile(App.getTempPath(),request['scratch']):raise ValueError('fixture-only isolated GUI required')
        path=root/'analysis.FCStd'
        if digest(path)!=request['fcstd_sha256']:raise ValueError('generated fixture SHA changed')
        # Exact native null Proxy identities and inventory, checked BEFORE open.
        inspect_native_archive(path,request['inventory'])
        Gui.activateWorkbench('FemWorkbench');doc=App.openDocument(str(path));state['doc']=doc
        FemGui.setActiveAnalysis(doc.getObject('Analysis'));result=doc.getObject('CCX_Results');state['result']=result
        if not result.Mesh.ViewObject.Visibility:raise ValueError('saved cloud mesh is hidden after restore')
        actual=field_values(result)
        if hashlib.sha256(json.dumps(actual,sort_keys=True,allow_nan=False).encode()).hexdigest()!=request['field_values_sha256']:raise ValueError('saved native fields changed')
        Gui.activeDocument().setEdit(result.Name)
        if not Gui.Control.activeDialog():raise ValueError('standard result task cannot open after restore')
        later(select_field)

    def select_field():
        button=visible_child(Gui.getMainWindow(),QtGui.QRadioButton,'rb_vm_stress');button.setChecked(True)
        panel=named_ancestor(button,'ShowDisplacement');visible_child(panel,QtGui.QCheckBox,'cb_show_displacement').setChecked(False)
        Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll();Gui.updateGui();state['captures']=0
        later(capture,600)

    def capture():
        image=root/'restored-native.png';Gui.activeDocument().activeView().saveImage(str(image),1200,800,'Black')
        pixels=inspect_native_png(image)
        if pixels['status']!='passed':
            state['captures']+=1;image.rename(root/('restore-failed-'+str(state['captures'])+'.png'))
            if state['captures']>=3:raise ValueError('saved fixture cloud is blank after restore')
            Gui.activeDocument().activeView().redraw();Gui.updateGui();Gui.activeDocument().activeView().fitAll();later(capture,600);return
        report['viewport_pixels']=pixels
        Gui.activeDocument().resetEdit()
        if Gui.Control.activeDialog():Gui.Control.closeDialog()
        later(open_solver)

    def open_solver():
        directory=root/'export';directory.mkdir()
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/General').SetBool('OverwriteSolverWorkingDirectory',False)
        state['doc'].getObject('CalculiX').WorkingDir=str(directory);Gui.activeDocument().setEdit('CalculiX');later(write_input)

    def write_input():
        field=visible_child(Gui.getMainWindow(),QtGui.QLineEdit,'le_working_dir')
        if not os.path.samefile(field.text(),root/'export'):raise ValueError('restored solver directory mismatch')
        visible_child(Gui.getMainWindow(),QtGui.QPushButton,'pb_write_inp').click();later(done)

    def done():
        if not (root/'export/Mesh.inp').is_file() or digest(root/'analysis.FCStd')!=request['fcstd_sha256']:raise ValueError('restored export missing or fixture modified')
        report.update(status='passed',field_values_sha256=request['field_values_sha256'],source_unchanged=True,native_write_button=True,pid=os.getpid());finish()

    later(start,500)


def verify_roundtrip_fixture(native,directory,deck,settings):
    """Fresh native process without an installed product FreeCAD add-on."""
    import shutil
    import time
    import uuid
    from mechanical_design_agent.analysis_gui import _configuration
    from mechanical_design_agent.analysis_native import _environment,_native_path
    from mechanical_design_agent.analysis_process import ProcessRequest
    from mechanical_design_agent.analysis_deck import inspect_native_deck,compare_deck_semantics
    root=native.owned.path/uuid.uuid4().hex;root.mkdir();home=root/'home';home.mkdir();_configuration(home,settings)
    produced=json.loads((directory/'response.json').read_text(encoding='utf-8'))
    shutil.copyfile(directory/'analysis.FCStd',root/'analysis.FCStd')
    sources=['analysis_gui_checks.py','analysis_gui_controls.py','analysis_gui_pixels.py','analysis_gui_rebuild.py','analysis_extract_results.py']
    for name in sources:shutil.copyfile(native.resources/name,root/name)
    shutil.copyfile(__file__,root/'fea_product_gui_roundtrip.py')
    request={'case_id':'synthetic-product-axial','fcstd_sha256':digest(root/'analysis.FCStd'),'inventory':[{'name':item['name'],'type_id':item['type_id']} for item in produced['objects']],'field_values_sha256':produced['field_values_sha256'],'scratch':_native_path(native.owned.path)}
    (root/'request.json').write_text(json.dumps(request),encoding='utf-8')
    statement='import sys;sys.dont_write_bytecode=True;sys.path.insert(0,'+ascii(_native_path(root))+');import fea_product_gui_roundtrip;fea_product_gui_roundtrip.run('+ascii(_native_path(root/'request.json'))+')\n'
    (root/'RestoreFixture.FCMacro').write_text(statement,encoding='ascii')
    env=_environment(home,native.owned.path);env.pop('QT_QPA_PLATFORM',None);env['QT_OPENGL']='desktop'
    identity=native.controller.start(ProcessRequest(argv=(_native_path(settings['tools']['freecadgui']['path']),'-u',_native_path(home/'user.cfg'),'-s',_native_path(home/'system.cfg'),_native_path(root/'RestoreFixture.FCMacro')),cwd=root,stdout_path=root/'stdout.log',stderr_path=root/'stderr.log',env=env,timeout_seconds=120))
    try:
        while (outcome:=native.controller.poll(identity)) is None:time.sleep(.1)
    finally:native.controller.terminate(identity,reason='interrupted')
    evidence=directory.parent/'GUI independent restore 中文';shutil.copytree(root,evidence)
    assert outcome.reason=='exited' and outcome.returncode==0
    report=json.loads((root/'roundtrip.json').read_text());assert report['status']=='passed',report
    assert compare_deck_semantics(deck['inspection'],inspect_native_deck(root/'export/Mesh.inp',allowed_root=root/'export'))['equivalent']
    return report
