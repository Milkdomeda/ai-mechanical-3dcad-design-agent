from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import pytest

import database_deployment_helpers as deployment_helpers
from database_deployment_helpers import (
    DeploymentGateError,
    DockerProjectInventory,
    clean_deployment_environment,
    create_attempt_layout,
    installed_script_path,
    live_evidence_filename,
    materialize_candidate_inputs,
    managed_compose_project,
    neo4j_empty_state_query,
    pytest_junit_fingerprint,
    run_checked,
    supported_live_platform,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = PROJECT_ROOT / "compose.yaml"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
WINDOWS_D3_RUNNER = PROJECT_ROOT / "scripts/windows_database_deployment_acceptance.ps1"
DATABASE_DEPLOYMENT_GUIDE = PROJECT_ROOT / "docs/DATABASE_DEPLOYMENT.md"
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.5-pg18@"
    "sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0"
)
NEO4J_IMAGE = (
    "neo4j:2026.06.0@"
    "sha256:42fd5b9ead4dd4211f6f91bd831c358e4e2117367d04633fbf88682ca4792b30"
)


def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_compose_has_exact_service_and_image_identity() -> None:
    text = compose_text()
    assert text.startswith("name: ai-mechanical-3dcad-design-agent\n")
    service_block, volume_block = text.split("\nvolumes:\n", 1)
    services = re.findall(r"^  ([a-z][a-z0-9_-]*):$", service_block, re.MULTILINE)
    volumes = re.findall(r"^  ([a-z][a-z0-9_-]*):$", volume_block, re.MULTILINE)
    assert services == ["postgres", "neo4j"]
    assert volumes == ["postgres_data", "neo4j_data"]
    assert f"image: {POSTGRES_IMAGE}" in text
    assert f"image: {NEO4J_IMAGE}" in text
    assert "latest" not in text.lower()


def test_compose_is_loopback_only_and_does_not_publish_neo4j_http() -> None:
    text = compose_text()
    assert (
        '"127.0.0.1:${MECH_DESIGN_POSTGRES_PORT:-55432}:5432"' in text
    )
    assert (
        '"127.0.0.1:${MECH_DESIGN_NEO4J_BOLT_PORT:-57687}:7687"' in text
    )
    assert "7474" not in text
    published = re.findall(r'^\s+-\s+"([^"]+:[0-9]+)"$', text, re.MULTILINE)
    assert published
    assert all(value.startswith("127.0.0.1:") for value in published)


def test_compose_has_no_repo_path_bind_mount_or_implicit_env_file() -> None:
    text = compose_text()
    for forbidden in (
        ".env.local",
        "env_file:",
        "docker-entrypoint-initdb.d",
        "./",
        "../",
        "/logs",
        "migrations/",
        "schemas/",
        "workspace",
        "output/",
        "knowledge/",
    ):
        assert forbidden not in text
    assert "postgres_data:/var/lib/postgresql" in text
    assert "neo4j_data:/data" in text


def test_compose_requires_passwords_and_uses_service_readiness_healthchecks() -> None:
    text = compose_text()
    assert "${MECH_DESIGN_POSTGRES_PASSWORD:?" in text
    assert "${MECH_DESIGN_NEO4J_PASSWORD:?" in text
    assert "${MECH_DESIGN_POSTGRES_USER:-mechanical_design}" in text
    assert "${MECH_DESIGN_POSTGRES_DB:-mechanical_design}" in text
    assert 'NEO4J_AUTH: "neo4j/' in text
    assert "pg_isready" in text
    assert "cypher-shell" in text
    assert "RETURN 1" in text
    for forbidden in ("mechanical-design database bootstrap", "migrate", "uv run"):
        assert forbidden not in text


def test_compose_keeps_normal_stop_data_in_two_named_volumes() -> None:
    text = compose_text()
    assert "restart: unless-stopped" in text
    assert text.count("restart: unless-stopped") == 2
    assert text.count("postgres_data:/var/lib/postgresql") == 1
    assert text.count("neo4j_data:/data") == 1


