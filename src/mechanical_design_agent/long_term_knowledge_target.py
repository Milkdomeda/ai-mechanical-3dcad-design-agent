from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Literal, Mapping

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .long_term_knowledge_migration import LongTermKnowledgeExport
from .knowledge_repository import KnowledgeRepository, KnowledgeScope
from .migrations import postgres_migrations_directory
from .models import canonical_json


_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_BUSINESS_TABLES = (
    "product_families",
    "knowledge_assertions",
    "design_lessons",
)


class KnowledgeMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _copy(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc


def _normalize_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _normalized_terms(*collections: object) -> list[str]:
    terms: set[str] = set()
    for collection in collections:
        values = collection if isinstance(collection, (list, tuple)) else [collection]
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = _normalize_term(value)
            if normalized:
                terms.add(normalized)
    return sorted(terms)


def _flatten_search_values(values: object) -> list[str]:
    flattened: list[str] = []
    if values is None:
        return flattened
    if isinstance(values, str):
        if values.strip():
            flattened.append(values.strip())
        return flattened
    if isinstance(values, Mapping):
        for key in sorted(values, key=str):
            flattened.extend(_flatten_search_values(str(key)))
            flattened.extend(_flatten_search_values(values[key]))
        return flattened
    if isinstance(values, (list, tuple)):
        for value in values:
            flattened.extend(_flatten_search_values(value))
        return flattened
    if isinstance(values, (bool, int, float)):
        flattened.append(str(values))
    return flattened


def _search_text(*values: object) -> str:
    text = " ".join(_flatten_search_values(values))
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("knowledge record has no searchable text")
    return normalized


@dataclass(frozen=True)
class SimplifiedKnowledgePayload:
    source_export_sha256: str
    product_families: tuple[dict[str, object], ...]
    knowledge_assertions: tuple[dict[str, object], ...]
    design_lessons: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "SimplifiedKnowledgePayload/v1",
            "source_export_sha256": self.source_export_sha256,
            "product_families": _copy(
                list(self.product_families), "product families"
            ),
            "knowledge_assertions": _copy(
                list(self.knowledge_assertions), "knowledge assertions"
            ),
            "design_lessons": _copy(list(self.design_lessons), "design lessons"),
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_json(self.as_dict()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class MigrationImportResult:
    status: Literal["imported", "already_imported"]
    source_export_sha256: str
    payload_sha256: str
    counts: dict[str, int]


def build_simplified_payload(
    export: LongTermKnowledgeExport,
) -> SimplifiedKnowledgePayload:
    if not isinstance(export, LongTermKnowledgeExport):
        raise ValueError("export must be a LongTermKnowledgeExport")

    families: list[dict[str, object]] = []
    for source in export.product_families:
        knowledge = dict(source.get("knowledge") or {})
        approved_profile = _copy(
            knowledge.get("approved_profile") or {}, "approved family profile"
        )
        if not isinstance(approved_profile, dict):
            raise ValueError("approved family profile must be an object")
        profile = dict(approved_profile)
        profile_evidence = _copy(
            knowledge.get("approved_profile_evidence") or [],
            "approved family profile evidence",
        )
        if profile_evidence:
            if "evidence" in profile and profile["evidence"] != profile_evidence:
                raise ValueError("approved family profile evidence conflicts with profile")
            profile["evidence"] = profile_evidence
        terms = _normalized_terms(
            source.get("canonical_name"),
            source.get("aliases") or [],
            knowledge.get("retrieval_terms") or [],
        )
        families.append(
            {
                "id": str(source["id"]),
                "organization_id": str(source["organization_id"]),
                "design_group_id": str(source["design_group_id"]),
                "canonical_name": str(source["canonical_name"]),
                "aliases": _copy(source.get("aliases") or [], "family aliases"),
                "profile": profile,
                "search_terms": terms,
                "search_text": _search_text(
                    source.get("canonical_name"),
                    source.get("aliases") or [],
                    terms,
                    profile,
                ),
                "status": "active",
            }
        )

    assertions: list[dict[str, object]] = []
    for source in export.knowledge_assertions:
        evidence = _copy(source.get("evidence") or [], "assertion evidence")
        if not isinstance(evidence, list):
            raise ValueError("assertion evidence must be a list")
        created_at = source.get("created_at")
        if created_at:
            evidence.append({"source_created_at": str(created_at)})
        terms = _normalized_terms(source.get("search_terms") or [])
        applicability = _copy(
            source.get("applicability") or {}, "assertion applicability"
        )
        assertions.append(
            {
                "id": str(source["id"]),
                "organization_id": str(source["organization_id"]),
                "design_group_id": str(source["design_group_id"]),
                "product_family_id": source.get("product_family_id"),
                "subject": str(source["subject"]),
                "predicate": str(source["predicate"]),
                "object_value": _copy(
                    source.get("object_value"), "assertion object value"
                ),
                "applicability": applicability,
                "evidence": evidence,
                "search_terms": terms,
                "search_text": _search_text(
                    source.get("subject"),
                    source.get("predicate"),
                    source.get("object_value"),
                    applicability,
                    evidence,
                    terms,
                ),
                "status": "active",
                "supersedes_id": source.get("supersedes_id"),
            }
        )

    reviews = {
        str(row["review_sha256"]): row for row in export.design_lesson_reviews
    }
    lessons: list[dict[str, object]] = []
    for source in export.design_lessons:
        review_sha256 = str(source["review_sha256"])
        review = reviews.get(review_sha256)
        if review is None:
            raise ValueError(f"design lesson review is missing: {review_sha256}")
        source_content = _copy(source.get("lesson") or {}, "design lesson content")
        if not isinstance(source_content, dict):
            raise ValueError("design lesson content must be an object")
        evidence = source_content.pop("evidence", [])
        source_key = source_content.pop("source_lesson_key", None)
        source_content.pop("search_terms", None)
        source_content.pop("product_family_id", None)
        source_content.pop("scope", None)
        source_content.pop("applicability", None)
        applicability = _copy(
            source.get("applicability") or {}, "design lesson applicability"
        )
        terms = _normalized_terms(source.get("search_terms") or [])
        review_card = dict(review.get("review_card") or {})
        provenance = {
            "source_lesson_key": source_key,
            "source_review_sha256": review_sha256,
            "source_review_id": review_card.get("review_id"),
            "decision_text": review.get("decision_text"),
            "source_decided_at": review.get("decided_at"),
            "source_created_at": source.get("created_at"),
            "evidence": evidence,
        }
        lessons.append(
            {
                "id": str(source["id"]),
                "organization_id": str(source["organization_id"]),
                "design_group_id": str(source["design_group_id"]),
                "product_family_id": source.get("product_family_id"),
                "content": source_content,
                "applicability": applicability,
                "provenance": provenance,
                "search_terms": terms,
                "search_text": _search_text(
                    source_content, applicability, provenance, terms
                ),
                "status": "active",
                "supersedes_id": source.get("supersedes_id"),
            }
        )

    return SimplifiedKnowledgePayload(
        source_export_sha256=export.sha256,
        product_families=tuple(sorted(families, key=lambda row: str(row["id"]))),
        knowledge_assertions=tuple(
            sorted(assertions, key=lambda row: str(row["id"]))
        ),
        design_lessons=tuple(sorted(lessons, key=lambda row: str(row["id"]))),
    )


def _validated_database_name(target_database_name: str) -> str:
    if not isinstance(target_database_name, str) or not _DATABASE_NAME.fullmatch(
        target_database_name
    ):
        raise ValueError("target database name must be a safe PostgreSQL identifier")
    return target_database_name


def derive_target_database_url(
    source_database_url: str, target_database_name: str
) -> str:
    target = _validated_database_name(target_database_name)
    if not isinstance(source_database_url, str) or not source_database_url.strip():
        raise ValueError("source_database_url is required")
    fields = conninfo_to_dict(source_database_url.strip())
    fields["dbname"] = target
    return make_conninfo(**fields)


def create_target_database(
    source_database_url: str,
    target_database_name: str,
    *,
    connect: Callable[..., Any] = psycopg.connect,
) -> str:
    target = _validated_database_name(target_database_name)
    target_url = derive_target_database_url(source_database_url, target)
    with connect(source_database_url.strip(), autocommit=True) as connection:
        current = connection.execute("SELECT current_database()").fetchone()
        current_name = current[0] if not isinstance(current, Mapping) else next(
            iter(current.values())
        )
        if str(current_name) == target:
            raise KnowledgeMigrationError(
                "SOURCE_TARGET_DATABASE_CONFLICT",
                "target database must differ from the source database",
            )
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (target,)
        ).fetchone()
        if exists:
            raise KnowledgeMigrationError(
                "TARGET_DATABASE_ALREADY_EXISTS",
                "target database already exists; creation requires a new empty database",
            )
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
    return target_url


def _apply_migrations(
    target_database_url: str,
    *,
    connect: Callable[..., Any],
) -> None:
    repository = KnowledgeRepository(
        target_database_url,
        KnowledgeScope(
            organization_id="migration-target",
            design_group_id="migration-target",
        ),
        connect=connect,
    )
    with postgres_migrations_directory() as root:
        repository.apply_migrations(root)


def _read_target_collections(connection: Any) -> dict[str, list[dict[str, object]]]:
    families = connection.execute(
        "SELECT id,organization_id,design_group_id,canonical_name,aliases,profile,"
        "search_terms,search_text,status FROM product_families ORDER BY id"
    ).fetchall()
    assertions = connection.execute(
        "SELECT id,organization_id,design_group_id,product_family_id,subject,"
        "predicate,object_value,applicability,evidence,search_terms,search_text,"
        "status,supersedes_id FROM knowledge_assertions ORDER BY id"
    ).fetchall()
    lessons = connection.execute(
        "SELECT id,organization_id,design_group_id,product_family_id,content,"
        "applicability,provenance,search_terms,search_text,status,supersedes_id "
        "FROM design_lessons ORDER BY id"
    ).fetchall()
    return {
        "product_families": [_copy(dict(row), "target product family") for row in families],
        "knowledge_assertions": [
            _copy(dict(row), "target knowledge assertion") for row in assertions
        ],
        "design_lessons": [_copy(dict(row), "target design lesson") for row in lessons],
    }


def _insert_payload(connection: Any, payload: SimplifiedKnowledgePayload) -> None:
    for row in payload.product_families:
        connection.execute(
            "INSERT INTO product_families("
            "id,organization_id,design_group_id,canonical_name,aliases,profile,"
            "search_terms,search_text,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["id"],
                row["organization_id"],
                row["design_group_id"],
                row["canonical_name"],
                row["aliases"],
                Jsonb(row["profile"]),
                row["search_terms"],
                row["search_text"],
                row["status"],
            ),
        )
    for row in payload.knowledge_assertions:
        connection.execute(
            "INSERT INTO knowledge_assertions("
            "id,organization_id,design_group_id,product_family_id,subject,predicate,"
            "object_value,applicability,evidence,search_terms,search_text,status,"
            "supersedes_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
            (
                row["id"],
                row["organization_id"],
                row["design_group_id"],
                row["product_family_id"],
                row["subject"],
                row["predicate"],
                Jsonb(row["object_value"]),
                Jsonb(row["applicability"]),
                Jsonb(row["evidence"]),
                row["search_terms"],
                row["search_text"],
                row["status"],
            ),
        )
    for row in payload.design_lessons:
        connection.execute(
            "INSERT INTO design_lessons("
            "id,organization_id,design_group_id,product_family_id,content,"
            "applicability,provenance,search_terms,search_text,status,supersedes_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
            (
                row["id"],
                row["organization_id"],
                row["design_group_id"],
                row["product_family_id"],
                Jsonb(row["content"]),
                Jsonb(row["applicability"]),
                Jsonb(row["provenance"]),
                row["search_terms"],
                row["search_text"],
                row["status"],
            ),
        )
    for table, rows in (
        ("knowledge_assertions", payload.knowledge_assertions),
        ("design_lessons", payload.design_lessons),
    ):
        for row in rows:
            if row.get("supersedes_id"):
                connection.execute(
                    sql.SQL("UPDATE {} SET supersedes_id=%s WHERE id=%s").format(
                        sql.Identifier(table)
                    ),
                    (row["supersedes_id"], row["id"]),
                )


