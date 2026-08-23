from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from .migrations import (
    discover_neo4j_migrations,
    discover_postgres_migrations,
    neo4j_constraint_names,
    neo4j_migrations_directory,
    postgres_migrations_directory,
)
from .projection import Neo4jProjection
from .repository import PostgresRepository


SCHEMA_VERSION = "MechanicalDesignDatabaseBootstrap/v1"
REQUIRED_POSTGRES_EXTENSIONS = {"vector", "pg_trgm", "pgcrypto"}
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
)
EXPECTED_NEO4J_MIGRATIONS = (
    "001_constraints.cypher",
    "002_design_lessons.cypher",
    "003_projection_state.cypher",
)


def _blocked_component(code: str, exc: Exception) -> dict[str, object]:
    return {
        "status": "blocked",
        "code": code,
        "error_type": type(exc).__name__,
    }


def _not_run_component(code: str) -> dict[str, object]:
    return {"status": "blocked", "code": code}


def _require_exact_resources(
    paths: list[Path],
    expected_names: tuple[str, ...],
    resource_type: str,
) -> None:
    actual_names = tuple(path.name for path in paths)
    if actual_names != expected_names:
        raise ValueError(f"{resource_type} migration resources are incomplete")


def _expected_postgres_ledger(paths: list[Path]) -> list[dict[str, object]]:
    ledger: list[dict[str, object]] = []
    for path in paths:
        contents = path.read_bytes()
        try:
            contents.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"PostgreSQL migration is not UTF-8: {path.name}"
            ) from exc
        ledger.append(
            {
                "version": int(path.name.split("_", 1)[0]),
                "filename": path.name,
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        )
    return ledger


def _postgres_result(
    repository: PostgresRepository,
    migration_result: dict[str, list[str]],
    expected_ledger: list[dict[str, object]],
) -> dict[str, object]:
    state = repository.migration_state()
    raw_ledger = state.get("ledger")
    if not isinstance(raw_ledger, list) or any(
        not isinstance(item, dict) for item in raw_ledger
    ):
        raise ValueError("PostgreSQL migration ledger result is invalid")
    ledger = [dict(item) for item in raw_ledger]
    if ledger != expected_ledger:
        raise ValueError("PostgreSQL migration ledger does not match package resources")
    raw_extensions = state.get("extensions")
    if not isinstance(raw_extensions, list):
        raise ValueError("PostgreSQL extension result is invalid")
    extensions = {str(item) for item in raw_extensions}
    if not REQUIRED_POSTGRES_EXTENSIONS <= extensions:
        raise ValueError("PostgreSQL required extensions are not installed")
    return {
        "status": "ok",
        "applied": list(migration_result["applied"]),
        "skipped": list(migration_result["skipped"]),
        "ledger_verified": True,
        "extensions_verified": True,
    }


def bootstrap_databases(
    *,
    database_url: str,
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    repository_factory: Callable[[str], PostgresRepository] = PostgresRepository,
    projection_factory: Callable[[str, str, str], Neo4jProjection] = Neo4jProjection,
) -> dict[str, object]:
    try:
        with postgres_migrations_directory() as postgres_root:
            postgres_paths = discover_postgres_migrations(postgres_root)
            _require_exact_resources(
                postgres_paths,
                EXPECTED_POSTGRES_MIGRATIONS,
                "PostgreSQL",
            )
            expected_ledger = _expected_postgres_ledger(postgres_paths)
            with neo4j_migrations_directory() as neo4j_root:
                neo4j_paths = discover_neo4j_migrations(neo4j_root)
                _require_exact_resources(
                    neo4j_paths,
                    EXPECTED_NEO4J_MIGRATIONS,
                    "Neo4j",
                )
                expected_constraints = neo4j_constraint_names(neo4j_paths)

                repository = repository_factory(database_url)
                projection = projection_factory(
                    neo4j_uri,
                    neo4j_user,
                    neo4j_password,
                )
                postgres_status = repository.status()
                neo4j_status = projection.status()
                if postgres_status.get("status") != "healthy":
                    error = RuntimeError("PostgreSQL preflight failed")
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "status": "blocked",
                        "postgresql": _blocked_component(
                            "POSTGRESQL_UNAVAILABLE", error
                        ),
                        "neo4j": _not_run_component(
                            "NEO4J_NOT_RUN_AFTER_POSTGRESQL_PREFLIGHT"
                        ),
                    }
                if neo4j_status.get("status") != "healthy":
                    error = RuntimeError("Neo4j preflight failed")
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "status": "blocked",
                        "postgresql": _not_run_component(
                            "POSTGRESQL_NOT_RUN_AFTER_NEO4J_PREFLIGHT"
                        ),
                        "neo4j": _blocked_component("NEO4J_UNAVAILABLE", error),
                    }

                try:
                    migration_result = repository.apply_migrations(postgres_root)
                    postgres_result = _postgres_result(
                        repository,
                        migration_result,
                        expected_ledger,
                    )
                except Exception as exc:
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "status": "blocked",
                        "postgresql": _blocked_component(
                            "POSTGRESQL_MIGRATION_FAILED", exc
                        ),
                        "neo4j": _not_run_component(
                            "NEO4J_NOT_RUN_AFTER_POSTGRESQL_FAILURE"
                        ),
                    }

                try:
                    projection.initialize_constraints()
                    if projection.constraint_names() != expected_constraints:
                        raise ValueError(
                            "Neo4j constraint state does not match package resources"
                        )
                except Exception as exc:
                    return {
                        "schema_version": SCHEMA_VERSION,
                        "status": "blocked",
                        "postgresql": postgres_result,
                        "neo4j": _blocked_component(
                            "NEO4J_MIGRATION_FAILED", exc
                        ),
                    }
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "postgresql": _blocked_component(
                "DATABASE_MIGRATION_RESOURCES_INVALID", exc
            ),
            "neo4j": _blocked_component(
                "DATABASE_MIGRATION_RESOURCES_INVALID", exc
            ),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "postgresql": postgres_result,
        "neo4j": {
            "status": "ok",
            "migration_resources_verified": True,
            "constraints_verified": True,
        },
    }
