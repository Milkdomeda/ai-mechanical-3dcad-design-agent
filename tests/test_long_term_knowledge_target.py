from __future__ import annotations

from copy import deepcopy

import pytest
from psycopg.conninfo import conninfo_to_dict

from mechanical_design_agent.long_term_knowledge_migration import (
    LongTermKnowledgeExport,
    build_long_term_export,
)
from mechanical_design_agent.long_term_knowledge_target import (
    KnowledgeMigrationError,
    build_simplified_payload,
    create_target_database,
    derive_target_database_url,
    import_simplified_payload,
    validate_simplified_target,
)
import mechanical_design_agent.long_term_knowledge_target as target_module
from mechanical_design_agent.models import canonical_json
from test_long_term_knowledge_migration import _source


def expected_export() -> LongTermKnowledgeExport:
    return build_long_term_export(_source())


def expected_payload():
    return build_simplified_payload(expected_export())


class _FakeTarget:
    def __init__(self) -> None:
        self.url = "postgresql:///disposable_target"
        self.tables: set[str] = set()
        self.rows: dict[str, list[dict[str, object]]] = {
            "product_families": [],
            "knowledge_assertions": [],
            "design_lessons": [],
        }

    def apply_migrations(self, _url: str, *, connect) -> None:
        self.tables = {
            "knowledge_schema_migrations",
            "product_families",
            "knowledge_assertions",
            "design_lessons",
        }

    def connect(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext()

    def read(self, _connection) -> dict[str, list[dict[str, object]]]:
        return deepcopy(self.rows)

    def insert(self, _connection, payload) -> None:
        self.rows = {
            "product_families": deepcopy(list(payload.product_families)),
            "knowledge_assertions": deepcopy(list(payload.knowledge_assertions)),
            "design_lessons": deepcopy(list(payload.design_lessons)),
        }

    def public_tables(self) -> set[str]:
        return set(self.tables)

    def change_family_name(self, family_id: str, value: str) -> None:
        family = next(row for row in self.rows["product_families"] if row["id"] == family_id)
        family["canonical_name"] = value


@pytest.fixture
def fake_target(monkeypatch) -> _FakeTarget:
    target = _FakeTarget()
    monkeypatch.setattr(target_module, "_apply_migrations", target.apply_migrations)
    monkeypatch.setattr(target_module, "_read_target_collections", target.read)
    monkeypatch.setattr(target_module, "_insert_payload", target.insert)
    monkeypatch.setattr(
        target_module, "_public_table_names", lambda _connection: target.public_tables()
    )
    return target


def test_payload_contains_only_three_business_collections() -> None:
    payload = build_simplified_payload(expected_export())
    assert set(payload.as_dict()) == {
        "schema_version",
        "source_export_sha256",
        "product_families",
        "knowledge_assertions",
        "design_lessons",
    }
    assert len(payload.product_families) == 2
    assert len(payload.knowledge_assertions) == 43
    assert len(payload.design_lessons) == 4
    encoded = canonical_json(payload.as_dict())
    for forbidden in (
        "design_lesson_reviews",
        "authorization",
        "outbox",
        "receipt",
    ):
        assert forbidden not in encoded


def test_source_approval_becomes_active_and_review_becomes_provenance() -> None:
    payload = build_simplified_payload(expected_export())
    assert {row["status"] for row in payload.knowledge_assertions} == {"active"}
    lesson = payload.design_lessons[0]
    assert lesson["status"] == "active"
    assert len(lesson["provenance"]["source_review_sha256"]) == 64


def test_payload_is_deterministic_and_does_not_alias_source_data() -> None:
    export = expected_export()
    first = build_simplified_payload(export)
    second = build_simplified_payload(export)
    assert first.as_dict() == second.as_dict()
    assert first.sha256 == second.sha256

    mutated = deepcopy(first.as_dict())
    mutated["product_families"][0]["profile"]["tampered"] = True
    assert first.as_dict() != mutated


def test_target_url_derivation_replaces_only_database_name() -> None:
    source = "postgresql://user:secret@localhost:5432/source_db?sslmode=disable"
    derived = derive_target_database_url(source, "target_db")
    fields = conninfo_to_dict(derived)
    assert fields["dbname"] == "target_db"
    assert fields["user"] == "user"
    assert fields["host"] == "localhost"
    assert fields["port"] == "5432"


@pytest.mark.parametrize("name", ["", "source-db", "bad/name", "x" * 64])
def test_target_database_name_must_be_a_safe_identifier(name: str) -> None:
    with pytest.raises(ValueError, match="target database name"):
        derive_target_database_url("postgresql:///source_db", name)


def test_error_exposes_stable_code() -> None:
    error = KnowledgeMigrationError("TARGET_CONTENT_MISMATCH", "conflict")
    assert error.code == "TARGET_CONTENT_MISMATCH"


class _AdminResult:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _AdminConnection:
    def __init__(self, current_database: str, target_exists: bool = False) -> None:
        self.current_database = current_database
        self.target_exists = target_exists
        self.statements: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, params=None):
        self.statements.append(query)
        text = str(query)
        if "current_database" in text:
            return _AdminResult((self.current_database,))
        if "pg_database" in text:
            return _AdminResult((1,) if self.target_exists else None)
        return _AdminResult(None)


