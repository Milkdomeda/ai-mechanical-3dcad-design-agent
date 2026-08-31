from __future__ import annotations

import os
from pathlib import Path

import pytest

from mechanical_design_agent.config import KnowledgeSettings
from mechanical_design_agent.database_bootstrap import bootstrap_knowledge_database
from mechanical_design_agent.migrations import (
    neo4j_migrations_directory,
    postgres_migrations_directory,
)


EXPECTED_POSTGRES_MIGRATIONS = (
    "001_knowledge_core.sql",
    "002_knowledge_search.sql",
    "003_knowledge_projection.sql",
)
EXPECTED_NEO4J_MIGRATIONS = (
    "001_constraints.cypher",
    "002_design_lessons.cypher",
    "003_projection_state.cypher",
)


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
