from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from mechanical_design_agent.repository import PostgresRepository


class Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class RecordingConnection:
    def __init__(self, existing: dict[str, object] | None = None) -> None:
        self.existing = existing
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.events: list[str] = []

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.events.append("begin")
        try:
            yield
        finally:
            self.events.append("commit")

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> Result:
        self.executed.append((query, parameters))
        if query.startswith("SELECT canonical_name"):
            return Result(self.existing)
        return Result()


def bootstrap_config(*, organization_name: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "product-family-bootstrap/v1",
        "organization_id": "org-generic",
        "design_group_id": "group-generic",
        "design_group_name": "Generic design group",
        "family_id": "family-generic",
        "family_name": "Generic family",
        "aliases": ["generic-alias"],
        "status": "awaiting-source-folder",
    }
    if organization_name is not None:
        value["organization_name"] = organization_name
    return value


def configured_repository(
    connection: RecordingConnection,
) -> tuple[PostgresRepository, list[tuple[object, ...]], dict[str, object]]:
    repository = PostgresRepository("postgresql://unused")
    enqueued: list[tuple[object, ...]] = []
    returned_family = {"id": "family-generic", "source": "get_family"}
    repository.connection = lambda: connection  # type: ignore[method-assign]
    repository._enqueue = lambda *args: enqueued.append(args)  # type: ignore[method-assign]
    repository.get_family = lambda family_id: returned_family  # type: ignore[method-assign]
    return repository, enqueued, returned_family


def parameters_for(
    connection: RecordingConnection,
    query_fragment: str,
) -> list[tuple[object, ...]]:
    return [
        parameters
        for query, parameters in connection.executed
        if query_fragment in query
    ]


def test_missing_display_names_fall_back_to_authoritative_ids() -> None:
    connection = RecordingConnection()
    repository, enqueued, returned_family = configured_repository(connection)

    result = repository.initialize_bootstrap(
        bootstrap_config(),
        "actor-explicit",
    )

    assert parameters_for(connection, "INSERT INTO organizations") == [
        ("org-generic", "org-generic")
    ]
    assert parameters_for(connection, "INSERT INTO actors") == [
        ("actor-explicit", "org-generic", "actor-explicit", "family_owner")
    ]
    assert connection.events == ["begin", "commit"]
    assert len(enqueued) == 1
    assert result is returned_family


def test_explicit_organization_name_is_preserved() -> None:
    connection = RecordingConnection()
    repository, _enqueued, _returned_family = configured_repository(connection)

    repository.initialize_bootstrap(
        bootstrap_config(organization_name="Named organization"),
        "actor-explicit",
    )

    assert parameters_for(connection, "INSERT INTO organizations") == [
        ("org-generic", "Named organization")
    ]


def test_existing_family_bootstrap_remains_idempotent() -> None:
    config = bootstrap_config()
    existing = {
        "canonical_name": config["family_name"],
        "aliases": config["aliases"],
        "status": config["status"],
        "config": config,
    }
    connection = RecordingConnection(existing)
    repository, enqueued, returned_family = configured_repository(connection)

    first = repository.initialize_bootstrap(config, "actor-explicit")
    second = repository.initialize_bootstrap(config, "actor-explicit")

    assert connection.events == ["begin", "commit", "begin", "commit"]
    assert parameters_for(connection, "INSERT INTO organizations") == [
        ("org-generic", "org-generic"),
        ("org-generic", "org-generic"),
    ]
    assert parameters_for(connection, "INSERT INTO actors") == [
        ("actor-explicit", "org-generic", "actor-explicit", "family_owner"),
        ("actor-explicit", "org-generic", "actor-explicit", "family_owner"),
    ]
    assert not parameters_for(connection, "INSERT INTO product_families")
    assert not parameters_for(connection, "UPDATE product_families")
    assert enqueued == []
    assert first is returned_family
    assert second is returned_family
