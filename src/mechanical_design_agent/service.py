from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from .artifacts import ArtifactChecksumMismatchError, ArtifactStore
from .config import JobCadSettings, JobSettings, Settings
from .context import DesignContextBuilder
from .design import DesignWorkspace, derive_iteration_candidates
from .design_lessons import (
    EVIDENCE_ROLE_VALIDATION_KINDS,
    DesignLessonStagingStore,
    match_design_lesson,
    satisfying_conditions,
    validate_design_lesson_package,
)
from .extractor import FreeCADExtractor
from .hashing import file_sha256, stable_hash
from .learning import family_statistics, generate_question_targets, parse_assertion_proposals
from .jobs import DesignJobManager, DesignJobManifest, JobFailure
from .library import LibraryScanner, scan_change_dict
from .lesson_reviews import DesignLessonReviewStore
from .migrations import postgres_migrations_directory
from .projection import Neo4jProjection
from .product_families import validate_product_family_config
from .repository import PostgresRepository
from .standard_parts import StandardPartRegistry
from .workspace_bootstrap import read_workspace_manifest
from .secure_fs import (
    atomic_publish_new,
    atomic_publish_owned_file,
    atomic_replace,
    ensure_managed_directory,
    relative_managed_path,
    same_managed_path,
    validate_managed_path,
)


class ImmutableReviewBindingDriftError(ValueError):
    """A prepared review no longer matches its immutable local bindings."""


