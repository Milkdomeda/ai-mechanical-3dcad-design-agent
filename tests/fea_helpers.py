"""Run inside a clean FreeCAD interpreter, using built-in modules only."""
from __future__ import annotations

import json
import contextlib
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback


def read_version(name, result):
    raw = result.stdout + result.stderr
    pattern = r"This is Version (\d+\.\d+(?:\.\d+)?)" if name == "ccx" else r"(\d+\.\d+(?:\.\d+)?)"
    match = re.search(pattern, raw)
    allowed_exit = result.returncode == 0 or (name == "ccx" and result.returncode == 201)
    if not allowed_exit or not match:
        raise RuntimeError("Cannot determine native " + name + " version")
    return match.group(1)


def _det(matrix):
    a, b, c = matrix
    return a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])


def tet10_quality(coordinates):
    """Sample the actual C3D10 map, including quadratic midside geometry.

    This sampling is evidence, not a global positivity proof for arbitrary
    curved elements. P0 calibration applies only to these straight benchmarks.
    """
    samples = [(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1), (.25,) * 4]
    edges = [(0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3)]
    for i, j in edges:
        point = [0.0] * 4
        point[i] = point[j] = .5
        samples.append(point)
    for i in range(4):
        samples.append(tuple(.5854101966249685 if j == i else .1381966011250105 for j in range(4)))
    gradients = [(-1, -1, -1), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    determinants, scaled = [], []
    for point in samples:
        derivatives = [[(4 * point[i] - 1) * value for value in gradients[i]] for i in range(4)]
        derivatives.extend([[4 * (point[i] * gradients[j][k] + point[j] * gradients[i][k]) for k in range(3)] for i, j in edges])
        matrix = [[sum(coordinates[n][axis] * derivatives[n][k] for n in range(10)) for k in range(3)] for axis in range(3)]
        determinant = _det(matrix)
        scale = math.prod(math.sqrt(sum(matrix[axis][k] ** 2 for axis in range(3))) for k in range(3))
        determinants.append(determinant)
        scaled.append(determinant / scale if scale else 0)
    return {"min_det_mm3": min(determinants), "min_scaled_jacobian": min(scaled), "sample_points": len(samples)}


def _solve(matrix, values):
    determinant = _det(matrix)
    return [_det([[values[row] if col == axis else matrix[row][col] for col in range(3)] for row in range(3)]) / determinant for axis in range(3)]


def ray_coverage(intervals):
    """Union coverage and conservative minimum element cover of a ray.

    A ray can lie on a shared face: summing element intervals would count
    that physical length twice. A spanning element must still fail the
    resolution check even if other overlapping intervals subdivide it.
    """
    intervals = sorted(intervals)
    if not intervals:
        return {"covered_mm": 0, "elements": 0}
    merged = []
    for low, high in intervals:
        if merged and low <= merged[-1][1] + 1e-8:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    count = 0
    for low, high in merged:
        cursor = low
        while cursor < high - 1e-8:
            end = max(b for a, b in intervals if a <= cursor + 1e-8)
            if end <= cursor + 1e-8:
                raise RuntimeError("Nonadvancing mesh ray cover")
            count += 1
            cursor = end
    return {"covered_mm": sum(high - low for low, high in merged), "elements": count}


def mesh_quality(inp, length, width, height):
    nodes, elements = {}, []
    section = ""
    for line in inp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            section = "nodes" if line.upper().startswith("*NODE,") else "elements" if line.upper().startswith("*ELEMENT,") and "C3D10" in line.upper() else ""
            continue
        data = [value.strip() for value in line.split(",") if value.strip()]
        if section == "nodes":
            nodes[int(data[0])] = tuple(float(value) for value in data[1:4])
        elif section == "elements":
            if len(data) != 11:
                raise RuntimeError("Expected one native ten-node tetrahedron per record")
            elements.append([int(value) for value in data[1:]])
    qualities = [tet10_quality([nodes[n] for n in element]) for element in elements]
    rays = []
    for x, y in ((.37 * length, .43 * width), (.51 * length, .57 * width), (.73 * length, .37 * width)):
        intervals = []
        for element in elements:
            points = [nodes[n] for n in element[:4]]
            matrix = [[points[col + 1][row] - points[0][row] for col in range(3)] for row in range(3)]
            intercept = _solve(matrix, [x - points[0][0], y - points[0][1], -points[0][2]])
            slope = _solve(matrix, [0, 0, 1])
            intercept = [1 - sum(intercept), *intercept]
            slope = [-sum(slope), *slope]
            low, high = 0.0, float(height)
            for a, b in zip(intercept, slope):
                if abs(b) < 1e-12:
                    if a < -1e-9:
                        high = -1
                elif b > 0:
                    low = max(low, -a / b)
                else:
                    high = min(high, -a / b)
            if high - low > 1e-7:
                intervals.append((low, high))
        rays.append({"xy_mm": [x, y], **ray_coverage(intervals)})
    return {"checked_elements": len(elements), "min_det_mm3": min(q["min_det_mm3"] for q in qualities), "min_scaled_jacobian": min(q["min_scaled_jacobian"] for q in qualities), "sample_points_per_element": 15, "thickness_rays": rays, "scope": "straight synthetic benchmarks only; not a global curved-element positivity proof"}


def _reaction(text):
    match = re.search(r"total force.*?\n\s*\n?\s*([-+\d.Ee]+)\s+([-+\d.Ee]+)\s+([-+\d.Ee]+)", text, re.I)
    if not match:
        raise RuntimeError("Native RF totals missing from dat output")
    return [float(value) for value in match.groups()]


def _cloads(path):
    total = [0.0, 0.0, 0.0]
    active = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            active = line.upper().startswith("*CLOAD")
        elif active:
            _, axis, value = line.split(",")[:3]
            total[int(axis) - 1] += float(value)
    return total


def validate_result_fields(mesh_nodes, ids, displacement, stress, mises):
    if (not ids or len(set(ids)) != len(ids) or set(ids) != set(mesh_nodes)
            or any(type(node) is not int or node <= 0 for node in ids)):
        raise RuntimeError("RESULT_NODE_INVENTORY")
    if any(len(field) != len(ids) for field in (displacement, stress, mises)):
        raise RuntimeError("RESULT_FIELD_LENGTH")
    if any(len(vector) != 3 for vector in displacement):
        raise RuntimeError("RESULT_VECTOR_LENGTH")
    values = list(stress) + list(mises) + [value for vector in displacement for value in vector]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise RuntimeError("RESULT_NONFINITE")
    return {"complete": True, "nodes": len(ids)}


def nodal_load_evidence(path, allowed_nodes):
    """Read actual native node coordinates and loads, about the global origin."""
    nodes, loads = {}, []
    active = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            active = line.split(",", 1)[0].upper()
        elif active == "*NODE":
            node, *xyz = line.split(",")
            nodes[int(node)] = tuple(float(value) for value in xyz[:3])
        elif active == "*CLOAD":
            node, axis, value = line.split(",")[:3]
            loads.append((int(node), int(axis), float(value)))
    force, moment = [0., 0., 0.], [0., 0., 0.]
    for node, axis, value in loads:
        if node not in nodes or axis not in (1, 2, 3) or not math.isfinite(value):
            raise RuntimeError("NATIVE_LOAD_MAPPING")
        vector = [0., 0., 0.]
        vector[axis - 1] = value
        x, y, z = nodes[node]
        fx, fy, fz = vector
        for i, component in enumerate(vector):
            force[i] += component
        for i, component in enumerate((y*fz-z*fy, z*fx-x*fz, x*fy-y*fx)):
            moment[i] += component
    return {"force_N": force, "moment_Nmm": moment, "moment_origin_mm": [0, 0, 0],
            "loaded_nodes": len({node for node, _, _ in loads}),
            "load_region_valid": bool(loads) and all(node in allowed_nodes for node, _, _ in loads)}


def prepare_native_gmsh_input(gmsh, output):
    # FreeCAD's embedded Windows Python can ignore PYTHONUTF8. The native
    # writer writes its working path into comments with the locale encoding.
    # Its public relative-working-directory API avoids that encoding boundary
    # while keeping every file in the requested Unicode workspace.
    with contextlib.chdir(output):
        gmsh.get_tmp_file_paths(".")
        gmsh.write_gmsh_input_files()
        return output / gmsh.temp_file_geo, output / gmsh.temp_file_mesh


def build_case(spec, config, output, *, evidence_hook=None, reverse_force=False, permute_references=False):
    import FreeCAD as App
    import Part
    import ObjectsFem
    import Fem
    from femmesh.gmshtools import GmshTools
    from femtools.ccxtools import FemToolsCcx
    from feminout.importCcxFrdResults import read_frd_result

    output.mkdir()
    doc = App.newDocument("P0_" + spec["name"])
    box = doc.addObject("Part::Feature", "Solid")
    box.Shape = Part.makeBox(spec["length_mm"], spec["width_mm"], spec["height_mm"])
    if spec["name"] == "multiface":
        box.Shape = Part.makeBox(100, 5, 10).fuse(Part.makeBox(100, 5, 10, App.Vector(0, 5, 0)))
    if spec["name"] == "curved_multiface":
        box.Shape = Part.makeCylinder(5, 40, App.Vector(0, 5, 5), App.Vector(1, 0, 0)).fuse(
            Part.makeCylinder(5, 60, App.Vector(40, 5, 5), App.Vector(1, 0, 0)))
    analysis = ObjectsFem.makeAnalysis(doc, "Analysis")
    material = ObjectsFem.makeMaterialSolid(doc, "Material")
    material.Material = {"Name": "Synthetic benchmark", "YoungsModulus": f'{spec["youngs_modulus_MPa"]} MPa', "PoissonRatio": str(spec["poisson_ratio"]), "Density": "1000 kg/m^3"}
    analysis.addObject(material)
    solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, "CalculiX")
    solver.AnalysisType = "static"
    solver.GeometricalNonlinearity = "linear"
    solver.WorkingDir = str(output)
    analysis.addObject(solver)
    fixed = ObjectsFem.makeConstraintFixed(doc, "Fixed")
    fixed.References = [(box, [f"Face{i}" for i, face in enumerate(box.Shape.Faces, 1) if abs(face.CenterOfMass.x) < 1e-7])]
    analysis.addObject(fixed)
    load = ObjectsFem.makeConstraintForce(doc, "Load")
    load_faces = [f"Face{i}" for i, face in enumerate(box.Shape.Faces, 1) if abs((face.CenterOfMass.z - 10) if spec["name"] == "multiface" else (face.CenterOfMass.x - spec["length_mm"])) < 1e-7]
    if spec["name"] == "curved_multiface":
        load_faces = [f"Face{i}" for i, face in enumerate(box.Shape.Faces, 1) if isinstance(face.Surface, Part.Cylinder)]
    if permute_references:load_faces=list(reversed(load_faces))
    load.References = [(box, load_faces)]
    load.Force = f'{spec["force_N"]} N'
    direction = doc.addObject("Part::Feature", "LoadDirection")
    direction.Shape = Part.makeLine(App.Vector(0, 0, 0), App.Vector(1, 0, 0) if spec["name"] == "axial_bar" else App.Vector(0, 0, -1))
    load.Direction = (direction, ["Edge1"])
    load.Reversed = reverse_force
    analysis.addObject(load)
    mesh = ObjectsFem.makeMeshGmsh(doc, "Mesh")
    mesh.Shape = box
    mesh.CharacteristicLengthMax = spec["mesh_size_mm"]
    mesh.CharacteristicLengthMin = spec["mesh_size_mm"]
    mesh.ElementOrder = "2nd"
    mesh.ElementDimension = "3D"
    analysis.addObject(mesh)
    doc.recompute()
    gmsh = GmshTools(mesh)
    gmsh.update_mesh_data()
    gmsh.get_gmsh_command()
    geo_path, mesh_path = prepare_native_gmsh_input(gmsh, output)
    with (output / "gmsh.log").open("w", encoding="utf-8") as log:
        run = subprocess.run([config["tools"]["gmsh"]["path"], "-", geo_path.name], cwd=output, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
    if run.returncode:
        raise RuntimeError("Gmsh failed")
    mesh.FemMesh = Fem.read(str(mesh_path))
    doc.recompute()
    fea = FemToolsCcx(analysis=analysis, solver=solver)
    fea.update_objects()
    fea.setup_working_dir(str(output))
    error = fea.check_prerequisites()
    if error:
        raise RuntimeError(error)
    fea.write_inp_file()
    inp = Path(fea.inp_file_name)
    if spec["name"] in {"multiface", "curved_multiface"}:
        doc.saveAs(str(output / "analysis.FCStd"))
        allowed_nodes = {node for face in load_faces for node in mesh.FemMesh.getNodesByFace(box.Shape.getElement(face))}
        result = {**nodal_load_evidence(inp, allowed_nodes), "faces": len(load_faces),
                  "mesh_inventory": {"nodes": mesh.FemMesh.NodeCount, "elements": mesh.FemMesh.VolumeCount},
                  "curved_faces": sum(isinstance(box.Shape.getElement(face).Surface, Part.Cylinder) for face in load_faces),
                  "partial_face": {"valid": box.Shape.isValid(), "solid_count": len(box.Shape.Solids), "volume_delta_mm3": box.Shape.Volume - (math.pi*2500 if spec["name"] == "curved_multiface" else 10000)}}
        if evidence_hook is not None:
            result['r1']=evidence_hook(doc=doc,analysis=analysis,mesh=mesh,fea=fea,solid=box,load=load,load_faces=load_faces,inp=inp,output=output,config=config)
        App.closeDocument(doc.Name)
        return result
    with (output / "ccx.log").open("w", encoding="utf-8") as log:
        run = subprocess.run([config["tools"]["ccx"]["path"], "-i", inp.stem], cwd=output, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
    if run.returncode:
        raise RuntimeError("CalculiX failed")
    data = read_frd_result(str(inp.with_suffix(".frd")))
    # Keep the native parser's full structured output for API characterization.
    (output / "frd-structure.txt").write_text(repr(data)[:5000], encoding="utf-8")
    fea.load_results()
    result = next(obj for obj in analysis.Group if hasattr(obj, "vonMises"))
    ids = list(result.NodeNumbers)
    displacement = list(result.DisplacementVectors)
    inventory = validate_result_fields(mesh.FemMesh.Nodes, ids,
        [(v.x, v.y, v.z) for v in displacement], list(result.NodeStressXX), list(result.vonMises))
    if spec["name"] == "axial_bar":
        tips = [v.x for n, v in zip(ids, displacement) if abs(mesh.FemMesh.getNodeById(n).x - spec["length_mm"]) < 1e-6]
        far = [value for n, value in zip(ids, result.NodeStressXX) if 40 < mesh.FemMesh.getNodeById(n).x < 60]
        measured_disp = sum(tips) / len(tips)
        measured_stress = sum(far) / len(far)
    else:
        tips = [-v.z for n, v in zip(ids, displacement) if abs(mesh.FemMesh.getNodeById(n).x - spec["length_mm"]) < 1e-6]
        measured_disp = sum(tips) / len(tips)
        # Native stress component field names are captured explicitly; fail
        # rather than silently replace signed bending stress by von Mises.
        components = getattr(result, "NodeStressXX", None)
        if components is None:
            raise RuntimeError("Native NodeStressXX unavailable: " + repr(result.PropertiesList))
        selected = [(n, value) for n, value in zip(ids, components) if abs(mesh.FemMesh.getNodeById(n).x - 100) < 4 and mesh.FemMesh.getNodeById(n).z > 9.999]
        measured_stress = sum(value for n, value in selected) / len(selected)
    doc.recompute()
    doc.saveAs(str(output / "analysis.FCStd"))
    values = list(result.vonMises) + [component for v in displacement for component in (v.x, v.y, v.z)]
    report = {
        "displacement_mm": measured_disp,
        "stress_MPa": measured_stress,
        "reaction_N": _reaction(inp.with_suffix(".dat").read_text(encoding="utf-8")),
        "elements": mesh.FemMesh.VolumeCount,
        "mesh_inventory": {"nodes": mesh.FemMesh.NodeCount, "elements": mesh.FemMesh.VolumeCount},
        "element_nodes": sorted({len(mesh.FemMesh.getElementNodes(e)) for e in mesh.FemMesh.Volumes}),
        "input_element_types": sorted(set(re.findall(r"^\*ELEMENT,\s*TYPE=([^,\s]+)", inp.read_text(encoding="utf-8"), re.M | re.I))),
        "finite": bool(values) and all(math.isfinite(value) for value in values),
        "result_inventory": inventory,
        "geometry_valid": box.Shape.isValid(),
        "solid_count": len(box.Shape.Solids),
        "volume_mm3": box.Shape.Volume,
        "rf_native_output": bool(re.search(r"\*NODE PRINT[^\n]*\nRF\s*\n", inp.read_text(encoding="utf-8"))),
        "result_properties": result.PropertiesList,
        "input_file": inp.name,
        "cload_N": _cloads(inp),
        "quality": mesh_quality(inp, spec["length_mm"], spec["width_mm"], spec["height_mm"]),
        "object_factories": ["makeAnalysis", "makeMaterialSolid", "makeSolverCalculiXCcxTools", "makeConstraintFixed", "makeConstraintForce", "makeMeshGmsh"],
    }
    App.closeDocument(doc.Name)
    return report


def main(request_path, response_path):
    import FreeCAD as App
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(Path(__file__).parent))
    from fea_r1_runtime import loaded_module_receipt,native_identity,startup_receipt
    from femmesh import meshtools
    config = json.loads(Path(request_path).read_text(encoding="utf-8"))
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Gmsh").SetString("gmshBinaryPath", config["tools"]["gmsh"]["path"])
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Gmsh").SetInt("NumOfThreads", 1)
    App.ParamGet("User parameter:BaseApp/Preferences/Mod/Fem/Ccx").SetString("ccxBinaryPath", config["tools"]["ccx"]["path"])
    fixtures = Path(__file__).parent / "fixtures" / "fea"
    report = {"version": ".".join(App.Version()[:3]), "auto_reopen_count": 0, "cases": {}}
    # Enforce the no-restore boundary in the live interpreter, not by a claim
    # inferred from a successful solver run.
    original_open = App.openDocument
    def deny_document_restore(*args, **kwargs):
        report["auto_reopen_count"] += 1
        raise RuntimeError("P0 forbids restoring persisted documents")
    App.openDocument = deny_document_restore
    try:
        report['native_module_before']=loaded_module_receipt(config['_native_module'],meshtools)
        report['startup_paths']=startup_receipt(Path(response_path).parent)
        report['native_runtime_versions']=native_identity()
        report['native_temp_path']=App.getTempPath()
        expected=config.get('_expected_native_temp')
        if expected and not os.path.samefile(report['native_temp_path'],expected):
            raise RuntimeError('NATIVE_TEMP_MISMATCH: refusing CAD work in an unexpected temporary directory')
        report["versions"] = {"freecadcmd": report["version"]}
        for name, flag in (("gmsh", "--version"), ("ccx", "-v")):
            version_run = subprocess.run([config["tools"][name]["path"], flag], capture_output=True, text=True, timeout=15)
            report["versions"][name] = read_version(name, version_run)
        for name in ("axial_bar", "cantilever"):
            spec = json.loads((fixtures / (name + ".json")).read_text(encoding="utf-8"))
            report["cases"][name] = build_case(spec, config, Path(response_path).parent / name)
        multi = {"name": "multiface", "length_mm": 100, "width_mm": 10, "height_mm": 10, "youngs_modulus_MPa": 200000, "poisson_ratio": 0.3, "force_N": 40, "mesh_size_mm": 4}
        report["multiface"] = build_case(multi, config, Path(response_path).parent / "multiface")
        report["partial_face"] = report["multiface"].pop("partial_face")
        multi["name"] = "curved_multiface"
        report["curved_multiface"] = build_case(multi, config, Path(response_path).parent / "curved_multiface")
        report['native_module_after']=loaded_module_receipt(config['_native_module'],meshtools)
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        App.openDocument = original_open
    Path(response_path).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
