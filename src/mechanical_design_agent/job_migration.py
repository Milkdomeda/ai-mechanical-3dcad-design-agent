from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .design import DesignWorkspace
from .fcstd_security import FcstdSecurityError, inspect_fcstd_bytes
from .jobs import DesignJobManager, JobFailure
from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_new,
    ensure_managed_directory,
    read_managed_file,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class LegacyJobMigration:
    """Copy legacy working copies into independent Jobs while retaining originals."""

    def __init__(
        self,
        *,
        repository: Any,
        jobs: DesignJobManager,
        design: DesignWorkspace,
        actor_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> None:
        self.repository = repository
        self.jobs = jobs
        self.design = design
        self.actor_id = actor_id
        self.organization_id = organization_id
        self.design_group_id = design_group_id

    def dry_run(self) -> dict[str, object]:
        rows = self.repository.list_legacy_working_copies(
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
        )
        items: list[dict[str, object]] = []
        for row in rows:
            source = Path(os.path.abspath(str(row["working_path"])))
            try:
                evidence = read_managed_file(source)
                inspect_fcstd_bytes(evidence.content)
                status = "ready"
                digest = evidence.sha256
                size = evidence.size_bytes
            except (OSError, SecureFilesystemError, FcstdSecurityError):
                status = "blocked"
                digest = None
                size = None
            items.append(
                {
                    "legacy_working_copy_id": str(row["id"]),
                    "family_id": str(row["family_id"]) if row.get("family_id") else None,
                    "source_sha256": digest,
                    "size_bytes": size,
                    "status": status,
                }
            )
        payload: dict[str, object] = {
            "schema_version": "MechanicalDesignLegacyMigrationPlan/v1",
            "organization_id": self.organization_id,
            "design_group_id": self.design_group_id,
            "items": items,
        }
        payload["receipt_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
        return payload

    def doctor(self) -> dict[str, object]:
        """Classify retained Legacy sources without treating them as writable Jobs."""
        rows = self.repository.list_legacy_migration_bindings(
            workspace_id=str(self.jobs.workspace.workspace_id),
            organization_id=self.organization_id,
            design_group_id=self.design_group_id,
        )
        items: list[dict[str, object]] = []
        counts = {"unmigrated": 0, "incomplete": 0, "hash_divergent": 0, "migrated": 0}
        receipt_fields = {
            "schema_version",
            "legacy_working_copy_id",
            "legacy_source_retained",
            "source_sha256",
            "job_id",
            "working_copy_id",
            "plan_receipt_sha256",
        }
        for row in rows:
            legacy_id = str(row["legacy_working_copy_id"])
            job_id = row.get("migration_job_id")
            working_copy_id = row.get("migrated_working_copy_id")
            status = "migrated"
            issue_code: str | None = None
            if job_id is None:
                status = "unmigrated"
                issue_code = "JOB_MIGRATION_REQUIRED"
            elif (
                row.get("active_working_copy_id") is None
                or working_copy_id is None
                or str(row.get("migrated_job_id")) != str(job_id)
            ):
                status = "incomplete"
                issue_code = "JOB_MIGRATION_INCOMPLETE"
            else:
                try:
                    source = read_managed_file(
                        Path(os.path.abspath(str(row["legacy_working_path"])))
                    )
                    target_path = Path(
                        os.path.abspath(str(row["migrated_working_path"]))
                    )
                    target = read_managed_file(target_path)
                    inspect_fcstd_bytes(source.content)
                    inspect_fcstd_bytes(target.content)
                    expected_relative = (
                        f"models/working/{working_copy_id}/working.FCStd"
                    )
                    if (
                        row.get("migrated_working_relative_path") != expected_relative
                        or tuple(target_path.parts[-4:])
                        != ("models", "working", str(working_copy_id), "working.FCStd")
                        or row.get("migrated_working_sha256") != source.sha256
                        or int(row.get("migrated_working_size_bytes") or -1)
                        != source.size_bytes
                        or target.sha256 != source.sha256
                        or target.size_bytes != source.size_bytes
                        or target.content != source.content
                        or target.link_count != 1
                    ):
                        raise ValueError("migration binding digest mismatch")
                    job_root = target_path.parents[3]
                    receipt_path = (
                        job_root
                        / "provenance"
                        / "migrations"
                        / f"legacy-{legacy_id}.json"
                    )
                    receipt = json.loads(
                        read_managed_file(receipt_path).content.decode("utf-8")
                    )
                    if (
                        not isinstance(receipt, dict)
                        or set(receipt) != receipt_fields
                        or receipt.get("schema_version")
                        != "MechanicalDesignLegacyMigration/v1"
                        or receipt.get("legacy_working_copy_id") != legacy_id
                        or receipt.get("legacy_source_retained") is not True
                        or receipt.get("source_sha256") != source.sha256
                        or receipt.get("job_id") != str(job_id)
                        or receipt.get("working_copy_id") != str(working_copy_id)
                        or not isinstance(receipt.get("plan_receipt_sha256"), str)
                        or len(str(receipt["plan_receipt_sha256"])) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in str(receipt["plan_receipt_sha256"])
                        )
                    ):
                        raise ValueError("migration receipt mismatch")
                except (
                    IndexError,
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    SecureFilesystemError,
                    FcstdSecurityError,
                    ValueError,
                ):
                    status = "hash_divergent"
                    issue_code = "JOB_MIGRATION_DIVERGED"
            counts[status] += 1
            items.append(
                {
                    "legacy_working_copy_id": legacy_id,
                    "status": status,
                    "issue_code": issue_code,
                    "migration_job_id": str(job_id) if job_id is not None else None,
                    "migrated_working_copy_id": (
                        str(working_copy_id) if working_copy_id is not None else None
                    ),
                }
            )
        return {
            "schema_version": "MechanicalDesignLegacyMigrationDoctor/v1",
            "unmigrated_count": counts["unmigrated"],
            "incomplete_count": counts["incomplete"],
            "hash_divergent_count": counts["hash_divergent"],
            "migrated_count": counts["migrated"],
            "items": items,
        }

    def apply(
        self,
        *,
        plan: dict[str, object],
        receipt_sha256: str,
        confirmation: str,
    ) -> dict[str, object]:
        current = self.dry_run()
        if current.get("receipt_sha256") != receipt_sha256 or plan != current:
            raise JobFailure(
                "JOB_MIGRATION_PLAN_STALE",
                "legacy migration inventory changed; run dry-run again",
            )
        if confirmation.strip() != f"迁移旧设计 {receipt_sha256}":
            raise JobFailure(
                "JOB_CONFIRMATION_INVALID",
                "confirmation must exactly match 迁移旧设计 <receipt-sha256>",
            )
        if any(item.get("status") != "ready" for item in current["items"]):
            raise JobFailure(
                "JOB_MIGRATION_BLOCKED",
                "one or more legacy working copies cannot be read safely",
            )
        rows = {
            str(row["id"]): row
            for row in self.repository.list_legacy_working_copies(
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
            )
        }
        migrated: list[dict[str, object]] = []
        for item in current["items"]:
            legacy_id = str(item["legacy_working_copy_id"])
            row = rows[legacy_id]
            manifest = self.jobs.create(
                job_type="mechanical_design",
                title=f"Legacy design {legacy_id}",
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=(str(row["family_id"]) if row.get("family_id") else None),
                idempotency_token=f"legacy-working-copy:{legacy_id}",
                actor_id=self.actor_id,
            )
            # Always execute the deterministic binding path. It verifies existing
            # bytes and republishes a missing/stale projection after a crash.
            result = self.design.migrate_legacy_working_copy(
                job_id=str(manifest.job_id),
                expected_job_revision=manifest.revision,
                legacy_working_copy_id=legacy_id,
                source_path=str(row["working_path"]),
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=(str(row["family_id"]) if row.get("family_id") else None),
                actor_id=self.actor_id,
            )
            receipt = {
                "schema_version": "MechanicalDesignLegacyMigration/v1",
                "legacy_working_copy_id": legacy_id,
                "legacy_source_retained": True,
                "source_sha256": item["source_sha256"],
                "job_id": str(manifest.job_id),
                "working_copy_id": str(result["id"]),
                "plan_receipt_sha256": receipt_sha256,
            }
            result_job = result.get("job")
            if not isinstance(result_job, dict):
                raise JobFailure(
                    "JOB_MIGRATION_RESULT_INVALID",
                    "migrated working copy did not return an authoritative Job binding",
                )
            with self.jobs.locked_active_mechanical_design_job(
                job_id=str(manifest.job_id),
                expected_job_revision=int(result_job["revision"]),
                organization_id=self.organization_id,
                design_group_id=self.design_group_id,
                family_id=(str(row["family_id"]) if row.get("family_id") else None),
            ) as (job_root, _):
                receipt_dir = ensure_managed_directory(
                    job_root / "provenance" / "migrations", parents=True, exist_ok=True
                ).path
                receipt_path = receipt_dir / f"legacy-{legacy_id}.json"
                expected_receipt = _canonical(receipt) + b"\n"
                if receipt_path.exists():
                    if read_managed_file(receipt_path).content != expected_receipt:
                        raise JobFailure(
                            "JOB_MIGRATION_DIVERGED",
                            "existing migration receipt disagrees with authority",
                        )
                else:
                    atomic_publish_new(receipt_path, expected_receipt)
            migrated.append({**receipt, "receipt_path": str(receipt_path)})
        return {
            "schema_version": "MechanicalDesignLegacyMigrationResult/v1",
            "plan_receipt_sha256": receipt_sha256,
            "migrated": migrated,
        }
