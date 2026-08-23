from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
from typing import Iterator

from .secure_fs import (
    FileIdentity,
    ManagedDirectoryEntry,
    ManagedFileRead,
    ManagedPath,
    SecureFilesystemError,
)


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_RMTREE_AVOIDS_SYMLINK_ATTACKS = shutil.rmtree.avoids_symlink_attacks


def open_directory_chain(root: Path, parts: tuple[str, ...] = ()) -> int:
    root_path = Path(root)
    if not root_path.is_absolute():
        raise ValueError("trusted directory root must be absolute")
    components = root_path.parts[1:] + parts
    if any(
        not component or component in {".", ".."} or os.sep in component
        for component in components
    ):
        raise ValueError("trusted directory path contains an unsafe component")

    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in components:
            next_descriptor = os.open(
                component, _DIRECTORY_FLAGS, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def open_or_create_directory_at(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def directory_entry_matches_fd(
    parent_fd: int, name: str, expected_descriptor: int
) -> bool:
    try:
        current_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        pinned = os.fstat(expected_descriptor)
        current = os.fstat(current_descriptor)
        return (pinned.st_dev, pinned.st_ino) == (current.st_dev, current.st_ino)
    finally:
        os.close(current_descriptor)


def _entry_matches_fd(
    parent_fd: int,
    name: str,
    expected_descriptor: int,
    *,
    directory: bool,
) -> bool:
    flags = _DIRECTORY_FLAGS if directory else os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        current_descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        pinned = os.fstat(expected_descriptor)
        current = os.fstat(current_descriptor)
        return (pinned.st_dev, pinned.st_ino) == (current.st_dev, current.st_ino)
    finally:
        os.close(current_descriptor)


def _open_pinned_directory_chain(path: Path) -> tuple[Path, list[int]]:
    canonical = _normalize_top_level_alias(_absolute(path))
    descriptors = [os.open(os.sep, _DIRECTORY_FLAGS)]
    try:
        for component in canonical.parts[1:]:
            descriptors.append(
                os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptors[-1])
            )
        return canonical, descriptors
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _verify_pinned_directory_chain(canonical: Path, descriptors: list[int]) -> None:
    for index, component in enumerate(canonical.parts[1:]):
        if not _entry_matches_fd(
            descriptors[index],
            component,
            descriptors[index + 1],
            directory=True,
        ):
            raise SecureFilesystemError(
                "MANAGED_PATH_IDENTITY_CHANGED",
                "managed directory ancestor changed during pinned operation",
            )


def remove_tree_at(parent_fd: int, child_name: str, *, label: str) -> None:
    try:
        child_fd = os.open(child_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{label} directory must not be a symlink") from exc
        raise
    else:
        os.close(child_fd)

    if _RMTREE_AVOIDS_SYMLINK_ATTACKS:
        shutil.rmtree(child_name, dir_fd=parent_fd)
    else:
        _remove_tree_at_fallback(parent_fd, child_name, label=label)
    os.fsync(parent_fd)


def _remove_tree_at_fallback(parent_fd: int, child_name: str, *, label: str) -> None:
    try:
        child_fd = os.open(child_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{label} directory must not be a symlink") from exc
        raise
    try:
        for entry in os.listdir(child_fd):
            metadata = os.stat(entry, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                _remove_tree_at_fallback(child_fd, entry, label=label)
            else:
                os.unlink(entry, dir_fd=child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(child_name, dir_fd=parent_fd)


def _absolute(path: Path) -> Path:
    value = Path(os.path.abspath(path))
    if not value.is_absolute():
        raise SecureFilesystemError(
            "MANAGED_PATH_INVALID", "managed path must be absolute"
        )
    return value


def _normalize_top_level_alias(path: Path) -> Path:
    """Canonicalize only OS-owned top-level aliases such as macOS /var."""
    if len(path.parts) < 2:
        return path
    top_level = Path(path.anchor) / path.parts[1]
    if not top_level.is_symlink():
        return path
    try:
        canonical_top_level = top_level.resolve(strict=True)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_INVALID",
            "managed path top-level alias cannot be resolved safely",
        ) from exc
    return canonical_top_level.joinpath(*path.parts[2:])


def validate_managed_path(path: Path, *, allow_missing_leaf: bool) -> ManagedPath:
    canonical = _normalize_top_level_alias(_absolute(path))
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        components = canonical.parts[1:]
        final_is_file = canonical.is_file()
        for index, component in enumerate(components):
            flags = (
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                if final_is_file and index == len(components) - 1
                else _DIRECTORY_FLAGS
            )
            try:
                next_descriptor = os.open(
                    component, flags, dir_fd=descriptor
                )
            except FileNotFoundError:
                if allow_missing_leaf:
                    return ManagedPath(canonical, None, Path(canonical.anchor))
                raise SecureFilesystemError(
                    "MANAGED_PATH_INVALID",
                    f"managed path does not exist: {canonical}",
                ) from None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SecureFilesystemError(
                        "MANAGED_PATH_SYMLINK",
                        "managed path must not contain a symlink",
                    ) from exc
                raise SecureFilesystemError(
                    "MANAGED_PATH_INVALID", "managed path cannot be opened safely"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        return ManagedPath(
            canonical,
            FileIdentity(metadata.st_dev, metadata.st_ino),
            Path(canonical.anchor),
        )
    finally:
        os.close(descriptor)


def validate_external_read_path(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SecureFilesystemError(
            "EXTERNAL_PATH_INVALID", "external path cannot be resolved"
        ) from exc


def read_managed_file(path: Path) -> ManagedFileRead:
    target = _normalize_top_level_alias(_absolute(path))
    try:
        parent, descriptors = _open_pinned_directory_chain(target.parent)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "managed file ancestors cannot be pinned",
        ) from exc
    leaf_fd: int | None = None
    try:
        leaf_fd = os.open(
            target.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptors[-1],
        )
        metadata = os.fstat(leaf_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecureFilesystemError(
                "MANAGED_FILE_CONFLICT", "managed read target must be a regular file"
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(leaf_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            size += len(chunk)
        if not _entry_matches_fd(
            descriptors[-1], target.name, leaf_fd, directory=False
        ):
            raise SecureFilesystemError(
                "MANAGED_PATH_IDENTITY_CHANGED",
                "managed file changed during pinned read",
            )
        _verify_pinned_directory_chain(parent, descriptors)
        return ManagedFileRead(
            content=b"".join(chunks),
            sha256=digest.hexdigest(),
            size_bytes=size,
            identity=FileIdentity(metadata.st_dev, metadata.st_ino),
        )
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "managed file cannot be read safely",
        ) from exc
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def list_managed_directory(path: Path) -> tuple[ManagedDirectoryEntry, ...]:
    try:
        canonical, descriptors = _open_pinned_directory_chain(path)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "managed directory cannot be pinned for enumeration",
        ) from exc
    children: list[tuple[str, bool, int, os.stat_result]] = []
    try:
        for name in sorted(os.listdir(descriptors[-1])):
            metadata = os.stat(
                name, dir_fd=descriptors[-1], follow_symlinks=False
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise SecureFilesystemError(
                    "MANAGED_PATH_SYMLINK",
                    "managed directory entry must not be a symlink",
                )
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if not is_directory and not stat.S_ISREG(metadata.st_mode):
                raise SecureFilesystemError(
                    "MANAGED_FILE_CONFLICT",
                    "managed directory contains an unsupported file type",
                )
            flags = (
                _DIRECTORY_FLAGS
                if is_directory
                else os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            child_fd = os.open(name, flags, dir_fd=descriptors[-1])
            opened_metadata = os.fstat(child_fd)
            if is_directory != stat.S_ISDIR(opened_metadata.st_mode) or (
                not is_directory and not stat.S_ISREG(opened_metadata.st_mode)
            ):
                os.close(child_fd)
                raise SecureFilesystemError(
                    "MANAGED_PATH_IDENTITY_CHANGED",
                    "managed directory entry type changed during enumeration",
                )
            children.append((name, is_directory, child_fd, opened_metadata))
        result: list[ManagedDirectoryEntry] = []
        for name, is_directory, child_fd, metadata in children:
            if not _entry_matches_fd(
                descriptors[-1], name, child_fd, directory=is_directory
            ):
                raise SecureFilesystemError(
                    "MANAGED_PATH_IDENTITY_CHANGED",
                    "managed directory entry changed during enumeration",
                )
            result.append(
                ManagedDirectoryEntry(
                    name=name,
                    is_directory=is_directory,
                    identity=FileIdentity(metadata.st_dev, metadata.st_ino),
                )
            )
        _verify_pinned_directory_chain(canonical, descriptors)
        return tuple(result)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "managed directory cannot be enumerated safely",
        ) from exc
    finally:
        for _, _, child_fd, _ in reversed(children):
            os.close(child_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def ensure_managed_directory(
    path: Path,
    *,
    parents: bool,
    exist_ok: bool,
) -> ManagedPath:
    candidate = validate_managed_path(path, allow_missing_leaf=True).path
    existed = candidate.exists()
    if existed and not exist_ok:
        raise FileExistsError(candidate)
    if existed:
        return validate_managed_path(candidate, allow_missing_leaf=False)
    if not parents and not candidate.parent.is_dir():
        raise FileNotFoundError(candidate.parent)
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        components = candidate.parts[1:]
        for index, component in enumerate(components):
            if not parents and index < len(components) - 1:
                next_descriptor = os.open(
                    component, _DIRECTORY_FLAGS, dir_fd=descriptor
                )
            else:
                next_descriptor = open_or_create_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = next_descriptor
    finally:
        os.close(descriptor)
    return validate_managed_path(candidate, allow_missing_leaf=False)


def _fsync_directory(path: Path) -> None:
    canonical = validate_managed_path(path, allow_missing_leaf=False).path
    descriptor = open_directory_chain(canonical)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    target = _absolute(path)
    validate_managed_path(target.parent, allow_missing_leaf=False)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_FILE_CONFLICT", "lock file is not a stable managed file"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SecureFilesystemError(
                "MANAGED_FILE_CONFLICT", "lock file must be a regular file"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def exclusive_creation_lock(path: Path) -> Iterator[None]:
    target = _managed_target(path)
    parent_fd = open_directory_chain(target.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target.name,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        identity = os.fstat(descriptor)
        os.fsync(parent_fd)
        try:
            yield
        finally:
            current = os.stat(
                target.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (current.st_dev, current.st_ino) != (
                identity.st_dev,
                identity.st_ino,
            ):
                raise SecureFilesystemError(
                    "MANAGED_PATH_IDENTITY_CHANGED",
                    "initialization lock changed before cleanup",
                )
            os.unlink(target.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _write_temporary(path: Path, content: bytes) -> Path:
    validate_managed_path(path.parent, allow_missing_leaf=False)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _managed_target(path: Path) -> Path:
    lexical = _absolute(path)
    parent = validate_managed_path(
        lexical.parent, allow_missing_leaf=False
    ).path
    return parent / lexical.name


def atomic_publish_new(path: Path, content: bytes) -> None:
    target = _managed_target(path)
    temporary = _write_temporary(target, content)
    try:
        os.link(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_file_identity(path: Path) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_FILE_CONFLICT", "managed target is not a stable file"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecureFilesystemError(
                "MANAGED_FILE_CONFLICT", "managed target must be a regular file"
            )
        return FileIdentity(metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)


def atomic_replace(path: Path, content: bytes) -> None:
    target = _managed_target(path)
    expected = _regular_file_identity(target)
    temporary = _write_temporary(target, content)
    try:
        if _regular_file_identity(target) != expected:
            raise SecureFilesystemError(
                "MANAGED_PATH_IDENTITY_CHANGED",
                "managed target changed during update",
            )
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_publish_owned_file(
    staged: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    source = validate_managed_path(staged, allow_missing_leaf=False).path
    target = _managed_target(destination)
    source_identity = _regular_file_identity(source)
    target_identity = None
    if target.exists() or target.is_symlink():
        if not replace_existing:
            raise FileExistsError(target)
        target_identity = _regular_file_identity(target)
    if _regular_file_identity(source) != source_identity:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "staged file changed before publication",
        )
    if target_identity is not None and _regular_file_identity(target) != target_identity:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "managed target changed before publication",
        )
    if replace_existing:
        os.replace(source, target)
    else:
        os.link(source, target)
        source.unlink()
    _fsync_directory(target.parent)


def atomic_publish_directory(source: Path, destination: Path) -> None:
    staged = validate_managed_path(source, allow_missing_leaf=False).path
    target = _managed_target(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    source_parent_fd = open_directory_chain(staged.parent)
    target_parent_fd = open_directory_chain(target.parent)
    try:
        os.rename(
            staged.name,
            target.name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=target_parent_fd,
        )
        os.fsync(target_parent_fd)
    finally:
        os.close(target_parent_fd)
        os.close(source_parent_fd)


def remove_owned_tree(path: Path, *, expected_parent: Path, label: str) -> None:
    target = _absolute(path)
    parent = _absolute(expected_parent)
    if target.parent != parent:
        raise SecureFilesystemError(
            "MANAGED_PATH_INVALID", f"{label} cleanup target has unexpected parent"
        )
    try:
        parent_fd = open_directory_chain(parent)
    except OSError as exc:
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            f"{label} root must remain a stable directory",
        ) from exc
    try:
        remove_tree_at(parent_fd, target.name, label=label)
    finally:
        os.close(parent_fd)


def _open_source(source: Path, allowed_root: Path | None) -> tuple[int, Path]:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if allowed_root is None:
        resolved = Path(source).resolve(strict=True)
        try:
            descriptor = os.open(resolved, os.O_RDONLY | no_follow)
        except OSError as exc:
            raise SecureFilesystemError(
                "ARTIFACT_SOURCE_INVALID",
                "artifact source is not a stable regular file",
            ) from exc
        source_path = resolved
    else:
        root = Path(allowed_root).resolve(strict=True)
        lexical_root = Path(os.path.abspath(allowed_root))
        lexical_source = Path(os.path.abspath(source))
        try:
            relative = lexical_source.relative_to(lexical_root)
        except ValueError as exc:
            try:
                relative = lexical_source.relative_to(root)
            except ValueError:
                raise SecureFilesystemError(
                    "ARTIFACT_SOURCE_INVALID",
                    "artifact source must remain inside the allowed workspace",
                ) from exc
        if not relative.parts:
            raise SecureFilesystemError(
                "ARTIFACT_SOURCE_INVALID", "artifact source must be a file"
            )
        source_path = root / relative
        directory_fd = open_directory_chain(root)
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | no_follow, dir_fd=directory_fd
            )
        except OSError as exc:
            raise SecureFilesystemError(
                "ARTIFACT_SOURCE_INVALID",
                "artifact source is not stable inside the allowed workspace",
            ) from exc
        finally:
            os.close(directory_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SecureFilesystemError(
            "ARTIFACT_SOURCE_INVALID", "artifact source must be a regular file"
        )
    return descriptor, source_path


def _digest_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _verify_file_at(
    parent_fd: int, name: str, expected_sha256: str
) -> tuple[str, int]:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, os.O_RDONLY | no_follow, dir_fd=parent_fd)
    except OSError as exc:
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact target is missing or unsafe",
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact target is not a regular file",
        )
    if stat.S_IMODE(metadata.st_mode) & 0o222:
        os.close(descriptor)
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact must not be writable",
        )
    actual_sha256, size_bytes = _digest_descriptor(descriptor)
    if actual_sha256 != expected_sha256:
        raise SecureFilesystemError(
            "ARTIFACT_CHECKSUM_MISMATCH",
            "content-addressed artifact checksum mismatch",
        )
    return actual_sha256, size_bytes


def _create_temporary_at(root_fd: int) -> tuple[int, str]:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    for _ in range(100):
        name = f".artifact.{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | no_follow,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise RuntimeError("could not allocate a unique artifact temporary file")


def verify_cas_file(path: Path, expected_sha256: str) -> tuple[str, int]:
    target = _absolute(path)
    try:
        descriptor = os.open(
            target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact must be a stable regular file",
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact target is not a regular file",
        )
    if stat.S_IMODE(metadata.st_mode) & 0o222:
        os.close(descriptor)
        raise SecureFilesystemError(
            "ARTIFACT_TARGET_INVALID",
            "content-addressed artifact must not be writable",
        )
    actual, size = _digest_descriptor(descriptor)
    if actual != expected_sha256:
        raise SecureFilesystemError(
            "ARTIFACT_CHECKSUM_MISMATCH",
            "content-addressed artifact checksum mismatch",
        )
    return actual, size


def ingest_cas_file(
    source: Path,
    destination: Path,
    expected_sha256: str,
    *,
    allowed_source_root: Path | None,
) -> tuple[str, int]:
    source_fd, _ = _open_source(source, allowed_source_root)
    target = _absolute(destination)
    cas_root = target.parents[2]
    try:
        root_fd = open_directory_chain(cas_root)
    except OSError as exc:
        os.close(source_fd)
        raise SecureFilesystemError(
            "MANAGED_PATH_IDENTITY_CHANGED",
            "artifact root must remain a stable directory",
        ) from exc
    temporary_fd: int | None = None
    temporary_name: str | None = None
    first_digest_fd: int | None = None
    target_parent_fd: int | None = None
    try:
        temporary_fd, temporary_name = _create_temporary_at(root_fd)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(source_fd, "rb") as input_stream, os.fdopen(
            temporary_fd, "wb"
        ) as output_stream:
            source_fd = -1
            temporary_fd = None
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(chunk)
                output_stream.write(chunk)
                size += len(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            os.fchmod(output_stream.fileno(), 0o444)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise SecureFilesystemError(
                "ARTIFACT_CHECKSUM_MISMATCH", "artifact source checksum changed"
            )
        first_digest_fd = open_or_create_directory_at(root_fd, actual[:2])
        target_parent_fd = open_or_create_directory_at(
            first_digest_fd, actual[2:4]
        )
        try:
            existing_fd = os.open(
                target.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=target_parent_fd,
            )
        except FileNotFoundError:
            existing_fd = None
        except OSError as exc:
            raise SecureFilesystemError(
                "ARTIFACT_TARGET_INVALID",
                "content-addressed artifact target is unsafe",
            ) from exc
        if existing_fd is not None:
            metadata = os.fstat(existing_fd)
            os.close(existing_fd)
            if stat.S_ISREG(metadata.st_mode) and not (
                stat.S_IMODE(metadata.st_mode) & 0o222
            ):
                os.unlink(temporary_name, dir_fd=root_fd)
            else:
                os.replace(
                    temporary_name,
                    target.name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=target_parent_fd,
                )
        else:
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=root_fd,
                dst_dir_fd=target_parent_fd,
            )
        os.fsync(target_parent_fd)
        verified = _verify_file_at(target_parent_fd, target.name, expected_sha256)
        if not directory_entry_matches_fd(
            root_fd, actual[:2], first_digest_fd
        ) or not directory_entry_matches_fd(
            first_digest_fd, actual[2:4], target_parent_fd
        ):
            raise SecureFilesystemError(
                "MANAGED_PATH_IDENTITY_CHANGED",
                "artifact target path changed during publication",
            )
        try:
            current_root_fd = open_directory_chain(cas_root)
        except OSError as exc:
            raise SecureFilesystemError(
                "MANAGED_PATH_IDENTITY_CHANGED",
                "artifact root path changed during publication",
            ) from exc
        try:
            pinned_root = os.fstat(root_fd)
            current_root = os.fstat(current_root_fd)
            if (pinned_root.st_dev, pinned_root.st_ino) != (
                current_root.st_dev,
                current_root.st_ino,
            ):
                raise SecureFilesystemError(
                    "MANAGED_PATH_IDENTITY_CHANGED",
                    "artifact root path changed during publication",
                )
        finally:
            os.close(current_root_fd)
        return verified
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        if target_parent_fd is not None:
            os.close(target_parent_fd)
        if first_digest_fd is not None:
            os.close(first_digest_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        os.close(root_fd)
