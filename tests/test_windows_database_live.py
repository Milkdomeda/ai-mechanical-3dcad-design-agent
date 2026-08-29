from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import pytest


# Pytest collects this repository's flat tests directory as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_release_helpers as release_helpers  # noqa: E402
from windows_release_helpers import (  # noqa: E402
    DEDICATED_NEO4J_DISPOSABLE_CONFIRMATION,
    DatabaseIsolationError,
    EXPECTED_NEO4J_CONSTRAINTS,
    WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE,
    WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS,
    InstalledWheelEnvironment,
    Neo4jIsolationConfig,
    build_release_artifacts,
    clean_release_environment,
    create_installed_wheel_environment,
    isolated_neo4j_target,
    isolated_postgres_database,
    materialize_windows_live_bundle,
    run_installed_wheel_live_bundle,
)

import mechanical_design_agent
from mechanical_design_agent.migrations import (
    neo4j_migrations_directory,
    postgres_migrations_directory,
)
from mechanical_design_agent.projection import Neo4jProjection
from mechanical_design_agent.repository import PostgresRepository


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
    "010_design_jobs.sql",
    "011_design_job_working_copies.sql",
    "012_design_job_binding_hardening.sql",
    "013_design_job_binding_security.sql",
    "014_design_job_knowledge.sql",
    "015_product_family_match_decisions.sql",
    "016_design_approval_envelopes.sql",
    "017_design_lesson_single_confirmation.sql",
)


class FakePostgresBackend:
    def __init__(self) -> None:
        self.admin = {
            "database": "postgres",
            "user": "w3-test-admin",
            "can_create_database": True,
        }
        self.created: list[str] = []
        self.dropped: list[str] = []
        self.existing: set[str] = set()
        self.derived_database: str | None = None
        self.drop_error: Exception | None = None
        self.observe_created = True
        self.leave_database_after_drop = False

    def inspect_admin(self, _admin_dsn: str) -> dict[str, object]:
        return dict(self.admin)

    def create_database(self, _admin_dsn: str, database_name: str) -> None:
        self.created.append(database_name)
        self.existing.add(database_name)

    def database_exists(self, _admin_dsn: str, database_name: str) -> bool:
        return self.observe_created and database_name in self.existing

    def derive_database_dsn(self, _admin_dsn: str, database_name: str) -> str:
        selected = self.derived_database or database_name
        return f"postgresql://redacted.invalid/{selected}"

    def database_name_from_dsn(self, dsn: str) -> str:
        return dsn.rsplit("/", 1)[-1]

    def drop_database(self, _admin_dsn: str, database_name: str) -> None:
        self.dropped.append(database_name)
        if self.drop_error is not None:
            raise self.drop_error
        if not self.leave_database_after_drop:
            self.existing.discard(database_name)


