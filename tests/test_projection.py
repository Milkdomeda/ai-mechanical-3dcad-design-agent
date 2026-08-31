from __future__ import annotations

import pytest

from mechanical_design_agent.projection import Neo4jProjection, ProjectionUnavailableError


class _Result:
    def consume(self):
        return self


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        return _Result()

    def execute_write(self, callback):
        return callback(self)


class _Driver:
    def __init__(self, session: _Session) -> None:
        self.value = session

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def session(self) -> _Session:
        return self.value


class _Repository:
    def projection_records(self):
        return {
            "product_family": [
                {"id": f"family-{index}", "status": "active"}
                for index in range(2)
            ],
            "assertion": [
                {"id": f"assertion-{index}", "status": "active"}
                for index in range(43)
            ],
            "design_lesson": [
                {"id": f"lesson-{index}", "status": "active"}
                for index in range(4)
            ],
        }


def _projection(session: _Session) -> Neo4jProjection:
    projection = Neo4jProjection("bolt://localhost", "neo4j", "secret")
    projection.initialize_constraints = lambda: None  # type: ignore[method-assign]
    projection._driver = lambda: _Driver(session)  # type: ignore[method-assign]
    return projection


def test_projection_has_no_incremental_sync_path() -> None:
    assert not hasattr(Neo4jProjection, "sync")


def test_rebuild_replaces_only_owned_knowledge_nodes() -> None:
    session = _Session()

    result = _projection(session).rebuild(_Repository())

    assert result["authoritative_source"] == "postgresql"
    assert result["counts"] == {
        "product_family": 2,
        "assertion": 43,
        "design_lesson": 4,
    }
    assert "projection_owner" in session.calls[0][0]
    assert any("MERGE (n:ProductFamily" in query for query, _ in session.calls)


def test_projection_status_redacts_unavailable_driver_details() -> None:
    projection = Neo4jProjection("bolt://private-host", "neo4j", "secret")
    projection._driver = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ModuleNotFoundError("private neo4j module path")
    )

    result = projection.status()

    assert result == {
        "status": "unavailable",
        "warning": "Neo4j unavailable (ModuleNotFoundError)",
    }
    assert "private" not in result["warning"]


def test_explicit_rebuild_raises_bounded_unavailable_error() -> None:
    projection = Neo4jProjection("bolt://private-host", "neo4j", "secret")
    projection._driver = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("private connection details")
    )

    with pytest.raises(ProjectionUnavailableError) as captured:
        projection.rebuild(_Repository())

    assert captured.value.code == "NEO4J_PROJECTION_UNAVAILABLE"
    assert "private" not in str(captured.value)
