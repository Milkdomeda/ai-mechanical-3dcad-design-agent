from __future__ import annotations

from contextlib import contextmanager
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Callable
import unittest
from uuid import UUID, uuid4

import pytest

from mechanical_design_agent import jobs as jobs_module
from mechanical_design_agent.jobs import (
    DesignJobManager,
    DesignJobManifest,
    DesignJobRepairResult,
    JobFailure,
    locked_job_root,
    managed_job_path,
)
from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.repository import PostgresRepository
from mechanical_design_agent.workspace_bootstrap import WorkspaceManifest


ORGANIZATION_ID = "organization-001"
DESIGN_GROUP_ID = "design-group-001"
ACTOR_ID = "actor-001"


class _Rows:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Transaction:
    def __init__(self, connection: "_JobConnection") -> None:
        self.connection = connection
        self.snapshot: tuple[dict[str, dict[str, object]], dict[tuple[str, str], str], list[dict[str, object]]] | None = None

    def __enter__(self) -> None:
        self.snapshot = copy.deepcopy(
            (
                self.connection.jobs_by_id,
                self.connection.job_ids_by_token,
                self.connection.job_events,
            )
        )
        self.connection.transaction_events.append("begin")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is not None:
            assert self.snapshot is not None
            (
                self.connection.jobs_by_id,
                self.connection.job_ids_by_token,
                self.connection.job_events,
            ) = self.snapshot
        self.connection.transaction_events.append(
            "rollback" if exc_type is not None else "commit"
        )


class _JobConnection:
    def __init__(self) -> None:
        self.jobs_by_id: dict[str, dict[str, object]] = {}
        self.job_ids_by_token: dict[tuple[str, str], str] = {}
        self.job_events: list[dict[str, object]] = []
        self.actor_organizations = {
            ACTOR_ID: ORGANIZATION_ID,
            "actor-other": "organization-other",
        }
        self.queries: list[str] = []
        self.query_calls: list[tuple[str, tuple[object, ...]]] = []
        self.transaction_events: list[str] = []
        self.fail_event_insert = False
        self.inject_display_conflict = False

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def _authorized(
        self,
        row: dict[str, object] | None,
        organization_id: object,
        design_group_id: object,
        actor_id: object | None = None,
    ) -> bool:
        if (
            row is None
            or row["organization_id"] != organization_id
            or row["design_group_id"] != design_group_id
        ):
            return False
        return actor_id is None or self.actor_organizations.get(str(actor_id)) == organization_id

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Rows:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        self.query_calls.append((normalized, parameters))

        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return _Rows([{}])

        if normalized.startswith("SELECT COALESCE(MAX(CAST(substring(display_id FROM 14)"):
            workspace_id, pattern = parameters
            prefix = str(pattern).removesuffix("%")
            sequences = [
                int(str(row["display_id"])[13:])
                for row in self.jobs_by_id.values()
                if row["workspace_id"] == str(workspace_id)
                and str(row["display_id"]).startswith(prefix)
            ]
            return _Rows([{"next_sequence": max(sequences, default=0) + 1}])

        if normalized.startswith("INSERT INTO design_jobs"):
            (
                job_id,
                workspace_id,
                display_id,
                job_type,
                title,
                slug,
                status,
                phase,
                organization_id,
                design_group_id,
                family_id,
                idempotency_token,
                actor_id,
            ) = parameters
            token_key = (str(workspace_id), str(idempotency_token))
            if self.inject_display_conflict:
                self.inject_display_conflict = False
                conflicting_id = "job-concurrent-display-allocation"
                self.jobs_by_id[conflicting_id] = {
                    "id": conflicting_id,
                    "workspace_id": str(workspace_id),
                    "display_id": display_id,
                    "job_type": job_type,
                    "title": "Concurrent Job",
                    "slug": "concurrent-job",
                    "status": "active",
                    "phase": phase,
                    "revision": 0,
                    "organization_id": "organization-other",
                    "design_group_id": "design-group-other",
                    "family_id": None,
                    "directory_name": None,
                    "idempotency_token": "concurrent-token",
                    "blocked_reason": None,
                    "provisioning_state": "provisioning",
                    "created_by": "actor-other",
                }
                self.job_ids_by_token[(str(workspace_id), "concurrent-token")] = (
                    conflicting_id
                )
                return _Rows()
            if token_key in self.job_ids_by_token:
                return _Rows()
            row: dict[str, object] = {
                "id": str(job_id),
                "workspace_id": str(workspace_id),
                "display_id": display_id,
                "job_type": job_type,
                "title": title,
                "slug": slug,
                "status": status,
                "phase": phase,
                "revision": 0,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "family_id": family_id,
                "directory_name": None,
                "idempotency_token": idempotency_token,
                "blocked_reason": None,
                "provisioning_state": "provisioning",
                "created_by": actor_id,
            }
            self.jobs_by_id[str(job_id)] = row
            self.job_ids_by_token[token_key] = str(job_id)
            return _Rows([row])

        if normalized.startswith(
            "SELECT * FROM design_jobs WHERE workspace_id=%s AND idempotency_token=%s"
        ):
            workspace_id, token, organization_id, design_group_id = parameters
            job_id = self.job_ids_by_token.get((str(workspace_id), str(token)))
            row = self.jobs_by_id.get(job_id) if job_id is not None else None
            return _Rows([row] if self._authorized(row, organization_id, design_group_id) else [])

        if normalized.startswith("UPDATE design_jobs SET directory_name=%s"):
            directory_name, job_id, expected_revision, organization_id, design_group_id, actor_id = parameters
            row = self.jobs_by_id.get(str(job_id))
            if (
                not self._authorized(row, organization_id, design_group_id, actor_id)
                or row["revision"] != expected_revision
                or row["provisioning_state"] != "provisioning"
                or row["directory_name"] is not None
            ):
                return _Rows()
            row["directory_name"] = directory_name
            row["provisioning_state"] = "ready"
            row["revision"] = int(row["revision"]) + 1
            return _Rows([row])

        if normalized.startswith("UPDATE design_jobs SET status=%s,phase=%s"):
            (
                status,
                phase,
                blocked_reason,
                job_id,
                expected_revision,
                organization_id,
                design_group_id,
                actor_id,
            ) = parameters
            row = self.jobs_by_id.get(str(job_id))
            if (
                not self._authorized(row, organization_id, design_group_id, actor_id)
                or row["revision"] != expected_revision
                or row["provisioning_state"] != "ready"
            ):
                return _Rows()
            row["status"] = status
            row["phase"] = phase
            row["blocked_reason"] = json.loads(str(blocked_reason)) if blocked_reason else None
            if "active_working_copy_id=NULL" in normalized:
                row["active_working_copy_id"] = None
            row["revision"] = int(row["revision"]) + 1
            return _Rows([row])

        if normalized.startswith("SELECT id,revision,provisioning_state,directory_name FROM design_jobs"):
            job_id, organization_id, design_group_id, actor_id = parameters
            row = self.jobs_by_id.get(str(job_id))
            if not self._authorized(row, organization_id, design_group_id, actor_id):
                return _Rows()
            return _Rows([
                {
                    "id": row["id"],
                    "revision": row["revision"],
                    "provisioning_state": row["provisioning_state"],
                    "directory_name": row["directory_name"],
                }
            ])

        if normalized.startswith("INSERT INTO design_job_events"):
            if self.fail_event_insert:
                raise RuntimeError("event insert failed")
            (
                job_id,
                revision,
                event_type,
                status,
                phase,
                provisioning_state,
                directory_name,
                blocked_reason,
                actor_id,
                reason,
            ) = parameters
            self.job_events.append(
                {
                    "job_id": str(job_id),
                    "revision": revision,
                    "event_type": event_type,
                    "status": status,
                    "phase": phase,
                    "provisioning_state": provisioning_state,
                    "directory_name": directory_name,
                    "blocked_reason": (
                        json.loads(str(blocked_reason)) if blocked_reason else None
                    ),
                    "actor_id": actor_id,
                    "reason": reason,
                }
            )
            return _Rows()

        if normalized.startswith(
            "SELECT * FROM design_jobs WHERE organization_id=%s AND design_group_id=%s"
        ):
            if "status=ANY(%s::text[])" in normalized:
                (
                    organization_id,
                    design_group_id,
                    statuses,
                    job_type,
                    job_type_match,
                    family_id,
                    family_id_match,
                    *query_parts,
                ) = parameters
            else:
                (
                    organization_id,
                    design_group_id,
                    status,
                    status_match,
                    job_type,
                    job_type_match,
                    family_id,
                    family_id_match,
                ) = parameters
                assert status == status_match
                statuses = [status] if status is not None else [
                    "active",
                    "blocked",
                    "completed",
                    "cancelled",
                    "archived",
                ]
                query_parts = []
            assert job_type == job_type_match
            assert family_id == family_id_match
            phrase = str(query_parts[0]).strip("%") if query_parts else ""
            rows = [
                row
                for row in self.jobs_by_id.values()
                if row["organization_id"] == organization_id
                and row["design_group_id"] == design_group_id
                and row["status"] in statuses
                and (job_type is None or row["job_type"] == job_type)
                and (family_id is None or row["family_id"] == family_id)
                and (
                    not phrase
                    or phrase.lower() in str(row["display_id"]).lower()
                    or phrase.lower() in str(row["title"]).lower()
                    or phrase.lower() in str(row["slug"]).lower()
                )
            ]
            return _Rows(rows)

        if normalized.startswith("SELECT * FROM design_jobs WHERE id=%s"):
            job_id, organization_id, design_group_id = parameters
            row = self.jobs_by_id.get(str(job_id))
            return _Rows([row] if self._authorized(row, organization_id, design_group_id) else [])

        raise AssertionError(f"unexpected query: {normalized}")


