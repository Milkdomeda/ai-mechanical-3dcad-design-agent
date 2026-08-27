from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
import uuid

import pytest

from mechanical_design_agent import cli, workspace_bootstrap as bootstrap
from mechanical_design_agent.product_families import (
    build_product_family_config,
    create_product_family_config,
)
from mechanical_design_agent.secure_fs import (
    SecureFilesystemError,
    validate_managed_path,
)
from mechanical_design_agent.workspace_bootstrap import (
    BootstrapFailure,
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_JOBS_ROOT,
    DEFAULT_PRODUCT_FAMILIES,
    DEFAULT_STANDARD_PART_SOURCES,
    EnvEntry,
    InitResult,
    ParsedEnvFile,
    SettingSource,
    initialize_workspace,
    parse_selected_env_file,
    read_workspace_manifest,
    resolve_setting,
    select_workspace,
)


def write_manifest_for_test(
    workspace: Path,
    *,
    workspace_id: object = "00000000-0000-4000-8000-000000000001",
    actor_id: object = "actor-test",
    artifact_root: object = "data/artifacts",
    jobs_root: object | None = None,
    standard_parts_sources: object = "config/standard_parts_sources.json",
    product_families: object = "config/product_families",
    default_product_family_id: object = None,
    freecad_command: object = None,
    schema_version: object = "MechanicalDesignWorkspace/v1",
) -> Path:
    (workspace / "config" / "product_families").mkdir(parents=True)
    (workspace / "data" / "artifacts").mkdir(parents=True)
    (workspace / "config" / "standard_parts_sources.json").write_text(
        json.dumps(
            {
                "schema_version": "StandardPartSources/v1",
                "verified_local_catalog": {
                    "enabled": False,
                    "global_root": None,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = workspace / "config" / "mechanical_design.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "workspace_id": workspace_id,
                "identity": {
                    "actor_id": actor_id,
                    "organization_id": None,
                    "design_group_id": None,
                },
                "paths": {
                    "artifact_root": artifact_root,
                    "standard_parts_sources": standard_parts_sources,
                    "product_families": product_families,
                    **({"jobs_root": jobs_root} if jobs_root is not None else {}),
                },
                "default_product_family_id": default_product_family_id,
                "freecad": {"command": freecad_command},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def canonical_managed_path(path: Path, *, allow_missing_leaf: bool) -> Path:
    return validate_managed_path(
        path,
        allow_missing_leaf=allow_missing_leaf,
    ).path


def test_env_file_is_unselected_when_no_source_is_configured(tmp_path: Path) -> None:
    assert parse_selected_env_file(None, {}, tmp_path) is None


def test_blank_environment_env_file_is_unselected(tmp_path: Path) -> None:
    assert (
        parse_selected_env_file(
            None,
            {"MECH_DESIGN_ENV_FILE": "  \t"},
            tmp_path,
        )
        is None
    )


def test_blank_runtime_env_file_is_invalid(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file("  ", {}, tmp_path)
    assert captured.value.code == "ENV_FILE_ARGUMENT"


def test_env_file_is_parsed_once_without_mutating_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "MECH_DESIGN_WORKSPACE='portable workspace'\n"
        "MECH_DESIGN_ACTOR_ID=agent-one\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "process-actor")
    before = dict(os.environ)

    parsed = parse_selected_env_file(str(env_file), os.environ, tmp_path)

    assert parsed is not None
    assert parsed.path == env_file.resolve()
    assert parsed.values["MECH_DESIGN_WORKSPACE"] == EnvEntry(
        value="portable workspace",
        line=1,
    )
    assert parsed.values["MECH_DESIGN_ACTOR_ID"] == EnvEntry(
        value="agent-one",
        line=2,
    )
    assert dict(os.environ) == before


def test_process_env_file_selection_resolves_relative_to_cwd(tmp_path: Path) -> None:
    env_file = tmp_path / "agent.env"
    env_file.write_text("KEY=value\n", encoding="utf-8")

    parsed = parse_selected_env_file(
        None,
        {"MECH_DESIGN_ENV_FILE": "agent.env"},
        tmp_path,
    )

    assert parsed is not None
    assert parsed.path == env_file.resolve()


def test_runtime_env_file_selection_wins_over_process_selection(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runtime.env"
    process_file = tmp_path / "process.env"
    runtime_file.write_text("KEY=runtime\n", encoding="utf-8")
    process_file.write_text("KEY=process\n", encoding="utf-8")

    parsed = parse_selected_env_file(
        runtime_file.name,
        {"MECH_DESIGN_ENV_FILE": str(process_file)},
        tmp_path,
    )

    assert parsed is not None
    assert parsed.path == runtime_file.resolve()
    assert parsed.values["KEY"] == EnvEntry(value="runtime", line=1)


def test_env_file_accepts_comments_quotes_literals_empty_values_and_unknown_keys(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "values.env"
    env_file.write_text(
        "# comment\n"
        "  # indented comment\n"
        "\n"
        "UNQUOTED=literal value\n"
        "SINGLE='single quoted'\n"
        'DOUBLE="double quoted"\n'
        "EMPTY=\n"
        "UNKNOWN_SETTING=value#literal\n",
        encoding="utf-8",
    )

    parsed = parse_selected_env_file(str(env_file), {}, tmp_path)

    assert parsed is not None
    assert {key: entry.value for key, entry in parsed.values.items()} == {
        "UNQUOTED": "literal value",
        "SINGLE": "single quoted",
        "DOUBLE": "double quoted",
        "EMPTY": "",
        "UNKNOWN_SETTING": "value#literal",
    }
    assert parsed.values["UNQUOTED"].line == 4


def test_env_file_does_not_recursively_load_another_env_file(tmp_path: Path) -> None:
    nested = tmp_path / "nested.env"
    selected = tmp_path / "selected.env"
    nested.write_text("NESTED=must-not-load\n", encoding="utf-8")
    selected.write_text(
        f"MECH_DESIGN_ENV_FILE={nested}\nSELECTED=loaded\n",
        encoding="utf-8",
    )

    parsed = parse_selected_env_file(str(selected), {}, tmp_path)

    assert parsed is not None
    assert set(parsed.values) == {"MECH_DESIGN_ENV_FILE", "SELECTED"}
    assert "NESTED" not in parsed.values


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        ("NOT AN ASSIGNMENT\n", "ENV_FILE_SYNTAX"),
        ("1INVALID=value\n", "ENV_FILE_SYNTAX"),
        ("KEY='unterminated\n", "ENV_FILE_SYNTAX"),
        ('KEY="unterminated\n', "ENV_FILE_SYNTAX"),
        ("KEY=value'\n", "ENV_FILE_SYNTAX"),
        ("KEY=one\nKEY=two\n", "ENV_FILE_DUPLICATE_KEY"),
        ("KEY=one\n KEY =two\n", "ENV_FILE_DUPLICATE_KEY"),
    ],
)
def test_invalid_env_file_is_blocked(
    tmp_path: Path,
    contents: str,
    code: str,
) -> None:
    env_file = tmp_path / "invalid.env"
    env_file.write_text(contents, encoding="utf-8")

    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file(str(env_file), {}, tmp_path)

    assert captured.value.code == code
    assert captured.value.status == "blocked"


def test_missing_env_file_is_blocked(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file(str(missing), {}, tmp_path)
    assert captured.value.code == "ENV_FILE_UNREADABLE"


def test_env_file_directory_is_blocked(tmp_path: Path) -> None:
    directory = tmp_path / "directory.env"
    directory.mkdir()
    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file(str(directory), {}, tmp_path)
    assert captured.value.code == "ENV_FILE_UNREADABLE"


def test_non_utf8_env_file_is_blocked(tmp_path: Path) -> None:
    env_file = tmp_path / "invalid-encoding.env"
    env_file.write_bytes(b"KEY=\xff\n")
    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file(str(env_file), {}, tmp_path)
    assert captured.value.code == "ENV_FILE_ENCODING"


def test_unreadable_env_file_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "unreadable.env"
    env_file.write_text("KEY=value\n", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_selected_file(path: Path, *args: object, **kwargs: object) -> str:
        if path == env_file.resolve():
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_selected_file)
    with pytest.raises(BootstrapFailure) as captured:
        parse_selected_env_file(str(env_file), {}, tmp_path)
    assert captured.value.code == "ENV_FILE_UNREADABLE"


def test_final_override_uses_runtime_before_every_other_source(tmp_path: Path) -> None:
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType({"SETTING": EnvEntry("env-file", 7)}),
    )
    resolved = resolve_setting(
        environment_key="SETTING",
        runtime_value="runtime",
        environ={"SETTING": "process"},
        env_file=parsed,
        manifest_value="manifest",
        package_default="logical-default",
    )
    assert resolved.value == "runtime"
    assert resolved.source == SettingSource(kind="runtime")


def test_final_override_uses_process_environment_before_env_file(tmp_path: Path) -> None:
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType({"SETTING": EnvEntry("env-file", 7)}),
    )
    resolved = resolve_setting(
        environment_key="SETTING",
        runtime_value=None,
        environ={"SETTING": "process"},
        env_file=parsed,
        manifest_value="manifest",
        package_default="logical-default",
    )
    assert resolved.value == "process"
    assert resolved.source == SettingSource(kind="process_environment")


def test_final_override_preserves_env_file_source_line(tmp_path: Path) -> None:
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType({"SETTING": EnvEntry("env-file", 7)}),
    )
    resolved = resolve_setting(
        environment_key="SETTING",
        runtime_value=None,
        environ={},
        env_file=parsed,
        manifest_value="manifest",
        package_default="logical-default",
    )
    assert resolved.value == "env-file"
    assert resolved.source == SettingSource(
        kind="env_file",
        location=str(parsed.path),
        line=7,
    )


def test_final_override_uses_manifest_then_logical_package_default() -> None:
    from_manifest = resolve_setting(
        environment_key="SETTING",
        runtime_value=None,
        environ={},
        env_file=None,
        manifest_value="manifest",
        package_default="logical-default",
    )
    from_default = resolve_setting(
        environment_key="SETTING",
        runtime_value=None,
        environ={},
        env_file=None,
        manifest_value=None,
        package_default="logical-default",
    )
    assert from_manifest.value == "manifest"
    assert from_manifest.source == SettingSource(kind="manifest")
    assert from_default.value == "logical-default"
    assert from_default.source == SettingSource(kind="package_default")


def test_package_path_defaults_are_logical_workspace_relative_values() -> None:
    assert DEFAULT_ARTIFACT_ROOT == "data/artifacts"
    assert DEFAULT_JOBS_ROOT == "jobs"
    assert DEFAULT_STANDARD_PART_SOURCES == "config/standard_parts_sources.json"
    assert DEFAULT_PRODUCT_FAMILIES == "config/product_families"
    assert not Path(DEFAULT_ARTIFACT_ROOT).is_absolute()
    assert not Path(DEFAULT_JOBS_ROOT).is_absolute()
    assert not Path(DEFAULT_STANDARD_PART_SOURCES).is_absolute()
    assert not Path(DEFAULT_PRODUCT_FAMILIES).is_absolute()


def test_v1_manifest_without_jobs_root_uses_portable_default(tmp_path: Path) -> None:
    workspace = tmp_path / "legacy workspace"
    write_manifest_for_test(workspace)
    manifest = read_workspace_manifest(workspace)
    assert manifest.jobs_root == manifest.workspace / "jobs"


def test_init_creates_jobs_root(tmp_path: Path) -> None:
    workspace = tmp_path / "机械设计 workspace"
    result = initialize_workspace(workspace=workspace, actor_id="actor-1", dry_run=False)
    assert (workspace / "jobs").is_dir()
    assert "jobs" in result.created


def test_init_upgrades_legacy_manifest_jobs_root_without_rewriting_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "legacy workspace"
    write_manifest_for_test(workspace, jobs_root="var/design-jobs")
    manifest_path = workspace / "config/mechanical_design.json"
    before = manifest_path.read_bytes()

    result = initialize_workspace(
        workspace=workspace,
        actor_id="actor-test",
        dry_run=False,
    )

    assert (workspace / "var/design-jobs").is_dir()
    assert result.created == ("var/design-jobs",)
    assert "var/design-jobs" not in result.reused
    assert manifest_path.read_bytes() == before


def test_workspace_selection_uses_runtime_before_process_and_env_file(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    process = tmp_path / "process"
    env_workspace = tmp_path / "env-file"
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType(
            {"MECH_DESIGN_WORKSPACE": EnvEntry(str(env_workspace), 3)}
        ),
    )
    selected = select_workspace(
        runtime_workspace=runtime,
        environ={"MECH_DESIGN_WORKSPACE": str(process)},
        env_file=parsed,
        cwd=tmp_path,
        require_manifest=False,
    )
    assert selected.path == canonical_managed_path(
        runtime,
        allow_missing_leaf=True,
    )
    assert selected.source == SettingSource(kind="runtime")


def test_workspace_selection_uses_process_before_env_file(tmp_path: Path) -> None:
    process = tmp_path / "process"
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType(
            {"MECH_DESIGN_WORKSPACE": EnvEntry("env-workspace", 3)}
        ),
    )
    selected = select_workspace(
        runtime_workspace=None,
        environ={"MECH_DESIGN_WORKSPACE": str(process)},
        env_file=parsed,
        cwd=tmp_path,
        require_manifest=False,
    )
    assert selected.path == canonical_managed_path(
        process,
        allow_missing_leaf=True,
    )
    assert selected.source == SettingSource(kind="process_environment")


def test_workspace_selection_uses_env_file_and_resolves_relative_to_cwd(
    tmp_path: Path,
) -> None:
    parsed = ParsedEnvFile(
        path=tmp_path / "agent.env",
        values=MappingProxyType(
            {"MECH_DESIGN_WORKSPACE": EnvEntry("env-workspace", 3)}
        ),
    )
    selected = select_workspace(
        runtime_workspace=None,
        environ={},
        env_file=parsed,
        cwd=tmp_path,
        require_manifest=False,
    )
    assert selected.path == canonical_managed_path(
        tmp_path / "env-workspace",
        allow_missing_leaf=True,
    )
    assert selected.source == SettingSource(
        kind="env_file",
        location=str(parsed.path),
        line=3,
    )


def test_nearest_parent_manifest_wins_when_workspace_is_not_explicit(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    project = outer / "project"
    inner = project / "child"
    write_manifest_for_test(outer)
    write_manifest_for_test(project)
    inner.mkdir(parents=True)

    selected = select_workspace(
        runtime_workspace=None,
        environ={},
        env_file=None,
        cwd=inner,
        require_manifest=True,
    )

    assert selected.path == canonical_managed_path(
        project,
        allow_missing_leaf=False,
    )
    assert selected.source == SettingSource(kind="nearest_parent")


def test_missing_workspace_selection_is_setup_required(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        select_workspace(
            runtime_workspace=None,
            environ={},
            env_file=None,
            cwd=tmp_path,
            require_manifest=False,
        )
    assert captured.value.code == "WORKSPACE_NOT_SELECTED"
    assert captured.value.status == "setup_required"


def test_blank_runtime_workspace_is_blocked(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        select_workspace(
            runtime_workspace=" ",
            environ={},
            env_file=None,
            cwd=tmp_path,
            require_manifest=False,
        )
    assert captured.value.code == "WORKSPACE_ARGUMENT"


def test_required_workspace_manifest_must_exist(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        select_workspace(
            runtime_workspace=tmp_path / "not-initialized",
            environ={},
            env_file=None,
            cwd=tmp_path,
            require_manifest=True,
        )
    assert captured.value.code == "WORKSPACE_NOT_INITIALIZED"
    assert captured.value.status == "setup_required"


def test_manifest_loads_typed_identity_and_workspace_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    write_manifest_for_test(
        workspace,
        default_product_family_id="family-one",
        freecad_command="FreeCADCmd",
    )

    manifest = read_workspace_manifest(workspace)

    assert str(manifest.workspace_id) == "00000000-0000-4000-8000-000000000001"
    assert manifest.actor_id == "actor-test"
    assert manifest.artifact_root == canonical_managed_path(
        workspace / "data/artifacts",
        allow_missing_leaf=False,
    )
    assert manifest.standard_parts_sources == canonical_managed_path(
        workspace / "config/standard_parts_sources.json",
        allow_missing_leaf=False,
    )
    assert manifest.product_families == canonical_managed_path(
        workspace / "config/product_families",
        allow_missing_leaf=False,
    )
    assert manifest.default_product_family_id == "family-one"
    assert manifest.freecad_command == "FreeCADCmd"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"schema_version": "unsupported/v1"}, "MANIFEST_INVALID"),
        ({"workspace_id": "not-a-uuid"}, "MANIFEST_INVALID"),
        ({"actor_id": ""}, "ACTOR_ID_INVALID"),
        ({"actor_id": " actor"}, "ACTOR_ID_INVALID"),
        ({"actor_id": "actor/unsafe"}, "ACTOR_ID_INVALID"),
        ({"actor_id": "é"}, "ACTOR_ID_INVALID"),
        ({"actor_id": "a" * 129}, "ACTOR_ID_INVALID"),
        ({"default_product_family_id": 1}, "MANIFEST_INVALID"),
        ({"freecad_command": 1}, "MANIFEST_INVALID"),
    ],
)
def test_manifest_rejects_invalid_schema_and_identity_fields(
    tmp_path: Path,
    overrides: dict[str, object],
    code: str,
) -> None:
    workspace = tmp_path / "workspace"
    write_manifest_for_test(workspace, **overrides)
    with pytest.raises(BootstrapFailure) as captured:
        read_workspace_manifest(workspace)
    assert captured.value.code == code


@pytest.mark.parametrize(
    "field",
    ["artifact_root", "standard_parts_sources", "product_families"],
)
def test_manifest_rejects_managed_path_escape(tmp_path: Path, field: str) -> None:
    workspace = tmp_path / "workspace"
    write_manifest_for_test(workspace, **{field: "../outside"})
    with pytest.raises(BootstrapFailure) as captured:
        read_workspace_manifest(workspace)
    assert captured.value.code == "MANIFEST_PATH_ESCAPE"


def test_manifest_rejects_workspace_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    write_manifest_for_test(target)
    link = tmp_path / "workspace-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(BootstrapFailure) as captured:
        read_workspace_manifest(link)
    assert captured.value.code == "WORKSPACE_SYMLINK"


def test_manifest_rejects_manifest_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    original = write_manifest_for_test(workspace)
    external = tmp_path / "external-manifest.json"
    original.replace(external)
    original.symlink_to(external)
    with pytest.raises(BootstrapFailure) as captured:
        read_workspace_manifest(workspace)
    assert captured.value.code == "MANIFEST_SYMLINK"


def test_init_dry_run_creates_nothing_and_reserves_no_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"

    def reject_uuid_generation() -> uuid.UUID:
        raise AssertionError("dry-run must not generate persistent IDs")

    monkeypatch.setattr(bootstrap.uuid, "uuid4", reject_uuid_generation)
    result = initialize_workspace(
        workspace=workspace,
        actor_id=None,
        dry_run=True,
    )

    assert isinstance(result, InitResult)
    assert result.status == "ok"
    assert result.result == "dry_run"
    assert result.workspace == canonical_managed_path(
        workspace,
        allow_missing_leaf=True,
    )
    assert not workspace.exists()


def test_init_creates_exact_portable_workspace_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = initialize_workspace(
        workspace=workspace,
        actor_id=None,
        dry_run=False,
    )

    assert result.status == "ok"
    assert result.result == "initialized"
    assert sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
    ) == [
        "config",
        "config/mechanical_design.json",
        "config/product_families",
        "config/standard_parts_sources.json",
        "data",
        "data/artifacts",
        "jobs",
    ]
    assert not (workspace / "output").exists()
    assert not (workspace / "knowledge").exists()
    assert list((workspace / "config/product_families").iterdir()) == []

    manifest_path = workspace / "config/mechanical_design.json"
    manifest_bytes = manifest_path.read_bytes()
    assert manifest_bytes.endswith(b"\n")
    manifest = json.loads(manifest_bytes)
    assert manifest_bytes == (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    assert uuid.UUID(manifest["workspace_id"]).version == 4
    assert manifest["identity"]["actor_id"].startswith("actor-")
    assert uuid.UUID(manifest["identity"]["actor_id"].removeprefix("actor-")).version == 4
    assert manifest["default_product_family_id"] is None
    assert manifest["freecad"] == {"command": None, "sha256": None}
    assert manifest["paths"] == {
        "artifact_root": "data/artifacts",
        "jobs_root": "jobs",
        "product_families": "config/product_families",
        "standard_parts_sources": "config/standard_parts_sources.json",
    }

    sources_bytes = (workspace / "config/standard_parts_sources.json").read_bytes()
    sources = json.loads(sources_bytes)
    assert sources_bytes == (
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    assert sources == {
        "schema_version": "StandardPartSources/v1",
        "verified_local_catalog": {"enabled": False, "global_root": None},
    }


@pytest.mark.parametrize(
    "actor_id",
    [
        "a",
        "actor.explicit_1-test",
        "a" * 128,
    ],
)
def test_init_accepts_explicit_safe_actor_ids(
    tmp_path: Path,
    actor_id: str,
) -> None:
    workspace = tmp_path / actor_id[:20]
    initialize_workspace(workspace=workspace, actor_id=actor_id, dry_run=False)
    manifest = json.loads(
        (workspace / "config/mechanical_design.json").read_text(encoding="utf-8")
    )
    assert manifest["identity"]["actor_id"] == actor_id


def test_init_can_establish_design_scope_without_a_product_family(tmp_path: Path) -> None:
    workspace = tmp_path / "neutral-scope"

    initialize_workspace(
        workspace=workspace,
        actor_id="actor-neutral",
        dry_run=False,
        organization_id="org-neutral",
        design_group_id="group-neutral",
    )

    manifest = json.loads(
        (workspace / "config/mechanical_design.json").read_text(encoding="utf-8")
    )
    assert manifest["identity"] == {
        "actor_id": "actor-neutral",
        "organization_id": "org-neutral",
        "design_group_id": "group-neutral",
    }
    assert manifest["default_product_family_id"] is None
    assert list((workspace / "config/product_families").iterdir()) == []


def test_init_requires_complete_design_scope(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(
            workspace=tmp_path / "incomplete-scope",
            actor_id="actor-neutral",
            dry_run=False,
            organization_id="org-neutral",
        )

    assert captured.value.code == "WORKSPACE_IDENTITY_INCOMPLETE"
    assert not (tmp_path / "incomplete-scope").exists()


@pytest.mark.parametrize(
    "actor_id",
    [
        "",
        " actor",
        "actor ",
        "actor/unsafe",
        "é",
        "a" * 129,
    ],
)
def test_init_rejects_invalid_actor_before_writing(
    tmp_path: Path,
    actor_id: str,
) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=actor_id, dry_run=False)
    assert captured.value.code == "ACTOR_ID_INVALID"
    assert not workspace.exists()


def test_init_is_idempotent_and_environment_actor_never_changes_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    first = initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    manifest_path = workspace / "config/mechanical_design.json"
    sources_path = workspace / "config/standard_parts_sources.json"
    before_manifest = manifest_path.read_bytes()
    before_sources = sources_path.read_bytes()
    before_mtimes = {
        manifest_path: manifest_path.stat().st_mtime_ns,
        sources_path: sources_path.stat().st_mtime_ns,
    }
    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "process-only-actor")

    second = initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert first.result == "initialized"
    assert second.result == "already_initialized"
    assert manifest_path.read_bytes() == before_manifest
    assert sources_path.read_bytes() == before_sources
    assert manifest_path.stat().st_mtime_ns == before_mtimes[manifest_path]
    assert sources_path.stat().st_mtime_ns == before_mtimes[sources_path]


def test_same_explicit_actor_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-one", dry_run=False)
    result = initialize_workspace(
        workspace=workspace,
        actor_id="actor-one",
        dry_run=False,
    )
    assert result.result == "already_initialized"


def test_different_explicit_actor_blocks_without_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-one", dry_run=False)
    manifest_path = workspace / "config/mechanical_design.json"
    before = manifest_path.read_bytes()

    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(
            workspace=workspace,
            actor_id="actor-two",
            dry_run=False,
        )

    assert captured.value.code == "ACTOR_ID_CONFLICT"
    assert manifest_path.read_bytes() == before


def test_init_publishes_manifest_after_other_managed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    published: list[str] = []
    original_publish = bootstrap._publish_new_file

    def record_publish(path: Path, content: bytes) -> None:
        published.append(path.name)
        original_publish(path, content)

    monkeypatch.setattr(bootstrap, "_publish_new_file", record_publish)
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    assert published == ["standard_parts_sources.json", "mechanical_design.json"]


def test_failed_manifest_publish_leaves_ids_without_persistent_meaning_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    generated = iter(
        [
            uuid.UUID("00000000-0000-4000-8000-000000000011"),
            uuid.UUID("00000000-0000-4000-8000-000000000012"),
            uuid.UUID("00000000-0000-4000-8000-000000000021"),
            uuid.UUID("00000000-0000-4000-8000-000000000022"),
        ]
    )
    monkeypatch.setattr(bootstrap.uuid, "uuid4", lambda: next(generated))
    original_publish = bootstrap._publish_new_file

    def fail_manifest(path: Path, content: bytes) -> None:
        if path.name == "mechanical_design.json":
            raise BootstrapFailure("INJECTED_FAILURE", "manifest publish failed")
        original_publish(path, content)

    monkeypatch.setattr(bootstrap, "_publish_new_file", fail_manifest)
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    assert captured.value.code == "INJECTED_FAILURE"
    assert not (workspace / "config/mechanical_design.json").exists()
    assert (workspace / "config/standard_parts_sources.json").is_file()
    assert not (workspace / ".mechanical-design-init.lock").exists()

    monkeypatch.setattr(bootstrap, "_publish_new_file", original_publish)
    result = initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    assert result.result == "initialized"
    manifest = json.loads(
        (workspace / "config/mechanical_design.json").read_text(encoding="utf-8")
    )
    assert manifest["workspace_id"] == "00000000-0000-4000-8000-000000000021"
    assert manifest["identity"]["actor_id"] == (
        "actor-00000000-0000-4000-8000-000000000022"
    )


def test_partial_conflicting_managed_file_is_never_overwritten(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sources = workspace / "config/standard_parts_sources.json"
    sources.parent.mkdir(parents=True)
    sources.write_bytes(b"user-owned-content\n")

    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert captured.value.code == "MANAGED_FILE_CONFLICT"
    assert sources.read_bytes() == b"user-owned-content\n"
    assert not (workspace / "config/mechanical_design.json").exists()


def test_initialized_workspace_missing_managed_directory_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    (workspace / "config/product_families").rmdir()

    original_validate = bootstrap.validate_managed_path

    def windows_missing_path_failure(path: Path, *, allow_missing_leaf: bool):
        if Path(path).name == "product_families" and not allow_missing_leaf:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed path does not exist",
            )
        return original_validate(path, allow_missing_leaf=allow_missing_leaf)

    monkeypatch.setattr(
        bootstrap,
        "validate_managed_path",
        windows_missing_path_failure,
    )

    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert captured.value.code == "MANAGED_CONFIG_INVALID"


def test_init_rejects_managed_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.mkdir()
    (workspace / "config").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert captured.value.code == "MANAGED_PATH_SYMLINK"
    assert list(outside.iterdir()) == []


def test_init_lock_conflict_is_blocked_and_preserved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = workspace / ".mechanical-design-init.lock"
    lock.write_text("another initializer\n", encoding="utf-8")

    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert captured.value.code == "INIT_LOCKED"
    assert lock.read_text(encoding="utf-8") == "another initializer\n"


def test_atomic_publish_failure_cleans_temporary_and_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"

    def fail_link(source: object, target: object) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(bootstrap, "atomic_publish_new", fail_link)
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)

    assert captured.value.code == "ATOMIC_WRITE_FAILED"
    assert not (workspace / ".mechanical-design-init.lock").exists()
    assert not list(workspace.rglob(".standard_parts_sources.json.*"))


def test_init_preserves_unrelated_existing_workspace_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unrelated = workspace / "user-file.txt"
    unrelated.write_text("preserve me\n", encoding="utf-8")
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    assert unrelated.read_text(encoding="utf-8") == "preserve me\n"


def test_init_rejects_workspace_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    assert captured.value.code == "WORKSPACE_INVALID"


def _clear_cli_bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MECH_DESIGN_WORKSPACE",
        "MECH_DESIGN_ENV_FILE",
        "MECH_DESIGN_ACTOR_ID",
        "MECH_DESIGN_DATABASE_URL",
        "MECH_DESIGN_NEO4J_URI",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
        "MECH_DESIGN_FREECADCMD",
        "MECH_DESIGN_ARTIFACT_ROOT",
        "MECH_DESIGN_PRODUCT_FAMILY_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cli_init_is_isolated_from_operational_settings_and_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.setattr(
        cli.Settings,
        "from_environment",
        classmethod(lambda cls: pytest.fail("operational settings were loaded")),
    )

    class UnexpectedService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("operational service was constructed")

    monkeypatch.setattr(cli, "MechanicalDesignService", UnexpectedService)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "init", "--workspace", str(workspace)],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "MechanicalDesignWorkspaceInit/v1"
    assert payload["status"] == "ok"
    assert payload["result"] == "initialized"
    assert workspace.is_dir()


