from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import uuid
import json
import secrets
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .hashing import file_sha256
from .freecad_runner import run_freecad_script
from .fcstd_security import FcstdSecurityError, inspect_fcstd_bytes
from .package_resources import freecad_scripts_directory
from .assembly import validate_assembly_completeness
from .artifacts import ArtifactStore
from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_file_lock,
    list_managed_directory,
    read_managed_file,
    remove_owned_tree,
    same_managed_path,
    set_managed_file_readonly,
    validate_managed_path,
)
from .jobs import JobFailure, managed_job_path


def derive_iteration_candidates(
    changes: list[dict[str, Any]], validations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return deterministic audit facts that may merit semantic review."""
    change_targets: dict[str, dict[str, Any]] = {}
    for change_set in changes:
        change_set_id = str(change_set.get("id", ""))
        structured_changes = change_set.get("changes", [])
        if not isinstance(structured_changes, list):
            continue
        for change in structured_changes:
            if not isinstance(change, dict):
                continue
            target = change.get("target")
            if not isinstance(target, str) or not target.strip():
                continue
            normalized_target = target.strip()
            grouped = change_targets.setdefault(
                normalized_target, {"occurrences": 0, "source_ids": set()}
            )
            grouped["occurrences"] += 1
            if change_set_id:
                grouped["source_ids"].add(change_set_id)

    failed_checks: dict[str, dict[str, Any]] = {}
    for validation in validations:
        validation_id = str(validation.get("id", ""))
        checks = validation.get("checks", [])
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            check_status = check.get("status")
            if not isinstance(check_status, str) or check_status in {"passed", "ok"}:
                continue
            check_id = check.get("check_id", check.get("id", check.get("name")))
            if not isinstance(check_id, str) or not check_id.strip():
                continue
            normalized_check_id = check_id.strip()
            grouped = failed_checks.setdefault(
                normalized_check_id, {"occurrences": 0, "source_ids": set()}
            )
            grouped["occurrences"] += 1
            if validation_id:
                grouped["source_ids"].add(validation_id)

    candidates = [
        {
            "candidate_kind": "repeated_change_target",
            "target": target,
            "occurrences": grouped["occurrences"],
            "change_set_ids": sorted(grouped["source_ids"]),
        }
        for target, grouped in sorted(change_targets.items())
        if grouped["occurrences"] > 1
    ]
    candidates.extend(
        {
            "candidate_kind": "failed_validation_check",
            "check_id": check_id,
            "occurrences": grouped["occurrences"],
            "validation_report_ids": sorted(grouped["source_ids"]),
        }
        for check_id, grouped in sorted(failed_checks.items())
    )
    return candidates


class DesignWorkspace:
    def __init__(self, settings: Settings, repository: Any, design_jobs: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.design_jobs = design_jobs
        self.root = settings.workspace / "output" / "mechanical_design" / "working_copies"

    def _require_job_manager(self) -> Any:
        if self.design_jobs is None:
            raise RuntimeError("Job-aware CAD creation requires the Design Job manager")
        return self.design_jobs

    @contextmanager
    def locked_job_working_copy(
        self, working_copy_id: str
    ) -> Iterator[tuple[Path, Path, dict[str, Any], dict[str, Any]]]:
        """Bind one operational filesystem boundary to its active Job authority."""
        working = self.repository.get_working_copy(working_copy_id)
        job_id = working.get("job_id")
        if not job_id:
            raise JobFailure(
                "JOB_MIGRATION_REQUIRED",
                "the working copy is not bound to a Design Job",
            )
        organization_id = str(working["organization_id"])
        design_group_id = str(working["design_group_id"])
        job = self.repository.get_design_job(
            job_id=str(job_id),
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        manager = self._require_job_manager()
        with manager.locked_active_mechanical_design_job(
            job_id=str(job_id),
            expected_job_revision=int(job["revision"]),
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=(str(working["family_id"]) if working.get("family_id") else None),
        ) as (job_root, fresh):
            if str(fresh.get("active_working_copy_id")) != working_copy_id:
                raise JobFailure(
                    "JOB_WORKING_COPY_NOT_ACTIVE",
                    "the working copy is not the active copy for its Design Job",
                )
            relative = working.get("working_relative_path")
            if not isinstance(relative, str) or not relative:
                raise JobFailure(
                    "JOB_WORKING_COPY_PATH_INVALID",
                    "the governed working-copy path is incomplete",
                )
            path = managed_job_path(
                job_root=job_root,
                relative_path=relative,
                allow_missing_leaf=False,
            )
            authoritative = Path(os.path.abspath(str(working["working_path"])))
            if not same_managed_path(path, authoritative) or path.suffix.casefold() != ".fcstd":
                raise JobFailure(
                    "JOB_WORKING_COPY_PATH_INVALID",
                    "the governed working-copy path disagrees with authority",
                )
            yield job_root, path, working, fresh

    @staticmethod
    def _job_attempt_allowed_files(receipt: dict[str, object]) -> frozenset[str]:
        kind = receipt.get("artifact_kind")
        if kind == "source_snapshot":
            return frozenset(
                {".binding-attempt.json", "source.FCStd", "source.step"}
            )
        if kind == "working_copy":
            return frozenset({".binding-attempt.json", "working.FCStd"})
        raise JobFailure(
            "JOB_ATTEMPT_INVENTORY_UNSAFE",
            "Job binding attempt kind is not recognized",
        )

    @staticmethod
    def _job_attempt_directory(
        parent: Path, attempt_id: str, receipt: dict[str, object]
    ) -> Path:
        attempt = parent / attempt_id
        receipt_bytes = (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        if attempt.exists() or attempt.is_symlink():
            raise JobFailure(
                "JOB_ATTEMPT_RECOVERY_REQUIRED",
                "A preserved Job binding attempt requires explicit doctor/repair recovery",
            )
        created = ensure_managed_directory(
            attempt,
            parents=False,
            exist_ok=False,
        ).path
        try:
            atomic_publish_new(created / ".binding-attempt.json", receipt_bytes)
        except Exception as exc:
            raise JobFailure(
                "JOB_ATTEMPT_RECOVERY_REQUIRED",
                "A Job binding receipt could not be published; the attempt was preserved",
            ) from exc
        return created

    @staticmethod
    def _record_job_attempt_inventory(
        attempt: Path,
        *,
        receipt: dict[str, object],
        artifact_names: tuple[str, ...],
    ) -> None:
        artifacts: list[dict[str, object]] = []
        for name in artifact_names:
            if name not in DesignWorkspace._job_attempt_allowed_files(receipt):
                raise JobFailure(
                    "JOB_ATTEMPT_INVENTORY_UNSAFE",
                    "Job attempt receipt cannot claim an unexpected artifact",
                )
            read = read_managed_file(attempt / name)
            artifacts.append(
                {
                    "filename": name,
                    "sha256": read.sha256,
                    "size_bytes": read.size_bytes,
                    "identity": {
                        "volume": read.identity.volume,
                        "file_index": read.identity.file_index,
                    },
                }
            )
        final_receipt = {**receipt, "artifacts": artifacts}
        atomic_replace(
            attempt / ".binding-attempt.json",
            (json.dumps(final_receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )

    @staticmethod
    def _assert_job_attempt_inventory(
        attempt: Path,
        *,
        required_files: frozenset[str],
    ) -> None:
        try:
            entries = tuple(list_managed_directory(attempt))
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "FreeCAD output inventory cannot be verified safely",
            ) from exc
        names = {entry.name for entry in entries}
        if names != set(required_files) or any(entry.is_directory for entry in entries):
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "FreeCAD produced files outside the controlled output contract",
            )

    def _validate_job_fcstd(
        self,
        *,
        working_attempt: Path,
        working_path: Path,
    ) -> Any:
        try:
            before = read_managed_file(working_path)
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "FreeCAD working-copy output is missing or unsafe",
            ) from exc
        if (
            working_path.suffix.casefold() != ".fcstd"
            or before.size_bytes <= 0
            or before.link_count != 1
        ):
            code = (
                "JOB_OUTPUT_UNEXPECTED"
                if before.link_count != 1
                else "JOB_FCSTD_INVALID"
            )
            raise JobFailure(
                code,
                "FreeCAD working-copy output is not an exclusively owned nonempty FCStd",
            )
        try:
            inspect_fcstd_bytes(before.content)
        except FcstdSecurityError as exc:
            raise JobFailure(
                "JOB_FCSTD_INVALID",
                "FCStd static inspection rejected an unsafe or unsupported document",
            ) from exc
        self._assert_job_attempt_inventory(
            working_attempt,
            required_files=frozenset({".binding-attempt.json", "working.FCStd"}),
        )
        nonce = secrets.token_urlsafe(32)
        with freecad_scripts_directory() as scripts:
            completed = self._run_controlled_job_freecad(
                script=scripts / "validate_working_copy.py",
                arguments=[working_path, nonce],
                controlled_parent=working_attempt.parent,
                controlled_directory=working_attempt,
                timeout_seconds=900,
                failure_code="JOB_FCSTD_INVALID",
                failure_message="FreeCAD could not reopen, recompute, and validate the FCStd output",
                allow_stdout=True,
            )
        self._assert_job_attempt_inventory(
            working_attempt,
            required_files=frozenset({".binding-attempt.json", "working.FCStd"}),
        )
        try:
            prefix = "MECHANICAL_DESIGN_FCSTD_VALIDATION_V1 "
            if (
                not isinstance(completed.stdout, str)
                or not completed.stdout.startswith(prefix)
                or completed.stdout.count("\n") != 1
                or not completed.stdout.endswith("\n")
            ):
                raise ValueError("validation stdout is not exactly one evidence record")
            payload = json.loads(completed.stdout[len(prefix) : -1])
            after = read_managed_file(working_path)
            inspect_fcstd_bytes(after.content)
        except (
            SecureFilesystemError,
            FcstdSecurityError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise JobFailure(
                "JOB_FCSTD_INVALID",
                "FreeCAD validation process evidence is missing or invalid",
            ) from exc
        expected_fields = {
            "schema_version",
            "status",
            "nonce",
            "sha256",
            "size_bytes",
            "document_name",
            "object_count",
            "recomputed",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or payload.get("schema_version")
            != "MechanicalDesignWorkingCopyValidation/v2"
            or payload.get("status") != "valid"
            or payload.get("nonce") != nonce
            or payload.get("sha256") != before.sha256
            or payload.get("size_bytes") != before.size_bytes
            or not isinstance(payload.get("document_name"), str)
            or not str(payload.get("document_name")).strip()
            or type(payload.get("object_count")) is not int
            or int(payload["object_count"]) < 0
            or payload.get("recomputed") is not True
            or after.identity != before.identity
            or after.sha256 != before.sha256
            or after.size_bytes != before.size_bytes
            or after.content != before.content
            or after.link_count != 1
        ):
            raise JobFailure(
                "JOB_FCSTD_INVALID",
                "FreeCAD validation evidence does not match the controlled FCStd output",
            )
        return after

    def _run_controlled_job_freecad(
        self,
        *,
        script: Path,
        arguments: list[str | Path],
        controlled_parent: Path,
        controlled_directory: Path,
        timeout_seconds: int,
        failure_code: str,
        failure_message: str,
        allow_stdout: bool = False,
    ) -> Any:
        try:
            parent_before = tuple(list_managed_directory(controlled_parent))
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "Controlled FreeCAD output parent cannot be pinned",
            ) from exc
        run_error: Exception | None = None
        completed: Any | None = None
        try:
            completed = run_freecad_script(
                self.settings.freecadcmd,
                script,
                arguments,
                timeout_seconds=timeout_seconds,
                expected_sha256=self.settings.freecadcmd_sha256,
                expected_identity=self.settings.freecadcmd_identity,
                controlled_directory=controlled_directory,
            )
        except Exception as exc:
            run_error = exc
        try:
            parent_after = tuple(list_managed_directory(controlled_parent))
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "Controlled FreeCAD output parent changed identity",
            ) from exc
        if parent_after != parent_before:
            raise JobFailure(
                "JOB_OUTPUT_UNEXPECTED",
                "FreeCAD produced a sibling outside the controlled output directory",
            ) from run_error
        if run_error is not None or completed is None or completed.returncode != 0:
            raise JobFailure(failure_code, failure_message) from run_error
        if not isinstance(completed.stderr, str) or completed.stderr:
            raise JobFailure(
                failure_code,
                "FreeCAD produced an unexpected diagnostic process channel",
            )
        if not allow_stdout and (
            not isinstance(completed.stdout, str) or completed.stdout
        ):
            raise JobFailure(
                failure_code,
                "FreeCAD produced unexpected stdout outside the evidence contract",
            )
        return completed

    @staticmethod
    def _translate_job_publication_error(error: Exception) -> JobFailure:
        if isinstance(error, JobFailure):
            return error
        message = str(error).casefold()
        if "stale" in message and "revision" in message:
            return JobFailure(
                "JOB_REVISION_STALE",
                "The Design Job revision changed before working-copy publication",
            )
        if "active working copy" in message or "active working-copy" in message:
            return JobFailure(
                "JOB_ACTIVE_WORKING_COPY_EXISTS",
                "The Design Job already has an active working copy",
            )
        if isinstance(error, (KeyError, PermissionError)) or any(
            marker in message for marker in ("unauthorized", "scope", "family")
        ):
            return JobFailure(
                "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                "The Design Job or source revision is unavailable in the authorized scope",
            )
        if "active ready mechanical_design" in message:
            return JobFailure(
                "JOB_NOT_ACTIVE",
                "Working-copy creation requires an active ready mechanical-design Job",
            )
        return JobFailure(
            "JOB_DATABASE_PUBLICATION_FAILED",
            "The verified CAD bytes could not be published to the Design Job authority",
        )

    def _publish_job_working_copy(self, **publication: Any) -> dict[str, Any]:
        """Publish or reconcile a commit-ambiguous repository exception.

        Repository exceptions are never treated as rollback proof.  Reconciliation
        uses a fresh scoped connection in the repository and deterministic artifact
        identifiers before this caller decides whether owned bytes may be removed.
        """
        try:
            return self.repository.create_job_working_copy(**publication)
        except Exception as publication_error:
            try:
                reconciliation = (
                    self.repository.reconcile_job_working_copy_publication(
                        job_id=publication["job_id"],
                        expected_job_revision=publication["expected_job_revision"],
                        organization_id=publication["organization_id"],
                        design_group_id=publication["design_group_id"],
                        family_id=publication["family_id"],
                        working_copy_id=publication["working_copy_id"],
                        model_revision_id=publication["model_revision_id"],
                        source_sha256=publication["source_sha256"],
                        source_kind=publication["source_kind"],
                        design_origin=publication["design_origin"],
                        working_path=publication["working_path"],
                        working_sha256=publication.get("working_sha256"),
                        working_size_bytes=publication.get("working_size_bytes"),
                        working_relative_path=publication["working_relative_path"],
                        actor_id=publication["actor_id"],
                        source_snapshot=publication.get("source_snapshot"),
                    )
                )
            except Exception as reconciliation_error:
                raise JobFailure(
                    "JOB_DATABASE_COMMIT_UNKNOWN",
                    "Database publication could not be reconciled; owned CAD bytes were preserved",
                ) from reconciliation_error
            if not isinstance(reconciliation, dict):
                raise JobFailure(
                    "JOB_DATABASE_COMMIT_UNKNOWN",
                    "Database publication returned no authoritative reconciliation; owned CAD bytes were preserved",
                ) from publication_error
            status = reconciliation.get("status")
            if status == "committed":
                reconciled = reconciliation.get("publication")
                if not isinstance(reconciled, dict):
                    raise JobFailure(
                        "JOB_DATABASE_COMMIT_UNKNOWN",
                        "Committed publication evidence was incomplete; owned CAD bytes were preserved",
                    ) from publication_error
                return reconciled
            if status == "not_committed":
                raise self._translate_job_publication_error(publication_error) from publication_error
            raise JobFailure(
                "JOB_DATABASE_COMMIT_UNKNOWN",
                "Database publication state is inconsistent; owned CAD bytes were preserved",
            ) from publication_error

    def _reconcile_before_job_attempt(self, **publication: Any) -> dict[str, Any] | None:
        """Resolve deterministic binding IDs before inspecting or creating attempts."""
        try:
            reconciliation = self.repository.reconcile_job_working_copy_publication(
                job_id=publication["job_id"],
                expected_job_revision=publication["expected_job_revision"],
                organization_id=publication["organization_id"],
                design_group_id=publication["design_group_id"],
                family_id=publication["family_id"],
                working_copy_id=publication["working_copy_id"],
                model_revision_id=publication["model_revision_id"],
                source_sha256=publication.get("source_sha256"),
                source_kind=publication["source_kind"],
                design_origin=publication["design_origin"],
                working_path=publication["working_path"],
                working_sha256=publication.get("working_sha256"),
                working_size_bytes=publication.get("working_size_bytes"),
                working_relative_path=publication["working_relative_path"],
                actor_id=publication["actor_id"],
                source_snapshot=publication.get("source_snapshot"),
            )
        except Exception as exc:
            raise JobFailure(
                "JOB_DATABASE_COMMIT_UNKNOWN",
                "Database authority was unavailable before retry; existing attempt bytes were preserved",
            ) from exc
        if not isinstance(reconciliation, dict):
            raise JobFailure(
                "JOB_DATABASE_COMMIT_UNKNOWN",
                "Database authority returned no retry reconciliation",
            )
        status = reconciliation.get("status")
        if status == "not_committed":
            return None
        if status == "committed" and isinstance(reconciliation.get("publication"), dict):
            return dict(reconciliation["publication"])
        raise JobFailure(
            "JOB_DATABASE_COMMIT_UNKNOWN",
            "Database binding state was partial or mismatched; attempt bytes were preserved",
        )

    def create_job_working_copy(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        """Create a source snapshot and FCStd copy within one authorized Job lock."""
        source = Path(os.path.abspath(Path(source_path).expanduser()))
        source_suffix = source.suffix.lower()
        if source_suffix not in {".step", ".stp", ".fcstd"}:
            raise JobFailure(
                "JOB_SOURCE_FILE_INVALID",
                "Working-copy source must be one FCStd or STEP file",
            )
        manager = self._require_job_manager()
        snapshot_attempt: Path | None = None
        working_attempt: Path | None = None

        with manager.locked_active_mechanical_design_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
        ) as (job_root, _job):
            job_root = validate_managed_path(
                job_root, allow_missing_leaf=False
            ).path
            try:
                source_read = read_managed_file(source)
            except SecureFilesystemError as exc:
                raise JobFailure(
                    "JOB_SOURCE_UNSAFE",
                    "source CAD cannot be read through a stable no-follow handle",
                ) from exc
            try:
                job_namespace = uuid.UUID(job_id)
            except ValueError as exc:
                raise ValueError("job_id must be a UUID") from exc
            snapshot_id = str(
                uuid.uuid5(
                    job_namespace,
                    f"source-snapshot:{expected_job_revision}:{source_read.sha256}",
                )
            )
            working_copy_id = str(
                uuid.uuid5(
                    job_namespace,
                    f"working-copy:{expected_job_revision}:existing_model:{source_read.sha256}",
                )
            )
            source_model = self.repository.resolve_source_model_revision(
                organization_id=organization_id,
                design_group_id=design_group_id,
                source_sha256=source_read.sha256,
                requested_model_revision_id=model_revision_id,
                requested_family_id=family_id,
            )
            resolved_model_revision_id = str(source_model["id"])
            resolved_family_id = (
                str(source_model["family_id"])
                if source_model.get("family_id")
                else None
            )
            source_parent = validate_managed_path(
                job_root / "inputs" / "source", allow_missing_leaf=False
            ).path
            working_parent = validate_managed_path(
                job_root / "models" / "working", allow_missing_leaf=False
            ).path
            snapshot_name = "source.FCStd" if source_suffix == ".fcstd" else "source.step"
            snapshot_path = source_parent / snapshot_id / snapshot_name
            working_path = working_parent / working_copy_id / "working.FCStd"
            stored_path = snapshot_path.relative_to(job_root).as_posix()
            working_relative_path = working_path.relative_to(job_root).as_posix()
            source_snapshot = {
                "id": snapshot_id,
                "source_filename": source.name,
                "stored_path": stored_path,
                "sha256": source_read.sha256,
                "size_bytes": source_read.size_bytes,
                "source_kind": "existing_model",
                "source_model_revision_id": resolved_model_revision_id,
            }
            publication_request = {
                "job_id": job_id,
                "expected_job_revision": expected_job_revision,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "family_id": resolved_family_id,
                "working_copy_id": working_copy_id,
                "model_revision_id": resolved_model_revision_id,
                "source_sha256": source_read.sha256,
                "source_kind": "existing_model",
                "design_origin": "existing_model",
                "working_path": str(working_path),
                "working_sha256": None,
                "working_size_bytes": None,
                "working_relative_path": working_relative_path,
                "actor_id": actor_id,
                "source_snapshot": source_snapshot,
            }
            reconciled = self._reconcile_before_job_attempt(**publication_request)
            if reconciled is not None:
                try:
                    snapshot_read = read_managed_file(snapshot_path)
                    working_read = read_managed_file(working_path)
                    inspect_fcstd_bytes(working_read.content)
                except (SecureFilesystemError, FcstdSecurityError) as exc:
                    raise JobFailure(
                        "JOB_DATABASE_COMMIT_UNKNOWN",
                        "Committed binding bytes are missing or unsafe and require doctor/repair",
                    ) from exc
                if (
                    snapshot_read.sha256 != source_read.sha256
                    or snapshot_read.size_bytes != source_read.size_bytes
                    or snapshot_read.content != source_read.content
                    or working_read.sha256
                    != reconciled["working_copy"].get("working_sha256")
                    or working_read.size_bytes
                    != reconciled["working_copy"].get("working_size_bytes")
                    or snapshot_read.link_count != 1
                    or working_read.link_count != 1
                ):
                    raise JobFailure(
                        "JOB_DATABASE_COMMIT_UNKNOWN",
                        "Committed binding bytes do not match authoritative evidence",
                    )
                manifest = manager.publish_authoritative_manifest_locked(
                    locked_root=job_root,
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    working_copy_id=working_copy_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
                return {
                    **dict(reconciled["working_copy"]),
                    "source_sha256": source_read.sha256,
                    "source_snapshot": {
                        "snapshot_id": snapshot_id,
                        "stored_path": stored_path,
                        "sha256": source_read.sha256,
                        "source_kind": "existing_model",
                        "source_model_revision_id": resolved_model_revision_id,
                    },
                    "working_path": str(working_path),
                    "job": manifest.as_dict(),
                }
            snapshot_receipt = {
                "schema_version": "MechanicalDesignJobBindingAttempt/v2",
                "job_id": job_id,
                "expected_job_revision": expected_job_revision,
                "artifact_kind": "source_snapshot",
                "artifact_id": snapshot_id,
                "source_sha256": source_read.sha256,
                "artifacts": [],
            }
            working_receipt = {
                "schema_version": "MechanicalDesignJobBindingAttempt/v2",
                "job_id": job_id,
                "expected_job_revision": expected_job_revision,
                "artifact_kind": "working_copy",
                "artifact_id": working_copy_id,
                "source_sha256": source_read.sha256,
                "artifacts": [],
            }
            try:
                snapshot_attempt = self._job_attempt_directory(
                    source_parent,
                    snapshot_id,
                    snapshot_receipt,
                )
                snapshot_path = snapshot_attempt / snapshot_name
                atomic_publish_new(snapshot_path, source_read.content)
                set_managed_file_readonly(snapshot_path)
                snapshot_read = read_managed_file(snapshot_path)
                if (
                    snapshot_read.sha256 != source_read.sha256
                    or snapshot_read.content != source_read.content
                ):
                    raise RuntimeError("source snapshot verification failed")
                self._record_job_attempt_inventory(
                    snapshot_attempt,
                    receipt=snapshot_receipt,
                    artifact_names=(snapshot_name,),
                )

                working_attempt = self._job_attempt_directory(
                    working_parent,
                    working_copy_id,
                    working_receipt,
                )
                working_path = working_attempt / "working.FCStd"
                if source_suffix == ".fcstd":
                    atomic_publish_new(working_path, snapshot_read.content)
                else:
                    with freecad_scripts_directory() as scripts:
                        self._run_controlled_job_freecad(
                            script=scripts / "normalize_working_copy.py",
                            arguments=[snapshot_path, working_path],
                            controlled_parent=working_parent,
                            controlled_directory=working_attempt,
                            timeout_seconds=900,
                            failure_code="JOB_NORMALIZATION_FAILED",
                            failure_message="FreeCAD could not normalize the governed source snapshot",
                        )
                working_read = self._validate_job_fcstd(
                    working_attempt=working_attempt,
                    working_path=working_path,
                )
                self._record_job_attempt_inventory(
                    working_attempt,
                    receipt=working_receipt,
                    artifact_names=("working.FCStd",),
                )
                if source_suffix == ".fcstd" and (
                    working_read.sha256 != source_read.sha256
                    or working_read.content != source_read.content
                ):
                    raise JobFailure(
                        "JOB_FCSTD_INVALID",
                        "FCStd working-copy bytes do not match the governed snapshot",
                    )

                publication_request["working_sha256"] = working_read.sha256
                publication_request["working_size_bytes"] = working_read.size_bytes

                try:
                    final_source_read = read_managed_file(source)
                except SecureFilesystemError as exc:
                    raise JobFailure(
                        "JOB_SOURCE_CHANGED",
                        "source CAD identity became unavailable during binding",
                    ) from exc
                if (
                    final_source_read.identity != source_read.identity
                    or final_source_read.sha256 != source_read.sha256
                    or final_source_read.content != source_read.content
                ):
                    raise JobFailure(
                        "JOB_SOURCE_CHANGED",
                        "source CAD changed while creating the governed working copy",
                    )

                published = self._publish_job_working_copy(**publication_request)
                manifest = manager.publish_authoritative_manifest_locked(
                    locked_root=job_root,
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    working_copy_id=working_copy_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
            except Exception:
                # Cross-platform path-based deletion cannot continuously pin every
                # descendant. Preserve failed attempts for doctor/repair instead.
                raise

        public_snapshot = {
            "snapshot_id": snapshot_id,
            "stored_path": stored_path,
            "sha256": source_read.sha256,
            "source_kind": "existing_model",
            "source_model_revision_id": resolved_model_revision_id,
        }
        return {
            **dict(published["working_copy"]),
            "source_sha256": source_read.sha256,
            "source_snapshot": public_snapshot,
            "working_path": str(working_path),
            "job": manifest.as_dict(),
        }

    def create_job_new_working_copy(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        """Create one empty FCStd directly in a governed Job attempt directory."""
        manager = self._require_job_manager()
        try:
            job_namespace = uuid.UUID(job_id)
        except ValueError as exc:
            raise ValueError("job_id must be a UUID") from exc
        working_copy_id = str(
            uuid.uuid5(
                job_namespace,
                f"working-copy:{expected_job_revision}:new_design",
            )
        )
        working_attempt: Path | None = None
        working_receipt = {
            "schema_version": "MechanicalDesignJobBindingAttempt/v2",
            "job_id": job_id,
            "expected_job_revision": expected_job_revision,
            "artifact_kind": "working_copy",
            "artifact_id": working_copy_id,
            "source_sha256": None,
            "artifacts": [],
        }
        with manager.locked_active_mechanical_design_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
        ) as (job_root, _job):
            job_root = validate_managed_path(
                job_root, allow_missing_leaf=False
            ).path
            working_parent = validate_managed_path(
                job_root / "models" / "working", allow_missing_leaf=False
            ).path
            working_path = working_parent / working_copy_id / "working.FCStd"
            working_relative_path = working_path.relative_to(job_root).as_posix()
            publication_request = {
                "job_id": job_id,
                "expected_job_revision": expected_job_revision,
                "organization_id": organization_id,
                "design_group_id": design_group_id,
                "family_id": family_id,
                "working_copy_id": working_copy_id,
                "model_revision_id": None,
                "source_sha256": None,
                "source_kind": "new_design_seed",
                "design_origin": "new_design",
                "working_path": str(working_path),
                "working_sha256": None,
                "working_size_bytes": None,
                "working_relative_path": working_relative_path,
                "actor_id": actor_id,
                "source_snapshot": None,
            }
            reconciled = self._reconcile_before_job_attempt(**publication_request)
            if reconciled is not None:
                try:
                    working_read = read_managed_file(working_path)
                    inspect_fcstd_bytes(working_read.content)
                except (SecureFilesystemError, FcstdSecurityError) as exc:
                    raise JobFailure(
                        "JOB_DATABASE_COMMIT_UNKNOWN",
                        "Committed working-copy bytes are missing or unsafe and require doctor/repair",
                    ) from exc
                authoritative_sha = reconciled["working_copy"].get("working_sha256")
                authoritative_size = reconciled["working_copy"].get("working_size_bytes")
                if (
                    working_read.sha256 != authoritative_sha
                    or working_read.size_bytes != authoritative_size
                    or working_read.link_count != 1
                ):
                    raise JobFailure(
                        "JOB_DATABASE_COMMIT_UNKNOWN",
                        "Committed working-copy bytes do not match authoritative evidence",
                    )
                manifest = manager.publish_authoritative_manifest_locked(
                    locked_root=job_root,
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    working_copy_id=working_copy_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
                return {
                    **dict(reconciled["working_copy"]),
                    "source_sha256": working_read.sha256,
                    "source_snapshot": None,
                    "working_path": str(working_path),
                    "job": manifest.as_dict(),
                }
            try:
                working_attempt = self._job_attempt_directory(
                    working_parent,
                    working_copy_id,
                    working_receipt,
                )
                working_path = working_attempt / "working.FCStd"
                with freecad_scripts_directory() as scripts:
                    self._run_controlled_job_freecad(
                        script=scripts / "create_empty_working_copy.py",
                        arguments=[working_path],
                        controlled_parent=working_parent,
                        controlled_directory=working_attempt,
                        timeout_seconds=120,
                        failure_code="JOB_FCSTD_INVALID",
                        failure_message="FreeCAD could not create the governed FCStd working copy",
                    )
                working_read = self._validate_job_fcstd(
                    working_attempt=working_attempt,
                    working_path=working_path,
                )
                self._record_job_attempt_inventory(
                    working_attempt,
                    receipt=working_receipt,
                    artifact_names=("working.FCStd",),
                )
                publication_request["source_sha256"] = working_read.sha256
                publication_request["working_sha256"] = working_read.sha256
                publication_request["working_size_bytes"] = working_read.size_bytes
                published = self._publish_job_working_copy(**publication_request)
                manifest = manager.publish_authoritative_manifest_locked(
                    locked_root=job_root,
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    working_copy_id=working_copy_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
            except Exception:
                # Failed bytes remain isolated under their deterministic attempt
                # for explicit doctor/repair; no path-based recursive deletion.
                raise
        return {
            **dict(published["working_copy"]),
            "source_sha256": working_read.sha256,
            "source_snapshot": None,
            "working_path": str(working_path),
            "job": manifest.as_dict(),
        }

    def migrate_legacy_working_copy(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        legacy_working_copy_id: str,
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        """Copy a legacy FCStd into a new governed binding without mutating history."""
        source = Path(os.path.abspath(Path(source_path).expanduser()))
        try:
            source_read = read_managed_file(source)
            inspect_fcstd_bytes(source_read.content)
        except (SecureFilesystemError, FcstdSecurityError) as exc:
            raise JobFailure(
                "JOB_LEGACY_SOURCE_UNSAFE",
                "legacy FCStd bytes are missing or outside the supported safe subset",
            ) from exc
        manager = self._require_job_manager()
        with manager.locked_active_mechanical_design_job(
            job_id=job_id,
            expected_job_revision=expected_job_revision,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
        ) as (job_root, job):
            working_copy_id = str(
                uuid.uuid5(uuid.UUID(job_id), f"legacy-working-copy:{legacy_working_copy_id}")
            )
            relative_path = f"models/working/{working_copy_id}/working.FCStd"
            target = managed_job_path(
                job_root=job_root,
                relative_path=relative_path,
                allow_missing_leaf=True,
            )
            active_working_copy_id = job.get("active_working_copy_id")
            if active_working_copy_id is not None:
                if str(active_working_copy_id) != working_copy_id:
                    raise JobFailure(
                        "JOB_ACTIVE_WORKING_COPY_EXISTS",
                        "the Legacy Job is already bound to a different working copy",
                    )
                try:
                    working = self.repository.get_working_copy(working_copy_id)
                    existing = read_managed_file(target)
                    inspect_fcstd_bytes(existing.content)
                except (KeyError, SecureFilesystemError, FcstdSecurityError) as exc:
                    raise JobFailure(
                        "JOB_MIGRATION_DIVERGED",
                        "the existing migrated working copy is missing or unsafe",
                    ) from exc
                if (
                    str(working.get("job_id")) != job_id
                    or working.get("working_relative_path") != relative_path
                    or Path(os.path.abspath(str(working.get("working_path")))) != target
                    or working.get("working_sha256") != source_read.sha256
                    or working.get("source_sha256") != source_read.sha256
                    or int(working.get("working_size_bytes") or -1) != source_read.size_bytes
                    or existing.sha256 != source_read.sha256
                    or existing.size_bytes != source_read.size_bytes
                    or existing.content != source_read.content
                    or existing.link_count != 1
                ):
                    raise JobFailure(
                        "JOB_MIGRATION_DIVERGED",
                        "the existing migrated binding disagrees with the legacy inventory",
                    )
                manifest = manager.read_authoritative_manifest_locked(
                    locked_root=job_root,
                    authoritative_row=job,
                )
                return {
                    **dict(working),
                    "legacy_working_copy_id": legacy_working_copy_id,
                    "legacy_source_sha256": source_read.sha256,
                    "legacy_source_retained": str(source),
                    "job": manifest.as_dict(),
                }

            working_parent = validate_managed_path(
                job_root / "models" / "working", allow_missing_leaf=False
            ).path
            attempt_receipt = {
                "schema_version": "MechanicalDesignJobBindingAttempt/v2",
                "job_id": job_id,
                "expected_job_revision": expected_job_revision,
                "artifact_kind": "working_copy",
                "artifact_id": working_copy_id,
                "source_sha256": source_read.sha256,
                "artifacts": [],
            }
            attempt = self._job_attempt_directory(
                working_parent,
                working_copy_id,
                attempt_receipt,
            )
            target = attempt / "working.FCStd"
            try:
                atomic_publish_new(target, source_read.content)
                migrated_read = read_managed_file(target)
                inspect_fcstd_bytes(migrated_read.content)
                if (
                    migrated_read.sha256 != source_read.sha256
                    or migrated_read.size_bytes != source_read.size_bytes
                    or migrated_read.content != source_read.content
                    or migrated_read.link_count != 1
                ):
                    raise JobFailure(
                        "JOB_MIGRATION_DIVERGED",
                        "copied FCStd bytes do not match the legacy inventory",
                    )
                self._assert_job_attempt_inventory(
                    attempt,
                    required_files=frozenset({".binding-attempt.json", "working.FCStd"}),
                )
                self._record_job_attempt_inventory(
                    attempt,
                    receipt=attempt_receipt,
                    artifact_names=("working.FCStd",),
                )
                publication = self._publish_job_working_copy(
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    family_id=family_id,
                    working_copy_id=working_copy_id,
                    model_revision_id=None,
                    source_sha256=source_read.sha256,
                    source_kind="new_design_seed",
                    design_origin="new_design",
                    working_path=str(target),
                    working_sha256=source_read.sha256,
                    working_size_bytes=source_read.size_bytes,
                    working_relative_path=relative_path,
                    actor_id=actor_id,
                    source_snapshot=None,
                )
                manifest = manager.publish_authoritative_manifest_locked(
                    locked_root=job_root,
                    job_id=job_id,
                    expected_job_revision=expected_job_revision,
                    working_copy_id=working_copy_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
                return {
                    **dict(publication["working_copy"]),
                    "legacy_working_copy_id": legacy_working_copy_id,
                    "legacy_source_sha256": source_read.sha256,
                    "legacy_source_retained": str(source),
                    "job": manifest.as_dict(),
                }
            except Exception:
                # Preserve deterministic attempt bytes for doctor/repair.
                raise

    def _create_attempt_directory(self, copy_id: str) -> Path:
        root = ensure_managed_directory(
            self.root,
            parents=True,
            exist_ok=True,
        ).path
        return ensure_managed_directory(
            root / copy_id,
            parents=False,
            exist_ok=False,
        ).path

    def _discard_attempt_directory(self, target_dir: Path) -> None:
        remove_owned_tree(
            target_dir,
            expected_parent=Path(os.path.abspath(self.root)),
            label="working-copy attempt",
        )

    @contextmanager
    def locked_working_copy_path(self, working_copy_id: str) -> Iterator[Path]:
        """Serialize every Agent read/write boundary for one published FCStd path."""
        working = self.repository.get_working_copy(working_copy_id)
        if working.get("job_id"):
            with self.locked_job_working_copy(working_copy_id) as (_, path, _, _):
                yield path
            return
        raw_path = Path(os.path.abspath(str(working["working_path"])))
        workspace = validate_managed_path(
            self.settings.workspace, allow_missing_leaf=False
        ).path
        managed = validate_managed_path(raw_path, allow_missing_leaf=False)
        path = managed.path
        parent = path.parent
        if not parent.is_relative_to(workspace):
            raise ValueError("working copy must remain inside the configured workspace")
        lock_path = parent / ".working-copy.lock"
        with exclusive_file_lock(lock_path):
            path = validate_managed_path(
                raw_path, allow_missing_leaf=False
            ).path
            if not path.is_file() or path.suffix.lower() != ".fcstd":
                raise ValueError("working-copy target must be an FCStd file")
            if not path.is_relative_to(workspace):
                raise ValueError("working copy must remain inside the configured workspace")
            yield path

    def create_working_copy(
        self,
        *,
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        model_revision_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in {".step", ".stp", ".fcstd"}:
            raise ValueError("working-copy source must be a STEP or FCStd file")
        source_sha = file_sha256(source)
        source_model = self.repository.resolve_source_model_revision(
            organization_id=organization_id,
            design_group_id=design_group_id,
            source_sha256=source_sha,
            requested_model_revision_id=model_revision_id,
            requested_family_id=family_id,
        )
        model_revision_id = str(source_model["id"])
        family_id = str(source_model["family_id"]) if source_model.get("family_id") else None
        copy_id = str(uuid.uuid4())
        target_dir = self._create_attempt_directory(copy_id)
        target = target_dir / f"{source.stem}.working.FCStd"
        if source.suffix.lower() == ".fcstd":
            shutil.copyfile(source, target)
        else:
            with freecad_scripts_directory() as scripts:
                completed = run_freecad_script(
                    self.settings.freecadcmd,
                    scripts / "normalize_working_copy.py",
                    [source, target],
                    timeout_seconds=900,
                    expected_sha256=self.settings.freecadcmd_sha256,
                    expected_identity=self.settings.freecadcmd_identity,
                    controlled_directory=target_dir,
                )
            if completed.returncode != 0 or not target.is_file():
                self._discard_attempt_directory(target_dir)
                diagnostic = (completed.stderr + "\n" + completed.stdout)[-4000:]
                raise RuntimeError(f"FreeCAD working-copy normalization failed: {diagnostic}")
        if file_sha256(source) != source_sha:
            self._discard_attempt_directory(target_dir)
            raise RuntimeError("source CAD changed while creating working copy")
        try:
            record = self.repository.create_working_copy(
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                model_revision_id=model_revision_id,
                source_sha256=source_sha,
                source_kind="existing_model",
                design_origin="existing_model",
                working_path=str(target),
                actor_id=actor_id,
            )
        except Exception:
            self._discard_attempt_directory(target_dir)
            raise
        return {**record, "source_path": str(source), "source_sha256": source_sha, "working_path": str(target)}

    def create_new_working_copy(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        copy_id = str(uuid.uuid4())
        target_dir = self._create_attempt_directory(copy_id)
        target = target_dir / "new-design.working.FCStd"
        with freecad_scripts_directory() as scripts:
            completed = run_freecad_script(
                self.settings.freecadcmd,
                scripts / "create_empty_working_copy.py",
                [target],
                timeout_seconds=120,
                expected_sha256=self.settings.freecadcmd_sha256,
                expected_identity=self.settings.freecadcmd_identity,
                controlled_directory=target_dir,
            )
        if completed.returncode != 0 or not target.is_file():
            diagnostic = (completed.stderr + "\n" + completed.stdout)[-4000:]
            self._discard_attempt_directory(target_dir)
            raise RuntimeError(f"FreeCAD new working-copy creation failed: {diagnostic}")
        seed_sha = file_sha256(target)
        try:
            record = self.repository.create_working_copy(
                organization_id=organization_id,
                design_group_id=design_group_id,
                family_id=family_id,
                model_revision_id=None,
                source_sha256=seed_sha,
                source_kind="new_design_seed",
                design_origin="new_design",
                working_path=str(target),
                actor_id=actor_id,
            )
        except Exception:
            self._discard_attempt_directory(target_dir)
            raise
        return {**record, "source_path": None, "source_sha256": seed_sha, "working_path": str(target)}

    def record_change(
        self,
        *,
        working_copy_id: str,
        change_phase: str,
        changes: list[dict[str, Any]],
        knowledge_used: list[str],
        rationale: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not changes:
            raise ValueError("at least one structured change is required")
        if not rationale.strip():
            raise ValueError("change rationale is required")
        if change_phase not in {"design_proposal", "structure_change", "parameter_change"}:
            raise ValueError("change_phase must be design_proposal, structure_change, or parameter_change")
        return self.repository.record_change_set(
            working_copy_id, change_phase, changes, knowledge_used, rationale, actor_id
        )

    def mark_change_applied(self, *, change_set_id: str, confirmation: str) -> dict[str, Any]:
        if change_set_id not in confirmation or "已应用" not in confirmation:
            raise ValueError("confirmation must include change_set_id and 已应用")
        change = self.repository.get_change_set(change_set_id)
        working_copy_id = str(change["working_copy_id"])
        self.repository.require_completed_retrieval(
            working_copy_id,
            expected_used_knowledge_ids=list(change.get("knowledge_used") or []),
        )
        with self.locked_job_working_copy(working_copy_id) as (job_root, path, _, _):
            digest = file_sha256(path)
            revision_dir = ensure_managed_directory(
                job_root / "models" / "revisions" / working_copy_id,
                parents=True,
                exist_ok=True,
            ).path
            revision_path = revision_dir / f"{digest}.FCStd"
            if not revision_path.exists():
                atomic_publish_new(revision_path, read_managed_file(path).content)
            result = self.repository.mark_change_set_applied(change_set_id, digest)
            return {**result, "job_revision_path": str(revision_path)}

    def record_validation(
        self,
        *,
        working_copy_id: str,
        change_set_id: str | None,
        status: str,
        checks: list[dict[str, Any]],
        report_path: str = "",
        validation_kind: str = "geometry_model",
    ) -> dict[str, Any]:
        if not checks:
            raise ValueError("validation checks are required")
        mandatory_failures = [
            check for check in checks if check.get("mandatory", True) and check.get("status") not in {"passed", "ok"}
        ]
        if status == "passed" and mandatory_failures:
            raise ValueError("validation cannot pass while mandatory checks have not passed")
        if validation_kind not in {
            "geometry_model",
            "assembly_completeness",
            "fastener_interfaces",
            "mechanical_interfaces",
        }:
            raise ValueError("unsupported validation_kind")
        if status == "passed" and not report_path:
            raise ValueError("passed typed validation requires report_path")
        if status == "passed" and validation_kind == "geometry_model" and not any(
            check.get("validator") in {"freecad-model-validation", "freecad-mcp-model-validation"}
            for check in checks
        ):
            raise ValueError("passed validation requires evidence from freecad-model-validation")
        resolved_report = ""
        report_sha256 = ""
        if report_path:
            report = validate_managed_path(
                Path(os.path.abspath(Path(report_path).expanduser())),
                allow_missing_leaf=False,
            ).path
            if not report.is_file() or not report.is_relative_to(
                validate_managed_path(
                    self.settings.workspace, allow_missing_leaf=False
                ).path
            ):
                raise ValueError("validation report must be a file inside the workspace")
        with self.locked_job_working_copy(working_copy_id) as (job_root, working_path, _, _):
            if report_path:
                report_read = read_managed_file(report)
                report_sha256 = report_read.sha256
                report_dir = ensure_managed_directory(
                    job_root / "validation" / "reports" / working_copy_id,
                    parents=True,
                    exist_ok=True,
                ).path
                controlled_report = report_dir / f"{report_sha256}{report.suffix.casefold()}"
                if not controlled_report.exists():
                    atomic_publish_new(controlled_report, report_read.content)
                resolved_report = str(controlled_report)
            return self.repository.record_validation(
                working_copy_id,
                change_set_id,
                status,
                checks,
                file_sha256(working_path),
                resolved_report,
                validation_kind,
                report_sha256,
            )

    def validate_assembly_completeness(
        self, *, working_copy_id: str, change_set_id: str | None, manifest: dict[str, Any]
    ) -> dict[str, Any]:
        if manifest.get("schema_version") != "AssemblyCompleteness/v2":
            raise ValueError("assembly manifest must use AssemblyCompleteness/v2")
        if str(manifest.get("working_copy_id", "")) != working_copy_id:
            raise ValueError("assembly manifest working_copy_id mismatch")
        for evidence_group in ("fastener_geometry_checks", "mechanical_interface_checks"):
            evidence_items = manifest.get(evidence_group, [])
            if not isinstance(evidence_items, list):
                raise ValueError(f"{evidence_group} must be a list")
            for evidence in evidence_items:
                if not isinstance(evidence, dict) or not evidence.get("report_path"):
                    continue
                evidence_path = validate_managed_path(
                    Path(
                        os.path.abspath(
                            Path(str(evidence["report_path"])).expanduser()
                        )
                    ),
                    allow_missing_leaf=False,
                ).path
                if not evidence_path.is_file() or not evidence_path.is_relative_to(
                    validate_managed_path(
                        self.settings.workspace, allow_missing_leaf=False
                    ).path
                ):
                    raise ValueError("assembly-interface validation evidence must be a report inside the workspace")
        with self.locked_job_working_copy(working_copy_id) as (job_root, working_path, _, _):
            working_sha256 = file_sha256(working_path)
            if manifest.get("working_sha256") != working_sha256:
                raise ValueError("assembly manifest working_sha256 mismatch")
            result = validate_assembly_completeness(manifest)
            report_dir = job_root / "validation" / "assembly" / working_copy_id
            report_dir = ensure_managed_directory(
                report_dir,
                parents=True,
                exist_ok=True,
            ).path
            report_path = report_dir / "assembly-completeness.json"
            report_bytes = (
                json.dumps(
                    {"manifest": manifest, "validation": result},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            try:
                atomic_publish_new(report_path, report_bytes)
            except FileExistsError:
                atomic_replace(report_path, report_bytes)
            record = self.repository.record_validation(
                working_copy_id,
                change_set_id,
                result["status"],
                result["checks"],
                working_sha256,
                str(report_path),
                "assembly_completeness",
                file_sha256(report_path),
            )
        return {"record": record, "report_path": str(report_path), **result}

    def current_hash(self, working_copy_id: str) -> str:
        with self.locked_working_copy_path(working_copy_id) as working_path:
            return file_sha256(working_path)

    @contextmanager
    def locked_current_snapshot(
        self, working_copy_id: str, artifact_store: ArtifactStore
    ) -> Iterator[dict[str, Any]]:
        """Hold the shared FCStd lock from CAS capture through caller commit."""
        with self.locked_working_copy_path(working_copy_id) as working_path:
            yield artifact_store.ingest_file(
                working_path,
                allowed_root=self.settings.workspace,
            )

    def current_snapshot(
        self, working_copy_id: str, artifact_store: ArtifactStore
    ) -> dict[str, Any]:
        """Copy the current FCStd bytes to CAS for immutable approval binding."""
        with self.locked_current_snapshot(working_copy_id, artifact_store) as snapshot:
            return snapshot

    def approve_delivery(
        self,
        working_copy_id: str,
        actor_id: str,
        confirmation: str,
        artifact_store: ArtifactStore,
        *,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        with self.locked_job_working_copy(working_copy_id) as (job_root, working_path, _, _):
            working_read = read_managed_file(working_path)
            delivery_dir = ensure_managed_directory(
                job_root / "delivery" / working_copy_id,
                parents=True,
                exist_ok=True,
            ).path
            delivery_path = delivery_dir / f"{working_read.sha256}.FCStd"
            if not delivery_path.exists():
                atomic_publish_new(delivery_path, working_read.content)
            snapshot = {
                "sha256": working_read.sha256,
                "size_bytes": working_read.size_bytes,
                "storage_path": str(delivery_path),
            }
            return self.repository.approve_delivery(
                working_copy_id,
                actor_id,
                confirmation,
                str(snapshot["sha256"]),
                str(snapshot["storage_path"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
            )
