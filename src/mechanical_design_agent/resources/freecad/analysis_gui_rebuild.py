"""Fixed GUI-main-thread reconstruction from BREP, UNV and validated fields.

Never opens a persistent FCStd, executes caller code or runs a solver.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import traceback


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def field_values(result):
    from analysis_extract_results import values
    return {'node_ids':list(result.NodeNumbers),'displacement':[[v.x,v.y,v.z] for v in result.DisplacementVectors],
            'stress':list(map(list,zip(*(values(result,name,'MPa') for name in ('NodeStressXX','NodeStressYY','NodeStressZZ','NodeStressXY','NodeStressXZ','NodeStressYZ'))))),
            'von_mises':values(result,'vonMises','MPa'),
            'principal_stress':list(map(list,zip(*(values(result,name,'MPa') for name in ('PrincipalMax','PrincipalMed','PrincipalMin')))))}


def run(request_path,*,interactive=False):
    import FreeCAD as App
    import FreeCADGui as Gui
    import FemGui
    from PySide import QtCore,QtGui
    from analysis_build import build_objects
    from analysis_gui_controls import visible_child,named_ancestor
    from analysis_gui_pixels import inspect_native_png
    from analysis_gui_plot import frame_native_image
    from analysis_runtime_identity import loaded
    from femmesh import meshtools
    from femsolver.calculix import write_step_output
    from feminout.importCcxFrdResults import importFrd
    path=Path(request_path);root=path.parent
    request=json.loads(path.read_text(encoding='utf-8'))
    report={'schema_version':'AnalysisNativeGui/v1','request_id':request.get('request_id'),'request_sha256':digest(path),'pid':os.getpid(),'status':'failed','artifacts':{},'display_fields':{}}
    state={}

    def put():
        (root/'response.json').write_text(json.dumps(report,allow_nan=False,indent=2),encoding='utf-8')

    def finish(error=None):
        if interactive and error is None:
            later(open_interactive);return
        if error:report['error']=error
        try:
            if Gui.Control.activeDialog():Gui.Control.closeDialog()
            for name in list(App.listDocuments()):App.closeDocument(name)
            report['modules_after']=modules()
        except Exception:report['cleanup_error']=traceback.format_exc();report['status']='failed'
        put();QtCore.QTimer.singleShot(0,QtCore.QCoreApplication.quit)

    def open_interactive():
        state['doc'].Label='FEA interactive '+request['identity']['case_id']+' '+request['request_id'][:8]
        Gui.activeDocument().setEdit(state['result'].Name)
        later(select_interactive,500)

    def select_interactive():
        if not Gui.Control.activeDialog() or not state['result'].Mesh.ViewObject.Visibility:raise ValueError('GUI_INTERACTIVE_RESULT_TASK_MISSING')
        visible_child(Gui.getMainWindow(),QtGui.QRadioButton,'rb_vm_stress').setChecked(True)
        state['result'].ViewObject.Visibility=True;state['result'].Mesh.ViewObject.Visibility=True
        Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll();Gui.activeDocument().activeView().redraw();Gui.updateGui()
        later(interactive_ready,500)

    def interactive_ready():
        native=root/'interactive-native.png'
        Gui.activeDocument().activeView().saveImage(str(native),1200,800,'Black')
        if inspect_native_png(native)['status']!='passed':raise ValueError('GUI_INTERACTIVE_NATIVE_VIEWPORT_BLANK_OR_CLIPPED')
        raw=report['display_fields']['von_mises'];frame_native_image(native,root/'interactive-figure.png',case_id=request['identity']['case_id'],field='von_mises',unit='MPa',minimum=raw['raw_min'],maximum=raw['raw_max'])
        name='interactive-window.png'
        if not Gui.getMainWindow().grab().save(str(root/name),'PNG'):raise ValueError('GUI_INTERACTIVE_CAPTURE_FAILED')
        report.update(interactive_ready=True,modules_after=modules(),interactive_document_path=str(root/'analysis.FCStd'),interactive_window={'relative_path':name,'sha256':digest(root/name)},interactive_images=[{'relative_path':name,'sha256':digest(root/name)} for name in ('interactive-native.png','interactive-figure.png')])
        put()

    def later(operation,delay=300):
        def guarded():
            try:operation()
            except Exception:finish(traceback.format_exc())
        QtCore.QTimer.singleShot(delay,guarded)

    def modules():
        return {'faceforce':loaded(meshtools,request['modules']['faceforce'],['get_ref_facenodes_areas','get_force_obj_face_nodeload_table','t6_weights']),
                'output':loaded(write_step_output,request['modules']['output'],['write_step_output'])}

    def artifact(name):
        report['artifacts'][name]={'relative_path':name,'sha256':digest(root/name)}

    def start():
        if set(request)!={'schema_version','request_id','expected_temp','home','inputs','modules','identity'} or request['schema_version']!='AnalysisNativeGuiRequest/v1' or set(request['inputs'])!={'geometry.brep','mesh.unv','Mesh.frd','fields.json','deck.json'}:
            raise ValueError('GUI_REQUEST_INVALID')
        if not App.GuiUp or list(App.listDocuments()) or App.Version()[:3]!=['1','1','3'] or not os.path.samefile(App.getTempPath(),request['expected_temp']) or not os.path.samefile(App.getUserAppDataDir(),request['home']):raise ValueError('GUI_FRESH_ISOLATED_PROCESS_REQUIRED')
        for name,sha in request['inputs'].items():
            if digest(root/name)!=sha:raise ValueError('GUI_INPUT_CHANGED')
        report['modules_before']=modules();report['identity']=request['identity']
        fields=json.loads((root/'fields.json').read_text(encoding='utf-8'))
        deck=json.loads((root/'deck.json').read_text(encoding='utf-8'))
        if fields['spec_sha256']!=request['identity']['spec_sha256'] or fields['case_id']!=request['identity']['case_id'] or fields['source_input_sha256']!=request['identity']['input_sha256'] or fields['fields']['mesh_sha256']!=request['identity']['mesh_sha256'] or fields['fields']['validity']!='passed':raise ValueError('GUI_FIELDS_BINDING_INVALID')
        Gui.activateWorkbench('FemWorkbench')
        paths={name:root/name for name in ('geometry.brep','mesh.unv')}
        doc,analysis,solver,mesh=build_objects(deck['contract'],paths,document_name='FEA_'+request['request_id'])
        FemGui.setActiveAnalysis(analysis)
        result=importFrd(str(root/'Mesh.frd'),analysis=analysis,result_name_prefix='CCX_',result_analysis_type='static')
        if result is None:raise ValueError('GUI_NATIVE_RESULT_MISSING')
        actual=field_values(result);expected=fields['fields']
        if actual['node_ids']!=expected['node_ids'] or any(actual[key]!=expected[key]['values'] for key in ('displacement','stress','von_mises','principal_stress')):raise ValueError('GUI_NATIVE_FIELDS_CHANGED')
        report['field_values_sha256']=hashlib.sha256(json.dumps(actual,sort_keys=True,allow_nan=False).encode()).hexdigest()
        report['mesh_inventory']={'nodes':mesh.FemMesh.NodeCount,'elements':mesh.FemMesh.VolumeCount}
        report['objects']=[{'name':obj.Name,'type_id':obj.TypeId,'proxy_module':type(getattr(obj.ViewObject,'Proxy',None)).__module__,'proxy_class':type(getattr(obj.ViewObject,'Proxy',None)).__name__} for obj in doc.Objects]
        state.update(doc=doc,result=result,solver=solver,mesh=mesh,index=0,fields=[('displacement_x','rb_x_displacement','mm',[v[0] for v in actual['displacement']]),('displacement_magnitude','rb_abs_displacement','mm',list(result.DisplacementLengths)),('von_mises','rb_vm_stress','MPa',actual['von_mises'])])
        for obj in doc.Objects:
            if obj.Name!='Analysis' and hasattr(obj.ViewObject,'Visibility'):obj.ViewObject.Visibility=False
        mesh.ViewObject.Visibility=True
        Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll();Gui.updateGui()
        state['capture_attempt']=0;later(capture_mesh,500)

    def capture_mesh():
        name='mesh-native.png';Gui.activeDocument().activeView().saveImage(str(root/name),1200,800,'Black')
        from analysis_gui_pixels import inspect_rgb_pixels
        image=QtGui.QImage(str(root/name))
        if image.isNull():raise ValueError('GUI_NATIVE_MESH_IMAGE_MISSING')
        # Mesh display can be gray. Test foreground extent, without changing
        # the stricter saturated-color cloud rule or altering image pixels.
        pixels=inspect_rgb_pixels(image.width(),image.height(),lambda x,y:(max(QtGui.QColor(image.pixel(x,y)).getRgb()[:3]),0,0))
        pixels['policy']='native mesh brightness foreground >=1 percent, fully framed; no field-color assertion'
        if pixels['status']!='passed':
            state['capture_attempt']+=1;(root/name).rename(root/('mesh-failed-'+str(state['capture_attempt'])+'.png'))
            if state['capture_attempt']>=3:raise ValueError('GUI_NATIVE_MESH_BLANK_OR_CLIPPED')
            Gui.activeDocument().activeView().redraw();Gui.updateGui();Gui.activeDocument().activeView().fitAll();later(capture_mesh,500);return
        artifact(name);report['mesh_viewport_pixels']=pixels
        for obj in state['doc'].Objects:
            if obj.Name!='Analysis' and hasattr(obj.ViewObject,'Visibility'):obj.ViewObject.Visibility=False
        result=state['result']
        Gui.activeDocument().setEdit(result.Name)
        if not Gui.Control.activeDialog() or not result.Mesh.ViewObject.Visibility:raise ValueError('GUI_RESULT_TASK_OR_VIEW_MISSING')
        Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll()
        later(select)

    def select():
        if state['index']==len(state['fields']):
            Gui.activeDocument().resetEdit()
            if Gui.Control.activeDialog():Gui.Control.closeDialog()
            later(open_solver,400);return
        key,radio,unit,values=state['fields'][state['index']]
        button=visible_child(Gui.getMainWindow(),QtGui.QRadioButton,radio)
        panel=named_ancestor(button,'ShowDisplacement');button.setChecked(True)
        visible_child(panel,QtGui.QCheckBox,'cb_show_displacement').setChecked(False)
        state.update(panel=panel,capture_attempt=0)
        Gui.activeDocument().activeView().redraw();Gui.updateGui();Gui.activeDocument().activeView().fitAll()
        later(capture,500)

    def capture():
        key,radio,unit,values=state['fields'][state['index']]
        if not values or not all(math.isfinite(value) for value in values):raise ValueError('GUI_FIELD_NONFINITE_OR_EMPTY')
        from PySide6.QtOpenGLWidgets import QOpenGLWidget
        contexts=[]
        for widget in Gui.getMainWindow().findChildren(QOpenGLWidget):
            if not widget.isVisible():continue
            context=widget.context()
            if context is None or not context.isValid():raise ValueError('GUI_OPENGL_CONTEXT_INVALID')
            version=context.format();contexts.append({'valid':True,'version':[version.majorVersion(),version.minorVersion()],'is_opengl_es':context.isOpenGLES()})
        if not contexts or any(item['version'][0]<2 for item in contexts):raise ValueError('GUI_OPENGL_2_REQUIRED')
        report['graphics_contexts']=contexts
        native=root/(key+'-native.png');Gui.activeDocument().activeView().saveImage(str(native),1200,800,'Black')
        pixels=inspect_native_png(native)
        if pixels['status']!='passed':
            state['capture_attempt']+=1;native.rename(root/(key+'-failed-'+str(state['capture_attempt'])+'.png'))
            if state['capture_attempt']>=3:raise ValueError('GUI_NATIVE_VIEWPORT_BLANK_OR_CLIPPED')
            Gui.activeDocument().activeView().redraw();Gui.updateGui();Gui.activeDocument().activeView().fitAll();later(capture,500);return
        record={'unit':unit,'raw_min':min(values),'raw_max':max(values),'radio_button':radio,'viewport_pixels':pixels,'nodal_values_sha256':hashlib.sha256(json.dumps(values,allow_nan=False).encode()).hexdigest()}
        import re
        for name in ('le_min','le_max'):
            widget=visible_child(state['panel'],QtGui.QWidget,name);quantity=widget.property('quantity');text=str(widget.property('text'))
            token=re.match(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',text)
            if token is None:raise ValueError('GUI_QUANTITY_FORMAT_UNKNOWN')
            mantissa,_,exponent=token.group().lower().partition('e');power=(int(exponent) if exponent else 0)-len(mantissa.partition('.')[2])
            quantum=App.Units.Quantity('1e'+str(power)+' '+text[token.end():].strip())
            record[name]={'text':text,'normalized_value':float(quantity.getValueAs(unit).Value),'display_quantum':float(quantum.getValueAs(unit).Value)}
        figure=key+'-figure.png';record.update(frame_native_image(native,root/figure,case_id=request['identity']['case_id'],field=key,unit=unit,minimum=min(values),maximum=max(values)))
        panel=key+'-panel.png'
        if not state['panel'].grab().save(str(root/panel),'PNG'):raise ValueError('GUI_PANEL_CAPTURE_FAILED')
        for name in (native.name,figure,panel):artifact(name)
        record.update(native_image=native.name,figure=figure,panel=panel,step=expected_step(),stress_recovery='native_extrapolated_averaged_nodal')
        report['display_fields'][key]=record;state['index']+=1;later(select)

    def expected_step():return {'number':1,'increment':1,'time':1.}

    def open_solver():
        output=root/'export';output.mkdir()
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/General').SetBool('OverwriteSolverWorkingDirectory',False)
        state['solver'].WorkingDir=str(output)
        Gui.activeDocument().setEdit(state['solver'].Name)
        if not Gui.Control.activeDialog():raise ValueError('GUI_SOLVER_TASK_MISSING')
        later(write_input)

    def write_input():
        directory=visible_child(Gui.getMainWindow(),QtGui.QLineEdit,'le_working_dir')
        if not os.path.samefile(directory.text(),root/'export'):raise ValueError('GUI_SOLVER_DIRECTORY_CHANGED')
        visible_child(Gui.getMainWindow(),QtGui.QPushButton,'pb_write_inp').click()
        report['native_write_button']=True;later(save)

    def save():
        inp=root/'export'/'Mesh.inp'
        if not inp.is_file():raise ValueError('GUI_NATIVE_EXPORT_MISSING')
        (root/'native-export.inp').write_bytes(inp.read_bytes());artifact('native-export.inp')
        Gui.activeDocument().resetEdit()
        if Gui.Control.activeDialog():Gui.Control.closeDialog()
        # No transient W1 directory is embedded as a reusable solver location.
        state['solver'].WorkingDir=''
        for obj in state['doc'].Objects:
            if obj.Name!='Analysis' and hasattr(obj.ViewObject,'Visibility'):obj.ViewObject.Visibility=False
        # Native VPResultMechanical propagates its own Visibility to its
        # claimed mesh child on restore. Persist both parent and child as shown.
        state['result'].ViewObject.Visibility=True
        state['result'].Mesh.ViewObject.Visibility=True
        report['saved_result_visible']=bool(state['result'].ViewObject.Visibility)
        report['saved_result_mesh_visible']=bool(state['result'].Mesh.ViewObject.Visibility)
        state['doc'].recompute()
        if any(any(value in ('Invalid','Error','Recompute') for value in obj.State) for obj in state['doc'].Objects):raise ValueError('GUI_RECOMPUTE_FAILED')
        state['doc'].saveAs(str(root/'analysis.FCStd'));artifact('analysis.FCStd')
        if any(digest(root/name)!=sha for name,sha in request['inputs'].items()):raise ValueError('GUI_INPUT_CHANGED')
        report.update(status='passed',result_task=True,solver_task=True,source_fcstd_open_count=0,step=expected_step(),deformation_scale=0)
        finish()

    later(start,500)
