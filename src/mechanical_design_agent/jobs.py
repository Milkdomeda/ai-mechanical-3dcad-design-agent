from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Protocol
import unicodedata
from uuid import UUID, uuid4

from .secure_fs import (
    ManagedFileRead,
    SecureFilesystemError,
    FileIdentity,
    atomic_move_pinned_directory,
    atomic_publish_directory,
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_file_lock,
    list_managed_directory,
    read_managed_file,
    remove_owned_tree,
    validate_managed_path,
)
from .workspace_bootstrap import WorkspaceManifest


JOB_MANIFEST_SCHEMA = "MechanicalDesignJob/v1"
JOB_DOCTOR_SCHEMA = "MechanicalDesignJobDoctor/v1"
JOB_REPAIR_SCHEMA = "MechanicalDesignJobRepair/v1"
PROVISIONING_IDENTITY_SCHEMA = "MechanicalDesignJobProvisioning/v1"

_DISPLAY_ID = re.compile(r"JOB-(\d{8})-(\d{3,})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z"
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"/\\|?*')
_JOB_TYPES = {
    "mechanical_design": (
        "requirements",
        "design",
        "validation",
        "delivery",
        "lesson_capture",
        "completed",
    ),
    "product_family_onboarding": (
        "intake",
        "analysis",
        "knowledge_review",
        "database_publication",
        "completed",
    ),
}
_STATUSES = {"active", "blocked", "completed", "cancelled", "archived"}
_TERMINAL_STATUSES = {"completed", "cancelled", "archived"}
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_DIRECTORY_CONTRACT = (
    "inputs/source",
    "requirements/draft",
    "requirements/approved",
    "models/working",
    "models/revisions",
    "models/exports",
    "components/standard-parts",
    "analysis",
    "validation/specifications",
    "validation/reports",
    "validation/images",
    "knowledge/retrieval-receipts",
    "knowledge/extracted",
    "knowledge/design-lessons",
    "previews",
    "delivery",
    "provenance",
    "logs",
)
_AUTHORITATIVE_MANIFEST_FIELDS = (
    "job_id",
    "display_id",
    "job_type",
    "workspace_id",
    "title",
    "slug",
    "status",
    "phase",
    "revision",
    "organization_id",
    "design_group_id",
    "family_id",
    "directory_name",
    "created_at",
    "created_by",
    "updated_at",
)
_MANIFEST_FIELDS = {
    "schema_version",
    *_AUTHORITATIVE_MANIFEST_FIELDS,
    "active_working_copy_id",
    "source_snapshots",
}
_SNAPSHOT_FIELDS = {
    "snapshot_id",
    "stored_path",
    "sha256",
    "source_kind",
    "source_model_revision_id",
}


class JobFailure(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "blocked",
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": "MechanicalDesignJobError/v1",
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


def _invalid_manifest(message: str) -> JobFailure:
    return JobFailure("JOB_MANIFEST_INVALID", message)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_manifest(f"{label} must be a nonblank string")
    return value.strip()


def _parse_uuid(value: object, label: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _invalid_manifest(f"{label} must be a UUID") from exc
    if parsed.version is None:
        raise _invalid_manifest(f"{label} must be an RFC-4122 UUID")
    return parsed


def _parse_timestamp(value: object, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        spelling = value
        if _CANONICAL_TIMESTAMP.fullmatch(spelling) is None:
            raise _invalid_manifest(f"{label} must be an RFC-3339 timestamp")
        try:
            parsed = datetime.strptime(spelling, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise _invalid_manifest(f"{label} must be an RFC-3339 timestamp") from exc
    else:
        raise _invalid_manifest(f"{label} must be an RFC-3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_manifest(f"{label} must include a UTC offset")
    utc = parsed.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _portable_collision_key(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFKC", part).casefold() for part in parts
    )


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _portable_parts(value: str | os.PathLike[str], label: str) -> tuple[str, ...]:
    spelling = os.fspath(value)
    if not isinstance(spelling, str) or not spelling:
        raise JobFailure("JOB_PATH_OUTSIDE", f"{label} must be a relative path")
    windows = PureWindowsPath(spelling)
    posix = PurePosixPath(spelling)
    if spelling != unicodedata.normalize("NFC", spelling):
        raise JobFailure(
            "JOB_PATH_OUTSIDE", f"{label} must use canonical Unicode NFC"
        )
    if (
        "\\" in spelling
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
    ):
        raise JobFailure(
            "JOB_PATH_OUTSIDE",
            f"{label} must use a portable relative POSIX path",
        )
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise JobFailure(
            "JOB_PATH_OUTSIDE",
            f"{label} contains path traversal",
        )
    for part in parts:
        normalized_stem = unicodedata.normalize("NFKC", part).casefold().split(".", 1)[0]
        if (
            any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
            or any(unicodedata.category(character) == "Cc" for character in part)
            or part.endswith((" ", "."))
            or normalized_stem in _WINDOWS_RESERVED_NAMES
            or _utf16_length(part) > 255
        ):
            raise JobFailure(
                "JOB_PATH_OUTSIDE",
                f"{label} is not portable across macOS and Windows",
            )
    return parts


def _directory_component(value: object, label: str) -> str:
    component = _required_string(value, label)
    try:
        parts = _portable_parts(component, label)
    except JobFailure as exc:
        raise _invalid_manifest(exc.message) from exc
    if len(parts) != 1:
        raise _invalid_manifest(f"{label} must be one portable directory component")
    return component


def sanitize_job_slug(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise JobFailure("JOB_INPUT_INVALID", "title must be a nonblank string")
    normalized = unicodedata.normalize("NFKC", title).casefold()
    pieces: list[str] = []
    previous_separator = False
    for character in normalized:
        if character.isalnum():
            pieces.append(character)
            previous_separator = False
        elif not previous_separator and pieces:
            pieces.append("-")
            previous_separator = True
    raw_slug = "".join(pieces).strip("-")
    selected: list[str] = []
    for character in raw_slug:
        if _utf16_length("".join(selected) + character) > 72:
            break
        selected.append(character)
    slug = unicodedata.normalize("NFC", "".join(selected)).rstrip("- .")
    if not slug or slug.casefold().split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        slug = "design-job"
    return slug


def _snapshot(value: object, index: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid_manifest(f"source_snapshots[{index}] must be an object")
    if set(value) != _SNAPSHOT_FIELDS:
        raise _invalid_manifest(
            f"source_snapshots[{index}] fields do not match the v1 schema"
        )
    snapshot_id = _parse_uuid(
        value.get("snapshot_id"), f"source_snapshots[{index}].snapshot_id"
    )
    stored_path = _required_string(
        value.get("stored_path"), f"source_snapshots[{index}].stored_path"
    )
    try:
        stored_parts = _portable_parts(
            stored_path, f"source_snapshots[{index}].stored_path"
        )
    except JobFailure as exc:
        raise _invalid_manifest(exc.message) from exc
    if len(stored_parts) < 3 or stored_parts[:2] != ("inputs", "source"):
        raise _invalid_manifest(
            f"source_snapshots[{index}].stored_path must be beneath inputs/source"
        )
    sha256 = _required_string(
        value.get("sha256"), f"source_snapshots[{index}].sha256"
    )
    if _SHA256.fullmatch(sha256) is None:
        raise _invalid_manifest(
            f"source_snapshots[{index}].sha256 must be 64 lowercase hexadecimal characters"
        )
    source_kind = _required_string(
        value.get("source_kind"), f"source_snapshots[{index}].source_kind"
    )
    if source_kind not in {"existing_model", "new_design", "product_family_input"}:
        raise _invalid_manifest(f"source_snapshots[{index}].source_kind is invalid")
    source_revision = value.get("source_model_revision_id")
    if source_revision is not None:
        source_revision = str(
            _parse_uuid(
                source_revision,
                f"source_snapshots[{index}].source_model_revision_id",
            )
        )
    return MappingProxyType(
        {
            "snapshot_id": str(snapshot_id),
            "stored_path": PurePosixPath(*stored_parts).as_posix(),
            "sha256": sha256,
            "source_kind": source_kind,
            "source_model_revision_id": source_revision,
        }
    )


@dataclass(frozen=True)
class DesignJobManifest:
    job_id: UUID
    display_id: str
    job_type: str
    workspace_id: UUID
    title: str
    slug: str
    status: str
    phase: str
    revision: int
    organization_id: str
    design_group_id: str
    family_id: str | None
    directory_name: str
    active_working_copy_id: str | None
    source_snapshots: Sequence[Mapping[str, object]]
    created_at: str
    created_by: str
    updated_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> DesignJobManifest:
        if not isinstance(raw, Mapping):
            raise _invalid_manifest("job manifest must be a JSON object")
        if raw.get("schema_version") != JOB_MANIFEST_SCHEMA:
            raise _invalid_manifest("unsupported job manifest schema_version")
        if set(raw) != _MANIFEST_FIELDS:
            raise _invalid_manifest("job manifest fields do not match the v1 schema")
        job_id = _parse_uuid(raw.get("job_id"), "job_id")
        workspace_id = _parse_uuid(raw.get("workspace_id"), "workspace_id")
        display_id = _required_string(raw.get("display_id"), "display_id")
        if _DISPLAY_ID.fullmatch(display_id) is None:
            raise _invalid_manifest("display_id must use JOB-YYYYMMDD-NNN format")
        try:
            datetime.strptime(display_id[4:12], "%Y%m%d")
        except ValueError as exc:
            raise _invalid_manifest("display_id contains an invalid calendar date") from exc
        job_type = _required_string(raw.get("job_type"), "job_type")
        if job_type not in _JOB_TYPES:
            raise _invalid_manifest("job_type is invalid")
        title = _required_string(raw.get("title"), "title")
        slug = _directory_component(raw.get("slug"), "slug")
        if slug != sanitize_job_slug(slug):
            raise _invalid_manifest("slug must be sanitized")
        status = _required_string(raw.get("status"), "status")
        if status not in _STATUSES:
            raise _invalid_manifest("status is invalid")
        phase = _required_string(raw.get("phase"), "phase")
        if phase not in _JOB_TYPES[job_type]:
            raise _invalid_manifest("phase is invalid for job_type")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise _invalid_manifest("revision must be a non-negative integer")
        organization_id = _required_string(raw.get("organization_id"), "organization_id")
        design_group_id = _required_string(raw.get("design_group_id"), "design_group_id")
        family_value = raw.get("family_id")
        if family_value is not None and (
            not isinstance(family_value, str) or not family_value.strip()
        ):
            raise _invalid_manifest("family_id must be a nonblank string or null")
        family_id = family_value.strip() if isinstance(family_value, str) else None
        directory_name = _directory_component(raw.get("directory_name"), "directory_name")
        if directory_name != f"{display_id}-{slug}":
            raise _invalid_manifest("directory_name does not match display_id and slug")
        active_value = raw.get("active_working_copy_id")
        active_working_copy_id = (
            str(_parse_uuid(active_value, "active_working_copy_id"))
            if active_value is not None
            else None
        )
        snapshots_value = raw.get("source_snapshots")
        if not isinstance(snapshots_value, Sequence) or isinstance(
            snapshots_value, (str, bytes, bytearray)
        ):
            raise _invalid_manifest("source_snapshots must be an array")
        snapshots = tuple(
            _snapshot(value, index) for index, value in enumerate(snapshots_value)
        )
        snapshot_ids = [snapshot["snapshot_id"] for snapshot in snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise _invalid_manifest("source_snapshots must contain unique snapshot_id values")
        stored_paths = [snapshot["stored_path"] for snapshot in snapshots]
        if len(stored_paths) != len(set(stored_paths)):
            raise _invalid_manifest("source_snapshots must contain unique stored_path values")
        collision_keys = [
            _portable_collision_key(PurePosixPath(str(path)).parts)
            for path in stored_paths
        ]
        if len(collision_keys) != len(set(collision_keys)):
            raise _invalid_manifest(
                "source_snapshots contain casefolded or Unicode-normalized path collisions"
            )
        created_at = _parse_timestamp(raw.get("created_at"), "created_at")
        updated_at = _parse_timestamp(raw.get("updated_at"), "updated_at")
        if datetime.fromisoformat(updated_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        ):
            raise _invalid_manifest("updated_at must not precede created_at")
        created_by = _required_string(raw.get("created_by"), "created_by")
        return cls(
            job_id=job_id,
            display_id=display_id,
            job_type=job_type,
            workspace_id=workspace_id,
            title=title,
            slug=slug,
            status=status,
            phase=phase,
            revision=revision,
            organization_id=organization_id,
            design_group_id=design_group_id,
            family_id=family_id,
            directory_name=directory_name,
            active_working_copy_id=active_working_copy_id,
            source_snapshots=snapshots,
            created_at=created_at,
            created_by=created_by,
            updated_at=updated_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOB_MANIFEST_SCHEMA,
            "job_id": str(self.job_id),
            "display_id": self.display_id,
            "job_type": self.job_type,
            "workspace_id": str(self.workspace_id),
            "title": self.title,
            "slug": self.slug,
            "status": self.status,
            "phase": self.phase,
            "revision": self.revision,
            "organization_id": self.organization_id,
            "design_group_id": self.design_group_id,
            "family_id": self.family_id,
            "directory_name": self.directory_name,
            "active_working_copy_id": self.active_working_copy_id,
            "source_snapshots": [dict(snapshot) for snapshot in self.source_snapshots],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class DesignJobRepairResult:
    """A repaired projection and the non-secret mutation audit binding."""

    manifest: DesignJobManifest
    audit: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOB_REPAIR_SCHEMA,
            "job": self.manifest.as_dict(),
            "audit": dict(self.audit),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "DesignJobRepairResult":
        if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "job", "audit"}:
            raise JobFailure("JOB_REPAIR_RESULT_INVALID", "repair result fields do not match the v1 schema")
        if raw.get("schema_version") != JOB_REPAIR_SCHEMA:
            raise JobFailure("JOB_REPAIR_RESULT_INVALID", "unsupported repair result schema_version")
        job = raw.get("job")
        audit = raw.get("audit")
        if not isinstance(audit, Mapping) or set(audit) != {
            "action", "reason", "actor_id", "authoritative_revision", "quarantined_attempts"
        }:
            raise JobFailure("JOB_REPAIR_RESULT_INVALID", "repair audit fields do not match the v1 schema")
        manifest = DesignJobManifest.from_dict(job)  # type: ignore[arg-type]
        if audit.get("action") != "repair":
            raise JobFailure("JOB_REPAIR_RESULT_INVALID", "repair audit action is invalid")
        reason = audit.get("reason")
        actor_id = audit.get("actor_id")
        revision = audit.get("authoritative_revision")
        quarantined = audit.get("quarantined_attempts")
        if not isinstance(reason, str) or not reason.strip() or not isinstance(actor_id, str) or not actor_id.strip() or type(revision) is not int or revision != manifest.revision or not isinstance(quarantined, (list, tuple)):
            raise JobFailure("JOB_REPAIR_RESULT_INVALID", "repair audit is invalid")
        return cls(manifest=manifest, audit=MappingProxyType(dict(audit)))

    def __getattr__(self, name: str) -> object:
        # Retain the manifest-shaped manager result for existing internal callers.
        return getattr(self.manifest, name)


@dataclass(frozen=True)
class _LockedDoctorEvidence:
    """Pinned filesystem facts used to calculate one doctor receipt."""

    report: Mapping[str, object]
    layout_entries: Mapping[str, tuple[object, ...] | None]
    root_entries: tuple[object, ...]
    manifest_bytes: bytes | None
    manifest_sha256: str | None
    manifest_raw: Mapping[str, object] | None
    manifest: DesignJobManifest | None
    verified_snapshots: tuple[Mapping[str, object], ...]
    verified_active_working_copy_id: str | None
    verified_active_working_copy: Mapping[str, object] | None
    verified_attempts: tuple[Mapping[str, object], ...]


class _JobRepository(Protocol):
    def create_design_job(self, **kwargs: object) -> dict[str, Any]: ...
    def record_design_job_directory(self, **kwargs: object) -> dict[str, Any]: ...
    def transition_design_job(self, **kwargs: object) -> dict[str, Any]: ...
    def list_job_working_copies(self, **kwargs: object) -> list[dict[str, Any]]: ...
    def reactivate_design_job_working_copy(self, **kwargs: object) -> dict[str, Any]: ...
    def get_design_job(self, **kwargs: object) -> dict[str, Any]: ...
    def list_design_jobs(self, **kwargs: object) -> list[dict[str, Any]]: ...
    def resolve_design_jobs(self, **kwargs: object) -> list[dict[str, Any]]: ...


def managed_job_path(
    *,
    job_root: Path,
    relative_path: str | os.PathLike[str],
    allow_missing_leaf: bool = False,
) -> Path:
    parts = _portable_parts(relative_path, "managed Job path")
    try:
        canonical_root = validate_managed_path(
            Path(job_root), allow_missing_leaf=False
        ).path
        candidate = canonical_root.joinpath(*parts)
        canonical_candidate = validate_managed_path(
            candidate, allow_missing_leaf=allow_missing_leaf
        ).path
        canonical_candidate.relative_to(canonical_root)
    except SecureFilesystemError as exc:
        raise JobFailure(
            "JOB_PATH_UNSAFE",
            "managed Job path crosses an unsafe filesystem boundary",
        ) from exc
    except ValueError as exc:
        raise JobFailure(
            "JOB_PATH_OUTSIDE", "managed path must remain beneath the Job root"
        ) from exc
    return canonical_candidate


@contextmanager
def locked_job_root(*, job_root: Path) -> Iterator[Path]:
    try:
        before = validate_managed_path(job_root, allow_missing_leaf=False)
        with exclusive_file_lock(before.path / ".job.lock"):
            after = validate_managed_path(before.path, allow_missing_leaf=False)
            if before.identity is not None and after.identity != before.identity:
                raise JobFailure(
                    "JOB_PATH_UNSAFE", "Job root identity changed while acquiring its lock"
                )
            yield after.path
    except SecureFilesystemError as exc:
        raise JobFailure(
            "JOB_PATH_UNSAFE", "Job root cannot be locked safely"
        ) from exc


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _decode_json(read: ManagedFileRead) -> Mapping[str, object]:
    try:
        raw = json.loads(read.content.decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise _invalid_manifest("job.json is not valid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise _invalid_manifest("job.json must contain an object")
    return raw


def _read_json_with_evidence(path: Path) -> tuple[Mapping[str, object], ManagedFileRead]:
    try:
        read = read_managed_file(path)
    except SecureFilesystemError as exc:
        raise _invalid_manifest("job.json is missing or not a stable managed file") from exc
    return _decode_json(read), read


def _read_json(path: Path) -> Mapping[str, object]:
    return _read_json_with_evidence(path)[0]


def _manifest_bytes(manifest: DesignJobManifest) -> bytes:
    return _json_bytes(manifest.as_dict())


class DesignJobManager:
    def __init__(
        self,
        workspace: WorkspaceManifest,
        repository: _JobRepository,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        now_factory: Callable[[], datetime] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self._uuid_factory = uuid_factory
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._checkpoint_callback = checkpoint
        try:
            jobs_root = validate_managed_path(
                workspace.jobs_root, allow_missing_leaf=False
            ).path
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_ROOT_INVALID", "Workspace jobs_root is missing or unsafe"
            ) from exc
        if not jobs_root.is_dir():
            raise JobFailure("JOB_ROOT_INVALID", "Workspace jobs_root must be a directory")

    def _checkpoint(self, name: str) -> None:
        if self._checkpoint_callback is not None:
            self._checkpoint_callback(name)

    @contextmanager
    def _locked_jobs_root(self) -> Iterator[Path]:
        try:
            root = validate_managed_path(
                self.workspace.jobs_root, allow_missing_leaf=False
            )
            with exclusive_file_lock(root.path / ".design-jobs.lock"):
                current = validate_managed_path(root.path, allow_missing_leaf=False)
                if root.identity is not None and current.identity != root.identity:
                    raise JobFailure(
                        "JOB_ROOT_INVALID",
                        "Workspace jobs_root identity changed while acquiring its lock",
                    )
                yield current.path
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_ROOT_INVALID", "Workspace jobs_root cannot be locked safely"
            ) from exc

    def create(
        self,
        *,
        job_type: str,
        title: str,
        organization_id: str,
        design_group_id: str,
        family_id: str | None,
        idempotency_token: str,
        actor_id: str,
    ) -> DesignJobManifest:
        if job_type not in _JOB_TYPES:
            raise JobFailure("JOB_INPUT_INVALID", "job_type is invalid")
        title_value = self._input_string(title, "title")
        organization = self._input_string(organization_id, "organization_id")
        design_group = self._input_string(design_group_id, "design_group_id")
        token = self._input_string(idempotency_token, "idempotency_token")
        actor = self._input_string(actor_id, "actor_id")
        if family_id is not None and not isinstance(family_id, str):
            raise JobFailure("JOB_INPUT_INVALID", "family_id must be a string or null")
        family = family_id.strip() if isinstance(family_id, str) and family_id.strip() else None
        with self._locked_jobs_root() as jobs_root:
            instant = self._now_factory()
            if not isinstance(instant, datetime):
                raise JobFailure("JOB_INPUT_INVALID", "now_factory must return datetime")
            if instant.tzinfo is None or instant.utcoffset() is None:
                instant = instant.replace(tzinfo=timezone.utc)
            job_id = self._uuid_factory()
            if not isinstance(job_id, UUID) or job_id.version is None:
                raise JobFailure("JOB_INPUT_INVALID", "generated job_id is not an RFC-4122 UUID")
            try:
                row = self.repository.create_design_job(
                    job_id=str(job_id),
                    workspace_id=str(self.workspace.workspace_id),
                    display_date=instant.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                    job_type=job_type,
                    title=title_value,
                    slug=sanitize_job_slug(title_value),
                    organization_id=organization,
                    design_group_id=design_group,
                    family_id=family,
                    idempotency_token=token,
                    actor_id=actor,
                )
            except KeyError as exc:
                raise JobFailure(
                    "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                    "Job identity is unknown or outside the authorized scope",
                ) from exc
            self._checkpoint("after_db_provisioning")
            return self._finish_provisioning(
                row=row,
                jobs_root=jobs_root,
                organization_id=organization,
                design_group_id=design_group,
                actor_id=actor,
            )

    @staticmethod
    def _input_string(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise JobFailure("JOB_INPUT_INVALID", f"{label} is required")
        return value.strip()

    def _validate_row_scope(
        self,
        row: Mapping[str, object],
        *,
        organization_id: str,
        design_group_id: str,
    ) -> None:
        if (
            str(row.get("workspace_id")) != str(self.workspace.workspace_id)
            or row.get("organization_id") != organization_id
            or row.get("design_group_id") != design_group_id
        ):
            raise JobFailure(
                "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                "Job identity is unknown or outside the authorized scope",
            )

    def _manifest_from_row(
        self,
        row: Mapping[str, object],
    ) -> DesignJobManifest:
        slug = str(row.get("slug", ""))
        display_id = str(row.get("display_id", ""))
        directory = row.get("directory_name") or f"{display_id}-{slug}"
        payload: dict[str, object] = {
            "schema_version": JOB_MANIFEST_SCHEMA,
            "job_id": row.get("id"),
            "display_id": display_id,
            "job_type": row.get("job_type"),
            "workspace_id": row.get("workspace_id"),
            "title": row.get("title"),
            "slug": slug,
            "status": row.get("status"),
            "phase": row.get("phase"),
            "revision": row.get("revision"),
            "organization_id": row.get("organization_id"),
            "design_group_id": row.get("design_group_id"),
            "family_id": row.get("family_id"),
            "directory_name": directory,
            "active_working_copy_id": row.get("active_working_copy_id"),
            "source_snapshots": list(row.get("source_snapshots") or []),
            "created_at": row.get("created_at"),
            "created_by": row.get("created_by"),
            "updated_at": row.get("updated_at"),
        }
        return DesignJobManifest.from_dict(payload)

    def _identity_payload(
        self, *, row: Mapping[str, object], directory_name: str
    ) -> dict[str, object]:
        return {
            "schema_version": PROVISIONING_IDENTITY_SCHEMA,
            "job_id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "directory_name": directory_name,
        }

    def _assert_provisioning_identity(
        self, root: Path, *, row: Mapping[str, object], directory_name: str
    ) -> None:
        receipt_path = root / "provenance" / "provisioning.json"
        try:
            receipt = _read_json(receipt_path)
        except JobFailure as exc:
            raise JobFailure(
                "JOB_PROVISIONING_IDENTITY_MISMATCH",
                "provisioning tree does not contain a valid identity receipt",
            ) from exc
        if dict(receipt) != self._identity_payload(row=row, directory_name=directory_name):
            raise JobFailure(
                "JOB_PROVISIONING_IDENTITY_MISMATCH",
                "provisioning tree identity does not match the authoritative Job",
            )

    def _create_staging_tree(
        self, stage: Path, *, row: Mapping[str, object], directory_name: str
    ) -> None:
        ensure_managed_directory(stage, parents=False, exist_ok=False)
        try:
            provenance = ensure_managed_directory(
                stage / "provenance", parents=False, exist_ok=False
            ).path
            atomic_publish_new(
                provenance / "provisioning.json",
                _json_bytes(
                    self._identity_payload(row=row, directory_name=directory_name)
                ),
            )
        except Exception:
            remove_owned_tree(
                stage,
                expected_parent=stage.parent,
                label="design Job provisioning attempt",
            )
            raise

    def _create_layout(self, stage: Path) -> None:
        for relative in _DIRECTORY_CONTRACT:
            ensure_managed_directory(
                stage.joinpath(*PurePosixPath(relative).parts),
                parents=True,
                exist_ok=True,
            )

    def _finish_provisioning(
        self,
        *,
        row: Mapping[str, object],
        jobs_root: Path,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> DesignJobManifest:
        self._validate_row_scope(
            row,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if expected_revision is not None and row.get("revision") != expected_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        provisioning_state = row.get("provisioning_state")
        if provisioning_state == "ready":
            try:
                return self._read_ready_manifest_under_lock(
                    row=row,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
            except JobFailure as exc:
                if exc.code != "JOB_MANIFEST_MISMATCH":
                    raise
                return self._finish_ready_projection(
                    row=row,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
        if provisioning_state != "provisioning" or row.get("directory_name") is not None:
            raise JobFailure(
                "JOB_PROVISIONING_INCOMPLETE",
                "authoritative Job provisioning state is inconsistent",
            )
        job_id = str(_parse_uuid(row.get("id"), "job_id"))
        directory_name = self._manifest_from_row(row).directory_name
        provisioning_root = ensure_managed_directory(
            jobs_root / ".provisioning", parents=False, exist_ok=True
        ).path
        stage = provisioning_root / job_id
        final = jobs_root / directory_name
        if final.exists() or final.is_symlink():
            if stage.exists() or stage.is_symlink():
                raise JobFailure(
                    "JOB_PROVISIONING_CONFLICT",
                    "both staged and final Job directories exist",
                )
            try:
                final = validate_managed_path(final, allow_missing_leaf=False).path
            except SecureFilesystemError as exc:
                raise JobFailure(
                    "JOB_PATH_UNSAFE", "final Job directory is unsafe"
                ) from exc
        else:
            if stage.exists() or stage.is_symlink():
                try:
                    stage = validate_managed_path(stage, allow_missing_leaf=False).path
                except SecureFilesystemError as exc:
                    raise JobFailure(
                        "JOB_PATH_UNSAFE", "staged Job directory is unsafe"
                    ) from exc
            else:
                self._create_staging_tree(
                    stage, row=row, directory_name=directory_name
                )
            self._checkpoint("after_temporary_directory")
            with locked_job_root(job_root=stage) as locked_stage:
                fresh = self._fresh_locked_row(
                    original=row,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    directory_name=directory_name,
                    allow_unrecorded=True,
                )
                if (
                    expected_revision is not None
                    and fresh.get("revision") != expected_revision
                ):
                    raise JobFailure(
                        "JOB_STALE_REVISION", "expected Job revision is stale"
                    )
                if fresh.get("provisioning_state") != "provisioning":
                    raise JobFailure(
                        "JOB_PROVISIONING_CONFLICT",
                        "staged Job no longer has provisioning authority",
                    )
                self._assert_provisioning_identity(
                    locked_stage, row=fresh, directory_name=directory_name
                )
                self._create_layout(locked_stage)
                preliminary = self._manifest_from_row(fresh)
                manifest_path = locked_stage / "job.json"
                entries = {entry.name for entry in list_managed_directory(locked_stage)}
                if "job.json" in entries:
                    existing = DesignJobManifest.from_dict(_read_json(manifest_path))
                    self._assert_manifest_matches(existing, preliminary)
                else:
                    atomic_publish_new(manifest_path, _manifest_bytes(preliminary))
                self._checkpoint("after_manifest_write")
            try:
                atomic_publish_directory(stage, final)
            except FileExistsError as exc:
                raise JobFailure(
                    "JOB_PROVISIONING_CONFLICT",
                    "final Job directory already exists",
                ) from exc
            self._checkpoint("after_atomic_rename")
        with locked_job_root(job_root=final) as locked_final:
            fresh = self._fresh_locked_row(
                original=row,
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=directory_name,
                allow_unrecorded=True,
            )
            if (
                expected_revision is not None
                and fresh.get("revision") != expected_revision
            ):
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            self._assert_provisioning_identity(
                locked_final, row=fresh, directory_name=directory_name
            )
            if fresh.get("provisioning_state") == "provisioning":
                try:
                    self.repository.record_design_job_directory(
                        job_id=job_id,
                        organization_id=organization_id,
                        design_group_id=design_group_id,
                        expected_revision=int(fresh["revision"]),
                        directory_name=directory_name,
                        actor_id=actor_id,
                    )
                except KeyError as exc:
                    raise JobFailure(
                        "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                        "Job identity is unknown or outside the authorized scope",
                    ) from exc
                except ValueError as exc:
                    if "stale" not in str(exc) and "already recorded" not in str(exc):
                        raise
            self._checkpoint("after_directory_record")
            authoritative = self._fresh_locked_row(
                original=fresh,
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=directory_name,
                allow_unrecorded=False,
            )
            if authoritative.get("provisioning_state") != "ready":
                raise JobFailure(
                    "JOB_PROVISIONING_INCOMPLETE",
                    "Job directory publication did not reach authoritative ready state",
                )
            current = DesignJobManifest.from_dict(
                _read_json(locked_final / "job.json")
            )
            expected = self._manifest_from_row(authoritative)
            self._assert_recoverable_projection(
                current,
                expected,
                allowed_mismatches={"revision", "updated_at"},
            )
            self._replace_projection(locked_final / "job.json", expected)
            return expected

    def _finish_ready_projection(
        self,
        *,
        row: Mapping[str, object],
        organization_id: str,
        design_group_id: str,
    ) -> DesignJobManifest:
        """Finish only the known directory-record publication boundary."""
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            fresh = self._fresh_locked_row(
                original=row,
                job_id=str(row["id"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=str(row["directory_name"]),
                allow_unrecorded=False,
            )
            actual = DesignJobManifest.from_dict(_read_json(locked / "job.json"))
            expected = self._manifest_from_row(fresh)
            self._assert_recoverable_projection(
                actual,
                expected,
                allowed_mismatches={"revision", "updated_at"},
            )
            if actual.revision + 1 != expected.revision:
                raise JobFailure(
                    "JOB_MANIFEST_MISMATCH",
                    "job.json is not at the recoverable directory-record revision",
                )
            self._replace_projection(locked / "job.json", expected)
            return expected

    def _fresh_locked_row(
        self,
        *,
        original: Mapping[str, object],
        job_id: str,
        organization_id: str,
        design_group_id: str,
        directory_name: str,
        allow_unrecorded: bool,
    ) -> dict[str, Any]:
        fresh = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if str(fresh.get("id")) != str(original.get("id")):
            raise JobFailure("JOB_DIRECTORY_MISMATCH", "Job identity changed while locked")
        if (
            fresh.get("display_id") != original.get("display_id")
            or fresh.get("slug") != original.get("slug")
        ):
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "immutable Job directory components changed while locked",
            )
        recorded = fresh.get("directory_name")
        if recorded != directory_name and not (allow_unrecorded and recorded is None):
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "authoritative Job directory changed while locked",
            )
        if directory_name != f"{fresh.get('display_id')}-{fresh.get('slug')}":
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "Job directory no longer matches immutable identity",
            )
        return fresh

    @staticmethod
    def _assert_recoverable_projection(
        actual: DesignJobManifest,
        expected: DesignJobManifest,
        *,
        allowed_mismatches: set[str],
    ) -> None:
        DesignJobManager._assert_manifest_matches_bindings(actual, expected)
        actual_payload = actual.as_dict()
        expected_payload = expected.as_dict()
        mismatches = {
            field
            for field in _AUTHORITATIVE_MANIFEST_FIELDS
            if actual_payload[field] != expected_payload[field]
        }
        if not mismatches <= allowed_mismatches:
            raise JobFailure(
                "JOB_MANIFEST_MISMATCH",
                "job.json differs beyond the recoverable projection boundary",
            )

    @staticmethod
    def _replace_projection(path: Path, manifest: DesignJobManifest) -> None:
        try:
            atomic_replace(path, _manifest_bytes(manifest))
        except (OSError, SecureFilesystemError) as exc:
            raise JobFailure(
                "JOB_PROJECTION_INCOMPLETE",
                "PostgreSQL is authoritative but job.json publication is incomplete",
            ) from exc

    def _get_authoritative_row(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, Any]:
        try:
            row = self.repository.get_design_job(
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
            )
        except KeyError as exc:
            raise JobFailure(
                "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                "Job identity is unknown or outside the authorized scope",
            ) from exc
        self._validate_row_scope(
            row,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        return row

    def _final_path(self, row: Mapping[str, object]) -> Path:
        directory_name = row.get("directory_name")
        if not isinstance(directory_name, str):
            raise JobFailure(
                "JOB_PROVISIONING_INCOMPLETE", "Job directory is not yet recorded"
            )
        try:
            parts = _portable_parts(directory_name, "directory_name")
        except JobFailure as exc:
            raise JobFailure("JOB_PATH_UNSAFE", exc.message) from exc
        if len(parts) != 1:
            raise JobFailure("JOB_PATH_UNSAFE", "Job directory identity is invalid")
        expected = f"{row.get('display_id')}-{row.get('slug')}"
        if directory_name != expected:
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "authoritative directory does not match immutable Job identity",
            )
        candidate = self.workspace.jobs_root / directory_name
        try:
            return validate_managed_path(candidate, allow_missing_leaf=False).path
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_DIRECTORY_MISSING", "recorded Job directory is missing or unsafe"
            ) from exc

    def _read_ready_manifest(self, row: Mapping[str, object]) -> DesignJobManifest:
        if row.get("provisioning_state") != "ready":
            raise JobFailure(
                "JOB_PROVISIONING_INCOMPLETE", "Job provisioning is incomplete"
            )
        root = self._final_path(row)
        manifest = DesignJobManifest.from_dict(_read_json(root / "job.json"))
        expected = self._manifest_from_row(row)
        self._assert_manifest_matches(manifest, expected)
        return manifest

    def _read_ready_manifest_under_lock(
        self,
        *,
        row: Mapping[str, object],
        organization_id: str,
        design_group_id: str,
    ) -> DesignJobManifest:
        root = self._final_path(row)
        with locked_job_root(job_root=root):
            fresh = self._fresh_locked_row(
                original=row,
                job_id=str(row["id"]),
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=str(row["directory_name"]),
                allow_unrecorded=False,
            )
            return self._read_ready_manifest(fresh)

    @staticmethod
    def _assert_manifest_matches_bindings(
        actual: DesignJobManifest, expected: DesignJobManifest
    ) -> None:
        if (
            actual.active_working_copy_id != expected.active_working_copy_id
            or tuple(actual.source_snapshots) != tuple(expected.source_snapshots)
        ):
            raise JobFailure(
                "JOB_OPERATIONAL_BINDING_FORGED",
                "core Job manifest operational bindings disagree with PostgreSQL",
            )

    @staticmethod
    def _assert_manifest_matches(
        actual: DesignJobManifest, expected: DesignJobManifest
    ) -> None:
        DesignJobManager._assert_manifest_matches_bindings(actual, expected)
        actual_payload = actual.as_dict()
        expected_payload = expected.as_dict()
        mismatches = [
            field
            for field in _AUTHORITATIVE_MANIFEST_FIELDS
            if actual_payload[field] != expected_payload[field]
        ]
        if mismatches:
            raise JobFailure(
                "JOB_MANIFEST_MISMATCH",
                "job.json disagrees with PostgreSQL authority",
            )

    @contextmanager
    def locked_active_mechanical_design_job(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        family_id: str | None = None,
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield one freshly authorized active mechanical-design Job under its lock."""
        if type(expected_job_revision) is not int or expected_job_revision < 0:
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "expected_job_revision must be a non-negative integer",
            )
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if row.get("revision") != expected_job_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            try:
                fresh = self._fresh_locked_row(
                    original=row,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    directory_name=str(row["directory_name"]),
                    allow_unrecorded=False,
                )
            except JobFailure:
                raise
            except Exception as exc:
                raise JobFailure(
                    "JOB_ACCESS_UNAVAILABLE",
                    "authorized Job state is unavailable; reauthorize and retry",
                ) from exc
            if fresh.get("revision") != expected_job_revision:
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            if fresh.get("status") != "active" or fresh.get("job_type") != "mechanical_design":
                raise JobFailure(
                    "JOB_NOT_ACTIVE_MECHANICAL_DESIGN",
                    "working-copy creation requires an active mechanical_design Job",
                )
            if fresh.get("provisioning_state") != "ready":
                raise JobFailure(
                    "JOB_PROVISIONING_INCOMPLETE", "Job provisioning is incomplete"
                )
            if family_id is not None and fresh.get("family_id") != family_id:
                raise JobFailure(
                    "JOB_FAMILY_MISMATCH",
                    "working-copy family does not match the authorized Job",
                )
            self._read_ready_manifest(fresh)
            yield locked, fresh

    @contextmanager
    def locked_active_job(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        organization_id: str,
        design_group_id: str,
        job_type: str,
        family_id: str | None = None,
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield one active Job of the explicitly requested product-operation type."""
        if job_type not in _JOB_TYPES:
            raise JobFailure("JOB_INPUT_INVALID", "job_type is invalid")
        if type(expected_job_revision) is not int or expected_job_revision < 0:
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "expected_job_revision must be a non-negative integer",
            )
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if row.get("revision") != expected_job_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            fresh = self._fresh_locked_row(
                original=row,
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=str(row["directory_name"]),
                allow_unrecorded=False,
            )
            if fresh.get("revision") != expected_job_revision:
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            if fresh.get("status") != "active" or fresh.get("job_type") != job_type:
                raise JobFailure(
                    "JOB_TYPE_OR_STATUS_MISMATCH",
                    f"operation requires an active {job_type} Job",
                )
            if fresh.get("provisioning_state") != "ready":
                raise JobFailure(
                    "JOB_PROVISIONING_INCOMPLETE", "Job provisioning is incomplete"
                )
            if family_id is not None and fresh.get("family_id") != family_id:
                raise JobFailure(
                    "JOB_FAMILY_MISMATCH",
                    "operation family does not match the authorized Job",
                )
            self._read_ready_manifest(fresh)
            yield locked, fresh

    def publish_authoritative_manifest_locked(
        self,
        *,
        locked_root: Path,
        job_id: str,
        expected_job_revision: int,
        working_copy_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> DesignJobManifest:
        """Publish a committed binding projection while the caller holds the Job lock."""
        fresh = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if int(fresh.get("revision", -1)) != expected_job_revision + 1:
            raise JobFailure(
                "JOB_PROJECTION_INCOMPLETE",
                "authoritative working-copy binding committed at an unexpected revision",
            )
        if str(fresh.get("active_working_copy_id")) != working_copy_id:
            raise JobFailure(
                "JOB_PROJECTION_INCOMPLETE",
                "authoritative active working-copy binding is incomplete",
            )
        expected_root = self._final_path(fresh)
        if expected_root != locked_root:
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "authorized Job directory changed during working-copy publication",
            )
        manifest = self._manifest_from_row(fresh)
        self._replace_projection(locked_root / "job.json", manifest)
        return manifest

    def read_authoritative_manifest_locked(
        self,
        *,
        locked_root: Path,
        authoritative_row: Mapping[str, object],
    ) -> DesignJobManifest:
        """Read a current authoritative projection while its Job root is locked."""
        if self._final_path(authoritative_row) != locked_root:
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "authorized Job directory changed while reading its projection",
            )
        return self._read_ready_manifest(authoritative_row)

    def publish_authoritative_revision_locked(
        self,
        *,
        locked_root: Path,
        job_id: str,
        expected_previous_revision: int,
        organization_id: str,
        design_group_id: str,
    ) -> DesignJobManifest:
        """Publish an operation-owned Job revision after its database transaction."""
        fresh = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if int(fresh.get("revision", -1)) != expected_previous_revision + 1:
            raise JobFailure(
                "JOB_PROJECTION_INCOMPLETE",
                "authoritative operation committed at an unexpected Job revision",
            )
        if self._final_path(fresh) != locked_root:
            raise JobFailure(
                "JOB_DIRECTORY_MISMATCH",
                "authorized Job directory changed during operation publication",
            )
        manifest = self._manifest_from_row(fresh)
        self._replace_projection(locked_root / "job.json", manifest)
        return manifest

    def get(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> DesignJobManifest:
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        return self._read_ready_manifest_under_lock(
            row=row,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )

    def list(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        status: str | None = None,
        job_type: str | None = None,
        family_id: str | None = None,
    ) -> list[DesignJobManifest]:
        rows = self.repository.list_design_jobs(
            organization_id=organization_id,
            design_group_id=design_group_id,
            status=status,
            job_type=job_type,
            family_id=family_id,
        )
        manifests: list[DesignJobManifest] = []
        for row in rows:
            self._validate_row_scope(
                row,
                organization_id=organization_id,
                design_group_id=design_group_id,
            )
            manifests.append(
                self._read_ready_manifest_under_lock(
                    row=row,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
            )
        return manifests

    def resolve(
        self,
        *,
        organization_id: str,
        design_group_id: str,
        query: str,
        job_type: str | None = None,
        family_id: str | None = None,
        statuses: tuple[str, ...] = ("active", "blocked"),
    ) -> list[DesignJobManifest]:
        rows = self.repository.resolve_design_jobs(
            organization_id=organization_id,
            design_group_id=design_group_id,
            query=query,
            job_type=job_type,
            family_id=family_id,
            statuses=statuses,
        )
        manifests: list[DesignJobManifest] = []
        for row in rows:
            self._validate_row_scope(
                row,
                organization_id=organization_id,
                design_group_id=design_group_id,
            )
            manifests.append(
                self._read_ready_manifest_under_lock(
                    row=row,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                )
            )
        return manifests

    def _transition(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        expected_revision: int,
        status: str,
        phase: str,
        actor_id: str,
        reason: str,
    ) -> DesignJobManifest:
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if row.get("revision") != expected_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            try:
                fresh = self._fresh_locked_row(
                    original=row,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    directory_name=str(row["directory_name"]),
                    allow_unrecorded=False,
                )
            except Exception as exc:
                raise JobFailure(
                    "JOB_ACCESS_UNAVAILABLE",
                    "authorized Job state is unavailable; reauthorize and retry",
                ) from exc
            if fresh.get("revision") != expected_revision:
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            if status == "active":
                if fresh.get("status") not in _TERMINAL_STATUSES:
                    raise JobFailure(
                        "JOB_NOT_TERMINAL", "only a terminal Job can be reopened"
                    )
            elif fresh.get("status") in _TERMINAL_STATUSES:
                raise JobFailure(
                    "JOB_TERMINAL",
                    "terminal Job mutations require an explicit reopen operation",
                )
            self._read_ready_manifest(fresh)
            try:
                self.repository.transition_design_job(
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    expected_revision=expected_revision,
                    status=status,
                    phase=phase,
                    actor_id=actor_id,
                    reason=reason,
                )
            except KeyError as exc:
                raise JobFailure(
                    "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                    "Job identity is unknown or outside the authorized scope",
                ) from exc
            except ValueError as exc:
                if "stale" in str(exc):
                    raise JobFailure(
                        "JOB_STALE_REVISION", "expected Job revision is stale"
                    ) from exc
                raise
            self._checkpoint("after_lifecycle_transition")
            authoritative = self._fresh_locked_row(
                original=fresh,
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=str(fresh["directory_name"]),
                allow_unrecorded=False,
            )
            if int(authoritative["revision"]) != expected_revision + 1:
                raise JobFailure(
                    "JOB_PROJECTION_INCOMPLETE",
                    "authoritative transition committed at an unexpected revision",
                )
            updated = self._manifest_from_row(authoritative)
            self._replace_projection(locked / "job.json", updated)
        return updated

    def reactivate_working_copy_for_delivery(
        self,
        *,
        job_id: str,
        expected_job_revision: int,
        working_copy_id: str,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
    ) -> DesignJobManifest:
        """Restore one closed Job's sole verified copy for delivery approval only."""
        if type(expected_job_revision) is not int or expected_job_revision < 0:
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "expected_job_revision must be a non-negative integer",
            )
        requested = self._input_string(working_copy_id, "working_copy_id")
        actor = self._input_string(actor_id, "actor_id")
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if row.get("revision") != expected_job_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            fresh = self._fresh_locked_row(
                original=row,
                job_id=job_id,
                organization_id=organization_id,
                design_group_id=design_group_id,
                directory_name=str(row["directory_name"]),
                allow_unrecorded=False,
            )
            if fresh.get("revision") != expected_job_revision:
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            if (
                fresh.get("status") != "active"
                or fresh.get("job_type") != "mechanical_design"
                or fresh.get("provisioning_state") != "ready"
                or fresh.get("phase") not in {"delivery", "lesson_capture"}
            ):
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNAVAILABLE",
                    "working-copy reactivation is limited to an active delivery-stage mechanical Design Job",
                )
            active = fresh.get("active_working_copy_id")
            if active is not None:
                if str(active) == requested:
                    return self._read_ready_manifest(fresh)
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "another working copy is already active for this Design Job",
                )
            self._read_ready_manifest(fresh)
            try:
                candidates = self.repository.list_job_working_copies(
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    actor_id=actor,
                )
            except KeyError as exc:
                raise JobFailure(
                    "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                    "Job identity is unknown or outside the authorized scope",
                ) from exc
            if not candidates:
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNAVAILABLE",
                    "the Design Job has no governed working copy to restore",
                )
            if len(candidates) != 1:
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_AMBIGUOUS",
                    "the Design Job has multiple governed working copies; delivery recovery cannot choose one",
                )
            candidate = candidates[0]
            if str(candidate.get("id")) != requested:
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "the confirmed working copy is not the Design Job's sole governed copy",
                )
            relative = candidate.get("working_relative_path")
            authoritative_path = candidate.get("working_path")
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(authoritative_path, str)
                or not authoritative_path
            ):
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "the governed working-copy path is incomplete",
                )
            try:
                working_path = managed_job_path(
                    job_root=locked,
                    relative_path=relative,
                    allow_missing_leaf=False,
                )
                authoritative = validate_managed_path(
                    Path(os.path.abspath(authoritative_path)),
                    allow_missing_leaf=False,
                ).path
                working_read = read_managed_file(working_path)
            except (JobFailure, SecureFilesystemError) as exc:
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "the governed working-copy file is missing or unsafe",
                ) from exc
            if (
                working_path != authoritative
                or working_path.suffix.casefold() != ".fcstd"
                or working_read.link_count != 1
                or not working_read.content
                or candidate.get("working_sha256") != working_read.sha256
                or candidate.get("working_size_bytes") != working_read.size_bytes
            ):
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "the governed working-copy evidence no longer matches the authoritative binding",
                )
            try:
                self.repository.reactivate_design_job_working_copy(
                    job_id=job_id,
                    expected_revision=expected_job_revision,
                    working_copy_id=requested,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    actor_id=actor,
                )
            except KeyError as exc:
                raise JobFailure(
                    "JOB_NOT_FOUND_OR_UNAUTHORIZED",
                    "Job identity is unknown or outside the authorized scope",
                ) from exc
            except ValueError as exc:
                if "stale" in str(exc):
                    raise JobFailure(
                        "JOB_STALE_REVISION", "expected Job revision is stale"
                    ) from exc
                raise JobFailure(
                    "JOB_WORKING_COPY_REACTIVATION_UNSAFE",
                    "the authoritative reactivation candidate changed during recovery",
                ) from exc
            return self.publish_authoritative_manifest_locked(
                locked_root=locked,
                job_id=job_id,
                expected_job_revision=expected_job_revision,
                working_copy_id=requested,
                organization_id=organization_id,
                design_group_id=design_group_id,
            )

    def close(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        expected_revision: int,
        status: str,
        phase: str,
        actor_id: str,
        reason: str,
    ) -> DesignJobManifest:
        if status not in _TERMINAL_STATUSES:
            raise JobFailure(
                "JOB_INPUT_INVALID",
                "close status must be completed, cancelled, or archived",
            )
        return self._transition(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
            expected_revision=expected_revision,
            status=status,
            phase=phase,
            actor_id=actor_id,
            reason=reason,
        )

    def reopen(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        expected_revision: int,
        phase: str,
        actor_id: str,
        reason: str,
    ) -> DesignJobManifest:
        return self._transition(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
            expected_revision=expected_revision,
            status="active",
            phase=phase,
            actor_id=actor_id,
            reason=reason,
        )

    @staticmethod
    def _issue(code: str, message: str) -> dict[str, str]:
        return {"code": code, "message": message}

    def _doctor_report(
        self,
        *,
        row: Mapping[str, object],
        issues: list[dict[str, str]],
        manifest_sha256: str | None,
        verified_snapshots: Sequence[Mapping[str, object]],
        verified_active_working_copy_id: str | None,
        verified_active_working_copy: Mapping[str, object] | None,
        verified_attempts: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        report: dict[str, object] = {
            "schema_version": JOB_DOCTOR_SCHEMA,
            "job_id": str(row.get("id")),
            "workspace_id": str(row.get("workspace_id")),
            "authoritative_revision": row.get("revision"),
            "authoritative_updated_at": _parse_timestamp(
                row.get("updated_at"), "updated_at"
            ),
            "manifest_sha256": manifest_sha256,
            "verified_snapshots": [dict(snapshot) for snapshot in verified_snapshots],
            "verified_active_working_copy_id": verified_active_working_copy_id,
            "verified_active_working_copy": (
                {
                    **dict(verified_active_working_copy),
                    "identity": dict(verified_active_working_copy["identity"]),
                }
                if verified_active_working_copy is not None
                else None
            ),
            "verified_attempts": [
                {
                    **dict(attempt),
                    "directory_identity": dict(attempt["directory_identity"]),
                    "artifacts": [dict(item) for item in attempt["artifacts"]],
                }
                for attempt in verified_attempts
            ],
            "status": "ok" if not issues else "blocked",
            "issues": issues,
        }
        receipt_payload = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        report["receipt_sha256"] = hashlib.sha256(receipt_payload).hexdigest()
        return report

    @staticmethod
    def _public_doctor_report(evidence: _LockedDoctorEvidence) -> dict[str, object]:
        """Copy immutable internal receipt facts into the public doctor shape."""
        report = dict(evidence.report)
        report["issues"] = [dict(issue) for issue in evidence.report["issues"]]  # type: ignore[index]
        report["verified_snapshots"] = [
            dict(snapshot) for snapshot in evidence.verified_snapshots
        ]
        report["verified_active_working_copy_id"] = (
            evidence.verified_active_working_copy_id
        )
        report["verified_active_working_copy"] = (
            {
                **dict(evidence.verified_active_working_copy),
                "identity": dict(evidence.verified_active_working_copy["identity"]),
            }
            if evidence.verified_active_working_copy is not None
            else None
        )
        report["verified_attempts"] = [
            {
                **dict(attempt),
                "directory_identity": dict(attempt["directory_identity"]),
                "artifacts": [dict(item) for item in attempt["artifacts"]],
            }
            for attempt in evidence.verified_attempts
        ]
        return report

    def _locked_doctor_evidence(
        self, *, locked: Path, row: Mapping[str, object]
    ) -> _LockedDoctorEvidence:
        """Read all receipt facts once from pinned objects while holding the lock."""
        issues: list[dict[str, str]] = []
        layout_entries: dict[str, tuple[object, ...] | None] = {}
        for relative in _DIRECTORY_CONTRACT:
            try:
                candidate = managed_job_path(
                    job_root=locked,
                    relative_path=relative,
                    allow_missing_leaf=False,
                )
                layout_entries[relative] = tuple(list_managed_directory(candidate))
            except (JobFailure, SecureFilesystemError) as exc:
                layout_entries[relative] = None
                issues.append(
                    self._issue(
                        exc.code,
                        f"required managed directory is missing or unsafe: {relative}",
                    )
                )
        try:
            root_entries = tuple(list_managed_directory(locked))
        except SecureFilesystemError as exc:
            raise JobFailure(
                "JOB_REPAIR_UNSAFE", "Job root cannot be enumerated safely"
            ) from exc

        manifest_bytes: bytes | None = None
        manifest_sha256: str | None = None
        manifest_raw: Mapping[str, object] | None = None
        manifest: DesignJobManifest | None = None
        try:
            raw, manifest_read = _read_json_with_evidence(locked / "job.json")
            manifest_bytes = manifest_read.content
            manifest_sha256 = manifest_read.sha256
            manifest_raw = MappingProxyType(dict(raw))
            manifest = DesignJobManifest.from_dict(manifest_raw)
            expected = self._manifest_from_row(row)
            actual_payload = manifest.as_dict()
            expected_payload = expected.as_dict()
            if (
                manifest.active_working_copy_id != expected.active_working_copy_id
                or tuple(manifest.source_snapshots) != tuple(expected.source_snapshots)
            ):
                issues.append(
                    self._issue(
                        "JOB_OPERATIONAL_BINDING_FORGED",
                        "core Job manifest operational bindings disagree with PostgreSQL",
                    )
                )
            if manifest.revision != expected.revision:
                issues.append(
                    self._issue(
                        "JOB_REVISION_MISMATCH",
                        "job.json revision disagrees with PostgreSQL",
                    )
                )
            if any(
                field != "revision" and actual_payload[field] != expected_payload[field]
                for field in _AUTHORITATIVE_MANIFEST_FIELDS
            ):
                issues.append(
                    self._issue(
                        "JOB_MANIFEST_MISMATCH",
                        "job.json authoritative fields disagree with PostgreSQL",
                    )
                )
        except JobFailure as exc:
            issues.append(self._issue(exc.code, exc.message))

        verified: list[Mapping[str, object]] = []
        for snapshot in self._manifest_from_row(row).source_snapshots:
            try:
                snapshot_path = managed_job_path(
                    job_root=locked,
                    relative_path=str(snapshot["stored_path"]),
                    allow_missing_leaf=False,
                )
                snapshot_read = read_managed_file(snapshot_path)
                if snapshot_read.sha256 != snapshot["sha256"]:
                    issues.append(
                        self._issue(
                            "JOB_SOURCE_SNAPSHOT_HASH_MISMATCH",
                            "an authoritative source snapshot no longer matches its SHA-256",
                        )
                    )
                    continue
                verified.append(MappingProxyType(dict(snapshot)))
            except (JobFailure, SecureFilesystemError):
                issues.append(
                    self._issue(
                        "JOB_SOURCE_SNAPSHOT_UNAVAILABLE",
                        "an authoritative source snapshot is missing or unsafe",
                    )
                )
        verified_snapshots = tuple(verified)
        verified_active_working_copy_id: str | None = None
        verified_active_working_copy: Mapping[str, object] | None = None
        expected_manifest = self._manifest_from_row(row)
        if expected_manifest.active_working_copy_id is not None:
            raw_working_path = row.get("active_working_path")
            try:
                if not isinstance(raw_working_path, str) or not raw_working_path:
                    raise ValueError("active working-copy path is unavailable")
                working_path = validate_managed_path(
                    Path(raw_working_path), allow_missing_leaf=False
                ).path
                working_path.relative_to(locked)
                working_read = read_managed_file(working_path)
                if not working_read.content or working_path.suffix.casefold() != ".fcstd":
                    raise ValueError("active working-copy file is invalid")
                authoritative_relative = row.get("active_working_relative_path")
                authoritative_sha = row.get("active_working_sha256")
                authoritative_size = row.get("active_working_size_bytes")
                if (
                    authoritative_relative != working_path.relative_to(locked).as_posix()
                    or authoritative_sha != working_read.sha256
                    or authoritative_size != working_read.size_bytes
                ):
                    raise ValueError("active working-copy evidence disagrees with PostgreSQL")
                verified_active_working_copy_id = (
                    expected_manifest.active_working_copy_id
                )
                verified_active_working_copy = MappingProxyType(
                    {
                        "working_copy_id": expected_manifest.active_working_copy_id,
                        "relative_path": working_path.relative_to(locked).as_posix(),
                        "identity": MappingProxyType(
                            {
                                "volume": working_read.identity.volume,
                                "file_index": working_read.identity.file_index,
                            }
                        ),
                        "sha256": working_read.sha256,
                        "size_bytes": working_read.size_bytes,
                    }
                )
            except (JobFailure, SecureFilesystemError, ValueError):
                issues.append(
                    self._issue(
                        "JOB_ACTIVE_WORKING_COPY_UNAVAILABLE",
                        "the authoritative active working copy is missing or unsafe",
                    )
                )
        verified_attempts_list: list[Mapping[str, object]] = []
        known_snapshot_ids = {
            str(snapshot["snapshot_id"])
            for snapshot in expected_manifest.source_snapshots
        }
        raw_working_ids = row.get("working_copy_ids")
        known_working_ids = (
            {str(item) for item in raw_working_ids}
            if isinstance(raw_working_ids, list)
            else (
                {expected_manifest.active_working_copy_id}
                if expected_manifest.active_working_copy_id is not None
                else set()
            )
        )
        for relative_parent, artifact_kind, known_ids in (
            ("inputs/source", "source_snapshot", known_snapshot_ids),
            ("models/working", "working_copy", known_working_ids),
        ):
            parent_entries = layout_entries.get(relative_parent)
            if parent_entries is None:
                continue
            for directory_entry in parent_entries:
                if not directory_entry.is_directory or directory_entry.name in known_ids:
                    continue
                attempt_relative = f"{relative_parent}/{directory_entry.name}"
                try:
                    attempt_path = managed_job_path(
                        job_root=locked,
                        relative_path=attempt_relative,
                        allow_missing_leaf=False,
                    )
                    inventory = tuple(list_managed_directory(attempt_path))
                    by_name = {entry.name: entry for entry in inventory}
                    if (
                        len(by_name) != len(inventory)
                        or ".binding-attempt.json" not in by_name
                        or by_name[".binding-attempt.json"].is_directory
                    ):
                        raise ValueError("attempt inventory has no regular receipt")
                    receipt_read = read_managed_file(
                        attempt_path / ".binding-attempt.json"
                    )
                    receipt = json.loads(receipt_read.content.decode("utf-8"))
                    if not isinstance(receipt, dict) or set(receipt) != {
                        "schema_version",
                        "job_id",
                        "expected_job_revision",
                        "artifact_kind",
                        "artifact_id",
                        "source_sha256",
                        "artifacts",
                    }:
                        raise ValueError("attempt receipt fields are invalid")
                    artifacts = receipt.get("artifacts")
                    if (
                        receipt.get("schema_version")
                        != "MechanicalDesignJobBindingAttempt/v2"
                        or receipt.get("job_id") != str(row.get("id"))
                        or receipt.get("artifact_kind") != artifact_kind
                        or receipt.get("artifact_id") != directory_entry.name
                        or type(receipt.get("expected_job_revision")) is not int
                        or not isinstance(artifacts, list)
                        or len(artifacts) != 1
                        or not isinstance(artifacts[0], dict)
                    ):
                        raise ValueError("attempt receipt authority is invalid")
                    artifact = artifacts[0]
                    if set(artifact) != {
                        "filename", "sha256", "size_bytes", "identity"
                    }:
                        raise ValueError("attempt artifact evidence is invalid")
                    filename = artifact.get("filename")
                    if (
                        not isinstance(filename, str)
                        or filename not in by_name
                        or by_name[filename].is_directory
                        or set(by_name) != {".binding-attempt.json", filename}
                    ):
                        raise ValueError("attempt inventory is not exact")
                    artifact_read = read_managed_file(attempt_path / filename)
                    identity = artifact.get("identity")
                    if (
                        not isinstance(identity, dict)
                        or set(identity) != {"volume", "file_index"}
                        or artifact.get("sha256") != artifact_read.sha256
                        or artifact.get("size_bytes") != artifact_read.size_bytes
                        or identity.get("volume") != artifact_read.identity.volume
                        or identity.get("file_index")
                        != artifact_read.identity.file_index
                        or artifact_read.link_count != 1
                    ):
                        raise ValueError("attempt artifact changed after receipt")
                    verified_attempts_list.append(
                        MappingProxyType(
                            {
                                "artifact_kind": artifact_kind,
                                "artifact_id": directory_entry.name,
                                "relative_path": attempt_relative,
                                "directory_identity": MappingProxyType(
                                    {
                                        "volume": directory_entry.identity.volume,
                                        "file_index": directory_entry.identity.file_index,
                                    }
                                ),
                                "receipt_sha256": receipt_read.sha256,
                                "artifacts": (MappingProxyType(dict(artifact)),),
                            }
                        )
                    )
                    issues.append(
                        self._issue(
                            "JOB_PRESERVED_ATTEMPT_FOUND",
                            "a receipt-verified uncommitted binding attempt requires quarantine",
                        )
                    )
                except (JobFailure, SecureFilesystemError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    issues.append(
                        self._issue(
                            "JOB_ATTEMPT_RECEIPT_INVALID",
                            "an uncommitted binding attempt cannot be safely quarantined",
                        )
                    )
        verified_attempts = tuple(verified_attempts_list)
        public_report = self._doctor_report(
            row=row,
            issues=issues,
            manifest_sha256=manifest_sha256,
            verified_snapshots=verified_snapshots,
            verified_active_working_copy_id=verified_active_working_copy_id,
            verified_active_working_copy=verified_active_working_copy,
            verified_attempts=verified_attempts,
        )
        immutable_report = MappingProxyType(
            {
                **public_report,
                "issues": tuple(MappingProxyType(dict(issue)) for issue in issues),
                "verified_snapshots": verified_snapshots,
                "verified_active_working_copy_id": verified_active_working_copy_id,
                "verified_active_working_copy": verified_active_working_copy,
                "verified_attempts": verified_attempts,
            }
        )
        return _LockedDoctorEvidence(
            report=immutable_report,
            layout_entries=MappingProxyType(dict(layout_entries)),
            root_entries=root_entries,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
            manifest_raw=manifest_raw,
            manifest=manifest,
            verified_snapshots=verified_snapshots,
            verified_active_working_copy_id=verified_active_working_copy_id,
            verified_active_working_copy=verified_active_working_copy,
            verified_attempts=verified_attempts,
        )

    def doctor(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
    ) -> dict[str, object]:
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        issues: list[dict[str, str]] = []
        if row.get("provisioning_state") != "ready":
            issues.append(
                self._issue(
                    "JOB_PROVISIONING_INCOMPLETE",
                    "authoritative Job provisioning is incomplete",
                )
            )
            return self._doctor_report(
                row=row,
                issues=issues,
                manifest_sha256=None,
                verified_snapshots=(),
                verified_active_working_copy_id=None,
                verified_active_working_copy=None,
                verified_attempts=(),
            )
        try:
            root = self._final_path(row)
        except JobFailure as exc:
            issues.append(self._issue(exc.code, exc.message))
            return self._doctor_report(
                row=row,
                issues=issues,
                manifest_sha256=None,
                verified_snapshots=(),
                verified_active_working_copy_id=None,
                verified_active_working_copy=None,
                verified_attempts=(),
            )
        with locked_job_root(job_root=root) as locked:
            try:
                fresh = self._fresh_locked_row(
                    original=row,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    directory_name=str(row["directory_name"]),
                    allow_unrecorded=False,
                )
            except Exception as exc:
                # Authorization is checked again while holding the Job lock.  A
                # stale/revoked/failed authority read must never be converted
                # into disk diagnostics, which could disclose the Job path.
                raise JobFailure(
                    "JOB_ACCESS_UNAVAILABLE",
                    "authorized Job state is unavailable; reauthorize and retry",
                ) from exc
            evidence = self._locked_doctor_evidence(locked=locked, row=fresh)
        return self._public_doctor_report(evidence)

    def repair(
        self,
        *,
        job_id: str,
        organization_id: str,
        design_group_id: str,
        actor_id: str,
        expected_revision: int,
        doctor_receipt_hash: str,
        reason: str,
    ) -> DesignJobRepairResult:
        if type(expected_revision) is not int or expected_revision < 0:
            raise JobFailure(
                "JOB_INPUT_INVALID", "expected_revision must be a non-negative integer"
            )
        if not isinstance(doctor_receipt_hash, str) or _SHA256.fullmatch(doctor_receipt_hash) is None:
            raise JobFailure("JOB_INPUT_INVALID", "doctor_receipt_hash must be a SHA-256 digest")
        if not isinstance(reason, str) or not reason.strip():
            raise JobFailure("JOB_INPUT_INVALID", "reason is required")
        row = self._get_authoritative_row(
            job_id=job_id,
            organization_id=organization_id,
            design_group_id=design_group_id,
        )
        if row.get("revision") != expected_revision:
            raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
        if row.get("provisioning_state") == "provisioning":
            raise JobFailure(
                "JOB_REPAIR_UNSAFE",
                "Job provisioning is incomplete and cannot be receipt-bound repaired",
            )
        root = self._final_path(row)
        with locked_job_root(job_root=root) as locked:
            try:
                fresh = self._fresh_locked_row(
                    original=row,
                    job_id=job_id,
                    organization_id=organization_id,
                    design_group_id=design_group_id,
                    directory_name=str(row["directory_name"]),
                    allow_unrecorded=False,
                )
            except Exception as exc:
                raise JobFailure(
                    "JOB_ACCESS_UNAVAILABLE",
                    "authorized Job state is unavailable; reauthorize and retry",
                ) from exc
            if fresh.get("revision") != expected_revision:
                raise JobFailure("JOB_STALE_REVISION", "expected Job revision is stale")
            # Recompute the receipt under this same lock.  This makes the
            # caller's doctor evidence a precondition of the write, rather
            # than a check made by a separate service-level operation.
            evidence = self._locked_doctor_evidence(locked=locked, row=fresh)
            if evidence.report["receipt_sha256"] != doctor_receipt_hash:
                raise JobFailure(
                    "JOB_DOCTOR_RECEIPT_MISMATCH",
                    "doctor receipt does not match the authorized Job state",
                )
            self._checkpoint("after_repair_receipt_comparison")
            if any(entries is None for entries in evidence.layout_entries.values()):
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "Job directory contract is incomplete or unsafe",
                )
            quarantined_attempts: list[Mapping[str, object]] = []
            if evidence.verified_attempts:
                quarantine_root = ensure_managed_directory(
                    locked / "provenance" / "quarantine",
                    parents=False,
                    exist_ok=True,
                ).path
                for attempt in evidence.verified_attempts:
                    source = managed_job_path(
                        job_root=locked,
                        relative_path=str(attempt["relative_path"]),
                        allow_missing_leaf=False,
                    )
                    identity_raw = attempt["directory_identity"]
                    assert isinstance(identity_raw, Mapping)
                    identity = FileIdentity(
                        int(identity_raw["volume"]), int(identity_raw["file_index"])
                    )
                    quarantine_name = (
                        f"{attempt['artifact_kind']}-{attempt['artifact_id']}-"
                        f"{str(attempt['receipt_sha256'])[:12]}"
                    )
                    destination = quarantine_root / quarantine_name
                    try:
                        atomic_move_pinned_directory(
                            source,
                            destination,
                            expected_identity=identity,
                        )
                    except (OSError, SecureFilesystemError) as exc:
                        raise JobFailure(
                            "JOB_REPAIR_UNSAFE",
                            "receipt-verified attempt changed before atomic quarantine",
                        ) from exc
                    quarantined_attempts.append(
                        MappingProxyType(
                            {
                                "artifact_kind": attempt["artifact_kind"],
                                "artifact_id": attempt["artifact_id"],
                                "from_relative_path": attempt["relative_path"],
                                "quarantine_relative_path": destination.relative_to(
                                    locked
                                ).as_posix(),
                                "receipt_sha256": attempt["receipt_sha256"],
                            }
                        )
                    )
            root_entries = {entry.name for entry in evidence.root_entries}
            manifest_path = locked / "job.json"
            if "job.json" in root_entries:
                payload = evidence.manifest_raw
                if payload is None or evidence.manifest_bytes is None:
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "existing job.json cannot prove the Job identity",
                    )
                identity = {
                    "job_id": str(fresh.get("id")),
                    "workspace_id": str(fresh.get("workspace_id")),
                    "directory_name": str(fresh.get("directory_name")),
                }
                if any(str(payload.get(field)) != value for field, value in identity.items()):
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "job.json identity does not match the authoritative Job",
                    )
                existing = evidence.manifest
                if existing is None:
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "job.json cannot be validated for deterministic repair",
                    )
                authoritative = self._manifest_from_row(fresh)
                if (
                    existing.active_working_copy_id is not None
                    and existing.active_working_copy_id
                    != authoritative.active_working_copy_id
                ) or (
                    existing.source_snapshots
                    and tuple(existing.source_snapshots)
                    != tuple(authoritative.source_snapshots)
                ):
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "disk-only operational bindings cannot be repaired",
                    )
                if len(evidence.verified_snapshots) != len(
                    authoritative.source_snapshots
                ):
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "authoritative source snapshot bytes cannot be verified",
                    )
                if (
                    authoritative.active_working_copy_id is not None
                    and evidence.verified_active_working_copy_id
                    != authoritative.active_working_copy_id
                ):
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "authoritative active working-copy bytes cannot be verified",
                    )
                repaired = authoritative
                self._replace_projection(manifest_path, repaired)
                return self._repair_result(
                    repaired, reason, actor_id, quarantined_attempts
                )
            working_entries = evidence.layout_entries["models/working"]
            source_entries = evidence.layout_entries["inputs/source"]
            assert working_entries is not None and source_entries is not None
            authoritative = self._manifest_from_row(fresh)
            if (
                (working_entries or source_entries)
                and authoritative.active_working_copy_id is None
                and not authoritative.source_snapshots
            ):
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "missing job.json cannot be reconstructed around existing model bytes",
                )
            if len(evidence.verified_snapshots) != len(
                authoritative.source_snapshots
            ):
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "authoritative source snapshot bytes cannot be verified",
                )
            if (
                authoritative.active_working_copy_id is not None
                and evidence.verified_active_working_copy_id
                != authoritative.active_working_copy_id
            ):
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "authoritative active working-copy bytes cannot be verified",
                )
            repaired = authoritative
            try:
                atomic_publish_new(manifest_path, _manifest_bytes(repaired))
            except (OSError, SecureFilesystemError) as exc:
                raise JobFailure(
                    "JOB_PROJECTION_INCOMPLETE",
                    "PostgreSQL is authoritative but job.json publication is incomplete",
                ) from exc
            return self._repair_result(repaired, reason, actor_id, quarantined_attempts)

    def _repair_result(
        self,
        manifest: DesignJobManifest,
        reason: str,
        actor_id: str,
        quarantined_attempts: Sequence[Mapping[str, object]] = (),
    ) -> DesignJobRepairResult:
        return DesignJobRepairResult(
            manifest=manifest,
            audit=MappingProxyType(
                {
                    "action": "repair",
                    "reason": reason.strip(),
                    "actor_id": actor_id,
                    "authoritative_revision": manifest.revision,
                    "quarantined_attempts": tuple(
                        MappingProxyType(dict(attempt))
                        for attempt in quarantined_attempts
                    ),
                }
            ),
        )

__all__ = [
    "DesignJobManager",
    "DesignJobManifest",
    "DesignJobRepairResult",
    "JOB_MANIFEST_SCHEMA",
    "JOB_REPAIR_SCHEMA",
    "JobFailure",
    "locked_job_root",
    "managed_job_path",
    "sanitize_job_slug",
]
