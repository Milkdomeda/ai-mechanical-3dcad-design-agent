"""Strict FreeCAD-native validation for FCStd and STEP/STP artifacts.

Load this module inside the FreeCAD GUI process. It intentionally depends only on
FreeCAD, FreeCADGui, Import, Part, and the Python standard library.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path


METADATA_PROPERTIES = {
    "provider": "LibraryProvider",
    "category": "LibraryCategory",
    "standard": "LibraryStandard",
    "nominal_size": "LibraryNominalSize",
    "part_id": "LibraryPartId",
    "source_url": "LibrarySourceURL",
    "sha256": "LibrarySHA256",
    "source_commit": "LibrarySourceCommit",
}


# Mandatory installation limits for every detected fastener.  These are CAD
# validation limits, not manufacturing tolerances, and are intentionally fixed
# so a model cannot weaken the gate in its own specification.
FASTENER_AXIS_ANGULAR_TOLERANCE_DEG = 0.5
FASTENER_AXIS_OFFSET_TOLERANCE_MM = 0.25
FASTENER_CONTACT_TOLERANCE_MM = 0.05
FASTENER_COMMON_VOLUME_TOLERANCE_MM3 = 0.001
FASTENER_ROLES = {"bolt", "screw", "stud", "nut", "washer"}
FASTENER_PRIMARY_ROLES = {"bolt", "screw", "stud"}
FASTENER_CONNECTION_TYPES = {"through_bolt", "tapped"}
STEP_PROVENANCE_PROFILES = {
    "step_parts",
    "manufacturer_official",
    "verified_local_catalog",
}


def _load_mapping(value, label):
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    path = Path(value).expanduser().resolve()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return loaded


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_report(kind, source):
    return {
        "schema_version": 1,
        "validator": "freecad-model-validation",
        "engine": "FreeCAD Part/OCCT",
        "kind": kind,
        "source": str(Path(source).expanduser().resolve()),
        "working_sha256": None,
        "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [],
        "objects": [],
        "fastener_inventory": [],
        "artifacts": {},
    }


def _check(report, check_id, passed, message, actual=None, expected=None, mandatory=True):
    entry = {
        "id": str(check_id),
        "validator": "freecad-model-validation",
        "status": "passed" if passed else ("failed" if mandatory else "warning"),
        "mandatory": bool(mandatory),
        "message": str(message),
    }
    if actual is not None:
        entry["actual"] = actual
    if expected is not None:
        entry["expected"] = expected
    report["checks"].append(entry)
    return passed


def _shape_objects(document):
    result = []
    for obj in document.Objects:
        if str(getattr(obj, "TypeId", "")).startswith("App::"):
            continue
        shape = getattr(obj, "Shape", None)
        if shape is not None and not shape.isNull():
            result.append(obj)
    return result


def _bbox_dict(box):
    return {
        "min": [float(box.XMin), float(box.YMin), float(box.ZMin)],
        "max": [float(box.XMax), float(box.YMax), float(box.ZMax)],
        "size": [float(box.XLength), float(box.YLength), float(box.ZLength)],
    }


def _object_summary(obj):
    shape = getattr(obj, "Shape", None)
    summary = {
        "name": obj.Name,
        "label": obj.Label,
        "type_id": obj.TypeId,
        "state": [str(value) for value in getattr(obj, "State", [])],
        "placement": {},
        "library": {},
    }
    if hasattr(obj, "Placement"):
        summary["placement"] = {
            "base_mm": [
                float(obj.Placement.Base.x),
                float(obj.Placement.Base.y),
                float(obj.Placement.Base.z),
            ]
        }
    for key, prop in METADATA_PROPERTIES.items():
        if hasattr(obj, prop):
            summary["library"][key] = str(getattr(obj, prop))
    if shape is not None and not shape.isNull():
        summary.update(
            {
                "shape_valid": bool(shape.isValid()),
                "solid_count": len(shape.Solids),
                "volume_mm3": float(shape.Volume),
                "bbox_mm": _bbox_dict(shape.BoundBox),
            }
        )
    return summary


def _numeric(value):
    if hasattr(value, "Value"):
        return float(value.Value)
    return float(value)


def _metric_value(obj, metric):
    shape = getattr(obj, "Shape", None)
    if metric == "bbox_x":
        return float(shape.BoundBox.XLength)
    if metric == "bbox_y":
        return float(shape.BoundBox.YLength)
    if metric == "bbox_z":
        return float(shape.BoundBox.ZLength)
    if metric == "volume":
        return float(shape.Volume)
    if metric == "solid_count":
        return float(len(shape.Solids))
    if metric == "placement_x":
        return float(obj.Placement.Base.x)
    if metric == "placement_y":
        return float(obj.Placement.Base.y)
    if metric == "placement_z":
        return float(obj.Placement.Base.z)
    if metric.startswith("property:"):
        return _numeric(getattr(obj, metric.split(":", 1)[1]))
    raise ValueError(f"Unsupported metric: {metric}")


def _aggregate_shape(objects):
    shapes = [obj.Shape for obj in objects if hasattr(obj, "Shape") and not obj.Shape.isNull()]
    if not shapes:
        return None
    compound = shapes[0]
    if len(shapes) > 1:
        import Part

        compound = Part.makeCompound(shapes)
    return compound


class _AggregateObject:
    def __init__(self, shape):
        import FreeCAD as App

        self.Name = "__all__"
        self.Label = "All imported STEP objects"
        self.TypeId = "Part::Compound"
        self.State = []
        self.Shape = shape
        self.Placement = App.Placement()


def _objects_by_name(document, shape_objects):
    mapping = {obj.Name: obj for obj in document.Objects}
    aggregate = _aggregate_shape(shape_objects)
    if aggregate is not None:
        mapping["__all__"] = _AggregateObject(aggregate)
    return mapping


def _require_tolerance(report, item, check_id):
    if "tolerance" not in item:
        _check(report, check_id, False, "Numeric check has no declared tolerance")
        return None
    tolerance = float(item["tolerance"])
    if tolerance < 0:
        _check(report, check_id, False, "Tolerance must be non-negative", actual=tolerance)
        return None
    return tolerance


def _dimension_checks(report, specification, objects):
    for index, item in enumerate(specification.get("dimensions", [])):
        check_id = f"dimension.{index}"
        tolerance = _require_tolerance(report, item, check_id)
        if tolerance is None:
            continue
        object_name = str(item.get("object") or "")
        metric = str(item.get("metric") or "")
        obj = objects.get(object_name)
        if obj is None:
            _check(report, check_id, False, f"Dimension target {object_name!r} is missing")
            continue
        try:
            actual = _metric_value(obj, metric)
            expected = float(item["expected"])
            passed = math.isfinite(actual) and abs(actual - expected) <= tolerance
            _check(
                report,
                check_id,
                passed,
                f"{object_name}.{metric} within declared tolerance {tolerance}",
                actual=actual,
                expected=expected,
            )
        except Exception as exc:
            _check(report, check_id, False, f"Could not evaluate {object_name}.{metric}: {exc}")


def _placement_checks(report, specification, objects):
    for index, item in enumerate(specification.get("placements", [])):
        normalized = dict(item)
        axis = str(normalized.get("axis") or "").lower()
        normalized["metric"] = f"placement_{axis}"
        normalized["object"] = item.get("object")
        temp_spec = {"dimensions": [normalized]}
        before = len(report["checks"])
        _dimension_checks(report, temp_spec, objects)
        for check in report["checks"][before:]:
            check["id"] = f"placement.{index}"


def _standard_part_entries(specification):
    entries = list(specification.get("standard_parts", []))
    manifest_path = specification.get("standard_parts_manifest")
    if manifest_path:
        manifest = _load_mapping(manifest_path, "standard-parts manifest")
        manifest_entries = manifest.get("parts", [])
        if not isinstance(manifest_entries, list):
            raise ValueError("standard_parts_manifest.parts must be an array")
        entries.extend(manifest_entries)
    return entries


def _standard_part_checks(report, specification, objects):
    try:
        entries = _standard_part_entries(specification)
    except Exception as exc:
        _check(report, "standard-parts.manifest", False, f"Could not load standard-parts manifest: {exc}")
        return
    for index, expected in enumerate(entries):
        object_name = str(expected.get("object") or "")
        obj = objects.get(object_name)
        base_id = f"standard-part.{index}"
        if obj is None:
            _check(report, base_id, False, f"Required standard-part object {object_name!r} is missing")
            continue
        provider = str(getattr(obj, "LibraryProvider", ""))
        required = ["provider", "standard", "nominal_size", "source_url"]
        if provider == "STEP.parts":
            required.extend(["part_id", "sha256"])
        else:
            required.append("source_commit")
        for field in required:
            prop = METADATA_PROPERTIES[field]
            value = str(getattr(obj, prop, ""))
            _check(
                report,
                f"{base_id}.metadata.{field}",
                bool(value.strip()),
                f"{object_name} has {prop}",
                actual=value,
                expected="non-empty",
            )
        for field, expected_value in expected.items():
            if field == "object" or field not in METADATA_PROPERTIES:
                continue
            actual_value = str(getattr(obj, METADATA_PROPERTIES[field], ""))
            expected_text = str(expected_value)
            _check(
                report,
                f"{base_id}.match.{field}",
                actual_value == expected_text,
                f"{object_name} {field} matches manifest/BOM",
                actual=actual_value,
                expected=expected_text,
            )


def _name_list(value):
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _is_fastener(obj):
    provider = str(getattr(obj, "LibraryProvider", "")).lower()
    category = str(getattr(obj, "LibraryCategory", "")).lower()
    return (
        category == "fastener"
        or "fastener" in provider
        or hasattr(obj, "FastenerSetId")
        or hasattr(obj, "FastenerRole")
    )


def _unit_vector(vector):
    length = float(vector.Length)
    if length <= 0.0:
        raise ValueError("zero-length axis")
    return vector.multiply(1.0 / length)


def _axis_vector(obj):
    import FreeCAD as App

    token = str(getattr(obj, "FastenerAxisLocal", "")).strip().upper()
    axes = {
        "+X": App.Vector(1.0, 0.0, 0.0),
        "-X": App.Vector(-1.0, 0.0, 0.0),
        "+Y": App.Vector(0.0, 1.0, 0.0),
        "-Y": App.Vector(0.0, -1.0, 0.0),
        "+Z": App.Vector(0.0, 0.0, 1.0),
        "-Z": App.Vector(0.0, 0.0, -1.0),
    }
    if token not in axes:
        raise ValueError("FastenerAxisLocal must be one of +/-X, +/-Y, +/-Z")
    return _unit_vector(obj.Placement.Rotation.multVec(axes[token]))


def _axis_angle_deg(first, second):
    dot = max(-1.0, min(1.0, abs(_unit_vector(first).dot(_unit_vector(second)))))
    return math.degrees(math.acos(dot))


def _parallel_axis_offset_mm(first_point, first_axis, second_point, second_axis):
    first = _unit_vector(first_axis)
    second = _unit_vector(second_axis)
    cross = first.cross(second)
    delta = second_point.sub(first_point)
    if cross.Length <= 1.0e-12:
        return float(delta.cross(first).Length)
    return float(abs(delta.dot(_unit_vector(cross))))


def _bbox_projection_interval(box, axis):
    import FreeCAD as App

    unit = _unit_vector(axis)
    values = []
    for x in (box.XMin, box.XMax):
        for y in (box.YMin, box.YMax):
            for z in (box.ZMin, box.ZMax):
                values.append(float(App.Vector(x, y, z).dot(unit)))
    return min(values), max(values)


def _axial_bbox_overlap_mm(first, second, axis):
    first_min, first_max = _bbox_projection_interval(first.Shape.BoundBox, axis)
    second_min, second_max = _bbox_projection_interval(second.Shape.BoundBox, axis)
    return max(0.0, min(first_max, second_max) - max(first_min, second_min))


def _cylindrical_hole_candidates(host, point, axis, nominal_diameter_mm, threaded):
    candidates = []
    nominal_radius = 0.5 * float(nominal_diameter_mm)
    for face in host.Shape.Faces:
        surface = face.Surface
        if type(surface).__name__ != "Cylinder":
            continue
        radius = float(surface.Radius)
        if threaded:
            compatible = 0.25 * nominal_diameter_mm <= radius <= 0.75 * nominal_diameter_mm
        else:
            compatible = nominal_radius <= radius <= 1.5 * nominal_diameter_mm
        if not compatible:
            continue
        cylinder_axis = _unit_vector(surface.Axis)
        angle = _axis_angle_deg(axis, cylinder_axis)
        offset = _parallel_axis_offset_mm(point, axis, surface.Center, cylinder_axis)
        candidates.append(
            {
                "angle_deg": angle,
                "offset_mm": offset,
                "radius_mm": radius,
            }
        )
    candidates.sort(key=lambda item: (item["offset_mm"], item["angle_deg"]))
    return candidates


def _cached_pair_geometry(cache, first, second):
    key = tuple(sorted((first.Name, second.Name)))
    if key not in cache:
        cache[key] = {
            "common_volume_mm3": float(first.Shape.common(second.Shape).Volume),
            "distance_mm": float(first.Shape.distToShape(second.Shape)[0]),
        }
    return cache[key]


def _compressible_checks(report, specification, objects):
    compressible = {
        obj.Name: obj
        for obj in objects.values()
        if obj.Name != "__all__" and bool(getattr(obj, "IsCompressible", False))
    }
    _check(
        report,
        "compressible.coverage",
        True,
        "All objects marked compressible are subject to controlled-overlap validation",
        actual=len(compressible),
    )
    entries = specification.get("compressible_overlap_pairs", [])
    declared = {}
    for index, item in enumerate(entries):
        check_id = f"compressible-pair.{index}.contract"
        name = str(item.get("compressible") or "")
        mate = str(item.get("mate") or "")
        if "max_common_volume_mm3" not in item:
            _check(report, check_id, False, "Compressible overlap pair has no max_common_volume_mm3")
            continue
        limit = float(item["max_common_volume_mm3"])
        passed = bool(name and mate) and limit >= 0.0
        _check(
            report,
            check_id,
            passed,
            "Compressible overlap pair declares both objects and a non-negative limit",
            actual={"compressible": name, "mate": mate, "maximum": limit},
        )
        if passed:
            declared[(name, mate)] = limit

    for name, obj in compressible.items():
        allowed = _name_list(getattr(obj, "AllowedOverlapWith", ""))
        try:
            object_limit = float(getattr(obj, "MaxCompressionOverlapMM3"))
        except Exception:
            object_limit = -1.0
        covered = {mate for source, mate in declared if source == name}
        contract_passed = bool(allowed) and object_limit >= 0.0 and covered == set(allowed)
        _check(
            report,
            f"compressible.{name}.contract",
            contract_passed,
            f"{name} declares its allowed mates, numeric overlap limit, and complete specification coverage",
            actual={
                "allowed_mates": allowed,
                "object_maximum_mm3": object_limit,
                "specified_mates": sorted(covered),
            },
            expected="exact mate coverage and a non-negative limit",
        )
        for mate_name in allowed:
            mate = objects.get(mate_name)
            pair_limit = declared.get((name, mate_name))
            check_id = f"compressible.{name}.overlap.{mate_name}"
            if mate is None or pair_limit is None:
                _check(report, check_id, False, f"Allowed compressible mate {mate_name!r} is missing or unspecified")
                continue
            if pair_limit > object_limit:
                _check(
                    report,
                    check_id,
                    False,
                    "Pair limit exceeds the compressible object's maximum",
                    actual=pair_limit,
                    expected={"maximum": object_limit},
                )
                continue
            try:
                common_volume = float(obj.Shape.common(mate.Shape).Volume)
                _check(
                    report,
                    check_id,
                    common_volume <= pair_limit,
                    f"Controlled compressible overlap for {name}/{mate_name} is within its declared limit",
                    actual=common_volume,
                    expected={"maximum": pair_limit},
                )
            except Exception as exc:
                _check(report, check_id, False, f"Compressible overlap check failed to execute: {exc}")


def _fastener_checks(report, objects):
    fasteners = [
        obj for obj in objects.values()
        if obj.Name != "__all__" and _is_fastener(obj)
    ]
    _check(
        report,
        "fastener.coverage",
        True,
        "Every detected fastener is subject to mandatory installation validation",
        actual=len(fasteners),
    )
    groups = {}
    contracts = {}
    for obj in fasteners:
        set_id = str(getattr(obj, "FastenerSetId", "")).strip()
        role = str(getattr(obj, "FastenerRole", "")).strip().lower()
        connection = str(getattr(obj, "FastenerConnectionType", "")).strip().lower()
        axis_token = str(getattr(obj, "FastenerAxisLocal", "")).strip().upper()
        hosts = _name_list(getattr(obj, "FastenerHostObjects", ""))
        contacts = _name_list(getattr(obj, "FastenerContactObjects", ""))
        clearance = _name_list(getattr(obj, "FastenerClearanceObjects", ""))
        threaded = _name_list(getattr(obj, "FastenerThreadedObjects", ""))
        try:
            diameter = float(getattr(obj, "FastenerNominalDiameterMM"))
        except Exception:
            diameter = 0.0
        contract_passed = (
            bool(set_id)
            and role in FASTENER_ROLES
            and connection in FASTENER_CONNECTION_TYPES
            and diameter > 0.0
            and axis_token in {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}
            and bool(hosts)
            and bool(contacts)
            and all(name in objects for name in hosts + contacts + clearance + threaded)
            and not (set(clearance) & set(threaded))
        )
        if role in FASTENER_PRIMARY_ROLES:
            if connection == "through_bolt":
                contract_passed = contract_passed and bool(clearance) and not threaded
            elif connection == "tapped":
                contract_passed = contract_passed and bool(threaded)
        _check(
            report,
            f"fastener.{obj.Name}.contract",
            contract_passed,
            f"{obj.Name} declares a complete mandatory fastener installation contract",
            actual={
                "set_id": set_id,
                "role": role,
                "connection_type": connection,
                "nominal_diameter_mm": diameter,
                "axis_local": axis_token,
                "hosts": hosts,
                "contacts": contacts,
                "clearance_hosts": clearance,
                "threaded_hosts": threaded,
            },
        )
        report["fastener_inventory"].append({
            "object_id": obj.Name,
            "set_id": set_id,
            "role": role,
            "connection_type": connection,
            "nominal_diameter_mm": diameter,
            "axis_local": axis_token,
            "hosts": hosts,
            "contacts": contacts,
            "clearance_hosts": clearance,
            "threaded_hosts": threaded,
            "contract_check_id": f"fastener.{obj.Name}.contract",
        })
        contract = {
            "object": obj,
            "set_id": set_id,
            "role": role,
            "connection": connection,
            "diameter": diameter,
            "hosts": hosts,
            "contacts": contacts,
            "clearance": clearance,
            "threaded": threaded,
            "valid": contract_passed,
        }
        contracts[obj.Name] = contract
        if set_id:
            groups.setdefault(set_id, []).append(contract)

    geometry_cache = {}
    for set_id, members in sorted(groups.items()):
        primary = [item for item in members if item["role"] in FASTENER_PRIMARY_ROLES]
        connections = {item["connection"] for item in members}
        diameters = {round(item["diameter"], 9) for item in members}
        through_nuts = sum(item["role"] == "nut" for item in members)
        assembly_passed = (
            all(item["valid"] for item in members)
            and len(primary) == 1
            and len(connections) == 1
            and len(diameters) == 1
            and (
                (connections == {"through_bolt"} and through_nuts >= 1)
                or connections == {"tapped"}
            )
        )
        _check(
            report,
            f"fastener-set.{set_id}.assembly",
            assembly_passed,
            f"Fastener set {set_id} has one driver, compatible members, and a valid connection combination",
            actual={
                "members": [item["object"].Name for item in members],
                "primary_count": len(primary),
                "connections": sorted(connections),
                "nominal_diameters_mm": sorted(diameters),
                "nut_count": through_nuts,
            },
        )
        if len(primary) != 1:
            continue
        driver = primary[0]
        driver_obj = driver["object"]
        try:
            driver_axis = _axis_vector(driver_obj)
        except Exception as exc:
            _check(report, f"fastener-set.{set_id}.axis", False, f"Could not resolve primary axis: {exc}")
            continue

        for member in members:
            member_obj = member["object"]
            try:
                member_axis = _axis_vector(member_obj)
                angle = _axis_angle_deg(driver_axis, member_axis)
                offset = _parallel_axis_offset_mm(
                    driver_obj.Placement.Base,
                    driver_axis,
                    member_obj.Placement.Base,
                    member_axis,
                )
                passed = (
                    angle <= FASTENER_AXIS_ANGULAR_TOLERANCE_DEG
                    and offset <= FASTENER_AXIS_OFFSET_TOLERANCE_MM
                )
                _check(
                    report,
                    f"fastener-set.{set_id}.axis.{member_obj.Name}",
                    passed,
                    f"{member_obj.Name} is coaxial with the set primary fastener",
                    actual={"angle_deg": angle, "offset_mm": offset},
                    expected={
                        "maximum_angle_deg": FASTENER_AXIS_ANGULAR_TOLERANCE_DEG,
                        "maximum_offset_mm": FASTENER_AXIS_OFFSET_TOLERANCE_MM,
                    },
                )
            except Exception as exc:
                _check(
                    report,
                    f"fastener-set.{set_id}.axis.{member_obj.Name}",
                    False,
                    f"Could not evaluate fastener member axis: {exc}",
                )

        for member in members:
            if member is driver:
                continue
            member_obj = member["object"]
            threaded_member = member["role"] == "nut"
            candidates = _cylindrical_hole_candidates(
                member_obj,
                driver_obj.Placement.Base,
                driver_axis,
                driver["diameter"],
                threaded_member,
            )
            best = candidates[0] if candidates else None
            overlap = _axial_bbox_overlap_mm(driver_obj, member_obj, driver_axis)
            passed = (
                best is not None
                and best["angle_deg"] <= FASTENER_AXIS_ANGULAR_TOLERANCE_DEG
                and best["offset_mm"] <= FASTENER_AXIS_OFFSET_TOLERANCE_MM
                and overlap > 0.0
            )
            relation = "thread" if threaded_member else "hole"
            _check(
                report,
                f"fastener-set.{set_id}.member.{member_obj.Name}.{relation}",
                passed,
                f"Primary fastener is axially contained by {member_obj.Name}'s {relation}",
                actual={"best_cylinder": best, "axial_overlap_mm": overlap},
                expected={
                    "maximum_angle_deg": FASTENER_AXIS_ANGULAR_TOLERANCE_DEG,
                    "maximum_offset_mm": FASTENER_AXIS_OFFSET_TOLERANCE_MM,
                    "minimum_axial_overlap_mm_exclusive": 0.0,
                },
            )

        for host_name in driver["clearance"]:
            host = objects.get(host_name)
            if host is None:
                continue
            overlap = _axial_bbox_overlap_mm(driver_obj, host, driver_axis)
            _check(
                report,
                f"fastener.{driver_obj.Name}.host.{host_name}.position",
                overlap > 0.0,
                f"{driver_obj.Name} spans the axial extent of clearance host {host_name}",
                actual=overlap,
                expected={"minimum_exclusive": 0.0},
            )
            candidates = _cylindrical_hole_candidates(
                host,
                driver_obj.Placement.Base,
                driver_axis,
                driver["diameter"],
                False,
            )
            best = candidates[0] if candidates else None
            axis_passed = (
                best is not None
                and best["angle_deg"] <= FASTENER_AXIS_ANGULAR_TOLERANCE_DEG
                and best["offset_mm"] <= FASTENER_AXIS_OFFSET_TOLERANCE_MM
            )
            _check(
                report,
                f"fastener.{driver_obj.Name}.hole.{host_name}.axis",
                axis_passed,
                f"{driver_obj.Name} is coaxial with a compatible clearance hole in {host_name}",
                actual=best,
                expected={
                    "minimum_hole_radius_mm": 0.5 * driver["diameter"],
                    "maximum_angle_deg": FASTENER_AXIS_ANGULAR_TOLERANCE_DEG,
                    "maximum_offset_mm": FASTENER_AXIS_OFFSET_TOLERANCE_MM,
                },
            )
            geometry = _cached_pair_geometry(geometry_cache, driver_obj, host)
            _check(
                report,
                f"fastener.{driver_obj.Name}.hole.{host_name}.containment",
                geometry["common_volume_mm3"] <= FASTENER_COMMON_VOLUME_TOLERANCE_MM3,
                f"{driver_obj.Name} occupies the clearance void rather than {host_name} solid material",
                actual=geometry["common_volume_mm3"],
                expected={"maximum": FASTENER_COMMON_VOLUME_TOLERANCE_MM3},
            )

        for host_name in driver["threaded"]:
            host = objects.get(host_name)
            if host is None:
                continue
            overlap = _axial_bbox_overlap_mm(driver_obj, host, driver_axis)
            _check(
                report,
                f"fastener.{driver_obj.Name}.host.{host_name}.position",
                overlap > 0.0,
                f"{driver_obj.Name} spans the axial extent of threaded host {host_name}",
                actual=overlap,
                expected={"minimum_exclusive": 0.0},
            )
            candidates = _cylindrical_hole_candidates(
                host,
                driver_obj.Placement.Base,
                driver_axis,
                driver["diameter"],
                True,
            )
            best = candidates[0] if candidates else None
            passed = (
                best is not None
                and best["angle_deg"] <= FASTENER_AXIS_ANGULAR_TOLERANCE_DEG
                and best["offset_mm"] <= FASTENER_AXIS_OFFSET_TOLERANCE_MM
            )
            _check(
                report,
                f"fastener.{driver_obj.Name}.thread.{host_name}.axis",
                passed,
                f"{driver_obj.Name} is coaxial with a compatible threaded hole in {host_name}",
                actual=best,
                expected={
                    "maximum_angle_deg": FASTENER_AXIS_ANGULAR_TOLERANCE_DEG,
                    "maximum_offset_mm": FASTENER_AXIS_OFFSET_TOLERANCE_MM,
                },
            )

    for contract in contracts.values():
        obj = contract["object"]
        for host_name in contract["hosts"]:
            host = objects.get(host_name)
            check_id = f"fastener.{obj.Name}.host.{host_name}.interference"
            if host is None:
                _check(report, check_id, False, f"Fastener host {host_name!r} is missing")
                continue
            if contract["role"] in FASTENER_PRIMARY_ROLES and host_name in contract["threaded"]:
                _check(
                    report,
                    check_id,
                    True,
                    f"{obj.Name}/{host_name} overlap is explicitly classified as thread engagement",
                    actual="thread engagement",
                )
                continue
            geometry = _cached_pair_geometry(geometry_cache, obj, host)
            _check(
                report,
                check_id,
                geometry["common_volume_mm3"] <= FASTENER_COMMON_VOLUME_TOLERANCE_MM3,
                f"{obj.Name} has no unintended solid interference with host {host_name}",
                actual=geometry["common_volume_mm3"],
                expected={"maximum": FASTENER_COMMON_VOLUME_TOLERANCE_MM3},
            )
        for contact_name in contract["contacts"]:
            contact = objects.get(contact_name)
            check_id = f"fastener.{obj.Name}.contact.{contact_name}"
            if contact is None:
                _check(report, check_id, False, f"Fastener contact object {contact_name!r} is missing")
                continue
            geometry = _cached_pair_geometry(geometry_cache, obj, contact)
            passed = (
                geometry["common_volume_mm3"] <= FASTENER_COMMON_VOLUME_TOLERANCE_MM3
                and geometry["distance_mm"] <= FASTENER_CONTACT_TOLERANCE_MM
            )
            _check(
                report,
                check_id,
                passed,
                f"{obj.Name} bears on {contact_name} without unintended solid overlap",
                actual=geometry,
                expected={
                    "maximum_common_volume_mm3": FASTENER_COMMON_VOLUME_TOLERANCE_MM3,
                    "maximum_distance_mm": FASTENER_CONTACT_TOLERANCE_MM,
                },
            )


def _interference_checks(report, specification, objects):
    for index, item in enumerate(specification.get("interference_pairs", [])):
        check_id = f"interference.{index}"
        if "max_common_volume_mm3" not in item:
            _check(report, check_id, False, "Declared interference pair has no max_common_volume_mm3")
            continue
        a_name, b_name = str(item.get("a") or ""), str(item.get("b") or "")
        a, b = objects.get(a_name), objects.get(b_name)
        if a is None or b is None:
            _check(report, check_id, False, f"Interference objects are missing: {a_name!r}, {b_name!r}")
            continue
        if bool(getattr(a, "IsCompressible", False)) or bool(
            getattr(b, "IsCompressible", False)
        ):
            _check(
                report,
                check_id,
                False,
                "Compressible parts must use compressible_overlap_pairs, not ordinary interference_pairs",
            )
            continue
        limit = float(item["max_common_volume_mm3"])
        if limit < 0:
            _check(report, check_id, False, "max_common_volume_mm3 must be non-negative", actual=limit)
            continue
        try:
            common_volume = float(a.Shape.common(b.Shape).Volume)
            _check(
                report,
                check_id,
                common_volume <= limit,
                f"Common volume for {a_name}/{b_name} within declared limit",
                actual=common_volume,
                expected={"maximum": limit},
            )
        except Exception as exc:
            _check(report, check_id, False, f"Interference check failed to execute: {exc}")


def _document_checks(report, document, specification):
    try:
        recompute_result = document.recompute()
        _check(report, "document.recompute", True, "Document recomputed", actual=bool(recompute_result))
    except Exception as exc:
        _check(report, "document.recompute", False, f"Document recompute failed: {exc}")
    shape_objects = _shape_objects(document)
    _check(report, "document.geometry", bool(shape_objects), "Document contains shape-bearing objects", actual=len(shape_objects), expected={"minimum": 1})
    objects = _objects_by_name(document, shape_objects)
    for obj in document.Objects:
        state = [str(value).lower() for value in getattr(obj, "State", [])]
        bad = [value for value in state if "invalid" in value or "error" in value]
        _check(report, f"object.{obj.Name}.state", not bad, f"{obj.Name} has no invalid/error state", actual=state)
    for obj in shape_objects:
        shape = obj.Shape
        valid = bool(shape.isValid())
        solid_count = len(shape.Solids)
        volume = float(shape.Volume)
        _check(report, f"object.{obj.Name}.shape", valid, f"{obj.Name} shape is valid", actual=valid, expected=True)
        _check(report, f"object.{obj.Name}.solids", solid_count > 0, f"{obj.Name} contains at least one solid", actual=solid_count, expected={"minimum": 1})
        _check(report, f"object.{obj.Name}.volume", volume > 0, f"{obj.Name} has positive volume", actual=volume, expected={"minimum_exclusive": 0})
        report["objects"].append(_object_summary(obj))
    for name in specification.get("required_objects", []):
        _check(report, f"required-object.{name}", str(name) in objects, f"Required object {name!r} exists")
    for index, item in enumerate(specification.get("required_links", [])):
        obj = objects.get(str(item.get("object") or ""))
        prop = str(item.get("property") or "")
        value = getattr(obj, prop, None) if obj is not None else None
        populated = value is not None and value != [] and value != ()
        _check(report, f"required-link.{index}", populated, f"Required link {item.get('object')}.{prop} is populated")
    _dimension_checks(report, specification, objects)
    _placement_checks(report, specification, objects)
    _standard_part_checks(report, specification, objects)
    _compressible_checks(report, specification, objects)
    _fastener_checks(report, objects)
    _interference_checks(report, specification, objects)
    return shape_objects


def _save_snapshot(report, document, report_dir, stem):
    output_dir = Path(report_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"{stem}_validation.png"
    snapshot_document = None
    source_name = document.Name
    try:
        import FreeCAD as App
        import FreeCADGui as Gui

        snapshot_document = App.newDocument(f"ValidationSnapshot_{uuid.uuid4().hex[:10]}")
        cloned = 0
        for source_obj in _shape_objects(document):
            clone = snapshot_document.addObject("Part::Feature", f"Snapshot_{source_obj.Name}")
            clone.Shape = source_obj.Shape.copy()
            if hasattr(source_obj, "ViewObject"):
                clone.ViewObject.ShapeColor = source_obj.ViewObject.ShapeColor
                clone.ViewObject.LineColor = source_obj.ViewObject.LineColor
                clone.ViewObject.Transparency = source_obj.ViewObject.Transparency
            cloned += 1
        if not cloned:
            raise ValueError("No shape-bearing objects are available for the snapshot")
        snapshot_document.recompute()
        App.setActiveDocument(snapshot_document.Name)
        view = Gui.getDocument(snapshot_document.Name).activeView()
        view.viewAxonometric()
        view.fitAll()
        view.redraw()
        Gui.updateGui()
        time.sleep(0.3)
        Gui.updateGui()
        if image_path.exists():
            image_path.unlink()
        view.saveImage(str(image_path), 1200, 900, "Current")
        ok = image_path.is_file() and image_path.stat().st_size > 0
        _check(report, "visual.snapshot", ok, "FreeCAD validation snapshot saved", actual=str(image_path))
    except Exception as exc:
        _check(report, "visual.snapshot", False, f"Could not save FreeCAD snapshot: {exc}")
    finally:
        try:
            if snapshot_document is not None:
                App.closeDocument(snapshot_document.Name)
            if source_name in App.listDocuments():
                App.setActiveDocument(source_name)
                Gui.updateGui()
        except Exception:
            pass
    report["artifacts"]["png"] = str(image_path)


def _write_reports(report, report_dir, stem):
    output_dir = Path(report_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report["fastener_inventory"] = sorted(
        report.get("fastener_inventory", []),
        key=lambda item: item["object_id"],
    )
    failures = [item for item in report["checks"] if item["mandatory"] and item["status"] == "failed"]
    warnings = [item for item in report["checks"] if item["status"] == "warning"]
    report["summary"] = {
        "passed": len(report["checks"]) - len(failures) - len(warnings),
        "failed": len(failures),
        "warnings": len(warnings),
        "total": len(report["checks"]),
        "fasteners_detected": len(report["fastener_inventory"]),
    }
    report["status"] = "passed" if not failures else "failed"
    json_path = output_dir / f"{stem}_validation.json"
    markdown_path = output_dir / f"{stem}_validation.md"
    report["artifacts"]["json"] = str(json_path)
    report["artifacts"]["markdown"] = str(markdown_path)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"# Validation: {stem}",
        "",
        f"- Status: **{report['status']}**",
        f"- Engine: {report['engine']}",
        f"- Source: `{report['source']}`",
        f"- Checks: {report['summary']['passed']} passed, {report['summary']['failed']} failed, {report['summary']['warnings']} warnings",
        "",
        "## Checks",
        "",
    ]
    for item in report["checks"]:
        mark = "PASS" if item["status"] == "passed" else item["status"].upper()
        lines.append(f"- **{mark}** `{item['id']}` — {item['message']}")
    lines.extend(["", "Visual review of the PNG remains mandatory before handoff.", ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report


def _find_open_document(App, model_path):
    normalized = os.path.normcase(os.path.realpath(str(model_path)))
    for document in App.listDocuments().values():
        filename = getattr(document, "FileName", "")
        if filename and os.path.normcase(os.path.realpath(filename)) == normalized:
            return document
    return None


def validate_fcstd(model_path, specification, report_dir):
    """Validate an FCStd source-of-truth document and write strict evidence."""
    import FreeCAD as App

    model = Path(model_path).expanduser().resolve()
    spec = _load_mapping(specification, "validation specification")
    report = _new_report("FCStd", model)
    if model.is_file():
        report["working_sha256"] = _sha256(model)
    stem = model.stem
    document = None
    opened_here = False
    try:
        _check(report, "file.exists", model.is_file(), "FCStd file exists", actual=str(model))
        if not model.is_file():
            return _write_reports(report, report_dir, stem)
        document = _find_open_document(App, model)
        if document is None:
            document = App.openDocument(str(model))
            opened_here = True
        _check(report, "document.open", document is not None, "FCStd document opened")
        _document_checks(report, document, spec)
        _save_snapshot(report, document, report_dir, stem)
    except Exception as exc:
        _check(report, "validator.exception", False, f"FCStd validation aborted: {exc}")
    result = _write_reports(report, report_dir, stem)
    if opened_here and document is not None:
        App.closeDocument(document.Name)
    return result


def _expected_manifest_sha(manifest):
    record = manifest.get("part") or manifest.get("record") or {}
    return str(manifest.get("sha256") or (record.get("sha256") if isinstance(record, dict) else "") or "").lower()


def _normalized_step_provenance_profile(manifest):
    aliases = {
        "step.parts": "step_parts",
        "step_parts": "step_parts",
        "manufacturer_official": "manufacturer_official",
        "verified_local_catalog": "verified_local_catalog",
    }
    declared_profile = str(manifest.get("provenance_profile") or "").strip()
    declared_source_class = str(manifest.get("source_class") or "").strip()
    if not declared_profile and not declared_source_class:
        if str(manifest.get("provider") or "").strip() == "STEP.parts":
            return "step_parts", declared_profile, declared_source_class
        return "", declared_profile, declared_source_class
    profile = aliases.get(declared_profile.lower(), "")
    source_class = aliases.get(declared_source_class.lower(), "")
    if not declared_profile:
        profile = source_class
    return profile, declared_profile, declared_source_class


def _step_designation(manifest, record):
    designation = record.get("standard") or ""
    if isinstance(designation, dict):
        designation = designation.get("designation") or designation.get("name") or ""
    attributes = record.get("attributes") or {}
    if not isinstance(attributes, dict):
        attributes = {}
    return str(
        designation
        or attributes.get("nominalSize")
        or attributes.get("designation")
        or manifest.get("standard")
        or manifest.get("nominal_size")
        or record.get("name")
        or ""
    ).strip()


def _valid_sha256(value):
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _step_manifest_checks(report, manifest):
    record = manifest.get("part") or manifest.get("record") or {}
    if not isinstance(record, dict):
        _check(report, "manifest.record", False, "STEP manifest part record is not an object")
        return
    provider = str(manifest.get("provider") or "").strip()
    manufacturer = str(manifest.get("manufacturer") or "").strip()
    manifest_id = str(manifest.get("part_id") or "").strip()
    record_id = str(record.get("id") or "").strip()
    source_url = str(
        manifest.get("source_url")
        or record.get("pageUrl")
        or record.get("apiUrl")
        or ""
    ).strip()
    asset_url = str(
        manifest.get("asset_url")
        or manifest.get("step_url")
        or record.get("stepUrl")
        or ""
    ).strip()
    designation = _step_designation(manifest, record)
    profile, declared_profile, declared_source_class = _normalized_step_provenance_profile(manifest)
    raw_profile = declared_profile or declared_source_class or (
        "STEP.parts legacy inference" if profile == "step_parts" else ""
    )
    _check(
        report,
        "manifest.provenance-profile",
        profile in STEP_PROVENANCE_PROFILES,
        "Manifest declares a recognized STEP provenance profile",
        actual=raw_profile,
        expected=sorted(STEP_PROVENANCE_PROFILES),
    )
    if declared_source_class:
        normalized_source_class, _, _ = _normalized_step_provenance_profile(
            {"provenance_profile": declared_source_class}
        )
        _check(
            report,
            "manifest.source-class",
            bool(normalized_source_class) and normalized_source_class == profile,
            "Manifest source_class matches provenance_profile",
            actual=declared_source_class,
            expected=profile,
        )
    if profile == "step_parts":
        _check(
            report,
            "manifest.provider",
            provider == "STEP.parts",
            "STEP.parts manifest provider is exact",
            actual=provider,
            expected="STEP.parts",
        )
    else:
        _check(
            report,
            "manifest.provider",
            bool(provider),
            "Non-STEP.parts manifest declares a truthful provider",
            actual=provider,
            expected="non-empty",
        )
    if profile in {"manufacturer_official", "verified_local_catalog"}:
        _check(
            report,
            "manifest.manufacturer",
            bool(manufacturer),
            "Non-STEP.parts manifest declares the manufacturer",
            actual=manufacturer,
            expected="non-empty",
        )
    _check(report, "manifest.part-id", bool(manifest_id), "Manifest declares a part ID", actual=manifest_id, expected="non-empty")
    _check(report, "manifest.record-id", bool(record_id) and record_id == manifest_id, "Manifest part ID matches API record", actual=record_id, expected=manifest_id)
    _check(report, "manifest.source", bool(source_url), "Manifest declares a source URL", actual=source_url, expected="non-empty")
    if profile in {"manufacturer_official", "verified_local_catalog"}:
        _check(
            report,
            "manifest.asset-source",
            bool(asset_url),
            "Non-STEP.parts manifest declares the source STEP asset URL",
            actual=asset_url,
            expected="non-empty",
        )
    _check(report, "manifest.designation", bool(designation), "Catalog record has a standard or nominal designation", actual=designation, expected="non-empty")
    if profile == "verified_local_catalog":
        expected_sha = _expected_manifest_sha(manifest)
        source_identity = manifest.get("source_identity") or {}
        copy_identity = manifest.get("copy_identity") or {}
        if not isinstance(source_identity, dict):
            source_identity = {}
        if not isinstance(copy_identity, dict):
            copy_identity = {}
        source_path = str(source_identity.get("path") or "").strip()
        copy_path = str(copy_identity.get("path") or "").strip()
        source_sha = str(source_identity.get("sha256") or "").strip().lower()
        copy_sha = str(copy_identity.get("sha256") or "").strip().lower()
        _check(
            report,
            "manifest.source-identity.path",
            bool(source_path),
            "Verified-local manifest identifies the source artifact",
            actual=source_path,
            expected="non-empty",
        )
        _check(
            report,
            "manifest.source-identity.sha256",
            _valid_sha256(source_sha) and source_sha == expected_sha,
            "Verified-local source identity SHA-256 matches the manifest",
            actual=source_sha,
            expected=expected_sha,
        )
        _check(
            report,
            "manifest.copy-identity.path",
            bool(copy_path),
            "Verified-local manifest identifies the copied artifact",
            actual=copy_path,
            expected="non-empty",
        )
        _check(
            report,
            "manifest.copy-identity.sha256",
            _valid_sha256(copy_sha) and copy_sha == expected_sha,
            "Verified-local copy identity SHA-256 matches the manifest",
            actual=copy_sha,
            expected=expected_sha,
        )
        _check(
            report,
            "manifest.copy-identity.distinct",
            bool(source_path) and bool(copy_path) and source_path != copy_path,
            "Verified-local source and copy identities are distinct",
            actual={"source": source_path, "copy": copy_path},
            expected="different non-empty paths",
        )
        validated_filename = Path(str(report.get("source") or "")).name
        _check(
            report,
            "manifest.copy-identity.filename",
            bool(copy_path) and Path(copy_path).name == validated_filename,
            "Verified-local copy identity names the validated STEP file",
            actual=Path(copy_path).name if copy_path else "",
            expected=validated_filename,
        )


def validate_step(step_path, manifest_path, specification, report_dir):
    """Checksum, import, and validate a STEP/STP component in a temporary document."""
    import FreeCAD as App
    import Import

    step = Path(step_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve() if manifest_path else None
    spec = _load_mapping(specification, "validation specification")
    report = _new_report("STEP", step)
    if step.is_file():
        report["working_sha256"] = _sha256(step)
    stem = step.stem
    temp_document = None
    try:
        _check(report, "file.exists", step.is_file(), "STEP file exists", actual=str(step))
        manifest = {}
        if manifest_file is None:
            _check(report, "manifest.required", False, "STEP validation requires a provenance manifest")
        elif not manifest_file.is_file():
            _check(report, "manifest.exists", False, "STEP manifest is missing", actual=str(manifest_file))
        else:
            manifest = _load_mapping(manifest_file, "STEP manifest")
            _check(report, "manifest.exists", True, "STEP manifest loaded", actual=str(manifest_file))
            _step_manifest_checks(report, manifest)
        if step.is_file():
            actual_sha = _sha256(step)
            expected_sha = _expected_manifest_sha(manifest)
            _check(report, "checksum.declared", bool(expected_sha), "Manifest declares SHA-256", actual=expected_sha, expected="non-empty")
            _check(report, "checksum.match", bool(expected_sha) and actual_sha == expected_sha, "STEP SHA-256 matches manifest", actual=actual_sha, expected=expected_sha)
            temp_document = App.newDocument(f"Validation_{uuid.uuid4().hex[:10]}")
            try:
                Import.insert(str(step), temp_document.Name)
                _check(report, "step.import", True, "STEP imported into a temporary FreeCAD document")
                _document_checks(report, temp_document, spec)
                _save_snapshot(report, temp_document, report_dir, stem)
            except Exception as exc:
                _check(report, "step.import", False, f"STEP import failed without repair: {exc}")
    except Exception as exc:
        _check(report, "validator.exception", False, f"STEP validation aborted: {exc}")
    result = _write_reports(report, report_dir, stem)
    if temp_document is not None:
        App.closeDocument(temp_document.Name)
    return result
