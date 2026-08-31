from __future__ import annotations

from mechanical_design_agent.projection import Neo4jProjection


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
    def __init__(self) -> None:
        self.marked: list[int] = []

    def pending_projection_events(self, *, limit: int):
        assert limit == 100
        return [
            {
                "id": 1,
                "aggregate_type": "design_lesson",
                "aggregate_id": "lesson-1",
                "event_type": "published",
                "payload": {},
            }
        ]

    def projection_record(self, *, aggregate_type: str, aggregate_id: str):
        assert aggregate_type == "design_lesson"
        return {"id": aggregate_id, "status": "approved"}

    def mark_projection_event(self, event_id: int) -> None:
        self.marked.append(event_id)

    def projection_records(self):
        return {
            "product_family": [{"id": "family-1", "status": "active"}],
            "assertion": [],
            "design_lesson": [{"id": "lesson-1", "status": "approved"}],
        }


def _projection(session: _Session) -> Neo4jProjection:
    projection = Neo4jProjection("bolt://localhost", "neo4j", "secret")
    projection.initialize_constraints = lambda: None  # type: ignore[method-assign]
    projection._driver = lambda: _Driver(session)  # type: ignore[method-assign]
    return projection


def test_outbox_event_is_marked_only_after_graph_write() -> None:
    session = _Session()
    repository = _Repository()

    result = _projection(session).sync(repository)

    assert result["status"] == "completed"
    assert result["processed"] == 1
    assert repository.marked == [1]
    assert "MERGE (n:DesignLesson" in session.calls[0][0]


def test_rebuild_replaces_only_owned_knowledge_nodes() -> None:
    session = _Session()

    result = _projection(session).rebuild(_Repository())

    assert result["authoritative_source"] == "postgresql"
    assert result["counts"] == {
        "product_family": 1,
        "assertion": 0,
        "design_lesson": 1,
    }
    assert "projection_owner" in session.calls[0][0]
    assert any("MERGE (n:ProductFamily" in query for query, _ in session.calls)


def test_projection_failure_keeps_event_pending_without_leaking_details() -> None:
    class FailingRepository(_Repository):
        def projection_record(self, **_kwargs):
            raise RuntimeError("private database path")

    repository = FailingRepository()
    result = _projection(_Session()).sync(repository)

    assert result["status"] == "needs_retry"
    assert repository.marked == []
    assert "private database path" not in str(result["failed"])
