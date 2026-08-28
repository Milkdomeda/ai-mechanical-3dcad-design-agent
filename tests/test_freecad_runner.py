from __future__ import annotations

import ast
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

    injection_like_argument = (
        "quote' double\" backslash\\ newline\n$(touch should-not-run); & |"
    )

    run_freecad_script(
        executable,
        script,
        ["argument with spaces", injection_like_argument],
        timeout_seconds=3,
        expected_sha256=pinned.sha256,
        expected_identity=pinned.identity,
        controlled_directory=attempt,
    )

    invocation = captured["args"]
    assert isinstance(invocation, tuple)
    command = invocation[0]
    assert isinstance(command, list)
    assert command[:2] == [str(executable), "-c"]
    assert len(command) == 3

    outer = ast.parse(command[2], mode="exec")
    assert len(outer.body) == 1
    outer_expression = outer.body[0]
    assert isinstance(outer_expression, ast.Expr)
    outer_call = outer_expression.value
    assert isinstance(outer_call, ast.Call)
    assert isinstance(outer_call.func, ast.Name)
    assert outer_call.func.id == "exec"
    assert len(outer_call.args) == 1
    runner_source = ast.literal_eval(outer_call.args[0])
    assert isinstance(runner_source, str)

    runner = ast.parse(runner_source, mode="exec")
    argv_assignment = next(
        node
        for node in runner.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "sys"
        and node.targets[0].attr == "argv"
    )
    script_assignment = next(
        node
        for node in runner.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_script"
    )
    assert ast.literal_eval(argv_assignment.value) == [
        str(script),
        "argument with spaces",
        injection_like_argument,
    ]
    assert ast.literal_eval(script_assignment.value) == str(script)
    assert captured["stdin"] is subprocess.DEVNULL
    assert "input" not in captured
    assert "shell" not in captured


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
