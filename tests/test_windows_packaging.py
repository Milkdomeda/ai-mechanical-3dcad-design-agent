from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import platform
import subprocess
import struct
import sys
import tempfile
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from mechanical_design_agent.server import create_mcp

# Pytest collects this repository's flat tests directory as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_release_helpers as release_helpers  # noqa: E402
from windows_release_helpers import (  # noqa: E402
    REQUIRED_INSTALLED_RESOURCES,
    build_release_artifacts,
    clean_release_environment,
    create_installed_wheel_environment,
    inspect_installed_resources,
    inspect_release_archives,
    normalized_archive_names,
    run_checked,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_backend_is_exactly_pinned_for_release_reproducibility() -> None:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["build-system"]["requires"] == ["hatchling==1.32.0"]


def test_windows_archive_member_names_are_normalized_before_allowlist_checks() -> None:
    assert normalized_archive_names(
        [r"distribution\docs\ARCHITECTURE.md", "distribution/LICENSE"]
    ) == ("distribution/docs/ARCHITECTURE.md", "distribution/LICENSE")


def test_run_checked_decodes_utf8_output_independent_of_windows_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_subprocess_run(command: list[str], **kwargs: object):
        if kwargs.get("encoding") != "utf-8" or kwargs.get("errors") != "replace":
            raise UnicodeDecodeError(
                "gbk",
                "依赖安装完成".encode(),
                1,
                2,
                "synthetic Windows locale mismatch",
            )
        return subprocess.CompletedProcess(command, 0, "依赖安装完成", "")

    monkeypatch.setattr(release_helpers.subprocess, "run", fake_subprocess_run)

    result = run_checked(
        ["uv", "pip", "install", "--offline", "fixture.whl"],
        cwd=tmp_path,
        environment={},
    )

    assert result.stdout == "依赖安装完成"


def test_run_checked_reports_nonzero_utf8_output_without_decode_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_subprocess_run(command: list[str], **kwargs: object):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(command, 2, "", "离线安装失败")

    monkeypatch.setattr(release_helpers.subprocess, "run", fake_subprocess_run)

    with pytest.raises(AssertionError, match="离线安装失败"):
        run_checked(
            ["uv", "pip", "install", "--offline", "fixture.whl"],
            cwd=tmp_path,
            environment={},
        )


def test_w4_safe_failure_context_redacts_subprocess_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_output = (
        r"failed at C:\Users\Synthetic User\cache with password=hunter2 "
        "https://private.example/simple: no matching distribution found"
    )

    def fake_subprocess_run(command: list[str], **kwargs: object):
        return subprocess.CompletedProcess(command, 2, "", secret_output)

    monkeypatch.setattr(release_helpers.subprocess, "run", fake_subprocess_run)

    with pytest.raises(release_helpers.SafeSubprocessError) as captured:
        run_checked(
            ["uv", "pip", "install", "--offline", "fixture.whl"],
            cwd=tmp_path,
            environment={},
            safe_stage="W4_LIVE_OFFLINE_PROJECT_INSTALL",
        )

    error = captured.value
    assert error.stage == "W4_LIVE_OFFLINE_PROJECT_INSTALL"
    assert error.returncode == 2
    assert error.indicator == "offline_cache_miss"
    rendered = str(error)
    assert "Synthetic User" not in rendered
    assert "hunter2" not in rendered
    assert "private.example" not in rendered
    assert "C:\\" not in rendered


def test_w4_safe_failure_context_classifies_timeout_without_raw_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_subprocess_run(command: list[str], **kwargs: object):
        raise subprocess.TimeoutExpired(
            command,
            37,
            output=r"C:\Users\Synthetic User\private output",
            stderr="password=hunter2",
        )

    monkeypatch.setattr(release_helpers.subprocess, "run", fake_subprocess_run)

    with pytest.raises(release_helpers.SafeSubprocessError) as captured:
        run_checked(
            ["uv", "build", "--offline"],
            cwd=tmp_path,
            environment={},
            timeout=37,
            safe_stage="W4_CACHE_OFFLINE_BUILD",
        )

    error = captured.value
    assert error.stage == "W4_CACHE_OFFLINE_BUILD"
    assert error.timeout_seconds == 37
    assert error.indicator == "timeout"
    assert "Synthetic User" not in str(error)
    assert "hunter2" not in str(error)


def test_run_checked_without_w4_safe_context_preserves_legacy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_subprocess_run(command: list[str], **kwargs: object):
        return subprocess.CompletedProcess(command, 9, "", "legacy raw detail")

    monkeypatch.setattr(release_helpers.subprocess, "run", fake_subprocess_run)

    with pytest.raises(AssertionError, match="legacy raw detail"):
        run_checked(["legacy-tool"], cwd=tmp_path, environment={})


def test_clean_release_environment_removes_inherited_version_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MECH_DESIGN_FREECADCMD_EXPECTED_VERSION", "untrusted")

    environment = clean_release_environment(tmp_path)

    assert "MECH_DESIGN_FREECADCMD_EXPECTED_VERSION" not in environment


def test_clean_release_environment_uses_only_explicit_prepared_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_root = tmp_path / "default"
    explicit_root = tmp_path / "explicit"
    prepared_cache = tmp_path / "prepared uv cache"
    default_root.mkdir()
    explicit_root.mkdir()
    prepared_cache.mkdir()
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "untrusted inherited cache"))

    default_environment = clean_release_environment(default_root)
    explicit_environment = clean_release_environment(
        explicit_root,
        uv_cache_dir=prepared_cache,
    )

    assert default_environment["UV_CACHE_DIR"] == str(default_root / "uv-cache")
    assert explicit_environment["UV_CACHE_DIR"] == str(prepared_cache.resolve(strict=True))


