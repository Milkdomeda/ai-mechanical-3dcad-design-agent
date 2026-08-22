from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import uuid
import json
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .hashing import file_sha256
from .freecad_runner import run_freecad_script
from .package_resources import freecad_scripts_directory
from .assembly import validate_assembly_completeness
from .artifacts import ArtifactStore
from .secure_fs import (
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_file_lock,
    remove_owned_tree,
    validate_managed_path,
)


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
    def __init__(self, settings: Settings, repository: Any):
        self.settings = settings
        self.repository = repository
        self.root = settings.workspace / "output" / "mechanical_design" / "working_copies"

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
        with self.locked_working_copy_path(working_copy_id) as path:
            return self.repository.mark_change_set_applied(
                change_set_id, file_sha256(path)
            )

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
            resolved_report = str(report)
            report_sha256 = file_sha256(report)
        with self.locked_working_copy_path(working_copy_id) as working_path:
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
        with self.locked_working_copy_path(working_copy_id) as working_path:
            working_sha256 = file_sha256(working_path)
            if manifest.get("working_sha256") != working_sha256:
                raise ValueError("assembly manifest working_sha256 mismatch")
            result = validate_assembly_completeness(manifest)
            report_dir = self.settings.workspace / "output" / "mechanical_design" / "assembly_validation" / working_copy_id
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
        with self.locked_current_snapshot(working_copy_id, artifact_store) as snapshot:
            return self.repository.approve_delivery(
                working_copy_id,
                actor_id,
                confirmation,
                str(snapshot["sha256"]),
                str(snapshot["storage_path"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
            )
