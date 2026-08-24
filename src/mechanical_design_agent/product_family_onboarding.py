from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from .fcstd_security import FcstdSecurityError, inspect_fcstd_bytes
from .jobs import DesignJobManager, JobFailure, managed_job_path
from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_new,
    ensure_managed_directory,
    read_managed_file,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise JobFailure("JOB_ONBOARDING_IDENTITY_INVALID", f"{label} must be a SHA-256")
    return value


class ProductFamilyOnboarding:
    """Keep family discovery evidence inside one governed onboarding Job."""

    def __init__(
        self,
        *,
        repository: Any,
        jobs: DesignJobManager,
        actor_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> None:
        self.repository = repository
        self.jobs = jobs
        self.actor_id = actor_id
        self.organization_id = organization_id
        self.design_group_id = design_group_id

    @staticmethod
    def _source(path_value: str) -> tuple[Path, Any, str]:
        if not isinstance(path_value, str) or not path_value.strip():
            raise JobFailure("JOB_SOURCE_FILE_INVALID", "source path is required")
        source = Path(os.path.abspath(Path(path_value).expanduser()))
        suffix = source.suffix.casefold()
        if suffix not in {".fcstd", ".step", ".stp"}:
            raise JobFailure(
                "JOB_SOURCE_FILE_INVALID",
                "onboarding sources must be FCStd, STEP, or STP files",
            )
        try:
            evidence = read_managed_file(source)
            if evidence.size_bytes <= 0 or evidence.link_count != 1:
                raise SecureFilesystemError(
                    "SOURCE_FILE_UNSAFE", "source is not an exclusively readable regular file"
                )
            if suffix == ".fcstd":
                inspect_fcstd_bytes(evidence.content)
        except (OSError, SecureFilesystemError, FcstdSecurityError) as exc:
            raise JobFailure(
                "JOB_SOURCE_FILE_UNSAFE",
                "onboarding source is missing, unsafe, or unsupported",
            ) from exc
        return source, evidence, suffix

    def _manifest(
        self,
        *,
        locked_root: Path,
        job_id: str,
        previous_revision: int,
        changed: bool,
        job_row: dict[str, Any],
    ) -> dict[str, object]:
        if changed:
            return self.jobs.publish_authoritative_revision_locked(
                locked_root=locked_root,
                job_id=job_id,
                expected_previous_revision=previous_revision,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
            ).as_dict()
        return self.jobs.read_authoritative_manifest_locked(
            locked_root=locked_root,
            authoritative_row=job_row,
        ).as_dict()

    def start(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        source_paths: list[str],
    ) -> dict[str, object]:
        if not source_paths:
            raise JobFailure(
                "JOB_SOURCE_FILES_COUNT_INVALID",
                "onboarding requires at least one source model",
            )
        if not isinstance(family_id, str) or not family_id.strip():
            raise JobFailure("JOB_FAMILY_REQUIRED", "family_id is required")
        run_id = str(uuid5(UUID(job_id), "product-family-onboarding"))
        prepared = [self._source(value) for value in source_paths]
        if len({evidence.sha256 for _, evidence, _ in prepared}) != len(prepared):
            raise JobFailure(
                "JOB_SOURCE_FILES_DUPLICATE",
                "onboarding source models must be distinct",
            )
        with self.jobs.locked_active_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
            job_type="product_family_onboarding",
            family_id=family_id,
        ) as (job_root, _):
            snapshots: list[dict[str, object]] = []
            for index, (source, evidence, suffix) in enumerate(prepared):
                snapshot_id = str(
                    uuid5(UUID(run_id), f"source:{index}:{evidence.sha256}")
                )
                stored_filename = "source.FCStd" if suffix == ".fcstd" else "source.step"
                stored_path = f"inputs/source/{snapshot_id}/{stored_filename}"
                target = managed_job_path(
                    job_root=job_root,
                    relative_path=stored_path,
                    allow_missing_leaf=True,
                )
                if target.exists():
                    existing = read_managed_file(target)
                    if existing.content != evidence.content or existing.sha256 != evidence.sha256:
                        raise JobFailure(
                            "JOB_ONBOARDING_DIVERGED",
                            "existing onboarding snapshot disagrees with its source",
                        )
                else:
                    ensure_managed_directory(target.parent, parents=True, exist_ok=True)
                    atomic_publish_new(target, evidence.content)
                snapshots.append(
                    {
                        "id": snapshot_id,
                        "source_filename": stored_filename,
                        "original_filename": source.name,
                        "stored_path": stored_path,
                        "sha256": evidence.sha256,
                        "size_bytes": evidence.size_bytes,
                        "source_kind": "product_family_input",
                        "source_model_revision_id": None,
                    }
                )
            input_manifest = {
                "schema_version": "ProductFamilyOnboardingInputs/v1",
                "run_id": run_id,
                "job_id": job_id,
                "family_id": family_id,
                "snapshots": snapshots,
            }
            result = self.repository.start_product_family_onboarding(
                run_id=run_id,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=family_id,
                input_manifest=input_manifest,
                input_manifest_sha256=_sha256(input_manifest),
                snapshots=snapshots,
                actor_id=self.actor_id,
            )
            manifest = self._manifest(
                locked_root=job_root,
                job_id=job_id,
                previous_revision=expected_job_revision,
                changed=bool(result["changed"]),
                job_row=dict(result["job"]),
            )
        return {
            "schema_version": "ProductFamilyOnboardingStart/v1",
            "run": dict(result["run"]),
            "job": manifest,
        }

    @staticmethod
    def _candidate(value: object, index: int) -> dict[str, object]:
        required = {
            "subject_ref",
            "predicate",
            "object_value",
            "unit",
            "scope_kind",
            "risk_level",
            "source_kind",
            "evidence",
            "confidence",
            "applicability",
            "non_applicable_conditions",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID",
                f"candidate_knowledge[{index}] fields are invalid",
            )
        if any(
            not isinstance(value[field], str) or not str(value[field]).strip()
            for field in ("subject_ref", "predicate", "risk_level", "source_kind")
        ):
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID",
                f"candidate_knowledge[{index}] identity is incomplete",
            )
        if value["scope_kind"] != "family":
            raise JobFailure(
                "JOB_ONBOARDING_SCOPE_INVALID",
                "onboarding publishes only family-scoped knowledge",
            )
        if value["risk_level"] not in {"R0", "R1", "R2", "R3"}:
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID", "candidate risk_level is invalid"
            )
        confidence = value["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID", "candidate confidence is invalid"
            )
        if not isinstance(value["evidence"], list) or not value["evidence"]:
            raise JobFailure(
                "JOB_ONBOARDING_EVIDENCE_REQUIRED", "candidate evidence is required"
            )
        if not isinstance(value["applicability"], dict) or not isinstance(
            value["non_applicable_conditions"], list
        ):
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID",
                "candidate applicability fields are invalid",
            )
        unit = value["unit"]
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATE_INVALID", "candidate unit is invalid"
            )
        return dict(value)

    def analyze(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        analysis: dict[str, object],
        candidate_knowledge: list[object],
    ) -> dict[str, object]:
        if not isinstance(analysis, dict) or not analysis:
            raise JobFailure("JOB_ONBOARDING_ANALYSIS_REQUIRED", "analysis is required")
        candidates = [self._candidate(value, index) for index, value in enumerate(candidate_knowledge)]
        if not candidates:
            raise JobFailure(
                "JOB_ONBOARDING_CANDIDATES_REQUIRED",
                "analysis must produce at least one reviewable knowledge candidate",
            )
        run_id = str(uuid5(UUID(job_id), "product-family-onboarding"))
        current = self.repository.get_product_family_onboarding(
            job_id=job_id,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
        )
        input_manifest = current.get("input_manifest")
        snapshots = input_manifest.get("snapshots") if isinstance(input_manifest, dict) else None
        allowed_snapshot_ids = {
            str(snapshot["id"])
            for snapshot in snapshots or []
            if isinstance(snapshot, dict) and snapshot.get("id")
        }
        for index, candidate in enumerate(candidates):
            referenced = {
                str(item["snapshot_id"])
                for item in candidate["evidence"]
                if isinstance(item, dict) and item.get("snapshot_id")
            }
            if not referenced or not referenced <= allowed_snapshot_ids:
                raise JobFailure(
                    "JOB_ONBOARDING_EVIDENCE_INVALID",
                    f"candidate_knowledge[{index}] must cite captured snapshot identities",
                )
        analysis_record = {
            "schema_version": "ProductFamilyOnboardingAnalysis/v1",
            "run_id": run_id,
            "job_id": job_id,
            "family_id": family_id,
            "analysis": analysis,
        }
        package = {
            "schema_version": "ProductFamilyKnowledgeCandidates/v1",
            "run_id": run_id,
            "job_id": job_id,
            "family_id": family_id,
            "candidates": candidates,
        }
        analysis_sha = _sha256(analysis_record)
        package_sha = _sha256(package)
        analysis_path = f"analysis/onboarding-{run_id}.json"
        package_path = f"knowledge/extracted/{package_sha}.json"
        with self.jobs.locked_active_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
            job_type="product_family_onboarding",
            family_id=family_id,
        ) as (job_root, _):
            self._publish_json(job_root, analysis_path, analysis_record)
            self._publish_json(job_root, package_path, package)
            result = self.repository.analyze_product_family_onboarding(
                run_id=run_id,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=family_id,
                analysis=analysis_record,
                analysis_sha256=analysis_sha,
                analysis_path=analysis_path,
                candidate_knowledge=candidates,
                package_sha256=package_sha,
                package_path=package_path,
                actor_id=self.actor_id,
            )
            manifest = self._manifest(
                locked_root=job_root,
                job_id=job_id,
                previous_revision=expected_job_revision,
                changed=bool(result["changed"]),
                job_row=dict(result["job"]),
            )
        return {
            "schema_version": "ProductFamilyOnboardingAnalysisResult/v1",
            "run": dict(result["run"]),
            "analysis_sha256": analysis_sha,
            "package_sha256": package_sha,
            "job": manifest,
        }

    @staticmethod
    def _publish_json(job_root: Path, relative_path: str, value: object) -> None:
        path = managed_job_path(
            job_root=job_root,
            relative_path=relative_path,
            allow_missing_leaf=True,
        )
        ensure_managed_directory(path.parent, parents=True, exist_ok=True)
        content = _canonical(value) + b"\n"
        if path.exists():
            if read_managed_file(path).content != content:
                raise JobFailure(
                    "JOB_ONBOARDING_DIVERGED",
                    "existing onboarding artifact disagrees with its immutable identity",
                )
        else:
            atomic_publish_new(path, content)

    def review(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        package_sha256: str,
        decision: str,
        reviewer_text: str,
        confirmation: str,
    ) -> dict[str, object]:
        package_sha256 = _require_digest(package_sha256, "package_sha256")
        if decision not in {"approve", "reject"}:
            raise JobFailure("JOB_ONBOARDING_REVIEW_INVALID", "decision must be approve or reject")
        action = "批准产品族知识" if decision == "approve" else "拒绝产品族知识"
        if confirmation.strip() != f"{action} {package_sha256}":
            raise JobFailure(
                "JOB_CONFIRMATION_INVALID",
                f"confirmation must exactly match {action} <package-sha256>",
            )
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise JobFailure("JOB_ONBOARDING_REVIEW_INVALID", "reviewer_text is required")
        run_id = str(uuid5(UUID(job_id), "product-family-onboarding"))
        review_identity = _sha256(
            {
                "run_id": run_id,
                "package_sha256": package_sha256,
                "decision": decision,
                "reviewer_id": self.actor_id,
                "reviewer_text": reviewer_text.strip(),
            }
        )
        review_id = str(uuid5(UUID(run_id), f"review:{review_identity}"))
        review_card = {
            "schema_version": "ProductFamilyOnboardingReview/v1",
            "review_id": review_id,
            "review_identity": review_identity,
            "run_id": run_id,
            "job_id": job_id,
            "family_id": family_id,
            "package_sha256": package_sha256,
            "decision": decision,
            "reviewer_id": self.actor_id,
            "reviewer_text": reviewer_text.strip(),
        }
        review_path = f"knowledge/onboarding-review-{review_identity}.json"
        with self.jobs.locked_active_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
            job_type="product_family_onboarding",
            family_id=family_id,
        ) as (job_root, _):
            self._publish_json(job_root, review_path, review_card)
            result = self.repository.review_product_family_onboarding(
                review_id=review_id,
                review_identity=review_identity,
                run_id=run_id,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=family_id,
                package_sha256=package_sha256,
                decision=decision,
                reviewer_id=self.actor_id,
                reviewer_text=reviewer_text.strip(),
                review_path=review_path,
            )
            manifest = self._manifest(
                locked_root=job_root,
                job_id=job_id,
                previous_revision=expected_job_revision,
                changed=bool(result["changed"]),
                job_row=dict(result["job"]),
            )
        return {
            "schema_version": "ProductFamilyOnboardingReviewResult/v1",
            "review": dict(result["review"]),
            "job": manifest,
        }

    def publish(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        family_id: str,
        package_sha256: str,
        review_identity: str,
        confirmation: str,
    ) -> dict[str, object]:
        package_sha256 = _require_digest(package_sha256, "package_sha256")
        review_identity = _require_digest(review_identity, "review_identity")
        if confirmation.strip() != f"发布产品族知识 {review_identity}":
            raise JobFailure(
                "JOB_CONFIRMATION_INVALID",
                "confirmation must exactly match 发布产品族知识 <review-identity>",
            )
        run_id = str(uuid5(UUID(job_id), "product-family-onboarding"))
        run = self.repository.get_product_family_onboarding(
            job_id=job_id,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
        )
        candidates = run.get("candidate_knowledge")
        if not isinstance(candidates, list) or not candidates:
            raise JobFailure(
                "JOB_ONBOARDING_NOT_ANALYZED", "onboarding has no candidate knowledge"
            )
        assertion_ids = [
            str(uuid5(UUID(run_id), f"assertion:{package_sha256}:{index}"))
            for index in range(len(candidates))
        ]
        publication_identity = _sha256(
            {
                "run_id": run_id,
                "job_id": job_id,
                "family_id": family_id,
                "package_sha256": package_sha256,
                "review_identity": review_identity,
                "assertion_ids": assertion_ids,
            }
        )
        publication_id = str(
            uuid5(UUID(run_id), f"publication:{publication_identity}")
        )
        receipt = {
            "schema_version": "ProductFamilyOnboardingPublication/v1",
            "publication_id": publication_id,
            "publication_identity": publication_identity,
            "run_id": run_id,
            "job_id": job_id,
            "family_id": family_id,
            "package_sha256": package_sha256,
            "review_identity": review_identity,
            "assertion_ids": assertion_ids,
            "published_by": self.actor_id,
        }
        receipt_sha = _sha256(receipt)
        publication_path = f"knowledge/onboarding-publication-{publication_identity}.json"
        existing_publication = run.get("publication")
        if existing_publication is not None:
            if (
                not isinstance(existing_publication, dict)
                or existing_publication.get("publication_identity")
                != publication_identity
                or existing_publication.get("package_sha256") != package_sha256
                or existing_publication.get("assertion_ids") != assertion_ids
            ):
                raise JobFailure(
                    "JOB_ONBOARDING_DIVERGED",
                    "existing onboarding publication disagrees with the requested identity",
                )
            manifest = self.jobs.get(
                job_id=job_id,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
            )
            if manifest.revision != expected_job_revision:
                raise JobFailure(
                    "JOB_STALE_REVISION", "expected Job revision is stale"
                )
            return {
                "schema_version": "ProductFamilyOnboardingPublicationResult/v1",
                "publication": dict(existing_publication),
                "job": manifest.as_dict(),
            }
        with self.jobs.locked_active_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
            job_type="product_family_onboarding",
            family_id=family_id,
        ) as (job_root, _):
            self._publish_json(job_root, publication_path, receipt)
            result = self.repository.publish_product_family_onboarding(
                publication_id=publication_id,
                publication_identity=publication_identity,
                publication_receipt_sha256=receipt_sha,
                publication_path=publication_path,
                assertion_ids=assertion_ids,
                run_id=run_id,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=family_id,
                package_sha256=package_sha256,
                review_identity=review_identity,
                candidates=candidates,
                actor_id=self.actor_id,
            )
            manifest = self._manifest(
                locked_root=job_root,
                job_id=job_id,
                previous_revision=expected_job_revision,
                changed=bool(result["changed"]),
                job_row=dict(result["job"]),
            )
        return {
            "schema_version": "ProductFamilyOnboardingPublicationResult/v1",
            "publication": dict(result["publication"]),
            "job": manifest,
        }

    def status(self, *, job_id: str) -> dict[str, object]:
        return {
            "schema_version": "ProductFamilyOnboardingStatus/v1",
            "onboarding": self.repository.get_product_family_onboarding(
                job_id=job_id,
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
            ),
        }
