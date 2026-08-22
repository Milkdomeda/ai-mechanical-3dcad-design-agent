from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from mechanical_design_agent.freecad_discovery import (
    CERTIFIED_FREECADCMD_VERSIONS,
    FreeCADCandidate,
    FreeCADDiscoveryError,
    RegistryEntry,
    WindowsUninstallRegistry,
    discover_freecadcmd,
    run_freecad_version,
    validate_freecadcmd,
)
from mechanical_design_agent.secure_fs import FileIdentity, ManagedPath


class FakeRegistry:
    def __init__(self, entries: tuple[RegistryEntry, ...]) -> None:
        self._entries = entries

    def entries(self) -> tuple[RegistryEntry, ...]:
        return self._entries


def candidate(
    path: Path,
    source: str,
    *,
    volume: int,
    index: int,
    version: str = "1.1.1",
) -> FreeCADCandidate:
    return FreeCADCandidate(
        path=path,
        source=source,
        identity=FileIdentity(volume=volume, file_index=index),
        version=version,
    )


def test_registry_discovery_is_limited_to_the_three_approved_uninstall_roots() -> None:
    assert WindowsUninstallRegistry._ROOTS == (
        (
            "HKLM64",
            "HKEY_LOCAL_MACHINE",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            "HKLM32",
            "HKEY_LOCAL_MACHINE",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            "HKCU",
            "HKEY_CURRENT_USER",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    )


def test_certified_versions_are_an_explicit_two_version_allowlist() -> None:
    assert CERTIFIED_FREECADCMD_VERSIONS == frozenset({"1.1.1", "1.1.3"})
    assert "1.1.2" not in CERTIFIED_FREECADCMD_VERSIONS


def test_discovery_is_bounded_and_registry_names_are_filtered() -> None:
    program_files = Path("C:/Program Files")
    path_executable = Path("C:/Tools/FreeCADCmd.exe")
    registry_location = Path("D:/Applications/FreeCAD")
    inspected: list[tuple[Path, str]] = []

    def inspect(path: Path, source: str, _run_version) -> FreeCADCandidate:
        inspected.append((path, source))
        return candidate(path, source, volume=1, index=len(inspected))

    result = discover_freecadcmd(
        environ={"PATH": "bounded-path", "ProgramFiles": str(program_files)},
        registry=FakeRegistry(
            (
                RegistryEntry("FreeCAD", registry_location, "HKLM64"),
                RegistryEntry(" freecad 1.1.1 ", registry_location, "HKCU"),
                RegistryEntry("FreeCAD-MCP", Path("D:/Rejected/MCP"), "HKCU"),
                RegistryEntry("Another CAD", Path("D:/Rejected/Other"), "HKLM64"),
            )
        ),
        run_version=lambda _path: subprocess.CompletedProcess([], 0, "FreeCAD 1.1.1", ""),
        find_on_path=lambda name, path: str(path_executable)
        if (name, path) == ("FreeCADCmd.exe", "bounded-path")
        else None,
        inspect_candidate=inspect,
    )

    assert inspected == [
        (path_executable, "path"),
        (program_files / "FreeCAD 1.1/bin/FreeCADCmd.exe", "program_files:FreeCAD 1.1"),
        (
            program_files / "FreeCAD 1.1.1/bin/FreeCADCmd.exe",
            "program_files:FreeCAD 1.1.1",
        ),
        (
            program_files / "FreeCAD 1.1.3/bin/FreeCADCmd.exe",
            "program_files:FreeCAD 1.1.3",
        ),
        (registry_location / "bin/FreeCADCmd.exe", "registry:HKLM64"),
        (registry_location / "bin/FreeCADCmd.exe", "registry:HKCU"),
    ]
    assert result.conflict is True
    assert result.selected is None
    assert len(result.candidates) == 6


def test_discovery_deduplicates_aliases_by_file_identity_before_conflict() -> None:
    identity = FileIdentity(volume=7, file_index=99)

    def inspect(path: Path, source: str, _run_version) -> FreeCADCandidate:
        return FreeCADCandidate(path, source, identity, "1.1.1")

    result = discover_freecadcmd(
        environ={"PATH": "one", "ProgramFiles": "C:/Program Files"},
        registry=FakeRegistry(
            (RegistryEntry("FreeCAD 1.1.1", Path("D:/FreeCAD"), "HKCU"),)
        ),
        run_version=lambda _path: subprocess.CompletedProcess([], 0, "FreeCAD 1.1.1", ""),
        find_on_path=lambda _name, _path: "C:/Alias/FreeCADCmd.exe",
        inspect_candidate=inspect,
    )

    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.identity == identity
    assert result.candidates == (result.selected,)


def test_discovery_rejects_invalid_candidates_and_never_selects_by_sort_order() -> None:
    accepted = {
        Path("C:/Program Files/FreeCAD 1.1/bin/FreeCADCmd.exe"): candidate(
            Path("C:/Program Files/FreeCAD 1.1/bin/FreeCADCmd.exe"),
            "program_files:FreeCAD 1.1",
            volume=1,
            index=1,
        ),
        Path("C:/Program Files/FreeCAD 1.1.1/bin/FreeCADCmd.exe"): candidate(
            Path("C:/Program Files/FreeCAD 1.1.1/bin/FreeCADCmd.exe"),
            "program_files:FreeCAD 1.1.1",
            volume=1,
            index=2,
        ),
    }

    def inspect(path: Path, source: str, _run_version) -> FreeCADCandidate:
        if path not in accepted:
            raise FreeCADDiscoveryError("FREECADCMD_INVALID", "invalid candidate")
        return accepted[path]

    result = discover_freecadcmd(
        environ={"PATH": "one", "ProgramFiles": "C:/Program Files"},
        registry=FakeRegistry(()),
        run_version=lambda _path: subprocess.CompletedProcess([], 0, "FreeCAD 1.1.1", ""),
        find_on_path=lambda _name, _path: "C:/Rejected/FreeCADCmd.exe",
        inspect_candidate=inspect,
    )

    assert result.conflict is True
    assert result.selected is None
    assert tuple(item.identity.file_index for item in result.candidates) == (1, 2)


def _pe_x64() -> bytes:
    value = bytearray(256)
    value[:2] = b"MZ"
    value[0x3C:0x40] = (128).to_bytes(4, "little")
    value[128:132] = b"PE\0\0"
    value[132:134] = (0x8664).to_bytes(2, "little")
    return bytes(value)


def test_explicit_candidate_requires_absolute_x64_pe_and_parses_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "Portable FreeCAD 空间" / "FreeCADCmd.exe"
    executable.parent.mkdir()
    executable.write_bytes(_pe_x64())
    identity = FileIdentity(volume=5, file_index=11)
    monkeypatch.setattr(
        "mechanical_design_agent.freecad_discovery.validate_managed_path",
        lambda path, allow_missing_leaf: ManagedPath(Path(path), identity, tmp_path),
    )

    inspected = validate_freecadcmd(
        executable,
        source="runtime",
        run_version=lambda path: subprocess.CompletedProcess(
            [str(path), "--version"], 0, "FreeCAD 1.1.1\n", ""
        ),
    )

    assert inspected == FreeCADCandidate(executable, "runtime", identity, "1.1.1")

    relative = Path("FreeCADCmd.exe")
    with pytest.raises(FreeCADDiscoveryError, match="absolute"):
        validate_freecadcmd(
            relative,
            source="runtime",
            run_version=lambda _path: subprocess.CompletedProcess([], 0, "FreeCAD 1.1.1", ""),
        )

    executable.write_bytes(b"not a PE")
    with pytest.raises(FreeCADDiscoveryError, match="64-bit Windows PE"):
        validate_freecadcmd(
            executable,
            source="runtime",
            run_version=lambda _path: subprocess.CompletedProcess([], 0, "FreeCAD 1.1.1", ""),
        )


def test_version_runner_uses_argv_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "FreeCAD 1.1.1\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    executable = Path("C:/Program Files/FreeCAD 1.1/bin/FreeCADCmd.exe")

    result = run_freecad_version(executable)

    assert result.returncode == 0
    assert calls[0][0] == [str(executable), "--version"]
    assert calls[0][1]["timeout"] == 5
    assert calls[0][1].get("shell", False) is False
