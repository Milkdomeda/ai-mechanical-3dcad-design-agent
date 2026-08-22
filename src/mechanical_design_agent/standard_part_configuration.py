from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .package_resources import standard_part_provider_config
from .secure_fs import SecureFilesystemError, validate_external_read_path
from .workspace_bootstrap import (
    STANDARD_PART_SOURCES_TEMPLATE,
    BootstrapFailure,
    WorkspaceManifest,
    atomic_replace_managed_json,
)


PROVIDER_SCHEMA = "StandardPartProviders/v1"
SOURCE_SCHEMA = "StandardPartSources/v1"
SourceSchemaKind = Literal["v1", "legacy"]
_PROVIDER_REQUIRED_KEYS = {
    "id",
    "name",
    "priority",
    "trust_tier",
    "acquisition",
    "login_required",
    "categories",
}
_V1_TOP_KEYS = {"schema_version", "verified_local_catalog"}
_V1_LOCAL_KEYS = {"enabled", "global_root"}
_LEGACY_TOP_KEYS = {"verified_local_catalog"}
_LEGACY_LOCAL_KEYS = {"global_root"}


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: dict[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {key: _freeze_value(item) for key, item in value.items()}
    )


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class StandardPartProviderCatalog:
    providers: tuple[Mapping[str, object], ...]

    def as_dict(self, category: str = "") -> dict[str, object]:
        providers = self.providers
        if category:
            providers = tuple(
                provider
                for provider in providers
                if "all" in provider["categories"]
                or category in provider["categories"]
            )
        return {
            "schema_version": PROVIDER_SCHEMA,
            "category": category or None,
            "providers": [_json_value(provider) for provider in providers],
        }


@dataclass(frozen=True)
class StandardPartSources:
    path: Path
    schema_kind: SourceSchemaKind
    enabled: bool
    configured_root: str | None
    effective_root: Path | None
    status: Literal["ok", "warning"]
    code: str
    message: str

    def catalog_dict(self) -> dict[str, object]:
        return {
            "schema_kind": self.schema_kind,
            "enabled": self.enabled,
            "configured_root": self.configured_root,
            "effective_root": (
                str(self.effective_root) if self.effective_root is not None else None
            ),
        }


def _invalid_provider(message: str) -> BootstrapFailure:
    return BootstrapFailure("STANDARD_PART_PROVIDER_CONFIG_INVALID", message)


def _parse_standard_part_provider_catalog(
    value: object,
) -> StandardPartProviderCatalog:
    if not isinstance(value, dict) or value.get("schema_version") != PROVIDER_SCHEMA:
        raise _invalid_provider("invalid standard-part provider schema")
    raw = value.get("providers")
    if not isinstance(raw, list):
        raise _invalid_provider("standard-part providers must be a list")

    provider_ids: set[str] = set()
    priorities: set[int] = set()
    parsed: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or not _PROVIDER_REQUIRED_KEYS <= item.keys():
            raise _invalid_provider("standard-part provider entry is incomplete")
        for key in ("id", "name", "trust_tier", "acquisition"):
            field = item[key]
            if not isinstance(field, str) or not field.strip():
                raise _invalid_provider(
                    f"standard-part provider {key} must be a nonblank string"
                )
        priority = item["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise _invalid_provider(
                "standard-part provider priority must be an integer"
            )
        categories = item["categories"]
        if (
            not isinstance(categories, list)
            or not categories
            or any(
                not isinstance(category, str) or not category.strip()
                for category in categories
            )
        ):
            raise _invalid_provider(
                "standard-part provider categories must be nonblank strings"
            )
        if not isinstance(item["login_required"], bool):
            raise _invalid_provider(
                "standard-part provider login_required must be boolean"
            )

        provider_id = item["id"]
        assert isinstance(provider_id, str)
        if provider_id in provider_ids or priority in priorities:
            raise _invalid_provider(
                "standard-part provider IDs and priorities must be unique"
            )
        provider_ids.add(provider_id)
        priorities.add(priority)
        parsed.append(dict(item))

    parsed.sort(key=lambda item: int(item["priority"]))
    return StandardPartProviderCatalog(
        tuple(_freeze_mapping(provider) for provider in parsed)
    )


def load_standard_part_provider_catalog() -> StandardPartProviderCatalog:
    try:
        with standard_part_provider_config() as path:
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_provider(
            f"standard-part provider resource is invalid: {type(exc).__name__}"
        ) from exc
    return _parse_standard_part_provider_catalog(value)


def _invalid_sources(message: str) -> BootstrapFailure:
    return BootstrapFailure("STANDARD_PART_SOURCES_INVALID", message)