def _repository(connection: _JobConnection) -> PostgresRepository:
    repository = PostgresRepository("postgresql://unused")

    @contextmanager
    def fake_connection():
        yield connection

    repository.connection = fake_connection  # type: ignore[method-assign]
    return repository


def _create(
    repository: PostgresRepository,
    *,
    job_id: str = "job-001",
    idempotency_token: str = "request-001",
    organization_id: str = ORGANIZATION_ID,
    design_group_id: str = DESIGN_GROUP_ID,
    actor_id: str = ACTOR_ID,
) -> dict[str, object]:
    return repository.create_design_job(
        job_id=job_id,
        workspace_id="workspace-001",
        display_date="2026-08-23",
        job_type="mechanical_design",
        title="Pump housing redesign",
        slug="pump-housing-redesign",
        organization_id=organization_id,
        design_group_id=design_group_id,
        family_id="family-001" if organization_id == ORGANIZATION_ID else None,
        idempotency_token=idempotency_token,
        actor_id=actor_id,
    )


def _record_directory(repository: PostgresRepository, *, expected_revision: int = 0) -> dict[str, object]:
    return repository.record_design_job_directory(
        job_id="job-001",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=expected_revision,
        directory_name="JOB-20260823-001-pump-housing-redesign",
        actor_id=ACTOR_ID,
    )


def test_create_design_job_is_idempotent_and_records_initial_revision_event() -> None:
    connection = _JobConnection()
    repository = _repository(connection)

    created = _create(repository)
    replayed = _create(repository, job_id="job-ignored-on-replay")

    assert replayed == created
    assert created["revision"] == 0
    assert created["status"] == "active"
    assert created["phase"] == "requirements"
    assert created["provisioning_state"] == "provisioning"
    assert [event["event_type"] for event in connection.job_events] == ["created"]
    assert connection.transaction_events == ["begin", "commit", "begin", "commit"]
    replay_query = next(
        query
        for query in connection.queries
        if query.startswith("SELECT * FROM design_jobs WHERE workspace_id=%s")
    )
    assert "organization_id=%s AND design_group_id=%s" in replay_query


def test_scoped_reads_writes_and_resolver_do_not_reveal_foreign_jobs() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    created = _create(repository)

    assert repository.get_design_job(
        job_id="job-001",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    ) == created
    with pytest.raises(KeyError, match="unknown design_job_id or unauthorized"):
        repository.get_design_job(
            job_id="job-001",
            organization_id="organization-other",
            design_group_id=DESIGN_GROUP_ID,
        )
    with pytest.raises(KeyError, match="unknown design_job_id or unauthorized"):
        repository.record_design_job_directory(
            job_id="job-001",
            organization_id="organization-other",
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=0,
            directory_name="foreign-attempt",
            actor_id="actor-other",
        )
    assert created["revision"] == 0
    assert created["directory_name"] is None

    candidates = repository.resolve_design_jobs(
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        query="pump",
        job_type=None,
        family_id=None,
    )
    assert candidates == [created]
    assert repository.resolve_design_jobs(
        organization_id="organization-other",
        design_group_id=DESIGN_GROUP_ID,
        query="pump",
        job_type=None,
        family_id=None,
    ) == []
    get_query = next(query for query in connection.queries if query.startswith("SELECT * FROM design_jobs WHERE id=%s"))
    assert "organization_id=%s AND design_group_id=%s" in get_query


def test_resolver_returns_candidates_without_silently_selecting_one() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    first = _create(repository)
    second = _create(
        repository,
        job_id="job-002",
        idempotency_token="request-002",
    )
    archived = copy.deepcopy(first)
    archived.update({"id": "job-archived", "status": "archived", "title": "Pump archive"})
    connection.jobs_by_id["job-archived"] = archived

    assert repository.resolve_design_jobs(
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        query="pump",
        job_type="mechanical_design",
        family_id="family-001",
    ) == [first, second]


def test_transition_requires_completed_provisioning_and_rejects_stale_or_foreign_actor() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    _create(repository)

    with pytest.raises(ValueError, match="provisioning is incomplete"):
        repository.transition_design_job(
            job_id="job-001",
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=0,
            status="blocked",
            phase="design",
            actor_id=ACTOR_ID,
            reason="Awaiting operating-load limits",
        )

    directory_recorded = _record_directory(repository)
    assert directory_recorded["revision"] == 1
    with pytest.raises(ValueError, match="directory already recorded"):
        _record_directory(repository, expected_revision=1)
    with pytest.raises(ValueError, match="stale design job revision"):
        repository.transition_design_job(
            job_id="job-001",
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=0,
            status="blocked",
            phase="design",
            actor_id=ACTOR_ID,
            reason="Awaiting operating-load limits",
        )
    with pytest.raises(KeyError, match="unknown design_job_id or unauthorized"):
        repository.transition_design_job(
            job_id="job-001",
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=1,
            status="blocked",
            phase="design",
            actor_id="actor-other",
            reason="Foreign actor must not mutate",
        )

    blocked = repository.transition_design_job(
        job_id="job-001",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=1,
        status="blocked",
        phase="design",
        actor_id=ACTOR_ID,
        reason="Awaiting operating-load limits",
    )
    assert blocked["revision"] == 2
    assert blocked["blocked_reason"] == {"reason": "Awaiting operating-load limits"}
    transition_query = next(
        query for query in connection.queries if query.startswith("UPDATE design_jobs SET status=%s,phase=%s")
    )
    assert "WHERE id=%s AND revision=%s AND organization_id=%s AND design_group_id=%s" in transition_query


