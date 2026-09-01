from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .migrations import discover_postgres_migrations
from .knowledge_matching import collect_design_terms, normalize_search_term
from .models import canonical_json, require_safe_id


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_MIGRATIONS = ("001_knowledge.sql",)


def _json_copy(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc


def _search_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        result: list[str] = []
        for key in sorted(value, key=str):
            result.extend(_search_values(str(key)))
            result.extend(_search_values(value[key]))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_search_values(item))
        return result
    if isinstance(value, (bool, int, float)):
        return [str(value)]
    return []


def _search_text(*values: object) -> str:
    text = " ".join(_search_values(values))
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("knowledge record has no searchable text")
    return normalized


def _normalized_strings(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return sorted(
        {
            normalized
            for item in value
            if (normalized := normalize_search_term(item))
        }
    )


class KnowledgeDatabaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class KnowledgeScope:
    organization_id: str
    design_group_id: str

    def __post_init__(self) -> None:
        require_safe_id(self.organization_id, "organization_id")
        require_safe_id(self.design_group_id, "design_group_id")


class KnowledgeRepository:
    """PostgreSQL authority for Product Family Knowledge and Design Lessons."""

    def __init__(
        self,
        database_url: str,
        scope: KnowledgeScope,
        *,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        if not isinstance(database_url, str) or not database_url.strip():
            raise ValueError("database_url is required")
        self.database_url = database_url.strip()
        self.scope = scope
        self._connect = connect

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self._connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def apply_migrations(self, root: Path) -> dict[str, list[str]]:
        migrations = discover_postgres_migrations(root)
        if tuple(path.name for path in migrations) != _EXPECTED_MIGRATIONS:
            raise ValueError("knowledge migration inventory is incomplete or unexpected")
        applied: list[str] = []
        skipped: list[str] = []
        with self.connection() as connection:
            self._reject_incompatible_database(connection)
            for path in migrations:
                version = int(path.name[:3])
                sql_bytes = path.read_bytes()
                try:
                    sql = sql_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"migration is not UTF-8: {path.name}") from exc
                digest = hashlib.sha256(sql_bytes).hexdigest()
                with connection.transaction():
                    existing = connection.execute(
                        "SELECT filename,sha256 FROM knowledge_schema_migrations "
                        "WHERE version=%s",
                        (version,),
                    ).fetchone() if version != 1 or self._knowledge_schema_exists(connection) else None
                    if existing:
                        if (
                            existing["filename"] != path.name
                            or existing["sha256"] != digest
                        ):
                            raise KnowledgeDatabaseError(
                                "KNOWLEDGE_MIGRATION_DRIFT",
                                f"knowledge migration {version:03d} does not match",
                            )
                        skipped.append(path.name)
                        continue
                    connection.execute(sql)
                    connection.execute(
                        "INSERT INTO knowledge_schema_migrations"
                        "(version,filename,sha256) VALUES (%s,%s,%s)",
                        (version, path.name, digest),
                    )
                    applied.append(path.name)
        return {"applied": applied, "skipped": skipped}

    @staticmethod
    def _knowledge_schema_exists(connection: Any) -> bool:
        row = connection.execute(
            "SELECT to_regclass('public.knowledge_schema_migrations') AS name"
        ).fetchone()
        return bool(row and row.get("name"))

    def _reject_incompatible_database(self, connection: Any) -> None:
        row = connection.execute(
            "SELECT "
            "to_regclass('public.knowledge_schema_migrations') AS knowledge_schema,"
            "(SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE') AS existing_tables"
        ).fetchone() or {}
        if not row.get("knowledge_schema") and int(row.get("existing_tables") or 0) > 0:
            raise KnowledgeDatabaseError(
                "KNOWLEDGE_DATABASE_REINITIALIZATION_REQUIRED",
                "the database contains an unsupported schema; initialize a new knowledge database",
            )

    def publish_design_lesson_review(
        self,
        *,
        review_card: Mapping[str, object],
        review_sha256: str,
        decision_text: str = "approved",
    ) -> dict[str, object]:
        if not _SHA256.fullmatch(review_sha256):
            raise ValueError("review_sha256 must be lowercase 64-hex")
        card = _json_copy(dict(review_card), "review card")
        if not isinstance(card, dict) or card.get("schema_version") != (
            "DesignLessonReviewCard/v1"
        ):
            raise ValueError("review card schema is invalid")
        if hashlib.sha256(canonical_json(card).encode("utf-8")).hexdigest() != (
            review_sha256
        ):
            raise ValueError("review card SHA-256 does not match")
        lessons = card.get("lessons")
        if not isinstance(lessons, list) or not lessons:
            raise ValueError("review card has no publishable lessons")
        if not isinstance(decision_text, str) or not decision_text.strip():
            raise ValueError("decision_text is required")

        expected_rows: list[dict[str, object]] = []
        for index, lesson in enumerate(lessons, start=1):
            if not isinstance(lesson, Mapping):
                raise ValueError("review card lessons must be objects")
            copied = _json_copy(dict(lesson), "review card lesson")
            lesson_id = f"lesson-{review_sha256[:16]}-{index}"
            family_id = copied.pop("product_family_id", None)
            copied.pop("scope", None)
            search_terms = _normalized_strings(
                copied.pop("search_terms", []), "lesson search_terms"
            )
            raw_applicability = copied.pop("applicability", {})
            if isinstance(raw_applicability, Mapping):
                applicability = _json_copy(
                    dict(raw_applicability), "lesson applicability"
                )
            elif isinstance(raw_applicability, str):
                applicability = (
                    {"summary": raw_applicability.strip()}
                    if raw_applicability.strip()
                    else {}
                )
            else:
                raise ValueError("lesson applicability must be an object or string")
            evidence = copied.pop("evidence", [])
            provenance = {
                "source_review_sha256": review_sha256,
                "decision_text": decision_text.strip(),
                "source_review_id": card["review_id"],
                "evidence": evidence,
            }
            expected_rows.append(
                {
                    "id": lesson_id,
                    "product_family_id": family_id,
                    "content": copied,
                    "applicability": applicability,
                    "provenance": provenance,
                    "search_terms": search_terms,
                    "search_text": _search_text(
                        copied, applicability, provenance, search_terms
                    ),
                    "status": "active",
                    "supersedes_id": None,
                }
            )

        with self.connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id,product_family_id,content,applicability,provenance,"
                "search_terms,search_text,status,supersedes_id FROM design_lessons "
                "WHERE organization_id=%s AND design_group_id=%s "
                "AND provenance->>'source_review_sha256'=%s ORDER BY id",
                (
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    review_sha256,
                ),
            ).fetchall()
            if existing:
                if canonical_json([dict(row) for row in existing]) != canonical_json(
                    expected_rows
                ):
                    raise ValueError(
                        "Design Lessons already exist with different canonical content"
                    )
                return {
                    "publication_id": review_sha256,
                    "review_sha256": review_sha256,
                    "lesson_ids": [row["id"] for row in expected_rows],
                    "resumed": True,
                }
            for row in expected_rows:
                connection.execute(
                    "INSERT INTO design_lessons("
                    "id,organization_id,design_group_id,product_family_id,content,"
                    "applicability,provenance,search_terms,search_text,status,"
                    "supersedes_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
                    (
                        row["id"],
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        row["product_family_id"],
                        Jsonb(row["content"]),
                        Jsonb(row["applicability"]),
                        Jsonb(row["provenance"]),
                        row["search_terms"],
                        row["search_text"],
                        row["status"],
                    ),
                )
        return {
            "publication_id": review_sha256,
            "review_sha256": review_sha256,
            "lesson_ids": [row["id"] for row in expected_rows],
            "resumed": False,
        }

    def publish_product_family(
        self,
        *,
        family_id: str,
        family_name: str,
        aliases: list[object],
        knowledge: Mapping[str, object],
        decision_text: str,
    ) -> dict[str, object]:
        require_safe_id(family_id, "family_id")
        if not family_name.strip() or not decision_text.strip():
            raise ValueError("family_name and decision_text are required")
        if not all(isinstance(value, str) and value.strip() for value in aliases):
            raise ValueError("aliases must contain nonblank strings")
        copied_knowledge = _json_copy(dict(knowledge), "Product Family Knowledge")
        raw_assertions = copied_knowledge.pop("assertions", [])
        if not isinstance(raw_assertions, list):
            raise ValueError("knowledge.assertions must be a list")
        normalized_aliases = sorted({str(value).strip() for value in aliases})
        family_terms = sorted(
            {
                normalize_search_term(family_name),
                *[normalize_search_term(value) for value in normalized_aliases],
                *_normalized_strings(
                    copied_knowledge.get("retrieval_terms"),
                    "knowledge.retrieval_terms",
                ),
            }
            - {""}
        )
        family_row = {
            "id": family_id,
            "canonical_name": family_name.strip(),
            "aliases": normalized_aliases,
            "profile": copied_knowledge,
            "search_terms": family_terms,
            "search_text": _search_text(
                family_name, normalized_aliases, family_terms, copied_knowledge
            ),
            "status": "active",
        }
        assertion_rows: list[dict[str, object]] = []
        for raw in raw_assertions:
            if not isinstance(raw, Mapping):
                raise ValueError("knowledge assertions must be objects")
            assertion = _json_copy(dict(raw), "knowledge assertion")
            subject = assertion.get("subject")
            predicate = assertion.get("predicate")
            if not isinstance(subject, str) or not subject.strip():
                raise ValueError("knowledge assertion subject is required")
            if not isinstance(predicate, str) or not predicate.strip():
                raise ValueError("knowledge assertion predicate is required")
            if "object" not in assertion and "object_value" not in assertion:
                raise ValueError("knowledge assertion object is required")
            object_value = assertion.get("object", assertion.get("object_value"))
            applicability = _json_copy(
                assertion.get("applicability") or {}, "assertion applicability"
            )
            evidence = _json_copy(assertion.get("evidence") or [], "assertion evidence")
            search_terms = _normalized_strings(
                assertion.get("search_terms"), "assertion search_terms"
            )
            digest_input = {
                "subject": subject.strip(),
                "predicate": predicate.strip(),
                "object_value": object_value,
                "applicability": applicability,
                "evidence": evidence,
                "search_terms": search_terms,
            }
            digest = hashlib.sha256(
                canonical_json(digest_input).encode("utf-8")
            ).hexdigest()
            assertion_id = f"{family_id[:101]}-assertion-{digest[:16]}"
            assertion_rows.append(
                {
                    "id": assertion_id,
                    "product_family_id": family_id,
                    **digest_input,
                    "search_text": _search_text(
                        subject,
                        predicate,
                        object_value,
                        applicability,
                        evidence,
                        search_terms,
                    ),
                    "status": "active",
                    "supersedes_id": None,
                }
            )
        assertion_rows.sort(key=lambda row: str(row["id"]))
        if len({str(row["id"]) for row in assertion_rows}) != len(assertion_rows):
            raise ValueError("duplicate Product Family assertions are not allowed")
        with self.connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id,canonical_name,aliases,profile,search_terms,search_text,status "
                "FROM product_families WHERE organization_id=%s "
                "AND design_group_id=%s AND id=%s",
                (
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    family_id,
                ),
            ).fetchone()
            if existing:
                existing_family = dict(existing)
                if canonical_json(existing_family) != canonical_json(family_row):
                    raise ValueError("Product Family already exists with different content")
                existing_assertions = connection.execute(
                    "SELECT id,product_family_id,subject,predicate,object_value,"
                    "applicability,evidence,search_terms,search_text,status,supersedes_id "
                    "FROM knowledge_assertions WHERE organization_id=%s "
                    "AND design_group_id=%s AND product_family_id=%s ORDER BY id",
                    (
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        family_id,
                    ),
                ).fetchall()
                if canonical_json([dict(row) for row in existing_assertions]) != (
                    canonical_json(assertion_rows)
                ):
                    raise ValueError(
                        "Product Family assertions already exist with different content"
                    )
                return {
                    "family_id": family_id,
                    "assertion_ids": [row["id"] for row in assertion_rows],
                    "status": existing["status"],
                    "resumed": True,
                }
            connection.execute(
                "INSERT INTO product_families"
                "(id,organization_id,design_group_id,canonical_name,aliases,profile,"
                "search_terms,search_text,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    family_id,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    family_row["canonical_name"],
                    family_row["aliases"],
                    Jsonb(family_row["profile"]),
                    family_row["search_terms"],
                    family_row["search_text"],
                    family_row["status"],
                ),
            )
            for row in assertion_rows:
                connection.execute(
                    "INSERT INTO knowledge_assertions("
                    "id,organization_id,design_group_id,product_family_id,subject,"
                    "predicate,object_value,applicability,evidence,search_terms,"
                    "search_text,status,supersedes_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)",
                    (
                        row["id"],
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        family_id,
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
        return {
            "family_id": family_id,
            "assertion_ids": [row["id"] for row in assertion_rows],
            "status": "active",
            "resumed": False,
        }

    @staticmethod
    def _family_match(row: Mapping[str, object], match_kind: str) -> dict[str, object]:
        return {
            "knowledge_id": row["id"],
            "kind": "product_family",
            **dict(row),
            "match_kind": match_kind,
        }

    def match_product_family(
        self,
        *,
        query: str,
        design_features: Mapping[str, object],
        requested_family_id: str | None = None,
    ) -> dict[str, object] | None:
        terms = collect_design_terms(query, design_features)
        columns = (
            "id,canonical_name,aliases,profile,search_terms,search_text,status"
        )
        with self.connection() as connection:
            if requested_family_id:
                require_safe_id(requested_family_id, "requested_family_id")
                row = connection.execute(
                    f"SELECT {columns} FROM product_families "
                    "WHERE organization_id=%s AND design_group_id=%s "
                    "AND status='active' AND id=%s",
                    (
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        requested_family_id,
                    ),
                ).fetchone()
                if not row:
                    raise ValueError("Product Family does not exist in this scope")
                return self._family_match(row, "explicit_id")
            if not terms:
                return None
            exact = connection.execute(
                f"SELECT {columns} FROM product_families "
                "WHERE organization_id=%s AND design_group_id=%s AND status='active' "
                "AND search_terms && %s::text[] ORDER BY id LIMIT 2",
                (
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    list(terms),
                ),
            ).fetchall()
            if len(exact) > 1:
                raise ValueError("Product Family exact-term match is ambiguous")
            if exact:
                return self._family_match(exact[0], "exact_term")
            text_query = " ".join(terms)
            rows = connection.execute(
                f"SELECT {columns} FROM product_families "
                "WHERE organization_id=%s AND design_group_id=%s AND status='active' "
                "AND to_tsvector('simple',search_text) "
                "@@ plainto_tsquery('simple',%s) ORDER BY id LIMIT 1",
                (
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    text_query,
                ),
            ).fetchall()
        return self._family_match(rows[0], "full_text") if rows else None

    def search(
        self,
        *,
        query: str,
        product_family_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        terms = collect_design_terms(query, {})
        if not terms:
            raise ValueError("query is required")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_query = " ".join(terms)
        family_clause = (
            "AND (product_family_id=%s OR product_family_id IS NULL)"
            if product_family_id
            else ""
        )

        def scoped_parameters() -> list[object]:
            parameters: list[object] = [
                self.scope.organization_id,
                self.scope.design_group_id,
            ]
            if product_family_id:
                parameters.append(product_family_id)
            parameters.extend([list(terms), normalized_query, limit])
            return parameters

        with self.connection() as connection:
            if product_family_id:
                families = connection.execute(
                    "SELECT id,canonical_name,aliases,profile,search_terms,"
                    "search_text,status "
                    "FROM product_families WHERE organization_id=%s "
                    "AND design_group_id=%s AND status='active' AND id=%s LIMIT %s",
                    (
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        product_family_id,
                        limit,
                    ),
                ).fetchall()
            else:
                families = connection.execute(
                    "SELECT id,canonical_name,aliases,profile,search_terms,"
                    "search_text,status "
                    "FROM product_families WHERE organization_id=%s "
                    "AND design_group_id=%s AND status='active' "
                    "AND (search_terms && %s::text[] OR "
                    "to_tsvector('simple',search_text) "
                    "@@ plainto_tsquery('simple',%s)) "
                    "ORDER BY id LIMIT %s",
                    (
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        list(terms),
                        normalized_query,
                        limit,
                    ),
                ).fetchall()
            assertions = connection.execute(
                "SELECT id,product_family_id,subject,predicate,object_value,"
                "applicability,evidence,search_terms,status,supersedes_id "
                "FROM knowledge_assertions WHERE organization_id=%s "
                "AND design_group_id=%s AND status='active' "
                f"{family_clause} "
                "AND (search_terms && %s::text[] OR "
                "to_tsvector('simple',search_text) "
                "@@ plainto_tsquery('simple',%s)) ORDER BY id LIMIT %s",
                tuple(scoped_parameters()),
            ).fetchall()
            lessons = connection.execute(
                "SELECT id,product_family_id,content,applicability,provenance,"
                "search_terms,status,supersedes_id FROM design_lessons "
                "WHERE organization_id=%s AND design_group_id=%s "
                "AND status='active' "
                f"{family_clause} "
                "AND (search_terms && %s::text[] OR "
                "to_tsvector('simple',search_text) "
                "@@ plainto_tsquery('simple',%s)) ORDER BY id LIMIT %s",
                tuple(scoped_parameters()),
            ).fetchall()
        family_matches = [
            {"knowledge_id": row["id"], "kind": "product_family", **dict(row)}
            for row in families
        ]
        assertion_matches = [
            {
                "assertion_id": row["id"],
                "knowledge_id": row["id"],
                "kind": "knowledge_assertion",
                **dict(row),
            }
            for row in assertions
        ]
        lesson_matches = [
            {
                "design_lesson_ref": row["id"],
                "knowledge_id": row["id"],
                "kind": "design_lesson",
                **dict(row),
            }
            for row in lessons
        ]
        return {
            "schema_version": "KnowledgeSearchResult/v1",
            "status": (
                "completed_matches"
                if family_matches or assertion_matches or lesson_matches
                else "completed_no_match"
            ),
            "families": family_matches,
            "assertions": assertion_matches,
            "lessons": lesson_matches,
            "matches": [*family_matches, *assertion_matches, *lesson_matches],
        }

    def get_design_lesson(self, lesson_id: str) -> dict[str, object]:
        require_safe_id(lesson_id, "lesson_id")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,content,applicability,provenance,search_terms,"
                "product_family_id,status,supersedes_id,created_at "
                "FROM design_lessons WHERE id=%s AND organization_id=%s "
                "AND design_group_id=%s",
                (
                    lesson_id,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                ),
            ).fetchone()
        if not row:
            raise ValueError("Design Lesson does not exist in this scope")
        return dict(row)

    def set_design_lesson_status(
        self,
        *,
        lesson_id: str,
        status: str,
        replacement_lesson_id: str | None = None,
    ) -> dict[str, object]:
        if status not in {"superseded", "revoked"}:
            raise ValueError("status must be superseded or revoked")
        if status == "superseded" and not replacement_lesson_id:
            raise ValueError("superseded status requires replacement_lesson_id")
        require_safe_id(lesson_id, "lesson_id")
        if replacement_lesson_id:
            require_safe_id(replacement_lesson_id, "replacement_lesson_id")
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE design_lessons SET status=%s,supersedes_id=%s "
                "WHERE id=%s AND organization_id=%s AND design_group_id=%s "
                "AND status='active' RETURNING id,status,supersedes_id",
                (
                    status,
                    replacement_lesson_id,
                    lesson_id,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                ),
            ).fetchone()
            if not row:
                raise ValueError("active Design Lesson does not exist in this scope")
        return dict(row)

    def projection_record(
        self, *, aggregate_type: str, aggregate_id: str
    ) -> dict[str, object]:
        queries = {
            "product_family": (
                "SELECT id,canonical_name,status,organization_id,design_group_id "
                "FROM product_families WHERE id=%s"
            ),
            "assertion": (
                "SELECT id,subject,predicate,status,organization_id,design_group_id,"
                "product_family_id FROM knowledge_assertions WHERE id=%s"
            ),
            "design_lesson": (
                "SELECT id,status,organization_id,design_group_id,product_family_id "
                "FROM design_lessons WHERE id=%s"
            ),
        }
        query = queries.get(aggregate_type)
        if query is None:
            raise ValueError(f"unsupported knowledge aggregate: {aggregate_type}")
        with self.connection() as connection:
            row = connection.execute(query, (aggregate_id,)).fetchone()
        if not row:
            raise ValueError("knowledge record does not exist")
        return dict(row)

    def projection_records(self) -> dict[str, list[dict[str, object]]]:
        with self.connection() as connection:
            return {
                "product_family": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id,canonical_name,status,organization_id,design_group_id "
                        "FROM product_families ORDER BY id"
                    ).fetchall()
                ],
                "assertion": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id,subject,predicate,status,organization_id,design_group_id,"
                        "product_family_id FROM knowledge_assertions ORDER BY id"
                    ).fetchall()
                ],
                "design_lesson": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id,status,organization_id,design_group_id,product_family_id "
                        "FROM design_lessons ORDER BY id"
                    ).fetchall()
                ],
            }


__all__ = [
    "KnowledgeDatabaseError",
    "KnowledgeRepository",
    "KnowledgeScope",
]
