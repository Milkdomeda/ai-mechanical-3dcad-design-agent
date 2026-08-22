from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from mechanical_design_agent import cli
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


BOOTSTRAP_ENVIRONMENT_KEYS = (
    "MECH_DESIGN_ENV_FILE",
    "MECH_DESIGN_WORKSPACE",
    "MECH_DESIGN_ACTOR_ID",
    "MECH_DESIGN_PRODUCT_FAMILY_ID",
    "MECH_DESIGN_DATABASE_URL",
    "MECH_DESIGN_NEO4J_URI",
    "MECH_DESIGN_NEO4J_USER",
    "MECH_DESIGN_NEO4J_PASSWORD",
    "MECH_DESIGN_FREECADCMD",
)


def clear_bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in BOOTSTRAP_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def forbid_operational_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnexpectedService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("standard-part configuration constructed the full service")

    monkeypatch.setattr(cli, "MechanicalDesignService", UnexpectedService)


def run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *arguments])
    exit_code = 0
    try:
        cli.main()
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    output = capsys.readouterr()
    assert output.out, output.err
    return exit_code, json.loads(output.out)


def test_provider_cli_is_package_only_before_workspace_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clear_bootstrap_environment(monkeypatch)
    forbid_operational_service(monkeypatch)
    monkeypatch.chdir(tmp_path)

    all_code, all_value = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "providers",
    )
    filtered_code, filtered = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "providers",
        "--category",
        "fastener",
    )

    assert all_code == 0
    assert filtered_code == 0
    assert all_value["schema_version"] == "StandardPartProviders/v1"
    assert len(all_value["providers"]) == 8
    assert filtered["category"] == "fastener"
    assert all("all" in item["categories"] or "fastener" in item["categories"] for item in filtered["providers"])
    assert list(tmp_path.iterdir()) == []


def test_sources_status_without_workspace_is_structured_setup_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clear_bootstrap_environment(monkeypatch)
    forbid_operational_service(monkeypatch)
    monkeypatch.chdir(tmp_path)

    code, value = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "status",
    )

    assert code == 2
    assert value["schema_version"] == "StandardPartConfigurationResult/v1"
    assert value["status"] == "setup_required"
    assert value["code"] == "WORKSPACE_NOT_INITIALIZED"
    assert value["changed"] is False
    assert list(tmp_path.iterdir()) == []


def test_catalog_cli_existing_only_lifecycle_is_idempotent_and_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clear_bootstrap_environment(monkeypatch)
    forbid_operational_service(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-test", dry_run=False)
    manifest = workspace / "config/mechanical_design.json"
    families = workspace / "config/product_families"
    artifacts = workspace / "data/artifacts"
    manifest_before = manifest.read_bytes()
    family_before = list(families.iterdir())
    artifact_before = list(artifacts.iterdir())

    status_code, status = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "status",
        "--workspace",
        str(workspace),
    )
    assert status_code == 1
    assert status["code"] == "STANDARD_PART_CATALOG_DISABLED"

    missing = tmp_path / "missing-catalog"
    missing_code, missing_result = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "enable",
        "--root",
        str(missing),
        "--workspace",
        str(workspace),
    )
    assert missing_code == 3
    assert missing_result["code"] == "STANDARD_PART_CATALOG_ROOT_NOT_FOUND"
    assert not missing.exists()

    catalog = tmp_path / "external-catalog"
    catalog.mkdir()
    enabled_code, enabled = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "enable",
        "--root",
        str(catalog),
        "--workspace",
        str(workspace),
    )
    assert enabled_code == 0
    assert enabled["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
    assert list(catalog.iterdir()) == []

    sources = workspace / "config/standard_parts_sources.json"
    first_stat = sources.stat()
    first_snapshot = (sources.read_bytes(), first_stat.st_mtime_ns, first_stat.st_ino)
    repeated_code, repeated = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "enable",
        "--root",
        str(catalog),
        "--workspace",
        str(workspace),
    )
    second_stat = sources.stat()
    assert repeated_code == 0
    assert repeated["code"] == "STANDARD_PART_CATALOG_ALREADY_CONFIGURED"
    assert (sources.read_bytes(), second_stat.st_mtime_ns, second_stat.st_ino) == (
        first_snapshot
    )

    disabled_code, disabled = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "disable",
        "--workspace",
        str(workspace),
    )
    assert disabled_code == 0
    assert disabled["code"] == "STANDARD_PART_CATALOG_DISABLED"
    disabled_stat = sources.stat()
    disabled_snapshot = (
        sources.read_bytes(),
        disabled_stat.st_mtime_ns,
        disabled_stat.st_ino,
    )

    repeated_disable_code, repeated_disable = run_cli(
        monkeypatch,
        capsys,
        "standard-parts",
        "catalog",
        "disable",
        "--workspace",
        str(workspace),
    )
    final_stat = sources.stat()
    assert repeated_disable_code == 0
    assert repeated_disable["code"] == "STANDARD_PART_CATALOG_ALREADY_DISABLED"
    assert (sources.read_bytes(), final_stat.st_mtime_ns, final_stat.st_ino) == (
        disabled_snapshot
    )
    assert manifest.read_bytes() == manifest_before
    assert list(families.iterdir()) == family_before
    assert list(artifacts.iterdir()) == artifact_before
    assert list(catalog.iterdir()) == []
    assert not (workspace / "output").exists()
    assert not (workspace / "knowledge").exists()