class FakeNeo4jBackend:
    def __init__(self) -> None:
        self.named_admin = {
            "edition": "enterprise",
            "user": "w3-neo4j-admin",
        }
        self.named_databases: set[str] = set()
        self.named_users: set[str] = set()
        self.named_roles: set[str] = set()
        self.created: list[tuple[str, str, str]] = []
        self.dropped: list[tuple[str, str, str]] = []
        self.disposable = {
            "database": "neo4j",
            "nodes": 0,
            "relationships": 0,
            "constraints": (),
        }
        self.cleanup_error: Exception | None = None
        self.observe_named_target = True
        self.leave_database_after_drop = False

    def inspect_named_admin(self, _config: Neo4jIsolationConfig) -> dict[str, object]:
        return dict(self.named_admin)

    def create_named_target(
        self,
        _config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
        _password: str,
    ) -> None:
        self.created.append((database_name, user_name, role_name))
        self.named_databases.add(database_name)
        self.named_users.add(user_name)
        self.named_roles.add(role_name)

    def named_target_state(
        self,
        _config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> tuple[bool, bool, bool]:
        return (
            self.observe_named_target and database_name in self.named_databases,
            self.observe_named_target and user_name in self.named_users,
            self.observe_named_target and role_name in self.named_roles,
        )

    def drop_named_target(
        self,
        _config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> None:
        self.dropped.append((database_name, user_name, role_name))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.named_users.discard(user_name)
        self.named_roles.discard(role_name)
        if not self.leave_database_after_drop:
            self.named_databases.discard(database_name)

    def inspect_disposable(self, _config: Neo4jIsolationConfig) -> dict[str, object]:
        return dict(self.disposable)

    def clean_disposable(self, _config: Neo4jIsolationConfig) -> None:
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.disposable.update(nodes=0, relationships=0, constraints=())


def _named_config() -> Neo4jIsolationConfig:
    return Neo4jIsolationConfig(
        mode="temporary_named_database",
        uri="bolt://127.0.0.1:7687",
        admin_user="neo4j-admin",
        admin_password="not-a-real-secret",
    )


def _dedicated_config() -> Neo4jIsolationConfig:
    return Neo4jIsolationConfig(
        mode="dedicated_disposable_instance",
        uri="bolt://127.0.0.1:7687",
        admin_user="neo4j",
        admin_password="not-a-real-secret",
        disposable_instance_confirmation=DEDICATED_NEO4J_DISPOSABLE_CONFIRMATION,
    )


def test_neo4j_internal_identifier_accepts_owned_names_and_rejects_injection() -> None:
    assert release_helpers._neo4j_identifier("assertion_id_unique") == "`assertion_id_unique`"
    with pytest.raises(DatabaseIsolationError, match="NEO4J_INTERNAL_IDENTIFIER_INVALID"):
        release_helpers._neo4j_identifier("owned` DETACH DELETE n")


@pytest.mark.parametrize(
    "admin_patch,error_code",
    [
        ({"user": ""}, "POSTGRES_ADMIN_IDENTITY_UNPROVEN"),
        ({"database": ""}, "POSTGRES_ADMIN_IDENTITY_UNPROVEN"),
        ({"can_create_database": False}, "POSTGRES_CREATE_AUTHORITY_UNPROVEN"),
    ],
)
def test_postgres_isolation_fails_before_body_when_admin_proof_is_missing(
    admin_patch: dict[str, object], error_code: str
) -> None:
    backend = FakePostgresBackend()
    backend.admin.update(admin_patch)
    body_called = False

    with pytest.raises(DatabaseIsolationError, match=error_code):
        with isolated_postgres_database("postgresql://redacted", backend=backend):
            body_called = True

    assert body_called is False
    assert backend.created == []
    assert backend.dropped == []


def test_postgres_target_name_is_internal_uuid_and_cleanup_is_verified() -> None:
    backend = FakePostgresBackend()
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")

    with isolated_postgres_database(
        "postgresql://redacted",
        backend=backend,
        uuid_factory=lambda: fixed_uuid,
    ) as database_url:
        assert database_url.endswith("/mechanical_design_w3_12345678123456781234567812345678")
        assert backend.created == ["mechanical_design_w3_12345678123456781234567812345678"]

    assert backend.dropped == backend.created
    assert backend.existing == set()


def test_postgres_target_derivation_mismatch_never_enters_body_and_is_cleaned() -> None:
    backend = FakePostgresBackend()
    backend.derived_database = "some_existing_database"
    body_called = False

    with pytest.raises(DatabaseIsolationError, match="POSTGRES_TARGET_DERIVATION_INVALID"):
        with isolated_postgres_database("postgresql://redacted", backend=backend):
            body_called = True

    assert body_called is False
    assert backend.dropped == backend.created
    assert backend.existing == set()


def test_postgres_unobserved_generated_target_never_enters_body_and_is_cleaned() -> None:
    backend = FakePostgresBackend()
    backend.observe_created = False
    body_called = False

    with pytest.raises(DatabaseIsolationError, match="POSTGRES_TARGET_OWNERSHIP_UNPROVEN"):
        with isolated_postgres_database("postgresql://redacted", backend=backend):
            body_called = True

    assert body_called is False
    assert backend.dropped == backend.created


def test_postgres_cleanup_failure_overrides_a_passing_body_without_leaking_dsn() -> None:
    backend = FakePostgresBackend()
    backend.drop_error = RuntimeError(
        "cleanup failed for postgresql://admin:secret@private-host/postgres"
    )

    with pytest.raises(DatabaseIsolationError) as captured:
        with isolated_postgres_database("postgresql://admin:secret@private-host/postgres", backend=backend):
            pass

    assert captured.value.code == "POSTGRES_CLEANUP_FAILED"
    assert "secret" not in str(captured.value)
    assert "private-host" not in str(captured.value)


def test_postgres_cleanup_must_verify_database_absence() -> None:
    backend = FakePostgresBackend()
    backend.leave_database_after_drop = True

    with pytest.raises(DatabaseIsolationError) as captured:
        with isolated_postgres_database("postgresql://redacted", backend=backend):
            pass

    assert captured.value.code == "POSTGRES_CLEANUP_FAILED"


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_postgres_body_failure_is_preserved_unless_cleanup_also_fails(
    cleanup_fails: bool,
) -> None:
    backend = FakePostgresBackend()
    if cleanup_fails:
        backend.drop_error = RuntimeError("cleanup failed")

    with pytest.raises(Exception) as captured:
        with isolated_postgres_database("postgresql://redacted", backend=backend):
            raise ValueError("synthetic body failure")

    if cleanup_fails:
        assert isinstance(captured.value, DatabaseIsolationError)
        assert captured.value.code == "POSTGRES_CLEANUP_FAILED"
    else:
        assert isinstance(captured.value, ValueError)
        assert str(captured.value) == "synthetic body failure"
    assert backend.dropped == backend.created


def test_neo4j_named_database_uses_generated_database_and_home_user() -> None:
    backend = FakeNeo4jBackend()
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")

    with isolated_neo4j_target(
        _named_config(),
        backend=backend,
        uuid_factory=lambda: fixed_uuid,
        password_factory=lambda: "temporary-password",
    ) as credentials:
        assert credentials.user == "mechanical_design_w3_user_12345678123456781234567812345678"
        assert credentials.password == "temporary-password"
        assert backend.created == [
            (
                "mechanical-design-w3-12345678123456781234567812345678",
                credentials.user,
                "mechanical_design_w3_role_12345678123456781234567812345678",
            )
        ]

    assert backend.dropped == backend.created
    assert backend.named_databases == set()
    assert backend.named_users == set()
    assert backend.named_roles == set()


def test_neo4j_named_database_requires_enterprise_admin_identity() -> None:
    backend = FakeNeo4jBackend()
    backend.named_admin["edition"] = "community"
    body_called = False

    with pytest.raises(DatabaseIsolationError, match="NEO4J_NAMED_DATABASE_UNAVAILABLE"):
        with isolated_neo4j_target(_named_config(), backend=backend):
            body_called = True

    assert body_called is False
    assert backend.created == []


def test_neo4j_unobserved_named_target_never_enters_body_and_is_cleaned() -> None:
    backend = FakeNeo4jBackend()
    backend.observe_named_target = False
    body_called = False

    with pytest.raises(DatabaseIsolationError, match="NEO4J_TARGET_OWNERSHIP_UNPROVEN"):
        with isolated_neo4j_target(_named_config(), backend=backend):
            body_called = True

    assert body_called is False
    assert backend.dropped == backend.created


@pytest.mark.parametrize(
    "patch,error_code",
    [
        ({"nodes": 1}, "NEO4J_DISPOSABLE_TARGET_NOT_EMPTY"),
        ({"relationships": 1}, "NEO4J_DISPOSABLE_TARGET_NOT_EMPTY"),
        ({"constraints": ("existing_constraint",)}, "NEO4J_DISPOSABLE_SCHEMA_NOT_EMPTY"),
    ],
)
def test_neo4j_dedicated_instance_rejects_nonempty_or_preexisting_schema(
    patch: dict[str, object], error_code: str
) -> None:
    backend = FakeNeo4jBackend()
    backend.disposable.update(patch)
    body_called = False

    with pytest.raises(DatabaseIsolationError, match=error_code):
        with isolated_neo4j_target(_dedicated_config(), backend=backend):
            body_called = True

    assert body_called is False


def test_neo4j_dedicated_instance_requires_exact_strong_opt_in() -> None:
    backend = FakeNeo4jBackend()
    config = replace(_dedicated_config(), disposable_instance_confirmation="yes")

    with pytest.raises(DatabaseIsolationError, match="NEO4J_DISPOSABLE_OPT_IN_REQUIRED"):
        with isolated_neo4j_target(config, backend=backend):
            pytest.fail("body must not run")


def test_neo4j_unknown_mode_is_rejected_before_backend_access() -> None:
    backend = FakeNeo4jBackend()
    config = replace(_named_config(), mode="shared_default_database")

    with pytest.raises(DatabaseIsolationError, match="NEO4J_ISOLATION_MODE_INVALID"):
        with isolated_neo4j_target(config, backend=backend):
            pytest.fail("body must not run")

    assert backend.created == []


@pytest.mark.parametrize("mode", ["temporary_named_database", "dedicated_disposable_instance"])
def test_neo4j_cleanup_failure_overrides_passing_body_and_redacts_secrets(mode: str) -> None:
    backend = FakeNeo4jBackend()
    backend.cleanup_error = RuntimeError("bolt://neo4j:secret@private-host:7687 cleanup failed")
    config = _named_config() if mode == "temporary_named_database" else _dedicated_config()

    with pytest.raises(DatabaseIsolationError) as captured:
        with isolated_neo4j_target(config, backend=backend):
            pass

    assert captured.value.code == "NEO4J_CLEANUP_FAILED"
    assert "secret" not in str(captured.value)
    assert "private-host" not in str(captured.value)


def test_neo4j_named_cleanup_requires_database_and_user_to_be_absent() -> None:
    backend = FakeNeo4jBackend()
    backend.leave_database_after_drop = True

    with pytest.raises(DatabaseIsolationError) as captured:
        with isolated_neo4j_target(_named_config(), backend=backend):
            pass

    assert captured.value.code == "NEO4J_CLEANUP_FAILED"
    assert backend.named_users == set()
    assert backend.named_roles == set()
    assert backend.named_databases


@pytest.mark.parametrize(
    ("mode", "cleanup_fails"),
    [
        ("temporary_named_database", False),
        ("temporary_named_database", True),
        ("dedicated_disposable_instance", False),
        ("dedicated_disposable_instance", True),
    ],
)
def test_neo4j_body_failure_is_preserved_unless_cleanup_also_fails(
    mode: str,
    cleanup_fails: bool,
) -> None:
    backend = FakeNeo4jBackend()
    if cleanup_fails:
        backend.cleanup_error = RuntimeError("cleanup failed")
    config = _named_config() if mode == "temporary_named_database" else _dedicated_config()

    with pytest.raises(Exception) as captured:
        with isolated_neo4j_target(config, backend=backend):
            raise ValueError("synthetic body failure")

    if cleanup_fails:
        assert isinstance(captured.value, DatabaseIsolationError)
        assert captured.value.code == "NEO4J_CLEANUP_FAILED"
    else:
        assert isinstance(captured.value, ValueError)
        assert str(captured.value) == "synthetic body failure"


def test_live_bundle_is_an_exact_file_allowlist_without_globs() -> None:
    assert WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE == (
        "tests/windows_release_helpers.py",
        "tests/test_windows_database_live.py",
        "tests/test_migrations.py",
        "tests/test_design_lifecycle.py",
        "tests/test_design_lesson_repository.py",
        "tests/test_design_lesson_outbox.py",
        "tests/test_design_lesson_projection.py",
        "tests/test_design_lesson_reviews.py",
    )
    assert all("*" not in path and "?" not in path for path in WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE)
    assert all((PROJECT_ROOT / path).is_file() for path in WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE)


def test_live_bundle_materialization_copies_only_allowlisted_files(tmp_path: Path) -> None:
    destination = tmp_path / "outside checkout" / "w3-live-bundle"

    copied = materialize_windows_live_bundle(PROJECT_ROOT, destination)

    assert copied == tuple(destination / path for path in WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE)
    assert tuple(
        sorted(
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        )
    ) == tuple(sorted(WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE))


def test_installed_live_runner_uses_exact_nodes_and_passes_only_target_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    installed = InstalledWheelEnvironment(
        venv=tmp_path / "venv",
        python=tmp_path / "venv" / "Scripts" / "python.exe",
        cli=tmp_path / "venv" / "Scripts" / "mechanical-design.exe",
        mcp=tmp_path / "venv" / "Scripts" / "mechanical-design-mcp.exe",
    )
    install_environments: list[dict[str, str]] = []
    install_commands: list[list[str]] = []
    child_environments: list[dict[str, str]] = []
    observed_command: list[str] = []
    observed_subprocess_kwargs: dict[str, object] = {}

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        expected_returncode: int = 0,
        timeout: int = 120,
    ) -> object:
        del cwd, expected_returncode
        assert timeout == 900
        install_commands.append(list(command))
        install_environments.append(dict(environment))
        return object()

    def fake_subprocess_run(command: list[str], **kwargs: object):
        observed_command.extend(command)
        observed_subprocess_kwargs.update(kwargs)
        child_environment = dict(kwargs["env"])
        child_environments.append(child_environment)
        report_argument = next(item for item in command if item.startswith("--junitxml="))
        report = Path(report_argument.split("=", 1)[1])
        report.write_text(
            '<testsuites tests="42" failures="0" errors="0" skipped="0"></testsuites>',
            encoding="utf-8",
        )
        return type(
            "Result",
            (),
            {"args": command, "returncode": 0, "stdout": "42 passed", "stderr": ""},
        )()

    monkeypatch.setattr("windows_release_helpers.shutil.which", lambda _name: "uv")
    monkeypatch.setattr("windows_release_helpers.run_checked", fake_run_checked)
    monkeypatch.setattr("windows_release_helpers.subprocess.run", fake_subprocess_run)
    credentials = type(
        "Credentials",
        (),
        {
            "uri": "bolt://target.invalid:7687",
            "user": "temporary-user",
            "password": "temporary-password",
            "isolation_mode": "temporary_named_database",
        },
    )()

    result = run_installed_wheel_live_bundle(
        installed=installed,
        bundle_root=bundle_root,
        environment={
            "PYTHONPATH": "must-not-pass",
            "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN": "admin-dsn-must-not-pass",
            "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD": "admin-secret-must-not-pass",
            "SAFE_PARENT_VALUE": "preserved",
        },
        database_url="postgresql://target.invalid/w3",
        neo4j_credentials=credentials,
        offline=True,
    )

    assert len(install_environments) == 1
    assert install_commands[0][1:4] == ["pip", "install", "--offline"]
    assert len(child_environments) == 1
    for observed in (*install_environments, *child_environments):
        assert "PYTHONPATH" not in observed
        assert "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN" not in observed
        assert "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD" not in observed
        assert observed["SAFE_PARENT_VALUE"] == "preserved"
    assert child_environments[0]["MECH_DESIGN_DATABASE_URL"].endswith("/w3")
    assert child_environments[0]["MECH_DESIGN_NEO4J_USER"] == "temporary-user"
    assert child_environments[0]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert all(node in observed_command for node in WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS)
    assert observed_command[-len(WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS):] == list(
        WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS
    )
    assert "-p" in observed_command and "no:cacheprovider" in observed_command
    assert observed_subprocess_kwargs["encoding"] == "utf-8"
    assert observed_subprocess_kwargs["errors"] == "replace"
    assert "W3_LIVE_SUMMARY=" in result.stdout


def test_database_live_failure_output_redacts_credentials_endpoints_and_hosts() -> None:
    value = (
        "postgresql://admin:secret@private-db.example/postgres failed; "
        "bolt://neo4j:graph-secret@private-graph.example:7687 failed"
    )

    redacted = release_helpers._redact_database_output(
        value,
        ("secret", "graph-secret"),
    )

    assert "secret" not in redacted
    assert "private-db.example" not in redacted
    assert "private-graph.example" not in redacted
    assert "postgresql://" not in redacted
    assert "bolt://" not in redacted


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_WINDOWS_DB_LIVE_CHILD") != "1",
    reason="installed-wheel PostgreSQL contract runs only inside the W3 child bundle",
)
def test_installed_postgres_migration_contract() -> None:
    package_path = Path(mechanical_design_agent.__file__).resolve()
    assert package_path.is_relative_to(Path(sys.prefix).resolve())
    assert "PYTHONPATH" not in os.environ
    database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
    repository = PostgresRepository(database_url)

    with postgres_migrations_directory() as root:
        root = root.resolve()
        assert root.is_relative_to(Path(sys.prefix).resolve())
        expected_digests = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*.sql"))
        }
        first = repository.apply_migrations(root)
        second = repository.apply_migrations(root)

    assert first == {"applied": list(EXPECTED_POSTGRES_MIGRATIONS), "skipped": []}
    assert second == {"applied": [], "skipped": list(EXPECTED_POSTGRES_MIGRATIONS)}
    with repository.connection() as connection:
        ledger = connection.execute(
            "SELECT version,filename,sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        extensions = connection.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm') ORDER BY extname"
        ).fetchall()
    assert [
        (int(row["version"]), row["filename"], row["sha256"])
        for row in ledger
    ] == [
        (index, filename, expected_digests[filename])
        for index, filename in enumerate(EXPECTED_POSTGRES_MIGRATIONS, start=1)
    ]
    assert [row["extname"] for row in extensions] == ["pg_trgm", "vector"]


