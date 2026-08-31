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

from .migrations import discover_postgres_migrations
from .models import canonical_json, require_safe_id


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_MIGRATIONS = ("001_knowledge.sql",)


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
        card = json.loads(
            json.dumps(review_card, ensure_ascii=False, allow_nan=False)
        )
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

        with self.connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT review_sha256 FROM design_lesson_reviews "
                "WHERE review_sha256=%s",
                (review_sha256,),
            ).fetchone()
            if existing:
                rows = connection.execute(
                    "SELECT id FROM design_lessons WHERE review_sha256=%s ORDER BY id",
                    (review_sha256,),
                ).fetchall()
                return {
                    "publication_id": review_sha256,
                    "review_sha256": review_sha256,
                    "lesson_ids": [row["id"] for row in rows],
                    "resumed": True,
                }
            connection.execute(
                "INSERT INTO design_lesson_reviews"
                "(review_sha256,organization_id,design_group_id,product_family_id,"
                "review_card,decision,decision_text) "
                "VALUES (%s,%s,%s,NULL,%s::jsonb,'approved',%s)",
                (
                    review_sha256,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    canonical_json(card),
                    decision_text.strip(),
                ),
            )
            lesson_ids: list[str] = []
            for index, lesson in enumerate(lessons, start=1):
                if not isinstance(lesson, dict):
                    raise ValueError("review card lessons must be objects")
                lesson_id = f"lesson-{review_sha256[:16]}-{index}"
                family_id = lesson.get("product_family_id")
                search_terms = lesson.get("search_terms")
                if not isinstance(search_terms, list) or not all(
                    isinstance(value, str) for value in search_terms
                ):
                    raise ValueError("lesson search_terms are invalid")
                connection.execute(
                    "INSERT INTO design_lessons"
                    "(id,review_sha256,organization_id,design_group_id,"
                    "product_family_id,lesson,search_terms,applicability) "
                    "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)",
                    (
                        lesson_id,
                        review_sha256,
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        family_id,
                        canonical_json(lesson),
                        search_terms,
                        canonical_json(
                            {"summary": lesson.get("applicability", "")}
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_outbox"
                    "(aggregate_type,aggregate_id,event_type,payload) "
                    "VALUES ('design_lesson',%s,'published',%s::jsonb)",
                    (lesson_id, canonical_json({"lesson_id": lesson_id})),
                )
                lesson_ids.append(lesson_id)
        return {
            "publication_id": review_sha256,
            "review_sha256": review_sha256,
            "lesson_ids": lesson_ids,
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
        knowledge_json = canonical_json(dict(knowledge))
        with self.connection() as connection, connection.transaction():
            existing = connection.execute(
                "SELECT id,status FROM product_families WHERE id=%s",
                (family_id,),
            ).fetchone()
            if existing:
                return {"family_id": family_id, "status": existing["status"], "resumed": True}
            connection.execute(
                "INSERT INTO product_families"
                "(id,organization_id,design_group_id,canonical_name,aliases,knowledge) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (
                    family_id,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                    family_name.strip(),
                    canonical_json(aliases),
                    knowledge_json,
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_outbox"
                "(aggregate_type,aggregate_id,event_type,payload) "
                "VALUES ('product_family',%s,'published',%s::jsonb)",
                (family_id, canonical_json({"family_id": family_id})),
            )
        return {"family_id": family_id, "status": "active", "resumed": False}

    def search(
        self,
        *,
        query: str,
        product_family_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        if not query.strip():
            raise ValueError("query is required")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        lesson_family_clause = "AND product_family_id=%s" if product_family_id else ""
        lesson_parameters: list[object] = [
            self.scope.organization_id,
            self.scope.design_group_id,
        ]
        if product_family_id:
            lesson_parameters.append(product_family_id)
        lesson_parameters.extend([query.strip(), limit])
        with self.connection() as connection:
            if product_family_id:
                families = connection.execute(
                    "SELECT id,canonical_name,aliases,knowledge,status "
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
                    "SELECT id,canonical_name,aliases,knowledge,status "
                    "FROM product_families WHERE organization_id=%s "
                    "AND design_group_id=%s AND status='active' "
                    "AND to_tsvector('simple',canonical_name || ' ' || aliases::text "
                    "|| ' ' || knowledge::text) @@ plainto_tsquery('simple',%s) "
                    "ORDER BY id LIMIT %s",
                    (
                        self.scope.organization_id,
                        self.scope.design_group_id,
                        query.strip(),
                        limit,
                    ),
                ).fetchall()
            lessons = connection.execute(
                "SELECT id,lesson,product_family_id,status FROM design_lessons "
                "WHERE organization_id=%s AND design_group_id=%s "
                "AND status='approved' "
                f"{lesson_family_clause} "
                "AND search_document @@ plainto_tsquery('simple',%s) "
                "ORDER BY ts_rank(search_document,plainto_tsquery('simple',%s)) DESC,id "
                "LIMIT %s",
                tuple(
                    lesson_parameters[:-1]
                    + [query.strip(), lesson_parameters[-1]]
                ),
            ).fetchall()
        family_matches = [
            {"knowledge_id": row["id"], "kind": "product_family", **dict(row)}
            for row in families
        ]
        lesson_matches = [
            {"design_lesson_ref": row["id"], "kind": "design_lesson", **dict(row)}
            for row in lessons
        ]
        return {
            "schema_version": "KnowledgeSearchResult/v1",
            "status": (
                "completed_matches"
                if family_matches or lesson_matches
                else "completed_no_match"
            ),
            "families": family_matches,
            "lessons": lesson_matches,
            "matches": [*family_matches, *lesson_matches],
        }

    def get_design_lesson(self, lesson_id: str) -> dict[str, object]:
        require_safe_id(lesson_id, "lesson_id")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,lesson,product_family_id,status,supersedes_id,created_at "
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
                "AND status='approved' RETURNING id,status,supersedes_id",
                (
                    status,
                    replacement_lesson_id,
                    lesson_id,
                    self.scope.organization_id,
                    self.scope.design_group_id,
                ),
            ).fetchone()
            if not row:
                raise ValueError("approved Design Lesson does not exist in this scope")
            connection.execute(
                "INSERT INTO knowledge_outbox"
                "(aggregate_type,aggregate_id,event_type,payload) "
                "VALUES ('design_lesson',%s,%s,%s::jsonb)",
                (lesson_id, status, canonical_json({"lesson_id": lesson_id})),
            )
        return dict(row)

    def pending_projection_events(self, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,aggregate_type,aggregate_id,event_type,payload "
                "FROM knowledge_outbox WHERE projected_at IS NULL "
                "ORDER BY id LIMIT %s",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_projection_event(self, event_id: int) -> None:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE knowledge_outbox SET projected_at=now() "
                "WHERE id=%s AND projected_at IS NULL RETURNING id",
                (event_id,),
            ).fetchone()
            if not row:
                raise ValueError("projection event is missing or already completed")

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
