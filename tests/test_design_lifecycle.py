from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from types import SimpleNamespace
import unittest
import uuid

from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.repository import PostgresRepository
from mechanical_design_agent.service import MechanicalDesignService


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _SourceConnection:
    def __init__(self, candidates):
        self.candidates = candidates
        self.parameters = None
        self.query = ""

    def execute(self, query, parameters=()):
        self.query = " ".join(query.split())
        self.parameters = parameters
        return _Rows(self.candidates)


class _LifecycleConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.target = {
            "id": "change-old",
            "working_copy_id": "working-1",
            "status": "approved",
            "applied_at": None,
        }
        self.successor = {
            "id": "change-new",
            "working_copy_id": "working-1",
            "status": "approved",
            "applied_at": None,
        }

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT * FROM actors"):
            return _Rows([{"role": "family_owner", "organization_id": "org"}])
        if normalized.startswith("SELECT * FROM design_groups"):
            return _Rows([{"id": "group", "organization_id": "org"}])
        if normalized.startswith("SELECT * FROM design_working_copies"):
            return _Rows([{
                "id": "working-1",
                "organization_id": "org",
                "design_group_id": "group",
            }])
        if normalized.startswith("SELECT role FROM actors"):
            return _Rows([{"role": "family_owner"}])
        if normalized.startswith("SELECT * FROM design_change_sets WHERE id="):
            return _Rows([self.target if parameters[0] == "change-old" else self.successor])
        if normalized.startswith("UPDATE design_change_sets SET status="):
            return _Rows([{
                **self.target,
                "status": parameters[0],
                "superseded_by_change_set_id": parameters[1],
                "closure_reason": parameters[2],
                "closed_by": parameters[3],
                "closed_at": "now",
            }])
        if normalized.startswith("SELECT count(*) AS count FROM design_change_sets"):
            return _Rows([{"count": 0}])
        if normalized.startswith("SELECT * FROM design_lesson_summaries"):
            return _Rows([{"id": "summary-1", "publication_status": "blocked"}])
        if normalized.startswith("SELECT DISTINCT ON (validation_kind)"):
            return _Rows([
                {"validation_kind": "geometry_model", "status": "passed", "working_sha256": "a" * 64},
                {"validation_kind": "assembly_completeness", "status": "passed", "working_sha256": "a" * 64},
            ])
        if normalized.startswith("UPDATE design_working_copies"):
            return _Rows([{"id": "working-1", "status": "approved_for_delivery"}])
        if normalized.startswith("UPDATE design_lesson_summaries"):
            return _Rows([{
                "id": "summary-1",
                "summary_status": "completed",
                "publication_status": "ready",
                "publication_blocker": None,
            }])
        return _Rows()


class _RetrievalRepository:
    def __init__(self, working):
        self.working = working
        self.receipt_kwargs = None
        self.summary_kwargs = None

    def get_working_copy(self, _working_copy_id):
        return self.working

    def record_retrieval_receipt(self, **kwargs):
        self.receipt_kwargs = kwargs
        return {"id": "receipt-1", **kwargs}

    def record_design_lesson_summary(self, **kwargs):
        self.summary_kwargs = kwargs
        return {
            "id": "summary-1",
            "summary_status": "completed",
            "publication_status": "blocked",
            "publication_blocker": "working copy is not approved_for_delivery",
        }


class _ChangeRecordConnection:
    def __init__(self, retrieval_status: str) -> None:
        self.retrieval_status = retrieval_status
        self.inserted = False

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT w.*,m.product_id"):
            return _Rows([{
                "id": "working-1",
                "source_model_revision_id": None,
                "product_id": None,
                "family_id": None,
                "design_group_id": "group",
            }])
        if normalized.startswith("SELECT * FROM design_retrieval_receipts"):
            return _Rows([{
                "retrieval_status": self.retrieval_status,
                "used_knowledge_ids": [],
            }])
        if normalized.startswith("INSERT INTO design_change_sets"):
            self.inserted = True
            return _Rows([{"id": "change-1", "status": "proposed"}])
        return _Rows()


class _EmptyContextBuilder:
    def __init__(self):
        self.kwargs = None

    def build(self, **kwargs):
        self.kwargs = kwargs
        return {
            "hard_constraints": [],
            "preferences": [],
            "approved_facts": [],
            "specialized_knowledge": [],
            "approved_design_lessons": [],
            "similar_models": [],
        }


