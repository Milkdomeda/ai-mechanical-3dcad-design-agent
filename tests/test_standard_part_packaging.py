from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_NAME = "standard_part_providers.json"
CONFIG_SHA256 = "089afe6cbbdae68d72aea60497bf285fc01b1d589dde7379a2a124e346c35464"
EXPECTED_PROVIDER_IDS = [
    "freecad-fasteners", "freecad-gears", "step-parts", "verified-local",
    "manufacturer-official", "3dfindit-cadenas", "misumi", "traceparts",
]
EXPECTED_FASTENER_PROVIDER_IDS = [
    "freecad-fasteners", "step-parts", "verified-local", "manufacturer-official",
    "3dfindit-cadenas", "misumi", "traceparts",
]
CLEAN_ENVIRONMENT_KEYS = (
    "PYTHONPATH", "MECH_DESIGN_WORKSPACE", "MECH_DESIGN_ENV_FILE",
    "MECH_DESIGN_ACTOR_ID", "MECH_DESIGN_DATABASE_URL", "MECH_DESIGN_NEO4J_URI",
    "MECH_DESIGN_NEO4J_USER", "MECH_DESIGN_NEO4J_PASSWORD",
    "MECH_DESIGN_FREECADCMD", "MECH_DESIGN_ARTIFACT_ROOT",
    "MECH_DESIGN_PRODUCT_FAMILY_ID", "MECH_DESIGN_FAMILY_CONFIG",
)


def run_command(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True
    )


def directory_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            "directory" if path.is_dir() else "file",
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )


def installed_registry_result(
    python: Path,
    *,
    workspace: Path,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    installed_environment = {**environment, "PACKAGING_WORKSPACE": str(workspace)}
    script = (
        "import json, os\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from mechanical_design_agent.standard_parts import StandardPartRegistry\n"
        "workspace = Path(os.environ['PACKAGING_WORKSPACE'])\n"
        "registry = StandardPartRegistry(SimpleNamespace(workspace=workspace), object())\n"
        "all_providers = registry.list_providers()\n"
        "fasteners = registry.list_providers('fastener')\n"
        "print(json.dumps({'schema_version': all_providers['schema_version'], "
        "'all_ids': [item['id'] for item in all_providers['providers']], "
        "'fastener_ids': [item['id'] for item in fasteners['providers']], "
        "'catalog_root': None if registry.catalog_root is None else str(registry.catalog_root), "
        "'source_code': registry.sources.code}))\n"
    )
    result = run_command(
        [str(python), "-c", script], cwd=cwd, environment=installed_environment
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def text_content(result: object) -> str:
    content = result.content
    assert len(content) == 1
    value = content[0].text
    assert isinstance(value, str)
    return value


async def installed_standard_part_mcp(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    parameters = StdioServerParameters(command=str(executable), cwd=cwd, env=environment)
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            providers = await session.call_tool(
                "standard_part_providers_get", {},
                read_timeout_seconds=timedelta(seconds=15),
            )
            status = await session.call_tool(
                "standard_part_sources_status", {},
                read_timeout_seconds=timedelta(seconds=15),
            )
            disabled = await session.call_tool(
                "standard_part_catalog_disable", {},
                read_timeout_seconds=timedelta(seconds=15),
            )
    return {
        "tools": sorted(item.name for item in listed.tools),
        "providers": json.loads(text_content(providers)),
        "status": json.loads(text_content(status)),
        "disabled": json.loads(text_content(disabled)),
    }


def test_clean_installed_wheel_standard_part_configuration_lifecycle() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the release wheel"

    with tempfile.TemporaryDirectory(prefix="packaged-standard-part-providers-") as temporary:
        root = Path(temporary)
        dist = root / "dist"
        venv = root / "venv"
        workspace = root / "workspace"
        legacy_workspace = root / "legacy-workspace"
        catalog = root / "verified-catalog"
        outside = root / "outside-repository"
        home = root / "home"
        outside.mkdir()
        home.mkdir()

        environment = dict(os.environ)
        environment["HOME"] = str(home)
        environment.setdefault("UV_CACHE_DIR", str(root / "uv-cache"))
        for name in CLEAN_ENVIRONMENT_KEYS:
            environment.pop(name, None)

        build = run_command(
            [uv, "build", "--wheel", "--out-dir", str(dist)],
            cwd=PROJECT_ROOT,
            environment=environment,
        )
        assert build.returncode == 0, build.stderr
        wheel = next(dist.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            provider_bytes = archive.read(
                f"mechanical_design_agent/resources/config/{CONFIG_NAME}"
            )
        assert "mechanical_design_agent/standard_part_configuration.py" in names
        assert hashlib.sha256(provider_bytes).hexdigest() == CONFIG_SHA256

        created_venv = run_command(
            [uv, "venv", "--python", sys.executable, str(venv)],
            cwd=root,
            environment=environment,
        )
        assert created_venv.returncode == 0, created_venv.stderr
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cli = venv / (
            "Scripts/mechanical-design.exe" if os.name == "nt" else "bin/mechanical-design"
        )
        mcp = venv / (
            "Scripts/mechanical-design-mcp.exe" if os.name == "nt"
            else "bin/mechanical-design-mcp"
        )
        installed = run_command(
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            cwd=root,
            environment=environment,
        )
        assert installed.returncode == 0, installed.stderr

        module_path_result = run_command(
            [str(python), "-c", "import mechanical_design_agent as p; print(p.__file__)"],
            cwd=outside,
            environment=environment,
        )
        assert module_path_result.returncode == 0, module_path_result.stderr
        module_path = Path(module_path_result.stdout.strip()).resolve()
        assert module_path.is_relative_to(venv.resolve())
        assert not module_path.is_relative_to(PROJECT_ROOT.resolve())

        providers_before = run_command(
            [str(cli), "standard-parts", "providers"],
            cwd=outside,
            environment=environment,
        )
        assert providers_before.returncode == 0, providers_before.stderr
        assert [
            item["id"] for item in json.loads(providers_before.stdout)["providers"]
        ] == EXPECTED_PROVIDER_IDS

        initialized = run_command(
            [str(cli), "init", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert initialized.returncode == 0, initialized.stderr
        assert json.loads(initialized.stdout)["result"] == "initialized"
        assert not (workspace / "output").exists()
        assert not (workspace / "knowledge").exists()

        status = run_command(
            [str(cli), "standard-parts", "status", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert status.returncode == 1, status.stderr
        assert json.loads(status.stdout)["code"] == "STANDARD_PART_CATALOG_DISABLED"

        missing = root / "missing-catalog"
        rejected = run_command(
            [str(cli), "standard-parts", "catalog", "enable", "--root", str(missing),
             "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert rejected.returncode == 3, rejected.stderr
        assert json.loads(rejected.stdout)["code"] == "STANDARD_PART_CATALOG_ROOT_NOT_FOUND"
        assert not missing.exists()

        catalog.mkdir()
        (catalog / "fixture.txt").write_text("fixture remains unchanged\n", encoding="utf-8")
        catalog_before = directory_snapshot(catalog)
        enabled = run_command(
            [str(cli), "standard-parts", "catalog", "enable", "--root", str(catalog),
             "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        assert enabled.returncode == 0, enabled.stderr
        assert json.loads(enabled.stdout)["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
        assert directory_snapshot(catalog) == catalog_before

        sources = workspace / "config/standard_parts_sources.json"
        canonical = json.loads(sources.read_text(encoding="utf-8"))
        assert canonical == {
            "schema_version": "StandardPartSources/v1",
            "verified_local_catalog": {
                "enabled": True,
                "global_root": str(catalog.resolve()),
            },
        }
        first_stat = sources.stat()
        first_snapshot = (sources.read_bytes(), first_stat.st_mtime_ns, first_stat.st_ino)
        repeated_enable = run_command(
            [str(cli), "standard-parts", "catalog", "enable", "--root", str(catalog),
             "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        second_stat = sources.stat()
        assert repeated_enable.returncode == 0, repeated_enable.stderr
        assert json.loads(repeated_enable.stdout)["code"] == "STANDARD_PART_CATALOG_ALREADY_CONFIGURED"
        assert (sources.read_bytes(), second_stat.st_mtime_ns, second_stat.st_ino) == first_snapshot

        registry = installed_registry_result(
            python, workspace=workspace, cwd=outside, environment=environment
        )
        assert registry == {
            "schema_version": "StandardPartProviders/v1",
            "all_ids": EXPECTED_PROVIDER_IDS,
            "fastener_ids": EXPECTED_FASTENER_PROVIDER_IDS,
            "catalog_root": str(catalog.resolve()),
            "source_code": "STANDARD_PART_CATALOG_READY",
        }

        mcp_environment = {
            **environment,
            "MECH_DESIGN_WORKSPACE": str(workspace),
            "MECH_DESIGN_MCP_TOOL_PROFILE": "all",
        }
        mcp_result = asyncio.run(
            installed_standard_part_mcp(mcp, cwd=outside, environment=mcp_environment)
        )
        for tool_name in (
            "standard_part_providers_get", "standard_part_sources_status",
            "standard_part_catalog_enable", "standard_part_catalog_disable",
        ):
            assert tool_name in mcp_result["tools"]
        assert [item["id"] for item in mcp_result["providers"]["providers"]] == EXPECTED_PROVIDER_IDS
        assert mcp_result["status"]["code"] == "STANDARD_PART_CATALOG_READY"
        assert mcp_result["disabled"]["code"] == "STANDARD_PART_CATALOG_DISABLED"
        assert directory_snapshot(catalog) == catalog_before

        disabled_stat = sources.stat()
        disabled_snapshot = (
            sources.read_bytes(), disabled_stat.st_mtime_ns, disabled_stat.st_ino
        )
        repeated_disable = run_command(
            [str(cli), "standard-parts", "catalog", "disable", "--workspace", str(workspace)],
            cwd=outside,
            environment=environment,
        )
        final_stat = sources.stat()
        assert repeated_disable.returncode == 0, repeated_disable.stderr
        assert json.loads(repeated_disable.stdout)["code"] == "STANDARD_PART_CATALOG_ALREADY_DISABLED"
        assert (sources.read_bytes(), final_stat.st_mtime_ns, final_stat.st_ino) == disabled_snapshot
        providers_after = run_command(
            [str(cli), "standard-parts", "providers"], cwd=outside, environment=environment
        )
        assert providers_after.returncode == 0, providers_after.stderr
        assert [
            item["id"] for item in json.loads(providers_after.stdout)["providers"]
        ] == EXPECTED_PROVIDER_IDS

        legacy_initialized = run_command(
            [str(cli), "init", "--workspace", str(legacy_workspace)],
            cwd=outside,
            environment=environment,
        )
        assert legacy_initialized.returncode == 0, legacy_initialized.stderr
        legacy_sources = legacy_workspace / "config/standard_parts_sources.json"
        legacy_sources.write_text(
            json.dumps({"verified_local_catalog": {"global_root": str(catalog)}}) + "\n",
            encoding="utf-8",
        )
        legacy_registry = installed_registry_result(
            python, workspace=legacy_workspace, cwd=outside, environment=environment
        )
        assert legacy_registry["catalog_root"] == str(catalog.resolve())
        assert legacy_registry["source_code"] == "STANDARD_PART_SOURCES_LEGACY_FORMAT"
        converted = run_command(
            [str(cli), "standard-parts", "catalog", "enable", "--root", str(catalog),
             "--workspace", str(legacy_workspace)],
            cwd=outside,
            environment=environment,
        )
        assert converted.returncode == 0, converted.stderr
        assert json.loads(converted.stdout)["code"] == "STANDARD_PART_CATALOG_CONFIGURED"
        assert json.loads(legacy_sources.read_text(encoding="utf-8")) == canonical
        assert directory_snapshot(catalog) == catalog_before
        for checked_workspace in (workspace, legacy_workspace):
            assert not (checked_workspace / "output").exists()
            assert not (checked_workspace / "knowledge").exists()
