from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
from typing import Iterator
from uuid import UUID
import zipfile

import pytest

from mechanical_design_agent.jobs import JobFailure
from mechanical_design_agent.migrations import postgres_migrations_directory
from mechanical_design_agent.product_family_onboarding import ProductFamilyOnboarding
from mechanical_design_agent.repository import PostgresRepository


def _fcstd() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Document.xml",
            '<Document SchemaVersion="4" ProgramVersion="1.1.3"><ObjectData/></Document>',
        )
    return output.getvalue()


@dataclass
class _Manifest:
    row: dict[str, object]

    @property
    def revision(self) -> int:
        return int(self.row["revision"])

    def as_dict(self) -> dict[str, object]:
        return dict(self.row)


class _Jobs:
    def __init__(self, root: Path, *, job_type: str = "product_family_onboarding") -> None:
        self.root = root
        self.row: dict[str, object] = {
            "job_id": "50000000-0000-4000-8000-000000000001",
            "revision": 1,
            "job_type": job_type,
            "status": "active",
            "phase": "intake",
            "family_id": "family-001",
        }

    @contextmanager
    def locked_active_job(self, **values: object) -> Iterator[tuple[Path, dict[str, object]]]:
        if values["job_type"] != self.row["job_type"] or self.row["status"] != "active":
            raise JobFailure(
                "JOB_TYPE_OR_STATUS_MISMATCH",
                "operation requires an active product_family_onboarding Job",
            )
        assert values["job_id"] == self.row["job_id"]
        assert values["expected_job_revision"] == self.row["revision"]
        assert values["family_id"] == self.row["family_id"]
        yield self.root, dict(self.row)

    def publish_authoritative_revision_locked(self, **values: object) -> _Manifest:
        assert values["expected_previous_revision"] + 1 == self.row["revision"]
        return _Manifest(self.row)

    def read_authoritative_manifest_locked(self, **_values: object) -> _Manifest:
        return _Manifest(self.row)

    def get(self, **values: object) -> _Manifest:
        assert values["job_id"] == self.row["job_id"]
        return _Manifest(self.row)


class _Repository:
    def __init__(self, jobs: _Jobs) -> None:
        self.jobs = jobs
        self.run: dict[str, object] | None = None
        self.review: dict[str, object] | None = None
        self.publication: dict[str, object] | None = None
        self.outbox: list[dict[str, object]] = []

    def _advance(self, phase: str, *, completed: bool = False) -> dict[str, object]:
        self.jobs.row["revision"] = int(self.jobs.row["revision"]) + 1
        self.jobs.row["phase"] = phase
        if completed:
            self.jobs.row["status"] = "completed"
        return dict(self.jobs.row)

    def start_product_family_onboarding(self, **values: object) -> dict[str, object]:
        self.run = {
            "id": values["run_id"],
            "job_id": values["job_id"],
            "family_id": values["family_id"],
            "status": "started",
            "input_manifest": values["input_manifest"],
            "input_manifest_sha256": values["input_manifest_sha256"],
            "candidate_knowledge": None,
            "package_sha256": None,
            "review": None,
            "publication": None,
        }
        return {"run": self.run, "job": self._advance("intake"), "changed": True}

    def analyze_product_family_onboarding(self, **values: object) -> dict[str, object]:
        assert self.run is not None
        self.run.update(
            {
                "status": "analyzed",
                "analysis": values["analysis"],
                "analysis_sha256": values["analysis_sha256"],
                "candidate_knowledge": values["candidate_knowledge"],
                "package_sha256": values["package_sha256"],
            }
        )
        return {"run": self.run, "job": self._advance("analysis"), "changed": True}

    def review_product_family_onboarding(self, **values: object) -> dict[str, object]:
        assert self.run is not None
        self.review = {
            "id": values["review_id"],
            "review_identity": values["review_identity"],
            "package_sha256": values["package_sha256"],
            "decision": values["decision"],
            "job_id": values["job_id"],
        }
        self.run["status"] = "approved" if values["decision"] == "approve" else "rejected"
        self.run["review"] = self.review
        return {"review": self.review, "job": self._advance("knowledge_review"), "changed": True}

    def publish_product_family_onboarding(self, **values: object) -> dict[str, object]:
        assert self.run is not None
        self.publication = {
            "id": values["publication_id"],
            "publication_identity": values["publication_identity"],
            "package_sha256": values["package_sha256"],
            "assertion_ids": values["assertion_ids"],
            "job_id": values["job_id"],
        }
        self.run["status"] = "published"
        self.run["publication"] = self.publication
        for assertion_id in values["assertion_ids"]:
            self.outbox.append(
                {
                    "assertion_id": assertion_id,
                    "onboarding_job_id": values["job_id"],
                }
            )
        return {
            "publication": self.publication,
            "job": self._advance("completed", completed=True),
            "changed": True,
        }

    def get_product_family_onboarding(self, **values: object) -> dict[str, object]:
        assert values["job_id"] == self.jobs.row["job_id"]
        assert self.run is not None
        return dict(self.run)


