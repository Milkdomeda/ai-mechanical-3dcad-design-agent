import json
import os
from contextlib import contextmanager
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest
import uuid

from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.projection import Neo4jProjection
from mechanical_design_agent.repository import PostgresRepository


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query: str, parameters: tuple):
        self.calls.append((query, parameters))
        if "COALESCE(max(aggregate_version),0)+1" in query:
            return QueryRows([{"aggregate_version": 1}])
        return QueryRows()


class ClaimConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query: str, parameters: tuple):
        self.calls.append((query, parameters))
        return QueryRows([
            {
                "id": "event-1",
                "projection_occurred_at": "2026-08-18T16:01:00.000000Z",
            }
        ])


class QueryRows:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ResultStub:
    def consume(self):
        return self

    def single(self):
        return {"applied": True}


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **parameters):
        self.calls.append((query, parameters))
        return ResultStub()


class AuthoritativeLessonRepository:
    def __init__(self, lesson: dict) -> None:
        self.lesson = lesson
        self.reads = 0

    def projection_design_lessons(self) -> list[dict]:
        self.reads += 1
        return [self.lesson]


def approved_lesson() -> dict:
    return {
        "id": "lesson-42",
        "lesson_key": "DL-042",
        "title": "Preserve clearance",
        "status": "approved",
        "organization_id": "organization-1",
        "package_sha256": "a" * 64,
        "assertions": [],
    }


class DesignLessonOutboxTests(unittest.TestCase):
    def test_claimed_event_includes_projection_safe_utc_string(self) -> None:
        connection = ClaimConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        events = repository.claim_outbox(worker_id="projection-worker")

        self.assertEqual(
            events[0]["projection_occurred_at"],
            "2026-08-18T16:01:00.000000Z",
        )
        query, _parameters = connection.calls[0]
        self.assertIn("AT TIME ZONE 'UTC'", query)
        self.assertIn("AS projection_occurred_at", query)

    def test_review_approved_event_is_consumed_as_audit_projection_without_lesson_reload(self) -> None:
        session = RecordingSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)

        projection._project_event(
            session,
            object(),
            {
                "event_type": "design_lesson_review.approved",
                "aggregate_version": 1,
                "projection_occurred_at": "2026-08-18T16:01:00.000000Z",
                "payload": {
                    "review_id": "review-42",
                    "status": "approved-retrieval-pending",
                    "working_copy_id": "working-copy-42",
                    "lesson_id": "lesson-42",
                },
            },
        )

        self.assertEqual(len(session.calls), 1)
        query, parameters = session.calls[0]
        self.assertIn("DesignLessonReview", query)
        self.assertEqual(parameters["review_id"], "review-42")

    def test_lifecycle_event_targets_the_authoritative_lesson_event(self) -> None:
        connection = RecordingConnection()

        PostgresRepository._enqueue_design_lesson_event(
            connection,
            event_type="design_lesson.superseded",
            lesson_id="lesson-42",
        )

        self.assertEqual(len(connection.calls), 3)
        self.assertIn("pg_advisory_xact_lock", connection.calls[0][0])
        self.assertIn("max(aggregate_version)", connection.calls[1][0])
        query, parameters = connection.calls[2]
        self.assertIn("INSERT INTO outbox_events", query)
        self.assertEqual(parameters[:3], ("design_lesson", "lesson-42", "design_lesson.superseded"))
        self.assertEqual(json.loads(parameters[3]), {"lesson_id": "lesson-42"})
        self.assertEqual(parameters[4], 1)

    def test_produced_lesson_payload_is_consumed_by_authoritative_projection_read(self) -> None:
        connection = RecordingConnection()
        PostgresRepository._enqueue_design_lesson_event(
            connection,
            event_type="design_lesson.approved",
            lesson_id="lesson-42",
        )
        _query, parameters = connection.calls[-1]
        session = RecordingSession()
        repository = AuthoritativeLessonRepository(approved_lesson())
        projection = Neo4jProjection.__new__(Neo4jProjection)

        projection._project_event(
            session,
            repository,
            {
                "event_type": parameters[2],
                "payload": json.loads(parameters[3]),
                "aggregate_version": parameters[4],
            },
        )

        self.assertEqual(repository.reads, 1)
        self.assertEqual(session.calls[0][1]["id"], "lesson-42")


DATABASE_URL = os.environ.get("MECH_DESIGN_DATABASE_URL", "").strip()


@unittest.skipUnless(DATABASE_URL, "MECH_DESIGN_DATABASE_URL is not configured; live outbox lease test skipped")
class LiveOutboxLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DATABASE_URL)
        with postgres_migrations_directory() as migrations:
            cls.repository.apply_migrations(migrations)

    def setUp(self) -> None:
        self.aggregate_type = f"test-outbox-{uuid.uuid4().hex}"
        self.aggregate_id = uuid.uuid4().hex
        with self.repository.connection() as connection, connection.transaction():
            self.event_id = str(connection.execute(
                "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload,aggregate_version) "
                "VALUES (%s,%s,'test.event','{}'::jsonb,1) RETURNING id",
                (self.aggregate_type, self.aggregate_id),
            ).fetchone()["id"])

    def tearDown(self) -> None:
        with self.repository.connection() as connection, connection.transaction():
            connection.execute("DELETE FROM outbox_events WHERE aggregate_type=%s", (self.aggregate_type,))

    def test_two_workers_claim_once_and_expired_lease_is_retryable(self) -> None:
        barrier = Barrier(2)

        def claim(worker_id: str):
            barrier.wait(timeout=5)
            return self.repository.claim_outbox(
                worker_id=worker_id,
                limit=1,
                lease_seconds=60,
                aggregate_type=self.aggregate_type,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b")))

        claimed = [event for batch in results for event in batch]
        self.assertEqual([str(event["id"]) for event in claimed], [self.event_id])
        first_worker = claimed[0]["claimed_by"]
        blocked_worker = "worker-b" if first_worker == "worker-a" else "worker-a"
        self.assertEqual(
            self.repository.claim_outbox(
                worker_id=blocked_worker,
                limit=1,
                lease_seconds=60,
                aggregate_type=self.aggregate_type,
            ),
            [],
        )
        with self.repository.connection() as connection, connection.transaction():
            connection.execute(
                "UPDATE outbox_events SET claimed_at=now()-interval '2 minutes' WHERE id=%s",
                (self.event_id,),
            )
        retried = self.repository.claim_outbox(
            worker_id=blocked_worker,
            limit=1,
            lease_seconds=60,
            aggregate_type=self.aggregate_type,
        )
        self.assertEqual([str(event["id"]) for event in retried], [self.event_id])
        self.repository.mark_outbox(self.event_id, worker_id=blocked_worker)


if __name__ == "__main__":
    unittest.main()
