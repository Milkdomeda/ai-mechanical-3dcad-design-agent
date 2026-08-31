from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping
import uuid

import psycopg
from psycopg.rows import dict_row

from .long_term_knowledge_migration import (
    LongTermKnowledgeExport,
    build_long_term_export,
)
from .models import canonical_json
from .secure_fs import (
    atomic_publish_new,
    ensure_managed_directory,
    read_managed_file,
    set_managed_file_readonly,
    validate_managed_path,
)


SOURCE_QUERIES = {
    "organizations": "SELECT id,name FROM organizations ORDER BY id",
    "design_groups": (
        "SELECT id,organization_id,name FROM design_groups ORDER BY id"
    ),
    "product_families": (
        "SELECT id,organization_id,design_group_id,canonical_name,aliases,status,"
        "config,revision FROM product_families ORDER BY id"
    ),
    "family_profiles": (
        "SELECT id,family_id,revision,status,profile,evidence,created_at "
        "FROM family_profiles ORDER BY family_id,revision"
    ),
    "knowledge_assertions": (
        "SELECT id,organization_id,design_group_id,family_id,subject_ref,predicate,"
        "object_value,applicability,non_applicable_conditions,evidence,status,"
        "supersedes,source_kind,risk_level,confidence,created_by,created_at "
        "FROM knowledge_assertions ORDER BY id"
    ),
    "knowledge_search_documents": (
        "SELECT assertion_id,family_id,exact_terms,search_text "
        "FROM knowledge_search_documents ORDER BY assertion_id"
    ),
    "design_lesson_events": (
        "SELECT id,lesson_key,revision,organization_id,source_design_group_id,"
        "source_family_id,title,problem,root_causes,corrections,prevention,"
        "applicability,non_applicable_conditions,search_terms,evidence_manifest,"
        "status,supersedes,approved_by,approval_text,approved_at "
        "FROM design_lesson_events ORDER BY lesson_key,revision"
    ),
}


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError(f"source value type is not JSON-compatible: {type(value).__name__}")


def read_source_export(
    source_database_url: str,
    *,
    connect: Callable[..., Any] = psycopg.connect,
) -> LongTermKnowledgeExport:
    """Read only the reusable knowledge collections in one stable snapshot."""
    if not isinstance(source_database_url, str) or not source_database_url.strip():
        raise ValueError("source_database_url is required")
    source: dict[str, list[dict[str, object]]] = {}
    with connect(source_database_url.strip(), row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            for name, query in SOURCE_QUERIES.items():
                rows = connection.execute(query).fetchall()
                source[name] = [_json_value(dict(row)) for row in rows]
    return build_long_term_export(source)


def publish_source_backup(
    export: LongTermKnowledgeExport, destination: Path
) -> dict[str, object]:
    """Publish an immutable canonical backup without connection information."""
    if not isinstance(export, LongTermKnowledgeExport):
        raise ValueError("export must be a LongTermKnowledgeExport")
    destination = Path(destination).expanduser().resolve()
    ensure_managed_directory(destination.parent, parents=True, exist_ok=True)
    managed = validate_managed_path(destination, allow_missing_leaf=True).path
    content = canonical_json(export.as_dict()).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    status = "created"
    if managed.exists():
        existing = read_managed_file(managed)
        if existing.content != content:
            raise ValueError("source backup already exists with different content")
        status = "existing"
    else:
        atomic_publish_new(managed, content)
        set_managed_file_readonly(managed)
    return {
        "schema_version": "LongTermKnowledgeBackupResult/v1",
        "status": status,
        "path": str(managed),
        "sha256": digest,
        "export_sha256": export.sha256,
        "counts": dict(export.source_counts),
    }


__all__ = [
    "SOURCE_QUERIES",
    "publish_source_backup",
    "read_source_export",
]
