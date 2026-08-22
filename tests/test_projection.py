from __future__ import annotations

import unittest

from mechanical_design_agent.projection import Neo4jProjection


class FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def session(self):
        return FakeContext()

    def execute_write(self, work):
        return work(self)

    def run(self, *_args, **_kwargs):
        return self

    def consume(self):
        return self


class FakeOutboxRepository:
    def __init__(self) -> None:
        self.marks: list[tuple[str, str]] = []

    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int):
        return [{"id": "event-1", "event_type": "unknown.event", "payload": {}}]

    def mark_outbox(self, event_id: str, *, worker_id: str, error: str = "") -> None:
        self.marks.append((event_id, error))


class LostLeaseRepository:
    def __init__(self) -> None:
        self.marked: list[str] = []

    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int):
        return [
            {
                "id": "lost-lease",
                "aggregate_type": "design_working_copy",
                "aggregate_id": "working-lost-lease",
                "event_type": "design_working_copy.approved",
                "payload": {},
            },
            {
                "id": "next-event",
                "aggregate_type": "design_working_copy",
                "aggregate_id": "working-next-event",
                "event_type": "design_working_copy.approved",
                "payload": {},
            },
        ]

    def mark_outbox(self, event_id: str, *, worker_id: str, error: str = "") -> None:
        if event_id == "lost-lease":
            raise RuntimeError("outbox event is not leased by this worker")
        self.marked.append(event_id)


class ProjectionEventDispatchTests(unittest.TestCase):
    def test_known_non_graph_delivery_event_is_intentionally_acknowledged(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)

        projection._project_event(
            object(),
            object(),
            {"event_type": "design_working_copy.approved", "payload": {"working_copy_id": "working-1"}},
        )

    def test_unknown_outbox_event_is_not_silently_accepted(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)

        with self.assertRaisesRegex(ValueError, "unsupported outbox event type"):
            projection._project_event(
                object(),
                object(),
                {"event_type": "design_lesson.unknown", "payload": {}},
            )

    def test_unknown_outbox_event_remains_pending_with_an_error(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: FakeContext()
        repository = FakeOutboxRepository()

        result = projection.project_pending(repository)

        self.assertEqual(result["processed"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(repository.marks[0][0], "event-1")
        self.assertIn("unsupported outbox event type", repository.marks[0][1])

    def test_lost_lease_acknowledgement_does_not_abort_later_events(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: FakeContext()
        repository = LostLeaseRepository()

        result = projection.project_pending(repository)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(repository.marked, ["next-event"])
        self.assertEqual(result["failed"][0]["event_id"], "lost-lease")
        self.assertIn("not leased", result["failed"][0]["error"])


if __name__ == "__main__":
    unittest.main()
