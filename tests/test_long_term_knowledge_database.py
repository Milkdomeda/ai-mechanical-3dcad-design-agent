from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from mechanical_design_agent.long_term_knowledge_database import (
    SOURCE_QUERIES,
    publish_source_backup,
    read_source_export,
)
from mechanical_design_agent.long_term_knowledge_migration import (
    ALLOWED_SOURCE_KEYS,
    build_long_term_export,
)


def _empty_source() -> dict[str, list[dict[str, object]]]:
    return {key: [] for key in ALLOWED_SOURCE_KEYS}


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def transaction(self) -> object:
        return nullcontext()

    def execute(self, query: str) -> _Result:
        self.statements.append(" ".join(query.split()))
        return _Result([])


def test_source_reader_uses_read_only_transaction_and_allowed_tables() -> None:
    connection = _Connection()

    export = read_source_export(
        "postgresql://user:secret@example.invalid/source",
        connect=lambda *_args, **_kwargs: connection,
    )

    joined = " ".join(connection.statements)
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in joined
    assert set(SOURCE_QUERIES) == set(ALLOWED_SOURCE_KEYS)
    for table in SOURCE_QUERIES:
        assert f"FROM {table}" in joined
    for forbidden in (
        "design_jobs",
        "design_working_copies",
        "design_change_sets",
        "design_approval_envelopes",
    ):
        assert forbidden not in joined
    assert export.source_counts["product_families"] == 0


def test_backup_contains_canonical_export_and_no_database_url(tmp_path: Path) -> None:
    export = build_long_term_export(_empty_source())
    destination = tmp_path / "attempt" / "source-export.json"

    result = publish_source_backup(export, destination)

    payload = destination.read_text(encoding="utf-8")
    assert export.sha256 == result["sha256"]
    assert result["status"] == "created"
    assert "postgresql://" not in payload
    assert not destination.stat().st_mode & 0o200


def test_backup_is_idempotent_only_for_identical_content(tmp_path: Path) -> None:
    export = build_long_term_export(_empty_source())
    destination = tmp_path / "attempt" / "source-export.json"
    publish_source_backup(export, destination)

    repeated = publish_source_backup(export, destination)

    assert repeated["status"] == "existing"

    destination.chmod(0o600)
    destination.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="different content"):
        publish_source_backup(export, destination)
