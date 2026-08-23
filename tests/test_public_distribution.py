from __future__ import annotations

import asyncio
import email.policy
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
COMPOSE_SHA256 = "f6c798e8ecaa7eaf0d83cb9785c309ddc0e2dba1af59923e33b1e3413fad3ef2"
EXPECTED_DEPENDENCIES = [
    "mcp[cli]>=1.3.0,<2",
    "neo4j>=5.28.0,<7",
    "psycopg[binary]>=3.2.0,<4",
    "pywin32>=312; sys_platform == 'win32'",
]
EXPECTED_SCRIPTS = {
    "mechanical-design": "mechanical_design_agent.cli:main",
    "mechanical-design-mcp": "mechanical_design_agent.server:main",
}
EXPECTED_MCP_TOOL_NAMES = {
    "design_assembly_completeness_validate",
    "design_change_applied",
    "design_change_close",
    "design_change_record",
    "design_change_review",
    "design_confirmation_record",
    "design_context_build",
    "design_delivery_approve",
    "design_group_register",
    "design_job_close",
    "design_job_create",
    "design_job_get",
    "design_job_list",
    "design_job_new_working_copy_create",
    "design_job_reopen",
    "design_job_resolve",
    "design_job_working_copy_create",
    "design_knowledge_retrieve",
    "design_lesson_approve",
    "design_lesson_audit_get",
    "design_lesson_get",
    "design_lesson_review_approve",
    "design_lesson_review_context",
    "design_lesson_review_prepare",
    "design_lesson_review_reject",
    "design_lesson_review_status",
    "design_lesson_revoke",
    "design_lesson_search",
    "design_lesson_stage",
    "design_lesson_staged_get",
    "design_lesson_supersede",
    "design_new_working_copy_create",
    "design_retrieval_receipt_get",
    "design_system_doctor",
    "design_system_status",
    "design_validation_record",
    "design_working_copy_create",
    "evidence_artifact_register",
    "family_bootstrap_get",
    "family_bootstrap_update",
    "family_compare_models",
    "family_create",
    "family_folder_confirm",
    "family_profile_get",
    "family_profile_propose",
    "family_profile_review",
    "job_get",
    "knowledge_propose_assertions",
    "knowledge_review",
    "knowledge_search",
    "learning_defer_targets",
    "learning_next_targets",
    "learning_record_exchange",
    "learning_start_session",
    "library_ingest_changes",
    "library_register",
    "library_scan",
    "model_get_analysis",
    "model_identity_confirm",
    "projection_rebuild",
    "projection_sync",
    "standard_part_catalog_disable",
    "standard_part_catalog_enable",
    "standard_part_download_register",
    "standard_part_providers_get",
    "standard_part_sources_status",
    "subfamily_get",
    "subfamily_propose",
    "subfamily_review",
    "workspace_product_family_active",
    "workspace_product_family_create",
    "workspace_product_family_list",
    "workspace_product_family_set_default",
}
CLEAN_ENVIRONMENT_KEYS = {
    "PYTHONPATH",
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
    "MECH_DESIGN_JOB_ID",
    "MECH_DESIGN_FAMILY_CONFIG",
}


def clean_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HOME"] = str(root / "home")
    environment.setdefault("UV_CACHE_DIR", str(root / "uv-cache"))
    for name in CLEAN_ENVIRONMENT_KEYS:
        environment.pop(name, None)
    return environment


def run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def built_artifacts() -> tuple[Path, Path, Path]:
    uv = shutil.which("uv")
    assert uv is not None
    temporary = tempfile.TemporaryDirectory(prefix="public-distribution-")
    root = Path(temporary.name)
    (root / "home").mkdir()
    dist = root / "dist"
    result = run(
        [uv, "build", "--out-dir", str(dist)],
        cwd=PROJECT_ROOT,
        environment=clean_environment(root),
    )
    assert result.returncode == 0, result.stderr
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    yield root, wheel, sdist
    temporary.cleanup()