def _public_table_names(connection: Any) -> set[str]:
    return {
        str(row[0] if not isinstance(row, Mapping) else row["table_name"])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        ).fetchall()
    }


def _payload_dict_from_collections(
    source_export_sha256: str,
    collections: Mapping[str, list[dict[str, object]]],
) -> dict[str, object]:
    return {
        "schema_version": "SimplifiedKnowledgePayload/v1",
        "source_export_sha256": source_export_sha256,
        "product_families": collections["product_families"],
        "knowledge_assertions": collections["knowledge_assertions"],
        "design_lessons": collections["design_lessons"],
    }


def _verify_target_content(
    payload: SimplifiedKnowledgePayload,
    collections: Mapping[str, list[dict[str, object]]],
) -> None:
    actual = _payload_dict_from_collections(payload.source_export_sha256, collections)
    if canonical_json(actual) != canonical_json(payload.as_dict()):
        raise KnowledgeMigrationError(
            "TARGET_CONTENT_MISMATCH",
            "target knowledge rows differ from the canonical target payload",
        )


def import_simplified_payload(
    target_database_url: str,
    payload: SimplifiedKnowledgePayload,
    *,
    connect: Callable[..., Any] = psycopg.connect,
) -> MigrationImportResult:
    if not isinstance(payload, SimplifiedKnowledgePayload):
        raise ValueError("payload must be a SimplifiedKnowledgePayload")
    if not isinstance(target_database_url, str) or not target_database_url.strip():
        raise ValueError("target_database_url is required")
    _apply_migrations(target_database_url.strip(), connect=connect)
    imported = False
    with connect(target_database_url.strip(), row_factory=dict_row) as connection:
        with connection.transaction():
            collections = _read_target_collections(connection)
            if not any(collections[name] for name in _BUSINESS_TABLES):
                _insert_payload(connection, payload)
                imported = True
                collections = _read_target_collections(connection)
            _verify_target_content(payload, collections)
    counts = {name: len(collections[name]) for name in _BUSINESS_TABLES}
    return MigrationImportResult(
        status="imported" if imported else "already_imported",
        source_export_sha256=payload.source_export_sha256,
        payload_sha256=payload.sha256,
        counts=counts,
    )


