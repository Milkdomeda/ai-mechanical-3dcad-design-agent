from __future__ import annotations

from contextlib import contextmanager
import inspect

import pytest

from mechanical_design_agent.knowledge_repository import (
    KnowledgeRepository,
    KnowledgeScope,
)


class _Rows:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Transaction:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return None


class _FamilyConnection:
    def __init__(self, exact_rows=None, explicit_row=None, text_rows=None) -> None:
        self.exact_rows = list(exact_rows or [])
        self.explicit_row = explicit_row
        self.text_rows = list(text_rows or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters=()) -> _Rows:
        normalized = " ".join(query.split())
        self.calls.append((normalized, tuple(parameters)))
        if "AND id=%s" in normalized:
            return _Rows([self.explicit_row] if self.explicit_row else [])
        if "search_terms &&" in normalized:
            return _Rows(self.exact_rows)
        return _Rows(self.text_rows)


def _repository(connection) -> KnowledgeRepository:
    repository = KnowledgeRepository(
        "postgresql://unused", KnowledgeScope("org-1", "group-1")
    )

    @contextmanager
    def connect():
        yield connection

    repository.connection = connect  # type: ignore[method-assign]
    return repository


def test_family_match_prefers_exact_alias_before_full_text() -> None:
    connection = _FamilyConnection(
        exact_rows=[
            {
                "id": "PF-PILOT-001",
                "canonical_name": "Printed Ball Carrier",
                "aliases": ["四篮球载具"],
                "profile": {"mechanism": "spherical cradle"},
                "search_terms": ["四篮球载具"],
                "search_text": "Printed Ball Carrier 四篮球载具 spherical cradle",
                "status": "active",
            }
        ]
    )

    match = _repository(connection).match_product_family(
        query="四篮球载具", design_features={}, requested_family_id=None
    )

    assert match["id"] == "PF-PILOT-001"
    assert match["match_kind"] == "exact_term"
    assert len(connection.calls) == 1


def test_explicit_family_must_exist_in_scope() -> None:
    with pytest.raises(ValueError, match="does not exist in this scope"):
        _repository(_FamilyConnection()).match_product_family(
            query="carrier",
            design_features={},
            requested_family_id="PF-OTHER-SCOPE",
        )


def test_ambiguous_exact_family_match_fails_closed() -> None:
    rows = [
        {
            "id": value,
            "canonical_name": value,
            "aliases": [],
            "profile": {},
            "search_terms": ["carrier"],
            "search_text": "carrier",
            "status": "active",
        }
        for value in ("family-a", "family-b")
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        _repository(_FamilyConnection(exact_rows=rows)).match_product_family(
            query="carrier", design_features={}, requested_family_id=None
        )


class _SearchConnection:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters=()) -> _Rows:
        normalized = " ".join(query.split())
        self.calls.append((normalized, tuple(parameters)))
        if self.empty:
            return _Rows()
        if "FROM product_families" in normalized:
            return _Rows(
                [
                    {
                        "id": "PF-PILOT-001",
                        "canonical_name": "Printed Ball Carrier",
                        "aliases": ["四篮球载具"],
                        "profile": {"mechanism": "spherical cradle"},
                        "search_terms": ["spherical cradle"],
                        "search_text": "Printed Ball Carrier spherical cradle",
                        "status": "active",
                    }
                ]
            )
        if "FROM knowledge_assertions" in normalized:
            return _Rows(
                [
                    {
                        "id": "assertion-cradle",
                        "product_family_id": "PF-PILOT-001",
                        "subject": "ball support",
                        "predicate": "uses",
                        "object_value": "spherical cradle",
                        "applicability": {},
                        "evidence": [],
                        "search_terms": ["spherical cradle"],
                        "status": "active",
                        "supersedes_id": None,
                    }
                ]
            )
        return _Rows(
            [
                {
                    "id": "lesson-cradle",
                    "product_family_id": "PF-PILOT-001",
                    "content": {"problem": "poor ball support"},
                    "applicability": {},
                    "provenance": {"source_review_sha256": "a" * 64},
                    "search_terms": ["spherical cradle"],
                    "status": "active",
                    "supersedes_id": None,
                }
            ]
        )


