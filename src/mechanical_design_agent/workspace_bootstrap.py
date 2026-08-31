from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterator

from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_new,
    atomic_replace,
    ensure_managed_directory,
    exclusive_creation_lock,
    validate_managed_path,
    relative_managed_path,
)


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

MANIFEST_RELATIVE_PATH = Path("config/mechanical_design.json")
DEFAULT_ARTIFACT_ROOT = "data/artifacts"
DEFAULT_STANDARD_PART_SOURCES = "config/standard_parts_sources.json"
DEFAULT_PRODUCT_FAMILIES = "config/product_families"

STANDARD_PART_SOURCES_TEMPLATE: dict[str, object] = {
    "schema_version": "StandardPartSources/v1",
    "verified_local_catalog": {
        "enabled": False,
        "global_root": None,
    },
}


class BootstrapFailure(ValueError):
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
            "schema_version": "MechanicalDesignBootstrapError/v1",
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class EnvEntry:
    value: str
    line: int


@dataclass(frozen=True)
class ParsedEnvFile:
    path: Path
    values: Mapping[str, EnvEntry]


@dataclass(frozen=True)
class SettingSource:
    kind: str
    location: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ResolvedSetting:
    value: str
    source: SettingSource


@dataclass(frozen=True)
class WorkspaceSelection:
    path: Path
    source: SettingSource


@dataclass(frozen=True)
class WorkspaceManifest:
    workspace: Path
    workspace_id: uuid.UUID
    actor_id: str
    artifact_root: Path
    standard_parts_sources: Path
    product_families: Path
    default_product_family_id: str | None
    freecad_command: str | None
    freecad_sha256: str | None
    raw: Mapping[str, object]


@dataclass(frozen=True)
class InitResult:
    status: str
    result: str
    workspace: Path
    manifest_path: Path
    created: tuple[str, ...]
    reused: tuple[str, ...]
    next_steps: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "MechanicalDesignWorkspaceInit/v1",
            "status": self.status,
            "result": self.result,
            "workspace": str(self.workspace),
            "manifest_path": str(self.manifest_path),
            "created": list(self.created),
            "reused": list(self.reused),
            "next_steps": list(self.next_steps),
        }


def _parse_env_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise BootstrapFailure(
                "ENV_FILE_SYNTAX",
                f"unmatched quote at line {line_number}",
            )
        return value[1:-1]
    if value[-1] in {"'", '"'}:
        raise BootstrapFailure(
            "ENV_FILE_SYNTAX",
            f"unmatched quote at line {line_number}",
        )
    return value


