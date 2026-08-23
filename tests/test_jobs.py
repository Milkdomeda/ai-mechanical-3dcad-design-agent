from __future__ import annotations

from contextlib import contextmanager
import copy
import json

import pytest

from mechanical_design_agent.repository import PostgresRepository


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
        self.transaction_events: list[str] = []
        self.fail_event_insert = False

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
    display_id: str = "JOB-20260823-001",
    idempotency_token: str = "request-001",
) -> dict[str, object]:
    return repository.create_design_job(
        job_id=job_id,
        workspace_id="workspace-001",
        display_id=display_id,
        job_type="mechanical_design",
        title="Pump housing redesign",
        slug="pump-housing-redesign",
        organization_id=ORGANIZATION_ID,
        design_group_id=DESIGN_GROUP_ID,
        family_id="family-001",
        idempotency_token=idempotency_token,
        actor_id=ACTOR_ID,
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
        display_id="JOB-20260823-002",
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