def test_database_env_example_is_comment_only_explicit_and_nonfunctional() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert all(
        not line.strip() or line.lstrip().startswith("#")
        for line in text.splitlines()
    )
    for variable in (
        "MECH_DESIGN_POSTGRES_USER",
        "MECH_DESIGN_POSTGRES_DB",
        "MECH_DESIGN_POSTGRES_PASSWORD",
        "MECH_DESIGN_POSTGRES_PORT",
        "MECH_DESIGN_DATABASE_URL",
        "MECH_DESIGN_NEO4J_USER",
        "MECH_DESIGN_NEO4J_PASSWORD",
        "MECH_DESIGN_NEO4J_BOLT_PORT",
        "MECH_DESIGN_NEO4J_URI",
    ):
        assert variable in text
    assert "raw PostgreSQL password" in text
    assert "URL-encoded" in text
    assert "MECH_DESIGN_NEO4J_USER=neo4j" in text
    assert "55432" in text
    assert "57687" in text
    assert "<replace-with-postgresql-password>" in text
    assert "<replace-with-neo4j-password>" in text


def test_compose_image_tag_and_digest_match_machine_inventory() -> None:
    inventory = tomllib.loads(
        (PROJECT_ROOT / "third-party-components.toml").read_text(encoding="utf-8")
    )
    components = {item["id"]: item for item in inventory["components"]}
    expected = {
        f"{components['pgvector-service']['image']}@"
        f"{components['pgvector-service']['image_digest']}",
        f"{components['neo4j-server']['image']}@"
        f"{components['neo4j-server']['image_digest']}",
    }
    actual = set(re.findall(r"^\s+image:\s+(\S+)$", compose_text(), re.MULTILINE))
    assert actual == expected


def test_compose_is_public_and_sdist_only_after_d3_acceptance() -> None:
    public_manifest = tomllib.loads(
        (PROJECT_ROOT / "public-repository.toml").read_text(encoding="utf-8")
    )
    build_config = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "compose.yaml" in public_manifest["root_files"]
    assert "compose.yaml" not in public_manifest["excluded_private_paths"]
    assert "compose.yaml" in build_config["tool"]["hatch"]["build"]["targets"][
        "sdist"
    ]["only-include"]


class FakeDockerProjectBackend:
    def __init__(self) -> None:
        self.inventory = DockerProjectInventory((), (), ())
        self.labels: dict[tuple[str, str], dict[str, str]] = {}
        self.events: list[str] = []
        self.up_error: Exception | None = None
        self.down_error: Exception | None = None
        self.leave_after_down = False

    def list_project_resources(self, project: str) -> DockerProjectInventory:
        self.events.append(f"list:{project}")
        return self.inventory

    def labels_for(self, kind: str, resource_id: str) -> dict[str, str]:
        self.events.append(f"labels:{kind}:{resource_id}")
        return self.labels[(kind, resource_id)]

    def up(self, project: str) -> None:
        self.events.append(f"up:{project}")
        if self.up_error is not None:
            raise self.up_error
        self.inventory = DockerProjectInventory(
            ("container-postgres", "container-neo4j"),
            ("network",),
            ("volume-postgres", "volume-neo4j"),
        )
        for kind, identifiers in self.inventory.by_kind():
            for resource_id in identifiers:
                self.labels[(kind, resource_id)] = {
                    "com.docker.compose.project": project
                }

    def down(self, project: str) -> None:
        self.events.append(f"down:{project}")
        if self.down_error is not None:
            raise self.down_error
        if not self.leave_after_down:
            self.inventory = DockerProjectInventory((), (), ())


def test_attempt_layout_is_uuid_owned_canonical_and_rejects_symlink_base(
    tmp_path: Path,
) -> None:
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    layout = create_attempt_layout(tmp_path, uuid_factory=lambda: fixed)

    assert layout.project == "md3dcad-12345678123456781234567812345678"
    assert layout.root.parent == tmp_path.resolve()
    assert layout.root.name == layout.project
    assert layout.deployment.is_dir()
    assert layout.workspace.is_dir()
    assert layout.build.is_dir()
    assert layout.raw.is_dir()
    assert layout.safe_evidence.is_dir()

    linked = tmp_path.parent / f"{tmp_path.name}-link"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DeploymentGateError, match="ATTEMPT_BASE_UNSAFE"):
        create_attempt_layout(linked)


