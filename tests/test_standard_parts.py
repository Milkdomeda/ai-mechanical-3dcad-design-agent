import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mechanical_design_agent.standard_part_configuration import (
    load_standard_part_provider_catalog,
)
from mechanical_design_agent.standard_parts import StandardPartRegistry
from mechanical_design_agent.workspace_bootstrap import (
    BootstrapFailure,
    initialize_workspace,
)


class StandardPartProviderTests(unittest.TestCase):
    def test_mandatory_provider_order_precedes_expanded_channels(self):
        value = load_standard_part_provider_catalog().as_dict()
        ids = [item["id"] for item in value["providers"]]
        self.assertEqual(ids[:4], ["freecad-fasteners", "freecad-gears", "step-parts", "verified-local"])
        self.assertIn("manufacturer-official", ids[4:])
        self.assertIn("3dfindit-cadenas", ids[4:])

    def test_manufacturer_part_number_spelling_is_preserved_in_catalog_key(self):
        self.assertEqual(
            StandardPartRegistry._part_key("521H20A+1000.000Y=20.000"),
            "521H20A+1000.000Y=20.000",
        )


def settings_for_workspace(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace=workspace)


def test_registry_lists_providers_when_v1_catalog_is_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)

    registry = StandardPartRegistry(settings_for_workspace(workspace), object())

    assert registry.catalog_root is None
    assert registry.sources.code == "STANDARD_PART_CATALOG_DISABLED"
    assert registry.list_providers()["providers"]


def test_registry_reads_legacy_absolute_catalog_without_rewrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    sources = workspace / "config/standard_parts_sources.json"
    sources.write_text(
        json.dumps({"verified_local_catalog": {"global_root": str(catalog)}})
        + "\n",
        encoding="utf-8",
    )
    before = sources.read_bytes()

    registry = StandardPartRegistry(settings_for_workspace(workspace), object())

    assert registry.catalog_root == catalog.resolve()
    assert registry.sources.code == "STANDARD_PART_SOURCES_LEGACY_FORMAT"
    assert sources.read_bytes() == before


def test_disabled_registry_stops_before_registration_side_effects(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)
    source = workspace / "part.step"
    source.write_bytes(b"unchanged")
    repository = Mock()
    registry = StandardPartRegistry(settings_for_workspace(workspace), repository)

    with pytest.raises(BootstrapFailure) as captured:
        registry.register_download(
            provider_id="step-parts",
            file_path=str(source),
            part_number="PART-1",
            standard="TEST",
            nominal_size="1",
            source_url="https://example.invalid/part",
            metadata={},
            approval_reference="approved",
            validation_report_path=str(workspace / "missing-report.json"),
        )

    assert captured.value.code == "STANDARD_PART_CATALOG_DISABLED"
    assert captured.value.status == "setup_required"
    assert source.read_bytes() == b"unchanged"
    repository.register_standard_part.assert_not_called()


if __name__ == "__main__":
    unittest.main()
