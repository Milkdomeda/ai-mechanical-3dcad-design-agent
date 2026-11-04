from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import subprocess
from types import SimpleNamespace

import pytest

from mechanical_design_agent.freecad_process import freecad_environment, native_process_path


@pytest.mark.parametrize("value, expected", [
    (r"\\?\C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe", r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe"),
    (r"\\?\D:\Mechanical Design 中文\model.FCStd", r"D:\Mechanical Design 中文\model.FCStd"),
    (r"C:\FreeCAD\bin\FreeCADCmd.exe", r"C:\FreeCAD\bin\FreeCADCmd.exe"),
    ("argument with spaces", "argument with spaces"),
])
def test_windows_native_paths(value: str, expected: str) -> None:
    assert native_process_path(value, platform="win32") == expected


@pytest.mark.parametrize("value", [
    r"\\server\share\model.FCStd", r"\\?\UNC\server\share\model.FCStd",
    r"\\.\C:\model.FCStd", r"\\?\Volume{abc}\model.FCStd",
    r"\\?\C:\folder.\model.FCStd", r"\\?\C:\folder \model.FCStd",
    r"\\?\C:\folder\..\model.FCStd",
])
def test_native_translation_rejects_network_device_and_ambiguous_paths(value: str) -> None:
    with pytest.raises(ValueError):
        native_process_path(value, platform="win32")


def test_posix_path_and_environment_remain_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/unreviewed/path")
    monkeypatch.setenv("PYTHONPATH", "/unreviewed/modules")
    monkeypatch.setenv("MECH_DESIGN_DATABASE_URL", "synthetic-secret")
    home = Path("/controlled/attempt")
    env = freecad_environment(Path("/selected/FreeCADCmd"), home, platform="darwin")
    assert env["PATH"] == os.defpath
    assert env["HOME"] == str(home)
    assert "PYTHONPATH" not in env
    assert "MECH_DESIGN_DATABASE_URL" not in env
    assert native_process_path(r"\\?\C:\example", platform="darwin") == r"\\?\C:\example"


def test_windows_environment_uses_selected_runtime_and_system_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("PATH", r"Z:\unreviewed;.;C:\bin")
    monkeypatch.setenv("PYTHONHOME", r"Z:\other-python")
    monkeypatch.setenv("QT_PLUGIN_PATH", r"Z:\other-qt")
    monkeypatch.setenv("NEO4J_PASSWORD", "synthetic-secret")
    env = freecad_environment(
        r"\\?\C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
        r"\\?\D:\Design workspace 中文\attempt",
        platform="win32",
    )
    paths = env["PATH"].split(";")
    assert paths[0] == r"C:\Program Files\FreeCAD 1.1\bin"
    assert r"C:\Windows\System32" in paths
    assert r"C:\Windows" in paths
    assert all(path and path != "." and not path.startswith("Z:") for path in paths)
    for key in ("HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP", "FREECAD_USER_HOME", "FREECAD_USER_DATA", "FREECAD_USER_TEMP"):
        assert env[key] == r"D:\Design workspace 中文\attempt"
    for key in ("PYTHONHOME", "QT_PLUGIN_PATH", "NEO4J_PASSWORD"):
        assert key not in env


def test_runner_translates_every_native_path_and_keeps_both_pin_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    import mechanical_design_agent.freecad_runner as runner
    executable = PureWindowsPath(r"\\?\C:\FreeCAD tools\bin\FreeCADCmd.exe")
    script = PureWindowsPath(r"\\?\D:\package 中文\seed.py")
    controlled = PureWindowsPath(r"\\?\D:\workspace 中文\attempt")
    destination = controlled / "model.FCStd"
    pins = []
    calls = []
    monkeypatch.setattr(runner, "validate_managed_path", lambda *a, **k: SimpleNamespace(path=controlled))
    monkeypatch.setattr(runner, "_pin_executable", lambda path, **k: pins.append(str(path)))
    monkeypatch.setattr(runner, "native_process_path", lambda path: native_process_path(path, platform="win32"))
    monkeypatch.setattr(runner, "freecad_environment", lambda exe, home: freecad_environment(exe, home, platform="win32"))

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", execute)
    runner.run_freecad_script(executable, script, [destination, "literal argument"],
        timeout_seconds=3, expected_sha256="a" * 64, expected_identity=object(), controlled_directory=controlled)
    argv, options = calls[0]
    assert argv[2].isascii(), "Windows FreeCAD console transport must carry ASCII escapes"
    assert argv[0] == r"C:\FreeCAD tools\bin\FreeCADCmd.exe"
    assert options["cwd"] == r"D:\workspace 中文\attempt"
    assert options["env"]["HOME"] == options["cwd"]
    assert "\\\\\\\\?\\\\" not in argv[2]
    assert "literal argument" in argv[2]
    assert pins[0] == pins[-1] == str(executable)
    assert pins[1] == argv[0]


def test_version_probe_shares_native_paths_and_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    import mechanical_design_agent.freecad_discovery as discovery
    controlled = r"\\?\D:\probe 中文"
    monkeypatch.setattr(discovery.tempfile, "TemporaryDirectory", lambda **k: nullcontext(controlled))
    monkeypatch.setattr(discovery, "native_process_path", lambda path: native_process_path(path, platform="win32"))
    monkeypatch.setattr(discovery, "freecad_environment", lambda exe, home: freecad_environment(exe, home, platform="win32"))
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "FreeCAD 1.1.3", "")

    monkeypatch.setattr(discovery.subprocess, "run", execute)
    discovery.run_freecad_version(PureWindowsPath(r"\\?\C:\FreeCAD\bin\FreeCADCmd.exe"))
    argv, options = calls[0]
    assert argv == [r"C:\FreeCAD\bin\FreeCADCmd.exe", "--version"]
    assert options["cwd"] == r"D:\probe 中文"
    assert options["env"]["PATH"].split(";")[0] == r"C:\FreeCAD\bin"