def test_attempt_layout_accepts_canonicalized_ancestor_alias(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "platform-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    selected_base = alias / "attempt-base"
    selected_base.mkdir()

    layout = create_attempt_layout(selected_base)

    assert layout.root.parent == selected_base.resolve()


def test_clean_deployment_environment_removes_development_and_live_values(
    tmp_path: Path,
) -> None:
    original = {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/user",
        "PYTHONPATH": "/private/source",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "cp936",
        "MECH_DESIGN_DATABASE_URL": "postgresql://secret",
        "MECH_DESIGN_NEO4J_PASSWORD": "secret",
        "MECH_DESIGN_ENV_FILE": "/private/config",
        "MECH_DESIGN_DOCKER_DATABASE_LIVE": "1",
    }
    before = dict(original)

    environment = clean_deployment_environment(tmp_path, environ=original)

    assert environment["PATH"] == "/synthetic/bin"
    assert environment["HOME"] == str((tmp_path / "home").resolve())
    assert environment["DOCKER_CONFIG"] == str(Path("/synthetic/user") / ".docker")
    assert environment["UV_CACHE_DIR"] == str((tmp_path / "uv-cache").resolve())
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert original == before
    assert not any(
        key.startswith("MECH_DESIGN_") or key == "PYTHONPATH"
        for key in environment
    )


def test_run_checked_propagates_utf8_without_mutating_parent_environment(
    tmp_path: Path,
) -> None:
    attempt = tmp_path / "数据库 部署 验收"
    attempt.mkdir()
    original = {"PATH": os.environ["PATH"]}
    parent_before = {
        key: os.environ.get(key) for key in ("PYTHONUTF8", "PYTHONIOENCODING")
    }
    environment = clean_deployment_environment(attempt, environ=original)

    result = run_checked(
        [
            sys.executable,
            "-c",
            (
                "import json,os; "
                "print(json.dumps([os.environ['PYTHONUTF8'], "
                "os.environ['PYTHONIOENCODING'], '数据库 部署 验收'], "
                "ensure_ascii=False))"
            ),
        ],
        cwd=attempt,
        environment=environment,
        stage="UTF8_ENVIRONMENT_PROBE",
        timeout_seconds=30,
    )

    assert json.loads(result.stdout) == ["1", "utf-8", "数据库 部署 验收"]
    assert original == {"PATH": os.environ["PATH"]}
    assert {
        key: os.environ.get(key) for key in ("PYTHONUTF8", "PYTHONIOENCODING")
    } == parent_before


def test_clean_deployment_environment_preserves_explicit_prepared_cache(
    tmp_path: Path,
) -> None:
    prepared_cache = tmp_path / "prepared uv cache"
    prepared_cache.mkdir()
    original = {
        "PATH": os.environ["PATH"],
        "UV_CACHE_DIR": str(prepared_cache),
    }
    before = dict(original)

    environment = clean_deployment_environment(tmp_path / "attempt", environ=original)

    assert environment["UV_CACHE_DIR"] == str(prepared_cache.resolve())
    assert original == before


def test_clean_deployment_environment_preserves_explicit_docker_config(
    tmp_path: Path,
) -> None:
    environment = clean_deployment_environment(
        tmp_path,
        environ={
            "HOME": "/synthetic/user",
            "DOCKER_CONFIG": "/synthetic/docker-config",
        },
    )

    assert environment["HOME"] == str((tmp_path / "home").resolve())
    assert environment["DOCKER_CONFIG"] == "/synthetic/docker-config"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 required")
def test_windows_d3_runner_contract_keeps_second_volume_for_filesystem_gates() -> None:
    probe = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(WINDOWS_D3_RUNNER),
            "-ContractProbe",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    contract = json.loads(probe.stdout)
    assert contract["second_ntfs_gates"] == ["Gate00", "Gate02", "Gate03"]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 required")
def test_windows_d3_runner_separates_primary_temp_from_second_ntfs_root(
    tmp_path: Path,
) -> None:
    raw_second = os.environ.get("MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT", "").strip()
    if not raw_second:
        pytest.skip("explicit second fixed NTFS root required")

    primary = tmp_path / "primary temp parent"
    primary.mkdir()
    second = Path(raw_second) / f"d3-volume-layout-{uuid.uuid4().hex}"
    second.mkdir()
    try:
        probe = subprocess.run(
            [
                shutil.which("pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-File",
                str(WINDOWS_D3_RUNNER),
                "-VolumeLayoutProbe",
                "-PrimaryTempParent",
                str(primary),
                "-SecondNtfsRoot",
                str(second),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )

        assert probe.returncode == 0, probe.stderr
        result = json.loads(probe.stdout)
        assert result == {
            "primary_temp_fixed_ntfs": True,
            "second_root_fixed_ntfs": True,
            "distinct_volumes": True,
            "unicode_space_temp": True,
            "cleanup": True,
        }
        assert list(primary.iterdir()) == []
    finally:
        shutil.rmtree(second, ignore_errors=False)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 required")
def test_windows_d3_runner_rejects_same_volume_layout(tmp_path: Path) -> None:
    primary = tmp_path / "primary temp parent"
    second = tmp_path / "invalid second root"
    primary.mkdir()
    second.mkdir()

    probe = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(WINDOWS_D3_RUNNER),
            "-VolumeLayoutProbe",
            "-PrimaryTempParent",
            str(primary),
            "-SecondNtfsRoot",
            str(second),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )

    assert probe.returncode != 0
    assert "Second NTFS root must be on D:" in probe.stderr
    assert list(primary.iterdir()) == []


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 required")
def test_windows_d3_runner_cleanup_removes_only_owned_unicode_failure_tree(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "attempt parent"
    parent.mkdir()
    owned = parent / ("数据库 部署 验收 " + "a" * 32)
    nested = owned / "pytest-temporary" / "child"
    nested.mkdir(parents=True)
    (nested / "residue.txt").write_text("attempt-owned", encoding="utf-8")
    outside = tmp_path / "outside-owned-root"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    failed_child = subprocess.run(
        [sys.executable, "-c", "raise SystemExit(7)"],
        cwd=owned,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert failed_child.returncode == 7

    cleanup = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(WINDOWS_D3_RUNNER),
            "-CleanupProbe",
            "-CleanupProbeRoot",
            str(owned),
            "-CleanupProbeParent",
            str(parent),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert cleanup.returncode == 0
    assert not owned.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 required")
def test_windows_d3_runner_cleanup_unlinks_reparse_without_following_it(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "attempt parent"
    parent.mkdir()
    owned = parent / ("数据库 部署 验收 " + "b" * 32)
    owned.mkdir()
    outside = tmp_path / "outside-owned-root"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (owned / "linked").symlink_to(outside, target_is_directory=True)

    cleanup = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(WINDOWS_D3_RUNNER),
            "-CleanupProbe",
            "-CleanupProbeRoot",
            str(owned),
            "-CleanupProbeParent",
            str(parent),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert cleanup.returncode == 0
    assert not owned.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("machine", "expected"),
    (
        ("AMD64", "amd64"),
        ("x86_64", "amd64"),
        ("ARM64", "arm64"),
        ("aarch64", "arm64"),
    ),
)
def test_docker_platform_architecture_normalizes_windows_and_posix_names(
    machine: str,
    expected: str,
) -> None:
    assert deployment_helpers.docker_platform_architecture(machine) == expected


def test_pytest_junit_fingerprint_exposes_only_counts_and_node_ids(
    tmp_path: Path,
) -> None:
    report = tmp_path / "child-junit.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites tests="2" failures="1" errors="0" skipped="0">
  <testsuite name="pytest" tests="2" failures="1" errors="0" skipped="0">
    <testcase classname="tests.test_safe" name="test_passes" />
    <testcase classname="tests.test_safe" name="test_fails">
      <failure message="postgresql://user:secret@private-host/db">private path</failure>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    fingerprint = pytest_junit_fingerprint(report)

    assert fingerprint == {
        "tests": 2,
        "failures": 1,
        "errors": 0,
        "skipped": 0,
        "failed_nodes": ("tests.test_safe::test_fails",),
    }
    assert "secret" not in repr(fingerprint)


def test_installed_script_path_keeps_the_virtualenv_entrypoint_directory(
    tmp_path: Path,
) -> None:
    base_python = tmp_path / "base" / "python3.12"
    base_python.parent.mkdir()
    base_python.touch()
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)

    script = installed_script_path(venv_python, "mechanical-design", os_name="posix")

    assert script == venv_python.parent / "mechanical-design"


def test_neo4j_empty_state_query_retains_a_row_when_no_relationships_exist() -> None:
    query = neo4j_empty_state_query()

    assert "MATCH (n) WITH count(n) AS nodes" in query
    assert "OPTIONAL MATCH ()-[r]->()" in query


@pytest.mark.parametrize(
    ("platform_name", "expected_filename"),
    (
        ("darwin", "macos-d3-database-deployment-safe.json"),
        ("win32", "windows-d3-database-deployment-safe.json"),
    ),
)
def test_live_deployment_platform_contract(
    platform_name: str,
    expected_filename: str,
) -> None:
    assert supported_live_platform(platform_name)
    assert live_evidence_filename(platform_name) == expected_filename


def test_live_deployment_platform_contract_rejects_unapproved_hosts() -> None:
    assert not supported_live_platform("linux")
    with pytest.raises(ValueError, match="unsupported live platform"):
        live_evidence_filename("linux")


def test_windows_d3_runner_has_fail_closed_installed_deployment_contract() -> None:
    text = WINDOWS_D3_RUNNER.read_text(encoding="utf-8")

    assert "WindowsD3DockerDatabaseDeploymentRunner/v1" in text
    assert "MECH_DESIGN_DOCKER_DATABASE_LIVE" in text
    assert "test_clean_installed_wheel_database_deployment" in text
    assert "DockerDesktopLinuxEngine" in text
    assert "linux/amd64" in text
    assert "CPython 3.12 x64" in text
    assert "fixed NTFS" in text
    assert "ReparsePoint" in text
    assert "数据库 部署 验收" in text
    assert "cleanup_failure_overrides_body" in text
    assert "@(Get-D3DockerProjects).Count" in text
    assert "MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT" in text
    assert "required protected W3 environment" not in text
    assert "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN =" not in text
    assert "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD =" not in text


def test_public_deployment_guide_defines_knowledge_safety_contract() -> None:
    text = DATABASE_DEPLOYMENT_GUIDE.read_text(encoding="utf-8")

    assert "Python 3.12" in text
    assert "Docker Desktop" in text
    assert "installed package owns schema migration" in text
    assert "loopback" in text
    assert "001_knowledge_core.sql" in text
    assert "select a fresh knowledge\ndatabase" in text
    assert "completed CAD model remains completed" in text
    assert "production" in text


def test_managed_compose_project_rejects_preexisting_resources_before_up() -> None:
    backend = FakeDockerProjectBackend()
    backend.inventory = DockerProjectInventory(("preexisting",), (), ())

    with pytest.raises(DeploymentGateError, match="DOCKER_PROJECT_NOT_EMPTY"):
        with managed_compose_project(backend, "md3dcad-" + "a" * 32):
            pytest.fail("body must not run")

    assert not any(event.startswith("up:") for event in backend.events)
    assert not any(event.startswith("down:") for event in backend.events)


def test_managed_compose_project_verifies_labels_and_exact_cleanup() -> None:
    backend = FakeDockerProjectBackend()
    project = "md3dcad-" + "b" * 32

    with managed_compose_project(backend, project):
        assert backend.inventory.containers

    assert backend.inventory.empty
    assert backend.events.count(f"up:{project}") == 1
    assert backend.events.count(f"down:{project}") == 1


def test_unowned_resource_blocks_destructive_cleanup() -> None:
    backend = FakeDockerProjectBackend()
    project = "md3dcad-" + "c" * 32

    with pytest.raises(DeploymentGateError, match="DOCKER_OWNERSHIP_UNPROVEN"):
        with managed_compose_project(backend, project):
            backend.labels[("container", "container-postgres")] = {
                "com.docker.compose.project": "another-project"
            }

    assert f"down:{project}" not in backend.events


def test_cleanup_failure_overrides_passing_body_and_is_redacted() -> None:
    backend = FakeDockerProjectBackend()
    backend.down_error = RuntimeError(
        "cleanup failed at /private/user with password=secret-value"
    )

    with pytest.raises(DeploymentGateError) as captured:
        with managed_compose_project(backend, "md3dcad-" + "d" * 32):
            pass

    assert captured.value.code == "DOCKER_CLEANUP_FAILED"
    assert "secret-value" not in str(captured.value)
    assert "/private/user" not in str(captured.value)


def test_run_checked_has_bounded_timeout_utf8_replacement_and_safe_error() -> None:
    def failed_runner(*args, **kwargs):
        return type(
            "Result",
            (),
            {"returncode": 7, "stdout": b"progress \\xff", "stderr": b"password=secret"},
        )()

    with pytest.raises(DeploymentGateError) as captured:
        run_checked(
            ["synthetic-command"],
            cwd=Path.cwd(),
            environment={},
            stage="SYNTHETIC_STAGE",
            timeout_seconds=17,
            runner=failed_runner,
        )

    assert captured.value.code == "SUBPROCESS_FAILED"
    assert captured.value.stage == "SYNTHETIC_STAGE"
    assert captured.value.timeout_seconds == 17
    assert "secret" not in str(captured.value)


def test_body_failure_is_preserved_after_successful_cleanup() -> None:
    backend = FakeDockerProjectBackend()

    with pytest.raises(ValueError, match="synthetic body failure"):
        with managed_compose_project(backend, "md3dcad-" + "e" * 32):
            raise ValueError("synthetic body failure")

    assert backend.inventory.empty


def test_partial_up_failure_cleans_only_verified_owned_resources() -> None:
    backend = FakeDockerProjectBackend()
    project = "md3dcad-" + "f" * 32

    def partial_up(selected: str) -> None:
        FakeDockerProjectBackend.up(backend, selected)
        raise RuntimeError("synthetic partial startup failure")

    backend.up = partial_up  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="partial startup"):
        with managed_compose_project(backend, project):
            pytest.fail("body must not run")

    assert backend.inventory.empty
    assert backend.events.count(f"down:{project}") == 1


def test_run_checked_reports_timeout_without_command_or_output() -> None:
    def timeout_runner(*args, **kwargs):
        import subprocess

        raise subprocess.TimeoutExpired(
            cmd=["secret-command", "password=secret-value"],
            timeout=11,
            output=b"/private/user",
        )

    with pytest.raises(DeploymentGateError) as captured:
        run_checked(
            ["secret-command", "password=secret-value"],
            cwd=Path.cwd(),
            environment={},
            stage="TIMEOUT_STAGE",
            timeout_seconds=11,
            runner=timeout_runner,
        )

    assert captured.value.code == "SUBPROCESS_TIMEOUT"
    assert captured.value.timeout_seconds == 11
    assert "secret-value" not in str(captured.value)
    assert "/private/user" not in str(captured.value)


def test_candidate_projection_copies_only_approved_inputs_with_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "compose.yaml").write_text("name: synthetic\n", encoding="utf-8")
    (source / ".env.example").write_text("# synthetic\n", encoding="utf-8")
    (source / "private.txt").write_text("must not copy\n", encoding="utf-8")
    base = tmp_path / "attempts"
    base.mkdir()
    layout = create_attempt_layout(base)

    evidence = materialize_candidate_inputs(source, layout)

    assert sorted(path.name for path in layout.deployment.iterdir()) == [
        ".env.example",
        "compose.yaml",
    ]
    assert evidence.keys() == {"compose_sha256", "env_example_sha256"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in evidence.values())
