from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mechanical_design_agent import cli
from mechanical_design_agent.jobs import DesignJobRepairResult


def _repair_manifest(job_id: object, revision: int = 4) -> dict[str, object]:
    return {
        "schema_version": "MechanicalDesignJob/v1",
        "job_id": str(job_id),
        "display_id": "JOB-20260823-501",
        "job_type": "mechanical_design",
        "workspace_id": "20000000-0000-4000-8000-000000000001",
        "title": "CLI authorized pump",
        "slug": "cli-authorized-pump",
        "status": "active",
        "phase": "requirements",
        "revision": revision,
        "organization_id": "org-001",
        "design_group_id": "group-001",
        "family_id": None,
        "directory_name": "JOB-20260823-501-cli-authorized-pump",
        "active_working_copy_id": None,
        "source_snapshots": [],
        "created_at": "2026-08-23T08:15:30.000000Z",
        "created_by": "actor-001",
        "updated_at": "2026-08-23T08:15:30.000000Z",
    }


class _ReadyRuntime:
    def require_initialized(self, capability: str) -> None:
        assert capability == "design_job_workspace"

    def require_capability(self, request: object, *, probe: bool) -> None:
        assert probe is True
        assert getattr(request, "capability", request) == "design_job_workspace"

    def job_operational_settings(self) -> object:
        return object()


