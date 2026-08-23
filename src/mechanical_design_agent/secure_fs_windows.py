from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from .secure_fs import (
    FileIdentity,
    ManagedDirectoryEntry,
    ManagedFileRead,
    ManagedPath,
    SecureFilesystemError,
)


@dataclass(frozen=True)
class Win32Api:
    pywintypes: Any
    win32api: Any
    win32con: Any
    win32file: Any


@dataclass(frozen=True)
class _PinnedPath:
    path: Path
    identity: FileIdentity | None
    volume_root: Path
    missing_parts: tuple[str, ...]


def load_win32_api() -> Win32Api:
    try:
        api = Win32Api(
            pywintypes=importlib.import_module("pywintypes"),
            win32api=importlib.import_module("win32api"),
            win32con=importlib.import_module("win32con"),
            win32file=importlib.import_module("win32file"),
        )
        required = (
            (api.pywintypes, ("OVERLAPPED",)),
            (api.win32api, ("GetVolumeInformation",)),
            (
                api.win32file,
                (
                    "FileDispositionInfo",
                    "CreateFile",
                    "GetDriveType",
                    "GetFileAttributes",
                    "GetFileInformationByHandle",
                    "GetFinalPathNameByHandle",
                    "LockFileEx",
                    "MoveFileEx",
                    "ReadFile",
                    "SetFileAttributes",
                    "SetFileInformationByHandle",
                    "UnlockFileEx",
                ),
            ),
        )
        if any(
            not hasattr(module, name)
            for module, names in required
            for name in names
        ):
            raise AttributeError("required pywin32 filesystem API is missing")
        return api
    except (ImportError, OSError) as exc:
        raise SecureFilesystemError(
            "WINDOWS_SECURE_FS_UNAVAILABLE",
            "the required Windows secure filesystem backend is unavailable",
        ) from exc
    except AttributeError as exc:
        raise SecureFilesystemError(
            "WINDOWS_SECURE_FS_UNAVAILABLE",
            "the required Windows secure filesystem API is unavailable",
        ) from exc


def reject_network_path_spelling(value: str | os.PathLike[str]) -> None:
    spelling = os.fspath(value).replace("/", "\\")
    lowered = spelling.casefold()
    extended_local = (
        lowered.startswith("\\\\?\\")
        and len(spelling) >= 7
        and spelling[4].isalpha()
        and spelling[5:7] == ":\\"
    )
    if lowered.startswith("\\\\?\\unc\\") or (
        spelling.startswith("\\\\") and not extended_local
    ):
        raise SecureFilesystemError(
            "WINDOWS_NETWORK_PATH_BLOCKED",
            "managed paths must not use a network or UNC location",
        )


def _absolute(path: Path) -> Path:
    reject_network_path_spelling(path)
    value = Path(os.path.abspath(path))
    if not value.is_absolute() or not value.drive:
        raise SecureFilesystemError(
            "WINDOWS_VOLUME_UNSUPPORTED",
            "managed Windows paths must be absolute drive paths",
        )
    return value


def _volume_root(path: Path) -> Path:
    spelling = str(path).replace("/", "\\")
    if spelling.casefold().startswith("\\\\?\\"):
        drive = spelling[4:6]
    else:
        drive = path.drive
    return Path(f"{drive}\\")


def _validate_volume(path: Path, api: Win32Api) -> Path:
    root = _volume_root(path)
    try:
        drive_type = api.win32file.GetDriveType(str(root))
        volume_info = api.win32api.GetVolumeInformation(str(root))
    except Exception as exc:
        raise SecureFilesystemError(
            "WINDOWS_VOLUME_UNSUPPORTED",
            "managed path volume cannot be verified",
        ) from exc
    if drive_type == getattr(api.win32con, "DRIVE_REMOTE", 4):
        raise SecureFilesystemError(
            "WINDOWS_NETWORK_PATH_BLOCKED",
            "managed paths must not use a mapped or network drive",
        )
    if drive_type != getattr(api.win32con, "DRIVE_FIXED", 3):
        raise SecureFilesystemError(
            "WINDOWS_VOLUME_UNSUPPORTED",
            "managed paths require a local fixed volume",
        )
    filesystem = str(volume_info[4]).strip().casefold()
    if filesystem != "ntfs":
        raise SecureFilesystemError(
            "WINDOWS_VOLUME_UNSUPPORTED",
            "managed paths require a local fixed NTFS volume",
        )
    return root


