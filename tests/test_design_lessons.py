from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mechanical_design_agent.design_lessons import (
    DesignLessonStagingStore,
    condition_expression_satisfied,
    match_design_lesson,
    normalize_design_features,
    satisfying_conditions,
    validate_design_lesson_package,
)
from mechanical_design_agent.package_resources import schemas_directory
from mechanical_design_agent.secure_fs import validate_managed_path


def valid_package() -> dict:
    return {
        "schema_version": "DesignLessonPackage/v1",
        "lesson_id": "DL-001",
        "title": "Verify actuator mounting clearance",
        "codex_session_id": "session-001",
        "source": {
            "organization_id": "org-001",
            "design_group_id": "group-001",
            "family_id": "family-001",
            "working_copy_id": "00000000-0000-0000-0000-000000000011",
            "change_set_ids": ["00000000-0000-0000-0000-000000000012"],
            "before_model_sha256": "1" * 64,
            "after_model_sha256": "2" * 64,
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
        "evidence_manifest": [
            {
                "evidence_id": "validation-evidence",
                "path": "evidence.json",
                "role": "geometry_validation",
                "media_type": "application/json",
                "sha256": "3" * 64,
                "working_copy_id": "00000000-0000-0000-0000-000000000011",
                "change_set_id": "00000000-0000-0000-0000-000000000012",
                "model_sha256": "2" * 64,
                "validation_kind": "geometry_model",
            }
        ],
    }


def test_true_boolean_features_become_satisfied_conditions_without_mutation():
    original = {
        "has_lifting_interface": True,
        "has_compressible_component": False,
        "satisfied_conditions": ["freecad_connect_mechanical_cad"],
        "component_classes": ["lifting point"],
    }
    normalized = normalize_design_features(original)
    assert normalized["satisfied_conditions"] == [
        "freecad_connect_mechanical_cad",
        "has_lifting_interface",
    ]
    assert original["satisfied_conditions"] == ["freecad_connect_mechanical_cad"]


def test_false_boolean_feature_does_not_satisfy_condition():
    normalized = normalize_design_features({"has_lifting_interface": False})
    assert normalized["satisfied_conditions"] == []


def test_satisfying_conditions_selects_one_deterministic_any_of_branch():
    expression = {
        "all_of": [
            "freecad_connect_mechanical_cad",
            {"any_of": ["has_lifting_interface", "has_compressible_component"]},
        ]
    }
    result = satisfying_conditions(["validated-model"], expression)
    assert result == [
        "freecad_connect_mechanical_cad",
        "has_lifting_interface",
        "validated-model",
    ]
    assert condition_expression_satisfied(expression, set(result))


def valid_evidence_item(path: str = "evidence.json") -> dict[str, str]:
    return {
        "evidence_id": "validation-evidence",
        "path": path,
        "role": "geometry_validation",
        "media_type": "application/json",
        "working_copy_id": "00000000-0000-0000-0000-000000000011",
        "change_set_id": "00000000-0000-0000-0000-000000000012",
        "model_sha256": "2" * 64,
        "validation_kind": "geometry_model",
    }


class DesignLessonValidationTests(unittest.TestCase):
    @staticmethod
    def schema_validator() -> Draft202012Validator:
        with schemas_directory() as schemas:
            schema = json.loads(
                (schemas / "design-lesson-package-v1.schema.json").read_text(
                    encoding="utf-8"
                )
            )
        return Draft202012Validator(schema)

    def test_validate_returns_independent_validated_package(self) -> None:
        package = valid_package()

        validated = validate_design_lesson_package(package)
        package["title"] = "mutated after validation"

        self.assertEqual(validated["title"], "Verify actuator mounting clearance")

    def test_validate_rejects_missing_required_package_content(self) -> None:
        for field, value, message in (
            ("schema_version", "wrong", "invalid design lesson schema_version"),
            ("lesson_id", "unsafe/id", "lesson_id contains unsafe characters"),
            ("title", " ", "design lesson title is required"),
            ("root_causes", [], "root_causes and corrections must be nonempty"),
            ("atomic_assertions", [], "atomic_assertions must be nonempty"),
        ):
            with self.subTest(field=field):
                package = valid_package()
                package[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    validate_design_lesson_package(package)

    def test_validate_rejects_incomplete_or_unsupported_assertion(self) -> None:
        package = valid_package()
        package["atomic_assertions"][0]["assertion_key"] = "unsafe/key"
        with self.assertRaisesRegex(ValueError, "assertion_key contains unsafe characters"):
            validate_design_lesson_package(package)

        package = valid_package()
        package["atomic_assertions"][0]["constraint_kind"] = "advice"
        with self.assertRaisesRegex(ValueError, "unsupported constraint_kind: advice"):
            validate_design_lesson_package(package)

    def test_validate_rejects_duplicate_assertion_keys(self) -> None:
        package = valid_package()
        package["atomic_assertions"].append(dict(package["atomic_assertions"][0]))

        with self.assertRaisesRegex(ValueError, "atomic assertion keys must be unique"):
            validate_design_lesson_package(package)

    def test_validate_rejects_non_string_identifier_and_text_fields(self) -> None:
        for field, value in (
            ("lesson_id", 1),
            ("title", 1),
            ("codex_session_id", 1),
        ):
            with self.subTest(field=field):
                package = valid_package()
                package[field] = value
                with self.assertRaisesRegex(ValueError, f"{field} must be a string"):
                    validate_design_lesson_package(package)

        for field in ("assertion_key", "subject_ref", "predicate"):
            with self.subTest(field=field):
                package = valid_package()
                package["atomic_assertions"][0][field] = 1
                with self.assertRaisesRegex(ValueError, f"{field} must be a string"):
                    validate_design_lesson_package(package)

    def test_validate_rejects_non_finite_json_numbers_and_blank_search_terms(self) -> None:
        package = valid_package()
        package["atomic_assertions"][0]["object_value"] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
            validate_design_lesson_package(package)

        package = valid_package()
        package["search_terms"] = [" "]
        with self.assertRaisesRegex(ValueError, "search_terms must not contain blank values"):
            validate_design_lesson_package(package)

    def test_runtime_and_schema_require_evidence_for_approval_ready_packages(self) -> None:
        mutations = (
            lambda package: package.update(evidence_manifest=[]),
            lambda package: package["atomic_assertions"][0].update(evidence_refs=[]),
            lambda package: package["evidence_manifest"][0].update(role="supporting_report"),
        )
        validator = self.schema_validator()
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                package = valid_package()
                mutate(package)
                with self.assertRaises(ValueError):
                    validate_design_lesson_package(package)
                self.assertTrue(list(validator.iter_errors(package)))

    def test_runtime_and_schema_share_typed_evidence_contract(self) -> None:
        validator = self.schema_validator()
        valid = valid_package()
        self.assertEqual(list(validator.iter_errors(valid)), [])
        validate_design_lesson_package(valid)

        for mutate in (
            lambda package: package["evidence_manifest"][0].update(role="unknown-role"),
            lambda package: package["evidence_manifest"][0].pop("working_copy_id"),
            lambda package: package["evidence_manifest"][0].update(
                role="fastener_interface_validation",
                validation_kind="geometry_model",
            ),
        ):
            with self.subTest(mutate=mutate):
                package = valid_package()
                mutate(package)
                with self.assertRaises(ValueError):
                    validate_design_lesson_package(package)
                self.assertTrue(list(validator.iter_errors(package)))

    def test_validate_rejects_malformed_nested_domain_values(self) -> None:
        mutations = (
            (lambda package: package["applicability"].update(component_classes=[{}]), "component_classes"),
            (lambda package: package["problem"].update(failure_modes=[1]), "failure_modes"),
            (lambda package: package["prevention"].update(required_checks=[{}]), "required_checks"),
            (lambda package: package.update(root_causes=[{}]), "root_causes"),
            (lambda package: package.update(corrections=[""]), "corrections"),
            (lambda package: package["source"].update(change_set_ids=[{}]), "change_set_ids"),
            (lambda package: package.update(non_applicable_conditions=[{}]), "non_applicable_conditions"),
            (lambda package: package["atomic_assertions"][0].update(contradicts=[{}]), "contradicts"),
        )
        for mutate, label in mutations:
            with self.subTest(label=label):
                package = valid_package()
                mutate(package)
                with self.assertRaisesRegex(ValueError, label):
                    validate_design_lesson_package(package)

    def test_validate_rejects_undeclared_top_level_fields_and_duplicate_change_sets(self) -> None:
        for field, value in (
            ("status", "revoked"),
            ("unexpected", {"nested": "value"}),
        ):
            with self.subTest(field=field):
                package = valid_package()
                package[field] = value
                with self.assertRaisesRegex(ValueError, "unsupported top-level fields"):
                    validate_design_lesson_package(package)

        package = valid_package()
        change_set_id = package["source"]["change_set_ids"][0]
        package["source"]["change_set_ids"] = [change_set_id, change_set_id]
        with self.assertRaisesRegex(ValueError, "source.change_set_ids must contain unique values"):
            validate_design_lesson_package(package)

    def test_validate_accepts_structured_any_of_condition_expression(self) -> None:
        package = valid_package()
        package["applicability"]["required_conditions"] = []
        package["applicability"]["required_condition_expression"] = {
            "any_of": ["shaft-support", "guide-carrier"]
        }

        validated = validate_design_lesson_package(package)

        self.assertEqual(
            validated["applicability"]["required_condition_expression"],
            {"any_of": ["shaft-support", "guide-carrier"]},
        )

    def test_validate_rejects_malformed_condition_expression(self) -> None:
        for expression in (
            {"any_of": []},
            {"any_of": [1]},
            {"unknown": ["shaft-support"]},
            {"all_of": [{"any_of": []}]},
        ):
            with self.subTest(expression=expression):
                package = valid_package()
                package["applicability"]["required_condition_expression"] = expression
                with self.assertRaisesRegex(ValueError, "required_condition_expression"):
                    validate_design_lesson_package(package)


class DesignLessonStagingTests(unittest.TestCase):
    def test_slr700_shape_uses_stable_evidence_ids_and_any_of_applicability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence_items = []
            roles = (
                ("before-model", "source_before_model", None),
                ("after-working-copy", "source_after_model", None),
                ("geometry-validation", "geometry_validation", "geometry_model"),
                ("fastener-validation", "fastener_interface_validation", "fastener_interfaces"),
                ("mechanical-validation", "mechanical_interface_validation", "mechanical_interfaces"),
                ("assembly-validation", "assembly_completeness_validation", "assembly_completeness"),
                ("review-image", "review_image", None),
            )
            for evidence_id, role, validation_kind in roles:
                suffix = ".png" if role == "review_image" else ".json"
                path = f"{evidence_id}{suffix}"
                (workspace / path).write_bytes(evidence_id.encode("utf-8"))
                descriptor = {
                    "evidence_id": evidence_id,
                    "path": path,
                    "role": role,
                    "media_type": "image/png" if suffix == ".png" else "application/json",
                }
                if validation_kind is not None:
                    descriptor.update({
                        "working_copy_id": valid_package()["source"]["working_copy_id"],
                        "change_set_id": valid_package()["source"]["change_set_ids"][0],
                        "model_sha256": valid_package()["source"]["after_model_sha256"],
                        "validation_kind": validation_kind,
                    })
                evidence_items.append(descriptor)
            package = valid_package()
            package["lesson_id"] = "DL-SLR700-20260810-001"
            package["applicability"]["required_conditions"] = []
            alternatives = [
                "functional shaft or support datum is present",
                "critical carrier clearance is present",
                "clearance hole edge ligament requires evaluation",
                "threaded joint requires engagement validation",
                "delivery requires critical interface evidence",
            ]
            package["applicability"]["required_condition_expression"] = {
                "any_of": alternatives
            }
            package["atomic_assertions"] = [
                {
                    **package["atomic_assertions"][0],
                    "assertion_key": f"slr-rule-{index}",
                    "evidence_refs": refs,
                }
                for index, refs in enumerate((
                    ["before-model", "after-working-copy", "mechanical-validation"],
                    ["before-model", "after-working-copy", "mechanical-validation", "assembly-validation"],
                    ["before-model", "after-working-copy", "fastener-validation", "review-image"],
                    ["before-model", "after-working-copy", "fastener-validation", "review-image"],
                    ["after-working-copy", "geometry-validation", "fastener-validation", "mechanical-validation", "assembly-validation"],
                ), start=1)
            ]

            staged = DesignLessonStagingStore(workspace).stage(package, evidence_items)
            candidate = json.loads(Path(staged["lesson_json_path"]).read_text(encoding="utf-8"))

            evidence_ids = {item["evidence_id"] for item in candidate["evidence_manifest"]}
            self.assertEqual(len(evidence_ids), 7)
            self.assertTrue(all(set(item["evidence_refs"]) <= evidence_ids for item in candidate["atomic_assertions"]))
            matched = match_design_lesson(candidate, {
                "component_classes": ["actuator"],
                "interface_types": ["mount"],
                "declared_conditions": [alternatives[2]],
            }, "")
            unmatched = match_design_lesson(candidate, {
                "component_classes": ["actuator"],
                "interface_types": ["mount"],
                "declared_conditions": [],
            }, "")
            self.assertTrue(matched["eligible"])
            self.assertFalse(unmatched["eligible"])

    def test_stage_requires_stable_evidence_ids_and_typed_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)

            missing_id = valid_evidence_item()
            missing_id.pop("evidence_id")
            with self.assertRaisesRegex(ValueError, "evidence_id"):
                store.stage(valid_package(), [missing_id])

            unknown_role = valid_evidence_item()
            unknown_role["role"] = "validation 426/426"
            with self.assertRaisesRegex(ValueError, "evidence role"):
                store.stage(valid_package(), [unknown_role])

    def test_stage_rejects_assertion_evidence_reference_that_is_not_a_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            package = valid_package()
            package["atomic_assertions"][0]["evidence_refs"] = ["evidence.json"]

            with self.assertRaisesRegex(ValueError, "evidence_refs.*evidence_id"):
                DesignLessonStagingStore(workspace).stage(package, [valid_evidence_item()])

    def test_review_staging_keys_same_lesson_revisions_by_package_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            first_package = valid_package()
            edited_package = valid_package()
            edited_package["corrections"] = ["Add clearance and motion-envelope checks"]

            first = store.stage_review(first_package, [valid_evidence_item()])
            edited = store.stage_review(edited_package, [valid_evidence_item()])

            self.assertEqual(first["lesson_id"], edited["lesson_id"])
            self.assertNotEqual(first["package_sha256"], edited["package_sha256"])
            self.assertEqual(
                store.get_review(first["package_sha256"])["package"]["corrections"],
                ["Add the clearance check"],
            )
            self.assertEqual(
                store.get_review(edited["package_sha256"])["package"]["corrections"],
                ["Add clearance and motion-envelope checks"],
            )
            for path_field in (
                "lesson_json_path",
                "lesson_markdown_path",
                "evidence_manifest_path",
            ):
                Path(first[path_field]).write_bytes(Path(edited[path_field]).read_bytes())
            self.assertEqual(
                store.get_review(first["package_sha256"])["status"],
                "integrity-drift",
            )
            with self.assertRaisesRegex(ValueError, "review package is already staged"):
                store.stage_review(edited_package, [valid_evidence_item()])

            legacy = store.stage(valid_package(), [valid_evidence_item()])
            self.assertEqual(Path(legacy["lesson_json_path"]).parent.name, "DL-001")
            self.assertNotIn("storage_key", legacy)
            self.assertNotIn("storage_key", store.get("DL-001"))

    def test_review_digest_access_returns_verified_no_follow_package_and_evidence_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage_review(valid_package(), [valid_evidence_item()])

            package_paths = store.review_package_paths(staged["package_sha256"])
            evidence_paths = store.review_evidence_paths(staged["package_sha256"])

            self.assertTrue(package_paths["lesson_json"].is_file())
            self.assertEqual(evidence_paths[0][0]["evidence_id"], "validation-evidence")
            self.assertEqual(
                evidence_paths[0][1],
                validate_managed_path(
                    workspace / "evidence.json",
                    allow_missing_leaf=False,
                ).path,
            )

    def test_staged_get_returns_complete_integrity_report_and_evidence_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(valid_package(), [valid_evidence_item()])

            current = store.get(valid_package()["lesson_id"])

            self.assertEqual(current["package_sha256"], staged["package_sha256"])
            self.assertEqual(current["status"], "verified-local-only")
            self.assertEqual(current["paths"]["lesson_markdown"], staged["lesson_markdown_path"])
            self.assertEqual(current["file_integrity"]["lesson_markdown"]["status"], "verified")
            self.assertEqual(current["evidence_integrity"][0]["evidence_id"], "validation-evidence")
            self.assertEqual(current["evidence_integrity"][0]["status"], "verified")

            evidence.write_text('{"changed":true}', encoding="utf-8")
            drifted = store.get(valid_package()["lesson_id"])
            self.assertEqual(drifted["status"], "integrity-drift")
            self.assertEqual(drifted["evidence_integrity"][0]["status"], "sha256-mismatch")

    def test_staged_get_reports_a_missing_package_file_as_integrity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(valid_package(), [valid_evidence_item()])
            Path(staged["lesson_markdown_path"]).unlink()

            current = store.get(valid_package()["lesson_id"])

            self.assertEqual(current["status"], "integrity-drift")
            self.assertEqual(
                current["file_integrity"]["lesson_markdown"]["status"],
                "missing",
            )

    def test_markdown_renders_complete_package_and_escapes_injected_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            package = valid_package()
            package["title"] = "Safe title\n## Injected heading"
            package["root_causes"] = ["cause\n# forged"]
            package["prevention"]["required_checks"] = ["check *all* interfaces"]
            package["search_terms"] = ["mount clearance", "轴系支撑"]
            package["source"]["source_scope_note"] = "SOURCE-SCOPE-NOTE"
            package["applicability"]["application_note"] = "APPLICATION-NOTE"
            package["atomic_assertions"][0]["confidence"] = 0.875

            staged = DesignLessonStagingStore(workspace).stage(
                package, [valid_evidence_item()]
            )
            markdown = Path(staged["lesson_markdown_path"]).read_text(encoding="utf-8")

            for heading in (
                "## Prevention",
                "## Applicability",
                "## Non-applicable conditions",
                "## Search terms",
                "## Atomic assertions",
                "## Source",
                "## Canonical full package",
            ):
                self.assertIn(heading, markdown)
            self.assertIn("hard_constraint", markdown)
            self.assertIn("validation-evidence", markdown)
            self.assertIn("轴系支撑", markdown)
            self.assertIn("SOURCE-SCOPE-NOTE", markdown)
            self.assertIn("APPLICATION-NOTE", markdown)
            self.assertIn("0.875", markdown)
            for binding_value in (
                package["source"]["working_copy_id"],
                package["source"]["change_set_ids"][0],
                package["source"]["after_model_sha256"],
                valid_evidence_item()["validation_kind"],
            ):
                self.assertIn(binding_value, markdown)
            self.assertNotIn("\n## Injected heading\n", markdown)
            self.assertNotIn("\n# forged\n", markdown)

    def test_verify_rejects_markdown_changed_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(valid_package(), [valid_evidence_item()])
            Path(staged["lesson_markdown_path"]).write_text("tampered\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "lesson Markdown changed"):
                store.verify(valid_package()["lesson_id"], staged["package_sha256"])

    def test_stage_publishes_the_complete_directory_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "evidence.json").write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            original_write = store._atomic_write
            writes = 0

            def fail_second_file(path: Path, contents: bytes) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected staging failure")
                original_write(path, contents)

            with patch.object(store, "_atomic_write", side_effect=fail_second_file):
                with self.assertRaisesRegex(OSError, "injected staging failure"):
                    store.stage(valid_package(), [valid_evidence_item()])

            lesson_dir = store.staging_root / valid_package()["lesson_id"]
            self.assertFalse(lesson_dir.exists())
            self.assertEqual(list(store.staging_root.glob(".DL-001.*")), [])

            staged = store.stage(valid_package(), [valid_evidence_item()])
            self.assertTrue(Path(staged["lesson_json_path"]).is_file())

    def test_stage_writes_files_without_repository_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text('{"status":"passed"}', encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            result = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            lesson = json.loads(Path(result["lesson_json_path"]).read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "staged-local-only")
            self.assertTrue(Path(result["lesson_json_path"]).is_file())
            self.assertTrue(Path(result["lesson_markdown_path"]).is_file())
            self.assertEqual(len(result["package_sha256"]), 64)
            self.assertEqual(lesson["evidence_manifest"][0]["sha256"], "76f1805001bc4f155c8efa5651c57c4f1858af1f6786cbe4596214a4b64375a6")

    def test_verify_rejects_package_changed_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            Path(staged["lesson_json_path"]).write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "package SHA-256 changed"):
                store.verify(valid_package()["lesson_id"], staged["package_sha256"])

    def test_verify_rejects_evidence_changed_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            evidence.write_text('{"changed":true}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "evidence SHA-256 changed"):
                store.verify(valid_package()["lesson_id"], staged["package_sha256"])

    def test_verify_rejects_package_copied_to_a_different_lesson_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            copied_directory = store.staging_root / "DL-COPIED"
            copied_directory.mkdir()
            for source_key, target_name in (
                ("lesson_json_path", "lesson.json"),
                ("lesson_markdown_path", "lesson.md"),
                ("evidence_manifest_path", "evidence-manifest.json"),
            ):
                (copied_directory / target_name).write_bytes(
                    Path(staged[source_key]).read_bytes()
                )

            with self.assertRaisesRegex(ValueError, "lesson_id does not match requested lesson"):
                store.verify("DL-COPIED", staged["package_sha256"])

    def test_verify_rejects_changed_independent_evidence_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            Path(staged["evidence_manifest_path"]).write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "evidence manifest changed after staging"):
                store.verify(valid_package()["lesson_id"], staged["package_sha256"])

    def test_verify_rejects_symlinked_independent_evidence_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            evidence = workspace / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            store = DesignLessonStagingStore(workspace)
            staged = store.stage(
                valid_package(),
                [valid_evidence_item()],
            )
            manifest_path = Path(staged["evidence_manifest_path"])
            replacement = workspace / "replacement.json"
            replacement.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
            manifest_path.unlink()
            manifest_path.symlink_to(replacement)

            with self.assertRaisesRegex(ValueError, "evidence manifest path must not be a symlink"):
                store.verify(valid_package()["lesson_id"], staged["package_sha256"])

    def test_stage_rejects_workspace_escape_and_symlinked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            workspace = Path(temporary)
            outside_evidence = Path(outside) / "evidence.json"
            outside_evidence.write_text("{}", encoding="utf-8")
            linked_evidence = workspace / "linked.json"
            linked_evidence.symlink_to(outside_evidence)
            store = DesignLessonStagingStore(workspace)

            with self.assertRaisesRegex(ValueError, "evidence path escapes workspace"):
                store.stage(valid_package(), [valid_evidence_item("../evidence.json")])
            with self.assertRaisesRegex(ValueError, "evidence path must not be a symlink"):
                store.stage(valid_package(), [valid_evidence_item("linked.json")])

    def test_get_rejects_a_symlinked_staged_lesson_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            store = DesignLessonStagingStore(workspace)
            redirected = workspace / "redirected"
            redirected.mkdir()
            (redirected / "lesson.json").write_text("{}", encoding="utf-8")
            (store.staging_root / "DL-001").symlink_to(redirected, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "lesson package path must not be a symlink"):
                store.get("DL-001")

    @unittest.skipIf(os.name == "nt", "POSIX atomic publication regression")
    def test_atomic_write_removes_temporary_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "lesson.json"

            with patch(
                "mechanical_design_agent.secure_fs_posix.os.link",
                side_effect=OSError("link failed"),
            ):
                with self.assertRaisesRegex(OSError, "link failed"):
                    DesignLessonStagingStore._atomic_write(destination, b"package")

            self.assertEqual(list(Path(temporary).glob(".lesson.json.*")), [])


class DesignLessonMatchingTests(unittest.TestCase):
    def test_match_requires_conditions_and_two_applicability_dimensions(self) -> None:
        lesson = valid_package()
        features = {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": ["detail"],
            "failure_modes": [],
            "satisfied_conditions": ["moving-assembly"],
        }

        match = match_design_lesson(lesson, features, "")

        self.assertTrue(match["eligible"])
        self.assertEqual(match["matched_features"]["component_classes"], ["actuator"])
        self.assertEqual(match["unmet_conditions"], [])

        features["satisfied_conditions"] = []
        excluded = match_design_lesson(lesson, features, "")
        self.assertFalse(excluded["eligible"])
        self.assertEqual(excluded["exclusion_reasons"], ["unmet required conditions"])

    def test_match_explains_insufficient_single_dimension(self) -> None:
        lesson = valid_package()
        features = {
            "component_classes": ["actuator"],
            "interface_types": [],
            "design_stages": [],
            "failure_modes": [],
            "satisfied_conditions": ["moving-assembly"],
        }

        match = match_design_lesson(lesson, features, "")

        self.assertFalse(match["eligible"])
        self.assertEqual(match["matched_dimension_count"], 1)
        self.assertEqual(match["exclusion_reasons"], ["insufficient structured applicability match"])

    def test_match_rejects_declared_non_applicable_condition(self) -> None:
        lesson = valid_package()
        lesson["non_applicable_conditions"] = ["sealed-unit"]
        features = {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": ["detail"],
            "failure_modes": [],
            "satisfied_conditions": ["moving-assembly"],
            "declared_conditions": ["sealed-unit"],
        }

        match = match_design_lesson(lesson, features, "")

        self.assertFalse(match["eligible"])
        self.assertEqual(match["matched_non_applicable_conditions"], ["sealed-unit"])
        self.assertEqual(match["exclusion_reasons"], ["matched non-applicable conditions"])

    def test_match_allows_case_insensitive_exact_query(self) -> None:
        lesson = valid_package()
        features = {"component_classes": [], "interface_types": [], "design_stages": [], "failure_modes": [], "satisfied_conditions": ["moving-assembly"]}

        match = match_design_lesson(lesson, features, "  ACTUATOR CLEARANCE  ")

        self.assertTrue(match["eligible"])
        self.assertTrue(match["exact_query"])

    def test_match_does_not_treat_blank_query_or_term_as_exact_match(self) -> None:
        lesson = valid_package()
        lesson["search_terms"] = [""]
        features = {"component_classes": [], "interface_types": [], "design_stages": [], "failure_modes": [], "satisfied_conditions": ["moving-assembly"]}

        match = match_design_lesson(lesson, features, "   ")

        self.assertFalse(match["eligible"])
        self.assertFalse(match["exact_query"])

    def test_match_evaluates_any_of_condition_expression_logically(self) -> None:
        lesson = valid_package()
        lesson["applicability"]["required_conditions"] = []
        lesson["applicability"]["required_condition_expression"] = {
            "any_of": ["shaft-support", "guide-carrier"]
        }
        base_features = {
            "component_classes": ["actuator"],
            "interface_types": ["mount"],
            "design_stages": [],
            "failure_modes": [],
        }

        one_alternative = match_design_lesson(
            lesson,
            {**base_features, "declared_conditions": ["shaft-support"]},
            "",
        )
        no_alternative = match_design_lesson(
            lesson,
            {**base_features, "declared_conditions": []},
            "",
        )

        self.assertTrue(one_alternative["eligible"])
        self.assertFalse(no_alternative["eligible"])
        self.assertEqual(no_alternative["exclusion_reasons"], ["unmet required condition expression"])


if __name__ == "__main__":
    unittest.main()