def _onboarding(tmp_path: Path, *, job_type: str = "product_family_onboarding") -> tuple[ProductFamilyOnboarding, _Repository, _Jobs]:
    job_root = tmp_path / "job"
    for relative in (
        "inputs/source",
        "analysis",
        "knowledge/extracted",
        "knowledge",
    ):
        (job_root / relative).mkdir(parents=True, exist_ok=True)
    jobs = _Jobs(job_root, job_type=job_type)
    repository = _Repository(jobs)
    onboarding = ProductFamilyOnboarding(
        repository=repository,
        jobs=jobs,  # type: ignore[arg-type]
        actor_id="owner-001",
        organization_id="org-001",
        design_group_id="group-001",
    )
    return onboarding, repository, jobs


def _candidate(snapshot_id: str = "source-1") -> dict[str, object]:
    return {
        "subject_ref": "family:family-001",
        "predicate": "recommended_clearance",
        "object_value": {"value": 0.2, "unit": "mm"},
        "unit": "mm",
        "scope_kind": "family",
        "risk_level": "R1",
        "source_kind": "engineer_reviewed_onboarding",
        "evidence": [{"snapshot_id": snapshot_id, "observation": "measured interface"}],
        "confidence": 0.9,
        "applicability": {"interface": "guide"},
        "non_applicable_conditions": [],
    }


def test_product_family_onboarding_keeps_inputs_analysis_review_and_publication_in_one_job(
    tmp_path: Path,
) -> None:
    fcstd = tmp_path / "产品 A.FCStd"
    step = tmp_path / "产品 B.step"
    fcstd.write_bytes(_fcstd())
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    onboarding, repository, jobs = _onboarding(tmp_path)
    job_id = str(jobs.row["job_id"])

    started = onboarding.start(
        job_id=job_id,
        expected_job_revision=1,
        family_id="family-001",
        source_paths=[str(fcstd), str(step)],
    )
    snapshot_id = str(started["run"]["input_manifest"]["snapshots"][0]["id"])
    analyzed = onboarding.analyze(
        job_id=job_id,
        expected_job_revision=2,
        family_id="family-001",
        analysis={"common_interfaces": ["guide"], "differences": ["travel"]},
        candidate_knowledge=[_candidate(snapshot_id)],
    )
    package_sha = str(analyzed["package_sha256"])
    with pytest.raises(JobFailure) as invalid_confirmation:
        onboarding.review(
            job_id=job_id,
            expected_job_revision=3,
            family_id="family-001",
            package_sha256=package_sha,
            decision="approve",
            reviewer_text="Evidence checked",
            confirmation="approve",
        )
    assert invalid_confirmation.value.code == "JOB_CONFIRMATION_INVALID"
    reviewed = onboarding.review(
        job_id=job_id,
        expected_job_revision=3,
        family_id="family-001",
        package_sha256=package_sha,
        decision="approve",
        reviewer_text="Evidence checked",
        confirmation=f"批准产品族知识 {package_sha}",
    )
    review_identity = str(reviewed["review"]["review_identity"])
    published = onboarding.publish(
        job_id=job_id,
        expected_job_revision=4,
        family_id="family-001",
        package_sha256=package_sha,
        review_identity=review_identity,
        confirmation=f"发布产品族知识 {review_identity}",
    )
    retried = onboarding.publish(
        job_id=job_id,
        expected_job_revision=5,
        family_id="family-001",
        package_sha256=package_sha,
        review_identity=review_identity,
        confirmation=f"发布产品族知识 {review_identity}",
    )

    assert started["job"]["revision"] == 2
    assert analyzed["job"]["revision"] == 3
    assert reviewed["job"]["revision"] == 4
    assert published == retried
    assert published["job"]["status"] == "completed"
    assert len(repository.outbox) == 1
    assert repository.outbox[0]["onboarding_job_id"] == job_id
    assert len(list((jobs.root / "inputs" / "source").glob("*/source.*"))) == 2
    assert len(list((jobs.root / "analysis").glob("onboarding-*.json"))) == 1
    assert len(list((jobs.root / "knowledge" / "extracted").glob("*.json"))) == 1
    assert len(list((jobs.root / "knowledge").glob("onboarding-review-*.json"))) == 1
    assert len(list((jobs.root / "knowledge").glob("onboarding-publication-*.json"))) == 1


