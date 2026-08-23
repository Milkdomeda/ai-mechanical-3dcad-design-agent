from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import os
from threading import Barrier
import unittest
import uuid

from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.repository import PostgresRepository


DATABASE_URL = os.environ.get("MECH_DESIGN_DATABASE_URL", "").strip()


def _insert_governed_working_copy(
    connection,
    ids: dict[str, str],
    *,
    working_path: str,
) -> dict[str, object]:
    """Install a real governed new-design Job fixture, never a post-011 legacy row."""
    job_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    working_copy_id = str(uuid.uuid4())
    token = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO design_jobs(id,workspace_id,display_id,job_type,title,slug,status,phase,"
        "revision,organization_id,design_group_id,family_id,directory_name,idempotency_token,"
        "provisioning_state,created_by) VALUES (%s,%s,%s,'mechanical_design','Design lesson fixture',"
        "'design-lesson-fixture','active','lesson_capture',1,%s,%s,%s,%s,%s,'ready',%s)",
        (
            job_id,
            workspace_id,
            f"JOB-20260823-{token[:8]}",
            ids["organization_id"],
            ids["design_group_id"],
            ids["family_id"],
            f"JOB-20260823-{token[:8]}-design-lesson-fixture",
            f"design-lesson-fixture-{token}",
            ids["owner_id"],
        ),
    )
    working = connection.execute(
        "INSERT INTO design_working_copies(id,job_id,organization_id,design_group_id,family_id,"
        "source_snapshot_id,bound_job_revision,source_sha256,source_kind,design_origin,working_path,"
        "created_by) VALUES (%s,%s,%s,%s,%s,NULL,1,%s,'new_design_seed','new_design',%s,%s) "
        "RETURNING id",
        (
            working_copy_id,
            job_id,
            ids["organization_id"],
            ids["design_group_id"],
            ids["family_id"],
            "1" * 64,
            working_path,
            ids["owner_id"],
        ),
    ).fetchone()
    connection.execute(
        "UPDATE design_jobs SET active_working_copy_id=%s,revision=2 WHERE id=%s",
        (working_copy_id, job_id),
    )
    ids["job_id"] = job_id
    return dict(working)


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _LifecycleConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.lesson = {
            "id": "00000000-0000-0000-0000-000000000101",
            "organization_id": "org-001",
            "lesson_key": "DL-LOCK-001",
            "status": "approved",
            "supersedes": None,
        }

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT id,organization_id,lesson_key FROM design_lesson_events"):
            return _Rows([self.lesson])
        if normalized.startswith("SELECT * FROM actors"):
            return _Rows([{"id": "owner-001", "role": "family_owner", "organization_id": "org-001"}])
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return _Rows([{}])
        if "COALESCE(max(aggregate_version),0)+1" in normalized:
            return _Rows([{"aggregate_version": 1}])
        if normalized.startswith("SELECT * FROM design_lesson_events"):
            return _Rows([self.lesson])
        if normalized.startswith("UPDATE design_lesson_events"):
            return _Rows([{**self.lesson, "status": "revoked"}])
        if "FROM design_lesson_assertions l JOIN knowledge_assertions" in normalized:
            return _Rows()
        if normalized.startswith("SELECT change_set_id FROM design_lesson_change_sets"):
            return _Rows()
        return _Rows()


class DesignLessonLifecycleLockOrderTests(unittest.TestCase):
    def test_revoke_locks_actor_then_lineage_then_lesson(self) -> None:
        connection = _LifecycleConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        repository.revoke_design_lesson(
            lesson_id=connection.lesson["id"],
            reviewer_id="owner-001",
            reviewer_text="Obsolete lesson",
        )

        actor_lock = next(i for i, query in enumerate(connection.queries) if query.startswith("SELECT * FROM actors"))
        lineage_lock = next(i for i, query in enumerate(connection.queries) if query.startswith("SELECT pg_advisory_xact_lock"))
        lesson_lock = next(
            i for i, query in enumerate(connection.queries)
            if query.startswith("SELECT * FROM design_lesson_events") and "FOR UPDATE" in query
        )
        self.assertLess(actor_lock, lineage_lock)
        self.assertLess(lineage_lock, lesson_lock)