def parse_selected_env_file(
    runtime_path: str | None,
    environ: Mapping[str, str],
    cwd: Path,
) -> ParsedEnvFile | None:
    selected = runtime_path
    if selected is None:
        selected = environ.get("MECH_DESIGN_ENV_FILE", "").strip()
        if not selected:
            return None
    elif not selected.strip():
        raise BootstrapFailure(
            "ENV_FILE_ARGUMENT",
            "--env-file must not be blank",
        )

    path = Path(selected).expanduser()
    if not path.is_absolute():
        path = cwd / path
    path = path.resolve()
    if not path.is_file():
        raise BootstrapFailure(
            "ENV_FILE_UNREADABLE",
            f"env file is not a readable regular file: {path}",
        )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise BootstrapFailure(
            "ENV_FILE_ENCODING",
            f"env file is not valid UTF-8: {path}",
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "ENV_FILE_UNREADABLE",
            f"cannot read env file: {path}",
        ) from exc

    values: dict[str, EnvEntry] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BootstrapFailure(
                "ENV_FILE_SYNTAX",
                f"invalid assignment at line {line_number}",
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise BootstrapFailure(
                "ENV_FILE_SYNTAX",
                f"invalid key at line {line_number}",
            )
        if key in values:
            raise BootstrapFailure(
                "ENV_FILE_DUPLICATE_KEY",
                f"duplicate key {key} at line {line_number}",
            )
        values[key] = EnvEntry(
            value=_parse_env_value(raw_value.strip(), line_number),
            line=line_number,
        )

    return ParsedEnvFile(
        path=path,
        values=MappingProxyType(values),
    )


def resolve_setting(
    *,
    environment_key: str,
    runtime_value: object | None,
    environ: Mapping[str, str],
    env_file: ParsedEnvFile | None,
    manifest_value: object | None,
    package_default: object,
) -> ResolvedSetting:
    if runtime_value is not None:
        return ResolvedSetting(
            value=str(runtime_value),
            source=SettingSource(kind="runtime"),
        )
    if environment_key in environ:
        return ResolvedSetting(
            value=environ[environment_key],
            source=SettingSource(kind="process_environment"),
        )
    if env_file is not None and environment_key in env_file.values:
        entry = env_file.values[environment_key]
        return ResolvedSetting(
            value=entry.value,
            source=SettingSource(
                kind="env_file",
                location=str(env_file.path),
                line=entry.line,
            ),
        )
    if manifest_value is not None:
        return ResolvedSetting(
            value=str(manifest_value),
            source=SettingSource(kind="manifest"),
        )
    return ResolvedSetting(
        value=str(package_default),
        source=SettingSource(kind="package_default"),
    )


def select_workspace(
    *,
    runtime_workspace: str | Path | None,
    environ: Mapping[str, str],
    env_file: ParsedEnvFile | None,
    cwd: Path,
    require_manifest: bool,
) -> WorkspaceSelection:
    selected: str | Path | None = runtime_workspace
    source = SettingSource(kind="runtime")
    if selected is None and environ.get("MECH_DESIGN_WORKSPACE", "").strip():
        selected = environ["MECH_DESIGN_WORKSPACE"]
        source = SettingSource(kind="process_environment")
    if selected is None and env_file is not None:
        env_entry = env_file.values.get("MECH_DESIGN_WORKSPACE")
        if env_entry is not None and env_entry.value.strip():
            selected = env_entry.value
            source = SettingSource(
                kind="env_file",
                location=str(env_file.path),
                line=env_entry.line,
            )

    if selected is not None:
        if not str(selected).strip():
            raise BootstrapFailure(
                "WORKSPACE_ARGUMENT",
                "workspace must not be blank",
            )
        requested = Path(selected).expanduser()
        if not requested.is_absolute():
            requested = cwd / requested
        if requested.is_symlink():
            raise BootstrapFailure(
                "WORKSPACE_SYMLINK",
                f"workspace must not be a symlink: {requested}",
            )
        try:
            workspace = validate_managed_path(
                Path(os.path.abspath(requested)),
                allow_missing_leaf=True,
            ).path
        except SecureFilesystemError as exc:
            raise BootstrapFailure(
                exc.code if exc.code.startswith("WINDOWS_") else "WORKSPACE_SYMLINK",
                str(exc),
            ) from exc
        manifest_path = workspace / MANIFEST_RELATIVE_PATH
        if require_manifest and not manifest_path.is_file():
            raise BootstrapFailure(
                "WORKSPACE_NOT_INITIALIZED",
                f"workspace manifest does not exist: {manifest_path}",
                status="setup_required",
            )
        return WorkspaceSelection(path=workspace, source=source)

    absolute_cwd = Path(os.path.abspath(cwd))
    for candidate in (absolute_cwd, *absolute_cwd.parents):
        if (candidate / MANIFEST_RELATIVE_PATH).is_file():
            try:
                canonical = validate_managed_path(
                    candidate, allow_missing_leaf=False
                ).path
            except SecureFilesystemError as exc:
                raise BootstrapFailure(
                    exc.code
                    if exc.code.startswith("WINDOWS_")
                    else "WORKSPACE_SYMLINK",
                    str(exc),
                ) from exc
            return WorkspaceSelection(
                path=canonical,
                source=SettingSource(kind="nearest_parent"),
            )

    raise BootstrapFailure(
        "WORKSPACE_NOT_SELECTED",
        "pass --workspace or set MECH_DESIGN_WORKSPACE",
        status="setup_required",
    )


def _validate_actor_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ACTOR_ID.fullmatch(value):
        raise BootstrapFailure(
            "ACTOR_ID_INVALID",
            "actor ID must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}",
        )
    return value


def validate_actor_id(value: object) -> str:
    """Validate one runtime or persisted actor identifier."""
    return _validate_actor_id(value)


def _workspace_owned_path(
    workspace: Path,
    value: object,
    label: str,
) -> Path:
    if not isinstance(value, str) or not value:
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            f"{label} must be a nonempty path string",
        )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        candidate = validate_managed_path(
            Path(os.path.abspath(candidate)),
            allow_missing_leaf=True,
        ).path
        relative = relative_managed_path(
            candidate,
            workspace,
            allow_missing_leaf=True,
        )
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "MANIFEST_PATH_ESCAPE",
            str(exc),
        ) from exc
    except ValueError:
        raise BootstrapFailure(
            "MANIFEST_PATH_ESCAPE",
            f"{label} escapes workspace: {candidate}",
        ) from None
    return workspace / relative