def test_cli_init_dry_run_does_not_create_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "init",
            "--workspace",
            str(workspace),
            "--dry-run",
        ],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "dry_run"
    assert not workspace.exists()


def test_cli_init_selects_workspace_from_explicit_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "MECH_DESIGN_WORKSPACE=workspace-from-env\n",
        encoding="utf-8",
    )
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "init", "--env-file", env_file.name],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "initialized"
    assert Path(payload["workspace"]) == canonical_managed_path(
        tmp_path / "workspace-from-env",
        allow_missing_leaf=False,
    )


def test_cli_init_is_idempotent_and_process_actor_does_not_override_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "init", "--workspace", str(workspace)],
    )
    cli.main()
    first = json.loads(capsys.readouterr().out)
    manifest_path = workspace / "config/mechanical_design.json"
    manifest_before = manifest_path.read_bytes()
    mtime_before = manifest_path.stat().st_mtime_ns

    monkeypatch.setenv("MECH_DESIGN_ACTOR_ID", "process-only-actor")
    cli.main()
    second = json.loads(capsys.readouterr().out)

    assert first["result"] == "initialized"
    assert second["result"] == "already_initialized"
    assert manifest_path.read_bytes() == manifest_before
    assert manifest_path.stat().st_mtime_ns == mtime_before


