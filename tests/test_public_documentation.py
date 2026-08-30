from __future__ import annotations

import importlib.resources
import json
import re
from pathlib import Path

from mechanical_design_agent.product_families import (
    build_product_family_config,
    validate_product_family_config,
)
from mechanical_design_agent.workspace_bootstrap import initialize_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
ARCHITECTURE = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
DATABASE_DEPLOYMENT_GUIDE = PROJECT_ROOT / "docs" / "DATABASE_DEPLOYMENT.md"
FREECAD_GUI_MCP_GUIDE = PROJECT_ROOT / "docs" / "FREECAD_GUI_MCP_INTEGRATION.md"
WINDOWS_RELEASE_GUIDE = PROJECT_ROOT / "docs" / "WINDOWS_RELEASE_ACCEPTANCE.md"
DESIGN_JOB_GUIDE = PROJECT_ROOT / "docs" / "DESIGN_JOB_WORKSPACES.md"
ENGINEER_LEARNING_GUIDE = PROJECT_ROOT / "docs" / "ENGINEER_LEARNING_PLAYBOOK.md"
EXAMPLE_FAMILY = (
    PROJECT_ROOT / "examples" / "product_families" / "example-family.json"
)
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_public_identity_and_release_boundary() -> None:
    text = normalized(README)
    assert text.startswith("# AI Mechanical 3DCAD Design Agent")
    assert "coding agent" in text.lower()
    assert "does not include an embedded language-model client" in text.lower()
    assert (
        "standalone llm orchestration is not included in version 0.6.1"
        in text.lower()
    )
    assert "ai-mechanical-3dcad-design-agent" in text