class DesignLifecycleTests(unittest.TestCase):
    def test_governed_repository_create_requires_a_job_identity(self) -> None:
        repository = PostgresRepository("postgresql://unused")

        with self.assertRaisesRegex(ValueError, "job_id is required"):
            repository.create_job_working_copy(
                job_id="",
                expected_job_revision=1,
                organization_id="org",
                design_group_id="group",
                family_id=None,
                working_copy_id=str(uuid.uuid4()),
                model_revision_id=None,
                source_sha256="a" * 64,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path="/managed/models/working/model.FCStd",
                actor_id="owner",
                source_snapshot=None,
            )

    def test_existing_model_sha_unique_match_binds_revision(self) -> None:
        candidate = {
            "id": "revision-1",
            "organization_id": "org",
            "design_group_id": "group",
            "family_id": "family",
            "artifact_sha256": "a" * 64,
        }
        connection = _SourceConnection([candidate])
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        result = repository.resolve_source_model_revision(
            organization_id="org",
            design_group_id="group",
            source_sha256="a" * 64,
            requested_family_id="family",
        )

        self.assertEqual(result["id"], "revision-1")
        self.assertEqual(connection.parameters[-2:], ("family", "family"))

    def test_existing_model_non_unique_match_is_fail_closed(self) -> None:
        candidate = {
            "id": "revision-1",
            "organization_id": "org",
            "design_group_id": "group",
            "family_id": None,
            "artifact_sha256": "a" * 64,
        }
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield _SourceConnection([candidate, {**candidate, "id": "revision-2"}])

        repository.connection = fake_connection
        with self.assertRaisesRegex(ValueError, "exactly one.*found 2"):
            repository.resolve_source_model_revision(
                organization_id="org",
                design_group_id="group",
                source_sha256="a" * 64,
            )

    def test_explicit_source_revision_is_scoped_and_hashed_in_the_sql_predicate(self) -> None:
        candidate = {
            "id": "revision-1",
            "organization_id": "org",
            "design_group_id": "group",
            "family_id": "family",
            "artifact_sha256": "a" * 64,
        }
        connection = _SourceConnection([candidate])
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        repository.resolve_source_model_revision(
            organization_id="org",
            design_group_id="group",
            source_sha256="a" * 64,
            requested_model_revision_id="revision-1",
            requested_family_id="family",
        )

        self.assertIn("m.id=%s AND m.organization_id=%s", connection.query)
        self.assertIn("m.design_group_id=%s AND a.sha256=%s", connection.query)
        self.assertEqual(
            connection.parameters,
            ("revision-1", "org", "group", "a" * 64, "family", "family"),
        )

    def test_retrieval_not_executed_blocks_change_creation(self) -> None:
        connection = _ChangeRecordConnection("not_executed")
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        with self.assertRaisesRegex(ValueError, "must be completed"):
            repository.record_change_set(
                "working-1",
                "parameter_change",
                [{"parameter": "length", "delta_mm": 300}],
                [],
                "increase travel",
                "owner",
            )
        self.assertFalse(connection.inserted)

    def test_retrieval_completed_no_match_allows_change_creation(self) -> None:
        connection = _ChangeRecordConnection("completed_no_match")
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        result = repository.record_change_set(
            "working-1",
            "parameter_change",
            [{"parameter": "length", "delta_mm": 300}],
            [],
            "increase travel",
            "owner",
        )
        self.assertTrue(connection.inserted)
        self.assertEqual(result["status"], "proposed")

    def test_new_design_without_source_still_runs_all_applicable_retrieval(self) -> None:
        repository = _RetrievalRepository({
            "id": "working-1",
            "organization_id": "org",
            "design_group_id": "group",
            "family_id": "family",
            "source_model_revision_id": None,
            "design_origin": "new_design",
        })
        context_builder = _EmptyContextBuilder()
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service.repository = repository
        service.context_builder = context_builder
        service.settings = SimpleNamespace(actor_id="owner")
        service._require_database = lambda: None

        result = service.design_knowledge_retrieve(
            working_copy_id="working-1",
            query="new linear module requirements",
            design_features={},
            used_knowledge_ids=[],
        )

        self.assertIsNone(context_builder.kwargs["model_revision_id"])
        self.assertTrue(context_builder.kwargs["explicit_family_authorization"])
        self.assertEqual(result["retrieval_receipt"]["retrieval_status"], "completed_no_match")
        self.assertTrue(repository.receipt_kwargs["retrieval_scope"]["family_knowledge"])
        self.assertTrue(repository.receipt_kwargs["retrieval_scope"]["general_design_knowledge"])
        self.assertTrue(repository.receipt_kwargs["retrieval_scope"]["design_lessons"])

    def test_approved_change_can_be_superseded_and_delivery_accepts_closed_state(self) -> None:
        connection = _LifecycleConnection()
        repository = PostgresRepository("postgresql://unused")

        @contextmanager
        def fake_connection():
            yield connection

        repository.connection = fake_connection
        repository._enqueue = lambda *_args, **_kwargs: None
        closed = repository.close_change_set(
            change_set_id="change-old",
            disposition="superseded",
            reason="replaced by custom U handle",
            actor_id="owner",
            successor_change_set_id="change-new",
        )
        delivered = repository.approve_delivery(
            "working-1",
            "owner",
            "批准 working-1",
            "a" * 64,
            "/immutable/a.FCStd",
            organization_id="org",
            design_group_id="group",
        )

        self.assertEqual(closed["status"], "superseded")
        self.assertEqual(closed["superseded_by_change_set_id"], "change-new")
        delivery_query = next(
            query for query in connection.queries
            if query.startswith("SELECT count(*) AS count FROM design_change_sets")
        )
        self.assertIn("'superseded','cancelled'", delivery_query)
        self.assertEqual(delivered["status"], "approved_for_delivery")
        self.assertEqual(delivered["lesson_review_flow"]["status"], "ready")

    def test_design_confirmation_always_records_completed_lesson_summary(self) -> None:
        repository = _RetrievalRepository({})
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service.repository = repository
        service.settings = SimpleNamespace(actor_id="owner")
        service._require_database = lambda: None

        result = service.design_confirmation_record(
            working_copy_id="working-1",
            lesson_summary={"lesson": "preserve source identity"},
            confirmation="模型设计确认 working-1",
        )

        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["lesson_review_flow"]["status"], "blocked")
        self.assertEqual(repository.summary_kwargs["working_copy_id"], "working-1")