@unittest.skipUnless(
    DATABASE_URL,
    "MECH_DESIGN_DATABASE_URL is not configured; PostgreSQL lifecycle concurrency tests skipped",
)
class PostgresDesignLessonLifecycleConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DATABASE_URL)
        with postgres_migrations_directory() as migrations:
            cls.repository.apply_migrations(migrations)

    def setUp(self) -> None:
        token = uuid.uuid4().hex[:12]
        self.ids = {
            "organization_id": f"org-dl-lock-{token}",
            "design_group_id": f"group-dl-lock-{token}",
            "family_id": f"family-dl-lock-{token}",
            "owner_id": f"owner-dl-lock-{token}",
        }
        self.working_path = f"/tmp/design-lesson-lock-{token}.FCStd"
        with self.repository.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO organizations(id,name) VALUES (%s,'Lifecycle concurrency organization')",
                (self.ids["organization_id"],),
            )
            connection.execute(
                "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,'Lifecycle concurrency group')",
                (self.ids["design_group_id"], self.ids["organization_id"]),
            )
            connection.execute(
                "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,'Lifecycle owner','family_owner')",
                (self.ids["owner_id"], self.ids["organization_id"]),
            )
            connection.execute(
                "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,status,config) "
                "VALUES (%s,%s,%s,'Lifecycle family','active','{}'::jsonb)",
                (self.ids["family_id"], self.ids["organization_id"], self.ids["design_group_id"]),
            )
            working = _insert_governed_working_copy(
                connection,
                self.ids,
                working_path=self.working_path,
            )
            self.ids["working_copy_id"] = str(working["id"])
            change = connection.execute(
                "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,"
                "resulting_sha256,applied_at) VALUES (%s,'applied','detail','[]'::jsonb,'lock fixture',%s,%s,now()) "
                "RETURNING id",
                (self.ids["working_copy_id"], self.ids["owner_id"], "2" * 64),
            ).fetchone()
            self.ids["change_set_id"] = str(change["id"])
            connection.execute(
                "INSERT INTO validation_reports(working_copy_id,change_set_id,status,checks,working_sha256,validation_kind,"
                "report_path,report_sha256) VALUES (%s,%s,'passed','[]'::jsonb,%s,'geometry_model',%s,%s)",
                (
                    self.ids["working_copy_id"], self.ids["change_set_id"], "2" * 64,
                    "/tmp/validation.json", "3" * 64,
                ),
            )
        self.predecessor = self._approve("predecessor")

    def tearDown(self) -> None:
        with self.repository.connection() as connection, connection.transaction():
            assertion_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM knowledge_assertions WHERE organization_id=%s",
                    (self.ids["organization_id"],),
                ).fetchall()
            ]
            lesson_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM design_lesson_events WHERE organization_id=%s",
                    (self.ids["organization_id"],),
                ).fetchall()
            ]
            aggregate_ids = assertion_ids + lesson_ids
            if aggregate_ids:
                connection.execute("DELETE FROM outbox_events WHERE aggregate_id=ANY(%s)", (aggregate_ids,))
            if assertion_ids:
                connection.execute(
                    "DELETE FROM review_events WHERE assertion_id=ANY(%s::uuid[])",
                    (assertion_ids,),
                )
                connection.execute(
                    "DELETE FROM knowledge_search_documents WHERE assertion_id=ANY(%s::uuid[])",
                    (assertion_ids,),
                )
            connection.execute(
                "DELETE FROM design_lesson_events WHERE organization_id=%s",
                (self.ids["organization_id"],),
            )
            connection.execute(
                "DELETE FROM knowledge_assertions WHERE organization_id=%s",
                (self.ids["organization_id"],),
            )
            connection.execute(
                "DELETE FROM validation_reports WHERE working_copy_id=%s",
                (self.ids["working_copy_id"],),
            )
            connection.execute(
                "DELETE FROM design_change_sets WHERE working_copy_id=%s",
                (self.ids["working_copy_id"],),
            )
            connection.execute(
                "UPDATE design_jobs SET active_working_copy_id=NULL WHERE id=%s",
                (self.ids["job_id"],),
            )
            connection.execute(
                "DELETE FROM design_working_copies WHERE id=%s",
                (self.ids["working_copy_id"],),
            )
            connection.execute(
                "DELETE FROM design_jobs WHERE id=%s",
                (self.ids["job_id"],),
            )
            connection.execute("DELETE FROM product_families WHERE id=%s", (self.ids["family_id"],))
            connection.execute("DELETE FROM actors WHERE id=%s", (self.ids["owner_id"],))
            connection.execute("DELETE FROM design_groups WHERE id=%s", (self.ids["design_group_id"],))
            connection.execute("DELETE FROM organizations WHERE id=%s", (self.ids["organization_id"],))

    def _approve(self, label: str, *, supersedes_lesson_id: str | None = None) -> dict:
        package = repository_package(self.ids, lesson_id=f"DL-LOCK-{label.upper()}")
        digest = hashlib.sha256(f"{self.ids['organization_id']}:{label}".encode()).hexdigest()
        evidence = package["evidence_manifest"][0]
        return self.repository.approve_design_lesson(
            package=package,
            package_sha256=digest,
            archived_package_path=f"/artifacts/{digest}.json",
            archived_evidence=[{
                **evidence,
                "artifact_sha256": evidence["sha256"],
                "artifact_storage_path": f"/artifacts/{evidence['sha256']}.json",
                "artifact_source_path": "/tmp/validation.json",
            }],
            working_copy_artifact={
                "sha256": "2" * 64,
                "storage_path": f"/artifacts/{'2' * 64}.FCStd",
                "source_path": self.working_path,
            },
            reviewer_id=self.ids["owner_id"],
            reviewer_text=f"Reviewed {label}",
            supersedes_lesson_id=supersedes_lesson_id,
            working_copy_sha256_reader=lambda _path: "2" * 64,
        )

    def _run_concurrently(self, left, right):
        barrier = Barrier(2)

        def invoke(operation):
            barrier.wait(timeout=5)
            try:
                return ("ok", operation())
            except Exception as error:
                return ("error", error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke, operation) for operation in (left, right)]
            return [future.result(timeout=10) for future in futures]

    def test_concurrent_supersede_and_revoke_finish_without_deadlock(self) -> None:
        results = self._run_concurrently(
            lambda: self._approve("replacement", supersedes_lesson_id=self.predecessor["id"]),
            lambda: self.repository.revoke_design_lesson(
                lesson_id=self.predecessor["id"],
                reviewer_id=self.ids["owner_id"],
                reviewer_text="Concurrent revocation",
            ),
        )

        self.assertEqual(sorted(status for status, _ in results), ["error", "ok"])
        self.assertTrue(
            any(
                isinstance(value, ValueError) and "approved design lesson" in str(value)
                for status, value in results if status == "error"
            )
        )

    def test_concurrent_replacements_serialize_to_one_successor(self) -> None:
        results = self._run_concurrently(
            lambda: self._approve("replacement-a", supersedes_lesson_id=self.predecessor["id"]),
            lambda: self._approve("replacement-b", supersedes_lesson_id=self.predecessor["id"]),
        )

        self.assertEqual(sorted(status for status, _ in results), ["error", "ok"])
        with self.repository.connection() as connection:
            active = connection.execute(
                "SELECT count(*) AS count FROM design_lesson_events "
                "WHERE organization_id=%s AND lesson_key=%s AND status='approved'",
                (self.ids["organization_id"], self.predecessor["lesson_key"]),
            ).fetchone()
        self.assertEqual(int(active["count"]), 1)


