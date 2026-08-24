from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

from public_release_helpers import (
    iter_public_repository_files,
    load_public_repository_manifest,
    materialize_public_projection,
    read_public_text_files,
)
from windows_release_helpers import WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_README_ASSETS = {
    Path("docs/assets/ai-mechanical-design-agent-architecture-v2.png"):
        "f21051fa73ab6fb8a08729966cc2f74dabe78205f43de47466222209c7146541",
    Path("docs/assets/ai-mechanical-design-showcase.gif"):
        "66c618705ba4d501894735a83ba7edbb8434f96c46b1830179dd1fd237527328",
}
CAD_OR_REPORT_SUFFIXES = {
    ".fcstd",
    ".step",
    ".stp",
    ".stl",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
}
PRIVATE_PATH_PARTS = {"output", "jobs", "knowledge", "vendor", ".env" + ".local"}
EXPECTED_PUBLIC_CI = (".github/workflows/windows.yml",)
EXPECTED_PUBLIC_SCRIPTS = (
    "scripts/windows_database_deployment_acceptance.ps1",
    "scripts/windows_release_acceptance.ps1",
)
EXPECTED_PROJECT_SKILL_SOURCE_TREES = (
    ".agents/skills/freecad-model-validation",
    ".agents/skills/freecad-standard-parts",
    ".agents/skills/mechanical-design-job-workspace",
)
REQUIRED_DATABASE_DEPLOYMENT_PUBLIC_TESTS = {
    "tests/database_deployment_helpers.py",
    "tests/test_database_deployment.py",
    "tests/test_database_deployment_live.py",
}
REQUIRED_WINDOWS_PUBLIC_TESTS = {
    "tests/test_windows_database_live.py",
    "tests/test_windows_freecad_discovery.py",
    "tests/test_windows_freecad_gui_mcp_live.py",
    "tests/test_windows_packaging.py",
    "tests/test_windows_portability.py",
    "tests/test_windows_release_evidence.py",
    "tests/test_windows_secure_fs.py",
    "tests/windows_freecad_gui_mcp_live_helpers.py",
    "tests/windows_release_helpers.py",
    "tests/freecad_gui_mcp_provenance.py",
}


def test_manifest_authorizes_every_public_file_and_is_self_scanned() -> None:
    manifest = load_public_repository_manifest(PROJECT_ROOT)
    assert "public-repository.toml" in manifest.root_files
    assert "THIRD_PARTY_NOTICES.md" in manifest.root_files
    assert "third-party-components.toml" in manifest.root_files
    assert "compose.yaml" in manifest.root_files
    assert "docs/DATABASE_DEPLOYMENT.md" in manifest.public_docs
    assert "docs/DESIGN_JOB_WORKSPACES.md" in manifest.public_docs
    assert "docs/FREECAD_GUI_MCP_INTEGRATION.md" in manifest.public_docs
    assert ".agents/skills/README.md" in manifest.public_docs
    assert all(
        asset.as_posix() in manifest.public_docs
        for asset in PUBLIC_README_ASSETS
    )
    assert "tests/third_party_licensing_helpers.py" in manifest.public_tests
    assert "tests/test_third_party_licensing.py" in manifest.public_tests
    assert "tests/freecad_gui_mcp_live_helpers.py" in manifest.public_tests
    assert "tests/test_freecad_gui_mcp_integration_live.py" in manifest.public_tests
    assert "tests/test_design_job_freecad_live.py" in manifest.public_tests
    assert "tests/test_agent_skills.py" in manifest.public_tests
    assert manifest.source_trees == (
        *EXPECTED_PROJECT_SKILL_SOURCE_TREES,
        "examples/product_families",
        "src/mechanical_design_agent",
    )
    assert manifest.public_ci == EXPECTED_PUBLIC_CI
    assert manifest.public_scripts == EXPECTED_PUBLIC_SCRIPTS
    assert REQUIRED_WINDOWS_PUBLIC_TESTS <= set(manifest.public_tests)
    assert REQUIRED_DATABASE_DEPLOYMENT_PUBLIC_TESTS <= set(manifest.public_tests)
    assert set(WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE) <= set(manifest.public_tests)
    assert "tests/**" not in manifest.public_tests
    assert len(manifest.public_tests) == len(set(manifest.public_tests))

    files = iter_public_repository_files(PROJECT_ROOT, manifest)
    assert Path("public-repository.toml") in files
    assert Path("examples/product_families/example-family.json") in files
    assert Path("src/mechanical_design_agent/__init__.py") in files
    assert Path(".agents/skills/mechanical-design-job-workspace/SKILL.md") in files
    assert (
        Path(".agents/skills/mechanical-design-job-workspace/agents/openai.yaml")
        in files
    )
    assert (
        Path(".agents/skills/mechanical-design-job-workspace/references/job-contract.md")
        in files
    )
    assert Path("compose.yaml") in files
    binary_assets = {
        path for path in files if path.suffix.lower() in CAD_OR_REPORT_SUFFIXES
    }
    assert binary_assets == set(PUBLIC_README_ASSETS)
    assert all(not (set(path.parts) & PRIVATE_PATH_PARTS) for path in files)
    assert Path(".gitmodules") not in files
    assert not any(path.parts and path.parts[0] == "vendor" for path in files)

    for asset, expected_sha256 in PUBLIC_README_ASSETS.items():
        asset_bytes = (PROJECT_ROOT / asset).read_bytes()
        if asset.suffix == ".png":
            assert asset_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        elif asset.suffix == ".gif":
            assert asset_bytes.startswith((b"GIF87a", b"GIF89a"))
        assert hashlib.sha256(asset_bytes).hexdigest() == expected_sha256
        for forbidden in (
            b"/Users/",
            b":\\Users\\",
            b"PF-" + b"PILOT",
            b"BEGIN " + b"OPENSSH PRIVATE KEY",
            b"BEGIN " + b"RSA PRIVATE KEY",
        ):
            assert forbidden not in asset_bytes

    text_files = tuple(path for path in files if path not in PUBLIC_README_ASSETS)
    texts = read_public_text_files(PROJECT_ROOT, text_files)
    for relative, text in texts.items():
        assert re.search(r"/Users/[A-Za-z0-9._-]+/[A-Za-z0-9._ -]+", text) is None, relative
        assert re.search(
            r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\[A-Za-z0-9._ -]+",
            text,
        ) is None, relative
        assert "BEGIN " + "OPENSSH PRIVATE KEY" not in text, relative
        assert "BEGIN " + "RSA PRIVATE KEY" not in text, relative


