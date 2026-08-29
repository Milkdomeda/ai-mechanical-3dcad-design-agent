from __future__ import annotations

import base64
import binascii
import json
import re
import hashlib
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator
import uuid

from .approval_envelope import (
    classify_change_against_envelope,
    require_mutation_authorization,
    validate_approval_envelope_draft,
)
from .design_lessons import (
    EVIDENCE_ROLE_VALIDATION_KINDS,
    PUBLISHED_RISK,
    PUBLISHED_SCOPE,
    PUBLISHED_SOURCE,
    validate_design_lesson_package,
)
from .hashing import file_sha256, stable_hash
from .migrations import discover_postgres_migrations
from .models import AssertionProposal, ScanEntry


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def _search_terms(value: Any) -> list[str]:
    """Collect exact lookup terms without making semantic judgments."""
    terms: set[str] = set()
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized:
            terms.add(normalized)
    elif isinstance(value, dict):
        for item in value.values():
            terms.update(_search_terms(item))
    elif isinstance(value, list):
        for item in value:
            terms.update(_search_terms(item))
    return sorted(terms)


def _design_lesson_search_fingerprint(
    *,
    organization_id: str,
    normalized_query: str,
    design_group_id: str | None,
    family_id: str | None,
) -> str:
    scope = json.dumps(
        {
            "organization_id": organization_id,
            "query": normalized_query,
            "design_group_id": design_group_id,
            "family_id": family_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def _opaque_design_lesson_ref(lesson_id: Any) -> str:
    digest = hashlib.sha256(str(lesson_id).encode("utf-8")).hexdigest()
    return f"design-lesson-{digest}"


def _encode_design_lesson_search_cursor(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")


def _decode_design_lesson_search_cursor(
    cursor: str,
    *,
    expected_fingerprint: str,
    expected_mode: str,
) -> dict[str, Any]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid design lesson search cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("invalid design lesson search cursor")
    if payload.get("fingerprint") != expected_fingerprint or payload.get("mode") != expected_mode:
        raise ValueError("design lesson search cursor does not match query or scope")
    required = {"approved_at", "key"}
    if expected_mode == "ranked":
        required.update({"exact_match", "text_rank", "trigram_similarity"})
    if any(key not in payload for key in required):
        raise ValueError("invalid design lesson search cursor")
    try:
        cursor_key = str(payload["key"])
        if len(cursor_key) != 64 or any(
            character not in "0123456789abcdef" for character in cursor_key
        ):
            raise ValueError("invalid cursor key")
        if expected_mode == "ranked":
            int(payload["exact_match"])
            float(payload["text_rank"])
            float(payload["trigram_similarity"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid design lesson search cursor") from exc
    return payload


class PostgresRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def status(self) -> dict[str, Any]:
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT current_database() AS database, current_setting('server_version') AS version, "
                    "EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS vector_enabled, "
                    "EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm') AS trgm_enabled"
                ).fetchone()
            return {"status": "healthy", **dict(row)}
        except Exception as exc:
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    def apply_migrations(self, root: Path) -> dict[str, list[str]]:
        applied: list[str] = []
        skipped: list[str] = []
        lock_name = "mechanical-design-agent:postgres-migrations:v1"
        with self.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s,0))", (lock_name,)
                )
            try:
                paths = discover_postgres_migrations(root)
                for path in paths:
                    version = int(path.name.split("_", 1)[0])
                    sql_bytes = path.read_bytes()
                    digest = hashlib.sha256(sql_bytes).hexdigest()
                    try:
                        sql = sql_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(f"migration is not UTF-8: {path.name}") from exc
                    with connection.transaction():
                        connection.execute(
                            "CREATE TABLE IF NOT EXISTS schema_migrations ("
                            "version integer PRIMARY KEY,filename text NOT NULL UNIQUE,"
                            "sha256 char(64) NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
                        )
                        current = connection.execute(
                            "SELECT filename,sha256 FROM schema_migrations WHERE version=%s", (version,)
                        ).fetchone()
                        if current:
                            if current["filename"] != path.name or current["sha256"] != digest:
                                raise ValueError(f"migration digest mismatch: {path.name}")
                            skipped.append(path.name)
                            continue
                        connection.execute(sql)
                        connection.execute(
                            "INSERT INTO schema_migrations(version,filename,sha256) VALUES (%s,%s,%s)",
                            (version, path.name, digest),
                        )
                        applied.append(path.name)
            finally:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_name,)
                    )
        return {"applied": applied, "skipped": skipped}

    def migration_state(self) -> dict[str, object]:
        with self.connection() as connection:
            ledger = connection.execute(
                "SELECT version,filename,sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
            extensions = connection.execute(
                "SELECT extname FROM pg_extension "
                "WHERE extname IN ('vector','pg_trgm','pgcrypto') ORDER BY extname"
            ).fetchall()
        return {
            "ledger": [dict(row) for row in ledger],
            "extensions": [str(row["extname"]) for row in extensions],
        }

    def initialize_bootstrap(self, config: dict[str, Any], actor_id: str) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            organization_name = config.get("organization_name") or config["organization_id"]
            connection.execute(
                "INSERT INTO organizations(id,name) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING",
                (config["organization_id"], organization_name),
            )
            connection.execute(
                "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name",
                (config["design_group_id"], config["organization_id"], config["design_group_name"]),
            )
            connection.execute(
                "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role",
                (actor_id, config["organization_id"], actor_id, "family_owner"),
            )
            existing = connection.execute(
                "SELECT canonical_name,aliases,status,config FROM product_families WHERE id=%s FOR UPDATE",
                (config["family_id"],),
            ).fetchone()
            aliases = config.get("aliases", [])
            changed = existing is None or any(
                (
                    existing["canonical_name"] != config["family_name"],
                    existing["aliases"] != aliases,
                    existing["status"] != config["status"],
                    existing["config"] != config,
                )
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,aliases,status,config) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)",
                    (
                        config["family_id"],
                        config["organization_id"],
                        config["design_group_id"],
                        config["family_name"],
                        json.dumps(aliases, ensure_ascii=False),
                        config["status"],
                        json.dumps(config, ensure_ascii=False),
                    ),
                )
            elif changed:
                connection.execute(
                    "UPDATE product_families SET canonical_name=%s,aliases=%s::jsonb,status=%s,config=%s::jsonb,"
                    "revision=revision+1,updated_at=now() WHERE id=%s",
                    (
                        config["family_name"],
                        json.dumps(aliases, ensure_ascii=False),
                        config["status"],
                        json.dumps(config, ensure_ascii=False),
                        config["family_id"],
                    ),
                )
            if changed:
                self._enqueue(
                    connection,
                    "product_family",
                    config["family_id"],
                    "product_family.upserted",
                    {"family_id": config["family_id"]},
                )
        return self.get_family(config["family_id"])

    def initialize_runtime_identity(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
    ) -> None:
        """Initialize the family-independent authority required by Design Jobs."""
        with self.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO organizations(id,name) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING",
                (organization_id, organization_id),
            )
            connection.execute(
                "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s) "
                "ON CONFLICT(id) DO NOTHING",
                (design_group_id, organization_id, design_group_id),
            )
            connection.execute(
                "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(id) DO NOTHING",
                (actor_id, organization_id, actor_id, "design_owner"),
            )
            group = connection.execute(
                "SELECT organization_id FROM design_groups WHERE id=%s",
                (design_group_id,),
            ).fetchone()
            actor = connection.execute(
                "SELECT organization_id FROM actors WHERE id=%s",
                (actor_id,),
            ).fetchone()
            if group is None or str(group["organization_id"]) != organization_id:
                raise ValueError("configured design group belongs to another organization")
            if actor is None or str(actor["organization_id"]) != organization_id:
                raise ValueError("configured actor belongs to another organization")

    def get_family(self, family_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM product_families WHERE id=%s", (family_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown family_id: {family_id}")
        return dict(row)

    def get_design_group(self, design_group_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM design_groups WHERE id=%s", (design_group_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown design_group_id: {design_group_id}")
        return dict(row)

    def upsert_design_group(
        self, organization_id: str, design_group_id: str, name: str
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name RETURNING *",
                (design_group_id, organization_id, name),
            ).fetchone()
        return dict(row)

    def create_family(self, config: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,aliases,status,config) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb) RETURNING *",
                (
                    config["family_id"],
                    config["organization_id"],
                    config["design_group_id"],
                    config["family_name"],
                    json.dumps(config.get("aliases", []), ensure_ascii=False),
                    config["status"],
                    json.dumps(config, ensure_ascii=False),
                ),
            ).fetchone()
            self._enqueue(
                connection,
                "product_family",
                config["family_id"],
                "product_family.created",
                {"family_id": config["family_id"]},
            )
        return dict(row)

    def update_family_config(self, family_id: str, config: dict[str, Any]) -> dict[str, Any]:
        if config.get("family_id") != family_id:
            raise ValueError("family_id cannot be changed")
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE product_families SET canonical_name=%s,aliases=%s::jsonb,status=%s,config=%s::jsonb,"
                "revision=revision+1,updated_at=now() WHERE id=%s RETURNING *",
                (
                    config["family_name"],
                    json.dumps(config.get("aliases", []), ensure_ascii=False),
                    config["status"],
                    json.dumps(config, ensure_ascii=False),
                    family_id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown family_id: {family_id}")
            self._enqueue(connection, "product_family", family_id, "product_family.updated", {"family_id": family_id})
        return dict(row)

    def register_library(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        root_path: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO library_registrations(organization_id,design_group_id,root_path,registered_by) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(root_path) DO UPDATE SET read_only=true RETURNING *",
                (organization_id, design_group_id, root_path, actor_id),
            ).fetchone()
        return dict(row)

    def register_evidence_artifact(
        self, organization_id: str, artifact: dict[str, Any], media_type: str
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO artifacts(organization_id,sha256,size_bytes,media_type,storage_path,source_path) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(organization_id,sha256) DO UPDATE SET "
                "media_type=EXCLUDED.media_type,source_path=EXCLUDED.source_path RETURNING *",
                (
                    organization_id,
                    artifact["sha256"],
                    artifact["size_bytes"],
                    media_type,
                    artifact["storage_path"],
                    artifact["source_path"],
                ),
            ).fetchone()
        return dict(row)

    def get_library(self, library_id: str | None = None) -> dict[str, Any]:
        with self.connection() as connection:
            if library_id:
                row = connection.execute("SELECT * FROM library_registrations WHERE id=%s", (library_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM library_registrations ORDER BY registered_at DESC LIMIT 1"
                ).fetchone()
        if row is None:
            raise KeyError("no CAD library has been registered")
        return dict(row)

    def list_library_files(self, library_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT relative_path,absolute_path,family_folder,sha256,size_bytes,modified_at_ns,suffix,"
                "ingestion_status,model_revision_id,missing_at FROM library_files WHERE library_id=%s",
                (library_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_scan(self, library_id: str, entries: list[ScanEntry]) -> None:
        present = {entry.relative_path for entry in entries}
        with self.connection() as connection, connection.transaction():
            existing_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM library_files WHERE library_id=%s", (library_id,)
                ).fetchall()
            ]
            existing = {row["relative_path"]: row for row in existing_rows}
            by_sha: dict[str, list[dict[str, Any]]] = {}
            for row in existing_rows:
                by_sha.setdefault(row["sha256"], []).append(row)
            seen_current_hashes: set[str] = set()
            renamed_previous_paths: set[str] = set()
            for entry in entries:
                old = existing.get(entry.relative_path)
                if old is None:
                    prior_same = sorted(by_sha.get(entry.sha256, []), key=lambda item: item["relative_path"])
                    rename_source = next(
                        (item for item in prior_same if item["relative_path"] not in present), None
                    )
                    if rename_source is not None:
                        cross_family = rename_source["family_folder"] != entry.family_folder
                        status = "family_assignment_conflict" if cross_family else rename_source["ingestion_status"]
                        connection.execute(
                            "UPDATE library_files SET relative_path=%s,absolute_path=%s,family_folder=%s,size_bytes=%s,"
                            "modified_at_ns=%s,suffix=%s,ingestion_status=%s,last_seen_at=now(),missing_at=NULL "
                            "WHERE id=%s",
                            (
                                entry.relative_path,
                                entry.absolute_path,
                                entry.family_folder,
                                entry.size_bytes,
                                entry.modified_at_ns,
                                entry.suffix,
                                status,
                                rename_source["id"],
                            ),
                        )
                        if rename_source.get("model_revision_id"):
                            connection.execute(
                                "UPDATE model_revisions SET source_relative_path=%s,family_folder=%s WHERE id=%s",
                                (
                                    entry.relative_path,
                                    entry.family_folder,
                                    rename_source["model_revision_id"],
                                ),
                            )
                        connection.execute(
                            "INSERT INTO family_folder_mappings(library_id,folder_name,status) "
                            "VALUES (%s,%s,'pending_confirmation') ON CONFLICT(library_id,folder_name) DO NOTHING",
                            (library_id, entry.family_folder),
                        )
                        renamed_previous_paths.add(rename_source["relative_path"])
                        connection.execute(
                            "INSERT INTO library_file_events(library_id,event_kind,relative_path,previous_path,"
                            "previous_sha256,current_sha256,details) VALUES (%s,'renamed',%s,%s,%s,%s,%s::jsonb)",
                            (
                                library_id,
                                entry.relative_path,
                                rename_source["relative_path"],
                                entry.sha256,
                                entry.sha256,
                                json.dumps({"cross_family_folder": cross_family}, ensure_ascii=False),
                            ),
                        )
                        seen_current_hashes.add(entry.sha256)
                        continue
                    known = next((item for item in prior_same if item.get("model_revision_id")), None)
                    if known is not None:
                        cross_family = known["family_folder"] != entry.family_folder
                        status = "family_assignment_conflict" if cross_family else "duplicate_known"
                        inherited_revision = None if cross_family else known["model_revision_id"]
                    elif entry.sha256 in seen_current_hashes:
                        status = "duplicate_waiting"
                        inherited_revision = None
                    else:
                        status = "pending_new"
                        inherited_revision = None
                elif old["sha256"] != entry.sha256:
                    status = "pending_modified"
                    inherited_revision = old.get("model_revision_id")
                elif str(old["ingestion_status"]).startswith("pending"):
                    status = old["ingestion_status"]
                    inherited_revision = old.get("model_revision_id")
                elif old["ingestion_status"] in {
                    "duplicate_known",
                    "duplicate_waiting",
                    "family_assignment_conflict",
                }:
                    status = old["ingestion_status"]
                    inherited_revision = old.get("model_revision_id")
                else:
                    status = "ingested"
                    inherited_revision = old.get("model_revision_id")
                connection.execute(
                    "INSERT INTO library_files(library_id,relative_path,absolute_path,family_folder,sha256,size_bytes,"
                    "modified_at_ns,suffix,ingestion_status,model_revision_id,last_seen_at,missing_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),NULL) "
                    "ON CONFLICT(library_id,relative_path) DO UPDATE SET absolute_path=EXCLUDED.absolute_path,"
                    "family_folder=EXCLUDED.family_folder,sha256=EXCLUDED.sha256,size_bytes=EXCLUDED.size_bytes,"
                    "modified_at_ns=EXCLUDED.modified_at_ns,suffix=EXCLUDED.suffix,"
                    "ingestion_status=EXCLUDED.ingestion_status,model_revision_id=EXCLUDED.model_revision_id,"
                    "last_seen_at=now(),missing_at=NULL",
                    (
                        library_id,
                        entry.relative_path,
                        entry.absolute_path,
                        entry.family_folder,
                        entry.sha256,
                        entry.size_bytes,
                        entry.modified_at_ns,
                        entry.suffix,
                        status,
                        inherited_revision,
                    ),
                )
                if old is None:
                    connection.execute(
                        "INSERT INTO library_file_events(library_id,event_kind,relative_path,current_sha256,details) "
                        "VALUES (%s,%s,%s,%s,%s::jsonb)",
                        (
                            library_id,
                            "duplicate" if status.startswith("duplicate") else "new",
                            entry.relative_path,
                            entry.sha256,
                            json.dumps({"ingestion_status": status}, ensure_ascii=False),
                        ),
                    )
                elif old["sha256"] != entry.sha256:
                    connection.execute(
                        "INSERT INTO library_file_events(library_id,event_kind,relative_path,previous_sha256,current_sha256) "
                        "VALUES (%s,'modified',%s,%s,%s)",
                        (library_id, entry.relative_path, old["sha256"], entry.sha256),
                    )
                connection.execute(
                    "INSERT INTO family_folder_mappings(library_id,folder_name,status) VALUES (%s,%s,'pending_confirmation') "
                    "ON CONFLICT(library_id,folder_name) DO NOTHING",
                    (library_id, entry.family_folder),
                )
                seen_current_hashes.add(entry.sha256)
            if present:
                connection.execute(
                    "UPDATE library_files SET ingestion_status='missing',missing_at=now() "
                    "WHERE library_id=%s AND NOT (relative_path = ANY(%s)) AND missing_at IS NULL",
                    (library_id, sorted(present)),
                )
            else:
                connection.execute(
                    "UPDATE library_files SET ingestion_status='missing',missing_at=now() "
                    "WHERE library_id=%s AND missing_at IS NULL",
                    (library_id,),
                )
            for path, old in existing.items():
                if path in present or path in renamed_previous_paths or old.get("missing_at") is not None:
                    continue
                connection.execute(
                    "INSERT INTO library_file_events(library_id,event_kind,relative_path,previous_sha256) "
                    "VALUES (%s,'missing',%s,%s)",
                    (library_id, path, old["sha256"]),
                )

    def pending_library_files(self, library_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM library_files WHERE library_id=%s AND ingestion_status LIKE 'pending%%' "
                "ORDER BY relative_path",
                (library_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def family_assignment_conflicts(self, library_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM library_files WHERE library_id=%s AND ingestion_status='family_assignment_conflict' "
                "ORDER BY relative_path",
                (library_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def folder_mappings(self, library_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM family_folder_mappings WHERE library_id=%s ORDER BY folder_name",
                (library_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_folder_mapping(
        self, library_id: str, folder_name: str, family_id: str, actor_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            compatible = connection.execute(
                "SELECT 1 FROM library_registrations l JOIN product_families f "
                "ON f.organization_id=l.organization_id "
                "WHERE l.id=%s AND f.id=%s",
                (library_id, family_id),
            ).fetchone()
            if compatible is None:
                raise ValueError("family must belong to the registered library's organization")
            row = connection.execute(
                "UPDATE family_folder_mappings SET family_id=%s,status='confirmed',confirmed_by=%s,confirmed_at=now() "
                "WHERE library_id=%s AND folder_name=%s RETURNING *",
                (family_id, actor_id, library_id, folder_name),
            ).fetchone()
            if row is None:
                raise KeyError(f"folder mapping not found: {folder_name}")
        return dict(row)

    def family_for_folder(self, library_id: str, folder_name: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT family_id FROM family_folder_mappings WHERE library_id=%s AND folder_name=%s "
                "AND status='confirmed'",
                (library_id, folder_name),
            ).fetchone()
        return str(row["family_id"]) if row and row["family_id"] else None

    def create_job(self, library_id: str, selection: list[str]) -> str:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO ingestion_jobs(library_id,status,selection) VALUES (%s,'queued',%s::jsonb) RETURNING id",
                (library_id, json.dumps(selection, ensure_ascii=False)),
            ).fetchone()
        return str(row["id"])

    def update_job(
        self, job_id: str, status: str, *, result: dict[str, Any] | None = None, error: str = ""
    ) -> None:
        timestamp_field = "started_at=now()," if status == "running" else "completed_at=now()," if status in {"completed", "failed"} else ""
        query = f"UPDATE ingestion_jobs SET status=%s,{timestamp_field} result=%s::jsonb,error=%s WHERE id=%s"
        with self.connection() as connection, connection.transaction():
            connection.execute(
                query,
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error or None, job_id),
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM ingestion_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job_id: {job_id}")
        return dict(row)

    def save_model_analysis(
        self,
        *,
        library_id: str,
        file_record: dict[str, Any],
        artifact: dict[str, Any],
        manifest: dict[str, Any],
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
    ) -> str:
        with self.connection() as connection, connection.transaction():
            revision_number = 1
            if file_record.get("model_revision_id"):
                previous = connection.execute(
                    "SELECT revision_number FROM model_revisions WHERE id=%s",
                    (file_record["model_revision_id"],),
                ).fetchone()
                if previous:
                    revision_number = int(previous["revision_number"]) + 1
            artifact_row = connection.execute(
                "INSERT INTO artifacts(organization_id,sha256,size_bytes,media_type,storage_path,source_path) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(organization_id,sha256) DO UPDATE SET "
                "source_path=EXCLUDED.source_path RETURNING id",
                (
                    organization_id,
                    artifact["sha256"],
                    artifact["size_bytes"],
                    "application/step" if str(file_record["suffix"]).lower() in {".step", ".stp"} else "application/x-freecad",
                    artifact["storage_path"],
                    artifact["source_path"],
                ),
            ).fetchone()
            model_row = connection.execute(
                "INSERT INTO model_revisions(organization_id,design_group_id,family_id,source_artifact_id,"
                "source_relative_path,family_folder,revision_number,previous_revision_id,parser_version,status,manifest,geometry_vector,structure_vector) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'analyzed',%s::jsonb,%s::vector,%s::vector) "
                "ON CONFLICT(source_artifact_id,parser_version) DO UPDATE SET source_relative_path=EXCLUDED.source_relative_path,"
                "family_folder=EXCLUDED.family_folder RETURNING id",
                (
                    organization_id,
                    design_group_id,
                    family_id,
                    artifact_row["id"],
                    file_record["relative_path"],
                    file_record["family_folder"],
                    revision_number,
                    file_record.get("model_revision_id"),
                    manifest["parser_version"],
                    json.dumps(manifest, ensure_ascii=False),
                    _vector_literal(manifest["geometry_vector"]),
                    _vector_literal(manifest["structure_vector"]),
                ),
            ).fetchone()
            model_id = str(model_row["id"])
            connection.execute("DELETE FROM source_nodes WHERE model_revision_id=%s", (model_id,))
            for node in manifest.get("source_nodes", []):
                connection.execute(
                    "INSERT INTO source_nodes(model_revision_id,source_id,parent_source_id,node_kind,source_name,"
                    "source_label,payload) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        model_id,
                        node["source_id"],
                        node.get("primary_parent_source_id"),
                        node["node_kind"],
                        node["source_name"],
                        node["source_label"],
                        json.dumps(node, ensure_ascii=False),
                    ),
                )
            connection.execute("DELETE FROM structure_hypotheses WHERE model_revision_id=%s", (model_id,))
            for item in manifest.get("structure_hypotheses", []):
                connection.execute(
                    "INSERT INTO structure_hypotheses(model_revision_id,hypothesis_kind,subject_source_id,"
                    "object_source_id,confidence,status,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        model_id,
                        item["kind"],
                        item["subject_source_id"],
                        item.get("object_source_id"),
                        item["confidence"],
                        item["status"],
                        json.dumps(item.get("evidence", []), ensure_ascii=False),
                    ),
                )
            connection.execute(
                "UPDATE library_files SET ingestion_status='ingested',model_revision_id=%s WHERE library_id=%s AND relative_path=%s",
                (model_id, library_id, file_record["relative_path"]),
            )
            connection.execute(
                "UPDATE library_files SET ingestion_status='duplicate_known',model_revision_id=%s "
                "WHERE library_id=%s AND sha256=%s AND ingestion_status='duplicate_waiting'",
                (model_id, library_id, file_record["sha256"]),
            )
            self._enqueue(
                connection,
                "model_revision",
                model_id,
                "model_revision.analyzed",
                {"model_revision_id": model_id, "family_id": family_id},
            )
        return model_id

    def get_model_analysis(self, model_revision_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,organization_id,design_group_id,family_id,product_id,source_relative_path,family_folder,"
                "parser_version,status,manifest,created_at FROM model_revisions WHERE id=%s",
                (model_revision_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown model_revision_id: {model_revision_id}")
        return dict(row)

    def create_session(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        actor_id: str,
    ) -> str:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO interaction_sessions(organization_id,design_group_id,family_id,model_revision_id,created_by) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (organization_id, design_group_id, family_id, model_revision_id, actor_id),
            ).fetchone()
        return str(row["id"])

    def session(self, session_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM interaction_sessions WHERE id=%s", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown interaction session: {session_id}")
        return dict(row)

    def replace_open_questions(self, session_id: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.connection() as connection, connection.transaction():
            connection.execute("UPDATE question_items SET status='superseded' WHERE session_id=%s AND status='open'", (session_id,))
            created = []
            for item in questions:
                row = connection.execute(
                    "INSERT INTO question_items(session_id,question_kind,prompt_intent,target_refs,evidence,score) "
                    "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING *",
                    (
                        session_id,
                        item["question_kind"],
                        item["prompt_intent"],
                        json.dumps(item["target_refs"], ensure_ascii=False),
                        json.dumps(item["evidence"], ensure_ascii=False),
                        item["score"],
                    ),
                ).fetchone()
                created.append(dict(row))
        return created

    def open_questions(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM question_items WHERE session_id=%s AND status='open' ORDER BY score DESC,created_at LIMIT %s",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_question_signatures(self, session_id: str) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT question_kind,target_refs FROM question_items WHERE session_id=%s "
                "AND status IN ('answered','deferred','superseded')",
                (session_id,),
            ).fetchall()
        return {
            stable_hash([row["question_kind"], sorted(row["target_refs"])])
            for row in rows
        }

    def defer_questions(
        self, session_id: str, question_ids: list[str], reason: str
    ) -> list[dict[str, Any]]:
        if not question_ids:
            raise ValueError("at least one question id is required")
        with self.connection() as connection, connection.transaction():
            rows = connection.execute(
                "UPDATE question_items SET status='deferred',deferred_reason=%s "
                "WHERE session_id=%s AND id = ANY(%s::uuid[]) AND status='open' RETURNING *",
                (reason, session_id, question_ids),
            ).fetchall()
        if len(rows) != len(set(question_ids)):
            raise ValueError("one or more questions were not open in this session")
        return [dict(row) for row in rows]

    def record_exchange(
        self,
        *,
        session_id: str,
        question_ids: list[str],
        engineer_text: str,
        agent_interpretation: dict[str, Any],
        actor_id: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO answer_events(session_id,question_ids,engineer_text,agent_interpretation,actor_id,content_sha256) "
                "VALUES (%s,%s::jsonb,%s,%s::jsonb,%s,%s) ON CONFLICT(session_id,content_sha256) "
                "DO UPDATE SET engineer_text=EXCLUDED.engineer_text RETURNING *",
                (
                    session_id,
                    json.dumps(question_ids),
                    engineer_text,
                    json.dumps(agent_interpretation, ensure_ascii=False),
                    actor_id,
                    content_sha256,
                ),
            ).fetchone()
            if question_ids:
                connection.execute(
                    "UPDATE question_items SET status='answered' WHERE session_id=%s AND id = ANY(%s::uuid[])",
                    (session_id, question_ids),
                )
        return dict(row)

    def propose_assertions(
        self,
        *,
        session_id: str,
        proposals: list[AssertionProposal],
        actor_id: str,
    ) -> list[dict[str, Any]]:
        session = self.session(session_id)
        product_id = None
        if session.get("model_revision_id"):
            with self.connection() as lookup:
                model = lookup.execute(
                    "SELECT product_id FROM model_revisions WHERE id=%s",
                    (session["model_revision_id"],),
                ).fetchone()
                product_id = model["product_id"] if model else None
        created = []
        with self.connection() as connection, connection.transaction():
            for proposal in proposals:
                proposal.validate()
                if proposal.scope_kind == "model" and not session.get("model_revision_id"):
                    raise ValueError("model-scoped assertion requires a model-attached session")
                if proposal.scope_kind == "product" and not product_id:
                    raise ValueError("product-scoped assertion requires an engineer-confirmed model identity")
                if proposal.scope_kind in {"family", "design_group"} and not session.get("family_id"):
                    raise ValueError("family/design-group assertions require an engineer-confirmed family")
                if proposal.risk_level != "R0":
                    answer_ids = sorted(
                        {
                            str(item["answer_event_id"])
                            for item in proposal.evidence
                            if isinstance(item, dict) and item.get("answer_event_id")
                        }
                    )
                    if not answer_ids:
                        raise ValueError("R1-R3 assertions require evidence with an answer_event_id")
                    answer_count = connection.execute(
                        "SELECT count(*) AS count FROM answer_events WHERE session_id=%s AND id = ANY(%s::uuid[])",
                        (session_id, answer_ids),
                    ).fetchone()["count"]
                    if int(answer_count) != len(answer_ids):
                        raise ValueError("assertion answer evidence must belong to the same interaction session")
                row = connection.execute(
                    "INSERT INTO knowledge_assertions(organization_id,design_group_id,family_id,product_id,model_revision_id,"
                    "interaction_session_id,subject_ref,predicate,object_value,unit,scope_kind,risk_level,status,"
                    "source_kind,evidence,confidence,applicability,non_applicable_conditions,contradicts,supersedes,created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) "
                    "RETURNING *",
                    (
                        session["organization_id"],
                        session["design_group_id"],
                        session.get("family_id"),
                        product_id,
                        session.get("model_revision_id"),
                        session_id,
                        proposal.subject_ref,
                        proposal.predicate,
                        json.dumps(proposal.object_value, ensure_ascii=False),
                        proposal.unit or None,
                        proposal.scope_kind,
                        proposal.risk_level,
                        proposal.status,
                        proposal.source_kind,
                        json.dumps(proposal.evidence, ensure_ascii=False),
                        proposal.confidence,
                        json.dumps(proposal.applicability, ensure_ascii=False),
                        json.dumps(proposal.non_applicable_conditions, ensure_ascii=False),
                        json.dumps(proposal.contradicts, ensure_ascii=False),
                        proposal.supersedes or None,
                        actor_id,
                    ),
                ).fetchone()
                created.append(dict(row))
        return created

    def review_assertion(
        self,
        assertion_id: str,
        decision: str,
        reviewer_id: str,
        reviewer_text: str,
        corrected_object_value: Any | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "modify", "reject", "supersede"}:
            raise ValueError("decision must be approve, modify, reject, or supersede")
        with self.connection() as connection, connection.transaction():
            assertion = connection.execute(
                "SELECT * FROM knowledge_assertions WHERE id=%s FOR UPDATE", (assertion_id,)
            ).fetchone()
            if assertion is None:
                raise KeyError(f"unknown assertion_id: {assertion_id}")
            if assertion["status"] in {"approved", "rejected", "superseded"}:
                raise ValueError("terminal assertion revisions cannot be reviewed again")
            if assertion["risk_level"] in {"R2", "R3"} or assertion["scope_kind"] in {"family", "design_group", "organization_general"}:
                actor = connection.execute("SELECT role FROM actors WHERE id=%s", (reviewer_id,)).fetchone()
                if actor is None or actor["role"] != "family_owner":
                    raise PermissionError("R2/R3 and promoted-scope knowledge requires family_owner review")
            status = {
                "approve": "approved",
                "modify": "engineer_confirmed",
                "reject": "rejected",
                "supersede": "superseded",
            }[decision]
            predecessor = None
            if decision == "approve":
                predecessor_id = assertion.get("supersedes")
                if predecessor_id:
                    predecessor = connection.execute(
                        "SELECT * FROM knowledge_assertions WHERE id=%s FOR UPDATE",
                        (predecessor_id,),
                    ).fetchone()
                    if predecessor is None or predecessor["status"] != "approved":
                        raise ValueError("supersedes must reference an approved predecessor assertion")
                    comparable = (
                        "organization_id",
                        "design_group_id",
                        "family_id",
                        "product_id",
                        "model_revision_id",
                        "subject_ref",
                        "predicate",
                        "scope_kind",
                    )
                    if any(predecessor.get(key) != assertion.get(key) for key in comparable):
                        raise ValueError("a replacement assertion must preserve the predecessor subject and scope")
                contradiction_ids = {
                    str(value) for value in (assertion.get("contradicts") or [])
                }
                if predecessor_id:
                    contradiction_ids.discard(str(predecessor_id))
                if contradiction_ids:
                    unresolved = connection.execute(
                        "SELECT id FROM knowledge_assertions WHERE id = ANY(%s::uuid[]) AND status='approved'",
                        (sorted(contradiction_ids),),
                    ).fetchall()
                    if unresolved:
                        raise ValueError(
                            "approval blocked by unresolved approved contradictions: "
                            + ", ".join(str(row["id"]) for row in unresolved)
                        )
            object_value = assertion["object_value"] if corrected_object_value is None else corrected_object_value
            updated = connection.execute(
                "UPDATE knowledge_assertions SET status=%s,object_value=%s::jsonb,revision=revision+1,updated_at=now() "
                "WHERE id=%s RETURNING *",
                (status, json.dumps(object_value, ensure_ascii=False), assertion_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO review_events(assertion_id,decision,reviewer_id,reviewer_text,previous_status,resulting_status) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (assertion_id, decision, reviewer_id, reviewer_text, assertion["status"], status),
            )
            if predecessor is not None:
                connection.execute(
                    "UPDATE knowledge_assertions SET status='superseded',revision=revision+1,updated_at=now() "
                    "WHERE id=%s",
                    (predecessor["id"],),
                )
                connection.execute(
                    "DELETE FROM knowledge_search_documents WHERE assertion_id=%s",
                    (predecessor["id"],),
                )
                connection.execute(
                    "INSERT INTO review_events(assertion_id,decision,reviewer_id,reviewer_text,previous_status,resulting_status) "
                    "VALUES (%s,'superseded-by-revision',%s,%s,'approved','superseded')",
                    (predecessor["id"], reviewer_id, f"Superseded by approved assertion {assertion_id}"),
                )
                self._enqueue(
                    connection,
                    "knowledge_assertion",
                    str(predecessor["id"]),
                    "knowledge_assertion.reviewed",
                    {"assertion_id": str(predecessor["id"]), "status": "superseded"},
                )
            connection.execute("DELETE FROM knowledge_search_documents WHERE assertion_id=%s", (assertion_id,))
            if status == "approved":
                exact_terms = sorted(
                    set(_search_terms(object_value))
                    | {str(assertion["subject_ref"]).strip().lower(), str(assertion["predicate"]).strip().lower()}
                )
                search_text = " ".join(
                    [
                        str(assertion["subject_ref"]),
                        str(assertion["predicate"]),
                        json.dumps(object_value, ensure_ascii=False, sort_keys=True),
                    ]
                )
                connection.execute(
                    "INSERT INTO knowledge_search_documents(assertion_id,organization_id,design_group_id,family_id,"
                    "exact_terms,search_text) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        assertion_id,
                        assertion["organization_id"],
                        assertion["design_group_id"],
                        assertion.get("family_id"),
                        exact_terms,
                        search_text,
                    ),
                )
            self._enqueue(
                connection,
                "knowledge_assertion",
                assertion_id,
                "knowledge_assertion.reviewed",
                {"assertion_id": assertion_id, "status": status},
            )
        return dict(updated)

    def get_assertion(self, assertion_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM knowledge_assertions WHERE id=%s", (assertion_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown assertion_id: {assertion_id}")
        return dict(row)

    def confirm_model_identity(
        self,
        *,
        model_revision_id: str,
        family_id: str,
        canonical_name: str,
        aliases: list[str],
        approved_assertion_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            model = connection.execute(
                "SELECT * FROM model_revisions WHERE id=%s FOR UPDATE", (model_revision_id,)
            ).fetchone()
            if model is None:
                raise KeyError(f"unknown model_revision_id: {model_revision_id}")
            family = connection.execute("SELECT * FROM product_families WHERE id=%s", (family_id,)).fetchone()
            if family is None:
                raise KeyError(f"unknown family_id: {family_id}")
            if family["organization_id"] != model["organization_id"] or family["design_group_id"] != model["design_group_id"]:
                raise ValueError("family and model organization/design-group scopes do not match")
            if model["family_id"] and model["family_id"] != family_id:
                raise ValueError("model already has a different engineer-confirmed family")
            assertion = connection.execute(
                "SELECT * FROM knowledge_assertions WHERE id=%s AND status='approved'",
                (approved_assertion_id,),
            ).fetchone()
            if assertion is None or str(assertion.get("model_revision_id") or "") != model_revision_id:
                raise ValueError("identity assignment requires an approved assertion for this model revision")
            if model["product_id"]:
                raise ValueError("model identity is already confirmed; create an assertion revision before changing it")
            product = connection.execute(
                "INSERT INTO products(organization_id,design_group_id,family_id,canonical_name,aliases,"
                "identity_assertion_id,status) VALUES (%s,%s,%s,%s,%s::jsonb,%s,'engineer_confirmed') RETURNING *",
                (
                    model["organization_id"],
                    model["design_group_id"],
                    family_id,
                    canonical_name,
                    json.dumps(aliases, ensure_ascii=False),
                    approved_assertion_id,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE model_revisions SET product_id=%s,family_id=%s WHERE id=%s",
                (product["id"], family_id, model_revision_id),
            )
            self._enqueue(
                connection,
                "product",
                str(product["id"]),
                "model_identity.confirmed",
                {"product_id": str(product["id"]), "model_revision_id": model_revision_id},
            )
        return dict(product)

    def approved_assertions(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        include_design_lessons: bool = True,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.* FROM knowledge_assertions a "
                "LEFT JOIN model_revisions target_model ON target_model.id=%s "
                "WHERE a.organization_id=%s AND a.status='approved' "
                "AND (%s OR a.source_kind<>'approved_design_lesson') AND ("
                "scope_kind='organization_general' OR "
                "(scope_kind='model' AND a.model_revision_id=%s) OR "
                "(scope_kind='product' AND target_model.product_id IS NOT NULL AND a.product_id=target_model.product_id) OR "
                "(scope_kind='family' AND a.family_id=%s AND a.design_group_id=%s) OR "
                "(scope_kind='design_group' AND a.family_id=%s AND a.design_group_id=%s)) "
                "ORDER BY risk_level DESC,a.created_at",
                (
                    model_revision_id,
                    organization_id,
                    include_design_lessons,
                    model_revision_id,
                    family_id,
                    design_group_id,
                    family_id,
                    design_group_id,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def excluded_specialized_count(
        self, organization_id: str, design_group_id: str, family_id: str | None
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT family_id,scope_kind,count(*) AS count FROM knowledge_assertions "
                "WHERE organization_id=%s AND status='approved' AND scope_kind IN ('family','design_group','product') "
                "AND NOT (design_group_id=%s AND family_id IS NOT DISTINCT FROM %s) GROUP BY family_id,scope_kind",
                (organization_id, design_group_id, family_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_approved_knowledge(
        self,
        *,
        query: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized = query.strip().lower()
        if not normalized:
            raise ValueError("knowledge search query is required")
        if not 1 <= limit <= 50:
            raise ValueError("knowledge search limit must be between 1 and 50")
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.*,"
                "CASE WHEN %s = ANY(d.exact_terms) THEN 1 ELSE 0 END AS exact_match,"
                "ts_rank(d.search_vector,plainto_tsquery('simple',%s)) AS text_rank,"
                "similarity(d.search_text,%s) AS trigram_similarity "
                "FROM knowledge_search_documents d JOIN knowledge_assertions a ON a.id=d.assertion_id "
                "LEFT JOIN model_revisions target_model ON target_model.id=%s "
                "WHERE a.organization_id=%s AND a.status='approved' AND ("
                "a.scope_kind='organization_general' OR "
                "(a.scope_kind='model' AND a.model_revision_id=%s) OR "
                "(a.scope_kind='product' AND target_model.product_id IS NOT NULL AND a.product_id=target_model.product_id) OR "
                "(a.scope_kind='family' AND a.family_id=%s AND a.design_group_id=%s) OR "
                "(a.scope_kind='design_group' AND a.family_id=%s AND a.design_group_id=%s)) AND ("
                "%s = ANY(d.exact_terms) OR d.search_vector @@ plainto_tsquery('simple',%s) OR "
                "similarity(d.search_text,%s) >= 0.12) "
                "ORDER BY exact_match DESC,text_rank DESC,trigram_similarity DESC,a.created_at DESC LIMIT %s",
                (
                    normalized,
                    query,
                    query,
                    model_revision_id,
                    organization_id,
                    model_revision_id,
                    family_id,
                    design_group_id,
                    family_id,
                    design_group_id,
                    normalized,
                    query,
                    query,
                    limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_design_lesson_approval(
        self,
        *,
        package_sha256: str,
        reviewer_id: str,
        organization_id: str,
        supersedes_lesson_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Authorize the actor, then return an exact prior approval without mutable rechecks."""
        if len(package_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in package_sha256.lower()
        ):
            raise ValueError("package_sha256 must be a full SHA-256 digest")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            if (
                actor is None
                or actor["role"] != "family_owner"
                or actor["organization_id"] != organization_id
            ):
                raise PermissionError(
                    "design lesson approval requires a family_owner in the configured organization"
                )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"design-lesson-package:{package_sha256}",),
            )
            existing = connection.execute(
                "SELECT * FROM design_lesson_events WHERE package_sha256=%s",
                (package_sha256,),
            ).fetchone()
            if existing is None:
                return None
            expected_predecessor = str(supersedes_lesson_id or "")
            actual_predecessor = str(existing.get("supersedes") or "")
            if (
                existing["organization_id"] != organization_id
                or actual_predecessor != expected_predecessor
            ):
                raise ValueError(
                    "package digest is already bound to a different organization or lifecycle operation"
                )
            return self._design_lesson_record(connection, existing)

    def approve_design_lesson(
        self,
        *,
        package: dict[str, Any],
        package_sha256: str,
        archived_package_path: str,
        archived_evidence: list[dict[str, Any]] | None = None,
        working_copy_artifact: dict[str, Any] | None = None,
        reviewer_id: str,
        reviewer_text: str,
        supersedes_lesson_id: str | None = None,
        working_copy_sha256_reader: Callable[[str], str] | None = None,
        review_id: str | None = None,
        pre_commit_verifier: Callable[[], None] | None = None,
        verified_review_card_sha256: str | None = None,
        verified_review_path: str | None = None,
        verified_package_path: str | None = None,
        confirmation_mode: str = "legacy_review_id",
        decision_receipt_sha256: str | None = None,
        decision_receipt_path: str | None = None,
    ) -> dict[str, Any]:
        """Atomically promote one verified staging package into authoritative knowledge."""
        package = validate_design_lesson_package(package)
        if len(package_sha256) != 64 or any(character not in "0123456789abcdef" for character in package_sha256.lower()):
            raise ValueError("package_sha256 must be a full SHA-256 digest")
        if not archived_package_path.strip():
            raise ValueError("archived_package_path is required")
        if not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        if confirmation_mode not in {"legacy_review_id", "single_confirmation"}:
            raise ValueError("unsupported design lesson confirmation mode")
        if confirmation_mode == "single_confirmation" and (
            not decision_receipt_sha256 or not decision_receipt_path
        ):
            raise ValueError("single-confirmation approval requires a decision receipt")
        if decision_receipt_sha256 is not None and (
            len(decision_receipt_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in decision_receipt_sha256.lower()
            )
        ):
            raise ValueError("decision_receipt_sha256 must be a full SHA-256 digest")
        source = package["source"]
        required_source_fields = (
            "organization_id",
            "design_group_id",
            "working_copy_id",
            "before_model_sha256",
            "after_model_sha256",
            "change_set_ids",
        )
        missing = [field for field in required_source_fields if not source.get(field)]
        if missing:
            raise ValueError(f"design lesson source missing required fields: {', '.join(missing)}")
        for field in ("before_model_sha256", "after_model_sha256"):
            digest = str(source[field])
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ValueError(f"source {field} must be a full SHA-256 digest")
        change_set_ids = [str(value) for value in source["change_set_ids"]]
        if not change_set_ids or len(change_set_ids) != len(set(change_set_ids)):
            raise ValueError("source change_set_ids must be a nonempty unique list")
        evidence_ids = {item["evidence_id"] for item in package["evidence_manifest"]}
        for assertion in package["atomic_assertions"]:
            missing_evidence = set(assertion["evidence_refs"]) - evidence_ids
            if missing_evidence:
                raise ValueError(
                    "atomic assertion evidence_refs are missing evidence_id values from evidence manifest: "
                    + ", ".join(sorted(missing_evidence))
                )

        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("design lesson approval requires a family_owner")
            if actor["organization_id"] != source["organization_id"]:
                raise PermissionError("family_owner must belong to the source organization")

            review = None
            if review_id is not None:
                review = connection.execute(
                    "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                    (review_id,),
                ).fetchone()
                if review is None:
                    raise KeyError(f"unknown design lesson review: {review_id}")
                if review["status"] != "awaiting-engineer-review":
                    raise ValueError(
                        "design lesson review must be awaiting-engineer-review"
                    )
                if actor["organization_id"] != review["organization_id"]:
                    raise PermissionError(
                        "design lesson review actor must belong to the review organization"
                    )
                if review.get("review_outcome", "publish") != "publish":
                    raise ValueError("design lesson publication requires a publish review")
                if any(
                    (
                        review["organization_id"] != source["organization_id"],
                        review["design_group_id"] != source["design_group_id"],
                        str(review["working_copy_id"])
                        != str(source["working_copy_id"]),
                        review["lesson_id"] != package["lesson_id"],
                        review["package_sha256"] != package_sha256,
                        review["review_card_sha256"]
                        != verified_review_card_sha256,
                        review["review_path"] != verified_review_path,
                        review["package_path"] != verified_package_path,
                        review["final_model_sha256"]
                        != source["after_model_sha256"],
                    )
                ):
                    raise ValueError(
                        "design lesson review does not match the approved package"
                    )

            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"design-lesson-package:{package_sha256}",),
            )
            existing = connection.execute(
                "SELECT * FROM design_lesson_events WHERE package_sha256=%s",
                (package_sha256,),
            ).fetchone()
            if existing is not None:
                if review is not None:
                    raise ValueError(
                        "review package digest is already published without this review binding"
                    )
                expected_predecessor = str(supersedes_lesson_id or "")
                actual_predecessor = str(existing.get("supersedes") or "")
                if existing["organization_id"] != source["organization_id"] or actual_predecessor != expected_predecessor:
                    raise ValueError("package digest is already bound to a different organization or lifecycle operation")
                return self._design_lesson_record(connection, existing)

            archived_evidence = list(archived_evidence or [])
            archived_by_id = {
                str(item.get("evidence_id", "")): item for item in archived_evidence
            }
            if set(archived_by_id) != evidence_ids:
                raise ValueError("every evidence_manifest item must have one archived evidence artifact")
            for evidence in package["evidence_manifest"]:
                archived = archived_by_id[evidence["evidence_id"]]
                if archived.get("artifact_sha256") != evidence["sha256"]:
                    raise ValueError(
                        f"archived evidence SHA-256 mismatch: {evidence['evidence_id']}"
                    )
                if not str(archived.get("artifact_storage_path", "")).strip():
                    raise ValueError("archived evidence artifact_storage_path is required")
                if not str(archived.get("artifact_source_path", "")).strip():
                    raise ValueError("archived evidence artifact_source_path is required")
            if working_copy_artifact is None:
                raise ValueError("immutable working-copy artifact is required")
            if working_copy_artifact.get("sha256") != source["after_model_sha256"]:
                raise ValueError("working-copy artifact does not match the reviewed after-model hash")
            if not str(working_copy_artifact.get("storage_path", "")).strip():
                raise ValueError("working-copy artifact storage_path is required")

            predecessor_identity = None
            if supersedes_lesson_id:
                predecessor_identity = connection.execute(
                    "SELECT id,organization_id,lesson_key FROM design_lesson_events WHERE id::text=%s",
                    (supersedes_lesson_id,),
                ).fetchone()
                if predecessor_identity is None:
                    raise ValueError("supersedes_lesson_id must reference a design lesson")
                if predecessor_identity["organization_id"] != source["organization_id"]:
                    raise ValueError("replacement and predecessor must belong to the same organization")
                lineage_key = predecessor_identity["lesson_key"]
            else:
                lineage_key = package["lesson_id"]
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"design-lesson-lineage:{source['organization_id']}:{lineage_key}",),
            )

            working = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE",
                (source["working_copy_id"],),
            ).fetchone()
            if working is None:
                raise KeyError(f"unknown source working_copy_id: {source['working_copy_id']}")
            if working["organization_id"] != source["organization_id"] or working["design_group_id"] != source["design_group_id"]:
                raise ValueError("source working copy organization/design-group relationship is invalid")
            if working.get("job_id") is None:
                raise ValueError(
                    "JOB_MIGRATION_REQUIRED: source working copy is not bound to a Design Job"
                )
            if working.get("family_id") != source.get("family_id"):
                raise ValueError("source working copy family relationship is invalid")
            artifact_source_path = str(working_copy_artifact.get("source_path", ""))
            if artifact_source_path and artifact_source_path != str(working["working_path"]):
                raise ValueError("working-copy artifact was captured from a different working copy path")

            change_sets = connection.execute(
                "SELECT * FROM design_change_sets WHERE id = ANY(%s::uuid[]) FOR UPDATE",
                (change_set_ids,),
            ).fetchall()
            if {str(row["id"]) for row in change_sets} != set(change_set_ids):
                raise ValueError("source change-set relationship contains an unknown change set")
            change_sets_by_id = {str(row["id"]): row for row in change_sets}
            for change_set_id in change_set_ids:
                change_set = change_sets_by_id[change_set_id]
                if str(change_set["working_copy_id"]) != str(working["id"]):
                    raise ValueError("source change set belongs to another working copy")
                if change_set["status"] != "applied":
                    raise ValueError("design lesson approval requires applied change sets")
            if working["source_sha256"] != source["before_model_sha256"]:
                first_change_set = change_sets_by_id[change_set_ids[0]]
                if first_change_set.get("applied_at") is None:
                    raise ValueError("design lesson approval requires applied change-set timestamps")
                predecessor = connection.execute(
                    "SELECT resulting_sha256 FROM design_change_sets WHERE working_copy_id=%s "
                    "AND status='applied' AND (applied_at,created_at,id) < (%s,%s,%s) "
                    "ORDER BY applied_at DESC,created_at DESC,id DESC LIMIT 1 FOR UPDATE",
                    (
                        working["id"],
                        first_change_set["applied_at"],
                        first_change_set["created_at"],
                        first_change_set["id"],
                    ),
                ).fetchone()
                if (
                    predecessor is None
                    or predecessor["resulting_sha256"] != source["before_model_sha256"]
                ):
                    raise ValueError(
                        "source before-model hash must match the working-copy source or the immediate predecessor revision"
                    )
            final_change_set = change_sets_by_id[change_set_ids[-1]]
            if final_change_set["resulting_sha256"] != source["after_model_sha256"]:
                raise ValueError("final applied change-set hash does not match the after model")
            if review is None:
                sha256_reader = working_copy_sha256_reader or (
                    lambda value: file_sha256(Path(value).resolve(strict=True))
                )
                try:
                    current_locked_sha256 = sha256_reader(str(working["working_path"]))
                except (OSError, ValueError) as exc:
                    raise ValueError("current locked FCStd could not be hashed") from exc
                if current_locked_sha256 != source["after_model_sha256"]:
                    raise ValueError(
                        "current locked FCStd hash does not match the reviewed after-model hash"
                    )
            elif (
                working_copy_artifact.get("storage_path")
                != review["approved_final_artifact_path"]
            ):
                raise ValueError(
                    "approved final artifact does not match the review binding"
                )
            report_bindings: dict[str, dict[str, Any]] = {}
            for evidence in package["evidence_manifest"]:
                role = evidence["role"]
                if role not in EVIDENCE_ROLE_VALIDATION_KINDS:
                    continue
                if (
                    str(evidence["working_copy_id"]) != str(working["id"])
                    or str(evidence["change_set_id"]) != str(final_change_set["id"])
                    or evidence["model_sha256"] != source["after_model_sha256"]
                ):
                    raise ValueError(
                        f"validation evidence revision binding mismatch: {evidence['evidence_id']}"
                    )
                validation = connection.execute(
                    "SELECT * FROM validation_reports WHERE working_copy_id=%s AND change_set_id=%s "
                    "AND validation_kind=%s AND status='passed' AND working_sha256=%s "
                    "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                    (
                        working["id"],
                        final_change_set["id"],
                        evidence["validation_kind"],
                        source["after_model_sha256"],
                    ),
                ).fetchone()
                if validation is None:
                    raise ValueError(
                        f"design lesson approval requires same-revision passed {evidence['validation_kind']} evidence"
                    )
                archived = archived_by_id[evidence["evidence_id"]]
                if (
                    not str(validation.get("report_path") or "").strip()
                    or validation.get("report_sha256") != evidence["sha256"]
                ):
                    raise ValueError(
                        f"validation requires immutable report path and digest: {evidence['evidence_id']}"
                    )
                if review is None and str(validation["report_path"]) != str(archived["artifact_source_path"]):
                    raise ValueError(
                        f"validation report artifact mismatch: {evidence['evidence_id']}"
                    )
                report_bindings[evidence["evidence_id"]] = dict(validation)

            predecessor = None
            predecessor_assertions: dict[str, dict[str, Any]] = {}
            if supersedes_lesson_id:
                predecessor = connection.execute(
                    "SELECT * FROM design_lesson_events WHERE id::text=%s FOR UPDATE",
                    (supersedes_lesson_id,),
                ).fetchone()
                if predecessor is None or predecessor["status"] != "approved":
                    raise ValueError("supersedes_lesson_id must reference an approved design lesson")
                if predecessor_identity is None or predecessor["lesson_key"] != predecessor_identity["lesson_key"]:
                    raise RuntimeError("design lesson lineage changed while acquiring its lock")
                if predecessor["organization_id"] != source["organization_id"]:
                    raise ValueError("replacement and predecessor must belong to the same organization")
                predecessor_job = connection.execute(
                    "SELECT job_id FROM design_working_copies WHERE id=%s FOR UPDATE",
                    (predecessor["source_working_copy_id"],),
                ).fetchone()
                if (
                    predecessor_job is None
                    or predecessor_job.get("job_id") is None
                    or str(predecessor_job["job_id"]) != str(working["job_id"])
                ):
                    raise ValueError(
                        "replacement and predecessor must belong to the same Design Job"
                    )
                rows = connection.execute(
                    "SELECT a.*,l.assertion_key FROM design_lesson_assertions l "
                    "JOIN knowledge_assertions a ON a.id=l.assertion_id WHERE l.lesson_event_id=%s FOR UPDATE OF a",
                    (predecessor["id"],),
                ).fetchall()
                predecessor_assertions = {row["assertion_key"]: dict(row) for row in rows}
                lineage_rows = connection.execute(
                    "SELECT a.subject_ref,a.predicate,l.assertion_key FROM design_lesson_events e "
                    "JOIN design_lesson_assertions l ON l.lesson_event_id=e.id "
                    "JOIN knowledge_assertions a ON a.id=l.assertion_id "
                    "WHERE e.organization_id=%s AND e.lesson_key=%s FOR UPDATE OF a",
                    (source["organization_id"], predecessor["lesson_key"]),
                ).fetchall()
                for assertion in package["atomic_assertions"]:
                    mismatched_identity = any(
                        row["assertion_key"] == assertion["assertion_key"]
                        and (
                            row["subject_ref"] != assertion["subject_ref"]
                            or row["predicate"] != assertion["predicate"]
                        )
                        for row in lineage_rows
                    )
                    if mismatched_identity:
                        raise ValueError(
                            "a lesson lineage must preserve subject_ref and predicate for a stable assertion_key"
                        )
                lesson_key = predecessor["lesson_key"]
                revision = int(connection.execute(
                    "SELECT COALESCE(max(revision),0)+1 AS revision FROM design_lesson_events "
                    "WHERE organization_id=%s AND lesson_key=%s",
                    (source["organization_id"], lesson_key),
                ).fetchone()["revision"])
            else:
                prior = connection.execute(
                    "SELECT id,status FROM design_lesson_events WHERE organization_id=%s AND lesson_key=%s "
                    "ORDER BY revision DESC FOR UPDATE",
                    (source["organization_id"], package["lesson_id"]),
                ).fetchone()
                if prior is not None:
                    raise ValueError(
                        "a previously used lesson_key requires explicit replacement or restore lineage"
                    )
                lesson_key = package["lesson_id"]
                revision = 1

            lesson = connection.execute(
                "INSERT INTO design_lesson_events(lesson_key,revision,organization_id,source_design_group_id,"
                "source_family_id,source_working_copy_id,codex_session_id,title,before_model_sha256,after_model_sha256,"
                "problem,root_causes,corrections,prevention,applicability,non_applicable_conditions,search_terms,"
                "evidence_manifest,package_sha256,archived_package_path,status,supersedes,approved_by,approval_text) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,"
                "%s::jsonb,%s,%s::jsonb,%s,%s,'approved',%s,%s,%s) RETURNING *",
                (
                    lesson_key,
                    revision,
                    source["organization_id"],
                    source["design_group_id"],
                    source.get("family_id"),
                    working["id"],
                    package["codex_session_id"],
                    package["title"],
                    source["before_model_sha256"],
                    source["after_model_sha256"],
                    json.dumps(package["problem"], ensure_ascii=False),
                    json.dumps(package["root_causes"], ensure_ascii=False),
                    json.dumps(package["corrections"], ensure_ascii=False),
                    json.dumps(package["prevention"], ensure_ascii=False),
                    json.dumps(package["applicability"], ensure_ascii=False),
                    json.dumps(package.get("non_applicable_conditions", []), ensure_ascii=False),
                    package["search_terms"],
                    json.dumps(package["evidence_manifest"], ensure_ascii=False),
                    package_sha256,
                    archived_package_path,
                    predecessor["id"] if predecessor else None,
                    reviewer_id,
                    reviewer_text,
                ),
            ).fetchone()
            for sort_order, change_set_id in enumerate(change_set_ids):
                connection.execute(
                    "INSERT INTO design_lesson_change_sets(lesson_event_id,change_set_id,sort_order) VALUES (%s,%s,%s)",
                    (lesson["id"], change_set_id, sort_order),
                )

            working_snapshot_id = "approved-working-copy-snapshot"
            if working_snapshot_id in evidence_ids:
                raise ValueError(f"reserved evidence_id cannot be supplied: {working_snapshot_id}")
            connection.execute(
                "INSERT INTO design_lesson_evidence_artifacts(lesson_event_id,evidence_id,evidence_role,"
                "artifact_sha256,artifact_storage_path,artifact_source_path,media_type) "
                "VALUES (%s,%s,'source_after_model',%s,%s,%s,'application/x-freecad')",
                (
                    lesson["id"],
                    working_snapshot_id,
                    working_copy_artifact["sha256"],
                    working_copy_artifact["storage_path"],
                    working_copy_artifact.get("source_path") or working["working_path"],
                ),
            )
            immutable_evidence: dict[str, dict[str, Any]] = {}
            for descriptor in package["evidence_manifest"]:
                archived = archived_by_id[descriptor["evidence_id"]]
                connection.execute(
                    "INSERT INTO design_lesson_evidence_artifacts(lesson_event_id,evidence_id,evidence_role,"
                    "artifact_sha256,artifact_storage_path,artifact_source_path,media_type) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        lesson["id"],
                        descriptor["evidence_id"],
                        descriptor["role"],
                        archived["artifact_sha256"],
                        archived["artifact_storage_path"],
                        archived["artifact_source_path"],
                        descriptor["media_type"],
                    ),
                )
                immutable = {
                    "evidence_id": descriptor["evidence_id"],
                    "role": descriptor["role"],
                    "media_type": descriptor["media_type"],
                    "artifact_sha256": archived["artifact_sha256"],
                    "artifact_storage_path": archived["artifact_storage_path"],
                }
                binding = report_bindings.get(descriptor["evidence_id"])
                if binding is not None:
                    connection.execute(
                        "INSERT INTO design_lesson_report_bindings(lesson_event_id,evidence_id,validation_report_id,"
                        "validation_kind,working_copy_id,change_set_id,working_sha256) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            lesson["id"],
                            descriptor["evidence_id"],
                            binding["id"],
                            binding["validation_kind"],
                            binding["working_copy_id"],
                            binding["change_set_id"],
                            binding["working_sha256"],
                        ),
                    )
                    immutable["report_binding"] = {
                        "validation_report_id": str(binding["id"]),
                        "validation_kind": binding["validation_kind"],
                        "working_copy_id": str(binding["working_copy_id"]),
                        "change_set_id": str(binding["change_set_id"]),
                        "working_sha256": binding["working_sha256"],
                    }
                immutable_evidence[descriptor["evidence_id"]] = immutable

            for sort_order, assertion in enumerate(package["atomic_assertions"]):
                predecessor_assertion = predecessor_assertions.get(assertion["assertion_key"])
                contradiction_ids: list[str] = []
                for value in assertion.get("contradicts", []):
                    try:
                        contradiction_ids.append(str(uuid.UUID(str(value))))
                    except (ValueError, AttributeError, TypeError):
                        raise ValueError(f"invalid contradiction assertion id: {value}") from None
                if contradiction_ids:
                    targets = connection.execute(
                        "SELECT id,organization_id,status FROM knowledge_assertions WHERE id = ANY(%s::uuid[]) FOR UPDATE",
                        (contradiction_ids,),
                    ).fetchall()
                    target_by_id = {str(row["id"]): row for row in targets}
                    missing_targets = sorted(set(contradiction_ids) - set(target_by_id))
                    if missing_targets:
                        raise ValueError(
                            "contradiction target does not exist: " + ", ".join(missing_targets)
                        )
                    cross_organization = sorted(
                        target_id
                        for target_id, row in target_by_id.items()
                        if row["organization_id"] != source["organization_id"]
                    )
                    if cross_organization:
                        raise ValueError(
                            "contradiction targets must belong to the same organization: "
                            + ", ".join(cross_organization)
                        )
                    nonapproved = sorted(
                        target_id
                        for target_id, row in target_by_id.items()
                        if row["status"] != "approved"
                    )
                    if nonapproved:
                        raise ValueError(
                            "contradiction targets must be approved: " + ", ".join(nonapproved)
                        )
                    allowed_predecessor_ids = {
                        str(item["id"]) for item in predecessor_assertions.values()
                    }
                    unresolved_ids = sorted(set(contradiction_ids) - allowed_predecessor_ids)
                    if unresolved_ids:
                        raise ValueError(
                            "approval blocked by unresolved approved contradictions: "
                            + ", ".join(unresolved_ids)
                        )
                applicability = {
                    **package["applicability"],
                    "constraint_kind": assertion["constraint_kind"],
                    "lesson_id": package["lesson_id"],
                }
                evidence = [
                    {
                        "design_lesson_id": str(lesson["id"]),
                        "assertion_key": assertion["assertion_key"],
                    },
                    *[
                        immutable_evidence[evidence_id]
                        for evidence_id in assertion["evidence_refs"]
                    ],
                ]
                assertion_row = connection.execute(
                    "INSERT INTO knowledge_assertions(organization_id,design_group_id,family_id,subject_ref,predicate,"
                    "object_value,unit,scope_kind,risk_level,status,source_kind,evidence,confidence,applicability,"
                    "non_applicable_conditions,contradicts,supersedes,created_by) VALUES (%s,%s,NULL,%s,%s,%s::jsonb,"
                    "%s,%s,%s,'approved',%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) RETURNING *",
                    (
                        source["organization_id"],
                        source["design_group_id"],
                        assertion["subject_ref"],
                        assertion["predicate"],
                        json.dumps(assertion["object_value"], ensure_ascii=False),
                        assertion.get("unit"),
                        PUBLISHED_SCOPE,
                        PUBLISHED_RISK,
                        PUBLISHED_SOURCE,
                        json.dumps(evidence, ensure_ascii=False),
                        float(assertion.get("confidence", 1.0)),
                        json.dumps(applicability, ensure_ascii=False),
                        json.dumps(package.get("non_applicable_conditions", []), ensure_ascii=False),
                        json.dumps(contradiction_ids),
                        predecessor_assertion["id"] if predecessor_assertion else None,
                        reviewer_id,
                    ),
                ).fetchone()
                connection.execute(
                    "INSERT INTO design_lesson_assertions(lesson_event_id,assertion_id,assertion_key,sort_order) "
                    "VALUES (%s,%s,%s,%s)",
                    (lesson["id"], assertion_row["id"], assertion["assertion_key"], sort_order),
                )
                connection.execute(
                    "INSERT INTO review_events(assertion_id,decision,reviewer_id,reviewer_text,previous_status,resulting_status) "
                    "VALUES (%s,'approve-design-lesson',%s,%s,'external_staging','approved')",
                    (assertion_row["id"], reviewer_id, reviewer_text),
                )
                exact_terms = sorted(
                    set(item.strip().lower() for item in package["search_terms"] if item.strip())
                    | set(_search_terms(assertion["object_value"]))
                    | {assertion["subject_ref"].strip().lower(), assertion["predicate"].strip().lower()}
                )
                search_text = " ".join(
                    [
                        package["title"],
                        json.dumps(
                            package["problem"],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        " ".join(package["root_causes"]),
                        " ".join(package["corrections"]),
                        json.dumps(
                            package["prevention"],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            package["applicability"],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            package.get("non_applicable_conditions", []),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        assertion["subject_ref"],
                        assertion["predicate"],
                        json.dumps(assertion["object_value"], ensure_ascii=False, sort_keys=True),
                        " ".join(package["search_terms"]),
                    ]
                )
                connection.execute(
                    "INSERT INTO knowledge_search_documents(assertion_id,organization_id,design_group_id,family_id,"
                    "exact_terms,search_text) VALUES (%s,%s,%s,NULL,%s,%s)",
                    (assertion_row["id"], source["organization_id"], source["design_group_id"], exact_terms, search_text),
                )
                self._enqueue(
                    connection,
                    "knowledge_assertion",
                    str(assertion_row["id"]),
                    "knowledge_assertion.reviewed",
                    {
                        "assertion_id": str(assertion_row["id"]),
                        "design_lesson_id": str(lesson["id"]),
                        "status": "approved",
                    },
                )

            if predecessor is not None:
                connection.execute(
                    "UPDATE design_lesson_events SET status='superseded' WHERE id=%s",
                    (predecessor["id"],),
                )
                for predecessor_assertion in predecessor_assertions.values():
                    if predecessor_assertion["status"] == "approved":
                        connection.execute(
                            "UPDATE knowledge_assertions SET status='superseded',revision=revision+1,updated_at=now() WHERE id=%s",
                            (predecessor_assertion["id"],),
                        )
                        connection.execute(
                            "DELETE FROM knowledge_search_documents WHERE assertion_id=%s",
                            (predecessor_assertion["id"],),
                        )
                        connection.execute(
                            "INSERT INTO review_events(assertion_id,decision,reviewer_id,reviewer_text,previous_status,resulting_status) "
                            "VALUES (%s,'superseded-by-design-lesson',%s,%s,'approved','superseded')",
                            (predecessor_assertion["id"], reviewer_id, f"Superseded by design lesson {lesson['id']}"),
                        )
                        self._enqueue(
                            connection,
                            "knowledge_assertion",
                            str(predecessor_assertion["id"]),
                            "knowledge_assertion.reviewed",
                            {
                                "assertion_id": str(predecessor_assertion["id"]),
                                "design_lesson_id": str(predecessor["id"]),
                                "status": "superseded",
                            },
                        )
                self._enqueue_design_lesson_event(
                    connection,
                    event_type="design_lesson.superseded",
                    lesson_id=str(predecessor["id"]),
                )
            self._enqueue_design_lesson_event(
                connection,
                event_type="design_lesson.approved",
                lesson_id=str(lesson["id"]),
            )
            if review is not None:
                review = connection.execute(
                    "UPDATE design_lesson_reviews SET status='approved-retrieval-pending',"
                    "published_design_lesson_id=%s,reviewed_by=%s,reviewed_at=now(),reviewer_text=%s,"
                    "confirmation_mode=%s,decision_receipt_sha256=%s,decision_receipt_path=%s "
                    "WHERE id=%s AND status='awaiting-engineer-review' RETURNING *",
                    (
                        lesson["id"],
                        reviewer_id,
                        reviewer_text,
                        confirmation_mode,
                        decision_receipt_sha256,
                        decision_receipt_path,
                        review_id,
                    ),
                ).fetchone()
                if review is None:
                    raise RuntimeError(
                        "design lesson review state changed while locked"
                    )
                self._enqueue_design_lesson_review_event(
                    connection,
                    event_type="design_lesson_review.approved",
                    review=review,
                )
            if pre_commit_verifier is not None:
                pre_commit_verifier()
            return self._design_lesson_record(connection, lesson)

    def get_design_lesson(self, lesson_id: str, *, organization_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM design_lesson_events WHERE organization_id=%s AND (id::text=%s OR lesson_key=%s OR "
                "'design-lesson-'||encode(digest(id::text,'sha256'),'hex')=%s) "
                "ORDER BY revision DESC LIMIT 1",
                (organization_id, lesson_id, lesson_id, lesson_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown design lesson: {lesson_id}")
            return self._design_lesson_record(connection, row)

    def get_design_lesson_audit(
        self,
        *,
        lesson_id: str,
        organization_id: str,
        reviewer_id: str,
    ) -> dict[str, Any]:
        """Return source-sensitive audit detail only to an owner in the lesson organization."""
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            if (
                actor is None
                or actor["role"] != "family_owner"
                or actor["organization_id"] != organization_id
            ):
                raise PermissionError(
                    "design lesson audit requires a family_owner in the configured organization"
                )
            row = connection.execute(
                "SELECT * FROM design_lesson_events WHERE organization_id=%s "
                "AND (id::text=%s OR lesson_key=%s OR "
                "'design-lesson-'||encode(digest(id::text,'sha256'),'hex')=%s) "
                "ORDER BY revision DESC LIMIT 1 FOR UPDATE",
                (organization_id, lesson_id, lesson_id, lesson_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown design lesson: {lesson_id}")
            record = self._design_lesson_record(connection, row)
            record["design_lesson_ref"] = _opaque_design_lesson_ref(row["id"])
            record["review_history"] = [
                {
                    **dict(item),
                    "id": str(item["id"]),
                    "assertion_id": str(item["assertion_id"]),
                }
                for item in connection.execute(
                    "SELECT r.*,l.assertion_key FROM design_lesson_assertions l "
                    "JOIN review_events r ON r.assertion_id=l.assertion_id "
                    "WHERE l.lesson_event_id=%s ORDER BY r.created_at,r.id",
                    (row["id"],),
                ).fetchall()
            ]
            record["lineage"] = [
                {
                    "id": str(item["id"]),
                    "revision": int(item["revision"]),
                    "status": item["status"],
                    "supersedes": (
                        str(item["supersedes"]) if item.get("supersedes") is not None else None
                    ),
                }
                for item in connection.execute(
                    "SELECT id,revision,status,supersedes FROM design_lesson_events "
                    "WHERE organization_id=%s AND lesson_key=%s ORDER BY revision",
                    (organization_id, row["lesson_key"]),
                ).fetchall()
            ]
            record["evidence_artifacts"] = [
                {
                    **dict(item),
                    "lesson_event_id": str(item["lesson_event_id"]),
                    "validation_report_id": (
                        str(item["validation_report_id"])
                        if item.get("validation_report_id") is not None
                        else None
                    ),
                    "working_copy_id": (
                        str(item["working_copy_id"])
                        if item.get("working_copy_id") is not None
                        else None
                    ),
                    "change_set_id": (
                        str(item["change_set_id"])
                        if item.get("change_set_id") is not None
                        else None
                    ),
                }
                for item in connection.execute(
                    "SELECT e.*,b.validation_report_id,b.validation_kind,b.working_copy_id,b.change_set_id,"
                    "b.working_sha256 FROM design_lesson_evidence_artifacts e "
                    "LEFT JOIN design_lesson_report_bindings b ON b.lesson_event_id=e.lesson_event_id "
                    "AND b.evidence_id=e.evidence_id WHERE e.lesson_event_id=%s ORDER BY e.evidence_id",
                    (row["id"],),
                ).fetchall()
            ]
            return record

    def search_approved_design_lessons(
        self,
        *,
        organization_id: str,
        query: str = "",
        design_group_id: str | None = None,
        family_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("design lesson search limit must be between 1 and 50")
        return self.search_approved_design_lesson_page(
            organization_id=organization_id,
            query=query,
            design_group_id=design_group_id,
            family_id=family_id,
            page_size=limit,
        )["items"]

    def search_approved_design_lesson_page(
        self,
        *,
        organization_id: str,
        query: str = "",
        design_group_id: str | None = None,
        family_id: str | None = None,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 50
        ):
            raise ValueError("design lesson search page_size must be between 1 and 50")
        normalized = query.strip().lower()
        mode = "ranked" if normalized else "recent"
        fingerprint = _design_lesson_search_fingerprint(
            organization_id=organization_id,
            normalized_query=normalized,
            design_group_id=design_group_id,
            family_id=family_id,
        )
        decoded_cursor = (
            _decode_design_lesson_search_cursor(
                cursor,
                expected_fingerprint=fingerprint,
                expected_mode=mode,
            )
            if cursor
            else None
        )
        filters = ["l.organization_id=%s", "l.status='approved'"]
        parameters: list[Any] = [organization_id]
        if design_group_id:
            filters.append("l.source_design_group_id=%s")
            parameters.append(design_group_id)
        if family_id:
            filters.append("l.source_family_id=%s")
            parameters.append(family_id)
        with self.connection() as connection:
            if normalized:
                cursor_filter = ""
                cursor_parameters: list[Any] = []
                if decoded_cursor is not None:
                    cursor_filter = (
                        " AND (m.exact_match,m.text_rank,m.trigram_similarity,l.approved_at,"
                        "encode(digest(l.id::text,'sha256'),'hex')) "
                        "< (%s::integer,%s::real,%s::real,%s::timestamptz,%s)"
                    )
                    cursor_parameters = [
                        int(decoded_cursor["exact_match"]),
                        float(decoded_cursor["text_rank"]),
                        float(decoded_cursor["trigram_similarity"]),
                        decoded_cursor["approved_at"],
                        decoded_cursor["key"],
                    ]
                rows = connection.execute(
                    "WITH matches AS (SELECT la.lesson_event_id,"
                    "max(CASE WHEN %s=ANY(d.exact_terms) THEN 1 ELSE 0 END) AS exact_match,"
                    "max(ts_rank(d.search_vector,plainto_tsquery('simple',%s))) AS text_rank,"
                    "max(similarity(d.search_text,%s)) AS trigram_similarity "
                    "FROM design_lesson_assertions la JOIN knowledge_assertions a ON a.id=la.assertion_id "
                    "JOIN knowledge_search_documents d ON d.assertion_id=a.id WHERE a.status='approved' AND ("
                    "%s=ANY(d.exact_terms) OR d.search_vector @@ plainto_tsquery('simple',%s) OR similarity(d.search_text,%s)>=0.12) "
                    "GROUP BY la.lesson_event_id) SELECT l.*,w.job_id,m.exact_match,m.text_rank,m.trigram_similarity "
                    "FROM matches m JOIN design_lesson_events l ON l.id=m.lesson_event_id "
                    "LEFT JOIN design_working_copies w ON w.id=l.source_working_copy_id WHERE "
                    + " AND ".join(filters)
                    + cursor_filter
                    + " ORDER BY m.exact_match DESC,m.text_rank DESC,m.trigram_similarity DESC,"
                    "l.approved_at DESC,encode(digest(l.id::text,'sha256'),'hex') DESC LIMIT %s",
                    (
                        normalized,
                        query,
                        query,
                        normalized,
                        query,
                        query,
                        *parameters,
                        *cursor_parameters,
                        page_size + 1,
                    ),
                ).fetchall()
            else:
                cursor_filter = ""
                cursor_parameters = []
                if decoded_cursor is not None:
                    cursor_filter = (
                        " AND (l.approved_at,encode(digest(l.id::text,'sha256'),'hex')) "
                        "< (%s::timestamptz,%s)"
                    )
                    cursor_parameters = [
                        decoded_cursor["approved_at"],
                        decoded_cursor["key"],
                    ]
                rows = connection.execute(
                    "SELECT l.*,w.job_id FROM design_lesson_events l "
                    "LEFT JOIN design_working_copies w ON w.id=l.source_working_copy_id WHERE "
                    + " AND ".join(filters)
                    + cursor_filter
                    + " ORDER BY l.approved_at DESC,encode(digest(l.id::text,'sha256'),'hex') DESC"
                    + " LIMIT %s",
                    (*parameters, *cursor_parameters, page_size + 1),
                ).fetchall()
            page_rows = list(rows[:page_size])
            items = self._design_lesson_records(connection, page_rows)
        next_cursor = None
        if len(rows) > page_size and page_rows:
            last = page_rows[-1]
            payload: dict[str, Any] = {
                "v": 1,
                "fingerprint": fingerprint,
                "mode": mode,
                "approved_at": last["approved_at"].isoformat(),
                "key": hashlib.sha256(str(last["id"]).encode("utf-8")).hexdigest(),
            }
            if mode == "ranked":
                payload.update({
                    "exact_match": int(last["exact_match"]),
                    "text_rank": float(last["text_rank"]),
                    "trigram_similarity": float(last["trigram_similarity"]),
                })
            next_cursor = _encode_design_lesson_search_cursor(payload)
        return {"items": items, "next_cursor": next_cursor}

    def revoke_design_lesson(
        self,
        *,
        lesson_id: str,
        reviewer_id: str,
        reviewer_text: str,
    ) -> dict[str, Any]:
        if not reviewer_text.strip():
            raise ValueError("revocation reviewer_text is required")
        with self.connection() as connection, connection.transaction():
            identity = connection.execute(
                "SELECT id,organization_id,lesson_key FROM design_lesson_events WHERE id::text=%s",
                (lesson_id,),
            ).fetchone()
            if identity is None:
                raise KeyError(f"unknown design lesson: {lesson_id}")
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            if actor is None or actor["role"] != "family_owner" or actor["organization_id"] != identity["organization_id"]:
                raise PermissionError("design lesson revocation requires a family_owner in the lesson organization")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"design-lesson-lineage:{identity['organization_id']}:{identity['lesson_key']}",),
            )
            lesson = connection.execute(
                "SELECT * FROM design_lesson_events WHERE id::text=%s FOR UPDATE",
                (lesson_id,),
            ).fetchone()
            if (
                lesson is None
                or lesson["organization_id"] != identity["organization_id"]
                or lesson["lesson_key"] != identity["lesson_key"]
            ):
                raise RuntimeError("design lesson lineage changed while acquiring its lock")
            if lesson["status"] != "approved":
                raise ValueError("only an approved design lesson can be revoked")
            updated = connection.execute(
                "UPDATE design_lesson_events SET status='revoked',revoked_by=%s,revoked_reason=%s,revoked_at=now() "
                "WHERE id=%s RETURNING *",
                (reviewer_id, reviewer_text, lesson["id"]),
            ).fetchone()
            assertions = connection.execute(
                "SELECT a.* FROM design_lesson_assertions l JOIN knowledge_assertions a ON a.id=l.assertion_id "
                "WHERE l.lesson_event_id=%s FOR UPDATE OF a",
                (lesson["id"],),
            ).fetchall()
            for assertion in assertions:
                if assertion["status"] != "approved":
                    continue
                connection.execute(
                    "UPDATE knowledge_assertions SET status='superseded',revision=revision+1,updated_at=now() WHERE id=%s",
                    (assertion["id"],),
                )
                connection.execute("DELETE FROM knowledge_search_documents WHERE assertion_id=%s", (assertion["id"],))
                connection.execute(
                    "INSERT INTO review_events(assertion_id,decision,reviewer_id,reviewer_text,previous_status,resulting_status) "
                    "VALUES (%s,'revoke-design-lesson',%s,%s,'approved','superseded')",
                    (assertion["id"], reviewer_id, reviewer_text),
                )
                self._enqueue(
                    connection,
                    "knowledge_assertion",
                    str(assertion["id"]),
                    "knowledge_assertion.reviewed",
                    {
                        "assertion_id": str(assertion["id"]),
                        "design_lesson_id": str(lesson["id"]),
                        "status": "superseded",
                    },
                )
            self._enqueue_design_lesson_event(
                connection,
                event_type="design_lesson.revoked",
                lesson_id=str(updated["id"]),
            )
            return self._design_lesson_record(connection, updated)

    @staticmethod
    def _design_lesson_record(connection: Any, row: Any) -> dict[str, Any]:
        return PostgresRepository._design_lesson_records(connection, [row])[0]

    @staticmethod
    def _design_lesson_records(connection: Any, rows: list[Any]) -> list[dict[str, Any]]:
        if not rows:
            return []
        lesson_ids = [row["id"] for row in rows]
        jobs_by_lesson = {
            str(row["id"]): (
                str(row["job_id"]) if row.get("job_id") is not None else None
            )
            for row in rows
            if "job_id" in row
        }
        missing_origin_ids = [
            row["id"] for row in rows if "job_id" not in row
        ]
        if missing_origin_ids:
            origin_rows = connection.execute(
                "SELECT e.id AS lesson_event_id,w.job_id FROM design_lesson_events e "
                "LEFT JOIN design_working_copies w ON w.id=e.source_working_copy_id "
                "WHERE e.id = ANY(%s::uuid[])",
                (missing_origin_ids,),
            ).fetchall()
            jobs_by_lesson.update(
                {
                    str(item["lesson_event_id"]): (
                        str(item["job_id"])
                        if item.get("job_id") is not None
                        else None
                    )
                    for item in origin_rows
                }
            )
        assertion_rows = connection.execute(
            "SELECT l.lesson_event_id,a.*,l.assertion_key,l.sort_order,"
            "COALESCE((SELECT max(o.aggregate_version) FROM outbox_events o "
            "WHERE o.aggregate_type='knowledge_assertion' AND o.aggregate_id=a.id::text),0) "
            "AS aggregate_version "
            "FROM design_lesson_assertions l JOIN knowledge_assertions a ON a.id=l.assertion_id "
            "WHERE l.lesson_event_id = ANY(%s::uuid[]) ORDER BY l.lesson_event_id,l.sort_order",
            (lesson_ids,),
        ).fetchall()
        assertions_by_lesson: dict[str, list[dict[str, Any]]] = {
            str(lesson_id): [] for lesson_id in lesson_ids
        }
        for assertion_row in assertion_rows:
            assertion = dict(assertion_row)
            lesson_event_id = str(assertion.pop("lesson_event_id"))
            assertion["id"] = str(assertion["id"])
            if assertion.get("supersedes") is not None:
                assertion["supersedes"] = str(assertion["supersedes"])
            assertions_by_lesson[lesson_event_id].append(assertion)
        change_set_rows = connection.execute(
            "SELECT lesson_event_id,change_set_id,sort_order FROM design_lesson_change_sets "
            "WHERE lesson_event_id = ANY(%s::uuid[]) ORDER BY lesson_event_id,sort_order",
            (lesson_ids,),
        ).fetchall()
        change_sets_by_lesson: dict[str, list[str]] = {
            str(lesson_id): [] for lesson_id in lesson_ids
        }
        for item in change_set_rows:
            change_sets_by_lesson[str(item["lesson_event_id"])].append(
                str(item["change_set_id"])
            )
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            lesson_id = str(record["id"])
            record["id"] = lesson_id
            record["job_id"] = jobs_by_lesson.get(lesson_id)
            if record.get("supersedes") is not None:
                record["supersedes"] = str(record["supersedes"])
            record["assertions"] = assertions_by_lesson[lesson_id]
            record["change_set_ids"] = change_sets_by_lesson[lesson_id]
            records.append(record)
        return records

    def similar_models(self, family_id: str, model_revision_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "WITH source AS (SELECT source_artifact_id,geometry_vector,structure_vector FROM model_revisions WHERE id=%s), "
                "latest AS (SELECT DISTINCT ON (source_artifact_id) id,source_artifact_id,source_relative_path,"
                "geometry_vector,structure_vector,status FROM model_revisions WHERE family_id=%s "
                "ORDER BY source_artifact_id,created_at DESC) "
                "SELECT m.id,m.source_relative_path,1-(m.geometry_vector <=> source.geometry_vector) AS geometry_similarity,"
                "1-(m.structure_vector <=> source.structure_vector) AS structure_similarity "
                "FROM latest m,source WHERE m.source_artifact_id<>source.source_artifact_id AND m.status='analyzed' "
                "ORDER BY ((m.geometry_vector <=> source.geometry_vector)+(m.structure_vector <=> source.structure_vector)) LIMIT %s",
                (model_revision_id, family_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def family_model_count(self, family_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT count(DISTINCT source_artifact_id) AS count FROM model_revisions WHERE family_id=%s AND status='analyzed'",
                (family_id,),
            ).fetchone()
        return int(row["count"])

    def validate_family_answer_evidence(self, family_id: str, answer_event_ids: list[str]) -> None:
        unique_ids = sorted(set(answer_event_ids))
        if not unique_ids:
            raise ValueError("expert-declared family knowledge requires original answer_event_id evidence")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT count(DISTINCT a.id) AS count FROM answer_events a "
                "JOIN interaction_sessions s ON s.id=a.session_id "
                "WHERE s.family_id=%s AND a.id = ANY(%s::uuid[])",
                (family_id, unique_ids),
            ).fetchone()
        if int(row["count"]) != len(unique_ids):
            raise ValueError("expert-declared evidence must reference answers from this product family")

    def family_manifests(self, family_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT manifest FROM (SELECT DISTINCT ON (source_artifact_id) source_artifact_id,manifest,created_at "
                "FROM model_revisions WHERE family_id=%s AND status='analyzed' "
                "ORDER BY source_artifact_id,created_at DESC) latest ORDER BY created_at",
                (family_id,),
            ).fetchall()
        return [dict(row["manifest"]) for row in rows]

    def family_similarity_pairs(self, family_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "WITH latest AS (SELECT DISTINCT ON (source_artifact_id) id,source_artifact_id,geometry_vector,"
                "structure_vector,status FROM model_revisions WHERE family_id=%s "
                "ORDER BY source_artifact_id,created_at DESC) "
                "SELECT a.id AS left_model_revision_id,b.id AS right_model_revision_id,"
                "1-(a.geometry_vector <=> b.geometry_vector) AS geometry_similarity,"
                "1-(a.structure_vector <=> b.structure_vector) AS structure_similarity "
                "FROM latest a JOIN latest b ON a.source_artifact_id < b.source_artifact_id "
                "WHERE a.status='analyzed' AND b.status='analyzed' "
                "ORDER BY ((a.geometry_vector <=> b.geometry_vector)+(a.structure_vector <=> b.structure_vector)) "
                "LIMIT %s",
                (family_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def propose_subfamily(
        self,
        *,
        subfamily_id: str,
        family_id: str,
        canonical_name: str,
        aliases: list[str],
        model_revision_ids: list[str],
        evidence: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        if len(set(model_revision_ids)) < 2:
            raise ValueError("subfamily proposal requires at least two distinct model revisions")
        with self.connection() as connection, connection.transaction():
            rows = connection.execute(
                "SELECT id FROM model_revisions WHERE id = ANY(%s::uuid[]) AND family_id=%s AND status='analyzed'",
                (model_revision_ids, family_id),
            ).fetchall()
            if {str(row["id"]) for row in rows} != set(model_revision_ids):
                raise ValueError("all proposed subfamily models must be analyzed members of the same family")
            row = connection.execute(
                "INSERT INTO product_subfamilies(id,family_id,canonical_name,aliases,status,evidence,created_by) "
                "VALUES (%s,%s,%s,%s::jsonb,'proposed',%s::jsonb,%s) RETURNING *",
                (
                    subfamily_id,
                    family_id,
                    canonical_name,
                    json.dumps(aliases, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    actor_id,
                ),
            ).fetchone()
            for model_id in sorted(set(model_revision_ids)):
                connection.execute(
                    "INSERT INTO model_subfamily_assignments(model_revision_id,subfamily_id,status) "
                    "VALUES (%s,%s,'proposed')",
                    (model_id, subfamily_id),
                )
        return dict(row)

    def review_subfamily(
        self, subfamily_id: str, decision: str, actor_id: str
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("subfamily decision must be approve or reject")
        status = "approved" if decision == "approve" else "rejected"
        with self.connection() as connection, connection.transaction():
            actor = connection.execute("SELECT role FROM actors WHERE id=%s", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("subfamily review requires family_owner")
            row = connection.execute(
                "UPDATE product_subfamilies SET status=%s,approved_by=%s,reviewed_at=now() "
                "WHERE id=%s AND status='proposed' RETURNING *",
                (status, actor_id, subfamily_id),
            ).fetchone()
            if row is None:
                raise KeyError("subfamily proposal was not found or already reviewed")
            connection.execute(
                "UPDATE model_subfamily_assignments SET status=%s,confirmed_by=%s,confirmed_at=now() "
                "WHERE subfamily_id=%s",
                (status, actor_id, subfamily_id),
            )
            self._enqueue(
                connection,
                "product_subfamily",
                subfamily_id,
                "product_subfamily.reviewed",
                {"subfamily_id": subfamily_id, "status": status},
            )
        return dict(row)

    def family_subfamilies(self, family_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT s.*,COALESCE(jsonb_agg(jsonb_build_object('model_revision_id',a.model_revision_id,"
                "'status',a.status)) FILTER (WHERE a.model_revision_id IS NOT NULL),'[]'::jsonb) AS assignments "
                "FROM product_subfamilies s LEFT JOIN model_subfamily_assignments a ON a.subfamily_id=s.id "
                "WHERE s.family_id=%s GROUP BY s.id ORDER BY s.created_at",
                (family_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_subfamily(self, subfamily_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM product_subfamilies WHERE id=%s", (subfamily_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown subfamily_id: {subfamily_id}")
        return dict(row)

    def projection_families(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM product_families ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def product_family_inventory(
        self,
        *,
        organization_id: str,
        design_group_id: str,
    ) -> list[dict[str, Any]]:
        """Return authorized discovery metadata without specialized family content."""
        with self.connection() as connection:
            families = connection.execute(
                "SELECT id,canonical_name,aliases,status,config,revision,created_at,updated_at "
                "FROM product_families WHERE organization_id=%s AND design_group_id=%s "
                "ORDER BY canonical_name,id",
                (organization_id, design_group_id),
            ).fetchall()
            products = connection.execute(
                "SELECT family_id,canonical_name,aliases,status FROM products "
                "WHERE organization_id=%s AND design_group_id=%s AND family_id IS NOT NULL "
                "ORDER BY family_id,canonical_name,id",
                (organization_id, design_group_id),
            ).fetchall()
        products_by_family: dict[str, list[dict[str, Any]]] = {}
        for row in products:
            value = dict(row)
            family_id = str(value.pop("family_id"))
            products_by_family.setdefault(family_id, []).append(value)
        result: list[dict[str, Any]] = []
        for row in families:
            value = dict(row)
            config = value.pop("config", {})
            if not isinstance(config, dict):
                config = {}
            discovery = config.get("discovery", {})
            if not isinstance(discovery, dict):
                discovery = {}
            descriptors: list[str] = []
            for source in (
                config.get("discovery_descriptors", []),
                discovery.get("descriptors", []),
                config.get("component_classes", []),
            ):
                if not isinstance(source, list):
                    continue
                for item in source:
                    normalized = str(item).strip()
                    if normalized and normalized not in descriptors:
                        descriptors.append(normalized)
            family_id = str(value.pop("id"))
            result.append(
                {
                    "family_id": family_id,
                    **value,
                    "discovery_descriptors": descriptors,
                    "products": products_by_family.get(family_id, []),
                    "database_registered": True,
                }
            )
        return result

    def record_product_family_match(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
        query: str,
        request_features: dict[str, Any],
        result: dict[str, Any],
        job_id: str | None,
        working_copy_id: str | None,
    ) -> dict[str, Any]:
        candidates = list(result.get("candidates") or [])
        first_kind = str(candidates[0].get("match_kind") or "") if candidates else ""
        decision_source = {
            "existing_job_binding": "existing_job_binding",
            "source_model_binding": "source_model_binding",
            "explicit_family_id": "explicit_family",
            "canonical_name": "explicit_family",
            "approved_alias": "explicit_family",
            "approved_product_identifier": "approved_product_identifier",
            "semantic_candidate": "semantic_candidate",
        }.get(first_kind)
        if result.get("status") == "conflict":
            decision_source = "conflict"
        elif result.get("status") == "unbound_no_match":
            decision_source = "no_match"
        if decision_source is None:
            raise ValueError("product-family match result has no auditable decision source")
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO product_family_match_decisions(organization_id,design_group_id,job_id,"
                "working_copy_id,query_sha256,request_features,status,binding_family_id,candidates,"
                "decision_source,specialized_knowledge_authorized,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s) RETURNING *",
                (
                    organization_id,
                    design_group_id,
                    job_id,
                    working_copy_id,
                    digest,
                    json.dumps(request_features, ensure_ascii=False),
                    result["status"],
                    result.get("binding_family_id"),
                    json.dumps(candidates, ensure_ascii=False),
                    decision_source,
                    bool(result.get("specialized_knowledge_authorized")),
                    actor_id,
                ),
            ).fetchone()
        return dict(row)

    def projection_models(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,family_id,product_id,source_relative_path,status,manifest FROM model_revisions ORDER BY created_at,id"
            ).fetchall()
        return [dict(row) for row in rows]

    def projection_products(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM products ORDER BY created_at,id").fetchall()
        return [dict(row) for row in rows]

    def projection_subfamilies(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT s.*,COALESCE(jsonb_agg(a.model_revision_id) FILTER (WHERE a.status='approved'),'[]'::jsonb) "
                "AS model_revision_ids FROM product_subfamilies s LEFT JOIN model_subfamily_assignments a "
                "ON a.subfamily_id=s.id GROUP BY s.id ORDER BY s.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_product(self, product_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM products WHERE id=%s", (product_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown product_id: {product_id}")
        return dict(row)

    def projection_assertions(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.*,COALESCE((SELECT max(o.aggregate_version) FROM outbox_events o "
                "WHERE o.aggregate_type='knowledge_assertion' AND o.aggregate_id=a.id::text),0) "
                "AS aggregate_version FROM knowledge_assertions a "
                "WHERE a.status IN ('approved','rejected','superseded') "
                "ORDER BY a.created_at,a.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def projection_design_lessons(self) -> list[dict[str, Any]]:
        """Return complete lesson rows for the disposable Neo4j projection."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT l.*,w.source_model_revision_id,"
                "COALESCE((SELECT max(o.aggregate_version) FROM outbox_events o "
                "WHERE o.aggregate_type='design_lesson' AND o.aggregate_id=l.id::text),0) "
                "AS aggregate_version FROM design_lesson_events l "
                "LEFT JOIN design_working_copies w ON w.id=l.source_working_copy_id "
                "ORDER BY l.approved_at,l.id"
            ).fetchall()
            return [self._design_lesson_record(connection, row) for row in rows]

    def projection_design_lesson_reviews(self) -> list[dict[str, Any]]:
        """Return authoritative review lifecycle rows for graph reconstruction."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT r.*,r.id::text AS review_id,"
                "to_char(COALESCE(r.retrieval_verified_at,r.reviewed_at,r.created_at) "
                "AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS occurred_at,"
                "COALESCE((SELECT max(o.aggregate_version) FROM outbox_events o "
                "WHERE o.aggregate_type='design_lesson_review' AND o.aggregate_id=r.id::text),0) "
                "AS aggregate_version FROM design_lesson_reviews r "
                "ORDER BY r.created_at,r.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def projection_profiles(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM family_profiles ORDER BY family_id,revision").fetchall()
        return [dict(row) for row in rows]

    def save_family_profile(
        self,
        family_id: str,
        profile: dict[str, Any],
        evidence: list[dict[str, Any]],
        status: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if status not in {"proposed", "approved", "rejected"}:
            raise ValueError("invalid family profile status")
        with self.connection() as connection, connection.transaction():
            count = connection.execute(
                "SELECT count(DISTINCT source_artifact_id) AS count FROM model_revisions WHERE family_id=%s AND status='analyzed'",
                (family_id,),
            ).fetchone()["count"]
            revision = connection.execute(
                "SELECT COALESCE(max(revision),0)+1 AS revision FROM family_profiles WHERE family_id=%s",
                (family_id,),
            ).fetchone()["revision"]
            row = connection.execute(
                "INSERT INTO family_profiles(family_id,revision,status,distinct_model_count,profile,evidence,created_by,approved_by) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) RETURNING *",
                (
                    family_id,
                    revision,
                    status,
                    count,
                    json.dumps(profile, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    actor_id,
                    actor_id if status == "approved" else None,
                ),
            ).fetchone()
        return dict(row)

    def family_profile(self, family_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM family_profiles WHERE family_id=%s ORDER BY revision DESC LIMIT 1",
                (family_id,),
            ).fetchone()
        return dict(row) if row else None

    def approved_family_profile(self, family_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM family_profiles WHERE family_id=%s AND status='approved' "
                "ORDER BY revision DESC LIMIT 1",
                (family_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_family_profile_by_id(self, profile_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM family_profiles WHERE id=%s", (profile_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown family profile: {profile_id}")
        return dict(row)

    def review_family_profile(
        self, profile_id: str, decision: str, actor_id: str, confirmation: str
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("profile decision must be approve or reject")
        if profile_id not in confirmation or ("批准" not in confirmation and "拒绝" not in confirmation):
            raise ValueError("profile confirmation must include profile_id and the Chinese decision word")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute("SELECT role FROM actors WHERE id=%s", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("family profile review requires family_owner")
            row = connection.execute(
                "UPDATE family_profiles SET status=%s,approved_by=%s WHERE id=%s AND status='proposed' RETURNING *",
                ("approved" if decision == "approve" else "rejected", actor_id, profile_id),
            ).fetchone()
            if row is None:
                raise KeyError("profile was not found or is no longer proposed")
            self._enqueue(
                connection,
                "family_profile",
                profile_id,
                "family_profile.reviewed",
                {"profile_id": profile_id, "family_id": row["family_id"], "status": row["status"]},
            )
        return dict(row)

    def resolve_source_model_revision(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        source_sha256: str,
        requested_model_revision_id: str | None = None,
        requested_family_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            if requested_model_revision_id:
                row = connection.execute(
                    "SELECT m.*,a.sha256 AS artifact_sha256 FROM model_revisions m "
                    "JOIN artifacts a ON a.id=m.source_artifact_id WHERE m.id=%s "
                    "AND m.organization_id=%s AND m.design_group_id=%s AND a.sha256=%s "
                    "AND (%s::text IS NULL OR m.family_id=%s)",
                    (
                        requested_model_revision_id,
                        organization_id,
                        design_group_id,
                        source_sha256,
                        requested_family_id,
                        requested_family_id,
                    ),
                ).fetchone()
                candidates = [row] if row is not None else []
            else:
                candidates = connection.execute(
                    "SELECT m.*,a.sha256 AS artifact_sha256 FROM model_revisions m "
                    "JOIN artifacts a ON a.id=m.source_artifact_id "
                    "WHERE m.organization_id=%s AND m.design_group_id=%s AND a.sha256=%s "
                    "AND (%s::text IS NULL OR m.family_id=%s) "
                    "ORDER BY m.created_at DESC,m.id",
                    (
                        organization_id,
                        design_group_id,
                        source_sha256,
                        requested_family_id,
                        requested_family_id,
                    ),
                ).fetchall()
        if len(candidates) != 1:
            raise ValueError(
                "existing-model source must match exactly one ingested model revision; "
                f"found {len(candidates)}"
            )
        model = dict(candidates[0])
        if model["artifact_sha256"] != source_sha256:
            raise ValueError("working-copy source hash does not match the registered model artifact")
        if model["organization_id"] != organization_id or model["design_group_id"] != design_group_id:
            raise ValueError("working-copy source model scope does not match organization/design group")
        if requested_family_id is not None and requested_family_id != model.get("family_id"):
            raise ValueError("working-copy family must match the confirmed source model family")
        return model

    def create_working_copy(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        source_sha256: str,
        source_kind: str,
        design_origin: str,
        working_path: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if design_origin not in {"existing_model", "new_design"}:
            raise ValueError("design_origin must be existing_model or new_design")
        if design_origin == "existing_model" and not model_revision_id:
            raise ValueError("existing_model working copy requires source_model_revision_id")
        with self.connection() as connection, connection.transaction():
            if model_revision_id:
                source_model = connection.execute(
                    "SELECT m.*,a.sha256 AS artifact_sha256 FROM model_revisions m "
                    "JOIN artifacts a ON a.id=m.source_artifact_id WHERE m.id=%s",
                    (model_revision_id,),
                ).fetchone()
                if source_model is None:
                    raise KeyError(f"unknown source model revision: {model_revision_id}")
                if source_model["artifact_sha256"] != source_sha256:
                    raise ValueError("working-copy source hash does not match the registered model artifact")
                if source_model["organization_id"] != organization_id or source_model["design_group_id"] != design_group_id:
                    raise ValueError("working-copy source model scope does not match organization/design group")
                if family_id != source_model["family_id"]:
                    raise ValueError("working-copy family must match the confirmed source model family")
            row = connection.execute(
                "INSERT INTO design_working_copies(organization_id,design_group_id,family_id,source_model_revision_id,"
                "source_sha256,source_kind,design_origin,working_path,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    organization_id,
                    design_group_id,
                    family_id,
                    model_revision_id,
                    source_sha256,
                    source_kind,
                    design_origin,
                    working_path,
                    actor_id,
                ),
            ).fetchone()
        return dict(row)

    def create_job_working_copy(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        working_copy_id: str,
        model_revision_id: str | None,
        source_sha256: str,
        source_kind: str,
        design_origin: str,
        working_path: str,
        working_sha256: str,
        working_size_bytes: int,
        working_relative_path: str,
        actor_id: str,
        source_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Atomically bind verified on-disk CAD bytes to one active Job."""
        for label, value in (
            ("job_id", job_id),
            ("organization_id", organization_id),
            ("design_group_id", design_group_id),
            ("working_copy_id", working_copy_id),
            ("source_sha256", source_sha256),
            ("working_path", working_path),
            ("working_sha256", working_sha256),
            ("working_relative_path", working_relative_path),
            ("actor_id", actor_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} is required")
        if type(expected_job_revision) is not int or expected_job_revision < 0:
            raise ValueError("expected_job_revision must be a non-negative integer")
        if not re.fullmatch(r"[0-9a-f]{64}", working_sha256):
            raise ValueError("working_sha256 must be a lowercase SHA-256")
        if type(working_size_bytes) is not int or working_size_bytes <= 0:
            raise ValueError("working_size_bytes must be a positive integer")
        if not re.fullmatch(
            r"models/working/[^/]+/[^/]+[.]FCStd", working_relative_path
        ):
            raise ValueError("working_relative_path must be a controlled relative FCStd path")
        if design_origin not in {"existing_model", "new_design"}:
            raise ValueError("design_origin must be existing_model or new_design")
        if design_origin == "existing_model":
            if not model_revision_id or source_snapshot is None:
                raise ValueError(
                    "existing_model working copy requires a source revision and snapshot"
                )
        elif model_revision_id is not None or source_snapshot is not None:
            raise ValueError("new_design working copy must not bind a source snapshot")

        with self.connection() as connection, connection.transaction():
            job = connection.execute(
                "SELECT * FROM design_jobs WHERE id=%s AND organization_id=%s "
                "AND design_group_id=%s AND EXISTS (SELECT 1 FROM actors actor "
                "WHERE actor.id=%s AND actor.organization_id=design_jobs.organization_id) "
                "FOR UPDATE",
                (job_id, organization_id, design_group_id, actor_id),
            ).fetchone()
            if job is None:
                raise KeyError("unknown design_job_id or unauthorized")
            if int(job["revision"]) != expected_job_revision:
                raise ValueError("stale design job revision")
            if (
                job["job_type"] != "mechanical_design"
                or job["status"] != "active"
                or job["provisioning_state"] != "ready"
            ):
                raise ValueError(
                    "working-copy creation requires an active ready mechanical_design Job"
                )
            if job.get("family_id") is not None and job.get("family_id") != family_id:
                raise ValueError("working-copy family must match the design Job family")
            if job.get("active_working_copy_id") is not None:
                raise ValueError("design Job already has an active working copy")

            if model_revision_id is not None:
                source_model = connection.execute(
                    "SELECT m.*,a.sha256 AS artifact_sha256 FROM model_revisions m "
                    "JOIN artifacts a ON a.id=m.source_artifact_id WHERE m.id=%s "
                    "AND m.organization_id=%s AND m.design_group_id=%s FOR SHARE OF m",
                    (model_revision_id, organization_id, design_group_id),
                ).fetchone()
                if source_model is None:
                    raise KeyError("unknown source model revision or unauthorized")
                if source_model["artifact_sha256"] != source_sha256:
                    raise ValueError(
                        "working-copy source hash does not match the registered model artifact"
                    )
                if source_model.get("family_id") != family_id:
                    raise ValueError(
                        "working-copy family must match the confirmed source model family"
                    )

            snapshot_row = None
            if source_snapshot is not None:
                required_snapshot = {
                    "id",
                    "source_filename",
                    "stored_path",
                    "sha256",
                    "size_bytes",
                    "source_kind",
                    "source_model_revision_id",
                }
                if set(source_snapshot) != required_snapshot:
                    raise ValueError("source_snapshot fields are invalid")
                if source_snapshot["sha256"] != source_sha256:
                    raise ValueError("source snapshot hash must match the working-copy source")
                if source_snapshot["source_model_revision_id"] != model_revision_id:
                    raise ValueError("source snapshot revision must match the working copy")
                snapshot_row = connection.execute(
                    "INSERT INTO design_job_source_snapshots(id,job_id,organization_id,design_group_id,"
                    "source_model_revision_id,source_filename,stored_path,sha256,size_bytes,source_kind,created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        source_snapshot["id"],
                        job_id,
                        organization_id,
                        design_group_id,
                        model_revision_id,
                        source_snapshot["source_filename"],
                        source_snapshot["stored_path"],
                        source_snapshot["sha256"],
                        source_snapshot["size_bytes"],
                        source_snapshot["source_kind"],
                        actor_id,
                    ),
                ).fetchone()

            working = connection.execute(
                "INSERT INTO design_working_copies(id,job_id,organization_id,design_group_id,family_id,"
                "source_model_revision_id,source_snapshot_id,bound_job_revision,source_sha256,source_kind,"
                "design_origin,working_path,working_sha256,working_size_bytes,working_relative_path,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    working_copy_id,
                    job_id,
                    organization_id,
                    design_group_id,
                    family_id,
                    model_revision_id,
                    snapshot_row["id"] if snapshot_row is not None else None,
                    expected_job_revision,
                    source_sha256,
                    source_kind,
                    design_origin,
                    working_path,
                    working_sha256,
                    working_size_bytes,
                    working_relative_path,
                    actor_id,
                ),
            ).fetchone()
            updated_job = connection.execute(
                "UPDATE design_jobs SET active_working_copy_id=%s,revision=revision+1,updated_at=now() "
                "WHERE id=%s AND organization_id=%s AND design_group_id=%s AND revision=%s "
                "AND active_working_copy_id IS NULL RETURNING *",
                (
                    working_copy_id,
                    job_id,
                    organization_id,
                    design_group_id,
                    expected_job_revision,
                ),
            ).fetchone()
            if updated_job is None:
                raise ValueError("design Job active working-copy binding changed concurrently")
            self._record_design_job_event(
                connection,
                job=dict(updated_job),
                event_type="working_copy_bound",
                actor_id=actor_id,
                reason=f"bound working copy {working_copy_id}",
            )
        return {
            "working_copy": dict(working),
            "source_snapshot": dict(snapshot_row) if snapshot_row is not None else None,
            "job": dict(updated_job),
        }

    def reconcile_job_working_copy_publication(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        working_copy_id: str,
        model_revision_id: str | None,
        source_sha256: str | None,
        source_kind: str,
        design_origin: str,
        working_path: str,
        working_sha256: str | None,
        working_size_bytes: int | None,
        working_relative_path: str,
        actor_id: str,
        source_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Reconcile a commit-ambiguous Job binding on a fresh scoped connection."""
        with self.connection() as connection:
            job = connection.execute(
                "SELECT job.* FROM design_jobs job WHERE job.id=%s "
                "AND job.organization_id=%s AND job.design_group_id=%s "
                "AND EXISTS (SELECT 1 FROM actors actor WHERE actor.id=%s "
                "AND actor.organization_id=job.organization_id)",
                (job_id, organization_id, design_group_id, actor_id),
            ).fetchone()
            if job is None:
                return {"status": "unknown"}
            working = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s AND job_id=%s "
                "AND organization_id=%s AND design_group_id=%s",
                (working_copy_id, job_id, organization_id, design_group_id),
            ).fetchone()
            source_snapshot_id = (
                str(source_snapshot["id"])
                if isinstance(source_snapshot, dict) and source_snapshot.get("id")
                else None
            )
            snapshot = None
            if source_snapshot_id is not None:
                snapshot = connection.execute(
                    "SELECT * FROM design_job_source_snapshots WHERE id=%s AND job_id=%s "
                    "AND organization_id=%s AND design_group_id=%s",
                    (
                        source_snapshot_id,
                        job_id,
                        organization_id,
                        design_group_id,
                    ),
                ).fetchone()
            event = connection.execute(
                "SELECT * FROM design_job_events WHERE job_id=%s AND revision=%s",
                (job_id, expected_job_revision + 1),
            ).fetchone()

            if working is None and snapshot is None and event is None:
                return {"status": "not_committed"}
            if working is None or event is None or (
                source_snapshot_id is not None and snapshot is None
            ):
                return {"status": "unknown"}

            exact_working = (
                str(working.get("job_id")) == job_id
                and working.get("organization_id") == organization_id
                and working.get("design_group_id") == design_group_id
                and working.get("family_id") == family_id
                and (
                    str(working.get("source_model_revision_id"))
                    if working.get("source_model_revision_id") is not None
                    else None
                )
                == model_revision_id
                and (
                    str(working.get("source_snapshot_id"))
                    if working.get("source_snapshot_id") is not None
                    else None
                )
                == source_snapshot_id
                and int(working.get("bound_job_revision"))
                == expected_job_revision
                and (
                    working.get("source_sha256") == source_sha256
                    if source_sha256 is not None
                    else isinstance(working.get("source_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", working["source_sha256"])
                    is not None
                )
                and working.get("source_kind") == source_kind
                and working.get("design_origin") == design_origin
                and working.get("working_path") == working_path
                and (
                    working.get("working_sha256") == working_sha256
                    if working_sha256 is not None
                    else isinstance(working.get("working_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", working["working_sha256"])
                    is not None
                )
                and (
                    int(working.get("working_size_bytes")) == working_size_bytes
                    if working_size_bytes is not None
                    else type(working.get("working_size_bytes")) is int
                    and working["working_size_bytes"] > 0
                )
                and working.get("working_relative_path") == working_relative_path
                and working.get("created_by") == actor_id
            )
            exact_job = (
                str(job.get("active_working_copy_id")) == working_copy_id
                and int(job.get("revision")) == expected_job_revision + 1
                and job.get("job_type") == "mechanical_design"
                and job.get("status") == "active"
                and job.get("provisioning_state") == "ready"
                and job.get("family_id") == family_id
            )
            exact_snapshot = source_snapshot is None or (
                snapshot is not None
                and str(snapshot.get("id")) == source_snapshot_id
                and str(snapshot.get("job_id")) == job_id
                and snapshot.get("organization_id") == organization_id
                and snapshot.get("design_group_id") == design_group_id
                and (
                    str(snapshot.get("source_model_revision_id"))
                    if snapshot.get("source_model_revision_id") is not None
                    else None
                )
                == model_revision_id
                and snapshot.get("source_filename")
                == source_snapshot.get("source_filename")
                and snapshot.get("stored_path") == source_snapshot.get("stored_path")
                and int(snapshot.get("size_bytes"))
                == int(source_snapshot.get("size_bytes"))
                and snapshot.get("source_kind") == source_snapshot.get("source_kind")
                and snapshot.get("sha256") == source_snapshot.get("sha256")
                and snapshot.get("created_by") == actor_id
            )
            exact_event = (
                str(event.get("job_id")) == job_id
                and int(event.get("revision")) == expected_job_revision + 1
                and event.get("event_type") == "working_copy_bound"
                and event.get("actor_id") == actor_id
                and event.get("reason") == f"bound working copy {working_copy_id}"
                and event.get("status") == job.get("status")
                and event.get("phase") == job.get("phase")
                and event.get("provisioning_state") == job.get("provisioning_state")
                and event.get("directory_name") == job.get("directory_name")
                and event.get("blocked_reason") == job.get("blocked_reason")
            )
            if not (exact_working and exact_job and exact_snapshot and exact_event):
                return {"status": "unknown"}
            return {
                "status": "committed",
                "publication": {
                    "working_copy": dict(working),
                    "source_snapshot": dict(snapshot) if snapshot is not None else None,
                    "job": dict(job),
                },
            }

    @staticmethod
    def _authorized_knowledge_ids(
        connection: Any,
        working: dict[str, Any],
        knowledge_ids: list[str],
    ) -> set[str]:
        if not knowledge_ids:
            return set()
        rows = connection.execute(
            "SELECT id FROM knowledge_assertions WHERE id = ANY(%s::uuid[]) AND status='approved' AND ("
            "scope_kind='organization_general' OR "
            "(scope_kind='model' AND model_revision_id=%s) OR "
            "(scope_kind='product' AND product_id=%s) OR "
            "(scope_kind='family' AND family_id=%s AND design_group_id=%s) OR "
            "(scope_kind='design_group' AND family_id=%s AND design_group_id=%s))",
            (
                knowledge_ids,
                working["source_model_revision_id"],
                working.get("product_id"),
                working["family_id"],
                working["design_group_id"],
                working["family_id"],
                working["design_group_id"],
            ),
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def record_retrieval_receipt(
        self,
        *,
        working_copy_id: str,
        query: str,
        retrieval_scope: dict[str, Any],
        retrieved_knowledge_ids: list[str],
        used_knowledge_ids: list[str],
        retrieval_status: str,
        non_use_reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if retrieval_status not in {"completed", "completed_no_match", "not_executed"}:
            raise ValueError("invalid retrieval_status")
        if not query.strip():
            raise ValueError("retrieval query is required")
        if len(set(retrieved_knowledge_ids)) != len(retrieved_knowledge_ids):
            raise ValueError("retrieved knowledge IDs must be unique")
        if len(set(used_knowledge_ids)) != len(used_knowledge_ids):
            raise ValueError("used knowledge IDs must be unique")
        if not set(used_knowledge_ids).issubset(retrieved_knowledge_ids):
            raise ValueError("used knowledge IDs must be a subset of retrieved knowledge IDs")
        if retrieval_status == "completed" and not retrieved_knowledge_ids:
            raise ValueError("completed retrieval requires at least one retrieved knowledge ID")
        if retrieval_status == "completed_no_match" and (
            retrieved_knowledge_ids or used_knowledge_ids
        ):
            raise ValueError("completed_no_match requires empty retrieved and used knowledge IDs")
        if retrieval_status == "not_executed" and (
            retrieved_knowledge_ids or used_knowledge_ids
        ):
            raise ValueError("not_executed requires empty retrieved and used knowledge IDs")
        with self.connection() as connection, connection.transaction():
            working = connection.execute(
                "SELECT w.*,m.product_id FROM design_working_copies w LEFT JOIN model_revisions m "
                "ON m.id=w.source_model_revision_id WHERE w.id=%s FOR UPDATE OF w",
                (working_copy_id,),
            ).fetchone()
            if working is None:
                raise KeyError(f"unknown working_copy_id: {working_copy_id}")
            design_origin = working.get("design_origin") or (
                "new_design"
                if working.get("source_kind") == "new_design_seed"
                else "existing_model"
            )
            if design_origin == "existing_model" and not working["source_model_revision_id"]:
                raise ValueError("existing_model working copy is missing source_model_revision_id")
            authorized = self._authorized_knowledge_ids(
                connection, working, retrieved_knowledge_ids
            )
            if authorized != set(retrieved_knowledge_ids):
                raise ValueError("retrieval receipt contains unapproved or out-of-scope knowledge")
            row = connection.execute(
                "INSERT INTO design_retrieval_receipts(working_copy_id,design_origin,source_model_revision_id,"
                "family_id,query,retrieval_scope,retrieved_knowledge_ids,used_knowledge_ids,retrieval_status,"
                "non_use_reason,created_by) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s) "
                "RETURNING *",
                (
                    working_copy_id,
                    design_origin,
                    working["source_model_revision_id"],
                    working["family_id"],
                    query.strip(),
                    json.dumps(retrieval_scope, ensure_ascii=False),
                    json.dumps(retrieved_knowledge_ids),
                    json.dumps(used_knowledge_ids),
                    retrieval_status,
                    non_use_reason.strip() or None,
                    actor_id,
                ),
            ).fetchone()
        return dict(row)

    def latest_retrieval_receipt(self, working_copy_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM design_retrieval_receipts WHERE working_copy_id=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (working_copy_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def require_completed_retrieval(
        self,
        working_copy_id: str,
        *,
        expected_used_knowledge_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        receipt = self.latest_retrieval_receipt(working_copy_id)
        if receipt is None or receipt["retrieval_status"] not in {
            "completed",
            "completed_no_match",
        }:
            raise ValueError("knowledge retrieval must be completed before CAD change")
        if expected_used_knowledge_ids is not None and set(
            receipt.get("used_knowledge_ids") or []
        ) != set(expected_used_knowledge_ids):
            raise ValueError("change knowledge_used must match the latest retrieval receipt")
        return receipt

    def record_change_set(
        self,
        working_copy_id: str,
        change_phase: str,
        changes: list[dict[str, Any]],
        knowledge_used: list[str],
        rationale: str,
        actor_id: str,
        *,
        approval_envelope_draft: dict[str, Any] | None = None,
        semantic_impact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if approval_envelope_draft is not None:
            validate_approval_envelope_draft(approval_envelope_draft)
        with self.connection() as connection, connection.transaction():
            working = connection.execute(
                "SELECT w.*,m.product_id FROM design_working_copies w LEFT JOIN model_revisions m "
                "ON m.id=w.source_model_revision_id WHERE w.id=%s",
                (working_copy_id,),
            ).fetchone()
            if working is None:
                raise KeyError(f"unknown working_copy_id: {working_copy_id}")
            receipt = connection.execute(
                "SELECT * FROM design_retrieval_receipts WHERE working_copy_id=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (working_copy_id,),
            ).fetchone()
            if receipt is None or receipt["retrieval_status"] not in {
                "completed",
                "completed_no_match",
            }:
                raise ValueError("knowledge retrieval must be completed before CAD change")
            if set(receipt.get("used_knowledge_ids") or []) != set(knowledge_used):
                raise ValueError("change knowledge_used must match the latest retrieval receipt")
            authorized = self._authorized_knowledge_ids(connection, working, knowledge_used)
            if authorized != set(knowledge_used):
                raise ValueError("knowledge_used contains unapproved or out-of-scope assertions")
            active_envelope_row = connection.execute(
                "SELECT * FROM design_approval_envelopes "
                "WHERE working_copy_id=%s AND status='active' FOR UPDATE",
                (working_copy_id,),
            ).fetchone()
            active_envelope = dict(active_envelope_row) if active_envelope_row else None
            if approval_envelope_draft is not None:
                boundary_decision = {
                    "status": "requires_human_approval",
                    "requires_human_approval": True,
                    "reasons": ["new_design_intent_proposed"],
                    "rule_basis": "semantic_design_intent",
                }
            elif semantic_impact is not None:
                boundary_decision = classify_change_against_envelope(
                    active_envelope, semantic_impact
                )
            else:
                boundary_decision = {
                    "status": "requires_human_approval",
                    "requires_human_approval": True,
                    "reasons": ["incomplete_semantic_impact"],
                    "rule_basis": "semantic_design_intent",
                }
            autonomous = not boundary_decision["requires_human_approval"]
            row = connection.execute(
                "INSERT INTO design_change_sets("
                "working_copy_id,status,change_phase,changes,knowledge_used,rationale,created_by,"
                "approval_envelope_id,approval_envelope_draft,semantic_impact,boundary_decision,"
                "authorization_mode,requires_human_approval) "
                "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) "
                "RETURNING *",
                (
                    working_copy_id,
                    "approved" if autonomous else "proposed",
                    change_phase,
                    json.dumps(changes, ensure_ascii=False),
                    json.dumps(knowledge_used),
                    rationale,
                    actor_id,
                    active_envelope["id"] if autonomous else None,
                    (
                        json.dumps(approval_envelope_draft, ensure_ascii=False)
                        if approval_envelope_draft is not None
                        else None
                    ),
                    (
                        json.dumps(semantic_impact, ensure_ascii=False)
                        if semantic_impact is not None
                        else None
                    ),
                    json.dumps(boundary_decision, ensure_ascii=False),
                    "approval_envelope" if autonomous else "human_required",
                    not autonomous,
                ),
            ).fetchone()
            event_type = (
                "autonomous_authorized"
                if autonomous
                else (
                    "human_approval_required"
                    if approval_envelope_draft is not None
                    or active_envelope is None
                    else "boundary_fail_closed"
                )
            )
            connection.execute(
                "INSERT INTO design_change_audit_events("
                "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                "VALUES (%s,%s,%s,%s,%s::jsonb)",
                (
                    row["id"],
                    active_envelope["id"] if active_envelope else None,
                    event_type,
                    actor_id,
                    json.dumps(boundary_decision, ensure_ascii=False),
                ),
            )
        return dict(row)

    def review_change_set(
        self,
        change_set_id: str,
        decision: str,
        actor_id: str,
        review_text: str,
        approval_text: str = "",
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("change-set decision must be approve or reject")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute("SELECT role FROM actors WHERE id=%s", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("design change review requires family_owner")
            target_row = connection.execute(
                "SELECT * FROM design_change_sets WHERE id=%s AND status='proposed' FOR UPDATE",
                (change_set_id,),
            ).fetchone()
            if target_row is None:
                raise KeyError("change set was not found or is no longer proposed")
            target = dict(target_row)
            if decision == "reject":
                row = connection.execute(
                    "UPDATE design_change_sets SET status='rejected',reviewed_by=%s,"
                    "review_text=%s,reviewed_at=now() WHERE id=%s RETURNING *",
                    (actor_id, review_text, change_set_id),
                ).fetchone()
                connection.execute(
                    "INSERT INTO design_change_audit_events("
                    "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                    "VALUES (%s,%s,'human_rejected',%s,%s::jsonb)",
                    (
                        change_set_id,
                        target.get("approval_envelope_id"),
                        actor_id,
                        json.dumps({"decision": "reject", "review_text": review_text}),
                    ),
                )
                return dict(row)

            draft = target.get("approval_envelope_draft")
            if not isinstance(draft, dict):
                raise ValueError(
                    "approved design intent requires a complete approval_envelope_draft"
                )
            validate_approval_envelope_draft(draft)
            working = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE",
                (target["working_copy_id"],),
            ).fetchone()
            if working is None or not working.get("job_id"):
                raise ValueError("approval envelope requires a Design Job-bound working copy")
            job = connection.execute(
                "SELECT revision FROM design_jobs WHERE id=%s FOR UPDATE",
                (working["job_id"],),
            ).fetchone()
            if job is None:
                raise ValueError("approval envelope Design Job was not found")
            prior = connection.execute(
                "SELECT * FROM design_approval_envelopes "
                "WHERE working_copy_id=%s AND status='active' FOR UPDATE",
                (target["working_copy_id"],),
            ).fetchone()
            revision_row = connection.execute(
                "SELECT COALESCE(max(envelope_revision),0)+1 AS next_revision "
                "FROM design_approval_envelopes WHERE working_copy_id=%s",
                (target["working_copy_id"],),
            ).fetchone()
            envelope_revision = int(revision_row["next_revision"])
            envelope_id = str(uuid.uuid4())
            if prior is not None:
                connection.execute(
                    "UPDATE design_approval_envelopes SET status='superseded' WHERE id=%s",
                    (prior["id"],),
                )
            envelope = connection.execute(
                "INSERT INTO design_approval_envelopes("
                "id,approval_change_set_id,job_id,working_copy_id,organization_id,design_group_id,"
                "design_intent,architecture,"
                "key_interfaces,user_constraints,manufacturing_method,material_constraints,"
                "validation_requirements,approved_by,approval_text,approval_revision,envelope_revision) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,"
                "%s::jsonb,%s,%s,%s,%s) RETURNING *",
                (
                    envelope_id,
                    change_set_id,
                    working["job_id"],
                    target["working_copy_id"],
                    working["organization_id"],
                    working["design_group_id"],
                    json.dumps(draft["design_intent"], ensure_ascii=False),
                    json.dumps(draft["architecture"], ensure_ascii=False),
                    json.dumps(draft["key_interfaces"], ensure_ascii=False),
                    json.dumps(draft["user_constraints"], ensure_ascii=False),
                    json.dumps(draft["manufacturing_method"], ensure_ascii=False),
                    json.dumps(draft["material_constraints"], ensure_ascii=False),
                    json.dumps(draft["validation_requirements"], ensure_ascii=False),
                    actor_id,
                    approval_text.strip() or review_text,
                    int(job["revision"]),
                    envelope_revision,
                ),
            ).fetchone()
            if prior is not None:
                connection.execute(
                    "UPDATE design_approval_envelopes SET superseded_by=%s "
                    "WHERE id=%s",
                    (envelope["id"], prior["id"]),
                )
                connection.execute(
                    "INSERT INTO design_change_audit_events("
                    "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                    "VALUES (%s,%s,'envelope_superseded',%s,%s::jsonb)",
                    (
                        change_set_id,
                        prior["id"],
                        actor_id,
                        json.dumps({"superseded_by": str(envelope["id"])}),
                    ),
                )
            row = connection.execute(
                "UPDATE design_change_sets SET status='approved',reviewed_by=%s,review_text=%s,"
                "reviewed_at=now(),approval_envelope_id=%s,authorization_mode='human_approval',"
                "requires_human_approval=false WHERE id=%s RETURNING *",
                (actor_id, review_text, envelope["id"], change_set_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO design_change_audit_events("
                "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                "VALUES (%s,%s,'human_approved',%s,%s::jsonb)",
                (
                    change_set_id,
                    envelope["id"],
                    actor_id,
                    json.dumps(
                        {
                            "decision": "approve",
                            "review_text": review_text,
                            "approval_text": approval_text.strip() or review_text,
                            "approval_revision": int(job["revision"]),
                            "envelope_revision": envelope_revision,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        return dict(row)

    def get_active_approval_envelope(
        self, working_copy_id: str
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM design_approval_envelopes "
                "WHERE working_copy_id=%s AND status='active'",
                (working_copy_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_change_audit_events(
        self, change_set_id: str
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM design_change_audit_events WHERE change_set_id=%s "
                "ORDER BY created_at,id",
                (change_set_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def authorize_change_mutation(
        self, change_set_id: str, actor_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "SELECT * FROM design_change_sets WHERE id=%s FOR UPDATE",
                (change_set_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown change_set_id: {change_set_id}")
            change = dict(row)
            envelope = connection.execute(
                "SELECT * FROM design_approval_envelopes "
                "WHERE working_copy_id=%s AND status='active' FOR UPDATE",
                (change["working_copy_id"],),
            ).fetchone()
            active_envelope_id = str(envelope["id"]) if envelope else None
            normalized_change = {
                **change,
                "approval_envelope_id": (
                    str(change["approval_envelope_id"])
                    if change.get("approval_envelope_id")
                    else None
                ),
            }
            require_mutation_authorization(
                normalized_change, active_envelope_id=active_envelope_id
            )
            decision = {
                "status": "mutation_authorized",
                "authorization_mode": change["authorization_mode"],
                "approval_envelope_id": active_envelope_id,
            }
            connection.execute(
                "INSERT INTO design_change_audit_events("
                "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                "VALUES (%s,%s,'mutation_authorized',%s,%s::jsonb)",
                (
                    change_set_id,
                    active_envelope_id,
                    actor_id,
                    json.dumps(decision),
                ),
            )
        return {**normalized_change, "mutation_authorization": decision}

    def close_change_set(
        self,
        *,
        change_set_id: str,
        disposition: str,
        reason: str,
        actor_id: str,
        successor_change_set_id: str | None = None,
    ) -> dict[str, Any]:
        if disposition not in {"superseded", "cancelled"}:
            raise ValueError("change-set disposition must be superseded or cancelled")
        if not reason.strip():
            raise ValueError("change-set closure reason is required")
        if disposition == "superseded" and not successor_change_set_id:
            raise ValueError("superseded change set requires a successor")
        if disposition == "cancelled" and successor_change_set_id:
            raise ValueError("cancelled change set must not name a successor")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT role FROM actors WHERE id=%s", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("design change closure requires family_owner")
            target = connection.execute(
                "SELECT * FROM design_change_sets WHERE id=%s FOR UPDATE",
                (change_set_id,),
            ).fetchone()
            if target is None or target["status"] not in {"proposed", "approved"} or target["applied_at"]:
                raise KeyError("change set is not open for supersede/cancel")
            if disposition == "superseded":
                successor = connection.execute(
                    "SELECT * FROM design_change_sets WHERE id=%s FOR UPDATE",
                    (successor_change_set_id,),
                ).fetchone()
                if (
                    successor is None
                    or str(successor["working_copy_id"]) != str(target["working_copy_id"])
                    or successor["id"] == target["id"]
                    or successor["status"] not in {"approved", "applied"}
                ):
                    raise ValueError("successor must be an approved/applied change in the same working copy")
            row = connection.execute(
                "UPDATE design_change_sets SET status=%s,superseded_by_change_set_id=%s,"
                "closure_reason=%s,closed_by=%s,closed_at=now() WHERE id=%s RETURNING *",
                (
                    disposition,
                    successor_change_set_id if disposition == "superseded" else None,
                    reason.strip(),
                    actor_id,
                    change_set_id,
                ),
            ).fetchone()
        return dict(row)

    def mark_change_set_applied(
        self, change_set_id: str, resulting_sha256: str, actor_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            target_row = connection.execute(
                "SELECT * FROM design_change_sets WHERE id=%s FOR UPDATE",
                (change_set_id,),
            ).fetchone()
            if target_row is None:
                raise KeyError("change set was not found")
            target = dict(target_row)
            envelope = connection.execute(
                "SELECT * FROM design_approval_envelopes "
                "WHERE working_copy_id=%s AND status='active' FOR UPDATE",
                (target["working_copy_id"],),
            ).fetchone()
            active_envelope_id = str(envelope["id"]) if envelope else None
            require_mutation_authorization(
                {
                    **target,
                    "approval_envelope_id": (
                        str(target["approval_envelope_id"])
                        if target.get("approval_envelope_id")
                        else None
                    ),
                },
                active_envelope_id=active_envelope_id,
            )
            row = connection.execute(
                "UPDATE design_change_sets SET status='applied',applied_at=now(),resulting_sha256=%s "
                "WHERE id=%s AND status='approved' RETURNING *",
                (resulting_sha256, change_set_id),
            ).fetchone()
            if row is None:
                raise KeyError("change set was not found or has not been approved")
            connection.execute(
                "UPDATE design_working_copies SET status='draft' WHERE id=%s",
                (row["working_copy_id"],),
            )
            connection.execute(
                "INSERT INTO design_change_audit_events("
                "change_set_id,approval_envelope_id,event_type,actor_id,decision) "
                "VALUES (%s,%s,'change_applied',%s,%s::jsonb)",
                (
                    change_set_id,
                    active_envelope_id,
                    actor_id,
                    json.dumps({"resulting_sha256": resulting_sha256}),
                ),
            )
        return dict(row)

    def get_change_set(self, change_set_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM design_change_sets WHERE id=%s", (change_set_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown change_set_id: {change_set_id}")
        return dict(row)

    def get_working_copy(self, working_copy_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM design_working_copies WHERE id=%s", (working_copy_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown working_copy_id: {working_copy_id}")
        return dict(row)

    def list_legacy_working_copies(
        self, *, organization_id: str, design_group_id: str
    ) -> list[dict[str, Any]]:
        """Inventory pre-Job rows without granting them governed write authority."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM design_working_copies WHERE job_id IS NULL "
                "AND organization_id=%s AND design_group_id=%s ORDER BY created_at,id",
                (organization_id, design_group_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_legacy_migration_bindings(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> list[dict[str, Any]]:
        """Return read-only source/target evidence for Legacy migration doctor."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT legacy.id AS legacy_working_copy_id,"
                "legacy.family_id,legacy.working_path AS legacy_working_path,"
                "job.id AS migration_job_id,job.status AS migration_job_status,"
                "job.revision AS migration_job_revision,job.active_working_copy_id,"
                "migrated.id AS migrated_working_copy_id,migrated.job_id AS migrated_job_id,"
                "migrated.working_path AS migrated_working_path,"
                "migrated.working_relative_path AS migrated_working_relative_path,"
                "migrated.working_sha256 AS migrated_working_sha256,"
                "migrated.working_size_bytes AS migrated_working_size_bytes "
                "FROM design_working_copies legacy "
                "LEFT JOIN design_jobs job ON job.workspace_id=%s "
                "AND job.organization_id=legacy.organization_id "
                "AND job.design_group_id=legacy.design_group_id "
                "AND job.idempotency_token=concat('legacy-working-copy:',legacy.id::text) "
                "LEFT JOIN design_working_copies migrated "
                "ON migrated.id=job.active_working_copy_id "
                "AND migrated.job_id=job.id "
                "AND migrated.organization_id=job.organization_id "
                "AND migrated.design_group_id=job.design_group_id "
                "WHERE legacy.job_id IS NULL AND legacy.organization_id=%s "
                "AND legacy.design_group_id=%s ORDER BY legacy.created_at,legacy.id",
                (workspace_id, organization_id, design_group_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_design_lesson_summary(
        self,
        *,
        working_copy_id: str,
        summary: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        if not summary:
            raise ValueError("design lesson summary is required")
        with self.connection() as connection, connection.transaction():
            working = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE",
                (working_copy_id,),
            ).fetchone()
            if working is None:
                raise KeyError(f"unknown working_copy_id: {working_copy_id}")
            open_count = int(
                connection.execute(
                    "SELECT count(*) AS count FROM design_change_sets WHERE working_copy_id=%s "
                    "AND status IN ('proposed','approved')",
                    (working_copy_id,),
                ).fetchone()["count"]
            )
            if open_count:
                publication_status = "blocked"
                blocker = f"{open_count} design change set(s) remain open"
            elif working["status"] != "approved_for_delivery":
                publication_status = "blocked"
                blocker = "working copy is not approved_for_delivery"
            else:
                publication_status = "ready"
                blocker = None
            row = connection.execute(
                "INSERT INTO design_lesson_summaries(working_copy_id,summary,summary_status,publication_status,"
                "publication_blocker,created_by) VALUES (%s,%s::jsonb,'completed',%s,%s,%s) RETURNING *",
                (
                    working_copy_id,
                    json.dumps(summary, ensure_ascii=False),
                    publication_status,
                    blocker,
                    actor_id,
                ),
            ).fetchone()
        result = dict(row)
        result["next_action"] = (
            "prepare_design_lesson_review"
            if publication_status == "ready"
            else "resolve_publication_blocker"
        )
        return result

    def record_validation(
        self,
        working_copy_id: str,
        change_set_id: str | None,
        status: str,
        checks: list[dict[str, Any]],
        working_sha256: str,
        report_path: str = "",
        validation_kind: str = "geometry_model",
        report_sha256: str = "",
    ) -> dict[str, Any]:
        if status not in {"passed", "failed", "blocked"}:
            raise ValueError("validation status must be passed, failed, or blocked")
        with self.connection() as connection, connection.transaction():
            if change_set_id:
                change = connection.execute(
                    "SELECT * FROM design_change_sets WHERE id=%s", (change_set_id,)
                ).fetchone()
                if change is None or str(change["working_copy_id"]) != working_copy_id:
                    raise ValueError("validation change set does not belong to this working copy")
                if change["status"] != "applied" or change["resulting_sha256"] != working_sha256:
                    raise ValueError("validation requires an applied change set matching the current FCStd hash")
            row = connection.execute(
                "INSERT INTO validation_reports(working_copy_id,change_set_id,status,checks,working_sha256,report_path,"
                "validation_kind,report_sha256) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING *",
                (
                    working_copy_id,
                    change_set_id,
                    status,
                    json.dumps(checks, ensure_ascii=False),
                    working_sha256,
                    report_path or None,
                    validation_kind,
                    report_sha256 or None,
                ),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _validate_delivery_approval_scope(
        *,
        actor: Any,
        design_group: Any,
        working_copy: Any,
        organization_id: str,
        design_group_id: str,
        working_copy_id: str,
    ) -> None:
        if actor is None:
            raise PermissionError("delivery approval actor is not authorized")
        if (
            str(actor["organization_id"]) != organization_id
            or actor["role"] != "family_owner"
        ):
            raise PermissionError("delivery approval actor is outside configured scope")
        if design_group is None:
            raise KeyError("delivery approval design group is outside configured scope")
        if str(design_group["organization_id"]) != organization_id:
            raise PermissionError("delivery approval design group is outside configured scope")
        if working_copy is None:
            raise KeyError(f"delivery approval working copy is unknown: {working_copy_id}")
        if (
            str(working_copy["organization_id"]) != organization_id
            or str(working_copy["design_group_id"]) != design_group_id
        ):
            raise PermissionError("delivery approval working copy is outside configured scope")

    def authorize_delivery_approval(
        self,
        *,
        working_copy_id: str,
        actor_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> None:
        """Reject foreign delivery requests before any model or CAS access."""
        with self.connection() as connection:
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s", (actor_id,)
            ).fetchone()
            design_group = connection.execute(
                "SELECT * FROM design_groups WHERE id=%s", (design_group_id,)
            ).fetchone()
            working_copy = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s", (working_copy_id,)
            ).fetchone()
        self._validate_delivery_approval_scope(
            actor=actor,
            design_group=design_group,
            working_copy=working_copy,
            organization_id=organization_id,
            design_group_id=design_group_id,
            working_copy_id=working_copy_id,
        )

    def approve_delivery(
        self,
        working_copy_id: str,
        actor_id: str,
        confirmation: str,
        current_sha256: str,
        approved_final_artifact_path: str,
        *,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        if working_copy_id not in confirmation or "批准" not in confirmation:
            raise ValueError("delivery confirmation must include the working_copy_id and 批准")
        if (
            not isinstance(approved_final_artifact_path, str)
            or not approved_final_artifact_path.strip()
        ):
            raise ValueError("delivery approval requires an immutable final artifact")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (actor_id,)
            ).fetchone()
            design_group = connection.execute(
                "SELECT * FROM design_groups WHERE id=%s FOR UPDATE",
                (design_group_id,),
            ).fetchone()
            working_copy = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE",
                (working_copy_id,),
            ).fetchone()
            self._validate_delivery_approval_scope(
                actor=actor,
                design_group=design_group,
                working_copy=working_copy,
                organization_id=organization_id,
                design_group_id=design_group_id,
                working_copy_id=working_copy_id,
            )
            unready = connection.execute(
                "SELECT count(*) AS count FROM design_change_sets WHERE working_copy_id=%s "
                "AND status NOT IN ('applied','rejected','superseded','cancelled')",
                (working_copy_id,),
            ).fetchone()["count"]
            if int(unready):
                raise ValueError("delivery is blocked while design change sets are unreviewed or unapplied")
            summary = connection.execute(
                "SELECT * FROM design_lesson_summaries WHERE working_copy_id=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (working_copy_id,),
            ).fetchone()
            if summary is None:
                raise ValueError("delivery is blocked until design lesson summary is recorded")
            latest_by_kind = connection.execute(
                "SELECT DISTINCT ON (validation_kind) validation_kind,status,working_sha256 "
                "FROM validation_reports WHERE working_copy_id=%s "
                "ORDER BY validation_kind,created_at DESC",
                (working_copy_id,),
            ).fetchall()
            validations = {row["validation_kind"]: row for row in latest_by_kind}
            for required_kind in ("geometry_model", "assembly_completeness"):
                latest = validations.get(required_kind)
                if latest is None or latest["status"] != "passed":
                    raise ValueError(f"delivery is blocked until {required_kind} validation passes")
                if latest["working_sha256"] != current_sha256:
                    raise ValueError(f"working copy changed after {required_kind} validation; rerun it")
            row = connection.execute(
                "UPDATE design_working_copies SET status='approved_for_delivery',approved_final_sha256=%s,"
                "approved_final_artifact_path=%s "
                "WHERE id=%s RETURNING *",
                (current_sha256, approved_final_artifact_path, working_copy_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown working_copy_id: {working_copy_id}")
            updated_summary = connection.execute(
                "UPDATE design_lesson_summaries SET publication_status='ready',publication_blocker=NULL "
                "WHERE id=%s RETURNING *",
                (summary["id"],),
            ).fetchone()
            self._enqueue(
                connection,
                "design_working_copy",
                working_copy_id,
                "design_working_copy.approved",
                {
                    "working_copy_id": working_copy_id,
                    "approved_by": actor_id,
                    "confirmation": confirmation,
                    "approved_final_sha256": current_sha256,
                    "approved_final_artifact_path": approved_final_artifact_path,
                },
            )
        result = dict(row)
        result["lesson_summary"] = dict(updated_summary)
        result["lesson_review_flow"] = {
            "status": "ready",
            "next_tool": "design_lesson_stage",
        }
        return result

    def create_design_lesson_review(
        self,
        *,
        review_id: str,
        organization_id: str,
        design_group_id: str,
        working_copy_id: str,
        lesson_id: str | None,
        package_sha256: str,
        review_card_sha256: str,
        final_model_sha256: str,
        approved_final_artifact_path: str,
        review_path: str,
        package_path: str,
        actor_id: str,
        supersedes_review_id: str | None = None,
        review_outcome: str = "publish",
        pre_commit_verifier: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if review_outcome not in {"publish", "no_publish"}:
            raise ValueError("unsupported design lesson review outcome")
        if (review_outcome == "publish") != (lesson_id is not None):
            raise ValueError("design lesson review outcome and lesson_id are inconsistent")
        if (
            not isinstance(approved_final_artifact_path, str)
            or not approved_final_artifact_path.strip()
        ):
            raise ValueError(
                "design lesson review requires an immutable final artifact binding"
            )
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (actor_id,)
            ).fetchone()
            if actor is None or actor["organization_id"] != organization_id:
                raise PermissionError(
                    "design lesson review actor must belong to the configured organization"
                )
            working_copy = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s FOR UPDATE",
                (working_copy_id,),
            ).fetchone()
            if working_copy is None:
                raise KeyError(f"unknown working_copy_id: {working_copy_id}")
            if working_copy.get("job_id") is None:
                raise ValueError(
                    "JOB_MIGRATION_REQUIRED: working copy is not bound to a Design Job"
                )
            design_group = connection.execute(
                "SELECT * FROM design_groups WHERE id=%s FOR UPDATE",
                (design_group_id,),
            ).fetchone()
            if design_group is None:
                raise KeyError(f"unknown design_group_id: {design_group_id}")
            if design_group["organization_id"] != organization_id:
                raise ValueError(
                    "design group does not belong to the review organization"
                )
            if (
                working_copy["organization_id"] != organization_id
                or working_copy["design_group_id"] != design_group_id
            ):
                raise ValueError(
                    "working copy does not belong to the review organization and design group"
                )
            if working_copy.get("status") != "approved_for_delivery":
                raise ValueError("design lesson review requires a delivery-approved working copy")
            if working_copy.get("approved_final_sha256") != final_model_sha256:
                raise ValueError("design lesson review final SHA does not match delivery approval")
            if (
                working_copy.get("approved_final_artifact_path")
                != approved_final_artifact_path
            ):
                raise ValueError(
                    "design lesson review final artifact does not match delivery approval"
                )
            if supersedes_review_id:
                predecessor = connection.execute(
                    "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                    (supersedes_review_id,),
                ).fetchone()
                if predecessor is None:
                    raise KeyError(
                        f"unknown supersedes_review_id: {supersedes_review_id}"
                    )
                if predecessor["organization_id"] != organization_id:
                    raise ValueError(
                        "replacement review and predecessor must belong to the same organization"
                    )
                if any(
                    (
                        predecessor["design_group_id"] != design_group_id,
                        str(predecessor["working_copy_id"]) != str(working_copy_id),
                        predecessor["final_model_sha256"] != final_model_sha256,
                    )
                ):
                    raise ValueError(
                        "replacement review and predecessor must describe the same delivered model"
                    )
                if predecessor["status"] != "awaiting-engineer-review":
                    raise ValueError(
                        "superseded review must be awaiting-engineer-review"
                    )
                predecessor = connection.execute(
                    "UPDATE design_lesson_reviews SET status='superseded' "
                    "WHERE id=%s AND status='awaiting-engineer-review' RETURNING *",
                    (supersedes_review_id,),
                ).fetchone()
                if predecessor is None:
                    raise RuntimeError("design lesson review state changed while locked")
                self._enqueue_design_lesson_review_event(
                    connection,
                    event_type="design_lesson_review.superseded",
                    review=predecessor,
                    superseded_by_review_id=review_id,
                )

            row = connection.execute(
                "INSERT INTO design_lesson_reviews(id,organization_id,design_group_id,working_copy_id,lesson_id,"
                "package_sha256,review_card_sha256,final_model_sha256,status,review_path,package_path,created_by,"
                "supersedes_review_id,approved_final_artifact_path,review_outcome) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting-engineer-review',%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (
                    review_id,
                    organization_id,
                    design_group_id,
                    working_copy_id,
                    lesson_id,
                    package_sha256,
                    review_card_sha256,
                    final_model_sha256,
                    review_path,
                    package_path,
                    actor_id,
                    supersedes_review_id,
                    approved_final_artifact_path,
                    review_outcome,
                ),
            ).fetchone()
            self._enqueue_design_lesson_review_event(
                connection,
                event_type="design_lesson_review.prepared",
                review=row,
            )
            if pre_commit_verifier is not None:
                pre_commit_verifier()
        return dict(row)

    def record_design_lesson_review_no_publish(
        self,
        *,
        review_id: str,
        reviewer_id: str,
        reviewer_text: str,
        decision_receipt_sha256: str,
        decision_receipt_path: str,
        pre_commit_verifier: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if not reviewer_text.strip():
            raise ValueError("no-publication reviewer text is required")
        if (
            len(decision_receipt_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in decision_receipt_sha256.lower()
            )
        ):
            raise ValueError("decision_receipt_sha256 must be a full SHA-256 digest")
        if not decision_receipt_path.strip():
            raise ValueError("decision_receipt_path is required")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            review = connection.execute(
                "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                (review_id,),
            ).fetchone()
            if review is None:
                raise KeyError(f"unknown design lesson review: {review_id}")
            if (
                actor is None
                or actor["role"] != "family_owner"
                or actor["organization_id"] != review["organization_id"]
            ):
                raise PermissionError(
                    "design lesson no-publication decision requires an authorized family owner"
                )
            if review.get("review_outcome") != "no_publish":
                raise ValueError("no-publication decision requires a no_publish review")
            if review["status"] == "reviewed-no-publishable-lesson":
                if (
                    review.get("decision_receipt_sha256") == decision_receipt_sha256
                    and review.get("decision_receipt_path") == decision_receipt_path
                ):
                    return dict(review)
                raise ValueError("no-publication decision receipt diverged")
            if review["status"] != "awaiting-engineer-review":
                raise ValueError("design lesson review must be awaiting-engineer-review")
            if pre_commit_verifier is not None:
                pre_commit_verifier()
            row = connection.execute(
                "UPDATE design_lesson_reviews SET status='reviewed-no-publishable-lesson',"
                "reviewed_by=%s,reviewed_at=now(),reviewer_text=%s,"
                "confirmation_mode='single_confirmation',decision_receipt_sha256=%s,"
                "decision_receipt_path=%s WHERE id=%s AND status='awaiting-engineer-review' "
                "RETURNING *",
                (
                    reviewer_id,
                    reviewer_text,
                    decision_receipt_sha256,
                    decision_receipt_path,
                    review_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("design lesson review state changed while locked")
            self._enqueue_design_lesson_review_event(
                connection,
                event_type="design_lesson_review.no_publish",
                review=row,
            )
            if pre_commit_verifier is not None:
                pre_commit_verifier()
        return dict(row)

    def get_design_lesson_review(self, review_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT r.*,w.job_id FROM design_lesson_reviews r "
                "JOIN design_working_copies w ON w.id=r.working_copy_id "
                "WHERE r.id=%s",
                (review_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown design lesson review: {review_id}")
        return dict(row)

    def reject_design_lesson_review(
        self, *, review_id: str, reviewer_id: str, reviewer_text: str
    ) -> dict[str, Any]:
        if not reviewer_text.strip():
            raise ValueError("design lesson review rejection text is required")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            if actor is None:
                raise PermissionError(
                    "design lesson review actor must belong to the configured organization"
                )
            review = connection.execute(
                "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                (review_id,),
            ).fetchone()
            if review is None:
                raise KeyError(f"unknown design lesson review: {review_id}")
            if actor["organization_id"] != review["organization_id"]:
                raise PermissionError(
                    "design lesson review actor must belong to the configured organization"
                )
            if review["status"] != "awaiting-engineer-review":
                raise ValueError(
                    "design lesson review must be awaiting-engineer-review"
                )
            row = connection.execute(
                "UPDATE design_lesson_reviews SET status='rejected',reviewed_by=%s,reviewed_at=now(),reviewer_text=%s "
                "WHERE id=%s AND status='awaiting-engineer-review' RETURNING *",
                (reviewer_id, reviewer_text, review_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("design lesson review state changed while locked")
            self._enqueue_design_lesson_review_event(
                connection,
                event_type="design_lesson_review.rejected",
                review=row,
            )
        return dict(row)

    def invalidate_design_lesson_review(
        self, *, review_id: str, reviewer_id: str, reason: str
    ) -> dict[str, Any]:
        """Record terminal immutable-binding drift without publishing a lesson."""
        if not reason.strip():
            raise ValueError("design lesson review invalidation reason is required")
        with self.connection() as connection, connection.transaction():
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s FOR UPDATE", (reviewer_id,)
            ).fetchone()
            review = connection.execute(
                "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                (review_id,),
            ).fetchone()
            if review is None:
                raise KeyError(f"unknown design lesson review: {review_id}")
            if (
                actor is None
                or actor["organization_id"] != review["organization_id"]
                or actor["role"] != "family_owner"
            ):
                raise PermissionError(
                    "design lesson review actor must be an authorized family owner"
                )
            if review["status"] != "awaiting-engineer-review":
                raise ValueError(
                    "design lesson review must be awaiting-engineer-review"
                )
            row = connection.execute(
                "UPDATE design_lesson_reviews SET status='invalid',reviewed_by=%s,reviewed_at=now(),reviewer_text=%s "
                "WHERE id=%s AND status='awaiting-engineer-review' RETURNING *",
                (reviewer_id, reason, review_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("design lesson review state changed while locked")
            self._enqueue_design_lesson_review_event(
                connection,
                event_type="design_lesson_review.invalid",
                review=row,
            )
        return dict(row)

    def record_design_lesson_review_probe(
        self, *, review_id: str, probe: dict[str, Any], successful: bool
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            review = connection.execute(
                "SELECT * FROM design_lesson_reviews WHERE id=%s FOR UPDATE",
                (review_id,),
            ).fetchone()
            if review is None:
                raise KeyError(f"unknown design lesson review: {review_id}")
            if review["status"] != "approved-retrieval-pending":
                raise ValueError(
                    "design lesson review must be approved-retrieval-pending"
                )
            serialized_probe = json.dumps(probe, ensure_ascii=False)
            if successful:
                row = connection.execute(
                    "UPDATE design_lesson_reviews SET retrieval_probe=%s::jsonb,status='stored-and-retrievable',"
                    "retrieval_verified_at=now() WHERE id=%s AND status='approved-retrieval-pending' RETURNING *",
                    (serialized_probe, review_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "UPDATE design_lesson_reviews SET retrieval_probe=%s::jsonb "
                    "WHERE id=%s AND status='approved-retrieval-pending' RETURNING *",
                    (serialized_probe, review_id),
                ).fetchone()
            if row is None:
                raise RuntimeError("design lesson review state changed while locked")
            if successful:
                self._enqueue_design_lesson_review_event(
                    connection,
                    event_type="design_lesson_review.retrieval_verified",
                    review=row,
                )
        return dict(row)

    def processed_design_lesson_review_projection_witnesses(
        self, *, review_id: str, lesson_id: str
    ) -> list[dict[str, str]]:
        """Return only the two exact lifecycle events durably acked by projection."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id::text AS event_id,event_type,aggregate_type,aggregate_id "
                "FROM outbox_events WHERE processed_at IS NOT NULL AND ("
                "(event_type='design_lesson.approved' AND aggregate_type='design_lesson' AND aggregate_id=%s) OR "
                "(event_type='design_lesson_review.approved' AND aggregate_type='design_lesson_review' AND aggregate_id=%s)) "
                "ORDER BY CASE event_type WHEN 'design_lesson.approved' THEN 0 ELSE 1 END,id",
                (lesson_id, review_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def design_lesson_review_context(
        self,
        working_copy_id: str,
        *,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            working_copy = connection.execute(
                "SELECT * FROM design_working_copies WHERE id=%s AND organization_id=%s "
                "AND design_group_id=%s AND status='approved_for_delivery'",
                (working_copy_id, organization_id, design_group_id),
            ).fetchone()
            if working_copy is None:
                raise KeyError(
                    f"working copy is unknown or not delivery-approved: {working_copy_id}"
                )
            changes = connection.execute(
                "SELECT * FROM design_change_sets WHERE working_copy_id=%s AND status='applied' "
                "ORDER BY created_at,id",
                (working_copy_id,),
            ).fetchall()
            validations = connection.execute(
                "SELECT * FROM validation_reports WHERE working_copy_id=%s ORDER BY created_at,id",
                (working_copy_id,),
            ).fetchall()
            standard_parts = connection.execute(
                "SELECT * FROM standard_part_records WHERE metadata->>'working_copy_id'=%s "
                "AND metadata->>'model_sha256'=%s ORDER BY provider_id,part_number,id",
                (working_copy_id, working_copy["approved_final_sha256"]),
            ).fetchall()
        return {
            "working_copy": dict(working_copy),
            "change_sets": [dict(row) for row in changes],
            "validation_reports": [dict(row) for row in validations],
            "standard_part_provenance": [dict(row) for row in standard_parts],
        }

    def register_standard_part(self, **record: Any) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "INSERT INTO standard_part_records(provider_id,provider_name,trust_tier,part_number,standard,"
                "nominal_size,source_url,sha256,local_path,manifest_path,metadata,approval_reference,validation_report_path,approved_at) "
                "VALUES (%(provider_id)s,%(provider_name)s,%(trust_tier)s,%(part_number)s,%(standard)s,"
                "%(nominal_size)s,%(source_url)s,%(sha256)s,%(local_path)s,%(manifest_path)s,%(metadata)s::jsonb,"
                "%(approval_reference)s,%(validation_report_path)s,now()) "
                "ON CONFLICT(provider_id,part_number,sha256) DO UPDATE SET local_path=EXCLUDED.local_path,"
                "manifest_path=EXCLUDED.manifest_path,metadata=EXCLUDED.metadata,approval_reference=EXCLUDED.approval_reference,"
                "validation_report_path=EXCLUDED.validation_report_path,approved_at=EXCLUDED.approved_at RETURNING *",
                {**record, "metadata": json.dumps(record.get("metadata", {}), ensure_ascii=False)},
            ).fetchone()
        return dict(row)

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox_events WHERE processed_at IS NULL ORDER BY created_at LIMIT %s", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(
        self,
        *,
        worker_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
        aggregate_type: str | None = None,
    ) -> list[dict[str, Any]]:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("outbox worker_id is required")
        if not 1 <= limit <= 500:
            raise ValueError("outbox claim limit must be between 1 and 500")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("outbox lease_seconds must be between 1 and 3600")
        type_filter = ""
        parameters: list[Any] = [lease_seconds]
        if aggregate_type is not None:
            if not aggregate_type.strip():
                raise ValueError("aggregate_type filter must be nonblank")
            type_filter = " AND aggregate_type=%s"
            parameters.append(aggregate_type)
        parameters.extend([limit, worker_id])
        with self.connection() as connection, connection.transaction():
            rows = connection.execute(
                "WITH claimable AS (SELECT id FROM outbox_events WHERE processed_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at < now()-make_interval(secs => %s))"
                + type_filter
                + " ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT %s) "
                "UPDATE outbox_events AS event SET claimed_by=%s,claimed_at=now() "
                "FROM claimable WHERE event.id=claimable.id RETURNING event.*,"
                "to_char(event.created_at AT TIME ZONE 'UTC',"
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS projection_occurred_at",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_outbox(self, event_id: str, *, worker_id: str, error: str = "") -> None:
        if not worker_id.strip():
            raise ValueError("outbox worker_id is required")
        with self.connection() as connection, connection.transaction():
            if error:
                updated = connection.execute(
                    "UPDATE outbox_events SET attempts=attempts+1,last_error=%s,claimed_by=NULL,claimed_at=NULL "
                    "WHERE id=%s AND claimed_by=%s AND processed_at IS NULL RETURNING id",
                    (error, event_id, worker_id),
                ).fetchone()
            else:
                updated = connection.execute(
                    "UPDATE outbox_events SET processed_at=now(),attempts=attempts+1,last_error=NULL,"
                    "claimed_by=NULL,claimed_at=NULL WHERE id=%s AND claimed_by=%s AND processed_at IS NULL RETURNING id",
                    (event_id, worker_id),
                ).fetchone()
            if updated is None:
                raise RuntimeError("outbox event is not leased by this worker")

    @staticmethod
    def _require_onboarding_job(
        connection: Any,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        job = connection.execute(
            "SELECT * FROM design_jobs WHERE id=%s AND organization_id=%s "
            "AND design_group_id=%s AND EXISTS (SELECT 1 FROM actors actor "
            "WHERE actor.id=%s AND actor.organization_id=design_jobs.organization_id) "
            "FOR UPDATE",
            (job_id, organization_id, design_group_id, actor_id),
        ).fetchone()
        if job is None:
            raise KeyError("unknown product family onboarding Job or unauthorized")
        if int(job["revision"]) != expected_job_revision:
            raise ValueError("stale design job revision")
        if (
            job["job_type"] != "product_family_onboarding"
            or job["status"] != "active"
            or job["provisioning_state"] != "ready"
            or job.get("family_id") != family_id
        ):
            raise ValueError(
                "operation requires an active ready product_family_onboarding Job in the same family"
            )
        return dict(job)

    def start_product_family_onboarding(
        self,
        *,
        run_id: str,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str,
        input_manifest: dict[str, Any],
        input_manifest_sha256: str,
        snapshots: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            job = self._require_onboarding_job(
                connection,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                actor_id=actor_id,
            )
            existing = connection.execute(
                "SELECT * FROM product_family_onboarding_runs WHERE job_id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["id"]) != run_id
                    or existing["input_manifest_sha256"] != input_manifest_sha256
                    or existing["input_manifest"] != input_manifest
                ):
                    raise ValueError("onboarding Job is already bound to different inputs")
                return {"run": dict(existing), "job": job, "changed": False}
            for snapshot in snapshots:
                connection.execute(
                    "INSERT INTO design_job_source_snapshots(id,job_id,organization_id,design_group_id,"
                    "source_model_revision_id,source_filename,stored_path,sha256,size_bytes,source_kind,created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'product_family_input',%s)",
                    (
                        snapshot["id"],
                        job_id,
                        organization_id,
                        design_group_id,
                        None,
                        snapshot["source_filename"],
                        snapshot["stored_path"],
                        snapshot["sha256"],
                        snapshot["size_bytes"],
                        actor_id,
                    ),
                )
            run = connection.execute(
                "INSERT INTO product_family_onboarding_runs(id,job_id,organization_id,design_group_id,"
                "family_id,status,input_manifest,input_manifest_sha256,started_job_revision,created_by) "
                "VALUES (%s,%s,%s,%s,%s,'started',%s::jsonb,%s,%s,%s) RETURNING *",
                (
                    run_id,
                    job_id,
                    organization_id,
                    design_group_id,
                    family_id,
                    json.dumps(input_manifest, ensure_ascii=False),
                    input_manifest_sha256,
                    expected_job_revision + 1,
                    actor_id,
                ),
            ).fetchone()
            updated_job = connection.execute(
                "UPDATE design_jobs SET revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s RETURNING *",
                (job_id, expected_job_revision),
            ).fetchone()
            if updated_job is None:
                raise ValueError("stale design job revision")
            self._record_design_job_event(
                connection,
                job=dict(updated_job),
                event_type="transitioned",
                actor_id=actor_id,
                reason="product family onboarding inputs captured",
            )
        return {"run": dict(run), "job": dict(updated_job), "changed": True}

    def analyze_product_family_onboarding(
        self,
        *,
        run_id: str,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str,
        analysis: dict[str, Any],
        analysis_sha256: str,
        analysis_path: str,
        candidate_knowledge: list[dict[str, Any]],
        package_sha256: str,
        package_path: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            job = self._require_onboarding_job(
                connection,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                actor_id=actor_id,
            )
            run = connection.execute(
                "SELECT * FROM product_family_onboarding_runs WHERE id=%s AND job_id=%s "
                "AND organization_id=%s AND design_group_id=%s FOR UPDATE",
                (run_id, job_id, organization_id, design_group_id),
            ).fetchone()
            if run is None:
                raise KeyError("product family onboarding run is missing")
            if run["status"] != "started":
                if (
                    run.get("analysis_sha256") == analysis_sha256
                    and run.get("package_sha256") == package_sha256
                    and run.get("analysis") == analysis
                    and run.get("candidate_knowledge") == candidate_knowledge
                ):
                    return {"run": dict(run), "job": job, "changed": False}
                raise ValueError("onboarding analysis is already immutable")
            run = connection.execute(
                "UPDATE product_family_onboarding_runs SET status='analyzed',analysis=%s::jsonb,"
                "analysis_sha256=%s,analysis_path=%s,candidate_knowledge=%s::jsonb,"
                "package_sha256=%s,package_path=%s,analyzed_job_revision=%s,analyzed_at=now() "
                "WHERE id=%s RETURNING *",
                (
                    json.dumps(analysis, ensure_ascii=False),
                    analysis_sha256,
                    analysis_path,
                    json.dumps(candidate_knowledge, ensure_ascii=False),
                    package_sha256,
                    package_path,
                    expected_job_revision + 1,
                    run_id,
                ),
            ).fetchone()
            updated_job = connection.execute(
                "UPDATE design_jobs SET phase='analysis',revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s RETURNING *",
                (job_id, expected_job_revision),
            ).fetchone()
            if updated_job is None:
                raise ValueError("stale design job revision")
            self._record_design_job_event(
                connection,
                job=dict(updated_job),
                event_type="transitioned",
                actor_id=actor_id,
                reason="product family analysis and knowledge candidates recorded",
            )
        return {"run": dict(run), "job": dict(updated_job), "changed": True}

    def review_product_family_onboarding(
        self,
        *,
        review_id: str,
        review_identity: str,
        run_id: str,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str,
        package_sha256: str,
        decision: str,
        reviewer_id: str,
        reviewer_text: str,
        review_path: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            job = self._require_onboarding_job(
                connection,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                actor_id=reviewer_id,
            )
            actor = connection.execute(
                "SELECT * FROM actors WHERE id=%s AND organization_id=%s FOR SHARE",
                (reviewer_id, organization_id),
            ).fetchone()
            if actor is None or actor["role"] != "family_owner":
                raise PermissionError("product family onboarding review requires family_owner")
            run = connection.execute(
                "SELECT * FROM product_family_onboarding_runs WHERE id=%s AND job_id=%s "
                "AND organization_id=%s AND design_group_id=%s FOR UPDATE",
                (run_id, job_id, organization_id, design_group_id),
            ).fetchone()
            if run is None or run["status"] != "analyzed":
                existing = connection.execute(
                    "SELECT * FROM product_family_onboarding_reviews WHERE run_id=%s",
                    (run_id,),
                ).fetchone()
                if (
                    existing is not None
                    and existing["review_identity"] == review_identity
                    and existing["package_sha256"] == package_sha256
                    and existing["decision"] == decision
                ):
                    return {"review": dict(existing), "job": job, "changed": False}
                raise ValueError("onboarding run is not awaiting review")
            if run["package_sha256"] != package_sha256:
                raise ValueError("review package identity does not match onboarding analysis")
            review = connection.execute(
                "INSERT INTO product_family_onboarding_reviews(id,run_id,job_id,organization_id,"
                "design_group_id,family_id,package_sha256,review_identity,decision,reviewer_id,"
                "reviewer_text,review_path,reviewed_job_revision) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    review_id,
                    run_id,
                    job_id,
                    organization_id,
                    design_group_id,
                    family_id,
                    package_sha256,
                    review_identity,
                    decision,
                    reviewer_id,
                    reviewer_text,
                    review_path,
                    expected_job_revision + 1,
                ),
            ).fetchone()
            run_status = "approved" if decision == "approve" else "rejected"
            connection.execute(
                "UPDATE product_family_onboarding_runs SET status=%s WHERE id=%s",
                (run_status, run_id),
            )
            updated_job = connection.execute(
                "UPDATE design_jobs SET phase='knowledge_review',revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s RETURNING *",
                (job_id, expected_job_revision),
            ).fetchone()
            if updated_job is None:
                raise ValueError("stale design job revision")
            self._record_design_job_event(
                connection,
                job=dict(updated_job),
                event_type="transitioned",
                actor_id=reviewer_id,
                reason=f"product family knowledge review {decision}",
            )
        return {"review": dict(review), "job": dict(updated_job), "changed": True}

    def publish_product_family_onboarding(
        self,
        *,
        publication_id: str,
        publication_identity: str,
        publication_receipt_sha256: str,
        publication_path: str,
        assertion_ids: list[str],
        run_id: str,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str,
        package_sha256: str,
        review_identity: str,
        candidates: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection.transaction():
            job = self._require_onboarding_job(
                connection,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                actor_id=actor_id,
            )
            existing = connection.execute(
                "SELECT * FROM product_family_onboarding_publications WHERE run_id=%s FOR SHARE",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["publication_identity"] != publication_identity
                    or existing["publication_receipt_sha256"]
                    != publication_receipt_sha256
                    or existing["package_sha256"] != package_sha256
                ):
                    raise ValueError("onboarding publication identity diverged")
                return {"publication": dict(existing), "job": job, "changed": False}
            run = connection.execute(
                "SELECT * FROM product_family_onboarding_runs WHERE id=%s AND job_id=%s "
                "AND organization_id=%s AND design_group_id=%s FOR UPDATE",
                (run_id, job_id, organization_id, design_group_id),
            ).fetchone()
            review = connection.execute(
                "SELECT * FROM product_family_onboarding_reviews WHERE run_id=%s "
                "AND review_identity=%s FOR SHARE",
                (run_id, review_identity),
            ).fetchone()
            if (
                run is None
                or run["status"] != "approved"
                or run["package_sha256"] != package_sha256
                or run["candidate_knowledge"] != candidates
                or review is None
                or review["decision"] != "approve"
                or review["package_sha256"] != package_sha256
            ):
                raise ValueError("approved onboarding review does not match publication")
            if len(assertion_ids) != len(candidates) or len(set(assertion_ids)) != len(assertion_ids):
                raise ValueError("publication assertion identities are invalid")
            for assertion_id, candidate in zip(assertion_ids, candidates, strict=True):
                evidence = list(candidate["evidence"])
                evidence.append(
                    {
                        "onboarding_job_id": job_id,
                        "onboarding_run_id": run_id,
                        "package_sha256": package_sha256,
                        "review_identity": review_identity,
                    }
                )
                connection.execute(
                    "INSERT INTO knowledge_assertions(id,organization_id,design_group_id,family_id,"
                    "subject_ref,predicate,object_value,unit,scope_kind,risk_level,status,source_kind,"
                    "evidence,confidence,applicability,non_applicable_conditions,contradicts,created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'family',%s,'approved',%s,"
                    "%s::jsonb,%s,%s::jsonb,%s::jsonb,'[]'::jsonb,%s)",
                    (
                        assertion_id,
                        organization_id,
                        design_group_id,
                        family_id,
                        candidate["subject_ref"],
                        candidate["predicate"],
                        json.dumps(candidate["object_value"], ensure_ascii=False),
                        candidate.get("unit"),
                        candidate["risk_level"],
                        candidate["source_kind"],
                        json.dumps(evidence, ensure_ascii=False),
                        candidate["confidence"],
                        json.dumps(candidate["applicability"], ensure_ascii=False),
                        json.dumps(
                            candidate["non_applicable_conditions"], ensure_ascii=False
                        ),
                        actor_id,
                    ),
                )
                object_value = candidate["object_value"]
                exact_terms = sorted(
                    set(_search_terms(object_value))
                    | {
                        str(candidate["subject_ref"]).strip().lower(),
                        str(candidate["predicate"]).strip().lower(),
                    }
                )
                search_text = " ".join(
                    [
                        str(candidate["subject_ref"]),
                        str(candidate["predicate"]),
                        json.dumps(object_value, ensure_ascii=False, sort_keys=True),
                    ]
                )
                connection.execute(
                    "INSERT INTO knowledge_search_documents(assertion_id,organization_id,design_group_id,"
                    "family_id,exact_terms,search_text) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        assertion_id,
                        organization_id,
                        design_group_id,
                        family_id,
                        exact_terms,
                        search_text,
                    ),
                )
                self._enqueue(
                    connection,
                    "knowledge_assertion",
                    assertion_id,
                    "knowledge_assertion.reviewed",
                    {
                        "assertion_id": assertion_id,
                        "status": "approved",
                        "onboarding_job_id": job_id,
                        "onboarding_run_id": run_id,
                        "publication_identity": publication_identity,
                    },
                )
            publication = connection.execute(
                "INSERT INTO product_family_onboarding_publications(id,run_id,review_id,job_id,"
                "organization_id,design_group_id,family_id,package_sha256,publication_identity,"
                "publication_receipt_sha256,publication_path,assertion_ids,published_job_revision,published_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *",
                (
                    publication_id,
                    run_id,
                    review["id"],
                    job_id,
                    organization_id,
                    design_group_id,
                    family_id,
                    package_sha256,
                    publication_identity,
                    publication_receipt_sha256,
                    publication_path,
                    json.dumps(assertion_ids),
                    expected_job_revision + 1,
                    actor_id,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE product_family_onboarding_runs SET status='published' WHERE id=%s",
                (run_id,),
            )
            updated_job = connection.execute(
                "UPDATE design_jobs SET status='completed',phase='completed',blocked_reason=NULL,"
                "active_working_copy_id=NULL,revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s RETURNING *",
                (job_id, expected_job_revision),
            ).fetchone()
            if updated_job is None:
                raise ValueError("stale design job revision")
            self._record_design_job_event(
                connection,
                job=dict(updated_job),
                event_type="transitioned",
                actor_id=actor_id,
                reason="product family onboarding knowledge published",
            )
        return {
            "publication": dict(publication),
            "job": dict(updated_job),
            "changed": True,
        }

    def get_product_family_onboarding(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM product_family_onboarding_runs WHERE job_id=%s "
                "AND organization_id=%s AND design_group_id=%s",
                (job_id, organization_id, design_group_id),
            ).fetchone()
            if run is None:
                raise KeyError("product family onboarding run is missing or unauthorized")
            review = connection.execute(
                "SELECT * FROM product_family_onboarding_reviews WHERE run_id=%s",
                (run["id"],),
            ).fetchone()
            publication = connection.execute(
                "SELECT * FROM product_family_onboarding_publications WHERE run_id=%s",
                (run["id"],),
            ).fetchone()
        result = dict(run)
        result["review"] = dict(review) if review is not None else None
        result["publication"] = dict(publication) if publication is not None else None
        return result

    def create_design_job(
        self,
        *,
        job_id: str,
        workspace_id: str,
        display_date: str,
        job_type: str,
        title: str,
        slug: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        idempotency_token: str,
        actor_id: str,
    ) -> dict[str, Any]:
        initial_phases = {
            "mechanical_design": "requirements",
            "product_family_onboarding": "intake",
        }
        if job_type not in initial_phases:
            raise ValueError("invalid design job type")
        try:
            parsed_display_date = date.fromisoformat(display_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("display_date must use YYYY-MM-DD") from exc
        if parsed_display_date.isoformat() != display_date:
            raise ValueError("display_date must use YYYY-MM-DD")
        for field, value in (
            ("job_id", job_id),
            ("workspace_id", workspace_id),
            ("title", title),
            ("slug", slug),
            ("organization_id", organization_id),
            ("design_group_id", design_group_id),
            ("idempotency_token", idempotency_token),
            ("actor_id", actor_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} is required")
        with self.connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"design-job-token:{workspace_id}:{idempotency_token}",),
            )
            row = connection.execute(
                "SELECT * FROM design_jobs WHERE workspace_id=%s AND idempotency_token=%s "
                "AND organization_id=%s AND design_group_id=%s",
                (
                    workspace_id,
                    idempotency_token,
                    organization_id,
                    design_group_id,
                ),
            ).fetchone()
            if row is None:
                date_token = parsed_display_date.strftime("%Y%m%d")
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"design-job-display:{workspace_id}:{date_token}",),
                )
                created = False
                for _ in range(4):
                    sequence_row = connection.execute(
                        "SELECT COALESCE(MAX(CAST(substring(display_id FROM 14) AS integer)),0)+1 "
                        "AS next_sequence FROM design_jobs WHERE workspace_id=%s "
                        "AND display_id LIKE %s",
                        (workspace_id, f"JOB-{date_token}-%"),
                    ).fetchone()
                    if sequence_row is None:
                        raise RuntimeError("design Job display sequence allocation failed")
                    display_id = (
                        f"JOB-{date_token}-{int(sequence_row['next_sequence']):03d}"
                    )
                    row = connection.execute(
                        "INSERT INTO design_jobs(id,workspace_id,display_id,job_type,title,slug,status,phase,"
                        "organization_id,design_group_id,family_id,idempotency_token,created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING RETURNING *",
                        (
                            job_id,
                            workspace_id,
                            display_id,
                            job_type,
                            title.strip(),
                            slug.strip(),
                            "active",
                            initial_phases[job_type],
                            organization_id,
                            design_group_id,
                            family_id,
                            idempotency_token,
                            actor_id,
                        ),
                    ).fetchone()
                    if row is not None:
                        created = True
                        break
                    row = connection.execute(
                        "SELECT * FROM design_jobs WHERE workspace_id=%s AND idempotency_token=%s "
                        "AND organization_id=%s AND design_group_id=%s",
                        (
                            workspace_id,
                            idempotency_token,
                            organization_id,
                            design_group_id,
                        ),
                    ).fetchone()
                    if row is not None:
                        break
                if row is None:
                    raise KeyError("unknown design_job_id or unauthorized")
                if created:
                    self._record_design_job_event(
                        connection,
                        job=dict(row),
                        event_type="created",
                        actor_id=actor_id,
                    )
        return dict(row)

    def record_design_job_directory(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        expected_revision: int,
        directory_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(directory_name, str) or not directory_name.strip():
            raise ValueError("directory_name is required")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValueError("actor_id is required")
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE design_jobs SET directory_name=%s,provisioning_state='ready',"
                "revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s AND provisioning_state='provisioning' "
                "AND organization_id=%s AND design_group_id=%s AND directory_name IS NULL "
                "AND EXISTS (SELECT 1 FROM actors actor WHERE actor.id=%s "
                "AND actor.organization_id=design_jobs.organization_id) RETURNING *",
                (
                    directory_name.strip(),
                    job_id,
                    expected_revision,
                    organization_id,
                    design_group_id,
                    actor_id,
                ),
            ).fetchone()
            if row is None:
                self._raise_design_job_write_failure(
                    connection,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                    operation="directory",
                )
            self._record_design_job_event(
                connection,
                job=dict(row),
                event_type="directory_recorded",
                actor_id=actor_id,
            )
        return dict(row)

    def transition_design_job(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        expected_revision: int,
        status: str,
        phase: str,
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if status not in {"active", "blocked", "completed", "cancelled", "archived"}:
            raise ValueError("invalid design job status")
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("phase is required")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValueError("actor_id is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("design job transition reason is required")
        reason_text = reason.strip()
        blocked_reason = (
            json.dumps({"reason": reason_text}, ensure_ascii=False)
            if status == "blocked"
            else None
        )
        active_working_copy_update = (
            ",active_working_copy_id=NULL"
            if status in {"completed", "cancelled", "archived"}
            else ""
        )
        with self.connection() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE design_jobs SET status=%s,phase=%s,blocked_reason=%s::jsonb"
                + active_working_copy_update
                + ","
                "revision=revision+1,updated_at=now() WHERE id=%s AND revision=%s "
                "AND organization_id=%s AND design_group_id=%s AND provisioning_state='ready' "
                "AND EXISTS (SELECT 1 FROM actors actor WHERE actor.id=%s "
                "AND actor.organization_id=design_jobs.organization_id) RETURNING *",
                (
                    status,
                    phase.strip(),
                    blocked_reason,
                    job_id,
                    expected_revision,
                    organization_id,
                    design_group_id,
                    actor_id,
                ),
            ).fetchone()
            if row is None:
                self._raise_design_job_write_failure(
                    connection,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                    operation="transition",
                )
            self._record_design_job_event(
                connection,
                job=dict(row),
                event_type="transitioned",
                actor_id=actor_id,
                reason=reason_text,
            )
        return dict(row)

    def list_job_working_copies(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            authorized = connection.execute(
                "SELECT id FROM design_jobs WHERE id=%s AND organization_id=%s "
                "AND design_group_id=%s AND EXISTS (SELECT 1 FROM actors actor "
                "WHERE actor.id=%s AND actor.organization_id=design_jobs.organization_id)",
                (job_id, organization_id, design_group_id, actor_id),
            ).fetchone()
            if authorized is None:
                raise KeyError("unknown design_job_id or unauthorized")
            rows = connection.execute(
                "SELECT id,job_id,organization_id,design_group_id,working_path,"
                "working_relative_path,working_sha256,working_size_bytes,"
                "COALESCE((SELECT resulting_sha256 FROM design_change_sets c "
                "WHERE c.working_copy_id=design_working_copies.id AND c.status='applied' "
                "ORDER BY c.applied_at DESC,c.created_at DESC,c.id DESC LIMIT 1),"
                "working_sha256) AS delivery_recovery_sha256,"
                "CASE WHEN EXISTS (SELECT 1 FROM design_change_sets c "
                "WHERE c.working_copy_id=design_working_copies.id AND c.status='applied') "
                "THEN 'latest_applied_change_set' ELSE 'working_copy_binding' "
                "END AS delivery_recovery_source "
                "FROM design_working_copies WHERE job_id=%s AND organization_id=%s "
                "AND design_group_id=%s ORDER BY created_at,id",
                (job_id, organization_id, design_group_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def reactivate_design_job_working_copy(
        self,
        *,
        job_id: str,
        expected_revision: int,
        working_copy_id: str,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
        verified_current_sha256: str,
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not re.fullmatch(r"[0-9a-f]{64}", verified_current_sha256):
            raise ValueError("verified_current_sha256 must be a lowercase SHA-256")
        with self.connection() as connection, connection.transaction():
            job = connection.execute(
                "SELECT * FROM design_jobs WHERE id=%s AND organization_id=%s "
                "AND design_group_id=%s AND EXISTS (SELECT 1 FROM actors actor "
                "WHERE actor.id=%s AND actor.organization_id=design_jobs.organization_id) "
                "FOR UPDATE",
                (job_id, organization_id, design_group_id, actor_id),
            ).fetchone()
            if job is None:
                raise KeyError("unknown design_job_id or unauthorized")
            if int(job["revision"]) != expected_revision:
                raise ValueError("stale design job revision")
            if (
                job["status"] != "active"
                or job["job_type"] != "mechanical_design"
                or job["provisioning_state"] != "ready"
                or job["phase"] not in {"delivery", "lesson_capture"}
                or job.get("active_working_copy_id") is not None
            ):
                raise ValueError("design Job is not ready for working-copy reactivation")
            candidates = connection.execute(
                "SELECT id,COALESCE((SELECT resulting_sha256 FROM design_change_sets c "
                "WHERE c.working_copy_id=design_working_copies.id AND c.status='applied' "
                "ORDER BY c.applied_at DESC,c.created_at DESC,c.id DESC LIMIT 1),"
                "working_sha256) AS delivery_recovery_sha256 "
                "FROM design_working_copies WHERE job_id=%s "
                "AND organization_id=%s AND design_group_id=%s ORDER BY created_at,id FOR UPDATE",
                (job_id, organization_id, design_group_id),
            ).fetchall()
            if len(candidates) != 1 or str(candidates[0]["id"]) != working_copy_id:
                raise ValueError("working-copy reactivation candidate changed")
            if candidates[0]["delivery_recovery_sha256"] != verified_current_sha256:
                raise ValueError("working-copy reactivation evidence changed")
            open_count = int(
                connection.execute(
                    "SELECT count(*) AS count FROM design_change_sets WHERE working_copy_id=%s "
                    "AND status NOT IN ('applied','rejected','superseded','cancelled')",
                    (working_copy_id,),
                ).fetchone()["count"]
            )
            if open_count:
                raise ValueError("delivery recovery is blocked by an open design change set")
            summary = connection.execute(
                "SELECT id FROM design_lesson_summaries WHERE working_copy_id=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (working_copy_id,),
            ).fetchone()
            if summary is None:
                raise ValueError("delivery recovery requires a design lesson summary")
            latest_by_kind = connection.execute(
                "SELECT DISTINCT ON (validation_kind) validation_kind,status,working_sha256 "
                "FROM validation_reports WHERE working_copy_id=%s "
                "ORDER BY validation_kind,created_at DESC",
                (working_copy_id,),
            ).fetchall()
            validations = {item["validation_kind"]: item for item in latest_by_kind}
            for required_kind in ("geometry_model", "assembly_completeness"):
                validation = validations.get(required_kind)
                if (
                    validation is None
                    or validation["status"] != "passed"
                    or validation["working_sha256"] != verified_current_sha256
                ):
                    raise ValueError(
                        f"delivery recovery requires current {required_kind} validation"
                    )
            row = connection.execute(
                "UPDATE design_jobs SET active_working_copy_id=%s,revision=revision+1,updated_at=now() "
                "WHERE id=%s AND revision=%s AND active_working_copy_id IS NULL "
                "AND status='active' AND job_type='mechanical_design' "
                "AND phase IN ('delivery','lesson_capture') AND provisioning_state='ready' "
                "AND organization_id=%s AND design_group_id=%s RETURNING *",
                (
                    working_copy_id,
                    job_id,
                    expected_revision,
                    organization_id,
                    design_group_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("working-copy reactivation candidate changed")
            self._record_design_job_event(
                connection,
                job=dict(row),
                event_type="working_copy_bound",
                actor_id=actor_id,
                reason="restore sole verified working copy for delivery approval",
            )
        return dict(row)

    def get_design_job(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM design_jobs WHERE id=%s AND organization_id=%s AND design_group_id=%s",
                (job_id, organization_id, design_group_id),
            ).fetchone()
            result = (
                self._design_job_with_bindings(connection, dict(row))
                if row is not None
                else None
            )
        if row is None:
            raise KeyError("unknown design_job_id or unauthorized")
        assert result is not None
        return result

    def list_design_jobs(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        status: str | None,
        job_type: str | None,
        family_id: str | None,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM design_jobs WHERE organization_id=%s AND design_group_id=%s "
                "AND (%s::text IS NULL OR status=%s) "
                "AND (%s::text IS NULL OR job_type=%s) "
                "AND (%s::text IS NULL OR family_id=%s) "
                "ORDER BY updated_at DESC,id",
                (
                    organization_id,
                    design_group_id,
                    status,
                    status,
                    job_type,
                    job_type,
                    family_id,
                    family_id,
                ),
            ).fetchall()
            results = [
                self._design_job_with_bindings(connection, dict(row)) for row in rows
            ]
        return results

    def resolve_design_jobs(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        query: str,
        job_type: str | None = None,
        family_id: str | None = None,
        statuses: tuple[str, ...] = ("active", "blocked"),
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("design job resolve query is required")
        if not statuses or any(
            status not in {"active", "blocked", "completed", "cancelled", "archived"}
            for status in statuses
        ):
            raise ValueError("design job resolver statuses are invalid")
        phrase = f"%{query.strip()}%"
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM design_jobs WHERE organization_id=%s AND design_group_id=%s "
                "AND status=ANY(%s::text[]) "
                "AND (%s::text IS NULL OR job_type=%s) "
                "AND (%s::text IS NULL OR family_id=%s) "
                "AND (display_id ILIKE %s OR title ILIKE %s OR slug ILIKE %s) "
                "ORDER BY updated_at DESC,id",
                (
                    organization_id,
                    design_group_id,
                    list(statuses),
                    job_type,
                    job_type,
                    family_id,
                    family_id,
                    phrase,
                    phrase,
                    phrase,
                ),
            ).fetchall()
            results = [
                self._design_job_with_bindings(connection, dict(row)) for row in rows
            ]
        return results

    @staticmethod
    def _design_job_with_bindings(
        connection: Any, row: dict[str, Any]
    ) -> dict[str, Any]:
        # Pre-011 compatibility rows used by migration tooling and narrow test
        # doubles do not expose the additive authority column.
        if "active_working_copy_id" not in row:
            return row
        snapshots = connection.execute(
            "SELECT id AS snapshot_id,stored_path,sha256,source_kind,source_model_revision_id "
            "FROM design_job_source_snapshots WHERE job_id=%s ORDER BY created_at,id",
            (row["id"],),
        ).fetchall()
        row["source_snapshots"] = [dict(snapshot) for snapshot in snapshots]
        governed_working = connection.execute(
            "SELECT id::text FROM design_working_copies WHERE job_id=%s "
            "AND organization_id=%s AND design_group_id=%s ORDER BY created_at,id",
            (row["id"], row["organization_id"], row["design_group_id"]),
        ).fetchall()
        row["working_copy_ids"] = [str(item["id"]) for item in governed_working]
        row["active_working_path"] = None
        if row.get("active_working_copy_id") is not None:
            active = connection.execute(
                "SELECT working_path,working_sha256,working_size_bytes,working_relative_path "
                "FROM design_working_copies WHERE id=%s AND job_id=%s "
                "AND organization_id=%s AND design_group_id=%s",
                (
                    row["active_working_copy_id"],
                    row["id"],
                    row["organization_id"],
                    row["design_group_id"],
                ),
            ).fetchone()
            if active is not None:
                row["active_working_path"] = active["working_path"]
                row["active_working_sha256"] = active.get("working_sha256")
                row["active_working_size_bytes"] = active.get("working_size_bytes")
                row["active_working_relative_path"] = active.get("working_relative_path")
        return row

    @staticmethod
    def _raise_design_job_write_failure(
        connection: Any,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
        expected_revision: int,
        operation: str,
    ) -> None:
        diagnostic = connection.execute(
            "SELECT id,revision,provisioning_state,directory_name FROM design_jobs "
            "WHERE id=%s AND organization_id=%s AND design_group_id=%s "
            "AND EXISTS (SELECT 1 FROM actors actor WHERE actor.id=%s "
            "AND actor.organization_id=design_jobs.organization_id)",
            (job_id, organization_id, design_group_id, actor_id),
        ).fetchone()
        if diagnostic is None:
            raise KeyError("unknown design_job_id or unauthorized")
        if int(diagnostic["revision"]) != expected_revision:
            raise ValueError("stale design job revision")
        if operation == "directory" and diagnostic["directory_name"] is not None:
            raise ValueError("design job directory already recorded")
        if diagnostic["provisioning_state"] != "ready":
            raise ValueError("design job provisioning is incomplete")
        if operation == "directory":
            raise ValueError("design job directory already recorded")
        raise RuntimeError("design job update failed without a matching diagnostic")

    @staticmethod
    def _record_design_job_event(
        connection: Any,
        *,
        job: dict[str, Any],
        event_type: str,
        actor_id: str,
        reason: str | None = None,
    ) -> None:
        blocked_reason = job.get("blocked_reason")
        connection.execute(
            "INSERT INTO design_job_events(job_id,revision,event_type,status,phase,provisioning_state,"
            "directory_name,blocked_reason,actor_id,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
            (
                job["id"],
                job["revision"],
                event_type,
                job["status"],
                job["phase"],
                job["provisioning_state"],
                job.get("directory_name"),
                (
                    json.dumps(blocked_reason, ensure_ascii=False)
                    if blocked_reason is not None
                    else None
                ),
                actor_id,
                reason,
            ),
        )

    @staticmethod
    def _enqueue(connection: Any, aggregate_type: str, aggregate_id: str, event_type: str, payload: dict[str, Any]) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"outbox-aggregate:{aggregate_type}:{aggregate_id}",),
        )
        aggregate_version = int(connection.execute(
            "SELECT COALESCE(max(aggregate_version),0)+1 AS aggregate_version FROM outbox_events "
            "WHERE aggregate_type=%s AND aggregate_id=%s",
            (aggregate_type, aggregate_id),
        ).fetchone()["aggregate_version"])
        connection.execute(
            "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload,aggregate_version) "
            "VALUES (%s,%s,%s,%s::jsonb,%s)",
            (aggregate_type, aggregate_id, event_type, json.dumps(payload, ensure_ascii=False), aggregate_version),
        )

    @staticmethod
    def _enqueue_design_lesson_event(connection: Any, *, event_type: str, lesson_id: str) -> None:
        """Enqueue a lesson lifecycle event inside the caller's PostgreSQL transaction."""
        origin = connection.execute(
            "SELECT w.job_id FROM design_lesson_events e "
            "JOIN design_working_copies w ON w.id=e.source_working_copy_id "
            "WHERE e.id::text=%s",
            (lesson_id,),
        ).fetchone()
        if origin is None or origin.get("job_id") is None:
            raise ValueError(
                "JOB_MIGRATION_REQUIRED: design lesson has no originating Design Job"
            )
        PostgresRepository._enqueue(
            connection,
            "design_lesson",
            lesson_id,
            event_type,
            {"lesson_id": lesson_id, "job_id": str(origin["job_id"])},
        )

    @staticmethod
    def _enqueue_design_lesson_review_event(
        connection: Any,
        *,
        event_type: str,
        review: dict[str, Any],
        superseded_by_review_id: str | None = None,
    ) -> None:
        origin = connection.execute(
            "SELECT job_id FROM design_working_copies WHERE id=%s",
            (review["working_copy_id"],),
        ).fetchone()
        if origin is None or origin.get("job_id") is None:
            raise ValueError(
                "JOB_MIGRATION_REQUIRED: design lesson review has no originating Design Job"
            )
        payload = {
            "review_id": str(review["id"]),
            "organization_id": review["organization_id"],
            "design_group_id": review["design_group_id"],
            "status": review["status"],
            "working_copy_id": str(review["working_copy_id"]),
            "job_id": str(origin["job_id"]),
            "lesson_id": review["lesson_id"],
            "review_outcome": review.get("review_outcome", "publish"),
            "package_sha256": review["package_sha256"],
            "review_card_sha256": review["review_card_sha256"],
            "final_model_sha256": review["final_model_sha256"],
            "approved_final_artifact_path": review.get(
                "approved_final_artifact_path"
            ),
            "supersedes_review_id": review.get("supersedes_review_id"),
        }
        if superseded_by_review_id is not None:
            payload["superseded_by_review_id"] = superseded_by_review_id
        if review.get("published_design_lesson_id") is not None:
            payload["published_design_lesson_id"] = str(
                review["published_design_lesson_id"]
            )
        if review.get("decision_receipt_sha256") is not None:
            payload["decision_receipt_sha256"] = review[
                "decision_receipt_sha256"
            ]
        PostgresRepository._enqueue(
            connection,
            "design_lesson_review",
            str(review["id"]),
            event_type,
            payload,
        )
