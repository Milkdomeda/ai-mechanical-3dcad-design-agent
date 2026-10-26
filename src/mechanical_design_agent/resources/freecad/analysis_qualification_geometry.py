"""Generate only fixed synthetic software fixtures; no input CAD or caller code."""
import json
from pathlib import Path
import sys


def main(directory):
    import FreeCAD as App
    import Part
    root=Path(directory)
    if App.Version()[:3]!=['1','1','3']:raise ValueError('QUALIFICATION_NATIVE_VERSION')
    for name in ('box','cylinder'):
        doc=App.newDocument('SoftwareQualification_'+name)
        obj=doc.addObject('Part::Feature','Solid')
        obj.Shape=Part.makeBox(100,10,10) if name=='box' else Part.makeCylinder(5,100,App.Vector(0,5,5),App.Vector(1,0,0))
        if not obj.Shape.isValid() or len(obj.Shape.Solids)!=1:raise ValueError('QUALIFICATION_SHAPE_INVALID')
        doc.recompute();doc.saveAs(str(root/(name+'.FCStd')));App.closeDocument(doc.Name)
    (root/'created.json').write_text(json.dumps({'version':App.Version()[:3],'files':['box.FCStd','cylinder.FCStd']}),encoding='utf-8')


if __name__=='__main__':main(sys.argv[1])