def test_manifest_explicitly_excludes_private_development_content() -> None:
    manifest = load_public_repository_manifest(PROJECT_ROOT)
    excluded = set(manifest.excluded_private_paths)
    for path in (
        "config/product_families",
        "docs/superpowers",
        "scripts/run_external_step_validation.py",
        "tests/test_private_public_release_gate.py",
        "tests/test_documentation.py",
        "tests/test_runtime_hardcode_packaging.py",
    ):
        assert path in excluded


def test_repository_ignores_runtime_job_and_release_artifacts() -> None:
    rules = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {"/jobs/", "/output/", "/dist/"} <= rules
    tracked = subprocess.run(
        ["git", "ls-files", "jobs", "output", "dist"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout == ""


def test_projection_contains_only_materialized_allowlist(
    tmp_path: Path,
) -> None:
    manifest = load_public_repository_manifest(PROJECT_ROOT)
    destination = tmp_path / "public"
    copied = materialize_public_projection(PROJECT_ROOT, destination, manifest)
    assert copied == iter_public_repository_files(PROJECT_ROOT, manifest)
    assert (destination / "compose.yaml").read_bytes() == (
        PROJECT_ROOT / "compose.yaml"
    ).read_bytes()
    assert (destination / "docs" / "DATABASE_DEPLOYMENT.md").is_file()
    assert (
        destination / "scripts" / "windows_database_deployment_acceptance.ps1"
    ).is_file()
    assert not (destination / "docs" / "superpowers").exists()


@pytest.mark.parametrize(
    ("group", "value"),
    [
        ("root_files", "/absolute/path"),
        ("public_docs", "../outside.md"),
        ("public_tests", "tests/**"),
        ("public_ci", ".github/**"),
        ("public_scripts", "../outside.ps1"),
    ],
)
def test_manifest_rejects_unsafe_exact_paths(
    tmp_path: Path,
    group: str,
    value: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "public-repository.toml").write_text(
        "\n".join(
            (
                'schema_version = "PublicRepositoryAllowlist/v1"',
                f'{group} = ["{value}"]',
                *(f"{name} = []" for name in (
                    "root_files",
                    "public_docs",
                    "source_trees",
                    "public_tests",
                    "public_ci",
                    "public_scripts",
                    "excluded_private_paths",
                ) if name != group),
                "compatibility_paths = []",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_public_repository_manifest(root)


def test_source_tree_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "src"
    source.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (source / "escape.txt").symlink_to(outside)
    (root / "public-repository.toml").write_text(
        '\n'.join((
            'schema_version = "PublicRepositoryAllowlist/v1"',
            'root_files = ["public-repository.toml"]',
            'public_docs = []',
            'source_trees = ["src"]',
            'public_tests = []',
            'public_ci = []',
            'public_scripts = []',
            'excluded_private_paths = []',
            'compatibility_paths = []',
        )),
        encoding="utf-8",
    )
    manifest = load_public_repository_manifest(root)
    with pytest.raises(ValueError):
        iter_public_repository_files(root, manifest)


def test_public_windows_workflow_is_immutable_and_noninteractive() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "windows.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: windows-2025" in workflow
    assert "timeout-minutes: 180" in workflow
    assert "python-version: '3.12'" in workflow
    uses = re.findall(r"uses:\s*([^\s#]+)", workflow)
    assert uses == [
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
    for required in (
        "git config --global core.autocrlf false",
        "uv sync --frozen --group test --python 3.12",
        "UV_CACHE_DIR=$cache",
        "MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT=$secondVolume",
        "Prewarm the pinned build and runtime dependencies",
        "uv export --frozen --group test",
        "uv pip install --python $python --constraint $constraints $wheel.FullName pytest==9.1.1 jsonschema==4.26.0",
        "UV_OFFLINE",
        "pytest -q",
        "--junitxml=windows-public-offline.xml",
        "--junitxml=windows-public-boundary.xml",
        "$expectedSkipped = 59",
        "$expectedSkipped = 3",
        "$failed -ne 0 -or $skipped -ne $expectedSkipped",
        "uv build --offline",
        "test_public_release_contract.py",
        "test_public_distribution.py",
        "test_windows_release_evidence.py",
    ):
        assert required in workflow
    for forbidden in (
        "MECH_DESIGN_WINDOWS_DB_LIVE_TESTS",
        "MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS",
        "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN",
        "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD",
        "self-hosted",
        "tests/test_private_public_release_gate.py",
    ):
        assert forbidden not in workflow


def test_github_actions_have_exact_build_inventory_entries() -> None:
    inventory = tomllib.loads(
        (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    )
    actions = {
        item["id"]: item
        for item in inventory["components"]
        if item["component_kind"] == "github_action"
    }
    assert set(actions) == {"actions-checkout", "actions-setup-python"}
    assert actions["actions-checkout"]["commit"] == (
        "11d5960a326750d5838078e36cf38b85af677262"
    )
    assert actions["actions-setup-python"]["commit"] == (
        "a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    for action in actions.values():
        assert action["scope"] == "build"
        assert action["distribution"] == "not_distributed_by_project"
        assert action["source_url"].startswith("https://github.com/actions/")
        assert action["commit"] in action["evidence_source"]


def test_protected_windows_acceptance_orchestrator_contract() -> None:
    script = PROJECT_ROOT / "scripts" / "windows_release_acceptance.ps1"
    text = script.read_text(encoding="utf-8")
    for required in (
        "Set-StrictMode -Version Latest",
        "$ErrorActionPreference = 'Stop'",
        "[ValidateSet('W1', 'W2', 'W3', 'W4')]",
        "Assert-FixedNtfsRoot",
        "Assert-DistinctVolumes",
        "Assert-Cpython312X64",
        "Invoke-GatePytest",
        "Invoke-W2Gate",
        "finally",
        "cleanup_failed",
        "unexpected_skips",
        "(@($Gates) -join ',') -ne ($orderedGates -join ',')",
        "$root.LocalName -eq 'testsuites'",
        "GetAttribute('tests')",
    ):
        assert required in text
    for forbidden in (
        "Invoke-Expression",
        "Install-Package",
        "winget install",
        "docker compose",
        "Stop-Service",
        "Start-Service",
        "Stop-Process -Name FreeCAD",
    ):
        assert forbidden not in text

    pwsh = shutil.which("pwsh")
    if os.name != "nt":
        return
    assert pwsh is not None
    completed = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-File", str(script), "-ContractProbe"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        '{"schema_version":"WindowsReleaseAcceptancePlan/v1",'
        '"ordered_gates":["W1","W2","W3","W4"],'
        '"cleanup_failure_overrides_body":true}'
    )
