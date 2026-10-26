"""Fresh native standard FEM objects from approved plain data, Python 3.11.

No customer document/Proxy restore, code field, custom stiffness, or input edit.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback
from decimal import Decimal, localcontext


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_density(density):
    # Contract units remain tonne/mm^3. FreeCAD's material-card parser accepts
    # the native kg/m^3 spelling used by its standard workbench material cards.
    with localcontext() as context:
        context.prec = 256
        number = Decimal(density) * Decimal('1e12')
    if not number.is_finite() or number <= 0: raise ValueError('DECK_DENSITY_INVALID')
    return format(number, 'f')+' kg/m^3'


def build_objects(contract, paths, *, document_name='AnalysisModel'):
    import FreeCAD as App
    import Fem
    import ObjectsFem
    import Part
    if set(contract) != {'spec_sha256', 'geometry_sha256', 'mesh_sha256', 'node_count', 'element_count', 'material', 'case'}:
        raise ValueError('DECK_CONTRACT_INVALID')
    if digest(paths['geometry.brep']) != contract['geometry_sha256'] or digest(paths['mesh.unv']) != contract['mesh_sha256']:
        raise ValueError('DECK_INPUT_HASH_CHANGED')
    doc = App.newDocument(document_name)
    try:
        solid = doc.addObject('Part::Feature', 'AnalysisSolid')
        shape = Part.Shape()
        shape.read(str(paths['geometry.brep']))
        solid.Shape = shape
        if not shape.isValid() or not shape.isClosed() or len(shape.Solids) != 1:
            raise ValueError('DECK_SINGLE_VALID_SOLID_REQUIRED')
        analysis = ObjectsFem.makeAnalysis(doc, 'Analysis')
        material = ObjectsFem.makeMaterialSolid(doc, 'Material')
        declaration = contract['material']
        card = {'Name': 'Declared equivalent elastic material', 'YoungsModulus': declaration['youngs_modulus']['value']+' MPa', 'PoissonRatio': declaration['poisson_ratio']['value']}
        gravity = [load for load in contract['case']['native_objects'] if load['type'] == 'gravity']
        if len(gravity) > 1: raise ValueError('DECK_DUPLICATE_GRAVITY')
        density = gravity[0]['equivalent_density_tonne_mm3'] if gravity else (None if declaration['density'] is None else declaration['density']['value'])
        if density is not None: card['Density'] = native_density(density)
        material.Material = card
        analysis.addObject(material)
        solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, 'CalculiX')
        solver.AnalysisType = 'static'
        solver.GeometricalNonlinearity = 'linear'
        solver.MaterialNonlinearity = 'linear'
        solver.ModelSpace = '3D'
        solver.SplitInputWriter = False
        solver.WorkingDir = str(paths['geometry.brep'].parent)
        analysis.addObject(solver)
        mesh = ObjectsFem.makeMeshGmsh(doc, 'Mesh')
        mesh.Shape = solid
        mesh.ElementOrder = '2nd'
        mesh.ElementDimension = '3D'
        mesh.FemMesh = Fem.read(str(paths['mesh.unv']))
        if mesh.FemMesh.NodeCount != contract['node_count'] or mesh.FemMesh.VolumeCount != contract['element_count']:
            raise ValueError('DECK_MESH_INVENTORY_CHANGED')
        analysis.addObject(mesh)
        faces = contract['case']['fixed_face_indices']
        if not faces or len(faces) != len(set(faces)): raise ValueError('DECK_SUPPORT_INVALID')
        fixed = ObjectsFem.makeConstraintFixed(doc, 'Fixed')
        fixed.References = [(solid, ['Face'+str(face) for face in faces])]
        analysis.addObject(fixed)
        for index, load in enumerate(contract['case']['native_objects']):
            kind = load['type']
            if kind == 'total_force':
                force = [float(value) for value in load['components_N']]
                magnitude = math.sqrt(sum(value*value for value in force))
                if not math.isfinite(magnitude) or magnitude <= 0: raise ValueError('DECK_FORCE_INVALID')
                direction = doc.addObject('Part::Feature', 'ForceDirection'+str(index))
                direction.Shape = Part.makeLine(App.Vector(0, 0, 0), App.Vector(*[value/magnitude for value in force]))
                obj = ObjectsFem.makeConstraintForce(doc, 'Force'+str(index))
                obj.Force = format(magnitude, '.17g')+' N'
                obj.Direction = (direction, ['Edge1'])
                obj.Reversed = False
            elif kind == 'pressure':
                obj = ObjectsFem.makeConstraintPressure(doc, 'Pressure'+str(index))
                obj.Pressure = load['value_MPa']+' MPa'
                obj.Reversed = load['reversed']
            elif kind == 'gravity':
                acceleration = [float(value) for value in load['components_mm_s2']]
                magnitude = math.sqrt(sum(value*value for value in acceleration))
                if not math.isfinite(magnitude) or magnitude <= 0: raise ValueError('DECK_GRAVITY_INVALID')
                obj = ObjectsFem.makeConstraintSelfWeight(doc, 'Gravity')
                obj.GravityAcceleration = format(magnitude, '.17g')+' mm/s^2'
                obj.GravityDirection = App.Vector(*[value/magnitude for value in acceleration])
            else:
                raise ValueError('DECK_LOAD_UNSUPPORTED')
            if kind != 'gravity': obj.References = [(solid, ['Face'+str(face) for face in load['face_indices']])]
            analysis.addObject(obj)
        doc.recompute()
        if any(any(value in ('Invalid', 'Error', 'Recompute') for value in obj.State) for obj in doc.Objects):
            raise ValueError('DECK_RECOMPUTE_FAILED')
        return doc, analysis, solver, mesh
    except BaseException:
        App.closeDocument(doc.Name)
        raise


def write_deck(contract, paths):
    import FreeCAD as App
    from femtools.ccxtools import FemToolsCcx
    doc, analysis, solver, mesh = build_objects(contract, paths)
    try:
        tool = FemToolsCcx(analysis=analysis, solver=solver)
        tool.update_objects()
        tool.setup_working_dir(str(paths['geometry.brep'].parent))
        error = tool.check_prerequisites()
        if error: raise ValueError('DECK_NATIVE_PREREQUISITE: '+error)
        tool.write_inp_file()
        inp = Path(tool.inp_file_name)
        if not inp.is_file() or inp.parent != paths['geometry.brep'].parent: raise ValueError('DECK_OUTPUT_OUTSIDE_PHASE')
        inventory = [{'name': obj.Name, 'type_id': obj.TypeId, 'proxy_module': getattr(type(getattr(obj, 'Proxy', None)), '__module__', None)} for obj in doc.Objects]
        return inp, inventory
    finally:
        App.closeDocument(doc.Name)


def main(request_path, output_path):
    import FreeCAD as App
    sys.dont_write_bytecode = True
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    result = {'schema_version': 'AnalysisNativePhase/v1', 'phase': 'build', 'request_id': request['request_id'], 'request_sha256': digest(request_path), 'script_sha256': digest(__file__), 'app_version': App.Version()[:3], 'native_temp_path': App.getTempPath(), 'status': 'failed'}
    try:
        if set(request) != {'request_id', 'expected_temp', 'inputs', 'contract'} or App.Version()[:3] != ['1', '1', '3'] or not os.path.samefile(App.getTempPath(), request['expected_temp']) or set(request['inputs']) != {'geometry.brep', 'mesh.unv'}:
            raise ValueError('DECK_REQUEST_INVALID')
        paths = {name: Path(ref['path']) for name, ref in request['inputs'].items()}
        if any(digest(paths[name]) != ref['sha256'] for name, ref in request['inputs'].items()): raise ValueError('DECK_INPUT_HASH_CHANGED')
        inp, inventory = write_deck(request['contract'], paths)
        result.update(status='passed', objects=inventory, spec_sha256=request['contract']['spec_sha256'], artifacts={'input': {'relative_path': inp.name, 'sha256': digest(inp)}})
    except Exception:
        result['error'] = traceback.format_exc()
    Path(output_path).write_text(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
