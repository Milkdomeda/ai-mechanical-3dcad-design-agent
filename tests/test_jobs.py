from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from mechanical_design_agent.repository import PostgresRepository


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

    def __enter__(self) -> None:
        self.connection.transaction_events.append("begin")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.connection.transaction_events.append(
            "rollback" if exc_type is not None else "commit"
        )


class _JobConnection:
    def __init__(self) -> None:
        self.jobs_by_id: dict[str, dict[str, object]] = {}
        self.jobs_by_token: dict[tuple[str, str], dict[str, object]] = {}
        self.job_events: list[dict[str, object]] = []
        self.queries: list[str] = []
        self.transaction_events: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)

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
            if token_key in self.jobs_by_token:
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
            self.jobs_by_token[token_key] = row
            return _Rows([row])

        if normalized.startswith(
            "SELECT * FROM design_jobs WHERE workspace_id=%s AND idempotency_token=%s"
        ):
            row = self.jobs_by_token.get((str(parameters[0]), str(parameters[1])))
            return _Rows([row] if row is not None else [])

        if normalized.startswith("UPDATE design_jobs SET directory_name=%s"):
            directory_name, job_id, expected_revision = parameters
            row = self.jobs_by_id.get(str(job_id))
            if (
                row is None
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
            status, phase, blocked_reason, job_id, expected_revision = parameters
            row = self.jobs_by_id.get(str(job_id))
            if row is None or row["revision"] != expected_revision:
                return _Rows()
            row["status"] = status
            row["phase"] = phase
            row["blocked_reason"] = json.loads(str(blocked_reason)) if blocked_reason else None
            row["revision"] = int(row["revision"]) + 1
            return _Rows([row])

        if normalized.startswith("INSERT INTO design_job_events"):
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
            organization_id, design_group_id, status, status_match, job_type, job_type_match, family_id, family_id_match = parameters
            assert status == status_match
            assert job_type == job_type_match
            assert family_id == family_id_match
            rows = [
                row
                for row in self.jobs_by_id.values()
                if row["organization_id"] == organization_id
                and row["design_group_id"] == design_group_id
                and (status is None or row["status"] == status)
                and (job_type is None or row["job_type"] == job_type)
                and (family_id is None or row["family_id"] == family_id)
            ]
            return _Rows(rows)

        if normalized.startswith("SELECT * FROM design_jobs WHERE id=%s"):
            row = self.jobs_by_id.get(str(parameters[0]))
            return _Rows([row] if row is not None else [])

        raise AssertionError(f"unexpected query: {normalized}")


def _repository(connection: _JobConnection) -> PostgresRepository:
    repository = PostgresRepository("postgresql://unused")

    @contextmanager
    def fake_connection():
        yield connection

    repository.connection = fake_connection  # type: ignore[method-assign]
    return repository


def _create(repository: PostgresRepository, *, job_id: str = "job-001") -> dict[str, object]:
    return repository.create_design_job(
        job_id=job_id,
        workspace_id="workspace-001",
        display_id="JOB-20260823-001",
        job_type="mechanical_design",
        title="Pump housing redesign",
        slug="pump-housing-redesign",
        organization_id="organization-001",
        design_group_id="design-group-001",
        family_id="family-001",
        idempotency_token="request-001",
        actor_id="actor-001",
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
    assert connection.job_events == [
        {
            "job_id": "job-001",
            "revision": 0,
            "event_type": "created",
            "status": "active",
            "phase": "requirements",
            "provisioning_state": "provisioning",
            "directory_name": None,
            "blocked_reason": None,
            "actor_id": "actor-001",
            "reason": None,
        }
    ]
    assert connection.transaction_events == ["begin", "commit", "begin", "commit"]
    assert any(
        "ON CONFLICT(workspace_id,idempotency_token) DO NOTHING" in query
        for query in connection.queries
    )


def test_directory_and_transition_are_revisioned_and_reject_stale_updates() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    _create(repository)

    directory_recorded = repository.record_design_job_directory(
        job_id="job-001",
        expected_revision=0,
        directory_name="JOB-20260823-001-pump-housing-redesign",
        actor_id="actor-001",
    )

    assert directory_recorded["revision"] == 1
    assert directory_recorded["directory_name"] == "JOB-20260823-001-pump-housing-redesign"
    assert directory_recorded["provisioning_state"] == "ready"
    assert connection.job_events[-1]["event_type"] == "directory_recorded"
    assert connection.job_events[-1]["revision"] == 1

    with pytest.raises(ValueError, match="stale design job revision"):
        repository.transition_design_job(
            job_id="job-001",
            expected_revision=0,
            status="blocked",
            phase="design",
            actor_id="actor-001",
            reason="Awaiting operating-load limits",
        )

    blocked = repository.transition_design_job(
        job_id="job-001",
        expected_revision=1,
        status="blocked",
        phase="design",
        actor_id="actor-001",
        reason="Awaiting operating-load limits",
    )

    assert blocked["revision"] == 2
    assert blocked["status"] == "blocked"
    assert blocked["phase"] == "design"
    assert blocked["blocked_reason"] == {"reason": "Awaiting operating-load limits"}
    assert connection.job_events[-1] == {
        "job_id": "job-001",
        "revision": 2,
        "event_type": "transitioned",
        "status": "blocked",
        "phase": "design",
        "provisioning_state": "ready",
        "directory_name": "JOB-20260823-001-pump-housing-redesign",
        "blocked_reason": {"reason": "Awaiting operating-load limits"},
        "actor_id": "actor-001",
        "reason": "Awaiting operating-load limits",
    }
    transition_query = next(
        query for query in connection.queries if query.startswith("UPDATE design_jobs SET status=%s,phase=%s")
    )
    assert "WHERE id=%s AND revision=%s" in transition_query


def test_job_reads_return_complete_rows_and_are_scoped() -> None:
    connection = _JobConnection()
    repository = _repository(connection)
    created = _create(repository)

    assert repository.get_design_job("job-001") == created
    with pytest.raises(KeyError, match="unknown design_job_id: missing"):
        repository.get_design_job("missing")

    assert repository.list_design_jobs(
        organization_id="organization-001",
        design_group_id="design-group-001",
        status="active",
        job_type="mechanical_design",
        family_id="family-001",
    ) == [created]
    assert repository.list_design_jobs(
        organization_id="other-organization",
        design_group_id="design-group-001",
        status=None,
        job_type=None,
        family_id=None,
    ) == []
