from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mechanical_design_agent.artifacts import ArtifactStore
from mechanical_design_agent.design import DesignWorkspace
from mechanical_design_agent.design_lessons import DesignLessonStagingStore
from mechanical_design_agent.hashing import file_sha256
from mechanical_design_agent.jobs import JobFailure
from mechanical_design_agent.server import build_server, create_mcp
from mechanical_design_agent.service import MechanicalDesignService


class FakeProfileRepository:
    def __init__(self, model_count: int):
        self.model_count = model_count
        self.validated_answer_ids: list[str] = []

    def family_model_count(self, family_id: str) -> int:
        return self.model_count

    def validate_family_answer_evidence(self, family_id: str, answer_event_ids: list[str]) -> None:
        if not answer_event_ids:
            raise ValueError("original answer evidence required")
        self.validated_answer_ids = answer_event_ids

    def save_family_profile(self, family_id, profile, evidence, status, actor_id):
        return {"family_id": family_id, "profile": profile, "evidence": evidence, "status": status}


def profile_service(model_count: int) -> MechanicalDesignService:
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = FakeProfileRepository(model_count)
    service.bootstrap_config = {"minimum_distinct_models_for_generalization": 3}
    service.settings = SimpleNamespace(actor_id="owner")
    service._require_database = lambda: None
    return service


class FakeDesignLessonRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.existing_approval: dict | None = None
        self.raw_lesson: dict | None = None

    def existing_design_lesson_approval(self, **kwargs):
        if self.existing_approval is not None:
            self.calls.append(("existing", kwargs))
        return self.existing_approval

    def approve_design_lesson(self, **kwargs):
        self.calls.append(("approve", kwargs))
        return {"id": "00000000-0000-0000-0000-000000000101", "status": "approved"}

    def revoke_design_lesson(self, **kwargs):
        self.calls.append(("revoke", kwargs))
        return {"id": kwargs["lesson_id"], "status": "revoked"}

    def get_design_group(self, design_group_id: str):
        self.calls.append(("get_design_group", {"design_group_id": design_group_id}))
        return {"id": design_group_id, "organization_id": "org-001"}

    def get_design_lesson(self, lesson_id: str, *, organization_id: str):
        self.calls.append(("get", {"lesson_id": lesson_id, "organization_id": organization_id}))
        if self.raw_lesson is None:
            raise KeyError(lesson_id)
        return self.raw_lesson

    def get_design_lesson_audit(self, **kwargs):
        self.calls.append(("audit_get", kwargs))
        return {**(self.raw_lesson or {}), "review_history": [{"decision": "approve-design-lesson"}]}


class FakeDesignWorkspace:
    def __init__(self, current_sha256: str) -> None:
        self.current_sha256 = current_sha256
        self.requested_working_copy_ids: list[str] = []

    def current_hash(self, working_copy_id: str) -> str:
        self.requested_working_copy_ids.append(working_copy_id)
        return self.current_sha256

    def current_snapshot(self, working_copy_id: str, artifact_store: ArtifactStore) -> dict:
        self.requested_working_copy_ids.append(working_copy_id)
        return {
            "sha256": self.current_sha256,
            "size_bytes": 1,
            "source_path": "/workspace/current.working.FCStd",
            "storage_path": f"/artifacts/{self.current_sha256}.FCStd",
            "suffix": ".FCStd",
        }

    @contextmanager
    def locked_current_snapshot(
        self, working_copy_id: str, artifact_store: ArtifactStore
    ):
        yield self.current_snapshot(working_copy_id, artifact_store)


class LockingApprovalRepository(FakeDesignLessonRepository):
    def __init__(self, working_path: Path) -> None:
        super().__init__()
        self.working_path = working_path
        self.approval_entered = Event()
        self.allow_commit = Event()
        self.approved_working_copy_artifact: dict | None = None

    def get_working_copy(self, working_copy_id: str) -> dict:
        return {"id": working_copy_id, "working_path": str(self.working_path)}

    def approve_design_lesson(self, **kwargs):
        self.approved_working_copy_artifact = kwargs["working_copy_artifact"]
        self.approval_entered.set()
        if not self.allow_commit.wait(timeout=5):
            raise TimeoutError("approval test did not release the repository commit")
        return {"id": "00000000-0000-0000-0000-000000000101", "status": "approved"}


def design_lesson_package() -> dict:
    return {
        "schema_version": "DesignLessonPackage/v1",
        "lesson_id": "DL-SERVICE-001",
        "title": "Verify actuator mounting clearance",
        "codex_session_id": "codex-session-001",
        "source": {
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "family_id": "family-001",
            "working_copy_id": "00000000-0000-0000-0000-000000000011",
            "before_model_sha256": "1" * 64,
            "after_model_sha256": "2" * 64,
            "change_set_ids": ["00000000-0000-0000-0000-000000000012"],
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
            }
        ],
        "evidence_manifest": [],
    }


def design_lesson_evidence_item(path: str = "validation.json") -> dict[str, str]:
    package = design_lesson_package()
    return {
        "evidence_id": "validation-evidence",
        "path": path,
        "role": "geometry_validation",
        "media_type": "application/json",
        "working_copy_id": package["source"]["working_copy_id"],
        "change_set_id": package["source"]["change_set_ids"][0],
        "model_sha256": package["source"]["after_model_sha256"],
        "validation_kind": "geometry_model",
    }


def make_service_with_staged_lesson():
    temporary = tempfile.TemporaryDirectory()
    workspace = Path(temporary.name)
    (workspace / "validation.json").write_text('{"status":"passed"}', encoding="utf-8")
    repository = FakeDesignLessonRepository()
    package = design_lesson_package()
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.repository = repository
    service.settings = SimpleNamespace(actor_id="owner-001", workspace=workspace)
    service.bootstrap_config = {
        "organization_id": "org-001",
        "design_group_id": "group-001",
    }
    service.design_lesson_staging = DesignLessonStagingStore(workspace)
    service.artifacts = ArtifactStore(workspace / "artifacts")
    service.design_workspace = FakeDesignWorkspace(package["source"]["after_model_sha256"])
    service._require_database = lambda: None
    service._safe_projection = lambda: {"status": "deferred"}
    staged = service.design_lesson_stage(
        copy.deepcopy(package),
        [design_lesson_evidence_item()],
    )
    return temporary, service, staged


