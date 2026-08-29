import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mechanical_design_agent.projection import Neo4jProjection


class ResultStub:
    def __init__(self, record: dict | None = None) -> None:
        self.record = record

    def consume(self):
        return self

    def single(self):
        return self.record


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **parameters):
        self.calls.append((query, parameters))
        if "RETURN count(n) AS node_count" in query:
            return ResultStub({"node_count": parameters["expected_count"]})
        return ResultStub({"applied": True})

    def execute_write(self, work):
        return work(self)


class VersionAwareSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__()
        self.lesson_state: dict[str, dict] = {}
        self.assertion_state: dict[str, dict] = {}
        self.review_state: dict[str, dict] = {}

    def run(self, query: str, **parameters):
        self.calls.append((query, parameters))
        if "RETURN count(n) AS node_count" in query:
            return ResultStub({"node_count": parameters["expected_count"]})
        if "MERGE (l:DesignLesson {id:$id})" in query and "status" in parameters:
            state = self.lesson_state
            identifier = str(parameters["id"])
        elif "MERGE (a:KnowledgeAssertion {id:$id})" in query and "status" in parameters:
            state = self.assertion_state
            identifier = str(parameters["id"])
        elif "MERGE (r:DesignLessonReview {review_id:$review_id})" in query:
            state = self.review_state
            identifier = str(parameters["review_id"])
        else:
            return ResultStub({"applied": True})
        current = state.get(identifier)
        incoming_version = int(parameters.get("aggregate_version", 0))
        is_version_guarded = "coalesce(" in query and ".aggregate_version,-1) < $aggregate_version" in query
        if (
            is_version_guarded
            and current
            and incoming_version <= current["aggregate_version"]
            and not parameters.get("force")
        ):
            return ResultStub(None)
        state[identifier] = {
            "status": parameters["status"],
            "aggregate_version": incoming_version,
        }
        return ResultStub({"applied": True})


class AtomicEventSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__()
        self.lesson_state: dict[str, dict] = {}
        self.assertion_state: dict[str, dict] = {}
        self.fail_assertion_once = True

    def _run_against(
        self,
        lesson_state: dict[str, dict],
        assertion_state: dict[str, dict],
        query: str,
        parameters: dict,
    ) -> ResultStub:
        if "MERGE (l:DesignLesson {id:$id})" in query and "status" in parameters:
            lesson_id = str(parameters["id"])
            incoming_version = int(parameters.get("aggregate_version", 0))
            current = lesson_state.get(lesson_id)
            if current and incoming_version <= current["aggregate_version"]:
                return ResultStub(None)
            lesson_state[lesson_id] = {
                "status": parameters["status"],
                "aggregate_version": incoming_version,
            }
            return ResultStub({"applied": True})
        if "MERGE (a:KnowledgeAssertion {id:$id})" in query and "status" in parameters:
            if self.fail_assertion_once:
                self.fail_assertion_once = False
                raise RuntimeError("injected assertion projection failure")
            assertion_state[str(parameters["id"])] = {
                "status": parameters["status"],
                "aggregate_version": int(parameters.get("aggregate_version", 0)),
            }
        return ResultStub({"applied": True})

    def run(self, query: str, **parameters):
        self.calls.append((query, parameters))
        return self._run_against(
            self.lesson_state,
            self.assertion_state,
            query,
            parameters,
        )

    def execute_write(self, work):
        working_lessons = {key: dict(value) for key, value in self.lesson_state.items()}
        working_assertions = {key: dict(value) for key, value in self.assertion_state.items()}
        transaction = RecordingSession()

        def transactional_run(query: str, **parameters):
            transaction.calls.append((query, parameters))
            return self._run_against(
                working_lessons,
                working_assertions,
                query,
                parameters,
            )

        transaction.run = transactional_run
        result = work(transaction)
        self.calls.extend(transaction.calls)
        self.lesson_state = working_lessons
        self.assertion_state = working_assertions
        return result


class AtomicGraphSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__()
        self.nodes = {"old-active-node"}
        self.active_generation = "generation-old"

    def run(self, query: str, **parameters):
        """Model the old auto-commit path so the regression fails before the fix."""
        result = self._run_against(self.nodes, query, parameters)
        if "SET state.active_generation=$generation" in query:
            self.active_generation = parameters["generation"]
        return result

    def execute_write(self, work):
        working_nodes = set(self.nodes)
        working_generation = self.active_generation
        transaction = RecordingSession()

        def transactional_run(query: str, **parameters):
            nonlocal working_generation
            transaction.calls.append((query, parameters))
            result = self._run_against(working_nodes, query, parameters, record=False)
            if "SET state.active_generation=$generation" in query:
                working_generation = parameters["generation"]
            return result

        transaction.run = transactional_run
        result = work(transaction)
        self.calls.extend(transaction.calls)
        self.nodes = working_nodes
        self.active_generation = working_generation
        return result

    def _run_against(
        self,
        nodes: set[str],
        query: str,
        parameters: dict,
        *,
        record: bool = True,
    ) -> ResultStub:
        if record:
            self.calls.append((query, parameters))
        if "DETACH DELETE n" in query:
            nodes.clear()
        if "MERGE (l:DesignLesson {id:$id})" in query and "status" in parameters:
            nodes.add(str(parameters["id"]))
        if "RETURN count(n) AS node_count" in query:
            return ResultStub({"node_count": parameters["expected_count"]})
        return ResultStub({"applied": True})


class RecordingDriver:
    def __init__(self, session: RecordingSession) -> None:
        self.recording_session = session

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def session(self):
        return SessionContext(self.recording_session)


class SessionContext:
    def __init__(self, session: RecordingSession) -> None:
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return False


class AuthoritativeLessonRepository:
    def __init__(self, lessons: list[dict]) -> None:
        self.lessons = lessons
        self.reads = 0

    def projection_design_lessons(self) -> list[dict]:
        self.reads += 1
        return self.lessons


class RebuildRepository(AuthoritativeLessonRepository):
    def __init__(self, lessons: list[dict], reviews: list[dict] | None = None) -> None:
        super().__init__(lessons)
        self.reviews = reviews or []

    def projection_families(self) -> list[dict]:
        return []

    def projection_products(self) -> list[dict]:
        return []

    def projection_subfamilies(self) -> list[dict]:
        return []

    def projection_models(self) -> list[dict]:
        return []

    def projection_assertions(self) -> list[dict]:
        return []

    def projection_profiles(self) -> list[dict]:
        return []

    def projection_design_lesson_reviews(self) -> list[dict]:
        return self.reviews


class FailingRebuildRepository(RebuildRepository):
    def projection_design_lessons(self) -> list[dict]:
        raise RuntimeError("injected authoritative replay failure")


class ClaimedOutboxRepository(AuthoritativeLessonRepository):
    def __init__(self, lessons: list[dict]) -> None:
        super().__init__(lessons)
        self.claims: list[dict] = []
        self.marks: list[dict] = []

    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int) -> list[dict]:
        self.claims.append(
            {"worker_id": worker_id, "limit": limit, "lease_seconds": lease_seconds}
        )
        return [{
            "id": "event-1",
            "event_type": "design_lesson.revoked",
            "aggregate_type": "design_lesson",
            "aggregate_id": "lesson-1",
            "aggregate_version": 4,
            "payload": {"lesson_id": "lesson-1"},
        }]

    def pending_outbox(self, _limit: int) -> list[dict]:
        return []

    def mark_outbox(self, event_id: str, worker_id: str, error: str | None = None) -> None:
        self.marks.append(
            {"event_id": event_id, "worker_id": worker_id, "error": error}
        )


class RetryingOutboxRepository(ClaimedOutboxRepository):
    def __init__(self, lessons: list[dict]) -> None:
        super().__init__(lessons)
        self.processed = False

    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int) -> list[dict]:
        if self.processed:
            return []
        return [{
            "id": "event-retry",
            "event_type": "design_lesson.revoked",
            "aggregate_type": "design_lesson",
            "aggregate_id": "lesson-1",
            "aggregate_version": 4,
            "payload": {"lesson_id": "lesson-1"},
        }]

    def mark_outbox(self, event_id: str, *, worker_id: str, error: str = "") -> None:
        super().mark_outbox(event_id, worker_id=worker_id, error=error)
        if not error:
            self.processed = True


