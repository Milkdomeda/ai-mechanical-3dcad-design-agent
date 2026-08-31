from __future__ import annotations

import os
from pathlib import Path

import pytest
import psycopg

from mechanical_design_agent.config import KnowledgeSettings
from mechanical_design_agent.database_bootstrap import bootstrap_knowledge_database
from mechanical_design_agent.migrations import (
    neo4j_migrations_directory,
    postgres_migrations_directory,
)


EXPECTED_POSTGRES_MIGRATIONS = (
    "001_knowledge.sql",
)
EXPECTED_NEO4J_MIGRATIONS = (
    "001_constraints.cypher",
    "002_design_lessons.cypher",
    "003_projection_state.cypher",
)
EXPECTED_POSTGRES_TABLES = {
    "knowledge_schema_migrations",
    "product_families",
    "knowledge_assertions",
    "design_lessons",
}
EXPECTED_POSTGRES_INDEXES = {
    "product_families_scope_idx",
    "product_families_terms_idx",
    "product_families_text_idx",
    "knowledge_assertions_scope_idx",
    "knowledge_assertions_terms_idx",
    "knowledge_assertions_text_idx",
    "design_lessons_scope_idx",
    "design_lessons_terms_idx",
    "design_lessons_text_idx",
}


def test_installed_knowledge_migration_inventory_is_exact() -> None:
    with postgres_migrations_directory() as root:
        assert tuple(path.name for path in sorted(root.glob("*.sql"))) == (
            EXPECTED_POSTGRES_MIGRATIONS
        )
    with neo4j_migrations_directory() as root:
        assert tuple(path.name for path in sorted(root.glob("*.cypher"))) == (
            EXPECTED_NEO4J_MIGRATIONS
        )


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_DOCKER_DATABASE_LIVE_CHILD") != "1",
    reason="live knowledge bootstrap requires an explicitly isolated child environment",
)
@pytest.mark.live_database
def test_isolated_knowledge_services_bootstrap_idempotently(tmp_path: Path) -> None:
    settings = KnowledgeSettings(
        workspace=tmp_path,
        database_url=os.environ["MECH_DESIGN_DATABASE_URL"],
        neo4j_uri=os.environ["MECH_DESIGN_NEO4J_URI"],
        neo4j_user=os.environ["MECH_DESIGN_NEO4J_USER"],
        neo4j_password=os.environ["MECH_DESIGN_NEO4J_PASSWORD"],
        organization_id="live-test-org",
        design_group_id="live-test-group",
    )

    first = bootstrap_knowledge_database(settings)
    second = bootstrap_knowledge_database(settings)

    assert first["status"] == second["status"] == "ready"
    assert first["postgresql"] == {
        "applied": list(EXPECTED_POSTGRES_MIGRATIONS),
        "skipped": [],
    }
    assert second["postgresql"] == {
        "applied": [],
        "skipped": list(EXPECTED_POSTGRES_MIGRATIONS),
    }
    assert first["neo4j"] == second["neo4j"] == {
        "constraints": "initialized"
    }

    with psycopg.connect(settings.database_url) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
            ).fetchall()
        }
    assert tables == EXPECTED_POSTGRES_TABLES
    assert EXPECTED_POSTGRES_INDEXES <= indexes