def _neo4j_constraints(projection: Neo4jProjection) -> list[str]:
    with projection._driver() as driver, driver.session() as session:
        return [
            record["name"]
            for record in session.run(
                "SHOW CONSTRAINTS YIELD name RETURN name ORDER BY name"
            )
        ]


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_WINDOWS_DB_LIVE_CHILD") != "1",
    reason="installed-wheel Neo4j contract runs only inside the W3 child bundle",
)
def test_installed_neo4j_migration_contract() -> None:
    projection = Neo4jProjection(
        os.environ["MECH_DESIGN_NEO4J_URI"],
        os.environ["MECH_DESIGN_NEO4J_USER"],
        os.environ["MECH_DESIGN_NEO4J_PASSWORD"],
    )
    with neo4j_migrations_directory() as root:
        root = root.resolve()
        assert root.is_relative_to(Path(sys.prefix).resolve())
        files = tuple(path.name for path in sorted(root.glob("*.cypher")))
        digests = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*.cypher"))
        }
    assert files == (
        "001_constraints.cypher",
        "002_design_lessons.cypher",
        "003_projection_state.cypher",
    )
    assert all(len(value) == 64 for value in digests.values())
    assert _neo4j_constraints(projection) == []

    projection.initialize_constraints()
    first = _neo4j_constraints(projection)
    projection.initialize_constraints()
    second = _neo4j_constraints(projection)

    assert first == sorted(EXPECTED_NEO4J_CONSTRAINTS)
    assert second == first


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_WINDOWS_DB_LIVE_CHILD") != "1",
    reason="installed-wheel graph rebuild runs only inside the W3 child bundle",
)
def test_installed_neo4j_rebuild_projection_state_and_scoped_retrieval() -> None:
    repository = PostgresRepository(os.environ["MECH_DESIGN_DATABASE_URL"])
    projection = Neo4jProjection(
        os.environ["MECH_DESIGN_NEO4J_URI"],
        os.environ["MECH_DESIGN_NEO4J_USER"],
        os.environ["MECH_DESIGN_NEO4J_PASSWORD"],
    )

    rebuilt = projection.rebuild(repository)
    assert rebuilt["status"] == "rebuilt-from-postgresql"
    assert rebuilt["authoritative_source"] == "postgresql"
    assert all(value == 0 for value in rebuilt["counts"].values())

    token = uuid.uuid4().hex
    family_id = f"w3-family-{token}"
    model_id = f"w3-model-{token}"
    owner = "freecad-mechanical-design-agent"
    with projection._driver() as driver, driver.session() as session:
        state = session.run(
            "MATCH (s:ProjectionState {name:'mechanical-design-agent'}) "
            "RETURN s.active_generation AS generation"
        ).single()
        assert state is not None and state["generation"] == rebuilt["active_generation"]
        session.run(
            "CREATE (m:ModelRevision {id:$model_id,projection_owner:$owner})"
            "-[:BELONGS_TO]->"
            "(f:ProductFamily {id:$family_id,projection_owner:$owner})",
            model_id=model_id,
            family_id=family_id,
            owner=owner,
        ).consume()
    try:
        model_rows = projection.scoped_relationships(
            family_id=None,
            model_revision_id=model_id,
        )
        family_rows = projection.scoped_relationships(
            family_id=family_id,
            model_revision_id=None,
        )
        assert [(row["source_id"], row["relationship"], row["target_id"]) for row in model_rows] == [
            (model_id, "BELONGS_TO", family_id)
        ]
        assert [(row["source_id"], row["relationship"], row["target_id"]) for row in family_rows] == [
            (model_id, "BELONGS_TO", family_id)
        ]
    finally:
        with projection._driver() as driver, driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.id IN $ids AND n.projection_owner=$owner DETACH DELETE n",
                ids=[family_id, model_id],
                owner=owner,
            ).consume()


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MECH_DESIGN_WINDOWS_DB_LIVE_TESTS") != "1",
    reason="real Windows W3 database live gate requires explicit opt-in",
)
def test_windows_installed_wheel_database_live_gate() -> None:
    root_value = os.environ.get("MECH_DESIGN_W3_ROOT", "").strip()
    admin_dsn = os.environ.get("MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN", "").strip()
    neo4j_mode = os.environ.get("MECH_DESIGN_WINDOWS_NEO4J_MODE", "").strip()
    neo4j_uri = os.environ.get("MECH_DESIGN_WINDOWS_NEO4J_ADMIN_URI", "").strip()
    neo4j_user = os.environ.get("MECH_DESIGN_WINDOWS_NEO4J_ADMIN_USER", "").strip()
    neo4j_password = os.environ.get("MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD", "")
    assert root_value, "MECH_DESIGN_W3_ROOT must select the isolated fixed-NTFS test root"
    assert admin_dsn, "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN is required"
    config = Neo4jIsolationConfig(
        mode=neo4j_mode,
        uri=neo4j_uri,
        admin_user=neo4j_user,
        admin_password=neo4j_password,
        disposable_instance_confirmation=os.environ.get(
            "MECH_DESIGN_WINDOWS_NEO4J_DISPOSABLE_CONFIRMATION", ""
        ),
    )

    gate_root = Path(root_value).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="w3-database-live-", dir=gate_root) as value:
        root = Path(value)
        outside = root / "outside checkout"
        outside.mkdir()
        prepared_cache_value = os.environ.get("UV_CACHE_DIR", "").strip()
        assert prepared_cache_value, "W3 requires the Runbook-prepared UV cache"
        environment = clean_release_environment(
            root,
            uv_cache_dir=Path(prepared_cache_value),
        )
        wheel, _sdist = build_release_artifacts(
            project_root=PROJECT_ROOT,
            root=root,
            environment=environment,
        )
        installed = create_installed_wheel_environment(
            wheel=wheel,
            root=root,
            outside=outside,
            environment=environment,
            offline=True,
        )
        bundle_root = outside / f"w3-live-bundle-{uuid.uuid4().hex}"
        materialize_windows_live_bundle(PROJECT_ROOT, bundle_root)

        with isolated_postgres_database(admin_dsn) as database_url:
            with isolated_neo4j_target(config) as neo4j_credentials:
                result = run_installed_wheel_live_bundle(
                    installed=installed,
                    bundle_root=bundle_root,
                    environment=environment,
                    database_url=database_url,
                    neo4j_credentials=neo4j_credentials,
                    offline=True,
                )
        summary = json.loads(result.stdout.rsplit("W3_LIVE_SUMMARY=", 1)[1].splitlines()[0])
        assert summary["failed"] == 0
        assert summary["unexpected_skips"] == 0
        assert summary["installed_provenance"] is True