class PartialReviewOutboxRepository:
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.claims: list[dict] = []
        self.marks: list[dict] = []

    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int) -> list[dict]:
        self.claims.append(
            {"worker_id": worker_id, "limit": limit, "lease_seconds": lease_seconds}
        )
        return self.events

    def mark_outbox(self, event_id: str, *, worker_id: str, error: str | None = None) -> None:
        self.marks.append({"event_id": event_id, "worker_id": worker_id, "error": error})
        if event_id == "event-ack-failed" and not error:
            raise RuntimeError("injected acknowledgement failure")


class ReviewFailureSession(RecordingSession):
    def run(self, query: str, **parameters):
        if (
            "MERGE (r:DesignLessonReview {review_id:$review_id})" in query
            and parameters["review_id"] == "review-project-failed"
        ):
            raise RuntimeError("injected review projection failure")
        return super().run(query, **parameters)


def approved_lesson() -> dict:
    return {
        "id": "lesson-1",
        "lesson_key": "DL-001",
        "title": "Preserve bearing clearance",
        "status": "approved",
        "organization_id": "organization-1",
        "job_id": "job-1",
        "package_sha256": "a" * 64,
        "source_model_revision_id": "model-1",
        "assertions": [{
            "id": "assertion-1",
            "subject_ref": "shaft",
            "predicate": "requires-clearance",
            "status": "approved",
            "scope_kind": "organization_general",
            "risk_level": "R3",
            "family_id": None,
        }],
    }


def review_event(
    event_type: str,
    *,
    review_id: str = "review-1",
    status: str = "awaiting-engineer-review",
    published_design_lesson_id: str | None = None,
    aggregate_version: int = 1,
) -> dict:
    payload = {
        "review_id": review_id,
        "status": status,
        "working_copy_id": "working-copy-1",
        "job_id": "job-1",
        "lesson_id": "lesson-1",
    }
    if published_design_lesson_id is not None:
        payload["published_design_lesson_id"] = published_design_lesson_id
    return {
        "id": f"event-{review_id}",
        "event_type": event_type,
        "aggregate_type": "design_lesson_review",
        "aggregate_id": review_id,
        "aggregate_version": aggregate_version,
        "created_at": "2026-08-11T12:00:00Z",
        "payload": payload,
    }