def _optional_string(
    value: object,
    label: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            f"{label} must be a string or null",
        )
    return value


def read_workspace_manifest(workspace: Path) -> WorkspaceManifest:
    requested = workspace.expanduser()
    if requested.is_symlink():
        raise BootstrapFailure(
            "WORKSPACE_SYMLINK",
            f"workspace must not be a symlink: {requested}",
        )
    try:
        managed_workspace = validate_managed_path(
            Path(os.path.abspath(requested)),
            allow_missing_leaf=True,
        )
    except (OSError, SecureFilesystemError) as exc:
        if isinstance(exc, SecureFilesystemError) and exc.code.startswith("WINDOWS_"):
            raise BootstrapFailure(exc.code, str(exc)) from exc
        raise BootstrapFailure(
            "WORKSPACE_NOT_INITIALIZED",
            f"workspace does not exist: {requested}",
            status="setup_required",
        ) from exc
    if managed_workspace.identity is None:
        raise BootstrapFailure(
            "WORKSPACE_NOT_INITIALIZED",
            f"workspace does not exist: {requested}",
            status="setup_required",
        )
    canonical_workspace = managed_workspace.path
    if not canonical_workspace.is_dir():
        raise BootstrapFailure(
            "WORKSPACE_NOT_INITIALIZED",
            f"workspace is not a directory: {canonical_workspace}",
            status="setup_required",
        )

    manifest_path = canonical_workspace / MANIFEST_RELATIVE_PATH
    if manifest_path.is_symlink():
        raise BootstrapFailure(
            "MANIFEST_SYMLINK",
            f"workspace manifest must not be a symlink: {manifest_path}",
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            f"cannot read workspace manifest: {manifest_path}",
        ) from exc
    if not isinstance(raw, dict):
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            "workspace manifest must be a JSON object",
        )
    if raw.get("schema_version") != "MechanicalDesignWorkspace/v1":
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            "unsupported workspace manifest schema",
        )
    try:
        workspace_id = uuid.UUID(str(raw.get("workspace_id", "")))
    except ValueError as exc:
        raise BootstrapFailure(
            "MANIFEST_INVALID",
            "workspace_id must be a UUID",
        ) from exc

    identity = raw.get("identity")
    paths = raw.get("paths")
    freecad = raw.get("freecad")
    if not isinstance(identity, dict):
        raise BootstrapFailure("MANIFEST_INVALID", "identity must be an object")
    if not isinstance(paths, dict):
        raise BootstrapFailure("MANIFEST_INVALID", "paths must be an object")
    if not isinstance(freecad, dict):
        raise BootstrapFailure("MANIFEST_INVALID", "freecad must be an object")
    for identity_key in ("organization_id", "design_group_id"):
        _optional_string(identity.get(identity_key), f"identity.{identity_key}")

    return WorkspaceManifest(
        workspace=canonical_workspace,
        workspace_id=workspace_id,
        actor_id=_validate_actor_id(identity.get("actor_id")),
        artifact_root=_workspace_owned_path(
            canonical_workspace,
            paths.get("artifact_root"),
            "artifact_root",
        ),
        standard_parts_sources=_workspace_owned_path(
            canonical_workspace,
            paths.get("standard_parts_sources"),
            "standard_parts_sources",
        ),
        product_families=_workspace_owned_path(
            canonical_workspace,
            paths.get("product_families"),
            "product_families",
        ),
        default_product_family_id=_optional_string(
            raw.get("default_product_family_id"),
            "default_product_family_id",
        ),
        freecad_command=_optional_string(
            freecad.get("command"),
            "freecad.command",
        ),
        freecad_sha256=_optional_string(
            freecad.get("sha256"),
            "freecad.sha256",
        ),
        raw=MappingProxyType(raw),
    )


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _manifest_template(
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    organization_id: str | None,
    design_group_id: str | None,
) -> dict[str, object]:
    return {
        "schema_version": "MechanicalDesignWorkspace/v1",
        "workspace_id": str(workspace_id),
        "identity": {
            "actor_id": actor_id,
            "organization_id": organization_id,
            "design_group_id": design_group_id,
        },
        "paths": {
            "artifact_root": DEFAULT_ARTIFACT_ROOT,
            "standard_parts_sources": DEFAULT_STANDARD_PART_SOURCES,
            "product_families": DEFAULT_PRODUCT_FAMILIES,
        },
        "default_product_family_id": None,
        "freecad": {"command": None, "sha256": None},
    }


