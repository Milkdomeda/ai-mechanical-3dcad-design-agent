from __future__ import annotations

from pathlib import Path

import pytest

from mechanical_design_agent.workspace_bootstrap import (
    BootstrapFailure,
    EnvEntry,
    initialize_workspace,
    parse_selected_env_file,
    read_workspace_manifest,
)


def test_env_file_parses_utf8_values_without_mutating_environment(tmp_path: Path) -> None:
    path = tmp_path / "agent.env"
    path.write_text(
        "MECH_DESIGN_WORKSPACE='设计 workspace'\n"
        "MECH_DESIGN_DATABASE_URL=postgresql://localhost/knowledge\n",
        encoding="utf-8",
    )

    parsed = parse_selected_env_file(str(path), {}, tmp_path)

    assert parsed is not None
    assert parsed.values["MECH_DESIGN_WORKSPACE"] == EnvEntry(
        value="设计 workspace", line=1
    )


@pytest.mark.parametrize(
    "contents",
    ["NOT AN ASSIGNMENT\n", "1INVALID=value\n", "KEY=one\nKEY=two\n"],
)
def test_invalid_env_file_is_rejected(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "invalid.env"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(BootstrapFailure):
        parse_selected_env_file(str(path), {}, tmp_path)


def test_workspace_initialization_creates_only_current_runtime_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace with 空格"

    result = initialize_workspace(
        workspace=workspace,
        actor_id="agent-001",
        organization_id="org-001",
        design_group_id="group-001",
        dry_run=False,
    )
    manifest = read_workspace_manifest(workspace)

    assert result.status == "ok"
    assert manifest.actor_id == "agent-001"
    assert manifest.raw["identity"]["organization_id"] == "org-001"
    assert (workspace / "config" / "mechanical_design.json").is_file()
    assert (workspace / "config" / "standard_parts_sources.json").is_file()
    assert (workspace / "data" / "artifacts").is_dir()
    assert not (workspace / "jobs").exists()


def test_workspace_initialization_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = initialize_workspace(
        workspace=workspace, actor_id="agent", dry_run=False
    )
    second = initialize_workspace(
        workspace=workspace, actor_id="agent", dry_run=False
    )

    assert first.result == "initialized"
    assert second.result == "already_initialized"
    assert second.created == ()


def test_partial_knowledge_scope_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BootstrapFailure) as captured:
        initialize_workspace(
            workspace=tmp_path / "workspace",
            actor_id="agent",
            organization_id="org-001",
            design_group_id=None,
            dry_run=False,
        )

    assert captured.value.code == "WORKSPACE_IDENTITY_INCOMPLETE"