class DesignLessonProjectionTests(unittest.TestCase):
    def test_review_projection_never_passes_native_zoneinfo_to_neo4j_driver(self) -> None:
        session = RecordingSession()

        with self.assertRaisesRegex(TypeError, "UTC ISO-8601 string"):
            Neo4jProjection._project_design_lesson_review(
                session,
                review_event("design_lesson_review.approved")["payload"],
                occurred_at=datetime(2026, 8, 18, 16, 1, tzinfo=ZoneInfo("UTC")),
                aggregate_version=1,
            )

        self.assertEqual(session.calls, [])

    def test_review_projection_parses_utc_string_inside_neo4j(self) -> None:
        session = RecordingSession()

        Neo4jProjection._project_design_lesson_review(
            session,
            review_event("design_lesson_review.approved")["payload"],
            occurred_at="2026-08-18T16:01:56.942941Z",
            aggregate_version=1,
        )

        query, parameters = session.calls[0]
        self.assertIn("r.updated_at=datetime($occurred_at)", query)
        self.assertEqual(parameters["occurred_at"], "2026-08-18T16:01:56.942941Z")

    def test_review_event_prefers_projection_safe_utc_string(self) -> None:
        session = RecordingSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        event = review_event("design_lesson_review.approved")
        event["created_at"] = datetime(2026, 8, 18, 16, 1, tzinfo=ZoneInfo("UTC"))
        event["projection_occurred_at"] = "2026-08-18T16:01:00.000000Z"

        projection._project_event(session, object(), event)

        self.assertEqual(
            session.calls[0][1]["occurred_at"],
            "2026-08-18T16:01:00.000000Z",
        )

    def test_stale_prepared_review_event_cannot_downgrade_newer_review_state_or_rewrite_publication(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        for event_type, status in (
            ("design_lesson_review.approved", "approved-retrieval-pending"),
            ("design_lesson_review.rejected", "rejected"),
            ("design_lesson_review.invalid", "invalid"),
            ("design_lesson_review.retrieval_verified", "stored-and-retrievable"),
        ):
            with self.subTest(event_type=event_type):
                session = VersionAwareSession()
                projection._project_event(
                    session,
                    object(),
                    review_event(
                        "design_lesson_review.prepared",
                        aggregate_version=1,
                    ),
                )
                projection._project_event(
                    session,
                    object(),
                    review_event(
                        event_type,
                        status=status,
                        published_design_lesson_id="lesson-current",
                        aggregate_version=2,
                    ),
                )
                projection._project_event(
                    session,
                    object(),
                    review_event(
                        "design_lesson_review.prepared",
                        published_design_lesson_id="lesson-stale",
                        aggregate_version=1,
                    ),
                )

                self.assertEqual(
                    session.review_state["review-1"],
                    {"status": status, "aggregate_version": 2},
                )
                published_links = [
                    parameters["published_design_lesson_id"]
                    for query, parameters in session.calls
                    if "PUBLISHED_AS" in query
                ]
                self.assertEqual(published_links, ["lesson-current"])

    def test_equal_version_review_retry_is_an_acknowledgeable_no_op(self) -> None:
        session = VersionAwareSession()
        review = review_event(
            "design_lesson_review.approved",
            status="approved-retrieval-pending",
            published_design_lesson_id="lesson-published-1",
            aggregate_version=2,
        )

        first = Neo4jProjection._project_design_lesson_review(
            session, review["payload"], occurred_at=review["created_at"], aggregate_version=2
        )
        retry = Neo4jProjection._project_design_lesson_review(
            session, review["payload"], occurred_at=review["created_at"], aggregate_version=2
        )

        self.assertTrue(first)
        self.assertFalse(retry)
        self.assertEqual(
            session.review_state["review-1"],
            {"status": "approved-retrieval-pending", "aggregate_version": 2},
        )
        self.assertEqual(
            [query for query, _ in session.calls if "PUBLISHED_AS" in query],
            [
                "MATCH (r:DesignLessonReview {review_id:$review_id}) "
                "MERGE (l:DesignLesson {id:$published_design_lesson_id}) "
                "ON CREATE SET l.projection_owner=$owner "
                "MERGE (r)-[:PUBLISHED_AS]->(l)"
            ],
        )

    def test_approved_review_witness_is_emitted_only_after_outbox_acknowledgement(self) -> None:
        session = RecordingSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)
        repository = PartialReviewOutboxRepository([
            review_event(
                "design_lesson_review.approved",
                review_id="review-approved",
                status="approved-retrieval-pending",
                aggregate_version=2,
            ),
        ])

        result = projection.project_pending(repository)

        self.assertEqual(
            result["processed_events"],
            [{
                "event_id": "event-review-approved",
                "event_type": "design_lesson_review.approved",
                "aggregate_type": "design_lesson_review",
                "aggregate_id": "review-approved",
            }],
        )
        self.assertEqual(repository.marks[0]["error"], None)

    def test_review_lifecycle_events_project_audit_state_without_eligibility_fields(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        for event_type, status in (
            ("design_lesson_review.prepared", "awaiting-engineer-review"),
            ("design_lesson_review.superseded", "superseded"),
            ("design_lesson_review.approved", "approved-retrieval-pending"),
            ("design_lesson_review.rejected", "rejected"),
            ("design_lesson_review.retrieval_verified", "stored-and-retrievable"),
        ):
            with self.subTest(event_type=event_type):
                session = RecordingSession()

                projection._project_event(
                    session,
                    object(),
                    review_event(event_type, status=status),
                )

                audit_query, audit_parameters = next(
                    (query, parameters)
                    for query, parameters in session.calls
                    if "DesignLessonReview" in query
                )
                self.assertIn("MERGE (r:DesignLessonReview {review_id:$review_id})", audit_query)
                self.assertEqual(
                    audit_parameters,
                    {
                        "review_id": "review-1",
                        "status": status,
                        "working_copy_id": "working-copy-1",
                        "job_id": "job-1",
                        "lesson_id": "lesson-1",
                        "review_outcome": "publish",
                        "occurred_at": "2026-08-11T12:00:00Z",
                        "aggregate_version": 1,
                        "force": False,
                        "owner": "freecad-mechanical-design-agent",
                    },
                )
                projected_cypher = "\n".join(query for query, _ in session.calls)
                self.assertNotIn("KnowledgeAssertion", projected_cypher)
                self.assertNotIn("scope_kind", projected_cypher)
                self.assertNotIn("risk_level", projected_cypher)
                self.assertNotIn("applicability", projected_cypher)
                self.assertNotIn("search", projected_cypher)

    def test_review_event_links_published_lesson_when_published_id_is_present(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        session = RecordingSession()

        projection._project_event(
            session,
            object(),
            review_event(
                "design_lesson_review.approved",
                status="approved-retrieval-pending",
                published_design_lesson_id="lesson-published-1",
            ),
        )

        relationship_query, relationship_parameters = next(
            (query, parameters)
            for query, parameters in session.calls
            if "PUBLISHED_AS" in query
        )
        self.assertIn("MERGE (r)-[:PUBLISHED_AS]->(l)", relationship_query)
        self.assertEqual(relationship_parameters["review_id"], "review-1")
        self.assertEqual(relationship_parameters["published_design_lesson_id"], "lesson-published-1")

    def test_project_pending_reports_only_events_acknowledged_after_successful_projection(self) -> None:
        session = ReviewFailureSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)
        repository = PartialReviewOutboxRepository([
            review_event("design_lesson_review.prepared", review_id="review-succeeded"),
            review_event("design_lesson_review.rejected", review_id="review-project-failed"),
            {
                **review_event("unsupported.review_event", review_id="review-unsupported"),
                "aggregate_type": "unsupported",
            },
            review_event("design_lesson_review.approved", review_id="ack-failed"),
        ])
        repository.events[-1]["id"] = "event-ack-failed"

        result = projection.project_pending(repository)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["remaining_hint"], 3)
        self.assertEqual(
            result["processed_events"],
            [{
                "event_id": "event-review-succeeded",
                "event_type": "design_lesson_review.prepared",
                "aggregate_type": "design_lesson_review",
                "aggregate_id": "review-succeeded",
            }],
        )
        self.assertEqual(
            [failure["event_id"] for failure in result["failed"]],
            ["event-review-project-failed", "event-review-unsupported", "event-ack-failed"],
        )

    def test_pending_projection_uses_one_leased_worker_identity_through_acknowledgement(self) -> None:
        session = RecordingSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)
        repository = ClaimedOutboxRepository([
            {**approved_lesson(), "status": "revoked"},
        ])

        result = projection.project_pending(repository, limit=17)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(repository.claims), 1)
        self.assertEqual(repository.claims[0]["limit"], 17)
        self.assertGreater(repository.claims[0]["lease_seconds"], 0)
        self.assertEqual(
            repository.marks,
            [{
                "event_id": "event-1",
                "worker_id": repository.claims[0]["worker_id"],
                "error": None,
            }],
        )

    def test_stale_approved_event_cannot_overwrite_newer_inactive_lesson_state(self) -> None:
        for current_status in ("revoked", "superseded"):
            with self.subTest(current_status=current_status):
                session = VersionAwareSession()
                Neo4jProjection._project_design_lesson(
                    session,
                    {**approved_lesson(), "status": current_status, "aggregate_version": 2},
                )

                applied = Neo4jProjection._project_design_lesson(
                    session,
                    {**approved_lesson(), "status": "approved", "aggregate_version": 1},
                )

                self.assertFalse(applied)
                self.assertEqual(
                    session.lesson_state["lesson-1"],
                    {"status": current_status, "aggregate_version": 2},
                )

    def test_one_outbox_event_rolls_back_atomically_before_same_version_retry(self) -> None:
        session = AtomicEventSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)
        lesson = {**approved_lesson(), "status": "revoked"}
        lesson["assertions"] = [
            {**lesson["assertions"][0], "status": "superseded", "aggregate_version": 6}
        ]
        repository = RetryingOutboxRepository([lesson])

        first = projection.project_pending(repository)
        self.assertEqual(first["processed"], 0)
        self.assertEqual(session.lesson_state, {})
        self.assertEqual(session.assertion_state, {})

        second = projection.project_pending(repository)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(session.lesson_state["lesson-1"]["aggregate_version"], 4)
        self.assertEqual(session.assertion_state["assertion-1"]["status"], "superseded")

    def test_stale_approved_assertion_event_cannot_overwrite_newer_superseded_state(self) -> None:
        session = VersionAwareSession()
        assertion = approved_lesson()["assertions"][0]
        Neo4jProjection._project_assertion(
            session,
            {**assertion, "status": "superseded", "aggregate_version": 2},
        )

        applied = Neo4jProjection._project_assertion(
            session,
            {**assertion, "status": "approved", "aggregate_version": 1},
        )

        self.assertFalse(applied)
        self.assertEqual(
            session.assertion_state["assertion-1"],
            {"status": "superseded", "aggregate_version": 2},
        )

    def test_lesson_relationships_are_deterministic_history_without_family_authorization(self) -> None:
        lesson = {
            **approved_lesson(),
            "source_family_id": "family-provenance-only",
            "supersedes": "lesson-0",
            "assertions": [
                {**approved_lesson()["assertions"][0], "id": "assertion-2"},
                {**approved_lesson()["assertions"][0], "id": "assertion-1"},
            ],
        }

        relationships = Neo4jProjection.lesson_relationships(lesson)

        self.assertEqual(
            relationships,
            [
                {"type": "GENERATED_ASSERTION", "target_label": "KnowledgeAssertion", "target_id": "assertion-1"},
                {"type": "GENERATED_ASSERTION", "target_label": "KnowledgeAssertion", "target_id": "assertion-2"},
                {"type": "ORIGINATED_FROM", "target_label": "ModelRevision", "target_id": "model-1"},
                {"type": "SUPERSEDES", "target_label": "DesignLesson", "target_id": "lesson-0"},
            ],
        )

    def test_loads_every_constraint_file_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            constraints_dir = Path(temporary)
            (constraints_dir / "010_late.cypher").write_text("CREATE CONSTRAINT late;", encoding="utf-8")
            (constraints_dir / "002_early.cypher").write_text("CREATE CONSTRAINT early;", encoding="utf-8")
            session = RecordingSession()
            projection = Neo4jProjection("bolt://unused", "neo4j", "password")
            projection._driver = lambda: RecordingDriver(session)

            @contextmanager
            def migration_directory():
                yield constraints_dir

            with patch(
                "mechanical_design_agent.projection.neo4j_migrations_directory",
                migration_directory,
            ):
                projection.initialize_constraints()

        self.assertEqual(
            [query for query, _ in session.calls],
            ["CREATE CONSTRAINT early", "CREATE CONSTRAINT late"],
        )

    def test_projects_lesson_and_assertion_links(self) -> None:
        session = RecordingSession()

        Neo4jProjection._project_design_lesson(session, approved_lesson())

        queries = "\n".join(query for query, _ in session.calls)
        self.assertIn("DesignLesson", queries)
        self.assertIn("GENERATED_ASSERTION", queries)
        self.assertIn("ORIGINATED_FROM", queries)

    def test_lesson_projection_consumes_relationship_helper_output(self) -> None:
        session = RecordingSession()

        with patch.object(Neo4jProjection, "lesson_relationships", return_value=[]):
            Neo4jProjection._project_design_lesson(session, approved_lesson())

        queries = "\n".join(query for query, _ in session.calls)
        self.assertIn("DesignLesson", queries)
        self.assertNotIn("GENERATED_ASSERTION", queries)
        self.assertNotIn("ORIGINATED_FROM", queries)

    def test_lifecycle_events_reload_authoritative_lesson_state(self) -> None:
        projection = Neo4jProjection.__new__(Neo4jProjection)
        for event_type, status in (
            ("design_lesson.approved", "approved"),
            ("design_lesson.superseded", "superseded"),
            ("design_lesson.revoked", "revoked"),
        ):
            with self.subTest(event_type=event_type):
                session = RecordingSession()
                lesson = {**approved_lesson(), "status": status}
                repository = AuthoritativeLessonRepository([lesson])

                projection._project_event(
                    session,
                    repository,
                    {
                        "event_type": event_type,
                        "aggregate_version": 7,
                        "payload": {"lesson_id": lesson["id"]},
                    },
                )

                self.assertEqual(repository.reads, 1)
                self.assertEqual(session.calls[0][1]["status"], status)
                self.assertEqual(session.calls[0][1].get("aggregate_version"), 7)

    def test_projects_supersession_without_family_scope_edge(self) -> None:
        session = RecordingSession()
        lesson = {
            **approved_lesson(),
            "source_family_id": "family-provenance-only",
            "supersedes": "lesson-0",
        }

        Neo4jProjection._project_design_lesson(session, lesson)

        queries = "\n".join(query for query, _ in session.calls)
        self.assertIn("SUPERSEDES", queries)
        self.assertNotIn("SCOPED_TO", queries)

    def test_projects_linked_assertion_current_status(self) -> None:
        session = RecordingSession()
        lesson = approved_lesson()
        lesson["assertions"] = [{
            "id": "assertion-1",
            "subject_ref": "shaft",
            "predicate": "requires-clearance",
            "status": "superseded",
            "scope_kind": "organization_general",
            "risk_level": "R3",
            "family_id": None,
        }]

        Neo4jProjection._project_design_lesson(session, lesson)

        assertion_state_queries = [
            parameters
            for _query, parameters in session.calls
            if parameters.get("id") == "assertion-1"
        ]
        self.assertEqual(assertion_state_queries[0]["status"], "superseded")

    def test_rebuild_projects_lessons_from_the_authoritative_projection_read(self) -> None:
        session = RecordingSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)
        repository = RebuildRepository([approved_lesson()])

        result = projection.rebuild(repository)

        self.assertEqual(result["counts"]["design_lessons"], 1)
        self.assertEqual(repository.reads, 1)

    def test_rebuild_failure_rolls_back_without_changing_active_graph_generation(self) -> None:
        session = AtomicGraphSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)

        with self.assertRaisesRegex(RuntimeError, "injected authoritative replay failure"):
            projection.rebuild(FailingRebuildRepository([]))

        self.assertEqual(session.nodes, {"old-active-node"})
        self.assertEqual(session.active_generation, "generation-old")

    def test_rebuild_validates_new_generation_before_atomic_activation(self) -> None:
        session = AtomicGraphSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        projection.initialize_constraints = lambda: None
        projection._driver = lambda: RecordingDriver(session)

        result = projection.rebuild(RebuildRepository([approved_lesson()]))

        queries = [query for query, _parameters in session.calls]
        validation_positions = [
            index for index, query in enumerate(queries) if "RETURN count(n) AS node_count" in query
        ]
        activation_position = next(
            index for index, query in enumerate(queries) if "SET state.active_generation=$generation" in query
        )
        self.assertTrue(validation_positions)
        self.assertLess(max(validation_positions), activation_position)
        self.assertNotEqual(session.active_generation, "generation-old")
        self.assertEqual(session.nodes, {"lesson-1"})
        self.assertEqual(result["active_generation"], session.active_generation)

    def test_rebuild_restores_authoritative_review_state_and_publication_link(self) -> None:
        session = VersionAwareSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        repository = RebuildRepository(
            [approved_lesson()],
            reviews=[
                {
                    "review_id": "review-rebuild-1",
                    "status": "stored-and-retrievable",
                    "working_copy_id": "working-copy-1",
                    "lesson_id": "DL-001",
                    "published_design_lesson_id": "lesson-1",
                    "occurred_at": "2026-08-12T10:00:00Z",
                    "aggregate_version": 7,
                }
            ],
        )

        counts = projection._rebuild_transaction(
            session, repository, generation="review-rebuild-generation"
        )

        self.assertEqual(counts["design_lesson_reviews"], 1)
        self.assertEqual(
            session.review_state["review-rebuild-1"],
            {"status": "stored-and-retrievable", "aggregate_version": 7},
        )
        publication_parameters = [
            parameters
            for query, parameters in session.calls
            if "PUBLISHED_AS" in query
        ]
        self.assertEqual(
            publication_parameters[0]["published_design_lesson_id"], "lesson-1"
        )
        self.assertTrue(
            any(
                "MATCH (n:DesignLessonReview)" in query
                for query, _parameters in session.calls
            )
        )

    def test_rebuild_does_not_force_stale_snapshot_over_newer_projected_version(self) -> None:
        session = VersionAwareSession()
        projection = Neo4jProjection.__new__(Neo4jProjection)
        Neo4jProjection._project_design_lesson(
            session,
            {**approved_lesson(), "status": "revoked", "aggregate_version": 3},
        )

        projection._rebuild_transaction(
            session,
            RebuildRepository([
                {**approved_lesson(), "status": "approved", "aggregate_version": 2},
            ]),
            generation="stale-rebuild",
        )

        self.assertEqual(
            session.lesson_state["lesson-1"],
            {"status": "revoked", "aggregate_version": 3},
        )
        queries = [query for query, _parameters in session.calls]
        lock_position = next(
            index for index, query in enumerate(queries)
            if "ProjectionState" in query and "projection_epoch" in query
        )
        delete_position = next(
            index for index, query in enumerate(queries) if "DETACH DELETE n" in query
        )
        self.assertLess(lock_position, delete_position)


