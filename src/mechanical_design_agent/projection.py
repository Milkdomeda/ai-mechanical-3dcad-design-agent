from __future__ import annotations

from typing import Any, Mapping

from .migrations import neo4j_migrations_directory


_OWNER = "ai-mechanical-design-agent"
_LABELS = {
    "product_family": "ProductFamily",
    "assertion": "KnowledgeAssertion",
    "design_lesson": "DesignLesson",
}


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

    def sync(self, repository: object, limit: int = 100) -> dict[str, object]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        self.initialize_constraints()
        events = repository.pending_projection_events(limit=limit)
        processed = 0
        failures: list[dict[str, str]] = []
        with self._driver() as driver:
            for event in events:
                try:
                    aggregate_type = str(event["aggregate_type"])
                    aggregate_id = str(event["aggregate_id"])
                    label = _LABELS.get(aggregate_type)
                    if label is None:
                        raise ValueError(
                            f"unsupported knowledge aggregate: {aggregate_type}"
                        )
                    record = repository.projection_record(
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                    )
                    with driver.session() as session:
                        session.execute_write(
                            lambda transaction: self._upsert(
                                transaction,
                                label=label,
                                identifier=aggregate_id,
                                record=record,
                            )
                        )
                    repository.mark_projection_event(int(event["id"]))
                    processed += 1
                except Exception as exc:
                    failures.append(
                        {
                            "event_id": str(event.get("id", "unknown")),
                            "warning": f"projection failed ({type(exc).__name__})",
                        }
                    )
        return {
            "status": "completed" if not failures else "needs_retry",
            "processed": processed,
            "failed": failures,
        }

    def rebuild(self, repository: object) -> dict[str, object]:
        self.initialize_constraints()
        records = repository.projection_records()
        counts = {name: 0 for name in _LABELS}
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


__all__ = ["Neo4jProjection"]