def test_event_insert_failure_rolls_back_the_job_mutation() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    created = _create(repository)
    connection.fail_event_insert = True

    with pytest.raises(RuntimeError, match="event insert failed"):
        _record_directory(repository)

    assert created["revision"] == 0
    assert created["directory_name"] is None
    assert created["provisioning_state"] == "provisioning"
    persisted = connection.jobs_by_id["job-001"]
    assert persisted["revision"] == 0
    assert persisted["directory_name"] is None
    assert persisted["provisioning_state"] == "provisioning"
    assert [event["event_type"] for event in connection.job_events] == ["created"]
    assert connection.transaction_events[-2:] == ["begin", "rollback"]


def test_job_list_reads_return_complete_rows_and_remain_scoped() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    created = _create(repository)

    assert repository.list_design_jobs(
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        status="active",
        job_type="mechanical_design",
        family_id="family-001",
    ) == [created]
    assert repository.list_design_jobs(
        organization_id="organization-other",
        design_group_id=DESIGN_GROUP_ID,
        status=None,
        job_type=None,
        family_id=None,
    ) == []


def test_terminal_job_transition_atomically_releases_only_the_active_slot() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    created = _create(repository)
    ready = _record_directory(repository)
    connection.jobs_by_id[str(created["id"])]["active_working_copy_id"] = (
        "50000000-0000-4000-8000-000000000001"
    )

    closed = repository.transition_design_job(
        job_id=str(created["id"]),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=int(ready["revision"]),
        status="completed",
        phase="completed",
        actor_id=ACTOR_ID,
        reason="release the active revision slot",
    )

    assert closed["active_working_copy_id"] is None
    transition_query = next(
        query
        for query in connection.queries
        if query.startswith("UPDATE design_jobs SET status=%s,phase=%s")
    )
    assert "active_working_copy_id=NULL" in transition_query


def test_repository_allocates_display_ids_globally_under_transactional_locks() -> None:
    connection = _JobConnection()
    repository = _repository(connection)

    first = _create(repository)
    replayed = _create(repository, job_id="job-replay")
    foreign_scope = _create(
        repository,
        job_id="job-foreign",
        idempotency_token="request-foreign",
        organization_id="organization-other",
        design_group_id="design-group-other",
        actor_id="actor-other",
    )

    assert first["display_id"] == replayed["display_id"] == "JOB-20260823-001"
    assert foreign_scope["display_id"] == "JOB-20260823-002"
    advisory = [
        (query, parameters)
        for query, parameters in connection.query_calls
        if query.startswith("SELECT pg_advisory_xact_lock")
    ]
    assert any("design-job-token:" in str(parameters[0]) for _, parameters in advisory)
    assert any("design-job-display:" in str(parameters[0]) for _, parameters in advisory)
    allocation_query = next(
        query
        for query in connection.queries
        if "MAX" in query and "display_id" in query and "workspace_id=%s" in query
    )
    assert "organization_id" not in allocation_query


def test_repository_retries_a_display_id_conflict_without_exposing_foreign_scope() -> None:
    connection = _JobConnection()
    connection.inject_display_conflict = True
    repository = _repository(connection)

    created = _create(repository)

    assert created["display_id"] == "JOB-20260823-002"
    assert created["organization_id"] == ORGANIZATION_ID
    assert len(
        [query for query in connection.queries if query.startswith("INSERT INTO design_jobs")]
    ) == 2


LIVE_DATABASE_URL = os.environ.get("MECH_DESIGN_DATABASE_URL", "").strip()


@unittest.skipUnless(
    LIVE_DATABASE_URL,
    "MECH_DESIGN_DATABASE_URL is not configured; live Job allocation race skipped",
)
class LiveDesignJobAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repositories = [
            PostgresRepository(LIVE_DATABASE_URL),
            PostgresRepository(LIVE_DATABASE_URL),
        ]
        with postgres_migrations_directory() as migrations:
            cls.repositories[0].apply_migrations(migrations)

    def setUp(self) -> None:
        suffix = uuid4().hex
        self.workspace_id = str(uuid4())
        self.organization_ids = [f"job-race-org-a-{suffix}", f"job-race-org-b-{suffix}"]
        self.design_group_ids = [
            f"job-race-group-a-{suffix}",
            f"job-race-group-b-{suffix}",
        ]
        self.actor_ids = [f"job-race-actor-a-{suffix}", f"job-race-actor-b-{suffix}"]
        with self.repositories[0].connection() as connection, connection.transaction():
            for organization_id, design_group_id, actor_id in zip(
                self.organization_ids,
                self.design_group_ids,
                self.actor_ids,
                strict=True,
            ):
                connection.execute(
                    "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                    (organization_id, organization_id),
                )
                connection.execute(
                    "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                    (design_group_id, organization_id, design_group_id),
                )
                connection.execute(
                    "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,%s)",
                    (actor_id, organization_id, actor_id, "family_owner"),
                )

    def tearDown(self) -> None:
        with self.repositories[0].connection() as connection, connection.transaction():
            connection.execute(
                "ALTER TABLE design_job_events DISABLE TRIGGER "
                "design_job_events_append_only"
            )
            connection.execute(
                "DELETE FROM design_job_events WHERE job_id IN "
                "(SELECT id FROM design_jobs WHERE workspace_id=%s)",
                (self.workspace_id,),
            )
            connection.execute(
                "DELETE FROM design_jobs WHERE workspace_id=%s", (self.workspace_id,)
            )
            connection.execute("DELETE FROM actors WHERE id=ANY(%s)", (self.actor_ids,))
            connection.execute(
                "DELETE FROM design_groups WHERE id=ANY(%s)",
                (self.design_group_ids,),
            )
            connection.execute(
                "DELETE FROM organizations WHERE id=ANY(%s)",
                (self.organization_ids,),
            )
            connection.execute(
                "ALTER TABLE design_job_events ENABLE TRIGGER "
                "design_job_events_append_only"
            )

    def test_concurrent_scopes_receive_distinct_workspace_global_display_ids(self) -> None:
        barrier = threading.Barrier(2)

        def create(index: int) -> dict[str, object]:
            barrier.wait(timeout=5)
            return self.repositories[index].create_design_job(
                job_id=str(uuid4()),
                workspace_id=self.workspace_id,
                display_date="2026-08-23",
                job_type="mechanical_design",
                title=f"Race Job {index}",
                slug=f"race-job-{index}",
                organization_id=self.organization_ids[index],
                design_group_id=self.design_group_ids[index],
                family_id=None,
                idempotency_token=f"race-token-{index}",
                actor_id=self.actor_ids[index],
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            rows = list(executor.map(create, (0, 1)))

        self.assertEqual(
            {str(row["display_id"]) for row in rows},
            {"JOB-20260823-001", "JOB-20260823-002"},
        )
        for index, row in enumerate(rows):
            self.assertEqual(row["organization_id"], self.organization_ids[index])
            self.assertEqual(row["design_group_id"], self.design_group_ids[index])


WORKSPACE_ID = UUID("20000000-0000-4000-8000-000000000001")
JOB_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 23, 8, 15, 30, tzinfo=timezone.utc)