NEO4J_URI = os.environ.get("MECH_DESIGN_NEO4J_URI", "").strip()
NEO4J_USER = os.environ.get("MECH_DESIGN_NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.environ.get("MECH_DESIGN_NEO4J_PASSWORD", "").strip()


@unittest.skipUnless(
    NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD,
    "Neo4j credentials are not configured; live projection safety tests skipped",
)
class LiveDesignLessonProjectionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.projection = Neo4jProjection(
            NEO4J_URI,
            NEO4J_USER,
            NEO4J_PASSWORD,
        )
        cls.projection.initialize_constraints()

    def setUp(self) -> None:
        self.lesson_id = f"test-projection-{uuid.uuid4()}"
        self.assertion_id = f"test-projection-assertion-{uuid.uuid4()}"
        self.model_id = f"test-projection-model-{uuid.uuid4()}"

    def tearDown(self) -> None:
        with self.projection._driver() as driver, driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.projection_owner=$owner AND n.id IN $ids DETACH DELETE n",
                ids=[self.lesson_id, self.assertion_id, self.model_id],
                owner="freecad-mechanical-design-agent",
            ).consume()
            remaining = session.run(
                "MATCH (n) WHERE n.projection_owner=$owner AND n.id IN $ids "
                "RETURN count(n) AS node_count",
                ids=[self.lesson_id, self.assertion_id, self.model_id],
                owner="freecad-mechanical-design-agent",
            ).single()
        self.assertEqual(int(remaining["node_count"]), 0)

    def _lesson(self, *, status: str) -> dict:
        lesson = {
            **approved_lesson(),
            "id": self.lesson_id,
            "status": status,
            "source_model_revision_id": self.model_id,
        }
        lesson["assertions"] = [
            {**lesson["assertions"][0], "id": self.assertion_id}
        ]
        return lesson

    def _graph_snapshot(self):
        with self.projection._driver() as driver, driver.session() as session:
            record = session.run(
                "OPTIONAL MATCH (n) WHERE n.projection_owner=$owner "
                "WITH count(n) AS owned_count "
                "OPTIONAL MATCH (state:ProjectionState {name:$state_name}) "
                "RETURN owned_count,state.active_generation AS active_generation",
                owner="freecad-mechanical-design-agent",
                state_name="mechanical-design-agent",
            ).single()
        return int(record["owned_count"]), record["active_generation"]

    def test_live_stale_event_cannot_overwrite_newer_revoked_state(self) -> None:
        lesson = self._lesson(status="revoked")
        with self.projection._driver() as driver, driver.session() as session:
            self.projection._project_design_lesson(
                session, {**lesson, "aggregate_version": 2}
            )
            self.projection._project_design_lesson(
                session,
                {**lesson, "status": "approved", "aggregate_version": 1},
            )
            state = session.run(
                "MATCH (l:DesignLesson {id:$id}) RETURN l.status AS status,"
                "l.aggregate_version AS aggregate_version",
                id=self.lesson_id,
            ).single()

        self.assertEqual((state["status"], int(state["aggregate_version"])), ("revoked", 2))

    def test_live_stale_approved_assertion_cannot_overwrite_newer_superseded_state(self) -> None:
        assertion = self._lesson(status="revoked")["assertions"][0]
        with self.projection._driver() as driver, driver.session() as session:
            self.projection._project_assertion(
                session,
                {**assertion, "status": "superseded", "aggregate_version": 2},
            )
            self.projection._project_assertion(
                session,
                {**assertion, "status": "approved", "aggregate_version": 1},
            )
            state = session.run(
                "MATCH (a:KnowledgeAssertion {id:$id}) RETURN a.status AS status,"
                "a.aggregate_version AS aggregate_version",
                id=self.assertion_id,
            ).single()

        self.assertEqual(
            (state["status"], int(state["aggregate_version"])),
            ("superseded", 2),
        )

    def test_live_injected_rebuild_failure_preserves_active_graph_and_generation(self) -> None:
        before = self._graph_snapshot()

        with self.assertRaisesRegex(RuntimeError, "injected authoritative replay failure"):
            self.projection.rebuild(FailingRebuildRepository([]))

        self.assertEqual(self._graph_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