def _open_handle(path: Path, api: Win32Api, *, directory: bool) -> Any:
    flags = getattr(api.win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000)
    if directory:
        flags |= getattr(api.win32con, "FILE_FLAG_BACKUP_SEMANTICS", 0x02000000)
    share = getattr(api.win32con, "FILE_SHARE_READ", 1) | getattr(
        api.win32con, "FILE_SHARE_WRITE", 2
    )
    try:
        return api.win32file.CreateFile(
            str(path),
            getattr(api.win32con, "GENERIC_READ", 0x80000000),
            share,
            None,
            getattr(api.win32con, "OPEN_EXISTING", 3),
            flags,
            None,
        )
    except Exception as exc:
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "managed path could not be pinned safely",
        ) from exc


def _try_open_handle(path: Path, api: Win32Api, *, directory: bool) -> Any | None:
    try:
        return _open_handle(path, api, directory=directory)
    except SecureFilesystemError as exc:
        cause = exc.__cause__
        if getattr(cause, "winerror", None) in {2, 3}:
            return None
        raise


def _handle_facts(handle: Any, api: Win32Api) -> tuple[FileIdentity, int]:
    try:
        information = api.win32file.GetFileInformationByHandle(handle)
    except Exception as exc:
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "managed path identity could not be inspected",
        ) from exc
    attributes = int(information[0])
    volume = int(information[4])
    file_index = (int(information[8]) << 32) | int(information[9])
    return FileIdentity(volume, file_index), attributes


def _close_handle(handle: Any) -> None:
    close = getattr(handle, "Close", None)
    if close is not None:
        close()


def _assert_not_reparse(attributes: int, api: Win32Api) -> None:
    if attributes & getattr(api.win32con, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise SecureFilesystemError(
            "WINDOWS_REPARSE_POINT_BLOCKED",
            "managed paths must not contain a reparse point",
        )


def _canonical_handle_path(handle: Any, api: Win32Api) -> Path:
    try:
        return Path(api.win32file.GetFinalPathNameByHandle(handle, 0))
    except Exception as exc:
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "managed path canonical identity could not be resolved",
        ) from exc


def _identity(path: Path, api: Win32Api, *, directory: bool) -> FileIdentity:
    handle = _open_handle(path, api, directory=directory)
    try:
        identity, attributes = _handle_facts(handle, api)
        _assert_not_reparse(attributes, api)
        return identity
    finally:
        _close_handle(handle)


@contextmanager
def _pinned_path(path: Path, *, allow_missing_leaf: bool) -> Iterator[_PinnedPath]:
    api = load_win32_api()
    lexical = _absolute(path)
    volume_root = _validate_volume(lexical, api)
    handles: list[Any] = []
    missing_parts: list[str] = []
    final_identity: FileIdentity | None = None
    canonical_ancestor = volume_root
    current = volume_root
    try:
        root_handle = _open_handle(volume_root, api, directory=True)
        handles.append(root_handle)
        root_identity, root_attributes = _handle_facts(root_handle, api)
        _assert_not_reparse(root_attributes, api)
        canonical_ancestor = _canonical_handle_path(root_handle, api)
        final_identity = root_identity

        relative_parts = lexical.parts[1:]
        for index, component in enumerate(relative_parts):
            current = current / component
            if missing_parts:
                missing_parts.append(component)
                continue
            handle = _try_open_handle(current, api, directory=True)
            if handle is None:
                if not allow_missing_leaf:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        "managed path does not exist",
                    )
                missing_parts.append(component)
                final_identity = None
                continue
            handles.append(handle)
            final_identity, attributes = _handle_facts(handle, api)
            _assert_not_reparse(attributes, api)
            is_directory = bool(
                attributes
                & getattr(api.win32con, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
            )
            if index < len(relative_parts) - 1 and not is_directory:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "managed path contains a non-directory ancestor",
                )
            canonical_ancestor = _canonical_handle_path(handle, api)

        canonical = canonical_ancestor.joinpath(*missing_parts)
        if not missing_parts and _identity(
            current, api, directory=True
        ) != final_identity:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed path identity changed during validation",
            )
        yield _PinnedPath(
            canonical,
            final_identity,
            _canonical_handle_path(root_handle, api),
            tuple(missing_parts),
        )
    finally:
        for handle in reversed(handles):
            _close_handle(handle)


