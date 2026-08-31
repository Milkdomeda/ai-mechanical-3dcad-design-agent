from __future__ import annotations

import json
from pathlib import Path

import pytest

from mechanical_design_agent.config import StandardPartSettings
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.standard_part_configuration import (
    load_standard_part_provider_catalog,
)
from mechanical_design_agent.standard_parts import StandardPartRegistry


def test_provider_order_keeps_preferred_sources_first() -> None:
    providers = load_standard_part_provider_catalog().as_dict()["providers"]
    ids = [item["id"] for item in providers]

    assert ids[:4] == [
        "freecad-fasteners",
        "freecad-gears",
        "step-parts",
        "verified-local",
    ]


def test_manufacturer_part_number_spelling_is_preserved() -> None:
    assert StandardPartRegistry._part_key("521H20A+1000.000Y=20.000") == (
        "521H20A+1000.000Y=20.000"
    )


def test_disabled_catalog_stops_before_registration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "part.step"
    source.write_bytes(b"ISO-10303-21")
    registry = StandardPartRegistry(
        StandardPartSettings(workspace=workspace, catalog_root=None)
    )

    with pytest.raises(ValueError, match="not configured"):
        registry.register_download(
            provider_id="step-parts",
            file_path=str(source),
            part_number="PART-1",
            standard="TEST",
            nominal_size="1 mm",
            source_url="https://example.invalid/part",
            metadata={},
            validation_report_path=str(workspace / "missing.json"),
        )

    assert source.read_bytes() == b"ISO-10303-21"


def test_validated_part_is_registered_with_provenance_and_copied_into_design(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    catalog = tmp_path / "catalog"
    design = workspace / "designs" / "carrier"
    workspace.mkdir()
    catalog.mkdir()
    design.mkdir(parents=True)
    source = workspace / "bearing.step"
    source.write_bytes(b"ISO-10303-21;BEARING")
    report = workspace / "part-validation.json"
    report.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    registry = StandardPartRegistry(
        StandardPartSettings(workspace=workspace, catalog_root=catalog)
    )

    registered = registry.register_download(
        provider_id="step-parts",
        file_path=str(source),
        part_number="6204-2RS",
        standard="ISO 15",
        nominal_size="20x47x14 mm",
        source_url="https://step.parts/example",
        metadata={"manufacturer": "Example", "category": "bearing"},
        validation_report_path=str(report),
    )
    copied = registry.copy_into_design(registered=registered, design_root=design)

    assert registered["sha256"] == file_sha256(source)
    assert Path(str(registered["manifest_path"])).is_file()
    assert Path(str(copied["path"])).read_bytes() == source.read_bytes()
    assert copied["relative_path"].startswith("components/standard-parts/")
