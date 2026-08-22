"""Read-only FreeCAD/STEP extractor for ModelManifest/v2.

Run with FreeCADCmd:
    freecadcmd extract_model_manifest.py INPUT OUTPUT

The source document is never saved or modified on disk.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


PARSER_VERSION = "freecad-model-manifest/v2-extractor-0.3.1"
DISTANCE_TOLERANCE_MM = 0.02
INTERFERENCE_TOLERANCE_MM3 = 1e-5
MAX_CLEARANCE_CANDIDATE_MM = 2.0
MAX_RELATION_PAIRS = 2000
MAX_EXACT_RELATION_PAIRS = 50


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _vec(vector):
    return [float(vector.x), float(vector.y), float(vector.z)]


def _unit(vector):
    values = _vec(vector) if hasattr(vector, "x") else [float(v) for v in vector]
    length = math.sqrt(sum(v * v for v in values))
    if length <= 1e-12:
        return [0.0, 0.0, 0.0]
    result = [round(v / length, 9) for v in values]
    first = next((v for v in result if abs(v) > 1e-9), 1.0)
    return result if first > 0 else [-v for v in result]


def _bbox(shape):
    box = shape.BoundBox
    return {
        "min": [float(box.XMin), float(box.YMin), float(box.ZMin)],
        "max": [float(box.XMax), float(box.YMax), float(box.ZMax)],
        "size": [float(box.XLength), float(box.YLength), float(box.ZLength)],
        "center": [
            float((box.XMin + box.XMax) / 2.0),
            float((box.YMin + box.YMax) / 2.0),
            float((box.ZMin + box.ZMax) / 2.0),
        ],
    }


def _placement(obj):
    placement = obj.getGlobalPlacement() if hasattr(obj, "getGlobalPlacement") else obj.Placement
    qx, qy, qz, qw = placement.Rotation.Q
    return {
        "translation_mm": _vec(placement.Base),
        "rotation_quaternion_wxyz": [float(qw), float(qx), float(qy), float(qz)],
    }


def _shape_hash(shape):
    try:
        brep = shape.exportBrepToString()
        if isinstance(brep, str):
            brep = brep.encode("utf-8")
        return hashlib.sha256(brep).hexdigest()
    except Exception:
        signature = {
            "volume": round(float(shape.Volume), 9),
            "area": round(float(shape.Area), 9),
            "bbox": _bbox(shape),
            "solids": len(shape.Solids),
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
        }
        return hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()


def _surface_features(shape):
    counts = {}
    cylinders = []
    planes = []
    axes = []
    seen_axes = set()
    for index, face in enumerate(shape.Faces, start=1):
        surface = getattr(face, "Surface", None)
        kind = type(surface).__name__ if surface is not None else "UnknownSurface"
        counts[kind] = counts.get(kind, 0) + 1
        axis = getattr(surface, "Axis", None)
        if axis is not None:
            normalized = _unit(axis)
            key = tuple(normalized)
            if key not in seen_axes:
                axes.append(normalized)
                seen_axes.add(key)
        radius = getattr(surface, "Radius", None)
        if radius is not None and axis is not None:
            center = getattr(surface, "Center", None) or getattr(surface, "Location", None)
            cylinders.append(
                {
                    "face": f"Face{index}",
                    "radius_mm": float(radius),
                    "diameter_mm": float(radius) * 2.0,
                    "axis": _unit(axis),
                    "origin_mm": _vec(center) if center is not None else [0.0, 0.0, 0.0],
                    "interpretation": "cylindrical-face-candidate",
                }
            )
        if "plane" in kind.lower():
            try:
                normal = _unit(getattr(surface, "Axis", face.normalAt(0.0, 0.0)))
                origin = getattr(surface, "Position", None) or getattr(surface, "Location", None)
                if origin is None:
                    origin = face.CenterOfMass
                planes.append(
                    {
                        "face": f"Face{index}",
                        "normal": normal,
                        "origin_mm": _vec(origin),
                        "interpretation": "planar-face-candidate",
                    }
                )
            except Exception:
                pass
    return counts, cylinders[:64], planes[:64], axes[:16]


def _center_of_mass(shape):
    try:
        return _vec(shape.CenterOfMass)
    except Exception:
        solids = list(shape.Solids)
        total = sum(float(solid.Volume) for solid in solids)
        if total <= 1e-12:
            box = _bbox(shape)
            return box["center"]
        return [
            sum(float(solid.Volume) * _vec(solid.CenterOfMass)[axis] for solid in solids) / total
            for axis in range(3)
        ]


def _principal_inertia(matrix_values):
    """Return sorted eigenpairs for a real symmetric 3x3 inertia matrix."""
    matrix = [[float(matrix_values[row][column]) for column in range(3)] for row in range(3)]
    vectors = [[1.0 if row == column else 0.0 for column in range(3)] for row in range(3)]
    for _ in range(32):
        p, q = max(((0, 1), (0, 2), (1, 2)), key=lambda pair: abs(matrix[pair[0]][pair[1]]))
        if abs(matrix[p][q]) <= 1e-12:
            break
        angle = 0.5 * math.atan2(2.0 * matrix[p][q], matrix[q][q] - matrix[p][p])
        cosine = math.cos(angle)
        sine = math.sin(angle)
        app = matrix[p][p]
        aqq = matrix[q][q]
        apq = matrix[p][q]
        matrix[p][p] = cosine * cosine * app - 2.0 * sine * cosine * apq + sine * sine * aqq
        matrix[q][q] = sine * sine * app + 2.0 * sine * cosine * apq + cosine * cosine * aqq
        matrix[p][q] = matrix[q][p] = 0.0
        for index in range(3):
            if index in (p, q):
                continue
            aip = matrix[index][p]
            aiq = matrix[index][q]
            matrix[index][p] = matrix[p][index] = cosine * aip - sine * aiq
            matrix[index][q] = matrix[q][index] = sine * aip + cosine * aiq
        for index in range(3):
            vip = vectors[index][p]
            viq = vectors[index][q]
            vectors[index][p] = cosine * vip - sine * viq
            vectors[index][q] = sine * vip + cosine * viq
    pairs = sorted(
        (
            float(matrix[index][index]),
            _unit([vectors[0][index], vectors[1][index], vectors[2][index]]),
        )
        for index in range(3)
    )
    return {
        "principal_moments": [pair[0] for pair in pairs],
        "principal_axes": [pair[1] for pair in pairs],
    }


def _mesh_mass_properties(vertices, triangles):
    """Approximate uniform-density volume inertia from an oriented closed triangle mesh."""
    points = [
        _vec(vertex) if hasattr(vertex, "x") else [float(value) for value in vertex]
        for vertex in vertices
    ]
    signed_volume = 0.0
    first_moment = [0.0, 0.0, 0.0]
    second_moment = [[0.0, 0.0, 0.0] for _ in range(3)]
    for triangle in triangles:
        a, b, c = (points[int(index)] for index in triangle)
        cross = [
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0],
        ]
        tetra_volume = sum(a[index] * cross[index] for index in range(3)) / 6.0
        signed_volume += tetra_volume
        for row in range(3):
            first_moment[row] += tetra_volume * (a[row] + b[row] + c[row]) / 4.0
            for column in range(3):
                diagonal_sum = a[row] * a[column] + b[row] * b[column] + c[row] * c[column]
                coordinate_product = (a[row] + b[row] + c[row]) * (a[column] + b[column] + c[column])
                if row == column:
                    pair_sum = a[row] * b[row] + a[row] * c[row] + b[row] * c[row]
                    second_moment[row][column] += tetra_volume * (diagonal_sum + pair_sum) / 10.0
                else:
                    second_moment[row][column] += tetra_volume * (diagonal_sum + coordinate_product) / 20.0
    if abs(signed_volume) <= 1e-12:
        raise ValueError("tessellated shape has zero signed volume")
    orientation = 1.0 if signed_volume > 0 else -1.0
    volume = signed_volume * orientation
    first_moment = [value * orientation for value in first_moment]
    second_moment = [[value * orientation for value in row] for row in second_moment]
    center = [value / volume for value in first_moment]
    inertia_origin = [
        [
            (second_moment[1][1] + second_moment[2][2]) if row == column == 0 else
            (second_moment[0][0] + second_moment[2][2]) if row == column == 1 else
            (second_moment[0][0] + second_moment[1][1]) if row == column == 2 else
            -second_moment[row][column]
            for column in range(3)
        ]
        for row in range(3)
    ]
    center_squared = sum(value * value for value in center)
    inertia_center = [
        [
            inertia_origin[row][column]
            - volume * ((center_squared if row == column else 0.0) - center[row] * center[column])
            for column in range(3)
        ]
        for row in range(3)
    ]
    return {"volume_mm3": volume, "center_of_mass_mm": center, "matrix": inertia_center}


def _inertia(shape):
    try:
        matrix = shape.MatrixOfInertia
        rows = [
            [float(matrix.A11), float(matrix.A12), float(matrix.A13)],
            [float(matrix.A21), float(matrix.A22), float(matrix.A23)],
            [float(matrix.A31), float(matrix.A32), float(matrix.A33)],
        ]
        return {
            "diagonal": [float(matrix.A11), float(matrix.A22), float(matrix.A33)],
            "products": [float(matrix.A12), float(matrix.A13), float(matrix.A23)],
            **_principal_inertia(rows),
            "basis": "freecad-exact-shape-property",
            "units": "mm^5 at unit density",
        }
    except Exception:
        try:
            box = shape.BoundBox
            deflection = max(0.05, max(float(box.XLength), float(box.YLength), float(box.ZLength)) / 400.0)
            vertices, triangles = shape.tessellate(deflection)
            properties = _mesh_mass_properties(vertices, triangles)
            rows = properties["matrix"]
            return {
                "diagonal": [rows[index][index] for index in range(3)],
                "products": [rows[0][1], rows[0][2], rows[1][2]],
                **_principal_inertia(rows),
                "basis": "closed-triangle-mesh-uniform-density",
                "units": "mm^5 at unit density",
                "mesh_deflection_mm": deflection,
                "mesh_triangle_count": len(triangles),
                "mesh_volume_mm3": properties["volume_mm3"],
            }
        except Exception as exc:
            return {
                "diagonal": [0.0, 0.0, 0.0],
                "products": [0.0, 0.0, 0.0],
                "principal_moments": [0.0, 0.0, 0.0],
                "principal_axes": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "basis": "unavailable",
                "units": "mm^5 at unit density",
                "diagnostic": f"{type(exc).__name__}: {exc}",
            }


def _parents(obj):
    parents = []
    for parent in getattr(obj, "InList", []):
        type_id = str(getattr(parent, "TypeId", ""))
        if type_id in {"App::Part", "App::DocumentObjectGroup"} or hasattr(parent, "Group"):
            parents.append(parent.Name)
    return sorted(set(parents))


def _node_kind(obj, shape):
    type_id = str(getattr(obj, "TypeId", ""))
    if type_id in {"App::Part", "App::DocumentObjectGroup"}:
        return "assembly"
    if shape is None or shape.isNull():
        return "reference"
    if len(shape.Solids) > 1:
        return "multi-solid-shape"
    return "shape-definition"


def _record_object(obj):
    shape = getattr(obj, "Shape", None)
    has_shape = shape is not None and not shape.isNull() and len(shape.Solids) > 0
    parent_ids = _parents(obj)
    child_objects = list(getattr(obj, "Group", []))
    has_shaped_children = any(
        getattr(child, "Shape", None) is not None
        and not child.Shape.isNull()
        and len(child.Shape.Solids) > 0
        for child in child_objects
    )
    record = {
        "source_id": obj.Name,
        "source_name": obj.Name,
        "source_label": str(getattr(obj, "Label", obj.Name)),
        "type_id": str(getattr(obj, "TypeId", "")),
        "parent_source_ids": parent_ids,
        "primary_parent_source_id": parent_ids[0] if parent_ids else None,
        "node_kind": _node_kind(obj, shape),
        "geometry_role": "assembly-aggregate" if has_shape and has_shaped_children else "leaf-shape",
        "has_shape": has_shape,
        "properties": {},
    }
    for name in getattr(obj, "PropertiesList", []):
        if name.lower() in {"partnumber", "stocknumber", "description", "material", "label2"} or name.startswith("Library"):
            try:
                value = getattr(obj, name)
                record["properties"][name] = str(value)
            except Exception:
                pass
    if not has_shape:
        return record, None
    if has_shaped_children:
        bbox = _bbox(shape)
        aggregate_signature = {
            "children": sorted(child.Name for child in child_objects),
            "bbox_mm": bbox,
            "solid_count": len(shape.Solids),
            "volume_mm3": round(float(shape.Volume), 9),
        }
        record.update(
            {
                "placement": _placement(obj),
                "bbox_mm": bbox,
                "volume_mm3": float(shape.Volume),
                "area_mm2": float(shape.Area),
                "center_of_mass_mm": _center_of_mass(shape),
                "inertia": {
                    "basis": "not-computed-for-assembly-aggregate",
                    "reason": "leaf shapes carry geometry and inertia descriptors",
                },
                "topology": {
                    "solid_count": len(shape.Solids),
                    "shell_count": len(shape.Shells),
                    "face_count": len(shape.Faces),
                    "edge_count": len(shape.Edges),
                    "vertex_count": len(shape.Vertexes),
                },
                "surface_type_counts": {},
                "cylindrical_face_candidates": [],
                "hole_axis_candidates": [],
                "planar_face_candidates": [],
                "candidate_axes": [],
                "aggregate_child_source_ids": sorted(child.Name for child in child_objects),
                "shape_sha256": hashlib.sha256(
                    json.dumps(aggregate_signature, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        )
        return record, {"object": obj, "shape": shape, "record": record, "fragments": []}
    counts, cylinders, planes, axes = _surface_features(shape)
    shape_hash = _shape_hash(shape)
    record.update(
        {
            "placement": _placement(obj),
            "bbox_mm": _bbox(shape),
            "volume_mm3": float(shape.Volume),
            "area_mm2": float(shape.Area),
            "center_of_mass_mm": _center_of_mass(shape),
            "inertia": _inertia(shape),
            "topology": {
                "solid_count": len(shape.Solids),
                "shell_count": len(shape.Shells),
                "face_count": len(shape.Faces),
                "edge_count": len(shape.Edges),
                "vertex_count": len(shape.Vertexes),
            },
            "surface_type_counts": counts,
            "cylindrical_face_candidates": cylinders,
            "hole_axis_candidates": cylinders,
            "planar_face_candidates": planes,
            "candidate_axes": axes,
            "shape_sha256": shape_hash,
        }
    )
    fragments = []
    for index, solid in enumerate(shape.Solids, start=1):
        fragments.append(
            {
                "fragment_id": f"{obj.Name}:solid:{index}",
                "source_id": obj.Name,
                "fragment_kind": "solid-fragment-candidate",
                "shape_sha256": _shape_hash(solid),
                "bbox_mm": _bbox(solid),
                "volume_mm3": float(solid.Volume),
                "area_mm2": float(solid.Area),
                "center_of_mass_mm": _vec(solid.CenterOfMass),
            }
        )
    return record, {"object": obj, "shape": shape, "record": record, "fragments": fragments}


def _bbox_gap(a, b):
    squared = 0.0
    for axis in range(3):
        gap = max(0.0, a["min"][axis] - b["max"][axis], b["min"][axis] - a["max"][axis])
        squared += gap * gap
    return math.sqrt(squared)


def _bbox_overlap_volume(a, b):
    extents = [max(0.0, min(a["max"][i], b["max"][i]) - max(a["min"][i], b["min"][i])) for i in range(3)]
    return extents[0] * extents[1] * extents[2]


def _contains(a, b, tolerance=0.02):
    return all(a["min"][i] - tolerance <= b["min"][i] and a["max"][i] + tolerance >= b["max"][i] for i in range(3))


def _axis_parallel(a, b, tolerance_deg=0.5):
    dot = abs(sum(a[i] * b[i] for i in range(3)))
    return dot >= math.cos(math.radians(tolerance_deg))


def _coaxial_candidate(left, right):
    for first in left.get("cylindrical_face_candidates", [])[:12]:
        for second in right.get("cylindrical_face_candidates", [])[:12]:
            if not _axis_parallel(first["axis"], second["axis"]):
                continue
            delta = [second["origin_mm"][i] - first["origin_mm"][i] for i in range(3)]
            axis = first["axis"]
            parallel = sum(delta[i] * axis[i] for i in range(3))
            perpendicular = [delta[i] - parallel * axis[i] for i in range(3)]
            distance = math.sqrt(sum(value * value for value in perpendicular))
            if distance <= DISTANCE_TOLERANCE_MM:
                return {
                    "axis": axis,
                    "axis_distance_mm": distance,
                    "diameters_mm": [first["diameter_mm"], second["diameter_mm"]],
                }
    return None


def _coplanar_candidate(left, right):
    for first in left.get("planar_face_candidates", [])[:16]:
        for second in right.get("planar_face_candidates", [])[:16]:
            if not _axis_parallel(first["normal"], second["normal"]):
                continue
            delta = [second["origin_mm"][i] - first["origin_mm"][i] for i in range(3)]
            offset = abs(sum(delta[i] * first["normal"][i] for i in range(3)))
            if offset <= DISTANCE_TOLERANCE_MM:
                return {"normal": first["normal"], "plane_offset_mm": offset}
    return None


def _relations(shape_records):
    relations = []
    pair_count = 0
    exact_pair_count = 0
    exact_truncated = False
    for left_index, left in enumerate(shape_records):
        for right in shape_records[left_index + 1 :]:
            if pair_count >= MAX_RELATION_PAIRS:
                return relations, True, exact_truncated
            pair_count += 1
            left_record = left["record"]
            right_record = right["record"]
            left_bbox = left_record["bbox_mm"]
            right_bbox = right_record["bbox_mm"]
            gap = _bbox_gap(left_bbox, right_bbox)
            bbox_overlap = _bbox_overlap_volume(left_bbox, right_bbox)
            relation = {
                "subject_source_id": left_record["source_id"],
                "object_source_id": right_record["source_id"],
                "bbox_gap_mm": gap,
                "bbox_overlap_mm3": bbox_overlap,
                "candidates": [],
                "basis": ["bounding-box"],
            }
            if _contains(left_bbox, right_bbox):
                relation["candidates"].append("contains")
            elif _contains(right_bbox, left_bbox):
                relation["candidates"].append("contained-by")
            coaxial = _coaxial_candidate(left_record, right_record)
            if coaxial:
                relation["candidates"].append("coaxial")
                relation["coaxial_evidence"] = coaxial
            coplanar = _coplanar_candidate(left_record, right_record)
            if coplanar:
                relation["candidates"].append("coplanar")
                relation["coplanar_evidence"] = coplanar
            if gap <= MAX_CLEARANCE_CANDIDATE_MM or bbox_overlap > 0:
                if exact_pair_count >= MAX_EXACT_RELATION_PAIRS:
                    exact_truncated = True
                    relation["exact_check_skipped"] = "maximum exact relation-pair budget reached"
                    if bbox_overlap > 0:
                        relation["candidates"].append("bbox-overlap")
                        relation["basis"].append("bounding-box-overlap-only")
                else:
                    exact_pair_count += 1
                    try:
                        distance, _, _ = left["shape"].distToShape(right["shape"])
                        relation["minimum_distance_mm"] = float(distance)
                        relation["basis"].append("exact-distance")
                        if distance <= DISTANCE_TOLERANCE_MM:
                            relation["candidates"].append("contact")
                        elif distance <= MAX_CLEARANCE_CANDIDATE_MM:
                            relation["candidates"].append("clearance")
                        if bbox_overlap > 0:
                            common = left["shape"].common(right["shape"])
                            common_volume = float(common.Volume) if not common.isNull() else 0.0
                            relation["interference_volume_mm3"] = common_volume
                            relation["basis"].append("boolean-common")
                            if common_volume > INTERFERENCE_TOLERANCE_MM3:
                                relation["candidates"].append("interference")
                    except Exception as exc:
                        relation["diagnostic"] = f"exact relation check failed: {type(exc).__name__}"
            relation["candidates"] = sorted(set(relation["candidates"]))
            if relation["candidates"]:
                relations.append(relation)
    return relations, False, exact_truncated


def _bounded_log(value, scale=20.0):
    return min(1.0, math.log1p(max(0.0, float(value))) / scale)


def _vectors(shape_records, relations, source_nodes):
    if not shape_records:
        return [0.0] * 64, [0.0] * 32
    boxes = [item["record"]["bbox_mm"] for item in shape_records]
    minimum = [min(box["min"][i] for box in boxes) for i in range(3)]
    maximum = [max(box["max"][i] for box in boxes) for i in range(3)]
    size = [maximum[i] - minimum[i] for i in range(3)]
    major = max(size) or 1.0
    surface_keys = [
        "Plane",
        "Cylinder",
        "Cone",
        "Sphere",
        "Toroid",
        "BSplineSurface",
        "SurfaceOfRevolution",
        "SurfaceOfExtrusion",
    ]
    surface_totals = {key: 0 for key in surface_keys}
    total_faces = 0
    total_solids = 0
    total_edges = 0
    total_volume = 0.0
    total_area = 0.0
    hash_counts = {}
    for item in shape_records:
        record = item["record"]
        total_faces += record["topology"]["face_count"]
        total_solids += record["topology"]["solid_count"]
        total_edges += record["topology"]["edge_count"]
        total_volume += record["volume_mm3"]
        total_area += record["area_mm2"]
        hash_counts[record["shape_sha256"]] = hash_counts.get(record["shape_sha256"], 0) + 1
        for raw_kind, count in record["surface_type_counts"].items():
            for key in surface_keys:
                if key.lower() in raw_kind.lower():
                    surface_totals[key] += count
                    break
    repeated_instances = sum(count for count in hash_counts.values() if count > 1)
    relation_counts = {}
    for relation in relations:
        for candidate in relation["candidates"]:
            relation_counts[candidate] = relation_counts.get(candidate, 0) + 1
    geometry = [value / major for value in size]
    geometry.extend(
        [
            _bounded_log(total_volume),
            _bounded_log(total_area),
            _bounded_log(len(shape_records), 8.0),
            _bounded_log(total_solids, 8.0),
            _bounded_log(total_faces, 12.0),
            _bounded_log(total_edges, 12.0),
            repeated_instances / max(1, len(shape_records)),
        ]
    )
    geometry.extend(surface_totals[key] / max(1, total_faces) for key in surface_keys)
    geometry.extend(
        _bounded_log(relation_counts.get(key, 0), 8.0)
        for key in ("contact", "interference", "coaxial", "contains", "contained-by")
    )
    while len(geometry) < 64:
        index = len(geometry)
        source = geometry[index % max(1, min(len(geometry), 23))]
        geometry.append(round((source * (index + 1) * 0.61803398875) % 1.0, 9))
    geometry = geometry[:64]

    roots = sum(1 for item in source_nodes if not item["primary_parent_source_id"])
    assemblies = sum(1 for item in source_nodes if item["node_kind"] == "assembly")
    multisolid = sum(1 for item in shape_records if item["record"]["node_kind"] == "multi-solid-shape")
    structure = [
        _bounded_log(len(shape_records), 8.0),
        roots / max(1, len(source_nodes)),
        assemblies / max(1, len(source_nodes)),
        multisolid / max(1, len(shape_records)),
        repeated_instances / max(1, len(shape_records)),
        _bounded_log(len(relations), 8.0),
    ]
    structure.extend(
        relation_counts.get(key, 0) / max(1, len(relations))
        for key in ("contact", "interference", "coaxial", "contains", "contained-by")
    )
    while len(structure) < 32:
        index = len(structure)
        source = structure[index % max(1, min(len(structure), 11))]
        structure.append(round((source * (index + 1) * 0.41421356237) % 1.0, 9))
    return geometry, structure[:32]


def extract(input_path, output_path):
    import FreeCAD as App
    import Import

    input_path = Path(input_path).expanduser().resolve(strict=True)
    output_path = Path(output_path).expanduser().resolve()
    source_hash_before = hashlib.sha256(input_path.read_bytes()).hexdigest()
    opened_here = True
    if input_path.suffix.lower() == ".fcstd":
        document = App.openDocument(str(input_path), hidden=True)
        source_kind = "FCStd"
    else:
        document = App.newDocument("MechanicalDesignImport")
        Import.insert(str(input_path), document.Name)
        document.recompute()
        source_kind = "STEP"
    try:
        source_nodes = []
        raw_shape_records = []
        shape_records = []
        aggregate_shape_records = []
        fragments = []
        diagnostics = []
        for obj in document.Objects:
            try:
                node, shaped = _record_object(obj)
                source_nodes.append(node)
                if shaped:
                    raw_shape_records.append(shaped)
                    if node["geometry_role"] == "assembly-aggregate":
                        aggregate_shape_records.append(shaped)
                    else:
                        shape_records.append(shaped)
                        fragments.extend(shaped["fragments"])
            except Exception as exc:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "object-extraction-failed",
                        "source_id": getattr(obj, "Name", "unknown"),
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
        repeated = {}
        for item in shape_records:
            repeated.setdefault(item["record"]["shape_sha256"], []).append(item["record"]["source_id"])
        repeated_groups = [
            {"shape_sha256": key, "source_ids": sorted(values), "count": len(values)}
            for key, values in sorted(repeated.items())
            if len(values) > 1
        ]
        relations, relations_truncated, exact_relations_truncated = _relations(shape_records)
        if relations_truncated:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "relation-pair-limit",
                    "message": f"relation analysis stopped after {MAX_RELATION_PAIRS} pairs",
                }
            )
        if exact_relations_truncated:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "exact-relation-pair-limit",
                    "message": f"exact distance/boolean analysis stopped after {MAX_EXACT_RELATION_PAIRS} pairs; "
                    "remaining bbox overlaps are retained as lower-confidence candidates",
                }
            )
        hypotheses = []
        for node in source_nodes:
            for parent in node["parent_source_ids"]:
                hypotheses.append(
                    {
                        "kind": "source-parent",
                        "subject_source_id": node["source_id"],
                        "object_source_id": parent,
                        "confidence": 1.0,
                        "status": "observed",
                        "evidence": [{"basis": "freecad-document-tree"}],
                    }
                )
        for group in repeated_groups:
            hypotheses.append(
                {
                    "kind": "repeated-shape-definition",
                    "subject_source_id": group["source_ids"][0],
                    "object_source_id": None,
                    "confidence": 0.98,
                    "status": "inferred_candidate",
                    "evidence": [group],
                }
            )
        for relation in relations:
            hypotheses.append(
                {
                    "kind": "spatial-relation-candidate",
                    "subject_source_id": relation["subject_source_id"],
                    "object_source_id": relation["object_source_id"],
                    "confidence": 0.85 if "exact-distance" in relation["basis"] else 0.55,
                    "status": "inferred_candidate",
                    "evidence": [relation],
                }
            )
        geometry_definitions = []
        occurrences = []
        seen_definitions = set()
        for item in shape_records:
            record = item["record"]
            definition_id = f"shape:{record['shape_sha256']}"
            if definition_id not in seen_definitions:
                geometry_definitions.append(
                    {
                        "definition_id": definition_id,
                        "shape_sha256": record["shape_sha256"],
                        "representative_source_id": record["source_id"],
                        "topology": record["topology"],
                        "bbox_size_mm": record["bbox_mm"]["size"],
                        "volume_mm3": record["volume_mm3"],
                        "area_mm2": record["area_mm2"],
                    }
                )
                seen_definitions.add(definition_id)
            occurrences.append(
                {
                    "occurrence_id": f"occurrence:{record['source_id']}",
                    "source_id": record["source_id"],
                    "definition_id": definition_id,
                    "parent_source_ids": record["parent_source_ids"],
                    "placement": record["placement"],
                }
            )
        geometry_vector, structure_vector = _vectors(shape_records, relations, source_nodes)
        manifest = {
            "schema_version": "ModelManifest/v2",
            "parser_version": PARSER_VERSION,
            "created_at": _utc_now(),
            "source": {
                "path": str(input_path),
                "sha256": source_hash_before,
                "kind": source_kind,
                "size_bytes": input_path.stat().st_size,
                "units": "mm",
            },
            "document": {"name": document.Name, "label": document.Label},
            "source_nodes": source_nodes,
            "shape_definitions": [item["record"] for item in shape_records],
            "assembly_aggregate_shapes": [item["record"] for item in aggregate_shape_records],
            "geometry_definitions": geometry_definitions,
            "occurrences": occurrences,
            "review_view_targets": [
                {
                    "source_id": item["record"]["source_id"],
                    "source_label": item["record"]["source_label"],
                    "bbox_mm": item["record"]["bbox_mm"],
                    "action": "select-highlight-and-isolate-via-existing-freecad-mcp",
                }
                for item in raw_shape_records
            ],
            "solid_fragments": fragments,
            "repeated_shape_groups": repeated_groups,
            "relation_candidates": relations,
            "structure_hypotheses": hypotheses,
            "geometry_vector": geometry_vector,
            "structure_vector": structure_vector,
            "diagnostics": diagnostics,
            "limits": {
                "distance_tolerance_mm": DISTANCE_TOLERANCE_MM,
                "interference_tolerance_mm3": INTERFERENCE_TOLERANCE_MM3,
                "maximum_clearance_candidate_mm": MAX_CLEARANCE_CANDIDATE_MM,
                "maximum_relation_pairs": MAX_RELATION_PAIRS,
                "maximum_exact_relation_pairs": MAX_EXACT_RELATION_PAIRS,
            },
        }
        source_hash_after = hashlib.sha256(input_path.read_bytes()).hexdigest()
        if source_hash_after != source_hash_before:
            raise RuntimeError("source CAD hash changed during read-only extraction")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, output_path)
        return manifest
    finally:
        if opened_here:
            App.closeDocument(document.Name)


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: extract_model_manifest.py INPUT OUTPUT")
    input_path, output_path = sys.argv[-2], sys.argv[-1]
    try:
        extract(input_path, output_path)
    except Exception:
        sys.stderr.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
