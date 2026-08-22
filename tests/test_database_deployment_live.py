from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import timedelta
import uuid
import zipfile

import pytest

from database_deployment_helpers import (
    DockerComposeBackend,
    clean_deployment_environment,
    create_attempt_layout,
    docker_platform_architecture,
    installed_script_path,
    live_evidence_filename,
    managed_compose_project,
    materialize_candidate_inputs,
    neo4j_empty_state_query,
    run_checked,
    supported_live_platform,
)


LIVE_OPT_IN = "MECH_DESIGN_DOCKER_DATABASE_LIVE"
CHILD_OPT_IN = "MECH_DESIGN_DOCKER_DATABASE_LIVE_CHILD"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_POSTGRES_MIGRATIONS = (
    "001_init.sql",
    "002_design_lessons.sql",
    "003_design_lesson_hardening.sql",
    "004_validation_report_digest.sql",
    "005_design_lesson_reviews.sql",
    "006_delivery_approval_binding.sql",
    "007_review_immutable_snapshots.sql",
    "008_drop_legacy_snapshot_constraints.sql",
    "009_design_lifecycle_closure.sql",
)
EXPECTED_NEO4J_MIGRATIONS = (
    "001_constraints.cypher",
    "002_design_lessons.cypher",
    "003_projection_state.cypher",
)
EXPECTED_NEO4J_CONSTRAINTS = (
    "assertion_id_unique",
    "design_lesson_id_unique",
    "family_id_unique",
    "family_profile_id_unique",
    "model_revision_id_unique",
    "product_id_unique",
    "projection_state_name_unique",
    "source_node_key_unique",
    "subfamily_id_unique",
)
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.5-pg18@"
    "sha256:12a379b47ad65289572ea0756efc11b7c241a6662833e8af7038cd3b73d647e0"
)
NEO4J_IMAGE = (
    "neo4j:2026.06.0@"
    "sha256:42fd5b9ead4dd4211f6f91bd831c358e4e2117367d04633fbf88682ca4792b30"
)
LIVE_BUNDLE_FILES = (
    "tests/conftest.py",
    "tests/database_deployment_helpers.py",
    "tests/test_database_deployment_live.py",
    "tests/windows_release_helpers.py",
    "tests/test_windows_database_live.py",
    "tests/test_migrations.py",
    "tests/test_design_lifecycle.py",
    "tests/test_design_lesson_repository.py",
    "tests/test_design_lesson_outbox.py",
    "tests/test_design_lesson_projection.py",
    "tests/test_design_lesson_reviews.py",
)
LIVE_NODE_IDS = (
    "tests/test_database_deployment_live.py::test_installed_database_bootstrap_first_and_second",
    "tests/test_database_deployment_live.py::test_installed_postgres_migration_contract",
    "tests/test_database_deployment_live.py::test_installed_neo4j_migration_contract",
    "tests/test_migrations.py::LiveMigrationConcurrencyTests",
    "tests/test_design_lifecycle.py::LiveSourceRevisionResolutionTests",
    "tests/test_design_lesson_repository.py::PostgresDesignLessonLifecycleConcurrencyTests",
    "tests/test_design_lesson_repository.py::PostgresDesignLessonRepositoryTests",
    "tests/test_design_lesson_outbox.py::LiveOutboxLeaseTests",
    "tests/test_windows_database_live.py::test_installed_neo4j_rebuild_projection_state_and_scoped_retrieval",
    "tests/test_design_lesson_projection.py::LiveDesignLessonProjectionSafetyTests",
    "tests/test_design_lesson_reviews.py::test_live_confirmed_to_retrievable_flow_is_atomic_projected_and_searchable",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_script(name: str) -> Path:
    return installed_script_path(Path(sys.executable), name)


def _child_cli_json(*arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [str(_installed_script("mechanical-design")), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, "installed database bootstrap failed"
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


@pytest.mark.skipif(
    os.environ.get(CHILD_OPT_IN) != "1",
    reason="installed-wheel database bootstrap contract runs only in the D3 child",
)
def test_installed_database_bootstrap_first_and_second() -> None:
    import mechanical_design_agent

    package_path = Path(mechanical_design_agent.__file__).resolve()
    assert package_path.is_relative_to(Path(sys.prefix).resolve())
    workspace = os.environ["MECH_DESIGN_WORKSPACE"]
    env_file = os.environ["MECH_DESIGN_ENV_FILE"]
    command = (
        "database",
        "bootstrap",
        "--workspace",
        workspace,
        "--env-file",
        env_file,
    )

    first = _child_cli_json(*command)
    second = _child_cli_json(*command)

    assert first["status"] == second["status"] == "ok"
    assert first["postgresql"]["applied"] == list(EXPECTED_POSTGRES_MIGRATIONS)
    assert first["postgresql"]["skipped"] == []
    assert second["postgresql"]["applied"] == []
    assert second["postgresql"]["skipped"] == list(EXPECTED_POSTGRES_MIGRATIONS)
    assert first["neo4j"] == second["neo4j"] == {
        "status": "ok",
        "migration_resources_verified": True,
        "constraints_verified": True,
    }
    summary_path = Path(os.environ["MECH_DESIGN_D3_CHILD_SUMMARY"])
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": "D3InstalledBootstrapSummary/v1",
                "first_postgres_applied": first["postgresql"]["applied"],
                "second_postgres_skipped": second["postgresql"]["skipped"],
                "neo4j_first": first["neo4j"]["status"],
                "neo4j_second": second["neo4j"]["status"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    os.environ.get(CHILD_OPT_IN) != "1",
    reason="installed-wheel PostgreSQL contract runs only in the D3 child",
)
def test_installed_postgres_migration_contract() -> None:
    from mechanical_design_agent.migrations import postgres_migrations_directory
    from mechanical_design_agent.repository import PostgresRepository

    repository = PostgresRepository(os.environ["MECH_DESIGN_DATABASE_URL"])
    state = repository.migration_state()
    with postgres_migrations_directory() as root:
        root = root.resolve()
        assert root.is_relative_to(Path(sys.prefix).resolve())
        paths = sorted(root.glob("*.sql"))
        expected = [
            {
                "version": index,
                "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for index, path in enumerate(paths, start=1)
        ]
    assert tuple(path.name for path in paths) == EXPECTED_POSTGRES_MIGRATIONS
    assert state["ledger"] == expected
    assert set(state["extensions"]) == {"pg_trgm", "pgcrypto", "vector"}


@pytest.mark.skipif(
    os.environ.get(CHILD_OPT_IN) != "1",
    reason="installed-wheel Neo4j contract runs only in the D3 child",
)
def test_installed_neo4j_migration_contract() -> None:
    from mechanical_design_agent.migrations import neo4j_migrations_directory
    from mechanical_design_agent.projection import Neo4jProjection

    projection = Neo4jProjection(
        os.environ["MECH_DESIGN_NEO4J_URI"],
        os.environ["MECH_DESIGN_NEO4J_USER"],
        os.environ["MECH_DESIGN_NEO4J_PASSWORD"],
    )
    with neo4j_migrations_directory() as root:
        root = root.resolve()
        assert root.is_relative_to(Path(sys.prefix).resolve())
        paths = sorted(root.glob("*.cypher"))
    assert tuple(path.name for path in paths) == EXPECTED_NEO4J_MIGRATIONS
    assert tuple(projection.constraint_names()) == EXPECTED_NEO4J_CONSTRAINTS
    with projection._driver() as driver, driver.session() as session:
        state = session.run(neo4j_empty_state_query()).single()
    assert state is not None
    assert state["nodes"] == 0
    assert state["relationships"] == 0


_CHILD_ENABLED = os.environ.get(CHILD_OPT_IN) == "1"
test_installed_database_bootstrap_first_and_second.__test__ = _CHILD_ENABLED
test_installed_postgres_migration_contract.__test__ = _CHILD_ENABLED
test_installed_neo4j_migration_contract.__test__ = _CHILD_ENABLED


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_env_file(
    path: Path,
    *,
    postgres_port: int,
    neo4j_port: int,
    postgres_password: str,
    neo4j_password: str,
) -> None:
    path.write_text(
        "\n".join(
            (
                "MECH_DESIGN_POSTGRES_USER=mechanical_design",
                "MECH_DESIGN_POSTGRES_DB=mechanical_design",
                f"MECH_DESIGN_POSTGRES_PASSWORD={postgres_password}",
                f"MECH_DESIGN_POSTGRES_PORT={postgres_port}",
                "MECH_DESIGN_DATABASE_URL="
                f"postgresql://mechanical_design:{postgres_password}@127.0.0.1:"
                f"{postgres_port}/mechanical_design",
                "MECH_DESIGN_NEO4J_USER=neo4j",
                f"MECH_DESIGN_NEO4J_PASSWORD={neo4j_password}",
                f"MECH_DESIGN_NEO4J_BOLT_PORT={neo4j_port}",
                f"MECH_DESIGN_NEO4J_URI=bolt://127.0.0.1:{neo4j_port}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _build_and_install(
    layout,
    environment: dict[str, str],
) -> tuple[Path, Path, Path, Path, Path]:
    uv = shutil.which("uv")
    assert uv is not None
    run_checked(
        [uv, "build", "--wheel", "--sdist", "--out-dir", str(layout.build)],
        cwd=PROJECT_ROOT,
        environment=environment,
        stage="D3_BUILD_ARTIFACTS",
        timeout_seconds=600,
    )
    wheel = next(layout.build.glob("*.whl"))
    sdist = next(layout.build.glob("*.tar.gz"))
    venv = layout.root / "venv"
    run_checked(
        [uv, "venv", "--python", sys.executable, str(venv)],
        cwd=layout.root,
        environment=environment,
        stage="D3_CREATE_VENV",
        timeout_seconds=300,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    cli = venv / (
        "Scripts/mechanical-design.exe" if os.name == "nt" else "bin/mechanical-design"
    )
    mcp = venv / (
        "Scripts/mechanical-design-mcp.exe"
        if os.name == "nt"
        else "bin/mechanical-design-mcp"
    )
    run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(wheel),
            "pytest==9.1.1",
            "jsonschema==4.26.0",
        ],
        cwd=layout.root,
        environment=environment,
        stage="D3_INSTALL_WHEEL",
        timeout_seconds=900,
    )
    provenance = run_checked(
        [
            str(python),
            "-c",
            "import mechanical_design_agent as p; print(p.__file__)",
        ],
        cwd=layout.root,
        environment=environment,
        stage="D3_INSTALLED_PROVENANCE",
        timeout_seconds=60,
    )
    assert Path(provenance.stdout.strip()).resolve().is_relative_to(venv.resolve())
    assert cli.is_file() and mcp.is_file()
    return wheel, sdist, python, cli, mcp


@pytest.mark.skipif(os.name != "nt", reason="Windows Unicode path contract")
def test_installed_wheel_provenance_survives_unicode_space_path(
    tmp_path: Path,
) -> None:
    base = tmp_path / "数据库 部署 验收"
    base.mkdir()
    layout = create_attempt_layout(base)
    environment = clean_deployment_environment(layout.root)

    _, _, python, _, _ = _build_and_install(layout, environment)
    provenance = run_checked(
        [
            str(python),
            "-c",
            "import mechanical_design_agent as p; print(p.__file__)",
        ],
        cwd=layout.root,
        environment=environment,
        stage="D3_UNICODE_INSTALLED_PROVENANCE",
        timeout_seconds=60,
    )
    package_path = Path(provenance.stdout.strip()).resolve()

    assert package_path.is_relative_to((layout.root / "venv").resolve())
    assert not package_path.is_relative_to(PROJECT_ROOT.resolve())


def _materialize_live_bundle(destination: Path) -> None:
    for relative in LIVE_BUNDLE_FILES:
        source = PROJECT_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _source_state() -> tuple[str, str]:
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return tree, status


def _inspect_running_services(
    layout,
    environment: dict[str, str],
    *,
    postgres_port: int,
    neo4j_port: int,
) -> dict[str, object]:
    expected_arch = docker_platform_architecture(platform.machine())
    assert expected_arch is not None
    services = {
        "postgres": (POSTGRES_IMAGE, postgres_port, "5432/tcp"),
        "neo4j": (NEO4J_IMAGE, neo4j_port, "7687/tcp"),
    }
    facts: dict[str, object] = {"platform": f"linux/{expected_arch}"}
    for service, (image, host_port, container_port) in services.items():
        identifier = run_checked(
            [
                "docker",
                "compose",
                "--env-file",
                str(layout.env_file),
                "-f",
                str(layout.deployment / "compose.yaml"),
                "-p",
                layout.project,
                "ps",
                "-q",
                service,
            ],
            cwd=layout.deployment,
            environment=environment,
            stage="D3_INSPECT_SERVICE",
            timeout_seconds=60,
        ).stdout.strip()
        assert identifier
        inspected = run_checked(
            ["docker", "inspect", identifier],
            cwd=layout.deployment,
            environment=environment,
            stage="D3_INSPECT_CONTAINER",
            timeout_seconds=60,
        )
        record = json.loads(inspected.stdout)[0]
        assert record["Config"]["Image"] == image
        assert record["State"]["Health"]["Status"] == "healthy"
        bindings = record["NetworkSettings"]["Ports"][container_port]
        assert bindings == [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]
        image_record = json.loads(
            run_checked(
                ["docker", "image", "inspect", image],
                cwd=layout.deployment,
                environment=environment,
                stage="D3_INSPECT_IMAGE",
                timeout_seconds=60,
            ).stdout
        )[0]
        assert image_record["Os"] == "linux"
        assert image_record["Architecture"] == expected_arch
        facts[f"{service}_image"] = image
    return facts


def _initialize_workspace(cli: Path, layout, environment: dict[str, str]) -> None:
    run_checked(
        [
            str(cli),
            "init",
            "--workspace",
            str(layout.workspace),
            "--actor-id",
            "example-user",
        ],
        cwd=layout.root,
        environment=environment,
        stage="D3_INITIALIZE_WORKSPACE",
        timeout_seconds=60,
    )


def _create_and_select_family(cli: Path, layout, environment: dict[str, str]) -> None:
    run_checked(
        [
            str(cli),
            "family",
            "create",
            "--workspace",
            str(layout.workspace),
            "--organization-id",
            "example-org",
            "--organization-name",
            "Example Organization",
            "--design-group-id",
            "example-design-group",
            "--design-group-name",
            "Example Design Group",
            "--family-id",
            "example-family",
            "--family-name",
            "Example Product Family",
            "--set-default",
        ],
        cwd=layout.root,
        environment=environment,
        stage="D3_CREATE_FAMILY",
        timeout_seconds=60,
    )


async def _installed_mcp_contract(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, dict[str, object]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=str(executable),
        cwd=cwd,
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            status = await session.call_tool(
                "design_system_status",
                {},
                read_timeout_seconds=timedelta(seconds=20),
            )
    assert status.content
    return {
        tool.name: tool.inputSchema
        for tool in sorted(listed.tools, key=lambda item: item.name)
    }


def _source_mcp_contract() -> dict[str, dict[str, object]]:
    from mechanical_design_agent.server import create_mcp

    mcp = create_mcp()
    return {
        name: tool.parameters
        for name, tool in sorted(mcp._tool_manager._tools.items())
    }


def _run_installed_live_bundle(
    *,
    python: Path,
    bundle: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    junit_path = Path(environment["MECH_DESIGN_D3_CHILD_SUMMARY"]).with_name(
        "child-junit.xml"
    )
    result = run_checked(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            f"--junitxml={junit_path}",
            *LIVE_NODE_IDS,
        ],
        cwd=bundle,
        environment=environment,
        stage="D3_INSTALLED_LIVE_BUNDLE",
        timeout_seconds=1800,
        junit_path=junit_path,
    )
    summary = json.loads(Path(environment["MECH_DESIGN_D3_CHILD_SUMMARY"]).read_text())
    summary["pytest_output_digest"] = hashlib.sha256(
        result.stdout.encode("utf-8")
    ).hexdigest()
    return summary


@pytest.mark.skipif(
    not supported_live_platform(sys.platform) or os.environ.get(LIVE_OPT_IN) != "1",
    reason="set MECH_DESIGN_DOCKER_DATABASE_LIVE=1 on an approved D3 host",
)
def test_clean_installed_wheel_database_deployment() -> None:
    source_before = _source_state()
    source_contract = _source_mcp_contract()
    safe_output = os.environ.get("MECH_DESIGN_D3_SAFE_EVIDENCE_DIR", "").strip()
    with tempfile.TemporaryDirectory(prefix="md3dcad-d3-base-") as base_value:
        base = Path(base_value)
        layout = create_attempt_layout(base)
        candidate = materialize_candidate_inputs(PROJECT_ROOT, layout)
        environment = clean_deployment_environment(layout.root)
        wheel, sdist, python, cli, mcp = _build_and_install(layout, environment)
        _materialize_live_bundle(layout.root / "live-bundle")
        candidate.update(
            wheel_sha256=_sha256(wheel),
            sdist_sha256=_sha256(sdist),
        )

        postgres_port = _free_loopback_port()
        neo4j_port = _free_loopback_port()
        while neo4j_port == postgres_port:
            neo4j_port = _free_loopback_port()
        token = uuid.uuid4().hex
        postgres_password = f"d3-postgres-{token}"
        neo4j_password = f"d3-neo4j-{token}"
        _write_env_file(
            layout.env_file,
            postgres_port=postgres_port,
            neo4j_port=neo4j_port,
            postgres_password=postgres_password,
            neo4j_password=neo4j_password,
        )
        _initialize_workspace(cli, layout, environment)

        database_url = (
            f"postgresql://mechanical_design:{postgres_password}@127.0.0.1:"
            f"{postgres_port}/mechanical_design"
        )
        neo4j_uri = f"bolt://127.0.0.1:{neo4j_port}"
        child_summary_path = layout.safe_evidence / "child-summary.json"
        live_environment = {
            **environment,
            "MECH_DESIGN_WORKSPACE": str(layout.workspace),
            "MECH_DESIGN_ENV_FILE": str(layout.env_file),
            "MECH_DESIGN_DATABASE_URL": database_url,
            "MECH_DESIGN_NEO4J_URI": neo4j_uri,
            "MECH_DESIGN_NEO4J_USER": "neo4j",
            "MECH_DESIGN_NEO4J_PASSWORD": neo4j_password,
            CHILD_OPT_IN: "1",
            "MECH_DESIGN_WINDOWS_DB_LIVE_CHILD": "1",
            "MECH_DESIGN_D3_CHILD_SUMMARY": str(child_summary_path),
        }
        backend = DockerComposeBackend(
            compose_file=layout.deployment / "compose.yaml",
            env_file=layout.env_file,
            cwd=layout.deployment,
            environment=environment,
        )
        with managed_compose_project(backend, layout.project):
            image_facts = _inspect_running_services(
                layout,
                environment,
                postgres_port=postgres_port,
                neo4j_port=neo4j_port,
            )
            child_summary = _run_installed_live_bundle(
                python=python,
                bundle=layout.root / "live-bundle",
                environment=live_environment,
            )
            _create_and_select_family(cli, layout, live_environment)
            installed_contract = asyncio.run(
                _installed_mcp_contract(
                    mcp,
                    cwd=layout.root,
                    environment=live_environment,
                )
            )
            assert installed_contract == source_contract

        assert backend.list_project_resources(layout.project).empty
        assert _source_state() == source_before
        evidence = {
            "schema_version": "D3DatabaseDeploymentEvidence/v1",
            "status": "passed",
            "candidate": candidate,
            "images": image_facts,
            "bootstrap": child_summary,
            "mcp_contract_sha256": hashlib.sha256(
                json.dumps(installed_contract, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "cleanup": "passed",
            "source_integrity": "passed",
            "privacy": "passed",
        }
        safe_report = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        assert postgres_password not in safe_report
        assert neo4j_password not in safe_report
        assert str(layout.root) not in safe_report
        if safe_output:
            output = Path(safe_output).expanduser().resolve(strict=True)
            report = output / live_evidence_filename(sys.platform)
            report.write_text(safe_report, encoding="utf-8")
            (output / f"{report.name}.sha256").write_text(
                _sha256(report) + "\n",
                encoding="ascii",
            )
