from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .secure_fs import (
    SecureFilesystemError,
    atomic_publish_new,
    validate_managed_path,
)

from .workspace_bootstrap import (
    MANIFEST_RELATIVE_PATH,
    BootstrapFailure,
    ParsedEnvFile,
    SettingSource,
    WorkspaceManifest,
    atomic_replace_managed_json,
)


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class ProductFamilyConfig:
    family_id: str
    path: Path
    value: Mapping[str, object]


@dataclass(frozen=True)
class ProductFamilyCatalog:
    state: str
    families: Mapping[str, ProductFamilyConfig]


@dataclass(frozen=True)
class ProductFamilySelection:
    family_id: str
    config: ProductFamilyConfig
    source: SettingSource


def _invalid(message: str) -> BootstrapFailure:
    return BootstrapFailure("PRODUCT_FAMILY_CONFIG_INVALID", message)


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise _invalid(
            f"{label} must be 1 to 128 safe ASCII identifier characters"
        )
    return value


def _name(value: object, label: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise _invalid(f"{label} must be a nonblank string of at most 256 characters")
    return value


def validate_product_family_config(
    value: object,
    *,
    path: Path,
    require_filename_match: bool = True,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _invalid(f"product-family config must be a JSON object: {path}")
    if value.get("schema_version") != "product-family-bootstrap/v1":
        raise _invalid(f"unsupported product-family schema: {path}")
    family_id = _safe_id(value.get("family_id"), "family_id")
    _safe_id(value.get("organization_id"), "organization_id")
    _safe_id(value.get("design_group_id"), "design_group_id")
    _name(value.get("organization_name"), "organization_name", required=False)
    _name(value.get("design_group_name"), "design_group_name")
    _name(value.get("family_name"), "family_name")
    if require_filename_match and path.name != f"{family_id}.json":
        raise _invalid(
            f"product-family filename must match family_id {family_id}: {path}"
        )
    if value.get("subfamily_mode") != "discover-and-confirm":
        raise _invalid("subfamily_mode must remain discover-and-confirm")
    question_limit = value.get("question_batch_limit")
    if isinstance(question_limit, bool) or not isinstance(question_limit, int):
        raise _invalid("question_batch_limit must be an integer between 1 and 5")
    if question_limit not in range(1, 6):
        raise _invalid("question_batch_limit must be between 1 and 5")
    return MappingProxyType(dict(value))


def load_product_family_catalog(directory: Path) -> ProductFamilyCatalog:
    try:
        validate_managed_path(directory, allow_missing_leaf=False)
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            "PRODUCT_FAMILY_CONFIG_INVALID",
            str(exc),
        ) from exc
    if directory.is_symlink() or not directory.is_dir():
        raise _invalid(f"product-family directory must be a real directory: {directory}")
    families: dict[str, ProductFamilyConfig] = {}
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise _invalid(f"cannot inspect product-family directory: {directory}") from exc
    for path in entries:
        if path.name.startswith("."):
            continue
        try:
            validate_managed_path(path, allow_missing_leaf=False)
        except SecureFilesystemError as exc:
            raise BootstrapFailure(
                "PRODUCT_FAMILY_CONFIG_INVALID",
                str(exc),
            ) from exc
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise _invalid(f"unsupported product-family directory entry: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid(f"cannot read product-family config: {path}") from exc
        value = validate_product_family_config(raw, path=path)
        family_id = str(value["family_id"])
        if family_id in families:
            raise _invalid(f"duplicate product-family ID: {family_id}")
        families[family_id] = ProductFamilyConfig(
            family_id=family_id,
            path=path.resolve(),
            value=value,
        )
    return ProductFamilyCatalog(
        state="configured" if families else "empty",
        families=MappingProxyType(families),
    )


def resolve_product_family(
    *,
    catalog: ProductFamilyCatalog,
    runtime_family_id: str | None,
    environ: Mapping[str, str],
    env_file: ParsedEnvFile | None,
    manifest_default: str | None,
) -> ProductFamilySelection | None:
    selected: str | None
    source: SettingSource | None
    if runtime_family_id is not None:
        selected = runtime_family_id
        source = SettingSource(kind="runtime")
    elif "MECH_DESIGN_PRODUCT_FAMILY_ID" in environ:
        selected = environ["MECH_DESIGN_PRODUCT_FAMILY_ID"]
        source = SettingSource(kind="process_environment")
    elif env_file is not None and "MECH_DESIGN_PRODUCT_FAMILY_ID" in env_file.values:
        entry = env_file.values["MECH_DESIGN_PRODUCT_FAMILY_ID"]
        selected = entry.value
        source = SettingSource(
            kind="env_file",
            location=str(env_file.path),
            line=entry.line,
        )
    elif manifest_default is not None:
        selected = manifest_default
        source = SettingSource(kind="manifest")
    else:
        return None
    if _SAFE_ID.fullmatch(selected) is None:
        raise BootstrapFailure(
            "PRODUCT_FAMILY_SELECTION_INVALID",
            "selected product-family ID must be 1 to 128 safe ASCII identifier characters",
        )
    config = catalog.families.get(selected)
    if config is None:
        raise BootstrapFailure(
            "PRODUCT_FAMILY_NOT_FOUND",
            f"selected product family is not registered in the workspace: {selected}",
        )
    assert source is not None
    return ProductFamilySelection(
        family_id=selected,
        config=config,
        source=source,
    )


def build_product_family_config(
    *,
    organization_id: str,
    organization_name: str,
    design_group_id: str,
    design_group_name: str,
    family_id: str,
    family_name: str,
    aliases: Sequence[str],
    actor_id: str,
) -> dict[str, object]:
    organization_id = _safe_id(organization_id, "organization_id")
    design_group_id = _safe_id(design_group_id, "design_group_id")
    family_id = _safe_id(family_id, "family_id")
    actor_id = _safe_id(actor_id, "actor_id")
    organization_name = _name(organization_name, "organization_name") or ""
    design_group_name = _name(design_group_name, "design_group_name") or ""
    family_name = _name(family_name, "family_name") or ""
    normalized_aliases: set[str] = set()
    for alias in aliases:
        if not isinstance(alias, str):
            raise _invalid("aliases must contain only strings")
        normalized = alias.strip()
        if normalized:
            if len(normalized) > 256:
                raise _invalid("aliases must be at most 256 characters")
            normalized_aliases.add(normalized)
    return {
        "schema_version": "product-family-bootstrap/v1",
        "organization_id": organization_id,
        "organization_name": organization_name,
        "design_group_id": design_group_id,
        "design_group_name": design_group_name,
        "family_id": family_id,
        "family_name": family_name,
        "aliases": sorted(normalized_aliases),
        "status": "awaiting-source-folder",
        "subfamily_mode": "discover-and-confirm",
        "expected_initial_models": {"minimum": 1, "maximum": 9},
        "question_batch_limit": 5,
        "minimum_distinct_models_for_generalization": 3,
        "family_owner_actor_id": actor_id,
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


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _publish_new(path: Path, content: bytes) -> None:
    try:
        atomic_publish_new(path, content)
    except FileExistsError as exc:
        raise BootstrapFailure(
            "PRODUCT_FAMILY_CONFLICT",
            f"refusing to overwrite product-family config: {path}",
        ) from exc
    except SecureFilesystemError as exc:
        raise BootstrapFailure(
            exc.code if exc.code.startswith("WINDOWS_") else "ATOMIC_WRITE_FAILED",
            str(exc),
        ) from exc
    except OSError as exc:
        raise BootstrapFailure(
            "ATOMIC_WRITE_FAILED",
            f"cannot atomically publish product-family config: {path}",
        ) from exc


def create_product_family_config(
    *,
    manifest: WorkspaceManifest,
    config: Mapping[str, object],
) -> dict[str, object]:
    family_id = _safe_id(config.get("family_id"), "family_id")
    path = manifest.product_families / f"{family_id}.json"
    validated = validate_product_family_config(dict(config), path=path)
    expected = _canonical_json_bytes(validated)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise BootstrapFailure(
                "PRODUCT_FAMILY_CONFLICT",
                f"product-family target is unsafe: {path}",
            )
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BootstrapFailure(
                "PRODUCT_FAMILY_CONFLICT",
                f"cannot verify product-family config: {path}",
            ) from exc
        if actual != expected:
            raise BootstrapFailure(
                "PRODUCT_FAMILY_CONFLICT",
                f"product-family config already exists with different content: {path}",
            )
        result = "already_registered"
    else:
        _publish_new(path, expected)
        result = "created"
    return {
        "schema_version": "MechanicalDesignProductFamilyRegistration/v1",
        "status": "ok",
        "result": result,
        "family_id": family_id,
        "path": str(path),
    }


def set_default_product_family(
    *,
    manifest: WorkspaceManifest,
    family_id: str,
) -> dict[str, object]:
    family_id = _safe_id(family_id, "family_id")
    catalog = load_product_family_catalog(manifest.product_families)
    if family_id not in catalog.families:
        raise BootstrapFailure(
            "PRODUCT_FAMILY_NOT_FOUND",
            f"product family is not registered in the workspace: {family_id}",
        )
    if manifest.default_product_family_id == family_id:
        result = "already_selected"
    else:
        updated = dict(manifest.raw)
        updated["default_product_family_id"] = family_id
        atomic_replace_managed_json(
            manifest.workspace / MANIFEST_RELATIVE_PATH,
            updated,
        )
        result = "updated"
    return {
        "schema_version": "MechanicalDesignProductFamilyDefault/v1",
        "status": "ok",
        "result": result,
        "family_id": family_id,
    }
