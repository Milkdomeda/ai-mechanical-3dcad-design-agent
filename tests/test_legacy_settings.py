from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from mechanical_design_agent import config
from mechanical_design_agent.config import Settings
from mechanical_design_agent.workspace_bootstrap import (
    BootstrapFailure,
    ParsedEnvFile,
    parse_selected_env_file,
)


LEGACY_ENVIRONMENT_KEYS = (
    "MECH_DESIGN_ENV_FILE",
    "MECH_DESIGN_WORKSPACE",
    "MECH_DESIGN_FAMILY_CONFIG",
    "MECH_DESIGN_ACTOR_ID",
    "MECH_DESIGN_ARTIFACT_ROOT",
    "MECH_DESIGN_FREECADCMD",
    "MECH_DESIGN_DATABASE_URL",
    "MECH_DESIGN_NEO4J_URI",
    "MECH_DESIGN_NEO4J_USER",
    "MECH_DESIGN_NEO4J_PASSWORD",
)


def family_value() -> dict[str, object]:
    return {
        "schema_version": "product-family-bootstrap/v1",
        "organization_id": "org-legacy",
        "organization_name": "Legacy organization",
        "design_group_id": "group-legacy",
        "design_group_name": "Legacy group",
        "family_id": "family-legacy",
        "family_name": "Legacy family",
        "aliases": [],
        "status": "awaiting-source-folder",
        "subfamily_mode": "discover-and-confirm",
        "expected_initial_models": {"minimum": 1, "maximum": 9},
        "question_batch_limit": 5,
        "minimum_distinct_models_for_generalization": 3,
        "family_owner_actor_id": "actor-file-value",
        "knowledge_scope_policy": "family-isolated-explicit-promotion",
        "specialized_context_policy": "explicit-family-authorization-only",
        "source_formats": [".step", ".stp", ".FCStd"],
        "design_policy": {
            "priority": "existing-product-modification-first",
            "working_copy_required": True,
            "approval_before_delivery": True,
        },
        "validation_policy": {
            "geometry": True,
            "interfaces": True,
            "rules": True,
            "units": True,
            "basic_engineering_calculations": True,
            "fem_required": False,
        },
    }


def write_family(path: Path, value: object | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(family_value() if value is None else value) + "\n",
        encoding="utf-8",
    )
    return path


def legacy_layout(tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    artifacts = workspace / "data/artifacts"
    artifacts.mkdir(parents=True)
    family = write_family(workspace / "config/product_families/legacy-name.json")
    freecad = tmp_path / "FreeCADCmd"
    freecad.write_text("test executable placeholder\n", encoding="utf-8")
    return {
        "workspace": workspace,
        "artifacts": artifacts,
        "family": family,
        "freecad": freecad,
    }


def clear_legacy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in LEGACY_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def configure_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
    layout: dict[str, Path],
    *,
    family: str | None = None,
) -> None:
    clear_legacy_environment(monkeypatch)
    monkeypatch.setenv("MECH_DESIGN_WORKSPACE", str(layout["workspace"]))
    monkeypatch.setenv(
        "MECH_DESIGN_FAMILY_CONFIG",
        family if family is not None else str(layout["family"]),
    )
    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "actor-process")
    monkeypatch.setenv("MECH_DESIGN_ARTIFACT_ROOT", str(layout["artifacts"]))
    monkeypatch.setenv("MECH_DESIGN_FREECADCMD", str(layout["freecad"]))


def assert_failure(
    expected_code: str,
    expected_status: str,
) -> pytest.ExceptionInfo[BootstrapFailure]:
    with pytest.raises(BootstrapFailure) as captured:
        Settings.from_environment()
    assert captured.value.code == expected_code
    assert captured.value.status == expected_status
    assert captured.value.as_dict() == {
        "schema_version": "MechanicalDesignBootstrapError/v1",
        "status": expected_status,
        "code": expected_code,
        "message": captured.value.message,
    }
    return captured


def test_explicit_absolute_family_and_actor_preserve_legacy_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    configure_explicit_environment(monkeypatch, layout)

    settings = Settings.from_environment()

    assert settings.workspace == layout["workspace"].resolve()
    assert settings.family_config_path == layout["family"].resolve()
    assert settings.family_config_path.name == "legacy-name.json"
    assert settings.actor_id == "actor-process"