def repository_package(ids: dict[str, str], lesson_id: str = "DL-REPOSITORY-001") -> dict:
    return {
        "schema_version": "DesignLessonPackage/v1",
        "lesson_id": lesson_id,
        "title": "Verify actuator mounting clearance",
        "codex_session_id": "codex-session-integration",
        "source": {
            "organization_id": ids["organization_id"],
            "design_group_id": ids["design_group_id"],
            "family_id": ids["family_id"],
            "working_copy_id": ids["working_copy_id"],
            "before_model_sha256": "1" * 64,
            "after_model_sha256": "2" * 64,
            "change_set_ids": [ids["change_set_id"]],
        },
        "problem": {
            "summary": "Mounting clearance was missed",
            "discovery_stage": "assembly-validation",
            "severity": "major",
            "symptoms": ["interference"],
            "affected_components": ["actuator"],
            "affected_interfaces": ["mount"],
            "failure_modes": ["interference"],
        },
        "root_causes": ["Clearance check was omitted"],
        "corrections": ["Add the clearance check"],
        "prevention": {
            "required_checks": ["Validate before release"],
            "design_review_questions": ["Is mounting clearance verified?"],
            "workflow_gate": "validation-before-release",
            "detection_method": "clearance validation",
        },
        "applicability": {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": ["detail"],
            "required_conditions": ["moving-assembly"],
        },
        "non_applicable_conditions": [],
        "search_terms": ["actuator clearance", "mount clearance"],
        "atomic_assertions": [
            {
                "assertion_key": "actuator-clearance",
                "subject_ref": "component:actuator",
                "predicate": "requires-clearance",
                "object_value": {"minimum_mm": 2},
                "constraint_kind": "hard_constraint",
                "evidence_refs": ["validation-evidence"],
            },
            {
                "assertion_key": "mount-inspection",
                "subject_ref": "interface:mount",
                "predicate": "requires-inspection",
                "object_value": True,
                "constraint_kind": "check",
                "evidence_refs": ["validation-evidence"],
            },
        ],
        "evidence_manifest": [
            {
                "evidence_id": "validation-evidence",
                "path": "validation.json",
                "role": "geometry_validation",
                "media_type": "application/json",
                "sha256": "3" * 64,
                "working_copy_id": ids["working_copy_id"],
                "change_set_id": ids["change_set_id"],
                "model_sha256": "2" * 64,
                "validation_kind": "geometry_model",
            }
        ],
    }