class _JobCliService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.job_id = "00000000-0000-4000-8000-000000000501"

    def design_job_create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", kwargs))
        return {"schema_version": "MechanicalDesignJob/v1", "job_id": self.job_id, "revision": 1}

    def design_job_list(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list", kwargs))
        return {"schema_version": "MechanicalDesignJobList/v1", "jobs": []}

    def design_job_get(self, *, job_id: str) -> dict[str, object]:
        self.calls.append(("status", job_id))
        return {"schema_version": "MechanicalDesignJob/v1", "job_id": job_id, "revision": 4}

    def design_job_resolve(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("resolve", kwargs))
        return {"schema_version": "MechanicalDesignJobResolution/v1", "candidates": []}

    def design_job_close(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("close", kwargs))
        return {"schema_version": "MechanicalDesignJob/v1", "job_id": kwargs["job_id"], "revision": 5}

    def design_job_reopen(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("reopen", kwargs))
        return {"schema_version": "MechanicalDesignJob/v1", "job_id": kwargs["job_id"], "revision": 6}

    def design_job_doctor(self, job_id: str) -> dict[str, object]:
        self.calls.append(("doctor", job_id))
        return {
            "schema_version": "MechanicalDesignJobDoctor/v1",
            "job_id": job_id,
            "authoritative_revision": 4,
            "receipt_sha256": "a" * 64,
            "status": "ok",
            "issues": [],
        }

    def design_job_repair(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("repair", kwargs))
        return {
            "schema_version": "MechanicalDesignJobRepair/v1",
            "job": _repair_manifest(kwargs["job_id"]),
            "audit": {
                "action": "repair",
                "reason": kwargs["reason"],
                "actor_id": "actor",
                "authoritative_revision": 4,
                "quarantined_attempts": [],
            },
        }

    def design_job_migrate_legacy_dry_run(self) -> dict[str, object]:
        self.calls.append(("migrate-legacy-dry-run", None))
        return {
            "schema_version": "MechanicalDesignLegacyMigrationPlan/v1",
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "items": [],
            "receipt_sha256": "b" * 64,
        }

    def design_job_migrate_legacy_apply(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("migrate-legacy-apply", kwargs))
        return {
            "schema_version": "MechanicalDesignLegacyMigrationResult/v1",
            "plan_receipt_sha256": kwargs["receipt_sha256"],
            "migrated": [],
        }

    def product_family_onboarding_start(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("onboard-start", kwargs))
        return {"schema_version": "ProductFamilyOnboardingStart/v1", "job": {"revision": 2}}

    def product_family_onboarding_analyze(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("onboard-analyze", kwargs))
        return {
            "schema_version": "ProductFamilyOnboardingAnalysisResult/v1",
            "package_sha256": "c" * 64,
            "job": {"revision": 3},
        }

    def product_family_onboarding_review(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("onboard-review", kwargs))
        return {
            "schema_version": "ProductFamilyOnboardingReviewResult/v1",
            "review": {"review_identity": "d" * 64},
            "job": {"revision": 4},
        }

    def product_family_onboarding_publish(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("onboard-publish", kwargs))
        return {
            "schema_version": "ProductFamilyOnboardingPublicationResult/v1",
            "publication": {"publication_identity": "e" * 64},
            "job": {"revision": 5, "status": "completed"},
        }

    def product_family_onboarding_status(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("onboard-status", kwargs))
        return {
            "schema_version": "ProductFamilyOnboardingStatus/v1",
            "onboarding": {"status": "published"},
        }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> dict[str, object]:
    monkeypatch.setattr(sys, "argv", ["mechanical-design", *arguments])
    cli.main()
    return json.loads(capsys.readouterr().out)


def test_job_cli_routes_all_operations_through_the_scoped_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _JobCliService()
    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: service)
    monkeypatch.setenv("MECH_DESIGN_JOB_ID", service.job_id)

    created = _run_cli(
        monkeypatch,
        capsys,
        "job",
        "create",
        "--job-type",
        "mechanical_design",
        "--title",
        "Pump design",
        "--organization-id",
        "org-001",
        "--design-group-id",
        "group-001",
        "--idempotency-token",
        "job-create-001",
    )
    listed = _run_cli(monkeypatch, capsys, "job", "list")
    status = _run_cli(monkeypatch, capsys, "job", "status")
    resolved = _run_cli(monkeypatch, capsys, "job", "resolve", "--query", "pump")
    closed = _run_cli(
        monkeypatch,
        capsys,
        "job",
        "close",
        "--expected-revision",
        "4",
        "--status",
        "completed",
        "--phase",
        "completed",
        "--reason",
        "delivery complete",
        "--confirmation",
        f"关闭 {service.job_id}",
    )
    reopened = _run_cli(
        monkeypatch,
        capsys,
        "job",
        "reopen",
        "--expected-revision",
        "5",
        "--phase",
        "requirements",
        "--reason",
        "follow-up",
        "--confirmation",
        f"重开 {service.job_id}",
    )
    doctor = _run_cli(monkeypatch, capsys, "job", "doctor")
    repaired = _run_cli(
        monkeypatch,
        capsys,
        "job",
        "repair",
        "--expected-revision",
        "4",
        "--doctor-receipt-sha256",
        "a" * 64,
        "--reason",
        "republish manifest",
        "--confirmation",
        f"修复 {service.job_id}",
    )

    assert created["schema_version"] == "MechanicalDesignJob/v1"
    assert listed["schema_version"] == "MechanicalDesignJobList/v1"
    assert status["job_id"] == service.job_id
    assert resolved["candidates"] == []
    assert closed["revision"] == 5
    assert reopened["revision"] == 6
    assert doctor["receipt_sha256"] == "a" * 64
    assert set(repaired) == {"schema_version", "job", "audit"}
    assert repaired["schema_version"] == "MechanicalDesignJobRepair/v1"
    assert repaired["job"]["schema_version"] == "MechanicalDesignJob/v1"
    assert DesignJobRepairResult.from_dict(repaired).as_dict() == repaired
    assert [name for name, _ in service.calls] == [
        "create",
        "list",
        "status",
        "resolve",
        "close",
        "reopen",
        "doctor",
        "repair",
    ]
    assert ("status", service.job_id) in service.calls
    assert not hasattr(service, "active_job")


def test_job_cli_migrates_legacy_inventory_without_using_an_active_job_binding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    service = _JobCliService()
    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: service)
    monkeypatch.delenv("MECH_DESIGN_JOB_ID", raising=False)

    plan = _run_cli(monkeypatch, capsys, "job", "migrate-legacy", "--dry-run")
    plan_file = tmp_path / "legacy-plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    applied = _run_cli(
        monkeypatch,
        capsys,
        "job",
        "migrate-legacy",
        "--apply",
        "--plan-file",
        str(plan_file),
        "--receipt-sha256",
        str(plan["receipt_sha256"]),
        "--confirmation",
        f"迁移旧设计 {plan['receipt_sha256']}",
    )

    assert plan["schema_version"] == "MechanicalDesignLegacyMigrationPlan/v1"
    assert applied["schema_version"] == "MechanicalDesignLegacyMigrationResult/v1"
    assert [name for name, _ in service.calls] == [
        "migrate-legacy-dry-run",
        "migrate-legacy-apply",
    ]
    apply_call = service.calls[1][1]
    assert isinstance(apply_call, dict)
    assert apply_call["plan"] == plan
    assert apply_call["confirmation"] == f"迁移旧设计 {plan['receipt_sha256']}"