def _ensure_managed_directory(
    workspace: Path,
    relative: str,
) -> Path:
    current = workspace
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise BootstrapFailure(
                "MANAGED_PATH_SYMLINK",
                f"managed path must not be a symlink: {current}",
            )
        try:
            current = ensure_managed_directory(
                current,
                parents=False,
                exist_ok=True,
            ).path
        except SecureFilesystemError as exc:
            raise BootstrapFailure(
                exc.code if exc.code.startswith("WINDOWS_") else "MANAGED_PATH_INVALID",
                str(exc),
            ) from exc
        except OSError as exc:
            raise BootstrapFailure(
                "MANAGED_PATH_INVALID",
                f"cannot create managed directory: {current}",
            ) from exc
        if not current.is_dir():
            raise BootstrapFailure(
                "MANAGED_PATH_INVALID",
                f"managed path is not a directory: {current}",
            )
        if not current.resolve().is_relative_to(workspace):
            raise BootstrapFailure(
                "MANAGED_PATH_INVALID",
                f"managed directory escapes workspace: {current}",
            )
    return current


@contextmanager
def _initialization_lock(workspace: Path) -> Iterator[None]:
    lock_path = workspace / ".mechanical-design-init.lock"
    try:
        with exclusive_creation_lock(lock_path):
            yield
    except FileExistsError as exc:
        raise BootstrapFailure(
            "INIT_LOCKED",
            f"workspace initialization is already running: {workspace}",
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "ATOMIC_WRITE_FAILED",
            f"cannot acquire workspace initialization lock: {workspace}",
        ) from exc
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "ATOMIC_WRITE_FAILED",
            str(exc),
        ) from exc


def _publish_new_file(path: Path, content: bytes) -> None:
    try:
        atomic_publish_new(path, content)
    except FileExistsError as exc:
        raise BootstrapFailure(
            "MANAGED_FILE_CONFLICT",
            f"refusing to overwrite managed file: {path}",
        ) from exc
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "ATOMIC_WRITE_FAILED",
            str(exc),
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "ATOMIC_WRITE_FAILED",
            f"cannot atomically publish managed file: {path}",
        ) from exc


def atomic_replace_managed_json(
    path: Path,
    value: Mapping[str, object],
) -> None:
    """Atomically replace one existing managed JSON file without following links."""
    if path.is_symlink() or not path.is_file():
        raise BootstrapFailure(
            "MANAGED_FILE_CONFLICT",
            f"managed file target is unsafe: {path}",
        )
    content = _canonical_json_bytes(value)
    try:
        atomic_replace(path, content)
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "MANAGED_FILE_CONFLICT",
            str(exc),
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "ATOMIC_WRITE_FAILED",
            f"cannot atomically replace managed file: {path}",
        ) from exc