def test_explicit_env_file_is_isolated_and_process_values_win(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    env_file_path = tmp_path / "legacy.env"
    env_file_path.write_text(
        "\n".join(
            (
                f"MECH_DESIGN_WORKSPACE={layout['workspace']}",
                "MECH_DESIGN_FAMILY_CONFIG=config/product_families/legacy-name.json",
                "MECH_DESIGN_ACTOR_ID=actor-env-file",
                f"MECH_DESIGN_ARTIFACT_ROOT={layout['artifacts']}",
                f"MECH_DESIGN_FREECADCMD={layout['freecad']}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    clear_legacy_environment(monkeypatch)
    monkeypatch.setenv("MECH_DESIGN_ENV_FILE", str(env_file_path))
    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "actor-process")
    captured: list[ParsedEnvFile] = []
    mapping_snapshots: list[dict[str, object]] = []

    def recording_parser(
        runtime_path: str | None,
        environ: Mapping[str, str],
        cwd: Path,
    ) -> ParsedEnvFile | None:
        parsed = parse_selected_env_file(runtime_path, environ, cwd)
        if parsed is not None:
            captured.append(parsed)
            mapping_snapshots.append(dict(parsed.values))
        return parsed

    monkeypatch.setattr(config, "parse_selected_env_file", recording_parser)
    process_before = dict(os.environ)

    settings = Settings.from_environment()

    assert len(captured) == 1
    assert settings.actor_id == "actor-process"
    assert settings.family_config_path == layout["family"].resolve()
    assert dict(captured[0].values) == mapping_snapshots[0]
    assert dict(os.environ) == process_before


@pytest.mark.parametrize(
    ("missing_key", "value", "expected_code"),
    [
        ("MECH_DESIGN_FAMILY_CONFIG", None, "LEGACY_FAMILY_CONFIG_REQUIRED"),
        ("MECH_DESIGN_FAMILY_CONFIG", "   ", "LEGACY_FAMILY_CONFIG_REQUIRED"),
        ("MECH_DESIGN_ACTOR_ID", None, "LEGACY_ACTOR_ID_REQUIRED"),
        ("MECH_DESIGN_ACTOR_ID", "", "LEGACY_ACTOR_ID_REQUIRED"),
    ],
)
def test_required_legacy_values_are_structured_setup_required(
    missing_key: str,
    value: str | None,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    configure_explicit_environment(monkeypatch, layout)
    if value is None:
        monkeypatch.delenv(missing_key)
    else:
        monkeypatch.setenv(missing_key, value)

    assert_failure(expected_code, "setup_required")


def test_relative_family_requires_explicit_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    configure_explicit_environment(
        monkeypatch,
        layout,
        family="config/product_families/legacy-name.json",
    )
    monkeypatch.delenv("MECH_DESIGN_WORKSPACE")

    assert_failure("LEGACY_WORKSPACE_REQUIRED", "setup_required")


@pytest.mark.parametrize("case", ["missing", "directory", "encoding", "syntax", "duplicate"])
def test_explicit_env_file_failures_are_structured_and_do_not_leak_secrets(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_legacy_environment(monkeypatch)
    path = tmp_path / "legacy.env"
    if case == "missing":
        pass
    elif case == "directory":
        path.mkdir()
    elif case == "encoding":
        path.write_bytes(b"\xff\xfe")
    elif case == "syntax":
        path.write_text("MECH_DESIGN_ACTOR_ID='unterminated\n", encoding="utf-8")
    else:
        path.write_text(
            "MECH_DESIGN_ACTOR_ID=one\nMECH_DESIGN_ACTOR_ID=two\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("MECH_DESIGN_ENV_FILE", str(path))
    monkeypatch.setenv("MECH_DESIGN_DATABASE_URL", "postgresql://secret-value")

    captured = assert_failure("LEGACY_ENV_FILE_INVALID", "blocked")

    assert "secret-value" not in captured.value.message


def test_invalid_actor_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    configure_explicit_environment(monkeypatch, layout)
    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "bad actor")

    assert_failure("LEGACY_ACTOR_ID_INVALID", "blocked")


def test_explicit_invalid_workspace_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    configure_explicit_environment(
        monkeypatch,
        layout,
        family="config/product_families/legacy-name.json",
    )
    monkeypatch.setenv("MECH_DESIGN_WORKSPACE", str(tmp_path / "missing-workspace"))

    assert_failure("LEGACY_WORKSPACE_INVALID", "blocked")


@pytest.mark.parametrize("case", ["missing", "directory", "json", "schema"])
def test_invalid_family_config_is_structured(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    invalid = tmp_path / "invalid-family.json"
    if case == "missing":
        pass
    elif case == "directory":
        invalid.mkdir()
    elif case == "json":
        invalid.write_text("{not-json\n", encoding="utf-8")
    else:
        write_family(invalid, {**family_value(), "schema_version": "wrong"})
    configure_explicit_environment(monkeypatch, layout, family=str(invalid))

    assert_failure("LEGACY_FAMILY_CONFIG_INVALID", "blocked")


def test_workspace_relative_family_cannot_escape_with_dotdot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    outside = write_family(tmp_path / "outside/family.json")
    configure_explicit_environment(
        monkeypatch,
        layout,
        family="../outside/family.json",
    )

    assert outside.is_file()
    assert_failure("LEGACY_FAMILY_CONFIG_INVALID", "blocked")


def test_workspace_relative_family_cannot_escape_with_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = legacy_layout(tmp_path)
    outside = write_family(tmp_path / "outside/family.json")
    link = layout["workspace"] / "config/linked-family.json"
    link.symlink_to(outside)
    configure_explicit_environment(
        monkeypatch,
        layout,
        family="config/linked-family.json",
    )

    assert_failure("LEGACY_FAMILY_CONFIG_INVALID", "blocked")


def test_no_implicit_dotenv_search_or_environment_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_legacy_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    dotenv = tmp_path / ".env"
    dotenv_local = tmp_path / ".env.local"
    content = (
        "MECH_DESIGN_FAMILY_CONFIG=should-not-load.json\n"
        "MECH_DESIGN_ACTOR_ID=should-not-load\n"
    )
    dotenv.write_text(content, encoding="utf-8")
    dotenv_local.write_text(content, encoding="utf-8")
    process_before = dict(os.environ)

    assert_failure("LEGACY_FAMILY_CONFIG_REQUIRED", "setup_required")

    assert dict(os.environ) == process_before
    assert dotenv.read_text(encoding="utf-8") == content
    assert dotenv_local.read_text(encoding="utf-8") == content
