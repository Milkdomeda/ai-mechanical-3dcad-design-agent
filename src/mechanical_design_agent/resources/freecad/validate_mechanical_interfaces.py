"""Validate critical non-fastener assembly interfaces in the open FreeCAD model."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import FreeCAD as App
import FreeCADGui as Gui


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(report, check_id, passed, message, actual=None, expected=None):
    item = {
        "id": check_id,
        "validator": "freecad-mechanical-interface-validation",
        "mandatory": True,
        "status": "passed" if passed else "failed",
        "message": message,
    }
    if actual is not None:
        item["actual"] = actual
    if expected is not None:
        item["expected"] = expected
    report["checks"].append(item)


def _cylinder_axes(obj, radius_mm, radius_tolerance_mm, expected_direction):
    direction = App.Vector(*expected_direction)
    direction.normalize()
    candidates = []
    for face_index, face in enumerate(obj.Shape.Faces, 1):
        surface = face.Surface
        if not all(hasattr(surface, field) for field in ("Radius", "Axis", "Center")):
            continue
        if abs(float(surface.Radius) - radius_mm) > radius_tolerance_mm:
            continue
        axis = App.Vector(surface.Axis)
        axis.normalize()
        if abs(axis.dot(direction)) < 1.0 - 1e-6:
            continue
        candidates.append({"face": face_index, "radius": float(surface.Radius), "center": App.Vector(surface.Center), "axis": axis})
    return candidates


def _axis_line_offset(a_center, a_axis, b_center):
    return (b_center - a_center).cross(a_axis).Length


def validate_mechanical_interfaces(document_name, specification, report_dir):
    spec_path = Path(specification).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    output = Path(report_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    doc = App.getDocument(document_name)
    if doc is None:
        raise RuntimeError(f"FreeCAD document is not open: {document_name}")
    model_path = Path(doc.FileName).resolve()
    report = {
        "schema_version": "MechanicalInterfaceValidation/v1",
        "validator": "freecad-mechanical-interface-validation",
        "document": document_name,
        "source": str(model_path),
        "working_sha256": _sha256(model_path),
        "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "interfaces": [],
    }
    _check(report, "spec.schema", spec.get("schema_version") == "MechanicalInterfaceSpec/v1", "mechanical interface specification schema is supported", spec.get("schema_version"), "MechanicalInterfaceSpec/v1")
    _check(report, "model.hash", report["working_sha256"] == spec.get("working_sha256"), "specification matches current FCStd SHA-256", report["working_sha256"], spec.get("working_sha256"))

    for item in spec.get("axis_alignments", []):
        interface_id = str(item["id"])
        object_a = doc.getObject(str(item["a"]["object"]))
        object_b = doc.getObject(str(item["b"]["object"]))
        objects_exist = object_a is not None and object_b is not None
        _check(report, f"{interface_id}.objects", objects_exist, "axis-alignment objects exist")
        if not objects_exist:
            report["interfaces"].append({"id": interface_id, "status": "failed"})
            continue
        direction = item.get("expected_direction", [1, 0, 0])
        axes_a = _cylinder_axes(object_a, float(item["a"]["radius_mm"]), float(item["a"].get("radius_tolerance_mm", 0.01)), direction)
        axes_b = _cylinder_axes(object_b, float(item["b"]["radius_mm"]), float(item["b"].get("radius_tolerance_mm", 0.01)), direction)
        _check(report, f"{interface_id}.cylinders", bool(axes_a) and bool(axes_b), "declared cylindrical datum faces were found", {"a": len(axes_a), "b": len(axes_b)}, {"a_min": 1, "b_min": 1})
        if not axes_a or not axes_b:
            report["interfaces"].append({"id": interface_id, "status": "failed"})
            continue
        datum_a, datum_b = axes_a[0], axes_b[0]
        cosine = max(-1.0, min(1.0, abs(datum_a["axis"].dot(datum_b["axis"]))))
        angle = math.degrees(math.acos(cosine))
        offset = _axis_line_offset(datum_a["center"], datum_a["axis"], datum_b["center"])
        radial_clearance = float(item["a"]["radius_mm"]) - float(item["b"]["radius_mm"])
        max_angle = float(item.get("maximum_angle_deg", 0.1))
        max_offset = float(item.get("maximum_axis_offset_mm", 0.01))
        minimum_radial = float(item.get("minimum_radial_clearance_mm", 0.0))
        maximum_radial = float(item.get("maximum_radial_clearance_mm", 1.0))
        _check(report, f"{interface_id}.parallel", angle <= max_angle, "catalog support bore and screw journal axes are parallel", angle, {"maximum_deg": max_angle})
        _check(report, f"{interface_id}.concentric", offset <= max_offset, "catalog support bore and screw journal axes are concentric", offset, {"maximum_mm": max_offset})
        _check(report, f"{interface_id}.radial-clearance", minimum_radial <= radial_clearance <= maximum_radial, "support bore/journal radial clearance is within the declared range", radial_clearance, {"minimum_mm": minimum_radial, "maximum_mm": maximum_radial})
        report["interfaces"].append({"id": interface_id, "status": "passed" if angle <= max_angle and offset <= max_offset and minimum_radial <= radial_clearance <= maximum_radial else "failed", "axis_offset_mm": offset, "angle_deg": angle, "radial_clearance_mm": radial_clearance})

    for item in spec.get("spatial_interfaces", []):
        interface_id = str(item["id"])
        object_a = doc.getObject(str(item["a"]))
        object_b = doc.getObject(str(item["b"]))
        objects_exist = object_a is not None and object_b is not None
        _check(report, f"{interface_id}.objects", objects_exist, "spatial-interface objects exist")
        if not objects_exist:
            report["interfaces"].append({"id": interface_id, "status": "failed"})
            continue
        common = float(object_a.Shape.common(object_b.Shape).Volume)
        distance = float(object_a.Shape.distToShape(object_b.Shape)[0])
        maximum_common = float(item.get("maximum_common_volume_mm3", 0.01))
        minimum_distance = float(item.get("minimum_distance_mm", 0.0))
        maximum_distance = item.get("maximum_distance_mm")
        common_ok = common <= maximum_common
        minimum_ok = distance >= minimum_distance
        maximum_ok = maximum_distance is None or distance <= float(maximum_distance)
        _check(report, f"{interface_id}.common-volume", common_ok, "interface has no forbidden solid overlap", common, {"maximum_mm3": maximum_common})
        _check(report, f"{interface_id}.minimum-distance", minimum_ok, "interface meets the declared minimum clearance", distance, {"minimum_mm": minimum_distance})
        if maximum_distance is not None:
            _check(report, f"{interface_id}.maximum-distance", maximum_ok, "declared mating surfaces remain in contact/proximity", distance, {"maximum_mm": float(maximum_distance)})
        report["interfaces"].append({"id": interface_id, "status": "passed" if common_ok and minimum_ok and maximum_ok else "failed", "common_volume_mm3": common, "distance_mm": distance})

    failures = [item for item in report["checks"] if item["status"] == "failed"]
    report["summary"] = {"interfaces": len(report["interfaces"]), "checks": len(report["checks"]), "failed": len(failures), "passed": len(report["checks"]) - len(failures)}
    report["status"] = "passed" if not failures else "failed"
    json_path = output / "mechanical-interface-validation.json"
    md_path = output / "mechanical-interface-validation.md"
    png_path = output / "mechanical-interface-validation.png"
    report["artifacts"] = {"json": str(json_path), "markdown": str(md_path), "png": str(png_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Mechanical interface validation", "", f"Status: **{report['status']}**", "", f"Model SHA-256: `{report['working_sha256']}`", ""]
    for check in report["checks"]:
        lines.append(f"- **{check['status'].upper()}** `{check['id']}` — {check['message']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    Gui.activeDocument().activeView().viewAxonometric()
    Gui.activeDocument().activeView().fitAll()
    Gui.activeDocument().activeView().saveImage(str(png_path), 1600, 1000, "Current")
    return report