class MechanicalDesignService:
    def __init__(self, settings: Settings | JobSettings | JobCadSettings):
        self.settings = settings
        self.repository = PostgresRepository(settings.database_url)
        self.design_jobs = DesignJobManager(
            read_workspace_manifest(settings.workspace), self.repository
        )
        if isinstance(settings, JobSettings):
            self.bootstrap_config = {
                "organization_id": settings.organization_id,
                "design_group_id": settings.design_group_id,
            }
            if isinstance(settings, JobCadSettings):
                self.design_workspace = DesignWorkspace(
                    settings, self.repository, self.design_jobs
                )
            self.bootstrap_error = ""
            return
        self.artifacts = ArtifactStore(settings.artifact_root)
        self.design_lesson_staging = DesignLessonStagingStore(settings.workspace)
        self.design_lesson_reviews = DesignLessonReviewStore(settings.workspace)
        self.scanner = LibraryScanner()
        self.extractor = FreeCADExtractor(settings)
        self.projection = Neo4jProjection(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
        )
        self.context_builder = DesignContextBuilder(self.repository, self.projection)
        self.design_workspace = DesignWorkspace(
            settings, self.repository, self.design_jobs
        )
        self._standard_parts: StandardPartRegistry | None = None
        self._standard_parts_lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="freecad-ingest")
        self.futures: dict[str, Future[Any]] = {}
        self.futures_lock = Lock()
        self.bootstrap_config = self._read_bootstrap_file()
        self.bootstrap_error = ""
        try:
            self._initialize_database()
        except Exception as exc:
            # Keep the MCP process available so design_system_status can explain an unavailable database.
            self.bootstrap_error = f"{type(exc).__name__}: {exc}"

    def _read_bootstrap_file(self) -> dict[str, Any]:
        value = json.loads(self.settings.family_config_path.read_text(encoding="utf-8"))
        return dict(
            validate_product_family_config(
                value,
                path=self.settings.family_config_path,
                require_filename_match=False,
            )
        )

    @property
    def standard_parts(self) -> StandardPartRegistry:
        if self._standard_parts is not None:
            return self._standard_parts
        with self._standard_parts_lock:
            if self._standard_parts is None:
                self._standard_parts = StandardPartRegistry(self.settings, self.repository)
        return self._standard_parts

    def _initialize_database(self) -> None:
        with postgres_migrations_directory() as migrations:
            self.repository.apply_migrations(migrations)
        self.repository.initialize_bootstrap(self.bootstrap_config, self.settings.actor_id)

    def _require_database(self) -> None:
        if self.repository.status().get("status") != "healthy":
            raise RuntimeError("PostgreSQL is unavailable; run Docker services before using this tool")
        if self.bootstrap_error:
            self._initialize_database()
            self.bootstrap_error = ""

    def _configured_job_scope(
        self,
        *,
        organization_id: str | None = None,
        design_group_id: str | None = None,
    ) -> tuple[str, str]:
        """Return the only Job scope this service instance is allowed to use."""
        configured_organization = str(self.bootstrap_config.get("organization_id", "")).strip()
        configured_group = str(self.bootstrap_config.get("design_group_id", "")).strip()
        if not configured_organization or not configured_group:
            raise RuntimeError("configured Job organization and design group are required")
        if organization_id is not None and organization_id != configured_organization:
            raise PermissionError("organization_id does not match the configured organization")
        if design_group_id is not None and design_group_id != configured_group:
            raise PermissionError("design_group_id does not match the configured design group")
        return configured_organization, configured_group

    @staticmethod
    def _job_reference(value: object) -> str:
        """Accept only a UUID or immutable human display ID, never a path."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("job_id is required")
        reference = value.strip()
        if "/" in reference or "\\" in reference or reference in {".", ".."}:
            raise ValueError("job_id must be a Job UUID or display ID, not a filesystem path")
        try:
            return str(UUID(reference))
        except ValueError:
            if re.fullmatch(r"JOB-\d{8}-\d{3,}", reference) is None:
                raise ValueError("job_id must be a Job UUID or display ID") from None
        return reference

    @staticmethod
    def _expected_job_revision(value: object) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        return value

    @staticmethod
    def _required_job_reason(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reason is required")
        return value.strip()

    @staticmethod
    def _require_job_confirmation(
        confirmation: object,
        *,
        job_reference: str,
        action: str,
    ) -> str:
        if not isinstance(confirmation, str):
            raise ValueError("confirmation must use the canonical Job action phrase")
        parts = confirmation.split()
        if len(parts) == 2:
            try:
                parts[1] = str(UUID(parts[1]))
            except ValueError:
                pass
        expected = (action, job_reference)
        if tuple(parts) != expected:
            raise ValueError(
                f"confirmation must equal the canonical phrase: {action} {job_reference}"
            )
        return " ".join(parts)

    def _resolve_job_reference(
        self,
        job_reference: object,
        *,
        organization_id: str,
        design_group_id: str,
    ) -> str:
        """Resolve a UUID/display identity only within the authorized scope."""
        reference = self._job_reference(job_reference)
        try:
            return str(UUID(reference))
        except ValueError:
            candidates = self.design_jobs.resolve(
                organization_id=organization_id,
                design_group_id=design_group_id,
                query=reference,
                statuses=("active", "blocked", "completed", "cancelled", "archived"),
            )
        matches = [candidate for candidate in candidates if candidate.display_id == reference]
        if len(matches) == 1:
            return str(matches[0].job_id)
        if len(matches) > 1:
            raise JobFailure(
                "JOB_AMBIGUOUS",
                "Job identity is ambiguous; use the immutable Job UUID",
            )
        raise JobFailure(
            "JOB_NOT_FOUND_OR_UNAUTHORIZED",
            "Job identity is unknown or outside the authorized scope",
        )

    @staticmethod
    def _job_manifest_response(manifest: DesignJobManifest) -> dict[str, object]:
        return manifest.as_dict()

    def design_job_create(
        self,
        *,
        job_type: str,
        title: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        idempotency_token: str,
        source_files: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        """Create one scoped Job; product operations never create a Git worktree."""
        self._require_database()
        organization, group = self._configured_job_scope(
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if source_files is not None and not isinstance(source_files, (list, tuple)):
            raise JobFailure(
                "JOB_SOURCE_FILES_INVALID",
                "source_files must be an ordered list of CAD source references",
            )
        sources = tuple(source_files or ())
        if sources:
            if job_type != "mechanical_design":
                raise JobFailure(
                    "JOB_SOURCE_FILES_UNSUPPORTED_JOB_TYPE",
                    "source-file staging is supported only for mechanical-design Jobs",
                )
            if len(sources) != 1:
                raise JobFailure(
                    "JOB_SOURCE_FILES_COUNT_INVALID",
                    "source-file staging requires exactly one CAD source",
                )
            source = sources[0]
            if (
                not isinstance(source, str)
                or not source.strip()
                or Path(source.strip()).suffix.casefold()
                not in {".fcstd", ".step", ".stp"}
            ):
                raise JobFailure(
                    "JOB_SOURCE_FILE_INVALID",
                    "the staged source must be one FCStd or STEP path",
                )
        manifest = self.design_jobs.create(
            job_type=job_type,
            title=title,
            organization_id=organization,
            design_group_id=group,
            family_id=family_id,
            idempotency_token=idempotency_token,
            actor_id=self.settings.actor_id,
        )
        if sources:
            job = self._job_manifest_response(manifest)
            return {
                "schema_version": "MechanicalDesignJobSourceBinding/v1",
                "status": "staged",
                "job": job,
                "source_file_count": 1,
                "next_action": "design_job_working_copy_create",
                "next_action_arguments": {
                    "job_id": job["job_id"],
                    "expected_job_revision": job["revision"],
                    "organization_id": organization,
                    "design_group_id": group,
                    "family_id": family_id,
                    "model_revision_id": None,
                },
                "required_arguments": ["source_path"],
            }
        return self._job_manifest_response(manifest)

    def design_job_list(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        family_id: str | None = None,
    ) -> dict[str, object]:
        """List only Jobs in the configured authorized scope."""
        self._require_database()
        organization, group = self._configured_job_scope()
        jobs = self.design_jobs.list(
            organization_id=organization,
            design_group_id=group,
            status=status,
            job_type=job_type,
            family_id=family_id,
        )
        return {
            "schema_version": "MechanicalDesignJobList/v1",
            "jobs": [self._job_manifest_response(job) for job in jobs],
        }

    def design_job_get(self, *, job_id: str) -> dict[str, object]:
        """Read one UUID/display Job identity after scope authorization."""
        self._require_database()
        organization, group = self._configured_job_scope()
        resolved_job_id = self._resolve_job_reference(
            job_id,
            organization_id=organization,
            design_group_id=group,
        )
        return self._job_manifest_response(
            self.design_jobs.get(
                job_id=resolved_job_id,
                organization_id=organization,
                design_group_id=group,
            )
        )

    def design_job_resolve(
        self,
        *,
        query: str,
        job_type: str | None = None,
        family_id: str | None = None,
        statuses: tuple[str, ...] = ("active", "blocked"),
    ) -> dict[str, object]:
        """Return all authorized candidates and never select a Job implicitly."""
        self._require_database()
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        if not isinstance(statuses, tuple) or not all(
            isinstance(status, str) and status.strip() for status in statuses
        ):
            raise ValueError("statuses must be a non-empty tuple of status strings")
        organization, group = self._configured_job_scope()
        candidates = self.design_jobs.resolve(
            organization_id=organization,
            design_group_id=group,
            query=query.strip(),
            job_type=job_type,
            family_id=family_id,
            statuses=statuses,
        )
        return {
            "schema_version": "MechanicalDesignJobResolution/v1",
            "query": query.strip(),
            "candidates": [self._job_manifest_response(candidate) for candidate in candidates],
        }

    def design_job_close(
        self,
        *,
        job_id: str,
        expected_revision: int,
        status: str,
        phase: str,
        reason: str,
        confirmation: str,
    ) -> dict[str, object]:
        """Close a Job only with its revision, reason, and user confirmation."""
        self._require_database()
        reference = self._job_reference(job_id)
        revision = self._expected_job_revision(expected_revision)
        transition_reason = self._required_job_reason(reason)
        self._require_job_confirmation(
            confirmation, job_reference=reference, action="关闭"
        )
        organization, group = self._configured_job_scope()
        resolved_job_id = self._resolve_job_reference(
            reference,
            organization_id=organization,
            design_group_id=group,
        )
        return self._job_manifest_response(
            self.design_jobs.close(
                job_id=resolved_job_id,
                organization_id=organization,
                design_group_id=group,
                expected_revision=revision,
                status=status,
                phase=phase,
                actor_id=self.settings.actor_id,
                reason=transition_reason,
            )
        )

    def design_job_reopen(
        self,
        *,
        job_id: str,
        expected_revision: int,
        phase: str,
        reason: str,
        confirmation: str,
    ) -> dict[str, object]:
        """Reopen a terminal Job only with its revision and user confirmation."""
        self._require_database()
        reference = self._job_reference(job_id)
        revision = self._expected_job_revision(expected_revision)
        transition_reason = self._required_job_reason(reason)
        self._require_job_confirmation(
            confirmation, job_reference=reference, action="重开"
        )
        organization, group = self._configured_job_scope()
        resolved_job_id = self._resolve_job_reference(
            reference,
            organization_id=organization,
            design_group_id=group,
        )
        return self._job_manifest_response(
            self.design_jobs.reopen(
                job_id=resolved_job_id,
                organization_id=organization,
                design_group_id=group,
                expected_revision=revision,
                phase=phase,
                actor_id=self.settings.actor_id,
                reason=transition_reason,
            )
        )

    def design_job_doctor(self, *, job_id: str) -> dict[str, object]:
        """Inspect one authorized Job projection without changing it."""
        self._require_database()
        organization, group = self._configured_job_scope()
        resolved_job_id = self._resolve_job_reference(
            job_id,
            organization_id=organization,
            design_group_id=group,
        )
        return self.design_jobs.doctor(
            job_id=resolved_job_id,
            organization_id=organization,
            design_group_id=group,
        )

    def design_job_repair(
        self,
        *,
        job_id: str,
        expected_revision: int,
        doctor_receipt_sha256: str,
        reason: str,
        confirmation: str,
    ) -> dict[str, object]:
        """Perform only receipt-bound, identity-preserving Job repair."""
        self._require_database()
        reference = self._job_reference(job_id)
        revision = self._expected_job_revision(expected_revision)
        repair_reason = self._required_job_reason(reason)
        self._require_job_confirmation(
            confirmation, job_reference=reference, action="修复"
        )
        if not isinstance(doctor_receipt_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", doctor_receipt_sha256
        ) is None:
            raise ValueError("doctor_receipt_sha256 must be a SHA-256 digest")
        organization, group = self._configured_job_scope()
        resolved_job_id = self._resolve_job_reference(
            reference,
            organization_id=organization,
            design_group_id=group,
        )
        repaired = self.design_jobs.repair(
            job_id=resolved_job_id,
            organization_id=organization,
            design_group_id=group,
            actor_id=self.settings.actor_id,
            expected_revision=revision,
            doctor_receipt_hash=doctor_receipt_sha256,
            reason=repair_reason,
        )
        return repaired.as_dict()

    def system_status(self) -> dict[str, Any]:
        try:
            freecad = subprocess.run(
                [str(self.settings.freecadcmd), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
                check=False,
            )
            freecad_status = {
                "status": "healthy" if freecad.returncode == 0 else "failed",
                "path": str(self.settings.freecadcmd),
                "version_output": freecad.stdout.strip(),
            }
        except Exception as exc:
            freecad_status = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        return {
            "schema_version": "MechanicalDesignSystemStatus/v1",
            "semantic_reasoner": "current-codex-task-only",
            "external_language_model_api": False,
            "postgresql": self.repository.status(),
            "neo4j": self.projection.status(),
            "freecadcmd": freecad_status,
            "interactive_freecad_mcp": {
                "status": "external_not_probed",
                "required_for": "recommended_interactive_freecad_workflow",
                "bundled": False,
                "backend_dependency": False,
                "documentation": "docs/FREECAD_GUI_MCP_INTEGRATION.md",
                "validation": "independent_release_e2e",
            },
            "artifact_store": {"status": "healthy", "root": str(self.settings.artifact_root)},
            "bootstrap_error": self.bootstrap_error,
            "family_config": str(self.settings.family_config_path),
            "library_root_configured": bool(self.bootstrap_config.get("library_root")),
        }

    def family_bootstrap_get(self) -> dict[str, Any]:
        database_value = None
        database_error = ""
        try:
            database_value = self.repository.get_family(self.bootstrap_config["family_id"])
        except Exception as exc:
            database_error = f"{type(exc).__name__}: {exc}"
        return {"file_config": self.bootstrap_config, "database_record": database_value, "database_error": database_error}

    def family_bootstrap_update(self, patch: dict[str, Any], confirmation: str) -> dict[str, Any]:
        self._require_database()
        immutable = {"organization_id", "design_group_id", "family_id", "schema_version"}
        if immutable & patch.keys():
            raise ValueError(f"immutable bootstrap fields cannot be changed: {sorted(immutable & patch.keys())}")
        if self.bootstrap_config["family_id"] not in confirmation or "更新" not in confirmation:
            raise ValueError("confirmation must include the family_id and 更新")
        updated = json.loads(json.dumps(self.bootstrap_config, ensure_ascii=False))
        for key, value in patch.items():
            updated[key] = value
        if updated.get("subfamily_mode") != "discover-and-confirm":
            raise ValueError("subfamily discovery cannot be disabled")
        if int(updated.get("question_batch_limit", 0)) not in range(1, 6):
            raise ValueError("question_batch_limit must be between 1 and 5")
        self._atomic_write_json(self.settings.family_config_path, updated)
        self.bootstrap_config = updated
        record = self.repository.update_family_config(updated["family_id"], updated)
        return {"file_config": updated, "database_record": record}

    def design_group_register(self, design_group_id: str, design_group_name: str, confirmation: str) -> dict[str, Any]:
        self._require_database()
        if design_group_id not in confirmation or design_group_name not in confirmation or "确认" not in confirmation:
            raise ValueError("confirmation must include design_group_id, design_group_name, and 确认")
        return self.repository.upsert_design_group(
            self.bootstrap_config["organization_id"], design_group_id, design_group_name
        )

    def family_create(
        self,
        *,
        family_id: str,
        family_name: str,
        design_group_id: str,
        aliases: list[str],
        confirmation: str,
    ) -> dict[str, Any]:
        self._require_database()
        if family_id not in confirmation or family_name not in confirmation or "创建" not in confirmation:
            raise ValueError("confirmation must include family_id, family_name, and 创建")
        if not family_id.strip() or not family_name.strip() or not design_group_id.strip():
            raise ValueError("family_id, family_name, and design_group_id are required")
        config = {
            "schema_version": "product-family-bootstrap/v1",
            "organization_id": self.bootstrap_config["organization_id"],
            "design_group_id": design_group_id,
            "family_id": family_id,
            "family_name": family_name.strip(),
            "aliases": sorted({item.strip() for item in aliases if item.strip()}),
            "status": "awaiting-source-folder",
            "subfamily_mode": "discover-and-confirm",
            "expected_initial_models": {"minimum": 1, "maximum": 9},
            "question_batch_limit": 5,
            "minimum_distinct_models_for_generalization": 3,
            "family_owner_actor_id": self.settings.actor_id,
            "knowledge_scope_policy": "family-isolated-explicit-promotion",
            "specialized_context_policy": "explicit-family-authorization-only",
            "source_formats": [".step", ".stp", ".FCStd"],
            "design_policy": {
                "priority": "existing-product-modification-first",
                "working_copy_required": True,
                "approval_before_delivery": True,
            },
            "validation_policy": {
                "geometry": True,
                "interfaces": True,
                "rules": True,
                "units": True,
                "basic_engineering_calculations": True,
                "fem_required": False,
            },
            "library_root": None,
            "subfamily": None,
        }
        return self.repository.create_family(config)

    def library_register(self, root_path: str) -> dict[str, Any]:
        self._require_database()
        root = self.scanner.register(root_path)
        workspace = self.settings.workspace.resolve()
        if root == workspace or workspace.is_relative_to(root):
            raise ValueError("CAD library root cannot be the workspace or one of its ancestor directories")
        for path in (
            self.settings.workspace / "output",
            self.settings.package_root / "data",
            self.settings.workspace / "knowledge",
        ):
            resolved = path.resolve()
            if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
                raise ValueError(
                    "CAD library root must be a dedicated source directory and cannot include project outputs, "
                    "artifacts, or existing knowledge"
                )
        config = self.bootstrap_config
        record = self.repository.register_library(
            organization_id=config["organization_id"],
            design_group_id=config["design_group_id"],
            root_path=str(root),
            actor_id=self.settings.actor_id,
        )
        updated = dict(config)
        updated["library_root"] = str(root)
        updated["status"] = "awaiting-family-folder-confirmation"
        self._atomic_write_json(self.settings.family_config_path, updated)
        self.bootstrap_config = updated
        self.repository.update_family_config(updated["family_id"], updated)
        return {"library": record, "family_config_status": updated["status"], "read_only": True}

    def evidence_artifact_register(self, file_path: str, media_type: str) -> dict[str, Any]:
        self._require_database()
        source = Path(file_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("evidence artifact must be a regular file")
        artifact = self.artifacts.ingest_file(source)
        record = self.repository.register_evidence_artifact(
            self.bootstrap_config["organization_id"], artifact, media_type.strip() or "application/octet-stream"
        )
        return {"artifact": record, "content_addressed_copy": artifact["storage_path"]}

    def library_scan(self, library_id: str = "") -> dict[str, Any]:
        self._require_database()
        library = self.repository.get_library(library_id or None)
        root = Path(library["root_path"])
        current = self.scanner.inventory(root)
        previous_rows = self.repository.list_library_files(str(library["id"]))
        previous = [
            {
                key: row[key]
                for key in (
                    "relative_path",
                    "absolute_path",
                    "family_folder",
                    "sha256",
                    "size_bytes",
                    "modified_at_ns",
                    "suffix",
                )
            }
            for row in previous_rows
            if row.get("missing_at") is None
        ]
        changes = self.scanner.diff(current, previous)
        self.repository.record_scan(str(library["id"]), current)
        pending = self.repository.pending_library_files(str(library["id"]))
        mappings = self.repository.folder_mappings(str(library["id"]))
        conflicts = self.repository.family_assignment_conflicts(str(library["id"]))
        return {
            "library_id": str(library["id"]),
            "root_path": str(root),
            "read_only": True,
            "changes": [scan_change_dict(item) for item in changes if item.kind != "unchanged"],
            "pending_ingest": pending,
            "family_folder_mappings": mappings,
            "family_assignment_conflicts": conflicts,
            "automatic_ingest": False,
        }

    def family_folder_confirm(
        self, library_id: str, folder_name: str, family_id: str, confirmation: str
    ) -> dict[str, Any]:
        self._require_database()
        if family_id not in confirmation or folder_name not in confirmation or "确认" not in confirmation:
            raise ValueError("confirmation must include folder_name, family_id, and 确认")
        mapping = self.repository.confirm_folder_mapping(
            library_id, folder_name, family_id, self.settings.actor_id
        )
        if family_id == self.bootstrap_config["family_id"]:
            updated = dict(self.bootstrap_config)
            updated["status"] = "ready-for-manual-ingest"
            self._atomic_write_json(self.settings.family_config_path, updated)
            self.bootstrap_config = updated
            self.repository.update_family_config(family_id, updated)
        return mapping

    def library_ingest_changes(
        self,
        selection: list[str],
        library_id: str = "",
        wait_for_completion: bool = False,
        reanalyze: bool = False,
    ) -> dict[str, Any]:
        self._require_database()
        if not selection:
            raise ValueError("selection must list at least one pending relative path")
        library = self.repository.get_library(library_id or None)
        if reanalyze:
            eligible = {
                row["relative_path"]: row
                for row in self.repository.list_library_files(str(library["id"]))
                if row.get("missing_at") is None
                and row.get("model_revision_id")
                and row.get("ingestion_status") == "ingested"
            }
        else:
            eligible = {
                row["relative_path"]: row
                for row in self.repository.pending_library_files(str(library["id"]))
            }
        unknown = sorted(set(selection) - set(eligible))
        if unknown:
            state = "eligible ingested paths" if reanalyze else "pending paths"
            raise ValueError(f"selection contains paths that are not {state}: {unknown}")
        unmapped = sorted(
            path
            for path in selection
            if self.repository.family_for_folder(str(library["id"]), eligible[path]["family_folder"]) is None
        )
        if unmapped:
            raise ValueError(
                "ingestion is blocked until each first-level family folder has an engineer-confirmed mapping: "
                f"{unmapped}"
            )
        job_id = self.repository.create_job(str(library["id"]), selection)
        future = self.executor.submit(
            self._run_ingestion_job,
            job_id,
            dict(library),
            [eligible[path] for path in selection],
        )
        with self.futures_lock:
            self.futures[job_id] = future
        if wait_for_completion:
            future.result()
        return self.repository.get_job(job_id)

    def _run_ingestion_job(self, job_id: str, library: dict[str, Any], records: list[dict[str, Any]]) -> None:
        self.repository.update_job(job_id, "running")
        results = []
        try:
            for record in records:
                source = Path(record["absolute_path"]).resolve(strict=True)
                expected_root = Path(library["root_path"]).resolve(strict=True)
                source.relative_to(expected_root)
                artifact = self.artifacts.ingest_file(source)
                staging_manifest = (
                    self.settings.artifact_root
                    / "manifests"
                    / ".staging"
                    / f"{artifact['sha256']}.{job_id}.json"
                )
                manifest = self.extractor.extract(source, staging_manifest)
                parser_key = stable_hash(manifest["parser_version"])[:16]
                manifest_path = (
                    self.settings.artifact_root
                    / "manifests"
                    / str(artifact["sha256"])
                    / f"{parser_key}.json"
                )
                ensure_managed_directory(
                    manifest_path.parent,
                    parents=True,
                    exist_ok=True,
                )
                atomic_publish_owned_file(
                    staging_manifest,
                    manifest_path,
                    replace_existing=True,
                )
                family_id = self.repository.family_for_folder(str(library["id"]), record["family_folder"])
                if family_id is None:
                    raise RuntimeError("family folder mapping changed or was removed while ingestion was running")
                family = self.repository.get_family(family_id)
                model_id = self.repository.save_model_analysis(
                    library_id=str(library["id"]),
                    file_record=record,
                    artifact=artifact,
                    manifest=manifest,
                    organization_id=library["organization_id"],
                    design_group_id=family["design_group_id"],
                    family_id=family_id,
                )
                results.append(
                    {
                        "relative_path": record["relative_path"],
                        "model_revision_id": model_id,
                        "family_id": family_id,
                        "family_assignment_status": "confirmed-folder-mapping" if family_id else "awaiting-folder-confirmation",
                        "manifest_path": str(manifest_path),
                        "diagnostics": manifest.get("diagnostics", []),
                    }
                )
            projection = self._safe_projection()
            self.repository.update_job(job_id, "completed", result={"models": results, "projection": projection})
        except Exception as exc:
            self.repository.update_job(job_id, "failed", result={"models": results}, error=f"{type(exc).__name__}: {exc}")
            raise

    def job_get(self, job_id: str) -> dict[str, Any]:
        self._require_database()
        return self.repository.get_job(job_id)

    def model_get_analysis(self, model_revision_id: str) -> dict[str, Any]:
        self._require_database()
        model = self.repository.get_model_analysis(model_revision_id)
        manifest = model["manifest"]
        return {
            "model_revision": {key: value for key, value in model.items() if key != "manifest"},
            "manifest": manifest,
            "summary": {
                "source_node_count": len(manifest.get("source_nodes", [])),
                "shape_definition_count": len(manifest.get("shape_definitions", [])),
                "solid_fragment_count": len(manifest.get("solid_fragments", [])),
                "relation_candidate_count": len(manifest.get("relation_candidates", [])),
                "structure_hypothesis_count": len(manifest.get("structure_hypotheses", [])),
            },
        }

    def learning_start_session(self, model_revision_id: str) -> dict[str, Any]:
        self._require_database()
        model = self.repository.get_model_analysis(model_revision_id)
        session_id = self.repository.create_session(
            organization_id=model["organization_id"],
            design_group_id=model["design_group_id"],
            family_id=model.get("family_id"),
            model_revision_id=model_revision_id,
            actor_id=self.settings.actor_id,
        )
        return {"session_id": session_id, "model_revision_id": model_revision_id, "agent_runtime": "codex-current"}

    def learning_next_targets(self, session_id: str) -> dict[str, Any]:
        self._require_database()
        session = self.repository.session(session_id)
        limit = int(self.bootstrap_config.get("question_batch_limit", 5))
        existing = self.repository.open_questions(session_id, limit)
        if existing:
            return {"session_id": session_id, "targets": existing, "maximum": limit, "reused_open_targets": True}
        if not session.get("model_revision_id"):
            raise ValueError("learning session is not attached to a model revision")
        model = self.repository.get_model_analysis(str(session["model_revision_id"]))
        candidates = generate_question_targets(
            model["manifest"],
            family_confirmed=bool(model.get("family_id")),
            limit=limit,
            excluded_signatures=self.repository.session_question_signatures(session_id),
        )
        created = self.repository.replace_open_questions(session_id, candidates)
        return {"session_id": session_id, "targets": created, "maximum": limit, "reused_open_targets": False}

    def learning_defer_targets(
        self, session_id: str, question_ids: list[str], reason: str
    ) -> list[dict[str, Any]]:
        self._require_database()
        if not reason.strip():
            raise ValueError("defer reason is required")
        return self.repository.defer_questions(session_id, question_ids, reason.strip())

    def learning_record_exchange(
        self,
        *,
        session_id: str,
        question_ids: list[str],
        engineer_text: str,
        agent_interpretation: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_database()
        if not engineer_text.strip():
            raise ValueError("engineer_text is required")
        content_hash = stable_hash(
            {
                "session_id": session_id,
                "question_ids": question_ids,
                "engineer_text": engineer_text.strip(),
                "agent_interpretation": agent_interpretation,
            }
        )
        return self.repository.record_exchange(
            session_id=session_id,
            question_ids=question_ids,
            engineer_text=engineer_text.strip(),
            agent_interpretation=agent_interpretation,
            actor_id=self.settings.actor_id,
            content_sha256=content_hash,
        )

    def knowledge_propose_assertions(self, session_id: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._require_database()
        return self.repository.propose_assertions(
            session_id=session_id,
            proposals=parse_assertion_proposals(raw),
            actor_id=self.settings.actor_id,
        )

    def knowledge_review(
        self,
        assertion_id: str,
        decision: str,
        reviewer_text: str,
        corrected_object_value: Any | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        result = self.repository.review_assertion(
            assertion_id,
            decision,
            self.settings.actor_id,
            reviewer_text,
            corrected_object_value,
        )
        projection = self._safe_projection()
        return {"assertion": result, "projection": projection}

    def knowledge_search(
        self,
        *,
        query: str,
        organization_id: str,
        design_group_id: str,
        requested_family_id: str | None = None,
        model_revision_id: str | None = None,
        explicit_family_authorization: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        self._require_database()
        authorized_family = None
        basis = "organization-general-only"
        if model_revision_id:
            model = self.repository.get_model_analysis(model_revision_id)
            if model["organization_id"] != organization_id or model["design_group_id"] != design_group_id:
                raise ValueError("model does not belong to the requested organization/design group")
            authorized_family = model.get("family_id")
            basis = "confirmed-source-model-family" if authorized_family else "model-facts-and-organization-general"
            if requested_family_id and requested_family_id != authorized_family:
                raise ValueError("requested family conflicts with the confirmed model family")
        elif requested_family_id and explicit_family_authorization:
            family = self.repository.get_family(requested_family_id)
            if family["organization_id"] != organization_id or family["design_group_id"] != design_group_id:
                raise ValueError("requested family does not belong to the requested organization/design group")
            authorized_family = requested_family_id
            basis = "explicit-current-session-family-authorization"
        results = self.repository.search_approved_knowledge(
            query=query,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=authorized_family,
            model_revision_id=model_revision_id,
            limit=limit,
        )
        return {
            "query": query,
            "authorization_basis": basis,
            "authorized_family_id": authorized_family,
            "results": results,
            "specialized_knowledge_enabled": authorized_family is not None,
        }

    def model_identity_confirm(
        self,
        *,
        model_revision_id: str,
        family_id: str,
        canonical_name: str,
        aliases: list[str],
        approved_assertion_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        self._require_database()
        required = (model_revision_id, family_id, canonical_name, approved_assertion_id)
        if any(item not in confirmation for item in required) or "确认" not in confirmation:
            raise ValueError(
                "confirmation must include model_revision_id, family_id, canonical_name, approved_assertion_id, and 确认"
            )
        return self.repository.confirm_model_identity(
            model_revision_id=model_revision_id,
            family_id=family_id,
            canonical_name=canonical_name.strip(),
            aliases=sorted({item.strip() for item in aliases if item.strip()}),
            approved_assertion_id=approved_assertion_id,
        )

    def family_compare_models(self, family_id: str) -> dict[str, Any]:
        self._require_database()
        comparison = family_statistics(self.repository.family_manifests(family_id))
        pairs = self.repository.family_similarity_pairs(family_id)
        comparison["similarity_pairs"] = pairs
        comparison["subfamily_candidate_pairs"] = [
            item
            for item in pairs
            if float(item["geometry_similarity"] or 0) >= 0.90
            and float(item["structure_similarity"] or 0) >= 0.90
        ]
        comparison["subfamily_note"] = (
            "Similarity only proposes comparison targets. A current Codex discussion and family-owner review "
            "are required before any subfamily exists."
        )
        return comparison

    def subfamily_propose(
        self,
        *,
        subfamily_id: str,
        family_id: str,
        canonical_name: str,
        aliases: list[str],
        model_revision_ids: list[str],
        evidence: list[dict[str, Any]],
        confirmation: str,
    ) -> dict[str, Any]:
        self._require_database()
        if subfamily_id not in confirmation or canonical_name not in confirmation or "提议" not in confirmation:
            raise ValueError("confirmation must include subfamily_id, canonical_name, and 提议")
        if not evidence:
            raise ValueError("subfamily proposal requires comparison and engineer evidence")
        return self.repository.propose_subfamily(
            subfamily_id=subfamily_id,
            family_id=family_id,
            canonical_name=canonical_name,
            aliases=sorted({item.strip() for item in aliases if item.strip()}),
            model_revision_ids=model_revision_ids,
            evidence=evidence,
            actor_id=self.settings.actor_id,
        )

    def subfamily_review(self, subfamily_id: str, decision: str, confirmation: str) -> dict[str, Any]:
        self._require_database()
        word = "批准" if decision == "approve" else "拒绝"
        if subfamily_id not in confirmation or word not in confirmation:
            raise ValueError("confirmation must include subfamily_id and matching Chinese decision word")
        result = self.repository.review_subfamily(subfamily_id, decision, self.settings.actor_id)
        return {"subfamily": result, "projection": self._safe_projection()}

    def subfamily_get(self, family_id: str) -> list[dict[str, Any]]:
        self._require_database()
        return self.repository.family_subfamilies(family_id)

    def family_profile_propose(
        self, family_id: str, profile: dict[str, Any], evidence: list[dict[str, Any]], source_kind: str = "statistical"
    ) -> dict[str, Any]:
        self._require_database()
        if source_kind not in {"statistical", "expert_declared"}:
            raise ValueError("source_kind must be statistical or expert_declared")
        model_count = self.repository.family_model_count(family_id)
        minimum = int(self.bootstrap_config.get("minimum_distinct_models_for_generalization", 3))
        if source_kind != "expert_declared" and model_count < minimum:
            raise ValueError(
                f"statistical family profile requires at least {minimum} distinct models; current count is {model_count}"
            )
        if not evidence:
            raise ValueError("family profile evidence is required")
        if source_kind == "expert_declared":
            answer_ids = [
                str(item["answer_event_id"])
                for item in evidence
                if isinstance(item, dict) and item.get("answer_event_id")
            ]
            self.repository.validate_family_answer_evidence(family_id, answer_ids)
        payload = dict(profile)
        payload["source_kind"] = source_kind
        payload["minimum_generalization_models"] = minimum
        return self.repository.save_family_profile(family_id, payload, evidence, "proposed", self.settings.actor_id)

    def family_profile_review(self, profile_id: str, decision: str, confirmation: str) -> dict[str, Any]:
        self._require_database()
        result = self.repository.review_family_profile(profile_id, decision, self.settings.actor_id, confirmation)
        projection = self._safe_projection()
        return {"profile": result, "projection": projection}

    def family_profile_get(self, family_id: str) -> dict[str, Any] | None:
        self._require_database()
        return self.repository.family_profile(family_id)

    def design_context_build(self, **kwargs: Any) -> dict[str, Any]:
        configured_organization = str(self.bootstrap_config["organization_id"])
        requested_organization = str(kwargs.get("organization_id", ""))
        if requested_organization != configured_organization:
            raise PermissionError("organization_id does not match the configured organization")
        self._require_database()
        design_group_id = str(kwargs.get("design_group_id", ""))
        design_group = self.repository.get_design_group(design_group_id)
        if str(design_group["organization_id"]) != configured_organization:
            raise PermissionError("design group does not belong to the configured organization")
        kwargs["organization_id"] = configured_organization
        return self.context_builder.build(**kwargs)

    def design_lesson_stage(
        self,
        package: dict[str, Any],
        evidence_items: list[dict[str, str]],
        *,
        review_revision: bool = False,
    ) -> dict[str, Any]:
        """Write an immutable local review package without touching PostgreSQL."""
        if package.get("status") == "approved":
            raise ValueError("status is assigned by approval and cannot be supplied by the caller")
        if review_revision:
            return self.design_lesson_staging.stage_review(package, evidence_items)
        return self.design_lesson_staging.stage(package, evidence_items)

    def design_lesson_staged_get(self, lesson_id: str) -> dict[str, Any]:
        return self.design_lesson_staging.get(lesson_id)

    def design_lesson_approve(
        self,
        *,
        lesson_id: str,
        expected_package_sha256: str,
        reviewer_text: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = f"批准 {lesson_id} {expected_package_sha256}"
        if confirmation.strip() != expected_confirmation:
            raise ValueError(
                "confirmation must include lesson_id, SHA-256, and 批准 using canonical confirmation: "
                + expected_confirmation
            )
        return self._approve_verified_design_lesson(
            lesson_id=lesson_id,
            expected_package_sha256=expected_package_sha256,
            reviewer_text=reviewer_text,
        )

    def design_lesson_review_approve(
        self, *, review_id: str, reviewer_text: str, confirmation: str
    ) -> dict[str, Any]:
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = f"批准设计经验 {review_id}"
        if confirmation != expected_confirmation:
            raise ValueError(
                "confirmation must use canonical confirmation: "
                + expected_confirmation
            )
        self._require_database()
        review = self.repository.get_design_lesson_review(review_id)
        self._require_design_lesson_review_scope(review)
        if review["status"] == "stored-and-retrievable":
            lesson = self.repository.get_design_lesson(
                str(review["published_design_lesson_id"]),
                organization_id=self.bootstrap_config["organization_id"],
            )
            probe = review.get("retrieval_probe") or {}
            return self._design_lesson_review_result(
                review=review,
                lesson=lesson,
                projection=probe.get("projection"),
                retrieval_match=probe.get("match"),
                idempotent=True,
            )
        if review["status"] == "approved-retrieval-pending":
            return self._complete_design_lesson_review(
                review_id, idempotent=True
            )
        if review["status"] != "awaiting-engineer-review":
            raise ValueError(
                "design lesson review must be awaiting-engineer-review"
            )
        try:
            approved = self._approve_verified_design_lesson(
                lesson_id=str(review["lesson_id"]),
                expected_package_sha256=str(review["package_sha256"]),
                reviewer_text=reviewer_text,
                review_id=review_id,
                review=review,
            )
        except (
            ImmutableReviewBindingDriftError,
            ArtifactChecksumMismatchError,
        ) as error:
            try:
                self.repository.invalidate_design_lesson_review(
                    review_id=review_id,
                    reviewer_id=self.settings.actor_id,
                    reason="immutable review binding verification failed",
                )
            except Exception as invalidation_error:
                error.add_note(
                    "review invalidation failed: "
                    f"{type(invalidation_error).__name__}"
                )
            raise
        return self._complete_design_lesson_review(
            review_id, lesson=approved["lesson"]
        )

    def design_lesson_get(self, lesson_id: str) -> dict[str, Any]:
        self._require_database()
        lesson = self.repository.get_design_lesson(
            lesson_id, organization_id=self.bootstrap_config["organization_id"]
        )
        design_lesson_ref = DesignContextBuilder._opaque_lesson_ref(lesson)
        return {
            "schema_version": "DesignLessonGet/v1",
            "lesson": DesignContextBuilder._render_lesson(
                lesson,
                source_family_authorized=False,
                design_lesson_ref=design_lesson_ref,
            ),
        }

    def design_lesson_audit_get(self, lesson_id: str, confirmation: str) -> dict[str, Any]:
        expected_confirmation = f"审计 {lesson_id}"
        if confirmation.strip() != expected_confirmation:
            raise ValueError(
                "confirmation must use canonical confirmation: " + expected_confirmation
            )
        self._require_database()
        return self.repository.get_design_lesson_audit(
            lesson_id=lesson_id,
            organization_id=self.bootstrap_config["organization_id"],
            reviewer_id=self.settings.actor_id,
        )

    def design_lesson_search(
        self,
        query: str = "",
        *,
        design_group_id: str | None = None,
        family_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._require_database()
        return self.repository.search_approved_design_lessons(
            organization_id=self.bootstrap_config["organization_id"],
            query=query,
            design_group_id=design_group_id,
            family_id=family_id,
            limit=limit,
        )

    def design_lesson_search_page(
        self,
        query: str = "",
        *,
        design_group_id: str | None = None,
        family_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        return self.repository.search_approved_design_lesson_page(
            organization_id=self.bootstrap_config["organization_id"],
            query=query,
            design_group_id=design_group_id,
            family_id=family_id,
            page_size=limit,
            cursor=cursor,
        )

    def design_lesson_supersede(
        self,
        *,
        lesson_id: str,
        replacement_lesson_id: str,
        expected_package_sha256: str,
        reviewer_text: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = (
            f"替代 {lesson_id} -> {replacement_lesson_id} {expected_package_sha256}"
        )
        if confirmation.strip() != expected_confirmation:
            raise ValueError(
                "confirmation must include both lesson ids, SHA-256, and 替代 using canonical confirmation: "
                + expected_confirmation
            )
        return self._approve_verified_design_lesson(
            lesson_id=replacement_lesson_id,
            expected_package_sha256=expected_package_sha256,
            reviewer_text=reviewer_text,
            supersedes_lesson_id=lesson_id,
        )

    def design_lesson_revoke(
        self, *, lesson_id: str, reason: str, confirmation: str
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        expected_confirmation = f"撤销 {lesson_id}"
        if confirmation.strip() != expected_confirmation:
            raise ValueError(
                "confirmation must include lesson_id and 撤销 using canonical confirmation: "
                + expected_confirmation
            )
        self._require_database()
        lesson = self.repository.revoke_design_lesson(
            lesson_id=lesson_id,
            reviewer_id=self.settings.actor_id,
            reviewer_text=reason,
        )
        return {"lesson": lesson, "projection": self._safe_projection()}

    def _approve_verified_design_lesson(
        self,
        *,
        lesson_id: str,
        expected_package_sha256: str,
        reviewer_text: str,
        supersedes_lesson_id: str | None = None,
        review_id: str | None = None,
        review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        verified_review_card_sha256 = None
        verified_review_path = None
        verified_package_path = None
        if review_id is None:
            existing = self.repository.existing_design_lesson_approval(
                package_sha256=expected_package_sha256,
                reviewer_id=self.settings.actor_id,
                organization_id=self.bootstrap_config["organization_id"],
                supersedes_lesson_id=supersedes_lesson_id,
            )
            if existing is not None:
                return {
                    "lesson": existing,
                    "archived_files": {},
                    "archived_evidence": [],
                    "idempotent": True,
                    "projection": self._safe_projection(),
                }
            self.design_lesson_staging.verify(lesson_id, expected_package_sha256)
            paths = self.design_lesson_staging.package_paths(lesson_id)
        else:
            if review is None or str(review.get("id")) != review_id:
                raise ImmutableReviewBindingDriftError(
                    "design lesson review binding is required"
                )
            try:
                verified_review = self.design_lesson_reviews.verify(
                    review_id, expected_package_sha256
                )
                review_inspection = self.design_lesson_reviews.inspect(review_id)
                paths = self.design_lesson_staging.review_package_paths(
                    expected_package_sha256
                )
            except ValueError as error:
                raise ImmutableReviewBindingDriftError(str(error)) from error
            verified_review_card_sha256 = str(
                verified_review["review_card_sha256"]
            )
            verified_review_path = str(
                review_inspection["paths"]["review_card"]
            )
            verified_package_path = str(paths["lesson_json"])
            if any(
                (
                    review.get("review_card_sha256")
                    != verified_review_card_sha256,
                    review.get("review_path") != verified_review_path,
                    review.get("package_path") != verified_package_path,
                )
            ):
                raise ImmutableReviewBindingDriftError(
                    "database review binding does not match the verified local review"
                )
        archived_files: dict[str, dict[str, Any]] = {}
        for name, path in paths.items():
            try:
                archived_files[name] = self.artifacts.ingest_file(
                    path,
                    allowed_root=self.settings.workspace,
                )
            except ArtifactChecksumMismatchError as error:
                if review_id is not None:
                    raise ImmutableReviewBindingDriftError(str(error)) from error
                if name == "lesson_json":
                    raise ValueError(
                        "archived lesson package SHA-256 does not match the engineer-confirmed digest"
                    ) from error
                raise ValueError(
                    f"archived {name} content-addressed artifact is corrupt"
                ) from error
            except IOError as error:
                if name == "lesson_json":
                    raise ValueError(
                        "archived lesson package SHA-256 does not match the engineer-confirmed digest"
                    ) from error
                raise ValueError(f"archived {name} content-addressed artifact is corrupt") from error
        archived_package_path = Path(str(archived_files["lesson_json"]["storage_path"]))
        archived_package_bytes = archived_package_path.read_bytes()
        archived_package_sha256 = hashlib.sha256(archived_package_bytes).hexdigest()
        if archived_package_sha256 != expected_package_sha256:
            drift_error = (
                ImmutableReviewBindingDriftError
                if review_id is not None
                else ValueError
            )
            raise drift_error(
                "archived lesson package SHA-256 does not match the engineer-confirmed digest"
            )
        try:
            archived_package = json.loads(archived_package_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            drift_error = (
                ImmutableReviewBindingDriftError
                if review_id is not None
                else ValueError
            )
            raise drift_error("archived lesson package is not valid JSON") from None
        try:
            package = validate_design_lesson_package(archived_package)
        except ValueError as error:
            if review_id is not None:
                raise ImmutableReviewBindingDriftError(str(error)) from error
            raise
        if package["source"]["organization_id"] != self.bootstrap_config["organization_id"]:
            raise PermissionError("staged lesson source does not match the configured organization")
        if review_id is not None and any(
            (
                package["lesson_id"] != review["lesson_id"],
                package["source"]["design_group_id"] != review["design_group_id"],
                str(package["source"]["working_copy_id"])
                != str(review["working_copy_id"]),
                package["source"]["after_model_sha256"]
                != review["final_model_sha256"],
            )
        ):
            raise ImmutableReviewBindingDriftError(
                "review row does not match its immutable lesson package"
            )
        archived_manifest_path = Path(str(archived_files["evidence_manifest"]["storage_path"]))
        try:
            archived_manifest = json.loads(archived_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            drift_error = (
                ImmutableReviewBindingDriftError
                if review_id is not None
                else ValueError
            )
            raise drift_error("archived evidence manifest is not valid JSON") from None
        if archived_manifest != package["evidence_manifest"]:
            drift_error = (
                ImmutableReviewBindingDriftError
                if review_id is not None
                else ValueError
            )
            raise drift_error(
                "archived evidence manifest does not match the confirmed lesson package"
            )
        archived_evidence: list[dict[str, Any]] = []
        evidence_paths = (
            self.design_lesson_staging.evidence_paths(lesson_id)
            if review_id is None
            else self.design_lesson_staging.review_evidence_paths(
                expected_package_sha256
            )
        )
        for descriptor, evidence_path in evidence_paths:
            try:
                artifact = self.artifacts.ingest_file(
                    evidence_path,
                    allowed_root=self.settings.workspace,
                )
            except ArtifactChecksumMismatchError as error:
                if review_id is not None:
                    raise ImmutableReviewBindingDriftError(str(error)) from error
                raise
            if artifact["sha256"] != descriptor["sha256"]:
                drift_error = (
                    ImmutableReviewBindingDriftError
                    if review_id is not None
                    else ValueError
                )
                raise drift_error(
                    f"evidence artifact SHA-256 changed after verification: {descriptor['evidence_id']}"
                )
            archived_evidence.append({
                **descriptor,
                "artifact_sha256": artifact["sha256"],
                "artifact_storage_path": artifact["storage_path"],
                "artifact_source_path": artifact["source_path"],
                "artifact_size_bytes": artifact["size_bytes"],
            })
        if review_id is None:
            snapshot_context = self.design_workspace.locked_current_snapshot(
                package["source"]["working_copy_id"], self.artifacts
            )
        else:
            try:
                verified_final_artifact = self.artifacts.verify_file(
                    Path(str(review["approved_final_artifact_path"])),
                    str(review["final_model_sha256"]),
                )
            except ValueError as error:
                raise ImmutableReviewBindingDriftError(str(error)) from error
            snapshot_context = nullcontext(verified_final_artifact)
        with snapshot_context as working_copy_artifact:
            if working_copy_artifact["sha256"] != package["source"]["after_model_sha256"]:
                if review_id is None:
                    raise ValueError(
                        "current FCStd hash does not match the reviewed after-model hash"
                    )
                raise ImmutableReviewBindingDriftError(
                    "approved final artifact does not match the reviewed after-model hash"
                )

            pre_commit_verifier = None
            if review_id is not None:

                def pre_commit_verifier() -> None:
                    try:
                        self.design_lesson_reviews.verify(
                            review_id, expected_package_sha256
                        )
                        self.design_lesson_staging.review_package_paths(
                            expected_package_sha256
                        )
                        for descriptor, evidence_path in self.design_lesson_staging.review_evidence_paths(
                            expected_package_sha256
                        ):
                            if file_sha256(evidence_path) != descriptor["sha256"]:
                                raise ImmutableReviewBindingDriftError(
                                    "review evidence changed before approval commit"
                                )
                        self.artifacts.verify_file(
                            Path(str(review["approved_final_artifact_path"])),
                            str(review["final_model_sha256"]),
                        )
                    except ImmutableReviewBindingDriftError:
                        raise
                    except ValueError as error:
                        raise ImmutableReviewBindingDriftError(str(error)) from error

            lesson = self.repository.approve_design_lesson(
                package=package,
                package_sha256=expected_package_sha256,
                archived_package_path=str(archived_package_path),
                archived_evidence=archived_evidence,
                working_copy_artifact=working_copy_artifact,
                reviewer_id=self.settings.actor_id,
                reviewer_text=reviewer_text,
                supersedes_lesson_id=supersedes_lesson_id,
                review_id=review_id,
                pre_commit_verifier=pre_commit_verifier,
                verified_review_card_sha256=verified_review_card_sha256,
                verified_review_path=verified_review_path,
                verified_package_path=verified_package_path,
            )
        return {
            "lesson": lesson,
            "archived_files": archived_files,
            "archived_evidence": archived_evidence,
            "working_copy_artifact": working_copy_artifact,
            "projection": self._safe_projection() if review_id is None else None,
        }

    @staticmethod
    def _public_design_lesson_review_value(value: Any) -> Any:
        """Remove immutable-artifact internals from normal review responses."""
        if isinstance(value, dict):
            return {
                str(key): MechanicalDesignService._public_design_lesson_review_value(
                    nested
                )
                for key, nested in value.items()
                if not any(
                    marker in str(key).lower()
                    for marker in ("sha256", "digest", "hash", "path", "storage")
                )
            }
        if isinstance(value, list):
            return [
                MechanicalDesignService._public_design_lesson_review_value(item)
                for item in value
            ]
        if isinstance(value, str):
            redacted = re.sub(r"(?i)[0-9a-f]{64}", "[sha256-redacted]", value)
            return re.sub(r"(?<!:)/(?:[^\s\"']+)", "[path-redacted]", redacted)
        return value

    @staticmethod
    def _public_design_lesson_review(review: dict[str, Any]) -> dict[str, Any]:
        review_id = str(review.get("id") or review.get("review_id") or "")
        public = {
            "id": review_id,
            "review_id": review_id,
            "status": review.get("status"),
            "lesson_id": review.get("lesson_id"),
            "working_copy_id": review.get("working_copy_id"),
        }
        for field in ("supersedes_review_id", "published_design_lesson_id"):
            if field in review:
                public[field] = review[field]
        if review.get("retrieval_probe") is not None:
            public["retrieval_probe"] = review["retrieval_probe"]
        return MechanicalDesignService._public_design_lesson_review_value(public)

    @staticmethod
    def _design_lesson_review_result(
        *,
        review: dict[str, Any],
        lesson: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        retrieval_match: dict[str, Any] | None = None,
        retrieval_probe: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        return MechanicalDesignService._public_design_lesson_review_value({
            "status": review["status"],
            "review": MechanicalDesignService._public_design_lesson_review(review),
            "lesson": lesson,
            "projection": projection,
            "retrieval_match": retrieval_match,
            "retrieval_probe": retrieval_probe or review.get("retrieval_probe"),
            "failure": failure,
            "idempotent": idempotent,
        })

    def _complete_design_lesson_review(
        self,
        review_id: str,
        *,
        lesson: dict[str, Any] | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        projection = self._safe_projection()
        review = self.repository.get_design_lesson_review(review_id)
        if review["status"] != "approved-retrieval-pending":
            raise ValueError(
                "design lesson review must be approved-retrieval-pending"
            )
        lesson = lesson or self.repository.get_design_lesson(
            str(review["published_design_lesson_id"]),
            organization_id=self.bootstrap_config["organization_id"],
        )
        query = next(
            (
                value.strip()
                for value in lesson.get("search_terms", [])
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if not query:
            raise ValueError("approved design lesson requires a nonblank search term")
        applicability = lesson["applicability"]
        previous_probe = review.get("retrieval_probe") or {}
        durable_projection_witnesses = (
            self.repository.processed_design_lesson_review_projection_witnesses(
                review_id=review_id,
                lesson_id=str(lesson["id"]),
            )
        )
        projection_witnesses = self._merge_design_lesson_projection_witnesses(
            previous_probe.get("projection_witnesses", []),
            projection.get("processed_events", []),
            durable_projection_witnesses,
            review_id=review_id,
            lesson_id=str(lesson["id"]),
        )
        projection_proved_review = self._projection_proves_design_lesson_review(
            projection,
            projection_witnesses=projection_witnesses,
            review_id=review_id,
            lesson_id=str(lesson["id"]),
        )
        probe: dict[str, Any] = {
            "query": query,
            "conditions": [],
            "matched_lesson_id": None,
            "projection": projection,
            "projection_witnesses": projection_witnesses,
            "projection_proved_review": projection_proved_review,
            "match": None,
            "eligible": False,
            "status": "approved-retrieval-pending",
        }
        try:
            conditions = satisfying_conditions(
                applicability.get("required_conditions", []),
                applicability.get("required_condition_expression"),
            )
        except Exception as exc:
            return self._record_pending_design_lesson_review_failure(
                review=review,
                lesson=lesson,
                projection=projection,
                probe=probe,
                stage="condition-witness",
                error=exc,
                idempotent=idempotent,
            )
        probe["conditions"] = conditions
        try:
            matches = self.repository.search_approved_design_lessons(
                organization_id=self.bootstrap_config["organization_id"],
                query=query,
                design_group_id=lesson.get("source_design_group_id"),
                limit=20,
            )
        except Exception as exc:
            return self._record_pending_design_lesson_review_failure(
                review=review,
                lesson=lesson,
                projection=projection,
                probe=probe,
                stage="search",
                error=exc,
                idempotent=idempotent,
            )
        retrieved = next(
            (
                item
                for item in matches
                if str(item.get("id")) == str(lesson["id"])
            ),
            None,
        )
        design_features = {
            "component_classes": applicability.get("component_classes", []),
            "interface_types": applicability.get("interface_types", []),
            "design_stages": applicability.get("design_stages", []),
            "failure_modes": lesson.get("problem", {}).get("failure_modes", []),
            "satisfied_conditions": conditions,
        }
        try:
            retrieval_match = (
                match_design_lesson(retrieved, design_features, query)
                if retrieved is not None
                else {"eligible": False, "exclusion_reasons": ["lesson not retrieved"]}
            )
        except Exception as exc:
            probe["matched_lesson_id"] = (
                str(retrieved["id"]) if retrieved else None
            )
            return self._record_pending_design_lesson_review_failure(
                review=review,
                lesson=lesson,
                projection=projection,
                probe=probe,
                stage="match",
                error=exc,
                idempotent=idempotent,
            )
        successful = projection_proved_review and bool(retrieval_match["eligible"])
        probe.update(
            matched_lesson_id=str(retrieved["id"]) if retrieved else None,
            projection_proved_review=projection_proved_review,
            match=retrieval_match,
            eligible=bool(retrieval_match["eligible"]),
            status=(
                "stored-and-retrievable"
                if successful
                else "approved-retrieval-pending"
            ),
        )
        try:
            review = self.repository.record_design_lesson_review_probe(
                review_id=review_id, probe=probe, successful=successful
            )
        except Exception as exc:
            probe["status"] = "approved-retrieval-pending"
            failure = {
                "stage": "probe-persistence",
                "error": f"{type(exc).__name__}: {exc}",
            }
            probe["failure"] = failure
            return self._design_lesson_review_result(
                review=review,
                lesson=lesson,
                projection=projection,
                retrieval_match=retrieval_match,
                retrieval_probe=probe,
                failure=failure,
                idempotent=idempotent,
            )
        return self._design_lesson_review_result(
            review=review,
            lesson=lesson,
            projection=projection,
            retrieval_match=retrieval_match,
            retrieval_probe=probe,
            idempotent=idempotent,
        )

    def _record_pending_design_lesson_review_failure(
        self,
        *,
        review: dict[str, Any],
        lesson: dict[str, Any],
        projection: dict[str, Any],
        probe: dict[str, Any],
        stage: str,
        error: Exception,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        failure = {
            "stage": stage,
            "error": f"{type(error).__name__}: {error}",
        }
        probe.update(status="approved-retrieval-pending", failure=failure)
        try:
            review = self.repository.record_design_lesson_review_probe(
                review_id=str(review["id"]), probe=probe, successful=False
            )
        except Exception as persistence_error:
            failure = {
                "stage": "probe-persistence",
                "error": (
                    f"{type(persistence_error).__name__}: {persistence_error}"
                ),
                "cause": failure,
            }
            probe["failure"] = failure
        return self._design_lesson_review_result(
            review=review,
            lesson=lesson,
            projection=projection,
            retrieval_probe=probe,
            failure=failure,
            idempotent=idempotent,
        )

    @staticmethod
    def _merge_design_lesson_projection_witnesses(
        prior_witnesses: Any,
        processed_events: Any,
        durable_witnesses: Any,
        *,
        review_id: str,
        lesson_id: str,
    ) -> list[dict[str, str]]:
        expected = [
            ("design_lesson.approved", "design_lesson", lesson_id),
            (
                "design_lesson_review.approved",
                "design_lesson_review",
                review_id,
            ),
        ]
        candidates = [
            item
            for collection in (
                prior_witnesses,
                processed_events,
                durable_witnesses,
            )
            if isinstance(collection, list)
            for item in collection
            if isinstance(item, dict)
        ]
        witnesses: list[dict[str, str]] = []
        for event_type, aggregate_type, aggregate_id in expected:
            matches = [
                item
                for item in candidates
                if (
                    str(item.get("event_type")) == event_type
                    and str(item.get("aggregate_type")) == aggregate_type
                    and str(item.get("aggregate_id")) == aggregate_id
                )
            ]
            if not matches:
                continue
            selected = min(matches, key=lambda item: str(item.get("event_id", "")))
            witness = {
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
            }
            event_id = selected.get("event_id")
            if isinstance(event_id, str) and event_id:
                witness["event_id"] = event_id
            witnesses.append(witness)
        return witnesses

    @staticmethod
    def _projection_proves_design_lesson_review(
        projection: dict[str, Any],
        *,
        projection_witnesses: list[dict[str, str]],
        review_id: str,
        lesson_id: str,
    ) -> bool:
        witnessed = {
            (
                str(item.get("event_type")),
                str(item.get("aggregate_type")),
                str(item.get("aggregate_id")),
            )
            for item in projection_witnesses
        }
        return {
            ("design_lesson.approved", "design_lesson", lesson_id),
            (
                "design_lesson_review.approved",
                "design_lesson_review",
                review_id,
            ),
        }.issubset(witnessed)

    def design_lesson_review_status(
        self, review_id: str, retry: bool = True
    ) -> dict[str, Any]:
        self._require_database()
        review = self.repository.get_design_lesson_review(review_id)
        self._require_design_lesson_review_scope(review)
        if retry and review["status"] == "approved-retrieval-pending":
            return self._complete_design_lesson_review(review_id)
        lesson = None
        if review.get("published_design_lesson_id"):
            lesson = self.repository.get_design_lesson(
                str(review["published_design_lesson_id"]),
                organization_id=self.bootstrap_config["organization_id"],
            )
        probe = review.get("retrieval_probe") or {}
        return self._design_lesson_review_result(
            review=review,
            lesson=lesson,
            projection=probe.get("projection"),
            retrieval_match=probe.get("match"),
        )

    def design_lesson_review_reject(
        self, *, review_id: str, reviewer_text: str, confirmation: str
    ) -> dict[str, Any]:
        if not isinstance(reviewer_text, str) or not reviewer_text.strip():
            raise ValueError("reviewer_text is required")
        expected_confirmation = f"拒绝设计经验 {review_id}"
        if confirmation != expected_confirmation:
            raise ValueError(
                "confirmation must use canonical confirmation: "
                + expected_confirmation
            )
        self._require_database()
        review = self.repository.get_design_lesson_review(review_id)
        self._require_design_lesson_review_scope(review)
        review = self.repository.reject_design_lesson_review(
            review_id=review_id,
            reviewer_id=self.settings.actor_id,
            reviewer_text=reviewer_text,
        )
        return self._design_lesson_review_result(review=review)

    def _require_design_lesson_review_scope(self, review: dict[str, Any]) -> None:
        if (
            review["organization_id"]
            != self.bootstrap_config["organization_id"]
            or review["design_group_id"]
            != self.bootstrap_config["design_group_id"]
        ):
            raise PermissionError(
                "design lesson review does not belong to the configured scope"
            )

    def design_job_working_copy_create(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        source_path: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None = None,
        model_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        organization, group = self._configured_job_scope(
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        revision = self._expected_job_revision(expected_job_revision)
        resolved_job_id = self._resolve_job_reference(
            job_id,
            organization_id=organization,
            design_group_id=group,
        )
        return self.design_workspace.create_job_working_copy(
            job_id=resolved_job_id,
            expected_job_revision=revision,
            source_path=source_path,
            organization_id=organization,
            design_group_id=group,
            family_id=family_id,
            model_revision_id=model_revision_id,
            actor_id=self.settings.actor_id,
        )

    def design_job_new_working_copy_create(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str | None = None,
        explicit_family_authorization: bool = False,
    ) -> dict[str, Any]:
        self._require_database()
        organization, group = self._configured_job_scope(
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        revision = self._expected_job_revision(expected_job_revision)
        if family_id and not explicit_family_authorization:
            raise ValueError(
                "new design family assignment requires explicit_family_authorization"
            )
        if family_id:
            family = self.repository.get_family(family_id)
            if (
                family["organization_id"] != organization
                or family["design_group_id"] != group
            ):
                raise ValueError(
                    "family does not belong to the requested organization/design group"
                )
        resolved_job_id = self._resolve_job_reference(
            job_id,
            organization_id=organization,
            design_group_id=group,
        )
        return self.design_workspace.create_job_new_working_copy(
            job_id=resolved_job_id,
            expected_job_revision=revision,
            organization_id=organization,
            design_group_id=group,
            family_id=family_id,
            actor_id=self.settings.actor_id,
        )

    def _compatibility_working_copy_job(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        request_identity: dict[str, object],
    ) -> DesignJobManifest:
        organization, group = self._configured_job_scope(
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        identity = {
            "contract": "deprecated-v0.2-cad-working-copy",
            "organization_id": organization,
            "design_group_id": group,
            "family_id": family_id,
            **request_identity,
        }
        token = "compatibility-v0.2-" + hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self.design_jobs.create(
            job_type="mechanical_design",
            title="Deprecated v0.2 CAD working-copy compatibility",
            organization_id=organization,
            design_group_id=group,
            family_id=family_id,
            idempotency_token=token,
            actor_id=self.settings.actor_id,
        )

    @staticmethod
    def _compatibility_retry_result(
        manifest: DesignJobManifest,
    ) -> dict[str, Any] | None:
        if manifest.active_working_copy_id is None:
            return None
        return {
            "id": str(manifest.active_working_copy_id),
            "job_id": str(manifest.job_id),
            "status": "already_bound",
            "job": manifest.as_dict(),
        }

    def design_working_copy_create(self, **kwargs: Any) -> dict[str, Any]:
        """Deprecated v0.2 wrapper; every result is still Job-bound."""
        self._require_database()
        compatibility_request_id = kwargs.get("compatibility_request_id")
        if compatibility_request_id is not None and (
            not isinstance(compatibility_request_id, str)
            or not compatibility_request_id.strip()
        ):
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "compatibility_request_id must be a nonblank string when supplied",
            )
        source_path = str(kwargs["source_path"])
        model_revision_id = kwargs.get("model_revision_id")
        request_identity = {
            "operation": "existing_model",
            "request_id": (
                compatibility_request_id.strip()
                if isinstance(compatibility_request_id, str)
                else None
            ),
            "model_revision_id": model_revision_id,
            "source_request_sha256": hashlib.sha256(
                source_path.strip().replace("\\", "/").encode("utf-8")
            ).hexdigest(),
        }
        manifest = self._compatibility_working_copy_job(
            organization_id=str(kwargs["organization_id"]),
            design_group_id=str(kwargs["design_group_id"]),
            family_id=kwargs.get("family_id"),
            request_identity=request_identity,
        )
        retry = self._compatibility_retry_result(manifest)
        if retry is not None:
            return retry
        return self.design_job_working_copy_create(
            job_id=str(manifest.job_id),
            expected_job_revision=manifest.revision,
            source_path=source_path,
            organization_id=str(kwargs["organization_id"]),
            design_group_id=str(kwargs["design_group_id"]),
            family_id=kwargs.get("family_id"),
            model_revision_id=kwargs.get("model_revision_id"),
        )

    def design_new_working_copy_create(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        family_id: str | None = None,
        explicit_family_authorization: bool = False,
        compatibility_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Deprecated v0.2 wrapper; every result is still Job-bound."""
        self._require_database()
        if compatibility_request_id is not None and (
            not isinstance(compatibility_request_id, str)
            or not compatibility_request_id.strip()
        ):
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "compatibility_request_id must be a nonblank string when supplied",
            )
        manifest = self._compatibility_working_copy_job(
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            request_identity={
                "operation": "new_design",
                "request_id": (
                    compatibility_request_id.strip()
                    if isinstance(compatibility_request_id, str)
                    else uuid.uuid4().hex
                ),
            },
        )
        retry = self._compatibility_retry_result(manifest)
        if retry is not None:
            return retry
        return self.design_job_new_working_copy_create(
            job_id=str(manifest.job_id),
            expected_job_revision=manifest.revision,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            explicit_family_authorization=explicit_family_authorization,
        )

    def design_change_record(self, **kwargs: Any) -> dict[str, Any]:
        self._require_database()
        return self.design_workspace.record_change(actor_id=self.settings.actor_id, **kwargs)

    def design_knowledge_retrieve(
        self,
        *,
        working_copy_id: str,
        query: str,
        design_features: dict[str, Any],
        used_knowledge_ids: list[str],
        non_use_reason: str = "",
    ) -> dict[str, Any]:
        self._require_database()
        working = self.repository.get_working_copy(working_copy_id)
        context = self.context_builder.build(
            organization_id=str(working["organization_id"]),
            design_group_id=str(working["design_group_id"]),
            requested_family_id=(str(working["family_id"]) if working.get("family_id") else None),
            model_revision_id=(
                str(working["source_model_revision_id"])
                if working.get("source_model_revision_id")
                else None
            ),
            explicit_family_authorization=bool(working.get("family_id")),
            design_features=design_features,
            lesson_query=query,
        )
        retrieved_ids: list[str] = []
        for section in (
            "hard_constraints",
            "preferences",
            "approved_facts",
            "specialized_knowledge",
        ):
            for item in context.get(section, []):
                value = item.get("assertion_id") if isinstance(item, dict) else None
                try:
                    normalized = str(UUID(str(value)))
                except (TypeError, ValueError, AttributeError):
                    continue
                if normalized not in retrieved_ids:
                    retrieved_ids.append(normalized)
        for lesson in context.get("approved_design_lessons", []):
            for assertion in lesson.get("assertions", []):
                value = assertion.get("assertion_id")
                try:
                    normalized = str(UUID(str(value)))
                except (TypeError, ValueError, AttributeError):
                    continue
                if normalized not in retrieved_ids:
                    retrieved_ids.append(normalized)
        normalized_used_ids: list[str] = []
        for value in used_knowledge_ids:
            try:
                normalized = str(UUID(str(value)))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("used knowledge IDs must be valid UUIDs") from exc
            if normalized not in normalized_used_ids:
                normalized_used_ids.append(normalized)
        if (
            retrieved_ids
            and set(normalized_used_ids) != set(retrieved_ids)
            and not non_use_reason.strip()
        ):
            non_use_reason = "retrieved knowledge was reviewed but not selected for this change"
        receipt = self.repository.record_retrieval_receipt(
            working_copy_id=working_copy_id,
            query=query,
            retrieval_scope={
                "family_knowledge": bool(working.get("family_id")),
                "general_design_knowledge": True,
                "design_lessons": True,
                "similar_models": bool(
                    working.get("family_id") and working.get("source_model_revision_id")
                ),
            },
            retrieved_knowledge_ids=retrieved_ids,
            used_knowledge_ids=normalized_used_ids,
            retrieval_status="completed" if retrieved_ids else "completed_no_match",
            non_use_reason=non_use_reason,
            actor_id=self.settings.actor_id,
        )
        return {"context": context, "retrieval_receipt": receipt}

    def design_retrieval_receipt_get(self, working_copy_id: str) -> dict[str, Any] | None:
        self._require_database()
        return self.repository.latest_retrieval_receipt(working_copy_id)

    def design_change_review(
        self, change_set_id: str, decision: str, review_text: str, confirmation: str
    ) -> dict[str, Any]:
        self._require_database()
        decision_word = "批准" if decision == "approve" else "拒绝"
        if change_set_id not in confirmation or decision_word not in confirmation:
            raise ValueError("confirmation must include change_set_id and the matching Chinese decision word")
        return self.repository.review_change_set(
            change_set_id, decision, self.settings.actor_id, review_text
        )

    def design_change_applied(self, change_set_id: str, confirmation: str) -> dict[str, Any]:
        self._require_database()
        return self.design_workspace.mark_change_applied(
            change_set_id=change_set_id, confirmation=confirmation
        )

    def design_change_close(
        self,
        *,
        change_set_id: str,
        disposition: str,
        reason: str,
        confirmation: str,
        successor_change_set_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        if disposition not in {"superseded", "cancelled"}:
            raise ValueError("disposition must be superseded or cancelled")
        decision_word = "取代" if disposition == "superseded" else "取消"
        required_ids = [change_set_id]
        if successor_change_set_id:
            required_ids.append(successor_change_set_id)
        if decision_word not in confirmation or any(item not in confirmation for item in required_ids):
            raise ValueError(
                "confirmation must include the affected change-set IDs and matching Chinese closure word"
            )
        return self.repository.close_change_set(
            change_set_id=change_set_id,
            disposition=disposition,
            reason=reason,
            actor_id=self.settings.actor_id,
            successor_change_set_id=successor_change_set_id,
        )

    def design_confirmation_record(
        self,
        *,
        working_copy_id: str,
        lesson_summary: dict[str, Any],
        confirmation: str,
    ) -> dict[str, Any]:
        self._require_database()
        if working_copy_id not in confirmation or "模型设计确认" not in confirmation:
            raise ValueError("confirmation must include working_copy_id and 模型设计确认")
        result = self.repository.record_design_lesson_summary(
            working_copy_id=working_copy_id,
            summary=lesson_summary,
            actor_id=self.settings.actor_id,
        )
        result["lesson_review_flow"] = {
            "status": result["publication_status"],
            "next_tool": "design_lesson_stage" if result["publication_status"] == "ready" else None,
            "publication_blocker": result.get("publication_blocker"),
        }
        return result

    def design_validation_record(self, **kwargs: Any) -> dict[str, Any]:
        self._require_database()
        return self.design_workspace.record_validation(**kwargs)

    def design_assembly_completeness_validate(self, **kwargs: Any) -> dict[str, Any]:
        self._require_database()
        return self.design_workspace.validate_assembly_completeness(**kwargs)

    def standard_part_providers_get(self, category: str = "") -> dict[str, Any]:
        return self.standard_parts.list_providers(category)

    def standard_part_download_register(self, **kwargs: Any) -> dict[str, Any]:
        self._require_database()
        working_copy_id = str(kwargs.pop("working_copy_id", "") or "").strip()
        registered = self.standard_parts.register_download(**kwargs)
        if not working_copy_id:
            return registered
        with self.design_workspace.locked_job_working_copy(working_copy_id) as (
            job_root,
            _,
            _,
            _,
        ):
            used_copy = self.standard_parts.copy_into_job(
                registered=registered,
                job_root=job_root,
                working_copy_id=working_copy_id,
            )
        return {**registered, "job_used_copy": used_copy}

    def design_delivery_approve(self, working_copy_id: str, confirmation: str) -> dict[str, Any]:
        self._require_database()
        scope = {
            "organization_id": str(self.bootstrap_config["organization_id"]),
            "design_group_id": str(self.bootstrap_config["design_group_id"]),
        }
        self.repository.authorize_delivery_approval(
            working_copy_id=working_copy_id,
            actor_id=self.settings.actor_id,
            **scope,
        )
        approved = self.design_workspace.approve_delivery(
            working_copy_id,
            self.settings.actor_id,
            confirmation,
            self.artifacts,
            **scope,
        )
        return {
            **approved,
            "design_lesson_review": {
                "required": True,
                "working_copy_id": working_copy_id,
                "next_action": "design_lesson_review_context",
            },
        }

    def design_lesson_review_context(self, working_copy_id: str) -> dict[str, Any]:
        self._require_database()
        read_model = self._design_lesson_review_read_model(working_copy_id)
        current_sha256 = self.design_workspace.current_hash(working_copy_id)
        return self._design_lesson_review_context_from_read_model(
            working_copy_id, current_sha256, read_model
        )

    def _design_lesson_review_read_model(
        self, working_copy_id: str
    ) -> dict[str, Any]:
        configured_organization = str(self.bootstrap_config["organization_id"])
        configured_design_group = str(self.bootstrap_config["design_group_id"])
        return self.repository.design_lesson_review_context(
            working_copy_id,
            organization_id=configured_organization,
            design_group_id=configured_design_group,
        )

    def _design_lesson_review_context_from_read_model(
        self,
        working_copy_id: str,
        current_sha256: str,
        read_model: dict[str, Any],
    ) -> dict[str, Any]:
        configured_organization = str(self.bootstrap_config["organization_id"])
        configured_design_group = str(self.bootstrap_config["design_group_id"])
        working_copy = read_model["working_copy"]
        if (
            str(working_copy.get("organization_id", "")) != configured_organization
            or str(working_copy.get("design_group_id", "")) != configured_design_group
        ):
            raise PermissionError(
                "working copy does not belong to the configured organization and design group"
            )
        changes = read_model["change_sets"]
        validations = read_model["validation_reports"]
        approved_final_sha256 = working_copy.get("approved_final_sha256")
        approved_final_artifact_path = working_copy.get(
            "approved_final_artifact_path"
        )
        if (
            not isinstance(approved_final_sha256, str)
            or approved_final_sha256 != current_sha256
        ):
            raise ValueError(
                "working copy changed after delivery approval; approve delivery again"
            )
        if not isinstance(approved_final_artifact_path, str) or not approved_final_artifact_path:
            raise ValueError(
                "delivery approval has no immutable final model snapshot; approve delivery again"
            )

        return {
            "schema_version": "DesignLessonReviewContext/v1",
            "working_copy_id": working_copy_id,
            "final_model_sha256": approved_final_sha256,
            "working_path": working_copy["working_path"],
            "applied_change_sets": changes,
            "validation_history": validations,
            "standard_part_provenance": read_model.get(
                "standard_part_provenance", []
            ),
            "material_iteration_candidates": derive_iteration_candidates(
                changes, validations
            ),
            "next_action": "prepare_design_lesson_review",
        }

    def design_lesson_review_prepare(
        self,
        working_copy_id: str,
        package: dict[str, Any],
        evidence_items: list[dict[str, str]],
        supersedes_review_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_database()
        with self.design_workspace.locked_working_copy_path(
            working_copy_id
        ) as locked_working_path:
            read_model = self._design_lesson_review_read_model(working_copy_id)
            context = self._design_lesson_review_context_from_read_model(
                working_copy_id,
                file_sha256(locked_working_path),
                read_model,
            )
            return self._design_lesson_review_prepare_locked(
                working_copy_id,
                package,
                evidence_items,
                supersedes_review_id,
                context,
                locked_working_path,
            )

    def _design_lesson_review_prepare_locked(
        self,
        working_copy_id: str,
        package: dict[str, Any],
        evidence_items: list[dict[str, str]],
        supersedes_review_id: str | None,
        context: dict[str, Any],
        locked_working_path: Path,
    ) -> dict[str, Any]:
        if not isinstance(package, dict):
            raise ValueError("design lesson package must be an object")
        source = package.get("source")
        if not isinstance(source, dict):
            raise ValueError("design lesson package source must be an object")
        working_copy = self.repository.get_working_copy(working_copy_id)
        configured_organization = str(self.bootstrap_config["organization_id"])
        configured_design_group = str(self.bootstrap_config["design_group_id"])
        if str(source.get("working_copy_id", "")) != working_copy_id:
            raise ValueError("source working_copy_id does not match the requested working copy")
        if str(source.get("organization_id", "")) != configured_organization:
            raise ValueError("source organization_id does not match the configured organization")
        if str(source.get("design_group_id", "")) != configured_design_group:
            raise ValueError("source design_group_id does not match the configured design group")
        if (
            str(working_copy.get("organization_id", "")) != configured_organization
            or str(working_copy.get("design_group_id", "")) != configured_design_group
        ):
            raise ValueError("working copy organization/design-group relationship is invalid")
        if source.get("family_id") != working_copy.get("family_id"):
            raise ValueError("source family_id does not match the working copy")
        if source.get("after_model_sha256") != context["final_model_sha256"]:
            raise ValueError("source after_model_sha256 does not match the approved final model")

        requested_change_ids = source.get("change_set_ids")
        if not isinstance(requested_change_ids, list) or not requested_change_ids:
            raise ValueError("source change_set_ids must be nonempty")
        change_sets = {
            str(change_set.get("id")): change_set
            for change_set in context["applied_change_sets"]
            if isinstance(change_set, dict)
        }
        for change_set_id in requested_change_ids:
            change_set = change_sets.get(str(change_set_id))
            if change_set is None:
                raise ValueError(f"source change set is unknown or not applied: {change_set_id}")
            if str(change_set.get("working_copy_id", "")) != working_copy_id:
                raise ValueError("source change set belongs to another working copy")
            if change_set.get("status") != "applied":
                raise ValueError("design lesson review requires applied change sets")
        final_change_set = change_sets[str(requested_change_ids[-1])]
        if final_change_set.get("resulting_sha256") != context["final_model_sha256"]:
            raise ValueError("final applied change set does not match the approved final model")

        if not isinstance(evidence_items, list):
            raise ValueError("evidence_items must be a list")
        validation_history = context["validation_history"]
        workspace = validate_managed_path(
            self.settings.workspace,
            allow_missing_leaf=False,
        ).path
        validation_bindings: dict[str, dict[str, Any]] = {}
        immutable_evidence_items: list[dict[str, str]] = []
        for evidence in evidence_items:
            if not isinstance(evidence, dict):
                raise ValueError("evidence item must be an object")
            relative_path = evidence.get("path")
            if not isinstance(relative_path, str) or not relative_path:
                raise ValueError("evidence path is required")
            evidence_path = workspace / relative_path
            snapshot = self.artifacts.ingest_file(
                evidence_path, allowed_root=workspace
            )
            snapshot_path = Path(str(snapshot["storage_path"]))
            try:
                snapshot_relative_path = relative_managed_path(
                    snapshot_path,
                    workspace,
                )
            except ValueError as exc:
                raise ValueError(
                    "immutable evidence snapshot must remain inside the workspace"
                ) from exc
            immutable_evidence_items.append(
                {**evidence, "path": snapshot_relative_path.as_posix()}
            )

            role = evidence.get("role")
            if role not in EVIDENCE_ROLE_VALIDATION_KINDS:
                continue
            validation_kind = EVIDENCE_ROLE_VALIDATION_KINDS[role]
            if (
                str(evidence.get("working_copy_id", "")) != working_copy_id
                or str(evidence.get("change_set_id", "")) != str(requested_change_ids[-1])
                or evidence.get("model_sha256") != context["final_model_sha256"]
                or evidence.get("validation_kind") != validation_kind
            ):
                raise ValueError(
                    f"validation evidence revision binding mismatch: {evidence.get('evidence_id', '')}"
                )
            matching_validation = next(
                (
                    validation
                    for validation in reversed(validation_history)
                    if isinstance(validation, dict)
                    and validation.get("status") == "passed"
                    and validation.get("validation_kind") == validation_kind
                    and str(validation.get("working_copy_id", "")) == working_copy_id
                    and str(validation.get("change_set_id", ""))
                    == str(requested_change_ids[-1])
                    and validation.get("working_sha256")
                    == context["final_model_sha256"]
                    and same_managed_path(
                        Path(str(validation.get("report_path", ""))),
                        Path(str(snapshot["source_path"])),
                    )
                    and validation.get("report_sha256") == snapshot["sha256"]
                ),
                None,
            )
            if matching_validation is None:
                raise ValueError(
                    f"design lesson review requires same-revision passed {validation_kind} evidence"
                )
            validation_bindings[str(evidence.get("evidence_id", ""))] = matching_validation

        staged_package_sha256: str | None = None
        review_id: str | None = None
        prepared: dict[str, Any] | None = None
        try:
            staged = self.design_lesson_stage(
                package, immutable_evidence_items, review_revision=True
            )
            staged_package_sha256 = staged["package_sha256"]
            staged_inspection = self.design_lesson_staging.get_review(
                staged_package_sha256
            )
            if staged_inspection.get("status") != "verified-local-only":
                raise ValueError("staged design lesson is not immutable and verified")
            staged_evidence = {
                item["evidence_id"]: item
                for item in staged_inspection["package"]["evidence_manifest"]
            }
            for evidence_id, validation in validation_bindings.items():
                if staged_evidence[evidence_id]["sha256"] != validation.get(
                    "report_sha256"
                ):
                    raise ValueError(
                        f"staged evidence does not match validation report digest: {evidence_id}"
                    )

            evidence_summary = [
                {
                    field: evidence[field]
                    for field in (
                        "evidence_id",
                        "role",
                        "media_type",
                        "validation_kind",
                    )
                    if field in evidence
                }
                for evidence in staged_inspection["package"]["evidence_manifest"]
            ]
            validation_summary = []
            for validation_kind in sorted(
                {
                    validation["validation_kind"]
                    for validation in validation_bindings.values()
                }
            ):
                validation = next(
                    item
                    for item in validation_bindings.values()
                    if item["validation_kind"] == validation_kind
                )
                validation_summary.append(
                    {
                        "validation_kind": validation_kind,
                        "status": validation["status"],
                        "checks": [
                            {
                                "label": next(
                                    (
                                        str(check[key])
                                        for key in ("check_id", "id", "name")
                                        if check.get(key) not in (None, "")
                                    ),
                                    "unnamed-check",
                                ),
                                "status": str(check.get("status", "unknown")),
                            }
                            for check in validation.get("checks", [])
                            if isinstance(check, dict)
                        ],
                    }
                )

            review_id = f"DLR-{uuid.uuid4()}"
            prepared = self.design_lesson_reviews.prepare(
                review_id,
                staged_inspection,
                supersedes_review_id=supersedes_review_id,
                evidence_summary=evidence_summary,
                validation_summary=validation_summary,
            )
            approved_final_artifact_path = working_copy.get(
                "approved_final_artifact_path"
            )
            if not isinstance(approved_final_artifact_path, str):
                raise ValueError("delivery approval has no immutable final model snapshot")

            def verify_filesystem_bindings() -> None:
                self.artifacts.verify_file(
                    Path(approved_final_artifact_path),
                    context["final_model_sha256"],
                )
                final_snapshot = self.artifacts.ingest_file(
                    locked_working_path, allowed_root=workspace
                )
                if (
                    final_snapshot["sha256"] != context["final_model_sha256"]
                    or final_snapshot["storage_path"] != approved_final_artifact_path
                ):
                    raise ValueError(
                        "working copy changed after delivery approval; approve delivery again"
                    )
                current_staged = self.design_lesson_staging.get_review(
                    staged_package_sha256
                )
                current_review = self.design_lesson_reviews.inspect(review_id)
                if current_staged.get("status") != "verified-local-only":
                    raise ValueError(
                        "staged design lesson changed before review insertion"
                    )
                if current_review.get("status") != "verified-local-only":
                    raise ValueError(
                        "design lesson review changed before repository insertion"
                    )
                for evidence, evidence_path in (
                    self.design_lesson_staging.review_evidence_paths(
                        staged_package_sha256
                    )
                ):
                    self.artifacts.verify_file(evidence_path, evidence["sha256"])

            verify_filesystem_bindings()
            self.repository.create_design_lesson_review(
                review_id=review_id,
                organization_id=configured_organization,
                design_group_id=configured_design_group,
                working_copy_id=working_copy_id,
                lesson_id=staged_inspection["package"]["lesson_id"],
                package_sha256=staged_inspection["package_sha256"],
                review_card_sha256=prepared["review_card_sha256"],
                final_model_sha256=context["final_model_sha256"],
                approved_final_artifact_path=approved_final_artifact_path,
                review_path=prepared["review_card_path"],
                package_path=str(
                    self.design_lesson_staging.review_package_paths(
                        staged_package_sha256
                    )["lesson_json"]
                ),
                actor_id=self.settings.actor_id,
                supersedes_review_id=supersedes_review_id,
                pre_commit_verifier=verify_filesystem_bindings,
            )
        except Exception as original_error:
            cleanup_errors: list[tuple[str, Exception]] = []
            if prepared is not None and review_id is not None:
                try:
                    self.design_lesson_reviews.discard_prepared_attempt_owned(
                        review_id
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(("review", cleanup_error))
            if staged_package_sha256 is not None:
                try:
                    self.design_lesson_staging.discard_review_attempt_owned(
                        staged_package_sha256
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(("staging", cleanup_error))
            for label, cleanup_error in cleanup_errors:
                original_error.add_note(
                    f"{label} cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise
        return {
            "review_id": review_id,
            "status": prepared["status"],
            "review_card": prepared["review_card"],
            "review_card_markdown": prepared["review_card_markdown"],
            "confirmation": prepared["confirmation"],
        }

    def projection_sync(self, limit: int = 100) -> dict[str, Any]:
        self._require_database()
        return self.projection.project_pending(self.repository, limit)

    def projection_rebuild(self, confirmation: str) -> dict[str, Any]:
        self._require_database()
        if "PostgreSQL" not in confirmation or "重建" not in confirmation:
            raise ValueError("confirmation must include PostgreSQL and 重建")
        return self.projection.rebuild(self.repository)

    def _safe_projection(self, limit: int = 100) -> dict[str, Any]:
        try:
            return self.projection.project_pending(self.repository, limit)
        except Exception as exc:
            return {
                "status": "deferred",
                "reason": f"{type(exc).__name__}: {exc}",
                "authoritative_write_preserved": True,
            }

    @staticmethod
    def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
        ensure_managed_directory(path.parent, parents=True, exist_ok=True)
        content = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        try:
            atomic_publish_new(path, content)
        except FileExistsError:
            atomic_replace(path, content)
