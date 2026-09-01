from mechanical_design_agent.migrations import postgres_migrations_directory


EXPECTED_TABLES = {
    "knowledge_schema_migrations",
    "product_families",
    "knowledge_assertions",
    "design_lessons",
}


def test_postgres_has_one_simplified_baseline() -> None:
    with postgres_migrations_directory() as root:
        names = sorted(path.name for path in root.glob("*.sql"))
        sql = (root / "001_knowledge.sql").read_text(encoding="utf-8")
    assert names == ["001_knowledge.sql"]
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table}" in sql
    for forbidden in (
        "organizations",
        "design_groups",
        "review",
        "decision",
        "outbox",
        "projection_state",
        "import_receipt",
        "create extension",
        "embedding vector(",
    ):
        assert forbidden not in sql.lower()
