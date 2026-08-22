from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from mechanical_design_agent import standard_part_configuration as configuration
from mechanical_design_agent.package_resources import standard_part_provider_config
from mechanical_design_agent.standard_part_configuration import (
    _parse_standard_part_provider_catalog,
    disable_standard_part_catalog,
    enable_standard_part_catalog,
    load_standard_part_provider_catalog,
    load_standard_part_sources,
    probe_standard_part_catalog,
)
from mechanical_design_agent.workspace_bootstrap import (
    BootstrapFailure,
    WorkspaceManifest,
    initialize_workspace,
    read_workspace_manifest,
)


EXPECTED_PROVIDER_IDS = [
    "freecad-fasteners",
    "freecad-gears",
    "step-parts",
    "verified-local",
    "manufacturer-official",
    "3dfindit-cadenas",
    "misumi",
    "traceparts",
]
EXPECTED_FASTENER_PROVIDER_IDS = [
    "freecad-fasteners",
    "step-parts",
    "verified-local",
    "manufacturer-official",
    "3dfindit-cadenas",
    "misumi",
    "traceparts",
]
EXPECTED_PROVIDER_SHA256 = (
    "089afe6cbbdae68d72aea60497bf285fc01b1d589dde7379a2a124e346c35464"
)


def provider(
    provider_id: object = "provider-a",
    *,
    priority: object = 10,
    categories: object = None,
    login_required: object = False,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": "Provider A",
        "priority": priority,
        "trust_tier": "verified",
        "acquisition": "local",
        "login_required": login_required,
        "categories": ["all"] if categories is None else categories,
        "examples": ["Example A"],
    }


def provider_value(*providers: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "StandardPartProviders/v1",
        "providers": list(providers),
    }


def initialized_manifest(tmp_path: Path) -> WorkspaceManifest:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)
    return read_workspace_manifest(workspace)


def write_sources(manifest: WorkspaceManifest, value: object) -> None:
    manifest.standard_parts_sources.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def v1_sources(*, enabled: object, root: object) -> dict[str, object]:
    return {
        "schema_version": "StandardPartSources/v1",
        "verified_local_catalog": {
            "enabled": enabled,
            "global_root": root,
        },
    }


def file_snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns, stat.st_ino


def test_provider_catalog_preserves_packaged_order_filtering_and_sha256() -> None:
    with standard_part_provider_config() as path:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == EXPECTED_PROVIDER_SHA256

    catalog = load_standard_part_provider_catalog()
    all_value = catalog.as_dict()
    fasteners = catalog.as_dict("fastener")

    assert all_value["schema_version"] == "StandardPartProviders/v1"
    assert all_value["category"] is None
    assert [item["id"] for item in all_value["providers"]] == EXPECTED_PROVIDER_IDS
    assert [item["id"] for item in fasteners["providers"]] == (
        EXPECTED_FASTENER_PROVIDER_IDS
    )
    manufacturer = next(
        item
        for item in all_value["providers"]
        if item["id"] == "manufacturer-official"
    )
    assert manufacturer["examples"] == [
        "Thomson CAD Center",
        "HIWIN",
        "THK",
        "PMI",
        "TBI",
        "NSK",
        "Bosch Rexroth",
    ]


def test_provider_catalog_is_deeply_immutable_but_returns_json_copies() -> None:
    catalog = _parse_standard_part_provider_catalog(provider_value(provider()))

    with pytest.raises(TypeError):
        catalog.providers[0]["id"] = "changed"  # type: ignore[index]
    assert isinstance(catalog.providers[0]["categories"], tuple)
    assert isinstance(catalog.providers[0]["examples"], tuple)

    first = catalog.as_dict()
    first["providers"][0]["categories"].append("mutated")
    second = catalog.as_dict()
    assert second["providers"][0]["categories"] == ["all"]


def test_provider_catalog_sorts_a_copy_by_priority() -> None:
    catalog = _parse_standard_part_provider_catalog(
        provider_value(
            provider("provider-b", priority=20),
            provider("provider-a", priority=10),
        )
    )
    assert [item["id"] for item in catalog.as_dict()["providers"]] == [
        "provider-a",
        "provider-b",
    ]


def wrong_schema(value: dict[str, object]) -> None:
    value["schema_version"] = "invalid"


def non_list_providers(value: dict[str, object]) -> None:
    value["providers"] = {}


def duplicate_ids(value: dict[str, object]) -> None:
    value["providers"] = [
        provider("duplicate", priority=10),
        provider("duplicate", priority=20),
    ]


