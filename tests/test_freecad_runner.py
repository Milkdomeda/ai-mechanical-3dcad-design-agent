from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from mechanical_design_agent.freecad_runner import (
    FreeCADExecutableTrustError,
    run_freecad_script,
)
from mechanical_design_agent.secure_fs import read_managed_file


def test_runner_scrubs_environment_and_isolates_process_inside_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "FreeCAD Cmd"
    executable.write_bytes(b"reviewed official boundary")
    script = tmp_path / "script.py"
    script.write_text("pass\n", encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    pinned = read_managed_file(executable)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setenv("MECH_DESIGN_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_freecad_script(
        executable,
        script,
        [],
        timeout_seconds=3,
        expected_sha256=pinned.sha256,
        expected_identity=pinned.identity,
        controlled_directory=attempt,
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "MECH_DESIGN_DATABASE_URL" not in environment
    assert "NEO4J_PASSWORD" not in environment
    assert environment["HOME"] == str(attempt)
    assert environment["TMPDIR"] == str(attempt)
    assert captured["cwd"] == attempt


def test_runner_rejects_substituted_executable_after_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "FreeCADCmd"
    executable.write_bytes(b"reviewed")
    script = tmp_path / "script.py"
    script.write_text("pass\n", encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    pinned = read_managed_file(executable)

    def substitute(*args, **kwargs):
        executable.unlink()
        executable.write_bytes(b"wrapper")
        return subprocess.CompletedProcess(args[0], 0, "forged", "")

    monkeypatch.setattr(subprocess, "run", substitute)
    with pytest.raises(FreeCADExecutableTrustError):
        run_freecad_script(
            executable,
            script,
            [],
            timeout_seconds=3,
            expected_sha256=pinned.sha256,
            expected_identity=pinned.identity,
            controlled_directory=attempt,
        )