def test_public_metadata_and_license_contract() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert project["name"] == "ai-mechanical-3dcad-design-agent"
    assert project["version"] == "0.2.0"
    assert project["description"] == (
        "Deterministic mechanical 3D CAD workflows, knowledge, validation, "
        "and MCP tools for coding agents"
    )
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    assert project["requires-python"] == ">=3.12"
    assert project["dependencies"] == EXPECTED_DEPENDENCIES
    assert project["scripts"] == EXPECTED_SCRIPTS

    license_bytes = (PROJECT_ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == LICENSE_SHA256


def _normalized_sdist_members(sdist: Path) -> tuple[str, ...]:
    with tarfile.open(sdist, "r:gz") as archive:
        files = [member.name for member in archive.getmembers() if member.isfile()]
    assert all("\\" not in name for name in files)
    posix_files = [PurePosixPath(name) for name in files]
    roots = {name.parts[0] for name in posix_files}
    assert len(roots) == 1
    root = next(iter(roots))
    return tuple(
        sorted(name.relative_to(root).as_posix() for name in posix_files)
    )


def test_sdist_has_strict_public_release_contents(
    built_artifacts: tuple[Path, Path, Path],
) -> None:
    _, _, sdist = built_artifacts
    members = _normalized_sdist_members(sdist)
    exact = {
        ".env.example",
        ".gitignore",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "compose.yaml",
        "pyproject.toml",
        "third-party-components.toml",
        "docs/ARCHITECTURE.md",
        "docs/DATABASE_DEPLOYMENT.md",
        "docs/ENGINEER_LEARNING_PLAYBOOK.md",
        "docs/FREECAD_GUI_MCP_INTEGRATION.md",
        "docs/WINDOWS_RELEASE_ACCEPTANCE.md",
        "examples/product_families/example-family.json",
    }
    for member in members:
        assert member in exact or member.startswith("src/mechanical_design_agent/"), member
    assert exact <= set(members)
    assert not any(member.startswith("tests/") for member in members)
    assert "compose.yaml" in members
    assert "public-repository.toml" not in members
    assert "uv.lock" not in members


def test_sdist_compose_matches_d3_accepted_bytes(
    built_artifacts: tuple[Path, Path, Path],
) -> None:
    _, wheel, sdist = built_artifacts
    with tarfile.open(sdist, "r:gz") as archive:
        compose_member = next(
            member for member in archive.getmembers() if member.name.endswith("/compose.yaml")
        )
        extracted = archive.extractfile(compose_member)
        assert extracted is not None
        packaged_compose = extracted.read()
    assert packaged_compose == (PROJECT_ROOT / "compose.yaml").read_bytes()
    assert hashlib.sha256(packaged_compose).hexdigest() == COMPOSE_SHA256
    with zipfile.ZipFile(wheel) as archive:
        assert "compose.yaml" not in archive.namelist()


def test_wheel_metadata_license_and_entrypoints(
    built_artifacts: tuple[Path, Path, Path],
) -> None:
    _, wheel, _ = built_artifacts
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        license_names = [name for name in names if ".dist-info/licenses/" in name]
        metadata = BytesParser(policy=email.policy.default).parsebytes(
            archive.read(metadata_name)
        )
        entrypoints = archive.read(entry_name).decode("utf-8")
        assert {Path(name).name for name in license_names} == {
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
        }
        packaged_license = archive.read(
            next(name for name in license_names if name.endswith("/LICENSE"))
        )
        packaged_notices = archive.read(
            next(
                name
                for name in license_names
                if name.endswith("/THIRD_PARTY_NOTICES.md")
            )
        )
        assert "third-party-components.toml" not in names

    assert metadata["Name"] == "ai-mechanical-3dcad-design-agent"
    assert metadata["Version"] == "0.2.0"
    assert metadata["Summary"] == (
        "Deterministic mechanical 3D CAD workflows, knowledge, validation, "
        "and MCP tools for coding agents"
    )
    assert metadata["License-Expression"] == "Apache-2.0"
    assert metadata["Requires-Python"] == ">=3.12"
    assert {Requirement(value) for value in metadata.get_all("Requires-Dist")} == {
        Requirement(value) for value in EXPECTED_DEPENDENCIES
    }
    assert "mechanical-design = mechanical_design_agent.cli:main" in entrypoints
    assert "mechanical-design-mcp = mechanical_design_agent.server:main" in entrypoints
    assert hashlib.sha256(packaged_license).hexdigest() == LICENSE_SHA256
    assert packaged_notices == (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_bytes()


def test_artifacts_exclude_vendor_and_third_party_payloads(
    built_artifacts: tuple[Path, Path, Path],
) -> None:
    _, wheel, sdist = built_artifacts
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = tuple(archive.namelist())
    sdist_members = _normalized_sdist_members(sdist)
    for members in (wheel_members, sdist_members):
        lowered = tuple(member.lower() for member in members)
        assert not any(member.startswith("vendor/") for member in lowered)
        assert not any(".gitmodules" in member for member in lowered)
        assert not any("freecad_fastenerswb" in member for member in lowered)
        assert not any("freecad.gears" in member for member in lowered)
        assert not any("freecad-mcp" in member for member in lowered)
        assert not any(member.endswith((".fcstd", ".step", ".stp", ".stl")) for member in lowered)


async def installed_mcp_tool_names(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> set[str]:
    parameters = StdioServerParameters(
        command=str(executable),
        cwd=cwd,
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
    return {tool.name for tool in listed.tools}


def _wheel_runtime_hashes(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        selected = sorted(
            name
            for name in archive.namelist()
            if name.startswith("mechanical_design_agent/")
            or name.endswith(".dist-info/entry_points.txt")
        )
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest() for name in selected
        }


def test_sdist_rebuilds_and_installs_without_repository_access(
    built_artifacts: tuple[Path, Path, Path],
) -> None:
    root, source_wheel, sdist = built_artifacts
    uv = shutil.which("uv")
    assert uv is not None
    extracted_root = root / "extracted"
    rebuilt_dist = root / "rebuilt-dist"
    venv = root / "venv"
    outside = root / "outside-repository"
    outside.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(extracted_root, filter="data")
    source = next(extracted_root.iterdir())
    environment = clean_environment(root)

    rebuilt = run(
        [uv, "build", "--wheel", "--out-dir", str(rebuilt_dist)],
        cwd=source,
        environment=environment,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    rebuilt_wheel = next(rebuilt_dist.glob("*.whl"))
    assert _wheel_runtime_hashes(rebuilt_wheel) == _wheel_runtime_hashes(source_wheel)
    created = run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        cwd=root,
        environment=environment,
    )
    assert created.returncode == 0, created.stderr
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    cli = venv / (
        "Scripts/mechanical-design.exe" if os.name == "nt" else "bin/mechanical-design"
    )
    mcp = venv / (
        "Scripts/mechanical-design-mcp.exe"
        if os.name == "nt"
        else "bin/mechanical-design-mcp"
    )
    installed = run(
        [uv, "pip", "install", "--python", str(python), str(rebuilt_wheel)],
        cwd=outside,
        environment=environment,
    )
    assert installed.returncode == 0, installed.stderr

    imported = run(
        [
            str(python),
            "-c",
            "import mechanical_design_agent as p; print(p.__version__); print(p.__file__)",
        ],
        cwd=outside,
        environment=environment,
    )
    assert imported.returncode == 0, imported.stderr
    version, module_path = imported.stdout.splitlines()
    assert version == "0.2.0"
    assert Path(module_path).is_relative_to(venv)

    help_result = run([str(cli), "--help"], cwd=outside, environment=environment)
    assert help_result.returncode == 0, help_result.stderr
    assert "smoke-fixture" in help_result.stdout
    missing_source = run(
        [str(cli), "smoke-fixture"], cwd=outside, environment=environment
    )
    assert missing_source.returncode == 2
    assert "--source" in missing_source.stderr
    assert asyncio.run(
        installed_mcp_tool_names(mcp, cwd=outside, environment=environment)
    ) == EXPECTED_MCP_TOOL_NAMES