def duplicate_priorities(value: dict[str, object]) -> None:
    value["providers"] = [
        provider("provider-a", priority=10),
        provider("provider-b", priority=10),
    ]


def bool_priority(value: dict[str, object]) -> None:
    value["providers"] = [provider(priority=True)]


def blank_required_string(value: dict[str, object]) -> None:
    value["providers"][0]["name"] = "  "


def non_boolean_login(value: dict[str, object]) -> None:
    value["providers"] = [provider(login_required="false")]


def invalid_categories(value: dict[str, object]) -> None:
    value["providers"] = [provider(categories=["all", 1])]


@pytest.mark.parametrize(
    "mutate",
    [
        wrong_schema,
        non_list_providers,
        duplicate_ids,
        duplicate_priorities,
        bool_priority,
        blank_required_string,
        non_boolean_login,
        invalid_categories,
    ],
    ids=lambda value: value.__name__,
)
def test_invalid_provider_catalog_is_blocked(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    value = provider_value(provider())
    mutate(value)

    with pytest.raises(BootstrapFailure) as captured:
        _parse_standard_part_provider_catalog(value)

    assert captured.value.code == "STANDARD_PART_PROVIDER_CONFIG_INVALID"


def test_disabled_v1_sources_are_valid_without_a_catalog_root(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)

    sources = load_standard_part_sources(manifest)

    assert sources.schema_kind == "v1"
    assert sources.enabled is False
    assert sources.configured_root is None
    assert sources.effective_root is None
    assert sources.status == "warning"
    assert sources.code == "STANDARD_PART_CATALOG_DISABLED"
    assert sources.catalog_dict() == {
        "schema_kind": "v1",
        "enabled": False,
        "configured_root": None,
        "effective_root": None,
    }


def test_enabled_relative_sources_resolve_against_workspace(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = manifest.workspace / "data" / "standard_parts"
    catalog.mkdir()
    write_sources(manifest, v1_sources(enabled=True, root="data/standard_parts"))

    sources = load_standard_part_sources(manifest)

    assert sources.schema_kind == "v1"
    assert sources.enabled is True
    assert sources.configured_root == "data/standard_parts"
    assert sources.effective_root == catalog.resolve()
    assert sources.status == "ok"
    assert sources.code == "STANDARD_PART_CATALOG_READY"


def test_enabled_absolute_sources_resolve_external_catalog(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    write_sources(manifest, v1_sources(enabled=True, root=str(catalog)))

    sources = load_standard_part_sources(manifest)

    assert sources.configured_root == str(catalog)
    assert sources.effective_root == catalog.resolve()


def test_catalog_symlink_spelling_is_canonicalized_and_deduplicated(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    alias = tmp_path / "catalog-alias"
    alias.symlink_to(catalog, target_is_directory=True)
    write_sources(manifest, v1_sources(enabled=True, root=str(alias)))

    sources = load_standard_part_sources(manifest)

    assert sources.configured_root == str(alias)
    assert sources.effective_root == catalog.resolve()


def test_managed_sources_file_must_not_be_a_symlink(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    external = tmp_path / "sources.json"
    external.write_text(
        json.dumps(v1_sources(enabled=False, root=None)) + "\n",
        encoding="utf-8",
    )
    manifest.standard_parts_sources.unlink()
    manifest.standard_parts_sources.symlink_to(external)

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_SOURCES_INVALID"


def test_missing_catalog_root_has_dedicated_failure(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    missing = tmp_path / "missing-catalog"
    write_sources(manifest, v1_sources(enabled=True, root=str(missing)))

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_CATALOG_ROOT_NOT_FOUND"
    assert not missing.exists()


def test_catalog_root_must_be_a_directory(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    file_root = tmp_path / "catalog.step"
    file_root.write_bytes(b"not-a-directory")
    write_sources(manifest, v1_sources(enabled=True, root=str(file_root)))

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_CATALOG_ROOT_NOT_DIRECTORY"


@pytest.mark.parametrize(
    "relative",
    [
        ".",
        "config/catalog",
        "output/catalog",
        "knowledge/catalog",
        "config/product_families/catalog",
        "data/artifacts/catalog",
    ],
)
def test_catalog_root_cannot_overlap_protected_workspace_trees(
    tmp_path: Path,
    relative: str,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = manifest.workspace / relative
    catalog.mkdir(parents=True, exist_ok=True)
    write_sources(manifest, v1_sources(enabled=True, root=relative))

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_CATALOG_ROOT_UNSAFE"


def test_legacy_absolute_sources_are_read_only_compatible(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "legacy-catalog"
    catalog.mkdir()
    write_sources(
        manifest,
        {"verified_local_catalog": {"global_root": str(catalog)}},
    )
    before = manifest.standard_parts_sources.read_bytes()

    sources = load_standard_part_sources(manifest)

    assert sources.schema_kind == "legacy"
    assert sources.enabled is True
    assert sources.status == "warning"
    assert sources.code == "STANDARD_PART_SOURCES_LEGACY_FORMAT"
    assert sources.effective_root == catalog.resolve()
    assert manifest.standard_parts_sources.read_bytes() == before


def test_legacy_relative_sources_are_blocked(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = manifest.workspace / "data" / "standard_parts"
    catalog.mkdir()
    write_sources(
        manifest,
        {"verified_local_catalog": {"global_root": "data/standard_parts"}},
    )

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_SOURCES_INVALID"


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"schema_version": "invalid", "verified_local_catalog": {}},
        {
            "schema_version": "StandardPartSources/v1",
            "verified_local_catalog": {"enabled": False, "global_root": None},
            "extra": True,
        },
        {
            "schema_version": "StandardPartSources/v1",
            "verified_local_catalog": {
                "enabled": False,
                "global_root": None,
                "extra": True,
            },
        },
        v1_sources(enabled="false", root=None),
        v1_sources(enabled=False, root="/stale/path"),
        v1_sources(enabled=True, root=""),
        v1_sources(enabled=True, root="  "),
        {"verified_local_catalog": {"global_root": "/tmp", "enabled": True}},
    ],
)
def test_invalid_sources_shapes_are_blocked(
    tmp_path: Path,
    value: object,
) -> None:
    manifest = initialized_manifest(tmp_path)
    write_sources(manifest, value)

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_SOURCES_INVALID"


@pytest.mark.parametrize("content", [b"{not-json}\n", b"\xff\xfe\x00"])
def test_unreadable_sources_content_is_blocked(
    tmp_path: Path,
    content: bytes,
) -> None:
    manifest = initialized_manifest(tmp_path)
    manifest.standard_parts_sources.write_bytes(content)

    with pytest.raises(BootstrapFailure) as captured:
        load_standard_part_sources(manifest)

    assert captured.value.code == "STANDARD_PART_SOURCES_INVALID"


def test_enable_requires_existing_directory_and_never_creates_it(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    missing = tmp_path / "never-created"
    before = manifest.standard_parts_sources.read_bytes()

    with pytest.raises(BootstrapFailure) as captured:
        enable_standard_part_catalog(manifest=manifest, root_path=missing)

    assert captured.value.code == "STANDARD_PART_CATALOG_ROOT_NOT_FOUND"
    assert not missing.exists()
    assert manifest.standard_parts_sources.read_bytes() == before


def test_enable_binds_existing_external_directory_without_populating_it(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()

    result = enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    assert result == {
        "schema_version": "StandardPartConfigurationResult/v1",
        "operation": "catalog_enable",
        "status": "ok",
        "code": "STANDARD_PART_CATALOG_CONFIGURED",
        "message": "standard-part catalog binding configured",
        "changed": True,
        "catalog": {
            "schema_kind": "v1",
            "enabled": True,
            "configured_root": str(catalog.resolve()),
            "effective_root": str(catalog.resolve()),
        },
        "next_actions": [],
    }
    assert list(catalog.iterdir()) == []
    assert json.loads(manifest.standard_parts_sources.read_text(encoding="utf-8")) == (
        v1_sources(enabled=True, root=str(catalog.resolve()))
    )


def test_enable_stores_safe_workspace_catalog_as_posix_relative_path(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = manifest.workspace / "data" / "standard_parts"
    catalog.mkdir()

    result = enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    assert result["catalog"]["configured_root"] == "data/standard_parts"
    assert json.loads(manifest.standard_parts_sources.read_text(encoding="utf-8")) == (
        v1_sources(enabled=True, root="data/standard_parts")
    )


def test_enable_deduplicates_symlink_spelling_to_canonical_external_path(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    alias = tmp_path / "catalog-alias"
    alias.symlink_to(catalog, target_is_directory=True)

    result = enable_standard_part_catalog(manifest=manifest, root_path=alias)

    assert result["catalog"]["configured_root"] == str(catalog.resolve())


def test_enable_same_effective_v1_root_is_idempotent(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    enable_standard_part_catalog(manifest=manifest, root_path=catalog)
    before = file_snapshot(manifest.standard_parts_sources)

    result = enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    assert result["code"] == "STANDARD_PART_CATALOG_ALREADY_CONFIGURED"
    assert result["changed"] is False
    assert file_snapshot(manifest.standard_parts_sources) == before
    assert list(catalog.iterdir()) == []


def test_explicit_enable_converts_legacy_sources_to_v1(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "legacy-catalog"
    catalog.mkdir()
    write_sources(
        manifest,
        {"verified_local_catalog": {"global_root": str(catalog)}},
    )
    before = manifest.standard_parts_sources.read_bytes()

    result = enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    assert result["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
    assert result["changed"] is True
    assert manifest.standard_parts_sources.read_bytes() != before
    assert json.loads(manifest.standard_parts_sources.read_text(encoding="utf-8")) == (
        v1_sources(enabled=True, root=str(catalog.resolve()))
    )


def test_disable_removes_only_binding_and_preserves_catalog_contents(
    tmp_path: Path,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    sentinel = catalog / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    result = disable_standard_part_catalog(manifest=manifest)

    assert result["status"] == "ok"
    assert result["code"] == "STANDARD_PART_CATALOG_DISABLED"
    assert result["changed"] is True
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert json.loads(manifest.standard_parts_sources.read_text(encoding="utf-8")) == (
        v1_sources(enabled=False, root=None)
    )


def test_disable_already_disabled_v1_is_idempotent(tmp_path: Path) -> None:
    manifest = initialized_manifest(tmp_path)
    before = file_snapshot(manifest.standard_parts_sources)

    result = disable_standard_part_catalog(manifest=manifest)

    assert result["code"] == "STANDARD_PART_CATALOG_ALREADY_DISABLED"
    assert result["changed"] is False
    assert file_snapshot(manifest.standard_parts_sources) == before


@pytest.mark.parametrize("operation", ["enable", "disable"])
def test_invalid_managed_sources_are_not_implicitly_repaired(
    tmp_path: Path,
    operation: str,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    write_sources(manifest, {})
    before = manifest.standard_parts_sources.read_bytes()

    with pytest.raises(BootstrapFailure) as captured:
        if operation == "enable":
            enable_standard_part_catalog(manifest=manifest, root_path=catalog)
        else:
            disable_standard_part_catalog(manifest=manifest)

    assert captured.value.code == "STANDARD_PART_SOURCES_INVALID"
    assert manifest.standard_parts_sources.read_bytes() == before
    assert list(catalog.iterdir()) == []


@pytest.mark.parametrize("failure", ["create", "write", "cleanup"])
def test_probe_failure_prevents_configuration_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    before = manifest.standard_parts_sources.read_bytes()

    if failure == "create":
        def fail_create(*args: object, **kwargs: object) -> tuple[int, str]:
            raise PermissionError("create denied")

        monkeypatch.setattr(configuration.tempfile, "mkstemp", fail_create)
    elif failure == "write":
        def fail_write(descriptor: int) -> None:
            raise OSError("flush denied")

        monkeypatch.setattr(configuration.os, "fsync", fail_write)
    else:
        original_unlink = Path.unlink

        def fail_probe_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            if path.name.startswith(".mechanical-design-catalog-probe."):
                raise PermissionError("cleanup denied")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_probe_cleanup)

    with pytest.raises(BootstrapFailure) as captured:
        enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    expected = (
        "STANDARD_PART_CATALOG_PROBE_CLEANUP_FAILED"
        if failure == "cleanup"
        else "STANDARD_PART_CATALOG_ROOT_NOT_WRITABLE"
    )
    assert captured.value.code == expected
    assert manifest.standard_parts_sources.read_bytes() == before


def test_atomic_publication_failure_is_translated_and_preserves_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = initialized_manifest(tmp_path)
    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    before = manifest.standard_parts_sources.read_bytes()

    def fail_publication(path: Path, value: object) -> None:
        raise BootstrapFailure("ATOMIC_WRITE_FAILED", "injected failure")

    monkeypatch.setattr(
        configuration,
        "atomic_replace_managed_json",
        fail_publication,
    )

    with pytest.raises(BootstrapFailure) as captured:
        enable_standard_part_catalog(manifest=manifest, root_path=catalog)

    assert captured.value.code == "STANDARD_PART_CONFIG_WRITE_FAILED"
    assert manifest.standard_parts_sources.read_bytes() == before
    assert list(catalog.iterdir()) == []


def test_direct_probe_removes_its_temporary_file(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()

    probe_standard_part_catalog(catalog)

    assert list(catalog.iterdir()) == []
