"""Deterministic helpers for standard parts inside a running FreeCAD process."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


FASTENERS_COMMIT = "79a06dc067b57ebc89532be835704eb2af5da96c"
GEARS_COMMIT = "790e75c1dbc5c91b4abeee7ba7f972fa5dc7af57"


LIBRARY_FIELDS = (
    ("LibraryProvider", "Library provider"),
    ("LibraryCategory", "Catalog category"),
    ("LibraryStandard", "Standard or component type"),
    ("LibraryNominalSize", "Nominal size or designation"),
    ("LibraryPartId", "Catalog part identifier"),
    ("LibrarySourceURL", "Catalog or source URL"),
    ("LibrarySHA256", "Source STEP SHA-256"),
    ("LibrarySourceCommit", "Pinned source commit"),
)


def add_library_metadata(
    obj,
    *,
    provider: str,
    category: str = "",
    standard: str = "",
    nominal_size: str = "",
    part_id: str = "",
    source_url: str = "",
    sha256: str = "",
    source_commit: str = "",
) -> None:
    """Attach the shared standard-part provenance contract to a FreeCAD object."""
    for name, label in LIBRARY_FIELDS:
        if not hasattr(obj, name):
            obj.addProperty("App::PropertyString", name, "Library", label)
    obj.LibraryProvider = provider
    obj.LibraryCategory = category
    obj.LibraryStandard = standard
    obj.LibraryNominalSize = nominal_size
    obj.LibraryPartId = part_id
    obj.LibrarySourceURL = source_url
    obj.LibrarySHA256 = sha256
    obj.LibrarySourceCommit = source_commit


def _add_library_metadata(obj, provider: str, standard: str, source_commit: str) -> None:
    """Compatibility wrapper for older callers."""
    add_library_metadata(
        obj,
        provider=provider,
        standard=standard,
        source_commit=source_commit,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_record(manifest: dict) -> dict:
    record = manifest.get("part") or manifest.get("record") or manifest
    if not isinstance(record, dict):
        raise ValueError("STEP.parts manifest has no object-valued part record")
    return record


def _set_enumeration(obj, property_name: str, value: str) -> None:
    allowed = list(obj.getEnumerationsOfProperty(property_name))
    if value not in allowed:
        raise ValueError(
            f"{value!r} is not valid for {obj.Name}.{property_name}; "
            f"allowed values include {allowed[:20]!r}"
        )
    setattr(obj, property_name, value)


def create_fastener(
    document,
    standard: str,
    diameter: str,
    *,
    length: Optional[str] = None,
    name: Optional[str] = None,
    real_thread: bool = False,
):
    """Create a parametric Fasteners Workbench object in *document*."""
    import FreeCAD as App
    import FastenersCmd

    if standard not in FastenersCmd.FSScrewCommandTable:
        raise ValueError(f"Unsupported Fasteners Workbench standard: {standard}")

    object_name = name or f"{standard}_{diameter}_{length or 'standard'}"
    obj = document.addObject("Part::FeaturePython", object_name)
    FastenersCmd.FSScrewObject(obj, standard, None)
    if App.GuiUp:
        FastenersCmd.FSViewProviderTree(obj.ViewObject)
    _set_enumeration(obj, "Diameter", diameter)
    # Fasteners refreshes dependent enumerations such as Length in execute().
    document.recompute()
    if length is not None:
        if not hasattr(obj, "Length"):
            raise ValueError(f"{standard} does not accept a length")
        _set_enumeration(obj, "Length", str(length))
    if hasattr(obj, "Thread"):
        obj.Thread = bool(real_thread)
    add_library_metadata(
        obj,
        provider="FreeCAD Fasteners Workbench",
        category="fastener",
        standard=standard,
        nominal_size=" x ".join(v for v in (diameter, str(length) if length else "") if v),
        source_url="https://github.com/shaise/FreeCAD_FastenersWB",
        source_commit=FASTENERS_COMMIT,
    )
    document.recompute()
    return obj


def create_involute_gear(
    document,
    *,
    module_mm: float,
    teeth: int,
    height_mm: float,
    bore_mm: Optional[float] = None,
    pressure_angle_deg: float = 20.0,
    name: str = "InvoluteGear",
):
    """Create a parametric external involute gear in *document*."""
    import FreeCAD as App
    from freecad.gears.basegear import ViewProviderGear
    from freecad.gears.commands import CreateInvoluteGear

    if module_mm <= 0 or teeth < 3 or height_mm <= 0:
        raise ValueError("module and height must be positive; teeth must be at least 3")
    if bore_mm is not None and bore_mm <= 0:
        raise ValueError("bore must be positive when provided")

    previous = document.addObject("Part::FeaturePython", name)
    CreateInvoluteGear.GEAR_FUNCTION(previous)
    if App.GuiUp:
        ViewProviderGear(previous.ViewObject, CreateInvoluteGear.Pixmap)
    previous.num_teeth = int(teeth)
    previous.module = float(module_mm)
    previous.height = float(height_mm)
    previous.pressure_angle = float(pressure_angle_deg)
    previous.axle_hole = bore_mm is not None
    if bore_mm is not None:
        previous.axle_holesize = float(bore_mm)
    add_library_metadata(
        previous,
        provider="freecad.gears",
        category="gear",
        standard="External involute gear",
        nominal_size=f"m{module_mm:g} z{teeth} h{height_mm:g}",
        source_url="https://github.com/looooo/freecad.gears",
        source_commit=GEARS_COMMIT,
    )
    document.recompute()
    return previous


def import_step_part(
    document,
    step_path,
    manifest_path,
    *,
    name: Optional[str] = None,
    placement=None,
):
    """Import a checksum-verified cached STEP.parts component as one Part::Feature."""
    import Part

    step = Path(step_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    if not step.is_file() or not manifest_file.is_file():
        raise FileNotFoundError("STEP file and manifest.json must both exist")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    record = _manifest_record(manifest)
    expected = str(manifest.get("sha256") or record.get("sha256") or "").lower()
    actual = _sha256(step)
    if not expected:
        raise ValueError("STEP.parts manifest does not contain sha256")
    if actual != expected:
        raise ValueError(f"STEP checksum mismatch: expected {expected}, got {actual}")

    shape = Part.read(str(step))
    if shape is None or shape.isNull() or not shape.isValid():
        raise ValueError(f"Invalid STEP geometry: {step}")
    object_name = name or str(record.get("id") or step.stem)
    obj = document.addObject("Part::Feature", object_name)
    obj.Shape = shape
    if placement is not None:
        obj.Placement = placement
    standard_value = record.get("standard") or record.get("family") or "catalog component"
    if isinstance(standard_value, dict):
        standard_value = standard_value.get("designation") or standard_value.get("name") or ""
    attributes = record.get("attributes") or {}
    nominal = (
        attributes.get("nominalSize")
        or attributes.get("designation")
        or record.get("name")
        or ""
    )
    add_library_metadata(
        obj,
        provider="STEP.parts",
        category=str(record.get("category") or "catalog component"),
        standard=str(standard_value),
        nominal_size=str(nominal),
        part_id=str(record.get("id") or manifest.get("part_id") or ""),
        source_url=str(record.get("pageUrl") or record.get("apiUrl") or manifest.get("source_url") or ""),
        sha256=actual,
        source_commit=str(manifest.get("skill_commit") or ""),
    )
    document.recompute()
    return obj


def assert_valid_solid(obj) -> None:
    """Raise when an object does not contain a valid solid shape."""
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise AssertionError(f"{obj.Name} has no shape")
    if not shape.isValid():
        raise AssertionError(f"{obj.Name} has an invalid shape")
    if len(shape.Solids) < 1:
        raise AssertionError(f"{obj.Name} does not contain a solid")