def test_family_onboarding_cli_routes_all_evidence_operations_through_one_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    service = _JobCliService()
    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: service)
    analysis_file = tmp_path / "analysis.json"
    candidates_file = tmp_path / "candidates.json"
    analysis_file.write_text('{"summary":"family"}', encoding="utf-8")
    candidates_file.write_text("[]", encoding="utf-8")
    common = ("--job", service.job_id, "--family-id", "family-001")

    started = _run_cli(
        monkeypatch,
        capsys,
        "family",
        "onboard",
        "start",
        *common,
        "--expected-revision",
        "1",
        "--source",
        str(tmp_path / "source.FCStd"),
    )
    analyzed = _run_cli(
        monkeypatch,
        capsys,
        "family",
        "onboard",
        "analyze",
        *common,
        "--expected-revision",
        "2",
        "--analysis-file",
        str(analysis_file),
        "--candidate-file",
        str(candidates_file),
    )
    reviewed = _run_cli(
        monkeypatch,
        capsys,
        "family",
        "onboard",
        "review",
        *common,
        "--expected-revision",
        "3",
        "--package-sha256",
        "c" * 64,
        "--decision",
        "approve",
        "--reviewer-text",
        "checked",
        "--confirmation",
        f"批准产品族知识 {'c' * 64}",
    )
    published = _run_cli(
        monkeypatch,
        capsys,
        "family",
        "onboard",
        "publish",
        *common,
        "--expected-revision",
        "4",
        "--package-sha256",
        "c" * 64,
        "--review-identity",
        "d" * 64,
        "--confirmation",
        f"发布产品族知识 {'d' * 64}",
    )
    status = _run_cli(
        monkeypatch,
        capsys,
        "family",
        "onboard",
        "status",
        "--job",
        service.job_id,
    )

    assert started["job"]["revision"] == 2
    assert analyzed["package_sha256"] == "c" * 64
    assert reviewed["review"]["review_identity"] == "d" * 64
    assert published["job"]["status"] == "completed"
    assert status["onboarding"]["status"] == "published"
    assert [name for name, _ in service.calls] == [
        "onboard-start",
        "onboard-analyze",
        "onboard-review",
        "onboard-publish",
        "onboard-status",
    ]


def test_job_cli_explicit_job_overrides_process_scoped_binding_without_persisting_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _JobCliService()
    explicit = "00000000-0000-4000-8000-000000000599"
    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: service)
    monkeypatch.setenv("MECH_DESIGN_JOB_ID", service.job_id)

    result = _run_cli(monkeypatch, capsys, "job", "status", "--job", explicit)

    assert result["job_id"] == explicit
    assert service.calls == [("status", explicit)]


def test_job_cli_explicit_blank_never_falls_back_to_process_binding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _JobCliService()
    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: service)
    monkeypatch.setenv("MECH_DESIGN_JOB_ID", service.job_id)

    with pytest.raises(SystemExit) as captured:
        _run_cli(monkeypatch, capsys, "job", "status", "--job", "")

    response = json.loads(capsys.readouterr().out)
    assert captured.value.code == 3
    assert response["schema_version"] == "MechanicalDesignJobError/v1"
    assert response["code"] == "JOB_INPUT_INVALID"
    assert service.calls == []


def test_job_cli_redacts_unexpected_repository_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingService(_JobCliService):
        def design_job_list(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("/private/secret/Authorized Pump")

    monkeypatch.setattr(cli.BootstrapRuntime, "from_process", lambda **_kwargs: _ReadyRuntime())
    monkeypatch.setattr(cli, "MechanicalDesignService", lambda _settings: FailingService())
    with pytest.raises(SystemExit) as captured:
        _run_cli(monkeypatch, capsys, "job", "list")
    response = json.loads(capsys.readouterr().out)
    assert captured.value.code == 3
    assert response["code"] == "JOB_REQUEST_FAILED"
    assert response["next_action"]
    assert "secret" not in json.dumps(response)