def validate_simplified_target(
    target_database_url: str,
    payload: SimplifiedKnowledgePayload,
    *,
    connect: Callable[..., Any] = psycopg.connect,
) -> dict[str, object]:
    if not isinstance(payload, SimplifiedKnowledgePayload):
        raise ValueError("payload must be a SimplifiedKnowledgePayload")
    with connect(target_database_url.strip(), row_factory=dict_row) as connection:
        tables = _public_table_names(connection)
        expected_tables = {"knowledge_schema_migrations", *_BUSINESS_TABLES}
        if tables != expected_tables:
            raise KnowledgeMigrationError(
                "TARGET_SCHEMA_MISMATCH",
                "target public table inventory differs from the simplified schema",
            )
        collections = _read_target_collections(connection)
        _verify_target_content(payload, collections)
    return {
        "schema_version": "SimplifiedKnowledgeTargetValidation/v1",
        "status": "passed",
        "source_export_sha256": payload.source_export_sha256,
        "payload_sha256": payload.sha256,
        "counts": {name: len(collections[name]) for name in _BUSINESS_TABLES},
        "tables": sorted(tables),
    }


__all__ = [
    "KnowledgeMigrationError",
    "MigrationImportResult",
    "SimplifiedKnowledgePayload",
    "build_simplified_payload",
    "create_target_database",
    "derive_target_database_url",
    "import_simplified_payload",
    "validate_simplified_target",
]