def _is_under(candidate: Path, parent: Path) -> bool:
    return candidate == parent or candidate.is_relative_to(parent)


def _resolve_catalog_root(
    manifest: WorkspaceManifest,
    root_value: str,
    *,
    legacy: bool,
) -> Path:
    requested = Path(root_value).expanduser()
    if legacy and not requested.is_absolute():
        raise _invalid_sources(
            "legacy standard-part catalog root must be absolute"
        )
    candidate = requested if requested.is_absolute() else manifest.workspace / requested
    try:
        canonical = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BootstrapFailure(
            "STANDARD_PART_CATALOG_ROOT_NOT_FOUND",
            "standard-part catalog root does not exist",
        ) from exc
    except OSError as exc:
        raise _invalid_sources(
            f"standard-part catalog root cannot be resolved: {type(exc).__name__}"
        ) from exc
    if not canonical.is_dir():
        raise BootstrapFailure(
            "STANDARD_PART_CATALOG_ROOT_NOT_DIRECTORY",
            "standard-part catalog root is not a directory",
        )

    workspace = manifest.workspace.resolve()
    protected = (
        workspace / "config",
        workspace / "output",
        workspace / "knowledge",
        manifest.product_families.resolve(),
        manifest.artifact_root.resolve(),
    )
    if canonical == workspace or any(
        _is_under(canonical, item.resolve()) for item in protected
    ):
        raise BootstrapFailure(
            "STANDARD_PART_CATALOG_ROOT_UNSAFE",
            "standard-part catalog root overlaps a protected workspace tree",
        )
    return canonical


def _parse_v1_sources(
    *,
    manifest: WorkspaceManifest,
    path: Path,
    value: dict[str, object],
) -> StandardPartSources:
    if set(value) != _V1_TOP_KEYS or value.get("schema_version") != SOURCE_SCHEMA:
        raise _invalid_sources("invalid standard-part source schema")
    local = value.get("verified_local_catalog")
    if not isinstance(local, dict) or set(local) != _V1_LOCAL_KEYS:
        raise _invalid_sources("invalid verified-local catalog configuration")
    enabled = local.get("enabled")
    root_value = local.get("global_root")
    if not isinstance(enabled, bool):
        raise _invalid_sources("verified-local catalog enabled must be boolean")
    if not enabled:
        if root_value is not None:
            raise _invalid_sources("disabled catalog requires a null global_root")
        return StandardPartSources(
            path=path,
            schema_kind="v1",
            enabled=False,
            configured_root=None,
            effective_root=None,
            status="warning",
            code="STANDARD_PART_CATALOG_DISABLED",
            message="standard-part catalog is disabled",
        )
    if not isinstance(root_value, str) or not root_value.strip():
        raise _invalid_sources("enabled catalog requires a nonblank global_root")
    effective_root = _resolve_catalog_root(
        manifest,
        root_value,
        legacy=False,
    )
    return StandardPartSources(
        path=path,
        schema_kind="v1",
        enabled=True,
        configured_root=root_value,
        effective_root=effective_root,
        status="ok",
        code="STANDARD_PART_CATALOG_READY",
        message="standard-part catalog is configured",
    )