def _reuse_exact_or_publish(path: Path, expected: bytes) -> bool:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise BootstrapFailure(
                "MANAGED_FILE_CONFLICT",
                f"managed file path is unsafe: {path}",
            )
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BootstrapFailure(
                "MANAGED_FILE_CONFLICT",
                f"cannot verify managed file: {path}",
            ) from exc
        if actual != expected:
            raise BootstrapFailure(
                "MANAGED_FILE_CONFLICT",
                f"managed file conflicts with safe template: {path}",
            )
        return True
    _publish_new_file(path, expected)
    return False


def _validate_initialized_managed_state(manifest: WorkspaceManifest) -> None:
    try:
        validate_managed_path(manifest.workspace, allow_missing_leaf=False)
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "MANAGED_CONFIG_INVALID",
            str(exc),
        ) from exc
    for label, path in (
        ("product_families", manifest.product_families),
        ("artifact_root", manifest.artifact_root),
    ):
        try:
            managed = validate_managed_path(path, allow_missing_leaf=True)
        except SecureFilesystemError as exc:
            raise BootstrapFailure(
                exc.code if exc.code.startswith("WINDOWS_") else "MANAGED_CONFIG_INVALID",
                str(exc),
            ) from exc
        if managed.identity is None:
            raise BootstrapFailure(
                "MANAGED_CONFIG_INVALID",
                f"{label} must be a real directory: {path}",
            )
        path = managed.path
        if path.is_symlink() or not path.is_dir():
            raise BootstrapFailure(
                "MANAGED_CONFIG_INVALID",
                f"{label} must be a real directory: {path}",
            )
    sources_path = manifest.standard_parts_sources
    try:
        managed_sources = validate_managed_path(
            sources_path,
            allow_missing_leaf=True,
        )
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "MANAGED_CONFIG_INVALID",
            str(exc),
        ) from exc
    if managed_sources.identity is None:
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            f"standard-part sources must be a real file: {sources_path}",
        )
    sources_path = managed_sources.path
    if sources_path.is_symlink() or not sources_path.is_file():
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            f"standard-part sources must be a real file: {sources_path}",
        )
    try:
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            f"cannot read standard-part sources: {sources_path}",
        ) from exc
    if not isinstance(sources, dict):
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            "standard-part sources must be a JSON object",
        )
    if sources.get("schema_version") != "StandardPartSources/v1":
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            "unsupported standard-part sources schema",
        )
    local_catalog = sources.get("verified_local_catalog")
    if not isinstance(local_catalog, dict):
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            "verified_local_catalog must be an object",
        )
    if not isinstance(local_catalog.get("enabled"), bool):
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            "verified_local_catalog.enabled must be boolean",
        )
    catalog_root = local_catalog.get("global_root")
    if catalog_root is not None and not isinstance(catalog_root, str):
        raise BootstrapFailure(
            "MANAGED_CONFIG_INVALID",
            "verified_local_catalog.global_root must be string or null",
        )


def validate_workspace_managed_state(manifest: WorkspaceManifest) -> None:
    """Validate files and directories owned by an initialized workspace."""
    _validate_initialized_managed_state(manifest)


def _relative_names(paths: list[Path], workspace: Path) -> tuple[str, ...]:
    return tuple(path.relative_to(workspace).as_posix() for path in paths)


