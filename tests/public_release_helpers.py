from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MANIFEST_NAME = "public-repository.toml"
SCHEMA_VERSION = "PublicRepositoryAllowlist/v1"
TOP_LEVEL_KEYS = {
    "schema_version",
    "root_files",
    "public_docs",
    "source_trees",
    "public_tests",
    "public_ci",
    "public_scripts",
    "excluded_private_paths",
    "compatibility_paths",
}
EXACT_FILE_GROUPS = (
    "root_files",
    "public_docs",
    "public_tests",
    "public_ci",
    "public_scripts",
)


@dataclass(frozen=True)
class CompatibilityPath:
    path: str
    rule_id: str
    reason: str


@dataclass(frozen=True)
class PublicRepositoryManifest:
    root_files: tuple[str, ...]
    public_docs: tuple[str, ...]
    source_trees: tuple[str, ...]
    public_tests: tuple[str, ...]
    public_ci: tuple[str, ...]
    public_scripts: tuple[str, ...]
    excluded_private_paths: tuple[str, ...]
    compatibility_paths: tuple[CompatibilityPath, ...]


def _safe_relative_path(value: object, label: str, *, allow_glob: bool) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must contain nonblank normalized strings")
    if "\\" in value:
        raise ValueError(f"{label} paths must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{label} path escapes the repository: {value}")
    if not allow_glob and any(character in value for character in "*?[]"):
        raise ValueError(f"{label} does not permit globs: {value}")
    return value


def _path_group(
    raw: dict[str, object],
    key: str,
    *,
    allow_glob: bool = False,
) -> tuple[str, ...]:
    values = raw.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{key} must be an array")
    normalized = tuple(
        _safe_relative_path(value, key, allow_glob=allow_glob) for value in values
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{key} contains duplicate paths")
    return normalized


def load_public_repository_manifest(root: Path) -> PublicRepositoryManifest:
    manifest_path = root / MANIFEST_NAME
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load {MANIFEST_NAME}") from exc
    unknown = set(raw) - TOP_LEVEL_KEYS
    missing = TOP_LEVEL_KEYS - set(raw)
    if unknown or missing:
        raise ValueError(
            f"manifest keys mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported public repository manifest schema")

    compatibility_raw = raw["compatibility_paths"]
    if not isinstance(compatibility_raw, list):
        raise ValueError("compatibility_paths must be an array of tables")
    compatibility: list[CompatibilityPath] = []
    for item in compatibility_raw:
        if not isinstance(item, dict) or set(item) != {"path", "rule_id", "reason"}:
            raise ValueError("compatibility path entries require path, rule_id, reason")
        path = _safe_relative_path(item["path"], "compatibility_paths", allow_glob=False)
        rule_id = item["rule_id"]
        reason = item["reason"]
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("compatibility rule_id must be nonblank")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("compatibility reason must be nonblank")
        compatibility.append(
            CompatibilityPath(path=path, rule_id=rule_id, reason=reason)
        )
    compatibility_keys = {(entry.path, entry.rule_id) for entry in compatibility}
    if len(compatibility_keys) != len(compatibility):
        raise ValueError("compatibility_paths contains duplicate path/rule pairs")

    manifest = PublicRepositoryManifest(
        root_files=_path_group(raw, "root_files"),
        public_docs=_path_group(raw, "public_docs"),
        source_trees=_path_group(raw, "source_trees"),
        public_tests=_path_group(raw, "public_tests"),
        public_ci=_path_group(raw, "public_ci"),
        public_scripts=_path_group(raw, "public_scripts"),
        excluded_private_paths=_path_group(raw, "excluded_private_paths"),
        compatibility_paths=tuple(compatibility),
    )
    for group in EXACT_FILE_GROUPS:
        for relative in getattr(manifest, group):
            candidate = root / relative
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"{group} member is not a regular file: {relative}")
    return manifest


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def iter_public_repository_files(
    root: Path,
    manifest: PublicRepositoryManifest,
) -> tuple[Path, ...]:
    canonical_root = root.resolve()
    included: set[Path] = set()
    for group in EXACT_FILE_GROUPS:
        for relative in getattr(manifest, group):
            candidate = root / relative
            resolved = candidate.resolve()
            if candidate.is_symlink() or not _inside(canonical_root, resolved):
                raise ValueError(f"public path escapes repository: {relative}")
            included.add(candidate)

    for relative in manifest.source_trees:
        directory = root / relative
        resolved_directory = directory.resolve()
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not _inside(canonical_root, resolved_directory)
        ):
            raise ValueError(f"source tree is invalid: {relative}")
        for candidate in directory.rglob("*"):
            if candidate.is_dir():
                if candidate.is_symlink():
                    raise ValueError(f"source tree contains symlink: {candidate}")
                continue
            if "__pycache__" in candidate.parts or candidate.suffix == ".pyc":
                continue
            resolved = candidate.resolve()
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"source tree contains non-regular file: {candidate}")
            if not _inside(canonical_root, resolved):
                raise ValueError(f"source tree member escapes repository: {candidate}")
            included.add(candidate)

    relative_paths = sorted(
        (path.relative_to(root) for path in included),
        key=lambda path: path.as_posix(),
    )
    return tuple(relative_paths)


def materialize_public_projection(
    root: Path,
    destination: Path,
    manifest: PublicRepositoryManifest,
) -> tuple[Path, ...]:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("public projection destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for relative in iter_public_repository_files(root, manifest):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
        copied.append(relative)
    return tuple(copied)


def read_public_text_files(root: Path, files: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in files:
        try:
            result[relative.as_posix()] = (root / relative).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"public file is not UTF-8 text: {relative}") from exc
    return result