@unittest.skipUnless(
    os.environ.get("MECH_DESIGN_DATABASE_URL"),
    "MECH_DESIGN_DATABASE_URL is not configured; live source-revision test skipped",
)
class LiveSourceRevisionResolutionTests(unittest.TestCase):
    def test_optional_family_filter_is_typed_for_postgresql(self) -> None:
        database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
        repository = PostgresRepository(database_url)
        with postgres_migrations_directory() as migrations:
            repository.apply_migrations(migrations)
        token = uuid.uuid4().hex
        organization_id = f"org-source-revision-{token}"
        design_group_id = f"group-source-revision-{token}"
        family_id = f"PF-SOURCE-REVISION-{token.upper()}"
        artifact_id = str(uuid.uuid4())
        model_revision_id = str(uuid.uuid4())
        source_sha256 = token * 2

        try:
            with repository.connection() as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                    (organization_id, "Synthetic source-revision organization"),
                )
                connection.execute(
                    "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                    (
                        design_group_id,
                        organization_id,
                        "Synthetic source-revision group",
                    ),
                )
                connection.execute(
                    "INSERT INTO product_families(id,organization_id,design_group_id,"
                    "canonical_name,status,config) VALUES (%s,%s,%s,%s,'active','{}'::jsonb)",
                    (
                        family_id,
                        organization_id,
                        design_group_id,
                        "Synthetic source-revision family",
                    ),
                )
                connection.execute(
                    "INSERT INTO artifacts(id,organization_id,sha256,size_bytes,media_type,"
                    "storage_path,source_path) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        artifact_id,
                        organization_id,
                        source_sha256,
                        1,
                        "application/x-freecad",
                        f"/synthetic/{token}/source.FCStd",
                        f"/synthetic/{token}/source.FCStd",
                    ),
                )
                connection.execute(
                    "INSERT INTO model_revisions(id,organization_id,design_group_id,family_id,"
                    "source_artifact_id,source_relative_path,family_folder,parser_version,status,manifest) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)",
                    (
                        model_revision_id,
                        organization_id,
                        design_group_id,
                        family_id,
                        artifact_id,
                        "synthetic/source.FCStd",
                        "synthetic",
                        f"task3-{token}",
                        "confirmed",
                    ),
                )

            for requested_family_id in (family_id, None):
                with self.subTest(requested_family_id=requested_family_id):
                    resolved = repository.resolve_source_model_revision(
                        organization_id=organization_id,
                        design_group_id=design_group_id,
                        source_sha256=source_sha256,
                        requested_family_id=requested_family_id,
                    )
                    self.assertEqual(str(resolved["id"]), model_revision_id)
                    self.assertEqual(resolved["artifact_sha256"], source_sha256)
        finally:
            with repository.connection() as connection, connection.transaction():
                connection.execute(
                    "DELETE FROM model_revisions WHERE id=%s", (model_revision_id,)
                )
                connection.execute("DELETE FROM artifacts WHERE id=%s", (artifact_id,))
                connection.execute(
                    "DELETE FROM product_families WHERE id=%s", (family_id,)
                )
                connection.execute(
                    "DELETE FROM design_groups WHERE id=%s", (design_group_id,)
                )
                connection.execute(
                    "DELETE FROM organizations WHERE id=%s", (organization_id,)
                )


