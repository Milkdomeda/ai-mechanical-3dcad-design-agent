from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from mechanical_design_agent.freecad_runner import (
    FreeCADExecutableTrustError,
    _strip_freecad_progress_output,
    run_freecad_script,
)
from mechanical_design_agent.secure_fs import read_managed_file, same_managed_path


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
    assert same_managed_path(Path(environment["HOME"]), attempt)
    assert same_managed_path(Path(environment["TMPDIR"]), attempt)
    assert same_managed_path(Path(captured["cwd"]), attempt)


def test_runner_uses_noninteractive_console_argument_and_closed_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "FreeCAD Cmd"
    executable.write_bytes(b"reviewed official boundary")
    script = tmp_path / "script with spaces.py"
    script.write_text("print('evidence')\n", encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    pinned = read_managed_file(executable)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "evidence\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_freecad_script(
        executable,
        script,
        ["argument with spaces"],
        timeout_seconds=3,
        expected_sha256=pinned.sha256,
        expected_identity=pinned.identity,
        controlled_directory=attempt,
    )

    invocation = captured["args"]
    assert isinstance(invocation, tuple)
    command = invocation[0]
    assert command[:2] == [str(executable), "-c"]
    assert len(command) == 3
    assert "sys.argv =" in command[2]
    assert str(script) in command[2]
    assert "argument with spaces" in command[2]
    assert captured["stdin"] is subprocess.DEVNULL
    assert "input" not in captured


def test_runner_strips_only_complete_freecad_progress_blocks() -> None:
    evidence = "MECHANICAL_DESIGN_EVIDENCE {\"status\":\"valid\"}\n"
    progress = (
        "Importing project files......\n"
        "\t\t\t\t\t\t(100 %)\t\n"
        "\t\t\t\t\t\t\t\t\n"
        "Postprocessing......\n"
        "\t\t\t\t\t\t(100 %)\t\n"
        "\t\t\t\t\t\t\t\t\n"
        "Recompute......\n"
        "\t\t\t\t\t\t(0 %)\t\n"
        "\t\t\t\t\t\t(50 %)\t\n"
        "\t\t\t\t\t\t(100 %)\t\n"
        "\t\t\t\t\t\t\t\t\n"
    )

    assert _strip_freecad_progress_output(evidence + progress) == evidence


@pytest.mark.parametrize(
    "diagnostic",
    (
        "Recompute......\n\t\t\t\t\t\t(99 %)\t\n\t\t\t\t\t\t\t\t\n",
        "Recompute......\n\t\t\t\t\t\t(101 %)\t\n\t\t\t\t\t\t\t\t\n",
        "Recompute......\n\t\t\t\t\t\t(100 %)\t\nunexpected\n",
        "Unexpected diagnostic\n",
    ),
)
def test_runner_preserves_incomplete_or_unrecognized_diagnostics(
    diagnostic: str,
) -> None:
    assert _strip_freecad_progress_output(diagnostic) == diagnostic


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