class _ManagerRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.tokens: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _scope(
        row: dict[str, object], organization_id: str, design_group_id: str
    ) -> bool:
        return (
            row["organization_id"] == organization_id
            and row["design_group_id"] == design_group_id
        )

    def create_design_job(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", dict(kwargs)))
        token_key = (str(kwargs["workspace_id"]), str(kwargs["idempotency_token"]))
        existing = self.tokens.get(token_key)
        if existing is not None:
            row = self.jobs[existing]
            if not self._scope(
                row,
                str(kwargs["organization_id"]),
                str(kwargs["design_group_id"]),
            ):
                raise KeyError("unknown design_job_id or unauthorized")
            return copy.deepcopy(row)
        row: dict[str, object] = {
            "id": str(kwargs["job_id"]),
            "workspace_id": str(kwargs["workspace_id"]),
            "display_id": f"JOB-{str(kwargs['display_date']).replace('-', '')}-{len(self.jobs) + 1:03d}",
            "job_type": kwargs["job_type"],
            "title": kwargs["title"],
            "slug": kwargs["slug"],
            "status": "active",
            "phase": (
                "requirements"
                if kwargs["job_type"] == "mechanical_design"
                else "intake"
            ),
            "revision": 0,
            "organization_id": kwargs["organization_id"],
            "design_group_id": kwargs["design_group_id"],
            "family_id": kwargs["family_id"],
            "directory_name": None,
            "idempotency_token": kwargs["idempotency_token"],
            "blocked_reason": None,
            "provisioning_state": "provisioning",
            "created_by": kwargs["actor_id"],
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.jobs[str(row["id"])] = row
        self.tokens[token_key] = str(row["id"])
        return copy.deepcopy(row)

    def record_design_job_directory(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("record_directory", dict(kwargs)))
        row = self.get_design_job(
            job_id=str(kwargs["job_id"]),
            organization_id=str(kwargs["organization_id"]),
            design_group_id=str(kwargs["design_group_id"]),
        )
        persisted = self.jobs[str(kwargs["job_id"])]
        if row["revision"] != kwargs["expected_revision"]:
            raise ValueError("stale design job revision")
        if row["directory_name"] is not None:
            raise ValueError("design job directory already recorded")
        persisted["directory_name"] = kwargs["directory_name"]
        persisted["provisioning_state"] = "ready"
        persisted["revision"] = int(persisted["revision"]) + 1
        persisted["updated_at"] = NOW
        return copy.deepcopy(persisted)

    def transition_design_job(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("transition", dict(kwargs)))
        row = self.get_design_job(
            job_id=str(kwargs["job_id"]),
            organization_id=str(kwargs["organization_id"]),
            design_group_id=str(kwargs["design_group_id"]),
        )
        persisted = self.jobs[str(kwargs["job_id"])]
        if row["revision"] != kwargs["expected_revision"]:
            raise ValueError("stale design job revision")
        persisted["status"] = kwargs["status"]
        persisted["phase"] = kwargs["phase"]
        if kwargs["status"] in {"completed", "cancelled", "archived"}:
            persisted["active_working_copy_id"] = None
        persisted["revision"] = int(persisted["revision"]) + 1
        persisted["updated_at"] = NOW
        return copy.deepcopy(persisted)

    def get_design_job(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", dict(kwargs)))
        row = self.jobs.get(str(kwargs["job_id"]))
        if row is None or not self._scope(
            row,
            str(kwargs["organization_id"]),
            str(kwargs["design_group_id"]),
        ):
            raise KeyError("unknown design_job_id or unauthorized")
        return copy.deepcopy(row)

    def list_design_jobs(self, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append(("list", dict(kwargs)))
        return [
            copy.deepcopy(row)
            for row in self.jobs.values()
            if self._scope(
                row,
                str(kwargs["organization_id"]),
                str(kwargs["design_group_id"]),
            )
            and (kwargs["status"] is None or row["status"] == kwargs["status"])
            and (
                kwargs["job_type"] is None or row["job_type"] == kwargs["job_type"]
            )
            and (kwargs["family_id"] is None or row["family_id"] == kwargs["family_id"])
        ]

    def resolve_design_jobs(self, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append(("resolve", dict(kwargs)))
        query = str(kwargs["query"]).casefold()
        statuses = tuple(kwargs.get("statuses", ("active", "blocked")))
        return [
            copy.deepcopy(row)
            for row in self.jobs.values()
            if self._scope(
                row,
                str(kwargs["organization_id"]),
                str(kwargs["design_group_id"]),
            )
            and row["status"] in statuses
            and (
                kwargs.get("job_type") is None
                or row["job_type"] == kwargs["job_type"]
            )
            and (
                kwargs.get("family_id") is None
                or row["family_id"] == kwargs["family_id"]
            )
            and any(
                query in str(row[field]).casefold()
                for field in ("display_id", "title", "slug")
            )
        ]


def _workspace(tmp_path: Path) -> WorkspaceManifest:
    workspace = tmp_path / "机械设计 workspace"
    (workspace / "data/artifacts").mkdir(parents=True)
    (workspace / "config/product_families").mkdir(parents=True)
    (workspace / "config/standard_parts_sources.json").write_text(
        "{}", encoding="utf-8"
    )
    (workspace / "jobs").mkdir()
    return WorkspaceManifest(
        workspace=workspace,
        workspace_id=WORKSPACE_ID,
        actor_id=ACTOR_ID,
        artifact_root=workspace / "data/artifacts",
        standard_parts_sources=workspace / "config/standard_parts_sources.json",
        product_families=workspace / "config/product_families",
        default_product_family_id=None,
        freecad_command=None,
        raw={"paths": {"jobs_root": "jobs"}},
    )


def _manager(
    tmp_path: Path,
    repository: _ManagerRepository | None = None,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> tuple[DesignJobManager, _ManagerRepository]:
    selected_repository = repository or _ManagerRepository()
    return (
        DesignJobManager(
            _workspace(tmp_path),
            selected_repository,
            uuid_factory=lambda: JOB_ID,
            now_factory=lambda: NOW,
            checkpoint=checkpoint,
        ),
        selected_repository,
    )


def _create_managed_job(manager: DesignJobManager) -> DesignJobManifest:
    return manager.create(
        job_type="mechanical_design",
        title="Pump / housing 设计",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        family_id="family-001",
        idempotency_token="request-001",
        actor_id=ACTOR_ID,
    )


def _repair(manager: DesignJobManager, **kwargs: object):
    """Use the public receipt-bound repair contract in Task 3 regressions."""
    report = manager.doctor(
        job_id=str(kwargs["job_id"]),
        organization_id=str(kwargs["organization_id"]),
        design_group_id=str(kwargs["design_group_id"]),
    )
    return manager.repair(
        **kwargs,
        expected_revision=int(report["authoritative_revision"]),
        doctor_receipt_hash=str(report["receipt_sha256"]),
        reason="repair regression",
    )


def test_manager_provisions_exact_portable_layout_and_manifest(tmp_path: Path) -> None:
    manager, repository = _manager(tmp_path)

    manifest = _create_managed_job(manager)

    assert manifest.job_id == JOB_ID
    assert manifest.workspace_id == WORKSPACE_ID
    assert manifest.display_id == "JOB-20260823-001"
    assert manifest.slug == "pump-housing-设计"
    assert manifest.phase == "requirements"
    assert manifest.revision == 1
    assert manifest.active_working_copy_id is None
    assert manifest.source_snapshots == ()
    assert manifest.directory_name == "JOB-20260823-001-pump-housing-设计"
    root = manager.workspace.jobs_root / manifest.directory_name
    expected_directories = {
        "inputs/source",
        "requirements/draft",
        "requirements/approved",
        "models/working",
        "models/revisions",
        "models/exports",
        "components/standard-parts",
        "analysis",
        "validation/specifications",
        "validation/reports",
        "validation/images",
        "knowledge/retrieval-receipts",
        "knowledge/extracted",
        "knowledge/design-lessons",
        "previews",
        "delivery",
        "provenance",
        "logs",
    }
    assert expected_directories <= {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    payload = json.loads((root / "job.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "MechanicalDesignJob/v1"
    assert payload["created_at"] == "2026-08-23T08:15:30.000000Z"
    assert payload["updated_at"] == "2026-08-23T08:15:30.000000Z"
    assert payload["source_snapshots"] == []
    assert payload["active_working_copy_id"] is None
    assert not any(str(manager.workspace.workspace) in str(value) for value in payload.values())
    scoped_directory_call = next(
        values for name, values in repository.calls if name == "record_directory"
    )
    assert scoped_directory_call["organization_id"] == ORGANIZATION_ID
    assert scoped_directory_call["design_group_id"] == DESIGN_GROUP_ID
    assert not (manager.workspace.jobs_root / ".provisioning" / str(JOB_ID)).exists()


def test_same_token_reuses_identity_and_immutable_directory(tmp_path: Path) -> None:
    manager, repository = _manager(tmp_path)
    first = _create_managed_job(manager)

    replayed = manager.create(
        job_type="mechanical_design",
        title="A renamed title must not rename storage",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        family_id="family-001",
        idempotency_token="request-001",
        actor_id=ACTOR_ID,
    )

    assert replayed == first
    assert len(repository.jobs) == 1
    final_directories = [
        path
        for path in manager.workspace.jobs_root.iterdir()
        if path.is_dir() and path.name != ".provisioning"
    ]
    assert [path.name for path in final_directories] == [first.directory_name]


@pytest.mark.parametrize(
    "checkpoint_name",
    (
        "after_db_provisioning",
        "after_temporary_directory",
        "after_manifest_write",
        "after_atomic_rename",
        "after_directory_record",
    ),
)
def test_creation_retry_recovers_each_crash_boundary_without_a_second_job(
    tmp_path: Path, checkpoint_name: str
) -> None:
    tripped = False

    def fail_once(name: str) -> None:
        nonlocal tripped
        if name == checkpoint_name and not tripped:
            tripped = True
            raise RuntimeError(f"injected {name}")

    manager, repository = _manager(tmp_path, checkpoint=fail_once)
    with pytest.raises(RuntimeError, match=f"injected {checkpoint_name}"):
        _create_managed_job(manager)

    recovered = _create_managed_job(manager)

    assert recovered.job_id == JOB_ID
    assert len(repository.jobs) == 1
    final_directories = [
        path
        for path in manager.workspace.jobs_root.iterdir()
        if path.is_dir() and path.name != ".provisioning"
    ]
    assert [path.name for path in final_directories] == [recovered.directory_name]
    assert not (manager.workspace.jobs_root / ".provisioning" / str(JOB_ID)).exists()


@pytest.mark.parametrize(
    "value",
    (
        "../escape.FCStd",
        "inputs/../../escape.FCStd",
        "/tmp/escape.FCStd",
        "C:\\escape.FCStd",
        "C:/escape.FCStd",
        "inputs\\source\\escape.FCStd",
        "inputs/source/../escape.FCStd",
    ),
)
def test_managed_job_path_rejects_traversal_and_platform_absolute_spellings(
    tmp_path: Path, value: str
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name

    with pytest.raises(JobFailure) as captured:
        managed_job_path(job_root=root, relative_path=value, allow_missing_leaf=True)

    assert captured.value.code == "JOB_PATH_OUTSIDE"


def test_managed_job_path_supports_spaces_and_unicode_but_rejects_symlink(
    tmp_path: Path,
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name
    safe = root / "analysis" / "载荷 case 1"
    safe.mkdir()
    assert managed_job_path(
        job_root=root,
        relative_path="analysis/载荷 case 1/result.json",
        allow_missing_leaf=True,
    ) == safe / "result.json"

    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "analysis" / "escape-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(JobFailure) as captured:
        managed_job_path(
            job_root=root,
            relative_path="analysis/escape-link/model.FCStd",
            allow_missing_leaf=True,
        )
    assert captured.value.code == "JOB_PATH_UNSAFE"


def test_manifest_validation_rejects_invalid_uuid_hash_timestamp_phase_and_path() -> None:
    valid: dict[str, object] = {
        "schema_version": "MechanicalDesignJob/v1",
        "job_id": str(JOB_ID),
        "display_id": "JOB-20260823-001",
        "job_type": "mechanical_design",
        "workspace_id": str(WORKSPACE_ID),
        "title": "Pump",
        "slug": "pump",
        "status": "active",
        "phase": "requirements",
        "revision": 1,
        "organization_id": ORGANIZATION_ID,
        "design_group_id": DESIGN_GROUP_ID,
        "family_id": None,
        "directory_name": "JOB-20260823-001-pump",
        "active_working_copy_id": None,
        "source_snapshots": [
            {
                "snapshot_id": "30000000-0000-4000-8000-000000000001",
                "stored_path": "inputs/source/原始 model.FCStd",
                "sha256": "a" * 64,
                "source_kind": "existing_model",
                "source_model_revision_id": "40000000-0000-4000-8000-000000000001",
            }
        ],
        "created_at": "2026-08-23T08:15:30.000000Z",
        "created_by": ACTOR_ID,
        "updated_at": "2026-08-23T08:15:30.000000Z",
    }
    assert DesignJobManifest.from_dict(valid).source_snapshots[0]["sha256"] == "a" * 64

    invalid_cases = (
        ("job_id", "not-a-uuid"),
        ("phase", "database_publication"),
        ("created_at", "yesterday"),
        ("display_id", "job-1"),
    )
    for field, value in invalid_cases:
        payload = copy.deepcopy(valid)
        payload[field] = value
        with pytest.raises(JobFailure, match="JOB_MANIFEST_INVALID"):
            DesignJobManifest.from_dict(payload)

    for field, value in (
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("stored_path", "../source.FCStd"),
        ("stored_path", "C:\\source.FCStd"),
    ):
        payload = copy.deepcopy(valid)
        snapshots = payload["source_snapshots"]
        assert isinstance(snapshots, list)
        snapshots[0][field] = value
        with pytest.raises(JobFailure, match="JOB_MANIFEST_INVALID"):
            DesignJobManifest.from_dict(payload)

    unexpected_path = copy.deepcopy(valid)
    unexpected_path["original_source_path"] = "/Users/private/source.FCStd"
    with pytest.raises(JobFailure, match="JOB_MANIFEST_INVALID"):
        DesignJobManifest.from_dict(unexpected_path)


@pytest.mark.parametrize(
    "relative_path",
    (
        "analysis/bad<name>.json",
        "analysis/bad>name.json",
        'analysis/bad"name.json',
        "analysis/bad|name.json",
        "analysis/bad?name.json",
        "analysis/bad*name.json",
        "analysis/control\x01name.json",
        "analysis/control\x7fname.json",
        "analysis/" + "a" * 256,
        "analysis/COM¹.txt",
    ),
)
def test_portable_paths_reject_windows_invalid_characters_and_lengths(
    tmp_path: Path, relative_path: str
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name

    with pytest.raises(JobFailure) as captured:
        managed_job_path(
            job_root=root,
            relative_path=relative_path,
            allow_missing_leaf=True,
        )

    assert captured.value.code == "JOB_PATH_OUTSIDE"


def test_manifest_rejects_casefolded_snapshot_path_collisions() -> None:
    payload: dict[str, object] = {
        "schema_version": "MechanicalDesignJob/v1",
        "job_id": str(JOB_ID),
        "display_id": "JOB-20260823-001",
        "job_type": "mechanical_design",
        "workspace_id": str(WORKSPACE_ID),
        "title": "Pump",
        "slug": "pump",
        "status": "active",
        "phase": "requirements",
        "revision": 1,
        "organization_id": ORGANIZATION_ID,
        "design_group_id": DESIGN_GROUP_ID,
        "family_id": None,
        "directory_name": "JOB-20260823-001-pump",
        "active_working_copy_id": None,
        "source_snapshots": [
            {
                "snapshot_id": "30000000-0000-4000-8000-000000000001",
                "stored_path": "inputs/source/É.FCStd",
                "sha256": "a" * 64,
                "source_kind": "existing_model",
                "source_model_revision_id": None,
            },
            {
                "snapshot_id": "30000000-0000-4000-8000-000000000002",
                "stored_path": "inputs/source/é.fcstd",
                "sha256": "b" * 64,
                "source_kind": "existing_model",
                "source_model_revision_id": None,
            },
        ],
        "created_at": "2026-08-23T08:15:30.000000Z",
        "created_by": ACTOR_ID,
        "updated_at": "2026-08-23T08:15:30.000000Z",
    }

    with pytest.raises(JobFailure, match="JOB_MANIFEST_INVALID"):
        DesignJobManifest.from_dict(payload)


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-23T08:15:30Z",
        "2026-08-23T08:15:30.000Z",
        "2026-08-23T08:15:30.000000+00:00",
        "2026-08-23T16:15:30.000000+08:00",
        "2026-08-23t08:15:30.000000z",
    ),
)
def test_manifest_rejects_noncanonical_rfc3339_timestamp_spellings(
    timestamp: str,
) -> None:
    payload = DesignJobManifest(
        job_id=JOB_ID,
        display_id="JOB-20260823-001",
        job_type="mechanical_design",
        workspace_id=WORKSPACE_ID,
        title="Pump",
        slug="pump",
        status="active",
        phase="requirements",
        revision=1,
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        family_id=None,
        directory_name="JOB-20260823-001-pump",
        active_working_copy_id=None,
        source_snapshots=(),
        created_at="2026-08-23T08:15:30.000000Z",
        created_by=ACTOR_ID,
        updated_at="2026-08-23T08:15:30.000000Z",
    ).as_dict()
    payload["updated_at"] = timestamp

    with pytest.raises(JobFailure, match="JOB_MANIFEST_INVALID"):
        DesignJobManifest.from_dict(payload)


def test_manifest_preserves_canonical_rfc3339_microsecond_precision() -> None:
    payload = DesignJobManifest(
        job_id=JOB_ID,
        display_id="JOB-20260823-001",
        job_type="mechanical_design",
        workspace_id=WORKSPACE_ID,
        title="Pump",
        slug="pump",
        status="active",
        phase="requirements",
        revision=1,
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        family_id=None,
        directory_name="JOB-20260823-001-pump",
        active_working_copy_id=None,
        source_snapshots=(),
        created_at="2026-08-23T08:15:30.123456Z",
        created_by=ACTOR_ID,
        updated_at="2026-08-23T08:15:31.654321Z",
    ).as_dict()

    parsed = DesignJobManifest.from_dict(payload)

    assert parsed.created_at == "2026-08-23T08:15:30.123456Z"
    assert parsed.updated_at == "2026-08-23T08:15:31.654321Z"


def test_doctor_reports_hand_edit_and_revision_mismatch_then_repair_republishes(
    tmp_path: Path,
) -> None:
    manager, repository = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name
    manifest_path = root / "job.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["title"] = "hand edited"
    payload["revision"] = 99
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )

    assert report["status"] == "blocked"
    assert {issue["code"] for issue in report["issues"]} >= {
        "JOB_MANIFEST_MISMATCH",
        "JOB_REVISION_MISMATCH",
    }
    model = root / "models/working/pump.FCStd"
    model.write_bytes(b"changed model bytes must be preserved")
    repaired = _repair(manager,
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        actor_id=ACTOR_ID,
    )
    assert repaired.revision == repository.jobs[str(JOB_ID)]["revision"]
    assert repaired.title == "Pump / housing 设计"
    assert model.read_bytes() == b"changed model bytes must be preserved"
    assert manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )["status"] == "ok"


def test_authoritative_working_copy_and_snapshot_project_into_get_doctor_and_repair(
    tmp_path: Path,
) -> None:
    manager, repository = _manager(tmp_path)
    created = _create_managed_job(manager)
    root = manager.workspace.jobs_root / created.directory_name
    working_copy_id = "50000000-0000-4000-8000-000000000001"
    snapshot_id = "30000000-0000-4000-8000-000000000001"
    model_revision_id = "40000000-0000-4000-8000-000000000001"
    snapshot_path = root / "inputs/source" / snapshot_id / "source.FCStd"
    snapshot_path.parent.mkdir()
    snapshot_path.write_bytes(b"authoritative source snapshot")
    snapshot_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    working_path = root / "models/working" / working_copy_id / "working.FCStd"
    working_path.parent.mkdir()
    working_path.write_bytes(snapshot_path.read_bytes())
    authoritative = repository.jobs[str(JOB_ID)]
    authoritative["active_working_copy_id"] = working_copy_id
    authoritative["active_working_path"] = str(working_path.resolve())
    authoritative["source_snapshots"] = [
        {
            "snapshot_id": snapshot_id,
            "stored_path": snapshot_path.relative_to(root).as_posix(),
            "sha256": snapshot_sha256,
            "source_kind": "existing_model",
            "source_model_revision_id": model_revision_id,
        }
    ]
    authoritative["revision"] = created.revision + 1

    with locked_job_root(job_root=root) as locked:
        projected = manager.publish_authoritative_manifest_locked(
            locked_root=locked,
            job_id=str(JOB_ID),
            expected_job_revision=created.revision,
            working_copy_id=working_copy_id,
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
        )

    assert projected.active_working_copy_id == working_copy_id
    assert projected.source_snapshots[0]["snapshot_id"] == snapshot_id
    assert manager.get(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    ) == projected
    doctor = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    assert doctor["status"] == "ok"
    assert doctor["verified_snapshots"] == [dict(projected.source_snapshots[0])]
    active_evidence = doctor["verified_active_working_copy"]
    assert active_evidence == {
        "working_copy_id": working_copy_id,
        "relative_path": working_path.relative_to(root).as_posix(),
        "identity": active_evidence["identity"],
        "sha256": hashlib.sha256(working_path.read_bytes()).hexdigest(),
        "size_bytes": len(working_path.read_bytes()),
    }
    assert set(active_evidence["identity"]) == {"volume", "file_index"}

    payload = json.loads((root / "job.json").read_text(encoding="utf-8"))
    payload["source_snapshots"] = []
    payload["active_working_copy_id"] = None
    (root / "job.json").write_text(json.dumps(payload), encoding="utf-8")
    damaged = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    repaired = manager.repair(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        actor_id=ACTOR_ID,
        expected_revision=projected.revision,
        doctor_receipt_hash=str(damaged["receipt_sha256"]),
        reason="restore authoritative Task 6 bindings",
    )
    assert repaired.active_working_copy_id == working_copy_id
    assert repaired.source_snapshots[0]["snapshot_id"] == snapshot_id

    snapshot_bytes = snapshot_path.read_bytes()
    working_bytes = working_path.read_bytes()
    closed = manager.close(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=repaired.revision,
        status="completed",
        phase="completed",
        actor_id=ACTOR_ID,
        reason="release the active working-copy slot",
    )
    assert closed.active_working_copy_id is None
    assert closed.source_snapshots == repaired.source_snapshots
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert working_path.read_bytes() == working_bytes

    reopened = manager.reopen(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=closed.revision,
        phase="design",
        actor_id=ACTOR_ID,
        reason="start a new immutable working revision",
    )
    assert reopened.active_working_copy_id is None
    assert reopened.source_snapshots == repaired.source_snapshots
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert working_path.read_bytes() == working_bytes
    historical = repository.jobs[str(JOB_ID)]
    assert historical["source_snapshots"] == authoritative["source_snapshots"]
    assert working_path.exists()


def test_repair_rejects_active_working_copy_changed_after_doctor_receipt(
    tmp_path: Path,
) -> None:
    manager, repository = _manager(tmp_path)
    created = _create_managed_job(manager)
    root = manager.workspace.jobs_root / created.directory_name
    working_copy_id = "50000000-0000-4000-8000-000000000001"
    working_path = root / "models/working" / working_copy_id / "working.FCStd"
    working_path.parent.mkdir()
    working_path.write_bytes(b"doctor-bound-working-copy")
    authoritative = repository.jobs[str(JOB_ID)]
    authoritative["active_working_copy_id"] = working_copy_id
    authoritative["active_working_path"] = str(working_path.resolve())
    authoritative["revision"] = created.revision + 1
    with locked_job_root(job_root=root) as locked:
        projected = manager.publish_authoritative_manifest_locked(
            locked_root=locked,
            job_id=str(JOB_ID),
            expected_job_revision=created.revision,
            working_copy_id=working_copy_id,
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
        )
    report = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    working_path.write_bytes(b"changed-between-doctor-and-repair")

    with pytest.raises(JobFailure) as captured:
        manager.repair(
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            actor_id=ACTOR_ID,
            expected_revision=projected.revision,
            doctor_receipt_hash=str(report["receipt_sha256"]),
            reason="must not repair from stale model evidence",
        )

    assert captured.value.code == "JOB_DOCTOR_RECEIPT_MISMATCH"


def test_doctor_fails_closed_before_manifest_reads_when_locked_authority_is_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RevokingRepository(_ManagerRepository):
        fail_after: int | None = None

        def get_design_job(self, **kwargs: object) -> dict[str, object]:
            if self.fail_after is not None and len(self.calls) > self.fail_after:
                raise KeyError("private Job title and path")
            return super().get_design_job(**kwargs)

    repository = RevokingRepository()
    manager, _ = _manager(tmp_path, repository)
    _create_managed_job(manager)
    repository.fail_after = len(repository.calls)
    monkeypatch.setattr(
        manager,
        "_locked_doctor_evidence",
        lambda _root: pytest.fail("doctor read the managed Job after authorization failed"),
    )

    with pytest.raises(JobFailure) as captured:
        manager.doctor(
            job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
        )

    assert captured.value.code == "JOB_ACCESS_UNAVAILABLE"
    assert "private" not in str(captured.value)


def test_repair_refuses_manifest_identity_change(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    manifest_path = manager.workspace.jobs_root / manifest.directory_name / "job.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["job_id"] = "50000000-0000-4000-8000-000000000001"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobFailure) as captured:
        _repair(manager,
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            actor_id=ACTOR_ID,
        )

    assert captured.value.code == "JOB_REPAIR_UNSAFE"


def test_repair_recomputes_the_doctor_receipt_under_its_job_lock(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    report = manager.doctor(
        job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    path = manager.workspace.jobs_root / manifest.directory_name / "job.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revision"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobFailure) as captured:
        manager.repair(
            job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID, actor_id=ACTOR_ID,
            expected_revision=manifest.revision,
            doctor_receipt_hash=str(report["receipt_sha256"]),
            reason="repair receipt regression",
        )

    assert captured.value.code == "JOB_DOCTOR_RECEIPT_MISMATCH"


def test_repair_uses_only_the_pinned_receipt_evidence_after_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    report = manager.doctor(
        job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    manifest_path = manager.workspace.jobs_root / manifest.directory_name / "job.json"

    original_evidence = manager._locked_doctor_evidence

    def fail_second_evidence_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("repair must not acquire filesystem evidence after receipt creation")

    def arm_after_evidence(*, locked: Path, row: dict[str, object]):
        evidence = original_evidence(locked=locked, row=row)
        # Every semantic reader/enumerator used by this implementation is
        # armed immediately after the receipt evidence is complete. Publishing
        # remains intentionally unguarded: it may validate its write target.
        monkeypatch.setattr(jobs_module, "read_managed_file", fail_second_evidence_access)
        monkeypatch.setattr(jobs_module, "list_managed_directory", fail_second_evidence_access)
        monkeypatch.setattr(jobs_module, "_read_json_with_evidence", fail_second_evidence_access)
        monkeypatch.setattr(jobs_module, "_read_json", fail_second_evidence_access)
        return evidence

    def mutate_after_receipt(name: str) -> None:
        if name == "after_repair_receipt_comparison":
            manifest_path.write_text(
                json.dumps({"job_id": "forged-second-read"}), encoding="utf-8"
            )

    monkeypatch.setattr(manager, "_checkpoint", mutate_after_receipt)
    monkeypatch.setattr(manager, "_locked_doctor_evidence", arm_after_evidence)
    repaired = manager.repair(
        job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID, actor_id=ACTOR_ID,
        expected_revision=manifest.revision,
        doctor_receipt_hash=str(report["receipt_sha256"]),
        reason="pinned evidence regression",
    )

    assert isinstance(repaired, DesignJobRepairResult)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["job_id"] == str(JOB_ID)
    assert repaired.audit["reason"] == "pinned evidence regression"


def test_repair_result_has_an_exact_recursive_v1_schema(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    report = manager.doctor(
        job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    result = manager.repair(
        job_id=str(JOB_ID), organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID, actor_id=ACTOR_ID,
        expected_revision=manifest.revision,
        doctor_receipt_hash=str(report["receipt_sha256"]),
        reason="exact response schema",
    )
    payload = result.as_dict()

    assert set(payload) == {"schema_version", "job", "audit"}
    assert payload["schema_version"] == "MechanicalDesignJobRepair/v1"
    assert set(payload["audit"]) == {
        "action", "reason", "actor_id", "authoritative_revision"
    }
    parsed = DesignJobRepairResult.from_dict(payload)
    assert parsed.manifest.as_dict() == payload["job"]
    with pytest.raises(JobFailure):
        DesignJobRepairResult.from_dict({**payload, "repair_audit": {}})


def test_get_doctor_and_repair_reject_forged_operational_bindings(
    tmp_path: Path,
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    manifest_path = manager.workspace.jobs_root / manifest.directory_name / "job.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["active_working_copy_id"] = "50000000-0000-4000-8000-000000000001"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobFailure) as get_failure:
        manager.get(
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
        )
    report = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    with pytest.raises(JobFailure) as repair_failure:
        _repair(manager,
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            actor_id=ACTOR_ID,
        )

    assert get_failure.value.code == "JOB_OPERATIONAL_BINDING_FORGED"
    assert {issue["code"] for issue in report["issues"]} >= {
        "JOB_OPERATIONAL_BINDING_FORGED"
    }
    assert repair_failure.value.code == "JOB_REPAIR_UNSAFE"


def test_missing_manifest_repair_validates_the_full_directory_contract(
    tmp_path: Path,
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name
    (root / "job.json").unlink()
    (root / "validation/images").rmdir()

    with pytest.raises(JobFailure) as captured:
        _repair(manager,
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            actor_id=ACTOR_ID,
        )

    assert captured.value.code == "JOB_REPAIR_UNSAFE"


def test_doctor_and_missing_manifest_repair_use_only_pinned_read_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name
    assert manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )["status"] == "ok"

    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "read_text",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("detached read_text is forbidden")
            ),
        )
        assert manager.doctor(
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
        )["status"] == "ok"

    (root / "job.json").unlink()
    with monkeypatch.context() as scoped:
        scoped.setattr(
            Path,
            "iterdir",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("detached iterdir is forbidden")
            ),
        )
        repaired = _repair(manager,
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            actor_id=ACTOR_ID,
        )
    assert repaired.revision == manifest.revision


def test_repair_freshens_authority_after_lock_when_transition_commits_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, repository = _manager(tmp_path)
    manifest = _create_managed_job(manager)
    root = manager.workspace.jobs_root / manifest.directory_name
    original_lock = jobs_module.locked_job_root
    injected = False

    @contextmanager
    def transition_while_waiting(*, job_root: Path):
        nonlocal injected
        with original_lock(job_root=job_root) as locked:
            if not injected:
                injected = True
                repository.transition_design_job(
                    job_id=str(JOB_ID),
                    organization_id=ORGANIZATION_ID,
                    design_group_id=DESIGN_GROUP_ID,
                    expected_revision=manifest.revision,
                    status="completed",
                    phase="completed",
                    actor_id=ACTOR_ID,
                    reason="concurrent authoritative transition",
                )
            yield locked

    monkeypatch.setattr(jobs_module, "locked_job_root", transition_while_waiting)
    repaired = _repair(manager,
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        actor_id=ACTOR_ID,
    )

    assert repaired.revision == 2
    assert repaired.status == "completed"
    on_disk = json.loads((root / "job.json").read_text(encoding="utf-8"))
    assert on_disk["revision"] == 2
    assert on_disk["status"] == "completed"


def test_doctor_receipt_is_bound_to_workspace_revision_manifest_and_timestamp(
    tmp_path: Path,
) -> None:
    manager, _ = _manager(tmp_path)
    created = _create_managed_job(manager)
    manifest_path = manager.workspace.jobs_root / created.directory_name / "job.json"

    first = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )
    assert first["workspace_id"] == str(WORKSPACE_ID)
    assert first["authoritative_revision"] == created.revision
    assert first["authoritative_updated_at"] == "2026-08-23T08:15:30.000000Z"
    assert first["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert first["verified_snapshots"] == []

    closed = manager.close(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=created.revision,
        status="cancelled",
        phase="requirements",
        actor_id=ACTOR_ID,
        reason="receipt revision regression",
    )
    second = manager.doctor(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
    )

    assert second["authoritative_revision"] == closed.revision
    assert second["receipt_sha256"] != first["receipt_sha256"]


def test_lifecycle_projection_failure_is_typed_and_repairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, repository = _manager(tmp_path)
    created = _create_managed_job(manager)
    original_replace = jobs_module.atomic_replace
    failed = False

    def fail_final_projection(path: Path, content: bytes) -> None:
        nonlocal failed
        if path.name == "job.json" and not failed:
            failed = True
            raise OSError("injected final projection failure")
        original_replace(path, content)

    monkeypatch.setattr(jobs_module, "atomic_replace", fail_final_projection)
    with pytest.raises(JobFailure) as captured:
        manager.close(
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=created.revision,
            status="cancelled",
            phase="requirements",
            actor_id=ACTOR_ID,
            reason="projection failure regression",
        )

    assert captured.value.code == "JOB_PROJECTION_INCOMPLETE"
    assert repository.jobs[str(JOB_ID)]["revision"] == created.revision + 1
    repaired = _repair(manager,
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        actor_id=ACTOR_ID,
    )
    assert repaired.revision == created.revision + 1


def test_lifecycle_and_candidate_methods_republish_and_keep_scope(tmp_path: Path) -> None:
    manager, repository = _manager(tmp_path)
    created = _create_managed_job(manager)

    assert manager.list(
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        status="active",
        job_type=None,
        family_id=None,
    ) == [created]
    assert manager.resolve(
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        query="housing",
        job_type=None,
        family_id=None,
    ) == [created]
    closed = manager.close(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=created.revision,
        status="completed",
        phase="completed",
        actor_id=ACTOR_ID,
        reason="All governed gates completed",
    )
    reopened = manager.reopen(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=closed.revision,
        phase="lesson_capture",
        actor_id=ACTOR_ID,
        reason="Capture a corrected lesson",
    )

    assert closed.status == "completed"
    assert reopened.status == "active"
    assert reopened.phase == "lesson_capture"
    for name, values in repository.calls:
        if name in {"get", "transition", "resolve", "list"}:
            assert values["organization_id"] == ORGANIZATION_ID
            assert values["design_group_id"] == DESIGN_GROUP_ID
    on_disk = json.loads(
        (
            manager.workspace.jobs_root
            / reopened.directory_name
            / "job.json"
        ).read_text(encoding="utf-8")
    )
    assert on_disk["revision"] == reopened.revision
    assert on_disk["status"] == "active"


def test_close_rejects_a_terminal_job_without_mutating_it(tmp_path: Path) -> None:
    manager, repository = _manager(tmp_path)
    created = _create_managed_job(manager)
    closed = manager.close(
        job_id=str(JOB_ID),
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        expected_revision=created.revision,
        status="cancelled",
        phase="requirements",
        actor_id=ACTOR_ID,
        reason="User cancelled the design request",
    )

    with pytest.raises(JobFailure) as captured:
        manager.close(
            job_id=str(JOB_ID),
            organization_id=ORGANIZATION_ID,
            design_group_id=DESIGN_GROUP_ID,
            expected_revision=closed.revision,
            status="archived",
            phase="requirements",
            actor_id=ACTOR_ID,
            reason="Terminal state must be reopened before another write",
        )

    assert captured.value.code == "JOB_TERMINAL"
    assert repository.jobs[str(JOB_ID)]["revision"] == closed.revision