def initialize_workspace(
    *,
    workspace: Path,
    actor_id: str | None,
    dry_run: bool,
    organization_id: str | None = None,
    design_group_id: str | None = None,
) -> InitResult:
    if (organization_id is None) != (design_group_id is None):
        raise BootstrapFailure(
            "WORKSPACE_IDENTITY_INCOMPLETE",
            "organization_id and design_group_id must be provided together",
        )
    normalized_organization = (
        organization_id.strip() if isinstance(organization_id, str) else None
    )
    normalized_group = design_group_id.strip() if isinstance(design_group_id, str) else None
    if organization_id is not None and not normalized_organization:
        raise BootstrapFailure(
            "WORKSPACE_IDENTITY_INVALID",
            "organization_id must be a nonblank string",
        )
    if design_group_id is not None and not normalized_group:
        raise BootstrapFailure(
            "WORKSPACE_IDENTITY_INVALID",
            "design_group_id must be a nonblank string",
        )
    requested = workspace.expanduser()
    if requested.is_symlink():
        raise BootstrapFailure(
            "WORKSPACE_SYMLINK",
            f"workspace must not be a symlink: {requested}",
        )
    if requested.exists() and not requested.is_dir():
        raise BootstrapFailure(
            "WORKSPACE_INVALID",
            f"workspace must be a directory: {requested}",
        )
    try:
        canonical = validate_managed_path(
            Path(os.path.abspath(requested)), allow_missing_leaf=True
        ).path
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "WORKSPACE_INVALID",
            str(exc),
        ) from exc
    manifest_path = canonical / MANIFEST_RELATIVE_PATH

    if manifest_path.is_file() or manifest_path.is_symlink():
        manifest = read_workspace_manifest(canonical)
        if actor_id is not None and _validate_actor_id(actor_id) != manifest.actor_id:
            raise BootstrapFailure(
                "ACTOR_ID_CONFLICT",
                "explicit actor differs from initialized workspace default",
            )
        identity = manifest.raw.get("identity")
        assert isinstance(identity, Mapping)
        for label, requested_identity in (
            ("organization_id", normalized_organization),
            ("design_group_id", normalized_group),
        ):
            if requested_identity is not None and identity.get(label) != requested_identity:
                raise BootstrapFailure(
                    "WORKSPACE_IDENTITY_CONFLICT",
                    f"explicit {label} differs from initialized workspace identity",
                )
        _validate_initialized_managed_state(manifest)
        managed_names = (
            "config/mechanical_design.json",
            "config/standard_parts_sources.json",
            "config/product_families",
            "data/artifacts",
        )
        return InitResult(
            status="ok",
            result="already_initialized",
            workspace=canonical,
            manifest_path=manifest_path,
            created=(),
            reused=managed_names,
            next_steps=(),
        )

    validated_actor = _validate_actor_id(actor_id) if actor_id is not None else None
    planned_relative = (
        "config",
        "config/product_families",
        "config/standard_parts_sources.json",
        "data",
        "data/artifacts",
        "config/mechanical_design.json",
    )
    if dry_run:
        return InitResult(
            status="ok",
            result="dry_run",
            workspace=canonical,
            manifest_path=manifest_path,
            created=planned_relative,
            reused=(),
            next_steps=(
                f"mechanical-design init --workspace {canonical}",
            ),
        )

    try:
        canonical = ensure_managed_directory(
            canonical,
            parents=True,
            exist_ok=True,
        ).path
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "WORKSPACE_INVALID",
            str(exc),
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "WORKSPACE_INVALID",
            f"cannot create workspace: {canonical}",
        ) from exc

    managed_paths = [
        canonical / "config",
        canonical / "config/product_families",
        canonical / "config/standard_parts_sources.json",
        canonical / "data",
        canonical / "data/artifacts",
        manifest_path,
    ]
    existed_before = {path for path in managed_paths if path.exists()}
    with _initialization_lock(canonical):
        config = _ensure_managed_directory(canonical, "config")
        _ensure_managed_directory(canonical, "config/product_families")
        _ensure_managed_directory(canonical, "data/artifacts")
        sources_path = config / "standard_parts_sources.json"
        _reuse_exact_or_publish(
            sources_path,
            _canonical_json_bytes(STANDARD_PART_SOURCES_TEMPLATE),
        )
        workspace_id = uuid.uuid4()
        persisted_actor = validated_actor or f"actor-{uuid.uuid4()}"
        manifest_bytes = _canonical_json_bytes(
            _manifest_template(
                workspace_id=workspace_id,
                actor_id=persisted_actor,
                organization_id=normalized_organization,
                design_group_id=normalized_group,
            )
        )
        _publish_new_file(manifest_path, manifest_bytes)

    created = [path for path in managed_paths if path not in existed_before]
    reused = [path for path in managed_paths if path in existed_before]
    return InitResult(
        status="ok",
        result="initialized",
        workspace=canonical,
        manifest_path=manifest_path,
        created=_relative_names(created, canonical),
        reused=_relative_names(reused, canonical),
        next_steps=(
            "configure database credentials before database operations",
        ),
    )
