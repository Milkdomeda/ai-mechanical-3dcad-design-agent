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
            "schema_version": "MechanicalDesignJobRepair/v1",
            "job": self.manifest.as_dict(),
            "audit": dict(self.audit),
        }

    def __getattr__(self, name: str) -> object:
        # Retain the manifest-shaped manager result for existing internal callers.
        return getattr(self.manifest, name)


class _JobRepository(Protocol):
    def create_design_job(self, **kwargs: object) -> dict[str, Any]: ...
    def record_design_job_directory(self, **kwargs: object) -> dict[str, Any]: ...
    def transition_design_job(self, **kwargs: object) -> dict[str, Any]: ...
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
            "active_working_copy_id": None,
            "source_snapshots": [],
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
        DesignJobManager._assert_manifest_matches_bindings(actual)
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
    def _assert_manifest_matches_bindings(actual: DesignJobManifest) -> None:
        if actual.active_working_copy_id is not None or actual.source_snapshots:
            raise JobFailure(
                "JOB_OPERATIONAL_BINDING_FORGED",
                "core Job manifest contains disk-only operational bindings",
            )

    @staticmethod
    def _assert_manifest_matches(
        actual: DesignJobManifest, expected: DesignJobManifest
    ) -> None:
        DesignJobManager._assert_manifest_matches_bindings(actual)
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
            "status": "ok" if not issues else "blocked",
            "issues": issues,
        }
        receipt_payload = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        report["receipt_sha256"] = hashlib.sha256(receipt_payload).hexdigest()
        return report

    def _directory_contract_issues(self, root: Path) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []
        for relative in _DIRECTORY_CONTRACT:
            try:
                candidate = managed_job_path(
                    job_root=root,
                    relative_path=relative,
                    allow_missing_leaf=False,
                )
                list_managed_directory(candidate)
            except (JobFailure, SecureFilesystemError) as exc:
                issues.append(
                    self._issue(
                        exc.code,
                        f"required managed directory is missing or unsafe: {relative}",
                    )
                )
        return issues

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
            )
        manifest_sha256: str | None = None
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
            issues.extend(self._directory_contract_issues(locked))
            try:
                raw, manifest_read = _read_json_with_evidence(locked / "job.json")
                manifest_sha256 = manifest_read.sha256
                actual = DesignJobManifest.from_dict(raw)
            except JobFailure as exc:
                issues.append(self._issue(exc.code, exc.message))
            else:
                expected = self._manifest_from_row(fresh)
                actual_payload = actual.as_dict()
                expected_payload = expected.as_dict()
                if actual.active_working_copy_id is not None or actual.source_snapshots:
                    issues.append(
                        self._issue(
                            "JOB_OPERATIONAL_BINDING_FORGED",
                            "core Job manifest contains disk-only operational bindings",
                        )
                    )
                if actual.revision != expected.revision:
                    issues.append(
                        self._issue(
                            "JOB_REVISION_MISMATCH",
                            "job.json revision disagrees with PostgreSQL",
                        )
                    )
                mismatched = [
                    field
                    for field in _AUTHORITATIVE_MANIFEST_FIELDS
                    if field != "revision"
                    and actual_payload[field] != expected_payload[field]
                ]
                if mismatched:
                    issues.append(
                        self._issue(
                            "JOB_MANIFEST_MISMATCH",
                            "job.json authoritative fields disagree with PostgreSQL",
                        )
                    )
        return self._doctor_report(
            row=fresh,
            issues=issues,
            manifest_sha256=manifest_sha256,
            verified_snapshots=(),
        )

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
            doctor = self._doctor_report_for_locked_row(locked=locked, row=fresh)
            if doctor["receipt_sha256"] != doctor_receipt_hash:
                raise JobFailure(
                    "JOB_DOCTOR_RECEIPT_MISMATCH",
                    "doctor receipt does not match the authorized Job state",
                )
            if self._directory_contract_issues(locked):
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "Job directory contract is incomplete or unsafe",
                )
            try:
                root_entries = {
                    entry.name: entry for entry in list_managed_directory(locked)
                }
            except SecureFilesystemError as exc:
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE", "Job root cannot be enumerated safely"
                ) from exc
            manifest_path = locked / "job.json"
            if "job.json" in root_entries:
                try:
                    payload = _read_json(manifest_path)
                except JobFailure as exc:
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "existing job.json cannot prove the Job identity",
                    ) from exc
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
                try:
                    existing = DesignJobManifest.from_dict(payload)
                except JobFailure as exc:
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "job.json cannot be validated for deterministic repair",
                    ) from exc
                if existing.active_working_copy_id is not None or existing.source_snapshots:
                    raise JobFailure(
                        "JOB_REPAIR_UNSAFE",
                        "disk-only operational bindings cannot be repaired",
                    )
                repaired = self._manifest_from_row(fresh)
                self._replace_projection(manifest_path, repaired)
                return self._repair_result(repaired, reason, actor_id)
            try:
                working_entries = list_managed_directory(locked / "models/working")
                source_entries = list_managed_directory(locked / "inputs/source")
            except SecureFilesystemError as exc:
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "model directories cannot be enumerated safely",
                ) from exc
            if working_entries or source_entries:
                raise JobFailure(
                    "JOB_REPAIR_UNSAFE",
                    "missing job.json cannot be reconstructed around existing model bytes",
                )
            repaired = self._manifest_from_row(fresh)
            try:
                atomic_publish_new(manifest_path, _manifest_bytes(repaired))
            except (OSError, SecureFilesystemError) as exc:
                raise JobFailure(
                    "JOB_PROJECTION_INCOMPLETE",
                    "PostgreSQL is authoritative but job.json publication is incomplete",
                ) from exc
            return self._repair_result(repaired, reason, actor_id)

    def _repair_result(
        self, manifest: DesignJobManifest, reason: str, actor_id: str
    ) -> DesignJobRepairResult:
        return DesignJobRepairResult(
            manifest=manifest,
            audit=MappingProxyType(
                {
                    "action": "repair",
                    "reason": reason.strip(),
                    "actor_id": actor_id,
                    "authoritative_revision": manifest.revision,
                }
            ),
        )

    def _doctor_report_for_locked_row(
        self, *, locked: Path, row: Mapping[str, object]
    ) -> dict[str, object]:
        """Compute a doctor receipt from pinned bytes while the Job lock is held."""
        issues = self._directory_contract_issues(locked)
        manifest_sha256: str | None = None
        try:
            raw, manifest_read = _read_json_with_evidence(locked / "job.json")
            manifest_sha256 = manifest_read.sha256
            actual = DesignJobManifest.from_dict(raw)
            expected = self._manifest_from_row(row)
            actual_payload = actual.as_dict()
            expected_payload = expected.as_dict()
            if actual.active_working_copy_id is not None or actual.source_snapshots:
                issues.append(self._issue("JOB_OPERATIONAL_BINDING_FORGED", "core Job manifest contains disk-only operational bindings"))
            if actual.revision != expected.revision:
                issues.append(self._issue("JOB_REVISION_MISMATCH", "job.json revision disagrees with PostgreSQL"))
            if any(
                field != "revision" and actual_payload[field] != expected_payload[field]
                for field in _AUTHORITATIVE_MANIFEST_FIELDS
            ):
                issues.append(self._issue("JOB_MANIFEST_MISMATCH", "job.json authoritative fields disagree with PostgreSQL"))
        except JobFailure as exc:
            issues.append(self._issue(exc.code, exc.message))
        return self._doctor_report(
            row=row,
            issues=issues,
            manifest_sha256=manifest_sha256,
            verified_snapshots=(),
        )


__all__ = [
    "DesignJobManager",
    "DesignJobManifest",
    "DesignJobRepairResult",
    "JOB_MANIFEST_SCHEMA",
    "JobFailure",
    "locked_job_root",
    "managed_job_path",
    "sanitize_job_slug",
]