def test_system_status_reports_freecad_gui_mcp_as_unprobed_external_boundary(
    tmp_path: Path,
) -> None:
    freecadcmd = tmp_path / "FreeCADCmd"
    service = MechanicalDesignService.__new__(MechanicalDesignService)
    service.settings = SimpleNamespace(
        freecadcmd=freecadcmd,
        artifact_root=tmp_path / "artifacts",
        family_config_path=tmp_path / "example-family.json",
    )
    service.repository = SimpleNamespace(status=lambda: {"status": "healthy"})
    service.projection = SimpleNamespace(status=lambda: {"status": "healthy"})
    service.bootstrap_config = {"library_root": None}
    service.bootstrap_error = ""

    original_import = builtins.__import__

    def reject_freecad_mcp_import(name, *args, **kwargs):
        if name == "freecad_mcp" or name.startswith("freecad_mcp."):
            raise AssertionError("system status must not import FreeCAD GUI MCP")
        return original_import(name, *args, **kwargs)

    completed = subprocess.CompletedProcess(
        [str(freecadcmd), "--version"],
        returncode=0,
        stdout="FreeCAD 1.1.1\n",
    )
    with (
        patch(
            "mechanical_design_agent.service.subprocess.run",
            return_value=completed,
        ) as run_mock,
        patch(
            "mechanical_design_agent.service.subprocess.Popen",
            side_effect=AssertionError("system status must not start another MCP"),
        ) as popen_mock,
        patch(
            "socket.create_connection",
            side_effect=AssertionError("system status must not probe another MCP"),
        ) as socket_mock,
        patch("builtins.__import__", side_effect=reject_freecad_mcp_import),
    ):
        status = service.system_status()

    assert status["schema_version"] == "MechanicalDesignSystemStatus/v1"
    assert status["interactive_freecad_mcp"] == {
        "status": "external_not_probed",
        "required_for": "recommended_interactive_freecad_workflow",
        "bundled": False,
        "backend_dependency": False,
        "documentation": "docs/FREECAD_GUI_MCP_INTEGRATION.md",
        "validation": "independent_release_e2e",
    }
    run_mock.assert_called_once_with(
        [str(freecadcmd), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=False,
    )
    popen_mock.assert_not_called()
    socket_mock.assert_not_called()


class ServiceKnowledgeRulesTests(unittest.TestCase):
    def test_statistical_profile_is_blocked_before_three_distinct_sources(self) -> None:
        with self.assertRaises(ValueError):
            profile_service(2).family_profile_propose(
                "family", {"observation": "candidate"}, [{"model_revision_id": "m1"}]
            )

    def test_expert_declared_profile_requires_original_answer_event(self) -> None:
        with self.assertRaises(ValueError):
            profile_service(0).family_profile_propose(
                "family", {"rule": "declared"}, [{"note": "missing answer"}], "expert_declared"
            )

    def test_expert_declared_profile_preserves_answer_event(self) -> None:
        service = profile_service(0)
        result = service.family_profile_propose(
            "family",
            {"rule": "declared"},
            [{"answer_event_id": "00000000-0000-0000-0000-000000000001"}],
            "expert_declared",
        )
        self.assertEqual(result["profile"]["source_kind"], "expert_declared")
        self.assertEqual(
            service.repository.validated_answer_ids,
            ["00000000-0000-0000-0000-000000000001"],
        )


class ServiceDeliveryRulesTests(unittest.TestCase):
    def test_delivery_approval_authorizes_before_workspace_or_artifact_work(self) -> None:
        working_copy_id = "00000000-0000-0000-0000-000000000011"
        workspace_calls = []
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service.settings = SimpleNamespace(actor_id="owner-001")
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.repository = SimpleNamespace(
            authorize_delivery_approval=lambda **_kwargs: (_ for _ in ()).throw(
                PermissionError("delivery approval is outside configured scope")
            )
        )
        service.artifacts = object()
        service._require_database = lambda: None
        service.design_workspace = SimpleNamespace(
            approve_delivery=lambda *_args, **_kwargs: workspace_calls.append(True)
        )

        with self.assertRaisesRegex(PermissionError, "configured scope"):
            service.design_delivery_approve(
                working_copy_id, f"批准 {working_copy_id}"
            )

        self.assertEqual(workspace_calls, [])

    def test_delivery_approval_requires_the_design_lesson_review_next_action(self) -> None:
        working_copy_id = "00000000-0000-0000-0000-000000000011"
        approved = {
            "id": working_copy_id,
            "status": "approved_for_delivery",
        }
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service.settings = SimpleNamespace(actor_id="owner-001")
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.repository = SimpleNamespace(
            authorize_delivery_approval=lambda **_kwargs: None
        )
        service.artifacts = object()
        service._require_database = lambda: None
        service.design_workspace = SimpleNamespace(
            approve_delivery=lambda requested_id, actor_id, confirmation, artifacts, **_scope: approved
        )

        result = service.design_delivery_approve(
            working_copy_id, f"批准 {working_copy_id}"
        )

        self.assertEqual(result["id"], working_copy_id)
        self.assertEqual(
            result["design_lesson_review"],
            {
                "required": True,
                "working_copy_id": working_copy_id,
                "next_action": "design_lesson_review_context",
            },
        )

    def test_review_context_rejects_a_copy_before_delivery_approval(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service._require_database = lambda: None
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.repository = SimpleNamespace(
            design_lesson_review_context=lambda _working_copy_id, **_kwargs: (_ for _ in ()).throw(
                KeyError("working copy is unknown or not delivery-approved")
            )
        )

        with self.assertRaisesRegex(KeyError, "delivery-approved"):
            service.design_lesson_review_context("working-draft")

    def test_review_context_retains_failures_and_verifies_the_approved_final_hash(self) -> None:
        working_copy_id = "00000000-0000-0000-0000-000000000011"
        final_sha256 = "a" * 64
        changes = [
            {
                "id": "change-001",
                "working_copy_id": working_copy_id,
                "status": "applied",
                "changes": [{"target": "nozzle.N1"}],
            },
            {
                "id": "change-002",
                "working_copy_id": working_copy_id,
                "status": "applied",
                "changes": [{"target": "nozzle.N1"}],
            },
        ]
        validations = [
            {
                "id": "validation-failed",
                "working_copy_id": working_copy_id,
                "validation_kind": "geometry_model",
                "status": "failed",
                "working_sha256": "9" * 64,
                "checks": [{"check_id": "shape.valid", "status": "failed"}],
            },
            {
                "id": "validation-geometry-final",
                "working_copy_id": working_copy_id,
                "validation_kind": "geometry_model",
                "status": "passed",
                "working_sha256": final_sha256,
                "checks": [{"check_id": "shape.valid", "status": "passed"}],
            },
            {
                "id": "validation-assembly-final",
                "working_copy_id": working_copy_id,
                "validation_kind": "assembly_completeness",
                "status": "passed",
                "working_sha256": final_sha256,
                "checks": [{"check_id": "assembly.complete", "status": "passed"}],
            },
        ]
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service._require_database = lambda: None
        service.repository = SimpleNamespace(
            design_lesson_review_context=lambda requested_id, **_kwargs: {
                "working_copy": {
                    "id": requested_id,
                    "organization_id": "org-001",
                    "design_group_id": "group-001",
                    "status": "approved_for_delivery",
                    "approved_final_sha256": final_sha256,
                    "approved_final_artifact_path": "/artifacts/approved-final.FCStd",
                    "working_path": "/workspace/final.FCStd",
                },
                "change_sets": changes,
                "validation_reports": validations,
                "standard_part_provenance": [
                    {
                        "provider_id": "fasteners",
                        "part_number": "A-001",
                        "metadata": {
                            "working_copy_id": requested_id,
                            "model_sha256": final_sha256,
                        },
                    }
                ],
            }
        )
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.design_workspace = FakeDesignWorkspace(final_sha256)

        result = service.design_lesson_review_context(working_copy_id)

        self.assertEqual(result["schema_version"], "DesignLessonReviewContext/v1")
        self.assertEqual(result["working_copy_id"], working_copy_id)
        self.assertEqual(result["final_model_sha256"], final_sha256)
        self.assertEqual(result["working_path"], "/workspace/final.FCStd")
        self.assertEqual(result["applied_change_sets"], changes)
        self.assertEqual(result["validation_history"], validations)
        self.assertEqual(
            result["standard_part_provenance"][0]["part_number"], "A-001"
        )
        self.assertEqual(result["validation_history"][0]["status"], "failed")
        self.assertEqual(
            result["material_iteration_candidates"][0]["target"], "nozzle.N1"
        )
        self.assertEqual(result["next_action"], "prepare_design_lesson_review")
        self.assertEqual(service.design_workspace.requested_working_copy_ids, [working_copy_id])

    def test_review_context_rejects_final_model_drift_after_delivery_approval(self) -> None:
        approved_sha256 = "a" * 64
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service._require_database = lambda: None
        service.repository = SimpleNamespace(
            design_lesson_review_context=lambda working_copy_id, **_kwargs: {
                "working_copy": {
                    "id": working_copy_id,
                    "organization_id": "org-001",
                    "design_group_id": "group-001",
                    "status": "approved_for_delivery",
                    "approved_final_sha256": approved_sha256,
                    "working_path": "/workspace/final.FCStd",
                },
                "change_sets": [],
                "validation_reports": [
                    {
                        "id": "validation-geometry-final",
                        "validation_kind": "geometry_model",
                        "status": "passed",
                        "working_sha256": approved_sha256,
                        "checks": [],
                    },
                    {
                        "id": "validation-assembly-final",
                        "validation_kind": "assembly_completeness",
                        "status": "passed",
                        "working_sha256": approved_sha256,
                        "checks": [],
                    },
                ],
            }
        )
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.design_workspace = FakeDesignWorkspace("b" * 64)

        with self.assertRaisesRegex(ValueError, "changed after delivery approval"):
            service.design_lesson_review_context("working-approved")

    def test_review_context_rejects_foreign_tenant_before_returning_history(self) -> None:
        final_sha256 = "a" * 64
        for foreign_field, foreign_value in (
            ("organization_id", "org-other"),
            ("design_group_id", "group-other"),
        ):
            with self.subTest(foreign_field=foreign_field):
                working_copy = {
                    "id": "working-foreign",
                    "organization_id": "org-001",
                    "design_group_id": "group-001",
                    "status": "approved_for_delivery",
                    "approved_final_sha256": final_sha256,
                    "working_path": "/workspace/foreign.FCStd",
                }
                working_copy[foreign_field] = foreign_value
                repository = SimpleNamespace(
                    design_lesson_review_context=lambda *_args, **_kwargs: {
                        "working_copy": working_copy,
                        "change_sets": [{"secret": "foreign-change-history"}],
                        "validation_reports": [{"secret": "foreign-validation-history"}],
                        "standard_part_provenance": [],
                    }
                )
                service = MechanicalDesignService.__new__(MechanicalDesignService)
                service._require_database = lambda: None
                service.repository = repository
                service.bootstrap_config = {
                    "organization_id": "org-001",
                    "design_group_id": "group-001",
                }
                service.design_workspace = FakeDesignWorkspace(final_sha256)

                with self.assertRaises((KeyError, PermissionError, ValueError)):
                    service.design_lesson_review_context("working-foreign")

    def test_revalidation_does_not_rebind_a_stale_delivery_approval(self) -> None:
        approved_sha256 = "a" * 64
        revalidated_sha256 = "b" * 64
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service._require_database = lambda: None
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service.repository = SimpleNamespace(
            design_lesson_review_context=lambda *_args, **_kwargs: {
                "working_copy": {
                    "id": "working-approved",
                    "organization_id": "org-001",
                    "design_group_id": "group-001",
                    "status": "approved_for_delivery",
                    "approved_final_sha256": approved_sha256,
                    "working_path": "/workspace/final.FCStd",
                },
                "change_sets": [],
                "validation_reports": [
                    {
                        "validation_kind": "geometry_model",
                        "status": "passed",
                        "working_sha256": revalidated_sha256,
                        "checks": [],
                    },
                    {
                        "validation_kind": "assembly_completeness",
                        "status": "passed",
                        "working_sha256": revalidated_sha256,
                        "checks": [],
                    },
                ],
                "standard_part_provenance": [],
            }
        )
        service.design_workspace = FakeDesignWorkspace(revalidated_sha256)

        with self.assertRaisesRegex(ValueError, "changed after delivery approval"):
            service.design_lesson_review_context("working-approved")


class ServiceDesignLessonBoundaryTests(unittest.TestCase):
    def test_design_lesson_get_returns_safe_redacted_shape(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = FakeDesignLessonRepository()
        private_term = "PRIVATE-SOURCE-INCIDENT"
        repository.raw_lesson = {
            "id": "00000000-0000-0000-0000-000000000101",
            "lesson_key": "DL-PRIVATE-001",
            "revision": 1,
            "status": "approved",
            "source_family_id": "private-family",
            "title": private_term,
            "problem": {"summary": private_term},
            "root_causes": [private_term],
            "corrections": [private_term],
            "prevention": {"required_checks": [private_term]},
            "applicability": {"component_classes": [private_term]},
            "non_applicable_conditions": [private_term],
            "assertions": [{
                "id": "00000000-0000-0000-0000-000000000102",
                "subject_ref": "generic:shaft",
                "predicate": "requires-clearance",
                "object_value": {"minimum_mm": 2},
                "unit": "mm",
                "scope_kind": "organization_general",
                "family_id": None,
                "risk_level": "R3",
                "source_kind": "approved_design_lesson",
                "evidence": [{"path": private_term}],
                "applicability": {"constraint_kind": "hard_constraint", "private": private_term},
                "non_applicable_conditions": [private_term],
                "contradicts": [],
            }],
        }
        service.repository = repository
        service.bootstrap_config = {"organization_id": "org-001"}
        service._require_database = lambda: None

        result = service.design_lesson_get("DL-PRIVATE-001")

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["schema_version"], "DesignLessonGet/v1")
        self.assertTrue(result["lesson"]["source_details_redacted"])
        self.assertTrue(result["lesson"]["design_lesson_ref"])
        self.assertEqual(result["lesson"]["assertions"][0]["evidence"], [])
        self.assertNotIn(private_term, serialized)
        self.assertNotIn("private-family", serialized)
        self.assertNotIn("DL-PRIVATE-001", serialized)

    def test_design_lesson_audit_get_requires_exact_confirmation(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = FakeDesignLessonRepository()
        repository.raw_lesson = {"id": "lesson-1", "status": "approved"}
        service.repository = repository
        service.settings = SimpleNamespace(actor_id="owner-001")
        service.bootstrap_config = {"organization_id": "org-001"}
        service._require_database = lambda: None

        with self.assertRaisesRegex(ValueError, "canonical confirmation"):
            service.design_lesson_audit_get("lesson-1", "不要审计 lesson-1")
        result = service.design_lesson_audit_get("lesson-1", "审计 lesson-1")

        self.assertEqual(result["review_history"][0]["decision"], "approve-design-lesson")
        self.assertEqual([name for name, _ in repository.calls], ["audit_get"])

    def test_design_context_rejects_cross_tenant_identifiers_before_lookup(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = FakeDesignLessonRepository()
        service.repository = repository
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service._require_database = lambda: None
        service.context_builder = SimpleNamespace(build=lambda **kwargs: kwargs)

        with self.assertRaisesRegex(PermissionError, "configured organization"):
            service.design_context_build(
                organization_id="other-org",
                design_group_id="other-group",
                requested_family_id="other-family",
                explicit_family_authorization=True,
            )

        self.assertEqual(repository.calls, [])

    def test_design_context_rejects_group_outside_configured_organization(self) -> None:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        repository = FakeDesignLessonRepository()
        repository.get_design_group = lambda design_group_id: {
            "id": design_group_id,
            "organization_id": "other-org",
        }
        service.repository = repository
        service.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        service._require_database = lambda: None
        service.context_builder = SimpleNamespace(build=lambda **kwargs: kwargs)

        with self.assertRaisesRegex(PermissionError, "design group does not belong"):
            service.design_context_build(
                organization_id="org-001",
                design_group_id="other-group",
                requested_family_id="other-family",
                explicit_family_authorization=True,
            )
    def test_staging_makes_no_repository_call(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)

        self.assertEqual(staged["status"], "staged-local-only")
        self.assertEqual(service.repository.calls, [])

    def test_approve_requires_exact_digest_in_confirmation(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)

        with self.assertRaisesRegex(ValueError, "lesson_id, SHA-256, and 批准"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_approve_rejects_negated_prefix_or_noncanonical_confirmation(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        digest = staged["package_sha256"]
        lesson_id = staged["lesson_id"]

        for confirmation in (
            f"不批准 {lesson_id} {digest}",
            f"批准 {lesson_id} {digest[:32]}",
            f"请批准 {lesson_id} {digest}",
            f"批准 {lesson_id}-EXTRA {digest}",
        ):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(ValueError, "canonical confirmation"):
                    service.design_lesson_approve(
                        lesson_id=lesson_id,
                        expected_package_sha256=digest,
                        reviewer_text="Reviewed",
                        confirmation=confirmation,
                    )

        self.assertEqual(service.repository.calls, [])

    def test_approve_archives_verified_package_and_checks_current_fcstd_hash(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)

        result = service.design_lesson_approve(
            lesson_id=staged["lesson_id"],
            expected_package_sha256=staged["package_sha256"],
            reviewer_text="Reviewed",
            confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
        )

        self.assertEqual(result["lesson"]["status"], "approved")
        self.assertEqual(service.design_workspace.requested_working_copy_ids, [design_lesson_package()["source"]["working_copy_id"]])
        call = next(item for item in service.repository.calls if item[0] == "approve")
        self.assertEqual(call[0], "approve")
        self.assertEqual(call[1]["package_sha256"], staged["package_sha256"])
        self.assertEqual(Path(call[1]["archived_package_path"]).read_bytes(), Path(staged["lesson_json_path"]).read_bytes())
        self.assertEqual(
            [item["evidence_id"] for item in call[1]["archived_evidence"]],
            ["validation-evidence"],
        )
        self.assertEqual(
            call[1]["archived_evidence"][0]["artifact_sha256"],
            json.loads(Path(staged["lesson_json_path"]).read_text(encoding="utf-8"))["evidence_manifest"][0]["sha256"],
        )
        self.assertEqual(
            call[1]["working_copy_artifact"]["sha256"],
            design_lesson_package()["source"]["after_model_sha256"],
        )

    def test_approval_holds_the_shared_working_copy_lock_until_repository_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            working = workspace / "current.working.FCStd"
            original_bytes = b"reviewed-working-copy"
            working.write_bytes(original_bytes)
            evidence = workspace / "validation.json"
            evidence.write_text('{"status":"passed"}\n', encoding="utf-8")
            package = design_lesson_package()
            package["source"]["after_model_sha256"] = file_sha256(working)
            evidence_item = design_lesson_evidence_item()
            evidence_item["model_sha256"] = package["source"]["after_model_sha256"]
            repository = LockingApprovalRepository(working)
            settings = SimpleNamespace(
                actor_id="owner-001",
                workspace=workspace,
                package_root=workspace,
            )
            service = MechanicalDesignService.__new__(MechanicalDesignService)
            service.repository = repository
            service.settings = settings
            service.bootstrap_config = {
                "organization_id": "org-001",
                "design_group_id": "group-001",
            }
            service.design_lesson_staging = DesignLessonStagingStore(workspace)
            service.artifacts = ArtifactStore(workspace / "artifacts")
            service.design_workspace = DesignWorkspace(settings, repository)
            service._require_database = lambda: None
            service._safe_projection = lambda: {"status": "deferred"}
            staged = service.design_lesson_stage(package, [evidence_item])
            writer_started = Event()
            writer_finished = Event()

            def approve() -> dict:
                return service.design_lesson_approve(
                    lesson_id=staged["lesson_id"],
                    expected_package_sha256=staged["package_sha256"],
                    reviewer_text="Reviewed",
                    confirmation=(
                        f"批准 {staged['lesson_id']} {staged['package_sha256']}"
                    ),
                )

            def write_through_agent_lock() -> None:
                writer_started.set()
                with service.design_workspace.locked_working_copy_path(
                    package["source"]["working_copy_id"]
                ) as locked_path:
                    locked_path.write_bytes(b"concurrent-agent-edit")
                    writer_finished.set()

            with ThreadPoolExecutor(max_workers=2) as executor:
                approval_future = executor.submit(approve)
                self.assertTrue(repository.approval_entered.wait(timeout=3))
                writer_future = executor.submit(write_through_agent_lock)
                self.assertTrue(writer_started.wait(timeout=3))
                writer_was_blocked = not writer_finished.wait(timeout=0.25)
                repository.allow_commit.set()
                approval_future.result(timeout=3)
                writer_future.result(timeout=3)

            self.assertTrue(
                writer_was_blocked,
                "a working-copy edit completed after approval hashing but before commit",
            )
            self.assertEqual(
                repository.approved_working_copy_artifact["sha256"],
                hashlib.sha256(original_bytes).hexdigest(),
            )

    def test_exact_digest_idempotency_precedes_mutable_staging_rechecks(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        existing = {"id": "existing-lesson", "status": "approved"}
        service.repository.existing_approval = existing
        Path(staged["lesson_json_path"]).write_text("mutated", encoding="utf-8")
        (Path(staged["lesson_json_path"]).parents[3] / "validation.json").write_text(
            "mutated", encoding="utf-8"
        )

        result = service.design_lesson_approve(
            lesson_id=staged["lesson_id"],
            expected_package_sha256=staged["package_sha256"],
            reviewer_text="Reviewed",
            confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
        )

        self.assertEqual(result["lesson"], existing)
        self.assertEqual([name for name, _ in service.repository.calls], ["existing"])
        self.assertEqual(service.design_workspace.requested_working_copy_ids, [])

    def test_approve_rejects_evidence_replaced_between_verify_and_archive(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        original_verify = service.design_lesson_staging.verify
        evidence_path = Path(temporary.name) / "validation.json"

        def verify_then_replace(lesson_id: str, expected_package_sha256: str):
            result = original_verify(lesson_id, expected_package_sha256)
            evidence_path.write_text('{"tampered":true}', encoding="utf-8")
            return result

        service.design_lesson_staging.verify = verify_then_replace

        with self.assertRaisesRegex(ValueError, "evidence artifact SHA-256"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertFalse(any(name == "approve" for name, _ in service.repository.calls))

    def test_approve_rejects_evidence_path_swapped_to_external_symlink_before_archive(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        evidence_path = workspace / "validation.json"
        external = workspace.parent / f"external-{Path(temporary.name).name}.json"
        external.write_bytes(evidence_path.read_bytes())
        self.addCleanup(external.unlink, missing_ok=True)
        original_evidence_paths = service.design_lesson_staging.evidence_paths

        def evidence_paths_then_swap(lesson_id: str):
            result = original_evidence_paths(lesson_id)
            evidence_path.unlink()
            evidence_path.symlink_to(external)
            return result

        service.design_lesson_staging.evidence_paths = evidence_paths_then_swap

        with self.assertRaisesRegex(ValueError, "workspace"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertFalse(any(name == "approve" for name, _ in service.repository.calls))

    def test_approve_rejects_corrupt_existing_evidence_cas_object(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        descriptor = json.loads(
            Path(staged["lesson_json_path"]).read_text(encoding="utf-8")
        )["evidence_manifest"][0]
        corrupt_target = service.artifacts.path_for(descriptor["sha256"], ".json")
        corrupt_target.parent.mkdir(parents=True, exist_ok=True)
        corrupt_target.write_text('{"corrupt":true}', encoding="utf-8")
        os.chmod(corrupt_target, 0o444)

        with self.assertRaisesRegex(IOError, "content-addressed artifact checksum mismatch"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertFalse(any(name == "approve" for name, _ in service.repository.calls))

    def test_approve_rejects_changed_fcstd_before_repository_approval(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        service.design_workspace.current_sha256 = "9" * 64

        with self.assertRaisesRegex(ValueError, "current FCStd hash"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_approve_rejects_package_replaced_after_staging_verification(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        original_verify = service.design_lesson_staging.verify

        def verify_then_replace(lesson_id: str, expected_package_sha256: str):
            result = original_verify(lesson_id, expected_package_sha256)
            Path(staged["lesson_json_path"]).write_text('{"replaced":true}', encoding="utf-8")
            return result

        service.design_lesson_staging.verify = verify_then_replace

        with self.assertRaisesRegex(ValueError, "archived lesson package SHA-256"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertEqual(service.repository.calls, [])
        self.assertEqual(service.design_workspace.requested_working_copy_ids, [])

    def test_approve_hashes_archived_canonical_bytes_not_artifact_metadata(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        corrupt_target = service.artifacts.path_for(staged["package_sha256"], ".json")
        corrupt_target.parent.mkdir(parents=True, exist_ok=True)
        corrupt_target.write_text('{"replaced":true}', encoding="utf-8")
        os.chmod(corrupt_target, 0o444)

        with self.assertRaisesRegex(ValueError, "archived lesson package SHA-256"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_revoke_requires_lesson_id_and_chinese_confirmation(self) -> None:
        temporary, service, _ = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        lesson_id = "00000000-0000-0000-0000-000000000101"

        with self.assertRaisesRegex(ValueError, "lesson_id and 撤销"):
            service.design_lesson_revoke(lesson_id=lesson_id, reason="obsolete", confirmation=lesson_id)

        self.assertEqual(service.repository.calls, [])

    def test_revoke_rejects_negated_or_identifier_prefix_confirmation(self) -> None:
        temporary, service, _ = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        lesson_id = "DL-10"

        for confirmation in (f"不要撤销 {lesson_id}", "撤销 DL-1", f"请撤销 {lesson_id}"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(ValueError, "canonical confirmation"):
                    service.design_lesson_revoke(
                        lesson_id=lesson_id,
                        reason="obsolete",
                        confirmation=confirmation,
                    )

        self.assertEqual(service.repository.calls, [])

    def test_approve_rejects_blank_reviewer_reason_before_archiving(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)

        with self.assertRaisesRegex(ValueError, "reviewer_text is required"):
            service.design_lesson_approve(
                lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="   ",
                confirmation=f"批准 {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_revoke_rejects_blank_reviewer_reason_before_repository_access(self) -> None:
        temporary, service, _ = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        lesson_id = "00000000-0000-0000-0000-000000000101"

        with self.assertRaisesRegex(ValueError, "reason is required"):
            service.design_lesson_revoke(
                lesson_id=lesson_id,
                reason=" ",
                confirmation=f"撤销 {lesson_id}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_supersede_requires_both_ids_digest_and_replacement_confirmation(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        predecessor = "00000000-0000-0000-0000-000000000101"

        with self.assertRaisesRegex(ValueError, "both lesson ids, SHA-256, and 替代"):
            service.design_lesson_supersede(
                lesson_id=predecessor,
                replacement_lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed replacement",
                confirmation=f"批准 {predecessor} {staged['lesson_id']} {staged['package_sha256']}",
            )

        self.assertEqual(service.repository.calls, [])

    def test_supersede_rejects_negated_confirmation(self) -> None:
        temporary, service, staged = make_service_with_staged_lesson()
        self.addCleanup(temporary.cleanup)
        predecessor = "DL-10"
        confirmation = (
            f"不替代 {predecessor} -> {staged['lesson_id']} "
            f"{staged['package_sha256']}"
        )

        with self.assertRaisesRegex(ValueError, "canonical confirmation"):
            service.design_lesson_supersede(
                lesson_id=predecessor,
                replacement_lesson_id=staged["lesson_id"],
                expected_package_sha256=staged["package_sha256"],
                reviewer_text="Reviewed replacement",
                confirmation=confirmation,
            )

        self.assertEqual(service.repository.calls, [])


class FakeLessonMcpService:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.bootstrap_config = {
            "organization_id": "org-001",
            "design_group_id": "group-001",
        }
        self.search_results: list[dict] = []
        self.search_next_cursor: str | None = None

    def design_lesson_stage(self, package: dict, evidence_items: list[dict]) -> dict:
        self.calls.append(("stage", {"package": package, "evidence_items": evidence_items}))
        return {"status": "staged-local-only"}

    def design_lesson_staged_get(self, lesson_id: str) -> dict:
        self.calls.append(("staged_get", {"lesson_id": lesson_id}))
        return {"lesson_id": lesson_id}

    def design_lesson_approve(self, **kwargs) -> dict:
        self.calls.append(("approve", kwargs))
        return {"status": "approved"}

    def design_lesson_search(self, **kwargs) -> list[dict]:
        self.calls.append(("search", kwargs))
        return self.search_results

    def design_lesson_search_page(self, **kwargs) -> dict:
        self.calls.append(("search_page", kwargs))
        return {
            "items": self.search_results,
            "next_cursor": self.search_next_cursor,
        }

    def design_lesson_get(self, lesson_id: str) -> dict:
        self.calls.append(("get", {"lesson_id": lesson_id}))
        return {"lesson_id": lesson_id}

    def design_lesson_supersede(self, **kwargs) -> dict:
        self.calls.append(("supersede", kwargs))
        return {"status": "approved"}

    def design_lesson_revoke(self, **kwargs) -> dict:
        self.calls.append(("revoke", kwargs))
        return {"status": "revoked"}

    def design_lesson_review_context(self, working_copy_id: str) -> dict:
        self.calls.append(("review_context", {"working_copy_id": working_copy_id}))
        return {"working_copy_id": working_copy_id}

    def design_lesson_review_prepare(
        self,
        working_copy_id: str,
        package: dict,
        evidence_items: list[dict],
        supersedes_review_id: str | None = None,
    ) -> dict:
        self.calls.append(
            (
                "review_prepare",
                {
                    "working_copy_id": working_copy_id,
                    "package": package,
                    "evidence_items": evidence_items,
                    "supersedes_review_id": supersedes_review_id,
                },
            )
        )
        return {"status": "awaiting-engineer-review"}

    def design_lesson_review_approve(self, **kwargs) -> dict:
        self.calls.append(("review_approve", kwargs))
        return {"status": "stored-and-retrievable"}

    def design_lesson_review_reject(self, **kwargs) -> dict:
        self.calls.append(("review_reject", kwargs))
        return {"status": "rejected"}

    def design_lesson_review_status(self, review_id: str, retry: bool = True) -> dict:
        self.calls.append(("review_status", {"review_id": review_id, "retry": retry}))
        return {"status": "approved-retrieval-pending"}

    def design_context_build(self, **kwargs) -> dict:
        self.calls.append(("context", kwargs))
        return {"approved_design_lessons": []}


class McpDesignLessonBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeLessonMcpService()
        self.mcp = create_mcp(service=self.service)

    def tool(self, name: str):
        return self.mcp._tool_manager._tools[name].fn

    def test_build_server_is_the_import_compatible_mcp_factory(self) -> None:
        server = build_server()
        self.assertEqual(server.name, "FreeCAD Mechanical Design Knowledge")

    def test_lesson_review_tools_register_the_one_step_workflow(self) -> None:
        for name in (
            "design_lesson_review_context",
            "design_lesson_review_prepare",
            "design_lesson_review_approve",
            "design_lesson_review_reject",
            "design_lesson_review_status",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.mcp._tool_manager._tools)

        approval_parameters = inspect.signature(
            self.tool("design_lesson_review_approve")
        ).parameters
        self.assertEqual(
            list(approval_parameters),
            ["review_id", "reviewer_text", "confirmation"],
        )

    def test_lesson_review_prepare_parses_payloads_and_optional_predecessor(self) -> None:
        self.tool("design_lesson_review_prepare")(
            "working-copy-001",
            '{"lesson_id":"lesson-001"}',
            '[{"path":"validation.json","role":"geometry_validation"}]',
            "DLR-predecessor-001",
        )

        self.assertEqual(
            self.service.calls,
            [
                (
                    "review_prepare",
                    {
                        "working_copy_id": "working-copy-001",
                        "package": {"lesson_id": "lesson-001"},
                        "evidence_items": [
                            {"path": "validation.json", "role": "geometry_validation"}
                        ],
                        "supersedes_review_id": "DLR-predecessor-001",
                    },
                )
            ],
        )

    def test_lesson_review_approval_requires_exact_single_confirmation_before_service_call(self) -> None:
        tool = self.tool("design_lesson_review_approve")
        with self.assertRaisesRegex(ValueError, "canonical confirmation"):
            tool("DLR-001", "Approve the complete lesson", "批准设计经验 DLR-001 extra")
        self.assertEqual(self.service.calls, [])

        tool("DLR-001", "Approve the complete lesson", "批准设计经验 DLR-001")
        self.assertEqual(
            self.service.calls,
            [
                (
                    "review_approve",
                    {
                        "review_id": "DLR-001",
                        "reviewer_text": "Approve the complete lesson",
                        "confirmation": "批准设计经验 DLR-001",
                    },
                )
            ],
        )

    def test_lesson_review_status_accepts_only_boolean_single_retry_flag(self) -> None:
        tool = self.tool("design_lesson_review_status")
        with self.assertRaisesRegex(ValueError, "retry must be a boolean"):
            tool("DLR-001", 1)
        self.assertEqual(self.service.calls, [])

        tool("DLR-001", False)
        self.assertEqual(
            self.service.calls,
            [("review_status", {"review_id": "DLR-001", "retry": False})],
        )

    def test_lesson_review_tools_reject_unsafe_identifiers_before_service_calls(self) -> None:
        cases = (
            ("design_lesson_review_context", ("../working-copy",)),
            ("design_lesson_review_prepare", ("../working-copy", "{}", "[]", "")),
            ("design_lesson_review_prepare", ("working-copy", "{}", "[]", "../DLR-001")),
            ("design_lesson_review_approve", ("../DLR-001", "Approved", "批准设计经验 ../DLR-001")),
            ("design_lesson_review_reject", ("../DLR-001", "Rejected", "拒绝设计经验 ../DLR-001")),
            ("design_lesson_review_status", ("../DLR-001", True)),
        )
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaisesRegex(ValueError, "unsafe characters"):
                    self.tool(name)(*arguments)
                self.assertEqual(self.service.calls, [])

    def test_lesson_review_tools_reject_malformed_json_and_blank_reviewer_text_before_service_calls(self) -> None:
        prepare = self.tool("design_lesson_review_prepare")
        with self.assertRaisesRegex(ValueError, "strict JSON parse failed"):
            prepare("working-copy-001", "{", "[]")
        with self.assertRaisesRegex(ValueError, "evidence_items_json must be a JSON array"):
            prepare("working-copy-001", "{}", "{}")
        with self.assertRaisesRegex(ValueError, "reviewer_text is required"):
            self.tool("design_lesson_review_approve")(
                "DLR-001", "  ", "批准设计经验 DLR-001"
            )
        with self.assertRaisesRegex(ValueError, "reviewer_text is required"):
            self.tool("design_lesson_review_reject")(
                "DLR-001", "  ", "拒绝设计经验 DLR-001"
            )
        self.assertEqual(self.service.calls, [])

    def test_lesson_review_prepare_requires_nonblank_strict_json_containers_before_service_call(self) -> None:
        prepare = self.tool("design_lesson_review_prepare")
        invalid_packages = ("", "   ", "null", "[]", "{")
        invalid_evidence_items = ("", "   ", "null", "{}", "[")

        for package_json in invalid_packages:
            with self.subTest(field="package_json", value=package_json):
                self.service.calls.clear()
                with self.assertRaises(ValueError):
                    prepare("working-copy-001", package_json, "[]")
                self.assertEqual(self.service.calls, [])
        for evidence_items_json in invalid_evidence_items:
            with self.subTest(field="evidence_items_json", value=evidence_items_json):
                self.service.calls.clear()
                with self.assertRaises(ValueError):
                    prepare("working-copy-001", "{}", evidence_items_json)
                self.assertEqual(self.service.calls, [])

    def test_lesson_review_keeps_legacy_hash_bound_lesson_tools_registered(self) -> None:
        for name in (
            "design_lesson_stage",
            "design_lesson_staged_get",
            "design_lesson_approve",
            "design_lesson_supersede",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.mcp._tool_manager._tools)

    def test_stage_rejects_non_object_package_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "package_json must be a JSON object"):
            self.tool("design_lesson_stage")("[]", "[]")
        self.assertEqual(self.service.calls, [])

    def test_json_boundaries_reject_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            self.tool("design_lesson_stage")('{"object_value": NaN}', "[]")
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            self.tool("design_lesson_stage")("{}", '[{"value": Infinity}]')
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            self.tool("design_lesson_stage")('{"object_value": 1e999}', "[]")
        self.assertEqual(self.service.calls, [])

    def test_json_serialization_rejects_non_finite_numbers(self) -> None:
        candidate = self._lesson_candidate(
            "lesson-non-finite",
            private_term="private",
            component_classes=["actuator"],
            interface_types=["mount"],
        )
        candidate["assertions"][0]["object_value"] = float("nan")
        self.service.search_results = [candidate]
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.tool("design_lesson_search")(
                "",
                "org-001",
                '{"component_classes":["actuator"],"interface_types":["mount"]}',
                1,
            )

    def test_design_context_rejects_cross_tenant_organization_before_service_call(self) -> None:
        with self.assertRaisesRegex(PermissionError, "configured organization"):
            self.tool("design_context_build")(
                "other-org",
                "other-group",
                "other-family",
                "",
                True,
            )
        self.assertEqual(self.service.calls, [])

    def test_stage_rejects_non_object_evidence_element_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence_paths_json must contain evidence objects"):
            self.tool("design_lesson_stage")("{}", '["validation.json"]')
        self.assertEqual(self.service.calls, [])

    def test_stage_rejects_caller_approved_status_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "status is assigned by approval"):
            self.tool("design_lesson_stage")(json.dumps({"status": "approved"}), "[]")
        self.assertEqual(self.service.calls, [])

    def test_search_rejects_out_of_range_limit_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be between 1 and 50"):
            self.tool("design_lesson_search")("clearance", "org-001", "{}", 51)
        self.assertEqual(self.service.calls, [])

    def test_search_rejects_non_string_feature_element_before_service_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "component_classes must contain only strings"):
            self.tool("design_lesson_search")(
                "clearance", "org-001", '{"component_classes":["actuator", 2]}', 10
            )
        self.assertEqual(self.service.calls, [])

    def test_search_normalizes_true_boolean_feature_for_lesson_matching(self) -> None:
        candidate = self._lesson_candidate(
            "lesson-lifting-interface",
            private_term="PRIVATE-SOURCE-INCIDENT",
            component_classes=["actuator"],
            interface_types=["mount"],
        )
        candidate["applicability"]["required_conditions"] = ["has_lifting_interface"]
        self.service.search_results = [candidate]

        response = json.loads(
            self.tool("design_lesson_search")(
                "",
                "org-001",
                '{"component_classes":["actuator"],"interface_types":["mount"],"has_lifting_interface":true}',
                1,
            )
        )

        self.assertEqual(len(response["matches"]), 1)
        self.assertEqual(response["matches"][0]["match"]["unmet_conditions"], [])

    def test_search_returns_safe_match_explanations_after_filtering_all_candidates(self) -> None:
        private_term = "PRIVATE-SOURCE-INCIDENT"
        self.service.search_results = [
            self._lesson_candidate(
                "lesson-unmatched",
                private_term=private_term,
                component_classes=["unrelated-component"],
                interface_types=["unrelated-interface"],
            ),
            self._lesson_candidate(
                "lesson-matched",
                private_term=private_term,
                component_classes=["actuator"],
                interface_types=["mount"],
            ),
        ]

        response = json.loads(
            self.tool("design_lesson_search")(
                "",
                "org-001",
                '{"component_classes":["actuator"],"interface_types":["mount"]}',
                1,
            )
        )

        self.assertEqual(
            self.service.calls,
            [("search_page", {"query": "", "limit": 1, "cursor": None})],
        )
        self.assertEqual(len(response["matches"]), 1)
        self.assertEqual(response["matches"][0]["lesson"]["assertions"][0]["object_value"], "safe-generic-rule")
        self.assertEqual(response["matches"][0]["match"]["matched_features"], {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": [],
            "failure_modes": [],
        })
        self.assertEqual(response["matches"][0]["match"]["exact_query"], False)
        self.assertIn(
            "at least two structured applicability dimensions matched",
            response["matches"][0]["match"]["reasons"],
        )
        self.assertEqual(len(response["excluded_candidates"]), 1)
        excluded = response["excluded_candidates"][0]
        self.assertEqual(excluded["reason"], "insufficient structured applicability match")
        self.assertEqual(excluded["unmet_condition_count"], 0)
        self.assertTrue(excluded["source_details_redacted"])
        self.assertIn("source incident narrative", excluded["redactions"][0]["fields"])
        self.assertNotIn(private_term, json.dumps(response, ensure_ascii=False))

    def test_search_forwards_opaque_cursor_and_returns_next_page_cursor(self) -> None:
        self.service.search_next_cursor = "opaque-next-cursor"

        response = json.loads(
            self.tool("design_lesson_search")(
                "bearing seizure",
                "org-001",
                "{}",
                7,
                "opaque-input-cursor",
            )
        )

        self.assertEqual(
            self.service.calls,
            [(
                "search_page",
                {
                    "query": "bearing seizure",
                    "limit": 7,
                    "cursor": "opaque-input-cursor",
                },
            )],
        )
        self.assertEqual(response["next_cursor"], "opaque-next-cursor")

    def test_search_bounds_excluded_candidates_to_requested_page_size(self) -> None:
        self.service.search_results = [
            self._lesson_candidate(
                f"lesson-unmatched-{index}",
                private_term=f"PRIVATE-{index}",
                component_classes=["unrelated"],
                interface_types=["unrelated"],
            )
            for index in range(3)
        ]

        response = json.loads(
            self.tool("design_lesson_search")(
                "",
                "org-001",
                '{"component_classes":["actuator"],"interface_types":["mount"]}',
                2,
            )
        )

        self.assertEqual(len(response["excluded_candidates"]), 2)

    @staticmethod
    def _lesson_candidate(
        lesson_id: str,
        *,
        private_term: str,
        component_classes: list[str],
        interface_types: list[str],
    ) -> dict:
        return {
            "id": lesson_id,
            "lesson_key": lesson_id,
            "revision": 1,
            "status": "approved",
            "source_family_id": "private-family",
            "title": private_term,
            "problem": {"summary": private_term, "failure_modes": []},
            "root_causes": [private_term],
            "corrections": [private_term],
            "prevention": {"check": private_term},
            "applicability": {
                "component_classes": component_classes,
                "interface_types": interface_types,
                "design_stages": [],
                "required_conditions": [],
            },
            "non_applicable_conditions": [private_term],
            "search_terms": [],
            "assertions": [
                {
                    "id": f"assertion-{lesson_id}",
                    "subject_ref": "component:actuator",
                    "predicate": "requires-clearance",
                    "object_value": "safe-generic-rule",
                    "unit": None,
                    "scope_kind": "organization_general",
                    "family_id": None,
                    "risk_level": "R3",
                    "source_kind": "approved_design_lesson",
                    "evidence": [{"source": private_term}],
                    "applicability": {"constraint_kind": "check"},
                    "non_applicable_conditions": [],
                    "status": "approved",
                }
            ],
        }

    def test_design_context_compatibly_forwards_empty_lesson_inputs(self) -> None:
        self.tool("design_context_build")("org-001", "group-001")
        self.assertEqual(
            self.service.calls,
            [
                (
                    "context",
                    {
                        "organization_id": "org-001",
                        "design_group_id": "group-001",
                        "requested_family_id": None,
                        "model_revision_id": None,
                        "explicit_family_authorization": False,
                        "confirmed_in_current_session": False,
                        "user_requested_analogy": False,
                        "design_features": {"satisfied_conditions": []},
                        "lesson_query": "",
                    },
                )
            ],
        )

class _JobManifestForService:
    def __init__(self, job_id: str = "00000000-0000-4000-8000-000000000401") -> None:
        self.job_id = job_id

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "MechanicalDesignJob/v1",
            "job_id": self.job_id,
            "display_id": "JOB-20260823-401",
            "title": "Authorized pump",
            "revision": 4,
        }


class _JobRepairForService:
    def __init__(self, manifest: _JobManifestForService, reason: str) -> None:
        self.manifest = manifest
        self.reason = reason

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "MechanicalDesignJobRepair/v1",
            "job": self.manifest.as_dict(),
            "audit": {
                "action": "repair",
                "reason": self.reason,
                "actor_id": "configured-actor",
                "authoritative_revision": 4,
            },
        }


class _JobManagerForService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.manifest = _JobManifestForService()

    def create(self, **kwargs: object) -> _JobManifestForService:
        self.calls.append(("create", kwargs))
        return self.manifest

    def get(self, **kwargs: object) -> _JobManifestForService:
        self.calls.append(("get", kwargs))
        return self.manifest

    def list(self, **kwargs: object) -> list[_JobManifestForService]:
        self.calls.append(("list", kwargs))
        return [self.manifest]

    def resolve(self, **kwargs: object) -> list[_JobManifestForService]:
        self.calls.append(("resolve", kwargs))
        return [self.manifest]

    def close(self, **kwargs: object) -> _JobManifestForService:
        self.calls.append(("close", kwargs))
        return self.manifest

    def reopen(self, **kwargs: object) -> _JobManifestForService:
        self.calls.append(("reopen", kwargs))
        return self.manifest

    def doctor(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("doctor", kwargs))
        return {
            "schema_version": "MechanicalDesignJobDoctor/v1",
            "job_id": self.manifest.job_id,
            "authoritative_revision": 4,
            "receipt_sha256": "a" * 64,
            "status": "blocked",
            "issues": [],
        }

    def repair(self, **kwargs: object) -> _JobRepairForService:
        self.calls.append(("repair", kwargs))
        return _JobRepairForService(self.manifest, str(kwargs["reason"]))


class ServiceDesignJobFacadeTests(unittest.TestCase):
    def _service(self, manager: _JobManagerForService | None = None) -> MechanicalDesignService:
        service = MechanicalDesignService.__new__(MechanicalDesignService)
        service.settings = SimpleNamespace(actor_id="configured-actor")
        service.bootstrap_config = {
            "organization_id": "org-configured",
            "design_group_id": "group-configured",
        }
        service.design_jobs = manager or _JobManagerForService()
        service._require_database = lambda: None
        return service

    def test_design_job_create_uses_only_configured_scope_and_actor(self) -> None:
        service = self._service()

        result = service.design_job_create(
            job_type="mechanical_design",
            title="Pump design",
            organization_id="org-configured",
            design_group_id="group-configured",
            family_id="family-001",
            idempotency_token="job-create-001",
        )

        self.assertEqual(result["schema_version"], "MechanicalDesignJob/v1")
        name, call = service.design_jobs.calls[-1]
        self.assertEqual(name, "create")
        self.assertEqual(call["organization_id"], "org-configured")
        self.assertEqual(call["design_group_id"], "group-configured")
        self.assertEqual(call["actor_id"], "configured-actor")
        with self.assertRaisesRegex(PermissionError, "configured organization"):
            service.design_job_create(
                job_type="mechanical_design",
                title="Foreign",
                organization_id="other-org",
                design_group_id="group-configured",
                family_id=None,
                idempotency_token="job-create-foreign",
            )
        self.assertEqual([name for name, _ in service.design_jobs.calls], ["create"])

    def test_design_job_get_authorizes_before_manifest_access_and_redacts_failure(self) -> None:
        private_title = "PRIVATE foreign job title"

        class UnauthorizedManager(_JobManagerForService):
            def get(self, **kwargs: object) -> _JobManifestForService:
                self.calls.append(("get", kwargs))
                raise JobFailure(
                    "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                    "Job identity is unknown or outside the authorized scope",
                )

        manager = UnauthorizedManager()
        service = self._service(manager)
        with self.assertRaises(JobFailure) as captured:
            service.design_job_get(job_id="00000000-0000-4000-8000-000000000499")

        self.assertNotIn(private_title, str(captured.exception))
        self.assertEqual([name for name, _ in manager.calls], ["get"])
        self.assertEqual(manager.calls[0][1]["organization_id"], "org-configured")
        with self.assertRaisesRegex(ValueError, "filesystem path"):
            service.design_job_get(job_id="../private/job.json")
        self.assertEqual([name for name, _ in manager.calls], ["get"])

    def test_design_job_resolve_returns_candidates_without_selecting_one(self) -> None:
        service = self._service()

        result = service.design_job_resolve(query="pump")

        self.assertEqual(result["schema_version"], "MechanicalDesignJobResolution/v1")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(service.design_jobs.calls[-1][0], "resolve")

    def test_close_reopen_and_repair_require_revision_reason_and_user_confirmation(self) -> None:
        service = self._service()
        job_id = "00000000-0000-4000-8000-000000000401"
        with self.assertRaisesRegex(ValueError, "reason"):
            service.design_job_close(
                job_id=job_id,
                expected_revision=4,
                status="completed",
                phase="completed",
                reason=" ",
                confirmation=f"关闭 {job_id}",
            )
        with self.assertRaisesRegex(ValueError, "confirmation"):
            service.design_job_close(
                job_id=job_id,
                expected_revision=4,
                status="completed",
                phase="completed",
                reason="delivered",
                confirmation=f"重开 {job_id}",
            )
        self.assertEqual(service.design_jobs.calls, [])

        closed = service.design_job_close(
            job_id=job_id,
            expected_revision=4,
            status="completed",
            phase="completed",
            reason="delivered",
            confirmation=f"关闭 {job_id}",
        )
        reopened = service.design_job_reopen(
            job_id=job_id,
            expected_revision=4,
            phase="requirements",
            reason="follow-up",
            confirmation=f"重开 {job_id}",
        )
        repaired = service.design_job_repair(
            job_id=job_id,
            expected_revision=4,
            doctor_receipt_sha256="a" * 64,
            reason="republish manifest",
            confirmation=f"修复 {job_id}",
        )

        self.assertEqual(closed["schema_version"], "MechanicalDesignJob/v1")
        self.assertEqual(reopened["schema_version"], "MechanicalDesignJob/v1")
        self.assertEqual(repaired["schema_version"], "MechanicalDesignJobRepair/v1")
        repair = [item for item in service.design_jobs.calls if item[0] == "repair"]
        self.assertEqual(repair[0][1]["expected_revision"], 4)
        self.assertEqual([name for name, _ in service.design_jobs.calls], [
            "close", "reopen", "repair"
        ])

    def test_job_confirmation_is_one_exact_canonical_phrase(self) -> None:
        service = self._service()
        job_id = "00000000-0000-4000-8000-000000000401"
        for confirmation in (
            f"不关闭 {job_id}",
            f"关闭 {job_id} now",
            f"关闭 {job_id} {job_id}",
            f"关闭 {job_id}x",
            f"关闭 JOB-20260823-401",
        ):
            with self.assertRaisesRegex(ValueError, "canonical"):
                service.design_job_close(
                    job_id=job_id,
                    expected_revision=4,
                    status="completed",
                    phase="completed",
                    reason="delivered",
                    confirmation=confirmation,
                )
        service.design_job_close(
            job_id=job_id.upper(),
            expected_revision=4,
            status="completed",
            phase="completed",
            reason="delivered",
            confirmation=f"关闭\t{job_id.upper()}",
        )
        self.assertEqual([name for name, _ in service.design_jobs.calls], ["close"])

    def test_source_files_are_explicitly_rejected_until_snapshots_exist(self) -> None:
        service = self._service()
        with self.assertRaises(JobFailure) as captured:
            service.design_job_create(
                job_type="mechanical_design", title="Pump", organization_id="org-configured",
                design_group_id="group-configured", family_id=None, idempotency_token="source-001",
                source_files=["input.FCStd"],
            )
        self.assertEqual(captured.exception.code, "JOB_SOURCE_SNAPSHOTS_NOT_READY")
        self.assertEqual(service.design_jobs.calls, [])

    def test_repair_returns_only_the_exact_repair_wrapper(self) -> None:
        service = self._service()
        job_id = "00000000-0000-4000-8000-000000000401"
        response = service.design_job_repair(
            job_id=job_id, expected_revision=4, doctor_receipt_sha256="a" * 64,
            reason="exact service wrapper", confirmation=f"修复 {job_id}",
        )

        self.assertEqual(set(response), {"schema_version", "job", "audit"})
        self.assertEqual(response["schema_version"], "MechanicalDesignJobRepair/v1")
        self.assertEqual(response["job"]["schema_version"], "MechanicalDesignJob/v1")
        self.assertNotIn("repair_audit", response["job"])


if __name__ == "__main__":
    unittest.main()
