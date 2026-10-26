"""Fixed geometry/region/ROI preview on an isolated GUI main thread."""
import hashlib
import json
import os
from pathlib import Path
import traceback


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(request_path):
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from PySide import QtCore,QtGui
    from analysis_gui_pixels import inspect_native_png
    path=Path(request_path);root=path.parent;request=json.loads(path.read_text(encoding='utf-8'))
    report={'schema_version':'AnalysisNativePreview/v1','request_id':request.get('request_id'),'request_sha256':digest(path),'status':'failed','artifacts':{},'source_fcstd_open_count':0}
    state={'index':0}

    def finish(error=None):
        if error:report['error']=error
        for name in list(App.listDocuments()):App.closeDocument(name)
        (root/'response.json').write_text(json.dumps(report,allow_nan=False,indent=2),encoding='utf-8')
        QtCore.QTimer.singleShot(0,QtCore.QCoreApplication.quit)

    def later(operation,delay=400):
        def guarded():
            try:operation()
            except Exception:finish(traceback.format_exc())
        QtCore.QTimer.singleShot(delay,guarded)

    def start():
        if set(request)!={'schema_version','request_id','expected_temp','home','inputs','identity'} or request['schema_version']!='AnalysisNativePreviewRequest/v1' or set(request['inputs'])!={'geometry.brep','spec.json','geometry.json','regions.json','ledger.json'}:raise ValueError('PREVIEW_REQUEST_INVALID')
        if not App.GuiUp or list(App.listDocuments()) or App.Version()[:3]!=['1','1','3'] or not os.path.samefile(App.getTempPath(),request['expected_temp']) or not os.path.samefile(App.getUserAppDataDir(),request['home']):raise ValueError('PREVIEW_ISOLATED_GUI_REQUIRED')
        if any(digest(root/name)!=sha for name,sha in request['inputs'].items()):raise ValueError('PREVIEW_INPUT_CHANGED')
        for name in ('spec','geometry','regions','ledger'):state[name]=json.loads((root/(name+'.json')).read_text(encoding='utf-8'))
        shape=Part.Shape();shape.read(str(root/'geometry.brep'))
        if not shape.isValid() or not shape.isClosed() or len(shape.Solids)!=1:raise ValueError('PREVIEW_VALID_SINGLE_SOLID_REQUIRED')
        if digest(root/'geometry.brep')!=state['spec']['source']['geometry_sha256'] or state['regions']['bindings_sha256']!=state['spec']['source']['regions_sha256']:raise ValueError('PREVIEW_GEOMETRY_BINDING_CHANGED')
        doc=App.newDocument('FEA_'+request['request_id']);solid=doc.addObject('Part::Feature','AnalysisSolid');solid.Shape=shape
        state.update(doc=doc,solid=solid,helpers=[])
        report.update(identity=request['identity'],visible_region_ids=sorted(item['region_id'] for item in state['regions']['bindings']),visible_observable_ids=sorted(item['id'] for item in state['spec']['evaluation']['observables']),cases=[])
        later(select_case)

    def select_case():
        index=state['index'];spec=state['spec']
        if index==len(spec['cases']):
            if any(digest(root/name)!=sha for name,sha in request['inputs'].items()):raise ValueError('PREVIEW_INPUT_CHANGED')
            report['status']='passed';finish();return
        doc=state['doc'];solid=state['solid'];shape=solid.Shape
        for name in state['helpers']:doc.removeObject(name)
        state['helpers']=[]
        case=spec['cases'][index];ledger=next(item for item in state['ledger']['cases'] if item['case_id']==case['case_id'])
        regions={item['region_id']:item for item in state['regions']['bindings']}
        colors=[(.25,.65,.75)]*len(shape.Faces)
        for name in case['fixed_regions']:
            for face in regions[name]['face_indices']:colors[face-1]=(.1,1.,.1)
        for load in ledger['native_objects']:
            if load['type']!='gravity':
                for face in load['face_indices']:colors[face-1]=(1.,.15,.1)
            vector=list(map(float,load.get('components_N',load.get('resultant_N',load.get('components_mm_s2',[])))))
            if len(vector)!=3:raise ValueError('PREVIEW_LOAD_DIRECTION_MISSING')
            direction=App.Vector(*vector)
            if direction.Length<=0:raise ValueError('PREVIEW_LOAD_DIRECTION_ZERO')
            direction.normalize();origin=shape.CenterOfMass
            if load['type']!='gravity':
                selected=[shape.Faces[face-1] for face in load['face_indices']]
                origin=sum((face.CenterOfMass*face.Area for face in selected),App.Vector())/sum(face.Area for face in selected)
            length=float(spec['evaluation']['reference_length']['value'])*.12
            arrow=doc.addObject('Part::Feature','LoadDirection');arrow.Shape=Part.makeLine(origin,origin+direction*length)
            arrow.ViewObject.LineColor=(1.,.2,.1);arrow.ViewObject.LineWidth=5;state['helpers'].append(arrow.Name)
            cone=doc.addObject('Part::Feature','LoadArrow');cone.Shape=Part.makeCone(length*.06,0,length*.2,origin+direction*(length*.8),direction)
            cone.ViewObject.ShapeColor=(1.,.2,.1);state['helpers'].append(cone.Name)
        for observation in spec['evaluation']['observables']:
            if observation['case_id']!=case['case_id'] or observation['roi'] is None:continue
            roi=observation['roi'];lo,hi=([float(q['value']) for q in roi[key]] for key in ('minimum','maximum'))
            obj=doc.addObject('Part::Feature','ObservationROI');obj.Shape=Part.makeBox(*(b-a for a,b in zip(lo,hi)),App.Vector(*lo));obj.ViewObject.DisplayMode='Wireframe';obj.ViewObject.LineColor=(1.,.1,1.);obj.ViewObject.LineWidth=3;state['helpers'].append(obj.Name)
        solid.ViewObject.DiffuseColor=colors;solid.ViewObject.Transparency=30
        doc.recompute();Gui.activeDocument().activeView().viewAxonometric();Gui.activeDocument().activeView().fitAll();Gui.updateGui()
        state['case']=case;state['capture_attempt']=0;later(capture,600)

    def capture():
        case=state['case'];name='case-'+str(state['index']).zfill(3)+'.png';native=root/('native-'+name)
        Gui.activeDocument().activeView().saveImage(str(native),1200,800,'Black')
        pixels=inspect_native_png(native)
        if pixels['status']!='passed':
            state['capture_attempt']+=1;native.rename(root/('failed-'+str(state['capture_attempt'])+'-'+name))
            if state['capture_attempt']>=3:raise ValueError('PREVIEW_BLANK_OR_CLIPPED')
            Gui.activeDocument().activeView().redraw();Gui.updateGui();Gui.activeDocument().activeView().fitAll();later(capture,600);return
        image=QtGui.QImage(str(native));canvas=QtGui.QImage(1600,1080,QtGui.QImage.Format_RGB32);canvas.fill(QtGui.QColor('white'));p=QtGui.QPainter(canvas)
        try:
            p.setPen(QtGui.QColor('black'));p.setFont(QtGui.QFont('Sans Serif',16));p.drawText(24,36,'Analysis preview | '+case['case_id'])
            p.setFont(QtGui.QFont('Sans Serif',11));p.drawText(24,72,'Green: fixed region | Red: load region and direction | Magenta: declared stress ROI | arrow length is illustrative')
            p.drawImage(QtCore.QRect(20,100,1200,800),image)
            material=state['spec']['material'];p.drawText(24,940,'E = '+material['youngs_modulus']['value']+' MPa | nu = '+material['poisson_ratio']['value']+' | Source '+request['identity']['source_sha256'][:16])
            p.drawText(24,978,'Fixed: '+', '.join(case['fixed_regions']))
            ledger=next(item for item in state['ledger']['cases'] if item['case_id']==case['case_id'])
            p.drawText(24,1016,'Total force [N]: '+str(ledger['total_force_N'])+' | Total moment [N mm]: '+str(ledger['total_moment_Nmm']))
            p.drawText(24,1054,'Full material, exclusions, mass basis, factors and assumptions are in the exact hash-bound review summary.')
        finally:p.end()
        if not canvas.save(str(root/name),'PNG'):raise ValueError('PREVIEW_IMAGE_SAVE_FAILED')
        report['artifacts'][name]={'relative_path':name,'sha256':digest(root/name)}
        report['cases'].append({'case_id':case['case_id'],'fixed_regions':case['fixed_regions'],'load_regions':[item['region_id'] for item in state['ledger']['cases'][state['index']]['native_objects'] if item['type']!='gravity'],'viewport_pixels':pixels})
        state['index']+=1;later(select_case)

    later(start,500)