def validate_managed_path(path: Path, *, allow_missing_leaf: bool) -> ManagedPath:
    with _pinned_path(path, allow_missing_leaf=allow_missing_leaf) as pinned:
        return ManagedPath(pinned.path, pinned.identity, pinned.volume_root)


def validate_external_read_path(path: Path) -> Path:
    reject_network_path_spelling(path)
    try:
        canonical = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "external path cannot be resolved",
        ) from exc
    return validate_managed_path(canonical, allow_missing_leaf=False).path


def ensure_managed_directory(
    path: Path,
    *,
    parents: bool,
    exist_ok: bool,
) -> ManagedPath:
    requested = _absolute(path)
    first = True
    while True:
        with _pinned_path(requested, allow_missing_leaf=True) as pinned:
            if not pinned.missing_parts:
                if first and not exist_ok:
                    raise FileExistsError(pinned.path)
                return ManagedPath(pinned.path, pinned.identity, pinned.volume_root)
            if not parents and len(pinned.missing_parts) != 1:
                raise FileNotFoundError(pinned.path.parent)
            next_path = pinned.path
            for _ in pinned.missing_parts[1:]:
                next_path = next_path.parent
            try:
                os.mkdir(next_path)
            except FileExistsError:
                pass
        first = False


def _require_regular(path: Path, api: Win32Api) -> FileIdentity:
    handle = _open_handle(path, api, directory=False)
    try:
        identity, attributes = _handle_facts(handle, api)
        _assert_not_reparse(attributes, api)
        if attributes & getattr(api.win32con, "FILE_ATTRIBUTE_DIRECTORY", 0x10):
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed target must be a regular file",
            )
        return identity
    finally:
        _close_handle(handle)


def _entry_facts(
    path: Path,
    api: Win32Api,
    *,
    directory: bool,
) -> tuple[FileIdentity, int]:
    """Inspect one directory entry itself without following a reparse target."""
    handle = _open_handle(path, api, directory=directory)
    try:
        return _handle_facts(handle, api)
    finally:
        _close_handle(handle)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        handle = None
        flags = getattr(api.win32con, "FILE_ATTRIBUTE_NORMAL", 0x80) | getattr(
            api.win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000
        )
        share = getattr(api.win32con, "FILE_SHARE_READ", 1) | getattr(
            api.win32con, "FILE_SHARE_WRITE", 2
        )
        try:
            handle = api.win32file.CreateFile(
                str(target),
                getattr(api.win32con, "GENERIC_READ", 0x80000000)
                | getattr(api.win32con, "GENERIC_WRITE", 0x40000000),
                share,
                None,
                getattr(api.win32con, "OPEN_ALWAYS", 4),
                flags,
                None,
            )
            _, attributes = _handle_facts(handle, api)
            _assert_not_reparse(attributes, api)
            overlapped = api.pywintypes.OVERLAPPED()
            api.win32file.LockFileEx(
                handle,
                getattr(api.win32con, "LOCKFILE_EXCLUSIVE_LOCK", 2),
                0xFFFFFFFF,
                0xFFFFFFFF,
                overlapped,
            )
        except SecureFilesystemError:
            if handle is not None:
                _close_handle(handle)
            raise
        except Exception as exc:
            if handle is not None:
                _close_handle(handle)
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "working-copy lock cannot be acquired safely",
            ) from exc
        try:
            yield
        finally:
            try:
                api.win32file.UnlockFileEx(
                    handle, 0xFFFFFFFF, 0xFFFFFFFF, overlapped
                )
            finally:
                _close_handle(handle)