def test_product_family_onboarding_rejects_mechanical_job_and_non_family_knowledge(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.FCStd"
    source.write_bytes(_fcstd())
    wrong_job, _, jobs = _onboarding(tmp_path, job_type="mechanical_design")
    with pytest.raises(JobFailure) as wrong_type:
        wrong_job.start(
            job_id=str(jobs.row["job_id"]),
            expected_job_revision=1,
            family_id="family-001",
            source_paths=[str(source)],
        )
    assert wrong_type.value.code == "JOB_TYPE_OR_STATUS_MISMATCH"

    onboarding, _, jobs = _onboarding(tmp_path / "family")
    started = onboarding.start(
        job_id=str(jobs.row["job_id"]),
        expected_job_revision=1,
        family_id="family-001",
        source_paths=[str(source)],
    )
    snapshot_id = str(started["run"]["input_manifest"]["snapshots"][0]["id"])
    invalid = _candidate(snapshot_id)
    invalid["scope_kind"] = "design_group"
    with pytest.raises(JobFailure) as wrong_scope:
        onboarding.analyze(
            job_id=str(jobs.row["job_id"]),
            expected_job_revision=2,
            family_id="family-001",
            analysis={"summary": "candidate"},
            candidate_knowledge=[invalid],
        )
    assert wrong_scope.value.code == "JOB_ONBOARDING_SCOPE_INVALID"


def test_product_family_onboarding_migration_has_scoped_review_and_atomic_publication_contract() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "mechanical_design_agent"
        / "resources"
        / "migrations"
        / "postgres"
        / "014_design_job_knowledge.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(migration.split())

    assert "CREATE TABLE IF NOT EXISTS product_family_onboarding_runs" in migration
    assert "CREATE TABLE IF NOT EXISTS product_family_onboarding_reviews" in migration
    assert "CREATE TABLE IF NOT EXISTS product_family_onboarding_publications" in migration
    assert "FOREIGN KEY (job_id,organization_id,design_group_id)" in normalized
    assert "FOREIGN KEY (family_id,organization_id,design_group_id)" in normalized
    assert "review_identity char(64) NOT NULL UNIQUE" in normalized
    assert "publication_identity char(64) NOT NULL UNIQUE" in normalized
    assert "BEFORE UPDATE OR DELETE ON product_family_onboarding_reviews" in normalized
    assert "BEFORE UPDATE OR DELETE ON product_family_onboarding_publications" in normalized


@pytest.mark.skipif(
    not os.environ.get("MECH_DESIGN_DATABASE_URL"),
    reason="MECH_DESIGN_DATABASE_URL is not configured; live onboarding transaction skipped",
)
def test_live_product_family_publication_commits_assertions_outbox_and_completed_job_together() -> None:
    repository = PostgresRepository(os.environ["MECH_DESIGN_DATABASE_URL"])
    with postgres_migrations_directory() as migrations:
        repository.apply_migrations(migrations)
    token = os.urandom(8).hex()
    organization_id = f"org-onboard-{token}"
    design_group_id = f"group-onboard-{token}"
    actor_id = f"owner-onboard-{token}"
    family_id = f"family-onboard-{token}"
    job_id = str(UUID(bytes=os.urandom(16), version=4))
    workspace_id = str(UUID(bytes=os.urandom(16), version=4))
    run_id = str(UUID(bytes=os.urandom(16), version=4))
    review_id = str(UUID(bytes=os.urandom(16), version=4))
    publication_id = str(UUID(bytes=os.urandom(16), version=4))
    snapshot_id = str(UUID(bytes=os.urandom(16), version=4))
    assertion_id = str(UUID(bytes=os.urandom(16), version=4))
    digest = "a" * 64
    try:
        with repository.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO organizations(id,name) VALUES (%s,%s)",
                (organization_id, "Onboarding test organization"),
            )
            connection.execute(
                "INSERT INTO design_groups(id,organization_id,name) VALUES (%s,%s,%s)",
                (design_group_id, organization_id, "Onboarding test group"),
            )
            connection.execute(
                "INSERT INTO actors(id,organization_id,display_name,role) VALUES (%s,%s,%s,'family_owner')",
                (actor_id, organization_id, "Onboarding test owner"),
            )
            connection.execute(
                "INSERT INTO product_families(id,organization_id,design_group_id,canonical_name,status,config) "
                "VALUES (%s,%s,%s,%s,'active','{}'::jsonb)",
                (family_id, organization_id, design_group_id, "Onboarding test family"),
            )
        created = repository.create_design_job(
            job_id=job_id,
            workspace_id=workspace_id,
            display_date="2026-08-24",
            job_type="product_family_onboarding",
            title="Onboard test family",
            slug="onboard-test-family",
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            idempotency_token=f"onboard-{token}",
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
        snapshots = [
            {
                "id": snapshot_id,
                "source_filename": "source.FCStd",
                "stored_path": f"inputs/source/{snapshot_id}/source.FCStd",
                "sha256": digest,
                "size_bytes": 12,
            }
        ]
        input_manifest = {"schema_version": "ProductFamilyOnboardingInputs/v1", "snapshots": snapshots}
        started = repository.start_product_family_onboarding(
            run_id=run_id,
            job_id=job_id,
            expected_job_revision=int(ready["revision"]),
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            input_manifest=input_manifest,
            input_manifest_sha256=digest,
            snapshots=snapshots,
            actor_id=actor_id,
        )
        candidate = _candidate(snapshot_id)
        analyzed = repository.analyze_product_family_onboarding(
            run_id=run_id,
            job_id=job_id,
            expected_job_revision=int(started["job"]["revision"]),
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            analysis={"schema_version": "ProductFamilyOnboardingAnalysis/v1"},
            analysis_sha256="b" * 64,
            analysis_path=f"analysis/onboarding-{run_id}.json",
            candidate_knowledge=[candidate],
            package_sha256="c" * 64,
            package_path=f"knowledge/extracted/{'c' * 64}.json",
            actor_id=actor_id,
        )
        reviewed = repository.review_product_family_onboarding(
            review_id=review_id,
            review_identity="d" * 64,
            run_id=run_id,
            job_id=job_id,
            expected_job_revision=int(analyzed["job"]["revision"]),
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            package_sha256="c" * 64,
            decision="approve",
            reviewer_id=actor_id,
            reviewer_text="Evidence checked",
            review_path=f"knowledge/onboarding-review-{'d' * 64}.json",
        )
        published = repository.publish_product_family_onboarding(
            publication_id=publication_id,
            publication_identity="e" * 64,
            publication_receipt_sha256="f" * 64,
            publication_path=f"knowledge/onboarding-publication-{'e' * 64}.json",
            assertion_ids=[assertion_id],
            run_id=run_id,
            job_id=job_id,
            expected_job_revision=int(reviewed["job"]["revision"]),
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            package_sha256="c" * 64,
            review_identity="d" * 64,
            candidates=[candidate],
            actor_id=actor_id,
        )
        with repository.connection() as connection:
            assertion = connection.execute(
                "SELECT status,family_id,evidence FROM knowledge_assertions WHERE id=%s",
                (assertion_id,),
            ).fetchone()
            outbox = connection.execute(
                "SELECT payload FROM outbox_events WHERE aggregate_type='knowledge_assertion' "
                "AND aggregate_id=%s ORDER BY aggregate_version DESC LIMIT 1",
                (assertion_id,),
            ).fetchone()
        assert published["job"]["status"] == "completed"
        assert published["job"]["phase"] == "completed"
        assert assertion["status"] == "approved"
        assert assertion["family_id"] == family_id
        assert assertion["evidence"][-1]["onboarding_job_id"] == job_id
        assert outbox["payload"]["onboarding_job_id"] == job_id
    finally:
        # The isolated release database is disposable. Append-only lifecycle
        # tables intentionally prevent ad-hoc deletion from shared authority.
        pass