def test_cli_init_actor_conflict_is_blocked_with_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "init",
            "--workspace",
            str(workspace),
            "--actor-id",
            "actor-one",
        ],
    )
    cli.main()
    capsys.readouterr()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "init",
            "--workspace",
            str(workspace),
            "--actor-id",
            "actor-two",
        ],
    )

    with pytest.raises(SystemExit) as captured:
        cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert captured.value.code == 3
    assert payload["status"] == "blocked"
    assert payload["code"] == "ACTOR_ID_CONFLICT"


def test_cli_init_without_workspace_is_setup_required_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mechanical-design", "init"])

    with pytest.raises(SystemExit) as captured:
        cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert captured.value.code == 2
    assert payload["status"] == "setup_required"
    assert payload["code"] == "WORKSPACE_NOT_SELECTED"


def test_cli_init_missing_explicit_env_file_is_blocked_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_cli_bootstrap_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "init", "--env-file", "missing.env"],
    )

    with pytest.raises(SystemExit) as captured:
        cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert captured.value.code == 3
    assert payload["status"] == "blocked"
    assert payload["code"] == "ENV_FILE_UNREADABLE"


def test_cli_help_lists_explicit_init_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["mechanical-design", "--help"])

    with pytest.raises(SystemExit) as captured:
        cli.main()

    assert captured.value.code == 0
    assert "init" in capsys.readouterr().out