@contextmanager
def exclusive_creation_lock(path: Path) -> Iterator[None]:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        handle = None
        try:
            handle = api.win32file.CreateFile(
                str(target),
                getattr(api.win32con, "GENERIC_READ", 0x80000000)
                | getattr(api.win32con, "GENERIC_WRITE", 0x40000000)
                | getattr(api.win32con, "DELETE", 0x00010000),
                getattr(api.win32con, "FILE_SHARE_READ", 1)
                | getattr(api.win32con, "FILE_SHARE_WRITE", 2),
                None,
                getattr(api.win32con, "CREATE_NEW", 1),
                getattr(api.win32con, "FILE_ATTRIBUTE_NORMAL", 0x80)
                | getattr(
                    api.win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000
                ),
                None,
            )
            identity, attributes = _handle_facts(handle, api)
            _assert_not_reparse(attributes, api)
        except Exception as exc:
            if handle is not None:
                _close_handle(handle)
            if getattr(exc, "winerror", None) in {80, 183}:
                raise FileExistsError(target) from exc
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "workspace initialization lock cannot be created safely",
            ) from exc
        try:
            yield
        finally:
            try:
                current, current_attributes = _handle_facts(handle, api)
                _assert_not_reparse(current_attributes, api)
                if current != identity:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        "initialization lock changed before cleanup",
                    )
                api.win32file.SetFileInformationByHandle(
                    handle,
                    getattr(api.win32file, "FileDispositionInfo"),
                    True,
                )
            finally:
                _close_handle(handle)


def _write_temporary(path: Path, content: bytes) -> Path:
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


def _move(source: Path, target: Path, *, replace: bool, api: Win32Api) -> None:
    flags = getattr(api.win32con, "MOVEFILE_WRITE_THROUGH", 0x8)
    if replace:
        flags |= getattr(api.win32con, "MOVEFILE_REPLACE_EXISTING", 0x1)
    try:
        api.win32file.MoveFileEx(str(source), str(target), flags)
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if not replace and winerror in {80, 183}:
            raise FileExistsError(target) from exc
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "managed file could not be published atomically",
        ) from exc


def atomic_publish_new(path: Path, content: bytes) -> None:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        temporary = _write_temporary(target, content)
        try:
            _move(temporary, target, replace=False, api=api)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_replace(path: Path, content: bytes) -> None:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        expected = _require_regular(target, api)
        temporary = _write_temporary(target, content)
        try:
            if _require_regular(target, api) != expected:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "managed target changed during update",
                )
            _move(temporary, target, replace=True, api=api)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_publish_owned_file(
    staged: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    api = load_win32_api()
    source_lexical = _absolute(staged)
    target_lexical = _absolute(destination)
    with _pinned_path(
        source_lexical.parent, allow_missing_leaf=False
    ) as source_parent, _pinned_path(
        target_lexical.parent, allow_missing_leaf=False
    ) as target_parent:
        source = source_parent.path / source_lexical.name
        target = target_parent.path / target_lexical.name
        if source_parent.identity is None or target_parent.identity is None:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED", "publication parent is missing"
            )
        if source_parent.identity.volume != target_parent.identity.volume:
            raise SecureFilesystemError(
                "WINDOWS_VOLUME_UNSUPPORTED",
                "managed publication must stay on one NTFS volume",
            )
        source_identity = _require_regular(source, api)
        target_identity = None
        if target.exists():
            if not replace_existing:
                raise FileExistsError(target)
            target_identity = _require_regular(target, api)
        if _require_regular(source, api) != source_identity:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "staged file changed before publication",
            )
        if (
            target_identity is not None
            and _require_regular(target, api) != target_identity
        ):
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed target changed before publication",
            )
        _move(source, target, replace=replace_existing, api=api)