@pytest.mark.parametrize("invalid_kind", ["missing", "file", "symlink"])
def test_clean_release_environment_rejects_invalid_prepared_cache(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    root = tmp_path / "release root"
    root.mkdir()
    invalid = tmp_path / "invalid cache"
    if invalid_kind == "file":
        invalid.write_text("not a directory", encoding="utf-8")
    elif invalid_kind == "symlink":
        target = tmp_path / "cache target"
        target.mkdir()
        invalid.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="prepared UV cache"):
        clean_release_environment(root, uv_cache_dir=invalid)

    assert not (root / "isolated-home").exists()


def test_clean_wheel_install_has_bounded_slow_network_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release root"
    outside = root / "outside"
    root.mkdir()
    outside.mkdir()
    wheel = root / "agent.whl"
    wheel.write_bytes(b"synthetic wheel")
    observed_timeouts: list[int] = []
    observed_commands: list[list[str]] = []

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        expected_returncode: int = 0,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment, expected_returncode
        observed_commands.append(list(command))
        observed_timeouts.append(timeout)
        if command[1] == "venv":
            venv = Path(command[-1])
            scripts = venv / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            for name in (
                "python.exe" if os.name == "nt" else "python",
                "mechanical-design.exe" if os.name == "nt" else "mechanical-design",
                (
                    "mechanical-design-mcp.exe"
                    if os.name == "nt"
                    else "mechanical-design-mcp"
                ),
            ):
                (scripts / name).write_bytes(b"fixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_helpers.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(release_helpers, "run_checked", fake_run_checked)

    release_helpers.create_installed_wheel_environment(
        wheel=wheel,
        root=root,
        outside=outside,
        environment={},
    )

    assert observed_timeouts == [300, 900]
    assert "--offline" not in observed_commands[1]


def test_clean_wheel_install_offline_mode_never_uses_package_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release root"
    outside = root / "outside"
    root.mkdir()
    outside.mkdir()
    wheel = root / "agent.whl"
    wheel.write_bytes(b"synthetic wheel")
    observed_commands: list[list[str]] = []

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        expected_returncode: int = 0,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment, expected_returncode, timeout
        observed_commands.append(list(command))
        if command[1] == "venv":
            scripts = Path(command[-1]) / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            for name in (
                "python.exe" if os.name == "nt" else "python",
                "mechanical-design.exe" if os.name == "nt" else "mechanical-design",
                "mechanical-design-mcp.exe" if os.name == "nt" else "mechanical-design-mcp",
            ):
                (scripts / name).write_bytes(b"fixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_helpers.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(release_helpers, "run_checked", fake_run_checked)

    release_helpers.create_installed_wheel_environment(
        wheel=wheel,
        root=root,
        outside=outside,
        environment={"UV_CACHE_DIR": str(tmp_path / "prepared cache")},
        offline=True,
    )

    assert observed_commands[1][1:4] == ["pip", "install", "--offline"]


def test_w3_cache_preparation_warms_locked_dependencies_then_proves_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "preparation root"
    outside = root / "outside checkout"
    root.mkdir()
    outside.mkdir()
    prepared_cache = tmp_path / "prepared uv cache"
    prepared_cache.mkdir()
    environment = {"UV_CACHE_DIR": str(prepared_cache)}
    observed_commands: list[list[str]] = []
    install_environments: list[dict[str, str]] = []

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        expected_returncode: int = 0,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, expected_returncode, timeout
        observed_commands.append(list(command))
        if command[1] == "build":
            dist = Path(command[command.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "agent.whl").write_bytes(b"synthetic wheel")
            (dist / "agent.tar.gz").write_bytes(b"synthetic sdist")
        elif command[1] == "export":
            exported = Path(command[command.index("--output-file") + 1])
            exported.write_text("mcp[cli]==1.29.0\n", encoding="utf-8")
        elif command[1] == "venv":
            scripts = Path(command[-1]) / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            for name in (
                "python.exe" if os.name == "nt" else "python",
                "mechanical-design.exe" if os.name == "nt" else "mechanical-design",
                "mechanical-design-mcp.exe" if os.name == "nt" else "mechanical-design-mcp",
            ):
                (scripts / name).write_bytes(b"fixture")
        elif command[1:3] == ["pip", "install"]:
            install_environments.append(dict(environment))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_helpers.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(release_helpers, "run_checked", fake_run_checked)

    installed = release_helpers.prepare_and_verify_offline_project_wheel_cache(
        project_root=PROJECT_ROOT,
        root=root,
        outside=outside,
        environment=environment,
    )

    install_commands = [
        command for command in observed_commands if command[1:3] == ["pip", "install"]
    ]
    build_commands = [command for command in observed_commands if command[1] == "build"]
    assert len(build_commands) == 2
    assert "--offline" not in build_commands[0]
    assert "--offline" in build_commands[1]

    assert len(install_commands) == 4
    assert "--offline" not in install_commands[0]
    assert "--constraint" in install_commands[0]
    assert "--offline" not in install_commands[1]
    assert "pytest==9.1.1" in install_commands[1]
    assert "jsonschema==4.26.0" in install_commands[1]
    assert install_commands[2][1:4] == ["pip", "install", "--offline"]
    assert "--constraint" not in install_commands[2]
    assert install_commands[3][1:4] == ["pip", "install", "--offline"]
    assert "pytest>=8.3.0,<10" in install_commands[3]
    assert "jsonschema>=4.23.0,<5" in install_commands[3]
    assert install_commands[0][install_commands[0].index("--python") + 1] != str(
        installed.python
    )
    assert install_commands[1][install_commands[1].index("--python") + 1] != str(
        installed.python
    )
    assert install_commands[2][install_commands[2].index("--python") + 1] == str(
        installed.python
    )
    assert install_commands[3][install_commands[3].index("--python") + 1] == str(
        installed.python
    )
    assert [item["UV_CACHE_DIR"] for item in install_environments] == [
        str(prepared_cache),
        str(prepared_cache),
        str(prepared_cache),
        str(prepared_cache),
    ]
    assert any(command[-1] == "1.29.0" for command in observed_commands)
    assert any(command[-2:] == ["9.1.1", "4.26.0"] for command in observed_commands)


def test_w4_cache_preparation_uses_bound_python_same_cache_and_safe_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "w4 preparation root"
    outside = root / "outside checkout"
    root.mkdir()
    outside.mkdir()
    prepared_cache = tmp_path / "prepared uv cache"
    prepared_cache.mkdir()
    environment = {"UV_CACHE_DIR": str(prepared_cache)}
    observed_commands: list[list[str]] = []
    observed_stages: list[str | None] = []
    observed_caches: list[str] = []

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        expected_returncode: int = 0,
        timeout: int = 120,
        safe_stage: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, expected_returncode, timeout
        observed_commands.append(list(command))
        observed_stages.append(safe_stage)
        observed_caches.append(environment["UV_CACHE_DIR"])
        if command[1] == "build":
            dist = Path(command[command.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "agent.whl").write_bytes(b"synthetic wheel")
            (dist / "agent.tar.gz").write_bytes(b"synthetic sdist")
        elif command[1] == "export":
            exported = Path(command[command.index("--output-file") + 1])
            exported.write_text("mcp[cli]==1.29.0\n", encoding="utf-8")
        elif command[1] == "venv":
            scripts = Path(command[-1]) / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            for name in (
                "python.exe" if os.name == "nt" else "python",
                "mechanical-design.exe" if os.name == "nt" else "mechanical-design",
                "mechanical-design-mcp.exe" if os.name == "nt" else "mechanical-design-mcp",
            ):
                (scripts / name).write_bytes(b"fixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_helpers.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(release_helpers, "run_checked", fake_run_checked)

    release_helpers.prepare_and_verify_offline_project_wheel_cache(
        project_root=PROJECT_ROOT,
        root=root,
        outside=outside,
        environment=environment,
        safe_context="W4",
    )

    build_commands = [command for command in observed_commands if command[1] == "build"]
    install_commands = [
        command for command in observed_commands if command[1:3] == ["pip", "install"]
    ]
    venv_commands = [command for command in observed_commands if command[1] == "venv"]
    assert "--offline" not in build_commands[0]
    assert "--offline" in build_commands[1]
    assert "--offline" not in install_commands[0]
    assert install_commands[2][1:4] == ["pip", "install", "--offline"]
    assert all(command[command.index("--python") + 1] == sys.executable for command in venv_commands)
    assert set(observed_caches) == {str(prepared_cache)}
    assert {
        "W4_CACHE_ONLINE_BUILD",
        "W4_CACHE_LOCK_EXPORT",
        "W4_CACHE_WARMUP_VENV",
        "W4_CACHE_WARMUP_PROJECT_INSTALL",
        "W4_CACHE_WARMUP_TEST_INSTALL",
        "W4_CACHE_OFFLINE_BUILD",
        "W4_CACHE_OFFLINE_VENV",
        "W4_CACHE_OFFLINE_PROJECT_INSTALL",
        "W4_CACHE_OFFLINE_TEST_INSTALL",
    }.issubset(set(observed_stages))


def _source_mcp_contract() -> dict[str, dict[str, object]]:
    mcp = create_mcp()
    return {
        name: tool.parameters
        for name, tool in sorted(mcp._tool_manager._tools.items())
    }


async def _installed_mcp_contract(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> tuple[dict[str, dict[str, object]], dict[str, object], dict[str, object]]:
    parameters = StdioServerParameters(
        command=str(executable),
        cwd=cwd,
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            status_result = await session.call_tool(
                "design_system_status",
                {},
                read_timeout_seconds=timedelta(seconds=15),
            )
            doctor_result = await session.call_tool(
                "design_system_doctor",
                {},
                read_timeout_seconds=timedelta(seconds=15),
            )
    contract = {
        tool.name: tool.inputSchema
        for tool in sorted(listed.tools, key=lambda item: item.name)
    }
    status = json.loads(status_result.content[0].text)
    doctor = json.loads(doctor_result.content[0].text)
    return contract, status, doctor


@pytest.mark.skipif(
    os.name != "nt"
    or os.environ.get("MECH_DESIGN_WINDOWS_W3_CACHE_PREPARATION") != "1",
    reason="explicit Windows W3 locked-cache preparation gate",
)
def test_windows_w3_prepares_and_proves_project_wheel_offline_cache() -> None:
    explicit_root = os.environ.get("MECH_DESIGN_W3_ROOT", "").strip()
    prepared_cache_value = os.environ.get("UV_CACHE_DIR", "").strip()
    assert explicit_root, "MECH_DESIGN_W3_ROOT must select the isolated W3 test root"
    assert prepared_cache_value, "W3 requires the Runbook-prepared UV cache"

    gate_root = Path(explicit_root).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="w3-cache-preparation-", dir=gate_root) as value:
        root = Path(value)
        outside = root / "outside checkout"
        outside.mkdir()
        environment = clean_release_environment(
            root,
            uv_cache_dir=Path(prepared_cache_value),
        )

        installed = release_helpers.prepare_and_verify_offline_project_wheel_cache(
            project_root=PROJECT_ROOT,
            root=root,
            outside=outside,
            environment=environment,
        )

        assert installed.python.is_file()
        assert installed.cli.is_file()
        assert installed.mcp.is_file()


@pytest.mark.skipif(
    os.name != "nt"
    or os.environ.get("MECH_DESIGN_WINDOWS_W4_CACHE_PREPARATION") != "1",
    reason="explicit Windows W4 locked-cache preparation gate",
)
def test_windows_w4_prepares_and_proves_project_wheel_offline_cache() -> None:
    explicit_root = os.environ.get("MECH_DESIGN_W4_ROOT", "").strip()
    prepared_cache_value = os.environ.get("UV_CACHE_DIR", "").strip()
    assert explicit_root, "MECH_DESIGN_W4_ROOT must select the isolated W4 test root"
    assert prepared_cache_value, "W4 requires the Runbook-prepared UV cache"
    assert platform.python_implementation() == "CPython"
    assert sys.version_info[:2] == (3, 12)
    assert struct.calcsize("P") * 8 == 64

    from mechanical_design_agent.secure_fs import validate_managed_path

    gate_root = validate_managed_path(
        Path(explicit_root),
        allow_missing_leaf=False,
    ).path
    prepared_cache = validate_managed_path(
        Path(prepared_cache_value),
        allow_missing_leaf=False,
    ).path
    assert gate_root.is_dir()
    assert prepared_cache.is_dir()

    with tempfile.TemporaryDirectory(prefix="w4-cache-preparation-", dir=gate_root) as value:
        root = Path(value)
        outside = root / "outside checkout"
        outside.mkdir()
        environment = clean_release_environment(
            root,
            uv_cache_dir=prepared_cache,
        )

        installed = release_helpers.prepare_and_verify_offline_project_wheel_cache(
            project_root=PROJECT_ROOT,
            root=root,
            outside=outside,
            environment=environment,
            safe_context="W4",
        )

        assert installed.python.is_file()
        assert installed.cli.is_file()
        assert installed.mcp.is_file()
        assert inspect_installed_resources(
            python=installed.python,
            venv=installed.venv,
            cwd=outside,
            environment=environment,
        ) == {
            "all_inside_venv": True,
            "resources": sorted(REQUIRED_INSTALLED_RESOURCES),
            "version": "0.4.1",
        }


@pytest.mark.skipif(os.name != "nt", reason="native Windows installed-wheel gate")
def test_windows_clean_installed_wheel_core_contract() -> None:
    freecadcmd = os.environ.get("MECH_DESIGN_FREECADCMD", "").strip()
    if not freecadcmd:
        pytest.skip("official FreeCAD 1.1.3 FreeCADCmd is required for the W2 gate")

    explicit_root = os.environ.get("MECH_DESIGN_W2_ROOT", "").strip()
    if not explicit_root:
        pytest.skip("MECH_DESIGN_W2_ROOT must select the second fixed NTFS volume")
    expected_version = os.environ.get(
        "MECH_DESIGN_FREECADCMD_EXPECTED_VERSION", ""
    ).strip()
    assert expected_version == "1.1.3", (
        "Windows W2 requires MECH_DESIGN_FREECADCMD_EXPECTED_VERSION=1.1.3"
    )

    gate_root = Path(explicit_root).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="w2-installed-wheel-", dir=gate_root) as value:
        root = Path(value)
        outside = root / "outside checkout"
        workspace = root / "工作区 with spaces"
        assert len(str(workspace)) < 240
        outside.mkdir()
        environment = clean_release_environment(root)
        environment["MECH_DESIGN_FREECADCMD"] = freecadcmd
        environment["MECH_DESIGN_FREECADCMD_EXPECTED_VERSION"] = expected_version

        wheel, sdist = build_release_artifacts(
            project_root=PROJECT_ROOT,
            root=root,
            environment=environment,
        )
        assert inspect_release_archives(wheel, sdist) == REQUIRED_INSTALLED_RESOURCES

        installed = create_installed_wheel_environment(
            wheel=wheel,
            root=root,
            outside=outside,
            environment=environment,
        )
        inventory = inspect_installed_resources(
            python=installed.python,
            venv=installed.venv,
            cwd=outside,
            environment=environment,
        )
        assert inventory == {
            "all_inside_venv": True,
            "resources": sorted(REQUIRED_INSTALLED_RESOURCES),
            "version": "0.4.1",
        }

        initialized = run_checked(
            [str(installed.cli), "init", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert json.loads(initialized.stdout)["result"] == "initialized"
        created = run_checked(
            [
                str(installed.cli),
                "family",
                "create",
                "--workspace",
                str(workspace),
                "--organization-id",
                "example-org",
                "--organization-name",
                "Example organization",
                "--design-group-id",
                "example-design-group",
                "--design-group-name",
                "Example design group",
                "--family-id",
                "example-family",
                "--family-name",
                "Example family",
                "--set-default",
            ],
            cwd=outside,
            environment=environment,
        )
        assert json.loads(created.stdout)["result"] == "created"

        status = run_checked(
            [str(installed.cli), "status", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert json.loads(status.stdout)["status"] == {"overall": "ok"}
        doctor = run_checked(
            [str(installed.cli), "doctor", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
            expected_returncode=2,
        )
        assert json.loads(doctor.stdout)["status"] == {
            "overall": "setup_required"
        }

        mcp_environment = dict(environment)
        mcp_environment["MECH_DESIGN_WORKSPACE"] = str(workspace)
        installed_contract, mcp_status, mcp_doctor = asyncio.run(
            _installed_mcp_contract(
                installed.mcp,
                cwd=outside,
                environment=mcp_environment,
            )
        )
        assert installed_contract == _source_mcp_contract()
        assert mcp_status["status"] == {"overall": "ok"}
        assert mcp_doctor["status"] == {"overall": "setup_required"}
