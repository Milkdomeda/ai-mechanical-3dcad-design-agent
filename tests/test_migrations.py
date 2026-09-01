from __future__ import annotations

from pathlib import Path

from mechanical_design_agent.migrations import (
    discover_postgres_migrations,
    postgres_migrations_directory,
)


def _migration_text(name: str) -> str:
    with postgres_migrations_directory() as root:
        return (root / name).read_text(encoding="utf-8")


def test_packaged_database_has_exactly_the_knowledge_migration_line() -> None:
    with postgres_migrations_directory() as root:
        names = [path.name for path in discover_postgres_migrations(root)]

    assert names == ["001_knowledge.sql"]


def test_core_schema_contains_only_knowledge_authority() -> None:
    sql = _migration_text("001_knowledge.sql")

    for table in (
        "knowledge_schema_migrations",
        "product_families",
        "knowledge_assertions",
        "design_lessons",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "model.FCStd" not in sql


def test_search_schema_keeps_exact_and_text_retrieval_without_vector() -> None:
    sql = _migration_text("001_knowledge.sql")

    assert "to_tsvector('simple', search_text)" in sql
    assert "search_terms" in sql
    assert "CREATE EXTENSION" not in sql
    assert "embedding vector(" not in sql
    assert "knowledge_assertions_scope_idx" in sql
    assert "design_lessons_scope_idx" in sql


def test_discovery_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "001_one.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "001_two.sql").write_text("SELECT 2", encoding="utf-8")

    try:
        discover_postgres_migrations(tmp_path)
    except ValueError as exc:
        assert "duplicate migration version" in str(exc)
    else:
        raise AssertionError("duplicate migration versions were accepted")