def test_search_returns_family_assertion_and_lesson() -> None:
    connection = _SearchConnection()
    result = _repository(connection).search(
        query="spherical cradle", product_family_id="PF-PILOT-001"
    )

    assert result["status"] == "completed_matches"
    assert result["families"][0]["id"] == "PF-PILOT-001"
    assert result["assertions"][0]["kind"] == "knowledge_assertion"
    assert result["lessons"][0]["kind"] == "design_lesson"
    assert result["matches"] == [
        *result["families"],
        *result["assertions"],
        *result["lessons"],
    ]
    assert all("status='active'" in query for query, _ in connection.calls)


def test_search_excludes_nonactive_and_other_scope_records() -> None:
    result = _repository(_SearchConnection(empty=True)).search(
        query="private revoked term"
    )
    assert result["matches"] == []
    assert result["status"] == "completed_no_match"


class _PublishingConnection:
    def __init__(self) -> None:
        self.family: dict[str, object] | None = None
        self.assertions: list[dict[str, object]] = []
        self.calls: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    def execute(self, query: str, parameters=()) -> _Rows:
        normalized = " ".join(str(query).split())
        self.calls.append(normalized)
        if any(
            name in normalized
            for name in (
                "knowledge_outbox",
                "design_lesson_reviews",
                "knowledge_review_decisions",
            )
        ):
            raise AssertionError(f"deleted table referenced: {normalized}")
        if normalized.startswith("SELECT") and "FROM product_families" in normalized:
            if "AND id=%s" in normalized and self.family is None:
                return _Rows()
            return _Rows([self.family] if self.family else [])
        if normalized.startswith("INSERT INTO product_families"):
            self.family = {
                "id": str(parameters[0]),
                "canonical_name": str(parameters[3]),
                "aliases": ["sports-ball carrier"],
                "profile": {"mechanism": "spherical cradle"},
                "search_terms": ["printed ball carriers", "sports-ball carrier"],
                "search_text": "Printed Ball Carriers spherical cradle",
                "status": "active",
            }
            return _Rows()
        if normalized.startswith("INSERT INTO knowledge_assertions"):
            self.assertions.append(
                {
                    "id": str(parameters[0]),
                    "product_family_id": str(parameters[3]),
                    "subject": str(parameters[4]),
                    "predicate": str(parameters[5]),
                    "object_value": "broad radiused transition",
                    "applicability": {},
                    "evidence": [],
                    "search_terms": ["handle root"],
                    "status": "active",
                    "supersedes_id": None,
                }
            )
            return _Rows()
        if "FROM knowledge_assertions" in normalized:
            return _Rows(self.assertions)
        if "FROM design_lessons" in normalized:
            return _Rows()
        return _Rows()


def test_family_publication_persists_profile_and_generated_assertions() -> None:
    connection = _PublishingConnection()
    repository = _repository(connection)

    result = repository.publish_product_family(
        family_id="carrier-family",
        family_name="Printed Ball Carriers",
        aliases=["sports-ball carrier"],
        knowledge={
            "mechanism": "spherical cradle",
            "assertions": [
                {
                    "subject": "handle root",
                    "predicate": "uses",
                    "object": "broad radiused transition",
                    "search_terms": ["handle root"],
                }
            ],
        },
        decision_text="approved",
    )

    assert result["assertion_ids"]
    found = repository.search(query="handle root", product_family_id="carrier-family")
    assert found["assertions"][0]["subject"] == "handle root"
    assert "assertions" not in connection.family["profile"]


def test_repository_has_no_deleted_table_sql_or_incremental_projection_methods() -> None:
    source = inspect.getsource(KnowledgeRepository)
    for forbidden in (
        "design_lesson_reviews",
        "knowledge_review_decisions",
        "knowledge_outbox",
        "knowledge_projection_state",
    ):
        assert forbidden not in source
    assert not hasattr(KnowledgeRepository, "pending_projection_events")
    assert not hasattr(KnowledgeRepository, "mark_projection_event")