def atomic_publish_directory(source: Path, destination: Path) -> None:
    api = load_win32_api()
    source_lexical = _absolute(source)
    target_lexical = _absolute(destination)
    with _pinned_path(
        source_lexical.parent, allow_missing_leaf=False
    ) as source_parent, _pinned_path(
        target_lexical.parent, allow_missing_leaf=False
    ) as target_parent:
        staged = source_parent.path / source_lexical.name
        target = target_parent.path / target_lexical.name
        source_identity = _identity(staged, api, directory=True)
        if source_parent.identity is None or target_parent.identity is None:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED", "publication parent is missing"
            )
        if source_identity.volume != target_parent.identity.volume:
            raise SecureFilesystemError(
                "WINDOWS_VOLUME_UNSUPPORTED",
                "managed directory publication must stay on one NTFS volume",
            )
        if target.exists():
            raise FileExistsError(target)
        if _identity(staged, api, directory=True) != source_identity:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "staged directory changed before publication",
            )
        _move(staged, target, replace=False, api=api)


def _clear_readonly(path: Path, api: Win32Api) -> None:
    attributes = int(api.win32file.GetFileAttributes(str(path)))
    readonly = getattr(api.win32con, "FILE_ATTRIBUTE_READONLY", 0x1)
    if attributes & readonly:
        api.win32file.SetFileAttributes(str(path), attributes & ~readonly)


def remove_owned_tree(path: Path, *, expected_parent: Path, label: str) -> None:
    api = load_win32_api()
    target_lexical = _absolute(path)
    parent_lexical = _absolute(expected_parent)
    if os.path.normcase(str(target_lexical.parent)) != os.path.normcase(
        str(parent_lexical)
    ):
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            f"{label} cleanup target has an unexpected parent",
        )
    with _pinned_path(parent_lexical, allow_missing_leaf=False) as parent:
        target = parent.path / target_lexical.name
        if not target.exists():
            return
        target_identity = _identity(target, api, directory=True)
        entries: list[tuple[Path, bool, FileIdentity, bool]] = []
        reparse_flag = getattr(
            api.win32con,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        for current_root, directory_names, file_names in os.walk(
            target, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            _identity(current, api, directory=True)
            for name in list(directory_names):
                child = current / name
                identity, attributes = _entry_facts(
                    child,
                    api,
                    directory=True,
                )
                is_reparse = bool(attributes & reparse_flag)
                if is_reparse:
                    directory_names.remove(name)
                    _assert_not_reparse(attributes, api)
                elif _identity(child, api, directory=True) != identity:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        f"{label} cleanup target changed",
                    )
                entries.append((child, True, identity, is_reparse))
            for name in file_names:
                child = current / name
                identity, attributes = _entry_facts(
                    child,
                    api,
                    directory=False,
                )
                is_reparse = bool(attributes & reparse_flag)
                if not is_reparse and _require_regular(child, api) != identity:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        f"{label} cleanup target changed",
                    )
                entries.append((child, False, identity, is_reparse))
        for child, is_directory, expected, was_reparse in reversed(entries):
            current_identity, current_attributes = _entry_facts(
                child,
                api,
                directory=is_directory,
            )
            is_reparse = bool(current_attributes & reparse_flag)
            if current_identity != expected or is_reparse != was_reparse:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    f"{label} cleanup target changed",
                )
            if is_directory:
                child.rmdir()
            else:
                if not is_reparse:
                    _clear_readonly(child, api)
                child.unlink()
        if _identity(target, api, directory=True) != target_identity:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED", f"{label} cleanup root changed"
            )
        target.rmdir()


