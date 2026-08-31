from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from typing import Callable, Iterator, Literal, Protocol
import uuid
import xml.etree.ElementTree as ET
import zipfile


REQUIRED_INSTALLED_RESOURCES = frozenset(
    {
        "config/standard_part_providers.json",
        "freecad/create_empty_model.py",
        "freecad/extract_model_manifest.py",
        "freecad/normalize_model.py",
        "freecad/validate_external_step.py",
        "freecad/validate_fastener_interfaces.py",
        "freecad/validate_mechanical_interfaces.py",
        "migrations/neo4j/001_knowledge_projection.cypher",
        "migrations/postgres/001_knowledge.sql",
        "validation/step_component.json",
    }
)

_CLEAN_ENVIRONMENT_KEYS = (
    "PYTHONPATH",
    "MECH_DESIGN_WORKSPACE",
    "MECH_DESIGN_ENV_FILE",
    "MECH_DESIGN_ACTOR_ID",
    "MECH_DESIGN_DATABASE_URL",
    "MECH_DESIGN_NEO4J_URI",
    "MECH_DESIGN_NEO4J_USER",
    "MECH_DESIGN_NEO4J_PASSWORD",
    "MECH_DESIGN_FREECADCMD",
    "MECH_DESIGN_FREECADCMD_EXPECTED_VERSION",
    "MECH_DESIGN_ARTIFACT_ROOT",
    "MECH_DESIGN_PRODUCT_FAMILY_ID",
    "MECH_DESIGN_FAMILY_CONFIG",
    "MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN",
    "MECH_DESIGN_WINDOWS_NEO4J_MODE",
    "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_URI",
    "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_USER",
    "MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD",
    "MECH_DESIGN_WINDOWS_NEO4J_DISPOSABLE_CONFIRMATION",
    "MECH_DESIGN_WINDOWS_DB_LIVE_CHILD",
)

POSTGRES_DATABASE_PREFIX = "mechanical_design_w3_"
NEO4J_DATABASE_PREFIX = "mechanical-design-w3-"
NEO4J_USER_PREFIX = "mechanical_design_w3_user_"
NEO4J_ROLE_PREFIX = "mechanical_design_w3_role_"
DEDICATED_NEO4J_DISPOSABLE_CONFIRMATION = (
    "I_UNDERSTAND_THIS_NEO4J_INSTANCE_IS_DISPOSABLE"
)
EXPECTED_NEO4J_CONSTRAINTS = frozenset(
    {
        "knowledge_assertion_id_unique",
        "design_lesson_id_unique",
        "product_family_id_unique",
    }
)

WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE = (
    "tests/windows_release_helpers.py",
    "tests/test_migrations.py",
    "tests/test_design_lesson_repository.py",
    "tests/test_projection.py",
    "tests/test_product_family_knowledge.py",
)

WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS = (
    "tests/test_migrations.py",
    "tests/test_design_lesson_repository.py",
    "tests/test_projection.py",
    "tests/test_product_family_knowledge.py",
)


@dataclass(frozen=True)
class InstalledWheelEnvironment:
    venv: Path
    python: Path
    cli: Path
    mcp: Path


_W4_SAFE_STAGES = frozenset(
    {
        "W4_CACHE_ONLINE_BUILD",
        "W4_CACHE_LOCK_EXPORT",
        "W4_CACHE_WARMUP_VENV",
        "W4_CACHE_WARMUP_PROJECT_INSTALL",
        "W4_CACHE_WARMUP_TEST_INSTALL",
        "W4_CACHE_OFFLINE_BUILD",
        "W4_CACHE_OFFLINE_VENV",
        "W4_CACHE_OFFLINE_PROJECT_INSTALL",
        "W4_CACHE_OFFLINE_TEST_INSTALL",
        "W4_LIVE_OFFLINE_BUILD",
        "W4_LIVE_OFFLINE_VENV",
        "W4_LIVE_OFFLINE_PROJECT_INSTALL",
    }
)