def _fail_on_cli_operational_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.Settings,
        "from_environment",
        classmethod(lambda cls: pytest.fail("operational settings were loaded")),
    )

    class UnexpectedConstruction:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("operational dependency was constructed")

    def unexpected_resource(*args: object, **kwargs: object) -> None:
        pytest.fail("migration or smoke resource was accessed")

    monkeypatch.setattr(cli, "MechanicalDesignService", UnexpectedConstruction)
    monkeypatch.setattr(cli, "PostgresRepository", UnexpectedConstruction)
    monkeypatch.setattr(cli, "postgres_migrations_directory", unexpected_resource)
    monkeypatch.setattr(cli, "run_test_fixture", unexpected_resource)


@pytest.mark.parametrize(
    "command",
    [
        ("status",),
        ("doctor",),
        ("config", "show"),
    ],
)
def test_uninitialized_diagnostic_cli_is_structured_and_does_not_construct_runtime(
    command: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_cli_bootstrap_environment(monkeypatch)
    _fail_on_cli_operational_construction(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *command])

    with pytest.raises(SystemExit) as captured:
        cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert captured.value.code == 2
    assert payload["schema_version"] == "MechanicalDesignDiagnostics/v1"
    assert payload["status"] == {"overall": "setup_required"}
    assert list(tmp_path.iterdir()) == []