def _source_path(source: Path, allowed_root: Path | None) -> Path:
    if allowed_root is None:
        resolved = validate_external_read_path(source)
    else:
        root = validate_managed_path(
            Path(allowed_root), allow_missing_leaf=False
        ).path
        resolved = validate_managed_path(
            Path(source), allow_missing_leaf=False
        ).path
        try:
            common = os.path.commonpath((str(root), str(resolved)))
        except ValueError as exc:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "artifact source must remain inside the allowed workspace",
            ) from exc
        if os.path.normcase(common) != os.path.normcase(str(root)) or resolved == root:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "artifact source must remain inside the allowed workspace",
            )
    api = load_win32_api()
    try:
        _require_regular(resolved, api)
    except SecureFilesystemError as exc:
        raise SecureFilesystemError(
            "WINDOWS_PATH_IDENTITY_CHANGED",
            "artifact source must be a regular file",
        ) from exc
    return resolved


def _digest_handle(
    handle: Any,
    api: Win32Api,
    *,
    output_stream: Any | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        try:
            _, chunk = api.win32file.ReadFile(handle, 1024 * 1024)
        except Exception as exc:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed file could not be read through its pinned handle",
            ) from exc
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if output_stream is not None:
            output_stream.write(chunk)
    return digest.hexdigest(), size


def _open_regular_handle(path: Path, api: Win32Api) -> tuple[Any, FileIdentity, int]:
    handle = _open_handle(path, api, directory=False)
    try:
        identity, attributes = _handle_facts(handle, api)
        _assert_not_reparse(attributes, api)
        if attributes & getattr(api.win32con, "FILE_ATTRIBUTE_DIRECTORY", 0x10):
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed target must be a regular file",
            )
        return handle, identity, attributes
    except Exception:
        _close_handle(handle)
        raise


def read_managed_file(path: Path) -> ManagedFileRead:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        handle, identity, _ = _open_regular_handle(target, api)
        try:
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            size = 0
            while True:
                try:
                    _, chunk = api.win32file.ReadFile(handle, 1024 * 1024)
                except Exception as exc:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        "managed file could not be read through its pinned handle",
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
                size += len(chunk)
            if _require_regular(target, api) != identity:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "managed file changed during pinned read",
                )
            return ManagedFileRead(
                content=b"".join(chunks),
                sha256=digest.hexdigest(),
                size_bytes=size,
                identity=identity,
            )
        finally:
            _close_handle(handle)


def list_managed_directory(path: Path) -> tuple[ManagedDirectoryEntry, ...]:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical, allow_missing_leaf=False) as pinned:
        if pinned.identity is None:
            raise SecureFilesystemError(
                "WINDOWS_PATH_IDENTITY_CHANGED",
                "managed directory is missing during enumeration",
            )
        held: list[tuple[str, bool, Any, FileIdentity]] = []
        owned_handles: list[Any] = []
        reparse_flag = getattr(api.win32con, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        directory_flag = getattr(api.win32con, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
        try:
            with os.scandir(pinned.path) as discovered:
                for entry in sorted(discovered, key=lambda item: item.name):
                    metadata = entry.stat(follow_symlinks=False)
                    attributes = int(getattr(metadata, "st_file_attributes", 0))
                    if attributes & reparse_flag:
                        raise SecureFilesystemError(
                            "WINDOWS_REPARSE_POINT_BLOCKED",
                            "managed paths must not contain a reparse point",
                        )
                    is_directory = bool(attributes & directory_flag)
                    child = pinned.path / entry.name
                    handle = _open_handle(child, api, directory=is_directory)
                    owned_handles.append(handle)
                    identity, opened_attributes = _handle_facts(handle, api)
                    _assert_not_reparse(opened_attributes, api)
                    if bool(opened_attributes & directory_flag) != is_directory:
                        raise SecureFilesystemError(
                            "WINDOWS_PATH_IDENTITY_CHANGED",
                            "managed directory entry type changed during enumeration",
                        )
                    held.append((entry.name, is_directory, handle, identity))
            result: list[ManagedDirectoryEntry] = []
            for name, is_directory, _, identity in held:
                current, attributes = _entry_facts(
                    pinned.path / name,
                    api,
                    directory=is_directory,
                )
                _assert_not_reparse(attributes, api)
                if current != identity:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        "managed directory entry changed during enumeration",
                    )
                result.append(
                    ManagedDirectoryEntry(
                        name=name,
                        is_directory=is_directory,
                        identity=identity,
                    )
                )
            if _identity(pinned.path, api, directory=True) != pinned.identity:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "managed directory changed during enumeration",
                )
            return tuple(result)
        finally:
            for handle in reversed(owned_handles):
                _close_handle(handle)