class SafeSubprocessError(AssertionError):
    def __init__(
        self,
        *,
        stage: str,
        indicator: str,
        returncode: int | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        if stage not in _W4_SAFE_STAGES:
            raise ValueError("unsupported W4 safe subprocess stage")
        self.stage = stage
        self.indicator = indicator
        self.returncode = returncode
        self.timeout_seconds = timeout_seconds
        facts = [stage, f"indicator={indicator}"]
        if returncode is not None:
            facts.append(f"returncode={returncode}")
        if timeout_seconds is not None:
            facts.append(f"timeout_seconds={timeout_seconds}")
        super().__init__(" ".join(facts))


def _safe_subprocess_indicator(
    command: list[str],
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> str:
    text = f"{stdout or ''}\n{stderr or ''}".casefold()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "certificate" in text or "tls" in text or "ssl" in text:
        return "certificate"
    if "access is denied" in text or "access denied" in text or "permission denied" in text:
        return "access_denied"
    if "sharing violation" in text or "being used by another process" in text:
        return "sharing_violation"
    if "decode" in text or "codec" in text or "unicode" in text:
        return "decode_error"
    if "no solution found" in text or "unsatisfiable" in text:
        return "no_solution"
    if "--offline" in command and any(
        phrase in text
        for phrase in (
            "no matching distribution",
            "not found in the cache",
            "cache entry missing",
            "not available in the cache",
        )
    ):
        return "offline_cache_miss"
    if any(
        phrase in text
        for phrase in (
            "connection",
            "network",
            "name resolution",
            "dns",
            "retry",
        )
    ):
        return "network"
    return "unknown"


@dataclass(frozen=True)
class CleanupResult:
    attempted: bool
    succeeded: bool
    verified_absent_or_empty: bool
    redacted_error: str | None = None


class DatabaseIsolationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Neo4jIsolationConfig:
    mode: Literal[
        "temporary_named_database",
        "dedicated_disposable_instance",
    ] | str
    uri: str
    admin_user: str
    admin_password: str
    disposable_instance_confirmation: str = ""


@dataclass(frozen=True)
class Neo4jCredentials:
    uri: str
    user: str
    password: str
    isolation_mode: str


class _PostgresIsolationBackend(Protocol):
    def inspect_admin(self, admin_dsn: str) -> dict[str, object]: ...
    def create_database(self, admin_dsn: str, database_name: str) -> None: ...
    def database_exists(self, admin_dsn: str, database_name: str) -> bool: ...
    def derive_database_dsn(self, admin_dsn: str, database_name: str) -> str: ...
    def database_name_from_dsn(self, dsn: str) -> str: ...
    def drop_database(self, admin_dsn: str, database_name: str) -> None: ...


class _Neo4jIsolationBackend(Protocol):
    def inspect_named_admin(self, config: Neo4jIsolationConfig) -> dict[str, object]: ...
    def create_named_target(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
        password: str,
    ) -> None: ...
    def named_target_state(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> tuple[bool, bool, bool]: ...
    def drop_named_target(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> None: ...
    def inspect_disposable(self, config: Neo4jIsolationConfig) -> dict[str, object]: ...
    def clean_disposable(self, config: Neo4jIsolationConfig) -> None: ...


def _safe_failure(code: str, message: str) -> DatabaseIsolationError:
    return DatabaseIsolationError(code, message)


class _PsycopgIsolationBackend:
    def inspect_admin(self, admin_dsn: str) -> dict[str, object]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT current_database() AS database,current_user AS user,"
                "(rolcreatedb OR rolsuper) AS can_create_database "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
        return dict(row or {})

    def create_database(self, admin_dsn: str, database_name: str) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )

    def database_exists(self, admin_dsn: str, database_name: str) -> bool:
        import psycopg

        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)",
                (database_name,),
            ).fetchone()
        return bool(row and row[0])

    def derive_database_dsn(self, admin_dsn: str, database_name: str) -> str:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        values = conninfo_to_dict(admin_dsn)
        values["dbname"] = database_name
        return make_conninfo(**values)

    def database_name_from_dsn(self, dsn: str) -> str:
        from psycopg.conninfo import conninfo_to_dict

        return str(conninfo_to_dict(dsn).get("dbname", ""))

    def drop_database(self, admin_dsn: str, database_name: str) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


def _neo4j_identifier(value: str) -> str:
    if re.fullmatch(r"[a-z0-9_-]+", value) is None:
        raise _safe_failure(
            "NEO4J_INTERNAL_IDENTIFIER_INVALID",
            "generated identifier is invalid",
        )
    return f"`{value}`"


class _DriverNeo4jIsolationBackend:
    def _driver(self, config: Neo4jIsolationConfig):
        from neo4j import GraphDatabase

        return GraphDatabase.driver(
            config.uri,
            auth=(config.admin_user, config.admin_password),
        )

    def inspect_named_admin(self, config: Neo4jIsolationConfig) -> dict[str, object]:
        with self._driver(config) as driver:
            driver.verify_connectivity()
            with driver.session() as session:
                component = session.run(
                    "CALL dbms.components() YIELD edition RETURN edition LIMIT 1"
                ).single()
        return {
            "edition": str(component["edition"] if component else "").lower(),
            "user": config.admin_user,
        }

    def create_named_target(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
        password: str,
    ) -> None:
        database = _neo4j_identifier(database_name)
        user = _neo4j_identifier(user_name)
        role = _neo4j_identifier(role_name)
        with self._driver(config) as driver, driver.session(database="system") as session:
            session.run(f"CREATE DATABASE {database} WAIT 60 SECONDS").consume()
            session.run(f"CREATE ROLE {role}").consume()
            session.run(f"GRANT ACCESS ON DATABASE {database} TO {role}").consume()
            session.run(f"GRANT MATCH {{*}} ON GRAPH {database} TO {role}").consume()
            session.run(f"GRANT WRITE ON GRAPH {database} TO {role}").consume()
            session.run(
                f"GRANT NAME MANAGEMENT ON DATABASE {database} TO {role}"
            ).consume()
            session.run(
                f"GRANT CONSTRAINT MANAGEMENT ON DATABASE {database} TO {role}"
            ).consume()
            session.run(
                f"CREATE USER {user} SET PASSWORD $password CHANGE NOT REQUIRED "
                f"SET HOME DATABASE {database}",
                password=password,
            ).consume()
            session.run(f"GRANT ROLE {role} TO {user}").consume()

    def named_target_state(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> tuple[bool, bool, bool]:
        with self._driver(config) as driver, driver.session(database="system") as session:
            database = session.run(
                "SHOW DATABASES YIELD name WHERE name=$name RETURN count(*) AS count",
                name=database_name,
            ).single()
            user = session.run(
                "SHOW USERS YIELD user WHERE user=$user RETURN count(*) AS count",
                user=user_name,
            ).single()
            role = session.run(
                "SHOW ROLES YIELD role WHERE role=$role RETURN count(*) AS count",
                role=role_name,
            ).single()
        return (
            bool(database and database["count"] == 1),
            bool(user and user["count"] == 1),
            bool(role and role["count"] == 1),
        )

    def drop_named_target(
        self,
        config: Neo4jIsolationConfig,
        database_name: str,
        user_name: str,
        role_name: str,
    ) -> None:
        database = _neo4j_identifier(database_name)
        user = _neo4j_identifier(user_name)
        role = _neo4j_identifier(role_name)
        with self._driver(config) as driver, driver.session(database="system") as session:
            session.run(f"DROP USER {user} IF EXISTS").consume()
            session.run(f"DROP ROLE {role} IF EXISTS").consume()
            session.run(
                f"DROP DATABASE {database} IF EXISTS DESTROY DATA WAIT 60 SECONDS"
            ).consume()

    def inspect_disposable(self, config: Neo4jIsolationConfig) -> dict[str, object]:
        with self._driver(config) as driver:
            driver.verify_connectivity()
            with driver.session() as session:
                graph = session.run(
                    "MATCH (n) WITH count(n) AS nodes "
                    "OPTIONAL MATCH ()-[r]->() RETURN nodes,count(r) AS relationships"
                ).single()
                constraints = tuple(
                    record["name"]
                    for record in session.run(
                        "SHOW CONSTRAINTS YIELD name RETURN name ORDER BY name"
                    )
                )
                database = session.run(
                    "CALL db.info() YIELD name RETURN name LIMIT 1"
                ).single()
        return {
            "database": str(database["name"] if database else ""),
            "nodes": int(graph["nodes"] if graph else -1),
            "relationships": int(graph["relationships"] if graph else -1),
            "constraints": constraints,
        }

    def clean_disposable(self, config: Neo4jIsolationConfig) -> None:
        before = self.inspect_disposable(config)
        constraints = frozenset(str(name) for name in before["constraints"])
        unexpected = constraints - EXPECTED_NEO4J_CONSTRAINTS
        if unexpected:
            raise _safe_failure(
                "NEO4J_CLEANUP_UNEXPECTED_SCHEMA",
                "dedicated target contains constraints not owned by the W3 test",
            )
        with self._driver(config) as driver, driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
            for name in sorted(constraints):
                session.run(f"DROP CONSTRAINT {_neo4j_identifier(name)} IF EXISTS").consume()


@contextmanager
def isolated_postgres_database(
    admin_dsn: str,
    *,
    backend: _PostgresIsolationBackend | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> Iterator[str]:
    if not admin_dsn.strip():
        raise _safe_failure(
            "POSTGRES_ADMIN_IDENTITY_UNPROVEN",
            "an explicit PostgreSQL test-admin connection is required",
        )
    selected = backend or _PsycopgIsolationBackend()
    try:
        evidence = selected.inspect_admin(admin_dsn)
    except Exception as error:
        raise _safe_failure(
            "POSTGRES_ADMIN_IDENTITY_UNPROVEN",
            f"admin preflight failed ({type(error).__name__})",
        ) from None
    if not str(evidence.get("database", "")).strip() or not str(
        evidence.get("user", "")
    ).strip():
        raise _safe_failure(
            "POSTGRES_ADMIN_IDENTITY_UNPROVEN",
            "PostgreSQL admin identity could not be established",
        )
    if evidence.get("can_create_database") is not True:
        raise _safe_failure(
            "POSTGRES_CREATE_AUTHORITY_UNPROVEN",
            "PostgreSQL test admin lacks verified create-database authority",
        )

    database_name = POSTGRES_DATABASE_PREFIX + uuid_factory().hex
    cleanup_required = False
    try:
        cleanup_required = True
        selected.create_database(admin_dsn, database_name)
        if not selected.database_exists(admin_dsn, database_name):
            raise _safe_failure(
                "POSTGRES_TARGET_OWNERSHIP_UNPROVEN",
                "generated PostgreSQL target was not observed after creation",
            )
        database_dsn = selected.derive_database_dsn(admin_dsn, database_name)
        if selected.database_name_from_dsn(database_dsn) != database_name:
            raise _safe_failure(
                "POSTGRES_TARGET_DERIVATION_INVALID",
                "application connection does not resolve to the generated target",
            )
        yield database_dsn
    finally:
        if cleanup_required:
            try:
                selected.drop_database(admin_dsn, database_name)
                if selected.database_exists(admin_dsn, database_name):
                    raise RuntimeError("generated database remains present")
            except BaseException as error:
                raise _safe_failure(
                    "POSTGRES_CLEANUP_FAILED",
                    f"generated PostgreSQL target cleanup failed ({type(error).__name__})",
                ) from None


@contextmanager
def isolated_neo4j_target(
    config: Neo4jIsolationConfig,
    *,
    backend: _Neo4jIsolationBackend | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> Iterator[Neo4jCredentials]:
    if config.mode not in {
        "temporary_named_database",
        "dedicated_disposable_instance",
    }:
        raise _safe_failure(
            "NEO4J_ISOLATION_MODE_INVALID",
            "Neo4j target must use an approved W3 isolation mode",
        )
    if not config.uri.strip() or not config.admin_user.strip() or not config.admin_password:
        raise _safe_failure(
            "NEO4J_ADMIN_IDENTITY_UNPROVEN",
            "explicit Neo4j test-admin credentials are required",
        )
    selected = backend or _DriverNeo4jIsolationBackend()

    if config.mode == "temporary_named_database":
        try:
            evidence = selected.inspect_named_admin(config)
        except Exception as error:
            raise _safe_failure(
                "NEO4J_ADMIN_IDENTITY_UNPROVEN",
                f"Neo4j admin preflight failed ({type(error).__name__})",
            ) from None
        if not str(evidence.get("user", "")).strip():
            raise _safe_failure(
                "NEO4J_ADMIN_IDENTITY_UNPROVEN",
                "Neo4j admin identity could not be established",
            )
        if str(evidence.get("edition", "")).lower() != "enterprise":
            raise _safe_failure(
                "NEO4J_NAMED_DATABASE_UNAVAILABLE",
                "temporary named-database isolation requires Neo4j Enterprise",
            )
        token = uuid_factory().hex
        database_name = NEO4J_DATABASE_PREFIX + token
        user_name = NEO4J_USER_PREFIX + token
        role_name = NEO4J_ROLE_PREFIX + token
        password = password_factory()
        cleanup_required = False
        try:
            cleanup_required = True
            selected.create_named_target(
                config,
                database_name,
                user_name,
                role_name,
                password,
            )
            if selected.named_target_state(
                config,
                database_name,
                user_name,
                role_name,
            ) != (
                True,
                True,
                True,
            ):
                raise _safe_failure(
                    "NEO4J_TARGET_OWNERSHIP_UNPROVEN",
                    "generated Neo4j database and home user were not both observed",
                )
            yield Neo4jCredentials(
                uri=config.uri,
                user=user_name,
                password=password,
                isolation_mode=config.mode,
            )
        finally:
            if cleanup_required:
                try:
                    selected.drop_named_target(
                        config,
                        database_name,
                        user_name,
                        role_name,
                    )
                    if selected.named_target_state(
                        config,
                        database_name,
                        user_name,
                        role_name,
                    ) != (
                        False,
                        False,
                        False,
                    ):
                        raise RuntimeError("generated Neo4j target remains present")
                except BaseException as error:
                    raise _safe_failure(
                        "NEO4J_CLEANUP_FAILED",
                        f"generated Neo4j target cleanup failed ({type(error).__name__})",
                    ) from None
        return

    if (
        config.disposable_instance_confirmation
        != DEDICATED_NEO4J_DISPOSABLE_CONFIRMATION
    ):
        raise _safe_failure(
            "NEO4J_DISPOSABLE_OPT_IN_REQUIRED",
            "dedicated-instance mode requires the exact destructive test opt-in",
        )
    try:
        before = selected.inspect_disposable(config)
    except Exception as error:
        raise _safe_failure(
            "NEO4J_DISPOSABLE_IDENTITY_UNPROVEN",
            f"dedicated Neo4j preflight failed ({type(error).__name__})",
        ) from None
    if not str(before.get("database", "")).strip():
        raise _safe_failure(
            "NEO4J_DISPOSABLE_IDENTITY_UNPROVEN",
            "dedicated Neo4j database identity could not be established",
        )
    if int(before.get("nodes", -1)) != 0 or int(before.get("relationships", -1)) != 0:
        raise _safe_failure(
            "NEO4J_DISPOSABLE_TARGET_NOT_EMPTY",
            "dedicated Neo4j target is not empty",
        )
    if tuple(before.get("constraints", ())):
        raise _safe_failure(
            "NEO4J_DISPOSABLE_SCHEMA_NOT_EMPTY",
            "dedicated Neo4j target has pre-existing constraints",
        )
    try:
        yield Neo4jCredentials(
            uri=config.uri,
            user=config.admin_user,
            password=config.admin_password,
            isolation_mode=config.mode,
        )
    finally:
        try:
            selected.clean_disposable(config)
            after = selected.inspect_disposable(config)
            if (
                int(after.get("nodes", -1)) != 0
                or int(after.get("relationships", -1)) != 0
                or tuple(after.get("constraints", ()))
            ):
                raise RuntimeError("dedicated Neo4j target is not empty after cleanup")
        except BaseException as error:
            raise _safe_failure(
                "NEO4J_CLEANUP_FAILED",
                f"dedicated Neo4j target cleanup failed ({type(error).__name__})",
            ) from None


def materialize_windows_live_bundle(
    project_root: Path,
    destination: Path,
) -> tuple[Path, ...]:
    project_root = project_root.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    copied: list[Path] = []
    for relative in WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE:
        if "*" in relative or "?" in relative:
            raise AssertionError("Windows installed-wheel live bundle cannot contain globs")
        source = (project_root / relative).resolve(strict=True)
        if not source.is_relative_to(project_root) or not source.is_file():
            raise AssertionError(f"invalid Windows live bundle source: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    if actual != set(WINDOWS_INSTALLED_WHEEL_LIVE_BUNDLE):
        raise AssertionError("materialized Windows live bundle differs from exact allowlist")
    return tuple(copied)


def _redact_database_output(value: str, protected_values: tuple[str, ...]) -> str:
    redacted = value
    for protected in sorted(
        (item for item in protected_values if item),
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(protected, "[REDACTED]")
    redacted = re.sub(
        r"(?i)\b(?:postgres(?:ql)?|bolt|neo4j(?:\+s|\+ssc)?|neo4j)://\S+",
        "[REDACTED_ENDPOINT]",
        redacted,
    )
    return redacted


def run_installed_wheel_live_bundle(
    *,
    installed: InstalledWheelEnvironment,
    bundle_root: Path,
    environment: dict[str, str],
    database_url: str,
    neo4j_credentials: Neo4jCredentials,
    offline: bool = False,
) -> subprocess.CompletedProcess[str]:
    child_base_environment = dict(environment)
    for name in _CLEAN_ENVIRONMENT_KEYS:
        child_base_environment.pop(name, None)
    install_windows_live_test_dependencies(
        python=installed.python,
        cwd=bundle_root,
        environment=child_base_environment,
        offline=offline,
    )
    live_environment = dict(child_base_environment)
    live_environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "MECH_DESIGN_WINDOWS_DB_LIVE_CHILD": "1",
            "MECH_DESIGN_DATABASE_URL": database_url,
            "MECH_DESIGN_NEO4J_URI": neo4j_credentials.uri,
            "MECH_DESIGN_NEO4J_USER": neo4j_credentials.user,
            "MECH_DESIGN_NEO4J_PASSWORD": neo4j_credentials.password,
        }
    )
    report = bundle_root.parent / f"w3-live-junit-{uuid.uuid4().hex}.xml"
    command = [
        str(installed.python),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--disable-warnings",
        f"--junitxml={report}",
        *WINDOWS_INSTALLED_WHEEL_LIVE_NODE_IDS,
    ]
    result = subprocess.run(
        command,
        cwd=bundle_root,
        env=live_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    protected = (
        database_url,
        neo4j_credentials.uri,
        neo4j_credentials.user,
        neo4j_credentials.password,
    )
    safe_stdout = _redact_database_output(result.stdout, protected)
    safe_stderr = _redact_database_output(result.stderr, protected)
    if result.returncode != 0:
        raise AssertionError(
            "installed-wheel W3 live bundle failed:\n"
            + (safe_stdout + "\n" + safe_stderr)[-12000:]
        )
    if not report.is_file():
        raise AssertionError("installed-wheel W3 live bundle did not emit JUnit evidence")
    suite = ET.parse(report).getroot()
    tests = int(suite.attrib.get("tests", 0))
    failures = int(suite.attrib.get("failures", 0))
    errors = int(suite.attrib.get("errors", 0))
    skipped = int(suite.attrib.get("skipped", 0))
    summary = {
        "collected": tests,
        "passed": tests - failures - errors - skipped,
        "failed": failures + errors,
        "skipped": skipped,
        "unexpected_skips": skipped,
        "installed_provenance": True,
        "neo4j_isolation_mode": neo4j_credentials.isolation_mode,
    }
    if summary["failed"] or summary["unexpected_skips"]:
        raise AssertionError(f"installed-wheel W3 live summary failed: {summary}")
    combined_stdout = safe_stdout + "\nW3_LIVE_SUMMARY=" + json.dumps(
        summary,
        sort_keys=True,
    ) + "\n"
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        combined_stdout,
        safe_stderr,
    )


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_returncode: int = 0,
    timeout: int = 120,
    safe_stage: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if safe_stage is None:
            raise
        raise SafeSubprocessError(
            stage=safe_stage,
            indicator="timeout",
            timeout_seconds=timeout,
        ) from None
    if result.returncode != expected_returncode and safe_stage is not None:
        raise SafeSubprocessError(
            stage=safe_stage,
            indicator=_safe_subprocess_indicator(command, result.stdout, result.stderr),
            returncode=result.returncode,
        )
    assert result.returncode == expected_returncode, (
        f"command returned {result.returncode}, expected {expected_returncode}: "
        f"{result.stderr[-4000:]}"
    )
    return result


def clean_release_environment(
    root: Path,
    *,
    uv_cache_dir: Path | None = None,
) -> dict[str, str]:
    prepared_cache: Path | None = None
    if uv_cache_dir is not None:
        selected_cache = Path(uv_cache_dir)
        try:
            metadata = selected_cache.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if selected_cache.is_symlink() or bool(
                getattr(metadata, "st_file_attributes", 0) & reparse_flag
            ):
                raise ValueError("prepared UV cache must not be a symlink or reparse point")
            prepared_cache = selected_cache.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("prepared UV cache must be an existing directory") from exc
        if not prepared_cache.is_dir():
            raise ValueError("prepared UV cache must be an existing directory")

    environment = dict(os.environ)
    for name in _CLEAN_ENVIRONMENT_KEYS:
        environment.pop(name, None)
    isolated_home = root / "isolated-home"
    isolated_home.mkdir()
    environment["HOME"] = str(isolated_home)
    environment["USERPROFILE"] = str(isolated_home)
    environment["UV_CACHE_DIR"] = str(prepared_cache or root / "uv-cache")
    return environment


def build_release_artifacts(
    *,
    project_root: Path,
    root: Path,
    environment: dict[str, str],
    offline: bool = False,
    safe_stage: str | None = None,
) -> tuple[Path, Path]:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the Windows release gate"
    dist = root / "dist"
    command = [uv, "build"]
    if offline:
        command.append("--offline")
    command.extend(["--out-dir", str(dist)])
    options: dict[str, object] = {
        "cwd": project_root,
        "environment": environment,
        "timeout": 300,
    }
    if safe_stage is not None:
        options["safe_stage"] = safe_stage
    run_checked(command, **options)  # type: ignore[arg-type]
    wheels = tuple(dist.glob("*.whl"))
    sdists = tuple(dist.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    return wheels[0], sdists[0]


def normalized_archive_names(names: list[str]) -> tuple[str, ...]:
    return tuple(name.replace("\\", "/") for name in names)


def inspect_release_archives(wheel: Path, sdist: Path) -> frozenset[str]:
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = normalized_archive_names(archive.namelist())
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = normalized_archive_names(archive.getnames())

    for resource in REQUIRED_INSTALLED_RESOURCES:
        wheel_suffix = f"mechanical_design_agent/resources/{resource}"
        sdist_suffix = f"/src/mechanical_design_agent/resources/{resource}"
        assert any(name.endswith(wheel_suffix) for name in wheel_names), resource
        assert any(name.endswith(sdist_suffix) for name in sdist_names), resource
    return REQUIRED_INSTALLED_RESOURCES


def create_installed_wheel_environment(
    *,
    wheel: Path,
    root: Path,
    outside: Path,
    environment: dict[str, str],
    offline: bool = False,
    safe_venv_stage: str | None = None,
    safe_install_stage: str | None = None,
) -> InstalledWheelEnvironment:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the Windows release gate"
    venv = root / "installed wheel venv"
    venv_options: dict[str, object] = {
        "cwd": outside,
        "environment": environment,
        "timeout": 300,
    }
    if safe_venv_stage is not None:
        venv_options["safe_stage"] = safe_venv_stage
    run_checked(
        [uv, "venv", "--python", sys.executable, str(venv)],
        **venv_options,  # type: ignore[arg-type]
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install_command = [uv, "pip", "install"]
    if offline:
        install_command.append("--offline")
    install_command.extend(["--python", str(python), str(wheel)])
    install_options: dict[str, object] = {
        "cwd": outside,
        "environment": environment,
        "timeout": 900,
    }
    if safe_install_stage is not None:
        install_options["safe_stage"] = safe_install_stage
    run_checked(install_command, **install_options)  # type: ignore[arg-type]
    cli = venv / (
        "Scripts/mechanical-design.exe" if os.name == "nt" else "bin/mechanical-design"
    )
    mcp = venv / (
        "Scripts/mechanical-design-mcp.exe"
        if os.name == "nt"
        else "bin/mechanical-design-mcp"
    )
    assert python.is_file()
    assert cli.is_file()
    assert mcp.is_file()
    return InstalledWheelEnvironment(venv=venv, python=python, cli=cli, mcp=mcp)


def _locked_package_version(project_root: Path, package_name: str) -> str:
    with (project_root / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    versions = {
        str(package["version"])
        for package in lock.get("package", ())
        if package.get("name") == package_name and package.get("version")
    }
    assert len(versions) == 1, f"uv.lock must contain one {package_name} version"
    return versions.pop()


def install_windows_live_test_dependencies(
    *,
    python: Path,
    cwd: Path,
    environment: dict[str, str],
    offline: bool = False,
    safe_stage: str | None = None,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the Windows database live gate"
    command = [uv, "pip", "install"]
    if offline:
        command.append("--offline")
    command.extend(
        [
            "--python",
            str(python),
            "pytest>=8.3.0,<10",
            "jsonschema>=4.23.0,<5",
        ]
    )
    options: dict[str, object] = {
        "cwd": cwd,
        "environment": environment,
        "timeout": 900,
    }
    if safe_stage is not None:
        options["safe_stage"] = safe_stage
    run_checked(command, **options)  # type: ignore[arg-type]


def prepare_and_verify_offline_project_wheel_cache(
    *,
    project_root: Path,
    root: Path,
    outside: Path,
    environment: dict[str, str],
    safe_context: Literal["W4"] | None = None,
) -> InstalledWheelEnvironment:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the Windows release gate"
    cache_value = environment.get("UV_CACHE_DIR", "").strip()
    assert cache_value, "W3 cache preparation requires an explicit UV_CACHE_DIR"
    prepared_cache = Path(cache_value).resolve(strict=True)
    assert prepared_cache.is_dir(), "W3 prepared UV cache must be a directory"
    prepared_environment = dict(environment)
    prepared_environment["UV_CACHE_DIR"] = str(prepared_cache)
    stages = (
        {
            "online_build": "W4_CACHE_ONLINE_BUILD",
            "lock_export": "W4_CACHE_LOCK_EXPORT",
            "warmup_venv": "W4_CACHE_WARMUP_VENV",
            "warmup_project": "W4_CACHE_WARMUP_PROJECT_INSTALL",
            "warmup_test": "W4_CACHE_WARMUP_TEST_INSTALL",
            "offline_build": "W4_CACHE_OFFLINE_BUILD",
            "offline_venv": "W4_CACHE_OFFLINE_VENV",
            "offline_project": "W4_CACHE_OFFLINE_PROJECT_INSTALL",
            "offline_test": "W4_CACHE_OFFLINE_TEST_INSTALL",
        }
        if safe_context == "W4"
        else {}
    )

    online_build_root = root / "online build"
    online_build_root.mkdir()
    wheel, _sdist = build_release_artifacts(
        project_root=project_root,
        root=online_build_root,
        environment=prepared_environment,
        safe_stage=stages.get("online_build"),
    )
    constraints = root / "locked-runtime-requirements.txt"
    export_options: dict[str, object] = {
        "cwd": project_root,
        "environment": prepared_environment,
        "timeout": 300,
    }
    if stages.get("lock_export") is not None:
        export_options["safe_stage"] = stages["lock_export"]
    run_checked(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            "--output-file",
            str(constraints),
        ],
        **export_options,  # type: ignore[arg-type]
    )
    assert constraints.is_file(), "locked runtime constraints were not exported"

    warmup_venv = root / "cache warmup venv"
    warmup_venv_options: dict[str, object] = {
        "cwd": outside,
        "environment": prepared_environment,
        "timeout": 300,
    }
    if stages.get("warmup_venv") is not None:
        warmup_venv_options["safe_stage"] = stages["warmup_venv"]
    run_checked(
        [uv, "venv", "--python", sys.executable, str(warmup_venv)],
        **warmup_venv_options,  # type: ignore[arg-type]
    )
    warmup_python = warmup_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    warmup_project_options: dict[str, object] = {
        "cwd": outside,
        "environment": prepared_environment,
        "timeout": 900,
    }
    if stages.get("warmup_project") is not None:
        warmup_project_options["safe_stage"] = stages["warmup_project"]
    run_checked(
        [
            uv,
            "pip",
            "install",
            "--constraint",
            str(constraints),
            "--python",
            str(warmup_python),
            str(wheel),
        ],
        **warmup_project_options,  # type: ignore[arg-type]
    )

    locked_pytest_version = _locked_package_version(project_root, "pytest")
    locked_jsonschema_version = _locked_package_version(project_root, "jsonschema")
    warmup_test_options: dict[str, object] = {
        "cwd": outside,
        "environment": prepared_environment,
        "timeout": 900,
    }
    if stages.get("warmup_test") is not None:
        warmup_test_options["safe_stage"] = stages["warmup_test"]
    run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(warmup_python),
            f"pytest=={locked_pytest_version}",
            f"jsonschema=={locked_jsonschema_version}",
        ],
        **warmup_test_options,  # type: ignore[arg-type]
    )

    offline_build_root = root / "offline build"
    offline_build_root.mkdir()
    offline_wheel, _offline_sdist = build_release_artifacts(
        project_root=project_root,
        root=offline_build_root,
        environment=prepared_environment,
        offline=True,
        safe_stage=stages.get("offline_build"),
    )

    proof_root = root / "offline proof"
    proof_outside = outside / "offline proof"
    proof_root.mkdir()
    proof_outside.mkdir()
    installed = create_installed_wheel_environment(
        wheel=offline_wheel,
        root=proof_root,
        outside=proof_outside,
        environment=prepared_environment,
        offline=True,
        safe_venv_stage=stages.get("offline_venv"),
        safe_install_stage=stages.get("offline_project"),
    )
    install_windows_live_test_dependencies(
        python=installed.python,
        cwd=proof_outside,
        environment=prepared_environment,
        offline=True,
        safe_stage=stages.get("offline_test"),
    )
    locked_mcp_version = _locked_package_version(project_root, "mcp")
    run_checked(
        [
            str(installed.python),
            "-c",
            "import importlib.metadata as m,sys; "
            "assert m.version('mcp') == sys.argv[1]",
            locked_mcp_version,
        ],
        cwd=proof_outside,
        environment=prepared_environment,
    )
    run_checked(
        [
            str(installed.python),
            "-c",
            "import importlib.metadata as m,sys; "
            "assert m.version('pytest') == sys.argv[1]; "
            "assert m.version('jsonschema') == sys.argv[2]",
            locked_pytest_version,
            locked_jsonschema_version,
        ],
        cwd=proof_outside,
        environment=prepared_environment,
    )
    return installed


def inspect_installed_resources(
    *,
    python: Path,
    venv: Path,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    script = """
import json
from pathlib import Path
import mechanical_design_agent
from mechanical_design_agent.migrations import neo4j_migrations_directory, postgres_migrations_directory
from mechanical_design_agent.package_resources import freecad_scripts_directory, standard_part_provider_config, validation_resources_directory

venv = Path(__import__('sys').argv[1]).resolve()
resources = []
paths = [Path(mechanical_design_agent.__file__).resolve()]
with postgres_migrations_directory() as root:
    root = root.resolve(); paths.append(root)
    resources.extend('migrations/postgres/' + item.name for item in sorted(root.glob('*.sql')))
with neo4j_migrations_directory() as root:
    root = root.resolve(); paths.append(root)
    resources.extend('migrations/neo4j/' + item.name for item in sorted(root.glob('*.cypher')))
with validation_resources_directory() as root:
    root = root.resolve(); paths.append(root)
    resources.extend('validation/' + item.name for item in sorted(root.glob('*.json')))
with freecad_scripts_directory() as root:
    root = root.resolve(); paths.append(root)
    resources.extend('freecad/' + item.name for item in sorted(root.glob('*.py')))
with standard_part_provider_config() as item:
    item = item.resolve(); paths.append(item)
    resources.append('config/' + item.name)
print(json.dumps({
    'all_inside_venv': all(path.is_relative_to(venv) for path in paths),
    'resources': sorted(resources),
    'version': mechanical_design_agent.__version__,
}))
"""
    result = run_checked(
        [str(python), "-c", script, str(venv)],
        cwd=cwd,
        environment=environment,
    )
    return json.loads(result.stdout)
