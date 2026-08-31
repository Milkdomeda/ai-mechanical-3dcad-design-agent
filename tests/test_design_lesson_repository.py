from __future__ import annotations

from contextlib import contextmanager
import hashlib

import pytest

from mechanical_design_agent.knowledge_repository import (
    KnowledgeDatabaseError,
    KnowledgeRepository,
    KnowledgeScope,
)
from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.models import canonical_json


class _Rows:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Transaction:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc_value, traceback):
        return None


class _PublicationConnection:
    def __init__(self) -> None:
        self.review_sha256: str | None = None
        self.lesson_ids: list[str] = []
        self.outbox: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Rows:
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT review_sha256"):
            return _Rows(
                [{"review_sha256": self.review_sha256}]
                if self.review_sha256
                else []
            )
        if normalized.startswith("SELECT id FROM design_lessons"):
            return _Rows([{"id": value} for value in sorted(self.lesson_ids)])
        if normalized.startswith("INSERT INTO design_lesson_reviews"):
            self.review_sha256 = str(parameters[0])
        elif normalized.startswith("INSERT INTO design_lessons"):
            self.lesson_ids.append(str(parameters[0]))
        elif normalized.startswith("INSERT INTO knowledge_outbox"):
            self.outbox.append(str(parameters[0]))
        return _Rows()


def _card() -> dict[str, object]:
    return {
        "schema_version": "DesignLessonReviewCard/v1",
        "review_id": "review-carrier-123",
        "design_id": "carrier",
        "design_title": "Carrier",
        "model_sha256": "1" * 64,
        "validation_report_sha256": "2" * 64,
        "evidence": [],
        "lessons": [
            {
                "problem": "Handle root bending",
                "decision": "Use broad radiused roots",
                "evidence": ["validation/report.json"],
                "applicability": "Printed carriers",
                "prevention_action": "Inspect both load paths",
                "search_terms": ["handle root"],
                "scope": "organization_general",
                "product_family_id": None,
            }
        ],
        "screening": [],
    }


def test_review_publication_is_transactional_and_idempotent() -> None:
    connection = _PublicationConnection()
    repository = KnowledgeRepository(
        "postgresql://unused", KnowledgeScope("org-001", "group-001")
    )

    @contextmanager
    def connect():
        yield connection

    repository.connection = connect  # type: ignore[method-assign]
    card = _card()
    digest = hashlib.sha256(canonical_json(card).encode("utf-8")).hexdigest()

    first = repository.publish_design_lesson_review(
        review_card=card, review_sha256=digest, decision_text="批准"
    )
    repeated = repository.publish_design_lesson_review(
        review_card=card, review_sha256=digest, decision_text="approved"
    )

    assert first["resumed"] is False
    assert repeated["resumed"] is True
    assert repeated["lesson_ids"] == first["lesson_ids"]
    assert len(connection.lesson_ids) == 1
    assert connection.outbox == connection.lesson_ids


def test_review_publication_rejects_hash_mismatch() -> None:
    repository = KnowledgeRepository(
        "postgresql://unused", KnowledgeScope("org-001", "group-001")
    )

    with pytest.raises(ValueError, match="does not match"):
        repository.publish_design_lesson_review(
            review_card=_card(), review_sha256="0" * 64
        )


class _SearchConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Rows:
        self.calls.append((" ".join(query.split()), parameters))
        if "FROM product_families" in query:
            return _Rows(
                [
                    {
                        "id": "carrier-family",
                        "canonical_name": "Printed carriers",
                        "aliases": [],
                        "knowledge": {"assertions": [{"subject": "handle"}]},
                        "status": "active",
                    }
                ]
            )
        return _Rows(
            [
                {
                    "id": "lesson-handle-root",
                    "lesson": {"problem": "handle root bending"},
                    "product_family_id": "carrier-family",
                    "status": "approved",
                }
            ]
        )


def test_search_returns_product_family_knowledge_and_design_lessons() -> None:
    connection = _SearchConnection()
    repository = KnowledgeRepository(
        "postgresql://unused", KnowledgeScope("org-001", "group-001")
    )

    @contextmanager
    def connect():
        yield connection

    repository.connection = connect  # type: ignore[method-assign]
    result = repository.search(
        query="printed handle", product_family_id="carrier-family", limit=5
    )

    assert result["status"] == "completed_matches"
    assert result["families"][0]["knowledge_id"] == "carrier-family"
    assert result["lessons"][0]["design_lesson_ref"] == "lesson-handle-root"
    assert result["matches"] == [*result["families"], *result["lessons"]]
    assert connection.calls[0][1] == (
        "org-001",
        "group-001",
        "carrier-family",
        5,
    )
    assert connection.calls[1][1] == (
        "org-001",
        "group-001",
        "carrier-family",
        "printed handle",
        "printed handle",
        5,
    )


class _IncompatibleConnection:
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Rows:
        del parameters
        if "existing_tables" in query:
            return _Rows(
                [
                    {
                        "knowledge_schema": None,
                        "existing_tables": 8,
                    }
                ]
            )
        return _Rows()


def test_prior_database_requires_explicit_reinitialization() -> None:
    repository = KnowledgeRepository(
        "postgresql://unused", KnowledgeScope("org-001", "group-001")
    )

    @contextmanager
    def connect():
        yield _IncompatibleConnection()

    repository.connection = connect  # type: ignore[method-assign]
    with postgres_migrations_directory() as migrations:
        with pytest.raises(KnowledgeDatabaseError) as captured:
            repository.apply_migrations(migrations)

    assert captured.value.code == "KNOWLEDGE_DATABASE_REINITIALIZATION_REQUIRED"