def test_fresh_workspace_cli_status_config_and_doctor_have_exact_exit_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    _clear_cli_bootstrap_environment(monkeypatch)
    _fail_on_cli_operational_construction(monkeypatch)

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "status", "--workspace", str(workspace)],
    )
    cli.main()
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == {"overall": "ok"}

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "config", "show", "--workspace", str(workspace)],
    )
    cli.main()
    config = json.loads(capsys.readouterr().out)
    assert config["schema_version"] == "MechanicalDesignConfig/v1"
    assert config["status"] == "ok"

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "doctor", "--workspace", str(workspace)],
    )
    with pytest.raises(SystemExit) as captured:
        cli.main()
    doctor = json.loads(capsys.readouterr().out)
    assert captured.value.code == 2
    assert doctor["status"] == {"overall": "setup_required"}


@pytest.mark.parametrize(
    "arguments",
    [
        ("migrate",),
        ("bootstrap",),
        ("register-library", "/not-accessed"),
        ("scan",),
        ("project",),
        ("rebuild-projection", "--confirmation", "not-used"),
        ("smoke-fixture",),
    ],
)
def test_uninitialized_operational_cli_stops_at_structured_bootstrap_guard(
    arguments: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_cli_bootstrap_environment(monkeypatch)
    _fail_on_cli_operational_construction(monkeypatch)
    monkeypatch.chdir(tmp_path)
    expected_entries: list[Path] = []
    if arguments[0] == "smoke-fixture":
        source = tmp_path / "fixture.FCStd"
        source.write_bytes(b"synthetic source")
        arguments = (*arguments, "--source", str(source))
        expected_entries.append(source)
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *arguments])

    with pytest.raises(SystemExit) as captured:
        cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert captured.value.code == 2
    assert payload["schema_version"] == "MechanicalDesignSetupResponse/v1"
    assert payload["status"] == "setup_required"
    assert payload["code"] == "WORKSPACE_NOT_INITIALIZED"
    assert list(tmp_path.iterdir()) == expected_entries