def _parse_legacy_sources(
    *,
    manifest: WorkspaceManifest,
    path: Path,
    value: dict[str, object],
) -> StandardPartSources:
    if set(value) != _LEGACY_TOP_KEYS:
        raise _invalid_sources("invalid legacy standard-part source schema")
    local = value.get("verified_local_catalog")
    if not isinstance(local, dict) or set(local) != _LEGACY_LOCAL_KEYS:
        raise _invalid_sources("invalid legacy verified-local catalog configuration")
    root_value = local.get("global_root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise _invalid_sources("legacy catalog requires a nonblank global_root")
    effective_root = _resolve_catalog_root(
        manifest,
        root_value,
        legacy=True,
    )
    return StandardPartSources(
        path=path,
        schema_kind="legacy",
        enabled=True,
        configured_root=root_value,
        effective_root=effective_root,
        status="warning",
        code="STANDARD_PART_SOURCES_LEGACY_FORMAT",
        message="standard-part source configuration uses the legacy format",
    )


def load_standard_part_sources(
    manifest: WorkspaceManifest,
) -> StandardPartSources:
    path = manifest.standard_parts_sources
    if path.is_symlink() or not path.is_file():
        raise _invalid_sources(
            "standard-part source configuration must be a regular managed file"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_sources(
            f"standard-part source configuration is invalid: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise _invalid_sources("standard-part source configuration must be an object")
    if "schema_version" in value:
        return _parse_v1_sources(manifest=manifest, path=path, value=value)
    return _parse_legacy_sources(manifest=manifest, path=path, value=value)


def probe_standard_part_catalog(root: Path) -> None:
    try:
        root = validate_external_read_path(root)
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code
            if exc.code.startswith("WINDOWS_")
            else "STANDARD_PART_CATALOG_ROOT_NOT_WRITABLE",
            str(exc),
        ) from exc
    temporary: Path | None = None
    descriptor: int | None = None
    probe_error: OSError | None = None
    cleanup_error: OSError | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".mechanical-design-catalog-probe.",
            dir=root,
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(b"probe\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        probe_error = exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                probe_error = probe_error or exc
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = exc
    if cleanup_error is not None:
        raise BootstrapFailure(
            "STANDARD_PART_CATALOG_PROBE_CLEANUP_FAILED",
            "standard-part catalog write probe cleanup failed",
        ) from cleanup_error
    if probe_error is not None:
        raise BootstrapFailure(
            "STANDARD_PART_CATALOG_ROOT_NOT_WRITABLE",
            "standard-part catalog write probe failed",
        ) from probe_error


def _configuration_result(
    *,
    operation: str,
    status: str,
    code: str,
    message: str,
    changed: bool,
    sources: StandardPartSources | None,
    next_actions: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "schema_version": "StandardPartConfigurationResult/v1",
        "operation": operation,
        "status": status,
        "code": code,
        "message": message,
        "changed": changed,
        "catalog": sources.catalog_dict() if sources is not None else None,
        "next_actions": list(next_actions),
    }


def _stored_catalog_root(manifest: WorkspaceManifest, canonical: Path) -> str:
    if canonical.is_relative_to(manifest.workspace):
        return canonical.relative_to(manifest.workspace).as_posix()
    return str(canonical)


def _publish_sources(
    *,
    manifest: WorkspaceManifest,
    value: Mapping[str, object],
) -> StandardPartSources:
    try:
        atomic_replace_managed_json(manifest.standard_parts_sources, value)
        return load_standard_part_sources(manifest)
    except BootstrapFailure as exc:
        raise BootstrapFailure(
            "STANDARD_PART_CONFIG_WRITE_FAILED",
            "standard-part source configuration could not be atomically published",
        ) from exc


def enable_standard_part_catalog(
    *,
    manifest: WorkspaceManifest,
    root_path: str | Path,
) -> dict[str, object]:
    current = load_standard_part_sources(manifest)
    requested = str(root_path)
    canonical = _resolve_catalog_root(
        manifest,
        requested,
        legacy=False,
    )
    probe_standard_part_catalog(canonical)
    if (
        current.schema_kind == "v1"
        and current.enabled
        and current.effective_root == canonical
    ):
        return _configuration_result(
            operation="catalog_enable",
            status="ok",
            code="STANDARD_PART_CATALOG_ALREADY_CONFIGURED",
            message="standard-part catalog binding is already configured",
            changed=False,
            sources=current,
        )

    value: dict[str, object] = {
        "schema_version": SOURCE_SCHEMA,
        "verified_local_catalog": {
            "enabled": True,
            "global_root": _stored_catalog_root(manifest, canonical),
        },
    }
    published = _publish_sources(manifest=manifest, value=value)
    if (
        published.schema_kind != "v1"
        or not published.enabled
        or published.effective_root != canonical
    ):
        raise BootstrapFailure(
            "STANDARD_PART_CONFIG_WRITE_FAILED",
            "published standard-part catalog binding failed verification",
        )
    return _configuration_result(
        operation="catalog_enable",
        status="ok",
        code="STANDARD_PART_CATALOG_CONFIGURED",
        message="standard-part catalog binding configured",
        changed=True,
        sources=published,
    )


def disable_standard_part_catalog(
    *,
    manifest: WorkspaceManifest,
) -> dict[str, object]:
    current = load_standard_part_sources(manifest)
    if current.schema_kind == "v1" and not current.enabled:
        return _configuration_result(
            operation="catalog_disable",
            status="ok",
            code="STANDARD_PART_CATALOG_ALREADY_DISABLED",
            message="standard-part catalog binding is already disabled",
            changed=False,
            sources=current,
        )
    published = _publish_sources(
        manifest=manifest,
        value=STANDARD_PART_SOURCES_TEMPLATE,
    )
    if published.schema_kind != "v1" or published.enabled:
        raise BootstrapFailure(
            "STANDARD_PART_CONFIG_WRITE_FAILED",
            "published disabled standard-part catalog binding failed verification",
        )
    return _configuration_result(
        operation="catalog_disable",
        status="ok",
        code="STANDARD_PART_CATALOG_DISABLED",
        message="standard-part catalog binding disabled",
        changed=True,
        sources=published,
    )
