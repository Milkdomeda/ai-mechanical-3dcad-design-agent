"""Native FreeCAD 1.1.3 mesh factory; only BREP and closed fixed contracts.

Runs in a fresh owned interpreter. Never restores customer FEM objects.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import traceback

from analysis_mesh_math import EDGES, freecad_to_ccx_nodes, norm, ray_coverage, sub, tet10_quality, tetra_ray_intervals


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ray_may_intersect(points, start, end):
    controls = [*points[:4], *[[2*points[k+4][axis]-(points[i][axis]+points[j][axis])/2 for axis in range(3)] for k, (i, j) in enumerate(EDGES)]]
    low, high = 0., 1.
    for axis in range(3):
        minimum, maximum = min(p[axis] for p in controls), max(p[axis] for p in controls)
        slope = end[axis]-start[axis]
        if abs(slope) < 1e-15:
            if not minimum-1e-9 <= start[axis] <= maximum+1e-9: return False
        else:
            first, last = sorted(((minimum-start[axis])/slope, (maximum-start[axis])/slope))
            low, high = max(low, first), min(high, last)
            if high < low: return False
    return True


def create_mesh(contract, geometry_path, directory):
    import FreeCAD as App
    import Fem
    import ObjectsFem
    import Part
    from femmesh.gmshtools import GmshTools
    if set(contract) != {'level', 'bindings', 'gmsh_path'}: raise ValueError('MESH_REQUEST_INVALID')
    level, bindings = contract['level'], contract['bindings']
    if digest(geometry_path) != level['geometry_sha256'] or bindings['geometry_sha256'] != level['geometry_sha256'] or bindings['bindings_sha256'] != level['region_bindings_sha256'] or level['element_type'] != 'C3D10' or level['threads'] != 1:
        raise ValueError('MESH_BINDING_INVALID')
    shape = Part.Shape()
    shape.read(str(geometry_path))
    if not shape.isValid() or not shape.isClosed() or len(shape.Solids) != 1: raise ValueError('MESH_SINGLE_SOLID_REQUIRED')
    surface = Part.makeCompound(shape.Faces)
    for ray in level['critical_rays']:
        start, end = [App.Vector(*[float(value['value']) for value in ray[key]]) for key in ('start', 'end')]
        tolerance = float(ray['coverage_atol']['value'])
        if any(surface.distToShape(Part.Vertex(point))[0] > tolerance for point in (start, end)) or abs(shape.common(Part.makeLine(start, end)).Length-(end-start).Length) > tolerance:
            raise ValueError('MESH_RAY_NOT_FULL_SOLID_SPAN')
    doc = App.newDocument('AnalysisMesh')
    try:
        solid = doc.addObject('Part::Feature', 'AnalysisSolid')
        solid.Shape = shape
        analysis = ObjectsFem.makeAnalysis(doc, 'Analysis')
        mesh = ObjectsFem.makeMeshGmsh(doc, 'Mesh')
        mesh.Shape = solid
        mesh.CharacteristicLengthMax = level['size']['value'] + ' mm'
        locals_ = level['local_sizes']
        mesh.CharacteristicLengthMin = min(float(level['size']['value']), *(float(item['size']['value']) for item in locals_)) if locals_ else level['size']['value'] + ' mm'
        mesh.ElementOrder = '2nd'
        mesh.ElementDimension = '3D'
        mesh.Algorithm2D = 'Delaunay'
        mesh.Algorithm3D = 'Delaunay'
        mesh.SecondOrderLinear = False
        mesh.OptimizeStd = True
        mesh.OptimizeNetgen = False
        mesh.HighOrderOptimize = 'None'
        analysis.addObject(mesh)
        by_id = {binding['region_id']: binding for binding in bindings['bindings']}
        for index, item in enumerate(locals_):
            binding = by_id[item['region_id']]
            local = ObjectsFem.makeMeshRegion(doc, mesh, name='LocalMesh' + str(index))
            local.References = [(solid, ['Face' + str(face) for face in binding['face_indices']])]
            local.CharacteristicLength = item['size']['value'] + ' mm'
        App.ParamGet('User parameter:BaseApp/Preferences/Mod/Fem/Gmsh').SetInt('NumOfThreads', 1)
        doc.recompute()
        gmsh = GmshTools(mesh)
        gmsh.update_mesh_data()
        # No native tool discovery or shell. The host pins this exact binary.
        gmsh.gmsh_bin = contract['gmsh_path']
        gmsh.get_tmp_file_paths('.')
        gmsh.write_gmsh_input_files()
        geo = Path(gmsh.temp_file_geo)
        with geo.open('a', encoding='utf-8') as stream:
            stream.write('\n// Fixed deterministic adapter seed.\nMesh.RandomSeed = 1;\n')
        with (directory / 'gmsh.log').open('xb') as log:
            result = subprocess.run([contract['gmsh_path'], '-', geo.name], cwd=directory, stdout=log, stderr=subprocess.STDOUT, timeout=level['budget']['stage_seconds'], check=False)
        if result.returncode != 0: raise ValueError('MESH_GMSH_FAILED')
        native_mesh = directory / gmsh.temp_file_mesh
        mesh.FemMesh = Fem.read(str(native_mesh))
        fm = mesh.FemMesh
        nodes = {int(node): [point.x, point.y, point.z] for node, point in fm.Nodes.items()}
        elements = {int(element): freecad_to_ccx_nodes([int(node) for node in fm.getElementNodes(element)]) for element in fm.Volumes}
        if not nodes or not elements or any(len(element) != 10 or len(set(element)) != 10 or any(node not in nodes for node in element) for element in elements.values()):
            raise ValueError('MESH_C3D10_REQUIRED')
        if len(nodes) > level['budget']['max_nodes'] or len(elements) > level['budget']['max_elements']:
            raise ValueError('MESH_BUDGET_EXCEEDED')
        qualities = [tet10_quality([nodes[node] for node in element], float(level['minimum_scaled_jacobian']['value'])) for element in elements.values()]
        rays = []
        for ray in level['critical_rays']:
            start, end = [[float(value['value']) for value in ray[key]] for key in ('start', 'end')]
            intervals = []
            for element in elements.values():
                points = [nodes[node] for node in element]
                if _ray_may_intersect(points, start, end):
                    try:
                        intervals.extend(tetra_ray_intervals(points, start, end))
                    except ValueError as exc:
                        raise ValueError(str(exc) + ' ' + json.dumps({'points': points, 'start': start, 'end': end}, separators=(',', ':'))) from exc
            rays.append({'id': ray['id'], **ray_coverage(intervals)})
        mapping = {'geometry_sha256': bindings['geometry_sha256'], 'regions': []}
        for binding in bindings['bindings']:
            region_nodes, faces = set(), set()
            for face_id in binding['face_indices']:
                face = shape.Faces[face_id-1]
                ids = fm.getNodesByFace(face)
                pairs = fm.getccxVolumesByFace(face)
                if not ids or not pairs: raise ValueError('REGION_MESH_MAPPING_EMPTY')
                region_nodes.update(int(node) for node in ids)
                faces.update((int(pair[0]), int(pair[1])) for pair in pairs)
            mapping['regions'].append({'region_id': binding['region_id'], 'face_indices': binding['face_indices'], 'node_ids': sorted(region_nodes), 'element_faces': [list(pair) for pair in sorted(faces)]})
        data_path, mapping_path = directory / 'mesh-data.json', directory / 'mesh-regions.json'
        data_path.write_text(json.dumps({'node_order': 'CalculiX_C3D10', 'nodes': [[node, *point] for node, point in sorted(nodes.items())], 'elements': [[element, *ids] for element, ids in sorted(elements.items())]}, allow_nan=False, separators=(',', ':')), encoding='utf-8')
        mapping_path.write_text(json.dumps(mapping, allow_nan=False, separators=(',', ':')), encoding='utf-8')
        native_copy = directory / 'mesh.unv'
        fm.write(str(native_copy))
        artifacts = {key: {'relative_path': path.name, 'sha256': digest(path)} for key, path in [('data', data_path), ('mapping', mapping_path), ('native_mesh', native_copy), ('gmsh_geometry', geo)]}
        quality = {'checked_elements': len(elements), 'min_det_mm3': min(item['min_det_mm3'] for item in qualities), 'min_scaled_jacobian': min(item['min_scaled_jacobian'] for item in qualities), 'sample_points_per_element': 15, 'bernstein_scaled_lower_bound': min(item['bernstein_scaled_lower_bound'] for item in qualities), 'bound_subdivision_depth': max(item['bound_subdivision_depth'] for item in qualities), 'thickness_rays': rays}
        return {'schema_version': 'AnalysisMeshReceipt/v1', 'geometry_sha256': level['geometry_sha256'], 'region_bindings_sha256': level['region_bindings_sha256'], 'level': level['level'], 'element_type': 'C3D10', 'node_count': len(nodes), 'element_count': len(elements), 'disk_bytes': sum(path.stat().st_size for path in directory.iterdir() if path.is_file()), 'quality': quality, 'artifacts': artifacts}
    finally:
        App.closeDocument(doc.Name)


def main(request_path, output_path):
    import FreeCAD as App
    sys.dont_write_bytecode = True
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    result = {'schema_version': 'AnalysisNativePhase/v1', 'phase': 'mesh', 'request_id': request['request_id'], 'request_sha256': digest(request_path), 'script_sha256': digest(__file__), 'app_version': App.Version()[:3], 'native_temp_path': App.getTempPath(), 'status': 'failed'}
    try:
        if set(request) != {'request_id', 'source_path', 'expected_temp', 'contract'} or not os.path.samefile(App.getTempPath(), request['expected_temp']) or App.Version()[:3] != ['1', '1', '3']:
            raise ValueError('NATIVE_IDENTITY_MISMATCH')
        result['mesh'] = create_mesh(request['contract'], Path(request['source_path']), Path(output_path).parent)
        result['status'] = 'passed'
    except Exception:
        result['error'] = traceback.format_exc()
    Path(output_path).write_text(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