def test_cli_diagnostic_help_lists_bootstrap_selection_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in (("status",), ("doctor",), ("config", "show")):
        monkeypatch.setattr(sys, "argv", ["mechanical-design", *command, "--help"])
        with pytest.raises(SystemExit) as captured:
            cli.main()
        assert captured.value.code == 0
        output = capsys.readouterr().out
        assert "--workspace" in output
        assert "--env-file" in output


def test_family_cli_first_use_lifecycle_is_bootstrap_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-cli", dry_run=False)
    _clear_cli_bootstrap_environment(monkeypatch)
    _fail_on_cli_operational_construction(monkeypatch)

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "family", "list", "--workspace", str(workspace)],
    )
    cli.main()
    empty = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "family",
            "create",
            "--workspace",
            str(workspace),
            "--organization-id",
            "org-001",
            "--organization-name",
            "Example organization",
            "--design-group-id",
            "group-001",
            "--design-group-name",
            "Example group",
            "--family-id",
            "family-001",
            "--family-name",
            "Example family",
            "--alias",
            "Alias",
        ],
    )
    cli.main()
    created = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "family", "active", "--workspace", str(workspace)],
    )
    with pytest.raises(SystemExit) as unselected_exit:
        cli.main()
    unselected = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "family",
            "set-default",
            "family-001",
            "--workspace",
            str(workspace),
        ],
    )
    cli.main()
    default = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        ["mechanical-design", "family", "active", "--workspace", str(workspace)],
    )
    cli.main()
    selected = json.loads(capsys.readouterr().out)

    assert empty["state"] == "empty"
    assert created["result"] == "created"
    assert unselected_exit.value.code == 2
    assert unselected["code"] == "PRODUCT_FAMILY_SELECTION_REQUIRED"
    assert default["result"] == "updated"
    assert selected["family_id"] == "family-001"
    assert selected["source"]["kind"] == "manifest"


def test_family_cli_runtime_override_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id="actor-cli", dry_run=False)
    manifest = read_workspace_manifest(workspace)
    create_product_family_config(
        manifest=manifest,
        config=build_product_family_config(
            organization_id="org-001",
            organization_name="Example organization",
            design_group_id="group-001",
            design_group_name="Example group",
            family_id="family-001",
            family_name="Example family",
            aliases=[],
            actor_id=manifest.actor_id,
        ),
    )
    manifest_path = workspace / "config/mechanical_design.json"
    before = manifest_path.read_bytes()
    _clear_cli_bootstrap_environment(monkeypatch)
    _fail_on_cli_operational_construction(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechanical-design",
            "family",
            "active",
            "--workspace",
            str(workspace),
            "--product-family",
            "family-001",
        ],
    )

    cli.main()

    selected = json.loads(capsys.readouterr().out)
    assert selected["family_id"] == "family-001"
    assert selected["source"]["kind"] == "runtime"
    assert manifest_path.read_bytes() == before
