from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from .hashing import file_sha256, stable_hash
from .models import canonical_json, require_safe_id
from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_directory,
    atomic_publish_new,
    ensure_managed_directory,
    remove_owned_tree,
    relative_managed_path,
    validate_managed_path,
)


PUBLISHED_SCOPE = "organization_general"
PUBLISHED_RISK = "R3"
PUBLISHED_SOURCE = "approved_design_lesson"
CONSTRAINT_KINDS = {"check", "warning", "preference", "hard_constraint"}
EVIDENCE_ROLE_VALIDATION_KINDS = {
    "geometry_validation": "geometry_model",
    "assembly_completeness_validation": "assembly_completeness",
    "fastener_interface_validation": "fastener_interfaces",
    "mechanical_interface_validation": "mechanical_interfaces",
}
EVIDENCE_ROLES = {
    "source_before_model",
    "source_after_model",
    *EVIDENCE_ROLE_VALIDATION_KINDS,
    "review_image",
    "supporting_report",
}
SCREENING_EXCLUSION_REASONS = {
    "product_specific",
    "insufficient_evidence",
    "duplicate",
    "no_material_learning",
    "uncertain_applicability",
}

_REQUIRED_PACKAGE_FIELDS = (
    "schema_version",
    "lesson_id",
    "title",
    "codex_session_id",
    "source",
    "problem",
    "root_causes",
    "corrections",
    "prevention",
    "applicability",
    "non_applicable_conditions",
    "search_terms",
    "atomic_assertions",
    "evidence_manifest",
)
_STAGING_PARTS = ("output", "mechanical_design", "lesson_staging")
DESIGN_FEATURE_LIST_FIELDS = {
    "component_classes",
    "interface_types",
    "design_stages",
    "failure_modes",
    "satisfied_conditions",
    "declared_conditions",
    "explicit_requirements",
}


