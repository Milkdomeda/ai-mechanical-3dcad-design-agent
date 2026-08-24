from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import Sequence


@dataclass(frozen=True)
class FileIdentity:
    volume: int
    file_index: int


@dataclass(frozen=True)
class ManagedPath:
    path: Path
    identity: FileIdentity | None
    volume_root: Path


@dataclass(frozen=True)
class ManagedFileRead:
    content: bytes
    sha256: str
    size_bytes: int
    identity: FileIdentity
    link_count: int


@dataclass(frozen=True)
class ManagedDirectoryEntry:
    name: str
    is_directory: bool
    identity: FileIdentity


class SecureFilesystemError(ValueError, RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def backend_module_name(platform_name: str) -> str:
    if platform_name == "posix":
        return "mechanical_design_agent.secure_fs_posix"
    if platform_name == "nt":
        return "mechanical_design_agent.secure_fs_windows"
    raise RuntimeError(f"unsupported operating system: {platform_name}")


def _load_backend(platform_name: str | None = None) -> ModuleType:
    return importlib.import_module(backend_module_name(platform_name or os.name))


_backend: ModuleType | None = None


def _get_backend() -> ModuleType:
    global _backend
    if _backend is None:
        _backend = _load_backend()
    return _backend


def validate_managed_path(path: Path, *, allow_missing_leaf: bool) -> ManagedPath:
    return _get_backend().validate_managed_path(
        path, allow_missing_leaf=allow_missing_leaf
    )


def validate_external_read_path(path: Path) -> Path:
    return _get_backend().validate_external_read_path(path)


def relative_managed_path(
    path: Path,
    root: Path,
    *,
    allow_missing_leaf: bool = False,
) -> Path:
    """Return a relative path after canonicalizing both operands via the backend."""
    managed_root = validate_managed_path(root, allow_missing_leaf=False)
    managed_path = validate_managed_path(
        path,
        allow_missing_leaf=allow_missing_leaf,
    )
    return managed_path.path.relative_to(managed_root.path)


def same_managed_path(left: Path, right: Path) -> bool:
    """Compare two existing managed paths by backend-proven file identity."""
    managed_left = validate_managed_path(left, allow_missing_leaf=False)
    managed_right = validate_managed_path(right, allow_missing_leaf=False)
    if managed_left.identity is not None and managed_right.identity is not None:
        return managed_left.identity == managed_right.identity
    return managed_left.path == managed_right.path


def read_managed_file(path: Path) -> ManagedFileRead:
    """Read a regular managed file while its ancestors and leaf stay pinned."""
    return _get_backend().read_managed_file(path)


def list_managed_directory(path: Path) -> Sequence[ManagedDirectoryEntry]:
    """Enumerate one managed directory with stable ancestor and child identities."""
    return _get_backend().list_managed_directory(path)


def ensure_managed_directory(
    path: Path,
    *,
    parents: bool,
    exist_ok: bool,
) -> ManagedPath:
    return _get_backend().ensure_managed_directory(
        path,
        parents=parents,
        exist_ok=exist_ok,
    )


def exclusive_file_lock(path: Path):
    return _get_backend().exclusive_file_lock(path)


def exclusive_creation_lock(path: Path):
    return _get_backend().exclusive_creation_lock(path)


def atomic_publish_new(path: Path, content: bytes) -> None:
    _get_backend().atomic_publish_new(path, content)


def atomic_replace(path: Path, content: bytes) -> None:
    _get_backend().atomic_replace(path, content)


def atomic_publish_owned_file(
    staged: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    _get_backend().atomic_publish_owned_file(
        staged,
        destination,
        replace_existing=replace_existing,
    )


def atomic_publish_directory(source: Path, destination: Path) -> None:
    _get_backend().atomic_publish_directory(source, destination)


def atomic_move_pinned_directory(
    source: Path,
    destination: Path,
    *,
    expected_identity: FileIdentity,
) -> None:
    """Atomically move one directory only while its receipt-pinned identity matches."""
    _get_backend().atomic_move_pinned_directory(
        source, destination, expected_identity=expected_identity
    )


def remove_owned_tree(path: Path, *, expected_parent: Path, label: str) -> None:
    _get_backend().remove_owned_tree(
        path, expected_parent=expected_parent, label=label
    )


def remove_owned_directory_exact(
    path: Path,
    *,
    expected_parent: Path,
    allowed_files: set[str] | frozenset[str],
    label: str,
) -> None:
    """Remove a flat owned directory only when every descendant is expected."""
    _get_backend().remove_owned_directory_exact(
        path,
        expected_parent=expected_parent,
        allowed_files=frozenset(allowed_files),
        label=label,
    )


def set_managed_file_readonly(path: Path) -> None:
    """Remove write permissions while the regular file and ancestors stay pinned."""
    _get_backend().set_managed_file_readonly(path)


def ingest_cas_file(
    source: Path,
    destination: Path,
    expected_sha256: str,
    *,
    allowed_source_root: Path | None,
) -> tuple[str, int]:
    return _get_backend().ingest_cas_file(
        source,
        destination,
        expected_sha256,
        allowed_source_root=allowed_source_root,
    )


def verify_cas_file(path: Path, expected_sha256: str) -> tuple[str, int]:
    return _get_backend().verify_cas_file(path, expected_sha256)
