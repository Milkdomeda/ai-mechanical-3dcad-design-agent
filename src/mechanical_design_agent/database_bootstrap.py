from __future__ import annotations

from .config import KnowledgeSettings
from .knowledge_repository import KnowledgeRepository, KnowledgeScope
from .migrations import postgres_migrations_directory
from .projection import Neo4jProjection


def bootstrap_knowledge_database(settings: KnowledgeSettings) -> dict[str, object]:
    """Initialize the knowledge-only PostgreSQL schema and Neo4j constraints."""
    repository = KnowledgeRepository(
        settings.database_url,
        KnowledgeScope(settings.organization_id, settings.design_group_id),
    )
    with postgres_migrations_directory() as migrations:
        postgres = repository.apply_migrations(migrations)
    projection = Neo4jProjection(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    projection.initialize_constraints()
    return {
        "schema_version": "KnowledgeDatabaseBootstrap/v1",
        "status": "ready",
        "postgresql": postgres,
        "neo4j": {"constraints": "initialized"},
    }


__all__ = ["bootstrap_knowledge_database"]
