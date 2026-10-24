"""Fixed FreeCAD 1.1.3 geometry worker, compatible with embedded Python 3.11.

No host package imports, user code, FEM Proxy restore, or network operations.
Only a host-audited, hash-pinned source is opened. Outputs contain plain geometry.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quantity(value, unit):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('GEOMETRY_NONFINITE')
    return {'value': format(value, '.17g'), 'unit': unit}


def vector(value, unit):
    return [quantity(item, unit) for item in value]


def inspect(contract, source, directory):
    import FreeCAD as App
    import Part
    if set(contract) != {'schema_version', 'source_sha256', 'included_object', 'excluded_objects', 'partitions', 'volume_tolerance'} or contract['schema_version'] != 'AnalysisGeometryRequest/v1':
        raise ValueError('GEOMETRY_REQUEST_INVALID')
    if digest(source) != contract['source_sha256']:
        raise ValueError('ANALYSIS_SOURCE_CHANGED')
    source_doc = App.openDocument(str(source))
    derived_doc = None
    try:
        obj = source_doc.getObject(contract['included_object'])
        if obj is None or not hasattr(obj, 'Shape'):
            raise ValueError('GEOMETRY_OBJECT_MISSING')
        excluded = contract['excluded_objects']
        if obj.Name in excluded or any(source_doc.getObject(name) is None for name in excluded):
            raise ValueError('GEOMETRY_EXCLUSION_INVALID')
        construction = {item.Name for item in obj.OutListRecursive if hasattr(item, 'Shape') and item.Name not in excluded}
        # App::Part exposes a derived compound of its children. Native origins,
        # planes/axes/points are coordinate scaffolding, not extra workpieces.
        # Every actual solid-bearing leaf remains individually accounted for.
        for item in source_doc.Objects:
            if item.TypeId in ('App::Part', 'App::Origin') or (item.TypeId in ('App::Line', 'App::Plane', 'App::Point') and (not hasattr(item, 'Shape') or not item.Shape.Solids)):
                construction.add(item.Name)
        construction = sorted(construction)
        unaccounted = [item.Name for item in source_doc.Objects if hasattr(item, 'Shape') and not item.Shape.isNull() and item.Name not in [obj.Name] + excluded + construction]
        if unaccounted:
            raise ValueError('GEOMETRY_UNACCOUNTED_OBJECTS: ' + ','.join(sorted(unaccounted)))
        if any(item.State and any(state in ('Invalid', 'Error', 'Recompute') for state in item.State) for item in source_doc.Objects):
            raise ValueError('GEOMETRY_SOURCE_RECOMPUTE_ERROR')
        shape = obj.Shape.copy()
        shape.Placement = obj.getGlobalPlacement() * obj.Placement.inverse() * shape.Placement
        if shape.isNull() or not shape.isValid() or not shape.isClosed() or len(shape.Solids) != 1 or shape.Volume <= 0:
            raise ValueError('GEOMETRY_SINGLE_CLOSED_SOLID_REQUIRED')
        # Do not discard loose faces/edges from an otherwise one-solid compound.
        if abs(shape.Area - shape.Solids[0].Area) > 1e-10 or len(shape.Faces) != len(shape.Solids[0].Faces) or len(shape.Edges) != len(shape.Solids[0].Edges):
            raise ValueError('GEOMETRY_EXTRA_TOPOLOGY')
        shape = shape.Solids[0]
        original_volume = shape.Volume
        tolerance = float(contract['volume_tolerance']['value'])
        if contract['volume_tolerance']['unit'] != 'mm^3' or not math.isfinite(tolerance) or tolerance < 0 or len(contract['partitions']) > 16:
            raise ValueError('GEOMETRY_REQUEST_INVALID')
        for partition in contract['partitions']:
            if set(partition) != {'axis', 'coordinate'} or partition['axis'] not in ('x', 'y', 'z') or partition['coordinate']['unit'] != 'mm':
                raise ValueError('GEOMETRY_PARTITION_INVALID')
            axis = ('x', 'y', 'z').index(partition['axis'])
            coordinate = float(partition['coordinate']['value'])
            box = shape.BoundBox
            lower = [box.XMin, box.YMin, box.ZMin]
            upper = [box.XMax, box.YMax, box.ZMax]
            if not math.isfinite(coordinate) or not lower[axis] < coordinate < upper[axis]:
                raise ValueError('GEOMETRY_PARTITION_OUTSIDE_SOLID')
            margin = max(1., box.DiagonalLength)
            lower = [value - margin for value in lower]
            upper = [value + margin for value in upper]
            upper[axis] = coordinate
            clip = Part.makeBox(*[b - a for a, b in zip(lower, upper)], App.Vector(*lower))
            first, second = shape.common(clip), shape.cut(clip)
            if first.Volume <= 0 or second.Volume <= 0:
                raise ValueError('GEOMETRY_PARTITION_EMPTY')
            candidate = first.fuse(second)
            # Retain the split boundary faces; removeSplitter would erase them.
            if not candidate.isValid() or not candidate.isClosed() or len(candidate.Solids) != 1 or abs(candidate.Volume - original_volume) > tolerance:
                raise ValueError('GEOMETRY_PARTITION_TOPOLOGY_CHANGED')
            shape = candidate.Solids[0]
        derived_doc = App.newDocument('AnalysisGeometry')
        feature = derived_doc.addObject('Part::Feature', 'AnalysisSolid')
        feature.Shape = shape
        derived_doc.recompute()
        if feature.State and any(state in ('Invalid', 'Error', 'Recompute') for state in feature.State):
            raise ValueError('GEOMETRY_RECOMPUTE_ERROR')
        brep, fcstd = directory / 'geometry.brep', directory / 'geometry.FCStd'
        shape.exportBrep(str(brep))
        derived_doc.saveAs(str(fcstd))
        faces = []
        for index, face in enumerate(shape.Faces, 1):
            kind = {'Plane': 'plane', 'Cylinder': 'cylinder'}.get(type(face.Surface).__name__, 'other')
            umin, umax, vmin, vmax = face.ParameterRange
            faces.append({'index': index, 'surface_type': kind, 'area': quantity(face.Area, 'mm^2'), 'centroid': vector(face.CenterOfMass, 'mm'), 'normal': vector(face.normalAt((umin + umax) / 2, (vmin + vmax) / 2), '1')})
        box = shape.BoundBox
        if digest(source) != contract['source_sha256']:
            raise ValueError('ANALYSIS_SOURCE_CHANGED')
        return {'schema_version': 'AnalysisGeometry/v1', 'source_sha256': contract['source_sha256'], 'included_objects': [obj.Name], 'excluded_objects': excluded, 'construction_objects': construction, 'geometry_sha256': digest(brep), 'volume': quantity(shape.Volume, 'mm^3'), 'original_volume': quantity(original_volume, 'mm^3'), 'center_of_mass': vector(shape.CenterOfMass, 'mm'), 'bounds': vector([box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax], 'mm'), 'solid_count': len(shape.Solids), 'valid': shape.isValid(), 'closed': shape.isClosed(), 'partitions': contract['partitions'], 'faces': faces, 'artifacts': {'brep': {'relative_path': brep.name, 'sha256': digest(brep)}, 'fcstd': {'relative_path': fcstd.name, 'sha256': digest(fcstd)}}}
    finally:
        if derived_doc is not None:
            App.closeDocument(derived_doc.Name)
        App.closeDocument(source_doc.Name)


def main(request_path, output_path):
    import FreeCAD as App
    sys.dont_write_bytecode = True
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    result = {'schema_version': 'AnalysisNativePhase/v1', 'phase': 'geometry', 'request_id': request['request_id'], 'request_sha256': digest(request_path), 'script_sha256': digest(__file__), 'app_version': App.Version()[:3], 'native_temp_path': App.getTempPath(), 'status': 'failed'}
    try:
        if set(request) != {'request_id', 'source_path', 'expected_temp', 'contract'} or not os.path.samefile(App.getTempPath(), request['expected_temp']) or App.Version()[:3] != ['1', '1', '3']:
            raise ValueError('NATIVE_IDENTITY_MISMATCH')
        result['geometry'] = inspect(request['contract'], Path(request['source_path']), Path(output_path).parent)
        result['status'] = 'passed'
    except Exception:
        result['error'] = traceback.format_exc()
    Path(output_path).write_text(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