def test_target_creation_uses_admin_connection_and_distinct_safe_name() -> None:
    admin = _AdminConnection("source_db")
    result = create_target_database(
        "postgresql:///source_db",
        "target_db",
        connect=lambda *_args, **_kwargs: admin,
    )
    assert conninfo_to_dict(result)["dbname"] == "target_db"
    assert any("CREATE DATABASE" in str(statement) for statement in admin.statements)


def test_target_creation_rejects_source_database_name() -> None:
    admin = _AdminConnection("source_db")
    with pytest.raises(KnowledgeMigrationError, match="SOURCE_TARGET_DATABASE_CONFLICT"):
        create_target_database(
            "postgresql:///source_db",
            "source_db",
            connect=lambda *_args, **_kwargs: admin,
        )
    assert not any("CREATE DATABASE" in str(statement) for statement in admin.statements)


def test_import_has_no_receipt_or_projection_side_effects(fake_target) -> None:
    result = import_simplified_payload(
        fake_target.url, expected_payload(), connect=fake_target.connect
    )
    assert result.status == "imported"
    assert fake_target.public_tables() == {
        "knowledge_schema_migrations",
        "product_families",
        "knowledge_assertions",
        "design_lessons",
    }


def test_exact_rerun_is_idempotent_without_receipt(fake_target) -> None:
    first = import_simplified_payload(
        fake_target.url, expected_payload(), connect=fake_target.connect
    )
    second = import_simplified_payload(
        fake_target.url, expected_payload(), connect=fake_target.connect
    )
    assert first.payload_sha256 == second.payload_sha256
    assert second.status == "already_imported"


def test_conflicting_nonempty_target_fails_closed(fake_target) -> None:
    import_simplified_payload(
        fake_target.url, expected_payload(), connect=fake_target.connect
    )
    fake_target.change_family_name("PF-PILOT-001", "conflict")
    with pytest.raises(KnowledgeMigrationError, match="TARGET_CONTENT_MISMATCH"):
        import_simplified_payload(
            fake_target.url, expected_payload(), connect=fake_target.connect
        )


def test_target_validation_compares_semantic_rows(fake_target) -> None:
    payload = expected_payload()
    import_simplified_payload(fake_target.url, payload, connect=fake_target.connect)
    result = validate_simplified_target(
        fake_target.url, payload, connect=fake_target.connect
    )
    assert result["status"] == "passed"
    assert result["payload_sha256"] == payload.sha256
    assert result["counts"] == {
        "product_families": 2,
        "knowledge_assertions": 43,
        "design_lessons": 4,
    }
