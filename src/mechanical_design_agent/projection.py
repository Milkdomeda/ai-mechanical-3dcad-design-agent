from __future__ import annotations

from typing import Any, Mapping

from .migrations import neo4j_migrations_directory


_OWNER = "ai-mechanical-design-agent"
_LABELS = {
    "product_family": "ProductFamily",
    "assertion": "KnowledgeAssertion",
    "design_lesson": "DesignLesson",
}


class ProjectionUnavailableError(RuntimeError):
    def __init__(self, cause_type: str) -> None:
        self.code = "NEO4J_PROJECTION_UNAVAILABLE"
        super().__init__(f"Neo4j projection unavailable ({cause_type})")


class Neo4jProjection:
    """Rebuildable relationship projection of PostgreSQL knowledge records."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password

    def _driver(self):
        from neo4j import GraphDatabase

        return GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def status(self) -> dict[str, object]:
        try:
            with self._driver() as driver:
                driver.verify_connectivity()
            return {"status": "healthy"}
        except Exception as exc:
            return {
                "status": "unavailable",
                "warning": f"Neo4j unavailable ({type(exc).__name__})",
            }

    def initialize_constraints(self) -> None:
        with neo4j_migrations_directory() as migrations:
            statements = [
                statement.strip()
                for path in sorted(migrations.glob("*.cypher"))
                for statement in path.read_text(encoding="utf-8").split(";")
                if statement.strip()
            ]
        with self._driver() as driver, driver.session() as session:
            for statement in statements:
                session.run(statement).consume()

    def rebuild(self, repository: object) -> dict[str, object]:
        records = repository.projection_records()
        counts = {name: 0 for name in _LABELS}
        try:
            self.initialize_constraints()
            with self._driver() as driver, driver.session() as session:
                def rebuild_transaction(transaction: Any) -> None:
                    transaction.run(
                        "MATCH (n) WHERE n.projection_owner=$owner DETACH DELETE n",
                        owner=_OWNER,
                    ).consume()
                    for aggregate_type, label in _LABELS.items():
                        for record in records.get(aggregate_type, []):
                            identifier = str(record["id"])
                            self._upsert(
                                transaction,
                                label=label,
                                identifier=identifier,
                                record=record,
                            )
                            counts[aggregate_type] += 1

                session.execute_write(rebuild_transaction)
        except Exception as exc:
            raise ProjectionUnavailableError(type(exc).__name__) from None
        return {
            "status": "rebuilt",
            "authoritative_source": "postgresql",
            "counts": counts,
        }

    @staticmethod
    def _upsert(
        transaction: Any,
        *,
        label: str,
        identifier: str,
        record: Mapping[str, object],
    ) -> None:
        properties = {
            key: value
            for key, value in record.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        transaction.run(
            f"MERGE (n:{label} {{id:$id}}) "
            "SET n += $properties,n.projection_owner=$owner",
            id=identifier,
            properties=properties,
            owner=_OWNER,
        ).consume()


__all__ = ["Neo4jProjection", "ProjectionUnavailableError"]
