from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .design_lessons import validate_design_lesson_package
from .hashing import file_sha256
from .models import canonical_json, require_safe_id
from .secure_fs import (
    atomic_publish_directory,
    atomic_publish_new,
    ensure_managed_directory,
    remove_owned_tree,
    relative_managed_path,
    validate_managed_path,
)


_REVIEW_PARTS = ("output", "mechanical_design", "lesson_reviews")
_REVIEW_FIELDS = {
    "schema_version",
    "review_id",
    "lesson_id",
    "package_sha256",
    "working_copy_id",
    "final_model_sha256",
    "review_card_sha256",
    "supersedes_review_id",
    "status",
}
_SHA256_TOKEN = re.compile(r"(?i)[0-9a-f]{64}")


def _redact_human_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SHA256_TOKEN.sub("[sha256-redacted]", value)
    if isinstance(value, list):
        return [_redact_human_value(item) for item in value]
    if isinstance(value, dict):
        return {
            (
                _redact_human_value(key) if isinstance(key, str) else key
            ): _redact_human_value(item)
            for key, item in value.items()
        }
    return value


class DesignLessonReviewStore:
    """Filesystem-only immutable engineer-review cards for staged design lessons."""

    def __init__(self, workspace: Path) -> None:
        candidate = Path(workspace)
        if candidate.is_symlink():
            raise ValueError("workspace must not be a symlink")
        if not candidate.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.workspace = validate_managed_path(
            candidate,
            allow_missing_leaf=False,
        ).path

    @property
    def review_root(self) -> Path:
        current = self.workspace
        for part in _REVIEW_PARTS:
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("review path must not be a symlink")
            current = ensure_managed_directory(
                current,
                parents=False,
                exist_ok=True,
            ).path
            if not current.is_dir():
                raise ValueError("review path must be a directory")
        return current

    def prepare(
        self,
        review_id: str,
        staged_inspection: dict[str, Any],
        supersedes_review_id: str | None = None,
        *,
        evidence_summary: list[dict[str, Any]] | None = None,
        validation_summary: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        require_safe_id(review_id, "review_id")
        if supersedes_review_id is not None:
            require_safe_id(supersedes_review_id, "supersedes_review_id")
        package, package_sha256 = self._verified_staged_package(staged_inspection)
        review_root = self.review_root
        review_dir = review_root / review_id
        if review_dir.exists() or review_dir.is_symlink():
            raise ValueError(f"design lesson review is already prepared: {review_id}")

        review_markdown = self._render_markdown(
            review_id, package, evidence_summary, validation_summary
        )
        review = {
            "schema_version": "DesignLessonReview/v1",
            "review_id": review_id,
            "lesson_id": package["lesson_id"],
            "package_sha256": package_sha256,
            "working_copy_id": package["source"]["working_copy_id"],
            "final_model_sha256": package["source"]["after_model_sha256"],
            "review_card_sha256": hashlib.sha256(
                review_markdown.encode("utf-8")
            ).hexdigest(),
            "supersedes_review_id": supersedes_review_id,
            "status": "awaiting-engineer-review",
        }
        review_bytes = canonical_json(review).encode("utf-8")
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{review_id}.", dir=review_root))
        try:
            self._atomic_write(temporary_dir / "review.json", review_bytes)
            self._atomic_write(
                temporary_dir / "review.md", review_markdown.encode("utf-8")
            )
            atomic_publish_directory(temporary_dir, review_dir)
        finally:
            if temporary_dir.exists():
                remove_owned_tree(
                    temporary_dir,
                    expected_parent=review_root,
                    label="design lesson review attempt",
                )
        return {
            **review,
            "confirmation": f"批准设计经验 {review_id}",
            "review_card": self._review_card(
                package, evidence_summary, validation_summary
            ),
            "review_card_markdown": review_markdown,
            "review_json_path": str(review_dir / "review.json"),
            "review_card_path": str(review_dir / "review.md"),
        }

    def inspect(self, review_id: str) -> dict[str, Any]:
        require_safe_id(review_id, "review_id")
        paths = self._path_candidates(review_id)
        review: dict[str, Any] | None = None
        review_json_status = "missing"
        review_json_bytes: bytes | None = None
        if paths["review_json"].is_symlink():
            self._assert_regular_workspace_file(paths["review_json"], "review record")
        if paths["review_json"].exists():
            self._assert_regular_workspace_file(paths["review_json"], "review record")
            review_json_bytes = paths["review_json"].read_bytes()
            try:
                parsed = json.loads(review_json_bytes.decode("utf-8"))
                review = self._validate_review(parsed)
                if review["review_id"] != review_id:
                    review_json_status = "review-id-mismatch"
                elif canonical_json(review).encode("utf-8") != review_json_bytes:
                    review_json_status = "noncanonical"
                else:
                    review_json_status = "verified"
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                review_json_status = "invalid"

        file_integrity: dict[str, dict[str, Any]] = {
            "review_json": {
                "path": str(paths["review_json"]),
                "actual_sha256": (
                    hashlib.sha256(review_json_bytes).hexdigest()
                    if review_json_bytes is not None
                    else None
                ),
                "status": review_json_status,
            }
        }
        card_path = paths["review_card"]
        if card_path.is_symlink():
            self._assert_regular_workspace_file(card_path, "review card")
        if not card_path.exists():
            file_integrity["review_card"] = {
                "path": str(card_path),
                "actual_sha256": None,
                "status": "missing",
            }
        else:
            self._assert_regular_workspace_file(card_path, "review card")
            actual_sha256 = file_sha256(card_path)
            expected_sha256 = review["review_card_sha256"] if review else None
            file_integrity["review_card"] = {
                "path": str(card_path),
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "status": (
                    "verified"
                    if expected_sha256 is not None and actual_sha256 == expected_sha256
                    else "sha256-mismatch" if expected_sha256 is not None else "unverifiable"
                ),
            }
        verified = all(item["status"] == "verified" for item in file_integrity.values())
        return {
            "status": "verified-local-only" if verified else "integrity-drift",
            "review_id": review_id,
            "review": review,
            "paths": {name: str(path) for name, path in paths.items()},
            "file_integrity": file_integrity,
        }

    def verify(self, review_id: str, package_sha256: str) -> dict[str, Any]:
        require_safe_id(review_id, "review_id")
        self._require_sha256(package_sha256, "expected package SHA-256")
        inspection = self.inspect(review_id)
        review = inspection["review"]
        if review is None:
            raise ValueError("review record is invalid")
        if review["review_id"] != review_id:
            raise ValueError("review_id does not match requested review")
        if review["package_sha256"] != package_sha256:
            raise ValueError("package SHA-256 changed after review preparation")
        if inspection["file_integrity"]["review_json"]["status"] != "verified":
            raise ValueError("review record changed after preparation")
        if inspection["file_integrity"]["review_card"]["status"] != "verified":
            raise ValueError("review card changed after preparation")
        return {
            "status": "verified-local-only",
            "review_id": review_id,
            "lesson_id": review["lesson_id"],
            "package_sha256": package_sha256,
            "review_card_sha256": review["review_card_sha256"],
            "file_integrity": inspection["file_integrity"],
        }

    def _verified_staged_package(
        self, staged_inspection: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(staged_inspection, dict):
            raise ValueError("staged inspection must be an object")
        if staged_inspection.get("status") != "verified-local-only":
            raise ValueError("staged design lesson must be verified")
        package = staged_inspection.get("package")
        package_sha256 = staged_inspection.get("package_sha256")
        if not isinstance(package, dict):
            raise ValueError("staged inspection package is invalid")
        if not isinstance(package_sha256, str):
            raise ValueError("staged inspection package SHA-256 is invalid")
        self._require_sha256(package_sha256, "staged inspection package SHA-256")
        package = validate_design_lesson_package(package)
        actual_sha256 = hashlib.sha256(canonical_json(package).encode("utf-8")).hexdigest()
        if actual_sha256 != package_sha256:
            raise ValueError("staged inspection package SHA-256 is invalid")
        return package, package_sha256

    def _path_candidates(self, review_id: str) -> dict[str, Path]:
        review_dir = self.review_root / review_id
        if review_dir.is_symlink():
            raise ValueError("review path must not be a symlink")
        try:
            resolved_review_dir = review_dir.resolve(strict=True)
            resolved_review_dir.relative_to(self.review_root)
        except (FileNotFoundError, ValueError):
            raise ValueError(f"unknown design lesson review: {review_id}") from None
        if not resolved_review_dir.is_dir():
            raise ValueError("review path must be a directory")
        return {
            "review_json": resolved_review_dir / "review.json",
            "review_card": resolved_review_dir / "review.md",
        }

    @staticmethod
    def _review_card(
        package: dict[str, Any],
        evidence_summary: list[dict[str, Any]] | None = None,
        validation_summary: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        card = {
            "title": package["title"],
            "problem_summary": package["problem"]["summary"],
            "symptoms": package["problem"]["symptoms"],
            "iterations": package["corrections"],
            "root_causes": package["root_causes"],
            "corrections": package["corrections"],
            "prevention": package["prevention"],
            "applicability": package["applicability"],
            "non_applicable_conditions": package.get(
                "non_applicable_conditions", []
            ),
            "search_terms": package["search_terms"],
            "atomic_assertions": [
                {
                    field: assertion[field]
                    for field in (
                        "subject_ref",
                        "predicate",
                        "object_value",
                        "constraint_kind",
                    )
                }
                for assertion in package["atomic_assertions"]
            ],
        }
        if evidence_summary is not None:
            card["evidence_summary"] = evidence_summary
        if validation_summary is not None:
            card["validation_summary"] = validation_summary
        return _redact_human_value(card)

    @classmethod
    def _render_markdown(
        cls,
        review_id: str,
        package: dict[str, Any],
        evidence_summary: list[dict[str, Any]] | None = None,
        validation_summary: list[dict[str, Any]] | None = None,
    ) -> str:
        def text(value: Any) -> str:
            escaped = html.escape(str(value), quote=False)
            for character in "\\`*[]#+|":
                escaped = escaped.replace(character, f"\\{character}")
            return escaped.replace("\r", "").replace("\n", "<br>")

        def json_text(value: Any) -> str:
            return text(json.dumps(value, ensure_ascii=False, sort_keys=True))

        def append_values(lines: list[str], values: list[Any]) -> None:
            if values:
                lines.extend(f"- {text(value)}" for value in values)
            else:
                lines.append("- None")

        def append_mapping(lines: list[str], value: dict[str, Any]) -> None:
            for key in sorted(value):
                lines.append(f"- {text(key)}: {json_text(value[key])}")

        card = cls._review_card(package, evidence_summary, validation_summary)
        lines = [
            f"# Engineer review: {text(card['title'])}",
            "",
            "## Problem summary",
            "",
            text(card["problem_summary"]),
            "",
            "## Symptoms / iterations",
            "",
            "### Symptoms",
            "",
        ]
        append_values(lines, card["symptoms"])
        lines.extend(["", "### Iterations", ""])
        append_values(lines, card["iterations"])
        lines.extend(["", "## Root causes", ""])
        append_values(lines, card["root_causes"])
        lines.extend(["", "## Corrections", ""])
        append_values(lines, card["corrections"])
        lines.extend(["", "## Prevention", ""])
        append_mapping(lines, card["prevention"])
        lines.extend(["", "## Applicability", ""])
        append_mapping(lines, card["applicability"])
        lines.extend(["", "## Non-applicable conditions", ""])
        append_values(lines, card["non_applicable_conditions"])
        lines.extend(["", "## Search terms", ""])
        append_values(lines, card["search_terms"])
        if evidence_summary is not None:
            lines.extend(["", "## Evidence summary", ""])
            for evidence in card["evidence_summary"]:
                lines.append(
                    "- "
                    + text(evidence["evidence_id"])
                    + ": "
                    + text(evidence["role"])
                    + " ("
                    + text(evidence["media_type"])
                    + "; "
                    + text(evidence.get("validation_kind", "not-applicable"))
                    + ")"
                )
        if validation_summary is not None:
            lines.extend(["", "## Validation summary", ""])
            for validation in card["validation_summary"]:
                lines.append(
                    "- "
                    + text(validation["validation_kind"])
                    + ": "
                    + text(validation["status"])
                )
                for check in validation.get("checks", []):
                    label = check.get(
                        "label",
                        check.get(
                            "check_id",
                            check.get("id", check.get("name", "unnamed-check")),
                        ),
                    )
                    lines.append(
                        "  - " + text(label) + ": " + text(check.get("status", "unknown"))
                    )
        lines.extend(["", "## Atomic assertions", ""])
        for number, assertion in enumerate(card["atomic_assertions"], start=1):
            lines.extend([
                f"{number}. Subject: {text(assertion['subject_ref'])}",
                f"   Predicate: {text(assertion['predicate'])}",
                f"   Object value: {json_text(assertion['object_value'])}",
                f"   Constraint kind: `{text(assertion['constraint_kind'])}`",
            ])
        return "\n".join(lines) + "\n"

    def discard_prepared_attempt(
        self, review_id: str, expected_package_sha256: str
    ) -> None:
        inspection = self.inspect(review_id)
        review = inspection.get("review")
        if (
            inspection.get("status") != "verified-local-only"
            or not isinstance(review, dict)
            or review.get("package_sha256") != expected_package_sha256
        ):
            raise ValueError("cannot discard an unverified prepared review")
        review_dir = Path(inspection["paths"]["review_json"]).parent
        if review_dir.is_symlink() or review_dir.parent != self.review_root:
            raise ValueError("review path is unsafe")
        remove_owned_tree(
            review_dir,
            expected_parent=self.review_root,
            label="prepared review",
        )

    def discard_prepared_attempt_owned(self, review_id: str) -> None:
        """Remove only the direct review directory known to belong to this attempt."""
        require_safe_id(review_id, "review_id")
        remove_owned_tree(
            self.review_root / review_id,
            expected_parent=self.review_root,
            label="review attempt",
        )

    def _validate_review(self, review: Any) -> dict[str, Any]:
        if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS:
            raise ValueError("invalid review record")
        if review["schema_version"] != "DesignLessonReview/v1":
            raise ValueError("invalid review schema_version")
        for field in ("review_id", "lesson_id", "working_copy_id"):
            if not isinstance(review[field], str):
                raise ValueError(f"invalid review {field}")
            require_safe_id(review[field], field)
        for field in ("package_sha256", "final_model_sha256", "review_card_sha256"):
            if not isinstance(review[field], str):
                raise ValueError(f"invalid review {field}")
            self._require_sha256(review[field], field)
        supersedes_review_id = review["supersedes_review_id"]
        if supersedes_review_id is not None:
            if not isinstance(supersedes_review_id, str):
                raise ValueError("invalid review supersedes_review_id")
            require_safe_id(supersedes_review_id, "supersedes_review_id")
        if review["status"] != "awaiting-engineer-review":
            raise ValueError("invalid review status")
        return review

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
    def _require_sha256(value: str, label: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
            raise ValueError(f"{label} is invalid")

    @staticmethod
    def _atomic_write(path: Path, contents: bytes) -> None:
        atomic_publish_new(path, contents)
