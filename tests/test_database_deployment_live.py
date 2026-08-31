from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import pytest
import psycopg

from mechanical_design_agent.config import KnowledgeSettings
from mechanical_design_agent.database_bootstrap import bootstrap_knowledge_database
from mechanical_design_agent.long_term_knowledge_target import (
    KnowledgeMigrationError,
    SimplifiedKnowledgePayload,
    create_target_database,
    import_simplified_payload,
    validate_simplified_target,
)
from mechanical_design_agent.knowledge_repository import (
    KnowledgeRepository,
    KnowledgeScope,
)
from mechanical_design_agent.migrations import (
    neo4j_migrations_directory,
    postgres_migrations_directory,
)


EXPECTED_POSTGRES_MIGRATIONS = (
    "001_knowledge.sql",
)
EXPECTED_NEO4J_MIGRATIONS = (
    "001_knowledge_projection.cypher",
)
EXPECTED_POSTGRES_TABLES = {
    "knowledge_schema_migrations",
    "product_families",
    "knowledge_assertions",
    "design_lessons",
}
EXPECTED_POSTGRES_INDEXES = {
    "product_families_scope_idx",
    "product_families_text_idx",
    "knowledge_assertions_scope_idx",
    "knowledge_assertions_text_idx",
    "design_lessons_scope_idx",
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


def _live_payload() -> SimplifiedKnowledgePayload:
    return SimplifiedKnowledgePayload(
        source_export_sha256="a" * 64,
        product_families=(
            {
                "id": "live-family",
                "organization_id": "live-test-org",
                "design_group_id": "live-test-group",
                "canonical_name": "Live Family",
                "aliases": ["live alias"],
                "profile": {"mechanism": "test fixture"},
                "search_terms": ["live alias", "live family"],
                "search_text": "Live Family live alias test fixture",
                "status": "active",
            },
        ),
        knowledge_assertions=(
            {
                "id": "live-assertion",
                "organization_id": "live-test-org",
                "design_group_id": "live-test-group",
                "product_family_id": "live-family",
                "subject": "live subject",
                "predicate": "uses",
                "object_value": "live object",
                "applicability": {},
                "evidence": [],
                "search_terms": ["live assertion"],
                "search_text": "live subject uses live object live assertion",
                "status": "active",
                "supersedes_id": None,
            },
        ),
        design_lessons=(
            {
                "id": "live-lesson",
                "organization_id": "live-test-org",
                "design_group_id": "live-test-group",
                "product_family_id": "live-family",
                "content": {"title": "Live lesson", "problem": "fixture"},
                "applicability": {},
                "provenance": {"source_review_sha256": "b" * 64},
                "search_terms": ["live lesson"],
                "search_text": "Live lesson fixture",
                "status": "active",
                "supersedes_id": None,
            },
        ),
    )


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_DOCKER_DATABASE_LIVE_CHILD") != "1",
    reason="live knowledge import requires an explicitly isolated child environment",
)
@pytest.mark.live_database
def test_simplified_import_is_idempotent_and_rejects_conflict() -> None:
    database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
    payload = _live_payload()

    first = import_simplified_payload(database_url, payload)
    second = import_simplified_payload(database_url, payload)
    validation = validate_simplified_target(database_url, payload)

    assert first.status == "imported"
    assert second.status == "already_imported"
    assert validation["status"] == "passed"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE product_families SET canonical_name='conflict' "
            "WHERE id='live-family'"
        )
    with pytest.raises(KnowledgeMigrationError, match="TARGET_CONTENT_MISMATCH"):
        import_simplified_payload(database_url, payload)


@pytest.mark.skipif(
    os.environ.get("MECH_DESIGN_DOCKER_DATABASE_LIVE_CHILD") != "1",
    reason="live long-term import requires an explicitly isolated child environment",
)
@pytest.mark.live_database
def test_simplified_import_preserves_very_long_exact_terms() -> None:
    source_database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
    target_database_url = create_target_database(
        source_database_url,
        f"long_term_{uuid.uuid4().hex}",
    )
    original = _live_payload()
    long_term = "".join(
        hashlib.sha256(str(index).encode("ascii")).hexdigest()
        for index in range(100)
    )
    assertion = dict(original.knowledge_assertions[0])
    assertion["search_terms"] = [long_term]
    payload = SimplifiedKnowledgePayload(
        source_export_sha256=original.source_export_sha256,
        product_families=original.product_families,
        knowledge_assertions=(assertion,),
        design_lessons=original.design_lessons,
    )

    imported = import_simplified_payload(target_database_url, payload)
    result = KnowledgeRepository(
        target_database_url,
        KnowledgeScope("live-test-org", "live-test-group"),
    ).search(query=long_term, product_family_id="live-family")

    assert imported.status == "imported"
    assert [row["id"] for row in result["assertions"]] == ["live-assertion"]
