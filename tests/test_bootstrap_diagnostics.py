from __future__ import annotations

from pathlib import Path

from mechanical_design_agent.bootstrap_runtime import BootstrapRuntime
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


def test_uninitialized_status_is_read_only_and_actionable(tmp_path: Path) -> None:
    runtime = BootstrapRuntime.from_process(cwd=tmp_path, environ={})

    status = runtime.status()

    assert status["status"] == {"overall": "setup_required"}
    assert status["components"][0]["code"] == "WORKSPACE_NOT_INITIALIZED"
    assert list(tmp_path.iterdir()) == []


def test_initialized_workspace_does_not_require_knowledge_for_cad(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(
        workspace=workspace,
        actor_id="agent",
        organization_id="org-001",
        design_group_id="group-001",
        dry_run=False,
    )
    runtime = BootstrapRuntime.from_process(cwd=workspace, environ={})

    status = runtime.status()

    knowledge = next(
        item for item in status["components"] if item["name"] == "knowledge"
    )
    assert knowledge["status"] == "warning"
    assert knowledge["code"] == "KNOWLEDGE_OPTIONAL_UNAVAILABLE"


def test_knowledge_scope_is_independent_of_product_family(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(
        workspace=workspace,
        actor_id="agent",
        organization_id="org-001",
        design_group_id="group-001",
        dry_run=False,
    )
    runtime = BootstrapRuntime.from_process(cwd=workspace, environ={})

    assert runtime.design_knowledge_scope() == {
        "organization_id": "org-001",
        "design_group_id": "group-001",
    }


def test_runtime_loads_explicit_environment_file_without_shell_sourcing(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        workspace=tmp_path,
        actor_id="engineer",
        organization_id="org-001",
        design_group_id="group-001",
        dry_run=False,
    )
    env_file = tmp_path / "settings with spaces.env"
    env_file.write_text(
        "MECH_DESIGN_DATABASE_URL=postgresql://example.invalid/knowledge\n"
        "MECH_DESIGN_NEO4J_PASSWORD=secret-value\n",
        encoding="utf-8",
    )

    runtime = BootstrapRuntime.from_process(
        cwd=tmp_path,
        environ={"MECH_DESIGN_ENV_FILE": str(env_file)},
        workspace=tmp_path,
    )
    settings = runtime.knowledge_settings()

    assert settings.database_url == "postgresql://example.invalid/knowledge"
    assert settings.neo4j_password == "secret-value"