def test_lightweight_design_is_public_and_governance_is_optional() -> None:
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    skill = (
        PROJECT_ROOT
        / ".agents"
        / "skills"
        / "mechanical-design-job-workspace"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "## Default lightweight design workflow" in agents
    assert "one natural-language approval" in agents
    assert "Do not create a Design Job" in agents
    assert "design_start" in agents
    assert "design_record_result" in agents
    assert "optional `governed` profile" in agents
    assert "mechanical-design-job-workspace" in readme
    assert "Three project-owned skills" in readme
    assert "`superpowers:brainstorming`" in readme
    assert "optional" in readme.casefold()
    assert "not bundled" in readme.casefold()
    assert "not installed" in normalized(README).casefold()
    assert "Use this Skill only for the explicit `governed`" in skill
    assert "Do not use it" in skill


def test_design_lesson_single_confirmation_contract_is_public() -> None:
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    skill = (
        PROJECT_ROOT
        / ".agents"
        / "skills"
        / "mechanical-design-job-workspace"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    playbook = ENGINEER_LEARNING_GUIDE.read_text(encoding="utf-8")
    combined = "\n".join((agents, skill, playbook))

    for fragment in (
        "确认发布设计经验",
        "确认无可发布设计经验",
        "design_lesson_review_publish",
        "design_lesson_review_no_publish",
        "reviewed-no-publishable-lesson",
    ):
        assert fragment in combined
    assert "Model confirmation and Design Lesson publication are separate" in agents
    assert "Never combine either Lesson decision with `模型设计确认`" in skill
    assert "Report complete only for public status `published`" in playbook


def test_design_job_workspace_guide_covers_routing_storage_and_recovery() -> None:
    raw = DESIGN_JOB_GUIDE.read_text(encoding="utf-8")
    text = normalized(DESIGN_JOB_GUIDE)
    for fragment in (
        "same design",
        "independent requirement",
        "Do not create a Git worktree",
        "jobs/<job-directory>/",
        "product_family_onboarding",
        "originating `mechanical_design` Job",
        "migrate-legacy --dry-run",
        "migrate-legacy --apply",
        "JOB_AMBIGUOUS",
        "JOB_STALE_REVISION",
        "macOS",
        "PowerShell",
    ):
        assert fragment in text
    assert "docs/DESIGN_JOB_WORKSPACES.md" in README.read_text(encoding="utf-8")
    assert ".git" in raw


def test_freecad_gui_mcp_boundary() -> None:
    text = normalized(README)
    assert "required for the recommended interactive FreeCAD workflow" in text
    assert "not bundled with the core Python distribution" in text
    assert "viewing, selection, measurement, modeling, and modification" in text
    assert "FreeCADCmd" in text
    assert "docs/FREECAD_GUI_MCP_INTEGRATION.md" in README.read_text(encoding="utf-8")
    assert "THIRD_PARTY_NOTICES.md" in README.read_text(encoding="utf-8")


def test_freecad_gui_mcp_guide_defines_exact_external_boundary() -> None:
    text = normalized(FREECAD_GUI_MCP_GUIDE)
    raw = FREECAD_GUI_MCP_GUIDE.read_text(encoding="utf-8")
    for fragment in (
        "https://github.com/neka-nat/freecad-mcp",
        "MIT",
        "Copyright 2025 Shirokuma (k tanaka)",
        "7667e272e1db669ff61dd5411fb4f622691f2dbc",
        "no approved tag",
        "0.1.19",
        "0.1.17",
        "required external integration",
        "not bundled",
        "not a backend dependency",
        "viewing, selection, measurement, modeling, and modification",
        "clean checkout",
        "separate environment",
        "stdio",
        "127.0.0.1:9875",
        "remote_enabled=false",
        "macOS",
        "FreeCAD 1.1.1",
        "historical evidence only; not release-approved",
        "1.1.3 security release",
        "does not satisfy the public version 0.1.0 macOS interactive-release gate",
        "Windows 11 x64",
        "FreeCAD 1.1.3 x64",
        "passed",
    ):
        assert fragment in text
    assert "acceptance target" in text.lower()
    assert "other commit, version, tag, host, transport, or platform" in text
    for forbidden in (
        "/Users/",
        "vendor/freecad-mcp",
        "0.0.0.0",
        "remote_enabled=true",
    ):
        assert forbidden not in raw


def test_database_capabilities_publish_only_local_evaluation_compose() -> None:
    text = normalized(README)
    raw = README.read_text(encoding="utf-8")
    assert "PostgreSQL/pgvector" in text
    assert "Neo4j" in text
    assert "configurable runtime capabilities" in text
    assert "local and evaluation" in text.lower()
    assert "not a production deployment" in text.lower()
    assert "docker compose" in text.lower()
    assert "compose.yaml" in text
    assert "docs/DATABASE_DEPLOYMENT.md" in raw


def test_database_deployment_guide_is_complete_and_fail_closed() -> None:
    text = normalized(DATABASE_DEPLOYMENT_GUIDE)
    raw = DATABASE_DEPLOYMENT_GUIDE.read_text(encoding="utf-8")
    for fragment in (
        "local and evaluation",
        "not a production deployment",
        "pgvector/pgvector:0.8.5-pg18@sha256:",
        "neo4j:2026.06.0@sha256:",
        "docker compose --env-file",
        "config",
        "pull",
        "up -d --wait",
        "mechanical-design database bootstrap",
        "mechanical-design family create",
        "mechanical-design-mcp",
        "docker compose stop",
        "docker compose down",
        "macOS",
        "PowerShell",
        "Port conflict",
        "Authentication failure",
        "Missing extension",
        "Digest mismatch",
        "Unhealthy service",
        "Partial migration",
    ):
        assert fragment in text
    assert "down -v" in raw
    warning_index = raw.index("down -v")
    warning_window = raw[max(0, warning_index - 240) : warning_index + 240].lower()
    assert "irreversible" in warning_window
    assert "named volumes" in warning_window


def test_database_deployment_preserves_package_migration_ownership() -> None:
    architecture = normalized(ARCHITECTURE)
    deployment = normalized(DATABASE_DEPLOYMENT_GUIDE)
    assert "installed package owns PostgreSQL and Neo4j migration resources" in architecture
    assert "Compose provisions services" in deployment
    assert "installed Mechanical Design Agent owns schema migration" in deployment


def test_windows_guide_records_real_d3_docker_boundary() -> None:
    text = normalized(WINDOWS_RELEASE_GUIDE)
    for fragment in (
        "Docker Desktop 4.87.0",
        "Docker Engine 29.7.2",
        "Docker Compose 5.4.0",
        "WSL 2.7.12",
        "Linux/amd64",
        "two distinct fixed NTFS volumes",
        "local/evaluation",
        "not production",
    ):
        assert fragment in text


def test_public_readme_local_links_resolve_inside_project() -> None:
    for target in LOCAL_LINK.findall(README.read_text(encoding="utf-8")):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (PROJECT_ROOT / target.split("#", 1)[0]).resolve()
        assert resolved.is_relative_to(PROJECT_ROOT.resolve())
        assert resolved.exists(), target


def test_public_readme_uses_modern_synthetic_commands() -> None:
    text = README.read_text(encoding="utf-8")
    for fragment in (
        "mechanical-design init",
        "mechanical-design family create",
        "--organization-id example-org",
        "--design-group-id example-design-group",
        "--family-id example-family",
        "mechanical-design status",
        "mechanical-design doctor",
        "mechanical-design-mcp",
    ):
        assert fragment in text
    for line in text.splitlines():
        if "smoke-fixture" in line:
            assert "--source" in line


def test_env_example_is_comment_only_and_modern_first() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert all(
        not line.strip() or line.lstrip().startswith("#")
        for line in text.splitlines()
    )
    assert text.index("Modern workspace/bootstrap") < text.index(
        "Legacy compatibility"
    )
    for variable in (
        "MECH_DESIGN_WORKSPACE",
        "MECH_DESIGN_DATABASE_URL",
        "MECH_DESIGN_NEO4J_URI",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
        "MECH_DESIGN_FREECADCMD",
        "MECH_DESIGN_ENV_FILE",
        "MECH_DESIGN_ACTOR_ID",
        "MECH_DESIGN_FAMILY_CONFIG",
    ):
        assert variable in text
    assert re.search(r"/Users/[^/\s]+/", text) is None
    assert re.search(r"[A-Za-z]:\\Users\\[^\\\s]+\\", text) is None


def test_synthetic_product_family_is_canonical_and_schema_valid() -> None:
    expected = build_product_family_config(
        organization_id="example-org",
        organization_name="Example Organization",
        design_group_id="example-design-group",
        design_group_name="Example Design Group",
        family_id="example-family",
        family_name="Example Product Family",
        aliases=[],
        actor_id="example-user",
    )
    actual = json.loads(EXAMPLE_FAMILY.read_text(encoding="utf-8"))
    assert actual == expected
    assert validate_product_family_config(actual, path=EXAMPLE_FAMILY) == actual


def test_synthetic_example_is_not_a_runtime_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace=workspace, actor_id=None, dry_run=False)
    family_directory = workspace / "config" / "product_families"
    assert list(family_directory.iterdir()) == []
    manifest = json.loads(
        (workspace / "config" / "mechanical_design.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["default_product_family_id"] is None

    package_root = importlib.resources.files("mechanical_design_agent")
    assert not package_root.joinpath("examples").is_dir()


def test_windows_release_documentation_records_only_certified_boundary() -> None:
    guide = normalized(WINDOWS_RELEASE_GUIDE)
    for fragment in (
        "Windows 11 x64",
        "CPython 3.12",
        "FreeCAD 1.1.3 x64",
        "7667e272e1db669ff61dd5411fb4f622691f2dbc",
        "fixed local NTFS",
        "second fixed NTFS volume",
        "wheel-first",
        "PostgreSQL/pgvector",
        "Neo4j",
        "installed Mechanical Design Agent",
        "dedicated protected host",
        "portable ZIP",
    ):
        assert fragment in guide
    assert "local/evaluation Compose path is public but not production" in guide
    assert "FreeCAD GUI MCP" in guide
    assert "external" in guide
    assert "not bundled" in guide
    assert "not backend-probed" in guide

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            README,
            PROJECT_ROOT / "docs" / "ARCHITECTURE.md",
            DATABASE_DEPLOYMENT_GUIDE,
            FREECAD_GUI_MCP_GUIDE,
            WINDOWS_RELEASE_GUIDE,
        )
    )
    assert "Windows 11 x64" in combined
    assert "FreeCAD 1.1.3 x64" in combined
    assert "unvalidated and a version 0.1.0 release blocker" not in combined
    assert "C:\\Users\\" not in combined
    assert "/Users/" not in combined
    assert "PF-" + "PILOT" not in combined


def test_windows_release_guide_is_linked_from_public_documents() -> None:
    target = "docs/WINDOWS_RELEASE_ACCEPTANCE.md"
    assert target in README.read_text(encoding="utf-8")
    assert "WINDOWS_RELEASE_ACCEPTANCE.md" in (
        PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
    ).read_text(encoding="utf-8")
