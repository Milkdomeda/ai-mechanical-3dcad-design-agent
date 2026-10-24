"""Native synthetic geometry only; never reads a customer model."""
import json
from pathlib import Path
import sys

import FreeCAD as App
import Part


def main(directory):
    directory = Path(directory)
    for kind in ('box', 'cylinder', 'placed'):
        doc = App.newDocument('Fixture_' + kind)
        obj = doc.addObject('Part::Feature', 'Solid')
        obj.Shape = Part.makeCylinder(5, 100, App.Vector(0, 5, 5), App.Vector(1, 0, 0)) if kind == 'cylinder' else Part.makeBox(100, 10, 10)
        if kind == 'placed':
            parent = doc.addObject('App::Part', 'Parent')
            parent.Placement.Base = App.Vector(100, 0, 0)
            parent.addObject(obj)
            obj.Placement = App.Placement(App.Vector(10, 20, 30), App.Rotation(App.Vector(0, 0, 1), 90))
        for index in range(1, 5):
            reference = doc.addObject('Part::Feature', 'Ball' + str(index))
            reference.Shape = Part.makeSphere(2, App.Vector(index * 10, 20, 20))
        doc.recompute()
        doc.saveAs(str(directory / (kind + '.FCStd')))
        App.closeDocument(doc.Name)
    (directory / 'fixture-created.json').write_text(json.dumps({'version': App.Version()[:3], 'files': ['box.FCStd', 'cylinder.FCStd', 'placed.FCStd']}), encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1])