def normalize_design_features(features: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(features, dict):
        raise ValueError("design features must be an object")
    normalized = json.loads(json.dumps(features, ensure_ascii=False))
    declared = {
        str(item)
        for field in ("satisfied_conditions", "declared_conditions")
        for item in normalized.get(field, [])
        if isinstance(item, str) and item.strip()
    }
    declared.update(
        key
        for key, value in normalized.items()
        if key not in DESIGN_FEATURE_LIST_FIELDS and value is True
    )
    normalized["satisfied_conditions"] = sorted(declared)
    return normalized


def validate_design_lesson_package(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-compatible lesson package without any database access."""
    if not isinstance(raw, dict):
        raise ValueError("design lesson package must be an object")
    _reject_non_finite_json_numbers(raw)
    package = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    missing = [field for field in _REQUIRED_PACKAGE_FIELDS if field not in package]
    if missing:
        raise ValueError(f"design lesson package missing required fields: {', '.join(missing)}")
    unsupported = sorted(set(package) - set(_REQUIRED_PACKAGE_FIELDS))
    if unsupported:
        raise ValueError(
            "unsupported top-level fields: " + ", ".join(unsupported)
        )
    if package.get("schema_version") != "DesignLessonPackage/v1":
        raise ValueError("invalid design lesson schema_version")
    lesson_id = _require_string(package.get("lesson_id"), "lesson_id")
    require_safe_id(lesson_id, "lesson_id")
    title = _require_string(package.get("title"), "title")
    if not title.strip():
        raise ValueError("design lesson title is required")
    codex_session_id = _require_string(package.get("codex_session_id"), "codex_session_id")
    if not codex_session_id.strip():
        raise ValueError("codex_session_id is required")
    for field in ("source", "problem", "prevention", "applicability"):
        if not isinstance(package[field], dict):
            raise ValueError(f"{field} must be an object")
    source = package["source"]
    for field in ("organization_id", "design_group_id", "working_copy_id"):
        value = _require_string(source.get(field), f"source.{field}")
        if not value.strip():
            raise ValueError(f"source.{field} is required")
    family_id = source.get("family_id")
    if family_id is not None and (not isinstance(family_id, str) or not family_id.strip()):
        raise ValueError("source.family_id must be a nonblank string or null")
    for field in ("before_model_sha256", "after_model_sha256"):
        _require_sha256(source.get(field), f"source.{field}")
    change_set_ids = _require_string_list(
        source,
        "change_set_ids",
        nonempty=True,
        label="source.change_set_ids",
    )
    if len(set(change_set_ids)) != len(change_set_ids):
        raise ValueError("source.change_set_ids must contain unique values")

    problem = package["problem"]
    summary = problem.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("problem.summary must be a string")
    for field in ("discovery_stage", "severity"):
        value = _require_string(problem.get(field), f"problem.{field}")
        if not value.strip():
            raise ValueError(f"problem.{field} is required")
    for field in ("symptoms", "affected_components", "affected_interfaces", "failure_modes"):
        _require_string_list(problem, field, label=f"problem.{field}")

    if not isinstance(package["root_causes"], list) or not package["root_causes"] or not isinstance(package["corrections"], list) or not package["corrections"]:
        raise ValueError("root_causes and corrections must be nonempty")
    _require_nonblank_string_values(package["root_causes"], "root_causes")
    _require_nonblank_string_values(package["corrections"], "corrections")

    prevention = package["prevention"]
    _require_string_list(prevention, "required_checks", label="prevention.required_checks")
    _require_string_list(
        prevention,
        "design_review_questions",
        label="prevention.design_review_questions",
    )
    for field in ("workflow_gate", "detection_method"):
        value = _require_string(prevention.get(field), f"prevention.{field}")
        if not value.strip():
            raise ValueError(f"prevention.{field} is required")

    applicability = package["applicability"]
    for field in (
        "component_classes",
        "interface_types",
        "design_stages",
        "required_conditions",
    ):
        _require_string_list(applicability, field, label=f"applicability.{field}")
    expression = applicability.get("required_condition_expression")
    if expression is not None:
        _validate_condition_expression(expression, "required_condition_expression")
    if not isinstance(package["non_applicable_conditions"], list):
        raise ValueError("non_applicable_conditions must be a list")
    _require_nonblank_string_values(
        package["non_applicable_conditions"], "non_applicable_conditions"
    )
    if not isinstance(package["search_terms"], list) or not all(isinstance(item, str) for item in package["search_terms"]):
        raise ValueError("search_terms must be a list of strings")
    if any(not item.strip() for item in package["search_terms"]):
        raise ValueError("search_terms must not contain blank values")
    if not isinstance(package["evidence_manifest"], list) or not package["evidence_manifest"]:
        raise ValueError("evidence_manifest must contain baseline geometry validation evidence")
    evidence_ids: set[str] = set()
    for evidence in package["evidence_manifest"]:
        if not isinstance(evidence, dict):
            raise ValueError("evidence_manifest must contain objects")
        required_evidence = ("evidence_id", "path", "role", "media_type", "sha256")
        missing_evidence = [field for field in required_evidence if field not in evidence]
        if missing_evidence:
            raise ValueError(
                "evidence_manifest item missing required fields: "
                + ", ".join(missing_evidence)
            )
        evidence_id = _require_string(evidence["evidence_id"], "evidence_id")
        require_safe_id(evidence_id, "evidence_id")
        if evidence_id in evidence_ids:
            raise ValueError(f"evidence_id values must be unique: {evidence_id}")
        evidence_ids.add(evidence_id)
        for field in ("path", "media_type"):
            value = _require_string(evidence[field], f"evidence.{field}")
            if not value.strip():
                raise ValueError(f"evidence.{field} is required")
        role = _require_string(evidence["role"], "evidence.role")
        if role not in EVIDENCE_ROLES:
            raise ValueError(f"unsupported evidence role: {role}")
        _require_sha256(evidence["sha256"], "evidence.sha256")
        if role in EVIDENCE_ROLE_VALIDATION_KINDS:
            for field in ("working_copy_id", "change_set_id", "validation_kind"):
                value = _require_string(evidence.get(field), f"evidence.{field}")
                if not value.strip():
                    raise ValueError(f"evidence.{field} is required for validation evidence")
            _require_sha256(evidence.get("model_sha256"), "evidence.model_sha256")
            if evidence["validation_kind"] != EVIDENCE_ROLE_VALIDATION_KINDS[role]:
                raise ValueError("evidence validation_kind does not match its typed role")
    if "geometry_validation" not in {
        evidence["role"] for evidence in package["evidence_manifest"]
    }:
        raise ValueError("evidence_manifest requires baseline geometry validation evidence")
    assertions = package.get("atomic_assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("atomic_assertions must be nonempty")
    assertion_keys: set[str] = set()
    for assertion in assertions:
        if not isinstance(assertion, dict):
            raise ValueError("atomic_assertions must contain objects")
        required = ("assertion_key", "subject_ref", "predicate", "object_value", "constraint_kind", "evidence_refs")
        missing_assertion = [field for field in required if field not in assertion]
        if missing_assertion:
            raise ValueError(f"atomic_assertion missing required fields: {', '.join(missing_assertion)}")
        assertion_key = _require_string(assertion.get("assertion_key"), "assertion_key")
        require_safe_id(assertion_key, "assertion_key")
        if assertion_key in assertion_keys:
            raise ValueError(f"atomic assertion keys must be unique: {assertion_key}")
        assertion_keys.add(assertion_key)
        subject_ref = _require_string(assertion["subject_ref"], "subject_ref")
        predicate = _require_string(assertion["predicate"], "predicate")
        if not subject_ref.strip() or not predicate.strip():
            raise ValueError("atomic_assertion subject_ref and predicate are required")
        if not isinstance(assertion["evidence_refs"], list) or not assertion["evidence_refs"]:
            raise ValueError("atomic_assertion evidence_refs must be a nonempty list")
        if not all(isinstance(item, str) and item.strip() for item in assertion["evidence_refs"]):
            raise ValueError("atomic_assertion evidence_refs must contain nonblank strings")
        unknown_evidence = sorted(set(assertion["evidence_refs"]) - evidence_ids)
        if unknown_evidence:
            raise ValueError(
                "atomic assertion evidence_refs must reference evidence_id values: "
                + ", ".join(unknown_evidence)
            )
        if "contradicts" in assertion and not isinstance(assertion["contradicts"], list):
            raise ValueError("atomic_assertion contradicts must be a list")
        if "contradicts" in assertion:
            _require_nonblank_string_values(assertion["contradicts"], "contradicts")
        unit = assertion.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise ValueError("atomic_assertion unit must be a string or null")
        confidence = assertion.get("confidence")
        if confidence is not None and (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("atomic_assertion confidence must be between 0 and 1")
        kind = str(assertion.get("constraint_kind", ""))
        if kind not in CONSTRAINT_KINDS:
            raise ValueError(f"unsupported constraint_kind: {kind}")
    return package


def validate_design_lesson_screening_package(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate an immutable reviewed-no-publication screening package."""
    if not isinstance(raw, dict):
        raise ValueError("design lesson screening package must be an object")
    _reject_non_finite_json_numbers(raw)
    package = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    required = {
        "schema_version",
        "screening_id",
        "codex_session_id",
        "source",
        "summary",
        "excluded_candidates",
        "evidence_manifest",
    }
    missing = sorted(required - package.keys())
    if missing:
        raise ValueError(
            "design lesson screening package missing required fields: "
            + ", ".join(missing)
        )
    unsupported = sorted(set(package) - required)
    if unsupported:
        raise ValueError("unsupported top-level fields: " + ", ".join(unsupported))
    if package["schema_version"] != "DesignLessonScreeningPackage/v1":
        raise ValueError("invalid design lesson screening schema_version")
    screening_id = _require_string(package["screening_id"], "screening_id")
    require_safe_id(screening_id, "screening_id")
    for field in ("codex_session_id", "summary"):
        value = _require_string(package[field], field)
        if not value.strip():
            raise ValueError(f"{field} is required")

    source = package["source"]
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    allowed_source = {
        "organization_id",
        "design_group_id",
        "family_id",
        "working_copy_id",
        "change_set_ids",
        "before_model_sha256",
        "after_model_sha256",
    }
    unsupported_source = sorted(set(source) - allowed_source)
    if unsupported_source:
        raise ValueError("unsupported source fields: " + ", ".join(unsupported_source))
    for field in ("organization_id", "design_group_id", "working_copy_id"):
        value = _require_string(source.get(field), f"source.{field}")
        if not value.strip():
            raise ValueError(f"source.{field} is required")
    family_id = source.get("family_id")
    if family_id is not None and (
        not isinstance(family_id, str) or not family_id.strip()
    ):
        raise ValueError("source.family_id must be a nonblank string or null")
    for field in ("before_model_sha256", "after_model_sha256"):
        _require_sha256(source.get(field), f"source.{field}")
    change_set_ids = _require_string_list(
        source,
        "change_set_ids",
        nonempty=True,
        label="source.change_set_ids",
    )
    if len(change_set_ids) != len(set(change_set_ids)):
        raise ValueError("source.change_set_ids must contain unique values")

    evidence_ids = _validate_screening_evidence_manifest(package["evidence_manifest"])
    candidates = package["excluded_candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("excluded_candidates must be a nonempty list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("excluded_candidates must contain objects")
        required_candidate = {"candidate", "reason_code", "rationale", "evidence_refs"}
        if set(candidate) != required_candidate:
            raise ValueError("excluded candidate fields are invalid")
        for field in ("candidate", "rationale"):
            value = _require_string(candidate[field], field)
            if not value.strip():
                raise ValueError(f"excluded candidate {field} is required")
        if candidate["reason_code"] not in SCREENING_EXCLUSION_REASONS:
            raise ValueError(
                f"unsupported screening reason_code: {candidate['reason_code']}"
            )
        refs = candidate["evidence_refs"]
        if not isinstance(refs, list) or not refs or not all(
            isinstance(value, str) and value.strip() for value in refs
        ):
            raise ValueError("excluded candidate evidence_refs must be nonempty strings")
        if len(refs) != len(set(refs)):
            raise ValueError("excluded candidate evidence_refs must be unique")
        unknown = sorted(set(refs) - evidence_ids)
        if unknown:
            raise ValueError(
                "excluded candidate evidence_refs must reference evidence_id values: "
                + ", ".join(unknown)
            )
    return package


def validate_design_lesson_review_package(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("design lesson review package must be an object")
    schema_version = raw.get("schema_version")
    if schema_version == "DesignLessonPackage/v1":
        return validate_design_lesson_package(raw)
    if schema_version == "DesignLessonScreeningPackage/v1":
        return validate_design_lesson_screening_package(raw)
    raise ValueError("unsupported design lesson review package schema_version")


def _validate_screening_evidence_manifest(raw: Any) -> set[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("evidence_manifest must contain baseline geometry validation evidence")
    evidence_ids: set[str] = set()
    roles: set[str] = set()
    for evidence in raw:
        if not isinstance(evidence, dict):
            raise ValueError("evidence_manifest must contain objects")
        for field in ("evidence_id", "path", "role", "media_type", "sha256"):
            if field not in evidence:
                raise ValueError(f"evidence_manifest item missing required field: {field}")
        evidence_id = _require_string(evidence["evidence_id"], "evidence_id")
        require_safe_id(evidence_id, "evidence_id")
        if evidence_id in evidence_ids:
            raise ValueError(f"evidence_id values must be unique: {evidence_id}")
        evidence_ids.add(evidence_id)
        for field in ("path", "media_type"):
            value = _require_string(evidence[field], f"evidence.{field}")
            if not value.strip():
                raise ValueError(f"evidence.{field} is required")
        role = _require_string(evidence["role"], "evidence.role")
        if role not in EVIDENCE_ROLES:
            raise ValueError(f"unsupported evidence role: {role}")
        roles.add(role)
        _require_sha256(evidence["sha256"], "evidence.sha256")
        if role in EVIDENCE_ROLE_VALIDATION_KINDS:
            for field in ("working_copy_id", "change_set_id", "validation_kind"):
                value = _require_string(evidence.get(field), f"evidence.{field}")
                if not value.strip():
                    raise ValueError(f"evidence.{field} is required for validation evidence")
            _require_sha256(evidence.get("model_sha256"), "evidence.model_sha256")
            if evidence["validation_kind"] != EVIDENCE_ROLE_VALIDATION_KINDS[role]:
                raise ValueError("evidence validation_kind does not match its typed role")
    if "geometry_validation" not in roles:
        raise ValueError("evidence_manifest requires baseline geometry validation evidence")
    return evidence_ids


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
        raise ValueError(f"{label} must be a full SHA-256 digest")
    return digest.lower()


def _require_nonblank_string_values(values: Any, label: str) -> list[str]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) and item.strip() for item in values
    ):
        raise ValueError(f"{label} must contain nonblank strings")
    return values


def _require_string_list(
    container: dict[str, Any],
    field: str,
    *,
    nonempty: bool = False,
    label: str | None = None,
) -> list[str]:
    resolved_label = label or field
    values = container.get(field)
    if not isinstance(values, list):
        raise ValueError(f"{resolved_label} must be a list")
    if nonempty and not values:
        raise ValueError(f"{resolved_label} must be nonempty")
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ValueError(f"{resolved_label} must contain nonblank strings")
    return values


def _validate_condition_expression(expression: Any, label: str) -> None:
    if isinstance(expression, str):
        if not expression.strip():
            raise ValueError(f"{label} contains a blank condition")
        return
    if not isinstance(expression, dict) or len(expression) != 1:
        raise ValueError(f"{label} must contain exactly one logical operator")
    operator, operands = next(iter(expression.items()))
    if operator not in {"all_of", "any_of"}:
        raise ValueError(f"{label} contains an unsupported logical operator")
    if not isinstance(operands, list) or not operands:
        raise ValueError(f"{label}.{operator} must be a nonempty list")
    for operand in operands:
        _validate_condition_expression(operand, label)


def condition_expression_satisfied(expression: Any, declared_conditions: set[str]) -> bool:
    """Evaluate the package's explicit deterministic all-of/any-of grammar."""
    if expression is None:
        return True
    if isinstance(expression, str):
        return expression in declared_conditions
    operator, operands = next(iter(expression.items()))
    evaluations = [
        condition_expression_satisfied(operand, declared_conditions)
        for operand in operands
    ]
    return all(evaluations) if operator == "all_of" else any(evaluations)


def satisfying_conditions(required_conditions: list[str], expression: Any) -> list[str]:
    """Derive a deterministic set of declared conditions that satisfies an expression."""
    def expression_witness(operand: Any) -> set[str]:
        if isinstance(operand, str):
            return {operand}
        operator, operands = next(iter(operand.items()))
        if operator == "all_of":
            return set().union(*(expression_witness(item) for item in operands))
        return expression_witness(operands[0])

    conditions = set(required_conditions)
    if expression is not None:
        conditions.update(expression_witness(expression))
    result = sorted(conditions)
    assert condition_expression_satisfied(expression, set(result))
    return result


def _reject_non_finite_json_numbers(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite_json_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite_json_numbers(item)


class DesignLessonStagingStore:
    """Filesystem-only review queue for immutable design-lesson packages."""

    def __init__(
        self,
        workspace: Path,
        *,
        staging_parts: tuple[str, ...] = _STAGING_PARTS,
    ) -> None:
        candidate = Path(workspace)
        if candidate.is_symlink():
            raise ValueError("workspace must not be a symlink")
        if not candidate.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.workspace = validate_managed_path(
            candidate,
            allow_missing_leaf=False,
        ).path
        if not staging_parts or any(
            not isinstance(part, str)
            or not part
            or part in {".", ".."}
            or "/" in part
            or "\\" in part
            for part in staging_parts
        ):
            raise ValueError("staging_parts must contain safe path components")
        self._staging_parts = tuple(staging_parts)

    @property
    def staging_root(self) -> Path:
        current = self.workspace
        for part in self._staging_parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("staging path must not be a symlink")
            current = ensure_managed_directory(
                current,
                parents=False,
                exist_ok=True,
            ).path
            if not current.is_dir():
                raise ValueError("staging path must be a directory")
        return current

    def stage(self, package: dict[str, Any], evidence_items: list[dict[str, str]]) -> dict[str, Any]:
        return self._stage(package, evidence_items, review_keyed=False)

    def stage_review(
        self, package: dict[str, Any], evidence_items: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Stage an immutable review revision by canonical package digest."""
        return self._stage(package, evidence_items, review_keyed=True)

    def _stage(
        self,
        package: dict[str, Any],
        evidence_items: list[dict[str, str]],
        *,
        review_keyed: bool,
    ) -> dict[str, Any]:
        if not isinstance(package, dict):
            raise ValueError("design lesson package must be an object")
        _reject_non_finite_json_numbers(package)
        staged_package = json.loads(
            json.dumps(package, ensure_ascii=False, allow_nan=False)
        )
        manifest = self._evidence_manifest(evidence_items)
        staged_package["evidence_manifest"] = manifest
        staged_package = (
            validate_design_lesson_review_package(staged_package)
            if review_keyed
            else validate_design_lesson_package(staged_package)
        )
        if staged_package["schema_version"] == "DesignLessonPackage/v1":
            evidence_ids = {item["evidence_id"] for item in manifest}
            for assertion in staged_package["atomic_assertions"]:
                unknown = sorted(set(assertion["evidence_refs"]) - evidence_ids)
                if unknown:
                    raise ValueError(
                        "atomic assertion evidence_refs must reference evidence_id values: "
                        + ", ".join(unknown)
                    )
            lesson_id: str | None = staged_package["lesson_id"]
            subject_id = lesson_id
            review_outcome = "publish"
        else:
            lesson_id = None
            subject_id = staged_package["screening_id"]
            review_outcome = "no_publish"
        package_bytes = canonical_json(staged_package).encode("utf-8")
        package_sha256 = hashlib.sha256(package_bytes).hexdigest()
        storage_key = f"review-{package_sha256}" if review_keyed else subject_id
        staging_root = self.staging_root
        lesson_dir = staging_root / storage_key
        if lesson_dir.exists() or lesson_dir.is_symlink():
            if review_keyed:
                raise ValueError(
                    f"design lesson review package is already staged: {package_sha256}"
                )
            raise ValueError(f"design lesson is already staged: {lesson_id}")
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{storage_key}.", dir=staging_root)
        )
        try:
            self._atomic_write(temporary_dir / "lesson.json", package_bytes)
            self._atomic_write(
                temporary_dir / "lesson.md",
                self._render_markdown(staged_package, package_sha256).encode("utf-8"),
            )
            self._atomic_write(
                temporary_dir / "evidence-manifest.json",
                canonical_json(manifest).encode("utf-8"),
            )
            try:
                atomic_publish_directory(temporary_dir, lesson_dir)
            except (FileExistsError, SecureFilesystemError, OSError):
                if lesson_dir.exists() or lesson_dir.is_symlink():
                    if review_keyed:
                        raise ValueError(
                            f"design lesson review package is already staged: {package_sha256}"
                        ) from None
                    raise ValueError(
                        f"design lesson is already staged: {lesson_id}"
                    ) from None
                raise
        finally:
            if temporary_dir.exists():
                remove_owned_tree(
                    temporary_dir,
                    expected_parent=staging_root,
                    label="design lesson staging attempt",
                )
        lesson_json_path = lesson_dir / "lesson.json"
        lesson_markdown_path = lesson_dir / "lesson.md"
        evidence_manifest_path = lesson_dir / "evidence-manifest.json"
        result = {
            "status": "staged-local-only",
            "lesson_id": lesson_id,
            "review_subject_id": subject_id,
            "review_outcome": review_outcome,
            "package_sha256": package_sha256,
            "manifest_sha256": stable_hash(manifest),
            "lesson_json_path": str(lesson_json_path),
            "lesson_markdown_path": str(lesson_markdown_path),
            "evidence_manifest_path": str(evidence_manifest_path),
        }
        if review_keyed:
            result["storage_key"] = storage_key
        return result

    def get(self, lesson_id: str) -> dict[str, Any]:
        require_safe_id(lesson_id, "lesson_id")
        return self._get(lesson_id, expected_lesson_id=lesson_id)

    def get_review(self, package_sha256: str) -> dict[str, Any]:
        _require_sha256(package_sha256, "review package SHA-256")
        expected_sha256 = package_sha256.lower()
        inspection = self._get(
            f"review-{expected_sha256}", expected_lesson_id=None
        )
        if inspection.get("package_sha256") != expected_sha256:
            inspection["status"] = "integrity-drift"
            inspection["file_integrity"]["lesson_json"]["status"] = (
                "storage-key-sha256-mismatch"
            )
        return inspection

    def review_package_paths(self, package_sha256: str) -> dict[str, Path]:
        inspection = self.get_review(package_sha256)
        if inspection["status"] != "verified-local-only":
            raise ValueError("review package integrity drift")
        paths = {
            name: Path(path)
            for name, path in inspection["paths"].items()
        }
        for name, path in paths.items():
            self._assert_regular_workspace_file(path, name.replace("_", " "))
        return paths

    def review_evidence_paths(
        self, package_sha256: str
    ) -> list[tuple[dict[str, Any], Path]]:
        inspection = self.get_review(package_sha256)
        if inspection["status"] != "verified-local-only":
            raise ValueError("review package integrity drift")
        return [
            (evidence, self._resolve_evidence_path(evidence["path"]))
            for evidence in inspection["package"]["evidence_manifest"]
        ]

    def discard_review_attempt(self, package_sha256: str) -> None:
        inspection = self.get_review(package_sha256)
        if (
            inspection.get("package_sha256") != package_sha256.lower()
            or any(
                item.get("status") != "verified"
                for item in inspection.get("file_integrity", {}).values()
            )
        ):
            raise ValueError("cannot discard a review package with unverified package files")
        review_dir = Path(inspection["paths"]["lesson_json"]).parent
        if review_dir.is_symlink() or review_dir.parent != self.staging_root:
            raise ValueError("review package path is unsafe")
        remove_owned_tree(
            review_dir,
            expected_parent=self.staging_root,
            label="review package",
        )

    def discard_review_attempt_owned(self, package_sha256: str) -> None:
        """Remove only the direct digest directory known to belong to this attempt."""
        _require_sha256(package_sha256, "review package SHA-256")
        child_name = f"review-{package_sha256.lower()}"
        remove_owned_tree(
            self.staging_root / child_name,
            expected_parent=self.staging_root,
            label="review package attempt",
        )

    def _get(
        self, storage_key: str, *, expected_lesson_id: str | None
    ) -> dict[str, Any]:
        require_safe_id(storage_key, "storage_key")
        paths = self._package_path_candidates(storage_key)
        if not paths["lesson_json"].exists():
            file_integrity: dict[str, dict[str, Any]] = {}
            for name, path in paths.items():
                actual_sha256 = None
                status = "missing"
                if path.exists():
                    self._assert_regular_workspace_file(path, name.replace("_", " "))
                    actual_sha256 = file_sha256(path)
                    status = "unverifiable"
                file_integrity[name] = {
                    "path": str(path),
                    "actual_sha256": actual_sha256,
                    "status": status,
                }
            result = {
                "status": "integrity-drift",
                "lesson_id": expected_lesson_id or storage_key,
                "package": None,
                "package_sha256": None,
                "paths": {name: str(path) for name, path in paths.items()},
                "file_integrity": file_integrity,
                "evidence_integrity": [],
            }
            if expected_lesson_id is None:
                result["storage_key"] = storage_key
            return result
        self._assert_regular_workspace_file(paths["lesson_json"], "lesson package")
        package_bytes = paths["lesson_json"].read_bytes()
        actual_package_sha256 = hashlib.sha256(package_bytes).hexdigest()
        package: dict[str, Any] | None = None
        package_status = "verified"
        try:
            parsed = json.loads(
                package_bytes.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
            package = (
                validate_design_lesson_review_package(parsed)
                if expected_lesson_id is None
                else validate_design_lesson_package(parsed)
            )
            if (
                expected_lesson_id is not None
                and package["lesson_id"] != expected_lesson_id
            ):
                package_status = "lesson-id-mismatch"
            elif canonical_json(package).encode("utf-8") != package_bytes:
                package_status = "noncanonical"
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            package_status = "invalid"

        file_integrity: dict[str, dict[str, Any]] = {
            "lesson_json": {
                "path": str(paths["lesson_json"]),
                "actual_sha256": actual_package_sha256,
                "status": package_status,
            }
        }
        evidence_integrity: list[dict[str, Any]] = []
        if package is None:
            for name in ("lesson_markdown", "evidence_manifest"):
                if not paths[name].exists():
                    file_integrity[name] = {
                        "path": str(paths[name]),
                        "actual_sha256": None,
                        "status": "missing",
                    }
                    continue
                self._assert_regular_workspace_file(paths[name], name.replace("_", " "))
                file_integrity[name] = {
                    "path": str(paths[name]),
                    "actual_sha256": file_sha256(paths[name]),
                    "status": "unverifiable",
                }
        else:
            expected_files = {
                "lesson_markdown": self._render_markdown(
                    package, actual_package_sha256
                ).encode("utf-8"),
                "evidence_manifest": canonical_json(
                    package["evidence_manifest"]
                ).encode("utf-8"),
            }
            for name, expected_bytes in expected_files.items():
                if not paths[name].exists():
                    file_integrity[name] = {
                        "path": str(paths[name]),
                        "expected_sha256": hashlib.sha256(expected_bytes).hexdigest(),
                        "actual_sha256": None,
                        "status": "missing",
                    }
                    continue
                self._assert_regular_workspace_file(paths[name], name.replace("_", " "))
                actual_bytes = paths[name].read_bytes()
                expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
                actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()
                file_integrity[name] = {
                    "path": str(paths[name]),
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                    "status": "verified" if actual_bytes == expected_bytes else "sha256-mismatch",
                }
            for evidence in package["evidence_manifest"]:
                status = "verified"
                actual_sha256: str | None = None
                try:
                    evidence_path = self._resolve_evidence_path(evidence["path"])
                    actual_sha256 = file_sha256(evidence_path)
                    if actual_sha256 != evidence["sha256"]:
                        status = "sha256-mismatch"
                except ValueError:
                    status = "missing-or-unsafe"
                evidence_integrity.append({
                    "evidence_id": evidence["evidence_id"],
                    "path": evidence["path"],
                    "role": evidence["role"],
                    "expected_sha256": evidence["sha256"],
                    "actual_sha256": actual_sha256,
                    "status": status,
                })
        verified = all(
            item["status"] == "verified" for item in file_integrity.values()
        ) and all(item["status"] == "verified" for item in evidence_integrity)
        result = {
            "status": "verified-local-only" if verified else "integrity-drift",
            "lesson_id": (
                package.get("lesson_id")
                if package is not None
                else expected_lesson_id
            ),
            "package": package,
            "package_sha256": actual_package_sha256,
            "paths": {name: str(path) for name, path in paths.items()},
            "file_integrity": file_integrity,
            "evidence_integrity": evidence_integrity,
        }
        if expected_lesson_id is None:
            result["storage_key"] = storage_key
        return result

    def package_paths(self, lesson_id: str) -> dict[str, Path]:
        """Return the three immutable staging files after enforcing workspace boundaries."""
        paths = self._package_path_candidates(lesson_id)
        labels = {
            "lesson_json": "lesson package",
            "lesson_markdown": "lesson Markdown",
            "evidence_manifest": "evidence manifest",
        }
        for label, path in paths.items():
            self._assert_regular_workspace_file(path, labels[label])
        return paths

    def _package_path_candidates(self, lesson_id: str) -> dict[str, Path]:
        require_safe_id(lesson_id, "lesson_id")
        lesson_dir = self.staging_root / lesson_id
        if lesson_dir.is_symlink():
            raise ValueError("lesson package path must not be a symlink")
        try:
            resolved_lesson_dir = validate_managed_path(
                lesson_dir,
                allow_missing_leaf=False,
            ).path
            relative_managed_path(resolved_lesson_dir, self.staging_root)
        except (FileNotFoundError, ValueError):
            raise ValueError(f"unknown staged design lesson: {lesson_id}") from None
        if not resolved_lesson_dir.is_dir():
            raise ValueError("staged lesson path must be a directory")
        paths = {
            "lesson_json": resolved_lesson_dir / "lesson.json",
            "lesson_markdown": resolved_lesson_dir / "lesson.md",
            "evidence_manifest": resolved_lesson_dir / "evidence-manifest.json",
        }
        return paths

    def evidence_paths(self, lesson_id: str) -> list[tuple[dict[str, Any], Path]]:
        inspection = self.get(lesson_id)
        package = inspection.get("package")
        if not isinstance(package, dict):
            raise ValueError("lesson package is invalid")
        return [
            (evidence, self._resolve_evidence_path(evidence["path"]))
            for evidence in package["evidence_manifest"]
        ]

    def verify(self, lesson_id: str, expected_package_sha256: str) -> dict[str, Any]:
        require_safe_id(lesson_id, "lesson_id")
        if len(expected_package_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_package_sha256.lower()):
            raise ValueError("expected package SHA-256 is invalid")
        inspection = self.get(lesson_id)
        actual_package_sha256 = inspection["package_sha256"]
        if actual_package_sha256 != expected_package_sha256:
            raise ValueError("package SHA-256 changed after review")
        package = inspection["package"]
        if package is None:
            raise ValueError("lesson package is invalid")
        if package["lesson_id"] != lesson_id:
            raise ValueError("lesson_id does not match requested lesson")
        if inspection["file_integrity"]["lesson_json"]["status"] != "verified":
            raise ValueError("lesson package changed after staging")
        if inspection["file_integrity"]["lesson_markdown"]["status"] != "verified":
            raise ValueError("lesson Markdown changed after staging")
        if inspection["file_integrity"]["evidence_manifest"]["status"] != "verified":
            raise ValueError("evidence manifest changed after staging")
        for evidence in inspection["evidence_integrity"]:
            if evidence["status"] != "verified":
                raise ValueError(
                    f"evidence SHA-256 changed after staging: {evidence['path']}"
                )
        return {
            "status": "verified-local-only",
            "lesson_id": lesson_id,
            "package_sha256": actual_package_sha256,
            "evidence_count": len(package["evidence_manifest"]),
            "file_integrity": inspection["file_integrity"],
            "evidence_integrity": inspection["evidence_integrity"],
        }

    def _evidence_manifest(self, evidence_items: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(evidence_items, list):
            raise ValueError("evidence_items must be a list")
        manifest: list[dict[str, str]] = []
        evidence_ids: set[str] = set()
        for item in evidence_items:
            if not isinstance(item, dict):
                raise ValueError("evidence item must be an object")
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str):
                raise ValueError("evidence_id must be a string")
            require_safe_id(evidence_id, "evidence_id")
            if evidence_id in evidence_ids:
                raise ValueError(f"evidence_id values must be unique: {evidence_id}")
            evidence_ids.add(evidence_id)
            relative_path = item.get("path")
            role = item.get("role")
            media_type = item.get("media_type")
            if not isinstance(relative_path, str):
                raise ValueError("evidence path must be a string")
            if not isinstance(role, str) or role not in EVIDENCE_ROLES:
                raise ValueError(f"unsupported evidence role: {role}")
            if not isinstance(media_type, str) or not media_type.strip():
                raise ValueError("evidence media_type is required")
            path = self._resolve_evidence_path(relative_path)
            descriptor = {
                "evidence_id": evidence_id,
                "path": path.relative_to(self.workspace).as_posix(),
                "role": role,
                "media_type": media_type,
                "sha256": file_sha256(path),
            }
            if role in EVIDENCE_ROLE_VALIDATION_KINDS:
                for field in ("working_copy_id", "change_set_id", "model_sha256", "validation_kind"):
                    value = item.get(field)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(
                            f"evidence {field} is required for validation evidence"
                        )
                    descriptor[field] = value
                _require_sha256(descriptor["model_sha256"], "evidence.model_sha256")
                if descriptor["validation_kind"] != EVIDENCE_ROLE_VALIDATION_KINDS[role]:
                    raise ValueError("evidence validation_kind does not match its typed role")
            manifest.append(descriptor)
        return sorted(manifest, key=lambda item: item["evidence_id"])

    def _resolve_evidence_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if not relative_path or candidate.is_absolute():
            raise ValueError("evidence path escapes workspace")
        path = self.workspace / candidate
        current = self.workspace
        for part in candidate.parts:
            if part in {"", "."}:
                continue
            current = current / part
            if current.is_symlink():
                raise ValueError("evidence path must not be a symlink")
        try:
            resolved = validate_managed_path(
                path,
                allow_missing_leaf=False,
            ).path
            relative_managed_path(resolved, self.workspace)
        except (FileNotFoundError, ValueError):
            raise ValueError("evidence path escapes workspace") from None
        self._assert_regular_workspace_file(resolved, "evidence")
        return resolved

    def _assert_regular_workspace_file(self, path: Path, label: str) -> None:
        if path.is_symlink():
            raise ValueError(f"{label} path must not be a symlink")
        try:
            relative_path = path.relative_to(self.workspace)
        except ValueError:
            try:
                relative_path = relative_managed_path(path, self.workspace)
            except ValueError:
                raise ValueError(f"{label} path escapes workspace") from None
        current = self.workspace
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"{label} path must not be a symlink")
        try:
            resolved = validate_managed_path(
                path,
                allow_missing_leaf=False,
            ).path
            relative_managed_path(resolved, self.workspace)
        except (FileNotFoundError, ValueError):
            raise ValueError(f"{label} path escapes workspace") from None
        if not resolved.is_file():
            raise ValueError(f"{label} must be a regular file")

    @staticmethod
    def _atomic_write(path: Path, contents: bytes) -> None:
        atomic_publish_new(path, contents)

    @staticmethod
    def _render_markdown(package: dict[str, Any], package_sha256: str) -> str:
        def text(value: Any) -> str:
            escaped = html.escape(str(value), quote=False)
            for character in "\\`*[]#+|":
                escaped = escaped.replace(character, f"\\{character}")
            return escaped.replace("\r", "").replace("\n", "<br>")

        def json_text(value: Any) -> str:
            return text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))

        def append_values(lines: list[str], values: list[Any]) -> None:
            if values:
                lines.extend(f"- {text(item)}" for item in values)
            else:
                lines.append("- _None_")

        if package.get("schema_version") == "DesignLessonScreeningPackage/v1":
            lines = [
                "# Design Lesson screening: no publishable lesson",
                "",
                f"- Screening ID: `{text(package['screening_id'])}`",
                f"- Package SHA-256: `{package_sha256}`",
                f"- Codex session: `{text(package['codex_session_id'])}`",
                "",
                "## Source",
                "",
            ]
            for field, value in sorted(package["source"].items()):
                lines.append(f"- {text(field)}: {json_text(value)}")
            lines.extend(["", "## Screening summary", "", text(package["summary"]), ""])
            lines.extend(["## Excluded candidates", ""])
            for item in package["excluded_candidates"]:
                lines.extend(
                    [
                        f"### {text(item['candidate'])}",
                        "",
                        f"- Reason: `{text(item['reason_code'])}`",
                        f"- Rationale: {text(item['rationale'])}",
                        f"- Evidence refs: {json_text(item['evidence_refs'])}",
                        "",
                    ]
                )
            lines.extend(["## Evidence", ""])
            for item in package["evidence_manifest"]:
                lines.append(
                    f"- `{text(item['evidence_id'])}`: `{text(item['path'])}` "
                    f"({text(item['role'])}, {text(item['media_type'])}): `{item['sha256']}`"
                )
            canonical_package = html.escape(
                json.dumps(
                    package,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                ),
                quote=True,
            )
            lines.extend(["", "## Canonical full package", "", "<pre>", canonical_package, "</pre>", ""])
            return "\n".join(lines)

        problem = package["problem"]
        lines = [
            f"# {text(package['title'])}",
            "",
            f"- Lesson ID: `{text(package['lesson_id'])}`",
            f"- Package SHA-256: `{package_sha256}`",
            f"- Codex session: `{text(package['codex_session_id'])}`",
            "",
            "## Source",
            "",
        ]
        for field, value in sorted(package["source"].items()):
            lines.append(f"- {text(field)}: {json_text(value)}")
        lines.extend([
            "",
            "## Problem",
            "",
            f"- Summary: {text(problem.get('summary', ''))}",
            f"- Discovery stage: {text(problem['discovery_stage'])}",
            f"- Severity: {text(problem['severity'])}",
            f"- Symptoms: {json_text(problem['symptoms'])}",
            f"- Affected components: {json_text(problem['affected_components'])}",
            f"- Affected interfaces: {json_text(problem['affected_interfaces'])}",
            f"- Failure modes: {json_text(problem['failure_modes'])}",
            "",
            "## Root causes",
            "",
        ])
        append_values(lines, package["root_causes"])
        lines.extend(["", "## Corrections", ""])
        append_values(lines, package["corrections"])
        prevention = package["prevention"]
        lines.extend(["", "## Prevention", "", "### Required checks", ""])
        append_values(lines, prevention["required_checks"])
        lines.extend(["", "### Design review questions", ""])
        append_values(lines, prevention["design_review_questions"])
        lines.extend([
            "",
            f"- Workflow gate: {text(prevention['workflow_gate'])}",
            f"- Detection method: {text(prevention['detection_method'])}",
            "",
            "## Applicability",
            "",
        ])
        applicability = package["applicability"]
        for label, field in (
            ("Component classes", "component_classes"),
            ("Interface types", "interface_types"),
            ("Design stages", "design_stages"),
            ("Required conditions", "required_conditions"),
        ):
            lines.append(f"- {label}: {json_text(applicability[field])}")
        if "required_condition_expression" in applicability:
            lines.append(
                "- Required condition expression: "
                + json_text(applicability["required_condition_expression"])
            )
        known_applicability_fields = {
            "component_classes",
            "interface_types",
            "design_stages",
            "required_conditions",
            "required_condition_expression",
        }
        for field in sorted(set(applicability) - known_applicability_fields):
            lines.append(f"- {text(field)}: {json_text(applicability[field])}")
        lines.extend(["", "## Non-applicable conditions", ""])
        append_values(lines, package["non_applicable_conditions"])
        lines.extend(["", "## Search terms", ""])
        append_values(lines, package["search_terms"])
        lines.extend(["", "## Atomic assertions", ""])
        for assertion in package["atomic_assertions"]:
            lines.extend([
                f"### {text(assertion['assertion_key'])}",
                "",
                f"- Subject: {text(assertion['subject_ref'])}",
                f"- Predicate: {text(assertion['predicate'])}",
                f"- Object value: {json_text(assertion['object_value'])}",
                f"- Unit: {text(assertion.get('unit') or '')}",
                f"- Constraint kind: `{text(assertion['constraint_kind'])}`",
                f"- Confidence: {json_text(assertion.get('confidence'))}",
                f"- Evidence refs: {json_text(assertion['evidence_refs'])}",
                f"- Contradicts: {json_text(assertion.get('contradicts', []))}",
                "",
            ])
        lines.extend(["", "## Evidence", ""])
        for item in package["evidence_manifest"]:
            lines.extend([
                f"- `{text(item['evidence_id'])}`: `{text(item['path'])}` "
                f"({text(item['role'])}, {text(item['media_type'])}): `{item['sha256']}`",
                f"  - Complete descriptor: {json_text(item)}",
            ])
        canonical_package = html.escape(
            json.dumps(
                package,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ),
            quote=True,
        )
        lines.extend([
            "",
            "## Canonical full package",
            "",
            "<pre>",
            canonical_package,
            "</pre>",
        ])
        return "\n".join(lines) + "\n"


def match_design_lesson(lesson: dict[str, Any], design_features: dict[str, Any], explicit_query: str) -> dict[str, Any]:
    applicability = lesson["applicability"]
    declared_conditions = {
        str(condition)
        for field in ("satisfied_conditions", "declared_conditions")
        for condition in design_features.get(field, [])
        if isinstance(condition, str)
    }
    unmet = [
        condition
        for condition in applicability.get("required_conditions", [])
        if condition not in declared_conditions
    ]
    expression_satisfied = condition_expression_satisfied(
        applicability.get("required_condition_expression"), declared_conditions
    )
    matched_non_applicable = sorted(
        set(lesson.get("non_applicable_conditions", [])) & declared_conditions
    )
    matched = {
        "component_classes": sorted(set(applicability.get("component_classes", [])) & set(design_features.get("component_classes", []))),
        "interface_types": sorted(set(applicability.get("interface_types", [])) & set(design_features.get("interface_types", []))),
        "design_stages": sorted(set(applicability.get("design_stages", [])) & set(design_features.get("design_stages", []))),
        "failure_modes": sorted(set(lesson["problem"].get("failure_modes", [])) & set(design_features.get("failure_modes", []))),
    }
    normalized_query = explicit_query.strip()
    exact_query = bool(normalized_query) and normalized_query.lower() in {
        item.strip().lower() for item in lesson.get("search_terms", []) if isinstance(item, str) and item.strip()
    }
    dimensions = sum(bool(matched[key]) for key in ("component_classes", "interface_types", "design_stages"))
    eligible = (
        not unmet
        and expression_satisfied
        and not matched_non_applicable
        and (bool(matched["failure_modes"]) or dimensions >= 2 or exact_query)
    )
    exclusion_reasons = []
    if unmet:
        exclusion_reasons.append("unmet required conditions")
    elif not expression_satisfied:
        exclusion_reasons.append("unmet required condition expression")
    if matched_non_applicable:
        exclusion_reasons.append("matched non-applicable conditions")
    if not unmet and expression_satisfied and not matched_non_applicable and not eligible:
        exclusion_reasons.append("insufficient structured applicability match")
    return {
        "eligible": eligible,
        "matched_features": matched,
        "matched_dimension_count": dimensions,
        "unmet_conditions": unmet,
        "required_condition_expression_satisfied": expression_satisfied,
        "matched_non_applicable_conditions": matched_non_applicable,
        "exact_query": exact_query,
        "exclusion_reasons": exclusion_reasons,
    }
