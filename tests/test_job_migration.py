from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from uuid import UUID, uuid5
import zipfile

import pytest

from mechanical_design_agent.job_migration import LegacyJobMigration
from mechanical_design_agent.jobs import JobFailure


def _fcstd() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Document.xml",
            '<Document SchemaVersion="4" ProgramVersion="1.1.3"><ObjectData/></Document>',
        )
    return output.getvalue()


class _Repository:
    def __init__(
        self,
        rows: list[dict[str, object]],
        binding_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.rows = rows
        self.binding_rows = binding_rows or []

    def list_legacy_working_copies(self, **scope: str) -> list[dict[str, object]]:
        assert scope == {"organization_id": "org-001", "design_group_id": "group-001"}
        return [dict(row) for row in self.rows]

    def list_legacy_migration_bindings(self, **scope: str) -> list[dict[str, object]]:
        assert scope == {
            "workspace_id": "20000000-0000-4000-8000-000000000001",
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        return [dict(row) for row in self.binding_rows]


@dataclass
class _Manifest:
    job_id: UUID
    revision: int = 1
    active_working_copy_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": str(self.job_id),
            "revision": self.revision,
            "active_working_copy_id": self.active_working_copy_id,
        }


class _Jobs:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = SimpleNamespace(
            workspace_id=UUID("20000000-0000-4000-8000-000000000001")
        )
        self.manifests: dict[str, _Manifest] = {}
        self.tokens: list[str] = []
        self.lock_calls = 0

    def create(self, **values: object) -> _Manifest:
        token = str(values["idempotency_token"])
        self.tokens.append(token)
        if token not in self.manifests:
            job_id = uuid5(UUID("10000000-0000-4000-8000-000000000001"), token)
            job_root = self.root / str(job_id)
            job_root.mkdir()
            self.manifests[token] = _Manifest(job_id=job_id)
        return self.manifests[token]

    @contextmanager
    def locked_active_mechanical_design_job(
        self, **values: object
    ) -> Iterator[tuple[Path, dict[str, object]]]:
        self.lock_calls += 1
        job_id = str(values["job_id"])
        manifest = next(
            item for item in self.manifests.values() if str(item.job_id) == job_id
        )
        assert values["expected_job_revision"] == manifest.revision
        yield self.root / job_id, manifest.as_dict()


class _Design:
    def __init__(self, jobs: _Jobs) -> None:
        self.jobs = jobs
        self.calls: list[dict[str, object]] = []

    def migrate_legacy_working_copy(self, **values: object) -> dict[str, object]:
        self.calls.append(dict(values))
        legacy_id = str(values["legacy_working_copy_id"])
        working_copy_id = str(uuid5(UUID(str(values["job_id"])), f"legacy:{legacy_id}"))
        manifest = next(
            item
            for item in self.jobs.manifests.values()
            if str(item.job_id) == str(values["job_id"])
        )
        if manifest.active_working_copy_id is None:
            manifest.active_working_copy_id = working_copy_id
            manifest.revision += 1
        assert manifest.active_working_copy_id == working_copy_id
        return {"id": working_copy_id, "job": manifest.as_dict()}


def _migration(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[LegacyJobMigration, _Jobs, _Design]:
    jobs = _Jobs(tmp_path / "jobs")
    jobs.root.mkdir()
    design = _Design(jobs)
    migration = LegacyJobMigration(
        repository=_Repository(rows),
        jobs=jobs,  # type: ignore[arg-type]
        design=design,  # type: ignore[arg-type]
        actor_id="actor-001",
        organization_id="org-001",
        design_group_id="group-001",
    )
    return migration, jobs, design


def test_legacy_migration_dry_run_is_stable_and_rejects_unsafe_fcstd(tmp_path: Path) -> None:
    safe = tmp_path / "safe.FCStd"
    unsafe = tmp_path / "unsafe.FCStd"
    safe.write_bytes(_fcstd())
    unsafe.write_bytes(b"not an FCStd archive")
    migration, _, _ = _migration(
        tmp_path,
        [
            {"id": "10000000-0000-4000-8000-000000000011", "family_id": None, "working_path": str(safe)},
            {"id": "10000000-0000-4000-8000-000000000012", "family_id": "family-001", "working_path": str(unsafe)},
        ],
    )

    first = migration.dry_run()
    second = migration.dry_run()

    assert first == second
    assert [item["status"] for item in first["items"]] == ["ready", "blocked"]
    assert first["items"][1]["source_sha256"] is None
    assert len(str(first["receipt_sha256"])) == 64


def test_legacy_migration_requires_current_plan_and_exact_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "legacy.FCStd"
    source.write_bytes(_fcstd())
    migration, _, _ = _migration(
        tmp_path,
        [{"id": "10000000-0000-4000-8000-000000000021", "family_id": None, "working_path": str(source)}],
    )
    plan = migration.dry_run()
    receipt = str(plan["receipt_sha256"])

    with pytest.raises(JobFailure) as invalid:
        migration.apply(plan=plan, receipt_sha256=receipt, confirmation="yes")
    assert invalid.value.code == "JOB_CONFIRMATION_INVALID"

    source.write_bytes(_fcstd() + b"changed")
    with pytest.raises(JobFailure) as stale:
        migration.apply(
            plan=plan,
            receipt_sha256=receipt,
            confirmation=f"迁移旧设计 {receipt}",
        )
    assert stale.value.code == "JOB_MIGRATION_PLAN_STALE"


def test_legacy_migration_creates_independent_jobs_and_is_retry_safe(tmp_path: Path) -> None:
    sources = [tmp_path / "first.FCStd", tmp_path / "second.FCStd"]
    for source in sources:
        source.write_bytes(_fcstd())
    rows = [
        {"id": f"10000000-0000-4000-8000-00000000003{index}", "family_id": None, "working_path": str(source)}
        for index, source in enumerate(sources, start=1)
    ]
    migration, jobs, design = _migration(tmp_path, rows)
    plan = migration.dry_run()
    receipt = str(plan["receipt_sha256"])

    first = migration.apply(
        plan=plan,
        receipt_sha256=receipt,
        confirmation=f"迁移旧设计 {receipt}",
    )
    second = migration.apply(
        plan=plan,
        receipt_sha256=receipt,
        confirmation=f"迁移旧设计 {receipt}",
    )

    assert first == second
    assert len({item["job_id"] for item in first["migrated"]}) == 2
    assert len({item["working_copy_id"] for item in first["migrated"]}) == 2
    assert len(set(jobs.tokens)) == 2
    assert len(design.calls) == 4
    assert jobs.lock_calls == 4
    for item in first["migrated"]:
        assert Path(str(item["receipt_path"])).is_file()
        assert item["legacy_source_retained"] is True


def test_legacy_migration_doctor_distinguishes_retained_sources_from_failures(
    tmp_path: Path,
) -> None:
    payload = _fcstd()
    source = tmp_path / "legacy.FCStd"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    job_id = "30000000-0000-4000-8000-000000000001"
    working_copy_id = "40000000-0000-4000-8000-000000000001"
    job_root = tmp_path / "jobs" / job_id
    target = job_root / "models" / "working" / working_copy_id / "working.FCStd"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    receipt = {
        "schema_version": "MechanicalDesignLegacyMigration/v1",
        "legacy_working_copy_id": "10000000-0000-4000-8000-000000000043",
        "legacy_source_retained": True,
        "source_sha256": digest,
        "job_id": job_id,
        "working_copy_id": working_copy_id,
        "plan_receipt_sha256": "a" * 64,
    }
    receipt_path = (
        job_root
        / "provenance"
        / "migrations"
        / "legacy-10000000-0000-4000-8000-000000000043.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    missing_target = (
        tmp_path
        / "jobs"
        / "30000000-0000-4000-8000-000000000002"
        / "models"
        / "working"
        / "40000000-0000-4000-8000-000000000002"
        / "working.FCStd"
    )
    binding_rows = [
        {
            "legacy_working_copy_id": "10000000-0000-4000-8000-000000000041",
            "legacy_working_path": str(source),
            "migration_job_id": None,
            "active_working_copy_id": None,
            "migrated_working_copy_id": None,
        },
        {
            "legacy_working_copy_id": "10000000-0000-4000-8000-000000000042",
            "legacy_working_path": str(source),
            "migration_job_id": "30000000-0000-4000-8000-000000000003",
            "active_working_copy_id": None,
            "migrated_working_copy_id": None,
        },
        {
            "legacy_working_copy_id": "10000000-0000-4000-8000-000000000043",
            "legacy_working_path": str(source),
            "migration_job_id": job_id,
            "migration_job_status": "active",
            "migration_job_revision": 2,
            "active_working_copy_id": working_copy_id,
            "migrated_working_copy_id": working_copy_id,
            "migrated_job_id": job_id,
            "migrated_working_path": str(target),
            "migrated_working_relative_path": f"models/working/{working_copy_id}/working.FCStd",
            "migrated_working_sha256": digest,
            "migrated_working_size_bytes": len(payload),
        },
        {
            "legacy_working_copy_id": "10000000-0000-4000-8000-000000000044",
            "legacy_working_path": str(source),
            "migration_job_id": "30000000-0000-4000-8000-000000000002",
            "migration_job_status": "active",
            "migration_job_revision": 2,
            "active_working_copy_id": "40000000-0000-4000-8000-000000000002",
            "migrated_working_copy_id": "40000000-0000-4000-8000-000000000002",
            "migrated_job_id": "30000000-0000-4000-8000-000000000002",
            "migrated_working_path": str(missing_target),
            "migrated_working_relative_path": "models/working/40000000-0000-4000-8000-000000000002/working.FCStd",
            "migrated_working_sha256": digest,
            "migrated_working_size_bytes": len(payload),
        },
    ]
    jobs = _Jobs(tmp_path / "unused-jobs")
    jobs.root.mkdir()
    repository = _Repository([], binding_rows)
    migration = LegacyJobMigration(
        repository=repository,
        jobs=jobs,  # type: ignore[arg-type]
        design=_Design(jobs),  # type: ignore[arg-type]
        actor_id="actor-001",
        organization_id="org-001",
        design_group_id="group-001",
    )

    result = migration.doctor()

    assert result["unmigrated_count"] == 1
    assert result["incomplete_count"] == 1
    assert result["hash_divergent_count"] == 1
    assert result["migrated_count"] == 1
    assert [item["status"] for item in result["items"]] == [
        "unmigrated",
        "incomplete",
        "migrated",
        "hash_divergent",
    ]