@unittest.skipUnless(
    os.environ.get("MECH_DESIGN_DATABASE_URL"),
    "MECH_DESIGN_DATABASE_URL is not configured; live Job binding race skipped",
)
class LiveJobWorkingCopyBindingTests(unittest.TestCase):
    def test_concurrent_creates_publish_exactly_one_active_working_copy(self) -> None:
        database_url = os.environ["MECH_DESIGN_DATABASE_URL"]
        repository = PostgresRepository(database_url)
        with postgres_migrations_directory() as migrations:
            repository.apply_migrations(migrations)
        token = uuid.uuid4().hex
        organization_id = f"org-job-binding-{token}"
        design_group_id = f"group-job-binding-{token}"
        actor_id = f"actor-job-binding-{token}"
        job_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        working_ids = [str(uuid.uuid4()) for _ in range(4)]
        barrier = Barrier(2)

        try:
            with repository.connection() as connection, connection.transaction():
                connection.execute(
                    "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                    (organization_id, "Job binding race organization"),
                )
                connection.execute(
                    "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                    (design_group_id, organization_id, "Job binding race group"),
                )
                connection.execute(
                    "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
                    (actor_id, organization_id, "Job binding race actor", "family_owner"),
                )
            created = repository.create_design_job(
                job_id=job_id,
                workspace_id=workspace_id,
                display_date="2026-08-23",
                job_type="mechanical_design",
                title="Concurrent Job binding",
                slug="concurrent-job-binding",
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=None,
                idempotency_token=f"binding-{token}",
                actor_id=actor_id,
            )
            ready = repository.record_design_job_directory(
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                expected_revision=int(created["revision"]),
                directory_name=f"{created['display_id']}-{created['slug']}",
                actor_id=actor_id,
            )

            def create(index: int) -> tuple[str, object]:
                contender = PostgresRepository(database_url)
                barrier.wait(timeout=5)
                try:
                    result = contender.create_job_working_copy(
                        job_id=job_id,
                        expected_job_revision=int(ready["revision"]),
                        organization_id=organization_id,
                        design_group_id=design_group_id,
                        family_id=None,
                        working_copy_id=working_ids[index],
                        model_revision_id=None,
                        source_sha256=("a" if index == 0 else "b") * 64,
                        source_kind="new_design_seed",
                        design_origin="new_design",
                        working_path=f"models/working/{working_ids[index]}/working.FCStd",
                        actor_id=actor_id,
                        source_snapshot=None,
                    )
                    return "created", result
                except ValueError as exc:
                    return "rejected", str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(create, (0, 1)))

            self.assertEqual([status for status, _ in outcomes].count("created"), 1)
            self.assertEqual([status for status, _ in outcomes].count("rejected"), 1)
            with repository.connection() as connection:
                job = connection.execute(
                    "SELECT active_working_copy_id::text,revision FROM design_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                count = connection.execute(
                    "SELECT count(*) AS count FROM design_working_copies WHERE job_id=%s",
                    (job_id,),
                ).fetchone()
            self.assertIn(job["active_working_copy_id"], working_ids)
            self.assertEqual(int(job["revision"]), int(ready["revision"]) + 1)
            self.assertEqual(int(count["count"]), 1)

            first_working_id = str(job["active_working_copy_id"])
            first_path = f"models/working/{first_working_id}/working.FCStd"
            closed = repository.transition_design_job(
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                expected_revision=int(job["revision"]),
                status="completed",
                phase="completed",
                actor_id=actor_id,
                reason="release active working-copy slot",
            )
            self.assertIsNone(closed["active_working_copy_id"])
            reopened = repository.transition_design_job(
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                expected_revision=int(closed["revision"]),
                status="active",
                phase="design",
                actor_id=actor_id,
                reason="create a later immutable working revision",
            )
            self.assertIsNone(reopened["active_working_copy_id"])
            proven_absent_id = str(uuid.uuid4())
            proven_absent = repository.reconcile_job_working_copy_publication(
                job_id=job_id,
                expected_job_revision=int(reopened["revision"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=None,
                working_copy_id=proven_absent_id,
                model_revision_id=None,
                source_sha256=None,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path=f"models/working/{proven_absent_id}/working.FCStd",
                actor_id=actor_id,
                source_snapshot=None,
            )
            self.assertEqual(proven_absent, {"status": "not_committed"})
            later = repository.create_job_working_copy(
                job_id=job_id,
                expected_job_revision=int(reopened["revision"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=None,
                working_copy_id=working_ids[2],
                model_revision_id=None,
                source_sha256="c" * 64,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path=f"models/working/{working_ids[2]}/working.FCStd",
                actor_id=actor_id,
                source_snapshot=None,
            )
            self.assertEqual(
                str(later["job"]["active_working_copy_id"]), working_ids[2]
            )
            reconciled = repository.reconcile_job_working_copy_publication(
                job_id=job_id,
                expected_job_revision=int(reopened["revision"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=None,
                working_copy_id=working_ids[2],
                model_revision_id=None,
                source_sha256="c" * 64,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path=f"models/working/{working_ids[2]}/working.FCStd",
                actor_id=actor_id,
                source_snapshot=None,
            )
            self.assertEqual(reconciled["status"], "committed")
            self.assertEqual(
                str(reconciled["publication"]["working_copy"]["id"]),
                working_ids[2],
            )
            mismatched = repository.reconcile_job_working_copy_publication(
                job_id=job_id,
                expected_job_revision=int(reopened["revision"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=None,
                working_copy_id=str(uuid.uuid4()),
                model_revision_id=None,
                source_sha256="e" * 64,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path="models/working/absent/working.FCStd",
                actor_id=actor_id,
                source_snapshot=None,
            )
            self.assertEqual(mismatched, {"status": "unknown"})
            with self.assertRaisesRegex(ValueError, "already has an active"):
                repository.create_job_working_copy(
                    job_id=job_id,
                    expected_job_revision=int(later["job"]["revision"]),
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    family_id=None,
                    working_copy_id=working_ids[3],
                    model_revision_id=None,
                    source_sha256="d" * 64,
                    source_kind="new_design_seed",
                    design_origin="new_design",
                    working_path=f"models/working/{working_ids[3]}/working.FCStd",
                    actor_id=actor_id,
                    source_snapshot=None,
                )
            with repository.connection() as connection:
                historical = connection.execute(
                    "SELECT id::text,working_path FROM design_working_copies "
                    "WHERE job_id=%s ORDER BY created_at,id",
                    (job_id,),
                ).fetchall()
            self.assertEqual(len(historical), 2)
            self.assertEqual(
                {str(row["id"]) for row in historical},
                {first_working_id, working_ids[2]},
            )
            self.assertIn(first_path, {str(row["working_path"]) for row in historical})
        finally:
            with repository.connection() as connection, connection.transaction():
                connection.execute(
                    "UPDATE design_jobs SET active_working_copy_id=NULL WHERE id=%s",
                    (job_id,),
                )
                connection.execute(
                    "DELETE FROM design_working_copies WHERE job_id=%s", (job_id,)
                )
                connection.execute(
                    "ALTER TABLE design_job_events DISABLE TRIGGER design_job_events_append_only"
                )
                connection.execute(
                    "DELETE FROM design_job_events WHERE job_id=%s", (job_id,)
                )
                connection.execute("DELETE FROM design_jobs WHERE id=%s", (job_id,))
                connection.execute(
                    "ALTER TABLE design_job_events ENABLE TRIGGER design_job_events_append_only"
                )
                connection.execute("DELETE FROM actors WHERE id=%s", (actor_id,))
                connection.execute(
                    "DELETE FROM design_groups WHERE id=%s", (design_group_id,)
                )
                connection.execute(
                    "DELETE FROM organizations WHERE id=%s", (organization_id,)
                )


if __name__ == "__main__":
    unittest.main()
