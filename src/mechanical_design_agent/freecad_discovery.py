from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
from typing import Protocol

from .secure_fs import (
    FileIdentity,
    SecureFilesystemError,
    validate_managed_path,
)


_FREECAD_VERSION = re.compile(r"\bFreeCAD(?:Cmd)?\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)
_AMD64_MACHINE = 0x8664
CERTIFIED_FREECADCMD_VERSIONS = frozenset({"1.1.3"})


class FreeCADDiscoveryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RegistryEntry:
    display_name: str
    install_location: Path
    root: str


class RegistryReader(Protocol):
    def entries(self) -> Iterable[RegistryEntry]: ...


@dataclass(frozen=True)
class FreeCADCandidate:
    path: Path
    source: str
    identity: FileIdentity
    version: str


@dataclass(frozen=True)
class FreeCADDiscoveryResult:
    selected: FreeCADCandidate | None
    candidates: tuple[FreeCADCandidate, ...]
    conflict: bool


class WindowsUninstallRegistry:
    _ROOTS = (
        ("HKLM64", "HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM32", "HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKCU", "HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )

    def entries(self) -> tuple[RegistryEntry, ...]:
        try:
            import winreg
        except ImportError:
            return ()
        result: list[RegistryEntry] = []
        for label, hive_name, key_name in self._ROOTS:
            hive = getattr(winreg, hive_name)
            try:
                root = winreg.OpenKey(hive, key_name)
            except OSError:
                continue
            with root:
                index = 0
                while True:
                    try:
                        child_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        child = winreg.OpenKey(root, child_name)
                    except OSError:
                        continue
                    with child:
                        try:
                            display_name = str(
                                winreg.QueryValueEx(child, "DisplayName")[0]
                            )
                            install_location = str(
                                winreg.QueryValueEx(child, "InstallLocation")[0]
                            )
                        except OSError:
                            continue
                    result.append(
                        RegistryEntry(
                            display_name=display_name,
                            install_location=Path(install_location),
                            root=label,
                        )
                    )
        return tuple(result)


def _environment_value(environ: Mapping[str, str], name: str) -> str:
    folded = name.casefold()
    return next(
        (str(value) for key, value in environ.items() if key.casefold() == folded),
        "",
    ).strip()


def _registry_name_matches(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized == "freecad" or normalized.startswith("freecad ")


def _windows_absolute(path: Path) -> bool:
    value = PureWindowsPath(str(path))
    return value.is_absolute() and bool(value.drive)


def _candidate_paths(
    *,
    environ: Mapping[str, str],
    registry: RegistryReader,
    find_on_path: Callable[[str, str], str | None],
) -> tuple[tuple[Path, str], ...]:
    result: list[tuple[Path, str]] = []
    path_value = _environment_value(environ, "PATH")
    if path_value:
        located = find_on_path("FreeCADCmd.exe", path_value)
        if located:
            result.append((Path(located), "path"))

    program_files_value = _environment_value(environ, "ProgramFiles")
    if program_files_value:
        program_files = Path(program_files_value)
        if _windows_absolute(program_files):
            for directory in ("FreeCAD 1.1.3",):
                result.append(
                    (
                        program_files / directory / "bin" / "FreeCADCmd.exe",
                        f"program_files:{directory}",
                    )
                )

    for entry in registry.entries():
        if not _registry_name_matches(entry.display_name):
            continue
        if not _windows_absolute(entry.install_location):
            continue
        result.append(
            (
                entry.install_location / "bin" / "FreeCADCmd.exe",
                f"registry:{entry.root}",
            )
        )
    return tuple(result)


def _require_x64_pe(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                raise ValueError
            offset = int.from_bytes(header[0x3C:0x40], "little")
            if offset < 64:
                raise ValueError
            stream.seek(offset)
            coff = stream.read(6)
    except (OSError, ValueError) as exc:
        raise FreeCADDiscoveryError(
            "FREECADCMD_EXECUTABLE_INVALID",
            "FreeCADCmd must be a regular 64-bit Windows PE executable",
        ) from exc
    if len(coff) != 6 or coff[:4] != b"PE\0\0" or int.from_bytes(coff[4:6], "little") != _AMD64_MACHINE:
        raise FreeCADDiscoveryError(
            "FREECADCMD_EXECUTABLE_INVALID",
            "FreeCADCmd must be a regular 64-bit Windows PE executable",
        )


def run_freecad_version(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(path), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )


def _validate_freecadcmd_candidate(
    path: Path,
    *,
    source: str,
    run_version: Callable[[Path], subprocess.CompletedProcess[str]],
    require_x64_pe: bool,
) -> FreeCADCandidate:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise FreeCADDiscoveryError(
            "FREECADCMD_PATH_INVALID",
            "FreeCADCmd must use an absolute local path",
        )
    try:
        managed = validate_managed_path(requested, allow_missing_leaf=False)
    except (OSError, SecureFilesystemError) as exc:
        raise FreeCADDiscoveryError(
            getattr(exc, "code", "FREECADCMD_PATH_INVALID"),
            "FreeCADCmd path is not a safe local fixed executable",
        ) from exc
    if managed.identity is None or not managed.path.is_file():
        raise FreeCADDiscoveryError(
            "FREECADCMD_EXECUTABLE_INVALID",
            "FreeCADCmd must be a regular local executable",
        )
    if require_x64_pe:
        _require_x64_pe(managed.path)
    try:
        result = run_version(managed.path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FreeCADDiscoveryError(
            "FREECADCMD_VERSION_UNAVAILABLE",
            "FreeCADCmd version probe failed",
        ) from exc
    if result.returncode != 0:
        raise FreeCADDiscoveryError(
            "FREECADCMD_VERSION_UNAVAILABLE",
            "FreeCADCmd version probe failed",
        )
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    match = _FREECAD_VERSION.search(output)
    if match is None:
        raise FreeCADDiscoveryError(
            "FREECADCMD_VERSION_INVALID",
            "FreeCADCmd returned no parseable version",
        )
    return FreeCADCandidate(
        path=managed.path,
        source=source,
        identity=managed.identity,
        version=match.group(1),
    )


def validate_freecadcmd(
    path: Path,
    *,
    source: str,
    run_version: Callable[[Path], subprocess.CompletedProcess[str]],
) -> FreeCADCandidate:
    """Validate the Windows FreeCADCmd executable and capture its exact version."""
    return _validate_freecadcmd_candidate(
        path,
        source=source,
        run_version=run_version,
        require_x64_pe=True,
    )


def validate_local_freecadcmd(
    path: Path,
    *,
    source: str,
    run_version: Callable[[Path], subprocess.CompletedProcess[str]],
) -> FreeCADCandidate:
    """Validate a non-Windows local FreeCADCmd and capture its exact version."""
    return _validate_freecadcmd_candidate(
        path,
        source=source,
        run_version=run_version,
        require_x64_pe=False,
    )


def discover_freecadcmd(
    *,
    environ: Mapping[str, str],
    registry: RegistryReader,
    run_version: Callable[[Path], subprocess.CompletedProcess[str]],
    find_on_path: Callable[[str, str], str | None] = lambda name, path: shutil.which(
        name, path=path
    ),
    inspect_candidate: Callable[
        [Path, str, Callable[[Path], subprocess.CompletedProcess[str]]],
        FreeCADCandidate,
    ] = lambda path, source, runner: validate_freecadcmd(
        path, source=source, run_version=runner
    ),
) -> FreeCADDiscoveryResult:
    candidates: list[FreeCADCandidate] = []
    identities: set[FileIdentity] = set()
    for path, source in _candidate_paths(
        environ=environ,
        registry=registry,
        find_on_path=find_on_path,
    ):
        try:
            inspected = inspect_candidate(path, source, run_version)
        except (FreeCADDiscoveryError, OSError):
            continue
        if inspected.identity in identities:
            continue
        identities.add(inspected.identity)
        candidates.append(inspected)
    values = tuple(candidates)
    conflict = len(values) > 1
    return FreeCADDiscoveryResult(
        selected=values[0] if len(values) == 1 else None,
        candidates=values,
        conflict=conflict,
    )


def default_windows_discovery(
    environ: Mapping[str, str],
) -> FreeCADDiscoveryResult:
    return discover_freecadcmd(
        environ=environ,
        registry=WindowsUninstallRegistry(),
        run_version=run_freecad_version,
    )