def verify_cas_file(path: Path, expected_sha256: str) -> tuple[str, int]:
    api = load_win32_api()
    lexical = _absolute(path)
    with _pinned_path(lexical.parent, allow_missing_leaf=False) as parent:
        target = parent.path / lexical.name
        handle, identity, attributes = _open_regular_handle(target, api)
        try:
            if not attributes & getattr(
                api.win32con, "FILE_ATTRIBUTE_READONLY", 0x1
            ):
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "content-addressed artifact must not be writable",
                )
            actual, size = _digest_handle(handle, api)
            if actual != expected_sha256:
                raise SecureFilesystemError(
                    "ARTIFACT_CHECKSUM_MISMATCH",
                    "content-addressed artifact checksum mismatch",
                )
            if _require_regular(target, api) != identity:
                raise SecureFilesystemError(
                    "WINDOWS_PATH_IDENTITY_CHANGED",
                    "content-addressed artifact changed during verification",
                )
            return actual, size
        finally:
            _close_handle(handle)


def ingest_cas_file(
    source: Path,
    destination: Path,
    expected_sha256: str,
    *,
    allowed_source_root: Path | None,
) -> tuple[str, int]:
    api = load_win32_api()
    source_path = _source_path(source, allowed_source_root)
    source_handle, source_identity, _ = _open_regular_handle(source_path, api)
    target_lexical = _absolute(destination)
    try:
        ensure_managed_directory(target_lexical.parent, parents=True, exist_ok=True)
        with _pinned_path(
            target_lexical.parent, allow_missing_leaf=False
        ) as target_parent:
            target = target_parent.path / target_lexical.name
            descriptor, name = tempfile.mkstemp(
                prefix=".artifact.", dir=target_parent.path
            )
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output_stream:
                    actual, size = _digest_handle(
                        source_handle, api, output_stream=output_stream
                    )
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if actual != expected_sha256:
                    raise SecureFilesystemError(
                        "ARTIFACT_CHECKSUM_MISMATCH",
                        "artifact source checksum changed",
                    )
                if _require_regular(source_path, api) != source_identity:
                    raise SecureFilesystemError(
                        "WINDOWS_PATH_IDENTITY_CHANGED",
                        "artifact source changed during ingestion",
                    )
                attributes = int(api.win32file.GetFileAttributes(str(temporary)))
                api.win32file.SetFileAttributes(
                    str(temporary),
                    attributes
                    | getattr(api.win32con, "FILE_ATTRIBUTE_READONLY", 0x1),
                )
                replace = False
                if target.exists():
                    expected_target, target_attributes = _entry_facts(
                        target,
                        api,
                        directory=False,
                    )
                    _assert_not_reparse(target_attributes, api)
                    readonly = getattr(
                        api.win32con,
                        "FILE_ATTRIBUTE_READONLY",
                        0x1,
                    )
                    if target_attributes & readonly:
                        return verify_cas_file(target, expected_sha256)
                    replace = True
                    if _require_regular(target, api) != expected_target:
                        raise SecureFilesystemError(
                            "WINDOWS_PATH_IDENTITY_CHANGED",
                            "artifact target changed before repair",
                        )
                    if _require_regular(target, api) != expected_target:
                        raise SecureFilesystemError(
                            "WINDOWS_PATH_IDENTITY_CHANGED",
                            "artifact target changed during repair",
                        )
                try:
                    _move(temporary, target, replace=replace, api=api)
                except FileExistsError:
                    return verify_cas_file(target, expected_sha256)
                return verify_cas_file(target, expected_sha256)
            finally:
                if temporary.exists():
                    try:
                        _clear_readonly(temporary, api)
                    finally:
                        temporary.unlink(missing_ok=True)
    finally:
        _close_handle(source_handle)