@unittest.skipUnless(
    DATABASE_URL,
    "MECH_DESIGN_DATABASE_URL is not configured; PostgreSQL integration tests skipped",
)
class PostgresDesignLessonRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DATABASE_URL)
        with postgres_migrations_directory() as migrations:
            cls.repository.apply_migrations(migrations)

    def setUp(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        token = uuid.uuid4().hex[:12]
        self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        self.transaction = self.connection.transaction()
        self.transaction.__enter__()
        self.ids = {
            "organization_id": f"org-dl-{token}",
            "design_group_id": f"group-dl-{token}",
            "family_id": f"family-dl-{token}",
            "owner_id": f"owner-dl-{token}",
            "reviewer_id": f"reviewer-dl-{token}",
        }
        self.connection.execute(
            "INSERT INTO organizations(id,name) VALUES (%s,%s)",
            (self.ids["organization_id"], "Design lesson integration organization"),
        )
        self.connection.execute(
            "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
            (self.ids["design_group_id"], self.ids["organization_id"], "Design lesson integration group"),
        )
        self.connection.execute(
            "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,'family_owner'),(%s,%s,%s,'reviewer')",
            (
                self.ids["owner_id"], self.ids["organization_id"], "Family owner",
                self.ids["reviewer_id"], self.ids["organization_id"], "Reviewer",
            ),
        )
        self.connection.execute(
            "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,status,config) VALUES (%s,%s,%s,%s,'active','{}'::jsonb)",
            (self.ids["family_id"], self.ids["organization_id"], self.ids["design_group_id"], "Integration family"),
        )
        working = _insert_governed_working_copy(
            self.connection,
            self.ids,
            working_path="/tmp/design-lesson-integration.FCStd",
        )
        self.ids["working_copy_id"] = str(working["id"])
        change = self.connection.execute(
            "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,resulting_sha256,applied_at) "
            "VALUES (%s,'applied','detail','[]'::jsonb,'integration fixture',%s,%s,now()) RETURNING id",
            (self.ids["working_copy_id"], self.ids["owner_id"], "2" * 64),
        ).fetchone()
        self.ids["change_set_id"] = str(change["id"])
        self.connection.execute(
            "INSERT INTO validation_reports(working_copy_id,change_set_id,status,checks,working_sha256,validation_kind,"
            "report_path,report_sha256) VALUES (%s,%s,'passed','[]'::jsonb,%s,'geometry_model',%s,%s) RETURNING id",
            (
                self.ids["working_copy_id"], self.ids["change_set_id"], "2" * 64,
                "/tmp/validation.json", "3" * 64,
            ),
        )
        self.original_connection = self.repository.connection

        @contextmanager
        def shared_connection():
            yield self.connection

        self.repository.connection = shared_connection
        self.baselines = {
            table: self._absolute_count(table)
            for table in self.allowed_count_tables()
        }

    def tearDown(self) -> None:
        self.repository.connection = self.original_connection
        self.transaction.__exit__(RuntimeError, RuntimeError("rollback integration fixture"), None)
        self.connection.close()

    def package_digest(
        self, marker: str, *, organization_id: str | None = None
    ) -> str:
        scope = organization_id or self.ids["organization_id"]
        return hashlib.sha256(
            f"{scope}:{marker}".encode()
        ).hexdigest()

    def approve(self, package: dict | None = None, digest: str = "4" * 64, **kwargs):
        resolved_package = package or repository_package(self.ids)
        digest = self.package_digest(digest)
        working_copy_sha256_reader = kwargs.pop(
            "working_copy_sha256_reader",
            lambda _path: resolved_package["source"]["after_model_sha256"],
        )
        return self.repository.approve_design_lesson(
            package=resolved_package,
            package_sha256=digest,
            archived_package_path=f"/artifacts/{digest}.json",
            archived_evidence=[
                {
                    **item,
                    "artifact_sha256": item["sha256"],
                    "artifact_storage_path": f"/artifacts/{item['sha256']}.json",
                    "artifact_source_path": "/tmp/validation.json",
                }
                for item in resolved_package["evidence_manifest"]
            ],
            working_copy_artifact={
                "sha256": resolved_package["source"]["after_model_sha256"],
                "storage_path": f"/artifacts/{resolved_package['source']['after_model_sha256']}.FCStd",
                "source_path": "/tmp/design-lesson-integration.FCStd",
            },
            reviewer_id=self.ids["owner_id"],
            reviewer_text="Reviewed integration lesson",
            working_copy_sha256_reader=working_copy_sha256_reader,
            **kwargs,
        )

    def insert_design_lesson_review(
        self, *, review_id: str, digest: str = "4" * 64
    ) -> tuple[str, str]:
        package = repository_package(self.ids)
        digest = self.package_digest(digest)
        review_path = f"/reviews/{review_id}/review.md"
        package_path = f"/staging/review-{digest}/lesson.json"
        self.connection.execute(
            "INSERT INTO design_lesson_reviews(id,organization_id,design_group_id,working_copy_id,lesson_id,"
            "package_sha256,review_card_sha256,final_model_sha256,status,review_path,package_path,created_by,"
            "approved_final_artifact_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting-engineer-review',%s,%s,%s,%s)",
            (
                review_id,
                self.ids["organization_id"],
                self.ids["design_group_id"],
                self.ids["working_copy_id"],
                package["lesson_id"],
                digest,
                "5" * 64,
                "2" * 64,
                review_path,
                package_path,
                self.ids["owner_id"],
                f"/artifacts/{'2' * 64}.FCStd",
            ),
        )
        return review_path, package_path

    def create_additional_scope(self) -> dict[str, str]:
        token = uuid.uuid4().hex[:12]
        ids = {
            "organization_id": f"org-dl-other-{token}",
            "design_group_id": f"group-dl-other-{token}",
            "family_id": f"family-dl-other-{token}",
            "owner_id": f"owner-dl-other-{token}",
        }
        self.connection.execute(
            "INSERT INTO organizations(id,name) VALUES (%s,'Other organization')",
            (ids["organization_id"],),
        )
        self.connection.execute(
            "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,'Other group')",
            (ids["design_group_id"], ids["organization_id"]),
        )
        self.connection.execute(
            "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,'Other owner','family_owner')",
            (ids["owner_id"], ids["organization_id"]),
        )
        self.connection.execute(
            "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,status,config) "
            "VALUES (%s,%s,%s,'Other family','active','{}'::jsonb)",
            (ids["family_id"], ids["organization_id"], ids["design_group_id"]),
        )
        working = _insert_governed_working_copy(
            self.connection,
            ids,
            working_path="/tmp/other-design.FCStd",
        )
        ids["working_copy_id"] = str(working["id"])
        change = self.connection.execute(
            "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,resulting_sha256,applied_at) "
            "VALUES (%s,'applied','detail','[]'::jsonb,'other fixture',%s,%s,now()) RETURNING id",
            (ids["working_copy_id"], ids["owner_id"], "2" * 64),
        ).fetchone()
        ids["change_set_id"] = str(change["id"])
        self.connection.execute(
            "INSERT INTO validation_reports(working_copy_id,change_set_id,status,checks,working_sha256,validation_kind,"
            "report_path,report_sha256) VALUES (%s,%s,'passed','[]'::jsonb,%s,'geometry_model',%s,%s)",
            (
                ids["working_copy_id"], ids["change_set_id"], "2" * 64,
                "/tmp/validation.json", "3" * 64,
            ),
        )
        return ids

    def approve_for_scope(self, ids: dict[str, str], package: dict, digest: str):
        digest = self.package_digest(
            digest, organization_id=ids["organization_id"]
        )
        return self.repository.approve_design_lesson(
            package=package,
            package_sha256=digest,
            archived_package_path=f"/artifacts/{digest}.json",
            archived_evidence=[
                {
                    **item,
                    "artifact_sha256": item["sha256"],
                    "artifact_storage_path": f"/artifacts/{item['sha256']}.json",
                    "artifact_source_path": "/tmp/validation.json",
                }
                for item in package["evidence_manifest"]
            ],
            working_copy_artifact={
                "sha256": package["source"]["after_model_sha256"],
                "storage_path": f"/artifacts/{package['source']['after_model_sha256']}.FCStd",
                "source_path": "/tmp/other-design.FCStd",
            },
            reviewer_id=ids["owner_id"],
            reviewer_text="Reviewed other organization lesson",
            working_copy_sha256_reader=lambda _path: package["source"]["after_model_sha256"],
        )

    @staticmethod
    def allowed_count_tables() -> set[str]:
        return {
            "design_lesson_events", "design_lesson_change_sets", "design_lesson_assertions",
            "design_lesson_evidence_artifacts", "design_lesson_report_bindings",
            "knowledge_assertions", "review_events", "knowledge_search_documents", "outbox_events",
        }

    def _absolute_count(self, table: str) -> int:
        if table not in self.allowed_count_tables():
            raise ValueError(table)
        return int(self.connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"])

    def count(self, table: str) -> int:
        return self._absolute_count(table) - self.baselines[table]

    def test_approval_requires_family_owner(self) -> None:
        with self.assertRaisesRegex(PermissionError, "family_owner"):
            self.repository.approve_design_lesson(
                package=repository_package(self.ids),
                package_sha256=self.package_digest("4" * 64),
                archived_package_path="/artifacts/package.json",
                reviewer_id=self.ids["reviewer_id"],
                reviewer_text="Not an owner",
            )
        self.assertEqual(self.count("design_lesson_events"), 0)

    def test_approval_atomically_creates_event_assertions_links_audit_search_and_outbox(self) -> None:
        lesson = self.approve()

        self.assertEqual(lesson["status"], "approved")
        self.assertEqual(lesson["revision"], 1)
        self.assertEqual(self.count("design_lesson_events"), 1)
        self.assertEqual(self.count("design_lesson_change_sets"), 1)
        self.assertEqual(self.count("design_lesson_assertions"), 2)
        self.assertEqual(self.count("design_lesson_evidence_artifacts"), 2)
        self.assertEqual(self.count("design_lesson_report_bindings"), 1)
        self.assertEqual(self.count("knowledge_assertions"), 2)
        self.assertEqual(self.count("review_events"), 2)
        self.assertEqual(self.count("knowledge_search_documents"), 2)
        self.assertEqual(self.count("outbox_events"), 3)
        assertions = lesson["assertions"]
        self.assertTrue(all(item["scope_kind"] == "organization_general" for item in assertions))
        self.assertTrue(all(item["risk_level"] == "R3" for item in assertions))
        self.assertTrue(all(item["source_kind"] == "approved_design_lesson" for item in assertions))
        assertion_ids = [item["id"] for item in assertions]
        review_rows = self.connection.execute(
            "SELECT decision,previous_status,resulting_status FROM review_events "
            "WHERE assertion_id = ANY(%s::uuid[]) ORDER BY assertion_id",
            (assertion_ids,),
        ).fetchall()
        self.assertEqual(
            {(row["decision"], row["previous_status"], row["resulting_status"]) for row in review_rows},
            {("approve-design-lesson", "external_staging", "approved")},
        )
        outbox_types = self.connection.execute(
            "SELECT event_type FROM outbox_events WHERE aggregate_id = ANY(%s) ORDER BY aggregate_id",
            (assertion_ids,),
        ).fetchall()
        self.assertEqual([row["event_type"] for row in outbox_types], ["knowledge_assertion.reviewed"] * 2)
        lifecycle = self.connection.execute(
            "SELECT aggregate_type,aggregate_id::text,event_type,payload FROM outbox_events "
            "WHERE aggregate_type='design_lesson' AND aggregate_id=%s",
            (lesson["id"],),
        ).fetchone()
        self.assertEqual(
            (lifecycle["aggregate_type"], lifecycle["aggregate_id"], lifecycle["event_type"], lifecycle["payload"]),
            ("design_lesson", lesson["id"], "design_lesson.approved", {"lesson_id": lesson["id"]}),
        )
        binding = self.connection.execute(
            "SELECT evidence_id,validation_kind,working_copy_id::text,change_set_id::text,working_sha256 "
            "FROM design_lesson_report_bindings WHERE lesson_event_id=%s",
            (lesson["id"],),
        ).fetchone()
        self.assertEqual(
            (
                binding["evidence_id"],
                binding["validation_kind"],
                binding["working_copy_id"],
                binding["change_set_id"],
                binding["working_sha256"],
            ),
            (
                "validation-evidence",
                "geometry_model",
                self.ids["working_copy_id"],
                self.ids["change_set_id"],
                "2" * 64,
            ),
        )
        projection_lesson = next(
            item
            for item in self.repository.projection_design_lessons()
            if item["id"] == lesson["id"]
        )
        self.assertEqual(projection_lesson["aggregate_version"], 1)
        self.assertEqual(
            {item["aggregate_version"] for item in projection_lesson["assertions"]},
            {1},
        )
        projection_assertions = {
            str(item["id"]): item
            for item in self.repository.projection_assertions()
            if str(item["id"]) in assertion_ids
        }
        self.assertEqual(
            {item["aggregate_version"] for item in projection_assertions.values()},
            {1},
        )

    def test_review_bound_approval_atomically_publishes_and_binds_the_review(self) -> None:
        package = repository_package(self.ids)
        digest = self.package_digest("4" * 64)
        review_id = f"DLR-{uuid.uuid4().hex}"
        final_artifact_path = f"/artifacts/{'2' * 64}.FCStd"
        review_path = f"/reviews/{review_id}/review.md"
        package_path = f"/staging/review-{digest}/lesson.json"
        self.connection.execute(
            "INSERT INTO design_lesson_reviews(id,organization_id,design_group_id,working_copy_id,lesson_id,"
            "package_sha256,review_card_sha256,final_model_sha256,status,review_path,package_path,created_by,"
            "approved_final_artifact_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting-engineer-review',%s,%s,%s,%s)",
            (
                review_id,
                self.ids["organization_id"],
                self.ids["design_group_id"],
                self.ids["working_copy_id"],
                package["lesson_id"],
                digest,
                "5" * 64,
                "2" * 64,
                review_path,
                package_path,
                self.ids["owner_id"],
                final_artifact_path,
            ),
        )

        lesson = self.approve(
            review_id=review_id,
            verified_review_card_sha256="5" * 64,
            verified_review_path=review_path,
            verified_package_path=package_path,
        )

        review = self.connection.execute(
            "SELECT status,published_design_lesson_id::text,reviewed_by,reviewer_text "
            "FROM design_lesson_reviews WHERE id=%s",
            (review_id,),
        ).fetchone()
        self.assertEqual(review["status"], "approved-retrieval-pending")
        self.assertEqual(review["published_design_lesson_id"], str(lesson["id"]))
        self.assertEqual(review["reviewed_by"], self.ids["owner_id"])
        self.assertEqual(review["reviewer_text"], "Reviewed integration lesson")
        self.assertEqual(len(lesson["assertions"]), 2)
        event = self.connection.execute(
            "SELECT event_type,payload FROM outbox_events "
            "WHERE aggregate_type='design_lesson_review' AND aggregate_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        self.assertEqual(event["event_type"], "design_lesson_review.approved")
        self.assertEqual(event["payload"]["status"], "approved-retrieval-pending")

    def test_review_bound_approval_rejects_mismatched_verified_card_binding_atomically(self) -> None:
        review_id = f"DLR-{uuid.uuid4().hex}"
        review_path, package_path = self.insert_design_lesson_review(
            review_id=review_id
        )

        with self.assertRaisesRegex(ValueError, "review.*package"):
            self.approve(
                review_id=review_id,
                verified_review_card_sha256="9" * 64,
                verified_review_path=review_path,
                verified_package_path=package_path,
            )

        review = self.connection.execute(
            "SELECT status,published_design_lesson_id FROM design_lesson_reviews WHERE id=%s",
            (review_id,),
        ).fetchone()
        self.assertEqual(review["status"], "awaiting-engineer-review")
        self.assertIsNone(review["published_design_lesson_id"])
        self.assertEqual(self.count("design_lesson_events"), 0)
        self.assertEqual(self.count("knowledge_assertions"), 0)
        self.assertEqual(self.count("outbox_events"), 0)

    def test_review_bound_approval_rejects_mismatched_verified_review_path_atomically(self) -> None:
        review_id = f"DLR-{uuid.uuid4().hex}"
        _review_path, package_path = self.insert_design_lesson_review(
            review_id=review_id
        )

        with self.assertRaisesRegex(ValueError, "review.*package"):
            self.approve(
                review_id=review_id,
                verified_review_card_sha256="5" * 64,
                verified_review_path="/reviews/a-different-review/review.md",
                verified_package_path=package_path,
            )

        review = self.connection.execute(
            "SELECT status,published_design_lesson_id FROM design_lesson_reviews WHERE id=%s",
            (review_id,),
        ).fetchone()
        self.assertEqual(review["status"], "awaiting-engineer-review")
        self.assertIsNone(review["published_design_lesson_id"])
        self.assertEqual(self.count("design_lesson_events"), 0)
        self.assertEqual(self.count("knowledge_assertions"), 0)
        self.assertEqual(self.count("outbox_events"), 0)

    def test_approval_rejects_evidence_artifact_or_revision_binding_mismatch(self) -> None:
        package = repository_package(self.ids)
        archived_evidence = [{
            **package["evidence_manifest"][0],
            "artifact_sha256": "9" * 64,
            "artifact_storage_path": "/artifacts/tampered.json",
            "artifact_source_path": "/tmp/validation.json",
        }]

        with self.assertRaisesRegex(ValueError, "archived evidence SHA-256"):
            self.repository.approve_design_lesson(
                package=package,
                package_sha256=self.package_digest("e" * 64),
                archived_package_path="/artifacts/package.json",
                archived_evidence=archived_evidence,
                working_copy_artifact={
                    "sha256": "2" * 64,
                    "storage_path": "/artifacts/model.FCStd",
                    "source_path": "/tmp/design-lesson-integration.FCStd",
                },
                reviewer_id=self.ids["owner_id"],
                reviewer_text="Reviewed integration lesson",
            )

        revision_mismatch = repository_package(self.ids)
        revision_mismatch["evidence_manifest"][0]["model_sha256"] = "9" * 64
        with self.assertRaisesRegex(ValueError, "revision binding mismatch"):
            self.approve(package=revision_mismatch, digest="d" * 64)

    def test_approval_requires_each_typed_validation_report_on_the_same_revision(self) -> None:
        package = repository_package(self.ids)
        package["evidence_manifest"].append({
            **package["evidence_manifest"][0],
            "evidence_id": "fastener-validation-evidence",
            "role": "fastener_interface_validation",
            "validation_kind": "fastener_interfaces",
        })

        with self.assertRaisesRegex(
            ValueError,
            "same-revision passed fastener_interfaces evidence",
        ):
            self.approve(package=package, digest="0" * 64)

    def test_approval_rejects_a_package_without_baseline_validation_evidence(self) -> None:
        package = repository_package(self.ids)
        package["evidence_manifest"] = []
        for assertion in package["atomic_assertions"]:
            assertion["evidence_refs"] = []

        with self.assertRaisesRegex(ValueError, "geometry validation evidence"):
            self.approve(package=package, digest="6" * 64)
        self.assertEqual(self.count("design_lesson_events"), 0)

    def test_approval_rehashes_current_fcstd_inside_the_locked_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "current locked FCStd"):
            self.approve(
                digest="1" * 64,
                working_copy_sha256_reader=lambda _path: "9" * 64,
            )

    def test_approval_rejects_validation_without_immutable_report_binding(self) -> None:
        self.connection.execute(
            "UPDATE validation_reports SET report_path=NULL,report_sha256=NULL "
            "WHERE working_copy_id=%s AND change_set_id=%s",
            (self.ids["working_copy_id"], self.ids["change_set_id"]),
        )
        with self.assertRaisesRegex(ValueError, "immutable report"):
            self.approve(digest="2" * 64)

    def test_revoked_lesson_key_cannot_be_reused_without_explicit_lineage(self) -> None:
        lesson = self.approve()
        self.repository.revoke_design_lesson(
            lesson_id=lesson["id"],
            reviewer_id=self.ids["owner_id"],
            reviewer_text="Obsolete lesson",
        )

        with self.assertRaisesRegex(ValueError, "explicit replacement or restore lineage"):
            self.approve(digest="f" * 64)

    def test_invalid_assertion_rolls_back_every_authoritative_row(self) -> None:
        package = repository_package(self.ids)
        package["atomic_assertions"][1]["contradicts"] = ["not-a-uuid"]

        with self.assertRaises(ValueError):
            self.approve(package=package)

        for table in (
            "design_lesson_events", "design_lesson_change_sets", "design_lesson_assertions",
            "knowledge_assertions", "review_events", "knowledge_search_documents", "outbox_events",
        ):
            with self.subTest(table=table):
                self.assertEqual(self.count(table), 0)

    def test_package_digest_is_idempotent(self) -> None:
        first = self.approve()
        second = self.approve()

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.count("design_lesson_events"), 1)
        self.assertEqual(self.count("knowledge_assertions"), 2)
        self.assertEqual(self.count("outbox_events"), 3)

    def test_package_digest_is_isolated_from_another_organization(self) -> None:
        other_ids = self.create_additional_scope()
        marker = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        review_id = f"DLR-{uuid.uuid4().hex}"
        self.connection.execute(
            "INSERT INTO design_lesson_reviews(id,organization_id,design_group_id,working_copy_id,lesson_id,"
            "package_sha256,review_card_sha256,final_model_sha256,status,review_path,package_path,created_by,"
            "approved_final_artifact_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting-engineer-review',%s,%s,%s,%s)",
            (
                review_id,
                other_ids["organization_id"],
                other_ids["design_group_id"],
                other_ids["working_copy_id"],
                "DL-EXTERNAL-DIGEST-SENTINEL",
                marker,
                hashlib.sha256(f"review:{marker}".encode()).hexdigest(),
                "2" * 64,
                f"/reviews/{review_id}/review.md",
                f"/staging/review-{marker}/lesson.json",
                other_ids["owner_id"],
                f"/artifacts/{'2' * 64}.FCStd",
            ),
        )

        lesson = self.approve(digest=marker)

        self.assertEqual(lesson["organization_id"], self.ids["organization_id"])
        self.assertNotEqual(lesson["package_sha256"], marker)

    def test_approval_accepts_before_hash_from_immediate_predecessor_revision(self) -> None:
        final_change = self.connection.execute(
            "SELECT applied_at FROM design_change_sets WHERE id=%s",
            (self.ids["change_set_id"],),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,"
            "resulting_sha256,applied_at) VALUES (%s,'applied','detail','[]'::jsonb,'predecessor fixture',%s,%s,%s) ",
            (
                self.ids["working_copy_id"],
                self.ids["owner_id"],
                "8" * 64,
                final_change["applied_at"] - timedelta(seconds=1),
            ),
        )
        package = repository_package(self.ids, lesson_id="DL-INTERMEDIATE-REVISION-001")
        package["source"]["before_model_sha256"] = "8" * 64

        lesson = self.approve(package=package, digest="a" * 64)

        self.assertEqual(lesson["before_model_sha256"], "8" * 64)

    def test_multi_change_set_uses_ordered_final_change_and_its_validation(self) -> None:
        intermediate = self.connection.execute(
            "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,resulting_sha256,applied_at) "
            "VALUES (%s,'applied','concept','[]'::jsonb,'intermediate fixture',%s,%s,now()) RETURNING id",
            (self.ids["working_copy_id"], self.ids["owner_id"], "8" * 64),
        ).fetchone()
        unrelated = self.connection.execute(
            "INSERT INTO design_change_sets(working_copy_id,status,change_phase,changes,rationale,created_by,resulting_sha256,applied_at) "
            "VALUES (%s,'applied','review','[]'::jsonb,'unrelated fixture',%s,%s,now()) RETURNING id",
            (self.ids["working_copy_id"], self.ids["owner_id"], "9" * 64),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO validation_reports(working_copy_id,change_set_id,status,checks,working_sha256,validation_kind) "
            "VALUES (%s,%s,'failed','[]'::jsonb,%s,'geometry_model')",
            (self.ids["working_copy_id"], unrelated["id"], "9" * 64),
        )
        package = repository_package(self.ids)
        package["source"]["change_set_ids"] = [str(intermediate["id"]), self.ids["change_set_id"]]

        lesson = self.approve(package=package, digest="6" * 64)

        self.assertEqual(lesson["change_set_ids"], package["source"]["change_set_ids"])

    def test_same_lesson_key_has_organization_local_active_state_and_revision(self) -> None:
        first = self.approve()
        other_ids = self.create_additional_scope()
        other = self.approve_for_scope(
            other_ids,
            repository_package(other_ids, lesson_id="DL-REPOSITORY-001"),
            "7" * 64,
        )

        self.assertEqual(first["revision"], 1)
        self.assertEqual(other["revision"], 1)

    def test_revocation_removes_lesson_from_search_and_preserves_audit(self) -> None:
        lesson = self.approve()
        found = self.repository.search_approved_design_lessons(
            organization_id=self.ids["organization_id"], query="actuator clearance", limit=10
        )
        self.assertEqual([item["id"] for item in found], [lesson["id"]])

        revoked = self.repository.revoke_design_lesson(
            lesson_id=lesson["id"], reviewer_id=self.ids["owner_id"], reviewer_text="Obsolete lesson"
        )

        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.count("knowledge_search_documents"), 0)
        self.assertEqual(
            self.repository.search_approved_design_lessons(
                organization_id=self.ids["organization_id"], query="actuator clearance", limit=10
            ),
            [],
        )
        self.assertEqual(self.count("review_events"), 4)
        lifecycle_types = self.connection.execute(
            "SELECT event_type FROM outbox_events WHERE aggregate_type='design_lesson' "
            "AND aggregate_id=%s ORDER BY event_type",
            (lesson["id"],),
        ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in lifecycle_types],
            ["design_lesson.approved", "design_lesson.revoked"],
        )

    def test_superseding_replacement_retires_predecessor_in_same_transaction(self) -> None:
        predecessor = self.approve()
        replacement_package = repository_package(self.ids, lesson_id="DL-REPOSITORY-002")
        replacement_package["title"] = "Replacement clearance lesson"

        replacement = self.approve(
            package=replacement_package,
            digest="5" * 64,
            supersedes_lesson_id=predecessor["id"],
        )

        self.assertEqual(replacement["status"], "approved")
        self.assertEqual(replacement["supersedes"], predecessor["id"])
        retired = self.repository.get_design_lesson(
            predecessor["id"], organization_id=self.ids["organization_id"]
        )
        self.assertEqual(retired["status"], "superseded")
        found = self.repository.search_approved_design_lessons(
            organization_id=self.ids["organization_id"], query="", limit=10
        )
        self.assertEqual([item["id"] for item in found], [replacement["id"]])
        lifecycle_rows = self.connection.execute(
            "SELECT aggregate_id::text,event_type,payload FROM outbox_events WHERE aggregate_type='design_lesson' "
            "AND aggregate_id = ANY(%s) ORDER BY aggregate_id,event_type",
            ([predecessor["id"], replacement["id"]],),
        ).fetchall()
        self.assertEqual(
            {(row["aggregate_id"], row["event_type"], row["payload"]["lesson_id"]) for row in lifecycle_rows},
            {
                (predecessor["id"], "design_lesson.approved", predecessor["id"]),
                (predecessor["id"], "design_lesson.superseded", predecessor["id"]),
                (replacement["id"], "design_lesson.approved", replacement["id"]),
            },
        )

    def test_superseding_rejects_stable_key_with_changed_semantic_identity(self) -> None:
        predecessor = self.approve()
        replacement_package = repository_package(self.ids, lesson_id="DL-REPOSITORY-SEMANTIC")
        replacement_package["atomic_assertions"][0]["subject_ref"] = "component:different-actuator"

        with self.assertRaisesRegex(ValueError, "subject_ref and predicate"):
            self.approve(
                package=replacement_package,
                digest="8" * 64,
                supersedes_lesson_id=predecessor["id"],
            )

    def test_superseding_rejects_changed_identity_reintroduced_after_omitted_revision(self) -> None:
        first = self.approve()
        second_package = repository_package(self.ids, lesson_id="DL-REPOSITORY-OMITS-KEY")
        second_package["atomic_assertions"] = [second_package["atomic_assertions"][1]]
        second = self.approve(
            package=second_package,
            digest="c" * 64,
            supersedes_lesson_id=first["id"],
        )
        third_package = repository_package(self.ids, lesson_id="DL-REPOSITORY-REUSES-KEY")
        third_package["atomic_assertions"][0]["predicate"] = "different-semantic-predicate"

        with self.assertRaisesRegex(ValueError, "lesson lineage"):
            self.approve(
                package=third_package,
                digest="d" * 64,
                supersedes_lesson_id=second["id"],
            )

    def test_contradiction_target_must_exist(self) -> None:
        package = repository_package(self.ids, lesson_id="DL-CONTRADICTION-MISSING")
        package["atomic_assertions"][0]["contradicts"] = [str(uuid.uuid4())]

        with self.assertRaisesRegex(ValueError, "contradiction target does not exist"):
            self.approve(package=package, digest="9" * 64)

    def test_contradiction_target_must_belong_to_same_organization(self) -> None:
        other_ids = self.create_additional_scope()
        other = self.approve_for_scope(
            other_ids,
            repository_package(other_ids, lesson_id="DL-OTHER-CONTRADICTION"),
            "a" * 64,
        )
        package = repository_package(self.ids, lesson_id="DL-CONTRADICTION-CROSS-ORG")
        package["atomic_assertions"][0]["contradicts"] = [other["assertions"][0]["id"]]

        with self.assertRaisesRegex(ValueError, "same organization"):
            self.approve(package=package, digest="b" * 64)

    def test_get_requires_matching_organization(self) -> None:
        lesson = self.approve()

        with self.assertRaises(KeyError):
            self.repository.get_design_lesson(lesson["id"], organization_id="another-organization")

        fetched = self.repository.get_design_lesson(
            lesson["id"], organization_id=self.ids["organization_id"]
        )
        self.assertEqual(fetched["id"], lesson["id"])

    def test_owner_audit_get_resolves_history_lineage_and_immutable_evidence(self) -> None:
        lesson = self.approve()
        opaque_ref = "design-lesson-" + hashlib.sha256(
            lesson["id"].encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(PermissionError, "family_owner"):
            self.repository.get_design_lesson_audit(
                lesson_id=lesson["id"],
                organization_id=self.ids["organization_id"],
                reviewer_id=self.ids["reviewer_id"],
            )
        with self.assertRaisesRegex(PermissionError, "configured organization"):
            self.repository.get_design_lesson_audit(
                lesson_id=opaque_ref,
                organization_id="another-organization",
                reviewer_id=self.ids["owner_id"],
            )
        audit = self.repository.get_design_lesson_audit(
            lesson_id=opaque_ref,
            organization_id=self.ids["organization_id"],
            reviewer_id=self.ids["owner_id"],
        )

        self.assertEqual(audit["id"], lesson["id"])
        self.assertEqual(audit["design_lesson_ref"], opaque_ref)
        self.assertEqual(len(audit["review_history"]), 2)
        self.assertEqual(audit["lineage"], [{
            "id": lesson["id"],
            "revision": 1,
            "status": "approved",
            "supersedes": None,
        }])
        self.assertEqual(
            {item["evidence_id"] for item in audit["evidence_artifacts"]},
            {"approved-working-copy-snapshot", "validation-evidence"},
        )
        validation = next(
            item for item in audit["evidence_artifacts"]
            if item["evidence_id"] == "validation-evidence"
        )
        self.assertEqual(validation["validation_kind"], "geometry_model")
        self.assertEqual(validation["working_copy_id"], self.ids["working_copy_id"])

    def test_english_full_text_search_indexes_problem_prevention_and_applicability(self) -> None:
        package = repository_package(self.ids, lesson_id="DL-FTS-CONTENT-001")
        package["problem"]["failure_modes"] = ["brinelling damage"]
        package["prevention"]["required_checks"] = ["Confirm lubrication before release"]
        package["applicability"]["component_classes"] = ["journal bearing"]
        package["search_terms"] = ["unrelated exact phrase"]
        lesson = self.approve(package=package, digest="1" * 64)

        page = self.repository.search_approved_design_lesson_page(
            organization_id=self.ids["organization_id"],
            query="brinelling lubrication journal",
            page_size=10,
        )

        self.assertEqual([item["id"] for item in page["items"]], [lesson["id"]])

    def test_search_cursor_pages_without_duplicates_and_is_filter_bound(self) -> None:
        expected_ids: set[str] = set()
        for index, digest_character in enumerate(("5", "6", "7")):
            package = repository_package(
                self.ids,
                lesson_id=f"DL-CURSOR-PAGE-{index}",
            )
            package["search_terms"] = ["cursor pagination proof"]
            lesson = self.approve(package=package, digest=digest_character * 64)
            expected_ids.add(lesson["id"])

        first = self.repository.search_approved_design_lesson_page(
            organization_id=self.ids["organization_id"],
            query="cursor pagination proof",
            page_size=2,
        )
        self.assertIsNotNone(first["next_cursor"])
        second = self.repository.search_approved_design_lesson_page(
            organization_id=self.ids["organization_id"],
            query="cursor pagination proof",
            page_size=2,
            cursor=first["next_cursor"],
        )

        returned_ids = [item["id"] for item in [*first["items"], *second["items"]]]
        self.assertEqual(
            set(returned_ids),
            expected_ids,
            f"first={[(item['id'], item.get('exact_match'), item.get('text_rank'), item.get('trigram_similarity'), item.get('approved_at')) for item in first['items']]}; "
            f"second={[(item['id'], item.get('exact_match'), item.get('text_rank'), item.get('trigram_similarity'), item.get('approved_at')) for item in second['items']]}",
        )
        self.assertEqual(len(returned_ids), len(set(returned_ids)))
        self.assertIsNone(second["next_cursor"])
        with self.assertRaisesRegex(ValueError, "cursor does not match"):
            self.repository.search_approved_design_lesson_page(
                organization_id=self.ids["organization_id"],
                query="different query",
                page_size=2,
                cursor=first["next_cursor"],
            )


if __name__ == "__main__":
    unittest.main()
