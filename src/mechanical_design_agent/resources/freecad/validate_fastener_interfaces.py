"""FreeCAD-native validation of bolt/hole/thread interfaces.

This complements broad assembly interference checks. It classifies the
clearance body and threaded receiver separately so intended thread engagement
is not mistaken for an obstruction and head/shaft collisions are not ignored.
"""
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
        "validator": "freecad-fastener-interface-validation",
        "mandatory": True,
        "status": "passed" if passed else "failed",
        "message": message,
    }
    if actual is not None:
        item["actual"] = actual
    if expected is not None:
        item["expected"] = expected
    report["checks"].append(item)


def _linked_value(obj, property_name):
    linked = getattr(obj, "LinkedObject", None)
    if linked is None:
        return ""
    return str(getattr(linked, property_name, ""))


def validate_fastener_interfaces(document_name, specification, report_dir):
    spec_path = Path(specification).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    output = Path(report_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    doc = App.getDocument(document_name)
    if doc is None:
        raise RuntimeError(f"FreeCAD document is not open: {document_name}")
    model_path = Path(doc.FileName).resolve()
    report = {
        "schema_version": "FastenerInterfaceValidation/v1",
        "validator": "freecad-fastener-interface-validation",
        "document": document_name,
        "source": str(model_path),
        "working_sha256": _sha256(model_path),
        "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "interfaces": [],
    }
    _check(report, "spec.schema", spec.get("schema_version") == "FastenerInterfaceSpec/v1", "fastener interface specification schema is supported", spec.get("schema_version"), "FastenerInterfaceSpec/v1")
    _check(report, "model.hash", report["working_sha256"] == spec.get("working_sha256"), "specification matches current FCStd SHA-256", report["working_sha256"], spec.get("working_sha256"))

    for interface in spec.get("interfaces", []):
        interface_id = str(interface["id"])
        fastener_names = list(interface.get("fasteners", []))
        centers = list(interface.get("expected_centers", []))
        _check(report, f"{interface_id}.count", len(fastener_names) == len(centers) and bool(fastener_names), "fastener occurrence count matches declared hole centers", len(fastener_names), len(centers))
        expected_axis = App.Vector(*interface.get("axis", [0, 0, -1]))
        expected_axis.normalize()
        position_tolerance = float(interface.get("position_tolerance_mm", 0.01))
        angle_tolerance = float(interface.get("axis_tolerance_deg", 0.1))
        nominal = str(interface["nominal_diameter"])
        declared_length = float(interface["length_mm"])
        clearance_stack = float(interface["clearance_stack_mm"])
        thread_depth = float(interface["thread_depth_mm"])
        available_reach = declared_length - clearance_stack
        engagement = max(0.0, min(available_reach, thread_depth))
        through_thread = bool(interface.get("through_thread", False))
        bottom_clearance = None if through_thread else thread_depth - available_reach
        _check(report, f"{interface_id}.engagement", engagement >= float(interface["minimum_engagement_mm"]), "calculated thread engagement meets the declared minimum", engagement, {"minimum_mm": float(interface["minimum_engagement_mm"]), "clearance_stack_mm": clearance_stack})
        if not through_thread:
            _check(report, f"{interface_id}.bottom-clearance", bottom_clearance >= float(interface.get("minimum_bottom_clearance_mm", 0.0)), "blind threaded hole has declared bottom clearance", bottom_clearance, {"minimum_mm": float(interface.get("minimum_bottom_clearance_mm", 0.0))})

        clearance_parts = [doc.getObject(name) for name in interface.get("clearance_parts", [])]
        thread_part = doc.getObject(str(interface.get("threaded_part", "")))
        missing_parts = [name for name, obj in zip(interface.get("clearance_parts", []), clearance_parts) if obj is None]
        if thread_part is None:
            missing_parts.append(str(interface.get("threaded_part", "")))
        _check(report, f"{interface_id}.parts", not missing_parts, "clearance and threaded receiver objects exist", missing_parts, [])

        occurrence_results = []
        for index, fastener_name in enumerate(fastener_names):
            fastener = doc.getObject(fastener_name)
            if fastener is None:
                _check(report, f"{interface_id}.{index}.exists", False, f"fastener {fastener_name} exists")
                continue
            shape = getattr(fastener, "Shape", None)
            valid_shape = shape is not None and not shape.isNull() and shape.isValid()
            _check(report, f"{interface_id}.{index}.shape", valid_shape, f"{fastener_name} has valid modeled geometry")
            expected_center = App.Vector(*centers[index]) if index < len(centers) else App.Vector()
            actual_center = fastener.Placement.Base
            offset = (actual_center - expected_center).Length
            _check(report, f"{interface_id}.{index}.center", offset <= position_tolerance, f"{fastener_name} axis is concentric with the declared mounting hole", offset, {"maximum_mm": position_tolerance, "center": list(centers[index]) if index < len(centers) else []})
            actual_axis = fastener.Placement.Rotation.multVec(App.Vector(0, 0, -1))
            actual_axis.normalize()
            cosine = max(-1.0, min(1.0, actual_axis.dot(expected_axis)))
            angle = math.degrees(math.acos(cosine))
            _check(report, f"{interface_id}.{index}.axis", angle <= angle_tolerance, f"{fastener_name} axis matches the declared hole axis", angle, {"maximum_deg": angle_tolerance})
            _check(report, f"{interface_id}.{index}.diameter", _linked_value(fastener, "Diameter") == nominal, f"{fastener_name} nominal diameter matches the interface", _linked_value(fastener, "Diameter"), nominal)
            actual_length_text = _linked_value(fastener, "Length")
            try:
                actual_length = float(actual_length_text)
            except ValueError:
                actual_length = -1.0
            _check(report, f"{interface_id}.{index}.length", abs(actual_length - declared_length) <= 1e-9, f"{fastener_name} length matches the stack calculation", actual_length_text, declared_length)

            clearance_common = 0.0
            if valid_shape:
                for part in clearance_parts:
                    if part is not None:
                        clearance_common += float(shape.common(part.Shape).Volume)
            maximum_clearance_common = float(interface.get("clearance_max_common_volume_mm3", 0.01))
            _check(report, f"{interface_id}.{index}.clearance-collision", clearance_common <= maximum_clearance_common, f"{fastener_name} has no forbidden head/shaft overlap with its clearance body", clearance_common, {"maximum_mm3": maximum_clearance_common})

            thread_common = 0.0
            if valid_shape and thread_part is not None:
                thread_common = float(shape.common(thread_part.Shape).Volume)
            minimum_thread_common = float(interface.get("thread_common_volume_min_mm3", 0.01))
            maximum_thread_common = float(interface.get("thread_common_volume_max_mm3", 1000.0))
            _check(report, f"{interface_id}.{index}.thread-contact", minimum_thread_common <= thread_common <= maximum_thread_common, f"{fastener_name} reaches the modeled threaded receiver without excessive penetration", thread_common, {"minimum_mm3": minimum_thread_common, "maximum_mm3": maximum_thread_common})

            forbidden_common = 0.0
            for part_name in interface.get("forbidden_parts", []):
                part = doc.getObject(str(part_name))
                if valid_shape and part is not None:
                    forbidden_common += float(shape.common(part.Shape).Volume)
            maximum_forbidden = float(interface.get("forbidden_max_common_volume_mm3", 0.01))
            _check(report, f"{interface_id}.{index}.neighbor-clearance", forbidden_common <= maximum_forbidden, f"{fastener_name} clears declared neighboring obstructions", forbidden_common, {"maximum_mm3": maximum_forbidden})

            boundary = interface.get("seat_boundary")
            edge_distance = None
            if isinstance(boundary, dict):
                x, y = actual_center.x, actual_center.y
                edge_distance = min(x - float(boundary["xmin"]), float(boundary["xmax"]) - x, y - float(boundary["ymin"]), float(boundary["ymax"]) - y)
                minimum_edge = float(interface.get("minimum_edge_distance_mm", 0.0))
                _check(report, f"{interface_id}.{index}.edge-distance", edge_distance >= minimum_edge, f"{fastener_name} mounting center has the declared minimum plate-edge distance", edge_distance, {"minimum_mm": minimum_edge})
                if "clearance_hole_radius_mm" in interface:
                    clearance_ligament = edge_distance - float(interface["clearance_hole_radius_mm"])
                    minimum_clearance_ligament = float(interface.get("minimum_clearance_ligament_mm", 0.0))
                    _check(report, f"{interface_id}.{index}.clearance-ligament", clearance_ligament >= minimum_clearance_ligament, f"{fastener_name} clearance hole remains closed with the declared net edge ligament", clearance_ligament, {"minimum_mm": minimum_clearance_ligament})

            thread_edge_distance = None
            thread_ligament = None
            thread_boundaries = interface.get("thread_boundaries", [])
            if isinstance(thread_boundaries, list) and index < len(thread_boundaries) and isinstance(thread_boundaries[index], dict):
                thread_boundary = thread_boundaries[index]
                x, y = actual_center.x, actual_center.y
                thread_edge_distance = min(x - float(thread_boundary["xmin"]), float(thread_boundary["xmax"]) - x, y - float(thread_boundary["ymin"]), float(thread_boundary["ymax"]) - y)
                minimum_thread_edge = float(interface.get("minimum_thread_edge_distance_mm", 0.0))
                _check(report, f"{interface_id}.{index}.thread-edge-distance", thread_edge_distance >= minimum_thread_edge, f"{fastener_name} threaded receiver center has the declared material edge distance", thread_edge_distance, {"minimum_mm": minimum_thread_edge})
                if "thread_hole_radius_mm" in interface:
                    thread_ligament = thread_edge_distance - float(interface["thread_hole_radius_mm"])
                    minimum_thread_ligament = float(interface.get("minimum_thread_ligament_mm", 0.0))
                    _check(report, f"{interface_id}.{index}.thread-ligament", thread_ligament >= minimum_thread_ligament, f"{fastener_name} threaded receiver retains the declared net radial ligament", thread_ligament, {"minimum_mm": minimum_thread_ligament})

            occurrence_results.append({"fastener": fastener_name, "center_offset_mm": offset, "axis_error_deg": angle, "clearance_common_mm3": clearance_common, "thread_common_mm3": thread_common, "forbidden_common_mm3": forbidden_common, "edge_distance_mm": edge_distance, "thread_edge_distance_mm": thread_edge_distance, "thread_ligament_mm": thread_ligament})
        report["interfaces"].append({"id": interface_id, "joint_ids": interface.get("joint_ids", []), "engagement_mm": engagement, "bottom_clearance_mm": bottom_clearance, "occurrences": occurrence_results})

    failures = [item for item in report["checks"] if item["status"] == "failed"]
    report["summary"] = {"interfaces": len(report["interfaces"]), "checks": len(report["checks"]), "failed": len(failures), "passed": len(report["checks"]) - len(failures)}
    report["status"] = "passed" if not failures else "failed"
    json_path = output / "fastener-interface-validation.json"
    md_path = output / "fastener-interface-validation.md"
    png_path = output / "fastener-interface-validation.png"
    report["artifacts"] = {"json": str(json_path), "markdown": str(md_path), "png": str(png_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Fastener interface validation", "", f"Status: **{report['status']}**", "", f"Model SHA-256: `{report['working_sha256']}`", ""]
    for item in report["checks"]:
        lines.append(f"- **{item['status'].upper()}** `{item['id']}` — {item['message']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    Gui.activeDocument().activeView().viewAxonometric()
    Gui.activeDocument().activeView().fitAll()
    Gui.activeDocument().activeView().saveImage(str(png_path), 1600, 1000, "Current")
    return report
